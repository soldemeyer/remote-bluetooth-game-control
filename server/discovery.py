"""LAN discovery beacon.

Answers "which servers are on this network?" so a player on the same LAN never
has to type an IP address. The client broadcasts a probe; every server on the
subnet replies with its name, port, and current capacity.

Deliberately *not* mDNS/Zeroconf: this needs no extra dependency, no daemon,
and no service registration, and the payload can carry live capacity directly.
A plain broadcast is the right size of tool here.

The beacon reveals only what is already visible to anyone who can reach the
port -- name, port, capacity. It never touches the password, and discovering a
server still gets you nothing without it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket

log = logging.getLogger(__name__)

#: Magic prefix so we ignore unrelated broadcast traffic cheaply.
PROBE_MAGIC = b"RBGC?"
REPLY_MAGIC = b"RBGC!"

DEFAULT_DISCOVERY_PORT = 47801
MAX_REPLY_SIZE = 512


class DiscoveryBeacon:
    """Replies to LAN discovery probes."""

    def __init__(self, config, router) -> None:
        self._config = config
        self._router = router
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()

        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _BeaconProtocol(self._config, self._router),
                local_addr=("0.0.0.0", self._config.discovery_port),
                allow_broadcast=True,
                reuse_port=hasattr(socket, "SO_REUSEPORT"),
            )
        except OSError as exc:
            # Non-fatal: direct connection by address still works.
            log.warning(
                "Could not start LAN discovery on port %d (%s). "
                "Clients can still connect by address.",
                self._config.discovery_port,
                exc,
            )
            return

        log.info("LAN discovery beacon on UDP %d", self._config.discovery_port)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None


class _BeaconProtocol(asyncio.DatagramProtocol):
    def __init__(self, config, router) -> None:
        self._config = config
        self._router = router
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if not data.startswith(PROBE_MAGIC) or self._transport is None:
            return

        # The beacon belongs to the LAN transport: stay silent unless LAN
        # connections are switched on *and* set to visible. Not answering is the
        # whole of hidden mode -- a client that already knows the address and
        # password still connects, the server just does not announce itself.
        if not getattr(self._config, "lan_enabled", True):
            return
        if not getattr(self._config, "lan_discoverable", True):
            return

        payload = json.dumps(
            {
                "name": self._config.server_name,
                "port": self._config.port,
                "capacity": self._router.capacity,
                "in_use": sum(1 for c in self._router.channels() if c.is_assigned),
            },
            separators=(",", ":"),
        ).encode("utf-8")

        if len(payload) + len(REPLY_MAGIC) > MAX_REPLY_SIZE:
            return

        try:
            self._transport.sendto(REPLY_MAGIC + payload, addr)
        except OSError as exc:
            log.debug("Could not reply to discovery probe from %s: %s", addr, exc)

    def error_received(self, exc: Exception) -> None:
        log.debug("Discovery socket error: %s", exc)


# --------------------------------------------------------------------------
# Client side
# --------------------------------------------------------------------------


async def discover_servers(
    timeout: float = 1.5, port: int = DEFAULT_DISCOVERY_PORT
) -> list[dict]:
    """Broadcast a probe and collect replies.

    Returns one entry per server: ``host``, ``port``, ``name``, ``capacity``,
    ``in_use``. Never raises -- discovery failing is not a reason to prevent a
    manual connection, so callers get an empty list instead of an exception.
    """
    loop = asyncio.get_running_loop()
    found: dict[str, dict] = {}
    done = loop.create_future()

    class _Probe(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            if not data.startswith(REPLY_MAGIC):
                return
            try:
                info = json.loads(data[len(REPLY_MAGIC):].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return

            found[addr[0]] = {
                "host": addr[0],
                "port": int(info.get("port", 47800)),
                "name": str(info.get("name", addr[0]))[:64],
                "capacity": int(info.get("capacity", 0)),
                "in_use": int(info.get("in_use", 0)),
            }

    try:
        transport, _ = await loop.create_datagram_endpoint(
            _Probe, local_addr=("0.0.0.0", 0), allow_broadcast=True
        )
    except OSError as exc:
        log.debug("Could not open discovery socket: %s", exc)
        return []

    try:
        for address in _broadcast_addresses():
            try:
                transport.sendto(PROBE_MAGIC, (address, port))
            except OSError:
                continue

        await asyncio.wait([done], timeout=timeout)
    finally:
        transport.close()

    return sorted(found.values(), key=lambda entry: entry["name"])


def _broadcast_addresses() -> list[str]:
    """Broadcast targets to probe.

    The global 255.255.255.255 is blocked by some routers and by Windows
    Firewall profiles, so we also probe each interface's directed broadcast,
    which is more reliably delivered.
    """
    addresses = {"255.255.255.255"}

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            octets = ip.split(".")
            if len(octets) == 4:
                # Assume /24. Correct for essentially every home network, and a
                # wrong guess only costs one unanswered datagram.
                addresses.add(f"{octets[0]}.{octets[1]}.{octets[2]}.255")
    except (OSError, socket.gaierror):
        pass

    return sorted(addresses)

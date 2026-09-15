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
import struct
import sys

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

    targets = _broadcast_addresses()
    sent = 0
    try:
        for address in targets:
            try:
                transport.sendto(PROBE_MAGIC, (address, port))
            except OSError as exc:
                # Kept rather than swallowed: "every send failed" and "nobody
                # answered" are different faults and looked identical, because
                # both produced an empty list and one `continue`.
                log.debug("Probe to %s failed: %s", address, exc)
                continue
            sent += 1

        if not sent:
            log.warning(
                "Discovery could not send to any of %d broadcast address(es): %s",
                len(targets), ", ".join(targets),
            )

        await asyncio.wait([done], timeout=timeout)
    finally:
        transport.close()

    log.debug(
        "Discovery probed %s and found %d server(s)",
        ", ".join(targets), len(found),
    )

    return sorted(found.values(), key=lambda entry: entry["name"])


#: Linux ioctl for "what is this interface's broadcast address". Asking the
#: kernel beats deriving one: it is exact where a /24 guess is not, and it is
#: the only source that works when the hostname does not resolve to a real
#: address -- which on Linux it usually does not.
_SIOCGIFBRDADDR = 0x8919


def _kernel_broadcast_addresses() -> set[str]:
    """Each interface's broadcast address, straight from the kernel.

    Linux only, because the ioctl number is. Empty everywhere else, which
    leaves the other two sources to do the work.
    """
    if not sys.platform.startswith("linux"):
        return set()

    try:
        import fcntl
    except ImportError:  # pragma: no cover - fcntl is stdlib on linux
        return set()

    found: set[str] = set()
    try:
        interfaces = socket.if_nameindex()
    except OSError:
        return found

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _index, name in interfaces:
            if name == "lo":
                continue
            try:
                packed = struct.pack("256s", name[:15].encode("utf-8"))
                result = fcntl.ioctl(probe.fileno(), _SIOCGIFBRDADDR, packed)
            except OSError:
                # No broadcast address: a point-to-point link, a down
                # interface, or one with no IPv4 on it. Not an error.
                continue
            address = socket.inet_ntoa(result[20:24])
            if address != "0.0.0.0":
                found.add(address)
    finally:
        probe.close()
    return found


def _primary_address() -> str | None:
    """This machine's address on the route out, or None.

    `connect` on a UDP socket sends nothing -- it only fixes the local end --
    so this asks the routing table which address would be used and costs no
    traffic. Portable, and the answer is a real interface address even where
    the hostname resolves to loopback.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))      # TEST-NET-1; nothing is sent
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def _broadcast_addresses() -> list[str]:
    """Broadcast targets to probe.

    Three sources, because each covers a case the others miss:

    * **The kernel's own answer per interface** (Linux). Exact, including the
      prefix length, and it needs nothing to resolve.
    * **The route-out address**, /24. Portable, and the fallback that makes
      this work on Windows and macOS.
    * **The hostname's addresses**, /24. What this used to do, kept because it
      can name an interface that is not the default route.

    **The hostname alone was not enough, and on Linux it found nothing at
    all.** Debian and Ubuntu map the hostname to `127.0.1.1` in `/etc/hosts`,
    which is loopback and skipped -- so the only target left was the global
    255.255.255.255, which this function's own comment already records as the
    one routers drop. Measured on a Linux client: hostname resolved to
    127.0.1.1, targets came out as `['255.255.255.255']`, the real interface at
    172.26.132.139/20 was never probed, and every search returned nothing with
    no error anywhere.

    The global address is still probed. It reaches some networks the directed
    ones do not, and an unanswered datagram costs nothing.
    """
    addresses = {"255.255.255.255"}
    addresses |= _kernel_broadcast_addresses()

    candidates = []
    primary = _primary_address()
    if primary:
        candidates.append(primary)
    try:
        hostname = socket.gethostname()
        candidates += [
            info[4][0] for info in socket.getaddrinfo(hostname, None, socket.AF_INET)
        ]
    except (OSError, socket.gaierror):
        pass

    for ip in candidates:
        if ip.startswith("127."):
            continue
        octets = ip.split(".")
        if len(octets) == 4:
            # Assume /24. Correct for essentially every home network, and a
            # wrong guess only costs one unanswered datagram -- where the
            # kernel's answer above is exact when it is available.
            addresses.add(f"{octets[0]}.{octets[1]}.{octets[2]}.255")

    return sorted(addresses)

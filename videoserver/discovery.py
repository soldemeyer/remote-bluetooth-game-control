"""LAN discovery for video servers.

The same shape as ``server/discovery.py`` -- a magic-prefixed UDP broadcast,
no dependency and no daemon -- but a separate magic and port, because the two
answer different questions. A player asks "which consoles can I play on?"; the
Bluetooth server's operator asks "which machines can send me a picture?"

Sharing one beacon would mean a client's probe getting video servers back and a
video server having to know about controller capacity, so they stay apart.

**The reply carries no credential and proves nothing.** It says a video server
exists, its name, and where to reach it. Connecting still needs the password,
which is exactly the situation a discovered Bluetooth server is in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket

log = logging.getLogger(__name__)

#: Distinct from the gameplay beacon's RBGC?/RBGC! so the two never answer
#: each other's probes.
PROBE_MAGIC = b"RBGCV?"
REPLY_MAGIC = b"RBGCV!"

DEFAULT_DISCOVERY_PORT = 47811
MAX_REPLY_SIZE = 512


class VideoDiscoveryBeacon:
    """Answers "is there a video server here?" on the LAN."""

    def __init__(self, app, port: int = DEFAULT_DISCOVERY_PORT, name: str = "") -> None:
        self._app = app
        self._port = port
        self._name = name
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _BeaconProtocol(self._app, self._name),
                local_addr=("0.0.0.0", self._port),
                allow_broadcast=True,
                reuse_port=hasattr(socket, "SO_REUSEPORT"),
            )
        except OSError as exc:
            # Non-fatal, exactly as on the gameplay side: the operator can
            # always type the address in, and refusing to run because we
            # cannot be *found* would be a poor trade.
            log.warning(
                "Could not start video discovery on port %d (%s). "
                "The Bluetooth server can still be pointed here by address.",
                self._port,
                exc,
            )
            return
        log.info("Video discovery beacon on UDP %d", self._port)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None


class _BeaconProtocol(asyncio.DatagramProtocol):
    def __init__(self, app, name: str) -> None:
        self._app = app
        self._name = name
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if not data.startswith(PROBE_MAGIC) or self._transport is None:
            return

        try:
            payload = REPLY_MAGIC + json.dumps(
                self._describe(), separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError):
            log.debug("Could not build a discovery reply", exc_info=True)
            return

        if len(payload) > MAX_REPLY_SIZE:
            log.debug("Discovery reply too large; not sending")
            return

        try:
            self._transport.sendto(payload, addr)
        except OSError as exc:
            log.debug("Discovery reply to %s failed: %s", addr, exc)

    def _describe(self) -> dict:
        """What we are willing to tell an unauthenticated stranger.

        Enough to pick this machine out of a list, and nothing that would help
        anyone connect without the password.
        """
        status: dict = {}
        try:
            status = self._app.status()
        except Exception:
            log.debug("Could not read status for the beacon", exc_info=True)

        return {
            "name": self._name or socket.gethostname(),
            "port": int(getattr(self._app.net, "port", 0) or 0),
            "streaming": bool(status.get("streaming")),
            "encoder": str(status.get("encoder", ""))[:32],
            "width": int(status.get("width", 0) or 0),
            "height": int(status.get("height", 0) or 0),
            # So the operator can see at a glance whether the one they found is
            # already serving somebody.
            "clients": int(status.get("clients", 0) or 0),
        }


# --------------------------------------------------------------------------
# Probe side -- used by the Bluetooth server's web GUI
# --------------------------------------------------------------------------


async def discover_video_servers(
    timeout: float = 1.5, port: int = DEFAULT_DISCOVERY_PORT
) -> list[dict]:
    """Broadcast a probe and collect replies. Never raises.

    Returns one entry per video server found, keyed by address so a machine
    that answers on several interfaces appears once.
    """
    loop = asyncio.get_running_loop()
    found: dict[str, dict] = {}

    class _Probe(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            if not data.startswith(REPLY_MAGIC):
                return
            try:
                info = json.loads(data[len(REPLY_MAGIC) :].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            if not isinstance(info, dict):
                return

            found[addr[0]] = {
                "host": addr[0],
                "port": _small_int(info.get("port"), 47810),
                "name": str(info.get("name", addr[0]))[:64],
                "streaming": bool(info.get("streaming")),
                "encoder": str(info.get("encoder", ""))[:32],
                "width": _small_int(info.get("width"), 0),
                "height": _small_int(info.get("height"), 0),
                "clients": _small_int(info.get("clients"), 0),
            }

    try:
        transport, _ = await loop.create_datagram_endpoint(
            _Probe, local_addr=("0.0.0.0", 0), allow_broadcast=True
        )
    except OSError as exc:
        log.debug("Could not open the video discovery socket: %s", exc)
        return []

    try:
        for address in _broadcast_addresses():
            try:
                transport.sendto(PROBE_MAGIC, (address, port))
            except OSError:
                continue
        await asyncio.sleep(timeout)
    finally:
        transport.close()

    return sorted(found.values(), key=lambda entry: entry["name"].lower())


def _small_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _broadcast_addresses() -> list[str]:
    """Broadcast targets to probe.

    The global 255.255.255.255 is blocked by some routers and by Windows
    Firewall profiles, so each interface's directed broadcast is probed too.
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
                addresses.add(f"{octets[0]}.{octets[1]}.{octets[2]}.255")
    except (OSError, socket.gaierror):
        pass

    return sorted(addresses)

"""Rendezvous broker for UDP NAT hole-punching.

Two peers behind NAT cannot find each other unaided: neither knows the other's
external address, and neither can accept an unsolicited inbound packet. This
service is the mutually-reachable third party that fixes both problems.

    1. Server and client each send REGISTER to the broker from the *same*
       socket they will later use for gameplay. The broker sees the external
       (IP, port) their NAT assigned.
    2. When both sides of a room are present, the broker sends each peer the
       other's external address.
    3. Both peers immediately send UDP to each other. The outbound packets
       open their respective NAT mappings, so the packets crossing in flight
       are accepted. This is the "punch".
    4. If punching fails -- symmetric NAT on both ends, realistically 10-20%
       of cases -- the broker relays instead. Higher latency, but it works.

The broker never sees the game password and cannot decrypt session traffic; it
only ever learns which addresses want to talk. Relayed packets pass through
opaque, since they are already end-to-end encrypted.

Deployment: needs a publicly reachable UDP port. A $5 VPS is plenty -- the
signalling load is negligible, and only relayed sessions consume bandwidth.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field

log = logging.getLogger("rbgc.broker")

DEFAULT_PORT = 47900

#: Peers must re-register within this or they are forgotten. Also keeps their
#: NAT mapping to the broker alive.
PEER_TTL_S = 45.0

#: How long a relay session stays alive with no traffic.
RELAY_TTL_S = 120.0

MAX_ROOMS = 256
MAX_MESSAGE = 1024

ROLE_SERVER = "server"
ROLE_CLIENT = "client"


@dataclass(slots=True)
class Peer:
    role: str
    address: tuple[str, int]
    last_seen: float = field(default_factory=time.monotonic)

    #: Address the peer reports for itself on its own LAN. Sent to the other
    #: side so two peers behind the *same* NAT can connect directly instead of
    #: hairpinning through the router, which many home routers do badly.
    local_address: tuple[str, int] | None = None

    @property
    def is_stale(self) -> bool:
        return time.monotonic() - self.last_seen > PEER_TTL_S


@dataclass(slots=True)
class Room:
    code: str
    server: Peer | None = None
    clients: dict[tuple[str, int], Peer] = field(default_factory=dict)
    created: float = field(default_factory=time.monotonic)

    #: Set once a pair reports that punching failed and asks us to relay.
    relaying: set[tuple[tuple[str, int], tuple[str, int]]] = field(default_factory=set)

    def prune(self) -> None:
        if self.server is not None and self.server.is_stale:
            log.info("Room %s: server registration expired", self.code)
            self.server = None
        for address in [a for a, p in self.clients.items() if p.is_stale]:
            log.info("Room %s: client %s expired", self.code, address)
            del self.clients[address]

    @property
    def is_empty(self) -> bool:
        return self.server is None and not self.clients


class BrokerProtocol(asyncio.DatagramProtocol):
    """Signalling and optional relay."""

    def __init__(self) -> None:
        self._transport: asyncio.DatagramTransport | None = None
        self._rooms: dict[str, Room] = {}
        #: Relay routing: source address -> destination address.
        self._relay_routes: dict[tuple[str, int], tuple[str, int]] = {}
        self._relay_seen: dict[tuple[str, int], float] = {}

        self.packets_signalled = 0
        self.packets_relayed = 0

    def connection_made(self, transport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, address: tuple[str, int]) -> None:
        # Relay traffic is the common case once a session falls back, so check
        # it first and forward without parsing.
        destination = self._relay_routes.get(address)
        if destination is not None:
            self._relay_seen[address] = time.monotonic()
            self.packets_relayed += 1
            if self._transport:
                self._transport.sendto(data, destination)
            return

        if len(data) > MAX_MESSAGE:
            return

        try:
            message = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(message, dict):
            return

        self.packets_signalled += 1
        op = message.get("op")

        if op == "register":
            self._handle_register(message, address)
        elif op == "relay":
            self._handle_relay_request(message, address)
        elif op == "bye":
            self._handle_bye(address)

    # -- signalling --------------------------------------------------------

    def _handle_register(self, message: dict, address: tuple[str, int]) -> None:
        code = str(message.get("room", ""))[:64]
        role = message.get("role")

        if not code or role not in (ROLE_SERVER, ROLE_CLIENT):
            self._send(address, {"op": "error", "reason": "bad registration"})
            return

        room = self._rooms.get(code)
        if room is None:
            if len(self._rooms) >= MAX_ROOMS:
                self._prune_all()
                if len(self._rooms) >= MAX_ROOMS:
                    self._send(address, {"op": "error", "reason": "broker is full"})
                    return
            room = Room(code=code)
            self._rooms[code] = room

        room.prune()

        local = message.get("local")
        local_address = None
        if isinstance(local, list) and len(local) == 2:
            try:
                local_address = (str(local[0]), int(local[1]))
            except (TypeError, ValueError):
                local_address = None

        peer = Peer(role=role, address=address, local_address=local_address)

        if role == ROLE_SERVER:
            if room.server is None or room.server.address != address:
                log.info("Room %s: server registered from %s", code, address)
            room.server = peer
        else:
            if address not in room.clients:
                log.info("Room %s: client registered from %s", code, address)
            room.clients[address] = peer

        # Confirm, echoing the external address we observed. Peers use this to
        # detect that they are behind NAT at all.
        self._send(address, {"op": "registered", "external": list(address), "room": code})

        self._try_introduce(room)

    def _try_introduce(self, room: Room) -> None:
        """Once both sides are present, tell each about the other."""
        if room.server is None or not room.clients:
            return

        for client in room.clients.values():
            self._send(
                room.server.address,
                {
                    "op": "peer",
                    "role": ROLE_CLIENT,
                    "address": list(client.address),
                    "local": list(client.local_address) if client.local_address else None,
                },
            )
            self._send(
                client.address,
                {
                    "op": "peer",
                    "role": ROLE_SERVER,
                    "address": list(room.server.address),
                    "local": list(room.server.local_address)
                    if room.server.local_address
                    else None,
                },
            )

    def _handle_relay_request(self, message: dict, address: tuple[str, int]) -> None:
        """Set up relaying after a failed punch.

        Only wired up when a peer explicitly asks, so we never relay traffic
        that could have gone direct -- relay costs latency and our bandwidth.
        """
        peer_raw = message.get("peer")
        if not isinstance(peer_raw, list) or len(peer_raw) != 2:
            return
        try:
            peer_address = (str(peer_raw[0]), int(peer_raw[1]))
        except (TypeError, ValueError):
            return

        # Only relay between two peers that both registered in the same room.
        if not self._share_a_room(address, peer_address):
            self._send(address, {"op": "error", "reason": "peer not in your room"})
            return

        self._relay_routes[address] = peer_address
        self._relay_routes[peer_address] = address
        now = time.monotonic()
        self._relay_seen[address] = now
        self._relay_seen[peer_address] = now

        log.info("Relaying %s <-> %s (hole-punch failed)", address, peer_address)
        self._send(address, {"op": "relaying", "peer": list(peer_address)})
        self._send(peer_address, {"op": "relaying", "peer": list(address)})

    def _share_a_room(self, a: tuple[str, int], b: tuple[str, int]) -> bool:
        for room in self._rooms.values():
            addresses = set(room.clients)
            if room.server is not None:
                addresses.add(room.server.address)
            if a in addresses and b in addresses:
                return True
        return False

    def _handle_bye(self, address: tuple[str, int]) -> None:
        for room in list(self._rooms.values()):
            if room.server is not None and room.server.address == address:
                room.server = None
            room.clients.pop(address, None)
            if room.is_empty:
                del self._rooms[room.code]

        destination = self._relay_routes.pop(address, None)
        if destination is not None:
            self._relay_routes.pop(destination, None)
            self._relay_seen.pop(destination, None)
        self._relay_seen.pop(address, None)

    # -- housekeeping ------------------------------------------------------

    def _prune_all(self) -> None:
        for code in list(self._rooms):
            room = self._rooms[code]
            room.prune()
            if room.is_empty:
                del self._rooms[code]

        now = time.monotonic()
        for address in [a for a, t in self._relay_seen.items() if now - t > RELAY_TTL_S]:
            destination = self._relay_routes.pop(address, None)
            if destination is not None:
                self._relay_routes.pop(destination, None)
                self._relay_seen.pop(destination, None)
            self._relay_seen.pop(address, None)
            log.info("Relay session %s expired", address)

    def _send(self, address: tuple[str, int], message: dict) -> None:
        if self._transport is None:
            return
        try:
            self._transport.sendto(
                json.dumps(message, separators=(",", ":")).encode("utf-8"), address
            )
        except OSError as exc:
            log.debug("Send to %s failed: %s", address, exc)

    def stats(self) -> dict:
        return {
            "rooms": len(self._rooms),
            "relay_sessions": len(self._relay_routes) // 2,
            "packets_signalled": self.packets_signalled,
            "packets_relayed": self.packets_relayed,
        }


async def run_broker(host: str, port: int) -> int:
    loop = asyncio.get_running_loop()

    protocol = BrokerProtocol()
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol, local_addr=(host, port)
        )
    except OSError as exc:
        print(f"error: could not bind {host}:{port} ({exc})", file=sys.stderr)
        return 1

    print(f"\n  RBGC rendezvous broker listening on udp://{host}:{port}\n")

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop.set())

    async def housekeeping() -> None:
        while not stop.is_set():
            await asyncio.sleep(30)
            protocol._prune_all()
            stats = protocol.stats()
            if stats["rooms"] or stats["relay_sessions"]:
                log.info(
                    "rooms=%d relays=%d signalled=%d relayed=%d",
                    stats["rooms"],
                    stats["relay_sessions"],
                    stats["packets_signalled"],
                    stats["packets_relayed"],
                )

    task = asyncio.create_task(housekeeping())
    try:
        await stop.wait()
    finally:
        task.cancel()
        transport.close()

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rbgc-broker",
        description="Rendezvous broker for RBGC NAT hole-punching.",
        epilog=(
            "Run this on a machine with a public IP. It carries only signalling "
            "traffic unless a pair falls back to relay mode."
        ),
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("RBGC_BROKER_PORT", DEFAULT_PORT)))
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        return asyncio.run(run_broker(args.host, args.port))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

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
import secrets
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

#: Prefix on a relayed datagram, followed by the sender's 16-byte token.
#:
#: Relay used to be routed purely on the observed source address, which is
#: exactly what a proxy in front of us destroys: two peers arriving from one
#: address collapse to a single route and their traffic is misdelivered. A
#: token identifies the flow by content instead, so relaying works wherever the
#: broker happens to live.
#:
#: Chosen not to collide with anything else that reaches this socket: JSON
#: signalling starts with `{`, and the punch probes have their own prefixes.
RELAY_MAGIC = b"RBGR"
RELAY_TOKEN_BYTES = 16
_RELAY_HEADER = len(RELAY_MAGIC) + RELAY_TOKEN_BYTES

ROLE_SERVER = "server"
ROLE_CLIENT = "client"

#: Video peers share a room with the gameplay pair but form their own
#: introduction leg: a video source is not a game server, and a viewer is not a
#: player. Keeping them as distinct roles rather than overloading the existing
#: two means a room can hold both at once, which is the normal case.
ROLE_VIDEO_SOURCE = "video-source"
ROLE_VIDEO_CLIENT = "video-client"

_ALL_ROLES = (ROLE_SERVER, ROLE_CLIENT, ROLE_VIDEO_SOURCE, ROLE_VIDEO_CLIENT)


def _parse_reported_address(value) -> tuple[str, int] | None:
    """An ``[ip, port]`` a peer reported about itself, or None.

    Self-reported and unverifiable by us, which is deliberate: we are passing on
    a candidate, not vouching for it. Bounded so a malformed or hostile
    registration cannot put anything unreasonable on the wire to the other peer.
    """
    if not isinstance(value, list) or len(value) != 2:
        return None
    try:
        host = str(value[0])[:45]          # an IPv6 literal is at most 45 chars
        port = int(value[1])
    except (TypeError, ValueError):
        return None
    if not host or not (1 <= port <= 65535):
        return None
    return (host, port)


def _peers_of(room: "Room"):
    """Every Peer registered in a room, whatever its role."""
    if room.server is not None:
        yield room.server
    if room.video is not None:
        yield room.video
    yield from room.clients.values()
    yield from room.video_clients.values()


def _introduction(role: str, peer: Peer) -> dict:
    """What one side is told about the other.

    Three candidates, tried in that order by the peer:

      * ``local``   -- its LAN address, for two peers behind the same NAT;
      * ``public``  -- what it learned about itself from STUN, which is the only
        one that survives us sitting behind a proxy;
      * ``address`` -- what we observed, which is the proxy's when there is one.

    All three are sent whenever known. They usually agree (``public`` equals
    ``address`` on a directly-reachable broker), and the peer skips duplicates,
    so the common path is unchanged.
    """
    return {
        "op": "peer",
        "role": role,
        "address": list(peer.address),
        "local": list(peer.local_address) if peer.local_address else None,
        "public": list(peer.public_address) if peer.public_address else None,
    }


def _small_int(value, limit: int = 64) -> int:
    """Coerce untrusted JSON to a small non-negative int.

    Listing fields are cosmetic, so anything unparseable becomes 0 rather than
    rejecting the whole registration.
    """
    try:
        return max(0, min(int(value), limit))
    except (TypeError, ValueError):
        return 0


@dataclass(slots=True)
class Peer:
    role: str
    address: tuple[str, int]
    last_seen: float = field(default_factory=time.monotonic)

    #: Address the peer reports for itself on its own LAN. Sent to the other
    #: side so two peers behind the *same* NAT can connect directly instead of
    #: hairpinning through the router, which many home routers do badly.
    local_address: tuple[str, int] | None = None

    #: Public address the peer learned for itself from a STUN server, if it
    #: could. Passed through untouched -- we neither verify it nor need to
    #: understand it.
    #:
    #: This is what lets the broker run behind a proxy. `address` above is what
    #: *we* observed, and an L4 proxy, an frp tunnel or Docker's userland proxy
    #: all rewrite it to their own, which is useless to punch at. A peer that
    #: asked a directly-reachable STUN server knows the real one.
    public_address: tuple[str, int] | None = None

    #: Issued at registration and echoed back in relayed packets, so a relay
    #: can be routed without trusting the source address.
    token: str = ""

    @property
    def is_stale(self) -> bool:
        return time.monotonic() - self.last_seen > PEER_TTL_S


@dataclass(slots=True)
class Room:
    code: str
    server: Peer | None = None
    clients: dict[tuple[str, int], Peer] = field(default_factory=dict)
    created: float = field(default_factory=time.monotonic)

    #: Name the server advertises, when it opted in to being listed. Empty for
    #: hidden servers, which register normally but never appear in a listing --
    #: a client must already know the room code to reach them.
    public_name: str = ""

    #: Live capacity, purely informational for the listing.
    capacity: int = 0
    in_use: int = 0

    #: Set once a pair reports that punching failed and asks us to relay.
    relaying: set[tuple[tuple[str, int], tuple[str, int]]] = field(default_factory=set)

    #: The video source serving this room, and everyone watching it. Separate
    #: from server/clients above: the same machine may hold both a gameplay and
    #: a video registration, from two different sockets.
    video: Peer | None = None
    video_clients: dict[tuple[str, int], Peer] = field(default_factory=dict)

    def prune(self) -> None:
        if self.server is not None and self.server.is_stale:
            log.info("Room %s: server registration expired", self.code)
            self.server = None
        for address in [a for a, p in self.clients.items() if p.is_stale]:
            log.info("Room %s: client %s expired", self.code, address)
            del self.clients[address]
        if self.video is not None and self.video.is_stale:
            log.info("Room %s: video source registration expired", self.code)
            self.video = None
        for address in [a for a, p in self.video_clients.items() if p.is_stale]:
            log.info("Room %s: video client %s expired", self.code, address)
            del self.video_clients[address]

    @property
    def is_empty(self) -> bool:
        return (
            self.server is None
            and not self.clients
            and self.video is None
            and not self.video_clients
        )

    def addresses(self) -> set[tuple[str, int]]:
        """Every endpoint registered here, whatever its role."""
        found = set(self.clients) | set(self.video_clients)
        if self.server is not None:
            found.add(self.server.address)
        if self.video is not None:
            found.add(self.video.address)
        return found


class BrokerProtocol(asyncio.DatagramProtocol):
    """Signalling and optional relay."""

    def __init__(self) -> None:
        self._transport: asyncio.DatagramTransport | None = None
        self._rooms: dict[str, Room] = {}
        #: Relay routing: source address -> destination address.
        self._relay_routes: dict[tuple[str, int], tuple[str, int]] = {}
        self._relay_seen: dict[tuple[str, int], float] = {}

        #: token -> the address that token was last seen arriving from, and the
        #: token-to-token pairing that says where to forward. Kept apart from
        #: the address table above so an un-upgraded peer still relays the old
        #: way, and so a peer whose address changes (a NAT rebind, or a proxy
        #: re-flowing) keeps its route rather than losing it.
        self._tokens: dict[str, tuple[str, int]] = {}
        self._token_routes: dict[str, str] = {}

        self.packets_signalled = 0
        self.packets_relayed = 0

    def connection_made(self, transport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, address: tuple[str, int]) -> None:
        # Relay traffic is the common case once a session falls back, so check
        # it first and forward without parsing.
        # Token-framed relay first. One four-byte compare, and it is what makes
        # relaying work when everything arrives from the same proxy address.
        if data[: len(RELAY_MAGIC)] == RELAY_MAGIC:
            self._relay_by_token(data, address)
            return

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
        elif op == "list":
            self._handle_list(address)
        elif op == "relay":
            self._handle_relay_request(message, address)
        elif op == "bye":
            self._handle_bye(address)

    # -- signalling --------------------------------------------------------

    def _handle_register(self, message: dict, address: tuple[str, int]) -> None:
        code = str(message.get("room", ""))[:64]
        role = message.get("role")

        if not code or role not in _ALL_ROLES:
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

        # Before the peer object is replaced below, or this finds the new one
        # with a blank token and mints another. Peers re-register every 20 s,
        # so a token that did not survive that would drop a live relay.
        existing_token = self._token_for(address, code)

        local_address = _parse_reported_address(message.get("local"))
        public_address = _parse_reported_address(message.get("public"))

        peer = Peer(
            role=role,
            address=address,
            local_address=local_address,
            public_address=public_address,
        )

        if role == ROLE_SERVER:
            if room.server is None or room.server.address != address:
                log.info("Room %s: server registered from %s", code, address)
            room.server = peer

            # A server is listed only if it explicitly sends a name. Absent or
            # blank means hidden: it still registers and can still be reached by
            # anyone who knows the room code, but it is never enumerated.
            name = message.get("name")
            room.public_name = str(name)[:64] if isinstance(name, str) else ""
            room.capacity = _small_int(message.get("capacity"))
            room.in_use = _small_int(message.get("in_use"))
        elif role == ROLE_VIDEO_SOURCE:
            if room.video is None or room.video.address != address:
                log.info("Room %s: video source registered from %s", code, address)
            room.video = peer
        elif role == ROLE_VIDEO_CLIENT:
            if address not in room.video_clients:
                log.info("Room %s: video client registered from %s", code, address)
            room.video_clients[address] = peer
        else:
            if address not in room.clients:
                log.info("Room %s: client registered from %s", code, address)
            room.clients[address] = peer

        peer.token = existing_token or secrets.token_hex(RELAY_TOKEN_BYTES)
        self._tokens[peer.token] = address

        # Confirm, echoing the external address we observed. Peers use this to
        # detect that they are behind NAT at all, and carry the relay token in
        # case punching fails later.
        self._send(
            address,
            {
                "op": "registered",
                "external": list(address),
                "room": code,
                "token": peer.token,
            },
        )

        self._try_introduce(room)
        self._try_introduce_video(room)

    #: Cap on a listing reply, so one datagram cannot be amplified without
    #: bound. A client that needs more should be told to use a room code.
    MAX_LISTED = 32

    def _handle_list(self, address: tuple[str, int]) -> None:
        """Answer a client browsing for public servers.

        Only servers that opted in by sending a name are returned, and only the
        name, room code and capacity -- never an endpoint. A client still has to
        register into the room and be introduced, so listing reveals nothing
        that would let a stranger reach a server directly, and never reveals a
        password (the broker has never seen one).
        """
        self._prune_all()

        servers = [
            {
                "room": room.code,
                "name": room.public_name,
                "capacity": room.capacity,
                "in_use": room.in_use,
            }
            for room in self._rooms.values()
            if room.public_name and room.server is not None
        ]
        servers.sort(key=lambda entry: entry["name"].lower())

        self._send(address, {"op": "servers", "servers": servers[: self.MAX_LISTED]})

    def _try_introduce(self, room: Room) -> None:
        """Once both sides are present, tell each about the other."""
        if room.server is None or not room.clients:
            return

        for client in room.clients.values():
            self._send(room.server.address, _introduction(ROLE_CLIENT, client))
            self._send(client.address, _introduction(ROLE_SERVER, room.server))

    def _try_introduce_video(self, room: Room) -> None:
        """The same introduction, for the video leg of the room.

        Kept separate from _try_introduce rather than generalised: the two legs
        pair different roles, and a room routinely has one without the other
        (video off, or a viewer who is not playing).
        """
        if room.video is None or not room.video_clients:
            return

        for viewer in room.video_clients.values():
            self._send(room.video.address, _introduction(ROLE_VIDEO_CLIENT, viewer))
            self._send(viewer.address, _introduction(ROLE_VIDEO_SOURCE, room.video))

    def _relay_by_token(self, data: bytes, address: tuple[str, int]) -> None:
        """Forward one token-framed relay packet.

        The sender's token identifies *it*, so the payload goes to whoever it
        was paired with -- and the address it arrived from is recorded, which
        is how the reply finds its way back down a proxy's existing flow.
        """
        if len(data) < _RELAY_HEADER:
            return

        token = data[len(RELAY_MAGIC) : _RELAY_HEADER].hex()
        peer_token = self._token_routes.get(token)
        if peer_token is None:
            return

        # Where this peer is *now*. Learning it from every packet is what keeps
        # a relay alive across a NAT rebind.
        self._tokens[token] = address
        destination = self._tokens.get(peer_token)
        if destination is None:
            return

        now = time.monotonic()
        self._relay_seen[address] = now
        self._relay_seen[destination] = now
        self.packets_relayed += 1
        if self._transport:
            self._transport.sendto(data[_RELAY_HEADER:], destination)

    def _token_of(self, address: tuple[str, int]) -> str:
        """The relay token issued to whoever is registered at this address."""
        for room in self._rooms.values():
            for peer in _peers_of(room):
                if peer.address == address and peer.token:
                    return peer.token
        return ""

    def _token_for(self, address: tuple[str, int], code: str) -> str:
        """The token already issued to this address, if it still holds one."""
        for room in (self._rooms.get(code),):
            if room is None:
                continue
            for peer in _peers_of(room):
                if peer.address == address and peer.token:
                    return peer.token
        return ""

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

        # And pair them by token, which is the route that survives a proxy.
        mine = self._token_of(address)
        theirs = self._token_of(peer_address)
        if mine and theirs:
            self._token_routes[mine] = theirs
            self._token_routes[theirs] = mine
            self._tokens[mine] = address
            self._tokens[theirs] = peer_address
        now = time.monotonic()
        self._relay_seen[address] = now
        self._relay_seen[peer_address] = now

        log.info("Relaying %s <-> %s (hole-punch failed)", address, peer_address)
        self._send(address, {"op": "relaying", "peer": list(peer_address)})
        self._send(peer_address, {"op": "relaying", "peer": list(address)})

    def _share_a_room(self, a: tuple[str, int], b: tuple[str, int]) -> bool:
        for room in self._rooms.values():
            addresses = room.addresses()
            if a in addresses and b in addresses:
                return True
        return False

    def _handle_bye(self, address: tuple[str, int]) -> None:
        for room in list(self._rooms.values()):
            if room.server is not None and room.server.address == address:
                room.server = None
            if room.video is not None and room.video.address == address:
                room.video = None
            room.clients.pop(address, None)
            room.video_clients.pop(address, None)
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

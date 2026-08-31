"""UDP NAT hole-punching against the rendezvous broker.

The punch has to happen on the *same socket* that will carry gameplay traffic.
A NAT mapping belongs to a specific local port, so punching on one socket and
playing on another would open a hole nobody uses.

Sequence:

    1. REGISTER to the broker, which observes our external (IP, port).
    2. Wait for the broker to hand us the peer's external address.
    3. Blast UDP at the peer while it does the same at us. The outbound packets
       open our NAT mapping so the inbound ones are accepted.
    4. If nothing gets through, ask the broker to relay.

Also tries the peer's *local* address first when both sides are behind the same
NAT: many home routers handle hairpin NAT badly or not at all, and the direct
LAN path is faster anyway.
"""

from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass
from enum import Enum, auto

from common import protocol, stun
from common.timing import now_ns, ns_to_ms

log = logging.getLogger(__name__)

#: How long to keep punching before giving up and asking for a relay.
PUNCH_TIMEOUT_NS = 8_000_000_000       # 8 s

#: Gap between punch packets. Fast enough to cross paths with the peer's
#: attempts, slow enough not to look like a flood to an IDS.
PUNCH_INTERVAL_NS = 100_000_000        # 100 ms

REGISTER_TIMEOUT_NS = 10_000_000_000   # 10 s
REGISTER_RETRY_NS = 1_000_000_000

#: Sent during punching. Distinct from any game packet so the peer can
#: recognize a successful punch before the session handshake begins. Defined in
#: common/ because the server has to answer these too.
PUNCH_PROBE = protocol.PUNCH_PROBE
PUNCH_ACK = protocol.PUNCH_ACK_PROBE


class PunchResult(Enum):
    DIRECT_LOCAL = auto()   # reached the peer on its LAN address
    PUNCHED = auto()        # NAT traversal succeeded
    RELAY = auto()          # falling back through the broker
    FAILED = auto()


@dataclass(slots=True)
class PunchOutcome:
    result: PunchResult
    peer_address: tuple[str, int] | None = None

    #: Issued by the broker at registration. Only meaningful when relaying:
    #: the peer prefixes each relayed datagram with it so the broker can route
    #: by token rather than by source address, which is what lets relaying work
    #: when the broker sits behind a proxy that rewrites addresses.
    relay_token: str = ""
    relay_address: tuple[str, int] | None = None

    #: True when the broker gave us a port of its own for this conversation
    #: instead of routing by token on its signalling port. Then ``relay_address``
    #: already names that port and nothing needs framing -- which is what lets
    #: the far side tell several relayed peers apart.
    relay_allocated: bool = False

    #: True when relaying was asked for rather than fallen back to.
    chose_relay: bool = False

    external_address: tuple[str, int] | None = None
    elapsed_ms: float = 0.0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.result in (PunchResult.DIRECT_LOCAL, PunchResult.PUNCHED, PunchResult.RELAY)

    @property
    def is_relayed(self) -> bool:
        return self.result is PunchResult.RELAY

    def describe(self) -> str:
        if self.result is PunchResult.DIRECT_LOCAL:
            return f"Direct LAN connection to {_fmt(self.peer_address)}"
        if self.result is PunchResult.PUNCHED:
            return f"NAT traversal succeeded; direct to {_fmt(self.peer_address)}"
        if self.result is PunchResult.RELAY:
            # Whether traversal *failed* or was never attempted is the whole
            # difference between a fault and the configuration working, and
            # saying the wrong one on every connect is how a warning stops
            # being read.
            why = (
                "as configured"
                if self.chose_relay
                else "-- NAT traversal failed"
            )
            return (
                f"Relaying via {_fmt(self.relay_address)} {why}. "
                "Expect noticeably higher latency."
            )
        return self.detail or "Could not establish a connection"


class HolePuncher:
    """Runs the punch on a caller-provided socket.

    The socket is deliberately passed in: it must be the same one the session
    will use afterwards.
    """

    def __init__(
        self,
        sock: socket.socket,
        broker_address: tuple[str, int],
        room_code: str,
        role: str = "client",
        peer_role: str = "server",
        stun_servers: tuple[str, ...] | list[str] = (),
        force_relay: bool = False,
    ) -> None:
        self._sock = sock
        self._broker = broker_address
        self._room = room_code
        self._role = role

        #: Go straight to the relay without punching first.
        #:
        #: For a network already known not to traverse -- endpoint-dependent
        #: ("symmetric") NAT, where the mapping differs per destination, so the
        #: address a peer would punch at is never the one STUN reported. The
        #: punch cannot succeed there, and skipping it saves the full budget
        #: below (~9.5 s) on every single connection.
        #:
        #: The introduction is still required: the relay request names the peer,
        #: so both sides must be registered in the room regardless.
        self._force_relay = force_relay

        #: Where to ask what our own public address is. Empty disables it, and
        #: everything falls back to whatever the broker observed -- which is
        #: correct whenever the broker is directly reachable.
        self._stun_servers = list(stun_servers)

        #: Which introduction we are waiting for. A room carries two
        #: independent pairs -- gameplay and video -- and a peer message
        #: carries the role of whoever is being introduced. Without this
        #: filter a viewer would accept the *game server's* address, punch at
        #: it, and then fail to handshake against a socket serving something
        #: else entirely.
        self._peer_role = peer_role

        self._external: tuple[str, int] | None = None
        self._public: tuple[str, int] | None = None
        self._relay_token = ""

        #: A port of the broker's, dedicated to this conversation. Set only when
        #: the broker allocated one; otherwise relaying goes through its
        #: signalling port with token framing.
        self._relay_endpoint: tuple[str, int] | None = None

    def run(self) -> PunchOutcome:
        started = now_ns()

        peer, peer_local, peer_public = self._register_and_wait()
        if peer is None:
            return PunchOutcome(
                PunchResult.FAILED,
                elapsed_ms=ns_to_ms(now_ns() - started),
                detail=(
                    "The other side never appeared at the rendezvous broker. "
                    "Check that the server is running with the same room code."
                ),
            )

        if self._force_relay:
            log.info("Relay mode: skipping the punch and asking the broker to relay")
            if self._request_relay(peer):
                return self._relay_outcome(peer, started)
            return PunchOutcome(
                PunchResult.FAILED,
                elapsed_ms=ns_to_ms(now_ns() - started),
                detail=(
                    "The broker refused to relay. Check that the server is in "
                    "the same room, and that the broker allows relaying."
                ),
            )

        # Same-NAT case first: if it works it is both faster and more reliable
        # than asking the router to hairpin.
        if peer_local is not None:
            if self._punch_at(peer_local, timeout_ns=1_500_000_000):
                return PunchOutcome(
                    PunchResult.DIRECT_LOCAL,
                    peer_address=peer_local,
                    external_address=self._external,
                    elapsed_ms=ns_to_ms(now_ns() - started),
                )

        # The peer's own view of its public address first, then what the broker
        # observed. On a directly reachable broker these are the same address,
        # the duplicate collapses, and this does exactly what it always did.
        # They differ only when something rewrote the source address on the way
        # to the broker -- a proxy, an frp tunnel -- and then the self-reported
        # one is the only candidate worth punching at.
        candidates: list[tuple[str, int]] = []
        for candidate in (peer_public, peer):
            if candidate is None or candidate == peer_local:
                continue
            if candidate not in candidates:
                candidates.append(candidate)

        # One budget shared between them, not one each: a peer that cannot be
        # reached should reach the relay fallback just as quickly as before,
        # rather than waiting twice as long for the same answer.
        each_ns = PUNCH_TIMEOUT_NS // max(len(candidates), 1)
        for candidate in candidates:
            if self._punch_at(candidate, timeout_ns=each_ns):
                return PunchOutcome(
                    PunchResult.PUNCHED,
                    peer_address=candidate,
                    external_address=self._external,
                    elapsed_ms=ns_to_ms(now_ns() - started),
                )

        log.warning("Hole-punching to %s failed; requesting relay", peer)
        if self._request_relay(peer):
            return self._relay_outcome(peer, started)

        return PunchOutcome(
            PunchResult.FAILED,
            elapsed_ms=ns_to_ms(now_ns() - started),
            detail=(
                "NAT traversal failed and the broker would not relay. "
                "Try direct mode with port forwarding, or a VPN such as Tailscale."
            ),
        )

    def _relay_outcome(self, peer: tuple[str, int], started: int) -> PunchOutcome:
        """The relay result, whichever of the two relay shapes we were given.

        An allocated endpoint is a port of the broker's reserved for this
        conversation, and traffic to it needs no framing. Without one we fall
        back to token framing on the signalling port, which works but cannot
        carry more than one relayed peer to the same far side.
        """
        allocated = self._relay_endpoint is not None
        return PunchOutcome(
            PunchResult.RELAY,
            peer_address=peer,
            relay_token="" if allocated else self._relay_token,
            relay_address=self._relay_endpoint or self._broker,
            relay_allocated=allocated,
            chose_relay=self._force_relay,
            external_address=self._external,
            elapsed_ms=ns_to_ms(now_ns() - started),
        )

    # -- steps -------------------------------------------------------------

    def _register_and_wait(
        self,
    ) -> tuple[
        tuple[str, int] | None, tuple[str, int] | None, tuple[str, int] | None
    ]:
        """Register with the broker and wait to be told the peer's address."""
        # Before anything else is expected on this socket, and on *this* socket
        # deliberately: a NAT mapping belongs to one local port, so a public
        # address discovered on any other socket describes a mapping nobody
        # will use. Best-effort -- no answer just means one fewer candidate.
        self._public = stun.discover(self._sock, self._stun_servers)
        if self._public is not None:
            log.info("STUN sees us at %s", _fmt(self._public))

        body = {
            "op": "register",
            "room": self._room,
            "role": self._role,
            "local": list(self._local_address()),
            # We understand an allocated relay endpoint. The broker only uses
            # one when *both* peers say so, since it changes the address each
            # sees the other at.
            "alloc": True,
        }
        if self._public is not None:
            body["public"] = list(self._public)
        message = json.dumps(body).encode("utf-8")

        deadline = now_ns() + REGISTER_TIMEOUT_NS
        next_send = 0

        while now_ns() < deadline:
            if now_ns() >= next_send:
                try:
                    self._sock.sendto(message, self._broker)
                except OSError as exc:
                    log.warning("Could not reach the broker: %s", exc)
                next_send = now_ns() + REGISTER_RETRY_NS

            try:
                data, address = self._sock.recvfrom(2048)
            except BlockingIOError:
                continue
            except OSError:
                continue

            if address[:2] != self._broker:
                continue

            try:
                body = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue

            op = body.get("op")

            if op == "registered":
                external = body.get("external")
                if isinstance(external, list) and len(external) == 2:
                    self._external = (str(external[0]), int(external[1]))
                    log.info("Broker sees us as %s", _fmt(self._external))
                token = body.get("token")
                if isinstance(token, str):
                    self._relay_token = token

            elif op == "peer":
                # The role field has always been on the wire; it only started
                # mattering once a room could hold more than one pair.
                role = body.get("role")
                if isinstance(role, str) and role != self._peer_role:
                    log.debug("Ignoring introduction to a %s", role)
                    continue

                peer = _parse_address(body.get("address"))
                peer_local = _parse_address(body.get("local"))
                peer_public = _parse_address(body.get("public"))
                if peer is not None:
                    log.info("Broker introduced peer at %s", _fmt(peer))
                    if peer_public is not None and peer_public != peer:
                        # They differ only when something between us and the
                        # broker rewrote the source address -- a proxy, an frp
                        # tunnel. The self-reported one is then the only usable
                        # candidate, and the observed one is the proxy.
                        log.info(
                            "Peer reports itself at %s (broker saw %s)",
                            _fmt(peer_public), _fmt(peer),
                        )
                    return peer, peer_local, peer_public

            elif op == "error":
                log.error("Broker error: %s", body.get("reason", "unknown"))
                return None, None, None

        return None, None, None

    def _punch_at(self, peer: tuple[str, int], timeout_ns: int) -> bool:
        """Blast probes at ``peer`` until one comes back."""
        deadline = now_ns() + timeout_ns
        next_send = 0

        while now_ns() < deadline:
            if now_ns() >= next_send:
                try:
                    self._sock.sendto(PUNCH_PROBE, peer)
                except OSError:
                    pass
                next_send = now_ns() + PUNCH_INTERVAL_NS

            try:
                data, address = self._sock.recvfrom(2048)
            except BlockingIOError:
                continue
            except OSError:
                continue

            if address[:2] != peer:
                continue

            if data.startswith(PUNCH_PROBE):
                # The peer is punching at us too. Acknowledge, and keep going
                # briefly so our ack is not the one that gets lost.
                try:
                    self._sock.sendto(PUNCH_ACK, peer)
                except OSError:
                    pass
                return True

            if data.startswith(PUNCH_ACK):
                return True

        return False

    def _request_relay(self, peer: tuple[str, int]) -> bool:
        message = json.dumps({"op": "relay", "peer": list(peer)}).encode("utf-8")
        deadline = now_ns() + 5_000_000_000
        next_send = 0

        while now_ns() < deadline:
            if now_ns() >= next_send:
                try:
                    self._sock.sendto(message, self._broker)
                except OSError:
                    return False
                next_send = now_ns() + REGISTER_RETRY_NS

            try:
                data, address = self._sock.recvfrom(2048)
            except (BlockingIOError, OSError):
                continue

            if address[:2] != self._broker:
                continue
            try:
                body = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue

            if body.get("op") == "relaying":
                # Only the port travels: the broker cannot know which of its
                # addresses we reach it at, and any it named for itself would be
                # the wrong one exactly when it sits behind a proxy or a NAT.
                port = body.get("relay_port")
                if isinstance(port, int) and 1 <= port <= 65535:
                    self._relay_endpoint = (self._broker[0], port)
                    log.info(
                        "Broker allocated relay port %d for this connection", port
                    )
                return True
            if body.get("op") == "error":
                log.error("Broker refused to relay: %s", body.get("reason"))
                return False

        return False

    def _local_address(self) -> tuple[str, int]:
        """Our LAN address, for the same-NAT shortcut.

        Requires a bound socket -- the transport binds before punching, since
        the whole point is to advertise a port that will still be ours later.
        """
        try:
            port = self._sock.getsockname()[1]
        except OSError:
            return ("0.0.0.0", 0)

        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # No packet is sent; this just picks the route the OS would use.
                probe.connect(self._broker)
                return (probe.getsockname()[0], port)
            finally:
                probe.close()
        except OSError:
            return ("0.0.0.0", port)


def _parse_address(raw) -> tuple[str, int] | None:
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    try:
        return (str(raw[0]), int(raw[1]))
    except (TypeError, ValueError):
        return None


def _fmt(address: tuple[str, int] | None) -> str:
    return f"{address[0]}:{address[1]}" if address else "unknown"

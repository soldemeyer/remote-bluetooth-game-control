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

from common import protocol
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
    relay_address: tuple[str, int] | None = None
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
            return (
                f"Relaying via {_fmt(self.relay_address)} -- NAT traversal failed. "
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
    ) -> None:
        self._sock = sock
        self._broker = broker_address
        self._room = room_code
        self._role = role
        self._external: tuple[str, int] | None = None

    def run(self) -> PunchOutcome:
        started = now_ns()

        peer, peer_local = self._register_and_wait()
        if peer is None:
            return PunchOutcome(
                PunchResult.FAILED,
                elapsed_ms=ns_to_ms(now_ns() - started),
                detail=(
                    "The other side never appeared at the rendezvous broker. "
                    "Check that the server is running with the same room code."
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

        if self._punch_at(peer, timeout_ns=PUNCH_TIMEOUT_NS):
            return PunchOutcome(
                PunchResult.PUNCHED,
                peer_address=peer,
                external_address=self._external,
                elapsed_ms=ns_to_ms(now_ns() - started),
            )

        log.warning("Hole-punching to %s failed; requesting relay", peer)
        if self._request_relay(peer):
            return PunchOutcome(
                PunchResult.RELAY,
                peer_address=peer,
                relay_address=self._broker,
                external_address=self._external,
                elapsed_ms=ns_to_ms(now_ns() - started),
            )

        return PunchOutcome(
            PunchResult.FAILED,
            elapsed_ms=ns_to_ms(now_ns() - started),
            detail=(
                "NAT traversal failed and the broker would not relay. "
                "Try direct mode with port forwarding, or a VPN such as Tailscale."
            ),
        )

    # -- steps -------------------------------------------------------------

    def _register_and_wait(
        self,
    ) -> tuple[tuple[str, int] | None, tuple[str, int] | None]:
        """Register with the broker and wait to be told the peer's address."""
        message = json.dumps(
            {
                "op": "register",
                "room": self._room,
                "role": self._role,
                "local": list(self._local_address()),
            }
        ).encode("utf-8")

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

            elif op == "peer":
                peer = _parse_address(body.get("address"))
                peer_local = _parse_address(body.get("local"))
                if peer is not None:
                    log.info("Broker introduced peer at %s", _fmt(peer))
                    return peer, peer_local

            elif op == "error":
                log.error("Broker error: %s", body.get("reason", "unknown"))
                return None, None

        return None, None

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

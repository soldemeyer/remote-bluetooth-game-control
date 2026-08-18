"""Client session management: handshake, admission control, rate limiting.

Owns the server side of the handshake and the lifecycle of an authenticated
session. Admission is deliberately two-stage:

  1. **Authentication** -- proves the client knows the shared password.
  2. **Approval** -- the operator accepts or denies it in the web GUI.

Knowing the password gets you a session, not necessarily a controller. That
split is what makes the "accept/deny controller input" requirement meaningful:
the password stops strangers, and approval stops a housemate from grabbing
player 1 mid-game.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto

from common import crypto, protocol
from common.protocol import PROTOCOL_VERSION, RejectReason
from common.timing import LatencyStats, now_ns

log = logging.getLogger(__name__)

#: Sessions idle longer than this are reaped. Generous enough to survive a
#: brief network blip, short enough that a crashed client frees its slot fast.
SESSION_TIMEOUT_NS = 10_000_000_000  # 10 s

#: Online-guessing defence. Argon2id makes each attempt cost ~0.1 s of CPU, so
#: this also protects the Pi from being pinned by a flood of bogus handshakes.
MAX_ATTEMPTS_PER_WINDOW = 5
ATTEMPT_WINDOW_S = 60.0
LOCKOUT_S = 300.0

MAX_SLOTS_PER_CLIENT = 4

#: Cap on in-flight HELLOs awaiting their AUTH. Bounded so a flood cannot
#: exhaust memory; evicted oldest-first so a flood cannot displace a
#: legitimate handshake that is about to complete.
MAX_PENDING_HELLO = 64

#: Session roles. A client declares one in its AUTH payload; anything else is
#: read as a controller, which is what every pre-video client sends.
ROLE_CONTROLLER = "controller"
ROLE_VIDEO_SOURCE = "video-source"
ROLE_VIDEO_CLIENT = "video-client"

#: The Bluetooth server, connecting *out* to a video server to configure it.
#: Only this role may send control messages there, and it is exempt from the
#: ticket requirement -- it is the party that issues tickets.
ROLE_BT_SERVER = "bt-server"

_KNOWN_ROLES = frozenset(
    {ROLE_CONTROLLER, ROLE_VIDEO_SOURCE, ROLE_VIDEO_CLIENT, ROLE_BT_SERVER}
)

#: Headroom above ``max_clients`` for sessions that hold no player slot. One
#: video source plus a little slack; the point is that "not a controller" is
#: not the same as "unlimited".
MAX_AUXILIARY_SESSIONS = 4


class SessionState(Enum):
    PENDING = auto()      # authenticated, awaiting operator approval
    APPROVED = auto()     # streaming
    DENIED = auto()
    EXPIRED = auto()


@dataclass(slots=True)
class ControllerSlot:
    """One controller a client is streaming."""

    slot: int
    username: str = ""
    device_name: str = ""
    connected: bool = True

    #: This controller's rumble opt-in. Independent of the client-wide switch:
    #: a player may want haptics on their own pad and not on a spare.
    rumble_enabled: bool = True

    packets_received: int = 0
    packets_dropped: int = 0
    last_packet_ns: int = 0
    rtt: LatencyStats = field(default_factory=LatencyStats)

    def snapshot(self) -> dict[str, object]:
        return {
            "slot": self.slot,
            "username": self.username,
            "device_name": self.device_name,
            "connected": self.connected,
            "packets_received": self.packets_received,
            "packets_dropped": self.packets_dropped,
            "rtt_ms": self.rtt.snapshot(),
        }


@dataclass(slots=True)
class Session:
    """An authenticated client."""

    session_id: int
    client_id: str                       # hex of the client's 16-byte id
    address: tuple[str, int]
    crypto: crypto.SessionCrypto
    client_name: str = ""

    #: What this peer connected as. A "video-source" holds a session for the
    #: control plane only -- it never claims a controller slot, and it is the
    #: only role permitted to feed the preview. Anything unrecognised is
    #: treated as a controller, so an older client is unaffected.
    role: str = ROLE_CONTROLLER

    #: The ticket this peer presented, where tickets are required. Kept so a
    #: revoked one can be matched to the session it admitted.
    ticket: str = ""

    state: SessionState = SessionState.PENDING
    created_ns: int = field(default_factory=now_ns)
    last_seen_ns: int = field(default_factory=now_ns)

    slots: dict[int, ControllerSlot] = field(default_factory=dict)
    replay: protocol.ReplayWindow = field(default_factory=protocol.ReplayWindow)

    control_seq: int = 0

    #: Client's rumble opt-in. Starts False and is enabled only when the
    #: client says so, which is fail-safe: we never push feedback at a
    #: client that has not asked for it.
    rumble_enabled: bool = False

    packets_received: int = 0
    packets_rejected: int = 0

    @property
    def is_approved(self) -> bool:
        return self.state is SessionState.APPROVED

    @property
    def age_s(self) -> float:
        return (now_ns() - self.created_ns) / 1e9

    @property
    def idle_s(self) -> float:
        return (now_ns() - self.last_seen_ns) / 1e9

    def slot(self, index: int) -> ControllerSlot:
        entry = self.slots.get(index)
        if entry is None:
            entry = ControllerSlot(slot=index)
            self.slots[index] = entry
        return entry

    def next_control_seq(self) -> int:
        seq = self.control_seq
        self.control_seq = (seq + 1) & 0xFFFFFFFF
        return seq

    def snapshot(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "client_id": self.client_id,
            "client_name": self.client_name,
            "role": self.role,
            "address": f"{self.address[0]}:{self.address[1]}",
            "state": self.state.name,
            "age_s": round(self.age_s, 1),
            "idle_s": round(self.idle_s, 2),
            "packets_received": self.packets_received,
            "rumble_enabled": self.rumble_enabled,
            "slots": [s.snapshot() for s in sorted(self.slots.values(), key=lambda x: x.slot)],
        }


class _RateLimiter:
    """Per-address attempt limiter with lockout."""

    def __init__(self) -> None:
        self._attempts: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, address: str) -> bool:
        """True if an attempt from ``address`` is allowed right now."""
        now = time.monotonic()
        with self._lock:
            locked = self._locked_until.get(address)
            if locked is not None:
                if now < locked:
                    return False
                del self._locked_until[address]
                self._attempts.pop(address, None)

            recent = [t for t in self._attempts.get(address, []) if now - t < ATTEMPT_WINDOW_S]
            self._attempts[address] = recent
            return len(recent) < MAX_ATTEMPTS_PER_WINDOW

    def record_failure(self, address: str) -> None:
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._attempts.get(address, []) if now - t < ATTEMPT_WINDOW_S]
            recent.append(now)
            self._attempts[address] = recent

            if len(recent) >= MAX_ATTEMPTS_PER_WINDOW:
                self._locked_until[address] = now + LOCKOUT_S
                log.warning(
                    "Rate limiting %s after %d failed attempts; locked out for %.0f s",
                    address,
                    len(recent),
                    LOCKOUT_S,
                )

    def record_success(self, address: str) -> None:
        with self._lock:
            self._attempts.pop(address, None)
            self._locked_until.pop(address, None)

    def reset(self, address: str | None = None) -> None:
        with self._lock:
            if address is None:
                self._attempts.clear()
                self._locked_until.clear()
            else:
                self._attempts.pop(address, None)
                self._locked_until.pop(address, None)


class SessionManager:
    """Owns the handshake and the set of live sessions."""

    def __init__(
        self,
        password: str,
        *,
        max_clients: int = 4,
        auto_approve: bool = False,
        require_tickets: bool = False,
    ) -> None:
        if not password:
            raise ValueError("A server password is required")

        self._password = password
        self._max_clients = max_clients
        self.auto_approve = auto_approve

        #: When true, the password alone is not enough: the AUTH payload must
        #: also carry a ticket this manager currently accepts.
        #:
        #: The video server turns this on. The password is shared with every
        #: player, so without it "the operator denied you" would stop your
        #: controller and leave you watching -- the deny button would only half
        #: work. Tickets are issued by the Bluetooth server to clients it has
        #: approved, which is the only party that knows who those are.
        self._require_tickets = require_tickets
        self._tickets: set[str] = set()

        #: Fixed per-server salt. Deriving the master key once at startup keeps
        #: Argon2id off the handshake path entirely -- otherwise every
        #: connection would cost the Pi ~0.1 s of CPU.
        self._salt = crypto.new_salt()
        self._master_key = crypto.derive_master_key(password, self._salt)

        #: An optional *second* accepted password, for peers of a different
        #: kind. The video server uses it: the operator's video password admits
        #: the Bluetooth server, while players authenticate with the password
        #: they already have.
        #:
        #: Two credentials rather than one because they mean different things.
        #: If viewers shared the operator's password, a client that had once
        #: been approved would know it -- and could come back claiming to *be*
        #: the Bluetooth server, which is the one role exempt from tickets.
        #: Revocation would then only hold until they tried that.
        #:
        #: Both keys are derived at startup, so telling them apart at AUTH is
        #: two HMACs, not two Argon2id runs.
        self._viewer_key: bytes | None = None

        self._sessions: dict[str, Session] = {}
        self._by_address: dict[tuple[str, int], Session] = {}
        self._lock = threading.Lock()
        self._next_session_id = 1
        self._rate_limiter = _RateLimiter()

        #: Pending HELLOs: client_id -> (client_random, server_random).
        #: Bounded so a flood of HELLOs cannot exhaust memory.
        self._pending_hello: dict[bytes, tuple[bytes, bytes]] = {}

        log.info(
            "Session manager ready (max %d clients, auto-approve %s)",
            max_clients,
            "on" if auto_approve else "off",
        )

    def set_password(self, password: str) -> int:
        """Change the shared password. Returns how many sessions were dropped.

        Every live session is closed, because a session key is derived from the
        password: an existing session is by construction using the old one and
        there is no way for it to continue honestly. A fresh salt is generated
        too, so the new master key shares nothing with the old.

        Pending handshakes are discarded for the same reason -- one mid-flight
        would be answered against a key that no longer exists.
        """
        if not password:
            raise ValueError("A server password is required")

        with self._lock:
            self._password = password
            self._salt = crypto.new_salt()
            self._master_key = crypto.derive_master_key(password, self._salt)
            self._pending_hello.clear()

            dropped = len(self._sessions)
            for client_id in list(self._sessions):
                self._drop_locked(client_id)

        log.info("Server password changed; %d session(s) closed", dropped)
        return dropped

    # -- handshake ---------------------------------------------------------

    def handle_hello(self, data: bytes, address: tuple[str, int]) -> bytes | None:
        """HELLO -> CHALLENGE."""
        if not self._rate_limiter.check(address[0]):
            return _reject(RejectReason.RATE_LIMITED)

        if len(data) < 3 + 16 + crypto.RANDOM_SIZE:
            return _reject(RejectReason.MALFORMED)

        version = int.from_bytes(data[1:3], "little")
        if version != PROTOCOL_VERSION:
            log.warning("Rejecting client with protocol version %d", version)
            return _reject(RejectReason.VERSION_MISMATCH)

        client_id = data[3:19]
        client_random = data[19 : 19 + crypto.RANDOM_SIZE]

        server_random = crypto.new_random()

        with self._lock:
            # Evict oldest-first rather than clearing wholesale. Clearing let an
            # attacker discard every legitimate in-flight handshake by sending
            # 64 cheap HELLOs -- a free denial of service. Python dicts preserve
            # insertion order, so the first key is the oldest.
            while len(self._pending_hello) >= MAX_PENDING_HELLO:
                self._pending_hello.pop(next(iter(self._pending_hello)))
            self._pending_hello[client_id] = (client_random, server_random)

        return bytes([protocol.PacketType.CHALLENGE]) + self._salt + server_random

    def handle_auth(
        self, data: bytes, address: tuple[str, int], capacity: int
    ) -> tuple[bytes, Session | None]:
        """AUTH -> ACCEPT or REJECT. Returns ``(response, session or None)``."""
        if not self._rate_limiter.check(address[0]):
            return _reject(RejectReason.RATE_LIMITED), None

        if len(data) < 1 + 16 + crypto.PROOF_SIZE:
            return _reject(RejectReason.MALFORMED), None

        client_id = data[1:17]
        proof = data[17 : 17 + crypto.PROOF_SIZE]
        encrypted_info = data[17 + crypto.PROOF_SIZE :]

        with self._lock:
            randoms = self._pending_hello.get(client_id)
        if randoms is None:
            # AUTH without a matching HELLO -- either a replay or a client that
            # lost our CHALLENGE. Make it start over.
            return _reject(RejectReason.MALFORMED), None

        client_random, server_random = randoms

        # Which credential did they use? Both keys are already derived, so this
        # is a pair of HMACs. The answer decides what they are allowed to be:
        # only the primary password admits a control peer.
        session_key, matched_viewer = self._verify_against_keys(
            client_id, proof, client_random, server_random
        )
        if session_key is None:
            self._rate_limiter.record_failure(address[0])
            log.warning("Failed authentication from %s", address[0])
            return _reject(RejectReason.BAD_PASSWORD), None

        session_crypto = crypto.SessionCrypto.for_server(session_key)

        client_name = ""
        role = ROLE_CONTROLLER
        ticket = ""
        if encrypted_info:
            try:
                _, plaintext = session_crypto.decrypt(encrypted_info)
                _, body = protocol.decode_control(plaintext, 0)
                client_name = str(body.get("client_name", ""))[:64]
                declared = str(body.get("role", ROLE_CONTROLLER))
                role = declared if declared in _KNOWN_ROLES else ROLE_CONTROLLER
                ticket = str(body.get("ticket", ""))[:64]
            except (crypto.CryptoError, ValueError):
                # Non-fatal: the proof already authenticated them. Just means
                # we show a blank name in the GUI.
                log.debug("Could not decode client info from %s", address[0])

        if matched_viewer:
            # The viewer credential can only ever be a viewer, whatever the
            # payload claims. Trusting the declared role here would hand every
            # player the control role, which is the one exempt from tickets.
            role = ROLE_VIDEO_CLIENT

        # The control peer is exempt: it is the party that issues tickets, and
        # it authenticated with the credential only the operator holds.
        if self._require_tickets and role != ROLE_BT_SERVER:
            if not self._ticket_valid(ticket):
                # Authenticated, but not vouched for. NOT_APPROVED rather than
                # BAD_PASSWORD, because the password *was* right and telling
                # them otherwise sends them to fix the wrong thing.
                log.info("Refused %s from %s: no valid ticket", role, address[0])
                return _reject(RejectReason.NOT_APPROVED), None

        client_hex = client_id.hex()

        with self._lock:
            existing = self._sessions.get(client_hex)
            if existing is not None:
                # Reconnect from the same client -- replace rather than
                # refusing, so a client that crashed can come straight back.
                self._drop_locked(client_hex)

            # Only controllers occupy the *player* limit: a video source holds
            # a session purely to carry config and status, so counting it would
            # cost a real player their slot.
            #
            # But every role still has a ceiling. Exempting non-controllers
            # from the limit entirely would let anyone holding the password
            # open unbounded sessions just by declaring a different role, and
            # each session costs an Argon2id derivation plus permanent state.
            if role == ROLE_CONTROLLER:
                if self._controller_count_locked() >= self._max_clients:
                    return _reject(RejectReason.SERVER_FULL), None
            elif len(self._sessions) >= self._max_clients + MAX_AUXILIARY_SESSIONS:
                return _reject(RejectReason.SERVER_FULL), None

            session = Session(
                session_id=self._next_session_id,
                client_id=client_hex,
                address=address,
                crypto=session_crypto,
                client_name=client_name,
                role=role,
                ticket=ticket,
                state=SessionState.APPROVED if self.auto_approve else SessionState.PENDING,
            )
            self._next_session_id += 1
            self._sessions[client_hex] = session
            self._by_address[address] = session
            self._pending_hello.pop(client_id, None)

        self._rate_limiter.record_success(address[0])
        log.info(
            "%s %s authenticated from %s:%d (%s)",
            "Video source" if role == ROLE_VIDEO_SOURCE else "Client",
            client_name or client_hex[:8],
            address[0],
            address[1],
            "auto-approved" if self.auto_approve else "awaiting approval",
        )

        response = (
            bytes([protocol.PacketType.ACCEPT])
            + session.session_id.to_bytes(4, "little")
            + bytes([capacity])
            + bytes([1 if session.is_approved else 0])
        )
        return response, session

    # -- lookup ------------------------------------------------------------

    def by_address(self, address: tuple[str, int]) -> Session | None:
        """Datapath lookup. Lock-free -- see Router for the rationale."""
        return self._by_address.get(address)

    def by_client_id(self, client_id: str) -> Session | None:
        return self._sessions.get(client_id)

    def all_sessions(self) -> list[Session]:
        return list(self._sessions.values())

    @property
    def count(self) -> int:
        return len(self._sessions)

    @property
    def controller_count(self) -> int:
        """Sessions that occupy a player slot -- video sources excluded."""
        return self._controller_count_locked()

    def _controller_count_locked(self) -> int:
        """Caller may or may not hold the lock; a stale count is harmless here.

        Only ever compared against ``_max_clients`` under the lock in
        handle_auth, which is the one place it must be exact.
        """
        return sum(1 for s in self._sessions.values() if s.role == ROLE_CONTROLLER)

    def video_source(self) -> Session | None:
        """The attached video source, if one authenticated."""
        for session in self._sessions.values():
            if session.role == ROLE_VIDEO_SOURCE:
                return session
        return None

    # -- tickets -----------------------------------------------------------

    def _verify_against_keys(
        self,
        client_id: bytes,
        proof: bytes,
        client_random: bytes,
        server_random: bytes,
    ) -> tuple[bytes | None, bool]:
        """Check the proof against each accepted credential.

        Returns ``(session_key, matched_viewer_password)``, or ``(None, False)``
        when neither matches. The primary key is tried first so a peer holding
        it is never mistaken for a viewer.
        """
        session_key, proof_key = crypto.derive_session_keys(
            self._master_key, client_random, server_random
        )
        if crypto.verify_auth_proof(proof_key, client_id, PROTOCOL_VERSION, proof):
            return session_key, False

        if self._viewer_key is not None:
            viewer_session_key, viewer_proof_key = crypto.derive_session_keys(
                self._viewer_key, client_random, server_random
            )
            if crypto.verify_auth_proof(
                viewer_proof_key, client_id, PROTOCOL_VERSION, proof
            ):
                return viewer_session_key, True

        return None, False

    def set_viewer_password(self, password: str) -> int:
        """Set (or clear) the second accepted password. Returns sessions dropped.

        Costs one Argon2id derivation, so it is called when the Bluetooth
        server tells us the players' password -- not per connection.

        Sessions authenticated with the *old* viewer password are dropped: they
        are holding keys derived from a credential that is no longer accepted,
        which is the same reasoning as :meth:`set_password`.
        """
        with self._lock:
            if not password:
                changed = self._viewer_key is not None
                self._viewer_key = None
            else:
                key = crypto.derive_master_key(password, self._salt)
                changed = key != self._viewer_key
                self._viewer_key = key

            if not changed:
                return 0

            stale = [
                session
                for session in self._sessions.values()
                if session.role == ROLE_VIDEO_CLIENT
            ]
            for session in stale:
                self._drop_locked(session.client_id)

        if stale:
            log.info("Viewer password changed; dropped %d viewer session(s)", len(stale))
        return len(stale)

    @property
    def has_viewer_password(self) -> bool:
        return self._viewer_key is not None

    def _ticket_valid(self, ticket: str) -> bool:
        if not ticket:
            return False
        with self._lock:
            return ticket in self._tickets

    def set_tickets(self, tickets: set[str]) -> list[Session]:
        """Replace the accepted set. Returns sessions no longer admissible.

        Revocation has to reach *live* sessions, not just future handshakes:
        the point is to stop someone watching now, and a viewer whose ticket
        was withdrawn is exactly the person the operator just denied. Dropping
        them is the caller's job -- returning them keeps this class free of
        opinions about what a dropped session costs.
        """
        with self._lock:
            self._tickets = set(tickets)
            if not self._require_tickets:
                return []
            return [
                session
                for session in self._sessions.values()
                # The control peer holds no ticket -- it is the party that
                # issues them. Without this exemption it revokes itself the
                # moment it pushes a list: the link drops, reconnects, pushes
                # again, and drops again, forever.
                if session.role != ROLE_BT_SERVER
                and (not session.ticket or session.ticket not in self._tickets)
            ]

    # -- admission ---------------------------------------------------------

    def approve(self, client_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(client_id)
            if session is None:
                return False
            session.state = SessionState.APPROVED
        log.info("Operator approved %s", session.client_name or client_id[:8])
        return True

    def deny(self, client_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(client_id)
            if session is None:
                return False
            session.state = SessionState.DENIED
        log.info("Operator denied %s", session.client_name or client_id[:8])
        return True

    def drop(self, client_id: str) -> bool:
        with self._lock:
            return self._drop_locked(client_id)

    def _drop_locked(self, client_id: str) -> bool:
        session = self._sessions.pop(client_id, None)
        if session is None:
            return False
        # Rebuild rather than mutate, so datapath readers see a consistent map.
        self._by_address = {
            addr: s for addr, s in self._by_address.items() if s.client_id != client_id
        }
        return True

    def reap_expired(self) -> list[Session]:
        """Drop sessions that have gone silent. Returns what was removed."""
        now = now_ns()
        expired: list[Session] = []

        with self._lock:
            for client_id, session in list(self._sessions.items()):
                if now - session.last_seen_ns > SESSION_TIMEOUT_NS:
                    session.state = SessionState.EXPIRED
                    expired.append(session)
                    self._drop_locked(client_id)

        for session in expired:
            log.info(
                "Session %s timed out after %.1f s idle",
                session.client_name or session.client_id[:8],
                session.idle_s,
            )
        return expired

    def update_address(self, session: Session, address: tuple[str, int]) -> None:
        """Follow a client whose source port changed (NAT rebinding).

        Safe because the packet that triggered this was already authenticated
        by the AEAD -- an attacker cannot redirect a session without the key.
        """
        if session.address == address:
            return

        old = session.address
        with self._lock:
            by_address = dict(self._by_address)
            by_address.pop(old, None)
            by_address[address] = session
            self._by_address = by_address
            session.address = address

        log.info("Session %s moved from %s to %s", session.client_id[:8], old, address)

    def reset_rate_limit(self, address: str | None = None) -> None:
        """Operator escape hatch for a locked-out address."""
        self._rate_limiter.reset(address)

    def snapshot(self) -> list[dict[str, object]]:
        return [s.snapshot() for s in self.all_sessions()]


def _reject(reason: RejectReason) -> bytes:
    return bytes([protocol.PacketType.REJECT, reason])


def generate_password(length: int = 12) -> str:
    """Generate a readable random password.

    Ambiguous characters (0/O, 1/l/I) are excluded because this gets read aloud
    or typed from a screen.
    """
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))

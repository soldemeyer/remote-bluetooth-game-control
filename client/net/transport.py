"""Client-side UDP transport: handshake, encrypted session, latency tracking.

One socket carries everything (see CLAUDE.md): unreliable input packets and
reliable control messages share a single NAT mapping.

Threading: :meth:`send_input` and :meth:`service` are called from the input
thread. :meth:`queue_control` is safe to call from any thread -- the GUI uses
it -- and hands work to the input thread via a lock-protected list rather than
touching the socket directly.
"""

from __future__ import annotations

import logging
import socket
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Callable

from common import crypto, protocol
from common.protocol import (
    ControlOp,
    InputFlags,
    PacketType,
    PROTOCOL_VERSION,
    RejectReason,
)
from common.state import ControllerState
from common.timing import LatencyStats, StageTimings, now_ns, ns_to_ms

if TYPE_CHECKING:
    # Only for the annotation on connect_via_broker. Importing it at runtime
    # would be circular: holepunch builds on this module's socket.
    from client.net.holepunch import PunchOutcome

log = logging.getLogger(__name__)

#: Send buffer big enough that a brief scheduler stall does not drop packets,
#: small enough that we never build a queue of stale state.
_SOCKET_BUFFER = 256 * 1024

#: Heartbeat cadence. Well under the ~30 s NAT mapping timeout, and frequent
#: enough that the latency display is not stale while a player sits idle.
HEARTBEAT_INTERVAL_NS = 20_000_000        # 20 ms == 50 Hz

#: Give up on a session after this long with no traffic from the server.
SESSION_TIMEOUT_NS = 5_000_000_000        # 5 s

#: Handshake step retry cadence and total budget.
HANDSHAKE_RETRY_NS = 500_000_000          # 500 ms
HANDSHAKE_TIMEOUT_NS = 15_000_000_000     # 15 s -- Argon2id on a Pi is slow


class ConnectionState(Enum):
    DISCONNECTED = auto()
    RESOLVING = auto()      # discovery / hole-punching
    HANDSHAKING = auto()
    CONNECTED = auto()
    FAILED = auto()


class TransportError(RuntimeError):
    """Connection could not be established or was lost unrecoverably."""


@dataclass(slots=True)
class ControllerLatency:
    """Latency bookkeeping for one controller slot."""

    stats: StageTimings = field(default_factory=StageTimings)
    #: seq -> send timestamp, for matching acks. Bounded; stale entries are
    #: evicted rather than accumulating when acks are lost.
    _pending: dict[int, int] = field(default_factory=dict)

    MAX_PENDING = 256

    def record_send(self, seq: int, send_ts: int) -> None:
        if len(self._pending) >= self.MAX_PENDING:
            # Drop the oldest; a lost ack must not leak memory.
            oldest = min(self._pending)
            del self._pending[oldest]
        self._pending[seq] = send_ts

    def record_ack(self, seq: int, server_recv_ts: int, server_bt_ts: int) -> float | None:
        """Match an ack to its send and record the RTT. Returns RTT in ms.

        Measurement caveat: acks are read once per input-loop tick, so a
        returning ack can sit in the socket buffer for up to one poll period
        before we timestamp it. Measured RTT is therefore

            true RTT + [0, 1/poll_hz]

        At the default 500 Hz that is up to 2 ms of upward bias -- which is why
        a loopback test reports ~2 ms rather than the ~0.1 ms the network
        actually costs. The bias is in the *measurement*, not in the input
        path: input packets are sent the instant a change is detected.

        For an unbiased view of our own overhead, use ``bt_write`` below and
        the server's ``process_ms``: both are differences between two
        timestamps taken on the same clock with no polling in between.
        """
        send_ts = self._pending.pop(seq, None)
        if send_ts is None:
            return None

        rtt_ms = ns_to_ms(now_ns() - send_ts)
        self.stats.rtt.add(rtt_ms)

        if server_bt_ts >= server_recv_ts:
            # Both timestamps come from the server's clock, so their difference
            # is meaningful even though the two machines are unsynchronized.
            self.stats.bt_write.add(ns_to_ms(server_bt_ts - server_recv_ts))

        return rtt_ms


class ClientTransport:
    """Encrypted UDP session to the server."""

    def __init__(
        self,
        password: str,
        *,
        client_name: str = "client",
        on_control: Callable[[dict[str, Any]], None] | None = None,
        on_rumble: Callable[[int, int, int, int], None] | None = None,
        rumble_enabled: bool = True,
        on_state_change: Callable[[ConnectionState, str], None] | None = None,
        on_media: Callable[[bytes], None] | None = None,
        auth_extra: dict[str, Any] | None = None,
        stun_servers: tuple[str, ...] | list[str] = (),
    ) -> None:
        self._password = password
        self._client_name = client_name
        self._client_id = crypto.new_client_id()
        self._on_control = on_control
        self._on_rumble = on_rumble
        self._on_state_change = on_state_change

        #: Media packets are handed over whole rather than parsed here: this
        #: class knows nothing about frames, and the media layer wants the
        #: plaintext without a second dispatch.
        self._on_media = on_media

        #: Extra fields for the AUTH info payload -- the video server declares
        #: ``{"role": "video-source"}`` this way. Merged rather than replacing,
        #: so client_name always travels.
        self._auth_extra = dict(auth_extra) if auth_extra else {}

        #: Passed to the hole-puncher so it can learn our public address rather
        #: than relying on the broker to observe it. Only used on the punch
        #: path; a direct connection needs nothing of the sort.
        self._stun_servers = list(stun_servers)

        #: Prepended to every outgoing datagram while relaying, so the broker
        #: can identify the flow without trusting the source address. Empty on
        #: a direct or punched session.
        self._relay_prefix = b""

        #: Local rumble switch. Announced to the server so it stops sending
        #: rather than us discarding packets that already crossed the wire.
        self.rumble_enabled = rumble_enabled
        #: Per-slot rumble opt-in, or None for "whole client".
        self._rumble_slots: dict[int, bool] | None = None
        self.rumble_received = 0

        self._sock: socket.socket | None = None
        self._server_addr: tuple[str, int] | None = None
        self._session: crypto.SessionCrypto | None = None
        self._session_id: int = 0

        self._state = ConnectionState.DISCONNECTED
        self._state_detail = ""

        self._input_seq = 0
        self._control_seq = 0
        self._heartbeat_seq = 0
        self._last_heartbeat_ns = 0
        self._last_recv_ns = 0

        self._replay = protocol.ReplayWindow()
        self._latency: dict[int, ControllerLatency] = {}
        self._heartbeat_stats = LatencyStats()
        self._heartbeat_pending: dict[int, int] = {}

        #: Reused scratch buffer -- the hot path must not allocate.
        self._send_buf = bytearray(protocol.MAX_DATAGRAM)

        #: Control messages awaiting send/ack, guarded because the GUI thread
        #: enqueues into it.
        #: (seq, packet, last_sent_ns, op). The op is carried so a periodic
        #: message can supersede its own unacked predecessor.
        self._control_lock = threading.Lock()
        self._pending_control: list[tuple[int, bytes, int, str]] = []

        #: Server-advertised capacity. Drives which client slots the GUI enables.
        self.server_capacity = 0
        self.assignments: dict[int, str | None] = {}

        #: How the connection was established: direct | punched | relay.
        #: Surfaced in the GUI because a relayed path costs real latency.
        self.connection_mode = "direct"
        self.punch_outcome = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def state_detail(self) -> str:
        return self._state_detail

    @property
    def is_connected(self) -> bool:
        return self._state is ConnectionState.CONNECTED

    def _set_state(self, state: ConnectionState, detail: str = "") -> None:
        if state is self._state and detail == self._state_detail:
            return
        self._state = state
        self._state_detail = detail
        log.info("Connection state: %s%s", state.name, f" ({detail})" if detail else "")
        if self._on_state_change:
            self._on_state_change(state, detail)

    def connect(self, host: str, port: int, *, timeout_ns: int = HANDSHAKE_TIMEOUT_NS) -> None:
        """Direct connection: handshake straight to ``host:port``.

        Used for LAN, VPN, and port-forwarded servers. Blocks until connected
        or raises TransportError.
        """
        self.close()
        self._set_state(ConnectionState.HANDSHAKING, f"{host}:{port}")

        addr_info = socket.getaddrinfo(host, port, proto=socket.IPPROTO_UDP)
        if not addr_info:
            raise TransportError(f"Could not resolve {host}")
        family, _, _, _, sockaddr = addr_info[0]

        self._sock = self._make_socket(family)
        self._server_addr = sockaddr[:2]
        self.connection_mode = "direct"

        try:
            self._run_handshake(timeout_ns)
        except Exception:
            self.close()
            raise

        self._last_recv_ns = now_ns()
        self._announce_rumble()
        self._set_state(ConnectionState.CONNECTED, f"{host}:{port}")

    def connect_via_broker(
        self,
        broker_host: str,
        broker_port: int,
        room_code: str,
        *,
        timeout_ns: int = HANDSHAKE_TIMEOUT_NS,
        role: str = "client",
        peer_role: str = "server",
    ) -> "PunchOutcome":
        """Connect by NAT hole-punching through the rendezvous broker.

        The punch runs on **this transport's own socket**, and the session then
        uses that same socket. That is not an implementation convenience: a NAT
        mapping belongs to one local port, so punching anywhere else would open
        a hole no traffic uses.

        Returns the :class:`PunchOutcome` so callers can tell the operator
        whether the path is direct or relayed -- relayed sessions carry real
        extra latency and users deserve to know.

        ``role`` says which leg of the room we are. A room holds two
        independent pairs -- gameplay and video -- so a viewer registering as a
        plain ``client`` would be introduced to the game server and never to
        the video source, and would time out saying the other side never
        appeared.
        """
        from client.net.holepunch import HolePuncher

        self.close()
        self._set_state(ConnectionState.RESOLVING, f"rendezvous room '{room_code}'")

        try:
            info = socket.getaddrinfo(
                broker_host, broker_port, socket.AF_INET, socket.SOCK_DGRAM
            )
        except (socket.gaierror, OSError) as exc:
            raise TransportError(f"Could not resolve broker {broker_host}: {exc}") from exc
        if not info:
            raise TransportError(f"Could not resolve broker {broker_host}")

        broker = info[0][4][:2]
        self._sock = self._make_socket(socket.AF_INET)

        outcome = HolePuncher(
            self._sock,
            broker,
            room_code,
            role=role,
            peer_role=peer_role,
            stun_servers=self._stun_servers,
        ).run()
        if not outcome.ok:
            self.close()
            raise TransportError(outcome.describe())

        # In relay mode the broker forwards for us, so it is the peer we talk
        # to; otherwise we speak straight to the server.
        # Relayed traffic is framed so the broker can route it by token. Empty
        # otherwise, which is every direct and punched session -- the prefix is
        # 20 bytes and there is no reason to pay it when nothing forwards.
        if outcome.is_relayed and outcome.relay_token:
            self._relay_prefix = protocol.RELAY_MAGIC + bytes.fromhex(
                outcome.relay_token
            )

        self._server_addr = (
            outcome.relay_address if outcome.is_relayed else outcome.peer_address
        )
        self.punch_outcome = outcome
        self.connection_mode = "relay" if outcome.is_relayed else "punched"

        log.info("%s", outcome.describe())
        self._set_state(ConnectionState.HANDSHAKING, outcome.describe())

        try:
            self._run_handshake(timeout_ns)
        except Exception:
            self.close()
            raise

        self._last_recv_ns = now_ns()
        self._announce_rumble()
        self._set_state(ConnectionState.CONNECTED, outcome.describe())
        return outcome

    def _make_socket(self, family: int) -> socket.socket:
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _SOCKET_BUFFER)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _SOCKET_BUFFER)

        # Bind explicitly so the local port exists before we send anything.
        # Hole punching has to report this port to the broker, and on Windows
        # getsockname() on an unbound UDP socket fails outright (WinError
        # 10022) rather than returning a placeholder.
        sock.bind(("0.0.0.0" if family == socket.AF_INET else "::", 0))

        sock.setblocking(False)
        return sock

    def close(self) -> None:
        if self._sock is not None:
            if self._session is not None:
                # Best-effort graceful teardown so the server frees the slot
                # immediately instead of waiting for a timeout.
                try:
                    self._send_encrypted(bytes([PacketType.DISCONNECT]))
                except OSError:
                    pass
            try:
                self._sock.close()
            except OSError:
                pass

        self._sock = None
        self._session = None
        self._server_addr = None
        self._latency.clear()
        self._heartbeat_pending.clear()
        with self._control_lock:
            self._pending_control.clear()

        if self._state is not ConnectionState.DISCONNECTED:
            self._set_state(ConnectionState.DISCONNECTED)

    # -- handshake ---------------------------------------------------------

    def _run_handshake(self, timeout_ns: int) -> None:
        assert self._sock and self._server_addr
        deadline = now_ns() + timeout_ns

        client_random = crypto.new_random()

        hello = (
            bytes([PacketType.HELLO])
            + PROTOCOL_VERSION.to_bytes(2, "little")
            + self._client_id
            + client_random
        )

        # -- HELLO -> CHALLENGE
        challenge = self._exchange(hello, PacketType.CHALLENGE, deadline)

        salt = challenge[1 : 1 + crypto.SALT_SIZE]
        server_random = challenge[
            1 + crypto.SALT_SIZE : 1 + crypto.SALT_SIZE + crypto.RANDOM_SIZE
        ]
        if len(salt) != crypto.SALT_SIZE or len(server_random) != crypto.RANDOM_SIZE:
            raise TransportError("Malformed CHALLENGE from server")

        # Expensive, but exactly once per connection.
        self._set_state(ConnectionState.HANDSHAKING, "deriving key")
        master = crypto.derive_master_key(self._password, salt)
        session_key, proof_key = crypto.derive_session_keys(master, client_random, server_random)

        proof = crypto.compute_auth_proof(proof_key, self._client_id, PROTOCOL_VERSION)
        session = crypto.SessionCrypto.for_client(session_key)

        info = protocol.encode_control(
            0,
            ControlOp.SET_CONTROLLERS,
            {"client_name": self._client_name, **self._auth_extra},
        )
        auth = bytes([PacketType.AUTH]) + self._client_id + proof + session.encrypt(info)

        # -- AUTH -> ACCEPT / REJECT
        self._set_state(ConnectionState.HANDSHAKING, "authenticating")
        response = self._exchange(auth, (PacketType.ACCEPT, PacketType.REJECT), deadline)

        if response[0] == PacketType.REJECT:
            reason = RejectReason(response[1]) if len(response) > 1 else RejectReason.MALFORMED
            raise TransportError(reason.message())

        if len(response) < 7:
            raise TransportError("Malformed ACCEPT from server")

        self._session_id = int.from_bytes(response[1:5], "little")
        self.server_capacity = response[5]
        self._session = session

        log.info(
            "Connected: session %d, server capacity %d controller(s)",
            self._session_id,
            self.server_capacity,
        )

    def _exchange(
        self,
        payload: bytes,
        expected: int | tuple[int, ...],
        deadline: int,
    ) -> bytes:
        """Send ``payload`` and wait for a reply of an expected type, retrying.

        Handshake packets are unencrypted and travel over UDP, so loss is
        expected -- retry until the deadline rather than failing on the first
        dropped datagram.
        """
        assert self._sock and self._server_addr
        expected_types = (expected,) if isinstance(expected, int) else expected

        next_send = 0
        while now_ns() < deadline:
            if now_ns() >= next_send:
                self._transmit(payload)
                next_send = now_ns() + HANDSHAKE_RETRY_NS

            try:
                data, addr = self._sock.recvfrom(2048)
            except BlockingIOError:
                continue
            except ConnectionResetError:
                # Windows reports ICMP port-unreachable on a *connectionless*
                # UDP socket as WSAECONNRESET. It means "nothing is listening
                # there yet", not "the socket is broken" -- so keep retrying
                # until the deadline and let the timeout produce the real
                # diagnosis.
                continue
            except OSError as exc:
                raise TransportError(f"Socket error during handshake: {exc}") from exc

            if addr[:2] != self._server_addr or not data:
                continue
            if data[0] in expected_types:
                return data

        raise TransportError(
            "Server did not respond. Check the address, the port, and that the "
            "server is running."
        )

    # -- hot path ----------------------------------------------------------

    def send_input(
        self, slot: int, state: ControllerState, *, request_ack: bool,
        disconnected: bool = False, unbound: bool = False,
    ) -> None:
        """Send one controller state snapshot. Allocation-light by design.

        ``unbound`` says the neutral state being sent means "this pad has no
        bindings", not "the player is holding still". Without it the two are
        identical on the wire and the server has no way to tell an idle
        controller from one that can never produce input.
        """
        if self._session is None or self._sock is None or self._server_addr is None:
            return

        seq = self._input_seq
        self._input_seq = (seq + 1) & 0xFFFFFFFF

        flags = 0
        if request_ack:
            flags |= InputFlags.REQUEST_ACK
        if disconnected:
            flags |= InputFlags.CONTROLLER_DISCONNECTED
        if unbound:
            flags |= InputFlags.CONTROLLER_UNBOUND

        send_ts = now_ns()
        size = protocol.encode_input_into(
            self._send_buf, 0, seq, send_ts, slot, flags, state
        )

        if request_ack:
            self._latency_for(slot).record_send(seq, send_ts)

        self._send_encrypted(memoryview(self._send_buf)[:size])

    def _transmit(self, payload: bytes | memoryview) -> None:
        """The one place a datagram leaves this transport.

        Single point so relay framing cannot be forgotten on one path: the
        handshake needs forwarding just as much as the session traffic that
        follows it.
        """
        assert self._sock and self._server_addr
        if self._relay_prefix:
            payload = self._relay_prefix + bytes(payload)
        self._sock.sendto(payload, self._server_addr)

    def _send_encrypted(self, plaintext: bytes | memoryview) -> None:
        assert self._session and self._sock and self._server_addr
        try:
            self._transmit(self._session.encrypt(plaintext))
        except BlockingIOError:
            # Send buffer full. Dropping is correct here: the next snapshot
            # supersedes this one anyway, and blocking would add latency.
            log.debug("Send buffer full; dropped a packet")
        except OSError as exc:
            log.warning("Send failed: %s", exc)

    def service(self) -> None:
        """Drain incoming packets, send heartbeats, retry control messages.

        Called once per input-loop tick from the input thread.
        """
        if self._sock is None or self._session is None:
            return

        self._receive_all()
        self._maybe_heartbeat()
        self._retry_control()
        self._check_timeout()

    def _receive_all(self) -> None:
        assert self._sock
        while True:
            try:
                data, addr = self._sock.recvfrom(2048)
            except BlockingIOError:
                return
            except ConnectionResetError:
                # See _exchange(): a Windows UDP quirk, not a real error.
                continue
            except OSError as exc:
                log.warning("Receive failed: %s", exc)
                return

            if addr[:2] != self._server_addr:
                # Off-path datagram. Ignored rather than logged loudly: on the
                # open internet this is background noise.
                continue

            self._handle_packet(data)

    def _handle_packet(self, data: bytes) -> None:
        assert self._session
        try:
            counter, plaintext = self._session.decrypt(data)
        except crypto.CryptoError:
            # Wrong key, tampering, or a stray datagram. Never fatal.
            return

        if not self._replay.check_and_update(counter):
            return
        if not plaintext:
            return

        self._last_recv_ns = now_ns()
        kind = plaintext[0]

        # Media first: on a video session it is every packet but a handful,
        # and there is nothing for this class to do with one.
        if protocol.is_media_tag(kind):
            if self._on_media is not None:
                self._on_media(plaintext)
            return

        if kind == PacketType.INPUT_ACK:
            self._handle_input_ack(plaintext)
        elif kind == PacketType.FEEDBACK:
            self._handle_feedback(plaintext)
        elif kind == PacketType.HEARTBEAT_ACK:
            self._handle_heartbeat_ack(plaintext)
        elif kind == PacketType.CONTROL:
            self._handle_control(plaintext)
        elif kind == PacketType.CONTROL_ACK:
            self._handle_control_ack(plaintext)
        elif kind == PacketType.HEARTBEAT:
            self._send_encrypted(
                _heartbeat_reply(self._send_buf, plaintext)
            )
        elif kind == PacketType.DISCONNECT:
            self._set_state(ConnectionState.DISCONNECTED, "server closed the session")
            self.close()

    def _handle_input_ack(self, plaintext: bytes) -> None:
        try:
            seq, _client_ts, server_recv, server_bt, slot = protocol.decode_input_ack(
                plaintext, 0
            )
        except ValueError:
            return
        self._latency_for(slot).record_ack(seq, server_recv, server_bt)

    def _handle_feedback(self, plaintext: bytes) -> None:
        """Rumble from the console, bound for a local gamepad.

        Dropped outright when rumble is disabled locally. The server should not
        be sending any -- it only transmits when the client has opted in -- but
        checking here too means a stale or misbehaving server cannot make a
        pad buzz after the user has turned the feature off.
        """
        if not self.rumble_enabled:
            return

        try:
            slot, low, high, duration_ms = protocol.decode_feedback(plaintext, 0)
        except ValueError:
            return

        self.rumble_received += 1
        if self._on_rumble is not None:
            try:
                self._on_rumble(slot, low, high, duration_ms)
            except Exception:
                log.debug("Rumble callback failed", exc_info=True)

    def _announce_rumble(self) -> None:
        """Tell the server our rumble preference on connect.

        The server starts each session with rumble off and only enables it
        when told, so a client that never announces never receives any --
        fail-safe by construction."""
        self.queue_control(
            ControlOp.SET_RUMBLE,
            {
                "enabled": self.rumble_enabled,
                **(
                    {"slots": {str(s): bool(v) for s, v in self._rumble_slots.items()}}
                    if self._rumble_slots
                    else {}
                ),
            },
        )

    def set_rumble_enabled(
        self, enabled: bool, slots: dict[int, bool] | None = None
    ) -> None:
        """Turn rumble on or off, and tell the server so it stops transmitting.

        Telling the server matters: a purely local mute would still carry the
        packets across the network. With this, disabling on either end means the
        data is never sent at all.

        ``slots`` carries the per-controller switches. It is optional so an
        older server -- which ignores the field and applies ``enabled`` to the
        whole client -- still behaves sensibly.
        """
        changed = enabled != self.rumble_enabled or slots != self._rumble_slots
        if not changed:
            return

        self.rumble_enabled = enabled
        self._rumble_slots = dict(slots) if slots else None

        payload: dict = {"enabled": enabled}
        if slots:
            # JSON object keys must be strings.
            payload["slots"] = {str(slot): bool(on) for slot, on in slots.items()}

        self.queue_control(ControlOp.SET_RUMBLE, payload)
        log.info("Rumble %s%s", "enabled" if enabled else "disabled",
                 f" (per-slot: {slots})" if slots else "")

    def _handle_heartbeat_ack(self, plaintext: bytes) -> None:
        try:
            seq, _ = protocol.decode_heartbeat(plaintext, 0)
        except ValueError:
            return
        send_ts = self._heartbeat_pending.pop(seq, None)
        if send_ts is not None:
            self._heartbeat_stats.add(ns_to_ms(now_ns() - send_ts))

    def _handle_control(self, plaintext: bytes) -> None:
        try:
            seq, body = protocol.decode_control(plaintext, 0)
        except ValueError as exc:
            log.warning("Bad control message: %s", exc)
            return

        self._send_encrypted(protocol.encode_control_ack(seq))

        op = body.get("op")
        if op == ControlOp.CAPACITY:
            # Live capacity update -- the operator enabled/disabled an adapter.
            self.server_capacity = int(body.get("capacity", 0))
            log.info("Server capacity is now %d", self.server_capacity)
        elif op == ControlOp.ASSIGNMENT:
            self.assignments = {
                int(k): v for k, v in (body.get("assignments") or {}).items()
            }
        elif op == ControlOp.KICKED:
            self._set_state(
                ConnectionState.DISCONNECTED, body.get("reason", "kicked by operator")
            )
            self.close()
            return

        if self._on_control:
            self._on_control(body)

    def _handle_control_ack(self, plaintext: bytes) -> None:
        try:
            seq = protocol.decode_control_ack(plaintext, 0)
        except ValueError:
            return
        with self._control_lock:
            self._pending_control = [p for p in self._pending_control if p[0] != seq]

    # -- control channel ---------------------------------------------------

    def queue_control(self, op: str, payload: dict[str, Any] | None = None) -> None:
        """Enqueue a reliable control message. Safe to call from any thread."""
        self._queue_control(op, payload, replace=False)

    def queue_control_replacing(self, op: str, payload: dict[str, Any] | None = None) -> None:
        """Enqueue, dropping any unacked message of the same op first.

        For state that is periodic and absolute rather than incremental -- a
        video source's 1 Hz status, say. Without this a stalled link would
        accumulate a backlog of statuses and then deliver them all, every one
        of them stale by the time it arrived.
        """
        self._queue_control(op, payload, replace=True)

    def _queue_control(
        self, op: str, payload: dict[str, Any] | None, *, replace: bool
    ) -> None:
        seq = self._control_seq
        self._control_seq = (seq + 1) & 0xFFFFFFFF
        try:
            packet = protocol.encode_control(seq, op, payload)
        except ValueError as exc:
            log.error("Refusing to send oversized control message: %s", exc)
            return

        with self._control_lock:
            if replace:
                self._pending_control = [p for p in self._pending_control if p[3] != op]
            self._pending_control.append((seq, packet, 0, str(op)))

    def _retry_control(self) -> None:
        """Send unacked control messages, retrying on a fixed cadence.

        Packets are collected under the lock and sent outside it -- a blocked
        socket must never hold up the GUI thread trying to enqueue.
        """
        now = now_ns()
        to_send: list[bytes] = []

        with self._control_lock:
            for index, (seq, packet, last_sent, op) in enumerate(self._pending_control):
                if now - last_sent >= HANDSHAKE_RETRY_NS:
                    to_send.append(packet)
                    self._pending_control[index] = (seq, packet, now, op)

        for packet in to_send:
            self._send_encrypted(packet)

    # -- media channel -----------------------------------------------------

    def send_unreliable(self, plaintext: bytes | bytearray | memoryview) -> None:
        """Encrypt and send one packet with no retransmission or tracking.

        The media path's only send primitive. Safe from any thread: SessionCrypto
        serialises counter reservation itself, and a UDP sendto is atomic.
        """
        self._send_encrypted(plaintext)

    def fileno(self) -> int:
        """Underlying socket fd, so a media loop can select rather than spin."""
        if self._sock is None:
            raise TransportError("transport is not connected")
        return self._sock.fileno()

    # -- heartbeats & timeouts --------------------------------------------

    def _maybe_heartbeat(self) -> None:
        now = now_ns()
        if now - self._last_heartbeat_ns < HEARTBEAT_INTERVAL_NS:
            return
        self._last_heartbeat_ns = now

        seq = self._heartbeat_seq
        self._heartbeat_seq = (seq + 1) & 0xFFFFFFFF

        if len(self._heartbeat_pending) > 128:
            self._heartbeat_pending.pop(min(self._heartbeat_pending))
        self._heartbeat_pending[seq] = now

        size = protocol.encode_heartbeat_into(self._send_buf, 0, seq, now)
        self._send_encrypted(memoryview(self._send_buf)[:size])

    def _check_timeout(self) -> None:
        if now_ns() - self._last_recv_ns > SESSION_TIMEOUT_NS:
            self._set_state(ConnectionState.FAILED, "connection timed out")
            self.close()

    # -- stats -------------------------------------------------------------

    def _latency_for(self, slot: int) -> ControllerLatency:
        entry = self._latency.get(slot)
        if entry is None:
            entry = ControllerLatency()
            self._latency[slot] = entry
        return entry

    def latency_snapshot(self) -> dict[int, dict[str, Any]]:
        """Per-slot latency for the GUI."""
        return {slot: lat.stats.snapshot() for slot, lat in self._latency.items()}

    def idle_latency_snapshot(self) -> dict[str, float | int]:
        """Heartbeat RTT -- meaningful even when nobody is touching a controller."""
        return self._heartbeat_stats.snapshot()


def _heartbeat_reply(buf: bytearray, plaintext: bytes) -> memoryview:
    """Build a HEARTBEAT_ACK echoing the sender's sequence and timestamp."""
    seq, ts = protocol.decode_heartbeat(plaintext, 0)
    size = protocol.encode_heartbeat_ack_into(buf, 0, seq, ts)
    return memoryview(buf)[:size]

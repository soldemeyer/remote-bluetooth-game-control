"""The server datapath -- the hot path.

Runs on its own thread and does exactly one job: take UDP packets in, put HID
reports out. Every input packet's journey through this file is:

    recv -> decrypt -> replay check -> decode -> route -> build report -> BT write

Everything else on the server (web GUI, adapter management, approval) lives on
the asyncio thread and must never block this one. The two share state through
atomic snapshot reads -- see Router and SessionManager.

Rules for this file:
  * No allocation in the packet path. Buffers are preallocated per channel.
  * No logging per packet. Only on state transitions.
  * Never raise out of the packet handler. Anyone can send us a datagram, so
    malformed input is an expected condition, not an error.
"""

from __future__ import annotations

import logging
import selectors
import socket
import threading

from common import crypto, protocol
from common.protocol import InputFlags, PacketType
from common.state import ControllerState
from common.timing import (
    LatencyStats,
    configure_gc_for_realtime,
    now_ns,
    ns_to_ms,
    try_set_realtime_priority,
)
from server.router import Router
from server.sessions import Session, SessionManager

log = logging.getLogger(__name__)

#: Large receive buffer: a burst from four clients at 1 kHz must not overflow
#: while the thread is briefly descheduled.
_SOCKET_BUFFER = 1024 * 1024

#: Blocking timeout on the selector. Short enough to notice shutdown promptly,
#: long enough that an idle server does not spin.
_SELECT_TIMEOUT_S = 0.05

#: Reap expired sessions on this cadence.
_MAINTENANCE_INTERVAL_NS = 1_000_000_000


class Datapath:
    """UDP receive loop feeding Bluetooth HID outputs."""

    def __init__(
        self,
        sessions: SessionManager,
        router: Router,
        *,
        bind_host: str = "0.0.0.0",
        bind_port: int = 47800,
        realtime: bool = True,
    ) -> None:
        self._sessions = sessions
        self._router = router
        self._bind = (bind_host, bind_port)
        self._realtime = realtime

        self._sock: socket.socket | None = None
        self._selector: selectors.BaseSelector | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        #: Reused across packets -- the datapath must not allocate.
        self._scratch_state = ControllerState()
        self._send_buf = bytearray(protocol.MAX_DATAGRAM)

        self._last_maintenance_ns = 0

        # Diagnostics. Cheap counters; the web GUI reads them at 10 Hz.
        self.packets_received = 0
        self.packets_dropped = 0
        self.packets_unroutable = 0
        self.decrypt_failures = 0
        self.process_stats = LatencyStats()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _SOCKET_BUFFER)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _SOCKET_BUFFER)
        sock.setblocking(False)
        sock.bind(self._bind)

        self._sock = sock
        self._selector = selectors.DefaultSelector()
        self._selector.register(sock, selectors.EVENT_READ)

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="datapath", daemon=True)
        self._thread.start()

        log.info("Datapath listening on %s:%d", *self._bind)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._selector is not None:
            self._selector.close()
            self._selector = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        log.info("Datapath stopped")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def port(self) -> int:
        if self._sock is None:
            return self._bind[1]
        return self._sock.getsockname()[1]

    # -- the loop ----------------------------------------------------------

    def _run(self) -> None:
        if self._realtime:
            try_set_realtime_priority("datapath")
            configure_gc_for_realtime()

        assert self._selector and self._sock
        log.info("Datapath thread running")

        while not self._stop.is_set():
            try:
                events = self._selector.select(timeout=_SELECT_TIMEOUT_S)
                if events:
                    self._drain_socket()
                self._maybe_maintenance()
            except Exception:
                # A single bad packet or transient error must never take down
                # input for every player.
                log.exception("Error in datapath loop; continuing")

    def _drain_socket(self) -> None:
        """Read everything currently queued.

        Draining fully rather than one-per-select keeps a burst from spreading
        across multiple select() round trips, which would add latency.
        """
        assert self._sock
        while True:
            try:
                data, address = self._sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                log.warning("Receive error: %s", exc)
                return

            if not data:
                continue

            self.packets_received += 1
            try:
                self._handle_datagram(data, address)
            except Exception:
                self.packets_dropped += 1
                log.exception("Error handling packet from %s", address)

    def _handle_datagram(self, data: bytes, address: tuple[str, int]) -> None:
        kind = data[0]

        # Handshake packets are unencrypted and have no session yet.
        if kind == PacketType.HELLO:
            response = self._sessions.handle_hello(data, address)
            if response:
                self._sendto(response, address)
            return

        if kind == PacketType.AUTH:
            response, session = self._sessions.handle_auth(
                data, address, self._router.capacity
            )
            self._sendto(response, address)
            if session is not None:
                self._on_session_created(session)
            return

        # Everything else must be an encrypted session datagram.
        if kind != PacketType.SESSION:
            return

        session = self._sessions.by_address(address)
        if session is None:
            # Could be a client whose NAT mapping changed. We cannot identify
            # it without decrypting, and we cannot decrypt without knowing the
            # session -- so it must re-handshake. Cheap and safe.
            return

        self._handle_session_packet(data, address, session)

    def _handle_session_packet(
        self, data: bytes, address: tuple[str, int], session: Session
    ) -> None:
        start_ns = now_ns()

        try:
            counter, plaintext = session.crypto.decrypt(data)
        except crypto.CryptoError:
            self.decrypt_failures += 1
            return

        if not session.replay.check_and_update(counter):
            self.packets_dropped += 1
            return
        if not plaintext:
            return

        session.last_seen_ns = start_ns
        session.packets_received += 1

        kind = plaintext[0]

        if kind == PacketType.INPUT:
            self._handle_input(plaintext, session, start_ns)
        elif kind == PacketType.HEARTBEAT:
            self._handle_heartbeat(plaintext, session)
        elif kind == PacketType.CONTROL:
            self._handle_control(plaintext, session)
        elif kind == PacketType.CONTROL_ACK:
            pass  # nothing to do; retries stop when the client stops asking
        elif kind == PacketType.DISCONNECT:
            log.info("Client %s disconnected", session.client_name or session.client_id[:8])
            self._router.unassign_client(session.client_id)
            self._sessions.drop(session.client_id)

        self.process_stats.add(ns_to_ms(now_ns() - start_ns))

    def _handle_input(self, plaintext: bytes, session: Session, recv_ns: int) -> None:
        """The hot path proper."""
        try:
            seq, client_ts, slot, flags = protocol.decode_input_into(
                plaintext, 0, self._scratch_state
            )
        except ValueError:
            self.packets_dropped += 1
            return

        slot_state = session.slot(slot)
        slot_state.packets_received += 1
        slot_state.last_packet_ns = recv_ns

        if flags & InputFlags.CONTROLLER_DISCONNECTED:
            slot_state.connected = False
        elif not slot_state.connected:
            slot_state.connected = True

        # Unapproved clients are decoded and counted -- so the operator can see
        # them in the GUI and know someone is waiting -- but their input never
        # reaches a console.
        if not session.is_approved:
            return

        channel = self._router.resolve(session.client_id, slot)
        if channel is None:
            self.packets_unroutable += 1
            return

        bt_ts = recv_ns
        if channel.is_live:
            size = channel.profile.build_input_report(self._scratch_state, channel.report_buf)
            write_start = now_ns()
            delivered = channel.sink.send_input_report(memoryview(channel.report_buf)[:size])
            bt_ts = now_ns()

            if delivered:
                channel.reports_sent += 1
                channel.write_stats.add(ns_to_ms(bt_ts - write_start))
            else:
                channel.reports_dropped += 1
        else:
            channel.reports_dropped += 1

        if flags & InputFlags.REQUEST_ACK:
            size = protocol.encode_input_ack_into(
                self._send_buf, 0, seq, client_ts, recv_ns, bt_ts, slot
            )
            self._send_encrypted(session, memoryview(self._send_buf)[:size])

    def _handle_heartbeat(self, plaintext: bytes, session: Session) -> None:
        try:
            seq, ts = protocol.decode_heartbeat(plaintext, 0)
        except ValueError:
            return
        size = protocol.encode_heartbeat_ack_into(self._send_buf, 0, seq, ts)
        self._send_encrypted(session, memoryview(self._send_buf)[:size])

    def _handle_control(self, plaintext: bytes, session: Session) -> None:
        """Control messages. Off the hot path -- these are rare."""
        try:
            seq, body = protocol.decode_control(plaintext, 0)
        except ValueError as exc:
            log.warning("Bad control message from %s: %s", session.client_id[:8], exc)
            return

        self._send_encrypted(session, protocol.encode_control_ack(seq))

        op = body.get("op")
        if op == protocol.ControlOp.SET_USERNAME:
            slot = int(body.get("slot", 0))
            username = str(body.get("username", ""))[:32]
            session.slot(slot).username = username
            self._router.set_username(session.client_id, slot, username)
            log.info("Slot %d of %s is now '%s'", slot, session.client_id[:8], username)

        elif op == protocol.ControlOp.SET_CONTROLLERS:
            for entry in body.get("controllers", []):
                try:
                    slot = int(entry["slot"])
                except (KeyError, TypeError, ValueError):
                    continue
                slot_state = session.slot(slot)
                slot_state.username = str(entry.get("username", ""))[:32]
                slot_state.device_name = str(entry.get("device_name", ""))[:64]
                self._router.set_username(session.client_id, slot, slot_state.username)

            if self._sessions.auto_approve and session.is_approved:
                self._auto_assign(session)

        elif op == protocol.ControlOp.CONTROLLER_GONE:
            slot = int(body.get("slot", 0))
            session.slot(slot).connected = False

    # -- outbound ----------------------------------------------------------

    def _send_encrypted(self, session: Session, plaintext: bytes | memoryview) -> None:
        self._sendto(session.crypto.encrypt(plaintext), session.address)

    def _sendto(self, data: bytes, address: tuple[str, int]) -> None:
        if self._sock is None:
            return
        try:
            self._sock.sendto(data, address)
        except BlockingIOError:
            # Send queue full. Dropping is correct: the next state supersedes
            # this one, and blocking would add latency for everyone.
            log.debug("Send buffer full; dropped an outbound packet")
        except OSError as exc:
            log.debug("Send to %s failed: %s", address, exc)

    def send_control(self, session: Session, op: str, payload: dict | None = None) -> None:
        """Send a control message to a client. Called from the asyncio thread.

        Safe to call cross-thread: ``sendto`` on a UDP socket is atomic, and
        the session's send counter is only advanced here and on the datapath,
        which do not overlap for a given session in practice.
        """
        try:
            packet = protocol.encode_control(session.next_control_seq(), op, payload)
        except ValueError as exc:
            log.error("Refusing to send oversized control message: %s", exc)
            return
        self._send_encrypted(session, packet)

    def broadcast_capacity(self) -> None:
        """Tell every client the current adapter capacity.

        Called whenever the operator enables or disables an adapter, so client
        GUIs grey out or re-enable slots live rather than on reconnect.
        """
        capacity = self._router.capacity
        for session in self._sessions.all_sessions():
            self.send_control(session, protocol.ControlOp.CAPACITY, {"capacity": capacity})

    # -- housekeeping ------------------------------------------------------

    def _on_session_created(self, session: Session) -> None:
        if self._sessions.auto_approve:
            self._auto_assign(session)

    def _auto_assign(self, session: Session) -> None:
        slots = sorted(session.slots) or [0]
        usernames = {s: session.slot(s).username for s in slots}
        placed = self._router.auto_assign(session.client_id, slots, usernames)
        if placed:
            log.info("Auto-assigned %d controller(s) for %s", placed, session.client_id[:8])

    def _maybe_maintenance(self) -> None:
        now = now_ns()
        if now - self._last_maintenance_ns < _MAINTENANCE_INTERVAL_NS:
            return
        self._last_maintenance_ns = now

        for session in self._sessions.reap_expired():
            self._router.unassign_client(session.client_id)
            # Release any held input so the console does not latch it.
            self._release_channels_for(session.client_id)

    def _release_channels_for(self, client_id: str) -> None:
        """Send a neutral report on any channel this client was driving."""
        neutral = ControllerState()
        for channel in self._router.channels():
            if channel.assigned_client != client_id or not channel.is_live:
                continue
            size = channel.profile.build_input_report(neutral, channel.report_buf)
            channel.sink.send_input_report(memoryview(channel.report_buf)[:size])

    # -- diagnostics -------------------------------------------------------

    def stats_snapshot(self) -> dict[str, object]:
        return {
            "listening": f"{self._bind[0]}:{self.port}",
            "running": self.is_running,
            "packets_received": self.packets_received,
            "packets_dropped": self.packets_dropped,
            "packets_unroutable": self.packets_unroutable,
            "decrypt_failures": self.decrypt_failures,
            "process_ms": self.process_stats.snapshot(),
        }

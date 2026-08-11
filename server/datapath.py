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

#: Rumble updates per controller, per second. A console can emit these far
#: faster than a player can feel them; unthrottled they would compete with
#: input on the same socket for no benefit.
RUMBLE_MAX_HZ = 50
_RUMBLE_MIN_INTERVAL_NS = 1_000_000_000 // RUMBLE_MAX_HZ


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
        rendezvous=None,
        rumble_enabled: bool = True,
    ) -> None:
        self._sessions = sessions
        self._router = router
        self._bind = (bind_host, bind_port)
        self._realtime = realtime

        #: Optional RendezvousClient. Shares this socket deliberately -- the
        #: NAT mapping it opens must be the one gameplay traffic uses.
        self._rendezvous = rendezvous

        #: Server-side rumble switch. The client has its own; feedback is
        #: transmitted only when both are on.
        self.rumble_enabled = rumble_enabled

        self._sock: socket.socket | None = None
        self._selector: selectors.BaseSelector | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        #: Reused across packets -- the datapath must not allocate.
        self._scratch_state = ControllerState()
        self._send_buf = bytearray(protocol.MAX_DATAGRAM)
        #: Separate buffer: rumble is built on a Bluetooth thread while the
        #: datapath thread may be mid-way through _send_buf.
        self._rumble_buf = bytearray(64)
        self._last_rumble_ns: dict[tuple[str, int], int] = {}

        self._last_maintenance_ns = 0

        #: Whether client traffic is accepted at all. Rebound atomically by
        #: set_accepting(); a plain attribute read on the datapath thread is a
        #: single bytecode under the GIL, so the gate costs nothing measurable.
        self._accepting = True

        # Diagnostics. Cheap counters; the web GUI reads them at 10 Hz.
        self.packets_received = 0
        self.packets_dropped = 0
        self.packets_unroutable = 0
        self.decrypt_failures = 0
        self.rebinds = 0
        self.rumble_sent = 0
        self.process_stats = LatencyStats()

    # -- lifecycle ---------------------------------------------------------

    @property
    def accepting(self) -> bool:
        return self._accepting

    def set_accepting(self, accepting: bool) -> int:
        """Switch client traffic on or off. Returns how many sessions were dropped.

        Switching off closes every live session so nothing keeps streaming, and
        drops new datagrams before they are parsed. The Bluetooth side is
        untouched by design: adapters stay registered and paired consoles stay
        connected, so toggling this never disturbs a console mid-game.
        """
        accepting = bool(accepting)
        if accepting == self._accepting:
            return 0

        self._accepting = accepting

        if accepting:
            log.info("Server is accepting client connections")
            return 0

        dropped = 0
        for session in self._sessions.all_sessions():
            # Release the controller first: nothing may keep holding an adapter
            # once its client is gone, or the console would latch the last state
            # the departed player left behind.
            self._router.unassign_client(session.client_id)
            if self._sessions.drop(session.client_id):
                dropped += 1

        log.info("Server stopped accepting clients (%d session(s) closed)", dropped)
        return dropped

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
        # --- "Server off" gate, before anything else ---
        #
        # Checked ahead of every parse, punch reply and crypto operation, so a
        # switched-off server does no work on behalf of an unauthenticated
        # stranger. Rejecting here rather than closing the socket keeps the
        # on/off toggle instant and free of port-rebind races, while presenting
        # the same behaviour to the outside: nothing gets in.
        if not self._accepting:
            return

        # --- NAT hole-punching ---
        #
        # Probes arrive before any session exists, and answering them from this
        # socket is the point: the reply opens our NAT mapping toward the peer.
        if data.startswith(protocol.PUNCH_PROBE):
            self._sendto(protocol.PUNCH_ACK_PROBE, address)
            return
        if data.startswith(protocol.PUNCH_ACK_PROBE):
            return

        # Broker signalling shares this socket so the punched mapping is the
        # one gameplay uses.
        if self._rendezvous is not None and self._rendezvous.owns(address):
            self._rendezvous.handle_datagram(data, address)
            return

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
            session = self._adopt_rebound_session(data, address)
            if session is None:
                return

        self._handle_session_packet(data, address, session)

    def _adopt_rebound_session(self, data: bytes, address: tuple[str, int]):
        """Follow a session whose source address changed (NAT rebinding).

        Routine on a hole-punched path: a NAT can reassign the external port at
        any time, and without this the session would silently die until the
        client re-handshakes.

        Safe because a session only moves if the datagram both **decrypts** and
        **passes the replay window**. Decryption proves the sender holds the
        session key; the replay check stops an attacker from moving a session by
        replaying a captured packet from a spoofed address. Failing either, the
        packet is dropped and the session stays where it was.

        Cost is bounded: at most one trial decrypt per live session (<= 4), and
        only for datagrams from an address we do not already know.
        """
        for candidate in self._sessions.all_sessions():
            try:
                counter, plaintext = candidate.crypto.decrypt(data)
            except crypto.CryptoError:
                continue

            if not candidate.replay.check_and_update(counter):
                # Authentic key but a stale counter -- a replay. Do not move.
                self.packets_dropped += 1
                return None

            old = candidate.address
            self._sessions.update_address(candidate, address)
            self.rebinds += 1
            log.info(
                "Session %s moved %s:%d -> %s:%d (NAT rebinding)",
                candidate.client_id[:8],
                old[0],
                old[1],
                address[0],
                address[1],
            )

            # The replay window has already consumed this counter, so hand the
            # decrypted payload straight to the dispatcher rather than letting
            # _handle_session_packet decrypt it a second time and reject it.
            candidate.last_seen_ns = now_ns()
            candidate.packets_received += 1
            self._dispatch_plaintext(plaintext, candidate)
            return None

        return None

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
        session.last_seen_ns = start_ns
        session.packets_received += 1

        self._dispatch_plaintext(plaintext, session, start_ns)

        self.process_stats.add(ns_to_ms(now_ns() - start_ns))

    def _dispatch_plaintext(self, plaintext: bytes, session: Session, recv_ns: int = 0) -> None:
        """Route a decrypted payload to its handler.

        Split out so the NAT-rebinding path can reuse it without decrypting or
        replay-checking a second time.
        """
        if not plaintext:
            return
        if recv_ns == 0:
            recv_ns = now_ns()

        kind = plaintext[0]

        if kind == PacketType.INPUT:
            self._handle_input(plaintext, session, recv_ns)
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

        elif op == protocol.ControlOp.SET_RUMBLE:
            # Live toggle: the client can turn rumble on or off mid-session
            # without reconnecting, and we stop transmitting immediately.
            wanted = bool(body.get("enabled", False))
            if wanted != session.rumble_enabled:
                session.rumble_enabled = wanted
                log.info(
                    "Client %s %s rumble",
                    session.client_id[:8],
                    "enabled" if wanted else "disabled",
                )

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

        if self._rendezvous is not None:
            # Re-registration and outbound punching. Runs on this thread so the
            # packets leave the datapath socket.
            try:
                self._rendezvous.tick()
            except Exception:
                log.exception("Rendezvous tick failed; continuing")

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
            "nat_rebinds": self.rebinds,
            "rumble_enabled": self.rumble_enabled,
            "rumble_sent": self.rumble_sent,
            "process_ms": self.process_stats.snapshot(),
            "rendezvous": (
                self._rendezvous.snapshot() if self._rendezvous is not None else None
            ),
        }

    def send_rumble(self, bd_addr: str, command) -> None:
        """Route a console's rumble back to whichever client drives that adapter.

        Called from a Bluetooth control thread. Sends only when **both** sides
        have opted in: the server's own setting and the client's advertised
        preference. If either is off, nothing is transmitted at all -- the
        packet is never built, so a disabled toggle costs zero bandwidth rather
        than being filtered at the far end.

        Coalesced to RUMBLE_MAX_HZ. A console can emit rumble updates far faster
        than a player can feel them, and unthrottled they would compete with
        input on the same socket. Latest-wins: a dropped update is superseded by
        the next, so rate limiting costs nothing perceptible.
        """
        if not self.rumble_enabled:
            return

        channel = self._router.channel(bd_addr)
        if channel is None or not channel.is_assigned:
            return

        session = self._sessions.by_client_id(channel.assigned_client)
        if session is None or not session.is_approved or not session.rumble_enabled:
            return

        slot = channel.assigned_slot
        now = now_ns()
        key = (channel.assigned_client, slot)

        # A stop is always sent immediately: throttling "stop" would leave the
        # pad buzzing after the effect ended, which is far worse than a dropped
        # start.
        if not command.is_stop:
            last = self._last_rumble_ns.get(key, 0)
            if now - last < _RUMBLE_MIN_INTERVAL_NS:
                return
        self._last_rumble_ns[key] = now

        size = protocol.encode_feedback_into(
            self._rumble_buf,
            0,
            slot,
            command.low_freq,
            command.high_freq,
            command.duration_ms,
        )
        self._send_encrypted(session, memoryview(self._rumble_buf)[:size])
        self.rumble_sent += 1

    def send_raw(self, data: bytes, address: tuple[str, int]) -> None:
        """Send on the datapath socket. Used by the rendezvous client.

        Exposed so broker signalling and punch probes leave from the same
        socket (and therefore the same NAT mapping) as gameplay traffic.
        """
        self._sendto(data, address)

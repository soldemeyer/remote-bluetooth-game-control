"""The media socket: authenticated clients in, encrypted frames out.

Structurally this is a small Datapath. It reuses the server's SessionManager
verbatim, so a viewer authenticates with the same password, the same Argon2id
handshake and the same replay window as a controller client -- there is no
second security model to get wrong.

Two threads:

  * ``vs-recv`` owns the socket and does everything inbound: handshakes, punch
    probes, broker signalling, clock-sync replies, keyframe requests.
  * ``vs-send`` takes encoded frames and fans them out.

**Fan-out is interleaved, not sequential.** Slice *i* goes to every client
before slice *i+1* goes to anyone. Sending one client a whole frame before
starting the next would hand the last client a frame that is already
(clients-1) x frame-time old, which is precisely the asymmetry a four-player
session must not have.

**Large frames are paced.** A keyframe can be 40+ slices; pushing them into the
socket back to back overruns a home router's queue and the loss lands on our
own stream. Bursts are capped and spread across at most half a frame interval,
which is still far faster than the frame rate and never becomes the bottleneck.
"""

from __future__ import annotations

import logging
import selectors
import socket
import threading
from typing import Any, Callable

from common import crypto, protocol, video
from common.protocol import PacketType
from common.timing import LatencyStats, now_ns, sleep_until_ns
from common.video import MediaStats, VideoSettings
from server.sessions import ROLE_BT_SERVER, SessionManager

log = logging.getLogger(__name__)

_SOCKET_BUFFER = 1 << 20
_SELECT_TIMEOUT_S = 0.05
_MAINTENANCE_INTERVAL_NS = 1_000_000_000

#: Slices pushed back to back before the sender yields. See the module note on
#: pacing; 8 x ~1.2 kB is about 10 kB, comfortably inside any sane socket
#: buffer while still bounding the burst.
_BURST_SLICES = 8

#: A frame's fan-out is spread across at most this fraction of its interval.
#: The rest of the interval is slack, so a late frame never cascades.
_PACE_FRACTION = 0.5


class VideoNet:
    """UDP media endpoint with its own session table."""

    def __init__(
        self,
        settings: VideoSettings,
        password: str,
        *,
        bind_host: str = "0.0.0.0",
        bind_port: int = video.DEFAULT_VIDEO_PORT,
        max_clients: int = 4,
        auto_approve: bool = True,
        require_tickets: bool = False,
        on_idr_request: Callable[[], None] | None = None,
        on_control: Callable[[Any, dict], None] | None = None,
    ) -> None:
        self._settings = settings
        self._bind = (bind_host, bind_port)
        self._on_idr_request = on_idr_request

        #: Called with (session, body) for control messages from the Bluetooth
        #: server. It is the only peer that sends any -- viewers have nothing
        #: to say on this channel.
        self._on_control = on_control

        #: Password-gated admission, same machinery as the Bluetooth server.
        #:
        #: The password alone is not the whole check when a Bluetooth server is
        #: in charge of us: it is shared with every player, so it cannot tell
        #: an approved one from a denied one. ``require_tickets`` makes us
        #: additionally demand a token that only that server hands out, which
        #: is what stops "denied" meaning "denied a controller, but do watch".
        #:
        #: Standalone -- no Bluetooth server -- has nobody to issue tickets, so
        #: it stays password-only by necessity.
        self.sessions = SessionManager(
            password,
            max_clients=max_clients,
            auto_approve=auto_approve,
            require_tickets=require_tickets,
        )

        self._sock: socket.socket | None = None
        self._selector: selectors.BaseSelector | None = None
        self._recv_thread: threading.Thread | None = None
        self._send_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_maintenance_ns = 0

        #: Broker client, wired by the pipeline when the Internet path is on.
        self.rendezvous: Any = None

        #: Newest encoded frame awaiting fan-out. Depth 1, latest wins: if the
        #: sender has not picked up the previous frame the encoder has already
        #: produced a better one.
        self._frame_condition = threading.Condition()
        self._pending_frame: Any = None

        #: One reusable buffer per *thread*, not per purpose. Three threads
        #: build outbound packets here -- the frame sender, the audio encoder,
        #: and the receive loop answering probes -- and sharing a buffer
        #: between any two of them corrupts whichever packet loses the race.
        #: A clock-sync ack mangled that way is especially nasty: it decodes
        #: fine and poisons the client's latency estimate with no error
        #: anywhere.
        self._send_buf = bytearray(protocol.MAX_DATAGRAM)      # vs-send
        self._audio_buf = bytearray(protocol.MAX_DATAGRAM)     # audio encoder
        self._reply_buf = bytearray(256)                       # vs-recv
        self._audio_lock = threading.Lock()

        self._frame_id = 0
        self._audio_seq = 0
        self._stats: dict[str, MediaStats] = {}

        self.packets_received = 0
        self.decrypt_failures = 0
        self.rebinds = 0
        self.frames_sent = 0
        self.slices_sent = 0
        self.bytes_sent = 0
        self.send_failures = 0
        self.idr_requests = 0
        self.fanout = LatencyStats()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._recv_thread is not None:
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
        self._recv_thread = threading.Thread(target=self._recv_loop, name="vs-recv", daemon=True)
        self._send_thread = threading.Thread(target=self._send_loop, name="vs-send", daemon=True)
        self._recv_thread.start()
        self._send_thread.start()

        log.info("Media socket listening on %s:%d", self._bind[0], self.port)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        with self._frame_condition:
            self._frame_condition.notify_all()

        for thread in (self._recv_thread, self._send_thread):
            if thread is not None:
                thread.join(timeout=timeout)
        self._recv_thread = None
        self._send_thread = None

        if self._selector is not None:
            self._selector.close()
            self._selector = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        log.info("Media socket stopped")

    @property
    def is_running(self) -> bool:
        return self._recv_thread is not None and self._recv_thread.is_alive()

    @property
    def port(self) -> int:
        if self._sock is None:
            return self._bind[1]
        return self._sock.getsockname()[1]

    @property
    def client_count(self) -> int:
        """How many people are *watching*.

        The Bluetooth server's control session is not one of them, and counting
        it would tell the operator someone is watching when nobody is.
        """
        return sum(
            1
            for session in self.sessions.all_sessions()
            if session.role != ROLE_BT_SERVER
        )

    def set_settings(self, settings: VideoSettings) -> None:
        """Adopt new settings. Only the frame rate matters here, for pacing."""
        self._settings = settings

    def set_tickets(self, tickets: set[str]) -> None:
        """Replace the set of viewers we will admit.

        Revocation has to reach viewers already watching, not just the next
        handshake -- the operator pressed deny to stop someone *now*.
        """
        for session in self.sessions.set_tickets(tickets):
            log.info(
                "Dropping viewer %s: their ticket was withdrawn",
                session.client_name or session.client_id[:8],
            )
            self._stats.pop(session.client_id, None)
            self.sessions.drop(session.client_id)

    # -- producer side -----------------------------------------------------

    def submit_frame(self, frame: Any) -> None:
        """Hand an EncodedFrame to the sender. Latest wins."""
        with self._frame_condition:
            self._pending_frame = frame
            self._frame_condition.notify()

    def send_audio(self, opus: bytes, capture_ts: int) -> None:
        """Send one Opus packet to every client, immediately.

        Called straight from the audio encoder rather than queued: the packet is
        10 ms of sound and a couple of hundred bytes, and any buffering here
        would be buffering we then have to undo on the far side.
        """
        listeners = self._approved_sessions()
        if not listeners:
            return

        with self._audio_lock:
            seq = self._audio_seq
            self._audio_seq = (seq + 1) & 0xFFFFFFFF
            size = video.encode_audio_frame_into(self._audio_buf, 0, seq, capture_ts, opus)
            payload = bytes(self._audio_buf[:size])

        for session in listeners:
            self._send_encrypted(session, payload)

    # -- inbound -----------------------------------------------------------

    def _recv_loop(self) -> None:
        assert self._selector and self._sock
        while not self._stop.is_set():
            try:
                events = self._selector.select(timeout=_SELECT_TIMEOUT_S)
                if events:
                    self._drain_socket()
                self._maybe_maintenance()
            except Exception:
                # One bad packet must never take the stream down for everyone.
                log.exception("Error in media receive loop; continuing")

    def _drain_socket(self) -> None:
        assert self._sock
        while True:
            try:
                data, address = self._sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                log.debug("Media receive error: %s", exc)
                return

            if not data:
                continue
            self.packets_received += 1
            try:
                self._handle_datagram(data, address)
            except Exception:
                log.exception("Error handling media packet from %s", address)

    def _handle_datagram(self, data: bytes, address: tuple[str, int]) -> None:
        # Punch probes arrive before any session exists; answering opens our
        # NAT mapping toward the peer.
        if data.startswith(protocol.PUNCH_PROBE):
            self._sendto(protocol.PUNCH_ACK_PROBE, address)
            return
        if data.startswith(protocol.PUNCH_ACK_PROBE):
            return

        if self.rendezvous is not None and self.rendezvous.owns(address):
            self.rendezvous.handle_datagram(data, address)
            return

        kind = data[0]

        if kind == PacketType.HELLO:
            response = self.sessions.handle_hello(data, address)
            if response:
                self._sendto(response, address)
            return

        if kind == PacketType.AUTH:
            response, session = self.sessions.handle_auth(data, address, capacity=0)
            self._sendto(response, address)
            if session is not None:
                self._stats.setdefault(session.client_id, MediaStats())
                if self.rendezvous is not None:
                    self.rendezvous.peer_connected(address)

                if session.role == ROLE_BT_SERVER:
                    self._retire_stale_controllers(session)

                log.info(
                    "%s %s connected from %s:%d",
                    "Controller" if session.role == ROLE_BT_SERVER else "Viewer",
                    session.client_name or session.client_id[:8],
                    address[0],
                    address[1],
                )
                # A fresh viewer has no reference frame, so it needs one now
                # rather than at the next scheduled keyframe.
                self._request_idr()
            return

        if kind != PacketType.SESSION:
            return

        session = self.sessions.by_address(address)
        if session is None:
            session = self._adopt_rebound(data, address)
            if session is None:
                return
            return

        try:
            counter, plaintext = session.crypto.decrypt(data)
        except crypto.CryptoError:
            self.decrypt_failures += 1
            return
        if not session.replay.check_and_update(counter):
            return

        session.last_seen_ns = now_ns()
        session.packets_received += 1
        self._dispatch(plaintext, session)

    def _adopt_rebound(self, data: bytes, address: tuple[str, int]):
        """Follow a session whose source address changed. See Datapath's note.

        Safe for the same reason: the datagram must both decrypt and pass the
        replay window before a session moves.
        """
        for candidate in self.sessions.all_sessions():
            try:
                counter, plaintext = candidate.crypto.decrypt(data)
            except crypto.CryptoError:
                continue
            if not candidate.replay.check_and_update(counter):
                return None

            old = candidate.address
            self.sessions.update_address(candidate, address)
            self.rebinds += 1
            log.info(
                "Media session %s moved %s:%d -> %s:%d (NAT rebinding)",
                candidate.client_id[:8], old[0], old[1], address[0], address[1],
            )
            candidate.last_seen_ns = now_ns()
            self._dispatch(plaintext, candidate)
            return None
        return None

    def _dispatch(self, plaintext: bytes, session) -> None:
        if not plaintext:
            return
        kind = plaintext[0]

        if kind == PacketType.MEDIA_HEARTBEAT:
            self._handle_clock_probe(plaintext, session)
        elif kind == PacketType.IDR_REQUEST:
            self._handle_idr_request(plaintext, session)
        elif kind == PacketType.MEDIA_REPORT:
            self._handle_report(plaintext, session)
        elif kind == PacketType.HEARTBEAT:
            self._handle_heartbeat(plaintext, session)
        elif kind == PacketType.CONTROL:
            self._handle_control(plaintext, session)
        elif kind == PacketType.CONTROL_ACK:
            pass    # retries stop when the sender stops asking
        elif kind == PacketType.DISCONNECT:
            log.info(
                "%s %s disconnected",
                "Controller" if session.role == ROLE_BT_SERVER else "Viewer",
                session.client_id[:8],
            )
            self._stats.pop(session.client_id, None)
            self.sessions.drop(session.client_id)

    def _handle_control(self, plaintext: bytes, session) -> None:
        """A control message, which only the Bluetooth server may send.

        Role-gated for the same reason the preview path on the other side is:
        this is what carries settings and the list of who may watch, so a
        viewer reaching it could reconfigure the stream or admit itself.
        """
        try:
            seq, body = protocol.decode_control(plaintext, 0)
        except ValueError as exc:
            log.warning("Bad control message from %s: %s", session.client_id[:8], exc)
            return

        self._send_encrypted(session, protocol.encode_control_ack(seq))

        if session.role != ROLE_BT_SERVER:
            # Not suspicious enough to warn about: every client transport
            # announces its rumble preference on connect, so a viewer sends one
            # of these as a matter of course. It is dropped either way.
            log.debug(
                "Ignoring control op %r from %s (role %s)",
                body.get("op"),
                session.client_id[:8],
                session.role,
            )
            return

        if self._on_control is not None:
            try:
                self._on_control(session, body)
            except Exception:
                log.exception("Error handling a control message")

    def send_control(self, session, op: str, payload: dict | None = None) -> None:
        """Send a control message to the Bluetooth server.

        Unreliable by design, like the Bluetooth server's own control sends:
        status is periodic and absolute, so a lost one is superseded a second
        later rather than needing a retransmit.
        """
        try:
            packet = protocol.encode_control(session.next_control_seq(), op, payload)
        except ValueError as exc:
            log.error("Refusing to send oversized control message: %s", exc)
            return
        self._send_encrypted(session, packet)

    def send_to(self, session, plaintext: bytes) -> None:
        """Send one encrypted packet to a specific session."""
        self._send_encrypted(session, plaintext)

    def _retire_stale_controllers(self, current) -> None:
        """Drop any earlier control session when a new one authenticates.

        A Bluetooth server that reconnects -- after a password change, a
        restart, or a blip -- leaves its previous session behind until the
        reaper notices, ten seconds later. For that window there are two, and
        ``control_session()`` may well answer with the dead one: status and
        preview then go to a socket nobody is reading, the configuration is
        never acknowledged, and the operator watches ``config_pending`` stay
        true with the link plainly connected.

        There is only ever one controller, so retiring the old one is both safe
        and what the state already means.
        """
        for session in self.sessions.all_sessions():
            if session.role != ROLE_BT_SERVER or session.client_id == current.client_id:
                continue
            log.info("Replacing the previous control session %s", session.client_id[:8])
            self._stats.pop(session.client_id, None)
            self.sessions.drop(session.client_id)

    def control_session(self):
        """The Bluetooth server's session, if it is connected."""
        for session in self.sessions.all_sessions():
            if session.role == ROLE_BT_SERVER:
                return session
        return None

    def _handle_clock_probe(self, plaintext: bytes, session) -> None:
        """Answer a clock-sync probe, stamping receive and send separately.

        Both stamps are taken as close to the wire as possible: the gap between
        them is the one part of the round trip the client can subtract out, so
        anything we do lazily here shows up as measurement error there.
        """
        received = now_ns()
        try:
            seq, t0 = video.decode_media_heartbeat(plaintext, 0)
        except ValueError:
            return
        size = video.encode_media_heartbeat_ack_into(
            self._reply_buf, 0, seq, t0, received, now_ns()
        )
        self._send_encrypted(session, bytes(self._reply_buf[:size]))

    def _handle_idr_request(self, plaintext: bytes, session) -> None:
        try:
            _, reason = video.decode_idr_request(plaintext, 0)
        except ValueError:
            return
        self.idr_requests += 1
        stats = self._stats.setdefault(session.client_id, MediaStats())
        stats.idr_requests += 1
        log.debug("Keyframe requested by %s (reason %d)", session.client_id[:8], reason)
        self._request_idr()

    def _handle_report(self, plaintext: bytes, session) -> None:
        try:
            report = video.decode_media_report(plaintext, 0)
        except ValueError:
            return
        self._stats.setdefault(session.client_id, MediaStats()).last_report = report

    def _handle_heartbeat(self, plaintext: bytes, session) -> None:
        try:
            seq, send_ts = protocol.decode_heartbeat(plaintext, 0)
        except ValueError:
            return
        size = protocol.encode_heartbeat_ack_into(self._reply_buf, 0, seq, send_ts)
        self._send_encrypted(session, bytes(self._reply_buf[:size]))

    def _request_idr(self) -> None:
        if self._on_idr_request is not None:
            try:
                self._on_idr_request()
            except Exception:
                log.debug("IDR request callback raised", exc_info=True)

    def _maybe_maintenance(self) -> None:
        now = now_ns()
        if now - self._last_maintenance_ns < _MAINTENANCE_INTERVAL_NS:
            return
        self._last_maintenance_ns = now

        if self.rendezvous is not None:
            try:
                self.rendezvous.tick()
            except Exception:
                log.debug("Rendezvous tick failed", exc_info=True)

        for expired in self.sessions.reap_expired():
            log.info("Viewer %s timed out", expired.client_id[:8])
            self._stats.pop(expired.client_id, None)

    # -- outbound ----------------------------------------------------------

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            with self._frame_condition:
                if self._pending_frame is None:
                    self._frame_condition.wait(0.5)
                frame, self._pending_frame = self._pending_frame, None
            if frame is None:
                continue
            try:
                self._fan_out(frame)
            except Exception:
                log.exception("Error sending a frame; continuing")

    def _fan_out(self, frame: Any) -> None:
        listeners = self._approved_sessions()
        if not listeners:
            return

        started = now_ns()
        payload = memoryview(frame.data)
        total = len(payload)
        count = video.slice_count_for(total)
        frame_id = self._frame_id
        self._frame_id = (frame_id + 1) & 0xFFFFFFFF
        flags = video.SliceFlags.KEYFRAME if frame.keyframe else video.SliceFlags.NONE

        # Spread a big frame over part of its own interval rather than pushing
        # it all at once -- see the module note on pacing.
        fps = max(self._settings.fps, 1)
        budget_ns = int(1_000_000_000 / fps * _PACE_FRACTION)
        bursts = max((count + _BURST_SLICES - 1) // _BURST_SLICES, 1)
        gap_ns = budget_ns // bursts if bursts > 1 else 0

        sent_slices = 0
        for index in range(count):
            chunk = payload[
                index * video.VIDEO_SLICE_PAYLOAD : (index + 1) * video.VIDEO_SLICE_PAYLOAD
            ]
            size = video.encode_video_slice_into(
                self._send_buf,
                0,
                frame_id,
                index,
                count,
                flags,
                video.MediaCodec.H264,
                frame.capture_ts,
                chunk,
            )
            packet = bytes(self._send_buf[:size])

            # Interleaved: every client gets slice i before anyone gets i+1.
            for session in listeners:
                self._send_encrypted(session, packet)
                stats = self._stats.setdefault(session.client_id, MediaStats())
                stats.slices_sent += 1
                stats.bytes_sent += size

            sent_slices += 1
            if gap_ns and sent_slices % _BURST_SLICES == 0 and index + 1 < count:
                sleep_until_ns(now_ns() + gap_ns)

        self.frames_sent += 1
        self.slices_sent += count * len(listeners)
        self.bytes_sent += total * len(listeners)
        for session in listeners:
            self._stats.setdefault(session.client_id, MediaStats()).frames_sent += 1
        self.fanout.add((now_ns() - started) / 1_000_000)

    def _approved_sessions(self) -> list:
        """Sessions that should receive media.

        The Bluetooth server's control session is excluded: it is here to
        configure us, not to watch, and sending it the full stream would waste
        the very uplink the players need -- on a link that also carries the
        preview in the other direction.
        """
        return [
            session
            for session in self.sessions.all_sessions()
            if session.is_approved and session.role != ROLE_BT_SERVER
        ]

    def _send_encrypted(self, session, plaintext: bytes | memoryview) -> None:
        try:
            self._sendto(session.crypto.encrypt(plaintext), session.address)
        except crypto.CryptoError:
            log.debug("Encryption failed for %s", session.client_id[:8])

    def _sendto(self, data: bytes, address: tuple[str, int]) -> None:
        if self._sock is None:
            return
        try:
            self._sock.sendto(data, address)
        except BlockingIOError:
            # Send queue full. Dropping is right: the next frame supersedes
            # this one, and blocking would stall every other client too.
            self.send_failures += 1
        except OSError as exc:
            self.send_failures += 1
            log.debug("Media send to %s failed: %s", address, exc)

    def send_raw(self, data: bytes, address: tuple[str, int]) -> None:
        """Escape hatch for broker signalling, which shares this socket.

        The punched mapping belongs to this port, so registration and probes
        have to leave from here or the hole opens on the wrong address.
        """
        self._sendto(data, address)

    # -- introspection -----------------------------------------------------

    def client_snapshot(self) -> list[dict[str, object]]:
        entries = []
        for session in self.sessions.all_sessions():
            stats = self._stats.get(session.client_id, MediaStats())
            entries.append(
                {
                    "client_id": session.client_id,
                    "name": session.client_name,
                    "role": session.role,
                    "address": f"{session.address[0]}:{session.address[1]}",
                    "idle_s": round(session.idle_s, 2),
                    **stats.snapshot(),
                }
            )
        return entries

    def viewer_snapshot(self) -> list[dict[str, object]]:
        """Only the people watching -- what a GUI's viewer list wants."""
        return [
            entry for entry in self.client_snapshot() if entry["role"] != ROLE_BT_SERVER
        ]

    def snapshot(self) -> dict[str, object]:
        return {
            "listening": self.is_running,
            "port": self.port,
            "clients": self.sessions.count,
            "packets_received": self.packets_received,
            "decrypt_failures": self.decrypt_failures,
            "rebinds": self.rebinds,
            "frames_sent": self.frames_sent,
            "slices_sent": self.slices_sent,
            "bytes_sent": self.bytes_sent,
            "send_failures": self.send_failures,
            "idr_requests": self.idr_requests,
            "fanout_ms": self.fanout.snapshot(),
        }

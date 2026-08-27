"""Receiving the video stream.

A second transport on a second socket, deliberately. Sharing the gameplay
socket would put 1.2 kB video slices in the same receive queue as 29-byte input
packets, drained on the thread with a sub-millisecond budget -- the exact
head-of-line problem the input path was designed around. Two sockets means two
NAT mappings to keep open, which is a much cheaper problem: both sides heartbeat
anyway.

No PyAV here. This module gets bytes to the point of being a complete frame and
stops; decoding lives in ``client/media``. That split keeps the network layer
importable on a machine with no media extras, so the client still runs and can
say why video is unavailable.

Threading: one thread owns the socket and the assembler. Completed frames go to
a shallow queue the decoder drains. The GUI never touches any of it directly --
it polls :meth:`snapshot`, matching the rest of the client.
"""

from __future__ import annotations

import logging
import queue
import selectors
import threading
from enum import Enum, auto
from typing import Any, Callable

from common import protocol, video
from common.protocol import PacketType
from common.timing import LatencyStats, now_ns
from common.video import ClockSync, CompletedFrame, FrameAssembler, IdrReason
from client.net.transport import ClientTransport, ConnectionState, TransportError

log = logging.getLogger(__name__)

#: Clock probes: fast while converging, then just often enough to track drift.
_PROBE_FAST_NS = 200_000_000
_PROBE_SLOW_NS = 1_000_000_000
_PROBE_FAST_PERIOD_NS = 5_000_000_000

#: Never ask for keyframes faster than this. A burst of requests from a client
#: on a lossy path would make the source emit nothing but expensive frames,
#: worsening the loss that triggered them.
_IDR_MIN_INTERVAL_NS = 250_000_000

_REPORT_INTERVAL_NS = 1_000_000_000

#: No media for this long means something is wrong; longer still means give up.
_STALL_AFTER_NS = 3_000_000_000
_FAIL_AFTER_NS = 8_000_000_000

_SERVICE_TIMEOUT_S = 0.02

#: Two frames of slack. Deeper would just be latency: if the decoder is behind,
#: the right move is to drop, not to accumulate.
_FRAME_QUEUE_DEPTH = 2


class VideoStreamState(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    STREAMING = auto()
    STALLED = auto()       # connected, but nothing is arriving
    FAILED = auto()


class VideoReceiver:
    """Connects to a video source and turns datagrams into complete frames."""

    def __init__(
        self,
        password: str,
        *,
        client_name: str = "viewer",
        on_audio: Callable[[bytes, int, int], None] | None = None,
        on_state_change: Callable[[VideoStreamState, str], None] | None = None,
        stun_servers: tuple[str, ...] | list[str] = (),
    ) -> None:
        self._password = password
        self._client_name = client_name
        self._on_audio = on_audio
        self._on_state_change = on_state_change

        #: Passed through to the punch on the video socket. Video punches
        #: separately from gameplay -- two sockets, two NAT mappings -- so it
        #: needs its own discovery, not the gameplay socket's answer.
        self._stun_servers = list(stun_servers)

        self._transport: ClientTransport | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._assembler = FrameAssembler()
        self._clock = ClockSync()
        self._frames: queue.Queue[CompletedFrame] = queue.Queue(maxsize=_FRAME_QUEUE_DEPTH)

        self._state = VideoStreamState.DISCONNECTED
        self._detail = ""
        self._started_ns = 0
        self._last_media_ns = 0
        self._last_probe_ns = 0
        self._last_idr_ns = 0
        self._last_report_ns = 0
        self._probe_seq = 0
        self._send_buf = bytearray(protocol.MAX_DATAGRAM)

        #: Filled by the decoder and the window, read when building a report.
        self.decode_stats = LatencyStats()
        self.present_stats = LatencyStats()
        self.audio_underruns = 0

        self.frames_dropped_late = 0
        self.connection_mode = "direct"

        #: Viewing ticket from the advert, presented at the media handshake.
        self._ticket = ""

    # -- lifecycle ---------------------------------------------------------

    def connect_async(self, source: dict[str, Any]) -> None:
        """Start connecting in the background.

        Never blocks: the handshake can take seconds, and the caller is the Qt
        event loop.
        """
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(dict(source),), name="video-recv", daemon=True
        )
        self._thread.start()

    def close(self, timeout: float = 3.0) -> None:
        self._stop.set()

        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                # Still working the connection ladder, most likely mid
                # handshake. Leave the reference in place: the thread checks
                # _stop after each attempt and closes its own transport on the
                # way out. Clearing it here would orphan a socket that is
                # about to become a live session nobody is holding.
                log.debug("Video receiver did not stop in time; it will exit on its own")
                return
            self._thread = None

        transport, self._transport = self._transport, None
        if transport is not None:
            try:
                transport.close()
            except Exception:
                log.debug("Error closing the video transport", exc_info=True)
        self._set_state(VideoStreamState.DISCONNECTED)

    @property
    def state(self) -> VideoStreamState:
        return self._state

    @property
    def state_detail(self) -> str:
        return self._detail

    @property
    def is_streaming(self) -> bool:
        return self._state is VideoStreamState.STREAMING

    def _set_state(self, state: VideoStreamState, detail: str = "") -> None:
        if state is self._state and detail == self._detail:
            return
        self._state = state
        self._detail = detail
        log.info("Video stream: %s%s", state.name, f" ({detail})" if detail else "")
        if self._on_state_change is not None:
            try:
                self._on_state_change(state, detail)
            except Exception:
                log.debug("Video state callback raised", exc_info=True)

    # -- consumer side -----------------------------------------------------

    def get_frame(self, timeout: float = 0.1) -> CompletedFrame | None:
        """Next complete frame, or None. Called by the decoder thread."""
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def clock_offset_ns(self) -> int:
        """Add to a source timestamp to place it on our clock."""
        return self._clock.offset_ns

    @property
    def clock_locked(self) -> bool:
        return self._clock.locked

    # -- the thread --------------------------------------------------------

    def _run(self, source: dict[str, Any]) -> None:
        self._set_state(VideoStreamState.CONNECTING)
        transport = self._connect(source)
        if transport is None:
            return

        # close() may have been called while the ladder was working. Honour it
        # here rather than leaving a session nobody holds a reference to.
        if self._stop.is_set():
            transport.close()
            self._set_state(VideoStreamState.DISCONNECTED)
            return

        self._transport = transport
        self._started_ns = now_ns()
        self._last_media_ns = now_ns()
        self._set_state(VideoStreamState.STREAMING, self.connection_mode)

        # Nothing can be decoded until a keyframe arrives, and the next
        # scheduled one may be seconds away.
        self._request_idr(IdrReason.JOIN, force=True)

        selector = selectors.DefaultSelector()
        try:
            selector.register(transport.fileno(), selectors.EVENT_READ)
        except (TransportError, OSError, ValueError):
            self._set_state(VideoStreamState.FAILED, "socket closed")
            return

        try:
            while not self._stop.is_set():
                selector.select(timeout=_SERVICE_TIMEOUT_S)
                transport.service()

                if transport.state in (
                    ConnectionState.DISCONNECTED,
                    ConnectionState.FAILED,
                ):
                    self._set_state(VideoStreamState.FAILED, transport.state_detail)
                    return

                self._probe_clock(transport)
                self._send_report(transport)
                if self._check_liveness():
                    return
        except Exception:
            log.debug("Video receive loop failed", exc_info=True)
            self._set_state(VideoStreamState.FAILED, "receive error")
        finally:
            selector.close()

    def _connect(self, source: dict[str, Any]) -> ClientTransport | None:
        """Work down the connection ladder, closest path first.

        Order matters: a viewer on the same subnet as the capture PC should not
        be hairpinned through the router, and nobody should be relayed while a
        direct path exists.
        """
        password = str(source.get("password") or self._password)
        # Issued by the Bluetooth server to clients it approved. The source
        # refuses anyone without one, so a stale advert fails cleanly rather
        # than half-connecting.
        self._ticket = str(source.get("ticket") or "")
        attempts: list[tuple[str, str, int]] = []

        lan_host = str(source.get("lan_host") or "")
        host = str(source.get("host") or "")
        port = int(source.get("port") or video.DEFAULT_VIDEO_PORT)

        if lan_host:
            attempts.append(("direct", lan_host, port))
        if host and host != lan_host:
            attempts.append(("direct", host, port))

        errors: list[str] = []
        for mode, target, target_port in attempts:
            if self._stop.is_set():
                return None
            transport = self._make_transport(password)
            try:
                transport.connect(target, target_port, timeout_ns=3_000_000_000)
            except TransportError as exc:
                errors.append(f"{target}:{target_port}: {exc}")
                transport.close()
                continue
            self.connection_mode = mode
            return transport

        broker = str(source.get("broker") or "")
        room = str(source.get("room") or "")
        if broker and room:
            if self._stop.is_set():
                return None
            broker_host, _, broker_port = broker.partition(":")
            transport = self._make_transport(password)
            try:
                outcome = transport.connect_via_broker(
                    broker_host,
                    int(broker_port or 47900),
                    room,
                    timeout_ns=25_000_000_000,
                    # The video leg of the room, not the gameplay one.
                    role="video-client",
                    peer_role="video-source",
                )
                self.connection_mode = "relay" if outcome.is_relayed else "punched"
                return transport
            except TransportError as exc:
                errors.append(f"broker {broker}: {exc}")
                transport.close()

        self._set_state(
            VideoStreamState.FAILED,
            "; ".join(errors) if errors else "no reachable video source",
        )
        return None

    def _make_transport(self, password: str) -> ClientTransport:
        return ClientTransport(
            password,
            client_name=self._client_name,
            on_media=self._on_media,
            auth_extra={"role": "video-client", "ticket": self._ticket},
            rumble_enabled=False,
            stun_servers=self._stun_servers,
        )

    # -- inbound -----------------------------------------------------------

    def _on_media(self, plaintext: bytes) -> None:
        """Dispatch one media packet. Runs on the receive thread."""
        kind = plaintext[0]
        self._last_media_ns = now_ns()

        if kind == PacketType.VIDEO_FRAME:
            self._handle_slice(plaintext)
        elif kind == PacketType.AUDIO_FRAME:
            self._handle_audio(plaintext)
        elif kind == PacketType.MEDIA_HEARTBEAT_ACK:
            self._handle_clock_ack(plaintext)

    def _handle_slice(self, plaintext: bytes) -> None:
        try:
            parsed = video.decode_video_slice(plaintext, 0)
        except ValueError:
            return

        completed = self._assembler.add(*parsed)
        if self._assembler.gap_detected():
            self._request_idr(IdrReason.LOSS)

        if completed is None:
            return

        try:
            self._frames.put_nowait(completed)
        except queue.Full:
            # The decoder is behind. Drop the oldest rather than the newest:
            # keeping the stale one would show an old picture and then still
            # have to catch up.
            try:
                self._frames.get_nowait()
                self._frames.put_nowait(completed)
                self.frames_dropped_late += 1
            except (queue.Empty, queue.Full):
                pass

    def _handle_audio(self, plaintext: bytes) -> None:
        if self._on_audio is None:
            return
        try:
            seq, capture_ts, payload = video.decode_audio_frame(plaintext, 0)
        except ValueError:
            return
        try:
            # `seq` used to be discarded here, which left the audio path with
            # no way to tell a local fault from a lossy one -- no gap, reorder
            # or duplicate detection existed anywhere downstream.
            self._on_audio(bytes(payload), capture_ts, seq)
        except Exception:
            log.debug("Audio callback raised", exc_info=True)

    def _handle_clock_ack(self, plaintext: bytes) -> None:
        arrived = now_ns()
        try:
            _, t0, t1, t2 = video.decode_media_heartbeat_ack(plaintext, 0)
        except ValueError:
            return
        self._clock.add_sample(t0, t1, t2, arrived)

    # -- outbound ----------------------------------------------------------

    def _probe_clock(self, transport: ClientTransport) -> None:
        now = now_ns()
        converging = now - self._started_ns < _PROBE_FAST_PERIOD_NS
        interval = _PROBE_FAST_NS if converging else _PROBE_SLOW_NS
        if now - self._last_probe_ns < interval:
            return
        self._last_probe_ns = now

        seq = self._probe_seq
        self._probe_seq = (seq + 1) & 0xFFFFFFFF
        size = video.encode_media_heartbeat_into(self._send_buf, 0, seq, now_ns())
        transport.send_unreliable(bytes(self._send_buf[:size]))

    def _request_idr(self, reason: int, *, force: bool = False) -> None:
        transport = self._transport
        if transport is None:
            return
        now = now_ns()
        if not force and now - self._last_idr_ns < _IDR_MIN_INTERVAL_NS:
            return
        self._last_idr_ns = now

        size = video.encode_idr_request_into(
            self._send_buf, 0, self._assembler.frames_complete, reason
        )
        try:
            transport.send_unreliable(bytes(self._send_buf[:size]))
        except Exception:
            log.debug("Could not send a keyframe request", exc_info=True)

    def request_idr(self, reason: int = IdrReason.DECODER_ERROR) -> None:
        """Ask the source for a keyframe. Safe from the decoder thread."""
        self._request_idr(reason)

    def _send_report(self, transport: ClientTransport) -> None:
        now = now_ns()
        if now - self._last_report_ns < _REPORT_INTERVAL_NS:
            return
        self._last_report_ns = now

        size = video.encode_media_report_into(
            self._send_buf,
            0,
            self._assembler.frames_complete,
            self._assembler.frames_dropped,
            self._assembler.slices_received,
            self._assembler.slices_lost,
            self.decode_stats.p50,
            self.present_stats.p50,
            self.present_stats.p99,
            self.audio_underruns,
        )
        try:
            transport.send_unreliable(bytes(self._send_buf[:size]))
        except Exception:
            log.debug("Could not send a receiver report", exc_info=True)

    def _check_liveness(self) -> bool:
        """Update state from how long the stream has been quiet. True to stop."""
        idle = now_ns() - self._last_media_ns
        if idle > _FAIL_AFTER_NS:
            self._set_state(VideoStreamState.FAILED, "no video for 8 s")
            return True
        if idle > _STALL_AFTER_NS:
            if self._state is not VideoStreamState.STALLED:
                self._set_state(VideoStreamState.STALLED, "no video arriving")
                self._request_idr(IdrReason.LOSS, force=True)
        elif self._state is VideoStreamState.STALLED:
            self._set_state(VideoStreamState.STREAMING, self.connection_mode)
        return False

    # -- introspection -----------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        return {
            "state": self._state.name,
            "detail": self._detail,
            "mode": self.connection_mode,
            "clock": self._clock.snapshot(),
            "assembler": self._assembler.snapshot(),
            "decode_ms": self.decode_stats.snapshot(),
            "video_latency_ms": self.present_stats.snapshot(),
            "frames_dropped_late": self.frames_dropped_late,
            "audio_underruns": self.audio_underruns,
        }

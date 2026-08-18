"""Audio playout: Opus in, speakers out, with a jitter buffer.

Video and audio are synchronised by construction rather than by scheduling one
against the other. Video presents immediately, always; audio rides a small
fixed buffer. At the latencies this system targets, both land well inside the
~45 ms window where lip-sync error becomes noticeable, and neither ever waits
for the other.

That choice is worth stating plainly because the textbook approach -- decode
both, timestamp both, present audio against the video clock -- would be *worse*
here. It works by delaying whichever stream is early, and the stream that is
early is almost always video. Deliberately adding video latency to match audio
is exactly backwards for a game.

So the buffer is the only lever, and the governor nudges it: if the two drift
apart by more than the audible threshold, the audio target moves a few
milliseconds at a time until they agree. Slow enough not to be heard as pitch
change, fast enough to converge in a few seconds.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Callable

from common.timing import now_ns

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_FRAME = 2 * CHANNELS          # s16 stereo

#: Buffer targets, in milliseconds of audio held before playing.
DEFAULT_TARGET_MS = 30
MIN_TARGET_MS = 20
MAX_TARGET_MS = 60

#: Headroom above the target before audio is thrown away.
#:
#: Deliberately its own constant. The capacity used to *be* MAX_TARGET_MS, so
#: the buffer could never hold more than the largest target the governor might
#: pick -- which left nothing to absorb the late burst a jitter buffer exists
#: for. Measured against a 6 s tone with packets jittered +/-40 ms: 102
#: overruns and **17% of the audio discarded**, heard as constant chopping.
#: With headroom the same run drops nothing.
#:
#: Costs nothing when the path is clean: this is a ceiling, not a target, and
#: the buffer still sits at `target_ms` in steady state.
BURST_HEADROOM_MS = 150

#: Where the governor stops caring. Below this the drift is inaudible, and
#: chasing it would just make the buffer oscillate.
_SYNC_TOLERANCE_MS = 45.0
_GOVERNOR_INTERVAL_NS = 1_000_000_000
_NUDGE_MS = 5


def _ms_to_bytes(ms: float) -> int:
    return int(SAMPLE_RATE * ms / 1000) * BYTES_PER_FRAME


def _perceptual(percent: int) -> float:
    """Slider position to linear gain.

    Squared rather than linear: loudness is roughly logarithmic, so a linear
    gain control does almost nothing over its top half and everything in the
    bottom fifth. Squaring is the cheap approximation Qt itself suggests, and
    it puts the useful range under the middle of the travel where it belongs.
    """
    fraction = max(0.0, min(percent / 100.0, 1.0))
    return fraction * fraction


class AudioPlayout:
    """Decodes Opus and feeds a sink, keeping a bounded buffer.

    The sink is injected rather than constructed here so this class can be
    tested without an audio device -- CI has none, and a test that needs one is
    a test that does not run.
    """

    def __init__(
        self,
        *,
        sink: Any = None,
        target_ms: int = DEFAULT_TARGET_MS,
        volume: int = 100,
        muted: bool = False,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._sink = sink
        self._on_error = on_error
        self._target_ms = float(target_ms)
        self._volume = max(0, min(int(volume), 100))
        self._muted = bool(muted)

        self._decoder: Any = None
        #: Built only if a decoded frame ever arrives in an unexpected layout.
        self._resampler: Any = None
        self._lock = threading.Lock()
        self._buffer: deque[bytes] = deque()
        self._buffered_bytes = 0

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_governor_ns = 0

        #: Audio latency as last measured at the point of buffering, so the
        #: governor can compare it against the video figure.
        self._audio_latency_ms = 0.0

        self.packets_received = 0
        self.samples_played = 0
        self.underruns = 0
        self.overruns = 0
        self.decode_errors = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            import av

            self._decoder = av.CodecContext.create("libopus", "r")
            self._decoder.sample_rate = SAMPLE_RATE
            self._decoder.format = "s16"
            self._decoder.layout = "stereo"
        except Exception as exc:  # noqa: BLE001
            self._report(f"Audio playback needs PyAV with Opus: {exc}")
            return

        if self._sink is None:
            self._sink = _build_qt_sink(self._report)
        if self._sink is None:
            return
        # The device exists now, so the remembered level can finally be applied.
        self._apply_volume()

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="audio-out", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        sink, self._sink = self._sink, None
        if sink is not None:
            try:
                sink.close()
            except Exception:
                log.debug("Error closing the audio sink", exc_info=True)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- output level ------------------------------------------------------

    @property
    def volume(self) -> int:
        """Requested volume, 0-100. Independent of mute."""
        return self._volume

    def set_volume(self, percent: int) -> None:
        """Set the output level, 0-100. Remembered even with no device yet."""
        self._volume = max(0, min(int(percent), 100))
        self._apply_volume()

    @property
    def muted(self) -> bool:
        return self._muted

    def set_muted(self, muted: bool) -> None:
        """Silence output without losing the level the player chose."""
        self._muted = bool(muted)
        self._apply_volume()

    def _apply_volume(self) -> None:
        sink = self._sink
        if sink is None:
            return
        try:
            sink.set_volume(0.0 if self._muted else _perceptual(self._volume))
        except AttributeError:
            # A sink that cannot do volume (the test double, or a backend
            # without it) is not an error -- the stream still plays.
            pass
        except Exception:
            log.debug("Could not set the output volume", exc_info=True)

    # -- inbound -----------------------------------------------------------

    def feed(self, opus: bytes, capture_ts: int, clock_offset_ns: int = 0) -> None:
        """Decode one Opus packet and buffer it. Runs on the receive thread."""
        self.packets_received += 1
        decoder = self._decoder
        if decoder is None:
            return

        try:
            import av

            for frame in decoder.decode(av.Packet(opus)):
                pcm = self._pcm_from(frame)
                if pcm:
                    self._enqueue(pcm)
        except Exception as exc:  # noqa: BLE001
            self.decode_errors += 1
            log.debug("Opus decode failed: %s", exc, exc_info=True)
            return

        if clock_offset_ns:
            local_capture = capture_ts + clock_offset_ns
            self._audio_latency_ms = (now_ns() - local_capture) / 1_000_000

    def _pcm_from(self, frame: Any) -> bytes:
        """Interleaved s16 bytes for exactly the samples this frame holds.

        **Never `bytes(frame.planes[0])`.** FFmpeg sizes an audio plane for the
        largest frame the codec can produce, and for Opus that is 120 ms -- so
        the plane is 23040 bytes whatever the frame actually holds. A 10 ms
        packet carries 1920 bytes of audio in a 23040-byte buffer, and handing
        the whole thing to the sink played 10 ms of sound followed by 110 ms of
        padding, over and over.

        That is exactly what it sounds like: mostly silence, with fragments.
        Nothing errors, the packet counters are perfect, and the padding is
        usually zeroed -- so the only symptom is the sound being wrong.
        """
        if frame.format.is_planar and len(frame.planes) > 1:
            # Not what libopus gives us today (it decodes to packed s16), but a
            # planar frame would put the left channel alone in plane 0 -- half
            # the audio, at the wrong speed. Convert rather than mangle.
            frame = self._to_packed(frame)
            if frame is None:
                return b""

        size = frame.samples * BYTES_PER_FRAME
        return bytes(memoryview(frame.planes[0])[:size])

    def _to_packed(self, frame: Any) -> Any:
        """Convert an unexpected layout to packed s16 stereo at 48 kHz."""
        try:
            import av

            if self._resampler is None:
                self._resampler = av.AudioResampler(
                    format="s16", layout="stereo", rate=SAMPLE_RATE
                )
            converted = self._resampler.resample(frame)
            return converted[0] if converted else None
        except Exception:  # noqa: BLE001
            log.debug("Could not convert audio to packed s16", exc_info=True)
            return None

    def _enqueue(self, pcm: bytes) -> None:
        cap = _ms_to_bytes(self._target_ms + BURST_HEADROOM_MS)
        with self._lock:
            self._buffer.append(pcm)
            self._buffered_bytes += len(pcm)
            # Overrun: the source is ahead of our clock, or the sink stalled.
            # Dropping the oldest skips forward rather than accumulating delay.
            while self._buffered_bytes > cap and len(self._buffer) > 1:
                dropped = self._buffer.popleft()
                self._buffered_bytes -= len(dropped)
                self.overruns += 1

    # -- playback ----------------------------------------------------------

    def _run(self) -> None:
        sink = self._sink
        assert sink is not None
        silence = b"\x00" * _ms_to_bytes(10)

        while not self._stop.is_set():
            try:
                free = sink.bytes_free()
            except Exception:
                log.debug("Audio sink query failed", exc_info=True)
                return

            if free <= 0:
                self._stop.wait(0.005)
                continue

            chunk = self._take(free)
            if chunk is None:
                # Distinguish "still filling the buffer" from "genuinely dry".
                # Both look like no data, but only the second is an underrun:
                # counting the first injects silence throughout normal
                # playback, which drives the buffer to its cap (dropping real
                # audio) and reports a fault rate the source then reacts to.
                if self._is_empty():
                    self.underruns += 1
                    chunk = silence[:free] if free < len(silence) else silence
                else:
                    self._stop.wait(0.005)
                    continue

            try:
                sink.write(chunk)
                self.samples_played += len(chunk) // BYTES_PER_FRAME
            except Exception:
                log.debug("Audio sink write failed", exc_info=True)
                return

    def _is_empty(self) -> bool:
        """True when there is genuinely nothing left to play."""
        with self._lock:
            return self._buffered_bytes == 0

    def _take(self, limit: int) -> bytes | None:
        """Pull up to ``limit`` bytes, holding back the buffer target.

        Returns None both when empty and when the buffer has not yet reached
        its target -- the caller tells them apart with :meth:`_is_empty`.

        **The hold-back is the cushion, and it applies to every read.** It
        looks like it could be a one-time priming step, and that is wrong:
        releasing freely once primed lets the sink drain the whole buffer (it
        asks for everything it has room for), leaving nothing to absorb the
        next late packet. Measured, 6 s of tone at +/-8 ms jitter: holding
        back gives 4 underruns and one gap, priming-only gives **40 underruns
        and gaps up to 259 ms**. Keeping `target_ms` permanently in reserve is
        what makes the buffer a jitter buffer rather than a queue.
        """
        hold = _ms_to_bytes(self._target_ms)
        with self._lock:
            if self._buffered_bytes <= hold:
                return None
            available = min(limit, self._buffered_bytes - hold)
            if available <= 0:
                return None

            out = bytearray()
            while self._buffer and len(out) < available:
                head = self._buffer[0]
                need = available - len(out)
                if len(head) <= need:
                    out += self._buffer.popleft()
                    self._buffered_bytes -= len(head)
                else:
                    out += head[:need]
                    self._buffer[0] = head[need:]
                    self._buffered_bytes -= need
            return bytes(out)

    # -- synchronization ---------------------------------------------------

    def tick_sync(self, video_latency_ms: float) -> None:
        """Nudge the buffer toward the video path's latency. Call about 1 Hz."""
        now = now_ns()
        if now - self._last_governor_ns < _GOVERNOR_INTERVAL_NS:
            return
        self._last_governor_ns = now

        if not video_latency_ms or not self._audio_latency_ms:
            return

        drift = self._audio_latency_ms - video_latency_ms
        if abs(drift) <= _SYNC_TOLERANCE_MS:
            return

        # Audio ahead of video means we are holding too little; behind means
        # too much. Move a few milliseconds at a time -- a large jump would be
        # heard as a click.
        step = -_NUDGE_MS if drift > 0 else _NUDGE_MS
        updated = min(max(self._target_ms + step, MIN_TARGET_MS), MAX_TARGET_MS)
        if updated != self._target_ms:
            log.debug(
                "A/V drift %.1f ms; audio buffer %.0f -> %.0f ms",
                drift, self._target_ms, updated,
            )
            self._target_ms = updated

    @property
    def target_ms(self) -> float:
        return self._target_ms

    @property
    def buffered_ms(self) -> float:
        with self._lock:
            return self._buffered_bytes / BYTES_PER_FRAME / SAMPLE_RATE * 1000

    def _report(self, message: str) -> None:
        log.warning("%s", message)
        if self._on_error is not None:
            try:
                self._on_error(message)
            except Exception:
                log.debug("Audio error callback raised", exc_info=True)

    def snapshot(self) -> dict[str, object]:
        return {
            "running": self.is_running,
            "packets": self.packets_received,
            "underruns": self.underruns,
            "overruns": self.overruns,
            "decode_errors": self.decode_errors,
            "buffered_ms": round(self.buffered_ms, 1),
            "target_ms": round(self._target_ms, 1),
            "latency_ms": round(self._audio_latency_ms, 1),
        }


class QtAudioSink:
    """QAudioSink in push mode, wrapped to the tiny interface AudioPlayout uses."""

    def __init__(self, buffer_ms: int = 40) -> None:
        from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(CHANNELS)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)

        device = QMediaDevices.defaultAudioOutput()
        if device is None or device.isNull():
            raise RuntimeError("no audio output device")

        self._sink = QAudioSink(device, fmt)
        self._sink.setBufferSize(_ms_to_bytes(buffer_ms))
        self._io = self._sink.start()
        if self._io is None:
            raise RuntimeError("could not start the audio device")

    def bytes_free(self) -> int:
        return max(self._sink.bytesFree(), 0)

    def write(self, data: bytes) -> None:
        self._io.write(data)

    def set_volume(self, gain: float) -> None:
        """Linear gain, 0.0-1.0. The perceptual curve is applied by the caller."""
        self._sink.setVolume(max(0.0, min(float(gain), 1.0)))

    def close(self) -> None:
        try:
            self._sink.stop()
        except Exception:
            log.debug("QAudioSink stop failed", exc_info=True)


def _build_qt_sink(report: Callable[[str], None]) -> Any:
    """Build the real sink, reporting rather than raising when there is none.

    A machine with no sound card must still play video.
    """
    try:
        return QtAudioSink()
    except Exception as exc:  # noqa: BLE001
        report(f"Audio output unavailable: {exc}")
        return None

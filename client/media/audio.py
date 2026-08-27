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

from common.diagnostics import enable_if_asked
from common.timing import now_ns

log = logging.getLogger(__name__)

#: Diagnostics ride their own logger so the per-second line can be switched on
#: without raising the level of everything else:
#:
#:     logging.getLogger("rbgc.audio.diag").setLevel(logging.INFO)
#:
diag_log = logging.getLogger("rbgc.audio.diag")

SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_FRAME = 2 * CHANNELS          # s16 stereo
BYTES_PER_MS = SAMPLE_RATE * BYTES_PER_FRAME // 1000     # 192

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
#: **Raised from 150 ms after a measured failure.** A DirectShow capture
#: device delivered 500 ms frames -- FFmpeg's documented dshow default is
#: "typically some multiple of 500ms" -- so every burst overflowed the cap and
#: was partly discarded *on arrival*. Measured on hardware: 62-100 overruns a
#: second and **only ~32% of the audio reaching the speakers**.
#:
#: The source is fixed too (`AudioCapture._open` now asks for a small device
#: buffer), but the client cannot assume every device obeys, and discarding
#: audio is a far worse failure than briefly holding more of it.
#:
#: Costs nothing when the path is clean: this is a ceiling, not a target, and
#: the buffer still sits at `target_ms` in steady state.
BURST_HEADROOM_MS = 600

#: Where the governor stops caring. Below this the drift is inaudible, and
#: chasing it would just make the buffer oscillate.
_SYNC_TOLERANCE_MS = 45.0
_GOVERNOR_INTERVAL_NS = 1_000_000_000
_NUDGE_MS = 5

#: Clock drift correction.
#:
#: The capture card and the playback device have independent crystals, and two
#: nominal 48 kHz clocks do not advance at the same rate. **Measured on real
#: hardware: +100 ppm, which is 6 ms of audio piling up every minute.** Left
#: alone the buffer climbs forever -- observed reaching 590 ms over 97 minutes
#: of play, at which point it was discarding audio *and* carrying two thirds of
#: a second of latency.
#:
#: Correction is by the **rolling minimum**, not the instantaneous level, and
#: that distinction is the whole design. A bursty source swings the level from
#: near zero to hundreds of milliseconds and back; its minimum stays at the
#: target. Drift moves the minimum. Shedding on the instantaneous level would
#: eat every burst -- exactly the audio the headroom exists to keep.
#: The window is measured in **audio played, not wall time**. That is the unit
#: the question is actually asked in -- drift accumulates per sample delivered,
#: not per second the process has existed -- and it means the window does not
#: advance while playback is stopped. It also makes the governor drivable
#: against a virtual clock, which is the only way to test five minutes of
#: 100 ppm drift without waiting five minutes.
_DRIFT_WINDOW_FRAMES = SAMPLE_RATE * 10
_DRIFT_SLACK_MS = 20
_DRIFT_SHED_MS = 10

#: How often the diagnostics line is emitted.
_DIAG_INTERVAL_NS = 1_000_000_000

#: Environment switch for the diagnostics line. `1` enables it; anything that
#: looks like a path also writes it to that file. The video server reads the
#: same variable, so one setting instruments both ends of the audio path.
DIAG_ENV = "RBGC_AUDIO_DIAG"


def _enable_diagnostics_if_asked() -> None:
    """Turn the per-second line on from the environment. See common.diagnostics.

    Shared with the video server so one variable lights up both ends of the
    audio path at once -- they are two processes, and a measurement of only one
    half cannot say which half is at fault.
    """
    enable_if_asked(diag_log, DIAG_ENV)


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


class _PlayoutDiagnostics:
    """One compact line a second describing what the audio path is doing.

    Every figure here exists because the choppy-audio investigation had nothing
    to argue with. The playout reported ``buffered_ms``, ``underruns`` and
    ``overruns``, and all three can read perfectly healthy while the speaker is
    starving -- so a fault that is plainly audible left no trace anywhere.

    **The decisive pair is ``snk`` and ``dry``**: how much audio the *device*
    still holds, and how often that reached zero. ``buf`` measures the queue in
    front of the device, which is not the thing that runs out.

    One line a second rather than one per packet, deliberately: at 100 packets
    a second, logging each would change the timing this is meant to measure.
    Accumulation is O(1) per observation -- running min and sum, no sorting, no
    allocation -- because the loop it sits in does not sleep between writes.

    Counters written by the receive thread (``feed``) and read here on the
    playout thread are deliberately unsynchronised. A meter that loses a sample
    at the moment it resets is still a correct meter, and a lock on this path
    would be a real cost for no gain.
    """

    __slots__ = (
        "_next_ns", "_samples", "_buf_sum", "_buf_min", "_snk_sum", "_snk_min",
        "_dry_events", "_was_dry", "_writes", "_written_bytes",
        "feed_count", "feed_gap_sum_ms", "feed_gap_max_ms", "_last_feed_ns",
    )

    def __init__(self) -> None:
        self._next_ns = 0
        self._last_feed_ns = 0
        self._was_dry = False
        self.feed_count = 0
        self.feed_gap_sum_ms = 0.0
        self.feed_gap_max_ms = 0.0
        self._reset_window()

    def _reset_window(self) -> None:
        self._samples = 0
        self._buf_sum = 0
        self._buf_min = -1
        self._snk_sum = 0
        self._snk_min = -1
        self._dry_events = 0
        self._writes = 0
        self._written_bytes = 0
        self.feed_count = 0
        self.feed_gap_sum_ms = 0.0
        self.feed_gap_max_ms = 0.0

    # -- the playout thread ------------------------------------------------

    def observe(self, buffered_bytes: int, sink_queued: int) -> None:
        """One sample of both queue depths. Called every loop pass."""
        self._samples += 1
        self._buf_sum += buffered_bytes
        self._snk_sum += sink_queued
        if self._buf_min < 0 or buffered_bytes < self._buf_min:
            self._buf_min = buffered_bytes
        if self._snk_min < 0 or sink_queued < self._snk_min:
            self._snk_min = sink_queued

        # Count the *transition* into dry, not every pass spent there: this
        # loop does not sleep after a write, so passes are not a unit of time.
        dry = sink_queued <= 0
        if dry and not self._was_dry:
            self._dry_events += 1
        self._was_dry = dry

    def wrote(self, count: int) -> None:
        self._writes += 1
        self._written_bytes += count

    # -- the receive thread ------------------------------------------------

    def fed(self, now: int) -> None:
        """One packet arrived. Records the gap since the last one."""
        last = self._last_feed_ns
        self._last_feed_ns = now
        self.feed_count += 1
        if not last:
            return
        gap_ms = (now - last) / 1_000_000
        self.feed_gap_sum_ms += gap_ms
        if gap_ms > self.feed_gap_max_ms:
            self.feed_gap_max_ms = gap_ms

    # -- emission ----------------------------------------------------------

    def maybe_emit(self, playout: AudioPlayout) -> None:
        if not diag_log.isEnabledFor(logging.INFO):
            return
        now = now_ns()
        if not self._next_ns:
            self._next_ns = now + _DIAG_INTERVAL_NS
            return
        if now < self._next_ns:
            return

        elapsed_s = _DIAG_INTERVAL_NS / 1_000_000_000
        self._next_ns = now + _DIAG_INTERVAL_NS
        samples = self._samples or 1
        fed = self.feed_count or 1

        diag_log.info(
            "buf %.1f/%.1fms snk %.1f/%.1fms dry %d shed %.0fms "
            "w %d/s %.0fkB/s | feed %d gap %.1f/%.1fms "
            "lost %d gaps %d reord %d dup %d | over %d under %d derr %d "
            "target %.0fms",
            self._buf_sum / samples / BYTES_PER_MS,
            max(self._buf_min, 0) / BYTES_PER_MS,
            self._snk_sum / samples / BYTES_PER_MS,
            max(self._snk_min, 0) / BYTES_PER_MS,
            self._dry_events,
            playout.drift_shed_ms,
            int(self._writes / elapsed_s),
            self._written_bytes / elapsed_s / 1000,
            self.feed_count,
            self.feed_gap_sum_ms / fed,
            self.feed_gap_max_ms,
            playout.packets_lost,
            playout.seq_gaps,
            playout.reordered,
            playout.duplicates,
            playout.overruns,
            playout.underruns,
            playout.decode_errors,
            playout.target_ms,
        )
        self._reset_window()


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

        #: Rolling minimum of in-flight audio, for the drift governor.
        self._drift_min_bytes = -1
        self._drift_next_frames = 0
        self.drift_shed_ms = 0.0

        #: True while filling toward the target before playing anything. Set
        #: again whenever the device genuinely runs dry, so the buffer rebuilds
        #: its cushion instead of limping along at zero for the rest of the
        #: session -- which is what the old code did, with no way to recover.
        self._priming = True

        #: Audio latency as last measured at the point of buffering, so the
        #: governor can compare it against the video figure.
        self._audio_latency_ms = 0.0

        self.packets_received = 0
        self.samples_played = 0
        self.underruns = 0
        self.overruns = 0
        self.decode_errors = 0

        #: Stream continuity, from the wire `seq` the receiver used to discard.
        #: Nothing acts on these yet -- they exist to tell a local fault apart
        #: from a lossy path, which no counter here could previously do.
        self.seq_gaps = 0
        self.packets_lost = 0
        self.reordered = 0
        self.duplicates = 0
        self._last_seq: int | None = None

        self._diag = _PlayoutDiagnostics()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        _enable_diagnostics_if_asked()
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

    def feed(
        self,
        opus: bytes,
        capture_ts: int,
        clock_offset_ns: int = 0,
        seq: int | None = None,
    ) -> None:
        """Decode one Opus packet and buffer it. Runs on the receive thread.

        ``seq`` is optional so a caller that has not got one -- the tests, and
        anything predating the field being plumbed through -- still works.
        """
        self.packets_received += 1
        self._diag.fed(now_ns())
        if seq is not None:
            self._note_seq(seq)
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

    def _note_seq(self, seq: int) -> None:
        """Track continuity of the audio stream. Counts only -- see the plan.

        ``_last_seq`` follows the **highest** sequence seen rather than the
        most recent, so a single reordered packet cannot be mistaken for a gap
        followed by a burst of duplicates.
        """
        last = self._last_seq
        if last is None:
            self._last_seq = seq
            return

        # u32 wrap: the source counter is masked to 32 bits, so the difference
        # has to be too, and the top half of the range means "older than last".
        delta = (seq - last) & 0xFFFFFFFF
        if delta == 0:
            self.duplicates += 1
        elif delta < 0x80000000:
            if delta > 1:
                self.seq_gaps += 1
                self.packets_lost += delta - 1
            self._last_seq = seq
        else:
            self.reordered += 1

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

        while not self._stop.is_set():
            wait_s = self._pump_once(sink)
            if wait_s is None:
                return
            if wait_s:
                self._stop.wait(wait_s)

    def _pump_once(self, sink: Any) -> float | None:
        """One pass of the playout loop. Seconds to wait next, or None to stop.

        **The reserve is counted across the deque and the device together.**
        That is the whole correction. The old loop withheld `target_ms` from
        every read and counted only the deque, so `_buffered_bytes` could never
        fall below the target once it got there: the reserve was unspendable,
        `_is_empty()` was never true again, the underrun branch was dead code,
        and the device was left holding whatever happened to be in flight --
        measured at 4.6 ms against a 30 ms target, and 0 ms twice a second on
        real hardware.

        The intent was right; the accounting was in the wrong place. The device
        is the thing that runs out, so the reserve belongs *in* it.

        Split out of :meth:`_run` so the loop can be stepped against a virtual
        clock -- `tests/test_client_audio_playout.py`.
        """
        diagnostics = self._diag
        try:
            free = sink.bytes_free()
            queued = self._sink_queued(sink)
        except Exception:  # noqa: BLE001
            log.debug("Audio sink query failed", exc_info=True)
            return None

        # Gated on the logger rather than always accumulating: the running
        # sums would otherwise grow for the life of the process with nothing
        # ever consuming them.
        #
        # `_buffered_bytes` is read without the lock on purpose. This is a
        # meter, an int read is atomic under CPython, and a sample one
        # iteration stale changes nothing it is used for. Taking the lock
        # would contend with the receive thread on every pass of a loop that
        # does not sleep between writes.
        buffered = self._buffered_bytes
        if diag_log.isEnabledFor(logging.INFO):
            diagnostics.observe(buffered, queued)
            diagnostics.maybe_emit(self)

        target_bytes = _ms_to_bytes(self._target_ms)
        if not self._priming:
            self._track_drift(buffered + queued, target_bytes)

        # Priming: at startup, and after the device has genuinely run dry.
        # Holding off here is the *one* place withholding is right -- there is
        # nothing to play yet, and starting early only guarantees running out
        # again a moment later.
        if self._priming:
            if buffered + queued < target_bytes:
                return 0.005
            self._priming = False

        if free <= 0:
            return 0.005

        # Top the device up to the target and no further. The deque keeps the
        # rest, so a burst is absorbed rather than either discarded or turned
        # into latency the device then has to carry.
        want = min(free, target_bytes - queued)
        if want <= 0:
            return 0.005

        chunk = self._take(want)
        if chunk is None:
            if queued <= 0:
                # The device is empty and we have nothing for it. This is the
                # real underrun, and it is now reachable -- the old condition
                # (`_buffered_bytes == 0`) could not fire after startup.
                self.underruns += 1
                self._priming = True
            return 0.005

        try:
            sink.write(chunk)
        except Exception:  # noqa: BLE001
            log.debug("Audio sink write failed", exc_info=True)
            return None

        self.samples_played += len(chunk) // BYTES_PER_FRAME
        diagnostics.wrote(len(chunk))
        return 0.0

    def _track_drift(self, inflight: int, target_bytes: int) -> None:
        """Shed audio that clock drift has piled up, leaving bursts alone.

        Keyed on the **minimum** in-flight over a window rather than the level
        right now. A source that delivers in bursts swings between near zero
        and hundreds of milliseconds while its minimum sits at the target;
        drift is what moves the minimum. Shedding on the instantaneous level
        would throw away every burst, which is the audio `BURST_HEADROOM_MS`
        exists to keep.

        Dropping from the front discards what would have played next, which is
        the standard way to catch up: a small skip now rather than a permanent
        offset. At the measured +100 ppm this fires roughly once every 100
        seconds and removes 10 ms.
        """
        if self._drift_min_bytes < 0 or inflight < self._drift_min_bytes:
            self._drift_min_bytes = inflight

        played = self.samples_played
        if not self._drift_next_frames:
            self._drift_next_frames = played + _DRIFT_WINDOW_FRAMES
            return
        if played < self._drift_next_frames:
            return

        floor = self._drift_min_bytes
        self._drift_min_bytes = -1
        self._drift_next_frames = played + _DRIFT_WINDOW_FRAMES

        if floor <= target_bytes + _ms_to_bytes(_DRIFT_SLACK_MS):
            return

        # `_take` removes from the front; the result is simply discarded.
        chunk = self._take(min(_ms_to_bytes(_DRIFT_SHED_MS), floor - target_bytes))
        if chunk:
            self.drift_shed_ms += len(chunk) / BYTES_PER_MS

    @staticmethod
    def _sink_queued(sink: Any) -> int:
        """Bytes the device still holds, or 0 from a sink that cannot say.

        Optional rather than part of the sink protocol: the test doubles
        predate it, and a sink that cannot report its fill is not an error --
        it simply cannot contribute to the measurement.
        """
        getter = getattr(sink, "bytes_queued", None)
        if getter is None:
            return 0
        try:
            return max(int(getter()), 0)
        except Exception:  # noqa: BLE001
            return 0

    def _is_empty(self) -> bool:
        """True when there is genuinely nothing left to play."""
        with self._lock:
            return self._buffered_bytes == 0

    def _take(self, limit: int) -> bytes | None:
        """Pull up to ``limit`` bytes. None when there is nothing at all.

        **No hold-back here any more.** It used to withhold `target_ms` on
        every read, which put the reserve in the deque -- where the audio
        device cannot reach it. `_buffered_bytes` then had a floor it could
        never go below, so the buffer never read empty, the underrun counter
        never moved again, and a gap well inside the target still emptied the
        device. Measured on hardware: 30 ms held, 0 ms at the speaker.

        How much to release is the caller's decision, because only the caller
        knows how much the device is already holding. See :meth:`_pump_once`.
        """
        with self._lock:
            if self._buffered_bytes <= 0:
                return None
            available = min(limit, self._buffered_bytes)
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

    @property
    def sink_ms(self) -> float:
        """Milliseconds of audio the output device still holds.

        The figure the buffer counters could never show, and the one that
        decides whether the speaker is about to run dry.
        """
        sink = self._sink
        if sink is None:
            return 0.0
        return self._sink_queued(sink) / BYTES_PER_MS

    def snapshot(self) -> dict[str, object]:
        return {
            "running": self.is_running,
            "packets": self.packets_received,
            "underruns": self.underruns,
            "overruns": self.overruns,
            "decode_errors": self.decode_errors,
            "buffered_ms": round(self.buffered_ms, 1),
            "sink_ms": round(self.sink_ms, 1),
            "target_ms": round(self._target_ms, 1),
            "latency_ms": round(self._audio_latency_ms, 1),
            "drift_shed_ms": round(self.drift_shed_ms, 1),
            "seq_gaps": self.seq_gaps,
            "packets_lost": self.packets_lost,
            "reordered": self.reordered,
            "duplicates": self.duplicates,
        }


class QtAudioSink:
    """QAudioSink in push mode, wrapped to the tiny interface AudioPlayout uses."""

    def __init__(self, buffer_ms: int = 120) -> None:
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

        # Read back rather than trusting the request: Qt is free to round the
        # buffer to the backend period size, and `bytes_queued` would then be
        # wrong by exactly that difference.
        #
        # Falling back to the requested size matters more than it looks. A
        # backend that answers 0 here would make `bytes_queued` read 0 forever
        # -- which is *precisely* the reading a starved device gives, so the
        # one measurement built to identify the fault would instead fabricate
        # it. A diagnostic that can silently invent its own symptom is worse
        # than no diagnostic.
        requested = _ms_to_bytes(buffer_ms)
        reported = max(int(self._sink.bufferSize()), 0)
        self._capacity = reported or requested
        if not reported:
            log.warning(
                "Audio sink reports no buffer size; assuming the %d bytes "
                "requested (%.1f ms). Device fill readings are estimates.",
                requested, requested / BYTES_PER_MS,
            )
        log.debug(
            "Audio sink open: asked %d bytes, got %d (%.1f ms)",
            requested, self._capacity, self._capacity / BYTES_PER_MS,
        )

    def bytes_free(self) -> int:
        return max(self._sink.bytesFree(), 0)

    def bytes_queued(self) -> int:
        """Bytes written that the device has not played yet."""
        return max(self._capacity - self.bytes_free(), 0)

    @property
    def capacity(self) -> int:
        return self._capacity

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

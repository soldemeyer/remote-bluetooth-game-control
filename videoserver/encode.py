"""Encoding: H.264 for video, Opus for audio, both tuned for latency.

Every encoder setting here exists to stop the encoder buffering. The defaults
of any H.264 encoder assume you are producing a file, where a few frames of
lookahead costs nothing and buys quality; here it costs exactly what we are
trying to save.

The four that matter:

  * **No B-frames.** A B-frame refers forward, so the encoder cannot emit it
    until the next frame arrives -- one guaranteed frame of delay.
  * **No lookahead / zero latency.** One frame in, one frame out.
  * **Tight VBV.** The rate controller is told the buffer is about one frame
    deep, so it cannot save up bits and emit a burst that takes 40 ms to drain
    down a home uplink.
  * **In-band SPS/PPS.** Parameter sets ride every keyframe rather than living
    in a container header, so a client that joins mid-stream needs one IDR and
    nothing else.

Encoder choice is a probe, not a guess: hardware encoders routinely open fine
and fail on the first real frame (a driver that is present but has no free
session, say). ``pick_encoder`` therefore encodes a throwaway frame before
declaring a winner, and falls through to libx264, which always works.
"""

from __future__ import annotations

import logging
import platform
import threading
from dataclasses import dataclass
from typing import Any, Callable

from common.timing import LatencyStats, now_ns
from common.video import VideoSettings

log = logging.getLogger(__name__)

#: Preference order on a desktop: NVIDIA, Intel, AMD, then software.
ENCODER_CHAIN_PC = ("h264_nvenc", "h264_qsv", "h264_amf", "libx264")

#: On a Raspberry Pi, h264_v4l2m2m is the hardware block -- present on the Pi 4,
#: absent on the Pi 5, which has no H.264 encoder at all and must use software.
ENCODER_CHAIN_PI = ("h264_v4l2m2m", "libx264")

#: A forced keyframe costs a large frame, so honour requests at most this often
#: however many clients ask. One IDR serves everyone.
IDR_MIN_INTERVAL_NS = 500_000_000


class EncoderError(RuntimeError):
    """No usable encoder, or the encoder failed mid-stream."""


@dataclass(slots=True)
class EncodedFrame:
    """One encoded frame, ready to slice and send."""

    data: bytes
    keyframe: bool
    capture_ts: int
    encode_ns: int


def _looks_like_a_pi() -> bool:
    """True on ARM Linux, where the encoder chain differs."""
    machine = platform.machine().lower()
    return machine.startswith(("arm", "aarch64"))


def encoder_candidates(preference: str = "auto") -> tuple[str, ...]:
    """The chain to try, honouring an explicit choice by putting it first."""
    chain = ENCODER_CHAIN_PI if _looks_like_a_pi() else ENCODER_CHAIN_PC
    if preference and preference != "auto":
        remaining = tuple(name for name in chain if name != preference)
        return (preference, *remaining)
    return chain


def available_encoders() -> list[str]:
    """Which of our candidates this PyAV build ships. Never raises.

    **Built in, not necessarily usable.** FFmpeg is compiled with NVENC, QSV
    and AMF support regardless of what silicon is present, so this lists all
    three on a machine with only one of them -- or none. Use
    :func:`usable_encoders` when the question is what will actually work.
    """
    try:
        import av
    except ImportError:
        return []
    try:
        pool = av.codecs_available
    except Exception:
        return []
    return [name for name in dict.fromkeys(ENCODER_CHAIN_PC + ENCODER_CHAIN_PI) if name in pool]


#: Cache for usable_encoders(). Probing costs a real encode per candidate, and
#: the answer cannot change without new hardware and a restart.
_usable_cache: list[str] | None = None


def usable_encoders(force: bool = False) -> list[str]:
    """Which encoders actually open and encode on this machine.

    The honest version of :func:`available_encoders`: it opens each candidate
    and pushes a frame through, so an encoder whose hardware is absent is
    excluded rather than merely advertised. Cached, because the probe is not
    free and the answer only changes with the hardware.
    """
    global _usable_cache
    if _usable_cache is not None and not force:
        return list(_usable_cache)

    probe = VideoSettings(width=320, height=240, fps=30, bitrate_kbps=1000)
    usable: list[str] = []
    for name in available_encoders():
        try:
            ctx = _build_context(name, probe)
            _probe_encode(ctx, probe)
        except Exception:
            log.debug("Encoder %s is built in but not usable here", name, exc_info=True)
            continue
        usable.append(name)

    _usable_cache = usable
    return list(usable)


def configure_low_latency(
    ctx: Any, name: str, settings: VideoSettings, *, keyframe_extras: bool = True
) -> None:
    """Apply the settings that stop an encoder buffering. See module docstring.

    ``keyframe_extras`` can be turned off so a caller can retry when an encoder
    rejects them -- see :func:`keyframe_options`.
    """
    fps = max(settings.fps, 1)
    bitrate = settings.bitrate_kbps * 1000

    ctx.bit_rate = bitrate
    ctx.pix_fmt = "yuv420p"
    ctx.gop_size = max(int(settings.gop_s * fps), 1)
    ctx.max_b_frames = 0

    # A VBV about one and a half frames deep: enough to absorb one busy frame,
    # too small to accumulate a burst.
    options: dict[str, str] = {
        "maxrate": str(bitrate),
        "bufsize": str(int(bitrate / fps * 1.5)),
    }

    if name == "libx264":
        options.update(
            {
                "preset": "ultrafast",
                "tune": "zerolatency",
                # sliced-threads keeps threading from adding a frame of delay,
                # which the default frame-threading does.
                #
                # scenecut=0 is not an optimization -- it is required here. A
                # scene cut emits an unscheduled full keyframe, which is the
                # one thing guaranteed to burst the uplink and blow the latency
                # budget, and with rc-lookahead at 0 the detector has nothing to
                # look ahead *at*: on fast-moving content it fires on nearly
                # every frame. Measured on a test pattern, leaving it enabled
                # produced 1202 keyframes out of 1202 frames. Periodic IDR plus
                # an explicit request from a client that lost one is the design.
                "x264-params": (
                    "sliced-threads=1:sync-lookahead=0:rc-lookahead=0:scenecut=0"
                ),
            }
        )
        if settings.intra_refresh:
            # Spreads the cost of a keyframe across a whole GOP, so there is no
            # periodic large frame to spike the uplink.
            options["x264-params"] += ":intra-refresh=1"
    elif name == "h264_nvenc":
        options.update({"preset": "p1", "tuning_info": "ultralowlatency", "delay": "0"})
    elif name == "h264_qsv":
        options.update({"preset": "veryfast", "async_depth": "1", "low_power": "1"})
    elif name == "h264_amf":
        options.update({"usage": "ultralowlatency", "quality": "speed"})

    if keyframe_extras:
        options.update(keyframe_options(name))
    elif name == "h264_v4l2m2m":
        options.pop("maxrate", None)
        options.pop("bufsize", None)

    ctx.options = options


def keyframe_options(name: str) -> dict[str, str]:
    """Options that make a *requested* keyframe actually be one.

    Every client that joins an already-running stream depends on this: it asks
    for a keyframe and can decode nothing until it gets one that carries SPS
    and PPS.

    Hardware encoders do not do that by default. Measured on NVENC with a
    keyframe requested mid-stream: **no IDR at all and no parameter sets** --
    ``pict_type = I`` is quietly ignored, so a viewer who joins after the
    opening frame never decodes anything, forever, with no error on either
    side. libx264 needs nothing here; it honours the request already.

    QSV and AMF are listed from their documentation rather than measurement --
    neither could be opened on the machine this was written on. That is why
    :func:`_build_context` retries without these if the encoder rejects them:
    a guess about an option must never cost somebody a working encoder.
    """
    if name == "h264_nvenc":
        return {"forced-idr": "1", "repeat_headers": "1"}
    if name in ("h264_qsv", "h264_amf"):
        return {"forced_idr": "1"}
    return {}


def pick_encoder(settings: VideoSettings) -> tuple[str, Any]:
    """Open the best working encoder. Returns ``(name, CodecContext)``.

    Each candidate is *used*, not merely opened: hardware encoders frequently
    construct without error and then fail on the first frame, and discovering
    that at startup is far better than discovering it once a player is waiting.
    """
    errors: list[str] = []
    for name in encoder_candidates(settings.encoder):
        try:
            probe = _build_context(name, settings)
        except Exception as exc:  # noqa: BLE001 - absent codec is a normal outcome
            errors.append(f"{name}: unavailable ({exc})")
            continue

        try:
            _probe_encode(probe, settings)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: failed ({exc})")
            log.debug("Encoder %s rejected", name, exc_info=True)
            # PyAV frees a codec context when it is collected; there is no
            # close() to call, so dropping the reference is the teardown.
            continue

        log.info("Encoder: %s at %dx%d %d fps, %d kbps",
                 name, settings.width, settings.height, settings.fps, settings.bitrate_kbps)
        # A *fresh* context, never the one just probed. The probe frame
        # consumes the stream's opening IDR and its SPS/PPS, and those are
        # discarded with the probe's output -- so a context reused afterwards
        # begins mid-GOP with parameter sets nobody ever saw. Every client then
        # receives a stream it cannot decode a single frame of, while the
        # encoder's own counters look perfectly healthy. Measured on NVENC: 70
        # frames, zero IDRs, zero pictures decoded.
        return name, _build_context(name, settings)

    raise EncoderError("No usable H.264 encoder. Tried:\n  " + "\n  ".join(errors))


def _build_context(name: str, settings: VideoSettings):
    """Create, configure and open one encoder context.

    Retries without the keyframe options if the encoder refuses them. They are
    verified only on NVENC, and rejecting an otherwise-working encoder over an
    option guessed from documentation would be a worse outcome than a stream
    whose mid-join behaviour is imperfect -- which is at least visible.
    """
    import av

    def build(keyframe_extras: bool) -> Any:
        ctx = av.CodecContext.create(name, "w")
        ctx.width = settings.width
        ctx.height = settings.height
        ctx.framerate = settings.fps
        ctx.time_base = _time_base(settings.fps)
        configure_low_latency(ctx, name, settings, keyframe_extras=keyframe_extras)
        ctx.open()
        return ctx

    try:
        return build(True)
    except Exception:
        if not keyframe_options(name):
            raise
        log.warning(
            "%s rejected %s; retrying without them. A viewer joining an "
            "already-running stream may wait longer for a usable picture.",
            name,
            ", ".join(sorted(keyframe_options(name))),
        )
        return build(False)


def _time_base(fps: int):
    from fractions import Fraction

    return Fraction(1, max(fps, 1))


def _probe_encode(ctx: Any, settings: VideoSettings) -> None:
    """Encode one black frame to prove the encoder really works."""
    import av

    frame = av.VideoFrame(settings.width, settings.height, "yuv420p")
    for plane in frame.planes:
        # A fresh VideoFrame's buffer is uninitialised; fill it so the probe is
        # deterministic rather than encoding whatever was in that memory.
        plane.update(bytes(plane.buffer_size))
    frame.pts = 0
    frame.time_base = ctx.time_base
    ctx.encode(frame)


class VideoEncoder:
    """Encodes captured frames on its own thread and hands them to a sink.

    Restarting rather than reconfiguring on a settings change is deliberate:
    changing resolution or bitrate on a live codec context is not portable
    across the four encoders here, and a ~200 ms gap when the operator changes
    a setting is not worth the fragility.
    """

    def __init__(
        self,
        settings: VideoSettings,
        source: Any,                                    # VideoCapture
        on_frame: Callable[[EncodedFrame], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._settings = settings
        self._source = source
        self._on_frame = on_frame
        self._on_error = on_error

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._idr_requested = threading.Event()
        self._last_idr_ns = 0
        self._reformatter: Any = None

        self.encoder_name = ""
        self.frames_encoded = 0
        self.bytes_encoded = 0
        self.keyframes = 0
        self.errors = 0
        self.encode = LatencyStats()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="vs-vencode", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def request_idr(self) -> None:
        """Ask for a keyframe. Coalesced -- one IDR serves every client."""
        self._idr_requested.set()

    # -- the thread --------------------------------------------------------

    def _run(self) -> None:
        try:
            self.encoder_name, ctx = pick_encoder(self._settings)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            self._report(str(exc))
            return

        pts = 0
        try:
            while not self._stop.is_set():
                captured = self._source.get_frame(timeout=0.5)
                if captured is None:
                    continue

                started = now_ns()
                try:
                    frame = self._prepare(captured.frame, ctx)
                    frame.pts = pts
                    frame.time_base = ctx.time_base
                    pts += 1

                    # Clearing pict_type is mandatory, not tidiness. A frame
                    # that came out of a decoder carries the picture type it
                    # was *decoded* as, reformat() preserves it, and the
                    # encoder treats a set pict_type as "emit this kind of
                    # frame". Capture sources hand us I on every frame, so
                    # leaving it alone makes the entire stream intra-only:
                    # measured at 90/90 keyframes and 15.6 kB per frame,
                    # against 2/90 and 3.2 kB once cleared. Five times the
                    # bitrate for identical quality, and it looks like
                    # "video is just expensive" rather than like a bug.
                    if self._take_idr_request():
                        frame.pict_type = self._keyframe_type()
                    else:
                        frame.pict_type = self._auto_type()

                    packets = ctx.encode(frame)
                except Exception as exc:  # noqa: BLE001
                    self.errors += 1
                    self._report(f"Encode failed: {exc}")
                    log.debug("Encode error", exc_info=True)
                    continue

                elapsed = now_ns() - started
                for packet in packets:
                    self._emit(packet, captured.capture_ts, elapsed)
        finally:
            try:
                for packet in ctx.encode(None):     # flush
                    self._emit(packet, now_ns(), 0)
            except Exception:
                log.debug("Encoder flush failed", exc_info=True)

    def _emit(self, packet: Any, capture_ts: int, encode_ns: int) -> None:
        data = bytes(packet)
        if not data:
            return
        keyframe = bool(packet.is_keyframe)
        self.frames_encoded += 1
        self.bytes_encoded += len(data)
        if keyframe:
            self.keyframes += 1
        if encode_ns:
            self.encode.add(encode_ns / 1_000_000)
        self._on_frame(
            EncodedFrame(
                data=data, keyframe=keyframe, capture_ts=capture_ts, encode_ns=encode_ns
            )
        )

    def _prepare(self, frame: Any, ctx: Any) -> Any:
        """Scale and convert to what the encoder wants, reusing one reformatter.

        Ours, not the one `frame.reformat()` caches on the frame. Capture hands
        the *same* object to this thread and to both preview encoders, and that
        cached reformatter is shared state between all three -- two threads
        inside it wedge one of them for good. See `videoserver/preview.py`.
        """
        if (
            frame.width == ctx.width
            and frame.height == ctx.height
            and frame.format.name == "yuv420p"
        ):
            return frame
        if self._reformatter is None:
            from av.video.reformatter import VideoReformatter

            self._reformatter = VideoReformatter()
        return self._reformatter.reformat(
            frame, width=ctx.width, height=ctx.height, format="yuv420p"
        )

    def _take_idr_request(self) -> bool:
        """True at most once per IDR_MIN_INTERVAL_NS, however often asked."""
        if not self._idr_requested.is_set():
            return False
        now = now_ns()
        if self._last_idr_ns and now - self._last_idr_ns < IDR_MIN_INTERVAL_NS:
            return False
        self._idr_requested.clear()
        self._last_idr_ns = now
        return True

    @staticmethod
    def _keyframe_type():
        import av

        return av.video.frame.PictureType.I

    @staticmethod
    def _auto_type():
        """Let the encoder decide. See the comment at the call site."""
        import av

        return av.video.frame.PictureType.NONE

    def _report(self, message: str) -> None:
        log.error("%s", message)
        if self._on_error is not None:
            try:
                self._on_error(message)
            except Exception:
                log.debug("Encoder error callback raised", exc_info=True)

    def snapshot(self) -> dict[str, object]:
        return {
            "running": self.is_running,
            "encoder": self.encoder_name,
            "frames_encoded": self.frames_encoded,
            "keyframes": self.keyframes,
            "bytes_encoded": self.bytes_encoded,
            "errors": self.errors,
            "encode_ms": self.encode.snapshot(),
        }


class AudioEncoder:
    """Opus at 48 kHz in 10 ms frames, sent inline as each packet is produced.

    10 ms is the sweet spot: Opus supports 2.5 ms, but the per-packet overhead
    (IP + UDP + AEAD is ~70 bytes) starts to dominate the payload, and the
    saving is invisible next to the playout buffer on the far end.
    """

    SAMPLE_RATE = 48000
    FRAME_SAMPLES = 480          # 10 ms
    LAYOUT = "stereo"

    def __init__(
        self,
        settings: VideoSettings,
        source: Any,                                     # AudioCapture
        on_packet: Callable[[bytes, int], None],         # (opus, capture_ts)
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._settings = settings
        self._source = source
        self._on_packet = on_packet
        self._on_error = on_error

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self.packets_encoded = 0
        self.bytes_encoded = 0
        self.errors = 0

        #: Signal level, 0..1, for the GUI meter. See `_measure_level`.
        self.level_peak = 0.0
        self.level_rms = 0.0
        self.level_ns = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="vs-aencode", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        try:
            import av
        except ImportError as exc:
            self._report(f"Audio encoding needs PyAV: {exc}")
            return

        try:
            ctx = av.CodecContext.create("libopus", "w")
            ctx.sample_rate = self.SAMPLE_RATE
            ctx.format = "s16"
            ctx.layout = self.LAYOUT
            ctx.bit_rate = self._settings.audio_bitrate_kbps * 1000
            ctx.options = {"application": "lowdelay", "frame_duration": "10"}
            ctx.open()
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            self._report(f"Could not open the Opus encoder: {exc}")
            return

        resampler = av.AudioResampler(
            format="s16", layout=self.LAYOUT, rate=self.SAMPLE_RATE
        )

        try:
            while not self._stop.is_set():
                captured = self._source.get_frame(timeout=0.5)
                if captured is None:
                    continue
                try:
                    for resampled in resampler.resample(captured.frame):
                        resampled.pts = None
                        self._measure_level(resampled)
                        for packet in ctx.encode(resampled):
                            data = bytes(packet)
                            if not data:
                                continue
                            self.packets_encoded += 1
                            self.bytes_encoded += len(data)
                            self._on_packet(data, captured.capture_ts)
                except Exception as exc:  # noqa: BLE001
                    self.errors += 1
                    log.debug("Audio encode error: %s", exc, exc_info=True)
        finally:
            try:
                for packet in ctx.encode(None):     # flush
                    data = bytes(packet)
                    if data:
                        self._on_packet(data, now_ns())
            except Exception:
                log.debug("Opus flush failed", exc_info=True)

    def _measure_level(self, frame: Any) -> None:
        """Track how loud the captured audio is, for the GUI's meter.

        Measured here, on audio that has already been resampled to what we
        actually encode, so the meter answers the question an operator asks it:
        "is sound reaching the stream?" -- not "is the device open?", which
        every other counter already answers and which is true of a muted input.

        Peak decays rather than resetting, so a transient is visible for long
        enough to see at a 4 Hz refresh; RMS is instantaneous.

        Deliberately cheap: `struct`-free, one pass over the buffer through
        `memoryview.cast`, no numpy (which the video server does not otherwise
        require) and no per-sample Python loop over anything but a stride.
        """
        try:
            plane = frame.planes[0]
            size = frame.samples * 2 * 2                 # s16, stereo
            samples = memoryview(plane)[:size].cast("h")
            if not len(samples):
                return

            # Every 16th sample is plenty for a level meter and keeps this off
            # the critical path -- 60 samples per 10 ms frame.
            stride = 16
            peak = 0
            total = 0
            count = 0
            for index in range(0, len(samples), stride):
                value = samples[index]
                magnitude = value if value >= 0 else -value
                if magnitude > peak:
                    peak = magnitude
                total += magnitude * magnitude
                count += 1
            if not count:
                return

            self.level_rms = min((total / count) ** 0.5 / 32768.0, 1.0)
            scaled = min(peak / 32768.0, 1.0)
            # Fast attack, slow release: a meter that tracks the decay of the
            # signal exactly is unreadable at any sane refresh rate.
            self.level_peak = (
                scaled if scaled >= self.level_peak else self.level_peak * 0.85
            )
            self.level_ns = now_ns()
        except Exception:  # noqa: BLE001
            log.debug("Could not measure the audio level", exc_info=True)

    def _report(self, message: str) -> None:
        log.error("%s", message)
        if self._on_error is not None:
            try:
                self._on_error(message)
            except Exception:
                log.debug("Audio error callback raised", exc_info=True)

    def snapshot(self) -> dict[str, object]:
        return {
            "running": self.is_running,
            "packets_encoded": self.packets_encoded,
            "bytes_encoded": self.bytes_encoded,
            "errors": self.errors,
            "level_peak": round(self.level_peak, 4),
            "level_rms": round(self.level_rms, 4),
            # Silence and "nothing arriving at all" are different faults with
            # the same reading, and only one of them is the operator's to fix.
            "level_fresh": bool(
                self.level_ns and now_ns() - self.level_ns < 1_000_000_000
            ),
        }

"""Capture: pulling frames off a capture card (or a synthetic test source).

PyAV is imported lazily inside the functions that need it, following the same
rule as the client's optional dependencies: the app must start and explain
itself on a machine where the media extras are missing, not die on import.

Threading: each capture object owns one thread that blocks in FFmpeg's demuxer.
Blocking is correct here -- the device paces us, and any polling loop would
either burn CPU or add latency. Frames are published to a depth-1 slot: if the
encoder has not collected the previous frame, it is overwritten. A stale frame
is worth nothing, and queueing them is how a stream ends up smooth and late.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from common.timing import LatencyStats, now_ns
from common.video import VideoSettings

log = logging.getLogger(__name__)

#: Reopen backoff after a device read fails, in seconds. A capture card that
#: was unplugged should be picked up again when it comes back without the
#: operator restarting anything.
_REOPEN_DELAYS = (1.0, 2.0, 5.0)

#: Audio device buffer to ask for, in milliseconds, best first.
#:
#: **This is the single largest thing measured wrong in the audio path.**
#: FFmpeg's dshow default is, in its own documentation, "typically some
#: multiple of 500ms" -- and a capture card duly delivered 22050-sample frames
#: at 44.1 kHz, which is exactly 500 ms per frame. That is 500 ms of latency
#: before the encoder sees a sample, and it arrives as one burst: the whole
#: 500 ms becomes 50 back-to-back Opus packets, which then overflowed the
#: client's buffer cap and had two thirds of it discarded on arrival.
#:
#: A ladder rather than one value, because a device that will not give the
#: size asked for fails the open outright -- reported only as "Could not set
#: audio options", which reads as a broken device. Falling back costs nothing;
#: refusing to open would cost all the audio.
_AUDIO_BUFFER_MS = (20, 50, 100)


class CaptureError(RuntimeError):
    """The device could not be opened, or vanished mid-stream."""


@dataclass(slots=True)
class CapturedFrame:
    """One frame plus the moment it was captured, on our monotonic clock."""

    frame: Any            # av.VideoFrame; typed loosely so this module imports without av
    capture_ts: int


def default_backend() -> str:
    """The capture backend native to this platform."""
    if sys.platform == "win32":
        return "dshow"
    if sys.platform == "darwin":
        return "avfoundation"
    return "v4l2"


def enumerate_devices(backend: str = "auto") -> list[dict[str, str]]:
    """List capture devices. Never raises -- an empty list means "none found".

    Device enumeration is the least portable corner of the whole feature, so it
    is deliberately best-effort: the operator can always type a device name in
    by hand, and on Windows the ``@device_pnp_...`` form is accepted verbatim.
    """
    if backend == "auto":
        backend = default_backend()

    try:
        if backend == "v4l2":
            return _enumerate_v4l2()
        if backend == "dshow":
            return _enumerate_dshow()
    except Exception:
        log.debug("Device enumeration failed for backend %s", backend, exc_info=True)
    return []


def _enumerate_v4l2() -> list[dict[str, str]]:
    """Scan /sys for video capture nodes, reading the card name where present."""
    from pathlib import Path

    devices: list[dict[str, str]] = []
    for node in sorted(Path("/dev").glob("video*")):
        name = node.name
        sysfs = Path(f"/sys/class/video4linux/{name}/name")
        label = name
        try:
            label = sysfs.read_text(encoding="utf-8").strip() or name
        except OSError:
            pass
        devices.append({"id": str(node), "name": f"{label} ({node})", "kind": "video"})
    return devices


#: One DirectShow device as FFmpeg prints it: a quoted friendly name followed
#: by its kind. Tolerant of line breaks because FFmpeg emits the name, the
#: "(video", and the ")" as *separate* log records, which the joined text puts
#: back together with newlines in the middle.
_DSHOW_DEVICE = re.compile(r'"([^"]+)"\s*\(\s*(video|audio)\s*\)', re.IGNORECASE)

#: The stable identifier that follows each device. Friendly names are not
#: unique -- two identical capture cards produce two identical names -- and
#: FFmpeg accepts this form directly.
_DSHOW_ALTERNATIVE = re.compile(r'Alternative name\s+"([^"]+)"', re.IGNORECASE)


def _enumerate_dshow() -> list[dict[str, str]]:
    """Ask FFmpeg to list DirectShow devices.

    The listing arrives on **FFmpeg's log**, not in the exception. The dummy
    open always fails -- that part is by design and documented -- but the
    exception says only "Immediate exit requested", while the device names go
    to the logging callback. Reading the exception text therefore found
    nothing, every time, on every machine: the dropdown simply stayed empty
    and looked like a driver problem.

    Two details make or break it. The capture is **thread-local**, so a
    concurrent encode keeps its own log lines instead of having them diverted
    here. And the level has to be raised to INFO for the duration: the listing
    is emitted at INFO, so at the default level it is discarded before the
    capture ever sees it -- which is indistinguishable from a machine with no
    capture devices.
    """
    import av

    try:
        previous = av.logging.get_level()
        av.logging.set_level(av.logging.INFO)
        try:
            with av.logging.Capture() as logs:
                try:
                    av.open("dummy", format="dshow", options={"list_devices": "true"})
                except Exception:  # noqa: BLE001 - failing *is* how the listing ends
                    pass
        finally:
            av.logging.set_level(previous)
    except Exception:
        log.debug("Could not capture the DirectShow listing", exc_info=True)
        return []

    text = "".join(message for _level, _name, message in logs)
    return _parse_dshow_listing(text)


def _parse_dshow_listing(text: str) -> list[dict[str, str]]:
    """Turn FFmpeg's device listing into entries. Pure, so it can be tested."""
    devices: list[dict[str, str]] = []

    for match in _DSHOW_DEVICE.finditer(text):
        name = match.group(1).strip()
        kind = match.group(2).lower()
        if not name or name.startswith("@"):
            continue

        # The alternative name is printed after the device it belongs to, so
        # take the first one following this match.
        alternative = _DSHOW_ALTERNATIVE.search(text, match.end())
        following = _DSHOW_DEVICE.search(text, match.end())
        if alternative and following and alternative.start() > following.start():
            alternative = None      # it belongs to the next device, not this one

        devices.append(
            {
                # Prefer the friendly name: it is what the operator recognises,
                # and FFmpeg accepts it. The alternative is kept so a duplicate
                # name can still be told apart.
                "id": name,
                "name": name,
                "kind": kind,
                "alternative": alternative.group(1) if alternative else "",
            }
        )

    return devices


def _open_video_container(settings: VideoSettings):
    """Open the configured video source. Raises CaptureError with a readable why."""
    import av

    if settings.test_source or settings.backend == "lavfi":
        # The `realtime` filter is load-bearing, not decoration. A lavfi source
        # generates frames as fast as the CPU allows -- measured at ~10,000 fps
        # for a 320x240 pattern -- so without it the test source is nothing like
        # a capture card, the encoder is pinned, and every latency number
        # measured against it is meaningless.
        spec = (
            f"testsrc=size={settings.width}x{settings.height}:rate={settings.fps},realtime"
        )
        return av.open(spec, format="lavfi")

    backend = settings.backend if settings.backend != "auto" else default_backend()
    size = f"{settings.width}x{settings.height}"
    base: dict[str, str] = {}

    if backend == "dshow":
        device = settings.device or _first_device_name("video")
        if not device:
            raise CaptureError("No DirectShow video device found.")
        target = device if device.startswith("video=") else f"video={device}"
        base["rtbufsize"] = "64M"
    elif backend == "v4l2":
        target = settings.device or "/dev/video0"
        base["input_format"] = "mjpeg"
    else:
        target = settings.device
        if not target:
            raise CaptureError(f"Backend {backend} needs an explicit device.")

    # The requested size and rate are a *preference*, not a requirement. A
    # camera offers a fixed menu of (format, size, rate) combinations, and
    # asking for one that is not on it fails the open outright -- dshow reports
    # only "Could not set video options", which reads as a broken device.
    #
    # Falling back costs nothing, because capture size and encode size are
    # independent: the encoder reformats whatever arrives to the configured
    # output. So take the best mode the device will actually give us.
    attempts = [
        ("", {"framerate": str(settings.fps), "video_size": size}),
        ("letting it choose the frame rate", {"video_size": size}),
        ("letting it choose the size", {"framerate": str(settings.fps)}),
        ("using its own defaults entirely", {}),
    ]

    problems: list[str] = []
    for concession, extra in attempts:
        try:
            container = av.open(target, format=backend, options={**base, **extra})
        except Exception as exc:
            problems.append(f"{concession or 'as requested'}: {exc}")
            continue

        if concession:
            # Say so plainly. Otherwise the operator sees a resolution or a
            # frame rate they did not choose, with nothing to say the camera
            # refused theirs.
            codec = container.streams.video[0].codec_context
            log.warning(
                "%s refused %s at %d fps; opened it %s and got %dx%d. "
                "The stream is still encoded at %s.",
                target, size, settings.fps, concession,
                codec.width, codec.height, size,
            )
        return container

    raise CaptureError(
        f"Could not open {target} via {backend}: " + "; ".join(problems[:2])
    )


def _first_device_name(kind: str) -> str:
    for entry in enumerate_devices():
        if entry.get("kind") == kind:
            return entry["id"]
    return ""


class VideoCapture:
    """Owns one capture thread and publishes the newest frame.

    The slot is depth 1 on purpose (see the module docstring). ``get_frame``
    blocks until something new arrives so the encoder thread never spins.
    """

    def __init__(
        self,
        settings: VideoSettings,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._settings = settings
        self._on_error = on_error

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._condition = threading.Condition()
        self._pending: CapturedFrame | None = None

        #: Newest frame, kept alongside the consumable slot so a second reader
        #: (the preview) can sample without stealing frames from the encoder.
        #: A bare attribute is enough: rebinding is atomic under the GIL, and a
        #: preview that samples one frame late does not matter.
        self.latest: CapturedFrame | None = None

        self.frames_captured = 0
        self.capture_errors = 0
        self.interval = LatencyStats()
        self._last_capture_ns = 0

        #: Last error text, so a repeating failure is logged once rather than
        #: on every retry.
        self._last_error = ""

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="vs-capture", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        """Ask the capture thread to finish. True if it actually did.

        The reference is **kept** when it does not, which matters more than it
        looks. The pump only notices the stop flag between frames, so a camera
        that has stalled leaves this thread parked inside FFmpeg's read with
        the device still open. Reporting success and dropping the reference
        then lets a restart open a *second* capture on the same device, which
        DirectShow refuses with "Could not set video options" -- forever, and
        with nothing pointing at the real cause.
        """
        self._stop.set()
        with self._condition:
            self._condition.notify_all()

        thread = self._thread
        if thread is None:
            return True

        thread.join(timeout=timeout)
        if thread.is_alive():
            log.warning(
                "Capture thread did not stop; the device is still held. "
                "It will be released when the device next produces a frame."
            )
            return False

        self._thread = None
        return True

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- consumer side -----------------------------------------------------

    def get_frame(self, timeout: float = 1.0) -> CapturedFrame | None:
        """Wait for the next frame. Returns None on timeout or shutdown."""
        with self._condition:
            if self._pending is None:
                self._condition.wait(timeout)
            frame, self._pending = self._pending, None
            return frame

    def _publish(self, frame: Any, capture_ts: int) -> None:
        captured = CapturedFrame(frame=frame, capture_ts=capture_ts)
        self.latest = captured
        with self._condition:
            self._pending = captured
            self._condition.notify()

    # -- the thread --------------------------------------------------------

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                container = _open_video_container(self._settings)
            except CaptureError as exc:
                self.capture_errors += 1
                self._report(str(exc))
                delay = _REOPEN_DELAYS[min(attempt, len(_REOPEN_DELAYS) - 1)]
                attempt += 1
                if self._stop.wait(delay):
                    return
                continue

            attempt = 0
            log.info("Capture started")
            try:
                self._pump(container)
            except Exception as exc:  # noqa: BLE001 - any decode error must retry, not die
                self.capture_errors += 1
                self._report(f"Capture stopped: {exc}")
                log.debug("Capture pump failed", exc_info=True)
            finally:
                try:
                    container.close()
                except Exception:
                    log.debug("Error closing capture container", exc_info=True)

            if self._stop.is_set():
                return
            if self._stop.wait(_REOPEN_DELAYS[0]):
                return

    def _pump(self, container) -> None:
        stream = container.streams.video[0]
        # Frames are of no use to us out of order, and dropping our own
        # threading latency matters more than squeezing the last frame out of
        # a stalled device.
        stream.thread_type = "AUTO"

        for frame in container.decode(stream):
            if self._stop.is_set():
                return
            captured = now_ns()
            if self._last_capture_ns:
                self.interval.add((captured - self._last_capture_ns) / 1_000_000)
            self._last_capture_ns = captured
            self.frames_captured += 1
            self._publish(frame, captured)

    def _report(self, message: str) -> None:
        # A missing capture device retries forever, so the same line would
        # otherwise repeat every couple of seconds for as long as the server
        # runs -- enough to bury everything else in the log, and on the
        # embedded path enough to fill the pipe the parent reads from.
        if message == self._last_error:
            log.debug("%s (still)", message)
        else:
            log.warning("%s", message)
            self._last_error = message

        if self._on_error is not None:
            try:
                self._on_error(message)
            except Exception:
                log.debug("Capture error callback raised", exc_info=True)

    def snapshot(self) -> dict[str, object]:
        return {
            "running": self.is_running,
            "frames_captured": self.frames_captured,
            "errors": self.capture_errors,
            "interval_ms": self.interval.snapshot(),
        }


class AudioCapture:
    """Same shape as VideoCapture, for the card's audio pair.

    Audio uses a small queue rather than a single slot: packets are tiny, and
    dropping one is audible where dropping a video frame is not.
    """

    _MAX_QUEUED = 16

    def __init__(
        self,
        settings: VideoSettings,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._settings = settings
        self._on_error = on_error

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._condition = threading.Condition()
        self._queue: deque[CapturedFrame] = deque(maxlen=self._MAX_QUEUED)

        self.frames_captured = 0
        self.capture_errors = 0
        self.dropped = 0
        self._last_error = ""

        #: Cadence of the device itself. The encoder needs 480-sample frames
        #: and the device supplies whatever it likes -- commonly 1024, which
        #: is 21.3 ms and makes Opus packets leave in bursts of two or three
        #: rather than one every 10 ms. Nothing measured that before.
        self.samples_per_frame = 0
        self.samples_per_frame_min = 0
        self.samples_per_frame_max = 0
        #: The device's own rate, which is not necessarily 48 kHz -- a card
        #: measured in the field ran at 44100. Reporting a frame's duration
        #: against an assumed rate got it wrong by 8%, which is exactly the
        #: sort of thing that makes a reader distrust the whole line.
        self.sample_rate = 0
        self.frame_gap_ms = 0.0
        self.frame_gap_max_ms = 0.0
        self._last_frame_ns = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="vs-acapture", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def get_frame(self, timeout: float = 1.0) -> CapturedFrame | None:
        with self._condition:
            if not self._queue:
                self._condition.wait(timeout)
            if not self._queue:
                return None
            return self._queue.popleft()

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                container = self._open()
            except CaptureError as exc:
                self.capture_errors += 1
                self._report(str(exc))
                delay = _REOPEN_DELAYS[min(attempt, len(_REOPEN_DELAYS) - 1)]
                attempt += 1
                if self._stop.wait(delay):
                    return
                continue

            attempt = 0
            log.info("Audio capture started")
            try:
                stream = container.streams.audio[0]
                for frame in container.decode(stream):
                    if self._stop.is_set():
                        return
                    self.frames_captured += 1
                    self._note_cadence(frame)
                    with self._condition:
                        if len(self._queue) == self._MAX_QUEUED:
                            self.dropped += 1
                        self._queue.append(CapturedFrame(frame=frame, capture_ts=now_ns()))
                        self._condition.notify()
            except Exception as exc:  # noqa: BLE001
                self.capture_errors += 1
                self._report(f"Audio capture stopped: {exc}")
                log.debug("Audio pump failed", exc_info=True)
            finally:
                try:
                    container.close()
                except Exception:
                    log.debug("Error closing audio container", exc_info=True)

            if self._stop.is_set() or self._stop.wait(_REOPEN_DELAYS[0]):
                return

    def _note_cadence(self, frame: Any) -> None:
        """Record what the device is actually delivering, and how often.

        Cheap enough to sit on the capture thread: two comparisons and a
        subtraction per frame, at roughly 47 frames a second.
        """
        rate = int(getattr(frame, "sample_rate", 0) or 0)
        if rate:
            self.sample_rate = rate

        samples = int(getattr(frame, "samples", 0) or 0)
        if samples:
            self.samples_per_frame = samples
            if not self.samples_per_frame_min or samples < self.samples_per_frame_min:
                self.samples_per_frame_min = samples
            if samples > self.samples_per_frame_max:
                self.samples_per_frame_max = samples

        now = now_ns()
        last = self._last_frame_ns
        self._last_frame_ns = now
        if last:
            gap_ms = (now - last) / 1_000_000
            self.frame_gap_ms = gap_ms
            if gap_ms > self.frame_gap_max_ms:
                self.frame_gap_max_ms = gap_ms

    def _open(self):
        import av

        settings = self._settings
        if settings.test_source or settings.backend == "lavfi":
            # `realtime` for the same reason as the video test source.
            return av.open(
                "sine=frequency=440:sample_rate=48000,aresample=48000,arealtime",
                format="lavfi",
            )

        backend = settings.backend if settings.backend != "auto" else default_backend()
        options: dict[str, str] = {}
        if backend == "dshow":
            device = settings.audio_device or _first_device_name("audio")
            if not device:
                raise CaptureError("No DirectShow audio device found.")
            target = device if device.startswith("audio=") else f"audio={device}"
            fmt = "dshow"
        elif backend == "v4l2":
            # ALSA rather than v4l2: video4linux carries no audio.
            target = settings.audio_device or "default"
            fmt = "alsa"
        else:
            target = settings.audio_device
            fmt = backend
            if not target:
                raise CaptureError(f"Backend {backend} needs an explicit audio device.")

        # Ask for a small device buffer, then settle for whatever it will give.
        # Only dshow has the option; ALSA's default period is already small.
        attempts: list[dict[str, str]] = []
        if fmt == "dshow":
            attempts = [{"audio_buffer_size": str(ms)} for ms in _AUDIO_BUFFER_MS]
        attempts.append({})

        last: Exception | None = None
        for options in attempts:
            try:
                container = av.open(target, format=fmt, options=options or None)
            except Exception as exc:  # noqa: BLE001
                last = exc
                continue
            if options:
                log.info(
                    "Audio capture asked for a %s ms device buffer",
                    options["audio_buffer_size"],
                )
            else:
                log.warning(
                    "Audio device %s would not accept a buffer size; using its "
                    "default. If it is large, audio will arrive in bursts -- "
                    "check the frame size in the RBGC_AUDIO_DIAG source line.",
                    target,
                )
            return container

        raise CaptureError(f"Could not open audio {target}: {last}") from last

    def _report(self, message: str) -> None:
        # Same reasoning as VideoCapture._report: a missing device retries
        # forever, and the repeated line is pure noise after the first.
        if message == self._last_error:
            log.debug("%s (still)", message)
        else:
            log.warning("%s", message)
            self._last_error = message

        if self._on_error is not None:
            try:
                self._on_error(message)
            except Exception:
                log.debug("Audio error callback raised", exc_info=True)

    def snapshot(self) -> dict[str, object]:
        return {
            "running": self.is_running,
            "frames_captured": self.frames_captured,
            "errors": self.capture_errors,
            "dropped": self.dropped,
            "samples_per_frame": self.samples_per_frame,
            "sample_rate": self.sample_rate,
            "samples_per_frame_min": self.samples_per_frame_min,
            "samples_per_frame_max": self.samples_per_frame_max,
            "frame_gap_ms": round(self.frame_gap_ms, 2),
            "frame_gap_max_ms": round(self.frame_gap_max_ms, 2),
        }

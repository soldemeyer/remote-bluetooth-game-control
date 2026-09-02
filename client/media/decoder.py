"""Decoding: H.264 frames to something Qt can paint.

Runs on its own thread and publishes to a latest-wins slot rather than a queue.
The window paints whatever is newest at the moment it repaints; a frame that
arrived while the compositor was busy is simply skipped. Queueing them would
convert a momentary hiccup into permanent added latency, which is the failure
mode this whole design exists to avoid.

A publish also *notifies*, through ``set_frame_listener``. The window used to
poll the version counter on a 5 ms timer, which cost a decoded frame 0-5 ms of
pure waiting for no reason other than that this module must not touch Qt. It
still must not -- the listener is a plain callable, and it is the window's job
to turn that into something Qt can deliver on its own thread.

**Nothing is copied out of the decoded frame.** The published object holds a
memoryview over the converted frame's pixel plane plus a reference to the frame
that owns it, and the window wraps that view in a QImage. Each ``reformat()``
returns its own buffer -- verified, not assumed -- so a frame already handed
over is never written into.

That is a latency fix, not a tidiness one. The copy it replaces was
``bytes(plane)`` over 6.22 MB at 1080p, and **CPython holds the GIL for the
whole of it**. Measured against a 500 Hz canary at the input loop's own rate,
that single call put its p99 wake-up lateness at 4.87 ms -- more than two whole
periods of the loop this project cares most about. Preallocating a destination
does not help: the hold is the memcpy itself, not the allocation (measured at
4.66 ms into a preallocated bytearray). The only fix is not to copy.

**The frame is scaled here, to the size the window will draw it at.** That is a
latency decision, not a convenience. FFmpeg releases the GIL inside swscale;
``QPainter.drawImage`` does not release it while scaling. Measured against a
500 Hz canary at the input loop's own rate, painting 1080p into a 1280x720
window cost that loop 1.81 ms at p99, while painting it **1:1** cost 0.51 ms --
and 1:1 is what the window does once the frame already arrives at its size.
Scaling to a 1440p fullscreen window cost 4.52 ms.

So the pixels are resampled on this thread, where the GIL is free during the
work, and the paint becomes a straight blit. It costs nothing extra: the
conversion to RGB was already a swscale pass, and scaling in the same pass is
what swscale does anyway.

FFmpeg releases the GIL inside decode and scale, so this thread is far less
disruptive to the 500 Hz input loop than its CPU time suggests -- but it is not
free, and ``decode_ms`` in the receiver report is what to watch if input tail
latency ever regresses.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from common.timing import LatencyStats, now_ns

log = logging.getLogger(__name__)

#: Frames that decode to nothing before we assume the reference chain is gone
#: and ask for a keyframe. Two is enough to rule out a single damaged frame
#: while still recovering in well under a second at any real frame rate.
_STARVED_FRAMES_BEFORE_IDR = 2


@dataclass(slots=True)
class PresentFrame:
    """A decoded frame ready to paint. Nothing here is a copy."""

    #: RGB888 pixels, rows padded to `stride`. A memoryview over `owner`'s
    #: plane, so reading it costs nothing and building it costs nothing.
    pixels: object
    #: The av.VideoFrame that owns those bytes. **Load-bearing**: drop this and
    #: the view dangles, and the window paints freed memory. It is a field
    #: rather than a local precisely so the lifetime is written down.
    owner: object
    width: int
    height: int
    #: Row length in bytes. Not width*3: the scaler pads rows for alignment,
    #: and a QImage built without the real stride shears the picture
    #: diagonally -- which reads as a corrupt stream rather than a wrong
    #: constant.
    stride: int
    capture_ts: int        # source clock
    decoded_ns: int        # our clock, when decoding finished
    version: int


class VideoDecoder:
    """Decodes frames from a receiver and publishes the newest one."""

    def __init__(
        self,
        receiver: Any,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._receiver = receiver
        self._on_error = on_error

        #: Called with no arguments on the decode thread after every publish.
        #: Deliberately a bare callable rather than anything Qt: this module is
        #: imported on machines with no GUI at all, and `client/net/video.py`
        #: keeps the same rule about PyAV.
        self._listener: Callable[[], None] | None = None

        #: Target the frame is scaled to, in **physical** pixels, or None for
        #: the stream's own size. Set by whatever is drawing -- a plain tuple,
        #: rebound atomically, because the decode thread reads it once per
        #: frame and a viewport one frame stale is invisible.
        self._viewport: tuple[int, int] | None = None

        #: Our own scaler, never the one `frame.reformat()` caches on the
        #: frame. Same rule as `videoserver/preview.py`: that cache is shared
        #: state, and it is also rebuilt whenever the target size changes --
        #: which, during a window resize, is every frame.
        self._reformatter: Any = None

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: PresentFrame | None = None
        self._version = 0

        self.frames_decoded = 0
        self.decode_errors = 0
        self.recoveries = 0
        self.decode = LatencyStats()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="video-decode", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- consumer side -----------------------------------------------------

    @property
    def version(self) -> int:
        """Bumped per published frame, so a painter can skip when unchanged."""
        return self._version

    def latest(self) -> PresentFrame | None:
        with self._lock:
            return self._latest

    def set_viewport(self, width: int, height: int) -> None:
        """Ask for frames scaled to fit ``width`` x ``height`` physical pixels.

        The aspect ratio of the stream is preserved, so the result fits inside
        the viewport rather than filling it -- the same fit the window would
        have applied at paint time, done where the GIL is not held.

        Pass zero or less to go back to the stream's native size, which is what
        an unwatched stream uses.
        """
        if width <= 0 or height <= 0:
            self._viewport = None
            return
        self._viewport = (int(width), int(height))

    def _target_size(self, width: int, height: int) -> tuple[int, int]:
        """Fit the stream's size inside the viewport, preserving aspect."""
        viewport = self._viewport
        if viewport is None or width <= 0 or height <= 0:
            return width, height

        scale = min(viewport[0] / width, viewport[1] / height)
        # Even, because an odd width makes swscale pad the row and every
        # consumer then has to care about stride for no reason.
        target_w = max(2, (int(width * scale) // 2) * 2)
        target_h = max(2, (int(height * scale) // 2) * 2)
        return target_w, target_h

    def set_frame_listener(self, listener: Callable[[], None] | None) -> None:
        """Be told when a frame is published. ``None`` clears it.

        Cleared by the window on close, because the decoder outlives it -- the
        stream keeps running while nobody is watching, and calling into a
        window that has gone away is not something the decode thread should be
        able to do.
        """
        self._listener = listener

    def _notify(self) -> None:
        """Ring the listener. Never lets it take the decode thread down."""
        listener = self._listener
        if listener is None:
            return
        try:
            listener()
        except Exception:
            log.debug("Frame listener raised", exc_info=True)

    # -- the thread --------------------------------------------------------

    def _run(self) -> None:
        try:
            import av
        except ImportError as exc:
            self._report(f"Video playback needs PyAV: {exc}")
            return

        codec = av.CodecContext.create("h264", "r")
        # Threading in the decoder buys throughput at the cost of latency;
        # slice threading keeps the picture-level pipeline one frame deep.
        codec.thread_type = "SLICE"

        starved = 0
        while not self._stop.is_set():
            frame = self._receiver.get_frame(timeout=0.1)
            if frame is None:
                continue

            started = now_ns()
            produced = 0
            try:
                for packet in codec.parse(frame.data):
                    for picture in codec.decode(packet):
                        self._publish(picture, frame.capture_ts, started)
                        produced += 1
            except Exception as exc:  # noqa: BLE001
                self.decode_errors += 1
                log.debug("Decode error: %s", exc, exc_info=True)
                codec = self._fresh_codec(av)

            # A broken reference chain usually fails *silently*: frames arrive,
            # decode raises nothing, and no picture comes out. Watching only
            # for exceptions leaves the window frozen with nothing asking for a
            # way out, so treat a run of empty frames the same as an error.
            if produced:
                starved = 0
            else:
                starved += 1
                if starved >= _STARVED_FRAMES_BEFORE_IDR:
                    starved = 0
                    self.recoveries += 1
                    codec = self._fresh_codec(av)
                    try:
                        self._receiver.request_idr()
                    except Exception:
                        log.debug("Could not request a keyframe", exc_info=True)

    @staticmethod
    def _fresh_codec(av_module):
        """A clean decoder. Also resets the parser, which holds partial NALs."""
        codec = av_module.CodecContext.create("h264", "r")
        codec.thread_type = "SLICE"
        return codec

    def _publish(self, picture: Any, capture_ts: int, started_ns: int) -> None:
        try:
            if self._reformatter is None:
                from av.video.reformatter import VideoReformatter

                self._reformatter = VideoReformatter()
            target_w, target_h = self._target_size(picture.width, picture.height)
            rgb = self._reformatter.reformat(
                picture, width=target_w, height=target_h, format="rgb24"
            )
            plane = rgb.planes[0]
            frame = PresentFrame(
                # No copy. See the module docstring: `bytes(plane)` here held
                # the GIL for 6.22 MB at 1080p, which is two periods of the
                # 500 Hz input loop sharing this process.
                pixels=memoryview(plane),
                owner=rgb,
                width=rgb.width,
                height=rgb.height,
                stride=plane.line_size,
                capture_ts=capture_ts,
                decoded_ns=now_ns(),
                version=self._version + 1,
            )
        except Exception as exc:  # noqa: BLE001
            self.decode_errors += 1
            log.debug("Could not convert a decoded frame: %s", exc, exc_info=True)
            return

        self.decode.add((now_ns() - started_ns) / 1_000_000)
        self._receiver.decode_stats = self.decode
        self.frames_decoded += 1

        with self._lock:
            self._latest = frame
        # Published before the version is visible, so a painter that sees the
        # new version always finds the frame that goes with it.
        self._version = frame.version
        # ...and only then notified, for the same reason: the listener's first
        # act is to read the version.
        self._notify()

    def _report(self, message: str) -> None:
        log.error("%s", message)
        if self._on_error is not None:
            try:
                self._on_error(message)
            except Exception:
                log.debug("Decoder error callback raised", exc_info=True)

    def snapshot(self) -> dict[str, object]:
        return {
            "running": self.is_running,
            "frames_decoded": self.frames_decoded,
            "errors": self.decode_errors,
            "decode_ms": self.decode.snapshot(),
        }

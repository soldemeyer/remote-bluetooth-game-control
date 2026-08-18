"""Decoding: H.264 frames to something Qt can paint.

Runs on its own thread and publishes to a latest-wins slot rather than a queue.
The window paints whatever is newest at the moment it repaints; a frame that
arrived while the compositor was busy is simply skipped. Queueing them would
convert a momentary hiccup into permanent added latency, which is the failure
mode this whole design exists to avoid.

**Buffers are double-buffered, not reused in place.** The paint thread wraps the
published buffer in a QImage without copying, so writing the next frame into
that same memory would tear the picture being drawn. Two buffers alternating
costs a few megabytes and removes the whole class of problem.

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
    """A decoded frame ready to paint."""

    data: bytes            # RGB888, rows padded to `stride`
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
            rgb = picture.reformat(format="rgb24")
            plane = rgb.planes[0]
            data = bytes(plane)
            frame = PresentFrame(
                data=data,
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

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

from fractions import Fraction

from client.media.planner import (
    GUTTER_PX as _GUTTER_PX,
    TRANSITION_NS,
    compose as _compose_layout,
    eased as _eased,
    lerp_rect as _lerp_rect,
    union_rect as _union_rect,
)
from common.timing import LatencyStats, now_ns

log = logging.getLogger(__name__)

#: Frames that decode to nothing before we assume the reference chain is gone
#: and ask for a keyframe. Two is enough to rule out a single damaged frame
#: while still recovering in well under a second at any real frame rate.
_STARVED_FRAMES_BEFORE_IDR = 2

# `TRANSITION_NS`, `_eased`, `_lerp_rect`, `_union_rect` and `_GUTTER_PX` are
# imported from `client.media.planner`, which is where the geometry lives now
# so that the software path and the GPU path cannot drift apart. They are
# re-exported under their old private names because this module's callers --
# and `tests/test_client_zoom.py` -- have always reached for them here.

#: The most the intermediate frame may be enlarged beyond the viewport while
#: the camera is moving.
#:
#: During a move the decoder renders the *union* of the two views and the
#: window presents a travelling sub-rectangle of it. To land on the final view
#: at full sharpness that union has to be rendered bigger than the window --
#: twice over, for a quadrant. This caps how much bigger, because the cost is
#: quadratic in it and the alternative to a cap is a 4x4 union on some future
#: layout costing sixteen times the pixels.
MAX_TRANSITION_SCALE = 2.5


@dataclass(slots=True)
class RegionView:
    """One cropped piece of a frame, already scaled to the size it is drawn at.

    A player assigned two regions that do not touch gets two of these rather
    than one rectangle covering both -- that rectangle would include the
    players between them. See ``common/screen_regions.py``; the decision is
    made on the server and arrives as a list of crops.
    """

    pixels: object
    owner: object
    width: int
    height: int
    stride: int
    #: Where this piece sits inside the composed picture, in physical pixels.
    #: The composed picture is built to fit the viewport exactly, so the window
    #: blits each piece 1:1 at this offset and never scales anything.
    x: int
    y: int


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

    #: Cropped pieces, when the server has told this client it owns part of a
    #: split screen. **Empty is the ordinary case** and means "draw the whole
    #: frame" -- the fields above -- so a client that has never heard of
    #: regions, or one told it owns none, takes exactly the path it always did.
    views: tuple[RegionView, ...] = ()

    #: Size of the composed picture the views tile into, physical pixels.
    #: Meaningless when ``views`` is empty.
    composed_width: int = 0
    composed_height: int = 0

    #: While the camera is moving between two views: the sub-rectangle of
    #: ``pixels`` to present, ``(x, y, w, h)`` in pixels, scaled to fill the
    #: window. ``None`` the rest of the time, which is every frame that is not
    #: inside a 400 ms transition -- so the ordinary path is untouched.
    zoom: tuple[int, int, int, int] | None = None


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

        #: The GPU renderer, or None for the software path.
        #:
        #: **None is the default and the default is the whole point.** When it
        #: is None nothing in this module behaves differently from how it did
        #: before GPU enhancement existed: `_publish` branches on it before
        #: anything else and takes the code it always took. There is no
        #: pass-through object, because a pass-through would be a change to
        #: the path the requirement says must not change.
        #:
        #: Rebound atomically from the GUI thread, like `_crops` -- read once
        #: per frame, and a frame late is invisible.
        self._upscale = None

        #: Hardware decode device (`d3d11va`, `vulkan`, ...) or "" for
        #: software. Changing it is the one setting that rebuilds the codec,
        #: so it is applied in the decode loop rather than taken per frame.
        self._hw_device = ""
        self._hw_wanted = ""

        #: The overlay the window wants composited into the presented frame.
        #: Only used on the GPU path -- on the software path the window draws
        #: its own, because it still owns the pixels there.
        self._overlay = None

        #: What the renderer did with the last frame, for the overlay to show.
        self.last_path = ""
        self.last_gpu_ms = -1.0
        self.last_output = (0, 0)

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

        #: Crops this client owns, each ``(x, y, w, h)`` as a fraction of the
        #: frame. Empty means the whole picture, which is the default and the
        #: state everything fails back to. Rebound atomically and read once per
        #: frame, the same discipline as `_viewport`.
        self._crops: tuple[tuple[float, float, float, float], ...] = ()

        #: One FFmpeg filter graph per crop, cached. Keyed by the crop, the
        #: target size *and* the stream's own size and format, so a resolution
        #: change cannot leave a graph configured for the old one.
        self._graphs: dict[tuple, Any] = {}

        #: ``(started_ns, from_rect, to_rect)`` while the camera is moving.
        #:
        #: Driven off the wall clock and evaluated per decoded frame rather
        #: than by a timer: the animation is only visible on frames that are
        #: actually presented, so there is nothing for a timer to do between
        #: them, and a stream that stalls mid-move simply arrives having
        #: finished it.
        self._transition: tuple[int, tuple, tuple] | None = None

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

    def set_regions(self, crops: object) -> None:
        """Show only these parts of the picture. Empty means all of it.

        Each crop is ``(x, y, w, h)`` as a fraction of the frame -- normalised,
        which is what the server sends, so a resolution change mid-stream does
        not invalidate them and this side needs no idea what the source is
        running at. Dicts with ``x``/``y``/``w``/``h`` are accepted too, since
        that is the shape they arrive in over the control channel.

        **Fails open.** Anything unreadable clears the crop rather than
        guessing: showing a player the whole game is what they had before this
        feature existed, while a wrong crop shows them a slice of somebody
        else's screen and looks perfectly correct while doing it.
        """
        wanted: list[tuple[float, float, float, float]] = []
        try:
            for crop in crops or ():
                if isinstance(crop, dict):
                    values = [float(crop[k]) for k in ("x", "y", "w", "h")]
                else:
                    values = [float(v) for v in crop]
                x, y, w, h = values
                if w <= 0.0 or h <= 0.0:
                    continue
                # Clamped rather than rejected: a rounding error at the far
                # edge should not cost the player their picture.
                x = min(max(x, 0.0), 1.0)
                y = min(max(y, 0.0), 1.0)
                wanted.append((x, y, min(w, 1.0 - x), min(h, 1.0 - y)))
        except (TypeError, ValueError, KeyError, IndexError):
            log.debug("Ignoring malformed screen regions: %r", crops)
            wanted = []

        fresh = tuple(wanted)
        if fresh == self._crops:
            return

        self._begin_transition(self._crops, fresh)
        self._crops = fresh
        # The graphs are configured for the old crops and cannot be reused.
        # Cleared rather than left to age out, because each holds buffers.
        self._graphs = {}
        log.info(
            "Video now shows %s",
            f"{len(fresh)} region(s) of the screen" if fresh else "the whole screen",
        )

    def _begin_transition(self, old: tuple, new: tuple) -> None:
        """Start the camera moving from one view to the other.

        Only between **single** views, counting "the whole picture" as one.
        A client holding two regions that do not touch is showing two separate
        pieces, and there is no single camera position that describes where it
        is looking -- so those still cut. Every layout change for a player with
        one controller, which is the ordinary case, is a single-view move.

        Starting from wherever the camera currently *is* rather than from the
        nominal old view, so a change arriving mid-move continues smoothly
        instead of jumping back.
        """
        if len(old) > 1 or len(new) > 1:
            self._transition = None
            return

        whole = (0.0, 0.0, 1.0, 1.0)
        start = self._current_rect() or (old[0] if old else whole)
        end = new[0] if new else whole
        if start == end:
            self._transition = None
            return
        self._transition = (now_ns(), start, end)

    def _current_rect(self) -> tuple | None:
        """Where the camera is right now, if it is already moving."""
        if self._transition is None:
            return None
        started, start, end = self._transition
        return _lerp_rect(start, end, _eased(now_ns() - started))

    def _transition_frame(self, picture: Any, capture_ts: int) -> PresentFrame:
        """One frame of the camera move.

        The decoder renders the **union** of the two views and the window
        presents a travelling sub-rectangle of it. One filter graph for the
        whole move rather than one per frame, which matters because a graph is
        cached by its crop and an interpolating crop would build -- and throw
        away -- a new one every frame.
        """
        started, start, end = self._transition        # type: ignore[misc]
        progress = _eased(now_ns() - started)
        current = _lerp_rect(start, end, progress)
        union = _union_rect(start, end)

        viewport = self._viewport or (picture.width, picture.height)
        # The scale the *final* view will be drawn at, so the move lands at
        # exactly the sharpness the settled picture has and there is no pop.
        final_w = max(picture.width * end[2], 1.0)
        final_h = max(picture.height * end[3], 1.0)
        scale = min(viewport[0] / final_w, viewport[1] / final_h)
        scale = min(scale, MAX_TRANSITION_SCALE * min(
            viewport[0] / max(picture.width * union[2], 1.0),
            viewport[1] / max(picture.height * union[3], 1.0),
        ) or scale)

        width = max(2, (int(picture.width * union[2] * scale) // 2) * 2)
        height = max(2, (int(picture.height * union[3] * scale) // 2) * 2)

        import av

        graph = self._graph_for(av, picture, union, width, height)
        graph.push(picture)
        out = graph.pull()
        plane = out.planes[0]

        # Where the travelling view sits inside that image.
        span_x = union[2] or 1.0
        span_y = union[3] or 1.0
        zoom = (
            max(0, int((current[0] - union[0]) / span_x * width)),
            max(0, int((current[1] - union[1]) / span_y * height)),
            max(2, int(current[2] / span_x * width)),
            max(2, int(current[3] / span_y * height)),
        )

        if progress >= 1.0:
            self._transition = None

        return PresentFrame(
            pixels=memoryview(plane),
            owner=out,
            width=out.width,
            height=out.height,
            stride=plane.line_size,
            capture_ts=capture_ts,
            decoded_ns=now_ns(),
            version=self._version + 1,
            zoom=zoom,
        )

    def _compose(self, frame_w: int, frame_h: int):
        """Where each crop goes, and how big the composed picture is.

        Delegates to ``planner.compose``, which the GPU path uses too. One
        implementation rather than two agreeing implementations: the property
        that matters is that switching render mode leaves every piece exactly
        where it was, and sharing the code is how that is guaranteed rather
        than merely tested.
        """
        return _compose_layout(self._crops, frame_w, frame_h, self._viewport)

    def _graph_for(self, av_module, picture, crop, width: int, height: int):
        """A crop-then-scale filter graph, cached.

        Crop *before* scale, and that ordering is the whole reason this is a
        filter graph rather than another reformat. FFmpeg's crop is pointer
        arithmetic, so the scaler that follows it touches only the pixels this
        player is entitled to see. Measured on 1080p, one quadrant into a
        1280x720 window: **0.60 ms**, against 0.78 ms to scale the whole frame
        with no crop at all, and 1.66 ms to scale the frame up until the
        quadrant fills the viewport and then slice it out. Cropping is cheaper
        than not cropping.

        Each graph also produces its own correctly-strided output, so nothing
        downstream slices a buffer -- which is where QImage's row-alignment
        trap would otherwise be waiting.
        """
        key = (crop, width, height, picture.width, picture.height, picture.format.name)
        graph = self._graphs.get(key)
        if graph is not None:
            return graph

        # Even offsets and sizes: the decoded frame is 4:2:0, so an odd crop
        # has no chroma sample to start from and FFmpeg refuses it.
        source_x = int(picture.width * crop[0]) & ~1
        source_y = int(picture.height * crop[1]) & ~1
        source_w = max(2, (int(picture.width * crop[2]) // 2) * 2)
        source_h = max(2, (int(picture.height * crop[3]) // 2) * 2)
        # Trimmed rather than left to overrun: a crop that rounds past the edge
        # would make the graph refuse to configure, which costs the picture.
        source_w = min(source_w, picture.width - source_x)
        source_h = min(source_h, picture.height - source_y)

        graph = av_module.filter.Graph()
        buffer = graph.add_buffer(
            width=picture.width,
            height=picture.height,
            format=picture.format.name,
            # Supplied explicitly. Without it PyAV guesses and warns, which at
            # 60 fps is a log line per frame.
            time_base=Fraction(1, 1000),
        )
        cropper = graph.add("crop", f"{source_w}:{source_h}:{source_x}:{source_y}")
        scaler = graph.add("scale", f"{width}:{height}")
        formatter = graph.add("format", "rgb24")
        sink = graph.add("buffersink")
        buffer.link_to(cropper)
        cropper.link_to(scaler)
        scaler.link_to(formatter)
        formatter.link_to(sink)
        graph.configure()

        # At most four crops are ever live, but a resize or a resolution change
        # makes new keys, so the old ones are dropped rather than accumulating
        # buffers for the life of the process.
        if len(self._graphs) > 8:
            self._graphs.clear()
        self._graphs[key] = graph
        return graph

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

        codec = self._build_codec(av)

        starved = 0
        while not self._stop.is_set():
            # The one setting that cannot be applied per frame: a decoder is
            # built for one device, so changing it means a new one -- and a new
            # decoder has no reference chain, hence the keyframe request.
            if self._hw_wanted != self._hw_device:
                self._hw_device = self._hw_wanted
                codec = self._build_codec(av)
                try:
                    self._receiver.request_idr()
                except Exception:
                    log.debug("Could not request a keyframe", exc_info=True)

            frame = self._receiver.get_frame(timeout=0.1)
            if frame is None:
                # Nothing arrived. The window may still have resized or its
                # overlay changed, and on the GPU path only this thread can
                # present -- so keep the picture current rather than leaving
                # it stale until the next frame.
                upscale = self._upscale
                if upscale is not None:
                    upscale.repaint()
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

    def _fresh_codec(self, av_module):
        """A clean decoder. Also resets the parser, which holds partial NALs.

        Goes through `_build_codec` so an error recovery keeps whatever
        decoder the player asked for. It used to be a static method building a
        software one unconditionally, which would have turned hardware
        decoding off at the first damaged frame and never turned it back on.
        """
        return self._build_codec(av_module)

    def _build_codec(self, av_module):
        """The decoder the current settings ask for.

        Falls back to software with one log line if the hardware one cannot be
        built. Never raises: there is no useful way to recover from "no
        decoder at all", so this always returns one.
        """
        device = self._hw_device
        if device:
            from client.media import hwdecode

            codec = hwdecode.make_codec(device)
            if codec is not None:
                log.info("Decoding H.264 on the GPU (%s)", device)
                return codec
            log.info("Hardware decoding (%s) is unavailable; using software", device)

        codec = av_module.CodecContext.create("h264", "r")
        # Threading in the decoder buys throughput at the cost of latency;
        # slice threading keeps the picture-level pipeline one frame deep.
        codec.thread_type = "SLICE"
        return codec

    def _publish(self, picture: Any, capture_ts: int, started_ns: int) -> None:
        # Read once, before anything else. Everything below this line is the
        # software path exactly as it was; the GPU path never reaches it.
        upscale = self._upscale
        if upscale is not None:
            self._gpu_frame(picture, capture_ts, started_ns, upscale)
            return

        try:
            # Read once. A crop list swapped mid-frame would otherwise compose
            # a layout from one set and fill it from another.
            crops = self._crops
            # Retired *before* the frame is built, not after: at the end of a
            # move the travelling rectangle is exactly the final view, so
            # rendering it through the union costs a large scale-up for a
            # picture the cheap cropped path produces identically.
            if self._transition is not None and _eased(
                now_ns() - self._transition[0]
            ) >= 1.0:
                self._transition = None
            if self._transition is not None:
                frame = self._transition_frame(picture, capture_ts)
            elif crops:
                frame = self._crop_frame(picture, crops, capture_ts)
            else:
                frame = self._whole_frame(picture, capture_ts)
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

    def _whole_frame(self, picture: Any, capture_ts: int) -> PresentFrame:
        """The uncropped path, unchanged. Still one reformat and no copy."""
        if self._reformatter is None:
            from av.video.reformatter import VideoReformatter

            self._reformatter = VideoReformatter()
        target_w, target_h = self._target_size(picture.width, picture.height)
        rgb = self._reformatter.reformat(
            picture, width=target_w, height=target_h, format="rgb24"
        )
        plane = rgb.planes[0]
        return PresentFrame(
            # No copy. See the module docstring: `bytes(plane)` here held the
            # GIL for 6.22 MB at 1080p, which is two periods of the 500 Hz
            # input loop sharing this process.
            pixels=memoryview(plane),
            owner=rgb,
            width=rgb.width,
            height=rgb.height,
            stride=plane.line_size,
            capture_ts=capture_ts,
            decoded_ns=now_ns(),
            version=self._version + 1,
        )

    def _crop_frame(self, picture: Any, crops, capture_ts: int) -> PresentFrame:
        """One view per crop, each already the size it will be drawn at.

        No copy here either: every graph produces its own output frame, and the
        view holds a reference to it.
        """
        import av

        placed, composed_w, composed_h = self._compose(picture.width, picture.height)

        views = []
        for crop, x, y, width, height in placed:
            graph = self._graph_for(av, picture, crop, width, height)
            graph.push(picture)
            out = graph.pull()
            plane = out.planes[0]
            views.append(
                RegionView(
                    pixels=memoryview(plane),
                    owner=out,
                    width=out.width,
                    height=out.height,
                    stride=plane.line_size,
                    x=x,
                    y=y,
                )
            )

        first = views[0]
        return PresentFrame(
            # The first view fills the single-picture fields as well, so
            # anything reading a frame without knowing about regions -- a test,
            # a screenshot, the latency overlay -- still finds a picture rather
            # than nothing. It is a piece of the screen, which is exactly what
            # this client is now showing.
            pixels=first.pixels,
            owner=first.owner,
            width=first.width,
            height=first.height,
            stride=first.stride,
            capture_ts=capture_ts,
            decoded_ns=now_ns(),
            version=self._version + 1,
            views=tuple(views),
            composed_width=composed_w,
            composed_height=composed_h,
        )

    # -- the GPU path ------------------------------------------------------
    #
    # Everything below is reached only when an upscaler is attached. With none
    # attached `_publish` returns before any of it, which is what keeps Off
    # byte-for-byte the path it always was.

    def set_upscaler(self, upscaler) -> None:
        """Attach a GPU renderer, or None to go back to software.

        A plain attribute rebind, read once per frame -- the same discipline
        `set_regions` and `set_viewport` use. No lock: the decode thread sees
        either the old value or the new one, and a frame late is invisible.

        The filter-graph cache is cleared on the way back to software, because
        its graphs are keyed for that path's own output format and size and a
        stale one would produce a correctly-shaped picture of the wrong thing.
        """
        if upscaler is self._upscale:
            return
        self._upscale = upscaler
        if upscaler is None:
            self._graphs = {}
            self.last_path = ""
            self.last_gpu_ms = -1.0

    def set_hw_decode(self, device: str) -> None:
        """Ask for a hardware decoder, or "" for software.

        Applied by the decode loop rather than here: a decoder is built for one
        device, so this is the one setting that cannot be taken per frame.
        """
        self._hw_wanted = device or ""

    def set_overlay(self, overlay) -> None:
        """What to composite over the picture, on the GPU path.

        A native child window draws above every Qt sibling, so the window's own
        overlay would be hidden the moment the GPU path turns on. It hands the
        pixels here instead. None means draw nothing.
        """
        self._overlay = overlay

    def _plane_addresses(self, picture, upload):
        """Plane addresses offset to the upload rectangle, and their strides.

        The crop, for the software path, and it costs nothing: advancing a
        pointer is how FFmpeg's own crop filter does it. Only the bytes inside
        the rectangle are ever read.

        Returns None for anything that is not yuv420p -- 10-bit, 4:2:2, or a
        future HEVC path. Uploading one of those as three 8-bit planes gives a
        green skewed picture with no error anywhere, so those frames take the
        software path instead.
        """
        if picture.format.name != "yuv420p":
            return None
        x0, y0, _, _ = upload
        try:
            planes = picture.planes
            luma, chroma_u, chroma_v = planes[0], planes[1], planes[2]
            addresses = (
                luma.buffer_ptr + y0 * luma.line_size + x0,
                chroma_u.buffer_ptr + (y0 // 2) * chroma_u.line_size + (x0 // 2),
                chroma_v.buffer_ptr + (y0 // 2) * chroma_v.line_size + (x0 // 2),
            )
            strides = (luma.line_size, chroma_u.line_size, chroma_v.line_size)
        except (AttributeError, IndexError, TypeError):
            return None
        return addresses, strides

    def _gpu_frame(self, picture, capture_ts: int, started_ns: int, upscale) -> None:
        """One frame, drawn and presented on the GPU.

        Nothing is published to `_latest` and nothing is handed to the window:
        on this path the picture goes straight to the screen from here, which
        is why presentation stops costing the GUI thread anything.
        """
        from client.media import hwdecode
        from client.media.planner import plan_blits, rebase

        crops = self._crops
        transition = self._transition

        upload, blits, composed_w, composed_h, moving = plan_blits(
            crops, transition, picture.width, picture.height,
            self._viewport, now_ns(),
        )
        if not moving:
            self._transition = None

        colorspace = int(getattr(picture, "colorspace", 1) or 1)
        color_range = int(getattr(picture, "color_range", 1) or 1)

        handles = hwdecode.gpu_handles(picture)
        if handles is not None:
            # The decoder owns a texture holding the whole frame, so the
            # rectangles are expressed against all of it. Nothing is uploaded
            # and nothing touches system memory.
            texture, slice_index = handles
            result = upscale.submit(
                blits=rebase(blits, upload, picture.width, picture.height),
                composed=(composed_w, composed_h),
                src_size=(picture.width, picture.height),
                colorspace=colorspace,
                color_range=color_range,
                texture=texture,
                slice_index=slice_index,
                overlay=self._overlay,
            )
        else:
            addressed = self._plane_addresses(picture, upload)
            if addressed is None:
                log.debug("Falling back to software for a %s frame",
                          picture.format.name)
                self._software_frame(picture, capture_ts, started_ns)
                return
            addresses, strides = addressed
            result = upscale.submit(
                blits=blits,
                composed=(composed_w, composed_h),
                src_size=(upload[2], upload[3]),
                colorspace=colorspace,
                color_range=color_range,
                planes=addresses,
                strides=strides,
                overlay=self._overlay,
            )

        if not result.ok:
            self.decode_errors += 1
            if result.fatal:
                # The renderer is done. Detach it and carry on in software: a
                # frozen picture with healthy counters is a far worse outcome
                # than losing an enhancement nobody can see is missing.
                self._upscale = None
                self._graphs = {}
                self._report("GPU video enhancement stopped: " + result.reason)
            else:
                log.debug("A frame was not drawn: %s", result.reason)
            return

        self.decode.add((now_ns() - started_ns) / 1_000_000)
        self._receiver.decode_stats = self.decode
        self.frames_decoded += 1
        self._version += 1

        self.last_path = result.path_name
        if result.gpu_ms >= 0.0:
            self.last_gpu_ms = result.gpu_ms
        self.last_output = (result.output_width, result.output_height)

        # **The end-to-end figure has to be taken here.** On the software path
        # the window stamps it at the end of paintEvent, because that is where
        # the picture actually reaches the screen. Here paintEvent draws no
        # video at all, so a stamp left there would freeze -- and the audio
        # governor synchronises against this statistic, so it would quietly
        # run on a number that never changes.
        if not result.skipped:
            self._note_presented(capture_ts)

    def _note_presented(self, capture_ts: int) -> None:
        """Capture -> presented, on the source's clock offset to ours."""
        receiver = self._receiver
        try:
            if not getattr(receiver, "clock_locked", False):
                return
            local_capture = capture_ts + receiver.clock_offset_ns
            latency_ms = (now_ns() - local_capture) / 1_000_000
            if latency_ms > 0:
                receiver.present_stats.add(latency_ms)
        except Exception:  # noqa: BLE001
            log.debug("Could not record presentation latency", exc_info=True)

    def _software_frame(self, picture, capture_ts: int, started_ns: int) -> None:
        """The software path, for a frame the GPU path cannot take.

        Exactly what `_publish` does with no upscaler attached, split out so
        the fallback and the ordinary path cannot drift.
        """
        try:
            crops = self._crops
            if self._transition is not None and _eased(
                now_ns() - self._transition[0]
            ) >= 1.0:
                self._transition = None
            if self._transition is not None:
                frame = self._transition_frame(picture, capture_ts)
            elif crops:
                frame = self._crop_frame(picture, crops, capture_ts)
            else:
                frame = self._whole_frame(picture, capture_ts)
        except Exception as exc:  # noqa: BLE001
            self.decode_errors += 1
            log.debug("Could not convert a decoded frame: %s", exc, exc_info=True)
            return

        self.decode.add((now_ns() - started_ns) / 1_000_000)
        self._receiver.decode_stats = self.decode
        self.frames_decoded += 1

        with self._lock:
            self._latest = frame
        self._version = frame.version
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

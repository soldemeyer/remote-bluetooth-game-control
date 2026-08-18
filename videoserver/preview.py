"""Preview: a small JPEG for the Bluetooth server's web GUI.

The web preview deliberately does not touch the real stream. Handing a browser
H.264 would mean either a container format (which buffers) or a JavaScript
decoder (which is heavy and fragile), and either way the operator's browser
would be pulling on the same path the players depend on.

A 320-wide JPEG a few times a second costs almost nothing, needs no client-side
anything, and answers the only question the preview exists to answer: is the
capture card showing the right thing?
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

#: Fallback preview width; height follows the source aspect ratio. The real
#: value comes from `VideoSettings.preview_width`, which the operator sets.
PREVIEW_WIDTH = 640

#: Refuse to send anything larger. A 640-wide JPEG is normally 30-60 kB and a
#: 1280-wide one 100-200 kB; the cap is protection against a pathological
#: frame, not a target. Must not exceed `server.video.MAX_PREVIEW_BYTES`, or
#: the reassembler would drop what we consider acceptable to send.
MAX_PREVIEW_BYTES = 1024 * 1024


class PreviewEncoder:
    """Encodes single frames to JPEG. Not thread-safe; used from one thread."""

    def __init__(self, width: int = PREVIEW_WIDTH) -> None:
        self.width = width
        self._ctx: Any = None
        self._size: tuple[int, int] | None = None
        self._pts = 0
        self.frames_encoded = 0
        self.errors = 0

        # Our own scaler, never the one `frame.reformat()` caches on the frame.
        #
        # Capture publishes ONE CapturedFrame to both `latest` and the encoder's
        # queue, so the same object is reformatted by the encoder thread, by the
        # GUI's preview and by the responder's preview. `frame.reformat()` runs
        # them all through a single reformatter cached on that frame, and two
        # threads inside it do not raise -- one **wedges and never returns**.
        # That is a frozen preview, or a video server window that Windows
        # offers to close. Owning the reformatter removes the sharing outright.
        self._reformatter: Any = None

    def encode(self, frame: Any) -> bytes | None:
        """Return JPEG bytes for ``frame``, or None if it could not be encoded."""
        try:
            target = self._target_size(frame)
            ctx = self._context(target)
            scaled = self._scaler().reformat(
                frame, width=target[0], height=target[1], format="yuvj420p"
            )

            # Both, and always together. A timestamp is meaningless without the
            # base it is counted in, and a reformatted frame inherits the
            # *capture's* base -- a webcam's is 1/10000000. Setting a small
            # counter as pts against that rescales every frame to 0 in the
            # encoder's own 1/1000 base, so from the second frame onward
            # avcodec_send_frame rejects them as non-monotonic (EINVAL) and the
            # preview freezes on its first picture.
            #
            # It only shows up with a real capture device: the lavfi test
            # pattern has a coarse time base, so the same counter rescales to
            # distinct values and everything looks fine.
            self._pts += 1
            scaled.pts = self._pts
            scaled.time_base = ctx.time_base

            packets = ctx.encode(scaled)
            data = b"".join(bytes(p) for p in packets)
            if not data:
                return None
            if len(data) > MAX_PREVIEW_BYTES:
                log.debug("Preview frame of %d bytes discarded (over cap)", len(data))
                return None
            self.frames_encoded += 1
            return data
        except Exception:
            self.errors += 1
            log.debug("Preview encode failed", exc_info=True)
            return None

    def _scaler(self) -> Any:
        if self._reformatter is None:
            from av.video.reformatter import VideoReformatter

            self._reformatter = VideoReformatter()
        return self._reformatter

    def _target_size(self, frame: Any) -> tuple[int, int]:
        width = min(self.width, frame.width or self.width)
        if not frame.width or not frame.height:
            return width, width * 9 // 16
        height = max(int(round(width * frame.height / frame.width)), 2)
        # 4:2:0 again: both dimensions must be even.
        return width & ~1, height & ~1

    def _context(self, size: tuple[int, int]) -> Any:
        if self._ctx is not None and self._size == size:
            return self._ctx

        import av
        from fractions import Fraction

        ctx = av.CodecContext.create("mjpeg", "w")
        ctx.width, ctx.height = size
        ctx.pix_fmt = "yuvj420p"
        ctx.time_base = Fraction(1, 1000)
        # Mid-range quality: the preview is a sanity check, not a monitor feed.
        ctx.options = {"q:v": "6"}
        ctx.open()

        self._ctx = ctx
        self._size = size
        return ctx

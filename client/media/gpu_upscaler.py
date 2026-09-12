"""The GPU backend, as the decoder sees it.

Everything ctypes lives here and in ``videofx.py``; the decoder passes plain
Python values and gets a small result back. Nothing above this line knows what
a structure pointer is.

**Every call is synchronous and copies.** When ``submit`` returns, the native
library holds no pointer into the decoded frame -- there is no render thread,
no queue and no handshake, so a use-after-free is not constructible rather than
merely absent. That matters more than it sounds: in software mode the planes
handed over come straight out of FFmpeg's buffer pool and may still be a
reference frame for pictures yet to be decoded. Reading them inside the call is
fine; holding them would pin the pool and stall the decoder.

**Nothing here raises at the caller.** Every failure comes back as a result
with ``ok`` false and a reason, because the caller's answer to all of them is
the same -- drop to the software path and keep the stream running. An exception
crossing the decode thread would take the picture down instead, which is
strictly worse than losing an enhancement nobody can see is missing.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from dataclasses import dataclass

from client.media import videofx

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """What happened to one frame."""

    ok: bool = False
    #: What the renderer actually did, which is not always what was asked --
    #: a piece that is not being enlarged gets no super-resolution. See
    #: `videofx.PATH_NAMES`.
    path: int = videofx.PATH_NONE
    #: Enhancement time on the GPU, or < 0 when the query is not back yet.
    #: Read a frame late and never waited on.
    gpu_ms: float = -1.0
    output_width: int = 0
    output_height: int = 0
    #: The window was hidden, so nothing was drawn. Not a fault.
    skipped: bool = False
    #: True when the renderer cannot continue and the caller must fall back.
    fatal: bool = False
    reason: str = ""

    @property
    def path_name(self) -> str:
        return videofx.PATH_NAMES.get(self.path, "?")


class GPUUpscaler:
    """One native renderer, bound to one window.

    Created when the player selects a GPU mode and destroyed when they go back
    to Off. Switching *between* GPU modes does not recreate it -- see
    :meth:`set_mode`.
    """

    def __init__(self) -> None:
        #: Serialises every call into the library.
        #:
        #: **Two threads reach this object, and a Direct3D 11 immediate
        #: context tolerates exactly one.** The decode thread submits frames;
        #: the GUI thread changes mode, changes sharpness, and -- the
        #: dangerous one -- destroys the renderer when the player switches
        #: back to Off. `rbgc_destroy` tears down the swap chain and flushes
        #: the context, and doing that underneath a submit in flight is how
        #: selecting a mode froze the whole program.
        #:
        #: It also guards the handle's lifetime, which a mutex inside the C++
        #: could not: the object must not be freed while a call is inside it.
        #:
        #: Uncontended almost always -- a submit is about 0.6 ms. The
        #: exception is the *first* submit on a new device, measured at
        #: **217 ms**, and it is worth knowing what that is: creating the
        #: Direct3D device, the swap chain and the shaders, which happens
        #: whichever mode is chosen first. Measured on the same machine, with
        #: the device already up, a *second* mode costs 4.4 ms (FSR) or 29 ms
        #: (RTX VSR, which builds a video processor). So it is not the
        #: super-resolution model loading, and choosing a cheaper mode does
        #: not avoid it.
        #:
        #: A mode change landing in that window waits for it -- a hitch rather
        #: than a hang, and the honest cost of not corrupting the context.
        self._lock = threading.Lock()
        self._handle: ctypes.c_void_p | None = None
        self._lib = None
        self._mode = videofx.MODE_LANCZOS
        #: Reused across frames. Rebuilding these per frame would allocate on
        #: the decode thread for no reason; the contents are overwritten.
        self._frame = videofx.CFrame()
        self._frame.struct_size = ctypes.sizeof(videofx.CFrame)
        self._result = videofx.CResult()
        self._result.struct_size = ctypes.sizeof(videofx.CResult)
        self._blits = (videofx.CBlit * 8)()
        #: Keeps the overlay image alive while native reads it. QImage frees
        #: its buffer when the last reference goes, and native has no
        #: reference of its own -- see the module docstring.
        self._overlay_ref: object | None = None

    # -- lifetime ------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    def initialize(self, native_window: int, mode: str, sharpness: int = 50,
                   backdrop: int = 0x000000) -> tuple[bool, str]:
        """Create the renderer. Returns ``(ok, reason)`` and never raises."""
        lib = videofx.library()
        if lib is None:
            return False, videofx.load_error()
        if not native_window:
            return False, "the video surface has no window yet"

        number = videofx.MODE_FOR_SETTING.get(mode)
        if number is None:
            return False, f"unknown enhancement mode {mode!r}"

        handle = ctypes.c_void_p()
        try:
            with self._lock:
                status = lib.rbgc_create(
                    ctypes.c_void_p(int(native_window)), number, ctypes.byref(handle))
        except Exception as exc:  # noqa: BLE001
            return False, f"the enhancement library failed: {exc}"

        if status != videofx.OK:
            return False, videofx.STATUS_NAMES.get(status, f"error {status}")

        self._lib = lib
        self._handle = handle
        self._mode = number
        self.set_sharpness(sharpness)
        self.set_backdrop(backdrop)
        return True, ""

    def shutdown(self) -> None:
        """Release everything. Safe to call twice, and from any thread."""
        # Held across the destroy, so a submit already inside the library
        # finishes first. Without it the context is torn down underneath it.
        with self._lock:
            handle, self._handle = self._handle, None
            lib, self._lib = self._lib, None
            self._overlay_ref = None
            if handle is None or lib is None:
                return
            try:
                lib.rbgc_destroy(handle)
            except Exception:  # noqa: BLE001
                log.debug("Releasing the GPU renderer raised", exc_info=True)

    # -- settings ------------------------------------------------------------

    def set_mode(self, mode: str) -> bool:
        """Switch between GPU modes without recreating anything.

        The clean boundary is between submits, on the decode thread, which is
        where this is called from: nothing is in flight, and the renderer
        rebuilds only the pieces that differ.
        """
        if self._handle is None or self._lib is None:
            return False
        number = videofx.MODE_FOR_SETTING.get(mode)
        if number is None or number == self._mode:
            return number == self._mode
        with self._lock:
            if self._handle is None or self._lib is None:
                return False
            if self._lib.rbgc_set_mode(self._handle, number) != videofx.OK:
                return False
        self._mode = number
        return True

    def set_sharpness(self, percent: int) -> None:
        if self._handle is None or self._lib is None:
            return
        from client.media.upscale import sharpness_to_attenuation

        with self._lock:
            if self._handle is None or self._lib is None:
                return
            self._lib.rbgc_set_sharpness(
                self._handle, ctypes.c_float(sharpness_to_attenuation(percent)))

    def set_backdrop(self, rgb: int) -> None:
        with self._lock:
            if self._handle is None or self._lib is None:
                return
            self._lib.rbgc_set_backdrop(self._handle, ctypes.c_uint32(rgb & 0xFFFFFF))

    # -- the frame path ------------------------------------------------------

    def submit(
        self,
        *,
        blits,
        composed: tuple[int, int],
        src_size: tuple[int, int],
        colorspace: int = 1,
        color_range: int = 1,
        planes: tuple[int, int, int] | None = None,
        strides: tuple[int, int, int] | None = None,
        texture: int = 0,
        slice_index: int = 0,
        overlay=None,
    ) -> SubmitResult:
        """Draw one frame. Synchronous; see the module docstring.

        ``planes`` are raw addresses already offset to the uploaded
        rectangle's origin, or ``texture`` is a decoder-owned GPU texture.
        One or the other, never both.
        """
        if self._handle is None or self._lib is None:
            return SubmitResult(reason="the renderer is not open", fatal=True)
        if not blits:
            return SubmitResult(reason="nothing to draw")

        frame = self._frame
        count = min(len(blits), len(self._blits))
        for index in range(count):
            source, destination = blits[index].src, blits[index].dst
            entry = self._blits[index]
            entry.src[0], entry.src[1], entry.src[2], entry.src[3] = source
            entry.dst[0], entry.dst[1], entry.dst[2], entry.dst[3] = destination

        if planes is not None:
            frame.plane[0], frame.plane[1], frame.plane[2] = planes
            frame.stride[0], frame.stride[1], frame.stride[2] = strides or (0, 0, 0)
            frame.hw_texture = None
            frame.hw_slice = 0
        else:
            frame.plane[0] = frame.plane[1] = frame.plane[2] = None
            frame.stride[0] = frame.stride[1] = frame.stride[2] = 0
            frame.hw_texture = ctypes.c_void_p(texture)
            frame.hw_slice = slice_index

        frame.src_width, frame.src_height = src_size
        frame.colorspace = colorspace
        frame.color_range = color_range
        frame.composed_width, frame.composed_height = composed
        frame.blits = ctypes.cast(self._blits, ctypes.POINTER(videofx.CBlit))
        frame.blit_count = count

        if overlay is not None:
            # Held for the duration of the call, and no longer. Native copies
            # it into a texture and keeps nothing.
            self._overlay_ref = overlay.owner
            frame.overlay = overlay.address
            frame.overlay_stride = overlay.stride
            frame.overlay_x = overlay.x
            frame.overlay_y = overlay.y
            frame.overlay_width = overlay.width
            frame.overlay_height = overlay.height
            frame.overlay_version = overlay.version
        else:
            self._overlay_ref = None
            frame.overlay = None
            frame.overlay_width = 0
            frame.overlay_height = 0

        detail = ""
        try:
            with self._lock:
                if self._handle is None or self._lib is None:
                    return SubmitResult(reason="the renderer closed", fatal=True)
                status = self._lib.rbgc_submit(
                    self._handle, ctypes.byref(frame), ctypes.byref(self._result))
                if status != videofx.OK:
                    # Read inside the lock: the message belongs to this handle,
                    # and the handle can be destroyed the moment we let go.
                    try:
                        detail = (self._lib.rbgc_last_error(self._handle)
                                  or b"").decode("utf-8", "replace")
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            return SubmitResult(reason=f"the enhancement library failed: {exc}",
                                fatal=True)
        finally:
            # Dropped immediately. Native has copied whatever it needed, and
            # holding a reference to a decoded frame past the call is exactly
            # what would pin FFmpeg's buffer pool.
            self._overlay_ref = None

        if status != videofx.OK:
            reason = videofx.STATUS_NAMES.get(status, f"error {status}")
            return SubmitResult(
                reason=f"{reason}{f' ({detail})' if detail else ''}",
                # Everything but a caller mistake means this renderer is done.
                # A caller mistake is a bug to fix, not a reason to tear the
                # stream down for a frame that happened to be malformed.
                fatal=status != videofx.ERR_ARG,
            )

        return SubmitResult(
            ok=True,
            path=self._result.path,
            gpu_ms=self._result.gpu_ms,
            output_width=self._result.output_width,
            output_height=self._result.output_height,
            skipped=bool(self._result.present_skipped),
        )

    def repaint(self) -> bool:
        """Present the last frame again -- the overlay changed, or the window
        was resized while the stream was idle."""
        try:
            with self._lock:
                if self._handle is None or self._lib is None:
                    return False
                return self._lib.rbgc_repaint(self._handle) == videofx.OK
        except Exception:  # noqa: BLE001
            return False


@dataclass(frozen=True, slots=True)
class Overlay:
    """The client's own drawing, to be composited into the presented frame.

    ``owner`` keeps whatever holds the pixels alive across the call -- a
    QImage frees its buffer when the last reference goes, and the native side
    has no reference of its own.
    """

    address: int
    stride: int
    x: int
    y: int
    width: int
    height: int
    version: int
    owner: object = None

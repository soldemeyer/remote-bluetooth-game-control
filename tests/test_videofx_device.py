"""The Direct3D 11 renderer, against a real graphics device.

Skips cleanly everywhere else, in the idiom the rest of this suite uses. What
it needs is a GPU and the built library; a machine with neither is the case
``tests/test_videofx_loader.py`` covers.

**The colour tests are the valuable ones.** Everything else in this layer
fails loudly -- a shader that will not compile, a device that will not create.
The faults that survive to a user are the quiet ones, and they all look like
a plausible picture that is subtly wrong:

* a transposed colour matrix (HLSL defaults to column-major; the constants are
  written row-major);
* limited range treated as full, which crushes blacks and clips highlights and
  gets reported as "the stream looks contrasty";
* BT.601 used for HD, which turns skin tones green;
* the NV12 chroma plane read at the frame's height rather than the texture's,
  which gives correct luma and garbage colour.

None of those raises anything. Feeding known YUV in and checking the RGB that
comes out is the only thing that catches them.

The window is raw Win32 rather than Qt on purpose: several modules in this
suite set ``QT_QPA_PLATFORM=offscreen`` at import, and an offscreen window has
no HWND to present into. A test that silently skipped because of another
file's import order would be worse than no test.
"""

from __future__ import annotations

import ctypes
import sys

import pytest

from client.media import videofx

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the Direct3D 11 backend is Windows-only"
)


def _library():
    videofx.reset_cache()
    if not videofx.is_available():
        pytest.skip(f"no GPU enhancement library: {videofx.load_error()}")
    caps = videofx.probe()
    if not caps.usable:
        pytest.skip(f"no usable graphics device: {caps.reason_usable}")
    return videofx.library(), caps


# -- a window to present into -----------------------------------------------

WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000


class _Window:
    """A bare top-level window, created without registering a class.

    "STATIC" is pre-registered by the system, so this needs no WNDCLASS, no
    window procedure and no message loop -- the renderer only ever asks for its
    client rectangle and presents to it.
    """

    def __init__(self, width: int, height: int) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreateWindowExW.argtypes = [
            ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        user32.DestroyWindow.argtypes = [ctypes.c_void_p]
        self._user32 = user32
        # WS_POPUP with no border, so the client rectangle is the whole window
        # and the renderer's back buffer is exactly the size asked for.
        self.hwnd = user32.CreateWindowExW(
            0, "STATIC", "rbgc test", WS_POPUP,
            0, 0, width, height, None, None, None, None,
        )
        if not self.hwnd:
            raise OSError(ctypes.get_last_error())

    def close(self) -> None:
        if self.hwnd:
            self._user32.DestroyWindow(ctypes.c_void_p(self.hwnd))
            self.hwnd = None


@pytest.fixture
def window():
    win = _Window(320, 240)
    try:
        yield win
    finally:
        win.close()


# -- building a frame -------------------------------------------------------


class Frame:
    """A synthetic yuv420p picture, and the ctypes structures around it.

    The buffers are held on the instance because the C struct points into
    them; letting them fall out of scope would hand the library freed memory.
    """

    def __init__(self, width: int, height: int, y: int, u: int, v: int,
                 colorspace: int = 1, color_range: int = 1) -> None:
        self.width = width
        self.height = height
        cw, ch = (width + 1) // 2, (height + 1) // 2
        self.buf_y = (ctypes.c_uint8 * (width * height))(*([y] * (width * height)))
        self.buf_u = (ctypes.c_uint8 * (cw * ch))(*([u] * (cw * ch)))
        self.buf_v = (ctypes.c_uint8 * (cw * ch))(*([v] * (cw * ch)))
        self.blits = (videofx.CBlit * 1)()
        self.colorspace = colorspace
        self.color_range = color_range

    def build(self, dst: tuple[int, int, int, int],
              composed: tuple[int, int],
              src: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)):
        blit = self.blits[0]
        blit.src[0], blit.src[1], blit.src[2], blit.src[3] = src
        blit.dst[0], blit.dst[1], blit.dst[2], blit.dst[3] = dst

        frame = videofx.CFrame()
        frame.struct_size = ctypes.sizeof(videofx.CFrame)
        frame.plane[0] = ctypes.cast(self.buf_y, ctypes.c_void_p).value
        frame.plane[1] = ctypes.cast(self.buf_u, ctypes.c_void_p).value
        frame.plane[2] = ctypes.cast(self.buf_v, ctypes.c_void_p).value
        frame.stride[0] = self.width
        frame.stride[1] = (self.width + 1) // 2
        frame.stride[2] = (self.width + 1) // 2
        frame.src_width = self.width
        frame.src_height = self.height
        frame.colorspace = self.colorspace
        frame.color_range = self.color_range
        frame.composed_width = composed[0]
        frame.composed_height = composed[1]
        frame.blits = ctypes.cast(self.blits, ctypes.POINTER(videofx.CBlit))
        frame.blit_count = 1
        return frame


def make_renderer(lib, hwnd, mode):
    handle = ctypes.c_void_p()
    status = lib.rbgc_create(ctypes.c_void_p(hwnd), mode, ctypes.byref(handle))
    assert status == videofx.OK, videofx.STATUS_NAMES.get(status, status)
    # The swap chain uses FLIP_DISCARD, whose contract is that the back buffer
    # is gone the moment Present returns -- so without this every readback is
    # black, which is exactly what a renderer that drew nothing also produces.
    # Off in the client; it is a full-resolution copy per frame.
    lib.rbgc_debug_capture(handle, 1)
    return handle


def submit(lib, handle, frame):
    result = videofx.CResult()
    result.struct_size = ctypes.sizeof(videofx.CResult)
    status = lib.rbgc_submit(handle, ctypes.byref(frame), ctypes.byref(result))
    if status != videofx.OK:
        detail = (lib.rbgc_last_error(handle) or b"").decode("utf-8", "replace")
        pytest.fail(f"submit failed: {videofx.STATUS_NAMES.get(status, status)} ({detail})")
    return result


def readback(lib, handle):
    """The presented picture as (pixels, width, height), RGBA8 tightly packed."""
    width = ctypes.c_int32()
    height = ctypes.c_int32()
    lib.rbgc_debug_readback(handle, None, 0, ctypes.byref(width), ctypes.byref(height))
    size = width.value * height.value * 4
    assert size > 0
    buffer = (ctypes.c_uint8 * size)()
    status = lib.rbgc_debug_readback(
        handle, ctypes.cast(buffer, ctypes.c_void_p), size,
        ctypes.byref(width), ctypes.byref(height))
    assert status == videofx.OK, videofx.STATUS_NAMES.get(status, status)
    return bytes(buffer), width.value, height.value


def pixel(pixels, width, x, y):
    offset = (y * width + x) * 4
    return tuple(pixels[offset:offset + 3])


class TestTheDeviceComesUp:
    def test_a_renderer_can_be_created_and_destroyed(self, window):
        lib, _ = _library()
        handle = make_renderer(lib, window.hwnd, videofx.MODE_LANCZOS)
        lib.rbgc_destroy(handle)

    def test_a_null_window_is_refused(self):
        lib, _ = _library()
        handle = ctypes.c_void_p()
        assert lib.rbgc_create(None, videofx.MODE_LANCZOS, ctypes.byref(handle)) \
            == videofx.ERR_ARG

    def test_an_unknown_mode_is_refused(self, window):
        lib, _ = _library()
        handle = ctypes.c_void_p()
        assert lib.rbgc_create(ctypes.c_void_p(window.hwnd), 99, ctypes.byref(handle)) \
            == videofx.ERR_ARG

    def test_destroying_nothing_is_harmless(self):
        lib, _ = _library()
        lib.rbgc_destroy(None)


class TestColour:
    """What the quiet faults in this layer actually look like.

    BT.709 limited range, which is what essentially every console and capture
    card produces: luma 16-235, chroma centred on 128.
    """

    @pytest.mark.parametrize(
        "name,yuv,expected",
        [
            ("black", (16, 128, 128), (0, 0, 0)),
            ("white", (235, 128, 128), (255, 255, 255)),
            ("mid grey", (126, 128, 128), (128, 128, 128)),
        ],
    )
    def test_limited_range_luma_maps_to_the_right_grey(self, window, name, yuv, expected):
        lib, _ = _library()
        handle = make_renderer(lib, window.hwnd, videofx.MODE_LANCZOS)
        try:
            frame = Frame(64, 64, *yuv, colorspace=1, color_range=1)
            submit(lib, handle, frame.build(dst=(0, 0, 320, 240), composed=(320, 240)))
            pixels, width, _ = readback(lib, handle)
            got = pixel(pixels, width, width // 2, 120)
            for channel, want in zip(got, expected):
                assert abs(channel - want) <= 4, f"{name}: got {got}, wanted {expected}"
        finally:
            lib.rbgc_destroy(handle)

    def test_full_range_is_not_treated_as_limited(self, window):
        """The two differ by about 7% of the range at black, which is very
        visible and completely silent."""
        lib, _ = _library()
        handle = make_renderer(lib, window.hwnd, videofx.MODE_LANCZOS)
        try:
            limited = Frame(64, 64, 16, 128, 128, color_range=1)
            submit(lib, handle, limited.build(dst=(0, 0, 320, 240), composed=(320, 240)))
            as_limited = pixel(*readback(lib, handle)[:2], 160, 120)

            full = Frame(64, 64, 16, 128, 128, color_range=2)
            submit(lib, handle, full.build(dst=(0, 0, 320, 240), composed=(320, 240)))
            as_full = pixel(*readback(lib, handle)[:2], 160, 120)

            assert as_limited != as_full
            assert as_limited[0] <= 4          # limited: 16 is black
            assert as_full[0] >= 10            # full: 16 is a dark grey
        finally:
            lib.rbgc_destroy(handle)

    def test_the_matrix_is_not_transposed(self, window):
        """A transposed matrix still produces plausible colours -- which is
        why this checks a chroma-dominant one rather than a grey.

        HLSL packs matrices column-major by default; the constants here are
        written row-major, so the shaders are compiled with /Zpr. If that flag
        is ever dropped, greys stay perfect and everything coloured is wrong.
        """
        lib, _ = _library()
        handle = make_renderer(lib, window.hwnd, videofx.MODE_LANCZOS)
        try:
            # Strong red in BT.709 limited: high V, low U.
            frame = Frame(64, 64, 63, 102, 240, colorspace=1, color_range=1)
            submit(lib, handle, frame.build(dst=(0, 0, 320, 240), composed=(320, 240)))
            red, green, blue = pixel(*readback(lib, handle)[:2], 160, 120)
            assert red > 180, f"expected red, got {(red, green, blue)}"
            assert red > green + 60 and red > blue + 60
        finally:
            lib.rbgc_destroy(handle)


class TestTheLetterbox:
    def test_what_surrounds_the_picture_is_the_backdrop(self, window):
        """The composed picture is centred, and everything else is the
        client's own theme colour -- which is pushed rather than baked in,
        because the theme is switchable while the client runs.
        """
        lib, _ = _library()
        handle = make_renderer(lib, window.hwnd, videofx.MODE_LANCZOS)
        try:
            lib.rbgc_set_backdrop(handle, 0x204060)
            frame = Frame(64, 64, 235, 128, 128)
            # A composed picture smaller than the 320x240 window, so there is
            # a border to inspect.
            submit(lib, handle, frame.build(dst=(0, 0, 160, 120), composed=(160, 120)))
            pixels, width, height = readback(lib, handle)

            corner = pixel(pixels, width, 2, 2)
            assert abs(corner[0] - 0x20) <= 2
            assert abs(corner[1] - 0x40) <= 2
            assert abs(corner[2] - 0x60) <= 2

            middle = pixel(pixels, width, width // 2, height // 2)
            assert middle[0] > 200, "the picture is not in the middle"
        finally:
            lib.rbgc_destroy(handle)


class TestTheEnhancementActuallyRuns:
    def test_the_reported_path_says_what_happened(self, window):
        """Not what was asked for. An enhancement pass that silently does
        nothing is indistinguishable from one that works, which is the failure
        this project keeps rediscovering."""
        lib, caps = _library()
        if not caps.fsr1:
            pytest.skip(f"FSR 1 unavailable: {caps.reason_fsr1}")

        handle = make_renderer(lib, window.hwnd, videofx.MODE_FSR1)
        try:
            frame = Frame(64, 64, 126, 128, 128)
            result = submit(lib, handle, frame.build(dst=(0, 0, 320, 240), composed=(320, 240)))
            assert result.path == videofx.PATH_EASU_RCAS
        finally:
            lib.rbgc_destroy(handle)

    def test_no_super_resolution_when_nothing_is_being_enlarged(self, window):
        """The requirement: do not run super-resolution when the output is no
        bigger than the input. Decided per piece, per frame."""
        lib, caps = _library()
        if not caps.fsr1:
            pytest.skip("FSR 1 unavailable")

        handle = make_renderer(lib, window.hwnd, videofx.MODE_FSR1)
        try:
            frame = Frame(320, 240, 126, 128, 128)
            result = submit(lib, handle, frame.build(dst=(0, 0, 320, 240), composed=(320, 240)))
            assert result.path in (videofx.PATH_COPY, videofx.PATH_LANCZOS,
                                   videofx.PATH_DOWNSCALE)
            assert result.path != videofx.PATH_EASU_RCAS
        finally:
            lib.rbgc_destroy(handle)

    def test_every_mode_produces_a_distinct_picture(self, window):
        """The only evidence available that each backend is really running.

        No API reports whether the NVIDIA driver applied super-resolution
        after accepting the extension, so "is RTX VSR on?" cannot be asked
        directly. What can be asked is whether its output differs from the
        other two -- and a mode that has quietly fallen through to a plain
        scale would match one of them.

        Detailed content, not a flat field: every resampler agrees about a
        flat field, so one would prove nothing.
        """
        lib, caps = _library()
        if not (caps.fsr1 and caps.rtx_vsr):
            pytest.skip("needs both FSR 1 and RTX VSR to compare three paths")

        size = 240
        luma = bytearray(size * size)
        for y in range(size):
            for x in range(size):
                checker = 200 if ((x // 3 + y // 3) % 2) else 60
                luma[y * size + x] = min(235, max(16, checker + (x * 40 // size) - 20))

        def render(mode):
            handle = make_renderer(lib, window.hwnd, mode)
            try:
                frame = Frame(size, size, 128, 128, 128)
                ctypes.memmove(frame.buf_y, bytes(luma), len(luma))
                built = frame.build(dst=(0, 0, 320, 240), composed=(320, 240))
                for _ in range(3):
                    submit(lib, handle, built)
                return readback(lib, handle)[0]
            finally:
                lib.rbgc_destroy(handle)

        def mean_difference(a, b):
            count = min(len(a), len(b))
            total = sum(abs(a[i] - b[i]) for i in range(0, count, 4))
            return total / (count / 4)

        lanczos = render(videofx.MODE_LANCZOS)
        fsr = render(videofx.MODE_FSR1)
        vsr = render(videofx.MODE_RTX_VSR)

        assert mean_difference(lanczos, fsr) > 1.0, "FSR 1 matches the control"
        assert mean_difference(lanczos, vsr) > 1.0, "RTX VSR matches the control"
        assert mean_difference(fsr, vsr) > 1.0, "RTX VSR matches FSR 1"

    def test_easu_and_lanczos_do_not_produce_the_same_picture(self, window):
        """i.e. FSR is genuinely running, not falling through to the control.

        A gradient rather than a flat field: every resampler agrees about a
        flat field, so one would prove nothing.
        """
        lib, caps = _library()
        if not caps.fsr1:
            pytest.skip("FSR 1 unavailable")

        width = height = 32
        luma = bytearray()
        for y in range(height):
            for x in range(width):
                luma.append(16 + (219 * ((x // 4 + y // 4) % 2)))

        def render(mode):
            handle = make_renderer(lib, window.hwnd, mode)
            try:
                frame = Frame(width, height, 128, 128, 128)
                ctypes.memmove(frame.buf_y, bytes(luma), len(luma))
                submit(lib, handle, frame.build(dst=(0, 0, 320, 240), composed=(320, 240)))
                return readback(lib, handle)[0]
            finally:
                lib.rbgc_destroy(handle)

        assert render(videofx.MODE_FSR1) != render(videofx.MODE_LANCZOS)


class TestModeSwitchingWithoutRecreating:
    def test_every_transition_keeps_working(self, window):
        """Six transitions between the three GPU modes, with a frame through
        each. Nothing may be recreated but the pieces that have to be, and
        nothing may leak -- see the repeat count.
        """
        lib, caps = _library()
        modes = [videofx.MODE_LANCZOS]
        if caps.fsr1:
            modes.append(videofx.MODE_FSR1)
        if caps.rtx_vsr:
            modes.append(videofx.MODE_RTX_VSR)

        handle = make_renderer(lib, window.hwnd, modes[0])
        try:
            frame = Frame(64, 64, 126, 128, 128)
            for _ in range(4):
                for mode in modes:
                    assert lib.rbgc_set_mode(handle, mode) == videofx.OK
                    submit(lib, handle,
                           frame.build(dst=(0, 0, 320, 240), composed=(320, 240)))
        finally:
            lib.rbgc_destroy(handle)

    def test_sharpness_is_accepted_across_its_whole_range(self, window):
        lib, _ = _library()
        handle = make_renderer(lib, window.hwnd, videofx.MODE_FSR1)
        try:
            for attenuation in (0.0, 0.25, 0.5, 1.0, 2.0):
                assert lib.rbgc_set_sharpness(handle, attenuation) == videofx.OK
            assert lib.rbgc_set_sharpness(handle, -1.0) == videofx.ERR_ARG
            assert lib.rbgc_set_sharpness(handle, 99.0) == videofx.ERR_ARG
        finally:
            lib.rbgc_destroy(handle)


class TestSplitScreen:
    def test_three_pieces_render_without_reaching_into_each_other(self, window):
        """Each piece is a different flat colour, drawn from a different part
        of one source. If a piece sampled its neighbour, the colours would
        bleed at the seams -- which is the leak this whole feature exists to
        prevent, and it would look like an encoder artefact.
        """
        lib, _ = _library()
        handle = make_renderer(lib, window.hwnd, videofx.MODE_LANCZOS)
        try:
            # Four quadrants of a 64x64 luma plane at four distinct levels.
            width = height = 64
            luma = bytearray(width * height)
            for y in range(height):
                for x in range(width):
                    level = 40 + 60 * ((1 if x >= 32 else 0) + 2 * (1 if y >= 32 else 0))
                    luma[y * width + x] = level

            frame = Frame(width, height, 0, 128, 128)
            ctypes.memmove(frame.buf_y, bytes(luma), len(luma))

            blits = (videofx.CBlit * 3)()
            quadrants = [
                ((0.0, 0.0, 0.5, 0.5), (0, 0, 150, 110)),
                ((0.5, 0.0, 0.5, 0.5), (160, 0, 150, 110)),
                ((0.0, 0.5, 0.5, 0.5), (80, 120, 150, 110)),
            ]
            for blit, (src, dst) in zip(blits, quadrants):
                blit.src[0], blit.src[1], blit.src[2], blit.src[3] = src
                blit.dst[0], blit.dst[1], blit.dst[2], blit.dst[3] = dst

            built = frame.build(dst=(0, 0, 320, 240), composed=(320, 240))
            built.blits = ctypes.cast(blits, ctypes.POINTER(videofx.CBlit))
            built.blit_count = 3
            submit(lib, handle, built)

            pixels, w, _ = readback(lib, handle)
            centres = [
                pixel(pixels, w, 75, 55),
                pixel(pixels, w, 235, 55),
                pixel(pixels, w, 155, 175),
            ]
            greys = [c[0] for c in centres]
            assert len(set(greys)) == 3, f"pieces are not distinct: {greys}"
            assert greys == sorted(greys), f"pieces are in the wrong order: {greys}"
        finally:
            lib.rbgc_destroy(handle)


class TestItSurvivesTheAwkwardCases:
    def test_a_malformed_frame_is_refused_rather_than_crashing(self, window):
        lib, _ = _library()
        handle = make_renderer(lib, window.hwnd, videofx.MODE_LANCZOS)
        try:
            empty = videofx.CFrame()
            empty.struct_size = ctypes.sizeof(videofx.CFrame)
            assert lib.rbgc_submit(handle, ctypes.byref(empty), None) == videofx.ERR_ARG
            assert lib.rbgc_submit(handle, None, None) == videofx.ERR_ARG
        finally:
            lib.rbgc_destroy(handle)

    def test_the_window_can_be_resized_mid_stream(self, window):
        """Resize, fullscreen, a move to another monitor and a DPI change are
        all the same thing to this layer: the client rectangle differs from
        the back buffer, so the back buffer follows it.

        Nothing in Python is involved, which is what sidesteps the trap the
        client already documents -- a move to a monitor with a different device
        pixel ratio raises no resize event of its own.
        """
        import ctypes

        lib, _ = _library()
        user32 = ctypes.WinDLL("user32")
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]

        handle = make_renderer(lib, window.hwnd, videofx.MODE_LANCZOS)
        try:
            frame = Frame(64, 64, 126, 128, 128)
            seen = []
            for width, height in ((320, 240), (640, 480), (500, 300), (320, 240)):
                # SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE
                user32.SetWindowPos(ctypes.c_void_p(window.hwnd), None, 0, 0,
                                    width, height, 0x0002 | 0x0004 | 0x0010)
                result = submit(lib, handle,
                                frame.build(dst=(0, 0, width, height),
                                            composed=(width, height)))
                seen.append((result.output_width, result.output_height))

            assert seen == [(320, 240), (640, 480), (500, 300), (320, 240)], seen
        finally:
            lib.rbgc_destroy(handle)

    def test_a_resize_does_not_leak_the_old_buffers(self, window):
        """Repeated, because a swap chain that kept a reference to every back
        buffer it ever had would look fine for one resize."""
        import ctypes

        lib, _ = _library()
        user32 = ctypes.WinDLL("user32")
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]

        handle = make_renderer(lib, window.hwnd, videofx.MODE_FSR1)
        try:
            frame = Frame(64, 64, 126, 128, 128)
            for step in range(30):
                width = 320 + (step % 8) * 40
                height = 240 + (step % 8) * 30
                user32.SetWindowPos(ctypes.c_void_p(window.hwnd), None, 0, 0,
                                    width, height, 0x0002 | 0x0004 | 0x0010)
                submit(lib, handle,
                       frame.build(dst=(0, 0, width, height),
                                   composed=(width, height)))
        finally:
            lib.rbgc_destroy(handle)

    def test_the_stream_resolution_can_change_mid_session(self, window):
        """The decoder's graph cache keys on this; the renderer has to notice
        it too, or it uploads a 720p frame into 1080p textures."""
        lib, _ = _library()
        handle = make_renderer(lib, window.hwnd, videofx.MODE_LANCZOS)
        try:
            for size in (64, 128, 96, 64):
                frame = Frame(size, size, 126, 128, 128)
                submit(lib, handle,
                       frame.build(dst=(0, 0, 320, 240), composed=(320, 240)))
        finally:
            lib.rbgc_destroy(handle)

    def test_many_frames_do_not_exhaust_anything(self, window):
        lib, _ = _library()
        handle = make_renderer(lib, window.hwnd, videofx.MODE_LANCZOS)
        try:
            frame = Frame(64, 64, 126, 128, 128)
            built = frame.build(dst=(0, 0, 320, 240), composed=(320, 240))
            for _ in range(120):
                submit(lib, handle, built)
        finally:
            lib.rbgc_destroy(handle)

    def test_a_repaint_with_no_new_picture_is_harmless(self, window):
        lib, _ = _library()
        handle = make_renderer(lib, window.hwnd, videofx.MODE_LANCZOS)
        try:
            assert lib.rbgc_repaint(handle) == videofx.OK       # before any frame
            frame = Frame(64, 64, 126, 128, 128)
            submit(lib, handle, frame.build(dst=(0, 0, 320, 240), composed=(320, 240)))
            assert lib.rbgc_repaint(handle) == videofx.OK
        finally:
            lib.rbgc_destroy(handle)


class TestTiming:
    def test_gpu_time_is_reported_without_stalling(self, window):
        """Read back a frame late and skipped when not ready. A blocking read
        would make the measurement the largest thing being measured."""
        lib, _ = _library()
        handle = make_renderer(lib, window.hwnd, videofx.MODE_LANCZOS)
        try:
            frame = Frame(64, 64, 126, 128, 128)
            built = frame.build(dst=(0, 0, 320, 240), composed=(320, 240))
            seen = []
            for _ in range(20):
                seen.append(submit(lib, handle, built).gpu_ms)
            assert any(value >= 0.0 for value in seen), "no timing was ever reported"
            for value in seen:
                assert value < 100.0, f"implausible GPU time {value} ms"
        finally:
            lib.rbgc_destroy(handle)

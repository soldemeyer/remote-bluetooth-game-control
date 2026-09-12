"""What happens after the GPU renderer fails, and who is allowed to call it.

Both of these were live faults, and both presented as *"selecting the upscaler
freezes the program and then nothing happens"* -- which is one sentence
covering two unrelated causes:

* The decode thread drops the renderer by itself when it fails, and nothing
  told the GUI. The window suppresses its own painting while it believes a
  renderer is presenting, so the picture froze while the decoder went on
  producing frames nobody drew, with every counter healthy.
* Nothing serialised the two threads that reach the renderer. A Direct3D 11
  immediate context tolerates exactly one caller, and switching back to Off
  destroys the swap chain -- on the GUI thread, underneath a submit in flight
  on the decode thread.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

pytest.importorskip("PySide6", reason="client GUI extras not installed")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from client.media.gpu_upscaler import GPUUpscaler  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


# --------------------------------------------------------------------------
# The window has to notice a detach it did not perform
# --------------------------------------------------------------------------


class FakeDecoder:
    """Just enough of a decoder for the window's frame tick."""

    def __init__(self) -> None:
        self.version = 0
        self.upscaler_fault = ""
        self.frames_decoded = 0
        self.decode_errors = 0
        self.recoveries = 0
        self.last_path = ""
        self.last_gpu_ms = -1.0
        self.last_output = (0, 0)
        self.upscaler = None
        self.overlay = None
        self.viewport = None

    def set_frame_listener(self, listener) -> None:
        self.listener = listener

    def set_upscaler(self, upscaler) -> None:
        self.upscaler = upscaler

    def set_overlay(self, overlay) -> None:
        self.overlay = overlay

    def set_viewport(self, width, height) -> None:
        self.viewport = (width, height)

    def latest(self):
        return None

    def stats(self) -> dict:
        return {}


class FakeReceiver:
    from common.timing import LatencyStats

    present_stats = LatencyStats()
    decode_stats = LatencyStats()
    clock_locked = True
    audio_underruns = 0

    def stats(self) -> dict:
        return {}


def window(qapp, decoder):
    from client.gui.video_window import VideoWindow

    return VideoWindow(decoder, FakeReceiver())


class StubRenderer:
    """Stands in for an attached renderer without needing a GPU."""

    def __init__(self) -> None:
        self.shut_down = False

    def shutdown(self) -> None:
        self.shut_down = True


class TestTheWindowActsOnADetachItDidNotPerform:
    def test_a_latched_fault_tears_the_gpu_path_down(self, qapp):
        surface = window(qapp, FakeDecoder())
        try:
            # The window gates every paint path on this being non-None, which
            # is precisely what made a silent detach freeze the picture.
            renderer = StubRenderer()
            surface._upscaler = renderer
            surface._decoder.upscaler_fault = "the device was lost"

            seen: list[str] = []
            surface.gpu_failed.connect(seen.append)
            surface._on_frame_ready()

            assert surface._upscaler is None, "the window kept suppressing its paint"
            assert renderer.shut_down, "the renderer was leaked"
            assert surface._decoder.upscaler is None, "the decoder was left attached"
            assert seen == ["the device was lost"]
        finally:
            surface.deleteLater()

    def test_the_fault_is_consumed_so_it_is_reported_once(self, qapp):
        surface = window(qapp, FakeDecoder())
        try:
            surface._upscaler = StubRenderer()
            surface._decoder.upscaler_fault = "the device was lost"

            seen: list[str] = []
            surface.gpu_failed.connect(seen.append)
            surface._on_frame_ready()
            surface._on_frame_ready()

            assert len(seen) == 1, "the failure was reported on every tick"
            assert surface._decoder.upscaler_fault == ""
        finally:
            surface.deleteLater()

    def test_nothing_happens_when_the_gpu_path_was_never_attached(self, qapp):
        """The ordinary case: no renderer, no fault, no work."""
        surface = window(qapp, FakeDecoder())
        try:
            seen: list[str] = []
            surface.gpu_failed.connect(seen.append)
            surface._on_frame_ready()
            assert seen == []
        finally:
            surface.deleteLater()


# --------------------------------------------------------------------------
# One caller at a time
# --------------------------------------------------------------------------


class SlowLibrary:
    """A library whose submit takes long enough to be caught mid-call."""

    def __init__(self, hold: float = 0.25) -> None:
        self.hold = hold
        self.inside = threading.Event()
        self.destroyed_at: float | None = None
        self.submit_left_at: float | None = None

    def rbgc_submit(self, handle, frame, result):
        self.inside.set()
        time.sleep(self.hold)
        self.submit_left_at = time.perf_counter()
        return 0

    def rbgc_destroy(self, handle):
        self.destroyed_at = time.perf_counter()

    def rbgc_last_error(self, handle):
        return b""

    def rbgc_set_sharpness(self, handle, value):
        return 0

    def rbgc_set_backdrop(self, handle, value):
        return 0


def opened(lib) -> GPUUpscaler:
    import ctypes

    upscaler = GPUUpscaler()
    upscaler._lib = lib
    upscaler._handle = ctypes.c_void_p(0x1234)
    upscaler._mode = 1
    return upscaler


def a_submit(upscaler):
    from client.media.planner import Blit

    return upscaler.submit(
        blits=(Blit(src=(0.0, 0.0, 1.0, 1.0), dst=(0, 0, 640, 360)),),
        composed=(640, 360),
        src_size=(640, 360),
        colorspace=1,
        color_range=1,
        planes=(1, 2, 3),
        strides=(640, 320, 320),
        overlay=None,
    )


class TestOnlyOneThreadIsInsideTheLibrary:
    def test_destroying_waits_for_a_submit_in_flight(self):
        """**This is the freeze.**

        `rbgc_destroy` tears the swap chain down and flushes the context. Doing
        that underneath a submit is undefined behaviour on a non-thread-safe
        immediate context, and it is the ordinary path -- switching the setting
        back to Off while the stream is running.
        """
        lib = SlowLibrary(hold=0.25)
        upscaler = opened(lib)

        worker = threading.Thread(target=a_submit, args=(upscaler,))
        worker.start()
        assert lib.inside.wait(2.0), "the submit never started"

        upscaler.shutdown()
        worker.join(5.0)

        assert lib.destroyed_at is not None, "nothing was destroyed"
        assert lib.submit_left_at is not None
        assert lib.destroyed_at >= lib.submit_left_at, (
            "the renderer was destroyed while a submit was inside it")

    def test_a_submit_after_shutdown_is_refused_rather_than_using_a_dead_handle(self):
        lib = SlowLibrary(hold=0.0)
        upscaler = opened(lib)
        upscaler.shutdown()

        result = a_submit(upscaler)
        assert not result.ok
        assert result.fatal, "the caller must drop to software, not retry"

    def test_shutdown_is_safe_twice(self):
        lib = SlowLibrary(hold=0.0)
        upscaler = opened(lib)
        upscaler.shutdown()
        upscaler.shutdown()
        assert not upscaler.is_open

"""Drawing several cropped pieces in one window.

The decoder produces the pieces already scaled to the size they are drawn at
(``test_client_regions.py``); this is the other half of that bargain -- the
window has to place them without scaling anything, or the GIL cost the decoder
went to trouble to avoid comes straight back at paint time.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="client GUI extras not installed")
pytest.importorskip("av", reason="video extras not installed")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage  # noqa: E402

from client.media.decoder import PresentFrame, RegionView  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app):
    from client.gui.video_window import VideoWindow

    class FakeDecoder:
        version = 0

        def latest(self):
            return None

        def set_viewport(self, width, height):
            pass

        def set_frame_listener(self, listener):
            pass

    class FakeReceiver:
        # Real stats objects, not None: the on-screen overlay reads them
        # during paintEvent, and an exception there is reported as a Qt
        # override failure rather than as anything to do with this test.
        def __init__(self):
            from common.timing import LatencyStats

            self.present_stats = LatencyStats()
            self.decode_stats = LatencyStats()
            self.audio_underruns = 0

        def snapshot(self):
            return {}

    win = VideoWindow(FakeDecoder(), FakeReceiver())
    win.resize(640, 480)
    # The latency overlay draws over the picture and reads a dozen fields off
    # the receiver. These tests are about where the pieces land, so it is off
    # rather than stubbed -- and a sampled pixel then cannot be overlay text.
    win._show_osd = False
    try:
        yield win
    finally:
        win.close()


def view(width, height, x, y, value):
    """A flat RGB block, as the decoder would hand one over."""
    stride = width * 3
    pixels = memoryview(bytearray([value]) * (stride * height))
    return RegionView(
        pixels=pixels, owner=pixels, width=width, height=height,
        stride=stride, x=x, y=y,
    )


def frame_with(views, composed):
    first = views[0]
    return PresentFrame(
        pixels=first.pixels, owner=first.owner,
        width=first.width, height=first.height, stride=first.stride,
        capture_ts=0, decoded_ns=0, version=1,
        views=tuple(views),
        composed_width=composed[0], composed_height=composed[1],
    )


def painted(window) -> QImage:
    """What the window actually draws, rendered offscreen."""
    image = QImage(window.width(), window.height(), QImage.Format.Format_RGB32)
    image.fill(0)
    window.render(image)
    return image


class TestItDrawsEveryPiece:
    def test_two_pieces_both_reach_the_screen(self, window):
        """The failure this catches is drawing only the first, which looks
        like a working single-region crop and silently loses the player's
        second controller's view."""
        window._adopt_views(
            frame_with(
                [view(300, 220, 0, 0, 60), view(300, 220, 340, 260, 200)],
                (640, 480),
            )
        )
        image = painted(window)

        assert window._views and len(window._views) == 2
        # A pixel well inside each piece carries that piece's value.
        assert abs((image.pixel(150, 110) & 0xFF) - 60) <= 2
        assert abs((image.pixel(490, 370) & 0xFF) - 200) <= 2

    def test_the_gap_between_them_is_backdrop(self, window):
        """Two unrelated viewports butted together read as one picture with a
        seam, which is the very thing the detector spends its time hunting."""
        window._adopt_views(
            frame_with(
                [view(300, 480, 0, 0, 60), view(300, 480, 340, 0, 200)],
                (640, 480),
            )
        )
        image = painted(window)
        gutter = image.pixel(320, 240) & 0xFF
        assert gutter != 60 and gutter != 200


class TestItFallsBackToTheWholePicture:
    def test_no_views_leaves_the_single_image_path_alone(self, window):
        frame = PresentFrame(
            pixels=memoryview(bytearray([120]) * (320 * 3 * 240)),
            owner=object(), width=320, height=240, stride=320 * 3,
            capture_ts=0, decoded_ns=0, version=1,
        )
        window._adopt_views(frame)
        assert window._views == []
        assert window._composed == (0, 0)

    def test_clearing_regions_drops_the_previous_views(self, window):
        """Otherwise a player told to stop cropping would keep seeing the last
        crop forever, with every counter reporting a healthy stream."""
        window._adopt_views(
            frame_with([view(300, 220, 0, 0, 60)], (300, 220))
        )
        assert window._views

        window._adopt_views(
            PresentFrame(
                pixels=memoryview(bytearray(9)), owner=object(),
                width=1, height=1, stride=3,
                capture_ts=0, decoded_ns=0, version=2,
            )
        )
        assert window._views == []

    def test_a_frame_without_the_field_still_paints(self, window):
        """A decoder from before regions existed, or a stub in a test. This
        runs inside paintEvent, which is a bad place to raise."""

        class OldFrame:
            pixels = memoryview(bytearray(9))
            owner = object()
            width, height, stride = 1, 1, 3

        window._adopt_views(OldFrame())
        assert window._views == []


class TestNothingIsScaledAtPaintTime:
    def test_a_composed_picture_that_fits_is_drawn_one_to_one(self, window):
        """The decoder sizes the composed picture to the viewport precisely so
        this is a blit. If it ever is not, the GIL cost the decoder avoided
        comes back at paint time -- measured at 1.81 ms p99 against a 500 Hz
        input loop."""
        window._adopt_views(
            frame_with([view(640, 480, 0, 0, 90)], (640, 480))
        )
        image, x, y, width, height = window._views[0]
        assert (width, height) == (640, 480)
        assert (x, y) == (0, 0)
        assert image.size().width() == 640

    def test_the_views_are_not_copies(self, window):
        """QImage wraps the buffer, and the window holds the owner so it
        outlives the image. A copy here would be the 6.22 MB under the GIL
        that client/media/decoder.py exists to avoid."""
        one = view(320, 240, 0, 0, 90)
        window._adopt_views(frame_with([one], (320, 240)))
        assert window._view_owners == [one.owner]

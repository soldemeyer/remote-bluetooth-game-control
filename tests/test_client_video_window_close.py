"""Closing the video window has to be a thing the player can actually do.

Two faults, and the first hid the second:

  * the main window connected to ``QObject.destroyed``, which never fires --
    the video window has a parent and the app holds a reference, so closing it
    only hides it. The button therefore stayed on "Close video" for good.
  * ``_tick_video`` opens the window whenever the stream is up and no window
    exists, on *every* tick. With the first fault fixed, closing it would have
    reopened it immediately: an unclosable window.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="client GUI extras not installed")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from client import config as client_config  # noqa: E402
from client.gui.app import MainWindow  # noqa: E402
from client.net.video import VideoStreamState  # noqa: E402
from common.timing import LatencyStats  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, monkeypatch):
    monkeypatch.setattr(client_config, "save", lambda config, path=None: None)
    win = MainWindow(client_config.ClientConfig(backend_override="synthetic"))
    try:
        yield win
    finally:
        win.close()


class FakeDecoder:
    version = 0
    width = 320
    height = 180

    def __init__(self) -> None:
        self.listener = None
        self.viewport = None

    def take_present_frame(self):
        return None

    def latest(self):
        return None

    def set_viewport(self, width, height):
        # Also required, not optional: the window scales through the
        # decoder now, because QPainter holds the GIL while it scales
        # and the 500 Hz input loop shares this process.
        self.viewport = (width, height)

    def set_frame_listener(self, listener):
        # Required, not optional. A window whose decoder cannot notify it
        # would silently fall back to the 100 ms safety timer -- a tenfold
        # presentation regression with nothing to say it had happened --
        # so the window asks for this outright rather than probing for it,
        # and a double has to provide it.
        self.listener = listener


class FakeReceiver:
    """Enough of VideoReceiver for the window to paint itself.

    paintEvent runs the moment the window is shown and reads most of these, so
    a thinner stand-in fails on the first repaint rather than on the behaviour
    under test.
    """

    def __init__(self) -> None:
        self.decode_stats = LatencyStats()
        self.present_stats = LatencyStats()
        self.idr_requests = 0
        self.clock_offset_ns = 0
        self.clock_locked = True
        self.connection_mode = "direct"
        self.state = VideoStreamState.STREAMING
        self.state_detail = ""
        self.audio_underruns = 0
        self.frames_decoded = 0

    def get_frame(self, timeout: float = 0.1):
        return None

    def request_idr(self, reason: int = 0) -> None:
        self.idr_requests += 1


class TestTheWindowReportsItsOwnClosing:
    def test_closing_emits_the_signal(self, qt_app):
        from client.gui.video_window import VideoWindow

        video = VideoWindow(FakeDecoder(), FakeReceiver())
        seen: list[int] = []
        video.closed.connect(lambda: seen.append(1))

        video.show()
        qt_app.processEvents()
        video.close()
        qt_app.processEvents()

        assert seen == [1], "closing the window told nobody"

    def test_destroyed_would_not_have_fired(self, qt_app):
        """Guards the reason `closed` exists rather than reusing `destroyed`."""
        from PySide6.QtWidgets import QWidget

        from client.gui.video_window import VideoWindow

        parent = QWidget()
        video = VideoWindow(FakeDecoder(), FakeReceiver(), parent)
        destroyed: list[int] = []
        video.destroyed.connect(lambda *_: destroyed.append(1))

        video.show()
        qt_app.processEvents()
        video.close()
        qt_app.processEvents()

        assert destroyed == [], (
            "destroyed now fires on close, so the comment on `closed` is stale"
        )


class TestTheButtonFollowsThePicture:
    """The picture is embedded now, so there is no window to close.

    The property these guard is unchanged and is the one that was reported:
    the button changed to say the picture was up and never changed back.
    """

    def _show(self, window, qt_app):
        window._video_decoder = FakeDecoder()
        window._video_receiver = FakeReceiver()
        window._show_video()
        qt_app.processEvents()

    def test_showing_switches_the_button(self, window, qt_app):
        self._show(window, qt_app)

        assert window._video_surface is not None
        assert window._stage.has_surface()
        assert window._connection.video_button.text() == "Hide video"

    def test_hiding_puts_the_button_back(self, window, qt_app):
        self._show(window, qt_app)

        window._hide_video()
        qt_app.processEvents()

        assert window._connection.video_button.text() == "Watch stream"
        assert window._video_surface is None
        assert not window._stage.has_surface()

    def test_the_button_itself_puts_it_back_too(self, window, qt_app):
        self._show(window, qt_app)

        window._on_watch_clicked()
        qt_app.processEvents()

        assert window._connection.video_button.text() == "Watch stream"
        assert window._video_surface is None

    def test_one_click_is_enough_to_show_it_again(self, window, qt_app):
        """A stale reference would make the next click hide nothing."""
        self._show(window, qt_app)
        window._hide_video()
        qt_app.processEvents()

        window._on_watch_clicked()
        qt_app.processEvents()

        assert window._video_surface is not None
        assert window._connection.video_button.text() == "Hide video"

    def test_the_stage_falls_back_to_its_placeholder(self, window, qt_app):
        """Hiding must leave something behind, not an empty panel."""
        self._show(window, qt_app)
        window._hide_video()
        qt_app.processEvents()

        assert window._stage.placeholder.isVisible() or not window._stage.has_surface()


class TestHidingSticks:
    def test_hiding_marks_it_dismissed(self, window, qt_app):
        window._video_decoder = FakeDecoder()
        window._video_receiver = FakeReceiver()
        window._show_video()
        qt_app.processEvents()

        window._hide_video()
        qt_app.processEvents()

        assert window._video_dismissed is True, (
            "the every-tick auto-show would put the picture straight back"
        )

    def test_showing_clears_the_dismissal(self, window, qt_app):
        window._video_dismissed = True
        window._video_decoder = FakeDecoder()
        window._video_receiver = FakeReceiver()

        window._show_video()
        qt_app.processEvents()

        assert window._video_dismissed is False

    def test_a_stream_restart_re_arms_the_auto_show(self, window, qt_app):
        """A retry or reconnect should show the picture again by itself."""
        window._video_dismissed = True

        window._stop_video()

        assert window._video_dismissed is False
        assert window._connection.video_button.text() == "Watch stream"


class TestTheDecoderIsLetGoOf:
    """`close()` used to do this. Nothing closes an embedded widget.

    Left undone, the decoder keeps a callback into a surface nobody is showing
    and goes on scaling every frame to a viewport that is not visible -- a
    cost with no symptom, which is the kind that survives for a long time.
    """

    def test_hiding_detaches_the_frame_listener(self, window, qt_app):
        decoder = FakeDecoder()
        window._video_decoder = decoder
        window._video_receiver = FakeReceiver()
        window._show_video()
        qt_app.processEvents()
        assert decoder.listener is not None

        window._hide_video()
        qt_app.processEvents()

        assert decoder.listener is None

    def test_stopping_the_stream_detaches_it_too(self, window, qt_app):
        decoder = FakeDecoder()
        window._video_decoder = decoder
        window._video_receiver = FakeReceiver()
        window._show_video()
        qt_app.processEvents()

        window._stop_video()
        qt_app.processEvents()

        assert decoder.listener is None

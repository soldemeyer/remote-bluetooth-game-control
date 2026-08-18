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

    def take_present_frame(self):
        return None


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


class TestTheButtonFollowsTheWindow:
    def _open(self, window, qt_app):
        window._video_decoder = FakeDecoder()
        window._video_receiver = FakeReceiver()
        window._open_video_window()
        qt_app.processEvents()

    def test_opening_switches_the_button(self, window, qt_app):
        self._open(window, qt_app)

        assert window._video_window is not None
        assert window._video_button.text() == "Close video"

    def test_closing_the_window_puts_the_button_back(self, window, qt_app):
        """The report: it changed to "Close Video" and never changed back."""
        self._open(window, qt_app)

        window._video_window.close()
        qt_app.processEvents()

        assert window._video_button.text() == "Watch stream"
        assert window._video_window is None

    def test_the_close_button_puts_it_back_too(self, window, qt_app):
        self._open(window, qt_app)

        window._on_watch_clicked()
        qt_app.processEvents()

        assert window._video_button.text() == "Watch stream"
        assert window._video_window is None

    def test_one_click_is_enough_to_reopen_afterwards(self, window, qt_app):
        """A stale reference would make the next click close nothing."""
        self._open(window, qt_app)
        window._video_window.close()
        qt_app.processEvents()

        window._on_watch_clicked()
        qt_app.processEvents()

        assert window._video_window is not None
        assert window._video_button.text() == "Close video"


class TestClosingSticks:
    def test_closing_marks_it_dismissed(self, window, qt_app):
        window._video_decoder = FakeDecoder()
        window._video_receiver = FakeReceiver()
        window._open_video_window()
        qt_app.processEvents()

        window._video_window.close()
        qt_app.processEvents()

        assert window._video_window_dismissed is True, (
            "the every-tick auto-open would put the window straight back"
        )

    def test_opening_clears_the_dismissal(self, window, qt_app):
        window._video_window_dismissed = True
        window._video_decoder = FakeDecoder()
        window._video_receiver = FakeReceiver()

        window._open_video_window()
        qt_app.processEvents()

        assert window._video_window_dismissed is False

    def test_a_stream_restart_re_arms_the_auto_open(self, window, qt_app):
        """A retry or reconnect should show the picture again by itself."""
        window._video_window_dismissed = True

        window._stop_video()

        assert window._video_window_dismissed is False
        assert window._video_button.text() == "Watch stream"

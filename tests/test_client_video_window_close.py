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
        self.hw_decode = None
        self.regions = None
        self.last_path = ""
        self.last_path_code = 0
        self.last_gpu_ms = -1.0
        self.upscaler = None
        self.overlay = None
        self.upscaler_fault = ""

    def take_present_frame(self):
        return None

    def latest(self):
        return None

    def set_viewport(self, width, height):
        # Also required, not optional: the window scales through the
        # decoder now, because QPainter holds the GIL while it scales
        # and the 500 Hz input loop shares this process.
        self.viewport = (width, height)

    def set_regions(self, regions):
        self.regions = regions

    def set_hw_decode(self, device):
        # Required, not optional, for the same reason `set_frame_listener` is:
        # showing the picture is what applies the video settings to it, so a
        # double without this fails at `_show_video`. That ordering is the
        # point -- a preference chosen before the stream existed used to take
        # effect only if the player touched the control a second time.
        self.hw_decode = device

    def set_upscaler(self, upscaler):
        self.upscaler = upscaler

    def set_overlay(self, overlay):
        self.overlay = overlay

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
        # What the placeholder reads to say *why* there is no picture. The
        # window reads them defensively, so a fake without them still paints --
        # but a fake of a real class should have the real class's shape, or the
        # test passes against a widget nobody could actually use.
        self.slices_received = 0
        self.frames_arrived = False

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
        assert window.video_button.text() == "Hide video"

    def test_hiding_puts_the_button_back(self, window, qt_app):
        self._show(window, qt_app)

        window._hide_video()
        qt_app.processEvents()

        assert window.video_button.text() == "Watch stream"
        assert window._video_surface is None
        assert not window._stage.has_surface()

    def test_the_button_itself_puts_it_back_too(self, window, qt_app):
        self._show(window, qt_app)

        window._on_watch_clicked()
        qt_app.processEvents()

        assert window.video_button.text() == "Watch stream"
        assert window._video_surface is None

    def test_one_click_is_enough_to_show_it_again(self, window, qt_app):
        """A stale reference would make the next click hide nothing."""
        self._show(window, qt_app)
        window._hide_video()
        qt_app.processEvents()

        window._on_watch_clicked()
        qt_app.processEvents()

        assert window._video_surface is not None
        assert window.video_button.text() == "Hide video"

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
        assert window.video_button.text() == "Watch stream"


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


class TestTheStatusBarIsHandedBack:
    """*"Connecting to the video stream..."* was set once and never taken back.

    It then sat under a perfectly good picture for the rest of the session,
    and read as the stream being stuck -- which is exactly what somebody
    chasing an unrelated fault does not need to see. Reported alongside a real
    GPU fault, where it made a fixed problem look unfixed.
    """

    def _connected(self, window):
        window._set_status("Connected (direct) — streaming 1 controller(s)")

    def test_it_says_what_it_is_doing_while_connecting(self, window, qt_app,
                                                       monkeypatch):
        self._connected(window)
        monkeypatch.setattr(window, "_pending_video_source", lambda: None)

        # Drive it the way `_tick_video` does, without a real receiver.
        window._status_before_video = window.statusBar().currentMessage()
        window._video_status_state = None
        window._set_status("Connecting to the video stream...")
        assert "Connecting to the video stream" in window.statusBar().currentMessage()

    def test_streaming_gives_the_bar_back(self, window, qt_app, monkeypatch):
        self._connected(window)
        before = window.statusBar().currentMessage()

        window._status_before_video = before
        window._video_status_state = None
        window._set_status("Connecting to the video stream...")

        window._video_decoder = FakeDecoder()
        window._video_receiver = FakeReceiver()   # state is STREAMING
        monkeypatch.setattr(
            window, "_pending_video_source", lambda: {"available": True})
        monkeypatch.setattr(window, "_pending_video_regions", lambda: [])
        window._video_dismissed = True            # do not open a surface here

        window._tick_video()

        assert window.statusBar().currentMessage() == before, (
            "the connecting message outlived the connection"
        )

    def test_a_failure_says_so_rather_than_staying_on_connecting(
            self, window, qt_app, monkeypatch):
        from client.net.video import VideoStreamState

        self._connected(window)
        window._status_before_video = window.statusBar().currentMessage()
        window._video_status_state = None
        window._set_status("Connecting to the video stream...")

        receiver = FakeReceiver()
        receiver.state = VideoStreamState.FAILED
        window._video_decoder = FakeDecoder()
        window._video_receiver = receiver
        monkeypatch.setattr(
            window, "_pending_video_source", lambda: {"available": True})
        monkeypatch.setattr(window, "_pending_video_regions", lambda: [])

        window._tick_video()

        message = window.statusBar().currentMessage()
        assert "Connecting to the video stream" not in message
        assert "failed" in message.lower()


class TestTheVideoSettingsReachANewSurface:
    def test_showing_the_picture_applies_them(self, window, qt_app, monkeypatch):
        """A preference chosen before the stream existed -- the ordinary
        order, since the panel is reachable from the moment the app opens --
        used to take effect only when the control was touched again."""
        applied: list[int] = []
        monkeypatch.setattr(window, "_apply_video_settings",
                            lambda: applied.append(1))

        window._video_decoder = FakeDecoder()
        window._video_receiver = FakeReceiver()
        window._show_video()
        qt_app.processEvents()

        assert applied == [1], "the settings were never pushed at the new surface"

    def test_a_renderer_that_fails_puts_the_control_back_to_off(
            self, window, qt_app):
        """The decode thread drops the GPU path by itself. A selector still
        reading "RTX VSR" over a software picture is the control lying about
        what it did -- and it re-arms the same failure on the next reconnect."""
        window._video_panel.select("fsr1")
        qt_app.processEvents()

        window._on_gpu_failed("the device was lost")

        assert window._video_panel.selected_mode() == "off"
        assert "device was lost" in window._video_panel.status.text()


class TestThePanelSaysWhatIsActuallyRunning:
    """*"Nothing seems to happen"* is a correct outcome as often as a fault.

    Super resolution is skipped whenever the output is no larger than the
    input -- which is exactly the case when the video panel happens to be the
    stream's own size. Without this the player selects something, the picture
    does not change, and nothing anywhere says why. It was in the OSD, which
    is off by default and nowhere near the control being questioned.
    """

    def _streaming(self, window, monkeypatch, code):
        decoder = FakeDecoder()
        from client.media import videofx

        decoder.last_path_code = code
        decoder.last_path = videofx.PATH_NAMES[code]
        window._video_decoder = decoder
        window._video_receiver = FakeReceiver()
        window._video_dismissed = True
        monkeypatch.setattr(
            window, "_pending_video_source", lambda: {"available": True})
        monkeypatch.setattr(window, "_pending_video_regions", lambda: [])
        return decoder

    def test_a_skipped_pass_says_so_and_says_why(self, window, qt_app, monkeypatch):
        from client.media import videofx

        window._config.video_upscaler = "rtx_vsr"
        self._streaming(window, monkeypatch, videofx.PATH_COPY)
        window._tick_video()

        text = window._video_panel.status.text()
        assert "Not enhancing" in text
        assert "Enlarge" in text, "the player is not told what to do about it"

    def test_a_running_pass_names_itself(self, window, qt_app, monkeypatch):
        from client.media import videofx

        window._config.video_upscaler = "fsr1"
        self._streaming(window, monkeypatch, videofx.PATH_EASU_RCAS)
        window._tick_video()

        assert "Running" in window._video_panel.status.text()
        assert "FSR" in window._video_panel.status.text()

    def test_rtx_vsr_says_it_is_running_and_why_that_cannot_be_confirmed(
            self, window, qt_app, monkeypatch):
        """**"Running: RTX VSR (requested)" reads as a contradiction.**

        The caveat is real -- no API reports whether the driver applied super
        resolution -- but a bare "(requested)" is taken to mean it did not
        happen, which was reported within a day of shipping. Underclaiming is
        as wrong as overclaiming; the GPU cost is the evidence and belongs
        beside it.
        """
        from client.media import videofx

        window._config.video_upscaler = "rtx_vsr"
        decoder = self._streaming(window, monkeypatch, videofx.PATH_VSR)
        decoder.last_gpu_ms = 0.26
        window._tick_video()

        text = window._video_panel.status.text()
        assert "Running" in text
        assert "requested" not in text, (
            "the word that made a working upscaler read as a failed one"
        )
        assert "0.26 ms" in text, "the evidence is the GPU cost; show it"
        assert "confirm" in text, "the limitation still has to be stated"

    def test_off_says_nothing(self, window, qt_app, monkeypatch):
        from client.media import videofx

        window._config.video_upscaler = "off"
        self._streaming(window, monkeypatch, videofx.PATH_COPY)
        window._tick_video()

        assert window._video_panel.status.text() == ""

    def test_it_is_written_once_not_ten_times_a_second(
            self, window, qt_app, monkeypatch):
        from client.media import videofx

        window._config.video_upscaler = "fsr1"
        self._streaming(window, monkeypatch, videofx.PATH_EASU_RCAS)
        window._tick_video()

        writes: list[str] = []
        monkeypatch.setattr(window._video_panel.status, "setText", writes.append)
        for _ in range(5):
            window._tick_video()

        assert writes == [], "the label is rewritten on every tick"

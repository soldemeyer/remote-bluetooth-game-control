"""The video server window.

Guards the same class of bug the client GUI tests exist for: widgets wired to
the wrong thing, settings that round-trip incorrectly, and a window that cannot
be built at all. Nothing here starts a real pipeline except where it says so.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="client GUI extras not installed")
pytest.importorskip("av", reason="video extras not installed")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from common.video import VideoSettings  # noqa: E402
from videoserver import config as video_config  # noqa: E402
from videoserver.config import VideoServerConfig  # noqa: E402
from videoserver.gui import VideoServerWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    """A window whose saves go nowhere near the real config file."""
    saved: list = []
    monkeypatch.setattr(video_config, "save", lambda cfg, path=None: saved.append(cfg))

    config = VideoServerConfig(
        password="window-test-password",
        name="capture-pc",
        discoverable=True,
        settings=VideoSettings(width=1280, height=720, fps=60, bitrate_kbps=8000),
    )
    win = VideoServerWindow(config)
    win.saved = saved
    yield win
    win.close()


class TestConstruction:
    def test_the_window_builds(self, window):
        assert window.windowTitle()
        assert window._start_button.text() == "Start streaming"

    def test_config_reaches_the_widgets(self, window):
        assert window._name.text() == "capture-pc"
        assert window._password.text() == "window-test-password"
        assert window._discoverable.isChecked() is True
        assert window._bitrate.value() == 8000
        assert window._resolution.currentData() == (1280, 720)
        assert window._fps.currentData() == 60

    def test_the_password_field_is_masked(self, qapp, monkeypatch):
        """It is a credential, and someone is usually watching a capture PC."""
        from PySide6.QtWidgets import QLineEdit

        monkeypatch.setattr(video_config, "save", lambda cfg, path=None: None)
        win = VideoServerWindow(VideoServerConfig(password="x" * 8))
        try:
            assert win._password.echoMode() == QLineEdit.EchoMode.Password
        finally:
            win.close()

    def test_the_encoder_list_offers_automatic_first(self, window):
        """Auto-detect is right for almost everyone; the list is the escape hatch."""
        assert window._encoder.itemData(0) == "auto"
        assert window._encoder.count() >= 2, "no encoders were detected at all"


class TestSettingsRoundTrip:
    def test_widget_changes_reach_the_settings(self, window):
        window._bitrate.setValue(4500)
        window._fps.setCurrentIndex(window._fps.findData(30))
        window._audio_enabled.setChecked(False)
        window._test_source.setChecked(True)

        settings = window._settings_from_ui()
        assert settings.bitrate_kbps == 4500
        assert settings.fps == 30
        assert settings.audio_enabled is False
        assert settings.test_source is True

    def test_saving_writes_through_the_config(self, window):
        window._name.setText("living-room")
        window._bitrate.setValue(3000)
        window._save_ui_into_config()

        assert window._config.name == "living-room"
        assert window._config.settings.bitrate_kbps == 3000
        assert window.saved, "nothing was persisted"

    def test_the_password_and_visibility_round_trip(self, window):
        window._password.setText("a-new-video-password")
        window._discoverable.setChecked(False)
        window._save_ui_into_config()

        assert window._config.password == "a-new-video-password"
        assert window._config.discoverable is False

    def test_settings_are_clamped_on_the_way_out(self, window):
        """The encoder must never be handed something it cannot open."""
        window._bitrate.setMaximum(99999)
        window._bitrate.setValue(99999)
        assert window._settings_from_ui().bitrate_kbps <= 50000


class TestGuards:
    def test_starting_without_a_password_is_refused(self, window, monkeypatch):
        warned: list = []
        monkeypatch.setattr(
            "videoserver.gui.QMessageBox.warning",
            lambda *args, **kwargs: warned.append(args),
        )
        window._password.setText("")
        window._start()

        assert warned, "it tried to start with no password"
        assert window._app is None

    def test_an_invalid_port_is_refused(self, window, monkeypatch):
        warned: list = []
        monkeypatch.setattr(
            "videoserver.gui.QMessageBox.warning",
            lambda *args, **kwargs: warned.append(args),
        )
        # Media and discovery are two sockets; one port cannot serve both.
        window._media_port.setValue(window._config.discovery_port)
        window._start()

        assert warned
        assert window._app is None

    def test_tick_is_harmless_before_anything_starts(self, window):
        window._tick()      # must not raise


class TestLivePipeline:
    def test_starting_and_stopping_a_real_pipeline(self, window, qapp):
        """The window drives the real thing, so build it once and tear it down."""
        window._test_source.setChecked(True)
        window._media_port.setValue(0)
        window._resolution.setCurrentIndex(0)    # 640x480, cheap
        window._start()

        try:
            assert window._app is not None
            assert window._app.is_running
            assert window._start_button.text() == "Stop streaming"

            # Poll the way the timer does; must not raise before frames exist.
            for _ in range(3):
                window._tick()
                qapp.processEvents()
        finally:
            window._stop()

        assert window._app is None
        assert window._start_button.text() == "Start streaming"

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
            "videoserver.gui.Notice.warning",
            lambda *args, **kwargs: warned.append(args),
        )
        window._password.setText("")
        window._start()

        assert warned, "it tried to start with no password"
        assert window._app is None

    def test_an_invalid_port_is_refused(self, window, monkeypatch):
        warned: list = []
        monkeypatch.setattr(
            "videoserver.gui.Notice.warning",
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


class TestThePlayerIdentificationPanel:
    """The debug view, which most people who open this window never turn on.

    Its whole value is answering "why is nobody being labelled", so the cases
    worth pinning are the ones where the answer is *nothing* -- an empty table
    under a confident heading is the failure it exists to replace.
    """

    class _FakeApp:
        def __init__(self, rows=(), judgements=(), stats=None):
            self._rows = list(rows)
            self._judgements = list(judgements)
            self._stats = stats or {}

        def player_rows(self):
            return self._rows

        def player_judgements(self):
            return self._judgements

        def player_id_stats(self):
            return self._stats

    def test_the_panel_is_hidden_while_nothing_is_identifying(self, window):
        """Hidden, not empty. A table captioned "Player identification" with
        no rows in it reads as a broken feature rather than an unused one."""
        window._update_players(self._FakeApp())

        # `isHidden`, not `isVisible`: a widget inside a window that was never
        # shown reports isVisible() False in every state, so the obvious
        # spelling here passes whatever the code does. Measured.
        assert window._players_group.isHidden()
        assert window._overlay_boxes == ()

    def test_it_appears_and_fills_once_identification_runs(self, window):
        from common.screen_regions import Rect
        from videoserver.playervision.types import (
            Judgement, SignalScore, TrackedPlayer,
        )

        rows = [
            TrackedPlayer(
                track_id=4, box=Rect(0.1, 0.1, 0.2, 0.3), player_id=2,
                confidence=0.92, region="upper_left", source="viewport",
            ),
            TrackedPlayer(track_id=9, box=Rect(0.6, 0.1, 0.2, 0.3)),
        ]
        judgements = [
            Judgement(
                track_id=4, player_id=2, confidence=0.92, source="viewport",
                region="upper_left",
                scores=(SignalScore("viewport", 2, 0.92, used=True),),
            ),
            Judgement(track_id=9, note="no player map: nobody is playing yet"),
        ]

        window._update_players(
            self._FakeApp(rows, judgements, {"backend": {"backend": "onnx"},
                                             "layout": "QUAD_4", "players": 2})
        )

        assert not window._players_group.isHidden()
        assert window._players_table.rowCount() == 2
        assert window._players_table.item(0, 0).text() == "Player 2"
        assert window._players_table.item(0, 2).text() == "viewport"
        assert window._players_table.item(0, 3).text() == "upper_left"
        # The nameless track is shown, carrying its reason rather than a blank.
        assert "no player map" in window._players_table.item(1, 2).text()
        assert "no player map" in window._players_detail.text()

    def test_the_summary_names_the_backend_and_the_counts(self, window):
        from common.screen_regions import Rect
        from videoserver.playervision.types import TrackedPlayer

        window._update_players(
            self._FakeApp(
                [TrackedPlayer(track_id=1, box=Rect(0, 0, 0.1, 0.1),
                               player_id=1, confidence=0.9, source="viewport")],
                [],
                {"backend": {"backend": "heuristic"}, "layout": "FULL",
                 "players": 1},
            )
        )

        text = window._players_summary.text()
        assert "heuristic" in text
        assert "1 tracked, 1 identified" in text

    def test_a_stopped_backend_is_said_plainly(self, window):
        """"no labels" and "the backend gave up" look identical otherwise."""
        window._update_players(
            self._FakeApp([], [], {"failed": "RuntimeError: out of memory",
                                   "backend": {"backend": "onnx"}})
        )

        assert "STOPPED" in window._players_summary.text()
        assert "out of memory" in window._players_summary.text()

    def test_painting_the_overlay_does_not_raise_without_a_picture(self, window):
        """The annotation may fail; the picture underneath must not."""
        from PySide6.QtGui import QPixmap

        from common.screen_regions import Rect
        from videoserver.playervision.types import TrackedPlayer

        window._update_players(
            self._FakeApp(
                [TrackedPlayer(track_id=1, box=Rect(0.1, 0.1, 0.2, 0.2),
                               player_id=1, confidence=0.9, source="viewport")],
                [],
                {"backend": {"backend": "heuristic"}},
            )
        )

        window._paint_overlay(QPixmap(320, 180))      # must not raise
        window._paint_overlay(QPixmap())              # null pixmap either


class TestThePopOutPreview:
    """A resizable window showing the same picture and the same overlay.

    Note `isVisible()` is trustworthy here where it was not for the panel
    above: this is a *top-level* window, so it tracks show and close offscreen
    rather than reporting False for a child of a window nobody showed.
    Verified before these were written.
    """

    def test_it_opens_on_the_button_and_not_before(self, window):
        assert window._preview_window is None

        window._toggle_preview_window()

        assert window._preview_window is not None
        assert window._preview_window.isVisible()
        assert "Close" in window._popout_button.text()

    def test_the_button_closes_it_again(self, window):
        window._toggle_preview_window()
        window._toggle_preview_window()

        assert not window._preview_window.isVisible()
        assert "Open" in window._popout_button.text()

    def test_closing_the_window_itself_is_noticed(self, window):
        """The operator will use the frame's own close button, not ours.

        Without this the button would still read "Close preview window" over a
        window that is already gone, and the next press would close nothing.
        """
        window._toggle_preview_window()

        window._preview_window.close()

        assert "Open" in window._popout_button.text()

    def test_reopening_reuses_the_window_it_already_built(self, window):
        """So it keeps the size and position the operator gave it."""
        window._toggle_preview_window()
        first = window._preview_window
        window._toggle_preview_window()
        window._toggle_preview_window()

        assert window._preview_window is first

    def test_nothing_opens_it_by_itself(self, window):
        """The client's video window had to grow a "dismissed" flag because
        its tick reopened it the moment it was closed. This one is opened only
        by the button, so there is no such state to get wrong."""
        window._toggle_preview_window()
        window._preview_window.close()

        for _ in range(5):
            window._tick()
            window._tick_preview()

        assert not window._preview_window.isVisible()


class TestThePreviewEncodeWidth:
    """How wide the picture is encoded, for the surfaces on screen.

    Pure arithmetic, and the part worth pinning: asking the encoder for a new
    size rebuilds its codec context, so a window being dragged would otherwise
    build a fresh MJPEG encoder every frame.
    """

    def test_the_inline_thumbnail_alone_keeps_the_original_width(self, window):
        from videoserver.gui import PREVIEW_WIDTH_LOCAL

        surfaces = [(_Size(320, 180), None)]

        assert window._wanted_preview_width(surfaces) == PREVIEW_WIDTH_LOCAL

    def test_a_larger_surface_raises_it(self, window):
        from videoserver.gui import PREVIEW_WIDTH_LOCAL

        wide = window._wanted_preview_width([(_Size(1600, 900), None)])

        assert wide > PREVIEW_WIDTH_LOCAL

    def test_it_is_quantised_so_a_drag_does_not_rebuild_the_encoder(self, window):
        """A drag changes width every frame; the encoder must not follow it.

        Not "one size for any 160 pixels" -- rounding up means some 160-wide
        span always straddles a boundary -- but that the *count* is bounded by
        the step rather than by the drag, which is what stops a fresh MJPEG
        context per mouse movement.
        """
        from videoserver.gui import PREVIEW_WIDTH_STEP

        span = 640
        widths = {
            window._wanted_preview_width([(_Size(w, 500), None)])
            for w in range(1000, 1000 + span)
        }

        assert len(widths) <= span // PREVIEW_WIDTH_STEP + 1
        assert len(widths) < 10, "the encoder would be rebuilt during a drag"

    def test_it_is_capped(self, window):
        from videoserver.gui import PREVIEW_WIDTH_MAX

        assert window._wanted_preview_width(
            [(_Size(9000, 5000), None)]
        ) == PREVIEW_WIDTH_MAX

    def test_the_widest_surface_wins(self, window):
        """Both can be open at once, and the thumbnail must not drag the
        pop-out's picture back down to a thumbnail's width."""
        mixed = window._wanted_preview_width(
            [(_Size(320, 180), None), (_Size(1600, 900), None)]
        )

        assert mixed == window._wanted_preview_width([(_Size(1600, 900), None)])


class _Size:
    """The two methods `_wanted_preview_width` asks of a QSize."""

    def __init__(self, width, height):
        self._w, self._h = width, height

    def width(self):
        return self._w

    def height(self):
        return self._h

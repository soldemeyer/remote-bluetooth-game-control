"""Reopening capture when a device has not let go yet.

A settings change that alters the capture format has to close the device and
open it again. The two devices release at their own pace, and audio is
routinely still held when video has already gone -- so this is the ordinary
path on a resolution change, not a rare one.

The rule, for both: a capture that has not let go is **kept, never replaced**.
Assigning over it orphans a thread that still holds the device, so the
replacement cannot open it either, and nothing is left holding a reference to
the one that must be stopped first. Whichever half was skipped is retried from
the governor tick.
"""

from __future__ import annotations

import pytest

pytest.importorskip("av", reason="video extras not installed")

from common.video import VideoSettings  # noqa: E402
from videoserver.pipeline import VideoServerApp  # noqa: E402


class FakeCapture:
    """A capture that can be told whether it has released its device."""

    def __init__(self, running: bool = True) -> None:
        self._running = running
        self.started = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        self.started = True
        self._running = True

    def release(self) -> None:
        self._running = False


def _app(**overrides) -> VideoServerApp:
    """An app with nothing started -- only the restart bookkeeping is exercised."""
    app = VideoServerApp.__new__(VideoServerApp)
    import threading

    app._media_lock = threading.RLock()
    app._lock = threading.Lock()
    app._errors = []
    app._running = True
    app._capture = None
    app._audio_capture = None
    app._encoder = None
    app._audio_encoder = None
    app.settings = VideoSettings(audio_enabled=True, **overrides)
    return app


class TestAudioIsNotReplacedWhileItHoldsTheDevice:
    def test_a_held_audio_capture_is_kept(self):
        app = _app()
        held = FakeCapture(running=True)
        app._audio_capture = held

        with app._media_lock:
            app._start_audio_locked()

        assert app._audio_capture is held, (
            "assigned over a capture that still holds the microphone; the "
            "thread that must be stopped first is now unreachable"
        )

    def test_the_operator_is_told_why(self):
        app = _app()
        app._audio_capture = FakeCapture(running=True)

        with app._media_lock:
            app._start_audio_locked()

        assert any("audio" in message.lower() for message in app._errors)

    def test_audio_disabled_starts_nothing(self):
        app = _app()
        app.settings = VideoSettings(audio_enabled=False)

        with app._media_lock:
            app._start_audio_locked()

        assert app._audio_capture is None


class TestAudioRecoversOnItsOwn:
    """Video recovery only watches video, so audio needs its own pass."""

    def test_it_retries_once_the_device_is_released(self, monkeypatch):
        app = _app()
        app._capture = FakeCapture(running=True)      # video is fine
        stuck = FakeCapture(running=True)
        app._audio_capture = stuck

        started: list[str] = []
        monkeypatch.setattr(
            VideoServerApp, "_start_audio_locked",
            lambda self: started.append("audio"),
        )

        app._recover_audio_if_needed()
        assert started == [], "restarted audio while the device was still held"

        stuck.release()
        app._recover_audio_if_needed()
        assert started == ["audio"], (
            "audio was never retried; a resolution change would leave the "
            "stream permanently silent while the picture looked fine"
        )

    def test_it_leaves_a_running_capture_alone(self, monkeypatch):
        app = _app()
        app._capture = FakeCapture(running=True)
        app._audio_capture = FakeCapture(running=True)

        started: list[str] = []
        monkeypatch.setattr(
            VideoServerApp, "_start_audio_locked",
            lambda self: started.append("audio"),
        )

        app._recover_audio_if_needed()
        assert started == []

    def test_it_defers_while_video_is_also_down(self, monkeypatch):
        """Video's own recovery starts both; two starters would race."""
        app = _app()
        app._capture = None
        app._audio_capture = None

        started: list[str] = []
        monkeypatch.setattr(
            VideoServerApp, "_start_audio_locked",
            lambda self: started.append("audio"),
        )

        app._recover_audio_if_needed()
        assert started == []

    def test_nothing_happens_when_audio_is_off(self, monkeypatch):
        app = _app()
        app.settings = VideoSettings(audio_enabled=False)
        app._capture = FakeCapture(running=True)

        started: list[str] = []
        monkeypatch.setattr(
            VideoServerApp, "_start_audio_locked",
            lambda self: started.append("audio"),
        )

        app._recover_audio_if_needed()
        assert started == []

    def test_a_stopped_server_does_not_restart_anything(self, monkeypatch):
        app = _app()
        app._running = False
        app._capture = FakeCapture(running=True)

        started: list[str] = []
        monkeypatch.setattr(
            VideoServerApp, "_start_audio_locked",
            lambda self: started.append("audio"),
        )

        app._recover_audio_if_needed()
        assert started == []

"""Output volume on the client, and the capture level meter on the server.

Both exist for the same reason: audio is the one part of the pipeline where
"working" and "broken" produce identical readings everywhere else. The meter
answers *is sound being captured*, and the volume control answers *why can I
not hear it* -- neither of which any packet counter can.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class RecordingSink:
    """A sink that supports volume, as QAudioSink does."""

    def __init__(self) -> None:
        self.gain: float | None = None

    def bytes_free(self) -> int:
        return 0

    def write(self, data: bytes) -> None:
        pass

    def set_volume(self, gain: float) -> None:
        self.gain = gain

    def close(self) -> None:
        pass


class DumbSink:
    """A sink with no volume support at all."""

    def bytes_free(self) -> int:
        return 0

    def write(self, data: bytes) -> None:
        pass


class TestClientVolume:
    def test_the_level_reaches_the_sink(self):
        from client.media.audio import AudioPlayout

        sink = RecordingSink()
        playout = AudioPlayout(sink=sink)

        playout.set_volume(50)

        assert sink.gain is not None
        assert 0.0 < sink.gain < 1.0

    def test_full_volume_is_unity_gain(self):
        from client.media.audio import AudioPlayout

        sink = RecordingSink()
        AudioPlayout(sink=sink).set_volume(100)
        assert sink.gain == pytest.approx(1.0)

    def test_the_curve_is_perceptual_not_linear(self):
        """A linear control does nothing over its top half."""
        from client.media.audio import _perceptual

        assert _perceptual(50) == pytest.approx(0.25)
        assert _perceptual(50) < 0.5, "half travel should be well below half gain"

    def test_muting_silences_without_losing_the_level(self):
        from client.media.audio import AudioPlayout

        sink = RecordingSink()
        playout = AudioPlayout(sink=sink)
        playout.set_volume(70)

        playout.set_muted(True)
        assert sink.gain == 0.0
        assert playout.volume == 70, "mute overwrote the chosen level"

        playout.set_muted(False)
        assert sink.gain > 0.0

    def test_the_level_is_remembered_until_a_device_exists(self):
        """The sink is built at start(), after the config has been read."""
        from client.media.audio import AudioPlayout

        playout = AudioPlayout(sink=None, volume=40)
        assert playout.volume == 40      # no device, no error

        sink = RecordingSink()
        playout._sink = sink
        playout._apply_volume()
        assert sink.gain == pytest.approx(0.16)

    def test_a_sink_without_volume_is_not_an_error(self):
        from client.media.audio import AudioPlayout

        playout = AudioPlayout(sink=DumbSink())
        playout.set_volume(50)           # must not raise
        assert playout.volume == 50

    def test_the_range_is_clamped(self):
        from client.media.audio import AudioPlayout

        playout = AudioPlayout(sink=RecordingSink())
        playout.set_volume(500)
        assert playout.volume == 100
        playout.set_volume(-20)
        assert playout.volume == 0


class TestTheClientRemembersIt:
    def test_the_fields_exist_and_default_sensibly(self):
        from client.config import ClientConfig

        cfg = ClientConfig()
        assert cfg.video_volume == 100
        assert cfg.video_muted is False

    def test_they_survive_a_save_and_load(self, tmp_path):
        from client import config as client_config

        path = tmp_path / "client.json"
        cfg = client_config.ClientConfig(video_volume=35, video_muted=True)
        client_config.save(cfg, path)

        loaded = client_config.load(path)
        assert loaded.video_volume == 35
        assert loaded.video_muted is True


class TestTheServerLevelMeter:
    def test_a_silent_frame_reads_zero(self):
        pytest.importorskip("av", reason="video extras not installed")
        import av

        from common.video import VideoSettings
        from videoserver.encode import AudioEncoder

        encoder = AudioEncoder(VideoSettings(), None, on_packet=lambda *a: None)
        frame = av.AudioFrame(format="s16", layout="stereo", samples=480)
        for plane in frame.planes:
            plane.update(b"\x00" * plane.buffer_size)

        encoder._measure_level(frame)
        assert encoder.level_rms == 0.0

    def test_a_loud_frame_reads_high(self):
        pytest.importorskip("av", reason="video extras not installed")
        import av

        from common.video import VideoSettings
        from videoserver.encode import AudioEncoder

        encoder = AudioEncoder(VideoSettings(), None, on_packet=lambda *a: None)
        frame = av.AudioFrame(format="s16", layout="stereo", samples=480)
        # Alternating +/- near full scale.
        loud = b"\x00\x40" * (480 * 2)
        for plane in frame.planes:
            plane.update(loud[: plane.buffer_size])

        encoder._measure_level(frame)
        assert encoder.level_peak > 0.4
        assert encoder.level_rms > 0.4

    def test_the_level_is_reported_in_the_snapshot(self):
        pytest.importorskip("av", reason="video extras not installed")

        from common.video import VideoSettings
        from videoserver.encode import AudioEncoder

        encoder = AudioEncoder(VideoSettings(), None, on_packet=lambda *a: None)
        snapshot = encoder.snapshot()

        assert "level_peak" in snapshot
        assert "level_rms" in snapshot
        assert snapshot["level_fresh"] is False, (
            "nothing has been measured, so the meter must not claim a reading"
        )

    def test_staleness_is_told_apart_from_silence(self):
        """A muted input and a dead capture both read zero; only one is a fault."""
        pytest.importorskip("av", reason="video extras not installed")
        import av

        from common.timing import now_ns
        from common.video import VideoSettings
        from videoserver.encode import AudioEncoder

        encoder = AudioEncoder(VideoSettings(), None, on_packet=lambda *a: None)
        frame = av.AudioFrame(format="s16", layout="stereo", samples=480)
        for plane in frame.planes:
            plane.update(b"\x00" * plane.buffer_size)

        encoder._measure_level(frame)
        assert encoder.snapshot()["level_fresh"] is True, "silence is still a reading"

        encoder.level_ns = now_ns() - 5_000_000_000
        assert encoder.snapshot()["level_fresh"] is False


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6", reason="GUI extras not installed")
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


class TestTheMeterWidget:

    def test_it_paints_at_every_level(self, qt_app):
        from videoserver.levelmeter import LevelMeter

        meter = LevelMeter()
        meter.resize(200, 18)
        for rms, peak in ((0.0, 0.0), (0.3, 0.5), (0.95, 1.0)):
            meter.set_level(rms, peak)
            meter.repaint()          # would raise if the painting is wrong

    def test_the_peak_marker_holds_then_decays(self, qt_app):
        from videoserver.levelmeter import LevelMeter

        meter = LevelMeter()
        meter.set_level(0.8, 0.9)
        held = meter._peak
        assert held == pytest.approx(0.9)

        meter.set_level(0.1, 0.1)
        assert meter._peak < held, "the peak never fell"
        assert meter._peak > 0.1, "the peak fell straight to the new value"

    def test_no_audio_is_a_distinct_state(self, qt_app):
        from videoserver.levelmeter import LevelMeter

        meter = LevelMeter()
        meter.set_level(0.0, 0.0, live=False)
        meter.resize(200, 18)
        meter.repaint()
        assert meter._live is False

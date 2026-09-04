"""The video server's pipeline strip.

Its whole job is answering *where has it stopped?*, so what is pinned here is
which stage lights up for which fault -- and, just as important, which stages
decline to claim health when nothing is reaching them.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="video extras not installed")

from PySide6.QtWidgets import QApplication          # noqa: E402

from qtui.theme import apply_theme                  # noqa: E402
from videoserver.pipeline_strip import PipelineStrip  # noqa: E402


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    apply_theme(instance, "amber")
    return instance


def _status(**over):
    base = dict(
        streaming=True, width=1920, height=1080, fps=60.0, encoder="h264_nvenc",
        encode_p50_ms=0.9, encode_p99_ms=2.1, bitrate_kbps=8000, dropped=0, clients=2,
    )
    base.update(over)
    return base


def _tokens(app, **over):
    strip = PipelineStrip()
    strip.update_from(_status(**over), streaming=True)
    return {key: card._token for key, card in strip.cards.items()}


def _values(app, **over):
    strip = PipelineStrip()
    strip.update_from(_status(**over), streaming=True)
    return {key: card._value.text() for key, card in strip.cards.items()}


class TestHealthySteadyState:
    def test_every_stage_reads_healthy(self, app):
        assert set(_tokens(app).values()) == {"success"}


class TestEachFaultLightsItsOwnStage:
    def test_a_slow_encoder_is_the_encoder(self, app):
        tokens = _tokens(app, encoder="libx264", encode_p50_ms=12.0, encode_p99_ms=21.5)
        assert tokens["encode"] == "error"
        assert tokens["capture"] == "success"

    def test_dropped_packets_are_the_network(self, app):
        tokens = _tokens(app, dropped=412)
        assert tokens["network"] == "warning"
        assert tokens["encode"] == "success"

    def test_a_device_that_sends_nothing_is_capture(self, app):
        tokens = _tokens(app, width=0, height=0, fps=0.0, bitrate_kbps=0)
        assert tokens["capture"] == "error"


class TestIdleStagesDoNotClaimHealth:
    """The confidently-wrong display this window exists to replace.

    A capture card that opens and sends nothing left encode showing a green
    "0.0 ms" and network a green "0.0 Mbps" -- three stages reporting fine
    while the picture was dead.
    """

    def test_encode_and_network_wait_rather_than_pass(self, app):
        tokens = _tokens(app, width=0, height=0, fps=0.0,
                         encode_p50_ms=0.0, encode_p99_ms=0.0, bitrate_kbps=0)
        assert tokens["encode"] != "success"
        assert tokens["network"] != "success"

    def test_they_say_so_in_words_too(self, app):
        """Colour is never the only signal."""
        values = _values(app, width=0, height=0, fps=0.0,
                         encode_p50_ms=0.0, encode_p99_ms=0.0, bitrate_kbps=0)
        assert values["encode"] == "waiting"
        assert values["network"] == "waiting"
        assert values["capture"] == "no signal"


class TestNotStreaming:
    def test_all_four_say_so(self, app):
        strip = PipelineStrip()
        strip.update_from(None, streaming=False)
        assert {card._value.text() for card in strip.cards.values()} == {"—"}
        assert {card._token for card in strip.cards.values()} == {"text-muted"}


class TestNobodyWatchingIsNotAFault:
    def test_zero_viewers_is_muted_not_red(self, app):
        """A source with nobody watching is doing its job."""
        assert _tokens(app, clients=0)["viewers"] == "text-muted"

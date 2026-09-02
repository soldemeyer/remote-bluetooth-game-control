"""Phase 8: congestion seen as delay, before it becomes loss.

`tick_governor` reacted only to loss, on 5-second windows. **Loss is a lagging
indicator**: by the time a router drops, its queue has already been full for
some time and every packet crossing it has been paying that delay. A rising
one-way delay is the same event seen earlier, and it is the only signal
available before the damage is done.

These tests use the real report codec and the real governor. The one-way delay
itself is measured in `VideoReceiver`; what is exercised here is the half that
decides what to do about it, plus the wire format that carries it.
"""

from __future__ import annotations

from common.video import (
    MEDIA_REPORT_SIZE,
    decode_media_report,
    encode_media_report_into,
)


def _report(queue_ms: float = 0.0, **kwargs) -> dict:
    buf = bytearray(128)
    size = encode_media_report_into(
        buf, 0,
        kwargs.get("frames_complete", 1000),
        kwargs.get("frames_dropped", 0),
        kwargs.get("slices_received", 10_000),
        kwargs.get("slices_lost", 0),
        4.0, 20.0, 30.0, 0,
        pickup_p50_ms=0.5,
        paint_p50_ms=1.5,
        queue_ms=queue_ms,
    )
    return decode_media_report(bytes(buf[:size]), 0)


def _tick(app) -> None:
    """Run one governor pass, defeating both rate limits.

    The delay check and the loss check run on different clocks now -- the
    first at 1 Hz so it can act before a bloated buffer fills, the second at
    5 s. A test that reset only one of them would silently exercise half the
    governor.
    """
    app._last_governor_ns = 0
    app._last_queue_ns = 0
    app.tick_governor()


class TestTheWireCarriesTheQueue:
    def test_it_round_trips(self):
        assert _report(queue_ms=42.5)["queue_ms"] == 42.5

    def test_a_peer_that_cannot_report_is_not_read_as_zero(self):
        """Absent must be distinguishable from "no queue".

        Treating an older client's silence as an idle path would let one stale
        peer mask a real queue on another.
        """
        buf = bytearray(128)
        encode_media_report_into(buf, 0, 10, 0, 100, 0, 1.0, 2.0, 3.0, 0)
        old = decode_media_report(bytes(buf[:MEDIA_REPORT_SIZE]), 0)
        assert old["queue_reported"] is False
        assert old["queue_ms"] == 0.0

        assert _report(queue_ms=0.0)["queue_reported"] is True

    def test_the_older_paint_block_still_decodes(self):
        """Each optional field is taken only if its bytes are there.

        An all-or-nothing extension would have made a two-field sender lose
        both fields as soon as a third was defined.
        """
        buf = bytearray(128)
        encode_media_report_into(
            buf, 0, 10, 0, 100, 0, 1.0, 2.0, 3.0, 0,
            pickup_p50_ms=0.4, paint_p50_ms=1.3, queue_ms=9.9,
        )
        # Truncate to base + two of the three optional fields.
        truncated = bytes(buf[: MEDIA_REPORT_SIZE + 4])
        report = decode_media_report(truncated, 0)
        assert report["present_reported"] is True
        assert report["paint_p50_ms"] == 1.3
        assert report["queue_reported"] is False


class TestTheGovernorActsOnDelayBeforeLoss:
    def _app(self, monkeypatch, reports):
        from common.video import VideoSettings
        from videoserver.config import VideoServerConfig
        from videoserver.pipeline import VideoServerApp

        settings = VideoSettings(width=640, height=480, fps=30, bitrate_kbps=8000)
        app = VideoServerApp.__new__(VideoServerApp)   # no sockets, no capture
        app.settings = settings
        app.config = VideoServerConfig(settings=settings)
        # One call, so a new governor field does not silently break this.
        app._init_governor()
        app._running = False           # so _apply_bitrate does not restart media
        app._lock = __import__("threading").Lock()
        app._capture = None
        app._encoder = None
        app._audio_capture = None
        app._audio_encoder = None

        class _Net:
            """Only what the governor touches -- and all of it.

            A stub missing a method the governor calls fails as an
            AttributeError deep inside a tick, which reads as a broken test
            rather than as an incomplete double.
            """

            fec_enabled = False

            def client_snapshot(self_inner):
                return reports

            def set_fec(self_inner, enabled):
                changed = bool(enabled) != self_inner.fec_enabled
                self_inner.fec_enabled = bool(enabled)
                return changed

        app.net = _Net()
        return app

    def test_a_standing_queue_reduces_the_bitrate(self, monkeypatch):
        reports = [{"report": {"queue_ms": 120.0, "queue_reported": True,
                               "slices_received": 10_000, "slices_lost": 0}}]
        app = self._app(monkeypatch, reports)

        # Two checks: the first only confirms, the second acts.
        _tick(app)
        first = app._active_bitrate
        _tick(app)

        assert first == 8000, "one reading must not be enough"
        assert app._active_bitrate < 8000, "a confirmed queue must reduce bitrate"

    def test_it_acts_with_no_loss_at_all(self):
        """The whole point: this fires while the path is still lossless."""
        reports = [{"report": {"queue_ms": 200.0, "queue_reported": True,
                               "slices_received": 50_000, "slices_lost": 0}}]
        app = self._app(None, reports)
        for _ in range(3):
            _tick(app)
        assert app._active_bitrate < 8000

    def test_a_quiet_path_is_left_alone(self):
        reports = [{"report": {"queue_ms": 2.0, "queue_reported": True,
                               "slices_received": 10_000, "slices_lost": 0}}]
        app = self._app(None, reports)
        for _ in range(5):
            _tick(app)
        assert app._active_bitrate == 8000

    def test_jitter_below_the_threshold_does_not_trip_it(self):
        """Measured WiFi jitter was p99 5.93 ms, p99.9 18.09 ms.

        A threshold that ordinary variation can reach would throw away bitrate
        on a healthy path, which is worse than reacting late.
        """
        reports = [{"report": {"queue_ms": 18.0, "queue_reported": True,
                               "slices_received": 10_000, "slices_lost": 0}}]
        app = self._app(None, reports)
        for _ in range(5):
            _tick(app)
        assert app._active_bitrate == 8000

    def test_a_client_that_cannot_report_does_not_mask_one_that_can(self):
        reports = [
            {"report": {"slices_received": 10_000, "slices_lost": 0}},   # older peer
            {"report": {"queue_ms": 150.0, "queue_reported": True,
                        "slices_received": 10_000, "slices_lost": 0}},
        ]
        app = self._app(None, reports)
        for _ in range(3):
            _tick(app)
        assert app._active_bitrate < 8000

    def test_the_queue_must_persist_not_merely_spike(self):
        """One unlucky window is not a standing queue."""
        app = self._app(None, [{"report": {"queue_ms": 300.0, "queue_reported": True,
                                           "slices_received": 10_000, "slices_lost": 0}}])
        _tick(app)                                # check 1: confirm only
        assert app._active_bitrate == 8000

        # The queue clears before the second tick.
        app.net.client_snapshot = lambda: [
            {"report": {"queue_ms": 1.0, "queue_reported": True,
                        "slices_received": 10_000, "slices_lost": 0}}
        ]
        _tick(app)
        assert app._active_bitrate == 8000, "a spike must not reduce bitrate"


class TestLossIsMeasuredOverAWindowNotALifetime:
    """The counters clients report are lifetime totals, so they must be differenced.

    Dividing them gives the loss rate *since the session began*, which is not a
    control signal: once a session has had one bad patch the ratio stays
    elevated for good. Measured directly on hardware -- the parity slice
    switched on correctly when loss appeared and was **still on twenty-five
    seconds after the path was completely clean**, because the lifetime average
    could never fall back. The same reading feeds the bitrate governor, which
    would equally hold a reduced bitrate on a path that had long since
    recovered.
    """

    def _app(self, reports_box):
        from common.video import VideoSettings
        from videoserver.config import VideoServerConfig
        from videoserver.pipeline import VideoServerApp

        settings = VideoSettings(width=640, height=480, fps=30, bitrate_kbps=8000)
        app = VideoServerApp.__new__(VideoServerApp)
        app.settings = settings
        app.config = VideoServerConfig(settings=settings)
        app._init_governor()
        app._running = False

        class _Net:
            fec_enabled = False

            def client_snapshot(self_inner):
                return reports_box[0]

            def set_fec(self_inner, enabled):
                changed = bool(enabled) != self_inner.fec_enabled
                self_inner.fec_enabled = bool(enabled)
                return changed

        app.net = _Net()
        return app

    @staticmethod
    def _entry(received: int, lost: int):
        return [{"client_id": "a",
                 "report": {"slices_received": received, "slices_lost": lost}}]

    def test_a_clean_window_reads_clean_after_a_bad_one(self):
        box = [self._entry(0, 0)]
        app = self._app(box)

        box[0] = self._entry(1000, 100)        # a 10% window
        app._sample_loss()
        box[0] = self._entry(2000, 200)
        assert app._sample_loss() > 0.05, "the bad window must register"

        # Path recovers: lifetime is still 200/2000 = 10%, this window is 0.
        box[0] = self._entry(12_000, 200)
        assert app._sample_loss() == 0.0, "a lifetime average would still read 10%"

    def test_parity_switches_off_once_the_path_clears(self):
        box = [self._entry(0, 0)]
        app = self._app(box)

        received, lost = 0, 0
        for _ in range(4):                      # lossy
            received += 2000; lost += 60
            box[0] = self._entry(received, lost)
            app._last_queue_ns = 0
            app.tick_governor()
        assert app.net.fec_enabled, "parity should be on while the path is losing"

        for _ in range(40):                     # clean, well past the hysteresis
            received += 2000
            box[0] = self._entry(received, lost)
            app._last_queue_ns = 0
            app.tick_governor()
        assert not app.net.fec_enabled, "parity must be dropped once loss stops"

    def test_a_reconnected_client_does_not_produce_a_negative_rate(self):
        box = [self._entry(50_000, 500)]
        app = self._app(box)
        app._sample_loss()
        box[0] = self._entry(10, 0)             # counters restarted
        assert app._sample_loss() == 0.0

    def test_departed_clients_do_not_accumulate(self):
        box = [self._entry(1000, 0)]
        app = self._app(box)
        app._sample_loss()
        assert "a" in app._loss_prev
        box[0] = []
        app._sample_loss()
        assert app._loss_prev == {}, "state for a gone client must be released"

"""Phase 23: does the system come back, or does the damage stick?

Every optimization here trades margin for latency, and the way that goes wrong
is not a crash. It is a jitter buffer that grows under load and stays grown, a
bitrate that drops in seconds and returns over minutes, a stream that stalls and
never asks for the frame that would restart it. All of those look healthy on
every counter and feel broken to play.

So each test asserts on the *third* measurement: not that the system survived
the fault, but that it returned to where it started.
"""

from __future__ import annotations

import time

from client.net.video import VideoStreamState
from common.timing import now_ns


# --------------------------------------------------------------------------
# Bitrate
# --------------------------------------------------------------------------


def _governor(bitrate: int = 6000, reports=None):
    from common.video import VideoSettings
    from videoserver.config import VideoServerConfig
    from videoserver.pipeline import VideoServerApp

    settings = VideoSettings(width=1280, height=720, fps=60, bitrate_kbps=bitrate)
    app = VideoServerApp.__new__(VideoServerApp)
    app.settings = settings
    app.config = VideoServerConfig(settings=settings)
    app._init_governor()
    app._running = False

    box = reports if reports is not None else [[]]

    class _Net:
        fec_enabled = False

        def client_snapshot(self_inner):
            return box[0]

        def set_fec(self_inner, enabled):
            changed = bool(enabled) != self_inner.fec_enabled
            self_inner.fec_enabled = bool(enabled)
            return changed

    app.net = _Net()
    return app, box


def _clean_report(received: int):
    return [{"client_id": "a", "report": {
        "slices_received": received, "slices_lost": 0,
        "queue_ms": 0.0, "queue_reported": True}}]


class TestBitrateComesBackInSecondsNotMinutes:
    """Down fast is right; up slowly is not the same thing as up carefully.

    Measured on hardware before this was changed: after congestion cleared, the
    bitrate was still at 2256 of 6000 kbps **three minutes later**, because the
    ramp was +10% every 30 seconds. A thirty-second problem cost eight minutes
    of degraded picture.
    """

    def test_it_climbs_back_from_the_floor_promptly(self):
        """Counted in *steps*, then priced at the real interval.

        Wall time cannot be used: the governor's own rate limits are what is
        under test, so a test that waited them out would take a minute. Each
        iteration here is one recovery opportunity, and the time it represents
        is `_RECOVERY_INTERVAL_NS`.
        """
        from videoserver.pipeline import _RECOVERY_INTERVAL_NS

        app, box = _governor(6000)
        app._active_bitrate = 1000            # as if congestion had bottomed it out

        received = 0
        steps = 0
        for _ in range(200):
            received += 5000
            box[0] = _clean_report(received)
            app._last_governor_ns = 0
            app._last_queue_ns = 0
            app._last_recovery_ns = 0         # one recovery opportunity per pass
            app.tick_governor()
            steps += 1
            if app._active_bitrate >= 6000:
                break

        seconds = steps * _RECOVERY_INTERVAL_NS / 1e9
        assert app._active_bitrate >= 6000, (
            f"only reached {app._active_bitrate} kbps after {steps} steps"
        )
        assert seconds <= 45, (
            f"{steps} steps at {_RECOVERY_INTERVAL_NS / 1e9:.0f}s each = {seconds:.0f}s"
        )

    def test_it_never_climbs_past_what_was_configured(self):
        app, box = _governor(6000)
        app._active_bitrate = 5500
        received = 0
        for _ in range(40):
            received += 5000
            box[0] = _clean_report(received)
            app._last_governor_ns = 0
            app._last_queue_ns = 0
            app._last_recovery_ns = 0
            app.tick_governor()
        assert app._active_bitrate == 6000

    def test_a_queue_stops_the_ramp(self):
        """Recovery must not fight the thing that just reduced the bitrate.

        The hold-off is `_last_recovery_ns`, which the queue path sets every
        time it acts -- so unlike the tests above, this one must **not** reset
        it. Doing so defeats the exact mechanism under test, and the first
        version of this test did, and duly watched the bitrate climb.
        """
        app, box = _governor(6000)
        app._active_bitrate = 2000
        app._last_recovery_ns = now_ns()      # as if it had just been reduced

        received = 0
        for _ in range(20):
            received += 5000
            box[0] = [{"client_id": "a", "report": {
                "slices_received": received, "slices_lost": 0,
                "queue_ms": 300.0, "queue_reported": True}}]
            app._last_governor_ns = 0
            app._last_queue_ns = 0
            app.tick_governor()

        assert app._active_bitrate <= 2000, "it climbed while the path was congested"


# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------


def _playout(target_ms: int = 30):
    from client.media.audio import AudioPlayout

    class _Sink:
        def bytes_free(self):
            return 0

        def bytes_queued(self):
            return 0

        def write(self, data):
            pass

    return AudioPlayout(sink=_Sink(), target_ms=target_ms)


def _settle(playout, spread_ms: float, ticks: int) -> float:
    from client.media.audio import _GOVERNOR_INTERVAL_NS

    for _ in range(ticks):
        for index in range(64):
            playout._transit.add(10.0 + (index % 2) * spread_ms)
        playout._last_governor_ns -= _GOVERNOR_INTERVAL_NS * 2
        playout.tick_sync()
    return playout.target_ms


class TestTheAudioBufferGivesBackWhatItTook:
    def test_it_grows_under_jitter_and_shrinks_afterwards(self):
        playout = _playout(30)
        clean = _settle(playout, 0.5, 40)
        jittery = _settle(playout, 45.0, 40)
        recovered = _settle(playout, 0.5, 120)

        assert jittery > clean, "the buffer must absorb jitter"
        assert recovered <= clean, (
            f"the buffer kept {recovered - clean:.0f} ms after the jitter cleared"
        )

    def test_repeated_bursts_do_not_ratchet_it_upward(self):
        """Each burst must be given back, or a long session only ever grows.

        This is the failure that hides: the audio is perfect throughout, and
        the only symptom is that it is further and further behind.
        """
        playout = _playout(30)
        baseline = _settle(playout, 0.5, 40)
        for _ in range(4):
            _settle(playout, 40.0, 20)
            _settle(playout, 0.5, 100)
        assert playout.target_ms <= baseline


# --------------------------------------------------------------------------
# Video stream liveness
# --------------------------------------------------------------------------


class TestTheVideoStreamRecoversFromAStall:
    """Measured on hardware, recovery from a total blackout:

        blackout   state during   first frame after
          1.0 s     STREAMING          10 ms
          2.0 s     STREAMING          21 ms
          4.0 s     STALLED            20 ms
          7.0 s     FAILED             never

    Under the give-up threshold it is essentially instant. Past it the receiver
    stops for good and recovery becomes the client's job -- so what matters is
    that the stall path asks for the keyframe that restarts the picture, and
    that it goes back to STREAMING by itself.
    """

    def _receiver(self):
        from client.net.video import VideoReceiver

        receiver = VideoReceiver("password", client_name="recovery")
        receiver._started_ns = now_ns()
        receiver._last_media_ns = now_ns()
        receiver._set_state(VideoStreamState.STREAMING, "direct")
        return receiver

    def test_a_stall_asks_for_a_keyframe(self):
        receiver = self._receiver()
        receiver._last_media_ns = now_ns() - 4_000_000_000     # 4 s of silence

        sent: list[int] = []
        receiver._request_idr = lambda reason, force=False: sent.append(reason)

        assert receiver._check_liveness() is False, "4 s must not be fatal"
        assert receiver.state is VideoStreamState.STALLED
        assert sent, "a stalled stream must ask for a keyframe"

    def test_it_returns_to_streaming_on_its_own(self):
        receiver = self._receiver()
        receiver._last_media_ns = now_ns() - 4_000_000_000
        receiver._request_idr = lambda reason, force=False: None
        receiver._check_liveness()
        assert receiver.state is VideoStreamState.STALLED

        receiver._last_media_ns = now_ns()                     # media returns
        assert receiver._check_liveness() is False
        assert receiver.state is VideoStreamState.STREAMING

    def test_a_long_outage_is_reported_as_failed_not_silently_stalled(self):
        """The client layer retries a FAILED stream; it cannot retry a stall.

        So the boundary has to be visible: a stream that gave up must say so,
        or nothing above it knows to rebuild the session.
        """
        receiver = self._receiver()
        receiver._last_media_ns = now_ns() - 9_000_000_000
        receiver._request_idr = lambda reason, force=False: None
        assert receiver._check_liveness() is True, "the loop must stop"
        assert receiver.state is VideoStreamState.FAILED

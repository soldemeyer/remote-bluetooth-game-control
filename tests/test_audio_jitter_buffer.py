"""The adaptive jitter buffer: sized by the path, not by a constant.

The target used to be a fixed 30 ms that only moved to chase A/V skew. That
gets the causality backwards -- the buffer exists to absorb *network jitter*,
so how big it needs to be is a property of the network. And the old rule's
response to "audio is ahead of video" was to grow the buffer, i.e. to add audio
latency so that a slow video path would match, which for a game is precisely
the trade the brief says not to make.

Measured on this project's hardware, 4501 Opus packets over real WiFi to a
Raspberry Pi -- delay above the fastest packet:

    p50 0.78    p95 4.43    p99 5.93    p99.9 18.09    max 30.41 ms

So the static 30 ms was sized for the single worst packet in 45 seconds, and
about 24 ms of it was pure latency for the other 99%.
"""

from __future__ import annotations

from client.media.audio import (
    MAX_TARGET_MS,
    MIN_TARGET_MS,
    AudioPlayout,
    _GOVERNOR_INTERVAL_NS,
    _ms_to_bytes,
)


class _Sink:
    """Enough of a sink to construct a playout; nothing here plays anything."""

    def bytes_free(self) -> int:
        return 0

    def bytes_queued(self) -> int:
        return 0

    def write(self, data: bytes) -> None:
        pass


def _playout(target_ms: int = 30) -> AudioPlayout:
    return AudioPlayout(sink=_Sink(), target_ms=target_ms)


def _feed_transit(playout: AudioPlayout, samples_ms: list[float]) -> None:
    """Push transit times straight in, bypassing Opus.

    The governor reasons about `_transit`; decoding real packets to populate it
    would make these tests depend on libopus and on wall-clock timing, and
    would test the decoder rather than the governor.
    """
    for value in samples_ms:
        playout._transit.add(value)


def _tick(playout: AudioPlayout, video_ms: float = 0.0) -> None:
    """Run the governor, defeating its own 1 Hz rate limit."""
    playout._last_governor_ns -= _GOVERNOR_INTERVAL_NS * 2
    playout.tick_sync(video_ms)


class TestTheTargetFollowsMeasuredJitter:
    def test_a_clean_path_shrinks_the_buffer(self):
        playout = _playout(target_ms=30)
        # The real WiFi measurement: almost everything within a millisecond.
        _feed_transit(playout, [20.0] * 400 + [21.0] * 90 + [26.0] * 10)
        before = playout.target_ms
        for _ in range(20):
            _tick(playout)
        assert playout.target_ms < before
        assert playout.target_ms >= MIN_TARGET_MS

    def test_a_jittery_path_grows_it(self):
        playout = _playout(target_ms=20)
        # A path with 40 ms of spread needs a buffer that can absorb it.
        _feed_transit(playout, [10.0] * 250 + [50.0] * 250)
        for _ in range(20):
            _tick(playout)
        assert playout.target_ms > 20

    def test_it_never_leaves_the_configured_bounds(self):
        for samples in ([5.0] * 500, [0.0] * 250 + [500.0] * 250):
            playout = _playout()
            _feed_transit(playout, samples)
            for _ in range(50):
                _tick(playout)
            assert MIN_TARGET_MS <= playout.target_ms <= MAX_TARGET_MS

    def test_too_few_samples_changes_nothing(self):
        """A fresh stream must not resize off three packets."""
        playout = _playout(target_ms=30)
        _feed_transit(playout, [5.0, 500.0, 5.0])
        _tick(playout)
        assert playout.target_ms == 30
        # None, never 0.0: zero is what a *clean* path measures, and
        # that is the case where the buffer should shrink.
        assert playout.measured_jitter_ms() is None

    def test_the_estimate_ignores_a_constant_offset(self):
        """Jitter is a spread, so it must need no clock synchronisation."""
        near = _playout()
        far = _playout()
        _feed_transit(near, [1.0, 2.0, 3.0] * 40)
        _feed_transit(far, [1001.0, 1002.0, 1003.0] * 40)
        assert abs(near.measured_jitter_ms() - far.measured_jitter_ms()) < 0.001

    def test_one_freak_packet_does_not_set_the_buffer(self):
        """A single outlier must not dictate the next minute of latency."""
        playout = _playout()
        _feed_transit(playout, [10.0] * 499 + [400.0])
        assert playout.measured_jitter_ms() < 50.0

    def test_a_perfectly_clean_path_is_not_mistaken_for_no_data(self):
        """Zero jitter must shrink the buffer, not freeze it.

        Both used to return 0.0, so the governor held its starting target on
        exactly the paths that least needed a buffer.
        """
        playout = _playout(target_ms=45)
        _feed_transit(playout, [12.0] * 500)       # identical, i.e. zero spread
        assert playout.measured_jitter_ms() == 0.0
        for _ in range(30):
            _tick(playout)
        assert playout.target_ms == MIN_TARGET_MS


class TestItReactsToTroubleFasterThanItGivesLatencyBack:
    def test_an_underrun_raises_the_target_at_once(self):
        playout = _playout(target_ms=20)
        _feed_transit(playout, [10.0] * 500)      # path looks perfect
        _tick(playout)
        settled = playout.target_ms

        playout.underruns += 1                     # ...but the speaker ran dry
        _tick(playout)
        assert playout.target_ms > settled, "an underrun must widen the buffer"

    def test_it_is_raised_faster_than_it_is_lowered(self):
        rising = _playout(target_ms=20)
        _feed_transit(rising, [0.0] * 250 + [40.0] * 250)
        _tick(rising)
        raised = rising.target_ms - 20

        falling = _playout(target_ms=60)
        _feed_transit(falling, [10.0] * 500)
        _tick(falling)
        lowered = 60 - falling.target_ms

        assert raised > lowered


class TestAvSkewMayShrinkTheBufferButNeverGrowIt:
    """Adding audio latency to match slow video is the wrong trade for a game."""

    def test_audio_behind_video_shrinks_it(self):
        playout = _playout(target_ms=40)
        _feed_transit(playout, [10.0] * 500)
        playout._audio_latency_ms = 200.0          # far behind
        before = playout.target_ms
        _tick(playout, video_ms=10.0)
        assert playout.target_ms < before

    def test_audio_ahead_of_video_does_not_grow_it(self):
        playout = _playout(target_ms=20)
        _feed_transit(playout, [10.0] * 500)       # clean path, wants small
        playout._audio_latency_ms = 5.0            # far ahead of video
        for _ in range(10):
            _tick(playout, video_ms=200.0)
        # The old rule would have grown the buffer toward the video figure.
        assert playout.target_ms <= 20

    def test_no_video_figure_is_not_an_error(self):
        """The governor runs on jitter alone when nothing is being watched."""
        playout = _playout(target_ms=40)
        _feed_transit(playout, [10.0] * 500)
        for _ in range(20):
            _tick(playout)                          # no video argument at all
        assert playout.target_ms < 40


class TestItIsReportedNotJustActedOn:
    def test_the_snapshot_carries_the_jitter_it_reasoned_from(self):
        playout = _playout()
        _feed_transit(playout, [10.0] * 250 + [30.0] * 250)
        _tick(playout)
        snap = playout.snapshot()
        assert "jitter_ms" in snap
        assert snap["jitter_ms"] > 0
        # A buffer that looks wrong must be checkable against the path.
        assert "target_ms" in snap


class _SlowDevice:
    """An output device that will not hold less than `period_ms`.

    Bluetooth headphones behave like this -- 40 ms periods are routine -- and
    so do some USB DACs. It is the shape that breaks the top-up rule: the
    device never falls below the target, so `target - queued` is never
    positive and nothing substantial is ever written.
    """

    def __init__(self, period_ms: float = 40.0, capacity_ms: float = 120.0) -> None:
        self._period = _ms_to_bytes(period_ms)
        self._capacity = _ms_to_bytes(capacity_ms)
        self._queued = self._period

    def bytes_free(self) -> int:
        return max(self._capacity - self._queued, 0)

    def bytes_queued(self) -> int:
        return self._queued

    def write(self, data: bytes) -> None:
        # Plays what it is given and settles back to holding exactly one
        # period. A double that only accumulated would starve *every* target,
        # including ones a real device manages perfectly well -- which is what
        # the first version of this did, and it duly "found" the fault on a
        # 2 ms device.
        self._queued = min(self._capacity, self._period)


class TestADeviceThatCannotHoldToTheTarget:
    """Measured before this was handled, pinning the target under the period:

        target 12 ms -> deque   8 ms, device 10 ms, heard  10.4 ms
        target 10 ms -> deque  12 ms, device 10 ms, heard  12.5 ms
        target  8 ms -> deque 602 ms, device  8 ms, heard 604.4 ms

    `MIN_TARGET_MS` at 20 was the only thing preventing it, undocumented, and
    only sufficient for devices whose period is under 20 ms.
    """

    def _playout_on(self, device, target_ms: int = 20):
        return AudioPlayout(sink=device, target_ms=target_ms)

    def test_the_floor_is_learned_from_the_device(self):
        device = _SlowDevice(period_ms=40.0)
        playout = self._playout_on(device, target_ms=20)
        playout._priming = False

        # Audio arriving faster than a 20 ms target can absorb on this device.
        for _ in range(60):
            playout._enqueue(b"\0" * _ms_to_bytes(10))
            playout._pump_once(device)

        assert playout.device_floor_ms >= 40.0, (
            f"the device needs 40 ms and the floor learned {playout.device_floor_ms}"
        )

    def test_the_target_is_clamped_to_it(self):
        device = _SlowDevice(period_ms=40.0)
        playout = self._playout_on(device, target_ms=20)
        playout._priming = False
        for _ in range(60):
            playout._enqueue(b"\0" * _ms_to_bytes(10))
            playout._pump_once(device)

        # A clean path would otherwise drive the target to MIN_TARGET_MS.
        _feed_transit(playout, [10.0] * 500)
        for _ in range(40):
            _tick(playout)
        assert playout.target_ms >= 40.0, (
            "the governor drove the target below what the device can hold"
        )

    def test_the_queue_does_not_grow_without_bound(self):
        """The symptom that made this worth finding: 600 ms of latency."""
        device = _SlowDevice(period_ms=40.0)
        playout = self._playout_on(device, target_ms=20)
        playout._priming = False

        for _ in range(200):
            playout._enqueue(b"\0" * _ms_to_bytes(10))
            playout._pump_once(device)
            # The governor is what applies the learned floor.
            playout._last_governor_ns -= _GOVERNOR_INTERVAL_NS * 2
            playout.tick_sync()

        assert playout.buffered_ms < 200.0, (
            f"the deque grew to {playout.buffered_ms:.0f} ms"
        )

    def test_an_ordinary_device_pays_nothing(self):
        """The floor must stay dormant on hardware that behaves."""
        device = _SlowDevice(period_ms=2.0)
        playout = self._playout_on(device, target_ms=30)
        playout._priming = False
        for _ in range(60):
            playout._enqueue(b"\0" * _ms_to_bytes(10))
            playout._pump_once(device)
        assert playout.device_floor_ms == 0.0
        assert playout.snapshot()["device_floor_ms"] == 0.0

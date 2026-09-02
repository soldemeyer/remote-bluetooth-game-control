"""Timing, pacing, and latency statistics.

Timing tests are inherently a little loose -- CI machines stall. Tolerances are
chosen to catch real regressions (a 500 Hz loop running at 64 Hz because the
Windows timer fix broke) without failing on ordinary scheduler noise.
"""

from __future__ import annotations

import time

import pytest

from common.timing import (
    LatencyStats,
    RateLimiter,
    StageTimings,
    high_resolution_timers,
    now_ns,
    ns_to_ms,
    sleep_until_ns,
    NS_PER_MS,
)


def test_now_ns_is_monotonic():
    samples = [now_ns() for _ in range(1000)]
    assert all(b >= a for a, b in zip(samples, samples[1:]))


def test_ns_to_ms():
    assert ns_to_ms(1_000_000) == 1.0
    assert ns_to_ms(500_000) == 0.5


def test_high_resolution_timers_is_reentrant_and_safe():
    """No-op on Linux, real API calls on Windows; must not raise on either."""
    with high_resolution_timers():
        with high_resolution_timers():
            pass


def test_sleep_until_past_deadline_returns_immediately():
    start = now_ns()
    sleep_until_ns(start - 1_000_000)
    assert now_ns() - start < 5 * NS_PER_MS


def test_sleep_until_is_reasonably_accurate():
    """The whole latency goal rests on this. Inside the timer-resolution
    context we expect sub-millisecond-ish precision, so allow 3 ms of slop for
    scheduler noise but nothing like the 15.6 ms Windows default tick."""
    with high_resolution_timers():
        target = now_ns() + 5 * NS_PER_MS
        sleep_until_ns(target)
        overshoot_ms = ns_to_ms(now_ns() - target)

    assert -0.5 < overshoot_ms < 3.0, f"overshoot {overshoot_ms:.2f} ms"


class TestRateLimiter:
    def test_rejects_nonpositive_rate(self):
        with pytest.raises(ValueError):
            RateLimiter(0)

    def test_period_matches_rate(self):
        assert RateLimiter(1000).period_ns == 1_000_000
        assert RateLimiter(500).period_ns == 2_000_000

    def test_achieves_approximately_the_target_rate(self):
        """Guards the regression that matters: a nominally 200 Hz loop
        collapsing to the OS tick rate."""
        with high_resolution_timers():
            limiter = RateLimiter(200)
            start = now_ns()
            for _ in range(20):
                limiter.wait()
            elapsed_ms = ns_to_ms(now_ns() - start)

        assert 80 < elapsed_ms < 180, f"20 ticks at 200 Hz took {elapsed_ms:.1f} ms"

    def test_does_not_burst_after_a_long_stall(self):
        """After a GC pause or a laptop resume, catching up by firing a burst
        would flood the network with stale state. The limiter should resync."""
        limiter = RateLimiter(1000, max_catchup_periods=3)
        time.sleep(0.05)  # simulate a 50 ms stall == 50 missed periods

        start = now_ns()
        limiter.wait()
        limiter.wait()
        elapsed = now_ns() - start

        # If it were burst-firing, both waits would return instantly.
        assert elapsed > 500_000, "limiter burst instead of resyncing"

    def test_reset_reanchors_the_deadline(self):
        limiter = RateLimiter(100)
        time.sleep(0.03)
        limiter.reset()
        start = now_ns()
        limiter.wait()
        assert ns_to_ms(now_ns() - start) > 5


class TestLatencyStats:
    def test_empty_stats_are_zero(self):
        stats = LatencyStats()
        assert stats.count == 0
        assert stats.mean == 0.0
        assert stats.p50 == 0.0
        assert stats.p99 == 0.0
        assert stats.worst == 0.0

    def test_tracks_mean_and_last(self):
        stats = LatencyStats()
        for value in (10.0, 20.0, 30.0):
            stats.add(value)
        assert stats.mean == pytest.approx(20.0)
        assert stats.last_ms == 30.0
        assert stats.count == 3

    def test_percentiles(self):
        stats = LatencyStats()
        for value in range(1, 101):
            stats.add(float(value))
        assert stats.p50 == pytest.approx(50.0, abs=2.0)
        assert stats.p99 == pytest.approx(99.0, abs=2.0)
        assert stats.worst == 100.0

    def test_window_bounds_memory_and_ages_out_old_samples(self):
        stats = LatencyStats(window=10)
        for value in range(100):
            stats.add(float(value))

        assert stats.count == 10
        assert stats.total_count == 100
        # Only the last 10 samples (90..99) should remain.
        assert stats.mean == pytest.approx(94.5)
        assert stats.worst == 99.0

    def test_rolling_sum_stays_correct_after_eviction(self):
        """The incremental sum is easy to get subtly wrong; check it against a
        full recomputation."""
        stats = LatencyStats(window=5)
        for value in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0):
            stats.add(value)
        assert stats.mean == pytest.approx((3 + 4 + 5 + 6 + 7) / 5)

    def test_clear_resets(self):
        stats = LatencyStats()
        stats.add(5.0)
        stats.clear()
        assert stats.count == 0
        assert stats.last_ms == 0.0

    def test_p99_reflects_a_spike(self):
        """A controller that is usually fine but spikes should look bad at p99
        while the mean stays deceptively low -- that's the whole point."""
        stats = LatencyStats()
        for _ in range(99):
            stats.add(10.0)
        stats.add(500.0)
        assert stats.mean < 20.0
        assert stats.p99 >= 100.0

    def test_snapshot_shape(self):
        """Pinned exactly, so adding a figure is a decision rather than a drift.

        The shape is consumed by the web GUI, the client GUI and the WebSocket
        feed, so a key appearing or vanishing is a change to something other
        people read.
        """
        stats = LatencyStats()
        stats.add(12.345)
        snap = stats.snapshot()
        assert set(snap) == {
            "last", "mean", "best", "p50", "p90", "p95", "p99", "worst", "count",
        }
        assert snap["last"] == 12.345


def test_stage_timings_snapshot_covers_all_stages():
    timings = StageTimings()
    timings.rtt.add(20.0)
    timings.server_process.add(0.1)

    snap = timings.snapshot()
    assert set(snap) == {"rtt", "input_age", "server_process", "bt_write"}
    assert snap["rtt"]["last"] == 20.0
    assert snap["server_process"]["last"] == 0.1


class TestTheSnapshotCarriesTheWholeSpread:
    """Median, P90, P95, P99, minimum and maximum -- the full set.

    The snapshot used to stop at p50 and p99, so every measurement taken
    during the latency work was richer than anything an operator could see:
    p90 and p95 were computed by calling `percentile()` directly and then
    discarded. p95 is where the video path's tail appears before p99 does.
    """

    def _stats(self):
        stats = LatencyStats()
        for value in range(1, 101):
            stats.add(float(value))
        return stats

    def test_every_named_figure_is_present(self):
        snap = self._stats().snapshot()
        for key in ("best", "p50", "p90", "p95", "p99", "worst", "mean", "count"):
            assert key in snap, f"{key} missing from the snapshot"

    def test_they_are_ordered(self):
        snap = self._stats().snapshot()
        assert (
            snap["best"] <= snap["p50"] <= snap["p90"]
            <= snap["p95"] <= snap["p99"] <= snap["worst"]
        ), snap

    def test_the_extremes_are_the_extremes(self):
        stats = self._stats()
        assert stats.best == 1.0
        assert stats.worst == 100.0

    def test_an_empty_window_reads_zero_rather_than_raising(self):
        snap = LatencyStats().snapshot()
        assert snap["best"] == 0.0
        assert snap["p95"] == 0.0

    def test_a_single_sample_is_every_percentile(self):
        stats = LatencyStats()
        stats.add(7.5)
        snap = stats.snapshot()
        for key in ("best", "p50", "p90", "p95", "p99", "worst"):
            assert snap[key] == 7.5, f"{key} was {snap[key]}"

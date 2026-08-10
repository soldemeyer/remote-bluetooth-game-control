"""High-resolution timing, loop pacing, and latency statistics.

The single most important thing in this module is the Windows timer resolution
fix. By default, Windows' scheduler tick is ~15.6 ms, so ``time.sleep(0.002)``
can actually sleep 15 ms. A 500 Hz poll loop would degrade to ~64 Hz and the
entire latency goal collapses. ``timeBeginPeriod(1)`` drops the tick to 1 ms;
the remaining sub-millisecond fraction is spin-waited.

Everything here uses ``time.perf_counter_ns()``. It is monotonic (immune to NTP
steps and DST), nanosecond-resolution, and on both Windows and Linux it maps to
a cheap userspace read.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000

#: Below this remaining time, spin instead of sleeping. Sleeping cannot resolve
#: finer than ~1 ms even with timeBeginPeriod(1), and oversleeping by 0.5 ms on
#: every iteration is a systematic latency tax we can trade CPU for.
_SPIN_THRESHOLD_NS = 1_500_000  # 1.5 ms


def now_ns() -> int:
    """Monotonic nanosecond timestamp. The clock used everywhere in this project."""
    return time.perf_counter_ns()


@contextlib.contextmanager
def high_resolution_timers() -> Iterator[None]:
    """Raise OS timer resolution for the duration of the block.

    On Windows this is essential (see module docstring). On Linux the default
    tick is already fine and this is a no-op.

    Always used as a context manager because ``timeBeginPeriod`` must be paired
    with ``timeEndPeriod`` -- leaking it raises power draw system-wide until
    the process exits.
    """
    if sys.platform != "win32":
        yield
        return

    import ctypes

    winmm = ctypes.WinDLL("winmm")
    result = winmm.timeBeginPeriod(1)
    if result != 0:
        log.warning(
            "timeBeginPeriod(1) failed (code %d); loop pacing will be coarse "
            "and latency will suffer",
            result,
        )
        yield
        return

    log.debug("Windows timer resolution raised to 1 ms")
    try:
        yield
    finally:
        winmm.timeEndPeriod(1)
        log.debug("Windows timer resolution restored")


def sleep_until_ns(deadline_ns: int) -> None:
    """Sleep until ``deadline_ns``, spin-waiting the final fraction.

    Hybrid on purpose: sleeping the bulk keeps a core free for the rest of the
    system, while spinning the tail gives us the sub-millisecond precision that
    ``sleep`` alone cannot deliver on any OS.
    """
    while True:
        remaining = deadline_ns - now_ns()
        if remaining <= 0:
            return
        if remaining > _SPIN_THRESHOLD_NS:
            # Leave the threshold behind for the spin phase to absorb overshoot.
            time.sleep((remaining - _SPIN_THRESHOLD_NS) / NS_PER_S)
        else:
            # Yield the GIL so we don't starve other threads while spinning.
            time.sleep(0)


class RateLimiter:
    """Fixed-rate loop pacer with drift compensation.

    Advances the deadline by exactly one period each tick rather than measuring
    from "now", so scheduling jitter does not accumulate into a slow drift.
    If the loop falls badly behind (a GC pause, a laptop resuming from sleep),
    the deadline is resynchronized rather than firing a burst of catch-up
    iterations that would flood the network with stale state.
    """

    __slots__ = ("_period_ns", "_next_deadline", "_max_catchup_ns")

    def __init__(self, hz: float, *, max_catchup_periods: int = 3) -> None:
        if hz <= 0:
            raise ValueError("rate must be positive")
        self._period_ns = int(NS_PER_S / hz)
        self._next_deadline = now_ns() + self._period_ns
        self._max_catchup_ns = self._period_ns * max_catchup_periods

    @property
    def period_ns(self) -> int:
        return self._period_ns

    def wait(self) -> None:
        """Block until the next tick is due."""
        sleep_until_ns(self._next_deadline)

        now = now_ns()
        self._next_deadline += self._period_ns

        if now - self._next_deadline > self._max_catchup_ns:
            # We are far behind -- resync instead of burst-firing.
            self._next_deadline = now + self._period_ns

    def reset(self) -> None:
        self._next_deadline = now_ns() + self._period_ns


@dataclass(slots=True)
class LatencyStats:
    """Rolling latency window with percentiles.

    p99 matters more than the mean here: a controller that is usually 12 ms but
    spikes to 90 ms feels broken, while a steady 20 ms feels fine. The GUIs
    surface p50 and p99 side by side for exactly that reason.

    Fixed-size deque, so memory is bounded and old samples age out on their own.
    """

    window: int = 512
    _samples: deque[float] = field(default_factory=deque, init=False)
    _sum: float = field(default=0.0, init=False)
    total_count: int = field(default=0, init=False)
    last_ms: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self._samples = deque(maxlen=self.window)

    def add(self, value_ms: float) -> None:
        if len(self._samples) == self.window:
            self._sum -= self._samples[0]
        self._samples.append(value_ms)
        self._sum += value_ms
        self.total_count += 1
        self.last_ms = value_ms

    def clear(self) -> None:
        self._samples.clear()
        self._sum = 0.0
        self.last_ms = 0.0

    @property
    def count(self) -> int:
        return len(self._samples)

    @property
    def mean(self) -> float:
        n = len(self._samples)
        return self._sum / n if n else 0.0

    def percentile(self, pct: float) -> float:
        """Nearest-rank percentile. ``pct`` in 0..100."""
        n = len(self._samples)
        if n == 0:
            return 0.0
        ordered = sorted(self._samples)
        rank = max(0, min(n - 1, int(round(pct / 100.0 * n + 0.5)) - 1))
        return ordered[rank]

    @property
    def p50(self) -> float:
        return self.percentile(50)

    @property
    def p99(self) -> float:
        return self.percentile(99)

    @property
    def worst(self) -> float:
        return max(self._samples) if self._samples else 0.0

    def snapshot(self) -> dict[str, float | int]:
        """Plain-dict view for the GUIs and the WebSocket feed."""
        return {
            "last": round(self.last_ms, 3),
            "mean": round(self.mean, 3),
            "p50": round(self.p50, 3),
            "p99": round(self.p99, 3),
            "worst": round(self.worst, 3),
            "count": self.total_count,
        }


@dataclass(slots=True)
class StageTimings:
    """Per-stage latency breakdown for one input, in milliseconds.

    Exists so "latency is bad" can be answered with data instead of guesses.
    The client fills ``rtt`` and ``input_age``; the server reports
    ``server_process`` and ``bt_write``.
    """

    rtt: LatencyStats = field(default_factory=LatencyStats)
    input_age: LatencyStats = field(default_factory=LatencyStats)
    server_process: LatencyStats = field(default_factory=LatencyStats)
    bt_write: LatencyStats = field(default_factory=LatencyStats)

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        return {
            "rtt": self.rtt.snapshot(),
            "input_age": self.input_age.snapshot(),
            "server_process": self.server_process.snapshot(),
            "bt_write": self.bt_write.snapshot(),
        }


def ns_to_ms(ns: int) -> float:
    return ns / NS_PER_MS


def try_set_realtime_priority(thread_name: str = "datapath") -> bool:
    """Request SCHED_FIFO for the calling thread on Linux. Returns success.

    Keeps the datapath from being preempted by the web GUI or by background
    system work, which is what produces p99 latency spikes.

    Failure is normal and non-fatal: it needs CAP_SYS_NICE or root, and the
    system works fine without it -- just with worse tail latency. We log at
    info, not warning, so a non-root dev run is not noisy.
    """
    if sys.platform != "linux":
        return False

    try:
        import os

        param = os.sched_param(10)  # low-ish RT priority; we are not a kernel driver
        os.sched_setscheduler(0, os.SCHED_FIFO, param)
    except (AttributeError, PermissionError, OSError) as exc:
        log.info(
            "Could not set SCHED_FIFO for %s thread (%s). Running at normal priority; "
            "tail latency may be higher. Grant CAP_SYS_NICE to improve this.",
            thread_name,
            exc,
        )
        return False

    log.info("Thread %s running with SCHED_FIFO priority", thread_name)
    return True


def configure_gc_for_realtime() -> None:
    """Freeze existing objects and disable the cyclic collector.

    GC pauses are the largest source of tail-latency jitter in a Python hot
    loop. We allocate almost nothing per packet by design, so the cyclic
    collector has very little to do -- disabling it trades a small, bounded
    memory growth risk for materially better p99.

    ``gc.freeze()`` moves everything allocated during startup into a permanent
    generation so it is never rescanned.
    """
    import gc

    gc.collect()
    gc.freeze()
    gc.disable()
    log.info("GC frozen and disabled for realtime operation")


def restore_gc() -> None:
    """Re-enable the collector. Used on shutdown and by tests."""
    import gc

    gc.enable()
    gc.unfreeze()

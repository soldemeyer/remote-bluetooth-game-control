"""What the playout actually does to an audio device, measured.

The choppy-audio investigation had nothing to argue with. `AudioPlayout`
reported `buffered_ms`, `underruns` and `overruns`, and **all three can read
perfectly healthy while the speaker is starving** -- so a fault that is plainly
audible left no trace anywhere. Worse, the one measurement recorded in the
code's own docstring (holding back gives 4 underruns, priming-only gives 40)
came from a script that is not in this repo, and no committed test could
reproduce it: every sink double in the suite answers `bytes_free()` with 0, so
the playout loop exits on its first branch and never runs at all against them.

This module supplies what was missing: a sink that behaves like a real device.
`DrainingSink` is a fixed-capacity ring that empties at exactly 48 kHz against a
virtual clock, and it reports the only number a listener actually experiences --
**how many milliseconds it spent with nothing to play**.

Deterministic on purpose. An audio test that starts the real thread and hopes
the scheduler cooperates measures the scheduler as much as it measures the
buffer; stepping `_pump_once` against a virtual clock makes a claim about
dropouts reproducible.

It needs no PyAV and no audio device: PCM goes in through `_enqueue`, which is
where the decoder would have put it.
"""

from __future__ import annotations

import random

import pytest

from client.media.audio import (
    BYTES_PER_FRAME,
    BYTES_PER_MS,
    SAMPLE_RATE,
    AudioPlayout,
    _ms_to_bytes,
)

PACKET_MS = 10.0
PACKET_SAMPLES = 480
#: What a capture device typically hands the resampler. 21.33 ms, not 10.
DEVICE_SAMPLES = 1024


class VirtualClock:
    """Time the test advances by hand, in fractional milliseconds."""

    def __init__(self) -> None:
        self.ns = 0

    def advance_ms(self, ms: float) -> None:
        self.ns += int(ms * 1_000_000)


class DrainingSink:
    """A fixed-capacity buffer that empties at the sample rate, like a device.

    The existing test doubles answer `bytes_free()` with 0 forever, which means
    the playout loop exits on its first branch and nothing downstream is
    exercised. This one consumes what it is given at 192 bytes/ms and records
    what happened when it had nothing left.

    `silence_ms` is the headline: wall-clock time the device spent with an empty
    buffer, which is exactly what a listener hears as a dropout.
    """

    def __init__(
        self,
        clock: VirtualClock,
        capacity_ms: float = 40.0,
        drift_ppm: float = 0.0,
    ) -> None:
        self._clock = clock
        self.capacity = _ms_to_bytes(capacity_ms)
        # A playback crystal is not the capture card crystal. Positive ppm
        # means this device runs fast and consumes more than arrives.
        self._rate = BYTES_PER_MS * (1.0 + drift_ppm / 1_000_000.0)

        self._queued = 0.0
        self._last_ns = 0
        self._was_dry = True

        self.silence_ms = 0.0
        self.dry_events = 0
        self.played_bytes = 0.0
        self.written_bytes = 0
        self.short_writes = 0

    # -- the device ---------------------------------------------------------

    def _drain(self) -> None:
        now = self._clock.ns
        elapsed_ms = (now - self._last_ns) / 1_000_000.0
        self._last_ns = now
        if elapsed_ms <= 0:
            return

        wanted = elapsed_ms * self._rate
        if wanted <= self._queued:
            self._queued -= wanted
            self.played_bytes += wanted
            self._was_dry = False
            return

        # It ran out partway through the interval. The remainder is silence.
        self.played_bytes += self._queued
        shortfall = wanted - self._queued
        self._queued = 0.0
        self.silence_ms += shortfall / self._rate
        if not self._was_dry:
            self.dry_events += 1
        self._was_dry = True

    # -- the sink protocol --------------------------------------------------

    def bytes_free(self) -> int:
        self._drain()
        return max(int(self.capacity - self._queued), 0)

    def bytes_queued(self) -> int:
        self._drain()
        return max(int(self._queued), 0)

    def write(self, data: bytes) -> None:
        self._drain()
        room = self.capacity - self._queued
        accepted = min(len(data), int(room))
        if accepted < len(data):
            self.short_writes += 1
        self._queued += accepted
        self.written_bytes += accepted
        if accepted:
            self._was_dry = False

    def close(self) -> None:
        pass

    # -- measurement --------------------------------------------------------

    def reset_stats(self) -> None:
        """Forget the startup transient, so steady state is measured alone."""
        self._drain()
        self.silence_ms = 0.0
        self.dry_events = 0
        self.played_bytes = 0.0
        self.written_bytes = 0
        self.short_writes = 0

    @property
    def queued_ms(self) -> float:
        return self._queued / BYTES_PER_MS


# -- what the source sends ---------------------------------------------------


def smooth_packets(duration_ms: float, start_ms: float = 0.0) -> list:
    """One 10 ms packet every 10 ms -- what the wire format implies."""
    out = []
    t = start_ms
    while t < duration_ms:
        out.append((t, bytes(PACKET_SAMPLES * BYTES_PER_FRAME)))
        t += PACKET_MS
    return out


def bursty_packets(
    duration_ms: float, device_samples: int = DEVICE_SAMPLES, start_ms: float = 0.0
) -> list:
    """What the source sends **today**, and why it matters.

    `AudioEncoder` builds its resampler with no `frame_size`, so the resampler
    emits device-sized frames -- commonly 1024 samples, 21.33 ms. PyAV then
    re-fifos those down to the 480 samples libopus requires, so a single
    `encode()` call emits two or three packets and they leave together.

    The stream is still 100 packets a second on average; it is the *cadence*
    that is wrong, and a buffer with no cushion has nothing to absorb it.
    """
    out = []
    frame_ms = device_samples / SAMPLE_RATE * 1000.0
    pending = 0
    t = start_ms
    while t < duration_ms:
        pending += device_samples
        while pending >= PACKET_SAMPLES:
            pending -= PACKET_SAMPLES
            out.append((t, bytes(PACKET_SAMPLES * BYTES_PER_FRAME)))
        t += frame_ms
    return out


def drop_every(packets: list, nth: int) -> list:
    """Lose one packet in `nth`, keeping the arrival times of the rest."""
    return [p for index, p in enumerate(packets) if (index + 1) % nth]


def jittered(packets: list, spread_ms: float, seed: int = 7) -> list:
    """Perturb arrival times without changing the average rate.

    Seeded, because a flaky audio test is worse than no audio test. Even a
    loopback path has jitter: the receive thread reassembles video slices
    between audio packets, and the playout thread polls on a 5 ms tick.
    """
    rng = random.Random(seed)
    moved = [
        (max(t + rng.uniform(-spread_ms, spread_ms), 0.0), payload)
        for t, payload in packets
    ]
    return sorted(moved, key=lambda item: item[0])


# -- the driver --------------------------------------------------------------


class Simulation:
    """Steps the real playout loop against the virtual clock."""

    #: A pass that writes costs a syscall, not zero. Small enough not to change
    #: the result, large enough that a spin cannot hang the test.
    SPIN_MS = 0.05

    def __init__(
        self,
        playout: AudioPlayout,
        sink: DrainingSink,
        clock: VirtualClock,
        packets: list,
    ) -> None:
        self.playout = playout
        self.sink = sink
        self.clock = clock
        self.packets = sorted(packets, key=lambda item: item[0])
        self.t = 0.0
        self._next = 0
        self._next_pump = 0.0
        self.delivered = 0

        #: Sink fill sampled at every pump, so the steady-state level can be
        #: asserted on directly. This is the number that was unmeasurable from
        #: inside the application and that decides everything else.
        self.sink_levels: list[float] = []

    def run(self, duration_ms: float) -> None:
        """Advance to the next event, whichever it is.

        **Arrivals and pumps are independent clocks, and keeping them so is the
        whole point.** The receive thread enqueues a packet the moment it lands;
        the playout thread notices only when it next wakes, which is its own 5 ms
        poll. An earlier version of this driver stepped to each arrival *and*
        pumped there, which quietly handed the playout a wakeup per packet --
        the sink then survived on a knife edge that does not exist in a real
        process, and the harness measured itself.
        """
        deadline = self.t + duration_ms
        while self.t < deadline:
            arrival = (
                self.packets[self._next][0]
                if self._next < len(self.packets)
                else float("inf")
            )
            target = min(arrival, self._next_pump, deadline)
            if target > self.t:
                self.clock.advance_ms(target - self.t)
                self.t = target

            while (
                self._next < len(self.packets)
                and self.packets[self._next][0] <= self.t + 1e-9
            ):
                self.playout._enqueue(self.packets[self._next][1])
                self._next += 1
                self.delivered += 1

            if self.t + 1e-9 >= self._next_pump:
                self.sink_levels.append(self.sink.queued_ms)
                wait_s = self.playout._pump_once(self.sink)
                if wait_s is None:
                    raise AssertionError("the playout gave up on the sink")
                self._next_pump = self.t + (
                    wait_s * 1000.0 if wait_s else self.SPIN_MS
                )

    # -- measurement --------------------------------------------------------

    def reset_levels(self) -> None:
        self.sink_levels.clear()

    @property
    def sink_level_min(self) -> float:
        return min(self.sink_levels) if self.sink_levels else 0.0

    @property
    def sink_level_mean(self) -> float:
        return (
            sum(self.sink_levels) / len(self.sink_levels)
            if self.sink_levels
            else 0.0
        )


def build(
    packets: list,
    *,
    target_ms: int = 30,
    capacity_ms: float = 40.0,
    drift_ppm: float = 0.0,
):
    clock = VirtualClock()
    sink = DrainingSink(clock, capacity_ms=capacity_ms, drift_ppm=drift_ppm)
    playout = AudioPlayout(sink=sink, target_ms=target_ms)
    return Simulation(playout, sink, clock, packets), sink, playout


# -- the harness must be right before anything it measures is ----------------


class TestTheHarnessModelsADevice:
    def test_an_untouched_sink_is_silent_for_the_whole_interval(self):
        clock = VirtualClock()
        sink = DrainingSink(clock)
        clock.advance_ms(100.0)
        sink._drain()
        assert sink.silence_ms == pytest.approx(100.0, abs=0.5)

    def test_a_full_sink_plays_without_a_gap(self):
        clock = VirtualClock()
        sink = DrainingSink(clock, capacity_ms=40.0)
        sink.write(bytes(_ms_to_bytes(40)))
        clock.advance_ms(40.0)
        sink._drain()
        assert sink.silence_ms == pytest.approx(0.0, abs=0.5)
        assert sink.played_bytes == pytest.approx(_ms_to_bytes(40), rel=0.02)

    def test_the_sink_cannot_take_more_than_it_holds(self):
        clock = VirtualClock()
        sink = DrainingSink(clock, capacity_ms=40.0)
        sink.write(bytes(_ms_to_bytes(100)))
        assert sink.short_writes == 1
        assert sink.queued_ms == pytest.approx(40.0, abs=0.5)

    def test_the_source_generators_produce_a_real_time_stream(self):
        """Both cadences must carry the same audio per second."""
        smooth = smooth_packets(1000.0)
        bursty = bursty_packets(1000.0)
        assert len(smooth) == 100
        # 1024-sample device frames yield 100 packets/s on average, in bursts.
        assert 97 <= len(bursty) <= 103
        arrivals = sorted({round(t, 3) for t, _ in bursty})
        assert len(arrivals) < len(bursty), "bursty source produced no bursts"


# -- the properties that matter ----------------------------------------------


class TestTheDeviceKeepsAMargin:
    """Where the reserve actually sits, and what that costs.

    The design intends to hold `target_ms` in reserve ahead of the speaker. It
    holds it in the **deque**, which the device cannot reach, so the margin the
    device actually has is whatever happens to be in flight -- measured at 4-5
    ms against a 30 ms target.

    That is not by itself enough to chop: measured across jitter to +/-10 ms,
    drift to 1000 ppm, source stalls to 100 ms and 1% packet loss, the current
    code loses well under 0.1% of the audio. What it has is **no margin left
    for anything bigger**, and `TestACoarseCaptureDevice` is what bigger looks
    like.

    `underruns` is deliberately not asserted on anywhere: the implementation
    can never increment it (see `TestTheReserveIsReachable`), so a test built
    on it would pass while the audio chopped.
    """

    def test_the_device_is_left_holding_the_target(self):
        """The reserve must sit where the speaker can reach it.

        Not asserted as *exactly* the target: the device drains between polls
        (5 ms) and the source arrives in bursts, so the fill sawtooths below
        the top-up point. What matters is that it tracks the target rather
        than one arrival interval -- it was 4.6 ms min / 21.8 ms mean against
        a 30 ms target before the reserve was made reachable.
        """
        sim, sink, playout = build(bursty_packets(4000.0), target_ms=30)
        sim.run(500.0)              # priming: a gap here is expected and fine
        sink.reset_stats()
        sim.reset_levels()
        sim.run(3000.0)

        target = playout.target_ms
        assert sim.sink_level_mean > target * 0.75, (
            f"device averaged {sim.sink_level_mean:.1f} ms against a "
            f"{target:.0f} ms target"
        )
        assert sim.sink_level_min > target * 0.4, (
            f"device fell to {sim.sink_level_min:.1f} ms (mean "
            f"{sim.sink_level_mean:.1f}) against a {target:.0f} ms target"
        )

    def test_ordinary_jitter_costs_no_audio(self):
        """Guards the margin that does work, so a fix cannot regress it."""
        sim, sink, playout = build(jittered(bursty_packets(4000.0), 10.0))
        sim.run(500.0)
        sink.reset_stats()
        sim.run(3000.0)

        assert sink.silence_ms < 30.0, (
            f"device starved for {sink.silence_ms:.1f} ms over 3 s at "
            f"+/-10 ms jitter ({sink.dry_events} dry events)"
        )
        assert playout.overruns == 0

    def test_a_gap_inside_the_target_is_absorbed(self):
        """The plainest statement of what the reserve is for.

        A 25 ms hole in delivery is comfortably inside the 30 ms the buffer
        says it is holding, so it should be inaudible. It is not: the reserve
        sits in the deque, the device has 4-5 ms, and it runs dry. Nothing
        about a 100 ms stall would prove this -- a 30 ms buffer genuinely
        cannot cover one, and expecting it to would be asking for magic.
        """
        stall_ms = 25.0
        packets = bursty_packets(4000.0)
        moved = [
            (t if not (900.0 <= t < 900.0 + stall_ms) else 900.0 + stall_ms, payload)
            for t, payload in packets
        ]
        sim, sink, playout = build(moved)
        sim.run(500.0)
        sink.reset_stats()
        sim.reset_levels()
        sim.run(3000.0)

        assert sink.silence_ms < 1.0, (
            f"a {stall_ms:.0f} ms gap -- inside the "
            f"{playout.target_ms:.0f} ms target -- cost "
            f"{sink.silence_ms:.1f} ms of silence; the device fell to "
            f"{sim.sink_level_min:.1f} ms"
        )


class TestACoarseCaptureDevice:
    """The one input that does destroy the audio, and it is not the network.

    `AudioCapture._open` passes **no** buffer options to dshow or ALSA, so the
    device delivers whatever size frame it likes. `AudioEncoder` then resamples
    without a `frame_size` and sends each Opus packet inline as it is produced,
    so a coarse device frame becomes a burst of packets on the wire, arriving
    at the client together.

    `_enqueue` caps the deque at `target_ms + BURST_HEADROOM_MS` = 180 ms and
    drops the **oldest** past that. A device frame larger than the cap
    therefore has part of itself thrown away on arrival, every single frame.
    Measured at 200 ms frames: a quarter of the audio discarded.

    That is the shape of the reported fault, and unlike everything else tested
    here it does not need a network, a slow machine or bad luck.
    """

    def test_a_frame_the_buffer_can_hold_is_fine(self):
        sim, sink, playout = build(bursty_packets(6000.0, device_samples=2048))
        sim.run(1000.0)
        sink.reset_stats()
        sim.run(4000.0)
        assert playout.overruns == 0
        assert sink.silence_ms < 30.0

    def test_a_coarse_capture_device_does_not_cost_audio(self):
        sim, sink, playout = build(bursty_packets(6000.0, device_samples=9600))
        sim.run(1000.0)
        sink.reset_stats()
        sim.run(4000.0)

        assert sink.silence_ms < 40.0 and playout.overruns == 0, (
            f"200 ms device frames cost {sink.silence_ms:.1f} ms of silence "
            f"in 4 s ({sink.silence_ms / 40.0:.1f}% of the stream) with "
            f"{playout.overruns} overruns"
        )

    def test_the_measured_field_case(self):
        """The fault as it actually presented, reproduced from its own numbers.

        A DirectShow capture device running at **44.1 kHz in 22050-sample
        frames -- exactly 500 ms** -- which is FFmpeg's documented dshow
        default ("typically some multiple of 500ms") because
        `AudioCapture._open` requests no buffer size at all. The whole 500 ms
        becomes 50 Opus packets sent back to back, and the client's 180 ms cap
        throws away everything past the cap on arrival.

        Reproduced against the field log to within measurement noise: 70
        overruns/s against 62-100 observed, 58 kB/s against 58-65 observed,
        30% of the audio delivered against ~32% observed.
        """
        # 24000 samples at 48 kHz is what 22050 at 44.1 kHz resamples to.
        sim, sink, playout = build(bursty_packets(20000.0, device_samples=24000))
        sim.run(2000.0)
        sink.reset_stats()
        sim.run(10000.0)

        delivered = sink.written_bytes / 10.0 / (BYTES_PER_MS * 1000)
        assert playout.overruns == 0 and delivered > 0.98, (
            f"only {delivered * 100:.0f}% of the audio reached the device "
            f"({sink.written_bytes / 10.0 / 1000:.0f} kB/s of "
            f"{BYTES_PER_MS} kB/s) with {playout.overruns} overruns"
        )


class TestTheReserveIsReachable:
    """The buffer must be able to hand over everything it holds.

    `_take` subtracts `hold = target_ms` on every read and `_buffered_bytes`
    decreases nowhere else but the overrun drop, so once the deque exceeds the
    target it can never fall below it again. Three things follow: the reserve
    is unspendable latency, `_is_empty()` is never true again, and the underrun
    branch of the playout loop is dead code.
    """

    def test_the_buffer_can_be_drained_to_empty(self):
        playout = AudioPlayout(sink=object(), target_ms=30)
        playout._enqueue(bytes(_ms_to_bytes(100)))

        for _ in range(100):
            if playout._take(_ms_to_bytes(10)) is None:
                break

        assert playout._is_empty(), (
            f"{playout.buffered_ms:.1f} ms is stranded in the buffer with no "
            f"way to reach the device"
        )

    def test_a_starved_device_is_counted_as_an_underrun(self):
        sim, sink, playout = build(smooth_packets(1000.0))
        sim.run(500.0)
        sink.reset_stats()
        before = playout.underruns
        sim.run(2000.0)             # the source has stopped: total starvation

        assert sink.silence_ms > 100.0, "the harness did not starve the device"
        assert playout.underruns > before, (
            f"device was silent for {sink.silence_ms:.1f} ms and the playout "
            f"counted {playout.underruns - before} underruns"
        )

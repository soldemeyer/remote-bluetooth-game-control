"""Phase 23: does latency recover, or does it accumulate?

Every optimization in this project trades some margin for latency, and the way
that goes wrong is not a crash -- it is a system that never comes back to where
it started. A jitter buffer that grows under load and stays grown, a bitrate
that drops and creeps back over ten minutes, an input loop that loses its
cadence under CPU pressure and does not regain it: all of those look healthy on
every counter and feel broken to play.

So each scenario here measures the same thing three times -- **before, during,
and after** -- and the question is always whether "after" matches "before".

Usage:
    python -m tools.recovery_harness                 # all scenarios
    python -m tools.recovery_harness --only cpu
    python -m tools.recovery_harness --list
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from common.timing import (
    LatencyStats,
    RateLimiter,
    high_resolution_timers,
    now_ns,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


class Phases:
    """Before / during / after for one measured quantity."""

    def __init__(self, name: str, unit: str = "ms", tolerance: float = 1.5) -> None:
        self.name = name
        self.unit = unit
        #: How much worse "after" may be than "before" and still count as
        #: recovered. A ratio, because these quantities span two orders of
        #: magnitude and an absolute slack would be meaningless for both ends.
        self.tolerance = tolerance
        self.before = 0.0
        self.during = 0.0
        self.after = 0.0

    @property
    def recovered(self) -> bool:
        if self.before <= 0:
            return self.after <= max(self.during * 0.5, 0.001)
        return self.after <= self.before * self.tolerance

    def line(self) -> str:
        verdict = "recovered" if self.recovered else "STILL DEGRADED"
        return (
            f"    {self.name:34} {self.before:8.2f} -> {self.during:8.2f} -> "
            f"{self.after:8.2f} {self.unit:3}  {verdict}"
        )


def header(title: str) -> None:
    print(f"\n  {title}")
    print("  " + "-" * (len(title)))
    print(f"    {'measure':34} {'before':>8}    {'during':>8}    {'after':>8}")


# --------------------------------------------------------------------------
# Scenario: CPU starvation vs the input loop
# --------------------------------------------------------------------------


class _GilHog:
    """Threads doing pure-Python work, i.e. contending for the GIL.

    The realistic client-side stress: video decode, a browser, a compile. Not
    `time.sleep`, which yields, and not a C extension that releases the GIL --
    the thing that actually hurts a Python real-time loop is other *Python*.
    """

    def __init__(self, threads: int = 4) -> None:
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(target=self._burn, daemon=True) for _ in range(threads)
        ]

    def _burn(self) -> None:
        value = 0
        while not self._stop.is_set():
            for index in range(5000):
                value = (value + index) % 1_000_003

    def __enter__(self):
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0)


def _pace_lateness(seconds: float, hz: int = 500) -> LatencyStats:
    """How late each wake-up is against its scheduled deadline."""
    stats = LatencyStats(window=8192)
    limiter = RateLimiter(hz)
    expected = now_ns() + limiter.period_ns
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        limiter.wait()
        now = now_ns()
        stats.add(max(0.0, (now - expected) / 1_000_000))
        expected = now + limiter.period_ns
    return stats


def scenario_cpu(seconds: float) -> list[Phases]:
    """Does the 500 Hz loop regain its cadence after CPU pressure lifts?

    This is the scenario that validates lowering `_SPIN_THRESHOLD_NS` from
    1.5 ms to 0.75 ms. Less spinning is less CPU, but it also means less margin
    against a late wake-up -- so the question is not only whether the loop
    survives contention, but whether it comes back afterwards.
    """
    header("CPU starvation vs the 500 Hz input loop")
    p50 = Phases("wake-up lateness p50")
    p99 = Phases("wake-up lateness p99", tolerance=3.0)

    with high_resolution_timers():
        before = _pace_lateness(seconds)
        with _GilHog():
            during = _pace_lateness(seconds)
        after = _pace_lateness(seconds)

    for phase, key in ((p50, "p50"), (p99, "p99")):
        phase.before = getattr(before, key)
        phase.during = getattr(during, key)
        phase.after = getattr(after, key)
        print(phase.line())
    return [p50, p99]


# --------------------------------------------------------------------------
# Scenario: the audio jitter buffer after a burst of jitter
# --------------------------------------------------------------------------


def scenario_audio_jitter(seconds: float) -> list[Phases]:
    """The buffer must grow under jitter and give the latency back afterwards.

    Growing is the easy half. A buffer that grows and stays grown is a
    permanent latency increase caused by a problem that has gone away, and it
    is invisible: every counter reads healthy because the audio is perfect.
    """
    from client.media.audio import AudioPlayout

    header("Audio jitter buffer under a burst of jitter")
    target = Phases("jitter buffer target", tolerance=1.05)

    class _Sink:
        def bytes_free(self):
            return 0

        def bytes_queued(self):
            return 0

        def write(self, data):
            pass

    playout = AudioPlayout(sink=_Sink(), target_ms=30)

    def settle(spread_ms: float, ticks: int) -> float:
        for _ in range(ticks):
            for index in range(64):
                playout._transit.add(10.0 + (index % 2) * spread_ms)
            playout._last_governor_ns = 0
            playout.tick_sync()
        return playout.target_ms

    target.before = settle(0.5, 40)          # clean path
    target.during = settle(45.0, 40)         # jitter arrives
    target.after = settle(0.5, 80)           # and clears
    print(target.line())
    return [target]


# --------------------------------------------------------------------------
# Scenario: video through a path that breaks and comes back
# --------------------------------------------------------------------------


def _video_rig(device: str, port: int, password: str):
    """A real source, a degradable path and a real decoding client."""
    from client.media.decoder import VideoDecoder
    from client.net.video import VideoReceiver, VideoStreamState
    from common.video import VideoSettings
    from tools.impair import ImpairedRelay, Impairment
    from videoserver.config import VideoServerConfig
    from videoserver.pipeline import VideoServerApp

    settings = VideoSettings(
        backend="dshow", device=device, width=1280, height=720, fps=60,
        bitrate_kbps=6000, audio_enabled=False, preview_enabled=False,
    )
    app = VideoServerApp(VideoServerConfig(
        standalone=True, password=password, settings=settings,
        media_bind_host="127.0.0.1", media_port=port))
    app.start()

    impairment = Impairment()
    relay = ImpairedRelay(("127.0.0.1", port), to_client=impairment, seed=31)
    relay.start()

    receiver = VideoReceiver(password, client_name="recovery")
    decoder = VideoDecoder(receiver)
    decoder.start()
    receiver.connect_async({"host": relay.address[0], "port": relay.address[1],
                            "password": password})

    deadline = time.time() + 20
    while time.time() < deadline and receiver.state is not VideoStreamState.STREAMING:
        time.sleep(0.1)
    if receiver.state is not VideoStreamState.STREAMING:
        detail = receiver.state_detail
        decoder.stop(); relay.stop(); app.stop()
        raise RuntimeError(f"could not start the stream: {detail}")
    time.sleep(3.0)
    return app, relay, impairment, receiver, decoder


def _fps_over(app, decoder, duration: float) -> float:
    base = decoder.frames_decoded
    started = time.time()
    end = started + duration
    while time.time() < end:
        app.tick_governor()
        time.sleep(0.05)
    return (decoder.frames_decoded - base) / (time.time() - started)


def scenario_video(seconds: float, device: str, port: int) -> list[Phases]:
    """A total blackout, then a clean path. Does the picture come straight back?

    The blackout is the harshest version: 100% loss, so every slice, every
    keyframe and every keyframe *request* is gone. What must not happen is the
    stream staying broken once the path returns -- the client has to notice, ask
    for a keyframe, and get one.
    """
    header("Video across a path that fails completely and returns")
    rate = Phases("decoded frames per second", unit="fps")

    app, relay, impairment, receiver, decoder = _video_rig(device, port, "recovery-a")
    try:
        rate.before = _fps_over(app, decoder, seconds)
        impairment.loss = 1.0
        rate.during = _fps_over(app, decoder, seconds)

        # Time to the first frame after the path returns.
        impairment.loss = 0.0
        base = decoder.frames_decoded
        started = now_ns()
        while decoder.frames_decoded == base and now_ns() - started < 15e9:
            app.tick_governor()
            time.sleep(0.01)
        first_frame_ms = (now_ns() - started) / 1e6

        rate.after = _fps_over(app, decoder, seconds)
    finally:
        receiver.close(); decoder.stop(); relay.stop(); app.stop()

    # Higher is better for a frame rate, so the generic check is inverted here.
    rate.recovered_value = rate.after >= rate.before * 0.9
    print(f"    {'decoded fps':34} {rate.before:8.1f} -> {rate.during:8.1f} -> "
          f"{rate.after:8.1f} fps  "
          f"{'recovered' if rate.recovered_value else 'STILL DEGRADED'}")
    print(f"    {'first frame after the blackout':34} {first_frame_ms:8.0f} ms")
    return [_Inverted(rate)]


def scenario_bitrate(seconds: float, device: str, port: int) -> list[Phases]:
    """Bandwidth collapses and then returns. How long until quality is back?

    The governor drops quickly by design. Coming back is the half nobody
    watches, and a stream that takes minutes to recover its bitrate after a
    thirty-second problem is a stream that spends most of a session below the
    quality the link can carry.
    """
    header("Bitrate after bandwidth collapses and returns")
    kbps = Phases("active bitrate", unit="kbps")

    app, relay, impairment, receiver, decoder = _video_rig(device, port, "recovery-b")
    try:
        kbps.before = app.status()["bitrate_kbps"]

        impairment.rate_kbps = 2000
        impairment.burst_bytes = 256 * 1024
        end = time.time() + seconds * 3
        while time.time() < end:
            app.tick_governor()
            time.sleep(0.05)
        kbps.during = app.status()["bitrate_kbps"]

        # Path restored: measure how long it takes to climb back.
        impairment.rate_kbps = 0.0
        started = now_ns()
        target = kbps.before * 0.9
        while now_ns() - started < 180e9:
            app.tick_governor()
            time.sleep(0.05)
            if app.status()["bitrate_kbps"] >= target:
                break
        recovery_s = (now_ns() - started) / 1e9
        kbps.after = app.status()["bitrate_kbps"]
    finally:
        receiver.close(); decoder.stop(); relay.stop(); app.stop()

    kbps.recovered_value = kbps.after >= kbps.before * 0.9
    print(f"    {'active bitrate':34} {kbps.before:8.0f} -> {kbps.during:8.0f} -> "
          f"{kbps.after:8.0f} kbps  "
          f"{'recovered' if kbps.recovered_value else 'STILL DEGRADED'}")
    print(f"    {'time to climb back':34} {recovery_s:8.0f} s")
    return [_Inverted(kbps)]


class _Inverted:
    """Wraps a Phases whose "better" direction is up, not down."""

    def __init__(self, phases: Phases) -> None:
        self._phases = phases
        self.name = phases.name

    @property
    def recovered(self) -> bool:
        return bool(getattr(self._phases, "recovered_value", False))

    def line(self) -> str:
        return self._phases.line()


SCENARIOS = {
    "cpu": ("CPU starvation vs the input loop", scenario_cpu),
    "audio": ("Audio jitter buffer recovery", scenario_audio_jitter),
    "video": ("Video across a failing path", scenario_video),
    "bitrate": ("Bitrate recovery after congestion", scenario_bitrate),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recovery-harness")
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--only", action="append", choices=sorted(SCENARIOS))
    parser.add_argument("--device", default="ShadowCast 3")
    parser.add_argument("--port", type=int, default=47850)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s",
                        stream=sys.stdout)

    if args.list:
        for key, (title, _) in sorted(SCENARIOS.items()):
            print(f"  {key:8} {title}")
        return 0

    chosen = args.only or sorted(SCENARIOS)
    port = args.port
    print("\n  Recovery harness -- before / during / after")
    print("  The question is always whether 'after' matches 'before'.")

    results: list[Phases] = []
    for key in chosen:
        _title, func = SCENARIOS[key]
        if key in ("video", "bitrate"):
            results += func(args.seconds, args.device, port)
            port += 1
        else:
            results += func(args.seconds)

    failed = [p for p in results if not p.recovered]
    print()
    if failed:
        for phase in failed:
            print(f"  DID NOT RECOVER: {phase.name}")
        return 1
    print("  All measured quantities returned to baseline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

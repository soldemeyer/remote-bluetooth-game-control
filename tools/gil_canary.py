"""How late does a 500 Hz loop wake up while the video path is working?

    python -m tools.gil_canary --seconds 20
    python -m tools.gil_canary --paint 1920x1080 --window 1280x720

The client's input loop runs at 500 Hz and is the thing this whole project is
built around. Everything else in the process competes with it for one GIL, so
"what does the video path cost the input loop?" is the question that decides
what the video path is allowed to do.

**This tool exists because the numbers it measures were previously
unreproducible.** ``client/media/decoder.py``'s module docstring rests on three
measurements -- 1.81 ms p99 painting 1080p into a 1280x720 window, 4.52 ms
scaling to 1440p, 4.87 ms for one ``bytes(plane)`` copy -- and the script that
produced them was never committed. They are the justification for the zero-copy
publish, for scaling on the decode thread, and now for presenting on the GPU,
and until this file existed nobody could check any of them.

WHAT IT MEASURES, and why lateness rather than duration
--------------------------------------------------------
A canary thread sleeps for exactly 2 ms, wakes, and records how much later than
2 ms it actually woke. That lateness is the GIL hold it could not interrupt --
which is precisely what a player feels as input lag, and is invisible in any
profile of the code that caused it.

The p99 is the number that matters. A mean hides the thing being looked for:
one 5 ms hold in a hundred wake-ups is a stutter a player notices, and it moves
the mean by 50 microseconds.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.timing import high_resolution_timers, now_ns, sleep_until_ns  # noqa: E402

#: The client's own input rate.
CANARY_HZ = 500


class Canary:
    """A 500 Hz loop that records how late each wake-up was."""

    def __init__(self, hz: int = CANARY_HZ) -> None:
        self._interval_ns = int(1e9 / hz)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lateness_ms: list[float] = []

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="canary", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        due = now_ns() + self._interval_ns
        while not self._stop.is_set():
            sleep_until_ns(due)
            woke = now_ns()
            self.lateness_ms.append(max(0.0, (woke - due) / 1e6))
            # From the deadline, not from now: drifting the schedule forward by
            # however late the last wake-up was would hide exactly the thing
            # being measured.
            due += self._interval_ns
            if woke > due + self._interval_ns * 4:
                # Badly behind -- resynchronise rather than spinning to catch
                # up, which would report one long stall as hundreds of short
                # ones.
                due = woke + self._interval_ns

    def stop(self) -> dict[str, float]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        samples = sorted(self.lateness_ms)
        if not samples:
            return {}
        return {
            "count": len(samples),
            "p50": statistics.median(samples),
            "p90": samples[int(len(samples) * 0.90)],
            "p99": samples[int(len(samples) * 0.99)],
            "max": samples[-1],
        }


def _parse_size(text: str) -> tuple[int, int]:
    width, _, height = text.lower().partition("x")
    return int(width), int(height)


def run_idle(seconds: float) -> None:
    time.sleep(seconds)


def run_qpainter(seconds: float, source: tuple[int, int],
                 window: tuple[int, int]) -> None:
    """The load the client's Off path puts on the GUI thread.

    ``QPainter`` does **not** release the GIL while it scales, which is the
    whole reason the decoder resamples on its own thread and the window only
    blits. This reproduces both cases: a 1:1 blit, and the scale that happens
    when the picture and the window disagree.
    """
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    picture = QImage(source[0], source[1], QImage.Format.Format_RGB888)
    picture.fill(0x404040)
    target = QImage(window[0], window[1], QImage.Format.Format_RGB888)

    deadline = time.perf_counter() + seconds
    frames = 0
    while time.perf_counter() < deadline:
        painter = QPainter(target)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(target.rect(), picture)
        painter.end()
        frames += 1
        # 60 fps, so this is a video player rather than a benchmark.
        time.sleep(max(0.0, 1 / 60))
    print(f"  painted {frames} frames")
    del app


def run_copy(seconds: float, source: tuple[int, int]) -> None:
    """One ``bytes(plane)`` per frame, the copy the decoder no longer makes.

    CPython holds the GIL for the whole of a memcpy this size. Preallocating a
    destination does not help -- the hold is the copy itself.
    """
    payload = bytearray(source[0] * source[1] * 3)
    view = memoryview(payload)
    deadline = time.perf_counter() + seconds
    frames = 0
    while time.perf_counter() < deadline:
        _ = bytes(view)
        frames += 1
        time.sleep(max(0.0, 1 / 60))
    print(f"  copied {frames} frames ({len(payload) / 1e6:.2f} MB each)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--source", default="1920x1080",
                        help="the decoded picture's size")
    parser.add_argument("--window", default="1280x720",
                        help="the size it is painted into")
    parser.add_argument(
        "--load", default="all",
        choices=("all", "idle", "paint", "blit", "copy"),
        help="which load to run the canary against",
    )
    args = parser.parse_args(argv)

    source = _parse_size(args.source)
    window = _parse_size(args.window)

    loads: list[tuple[str, callable]] = []
    if args.load in ("all", "idle"):
        loads.append(("idle (the floor)", lambda s: run_idle(s)))
    if args.load in ("all", "blit"):
        loads.append((f"QPainter 1:1 blit {window[0]}x{window[1]}",
                      lambda s: run_qpainter(s, window, window)))
    if args.load in ("all", "paint"):
        loads.append((f"QPainter scale {source[0]}x{source[1]} -> "
                      f"{window[0]}x{window[1]}",
                      lambda s: run_qpainter(s, source, window)))
    if args.load in ("all", "copy"):
        loads.append((f"bytes() of a {source[0]}x{source[1]} rgb24 frame",
                      lambda s: run_copy(s, source)))

    print(f"Canary at {CANARY_HZ} Hz, {args.seconds:.0f} s per load.")
    print("Lateness is how much later than its deadline the loop woke: the")
    print("GIL hold it could not interrupt, which is what a player feels.")
    print()
    print(f"{'load':<48}{'p50':>8}{'p90':>8}{'p99':>8}{'max':>9}")
    print("-" * 81)

    # Inside the timer context for the whole run. Without timeBeginPeriod(1)
    # on Windows the sleep granularity is about 15.6 ms, and every row would
    # measure the scheduler rather than the load -- which is the same trap the
    # client's own input loop exists inside.
    with high_resolution_timers():
        for name, run in loads:
            canary = Canary()
            canary.start()
            time.sleep(0.5)      # let it settle before the load starts
            run(args.seconds)
            stats = canary.stop()
            if not stats:
                print(f"{name:<48}{'no samples':>33}")
                continue
            print(f"{name:<48}{stats['p50']:>8.3f}{stats['p90']:>8.3f}"
                  f"{stats['p99']:>8.3f}{stats['max']:>9.3f}")

    print()
    print("All figures in milliseconds. Compare against the idle floor rather")
    print("than against zero -- the scheduler's own jitter is in every row.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

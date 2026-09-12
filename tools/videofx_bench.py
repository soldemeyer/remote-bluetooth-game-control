"""What each video enhancement mode costs on this machine.

    python -m tools.videofx_bench
    python -m tools.videofx_bench --frames 500

Measures the GPU time of the enhancement itself, through the library's own
timestamp queries -- which are read back a frame late and never waited on, so
running this does not change what it is measuring.

**This is not end-to-end latency and must not be quoted as if it were.** It is
the cost of one mode's work on the GPU, which is the number you need to answer
"what does RTX VSR cost me?" and "is FSR 1 worth it?". The client's own overlay
carries the whole-path figure (capture -> presented), and that is the one a
player feels.

The Off mode is absent on purpose. Off does not go through this library at all
-- it takes the client's existing QPainter path -- so there is no GPU time to
measure and nothing here could compare the two honestly. What separates Off
from the rest is where the work happens and whether it holds the GIL, which is
what `tools/gil_canary.py` is for.
"""

from __future__ import annotations

import argparse
import ctypes
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.media import videofx  # noqa: E402

#: Source -> output pairs worth knowing about.
#:
#: The last is a four-player split-screen quadrant of a 1080p console picture,
#: shown full screen. It is the case with the most to gain -- the source is a
#: quarter of the frame and it is being enlarged four times over -- and the one
#: where a slow upscaler would be most obvious.
CASES: tuple[tuple[str, int, int, int, int], ...] = (
    ("1280x720  -> 1920x1080", 1280, 720, 1920, 1080),
    ("1920x1080 -> 2560x1440", 1920, 1080, 2560, 1440),
    ("1920x1080 -> 3840x2160", 1920, 1080, 3840, 2160),
    ("960x540   -> 1920x1080", 960, 540, 1920, 1080),
)

MODES: tuple[tuple[str, int], ...] = (
    ("GPU present (Lanczos)", videofx.MODE_LANCZOS),
    ("FSR 1 EASU+RCAS", videofx.MODE_FSR1),
    ("RTX VSR", videofx.MODE_RTX_VSR),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=200,
                        help="frames to time per mode (default 200)")
    parser.add_argument("--warmup", type=int, default=30,
                        help="frames to discard first (default 30)")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("The Direct3D 11 backend is Windows-only.", file=sys.stderr)
        return 1

    videofx.reset_cache()
    if not videofx.is_available():
        print(f"No GPU enhancement library: {videofx.load_error()}", file=sys.stderr)
        return 1

    caps = videofx.probe()
    if not caps.usable:
        print(f"No usable graphics device: {caps.reason_usable}", file=sys.stderr)
        return 1

    # The test module already owns a window and a synthetic frame, and
    # duplicating either here would give two things that have to agree.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
    from test_videofx_device import Frame, _Window, make_renderer, submit

    lib = videofx.library()
    print(f"GPU       {caps.gpu_name}")
    print(f"Renderer  {caps.backend}   driver {caps.driver}")
    print(f"Frames    {args.frames} timed, {args.warmup} discarded")
    print()
    print(f"{'case':<26}{'mode':<24}{'p50 ms':>9}{'p99 ms':>9}{'path':>20}")
    print("-" * 88)

    for label, src_w, src_h, dst_w, dst_h in CASES:
        window = _Window(dst_w, dst_h)
        try:
            for mode_name, mode in MODES:
                if mode == videofx.MODE_RTX_VSR and not caps.rtx_vsr:
                    continue
                if mode == videofx.MODE_FSR1 and not caps.fsr1:
                    continue

                handle = make_renderer(lib, window.hwnd, mode)
                # Explicitly off: the capture is a full-resolution copy per
                # frame, and measuring the renderer with it on would measure
                # something nobody runs.
                lib.rbgc_debug_capture(handle, 0)
                try:
                    frame = Frame(src_w, src_h, 126, 128, 128)
                    built = frame.build(dst=(0, 0, dst_w, dst_h),
                                        composed=(dst_w, dst_h))

                    for _ in range(args.warmup):
                        submit(lib, handle, built)

                    times: list[float] = []
                    path = videofx.PATH_NONE
                    for _ in range(args.frames):
                        result = submit(lib, handle, built)
                        path = result.path
                        if result.gpu_ms >= 0.0:
                            times.append(result.gpu_ms)

                    if not times:
                        print(f"{label:<26}{mode_name:<24}{'n/a':>9}")
                        continue

                    times.sort()
                    p50 = statistics.median(times)
                    p99 = times[min(len(times) - 1, int(len(times) * 0.99))]
                    name = videofx.PATH_NAMES.get(path, str(path))
                    print(f"{label:<26}{mode_name:<24}{p50:>9.3f}{p99:>9.3f}{name:>20}")
                finally:
                    lib.rbgc_destroy(handle)
        finally:
            window.close()

    print()
    print("The path column is what the renderer ACTUALLY did, not what was")
    print("asked for. RTX VSR reads 'requested' because no API reports whether")
    print("the driver ran it -- the GPU time is the evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

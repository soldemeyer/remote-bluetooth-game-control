"""End-to-end audio latency probe: capture card to speaker, all real code.

Phases 10-12 of the latency work. The audio path had **no measurements at all**
-- every video benchmark in this project ran with ``audio_enabled=False`` -- and
it is the subsystem with the worst track record: three severe faults are
documented in CLAUDE.md (500 ms capture frames, an unspendable jitter reserve,
+100 ppm clock drift), and every one of them was invisible to every counter
until somebody measured the right thing.

So this runs the whole chain, unmodified:

    AudioCapture -> AudioEncoder -> VideoNet -> UDP -> VideoReceiver
                 -> AudioPlayout -> QtAudioSink -> speaker

and reports each stage's contribution, rather than asking any single component
whether it is happy.

**The output device is muted by default.** Volume does not affect how fast a
sink drains, so muting costs the measurement nothing and saves whoever is near
the machine from a surprise. Pass ``--unmute`` if you want to hear it.

Usage:
    python -m tools.audio_latency_probe --seconds 60
    python -m tools.audio_latency_probe --seconds 900 --diag    # drift needs time
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

# Qt needs a platform plugin even though nothing is drawn; audio output works
# fine under the offscreen one.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from client.media.audio import (
    BYTES_PER_MS,
    _SYNC_TOLERANCE_MS,
    AudioPlayout,
    QtAudioSink,
)
from client.net.video import VideoReceiver, VideoStreamState
from common.timing import LatencyStats, now_ns
from common.video import VideoSettings
from videoserver.config import VideoServerConfig
from videoserver.pipeline import VideoServerApp

PASSWORD = "audio-probe-password"
PORT = 47816


def _fmt(stats: LatencyStats, width: int = 7) -> str:
    if not stats.count:
        return "(no samples)"
    return (
        f"p50 {stats.p50:{width}.2f}  p95 {stats.percentile(95):{width}.2f}  "
        f"p99 {stats.p99:{width}.2f}  worst {stats.worst:{width}.2f} ms"
    )


def build_source(args) -> VideoServerApp:
    settings = VideoSettings(
        backend="dshow",
        device=args.device,
        audio_device=args.audio_device,
        audio_enabled=True,
        audio_bitrate_kbps=args.audio_bitrate,
        # Video runs too, because the point is to measure audio under the load
        # it actually shares a machine with. --no-video isolates it instead.
        width=args.width,
        height=args.height,
        fps=args.fps,
        bitrate_kbps=args.bitrate,
        test_source=args.no_video,
        preview_enabled=False,
    )
    config = VideoServerConfig(
        standalone=True,
        password=PASSWORD,
        settings=settings,
        media_bind_host="127.0.0.1",
        media_port=PORT,
    )
    app = VideoServerApp(config)
    app.start()
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audio-latency-probe",
        description="Measure the audio path end to end, on real hardware.",
    )
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--device", default="ShadowCast 3")
    parser.add_argument("--audio-device", default="", help="blank = first found")
    parser.add_argument("--audio-bitrate", type=int, default=96)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--bitrate", type=int, default=8000)
    parser.add_argument("--target-ms", type=int, default=None,
                        help="override the jitter buffer target")
    parser.add_argument("--capture-ms", type=int, default=None,
                        help="override the capture device buffer ladder (A/B)")
    parser.add_argument("--no-video", action="store_true",
                        help="use a video test pattern, to isolate audio from capture load")
    parser.add_argument("--unmute", action="store_true", help="actually play it")
    parser.add_argument("--diag", action="store_true",
                        help="emit the per-second RBGC_AUDIO_DIAG line from both ends")
    parser.add_argument("--no-av-sync", action="store_true",
                        help="skip the video half, and with it the A/V skew")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.diag else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    if args.diag:
        os.environ["RBGC_AUDIO_DIAG"] = "1"
    else:
        # Keep the noisy per-second lines off, but let warnings through.
        logging.getLogger("rbgc.audio.diag").setLevel(logging.WARNING)

    from PySide6.QtWidgets import QApplication

    app_qt = QApplication.instance() or QApplication([])

    print("\n  Audio latency probe")
    print(f"    duration    {args.seconds:.0f} s")
    print(f"    video       {'test pattern' if args.no_video else f'{args.width}x{args.height}@{args.fps}'}")
    print(f"    output      {'audible' if args.unmute else 'muted (does not affect timing)'}")
    print()

    if args.capture_ms:
        import videoserver.capture as vcap
        vcap._AUDIO_BUFFER_MS = (args.capture_ms,)

    source = build_source(args)

    try:
        sink = QtAudioSink()
    except Exception as exc:
        print(f"  No audio output device: {exc}", file=sys.stderr)
        source.stop()
        return 1

    playout = AudioPlayout(
        sink=sink,
        muted=not args.unmute,
        **({"target_ms": args.target_ms} if args.target_ms else {}),
    )
    playout.start()

    # Latency as the *speaker* sees it, which is the only figure that matters
    # and the one nothing currently records: what `feed` measures stops at the
    # moment a packet is buffered, before the jitter buffer and the device.
    speaker = LatencyStats(window=4096)
    buffered = LatencyStats(window=4096)
    device = LatencyStats(window=4096)

    receiver = VideoReceiver(PASSWORD, client_name="audio-probe")

    def on_audio(data: bytes, capture_ts: int, seq: int) -> None:
        playout.feed(data, capture_ts, receiver.clock_offset_ns, seq)

    receiver._on_audio = on_audio

    # The video half exists solely so `present_stats` is populated and the A/V
    # skew below is a real measurement.
    #
    # **It did not, and the skew line silently never printed.** `present_stats`
    # is filled by `VideoWindow` at paint time and by nothing else, so a probe
    # with no window reports a skew of zero -- which reads as "perfectly in
    # sync" rather than as "not measured". The window is offscreen; it is here
    # to run the real presentation path, not to be looked at.
    decoder = window = None
    if not args.no_av_sync:
        from client.gui.video_window import VideoWindow
        from client.media.decoder import VideoDecoder

        decoder = VideoDecoder(receiver)
        decoder.start()
        window = VideoWindow(decoder, receiver)
        window.resize(1280, 720)
        window.show()

    receiver.connect_async({"host": "127.0.0.1", "port": PORT, "password": PASSWORD})

    deadline = time.time() + 15
    while time.time() < deadline and receiver.state is not VideoStreamState.STREAMING:
        app_qt.processEvents()
        time.sleep(0.1)
    if receiver.state is not VideoStreamState.STREAMING:
        print(f"  client never connected: {receiver.state.name} {receiver.state_detail}")
        playout.stop()
        source.stop()
        return 1

    print("  streaming; sampling...\n")
    end = time.time() + args.seconds
    while time.time() < end:
        app_qt.processEvents()
        # Sampled here rather than inside the playout loop: this is a meter and
        # must not perturb the thread it is measuring.
        buf_ms = playout.buffered_ms
        snk_ms = playout.sink_ms
        # Already capture-to-speaker: it includes whatever was queued ahead of
        # the packet when it arrived. Adding the current queue on top would
        # count the same audio twice -- which this probe did at first, and it
        # read as a 50 ms regression that had not happened.
        heard_ms = playout._audio_latency_ms
        if heard_ms:
            buffered.add(buf_ms)
            device.add(snk_ms)
            speaker.add(heard_ms)
        source.tick_governor()
        playout.tick_sync(receiver.present_stats.p50)
        time.sleep(0.05)

    snap = source.snapshot()
    cap = snap.get("audio_capture", {})
    enc = snap.get("audio", {})
    pl = playout.snapshot()

    print("=" * 74)
    print("  SOURCE  (capture card -> Opus)")
    print("=" * 74)
    rate = int(cap.get("sample_rate", 0) or 0)
    spf = int(cap.get("samples_per_frame", 0) or 0)
    print(f"    device rate            {rate} Hz"
          f"{'   <- resampled to 48000 for Opus' if rate and rate != 48000 else ''}")
    print(f"    frame size             {spf} samples"
          f"  = {spf / (rate / 1000.0):.1f} ms" if rate and spf else "")
    print(f"    frame size min/max     {cap.get('samples_per_frame_min')} / "
          f"{cap.get('samples_per_frame_max')}")
    print(f"    delivery gap           {cap.get('frame_gap_ms')} ms  "
          f"(worst {cap.get('frame_gap_max_ms')} ms)")
    print(f"    capture dropped/errors {cap.get('dropped')} / {cap.get('errors')}")
    print(f"    packets per encode max {enc.get('packets_per_encode_max')}"
          f"     <- above 1 means Opus packets leave in bursts")
    print(f"    resampled samples max  {enc.get('resampled_samples_max')}")
    print(f"    packets encoded        {enc.get('packets_encoded')}")
    print(f"    level rms / peak       {enc.get('level_rms')} / {enc.get('level_peak')}"
          f"   fresh={enc.get('level_fresh')}")

    print()
    print("=" * 74)
    print("  CLIENT  (Opus -> speaker)")
    print("=" * 74)
    print(f"    packets received       {pl['packets']}")
    print(f"    lost/gaps/reord/dup    {pl['packets_lost']} / {pl['seq_gaps']} / "
          f"{pl['reordered']} / {pl['duplicates']}")
    print(f"    underruns / overruns   {pl['underruns']} / {pl['overruns']}")
    print(f"    decode errors          {pl['decode_errors']}")
    print(f"    drift shed             {pl['drift_shed_ms']:.0f} ms over the run")
    print(f"    jitter target          {pl['target_ms']:.0f} ms")

    print()
    print("=" * 74)
    print("  LATENCY  (all figures milliseconds)")
    print("=" * 74)
    print(f"    jitter buffer held     {_fmt(buffered)}")
    print(f"    output device held     {_fmt(device)}")
    print(f"    capture -> SPEAKER     {_fmt(speaker)}")
    print()
    print("    'capture -> speaker' is when a packet's audio actually reaches the")
    print("    device, i.e. including everything queued ahead of it. The jitter")
    print("    buffer and device rows above are the standing queue depths, and")
    print("    they are components of it -- do not add them to it.")

    print()
    print("=" * 74)
    print("  A/V SYNC")
    print("=" * 74)
    video = receiver.present_stats
    if not video.count:
        print("    video path produced no samples -- skew NOT measured")
    elif not speaker.count:
        print("    audio path produced no samples -- skew NOT measured")
    else:
        skew = speaker.p50 - video.p50
        print(f"    video capture -> painted   {_fmt(video)}")
        print(f"    audio capture -> speaker   {_fmt(speaker)}")
        print(f"    skew (audio - video)       {skew:+.1f} ms p50  "
              f"({'audio behind' if skew > 0 else 'audio ahead'})")
        print(f"    governor tolerance         +/-{_SYNC_TOLERANCE_MS:.0f} ms  "
              f"-> {'INSIDE, governor idle' if abs(skew) <= _SYNC_TOLERANCE_MS else 'OUTSIDE, governor will act'}")
        print("    (both figures exclude the capture card and the compositor)")

    receiver.close()
    playout.stop()
    if decoder is not None:
        decoder.stop()
    if window is not None:
        window.close()
    source.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

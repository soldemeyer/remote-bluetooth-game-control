# Latency optimization log

Running record of the button-to-photon work: what was measured, what changed,
and what it bought. Every row here is a measurement or is explicitly marked
`NEEDS MEASUREMENT`. Estimates are labelled as estimates.

The controller path already has this treatment and is documented in
[CLAUDE.md](CLAUDE.md) ("The latency budget"). This file is about the **video
and audio return path**, which had never had it.

---

## The setup these numbers come from

| | |
|---|---|
| Bluetooth server | Raspberry Pi **4B** (8 GB), kernel 6.18, BlueZ 5.82 |
| Video server | `video_mode: external` — a Windows PC with the capture card |
| Stream | **1920×1080 @ 60 fps, 8000 kbps**, Opus 96 kbps |
| Capture card | Genki **ShadowCast 3** (`usb vid_32ed pid_3701`), UVC / DirectShow |
| Transport | Internet only (`lan_enabled: false`), broker + STUN |

**The card's advertised media types**, read off the DirectShow pin (`list_options`):

```
pixel_format=yuyv422   1920x1080  up to  60 fps      <- listed first
vcodec=mjpeg           1920x1080  up to 120 fps
raw RGB (0xE436EB7D)   1920x1080  up to  60 fps
pixel_format=nv12      1920x1080  up to 120 fps
```

**Measured, with the card live:** the code path negotiates
`rawvideo yuyv422 1920x1080 @ 60 fps` -- the *first* media type on the pin,
exactly as predicted, because nothing in the open requests a format. That is
4.15 MB per frame, ~249 MB/s over USB, and a clean 60.0 fps with zero capture
errors and zero frames superseded.

**No H.264 or HEVC.** That is what made the capture decoder's old
`thread_type = "AUTO"` harmless *here* rather than a multi-frame hazard: mjpeg
and rawvideo declare no threading capability at all, while h264 and hevc declare
`FRAME_THREADS`. See change 3 -- it was fixed anyway, because the next capture
card may well output H.264.

---

## Optimization log

| # | Optimization | Status | Before | After | Improvement |
|---|---|---|---|---|---|
| 1 | Video latency stamped at paint, not at pickup | **Complete** | measured to the wrong point | measured to `painter.end()` | correctness, not speed |
| 1b | Frame delivery: 5 ms poll → queued signal | **Complete** | p50 2.64 / p99 5.87 ms | p50 1.43 / p99 2.02 ms | **−1.2 ms p50, −3.9 ms p99** |
| 1c | `LatencyStats` percentile read is thread-safe | **Complete** | could raise and kill the stream | copies before sorting | removes a crash |
| 2 | `rtbufsize` 64 MB → 3 frames | **Complete** | 257 ms of slack at 1080p | 50 ms | removes a 200 ms cliff |
| 3 | Capture `thread_type` AUTO → SLICE | **Complete** | 0 for this card | 0 | insurance; 50 ms on an H.264 card |
| 4 | Pace the burst, not every frame | **Complete** | fan-out p50 4.70 ms | **0.53 ms** | **−4.2 ms per frame** |
| 5 | Avoid the per-frame 6.22 MB `bytes()` copy | **Complete** | tick p99 ~2.40 ms | ~2.55 ms | **none — see below** |
| 6 | Move scaling off the paint thread | **Complete** | input tick p99 ~2.50 ms | **~1.53 ms** | **−1.0 ms p99** (p50 +0.08) |
| 7 | Request an explicit capture pixel format | **Measured — not worth doing** | `yuyv422` | — | see below |
| 8 | Audio: capture buffer 20 → 10 ms | **Complete** | 2 Opus pkts/encode | 1 | cadence, not latency |
| 9 | Audio: latency measured to the speaker | **Complete** | reported 0.8 ms | 30.4 ms | corrects a 30 ms understatement |
| 10 | Audio: adaptive jitter buffer | **Complete** | static 30 ms | 20 ms on a clean path | **−8 ms** |
| 11 | Spin threshold 1.5 → 0.75 ms | **Complete** | input tick p99 1.53 ms | **0.96 ms** | **−0.6 ms p99, CPU 67→15%** |
| 12 | Poll rate (Ph. 16) | **Measured — 500 Hz confirmed** | assumption | measurement | no change needed |
| 13 | Impairment harness (`tools/impair.py`) | **Complete** | no way to degrade a path | loss/jitter/cap/dup | unblocks Ph. 8, 9, 23 |
| 14 | Delay-based congestion (Ph. 8) | **Complete** | 985 slices lost, 8000→1423 kbps | **0 lost, converges 4725** | **loss eliminated** |
| 15 | Bitrate change no longer reopens the camera | **Complete** | 8 device reopens per episode | 0 | removes a stream gap |
| 16 | Loss recovery: XOR parity (Ph. 9) | **Complete** | 45.7 fps at 1% loss | **59.7 fps** | **+31% frame rate** |
| 17 | Loss measured over a window, not a lifetime | **Complete** | parity never switched off | switches off in ~15 s | fixes both governors |
| 18 | Bitrate recovery ramp (Ph. 23) | **Complete** | >180 s to recover | **20 s** | ends minutes of degradation |
| 19 | Recovery harness + tests (Ph. 23) | **Complete** | untested | 4 scenarios | validates the rest |
| 20 | Audio buffer floor | **Measured — leave at 20 ms** | 26.6 ms heard | unchanged | see below |
| 21 | Output devices that cannot hold to the target | **Complete** | 604 ms of latency | ~11 ms | fixes BT headphones |
| 22 | Audio loss concealment (Ph. 12) | **Complete** | 1055 ms lost per 35 s at 3% | **12 ms** | **underruns 24 → 0** |
| 23 | `intra_refresh` reaches every encoder (Ph. 5) | **Complete** | dead on NVENC | applied | a setting that did nothing |
| 24 | H.264 vs HEVC vs AV1 (Ph. 5) | **Measured — keep H.264** | assumption | measurement | no change needed |
| 25 | Full percentile spread in the snapshot (Ph. 3) | **Complete** | p50, p99 only | + best/p90/p95 | closes a brief item |
| 26 | A/V skew measured (Ph. 22) | **Measured — +1.6 ms** | never measured | measured | governor idle, correctly |
| 27 | Lock + allocation audit (Ph. 18/19) | **Measured — nothing to fix** | argued | measured | — |

---

## Change 1 — honest presentation measurement

### Finding

The one end-to-end video figure the system produces was stamped before the
frame was drawn.

**Location:** `client/gui/video_window.py` — `_check_for_frame`, now
`_on_frame_ready` / `_note_paint`.

**Previous behaviour.** A `QTimer` fired every 5 ms, compared
`decoder.version`, wrapped the decoded bytes in a `QImage`, **recorded
capture→present**, and only then called `update()`. Qt delivered `paintEvent`
later; the backing store reached the compositor later still.

**Why it mattered.** The recorded figure therefore excluded the paint, the
backing-store flush, compositing and scanout. It is consumed in three places:

- the player's OSD,
- `MEDIA_REPORT`, which is what the source displays per client,
- `AudioPlayout.tick_sync`, the A/V governor — which was steering the audio
  buffer against a video number that was systematically too small.

Measured paint cost alone on this PC (1080p RGB888 source):

| target | smooth (as shipped) | fast |
|---|---|---|
| → 960×540 | 1.30 ms p50 / 6.08 p99 | 0.56 / 1.04 |
| → 1920×1080 | 0.53 ms p50 / 3.24 p99 | 0.65 / 1.30 |
| → 2560×1440 | **4.49 ms p50** / 5.90 p99 | — |

So on a fullscreen 1440p player the excluded paint alone was ~4.5 ms, before
the compositor.

### What changed

- The stamp moved to the end of `paintEvent`, after `painter.end()`.
- Two new statistics separate work from waiting: `paint_stats` (the paint) and
  `pickup_stats` (decoder publish → paint begins).
- A repaint with no new frame — a resize, an expose, the OSD toggling — records
  paint cost but **no** latency sample. Counting those would have measured how
  old an unchanged picture was.
- The OSD and the `combined` line now name what is still excluded: the console,
  the capture card, and the compositor. `painter.end()` is the last thing this
  process can see; there is no portable API for the rest.

### What it still cannot see

Composition and scanout. Estimated at 1–2 display refreshes (16–33 ms at
60 Hz) and **NEEDS MEASUREMENT** — it is the largest remaining unknown on the
client, and change #8 is gated on measuring it.

---

## Change 1b — the frame arrives by signal

The 5 ms timer existed only to honour the rule that no worker thread touches
Qt. A queued-connection signal honours the same rule and delivers on the next
turn of the event loop instead of on the next tick.

`VideoDecoder.set_frame_listener` takes a **plain callable**, so
`client/media/decoder.py` stays importable with no Qt — the same rule
`client/net/video.py` follows about PyAV. The window turns it into a signal.

**Measured**, real decoder and real window, 1080p60 stream paced at the frame
interval, 150 samples each, decoder-publish → paint-start:

| | p50 | p95 | p99 | worst |
|---|---|---|---|---|
| 5 ms polling timer (before) | 2.64 ms | 5.31 ms | 5.87 ms | 6.70 ms |
| queued signal (after) | **1.43 ms** | **1.82 ms** | **2.02 ms** | **2.18 ms** |
| improvement | −1.21 ms | −3.49 ms | **−3.85 ms** | −4.52 ms |

The tail matters more than the median here: the old path's delay was spread
across the whole tick interval, so it was a source of jitter as well as delay.
The residual ~1.4 ms is the event-loop turn plus building the `QImage` — work,
not waiting.

The timer survives at **100 ms** as a safety net. It is deliberately far slower
than the frame rate: it is not the delivery path, and speeding it up would
quietly restore the polling latency this removed. A decoder that cannot notify
is a hard error at window construction rather than a silent fall back to 10 Hz
presentation.

---

## Change 1c — a statistic could kill the video session

`LatencyStats.percentile` did `sorted(self._samples)` on a `deque` that another
thread appends to. Iterating a deque during mutation raises
`RuntimeError: deque mutated during iteration`.

Every one of these stats is cross-thread: the decoder writes `decode` and the
receive loop reads it; the GUI writes `present` and the same loop reads it. The
read site in `VideoReceiver._send_report` is not wrapped, so the exception would
escape into `_run`'s handler and put the stream into **FAILED** — a video
session killed by a meter. Change 1 adds two more cross-thread stats, which
would have made it likelier.

`deque.copy()` is a single C-level operation and cannot be interrupted by
another Python thread, so `sorted(self._samples.copy())` is safe. `worst` had
the same hazard via `max()`.

---

## New instrumentation available from this change

Client, in `VideoReceiver.snapshot()` and the OSD:

- `paint_ms` — the paint itself
- `pickup_ms` — publish → paint start
- `video_latency_ms` — now capture → **painted**

Source, in `status()`, so the Pi's web GUI can finally see them:

- `fanout_p50_ms` / `fanout_p99_ms` — **already measured since this class was
  written**, and visible only in the standalone GUI's own snapshot. This is the
  pacing cost (bottleneck #2 below).
- `pickup_p50_ms` — how long a frame waited in the depth-1 slot for the encoder
- `frames_superseded` — frames overwritten unread. Not a fault; it is the
  latest-wins slot working. But it is the difference between "the encoder keeps
  up" and "the encoder runs at half the capture rate", and nothing reported it.
- `source_format` — **what the card actually negotiated**. The requested
  width/height/fps do not say: the open is a ladder of concessions and not one
  rung names a pixel format, so the driver picks.

On the wire, `MEDIA_REPORT` grew an appended, optional presentation block
(`pickup_p50`, `paint_p50`). A peer without it decodes the original fields
unchanged and reports `present_reported: False` — deliberately *not* zero,
because zero is a plausible reading for both and an old peer must not look like
one reporting instant paints.

---

## Change 2 — the real-time buffer is sized in frames

`videoserver/capture.py` asked DirectShow for a flat **`rtbufsize = 64M`**. That
is not a small number once expressed in the unit that matters:

| resolution | 64 MB was | now (3 frames) |
|---|---|---|
| 1080p (**configured here**) | 15.4 frames = **257 ms** | 12.4 MB = 50 ms |
| 720p | 34.7 frames = **579 ms** | 5.5 MB = 50 ms |
| 480p | 104 frames = 1.7 s | 4.2 MB (floor) = 114 ms |
| 4K | 3.9 frames = 64 ms | 49.8 MB = 50 ms |

*(An earlier note in the plan quoted 570 ms for the configured setup. That was
the 720p figure; at the 1080p actually configured it is 257 ms. Corrected.)*

It costs nothing while the capture thread keeps up — the buffer sits near empty.
The hazard is one-directional: once the thread falls behind (a slow MJPEG
decode, a busy machine), the backlog grows and **nothing ever drains it back
out**, so the latency stays for the session. There is no counter for it; the
only evidence is FFmpeg's own "real-time buffer ... too full" line, which by
construction never appears while the buffer is large enough to absorb the
problem.

Sizing it from the frame rather than from a constant bounds it at ~50 ms and
makes overflow *the intended outcome*: a dropped frame plus a diagnostic beats a
smooth, permanently late picture. That is the same trade the depth-1 publish
slot immediately downstream already makes.

**Not yet validated against the real card** — it could not be opened during this
work (`I/O error`, no HDMI signal). The change is a size, not a mode, so the
open behaviour is unchanged; but the first live run should be watched for the
overflow line, and if it appears on a healthy path `_RTBUF_FRAMES` is the one
number to raise.

## Change 3 — the capture decoder no longer asks for frame threading

`stream.thread_type = "AUTO"` carried a comment claiming it was the low-latency
choice. It is the opposite: `AUTO` is `FRAME | SLICE`, and frame threading
delays a decoder's output by *(threads − 1)* frames by construction. It is a
throughput feature. The client's own decoder has always set `SLICE` for exactly
this reason — the two files disagreed, and the capture side was wrong.

**On this card it costs nothing, and that is why it survived.** Measured
capability bits:

| decoder | `FRAME_THREADS` | `SLICE_THREADS` |
|---|---|---|
| mjpeg | no | no |
| rawvideo | no | no |
| **h264** | **yes** | yes |
| **hevc** | **yes** | yes |

The ShadowCast 3 offers only yuyv422, mjpeg, raw RGB and nv12, so `AUTO`
quietly resolved to no threading at all. On a capture card that outputs H.264 —
common, and the obvious upgrade path — it would have been up to three frames,
**50 ms at 60 fps**, with nothing anywhere to say so.

The test for it asserts the premise as well as the string: if `h264` ever stops
advertising frame threading the test says so, rather than silently guarding
nothing.

---

## The GIL investigation — one negative result, one real finding

The client decodes and paints video in the **same process** as the 500 Hz input
loop. CLAUDE.md treats a regression in that loop's tick as a real bug, so the
question is not "how much CPU does video use" but "what does the input loop
feel". Measured with the real loop against a real loopback server, 12 s, 2
synthetic controllers:

| | tick p50 | tick p99 | worst |
|---|---|---|---|
| input loop alone | 0.223 ms | **0.380 ms** | 0.462 ms |
| + 1080p60 decode & paint | 0.231 ms | **2.339 ms** | 2.936 ms |

p50 untouched while p99 grows 6× is the signature of **GIL contention**, not of
CPU load. Something in the video path holds the GIL for longer than the loop's
2 ms period.

### The negative result: it was not the copy

`bytes(plane)` over a 6.22 MB RGB plane looked like the obvious culprit, and in
isolation it is spectacular. Measured against a 500 Hz canary at the input
loop's own rate:

| operation (1080p) | canary p99 |
|---|---|
| nothing running | 0.002 ms |
| `reformat()` yuv420p → rgb24 (swscale) | 0.383 ms |
| **`bytes(plane)` 6.22 MB** | **4.873 ms** |
| `memoryview(plane).tobytes()` | 4.880 ms |
| copy into a **preallocated** bytearray | 4.661 ms |

Two things fall out. swscale is nearly free because PyAV releases the GIL
around it. And **preallocating does not help** — the hold is the memcpy itself,
not the allocation. The plan had assumed a buffer-pool fix; that assumption was
wrong.

So the copy was removed rather than made cheaper: `PresentFrame` now carries a
`memoryview` over the converted frame's plane plus the frame that owns it, and
the window wraps that view. Verified before relying on it — `reformat()` hands
back a distinct, stable buffer per call, so a frame already published is never
written into.

**And at a realistic duty cycle it changed nothing.** Three runs each:

| | tick p99 |
|---|---|
| with the copy | 2.437 / 2.397 / 1.240 ms |
| without the copy | 2.554 / 0.788 / 2.545 ms |

Indistinguishable. The change is **kept** — it removes 1.4 ms of CPU and one
6.22 MB allocation per frame, which at 60 fps is 8% of a core and 373 MB/s of
allocator traffic, and at 4K would be four times that — but it must not be
recorded as a tail-latency win, because it is not one. Left unqualified, the
next person reads the number and believes the problem was solved.

### The real finding: the paint holds the GIL, and scaling is why

Paced at a realistic 60 Hz against the same 500 Hz canary:

| paint | p95 | p99 | worst |
|---|---|---|---|
| nothing painting | 0.001 | **0.008** | 0.079 |
| 1080p → 1280×720, **smooth** (as shipped) | 1.153 | **1.807** | 4.051 |
| 1080p → 1280×720, fast | 0.009 | **0.565** | 1.038 |
| 1080p → 1920×1080, smooth (**1:1**) | 0.008 | **0.509** | 1.130 |
| 1080p → 2560×1440, smooth (fullscreen 1440p) | 3.781 | **4.518** | 5.603 |
| 1080p → 2560×1440, fast | 2.972 | 3.635 | 3.952 |

`QPainter.drawImage` is where the input loop's p99 goes. And the decisive row is
the **1:1** one: at native size the paint is a straight blit with no scaling and
costs the canary almost nothing (0.509 ms), *even with smoothing on*. The cost
is the **scale**, not the smoothing and not the pixel count.

Which means there is a fix that gives up no quality at all: **scale in
`reformat()` instead of in `drawImage()`.** swscale already runs on the decoder
thread, already releases the GIL (0.383 ms p99 above), and resamples at least as
well as Qt's bilinear. Reformat to the window's size and the paint becomes the
free 1:1 case.

The alternative levers are worse. Turning off `SmoothPixmapTransform` buys
1.81 → 0.57 ms but visibly aliases a downscaled game picture. A GPU present path
(`QRhiWidget`) is a much larger change for a similar result on this axis.

---

## Change 6 — the scale moved to where the GIL is free

Acting on the finding above: the decoder now reformats each frame **to the
window's size** rather than to the stream's, so `drawImage` gets a picture it
can blit 1:1 instead of one it has to scale.

Nothing is given up. The conversion to RGB was already a swscale pass, and
swscale resamples in the same pass at no extra cost — and at least as well as
Qt's bilinear. The alternative levers both cost something real:
`SmoothPixmapTransform` off would visibly alias a downscaled game picture, and a
GPU present path is a far larger change for the same result on this axis.

**Measured**, three runs each, real input loop against a real loopback server
with a 1080p60 decode-and-paint pipeline in the same process, window 1280×720:

| | tick p50 | tick p99 | worst |
|---|---|---|---|
| input loop alone | 0.223 ms | 0.380 ms | 0.462 ms |
| scaling in `drawImage` (before) | 0.231 ms | 2.554 / 0.788 / 2.545 ms | 2.94 ms |
| scaling in `reformat` (after) | 0.307 ms | **1.457 / 1.526 / 1.775 ms** | 1.98 ms |

**p99 roughly halves, and the spread collapses** — before, the figure swung
between 0.79 and 2.62 ms across runs; after, it sits in a 0.3 ms band. For a
loop whose whole purpose is predictable timing, that consistency is worth as
much as the median.

**The p50 got worse, by 0.08 ms, and that is a real trade.** The decode thread
does more work per frame now: resampling costs more per output pixel than a
straight colour convert, even though it writes fewer of them. Taking +0.08 ms
on the median to take −1.0 ms off the tail is the right way round for this
project — CLAUDE.md is explicit that p99 is what makes a controller feel broken
— but it is a trade, not a free win, and it should not be recorded as one.

Three details that are load-bearing:

- **The viewport is in physical pixels, and the QImage carries the device pixel
  ratio.** Qt divides an image's size by its ratio when mapping it to the
  logical paint rect, so on a high-DPI display a frame sized in logical pixels
  would be scaled by the ratio at paint time — reintroducing exactly the cost
  being removed, and only on the machines that have the ratio.
- **The viewport is re-synced per frame, not from `resizeEvent` alone.** It is
  two multiplications and a comparison, and it catches the case `resizeEvent`
  does not: a window dragged to a monitor with a different pixel ratio.
  `resizeEvent` still syncs too, so a window resized while the stream is
  *stalled* does not keep asking for the old size until it recovers.
- **Closing the window clears the viewport.** A stream nobody is watching must
  not still be scaling to a window that has gone.

### A test that only passed because the decoder was slow

Removing the copy and giving the decoder its own reformatter made it fast
enough that `test_version_advances_with_each_decoded_frame` began failing: it
decoded 11 of 12 frames before the first checkpoint was read, so the counter
could not advance between the two checkpoints. Its own docstring records an
earlier round of the same problem.

The assertion was never about speed, so the fix is to stop the queue draining
instantly: `FakeReceiver` takes a `pace_s` and this test uses it. Two of the
new scaling tests needed the same treatment. Worth noting because the failure
mode is inverted from the usual one — **a performance improvement broke a test**,
and the tempting reading is that the improvement was wrong.

---

## Change 4 — pace the burst, not every frame

### What the field measurement actually said

Run on the real capture card, encoding with NVENC, delivered to a Raspberry Pi
over **WiFi** — a real network with real queues, not loopback.

| `_PACE_FRACTION` | fan-out p50 | slice loss at the Pi |
|---|---|---|
| 0.5 (as shipped) | 4.70 / 4.71 / 4.69 ms | 0.000% / 0.000% / **0.348%** |
| 0.0 (no pacing) | 0.46 / 0.51 / 0.46 ms | 0.112% / 0.000% / 0.059% |

**The first pair of runs said pacing was working, and it was wrong.** One run
with pacing lost nothing and one without lost 0.112%, which is exactly the
result the pacer's rationale predicts — and it would have been recorded as
confirmation if the pairs had not been repeated. They were, and the worst loss
of the whole set (0.348%) turned out to be a run with pacing **enabled**. The
loss is ambient WiFi variance and has no relationship to the setting.

The lesson is the one this project keeps paying for: **a single pair of runs on
a noisy link cannot distinguish a real effect from the noise**, and the run that
matches the hypothesis is the one least likely to be questioned.

### What changed, and why not simply switch pacing off

Switching it off would have been the wrong answer even though the measurement
allowed it. The structural point stands: a keyframe **is** a genuine burst —
routinely 100+ slices — and pushing that into the socket back to back is what
the pacer was written for. What it could not do was tell a keyframe from an
ordinary frame.

At the settings actually in use, an average frame is 15 slices, and the old rule
paced anything over one 8-slice burst across half a frame interval. So an
ordinary frame — the overwhelming majority — drew the full keyframe treatment:
one 4.17 ms sleep in the middle of its own fan-out, on every frame, forever.

The pacer now sizes an **allowance** from the nominal frame (bitrate ÷ frame
rate, ×1.5) and paces only what exceeds it:

| frame | slices | fan-out |
|---|---|---|
| ordinary (nominal, 16 kB) | 15 | **0.16 ms** — unpaced |
| at the allowance (25 kB) | 22 | **0.08 ms** — unpaced |
| keyframe (120 kB) | 105 | **7.86 ms** — paced, as before |

The allowance is derived from the **configuration**, never from the frame in
hand. A pacer whose behaviour tracked how busy the picture was would produce
content-dependent jitter, which is indistinguishable from a network fault to
whoever has to diagnose it. And it sits at 1.5× rather than 1×, because encoder
output varies around the rate it was asked for — an allowance at exactly the
average puts ordinary frames on both sides of it and makes pacing a coin toss.

### Verified over the same WiFi path

| | fan-out p50 | fan-out p99 | loss at the Pi |
|---|---|---|---|
| old pacer | 4.70 ms | 6.37 ms | 0–0.35% (noise) |
| no pacing | 0.46 ms | 0.87 ms | 0–0.11% (noise) |
| **this pacer** | **0.53 ms** | **0.99 ms** | **0.000%**, 2283 frames / 32 456 slices |

`tests/test_video_pacing.py` pins both halves — that an ordinary frame is not
paced, *and* that a keyframe still is. Reverting to the always-pace rule fails
two of them.

## Change 7 — the capture pixel format is not worth changing

Now measurable rather than inferred. The card negotiates **`yuyv422`**, the
first media type its pin lists, because nothing in the open asks for one.

The case for switching to `nv12` was USB payload: 3.11 MB per frame against
4.15 MB, a 25% saving. The case *against* changing it is that the saving buys
nothing measurable:

- **Conversion cost is a wash.** `yuyv422 → yuv420p` measured 0.82 ms p50;
  `nv12 → yuv420p` measured 0.85 ms. The intuition that nv12 is "closer" to the
  encoder's format does not survive contact with swscale.
- **The USB link is not the bottleneck.** 249 MB/s is comfortable on USB 3, and
  the card delivers a clean 60.0 fps with zero capture errors and zero frames
  superseded — the encoder keeps up completely.

So this would be a change to a working path with no measured benefit and a real
risk: pinning a format the operator's *next* card does not offer turns a working
capture into a failed open. Left alone deliberately. The one thing that was
genuinely missing — knowing which format is in use — is now logged at startup.

---

## Audio (Phases 10, 11, 12) — the untouched half

Every video benchmark in this project ran with `audio_enabled=False`, so the
audio path had **no measurements at all**. `tools/audio_latency_probe.py` now
drives the whole chain on real hardware and reports each stage.

### What the card actually does

The ShadowCast 3's audio pin offers **no 48 kHz mode** — its best is
`ch=2 bits=16 rate=44100` — so every frame is resampled to feed Opus, and the
capture and playback crystals are nominally different rather than merely
independent.

**The 500 ms default is real, on this hardware.** Measured, asking the card for
each size in turn:

| requested | granted | delivery gap |
|---|---|---|
| 5 ms | 5.0 ms | 6.62 ms — irregular |
| **10 ms** | **10.0 ms** | **10.00 ms — clean** |
| 15 ms | 15.0 ms | 19.75 ms — irregular |
| 20 ms (was first on the ladder) | 20.0 ms | 20.00 ms |
| **default** | **500.0 ms** | **499.94 ms** |

So the ladder in `videoserver/capture.py` is load-bearing exactly as documented,
and it now starts at **10 ms**. That matches the Opus frame duration: at 20 ms
each capture frame produced **two** Opus packets, so they left in pairs every
20 ms instead of singly every 10. `packets_per_encode_max` was 2 and is now 1.
5 ms is deliberately not on the ladder — irregular delivery, and it cannot help
while Opus frames are 10 ms.

### The audio latency figure was measured at the wrong point too

`AudioPlayout.feed` recorded capture → *buffered*: the moment a packet was
decoded and queued, **before the jitter buffer and the output device**, which
together are the largest part of the path. Measured: 0.8 ms to that point,
30.4 ms to the speaker.

It is not cosmetic. `tick_sync` compares this figure against the video path's,
and the video figure now runs all the way to the paint (change 1) — so audio
looked ~30 ms earlier than it is, and the old rule's response to "audio is
ahead" was to **grow** the buffer. Under-reporting one side of a comparison
biased the correction toward *more* latency.

### The jitter buffer is now sized by the path

Measured over real WiFi to the Pi, 4501 packets — delay above the fastest:

| p50 | p95 | p99 | p99.9 | max |
|---|---|---|---|---|
| 0.78 ms | 4.43 ms | 5.93 ms | 18.09 ms | 30.41 ms |

The static 30 ms target was sized for the single worst packet in 45 seconds:
about 24 ms of pure latency for the other 99%. The target now follows measured
jitter (a percentile *span*, so it needs no clock sync — a constant offset
cancels), raising by 10 ms and lowering by 2 ms per tick, and raising at once on
any underrun.

**A/V skew may now only shrink the buffer, never grow it.** Growing it to match
a slow video path is adding audio latency for lip-sync, which the brief rules
out for a game.

### The part that made the whole thing cosmetic

Lowering the target **did nothing**, and this is the least obvious result of the
whole session. Audio arrives at exactly the rate it is consumed, so the amount
in flight is *conserved* — established once when priming ends, and unchanged
afterwards. The target only decides how that fixed amount is split. Measured
directly, moving the target mid-stream:

| target | deque | device | **in flight** |
|---|---|---|---|
| 30 ms | 0.0 | 20.0 | **20.0 ms** |
| 20 ms | 10.0 | 10.0 | **20.0 ms** |
| 10 ms | 20.0 | 0.0 | **20.0 ms** |

A governor that moved the number and changed nothing audible. `_shed()` now
gives up the difference when the target is lowered, from the front of the
deque — the same mechanism `_track_drift` already uses, and small enough
(2 ms per tick) to be a skip rather than a gap.

### Result

| | capture→speaker p50 | p99 | underruns |
|---|---|---|---|
| before | 30.63 ms | 31.31 ms | 0 |
| after | **22.42 ms** | **28.48 ms** | 0 |

`MIN_TARGET_MS = 20` is now the binding constraint, not the measurement — the
path wants roughly 16 ms. Lowering the floor is deliberately **not** done yet:
it trades directly against underruns, and validating it needs the impairment
harness (Ph. 8/9/23), which does not exist yet. Reducing a buffer without the
tool to prove it still absorbs a bad path is how the documented audio faults
happened in the first place.

### Two things measured and left alone

- **Opus 10 → 5 ms frames** (the brief asks). The capture probe shows 5 ms
  delivery is irregular on this card (6.62 ms gaps), so the source cannot feed
  it cleanly; and it would double the packet rate against ~70 bytes of
  IP+UDP+AEAD overhead per packet.
- **`BURST_HEADROOM_MS` (600 ms)** stays. It is a ceiling, not a target, costs
  nothing on a clean path, and CLAUDE.md records what shrinking it did before.

---

## Phase 16 — the poll rate, and the spin it was hiding

### The sweep

`tools/latency_harness.py` already took `--poll-hz`, so 500 Hz being right was
testable and had never been tested. Two synthetic controllers, 12 s each, with
`axis_deadband=0` so every tick sends -- the worst case for packet rate, and
what a player moving a stick continuously actually produces.

**RTT is deliberately not in this table.** It is biased upward by up to one poll
period by construction, so a sweep measured on RTT would show an "improvement"
that is only the bias shrinking. These are figures taken between two stamps on
one clock.

| rate | sampling delay | tick p99 | server proc p50 | CPU | pkt/s |
|---|---|---|---|---|---|
| 125 Hz | 4.00 ms | 0.494 ms | 0.049 ms | 11.1% | 292 |
| 250 Hz | 2.00 ms | 0.398 ms | 0.041 ms | 22.5% | 546 |
| **500 Hz** | **1.00 ms** | **0.428 ms** | **0.040 ms** | **41.7%** | **1048** |
| 1000 Hz | 0.50 ms | 0.299 ms | 0.035 ms | 66.9% | 2049 |

Every rate is achieved exactly, 1000 Hz included -- the pacing is not the limit.
The limit is what each rate costs.

**500 Hz stays.** Going to 1000 Hz buys 0.5 ms of sampling delay for +25 points
of CPU and twice the packet rate, against a Bluetooth floor of 7.5–30 ms that
we do not control: it is 2–7% of a number nothing can reduce. Going down to
250 Hz gives back 19 CPU points for +1 ms, which is a reasonable trade on a
weak client -- and `poll_hz` is already configurable, so that choice exists
without a code change.

### What the sweep actually turned up

The CPU column was the interesting one. At 500 Hz the client was burning **74%
of a core**, and almost none of it was work: `sleep_until_ns` spins the last
`_SPIN_THRESHOLD_NS` of every period, and that spin is `time.sleep(0)` -- which
drops and retakes the GIL every iteration, in a process that is also decoding
1080p60 video.

The threshold was 1.5 ms, justified by a comment saying sleep "cannot resolve
finer than ~1 ms even with `timeBeginPeriod(1)`". **That is no longer true.**
Measured on the reference machine, sleep overshoot is identical with and
without `timeBeginPeriod(1)` -- modern Windows and CPython already use a
high-resolution waitable timer:

| | p50 | p99 | worst |
|---|---|---|---|
| overshoot, no `timeBeginPeriod` | 0.504 ms | 0.625 ms | 1.014 ms |
| overshoot, with `timeBeginPeriod(1)` | 0.504 ms | 0.624 ms | 0.635 ms |

So the threshold was sized for a constraint that had gone away.

**Measured against the right metric.** The first sweep used tick *duration* and
showed almost no difference down to 0.25 ms -- but tick duration is the work per
tick, and a loop can be perfectly paced and uniformly *late* while the rate
still reads exact. Re-measured as wake-up lateness against the scheduled
deadline, at 500 Hz:

| spin | late p99 | worst | CPU |
|---|---|---|---|
| 1.50 ms (was) | 0.001 ms | 0.014 ms | 67.1% |
| 1.00 ms | 0.001 ms | 0.052 ms | 45.8% |
| **0.75 ms (is)** | **0.003 ms** | **0.240 ms** | **14.8%** |
| 0.50 ms | 0.052 ms | 0.166 ms | 9.9% |
| 0.25 ms | 0.116 ms | 0.230 ms | 3.1% |

0.75 ms buys a **4.5x CPU reduction for two microseconds of p99 lateness**.
Below it the tail degrades sharply for very little further saving.

### And it closed the GIL problem

This is the payoff, and it was not the reason the sweep was run. Less spinning
means far fewer GIL handoffs, so the input loop stops fighting the video
threads. Input tick p99 with a 1080p60 decode-and-paint pipeline in the same
process:

| | tick p50 | tick p99 |
|---|---|---|
| no video at all | 0.223 ms | 0.380 ms |
| original | 0.231 ms | **2.339 ms** |
| after scaling moved to `reformat` | 0.307 ms | 1.53 ms |
| **after the spin threshold** | **0.234 ms** | **0.96 ms** |

The p50 regression the scaling change cost (+0.08 ms) is gone as well. Total
improvement on the input loop's tail while video runs: **2.34 → 0.96 ms**, and
it is now within a factor of 2.5 of the no-video baseline rather than 6.

---

## The impairment harness

`tools/impair.py` is a UDP relay that degrades a path on purpose: loss, fixed
delay, jitter, duplication, and a token-bucket bandwidth cap. Phases 8, 9 and
23 all need it -- congestion control cannot be measured on a path that never
congests -- so it was built once, and `tests/test_impairment.py` tests it as an
*instrument* before anything is concluded with it: asking for 20% loss must
produce about 20% loss, a clean relay must be transparent, and a seeded run
must reproduce.

**The bandwidth cap queues rather than drops**, which turned out to be the
important design decision. A rate limiter that only drops is a loss generator
wearing a hat; the queueing is the thing congestion control has to notice. The
bucket's `burst_bytes` *is* the modelled buffer depth, and that distinction
decided the whole experiment below.

## Phase 8 — congestion seen as delay, before it becomes loss

`tick_governor` reacted only to loss, on 5-second windows. **Loss is a lagging
indicator**: by the time a router drops, its queue has been full for some time
and every packet crossing it has been paying that delay.

The client now measures one-way delay per slice, tracks the minimum per report
window against a 20-window baseline, and reports how far above its own best the
path is sitting. Absolute values need no clock accuracy -- a constant offset
cancels out of a difference. The minimum is used rather than the mean because a
queue raises the *floor*, while jitter only widens the spread above it; the
mean would make a bursty but uncongested path look congested.

### Two things measurement changed about the design

**A shallow cap is not bufferbloat.** The first run used a 48 kB burst against a
3900 kbps cap and produced 26.9% loss with almost no queue -- which models a
*well-managed* small buffer, not the problem. Bufferbloat needs an oversized
buffer: 1 MB at 5000 kbps is ~1.6 s of queue, and that is what showed a 1592 ms
standing delay.

**The delay check has to be faster than the loss check, or it never fires.**
With the queue check behind the same 5 s gate as the loss check, the bloated
buffer filled in about three seconds -- so there was never a tick where delay
was high and loss was still zero. Loss won every time and the "early" signal
was decoration. It now runs at 1 Hz, matching the report rate, and confirms
over two consecutive readings.

### Measured on real hardware

Real capture card, NVENC, 1280x720@60 asking for 8000 kbps (~7800 actual),
through a 5000 kbps cap with a 1 MB buffer:

| | loss-based only (before) | delay-based (after) |
|---|---|---|
| slices lost at the client | **985** | **0** |
| relay queue drops | **985** | **0** |
| peak standing queue | 1592 ms | 1242 ms during convergence, then ~1 ms |
| bitrate outcome | collapsed to **1423 kbps** | converged to **4725 kbps** |

The entire congestion episode is now handled **without a single dropped
packet**, and the bitrate settles near the true capacity instead of
overshooting to a third of it. The loss path is deliberately kept: delay-based
detection is blind to a path that drops rather than queues.

The threshold is 40 ms, chosen against the measured WiFi jitter (p99 5.93 ms,
p99.9 18.09) so ordinary variation cannot trip it -- a governor that throws
away bitrate on a healthy path is worse than one that reacts late.

## A bitrate change was reopening the capture card

Found while reading the governor. `apply_config` is explicit about this --

> a bitrate change, which the governor makes on its own, repeatedly, must never
> cost a device reopen

-- and `_apply_bitrate` then called `_restart_media()`, which does exactly that.
It is the governor's own action, so it happens most during congestion, which is
when the stream can least afford a gap: the measured loss-based run made eight
reductions in one minute and therefore eight capture reopens. A reopen can also
fail outright if the previous capture has not released the device, turning a
bitrate adjustment into a dead stream.

It calls `_restart_encoders()` now, which is what the surrounding code always
intended.

---

## Phase 9 — loss recovery

### What loss actually cost, measured first

A lost slice loses the whole frame; the assembler sees a gap and the client
asks for a keyframe -- the largest and most loss-prone frame there is. Measured
through the impairment harness at 1280x720@60, ~11 slices per frame:

| slice loss | decoded fps | frames dropped | IDR requests | keyframes / 25 s | decoder resets |
|---|---|---|---|---|---|
| 0.0% | 60.0 | 0 | 0 | 13 | 0 |
| 0.5% | **51.6** | 75 | 45 | **40** | 72 |
| 1.0% | **45.7** | 125 | 59 | **47** | 121 |
| 2.0% | 29.7 | 240 | 79 | 49 | 295 |
| 5.0% | 18.6 | 521 | 89 | 49 | 326 |

**Half a percent of slice loss cost 14% of the frame rate**, and 1% cost 24% --
far more than the arithmetic of "1% of slices" suggests. Three effects compound:
a frame dies to one missing slice, its broken reference chain kills the frames
behind it, and the repair keyframe is both the biggest frame and the one most
likely to be hit itself. Keyframes went from the scheduled 13 to 40-49, which is
the 500 ms IDR rate limit saturating: the source spends the episode emitting
nothing but expensive frames.

### Why parity and not retransmission

`NACK` was the other candidate and is rejected on the one property that matters
here: **it needs a round trip.** Whether a retransmit is useful depends entirely
on whether the reply beats the frame's display deadline -- at 60 fps that is
~16 ms, so it works on a LAN and is useless on a 60 ms internet path, which is
exactly the path this deployment uses. It also requires the source to buffer
recent slices.

XOR parity needs no round trip at all, so it behaves identically at 1 ms and at
100 ms of RTT. One parity slice recovers exactly one lost slice per frame, which
matches how sparse independent loss actually arrives.

### The design, and the one detail that would have corrupted frames silently

`SliceFlags.FEC` is set on **every** slice of a protected frame, not just the
parity -- the receiver has to know a frame is protected even when the slice that
went missing *is* the parity slice. The parity is always the last index, so
`data_count = slice_count - 1`.

XOR needs equal-length operands, so the parity is computed over slices padded to
`VIDEO_SLICE_PAYLOAD`. Every data slice is full length **except the last**, so
rebuilding the last one from padded parity yields a slice with zeros on the end
-- which appends garbage to the frame and decodes to nonsense rather than
failing. The parity payload therefore carries a u16 with the true length of the
last data slice. It fits: the slice was 1194 bytes on the wire against a 1200
cap, and this uses 2 of the 6 spare.

It is **adaptive**, not always on. Parity costs one slice per frame -- 9.1% at
6000 kbps/60 fps, 6.7% at 8000 -- which buys nothing on a clean path, so it is
switched on from what clients report losing (on above 0.2%, off below 0.05%
after 15 clean checks). The hysteresis is wide because each change alters the
encoder's effective budget, and loss arrives in bursts.

### Result

Same runs, parity enabled:

| slice loss | decoded fps | keyframes / 25 s | decoder resets | frames rebuilt |
|---|---|---|---|---|
| 0.0% | 60.0 | 13 | 0 | 0 |
| 0.5% | **59.9** (was 51.6) | **14** (was 40) | **0** (was 72) | 90 |
| 1.0% | **59.7** (was 45.7) | **16** (was 47) | **0** (was 121) | 156 |
| 2.0% | **55.4** (was 29.7) | 24 (was 49) | 45 (was 295) | 253 |
| 5.0% | **51.0** (was 18.6) | 47 (was 49) | 66 (was 326) | 433 |

**Up to 1% loss the stream is effectively undamaged** -- full frame rate, the
scheduled keyframe count, and not one decoder reset. The loss spiral is broken:
because frames survive, no keyframe is requested, so the expensive frames that
made loss worse never get emitted.

At 2% and 5% two slices in one frame become common (parity recovers one), so
damage returns -- but 51 fps against 18.6 is still a different experience.

**What it does not do**, stated so nobody expects otherwise: two lost slices in
one frame is still a lost frame, exactly as every frame is today. `recovered`
is reported so the difference between "parity is earning its bandwidth" and
"parity is being paid for and not helping" is visible rather than assumed.

---

## The loss signal was a lifetime average

Found by running the adaptive parity switch on real hardware rather than
trusting it: parity turned **on** correctly when loss appeared, and was still on
twenty-five seconds after the path was completely clean.

`_worst_loss()` divided the client's `slices_lost` by `slices_received` -- and
both are **lifetime counters**. So the figure was the loss rate *since the
session began*, which is not a control signal at all:

- it can never fall back after one bad patch, so anything keyed to a low
  threshold latches on permanently;
- it is diluted by history, so it under-reports what is happening now. Measured:
  an injected 1.5% loss was reported as **0.22%** on the first detection.
  Windowed, the same condition reads **1.38%**.

The bitrate governor reads the same number, so it would equally have held a
reduced bitrate on a path that had long since recovered -- it only escaped
notice because its threshold is 5%, high enough that the lifetime average
usually fell below it eventually.

It is now differenced between samples, taken **once** per governor pass with
both consumers reading the stored value. A differencing measurement that
consumed its own state would have given whichever caller ran first the real
number and the other a zero -- the same shape as the config-push flag CLAUDE.md
already documents. A client whose counters go backwards has reconnected and its
window is skipped rather than producing a negative rate, and state for departed
clients is released rather than accumulating for the life of the process.

### And the fixture that broke three times

The governor's state was enumerated by hand in a test, so every field added to
it broke that test -- three times across this session. `_init_governor()` now
holds all of it in one call, which the test uses. A control loop's state should
be constructible in one call.

---

## Phase 23 — failure and recovery

`tools/recovery_harness.py` measures the same quantity **three times** -- before,
during, and after a fault -- because the way these optimizations go wrong is not
a crash. It is a jitter buffer that grows under load and stays grown, or a
bitrate that drops in seconds and returns over minutes: both look healthy on
every counter and feel broken to play.

### It cleared the riskiest change

Lowering `_SPIN_THRESHOLD_NS` traded margin for CPU, so the fair question is not
only whether the loop survives contention but whether it comes back. Against
four GIL-competing Python threads:

| spin | p50 | p95 | p99 | worst |
|---|---|---|---|---|
| 1.50 ms (was) | 16.30 | 68.18 | 117.31 | 133.41 ms |
| **0.75 ms (is)** | 16.61 | 67.66 | **81.19** | **82.18 ms** |

Indistinguishable at the median and *better* at the tail, and lateness returns
to 0.00 ms the moment the pressure lifts. The change is clean.

### Video recovery is excellent up to the give-up threshold, then absolute

Total blackout, real hardware:

| blackout | state during | first frame after | fps after |
|---|---|---|---|
| 1.0 s | STREAMING | **10 ms** | 60.3 |
| 2.0 s | STREAMING | **21 ms** | 60.3 |
| 4.0 s | STALLED | **20 ms** | 59.8 |
| 7.0 s+ | FAILED | never | 0.0 |

Under `_FAIL_AFTER_NS` (8 s) recovery is essentially instant. Past it the
receive loop exits by design and rebuilding the session is the client layer's
job -- the harness has no GUI, which is why it read as a total failure. That
boundary is paired with the server's 10 s session reap, so it is deliberate, but
it is worth stating plainly: **an outage longer than eight seconds costs a full
handshake, Argon2id included.**

### The real find: recovery took minutes

Down was fast and up was glacial -- not by a factor of a few, but of a hundred.
`_RECOVERY_INTERVAL_NS` was 30 s with a +10% step, so climbing from the floor to
6000 kbps needs sixteen steps: **eight minutes**. Measured on hardware, after a
thirty-second congestion episode cleared, the bitrate was still at 2256 of 6000
three minutes later.

Now 5 s and +25%:

| | before | after |
|---|---|---|
| time to climb back | **>180 s** (reached 2256) | **20 s** (reached 6000) |

The brisk ramp is only safe because of Phase 8: overshooting into congestion is
now detected by delay within two seconds, long before loss. It would not have
been a defensible change when loss was the only warning.

### And a flaw in the instrument itself

The bitrate scenario first reported a clean pass with the bitrate never moving.
`rate_kbps` was read **once**, at construction, so turning congestion on halfway
through a scenario silently did nothing -- `loss` and `jitter` are read per
datagram and always worked. An instrument that ignores a setting is worse than
one that refuses it, because it reports a pass. The bucket is rebuilt when the
rate changes, and `tests/test_impairment.py` now pins that every setting is
live mid-run.

---

## The audio floor: measured, and left alone

`MIN_TARGET_MS = 20` became the binding constraint once the jitter buffer went
adaptive -- on a clean path the governor drives straight to it. The impairment
harness made lowering it testable, so it was tested: a clean path with a 35 ms
jitter burst for 1.5 s every 20 s, one process per floor, 60 s each.

| floor | settled | underruns | at the floor | heard p50 | heard p99 |
|---|---|---|---|---|---|
| 20 ms | 20 | 2 | 41% | **26.6 ms** | 51.1 |
| 16 ms | 16 | 4 | 37% | **26.8 ms** | 53.2 |
| 12 ms | 12 | 3 | 35% | **26.5 ms** | 50.7 |
| 10 ms | 11 | 5 | 0% | **27.4 ms** | 56.5 |

**Latency is flat and underruns rise.** Lowering the floor buys nothing and
costs glitches, so it stays at 20 ms. The earlier note that "the path wants
roughly 16 ms" was reading the jitter measurement alone -- the *jitter* wants
16 ms, but what is actually heard is set by the output device, and under real
bursts the governor spends most of its time above the floor anyway.

Two earlier attempts at this sweep were invalid and are worth recording,
because both produce confident-looking numbers:

- **Bursts every 6 s meant the floor was never reached.** The governor's
  measurement window is 5 s, so it stayed contaminated and settled at ~45 ms
  whatever the floor was set to. A floor sweep in which the floor never binds
  measures nothing.
- **Sequential runs in one process fought over the capture device**, logging
  "the audio capture is still holding its device" every time, so each run
  measured a pipeline that had partly failed to start. A synthetic source was
  tried as a fix and rejected: lavfi does not pace like a capture device and
  pinned the playout at its 600 ms overrun cap.

## The bug the sweep actually found

Pinning the target below the output device's period:

| target | deque | device | heard p50 |
|---|---|---|---|
| 12 ms | 8 ms | 10 ms | 10.4 ms |
| 10 ms | 12 ms | 10 ms | 12.5 ms |
| **8 ms** | **602 ms** | 8 ms | **604.4 ms** |

`_pump_once` tops the device up to `target_ms` and keeps the rest in the deque.
When the **device holds more than the target**, that top-up is never positive:
only trickles get written, the deque grows to its overrun cap, and latency
becomes 600 ms. `MIN_TARGET_MS = 20` was the only thing preventing it -- which
nothing documented, no test covered, and which is only sufficient for devices
whose period is under 20 ms. **Bluetooth headphones are routinely 40 ms or
more**, and on one of those this fires at the default target.

Two fixes were tried and rejected, both treating the symptom:

- *"Write at least one frame whenever there is room."* Clears the deadlock and
  drains the deque every pass, so the jitter reserve stops existing and `_shed`
  has nothing to take -- the adaptive governor disconnected from its own
  output. Measured: the buffer sat at the device's period whatever the target
  said.
- *"Write if nothing has gone out for 50 ms."* Never fires, because small
  writes *do* happen whenever the device dips a byte below the target. The
  deadlock is writes smaller than arrivals, not an absence of writes.

The cause is simply that the target is below what the device can achieve, so
the device's floor is now **learned from the condition itself** and the target
clamped to it. Self-calibrating, because no API reports the figure, and dormant
on hardware that behaves -- `device_floor_ms` stays 0 and is reported, so a
machine where it fires can be told apart from one where it never did.

Verified on hardware after the fix: at targets of 10, 8 and 6 ms the floor is
learned as 20 ms, the deque stays at ~0.3 ms, and there are no underruns. The
600 ms balloon is gone.

---

## Audio loss recovery (Phase 12)

Video gained parity in Phase 9; audio had **nothing**. A lost Opus packet did
not arrive, the counter went up, and the playout carried on with the next one
-- so 10 ms of audio vanished and everything after it moved 10 ms earlier.

**Opus's own mechanisms are unreachable through PyAV, measured not assumed:**

| | result |
|---|---|
| encoder `fec=1`, `packet_loss=10` | accepted -- redundancy *can* be produced |
| decoder `decode_fec` | not exposed by PyAV, so it could never be used |
| `decode()` on an empty packet | returns no frames -- libopus is never asked to conceal |

In-band FEC would therefore be transmitted and ignored: pure overhead.

**Packet-level redundancy was rejected separately.** Re-sending the previous
payload alongside each packet does recover the loss, but the copy arrives
*after* the packet that follows it, and this playout is a byte deque with no
reordering. Making it sequence-aware costs one packet of delay -- 10 ms,
permanently, on every stream -- to fix an event that happens once a second at
1% loss. For this project that is the wrong way round.

So the gap is filled locally: repeat the last frame, fading out, capped at
three packets. It does not recover the audio, but it removes the click and
keeps the timeline honest at no latency cost.

### The result, on real hardware through the impairment relay

35-second runs, real capture card, measuring how much audio actually reached
the device against how much wall time passed:

| concealment | loss | packets lost | concealed | **timeline shortfall** | **underruns** |
|---|---|---|---|---|---|
| off | 1.0% | 39 | 0 ms | **372 ms** | **10** |
| off | 3.0% | 116 | 0 ms | **1055 ms** | **24** |
| **on** | 1.0% | 38 | 380 ms | **10 ms** | **0** |
| **on** | 3.0% | 104 | 1040 ms | **12 ms** | **0** |

The shortfall is the finding. At 3% loss the audio ran **over a second short in
thirty-five seconds** -- cumulative, invisible to every counter, and dragging
audio steadily ahead of video for as long as the session lasts. Concealment
reduces it to measurement noise.

**Underruns going to zero was not predicted.** The missing audio was starving
the output device: filling the gaps keeps it fed, so a fault that presented as
"the speaker occasionally runs dry" was really "the stream is short of audio".

## `intra_refresh` was a setting that did nothing

The flag was applied only inside the `libx264` branch of
`configure_low_latency`. On any machine that picks NVENC -- every machine with
an NVIDIA card, including this one -- switching it on in the web GUI changed
nothing and said nothing. Same shape as the `vendor_id` / `product_id` and
`advertise_host` dead parameters CLAUDE.md already documents.

`intra_refresh_options()` now states support per encoder, verified on hardware
for libx264 and h264_nvenc and taken from documentation for QSV and AMF --
neither of which can be opened on this machine, which is why `_build_context`
retries without them. **Intra refresh is dropped first and separately**, so an
encoder that dislikes it does not also cost the keyframe options, which every
mid-stream joiner depends on. `h264_v4l2m2m` deliberately declares nothing:
claiming an option it does not have would be the original bug in a new place.

## H.264 vs HEVC vs AV1

The brief asks for this explicitly, and warns that AV1 is not automatically
better for being newer. What matters here is the *pair*: the client decodes in
**software**, so a codec that encodes fast on the GPU and decodes slowly on the
CPU is a loss however well it compresses.

Measured on **real capture-card frames**, 1280x720@60, asking for 6000 kbps:

| codec | encode p50 | decode p50 / p99 | bitrate |
|---|---|---|---|
| **H.264 (NVENC)** | 0.89 ms | **2.21 / 2.68 ms** | 5226 kbps |
| HEVC (NVENC) | 0.87 ms | 3.23 / 3.58 ms | 5365 kbps |
| AV1 (NVENC) | 0.88 ms | 2.34 / **7.52 ms** | **6098 kbps** |
| H.264 (libx264) | 0.67 ms | 0.79 / 1.08 ms | 4033 kbps |

**H.264 stays.** HEVC costs 46% more decode for no bitrate advantage at all,
and AV1 is worst on both axes.

**A first run on a synthetic gradient said HEVC compressed 2.2x better**, which
would have been a strong argument for it. That was an artifact of the pattern;
on real content the advantage vanishes entirely. A codec comparison run on
made-up content flatters whichever codec happens to suit it.

### One lever the comparison exposed, and why it is not being pulled

The entropy coder explains most of NVENC's decode cost:

| | decode p50 | bitrate |
|---|---|---|
| NVENC, CABAC (default) | 2.34 ms | 5193 kbps |
| NVENC, **CAVLC** | **1.65 ms** | 5122 kbps |

CAVLC decodes 29% faster and, on this content, cost no bitrate. It is left
alone: CABAC is generally 5-10% more efficient, one content sample cannot
overturn that, and 0.7 ms against a ~40 ms budget is not worth risking a
bitrate increase on a congested path. Recorded so the decision has data behind
it rather than being made twice.

Also worth noting: libx264 beat NVENC on **both** decode cost and bitrate.
That is a genuinely open question about encoder preference -- hardware frees
the CPU, which matters for the embedded-on-a-Pi case -- and is not the same
question as which codec to use, so it is left as a finding rather than acted on.

---

## Closing the three remaining brief items

### The snapshot was narrower than the measurements (Phase 3)

The brief asks for "median, P90, P95, P99, minimum, maximum". `LatencyStats`
computed all of them and `snapshot()` returned `last, mean, p50, p99, worst,
count` -- so every measurement in this work reported p90 and p95 by calling
`percentile()` directly, while the figures actually reaching the GUIs and the
WebSocket feed stopped short of them. p95 is where the video path's tail shows
up before p99 does.

`best`, `p90` and `p95` are now in the snapshot. The shape stays **pinned
exactly** by a test: it is consumed by three front ends, so a key appearing or
vanishing is a change to something other people read.

### A/V skew, measured for the first time (Phase 22)

The policy was reviewed and changed earlier -- A/V skew may only *shrink* the
audio buffer, never grow it, because growing it means adding audio latency to
match slow video. But the skew itself had never been measured, and
`_SYNC_TOLERANCE_MS = 45` was inherited rather than chosen from data.

**The probe silently could not measure it.** `present_stats` is populated by
`VideoWindow` at paint time and by nothing else, so an audio probe with no
window read a skew of exactly zero -- which prints as "perfectly in sync"
rather than as "not measured". The probe now runs the real video path
(offscreen) so the number is real:

```
video capture -> painted   p50 20.78  p95 28.69  p99 30.47 ms
audio capture -> speaker   p50 22.43  p95 22.80  p99 24.39 ms
skew (audio - video)       +1.6 ms p50   (audio behind)
governor tolerance         +/-45 ms  ->  INSIDE, governor idle
```

**The two paths are aligned to 1.6 ms**, so the governor correctly never fires
and is not oscillating against a tolerance it cannot meet. Both figures exclude
the capture card and the compositor, which affect the video side only -- so the
true skew is larger than this and in the same direction the governor is already
forbidden from "fixing".

### Locks and allocations (Phases 18 and 19)

Previously argued from the outcome -- a 0.96 ms input tick p99 with a full
pipeline in the same process -- which is not the same as having looked.

**Locks.** Measured acquisition cost:

| | p50 | p99 | worst |
|---|---|---|---|
| `threading.Lock`, uncontended | 0.200 us | 0.300 us | 1.50 us |
| `threading.Lock`, 3 contenders | 0.200 us | 0.300 us | 3.10 us |
| `threading.RLock`, uncontended | 0.200 us | 0.300 us | 0.70 us |

Contention does not move the median at all, and the worst case is 3.1 us
against a 2 ms tick period. The hot paths take one lock per tick (input), per
packet (audio) or per frame (video), so the total is a few hundred microseconds
a second. Nothing to reclaim.

No lock is held across I/O on any media path -- `send_audio` builds its packet
under `_audio_lock` and sends outside it, `_take` releases before
`sink.write`, and both publish paths hold only a rebinding. The one exception
is `HIDServer._io_lock`, which deliberately wraps a syscall and whose reasoning
and deadlock test CLAUDE.md already documents.

**Allocations.** Two different questions, and the first measurement only
answered one of them. `sys.getallocatedblocks()` is a *net* count, so an object
allocated and freed inside a loop is invisible to it: it proves there is no
leak, not that there is no churn. Churn was then measured separately by
counting gen-0 collections, which fire every `threshold0` allocations.

| operation | net blocks/op | tracked allocs/op |
|---|---|---|
| `backend.poll` | 0.00 | **0.00** |
| `state.differs_from` | 0.00 | **0.00** |
| `profile.build_input_report` | 0.00 | **0.00** |
| `encode_video_slice_into` | 0.00 | **0.00** |
| `encode_audio_frame_into` | 0.00 | — |
| `transport.send_input` (full send) | 0.06 | **0.00** |

**Zero gen-0 collections across 100,000-200,000 iterations of every hot path.**
Untracked `bytes` churn does exist -- the AEAD output, for one -- but untracked
objects cannot cause a GC pause, which is the property that matters for tail
latency, and that cost is already inside the measured 0.069 ms of
`send_input`. The allocation discipline the conventions claim is real.

---

## Outstanding bottlenecks, ranked

| # | Bottleneck | Est. impact | Confidence |
|---|---|---|---|
| 1 | Capture card + UVC driver pipeline | 20–100 ms | **NEEDS MEASUREMENT** |
| 2 | Compositor + scanout after `painter.end()` | 16–33 ms | **NEEDS MEASUREMENT** |
| 3 | Fan-out pacing, `_PACE_FRACTION = 0.5` | ~4.2 ms/frame | derived; `fanout_ms` now exposed |
| 4 | Residual GIL contention from decode + paint | input tick p99 0.38 → 0.96 ms | **measured**, after the spin fix |
| 6 | x264 encode at 1080p60 on the capture PC | ? | `encode_p50_ms` exists — read it |
| 7 | ~~Congestion control keyed on loss~~ | **fixed** — see Phase 8 above | **measured** |
| 8 | Capture pixel format left to the driver | USB payload | pin list read; runtime unconfirmed |

### The source-side budget, measured on real hardware

1080p60, 8 Mbps, NVENC, real capture card, 25 s:

| stage | p50 | p99 |
|---|---|---|
| capture → encoder pickup | **0.024 ms** | 0.044 ms |
| encode (incl. yuyv→yuv420p convert) | **3.44 ms** | 4.30 ms |
| fan-out (was 5.50, now) | **0.53 ms** | 0.99 ms |
| client decode | 3.75 ms | 5.12 ms |

`frames_superseded = 0` over 1470 frames: the depth-1 slot never had to discard
anything, so the encoder is keeping up with the card completely. The pickup
figure confirms the slot costs essentially nothing.

---

## Things checked and found already correct

Recording these so they are not re-litigated:

- Depth-1 latest-wins slots at capture→encode, encode→send, and decode→paint.
- The client's 2-deep completed-frame queue, which drops oldest.
- `FrameAssembler` latest-wins; `capture_ts` in every slice.
- Encoder: no B-frames, no lookahead, `scenecut=0`, tight VBV, in-band SPS/PPS,
  `sliced-threads` — and the probe-then-rebuild that keeps the opening IDR.
- Client decoder on `thread_type = "SLICE"`, deliberately not frame threading.
- Opus at 10 ms with `application=lowdelay`; audio sent inline, unqueued.
- The audio reserve accounting and the drift governor keyed on rolling minimum.
- Two sockets, so input never queues behind video (Phase 17 is already met).
- The whole controller path — ~0.25 ms of software cost.

## Known flake, pre-existing

`tests/test_videoserver_pipeline.py::TestMediaSession::test_received_frames_actually_decode`
fails roughly 1 run in 6, **on the unmodified tree as well** (verified by
stashing). Not caused by this work, but worth fixing before it masks a real
regression in exactly the area being changed.

---

## A caution about the harness numbers

`tools.latency_harness` moves with the machine's state, not only with the
code. The same unmodified tree measured tick p50 0.240 ms early in this
work and 0.298 ms after an afternoon of benchmarking on the same box --
a 24% swing from thermals and background load alone, which is larger than
most of the changes here.

So **always A/B against a stashed tree in the same session**, never against
a number written down earlier. Doing that here showed the controller path
unchanged by this work:

| | tick p50 | tick p99 |
|---|---|---|
| unmodified tree, 3 runs | 0.298 / 0.293 / 0.319 ms | 0.558 / 0.575 / 0.547 ms |
| with all changes, 3 runs | 0.303 / 0.295 / 0.291 ms | 0.525 / 0.527 / 0.525 ms |

## How to reproduce these measurements

```bash
pytest tests/ -q                       # full suite
pytest tests/test_client_video.py -q   # the paint-time stamping tests
python -m tools.latency_harness        # controller path must not regress
```

Field check:

```bash
# capture PC: read the new "Capture source format:" line at INFO
RBGC_PASSWORD=... python -m videoserver.main --media-bind 0.0.0.0:47810 -v

# Pi
ssh -i ~/.ssh/rbgc_pi_fixed spencer@controller-server 'journalctl -u rbgc-server -f'
```

Then read `paint`/`wait` on the client OSD (`L` toggles it) and
`fanout_p50_ms` / `source_format` in the server's video status.

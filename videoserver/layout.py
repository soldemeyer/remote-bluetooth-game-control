"""Deciding whether the console is showing one picture or several.

Runs on the video server, on sampled frames, off the encode path. What it does
*not* do is as important as what it does: it knows nothing about controllers,
clients or who is watching. It answers one question -- how is this picture
divided -- and publishes the answer. ``server/screen_state.py`` does the rest.

Cost, and why it is shaped this way
-----------------------------------
The frame is reformatted to **gray** at a few hundred pixels wide before
anything looks at it, so FFmpeg does the scaling in C and the analysis walks a
few tens of kilobytes rather than a few megabytes. One 8-bit plane, no chroma
to step over -- a seam is a luma discontinuity and colour adds nothing.

Measured on the reference machine: **3.5-3.9 ms per frame**, and flat across
1280x720, 1366x768 and 1920x1080 because the downscale happens first and
everything after it works on the same 320-wide plane. At the default 2 Hz that
is **0.75% of one core**, paid by the video server and never by the encode
path.

**No numpy.** ``videoserver/encode.py`` already states the rule for the audio
level meter -- "no numpy, which the video server does not otherwise require" --
and the video extra is PyAV alone. Adding it here would put it in the Windows
PyInstaller bundle and the Pi AppImage for one function's benefit.

**Its own ``VideoReformatter``.** Never ``frame.reformat()``: capture hands the
same object to the encoder and both previews, and that call runs through a
scaler cached *on the frame*. Two threads inside it wedge one of them
permanently -- see the measurements in CLAUDE.md. Owning a reformatter is the
fix; the lock around ``encode_preview`` is belt and braces.

How a split is recognised without knowing the game
--------------------------------------------------
Adjacent columns of a photograph or a rendered scene are highly similar --
that is what makes images compressible. Adjacent columns either side of a split
boundary are not: they come from two unrelated viewports. So the signal is the
*fraction of rows* where neighbouring pixels differ sharply, measured per
column, and a seam is where that fraction is near one.

Using the fraction of rows rather than the mean difference is what makes a
heads-up display safe. A health bar or a crosshair near the middle produces a
strong edge too, but only across the few rows it occupies; a seam runs the whole
height. Requiring near-full coverage separates them without knowing what either
one is.

The peak is then measured against the *strongest ordinary* column so a busy, high-contrast
scene does not read as a seam everywhere: confidence is how far the candidate
stands out from the rest of the picture, not how large it is absolutely -- and
the bar is a high percentile rather than the median, because a split boundary is
not merely *an* edge but the strongest sustained one in the frame. A menu
or a loading screen is flat, so nothing stands out and confidence collapses to
zero -- which is the correct answer for a screen with no gameplay on it.

Known limitation, stated rather than hidden: two viewports showing nearly
identical content -- both players stationary at the same spawn point -- have no
discontinuity to find. Detection will read FULL until they diverge. The
debouncing below means that shows up as a delayed switch rather than a flicker,
and the manual override exists for games this cannot serve.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from common.screen_regions import FULL, HORIZONTAL_2, QUAD_4, VERTICAL_2, normalise_layout

log = logging.getLogger(__name__)

#: How different two neighbouring samples must be to count as an edge, on a
#: 0..255 luma scale. Low enough to catch a soft boundary between two dim
#: scenes, high enough that sensor noise and gradient skies do not qualify.
EDGE_DELTA = 24

#: Rows are sampled rather than walked. A seam spans the whole height, so every
#: fourth row carries the same evidence at a quarter of the cost.
ROW_STEP = 4

#: Samples either side of a candidate are averaged over this many pixels before
#: being compared. Downscaling by a non-integer factor -- 1366 wide into 320 is
#: 4.27 -- smears a one-pixel boundary across two or three columns, and
#: comparing single neighbours then sees two small steps instead of one large
#: one. Measured: a 1366x768 quad split scored 0.51 comparing neighbours and
#: 1.00 comparing two-pixel means, while 1280x720 scored 1.00 either way. The
#: window costs one extra add per sample and removes a dependence on the
#: capture resolution that nothing downstream could have explained.
STEP_SPAN = 2

#: A candidate must reach this coverage before it is considered at all,
#: whatever its prominence. Guards the degenerate case where the picture is
#: almost uniform and *everything* stands out from a near-zero baseline.
MIN_COVERAGE = 0.55

#: Columns at the very edge are excluded from the baseline: letterbox bars and
#: overscan produce hard edges that are not seams and would inflate the typical
#: value, hiding a real one.
_EDGE_MARGIN = 0.04

#: Where in the background distribution the bar is set. Not the median: with a
#: median baseline any full-height edge that happened to fall near the centre
#: outscored a background of zero and read as a split -- measured at 0.78
#: confidence on a frame with no split in it at all. A split boundary is not
#: merely *an* edge, it is the strongest sustained discontinuity in the
#: picture, so it has to beat the strongest ordinary one rather than the
#: typical one. A percentile rather than the maximum so a single stray column
#: cannot veto a real seam.
_BACKGROUND_PERCENTILE = 0.92


@dataclass(slots=True)
class DetectorConfig:
    """Everything tunable, in one object.

    One constructible thing rather than a handful of loose attributes, for the
    reason ``_init_governor`` gives in ``pipeline.py``: a control loop's state
    should be constructible in one call, or the next person to add a field will
    miss a site.
    """

    width: int = 320
    confidence: float = 0.75
    activate_samples: int = 3
    deactivate_samples: int = 5
    #: How far from dead centre a boundary may sit, as a fraction of the
    #: dimension. Consoles do not always split at exactly 50%, and the
    #: downscale moves it further.
    tolerance: float = 0.04


@dataclass(slots=True)
class LayoutSample:
    """One frame's verdict, before any debouncing."""

    layout: str = FULL
    confidence: float = 0.0
    #: Where the boundaries were found, 0..1, for the debug overlay. Empty when
    #: nothing was found.
    vertical_at: float = 0.0
    horizontal_at: float = 0.0


# -- the pure part ---------------------------------------------------------
#
# Takes bytes, returns a verdict. No PyAV, so it can be tested with a bytearray
# on any machine -- the same split ``hogp.py`` makes against the D-Bus modules.


def _coverage_profile_columns(
    data: memoryview, width: int, height: int, stride: int
) -> list[float]:
    """Per column, the fraction of sampled rows showing a sustained step.

    Each candidate compares the mean of ``STEP_SPAN`` samples to its left with
    the mean of ``STEP_SPAN`` to its right, so a boundary smeared across two
    columns by the downscale still registers as one full-size step.
    """
    span = STEP_SPAN
    count = width - 2 * span
    if count <= 0:
        return []

    counts = [0] * count
    rows = 0
    for y in range(0, height, ROW_STEP):
        base = y * stride
        # Copied to bytes rather than indexed as a memoryview slice: the loop
        # below reads each row four hundred times over, and a bytes index is
        # markedly cheaper than a memoryview one. Measured 2.93 -> 1.7 ms.
        row = bytes(data[base : base + width])
        rows += 1
        left = sum(row[0:span])
        right = sum(row[span : 2 * span])
        for index in range(count):
            difference = left - right
            if difference > EDGE_DELTA * span or -difference > EDGE_DELTA * span:
                counts[index] += 1
            left += row[index + span] - row[index]
            right += row[index + 2 * span] - row[index + span]
    if not rows:
        return []
    return [value / rows for value in counts]


def _coverage_profile_rows(
    data: memoryview, width: int, height: int, stride: int
) -> list[float]:
    """Per row, the fraction of sampled columns showing a sustained step.

    The transpose of the column profile, including the ``STEP_SPAN`` window --
    the horizontal boundary of a horizontal split is smeared by the downscale
    in exactly the same way.

    Shaped differently from its transpose for one reason: walking a column
    means a multiply per read, and there is no row to slice. So each row is
    reduced *once* to just its sampled columns, and the window sums then roll
    down the picture with one add and one subtract per entry. Measured on a
    320x180 gray plane: 5.52 ms indexing the plane directly, 1.5 ms this way,
    against 3.07 ms for the column half doing the obvious thing.
    """
    span = STEP_SPAN
    count = height - 2 * span
    if count <= 0 or width <= 0:
        return []

    # One compact row per line, holding only the columns we sample. Indexing a
    # bytes object costs no multiply, and the rolling sums below then never
    # touch the strided plane again.
    rows = [bytes(data[y * stride : y * stride + width : ROW_STEP]) for y in range(height)]
    sampled = len(rows[0])
    if not sampled:
        return []

    windows: list[list[int]] = []
    first = [0] * sampled
    for offset in range(span):
        row = rows[offset]
        for index in range(sampled):
            first[index] += row[index]
    windows.append(first)
    for y in range(1, count + span):
        previous = windows[-1]
        windows.append(
            [
                value + entering - leaving
                for value, entering, leaving in zip(previous, rows[y + span - 1], rows[y - 1])
            ]
        )

    threshold = EDGE_DELTA * span
    counts = []
    for y in range(count):
        above = windows[y]
        below = windows[y + span]
        hits = 0
        for a, b in zip(above, below):
            difference = a - b
            if difference > threshold or -difference > threshold:
                hits += 1
        counts.append(hits / sampled)
    return counts

def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _score_boundary(profile: list[float], tolerance: float) -> tuple[float, float]:
    """Confidence that a boundary sits near the middle, and where.

    Returns ``(confidence, position)`` with position normalised 0..1.

    Confidence is prominence, not magnitude: how far the best candidate near
    the centre stands above the typical column elsewhere. A picture full of
    hard vertical edges -- a fence, a tiled wall, a scoreboard -- has a high
    typical value, so a seam has to beat it rather than merely be large.
    """
    count = len(profile)
    if count < 8:
        return 0.0, 0.0

    centre = count / 2.0
    span = max(1, int(round(count * tolerance)))
    low = max(0, int(centre) - span)
    high = min(count, int(centre) + span + 1)
    if low >= high:
        return 0.0, 0.0

    peak = 0.0
    peak_at = 0
    for index in range(low, high):
        if profile[index] > peak:
            peak = profile[index]
            peak_at = index

    if peak < MIN_COVERAGE:
        return 0.0, 0.0

    margin = max(1, int(round(count * _EDGE_MARGIN)))
    background = sorted(
        profile[index]
        for index in range(margin, count - margin)
        if not low <= index < high
    )
    if not background:
        return 0.0, 0.0
    rank = min(len(background) - 1, int(len(background) * _BACKGROUND_PERCENTILE))
    typical = background[rank]

    headroom = 1.0 - typical
    if headroom <= 1e-6:
        return 0.0, 0.0

    confidence = (peak - typical) / headroom
    confidence = min(1.0, max(0.0, confidence))
    # +1 because the profile indexes the gap *between* two samples.
    return confidence, (peak_at + 1) / (count + 1)


def analyse_gray(
    data: memoryview | bytes,
    width: int,
    height: int,
    stride: int,
    config: DetectorConfig | None = None,
) -> LayoutSample:
    """Classify one 8-bit greyscale frame. Never raises.

    ``stride`` is the row length in bytes and is **not** width: FFmpeg pads rows
    for alignment, and indexing by width shears the image -- the same trap the
    client's QImage stride note describes.
    """
    config = config or DetectorConfig()
    if width < 16 or height < 16 or stride < width:
        return LayoutSample()

    view = data if isinstance(data, memoryview) else memoryview(data)
    if len(view) < stride * (height - 1) + width:
        return LayoutSample()

    vertical, vertical_at = _score_boundary(
        _coverage_profile_columns(view, width, height, stride), config.tolerance
    )
    horizontal, horizontal_at = _score_boundary(
        _coverage_profile_rows(view, width, height, stride), config.tolerance
    )

    threshold = config.confidence
    has_v = vertical >= threshold
    has_h = horizontal >= threshold

    if has_v and has_h:
        return LayoutSample(QUAD_4, min(vertical, horizontal), vertical_at, horizontal_at)
    if has_v:
        return LayoutSample(VERTICAL_2, vertical, vertical_at, 0.0)
    if has_h:
        return LayoutSample(HORIZONTAL_2, horizontal, 0.0, horizontal_at)

    # Report the strongest thing seen even when it did not qualify: an operator
    # tuning the threshold needs to know it was at 0.7, not merely that the
    # answer was FULL.
    return LayoutSample(FULL, max(vertical, horizontal))


# -- the PyAV part ---------------------------------------------------------


class LayoutDetector:
    """Samples frames and classifies them. Not thread-safe; used from one thread.

    Same contract as ``PreviewEncoder``, and for the same reasons -- it owns a
    scaler, and a second thread inside that scaler is the failure this
    subsystem already paid for once.
    """

    def __init__(self, config: DetectorConfig | None = None) -> None:
        self.config = config or DetectorConfig()
        self._reformatter: Any = None
        self.frames_analysed = 0
        self.errors = 0

    def sample(self, frame: Any) -> LayoutSample | None:
        """Classify one captured frame. Returns None if it could not be read.

        Never raises: a detector that takes the video server down would be a
        far worse bug than one that occasionally cannot classify a frame.
        """
        try:
            target = self._target_size(frame)
            gray = self._scaler().reformat(
                frame, width=target[0], height=target[1], format="gray"
            )
            plane = gray.planes[0]
            sample = analyse_gray(
                memoryview(plane),
                gray.width,
                gray.height,
                plane.line_size,
                self.config,
            )
        except Exception:
            self.errors += 1
            log.debug("Could not analyse a frame for layout", exc_info=True)
            return None

        self.frames_analysed += 1
        return sample

    def _scaler(self) -> Any:
        if self._reformatter is None:
            from av.video.reformatter import VideoReformatter

            self._reformatter = VideoReformatter()
        return self._reformatter

    def _target_size(self, frame: Any) -> tuple[int, int]:
        width = min(self.config.width, frame.width or self.config.width)
        if not frame.width or not frame.height:
            return width, width * 9 // 16
        height = max(int(round(width * frame.height / frame.width)), 16)
        return width & ~1, height & ~1

    def stats(self) -> dict[str, int]:
        return {"frames_analysed": self.frames_analysed, "errors": self.errors}


# -- debouncing ------------------------------------------------------------


@dataclass(slots=True)
class SplitLayoutState:
    """Turns a stream of per-frame verdicts into a layout that holds still.

    Games show menus, maps, score screens and cinematics, any of which can
    briefly look like a boundary -- and a layout that followed every frame
    would rearrange the picture during a loading screen. So a candidate has to
    persist before it is adopted, and leaving a layout is deliberately harder
    than entering one: a split-screen game that cuts to a full-screen replay
    for two seconds should not resize every player's window twice.
    """

    config: DetectorConfig = field(default_factory=DetectorConfig)

    #: What everything downstream believes. FULL until proven otherwise, which
    #: is the state where every client sees the whole picture.
    layout: str = FULL
    confidence: float = 0.0

    #: The candidate being counted towards a change, and how many samples it
    #: has held for.
    _candidate: str = FULL
    _agreed: int = 0

    #: Set by the operator to pin the layout. "auto" means detect.
    override: str = "auto"

    def set_override(self, value: object) -> bool:
        """Pin the layout, or return to detection. True if anything changed."""
        text = value if isinstance(value, str) else "auto"
        text = text.strip() or "auto"
        if text != "auto":
            text = normalise_layout(text)
        if text == self.override:
            return False
        self.override = text
        if text == "auto":
            # Re-earn it. Adopting the last detected layout the instant the
            # operator releases the pin would surprise them.
            self._candidate = self.layout
            self._agreed = 0
            return False
        changed = self.layout != text
        self.layout = text
        self.confidence = 1.0
        self._candidate = text
        self._agreed = 0
        return changed

    def update(self, sample: LayoutSample | None) -> bool:
        """Fold one sample in. True when the confirmed layout changed.

        A sample that could not be read is not evidence either way, so it is
        ignored rather than counted towards falling back -- a dropped frame
        should not eventually flip the layout.
        """
        if self.override != "auto":
            return False
        if sample is None:
            return False

        candidate = normalise_layout(sample.layout)
        if candidate == self._candidate:
            self._agreed += 1
        else:
            self._candidate = candidate
            self._agreed = 1

        if candidate == self.layout:
            self.confidence = sample.confidence
            return False

        needed = (
            self.config.deactivate_samples
            if candidate == FULL
            else self.config.activate_samples
        )
        if self._agreed < needed:
            return False

        previous = self.layout
        self.layout = candidate
        self.confidence = sample.confidence
        log.info("Screen layout changed: %s -> %s", previous, candidate)
        return True

    def snapshot(self) -> dict[str, object]:
        """What travels in VIDEO_STATUS and reaches both GUIs."""
        return {
            "mode": self.layout,
            "confidence": round(self.confidence, 3),
            "source": "override" if self.override != "auto" else "auto",
        }

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
import math
import time
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

#: The same gate for *keeping* a boundary already confirmed, and deliberately
#: lower. Entering a split has to reject menus and busy scenery, so it asks a
#: lot; staying in one asks only whether the seam is still where it was, and a
#: seam that shows across 45% of the width -- two viewports that happen to
#: agree at the join, a pause overlay, a sky the colour of the road below it --
#: is plainly still there. Reported from the field as a layout that flipped to
#: full screen and back mid-race, because five such frames in a row were enough
#: to leave. The position test still applies, so a menu is refused either way.
HOLD_MIN_COVERAGE = 0.35

#: Columns at the very edge are excluded from the baseline: letterbox bars and
#: overscan produce hard edges that are not seams and would inflate the typical
#: value, hiding a real one.
_EDGE_MARGIN = 0.04

#: A pixel at or below this is treated as a bar rather than as picture. Not
#: zero: a capture card digitising an analogue signal puts black at 16 in
#: limited range and adds noise on top, so an exact test finds no bars at all.
_BAR_LEVEL = 40

#: How many rows (or columns) are sampled when deciding whether a column (or
#: row) is part of a bar. A bar is uniform by definition, so a handful spread
#: across the dimension answers it, and the scan stays off the frame budget.
_BAR_SAMPLES = 12

#: How far from the centre a boundary may be *looked for*. Wider than the
#: tolerance that finally accepts it: the band has to be found before it can be
#: placed, and its sharpest line may sit at either edge of it.
_SEARCH_WINDOW = 0.06

#: How far in from one edge the scan will go before giving up on that side.
_MAX_BAR_FRACTION = 0.45

#: How much of a dimension has to survive for the reading to be believed.
#:
#: A fade to black, a loading screen, or simply a very dark scene looks like
#: bars all the way in, and cropping to the sliver left over would measure
#: noise. Bars that eat more than half a dimension are not a letterbox.
#:
#: Real ones are nowhere near this: 4:3 inside 16:9 leaves 75% of the width,
#: and 2.35:1 inside 16:9 leaves 68% of the height. Refusing means analysing
#: the whole frame, which for a dark picture scores nothing anyway -- the safe
#: answer, and the same direction everything else here fails in.
_MIN_ACTIVE_FRACTION = 0.5

#: How far apart a divider line's two edges may be, as a fraction of the
#: dimension, and still count as one seam. Mario Kart 64's line is about 2% of
#: the height, which leaves one weak row between its two edges at the default
#: analysis size; 2.5% leaves room for a thicker divider without reaching the
#: scenery. See `_score_boundary`.
_DIVIDER_GAP = 0.025

#: How strong a line has to be, relative to the band's peak, to be part of
#: the band at all.
_BAND_LEVEL = 0.6

#: How alike two runs must be to be one divider's two edges. Over 263 frames
#: of a Mario Kart 64 race the weaker edge was 0.96 of the stronger at the
#: median and 0.89 at the 5th percentile -- the portraits riding the line
#: cover both edges at once -- with one frame down at 0.73.
#:
#: Swept from 0.9 to 0.7 together with `_RIVAL_RATIO` over that race and over
#: 429 frames of the same game's menus and one-player racing: no frame's
#: verdict changed anywhere in the range. On that game the nearer-centre edge
#: always carried it. So these are set for a line whose edges are further
#: apart in strength than any measured, not tuned to a number the data
#: never tested.
_PAIR_RATIO = 0.8

#: How strong a candidate must be, relative to the strongest line near the
#: centre, to be judged instead of it. High, so a faint line near the middle
#: cannot stand in for the real, stronger one elsewhere -- the property that
#: keeps "nearest the centre" from admitting a menu.
_RIVAL_RATIO = 0.8

#: Where in the background distribution the bar is set. Not the median: with a
#: median baseline any full-height edge that happened to fall near the centre
#: outscored a background of zero and read as a split -- measured at 0.78
#: confidence on a frame with no split in it at all. A split boundary is not
#: merely *an* edge, it is the strongest sustained discontinuity in the
#: picture, so it has to beat the strongest ordinary one rather than the
#: typical one. A percentile rather than the maximum so a single stray column
#: cannot veto a real seam.
#:
#: **0.92 was still too low, and a menu proved it.** A Mario Kart 64
#: map-select screen -- four cup buttons, four thumbnails, four label bars --
#: had 18% of its lines clearing MIN_COVERAGE, so the 92nd percentile *was*
#: one of the strong ones: the test compared a strong edge to a strong edge
#: and passed, at 0.609 against a 0.60 threshold. One of the menu's bars
#: happened to sit near the centre and scored 0.85, which is exactly what a
#: real seam scores. Strength cannot separate them; only the company the edge
#: keeps can.
#:
#: A higher percentile asks the right question -- is this candidate stronger
#: than essentially everything else in the picture? -- but it cannot go far,
#: and the ceiling is worth writing down because it is not obvious.
#:
#: **A couple of strong edges elsewhere is enough to saturate a high
#: percentile**, and real games have them: a HUD bar across the top of each
#: viewport is four hard full-width lines. At 0.97 and above, a synthetic
#: split with two such bars scores **0.000** -- the seam is suppressed by the
#: furniture. The percentile has to stay low enough that a handful of strong
#: lines cannot fill it, and high enough that a menu's three dozen cannot
#: reach it.
#:
#: Measured over 11 real split frames, 7 real menu frames, and a synthetic
#: split carrying two HUD bars. The window is where a threshold can live:
#:
#:     pct    real split   HUD split   menu    plain    usable window
#:     0.92   0.706        1.000       0.609   0.438    (0.609, 0.706)
#:     0.95   0.697        1.000       0.591   0.250    (0.591, 0.697)
#:     0.96   0.677        1.000       0.550   0.182    (0.550, 0.677)  <- widest
#:     0.97   0.655        0.000       0.438   0.100    none
#:     0.98   0.524        0.000       0.182   0.000    none
_BACKGROUND_PERCENTILE = 0.96


@dataclass(slots=True)
class DetectorConfig:
    """Everything tunable, in one object.

    One constructible thing rather than a handful of loose attributes, for the
    reason ``_init_governor`` gives in ``pipeline.py``: a control loop's state
    should be constructible in one call, or the next person to add a field will
    miss a site.
    """

    width: int = 320
    #: How far a candidate must stand above the strongest ordinary edge.
    #:
    #: Was 0.75, which was picked before the detector had ever met a game and
    #: then "validated" against synthetic frames where a true seam scores 1.00
    #: and everything else 0.00 -- a test any threshold between 0 and 1 passes,
    #: so it calibrated nothing.
    #:
    #: Measured against a real two-player Mario Kart 64 capture -- 11 split
    #: frames, 7 menu frames -- with the letterbox fix in place and the
    #: background taken at the 98th percentile:
    #:
    #:     a real split                    0.677 - 0.87
    #:     a menu screen                   0.550
    #:     one viewport alone (no split)   0.000
    #:     plain gameplay, no split        0.182
    #:
    #: So it has to sit in (0.550, 0.677); 0.61 is the midpoint, leaving about
    #: 0.06 either way. That is **not** a comfortable margin, and saying so is
    #: the point: it is one game's worth of evidence, the setting is exposed,
    #: and the debouncer's three-sample confirmation is what absorbs a frame
    #: that dips.
    #:
    #: It has moved twice, and both times the number was the symptom rather
    #: than the cause: 0.75 detected nothing real (the bars -- see
    #: `active_area`) and 0.60 detected a menu (the percentile -- see
    #: `_BACKGROUND_PERCENTILE`). Reach for those before reaching for this.
    #:
    #: Real content has legitimate full-width structure -- a racing game's
    #: horizon runs edge to edge in both halves -- so the background it is
    #: scored against sits near 0.42 rather than at zero, and confidence
    #: compresses accordingly. 0.75 sat *inside* the true-positive band, which
    #: is the one place a threshold must not be: it detected 6 frames of 11 on
    #: content that was split in every one of them.
    confidence: float = 0.61
    activate_samples: int = 3
    deactivate_samples: int = 5
    #: How far from dead centre the boundary's **band centre** may sit, as a
    #: fraction of the dimension.
    #:
    #: Was 0.04, which was reasoning about a thing this system cannot serve:
    #: everything downstream crops to exact halves and quadrants, so a
    #: boundary that is not near the middle is not one we can act on. A loose
    #: tolerance only admits edges we would then mis-crop.
    #:
    #: Measured: a real seam's band centre sits 0.0057 from the middle, on
    #: every one of 11 frames. Two different states of the same game's
    #: map-select screen put UI rows at 0.0287 and 0.0345. 0.015 sits between
    #: them with better than 2x margin either way -- far more room than
    #: prominence had, which is why this is what rejects a menu now.
    tolerance: float = 0.015

    #: How strong a boundary already confirmed has to stay for the layout to
    #: hold. Lower than `confidence` on purpose -- see `HOLD_MIN_COVERAGE`.
    #: The manual value, used when `hold_auto` is off or nothing has been
    #: learned yet.
    hold: float = 0.35
    #: Learn `hold` from this session's play. See `SplitCalibration`.
    hold_auto: bool = True
    #: Learn how long to wait before leaving a layout from the dips this
    #: session has recovered from. Never shorter than `deactivate_samples`.
    leave_auto: bool = True
    #: Seconds over which the edge profiles are averaged; 0 judges every frame
    #: alone. The seam never moves and scene edges do, so averaging is what
    #: tells them apart -- one frame cannot.
    smoothing_s: float = 2.0
    #: Luma step that counts as an edge. Exposed because a very dark game has
    #: soft boundaries; see `EDGE_DELTA`.
    edge_delta: int = EDGE_DELTA
    #: Samples per second. Only used to turn the leave delay's twenty-second
    #: ceiling into a sample count; the rate itself is the pipeline's.
    hz: float = 2.0


@dataclass(slots=True)
class LayoutSample:
    """One frame's verdict, before any debouncing."""

    layout: str = FULL
    confidence: float = 0.0
    #: Where the boundaries were found, 0..1, for the debug overlay. Empty when
    #: nothing was found.
    vertical_at: float = 0.0
    horizontal_at: float = 0.0

    #: The picture inside the letterbox, as ``(x, y, w, h)`` normalised 0..1.
    #: ``(0, 0, 1, 1)`` means no bars, which is also what a reading that could
    #: not be trusted falls back to.
    #:
    #: Measured here because this is already the one place that looks at a
    #: frame for structure, and because a client cropping a region wants the
    #: same answer the detector used -- two independent measurements of the
    #: same bars would eventually disagree.
    active: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)

    #: Each axis measured twice: against the entry test and against the
    #: easier one for keeping a boundary already confirmed. ``None`` means not
    #: measured -- a hand-built sample, or one from an older detector -- and
    #: then `layout` alone is believed, exactly as before these existed.
    vertical: float | None = None
    horizontal: float | None = None
    vertical_hold: float | None = None
    horizontal_hold: float | None = None

    @property
    def has_axes(self) -> bool:
        return self.vertical is not None and self.horizontal is not None


# -- the pure part ---------------------------------------------------------
#
# Takes bytes, returns a verdict. No PyAV, so it can be tested with a bytearray
# on any machine -- the same split ``hogp.py`` makes against the D-Bus modules.


def active_area(
    data: memoryview, width: int, height: int, stride: int
) -> tuple[int, int, int, int]:
    """The picture inside the letterbox, as ``(x0, x1, y0, y1)`` inclusive.

    **Bars are not part of the picture, and counting them is a measurement
    error rather than a missing refinement.** Coverage is the *fraction* of
    rows showing a step at a column, so a black bar down each side -- which is
    what a 4:3 console looks like on a 16:9 capture, and therefore what most
    of this project's targets look like -- contributes columns where no
    viewport boundary can exist. Measured on a real 1920x1080 capture of a
    two-player Mario Kart 64 split: 27% of columns were bar, a perfect seam
    could therefore score at most 0.73, and the real one scored 0.62 against a
    threshold it could never reach. Cropping first took the same frames from
    0.62 to 0.85 and from *nothing detected* to *everything detected*.

    Cheap on purpose: it scans inward from each edge and stops at the first
    line that is not a bar, sampling a dozen pixels across each line rather
    than all of them. A bar is uniform, so a dozen answers it.
    """
    if width <= 0 or height <= 0:
        return 0, max(0, width - 1), 0, max(0, height - 1)

    row_step = max(1, height // _BAR_SAMPLES)
    col_step = max(1, width // _BAR_SAMPLES)
    rows = range(0, height, row_step)
    columns = range(0, width, col_step)

    def column_is_bar(x: int) -> bool:
        return all(data[y * stride + x] <= _BAR_LEVEL for y in rows)

    def row_is_bar(y: int) -> bool:
        base = y * stride
        return all(data[base + x] <= _BAR_LEVEL for x in columns)

    x_limit = int(width * _MAX_BAR_FRACTION)
    y_limit = int(height * _MAX_BAR_FRACTION)

    x0 = 0
    while x0 < x_limit and column_is_bar(x0):
        x0 += 1
    x1 = width - 1
    while x1 > width - 1 - x_limit and column_is_bar(x1):
        x1 -= 1
    y0 = 0
    while y0 < y_limit and row_is_bar(y0):
        y0 += 1
    y1 = height - 1
    while y1 > height - 1 - y_limit and row_is_bar(y1):
        y1 -= 1

    # **A letterbox is centred; a dark scene is not.** Each side is cropped
    # only as far as its opposite, so the narrower bar is taken as the true
    # one and anything beyond it on the other side is picture.
    #
    # Without this a dark top -- Rainbow Road's black sky, a tunnel, a night
    # track -- was cropped as though it were a bar. The crop then shrank on
    # one side only, its middle moved off the seam by half the dark band, and
    # the seam failed a centring tolerance of about three rows: the sample
    # read FULL while the game was split. That is one of the causes of a
    # layout reported as flipping between split and full mid-race.
    x_bar = min(x0, width - 1 - x1)
    x0, x1 = x_bar, width - 1 - x_bar
    y_bar = min(y0, height - 1 - y1)
    y0, y1 = y_bar, height - 1 - y_bar

    # Refuse a reading that ate most of the frame: see _MIN_ACTIVE_FRACTION.
    if x1 - x0 + 1 < width * _MIN_ACTIVE_FRACTION:
        x0, x1 = 0, width - 1
    if y1 - y0 + 1 < height * _MIN_ACTIVE_FRACTION:
        y0, y1 = 0, height - 1
    return x0, x1, y0, y1


def _coverage_profile_columns(
    data: memoryview, width: int, height: int, stride: int,
    edge_delta: int = EDGE_DELTA,
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
    threshold = edge_delta * span
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
            if difference > threshold or -difference > threshold:
                counts[index] += 1
            left += row[index + span] - row[index]
            right += row[index + 2 * span] - row[index + span]
    if not rows:
        return []
    return [value / rows for value in counts]


def _coverage_profile_rows(
    data: memoryview, width: int, height: int, stride: int,
    edge_delta: int = EDGE_DELTA,
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

    threshold = edge_delta * span
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


def _central_band(
    profile: list[float], low: int, high: int, peak: float, min_coverage: float
) -> tuple[float, float]:
    """The strong band nearest the centre, and how strong it is.

    Returns ``(band_at, strength)``: the band's middle as a profile index and
    its own peak. Candidates are the runs of lines at the band level that
    touch the search window ``[low, high)``, plus any two neighbouring runs
    that look like the two edges of one divider line. Only candidates nearly
    as strong as the window's ``peak`` are considered, so a faint line near
    the middle cannot stand in for a strong one off it -- which would let a
    menu's furniture pass for a seam.

    A row joins a run only if it is strong *relative to the peak* as well as
    absolutely: the hold gate asks for as little as 0.35, and at that level
    HUD text beside a line would join the run and drag its centre off.
    """
    count = len(profile)
    level = max(min_coverage, peak * _BAND_LEVEL)

    runs: list[tuple[int, int, float]] = []
    index = 0
    while index < count:
        if profile[index] < level:
            index += 1
            continue
        start = index
        while index + 1 < count and profile[index + 1] >= level:
            index += 1
        if start < high and index >= low:
            runs.append((start, index, max(profile[start : index + 1])))
        index += 1

    candidates = list(runs)
    reach = max(1, int(round(count * _DIVIDER_GAP)))
    for (a_first, a_last, a_max), (b_first, b_last, b_max) in zip(runs, runs[1:]):
        gap = b_first - a_last - 1
        if gap <= reach and min(a_max, b_max) >= _PAIR_RATIO * max(a_max, b_max):
            candidates.append((a_first, b_last, max(a_max, b_max)))

    strong = [band for band in candidates if band[2] >= _RIVAL_RATIO * peak]
    if not strong:
        return 0.0, 0.0
    centre = (count - 1) / 2.0
    first, last, strength = min(
        strong, key=lambda band: abs((band[0] + band[1]) / 2.0 - centre)
    )
    return (first + last) / 2.0, strength


def _score_boundary(
    profile: list[float], tolerance: float, min_coverage: float = MIN_COVERAGE
) -> tuple[float, float]:
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
    # Searched generously, judged tightly. The band has to be *found* before
    # it can be placed, and its sharpest line may sit at either edge of it.
    span = max(1, int(round(count * _SEARCH_WINDOW)))
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

    if peak < min_coverage:
        return 0.0, 0.0

    # Where the boundary *is*, as the middle of the run of strong lines around
    # the peak rather than the peak itself.
    #
    # A seam is a band, not a line: the downscale smears it, and a console
    # usually draws a divider a few pixels thick. Which edge of that band comes
    # out sharpest is arbitrary -- it depends on what happens to lie against it
    # in each viewport -- so the peak wanders while the band's centre does not.
    # Measured over 11 real split frames: the peak sat 0.0000-0.0172 from dead
    # centre, the band centre sat at 0.0057 in **every one**.
    #
    # **But "the run around the peak" is not always the seam.** Mario Kart 64
    # draws its split as a black line a few pixels thick, so there are two
    # strong runs -- where the top viewport meets the line and where the line
    # meets the bottom one -- with a weak row inside it. Race-position
    # portraits ride that line and lap and time text sits against it, and
    # whichever edge came out sharper that frame decided the position. The
    # lower edge sits two rows off centre, outside the tolerance below.
    # Measured on 263 frames of a real race: the seam at 0.81-0.88 coverage
    # throughout, and 186 of them scored *zero* -- the layout flipped to full
    # screen and back six times in 90 seconds.
    #
    # So every strong run near the centre is a candidate, and so is a *pair*
    # of runs that look like one line's two edges: a line's width apart and
    # nearly equally strong. The portraits straddle the line and cover both
    # edges alike, so its edges measured within 5% of each other; the text
    # beside it touches one side only and came in at 0.55-0.72 against a peak
    # of 0.85, which is why "bridge to the next strong run" -- the first
    # version of this -- walked into the text instead. Of the candidates
    # nearly as strong as the peak, the one nearest the centre is judged.
    band_at, peak = _central_band(profile, low, high, peak, min_coverage)

    # **This is the test that rejects a menu, and it is not a heuristic.**
    #
    # Everything downstream crops to exact halves and quadrants -- those are
    # the only regions that exist. So a boundary that is not near the middle is
    # not something this system can serve: cropping to halves would put the
    # seam somewhere other than where the viewer's picture divides. Requiring
    # it near the middle is consistency with what we do about it.
    #
    # It is also what finally separated a Mario Kart 64 map-select screen from
    # the same game's real split, after prominence, edge counting, contiguity
    # and scene-detail had all failed to. A menu's furniture lands where the
    # layout puts it; measured at 0.0287 and 0.0345 from centre against the
    # real seam's 0.0057.
    if abs(band_at / count - 0.5) > tolerance:
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
    return confidence, (band_at + 1) / (count + 1)


def _usable(data, width: int, height: int, stride: int):
    """The frame as a memoryview, or None when it cannot be analysed."""
    if width < 16 or height < 16 or stride < width:
        return None
    view = data if isinstance(data, memoryview) else memoryview(data)
    if len(view) < stride * (height - 1) + width:
        return None
    return view


def _box_from_active(
    active: tuple[float, float, float, float] | None, width: int, height: int
) -> tuple[int, int, int, int] | None:
    """A normalised ``(x, y, w, h)`` as an inclusive pixel box, if it is sane.

    The settled letterbox arrives normalised because that is what travels to
    the clients; the analysis wants pixels in the plane it is looking at.
    Anything that would leave less than `_MIN_ACTIVE_FRACTION` of a dimension
    is refused, for the same reason `active_area` refuses it.
    """
    if active is None:
        return None
    try:
        ax, ay, aw, ah = (float(value) for value in active)
    except (TypeError, ValueError):
        return None
    x0 = max(0, min(width - 1, int(round(ax * width))))
    x1 = max(0, min(width - 1, int(round((ax + aw) * width)) - 1))
    y0 = max(0, min(height - 1, int(round(ay * height))))
    y1 = max(0, min(height - 1, int(round((ay + ah) * height)) - 1))
    if x1 - x0 + 1 < width * _MIN_ACTIVE_FRACTION:
        return None
    if y1 - y0 + 1 < height * _MIN_ACTIVE_FRACTION:
        return None
    return x0, x1, y0, y1


def measure_profiles(
    view: memoryview,
    stride: int,
    box: tuple[int, int, int, int],
    edge_delta: int = EDGE_DELTA,
) -> tuple[list[float], list[float]]:
    """The column and row coverage profiles inside ``box``.

    A slice rather than a copy: the sub-rectangle is the original buffer read
    from a later offset at the same stride, which is exactly what the profile
    functions expect.
    """
    x0, x1, y0, y1 = box
    inner_w = x1 - x0 + 1
    inner_h = y1 - y0 + 1
    inner = view[y0 * stride + x0 :]
    return (
        _coverage_profile_columns(inner, inner_w, inner_h, stride, edge_delta),
        _coverage_profile_rows(inner, inner_w, inner_h, stride, edge_delta),
    )


def score_profiles(
    columns: list[float],
    rows: list[float],
    box: tuple[int, int, int, int],
    measured: tuple[int, int, int, int],
    width: int,
    height: int,
    config: DetectorConfig,
) -> LayoutSample:
    """Turn two profiles into a verdict, with both axes measured both ways.

    ``box`` is where the profiles were measured; ``measured`` is this frame's
    own reading of the letterbox, which is reported whether or not it was the
    one analysed -- the debounce needs every frame's measurement, or a
    letterbox that genuinely changed could never be adopted.
    """
    tolerance = config.tolerance
    vertical, vertical_at = _score_boundary(columns, tolerance)
    horizontal, horizontal_at = _score_boundary(rows, tolerance)
    vertical_hold, _ = _score_boundary(columns, tolerance, HOLD_MIN_COVERAGE)
    horizontal_hold, _ = _score_boundary(rows, tolerance, HOLD_MIN_COVERAGE)

    x0, x1, y0, y1 = box
    inner_w = x1 - x0 + 1
    inner_h = y1 - y0 + 1
    # Reported against the whole frame, because that is the picture anyone
    # looking at an overlay is seeing. A position measured inside the crop
    # would sit somewhere else entirely on a pillarboxed source.
    if vertical_at:
        vertical_at = (x0 + vertical_at * inner_w) / width
    if horizontal_at:
        horizontal_at = (y0 + horizontal_at * inner_h) / height

    mx0, mx1, my0, my1 = measured
    active = (
        mx0 / width,
        my0 / height,
        (mx1 - mx0 + 1) / width,
        (my1 - my0 + 1) / height,
    )

    threshold = config.confidence
    has_v = vertical >= threshold
    has_h = horizontal >= threshold
    if has_v and has_h:
        layout, confidence = QUAD_4, min(vertical, horizontal)
    elif has_v:
        layout, confidence = VERTICAL_2, vertical
    elif has_h:
        layout, confidence = HORIZONTAL_2, horizontal
    else:
        # Report the strongest thing seen even when it did not qualify: an
        # operator tuning the threshold needs to know it was at 0.7, not
        # merely that the answer was FULL.
        layout, confidence = FULL, max(vertical, horizontal)

    return LayoutSample(
        layout,
        confidence,
        vertical_at if has_v else 0.0,
        horizontal_at if has_h else 0.0,
        active,
        vertical=vertical,
        horizontal=horizontal,
        vertical_hold=vertical_hold,
        horizontal_hold=horizontal_hold,
    )


def analyse_gray(
    data: memoryview | bytes,
    width: int,
    height: int,
    stride: int,
    config: DetectorConfig | None = None,
    *,
    active: tuple[float, float, float, float] | None = None,
) -> LayoutSample:
    """Classify one 8-bit greyscale frame on its own. Never raises.

    ``stride`` is the row length in bytes and is **not** width: FFmpeg pads rows
    for alignment, and indexing by width shears the image -- the same trap the
    client's QImage stride note describes.

    ``active`` is the settled letterbox, normalised, when one is known: the
    frame is then analysed inside it rather than inside its own reading. See
    `LayoutAnalyser` for the stateful form that also averages over time.
    """
    config = config or DetectorConfig()
    view = _usable(data, width, height, stride)
    if view is None:
        return LayoutSample()

    # Bars first. Everything below measures the *fraction* of lines showing a
    # step, so a black band down each side dilutes a real seam towards nothing
    # -- see `active_area`, and the measurement recorded there.
    measured = active_area(view, width, height, stride)
    box = _box_from_active(active, width, height) or measured
    columns, rows = measure_profiles(view, stride, box, config.edge_delta)
    return score_profiles(columns, rows, box, measured, width, height, config)


@dataclass(slots=True)
class ProfileSmoother:
    """Averages the edge profiles over time. Pure; one caller.

    The seam of a split screen does not move. Everything else in a game's
    picture does -- scenery scrolling past, a fence, stairs, another kart --
    so a scene edge that happens to run the width of the picture in one frame
    is somewhere else in the next. An exponential average per column and per
    row lets the seam stand out against edges that would each have beaten it
    in the single frame they appeared in.

    Reset whenever the geometry changes: profiles measured inside two
    different crops do not line up column for column, and averaging across
    them would smear one seam into two.
    """

    key: tuple = ()
    columns: list[float] = field(default_factory=list)
    rows: list[float] = field(default_factory=list)
    last_ns: int = 0
    #: Samples folded in since the last reset.
    samples: int = 0

    def reset(self) -> None:
        self.key = ()
        self.columns = []
        self.rows = []
        self.last_ns = 0
        self.samples = 0

    def update(
        self,
        key: tuple,
        columns: list[float],
        rows: list[float],
        now_ns: int,
        time_constant_s: float,
    ) -> tuple[list[float], list[float]]:
        """Fold one frame's profiles in and return the averaged ones."""
        if time_constant_s <= 0.0:
            self.reset()
            return columns, rows
        if (
            key != self.key
            or not self.samples
            or len(columns) != len(self.columns)
            or len(rows) != len(self.rows)
        ):
            self.key = key
            self.columns = list(columns)
            self.rows = list(rows)
            self.last_ns = now_ns
            self.samples = 1
            return self.columns, self.rows

        elapsed = (now_ns - self.last_ns) / 1_000_000_000
        self.last_ns = now_ns
        # Weighted by the time that passed rather than per sample, so changing
        # the sample rate does not change how long the average remembers. A
        # clock that did not move still folds the frame in, at the weight of
        # one sample at the default rate.
        if elapsed <= 0.0:
            elapsed = 0.5
        weight = 1.0 - math.exp(-elapsed / time_constant_s)

        stored = self.columns
        for index, value in enumerate(columns):
            stored[index] += (value - stored[index]) * weight
        stored = self.rows
        for index, value in enumerate(rows):
            stored[index] += (value - stored[index]) * weight
        self.samples += 1
        return self.columns, self.rows


class LayoutAnalyser:
    """`analyse_gray` with a memory: the profiles are averaged over time.

    Pure -- bytes in, verdict out -- so it tests without PyAV. Not
    thread-safe; one caller, like the detector that owns it.
    """

    def __init__(self, config: DetectorConfig | None = None) -> None:
        self.config = config or DetectorConfig()
        self.smoother = ProfileSmoother()

    def analyse(
        self,
        data: memoryview | bytes,
        width: int,
        height: int,
        stride: int,
        *,
        now_ns: int = 0,
        active: tuple[float, float, float, float] | None = None,
    ) -> LayoutSample:
        config = self.config
        view = _usable(data, width, height, stride)
        if view is None:
            return LayoutSample()

        measured = active_area(view, width, height, stride)
        box = _box_from_active(active, width, height) or measured
        columns, rows = measure_profiles(view, stride, box, config.edge_delta)
        columns, rows = self.smoother.update(
            (width, height, box), columns, rows, now_ns, config.smoothing_s
        )
        return score_profiles(columns, rows, box, measured, width, height, config)


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
        self._analyser = LayoutAnalyser(self.config)
        self.frames_analysed = 0
        self.errors = 0

    def sample(
        self,
        frame: Any,
        *,
        now_ns: int | None = None,
        active: tuple[float, float, float, float] | None = None,
    ) -> LayoutSample | None:
        """Classify one captured frame. Returns None if it could not be read.

        ``active`` is the settled letterbox, when there is one: the frame is
        analysed inside it rather than inside its own reading, which is what
        stops a dark sky being cropped as a bar.

        Never raises: a detector that takes the video server down would be a
        far worse bug than one that occasionally cannot classify a frame.
        """
        try:
            target = self._target_size(frame)
            gray = self._scaler().reformat(
                frame, width=target[0], height=target[1], format="gray"
            )
            plane = gray.planes[0]
            sample = self._analyser.analyse(
                memoryview(plane),
                gray.width,
                gray.height,
                plane.line_size,
                now_ns=time.monotonic_ns() if now_ns is None else now_ns,
                active=active,
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


# -- self-calibration ------------------------------------------------------

#: Samples each distribution must hold before a learned hold threshold is
#: used. Thirty is fifteen seconds at the default rate: long enough that one
#: odd scene cannot set the threshold for the rest of the session.
_LEARN_MIN_SAMPLES = 30

#: How many recent samples each distribution remembers. Two minutes at the
#: default rate, so a change of course or of game is followed within a race.
_LEARN_WINDOW = 240

#: What the noise is assumed to be before any has been seen: the "plain
#: gameplay" figure recorded under `DetectorConfig.confidence`, rounded down.
_NOISE_PRIOR = 0.15

#: The learned hold threshold never goes below this. Under it the easier test
#: would begin keeping layouts on the strength of ordinary scene edges.
_HOLD_FLOOR = 0.20

#: ...and never within this of the entry threshold, or holding would ask as
#: much as entering and the whole point of two thresholds would be lost.
_HOLD_BELOW_ENTRY = 0.05

#: The longest the learned leave delay may grow to, in seconds. A real change
#: to full screen -- a menu, a results screen -- must still be followed.
_LEAVE_CAP_S = 20.0

#: Recovered dips remembered. The longest of them sets the leave delay.
_DIP_WINDOW = 20


def _percentile(values, fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(len(ordered) * fraction)))
    return ordered[index]


@dataclass(slots=True)
class SplitCalibration:
    """What this session's play says the thresholds should be. Pure.

    Relearned every session and never persisted -- a threshold learned on one
    game has no business deciding the next one.

    **It learns only from unambiguous evidence**, which is what stops it
    teaching itself. A seam sample is taken only when that axis passed the
    strict entry test, and a noise sample only from an axis that is not part
    of a confirmed layout. Learning from every sample it *held* on would lower
    the threshold, which would hold on more, which would lower it further.
    """

    seam: list[float] = field(default_factory=list)
    noise: list[float] = field(default_factory=list)
    dips: list[int] = field(default_factory=list)

    def reset(self) -> None:
        self.seam.clear()
        self.noise.clear()
        self.dips.clear()

    def observe_seam(self, score: float) -> None:
        self.seam.append(float(score))
        if len(self.seam) > _LEARN_WINDOW:
            del self.seam[: len(self.seam) - _LEARN_WINDOW]

    def observe_noise(self, score: float) -> None:
        self.noise.append(float(score))
        if len(self.noise) > _LEARN_WINDOW:
            del self.noise[: len(self.noise) - _LEARN_WINDOW]

    def observe_dip(self, samples: int) -> None:
        """A run of samples that read as leaving, after which the seam came back."""
        if samples <= 0:
            return
        self.dips.append(int(samples))
        if len(self.dips) > _DIP_WINDOW:
            del self.dips[: len(self.dips) - _DIP_WINDOW]

    def longest_dip(self) -> int:
        return max(self.dips, default=0)

    def learned_hold(self, entry: float) -> float | None:
        """Midway between the seam's weak end and the noise's strong end.

        None until enough seam has been seen. The noise side falls back to a
        prior, because a session that has only ever been split (a quad game
        has no absent axis) still deserves a learned value.
        """
        if len(self.seam) < _LEARN_MIN_SAMPLES:
            return None
        seam_low = _percentile(self.seam, 0.10)
        noise_high = (
            _percentile(self.noise, 0.95)
            if len(self.noise) >= _LEARN_MIN_SAMPLES
            else _NOISE_PRIOR
        )
        ceiling = max(_HOLD_FLOOR, entry - _HOLD_BELOW_ENTRY)
        return min(ceiling, max(_HOLD_FLOOR, (seam_low + noise_high) / 2.0))

    def learned_leave(self, manual: int, hz: float) -> int:
        """Samples to wait before leaving: twice the longest recovered dip.

        Never shorter than the operator's own setting, and capped so a real
        change still arrives within `_LEAVE_CAP_S`.
        """
        cap = max(int(manual), int(round(_LEAVE_CAP_S * max(hz, 0.05))))
        return min(cap, max(int(manual), 2 * self.longest_dip()))

    def snapshot(self, entry: float, manual_leave: int, hz: float) -> dict:
        hold = self.learned_hold(entry)
        return {
            "hold": None if hold is None else round(hold, 3),
            "leave": self.learned_leave(manual_leave, hz),
            "seam_samples": len(self.seam),
            "noise_samples": len(self.noise),
            "longest_dip": self.longest_dip(),
        }


# -- debouncing ------------------------------------------------------------


def _axes(layout: str) -> tuple[bool, bool]:
    """(vertical, horizontal) -- which boundaries a layout has."""
    return layout in (VERTICAL_2, QUAD_4), layout in (HORIZONTAL_2, QUAD_4)


def _layout_of(vertical: bool, horizontal: bool) -> str:
    if vertical and horizontal:
        return QUAD_4
    if vertical:
        return VERTICAL_2
    if horizontal:
        return HORIZONTAL_2
    return FULL


@dataclass(slots=True)
class SplitLayoutState:
    """Turns a stream of per-frame verdicts into a layout that holds still.

    Games show menus, maps, score screens and cinematics, any of which can
    briefly look like a boundary -- and a layout that followed every frame
    would rearrange the picture during a loading screen. So a candidate has to
    persist before it is adopted, and leaving a layout is deliberately harder
    than entering one: a split-screen game that cuts to a full-screen replay
    for two seconds should not resize every player's window twice.

    **Harder in the evidence as well as in the count.** A boundary already
    confirmed is kept while its easier *hold* score stays above the hold
    threshold; only entering needs the strict test. Before this the two asked
    the same question, and a Mario Kart 64 race flipped to full screen and
    back whenever five weak frames came in a row.
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

    #: The picture inside the letterbox, once it has held still.
    active: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    _active_candidate: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    _active_agreed: int = 0
    #: True once a letterbox reading has held for long enough to be adopted,
    #: whatever it was. Until then there is nothing settled to analyse inside.
    active_settled: bool = False

    #: This session's learning. See `SplitCalibration`.
    calibration: SplitCalibration = field(default_factory=SplitCalibration)

    #: The last sample's per-axis strength, for the readouts: the stronger of
    #: its entry and hold scores.
    vertical: float = 0.0
    horizontal: float = 0.0

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

    def hold_threshold(self) -> float:
        """The hold score a confirmed boundary must keep. Learned or manual."""
        config = self.config
        if config.hold_auto:
            learned = self.calibration.learned_hold(config.confidence)
            if learned is not None:
                return learned
        return min(config.hold, config.confidence)

    def leave_samples(self) -> int:
        """Samples a weaker candidate must hold before a boundary is dropped."""
        config = self.config
        if config.leave_auto:
            return self.calibration.learned_leave(config.deactivate_samples, config.hz)
        return config.deactivate_samples

    def reset_learning(self) -> None:
        self.calibration.reset()

    def update(self, sample: LayoutSample | None) -> bool:
        """Fold one sample in. True when the confirmed layout changed.

        A sample that could not be read is not evidence either way, so it is
        ignored rather than counted towards falling back -- a dropped frame
        should not eventually flip the layout.
        """
        if sample is not None:
            self._update_active(sample.active)
        if self.override != "auto":
            return False
        if sample is None:
            return False

        candidate = self._candidate_for(sample)
        if candidate == self._candidate:
            self._agreed += 1
        else:
            # A run that read as leaving and then came back is a recovered
            # dip -- the thing the leave delay has to outlast.
            if self._candidate != self.layout and candidate == self.layout:
                self.calibration.observe_dip(self._agreed)
            self._candidate = candidate
            self._agreed = 1

        if candidate == self.layout:
            self.confidence = sample.confidence if not sample.has_axes else self._strength(
                sample, self.layout
            )
            return False

        # Losing a boundary is leaving, whether it leaves for full screen or
        # for a split with fewer pieces.
        now_v, now_h = _axes(self.layout)
        next_v, next_h = _axes(candidate)
        leaving = (now_v and not next_v) or (now_h and not next_h)
        needed = self.leave_samples() if leaving else self.config.activate_samples
        if self._agreed < needed:
            return False

        previous = self.layout
        self.layout = candidate
        self.confidence = sample.confidence if not sample.has_axes else self._strength(
            sample, candidate
        )
        self._agreed = 0
        self._candidate = candidate
        log.info(
            "Screen layout changed: %s -> %s (vertical %.2f, horizontal %.2f, "
            "stay above %.2f, leave after %d samples)",
            previous, candidate, self.vertical, self.horizontal,
            self.hold_threshold(), self.leave_samples(),
        )
        return True

    def _candidate_for(self, sample: LayoutSample) -> str:
        """The layout this sample argues for, with hysteresis applied.

        A sample with no axis readings -- hand-built, or from an older
        detector -- is believed as it stands, which is exactly how this
        behaved before there were two thresholds.
        """
        if not sample.has_axes:
            return normalise_layout(sample.layout)

        config = self.config
        vertical = float(sample.vertical or 0.0)
        horizontal = float(sample.horizontal or 0.0)
        vertical_hold = float(sample.vertical_hold or 0.0)
        horizontal_hold = float(sample.horizontal_hold or 0.0)
        self.vertical = max(vertical, vertical_hold)
        self.horizontal = max(horizontal, horizontal_hold)

        hold = self.hold_threshold()
        in_v, in_h = _axes(self.layout)
        enter_v = vertical >= config.confidence
        enter_h = horizontal >= config.confidence
        has_v = enter_v or (in_v and vertical_hold >= hold)
        has_h = enter_h or (in_h and horizontal_hold >= hold)
        self._learn(in_v, in_h, enter_v, enter_h, vertical_hold, horizontal_hold)
        return _layout_of(has_v, has_h)

    def _learn(
        self, in_v: bool, in_h: bool, enter_v: bool, enter_h: bool,
        vertical_hold: float, horizontal_hold: float,
    ) -> None:
        """Feed the calibration from unambiguous evidence only."""
        calibration = self.calibration
        if in_v and enter_v:
            calibration.observe_seam(vertical_hold)
        if in_h and enter_h:
            calibration.observe_seam(horizontal_hold)
        # Noise: an axis the confirmed layout does not have, on a sample that
        # was not itself arguing for it -- a split just starting would
        # otherwise teach the noise what a seam looks like.
        if not in_v and not enter_v:
            calibration.observe_noise(vertical_hold)
        if not in_h and not enter_h:
            calibration.observe_noise(horizontal_hold)

    @staticmethod
    def _strength(sample: LayoutSample, layout: str) -> float:
        """How strongly this sample supports ``layout``, for the readouts."""
        has_v, has_h = _axes(layout)
        vertical = max(sample.vertical or 0.0, sample.vertical_hold or 0.0)
        horizontal = max(sample.horizontal or 0.0, sample.horizontal_hold or 0.0)
        if has_v and has_h:
            return min(vertical, horizontal)
        if has_v:
            return vertical
        if has_h:
            return horizontal
        return max(sample.vertical or 0.0, sample.horizontal or 0.0)

    def _update_active(self, measured: tuple[float, float, float, float]) -> None:
        """Adopt a new active area only once it has held still.

        The bars are a property of the signal and do not move, so a reading
        that changes is a reading that was wrong -- a dark scene, a fade, a
        frame caught mid-transition. Adopting it immediately would zoom every
        player's picture for one sample and put it back, which is far worse
        than being a second late to a change that essentially never happens.

        Quantised before comparing: the measurement comes from a 320-wide
        plane, so it is already integral there, and comparing floats for
        equality is asking for a candidate that never agrees with itself.
        """
        rounded = tuple(round(value, 3) for value in measured)
        if rounded == self._active_candidate:
            self._active_agreed += 1
        else:
            self._active_candidate = rounded  # type: ignore[assignment]
            self._active_agreed = 1
        if self._active_agreed >= self.config.activate_samples:
            if rounded != self.active:
                log.info(
                    "Active picture area is now x %.3f..%.3f, y %.3f..%.3f",
                    rounded[0], rounded[0] + rounded[2],
                    rounded[1], rounded[1] + rounded[3],
                )
            self.active = rounded  # type: ignore[assignment]
            self.active_settled = True

    def learned(self) -> dict:
        """What this session has learned, beside what is in force."""
        config = self.config
        snapshot = self.calibration.snapshot(
            config.confidence, config.deactivate_samples, config.hz
        )
        snapshot["hold_in_force"] = round(self.hold_threshold(), 3)
        snapshot["leave_in_force"] = self.leave_samples()
        return snapshot

    def snapshot(self) -> dict[str, object]:
        """What travels in VIDEO_STATUS and reaches both GUIs."""
        x, y, w, h = self.active
        return {
            "mode": self.layout,
            "confidence": round(self.confidence, 3),
            "source": "override" if self.override != "auto" else "auto",
            # The picture inside the letterbox. Clients intersect their region
            # with this so a half-screen view is half the *game* rather than
            # half the frame -- on a 4:3 console in a 16:9 capture the
            # difference is about a quarter of the window given over to black.
            "active": {"x": x, "y": y, "w": w, "h": h},
            # Each axis's strength on the last sample, for the readouts that
            # let an operator see *why* a layout is or is not being held. Two
            # decimals: this rides a message with a hard ceiling.
            "v": round(self.vertical, 2),
            "h": round(self.horizontal, 2),
        }

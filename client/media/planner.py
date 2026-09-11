"""Where each piece of the picture comes from, and where it lands.

Pure geometry: stdlib only, no PyAV, no Qt, no GPU. That is the same split
``common/screen_regions.py`` makes against the renderer, and for the same
reason -- this is the part most likely to be silently wrong, and it is the
cheapest thing in the whole video path to test.

**Python plans, the GPU executes.** The native backends take a list of
rectangles and copy pixels; every decision about *which* rectangles is made
here, in code that runs on any machine with no graphics driver at all. Nothing
in this module is reimplemented in C++.

The two paths share this
------------------------
``compose`` is used by the existing software path *and* by the GPU path, so the
split-screen layout is identical in both by construction rather than by a test
that has to be remembered. Only who acts on the numbers differs: today swscale
resamples to the destination size, and in GPU mode the upscaler does.

The one rule
------------
:func:`plan_blits` answers every case -- whole picture, one crop, two to four
crops with gutters, and a camera move -- with a single shape:

    upload one rectangle of the source, then draw a list of (src -> dst).

``src`` is normalised **inside the uploaded rectangle**, not inside the frame,
so the GPU never needs to know what was left out. The uploaded rectangle is the
union of everything being shown, which is what keeps a single-quadrant player
uploading a quarter of the frame rather than all of it.

Why a camera move needs no special machinery here
-------------------------------------------------
The software path renders the *union* of the two views enlarged, and hands the
window a travelling sub-rectangle to scale down. That exists because a filter
graph is cached by its crop, so an interpolating crop would build and throw
away a graph every frame -- and because the intermediate resample made the
picture visibly sharpen as it settled, which is what ``MAX_TRANSITION_SCALE``
and the retire-before-the-last-frame rule in ``decoder.py`` exist to hide.

On the GPU a crop is four floats in a constant buffer and every frame samples
native-resolution pixels, so none of that applies: the move is one blit whose
``src`` walks from one view to the other, and there is no sharpening pop to
prevent.
"""

from __future__ import annotations

from dataclasses import dataclass

#: How long the camera takes to move from one view to another.
#:
#: Long enough to read as a camera move rather than a glitch, short enough that
#: nobody is playing on a moving picture for meaningfully long.
TRANSITION_NS = 400_000_000

#: Space between two pieces of a split screen that could not be merged into one
#: rectangle. Without it two unrelated viewports butted together read as a
#: single picture with a seam down the middle -- which is precisely what the
#: layout detector spends its time looking for.
GUTTER_PX = 8

#: A normalised rectangle covering everything.
WHOLE = (0.0, 0.0, 1.0, 1.0)


def eased(elapsed_ns: int) -> float:
    """0..1 through the move, smoothed at both ends.

    Smoothstep rather than linear: a camera that starts and stops abruptly
    reads as a glitch even when the middle of the move is perfectly smooth.
    """
    if elapsed_ns >= TRANSITION_NS:
        return 1.0
    t = max(0.0, elapsed_ns / TRANSITION_NS)
    return t * t * (3.0 - 2.0 * t)


def lerp_rect(start: tuple, end: tuple, t: float) -> tuple:
    return tuple(a + (b - a) * t for a, b in zip(start, end))


def union_rect(a: tuple, b: tuple) -> tuple:
    """The bounding box of two views -- everything the move passes over."""
    x0 = min(a[0], b[0])
    y0 = min(a[1], b[1])
    x1 = max(a[0] + a[2], b[0] + b[2])
    y1 = max(a[1] + a[3], b[1] + b[3])
    return (x0, y0, x1 - x0, y1 - y0)


@dataclass(frozen=True, slots=True)
class Blit:
    """One piece of the uploaded picture, and where it is drawn.

    ``src`` is normalised inside the **uploaded rectangle**, not inside the
    frame -- the backend samples a texture holding only those pixels and has no
    idea what surrounds them.

    ``dst`` is in physical pixels inside the composed picture, the same space
    ``RegionView.x``/``y`` uses, so the two paths place pieces identically.
    """

    src: tuple[float, float, float, float]
    dst: tuple[int, int, int, int]


def compose(
    crops: tuple, frame_w: int, frame_h: int, viewport: tuple[int, int] | None
):
    """Where each crop goes, and how big the composed picture is.

    Returns ``([(crop, x, y, width, height), ...], composed_w, composed_h)`` in
    physical pixels, sized so the whole thing fits the viewport exactly.

    Shared by both render paths. In the software path these sizes are what
    swscale resamples to and the window blits 1:1; in the GPU path they are
    what the upscaler targets. Same numbers either way, which is what makes a
    mode switch leave the picture exactly where it was.
    """
    viewport = viewport or (frame_w, frame_h)
    # Empty means the whole picture. The software path never asks -- it has a
    # separate uncropped branch -- but this function is shared now, and a
    # shared function that raises on a legal input is a trap waiting for the
    # next caller.
    crops = tuple(crops) or (WHOLE,)

    if len(crops) == 1:
        crop = crops[0]
        source_w = max(frame_w * crop[2], 1.0)
        source_h = max(frame_h * crop[3], 1.0)
        scale = min(viewport[0] / source_w, viewport[1] / source_h)
        width = max(2, (int(source_w * scale) // 2) * 2)
        height = max(2, (int(source_h * scale) // 2) * 2)
        return [(crop, 0, 0, width, height)], width, height

    # Two to four pieces that could not be merged, tiled with a gutter between
    # them.
    from common.screen_regions import tile

    # The pieces' own shape decides the grid, not just the viewport's -- see
    # `tile`. After the all-or-nothing merge rule every piece of a multi-piece
    # layout is one grid cell, so they are all the same shape and the first one
    # speaks for the rest.
    first_w = max(frame_w * crops[0][2], 1.0)
    first_h = max(frame_h * crops[0][3], 1.0)
    columns, rows = tile(
        len(crops), viewport[0] / max(viewport[1], 1), first_w / first_h
    )
    cell_w = max(2, (viewport[0] - GUTTER_PX * (columns - 1)) // columns)
    cell_h = max(2, (viewport[1] - GUTTER_PX * (rows - 1)) // rows)

    placed = []
    for index, crop in enumerate(crops):
        source_w = max(frame_w * crop[2], 1.0)
        source_h = max(frame_h * crop[3], 1.0)
        scale = min(cell_w / source_w, cell_h / source_h)
        width = max(2, (int(source_w * scale) // 2) * 2)
        height = max(2, (int(source_h * scale) // 2) * 2)
        column, row = index % columns, index // columns
        # A row that does not fill its columns is centred, so three pieces in a
        # 2x2 grid read as a triangle rather than an L with a hole in the
        # corner. Three players each get an equal share and the odd one sits
        # under the gap between the other two.
        in_row = min(columns, len(crops) - row * columns)
        row_offset = (columns - in_row) * (cell_w + GUTTER_PX) // 2
        # Centred in its cell too, so pieces of different shapes do not sit
        # against one edge with the whole gutter on the other side.
        x = row_offset + column * (cell_w + GUTTER_PX) + (cell_w - width) // 2
        y = row * (cell_h + GUTTER_PX) + (cell_h - height) // 2
        placed.append((crop, x, y, width, height))

    composed_w = columns * cell_w + GUTTER_PX * (columns - 1)
    composed_h = rows * cell_h + GUTTER_PX * (rows - 1)
    return placed, composed_w, composed_h


def pixel_rect(rect: tuple, frame_w: int, frame_h: int):
    """A normalised rectangle as even-aligned pixels inside the frame.

    Even because the decoded frame is 4:2:0: an odd origin has no chroma sample
    to start from, and an odd size leaves half a chroma row at the far edge.
    The same rule the software path's crop filter already obeys.

    Trimmed rather than left to overrun, because a rectangle that rounds past
    the edge is a texture upload reading off the end of a plane.
    """
    x0 = max(0, int(frame_w * rect[0])) & ~1
    y0 = max(0, int(frame_h * rect[1])) & ~1
    width = max(2, (int(frame_w * rect[2]) // 2) * 2)
    height = max(2, (int(frame_h * rect[3]) // 2) * 2)
    width = max(2, min(width, frame_w - x0))
    height = max(2, min(height, frame_h - y0))
    return x0, y0, width, height


def _bounding_px(rects, frame_w: int, frame_h: int):
    """The even-aligned pixel box containing every one of these pixel rects."""
    x0 = min(r[0] for r in rects) & ~1
    y0 = min(r[1] for r in rects) & ~1
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    width = max(2, min(((x1 - x0 + 1) // 2) * 2, frame_w - x0))
    height = max(2, min(((y1 - y0 + 1) // 2) * 2, frame_h - y0))
    return x0, y0, width, height


def _src_within(inner, outer):
    """``inner`` expressed as a 0..1 rectangle inside ``outer``, both pixels."""
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return ((ix - ox) / ow, (iy - oy) / oh, iw / ow, ih / oh)


def plan_blits(
    crops: tuple,
    transition: tuple | None,
    frame_w: int,
    frame_h: int,
    viewport: tuple[int, int] | None,
    now_ns: int,
):
    """What to upload, and what to draw with it.

    Returns ``(upload_rect, blits, composed_w, composed_h, moving)``:

    * ``upload_rect`` -- pixels ``(x, y, w, h)`` of the decoded frame to put on
      the GPU, even-aligned and inside the frame;
    * ``blits`` -- one per visible piece, ``src`` normalised inside that
      rectangle and ``dst`` in composed pixels;
    * ``composed_w``/``composed_h`` -- the picture the backend presents, which
      the presenter then centres in whatever the window currently is;
    * ``moving`` -- whether a camera move is still running, so the caller knows
      when to retire it.

    ``transition`` is ``(started_ns, start_rect, end_rect)`` or ``None`` -- the
    same tuple the software path already keeps.
    """
    frame_w = max(2, int(frame_w))
    frame_h = max(2, int(frame_h))
    view = viewport or (frame_w, frame_h)

    if transition is not None:
        return _plan_move(transition, frame_w, frame_h, view, now_ns)

    wanted = tuple(crops) if crops else (WHOLE,)
    placed, composed_w, composed_h = compose(wanted, frame_w, frame_h, view)
    pieces = [pixel_rect(crop, frame_w, frame_h) for crop, *_ in placed]
    upload = _bounding_px(pieces, frame_w, frame_h)

    blits = tuple(
        Blit(_src_within(piece, upload), (x, y, width, height))
        for piece, (_, x, y, width, height) in zip(pieces, placed)
    )
    return upload, blits, composed_w, composed_h, False


def _plan_move(transition, frame_w: int, frame_h: int, view, now_ns: int):
    """One frame of a camera move: upload the union, draw the travelling rect.

    The union is uploaded rather than the whole frame, so a move between two
    quadrants still costs half a frame, and a move ending at the whole picture
    costs exactly what the settled view will.

    The travelling rectangle is deliberately **not** snapped to even pixels --
    only the upload is. It moves sub-pixel, and rounding it is visible as the
    camera juddering two pixels at a time.
    """
    started, start, end = transition
    progress = eased(now_ns - started)
    current = lerp_rect(start, end, progress)

    upload = pixel_rect(union_rect(start, end), frame_w, frame_h)

    # Fitted to the travelling rectangle's own shape, so the picture keeps the
    # aspect ratio it actually has at each instant -- which is what the window
    # does today when it scales the zoom sub-rectangle with KeepAspectRatio.
    cur_w = max(frame_w * current[2], 1.0)
    cur_h = max(frame_h * current[3], 1.0)
    scale = min(view[0] / cur_w, view[1] / cur_h)
    width = max(2, (int(cur_w * scale) // 2) * 2)
    height = max(2, (int(cur_h * scale) // 2) * 2)

    cur_px = (
        frame_w * current[0],
        frame_h * current[1],
        max(1.0, frame_w * current[2]),
        max(1.0, frame_h * current[3]),
    )
    src = _src_within(cur_px, upload)

    return upload, (Blit(src, (0, 0, width, height)),), width, height, progress < 1.0

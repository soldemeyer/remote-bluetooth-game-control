"""Split-screen layouts, region assignments, and what a client should show.

Pure logic: stdlib only, no Qt, no PyAV, no sockets. That is deliberate and is
the same split ``common/video.py`` makes against the codec layer -- the rules
for "which part of the picture belongs to this player" are worth testing on any
machine, and they are the part of this feature most likely to be wrong in a way
nobody notices.

Three things live here and nothing else does:

* the vocabulary -- four layouts, eight region names, and which regions mean
  anything in which layout;
* :func:`resolve`, which turns *one layout plus a set of assignments* into the
  rectangles a client should draw;
* :class:`Rect`, normalised 0..1 so nothing here has to know the video's size.

What is **not** here: which controller belongs to whom (that is
``server/screen_state.py``), how a layout is detected (``videoserver/layout.py``),
and how a rectangle becomes pixels (``client/media/decoder.py``). The detector
must not know about controllers and the renderer must not know about
assignments; keeping the vocabulary in one dependency-free module is what lets
all three agree without depending on each other.

The merge rule is the subtle part
---------------------------------
A client can own several controllers, so it can own several regions. When those
regions happen to form a rectangle they must be shown as **one** crop -- a
player holding the two left-hand quadrants wants the left half of the screen,
not two stacked pictures with a seam through the middle.

When they do not form a rectangle they must **not** be merged into their
bounding box, because that box contains regions belonging to other players. A
client assigned ``upper_left`` and ``lower_right`` would otherwise be shown the
whole screen, quietly handing it both opponents' views. That case draws each
region separately instead.
"""

from __future__ import annotations

from dataclasses import dataclass

# -- layouts ---------------------------------------------------------------
#
# Strings rather than an enum: these cross the wire in JSON control messages
# and land in a config file, and a plain string round-trips through both
# without a converter. An unknown value is treated as FULL everywhere, which is
# what makes the whole feature fail open.

FULL = "FULL"
VERTICAL_2 = "VERTICAL_2"
HORIZONTAL_2 = "HORIZONTAL_2"
QUAD_4 = "QUAD_4"

LAYOUTS: tuple[str, ...] = (FULL, VERTICAL_2, HORIZONTAL_2, QUAD_4)

# -- regions ---------------------------------------------------------------

UPPER_LEFT = "upper_left"
UPPER_RIGHT = "upper_right"
LOWER_LEFT = "lower_left"
LOWER_RIGHT = "lower_right"
UPPER = "upper"
LOWER = "lower"
LEFT = "left"
RIGHT = "right"

REGIONS: tuple[str, ...] = (
    UPPER_LEFT, UPPER_RIGHT, LOWER_LEFT, LOWER_RIGHT,
    UPPER, LOWER, LEFT, RIGHT,
)

#: Which region names mean anything in each layout. A controller carries every
#: assignment it might need -- ``upper_left`` for a four-player game *and*
#: ``left`` for a two-player one -- and the irrelevant ones are filtered here
#: rather than the operator having to re-assign when the game changes mode.
#:
#: FULL has none: there is nothing to divide, so every client sees everything.
REGIONS_FOR_LAYOUT: dict[str, frozenset[str]] = {
    FULL: frozenset(),
    VERTICAL_2: frozenset({LEFT, RIGHT}),
    HORIZONTAL_2: frozenset({UPPER, LOWER}),
    QUAD_4: frozenset({UPPER_LEFT, UPPER_RIGHT, LOWER_LEFT, LOWER_RIGHT}),
}

#: The grid each layout divides the picture into, as (columns, rows).
_GRID: dict[str, tuple[int, int]] = {
    FULL: (1, 1),
    VERTICAL_2: (2, 1),
    HORIZONTAL_2: (1, 2),
    QUAD_4: (2, 2),
}

#: Where each region sits in its layout's grid, as (column, row).
_CELL: dict[str, tuple[int, int]] = {
    LEFT: (0, 0), RIGHT: (1, 0),
    UPPER: (0, 0), LOWER: (0, 1),
    UPPER_LEFT: (0, 0), UPPER_RIGHT: (1, 0),
    LOWER_LEFT: (0, 1), LOWER_RIGHT: (1, 1),
}


@dataclass(frozen=True, slots=True)
class Rect:
    """A sub-rectangle of the video, normalised to 0..1.

    Normalised because nothing in this module should know the capture's size:
    the resolution can change under us mid-session, and a rectangle in pixels
    would be silently wrong the moment it did. The renderer multiplies by the
    frame it actually has.
    """

    x: float
    y: float
    width: float
    height: float

    @property
    def is_full(self) -> bool:
        """True when this covers the whole picture, within rounding."""
        return (
            abs(self.x) < 1e-9
            and abs(self.y) < 1e-9
            and abs(self.width - 1.0) < 1e-9
            and abs(self.height - 1.0) < 1e-9
        )

    def scaled_to(self, width: int, height: int) -> tuple[int, int, int, int]:
        """Pixel ``(x, y, w, h)`` inside a ``width`` x ``height`` frame.

        Rounds the far edge rather than the size, so two regions that meet in
        the middle still meet after rounding instead of leaving a one-pixel
        seam or overlapping by one.
        """
        x0 = int(round(self.x * width))
        y0 = int(round(self.y * height))
        x1 = int(round((self.x + self.width) * width))
        y1 = int(round((self.y + self.height) * height))
        return x0, y0, max(1, x1 - x0), max(1, y1 - y0)


FULL_RECT = Rect(0.0, 0.0, 1.0, 1.0)


def regions_for_layout(layout: str) -> frozenset[str]:
    """Which region names are meaningful in ``layout``. Unknown -> none."""
    return REGIONS_FOR_LAYOUT.get(layout, frozenset())


def is_valid_layout(layout: object) -> bool:
    return isinstance(layout, str) and layout in LAYOUTS


def normalise_layout(layout: object) -> str:
    """Coerce anything to a known layout, defaulting to FULL.

    Every entry point runs input through this: a control message from the
    network, a manual override typed into the web GUI, a value read back from a
    config file written by an older build. FULL is the safe answer because it
    shows the player everything rather than nothing -- the feature fails open.
    """
    return layout if is_valid_layout(layout) else FULL


def normalise_regions(regions: object) -> list[str]:
    """Coerce anything to a list of known region names, order preserved.

    Duplicates and unknown names are dropped. Used on config load and on any
    region list arriving over the wire.
    """
    if not isinstance(regions, (list, tuple, set, frozenset)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for entry in regions:
        if isinstance(entry, str) and entry in REGIONS and entry not in seen:
            seen.add(entry)
            out.append(entry)
    return out


def resolve(layout: object, assigned: object) -> list[Rect]:
    """The rectangles a client should draw, given the layout and its regions.

    Returns an **empty list** to mean "show the whole picture". That is the
    answer for FULL, for a client with no assignment, and for a client whose
    assignments say nothing about the current layout -- a controller set up for
    four-player quadrants when the game is in a two-player vertical split. All
    three are ordinary situations rather than errors, and all three must leave
    the player watching the game rather than a blank window.

    One rectangle comes back when the assigned regions form a rectangle:
    ``{upper_left, lower_left}`` is the left half, all four quadrants is the
    whole picture. Several come back when they do not, and they are deliberately
    *not* merged into their bounding box -- see the module docstring.
    """
    layout = normalise_layout(layout)
    if layout == FULL:
        return []

    relevant = regions_for_layout(layout)
    names = [name for name in normalise_regions(assigned) if name in relevant]
    if not names:
        return []

    cells = {_CELL[name] for name in names}
    columns, rows = _GRID[layout]

    if len(cells) == columns * rows:
        return []                      # everything is assigned: that is full screen

    return [_cell_rect(block, columns, rows) for block in _merge(cells, columns, rows)]


# -- merging ---------------------------------------------------------------


def _merge(
    cells: set[tuple[int, int]], columns: int, rows: int
) -> list[tuple[int, int, int, int]]:
    """Group cells into as few rectangles as possible, without over-reaching.

    Returns blocks as ``(col, row, span_cols, span_rows)``.

    The bounding box is tried first and taken only when it is *entirely*
    assigned. That is the whole safety property: ``{upper_left, lower_right}``
    has a bounding box covering the screen, and taking it would show the client
    two views it has no claim to.

    Failing that, whole rows are taken, then whole columns, then whatever is
    left goes out on its own. On a 2x2 grid that is every case: the diagonals
    become two single cells, and an L-shape becomes one row plus one cell.
    """
    remaining = set(cells)
    blocks: list[tuple[int, int, int, int]] = []

    min_c = min(c for c, _ in remaining)
    max_c = max(c for c, _ in remaining)
    min_r = min(r for _, r in remaining)
    max_r = max(r for _, r in remaining)
    box = {
        (c, r)
        for c in range(min_c, max_c + 1)
        for r in range(min_r, max_r + 1)
    }
    if box <= remaining:
        return [(min_c, min_r, max_c - min_c + 1, max_r - min_r + 1)]

    for row in range(rows):
        whole = {(c, row) for c in range(columns)}
        if whole <= remaining:
            remaining -= whole
            blocks.append((0, row, columns, 1))

    for column in range(columns):
        whole = {(column, r) for r in range(rows)}
        if whole <= remaining:
            remaining -= whole
            blocks.append((column, 0, 1, rows))

    for cell in sorted(remaining):
        blocks.append((cell[0], cell[1], 1, 1))

    # Reading order, so two clients with the same assignment always draw the
    # same arrangement and a screenshot is reproducible.
    blocks.sort(key=lambda b: (b[1], b[0]))
    return blocks


def _cell_rect(
    block: tuple[int, int, int, int], columns: int, rows: int
) -> Rect:
    column, row, span_c, span_r = block
    return Rect(
        x=column / columns,
        y=row / rows,
        width=span_c / columns,
        height=span_r / rows,
    )


# -- presentation ----------------------------------------------------------


def tile(count: int, viewport_aspect: float) -> tuple[int, int]:
    """How to arrange ``count`` separate regions, as (columns, rows).

    Only reached when the regions did not merge into one rectangle, which on a
    2x2 grid means two or three of them. Picks the arrangement whose shape is
    closest to the viewport's so the result wastes the least space: two regions
    go side by side in a wide window and stacked in a tall one.
    """
    if count <= 1:
        return 1, 1

    best = (1, count)
    best_error = float("inf")
    for columns in range(1, count + 1):
        rows = -(-count // columns)          # ceil
        if columns * rows - count >= rows:   # a whole empty column: never useful
            continue
        aspect = columns / rows
        error = abs(aspect - viewport_aspect)
        if error < best_error:
            best_error = error
            best = (columns, rows)
    return best

"""Finding moving things, with no model at all.

Stdlib only -- no numpy, no ONNX, no GPU -- so it runs on the reference Pi, in
CI, and on any machine somebody tries this on. It is what makes the whole
chain demonstrable before a single model exists, the same role ``--mock-bt``
and ``--test-source`` already play elsewhere in this project.

WHAT IT IS, HONESTLY
---------------------
Background subtraction on a coarse grid, then flood-fill into blobs. It finds
**what is not the background**, which is not the same question as what a
player is. It will find a spinning coin, an explosion and a scrolling HUD, and
it will lose a character who stands still long enough to *become* background.
It produces no appearance vectors, so identity has viewport ownership,
continuity and controller correlation to work with and nothing else.

**Against a running background, not against the previous frame**, and the
difference is not subtle. Differencing consecutive frames lights up both the
place an entity left and the place it arrived, so a moving character produces
two blobs that do not overlap -- which the tracker correctly reads as two
short-lived entities rather than one moving one. Measured: four tracks in six
frames for a single square crossing the screen, none of them living long
enough to own a viewport. A background model gives one blob, at the entity's
current position, that persists.

That is enough for a split screen, where the operator has already said which
viewport belongs to whom, and it is enough to exercise every rule downstream.
It is **not** enough to identify anybody on a shared screen by appearance, and
nothing here pretends otherwise: ``embeddings`` is False and the capability
line says so in words the operator will read.

WHY A COARSE GRID
------------------
The obvious implementation walks every pixel. At 320x180 that is 57,600 Python
byte reads per frame, and at 6 Hz on a Pi it is not affordable next to an
encoder. The grid samples a few pixels per cell instead, which turns the frame
into ~1,200 comparisons -- and the resolution lost is resolution a bounding
box for a label never needed.
"""

from __future__ import annotations

import logging

from common.screen_regions import Rect

from ..types import Detection
from .base import Capabilities, SampleFrame, PlayerVisionBackend

log = logging.getLogger(__name__)

__all__ = ["HeuristicBackend"]

#: Cell size in pixels of the sampled frame. A label's box does not need finer
#: than this, and every halving quadruples the work.
CELL = 8

#: Pixels probed per cell, per axis. Four samples in a 8x8 cell is enough to
#: tell "this part of the picture changed" from "it did not", and it is what
#: keeps the whole pass inside a millisecond.
PROBES = 2

#: How far a cell must sit from the background to count. Below this is sensor
#: noise and encoder ringing, which are present in every frame of every
#: capture and would otherwise light up the entire grid.
LEVEL_DELTA = 18

#: How fast the background follows the picture, as a right shift.
#:
#: Three is a time constant of roughly eight frames -- a second and a bit at
#: the default rate. Fast enough that a camera pan or a lighting change is
#: absorbed rather than reported as a screenful of entities; slow enough that
#: a character who pauses keeps their track for a few seconds, which is long
#: enough for continuity to carry them.
BACKGROUND_SHIFT = 3

#: How many of a cell's probes must have changed for the cell to count.
#: Requiring more than one is what stops a single noisy pixel seeding a blob.
MIN_PROBES = 2

#: Cells a blob must have before it is reported. One cell is 8x8 pixels of a
#: downscaled frame -- far too small to be a character, and exactly the size
#: of a compression artefact.
MIN_CELLS = 4

#: The most blobs one frame may yield, largest first. A scene change lights up
#: the whole grid; reporting four hundred boxes would cost the tracker far
#: more than it costs to find them, and none of them would be a player.
MAX_BLOBS = 12

#: A blob covering more of the frame than this is the camera panning, a scene
#: cut or a flash -- not an entity. Reporting it would hand a viewport's
#: identity to the background.
MAX_BLOB_FRACTION = 0.5


class HeuristicBackend(PlayerVisionBackend):
    """Motion blobs. No models, no GPU, no appearance."""

    name = "heuristic"
    isolated = False
    embeddings = False

    def __init__(self) -> None:
        #: The running background, flat, row-major by cell. Not the previous
        #: frame -- see the module docstring.
        self._background: list[int] | None = None
        self._shape: tuple[int, int] = (0, 0)
        self.frames = 0
        self.blobs = 0

    @classmethod
    def probe(cls) -> Capabilities:
        return Capabilities(
            backend=cls.name,
            available=True,
            reason="",
            device="CPU",
            embeddings=False,
        )

    def start(self) -> Capabilities:
        self._background = None
        return self.probe()

    def stop(self) -> None:
        self._background = None

    def detect(self, frame: SampleFrame) -> list[Detection]:
        if frame.width <= 0 or frame.height <= 0:
            return []

        columns = max(1, frame.width // CELL)
        rows = max(1, frame.height // CELL)
        current = self._sample(frame, columns, rows)
        self.frames += 1

        background = self._background
        if background is None or self._shape != (columns, rows):
            # First frame, or the capture changed size. Nothing to compare
            # against; calling the whole grid foreground would be far worse
            # than reporting nothing.
            self._background = current
            self._shape = (columns, rows)
            return []
        self._shape = (columns, rows)

        moved = self._foreground_cells(background, current)
        self._age_background(background, current)
        if not moved:
            return []

        blobs = self._blobs(moved, columns, rows)
        detections = self._boxes(blobs, columns, rows)
        self.blobs += len(detections)
        return detections

    def snapshot(self) -> dict[str, object]:
        return {"backend": self.name, "frames": self.frames, "blobs": self.blobs}

    # -- internals ---------------------------------------------------------

    def _sample(self, frame: SampleFrame, columns: int, rows: int) -> list[int]:
        """Probe values per cell, summed. One pass, no allocation per cell."""
        data = frame.data
        stride = frame.stride
        step = max(1, CELL // PROBES)

        values: list[int] = []
        append = values.append
        for row in range(rows):
            base_y = row * CELL
            for column in range(columns):
                base_x = column * CELL
                total = 0
                for dy in range(0, CELL, step):
                    offset = (base_y + dy) * stride + base_x
                    for dx in range(0, CELL, step):
                        total += data[offset + dx]
                append(total)
        return values

    @staticmethod
    def _age_background(background: list[int], current: list[int]) -> None:
        """Let the background drift towards the picture. In place, no alloc."""
        for index, value in enumerate(current):
            background[index] += (value - background[index]) >> BACKGROUND_SHIFT

    def _foreground_cells(
        self, background: list[int], current: list[int]
    ) -> set[int]:
        """Cell indices sitting far enough from the background to count."""
        # Every probe in the cell is summed, so the threshold scales with the
        # probe count rather than being a per-pixel number in disguise.
        probes = (CELL // max(1, CELL // PROBES)) ** 2
        floor = LEVEL_DELTA * MIN_PROBES
        ceiling = LEVEL_DELTA * probes
        moved: set[int] = set()
        for index, (was, now) in enumerate(zip(background, current)):
            delta = now - was
            if delta < 0:
                delta = -delta
            if delta >= min(floor, ceiling):
                moved.add(index)
        return moved

    @staticmethod
    def _blobs(moved: set[int], columns: int, rows: int) -> list[set[int]]:
        """Four-connected groups of changed cells.

        Iterative flood fill rather than recursion: a scene change can connect
        every cell in the grid, and a recursive fill would hit Python's
        recursion limit and raise -- on the one path that must not.
        """
        remaining = set(moved)
        groups: list[set[int]] = []
        while remaining:
            seed = remaining.pop()
            group = {seed}
            stack = [seed]
            while stack:
                index = stack.pop()
                row, column = divmod(index, columns)
                for neighbour in (
                    index - 1 if column > 0 else -1,
                    index + 1 if column < columns - 1 else -1,
                    index - columns if row > 0 else -1,
                    index + columns if row < rows - 1 else -1,
                ):
                    if neighbour >= 0 and neighbour in remaining:
                        remaining.discard(neighbour)
                        group.add(neighbour)
                        stack.append(neighbour)
            groups.append(group)
        return groups

    @staticmethod
    def _boxes(
        blobs: list[set[int]], columns: int, rows: int
    ) -> list[Detection]:
        """Normalised bounding boxes, largest first, bounded in number."""
        total_cells = columns * rows
        scored: list[tuple[int, Detection]] = []
        for group in blobs:
            if len(group) < MIN_CELLS:
                continue
            if len(group) > total_cells * MAX_BLOB_FRACTION:
                # The camera panned, or the scene cut. Not an entity.
                continue

            first_column = columns
            last_column = -1
            first_row = rows
            last_row = -1
            for index in group:
                row, column = divmod(index, columns)
                first_column = min(first_column, column)
                last_column = max(last_column, column)
                first_row = min(first_row, row)
                last_row = max(last_row, row)

            x = first_column / columns
            y = first_row / rows
            width = (last_column + 1) / columns - x
            height = (last_row + 1) / rows - y
            scored.append(
                (
                    len(group),
                    Detection(
                        box=Rect(x, y, width, height),
                        # How much of its own bounding box the blob fills:
                        # a solid shape is far likelier to be one entity than
                        # a sparse scatter that happens to be connected.
                        score=round(
                            len(group)
                            / max(1, (last_column - first_column + 1)
                                  * (last_row - first_row + 1)),
                            3,
                        ),
                    ),
                )
            )

        scored.sort(key=lambda item: item[0], reverse=True)
        return [detection for _, detection in scored[:MAX_BLOBS]]

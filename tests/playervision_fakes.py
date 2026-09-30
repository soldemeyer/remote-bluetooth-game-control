"""A stand-in detector for tests, so the identification chain runs without a model.

The product has no model-free backend any more: identification is the model's
job, and a detector that guessed from motion labelled HUD icons and other karts
as the player. But every rule downstream of detection -- tracking, viewport
ownership, correlation, the wire, the per-viewport filtering, the drawing -- is
worth testing on any machine, and a real model needs an optional extra and a
download.

So the tests bring their own. `BrightBoxBackend` finds bright rectangles in a
frame, which is what every synthetic frame in this suite draws. It is
deterministic and knows nothing about games: it is a fixture, not a detection
method, and it lives under ``tests/`` so no build can ship it.

It is **registered**, never named in settings. ``player_id_backend`` is clamped
to known values before it reaches the service, so nothing arriving over the
wire can select a registered backend; tests reach it through ``auto``, which
tries registered backends before the model.
"""

from __future__ import annotations

import contextlib
from typing import Iterator

from common.screen_regions import Rect
from videoserver.playervision.backends.base import (
    Capabilities,
    PlayerVisionBackend,
    SampleFrame,
)
from videoserver.playervision.types import Detection

__all__ = [
    "BrightBoxBackend",
    "IsolatedBrightBoxBackend",
    "MODULE_PATH",
    "registered",
]

#: What the child process is told to import. Shaped ``module:Class``.
MODULE_PATH = "tests.playervision_fakes:IsolatedBrightBoxBackend"

#: A pixel at or above this is part of a box.
BRIGHT = 180

#: Grid cell, in pixels of the sampled frame. Small enough that a 44-pixel
#: square on a 320-wide sample comes back within a cell of its true edges.
CELL = 4


class BrightBoxBackend(PlayerVisionBackend):
    """Finds bright rectangles. Inline, luma, no embeddings."""

    name = "brightbox"
    isolated = False
    embeddings = False

    def __init__(self) -> None:
        self.frames = 0
        self.found = 0

    @classmethod
    def probe(cls) -> Capabilities:
        return Capabilities(backend=cls.name, available=True, device="CPU")

    def start(self) -> Capabilities:
        return self.probe()

    def stop(self) -> None:
        return None

    def detect(self, frame: SampleFrame) -> list[Detection]:
        self.frames += 1
        columns = max(1, frame.width // CELL)
        rows = max(1, frame.height // CELL)
        channels = 3 if frame.pixel_format == "rgb24" else 1
        data = frame.data
        lit: set[int] = set()
        for row in range(rows):
            y = row * CELL + CELL // 2
            base = y * frame.stride
            for column in range(columns):
                x = (column * CELL + CELL // 2) * channels
                if data[base + x] >= BRIGHT:
                    lit.add(row * columns + column)

        found: list[Detection] = []
        while lit:
            seed = lit.pop()
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
                    if neighbour >= 0 and neighbour in lit:
                        lit.discard(neighbour)
                        group.add(neighbour)
                        stack.append(neighbour)
            if len(group) < 4:
                continue
            cols = [index % columns for index in group]
            rws = [index // columns for index in group]
            x0, x1 = min(cols), max(cols) + 1
            y0, y1 = min(rws), max(rws) + 1
            found.append(
                Detection(
                    box=Rect(
                        x0 / columns, y0 / rows,
                        (x1 - x0) / columns, (y1 - y0) / rows,
                    ),
                    score=0.9,
                )
            )
        found.sort(key=lambda d: d.box.width * d.box.height, reverse=True)
        self.found += len(found)
        return found

    def snapshot(self) -> dict[str, object]:
        return {"backend": self.name, "frames": self.frames, "found": self.found}


class IsolatedBrightBoxBackend(BrightBoxBackend):
    """The same, run in its own process -- how a model backend runs."""

    name = "brightbox-isolated"
    isolated = True


@contextlib.contextmanager
def registered(*backends: type[PlayerVisionBackend]) -> Iterator[None]:
    """Register backends for the duration of a test, and always unregister.

    Defaults to the inline one. A registration that outlived its test would
    change what ``auto`` resolves to for every test after it.
    """
    from videoserver.playervision.service import register_backend, unregister_backend

    chosen = backends or (BrightBoxBackend,)
    for backend in chosen:
        register_backend(backend)
    try:
        yield
    finally:
        for backend in chosen:
            unregister_backend(backend.name)

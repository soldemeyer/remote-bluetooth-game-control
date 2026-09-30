"""What the debug overlay draws, decided without a toolkit.

Stdlib only -- no Qt, no PyAV -- for the same reason ``client/media/planner.py``
is: the part most likely to be silently wrong is *where a box lands and what it
says*, and that is arithmetic. A wrong rectangle drawn perfectly looks exactly
like a right one, so the geometry is tested with tuples and the painting is
left to the window, which only copies pixels.

**This is the operator's view, not a player's.** It is drawn on the video
server's own preview and nowhere else: not into the encoded stream, not into
the preview the Bluetooth server relays to its web GUI. A player watching their
game must never inherit somebody else's debugging, and a box saying
``unidentified`` over a character is precisely the "confidently wrong display"
this project keeps having to unpick.

It deliberately shows **every track**, including the ones with no player
attached. An entity the detector found and the identity manager refused is the
single most useful thing on screen when the question is *why is nobody being
labelled* -- and it is exactly what the player-facing path throws away.
"""

from __future__ import annotations

from dataclasses import dataclass

from common.screen_regions import Rect

from .types import Judgement, SignalScore, TrackedPlayer

__all__ = [
    "OverlayBox",
    "TONE_IDENTIFIED",
    "TONE_UNIDENTIFIED",
    "TONE_WEAK",
    "box_pixels",
    "breakdown_lines",
    "overlay_boxes",
]

#: A name is being published for this track.
TONE_IDENTIFIED = "identified"
#: Identified, but not by enough to be believed without looking.
#:
#: Split out from `TONE_IDENTIFIED` because continuity publishes at 0.70 and
#: appearance can publish just over the floor, and those carry a label that is
#: *held* rather than *recognised*. An operator deciding whether to trust what
#: they are seeing needs that separable at a glance.
TONE_WEAK = "weak"
#: Something is there and nobody owns it.
TONE_UNIDENTIFIED = "unidentified"

#: Above this an identification is drawn as settled rather than provisional.
#:
#: The same number `GALLERY_MIN_CONFIDENCE` uses, and for a related reason:
#: that is the bar this subsystem already treats as "sure enough to learn
#: from", so it is the honest place to stop hedging on screen too.
STRONG_CONFIDENCE = 0.80

#: Longest note rendered inside a box before it is cut.
#:
#: The full text is always in the panel; the box has to stay smaller than the
#: thing it is annotating or it hides the entity it is pointing at.
MAX_BOX_NOTE = 44


@dataclass(frozen=True, slots=True)
class OverlayBox:
    """One rectangle to draw, and what to write on it."""

    track_id: int
    box: Rect
    #: First line: who this is, or that nobody knows.
    title: str
    #: Second line: how it was decided, or why it was not.
    detail: str
    tone: str = TONE_UNIDENTIFIED
    player_id: int = 0
    confidence: float = 0.0
    #: Which signal produced the answer, and which cell it was found in.
    #: Carried rather than parsed back out of `detail`: recovering data from a
    #: string built for display is how a wording change silently empties a
    #: column.
    source: str = "none"
    region: str = ""

    @property
    def identified(self) -> bool:
        return self.tone in (TONE_IDENTIFIED, TONE_WEAK)


def _tone(row: TrackedPlayer) -> str:
    if not row.identified:
        return TONE_UNIDENTIFIED
    if row.confidence >= STRONG_CONFIDENCE:
        return TONE_IDENTIFIED
    return TONE_WEAK


def _shorten(text: str, limit: int = MAX_BOX_NOTE) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def overlay_boxes(
    rows: list[TrackedPlayer] | tuple[TrackedPlayer, ...],
    judgements: list[Judgement] | tuple[Judgement, ...] = (),
) -> tuple[OverlayBox, ...]:
    """Join the published rows with their reasoning, ready to draw.

    The rows carry the geometry and the reasoning carries the explanation, and
    they are joined on ``track_id`` rather than by position: an isolated
    backend delivers them in the same message but a dropped or malformed entry
    on either side would otherwise shift every box onto the wrong entity --
    silently, and looking entirely plausible.

    A row with no matching judgement still draws. Losing the explanation must
    cost the explanation only, never the box.
    """
    by_track = {j.track_id: j for j in judgements}
    boxes: list[OverlayBox] = []
    for row in rows:
        judgement = by_track.get(row.track_id)
        if row.identified:
            title = f"Player {row.player_id}"
            detail = f"{row.confidence:.2f} · {row.source}"
        else:
            title = "unidentified"
            # The note is the whole value here: "no player map" and "two
            # candidates too close to call" are different faults and neither
            # is visible from the picture.
            detail = _shorten(
                judgement.note if judgement is not None and judgement.note
                else "nothing matched this track"
            )
        boxes.append(
            OverlayBox(
                track_id=row.track_id,
                box=row.box,
                title=title,
                detail=detail,
                tone=_tone(row),
                player_id=row.player_id,
                confidence=row.confidence,
                source=row.source,
                region=row.region,
            )
        )
    return tuple(boxes)


def box_pixels(
    box: Rect, width: int, height: int
) -> tuple[int, int, int, int]:
    """A normalised box as ``(x, y, w, h)`` pixels inside ``width x height``.

    Clamped to the surface, and never narrower than a pixel. A detector may
    report a box whose edge sits a hair outside the frame, and a rectangle
    with negative width draws as nothing at all -- which reads as the entity
    not having been found.
    """
    left = min(max(box.x, 0.0), 1.0)
    top = min(max(box.y, 0.0), 1.0)
    right = min(max(box.x + box.width, 0.0), 1.0)
    bottom = min(max(box.y + box.height, 0.0), 1.0)

    x = int(round(left * width))
    y = int(round(top * height))
    w = max(1, int(round((right - left) * width)))
    h = max(1, int(round((bottom - top) * height)))
    # Keep the far edge inside the surface after rounding, rather than
    # trusting that two roundings agree.
    w = min(w, max(1, width - x))
    h = min(h, max(1, height - y))
    return x, y, w, h


def _score_line(score: SignalScore) -> str:
    mark = "->" if score.used else "  "
    who = f"P{score.player_id}" if score.player_id else "--"
    head = f"{mark} {score.signal:<10} {who:>3} {score.score:5.2f}"
    return f"{head}  {score.note}".rstrip()


def breakdown_lines(judgement: Judgement) -> tuple[str, ...]:
    """The full reasoning for one track, as lines for a monospaced panel.

    Every signal that looked, in the order they were tried, with the one that
    produced the answer marked. A signal that was never consulted is listed
    too, carrying the reason -- its absence would read as "this signal found
    nothing", which points at the model when the truth is that a stronger
    signal had already settled the track.
    """
    if judgement.identified:
        head = (
            f"Track #{judgement.track_id}: Player {judgement.player_id} "
            f"at {judgement.confidence:.2f} via {judgement.source}"
        )
    else:
        head = f"Track #{judgement.track_id}: no player"

    lines = [head]
    if judgement.region:
        lines.append(f"   in {judgement.region}")
    if judgement.note:
        lines.append(f"   {judgement.note}")
    if not judgement.scores:
        lines.append("   no signal scored this track")
    lines.extend(f"   {_score_line(score)}" for score in judgement.scores)
    return tuple(lines)

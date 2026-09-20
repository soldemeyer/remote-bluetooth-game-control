"""Holding, smoothing and expiring the labels the server sends.

Stdlib only -- no Qt, no PyAV -- so the arithmetic that decides where a name
is drawn and when it disappears is testable without a window. The widget above
this does the pixel mapping, using rectangles it has already computed for the
picture, and nothing here knows what a widget is.

WHY IT SMOOTHS TOWARDS, RATHER THAN INTERPOLATING BETWEEN
-----------------------------------------------------------
Labels arrive about ten times a second and the window paints at sixty, so
something has to fill the gap. The textbook answer is to interpolate between
the last two reported positions, which is perfectly smooth and puts every
label a full update *behind* -- and these are already behind: identified on
the source at a few hertz, shipped to the Bluetooth server, filtered, shipped
here. Adding another 100 ms to a figure that is already 150-250 ms would be
paying for smoothness with the one thing there is none of.

So the drawn position eases towards the newest reported one with a short time
constant. No overshoot, no added lag, and a label that stops moving settles
rather than drifting past.

WHAT MAKES A LABEL GO AWAY
---------------------------
Three things, and all three matter:

  * **the server stops mentioning it** -- the entity left, the player left, or
    the source went quiet. Expiry is what turns silence into the label
    vanishing rather than freezing over whatever is now in that spot.
  * **confidence drops** below the floor. A wrong name is worse than no name,
    and this is the last place that rule is enforced.
  * **the stream restarts or the layout changes**, where the previous
    positions describe a picture that no longer exists.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["Label", "LabelStore", "anchor_in"]

#: How long a label survives without being mentioned again.
#:
#: Six update periods at the server's 10 Hz. Long enough to ride out a lost
#: datagram or two on a bad connection -- this channel has no retransmit --
#: and short enough that a name does not hang over an empty patch of screen
#: for anything a player would call a moment.
STALE_NS = 600_000_000

#: Time constant of the easing, in nanoseconds. About four frames at 60 Hz:
#: fast enough to keep up with a character at speed, slow enough to take the
#: step out of a 10 Hz update.
SMOOTH_NS = 60_000_000

#: Below this, draw nothing. The floor the server already applied is about
#: which labels to *send*; this is the player's own last line of defence, and
#: it is deliberately a separate number so a client can be stricter.
MIN_CONFIDENCE = 0.55


@dataclass(slots=True)
class Label:
    """One name to draw, in whole-frame normalised coordinates.

    ``x``/``y``/``w``/``h`` are the entity's box as the server reported it;
    ``draw_x``/``draw_y`` are the eased anchor actually used, which is what
    the window asks for.
    """

    player_id: int
    track_id: int
    name: str
    region: str
    x: float
    y: float
    w: float
    h: float
    confidence: float
    updated_ns: int
    draw_x: float = 0.0
    draw_y: float = 0.0
    eased_ns: int = 0

    @property
    def anchor(self) -> tuple[float, float]:
        """Where the name points: the top-middle of the entity's box.

        The top rather than the centre, because the label is drawn *above* the
        character and a centre anchor would put it over their head at one size
        and over their feet at another.
        """
        return self.draw_x, self.draw_y


def anchor_in(
    label: Label, crop: tuple[float, float, float, float]
) -> tuple[float, float] | None:
    """Where inside ``crop`` this label sits, 0..1. ``None`` if outside.

    The containment test uses the **anchor**, not the box: an entity half in
    one viewport and half in another belongs to one of them, and the anchor is
    the point the name is drawn at. Testing overlap instead would draw the
    same name in two of a client's views at once.

    Returning ``None`` rather than clamping is the safe half of this. A label
    whose anchor is in none of the crops a client holds is simply not drawn --
    the feature fails open to *less*, never to a name over somebody else's
    picture.
    """
    x, y, width, height = crop
    if width <= 0.0 or height <= 0.0:
        return None
    ax, ay = label.anchor
    if not (x <= ax <= x + width and y <= ay <= y + height):
        return None
    return (ax - x) / width, (ay - y) / height


class LabelStore:
    """The newest labels, eased and expiring. One thread: the GUI's."""

    def __init__(
        self,
        *,
        stale_ns: int = STALE_NS,
        smooth_ns: int = SMOOTH_NS,
        min_confidence: float = MIN_CONFIDENCE,
    ) -> None:
        self.stale_ns = stale_ns
        self.smooth_ns = smooth_ns
        self.min_confidence = min_confidence
        self.layout = "FULL"
        self._labels: dict[int, Label] = {}

    # -- in ----------------------------------------------------------------

    def ingest(self, layout: str, labels: list[dict], now_ns: int) -> None:
        """Absorb one PLAYER_LABELS body.

        Each message is the client's **complete** answer, so anything not
        mentioned is on its way out -- but it is expired by the clock rather
        than dropped here, so one lost datagram does not make every label
        flicker. This channel has no retransmit, and a flicker is what a
        wholesale replace would look like on a lossy link.

        A layout change clears everything: the previous positions describe a
        picture that is no longer on screen, and easing towards a new one
        across a layout change would drag every name over the whole screen.
        """
        if layout != self.layout:
            self.layout = layout
            self._labels.clear()

        for raw in labels:
            try:
                track_id = int(raw["track_id"])
                confidence = float(raw["confidence"])
            except (KeyError, TypeError, ValueError):
                continue
            if confidence < self.min_confidence:
                # Below the floor. Dropped rather than kept and hidden, so it
                # cannot come back on the next frame by a rounding accident.
                self._labels.pop(track_id, None)
                continue

            x = _unit(raw.get("x"))
            y = _unit(raw.get("y"))
            width = _unit(raw.get("w"))
            height = _unit(raw.get("h"))
            anchor_x = x + width / 2.0
            anchor_y = y

            existing = self._labels.get(track_id)
            if existing is None:
                # A new label appears where it is, not eased in from the last
                # place something else happened to be.
                existing = Label(
                    player_id=int(raw.get("player_id", 0)),
                    track_id=track_id,
                    name=str(raw.get("name", "")),
                    region=str(raw.get("region", "")),
                    x=x, y=y, w=width, h=height,
                    confidence=confidence,
                    updated_ns=now_ns,
                    draw_x=anchor_x,
                    draw_y=anchor_y,
                    eased_ns=now_ns,
                )
                self._labels[track_id] = existing
                continue

            existing.player_id = int(raw.get("player_id", existing.player_id))
            existing.name = str(raw.get("name", existing.name))
            existing.region = str(raw.get("region", existing.region))
            existing.x, existing.y = x, y
            existing.w, existing.h = width, height
            existing.confidence = confidence
            existing.updated_ns = now_ns

    def clear(self) -> None:
        """Forget everything. A stream restart, or labels switched off."""
        self._labels.clear()

    # -- out ---------------------------------------------------------------

    def visible(self, now_ns: int) -> list[Label]:
        """The labels to draw right now, eased to this instant.

        Expiry happens here rather than on ingest so a client that stops
        receiving -- the server gone, the source quiet, the network dropped --
        still watches its labels disappear on time. A store that only expired
        on the next message would freeze them for ever at exactly the moment
        they became wrong.
        """
        alive: list[Label] = []
        for track_id, label in list(self._labels.items()):
            if now_ns - label.updated_ns > self.stale_ns:
                del self._labels[track_id]
                continue
            self._ease(label, now_ns)
            alive.append(label)
        # Stable order, so two names at the same spot do not swap every frame.
        alive.sort(key=lambda item: (item.player_id, item.track_id))
        return alive

    def _ease(self, label: Label, now_ns: int) -> None:
        elapsed = now_ns - label.eased_ns
        label.eased_ns = now_ns
        target_x = label.x + label.w / 2.0
        target_y = label.y
        if elapsed <= 0:
            return
        if self.smooth_ns <= 0:
            label.draw_x, label.draw_y = target_x, target_y
            return
        # Exponential approach: frame-rate independent, and it cannot
        # overshoot however long a frame took.
        alpha = 1.0 - math.exp(-elapsed / self.smooth_ns)
        label.draw_x += (target_x - label.draw_x) * alpha
        label.draw_y += (target_y - label.draw_y) * alpha

    def __len__(self) -> int:
        return len(self._labels)


def _unit(value: object) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if result != result:      # NaN sails through every range check
        return 0.0
    return min(1.0, max(0.0, result))

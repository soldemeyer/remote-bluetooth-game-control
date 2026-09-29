"""Holding, smoothing and expiring the labels the server sends.

Stdlib only -- no Qt, no PyAV -- so the arithmetic that decides where a name
is drawn and when it disappears is testable without a window. The widget above
this does the pixel mapping, using rectangles it has already computed for the
picture, and nothing here knows what a widget is.

WHY IT PREDICTS, RATHER THAN INTERPOLATING OR CHASING
-------------------------------------------------------
New positions arrive a few to fifteen times a second and the window paints at
sixty, so something has to fill the gap. The textbook answer is to
interpolate between the last two reported positions, which is perfectly
smooth and puts every label a full update *behind* -- and these are already
behind: identified on the source, shipped to the Bluetooth server, filtered,
shipped here.

Two versions of this got it wrong in ways worth remembering, both reported:

* **a 60 ms ease towards the newest position was stop-and-go.** It covered
  each step in a fraction of the gap and sat still for the rest: 19% of
  frames barely moved;
* **a critically damped follower was smooth and trailed.** Anything that
  chases a moving target trails it by its own smoothing time -- 119 ms,
  measured, whatever the update rate -- and that was reported as the names
  lagging behind the characters.

So the name is drawn **at the prediction** -- the last sample moved on at the
character's own speed -- and smoothing applies only to the *correction* each
new sample makes, which fades out without a jump in position or in speed. At
a steady speed that adds no lag at all; a change of direction is a curve. See
`SMOOTH_NS` for the measurements.

The speed is estimated only from positions that actually changed. The
Bluetooth server also repeats the latest tracks on its status tick, and
counting those repeats as "not moving" zeroed the speed every other message.
A jump too large to be motion -- a different box for the same character --
resets the speed rather than flinging the label.

**One label per player per view, not per track.** Identity places a player
once per viewport, but the track under the name changes whenever the model
boxes a different piece of the character. Keyed by track, every change made a
new label that appeared where it was -- a jump -- while the old one lingered
until it expired.

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

__all__ = ["Bubble", "Label", "LabelStore", "anchor_in", "place_bubble"]

#: How long a label survives without being mentioned again.
#:
#: Six update periods at the server's 10 Hz. Long enough to ride out a lost
#: datagram or two on a bad connection -- this channel has no retransmit --
#: and short enough that a name does not hang over an empty patch of screen
#: for anything a player would call a moment.
STALE_NS = 600_000_000

#: How long a correction takes to fade out, in nanoseconds. The name is drawn
#: at the character's *predicted* position; this only smooths the difference
#: each new sample makes. Chosen by measurement, in the simulation
#: `tests/test_client_player_labels.py` runs -- a character moving steadily,
#: a new sample every 66 ms delivered as it arrives, painted at 60 Hz:
#:
#:                                    barely moving  unevenness  trails by
#:   60 ms ease (first)                    19%          0.81       154 ms
#:   120 ms follower (second)               0%          0.04       119 ms
#:   prediction, 100 ms fade (this)         0%          0.06         6 ms
#:
#: The follower was smooth and trailed the character by its own smoothing
#: time whatever the update rate -- which was reported as the names lagging.
#: "Unevenness" is the spread of the per-frame step against its mean. The
#: cost is an overshoot when a character stops dead: about 3% of the picture's
#: width at a fast 0.3 widths a second, faded back within about 0.15 s.
SMOOTH_NS = 100_000_000

#: Below this, draw nothing. The floor the server already applied is about
#: which labels to *send*; this is the player's own last line of defence, and
#: it is deliberately a separate number so a client can be stricter.
MIN_CONFIDENCE = 0.55

#: How much of each new speed reading is believed. Half: a detector's box
#: wobbles by a few pixels from sample to sample, and every wobble divided by
#: a sixth of a second is a speed.
VELOCITY_BLEND = 0.5

#: A step larger than this, in whole-frame units, is not motion but a
#: different box for the same character -- a cap, then the whole kart. It
#: moves the label, eased, and resets the speed rather than flinging it.
TELEPORT = 0.08

#: The fastest a label may be predicted to move, in whole-frame units per
#: second. Past this a reading is noise, not a kart.
MAX_SPEED = 1.5

#: How many sample intervals ahead a label may be predicted. One and a half:
#: a sample that arrives a little late -- network jitter, a busy worker --
#: must not stop the prediction short, or the name pauses. Measured with
#: samples noticed 100 or 200 ms apart: one interval left 19% of frames
#: barely moving, one and a half 3%.
LEAD_INTERVALS = 1.5

#: What the sample interval is assumed to be before one has been measured,
#: and the bounds on what is believed afterwards.
DEFAULT_INTERVAL_NS = 150_000_000
MIN_INTERVAL_NS = 40_000_000
MAX_INTERVAL_NS = 400_000_000


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
    #: When the reported position last *changed* -- a new sample, as opposed
    #: to the Bluetooth server repeating the last one.
    moved_ns: int = 0
    #: Estimated speed of the anchor, whole-frame units per second.
    vx: float = 0.0
    vy: float = 0.0
    #: How often new samples arrive for this label, smoothed.
    interval_ns: int = DEFAULT_INTERVAL_NS
    #: The correction still being faded out, and how fast it is changing.
    #: The name is drawn at the predicted position plus this; see `_ease`.
    off_x: float = 0.0
    off_y: float = 0.0
    off_vx: float = 0.0
    off_vy: float = 0.0

    @property
    def anchor(self) -> tuple[float, float]:
        """Where the name points: the top-middle of the entity's box.

        The top rather than the centre, because the label is drawn *above* the
        character and a centre anchor would put it over their head at one size
        and over their feet at another.
        """
        return self.draw_x, self.draw_y


#: The pointer under a name bubble, in pixels at 1x. Small: it says *which*
#: character, and a large one would cover the character's head.
TAIL_HEIGHT = 7
TAIL_WIDTH = 12


@dataclass(frozen=True, slots=True)
class Bubble:
    """Where a name bubble goes, and the pointer from it to the character.

    ``tail`` is three points -- the two ends of its base on the bubble's edge,
    then the tip at the character -- or empty when there is no room for one:
    the bubble has been pushed onto the character by its view's edge, and a
    pointer from a bubble that already covers the character would point at
    nothing.
    """

    x: int
    y: int
    width: int
    height: int
    tail: tuple[tuple[int, int], ...] = ()


def place_bubble(
    anchor: tuple[int, int],
    size: tuple[int, int],
    bounds: tuple[int, int, int, int],
    *,
    tail_height: int = TAIL_HEIGHT,
    tail_width: int = TAIL_WIDTH,
    radius: int = 6,
) -> Bubble:
    """A name bubble above ``anchor``, kept inside ``bounds``, with a pointer.

    Pure, so both painters -- the window's and the GPU overlay's -- place a
    bubble identically and the arithmetic tests without Qt. ``bounds`` is
    ``(left, top, right, bottom)`` of the view the name belongs to: a bubble
    pushed out of its own view would land on the neighbouring player's
    picture.

    **The pointer follows the character, not the bubble.** When the bubble is
    pushed sideways by the view's edge its base slides along the bubble's
    bottom, kept clear of the rounded corners, and the tip stays on the
    character -- so a slanted pointer still says whose name it is.
    """
    ax, ay = anchor
    width, height = size
    left, top, right, bottom = bounds

    x = ax - width // 2
    y = ay - height - tail_height
    x = max(left, min(x, right - width))
    y = max(top, min(y, bottom - height))

    half = tail_width // 2
    low = x + radius + half
    high = x + width - radius - half
    base = min(max(ax, low), high) if low <= high else x + width // 2

    # The only way the bubble moves off its place above the character is
    # *down*, onto it, by the top of its view -- an anchor is always inside
    # its view, so nothing pushes a bubble below one. Then there is nothing to
    # point at and no pointer.
    tail: tuple[tuple[int, int], ...] = ()
    if ay > y + height:
        edge = y + height
        tail = ((base - half, edge), (base + half, edge), (ax, ay))
    return Bubble(x, y, width, height, tail)


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
        lead: float = LEAD_INTERVALS,
    ) -> None:
        self.stale_ns = stale_ns
        self.smooth_ns = smooth_ns
        self.lead = lead
        self.min_confidence = min_confidence
        self.layout = "FULL"
        #: Keyed by (player, region): one label per player per view. See the
        #: module note -- keyed by track, a new piece of the same character
        #: was a new label that jumped.
        self._labels: dict[tuple[int, str], Label] = {}

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
                player_id = int(raw.get("player_id", 0))
            except (KeyError, TypeError, ValueError):
                continue
            region = str(raw.get("region", ""))
            key = (player_id, region)
            if confidence < self.min_confidence:
                # Below the floor. Dropped rather than kept and hidden, so it
                # cannot come back on the next frame by a rounding accident.
                self._labels.pop(key, None)
                continue

            x = _unit(raw.get("x"))
            y = _unit(raw.get("y"))
            width = _unit(raw.get("w"))
            height = _unit(raw.get("h"))
            anchor_x = x + width / 2.0
            anchor_y = y

            existing = self._labels.get(key)
            if existing is None:
                # A new label appears where it is, not eased in from the last
                # place something else happened to be.
                self._labels[key] = Label(
                    player_id=player_id,
                    track_id=track_id,
                    name=str(raw.get("name", "")),
                    region=region,
                    x=x, y=y, w=width, h=height,
                    confidence=confidence,
                    updated_ns=now_ns,
                    draw_x=anchor_x,
                    draw_y=anchor_y,
                    eased_ns=now_ns,
                    moved_ns=now_ns,
                )
                continue

            if (x, y, width, height) != (existing.x, existing.y, existing.w, existing.h):
                # Where the name is right now, and how fast it is moving,
                # under what we believed before this sample...
                self._ease(existing, now_ns)
                shown_x, shown_y = existing.draw_x, existing.draw_y
                was_vx, was_vy = self._lead_velocity(existing, now_ns)
                self._new_sample(existing, anchor_x, anchor_y, now_ns)
                existing.x, existing.y = x, y
                existing.w, existing.h = width, height
                # ...and the difference from what we believe now becomes a
                # correction that fades, so neither the position nor its speed
                # jumps. Continuity of speed is what turns a change of
                # direction into a curve rather than a kink.
                new_x, new_y = self._predict(existing, now_ns)
                new_vx, new_vy = self._lead_velocity(existing, now_ns)
                existing.off_x, existing.off_y = shown_x - new_x, shown_y - new_y
                existing.off_vx, existing.off_vy = was_vx - new_vx, was_vy - new_vy
            existing.track_id = track_id
            existing.name = str(raw.get("name", existing.name))
            existing.x, existing.y = x, y
            existing.w, existing.h = width, height
            existing.confidence = confidence
            existing.updated_ns = now_ns

    @staticmethod
    def _new_sample(label: Label, anchor_x: float, anchor_y: float, now_ns: int) -> None:
        """A position that changed: update the speed and the sample interval.

        Only here, never on a repeat. The Bluetooth server re-sends the latest
        tracks at 10 Hz while the source samples at about six, so most
        messages are the previous sample again; treating those as "stood
        still" zeroed the speed every other message.
        """
        elapsed = now_ns - label.moved_ns
        old_x = label.x + label.w / 2.0
        old_y = label.y
        step_x, step_y = anchor_x - old_x, anchor_y - old_y
        label.moved_ns = now_ns
        if elapsed <= 0 or elapsed > MAX_INTERVAL_NS * 2:
            # The first sample after a long silence says nothing about speed.
            label.vx = label.vy = 0.0
            return
        label.interval_ns = int(min(
            MAX_INTERVAL_NS,
            max(MIN_INTERVAL_NS, label.interval_ns + (elapsed - label.interval_ns) * 0.3),
        ))
        if math.hypot(step_x, step_y) > TELEPORT:
            label.vx = label.vy = 0.0
            return
        seconds = elapsed / 1_000_000_000
        seen_x, seen_y = step_x / seconds, step_y / seconds
        label.vx += (seen_x - label.vx) * VELOCITY_BLEND
        label.vy += (seen_y - label.vy) * VELOCITY_BLEND
        speed = math.hypot(label.vx, label.vy)
        if speed > MAX_SPEED:
            label.vx *= MAX_SPEED / speed
            label.vy *= MAX_SPEED / speed

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
        for key, label in list(self._labels.items()):
            if now_ns - label.updated_ns > self.stale_ns:
                del self._labels[key]
                continue
            self._ease(label, now_ns)
            alive.append(label)
        # Stable order, so two names at the same spot do not swap every frame.
        alive.sort(key=lambda item: (item.player_id, item.track_id))
        return alive

    def _predict(self, label: Label, now_ns: int) -> tuple[float, float]:
        """Where the character is now, by its last sample and its speed.

        Led for up to `lead` sample intervals. A character that has stopped
        sends no new position to say so, so past twice that the lead is
        dropped and the prediction settles on the last position reported.
        """
        x = label.x + label.w / 2.0
        y = label.y
        since = now_ns - label.moved_ns
        horizon = label.interval_ns * self.lead
        if 0 < since <= horizon * 2:
            lead = min(since, horizon) / 1_000_000_000
            x += label.vx * lead
            y += label.vy * lead
        return min(1.0, max(0.0, x)), min(1.0, max(0.0, y))

    def _lead_velocity(self, label: Label, now_ns: int) -> tuple[float, float]:
        """How fast `_predict` is moving at ``now_ns``."""
        since = now_ns - label.moved_ns
        if 0 <= since < label.interval_ns * self.lead:
            return label.vx, label.vy
        return 0.0, 0.0

    def _ease(self, label: Label, now_ns: int) -> None:
        """Draw at the prediction, plus whatever correction is still fading.

        **No lag of its own at a steady speed.** The follower this replaced
        chased the moving target and so trailed it by its own smoothing time
        -- 120 ms whatever the update rate, measured, which is most of what
        was reported as the names lagging behind the characters. Here the
        name *is* the prediction, and smoothing applies only to the
        correction each new sample makes, which a critically damped decay
        takes out without a jump in position or in speed.
        """
        elapsed = now_ns - label.eased_ns
        if elapsed < 0:
            return
        label.eased_ns = now_ns
        if self.smooth_ns <= 0:
            label.off_x = label.off_y = label.off_vx = label.off_vy = 0.0
        elif elapsed > 0:
            seconds = elapsed / 1_000_000_000
            smooth = self.smooth_ns / 1_000_000_000
            label.off_x, label.off_vx = _follow(label.off_x, label.off_vx, 0.0, smooth, seconds)
            label.off_y, label.off_vy = _follow(label.off_y, label.off_vy, 0.0, smooth, seconds)
        x, y = self._predict(label, now_ns)
        label.draw_x = min(1.0, max(0.0, x + label.off_x))
        label.draw_y = min(1.0, max(0.0, y + label.off_y))

    def __len__(self) -> int:
        return len(self._labels)


def _follow(
    position: float, speed: float, target: float, smooth: float, seconds: float
) -> tuple[float, float]:
    """One step of a critically damped follower. Returns (position, speed).

    Used to fade each correction out (towards 0), never to chase the
    character itself -- see the module note for what chasing cost. Second
    order, so it carries its own speed: a correction starts at the speed the
    name already had rather than with a jolt. Critically damped, so it
    settles without swinging past. The closed form is exact for any frame
    time, so a long frame cannot fling it.
    """
    omega = 2.0 / max(smooth, 1e-6)
    x = omega * seconds
    decay = 1.0 / (1.0 + x + 0.48 * x * x + 0.235 * x * x * x)
    change = position - target
    temp = (speed + omega * change) * seconds
    speed = (speed - omega * temp) * decay
    result = target + (change + temp) * decay
    # Never past a target it was heading for: it arrives and stops.
    if (target - position > 0.0) == (result > target):
        return target, 0.0
    return result, speed


def _unit(value: object) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if result != result:      # NaN sails through every range check
        return 0.0
    return min(1.0, max(0.0, result))

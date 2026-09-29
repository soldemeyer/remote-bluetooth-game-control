"""Which player is which, and how sure we are.

Arithmetic and bookkeeping only -- no models, no PyAV, no sockets, no numpy --
so the part of this subsystem most likely to be subtly wrong is testable with
tuples on any machine. Same call as ``server/sync_latency.py``.

THE RULE THIS EXISTS TO ENFORCE
--------------------------------
**A wrong name is worse than no name.** A label is a confident claim rendered
over somebody's game in clean text, and it looks exactly as authoritative when
it is wrong as when it is right. Everything below is shaped by preferring
silence: a match that does not clear its threshold publishes no player, a
player claimed by two entities is dropped from both, and the reference gallery
is only ever written from an assignment we were already sure of.

THE SIGNALS, IN DESCENDING ORDER OF TRUST
------------------------------------------
1. **Viewport ownership.** In a split screen the operator has already told us
   which part of the picture belongs to which player. The entity that viewport's
   camera is holding -- persistent, near where the camera keeps its player, the
   largest thing there -- is that player. It is what bootstraps every gallery.

   The appearance model is not for this. It is for the *other* direction:
   recognising that same player when they turn up inside somebody else's
   viewport, which is precisely what the feature has to draw.

2. **Appearance.** The model's own answer, against that player's gallery.

3. **Controller correlation.** What a player's thumb did against what moved on
   screen -- used *alongside* the model, where it cannot identify a player on
   its own: it settles the ties appearance refuses (two players who picked the
   same character look identical and do not steer identically), and it names
   what appearance had nothing to say about.

4. **Continuity.** A track that was player 2 a moment ago is still player 2
   unless something says otherwise. Cheap, and it is what carries an entity
   through the frames where it is turned away or half behind scenery.

WHAT IS DELIBERATELY NOT HERE
------------------------------
No character names, no game detection, no per-title tuning. This module never
learns that something is a kart or a plumber -- only that the thing in the
upper-left viewport belongs to whoever owns the upper-left viewport.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from common.screen_regions import FULL, Rect

from .types import (
    NOT_CONSULTED,
    UNIDENTIFIED,
    Evidence,
    IdentityTuning,
    InputTrace,
    Judgement,
    SignalScore,
    Track,
    TrackedPlayer,
    centre_of,
)

log = logging.getLogger(__name__)

__all__ = [
    "IdentityCalibration",
    "PlayerGallery",
    "PlayerIdentityManager",
    "cosine",
    "correlate",
]

#: How many exemplars one player's gallery holds before the oldest is dropped.
#:
#: Small on purpose. A gallery is a *session* memory of what somebody looks
#: like from a few angles, not a training set, and every extra entry is
#: another chance for one bad admission to outvote the good ones. Eight covers
#: front, back and both sides with room for a costume change.
GALLERY_CAPACITY = 8

#: An assignment must reach this before it is allowed to write to a gallery.
#:
#: Deliberately well above the publishing floor. Publishing a slightly shaky
#: label costs one wrong name for one frame; admitting a slightly shaky
#: exemplar poisons every comparison that follows it, and nothing downstream
#: can tell a contaminated gallery from a good one. Raised to the operator's
#: floor when they have set one higher.
GALLERY_MIN_CONFIDENCE = 0.80

#: How far the best candidate must beat the runner-up to be believed.
#:
#: This is the whole answer to two identical characters. When two entities
#: look equally like player 1 -- which is exactly what happens when both
#: players picked the same character -- the difference between them is noise,
#: and picking the higher number would be a coin toss rendered as a fact.
#: Inside the margin, both are published unidentified until continuity or
#: controller correlation separates them.
AMBIGUITY_MARGIN = 0.08

#: Frames a track must have been seen in before it can own a viewport.
#:
#: A camera subject is a *persistent* thing. Without this, a one-frame
#: detection drifting through the middle of somebody's viewport takes their
#: identity away from the entity that has been there all along.
VIEWPORT_MIN_HITS = 3

#: How strongly a camera subject is believed, once found. Not 1.0: the
#: operator's region assignment is authoritative, but "which of the things in
#: this viewport is the camera following" is still a heuristic.
VIEWPORT_CONFIDENCE = 0.92

#: Confidence carried by an assignment that is only continuity.
#:
#: Below ``GALLERY_MIN_CONFIDENCE`` on purpose, so continuity can hold a label
#: on screen but can never feed the gallery. Otherwise a track that drifted
#: onto the wrong entity would teach us that entity's appearance, and the
#: mistake would outlive the drift.
CONTINUITY_CONFIDENCE = 0.70

#: A correlation this strong is treated as an identification on its own.
CORRELATION_MIN = 0.55

#: The **owner window**: where a viewport's camera keeps its player, as a box
#: around the anchor, in fractions of the viewport.
#:
#: The anchor marks where the character sits; the window reaches further up
#: than down so it holds the driver's head as well as the kart -- the cap is
#: the most distinctive colour a racer has, and a window centred on the anchor
#: cut it off. Narrow, because a chase camera often has the next kart just
#: ahead and to one side, and a window that took it in would learn two
#: characters as one. Measured on Mario Kart 64: each player's kart and driver
#: sat inside 0.41-0.64 across and 0.36-0.82 down with the anchor at 0.70.
OWNER_WINDOW_WIDTH = 0.22
OWNER_WINDOW_ABOVE = 0.30
OWNER_WINDOW_BELOW = 0.15

#: How far below the appearance floor a track's appearance may fall before
#: continuity stops carrying its label. Continuity exists to hold a name
#: through frames where the character is turned away or half hidden, so it
#: must tolerate a weaker match -- but not an outright contradiction. Without
#: this, one wrong assignment was carried for as long as its track lived: the
#: minimap held player 2's name indefinitely.
CONTINUITY_SLACK = 0.10


#: How far each box is grown before two are tested for touching, as a share of
#: its own size. A driver's cap and the kart under it are boxed apart with a
#: few pixels between them, and they are still one character.
_TOUCH_PAD = 0.10


def _touching(a: Rect | None, b: Rect | None) -> bool:
    """Whether two boxes overlap or nearly touch -- pieces of one thing."""
    if a is None or b is None:
        return False
    pad_ax, pad_ay = a.width * _TOUCH_PAD, a.height * _TOUCH_PAD
    pad_bx, pad_by = b.width * _TOUCH_PAD, b.height * _TOUCH_PAD
    return (
        a.x - pad_ax < b.x + b.width + pad_bx
        and b.x - pad_bx < a.x + a.width + pad_ax
        and a.y - pad_ay < b.y + b.height + pad_by
        and b.y - pad_by < a.y + a.height + pad_ay
    )


def cosine(a: tuple[float, ...] | None, b: tuple[float, ...] | None) -> float:
    """Cosine similarity of two vectors, 0.0 when either is missing or flat.

    Zero rather than an exception for an empty or mismatched vector: a backend
    with no embedding model returns ``None`` as its ordinary answer, and this
    is asked on every pairing of every frame. Refusing to compare is the same
    outcome as comparing and finding nothing alike, and it is the safe one.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for left, right in zip(a, b):
        dot += left * right
        norm_a += left * left
        norm_b += right * right
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


def _unit(vector: tuple[float, ...]) -> tuple[float, ...]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0:
        return vector
    return tuple(value / norm for value in vector)


def _velocity_series(
    history: list[tuple[int, float, float]], count: int, hz: float, now_ns: int
) -> list[tuple[float, float]]:
    """Resample a track's motion onto ``count`` samples at ``hz``, newest last.

    The track is sampled whenever a frame was analysed -- irregularly, and far
    slower than the stick was read -- so the two series have to be put on one
    timeline before they can be compared at all. Linear interpolation between
    the two nearest observations is enough: this is a shape comparison, and
    the shapes being told apart are "went left" against "went right".
    """
    if len(history) < 2 or count <= 0 or hz <= 0.0:
        return []

    step_ns = int(1_000_000_000 / hz)
    series: list[tuple[float, float]] = []
    for index in range(count):
        # Newest sample last, so walk backwards from now.
        at = now_ns - (count - 1 - index) * step_ns
        series.append(_velocity_at(history, at, step_ns))
    return series


def _velocity_at(
    history: list[tuple[int, float, float]], at_ns: int, step_ns: int
) -> tuple[float, float]:
    """Motion per ``step_ns`` around ``at_ns``, from the nearest observations."""
    before: tuple[int, float, float] | None = None
    after: tuple[int, float, float] | None = None
    for sample in history:
        if sample[0] <= at_ns:
            before = sample
        elif after is None:
            after = sample
            break
    if before is None or after is None:
        return 0.0, 0.0
    span = after[0] - before[0]
    if span <= 0:
        return 0.0, 0.0
    scale = step_ns / span
    return (after[1] - before[1]) * scale, (after[2] - before[2]) * scale


def correlate(track: Track, trace: InputTrace, now_ns: int) -> float:
    """How much this entity moved like that player's stick. 0.0 to 1.0.

    **Screen convention throughout**: ``dx`` positive is rightwards and ``dy``
    positive is downwards, matching the normalised frame coordinates a box is
    in. The Bluetooth server flips the gamepad's Y before sending, because it
    is the end that knows it is looking at a gamepad; doing it here would mean
    this module knowing what a thumbstick is.

    Scaled to a common magnitude before comparing, because the stick is a
    deflection and the entity's motion is pixels per second -- there is no
    conversion between them that holds across games, and the *direction over
    time* is the whole signal. A negative correlation is evidence against, and
    clamps to zero rather than going below it: "moved the opposite way" and
    "did not move like that at all" are the same answer for our purposes.
    """
    samples = trace.samples
    if not samples:
        return 0.0
    series = _velocity_series(track.history, len(samples), trace.hz, now_ns)
    if not series:
        return 0.0

    flat_track = _unit(tuple(value for pair in series for value in pair))
    flat_input = _unit(tuple(value for pair in samples for value in pair))
    return max(0.0, cosine(flat_track, flat_input))


@dataclass(slots=True)
class PlayerGallery:
    """What one player has looked like this session.

    Session-lived and never persisted. Appearance here is a property of the
    character somebody picked twenty minutes ago, not of the person, and
    carrying it into the next game would be carrying a stale claim that looks
    identical to a fresh one.
    """

    capacity: int = GALLERY_CAPACITY
    exemplars: list[tuple[float, ...]] = field(default_factory=list)
    #: How many admissions have been refused for being too shaky. Reported, so
    #: "identification is not working" can be told from "identification is
    #: refusing to guess", which look the same from outside.
    refused: int = 0

    def add(self, embedding: tuple[float, ...] | None, confidence: float) -> bool:
        """Admit an exemplar, if the assignment behind it was sure enough."""
        if not embedding:
            return False
        if confidence < GALLERY_MIN_CONFIDENCE:
            self.refused += 1
            return False
        self.exemplars.append(_unit(tuple(embedding)))
        # Oldest out: a player who changed costume or vehicle should converge
        # on what they look like now rather than averaging over the session.
        while len(self.exemplars) > self.capacity:
            self.exemplars.pop(0)
        return True

    def best(self, embedding: tuple[float, ...] | None) -> float:
        """The closest exemplar's similarity. 0.0 for an empty gallery.

        The best rather than the mean: a player seen from behind matches the
        one rear exemplar and nothing else, and averaging would bury it under
        the front views.
        """
        if not embedding or not self.exemplars:
            return 0.0
        probe = _unit(tuple(embedding))
        return max(cosine(probe, known) for known in self.exemplars)

    def __len__(self) -> int:
        return len(self.exemplars)


#: How much better a challenger must score than a viewport's current owner to
#: take the viewport from it. A kart passing the anchor for a moment is not the
#: camera's subject, and without this the label hopped to it and back.
INCUMBENT_MARGIN = 0.15

#: Observations a learned value needs before it is used, and how many recent
#: ones are kept. Twenty is about three seconds of one owner at the default
#: rate; the window is a couple of minutes, so a change of game is followed.
_LEARN_MIN = 20
_LEARN_WINDOW = 240

#: A viewport owner teaches the anchor only once it has held the viewport for
#: this many times the ownership floor -- seconds of being the same entity in
#: the same place, which is what a chase camera's player is and a kart
#: overtaking is not.
_ANCHOR_LEARN_HITS = 3

#: The learned detector floor's bounds. It starts at the bottom when learning
#: is on, because a detector that scores the players below a fixed floor never
#: detects them, never identifies them, and so never learns anything -- a
#: general-purpose model on a game it was not trained for may well do that.
SCORE_FLOOR_MIN = 0.10
SCORE_FLOOR_MAX = 0.50

#: Exemplars a gallery needs before its similarity to somebody else's player
#: says anything about how alike two characters look.
_IMPOSTOR_MIN_GALLERY = 3
#: The appearance floor may be learned upward to at most this, and sits this
#: far above the most similar *other* player seen.
_APPEARANCE_CEILING = 0.95
_IMPOSTOR_MARGIN = 0.05


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * fraction)))]


def _keep_recent(values: list, limit: int = _LEARN_WINDOW) -> None:
    if len(values) > limit:
        del values[: len(values) - limit]


@dataclass(slots=True)
class IdentityCalibration:
    """What this session's play says about the game. Pure; relearned each session.

    **Learning can only make identification stricter or better placed, never
    looser about names.** The anchor moves to where the camera actually keeps
    its player; the detector floor follows how sure the detector is about the
    players (which decides what is *tracked*, not what is *named*); and the
    appearance floor can rise above the operator's when two characters look
    alike, but never fall below it.
    """

    anchors: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    scores: list[float] = field(default_factory=list)
    impostors: list[float] = field(default_factory=list)

    def reset(self) -> None:
        self.anchors.clear()
        self.scores.clear()
        self.impostors.clear()

    def observe_owner(self, region: str, fx: float, fy: float) -> None:
        points = self.anchors.setdefault(region, [])
        points.append((min(1.0, max(0.0, fx)), min(1.0, max(0.0, fy))))
        _keep_recent(points)

    def learned_anchor(self, region: str) -> tuple[float, float] | None:
        points = self.anchors.get(region, ())
        if len(points) < _LEARN_MIN:
            return None
        return (
            _median([point[0] for point in points]),
            _median([point[1] for point in points]),
        )

    def observe_score(self, score: float) -> None:
        self.scores.append(float(score))
        _keep_recent(self.scores)

    def learned_floor(self) -> float | None:
        """Four fifths of the players' weak end: low enough to keep them."""
        if len(self.scores) < _LEARN_MIN:
            return None
        floor = _percentile(self.scores, 0.10) * 0.8
        return min(SCORE_FLOOR_MAX, max(SCORE_FLOOR_MIN, floor))

    def observe_impostor(self, similarity: float) -> None:
        """How much one player's kart looked like somebody else's gallery."""
        self.impostors.append(float(similarity))
        _keep_recent(self.impostors)

    def learned_appearance_floor(self, operator: float) -> float:
        if len(self.impostors) < _LEARN_MIN:
            return operator
        above = _percentile(self.impostors, 0.99) + _IMPOSTOR_MARGIN
        return max(operator, min(_APPEARANCE_CEILING, above))

    def snapshot(self, operator_floor: float) -> dict[str, object]:
        anchors = {}
        for region in sorted(self.anchors):
            learned = self.learned_anchor(region)
            anchors[region] = (
                None if learned is None
                else [round(learned[0], 3), round(learned[1], 3)]
            )
        floor = self.learned_floor()
        return {
            "anchors": anchors,
            "anchor_samples": {r: len(p) for r, p in sorted(self.anchors.items())},
            "score_floor": None if floor is None else round(floor, 3),
            "score_samples": len(self.scores),
            "appearance_floor": round(self.learned_appearance_floor(operator_floor), 3),
            "impostor_samples": len(self.impostors),
        }


class PlayerIdentityManager:
    """Attaches players to tracks, and remembers what they look like."""

    def __init__(
        self, *, confidence: float = 0.6, tuning: IdentityTuning | None = None
    ) -> None:
        #: The publishing floor. Below it a track is still published -- so the
        #: debug view can show that something is there -- but with no player
        #: attached, and the client draws nothing.
        self.confidence = max(0.05, min(0.99, float(confidence)))
        self.tuning = tuning or IdentityTuning()
        #: This session's learning. Never persisted.
        self.calibration = IdentityCalibration()
        self._galleries: dict[int, PlayerGallery] = {}
        #: track_id -> player_id from the previous round. The continuity
        #: signal, and the only state that has to survive between frames.
        self._previous: dict[int, int] = {}
        #: region -> the track that was that viewport's subject last round.
        #: What lets an incumbent keep its viewport against a passer-by.
        self._owners: dict[str, int] = {}
        #: The least an appearance match may score, whatever the operator's
        #: floor: the backend's own, set by the worker. Its descriptor decides
        #: how high unrelated things score against each other.
        self.appearance_minimum = 0.0
        #: Set by the worker while owner windows are in play: a split picture
        #: and a backend that can describe what is at each anchor. Then the
        #: window is the **only** thing that writes a gallery, and a viewport
        #: whose window has not settled waits rather than taking the nearest
        #: box. Both were measured the hard way: a fragment taken as player 2
        #: in the first samples put a black-and-white exemplar in the gallery,
        #: the minimap matched it at 0.93 and was admitted, and from then on
        #: the minimap matched *itself* at 1.00.
        self.owner_windows_expected = False
        self.assignments = 0
        self.ambiguous = 0

        #: Why each track came out the way it did, rebuilt every round.
        #:
        #: Kept because the losing scores are the diagnosis: "appearance
        #: scored 0.41 against a 0.60 floor" and "appearance was never asked"
        #: look identical from the published row, and they point at completely
        #: different things to fix. Local to this machine -- see `Judgement`.
        self._scored: dict[int, list[SignalScore]] = {}
        self._notes: dict[int, str] = {}
        self._judgements: list[Judgement] = []

    # -- public ------------------------------------------------------------

    def gallery(self, player_id: int) -> PlayerGallery:
        gallery = self._galleries.get(player_id)
        if gallery is None:
            gallery = PlayerGallery()
            self._galleries[player_id] = gallery
        return gallery

    def forget(self, player_id: int) -> None:
        """Drop a player entirely. Called when their controller goes away.

        The same leak ``_forget_rumble_state`` and ``SyncGovernor.forget``
        exist to fix: without it a gallery for a player who left an hour ago
        goes on competing for every track.
        """
        self._galleries.pop(player_id, None)
        for track_id, assigned in list(self._previous.items()):
            if assigned == player_id:
                del self._previous[track_id]

    def reset(self) -> None:
        """Forget everything. A new game, or a stream that restarted."""
        self._galleries.clear()
        self._previous.clear()
        self._owners.clear()
        self.calibration.reset()

    def reset_learning(self) -> None:
        """Forget what this session learned about the game, keep the players."""
        self.calibration.reset()

    def appearance_floor(self) -> float:
        """What an appearance match must reach: the operator's, or higher."""
        return max(
            self.calibration.learned_appearance_floor(self.confidence),
            self.appearance_minimum,
        )

    def owner_windows(self, evidence: Evidence) -> list[tuple[str, Rect]]:
        """Each owned viewport's window: where its camera keeps its player.

        Only for regions a present player owns, and never on a shared screen,
        where there is no viewport to own. The worker describes what is in
        each and hands it back as a detection -- see `Detection.owner`.
        """
        if evidence.layout == FULL:
            return []
        windows: list[tuple[str, Rect]] = []
        seen: set[str] = set()
        for hint in evidence.hints:
            if hint.player_id == UNIDENTIFIED:
                continue
            region = self._live_region(hint, evidence.layout)
            if not region or region in seen:
                continue
            seen.add(region)
            windows.append((region, self._owner_window(region, evidence)))
        return windows

    def _owner_window(self, region: str, evidence: Evidence) -> Rect:
        cell = self._cell_rect(region, evidence.layout, evidence.active)
        ax, ay = self.anchor(region)
        left = min(max(ax - OWNER_WINDOW_WIDTH / 2.0, 0.0), 1.0 - OWNER_WINDOW_WIDTH)
        top = max(ay - OWNER_WINDOW_ABOVE, 0.0)
        bottom = min(ay + OWNER_WINDOW_BELOW, 1.0)
        if bottom <= top:
            top, bottom = 0.0, 1.0
        return Rect(
            cell.x + left * cell.width,
            cell.y + top * cell.height,
            OWNER_WINDOW_WIDTH * cell.width,
            (bottom - top) * cell.height,
        )

    def detection_floor(self, manual: float, auto: bool) -> float:
        """The score a detection needs to be tracked at all."""
        if not auto:
            return min(SCORE_FLOOR_MAX, max(0.01, float(manual)))
        learned = self.calibration.learned_floor()
        return SCORE_FLOOR_MIN if learned is None else learned

    def anchor(self, region: str) -> tuple[float, float]:
        """Where in ``region`` the camera keeps its player: learned or manual."""
        tuning = self.tuning
        if tuning.anchor_auto:
            learned = self.calibration.learned_anchor(region)
            if learned is not None:
                return learned
        return tuning.anchor_x, tuning.anchor_y

    def assign(
        self, tracks: list[Track], evidence: Evidence, now_ns: int
    ) -> list[TrackedPlayer]:
        """Attach players to tracks. One round, no state beyond the galleries.

        Order is the trust order from the module docstring: viewport ownership
        first because the operator told us, then the model's own appearance
        matching, then controller correlation -- which breaks the ties
        appearance refuses and fills in where it has nothing -- then
        continuity. Each pass only looks at tracks nobody has claimed yet, so
        a weaker signal can never overturn a stronger one.

        **A player is assigned at most once per viewport**, not once per
        frame. In a split screen a player's kart legitimately appears twice --
        in their own viewport, and in somebody else's when they are behind
        them -- and the second is exactly the label this feature exists to
        draw. On a shared screen there is one picture, and once is once.
        """
        claimed: dict[int, tuple[int, float, str]] = {}   # track_id -> (player, conf, src)
        taken: set[tuple[int, str]] = set()                # (player, viewport) placed
        self._scored = {}
        self._notes = {}
        scope = {track.track_id: self._scope(track, evidence.layout) for track in tracks}

        self._assign_viewports(tracks, evidence, claimed, taken, scope, now_ns)
        contested = self._assign_appearance(tracks, evidence, claimed, taken, scope)
        self._assign_correlation(
            tracks, evidence, claimed, taken, scope, now_ns, contested
        )
        self._assign_continuity(tracks, claimed, taken, scope)

        rows = self._publish(tracks, claimed, evidence)
        self._previous = {
            row.track_id: row.player_id for row in rows if row.identified
        }
        return rows

    def judgements(self) -> list[Judgement]:
        """The reasoning behind the last round. Never crosses the wire."""
        return list(self._judgements)

    def snapshot(self) -> dict[str, object]:
        return {
            "players": len(self._galleries),
            "exemplars": {
                str(player): len(gallery)
                for player, gallery in sorted(self._galleries.items())
            },
            "refused": sum(g.refused for g in self._galleries.values()),
            "assignments": self.assignments,
            "ambiguous": self.ambiguous,
            "learned": self.calibration.snapshot(self.confidence),
        }

    # -- the passes --------------------------------------------------------

    def _assign_viewports(
        self,
        tracks: list[Track],
        evidence: Evidence,
        claimed: dict[int, tuple[int, float, str]],
        taken: set[tuple[int, str]],
        scope: dict[int, str],
        now_ns: int,
    ) -> None:
        """The camera subject of each owned viewport is that viewport's player.

        Skipped entirely on a shared screen: with no split there is no
        viewport to own, and pretending otherwise would hand whoever happens
        to be nearest the middle of the picture somebody else's name.
        """
        if evidence.layout == FULL:
            self._owners.clear()
            return

        hits_needed = self.tuning.viewport_hits
        subjects: dict[str, int] = {}
        for hint in evidence.hints:
            if hint.player_id == UNIDENTIFIED:
                continue
            region = self._live_region(hint, evidence.layout)
            if not region or (hint.player_id, region) in taken:
                continue

            # **The owner window first.** It is where the camera keeps its
            # player, judged by what is there rather than by whichever box a
            # general-purpose model drew: on a real Mario Kart 64 frame the
            # model found neither player's own kart and a HUD numeral won the
            # viewport, then taught the gallery what a numeral looks like.
            # Only a window described this round counts -- the worker adds one
            # only while what it holds is steady.
            subject = next(
                (
                    track for track in tracks
                    if track.owner == region
                    and track.last_ns == now_ns
                    and track.track_id not in claimed
                ),
                None,
            )
            if subject is not None:
                self._record(
                    subject.track_id, "viewport", hint.player_id,
                    VIEWPORT_CONFIDENCE,
                    f"where {region}'s camera keeps its player",
                )
            elif self.owner_windows_expected:
                # Its window has not held still yet -- the first samples, or a
                # camera that does not hold its player. Nearest-box is exactly
                # what poisoned a gallery here, so wait.
                continue
            else:
                candidates = [
                    track
                    for track in tracks
                    if track.region == region
                    and not track.owner
                    and track.track_id not in claimed
                    and track.hits >= hits_needed
                ]
                subject = self._camera_subject(
                    candidates, region, evidence, player_id=hint.player_id,
                )
            if subject is None:
                continue

            subjects[region] = subject.track_id
            claimed[subject.track_id] = (
                hint.player_id, VIEWPORT_CONFIDENCE, "viewport",
            )
            taken.add((hint.player_id, region))
            self._learn_from_owner(subject, hint.player_id, region, evidence, now_ns)
            self.gallery(hint.player_id).add(subject.embedding, VIEWPORT_CONFIDENCE)
        self._owners = subjects

    def _learn_from_owner(
        self, subject: Track, player_id: int, region: str,
        evidence: Evidence, now_ns: int,
    ) -> None:
        """Feed the calibration from a viewport's owner. Never decides anything.

        Only a *settled* owner teaches: one that has held its viewport for
        several times the ownership floor, and -- for the anchor -- one whose
        identity something besides position agrees with. Otherwise a HUD icon
        that won the viewport once would teach the anchor to look at icons.
        """
        calibration = self.calibration
        own = self._galleries.get(player_id)

        # How much this player's kart looks like everybody else's gallery:
        # the impostor scores that set how high the appearance floor must sit.
        if subject.embedding:
            for other, gallery in self._galleries.items():
                if other != player_id and len(gallery) >= _IMPOSTOR_MIN_GALLERY:
                    calibration.observe_impostor(gallery.best(subject.embedding))

        # An owner window teaches neither the anchor nor the detector floor:
        # it sits *at* the anchor by construction, and its score is ours, not
        # the model's. Learning from it would be learning from itself.
        if subject.owner:
            return
        if subject.hits < self.tuning.viewport_hits * _ANCHOR_LEARN_HITS:
            return
        if subject.score > 0.0:
            calibration.observe_score(subject.score)

        agrees = False
        if own is not None and len(own) >= _IMPOSTOR_MIN_GALLERY and subject.embedding:
            agrees = own.best(subject.embedding) >= self.appearance_floor()
        if not agrees:
            trace = evidence.trace(player_id)
            if trace is not None:
                agrees = correlate(subject, trace, now_ns) >= self.tuning.correlation_floor
        if not agrees:
            return

        cell = self._cell_rect(region, evidence.layout, evidence.active)
        cx, cy = centre_of(subject.box)
        calibration.observe_owner(
            region,
            (cx - cell.x) / max(cell.width, 1e-6),
            (cy - cell.y) / max(cell.height, 1e-6),
        )

    def _assign_appearance(
        self,
        tracks: list[Track],
        evidence: Evidence,
        claimed: dict[int, tuple[int, float, str]],
        taken: set[tuple[int, str]],
        scope: dict[int, str],
    ) -> dict[int, set[int]]:
        """Match what is left against each player's gallery.

        Returns the tracks appearance refused as a tie, and between which
        players -- so controller correlation can settle them, which is the
        one thing it can do that appearance cannot: two players who picked
        the same character look identical and do not steer identically.
        """
        floor = self.appearance_floor()
        scores: dict[tuple[int, int], float] = {}
        for track in tracks:
            if track.track_id in claimed or not track.embedding:
                continue
            for hint in evidence.hints:
                if hint.player_id == UNIDENTIFIED:
                    continue
                if (hint.player_id, scope[track.track_id]) in taken:
                    continue
                gallery = self._galleries.get(hint.player_id)
                if gallery is None or not len(gallery):
                    continue
                score = gallery.best(track.embedding)
                self._record(
                    track.track_id, "appearance", hint.player_id, score,
                    "" if score >= floor
                    else f"below the {floor:.2f} appearance floor",
                )
                if score >= floor:
                    scores[(track.track_id, hint.player_id)] = score

        contested: dict[int, set[int]] = {}
        boxes = {track.track_id: track.box for track in tracks}
        for track_id, player_id, score in self._mutual_best(
            scores, "appearance", scope, contested,
            together=lambda a, b: _touching(boxes.get(a), boxes.get(b)),
        ):
            if track_id in claimed or (player_id, scope[track_id]) in taken:
                continue
            claimed[track_id] = (player_id, score, "appearance")
            taken.add((player_id, scope[track_id]))
            track = self._track(tracks, track_id)
            if track is not None and not self.owner_windows_expected:
                self.gallery(player_id).add(track.embedding, score)
        return contested

    def _assign_correlation(
        self,
        tracks: list[Track],
        evidence: Evidence,
        claimed: dict[int, tuple[int, float, str]],
        taken: set[tuple[int, str]],
        scope: dict[int, str],
        now_ns: int,
        contested: dict[int, set[int]],
    ) -> None:
        """Match what moved on screen against what each thumb was doing.

        Used alongside the model, not instead of it: for a track appearance
        refused as a tie it only chooses *between the players appearance
        could not separate*; for a track appearance had nothing to say about,
        it may name anybody.

        Mutual-best with a margin either way: a track is only given to a
        player if that player is the track's best match *and* the track is
        that player's best match *and* it beats the runner-up by
        ``AMBIGUITY_MARGIN``. Two players pushing their sticks the same way
        at the same moment -- which is most of a racing game's straight -- is
        exactly the case this must refuse.
        """
        if not evidence.traces:
            return

        floor = self.tuning.correlation_floor
        scores: dict[tuple[int, int], float] = {}
        for track in tracks:
            if track.track_id in claimed:
                continue
            allowed = contested.get(track.track_id)
            for trace in evidence.traces:
                if trace.player_id == UNIDENTIFIED:
                    continue
                if allowed is not None and trace.player_id not in allowed:
                    continue
                if (trace.player_id, scope[track.track_id]) in taken:
                    continue
                score = correlate(track, trace, now_ns)
                # Recorded whichever side of the floor it lands on. A stick
                # that was moving and scored 0.2 says the correlation was
                # tried and the motion did not match; no entry at all says it
                # was never asked, and those want opposite investigations.
                self._record(
                    track.track_id, "input", trace.player_id, score,
                    "" if score >= floor
                    else f"below the {floor:.2f} correlation floor",
                )
                if score >= floor:
                    scores[(track.track_id, trace.player_id)] = score

        for track_id, player_id, score in self._mutual_best(scores, "input", scope):
            if track_id in claimed or (player_id, scope[track_id]) in taken:
                continue
            claimed[track_id] = (player_id, score, "input")
            taken.add((player_id, scope[track_id]))
            track = self._track(tracks, track_id)
            if track is not None and not self.owner_windows_expected:
                self.gallery(player_id).add(track.embedding, score)

    def _assign_continuity(
        self,
        tracks: list[Track],
        claimed: dict[int, tuple[int, float, str]],
        taken: set[tuple[int, str]],
        scope: dict[int, str],
    ) -> None:
        """Keep last round's answer where nothing has contradicted it.

        **Appearance can contradict it.** Continuity is what holds a name
        through frames where the character is turned away, so it tolerates a
        match below the floor -- but not one `CONTINUITY_SLACK` below it. A
        label that was wrong once was otherwise carried for as long as its
        track lived.
        """
        floor = self.appearance_floor() - CONTINUITY_SLACK
        for track in tracks:
            if track.track_id in claimed:
                continue
            player_id = self._previous.get(track.track_id, UNIDENTIFIED)
            if player_id == UNIDENTIFIED:
                continue
            gallery = self._galleries.get(player_id)
            if (
                track.embedding
                and gallery is not None
                and len(gallery) >= _IMPOSTOR_MIN_GALLERY
            ):
                match = gallery.best(track.embedding)
                if match < floor:
                    self._record(
                        track.track_id, "continuity", player_id, match,
                        f"its appearance now contradicts that player: "
                        f"{match:.2f} against {floor:.2f}",
                    )
                    continue
            if (player_id, scope[track.track_id]) in taken:
                # Worth recording rather than skipping silently: "this track
                # was player 2 last round and player 2 has since been given to
                # somebody else" is the signature of a swap, and it is
                # invisible from the published row.
                self._record(
                    track.track_id, "continuity", player_id,
                    CONTINUITY_CONFIDENCE,
                    "that player was already claimed by a stronger signal",
                )
                continue
            self._record(
                track.track_id, "continuity", player_id, CONTINUITY_CONFIDENCE,
            )
            claimed[track.track_id] = (
                player_id, CONTINUITY_CONFIDENCE, "continuity",
            )
            taken.add((player_id, scope[track.track_id]))

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _scope(track: Track, layout: str) -> str:
        """Where a player may appear once: a viewport, or the whole picture."""
        return track.region if layout != FULL else ""

    def _mutual_best(
        self,
        scores: dict[tuple[int, int], float],
        signal: str = "",
        scope: dict[int, str] | None = None,
        contested: dict[int, set[int]] | None = None,
        together=None,
    ) -> list[tuple[int, int, float]]:
        """Pairings where each side is the other's clear best. Strongest first.

        The ambiguity rule lives here, once, rather than in each caller: a
        pairing is only returned when it beats every other claim on *either*
        side by ``AMBIGUITY_MARGIN``. Two entities that look equally like one
        player, or one entity that looks equally like two players, produce no
        pairing at all -- which publishes both unidentified, which draws
        nothing.

        Rivals for a player are counted within the same viewport only: the
        same player's kart in two viewports is the same player twice, not two
        claims on one player. ``contested`` collects each refused track and
        the players it was refused between, for a later signal to settle.

        ``together(a, b)`` says two tracks are pieces of **one** thing, and
        pieces are not rivals. A general-purpose model boxes a character's
        head and kart separately, and on a real frame four boxes of one Mario
        each matched him at about 0.9 -- and each refused the others, so the
        Mario plainly on screen went unnamed. The best piece takes the name;
        the rest are left, since a player is placed once per viewport.
        """
        if not scores:
            return []
        scope = scope or {}
        together = together or (lambda a, b: False)

        accepted: list[tuple[int, int, float]] = []
        for (track_id, player_id), score in sorted(
            scores.items(), key=lambda item: item[1], reverse=True
        ):
            where = scope.get(track_id, "")
            rival_for_track = max(
                (value for (t, p), value in scores.items()
                 if t == track_id and p != player_id),
                default=0.0,
            )
            rival_for_player = max(
                (value for (t, p), value in scores.items()
                 if p == player_id and t != track_id
                 and scope.get(t, "") == where
                 and not together(track_id, t)),
                default=0.0,
            )
            rival = max(rival_for_track, rival_for_player)
            if rival > 0.0 and score - rival < AMBIGUITY_MARGIN:
                self.ambiguous += 1
                # The refusal is the whole ambiguity rule working, and from
                # the published row it is indistinguishable from the signal
                # finding nothing. Say which it was.
                self._notes.setdefault(
                    track_id,
                    f"{signal or 'a signal'} refused: {score:.2f} against a "
                    f"rival {rival:.2f}, inside the {AMBIGUITY_MARGIN:.2f} margin",
                )
                if contested is not None:
                    tied = contested.setdefault(track_id, set())
                    tied.add(player_id)
                    tied.update(
                        p for (t, p), value in scores.items()
                        if t == track_id and score - value < AMBIGUITY_MARGIN
                    )
                    # A player tied across two tracks is contested on both.
                    for (t, p), value in scores.items():
                        if (p == player_id and t != track_id
                                and scope.get(t, "") == where
                                and score - value < AMBIGUITY_MARGIN):
                            contested.setdefault(t, set()).add(player_id)
                continue
            accepted.append((track_id, player_id, score))
        return accepted

    def _camera_subject(
        self, candidates: list[Track], region: str, evidence: Evidence,
        *, player_id: int = UNIDENTIFIED,
    ) -> Track | None:
        """The entity a viewport's camera is holding, or None if unclear.

        Scored on closeness to the viewport's **anchor** -- where the camera
        keeps its player, low in the middle for a chase camera -- and a little
        on size. Not the geometric centre: in a racing game that is the road
        ahead and everybody on it, and scoring against it handed viewports to
        the kart in front.

        Nothing inside the HUD band at the viewport's edge can win, and
        nothing further from the anchor than the radius can win: those are
        the icons and the passers-by, and "nobody" beats either. The viewport's
        subject from last round keeps it against a challenger that is not
        clearly better. Two candidates too close to call, with no incumbent
        between them, are refused.
        """
        if not candidates:
            return None

        tuning = self.tuning
        cell = self._cell_rect(region, evidence.layout, evidence.active)
        ax, ay = self.anchor(region)
        radius = max(tuning.anchor_radius, 1e-6)
        margin = tuning.edge_margin

        scored: list[tuple[float, Track]] = []
        for track in candidates:
            cx, cy = centre_of(track.box)
            fx = (cx - cell.x) / max(cell.width, 1e-6)
            fy = (cy - cell.y) / max(cell.height, 1e-6)
            if min(fx, fy, 1.0 - fx, 1.0 - fy) < margin:
                self._record(
                    track.track_id, "viewport", player_id, 0.0,
                    f"in the HUD band at the edge of {region}",
                )
                continue
            distance = math.hypot(fx - ax, fy - ay)
            if distance > radius:
                self._record(
                    track.track_id, "viewport", player_id, 0.0,
                    f"{distance:.2f} from where the camera keeps its player, "
                    f"beyond the {radius:.2f} radius",
                )
                continue
            area = track.box.width * track.box.height
            size = min(1.0, area / max(cell.width * cell.height * 0.25, 1e-6))
            scored.append(((1.0 - distance / radius) * 0.8 + size * 0.2, track))

        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)

        winner: tuple[float, Track] | None = None
        kept = False
        incumbent_id = self._owners.get(region)
        incumbent = next(
            (entry for entry in scored if entry[1].track_id == incumbent_id), None
        )
        if incumbent is not None and scored[0][0] - incumbent[0] < INCUMBENT_MARGIN:
            winner, kept = incumbent, True
        elif len(scored) > 1 and scored[0][0] - scored[1][0] < AMBIGUITY_MARGIN:
            winner = None
        else:
            winner = scored[0]

        # Recorded as the confidence the assignment is actually *published*
        # at, not as the closeness that picked it. Those are two different
        # numbers -- the operator's region assignment is what is believed,
        # closeness only chose which entity in the cell -- and showing the
        # second where the first decided the outcome is precisely the
        # confidently-wrong readout this view exists to replace. The closeness
        # is kept in the note rather than dropped.
        for value, track in scored:
            if winner is not None and track is winner[1]:
                self._record(
                    track.track_id, "viewport", player_id, VIEWPORT_CONFIDENCE,
                    f"camera subject of {len(scored)} in {region}, "
                    f"closeness {value:.2f}"
                    + (" (kept the viewport)" if kept else ""),
                )
            elif winner is None:
                self._record(
                    track.track_id, "viewport", player_id, value,
                    "two candidates too close to call in this viewport",
                )
            else:
                self._record(
                    track.track_id, "viewport", player_id, value,
                    f"not the camera subject: {value:.2f} against {winner[0]:.2f}",
                )
        if winner is None:
            self.ambiguous += 1
            self._notes.setdefault(
                scored[0][1].track_id,
                f"viewport refused: {scored[0][0]:.2f} against "
                f"{scored[1][0]:.2f} in the same cell",
            )
            return None
        return winner[1]

    @staticmethod
    def _cell_rect(
        region: str, layout: str,
        active: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    ) -> Rect:
        """A viewport, as a division of the picture inside the letterbox."""
        from common.screen_regions import _CELL, _GRID

        columns, rows = _GRID.get(layout, (1, 1))
        column, row = _CELL.get(region, (0, 0))
        ax, ay, aw, ah = active
        if aw <= 0.0 or ah <= 0.0:
            ax, ay, aw, ah = 0.0, 0.0, 1.0, 1.0
        return Rect(
            ax + aw * column / columns,
            ay + ah * row / rows,
            aw / columns,
            ah / rows,
        )

    @staticmethod
    def _live_region(hint, layout: str) -> str:
        from common.screen_regions import regions_for_layout

        allowed = regions_for_layout(layout)
        for name in hint.regions:
            if name in allowed:
                return name
        return ""

    def _record(
        self, track_id: int, signal: str, player_id: int, score: float,
        note: str = "",
    ) -> None:
        """Note what one signal made of one track. Never decides anything.

        Called from every pass, for every score it computed, including the
        ones that fell short of their own floor -- those are the interesting
        half. Rounded here rather than at the display, so the number an
        operator reads is the number that was compared.
        """
        self._scored.setdefault(track_id, []).append(
            SignalScore(
                signal=signal,
                player_id=int(player_id),
                score=round(float(score), 3),
                note=note,
            )
        )

    @staticmethod
    def _track(tracks: list[Track], track_id: int) -> Track | None:
        for track in tracks:
            if track.track_id == track_id:
                return track
        return None

    def _publish(
        self,
        tracks: list[Track],
        claimed: dict[int, tuple[int, float, str]],
        evidence: Evidence,
    ) -> list[TrackedPlayer]:
        rows: list[TrackedPlayer] = []
        judgements: list[Judgement] = []
        for track in tracks:
            player_id, confidence, source = claimed.get(
                track.track_id, (UNIDENTIFIED, 0.0, "none")
            )
            demoted = 0.0
            if player_id != UNIDENTIFIED and confidence < self.confidence:
                # Reached the floor from a weaker pass. Publish the track so
                # the debug view shows something is there, but attach nobody.
                demoted = confidence
                player_id, confidence, source = UNIDENTIFIED, confidence, "none"
            if player_id != UNIDENTIFIED:
                self.assignments += 1
            rows.append(
                TrackedPlayer(
                    track_id=track.track_id,
                    box=track.box,
                    player_id=player_id,
                    confidence=round(confidence, 3),
                    region=track.region,
                    source=source,
                )
            )
            judgements.append(
                self._judge(
                    track, player_id, confidence, source, demoted, evidence,
                )
            )
        self._judgements = judgements
        return rows

    def _judge(
        self,
        track: Track,
        player_id: int,
        confidence: float,
        source: str,
        demoted: float,
        evidence: Evidence,
    ) -> Judgement:
        """Assemble one track's reasoning, strongest signal first."""
        recorded = sorted(
            self._scored.get(track.track_id, ()),
            key=lambda entry: entry.score,
            reverse=True,
        )
        scores = tuple(
            SignalScore(
                signal=entry.signal,
                player_id=entry.player_id,
                score=entry.score,
                used=(
                    player_id != UNIDENTIFIED
                    and entry.signal == source
                    and entry.player_id == player_id
                ),
                note=entry.note,
            )
            for entry in recorded
        )

        # Which signals never got a look, and why. Without this an operator
        # reads a missing row as "appearance found nothing" and goes looking
        # at the gallery, when the truth is that a stronger signal had already
        # taken the track and appearance was never asked.
        looked = {entry.signal for entry in recorded}
        if player_id != UNIDENTIFIED:
            scores = scores + tuple(
                SignalScore(signal=name, note=NOT_CONSULTED)
                for name in ("viewport", "input", "continuity", "appearance")
                if name not in looked
            )

        return Judgement(
            track_id=track.track_id,
            player_id=player_id,
            confidence=round(confidence, 3),
            source=source,
            region=track.region,
            scores=scores,
            note=self._note(player_id, demoted, track, evidence),
        )

    def _note(
        self, player_id: int, demoted: float, track: Track, evidence: Evidence,
    ) -> str:
        """One line answering "why is this not a name".

        Ordered by how specific the answer is, because the vaguest one is
        always true and would otherwise mask the others.
        """
        if player_id != UNIDENTIFIED:
            return ""
        if demoted:
            return (
                f"matched at {demoted:.2f}, below the {self.confidence:.2f} "
                f"floor to publish a name"
            )
        refusal = self._notes.get(track.track_id, "")
        if refusal:
            return refusal
        if not evidence.hints:
            # The commonest reason nothing identifies while everything looks
            # healthy, and the one furthest from this module: the map arrives
            # from the Bluetooth server, so an operator staring at the video
            # server has no way to see it is missing.
            return (
                "no player map: the Bluetooth server has not said who is "
                "playing, so there is nobody to match against"
            )
        if not self._scored.get(track.track_id):
            return "no signal produced a candidate for this track"
        return "no signal cleared its threshold"

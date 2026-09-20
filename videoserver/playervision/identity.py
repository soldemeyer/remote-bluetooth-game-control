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
   camera is holding -- persistent, near the middle of its own cell, the largest
   thing there -- is that player. This needs no model at all, and it is what
   bootstraps every gallery.

   The appearance model is not for this. It is for the *other* direction:
   recognising that same player when they turn up inside somebody else's
   viewport, which is precisely what the feature has to draw.

2. **Controller correlation.** What a player's thumb did against what moved on
   screen. The only signal that survives a shared screen, where there is no
   viewport to attribute anything to, and the only one that can separate two
   players who picked the same character.

3. **Continuity.** A track that was player 2 a moment ago is still player 2
   unless something says otherwise. Cheap, and it is what carries an entity
   through the frames where it is turned away or half behind scenery.

4. **Appearance.** Matched against that player's gallery.

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
    UNIDENTIFIED,
    Evidence,
    InputTrace,
    Track,
    TrackedPlayer,
    centre_of,
)

log = logging.getLogger(__name__)

__all__ = ["PlayerGallery", "PlayerIdentityManager", "cosine", "correlate"]

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


class PlayerIdentityManager:
    """Attaches players to tracks, and remembers what they look like."""

    def __init__(self, *, confidence: float = 0.6) -> None:
        #: The publishing floor. Below it a track is still published -- so the
        #: debug view can show that something is there -- but with no player
        #: attached, and the client draws nothing.
        self.confidence = max(0.05, min(0.99, float(confidence)))
        self._galleries: dict[int, PlayerGallery] = {}
        #: track_id -> player_id from the previous round. The continuity
        #: signal, and the only state that has to survive between frames.
        self._previous: dict[int, int] = {}
        self.assignments = 0
        self.ambiguous = 0

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

    def assign(
        self, tracks: list[Track], evidence: Evidence, now_ns: int
    ) -> list[TrackedPlayer]:
        """Attach players to tracks. One round, no state beyond the galleries.

        Order matters and is the trust order from the module docstring:
        viewport ownership first because the operator told us, then
        correlation, then continuity, then appearance. Each pass only looks at
        tracks nobody has claimed yet, so a weaker signal can never overturn a
        stronger one -- it can only fill a gap the stronger one left.
        """
        claimed: dict[int, tuple[int, float, str]] = {}   # track_id -> (player, conf, src)
        taken: set[int] = set()                            # player ids already placed

        self._assign_viewports(tracks, evidence, claimed, taken)
        self._assign_correlation(tracks, evidence, claimed, taken, now_ns)
        self._assign_continuity(tracks, claimed, taken)
        self._assign_appearance(tracks, evidence, claimed, taken)

        rows = self._publish(tracks, claimed)
        self._previous = {
            row.track_id: row.player_id for row in rows if row.identified
        }
        return rows

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
        }

    # -- the passes --------------------------------------------------------

    def _assign_viewports(
        self,
        tracks: list[Track],
        evidence: Evidence,
        claimed: dict[int, tuple[int, float, str]],
        taken: set[int],
    ) -> None:
        """The camera subject of each owned viewport is that viewport's player.

        Skipped entirely on a shared screen: with no split there is no
        viewport to own, and pretending otherwise would hand whoever happens
        to be nearest the middle of the picture somebody else's name.
        """
        if evidence.layout == FULL:
            return

        for hint in evidence.hints:
            if hint.player_id in taken or hint.player_id == UNIDENTIFIED:
                continue
            region = self._live_region(hint, evidence.layout)
            if not region:
                continue

            candidates = [
                track
                for track in tracks
                if track.region == region
                and track.track_id not in claimed
                and track.hits >= VIEWPORT_MIN_HITS
            ]
            subject = self._camera_subject(candidates, region, evidence.layout)
            if subject is None:
                continue

            claimed[subject.track_id] = (
                hint.player_id, VIEWPORT_CONFIDENCE, "viewport",
            )
            taken.add(hint.player_id)
            self.gallery(hint.player_id).add(subject.embedding, VIEWPORT_CONFIDENCE)

    def _assign_correlation(
        self,
        tracks: list[Track],
        evidence: Evidence,
        claimed: dict[int, tuple[int, float, str]],
        taken: set[int],
        now_ns: int,
    ) -> None:
        """Match what moved on screen against what each thumb was doing.

        Mutual-best with a margin: a track is only given to a player if that
        player is the track's best match *and* the track is that player's best
        match *and* it beats the runner-up by ``AMBIGUITY_MARGIN``. Two players
        pushing their sticks the same way at the same moment -- which is most
        of a racing game's straight -- is exactly the case this must refuse.
        """
        if not evidence.traces:
            return

        scores: dict[tuple[int, int], float] = {}
        for track in tracks:
            if track.track_id in claimed:
                continue
            for trace in evidence.traces:
                if trace.player_id in taken or trace.player_id == UNIDENTIFIED:
                    continue
                score = correlate(track, trace, now_ns)
                if score >= CORRELATION_MIN:
                    scores[(track.track_id, trace.player_id)] = score

        for track_id, player_id, score in self._mutual_best(scores):
            if track_id in claimed or player_id in taken:
                continue
            claimed[track_id] = (player_id, score, "input")
            taken.add(player_id)
            track = self._track(tracks, track_id)
            if track is not None:
                self.gallery(player_id).add(track.embedding, score)

    def _assign_continuity(
        self,
        tracks: list[Track],
        claimed: dict[int, tuple[int, float, str]],
        taken: set[int],
    ) -> None:
        """Keep last round's answer where nothing has contradicted it."""
        for track in tracks:
            if track.track_id in claimed:
                continue
            player_id = self._previous.get(track.track_id, UNIDENTIFIED)
            if player_id == UNIDENTIFIED or player_id in taken:
                continue
            claimed[track.track_id] = (
                player_id, CONTINUITY_CONFIDENCE, "continuity",
            )
            taken.add(player_id)

    def _assign_appearance(
        self,
        tracks: list[Track],
        evidence: Evidence,
        claimed: dict[int, tuple[int, float, str]],
        taken: set[int],
    ) -> None:
        """Match whatever is left against each player's gallery."""
        scores: dict[tuple[int, int], float] = {}
        for track in tracks:
            if track.track_id in claimed or not track.embedding:
                continue
            for hint in evidence.hints:
                if hint.player_id in taken or hint.player_id == UNIDENTIFIED:
                    continue
                gallery = self._galleries.get(hint.player_id)
                if gallery is None or not len(gallery):
                    continue
                score = gallery.best(track.embedding)
                if score >= self.confidence:
                    scores[(track.track_id, hint.player_id)] = score

        for track_id, player_id, score in self._mutual_best(scores):
            if track_id in claimed or player_id in taken:
                continue
            claimed[track_id] = (player_id, score, "appearance")
            taken.add(player_id)
            track = self._track(tracks, track_id)
            if track is not None:
                self.gallery(player_id).add(track.embedding, score)

    # -- helpers -----------------------------------------------------------

    def _mutual_best(
        self, scores: dict[tuple[int, int], float]
    ) -> list[tuple[int, int, float]]:
        """Pairings where each side is the other's clear best. Strongest first.

        The ambiguity rule lives here, once, rather than in each caller: a
        pairing is only returned when it beats every other claim on *either*
        side by ``AMBIGUITY_MARGIN``. Two entities that look equally like one
        player, or one entity that looks equally like two players, produce no
        pairing at all -- which publishes both unidentified, which draws
        nothing.
        """
        if not scores:
            return []

        accepted: list[tuple[int, int, float]] = []
        for (track_id, player_id), score in sorted(
            scores.items(), key=lambda item: item[1], reverse=True
        ):
            rival_for_track = max(
                (value for (t, p), value in scores.items()
                 if t == track_id and p != player_id),
                default=0.0,
            )
            rival_for_player = max(
                (value for (t, p), value in scores.items()
                 if p == player_id and t != track_id),
                default=0.0,
            )
            rival = max(rival_for_track, rival_for_player)
            if rival > 0.0 and score - rival < AMBIGUITY_MARGIN:
                self.ambiguous += 1
                continue
            accepted.append((track_id, player_id, score))
        return accepted

    def _camera_subject(
        self, candidates: list[Track], region: str, layout: str
    ) -> Track | None:
        """The entity a viewport's camera is holding, or None if unclear.

        Scored on closeness to the middle of that viewport and on size, the
        two things a followed subject has in nearly every game that splits the
        screen at all. Returns None when the top two are too close to call:
        a viewport with two similar things in the middle of it is one where
        this heuristic has no answer, and it must say so rather than pick.
        """
        if not candidates:
            return None

        cell = self._cell_rect(region, layout)
        scored: list[tuple[float, Track]] = []
        for track in candidates:
            cx, cy = centre_of(track.box)
            # Distance from the viewport's middle, as a fraction of its size.
            dx = abs(cx - (cell.x + cell.width / 2.0)) / max(cell.width, 1e-6)
            dy = abs(cy - (cell.y + cell.height / 2.0)) / max(cell.height, 1e-6)
            centrality = max(0.0, 1.0 - math.hypot(dx, dy))
            area = track.box.width * track.box.height
            size = min(1.0, area / max(cell.width * cell.height * 0.25, 1e-6))
            scored.append((centrality * 0.7 + size * 0.3, track))

        scored.sort(key=lambda item: item[0], reverse=True)
        if len(scored) > 1 and scored[0][0] - scored[1][0] < AMBIGUITY_MARGIN:
            self.ambiguous += 1
            return None
        return scored[0][1]

    @staticmethod
    def _cell_rect(region: str, layout: str) -> Rect:
        from common.screen_regions import _CELL, _GRID

        columns, rows = _GRID.get(layout, (1, 1))
        column, row = _CELL.get(region, (0, 0))
        return Rect(column / columns, row / rows, 1.0 / columns, 1.0 / rows)

    @staticmethod
    def _live_region(hint, layout: str) -> str:
        from common.screen_regions import regions_for_layout

        allowed = regions_for_layout(layout)
        for name in hint.regions:
            if name in allowed:
                return name
        return ""

    @staticmethod
    def _track(tracks: list[Track], track_id: int) -> Track | None:
        for track in tracks:
            if track.track_id == track_id:
                return track
        return None

    def _publish(
        self, tracks: list[Track], claimed: dict[int, tuple[int, float, str]]
    ) -> list[TrackedPlayer]:
        rows: list[TrackedPlayer] = []
        for track in tracks:
            player_id, confidence, source = claimed.get(
                track.track_id, (UNIDENTIFIED, 0.0, "none")
            )
            if player_id != UNIDENTIFIED and confidence < self.confidence:
                # Reached the floor from a weaker pass. Publish the track so
                # the debug view shows something is there, but attach nobody.
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
        return rows

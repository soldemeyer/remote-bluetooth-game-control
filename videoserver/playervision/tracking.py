"""Detections in, tracks out.

Stdlib only. A backend says what it can see in one frame; this remembers what
it saw in the last few and joins them up, so identity has something with a
history to attach a player to.

Deliberately simple -- greedy nearest-match with an overlap floor -- and that
is a decision rather than a placeholder. The expensive, clever part of this
feature is appearance matching and it lives in ``identity``; a tracker that
tried to be clever here would be a second thing guessing at the same question,
and when the two disagreed there would be no way to tell which was wrong.
What this has to get right is narrow: do not swap two entities that cross, and
do not hand a recycled id to a different entity.

**Ids are never reused.** A track that dies takes its number with it. Identity
keys continuity on the track id, so recycling one would silently transfer a
player's label to whatever entity inherited the number -- a wrong name, arrived
at through perfectly healthy bookkeeping, which is the failure mode this whole
subsystem is shaped to avoid.
"""

from __future__ import annotations

import logging

from common.screen_regions import Rect

from .types import Detection, Track, centre_of, region_of

log = logging.getLogger(__name__)

__all__ = ["EntityTracker", "iou"]

#: How much two boxes must overlap before they can be the same entity.
#:
#: Low, because the sample rate is low: at 6 Hz a moving character can have
#: left its previous box entirely between frames, and a strict floor would
#: break the track every time somebody accelerated. The distance tie-break
#: does the real work; this only rules out matches that are absurd.
MIN_IOU = 0.05

#: How far a centre may move between samples, as a fraction of the frame.
#:
#: The second half of the association rule, and the one that stops two
#: entities crossing from swapping labels: a match must be both overlapping
#: *and* plausibly close.
MAX_TRAVEL = 0.25

#: Samples a track may go unseen before it is dropped.
#:
#: Three at 6 Hz is half a second. Long enough to carry an entity behind
#: scenery or through a frame the detector missed; short enough that a
#: character who genuinely left the screen stops holding their player's
#: identity hostage.
MAX_MISSES = 3

#: How many centres a track remembers. Controller correlation reads this, and
#: needs about a second of it -- ten samples at 6-10 Hz.
HISTORY = 12


def iou(a: Rect, b: Rect) -> float:
    """Intersection over union of two normalised boxes."""
    left = max(a.x, b.x)
    top = max(a.y, b.y)
    right = min(a.x + a.width, b.x + b.width)
    bottom = min(a.y + a.height, b.y + b.height)
    if right <= left or bottom <= top:
        return 0.0
    overlap = (right - left) * (bottom - top)
    union = a.width * a.height + b.width * b.height - overlap
    if union <= 0.0:
        return 0.0
    return overlap / union


class EntityTracker:
    """Follows detections across frames. Not thread-safe; one caller."""

    def __init__(self, *, max_misses: int = MAX_MISSES) -> None:
        self._tracks: dict[int, Track] = {}
        self._misses: dict[int, int] = {}
        self._next_id = 1
        self.max_misses = max_misses
        self.created = 0
        self.dropped = 0

    @property
    def tracks(self) -> list[Track]:
        return list(self._tracks.values())

    def reset(self) -> None:
        """Forget everything. A stream restart, or a layout change.

        Ids keep counting up across a reset rather than starting again, for
        the same reason they are never recycled within a run: identity keys
        continuity on the number.
        """
        self._tracks.clear()
        self._misses.clear()

    def update(
        self, detections: list[Detection], layout: str, now_ns: int
    ) -> list[Track]:
        """Fold one frame's detections in and return the live tracks."""
        pairs = self._associate(detections)

        seen: set[int] = set()
        for track_id, detection in pairs:
            self._advance(self._tracks[track_id], detection, layout, now_ns)
            seen.add(track_id)

        matched = {detection for _, detection in pairs}
        for detection in detections:
            if detection in matched:
                continue
            track = self._create(detection, layout, now_ns)
            seen.add(track.track_id)

        self._retire(seen)
        return self.tracks

    # -- internals ---------------------------------------------------------

    def _associate(
        self, detections: list[Detection]
    ) -> list[tuple[int, Detection]]:
        """Greedy best-first pairing of existing tracks to new detections.

        Best-first rather than in order: pairing track by track lets an early,
        poor match consume a detection that a later track needed, which is
        exactly how two entities passing each other end up swapped.
        """
        scored: list[tuple[float, int, Detection]] = []
        for track_id, track in self._tracks.items():
            for detection in detections:
                overlap = iou(track.box, detection.box)
                if overlap < MIN_IOU:
                    continue
                tx, ty = centre_of(track.box)
                dx, dy = centre_of(detection.box)
                travel = ((tx - dx) ** 2 + (ty - dy) ** 2) ** 0.5
                if travel > MAX_TRAVEL:
                    continue
                scored.append((overlap - travel, track_id, detection))

        scored.sort(key=lambda item: item[0], reverse=True)
        used_tracks: set[int] = set()
        used_detections: set[int] = set()
        pairs: list[tuple[int, Detection]] = []
        for _, track_id, detection in scored:
            if track_id in used_tracks or id(detection) in used_detections:
                continue
            used_tracks.add(track_id)
            used_detections.add(id(detection))
            pairs.append((track_id, detection))
        return pairs

    def _advance(
        self, track: Track, detection: Detection, layout: str, now_ns: int
    ) -> None:
        previous = centre_of(track.box)
        elapsed = max(1, now_ns - track.last_ns)

        track.box = detection.box
        track.region = region_of(detection.box, layout)
        track.last_ns = now_ns
        track.hits += 1
        track.score = detection.score
        if detection.embedding is not None:
            track.embedding = detection.embedding

        cx, cy = centre_of(detection.box)
        scale = 1_000_000_000 / elapsed
        track.velocity = ((cx - previous[0]) * scale, (cy - previous[1]) * scale)
        track.history.append((now_ns, cx, cy))
        if len(track.history) > HISTORY:
            del track.history[: len(track.history) - HISTORY]
        self._misses[track.track_id] = 0

    def _create(self, detection: Detection, layout: str, now_ns: int) -> Track:
        track_id = self._next_id
        self._next_id += 1
        self.created += 1
        track = Track(
            track_id=track_id,
            box=detection.box,
            region=region_of(detection.box, layout),
            first_ns=now_ns,
            last_ns=now_ns,
            hits=1,
            embedding=detection.embedding,
            history=[(now_ns, *centre_of(detection.box))],
            score=detection.score,
        )
        self._tracks[track_id] = track
        self._misses[track_id] = 0
        return track

    def _retire(self, seen: set[int]) -> None:
        for track_id in list(self._tracks):
            if track_id in seen:
                continue
            misses = self._misses.get(track_id, 0) + 1
            if misses > self.max_misses:
                del self._tracks[track_id]
                self._misses.pop(track_id, None)
                self.dropped += 1
            else:
                self._misses[track_id] = misses

    def snapshot(self) -> dict[str, int]:
        return {
            "live": len(self._tracks),
            "created": self.created,
            "dropped": self.dropped,
        }

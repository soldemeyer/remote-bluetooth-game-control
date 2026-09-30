"""Following entities between frames.

The two properties worth pinning are both about *not* confusing two things:
entities that cross must not swap ids, and a dead track's id must never be
handed to something else. Identity keys continuity on the track id, so either
mistake becomes a wrong name over somebody's character -- arrived at through
bookkeeping that looks perfectly healthy.
"""

from __future__ import annotations

from common.screen_regions import FULL, QUAD_4, Rect

from videoserver.playervision.tracking import (
    MAX_MISSES,
    EntityTracker,
    iou,
)
from videoserver.playervision.types import Detection

STEP = 100_000_000   # 10 Hz


def _d(x, y, w=0.1, h=0.1, embedding=None):
    return Detection(box=Rect(x, y, w, h), embedding=embedding)


class TestIou:
    def test_identical_boxes_overlap_completely(self):
        assert iou(Rect(0, 0, 1, 1), Rect(0, 0, 1, 1)) == 1.0

    def test_disjoint_boxes_do_not_overlap(self):
        assert iou(Rect(0, 0, 0.1, 0.1), Rect(0.5, 0.5, 0.1, 0.1)) == 0.0

    def test_touching_edges_are_not_an_overlap(self):
        assert iou(Rect(0, 0, 0.1, 0.1), Rect(0.1, 0, 0.1, 0.1)) == 0.0

    def test_a_zero_area_box_does_not_divide_by_zero(self):
        assert iou(Rect(0, 0, 0, 0), Rect(0, 0, 0.1, 0.1)) == 0.0


class TestFollowing:
    def test_a_stationary_entity_keeps_its_id(self):
        tracker = EntityTracker()
        first = tracker.update([_d(0.5, 0.5)], FULL, STEP)
        second = tracker.update([_d(0.5, 0.5)], FULL, STEP * 2)
        assert first[0].track_id == second[0].track_id
        assert second[0].hits == 2

    def test_a_drifting_entity_keeps_its_id(self):
        tracker = EntityTracker()
        tracker.update([_d(0.50, 0.5)], FULL, STEP)
        tracks = tracker.update([_d(0.53, 0.5)], FULL, STEP * 2)
        assert len(tracks) == 1
        assert tracks[0].hits == 2

    def test_a_teleporting_entity_is_a_new_track(self):
        """Overlap alone is not enough; a match must also be plausibly close."""
        tracker = EntityTracker()
        tracker.update([_d(0.1, 0.1)], FULL, STEP)
        tracker.update([_d(0.9, 0.9)], FULL, STEP * 2)
        assert tracker.created == 2

    def test_velocity_is_normalised_units_per_second(self):
        """The same space a stick vector is reported in, so correlation
        compares like with like."""
        tracker = EntityTracker()
        tracker.update([_d(0.50, 0.5)], FULL, STEP)
        tracks = tracker.update([_d(0.55, 0.5)], FULL, STEP * 2)
        # 0.05 of the frame in 0.1 s.
        assert abs(tracks[0].velocity[0] - 0.5) < 1e-6
        assert abs(tracks[0].velocity[1]) < 1e-6

    def test_history_is_bounded(self):
        """Correlation needs about a second of it, and no more."""
        tracker = EntityTracker()
        for index in range(40):
            tracker.update([_d(0.5 + index * 0.001, 0.5)], FULL, STEP * (index + 1))
        assert len(tracker.tracks[0].history) <= 12

    def test_the_region_follows_the_box_across_a_seam(self):
        """Walked across the boundary, not teleported: a jump that far is a
        different entity as far as association is concerned, which is the
        next test."""
        tracker = EntityTracker()
        tracks = tracker.update([_d(0.7, 0.42)], QUAD_4, STEP)
        assert tracks[0].region == "upper_right"
        tracks = tracker.update([_d(0.7, 0.47)], QUAD_4, STEP * 2)
        assert len(tracks) == 1, "the entity was not followed across the seam"
        assert tracks[0].region == "lower_right"

    def test_an_embedding_is_carried_forward_when_a_frame_has_none(self):
        """A backend may only embed occasionally -- it is the expensive half."""
        tracker = EntityTracker()
        tracker.update([_d(0.5, 0.5, embedding=(1.0, 0.0))], FULL, STEP)
        tracks = tracker.update([_d(0.5, 0.5)], FULL, STEP * 2)
        assert tracks[0].embedding == (1.0, 0.0)


class TestRetirement:
    def test_a_missing_entity_survives_a_gap(self):
        """Long enough to carry something behind scenery, or through a frame
        the detector missed."""
        tracker = EntityTracker()
        tracker.update([_d(0.5, 0.5)], FULL, STEP)
        tracker.update([], FULL, STEP * 2)
        assert len(tracker.tracks) == 1

    def test_it_is_dropped_once_the_gap_is_too_long(self):
        tracker = EntityTracker()
        tracker.update([_d(0.5, 0.5)], FULL, STEP)
        for index in range(MAX_MISSES + 1):
            tracker.update([], FULL, STEP * (index + 2))
        assert tracker.tracks == []
        assert tracker.dropped == 1

    def test_an_id_is_never_reused(self):
        """A recycled id silently transfers a player's label to whatever
        entity inherited the number."""
        tracker = EntityTracker()
        tracker.update([_d(0.5, 0.5)], FULL, STEP)
        first = tracker.tracks[0].track_id
        for index in range(MAX_MISSES + 1):
            tracker.update([], FULL, STEP * (index + 2))
        tracker.update([_d(0.5, 0.5)], FULL, STEP * 10)
        assert tracker.tracks[0].track_id != first

    def test_reset_does_not_restart_the_numbering(self):
        tracker = EntityTracker()
        tracker.update([_d(0.5, 0.5)], FULL, STEP)
        first = tracker.tracks[0].track_id
        tracker.reset()
        tracker.update([_d(0.5, 0.5)], FULL, STEP * 2)
        assert tracker.tracks[0].track_id > first


class TestCrossing:
    def test_two_entities_passing_do_not_swap_ids(self):
        """The one thing a tracker this simple still has to get right."""
        tracker = EntityTracker()
        left_id = right_id = None
        for step in range(6):
            left = 0.30 + step * 0.02
            right = 0.60 - step * 0.02
            tracks = tracker.update(
                [_d(left, 0.5), _d(right, 0.5)], FULL, STEP * (step + 1)
            )
            by_x = sorted(tracks, key=lambda t: t.box.x)
            if left_id is None:
                left_id, right_id = by_x[0].track_id, by_x[1].track_id
            elif len(by_x) == 2 and by_x[0].box.x < by_x[1].box.x - 0.03:
                # While they are still clearly apart, the left one must still
                # be the track that started on the left.
                assert by_x[0].track_id == left_id
                assert by_x[1].track_id == right_id

    def test_best_first_pairing_does_not_starve_a_later_track(self):
        """Pairing track by track lets an early, poor match consume a
        detection a later track needed -- which is how two entities end up
        swapped."""
        tracker = EntityTracker()
        tracker.update([_d(0.30, 0.5), _d(0.40, 0.5)], FULL, STEP)
        tracks = tracker.update([_d(0.31, 0.5), _d(0.41, 0.5)], FULL, STEP * 2)
        assert len(tracks) == 2
        assert tracker.created == 2, "a detection was orphaned into a new track"


class TestEmpty:
    def test_no_detections_is_not_an_error(self):
        assert EntityTracker().update([], FULL, STEP) == []

    def test_the_snapshot_reports_what_happened(self):
        tracker = EntityTracker()
        tracker.update([_d(0.5, 0.5)], FULL, STEP)
        assert tracker.snapshot() == {"live": 1, "created": 1, "dropped": 0}

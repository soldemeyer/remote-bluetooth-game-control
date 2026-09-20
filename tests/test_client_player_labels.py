"""Holding, easing and expiring labels on the client.

No Qt here: this is the arithmetic that decides where a name is drawn and when
it stops being drawn, and it should be answerable without a window.

The tests that matter most are the ones about *stopping*. A label that keeps
being drawn after the thing it named has gone is the failure this feature
cannot have, and it is the one that looks fine in every counter.
"""

from __future__ import annotations

import pytest

from client.gui.player_labels import (
    MIN_CONFIDENCE,
    SMOOTH_NS,
    STALE_NS,
    LabelStore,
    anchor_in,
)

MS = 1_000_000


def _label(track_id=1, player_id=2, *, x=0.4, y=0.3, w=0.1, h=0.2,
           confidence=0.9, name="Bo", region="upper_right"):
    return {
        "player_id": player_id, "track_id": track_id, "name": name,
        "region": region, "x": x, "y": y, "w": w, "h": h,
        "confidence": confidence,
    }


class TestIngest:
    def test_a_label_appears_where_it_is(self):
        """Not eased in from wherever the last thing happened to be."""
        store = LabelStore()
        store.ingest("QUAD_4", [_label(x=0.4, y=0.3, w=0.1)], 1000 * MS)
        label = store.visible(1000 * MS)[0]
        assert label.anchor == (0.45, 0.3)

    def test_the_anchor_is_the_top_middle_of_the_box(self):
        """The label is drawn above the character. A centre anchor would put
        it over their head at one size and their feet at another."""
        store = LabelStore()
        store.ingest("FULL", [_label(x=0.2, y=0.5, w=0.4, h=0.3)], MS)
        assert store.visible(MS)[0].anchor == (0.4, 0.5)

    def test_a_layout_change_clears_everything(self):
        """The previous positions describe a picture that is no longer on
        screen; easing across the change would drag every name over the whole
        screen."""
        store = LabelStore()
        store.ingest("QUAD_4", [_label()], MS)
        assert len(store) == 1
        store.ingest("VERTICAL_2", [], MS)
        assert len(store) == 0

    def test_a_malformed_label_is_skipped_not_raised(self):
        store = LabelStore()
        store.ingest("FULL", [{"nope": 1}, _label(), {"track_id": "x"}], MS)
        assert len(store) == 1

    def test_nonsense_coordinates_clamp(self):
        store = LabelStore()
        store.ingest("FULL", [_label(x=5.0, y=-2.0, w=0.0)], MS)
        assert store.visible(MS)[0].anchor == (1.0, 0.0)

    def test_a_nan_coordinate_does_not_reach_the_geometry(self):
        """NaN fails every comparison, so it sails through a range check and
        lands in the window's geometry as a position that cannot be drawn."""
        store = LabelStore()
        store.ingest("FULL", [_label(x=float("nan"))], MS)
        label = store.visible(MS)[0]
        assert label.x == 0.0
        assert label.draw_x == label.draw_x, "NaN reached the drawn position"
        assert 0.0 <= label.draw_x <= 1.0


class TestConfidence:
    def test_a_low_confidence_label_is_not_drawn(self):
        """A wrong name is worse than no name, and this is the last place that
        rule is enforced."""
        store = LabelStore()
        store.ingest("FULL", [_label(confidence=MIN_CONFIDENCE - 0.01)], MS)
        assert store.visible(MS) == []

    def test_confidence_falling_removes_one_already_shown(self):
        store = LabelStore()
        store.ingest("FULL", [_label(confidence=0.9)], MS)
        store.ingest("FULL", [_label(confidence=0.1)], 2 * MS)
        assert store.visible(2 * MS) == []

    def test_it_is_dropped_rather_than_hidden(self):
        """So it cannot reappear on the next frame by a rounding accident."""
        store = LabelStore()
        store.ingest("FULL", [_label(confidence=0.9)], MS)
        store.ingest("FULL", [_label(confidence=0.1)], 2 * MS)
        assert len(store) == 0


class TestExpiry:
    def test_a_label_nobody_mentions_goes_away(self):
        store = LabelStore()
        store.ingest("FULL", [_label()], MS)
        assert store.visible(MS + STALE_NS) != []
        assert store.visible(MS + STALE_NS + 1) == []

    def test_expiry_happens_without_another_message(self):
        """The server gone, the source quiet, the network dropped. A store
        that only expired on the next message would freeze every label at
        exactly the moment they became wrong."""
        store = LabelStore()
        store.ingest("FULL", [_label()], MS)
        assert store.visible(MS + 10 * STALE_NS) == []
        assert len(store) == 0

    def test_one_lost_message_does_not_flicker_every_label(self):
        """This channel has no retransmit, and a wholesale replace on every
        message is what a flicker looks like on a lossy link."""
        store = LabelStore()
        store.ingest("FULL", [_label(1), _label(2)], MS)
        # The next message mentions only one of them -- a loss, not a
        # departure. The other rides it out.
        store.ingest("FULL", [_label(1)], 100 * MS)
        assert len(store.visible(100 * MS)) == 2

    def test_a_genuine_departure_still_expires(self):
        store = LabelStore()
        store.ingest("FULL", [_label(1), _label(2)], MS)
        for step in range(1, 12):
            store.ingest("FULL", [_label(1)], MS + step * 100 * MS)
        remaining = {label.track_id for label in store.visible(MS + 1200 * MS)}
        assert remaining == {1}

    def test_clear_removes_everything(self):
        store = LabelStore()
        store.ingest("FULL", [_label()], MS)
        store.clear()
        assert store.visible(MS) == []


class TestEasing:
    def test_it_moves_towards_the_new_position(self):
        store = LabelStore()
        store.ingest("FULL", [_label(x=0.0, w=0.0)], 0)
        store.ingest("FULL", [_label(x=1.0, w=0.0)], MS)
        first = store.visible(MS + SMOOTH_NS // 2)[0].draw_x
        second = store.visible(MS + SMOOTH_NS * 2)[0].draw_x
        assert 0.0 < first < second < 1.0

    def test_it_arrives_rather_than_overshooting(self):
        """Exponential approach cannot overshoot however long a frame took --
        a name that sailed past its character and came back would read as a
        bug in the tracking."""
        store = LabelStore()
        store.ingest("FULL", [_label(x=0.0, w=0.0)], 0)
        store.ingest("FULL", [_label(x=1.0, w=0.0)], MS)
        # Refreshed as we go, or the label expires long before it arrives --
        # STALE_NS is ten times SMOOTH_NS.
        drawn = 0.0
        for step in range(1, 40):
            at = MS + step * SMOOTH_NS
            store.ingest("FULL", [_label(x=1.0, w=0.0)], at)
            drawn = store.visible(at)[0].draw_x
            assert drawn <= 1.0
        assert abs(drawn - 1.0) < 1e-6

    def test_a_long_frame_does_not_jump_further_than_the_target(self):
        store = LabelStore()
        store.ingest("FULL", [_label(x=0.0, w=0.0)], 0)
        at = MS + 100 * SMOOTH_NS
        store.ingest("FULL", [_label(x=1.0, w=0.0)], at)
        assert store.visible(at)[0].draw_x <= 1.0

    def test_it_adds_no_update_of_lag(self):
        """Interpolating *between* the last two reports is smoother and puts
        every label a full update behind. These are already 150-250 ms behind
        by the time they arrive."""
        store = LabelStore(smooth_ns=0)
        store.ingest("FULL", [_label(x=0.0, w=0.0)], 0)
        store.ingest("FULL", [_label(x=1.0, w=0.0)], MS)
        assert store.visible(MS + 1)[0].draw_x == 1.0

    def test_the_order_is_stable(self):
        """Two names at the same spot must not swap every frame."""
        store = LabelStore()
        store.ingest("FULL", [_label(3, player_id=3), _label(1, player_id=1)], MS)
        first = [label.track_id for label in store.visible(MS)]
        second = [label.track_id for label in store.visible(2 * MS)]
        assert first == second


class TestAnchorIn:
    def _one(self, **kwargs):
        store = LabelStore()
        store.ingest("QUAD_4", [_label(**kwargs)], MS)
        return store.visible(MS)[0]

    def test_a_point_inside_a_crop_maps_to_its_fraction(self):
        label = self._one(x=0.7, y=0.1, w=0.0)
        assert anchor_in(label, (0.5, 0.0, 0.5, 0.5)) == pytest.approx((0.4, 0.2))

    def test_a_point_outside_is_none_rather_than_clamped(self):
        """A label whose anchor is in none of a client's crops is simply not
        drawn. The feature fails open to less, never to a name over somebody
        else's picture."""
        label = self._one(x=0.2, y=0.2, w=0.0)
        assert anchor_in(label, (0.5, 0.0, 0.5, 0.5)) is None

    def test_the_whole_frame_contains_everything(self):
        label = self._one(x=0.9, y=0.9, w=0.0)
        assert anchor_in(label, (0.0, 0.0, 1.0, 1.0)) == (0.9, 0.9)

    def test_an_entity_straddling_a_seam_belongs_to_one_view(self):
        """Testing overlap instead of the anchor would draw the same name in
        two of a client's views at once."""
        label = self._one(x=0.45, y=0.3, w=0.2)      # anchor at 0.55
        assert anchor_in(label, (0.0, 0.0, 0.5, 1.0)) is None
        assert anchor_in(label, (0.5, 0.0, 0.5, 1.0)) is not None

    def test_a_degenerate_crop_is_not_a_division_by_zero(self):
        label = self._one()
        assert anchor_in(label, (0.0, 0.0, 0.0, 0.0)) is None

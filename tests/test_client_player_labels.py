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
    FADE_NS,
    GHOST_NS,
    MAX_DRAW_SPEED,
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
        """Faded out rather than popped -- a name vanishing mid-screen was one
        of the jumps reported -- and gone by the end of the fade."""
        store = LabelStore()
        store.ingest("FULL", [_label()], MS)
        assert store.visible(MS + STALE_NS) != []
        fading = store.visible(MS + STALE_NS + FADE_NS // 2)
        assert fading and 0.0 < fading[0].opacity < 1.0
        assert store.visible(MS + STALE_NS + FADE_NS + 1) == []

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
        store.ingest("FULL", [_label(1, player_id=1), _label(2, player_id=2)], MS)
        # The next message mentions only one of them -- a loss, not a
        # departure. The other rides it out.
        store.ingest("FULL", [_label(1, player_id=1)], 100 * MS)
        assert len(store.visible(100 * MS)) == 2

    def test_a_genuine_departure_still_expires(self):
        store = LabelStore()
        store.ingest("FULL", [_label(1, player_id=1), _label(2, player_id=2)], MS)
        for step in range(1, 12):
            store.ingest("FULL", [_label(1, player_id=1)], MS + step * 100 * MS)
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
        # A far jump moves once a second sample agrees -- see
        # TestAnOutlierIsNotFollowed.
        store.ingest("FULL", [_label(x=1.0, w=0.0)], MS // 2)
        store.ingest("FULL", [_label(x=0.999, w=0.0)], MS)
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
        store.ingest("FULL", [_label(x=0.40, w=0.0)], 0)
        store.ingest("FULL", [_label(x=0.45, w=0.0)], MS)
        assert store.visible(MS + 1)[0].draw_x == pytest.approx(0.45, abs=1e-3)

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


class TestItMovesLikeTheCharacter:
    """Reported as the names moving jerkily and then, once that was smoothed,
    as them trailing the characters. Measured, it was first stop-and-go (19%
    of frames barely moving), then a follower that trailed by its own
    smoothing time (119 ms) whatever the update rate."""

    FRAME_NS = 16 * MS

    def _run(self, store, speed=0.2, seconds=2.0, *, sample_ns=66 * MS,
             prompt=True, repeat_ns=100 * MS):
        """A character moving steadily right; returns drawn x per frame.

        ``prompt`` delivers each sample the moment it exists, as the
        Bluetooth server now does; otherwise samples are only seen on the
        ``repeat_ns`` push, as they were. The push repeats either way.
        """
        drawn = []
        sent_at = None
        last = None
        for frame in range(int(seconds * 1e9 / self.FRAME_NS)):
            now = frame * self.FRAME_NS
            sample = (now // sample_ns) * sample_ns
            due = sent_at is None or now - sent_at >= repeat_ns
            if (prompt and sample != last) or due:
                sent_at = now if due else sent_at
                last = sample
                x = 0.2 + speed * sample / 1e9
                store.ingest("FULL", [_label(x=x, w=0.0)], now)
            drawn.append(store.visible(now)[0].draw_x)
        return drawn

    def _steps(self, drawn):
        return [b - a for a, b in zip(drawn, drawn[1:])][40:]

    def test_it_never_stalls_while_the_character_moves(self):
        steps = self._steps(self._run(LabelStore()))
        mean = sum(steps) / len(steps)
        assert min(steps) > 0.5 * mean, "stop-and-go: a frame that barely moved"

    def test_it_does_not_trail_the_character(self):
        """The follower trailed by its own smoothing time, 119 ms. Drawn at
        the prediction, a steady character is matched within a frame."""
        speed = 0.2
        drawn = self._run(LabelStore(), speed=speed)
        now = (len(drawn) - 1) * self.FRAME_NS
        true_x = 0.2 + speed * now / 1e9
        trailing_ms = (true_x - drawn[-1]) / speed * 1000
        assert abs(trailing_ms) < 20, f"{trailing_ms:.0f} ms behind"

    def test_late_samples_rarely_stall_it(self):
        """The old delivery: a 170 ms source only seen on a 100 ms push, so
        samples land 100 or 200 ms apart. The lead window covers most of it."""
        steps = self._steps(self._run(
            LabelStore(), sample_ns=170 * MS, prompt=False, seconds=3.0,
        ))
        mean = sum(steps) / len(steps)
        still = sum(1 for step in steps if step < 0.2 * mean) / len(steps)
        assert still < 0.05, f"{still:.0%} of frames barely moved"

    def test_a_stop_is_overshot_only_a_little_and_settled_on(self):
        store = LabelStore()
        speed, stop_s = 0.3, 1.0
        peak = 0.0
        for frame in range(int(2.5e9 / self.FRAME_NS)):
            now = frame * self.FRAME_NS
            sample = (now // (66 * MS)) * (66 * MS)
            x = 0.2 + speed * min(sample / 1e9, stop_s)
            store.ingest("FULL", [_label(x=x, w=0.0)], now)
            drawn = store.visible(now)[0].draw_x
            peak = max(peak, drawn - (0.2 + speed * stop_s))
        assert peak < 0.04, "flew well past where the character stopped"
        assert abs(drawn - (0.2 + speed * stop_s)) < 1e-3, "never settled"

    def test_a_repeat_is_not_a_standstill(self):
        store = LabelStore()
        store.ingest("FULL", [_label(x=0.20, w=0.0)], 0)
        store.ingest("FULL", [_label(x=0.23, w=0.0)], 150 * MS)
        speed = store.visible(150 * MS)[0].vx
        store.ingest("FULL", [_label(x=0.23, w=0.0)], 250 * MS)    # the same sample
        assert store.visible(250 * MS)[0].vx == speed
        assert speed > 0

    def test_a_different_box_for_the_character_does_not_fling_it(self):
        """A cap, then the whole kart: a step too big to be motion."""
        store = LabelStore()
        store.ingest("FULL", [_label(x=0.20, w=0.0)], 0)
        store.ingest("FULL", [_label(x=0.50, w=0.0)], 150 * MS)
        label = store.visible(150 * MS)[0]
        assert label.vx == 0.0 and label.vy == 0.0

    def test_a_character_that_stops_is_settled_on(self):
        store = LabelStore()
        for step in range(6):
            store.ingest("FULL", [_label(x=0.20 + 0.01 * step, w=0.0)], step * 150 * MS)
        last = 0.25
        at = 5 * 150 * MS
        for frame in range(1, 60):
            now = at + frame * 16 * MS
            store.ingest("FULL", [_label(x=last, w=0.0)], now)   # repeats only
            drawn = store.visible(now)[0].draw_x
        assert abs(drawn - last) < 1e-3

    def test_one_label_per_player_per_view_through_a_track_change(self):
        """The model boxed a different piece of the same character: a new
        track, the same player in the same view. It glides; it does not
        appear anew beside the old one."""
        store = LabelStore()
        store.ingest("QUAD_4", [_label(7, x=0.40, w=0.0)], 0)
        store.ingest("QUAD_4", [_label(8, x=0.43, w=0.0)], 150 * MS)
        shown = store.visible(160 * MS)
        assert len(shown) == 1
        assert 0.40 < shown[0].draw_x < 0.43, "it jumped instead of gliding"
        assert shown[0].track_id == 8

    def test_the_same_player_in_two_views_is_two_labels(self):
        store = LabelStore()
        store.ingest("QUAD_4", [_label(1, region="upper_left"),
                                _label(2, region="upper_right")], MS)
        assert len(store.visible(MS)) == 2


class TestTheBubbleAndItsPointer:
    BOUNDS = (0, 0, 400, 300)

    def test_it_sits_above_the_character_pointing_down_at_it(self):
        from client.gui.player_labels import TAIL_HEIGHT, place_bubble

        bubble = place_bubble((200, 150), (60, 24), self.BOUNDS)
        assert bubble.y + bubble.height + TAIL_HEIGHT == 150
        assert bubble.x == 170
        assert bubble.tail[-1] == (200, 150), "the tip is on the character"
        (left, base_y), (right, _), _tip = bubble.tail
        assert base_y == bubble.y + bubble.height and left < 200 < right

    def test_pushed_sideways_the_pointer_still_reaches_the_character(self):
        from client.gui.player_labels import place_bubble

        bubble = place_bubble((5, 150), (60, 24), self.BOUNDS, radius=6)
        assert bubble.x == 0, "kept inside its own view"
        (left, _), (right, _), tip = bubble.tail
        assert tip == (5, 150)
        assert left >= bubble.x + 6, "the base ran into the rounded corner"
        assert right <= bubble.x + bubble.width - 6

    def test_no_pointer_when_the_bubble_already_covers_the_character(self):
        """No room above: the bubble is pushed onto the character, and a
        pointer from it would point at nothing."""
        from client.gui.player_labels import place_bubble

        bubble = place_bubble((200, 10), (60, 24), self.BOUNDS)
        assert bubble.y == 0
        assert bubble.tail == ()

    def test_it_never_leaves_its_view(self):
        from client.gui.player_labels import place_bubble

        for anchor in ((0, 0), (399, 299), (-50, 500), (200, 150)):
            bubble = place_bubble(anchor, (60, 24), self.BOUNDS)
            assert 0 <= bubble.x and bubble.x + bubble.width <= 400
            assert 0 <= bubble.y and bubble.y + bubble.height <= 300


class TestEveryMoveIsAnimated:
    """Reported as the name still jumping around the screen in a jarring way,
    with the request that every change of position be animated. Three kinds
    of jump: a correction that played out in a tenth of a second, a name that
    popped into or out of existence, and a name that vanished and reappeared
    somewhere else."""

    FRAME = 16 * MS

    def _frames(self, store, start, count):
        return [store.visible(start + i * self.FRAME)[0] for i in range(1, count + 1)]

    def test_a_name_fades_in_rather_than_popping(self):
        store = LabelStore()
        store.ingest("FULL", [_label()], 0)
        assert store.visible(0)[0].opacity == 0.0
        assert 0.0 < store.visible(FADE_NS // 2)[0].opacity < 1.0
        assert store.visible(FADE_NS)[0].opacity == 1.0

    def test_no_jump_moves_it_faster_than_the_limit(self):
        """Half the picture in one sample -- a misidentification, or a very
        different box -- is a glide, not a whip."""
        store = LabelStore()
        store.ingest("FULL", [_label(x=0.1, w=0.0)], 0)
        store.visible(0)
        store.ingest("FULL", [_label(x=0.6, w=0.0)], self.FRAME)
        drawn = [0.1]
        for frame in range(2, 200):
            now = frame * self.FRAME
            store.ingest("FULL", [_label(x=0.6, w=0.0)], now)
            drawn.append(store.visible(now)[0].draw_x)
        steps = [abs(b - a) for a, b in zip(drawn, drawn[1:])]
        assert max(steps) <= MAX_DRAW_SPEED * self.FRAME / 1e9 + 1e-9
        assert abs(drawn[-1] - 0.6) < 1e-3, "it never arrived"

    def test_a_big_correction_is_slower_than_a_small_one(self):
        def time_to_cover(distance):
            store = LabelStore()
            store.ingest("FULL", [_label(x=0.2, w=0.0)], 0)
            store.visible(0)
            for frame in range(1, 400):
                now = frame * self.FRAME
                store.ingest("FULL", [_label(x=0.2 + distance, w=0.0)], now)
                if store.visible(now)[0].draw_x >= 0.2 + 0.9 * distance:
                    return now
            return None

        small, large = time_to_cover(0.01), time_to_cover(0.3)
        assert small is not None and large is not None
        assert large > 2 * small

    def test_a_returning_name_glides_from_where_it_was(self):
        """Identification lost the player for longer than the stale time and
        found them again a little way off."""
        store = LabelStore()
        store.ingest("QUAD_4", [_label(1, player_id=2, x=0.30, w=0.0)], 0)
        store.visible(FADE_NS)
        gone = STALE_NS + FADE_NS + 10 * MS
        assert store.visible(gone) == []
        store.ingest("QUAD_4", [_label(9, player_id=2, x=0.40, w=0.0)], gone + MS)
        back = store.visible(gone + MS)[0]
        assert back.draw_x == pytest.approx(0.30), "it appeared at the new place"
        later = store.visible(gone + 200 * MS)[0]
        assert 0.30 < later.draw_x <= 0.40

    def test_a_long_absence_is_a_fresh_start(self):
        store = LabelStore()
        store.ingest("QUAD_4", [_label(1, player_id=2, x=0.30, w=0.0)], 0)
        store.visible(FADE_NS)
        gone = STALE_NS + FADE_NS + 10 * MS
        store.visible(gone)
        later = gone + GHOST_NS + 100 * MS
        store.visible(later)
        store.ingest("QUAD_4", [_label(9, player_id=2, x=0.40, w=0.0)], later)
        assert store.visible(later)[0].draw_x == pytest.approx(0.40)

    def test_never_from_somebody_elses_name(self):
        store = LabelStore()
        store.ingest("QUAD_4", [_label(1, player_id=2, x=0.30, w=0.0)], 0)
        store.visible(FADE_NS)
        gone = STALE_NS + FADE_NS + 10 * MS
        store.visible(gone)
        store.ingest("QUAD_4", [_label(9, player_id=3, x=0.40, w=0.0)], gone + MS)
        assert store.visible(gone + MS)[0].draw_x == pytest.approx(0.40)

    def test_a_name_mentioned_again_while_fading_comes_back(self):
        store = LabelStore()
        store.ingest("FULL", [_label()], 0)
        store.visible(FADE_NS)
        half = STALE_NS + FADE_NS // 2
        dimmed = store.visible(half)[0].opacity
        store.ingest("FULL", [_label()], half)
        assert store.visible(half + FADE_NS)[0].opacity == 1.0
        assert dimmed < 1.0


class TestAnOutlierIsNotFollowed:
    """The name landing on a wrong box for a sample -- a moment of
    misidentification -- used to swing the bubble there and back. A far jump
    now waits for a second opinion."""

    FRAME = 16 * MS

    def _steady(self, store, until_ns, x=0.30):
        for now in range(0, until_ns, 66 * MS):
            store.ingest("FULL", [_label(x=x + 0.0001 * (now // (66 * MS)), w=0.0)], now)
            store.visible(now)

    def test_one_wrong_sample_does_not_move_it(self):
        store = LabelStore()
        self._steady(store, 1_000 * MS)
        store.ingest("FULL", [_label(x=0.60, w=0.0)], 1_000 * MS)      # the outlier
        store.ingest("FULL", [_label(x=0.60, w=0.0)], 1_050 * MS)      # a repeat of it
        store.ingest("FULL", [_label(x=0.302, w=0.0)], 1_066 * MS)     # back on the character
        drawn = max(store.visible(1_066 * MS + i * self.FRAME)[0].draw_x for i in range(20))
        assert drawn < 0.31, f"it swung towards the outlier: {drawn:.3f}"

    def test_a_confirmed_jump_is_followed(self):
        store = LabelStore()
        self._steady(store, 1_000 * MS)
        store.ingest("FULL", [_label(x=0.60, w=0.0)], 1_000 * MS)
        store.ingest("FULL", [_label(x=0.601, w=0.0)], 1_066 * MS)     # a second opinion
        for now in range(1_066 * MS, 1_866 * MS, 50 * MS):             # still reported
            store.ingest("FULL", [_label(x=0.601, w=0.0)], now)
            store.visible(now)
        later = store.visible(1_866 * MS)[0].draw_x
        assert later == pytest.approx(0.601, abs=0.01)

    def test_a_box_that_stays_put_is_confirmed_by_time(self):
        """A player's own-viewport box is identical every sample; repeats
        alone must eventually move the name, or it would be held for ever."""
        store = LabelStore()
        self._steady(store, 1_000 * MS)
        for now in range(1_000 * MS, 2_400 * MS, 50 * MS):
            store.ingest("FULL", [_label(x=0.60, w=0.0)], now)
            store.visible(now)
        later = store.visible(2_400 * MS)[0].draw_x
        assert later == pytest.approx(0.60, abs=0.01)

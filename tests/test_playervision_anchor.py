"""Where the camera keeps its player, and what that rules out.

Reported from Mario Kart 64: identification labelled HUD icons and other karts
as the player. A chase camera holds the player low in the middle of the view,
and the middle of the view is the road ahead -- where every other kart is. The
old scoring measured distance to the geometric centre, so it handed each
viewport to the kart in front.

Also here: a player may be assigned **once per viewport**, not once per frame.
In a split screen a player's kart appears in their own viewport *and* in
somebody else's when they are behind them, and the second is the label this
feature exists to draw -- which the old rule made impossible.
"""

from __future__ import annotations

import pytest

from common.screen_regions import FULL, HORIZONTAL_2, QUAD_4, Rect

from videoserver.playervision.identity import (
    INCUMBENT_MARGIN,
    SCORE_FLOOR_MAX,
    SCORE_FLOOR_MIN,
    IdentityCalibration,
    PlayerIdentityManager,
)
from videoserver.playervision.types import (
    UNIDENTIFIED,
    Evidence,
    IdentityTuning,
    InputTrace,
    PlayerHint,
    Track,
)

SECOND = 1_000_000_000


def track(track_id, cx, cy, *, w=0.08, h=0.08, region="upper", hits=10,
          embedding=None, score=0.8, history=None):
    """A track whose box is *centred* on (cx, cy), whole-frame normalised."""
    return Track(
        track_id=track_id,
        box=Rect(cx - w / 2, cy - h / 2, w, h),
        region=region,
        first_ns=0,
        last_ns=SECOND,
        hits=hits,
        embedding=embedding,
        history=list(history or []),
        score=score,
    )


def two_up(*, active=(0.0, 0.0, 1.0, 1.0), traces=()):
    """A horizontal split: player 1 upper, player 2 lower."""
    return Evidence(
        layout=HORIZONTAL_2,
        hints=(PlayerHint(1, ("upper",)), PlayerHint(2, ("lower",))),
        traces=tuple(traces),
        active=active,
    )


def by_track(rows):
    return {row.track_id: row for row in rows}


class TestTheAnchorNotTheCentre:
    def test_the_kart_at_the_bottom_beats_the_kart_ahead(self):
        """Upper viewport spans y 0..0.5. The player's kart sits at 70% of it
        (y 0.35); another kart is up the road at the viewport's centre."""
        manager = PlayerIdentityManager()
        rows = by_track(manager.assign(
            [track(1, 0.50, 0.35), track(2, 0.50, 0.25)], two_up(), SECOND
        ))
        assert rows[1].player_id == 1
        assert rows[2].player_id == UNIDENTIFIED

    def test_the_anchor_is_the_operators_to_move(self):
        """A centred camera -- a top-down game -- keeps its player mid-view."""
        manager = PlayerIdentityManager(tuning=IdentityTuning(anchor_y=0.5, anchor_auto=False))
        rows = by_track(manager.assign(
            [track(1, 0.50, 0.35), track(2, 0.50, 0.25)], two_up(), SECOND
        ))
        assert rows[2].player_id == 1


class TestWhatCanNeverBeTheSubject:
    def test_an_icon_in_the_hud_band_is_refused(self):
        """Near the viewport's edge: the lap counter, the item box, a map."""
        manager = PlayerIdentityManager(tuning=IdentityTuning(anchor_radius=1.0))
        rows = manager.assign([track(5, 0.03, 0.05)], two_up(), SECOND)
        assert rows[0].player_id == UNIDENTIFIED
        judgement = manager.judgements()[0]
        assert any("HUD band" in s.note for s in judgement.scores)

    def test_something_far_from_the_anchor_is_refused_rather_than_chosen(self):
        """The best of bad candidates is still a wrong name."""
        manager = PlayerIdentityManager()
        rows = manager.assign([track(6, 0.85, 0.12)], two_up(), SECOND)
        assert rows[0].player_id == UNIDENTIFIED
        judgement = manager.judgements()[0]
        assert any("beyond" in s.note for s in judgement.scores)


class TestTheIncumbentKeepsItsViewport:
    def test_a_passing_kart_does_not_take_the_label(self):
        manager = PlayerIdentityManager()
        manager.assign([track(1, 0.50, 0.38)], two_up(), SECOND)
        # Next round a second kart is a touch closer to the anchor.
        rows = by_track(manager.assign(
            [track(1, 0.50, 0.38), track(2, 0.50, 0.355)], two_up(), SECOND
        ))
        assert rows[1].player_id == 1
        assert rows[2].player_id == UNIDENTIFIED

    def test_a_clearly_better_challenger_does(self):
        manager = PlayerIdentityManager()
        manager.assign([track(1, 0.62, 0.40)], two_up(), SECOND)
        rows = by_track(manager.assign(
            [track(1, 0.62, 0.40), track(2, 0.50, 0.35, w=0.2, h=0.2)], two_up(), SECOND
        ))
        assert rows[2].player_id == 1

    def test_the_margin_is_a_named_constant(self):
        assert 0.0 < INCUMBENT_MARGIN < 0.5


class TestViewportsAreDivisionsOfThePicture:
    def test_a_pillarboxed_quadrant_ends_at_the_bars(self):
        cell = PlayerIdentityManager._cell_rect("upper_left", QUAD_4, (0.125, 0.0, 0.75, 1.0))
        assert cell.x == pytest.approx(0.125)
        assert cell.width == pytest.approx(0.375)
        assert cell.height == pytest.approx(0.5)

    def test_the_subject_is_measured_inside_the_bars(self):
        """On a pillarboxed quad split the frame's cell centre sits a sixth of
        a cell out towards the bar -- towards the HUD. Measured inside the
        picture, the kart at the true anchor wins."""
        evidence = Evidence(
            layout=QUAD_4,
            hints=(PlayerHint(1, ("upper_left",)),),
            active=(0.125, 0.0, 0.75, 1.0),
        )
        manager = PlayerIdentityManager()
        # True anchor inside the picture: x 0.125 + 0.5*0.375 = 0.3125.
        rows = by_track(manager.assign(
            [
                track(1, 0.3125, 0.35, region="upper_left"),
                track(2, 0.17, 0.35, region="upper_left"),
            ],
            evidence, SECOND,
        ))
        assert rows[1].player_id == 1


class TestOncePerViewport:
    def test_a_player_is_named_in_somebody_elses_viewport_too(self):
        """Failed before: player 1 was 'taken' by their own viewport, so their
        kart seen from player 2's camera could never be labelled."""
        manager = PlayerIdentityManager(confidence=0.5)
        manager.gallery(1).add((1.0, 0.0), 1.0)
        manager.gallery(2).add((0.0, 1.0), 1.0)
        rows = by_track(manager.assign(
            [
                track(1, 0.50, 0.35, region="upper", embedding=(1.0, 0.0)),
                track(2, 0.50, 0.85, region="lower", embedding=(0.0, 1.0)),
                # Player 1's kart, up the road in player 2's view.
                track(9, 0.40, 0.70, region="lower", embedding=(1.0, 0.0)),
            ],
            two_up(), SECOND,
        ))
        assert rows[1].player_id == 1
        assert rows[2].player_id == 2
        assert rows[9].player_id == 1
        assert rows[9].source == "appearance"

    def test_but_never_twice_in_one_viewport(self):
        manager = PlayerIdentityManager(confidence=0.5)
        manager.gallery(1).add((1.0, 0.0), 1.0)
        rows = manager.assign(
            [
                track(8, 0.30, 0.70, region="lower", embedding=(1.0, 0.0)),
                track(9, 0.70, 0.70, region="lower", embedding=(0.99, 0.14)),
            ],
            Evidence(layout=HORIZONTAL_2, hints=(PlayerHint(1, ("upper",)),)),
            SECOND,
        )
        assert len([row for row in rows if row.player_id == 1]) <= 1

    def test_and_once_on_a_shared_screen(self):
        manager = PlayerIdentityManager(confidence=0.5)
        manager.gallery(1).add((1.0, 0.0), 1.0)
        rows = manager.assign(
            [
                track(8, 0.30, 0.50, region="", embedding=(1.0, 0.0)),
                track(9, 0.70, 0.50, region="", embedding=(0.99, 0.14)),
            ],
            Evidence(layout=FULL, hints=(PlayerHint(1),)),
            SECOND,
        )
        assert len([row for row in rows if row.player_id == 1]) <= 1


def walking(dx, dy, *, count=10, hz=20.0):
    step = int(SECOND / hz)
    start = SECOND - count * step
    return [(start + i * step, 0.5 + dx * i * 0.01, 0.5 + dy * i * 0.01)
            for i in range(count + 1)]


class TestControlsBreakTheModelsTies:
    def test_correlation_only_chooses_between_the_tied_players(self):
        """Appearance says 'player 1 or player 2'. Player 3's stick matching
        the motion does not overrule that -- the model knows it is not them."""
        manager = PlayerIdentityManager(confidence=0.5)
        manager.gallery(1).add((1.0, 0.0), 1.0)
        manager.gallery(2).add((1.0, 0.0), 1.0)
        manager.gallery(3).add((0.0, 1.0), 1.0)
        rows = manager.assign(
            [track(8, 0.5, 0.5, region="", embedding=(1.0, 0.0),
                   history=walking(1.0, 0.0))],
            Evidence(
                layout=FULL,
                hints=(PlayerHint(1), PlayerHint(2), PlayerHint(3)),
                traces=(
                    InputTrace(1, 20.0, tuple((0.0, 1.0) for _ in range(10))),
                    InputTrace(2, 20.0, tuple((0.0, -1.0) for _ in range(10))),
                    InputTrace(3, 20.0, tuple((1.0, 0.0) for _ in range(10))),
                ),
            ),
            SECOND,
        )
        assert rows[0].player_id != 3

    def test_and_settles_the_tie_when_one_of_them_steered_that_way(self):
        manager = PlayerIdentityManager(confidence=0.5)
        manager.gallery(1).add((1.0, 0.0), 1.0)
        manager.gallery(2).add((1.0, 0.0), 1.0)
        rows = manager.assign(
            [track(8, 0.5, 0.5, region="", embedding=(1.0, 0.0),
                   history=walking(1.0, 0.0))],
            Evidence(
                layout=FULL,
                hints=(PlayerHint(1), PlayerHint(2)),
                traces=(
                    InputTrace(1, 20.0, tuple((1.0, 0.0) for _ in range(10))),
                    InputTrace(2, 20.0, tuple((0.0, 1.0) for _ in range(10))),
                ),
            ),
            SECOND,
        )
        assert rows[0].player_id == 1
        assert rows[0].source == "input"


class TestItLearnsWhereTheCameraKeepsThePlayer:
    """Relearned every session; nothing here is persisted."""

    def settled_owner(self, fx, fy):
        """Player 1's kart held at (fx, fy) of the upper viewport, looking
        like player 1's gallery -- which is what lets it teach the anchor."""
        return track(1, fx, fy * 0.5, hits=40, embedding=(1.0, 0.0))

    def test_the_anchor_moves_to_where_the_owner_actually_sits(self):
        manager = PlayerIdentityManager(confidence=0.5)
        for _ in range(3):
            manager.gallery(1).add((1.0, 0.0), 1.0)
        for _ in range(30):
            manager.assign([self.settled_owner(0.52, 0.82)], two_up(), SECOND)
        ax, ay = manager.anchor("upper")
        assert ax == pytest.approx(0.52, abs=0.01)
        assert ay == pytest.approx(0.82, abs=0.01)

    def test_nothing_is_learned_without_something_that_agrees(self):
        """Position alone would let an icon that won once teach the anchor to
        look at icons. No gallery and no stick: no learning."""
        manager = PlayerIdentityManager()
        for _ in range(30):
            manager.assign([track(1, 0.52, 0.41, hits=40)], two_up(), SECOND)
        assert manager.anchor("upper") == (0.50, 0.70)

    def test_a_manual_anchor_is_not_overridden(self):
        manager = PlayerIdentityManager(
            confidence=0.5, tuning=IdentityTuning(anchor_x=0.3, anchor_y=0.6, anchor_auto=False)
        )
        for _ in range(3):
            manager.gallery(1).add((1.0, 0.0), 1.0)
        for _ in range(30):
            manager.assign([self.settled_owner(0.4, 0.62)], two_up(), SECOND)
        assert manager.anchor("upper") == (0.3, 0.6)

    def test_reset_learning_forgets_it(self):
        manager = PlayerIdentityManager(confidence=0.5)
        for _ in range(3):
            manager.gallery(1).add((1.0, 0.0), 1.0)
        for _ in range(30):
            manager.assign([self.settled_owner(0.52, 0.82)], two_up(), SECOND)
        manager.reset_learning()
        assert manager.anchor("upper") == (0.50, 0.70)
        assert len(manager.gallery(1)) > 0, "learning reset took the players with it"


class TestTheFloorsItLearns:
    def test_auto_starts_at_the_bottom(self):
        """A detector that scores the players under a fixed floor never
        detects them, so never identifies them, so never learns anything."""
        assert PlayerIdentityManager().detection_floor(0.25, auto=True) == SCORE_FLOOR_MIN

    def test_manual_is_manual(self):
        assert PlayerIdentityManager().detection_floor(0.3, auto=False) == pytest.approx(0.3)

    @pytest.mark.parametrize("seen", [0.02, 0.2, 0.99])
    def test_the_learned_detector_floor_is_bounded(self, seen):
        calibration = IdentityCalibration()
        for _ in range(40):
            calibration.observe_score(seen)
        learned = calibration.learned_floor()
        assert SCORE_FLOOR_MIN <= learned <= SCORE_FLOOR_MAX

    def test_the_appearance_floor_rises_when_players_look_alike(self):
        calibration = IdentityCalibration()
        for _ in range(40):
            calibration.observe_impostor(0.8)
        assert calibration.learned_appearance_floor(0.6) == pytest.approx(0.85)

    def test_but_never_falls_below_the_operators(self):
        calibration = IdentityCalibration()
        for _ in range(40):
            calibration.observe_impostor(0.1)
        assert calibration.learned_appearance_floor(0.6) == pytest.approx(0.6)

    def test_nor_rises_past_the_ceiling(self):
        calibration = IdentityCalibration()
        for _ in range(40):
            calibration.observe_impostor(0.999)
        assert calibration.learned_appearance_floor(0.6) <= 0.95


class TestTheTuningBlock:
    def test_missing_keys_keep_their_defaults(self):
        assert IdentityTuning.from_dict({}) == IdentityTuning()

    @pytest.mark.parametrize("raw", [None, "junk", {"pid_anchor_x": "left"},
                                     {"pid_anchor_radius": float("nan")}])
    def test_rubbish_is_harmless(self, raw):
        tuning = IdentityTuning.from_dict(raw)
        assert 0.0 <= tuning.anchor_x <= 1.0
        assert 0.05 <= tuning.anchor_radius <= 1.0

    def test_values_are_clamped(self):
        tuning = IdentityTuning.from_dict(
            {"pid_anchor_y": 4.0, "pid_edge_margin": 0.9, "pid_viewport_hits": 0}
        )
        assert tuning.anchor_y == 1.0
        assert tuning.edge_margin == 0.3
        assert tuning.viewport_hits == 1

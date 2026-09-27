"""Why a track was or was not identified, recorded rather than inferred.

The published row says *what* was decided. These cover *why*, which is the
half an operator needs when the answer is "nobody" and every counter reads
healthy -- and which the identity manager previously computed and threw away.

Stdlib only, like the manager itself.
"""

from __future__ import annotations

from common.screen_regions import Rect
from videoserver.playervision.child import _judgement as encode_judgement
from videoserver.playervision.identity import (
    CONTINUITY_CONFIDENCE,
    CORRELATION_MIN,
    PlayerIdentityManager,
    VIEWPORT_CONFIDENCE,
)
from videoserver.playervision.runner import _judgements_from
from videoserver.playervision.types import (
    NOT_CONSULTED,
    Evidence,
    InputTrace,
    PlayerHint,
    Track,
)

SECOND = 1_000_000_000


def _track(track_id, x, y, region="", hits=5, embedding=None, history=()):
    return Track(
        track_id=track_id,
        box=Rect(x, y, 0.12, 0.16),
        region=region,
        first_ns=0,
        last_ns=SECOND,
        hits=hits,
        embedding=embedding,
        history=list(history),
    )


def _by_track(manager):
    return {j.track_id: j for j in manager.judgements()}


class TestTheWinnerIsNamed:
    def test_a_viewport_assignment_records_the_signal_that_won(self):
        manager = PlayerIdentityManager(confidence=0.6)
        evidence = Evidence(
            layout="QUAD_4", hints=(PlayerHint(1, ("upper_left",)),)
        )

        manager.assign([_track(4, 0.2, 0.2, "upper_left")], evidence, SECOND)

        judgement = _by_track(manager)[4]
        assert judgement.player_id == 1
        assert judgement.source == "viewport"
        won = [score for score in judgement.scores if score.used]
        assert len(won) == 1
        assert won[0].signal == "viewport"

    def test_the_winning_score_is_the_one_the_assignment_was_published_at(self):
        """Not the closeness to the anchor that merely chose which entity in
        the cell.

        Two different numbers for one decision is the confusion this view
        exists to remove: the operator's region assignment is what is
        believed, and closeness only picked the subject. It is kept in the
        note rather than dropped.
        """
        manager = PlayerIdentityManager(confidence=0.6)
        evidence = Evidence(
            layout="QUAD_4", hints=(PlayerHint(1, ("upper_left",)),)
        )

        rows = manager.assign(
            [_track(4, 0.2, 0.2, "upper_left")], evidence, SECOND
        )

        judgement = _by_track(manager)[4]
        won = [score for score in judgement.scores if score.used][0]
        assert won.score == VIEWPORT_CONFIDENCE == rows[0].confidence
        assert "closeness" in won.note


class TestTheLosersAreKept:
    def test_a_correlation_below_its_floor_is_recorded_with_the_reason(self):
        """"input scored 0.1" and "input was never asked" are different
        faults, and only one of them points at the controller."""
        manager = PlayerIdentityManager(confidence=0.6)
        # Motion that does not match the stick at all.
        track = _track(
            1, 0.4, 0.4,
            history=[(i * 10_000_000, 0.4, 0.4) for i in range(20)],
        )
        evidence = Evidence(
            layout="FULL",
            hints=(PlayerHint(1, ()),),
            traces=(InputTrace(1, 20.0, tuple((1.0, 0.0) for _ in range(20))),),
        )

        manager.assign([track], evidence, SECOND)

        scores = {s.signal: s for s in _by_track(manager)[1].scores}
        assert "input" in scores, "a correlation that was tried must be shown"
        assert scores["input"].note
        assert str(round(CORRELATION_MIN, 2)) in scores["input"].note

    def test_an_entity_that_is_not_the_camera_subject_says_so(self):
        """The commonest reason a track in an owned viewport gets no name."""
        manager = PlayerIdentityManager(confidence=0.6)
        evidence = Evidence(
            layout="QUAD_4", hints=(PlayerHint(1, ("upper_left",)),)
        )
        # One at the anchor, one inside the radius but clearly further off.
        near = _track(4, 0.2, 0.3, "upper_left")
        off = _track(6, 0.12, 0.2, "upper_left")
        corner = _track(5, 0.01, 0.01, "upper_left")

        manager.assign([near, off, corner], evidence, SECOND)

        judgements = _by_track(manager)
        assert judgements[6].player_id == 0
        assert any(
            "not the camera subject" in score.note for score in judgements[6].scores
        )
        # And the far one says *why* it was never a candidate -- too far from
        # where the camera keeps its player -- which is a more useful answer
        # than merely having lost.
        assert judgements[5].player_id == 0
        assert any("radius" in score.note for score in judgements[5].scores)


class TestWhatWasNeverAsked:
    def test_signals_a_stronger_one_pre_empted_are_listed_as_such(self):
        """Each pass only sees unclaimed tracks -- that ordering is the whole
        design -- so a blank row would read as "appearance found nothing" and
        send somebody to look at the gallery."""
        manager = PlayerIdentityManager(confidence=0.6)
        evidence = Evidence(
            layout="QUAD_4", hints=(PlayerHint(1, ("upper_left",)),)
        )

        manager.assign([_track(4, 0.2, 0.2, "upper_left")], evidence, SECOND)

        scores = {s.signal: s for s in _by_track(manager)[4].scores}
        assert scores["appearance"].note == NOT_CONSULTED
        assert scores["continuity"].note == NOT_CONSULTED


class TestTheNote:
    def test_no_player_map_is_named_as_the_cause(self):
        """The likeliest reason nothing identifies while everything looks
        healthy, and the one furthest from this module -- the map arrives from
        the Bluetooth server, so the video server's operator cannot see it is
        missing."""
        manager = PlayerIdentityManager(confidence=0.6)

        manager.assign([_track(1, 0.4, 0.4)], Evidence(layout="FULL"), SECOND)

        assert "no player map" in _by_track(manager)[1].note

    def test_an_identified_track_carries_no_note(self):
        manager = PlayerIdentityManager(confidence=0.6)
        evidence = Evidence(
            layout="QUAD_4", hints=(PlayerHint(1, ("upper_left",)),)
        )

        manager.assign([_track(4, 0.2, 0.2, "upper_left")], evidence, SECOND)

        assert _by_track(manager)[4].note == ""

    def test_a_continuity_label_records_the_signal_it_came_from(self):
        manager = PlayerIdentityManager(confidence=0.6)
        evidence = Evidence(
            layout="QUAD_4", hints=(PlayerHint(1, ("upper_left",)),)
        )
        manager.assign([_track(4, 0.2, 0.2, "upper_left")], evidence, SECOND)

        # Same track, now outside any owned viewport: only continuity is left.
        manager.assign([_track(4, 0.8, 0.8, "lower_right")], evidence, SECOND)

        judgement = _by_track(manager)[4]
        assert judgement.source == "continuity"
        won = [score for score in judgement.scores if score.used][0]
        assert won.score == CONTINUITY_CONFIDENCE


class TestItIsRebuiltEveryRound:
    def test_a_round_does_not_inherit_the_last_one(self):
        """A stale breakdown beside fresh rows would explain a decision that
        is no longer on screen."""
        manager = PlayerIdentityManager(confidence=0.6)
        evidence = Evidence(
            layout="QUAD_4", hints=(PlayerHint(1, ("upper_left",)),)
        )
        manager.assign([_track(4, 0.2, 0.2, "upper_left")], evidence, SECOND)

        manager.assign([_track(9, 0.7, 0.7, "lower_right")], evidence, SECOND)

        assert [j.track_id for j in manager.judgements()] == [9]


class TestAcrossTheProcessBoundary:
    def test_the_reasoning_survives_the_pipe(self):
        """An isolated backend is the normal case for a model, so the debug
        view must not quietly empty itself the moment somebody selects one."""
        manager = PlayerIdentityManager(confidence=0.6)
        evidence = Evidence(
            layout="QUAD_4",
            hints=(PlayerHint(1, ("upper_left",)), PlayerHint(2, ("upper_right",))),
        )
        manager.assign(
            [_track(4, 0.2, 0.2, "upper_left"), _track(9, 0.6, 0.6)],
            evidence,
            SECOND,
        )
        original = manager.judgements()

        rebuilt = _judgements_from([encode_judgement(j) for j in original])

        assert len(rebuilt) == len(original)
        for before, after in zip(original, rebuilt):
            assert after.track_id == before.track_id
            assert after.player_id == before.player_id
            assert after.source == before.source
            assert after.note == before.note
            assert len(after.scores) == len(before.scores)
            for old, new in zip(before.scores, after.scores):
                assert new.signal == old.signal
                assert new.used == old.used
                assert new.note == old.note
                assert abs(new.score - old.score) < 1e-3

    def test_a_malformed_entry_costs_only_itself(self):
        """A debug view that raised on one bad record would take out the
        display of every good one beside it."""
        good = encode_judgement(_one_judgement())

        rebuilt = _judgements_from([good, ["nonsense"], None, good])

        assert len(rebuilt) == 2


def _one_judgement():
    manager = PlayerIdentityManager(confidence=0.6)
    manager.assign(
        [_track(4, 0.2, 0.2, "upper_left")],
        Evidence(layout="QUAD_4", hints=(PlayerHint(1, ("upper_left",)),)),
        SECOND,
    )
    return manager.judgements()[0]

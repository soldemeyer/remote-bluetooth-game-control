"""Who gets a label, and -- more importantly -- who does not.

The rule under test throughout is that a wrong name is worse than no name.
Most of these assert a *refusal*: two identical characters, an entity claimed
by two players, a gallery admission from a shaky match. Those are the cases
that make a label trustworthy, and they are the ones an optimisation would
quietly remove.
"""

from __future__ import annotations

from common.screen_regions import FULL, QUAD_4, VERTICAL_2, Rect

from videoserver.playervision.identity import (
    AMBIGUITY_MARGIN,
    CORRELATION_MIN,
    GALLERY_MIN_CONFIDENCE,
    PlayerGallery,
    PlayerIdentityManager,
    correlate,
    cosine,
)
from videoserver.playervision.types import (
    UNIDENTIFIED,
    Evidence,
    InputTrace,
    PlayerHint,
    Track,
)

SECOND = 1_000_000_000


def _track(track_id, x, y, *, w=0.1, h=0.1, region="", hits=10, embedding=None,
           history=None, now=SECOND):
    return Track(
        track_id=track_id,
        box=Rect(x, y, w, h),
        region=region,
        first_ns=now - SECOND,
        last_ns=now,
        hits=hits,
        embedding=embedding,
        history=list(history or []),
    )


def _quad_evidence(*players):
    """QUAD_4 with each given player owning one quadrant, in reading order."""
    cells = ("upper_left", "upper_right", "lower_left", "lower_right")
    return Evidence(
        layout=QUAD_4,
        hints=tuple(
            PlayerHint(player_id=p, regions=(cells[i],))
            for i, p in enumerate(players)
        ),
    )


class TestCosine:
    def test_identical_vectors_match(self):
        assert cosine((1.0, 0.0), (2.0, 0.0)) == 1.0

    def test_opposite_vectors_are_negative(self):
        assert cosine((1.0, 0.0), (-1.0, 0.0)) == -1.0

    def test_a_missing_vector_is_not_an_error(self):
        """A backend with no embedding model returns None as its ordinary
        answer, and this is asked on every pairing of every frame."""
        assert cosine(None, (1.0, 0.0)) == 0.0
        assert cosine((1.0, 0.0), None) == 0.0
        assert cosine((), ()) == 0.0

    def test_mismatched_lengths_do_not_raise(self):
        assert cosine((1.0, 0.0), (1.0, 0.0, 0.0)) == 0.0

    def test_a_zero_vector_does_not_divide_by_zero(self):
        assert cosine((0.0, 0.0), (1.0, 1.0)) == 0.0


class TestGallery:
    def test_a_shaky_assignment_is_refused(self):
        """The whole point. Publishing a shaky label costs one wrong name for
        one frame; admitting a shaky exemplar poisons everything after it."""
        gallery = PlayerGallery()
        assert gallery.add((1.0, 0.0), GALLERY_MIN_CONFIDENCE - 0.01) is False
        assert len(gallery) == 0
        assert gallery.refused == 1

    def test_a_confident_assignment_is_admitted(self):
        gallery = PlayerGallery()
        assert gallery.add((1.0, 0.0), GALLERY_MIN_CONFIDENCE) is True
        assert len(gallery) == 1

    def test_it_is_bounded_and_drops_the_oldest(self):
        """A player who changed vehicle should converge on what they look like
        now, rather than averaging over the whole session."""
        gallery = PlayerGallery(capacity=3)
        for index in range(6):
            gallery.add((float(index), 1.0), 1.0)
        assert len(gallery) == 3
        # The first three are gone: the best match for the earliest exemplar
        # is now worse than for the latest.
        assert gallery.best((5.0, 1.0)) > gallery.best((0.0, 1.0))

    def test_best_not_mean(self):
        """A player seen from behind matches the one rear exemplar and nothing
        else. Averaging would bury it under the front views."""
        gallery = PlayerGallery()
        gallery.add((1.0, 0.0), 1.0)
        gallery.add((1.0, 0.0), 1.0)
        gallery.add((0.0, 1.0), 1.0)
        assert gallery.best((0.0, 1.0)) == 1.0

    def test_an_empty_gallery_matches_nothing(self):
        assert PlayerGallery().best((1.0, 0.0)) == 0.0


class TestViewportOwnership:
    def test_the_camera_subject_gets_the_viewport_owner(self):
        manager = PlayerIdentityManager()
        # Centred in the upper-left quadrant, which player 1 owns.
        tracks = [_track(1, 0.22, 0.22, region="upper_left")]
        rows = manager.assign(tracks, _quad_evidence(1), SECOND)
        assert rows[0].player_id == 1
        assert rows[0].source == "viewport"

    def test_it_bootstraps_the_gallery_with_no_model_at_all(self):
        """This is what makes the split-screen case work before any appearance
        matching has anything to match against."""
        manager = PlayerIdentityManager()
        tracks = [_track(1, 0.22, 0.22, region="upper_left", embedding=(1.0, 0.0))]
        manager.assign(tracks, _quad_evidence(1), SECOND)
        assert len(manager.gallery(1)) == 1

    def test_a_fleeting_detection_cannot_take_a_viewport(self):
        """A camera subject is a persistent thing. Without the hit floor, one
        frame of noise drifting through the middle takes the identity away
        from the entity that has been there all along."""
        manager = PlayerIdentityManager()
        tracks = [_track(1, 0.22, 0.22, region="upper_left", hits=1)]
        rows = manager.assign(tracks, _quad_evidence(1), SECOND)
        assert rows[0].player_id == UNIDENTIFIED

    def test_two_similar_things_in_one_viewport_are_refused(self):
        """A viewport with two equally central things is one where this
        heuristic has no answer, and it must say so rather than pick."""
        manager = PlayerIdentityManager()
        tracks = [
            _track(1, 0.20, 0.22, region="upper_left"),
            _track(2, 0.24, 0.22, region="upper_left"),
        ]
        rows = manager.assign(tracks, _quad_evidence(1), SECOND)
        assert {row.player_id for row in rows} == {UNIDENTIFIED}

    def test_a_shared_screen_has_no_viewports_to_own(self):
        """With no split, whoever is nearest the middle would otherwise be
        handed somebody else's name."""
        manager = PlayerIdentityManager()
        tracks = [_track(1, 0.45, 0.45)]
        evidence = Evidence(
            layout=FULL, hints=(PlayerHint(1, ("upper_left",)),)
        )
        rows = manager.assign(tracks, evidence, SECOND)
        assert rows[0].player_id == UNIDENTIFIED

    def test_a_region_from_another_layout_is_ignored(self):
        """A controller carries every assignment it might need. Only the one
        belonging to the picture on screen means anything right now."""
        manager = PlayerIdentityManager()
        tracks = [_track(1, 0.22, 0.22, region="upper_left")]
        evidence = Evidence(
            layout=QUAD_4, hints=(PlayerHint(1, ("left",)),)
        )
        rows = manager.assign(tracks, evidence, SECOND)
        assert rows[0].player_id == UNIDENTIFIED

    def test_each_player_owns_their_own_quadrant(self):
        manager = PlayerIdentityManager()
        tracks = [
            _track(1, 0.22, 0.22, region="upper_left"),
            _track(2, 0.72, 0.22, region="upper_right"),
            _track(3, 0.22, 0.72, region="lower_left"),
            _track(4, 0.72, 0.72, region="lower_right"),
        ]
        rows = manager.assign(tracks, _quad_evidence(1, 2, 3, 4), SECOND)
        assert {row.track_id: row.player_id for row in rows} == {
            1: 1, 2: 2, 3: 3, 4: 4,
        }


class TestAppearance:
    def _bootstrapped(self):
        """Player 1 established in their own viewport, with an appearance."""
        manager = PlayerIdentityManager(confidence=0.5)
        manager.gallery(1).add((1.0, 0.0), 1.0)
        manager.gallery(2).add((0.0, 1.0), 1.0)
        return manager

    def test_it_finds_a_player_inside_somebody_elses_viewport(self):
        """The thing the embedding model is actually for: player 1's avatar
        appearing in player 2's view, which is what the feature has to draw."""
        manager = self._bootstrapped()
        tracks = [
            # Player 2's own subject, centred in their quadrant.
            _track(2, 0.72, 0.22, region="upper_right", embedding=(0.0, 1.0)),
            # Player 1's avatar, off to one side of player 2's view.
            _track(9, 0.92, 0.35, region="upper_right", embedding=(1.0, 0.0)),
        ]
        # Both players are in the roster; only player 2's viewport is on
        # screen here. A player absent from the hints is not in the game, so
        # the roster has to name everybody even when one of them is off in
        # another quadrant.
        evidence = Evidence(
            layout=QUAD_4,
            hints=(
                PlayerHint(1, ("upper_left",)),
                PlayerHint(2, ("upper_right",)),
            ),
        )
        rows = manager.assign(tracks, evidence, SECOND)
        by_track = {row.track_id: row for row in rows}
        assert by_track[2].player_id == 2
        assert by_track[9].player_id == 1
        assert by_track[9].source == "appearance"

    def test_two_identical_characters_are_both_refused(self):
        """Appearance alone cannot separate them, and picking the higher
        number would be a coin toss rendered as a fact."""
        manager = self._bootstrapped()
        same = (1.0, 0.0)
        tracks = [
            _track(8, 0.30, 0.60, embedding=same),
            _track(9, 0.60, 0.60, embedding=same),
        ]
        evidence = Evidence(layout=FULL, hints=(PlayerHint(1), PlayerHint(2)))
        rows = manager.assign(tracks, evidence, SECOND)
        assert {row.player_id for row in rows} == {UNIDENTIFIED}
        assert manager.ambiguous > 0

    def test_a_clear_winner_beats_the_margin(self):
        manager = self._bootstrapped()
        tracks = [
            _track(8, 0.30, 0.60, embedding=(1.0, 0.0)),
            _track(9, 0.60, 0.60, embedding=(0.0, 1.0)),
        ]
        evidence = Evidence(layout=FULL, hints=(PlayerHint(1), PlayerHint(2)))
        rows = manager.assign(tracks, evidence, SECOND)
        assert {row.track_id: row.player_id for row in rows} == {8: 1, 9: 2}

    def test_below_the_floor_publishes_no_player(self):
        """A track is still published so the debug view shows something is
        there -- with nobody attached, so the client draws nothing."""
        manager = PlayerIdentityManager(confidence=0.95)
        manager.gallery(1).add((1.0, 0.0), 1.0)
        tracks = [_track(8, 0.3, 0.6, embedding=(0.6, 0.8))]
        evidence = Evidence(layout=FULL, hints=(PlayerHint(1),))
        rows = manager.assign(tracks, evidence, SECOND)
        assert len(rows) == 1
        assert rows[0].player_id == UNIDENTIFIED

    def test_a_backend_with_no_embeddings_still_works_by_viewport(self):
        """Appearance is optional. Viewport ownership identifies a player with
        no appearance matching at all, which is the no-model backend's case."""
        manager = PlayerIdentityManager()
        tracks = [_track(1, 0.22, 0.22, region="upper_left", embedding=None)]
        rows = manager.assign(tracks, _quad_evidence(1), SECOND)
        assert rows[0].player_id == 1


class TestContinuity:
    def test_it_carries_a_track_through_a_frame_with_no_other_signal(self):
        manager = PlayerIdentityManager()
        tracks = [_track(1, 0.22, 0.22, region="upper_left")]
        manager.assign(tracks, _quad_evidence(1), SECOND)

        # The same track, now with nothing owning a viewport.
        moved = [_track(1, 0.45, 0.45)]
        rows = manager.assign(moved, Evidence(layout=FULL, hints=(PlayerHint(1),)), SECOND)
        assert rows[0].player_id == 1
        assert rows[0].source == "continuity"

    def test_continuity_never_writes_to_the_gallery(self):
        """A track that drifted onto the wrong entity would otherwise teach us
        that entity's appearance, and the mistake would outlive the drift."""
        manager = PlayerIdentityManager()
        manager.assign(
            [_track(1, 0.22, 0.22, region="upper_left")], _quad_evidence(1), SECOND
        )
        before = len(manager.gallery(1))
        manager.assign(
            [_track(1, 0.45, 0.45, embedding=(0.0, 1.0))],
            Evidence(layout=FULL, hints=(PlayerHint(1),)),
            SECOND,
        )
        assert len(manager.gallery(1)) == before

    def test_a_lost_track_loses_its_player(self):
        manager = PlayerIdentityManager()
        manager.assign(
            [_track(1, 0.22, 0.22, region="upper_left")], _quad_evidence(1), SECOND
        )
        rows = manager.assign(
            [_track(7, 0.45, 0.45)], Evidence(layout=FULL, hints=(PlayerHint(1),)), SECOND
        )
        assert rows[0].player_id == UNIDENTIFIED


class TestCorrelation:
    @staticmethod
    def _history(dx, dy, *, count=10, hz=20.0, now=SECOND):
        """A track walking steadily in one direction, newest last."""
        step = int(1_000_000_000 / hz)
        start = now - count * step
        return [
            (start + i * step, 0.5 + dx * i * 0.01, 0.5 + dy * i * 0.01)
            for i in range(count + 1)
        ]

    def test_moving_with_the_stick_correlates(self):
        track = _track(1, 0.5, 0.5, history=self._history(1.0, 0.0))
        trace = InputTrace(1, hz=20.0, samples=tuple((1.0, 0.0) for _ in range(10)))
        assert correlate(track, trace, SECOND) > CORRELATION_MIN

    def test_moving_against_the_stick_does_not(self):
        """Negative correlation is evidence against, and clamps to zero:
        'moved the opposite way' and 'did not move like that' are the same
        answer for our purposes."""
        track = _track(1, 0.5, 0.5, history=self._history(1.0, 0.0))
        trace = InputTrace(1, hz=20.0, samples=tuple((-1.0, 0.0) for _ in range(10)))
        assert correlate(track, trace, SECOND) == 0.0

    def test_a_track_with_no_history_correlates_with_nothing(self):
        track = _track(1, 0.5, 0.5, history=[])
        trace = InputTrace(1, hz=20.0, samples=tuple((1.0, 0.0) for _ in range(10)))
        assert correlate(track, trace, SECOND) == 0.0

    def test_an_empty_trace_correlates_with_nothing(self):
        track = _track(1, 0.5, 0.5, history=self._history(1.0, 0.0))
        assert correlate(track, InputTrace(1, samples=()), SECOND) == 0.0

    def test_it_separates_two_identical_characters(self):
        """The one thing appearance cannot do, and the reason this signal is
        worth its complexity."""
        manager = PlayerIdentityManager(confidence=0.5)
        same = (1.0, 0.0)
        tracks = [
            _track(8, 0.3, 0.5, embedding=same, history=self._history(1.0, 0.0)),
            _track(9, 0.6, 0.5, embedding=same, history=self._history(0.0, 1.0)),
        ]
        evidence = Evidence(
            layout=FULL,
            hints=(PlayerHint(1), PlayerHint(2)),
            traces=(
                InputTrace(1, 20.0, tuple((1.0, 0.0) for _ in range(10))),
                InputTrace(2, 20.0, tuple((0.0, 1.0) for _ in range(10))),
            ),
        )
        rows = manager.assign(tracks, evidence, SECOND)
        by_track = {row.track_id: row for row in rows}
        assert by_track[8].player_id == 1
        assert by_track[9].player_id == 2
        assert by_track[8].source == "input"

    def test_two_players_moving_alike_are_refused(self):
        """Most of a racing game's straight. This must refuse rather than
        allocate them arbitrarily."""
        manager = PlayerIdentityManager(confidence=0.5)
        tracks = [
            _track(8, 0.3, 0.5, history=self._history(1.0, 0.0)),
            _track(9, 0.6, 0.5, history=self._history(1.0, 0.0)),
        ]
        evidence = Evidence(
            layout=FULL,
            hints=(PlayerHint(1), PlayerHint(2)),
            traces=(
                InputTrace(1, 20.0, tuple((1.0, 0.0) for _ in range(10))),
                InputTrace(2, 20.0, tuple((1.0, 0.0) for _ in range(10))),
            ),
        )
        rows = manager.assign(tracks, evidence, SECOND)
        assert {row.player_id for row in rows} == {UNIDENTIFIED}

    def test_no_traces_leaves_the_rest_of_the_design_untouched(self):
        """Evidence() with no traces is the whole design with controller
        correlation switched off."""
        manager = PlayerIdentityManager()
        tracks = [_track(1, 0.22, 0.22, region="upper_left")]
        rows = manager.assign(tracks, _quad_evidence(1), SECOND)
        assert rows[0].player_id == 1


class TestTrustOrder:
    def test_a_weaker_signal_cannot_overturn_a_stronger_one(self):
        """Each pass only looks at tracks nobody has claimed. A weaker signal
        can fill a gap; it can never take a track from a stronger one."""
        manager = PlayerIdentityManager(confidence=0.5)
        # Player 2's gallery says track 1 looks like them...
        manager.gallery(2).add((1.0, 0.0), 1.0)
        # ...but track 1 is the camera subject of player 1's viewport.
        tracks = [
            _track(1, 0.22, 0.22, region="upper_left", embedding=(1.0, 0.0))
        ]
        evidence = Evidence(
            layout=QUAD_4,
            hints=(PlayerHint(1, ("upper_left",)), PlayerHint(2, ("upper_right",))),
        )
        rows = manager.assign(tracks, evidence, SECOND)
        assert rows[0].player_id == 1
        assert rows[0].source == "viewport"

    def test_one_player_is_never_on_two_tracks_at_once(self):
        manager = PlayerIdentityManager(confidence=0.5)
        manager.gallery(1).add((1.0, 0.0), 1.0)
        tracks = [
            _track(8, 0.3, 0.5, embedding=(1.0, 0.0)),
            _track(9, 0.6, 0.5, embedding=(0.99, 0.14)),
        ]
        evidence = Evidence(layout=FULL, hints=(PlayerHint(1),))
        rows = manager.assign(tracks, evidence, SECOND)
        assigned = [row for row in rows if row.identified]
        assert len(assigned) <= 1


class TestLifecycle:
    def test_forget_drops_a_departed_player_entirely(self):
        """The same leak _forget_rumble_state and SyncGovernor.forget exist to
        fix: a gallery for somebody who left goes on competing for tracks."""
        manager = PlayerIdentityManager()
        manager.assign(
            [_track(1, 0.22, 0.22, region="upper_left", embedding=(1.0, 0.0))],
            _quad_evidence(1),
            SECOND,
        )
        assert len(manager.gallery(1)) == 1
        manager.forget(1)
        assert len(manager.gallery(1)) == 0

        rows = manager.assign(
            [_track(1, 0.45, 0.45)], Evidence(layout=FULL, hints=(PlayerHint(1),)), SECOND
        )
        assert rows[0].player_id == UNIDENTIFIED, "continuity survived forget()"

    def test_reset_clears_everything(self):
        manager = PlayerIdentityManager()
        manager.assign(
            [_track(1, 0.22, 0.22, region="upper_left")], _quad_evidence(1), SECOND
        )
        manager.reset()
        assert manager.snapshot()["players"] == 0

    def test_no_tracks_is_not_an_error(self):
        manager = PlayerIdentityManager()
        assert manager.assign([], _quad_evidence(1), SECOND) == []

    def test_no_hints_publishes_everything_unidentified(self):
        """Before the Bluetooth server has said who is playing."""
        manager = PlayerIdentityManager()
        rows = manager.assign([_track(1, 0.5, 0.5)], Evidence(), SECOND)
        assert rows[0].player_id == UNIDENTIFIED

    def test_the_snapshot_reports_refusals(self):
        """'Identification is not working' and 'identification is refusing to
        guess' look the same from outside unless this is reported."""
        manager = PlayerIdentityManager()
        manager.gallery(1).add((1.0, 0.0), 0.1)
        assert manager.snapshot()["refused"] == 1


class TestMarginIsLoadBearing:
    def test_the_margin_is_what_refuses_a_tie(self):
        """Stated as a behaviour rather than a constant: if AMBIGUITY_MARGIN
        were removed or set to zero, the tie above would resolve arbitrarily
        and this pins that it must not."""
        assert AMBIGUITY_MARGIN > 0.0

        manager = PlayerIdentityManager(confidence=0.5)
        manager.gallery(1).add((1.0, 0.0), 1.0)
        manager.gallery(2).add((1.0, 0.0), 1.0)
        tracks = [_track(8, 0.3, 0.5, embedding=(1.0, 0.0))]
        evidence = Evidence(layout=FULL, hints=(PlayerHint(1), PlayerHint(2)))
        rows = manager.assign(tracks, evidence, SECOND)
        assert rows[0].player_id == UNIDENTIFIED

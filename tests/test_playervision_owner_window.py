"""A viewport's player is where its camera keeps them, judged by colour.

Reported from a three-player Mario Kart 64 race, players 1 and 2 connected:
Mario should be named in every viewport he appears in, and Luigi in every one
he appears in -- including viewport 1, where only his head shows, and viewport
3, where he is cut in half by the seam. Measured through the running system:

* the general-purpose detector found **neither player's own kart**. It boxed
  the big "1" numeral instead, which then won player 1's viewport and taught
  the gallery what a numeral looks like -- so the "LAP 1/3" text matched
  player 1 at 0.99 in two other viewports;
* player 2 was never placed in their own viewport at all, and continuity
  carried a stale player 2 label on the minimap indefinitely;
* the ImageNet embedder scored everything 0.54-0.90 against everyone.

`playervision_signatures_mk64.json` holds colour signatures measured from that
frame -- numbers, not pictures -- with each player's owner window as the
worker describes it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from common.screen_regions import FULL, QUAD_4, Rect
from videoserver.playervision.backends.base import (
    Capabilities,
    PlayerVisionBackend,
    SampleFrame,
)
from videoserver.playervision.identity import (
    CONTINUITY_SLACK,
    OWNER_WINDOW_ABOVE,
    OWNER_WINDOW_BELOW,
    PlayerIdentityManager,
    cosine,
)
from videoserver.playervision.signature import APPEARANCE_FLOOR
from videoserver.playervision.tracking import EntityTracker
from videoserver.playervision.types import (
    UNIDENTIFIED,
    Detection,
    Evidence,
    PlayerHint,
    Track,
)
from videoserver.playervision.worker import (
    OWNER_STEADY_SAMPLES,
    VisionWorker,
)

SECOND = 1_000_000_000
SIGNATURES = {
    key: tuple(value)
    for key, value in json.loads(
        (Path(__file__).parent / "playervision_signatures_mk64.json").read_text(
            encoding="utf-8"
        )
    ).items()
    if not key.startswith("_")
}

#: The capture's letterbox and the operator's assignment, as reported live.
ACTIVE = (0.09, 0.0, 0.821, 1.0)


def three_player() -> Evidence:
    return Evidence(
        layout=QUAD_4,
        hints=(
            PlayerHint(1, ("upper_left", "upper", "left")),
            PlayerHint(2, ("upper_right", "lower", "right")),
        ),
        active=ACTIVE,
    )


def track(track_id, box, region, signature, *, owner="", now=SECOND, hits=10):
    return Track(
        track_id=track_id, box=box, region=region, first_ns=0, last_ns=now,
        hits=hits, embedding=SIGNATURES.get(signature), owner=owner,
    )


def manager_like_the_worker() -> PlayerIdentityManager:
    """How the worker configures it for the ONNX backend on a split."""
    manager = PlayerIdentityManager(confidence=0.4)     # the operator's saved floor
    manager.appearance_minimum = APPEARANCE_FLOOR
    manager.owner_windows_expected = True
    return manager


def the_race(manager: PlayerIdentityManager, now=SECOND) -> list[Track]:
    """The frame, as tracks: both owner windows and what the model boxed."""
    windows = dict(manager.owner_windows(three_player()))
    return [
        track(101, windows["upper_left"], "upper_left", "mario_own",
              owner="upper_left", now=now),
        track(102, windows["upper_right"], "upper_right", "luigi_own",
              owner="upper_right", now=now),
        # What the model found. Positions are the frame's, roughly.
        track(1, Rect(0.17, 0.34, 0.05, 0.14), "upper_left", "hud_numeral_1", now=now),
        track(4, Rect(0.40, 0.28, 0.10, 0.21), "upper_left", "luigi_head", now=now),
        track(2, Rect(0.62, 0.17, 0.05, 0.11), "upper_right", "mario_distant", now=now),
        track(8, Rect(0.53, 0.36, 0.07, 0.09), "upper_right", "hud_lap_text", now=now),
        track(9, Rect(0.80, 0.35, 0.07, 0.12), "upper_right", "hud_numeral_2", now=now),
        track(3, Rect(0.35, 0.73, 0.04, 0.10), "lower_left", "mario_viewport3", now=now),
        track(6, Rect(0.26, 0.75, 0.07, 0.19), "lower_left", "peach", now=now),
        track(5, Rect(0.479, 0.74, 0.02, 0.18), "lower_left", "luigi_at_seam", now=now),
        track(7, Rect(0.67, 0.57, 0.08, 0.38), "lower_right", "minimap", now=now),
    ]


def names(rows) -> dict[int, int]:
    return {row.track_id: row.player_id for row in rows}


class TestTheReportedRace:
    def test_each_player_is_named_wherever_they_appear(self):
        manager = manager_like_the_worker()
        named = names(manager.assign(the_race(manager), three_player(), SECOND))

        assert named[101] == 1 and named[102] == 2, "their own viewports"
        assert named[2] == 1, "Mario, distant, in player 2's viewport"
        assert named[3] == 1, "Mario in viewport 3"
        assert named[4] == 2, "Luigi's head in player 1's viewport"
        assert named[5] == 2, "Luigi cut by the seam in viewport 3"

    def test_nothing_else_is_named(self):
        manager = manager_like_the_worker()
        named = names(manager.assign(the_race(manager), three_player(), SECOND))

        for hud_or_bystander in (1, 6, 7, 8, 9):
            assert named[hud_or_bystander] == UNIDENTIFIED, hud_or_bystander

    def test_it_holds_over_many_rounds(self):
        """Galleries fill; nothing drifts onto the minimap or the HUD."""
        manager = manager_like_the_worker()
        for step in range(1, 30):
            named = names(manager.assign(
                the_race(manager, now=step * SECOND), three_player(), step * SECOND
            ))
        assert {k: named[k] for k in (101, 102, 2, 3, 4, 5)} == {
            101: 1, 102: 2, 2: 1, 3: 1, 4: 2, 5: 2,
        }
        assert all(named[k] == UNIDENTIFIED for k in (1, 6, 7, 8, 9))

    def test_the_signatures_are_the_measured_gap(self):
        """True matches sit above the floor and everything else below it --
        the property the floor was chosen for, on the real frame."""
        mario, luigi = SIGNATURES["mario_own"], SIGNATURES["luigi_own"]
        for key in ("mario_distant", "mario_viewport3"):
            assert cosine(SIGNATURES[key], mario) >= APPEARANCE_FLOOR, key
        for key in ("luigi_head", "luigi_at_seam"):
            assert cosine(SIGNATURES[key], luigi) >= APPEARANCE_FLOOR, key
        for key in ("peach", "peach_at_seam", "hud_lap_text", "hud_numeral_1",
                    "hud_numeral_2", "minimap"):
            assert cosine(SIGNATURES[key], mario) < APPEARANCE_FLOOR - 0.2, key
            assert cosine(SIGNATURES[key], luigi) < APPEARANCE_FLOOR - 0.2, key


class TestTheOwnerWindow:
    def test_it_beats_a_box_right_on_the_anchor(self):
        """What the camera holds is the player, whatever the model drew."""
        manager = manager_like_the_worker()
        window = dict(manager.owner_windows(three_player()))["upper_left"]
        numeral = Rect(window.x + 0.01, window.y + 0.05, 0.04, 0.10)
        rows = names(manager.assign([
            track(101, window, "upper_left", "mario_own", owner="upper_left"),
            track(1, numeral, "upper_left", "hud_numeral_1"),
        ], three_player(), SECOND))
        assert rows[101] == 1
        assert rows[1] == UNIDENTIFIED

    def test_a_viewport_waits_for_its_window_rather_than_taking_a_box(self):
        """Nearest-box is what poisoned a gallery here: a fragment taken as
        player 2 in the first samples, then the minimap matched it."""
        manager = manager_like_the_worker()
        rows = names(manager.assign([
            track(1, Rect(0.26, 0.26, 0.08, 0.12), "upper_left", "hud_numeral_1"),
        ], three_player(), SECOND))
        assert rows[1] == UNIDENTIFIED
        assert not manager.snapshot()["exemplars"]

    def test_a_stale_window_does_not_count(self):
        """The worker adds one only while the view holds still; the tracker
        keeps a missed track for a few frames, and those are not this round."""
        manager = manager_like_the_worker()
        window = dict(manager.owner_windows(three_player()))["upper_left"]
        rows = names(manager.assign([
            track(101, window, "upper_left", "mario_own", owner="upper_left",
                  now=SECOND // 2),
        ], three_player(), SECOND))
        assert rows[101] == UNIDENTIFIED

    def test_only_the_window_writes_a_gallery(self):
        manager = manager_like_the_worker()
        manager.assign(the_race(manager), three_player(), SECOND)
        assert manager.snapshot()["exemplars"] == {"1": 1, "2": 1}

    def test_without_windows_matches_still_teach(self):
        """A shared screen has no windows, and its galleries are built from
        the signals it has."""
        manager = PlayerIdentityManager(confidence=0.4)
        manager.gallery(1).add(SIGNATURES["mario_own"], 0.92)
        full = Evidence(layout=FULL, hints=(PlayerHint(1),))
        manager.assign([track(3, Rect(0.3, 0.3, 0.1, 0.1), "", "mario_viewport3")],
                       full, SECOND)
        assert manager.snapshot()["exemplars"] == {"1": 2}

    def test_it_is_only_for_owned_viewports_of_a_split(self):
        manager = manager_like_the_worker()
        assert [region for region, _ in manager.owner_windows(three_player())] == [
            "upper_left", "upper_right",
        ]
        shared = Evidence(layout=FULL, hints=three_player().hints)
        assert manager.owner_windows(shared) == []

    def test_it_sits_inside_its_viewport_and_reaches_up_for_the_head(self):
        manager = manager_like_the_worker()
        cell = manager._cell_rect("upper_left", QUAD_4, ACTIVE)
        window = dict(manager.owner_windows(three_player()))["upper_left"]
        assert cell.x <= window.x and window.x + window.width <= cell.x + cell.width
        assert cell.y <= window.y and window.y + window.height <= cell.y + cell.height
        ax, ay = manager.anchor("upper_left")
        above = ay - (window.y - cell.y) / cell.height
        below = (window.y + window.height - cell.y) / cell.height - ay
        assert above == pytest.approx(OWNER_WINDOW_ABOVE)
        assert below == pytest.approx(OWNER_WINDOW_BELOW)
        assert above > below

    def test_it_teaches_neither_the_anchor_nor_the_detector_floor(self):
        """It sits at the anchor by construction; learning from it would be
        learning from itself."""
        manager = manager_like_the_worker()
        for step in range(1, 60):
            manager.assign(the_race(manager, now=step * SECOND), three_player(),
                           step * SECOND)
        assert manager.calibration.learned_anchor("upper_left") is None
        assert manager.calibration.learned_floor() is None


class TestPiecesOfOneThingAreNotRivals:
    """On a real frame four boxes of one Mario each matched him at about 0.9,
    and each refused the others -- the Mario on screen went unnamed."""

    def _with_pieces(self, pieces):
        manager = manager_like_the_worker()
        window = dict(manager.owner_windows(three_player()))["upper_left"]
        tracks = [track(101, window, "upper_left", "mario_own", owner="upper_left")]
        tracks += pieces
        return names(manager.assign(tracks, three_player(), SECOND))

    def test_a_cap_and_the_kart_under_it_name_one_mario(self):
        cap = track(20, Rect(0.35, 0.72, 0.035, 0.06), "lower_left", "mario_viewport3")
        kart = track(21, Rect(0.34, 0.785, 0.07, 0.07), "lower_left", "mario_distant")
        named = self._with_pieces([cap, kart])
        assert sorted([named[20], named[21]]) == [UNIDENTIFIED, 1]

    def test_two_separate_marios_are_still_a_tie(self):
        """Apart, they are two things that look the same -- the case the
        ambiguity rule exists for."""
        left = track(20, Rect(0.15, 0.72, 0.035, 0.06), "lower_left", "mario_viewport3")
        right = track(21, Rect(0.40, 0.72, 0.035, 0.06), "lower_left", "mario_distant")
        named = self._with_pieces([left, right])
        assert named[20] == UNIDENTIFIED and named[21] == UNIDENTIFIED


class TestTheAppearanceFloor:
    def test_the_backends_floor_beats_a_lower_operator_floor(self):
        """Unrelated colour signatures routinely score 0.6; the operator's
        0.4 would name the minimap."""
        manager = PlayerIdentityManager(confidence=0.4)
        manager.appearance_minimum = APPEARANCE_FLOOR
        assert manager.appearance_floor() == APPEARANCE_FLOOR

    def test_a_higher_operator_floor_still_wins(self):
        manager = PlayerIdentityManager(confidence=0.95)
        manager.appearance_minimum = APPEARANCE_FLOOR
        assert manager.appearance_floor() == pytest.approx(0.95)


class TestContinuityYieldsToAContradiction:
    def _manager_with_luigi(self):
        manager = manager_like_the_worker()
        for _ in range(3):
            manager.gallery(2).add(SIGNATURES["luigi_own"], 0.92)
        return manager

    def test_a_label_its_appearance_contradicts_is_dropped(self):
        """The minimap held player 2's name for as long as its track lived."""
        manager = self._manager_with_luigi()
        manager._previous = {7: 2}
        minimap = track(7, Rect(0.67, 0.57, 0.08, 0.38), "lower_right", "minimap")
        row = manager.assign([minimap], three_player(), SECOND)[0]
        assert row.player_id == UNIDENTIFIED
        assert "contradicts" in " ".join(
            s.note for j in manager.judgements() for s in j.scores
        )

    def test_a_weaker_but_consistent_look_is_carried(self):
        """Continuity exists for the frames where a character is half
        hidden: a match below the floor but inside the slack keeps its name."""
        manager = self._manager_with_luigi()
        manager._previous = {5: 2}
        luigi = SIGNATURES["luigi_own"]
        mario = SIGNATURES["mario_own"]

        def blend(weight):
            # Luigi, with some of the kart beside him in the box.
            mixed = tuple(a + weight * b for a, b in zip(luigi, mario))
            norm = sum(v * v for v in mixed) ** 0.5
            return tuple(v / norm for v in mixed)

        blended = next(
            view for view in (blend(step / 20) for step in range(1, 60))
            if cosine(view, luigi) < APPEARANCE_FLOOR - CONTINUITY_SLACK / 2
        )
        score = cosine(blended, luigi)
        assert APPEARANCE_FLOOR - CONTINUITY_SLACK <= score < APPEARANCE_FLOOR
        half_hidden = Track(
            track_id=5, box=Rect(0.479, 0.74, 0.02, 0.18), region="lower_left",
            first_ns=0, last_ns=SECOND, hits=10, embedding=blended,
        )
        row = manager.assign([half_hidden], three_player(), SECOND)[0]
        assert row.player_id == 2 and row.source == "continuity"


class TestTheTracker:
    def test_a_window_follows_only_its_own_window(self):
        """Paired with a fragment inside it, the window's identity would pass
        to the fragment and the window would come back as a stranger."""
        tracker = EntityTracker()
        window = Rect(0.25, 0.2, 0.09, 0.22)
        first = tracker.update(
            [Detection(box=window, owner="upper_left")], QUAD_4, SECOND
        )
        window_id = first[0].track_id
        fragment = Rect(0.26, 0.25, 0.08, 0.15)       # overlaps it heavily
        tracks = tracker.update([Detection(box=fragment)], QUAD_4, 2 * SECOND)
        by_id = {t.track_id: t for t in tracks}
        assert by_id[window_id].box == window, "the window took the fragment"
        assert any(t.owner == "" and t.box == fragment for t in tracks)


class _TilingBackend(PlayerVisionBackend):
    """What the worker needs from a backend: cells in, a view of a window."""

    name = "tiling-fake"
    embeddings = True
    tiles = True
    appearance_floor = APPEARANCE_FLOOR

    def __init__(self):
        self.cells_seen: list[tuple] = []
        self.window_vector = SIGNATURES["mario_own"]

    def start(self):
        return Capabilities(backend=self.name, available=True, reason="")

    def detect(self, frame, cells=()):
        self.cells_seen.append(tuple(cells))
        return []

    def describe(self, frame, boxes):
        return [self.window_vector for _ in boxes]

    def stop(self):
        pass


def _frame():
    data = bytes(64 * 36 * 3)
    return SampleFrame(memoryview(data), 64, 36, 64 * 3, pixel_format="rgb24")


class TestTheWorker:
    def _worker(self, layout=QUAD_4):
        backend = _TilingBackend()
        worker = VisionWorker(backend, confidence=0.4)
        worker.configure(layout=layout, active=ACTIVE, hints=three_player().hints)
        return worker, backend

    def test_a_tiling_backend_is_given_the_viewports_of_a_split(self):
        worker, backend = self._worker()
        worker.process(_frame(), SECOND)
        assert len(backend.cells_seen[-1]) == 4

    def test_and_none_on_a_shared_screen(self):
        worker, backend = self._worker(layout=FULL)
        worker.process(_frame(), SECOND)
        assert backend.cells_seen[-1] == ()

    def test_the_window_stands_in_only_once_it_has_held_still(self):
        worker, backend = self._worker()
        owned = []
        for step in range(1, OWNER_STEADY_SAMPLES + 3):
            rows = worker.process(_frame(), step * SECOND)
            owned.append(any(r.player_id == 1 and r.source == "viewport" for r in rows))
        assert owned[:OWNER_STEADY_SAMPLES] == [False] * OWNER_STEADY_SAMPLES
        assert all(owned[OWNER_STEADY_SAMPLES:])

    def test_a_window_that_keeps_changing_never_does(self):
        """What a first-person camera looks like: whatever the player faces."""
        worker, backend = self._worker()
        views = [SIGNATURES[k] for k in ("mario_own", "minimap", "luigi_own",
                                         "hud_numeral_1", "peach", "luigi_head")]
        for step, view in enumerate(views, start=1):
            backend.window_vector = view
            rows = worker.process(_frame(), step * SECOND)
            assert not any(r.source == "viewport" for r in rows)

    def test_the_backends_floor_reaches_identity(self):
        worker, _ = self._worker()
        worker.process(_frame(), SECOND)
        assert worker._identity.appearance_floor() == APPEARANCE_FLOOR

    def test_a_backend_that_cannot_describe_is_not_waited_for(self):
        """Appearance vectors but the base `describe`: every viewport would
        wait for a window that can never come."""

        class NoDescribe(_TilingBackend):
            describe = PlayerVisionBackend.describe

        backend = NoDescribe()
        worker = VisionWorker(backend, confidence=0.4)
        worker.configure(layout=QUAD_4, active=ACTIVE, hints=three_player().hints)
        worker.process(_frame(), SECOND)
        assert worker._identity.owner_windows_expected is False


class TestTheSampleIsBigEnoughToTile:
    def test_a_split_gets_a_wider_sample_for_a_tiling_backend(self):
        from videoserver.playervision.service import TILED_SCALE, PlayerVisionService

        service = PlayerVisionService()
        service._backend = _TilingBackend()
        service._caps = Capabilities(input_width=416, input_height=416)
        assert service.sample_width() == 416
        service._layout = QUAD_4
        assert service.sample_width() == int(416 * TILED_SCALE)

    def test_a_backend_that_does_not_tile_is_unchanged(self):
        from videoserver.playervision.service import PlayerVisionService

        class Plain(_TilingBackend):
            tiles = False

        service = PlayerVisionService()
        service._backend = Plain()
        service._caps = Capabilities(input_width=416, input_height=416)
        service._layout = QUAD_4
        assert service.sample_width() == 416

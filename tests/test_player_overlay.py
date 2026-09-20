"""Who sees which label.

The case this module exists for is the one in the middle: a client holding
several controllers, drawing several viewports, which must have each player
hidden in *their own* view and shown in the others. Getting that wrong does
not look like a bug -- it looks like a player being told their own name, or
worse, never being told anybody else's.
"""

from __future__ import annotations

from common.screen_regions import FULL, HORIZONTAL_2, QUAD_4, VERTICAL_2

from server.player_overlay import (
    MAX_LABELS,
    labels_for_client,
    multiplayer,
    nothing_to_show,
    player_hints,
    player_names,
)


class FakeChannel:
    """Enough of an OutputChannel for the join. Nothing here touches a radio."""

    def __init__(self, number, *, client=None, slot=0, username="", regions=()):
        self.number = number
        self.assigned_client = client
        self.assigned_slot = slot if client else None
        self.username = username
        self.regions = list(regions)

    @property
    def is_assigned(self):
        return self.assigned_client is not None and self.assigned_slot is not None


class FakeRouter:
    def __init__(self, *channels):
        self._channels = list(channels)

    def channels(self):
        return list(self._channels)


def _track(player, x=0.5, y=0.5, *, region="", track_id=1, confidence=0.9):
    return {
        "p": player, "t": track_id, "r": region,
        "x": x, "y": y, "w": 0.1, "h": 0.2, "c": confidence,
    }


def _quad_router():
    """Four players, four clients, one quadrant each."""
    return FakeRouter(
        FakeChannel(1, client="c1", username="Alex", regions=["upper_left"]),
        FakeChannel(2, client="c2", username="Bo", regions=["upper_right"]),
        FakeChannel(3, client="c3", username="Cass", regions=["lower_left"]),
        FakeChannel(4, client="c4", username="Dee", regions=["lower_right"]),
    )


def _all_tracks():
    return [
        _track(1, 0.2, 0.2, region="upper_left", track_id=11),
        _track(2, 0.7, 0.2, region="upper_right", track_id=12),
        _track(3, 0.2, 0.7, region="lower_left", track_id=13),
        _track(4, 0.7, 0.7, region="lower_right", track_id=14),
    ]


def _players(body):
    return {label["p"] for label in body["labels"]}


class TestMultiplayer:
    def test_two_assigned_adapters_is_multiplayer(self):
        router = FakeRouter(
            FakeChannel(1, client="c1"), FakeChannel(2, client="c2")
        )
        assert multiplayer(router) is True

    def test_one_assigned_adapter_is_not(self):
        router = FakeRouter(FakeChannel(1, client="c1"), FakeChannel(2))
        assert multiplayer(router) is False

    def test_an_unassigned_adapter_is_hardware_not_a_person(self):
        """Four dongles plugged in with one player is still one player."""
        router = FakeRouter(*(FakeChannel(n) for n in range(1, 5)))
        assert multiplayer(router) is False

    def test_it_is_never_inferred_from_the_layout(self):
        """A shared-screen four-player game looks exactly like a one-player
        game to a split-screen detector. This is the assumption that must not
        be made."""
        single = FakeRouter(FakeChannel(1, client="c1", regions=["upper_left"]))
        body = labels_for_client(single, "c1", QUAD_4, _all_tracks())
        assert body["labels"] == []


class TestSplitScreen:
    def test_a_player_is_not_shown_their_own_name_in_their_own_view(self):
        body = labels_for_client(_quad_router(), "c1", QUAD_4, _all_tracks())
        assert 1 not in _players(body)

    def test_they_are_shown_everybody_else(self):
        body = labels_for_client(_quad_router(), "c1", QUAD_4, _all_tracks())
        assert _players(body) == {2, 3, 4}

    def test_every_player_sees_the_other_three(self):
        router = _quad_router()
        for client, own in (("c1", 1), ("c2", 2), ("c3", 3), ("c4", 4)):
            body = labels_for_client(router, client, QUAD_4, _all_tracks())
            assert _players(body) == {1, 2, 3, 4} - {own}, client

    def test_a_player_is_shown_in_somebody_elses_viewport(self):
        """The case the appearance model exists for: player 1's character
        turning up inside player 2's view, which is exactly what should be
        labelled."""
        tracks = [_track(1, 0.9, 0.3, region="upper_right", track_id=21)]
        body = labels_for_client(_quad_router(), "c2", QUAD_4, tracks)
        assert _players(body) == {1}

    def test_two_player_vertical(self):
        router = FakeRouter(
            FakeChannel(1, client="c1", username="Alex", regions=["left"]),
            FakeChannel(2, client="c2", username="Bo", regions=["right"]),
        )
        tracks = [
            _track(1, 0.2, 0.5, region="left"),
            _track(2, 0.8, 0.5, region="right", track_id=2),
        ]
        assert _players(labels_for_client(router, "c1", VERTICAL_2, tracks)) == {2}
        assert _players(labels_for_client(router, "c2", VERTICAL_2, tracks)) == {1}

    def test_a_region_from_another_layout_excludes_nobody(self):
        """A controller carries every assignment it might need. One belonging
        to a layout that is not on screen must not silently hide a label."""
        router = FakeRouter(
            FakeChannel(1, client="c1", username="Alex", regions=["upper_left"]),
            FakeChannel(2, client="c2", username="Bo", regions=["lower_right"]),
        )
        # HORIZONTAL_2 is on screen; neither holds `upper` or `lower`.
        tracks = [_track(1, 0.2, 0.2, region="upper"), _track(2, 0.7, 0.7, region="lower", track_id=2)]
        body = labels_for_client(router, "c1", HORIZONTAL_2, tracks)
        assert _players(body) == {1, 2}


class TestOneClientManyControllers:
    """The case this module exists for. Do not assume one client == one player."""

    @staticmethod
    def _router():
        # One client holds players 1 and 2; another holds 3 and 4.
        return FakeRouter(
            FakeChannel(1, client="host", slot=0, username="Alex", regions=["upper_left"]),
            FakeChannel(2, client="host", slot=1, username="Bo", regions=["upper_right"]),
            FakeChannel(3, client="away", slot=0, username="Cass", regions=["lower_left"]),
            FakeChannel(4, client="away", slot=1, username="Dee", regions=["lower_right"]),
        )

    def test_each_of_its_players_is_hidden_in_their_own_view(self):
        body = labels_for_client(self._router(), "host", QUAD_4, _all_tracks())
        labelled = {(label["p"], label["r"]) for label in body["labels"]}
        assert (1, "upper_left") not in labelled
        assert (2, "upper_right") not in labelled

    def test_but_each_is_still_shown_in_the_others_view(self):
        """Not hidden client-wide. Seeing your team-mate's name over their
        character in *their* viewport is the thing this is for."""
        tracks = [
            _track(1, 0.7, 0.3, region="upper_right", track_id=31),
            _track(2, 0.2, 0.3, region="upper_left", track_id=32),
        ]
        body = labels_for_client(self._router(), "host", QUAD_4, tracks)
        assert _players(body) == {1, 2}

    def test_the_other_clients_players_are_always_shown(self):
        body = labels_for_client(self._router(), "host", QUAD_4, _all_tracks())
        assert {3, 4} <= _players(body)


class TestSharedScreen:
    def test_everybody_sees_every_label(self):
        """No viewport to own, so nothing to exclude -- and being told which
        of four characters is which is the entire point."""
        router = _quad_router()
        tracks = [_track(n, 0.2 * n, 0.5, track_id=n) for n in (1, 2, 3, 4)]
        for client in ("c1", "c2", "c3", "c4"):
            assert _players(labels_for_client(router, client, FULL, tracks)) == {1, 2, 3, 4}

    def test_the_local_players_label_is_not_suppressed(self):
        """Explicitly the opposite of the split-screen rule."""
        router = _quad_router()
        body = labels_for_client(router, "c1", FULL, [_track(1, 0.3, 0.5)])
        assert _players(body) == {1}

    def test_a_stale_region_does_not_exclude_on_a_shared_screen(self):
        router = _quad_router()
        tracks = [_track(1, 0.2, 0.2, region="upper_left")]
        assert _players(labels_for_client(router, "c1", FULL, tracks)) == {1}


class TestRefusals:
    def test_an_unidentified_track_carries_no_label(self):
        """The source publishes these so its own debug view can show that
        something is there. There is no name to draw."""
        body = labels_for_client(_quad_router(), "c1", QUAD_4, [_track(0, 0.7, 0.3)])
        assert body["labels"] == []

    def test_a_departed_players_label_goes_away_with_them(self):
        """The source may still remember somebody whose adapter has been
        unassigned or disabled."""
        router = FakeRouter(
            FakeChannel(1, client="c1", username="Alex", regions=["upper_left"]),
            FakeChannel(2, client="c2", username="Bo", regions=["upper_right"]),
        )
        tracks = [_track(3, 0.2, 0.7, region="lower_left")]
        assert labels_for_client(router, "c1", QUAD_4, tracks)["labels"] == []

    def test_an_unnumbered_adapter_is_nobody(self):
        """A number is allocated the first time an adapter is enabled, so zero
        is an ordinary state -- and must read as no identity, never player
        zero."""
        router = FakeRouter(
            FakeChannel(0, client="c1", username="Alex"),
            FakeChannel(0, client="c2", username="Bo"),
        )
        assert player_names(router) == {}
        assert labels_for_client(router, "c1", FULL, [_track(0)])["labels"] == []

    def test_no_tracks_still_sends_a_message(self):
        """A client that was drawing labels has to be told to stop, and
        silence cannot say that."""
        body = labels_for_client(_quad_router(), "c1", QUAD_4, [])
        assert body == {"layout": QUAD_4, "labels": []}

    def test_an_empty_client_id_shows_nothing(self):
        assert labels_for_client(_quad_router(), "", QUAD_4, _all_tracks())["labels"] == []


class TestBounds:
    def test_the_label_count_is_capped(self):
        """The control channel refuses an oversized message whole, so an
        unbounded list means a busy scene costs a client every label."""
        router = FakeRouter(
            *(FakeChannel(n, client=f"c{n}", username=f"P{n}") for n in range(1, 5))
        )
        tracks = [_track((n % 4) + 1, track_id=n) for n in range(40)]
        body = labels_for_client(router, "c1", FULL, tracks)
        assert len(body["labels"]) <= MAX_LABELS

    def test_coordinates_are_clamped_not_dropped(self):
        """A box a hair outside the frame is a rounding artefact; dropping the
        label would be the worse answer."""
        router = _quad_router()
        body = labels_for_client(router, "c1", FULL, [_track(2, x=1.4, y=-0.3)])
        label = body["labels"][0]
        assert label["x"] == 1.0 and label["y"] == 0.0

    def test_nonsense_coordinates_do_not_raise(self):
        router = _quad_router()
        track = _track(2)
        track.update({"x": "banana", "y": None, "w": [], "c": "x"})
        body = labels_for_client(router, "c1", FULL, [track])
        assert body["labels"][0]["x"] == 0.0

    def test_a_nan_coordinate_becomes_zero(self):
        """NaN fails every comparison, so it sails through a range check and
        lands in the client's geometry as a box that cannot be drawn."""
        router = _quad_router()
        track = _track(2)
        track["x"] = float("nan")
        assert labels_for_client(router, "c1", FULL, [track])["labels"][0]["x"] == 0.0

    def test_a_long_name_is_trimmed(self):
        router = FakeRouter(
            FakeChannel(1, client="c1", username="x" * 200),
            FakeChannel(2, client="c2", username="Bo"),
        )
        assert len(player_names(router)[1]) <= 24


class TestNames:
    def test_the_username_is_what_the_player_typed(self):
        router = FakeRouter(
            FakeChannel(1, client="c1", username="Alex"),
            FakeChannel(2, client="c2", username="Bo"),
        )
        assert player_names(router) == {1: "Alex", 2: "Bo"}

    def test_a_nameless_player_falls_back_to_their_number(self):
        router = FakeRouter(
            FakeChannel(1, client="c1"), FakeChannel(2, client="c2")
        )
        assert player_names(router) == {1: "Player 1", 2: "Player 2"}

    def test_whitespace_is_not_a_name(self):
        router = FakeRouter(
            FakeChannel(1, client="c1", username="   "),
            FakeChannel(2, client="c2", username="Bo"),
        )
        assert player_names(router)[1] == "Player 1"


class TestHints:
    def test_ids_only_never_names(self):
        """In external mode the capture machine belongs to somebody else and
        has no business learning who is playing."""
        hints = player_hints(_quad_router(), QUAD_4)
        blob = repr(hints)
        for name in ("Alex", "Bo", "Cass", "Dee"):
            assert name not in blob

    def test_it_carries_the_viewport_map(self):
        assert player_hints(_quad_router(), QUAD_4) == [
            {"id": 1, "r": ["upper_left"]},
            {"id": 2, "r": ["upper_right"]},
            {"id": 3, "r": ["lower_left"]},
            {"id": 4, "r": ["lower_right"]},
        ]

    def test_regions_are_filtered_to_the_live_layout(self):
        router = FakeRouter(
            FakeChannel(1, client="c1", regions=["upper_left", "left"]),
            FakeChannel(2, client="c2", regions=["upper_right", "right"]),
        )
        assert player_hints(router, VERTICAL_2) == [
            {"id": 1, "r": ["left"]},
            {"id": 2, "r": ["right"]},
        ]

    def test_an_unassigned_or_unnumbered_adapter_is_left_out(self):
        router = FakeRouter(
            FakeChannel(1, client="c1", regions=["upper_left"]),
            FakeChannel(2, regions=["upper_right"]),          # nobody on it
            FakeChannel(0, client="c3", regions=["lower_left"]),  # never enabled
        )
        assert [hint["id"] for hint in player_hints(router, QUAD_4)] == [1]

    def test_it_is_ordered_by_player_number(self):
        router = FakeRouter(
            FakeChannel(3, client="c3"), FakeChannel(1, client="c1"),
            FakeChannel(2, client="c2"),
        )
        assert [hint["id"] for hint in player_hints(router, FULL)] == [1, 2, 3]


class TestNothingToShow:
    def test_it_is_an_explicit_empty_message(self):
        assert nothing_to_show(QUAD_4) == {"layout": QUAD_4, "labels": []}

    def test_an_unknown_layout_falls_back_to_full(self):
        assert nothing_to_show("QUAD_5")["layout"] == FULL

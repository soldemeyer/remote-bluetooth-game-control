"""The whole chain, from a detected layout to a client's crop.

Phase 8. Every stage has its own tests; each of them looks correct alone, and
this is the one that would catch two of them disagreeing about the shape of
what passes between them.

Deliberately not an end-to-end socket test -- ``test_video_e2e.py`` is that,
and it takes eight seconds. These drive the real objects through their real
interfaces with nothing faked but the transport, which is where the joins
actually are.
"""

from __future__ import annotations

import pytest

from common.protocol import ControlOp
from common.screen_regions import (
    FULL,
    HORIZONTAL_2,
    LEFT,
    LOWER_LEFT,
    LOWER_RIGHT,
    QUAD_4,
    RIGHT,
    UPPER_LEFT,
    UPPER_RIGHT,
    VERTICAL_2,
)
from server.bt.profiles import create_profile
from server.bt.sink import MockSink
from server.config import ServerConfig
from server.router import OutputChannel, Router
from server.screen_state import regions_message
from server.video import VideoRegistry


class Sent:
    """Every control message the datapath would have put on the wire."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, dict]] = []

    def of(self, client_id: str, op: str) -> list[dict]:
        return [b for c, o, b in self.messages if c == client_id and o == op]

    def latest_regions(self, client_id: str) -> dict | None:
        found = self.of(client_id, ControlOp.VIDEO_REGIONS)
        return found[-1] if found else None


class FakeSession:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.role = "controller"
        self.address = ("127.0.0.1", 1234)


class Harness:
    """A router, a registry, and a datapath stub that records its sends.

    The datapath's own send path is exercised by its own tests; what matters
    here is that the right body reaches the right client at the right moment.
    """

    def __init__(self, *clients: str) -> None:
        self.router = Router()
        self.registry = VideoRegistry()
        self.registry.attach_source_endpoint("10.0.0.5", 47810)
        self.sessions = [FakeSession(name) for name in clients]
        self.sent = Sent()

    def channel(self, bd_addr: str, regions: list[str], client: str | None = None,
                slot: int = 0) -> OutputChannel:
        channel = OutputChannel(
            bd_addr=bd_addr,
            hci_name="mock0",
            profile=create_profile("generic"),
            sink=MockSink(name=bd_addr),
            regions=list(regions),
        )
        if client is not None:
            channel.assigned_client = client
            channel.assigned_slot = slot
        self.router.add_channel(channel)
        return channel

    def report_layout(self, layout: str) -> bool:
        """What the video link does when a status arrives."""
        return self.registry.update_status_from_link(
            {"status": {"layout": {"mode": layout, "confidence": 0.9}}}
        )

    def broadcast(self) -> None:
        """What Datapath.broadcast_regions does, minus the socket."""
        layout = self.registry.layout
        for session in self.sessions:
            self.sent.messages.append((
                session.client_id,
                ControlOp.VIDEO_REGIONS,
                regions_message(self.router, session.client_id, layout),
            ))

    def crops_seen_by(self, client_id: str):
        """What that client's decoder would end up cropping to."""
        from client.media.decoder import VideoDecoder

        message = self.sent.latest_regions(client_id)
        decoder = VideoDecoder.__new__(VideoDecoder)
        decoder._crops = ()
        decoder._graphs = {}
        decoder._viewport = None
        decoder.set_regions(message["crops"] if message else [])
        return decoder._crops


class TestAFourPlayerGame:
    @pytest.fixture()
    def game(self):
        harness = Harness("alice", "bob", "carol", "dave")
        harness.channel("00:00:00:00:00:01", [UPPER_LEFT, LEFT], "alice")
        harness.channel("00:00:00:00:00:02", [UPPER_RIGHT, RIGHT], "bob")
        harness.channel("00:00:00:00:00:03", [LOWER_LEFT], "carol")
        harness.channel("00:00:00:00:00:04", [LOWER_RIGHT], "dave")
        return harness

    def test_each_player_ends_up_with_their_own_quadrant(self, game):
        game.report_layout(QUAD_4)
        game.broadcast()

        assert game.crops_seen_by("alice") == ((0.0, 0.0, 0.5, 0.5),)
        assert game.crops_seen_by("bob") == ((0.5, 0.0, 0.5, 0.5),)
        assert game.crops_seen_by("carol") == ((0.0, 0.5, 0.5, 0.5),)
        assert game.crops_seen_by("dave") == ((0.5, 0.5, 0.5, 0.5),)

    def test_the_quadrants_tile_the_screen_exactly(self, game):
        """Four players, four quadrants, no overlap and no gap. A rounding
        error here would show one player a strip of the next one's game."""
        game.report_layout(QUAD_4)
        game.broadcast()

        area = 0.0
        for name in ("alice", "bob", "carol", "dave"):
            for _x, _y, w, h in game.crops_seen_by(name):
                area += w * h
        assert abs(area - 1.0) < 1e-9

    def test_full_screen_gives_everybody_the_whole_picture(self, game):
        game.report_layout(FULL)
        game.broadcast()

        for name in ("alice", "bob", "carol", "dave"):
            assert game.crops_seen_by(name) == ()


class TestLayoutTransitions:
    @pytest.fixture()
    def game(self):
        harness = Harness("alice", "bob")
        harness.channel("00:00:00:00:00:01", [UPPER_LEFT, LEFT], "alice")
        harness.channel("00:00:00:00:00:02", [LOWER_RIGHT, RIGHT], "bob")
        return harness

    def test_moving_to_a_split_crops_everybody(self, game):
        game.report_layout(FULL)
        game.broadcast()
        assert game.crops_seen_by("alice") == ()

        game.report_layout(VERTICAL_2)
        game.broadcast()
        assert game.crops_seen_by("alice") == ((0.0, 0.0, 0.5, 1.0),)
        assert game.crops_seen_by("bob") == ((0.5, 0.0, 0.5, 1.0),)

    def test_coming_back_to_full_screen_uncrops_everybody(self, game):
        """The direction that must never be missed: a client left cropped
        after the game went full-screen is watching a quarter of it, and
        nothing about the stream looks wrong."""
        game.report_layout(QUAD_4)
        game.broadcast()
        assert game.crops_seen_by("alice") != ()

        game.report_layout(FULL)
        game.broadcast()
        assert game.crops_seen_by("alice") == ()
        assert game.crops_seen_by("bob") == ()

    def test_a_layout_with_no_assignment_is_full_screen(self, game):
        """Neither player is assigned an `upper` or `lower`, so a stacked
        split leaves them both watching everything -- which is right: the
        alternative is showing them a half that means nothing."""
        game.report_layout(HORIZONTAL_2)
        game.broadcast()
        assert game.crops_seen_by("alice") == ()
        assert game.crops_seen_by("bob") == ()

    def test_only_a_real_change_is_worth_broadcasting(self, game):
        assert game.report_layout(QUAD_4) is True
        assert game.report_layout(QUAD_4) is False
        assert game.report_layout(VERTICAL_2) is True


class TestOnePlayerWithSeveralControllers:
    def test_two_contiguous_quadrants_become_one_crop(self):
        harness = Harness("alice", "bob")
        harness.channel("00:00:00:00:00:01", [UPPER_LEFT], "alice", slot=0)
        harness.channel("00:00:00:00:00:02", [LOWER_LEFT], "alice", slot=1)
        harness.channel("00:00:00:00:00:03", [UPPER_RIGHT], "bob")
        harness.report_layout(QUAD_4)
        harness.broadcast()

        assert harness.crops_seen_by("alice") == ((0.0, 0.0, 0.5, 1.0),)

    def test_two_opposite_quadrants_stay_two_crops(self):
        """The safety property, through the whole chain. The bounding box of
        these two is the entire screen, and taking it would show this player
        both opponents -- while looking like a perfectly ordinary picture."""
        harness = Harness("alice", "bob", "carol")
        harness.channel("00:00:00:00:00:01", [UPPER_LEFT], "alice", slot=0)
        harness.channel("00:00:00:00:00:02", [LOWER_RIGHT], "alice", slot=1)
        harness.channel("00:00:00:00:00:03", [UPPER_RIGHT], "bob")
        harness.channel("00:00:00:00:00:04", [LOWER_LEFT], "carol")
        harness.report_layout(QUAD_4)
        harness.broadcast()

        crops = harness.crops_seen_by("alice")
        assert len(crops) == 2
        assert set(crops) == {(0.0, 0.0, 0.5, 0.5), (0.5, 0.5, 0.5, 0.5)}
        # And neither of them touches the two quadrants alice does not own.
        for x, y, w, h in crops:
            assert (w, h) == (0.5, 0.5)

    def test_losing_one_controller_narrows_the_crop(self):
        harness = Harness("alice")
        harness.channel("00:00:00:00:00:01", [UPPER_LEFT], "alice", slot=0)
        second = harness.channel("00:00:00:00:00:02", [LOWER_LEFT], "alice", slot=1)
        harness.report_layout(QUAD_4)
        harness.broadcast()
        assert harness.crops_seen_by("alice") == ((0.0, 0.0, 0.5, 1.0),)

        second.assigned_client = None
        second.assigned_slot = None
        harness.broadcast()
        assert harness.crops_seen_by("alice") == ((0.0, 0.0, 0.5, 0.5),)


class TestReconnectAndLoss:
    def test_a_reconnecting_client_is_told_immediately(self):
        """It is sent on session creation, so a client that drops mid-game
        comes back cropped rather than watching everything until somebody
        else's assignment happens to change."""
        harness = Harness("alice")
        harness.channel("00:00:00:00:00:01", [UPPER_LEFT], "alice")
        harness.report_layout(QUAD_4)

        # What _on_session_created does for one session.
        message = regions_message(harness.router, "alice", harness.registry.layout)
        assert message["crops"] == [{"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}]

    def test_losing_the_video_source_uncrops_everybody(self):
        """A layout held over from a dead source would crop every client to a
        division of a picture that no longer exists."""
        harness = Harness("alice")
        harness.channel("00:00:00:00:00:01", [UPPER_LEFT], "alice")
        harness.report_layout(QUAD_4)
        harness.broadcast()
        assert harness.crops_seen_by("alice") != ()

        harness.registry.detach_source("video-link")
        harness.broadcast()
        assert harness.crops_seen_by("alice") == ()

    def test_a_client_that_owns_nothing_is_still_told(self):
        """Silence cannot tell a client that *was* cropping to stop."""
        harness = Harness("alice", "bob")
        harness.channel("00:00:00:00:00:01", [UPPER_LEFT], "alice")
        harness.report_layout(QUAD_4)
        harness.broadcast()

        assert harness.sent.latest_regions("bob") == {
            "layout": QUAD_4, "regions": [], "crops": []
        }


class TestTheFeatureIsInertWhenOff:
    def test_detection_off_means_no_layout_and_no_crops(self):
        """The server never hears a layout, so everything reads FULL and
        every client draws the whole picture -- exactly what it did before
        any of this existed."""
        harness = Harness("alice")
        harness.channel("00:00:00:00:00:01", [UPPER_LEFT, LEFT], "alice")
        # No status carrying a layout, which is what a source with detection
        # switched off sends.
        harness.registry.update_status_from_link({"status": {"streaming": True}})
        harness.broadcast()

        assert harness.registry.layout == FULL
        assert harness.crops_seen_by("alice") == ()

    def test_an_unassigned_adapter_costs_nothing(self):
        harness = Harness("alice")
        harness.channel("00:00:00:00:00:01", [], "alice")
        harness.report_layout(QUAD_4)
        harness.broadcast()
        assert harness.crops_seen_by("alice") == ()


class TestAssignmentsPersistAcrossTheChain:
    def test_a_region_set_in_the_config_reaches_the_client(self):
        """The join phases 5 and 6 make: config -> channel -> message ->
        decoder. Each link is tested alone; this is the one that fails if two
        of them disagree about the shape."""
        config = ServerConfig(password="secret")
        config.set_adapter_regions("00:00:00:00:00:01", [LOWER_RIGHT])

        harness = Harness("alice")
        harness.channel(
            "00:00:00:00:00:01",
            config.adapter("00:00:00:00:00:01").regions,
            "alice",
        )
        harness.report_layout(QUAD_4)
        harness.broadcast()

        assert harness.crops_seen_by("alice") == ((0.5, 0.5, 0.5, 0.5),)

"""The whole chain, from a frame to what one client is told to draw.

Each side of this looks correct on its own -- that is the failure mode the
video end-to-end test already exists for. This drives the real modules in
order: a real frame through the real vision service, the real wire codec, the
real registry, the real join, and the real client-side decode.

The scenarios are the ones asked for that are reachable without a console:
feature off, server on and client off, split screen, shared screen, one client
with several controllers, a player leaving, a source going quiet, and a layout
change mid-session.
"""

from __future__ import annotations

import pytest

from common.player_labels import decode_labels, encode_labels, encode_tracks
from common.screen_regions import FULL, QUAD_4, VERTICAL_2, Rect
from common.video import VideoSettings

from server import player_overlay
from server.video import TRACKS_STALE_NS, VideoRegistry
from videoserver.playervision.service import PlayerVisionService
from videoserver.playervision.types import PlayerHint

av = pytest.importorskip("av")

WIDTH, HEIGHT = 640, 360


@pytest.fixture(autouse=True)
def _stand_in_detector():
    """The tests' own detector: finds the bright squares these frames draw.

    There is no model-free backend in the product any more, and a model needs
    an optional extra and a download. Everything this file checks happens
    after detection, so a stand-in is enough to exercise all of it.
    """
    from tests.playervision_fakes import BrightBoxBackend, registered

    with registered(BrightBoxBackend):
        yield


def frame_with(*squares, size=44):
    """A yuv420p frame with a bright square at each normalised centre."""
    picture = av.VideoFrame(WIDTH, HEIGHT, "yuv420p")
    plane = picture.planes[0]
    stride = plane.line_size
    buf = bytearray([40]) * plane.buffer_size

    boxes = []
    for cx, cy in squares:
        x = int(cx * WIDTH) - size // 2
        y = int(cy * HEIGHT) - size // 2
        boxes.append((max(0, x), max(0, y)))
    for x, y in boxes:
        for row in range(y, min(HEIGHT, y + size)):
            base = row * stride
            for col in range(x, min(WIDTH, x + size)):
                buf[base + col] = 220
    plane.update(bytes(buf))
    for other in picture.planes[1:]:
        other.update(bytes(bytearray([128]) * other.buffer_size))
    return picture


class FakeChannel:
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


def quad_router():
    return FakeRouter(
        FakeChannel(1, client="c1", username="Alex", regions=["upper_left"]),
        FakeChannel(2, client="c2", username="Bo", regions=["upper_right"]),
        FakeChannel(3, client="c3", username="Cass", regions=["lower_left"]),
        FakeChannel(4, client="c4", username="Dee", regions=["lower_right"]),
    )


def run_source(layout, hints, centres, *, settings=None, steps=8, drift=0.012):
    """Drive the real vision service until it has something to say.

    Returns the rows it published. The squares drift so the background model
    keeps seeing them as foreground, which is what a character standing on a
    scrolling stage does anyway.
    """
    settings = settings or VideoSettings(
        player_id_enabled=True, player_id_backend="auto", player_id_hz=1000.0
    )
    service = PlayerVisionService()
    service.configure(layout=layout, hints=hints)

    now = 1_000_000_000
    rows = []
    for step in range(steps):
        now += 100_000_000
        moved = [(cx + step * drift, cy) for cx, cy in centres]
        rows = service.sample(frame_with(*moved), settings, True, now) or []
    return rows, service


def deliver(router, client_id, rows, layout):
    """Everything between the source and the client, in order."""
    registry = VideoRegistry()
    registry.update_tracks(encode_tracks(rows, layout))
    live_layout, tracks = registry.tracks
    body = player_overlay.labels_for_client(router, client_id, live_layout, tracks)
    return decode_labels(encode_labels(body))


class TestFeatureOff:
    def test_the_source_publishes_nothing(self):
        """Scenario 1. Not 'publishes empty' -- does not run at all."""
        service = PlayerVisionService()
        assert service.sample(frame_with((0.25, 0.25)), VideoSettings(), True, 1) is None
        assert service.running is False

    def test_the_capture_machine_can_refuse_independently(self):
        on = VideoSettings(player_id_enabled=True, player_id_backend="auto")
        service = PlayerVisionService()
        assert service.sample(frame_with((0.25, 0.25)), on, False, 1) is None
        assert service.running is False


class TestSplitScreen:
    def test_four_players_each_see_the_other_three(self):
        """Scenarios 3 and 6, through the real detector and the real wire."""
        hints = (
            PlayerHint(1, ("upper_left",)), PlayerHint(2, ("upper_right",)),
            PlayerHint(3, ("lower_left",)), PlayerHint(4, ("lower_right",)),
        )
        rows, _ = run_source(
            QUAD_4, hints, [(0.25, 0.25), (0.72, 0.25), (0.25, 0.72), (0.72, 0.72)]
        )
        identified = {row.player_id for row in rows if row.identified}
        assert identified == {1, 2, 3, 4}, f"the source identified {identified}"

        router = quad_router()
        for client, own in (("c1", 1), ("c2", 2), ("c3", 3), ("c4", 4)):
            layout, labels = deliver(router, client, rows, QUAD_4)
            assert layout == QUAD_4
            seen = {label["player_id"] for label in labels}
            assert own not in seen, f"{client} was shown their own name"
            assert seen == {1, 2, 3, 4} - {own}, client

    def test_two_player_vertical(self):
        """Scenario 5."""
        hints = (PlayerHint(1, ("left",)), PlayerHint(2, ("right",)))
        rows, _ = run_source(VERTICAL_2, hints, [(0.25, 0.5), (0.75, 0.5)])
        assert {r.player_id for r in rows if r.identified} == {1, 2}

        router = FakeRouter(
            FakeChannel(1, client="c1", username="Alex", regions=["left"]),
            FakeChannel(2, client="c2", username="Bo", regions=["right"]),
        )
        _, labels = deliver(router, "c1", rows, VERTICAL_2)
        assert [label["player_id"] for label in labels] == [2]
        assert labels[0]["name"] == "Bo"

    def test_the_label_lands_on_the_other_players_half(self):
        """A label whose coordinates were wrong would still pass a
        who-sees-what test, and would be drawn over the wrong character."""
        hints = (PlayerHint(1, ("left",)), PlayerHint(2, ("right",)))
        rows, _ = run_source(VERTICAL_2, hints, [(0.25, 0.5), (0.75, 0.5)])
        router = FakeRouter(
            FakeChannel(1, client="c1", username="Alex", regions=["left"]),
            FakeChannel(2, client="c2", username="Bo", regions=["right"]),
        )
        _, labels = deliver(router, "c1", rows, VERTICAL_2)
        label = labels[0]
        centre_x = label["x"] + label["w"] / 2
        assert centre_x > 0.5, "player 2's label is not on the right-hand half"


class TestOneClientManyControllers:
    def test_each_of_its_players_is_hidden_only_in_their_own_view(self):
        """Scenario 9, and the case the join exists for."""
        hints = (
            PlayerHint(1, ("upper_left",)), PlayerHint(2, ("upper_right",)),
            PlayerHint(3, ("lower_left",)), PlayerHint(4, ("lower_right",)),
        )
        rows, _ = run_source(
            QUAD_4, hints, [(0.25, 0.25), (0.72, 0.25), (0.25, 0.72), (0.72, 0.72)]
        )
        router = FakeRouter(
            FakeChannel(1, client="host", slot=0, username="Alex", regions=["upper_left"]),
            FakeChannel(2, client="host", slot=1, username="Bo", regions=["upper_right"]),
            FakeChannel(3, client="away", slot=0, username="Cass", regions=["lower_left"]),
            FakeChannel(4, client="away", slot=1, username="Dee", regions=["lower_right"]),
        )
        _, labels = deliver(router, "host", rows, QUAD_4)
        placed = {(label["player_id"], label["region"]) for label in labels}
        assert (1, "upper_left") not in placed
        assert (2, "upper_right") not in placed
        assert {3, 4} <= {label["player_id"] for label in labels}


class TestSharedScreen:
    def test_everybody_is_labelled_including_the_local_player(self):
        """Scenario 7. Explicitly the opposite of the split-screen rule: with
        one camera there is no 'your own view' to exclude yourself from, and
        being told which of four characters is which is the entire point."""
        router = quad_router()
        rows = [
            _row(track_id=10 + n, player_id=n, x=0.1 + 0.2 * n)
            for n in (1, 2, 3, 4)
        ]
        for client in ("c1", "c2", "c3", "c4"):
            _, labels = deliver(router, client, rows, FULL)
            assert {label["player_id"] for label in labels} == {1, 2, 3, 4}


class TestOnePlayer:
    def test_a_single_player_game_gets_no_labels(self):
        """Scenario 4. Answered from the controller registry, never inferred
        from the picture -- a shared-screen four-player game looks exactly
        like this one to a split-screen detector."""
        router = FakeRouter(
            FakeChannel(1, client="c1", username="Alex", regions=["upper_left"]),
            FakeChannel(2, regions=["upper_right"]),
        )
        rows = [_row(track_id=11, player_id=1, x=0.2)]
        _, labels = deliver(router, "c1", rows, FULL)
        assert labels == []


class TestDepartures:
    def test_a_player_who_leaves_loses_their_label(self):
        """Scenario 11. The source may still be reporting them."""
        router = quad_router()
        rows = [_row(track_id=11, player_id=3, x=0.2, region="lower_left")]
        assert deliver(router, "c1", rows, QUAD_4)[1]

        gone = FakeRouter(
            FakeChannel(1, client="c1", username="Alex", regions=["upper_left"]),
            FakeChannel(2, client="c2", username="Bo", regions=["upper_right"]),
        )
        assert deliver(gone, "c1", rows, QUAD_4)[1] == []

    def test_a_source_that_goes_quiet_takes_every_label_with_it(self):
        """Scenario 17, and the reason tracks carry their own staleness: a
        name nobody has renewed is a claim to withdraw, not to keep drawing."""
        registry = VideoRegistry()
        registry.update_tracks(
            encode_tracks([_row(track_id=11, player_id=2, x=0.7)], FULL)
        )
        assert registry.tracks[1]

        # Reach past the clock rather than sleeping two seconds.
        registry._tracks_ns -= TRACKS_STALE_NS + 1
        assert registry.tracks[1] == []

    def test_an_entity_that_disappears_stops_being_reported(self):
        """Scenario 12."""
        router = quad_router()
        assert deliver(router, "c1", [], QUAD_4)[1] == []


class TestLayoutChange:
    def test_labels_follow_the_layout(self):
        """Scenario 19. The same track, with the picture divided differently,
        must be excluded from a different client."""
        router = FakeRouter(
            FakeChannel(1, client="c1", username="Alex", regions=["upper_left", "left"]),
            FakeChannel(2, client="c2", username="Bo", regions=["upper_right", "right"]),
        )
        quad = [_row(track_id=11, player_id=1, x=0.2, y=0.2, region="upper_left")]
        assert deliver(router, "c1", quad, QUAD_4)[1] == []
        assert deliver(router, "c2", quad, QUAD_4)[1]

        vertical = [_row(track_id=11, player_id=1, x=0.2, y=0.5, region="left")]
        assert deliver(router, "c1", vertical, VERTICAL_2)[1] == []
        assert deliver(router, "c2", vertical, VERTICAL_2)[1]

    def test_a_menu_is_a_shared_screen_and_shows_everybody(self):
        """Scenario 20: gameplay to a full-screen menu and back. The layout
        going FULL is what the detector reports for a menu, and everybody
        should then see every label."""
        router = quad_router()
        rows = [_row(track_id=10 + n, player_id=n, x=0.1 + 0.2 * n) for n in (1, 2)]
        _, labels = deliver(router, "c1", rows, FULL)
        assert {label["player_id"] for label in labels} == {1, 2}


class TestUncertainty:
    def test_an_unidentified_entity_is_never_labelled(self):
        """Scenarios 13 and 14 at the wire level: the source publishes the
        track so its debug view can show it, and no name goes out."""
        router = quad_router()
        rows = [_row(track_id=11, player_id=0, x=0.5, confidence=0.0)]
        assert deliver(router, "c1", rows, FULL)[1] == []


def _row(*, track_id, player_id, x, y=0.5, region="", confidence=0.9):
    class Row:
        pass

    row = Row()
    row.track_id = track_id
    row.player_id = player_id
    row.box = Rect(x, y, 0.08, 0.16)
    row.confidence = confidence
    row.region = region
    row.source = "viewport"
    row.identified = player_id != 0
    return row

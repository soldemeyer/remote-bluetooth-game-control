"""The four player-identification messages, and what they must survive.

Two kinds of test here. The first is that everything round-trips -- and the
region-code table earns its own class, because the first attempt derived the
codes from the region names and ``lower`` and ``left`` both gave ``l``, so one
decoded as the other. A label silently attributed to the wrong half of the
screen is exactly the leak this feature is built to avoid, and it was caught
by round-tripping the vocabulary rather than by reading the code.

The second is that nothing raises. These bodies arrive over the network, and
the label one is decoded on the client's 500 Hz input-loop thread: an
exception there does not cost a label, it costs the controller.
"""

from __future__ import annotations

import json

from common import protocol
from common.player_labels import (
    MAX_LABELS,
    MAX_TRACKS,
    REGION_CODES,
    SCALE,
    decode_labels,
    decode_player_map,
    decode_tracks,
    decode_traces,
    encode_labels,
    encode_player_map,
    encode_tracks,
    encode_traces,
)
from common.screen_regions import FULL, QUAD_4, REGIONS, Rect


class _Row:
    """TrackedPlayer-shaped. Duck-typed on purpose -- common/ must not import
    from videoserver/."""

    def __init__(self, track_id, player_id, box, region, confidence=0.9,
                 source="viewport"):
        self.track_id = track_id
        self.player_id = player_id
        self.box = box
        self.region = region
        self.confidence = confidence
        self.source = source


class _Trace:
    def __init__(self, player_id, samples, hz=20.0):
        self.player_id = player_id
        self.samples = samples
        self.hz = hz


def _rows(count=4):
    cells = ("upper_left", "upper_right", "lower_left", "lower_right")
    return [
        _Row(17 + i, i + 1, Rect(0.531, 0.382, 0.104, 0.221), cells[i])
        for i in range(count)
    ]


def _labels(count=4):
    cells = ("upper_left", "upper_right", "lower_left", "lower_right")
    names = ("Alexander", "Bo", "Cassandra", "Dee")
    return {
        "layout": QUAD_4,
        "labels": [
            {"p": i + 1, "n": names[i], "t": 17 + i, "r": cells[i],
             "x": 0.53, "y": 0.38, "w": 0.1, "h": 0.22, "c": 0.94}
            for i in range(count)
        ],
    }


def _wire_bytes(op, body):
    return len(json.dumps({"op": op, **body}, separators=(",", ":")).encode())


class TestRegionCodes:
    def test_every_region_has_one(self):
        assert set(REGION_CODES) == set(REGIONS)

    def test_no_two_regions_share_a_code(self):
        """The bug this table exists for. Derived initials gave `lower` and
        `left` both `l`, so one decoded as the other -- a label attributed to
        the wrong half of the screen, silently."""
        assert len(set(REGION_CODES.values())) == len(REGION_CODES)

    def test_every_region_round_trips(self):
        for name in REGIONS:
            row = _Row(1, 1, Rect(0.1, 0.1, 0.1, 0.1), name)
            _, _, back = decode_tracks(encode_tracks([row], QUAD_4))
            assert back[0]["r"] == name, name

    def test_an_unknown_code_decodes_to_no_region(self):
        """Fails open: a label with no region is shown everywhere rather than
        excluded from the wrong viewport."""
        _, back = decode_labels({"l": QUAD_4, "b": [[1, 1, "zz", 0, 0, 1, 1, 90]], "n": {}})
        assert back[0]["region"] == ""


class TestTracks:
    def test_it_round_trips(self):
        layout, pts, back = decode_tracks(encode_tracks(_rows(), QUAD_4, 18374621))
        assert layout == QUAD_4
        assert pts == 18374621
        assert len(back) == 4
        assert back[0] == {
            "t": 17, "p": 1, "r": "upper_left",
            "x": 0.531, "y": 0.382, "w": 0.104, "h": 0.221,
            "c": 0.9, "s": "viewport",
        }

    def test_four_players_fit_with_room_to_spare(self):
        """`encode_control` refuses an oversized message whole rather than
        truncating it, so headroom is the thing to assert -- not that today's
        message happens to fit."""
        size = _wire_bytes("video_tracks", encode_tracks(_rows(), QUAD_4, 1 << 40))
        assert size < protocol.MAX_DATAGRAM - 5
        assert protocol.MAX_DATAGRAM - 5 - size > 500, f"only {size} B used but little spare"

    def test_the_track_count_is_capped(self):
        rows = _rows(1) * 100
        body = encode_tracks(rows, QUAD_4)
        assert len(body["t"]) == MAX_TRACKS

    def test_a_crowd_of_unnamed_rows_never_pushes_out_a_name(self):
        """A split scanned one viewport at a time publishes around thirty
        rows, most unnamed. In the order found, names fell past the cap:
        measured, 12 tracks reached the Bluetooth server with 2 names while
        the source's own preview showed six."""
        unnamed = [
            _Row(100 + i, 0, Rect(0.01 * i, 0.5, 0.05, 0.05), "lower_left", 0.0, "none")
            for i in range(30)
        ]
        named = [
            _Row(1, 1, Rect(0.2, 0.2, 0.1, 0.1), "upper_left", 0.92, "viewport"),
            _Row(2, 2, Rect(0.7, 0.2, 0.1, 0.1), "upper_right", 0.92, "viewport"),
            _Row(3, 1, Rect(0.6, 0.1, 0.1, 0.1), "upper_right", 0.99, "appearance"),
            _Row(4, 2, Rect(0.4, 0.3, 0.1, 0.1), "upper_left", 0.93, "appearance"),
            _Row(5, 1, Rect(0.3, 0.7, 0.1, 0.1), "lower_left", 0.97, "appearance"),
            _Row(6, 2, Rect(0.45, 0.7, 0.1, 0.1), "lower_left", 0.70, "continuity"),
        ]
        body = encode_tracks(unnamed + named, QUAD_4)
        _layout, _pts, tracks = decode_tracks(body)
        assert sorted(t["t"] for t in tracks if t["p"]) == [1, 2, 3, 4, 5, 6]
        assert len(tracks) == MAX_TRACKS

    def test_there_is_room_for_four_players_in_four_viewports(self):
        assert MAX_TRACKS >= 16

    def test_even_a_full_message_fits(self):
        rows = [
            _Row(60000 + i, 4, Rect(0.9999, 0.9999, 0.9999, 0.9999),
                 "lower_right", 1.0, "appearance")
            for i in range(MAX_TRACKS)
        ]
        assert _wire_bytes("video_tracks", encode_tracks(rows, QUAD_4, 1 << 46)) < protocol.MAX_DATAGRAM - 5

    def test_an_unidentified_row_survives(self):
        """The source publishes these so its own debug view can show that
        something is there."""
        row = _Row(9, 0, Rect(0.1, 0.1, 0.1, 0.1), "", 0.0, "none")
        _, _, back = decode_tracks(encode_tracks([row], FULL))
        assert back[0]["p"] == 0


class TestLabels:
    def test_it_round_trips(self):
        layout, back = decode_labels(encode_labels(_labels()))
        assert layout == QUAD_4
        assert back[0] == {
            "player_id": 1, "track_id": 17, "name": "Alexander",
            "region": "upper_left", "x": 0.53, "y": 0.38,
            "w": 0.1, "h": 0.22, "confidence": 0.94,
        }

    def test_a_name_travels_once_however_often_a_player_appears(self):
        """A player visible in two viewports would otherwise carry their name
        twice in a message with a hard ceiling."""
        body = _labels(1)
        body["labels"].append(dict(body["labels"][0], t=99, r="lower_right"))
        encoded = encode_labels(body)
        assert len(encoded["n"]) == 1
        assert len(encoded["b"]) == 2

    def test_four_labels_fit_with_room_to_spare(self):
        size = _wire_bytes("player_labels", encode_labels(_labels()))
        assert protocol.MAX_DATAGRAM - 5 - size > 500

    def test_a_full_message_of_long_names_still_fits(self):
        """Names come from clients and are not ours to trust for length."""
        body = {
            "layout": QUAD_4,
            "labels": [
                {"p": i + 1, "n": "W" * 40, "t": 60000 + i, "r": "lower_right",
                 "x": 0.9999, "y": 0.9999, "w": 0.9999, "h": 0.9999, "c": 1.0}
                for i in range(MAX_LABELS)
            ],
        }
        assert _wire_bytes("player_labels", encode_labels(body)) < protocol.MAX_DATAGRAM - 5

    def test_the_label_count_is_capped(self):
        body = _labels(1)
        body["labels"] = body["labels"] * 50
        assert len(encode_labels(body)["b"]) <= MAX_LABELS

    def test_an_empty_message_is_still_a_message(self):
        """A client that was drawing labels has to be told to stop."""
        layout, back = decode_labels(encode_labels({"layout": QUAD_4, "labels": []}))
        assert layout == QUAD_4 and back == []


class TestPrecision:
    def test_a_coordinate_survives_to_better_than_a_pixel(self):
        """1/SCALE is 0.19 px at 1920 wide."""
        row = _Row(1, 1, Rect(0.12345, 0.6789, 0.5, 0.5), "upper_left")
        _, _, back = decode_tracks(encode_tracks([row], QUAD_4))
        assert abs(back[0]["x"] - 0.12345) <= 1.0 / SCALE
        assert abs(back[0]["y"] - 0.6789) <= 1.0 / SCALE

    def test_out_of_range_coordinates_clamp(self):
        row = _Row(1, 1, Rect(-0.5, 1.5, 2.0, 2.0), "upper_left")
        _, _, back = decode_tracks(encode_tracks([row], QUAD_4))
        assert back[0]["x"] == 0.0
        assert back[0]["y"] == 1.0


class TestMalformed:
    """None of these may raise. The label decoder runs on the 500 Hz loop."""

    def test_a_body_that_is_not_a_dict_shaped_message(self):
        assert decode_tracks({}) == (FULL, 0, [])
        assert decode_labels({}) == (FULL, [])
        assert decode_player_map({}) == []
        assert decode_traces({}) == []

    def test_rows_that_are_not_lists(self):
        assert decode_tracks({"l": QUAD_4, "t": ["x", 5, None, {}]})[2] == []
        assert decode_labels({"l": QUAD_4, "b": ["x", 5, None, {}]})[1] == []

    def test_rows_that_are_too_short(self):
        assert decode_tracks({"l": QUAD_4, "t": [[1, 2, "ul"]]})[2] == []
        assert decode_labels({"l": QUAD_4, "b": [[1, 2]]})[1] == []

    def test_fields_of_the_wrong_type(self):
        assert decode_tracks({"l": QUAD_4, "t": [["a", "b", 3, "c", "d", "e", "f", "g"]]})[2] == []
        assert decode_labels({"l": QUAD_4, "b": [["a", "b", 3, "c", "d", "e", "f", "g"]], "n": {}})[1] == []

    def test_a_names_table_of_the_wrong_type(self):
        _, back = decode_labels({"l": QUAD_4, "b": [[1, 1, "ul", 0, 0, 1, 1, 90]], "n": "nope"})
        assert back[0]["name"] == ""

    def test_a_nonsense_layout_falls_back_to_full(self):
        assert decode_tracks({"l": "QUAD_5", "t": []})[0] == FULL
        assert decode_labels({"l": 7, "b": []})[0] == FULL

    def test_an_oversized_list_is_bounded_not_refused(self):
        rows = [[i, 1, "ul", 0, 0, 1, 1, 90] for i in range(1000)]
        assert len(decode_tracks({"l": QUAD_4, "t": rows})[2]) == MAX_TRACKS


class TestPlayerMap:
    def test_it_round_trips(self):
        hints = [{"id": 1, "r": ["upper_left", "left"]}, {"id": 2, "r": ["right"]}]
        assert decode_player_map(encode_player_map(hints)) == [
            (1, ("upper_left", "left")),
            (2, ("right",)),
        ]

    def test_it_carries_no_names(self):
        """In external mode the capture machine belongs to somebody else."""
        encoded = encode_player_map([{"id": 1, "r": ["upper_left"], "name": "Alex"}])
        assert "Alex" not in json.dumps(encoded)

    def test_an_unnumbered_player_is_left_out(self):
        assert decode_player_map(encode_player_map([{"id": 0, "r": ["left"]}])) == []

    def test_it_fits_easily(self):
        hints = [{"id": i, "r": ["upper_left", "left"]} for i in range(1, 5)]
        assert _wire_bytes("player_map", encode_player_map(hints)) < 200


class TestTraces:
    def test_it_round_trips_to_within_a_step(self):
        samples = tuple((0.5, -0.5) for _ in range(24))
        back = decode_traces(encode_traces([_Trace(1, samples)]))
        assert len(back) == 1
        player_id, hz, pairs = back[0]
        assert player_id == 1 and hz == 20.0 and len(pairs) == 24
        assert abs(pairs[0][0] - 0.5) < 1.0 / 127
        assert abs(pairs[0][1] + 0.5) < 1.0 / 127

    def test_four_players_of_a_full_second_fit(self):
        traces = [
            _Trace(i, tuple((1.0, -1.0) for _ in range(24))) for i in range(1, 5)
        ]
        size = _wire_bytes("video_player_input", encode_traces(traces))
        assert size < protocol.MAX_DATAGRAM - 5, size

    def test_the_sample_count_is_capped(self):
        trace = _Trace(1, tuple((0.1, 0.1) for _ in range(500)))
        assert len(decode_traces(encode_traces([trace]))[0][2]) <= 24

    def test_an_empty_trace_is_omitted(self):
        assert decode_traces(encode_traces([_Trace(1, ())])) == []

    def test_an_odd_length_sample_list_does_not_raise(self):
        assert decode_traces({"hz": 20, "p": {"1": [1, 2, 3]}})[0][2] == ((1 / 127, 2 / 127),)

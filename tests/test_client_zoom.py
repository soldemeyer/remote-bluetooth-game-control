"""The camera moving between views, rather than cutting.

When the layout changes, a player's picture used to snap from one crop to
another. It now travels: the decoder renders the **union** of the old and new
views and reports a sub-rectangle of it that walks from one to the other, and
the window presents that rectangle scaled to the window.

Rendering the union rather than an interpolating crop is the load-bearing
decision. A filter graph is cached by its crop, so a crop that changes every
frame would build and throw away a graph per frame -- which is the expensive
thing in this path. One union means one graph for the whole move.

The move is **not** restricted to views that nest. Sweeping from one quadrant
to the opposite one passes over the two in between, and the operator asked for
that after it was put to them; the alternative offered was cutting for those
cases. Worth knowing when reading the safety notes elsewhere in this feature,
which are otherwise absolute.
"""

from __future__ import annotations

import time

import pytest

av = pytest.importorskip("av", reason="video extras not installed")

from client.media.decoder import (  # noqa: E402
    TRANSITION_NS,
    VideoDecoder,
    _eased,
    _lerp_rect,
    _union_rect,
)

WHOLE = (0.0, 0.0, 1.0, 1.0)
UPPER_LEFT = (0.0, 0.0, 0.5, 0.5)
LOWER_RIGHT = (0.5, 0.5, 0.5, 0.5)
LEFT_HALF = (0.0, 0.0, 0.5, 1.0)


class FakeReceiver:
    decode_stats = None

    def request_idr(self):
        pass


def frame(width=1280, height=720):
    from av.video.frame import VideoFrame

    picture = VideoFrame(width, height, "yuv420p")
    for plane in picture.planes:
        plane.update(bytes(plane.buffer_size))
    return picture


def decoder(viewport=(1280, 720)):
    decode = VideoDecoder(FakeReceiver())
    decode.set_viewport(*viewport)
    return decode


def publish(decode, picture):
    decode._publish(picture, 0, time.perf_counter_ns())
    return decode.latest()


def settle(decode, picture, limit=200):
    for _ in range(limit):
        publish(decode, picture)
        if decode.latest().zoom is None:
            return decode.latest()
        time.sleep(0.01)
    raise AssertionError("the camera never came to rest")


class TestTheMaths:
    """Pure, so these say what the move is without needing a decoder."""

    def test_easing_starts_and_ends_still(self):
        """A camera that starts and stops abruptly reads as a glitch even
        when the middle of the move is smooth."""
        assert _eased(0) == 0.0
        assert _eased(TRANSITION_NS) == 1.0
        assert _eased(TRANSITION_NS * 2) == 1.0
        # Smoothstep: slow at both ends, fastest in the middle.
        assert _eased(int(TRANSITION_NS * 0.1)) < 0.1
        assert _eased(int(TRANSITION_NS * 0.9)) > 0.9
        assert _eased(int(TRANSITION_NS * 0.5)) == pytest.approx(0.5)

    def test_easing_is_monotonic(self):
        seen = [_eased(int(TRANSITION_NS * t / 20)) for t in range(21)]
        assert seen == sorted(seen)

    def test_the_union_covers_both_views(self):
        union = _union_rect(UPPER_LEFT, LOWER_RIGHT)
        assert union == (0.0, 0.0, 1.0, 1.0)
        for rect in (UPPER_LEFT, LOWER_RIGHT):
            assert rect[0] >= union[0]
            assert rect[1] >= union[1]
            assert rect[0] + rect[2] <= union[0] + union[2]
            assert rect[1] + rect[3] <= union[1] + union[3]

    def test_a_nested_move_needs_no_more_than_the_larger_view(self):
        """Zooming within one half must not drag the whole picture into the
        union -- that would render four times the pixels for nothing."""
        assert _union_rect(UPPER_LEFT, LEFT_HALF) == LEFT_HALF

    def test_the_ends_are_exact(self):
        assert _lerp_rect(UPPER_LEFT, LOWER_RIGHT, 0.0) == UPPER_LEFT
        assert _lerp_rect(UPPER_LEFT, LOWER_RIGHT, 1.0) == LOWER_RIGHT


class TestItMoves:
    @pytest.mark.parametrize(
        ("label", "start", "end"),
        [
            ("full to a quadrant", [], [UPPER_LEFT]),
            ("a quadrant to full", [UPPER_LEFT], []),
            ("a quadrant to the half holding it", [UPPER_LEFT], [LEFT_HALF]),
            ("across to the opposite quadrant", [UPPER_LEFT], [LOWER_RIGHT]),
        ],
    )
    def test_the_camera_travels(self, label, start, end):
        picture = frame()
        decode = decoder()
        decode.set_regions(start)
        settle(decode, picture)

        decode.set_regions(end)
        assert publish(decode, picture).zoom is not None, label

    def test_it_comes_to_rest_on_the_cheap_path(self):
        """The move is the expensive path -- the window scales rather than
        blits. It must hand back."""
        picture = frame()
        decode = decoder()
        decode.set_regions([])
        settle(decode, picture)

        decode.set_regions([UPPER_LEFT])
        final = settle(decode, picture)
        assert final.zoom is None
        assert len(final.views) == 1

    def test_it_lands_exactly_on_the_new_view(self):
        picture = frame()
        decode = decoder()
        decode.set_regions([])
        settle(decode, picture)
        decode.set_regions([UPPER_LEFT])
        final = settle(decode, picture)

        # A quadrant of 1280x720 into a 1280x720 window: fills it.
        assert (final.views[0].width, final.views[0].height) == (1280, 720)

    def test_nothing_errors_along_the_way(self):
        picture = frame()
        decode = decoder()
        decode.set_regions([UPPER_LEFT])
        settle(decode, picture)
        decode.set_regions([LOWER_RIGHT])
        settle(decode, picture)
        assert decode.decode_errors == 0


class TestItDoesNotMoveWhenItCannot:
    def test_two_pieces_cut_instead(self):
        """A client showing two separate pieces has no single camera position,
        so there is nothing to interpolate."""
        picture = frame()
        decode = decoder()
        decode.set_regions([UPPER_LEFT])
        settle(decode, picture)

        decode.set_regions([UPPER_LEFT, LOWER_RIGHT])
        assert publish(decode, picture).zoom is None

    def test_leaving_two_pieces_cuts_too(self):
        picture = frame()
        decode = decoder()
        decode.set_regions([UPPER_LEFT, LOWER_RIGHT])
        settle(decode, picture)

        decode.set_regions([LEFT_HALF])
        assert publish(decode, picture).zoom is None

    def test_re_sending_the_same_regions_does_not_start_a_move(self):
        """The GUI re-applies the regions every tick, so a move on every tick
        would be a picture that never stops swimming."""
        picture = frame()
        decode = decoder()
        decode.set_regions([UPPER_LEFT])
        settle(decode, picture)

        for _ in range(5):
            decode.set_regions([UPPER_LEFT])
            assert publish(decode, picture).zoom is None


class TestAChangeMidMove:
    def test_it_continues_from_where_the_camera_is(self):
        """Restarting from the nominal old view would make the picture jump
        backwards before setting off again."""
        picture = frame()
        decode = decoder()
        decode.set_regions([])
        settle(decode, picture)

        decode.set_regions([UPPER_LEFT])
        publish(decode, picture)
        time.sleep(TRANSITION_NS / 1e9 * 0.4)
        publish(decode, picture)
        midway = decode._current_rect()
        assert midway is not None

        decode.set_regions([LOWER_RIGHT])
        started, start, end = decode._transition
        assert start == pytest.approx(midway, abs=0.05)
        assert end == LOWER_RIGHT

    def test_it_still_settles(self):
        picture = frame()
        decode = decoder()
        decode.set_regions([])
        settle(decode, picture)
        decode.set_regions([UPPER_LEFT])
        publish(decode, picture)
        decode.set_regions([LOWER_RIGHT])
        final = settle(decode, picture)
        assert final.zoom is None
        assert decode.decode_errors == 0


class TestCost:
    def test_moving_does_not_cost_much_more_than_resting(self):
        """The union is rendered larger than the window so the move lands at
        the settled picture's sharpness -- up to 2x per side for a quadrant.
        That is real work, and worth keeping an eye on, but it is off-GIL in
        swscale and lasts 400 ms.

        Generously bounded: this runs on whatever CI has, and the point is to
        catch a change of approach rather than to measure the machine.
        """
        picture = frame()
        decode = decoder()
        decode.set_regions([UPPER_LEFT])
        settle(decode, picture)

        started = time.perf_counter()
        for _ in range(30):
            publish(decode, picture)
        resting = (time.perf_counter() - started) / 30

        decode.set_regions([])
        started = time.perf_counter()
        count = 0
        while decode.latest().zoom is not None or count == 0:
            publish(decode, picture)
            count += 1
            if time.perf_counter() - started > 1.0:
                break
        moving = (time.perf_counter() - started) / count

        assert moving < resting * 4.0, (
            f"a moving frame cost {moving / resting:.1f}x a resting one; the "
            "move should render one union, not a fresh crop per frame"
        )

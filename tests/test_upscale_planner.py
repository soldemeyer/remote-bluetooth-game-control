"""The blit planner: which pixels go where, before any GPU is involved.

This is the seam that makes the GPU video path testable on a machine with no
graphics driver at all. ``client/media/planner.py`` decides *what* to upload
and *where* each piece lands; the native backends only copy pixels. So almost
everything worth getting right about cropping, split-screen layout and the
camera move can be checked here, in plain arithmetic.

The properties, not the numbers. A test that pins ``dst == (324, 364, 632,
356)`` breaks every time somebody changes a gutter; a test that pins "the odd
piece is centred" and "no piece leaves the composed picture" survives that and
still catches a real fault.

Two of these matter more than the rest:

* **the upload rectangle never exceeds what is shown** -- that is what makes
  "crop before you upscale" structural rather than a check somebody has to
  remember;
* **the software and GPU paths agree on every destination rectangle** -- they
  share ``compose`` precisely so a mode switch cannot move the picture, and
  this is what says so out loud.
"""

from __future__ import annotations

import pytest

from client.media.planner import (
    GUTTER_PX,
    TRANSITION_NS,
    WHOLE,
    Blit,
    compose,
    eased,
    plan_blits,
    union_rect,
)

FRAME = (1920, 1080)
VIEWPORT = (1280, 720)

UPPER_LEFT = (0.0, 0.0, 0.5, 0.5)
UPPER_RIGHT = (0.5, 0.0, 0.5, 0.5)
LOWER_LEFT = (0.0, 0.5, 0.5, 0.5)
LOWER_RIGHT = (0.5, 0.5, 0.5, 0.5)
LEFT_HALF = (0.0, 0.0, 0.5, 1.0)

#: Every arrangement a client can actually be asked to draw.
LAYOUTS = {
    "whole": (),
    "one quadrant": (UPPER_LEFT,),
    "one half": (LEFT_HALF,),
    "diagonal": (UPPER_LEFT, LOWER_RIGHT),
    "anti-diagonal": (UPPER_RIGHT, LOWER_LEFT),
    "three": (UPPER_LEFT, UPPER_RIGHT, LOWER_LEFT),
    "three, other corner": (UPPER_RIGHT, LOWER_LEFT, LOWER_RIGHT),
    "four": (UPPER_LEFT, UPPER_RIGHT, LOWER_LEFT, LOWER_RIGHT),
}


def plan(crops, transition=None, now=0, frame=FRAME, viewport=VIEWPORT):
    return plan_blits(crops, transition, frame[0], frame[1], viewport, now)


class TestTheUploadIsOnlyWhatIsShown:
    """The requirement that "upscaling must happen after the crop" turns into.

    Nothing checks this at render time; it is true because the planner never
    asks for more than the union of the visible pieces.
    """

    @pytest.mark.parametrize("name", sorted(LAYOUTS))
    def test_it_never_exceeds_the_frame(self, name):
        x, y, w, h = plan(LAYOUTS[name])[0]
        assert x >= 0 and y >= 0
        assert x + w <= FRAME[0]
        assert y + h <= FRAME[1]

    @pytest.mark.parametrize("name", sorted(LAYOUTS))
    def test_it_is_exactly_the_bounding_box_of_what_is_drawn(self, name):
        """Not larger -- a wasted upload -- and not smaller, which would be a
        piece sampling pixels that were never sent."""
        upload, blits, _, _, _ = plan(LAYOUTS[name])
        assert blits
        assert min(b.src[0] for b in blits) == pytest.approx(0.0, abs=2e-3)
        assert min(b.src[1] for b in blits) == pytest.approx(0.0, abs=2e-3)
        assert max(b.src[0] + b.src[2] for b in blits) == pytest.approx(1.0, abs=2e-3)
        assert max(b.src[1] + b.src[3] for b in blits) == pytest.approx(1.0, abs=2e-3)

    def test_a_single_quadrant_uploads_a_quarter_of_the_frame(self):
        """The case the whole feature exists for: a four-player split.

        1920x1080 decoded, one quadrant shown. Uploading the frame would cost
        four times the bytes and then throw three quarters of them away.
        """
        upload, blits, _, _, _ = plan((UPPER_LEFT,))
        assert upload == (0, 0, 960, 540)
        assert blits[0].src == (0.0, 0.0, 1.0, 1.0)

    def test_that_quadrant_is_drawn_larger_than_it_arrived(self):
        """i.e. there is something for an upscaler to do. 960x540 -> 1280x720."""
        upload, blits, _, _, _ = plan((UPPER_LEFT,))
        _, _, src_w, src_h = upload
        _, _, dst_w, dst_h = blits[0].dst
        assert dst_w > src_w and dst_h > src_h

    def test_a_half_uploads_a_half(self):
        assert plan((LEFT_HALF,))[0] == (0, 0, 960, 1080)


class TestChromaAlignment:
    """4:2:0 has one chroma sample per 2x2 luma block.

    An odd origin has no chroma sample to start from and an odd size leaves
    half a sample at the far edge. The software path's crop filter refuses
    outright; a texture upload does not, and produces a picture with colour
    fringing that reads as a bad capture.
    """

    @pytest.mark.parametrize("name", sorted(LAYOUTS))
    def test_the_upload_rectangle_is_even_on_every_side(self, name):
        x, y, w, h = plan(LAYOUTS[name])[0]
        assert x % 2 == 0 and y % 2 == 0
        assert w % 2 == 0 and h % 2 == 0

    @pytest.mark.parametrize("frame", [(1920, 1080), (1280, 720), (1366, 768), (641, 361)])
    def test_it_holds_for_odd_and_awkward_frame_sizes(self, frame):
        for crops in LAYOUTS.values():
            x, y, w, h = plan(crops, frame=frame)[0]
            assert (x % 2, y % 2, w % 2, h % 2) == (0, 0, 0, 0), (frame, crops)
            assert x + w <= frame[0] and y + h <= frame[1]


class TestEveryPieceStaysInsideThePicture:
    @pytest.mark.parametrize("name", sorted(LAYOUTS))
    def test_sources_are_normalised(self, name):
        for blit in plan(LAYOUTS[name])[1]:
            x, y, w, h = blit.src
            assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
            assert w > 0.0 and h > 0.0
            assert x + w <= 1.0 + 1e-9 and y + h <= 1.0 + 1e-9

    @pytest.mark.parametrize("name", sorted(LAYOUTS))
    def test_destinations_stay_inside_the_composed_picture(self, name):
        _, blits, composed_w, composed_h, _ = plan(LAYOUTS[name])
        for x, y, w, h in (b.dst for b in blits):
            assert x >= 0 and y >= 0
            assert x + w <= composed_w
            assert y + h <= composed_h

    @pytest.mark.parametrize("name", sorted(LAYOUTS))
    def test_the_composed_picture_fits_the_viewport(self, name):
        """It is presented 1:1 into the window, so anything larger would be
        scaled a second time."""
        _, _, composed_w, composed_h, _ = plan(LAYOUTS[name])
        assert composed_w <= VIEWPORT[0]
        assert composed_h <= VIEWPORT[1]

    @pytest.mark.parametrize("name", sorted(LAYOUTS))
    def test_pieces_never_overlap(self, name):
        """Two players' pictures on top of each other is the one arrangement
        that is worse than showing the wrong crop."""
        boxes = [b.dst for b in plan(LAYOUTS[name])[1]]
        for index, (ax, ay, aw, ah) in enumerate(boxes):
            for bx, by, bw, bh in boxes[index + 1:]:
                apart = (
                    ax + aw <= bx or bx + bw <= ax
                    or ay + ah <= by or by + bh <= ay
                )
                assert apart, f"{name}: {boxes}"


class TestTheGutter:
    def test_adjacent_pieces_are_separated_by_exactly_the_gutter(self):
        """Not decoration: two unrelated viewports butted together read as one
        picture with a seam, which is the thing the detector hunts for."""
        _, blits, _, _, _ = plan((UPPER_LEFT, UPPER_RIGHT, LOWER_LEFT))
        top = min(b.dst[1] for b in blits)
        row = sorted((b for b in blits if b.dst[1] == top), key=lambda b: b.dst[0])
        assert len(row) == 2
        left, right = row
        # Cells abut across the gutter; each piece is then centred in its cell,
        # so the gap between the drawn pieces is at least the gutter.
        gap = right.dst[0] - (left.dst[0] + left.dst[2])
        assert gap >= GUTTER_PX

    def test_rows_are_separated_too(self):
        _, blits, _, _, _ = plan((UPPER_LEFT, UPPER_RIGHT, LOWER_LEFT))
        top = min(b.dst[1] + b.dst[3] for b in blits)
        bottom = max(b.dst[1] for b in blits)
        assert bottom - top >= GUTTER_PX


class TestThreePiecesAreATriangle:
    """The layout this work was asked for, restated at the planner level."""

    CROPS = (UPPER_LEFT, UPPER_RIGHT, LOWER_LEFT)

    def test_all_three_are_the_same_size(self):
        blits = plan(self.CROPS)[1]
        assert len({(b.dst[2], b.dst[3]) for b in blits}) == 1

    def test_two_on_top_one_below(self):
        blits = plan(self.CROPS)[1]
        rows = sorted({b.dst[1] for b in blits})
        assert len(rows) == 2
        assert len([b for b in blits if b.dst[1] == rows[0]]) == 2
        assert len([b for b in blits if b.dst[1] == rows[1]]) == 1

    def test_the_odd_one_is_centred(self):
        _, blits, composed_w, _, _ = plan(self.CROPS)
        lowest = max(blits, key=lambda b: b.dst[1])
        centre = lowest.dst[0] + lowest.dst[2] / 2
        assert abs(centre - composed_w / 2) <= 2


class TestTheCameraMove:
    MOVE = (0, UPPER_LEFT, LOWER_RIGHT)

    def test_it_starts_exactly_on_the_old_view(self):
        upload, blits, _, _, moving = plan((), self.MOVE, now=0)
        assert moving
        assert blits[0].src == pytest.approx((0.0, 0.0, 0.5, 0.5), abs=1e-6)

    def test_it_lands_exactly_on_the_new_view(self):
        """The property ``tests/test_client_zoom.py`` pins for the software
        path. A move that stops a pixel short leaves the picture subtly wrong
        for as long as nobody changes region again."""
        _, blits, _, _, moving = plan((), self.MOVE, now=TRANSITION_NS)
        assert not moving
        assert blits[0].src == pytest.approx((0.5, 0.5, 0.5, 0.5), abs=1e-6)

    def test_it_is_still_running_in_between(self):
        assert plan((), self.MOVE, now=TRANSITION_NS // 2)[4] is True

    def test_it_moves_monotonically(self):
        seen = [
            plan((), self.MOVE, now=TRANSITION_NS * step // 20)[1][0].src[0]
            for step in range(21)
        ]
        assert seen == sorted(seen)
        assert seen[0] < seen[-1]

    def test_the_upload_is_the_union_and_no_more(self):
        """A move between two quadrants still costs half a frame, not all of
        it -- the union generalisation is what keeps the common single-player
        case cheap."""
        upload = plan((), (0, UPPER_LEFT, UPPER_RIGHT), now=0)[0]
        assert upload == (0, 0, 1920, 540)
        assert union_rect(UPPER_LEFT, UPPER_RIGHT) == (0.0, 0.0, 1.0, 0.5)

    def test_a_move_that_ends_where_it_is_uploads_no_more_than_settled(self):
        moving_upload = plan((), (0, UPPER_LEFT, UPPER_LEFT), now=0)[0]
        settled_upload = plan((UPPER_LEFT,))[0]
        assert moving_upload == settled_upload

    def test_the_travelling_rectangle_is_not_snapped_to_even_pixels(self):
        """Only the upload is aligned. Rounding the travelling rectangle shows
        up as the camera juddering two pixels at a time."""
        seen = {
            plan((), self.MOVE, now=TRANSITION_NS * step // 200)[1][0].src[0]
            for step in range(1, 40)
        }
        assert len(seen) > 30, "the camera is moving in steps, not smoothly"

    def test_a_move_from_the_whole_picture_is_handled(self):
        _, blits, _, _, _ = plan((), (0, WHOLE, UPPER_LEFT), now=0)
        assert blits[0].src == pytest.approx((0.0, 0.0, 1.0, 1.0), abs=1e-6)


class TestBothPathsAgree:
    """The software path and the GPU path must place every piece identically.

    They share ``compose`` for exactly this reason, so this is really a test
    that nobody has quietly given one of them its own copy -- which is how the
    two would drift, and the symptom would be the picture jumping when the
    operator changes mode.
    """

    @pytest.mark.parametrize("name", sorted(LAYOUTS))
    def test_the_decoder_and_the_planner_produce_the_same_rectangles(self, name):
        from client.media.decoder import VideoDecoder

        crops = LAYOUTS[name]
        decode = VideoDecoder(receiver=object())
        decode.set_viewport(*VIEWPORT)
        decode._crops = crops

        # The software path composes the crops it holds; with none it draws the
        # whole picture, which the planner spells as one full-frame piece.
        placed, composed_w, composed_h = decode._compose(*FRAME)
        _, blits, plan_w, plan_h, _ = plan(crops)

        if crops:
            assert [(x, y, w, h) for _, x, y, w, h in placed] == [b.dst for b in blits]
            assert (composed_w, composed_h) == (plan_w, plan_h)
        else:
            assert (plan_w, plan_h) == compose((WHOLE,), *FRAME, VIEWPORT)[1:]


class TestItSurvivesNonsense:
    """Everything here fails open to a picture. A planner that raises takes the
    stream down, which is strictly worse than a slightly wrong rectangle."""

    def test_no_viewport_means_the_streams_own_size(self):
        _, blits, w, h, _ = plan((), viewport=None)
        assert (w, h) == FRAME
        assert blits[0].dst == (0, 0, *FRAME)

    def test_a_tiny_frame_still_produces_a_usable_rectangle(self):
        upload, blits, w, h, _ = plan((UPPER_LEFT,), frame=(2, 2))
        assert upload[2] >= 2 and upload[3] >= 2
        assert w >= 2 and h >= 2

    def test_a_degenerate_crop_does_not_divide_by_zero(self):
        upload, blits, _, _, _ = plan(((0.0, 0.0, 0.0, 0.0),))
        assert upload[2] >= 2 and upload[3] >= 2
        assert blits[0].dst[2] >= 2

    def test_a_crop_running_off_the_edge_is_trimmed(self):
        upload = plan(((0.9, 0.9, 0.5, 0.5),))[0]
        assert upload[0] + upload[2] <= FRAME[0]
        assert upload[1] + upload[3] <= FRAME[1]


class TestEasing:
    def test_it_is_pinned_at_both_ends(self):
        assert eased(0) == 0.0
        assert eased(TRANSITION_NS) == 1.0
        assert eased(TRANSITION_NS * 2) == 1.0

    def test_it_is_smooth_rather_than_linear(self):
        assert eased(int(TRANSITION_NS * 0.1)) < 0.1
        assert eased(int(TRANSITION_NS * 0.9)) > 0.9
        assert eased(int(TRANSITION_NS * 0.5)) == pytest.approx(0.5)


def test_a_blit_is_immutable():
    """It crosses into native code. Something that can be edited after it was
    handed over is a description of pixels nobody is actually drawing."""
    blit = Blit((0.0, 0.0, 1.0, 1.0), (0, 0, 10, 10))
    with pytest.raises(Exception):
        blit.src = (1.0, 1.0, 1.0, 1.0)  # type: ignore[misc]

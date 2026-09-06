"""What a client should display, given a layout and its controllers' regions.

Pure logic, so these run anywhere -- no Qt, no PyAV, no sockets, no capture
device. That is the point of keeping ``common/screen_regions.py`` free of them:
this is the part of split-screen most likely to be wrong in a way nobody
notices, because a wrong crop still shows *a* picture.

The safety property worth stating plainly, since it is the one that would leak
information rather than merely look odd: a client assigned two regions that do
not touch must never be shown the rectangle that contains them both. That box
holds other players' views.
"""

from __future__ import annotations

import pytest

from common.screen_regions import (
    FULL,
    FULL_RECT,
    HORIZONTAL_2,
    LAYOUTS,
    LOWER,
    LOWER_LEFT,
    LOWER_RIGHT,
    QUAD_4,
    REGIONS,
    UPPER,
    UPPER_LEFT,
    UPPER_RIGHT,
    VERTICAL_2,
    LEFT,
    RIGHT,
    Rect,
    normalise_layout,
    normalise_regions,
    regions_for_layout,
    resolve,
    tile,
)


def rects(layout, assigned):
    return resolve(layout, assigned)


def only(layout, assigned) -> Rect:
    """The single rectangle this resolves to, asserting there is exactly one."""
    got = resolve(layout, assigned)
    assert len(got) == 1, f"expected one rect, got {got}"
    return got[0]


class TestTheSpecMatrix:
    """Every case named in the feature specification."""

    def test_quad_single_quadrant(self):
        assert only(QUAD_4, [UPPER_LEFT]) == Rect(0.0, 0.0, 0.5, 0.5)
        assert only(QUAD_4, [UPPER_RIGHT]) == Rect(0.5, 0.0, 0.5, 0.5)
        assert only(QUAD_4, [LOWER_LEFT]) == Rect(0.0, 0.5, 0.5, 0.5)
        assert only(QUAD_4, [LOWER_RIGHT]) == Rect(0.5, 0.5, 0.5, 0.5)

    def test_quad_two_quadrants_that_form_a_rectangle_merge(self):
        assert only(QUAD_4, [UPPER_LEFT, LOWER_LEFT]) == Rect(0.0, 0.0, 0.5, 1.0)
        assert only(QUAD_4, [UPPER_RIGHT, LOWER_RIGHT]) == Rect(0.5, 0.0, 0.5, 1.0)
        assert only(QUAD_4, [UPPER_LEFT, UPPER_RIGHT]) == Rect(0.0, 0.0, 1.0, 0.5)
        assert only(QUAD_4, [LOWER_LEFT, LOWER_RIGHT]) == Rect(0.0, 0.5, 1.0, 0.5)

    def test_quad_all_four_is_full_screen(self):
        # Empty means "draw everything", which is cheaper than a full-frame
        # crop and takes the renderer down its untouched path.
        assert rects(QUAD_4, [UPPER_LEFT, UPPER_RIGHT, LOWER_LEFT, LOWER_RIGHT]) == []

    def test_vertical_split(self):
        assert only(VERTICAL_2, [LEFT]) == Rect(0.0, 0.0, 0.5, 1.0)
        assert only(VERTICAL_2, [RIGHT]) == Rect(0.5, 0.0, 0.5, 1.0)

    def test_horizontal_split(self):
        assert only(HORIZONTAL_2, [UPPER]) == Rect(0.0, 0.0, 1.0, 0.5)
        assert only(HORIZONTAL_2, [LOWER]) == Rect(0.0, 0.5, 1.0, 0.5)

    def test_an_assignment_from_another_layout_means_full_screen(self):
        """A controller set up for quadrants, in a two-player vertical game.

        Ordinary, not an error: the operator assigns every region a controller
        might need and the active layout picks. Nothing relevant means the
        player watches the whole game rather than a blank window.
        """
        assert rects(VERTICAL_2, [UPPER_LEFT]) == []
        assert rects(HORIZONTAL_2, [LEFT, RIGHT]) == []
        assert rects(QUAD_4, [LEFT, UPPER]) == []

    def test_full_layout_ignores_every_assignment(self):
        for assigned in ([], [UPPER_LEFT], [LEFT], list(REGIONS)):
            assert rects(FULL, assigned) == []


class TestNonContiguousRegionsAreNeverMerged:
    """The safety property. A bounding box over two opposite quadrants covers
    the whole screen, and taking it would hand the client both opponents'
    views -- silently, and looking entirely correct."""

    def test_the_diagonals_stay_separate(self):
        for pair in ([UPPER_LEFT, LOWER_RIGHT], [UPPER_RIGHT, LOWER_LEFT]):
            got = rects(QUAD_4, pair)
            assert len(got) == 2, f"{pair} merged into {got}"
            assert FULL_RECT not in got
            for rect in got:
                assert (rect.width, rect.height) == (0.5, 0.5)

    def test_the_diagonal_shows_exactly_the_two_assigned_quadrants(self):
        got = rects(QUAD_4, [UPPER_LEFT, LOWER_RIGHT])
        assert set(got) == {Rect(0.0, 0.0, 0.5, 0.5), Rect(0.5, 0.5, 0.5, 0.5)}

    def test_three_quadrants_merge_what_they_can(self):
        """An L-shape is not a rectangle, but its top row is one."""
        got = rects(QUAD_4, [UPPER_LEFT, UPPER_RIGHT, LOWER_LEFT])
        assert got == [Rect(0.0, 0.0, 1.0, 0.5), Rect(0.0, 0.5, 0.5, 0.5)]

        got = rects(QUAD_4, [UPPER_RIGHT, LOWER_LEFT, LOWER_RIGHT])
        assert got == [Rect(0.5, 0.0, 0.5, 0.5), Rect(0.0, 0.5, 1.0, 0.5)]

    def test_no_result_ever_covers_an_unassigned_cell(self):
        """The invariant behind all of the above, over every combination."""
        from itertools import combinations

        quadrants = {
            UPPER_LEFT: (0, 0), UPPER_RIGHT: (1, 0),
            LOWER_LEFT: (0, 1), LOWER_RIGHT: (1, 1),
        }
        for size in range(1, 4):          # 4 == full screen, checked elsewhere
            for chosen in combinations(quadrants, size):
                covered = set()
                for rect in rects(QUAD_4, list(chosen)):
                    for col in (0, 1):
                        for row in (0, 1):
                            if (
                                rect.x <= col / 2 < rect.x + rect.width
                                and rect.y <= row / 2 < rect.y + rect.height
                            ):
                                covered.add((col, row))
                assert covered == {quadrants[name] for name in chosen}, (
                    f"{chosen} exposed {covered}"
                )


class TestOutputIsStable:
    def test_order_does_not_depend_on_assignment_order(self):
        a = rects(QUAD_4, [LOWER_RIGHT, UPPER_LEFT])
        b = rects(QUAD_4, [UPPER_LEFT, LOWER_RIGHT])
        assert a == b

    def test_duplicates_change_nothing(self):
        assert rects(QUAD_4, [UPPER_LEFT, UPPER_LEFT]) == rects(QUAD_4, [UPPER_LEFT])

    def test_results_are_read_order(self):
        got = rects(QUAD_4, [LOWER_RIGHT, UPPER_LEFT])
        assert got[0].y <= got[1].y


class TestFailOpen:
    """Every bad input shows the whole picture. None shows nothing."""

    @pytest.mark.parametrize(
        "layout",
        [None, "", "quad", "QUAD", 4, {}, [], "QUAD_5", object()],
    )
    def test_an_unknown_layout_is_full_screen(self, layout):
        assert rects(layout, [UPPER_LEFT]) == []
        assert normalise_layout(layout) == FULL

    @pytest.mark.parametrize(
        "assigned",
        [None, "", "upper_left", 0, {}, object(), ["nonsense"], [None], [42]],
    )
    def test_a_malformed_assignment_is_full_screen(self, assigned):
        assert rects(QUAD_4, assigned) == []

    def test_a_string_is_not_a_region_list(self):
        """'left' iterates to characters; it must not resolve to anything."""
        assert normalise_regions(LEFT) == []
        assert rects(VERTICAL_2, LEFT) == []

    def test_known_names_survive_a_mixed_list(self):
        assert normalise_regions([UPPER_LEFT, "junk", None, LEFT]) == [
            UPPER_LEFT, LEFT
        ]


class TestRectGeometry:
    def test_scaling_to_pixels(self):
        assert Rect(0.0, 0.0, 0.5, 0.5).scaled_to(1920, 1080) == (0, 0, 960, 540)
        assert Rect(0.5, 0.0, 0.5, 0.5).scaled_to(1920, 1080) == (960, 0, 960, 540)
        assert Rect(0.0, 0.5, 0.5, 0.5).scaled_to(1920, 1080) == (0, 540, 960, 540)
        assert Rect(0.5, 0.5, 0.5, 0.5).scaled_to(1920, 1080) == (960, 540, 960, 540)

    def test_halves_meet_exactly_on_an_odd_size(self):
        """Rounding the far edge, not the width, so there is no seam or overlap.

        Resolutions are not all even -- 1366x768 is the classic one -- and a
        crop that rounds each width independently either drops a column or
        claims one from the neighbour.
        """
        for width in (1919, 1365, 801, 33):
            left = Rect(0.0, 0.0, 0.5, 1.0).scaled_to(width, 100)
            right = Rect(0.5, 0.0, 0.5, 1.0).scaled_to(width, 100)
            assert left[0] + left[2] == right[0], width
            assert right[0] + right[2] == width, width

    def test_a_tiny_frame_still_yields_a_usable_rect(self):
        x, y, w, h = Rect(0.5, 0.5, 0.5, 0.5).scaled_to(1, 1)
        assert w >= 1 and h >= 1

    def test_is_full(self):
        assert FULL_RECT.is_full
        assert not Rect(0.0, 0.0, 0.5, 1.0).is_full


class TestVocabulary:
    def test_every_region_belongs_to_exactly_one_layout(self):
        seen: dict[str, str] = {}
        for layout in LAYOUTS:
            for region in regions_for_layout(layout):
                assert region not in seen, f"{region} in {layout} and {seen[region]}"
                seen[region] = layout
        assert set(seen) == set(REGIONS)

    def test_full_divides_nothing(self):
        assert regions_for_layout(FULL) == frozenset()

    def test_every_layout_partitions_the_frame(self):
        """Each layout's regions must tile the picture exactly, no gaps."""
        for layout in LAYOUTS:
            names = sorted(regions_for_layout(layout))
            if not names:
                continue
            area = 0.0
            for name in names:
                rect = only(layout, [name])
                area += rect.width * rect.height
            assert abs(area - 1.0) < 1e-9, layout


class TestTiling:
    """Only reached for regions that did not merge -- two or three of them."""

    def test_a_wide_viewport_puts_two_side_by_side(self):
        assert tile(2, 16 / 9) == (2, 1)

    def test_a_tall_viewport_stacks_two(self):
        assert tile(2, 9 / 16) == (1, 2)

    def test_one_region_is_not_tiled(self):
        assert tile(1, 16 / 9) == (1, 1)

    def test_every_arrangement_holds_every_region(self):
        for count in range(1, 5):
            for aspect in (0.5, 1.0, 16 / 9, 21 / 9):
                columns, rows = tile(count, aspect)
                assert columns * rows >= count

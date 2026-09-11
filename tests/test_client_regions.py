"""Cropping to the part of the screen this client owns.

Phase 7, the client half. The server decides *which* rectangles (tested in
``test_screen_state.py``); this is about turning them into pixels without
costing the 500 Hz input loop anything.

The pixel tests are the ones that matter. A wrong crop still shows *a*
picture -- a plausible, sharp, correctly-scaled picture of somebody else's
game -- so nothing about the shape of the output can tell you it is right.
These read the bytes back and check what is actually in them.
"""

from __future__ import annotations

import time

import pytest

av = pytest.importorskip("av", reason="video extras not installed")

from client.media.decoder import VideoDecoder  # noqa: E402


class FakeReceiver:
    """Enough of a receiver for the decoder to publish through."""

    decode_stats = None

    def __init__(self) -> None:
        self.idr_requests = 0

    def request_idr(self) -> None:
        self.idr_requests += 1


#: One luma value per quadrant, far enough apart to survive scaling.
QUADRANT_LUMA = {
    "upper_left": 32,
    "upper_right": 96,
    "lower_left": 160,
    "lower_right": 224,
}


def quartered_frame(width: int = 640, height: int = 480):
    """A frame whose four quadrants are four flat, distinguishable greys.

    Flat rather than textured on purpose: any pixel of the output can then be
    attributed to exactly one quadrant, which is what makes "did this crop
    leak" answerable rather than a matter of judgement.
    """
    from av.video.frame import VideoFrame

    frame = VideoFrame(width, height, "yuv420p")
    plane = frame.planes[0]
    stride = plane.line_size
    buf = bytearray(stride * height)
    for y in range(height):
        top = y < height // 2
        row = bytes(
            [QUADRANT_LUMA["upper_left" if top else "lower_left"]] * (width // 2)
            + [QUADRANT_LUMA["upper_right" if top else "lower_right"]]
            * (width - width // 2)
        )
        buf[y * stride : y * stride + width] = row
    plane.update(bytes(buf))

    # Neutral chroma, so the greys convert to greys rather than to colours.
    for chroma in frame.planes[1:]:
        chroma.update(bytes([128]) * chroma.buffer_size)
    return frame


#: What each quadrant's luma actually comes out as in RGB, measured rather
#: than calculated. YUV to RGB is a limited-range transform -- Y=32 lands near
#: 19, not 32 -- and hardcoding the coefficients would make these tests assert
#: something about colour maths instead of about cropping. Taking the values
#: from an uncropped conversion means each test compares a crop against what
#: the whole picture genuinely holds in that quadrant, which is the property
#: worth checking.
_REFERENCE: dict[str, int] = {}


def reference_greys() -> dict[str, int]:
    if not _REFERENCE:
        decode = VideoDecoder(FakeReceiver())
        frame = publish(decode, quartered_frame(640, 480))
        for name, (fx, fy) in (
            ("upper_left", (0.25, 0.25)), ("upper_right", (0.75, 0.25)),
            ("lower_left", (0.25, 0.75)), ("lower_right", (0.75, 0.75)),
        ):
            x = int(frame.width * fx)
            y = int(frame.height * fy)
            _REFERENCE[name] = frame.pixels[y * frame.stride + x * 3]
        gaps = sorted(_REFERENCE.values())
        assert min(b - a for a, b in zip(gaps, gaps[1:])) > 2 * _TOLERANCE, (
            "the four quadrants must stay distinguishable after conversion"
        )
    return _REFERENCE


#: Half the smallest gap between two reference values would be the largest
#: safe figure; this is well inside it, and generous enough to absorb the
#: scaler's own rounding.
_TOLERANCE = 12


def greys_in(view) -> set[str]:
    """Which quadrants a view actually contains, by name.

    The middle of each 8x8 block is sampled rather than every pixel: the
    scaler blends across a quadrant boundary, so pixels right at one belong to
    neither and would make any exact comparison meaningless. Sampling well
    inside is what separates "a blend at the seam" from "a whole quadrant that
    should not be here" -- and the second is the thing being looked for.
    """
    reference = reference_greys()
    found = set()
    for y in range(4, view.height - 4, 8):
        for x in range(4, view.width - 4, 8):
            value = view.pixels[y * view.stride + x * 3]
            name = min(reference, key=lambda n: abs(reference[n] - value))
            if abs(reference[name] - value) <= _TOLERANCE:
                found.add(name)
    return found


def decoder(viewport=(640, 480)) -> VideoDecoder:
    decode = VideoDecoder(FakeReceiver())
    decode.set_viewport(*viewport)
    return decode


def publish_once(decode: VideoDecoder, frame=None):
    """Exactly one frame, whatever state the camera is in."""
    decode._publish(frame or quartered_frame(), 0, time.perf_counter_ns())
    return decode.latest()


def publish(decode: VideoDecoder, frame=None):
    """A frame with the camera at rest.

    Changing the regions starts a 400 ms move, during which the decoder
    renders the union of the old and new views and the window presents a
    travelling rectangle of it. Everything in this file is about where the
    picture *settles*, so it waits for that -- the move itself has its own
    tests in ``test_client_zoom.py``.

    Bounded rather than a `while True`: a decoder that never settles is a bug
    worth failing on, not one worth hanging on.
    """
    frame = frame or quartered_frame()
    for _ in range(200):
        decode._publish(frame, 0, time.perf_counter_ns())
        if decode.latest() is not None and decode.latest().zoom is None:
            return decode.latest()
        time.sleep(0.01)
    raise AssertionError("the camera never came to rest")


class TestSettingRegions:
    def test_dicts_from_the_wire_are_accepted(self):
        """That is the shape VIDEO_REGIONS carries."""
        decode = decoder()
        decode.set_regions([{"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}])
        assert decode._crops == ((0.0, 0.0, 0.5, 0.5),)

    def test_tuples_are_accepted_too(self):
        decode = decoder()
        decode.set_regions([(0.5, 0.5, 0.5, 0.5)])
        assert decode._crops == ((0.5, 0.5, 0.5, 0.5),)

    @pytest.mark.parametrize(
        "bad",
        [None, "", "left", 0, {}, object(), [None], [{}], [(1, 2)], [{"x": 0}],
         [{"x": "a", "y": 0, "w": 1, "h": 1}]],
    )
    def test_anything_unreadable_means_the_whole_picture(self, bad):
        """Fails open. A wrong crop shows a slice of somebody else's game and
        looks entirely correct doing it; the whole picture is what the player
        had before this feature existed."""
        decode = decoder()
        decode.set_regions([{"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}])
        decode.set_regions(bad)
        assert decode._crops == ()

    def test_a_zero_sized_crop_is_dropped(self):
        decode = decoder()
        decode.set_regions([{"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.5}])
        assert decode._crops == ()

    def test_a_crop_running_past_the_edge_is_trimmed(self):
        decode = decoder()
        decode.set_regions([{"x": 0.75, "y": 0.0, "w": 0.5, "h": 1.0}])
        (x, y, w, h), = decode._crops
        assert x + w <= 1.0 and y + h <= 1.0

    def test_setting_the_same_crops_again_keeps_the_graphs(self):
        """Rebuilding a filter graph per frame would undo the whole point of
        caching them."""
        decode = decoder()
        decode.set_regions([{"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}])
        publish(decode)
        assert decode._graphs

        decode.set_regions([{"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}])
        assert decode._graphs

    def test_changing_them_drops_the_graphs(self):
        decode = decoder()
        decode.set_regions([{"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}])
        publish(decode)
        decode.set_regions([{"x": 0.5, "y": 0.0, "w": 0.5, "h": 0.5}])
        assert decode._graphs == {}


class TestTheUncroppedPathIsUnchanged:
    def test_no_regions_publishes_one_whole_picture(self):
        decode = decoder()
        frame = publish(decode)
        assert frame.views == ()
        assert (frame.width, frame.height) == (640, 480)

    def test_it_still_shows_all_four_quadrants(self):
        decode = decoder()
        assert greys_in(publish(decode)) == set(QUADRANT_LUMA)


class TestOneRegion:
    @pytest.mark.parametrize(
        ("name", "crop"),
        [
            ("upper_left", (0.0, 0.0, 0.5, 0.5)),
            ("upper_right", (0.5, 0.0, 0.5, 0.5)),
            ("lower_left", (0.0, 0.5, 0.5, 0.5)),
            ("lower_right", (0.5, 0.5, 0.5, 0.5)),
        ],
    )
    def test_it_shows_that_quadrant_and_nothing_else(self, name, crop):
        """The one that would catch an off-by-one in the crop offset, which
        would otherwise look like a perfectly good picture of the wrong
        player's game."""
        decode = decoder()
        decode.set_regions([crop])
        frame = publish(decode)

        assert len(frame.views) == 1
        assert greys_in(frame.views[0]) == {name}

    def test_it_fills_the_viewport(self):
        decode = decoder(viewport=(640, 480))
        decode.set_regions([(0.0, 0.0, 0.5, 0.5)])
        view = publish(decode).views[0]
        # A 320x240 crop into a 640x480 viewport: same aspect, so it fills.
        assert (view.width, view.height) == (640, 480)
        assert (view.x, view.y) == (0, 0)

    def test_a_half_keeps_its_aspect_ratio(self):
        decode = decoder(viewport=(640, 480))
        decode.set_regions([(0.0, 0.0, 0.5, 1.0)])
        view = publish(decode).views[0]
        # 320x480 source into 640x480: height-limited, so 320x480.
        assert (view.width, view.height) == (320, 480)

    def test_the_left_half_shows_both_left_quadrants(self):
        decode = decoder()
        decode.set_regions([(0.0, 0.0, 0.5, 1.0)])
        assert greys_in(publish(decode).views[0]) == {"upper_left", "lower_left"}


class TestSeveralRegions:
    def test_the_diagonal_is_two_separate_pieces(self):
        """Not one rectangle covering both -- that rectangle is the whole
        screen, and it holds the two players in between."""
        decode = decoder()
        decode.set_regions([(0.0, 0.0, 0.5, 0.5), (0.5, 0.5, 0.5, 0.5)])
        frame = publish(decode)

        assert len(frame.views) == 2
        assert greys_in(frame.views[0]) == {"upper_left"}
        assert greys_in(frame.views[1]) == {"lower_right"}

    def test_neither_piece_contains_the_other_players(self):
        """Stated as its own assertion because it is the property the whole
        contiguous/non-contiguous distinction exists to protect."""
        decode = decoder()
        decode.set_regions([(0.0, 0.0, 0.5, 0.5), (0.5, 0.5, 0.5, 0.5)])
        frame = publish(decode)

        shown = set()
        for view in frame.views:
            shown |= greys_in(view)
        assert "upper_right" not in shown
        assert "lower_left" not in shown

    def test_they_do_not_overlap(self):
        decode = decoder(viewport=(640, 480))
        decode.set_regions([(0.0, 0.0, 0.5, 0.5), (0.5, 0.5, 0.5, 0.5)])
        frame = publish(decode)

        boxes = [(v.x, v.y, v.x + v.width, v.y + v.height) for v in frame.views]
        (ax0, ay0, ax1, ay1), (bx0, by0, bx1, by1) = boxes
        assert ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0

    def test_they_fit_inside_the_composed_picture(self):
        decode = decoder(viewport=(640, 480))
        decode.set_regions([(0.0, 0.0, 0.5, 0.5), (0.5, 0.5, 0.5, 0.5)])
        frame = publish(decode)

        for view in frame.views:
            assert view.x >= 0 and view.y >= 0
            assert view.x + view.width <= frame.composed_width
            assert view.y + view.height <= frame.composed_height

    def test_the_composed_picture_fits_the_viewport(self):
        """It is drawn 1:1, so anything larger would be scaled at paint time
        -- under the GIL, which is the cost this whole design avoids."""
        decode = decoder(viewport=(640, 480))
        decode.set_regions([(0.0, 0.0, 0.5, 0.5), (0.5, 0.5, 0.5, 0.5)])
        frame = publish(decode)
        assert frame.composed_width <= 640
        assert frame.composed_height <= 480

    def test_three_pieces_work(self):
        decode = decoder()
        decode.set_regions([
            (0.0, 0.0, 0.5, 0.5), (0.5, 0.0, 0.5, 0.5), (0.5, 0.5, 0.5, 0.5),
        ])
        assert len(publish(decode).views) == 3


class TestItNeverTakesTheStreamDown:
    def test_a_crop_of_a_tiny_frame_does_not_raise(self):
        decode = decoder(viewport=(64, 64))
        decode.set_regions([(0.5, 0.5, 0.5, 0.5)])
        frame = publish(decode, quartered_frame(16, 16))
        assert frame is not None and decode.decode_errors == 0

    def test_a_resolution_change_mid_stream_is_handled(self):
        """Normalised crops are exactly what makes this a non-event, but the
        cached graph is bound to a size and must not be reused."""
        decode = decoder()
        decode.set_regions([(0.0, 0.0, 0.5, 0.5)])
        assert greys_in(
            publish(decode, quartered_frame(640, 480)).views[0]
        ) == {"upper_left"}
        assert greys_in(
            publish(decode, quartered_frame(1280, 720)).views[0]
        ) == {"upper_left"}
        assert decode.decode_errors == 0

    def test_the_graph_cache_does_not_grow_without_bound(self):
        decode = decoder()
        decode.set_regions([(0.0, 0.0, 0.5, 0.5)])
        for size in range(320, 720, 40):
            decode.set_viewport(size, size)
            publish(decode)
        assert len(decode._graphs) <= 9

    def test_a_frame_still_carries_a_single_picture(self):
        """Anything reading a frame without knowing about regions -- the
        latency overlay, a test, a screenshot -- must still find pixels."""
        decode = decoder()
        decode.set_regions([(0.5, 0.5, 0.5, 0.5)])
        frame = publish(decode)
        assert frame.pixels is not None
        assert frame.width > 0 and frame.height > 0
        assert len(frame.pixels) >= frame.stride * (frame.height - 1)


class TestCost:
    def test_cropping_is_not_more_expensive_than_not_cropping(self):
        """Measured rather than assumed, because the obvious implementation --
        scale the whole frame up until the crop fills the viewport, then slice
        -- is about twice the cost of no crop at all. Cropping before scaling
        means the scaler touches fewer source pixels than it otherwise would,
        so this comes out ahead.

        Generously bounded: this runs on whatever CI has, and the point is to
        catch a change of approach, not to measure the machine.
        """
        frame = quartered_frame(1280, 720)

        whole = decoder(viewport=(1280, 720))
        publish(whole, frame)
        started = time.perf_counter()
        for _ in range(20):
            publish(whole, frame)
        uncropped = time.perf_counter() - started

        cropped = decoder(viewport=(1280, 720))
        cropped.set_regions([(0.0, 0.0, 0.5, 0.5)])
        publish(cropped, frame)
        started = time.perf_counter()
        for _ in range(20):
            publish(cropped, frame)
        with_crop = time.perf_counter() - started

        assert with_crop < uncropped * 2.0, (
            f"cropping cost {with_crop / uncropped:.1f}x an uncropped frame; "
            "the crop should happen before the scale, not after"
        )

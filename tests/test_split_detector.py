"""Recognising a split screen without knowing the game.

Frames are generated, not stored: there are no game screenshots in the repo, and
a detector that can only be exercised against a capture card is one nobody runs.
``analyse_gray`` takes bytes, so most of this needs neither PyAV nor a device.

The cases that matter most are the ones that must *not* trigger. A detector that
finds split screens is easy; one that leaves a menu, a loading screen and a
centred HUD alone is the useful part, because every false positive rearranges
somebody's window mid-game.
"""

from __future__ import annotations

import random

import pytest

from common.screen_regions import FULL, HORIZONTAL_2, QUAD_4, VERTICAL_2
from videoserver.layout import (
    DetectorConfig,
    LayoutSample,
    SplitLayoutState,
    analyse_gray,
)

WIDTH = 320
HEIGHT = 180
#: Deliberately not equal to width. FFmpeg pads rows, and a detector that
#: indexes by width instead of stride reads a sheared picture -- so every
#: fixture here is padded to keep that honest.
STRIDE = 336


def frame(fill: int = 0) -> bytearray:
    return bytearray([fill]) * (STRIDE * HEIGHT)


def put(buf: bytearray, x: int, y: int, value: int) -> None:
    buf[y * STRIDE + x] = value


def fill_rect(buf: bytearray, x0, y0, x1, y1, value) -> None:
    for y in range(y0, y1):
        base = y * STRIDE
        for x in range(x0, x1):
            buf[base + x] = value


def textured(
    buf: bytearray, x0, y0, x1, y1, seed: int, base: int = 40, spread: int = 60
) -> None:
    """A region with the local continuity of a real scene.

    Smooth in **both** directions: neighbouring pixels are close horizontally
    and vertically, which is what makes a photograph compressible and what
    makes a seam stand out against it.

    Getting this wrong is instructive. The first version restarted the random
    walk on every row, so adjacent rows were unrelated and every row boundary
    read as a horizontal edge -- the detector duly reported HORIZONTAL_2 for
    plain gameplay. The fixture was wrong, not the detector, but a detector
    tested only against vertically-incoherent noise would have proved nothing.
    """
    rng = random.Random(seed)
    width = x1 - x0
    value = base + rng.randrange(spread)
    row_values = []
    for _ in range(width):
        value = max(0, min(255, value + rng.randrange(-6, 7)))
        row_values.append(value)

    for y in range(y0, y1):
        row_values = [
            max(0, min(255, val + rng.randrange(-3, 4))) for val in row_values
        ]
        base_offset = y * STRIDE + x0
        for index, val in enumerate(row_values):
            buf[base_offset + index] = val


def gameplay(seed: int = 1) -> bytearray:
    buf = frame()
    textured(buf, 0, 0, WIDTH, HEIGHT, seed)
    return buf


def vertical_split(separator: int | None = 0, seed_a=1, seed_b=99) -> bytearray:
    """Two unrelated scenes side by side, optionally with a divider drawn."""
    buf = frame()
    half = WIDTH // 2
    textured(buf, 0, 0, half, HEIGHT, seed_a)
    textured(buf, half, 0, WIDTH, HEIGHT, seed_b, base=150)
    if separator is not None:
        fill_rect(buf, half - 1, 0, half + 1, HEIGHT, separator)
    return buf


def horizontal_split(separator: int | None = 0) -> bytearray:
    buf = frame()
    half = HEIGHT // 2
    textured(buf, 0, 0, WIDTH, half, 7)
    textured(buf, 0, half, WIDTH, HEIGHT, 21, base=150)
    if separator is not None:
        fill_rect(buf, 0, half - 1, WIDTH, half + 1, separator)
    return buf


def quad_split(separator: int | None = 0) -> bytearray:
    buf = frame()
    hx, hy = WIDTH // 2, HEIGHT // 2
    textured(buf, 0, 0, hx, hy, 3, base=30)
    textured(buf, hx, 0, WIDTH, hy, 4, base=150)
    textured(buf, 0, hy, hx, HEIGHT, 5, base=150)
    textured(buf, hx, hy, WIDTH, HEIGHT, 6, base=30)
    if separator is not None:
        fill_rect(buf, hx - 1, 0, hx + 1, HEIGHT, separator)
        fill_rect(buf, 0, hy - 1, WIDTH, hy + 1, separator)
    return buf


def analyse(buf: bytearray, config: DetectorConfig | None = None) -> LayoutSample:
    return analyse_gray(memoryview(buf), WIDTH, HEIGHT, STRIDE, config)


class TestItRecognisesEachLayout:
    def test_full_screen_gameplay(self):
        assert analyse(gameplay()).layout == FULL

    @pytest.mark.parametrize("separator", [0, 255, 128, None])
    def test_vertical_split_with_any_separator_or_none(self, separator):
        """Black, white, grey, and no drawn divider at all.

        The spec is explicit that a separator cannot be assumed. With none, the
        only evidence is that the two halves are unrelated pictures -- which is
        the signal the detector is actually built on.
        """
        sample = analyse(vertical_split(separator))
        assert sample.layout == VERTICAL_2, f"separator={separator}"
        assert sample.confidence >= 0.75

    @pytest.mark.parametrize("separator", [0, 255, None])
    def test_horizontal_split(self, separator):
        sample = analyse(horizontal_split(separator))
        assert sample.layout == HORIZONTAL_2, f"separator={separator}"

    @pytest.mark.parametrize("separator", [0, 255, None])
    def test_quad_split(self, separator):
        sample = analyse(quad_split(separator))
        assert sample.layout == QUAD_4, f"separator={separator}"

    def test_the_boundary_is_reported_near_the_middle(self):
        sample = analyse(vertical_split())
        assert 0.45 <= sample.vertical_at <= 0.55

    def test_a_boundary_slightly_off_centre_is_still_found(self):
        """Consoles do not always split at exactly 50%, and the downscale
        moves it further."""
        buf = frame()
        split = int(WIDTH * 0.52)
        textured(buf, 0, 0, split, HEIGHT, 1)
        textured(buf, split, 0, WIDTH, HEIGHT, 2, base=160)
        assert analyse(buf).layout == VERTICAL_2


class TestThingsThatMustNotTrigger:
    """Every false positive rearranges a player's window mid-game."""

    def test_a_blank_menu(self):
        assert analyse(frame(16)).layout == FULL

    def test_a_loading_screen_with_a_centred_logo(self):
        buf = frame(8)
        fill_rect(buf, 120, 70, 200, 110, 200)
        assert analyse(buf).layout == FULL

    def test_a_centred_hud_element(self):
        """A crosshair or health bar makes a strong edge near the middle -- but
        only over the rows it occupies. A seam runs the whole height, and that
        is the distinction the coverage measure is for."""
        buf = gameplay()
        fill_rect(buf, WIDTH // 2 - 1, 80, WIDTH // 2 + 1, 100, 255)
        assert analyse(buf).layout == FULL

    def test_a_full_height_bar_that_is_not_central(self):
        """A scoreboard or sidebar down one third of the screen is not a split."""
        buf = gameplay()
        fill_rect(buf, WIDTH // 4, 0, WIDTH // 4 + 2, HEIGHT, 255)
        assert analyse(buf).layout == FULL

    def test_a_busy_high_contrast_scene(self):
        """Vertical stripes everywhere: a fence, a tiled wall, a barcode.

        Confidence is prominence, not magnitude, so a picture where every
        column is an edge has no candidate that stands out."""
        buf = frame()
        for x in range(0, WIDTH, 4):
            fill_rect(buf, x, 0, x + 2, HEIGHT, 220)
        assert analyse(buf).layout == FULL

    def test_pure_noise(self):
        buf = frame()
        rng = random.Random(4)
        for y in range(HEIGHT):
            row = y * STRIDE
            for x in range(WIDTH):
                buf[row + x] = rng.randrange(256)
        assert analyse(buf).layout == FULL

    def test_a_hard_edge_at_the_frame_border(self):
        """Letterbox bars and overscan are edges, but not seams."""
        buf = gameplay()
        fill_rect(buf, 0, 0, 3, HEIGHT, 0)
        fill_rect(buf, WIDTH - 3, 0, WIDTH, HEIGHT, 0)
        assert analyse(buf).layout == FULL


class TestItNeverRaises:
    """A detector that takes the video server down is worse than one that
    cannot classify a frame."""

    @pytest.mark.parametrize(
        "width,height,stride",
        [(0, 0, 0), (8, 8, 8), (320, 180, 100), (-1, -1, -1), (320, 0, 336)],
    )
    def test_degenerate_geometry_is_full(self, width, height, stride):
        assert analyse_gray(memoryview(frame()), width, height, stride).layout == FULL

    def test_a_short_buffer_is_full(self):
        assert analyse_gray(memoryview(bytearray(64)), WIDTH, HEIGHT, STRIDE).layout == FULL

    def test_plain_bytes_work_too(self):
        assert analyse_gray(bytes(frame(16)), WIDTH, HEIGHT, STRIDE).layout == FULL


class TestDebouncing:
    """A layout that followed every frame would rearrange the picture during a
    loading screen."""

    def config(self, **over):
        return DetectorConfig(**{"activate_samples": 3, "deactivate_samples": 5, **over})

    def feed(self, state, layout, count, confidence=0.9):
        changed = False
        for _ in range(count):
            changed |= state.update(LayoutSample(layout, confidence))
        return changed

    def test_it_starts_full(self):
        assert SplitLayoutState().layout == FULL

    def test_one_frame_never_changes_the_layout(self):
        state = SplitLayoutState(config=self.config())
        assert state.update(LayoutSample(QUAD_4, 0.99)) is False
        assert state.layout == FULL

    def test_a_candidate_must_persist_to_be_adopted(self):
        state = SplitLayoutState(config=self.config())
        assert self.feed(state, QUAD_4, 2) is False
        assert state.layout == FULL
        assert self.feed(state, QUAD_4, 1) is True
        assert state.layout == QUAD_4

    def test_a_two_frame_flicker_is_ignored(self):
        """The menu-transition case from the spec."""
        state = SplitLayoutState(config=self.config())
        self.feed(state, QUAD_4, 3)
        assert state.layout == QUAD_4

        self.feed(state, FULL, 2)          # a brief cutaway
        assert state.layout == QUAD_4

        self.feed(state, QUAD_4, 1)        # back to the game
        assert state.layout == QUAD_4

    def test_leaving_a_layout_is_harder_than_entering_one(self):
        state = SplitLayoutState(config=self.config())
        self.feed(state, QUAD_4, 3)
        assert self.feed(state, FULL, 4) is False
        assert state.layout == QUAD_4
        assert self.feed(state, FULL, 1) is True
        assert state.layout == FULL

    def test_an_interrupted_run_starts_over(self):
        state = SplitLayoutState(config=self.config())
        self.feed(state, QUAD_4, 2)
        self.feed(state, VERTICAL_2, 1)
        self.feed(state, QUAD_4, 2)
        assert state.layout == FULL
        self.feed(state, QUAD_4, 1)
        assert state.layout == QUAD_4

    def test_an_unreadable_frame_is_not_evidence(self):
        """A dropped frame must not eventually flip the layout."""
        state = SplitLayoutState(config=self.config())
        self.feed(state, QUAD_4, 3)
        for _ in range(20):
            assert state.update(None) is False
        assert state.layout == QUAD_4

    def test_confidence_follows_the_confirmed_layout(self):
        state = SplitLayoutState(config=self.config())
        self.feed(state, QUAD_4, 3, confidence=0.82)
        assert state.confidence == pytest.approx(0.82)


class TestManualOverride:
    """Some games have layouts detection cannot identify, and testing without a
    split-screen game has to be possible."""

    def test_an_override_pins_the_layout(self):
        state = SplitLayoutState()
        assert state.set_override(QUAD_4) is True
        assert state.layout == QUAD_4
        assert state.snapshot()["source"] == "override"

    def test_detection_is_ignored_while_pinned(self):
        state = SplitLayoutState()
        state.set_override(VERTICAL_2)
        for _ in range(50):
            assert state.update(LayoutSample(QUAD_4, 1.0)) is False
        assert state.layout == VERTICAL_2

    def test_releasing_the_pin_returns_to_detection(self):
        state = SplitLayoutState()
        state.set_override(QUAD_4)
        state.set_override("auto")
        assert state.snapshot()["source"] == "auto"
        # And the detector has to earn the next change rather than inheriting it.
        assert state.update(LayoutSample(FULL, 0.9)) is False

    @pytest.mark.parametrize("value", [None, "", "nonsense", 4, {}, "quad_4"])
    def test_a_bad_override_falls_back_safely(self, value):
        state = SplitLayoutState()
        state.set_override(value)
        assert state.layout in (FULL,) or state.override == "auto"
        assert state.snapshot()["mode"] in ("FULL", "auto") or state.layout == FULL

    def test_setting_the_same_override_twice_is_not_a_change(self):
        state = SplitLayoutState()
        assert state.set_override(QUAD_4) is True
        assert state.set_override(QUAD_4) is False


class TestSnapshot:
    def test_it_carries_what_both_guis_need(self):
        state = SplitLayoutState()
        snap = state.snapshot()
        assert set(snap) == {"mode", "confidence", "source"}
        assert snap["mode"] == FULL


class TestAgainstRealFrames:
    """The PyAV half: reformat to gray, read the plane, classify.

    Everything above works on bytes, which is what makes it runnable anywhere.
    This is the part that needs a real ``av.VideoFrame`` -- the reformat, the
    plane's own ``line_size``, and the fact that the detector owns its scaler
    rather than calling ``frame.reformat()``.
    """

    def build(self, width=1280, height=720, quad=True):
        av = pytest.importorskip("av", reason="video extras not installed")
        from av.video.frame import VideoFrame

        frame = VideoFrame(width, height, "yuv420p")
        plane = frame.planes[0]
        stride = plane.line_size
        buf = bytearray(stride * height)
        rng = random.Random(1)

        if quad:
            blocks = (
                (0, width // 2, 0, height // 2, 30),
                (width // 2, width, 0, height // 2, 170),
                (0, width // 2, height // 2, height, 180),
                (width // 2, width, height // 2, height, 40),
            )
        else:
            blocks = ((0, width, 0, height, 90),)

        for x0, x1, y0, y1, base in blocks:
            # Mean-reverting, not a free random walk. An unbounded walk over
            # several hundred pixels drifts into the 0/255 clamps, sits there,
            # and then leaves -- which puts hard full-height edges at arbitrary
            # columns. Those are not what a game looks like, and they read as
            # competing seams: measured, they held a genuine quad split down to
            # 0.72 confidence by inflating the background it is scored against.
            value = float(base)
            values = []
            for _ in range(x1 - x0):
                value += rng.uniform(-5, 5) + (base - value) * 0.05
                values.append(int(max(0, min(255, value))))
            for y in range(y0, y1):
                values = [
                    int(max(0, min(255, v + rng.uniform(-2, 2) + (base - v) * 0.05)))
                    for v in values
                ]
                offset = y * stride + x0
                buf[offset : offset + len(values)] = bytes(values)

        plane.update(bytes(buf))
        return frame

    def test_a_real_quad_frame_is_recognised(self):
        from videoserver.layout import DetectorConfig, LayoutDetector

        detector = LayoutDetector(DetectorConfig(width=320))
        sample = detector.sample(self.build())
        assert sample is not None
        assert sample.layout == QUAD_4
        assert 0.45 <= sample.vertical_at <= 0.55
        assert 0.45 <= sample.horizontal_at <= 0.55
        assert detector.stats() == {"frames_analysed": 1, "errors": 0}

    def test_a_real_full_frame_is_left_alone(self):
        from videoserver.layout import DetectorConfig, LayoutDetector

        detector = LayoutDetector(DetectorConfig(width=320))
        sample = detector.sample(self.build(quad=False))
        assert sample is not None and sample.layout == FULL

    def test_an_odd_resolution_is_handled(self):
        """1366x768 is the classic one, and 4:2:0 needs even dimensions."""
        from videoserver.layout import DetectorConfig, LayoutDetector

        detector = LayoutDetector(DetectorConfig(width=320))
        sample = detector.sample(self.build(width=1366, height=768))
        assert sample is not None and sample.layout == QUAD_4

    def test_rubbish_input_does_not_raise(self):
        from videoserver.layout import LayoutDetector

        detector = LayoutDetector()
        assert detector.sample(object()) is None
        assert detector.sample(None) is None
        assert detector.stats()["errors"] == 2

    def test_it_owns_its_scaler(self):
        """Never the scaler cached on the frame: capture hands one object to
        the encoder and both previews, and two threads inside that cache wedge
        one of them for good. Checked behaviourally -- a grep for the call
        cannot tell an intention from a comment about one."""
        from videoserver.layout import LayoutDetector

        detector = LayoutDetector()
        assert detector._reformatter is None
        detector.sample(self.build(320, 180, quad=False))
        assert detector._reformatter is not None

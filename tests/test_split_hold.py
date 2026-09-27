"""A split screen that holds still.

Reported from the field on Mario Kart 64: the layout kept flipping between
full screen and split while the game stayed split the whole time. Three causes,
each pinned here against the code that had it:

* the analysis cropped to a letterbox measured **per frame**, so a dark sky or
  tunnel at the top of the picture was cropped as though it were a black bar,
  moving the "middle" off the seam;
* staying split needed exactly the evidence entering it did, so a handful of
  weak frames dropped the layout;
* every frame was judged alone, though the seam never moves and scene edges do.

The fixtures are the ones ``test_split_detector.py`` builds, so the calibration
recorded there -- the menu, the HUD split, 1366x768 -- is unchanged by
anything here.
"""

from __future__ import annotations

import random

import pytest

from common.screen_regions import FULL, HORIZONTAL_2, QUAD_4, VERTICAL_2
from tests.test_split_detector import (
    HEIGHT,
    STRIDE,
    WIDTH,
    fill_rect,
    frame,
    gameplay,
    horizontal_split,
    letterboxed,
    pillarboxed,
    quad_split,
    textured,
)
from videoserver.layout import (
    DetectorConfig,
    LayoutAnalyser,
    LayoutSample,
    SplitCalibration,
    SplitLayoutState,
    active_area,
    analyse_gray,
)

SECOND = 1_000_000_000


def dark_top_split(rows: int = 22, level: int = 8) -> bytearray:
    """A horizontal split whose upper viewport starts with a black sky.

    Rainbow Road, a tunnel, a night track: the top of the top player's picture
    is as dark as a letterbox bar, and nothing at the bottom matches it.
    """
    buf = horizontal_split()
    fill_rect(buf, 0, 0, WIDTH, rows, level)
    return buf


def weak_seam_split(visible: float = 0.45) -> bytearray:
    """A horizontal split where the seam shows across only part of the width.

    The rest of the picture carries on across the boundary unbroken -- a sky
    the same colour as the road beneath it, a pause overlay, two viewports
    that happen to agree at the join. Coverage lands below the entry gate
    while the seam is plainly still there.
    """
    buf = frame()
    cut = int(WIDTH * visible)
    half = HEIGHT // 2
    textured(buf, 0, 0, cut, half, 7)
    textured(buf, 0, half, cut, HEIGHT, 21, base=150)
    textured(buf, cut, 0, WIDTH, HEIGHT, 33)
    return buf


def sample(buf: bytearray, config: DetectorConfig | None = None) -> LayoutSample:
    return analyse_gray(memoryview(buf), WIDTH, HEIGHT, STRIDE, config)


class TestTheLetterboxIsMeasuredSymmetrically:
    """A letterbox is centred. A dark scene is not."""

    def test_a_dark_top_is_not_cropped_as_a_bar(self):
        x0, x1, y0, y1 = active_area(memoryview(dark_top_split()), WIDTH, HEIGHT, STRIDE)
        assert (y0, y1) == (0, HEIGHT - 1)

    def test_a_split_with_a_dark_sky_is_still_a_split(self):
        """The field case. Failed before: the crop moved the middle 11 rows
        off the seam, against a tolerance of about three."""
        assert sample(dark_top_split()).layout == HORIZONTAL_2

    def test_the_same_on_a_pillarboxed_capture(self):
        buf = pillarboxed(dark_top_split, bars=43)
        assert sample(buf).layout == HORIZONTAL_2

    def test_symmetric_bars_are_still_cropped(self):
        buf = letterboxed(gameplay, bars=24)
        _, _, y0, y1 = active_area(memoryview(buf), WIDTH, HEIGHT, STRIDE)
        assert y0 == pytest.approx(24, abs=2)
        assert y1 == pytest.approx(HEIGHT - 25, abs=2)

    def test_a_bar_widened_by_dark_picture_keeps_the_true_width(self):
        """Pillarbox bars of 43, and dark picture beside the right one. The
        narrower side is the bar; the extra is picture."""
        buf = pillarboxed(horizontal_split, bars=43)
        fill_rect(buf, WIDTH - 43 - 12, 0, WIDTH - 43, HEIGHT, 5)
        x0, x1, _, _ = active_area(memoryview(buf), WIDTH, HEIGHT, STRIDE)
        assert x0 == pytest.approx(43, abs=2)
        assert WIDTH - 1 - x1 == pytest.approx(43, abs=2)


class TestTheAnalysisUsesTheSettledLetterbox:
    def test_a_supplied_area_is_what_is_analysed(self):
        """Once the bars are known, a frame whose own reading disagrees is
        analysed inside the known ones rather than its own."""
        buf = pillarboxed(horizontal_split, bars=43)
        settled = (43 / WIDTH, 0.0, (WIDTH - 86) / WIDTH, 1.0)
        result = analyse_gray(memoryview(buf), WIDTH, HEIGHT, STRIDE, active=settled)
        assert result.layout == HORIZONTAL_2

    def test_the_per_frame_reading_is_still_reported(self):
        """The debounce needs every frame's own measurement, or a letterbox
        that genuinely changed could never be adopted."""
        buf = pillarboxed(horizontal_split, bars=43)
        result = analyse_gray(
            memoryview(buf), WIDTH, HEIGHT, STRIDE, active=(0.0, 0.0, 1.0, 1.0)
        )
        assert result.active[0] == pytest.approx(43 / WIDTH, abs=0.01)


class TestEnterAndHoldAreDifferentQuestions:
    def test_a_weak_seam_does_not_enter(self):
        result = sample(weak_seam_split())
        assert result.layout == FULL
        assert result.horizontal == pytest.approx(0.0)

    def test_but_it_is_measured_for_holding(self):
        result = sample(weak_seam_split())
        assert result.horizontal_hold is not None
        assert result.horizontal_hold >= 0.35

    def test_gameplay_has_nothing_to_hold(self):
        result = sample(gameplay())
        assert (result.horizontal_hold or 0.0) < 0.2
        assert (result.vertical_hold or 0.0) < 0.2

    def test_a_menu_is_refused_for_holding_too(self):
        """The position test is what rejects a menu, and it applies to both."""
        from tests.test_split_detector import menu_screen

        result = sample(menu_screen())
        assert (result.horizontal_hold or 0.0) == pytest.approx(0.0)


def feed(state: SplitLayoutState, buf: bytearray, count: int, config) -> list[str]:
    seen = []
    for _ in range(count):
        state.update(sample(buf, config))
        seen.append(state.layout)
    return seen


class TestASplitStaysSplit:
    def config(self, **over):
        values = {"activate_samples": 3, "deactivate_samples": 5,
                  "hold_auto": False, "leave_auto": False}
        values.update(over)
        return DetectorConfig(**values)

    def test_weak_frames_do_not_drop_it(self):
        """Failed before: five weak samples in a row read FULL and took the
        layout with them, 2.5 seconds at the default rate."""
        config = self.config()
        state = SplitLayoutState(config=config)
        feed(state, horizontal_split(), 3, config)
        assert state.layout == HORIZONTAL_2

        seen = feed(state, weak_seam_split(), 20, config)
        assert set(seen) == {HORIZONTAL_2}

    def test_weak_frames_cannot_enter_on_their_own(self):
        config = self.config()
        state = SplitLayoutState(config=config)
        feed(state, weak_seam_split(), 20, config)
        assert state.layout == FULL

    def test_real_full_screen_content_still_leaves(self):
        config = self.config()
        state = SplitLayoutState(config=config)
        feed(state, horizontal_split(), 3, config)
        seen = feed(state, gameplay(), 5, config)
        assert seen[-1] == FULL
        assert seen[:-1] == [HORIZONTAL_2] * 4

    def test_losing_one_axis_of_four_is_a_change(self):
        config = self.config()
        state = SplitLayoutState(config=config)
        feed(state, quad_split(), 3, config)
        assert state.layout == QUAD_4
        feed(state, horizontal_split(), 5, config)
        assert state.layout == HORIZONTAL_2

    def test_samples_without_axis_readings_behave_as_before(self):
        """Hand-built samples -- the debounce tests, and an older source --
        carry a verdict and nothing else. They must still be believed."""
        state = SplitLayoutState(config=self.config())
        for _ in range(3):
            state.update(LayoutSample(VERTICAL_2, 0.9))
        assert state.layout == VERTICAL_2


class TestSmoothing:
    """The seam never moves; scene edges do. Averaging over time is what
    tells them apart, and a single frame cannot."""

    @staticmethod
    def busy_frame(seed: int) -> bytearray:
        """A strong seam with a dozen strong full-width edges that move every
        frame -- scrolling scenery, a fence, stairs going past."""
        buf = horizontal_split()
        rng = random.Random(seed)
        for _ in range(14):
            row = rng.randrange(4, HEIGHT - 6)
            if abs(row - HEIGHT // 2) < 8:
                continue
            fill_rect(buf, 0, row, WIDTH, row + 2, 250)
        return buf

    def test_one_busy_frame_alone_is_not_believed(self):
        assert sample(self.busy_frame(1)).layout == FULL

    def test_averaged_over_time_the_seam_stands_out(self):
        analyser = LayoutAnalyser(DetectorConfig(smoothing_s=2.0, hz=4.0))
        result = None
        for index in range(12):
            result = analyser.analyse(
                memoryview(self.busy_frame(index)), WIDTH, HEIGHT, STRIDE,
                now_ns=index * SECOND // 4,
            )
        assert result is not None and result.layout == HORIZONTAL_2

    def test_zero_turns_it_off(self):
        analyser = LayoutAnalyser(DetectorConfig(smoothing_s=0.0))
        result = None
        for index in range(12):
            result = analyser.analyse(
                memoryview(self.busy_frame(index)), WIDTH, HEIGHT, STRIDE,
                now_ns=index * SECOND // 4,
            )
        assert result is not None and result.layout == FULL

    def test_a_new_letterbox_starts_the_average_again(self):
        """Profiles measured inside different crops do not line up column for
        column; averaging across them would smear a seam into two."""
        analyser = LayoutAnalyser(DetectorConfig(smoothing_s=5.0))
        analyser.analyse(memoryview(horizontal_split()), WIDTH, HEIGHT, STRIDE, now_ns=0)
        assert analyser.smoother.samples == 1
        analyser.analyse(
            memoryview(horizontal_split()), WIDTH, HEIGHT, STRIDE, now_ns=SECOND,
            active=(43 / WIDTH, 0.0, (WIDTH - 86) / WIDTH, 1.0),
        )
        assert analyser.smoother.samples == 1


class TestItCalibratesItself:
    """Relearned every session -- nothing here persists."""

    def test_nothing_is_learned_from_too_few_samples(self):
        calibration = SplitCalibration()
        for _ in range(10):
            calibration.observe_seam(0.8)
            calibration.observe_noise(0.1)
        assert calibration.learned_hold(0.61) is None

    def test_the_hold_threshold_sits_between_seam_and_noise(self):
        calibration = SplitCalibration()
        rng = random.Random(3)
        for _ in range(80):
            calibration.observe_seam(0.55 + rng.random() * 0.35)
            calibration.observe_noise(rng.random() * 0.15)
        learned = calibration.learned_hold(0.61)
        assert learned is not None
        assert 0.25 < learned < 0.55

    @pytest.mark.parametrize("seam, noise", [(0.99, 0.95), (0.05, 0.0)])
    def test_it_is_bounded_whatever_it_sees(self, seam, noise):
        calibration = SplitCalibration()
        for _ in range(60):
            calibration.observe_seam(seam)
            calibration.observe_noise(noise)
        learned = calibration.learned_hold(0.61)
        assert learned is not None
        assert 0.20 <= learned <= 0.56

    def test_the_leave_delay_covers_the_dips_it_has_seen(self):
        calibration = SplitCalibration()
        calibration.observe_dip(6)
        calibration.observe_dip(3)
        assert calibration.learned_leave(manual=5, hz=2.0) == 12

    def test_the_leave_delay_is_never_below_the_manual_one(self):
        calibration = SplitCalibration()
        calibration.observe_dip(1)
        assert calibration.learned_leave(manual=5, hz=2.0) == 5

    def test_the_leave_delay_is_capped_at_twenty_seconds(self):
        calibration = SplitCalibration()
        calibration.observe_dip(500)
        assert calibration.learned_leave(manual=5, hz=2.0) == 40

    def test_reset_forgets_everything(self):
        calibration = SplitCalibration()
        for _ in range(60):
            calibration.observe_seam(0.8)
            calibration.observe_noise(0.1)
        calibration.observe_dip(9)
        calibration.reset()
        assert calibration.learned_hold(0.61) is None
        assert calibration.learned_leave(manual=5, hz=2.0) == 5

    def test_a_recovered_dip_is_learned_from_real_play(self):
        """The state records a dip only when the seam came back, so a real
        change of layout does not teach it to hold on longer."""
        config = DetectorConfig(
            activate_samples=3, deactivate_samples=5, hold_auto=False, leave_auto=True
        )
        state = SplitLayoutState(config=config)
        feed(state, horizontal_split(), 3, config)
        feed(state, gameplay(), 4, config)          # a dip of four
        feed(state, horizontal_split(), 1, config)  # it came back
        assert state.calibration.longest_dip() == 4
        assert state.leave_samples() == 8

    def test_learning_can_be_switched_off(self):
        config = DetectorConfig(hold=0.4, hold_auto=False, leave_auto=False)
        state = SplitLayoutState(config=config)
        for _ in range(100):
            state.calibration.observe_seam(0.9)
            state.calibration.observe_noise(0.05)
        state.calibration.observe_dip(30)
        assert state.hold_threshold() == pytest.approx(0.4)
        assert state.leave_samples() == config.deactivate_samples


class TestTheSnapshotCarriesTheAxes:
    def test_both_axes_are_reported_for_the_readouts(self):
        config = DetectorConfig()
        state = SplitLayoutState(config=config)
        state.update(sample(horizontal_split(), config))
        snap = state.snapshot()
        assert snap["h"] > 0.5
        assert snap["v"] < 0.2

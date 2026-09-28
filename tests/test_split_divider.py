"""A split drawn as a line, with things riding it.

Mario Kart 64 draws its two-player split as a black line a few pixels thick,
and puts the race-position portraits *on* it, with the lap and time text
against it. So the seam shows as two strong runs -- the top viewport meeting
the line and the line meeting the bottom viewport -- and whichever came out
sharper decided where the detector thought the seam was. The lower edge sits
two rows off centre, outside the centring tolerance that rejects menus.

Measured on 263 frames of a real race: the seam at 0.81-0.88 coverage
throughout, 186 frames scoring *zero*, and the layout flipping to full screen
and back six times in 90 seconds. Replayed through the detector at 10 Hz, the
old code held the split for 11.8% of the race and this one for 99.7% -- and
both read 0% split across 150 s of the same game's player-select screen and
one-player racing.

`split_profiles_mk64.json` holds the row profiles those frames produced --
numbers, not pictures -- so the real cases are pinned without shipping a
game's screenshots.
"""

from __future__ import annotations

import json
from pathlib import Path

from common.screen_regions import HORIZONTAL_2
from tests.test_split_detector import (
    HEIGHT,
    STRIDE,
    WIDTH,
    analyse,
    fill_rect,
    frame,
    textured,
)
from videoserver.layout import (
    HOLD_MIN_COVERAGE,
    MIN_COVERAGE,
    DetectorConfig,
    _central_band,
    _score_boundary,
    measure_profiles,
)

PROFILES = json.loads(
    (Path(__file__).parent / "split_profiles_mk64.json").read_text(encoding="utf-8")
)
TOLERANCE = DetectorConfig().tolerance
ENTRY = DetectorConfig().confidence


def _peak_offset(profile: list[float]) -> float:
    """How far from centre the single sharpest line near the middle sits --
    what the detector used to judge."""
    count = len(profile)
    low, high = int(count * 0.44), int(count * 0.56) + 1
    peak_at = max(range(low, high), key=lambda i: (profile[i], -i))
    return abs(peak_at / count - 0.5)


class TestTheRealRace:
    def test_every_frame_is_read_as_the_split_it_is(self):
        for name, profile in PROFILES["split_race"].items():
            confidence, position = _score_boundary(profile, TOLERANCE)
            assert confidence >= ENTRY, f"{name}: {confidence:.3f}"
            assert abs(position - 0.5) <= TOLERANCE, name

    def test_the_old_rule_would_have_refused_them(self):
        """The sharpest line alone sat outside the tolerance on these frames --
        the lower edge of the line, which is where the portraits and the time
        text made it strongest. The control for the test above."""
        offsets = [_peak_offset(profile) for profile in PROFILES["split_race"].values()]
        assert sum(1 for offset in offsets if offset > TOLERANCE) >= 2, offsets

    def test_the_menu_is_still_refused(self):
        """Player select: two rows of portraits with a gap across the middle,
        exactly the shape a looser rule would take for a split."""
        profile = PROFILES["player_select"]
        assert _score_boundary(profile, TOLERANCE)[0] == 0.0
        assert _score_boundary(profile, TOLERANCE, HOLD_MIN_COVERAGE)[0] == 0.0

    def test_one_player_racing_is_not_entered(self):
        """The strongest one-player frame of the capture, with the rank column
        down its side."""
        assert _score_boundary(PROFILES["one_player_race"], TOLERANCE)[0] < ENTRY


def _profile(count: int, runs: dict[tuple[int, int], float]) -> list[float]:
    profile = [0.05] * count
    for (first, last), value in runs.items():
        for index in range(first, last + 1):
            profile[index] = value
    return profile


class TestTheTwoEdgesOfOneLine:
    COUNT = 240                     # centre at index 120; tolerance 3.6 rows

    def test_a_line_is_judged_by_its_middle_when_neither_edge_is_centred(self):
        profile = _profile(self.COUNT, {(115, 117): 0.84, (123, 125): 0.84})

        confidence, position = _score_boundary(profile, TOLERANCE)

        assert confidence > 0.0
        assert abs(position - 0.5) <= TOLERANCE

    def test_either_edge_alone_would_be_refused(self):
        """The control: the pair above passes only because it is a pair."""
        for run in ((115, 117), (123, 125)):
            profile = _profile(self.COUNT, {run: 0.84})
            assert _score_boundary(profile, TOLERANCE)[0] == 0.0

    def test_unequal_runs_are_not_one_line(self):
        """Text beside the line touches one side of it only, and measured at
        0.60 of the line's strength at the median. Pairing it with the line
        would pull the band towards the text."""
        profile = _profile(self.COUNT, {(115, 117): 0.84, (123, 125): 0.55})

        assert _score_boundary(profile, TOLERANCE)[0] == 0.0

    def test_runs_too_far_apart_are_not_one_line(self):
        profile = _profile(self.COUNT, {(110, 112): 0.84, (128, 130): 0.84})

        assert _score_boundary(profile, TOLERANCE)[0] == 0.0

    def test_a_faint_line_near_the_middle_cannot_stand_in_for_a_strong_one(self):
        """What keeps "nearest the centre" from admitting a menu: a candidate
        has to be nearly as strong as the strongest line, not merely present."""
        profile = _profile(self.COUNT, {(114, 114): 0.95, (120, 120): 0.60})

        band_at, strength = _central_band(profile, 106, 135, 0.95, MIN_COVERAGE)

        assert band_at == 114.0
        assert strength == 0.95


def divider_race(*, rider_xs=(20, 70, 120, 170, 220, 270)) -> bytearray:
    """Two viewports split by a thick black line with sprites riding it.

    The top viewport is dark beside part of the line, so its edge of the line
    is the weaker one -- the arrangement that made the lower edge the peak.
    Text sits under the line on one side, as the time readout does.
    """
    buf = frame()
    half = HEIGHT // 2
    textured(buf, 0, 0, WIDTH, half, 7, base=150)
    textured(buf, 0, half, WIDTH, HEIGHT, 21, base=150)
    # The line: rows 90-93, so its lower edge sits two rows off centre.
    fill_rect(buf, 0, half, WIDTH, half + 4, 0)
    # A shadow on the road above part of the line: no step there. Enough to
    # make the upper edge the weaker one, as the real frames' text and HUD
    # did, and no more -- the real edges measured within 11% of each other in
    # 95% of frames, because the portraits cover both at once.
    fill_rect(buf, 280, half - 4, 320, half, 10)
    # Portraits riding the line: bright frame, busy inside.
    for x in rider_xs:
        fill_rect(buf, x, half - 6, x + 12, half + 10, 230)
        textured(buf, x + 2, half - 4, x + 10, half + 8, x, base=90)
    # Time text under the line, right-hand side.
    for x in range(150, 230, 4):
        fill_rect(buf, x, half + 7, x + 2, half + 11, 240)
    return buf


class TestASyntheticDivider:
    def test_it_is_read_as_a_split(self):
        sample = analyse(divider_race())

        assert sample.layout == HORIZONTAL_2
        assert abs(sample.horizontal_at - 0.5) <= 0.03

    def test_the_fixture_is_the_hard_case(self):
        """Its sharpest line is the lower edge, outside the tolerance -- or
        the test above proves nothing about this fix."""
        buf = divider_race()
        _, rows = measure_profiles(
            memoryview(buf), STRIDE, (0, WIDTH - 1, 0, HEIGHT - 1)
        )
        assert _peak_offset(rows) > TOLERANCE

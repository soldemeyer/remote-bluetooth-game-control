"""The debug overlay's geometry and wording.

Pure arithmetic and string building, so this runs anywhere and needs neither
Qt nor a capture device. That split is the point: a box drawn perfectly in the
wrong place looks exactly like one drawn in the right place, and the only
defence is to check the numbers rather than the picture.
"""

from __future__ import annotations

from common.screen_regions import Rect
from videoserver.playervision.overlay import (
    STRONG_CONFIDENCE,
    TONE_IDENTIFIED,
    TONE_UNIDENTIFIED,
    TONE_WEAK,
    box_pixels,
    breakdown_lines,
    overlay_boxes,
)
from videoserver.playervision.types import (
    Judgement,
    SignalScore,
    TrackedPlayer,
)


def _row(track_id=1, player_id=0, confidence=0.0, source="none",
         region="", box=None):
    return TrackedPlayer(
        track_id=track_id,
        box=box or Rect(0.1, 0.2, 0.3, 0.4),
        player_id=player_id,
        confidence=confidence,
        region=region,
        source=source,
    )


class TestWhatIsDrawn:
    def test_every_track_is_drawn_including_the_nameless_ones(self):
        """The refused ones are the whole reason to look at this.

        The player-facing path drops a track with no player; here it is the
        most informative thing on screen when the question is why nobody is
        being labelled.
        """
        boxes = overlay_boxes([
            _row(track_id=1, player_id=2, confidence=0.9, source="viewport"),
            _row(track_id=2),
        ])

        assert [b.track_id for b in boxes] == [1, 2]
        assert boxes[1].title == "unidentified"

    def test_an_identified_box_names_the_player_and_the_signal(self):
        box = overlay_boxes([
            _row(player_id=3, confidence=0.87, source="appearance")
        ])[0]

        assert box.title == "Player 3"
        assert "0.87" in box.detail and "appearance" in box.detail
        assert box.source == "appearance"

    def test_a_nameless_box_carries_the_reason_not_a_blank(self):
        """"unidentified" alone sends somebody to the wrong subsystem."""
        judgement = Judgement(
            track_id=1,
            note="no player map: the Bluetooth server has not said who is playing",
        )

        box = overlay_boxes([_row()], [judgement])[0]

        assert "no player map" in box.detail

    def test_the_reason_is_cut_to_fit_rather_than_covering_the_entity(self):
        judgement = Judgement(track_id=1, note="x" * 400)

        box = overlay_boxes([_row()], [judgement])[0]

        assert len(box.detail) < 60

    def test_losing_the_reasoning_costs_the_reason_and_not_the_box(self):
        """A judgement that did not arrive must not remove the rectangle."""
        boxes = overlay_boxes([_row(track_id=9)], [])

        assert len(boxes) == 1
        assert boxes[0].detail


class TestTheJoin:
    def test_rows_and_reasoning_are_matched_by_track_not_by_position(self):
        """Position would shift every box onto the wrong entity, silently.

        Both arrive in one message from an isolated backend, so they are
        normally parallel -- which is exactly what makes a positional join
        look correct right up until one entry is dropped as malformed.
        """
        rows = [_row(track_id=7), _row(track_id=8)]
        # Deliberately the other order, and only one of them.
        judgements = [Judgement(track_id=8, note="the eighth track")]

        boxes = {b.track_id: b for b in overlay_boxes(rows, judgements)}

        assert "eighth" in boxes[8].detail
        assert "eighth" not in boxes[7].detail


class TestTone:
    def test_a_strong_identification_reads_as_settled(self):
        box = overlay_boxes([
            _row(player_id=1, confidence=0.95, source="viewport")
        ])[0]
        assert box.tone == TONE_IDENTIFIED

    def test_a_held_label_is_separable_from_a_recognised_one(self):
        """Continuity publishes at 0.70 and is a label *held*, not matched.

        An operator deciding whether to believe the screen needs that at a
        glance, without reading the number.
        """
        box = overlay_boxes([
            _row(player_id=1, confidence=0.70, source="continuity")
        ])[0]

        assert box.tone == TONE_WEAK
        assert box.tone != TONE_IDENTIFIED
        assert 0.70 < STRONG_CONFIDENCE

    def test_no_player_is_its_own_tone(self):
        assert overlay_boxes([_row()])[0].tone == TONE_UNIDENTIFIED
        assert not overlay_boxes([_row()])[0].identified


class TestPixels:
    def test_a_box_maps_onto_the_surface(self):
        assert box_pixels(Rect(0.0, 0.0, 1.0, 1.0), 640, 360) == (0, 0, 640, 360)
        assert box_pixels(Rect(0.5, 0.5, 0.5, 0.5), 640, 360) == (320, 180, 320, 180)

    def test_a_box_hanging_off_the_edge_is_clamped_inside(self):
        """A detector may report a box a hair outside the frame."""
        x, y, w, h = box_pixels(Rect(0.9, 0.9, 0.5, 0.5), 100, 100)

        assert x + w <= 100 and y + h <= 100

    def test_a_degenerate_box_still_draws(self):
        """Zero width draws as nothing, which reads as "not detected"."""
        _x, _y, w, h = box_pixels(Rect(0.5, 0.5, 0.0, 0.0), 100, 100)

        assert w >= 1 and h >= 1

    def test_a_negative_box_does_not_produce_a_negative_rectangle(self):
        x, y, w, h = box_pixels(Rect(-0.5, -0.5, 0.2, 0.2), 100, 100)

        assert x >= 0 and y >= 0 and w >= 1 and h >= 1


class TestTheBreakdown:
    def test_the_winning_signal_is_marked(self):
        judgement = Judgement(
            track_id=4, player_id=2, confidence=0.92, source="viewport",
            scores=(
                SignalScore("viewport", 2, 0.92, used=True),
                SignalScore("appearance", 1, 0.41, note="below the floor"),
            ),
        )

        lines = "\n".join(breakdown_lines(judgement))

        assert "Player 2" in lines and "viewport" in lines
        assert "->" in lines
        # The loser is shown too: it is the half that explains a near miss.
        assert "0.41" in lines and "below the floor" in lines

    def test_a_signal_that_never_looked_says_so(self):
        """Absence reads as "found nothing", which points at the model.

        The truth is usually that a stronger signal had already settled the
        track and this one was never asked.
        """
        judgement = Judgement(
            track_id=1, player_id=1, confidence=0.9, source="viewport",
            scores=(
                SignalScore("viewport", 1, 0.9, used=True),
                SignalScore("appearance", note="a stronger signal had already claimed this track"),
            ),
        )

        lines = "\n".join(breakdown_lines(judgement))

        assert "appearance" in lines and "already claimed" in lines

    def test_a_track_nothing_scored_says_that_rather_than_showing_blank(self):
        lines = "\n".join(breakdown_lines(Judgement(track_id=3)))

        assert "no signal scored this track" in lines

    def test_the_note_is_shown_in_full_here(self):
        """The box truncates; the panel is where the whole sentence lives."""
        note = "no player map: the Bluetooth server has not said who is playing"
        lines = "\n".join(breakdown_lines(Judgement(track_id=1, note=note)))

        assert note in lines

"""The colour signature: what a character looks like, without a model.

See `videoserver.playervision.signature` for the measurement that replaced
an ImageNet embedder with it.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy", reason="the playervision extra is not installed")

from videoserver.playervision.identity import cosine  # noqa: E402
from videoserver.playervision.signature import (  # noqa: E402
    APPEARANCE_FLOOR,
    SIGNATURE_LENGTH,
    colour_signature,
)


def patch(rgb, size=(24, 24)):
    return np.full((*size, 3), rgb, dtype="uint8")


def character(cap, body=(255, 255, 255), size=24):
    """A cap over a body over black tyres, like a kart seen from behind."""
    picture = np.zeros((size * 3, size, 3), dtype="uint8")
    picture[:size] = cap
    picture[size:2 * size] = body
    return picture


class TestWhatItSees:
    def test_a_red_and_a_green_character_are_far_apart(self):
        """Two thirds of this picture is the white body and black tyres they
        share, and they still sit well under the floor. The real Mario and
        Luigi, with less in common, measured 0.38."""
        mario = colour_signature(character((220, 20, 20)))
        luigi = colour_signature(character((20, 200, 40)))
        assert cosine(mario, luigi) < APPEARANCE_FLOOR - 0.15

    def test_the_same_character_at_another_size_is_the_same(self):
        near = colour_signature(character((220, 20, 20), size=40))
        far = colour_signature(character((220, 20, 20), size=8))
        assert cosine(near, far) > 0.99

    def test_red_either_side_of_the_hue_wrap_is_one_colour(self):
        """Red sits at 0 and 360 degrees. Without the half-bin shift two
        crops of one cap could split their weight across the first and last
        bins by a shading change."""
        just_below = colour_signature(patch((230, 10, 30)))    # ~355 degrees
        just_above = colour_signature(patch((230, 30, 10)))    # ~5 degrees
        assert cosine(just_below, just_above) > 0.99

    def test_grey_road_says_nothing(self):
        """No hue, not white, not black: a box of road is not an appearance,
        and must not be a weak match to everybody."""
        assert colour_signature(patch((120, 120, 120))) is None

    def test_a_sliver_says_nothing(self):
        assert colour_signature(patch((220, 20, 20), size=(3, 3))) is None

    def test_white_and_black_count(self):
        """A white glove and a black tyre are part of what a character looks
        like -- two characters in the same colour differ in how much of each."""
        mostly_white = colour_signature(character((220, 20, 20), body=(250, 250, 250)))
        mostly_black = colour_signature(character((220, 20, 20), body=(5, 5, 5)))
        assert cosine(mostly_white, mostly_black) < 0.95

    def test_it_is_a_unit_vector_of_fixed_length(self):
        vector = colour_signature(character((20, 40, 220)))
        assert len(vector) == SIGNATURE_LENGTH
        assert sum(v * v for v in vector) == pytest.approx(1.0)

    def test_nonsense_input_is_none_not_an_exception(self):
        assert colour_signature(np.zeros((10,), dtype="uint8")) is None
        assert colour_signature(np.zeros((1, 1, 3), dtype="uint8")) is None

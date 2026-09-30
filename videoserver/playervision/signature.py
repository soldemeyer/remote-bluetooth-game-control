"""What a character looks like, as colour: the appearance descriptor.

numpy only, no model. It replaced an ImageNet embedder (MobileNetV2) as the
thing galleries hold, because on game graphics that embedder could not tell
the characters apart and this can. Measured on a real Mario Kart 64 frame,
each crop scored against the owner's kart as their own camera held it:

    crop                          MobileNetV2          colour signature
                                  Mario   Luigi        Mario   Luigi
    Mario, distant, viewport 2     0.90    0.74         0.98    0.49
    Mario, viewport 3              0.83    0.71         0.97    0.46
    Luigi's head, viewport 1       0.66    0.71         0.24    0.88
    Luigi at a viewport's edge     0.74    0.73         0.37    0.91
    Peach -- nobody's player       0.87    0.84         0.64    0.47
    HUD text, numerals, minimap    0.54-0.67            0.01-0.69

The embedder rated Peach *more* like Mario than Mario was, and put everything
in one band; the signature puts every true match at 0.88 or above and
everything else at 0.80 or below. That gap is `APPEARANCE_FLOOR`.

It is game-independent in the way the rest of this subsystem is: it knows
nothing about karts or plumbers, only that the thing the camera holds is red
and white and black, and that player colours are what split-screen games use
to tell players apart. Two players who picked the same character get the same
signature, which is exactly the case the ambiguity rule refuses.

The descriptor, per crop:

* a hue histogram over the **chromatic** pixels -- saturated and bright enough
  that hue means something. Grey road, shadow and mid-tones carry no hue and
  are left out, so the background a box inevitably includes counts for little;
* the share of **white** and of **black** pixels, because a white glove, a
  black tyre and a red cap are all part of what a character looks like;
* L2-normalised, so the identity layer's cosine is the comparison.
"""

from __future__ import annotations

import functools

__all__ = [
    "APPEARANCE_FLOOR",
    "HUE_BINS",
    "SIGNATURE_LENGTH",
    "classify",
    "colour_signature",
    "signature_of",
]

#: Hue bins. Twelve is 30 degrees each: fine enough to separate red, orange,
#: yellow, green and blue characters, coarse enough that a shading change or a
#: compression artefact does not move a colour into the next bin.
HUE_BINS = 12

#: Hue histogram, then white, then black.
SIGNATURE_LENGTH = HUE_BINS + 2

#: What an appearance match must reach with this descriptor, whatever the
#: operator's publishing floor. Every true match measured was 0.88 or above
#: and everything else 0.80 or below; 0.85 sits in that gap. Non-negative
#: histograms have a high cosine baseline -- two unrelated crops routinely
#: score 0.6 -- so a floor below this band would name things by accident.
APPEARANCE_FLOOR = 0.85

#: Saturation and value a pixel needs before its hue counts.
_CHROMA_SATURATION = 0.35
_CHROMA_VALUE = 0.25
#: White: nearly unsaturated and bright. Black: dark, whatever its hue.
_WHITE_SATURATION = 0.20
_WHITE_VALUE = 0.75
_BLACK_VALUE = 0.20

#: The fewest informative pixels a signature is worth computing from. Below
#: this a crop is a sliver or all road, and a signature from it would be noise
#: that happens to have a cosine.
_MIN_PIXELS = 24


#: The class a pixel with no hue, and neither white nor black, is given --
#: grey road, shadow, mid-tones. Counted nowhere.
_UNCOUNTED = SIGNATURE_LENGTH


def _classify_float(rgb):
    """The exact rule, on float RGB in 0..1. Used to build `_lookup` once."""
    import numpy as np

    red, green, blue = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    high = rgb.max(axis=-1)
    low = rgb.min(axis=-1)
    chroma = high - low
    saturation = np.where(high > 0.0, chroma / np.maximum(high, 1e-6), 0.0)
    span = np.maximum(chroma, 1e-6)
    hue = np.where(
        high == red,
        ((green - blue) / span) % 6.0,
        np.where(high == green, (blue - red) / span + 2.0, (red - green) / span + 4.0),
    ) / 6.0
    # Half a bin of shift puts pure red in the *middle* of bin 0 rather than
    # on the wrap between the first and last bins, where two crops of the
    # same red cap would split their weight between two bins by chance.
    hue = (hue + 0.5 / HUE_BINS) % 1.0
    bins = np.minimum((hue * HUE_BINS).astype(np.uint8), HUE_BINS - 1)

    # The three are disjoint by their thresholds, so the order of the writes
    # decides nothing.
    classes = np.full(high.shape, _UNCOUNTED, dtype=np.uint8)
    chromatic = (saturation > _CHROMA_SATURATION) & (high > _CHROMA_VALUE)
    classes[chromatic] = bins[chromatic]
    classes[(saturation < _WHITE_SATURATION) & (high > _WHITE_VALUE)] = HUE_BINS
    classes[high < _BLACK_VALUE] = HUE_BINS + 1
    return classes


@functools.lru_cache(maxsize=1)
def _lookup():
    """The class of every colour at 5 bits a channel -- 32768 entries.

    Built once from `_classify_float`, at each cell's centre. Classifying a
    frame is then a table lookup per pixel rather than float colour maths:
    measured, 14 ms a sample for the float rule at half resolution. Five bits
    moves a colour by at most four levels a channel, far inside every
    threshold's margin for anything a player would call a different colour.
    """
    import numpy as np

    levels = (np.arange(32, dtype=np.float32) * 8.0 + 4.0) / 255.0
    red, green, blue = np.meshgrid(levels, levels, levels, indexing="ij")
    table = _classify_float(np.stack((red, green, blue), axis=-1)).reshape(-1)
    table.setflags(write=False)
    return table


def classify(pixels, step: int = 1):
    """Every pixel's class, once: a hue bin, white, black, or uncounted.

    ``H x W x 3`` uint8 RGB in, ``H/step x W/step`` uint8 out. What makes a
    frame's signatures cheap: a sample carries around thirty boxes that
    overlap, and converting each box's pixels to hue on its own cost 19 ms a
    sample -- more than the GPU spent running the detector five times. Done
    once, at half resolution (a histogram does not need every pixel), through
    `_lookup`, each box is then a count.
    """
    import numpy as np

    rgb = np.asarray(pixels)
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        return np.zeros((0, 0), dtype=np.uint8)
    step = max(1, int(step))
    rgb = rgb[::step, ::step, :3]
    index = (
        (rgb[..., 0] >> 3).astype(np.uint16) << 10
        | (rgb[..., 1] >> 3).astype(np.uint16) << 5
        | (rgb[..., 2] >> 3).astype(np.uint16)
    )
    return _lookup()[index]


def signature_of(classes, left: int, top: int, right: int, bottom: int,
                 *, weight: int = 1) -> tuple[float, ...] | None:
    """The descriptor of one box of a `classify` map, or None.

    ``weight`` is how many pixels each class stands for -- the square of the
    step it was classified at -- so the "too few pixels to say anything"
    floor means the same whatever resolution the map was made at.
    """
    import numpy as np

    region = classes[max(0, top):max(0, bottom), max(0, left):max(0, right)]
    if region.size == 0:
        return None
    counts = np.bincount(region.ravel(), minlength=_UNCOUNTED + 1)[:_UNCOUNTED]
    if counts.sum() * weight < _MIN_PIXELS:
        return None
    vector = counts.astype(np.float64)
    norm = float(np.linalg.norm(vector))
    if norm <= 0.0:
        return None
    return tuple(float(value) for value in vector / norm)


def colour_signature(pixels) -> tuple[float, ...] | None:
    """The descriptor of an ``H x W x 3`` uint8 RGB crop, or None.

    None when the crop has too few informative pixels to say anything, which
    identity treats exactly as "no appearance" -- never as a weak match.
    """
    import numpy as np

    rgb = np.asarray(pixels)
    if rgb.ndim != 3 or rgb.shape[2] < 3 or rgb.shape[0] < 2 or rgb.shape[1] < 2:
        return None
    classes = classify(rgb)
    return signature_of(classes, 0, 0, classes.shape[1], classes.shape[0])

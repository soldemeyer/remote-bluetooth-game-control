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

__all__ = [
    "APPEARANCE_FLOOR",
    "HUE_BINS",
    "SIGNATURE_LENGTH",
    "colour_signature",
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


def colour_signature(pixels) -> tuple[float, ...] | None:
    """The descriptor of an ``H x W x 3`` uint8 RGB crop, or None.

    None when the crop has too few informative pixels to say anything, which
    identity treats exactly as "no appearance" -- never as a weak match.
    """
    import numpy as np

    rgb = np.asarray(pixels)
    if rgb.ndim != 3 or rgb.shape[2] < 3 or rgb.shape[0] < 2 or rgb.shape[1] < 2:
        return None
    flat = rgb[:, :, :3].reshape(-1, 3).astype(np.float32) / 255.0

    high = flat.max(axis=1)
    low = flat.min(axis=1)
    chroma = high - low
    saturation = np.where(high > 0.0, chroma / np.maximum(high, 1e-6), 0.0)

    red, green, blue = flat[:, 0], flat[:, 1], flat[:, 2]
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

    chromatic = (saturation > _CHROMA_SATURATION) & (high > _CHROMA_VALUE)
    white = (saturation < _WHITE_SATURATION) & (high > _WHITE_VALUE)
    black = high < _BLACK_VALUE

    histogram = np.histogram(hue[chromatic], bins=HUE_BINS, range=(0.0, 1.0))[0]
    vector = np.concatenate(
        [histogram.astype(np.float64), [float(white.sum()), float(black.sum())]]
    )
    if vector.sum() < _MIN_PIXELS:
        return None
    norm = float(np.linalg.norm(vector))
    if norm <= 0.0:
        return None
    return tuple(float(value) for value in vector / norm)

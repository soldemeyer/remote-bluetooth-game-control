"""The coloured backdrop everything else sits on.

**This is what makes the glass read as glass.** A translucent panel needs
something worth seeing through it; over a flat near-black it is just a slightly
lighter rectangle, which is what the first pass at this theme looked like. So
the window paints a saturated gradient with soft orbs floating in it, and the
panels above are genuinely translucent over that.

### Why there is no blur here

Qt has no backdrop-filter, and `QGraphicsBlurEffect` re-renders its source on
every repaint of the region beneath it -- which in this application is the
video surface, at frame rate. That is the one effect the measured paint budget
rules out.

It is not needed. Backdrop blur exists to stop *detail* behind the glass from
competing with the content on it, and this backdrop has no detail: it is built
from linear and radial gradients, so it is already soft everywhere. Blurring a
gradient changes almost nothing. What blur also gives you is the frosted
speckle, and that comes from the noise overlay below instead -- for the cost of
one small tiling pixmap, generated once.

### Why it is cached

Rendering four large radial gradients is tens of milliseconds. Doing it per
paint would make dragging the window crawl. The pixmap is regenerated only when
the size changes, so the steady-state cost of the whole backdrop is one blit.
"""

from __future__ import annotations

import random

from PySide6.QtCore import QPointF, QRect, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

from common.design.tokens import (
    ORB_MID_STOP,
    ORB_MID_STRENGTH,
    ORBS,
    VIGNETTE_ALPHA,
    VIGNETTE_START,
)
from qtui.theme import qcolor

__all__ = ["BackdropWidget", "backdrop_pixmap", "noise_tile", "reset_cache"]

#: The orb table lives in `common.design.tokens` so the CSS generator can
#: render the same one -- see ORBS there for why.
_ORBS = ORBS

#: Side of the tiling noise square. Small enough to be cheap, large enough that
#: the repeat is not visible as a grid.
_NOISE = 128

#: How strong the frosted speckle is. Above about 5% it stops reading as
#: texture and starts reading as a dirty screen.
_NOISE_ALPHA = 9

_cache: dict[tuple[int, int], QPixmap] = {}
_noise: QPixmap | None = None


def noise_tile() -> QPixmap:
    """A small tiling square of monochrome noise, generated once.

    This is the frost. Deterministic seed so the texture is identical in every
    window and in every screenshot -- a backdrop that differs run to run makes
    visual comparison useless.
    """
    global _noise
    if _noise is not None:
        return _noise

    image = QImage(_NOISE, _NOISE, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    rng = random.Random(0x5EED)
    for y in range(_NOISE):
        for x in range(_NOISE):
            value = rng.randint(140, 255)
            image.setPixelColor(x, y, QColor(value, value, value, _NOISE_ALPHA))
    _noise = QPixmap.fromImage(image)
    return _noise


def backdrop_pixmap(width: int, height: int, ratio: float = 1.0) -> QPixmap:
    """The backdrop at a given size, cached.

    `ratio` is the device pixel ratio: the pixmap is rendered at physical
    resolution and tagged, so the gradient does not band on a high-DPI screen.
    """
    key = (max(1, int(width * ratio)), max(1, int(height * ratio)))
    cached = _cache.get(key)
    if cached is not None:
        return cached

    pixmap = QPixmap(key[0], key[1])
    pixmap.setDevicePixelRatio(ratio)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # Corner to corner, so the hue shifts across the window rather than down it
    # -- the diagonal is what stops it looking like a web page header.
    base = QLinearGradient(0.0, 0.0, float(width), float(height))
    base.setColorAt(0.0, qcolor("background-sunken"))
    base.setColorAt(0.5, qcolor("backdrop-1"))
    base.setColorAt(1.0, qcolor("backdrop-3"))
    painter.fillRect(QRect(0, 0, width, height), QBrush(base))

    # The orbs. Additive-ish: each is a radial gradient falling to fully
    # transparent, so they blend into the base instead of sitting on it.
    span = max(width, height)
    for fx, fy, fr, token, strength in _ORBS:
        centre = QPointF(fx * width, fy * height)
        radius = fr * span
        colour = qcolor(token)
        glow = QRadialGradient(centre, radius)
        inner = QColor(colour)
        inner.setAlphaF(strength)
        mid = QColor(colour)
        mid.setAlphaF(strength * ORB_MID_STRENGTH)
        outer = QColor(colour)
        outer.setAlphaF(0.0)
        glow.setColorAt(0.0, inner)
        glow.setColorAt(ORB_MID_STOP, mid)
        glow.setColorAt(1.0, outer)
        painter.setBrush(QBrush(glow))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(centre, radius, radius)

    # A dark vignette at the bottom, so the status bar and the drawer's lower
    # edge keep their contrast wherever an orb happens to land.
    shade = QLinearGradient(0.0, height * VIGNETTE_START, 0.0, float(height))
    shade.setColorAt(0.0, QColor(0, 0, 0, 0))
    shade.setColorAt(1.0, QColor(0, 0, 0, round(VIGNETTE_ALPHA * 255)))
    painter.fillRect(QRect(0, 0, width, height), QBrush(shade))

    painter.setBrush(QBrush(noise_tile()))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRect(QRect(0, 0, width, height))
    painter.end()

    # One entry. The window is the only caller and it only ever wants its
    # current size, so keeping older sizes is just held memory -- and these are
    # full-window pixmaps.
    _cache.clear()
    _cache[key] = pixmap
    return pixmap


def reset_cache() -> None:
    """Forget the rendered backdrop. Called when the theme changes."""
    _cache.clear()


class BackdropWidget(QWidget):
    """A widget that paints the backdrop and nothing else.

    Used as the window's central widget so every panel above it is composited
    over real colour. It sets `WA_StyledBackground` off deliberately: the
    stylesheet must not paint a flat fill over what this draws.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAutoFillBackground(False)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.drawPixmap(
            0, 0, backdrop_pixmap(self.width(), self.height(), self.devicePixelRatioF())
        )

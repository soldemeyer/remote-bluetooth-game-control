"""A level meter for the captured audio.

The question this answers is the one every other audio indicator gets wrong:
**is sound actually reaching the stream?** "Audio: on" means a thread is alive,
and a muted microphone, a capture card on the wrong input, or a console with
its volume down all satisfy that while sending pure silence. A meter is the
only readout where the correct state and the broken state look different.

Kept deliberately plain -- a bar, a peak tick, a scale -- because it is glanced
at rather than studied, and because a meter that needs explaining has failed.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from qtui.theme import qcolor
from PySide6.QtGui import QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

#: Where the bar changes colour. Not a technical limit -- Opus does not clip at
#: -6 dBFS -- but the point past which a capture card's input is usually too hot
#: and the operator should turn something down.
_WARN = 0.70
_HOT = 0.90

#: How fast the floating peak tick falls back, per repaint. Slow enough to read
#: at a glance, fast enough not to look stuck.
_PEAK_DECAY = 0.02


class LevelMeter(QWidget):
    """Horizontal bar showing RMS level with a floating peak marker."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(18)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._level = 0.0
        self._peak = 0.0
        self._live = False
        self.setToolTip(
            "Audio reaching the encoder. Silence here with the capture running "
            "means the device is muted or on the wrong input."
        )

    def set_level(self, rms: float, peak: float, *, live: bool = True) -> None:
        """Update the meter. ``live`` is False when no audio is arriving at all."""
        self._level = max(0.0, min(float(rms), 1.0))
        incoming = max(0.0, min(float(peak), 1.0))
        # The peak tick holds the highest recent value and eases down, so a
        # transient is still visible a moment after it happened.
        self._peak = incoming if incoming >= self._peak else max(
            self._peak - _PEAK_DECAY, incoming
        )
        self._live = bool(live)
        self.update()

    def clear(self) -> None:
        self._level = 0.0
        self._peak = 0.0
        self._live = False
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        rect = self.rect().adjusted(0, 0, -1, -1)
        painter.fillRect(rect, qcolor("surface-glass", over="background-base"))
        painter.setPen(qcolor("border-subtle", over="background-base"))
        painter.drawRect(rect)

        inner = QRectF(rect).adjusted(1, 1, -1, -1)

        if not self._live:
            painter.setPen(qcolor("text-muted"))
            painter.drawText(inner, Qt.AlignmentFlag.AlignCenter, "no audio")
            painter.end()
            return

        if self._level > 0:
            gradient = QLinearGradient(inner.left(), 0, inner.right(), 0)
            gradient.setColorAt(0.0, qcolor("success"))
            gradient.setColorAt(_WARN, qcolor("success"))
            gradient.setColorAt(min(_HOT, 0.999), qcolor("warning"))
            gradient.setColorAt(1.0, qcolor("error"))

            filled = QRectF(inner)
            filled.setWidth(inner.width() * self._level)
            painter.fillRect(filled, gradient)

        if self._peak > 0:
            x = inner.left() + inner.width() * self._peak
            painter.setPen(qcolor("text-primary"))
            painter.drawLine(int(x), int(inner.top()), int(x), int(inner.bottom()))

        # Scale marks, so the bar means something without a legend.
        painter.setPen(qcolor("border-strong", over="background-base"))
        for fraction in (0.25, 0.5, _WARN, _HOT):
            x = int(inner.left() + inner.width() * fraction)
            painter.drawLine(x, int(inner.bottom()) - 3, x, int(inner.bottom()))

        painter.end()

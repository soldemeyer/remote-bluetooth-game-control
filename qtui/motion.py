"""Small, cheap interaction animations.

Qt stylesheets have no `transition`, so motion here is explicit. Three rules,
all of them consequences of this application sharing a process with a 500 Hz
input loop and a video decoder:

1. **Nothing animates continuously.** Every animation here is a one-shot
   response to something the user did, and it stops.
2. **Nothing animates over live video.** A widget over the video surface
   repaints the surface's region under it, at whatever rate it animates.
3. **Animate a value, not a stylesheet.** Re-running `setStyleSheet` per frame
   re-parses and re-polishes the whole widget tree beneath it; animating a
   float that a `paintEvent` reads costs one repaint of one widget.

`prefers_reduced_motion()` is honoured throughout -- and it is also what the
tests use, since an animation that is still running when an assertion fires is
a flake generator.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QEasingCurve, QVariantAnimation, Qt
from PySide6.QtWidgets import QWidget

from common.design.tokens import Motion

__all__ = ["Hoverable", "animator", "prefers_reduced_motion", "run"]


def prefers_reduced_motion() -> bool:
    """Whether to skip animation and jump straight to the end state.

    Honours `QT_REDUCED_MOTION` / `RBGC_REDUCED_MOTION`. There is no portable
    Qt query for the OS setting, so this is the explicit opt-out until there
    is.
    """
    return bool(
        os.environ.get("RBGC_REDUCED_MOTION") or os.environ.get("QT_REDUCED_MOTION")
    )


def animator(
    parent: QWidget,
    on_value,
    *,
    duration: int = Motion.FAST,
    curve: QEasingCurve.Type = QEasingCurve.Type.OutCubic,
) -> QVariantAnimation:
    """One reusable animation, owned by `parent`.

    **Never `DeleteWhenStopped` for an animation something keeps a reference
    to.** That policy destroys the C++ object the moment it stops, while the
    Python wrapper stays alive and looks perfectly usable -- so the next call
    to `stop()` or `setStartValue()` runs against freed memory. It does not
    raise: it **segfaults**, which was measured here, and in an application it
    presented as buttons that worked "part of the time" and a panel that would
    not reopen.

    Reusing one animation per widget also means no allocation per hover, and
    the object dies with its parent like every other child.
    """
    anim = QVariantAnimation(parent)
    anim.setDuration(duration)
    anim.setEasingCurve(curve)
    anim.valueChanged.connect(on_value)
    return anim


def run(anim: QVariantAnimation | None, start: float, end: float, on_value) -> None:
    """Drive a reusable animation from `start` to `end`, or jump to the end."""
    if anim is None or prefers_reduced_motion() or start == end:
        on_value(end)
        return
    anim.stop()
    anim.setStartValue(float(start))
    anim.setEndValue(float(end))
    anim.start()


class Hoverable:
    """Mixin giving a widget an animated 0..1 `hover` value.

    The widget's `paintEvent` reads `self.hover` and draws accordingly, so the
    cost of the animation is one repaint of one widget per frame for ~120 ms
    and nothing at rest.

    Cooperative: `enterEvent`/`leaveEvent` call up the MRO, so a subclass that
    also wants them keeps working.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.hover = 0.0
        # Created once and reused. See `animator` for why this is not made and
        # destroyed per hover.
        self._hover_anim = animator(self, self._set_hover, duration=Motion.FAST)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def _set_hover(self, value) -> None:
        self.hover = float(value)
        self.update()

    def _animate_hover(self, to: float) -> None:
        run(self._hover_anim, self.hover, to, self._set_hover)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._animate_hover(0.0)
        super().leaveEvent(event)

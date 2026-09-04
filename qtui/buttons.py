"""Buttons and toggles, as thin subclasses over the stylesheet's variants.

The styling lives in ``qtui.theme``; these exist so a call site reads
``PrimaryButton("Connect")`` rather than setting a dynamic property and
remembering to re-polish. They also carry the things a stylesheet cannot: a
pointing cursor, an accessible name, and the busy state.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QPushButton, QWidget

from common.design.tokens import Motion, Radius, Space
from qtui.motion import Hoverable, animator, run
from qtui.theme import icon as make_icon
from qtui.theme import restyle

__all__ = [
    "DangerButton",
    "GhostButton",
    "IconButton",
    "PrimaryButton",
    "SecondaryButton",
]


class _Button(Hoverable, QPushButton):
    """Shared behaviour: variant property, cursor, a busy state, and motion.

    **The stylesheet still draws the button.** `paintEvent` calls up first and
    then washes an animated veil over the result, so every QSS rule -- variant
    fills, focus rings, disabled colours, the icon and text layout Qt does for
    us -- keeps working, and the animation is one extra rounded rect.

    Repainting a button on its own hover is cheap and bounded; it stops when
    the pointer stops. The alternative, animating `setStyleSheet`, re-parses
    and re-polishes the widget tree on every frame.
    """

    VARIANT = ""

    #: How far hover and press move the veil. Small on purpose: this is meant
    #: to be felt rather than watched.
    HOVER_VEIL = 0.07
    PRESS_VEIL = 0.10

    def __init__(
        self,
        text: str = "",
        icon_name: str = "",
        *,
        parent: QWidget | None = None,
        tooltip: str = "",
    ) -> None:
        super().__init__(text, parent)
        self._busy_text = ""
        self._icon_name = icon_name

        if self.VARIANT:
            self.setProperty("variant", self.VARIANT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if icon_name:
            self.setIcon(make_icon(icon_name, self._icon_token()))
            self.setIconSize(QSize(18, 18))
        if tooltip:
            self.setToolTip(tooltip)
        self._press = 0.0
        self._press_anim = animator(self, self._set_press, duration=Motion.INSTANT)
        if text:
            self.setAccessibleName(text)

    def _icon_token(self) -> str:
        return "text-primary"

    # -- busy --------------------------------------------------------------

    # -- motion ------------------------------------------------------------

    def _set_press(self, value) -> None:
        self._press = float(value)
        self.update()

    def _animate_press(self, to: float) -> None:
        run(self._press_anim, self._press, to, self._set_press)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._animate_press(1.0)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._animate_press(0.0)
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().paintEvent(event)
        if not self.isEnabled():
            return
        # Press *darkens* and hover lightens, which is the way round every
        # physical control works -- a button that brightens as it goes down
        # reads as releasing rather than pressing.
        amount = self.HOVER_VEIL * self.hover
        veil = QColor(255, 255, 255, int(255 * amount))
        if self._press > 0.0:
            veil = QColor(0, 0, 0, int(255 * self.PRESS_VEIL * self._press))
        if veil.alpha() == 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(
            QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
            Radius.CONTROL,
            Radius.CONTROL,
        )
        painter.fillPath(path, veil)

    def set_busy(self, busy: bool, text: str = "") -> None:
        """Disable and relabel while an action is in flight.

        **The label is replaced, not the width.** A button that resizes under
        the pointer moves its neighbours out from under the next click, which
        is exactly the problem `withPending()` in the web GUI was written to
        avoid; this is the same behaviour on this side.

        The floor is the *natural* width of the current label, not `width()`:
        a button that has not been laid out yet still reports Qt's 640px
        default, and pinning that stretches it across the window the moment it
        goes busy. `sizeHint()` is right either way -- a layout that had
        stretched the button wider goes on stretching it, since a minimum does
        not cap anything.
        """
        if busy:
            if not self._busy_text:
                self._busy_text = self.text()
                self.setMinimumWidth(self.sizeHint().width())
                self.setText(text or self._busy_text)
            self.setEnabled(False)
        else:
            if self._busy_text:
                self.setText(self._busy_text)
                self._busy_text = ""
                self.setMinimumWidth(0)
            self.setEnabled(True)

    def set_variant(self, variant: str) -> None:
        """Change variant at runtime -- e.g. Start becoming Stop."""
        self.setProperty("variant", variant)
        restyle(self)


class PrimaryButton(_Button):
    """The one accent-filled action in a view. There should rarely be two."""

    VARIANT = "primary"

    def _icon_token(self) -> str:
        return "text-on-accent"


class SecondaryButton(_Button):
    VARIANT = ""


class DangerButton(_Button):
    """Destructive actions.

    An outline that fills on hover rather than a red fill, so it never competes
    with the primary action for attention and never differs from it by colour
    alone.
    """

    VARIANT = "danger"

    def _icon_token(self) -> str:
        return "error"


class GhostButton(_Button):
    """Low-emphasis inline action, e.g. inside a card header."""

    VARIANT = "ghost"

    def _icon_token(self) -> str:
        return "text-secondary"


class IconButton(_Button):
    """Icon-only, square, and large enough to hit without aiming.

    An icon alone is never self-explanatory, so a tooltip is required rather
    than optional -- and it doubles as the accessible name, which is the only
    thing a screen reader has to go on.
    """

    VARIANT = "icon"

    def __init__(
        self,
        icon_name: str,
        tooltip: str,
        *,
        parent: QWidget | None = None,
        checkable: bool = False,
        size: int = 20,
    ) -> None:
        super().__init__("", parent=parent, tooltip=tooltip)
        self.setIcon(make_icon(icon_name, "text-secondary", size))
        self.setIconSize(QSize(size, size))
        self.setCheckable(checkable)
        self.setAccessibleName(tooltip)
        self._name = icon_name
        self._size = size

    def set_icon_name(self, icon_name: str, token: str = "text-secondary") -> None:
        """Swap the glyph -- play becoming stop, muted becoming unmuted."""
        if icon_name == self._name:
            return
        self._name = icon_name
        self.setIcon(make_icon(icon_name, token, self._size))

"""Buttons and toggles, as thin subclasses over the stylesheet's variants.

The styling lives in ``qtui.theme``; these exist so a call site reads
``PrimaryButton("Connect")`` rather than setting a dynamic property and
remembering to re-polish. They also carry the things a stylesheet cannot: a
pointing cursor, an accessible name, and the busy state.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QPushButton, QWidget

from common.design.tokens import Space
from qtui.theme import icon as make_icon
from qtui.theme import restyle

__all__ = [
    "DangerButton",
    "GhostButton",
    "IconButton",
    "PrimaryButton",
    "SecondaryButton",
]


class _Button(QPushButton):
    """Shared behaviour: variant property, cursor, and a busy state."""

    VARIANT = ""

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
        if text:
            self.setAccessibleName(text)

    def _icon_token(self) -> str:
        return "text-primary"

    # -- busy --------------------------------------------------------------

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

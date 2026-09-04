"""Chrome both desktop applications share.

`HeaderBar` lives here rather than in `client/gui/` because the video server
needs it too, and importing it from the client dragged the entire `client`
package into the video server's bundle -- a dependency that is wrong on its own
terms and that PyInstaller would have to be told about.

`qtui` is the shared toolkit; anything two applications use belongs in it.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from common.design.tokens import Radius, Space
from qtui.status import Status, StatusBadge
from qtui.widgets import paint_glass

__all__ = ["HeaderBar"]


class HeaderBar(QWidget):
    """Brand, connection status, and the window's own controls."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("surface", "header")
        # Tall enough that a 42px icon button clears the bottom border with
        # room to spare, rather than resting on it.
        self.setFixedHeight(64)

        row = QHBoxLayout(self)
        row.setContentsMargins(Space.LG, Space.SM, Space.MD, Space.SM)
        row.setSpacing(Space.MD)

        self._title = QLabel(title)
        self._title.setProperty("role", "title")
        row.addWidget(self._title)

        self.status = StatusBadge(Status.IDLE)
        row.addWidget(self.status)
        row.addStretch(1)

        self._actions = row

    def add_action(self, widget: QWidget) -> None:
        self._actions.addWidget(widget)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Drawn a radius taller than the widget so only the bottom corners
        # round: a strip flush against the window's top edge should not have
        # rounded corners floating in the middle of nothing.
        painter = QPainter(self)
        bounds = QRectF(self.rect()).adjusted(0, -Radius.PANEL, 0, 0)
        paint_glass(painter, bounds, surface="header", radius=Radius.PANEL)

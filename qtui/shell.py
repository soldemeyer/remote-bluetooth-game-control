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


#: The height band every window in this project opens into.
#:
#: Shared rather than copied because the two applications are meant to open
#: the same size, and the video server's own default was a fixed 880x700 that
#: had no relationship to the client's at all -- which stopped being merely
#: untidy when the identification panel made its content taller than the
#: window and there was no way to reach the bottom of it.
DEFAULT_MIN_HEIGHT = 820
DEFAULT_MAX_HEIGHT = 1500


def default_window_size(
    *,
    min_width: int,
    max_width: int,
    min_height: int = DEFAULT_MIN_HEIGHT,
    max_height: int = DEFAULT_MAX_HEIGHT,
):
    """A window sized to the screen it opens on.

    Most of the available area, which leaves the taskbar and a sliver of the
    desktop showing so the window still reads as a window rather than as a
    failed fullscreen, and is capped so it does not become unwieldy on a very
    large display.

    **The width band is the caller's and the height band is shared.** The two
    applications hold different things across -- the client has a fixed 644px
    drawer beside the picture, the video server a single column of cards -- so
    a shared width would give one of them a mostly empty window. Height is the
    axis that runs out, and it runs out at the same place on the same screen.
    """
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QGuiApplication

    screen = QGuiApplication.primaryScreen()
    if screen is None:
        # No screen at all: offscreen tests, and a headless CI machine. Pick
        # the floor rather than raising -- a window that cannot be sized is
        # still a window that has to open.
        return QSize(min_width, min_height)

    available = screen.availableGeometry()
    return QSize(
        max(min_width, min(int(available.width() * 0.95), max_width)),
        max(min_height, min(int(available.height() * 0.95), max_height)),
    )

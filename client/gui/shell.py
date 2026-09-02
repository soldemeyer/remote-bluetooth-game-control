"""The client's window furniture: header, video stage, drawer, control bar.

**Presentation only.** Nothing here knows about transports, decoders or
gamepads: the shell is handed widgets that already exist and arranges them.
That is what keeps it separable from `MainWindow`, which still owns every
piece of behaviour it owned before.

The layout it produces is video-first -- the picture is the window, and the
controls that set a session up live in a drawer beside it. The previous shape
was three stacked group boxes with the video in a *separate* top-level window,
so the thing the player is actually looking at was the one thing this
application did not show.

### What is deliberately not done here

- **No drop shadows, blurs or graphics effects over the video surface.** An
  effect re-renders its source on every repaint of the region beneath it, and
  that region repaints at the frame rate. The measured cost of getting the
  paint path wrong in this window is 1.81 ms p99 on the 500 Hz input loop.
- **The control bar auto-hides**, which is a performance decision as much as a
  visual one: a child widget over the surface is repainted whenever the surface
  repaints under it, so while the player is actually playing there is nothing
  there to repaint.
- **Statistics reuse the surface's own OSD** rather than adding a second
  overlay. The OSD is drawn inside the existing `paintEvent` from
  `osd_lines()`, costs nothing while it is off, and needs no timer of its own.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLayout,
    QScrollArea,
    QSizePolicy,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from common.design.tokens import Space
from qtui.status import Status, StatusBadge
from qtui.widgets import EmptyState, GlassPanel

__all__ = ["ControlBar", "Drawer", "HeaderBar", "VideoStage"]

#: How long the control bar stays up after the pointer stops moving. Long
#: enough to travel from one control to another without it vanishing mid-reach.
CONTROLS_IDLE_MS = 2600

#: Width of the controls drawer. Set by the controller table's nine columns,
#: which are what actually needs the room.
DRAWER_WIDTH = 620


class HeaderBar(QWidget):
    """Brand, connection status, and the window's own controls."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("surface", "header")
        self.setFixedHeight(56)

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


class ControlBar(GlassPanel):
    """The floating bar over the picture.

    A `GlassPanel` with no shadow: see the module docstring -- an effect here
    would re-render on every frame the surface paints underneath it.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(surface="card", parent=parent, shadow=False,
                         padding=Space.SM, spacing=Space.SM)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._row = QHBoxLayout()
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(Space.SM)
        super().add(self._row)

    def add(self, item) -> None:
        """Put a widget or a layout on the bar's single row."""
        if isinstance(item, QLayout):
            self._row.addLayout(item)
        else:
            self._row.addWidget(item)

    def add_spacing(self, amount: int) -> None:
        self._row.addSpacing(amount)


class VideoStage(QWidget):
    """The picture, what stands in for it, and the bar that floats over it.

    A `QStackedLayout` rather than show/hide juggling: exactly one of the two
    is visible by construction, so there is no state in which the placeholder
    and a live picture are both up (or, worse, neither).
    """

    #: The pointer moved over the stage. The window uses it to wake chrome
    #: that hides itself.
    activity = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("surface", "stage")
        self.setMouseTracking(True)

        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)

        self.placeholder = EmptyState(
            "No video yet",
            "Connect to a server that has a video source and the picture "
            "appears here.",
            icon_name="video-off",
        )
        self._stack.addWidget(self.placeholder)

        self._surface: QWidget | None = None

        self.controls = ControlBar(self)
        self.controls.hide()

        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(CONTROLS_IDLE_MS)
        self._idle.timeout.connect(self._hide_controls)

    # -- the surface -------------------------------------------------------

    def set_surface(self, surface: QWidget | None) -> None:
        """Show a video surface, or go back to the placeholder.

        A surface being removed is **not** deleted here: it is owned by
        whoever created it, and its paint path is driven by a signal from the
        decoder thread. Deleting it on the way past is how this window would
        earn a crash rather than a blank panel.
        """
        if self._surface is not None:
            self._surface.removeEventFilter(self)
            self._stack.removeWidget(self._surface)
            self._surface.setParent(None)
        self._surface = surface
        if surface is not None:
            surface.setMouseTracking(True)
            # The surface covers the stage, so it -- not the stage -- receives
            # the pointer. Without this the bar never appears while a picture
            # is up, which is the only time it is wanted.
            surface.installEventFilter(self)
            self._stack.addWidget(surface)
            self._stack.setCurrentWidget(surface)
        else:
            self._stack.setCurrentWidget(self.placeholder)
        self._position_controls()

    def has_surface(self) -> bool:
        return self._surface is not None

    def set_placeholder(self, title: str, body: str = "") -> None:
        self.placeholder.set_text(title, body)

    # -- the auto-hiding bar -----------------------------------------------

    def wake_controls(self) -> None:
        """Show the bar and restart its idle countdown."""
        if not self.controls.isVisible():
            self.controls.show()
            self.controls.raise_()
            self._position_controls()
        self._idle.start()
        self.activity.emit()

    def _hide_controls(self) -> None:
        # Never while the pointer is inside it: a bar that vanishes from under
        # the cursor takes the click with it.
        if self.controls.underMouse():
            self._idle.start()
            return
        self.controls.hide()

    def _position_controls(self) -> None:
        bar = self.controls
        bar.adjustSize()
        x = max(0, (self.width() - bar.width()) // 2)
        y = max(0, self.height() - bar.height() - Space.XL)
        bar.move(x, y)

    # -- Qt ----------------------------------------------------------------

    def eventFilter(self, watched, event):  # noqa: N802 - Qt naming
        if event.type() in (QEvent.Type.MouseMove, QEvent.Type.Enter):
            self.wake_controls()
        # Never consumed: the surface's own double-click-to-fullscreen still
        # needs these, and so does anything else watching the same widget.
        return False

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.wake_controls()
        super().mouseMoveEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._position_controls()


class Drawer(QWidget):
    """The collapsible column of controls beside the picture.

    Collapsing hides the panel but leaves everything inside it constructed and
    connected: the window's `_tick` reads those widgets whether or not anyone
    is looking at them, and a drawer that destroyed its contents would turn a
    view preference into a functional change.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("surface", "drawer")
        self.setFixedWidth(DRAWER_WIDTH)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        body = QWidget()
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(Space.MD, Space.MD, Space.MD, Space.MD)
        self._body.setSpacing(Space.MD)
        self._scroll.setWidget(body)
        outer.addWidget(self._scroll)

        self._open = True

    def add(self, widget: QWidget, stretch: int = 0) -> None:
        self._body.addWidget(widget, stretch)

    def add_stretch(self) -> None:
        self._body.addStretch(1)

    def is_open(self) -> bool:
        return self._open

    def set_open(self, opened: bool) -> None:
        self._open = bool(opened)
        self.setVisible(self._open)

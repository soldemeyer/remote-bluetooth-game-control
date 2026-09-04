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

from PySide6.QtCore import QEvent, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QPainter
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

from common.design.tokens import Radius, Space
from qtui.shell import HeaderBar
from qtui.widgets import EmptyState, GlassPanel, paint_glass

__all__ = ["ControlBar", "Drawer", "HeaderBar", "VideoStage"]

#: How long the control bar stays up after the pointer stops moving. Long
#: enough to travel from one control to another without it vanishing mid-reach.
CONTROLS_IDLE_MS = 2600

#: Gap between the drawer's glass and everything around it -- the header above
#: it and the window edges. Without it the drawer's rounded corners meet the
#: header's bottom edge with nothing between, which reads as the two overlapping
#: rather than as two surfaces.
DRAWER_INSET = Space.MD

#: How far the scroll area sits *inside* the glass. The scrollbar is drawn at
#: the scroll area's edge, so with no inset it rides the card's border and
#: crosses the corner arc -- visible whenever the list is scrolled to either
#: end. At this inset the corner intrudes 2.1px where the scrollbar begins,
#: leaving about 6px of clearance (radius 16, scrollbar 10 wide).
SCROLL_INSET = Space.SM

#: Width of the controls drawer, glass plus both insets. Set by the controller
#: table's nine columns, which are what actually needs the room.
DRAWER_WIDTH = 620 + DRAWER_INSET * 2


class ControlBar(GlassPanel):
    """The floating bar over the picture.

    A `GlassPanel` with no shadow: see the module docstring -- an effect here
    would re-render on every frame the surface paints underneath it.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        # A heavier fill than the panels in the drawer. Those sit on a
        # coloured backdrop; this one sits on video, which is usually close to
        # black -- and the same translucency over black is a dark grey smear
        # rather than a floating control.
        super().__init__(surface="overlay", parent=parent, shadow=False,
                         padding=Space.SM, spacing=Space.SM,
                         radius=Radius.PILL)
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
        # The glass inset, plus a little more so the scrollbar is *inside* the
        # card rather than on its border.
        edge = DRAWER_INSET + SCROLL_INSET
        outer.setContentsMargins(edge, edge, edge, edge)
        outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        body = QWidget()
        self._body = QVBoxLayout(body)
        # The rest of the padding. Split between here and the scroll inset
        # above rather than all of it here, so the total from the glass edge to
        # the content is unchanged and only the scrollbar moved.
        self._body.setContentsMargins(Space.SM, Space.SM, Space.SM, Space.SM)
        self._body.setSpacing(Space.MD)
        self._scroll.setWidget(body)
        outer.addWidget(self._scroll)

        self._open = True

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Inset on every side and rounded on every corner: a card beside the
        # picture rather than a slab welded to the window edge.
        painter = QPainter(self)
        bounds = QRectF(self.rect()).adjusted(
            DRAWER_INSET, DRAWER_INSET, -DRAWER_INSET, -DRAWER_INSET
        )
        paint_glass(painter, bounds, surface="drawer", radius=Radius.PANEL)

    def add(self, widget: QWidget, stretch: int = 0) -> None:
        self._body.addWidget(widget, stretch)

    def add_stretch(self) -> None:
        self._body.addStretch(1)

    def is_open(self) -> bool:
        return self._open

    def set_open(self, opened: bool, *, animate: bool = True) -> None:
        """Show or hide the drawer. **Not animated, and that is measured.**

        Animating the width means a full relayout per frame, and this drawer
        holds a `ResizeToContents` header over four rows of cell widgets and a
        pyqtgraph plot. Measured on the reference machine, one width step cost
        a median of 8 ms and a worst case of **1561 ms** -- so the slide did not
        merely stutter, it starved its own animation and left the drawer
        stopped partway, neither open nor closed, with the toggle appearing
        dead from then on. That is what this looked like as a bug.

        The motion budget is spent where it is cheap instead: hover and press
        on controls, and the toast fade.
        """
        self._open = bool(opened)
        self.setVisible(self._open)
        self.setFixedWidth(DRAWER_WIDTH if self._open else 0)

    def add(self, widget: QWidget, stretch: int = 0) -> None:
        self._body.addWidget(widget, stretch)

    def add_stretch(self) -> None:
        self._body.addStretch(1)

    def is_open(self) -> bool:
        return self._open

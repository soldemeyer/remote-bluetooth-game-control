"""Live controller preview: draws a chosen system's pad and lights what is held.

Fed the same :class:`~common.state.ControllerState` the client is about to send,
so what lights up is literally what the console will receive. That is the point
of the widget: it is the only place a player can confirm a binding is right
without walking to the console.

Rendering is QPainter over the geometry in :mod:`client.gui.controller_layouts`
rather than a static SVG per system. A static SVG would need per-element
manipulation to highlight one button; here lighting is just a colour choice at
paint time, and the geometry stays declarative and reviewable.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from client.gui.controller_layouts import (
    DEFAULT_LAYOUT,
    VIEW_H,
    VIEW_W,
    Layout,
    get_layout,
)
from common.state import Button, ControllerState

#: Sticks deflect this far (in design units) at full travel.
_STICK_TRAVEL = 11.0

#: Axis reading past which a stick counts as "pushed" for the glow.
_STICK_ACTIVE = 8000


class ControllerPreview(QWidget):
    """Draws one controller layout and highlights held controls."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout: Layout = get_layout(DEFAULT_LAYOUT)
        self._state = ControllerState()
        #: Logical button bit the mapping screen wants the player to press.
        self._highlight = 0

        self.setMinimumSize(360, 234)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # -- inputs ------------------------------------------------------------

    def set_layout_key(self, key: str) -> None:
        self._layout = get_layout(key)
        self.update()

    @property
    def layout_key(self) -> str:
        return self._layout.key

    @property
    def note(self) -> str:
        return self._layout.note

    def set_state(self, state: ControllerState) -> None:
        """Show a new controller state. Copied, not referenced.

        The caller reuses one ControllerState across polls (the datapath must
        not allocate), so holding a reference would make the preview show
        whatever the next poll wrote.
        """
        self._state.buttons = state.buttons
        self._state.left_x = state.left_x
        self._state.left_y = state.left_y
        self._state.right_x = state.right_x
        self._state.right_y = state.right_y
        self._state.left_trigger = state.left_trigger
        self._state.right_trigger = state.right_trigger
        self.update()

    def set_highlight(self, button: int) -> None:
        """Pulse one control, to show which binding is being captured."""
        self._highlight = button
        self.update()

    # -- painting ----------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Uniform scale, centred: the layouts are authored for one aspect ratio
        # and stretching them would misplace every control relative to the art.
        scale = min(self.width() / VIEW_W, self.height() / VIEW_H)
        painter.translate(
            (self.width() - VIEW_W * scale) / 2,
            (self.height() - VIEW_H * scale) / 2,
        )
        painter.scale(scale, scale)

        layout = self._layout
        self._draw_body(painter, layout)

        for control in layout.controls:
            self._draw_control(painter, layout, control)

        painter.end()

    def _draw_body(self, painter: QPainter, layout: Layout) -> None:
        path = _parse_path(layout.body)
        painter.setBrush(QBrush(QColor(layout.shell)))
        painter.setPen(QPen(QColor(layout.shell_edge), 3))
        painter.drawPath(path)

    def _draw_control(self, painter: QPainter, layout: Layout, control) -> None:
        held = bool(control.button and (self._state.buttons & control.button))
        wanted = bool(control.button and control.button == self._highlight)

        if held:
            fill = QColor(layout.lit)
        elif control.color:
            fill = QColor(control.color)
        else:
            fill = QColor(layout.idle)

        # A held control gets a bright ring as well as a fill change. Fill alone
        # is not enough: a system's accent can land on top of a face button's
        # own colour (Xbox green A on a green highlight) and the press becomes
        # invisible, which defeats the entire point of the preview.
        if held:
            pen = QPen(QColor(layout.lit).lighter(140), 4)
        elif wanted:
            # A dashed ring says "press this one" without implying it is held.
            pen = QPen(QColor(layout.lit), 3, Qt.PenStyle.DashLine)
        else:
            pen = QPen(QColor(layout.shell_edge), 2)

        painter.setPen(pen)
        painter.setBrush(QBrush(fill))

        if control.shape == "stick":
            self._draw_stick(painter, layout, control, held)
        elif control.shape == "circle":
            painter.drawEllipse(QPointF(control.x, control.y), control.r, control.r)
        elif control.shape == "rect":
            painter.drawRect(
                QRectF(control.x - control.w / 2, control.y - control.h / 2,
                       control.w, control.h)
            )
        elif control.shape == "capsule":
            rect = QRectF(control.x - control.w / 2, control.y - control.h / 2,
                          control.w, control.h)
            painter.drawRoundedRect(rect, control.h / 2, control.h / 2)

        if control.label:
            self._draw_label(painter, layout, control, held)

    def _draw_stick(self, painter: QPainter, layout: Layout, control, held: bool) -> None:
        # Well first, then the cap offset by the current deflection, so the
        # player can see stick travel as well as the click.
        painter.setBrush(QBrush(QColor(layout.shell_edge).darker(115)))
        painter.drawEllipse(QPointF(control.x, control.y), control.r, control.r)

        if control.button == Button.LEFT_STICK:
            dx, dy = self._state.left_x, self._state.left_y
        else:
            dx, dy = self._state.right_x, self._state.right_y

        offset_x = control.x + (dx / 32767.0) * _STICK_TRAVEL
        offset_y = control.y + (dy / 32767.0) * _STICK_TRAVEL

        active = held or abs(dx) > _STICK_ACTIVE or abs(dy) > _STICK_ACTIVE
        painter.setBrush(QBrush(QColor(layout.lit if active else layout.idle)))
        painter.setPen(QPen(QColor(layout.shell_edge), 2))
        painter.drawEllipse(QPointF(offset_x, offset_y), control.r * 0.62, control.r * 0.62)

    def _draw_label(self, painter: QPainter, layout: Layout, control, held: bool) -> None:
        font = QFont(painter.font())
        font.setPixelSize(11 if len(control.label) <= 2 else 9)
        font.setBold(True)
        painter.setFont(font)

        painter.setPen(QPen(QColor(control.text or _contrast(
            QColor(layout.lit) if held else QColor(control.color or layout.idle)
        ))))

        box = max(control.w, control.r * 2, 24.0)
        painter.drawText(
            QRectF(control.x - box, control.y - 8, box * 2, 16),
            Qt.AlignmentFlag.AlignCenter,
            control.label,
        )


def _contrast(background: QColor) -> QColor:
    """Black or white, whichever stays readable on ``background``.

    Layouts range from near-white (PS5) to near-black (Genesis), so a fixed
    label colour is illegible on one end or the other.
    """
    luminance = (
        0.299 * background.red() + 0.587 * background.green() + 0.114 * background.blue()
    )
    return QColor("#10151c") if luminance > 140 else QColor("#f4f7fb")


def _parse_path(data: str) -> QPainterPath:
    """Parse the SVG path subset the layouts use: M, L, C and Z.

    Qt has no public SVG-path parser, and pulling QtSvg in just to draw eight
    outlines would mean shipping the module and still not being able to light
    individual controls. The layouts deliberately stay inside this subset.
    """
    path = QPainterPath()
    tokens = data.replace(",", " ").split()
    index = 0
    command = ""
    current = QPointF(0.0, 0.0)

    def number() -> float:
        nonlocal index
        value = float(tokens[index])
        index += 1
        return value

    while index < len(tokens):
        token = tokens[index]
        if token.isalpha():
            command = token
            index += 1
            if command in ("Z", "z"):
                path.closeSubpath()
                continue

        if command in ("M", "m"):
            x, y = number(), number()
            current = QPointF(x, y)
            path.moveTo(current)
            # A repeated coordinate pair after M is an implicit L, per the spec.
            command = "L" if command == "M" else "l"
        elif command in ("L", "l"):
            x, y = number(), number()
            current = QPointF(x, y)
            path.lineTo(current)
        elif command in ("C", "c"):
            x1, y1 = number(), number()
            x2, y2 = number(), number()
            x, y = number(), number()
            path.cubicTo(QPointF(x1, y1), QPointF(x2, y2), QPointF(x, y))
            current = QPointF(x, y)
        else:
            # Unknown command: skip the token rather than raising. A layout with
            # one bad segment should still draw the rest.
            index += 1

    return path

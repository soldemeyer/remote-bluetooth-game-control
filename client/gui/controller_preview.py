"""Live controller preview: draws a system's artwork and lights what is held.

Fed the same :class:`~common.state.ControllerState` the client is about to send,
so what lights up is literally what the console will receive. That is the point
of the widget: it is the only place a player can confirm a binding is right
without walking to the console.

**How it works.** The artwork is an SVG
(``client/gui/assets/controllers/*.svg``) in which every control is a group with
an id like ``c_a``. The base image is rendered once per size into a cached
pixmap, and pressed controls are highlighted by asking the renderer where that
element is (``boundsOnElement``) and drawing a glow over it. Art and hit boxes
therefore cannot drift apart, and improving the art never touches this file.

This replaces an earlier version that drew everything with QPainter from
coordinates in Python. That worked, but every visual fix was a coordinate edit
and the results looked hand-cut.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QSizePolicy, QWidget

from client.gui.assets import assets_dir
from client.gui.controller_layouts import (
    DEFAULT_LAYOUT,
    KIND_STICK,
    Layout,
    get_layout,
)
from common.state import Button, ControllerState

log = logging.getLogger(__name__)

#: How far a stick cap travels from centre at full deflection, as a fraction of
#: the element's radius.
_STICK_TRAVEL = 0.30

#: Axis reading past which a stick counts as "pushed" for the glow.
_STICK_ACTIVE = 8000


class ControllerPreview(QWidget):
    """Draws one controller's artwork and highlights held controls."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout: Layout = get_layout(DEFAULT_LAYOUT)
        self._renderer: QSvgRenderer | None = None
        self._state = ControllerState()
        self._highlight = 0
        #: Draw the highlighted control as pressed, not merely ringed.
        self._highlight_lit = False
        #: 0..1 sweep around the highlighted control, for hold-to-confirm.
        self._highlight_progress = 0.0
        #: Unit (dx, dy) when a stick *direction* is wanted rather than a click.
        self._highlight_direction: tuple[int, int] | None = None

        #: Base artwork, re-rendered only when the size or layout changes.
        #: Rasterising the SVG on every state update would burn real CPU at the
        #: dialog's 60 Hz refresh for no visual gain.
        self._cache: QPixmap | None = None
        self._cache_key: tuple = ()

        self.setMinimumSize(380, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._load()

    # -- inputs ------------------------------------------------------------

    def set_layout_key(self, key: str) -> None:
        if key == self._layout.key and self._renderer is not None:
            return
        self._layout = get_layout(key)
        self._load()
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

    def set_highlight(
        self,
        button: int,
        *,
        lit: bool = False,
        progress: float = 0.0,
        direction: tuple[int, int] | None = None,
    ) -> None:
        """Mark the control currently being captured.

        ``lit`` draws it as though pressed. The walk-through uses that to show
        *the control being set* rather than the one the player's press happens
        to be bound to today -- during a re-bind those are different controls,
        and lighting the old one is actively misleading.

        ``progress`` (0..1) sweeps a ring around it, for captures that require
        the input to be held.

        ``direction`` marks *which way* a stick is being asked for, as a unit
        (dx, dy). Without it, "click the left stick" and "push the left stick
        up" drew identically -- the same ring on the same element -- and the
        player had only the text to tell them apart.
        """
        self._highlight = button
        self._highlight_lit = lit
        self._highlight_progress = max(0.0, min(1.0, progress))
        self._highlight_direction = direction
        self.update()

    # -- artwork -----------------------------------------------------------

    def _load(self) -> None:
        path = assets_dir() / "controllers" / self._layout.svg
        renderer = QSvgRenderer(str(path))

        if not renderer.isValid():
            # A missing or malformed asset must not take the dialog down; the
            # bindings list beside it is still perfectly usable.
            log.warning("Controller artwork missing or invalid: %s", path)
            self._renderer = None
        else:
            self._renderer = renderer

        self._cache = None
        self._cache_key = ()

    def _target_rect(self) -> QRectF:
        """Where the artwork sits, preserving its aspect ratio."""
        if self._renderer is None:
            return QRectF(self.rect())

        box = self._renderer.viewBoxF()
        if box.width() <= 0 or box.height() <= 0:
            return QRectF(self.rect())

        scale = min(self.width() / box.width(), self.height() / box.height())
        w, h = box.width() * scale, box.height() * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _base(self) -> QPixmap | None:
        if self._renderer is None:
            return None

        ratio = self.devicePixelRatioF()
        key = (self.width(), self.height(), self._layout.key, ratio)
        if self._cache is not None and self._cache_key == key:
            return self._cache

        pixmap = QPixmap(int(self.width() * ratio), int(self.height() * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._renderer.render(painter, self._target_rect())
        painter.end()

        self._cache = pixmap
        self._cache_key = key
        return pixmap

    def _element_rect(self, element: str) -> QRectF | None:
        """Where an SVG element lands on screen, in widget coordinates."""
        if self._renderer is None or not self._renderer.elementExists(element):
            return None

        box = self._renderer.viewBoxF()
        if box.width() <= 0 or box.height() <= 0:
            return None

        bounds = self._renderer.boundsOnElement(element)
        target = self._target_rect()
        sx = target.width() / box.width()
        sy = target.height() / box.height()

        return QRectF(
            target.x() + (bounds.x() - box.x()) * sx,
            target.y() + (bounds.y() - box.y()) * sy,
            bounds.width() * sx,
            bounds.height() * sy,
        )

    # -- painting ----------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        base = self._base()
        if base is None:
            painter.setPen(QPen(self.palette().mid().color()))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter,
                f"Artwork for “{self._layout.name}” is missing.",
            )
            painter.end()
            return

        painter.drawPixmap(0, 0, base)

        lit = QColor(self._layout.lit)
        for control in self._layout.controls:
            rect = self._element_rect(control.element)
            if rect is None:
                continue

            if control.kind == KIND_STICK:
                self._draw_stick(painter, control, rect, lit)
                continue

            target = bool(control.button and control.button == self._highlight)
            held = bool(control.button and (self._state.buttons & control.button))

            if held or (target and self._highlight_lit):
                self._glow(painter, rect, lit)
            if target:
                self._ring(painter, rect, lit, self._highlight_progress)

        painter.end()

    def _glow(self, painter: QPainter, rect: QRectF, colour: QColor) -> None:
        """Light a pressed control.

        A translucent fill plus a bright ring: the fill alone is invisible when a
        system's accent lands on top of a button's own colour (a green highlight
        on Xbox's green A), and the ring alone reads as a selection cue rather
        than a press.
        """
        wash = QColor(colour)
        wash.setAlpha(120)
        painter.setBrush(QBrush(wash))
        painter.setPen(QPen(colour.lighter(150), max(2.0, rect.width() * 0.09)))

        inset = rect.adjusted(1, 1, -1, -1)
        if abs(rect.width() - rect.height()) < rect.width() * 0.25:
            painter.drawEllipse(inset)
        else:
            radius = min(inset.width(), inset.height()) / 2
            painter.drawRoundedRect(inset, radius, radius)

    def _ring(
        self, painter: QPainter, rect: QRectF, colour: QColor, progress: float = 0.0
    ) -> None:
        """Outline the control being captured.

        Dashed while simply waiting -- "press this one", without implying it is
        held. Once a hold is under way it becomes a **circular loader**: a faint
        full ring with a bright arc sweeping clockwise from twelve o'clock. The
        wait is deliberate, and a control that just sits there unresponsive for
        most of a second reads as broken unless something visibly counts it out.
        """
        painter.setBrush(Qt.BrushStyle.NoBrush)
        width = max(2.0, rect.width() * 0.07)
        inset = rect.adjusted(-3, -3, 3, 3)
        round_ish = abs(rect.width() - rect.height()) < rect.width() * 0.25

        if progress <= 0.0:
            painter.setPen(QPen(colour, width, Qt.PenStyle.DashLine))
            if round_ish:
                painter.drawEllipse(inset)
            else:
                radius = min(inset.width(), inset.height()) / 2
                painter.drawRoundedRect(inset, radius, radius)
            return

        # The loader is always circular, even on an oblong control: a partly
        # swept rounded rectangle does not read as progress.
        span = max(inset.width(), inset.height()) + width * 2
        circle = QRectF(0, 0, span, span)
        circle.moveCenter(inset.center())

        track = QColor(colour)
        track.setAlpha(70)
        painter.setPen(QPen(track, width))
        painter.drawEllipse(circle)

        painter.setPen(QPen(colour.lighter(140), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        # Qt angles are in sixteenths of a degree, anticlockwise from 3 o'clock.
        painter.drawArc(circle, 90 * 16, -int(360 * 16 * min(1.0, progress)))

    def _draw_stick(
        self, painter: QPainter, control, rect: QRectF, lit: QColor
    ) -> None:
        """Show stick travel as well as the click.

        The artwork draws the well and a resting cap; this paints a cap at the
        current deflection on top, so the player can see the axes working and
        not just the button.
        """
        if control.button == Button.LEFT_STICK:
            dx, dy = self._state.left_x, self._state.left_y
        else:
            dx, dy = self._state.right_x, self._state.right_y

        target = control.button == self._highlight
        wanted = self._highlight_direction if target else None

        if wanted is not None:
            # Asking for a direction, not a click: throw the cap that way and
            # point at it, so it cannot be mistaken for "press the stick in".
            self._draw_stick_direction(painter, rect, wanted, lit)
            return

        held = bool(self._state.buttons & control.button) or (
            target and self._highlight_lit
        )
        active = held or abs(dx) > _STICK_ACTIVE or abs(dy) > _STICK_ACTIVE

        radius = min(rect.width(), rect.height()) / 2
        centre = rect.center()
        offset = QPointF(
            centre.x() + (dx / 32767.0) * radius * _STICK_TRAVEL,
            centre.y() + (dy / 32767.0) * radius * _STICK_TRAVEL,
        )
        cap = radius * 0.74

        if active:
            wash = QColor(lit)
            wash.setAlpha(150)
            painter.setBrush(QBrush(wash))
            painter.setPen(QPen(lit.lighter(150), max(2.0, radius * 0.16)))
        else:
            # At rest, only move the cap if the stick is actually off centre;
            # otherwise leave the artwork alone.
            if abs(dx) < 1500 and abs(dy) < 1500:
                if target:
                    self._ring(painter, rect, lit, self._highlight_progress)
                return
            painter.setBrush(QBrush(QColor(0, 0, 0, 90)))
            painter.setPen(QPen(QColor(0, 0, 0, 140), 2))

        painter.drawEllipse(offset, cap, cap)

        if target:
            self._ring(painter, rect, lit, self._highlight_progress)

    def _draw_stick_direction(
        self, painter: QPainter, rect: QRectF, wanted: tuple[int, int], lit: QColor
    ) -> None:
        """Show a stick pushed one way, with an arrow saying which.

        Deliberately unlike the click treatment: the cap sits off-centre at
        full travel and an arrow points out of the well. A centred ring, which
        is what a click gets, said nothing about direction.
        """
        dx, dy = wanted
        radius = min(rect.width(), rect.height()) / 2
        centre = rect.center()
        offset = QPointF(
            centre.x() + dx * radius * _STICK_TRAVEL * 2.0,
            centre.y() + dy * radius * _STICK_TRAVEL * 2.0,
        )

        # The well the cap has left, so the travel is legible.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(0, 0, 0, 90), max(1.5, radius * 0.10)))
        painter.drawEllipse(centre, radius * 0.5, radius * 0.5)

        wash = QColor(lit)
        wash.setAlpha(170)
        painter.setBrush(QBrush(wash))
        painter.setPen(QPen(lit.lighter(150), max(2.0, radius * 0.16)))
        painter.drawEllipse(offset, radius * 0.62, radius * 0.62)

        self._draw_arrow(painter, centre, wanted, radius, lit)

        if self._highlight_progress > 0:
            self._ring(painter, rect, lit, self._highlight_progress)

    def _draw_arrow(
        self,
        painter: QPainter,
        centre: QPointF,
        wanted: tuple[int, int],
        radius: float,
        colour: QColor,
    ) -> None:
        """A chevron just outside the stick, pointing the way to push."""
        dx, dy = wanted
        tip = QPointF(
            centre.x() + dx * radius * 1.75, centre.y() + dy * radius * 1.75
        )
        # Perpendicular, for the chevron's two tails.
        px, py = -dy, dx
        back = radius * 0.55
        span = radius * 0.45

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(
            QPen(
                colour.lighter(160),
                max(2.5, radius * 0.20),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        path = QPolygonF(
            [
                QPointF(tip.x() - dx * back + px * span, tip.y() - dy * back + py * span),
                tip,
                QPointF(tip.x() - dx * back - px * span, tip.y() - dy * back - py * span),
            ]
        )
        painter.drawPolyline(path)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._cache = None
        super().resizeEvent(event)

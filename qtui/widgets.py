"""Surfaces and content widgets: panels, sections, metrics, empty states.

All of these are ordinary `QFrame`/`QWidget` subclasses that set a `surface`
property the stylesheet selects on. Nothing here paints its own background --
that keeps the look in one place, and it means a token change reaches every
surface without touching a widget.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from common.design.tokens import Space, Type
from qtui.theme import pixmap, qcolor, restyle

__all__ = ["EmptyState", "GlassPanel", "MetricCard", "SectionHeader", "SettingsSection"]


def _shadow(widget: QWidget, blur: int = 24, dy: int = 4) -> None:
    """A soft drop shadow under a floating surface.

    `QGraphicsDropShadowEffect` is cheap here in a way a *blur* effect is not:
    it rasterises the widget once and caches it, and these panels do not
    repaint per frame. It is still not applied to anything that sits over live
    video -- see `qtui.overlay`.
    """
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setXOffset(0)
    effect.setYOffset(dy)
    effect.setColor(qcolor("shadow"))
    widget.setGraphicsEffect(effect)


class GlassPanel(QFrame):
    """A translucent surface with a hairline border.

    ``surface`` picks the recipe: ``glass`` for floating panels, ``card`` for
    content cards, ``solid`` where translucency would cost legibility (dense
    forms, long lists), ``sunken`` for wells.
    """

    def __init__(
        self,
        *,
        surface: str = "glass",
        parent: QWidget | None = None,
        shadow: bool = False,
        padding: int = Space.LG,
        spacing: int = Space.MD,
    ) -> None:
        super().__init__(parent)
        self.setProperty("surface", surface)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(padding, padding, padding, padding)
        self._layout.setSpacing(spacing)
        if shadow:
            _shadow(self)

    def body(self) -> QVBoxLayout:
        """The panel's own layout, for callers adding content."""
        return self._layout

    def add(self, item) -> None:
        """Add a widget or a layout without the caller choosing the method."""
        if isinstance(item, QLayout):
            self._layout.addLayout(item)
        else:
            self._layout.addWidget(item)

    def set_surface(self, surface: str) -> None:
        self.setProperty("surface", surface)
        restyle(self)


class SectionHeader(QWidget):
    """A title, an optional subtitle, and optional trailing controls."""

    def __init__(
        self,
        title: str,
        subtitle: str = "",
        *,
        icon_name: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(Space.SM)

        if icon_name:
            glyph = QLabel()
            glyph.setPixmap(pixmap(icon_name, "text-secondary", 18))
            glyph.setFixedSize(18, 18)
            row.addWidget(glyph)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(2)

        self._title = QLabel(title)
        self._title.setProperty("role", "heading")
        text.addWidget(self._title)

        self._subtitle = QLabel(subtitle)
        self._subtitle.setProperty("role", "muted")
        self._subtitle.setWordWrap(True)
        self._subtitle.setVisible(bool(subtitle))
        text.addWidget(self._subtitle)

        row.addLayout(text, 1)
        self._trailing = row

    def add_action(self, widget: QWidget) -> None:
        """Put a control at the right-hand end of the header."""
        self._trailing.addWidget(widget)

    def set_subtitle(self, text: str) -> None:
        if self._subtitle.text() == text:
            return
        self._subtitle.setText(text)
        self._subtitle.setVisible(bool(text))


class SettingsSection(GlassPanel):
    """A titled group of settings. The themed replacement for `QGroupBox`.

    `QGroupBox` is styled too, so existing layouts look right without being
    rewritten -- but a group box cannot carry a subtitle or a header action,
    and both turned out to be needed in every panel that explains itself.
    """

    def __init__(
        self,
        title: str,
        subtitle: str = "",
        *,
        icon_name: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(surface="glass", parent=parent)
        self.header = SectionHeader(title, subtitle, icon_name=icon_name)
        self._layout.addWidget(self.header)


class MetricCard(QFrame):
    """One number, its label, and its unit.

    Values are monospaced with tabular figures. Latency and bitrate update
    several times a second, and proportional digits change width as the value
    changes, so the number jitters sideways while it is being read.
    """

    def __init__(
        self,
        label: str,
        value: str = "—",
        unit: str = "",
        *,
        parent: QWidget | None = None,
        tooltip: str = "",
    ) -> None:
        super().__init__(parent)
        self.setProperty("surface", "card")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(Space.MD, Space.MD, Space.MD, Space.MD)
        layout.setSpacing(Space.XS)

        self._label = QLabel(label.upper())
        self._label.setProperty("role", "meta")

        value_row = QHBoxLayout()
        value_row.setContentsMargins(0, 0, 0, 0)
        value_row.setSpacing(Space.XS)

        self._value = QLabel(value)
        self._value.setProperty("role", "metric")
        font = self._value.font()
        font.setFamilies(list(Type.FAMILIES_MONO))
        # Tabular figures. Without this the digits are proportional and the
        # number shifts sideways every time it changes.
        font.setStyleName("")
        self._value.setFont(font)

        self._unit = QLabel(unit)
        self._unit.setProperty("role", "meta")
        self._unit.setAlignment(
            Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft
        )
        self._unit.setVisible(bool(unit))

        value_row.addWidget(self._value)
        value_row.addWidget(self._unit)
        value_row.addStretch(1)

        layout.addWidget(self._label)
        layout.addLayout(value_row)

        if tooltip:
            self.setToolTip(tooltip)
        self._token = ""

    def set_value(self, value: str, token: str = "") -> None:
        """Update the reading, optionally colouring it by health.

        Guarded, because these are driven from a polling timer and writing the
        same text back still costs a relayout.
        """
        if self._value.text() != value:
            self._value.setText(value)
        if token != self._token:
            self._token = token
            colour = qcolor(token or "text-primary").name()
            self._value.setStyleSheet(f"color: {colour};")

    def set_unit(self, unit: str) -> None:
        if self._unit.text() == unit:
            return
        self._unit.setText(unit)
        self._unit.setVisible(bool(unit))


class EmptyState(QWidget):
    """What to show where there is nothing yet, and what to do about it.

    The applications previously said "No preview" and "Not connected" in grey
    text. That states the fact and leaves the operator to work out the remedy;
    an empty state should carry the next action.
    """

    def __init__(
        self,
        title: str,
        body: str = "",
        *,
        icon_name: str = "info",
        action: QWidget | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Space.XXL, Space.XXL, Space.XXL, Space.XXL)
        layout.setSpacing(Space.MD)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        glyph = QLabel()
        glyph.setPixmap(pixmap(icon_name, "text-muted", 32))
        glyph.setFixedSize(32, 32)
        layout.addWidget(glyph, 0, Qt.AlignmentFlag.AlignHCenter)

        self._title = QLabel(title)
        self._title.setProperty("role", "heading")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._title)

        self._body = QLabel(body)
        self._body.setProperty("role", "muted")
        self._body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._body.setWordWrap(True)
        # Both bounds. A word-wrapped QLabel's *minimum* size hint is one word
        # wide, and a centring layout gives a child its minimum -- so without a
        # floor the sentence wrapped into a column a few words across in the
        # middle of an otherwise empty panel.
        self._body.setMinimumWidth(280)
        self._body.setMaximumWidth(420)
        self._body.setVisible(bool(body))
        layout.addWidget(self._body, 0, Qt.AlignmentFlag.AlignHCenter)

        if action is not None:
            layout.addSpacing(Space.SM)
            layout.addWidget(action, 0, Qt.AlignmentFlag.AlignHCenter)

    def set_text(self, title: str, body: str = "") -> None:
        if self._title.text() != title:
            self._title.setText(title)
        if self._body.text() != body:
            self._body.setText(body)
            self._body.setVisible(bool(body))

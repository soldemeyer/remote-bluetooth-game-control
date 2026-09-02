"""The Qt rendering of the design system: one stylesheet, one icon factory.

Everything visual comes from ``common.design.tokens``. No colour, size or
duration is written here as a literal -- that is the whole point of the token
module, and a hard-coded `#888` is how the previous styling ended up scattered
across sixteen call sites with no way to change it.

**There is no blur here, deliberately.** Qt has no backdrop filter, and
``QGraphicsBlurEffect`` re-renders its source on every repaint -- in the client
that repaint competes with the video decoder for the GIL, which was measured to
cost the 500 Hz input loop real time (see ``LATENCY_OPTIMIZATION.md``). Glass
is a translucent fill over a static window gradient instead: visually
equivalent at panel sizes, and free.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

from common.design import icons as icon_source
from common.design.tokens import Color, Motion, Radius, Space, Type, palette

__all__ = ["apply_theme", "icon", "pixmap", "qcolor", "stylesheet"]


def qcolor(token: str, alpha: float | None = None, over: str | None = None) -> QColor:
    """A token as a `QColor`, for custom-painted widgets.

    `over` flattens a translucent result onto a backdrop token and returns it
    fully opaque. Qt composites a widget's own stylesheet background against
    whatever it is drawn on, but a colour handed to `QPainter` or written into
    a `background:` on a nested widget has no such guarantee -- so a tint is
    resolved here rather than left to chance.
    """
    colour = palette[token]
    if alpha is not None:
        colour = colour.alpha(alpha)
    if over is not None:
        colour = colour.over(palette[over])
    return QColor(colour.r, colour.g, colour.b, round(colour.a * 255))


def _c(token: str) -> str:
    """A token in Qt stylesheet syntax. Alpha is a percentage here, not a float."""
    return palette[token].qss


def _flat(token: str, on: str = "background-base") -> str:
    """A translucent token flattened onto a backdrop.

    For the few places where Qt's own compositing is unreliable -- notably an
    item view's popup, which is a separate top-level window and therefore has
    nothing behind it to blend with.
    """
    return palette[token].over(palette[on]).qss


# --------------------------------------------------------------------------
# Icons
# --------------------------------------------------------------------------


@functools.lru_cache(maxsize=512)
def pixmap(name: str, color: str = "text-primary", size: int = 20, ratio: int = 2) -> QPixmap:
    """One icon, rendered. Cached -- rasterising per repaint would be wasteful.

    ``ratio`` is the device pixel ratio to rasterise for; it is a plain int in
    the key so the cache stays hashable and small. Rendering at 2x and letting
    Qt scale down keeps icons crisp on high-DPI displays without a second code
    path for them.
    """
    document = icon_source.render_svg(name, color=palette[color].css)
    renderer = QSvgRenderer(QByteArray(document.encode("utf-8")))

    target = QPixmap(size * ratio, size * ratio)
    target.setDevicePixelRatio(ratio)
    target.fill(Qt.GlobalColor.transparent)

    painter = QPainter(target)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    # **The rect is required.** `render(painter)` with no rect fills the
    # painter's *device* viewport, which on a pixmap carrying a device pixel
    # ratio is `size * ratio` -- while the painter itself works in logical
    # coordinates. The icon is then drawn at twice its size and clipped to its
    # own top-left quarter, which reads as a broken glyph rather than as a
    # scaling mistake.
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return target


def icon(name: str, color: str = "text-primary", size: int = 20) -> QIcon:
    """One icon as a `QIcon`, for buttons and actions."""
    return QIcon(pixmap(name, color, size))


# --------------------------------------------------------------------------
# Stylesheet
# --------------------------------------------------------------------------


def _base() -> str:
    return f"""
QWidget {{
    background: transparent;
    color: {_c('text-primary')};
    font-family: "{Type.FAMILIES[0]}";
    font-size: {Type.BODY}px;
}}

/* The window's own backdrop. A static gradient rather than a flat fill: it is
   what gives the translucent panels above something to sit on, and it repaints
   only on resize. */
QMainWindow, QDialog {{
    background: qlineargradient(x1:0, y1:0, x2:0.6, y2:1,
        stop:0 {_c('background-raised')}, stop:1 {_c('background-base')});
}}

QLabel {{ background: transparent; }}
QLabel[role="title"]     {{ font-size: {Type.TITLE}px; font-weight: {Type.WEIGHT_SEMIBOLD}; }}
QLabel[role="heading"]   {{ font-size: {Type.HEADING}px; font-weight: {Type.WEIGHT_SEMIBOLD}; }}
QLabel[role="label"]     {{ font-size: {Type.LABEL}px; color: {_c('text-secondary')}; }}
QLabel[role="muted"]     {{ font-size: {Type.LABEL}px; color: {_c('text-muted')}; }}
QLabel[role="meta"]      {{ font-size: {Type.META}px; color: {_c('text-muted')}; }}
QLabel[role="metric"]    {{
    font-size: {Type.DISPLAY}px;
    font-weight: {Type.WEIGHT_SEMIBOLD};
    font-family: "{Type.FAMILIES_MONO[0]}";
}}

QToolTip {{
    background: {_flat('surface-solid-raised')};
    color: {_c('text-primary')};
    border: 1px solid {_c('border-strong')};
    border-radius: {Radius.CONTROL}px;
    padding: {Space.SM}px {Space.MD}px;
    font-size: {Type.LABEL}px;
}}

QStatusBar {{
    background: {_c('surface-glass')};
    border-top: 1px solid {_c('border-subtle')};
    color: {_c('text-secondary')};
    font-size: {Type.LABEL}px;
}}
QStatusBar::item {{ border: none; }}
"""


def _surfaces() -> str:
    return f"""
/* Glass. Translucent fill plus a hairline -- see the module docstring for why
   there is no blur. */
QFrame[surface="glass"] {{
    background: {_c('surface-glass')};
    border: 1px solid {_c('border-subtle')};
    border-radius: {Radius.PANEL}px;
}}
QFrame[surface="card"] {{
    background: {_c('surface-glass')};
    border: 1px solid {_c('border-subtle')};
    border-radius: {Radius.CARD}px;
}}
QFrame[surface="solid"] {{
    background: {_c('surface-solid')};
    border: 1px solid {_c('border-subtle')};
    border-radius: {Radius.CARD}px;
}}
QFrame[surface="sunken"] {{
    background: {_c('background-sunken')};
    border: 1px solid {_c('border-subtle')};
    border-radius: {Radius.CARD}px;
}}

/* GroupBox still exists in both applications. Styled so the current layouts
   look right immediately, before any of them are restructured. */
QGroupBox {{
    background: {_c('surface-glass')};
    border: 1px solid {_c('border-subtle')};
    border-radius: {Radius.PANEL}px;
    margin-top: {Space.MD}px;
    padding: {Space.XL}px {Space.LG}px {Space.LG}px {Space.LG}px;
    font-size: {Type.HEADING}px;
    font-weight: {Type.WEIGHT_SEMIBOLD};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: {Space.LG}px;
    padding: 0 {Space.SM}px;
    color: {_c('text-primary')};
}}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:vertical {{ height: {Space.MD}px; }}
QSplitter::handle:horizontal {{ width: {Space.MD}px; }}
"""


def _buttons() -> str:
    return f"""
QPushButton {{
    background: {_c('surface-glass-hover')};
    border: 1px solid {_c('border-subtle')};
    border-radius: {Radius.CONTROL}px;
    padding: {Space.SM}px {Space.LG}px;
    min-height: 20px;
    font-weight: {Type.WEIGHT_MEDIUM};
    color: {_c('text-primary')};
}}
QPushButton:hover   {{ background: {_c('surface-glass-active')};
                       border-color: {_c('border-strong')}; }}
QPushButton:pressed {{ background: {_c('surface-glass')}; }}
QPushButton:focus   {{ border: 2px solid {_c('border-active')}; }}
QPushButton:disabled {{ color: {_c('text-muted')};
                        background: {_c('surface-glass')};
                        border-color: {_c('border-subtle')}; }}

/* Primary: the one accent-filled control in a window. */
QPushButton[variant="primary"] {{
    background: {_c('accent-primary')};
    border: 1px solid {_c('accent-primary')};
    color: {_c('text-on-accent')};
    font-weight: {Type.WEIGHT_SEMIBOLD};
}}
QPushButton[variant="primary"]:hover   {{ background: {_c('accent-primary-hover')};
                                          border-color: {_c('accent-primary-hover')}; }}
QPushButton[variant="primary"]:pressed {{ background: {_c('accent-primary')}; }}
QPushButton[variant="primary"]:disabled {{
    background: {_c('accent-primary-muted')};
    border-color: transparent;
    color: {_c('text-muted')};
}}

/* Destructive: an outline that fills, so it never reads as the primary action
   and never differs from it by colour alone. */
QPushButton[variant="danger"] {{
    background: transparent;
    border: 1px solid {_c('error')};
    color: {_c('error')};
}}
QPushButton[variant="danger"]:hover   {{ background: {_c('error-muted')}; }}
QPushButton[variant="danger"]:pressed {{ background: {_c('error-muted')}; }}

QPushButton[variant="ghost"] {{
    background: transparent;
    border: 1px solid transparent;
    color: {_c('text-secondary')};
    padding: {Space.XS}px {Space.SM}px;
}}
QPushButton[variant="ghost"]:hover {{ background: {_c('surface-glass-hover')};
                                      color: {_c('text-primary')}; }}

/* Icon-only. Square, and large enough to hit from a sofa. */
QPushButton[variant="icon"] {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {Radius.CONTROL}px;
    padding: {Space.SM}px;
    min-width: 32px;
    min-height: 32px;
}}
/* A glyph in a narrow, fixed-width button -- the "x" that clears a binding
   and the "+" that adds a second source. The ordinary button padding is
   16px a side, so a `setFixedWidth(28)` control has *negative* room for its
   label and renders as an empty box: present, clickable, and showing
   nothing. Any caller pinning a button narrower than about 60px wants this. */
QPushButton[compact="true"] {{
    padding: {Space.XS}px;
    min-width: 0;
    font-weight: {Type.WEIGHT_SEMIBOLD};
}}
QPushButton[variant="icon"]:hover   {{ background: {_c('surface-glass-hover')}; }}
QPushButton[variant="icon"]:pressed {{ background: {_c('surface-glass-active')}; }}
QPushButton[variant="icon"]:checked {{ background: {_c('accent-primary-muted')}; }}
"""


def _inputs() -> str:
    return f"""
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {_c('surface-solid-raised')};
    border: 1px solid {_c('border-subtle')};
    border-radius: {Radius.CONTROL}px;
    padding: {Space.SM}px {Space.MD}px;
    selection-background-color: {_c('accent-primary')};
    selection-color: {_c('text-on-accent')};
    min-height: 18px;
}}
QLineEdit:hover, QSpinBox:hover, QComboBox:hover {{ border-color: {_c('border-strong')}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{ border: 2px solid {_c('border-active')}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    color: {_c('text-muted')};
    background: {_c('surface-solid')};
}}
QLineEdit[state="error"] {{ border-color: {_c('error')}; }}

/* **Styling a combo box takes its arrow away and puts nothing back.** Leaving
   this to the base style does not work: measured against plain Fusion, the
   themed control drew no arrow at all *and* stopped eliding its label, so long
   text ran through the space the arrow should occupy and hard-clipped with no
   ellipsis. Reserving the width here is what gives the label a correct rect to
   elide against, so the arrow and the eliding are one fix, not two. */
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    border: none;
    width: {Space.XL}px;
}}
QComboBox::down-arrow {{
    image: url({_indicator('chevron-down.svg')});
    width: 14px;
    height: 14px;
}}
QComboBox::down-arrow:disabled {{ image: none; }}
/* The popup is a separate top-level window, so a translucent fill has nothing
   behind it to blend with -- it must be flattened. */
QComboBox QAbstractItemView {{
    background: {_flat('surface-solid-raised')};
    border: 1px solid {_c('border-strong')};
    border-radius: {Radius.CONTROL}px;
    padding: {Space.XS}px;
    outline: none;
    selection-background-color: {_c('accent-primary-muted')};
    selection-color: {_c('text-primary')};
}}

/* Same trap as the combo box: styled, these buttons lose their arrows. */
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    background: transparent;
    border: none;
    width: {Space.LG}px;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url({_indicator('chevron-up.svg')});
    width: 10px;
    height: 10px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url({_indicator('chevron-down.svg')});
    width: 10px;
    height: 10px;
}}

/* Explicit size, or the indicator collapses to nothing under a stylesheet. */
QCheckBox, QRadioButton {{ spacing: {Space.SM}px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 18px; height: 18px; }}
QCheckBox::indicator {{
    border: 1px solid {_c('border-strong')};
    border-radius: 5px;
    background: {_c('surface-solid-raised')};
}}
QRadioButton::indicator {{
    border: 1px solid {_c('border-strong')};
    border-radius: 9px;
    background: {_c('surface-solid-raised')};
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {_c('accent-primary')};
    border-color: {_c('accent-primary')};
}}
/* Styling `::indicator` at all takes the glyph away from Qt, so without these
   a checked box is a plain blue square and a checked radio a plain blue disc
   -- state told by colour alone, which is exactly what the status language
   forbids. QSS resolves `image:` from a path, never a data URI, so these two
   are the one part of the icon set that has to exist as files on disk. */
QCheckBox::indicator:checked {{ image: url({_indicator('check.svg')}); }}
QRadioButton::indicator:checked {{ image: url({_indicator('radio.svg')}); }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {_c('accent-primary')};
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    background: {_c('surface-solid')};
    border-color: {_c('border-subtle')};
}}
QCheckBox:disabled, QRadioButton:disabled {{ color: {_c('text-muted')}; }}

QSlider::groove:horizontal {{
    height: 4px;
    background: {_c('surface-solid-raised')};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {_c('accent-primary')}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {_c('text-primary')};
    width: 14px; height: 14px;
    margin: -6px 0;
    border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{ background: #FFFFFF; }}
"""


def _collections() -> str:
    return f"""
QTableWidget, QTableView, QTreeView, QListView {{
    background: {_c('surface-solid')};
    alternate-background-color: {_c('surface-glass')};
    border: 1px solid {_c('border-subtle')};
    border-radius: {Radius.CARD}px;
    gridline-color: {_c('border-subtle')};
    selection-background-color: {_c('accent-primary-muted')};
    selection-color: {_c('text-primary')};
    outline: none;
}}
/* **No padding here.** An item view places a `setCellWidget` widget in the
   item's own rect, so padding on this selector shrinks every embedded control
   by twice its value -- measured: a 111px column gave its button 94px, which
   clipped the label to "onfigure.". Row height carries the breathing room
   instead, and text items are centred so a flush cell edge reads as
   deliberate. */
QTableWidget::item, QTableView::item {{ padding: 0; }}
QHeaderView::section {{
    background: transparent;
    color: {_c('text-muted')};
    border: none;
    border-bottom: 1px solid {_c('border-subtle')};
    padding: {Space.SM}px;
    font-size: {Type.META}px;
    font-weight: {Type.WEIGHT_SEMIBOLD};
    text-transform: uppercase;
}}
QTableCornerButton::section {{ background: transparent; border: none; }}

QTabWidget::pane {{
    background: {_c('surface-glass')};
    border: 1px solid {_c('border-subtle')};
    border-radius: {Radius.CARD}px;
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {_c('text-secondary')};
    border: none;
    padding: {Space.SM}px {Space.LG}px;
    margin-right: {Space.XS}px;
    border-radius: {Radius.CONTROL}px;
}}
QTabBar::tab:hover {{ color: {_c('text-primary')}; background: {_c('surface-glass-hover')}; }}
QTabBar::tab:selected {{ color: {_c('text-primary')}; background: {_c('surface-glass-active')}; }}
QTabBar:focus {{ outline: none; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle {{ background: {_c('border-strong')}; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:hover {{ background: {_c('text-muted')}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QMenu {{
    background: {_flat('surface-solid-raised')};
    border: 1px solid {_c('border-strong')};
    border-radius: {Radius.CONTROL}px;
    padding: {Space.XS}px;
}}
QMenu::item {{ padding: {Space.SM}px {Space.LG}px; border-radius: 6px; }}
QMenu::item:selected {{ background: {_c('accent-primary-muted')}; }}
"""


log = logging.getLogger(__name__)

#: Glyphs the stylesheet loads from disk. Generated by
#: `tools/build_design_tokens.py`; see `qtui/assets/__init__.py` for why they
#: cannot be inlined like every other icon.
_ASSETS = Path(__file__).resolve().parent / "assets"


@functools.lru_cache(maxsize=1)
def _indicator_images_available() -> bool:
    """Whether Qt can actually read the indicator SVGs.

    `svg` is an imageformats *plugin*, separate from the QtSvg module, so a
    stripped bundle can ship one without the other. Qt drops an `image:` rule
    it cannot load without a word, which would put the tick back to being
    invisible -- so this is checked rather than assumed, and the miss is
    logged: a checkbox that silently stops showing its own state is precisely
    the failure being fixed here.
    """
    from PySide6.QtGui import QImageReader

    if b"svg" in QImageReader.supportedImageFormats():
        return True
    log.warning(
        "Qt has no SVG image plugin, so checkbox ticks and radio dots cannot "
        "be drawn; those controls will show their state by fill colour alone"
    )
    return False


def _indicator(name: str) -> str:
    """An absolute path Qt can load, or an empty URL that disables the rule.

    Forward slashes on every platform: a Windows backslash inside `url()` is
    read as an escape and the rule is dropped.
    """
    if not _indicator_images_available():
        return ""
    return (_ASSETS / name).as_posix()


def stylesheet() -> str:
    """The whole application stylesheet, built from the tokens."""
    return "\n".join(
        (_base(), _surfaces(), _buttons(), _inputs(), _collections())
    )


def _build_palette() -> QPalette:
    """Palette for what the stylesheet does not reach.

    Qt draws some things -- disabled text, item-view backgrounds, the text
    cursor -- from the palette rather than from the stylesheet. Leaving it at
    the platform default is how a themed application ends up with one stubbornly
    light-grey widget nobody can find the rule for.
    """
    qp = QPalette()
    qp.setColor(QPalette.ColorRole.Window, qcolor("background-base"))
    qp.setColor(QPalette.ColorRole.WindowText, qcolor("text-primary"))
    qp.setColor(QPalette.ColorRole.Base, qcolor("surface-solid"))
    qp.setColor(QPalette.ColorRole.AlternateBase, qcolor("surface-solid-raised"))
    qp.setColor(QPalette.ColorRole.Text, qcolor("text-primary"))
    qp.setColor(QPalette.ColorRole.Button, qcolor("surface-solid-raised"))
    qp.setColor(QPalette.ColorRole.ButtonText, qcolor("text-primary"))
    qp.setColor(QPalette.ColorRole.Highlight, qcolor("accent-primary"))
    qp.setColor(QPalette.ColorRole.HighlightedText, qcolor("text-on-accent"))
    qp.setColor(QPalette.ColorRole.ToolTipBase, qcolor("surface-solid-raised"))
    qp.setColor(QPalette.ColorRole.ToolTipText, qcolor("text-primary"))
    qp.setColor(QPalette.ColorRole.PlaceholderText, qcolor("text-muted"))
    qp.setColor(QPalette.ColorRole.Link, qcolor("accent-primary"))

    for group in (QPalette.ColorGroup.Disabled,):
        qp.setColor(group, QPalette.ColorRole.Text, qcolor("text-muted"))
        qp.setColor(group, QPalette.ColorRole.ButtonText, qcolor("text-muted"))
        qp.setColor(group, QPalette.ColorRole.WindowText, qcolor("text-muted"))
    return qp


def apply_theme(app) -> None:
    """Theme a `QApplication`. Call once, before any window is shown.

    Fusion first: it is the only style that honours a stylesheet consistently
    across platforms. The native Windows and macOS styles ignore several of the
    rules above, which produces a window that is themed in parts.
    """
    app.setStyle("Fusion")
    app.setPalette(_build_palette())

    font = QFont()
    font.setFamilies(list(Type.FAMILIES))
    font.setPointSizeF(app.font().pointSizeF() or 9.5)
    app.setFont(font)

    app.setStyleSheet(stylesheet())


def restyle(widget) -> None:
    """Re-evaluate a widget's style after a dynamic property changed.

    Qt resolves stylesheet selectors when a widget is *polished*. A property
    that a selector depends on -- `variant`, `state`, `surface` -- therefore has
    no effect if it is set afterwards, which looks like the rule being wrong
    rather than like it never being applied.
    """
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


#: Re-exported so widgets can reference durations without another import.
MOTION = Motion

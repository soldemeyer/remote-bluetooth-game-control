"""The GPU presentation surface, and the overlay that has to ride on it.

A native child window. Qt's raster path cannot present a Direct3D swap chain,
and neither of the two routes that would have let Qt own the device exists in
PySide6 -- ``QRhi.nativeHandles()`` is not downcast to the D3D11 form, and
``QVulkanInstance`` is not bound at all. So the renderer creates its own swap
chain on a ``QWindow`` of surface type ``Direct3DSurface``, embedded with
``createWindowContainer``.

WHAT THAT COSTS, AND WHY THE OVERLAY IS HERE
---------------------------------------------
A native child window draws above every Qt *sibling*. Popups, menus, tooltips
and dialogs are separate top-level windows and are unaffected, so the damage is
exactly two things: the latency overlay and the floating control bar.

Hiding the latency overlay in the one mode where somebody is trying to prove
presentation got faster would be self-defeating, so both are drawn into an
image here and composited by the renderer instead. The image is rebuilt only
when it changes -- at most ten times a second, usually far less.

INPUT HAS TO BE FORWARDED, and it is not optional
--------------------------------------------------
Two independent reasons the obvious shortcut does not work:

* a hidden ``QWidget`` receives no mouse events at all -- Qt's hit test walks
  visible children only;
* even a visible one is below the native child in the platform's z-order, so
  Windows delivers the click to the native window regardless of Qt's opinion.

So "leave the real widget there for hit testing" is not a thing that can work,
and events are mapped and delivered by hand. Hover enter and leave have to be
synthesised too: ``sendEvent`` does not produce them, and without them the bar
looks dead rather than merely unstyled.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QWindow
from PySide6.QtWidgets import QApplication, QWidget

log = logging.getLogger(__name__)

#: Padding around the overlay text, matching the software path's own.
_OSD_MARGIN = 14


class NativeSurface(QWidget):
    """A window the renderer can present into, embedded in the layout.

    Deliberately thin. It owns a handle and its geometry; everything about
    what appears on it belongs to the renderer, which reads the window's own
    client rectangle each frame. That is what makes resize, fullscreen, DPI
    changes and monitor moves need no code here at all -- and it sidesteps the
    trap the client already documents, that moving to a monitor with a
    different device pixel ratio raises no resize event of its own.
    """

    #: The pointer moved over the surface. The stage wakes its chrome on this.
    activity = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)

        #: The widget that pointer events are delivered to by hand. See the
        #: module docstring: it is below the native child in the platform's
        #: z-order, so it cannot receive them any other way.
        self._input_target: QWidget | None = None
        self._hover: dict = {}

        self._window = QWindow()
        self._window.setSurfaceType(QWindow.SurfaceType.Direct3DSurface)
        self._container = QWidget.createWindowContainer(self._window, self)
        self._container.setMouseTracking(True)
        # The container must not take focus, or the window's keyboard
        # shortcuts -- mute, volume, fullscreen, the overlay toggle -- stop
        # reaching the widget that handles them. "Volume keys stopped working
        # in upscale mode" is the bug report that would follow.
        self._container.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._container.installEventFilter(self)

    @property
    def native_handle(self) -> int:
        """The HWND the renderer presents into, or 0 before it exists."""
        try:
            return int(self._window.winId())
        except Exception:  # noqa: BLE001
            return 0

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._container.setGeometry(0, 0, self.width(), self.height())

    def set_input_target(self, widget: QWidget | None) -> None:
        """Where forwarded pointer events go, or None to forward none."""
        if widget is not self._input_target:
            _leave(self._hover)
        self._input_target = widget

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind in (QEvent.Type.MouseMove, QEvent.Type.Enter,
                    QEvent.Type.HoverMove):
            self.activity.emit()

        # Forwarded rather than handled. The native child is what the platform
        # delivers to; the control bar is a Qt widget underneath it, and
        # without this it is a picture of a bar rather than a bar.
        if kind in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress,
                    QEvent.Type.MouseButtonRelease,
                    QEvent.Type.MouseButtonDblClick):
            target = self._input_target
            if target is not None:
                try:
                    taken = forward_mouse(
                        target, event.globalPosition().toPoint(), kind,
                        event.button(), event.modifiers(), self._hover)
                except RuntimeError:
                    # The bar was destroyed between events.
                    self._input_target = None
                    taken = False
                if taken:
                    return True
        elif kind == QEvent.Type.Leave:
            _leave(self._hover)
        return False

    def release(self) -> None:
        """Drop the platform window.

        **Before anything reparents this widget.** ``VideoStage.set_surface``
        calls ``setParent(None)`` on the outgoing surface, which destroys and
        recreates the platform window -- and the renderer's swap chain is
        bound to the old handle. Tearing down here means the renderer is
        already gone by then, rather than presenting into a dead window.
        """
        try:
            self._container.removeEventFilter(self)
        except Exception:  # noqa: BLE001
            pass


class OverlayPainter:
    """The client's own chrome, as one image for the renderer to composite.

    Rebuilt only when something in it changes, and versioned so the renderer
    can skip the upload when it has not. The version is what makes "re-send it
    every frame" cost nothing.
    """

    def __init__(self) -> None:
        self._image: QImage | None = None
        self._version = 0
        self._signature: tuple = ()
        self._rect = (0, 0, 0, 0)

    @property
    def version(self) -> int:
        return self._version

    def clear(self) -> None:
        self._image = None
        self._signature = ()

    def update(self, *, lines: list[str], bar_image: QImage | None,
               bar_at: QPoint | None, size: tuple[int, int],
               font, ink: QColor, panel: QColor) -> bool:
        """Rebuild if anything visible changed. Returns True if it did.

        The signature is what makes this cheap: the overlay's text changes at
        most ten times a second and the bar only on hover, so nearly every
        call finds nothing to do and returns immediately.
        """
        # The signature decides whether anything is redrawn. The overlay's
        # text changes at most ten times a second, so with the bar hidden --
        # which is nearly always, it auto-hides after a few seconds -- almost
        # every call returns here having done nothing.
        #
        # **While the bar is up, this rebuilds every tick, and that is
        # deliberate rather than an oversight.** `grab()` returns a fresh
        # pixmap each time, so its cacheKey is useless as a change test, and
        # anything that actually compared the pixels would cost more than
        # redrawing. Qt offers no "has this widget changed" signal. So the bar
        # counts as changed whenever it is visible: bounded, because it hides
        # itself, and at 10 Hz, which is the rate the overlay redraws at anyway
        # whenever the latency readout is on.
        signature = (
            tuple(lines),
            size,
            None if bar_image is None else (
                "visible", bar_at.x() if bar_at else 0,
                bar_at.y() if bar_at else 0, self._version),
        )
        if signature == self._signature and self._image is not None:
            return False
        self._signature = signature

        width, height = max(1, size[0]), max(1, size[1])
        if not lines and bar_image is None:
            self._image = None
            self._version += 1
            return True

        # Premultiplied, because that is what a straight src + dst*(1-a) blend
        # wants and it maps to DXGI_FORMAT_R8G8B8A8_UNORM with no swizzle.
        # ARGB32 would cost a per-pixel channel swap and leave a dark fringe
        # everywhere the panel is translucent -- which, for a deliberately
        # glassy design, is everywhere.
        image = QImage(width, height, QImage.Format.Format_RGBA8888_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)

        painter = QPainter(image)
        try:
            if lines:
                self._draw_lines(painter, lines, font, ink, panel)
            if bar_image is not None and bar_at is not None:
                painter.drawImage(bar_at, bar_image)
        finally:
            painter.end()

        self._image = image
        self._version += 1
        return True

    def _draw_lines(self, painter, lines, font, ink, panel) -> None:
        painter.setFont(font)
        metrics = painter.fontMetrics()
        line_height = metrics.height() + 2
        widest = max(metrics.horizontalAdvance(line) for line in lines)

        box = QRect(
            _OSD_MARGIN, _OSD_MARGIN,
            widest + _OSD_MARGIN * 2,
            line_height * len(lines) + _OSD_MARGIN * 2,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(panel)
        painter.drawRoundedRect(box, 8, 8)

        painter.setPen(ink)
        y = box.top() + _OSD_MARGIN + metrics.ascent()
        for line in lines:
            painter.drawText(box.left() + _OSD_MARGIN, y, line)
            y += line_height

    def to_overlay(self):
        """The renderer's view of it, or None when there is nothing to draw.

        The address comes through ctypes rather than from ``constBits()``,
        which in PySide6 returns a **memoryview** -- ``int()`` of one is a
        ValueError quoting several hundred bytes of pixel data, which is a
        confusing way to learn that. ``bits()`` gives a writable view, and
        ``c_char.from_buffer`` on it yields the real address; verified by
        poking through it and reading the change back out of the QImage.
        """
        if self._image is None:
            return None
        import ctypes

        from client.media.gpu_upscaler import Overlay

        view = self._image.bits()
        return Overlay(
            address=ctypes.addressof(ctypes.c_char.from_buffer(view)),
            stride=self._image.bytesPerLine(),
            x=0,
            y=0,
            width=self._image.width(),
            height=self._image.height(),
            version=self._version,
            # Keeps the pixels alive for the duration of the submit call.
            # QImage frees its buffer when the last reference goes and the
            # native side keeps none of its own. The memoryview goes in the
            # tuple too: `from_buffer` exports a buffer, and the QImage
            # refuses to be destroyed while one is outstanding -- dropping it
            # early raises rather than corrupting anything, which is the right
            # way round but still a crash.
            owner=(self._image, view),
        )


def forward_mouse(bar: QWidget, point: QPoint, event_type, button, modifiers,
                  state: dict) -> bool:
    """Deliver a pointer event to the control bar by hand.

    Returns True when the bar took it. ``state`` carries the previously
    hovered child between calls, so enter and leave can be synthesised --
    ``sendEvent`` does not produce them, and a bar that never receives them
    has no hover styling and no tooltips, which reads as it being dead.
    """
    if bar is None or not bar.isVisible():
        return False
    local = bar.mapFromGlobal(point)
    if not bar.rect().contains(local):
        _leave(state)
        return False

    child = bar.childAt(local) or bar
    previous = state.get("hovered")
    if previous is not child:
        _leave(state)
        QApplication.sendEvent(child, QEvent(QEvent.Type.Enter))
        state["hovered"] = child

    from PySide6.QtGui import QMouseEvent

    target_point = child.mapFromGlobal(point)
    QApplication.sendEvent(
        child,
        QMouseEvent(event_type, target_point, point, button, button, modifiers),
    )
    return True


def _leave(state: dict) -> None:
    previous = state.pop("hovered", None)
    if previous is not None:
        try:
            QApplication.sendEvent(previous, QEvent(QEvent.Type.Leave))
        except RuntimeError:
            # The widget was destroyed between events. Nothing to leave.
            pass

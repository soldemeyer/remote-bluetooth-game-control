"""The overlay that has to be composited, and the input that has to be forwarded.

A native child window draws above every Qt sibling, which costs exactly two
things: the latency overlay and the floating control bar. Both are drawn into
an image for the renderer instead, and the bar's pointer events are delivered
by hand.

None of this needs a GPU. What it needs is a Qt application, which the rest of
the suite already runs offscreen.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="client GUI extras not installed")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QHBoxLayout,
    QPushButton,
    QWidget,
)

from client.gui.video_surface import OverlayPainter, forward_mouse  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def painter_args(lines, size=(640, 360), bar_image=None, bar_at=None):
    font = QFont()
    font.setPixelSize(13)
    return {
        "lines": lines,
        "bar_image": bar_image,
        "bar_at": bar_at,
        "size": size,
        "font": font,
        "ink": QColor(255, 255, 255),
        "panel": QColor(0, 0, 0, 160),
    }


class TestItOnlyRedrawsWhenSomethingChanged:
    """The whole point of carrying a version: re-sending it every frame has to
    cost nothing."""

    def test_the_same_text_twice_is_not_a_redraw(self, qt_app):
        overlay = OverlayPainter()
        assert overlay.update(**painter_args(["video  p50 4.1 ms"])) is True
        assert overlay.update(**painter_args(["video  p50 4.1 ms"])) is False

    def test_changed_text_is_a_redraw(self, qt_app):
        overlay = OverlayPainter()
        overlay.update(**painter_args(["video  p50 4.1 ms"]))
        assert overlay.update(**painter_args(["video  p50 9.9 ms"])) is True

    def test_a_resize_is_a_redraw(self, qt_app):
        overlay = OverlayPainter()
        overlay.update(**painter_args(["a"], size=(640, 360)))
        assert overlay.update(**painter_args(["a"], size=(1280, 720))) is True

    def test_the_version_only_moves_on_a_redraw(self, qt_app):
        overlay = OverlayPainter()
        overlay.update(**painter_args(["a"]))
        first = overlay.version
        overlay.update(**painter_args(["a"]))
        assert overlay.version == first
        overlay.update(**painter_args(["b"]))
        assert overlay.version > first


class TestWhatTheRendererIsGiven:
    def test_nothing_to_draw_means_no_overlay(self, qt_app):
        overlay = OverlayPainter()
        overlay.update(**painter_args([]))
        assert overlay.to_overlay() is None

    def test_an_overlay_carries_a_real_address(self, qt_app):
        """PySide6's ``constBits()`` returns a memoryview, not an address --
        ``int()`` of one raises ValueError quoting several hundred bytes of
        pixel data, which is a confusing way to learn it."""
        overlay = OverlayPainter()
        overlay.update(**painter_args(["video  p50 4.1 ms"]))
        handed = overlay.to_overlay()

        assert handed is not None
        assert isinstance(handed.address, int) and handed.address > 0
        assert handed.stride >= handed.width * 4
        assert handed.width > 0 and handed.height > 0

    def test_it_keeps_the_pixels_alive(self, qt_app):
        """The renderer copies the image into a texture and keeps nothing of
        its own, so whatever owns the bytes has to survive the call. QImage
        frees its buffer when the last reference goes."""
        overlay = OverlayPainter()
        overlay.update(**painter_args(["x"]))
        handed = overlay.to_overlay()
        assert handed.owner is not None

    def test_the_format_is_premultiplied(self, qt_app):
        """It maps to DXGI_FORMAT_R8G8B8A8_UNORM with no swizzle, and
        premultiplied is what a src + dst*(1-a) blend wants. ARGB32 would cost
        a per-pixel channel swap and leave a dark fringe wherever the panel is
        translucent -- which, for a glassy design, is everywhere."""
        overlay = OverlayPainter()
        overlay.update(**painter_args(["x"]))
        assert overlay._image.format() == QImage.Format.Format_RGBA8888_Premultiplied

    def test_the_text_actually_reaches_the_pixels(self, qt_app):
        """Otherwise this is an expensive way to composite a transparent
        rectangle."""
        overlay = OverlayPainter()
        overlay.update(**painter_args(["video  p50 4.1 ms"]))
        image = overlay._image

        opaque = 0
        for y in range(0, image.height(), 3):
            for x in range(0, image.width(), 3):
                if image.pixelColor(x, y).alpha() > 0:
                    opaque += 1
        assert opaque > 50, "the overlay is empty"

    def test_most_of_it_stays_transparent(self, qt_app):
        """It is composited over the picture. An overlay that filled its
        rectangle would be a curtain."""
        overlay = OverlayPainter()
        overlay.update(**painter_args(["short"], size=(640, 360)))
        image = overlay._image
        corner = image.pixelColor(image.width() - 2, image.height() - 2)
        assert corner.alpha() == 0


class TestTheControlBarIsDrawnToo:
    def test_a_bar_image_is_composited_at_its_position(self, qt_app):
        overlay = OverlayPainter()
        bar = QImage(60, 20, QImage.Format.Format_RGBA8888_Premultiplied)
        bar.fill(QColor(255, 0, 0, 255))

        overlay.update(**painter_args([], bar_image=bar, bar_at=QPoint(100, 200)))
        image = overlay._image

        assert image is not None
        assert image.pixelColor(110, 205).alpha() > 0, "the bar was not drawn"
        assert image.pixelColor(10, 10).alpha() == 0, "it was drawn in the wrong place"

    def test_a_visible_bar_redraws_every_call_and_that_is_deliberate(self, qt_app):
        """``grab()`` returns a fresh pixmap each time, so its cacheKey cannot
        be a change test and comparing the pixels would cost more than
        redrawing. The bar hides itself after a few seconds, so the cost is
        bounded -- and while it is up the overlay is redrawing anyway."""
        overlay = OverlayPainter()
        bar = QImage(10, 10, QImage.Format.Format_RGBA8888_Premultiplied)
        bar.fill(QColor(0, 255, 0, 255))

        args = painter_args([], bar_image=bar, bar_at=QPoint(0, 0))
        assert overlay.update(**args) is True
        assert overlay.update(**args) is True

    def test_the_bar_going_away_is_a_redraw(self, qt_app):
        overlay = OverlayPainter()
        bar = QImage(10, 10, QImage.Format.Format_RGBA8888_Premultiplied)
        bar.fill(QColor(0, 0, 255, 255))
        overlay.update(**painter_args(["x"], bar_image=bar, bar_at=QPoint(0, 0)))
        assert overlay.update(**painter_args(["x"])) is True


class TestForwardingPointerEvents:
    """The native child is what the platform delivers to. Without forwarding
    the bar is a picture of a bar."""

    @pytest.fixture
    def bar(self, qt_app):
        holder = QWidget()
        holder.resize(200, 40)
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        left = QPushButton("left", holder)
        right = QPushButton("right", holder)
        row.addWidget(left)
        row.addWidget(right)
        holder.show()
        holder.left = left
        holder.right = right
        try:
            yield holder
        finally:
            holder.close()

    def test_a_click_inside_reaches_the_right_child(self, bar):
        clicked: list[str] = []
        bar.left.clicked.connect(lambda: clicked.append("left"))
        bar.right.clicked.connect(lambda: clicked.append("right"))

        state: dict = {}
        point = bar.right.mapToGlobal(bar.right.rect().center())
        assert forward_mouse(bar, point, QEvent.Type.MouseButtonPress,
                             Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier, state)
        assert forward_mouse(bar, point, QEvent.Type.MouseButtonRelease,
                             Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier, state)
        assert clicked == ["right"]

    def test_a_point_outside_is_not_taken(self, bar):
        state: dict = {}
        outside = bar.mapToGlobal(bar.rect().bottomRight()) + QPoint(200, 200)
        assert forward_mouse(bar, outside, QEvent.Type.MouseMove,
                             Qt.MouseButton.NoButton,
                             Qt.KeyboardModifier.NoModifier, state) is False

    def test_hover_enter_and_leave_are_synthesised(self, bar):
        """``sendEvent`` does not produce them, and a bar that never receives
        them has no hover styling and no tooltips -- which reads as it being
        dead rather than merely unstyled."""
        state: dict = {}
        left_point = bar.left.mapToGlobal(bar.left.rect().center())
        right_point = bar.right.mapToGlobal(bar.right.rect().center())

        forward_mouse(bar, left_point, QEvent.Type.MouseMove,
                      Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, state)
        assert state.get("hovered") is bar.left

        forward_mouse(bar, right_point, QEvent.Type.MouseMove,
                      Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, state)
        assert state.get("hovered") is bar.right

    def test_leaving_the_bar_clears_the_hover(self, bar):
        state: dict = {}
        inside = bar.left.mapToGlobal(bar.left.rect().center())
        forward_mouse(bar, inside, QEvent.Type.MouseMove, Qt.MouseButton.NoButton,
                      Qt.KeyboardModifier.NoModifier, state)
        assert "hovered" in state

        outside = bar.mapToGlobal(bar.rect().bottomRight()) + QPoint(500, 500)
        forward_mouse(bar, outside, QEvent.Type.MouseMove, Qt.MouseButton.NoButton,
                      Qt.KeyboardModifier.NoModifier, state)
        assert "hovered" not in state

    def test_a_hidden_bar_takes_nothing(self, bar):
        bar.hide()
        state: dict = {}
        point = bar.mapToGlobal(bar.rect().center())
        assert forward_mouse(bar, point, QEvent.Type.MouseMove,
                             Qt.MouseButton.NoButton,
                             Qt.KeyboardModifier.NoModifier, state) is False

    def test_no_bar_at_all_is_harmless(self, qt_app):
        assert forward_mouse(None, QPoint(0, 0), QEvent.Type.MouseMove,
                             Qt.MouseButton.NoButton,
                             Qt.KeyboardModifier.NoModifier, {}) is False

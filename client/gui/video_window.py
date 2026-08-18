"""The video window: paints the stream and shows what it cost.

Follows the client's established rule -- no signals from worker threads into
Qt. A short timer checks the decoder's version counter and repaints only when
there is genuinely a new frame, which keeps an idle or stalled stream from
burning CPU on identical repaints.

The window is *not* a dialog. A modal ``exec()`` would block the main window,
and the player needs to reach the controller table while watching.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import QWidget

from common.timing import LatencyStats, now_ns

log = logging.getLogger(__name__)

#: Repaint check interval. Well above any sane frame rate, so presentation is
#: never the thing adding latency; the check itself is one integer compare.
_TICK_MS = 5

_OSD_MARGIN = 14


class VideoWindow(QWidget):
    """Displays the decoded stream, with an optional latency overlay."""

    #: Emitted when the window closes, however it was closed.
    #:
    #: Not ``QObject.destroyed``: this window has a parent and the main window
    #: keeps a reference, so closing it only hides it and the C++ object is
    #: never deleted. Connecting to ``destroyed`` therefore fired nothing, and
    #: the "Watch stream" button stayed reading "Close video" for good.
    closed = Signal()

    #: Volume shortcuts. The window does not own the audio, so it asks.
    volume_nudged = Signal(int)      # percentage points, positive or negative
    mute_toggled = Signal()

    def __init__(self, decoder, receiver, parent=None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self._decoder = decoder
        self._receiver = receiver

        self.setWindowTitle("Remote video")
        self.setMinimumSize(320, 180)
        self.resize(960, 540)
        self.setAutoFillBackground(False)
        # Repaints are wholesale; letting Qt clear first only causes flicker.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        if parent is not None and not parent.windowIcon().isNull():
            self.setWindowIcon(parent.windowIcon())

        self._image: QImage | None = None
        #: Keeps the bytes the current QImage wraps alive. QImage does not copy
        #: when constructed over a buffer, so dropping this would paint freed
        #: memory.
        self._frame_data: bytes | None = None
        self._last_version = -1
        self._show_osd = True
        self._controller_rtt_ms = 0.0
        self._last_present_ns = 0

        #: Kept here rather than on the receiver because it is measured at the
        #: moment of painting -- the only place that knows when the picture
        #: actually reached the screen.
        self.present_interval = LatencyStats()

        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._check_for_frame)
        self._timer.start()

        fullscreen = QShortcut(QKeySequence(Qt.Key.Key_F11), self)
        fullscreen.activated.connect(self.toggle_fullscreen)
        overlay = QShortcut(QKeySequence(Qt.Key.Key_L), self)
        overlay.activated.connect(self.toggle_osd)

    # -- presentation ------------------------------------------------------

    def _check_for_frame(self) -> None:
        version = self._decoder.version
        if version == self._last_version:
            return
        self._last_version = version

        frame = self._decoder.latest()
        if frame is None:
            return

        # QImage wraps the buffer without copying, so the bytes must outlive
        # the image. Holding a reference to the frame's data does that: the
        # decoder publishes a fresh object per frame and never writes back
        # into one it has already handed over.
        self._frame_data = frame.data
        self._image = QImage(
            self._frame_data,
            frame.width,
            frame.height,
            frame.stride,
            QImage.Format.Format_RGB888,
        )

        present_ns = now_ns()
        # Gate on the clock being locked, never on the offset being non-zero:
        # zero is a perfectly ordinary offset (same machine, or two clocks that
        # happen to agree), and treating it as "not ready" hides the latency
        # display exactly where it is easiest to verify.
        if self._receiver.clock_locked:
            local_capture = frame.capture_ts + self._receiver.clock_offset_ns
            latency_ms = (present_ns - local_capture) / 1_000_000
            # A negative figure means the clocks have not settled; recording it
            # would drag the median somewhere impossible.
            if latency_ms > 0:
                self._receiver.present_stats.add(latency_ms)
        if self._last_present_ns:
            self.present_interval.add((present_ns - self._last_present_ns) / 1_000_000)
        self._last_present_ns = present_ns

        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0))

        if self._image is not None and not self._image.isNull():
            size = self._image.size().scaled(
                self.size(), Qt.AspectRatioMode.KeepAspectRatio
            )
            target = QRect(
                (self.width() - size.width()) // 2,
                (self.height() - size.height()) // 2,
                size.width(),
                size.height(),
            )
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(target, self._image)
        else:
            self._draw_message(painter)

        if self._show_osd:
            self._draw_osd(painter)
        painter.end()

    def _draw_message(self, painter: QPainter) -> None:
        state = self._receiver.state.name.replace("_", " ").title()
        detail = self._receiver.state_detail
        text = f"{state}\n{detail}" if detail else state

        painter.setPen(QColor(200, 200, 200))
        font = QFont()
        font.setPointSize(13)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)

    def _draw_osd(self, painter: QPainter) -> None:
        lines = self.osd_lines()
        if not lines:
            return

        font = QFont("Consolas")
        font.setPointSize(10)
        painter.setFont(font)
        metrics = painter.fontMetrics()

        width = max(metrics.horizontalAdvance(line) for line in lines) + _OSD_MARGIN * 2
        height = metrics.height() * len(lines) + _OSD_MARGIN * 2

        painter.fillRect(
            _OSD_MARGIN, _OSD_MARGIN, width, height, QColor(0, 0, 0, 150)
        )
        painter.setPen(QColor(235, 235, 235))
        y = _OSD_MARGIN * 2 + metrics.ascent()
        for line in lines:
            painter.drawText(_OSD_MARGIN * 2, y, line)
            y += metrics.height()

    def osd_lines(self) -> list[str]:
        """The overlay's text. Separate so it can be tested without painting."""
        video_stats = self._receiver.present_stats
        video_p50 = video_stats.p50
        video_p99 = video_stats.p99

        if not self._receiver.clock_locked:
            video_text = "video      syncing clocks..."
        else:
            video_text = f"video      p50 {video_p50:6.1f} ms   p99 {video_p99:6.1f} ms"

        lines = [video_text]

        if self._controller_rtt_ms:
            lines.append(f"controller rtt {self._controller_rtt_ms:6.1f} ms")
            if self._receiver.clock_locked and video_p50:
                # Half the controller round trip is the outbound leg; the video
                # figure is already one-way. Console processing and the capture
                # card's own delay are outside anything we can see.
                combined = self._controller_rtt_ms / 2 + video_p50
                lines.append(f"combined   {combined:10.1f} ms  (excl. console)")

        mode = self._receiver.connection_mode
        if mode == "relay":
            lines.append("path       relayed -- higher latency")
        elif mode != "direct":
            lines.append(f"path       {mode}")

        return lines

    def set_controller_rtt(self, rtt_ms: float) -> None:
        """Feed the controller figure in, so the overlay can combine the two."""
        self._controller_rtt_ms = rtt_ms

    # -- interaction -------------------------------------------------------

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def toggle_osd(self) -> None:
        self._show_osd = not self._show_osd
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.toggle_fullscreen()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        key = event.key()
        if key == Qt.Key.Key_Escape and self.isFullScreen():
            self.showNormal()
            return

        # Volume, so it is reachable while watching -- the controls themselves
        # live on the main window, which is behind this one and gone entirely
        # in fullscreen. Emitted rather than applied: this window has no
        # business knowing where the audio goes.
        if key == Qt.Key.Key_M:
            self.mute_toggled.emit()
            return
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.volume_nudged.emit(5)
            return
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Minus):
            self.volume_nudged.emit(-5)
            return

        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._timer.stop()
        self.closed.emit()
        super().closeEvent(event)

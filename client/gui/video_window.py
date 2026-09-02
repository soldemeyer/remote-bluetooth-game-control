"""The video window: paints the stream and shows what it cost.

**Where the latency stamp is taken is the whole point of this file.** It used
to be taken in the timer callback, at the moment the decoded bytes were wrapped
in a ``QImage`` -- before ``update()`` had even been called. So the one
end-to-end figure the player is shown, and the one the receiver report carries
back to the source, and the one the audio governor synchronises against,
excluded the paint, the backing-store flush and the compositor entirely. It was
not a slightly optimistic number; it was a number that stopped before the
largest client-side term.

It is now taken at the end of ``paintEvent``, so it covers everything up to the
point Qt has finished drawing. What it still cannot see is the compositor and
scanout after that, which no portable API exposes -- so the overlay says so
rather than implying the figure is complete.

A decoded frame is delivered by *signal*, not by polling. The rule this file
follows has always been "no worker thread touches Qt", and a 5 ms timer was how
it was obeyed; the cost was 0-5 ms of a finished frame sitting there waiting to
be noticed. A queued-connection signal obeys the same rule -- Qt marshals it
onto this thread -- and delivers on the next turn of the event loop. The timer
survives as a slow safety net, because a dropped notification must degrade to
"a bit late" rather than to a frozen window.

The window is *not* a dialog. A modal ``exec()`` would block the main window,
and the player needs to reach the controller table while watching.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QImage, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import QWidget

from common.design.tokens import Type
from common.timing import LatencyStats, now_ns
from qtui.theme import qcolor

log = logging.getLogger(__name__)

#: Safety net only. Frames arrive by signal; this exists so a notification lost
#: to any cause at all costs one tick of lateness rather than a window frozen
#: for good. Deliberately far slower than the frame rate -- it is not the
#: delivery path, and making it fast again would quietly restore the polling
#: latency the signal removed.
_SAFETY_TICK_MS = 100

_OSD_MARGIN = 14

# **Resolved once, at import.** These are used inside `paintEvent`, which runs
# per frame and holds the GIL while it does -- the measured cost of getting
# this path wrong is 1.81 ms p99 on the 500 Hz input loop. A token lookup is
# cheap, but it is not free, and nothing here changes between frames.
_BACKDROP = qcolor("video-backdrop")
_MESSAGE_INK = qcolor("text-secondary")
_OSD_PANEL = qcolor("scrim")
_OSD_INK = qcolor("text-primary")


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

    #: Emitted by the decoder thread when a frame is published.
    #:
    #: Emitting a signal across threads is safe and is the documented way to
    #: reach the GUI thread; Qt sees the emitter is not this object's thread
    #: and queues the delivery. That is the same guarantee the old timer gave,
    #: without the wait.
    frame_ready = Signal()

    #: Asks the owner to toggle fullscreen. Only used while embedded, where
    #: this widget cannot sensibly do it itself.
    fullscreen_requested = Signal()

    def __init__(self, decoder, receiver, parent=None) -> None:
        # **No explicit `Window` flag.** A parentless QWidget is already a
        # top-level window, so leaving this to Qt lets the same class be the
        # standalone window it always was *and* the surface embedded in the
        # main window's video stage -- decided by whether a parent is given,
        # which is Qt's own convention. Forcing the flag made it a window even
        # when parented, which is why the picture could not live in the app.
        super().__init__(parent)
        self._decoder = decoder
        self._receiver = receiver

        if self.isWindow():
            self.setWindowTitle("Remote video")
            self.resize(960, 540)
        self.setMinimumSize(320, 180)
        self.setAutoFillBackground(False)
        # Repaints are wholesale; letting Qt clear first only causes flicker.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        if self.isWindow() and parent is not None and not parent.windowIcon().isNull():
            self.setWindowIcon(parent.windowIcon())

        self._image: QImage | None = None
        #: Keeps the bytes the current QImage wraps alive. QImage does not copy
        #: when constructed over a buffer, so dropping this would paint freed
        #: memory.
        #: The decoded frame whose pixels the current QImage is wrapping.
        #: QImage does not copy, so this reference is what keeps those bytes
        #: alive -- dropping it paints freed memory.
        self._frame_owner: object | None = None
        self._last_version = -1
        self._show_osd = True
        self._controller_rtt_ms = 0.0
        self._last_present_ns = 0

        #: ``(capture_ts, decoded_ns)`` for a frame taken up but not yet
        #: painted, or None. This is what separates a repaint caused by a new
        #: frame from one caused by a resize, an expose or the OSD toggling --
        #: only the first is a latency sample, and counting the others would
        #: report paints of a picture that had not changed.
        self._pending_present: tuple[int, int] | None = None

        #: Last viewport handed to the decoder, so it is only told when it
        #: changes. See :meth:`_sync_viewport`.
        self._viewport: tuple[int, int] | None = None

        #: Kept here rather than on the receiver because they are measured at
        #: the moment of painting -- the only place that knows when the picture
        #: was actually drawn.
        self.present_interval = LatencyStats()
        #: The paint itself: the QImage scale plus the OSD.
        self.paint_stats = LatencyStats()
        #: Decoder publish -> paint begins. The delivery cost of the signal
        #: path, and the number that shows what the old 5 ms poll was costing.
        self.pickup_stats = LatencyStats()

        # Queued by Qt, because the decoder emits it from its own thread.
        self.frame_ready.connect(self._on_frame_ready)
        self._decoder.set_frame_listener(self.frame_ready.emit)

        self._sync_viewport()

        self._timer = QTimer(self)
        self._timer.setInterval(_SAFETY_TICK_MS)
        self._timer.timeout.connect(self._on_frame_ready)
        self._timer.start()

        fullscreen = QShortcut(QKeySequence(Qt.Key.Key_F11), self)
        fullscreen.activated.connect(self.toggle_fullscreen)
        overlay = QShortcut(QKeySequence(Qt.Key.Key_L), self)
        overlay.activated.connect(self.toggle_osd)

    # -- presentation ------------------------------------------------------

    def _on_frame_ready(self) -> None:
        """Take up the newest decoded frame and ask for a repaint.

        Reached two ways -- the decoder's signal and the safety timer -- so it
        has to be idempotent. The version check makes it so: a second call for
        a frame already taken up does nothing, which is also what stops an idle
        or stalled stream repainting an identical picture.

        **No latency is recorded here.** That was the defect: this point is
        before the paint, and stamping it here produced a figure that stopped
        short of everything expensive. See :meth:`_note_paint`.
        """
        self._sync_viewport()

        version = self._decoder.version
        if version == self._last_version:
            return
        self._last_version = version

        frame = self._decoder.latest()
        if frame is None:
            return

        # QImage wraps the buffer without copying, so the pixels must outlive
        # the image. Holding the frame that owns them does that: each decoded
        # frame has its own buffer and is never written into once published.
        #
        # The old assignment held a `bytes` the decoder had copied for us. That
        # copy was 6.22 MB under the GIL -- see client/media/decoder.py.
        self._frame_owner = frame.owner
        self._image = QImage(
            frame.pixels,
            frame.width,
            frame.height,
            frame.stride,
            QImage.Format.Format_RGB888,
        )
        # Physical pixels, so a high-DPI display gets a 1:1 blit too: Qt
        # divides an image's size by its device pixel ratio when it maps it to
        # the logical rect below. Without this the picture would be scaled by
        # the ratio at paint time, which is exactly the cost being avoided.
        self._image.setDevicePixelRatio(self._device_ratio())
        self._pending_present = (frame.capture_ts, frame.decoded_ns)
        self.update()

    def _device_ratio(self) -> float:
        try:
            return float(self.devicePixelRatioF())
        except Exception:      # noqa: BLE001 - a ratio must never stop the paint
            return 1.0

    def _sync_viewport(self) -> None:
        """Keep the decoder scaling to what this window will actually draw.

        Recomputed per frame rather than from ``resizeEvent`` alone: it is two
        multiplications and a comparison, and it picks up everything that can
        change the target -- a resize, fullscreen, and a move to a monitor with
        a different device pixel ratio, which raises no resize event of its own.
        """
        ratio = self._device_ratio()
        viewport = (
            max(int(self.width() * ratio), 0),
            max(int(self.height() * ratio), 0),
        )
        if viewport == self._viewport:
            return
        self._viewport = viewport
        self._decoder.set_viewport(*viewport)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        started = now_ns()
        painter = QPainter(self)
        painter.fillRect(self.rect(), _BACKDROP)

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
        self._note_paint(started)

    def _note_paint(self, started_ns: int) -> None:
        """Record what that paint cost, and how old the picture was when drawn.

        Called after ``painter.end()``, which is as late as this process can
        see. The backing store still has to reach the compositor and the
        compositor still has to reach the panel, and neither is measurable from
        here -- so the overlay names them as excluded rather than letting the
        figure be read as button-to-photon.
        """
        finished = now_ns()
        self.paint_stats.add((finished - started_ns) / 1_000_000)

        pending, self._pending_present = self._pending_present, None
        if pending is None:
            # A resize, an expose, or the OSD being toggled. Real work, but not
            # a new picture, so not a latency sample.
            return

        capture_ts, decoded_ns = pending
        if decoded_ns:
            self.pickup_stats.add((started_ns - decoded_ns) / 1_000_000)

        # Gate on the clock being locked, never on the offset being non-zero:
        # zero is a perfectly ordinary offset (same machine, or two clocks that
        # happen to agree), and treating it as "not ready" hides the latency
        # display exactly where it is easiest to verify.
        if self._receiver.clock_locked:
            local_capture = capture_ts + self._receiver.clock_offset_ns
            latency_ms = (finished - local_capture) / 1_000_000
            # A negative figure means the clocks have not settled; recording it
            # would drag the median somewhere impossible.
            if latency_ms > 0:
                self._receiver.present_stats.add(latency_ms)

        if self._last_present_ns:
            self.present_interval.add((finished - self._last_present_ns) / 1_000_000)
        self._last_present_ns = finished

        # Published where the receiver report can pick them up without this
        # window having to know anything about the wire format.
        self._receiver.paint_stats = self.paint_stats
        self._receiver.pickup_stats = self.pickup_stats

    def _draw_message(self, painter: QPainter) -> None:
        state = self._receiver.state.name.replace("_", " ").title()
        detail = self._receiver.state_detail
        text = f"{state}\n{detail}" if detail else state

        painter.setPen(_MESSAGE_INK)
        font = QFont()
        font.setPointSize(13)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)

    def _draw_osd(self, painter: QPainter) -> None:
        lines = self.osd_lines()
        if not lines:
            return

        # A family *stack*: "Consolas" alone resolves to nothing on the Linux
        # build, and Qt then falls back to a proportional face -- which makes
        # the OSD's aligned columns jitter as the digits change.
        font = QFont()
        font.setFamilies(list(Type.FAMILIES_MONO))
        font.setPointSize(10)
        painter.setFont(font)
        metrics = painter.fontMetrics()

        width = max(metrics.horizontalAdvance(line) for line in lines) + _OSD_MARGIN * 2
        height = metrics.height() * len(lines) + _OSD_MARGIN * 2

        painter.fillRect(_OSD_MARGIN, _OSD_MARGIN, width, height, _OSD_PANEL)
        painter.setPen(_OSD_INK)
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
            video_text = (
                f"video      p50 {video_p50:6.1f} ms   p99 {video_p99:6.1f} ms"
                "   capture->painted"
            )

        lines = [video_text]

        # What the picture cost once it reached this process, split so the two
        # halves can be told apart: `paint` is work, `wait` is delay. `wait`
        # existing at all is why the polling timer was replaced -- it used to
        # sit at half the tick interval and there was no way to see it.
        if self.paint_stats.count:
            lines.append(
                f"paint      p50 {self.paint_stats.p50:6.1f} ms   "
                f"wait {self.pickup_stats.p50:6.1f} ms"
            )

        if self._controller_rtt_ms:
            lines.append(f"controller rtt {self._controller_rtt_ms:6.1f} ms")
            if self._receiver.clock_locked and video_p50:
                # Half the controller round trip is the outbound leg; the video
                # figure is already one-way.
                #
                # Three real terms are missing and all three are invisible from
                # here, so they are named rather than quietly omitted: the
                # console's own processing, the capture card's pipeline, and
                # the compositor between `painter.end()` and the panel. A
                # figure that reads as button-to-photon and is not is worse
                # than one that says what it leaves out.
                combined = self._controller_rtt_ms / 2 + video_p50
                lines.append(
                    f"combined   {combined:10.1f} ms  "
                    "(excl. console, capture card, compositor)"
                )

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
        """Go fullscreen, or ask to be taken fullscreen.

        Embedded, this widget is a child in a layout and `showFullScreen`
        would rip it out of its parent -- so it asks instead, and the window
        that owns the layout decides what fullscreen means for the whole
        shell. Standalone it still does it itself, which is what the window
        mode has always done.
        """
        if not self.isWindow():
            self.fullscreen_requested.emit()
            return
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

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # _on_frame_ready also syncs, but that only runs when frames arrive;
        # a window resized while the stream is stalled would otherwise keep
        # asking for the old size until it recovered.
        self._sync_viewport()
        super().resizeEvent(event)

    def release(self) -> None:
        """Stop driving this surface and let go of the decoder.

        Split out of `closeEvent` because embedded there is no close: the
        surface is taken out of a layout instead, and nothing would otherwise
        stop the timer or detach the listener -- leaving the decoder holding a
        callback into a widget nobody is showing, still scaling every frame to
        a viewport that is no longer visible.
        """
        self._timer.stop()
        # The decoder outlives this surface -- the stream keeps running while
        # nobody is watching -- so it must not be left holding a way to call
        # into a widget that has gone.
        try:
            self._decoder.set_frame_listener(None)
            # ...and stop constraining its output size. A stream nobody is
            # watching should not still be scaling to a window that has gone.
            self._decoder.set_viewport(0, 0)
        except Exception:
            log.debug("Could not detach the frame listener", exc_info=True)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.release()
        self.closed.emit()
        super().closeEvent(event)

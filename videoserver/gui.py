"""The video server's window.

Follows the client GUI's conventions exactly, because they were arrived at the
hard way: the pipeline runs on its own threads and never calls into Qt, the
window polls its state on a timer, and no control is written to while the
operator is using it.

The preview is deliberately *better* here than the one sent to the web GUI.
That one stays small because it crosses the network; this one does not, and a
320-pixel picture refreshing four times a second reads as "the stream is low
quality" when the stream is nothing of the sort. It runs on its own timer, so
the numbers can keep updating slowly without making the picture stutter.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from common.video import VideoSettings
from videoserver import config as video_config
from videoserver.config import VideoServerConfig
from videoserver.assets import app_icon
from videoserver.levelmeter import LevelMeter

log = logging.getLogger(__name__)

#: Poll cadence. The pipeline's numbers change slowly; four times a second is
#: plenty and leaves the encoder alone.
UI_INTERVAL_MS = 250

#: Local preview size and rate. Independent of the preview sent to the web
#: GUI, which stays small because it crosses the network.
PREVIEW_WIDTH_LOCAL = 640
PREVIEW_INTERVAL_MS = 66          # ~15 fps, smooth enough to judge by

_RESOLUTIONS = [
    ("640 × 480", 640, 480),
    ("1280 × 720", 1280, 720),
    ("1920 × 1080", 1920, 1080),
]

_CLIENT_COLUMNS = ("Viewer", "Address", "Frames", "Loss", "Latency")


class VideoServerWindow(QMainWindow):
    def __init__(self, config: VideoServerConfig) -> None:
        super().__init__()
        self._config = config
        self._app = None
        self._control = None
        self._preview = None
        self._beacon = None
        self._loading = True

        self.setWindowTitle("Remote Game Video Server")
        self.setWindowIcon(app_icon())
        self.resize(880, 700)

        self._build_ui()
        self._load_config_into_ui()
        self._loading = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(UI_INTERVAL_MS)

        # Separate from the status tick: numbers change slowly and a picture
        # does not, and driving both at 4 Hz made the preview look like the
        # stream was stuttering.
        self._preview_timer = QTimer(self)
        self._preview_timer.timeout.connect(self._tick_preview)
        self._preview_timer.start(PREVIEW_INTERVAL_MS)

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        layout.addWidget(self._build_connection_group())
        layout.addWidget(self._build_capture_group())
        layout.addWidget(self._build_status_group(), 1)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self._set_status("Not streaming")

    def _build_connection_group(self) -> QGroupBox:
        group = QGroupBox("Connection")
        outer = QVBoxLayout(group)
        form = QFormLayout()

        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setPlaceholderText("Enter this in the Bluetooth server's web GUI")
        self._password.setToolTip(
            "This machine's own password. The Bluetooth server uses it to take "
            "charge of this video server.\n\n"
            "Deliberately not the players' password: players never learn this "
            "one, so a player the operator denied cannot pose as the server."
        )
        self._save_password = QCheckBox("Remember")
        password_row = QHBoxLayout()
        password_row.addWidget(self._password, 1)
        password_row.addWidget(self._save_password)
        form.addRow("This server's password:", _wrap(password_row))

        self._discoverable = QCheckBox("Announce this machine on the LAN")
        self._discoverable.setToolTip(
            "Lets the Bluetooth server's operator find this machine instead of "
            "typing its address. The announcement carries no password."
        )
        form.addRow("", self._discoverable)

        self._media_port = QSpinBox()
        self._media_port.setRange(0, 65535)
        self._media_port.setToolTip("0 lets the operating system choose one.")
        form.addRow("Media port:", self._media_port)

        self._name = QLineEdit()
        form.addRow("This PC:", self._name)

        buttons = QHBoxLayout()
        self._start_button = QPushButton("Start streaming")
        self._start_button.setDefault(True)
        self._start_button.clicked.connect(self._on_start_clicked)
        self._state_label = QLabel("Not streaming")
        self._state_label.setStyleSheet("color: #888;")
        buttons.addWidget(self._start_button)
        buttons.addWidget(self._state_label, 1)

        outer.addLayout(form)
        outer.addLayout(buttons)
        return group

    def _build_capture_group(self) -> QGroupBox:
        group = QGroupBox("Capture")
        form = QFormLayout(group)

        device_row = QHBoxLayout()
        self._device = QComboBox()
        self._device.setMinimumWidth(240)
        self._rescan = QPushButton("Rescan")
        self._rescan.clicked.connect(self._on_rescan)
        device_row.addWidget(self._device, 1)
        device_row.addWidget(self._rescan)
        form.addRow("Video device:", _wrap(device_row))

        self._audio_device = QComboBox()
        form.addRow("Audio device:", self._audio_device)

        self._resolution = QComboBox()
        for label, width, height in _RESOLUTIONS:
            self._resolution.addItem(label, (width, height))
        form.addRow("Resolution:", self._resolution)

        self._fps = QComboBox()
        for rate in (30, 60):
            self._fps.addItem(f"{rate} fps", rate)
        form.addRow("Frame rate:", self._fps)

        self._bitrate = QSpinBox()
        self._bitrate.setRange(500, 50000)
        self._bitrate.setSingleStep(500)
        self._bitrate.setSuffix(" kbps")
        form.addRow("Bitrate:", self._bitrate)

        self._encoder = QComboBox()
        self._encoder.addItem("Automatic", "auto")
        form.addRow("Encoder:", self._encoder)

        self._audio_enabled = QCheckBox("Stream audio")
        form.addRow("", self._audio_enabled)

        # Beside the switch, because the two are read together: the checkbox
        # says audio is meant to be streaming, the meter says whether any is.
        self._audio_meter = LevelMeter()
        form.addRow("Audio level:", self._audio_meter)

        self._test_source = QCheckBox("Test pattern (no capture card needed)")
        form.addRow("", self._test_source)

        self._apply = QPushButton("Apply")
        self._apply.clicked.connect(self._on_apply)
        form.addRow("", self._apply)

        return group

    def _build_status_group(self) -> QGroupBox:
        group = QGroupBox("Status")
        layout = QVBoxLayout(group)

        self._summary = QLabel("Not streaming")
        self._summary.setStyleSheet("font-family: Consolas, monospace;")
        layout.addWidget(self._summary)

        body = QHBoxLayout()

        self._preview_label = QLabel("No preview")
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setMinimumSize(320, 180)
        self._preview_label.setStyleSheet(
            "background: #000; color: #888; border: 1px solid #333;"
        )
        body.addWidget(self._preview_label, 1)

        self._clients = QTableWidget(0, len(_CLIENT_COLUMNS))
        self._clients.setHorizontalHeaderLabels(_CLIENT_COLUMNS)
        self._clients.verticalHeader().setVisible(False)
        self._clients.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._clients.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        body.addWidget(self._clients, 1)

        layout.addLayout(body, 1)
        return group

    # -- config <-> ui -----------------------------------------------------

    def _load_config_into_ui(self) -> None:
        cfg = self._config
        settings = cfg.settings

        self._password.setText(cfg.password)
        self._discoverable.setChecked(cfg.discoverable)
        self._save_password.setChecked(cfg.save_password)
        self._media_port.setValue(cfg.media_port)
        self._name.setText(cfg.name)

        self._select_data(self._resolution, (settings.width, settings.height))
        self._select_data(self._fps, settings.fps)
        self._bitrate.setValue(settings.bitrate_kbps)
        self._audio_enabled.setChecked(settings.audio_enabled)
        self._test_source.setChecked(settings.test_source)

        self._populate_encoders()
        self._refresh_devices()

    def _save_ui_into_config(self) -> None:
        cfg = self._config
        cfg.password = self._password.text()
        cfg.save_password = self._save_password.isChecked()
        cfg.discoverable = self._discoverable.isChecked()
        cfg.media_port = self._media_port.value()
        cfg.name = self._name.text().strip() or cfg.name
        cfg.settings = self._settings_from_ui()

        video_config.save(cfg)

    def _settings_from_ui(self) -> VideoSettings:
        width, height = self._resolution.currentData() or (1280, 720)
        values = self._config.settings.to_dict()
        values.update(
            {
                "width": width,
                "height": height,
                "fps": self._fps.currentData() or 60,
                "bitrate_kbps": self._bitrate.value(),
                "encoder": self._encoder.currentData() or "auto",
                "device": self._device.currentData() or "",
                "audio_device": self._audio_device.currentData() or "",
                "audio_enabled": self._audio_enabled.isChecked(),
                "test_source": self._test_source.isChecked(),
            }
        )
        return VideoSettings(**values).clamped()

    def _populate_encoders(self) -> None:
        """List only the encoders this machine can actually run.

        Not the build list: FFmpeg ships NVENC, QSV and AMF support whatever
        silicon is present, so offering those would let the operator pick one
        that cannot open. Nothing breaks -- the chain falls back -- but the
        status panel then reports an encoder they did not choose, which reads
        as the setting being ignored.
        """
        from videoserver.encode import usable_encoders

        self._encoder.clear()
        self._encoder.addItem("Automatic", "auto")
        for name in usable_encoders():
            self._encoder.addItem(name, name)
        self._select_data(self._encoder, self._config.settings.encoder)

    def _refresh_devices(self) -> None:
        from videoserver.capture import enumerate_devices

        devices = enumerate_devices(self._config.settings.backend)
        self._fill_devices(self._device, devices, "video", self._config.settings.device)
        self._fill_devices(
            self._audio_device, devices, "audio", self._config.settings.audio_device
        )

    @staticmethod
    def _fill_devices(combo: QComboBox, devices, kind: str, current: str) -> None:
        combo.clear()
        combo.addItem("First available", "")
        for entry in devices:
            if entry.get("kind") == kind:
                combo.addItem(entry["name"], entry["id"])
        VideoServerWindow._select_data(combo, current)

    @staticmethod
    def _select_data(combo: QComboBox, value) -> None:
        """Select the entry whose data equals ``value``.

        Compares in Python rather than using findData: Qt wraps item data in a
        QVariant, and a tuple like ``(1280, 720)`` does not compare equal
        through it -- the resolution box silently stayed on its first entry.
        """
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return

    # -- actions -----------------------------------------------------------

    def _on_start_clicked(self) -> None:
        if self._app is not None:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        self._save_ui_into_config()
        problems = self._config.validate()
        if problems:
            QMessageBox.warning(
                self, "Cannot start", "\n".join(f"• {p}" for p in problems)
            )
            return
        if not self._config.password:
            QMessageBox.warning(
                self, "Cannot start", "Set the password your players use."
            )
            return

        try:
            from videoserver.control import ControlResponder
            from videoserver.pipeline import VideoServerApp
            from videoserver.preview import PreviewEncoder
        except ImportError as exc:
            QMessageBox.critical(
                self,
                "Video unavailable",
                f"The media extras are not installed ({exc}).\n\n"
                "Install them with:  pip install -e '.[client,video]'",
            )
            return

        self._app = VideoServerApp(self._config)
        self._control = ControlResponder(self._app)
        self._app.responder = self._control
        self._app.start()
        self._control.start()
        # Bigger and smoother than the one sent to the web GUI: this one is
        # local, so there is no bandwidth to save, and a 320px 4 fps window
        # reads as "the stream is low quality" when it is nothing of the sort.
        self._preview = PreviewEncoder(width=PREVIEW_WIDTH_LOCAL)
        self._beacon = _start_beacon(self._app, self._config)

        self._start_button.setText("Stop streaming")
        self._set_status("Waiting for a Bluetooth server")

    def _stop(self) -> None:
        if self._beacon is not None:
            self._beacon()
            self._beacon = None
        if self._control is not None:
            self._control.stop()
            self._control = None
        if self._app is not None:
            self._app.stop()
            self._app = None
        self._preview = None

        self._start_button.setText("Start streaming")
        self._set_status("Not streaming")
        self._summary.setText("Not streaming")
        self._clients.setRowCount(0)
        self._preview_label.setPixmap(QPixmap())
        self._preview_label.setText("No preview")

    def _on_apply(self) -> None:
        self._save_ui_into_config()
        if self._app is not None:
            self._app.apply_config(self._config.settings)
            self._set_status("Settings applied")

    def _on_rescan(self) -> None:
        self._refresh_devices()
        self._set_status("Rescanned capture devices")

    # -- polling -----------------------------------------------------------

    def _tick(self) -> None:
        app = self._app
        if app is None:
            return

        status = app.status()
        self._summary.setText(
            f"{status['encoder'] or 'starting'}   "
            f"{status['width']}×{status['height']} @ {status['fps']:.0f} fps   "
            f"{status['bitrate_kbps']} kbps   "
            f"encode p50 {status['encode_p50_ms']:.1f} ms / p99 {status['encode_p99_ms']:.1f} ms"
        )

        if status["errors"]:
            self._set_status(status["errors"][-1])
        elif status["streaming"]:
            watchers = status["clients"]
            self._set_status(
                f"Streaming — {watchers} watching"
                + (f" — {self._control_state()}" if self._control else "")
            )
        elif self._control is not None and not self._control.connected:
            self._set_status("Waiting for a Bluetooth server to connect")

        self._update_audio_meter(status)

        # viewer_snapshot, not client_snapshot: the Bluetooth server holds a
        # session here too, and listing it as a viewer with 0 frames forever
        # reads as a broken viewer rather than as the controller it is.
        self._update_clients(app.net.viewer_snapshot())

    def _update_audio_meter(self, status: dict) -> None:
        """Show the level, or say plainly that there is nothing to show.

        Three states, and telling them apart is the whole point: audio turned
        off, audio on but nothing arriving, and audio arriving at some level.
        The middle one is the fault worth catching -- everything reports
        healthy and the stream is silent.
        """
        if not self._app.settings.audio_enabled:
            self._audio_meter.clear()
            self._audio_meter.setToolTip("Audio streaming is switched off.")
            return

        self._audio_meter.set_level(
            float(status.get("audio_rms", 0.0) or 0.0),
            float(status.get("audio_level", 0.0) or 0.0),
            live=bool(status.get("audio_live")),
        )
        self._audio_meter.setToolTip(
            "Audio reaching the encoder. Silence here while capture is running "
            "means the device is muted or on the wrong input."
        )

    def _tick_preview(self) -> None:
        app = self._app
        if app is not None:
            self._update_preview(app)

    def _control_state(self) -> str:
        """Whether a Bluetooth server has taken charge of us.

        Worth surfacing plainly: an unclaimed video server looks identical to a
        working one from here -- it captures, encodes, and shows a preview --
        but no player will ever be sent to it.
        """
        if self._control is None:
            return ""
        return "controlled" if self._control.connected else "waiting for a server"

    def _update_clients(self, entries) -> None:
        self._clients.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            report = entry.get("report") or {}
            received = report.get("slices_received", 0) or 0
            lost = report.get("slices_lost", 0) or 0
            total = received + lost
            loss = f"{(lost / total * 100):.1f}%" if total else "—"
            latency = report.get("vlat_p50_ms")

            values = (
                entry.get("name") or entry["client_id"][:8],
                entry["address"],
                str(entry.get("frames_sent", 0)),
                loss,
                f"{latency:.0f} ms" if latency else "—",
            )
            for column, value in enumerate(values):
                self._clients.setItem(row, column, QTableWidgetItem(value))

    def _update_preview(self, app) -> None:
        if self._preview is None:
            return
        # Through the app, never straight at the frame: the responder encodes
        # its own preview from the same object, and reformatting it from both
        # threads at once wedges one of them -- here, the GUI thread.
        jpeg, _captured = app.encode_preview(self._preview)
        if not jpeg:
            return

        image = QImage.fromData(jpeg, "JPEG")
        if image.isNull():
            return
        self._preview_label.setText("")
        self._preview_label.setPixmap(
            QPixmap.fromImage(image).scaled(
                self._preview_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _set_status(self, text: str) -> None:
        self.statusBar().showMessage(text)
        self._state_label.setText(text)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._save_ui_into_config()
        self._stop()
        super().closeEvent(event)


def _start_beacon(app, cfg):
    """Announce on the LAN while streaming. Returns a shutdown callable, or None."""
    if not cfg.discoverable:
        return None
    try:
        from videoserver.main import _start_beacon as start

        return start(app, cfg)
    except Exception:
        log.debug("Could not start the discovery beacon", exc_info=True)
        return None


def _wrap(layout) -> QWidget:
    holder = QWidget()
    holder.setLayout(layout)
    layout.setContentsMargins(0, 0, 0, 0)
    return holder


def _set_windows_app_id() -> None:
    """Give Windows an explicit AppUserModelID.

    Without one, Windows groups the taskbar button under the host interpreter
    and shows *its* icon, so a packaged app appears as generic Python. A
    distinct id from the client's, or the two would share a taskbar button and
    one icon despite being separate applications. No-op everywhere else.
    """
    import sys as _sys

    if _sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "rbgc.videoserver.remote-bluetooth-game-control"
        )
    except Exception:
        log.debug("Could not set the Windows app id", exc_info=True)


def run(config: VideoServerConfig, args) -> int:
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    # Application-wide as well as per-window: Windows takes the taskbar icon
    # from the application and the title bar from the window.
    app.setWindowIcon(app_icon())
    _set_windows_app_id()

    window = VideoServerWindow(config)
    window.show()

    # Auto-start when the command line already said what to do, so
    # `rbgc-video --server ... --test-source` needs no clicking.
    # Auto-start when the command line already said what to capture, so
    # `rbgc-video --test-source` needs no clicking.
    if getattr(args, "test_source", False) or getattr(args, "device", None):
        if config.password:
            window._start()

    return app.exec()

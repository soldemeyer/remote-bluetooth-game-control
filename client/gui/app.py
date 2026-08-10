"""Client GUI (PySide6).

Layout mirrors the order a player actually works through:

    Connection  ->  Controllers  ->  Latency

Threading rule: Qt objects are touched only on the GUI thread. The input loop
and transport run on their own thread and never call into Qt directly; the GUI
polls their state on a timer instead. That is deliberate -- marshalling every
packet into the Qt event loop would put GUI work on the latency path.
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
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
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from client import config as client_config
from client.gui.latency_plot import LatencyPlot
from client.input import InputBackendError, create_backend
from client.loop import InputLoop, SlotRuntime
from client.net.transport import ClientTransport, ConnectionState, TransportError
from common.protocol import ControlOp

log = logging.getLogger(__name__)

#: GUI refresh rate. Fast enough to feel live, slow enough to stay cheap.
UI_INTERVAL_MS = 100

MAX_CONTROLLERS = client_config.MAX_CONTROLLERS


class MainWindow(QMainWindow):
    def __init__(self, config: client_config.ClientConfig) -> None:
        super().__init__()
        self._config = config

        self._backend = None
        self._transport: ClientTransport | None = None
        self._loop: InputLoop | None = None
        self._devices: list = []

        self.setWindowTitle("Remote Bluetooth Game Control")
        self.resize(980, 720)

        self._build_ui()
        self._refresh_devices()
        self._load_config_into_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(UI_INTERVAL_MS)

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        layout.addWidget(self._build_connection_group())

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._build_controller_group())
        splitter.addWidget(self._build_latency_group())
        splitter.setSizes([340, 300])
        layout.addWidget(splitter, 1)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self._set_status("Not connected")

    def _build_connection_group(self) -> QGroupBox:
        group = QGroupBox("Connection")
        outer = QVBoxLayout(group)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._mode = QComboBox()
        self._mode.addItem("Auto (try direct, then NAT traversal)", "auto")
        self._mode.addItem("Direct / LAN / VPN", "direct")
        self._mode.addItem("Internet (NAT hole-punching)", "punch")
        self._mode.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Mode:", self._mode)

        host_row = QHBoxLayout()
        self._host = QLineEdit()
        self._host.setPlaceholderText("Server address, e.g. 192.168.1.50")
        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(client_config.DEFAULT_PORT)
        self._discover = QPushButton("Find on LAN")
        self._discover.clicked.connect(self._on_discover)
        host_row.addWidget(self._host, 1)
        host_row.addWidget(QLabel("Port:"))
        host_row.addWidget(self._port)
        host_row.addWidget(self._discover)
        self._host_row = _wrap(host_row)
        form.addRow("Server:", self._host_row)

        punch_row = QHBoxLayout()
        self._room = QLineEdit()
        self._room.setPlaceholderText("Room code (must match the server)")
        self._broker = QLineEdit()
        self._broker.setPlaceholderText("Broker address")
        punch_row.addWidget(self._room, 1)
        punch_row.addWidget(QLabel("Broker:"))
        punch_row.addWidget(self._broker, 1)
        self._punch_row = _wrap(punch_row)
        form.addRow("Rendezvous:", self._punch_row)

        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setPlaceholderText("Server password")
        self._save_password = QCheckBox("Remember")
        password_row = QHBoxLayout()
        password_row.addWidget(self._password, 1)
        password_row.addWidget(self._save_password)
        form.addRow("Password:", _wrap(password_row))

        self._client_name = QLineEdit()
        form.addRow("This PC:", self._client_name)

        outer.addLayout(form)

        buttons = QHBoxLayout()
        self._connect_button = QPushButton("Connect")
        self._connect_button.clicked.connect(self._on_connect_clicked)
        self._connect_button.setDefault(True)

        self._state_label = QLabel("Not connected")
        self._state_label.setStyleSheet("color: #888;")

        buttons.addWidget(self._connect_button)
        buttons.addWidget(self._state_label, 1)
        outer.addLayout(buttons)

        return group

    def _build_controller_group(self) -> QGroupBox:
        group = QGroupBox("Controllers")
        layout = QVBoxLayout(group)

        hint = QLabel(
            "Enable a controller, give it a player name, and pick which gamepad "
            "it uses. Slots beyond the server's capacity are disabled."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888;")
        layout.addWidget(hint)

        self._table = QTableWidget(MAX_CONTROLLERS, 5)
        self._table.setHorizontalHeaderLabels(
            ["Use", "Slot", "Player name", "Gamepad", "Status"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)

        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

        self._enable_boxes: list[QCheckBox] = []
        self._username_edits: list[QLineEdit] = []
        self._device_combos: list[QComboBox] = []

        for row in range(MAX_CONTROLLERS):
            enable = QCheckBox()
            enable.stateChanged.connect(self._on_slot_toggled)
            self._table.setCellWidget(row, 0, _center(enable))
            self._enable_boxes.append(enable)

            self._table.setItem(row, 1, QTableWidgetItem(str(row)))

            username = QLineEdit()
            username.setPlaceholderText(f"Player {row + 1}")
            username.editingFinished.connect(self._on_username_changed)
            self._table.setCellWidget(row, 2, username)
            self._username_edits.append(username)

            combo = QComboBox()
            self._table.setCellWidget(row, 3, combo)
            self._device_combos.append(combo)

            self._table.setItem(row, 4, QTableWidgetItem("—"))

        layout.addWidget(self._table)

        actions = QHBoxLayout()
        refresh = QPushButton("Refresh gamepad list")
        refresh.clicked.connect(self._refresh_devices)
        actions.addWidget(refresh)
        actions.addStretch(1)
        self._capacity_label = QLabel("")
        self._capacity_label.setStyleSheet("color: #888;")
        actions.addWidget(self._capacity_label)
        layout.addLayout(actions)

        return group

    def _build_latency_group(self) -> QGroupBox:
        group = QGroupBox("Latency")
        layout = QVBoxLayout(group)

        note = QLabel(
            "Round-trip time to the server. The Bluetooth hop to the console adds "
            "a further 5–15 ms that cannot be measured from here."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888;")
        layout.addWidget(note)

        self._latency_labels: list[QLabel] = []
        row = QHBoxLayout()
        for slot in range(MAX_CONTROLLERS):
            label = QLabel(f"Slot {slot}\n—")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setFont(QFont("", 10))
            label.setStyleSheet(
                "background: #22252e; border: 1px solid #333; "
                "border-radius: 6px; padding: 8px; color: #888;"
            )
            row.addWidget(label)
            self._latency_labels.append(label)
        layout.addLayout(row)

        self._plot = LatencyPlot()
        layout.addWidget(self._plot, 1)

        return group

    # -- config ------------------------------------------------------------

    def _load_config_into_ui(self) -> None:
        cfg = self._config

        index = self._mode.findData(cfg.mode)
        self._mode.setCurrentIndex(index if index >= 0 else 0)

        self._host.setText(cfg.host)
        self._port.setValue(cfg.port)
        self._room.setText(cfg.room_code)
        self._broker.setText(
            f"{cfg.broker_host}:{cfg.broker_port}" if cfg.broker_host else ""
        )
        self._password.setText(cfg.password)
        self._save_password.setChecked(cfg.save_password)
        self._client_name.setText(cfg.client_name)

        for row in range(MAX_CONTROLLERS):
            entry = cfg.controller(row)
            self._enable_boxes[row].setChecked(entry.enabled)
            self._username_edits[row].setText(entry.username)

        self._on_mode_changed()

    def _save_ui_into_config(self) -> None:
        cfg = self._config

        cfg.mode = self._mode.currentData()
        cfg.host = self._host.text().strip()
        cfg.port = self._port.value()
        cfg.room_code = self._room.text().strip()
        cfg.password = self._password.text()
        cfg.save_password = self._save_password.isChecked()
        cfg.client_name = self._client_name.text().strip() or cfg.client_name

        broker = self._broker.text().strip()
        if broker:
            host, _, port = broker.partition(":")
            cfg.broker_host = host
            if port.isdigit():
                cfg.broker_port = int(port)

        for row in range(MAX_CONTROLLERS):
            entry = cfg.controller(row)
            entry.enabled = self._enable_boxes[row].isChecked()
            entry.username = self._username_edits[row].text().strip()

            combo = self._device_combos[row]
            device = combo.currentData()
            if device is not None:
                entry.guid = device.guid
                entry.device_name = device.display_name()

        client_config.save(cfg)

    # -- devices -----------------------------------------------------------

    def _refresh_devices(self) -> None:
        if self._backend is None:
            try:
                self._backend = create_backend(self._config.input_backend)
                self._backend.open()
            except InputBackendError as exc:
                QMessageBox.warning(self, "No gamepad support", str(exc))
                return

        try:
            self._devices = self._backend.list_devices()
        except InputBackendError as exc:
            log.warning("Could not list devices: %s", exc)
            self._devices = []

        for row, combo in enumerate(self._device_combos):
            previous = combo.currentData()
            combo.clear()

            if not self._devices:
                combo.addItem("No gamepads detected", None)
                continue

            for device in self._devices:
                combo.addItem(device.display_name(), device)

            # Restore the prior selection, or default to a distinct device per
            # slot so four pads land in four slots without any clicking.
            restored = False
            if previous is not None:
                for index in range(combo.count()):
                    data = combo.itemData(index)
                    if data is not None and data.guid == previous.guid:
                        combo.setCurrentIndex(index)
                        restored = True
                        break
            if not restored and row < combo.count():
                combo.setCurrentIndex(row)

        self._update_slot_availability()

    # -- connection --------------------------------------------------------

    def _on_mode_changed(self) -> None:
        mode = self._mode.currentData()
        self._host_row.setVisible(mode in ("auto", "direct"))
        self._punch_row.setVisible(mode in ("auto", "punch"))

    def _on_discover(self) -> None:
        import asyncio

        from server.discovery import discover_servers

        self._set_status("Searching the LAN...")
        QApplication.processEvents()

        try:
            servers = asyncio.run(discover_servers(timeout=1.5))
        except Exception as exc:
            log.warning("Discovery failed: %s", exc)
            servers = []

        if not servers:
            self._set_status("No servers found on this network")
            QMessageBox.information(
                self,
                "No servers found",
                "No RBGC servers replied on this network.\n\n"
                "Check that the server is running with discovery enabled, that "
                "you are on the same network, and that a firewall is not "
                "blocking UDP broadcast.",
            )
            return

        first = servers[0]
        self._host.setText(first["host"])
        self._port.setValue(first["port"])
        self._set_status(
            f"Found '{first['name']}' at {first['host']} "
            f"({first['capacity']} controller slots)"
        )

    def _on_connect_clicked(self) -> None:
        if self._transport is not None and self._transport.is_connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        self._save_ui_into_config()
        cfg = self._config

        problems = cfg.validate()
        if problems:
            QMessageBox.warning(self, "Cannot connect", "\n".join(f"• {p}" for p in problems))
            return

        if self._backend is None:
            self._refresh_devices()
            if self._backend is None:
                return

        self._connect_button.setEnabled(False)
        self._set_status(f"Connecting to {cfg.host}:{cfg.port}...")
        QApplication.processEvents()

        transport = ClientTransport(cfg.password, client_name=cfg.client_name)

        try:
            transport.connect(cfg.host, cfg.port)
        except TransportError as exc:
            self._connect_button.setEnabled(True)
            self._set_status("Connection failed")
            QMessageBox.critical(self, "Connection failed", str(exc))
            return

        self._transport = transport

        slots = self._build_slots(transport.server_capacity)
        if not slots:
            transport.close()
            self._transport = None
            self._connect_button.setEnabled(True)
            self._set_status("No controllers enabled")
            QMessageBox.warning(
                self,
                "No controllers",
                "Enable at least one controller with a gamepad selected.",
            )
            return

        transport.queue_control(
            ControlOp.SET_CONTROLLERS,
            {
                "client_name": cfg.client_name,
                "controllers": [
                    {"slot": s.slot, "username": s.username, "device_name": s.device_name}
                    for s in slots
                ],
            },
        )

        self._loop = InputLoop(
            self._backend,
            transport,
            poll_hz=cfg.poll_hz,
            axis_deadband=cfg.axis_deadband,
        )
        self._loop.set_slots(slots)
        self._loop.start()

        self._plot.reset()
        self._connect_button.setText("Disconnect")
        self._connect_button.setEnabled(True)
        self._set_status(f"Connected — streaming {len(slots)} controller(s)")

    def _build_slots(self, capacity: int) -> list[SlotRuntime]:
        slots: list[SlotRuntime] = []

        for row in range(MAX_CONTROLLERS):
            if not self._enable_boxes[row].isChecked():
                continue
            if capacity and row >= capacity:
                continue

            device = self._device_combos[row].currentData()
            if device is None:
                continue

            try:
                acquired = self._backend.acquire(device.instance_id)
            except InputBackendError as exc:
                log.warning("Could not acquire %s: %s", device.display_name(), exc)
                continue

            slots.append(
                SlotRuntime(
                    slot=row,
                    instance_id=device.instance_id,
                    username=self._username_edits[row].text().strip() or f"Player {row + 1}",
                    device_name=acquired.display_name(),
                )
            )

        return slots

    def _disconnect(self) -> None:
        if self._loop is not None:
            self._loop.stop()
            self._loop = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None

        self._connect_button.setText("Connect")
        self._set_status("Disconnected")

        for label in self._latency_labels:
            label.setText("—")

    # -- slot state --------------------------------------------------------

    def _on_slot_toggled(self) -> None:
        self._update_slot_availability()

    def _on_username_changed(self) -> None:
        """Push a username edit to the server without needing a reconnect."""
        if self._transport is None or not self._transport.is_connected:
            return

        for row, edit in enumerate(self._username_edits):
            username = edit.text().strip()
            self._transport.queue_control(
                ControlOp.SET_USERNAME, {"slot": row, "username": username}
            )
            if self._loop is not None:
                self._loop.set_username(row, username)

    def _update_slot_availability(self) -> None:
        """Grey out slots the server has no adapter for.

        Capacity is pushed live, so enabling an adapter on the server re-enables
        the slot here without reconnecting.
        """
        capacity = self._transport.server_capacity if self._transport else 0

        for row in range(MAX_CONTROLLERS):
            available = capacity == 0 or row < capacity

            self._enable_boxes[row].setEnabled(available)
            self._username_edits[row].setEnabled(available)
            self._device_combos[row].setEnabled(available)

            item = self._table.item(row, 4)
            if not available:
                self._enable_boxes[row].setChecked(False)
                tip = (
                    f"The server has only {capacity} Bluetooth adapter"
                    f"{'' if capacity == 1 else 's'}, so this slot cannot be used."
                )
                self._enable_boxes[row].setToolTip(tip)
                if item:
                    item.setText("unavailable")
            else:
                self._enable_boxes[row].setToolTip("")
                if item and item.text() == "unavailable":
                    item.setText("—")

        if capacity:
            self._capacity_label.setText(f"Server capacity: {capacity} controller(s)")
        else:
            self._capacity_label.setText("")

    # -- periodic refresh --------------------------------------------------

    def _tick(self) -> None:
        transport = self._transport
        if transport is None:
            return

        if transport.state in (ConnectionState.DISCONNECTED, ConnectionState.FAILED):
            detail = transport.state_detail
            self._disconnect()
            self._set_status(f"Connection lost: {detail}")
            return

        self._update_slot_availability()

        latency = transport.latency_snapshot()
        loop_slots = {s.slot: s for s in self._loop.slots()} if self._loop else {}

        for row in range(MAX_CONTROLLERS):
            label = self._latency_labels[row]
            stats = latency.get(row)
            entry = loop_slots.get(row)

            if entry is None:
                label.setText(f"Slot {row}\n—")
                label.setStyleSheet(_latency_style(None))
                continue

            item = self._table.item(row, 4)
            if item:
                item.setText("streaming" if entry.was_connected else "disconnected")

            if not stats or not stats["rtt"]["count"]:
                label.setText(f"Slot {row}\nwaiting")
                label.setStyleSheet(_latency_style(None))
                continue

            rtt = stats["rtt"]
            label.setText(
                f"{entry.username or f'Slot {row}'}\n"
                f"{rtt['p50']:.1f} ms\n"
                f"p99 {rtt['p99']:.1f}"
            )
            label.setStyleSheet(_latency_style(rtt["p50"]))
            self._plot.add_sample(row, rtt["last"])

        self._plot.refresh()

    def _set_status(self, text: str) -> None:
        self.statusBar().showMessage(text)
        self._state_label.setText(text)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._save_ui_into_config()
        self._disconnect()
        if self._backend is not None:
            self._backend.close()
        super().closeEvent(event)


def _latency_style(p50: float | None) -> str:
    """Colour by what is actually achievable -- Bluetooth alone costs 5-15 ms."""
    base = "border-radius: 6px; padding: 8px; border: 1px solid #333;"
    if p50 is None:
        return f"background: #22252e; color: #888; {base}"
    if p50 < 25:
        return f"background: #14301f; color: #3ecf8e; {base}"
    if p50 < 60:
        return f"background: #322613; color: #f5a623; {base}"
    return f"background: #33191b; color: #ff5c5c; {base}"


def _wrap(layout) -> QWidget:
    widget = QWidget()
    widget.setLayout(layout)
    layout.setContentsMargins(0, 0, 0, 0)
    return widget


def _center(widget) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(widget)
    return container


def run(config: client_config.ClientConfig, args) -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Remote Bluetooth Game Control")
    app.setStyle("Fusion")

    window = MainWindow(config)
    window.show()

    return app.exec()

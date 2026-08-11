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
from client.gui.assets import app_icon
from client.gui.latency_plot import LatencyPlot
from client.gui.mapping_dialog import MappingDialog
from client.net.connect import connect as connect_to_server
from client.input import InputBackendError, create_backend
from client.input.mapping import DeviceMapping
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
        self._connect_result = None

        self.setWindowTitle("Remote Bluetooth Game Control")
        self.setWindowIcon(app_icon())
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

        configure = QPushButton("Configure controls…")
        configure.setToolTip(
            "Remap buttons, bind the keyboard, and watch a live preview of what "
            "is being sent.\n\nAlso how you make a gamepad work that Windows sees "
            "but SDL has no built-in layout for."
        )
        configure.clicked.connect(self._on_configure_controls)
        actions.addWidget(configure)

        self._rumble = QCheckBox("Rumble")
        self._rumble.setToolTip(
            "Play rumble sent back from the console.\n\n"
            "Turning this off tells the server to stop sending it, so no rumble "
            "data crosses the network at all -- it is not a local mute.\n\n"
            "The server has its own switch; both must be on."
        )
        self._rumble.stateChanged.connect(self._on_rumble_toggled)
        actions.addWidget(self._rumble)
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
        self._rumble.setChecked(cfg.rumble_enabled)
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
        cfg.rumble_enabled = self._rumble.isChecked()
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

    def _ensure_backend(self) -> bool:
        if self._backend is not None:
            return True
        try:
            # keyboard=True adds the keyboard as an extra virtual gamepad, so it
            # appears in the same list as real pads and can be assigned to a
            # slot like any of them.
            self._backend = create_backend(self._config.input_backend, keyboard=True)
            self._backend.open()
        except InputBackendError as exc:
            QMessageBox.warning(self, "No gamepad support", str(exc))
            return False

        self._apply_saved_mappings()
        return True

    def _apply_saved_mappings(self) -> None:
        """Push every stored binding into the backend.

        Done once on open and again whenever a mapping is edited, so an
        unmapped pad is usable from the moment it is selected rather than only
        after the mapping screen has been visited.
        """
        setter = getattr(self._backend, "set_mapping", None)
        if setter is None:
            return

        for guid, payload in (self._config.mappings or {}).items():
            try:
                setter(guid, DeviceMapping.from_dict(payload))
            except Exception:
                log.warning("Ignoring unreadable mapping for %s", guid, exc_info=True)

    def _refresh_devices(self) -> None:
        if not self._ensure_backend():
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
                note = device.status_note()
                combo.addItem(
                    f"{device.display_name()} — {note}" if note else device.display_name(),
                    device,
                )

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

    def _on_configure_controls(self) -> None:
        """Open the mapping screen for the currently selected device."""
        if not self._ensure_backend():
            return
        if not self._devices:
            self._refresh_devices()
        if not self._devices:
            QMessageBox.information(
                self,
                "No controllers",
                "No gamepad or keyboard is available to configure.\n\n"
                "Connect a controller and press 'Refresh gamepad list'.",
            )
            return

        device = self._selected_device()

        # The dialog polls the device directly, so it has to be open. When a
        # session is live the input loop already holds it and must keep it;
        # otherwise we opened it purely for the dialog and have to hand it back.
        borrowed = self._loop is None
        try:
            device = self._backend.acquire(device.instance_id)
        except InputBackendError as exc:
            QMessageBox.warning(self, "Controller unavailable", str(exc))
            return

        saved = (self._config.mappings or {}).get(device.guid)
        mapping = DeviceMapping.from_dict(saved) if saved else None

        dialog = MappingDialog(
            self._backend,
            device,
            mapping,
            self,
            preview_layout=self._config.preview_layout,
        )
        accepted = dialog.exec()

        # Remember the layout choice either way: it is a display preference,
        # not part of the binding being saved or discarded.
        self._config.preview_layout = dialog.preview_layout

        if accepted:
            self._config.mappings = dict(self._config.mappings or {})
            self._config.mappings[device.guid] = dialog.mapping.to_dict()
            self._apply_saved_mappings()
            self._save_ui_into_config()
            self._set_status(f"Saved controls for {device.display_name()}")
        else:
            # Cancel must undo anything the dialog pushed live while binding.
            self._apply_saved_mappings()

        if borrowed:
            self._backend.release(device.instance_id)

        self._refresh_devices()

    # -- keyboard-as-controller -------------------------------------------
    #
    # Qt delivers key events to the focused window, so these forward them into
    # the keyboard backend for as long as the client window has focus. There is
    # no global hook by design -- see client/input/keyboard_backend.py.

    def _feed_key(self, key: int, down: bool) -> None:
        if self._backend is None:
            return
        for backend in getattr(self._backend, "backends", [self._backend]):
            setter = getattr(backend, "set_key", None)
            if setter is not None:
                setter(key, down)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if not event.isAutoRepeat():
            self._feed_key(int(event.key()), True)
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if not event.isAutoRepeat():
            self._feed_key(int(event.key()), False)
        super().keyReleaseEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt override
        # Losing focus mid-keypress would otherwise latch that key down forever:
        # the release event goes to whichever window took focus, not to us.
        from PySide6.QtCore import QEvent

        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            if self._backend is not None:
                for backend in getattr(self._backend, "backends", [self._backend]):
                    clear = getattr(backend, "clear_keys", None)
                    if clear is not None:
                        clear()
        super().changeEvent(event)

    def _selected_device(self):
        """The device from the first enabled slot, else the first listed one."""
        for row, box in enumerate(self._enable_boxes):
            if box.isChecked():
                data = self._device_combos[row].currentData()
                if data is not None:
                    return data
        for combo in self._device_combos:
            data = combo.currentData()
            if data is not None:
                return data
        return self._devices[0]

    # -- connection --------------------------------------------------------

    def _on_mode_changed(self) -> None:
        mode = self._mode.currentData()
        self._host_row.setVisible(mode in ("auto", "direct"))
        self._punch_row.setVisible(mode in ("auto", "punch"))

    def _on_discover(self) -> None:
        """Browse for servers and let the operator choose one.

        Replaces an earlier version that silently connected to whichever server
        answered first -- fine with one server on the bench, wrong the moment
        there are two.
        """
        from client.gui.server_picker import ServerPicker

        broker_host, broker_port = self._broker_fields()

        picker = ServerPicker(
            self,
            broker_host=broker_host,
            broker_port=broker_port,
            password=self._password.text(),
        )
        if not picker.exec() or not picker.selection:
            return

        choice = picker.selection
        self._password.setText(choice.get("password", ""))

        if choice.get("kind") == "internet":
            # Listed over the broker: reach it by room code, not by address.
            self._mode.setCurrentIndex(max(0, self._mode.findData("punch")))
            self._room.setText(choice.get("room", ""))
            self._set_status(f"Selected '{choice.get('name')}' via the broker")
        else:
            self._mode.setCurrentIndex(max(0, self._mode.findData("direct")))
            self._host.setText(str(choice.get("host", "")))
            self._port.setValue(int(choice.get("port") or self._port.value()))
            self._set_status(
                f"Selected '{choice.get('name')}' at {choice.get('host')}"
            )

    def _broker_fields(self) -> tuple[str, int]:
        """Broker host and port from the connection form, or the config."""
        text = self._broker.text().strip() if hasattr(self, "_broker") else ""
        if not text:
            return self._config.broker_host, self._config.broker_port

        host, _, port_text = text.rpartition(":")
        if not host:
            return text, self._config.broker_port
        try:
            return host, int(port_text)
        except ValueError:
            return host, self._config.broker_port

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

        transport = ClientTransport(
            cfg.password,
            client_name=cfg.client_name,
            rumble_enabled=cfg.rumble_enabled,
        )

        try:
            # Goes through the shared ladder so the mode selector actually
            # applies -- direct, LAN discovery, then hole-punch.
            result = connect_to_server(transport, cfg)
        except TransportError as exc:
            self._connect_button.setEnabled(True)
            self._set_status("Connection failed")
            QMessageBox.critical(self, "Connection failed", str(exc))
            return

        self._transport = transport
        self._connect_result = result

        if result.is_relayed:
            QMessageBox.information(
                self,
                "Connected via relay",
                "NAT traversal failed, so traffic is being relayed through the "
                "rendezvous broker.\n\nThe connection works, but latency will be "
                "noticeably higher than a direct or hole-punched path.",
            )

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
        # Rumble arrives on the transport's receive path, which runs on the
        # input loop's thread, so it can call straight into the backend.
        transport._on_rumble = self._loop.play_rumble

        self._loop.set_slots(slots)
        self._loop.start()

        self._plot.reset()
        self._connect_button.setText("Disconnect")
        self._connect_button.setEnabled(True)
        mode = result.mode if result else "direct"
        self._set_status(
            f"Connected ({mode}) — streaming {len(slots)} controller(s)"
        )

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

    def _on_rumble_toggled(self) -> None:
        """Apply the rumble switch, live if we are connected.

        Telling the server matters: this is not a local mute. With the server
        informed, disabling here means the data is never transmitted.
        """
        enabled = self._rumble.isChecked()
        self._config.rumble_enabled = enabled
        if self._transport is not None and self._transport.is_connected:
            self._transport.set_rumble_enabled(enabled)

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


def _set_windows_app_id() -> None:
    """Give Windows an explicit AppUserModelID.

    Without one, Windows groups the taskbar button under the host interpreter
    and shows *its* icon -- so a packaged app appears as generic Python. Setting
    a distinct id makes the taskbar use our own icon and grouping. No-op
    everywhere else.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "rbgc.client.remote-bluetooth-game-control"
        )
    except Exception:
        log.debug("Could not set the Windows app id", exc_info=True)


def run(config: client_config.ClientConfig, args) -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Remote Bluetooth Game Control")
    app.setStyle("Fusion")
    # Set on the application as well as the window: Windows takes the taskbar
    # icon from the application, the title bar from the window.
    app.setWindowIcon(app_icon())

    _set_windows_app_id()

    window = MainWindow(config)
    window.show()

    return app.exec()

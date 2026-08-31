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
import threading
import time

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
    QSlider,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from client import config as client_config
from client.gui.assets import app_icon
from client.gui.controller_config import ConfigurationStore, default_configuration
from client.gui.controller_layouts import LAYOUTS, get_layout
from client.gui.controller_presets import mappings_for, materialise
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

#: How long to wait before retrying a video stream that failed while the
#: server still says a source exists. Long enough not to hammer a source that
#: is restarting, short enough that a player does not sit staring at a frozen
#: window wondering whether to reconnect by hand.
_VIDEO_RETRY_S = 5.0

MAX_CONTROLLERS = client_config.MAX_CONTROLLERS

#: Controller table columns.
#:
#: All of them are named, not just the awkward one. The status column has now
#: moved twice -- once when Controls/Configure/Rumble arrived, again for the
#: controller type -- and both times a surviving literal silently addressed a
#: cell *widget* instead, where writing text does nothing and reports no error.
_COL_USE = 0
_COL_SLOT = 1
_COL_NAME = 2
_COL_GAMEPAD = 3
_COL_CONFIG = 4
_COL_TYPE = 5
_COL_CONFIGURE = 6
_COL_RUMBLE = 7
_COL_STATUS = 8
_COL_COUNT = 9


class MainWindow(QMainWindow):
    def __init__(self, config: client_config.ClientConfig) -> None:
        super().__init__()
        self._config = config

        self._backend = None
        self._transport: ClientTransport | None = None
        self._loop: InputLoop | None = None
        self._devices: list = []
        self._connect_result = None
        self._configurations = ConfigurationStore.from_config(config)

        #: Video state. The advert is written from the input-loop thread when a
        #: control message lands and read from the GUI thread, so it is the one
        #: piece of cross-thread state here and takes a lock.
        self._video_lock = threading.Lock()
        self._video_source: dict | None = None
        self._video_receiver = None
        self._video_decoder = None
        self._video_audio = None
        self._video_window = None
        #: Set when the player closes the video window, so the every-tick
        #: auto-open does not immediately put it back.
        self._video_window_dismissed = False
        self._video_retry_at = 0.0
        self._video_query_at = 0.0
        self._video_unavailable = ""

        #: True while the window is being built and populated. Seeding a
        #: widget emits its change signal, and those handlers write the UI
        #: back to disk -- during construction the UI is not yet populated,
        #: so that would overwrite saved settings with blanks.
        self._loading = True

        self.setWindowTitle("Remote Bluetooth Game Control")
        self.setWindowIcon(app_icon())
        self.resize(980, 720)

        self._build_ui()
        self._refresh_devices()
        self._load_config_into_ui()
        self._loading = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(UI_INTERVAL_MS)

        # Populate the server list without blocking the first paint: discovery
        # waits over a second for replies, and doing that inside __init__ would
        # show the user an empty frozen window.
        QTimer.singleShot(150, self._on_discover)

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
        self._form = form

        # Every entry names one transport. There is deliberately no "Auto":
        # it tried direct then hole-punch, which meant a failed connection could
        # not be attributed to either path -- the player could not tell whether
        # the address was wrong or the broker was down. Choosing the transport
        # makes the failure legible.
        self._mode = QComboBox()
        self._mode.addItem("On this network (LAN / VPN)", "direct")
        self._mode.addItem("Through a tunnel or port forward", "tunnel")
        self._mode.addItem("Over the Internet (hole-punch)", "punch")
        self._mode.addItem("Over the Internet (relay via broker)", "relay")
        self._mode.setItemData(
            1,
            "A public address that forwards to the server -- an frp UDP proxy, "
            "a router port forward, or a mesh VPN such as Tailscale. The "
            "lowest-latency way across the internet, because nothing bounces "
            "off a third machine.",
            Qt.ItemDataRole.ToolTipRole,
        )
        self._mode.setItemData(
            2,
            "Connects the two machines directly by punching through both NATs. "
            "Falls back to relaying by itself if that fails.",
            Qt.ItemDataRole.ToolTipRole,
        )
        self._mode.setItemData(
            3,
            "Sends everything through the broker. Slower than hole-punch, but "
            "it works on networks where punching cannot -- and it skips the "
            "~10 s of probing that is guaranteed to fail there.",
            Qt.ItemDataRole.ToolTipRole,
        )
        self._mode.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Connect:", self._mode)

        # Servers found for the selected mode, plus a Custom row for a server
        # that is hidden or otherwise not announcing itself.
        server_row = QHBoxLayout()
        self._server_list = QComboBox()
        self._server_list.setMinimumWidth(280)
        self._server_list.currentIndexChanged.connect(self._on_server_selected)
        self._search_button = QPushButton("Search")
        self._search_button.clicked.connect(self._on_discover)
        server_row.addWidget(self._server_list, 1)
        server_row.addWidget(self._search_button)
        form.addRow("Server:", _wrap(server_row))

        host_row = QHBoxLayout()
        self._host = QLineEdit()
        self._host.setPlaceholderText("Server address, e.g. 192.168.1.50")
        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(client_config.DEFAULT_PORT)
        host_row.addWidget(self._host, 1)
        host_row.addWidget(QLabel("Port:"))
        host_row.addWidget(self._port)
        self._host_row = _wrap(host_row)
        form.addRow("Address:", self._host_row)

        punch_row = QHBoxLayout()
        self._room = QLineEdit()
        # NOT the server name. The broker keys rooms by this code alone
        # (`rendezvous/broker.py` -- `message.get("room")`); the name is a
        # cosmetic label in the public listing and matches nothing. The old
        # placeholder said "Server name or room code" and was followed
        # literally, which fails with no diagnosis on either side.
        self._room.setPlaceholderText("Room code from the server")
        self._room.setToolTip(
            "The room code set on the server, under Visibility. Not the "
            "server's name -- the broker matches on the code alone."
        )
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

        # Enabled only once the server tells us a source exists, so the button
        # never offers something that cannot happen.
        self._video_button = QPushButton("Watch stream")
        self._video_button.setEnabled(False)
        self._video_button.setToolTip(
            "Open the video stream. F11 for fullscreen, L for the latency overlay."
        )
        self._video_button.clicked.connect(self._on_watch_clicked)

        self._state_label = QLabel("Not connected")
        self._state_label.setStyleSheet("color: #888;")

        buttons.addWidget(self._connect_button)
        buttons.addWidget(self._video_button)
        buttons.addLayout(self._build_audio_controls())
        buttons.addWidget(self._state_label, 1)
        outer.addLayout(buttons)

        return group

    def _build_audio_controls(self) -> QHBoxLayout:
        """Mute and volume for the stream's audio.

        Here rather than inside the video window: the window is a bare painted
        surface with a fullscreen mode, and putting chrome in it would mean
        hiding that chrome again for fullscreen. The shortcuts (M, and the
        arrow keys) reach the same controls while watching.
        """
        row = QHBoxLayout()

        self._mute_button = QToolButton()
        self._mute_button.setCheckable(True)
        self._mute_button.setText("🔊")
        self._mute_button.setToolTip("Mute the stream's audio (M)")
        self._mute_button.toggled.connect(self._on_mute_toggled)

        self._volume_slider = QSlider(Qt.Orientation.Horizontal)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setFixedWidth(110)
        self._volume_slider.setToolTip("Stream volume")
        self._volume_slider.valueChanged.connect(self._on_volume_changed)

        row.addWidget(self._mute_button)
        row.addWidget(self._volume_slider)
        return row

    # -- audio output ------------------------------------------------------

    def _on_volume_changed(self, value: int) -> None:
        if self._loading:
            return
        self._config.video_volume = int(value)
        audio = self._video_audio
        if audio is not None:
            audio.set_volume(value)
        self._update_mute_icon()
        self._save_ui_into_config()

    def _on_mute_toggled(self, muted: bool) -> None:
        if self._loading:
            return
        self._config.video_muted = bool(muted)
        audio = self._video_audio
        if audio is not None:
            audio.set_muted(muted)
        self._update_mute_icon()
        self._save_ui_into_config()

    def _update_mute_icon(self) -> None:
        silent = self._config.video_muted or self._config.video_volume == 0
        self._mute_button.setText("🔇" if silent else "🔊")

    def adjust_volume(self, delta: int) -> None:
        """Nudge the volume, from a shortcut. Unmutes if it was muted."""
        if self._config.video_muted and delta > 0:
            self._mute_button.setChecked(False)
        self._volume_slider.setValue(self._volume_slider.value() + delta)

    def toggle_mute(self) -> None:
        self._mute_button.setChecked(not self._mute_button.isChecked())

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

        self._table = QTableWidget(MAX_CONTROLLERS, _COL_COUNT)
        self._table.setHorizontalHeaderLabels(
            [
                "Use", "Slot", "Player name", "Gamepad",
                "Configuration", "Controller type", "", "Rumble", "Status",
            ]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)

        header = self._table.horizontalHeader()
        for column in (_COL_USE, _COL_SLOT, _COL_CONFIGURE, _COL_RUMBLE, _COL_STATUS):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for column in (_COL_NAME, _COL_GAMEPAD, _COL_CONFIG, _COL_TYPE):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)

        self._enable_boxes: list[QCheckBox] = []
        self._username_edits: list[QLineEdit] = []
        self._device_combos: list[QComboBox] = []
        self._config_combos: list[QComboBox] = []
        self._type_combos: list[QComboBox] = []
        self._rumble_boxes: list[QCheckBox] = []

        for row in range(MAX_CONTROLLERS):
            enable = QCheckBox()
            enable.stateChanged.connect(self._on_slot_toggled)
            self._table.setCellWidget(row, _COL_USE, _center(enable))
            self._enable_boxes.append(enable)

            self._table.setItem(row, _COL_SLOT, QTableWidgetItem(str(row)))

            username = QLineEdit()
            username.setPlaceholderText(f"Player {row + 1}")
            username.editingFinished.connect(self._on_username_changed)
            self._table.setCellWidget(row, _COL_NAME, username)
            self._username_edits.append(username)

            combo = QComboBox()
            combo.currentIndexChanged.connect(
                lambda _=0, r=row: self._on_slot_device_changed(r)
            )
            self._table.setCellWidget(row, _COL_GAMEPAD, combo)
            self._device_combos.append(combo)

            # Which named configuration (a bundle of bindings, one set per
            # controller type) this slot loads. Per slot rather than per client:
            # each slot has its own pad.
            configuration = QComboBox()
            configuration.setToolTip(
                "Which saved configuration to load.\n\n"
                "A configuration holds bindings for every controller type; the "
                "next column picks which of them this slot uses."
            )
            configuration.currentIndexChanged.connect(
                lambda _=0, r=row: self._on_configuration_changed(r)
            )
            self._table.setCellWidget(row, _COL_CONFIG, configuration)
            self._config_combos.append(configuration)

            # Which controller type's bindings, inside that configuration, this
            # slot uses. Per slot and not on the configuration, because slots
            # share configurations by name -- storing it there meant two slots
            # on one configuration fought over the setting.
            controller_type = QComboBox()
            controller_type.setToolTip(
                "Which controller this slot's bindings are laid out for.\n\n"
                "Changes what the buttons are called and what the preview "
                "shows. It does not change what the server emulates."
            )
            for layout_entry in LAYOUTS:
                controller_type.addItem(layout_entry.name, layout_entry.key)
            controller_type.currentIndexChanged.connect(
                lambda _=0, r=row: self._on_type_changed(r)
            )
            self._table.setCellWidget(row, _COL_TYPE, controller_type)
            self._type_combos.append(controller_type)

            configure = QPushButton("Configure…")
            configure.clicked.connect(lambda _=False, r=row: self._on_configure_slot(r))
            self._table.setCellWidget(row, _COL_CONFIGURE, configure)

            rumble = QCheckBox()
            rumble.setToolTip(
                "Play console rumble on this controller.\n\n"
                "The client-wide switch still applies: a slot cannot opt in "
                "while rumble is off for the whole client."
            )
            rumble.stateChanged.connect(self._on_rumble_toggled)
            self._table.setCellWidget(row, _COL_RUMBLE, _center(rumble))
            self._rumble_boxes.append(rumble)

            self._table.setItem(row, _COL_STATUS, QTableWidgetItem("—"))

        layout.addWidget(self._table)

        actions = QHBoxLayout()
        refresh = QPushButton("Refresh gamepad list")
        refresh.clicked.connect(self._refresh_devices)
        actions.addWidget(refresh)

        self._capture = QCheckBox("Capture keyboard")
        self._capture.setToolTip(
            "Send keystrokes to the controller instead of typing them.\n\n"
            "Armed, every key goes to whichever slot uses the Keyboard, and "
            "nothing can be typed into this window. Press Esc to release.\n\n"
            "Gamepads never need this -- they work in the background."
        )
        self._capture.toggled.connect(self._on_capture_toggled)
        actions.addWidget(self._capture)

        self._capture_hint = QLabel("Keys type normally")
        self._capture_hint.setStyleSheet("color: #888;")
        actions.addWidget(self._capture_hint)

        manage_configs = QPushButton("Manage configurations…")
        manage_configs.setToolTip(
            "Edit, rename, delete, export or import your saved controller "
            "configurations."
        )
        manage_configs.clicked.connect(self._on_manage_configurations)
        actions.addWidget(manage_configs)

        self._rumble = QCheckBox("Rumble")
        self._rumble.setToolTip(
            "Play rumble sent back from the console.\n\n"
            "Turning this off tells the server to stop sending it, so no rumble "
            "data crosses the network at all -- it is not a local mute.\n\n"
            "Each controller has its own switch too, and the server has one; "
            "all of them must be on."
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

        # Seeding a widget emits its change signal, and those handlers write the
        # UI back into the config. During load the UI is only half-populated, so
        # letting them run overwrites saved settings with defaults -- the
        # per-slot rumble flags in particular.
        guarded = [
            self._rumble,
            self._volume_slider,
            self._mute_button,
            *self._rumble_boxes,
            *self._config_combos,
            *self._type_combos,
        ]
        for widget in guarded:
            widget.blockSignals(True)

        # "auto" was removed; an older config may still name it. Direct is the
        # closest equivalent and the overwhelmingly common case.
        mode = "direct" if cfg.mode == "auto" else cfg.mode
        index = self._mode.findData(mode)
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
        self._volume_slider.setValue(cfg.video_volume)
        self._mute_button.setChecked(cfg.video_muted)
        self._update_mute_icon()

        for row in range(MAX_CONTROLLERS):
            entry = cfg.controller(row)
            self._enable_boxes[row].setChecked(entry.enabled)
            self._username_edits[row].setText(entry.username)
            self._rumble_boxes[row].setChecked(entry.rumble_enabled)

        self._refresh_configuration_combos()

        for widget in guarded:
            widget.blockSignals(False)

        self._on_mode_changed()

    def _save_ui_into_config(self) -> None:
        if self._loading:
            return

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

            entry.rumble_enabled = self._rumble_boxes[row].isChecked()
            entry.configuration = self._config_combos[row].currentData() or ""
            entry.layout = self._type_combos[row].currentData() or ""

            combo = self._device_combos[row]
            device = combo.currentData()
            if device is not None:
                entry.guid = device.guid
                entry.device_name = device.display_name()

        self._configurations.into_config(cfg)
        client_config.save(cfg)

    # -- devices -----------------------------------------------------------

    def _ensure_backend(self) -> bool:
        if self._backend is not None:
            return True
        try:
            # keyboard=True adds the keyboard as an extra virtual gamepad, so it
            # appears in the same list as real pads and can be assigned to a
            # slot like any of them.
            self._backend = create_backend(self._config.effective_backend(), keyboard=True)
            self._backend.open()
        except InputBackendError as exc:
            QMessageBox.warning(self, "No gamepad support", str(exc))
            return False

        self._apply_saved_mappings()
        return True

    def _refresh_devices(self) -> None:
        if not self._ensure_backend():
            return

        try:
            self._devices = self._backend.list_devices()
        except InputBackendError as exc:
            log.warning("Could not list devices: %s", exc)
            self._devices = []

        claimed_guids: set[str] = set()

        for row, combo in enumerate(self._device_combos):
            previous = combo.currentData()
            combo.blockSignals(True)
            combo.clear()

            combo.addItem("None", None)

            for device in self._devices:
                note = device.status_note()
                combo.addItem(
                    f"{device.display_name()} — {note}" if note else device.display_name(),
                    device,
                )

            # Restore the prior selection, then the saved one; otherwise leave
            # the slot on None. Auto-assigning a different pad per slot guessed
            # wrong as often as right and silently claimed devices the player
            # had not chosen.
            restored = False
            for wanted in (previous.guid if previous is not None else None,
                           self._config.controller(row).guid):
                if not wanted:
                    continue
                for index in range(combo.count()):
                    data = combo.itemData(index)
                    if data is not None and data.guid == wanted:
                        combo.setCurrentIndex(index)
                        restored = True
                        break
                if restored:
                    break
            if not restored:
                combo.setCurrentIndex(0)      # None

            # A saved config could name one pad in two slots; the dropdown
            # cannot prevent that, so drop the later claim here.
            chosen = combo.currentData()
            if chosen is not None and not _is_shareable(chosen):
                if chosen.guid in claimed_guids:
                    combo.setCurrentIndex(0)
                else:
                    claimed_guids.add(chosen.guid)

            combo.blockSignals(False)

        self._refresh_configuration_combos()
        self._update_slot_availability()

    # -- controller configurations ----------------------------------------

    def _on_configure_slot(self, row: int) -> None:
        """Open the mapping screen for one slot's gamepad."""
        if not self._ensure_backend():
            return
        if not self._devices:
            self._refresh_devices()

        device = self._device_combos[row].currentData()
        if device is None:
            QMessageBox.information(
                self,
                "No controller selected",
                f"Slot {row} has no gamepad selected.\n\n"
                "Pick one in the Gamepad column, or press 'Refresh gamepad list' "
                "if the controller is not there.",
            )
            return

        # The dialog polls the device directly, so it has to be open. When a
        # session is live the input loop already holds it and must keep it;
        # otherwise we opened it purely for the dialog and have to hand it back.
        borrowed = self._loop is None
        try:
            device = self._backend.acquire(device.instance_id)
        except InputBackendError as exc:
            QMessageBox.warning(self, "Controller unavailable", str(exc))
            return

        # Edit whatever the Configuration column is showing. A built-in opens
        # too -- it just cannot be overwritten, and the dialog offers only
        # "Save as..." for it.
        entry = self._config.controller(row)
        configuration = self._configurations.get(entry.configuration)

        if configuration is None:
            working = default_configuration(device, self._slot_layout(row))
            working.name = self._configurations.unique_name(working.name)
        else:
            # A built-in stores no bindings, so resolve them for this pad;
            # a custom one is copied so Cancel really discards.
            working = materialise(
                configuration, device, self._pad_bindings(device), keep_builtin=True
            )

        working.layout = self._slot_layout(row)

        dialog = MappingDialog(
            self._backend, device, working, self, store=self._configurations
        )
        accepted = dialog.exec()

        # "Save as..." stores its copy immediately and carries on editing it, so
        # the copy has to be kept even when the dialog is then cancelled.
        if accepted or dialog.created_copy:
            saved = dialog.configuration
            self._configurations.upsert(saved)
            entry.configuration = saved.name
            entry.layout = saved.layout
            self._config.preview_layout = saved.layout
            self._configurations.into_config(self._config)
            self._refresh_configuration_combos()
            self._set_status(f"Slot {row} now uses '{saved.name}'")

        # Either way, re-push what is actually stored: the dialog writes
        # bindings into the backend live while binding, including ones the
        # player then cancelled.
        self._apply_saved_mappings()
        self._save_ui_into_config()

        if borrowed:
            self._backend.release(device.instance_id)

    def _on_slot_device_changed(self, row: int) -> None:
        """React to a slot's gamepad changing.

        Two rules beyond refreshing the configuration list:

        * **None disables the slot.** A slot with no controller cannot stream,
          so leaving "Use" ticked would advertise a controller that sends
          nothing and hold an adapter on the server for it.
        * **A physical pad belongs to one slot.** Enforced by disabling that
          entry in every other slot's dropdown (see
          :meth:`_refresh_device_availability`) rather than by taking it away
          from whoever had it, which was startling.
        """
        device = self._device_combos[row].currentData()

        if device is None:
            box = self._enable_boxes[row]
            box.blockSignals(True)
            box.setChecked(False)
            box.blockSignals(False)

        self._update_slot_availability()
        self._refresh_configuration_combos()
        self._save_ui_into_config()

    def _on_configuration_changed(self, row: int) -> None:
        name = self._config_combos[row].currentData()
        self._config.controller(row).configuration = name or ""
        # A different configuration has a different set of configured types, so
        # the type list has to follow.
        self._refresh_type_combos()
        self._apply_saved_mappings()
        self._save_ui_into_config()

    def _on_type_changed(self, row: int) -> None:
        key = self._type_combos[row].currentData()
        self._config.controller(row).layout = key or ""
        self._apply_saved_mappings()
        self._save_ui_into_config()

    def _refresh_configuration_combos(self) -> None:
        """Rebuild each slot's configuration list for the pad it is using."""
        for row, combo in enumerate(self._config_combos):
            device = self._device_combos[row].currentData()
            wanted = self._config.controller(row).configuration

            combo.blockSignals(True)
            combo.clear()
            combo.addItem("Default for this gamepad", "")

            entries = (
                self._configurations.for_device(device.guid)
                if device is not None
                else list(self._configurations)
            )
            for entry in entries:
                combo.addItem(entry.describe(), entry.name)

            index = combo.findData(wanted) if wanted else 0
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

        self._refresh_type_combos()

    def _refresh_type_combos(self) -> None:
        """Mark which controller types the slot's configuration actually has.

        Every type stays selectable -- picking one that has no bindings yet is
        how you start building it -- but an unconfigured one says so, rather
        than looking identical to a working one.
        """
        for row, combo in enumerate(self._type_combos):
            entry = self._config.controller(row)
            configuration = (
                self._configurations.get(entry.configuration)
                if entry.configuration
                else None
            )
            configured = set(
                configuration.configured_layouts() if configuration is not None else ()
            )

            combo.blockSignals(True)
            for index in range(combo.count()):
                key = combo.itemData(index)
                name = get_layout(key).name
                combo.setItemText(
                    index, name if key in configured else f"{name} (not configured)"
                )

            wanted = entry.layout or (
                configuration.layout if configuration is not None else ""
            )
            position = combo.findData(wanted) if wanted else -1
            combo.setCurrentIndex(position if position >= 0 else 0)
            combo.blockSignals(False)

    def _slot_layout(self, row: int) -> str:
        """Which controller type this slot uses, falling back sensibly."""
        entry = self._config.controller(row)
        if entry.layout:
            return entry.layout
        configuration = (
            self._configurations.get(entry.configuration) if entry.configuration else None
        )
        return configuration.layout if configuration is not None else LAYOUTS[0].key

    def _pad_bindings(self, device):
        """SDL's view of where this pad's controls sit, or None if unknown."""
        reader = getattr(self._backend, "pad_bindings", None)
        if reader is None or device is None:
            return None
        try:
            return reader(device.instance_id)
        except Exception:
            log.debug("Could not read pad bindings for %s", device.guid, exc_info=True)
            return None

    def _apply_saved_mappings(self) -> None:
        """Push each slot's chosen bindings into the backend.

        A slot with no named configuration falls back to any mapping stored for
        that device GUID, so a pad configured before configurations existed keeps
        working.
        """
        setter = getattr(self._backend, "set_mapping", None)
        if setter is None:
            return

        for guid, payload in (self._config.mappings or {}).items():
            try:
                setter(guid, DeviceMapping.from_dict(payload))
            except Exception:
                log.warning("Ignoring unreadable mapping for %s", guid, exc_info=True)

        # Named configurations win: they are what the slot explicitly selected.
        #
        # Applied in slot order, so when one device appears in two slots the
        # lowest-numbered one wins deterministically. set_mapping is keyed by
        # GUID, and the keyboard is the only device allowed in several slots at
        # once, so that is the only case this can arise -- flagged in the status
        # column rather than silently resolved.
        for row, combo in enumerate(self._device_combos):
            device = combo.currentData()
            if device is None:
                continue
            name = self._config.controller(row).configuration
            configuration = self._configurations.get(name) if name else None
            if configuration is None:
                continue

            mappings, _approximate = mappings_for(
                configuration, device, self._pad_bindings(device)
            )
            mapping = mappings.get(self._slot_layout(row))
            if mapping is not None and not mapping.is_empty():
                setter(device.guid, mapping)

    def _on_manage_configurations(self) -> None:
        """Open the list of saved configurations."""
        from client.gui.configurations_dialog import ConfigurationsDialog

        self._ensure_backend()
        if not self._devices:
            self._refresh_devices()

        dialog = ConfigurationsDialog(
            self._configurations,
            self._backend,
            self._devices,
            self,
            on_changed=self._configurations_changed,
            pad_bindings=self._pad_bindings,
        )
        dialog.exec()

    def _configurations_changed(self) -> None:
        """Persist and re-sync after the manage dialog edits the store."""
        self._configurations.into_config(self._config)

        # A deleted or renamed configuration leaves slots pointing at a name
        # that no longer exists; they fall back to their gamepad's default
        # rather than silently keeping stale bindings.
        live = {entry.name for entry in self._configurations}
        for row in range(MAX_CONTROLLERS):
            entry = self._config.controller(row)
            if entry.configuration and entry.configuration not in live:
                entry.configuration = ""

        self._refresh_configuration_combos()
        self._apply_saved_mappings()
        self._save_ui_into_config()

    # -- keyboard-as-controller -------------------------------------------
    #
    # Capture is armed explicitly rather than being implied by focus. Two
    # reasons: keys have to be intercepted *before* any focused child widget
    # consumes them (an earlier version overrode keyPressEvent on this window
    # and never saw a keystroke, because the table, combos and text fields ate
    # them first), and once they are intercepted the player can no longer type
    # a password or a player name. An explicit switch makes both states
    # unambiguous.
    #
    # The filter is installed on the QApplication, so it sees events ahead of
    # every widget. There is deliberately no global OS hook -- see
    # client/input/keyboard_backend.py.

    def _feed_key(self, key: int, down: bool) -> None:
        if self._backend is None:
            return
        for backend in getattr(self._backend, "backends", [self._backend]):
            setter = getattr(backend, "set_key", None)
            if setter is not None:
                setter(key, down)

    def _clear_keys(self) -> None:
        if self._backend is None:
            return
        for backend in getattr(self._backend, "backends", [self._backend]):
            clear = getattr(backend, "clear_keys", None)
            if clear is not None:
                clear()

    def _on_capture_toggled(self, checked: bool) -> None:
        app = QApplication.instance()
        if app is None:
            return

        if checked:
            app.installEventFilter(self)
            self._set_status(
                "Keyboard captured — keys drive the controller. "
                "Turn this off to type."
            )
        else:
            app.removeEventFilter(self)
            self._clear_keys()
            self._set_status("Keyboard released")

        self._capture_hint.setText(
            "Capturing — typing goes to the controller"
            if checked
            else "Keys type normally"
        )

    def _owns_focus(self) -> bool:
        """True while any window of ours is the active one."""
        if self.isActiveWindow():
            return True
        window = self._video_window
        return window is not None and window.isActiveWindow()

    def eventFilter(self, obj, event):  # noqa: N802 - Qt override
        """Route keystrokes to the keyboard controller while capture is armed."""
        from PySide6.QtCore import QEvent

        if not self._capture.isChecked():
            return super().eventFilter(obj, event)

        # Only while one of our own windows is active: capture must not follow
        # the user into another application. The video window counts -- playing
        # fullscreen is exactly when a keyboard player needs their controls,
        # and checking only the main window silently killed capture the moment
        # the stream was opened.
        if not self._owns_focus():
            return super().eventFilter(obj, event)

        if event.type() == QEvent.Type.KeyPress:
            # Leave the capture toggle itself operable by keyboard, so there is
            # always a way out that does not need the mouse.
            if int(event.key()) == Qt.Key.Key_Escape:
                self._capture.setChecked(False)
                return True
            if not event.isAutoRepeat():
                self._feed_key(int(event.key()), True)
            return True

        if event.type() == QEvent.Type.KeyRelease:
            if not event.isAutoRepeat():
                self._feed_key(int(event.key()), False)
            return True

        return super().eventFilter(obj, event)

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt override
        # Losing focus mid-keypress would otherwise latch that key down forever:
        # the release event goes to whichever window took focus, not to us.
        from PySide6.QtCore import QEvent

        # Focus moving between our own two windows is not "focus lost": the
        # video window taking over must not drop the keys being held.
        if event.type() == QEvent.Type.ActivationChange and not self._owns_focus():
            self._clear_keys()
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
        # Hide the whole form row, label included. Hiding only the field leaves
        # an orphaned "Rendezvous:" label sitting against blank space.
        # Both broker modes want the same two fields, and both address modes
        # want the host row -- the transport differs, the settings do not.
        self._set_row_visible(self._host_row, mode in ("direct", "tunnel"))
        self._set_row_visible(self._punch_row, mode in ("punch", "relay"))

        # The list only ever holds results for one transport, so switching
        # invalidates it -- and immediately repopulates it, since an empty list
        # after switching reads as "no servers" rather than "not looked yet".
        self._populate_server_list([], mode)
        if not self._loading:
            QTimer.singleShot(0, self._on_discover)

    def _set_row_visible(self, field: QWidget, visible: bool) -> None:
        """Show or hide a QFormLayout row and its label together."""
        setter = getattr(self._form, "setRowVisible", None)
        if setter is not None:
            setter(field, visible)
            return

        # Qt < 6.4 has no setRowVisible; fall back to the label lookup.
        field.setVisible(visible)
        label = self._form.labelForField(field)
        if label is not None:
            label.setVisible(visible)

    #: Sentinel for the "type it in yourself" row of the server list.
    CUSTOM_SERVER = "__custom__"

    @staticmethod
    def _uses_broker(mode: str) -> bool:
        """True for the modes whose server list comes from the broker.

        Both broker transports browse the same way; only what happens after the
        introduction differs. Written as a predicate rather than as
        ``mode != "direct"`` because ``tunnel`` is neither -- it has no
        discovery at all, and treating it as a broker mode sent it to ask a
        broker it was never given.
        """
        return mode in ("punch", "relay")

    def _on_discover(self) -> None:
        """Search for servers on whichever transport is selected.

        Results go into the inline list rather than being applied directly. An
        earlier version connected to whichever server answered first, which is
        fine with one server on the bench and wrong the moment there are two.
        """
        mode = self._mode.currentData()
        self._search_button.setEnabled(False)
        self._set_status(
            "Asking the broker..." if self._uses_broker(mode)
            else "Searching this network..."
        )
        QApplication.processEvents()

        try:
            servers = self._find_servers(mode)
        finally:
            self._search_button.setEnabled(True)

        self._populate_server_list(servers, mode)

        if servers:
            self._set_status(f"Found {len(servers)} server(s)")
        else:
            self._set_status(self._no_servers_message(mode))

    def _no_servers_message(self, mode: str) -> str:
        """Say which of the two empty answers this is.

        "No servers found" points the player at their own settings, which is
        wrong half the time: over the Internet the usual cause is a *server*
        that never registered with the broker, and nothing the player changes
        here will help. Measured case -- a server whose broker was saved after
        it started, so it never registered, so the broker listed nothing.
        """
        if mode == "tunnel":
            return "Enter the public address of the tunnel below"
        if not self._uses_broker(mode):
            return "No servers replied on this network — use Custom to enter an address"

        host, _ = self._broker_fields()
        if not host:
            return "Enter a broker address, then Search"

        from client.net.connect import broker_reachable

        if not broker_reachable.answered:
            return (
                f"No answer from broker {host} — check the address, or that it "
                f"is running"
            )
        return (
            f"Broker {host} lists no servers. Either none is registered with "
            f"it, or the one you want is hidden — use Custom with its room code."
        )

    def _find_servers(self, mode: str) -> list[dict]:
        # A tunnel announces itself nowhere: it is a public address somebody
        # configured, known to the operator and to nothing else.
        if mode == "tunnel":
            return []

        if not self._uses_broker(mode):
            import asyncio

            try:
                from server.discovery import discover_servers

                return asyncio.run(discover_servers(timeout=1.5))
            except Exception as exc:
                log.debug("LAN discovery failed: %s", exc)
                return []

        broker_host, broker_port = self._broker_fields()
        if not broker_host:
            return []

        from client.net.connect import list_broker_servers

        return list_broker_servers(broker_host, broker_port)

    def _populate_server_list(self, servers: list[dict], mode: str) -> None:
        self._server_list.blockSignals(True)
        self._server_list.clear()

        for entry in servers:
            if self._uses_broker(mode):
                label = f"{entry.get('name')} — via broker"
                data = {
                    "kind": "punch",
                    "room": entry.get("room"),
                    "name": entry.get("name", ""),
                }
            else:
                label = f"{entry.get('name') or entry.get('host')} — {entry.get('host')}"
                data = {
                    "kind": "direct",
                    "host": entry.get("host"),
                    "port": entry.get("port"),
                    "name": entry.get("name", ""),
                }

            capacity = entry.get("capacity")
            if capacity:
                label += f"  ({entry.get('in_use', 0)}/{capacity} in use)"
            self._server_list.addItem(label, data)

        # Always present, and the only option for a server set to hidden.
        self._server_list.addItem("Custom — enter details below", self.CUSTOM_SERVER)
        self._server_list.setCurrentIndex(self._preferred_server_index(servers, mode))
        self._server_list.blockSignals(False)

        self._on_server_selected()

    def _preferred_server_index(self, servers: list[dict], mode: str) -> int:
        """Which entry to select once a search finishes.

        Selecting the first result unconditionally destroys a configured
        address: discovery runs by itself at startup, the selection overwrites
        the host and port fields, and the next save persists the substitution.
        A server reachable only over a VPN, or one set to hidden, is silently
        replaced by whichever machine answered a broadcast first -- and the
        address the player typed is gone for good.

        So: prefer the entry that matches what is already configured; failing
        that, keep Custom selected whenever there is something to preserve.
        A fresh install has nothing to lose, and there the first result is the
        helpful answer.
        """
        custom_index = self._server_list.count() - 1
        by_room = self._uses_broker(mode)

        configured = (
            self._room.text().strip() if by_room else self._host.text().strip()
        )

        if configured:
            for index in range(custom_index):
                data = self._server_list.itemData(index)
                if not isinstance(data, dict):
                    continue
                found = data.get("room") if by_room else data.get("host")
                if found and str(found) == configured:
                    return index
            # Configured, but not among the results. Keep their details.
            return custom_index

        return 0 if servers else custom_index

    def _on_server_selected(self) -> None:
        """Fill the detail fields from the chosen server, or free them for Custom."""
        data = self._server_list.currentData()
        custom = data is None or data == self.CUSTOM_SERVER

        # Details stay editable on Custom and become read-only for a discovered
        # server, so it is obvious which one is in effect.
        for widget in (self._host, self._room):
            widget.setReadOnly(not custom)
        self._port.setReadOnly(not custom)

        if custom:
            return

        if data.get("kind") == "direct":
            self._host.setText(str(data.get("host", "")))
            self._port.setValue(int(data.get("port") or self._port.value()))
        else:
            self._room.setText(str(data.get("room", "")))

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
            on_control=self._on_server_control,
            stun_servers=cfg.stun_servers,
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

        # Only when it was *unexpected*. Someone who selected relay mode chose
        # this path and knows the trade; telling them again on every connect
        # turns a real warning into a dialog to click past.
        if result.is_relayed and result.fell_back:
            QMessageBox.information(
                self,
                "Connected via relay",
                "NAT traversal failed, so traffic is being relayed through the "
                "rendezvous broker.\n\nThe connection works, but latency will be "
                "noticeably higher than a direct or hole-punched path.\n\n"
                "If this happens every time, selecting \"relay via broker\" "
                "will skip the ~10 s of probing that failed here.",
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

        # Ask where the video is. The answer also arrives unprompted whenever
        # a source appears, but asking covers the case where one was already
        # streaming before we connected.
        transport.queue_control(ControlOp.VIDEO_QUERY, {})

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
        self._stop_video()
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

    # -- video -------------------------------------------------------------

    def _on_server_control(self, body: dict) -> None:
        """Handle a control message from the server.

        Runs on the **input loop's thread**, so it does the least possible work
        and touches nothing in Qt: it stores the message and lets ``_tick``
        act on it. Calling into widgets from here would be a crash waiting for
        the right timing.
        """
        if body.get("op") != ControlOp.VIDEO_SOURCE:
            return
        with self._video_lock:
            self._video_source = dict(body)

    def _pending_video_source(self) -> dict | None:
        with self._video_lock:
            return dict(self._video_source) if self._video_source else None

    def _start_video(self) -> None:
        """Bring up the video pipeline for the advertised source."""
        if self._video_receiver is not None:
            return

        source = self._pending_video_source()
        if not source or not source.get("available"):
            return

        try:
            from client.media.decoder import VideoDecoder
            from client.net.video import VideoReceiver
        except ImportError as exc:
            log.info("Video playback unavailable: %s", exc)
            self._video_unavailable = (
                "Video needs the media extras: pip install -e '.[client,video]'"
            )
            return

        cfg = self._config
        source.setdefault("password", cfg.password)

        audio = None
        if cfg.video_audio_enabled:
            try:
                from client.media.audio import AudioPlayout

                audio = AudioPlayout(
                    volume=cfg.video_volume, muted=cfg.video_muted
                )
                audio.start()
            except Exception:
                log.debug("Could not start audio playback", exc_info=True)
                audio = None

        receiver = VideoReceiver(
            cfg.password,
            client_name=cfg.client_name,
            on_audio=(
                (
                    lambda data, ts, seq: audio.feed(
                        data, ts, receiver.clock_offset_ns, seq
                    )
                )
                if audio is not None
                else None
            ),
            stun_servers=cfg.stun_servers,
            # Gameplay already established what this network can do. If it had
            # to relay, video will too, and punching first only delays the
            # picture by the budget that is about to fail.
            force_relay=(
                cfg.mode == "relay"
                or (self._connect_result is not None
                    and self._connect_result.is_relayed)
            ),
        )
        decoder = VideoDecoder(receiver)

        self._video_receiver = receiver
        self._video_decoder = decoder
        self._video_audio = audio

        decoder.start()
        # Connecting can take seconds; the ladder runs on the receiver's own
        # thread so the GUI never blocks on it.
        receiver.connect_async(source)
        self._set_status("Connecting to the video stream...")

    def _stop_video(self) -> None:
        window, self._video_window = self._video_window, None
        if window is not None:
            window.close()
        # The stream is going away, not being refused: a fresh one (a retry, a
        # reconnect, a new source) should open its window as usual.
        self._video_window_dismissed = False

        for component in (self._video_audio, self._video_decoder, self._video_receiver):
            if component is None:
                continue
            try:
                component.stop() if hasattr(component, "stop") else component.close()
            except Exception:
                log.debug("Error stopping %s", type(component).__name__, exc_info=True)

        self._video_audio = None
        self._video_decoder = None
        self._video_receiver = None
        with self._video_lock:
            self._video_source = None
        self._video_button.setEnabled(False)
        self._video_button.setText("Watch stream")

    def _on_watch_clicked(self) -> None:
        """Open or close the video window."""
        if self._video_window is not None:
            self._video_window.close()
            self._video_window = None
            self._video_button.setText("Watch stream")
            return

        if self._video_receiver is None:
            self._start_video()
        if self._video_decoder is None or self._video_receiver is None:
            if self._video_unavailable:
                QMessageBox.information(self, "Video unavailable", self._video_unavailable)
            return

        self._open_video_window()

    def _open_video_window(self) -> None:
        if self._video_window is not None:
            return
        # Asking for it counts as un-dismissing it, however we got here.
        self._video_window_dismissed = False
        from client.gui.video_window import VideoWindow

        window = VideoWindow(self._video_decoder, self._video_receiver, self)
        # `closed`, not `destroyed`: the window has a parent and we hold a
        # reference, so closing it never deletes the C++ object and `destroyed`
        # fired nothing -- leaving the button reading "Close video" forever.
        window.closed.connect(self._on_video_window_closed)
        window.volume_nudged.connect(self.adjust_volume)
        window.mute_toggled.connect(self.toggle_mute)
        if self._config.video_fullscreen:
            window.showFullScreen()
        else:
            window.show()
        self._video_window = window
        self._video_button.setText("Close video")

    def _on_video_window_closed(self, *_args) -> None:
        self._video_window = None
        self._video_window_dismissed = True
        self._video_button.setText("Watch stream")

    def _tick_video(self) -> None:
        """Drive the video side once per GUI tick. Called from ``_tick``."""
        source = self._pending_video_source()
        available = bool(source and source.get("available"))

        if not available:
            if self._video_receiver is not None:
                self._stop_video()
            else:
                self._video_button.setEnabled(False)
                # Ask again now and then. The server pushes an advert when
                # things change, but that direction has no retransmit -- and
                # the common case is a client that connected while still
                # awaiting approval, whose one answer was "no video".
                self._maybe_requery_video()
            return

        self._video_button.setEnabled(True)

        if self._video_receiver is None:
            if self._config.video_enabled:
                self._start_video()
            return

        from client.net.video import VideoStreamState

        state = self._video_receiver.state
        if state is VideoStreamState.FAILED:
            # The source is still advertised, so this is worth retrying --
            # but not faster than the reconnect interval.
            now = time.monotonic()
            if now - self._video_retry_at >= _VIDEO_RETRY_S:
                self._video_retry_at = now
                log.info("Retrying the video stream")
                self._stop_video()
                with self._video_lock:
                    self._video_source = source
            return

        if (
            state is VideoStreamState.STREAMING
            and self._video_window is None
            and not self._video_window_dismissed
        ):
            # Opens itself once when the picture becomes available, but never
            # again after the player closed it -- this runs every tick, so
            # without the flag the window reopened the instant it was shut and
            # could not be got rid of.
            if self._config.video_enabled:
                self._open_video_window()

        window = self._video_window
        if window is not None:
            window.set_controller_rtt(self._best_controller_rtt())

        audio = self._video_audio
        if audio is not None:
            audio.tick_sync(self._video_receiver.present_stats.p50)
            self._video_receiver.audio_underruns = audio.underruns

    def _maybe_requery_video(self) -> None:
        """Re-ask where the video is, at the retry cadence."""
        transport = self._transport
        if transport is None or not transport.is_connected:
            return

        now = time.monotonic()
        if now - self._video_query_at < _VIDEO_RETRY_S:
            return
        self._video_query_at = now
        transport.queue_control(ControlOp.VIDEO_QUERY, {})

    def _best_controller_rtt(self) -> float:
        """The controller figure the overlay pairs with the video one."""
        transport = self._transport
        if transport is None:
            return 0.0
        samples = [
            stats["rtt"]["p50"]
            for stats in transport.latency_snapshot().values()
            if stats["rtt"]["count"]
        ]
        return min(samples) if samples else 0.0

    # -- slot state --------------------------------------------------------

    def _on_rumble_toggled(self) -> None:
        """Apply the rumble switches, live if we are connected.

        Telling the server matters: this is not a local mute. With the server
        informed, disabling here means the data is never transmitted.

        The client-wide switch and the per-slot ones are both sent; the server
        requires all of its gates plus both of ours before it builds a packet.
        """
        enabled = self._rumble.isChecked()
        self._config.rumble_enabled = enabled

        slots = {}
        for row in range(MAX_CONTROLLERS):
            on = self._rumble_boxes[row].isChecked()
            self._config.controller(row).rumble_enabled = on
            slots[row] = on

        # Deliberately never disabled: both switches stay settable at any
        # time, connected or not. Greying the per-slot boxes out when the
        # client-wide one was off blocked setting them up in advance and read
        # as "rumble is locked while connected".

        if self._transport is not None and self._transport.is_connected:
            self._transport.set_rumble_enabled(enabled, slots)

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
        """Enable or disable each slot's controls.

        Capacity is pushed live, so enabling an adapter on the server re-enables
        the slot here without reconnecting.

        **The Gamepad dropdown is never disabled for lack of a device.** An
        earlier version greyed out the whole row whenever "None" was selected,
        including the dropdown itself -- which left no way to pick a controller
        and made None a dead end. Only the server's capacity can take a slot
        away entirely.
        """
        capacity = self._transport.server_capacity if self._transport else 0

        for row in range(MAX_CONTROLLERS):
            has_device = self._device_combos[row].currentData() is not None
            within_capacity = capacity == 0 or row < capacity
            usable = within_capacity and has_device

            if not usable and self._enable_boxes[row].isChecked():
                self._enable_boxes[row].setChecked(False)

            # Choosing a controller must stay possible as long as the slot
            # exists at all.
            self._device_combos[row].setEnabled(within_capacity)
            self._config_combos[row].setEnabled(within_capacity)
            self._type_combos[row].setEnabled(within_capacity)
            self._username_edits[row].setEnabled(within_capacity)
            self._rumble_boxes[row].setEnabled(within_capacity)
            self._enable_boxes[row].setEnabled(usable)

            item = self._table.item(row, _COL_STATUS)
            if not within_capacity:
                tip = (
                    f"The server has only {capacity} Bluetooth adapter"
                    f"{'' if capacity == 1 else 's'}, so this slot cannot be used."
                )
                self._enable_boxes[row].setToolTip(tip)
                if item:
                    item.setText("unavailable")
            elif not has_device:
                self._enable_boxes[row].setToolTip(
                    "Pick a controller for this slot first."
                )
                if item and item.text() in ("unavailable", "—"):
                    item.setText("no controller")
            else:
                self._enable_boxes[row].setToolTip("")
                if item and item.text() in ("unavailable", "no controller"):
                    item.setText("—")

        if capacity:
            self._capacity_label.setText(f"Server capacity: {capacity} controller(s)")
        else:
            self._capacity_label.setText("")

        self._refresh_device_availability()

    def _refresh_device_availability(self) -> None:
        """Grey out, inside each dropdown, the pads another slot already uses.

        Disabling the individual entries rather than the whole control: two
        slots polling one pad would send duplicate input under two player
        names, but the player still has to be able to open the list and choose
        something else.

        The keyboard is exempt -- it is virtual, and sharing it across slots is
        a legitimate way to test.
        """
        claimed: dict[str, int] = {}
        for row, combo in enumerate(self._device_combos):
            device = combo.currentData()
            if device is not None and not _is_shareable(device):
                claimed[device.guid] = row

        for row, combo in enumerate(self._device_combos):
            model = combo.model()
            for index in range(combo.count()):
                item = model.item(index)
                if item is None:
                    continue

                device = combo.itemData(index)
                owner = claimed.get(device.guid) if device is not None else None
                available = owner is None or owner == row

                item.setEnabled(available)
                item.setToolTip(
                    "" if available else f"Already used by slot {owner}."
                )

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

            item = self._table.item(row, _COL_STATUS)
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
        self._tick_video()

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


def _is_shareable(device) -> bool:
    """True if several slots may use this device at once.

    Only the virtual keyboard. A physical pad polled by two slots would send
    the same input twice under two player names.
    """
    from client.input.keyboard_backend import KEYBOARD_GUID

    return device.guid == KEYBOARD_GUID


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

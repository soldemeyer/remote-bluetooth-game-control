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
from client.gui.panels import (
    COL_STATUS,
    ConnectionPanel,
    ControllersPanel,
    LatencyPanel,
)
from client.net.connect import connect as connect_to_server
from client.input import InputBackendError, create_backend
from client.input.mapping import DeviceMapping
from client.loop import InputLoop, SlotRuntime
from client.net.transport import ClientTransport, ConnectionState, TransportError
from common.protocol import ControlOp
from common.design.tokens import Radius, Space, Type
from client.gui.shell import Drawer, HeaderBar, VideoStage
from qtui.buttons import IconButton
from qtui.status import Status
from qtui.theme import apply_theme, qcolor

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
COL_USE = 0
COL_SLOT = 1
COL_NAME = 2
COL_GAMEPAD = 3
COL_CONFIG = 4
COL_TYPE = 5
COL_CONFIGURE = 6
COL_RUMBLE = 7
COL_STATUS = 8
COL_COUNT = 9


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
        #: The embedded video surface, or None while nothing is showing.
        self._video_surface = None
        #: Set when the player hides the picture, so the every-tick auto-show
        #: does not immediately put it back.
        self._video_dismissed = False
        #: Drawer state while fullscreen, so leaving fullscreen restores what
        #: the player had rather than a default.
        self._drawer_was_open = True
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
        # Taller than it was: the theme gives every control a proper touch
        # height, so the same widgets need more room than Fusion's defaults.
        self.resize(1020, 820)

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
        """Video-first: the picture is the window, the controls sit beside it.

        The three groups are exactly the ones that were here before and are
        built by exactly the same methods -- only where they live has changed.
        """
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._header = HeaderBar("Remote Bluetooth Game Control")
        self._drawer_button = IconButton("menu", "Show or hide the controls")
        self._drawer_button.setCheckable(True)
        self._drawer_button.clicked.connect(self._on_drawer_clicked)
        self._header.add_action(self._drawer_button)
        root.addWidget(self._header)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._stage = VideoStage()
        body.addWidget(self._stage, 1)

        self._drawer = Drawer()
        self._drawer.add(self._build_connection_group())
        self._drawer.add(self._build_controller_group())
        self._drawer.add(self._build_latency_group(), 1)
        body.addWidget(self._drawer)
        root.addLayout(body, 1)

        self._build_control_bar()

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self._set_status("Not connected")

    def _build_control_bar(self) -> None:
        """The floating bar over the picture.

        Everything on it acts on the stream, so it is only reachable when
        there is a stream -- which is also why the audio controls moved here
        out of the connection panel: that panel is the one the player closes
        once a session is set up, and it took the volume with it. The keyboard
        shortcuts reach the same controls either way.
        """
        bar = self._stage.controls
        bar.add(self._mute_button)
        bar.add(self._volume_slider)
        bar.add_spacing(Space.MD)

        self._osd_button = IconButton("info", "Latency overlay (L)")
        self._osd_button.setCheckable(True)
        self._osd_button.setChecked(True)
        self._osd_button.clicked.connect(self._on_osd_clicked)
        bar.add(self._osd_button)

        self._fullscreen_button = IconButton("fullscreen", "Fullscreen (F11)")
        self._fullscreen_button.clicked.connect(self.toggle_fullscreen)
        bar.add(self._fullscreen_button)
        bar.add_spacing(Space.SM)

        self._bar_latency = QLabel("--")
        self._bar_latency.setProperty("role", "meta")
        font = self._bar_latency.font()
        font.setFamilies(list(Type.FAMILIES_MONO))
        self._bar_latency.setFont(font)
        self._bar_latency.setToolTip(
            "Controller round trip. The Bluetooth hop to the console adds a "
            "further 5-15 ms that cannot be measured from here."
        )
        bar.add(self._bar_latency)

    def _build_connection_group(self) -> QGroupBox:
        self._connection = ConnectionPanel(self)
        return self._connection

    def _build_audio_controls(self) -> None:
        """Mute and volume for the stream's audio.

        Built here, placed by `_build_control_bar` -- the bar is created after
        the drawer, so these have to exist by then. They are not put in a
        layout at this point; the bar takes them.
        """
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
        self._controllers = ControllersPanel(self)
        return self._controllers

    def _build_latency_group(self) -> QGroupBox:
        self._latency = LatencyPanel(MAX_CONTROLLERS, _latency_style)
        return self._latency

    # -- config ------------------------------------------------------------

    def _load_config_into_ui(self) -> None:
        cfg = self._config

        # Seeding a widget emits its change signal, and those handlers write the
        # UI back into the config. During load the UI is only half-populated, so
        # letting them run overwrites saved settings with defaults -- the
        # per-slot rumble flags in particular.
        guarded = [
            self._controllers.rumble,
            self._volume_slider,
            self._mute_button,
            *self._controllers.rumble_boxes,
            *self._controllers.config_combos,
            *self._controllers.type_combos,
        ]
        for widget in guarded:
            widget.blockSignals(True)

        # "auto" was removed; an older config may still name it. Direct is the
        # closest equivalent and the overwhelmingly common case.
        mode = "direct" if cfg.mode == "auto" else cfg.mode
        index = self._connection.mode.findData(mode)
        self._connection.mode.setCurrentIndex(index if index >= 0 else 0)

        self._connection.host.setText(cfg.host)
        self._connection.port.setValue(cfg.port)
        self._connection.room.setText(cfg.room_code)
        self._connection.broker.setText(
            f"{cfg.broker_host}:{cfg.broker_port}" if cfg.broker_host else ""
        )
        self._connection.password.setText(cfg.password)
        self._connection.save_password.setChecked(cfg.save_password)
        self._controllers.rumble.setChecked(cfg.rumble_enabled)
        self._connection.client_name.setText(cfg.client_name)
        self._volume_slider.setValue(cfg.video_volume)
        self._mute_button.setChecked(cfg.video_muted)
        self._set_drawer_open(cfg.controls_open)
        self._update_mute_icon()

        for row in range(MAX_CONTROLLERS):
            entry = cfg.controller(row)
            self._controllers.enable_boxes[row].setChecked(entry.enabled)
            self._controllers.username_edits[row].setText(entry.username)
            self._controllers.rumble_boxes[row].setChecked(entry.rumble_enabled)

        self._refresh_configuration_combos()

        for widget in guarded:
            widget.blockSignals(False)

        self._on_mode_changed()

    def _save_ui_into_config(self) -> None:
        if self._loading:
            return

        cfg = self._config

        cfg.mode = self._connection.mode.currentData()
        cfg.host = self._connection.host.text().strip()
        cfg.port = self._connection.port.value()
        cfg.room_code = self._connection.room.text().strip()
        cfg.password = self._connection.password.text()
        cfg.save_password = self._connection.save_password.isChecked()
        cfg.rumble_enabled = self._controllers.rumble.isChecked()
        cfg.client_name = self._connection.client_name.text().strip() or cfg.client_name

        broker = self._connection.broker.text().strip()
        if broker:
            host, _, port = broker.partition(":")
            cfg.broker_host = host
            if port.isdigit():
                cfg.broker_port = int(port)

        for row in range(MAX_CONTROLLERS):
            entry = cfg.controller(row)
            entry.enabled = self._controllers.enable_boxes[row].isChecked()
            entry.username = self._controllers.username_edits[row].text().strip()

            entry.rumble_enabled = self._controllers.rumble_boxes[row].isChecked()
            entry.configuration = self._controllers.config_combos[row].currentData() or ""
            entry.layout = self._controllers.type_combos[row].currentData() or ""

            combo = self._controllers.device_combos[row]
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

        for row, combo in enumerate(self._controllers.device_combos):
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

        device = self._controllers.device_combos[row].currentData()
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
        device = self._controllers.device_combos[row].currentData()

        if device is None:
            box = self._controllers.enable_boxes[row]
            box.blockSignals(True)
            box.setChecked(False)
            box.blockSignals(False)

        self._update_slot_availability()
        self._refresh_configuration_combos()
        self._save_ui_into_config()

    def _on_configuration_changed(self, row: int) -> None:
        name = self._controllers.config_combos[row].currentData()
        self._config.controller(row).configuration = name or ""
        # A different configuration has a different set of configured types, so
        # the type list has to follow.
        self._refresh_type_combos()
        self._apply_saved_mappings()
        self._save_ui_into_config()

    def _on_type_changed(self, row: int) -> None:
        key = self._controllers.type_combos[row].currentData()
        self._config.controller(row).layout = key or ""
        self._apply_saved_mappings()
        self._save_ui_into_config()

    def _refresh_configuration_combos(self) -> None:
        """Rebuild each slot's configuration list for the pad it is using."""
        for row, combo in enumerate(self._controllers.config_combos):
            device = self._controllers.device_combos[row].currentData()
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
        for row, combo in enumerate(self._controllers.type_combos):
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
        for row, combo in enumerate(self._controllers.device_combos):
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

        self._controllers.capture_hint.setText(
            "Capturing — typing goes to the controller"
            if checked
            else "Keys type normally"
        )

    def _owns_focus(self) -> bool:
        """True while our window is the active one.

        There used to be two -- the picture had a window of its own, and
        checking only this one silently killed keyboard capture the moment the
        stream was opened. The picture is part of this window now, so there is
        one thing to ask.
        """
        return self.isActiveWindow()

    def eventFilter(self, obj, event):  # noqa: N802 - Qt override
        """Route keystrokes to the keyboard controller while capture is armed."""
        from PySide6.QtCore import QEvent

        if not self._controllers.capture.isChecked():
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
                self._controllers.capture.setChecked(False)
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
        for row, box in enumerate(self._controllers.enable_boxes):
            if box.isChecked():
                data = self._controllers.device_combos[row].currentData()
                if data is not None:
                    return data
        for combo in self._controllers.device_combos:
            data = combo.currentData()
            if data is not None:
                return data
        return self._devices[0]

    # -- connection --------------------------------------------------------

    def _on_mode_changed(self) -> None:
        mode = self._connection.mode.currentData()
        # Hide the whole form row, label included. Hiding only the field leaves
        # an orphaned "Rendezvous:" label sitting against blank space.
        # Both broker modes want the same two fields, and both address modes
        # want the host row -- the transport differs, the settings do not.
        self._set_row_visible(self._connection.host_row, mode in ("direct", "tunnel"))
        self._set_row_visible(self._connection.punch_row, mode in ("punch", "relay"))

        # The list only ever holds results for one transport, so switching
        # invalidates it -- and immediately repopulates it, since an empty list
        # after switching reads as "no servers" rather than "not looked yet".
        self._populate_server_list([], mode)
        if not self._loading:
            QTimer.singleShot(0, self._on_discover)

    def _set_row_visible(self, field: QWidget, visible: bool) -> None:
        """Show or hide a QFormLayout row and its label together."""
        setter = getattr(self._connection.form, "setRowVisible", None)
        if setter is not None:
            setter(field, visible)
            return

        # Qt < 6.4 has no setRowVisible; fall back to the label lookup.
        field.setVisible(visible)
        label = self._connection.form.labelForField(field)
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
        mode = self._connection.mode.currentData()
        self._connection.search_button.setEnabled(False)
        self._set_status(
            "Asking the broker..." if self._uses_broker(mode)
            else "Searching this network..."
        )
        QApplication.processEvents()

        try:
            servers = self._find_servers(mode)
        finally:
            self._connection.search_button.setEnabled(True)

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
        self._connection.server_list.blockSignals(True)
        self._connection.server_list.clear()

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
            self._connection.server_list.addItem(label, data)

        # Always present, and the only option for a server set to hidden.
        self._connection.server_list.addItem("Custom — enter details below", self.CUSTOM_SERVER)
        self._connection.server_list.setCurrentIndex(self._preferred_server_index(servers, mode))
        self._connection.server_list.blockSignals(False)

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
        custom_index = self._connection.server_list.count() - 1
        by_room = self._uses_broker(mode)

        configured = (
            self._connection.room.text().strip() if by_room else self._connection.host.text().strip()
        )

        if configured:
            for index in range(custom_index):
                data = self._connection.server_list.itemData(index)
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
        data = self._connection.server_list.currentData()
        custom = data is None or data == self.CUSTOM_SERVER

        # Details stay editable on Custom and become read-only for a discovered
        # server, so it is obvious which one is in effect.
        for widget in (self._connection.host, self._connection.room):
            widget.setReadOnly(not custom)
        self._connection.port.setReadOnly(not custom)

        if custom:
            return

        if data.get("kind") == "direct":
            self._connection.host.setText(str(data.get("host", "")))
            self._connection.port.setValue(int(data.get("port") or self._connection.port.value()))
        else:
            self._connection.room.setText(str(data.get("room", "")))

    def _broker_fields(self) -> tuple[str, int]:
        """Broker host and port from the connection form, or the config."""
        text = self._connection.broker.text().strip() if hasattr(self, "_broker") else ""
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

        self._connection.connect_button.setEnabled(False)
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
            self._connection.connect_button.setEnabled(True)
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
            self._connection.connect_button.setEnabled(True)
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

        self._latency.plot.reset()
        self._connection.connect_button.setText("Disconnect")
        self._connection.connect_button.setEnabled(True)
        mode = result.mode if result else "direct"
        self._set_status(
            f"Connected ({mode}) — streaming {len(slots)} controller(s)"
        )

    def _build_slots(self, capacity: int) -> list[SlotRuntime]:
        slots: list[SlotRuntime] = []

        for row in range(MAX_CONTROLLERS):
            if not self._controllers.enable_boxes[row].isChecked():
                continue
            if capacity and row >= capacity:
                continue

            device = self._controllers.device_combos[row].currentData()
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
                    username=self._controllers.username_edits[row].text().strip() or f"Player {row + 1}",
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

        self._connection.connect_button.setText("Connect")
        self._set_status("Disconnected")

        for label in self._latency.cards:
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
        surface, self._video_surface = self._video_surface, None
        self._stage.set_surface(None)
        if surface is not None:
            surface.release()
            surface.deleteLater()
        # The stream is going away, not being refused: a fresh one (a retry, a
        # reconnect, a new source) should show itself as usual.
        self._video_dismissed = False

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
        self._connection.video_button.setEnabled(False)
        self._connection.video_button.setText("Watch stream")

    def _on_watch_clicked(self) -> None:
        """Show or hide the picture."""
        if self._video_surface is not None:
            self._hide_video()
            return

        if self._video_receiver is None:
            self._start_video()
        if self._video_decoder is None or self._video_receiver is None:
            if self._video_unavailable:
                QMessageBox.information(self, "Video unavailable", self._video_unavailable)
            return

        self._show_video()

    def _show_video(self) -> None:
        """Put the picture on the stage.

        The surface is a child of the stage rather than a window of its own,
        so there is nothing to raise, focus or close -- and no second window
        that can end up behind this one, which is what "the stream disappeared"
        usually meant.
        """
        if self._video_surface is not None:
            return
        # Asking for it counts as un-dismissing it, however we got here.
        self._video_dismissed = False
        from client.gui.video_window import VideoWindow

        surface = VideoWindow(self._video_decoder, self._video_receiver, self._stage)
        surface.volume_nudged.connect(self.adjust_volume)
        surface.mute_toggled.connect(self.toggle_mute)
        # Embedded, the surface cannot take itself fullscreen -- it is a child
        # in a layout -- so it asks and the window does it for the whole shell.
        surface.fullscreen_requested.connect(self.toggle_fullscreen)
        self._stage.set_surface(surface)
        self._video_surface = surface
        self._connection.video_button.setText("Hide video")
        if self._config.video_fullscreen and not self.isFullScreen():
            self.toggle_fullscreen()

    def _hide_video(self) -> None:
        """Take the picture off the stage, and remember that it was asked for.

        `release()` matters here in a way `close()` used to cover: nothing
        closes an embedded widget, so without it the decoder keeps a callback
        into a surface nobody is showing and goes on scaling every frame to a
        viewport that is no longer visible.
        """
        surface, self._video_surface = self._video_surface, None
        self._stage.set_surface(None)
        if surface is not None:
            surface.release()
            surface.deleteLater()
        self._video_dismissed = True
        self._connection.video_button.setText("Watch stream")

    def _tick_video(self) -> None:
        """Drive the video side once per GUI tick. Called from ``_tick``."""
        source = self._pending_video_source()
        available = bool(source and source.get("available"))

        if not available:
            if self._video_receiver is not None:
                self._stop_video()
            else:
                self._connection.video_button.setEnabled(False)
                # Ask again now and then. The server pushes an advert when
                # things change, but that direction has no retransmit -- and
                # the common case is a client that connected while still
                # awaiting approval, whose one answer was "no video".
                self._maybe_requery_video()
            return

        self._connection.video_button.setEnabled(True)

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
            and self._video_surface is None
            and not self._video_dismissed
        ):
            # Appears once when the picture becomes available, but never again
            # after the player hid it -- this runs every tick, so without the
            # flag it came straight back and could not be got rid of.
            if self._config.video_enabled:
                self._show_video()

        surface = self._video_surface
        if surface is not None:
            surface.set_controller_rtt(self._best_controller_rtt())

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
        enabled = self._controllers.rumble.isChecked()
        self._config.rumble_enabled = enabled

        slots = {}
        for row in range(MAX_CONTROLLERS):
            on = self._controllers.rumble_boxes[row].isChecked()
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

        for row, edit in enumerate(self._controllers.username_edits):
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
            has_device = self._controllers.device_combos[row].currentData() is not None
            within_capacity = capacity == 0 or row < capacity
            usable = within_capacity and has_device

            if not usable and self._controllers.enable_boxes[row].isChecked():
                self._controllers.enable_boxes[row].setChecked(False)

            # Choosing a controller must stay possible as long as the slot
            # exists at all.
            self._controllers.device_combos[row].setEnabled(within_capacity)
            self._controllers.config_combos[row].setEnabled(within_capacity)
            self._controllers.type_combos[row].setEnabled(within_capacity)
            self._controllers.username_edits[row].setEnabled(within_capacity)
            self._controllers.rumble_boxes[row].setEnabled(within_capacity)
            self._controllers.enable_boxes[row].setEnabled(usable)

            item = self._controllers.table.item(row, COL_STATUS)
            if not within_capacity:
                tip = (
                    f"The server has only {capacity} Bluetooth adapter"
                    f"{'' if capacity == 1 else 's'}, so this slot cannot be used."
                )
                self._controllers.enable_boxes[row].setToolTip(tip)
                if item:
                    item.setText("unavailable")
            elif not has_device:
                self._controllers.enable_boxes[row].setToolTip(
                    "Pick a controller for this slot first."
                )
                if item and item.text() in ("unavailable", "—"):
                    item.setText("no controller")
            else:
                self._controllers.enable_boxes[row].setToolTip("")
                if item and item.text() in ("unavailable", "no controller"):
                    item.setText("—")

        if capacity:
            self._controllers.capacity_label.setText(f"Server capacity: {capacity} controller(s)")
        else:
            self._controllers.capacity_label.setText("")

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
        for row, combo in enumerate(self._controllers.device_combos):
            device = combo.currentData()
            if device is not None and not _is_shareable(device):
                claimed[device.guid] = row

        for row, combo in enumerate(self._controllers.device_combos):
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
            label = self._latency.cards[row]
            stats = latency.get(row)
            entry = loop_slots.get(row)

            if entry is None:
                label.setText(f"Slot {row}\n—")
                label.setStyleSheet(_latency_style(None))
                continue

            item = self._controllers.table.item(row, COL_STATUS)
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
            self._latency.plot.add_sample(row, rtt["last"])

        self._latency.plot.refresh()
        self._tick_video()
        self._tick_shell()

    def _tick_shell(self) -> None:
        """Refresh the header badge and the bar's readout.

        Both are guarded writes: this runs ten times a second, and a `QLabel`
        set to the text it already holds still costs a relayout.
        """
        self._header.status.set_status(
            self._connection_status(), self.statusBar().currentMessage()
        )
        rtt = self._best_controller_rtt()
        text = f"{rtt:.0f} ms" if rtt > 0 else "--"
        if self._bar_latency.text() != text:
            self._bar_latency.setText(text)
            self._bar_latency.setStyleSheet(
                f"color: {qcolor(_latency_token(rtt if rtt > 0 else None)).name()};"
            )

    # -- shell ------------------------------------------------------------

    def _on_drawer_clicked(self) -> None:
        self._set_drawer_open(not self._drawer.is_open())

    def _set_drawer_open(self, opened: bool) -> None:
        self._drawer.set_open(opened)
        self._drawer_button.setChecked(not opened)
        self._drawer_button.setToolTip(
            "Hide the controls" if opened else "Show the controls"
        )
        if not self._loading:
            self._config.controls_open = bool(opened)

    def _on_osd_clicked(self) -> None:
        surface = self._video_surface
        if surface is not None:
            surface.toggle_osd()

    def toggle_fullscreen(self) -> None:
        """Fullscreen the whole shell, not a window of its own.

        The picture is a child widget now, so it cannot go fullscreen by
        itself -- and it should not: taking the window fullscreen and hiding
        the chrome leaves exactly the picture, which is what was wanted, with
        no second window to lose behind this one.
        """
        if self.isFullScreen():
            self.showNormal()
            self._header.show()
            self._set_drawer_open(self._drawer_was_open)
            self._fullscreen_button.set_icon_name("fullscreen")
            self._fullscreen_button.setToolTip("Fullscreen (F11)")
            return

        self._drawer_was_open = self._drawer.is_open()
        self._header.hide()
        self._drawer.set_open(False)
        self._drawer_button.setChecked(True)
        self._fullscreen_button.set_icon_name("fullscreen-exit")
        self._fullscreen_button.setToolTip("Leave fullscreen (Esc)")
        self.showFullScreen()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        # Esc leaves fullscreen. Keyboard capture gets Esc first, through the
        # application-level filter, so its documented "press Esc to release"
        # still wins -- a player who has armed capture presses Esc twice.
        if event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.toggle_fullscreen()
            return
        super().keyPressEvent(event)

    def _set_status(self, text: str) -> None:
        """Update the status bar and the header badge together.

        The badge's *state* comes from the transport rather than from this
        text: matching free-form sentences against an enum would put the two
        one wording change away from disagreeing, and the badge is the thing
        someone glances at.
        """
        self.statusBar().showMessage(text)
        self._header.status.set_status(self._connection_status(), text)

    def _connection_status(self) -> Status:
        """What the header badge should read, from the transport's own state."""
        transport = self._transport
        if transport is None:
            return Status.IDLE
        state = transport.state
        if state is ConnectionState.CONNECTED:
            # "Streaming" is the honest word once a picture is actually
            # arriving; connected-but-no-video is a different situation and
            # saying so saves the player looking for a fault.
            return Status.STREAMING if self._stage.has_surface() else Status.CONNECTED
        if state in (ConnectionState.RESOLVING, ConnectionState.HANDSHAKING):
            return Status.CONNECTING
        if state is ConnectionState.FAILED:
            return Status.ERROR
        return Status.IDLE

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._save_ui_into_config()
        self._disconnect()
        if self._backend is not None:
            self._backend.close()
        super().closeEvent(event)


#: Round-trip thresholds, in milliseconds. Chosen against what is actually
#: achievable rather than against a wish: Bluetooth alone costs 5-15 ms, so a
#: "good" reading here is not a small number in the abstract.
LATENCY_GOOD_MS = 25.0
LATENCY_FAIR_MS = 60.0


def _latency_token(p50: float | None) -> str:
    """The status token for a round-trip reading, or the idle one."""
    if p50 is None:
        return "text-muted"
    if p50 < LATENCY_GOOD_MS:
        return "success"
    if p50 < LATENCY_FAIR_MS:
        return "warning"
    return "error"


def _latency_style(p50: float | None) -> str:
    """Colour by what is actually achievable -- Bluetooth alone costs 5-15 ms.

    The tinted background is the status colour at low alpha over the card
    surface, resolved here rather than written as a fourth set of hand-picked
    hex values: the previous `#14301f` / `#322613` / `#33191b` were eyeballed
    against the greens and ambers they sit beside and drifted from them.
    """
    token = _latency_token(p50)
    colour = qcolor(token)
    if token == "text-muted":
        tint = qcolor("surface-solid-raised")
    else:
        tint = qcolor(token, alpha=0.14, over="surface-solid")
    return (
        f"background: {tint.name()}; color: {colour.name()}; "
        f"border-radius: {Radius.CONTROL}px; padding: {Space.SM}px; "
        f"border: 1px solid {qcolor('border-subtle', over='surface-solid').name()};"
    )


def _wrap(layout) -> QWidget:
    widget = QWidget()
    widget.setLayout(layout)
    layout.setContentsMargins(0, 0, 0, 0)
    return widget


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
    # Fusion plus the product stylesheet. `apply_theme` sets the style itself,
    # because a QSS built against Fusion's metrics renders wrong on the native
    # Windows style -- the two disagree about what a control's padding means.
    apply_theme(app)
    # Set on the application as well as the window: Windows takes the taskbar
    # icon from the application, the title bar from the window.
    app.setWindowIcon(app_icon())

    _set_windows_app_id()

    window = MainWindow(config)
    window.show()

    return app.exec()

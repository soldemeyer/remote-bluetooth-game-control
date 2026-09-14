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

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QActionGroup, QFont
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
    QPushButton,
    QSlider,
    QSpinBox,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QMenu,
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
    PlayersPanel,
)
from client.net.connect import connect as connect_to_server
from qtui.widgets import fit_combo_popup
from client.input import InputBackendError, create_backend
from client.input.mapping import DeviceMapping
from client.loop import InputLoop, SlotRuntime
from client.net.transport import ClientTransport, ConnectionState, TransportError
from common.protocol import ControlOp
from common.design.tokens import Radius, Space, Type
from client.gui.shell import Drawer, HeaderBar, VideoStage
from common.design.themes import LABELS as THEME_LABELS
from common.design.themes import active_theme, theme_names
from qtui.backdrop import BackdropWidget
from qtui.buttons import IconButton
from qtui.status import Status
from qtui.theme import apply_theme, qcolor
from qtui.feedback import Notice

log = logging.getLogger(__name__)

#: GUI refresh rate. Fast enough to feel live, slow enough to stay cheap.
UI_INTERVAL_MS = 100

#: How long to wait before retrying a video stream that failed while the
#: server still says a source exists. Long enough not to hammer a source that
#: is restarting, short enough that a player does not sit staring at a frozen
#: window wondering whether to reconnect by hand.
_VIDEO_RETRY_S = 5.0

MAX_CONTROLLERS = client_config.MAX_CONTROLLERS

# The controller table's columns are imported from the panel that builds it,
# never restated here. This module used to carry its own copy of all nine --
# *after* importing one of them, so the copy silently shadowed the import. They
# agreed, so it worked, and it is the same two-vocabularies trap that has bitten
# the adapter cards and the region names: the copies only have to disagree once,
# and a stale index addresses a cell widget instead of an item, where writing
# text does nothing and reports no error.


#: How long after the last keystroke a player name is pushed to the server.
#: Long enough that typing a name is one message, short enough that nobody
#: watching the server's card thinks nothing happened.
USERNAME_PUSH_MS = 450


def _default_window_size():
    """A window sized to the screen it opens on.

    The picture is the point of this window, and the drawer beside it is a
    fixed 644px -- so a small default spends most of the width on controls and
    leaves a stamp for the game. A fixed large default is no better: it is
    either bigger than somebody's laptop screen or smaller than their monitor.

    Most of the available area, which leaves the taskbar and a sliver of the
    desktop showing so the window still reads as a window rather than as a
    failed fullscreen, and is capped so it does not become unwieldy on a very
    large display.
    """
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QGuiApplication

    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return QSize(1600, 1000)

    available = screen.availableGeometry()
    return QSize(
        max(1020, min(int(available.width() * 0.95), 2400)),
        max(820, min(int(available.height() * 0.95), 1500)),
    )


def theme_needs_applying(name: str, app) -> bool:
    """Whether the application stylesheet has to be rebuilt for `name`.

    Extracted so it can be tested without touching Qt. Setting an application
    stylesheet re-polishes every widget that exists, so a test that exercises
    this through the real `apply_theme` measures how many widgets the session
    has accumulated rather than this decision -- two such tests cost 717s and
    352s of a 1221s run before they were replaced by the ones below it.
    """
    if name != active_theme():
        return True
    # Nothing themed yet: a window built outside `run()` still has to style
    # itself, or it comes up as bare Fusion.
    return not (app is not None and app.styleSheet())


class MainWindow(QMainWindow):
    #: The video capability scan finished.
    #:
    #: A signal rather than a direct call because the scan runs on a worker
    #: thread -- it creates a graphics device and decodes a test stream, which
    #: is a visible stall on the GUI thread. Qt sees the emitter is not this
    #: object's thread and queues the delivery, which is the documented way in.
    capabilities_ready = Signal()

    def __init__(self, config: client_config.ClientConfig) -> None:
        super().__init__()
        self.capabilities_ready.connect(self._on_capabilities_ready)
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

        #: Crops the server says this client owns, straight off the wire.
        #: Empty is the ordinary state and means the whole picture.
        self._video_regions: list = []
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
        #: What the status bar said before the video stream borrowed it, and
        #: the stream state that borrowed it. "Connecting to the video
        #: stream..." was set once and never taken back, so it sat under a
        #: perfectly good picture for the rest of the session -- and read as
        #: the stream being stuck, which is exactly what somebody chasing an
        #: unrelated fault does not need to see.
        self._status_before_video = ""
        self._video_status_state: object | None = None
        #: Last render path reported to the panel, as a code, so a steady
        #: stream writes to the label once rather than ten times a second.
        self._last_reported_path = 0
        self._video_unavailable = ""

        #: True while the window is being built and populated. Seeding a
        #: widget emits its change signal, and those handlers write the UI
        #: back to disk -- during construction the UI is not yet populated,
        #: so that would overwrite saved settings with blanks.
        self._loading = True

        #: Set once the first-run defaults have been considered, whether or not
        #: they applied. `_refresh_devices` runs again whenever a pad is
        #: plugged in, and without this a player who unticked slot 1 would have
        #: it ticked back the next time they touched a USB port.
        self._first_run_defaults_done = False

        self.setWindowTitle("Remote Bluetooth Game Control")
        self.setWindowIcon(app_icon())
        self.resize(_default_window_size())
        self._centre_on_screen()

        #: Coalesces a burst of keystrokes in a player-name field into one
        #: push. See `_on_username_typed` -- the name has to reach the server
        #: while the field still has focus, and per-keystroke would be seven
        #: control messages for a seven-letter name.
        self._username_push = QTimer(self)
        self._username_push.setSingleShot(True)
        self._username_push.setInterval(USERNAME_PUSH_MS)
        self._username_push.timeout.connect(self._on_username_changed)

        self._build_ui()
        self._refresh_devices()
        self._apply_theme(self._config.theme)
        self._load_config_into_ui()
        self._loading = False

        # **After the config is loaded, not during `_refresh_devices`.** That
        # runs before the load, so a tick set there was overwritten moments
        # later by `enabled=False` from a config nobody had touched -- and the
        # tick never reached the config either, because saving is suppressed
        # while loading.
        self._apply_first_run_defaults()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(UI_INTERVAL_MS)

        # Populate the server list without blocking the first paint: discovery
        # waits over a second for replies, and doing that inside __init__ would
        # show the user an empty frozen window.
        QTimer.singleShot(150, self._on_discover)

    # -- construction ------------------------------------------------------

    def _centre_on_screen(self) -> None:
        """Open in the middle of the screen rather than wherever Qt decides.

        A window sized from the screen is large, and Qt's default placement
        puts it at the top left -- so the right edge and the drawer with it can
        land off a smaller display.
        """
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        frame = self.frameGeometry()
        frame.moveCenter(screen.availableGeometry().center())
        self.move(frame.topLeft())

    def _build_ui(self) -> None:
        """Video-first: the picture is the window, the controls sit beside it.

        The three groups are exactly the ones that were here before and are
        built by exactly the same methods -- only where they live has changed.
        """
        # The backdrop is the central widget, so every panel above is
        # composited over real colour rather than over a flat fill. Glass with
        # nothing behind it is just a lighter rectangle.
        central = BackdropWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._header = HeaderBar("Remote Bluetooth Game Control")
        self._theme_button = IconButton("droplet", "Colour scheme")
        self._theme_button.setMenu(self._build_theme_menu())
        self._header.add_action(self._theme_button)
        root.addWidget(self._header)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._stage = VideoStage()
        body.addWidget(self._stage, 1)

        self._drawer = Drawer()
        # **Controllers first.** A controller has to be chosen before there is
        # anything worth connecting, and the drawer is read top to bottom.
        sections = self._config.drawer_sections
        for key, build in (
            ("players", self._build_players_group),
            ("controllers", self._build_controller_group),
            ("connection", self._build_connection_group),
            ("video", self._build_video_group),
            ("latency", self._build_latency_group),
        ):
            card = self._drawer.add_card(
                key, build(), opened=sections.get(key, True)
            )
            card.toggled.connect(self._on_section_toggled)
        self._drawer.add_stretch()
        body.addWidget(self._drawer)

        # **After the drawer**, because the header sits in front of controls
        # the panels build and `add_action` appends in order. Left to right:
        # theme, then the two session actions, then the drawer toggle.
        self._build_header_actions()
        root.addLayout(body, 1)

        self._build_control_bar()

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self._set_status("Not connected")

    def _build_header_actions(self) -> None:
        """Connect and Watch video, beside the theme picker.

        Both act on the session rather than on any one card, and both are
        wanted *while playing* -- when the drawer is usually shut. Putting them
        in the Connection card meant folding away the address fields, which are
        set once, also folded away the button that uses them.
        """
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self._on_connect_clicked)
        self.connect_button.setDefault(True)
        self.connect_button.setToolTip(
            "Connect to the server set up in the Connection card."
        )
        self._header.add_action(self.connect_button)

        # Enabled only once the server says a source exists, so the button
        # never offers something that cannot happen.
        self.video_button = QPushButton("Watch stream")
        self.video_button.setEnabled(False)
        self.video_button.setToolTip(
            "Open the video stream. F11 for fullscreen, L for the latency overlay."
        )
        self.video_button.clicked.connect(self._on_watch_clicked)
        self._header.add_action(self.video_button)

        self._drawer_button = IconButton("menu", "Show or hide the controls")
        self._drawer_button.setCheckable(True)
        self._drawer_button.clicked.connect(self._on_drawer_clicked)
        self._header.add_action(self._drawer_button)

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

    def _on_section_toggled(self, key: str, opened: bool) -> None:
        """Remember which drawer cards the player left open.

        Written straight to the config rather than through
        `_save_ui_into_config`: that reads every control in the window, and a
        card being folded does not change one of them.
        """
        self._config.drawer_sections[key] = bool(opened)
        if not self._loading:
            client_config.save(self._config)

    def _build_players_group(self) -> QGroupBox:
        self._players = PlayersPanel(self)
        return self._players

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

    def _build_video_group(self) -> QGroupBox:
        from client.gui.panels import VideoPanel

        self._video_panel = VideoPanel(self)
        # The scan creates a graphics device and decodes a test stream --
        # 50-150 ms, which is a visible stall if it runs here. The panel shows
        # "Detecting..." until the answer arrives.
        self._start_capability_scan()
        return self._video_panel

    def _start_capability_scan(self) -> None:
        """Work out what this machine can do, off the GUI thread.

        One shot. The answer cannot change without new hardware or a driver
        change and a restart, so there is nothing to poll and nothing to
        refresh.
        """
        import threading

        from client.media import upscale

        # Already answered -- by an earlier window in this process, or by
        # anything that asked first. No thread, no delay, and in a test suite
        # that builds hundreds of windows, no hundreds of threads.
        if upscale.cached() is not None:
            self._on_capabilities_ready()
            return

        def scan() -> None:
            try:
                upscale.capabilities()
            except Exception:  # noqa: BLE001
                log.debug("The video capability scan failed", exc_info=True)
                return
            try:
                # Back to the GUI thread. A queued signal is the documented
                # way in, and `capabilities` is cached so the second call on
                # the other side is free.
                self.capabilities_ready.emit()
            except RuntimeError:
                # The window was closed while the scan ran. Its C++ object is
                # gone and there is nothing to tell.
                pass

        threading.Thread(target=scan, name="video-caps", daemon=True).start()

    def _on_capabilities_ready(self) -> None:
        from client.media.upscale import capabilities, effective_mode

        caps = capabilities()
        for line in caps.describe():
            log.info("%s", line)

        panel = self._video_panel
        panel.apply_capabilities(caps)

        # The saved preference is honoured when the hardware can, and left
        # alone when it cannot -- so moving the client to another machine and
        # back does not lose it.
        wanted = self._config.video_upscaler
        panel.blockSignals(True)
        try:
            panel.select(effective_mode(wanted, caps))
            index = panel.hw_decode.findData(self._config.video_hw_decode)
            if index >= 0 and caps.hw_decode_ok:
                panel.hw_decode.setCurrentIndex(index)
        finally:
            panel.blockSignals(False)
        panel.sync_sharpness_enabled()
        self._apply_video_settings()

    # -- video enhancement -------------------------------------------------

    def _on_upscaler_changed(self, checked: bool) -> None:
        if self._loading or not checked:
            return
        self._config.video_upscaler = self._video_panel.selected_mode()
        self._video_panel.sync_sharpness_enabled()
        self._apply_video_settings()
        self._save_ui_into_config()

    def _on_hw_decode_changed(self, _index: int) -> None:
        if self._loading:
            return
        self._config.video_hw_decode = self._video_panel.hw_decode.currentData() or "off"
        self._apply_video_settings()
        self._save_ui_into_config()

    def _on_sharpness_changed(self, value: int) -> None:
        self._video_panel.sharpness_value.setText(f"{value}%")
        if self._loading:
            return
        self._config.video_fsr_sharpness = int(value)
        surface = self._video_surface
        if surface is not None and hasattr(surface, "set_sharpness"):
            surface.set_sharpness(int(value))
        self._save_ui_into_config()

    def _apply_video_settings(self) -> None:
        """Push the chosen mode at whatever is currently running.

        Called on every change and after the scan, and it is safe when there
        is no stream: with nothing to apply to, it does nothing and the
        settings take effect when the picture next appears.
        """
        from client.media.upscale import capabilities, effective_mode, hw_decode_device

        caps = capabilities()
        decoder = self._video_decoder
        if decoder is not None:
            decoder.set_hw_decode(hw_decode_device(self._config.video_hw_decode, caps))

        surface = self._video_surface
        if surface is None or not hasattr(surface, "attach_gpu"):
            return

        # A new mode is a new answer, so the label must be allowed to change.
        self._last_reported_path = 0

        mode = effective_mode(self._config.video_upscaler, caps)
        if mode == "off":
            surface.detach_gpu()
            self._video_panel.status.setText("")
            return

        ok, reason = surface.attach_gpu(
            mode, self._config.video_fsr_sharpness, self._backdrop_rgb())
        if not ok:
            # Never fatal. The stream keeps running on the software path, and
            # the player is told why rather than left with a setting that
            # appears to do nothing.
            log.warning("Could not start GPU video enhancement: %s", reason)
            surface.detach_gpu()
            self._video_panel.select("off")
            self._video_panel.status.setText(f"Upscaling is off: {reason}")

    def _on_gpu_failed(self, reason: str) -> None:
        """The renderer failed mid-stream and the decode thread dropped it.

        Puts the control back to Off, which is what is actually running. The
        alternative -- leave the selection alone because the preference is
        still the player's -- reads as the setting being on and doing nothing,
        and re-arms the same failure on the next reconnect.
        """
        log.warning("GPU video enhancement stopped: %s", reason)
        # Ordered: selecting Off cascades back through `_apply_video_settings`,
        # which clears the status line, so the reason is written afterwards.
        self._video_panel.select("off")
        self._video_panel.status.setText(f"Upscaling is off: {reason}")
        self._set_status("Video enhancement stopped — showing the plain picture")

    def _report_upscaler_path(self) -> None:
        """Say what the renderer is actually doing, not what was asked for.

        **"Nothing seems to happen" is a correct outcome here as often as it is
        a fault**, and the two are indistinguishable without this: super
        resolution is skipped whenever the output is no larger than the input,
        which is exactly the case when the video panel happens to be the
        stream's own size. The player selected something, the picture did not
        change, and nothing said why.

        The same failure this project records elsewhere as "untuned and fine
        are indistinguishable". It was in the OSD already, which is off by
        default and nowhere near the control being questioned.
        """
        decoder = self._video_decoder
        if decoder is None or self._config.video_upscaler == "off":
            return
        code = decoder.last_path_code
        if not code or code == self._last_reported_path:
            return
        self._last_reported_path = code
        path = decoder.last_path

        from client.media import videofx

        if code in (videofx.PATH_COPY, videofx.PATH_DOWNSCALE):
            self._video_panel.status.setText(
                f"Not enhancing: {path} — the picture is already at or above "
                "the stream's resolution. Enlarge the video panel to see a "
                "difference."
            )
            return

        cost = decoder.last_gpu_ms
        timing = f" · {cost:.2f} ms/frame on the GPU" if cost >= 0.0 else ""

        if code == videofx.PATH_VSR:
            # **"Running: RTX VSR (requested)" reads as a contradiction**, and
            # a player reasonably takes "requested" to mean it did not happen.
            # The caveat is real -- no API reports whether the driver applied
            # super resolution -- but stating it without the evidence
            # underclaims as badly as the opposite would overclaim, and that
            # was reported as confusion within a day.
            #
            # The GPU cost is the evidence and it is decisive: measured on an
            # RTX 5080, VSR is flat at 0.26-0.29 ms whatever the scale factor,
            # where Lanczos and FSR track output pixels (0.09 -> 0.35 ms from
            # 720p->1080p to 1080p->4K). A fixed cost independent of the work
            # is what a neural network looks like; a silent fallback to a
            # plain scale would track the others.
            self._video_panel.status.setText(
                f"Running: RTX Video Super Resolution{timing}. NVIDIA exposes "
                "no way to confirm the driver applied it — the GPU cost is "
                "the evidence, and it is far above a plain scale."
            )
        else:
            self._video_panel.status.setText(f"Running: {path}{timing}")

    def _backdrop_rgb(self) -> int:
        """The letterbox colour, as 0xRRGGBB.

        Pushed rather than baked into the renderer, because the theme is
        switchable while the client is running.
        """
        try:
            from qtui.theme import qcolor

            colour = qcolor("video-backdrop")
            return (colour.red() << 16) | (colour.green() << 8) | colour.blue()
        except Exception:  # noqa: BLE001
            return 0x000000

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
            self._video_panel.hw_decode,
            self._video_panel.sharpness,
            *(row.radio for row in self._video_panel.rows.values()),
            self._controllers.rumble,
            self._volume_slider,
            self._mute_button,
            *self._controllers.type_combos,
        ]
        for widget in guarded:
            widget.blockSignals(True)

        # The upscaler is seeded from the *saved* value even when the hardware
        # cannot honour it, because the preference has to survive being opened
        # on a machine that cannot. `_on_capabilities_ready` resolves it
        # against what this one can do, once it knows.
        self._video_panel.select(cfg.video_upscaler)
        self._video_panel.sharpness.setValue(cfg.video_fsr_sharpness)
        index = self._video_panel.hw_decode.findData(cfg.video_hw_decode)
        if index >= 0:
            self._video_panel.hw_decode.setCurrentIndex(index)

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
            self._players.username_edits[row].setText(entry.username)

        # The configuration and rumble are the Configure window's, and it
        # reads them from `cfg.controller(row)` when it opens. The controller
        # type is a column again, so it is seeded here.
        self._refresh_type_combos()

        for widget in guarded:
            widget.blockSignals(False)

        # The slider's label is not a signal handler's job here: its handler
        # was blocked above, so the text would keep whatever it was built with.
        self._video_panel.sharpness_value.setText(f"{cfg.video_fsr_sharpness}%")
        self._video_panel.sync_sharpness_enabled()

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
            entry.username = self._players.username_edits[row].text().strip()

            # `rumble_enabled` and `configuration` are deliberately **not**
            # read back from widgets: the Configure window writes them straight
            # into this entry and no cell holds a second answer. `layout` is a
            # column again, so it is read from it.
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
            Notice.warning(self, "No gamepad support", str(exc))
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

            # The popup must be measured against what is in it now, and this
            # list changes whenever a pad is plugged in or out.
            fit_combo_popup(combo)
            combo.blockSignals(False)

        self._update_slot_availability()

        # Only now do we know which device each slot holds.
        #
        # `_ensure_backend` pushes mappings too, but it runs at the *top* of
        # this method -- before the loop above has put anything in the device
        # combos. `_apply_saved_mappings` reads those combos to find each
        # slot's pad, so on the first pass every row read None, the
        # named-configuration loop skipped all of them, and nothing was
        # installed. The pad then produced nothing until the player opened the
        # mapping screen and pressed Save, which is the one other thing that
        # calls this.
        #
        # Reported exactly that way: "I have to go in and save the
        # configuration after opening the client for the controller to sense
        # button presses."
        #
        # Here rather than in `__init__` so a pad plugged in later, or a
        # "Refresh gamepad list", gets its configuration too.
        self._apply_saved_mappings()

    # -- controller configurations ----------------------------------------

    def _on_configure_slot(self, row: int) -> None:
        """Open the mapping screen for one slot's gamepad."""
        if not self._ensure_backend():
            return
        if not self._devices:
            self._refresh_devices()

        device = self._controllers.device_combos[row].currentData()
        if device is None:
            Notice.information(
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
            Notice.warning(self, "Controller unavailable", str(exc))
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
            self._backend, device, working, self, store=self._configurations,
            rumble=entry.rumble_enabled,
        )
        accepted = dialog.exec()

        # "Save as..." stores its copy immediately and carries on editing it, so
        # the copy has to be kept even when the dialog is then cancelled.
        if accepted or dialog.created_copy:
            saved = dialog.configuration
            self._configurations.upsert(saved)
            # All three of the slot's own settings come back from the dialog
            # now -- which configuration, which controller type, and rumble.
            # They were table columns; the window no longer has a widget
            # holding any of them, so this is where they are written.
            entry.configuration = saved.name
            entry.layout = saved.layout
            entry.rumble_enabled = dialog.rumble_enabled()
            self._config.preview_layout = saved.layout
            self._configurations.into_config(self._config)
            # The dialog can change the type as well as the bindings, so the
            # column has to follow it -- otherwise the table shows one type and
            # the slot uses another, with nothing to say which is real.
            self._refresh_type_combos()
            # Player-facing numbering, like the table's Slot column.
            self._set_status(f"Controller {row + 1} now uses '{saved.name}'")
            # Rumble and the controller type are both live settings: the server
            # is told without waiting for a reconnect, exactly as a player-name
            # edit is.
            self._push_slot_settings(row)

        # Either way, re-push what is actually stored: the dialog writes
        # bindings into the backend live while binding, including ones the
        # player then cancelled.
        self._apply_saved_mappings()
        self._save_ui_into_config()

        if borrowed:
            self._backend.release(device.instance_id)

    def _on_type_changed(self, row: int) -> None:
        """The controller type a slot's bindings are laid out for.

        Back in the table, where it sits beside the gamepad it describes. It
        stays *per slot* rather than on the configuration: slots reference
        configurations by name, so two slots sharing one used to fight over the
        setting -- changing one player's controller type silently changed
        another's.
        """
        key = self._controllers.type_combos[row].currentData()
        self._config.controller(row).layout = key or ""
        self._apply_saved_mappings()
        self._save_ui_into_config()
        # The server draws the pad a player is holding on its adapter card, so
        # a type change reaches the console without a reconnect.
        self._resync_slots()

    def _refresh_type_combos(self) -> None:
        """Select each slot's type, and mark the ones with no bindings yet.

        Every type stays selectable -- picking an empty one is how you start
        building it -- but an unconfigured one says so, rather than looking
        identical to a working one.
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
            # The item *texts* were just rewritten, and the marker is longer
            # than the name it is appended to -- so the popup has to be
            # re-measured here and not only where the list was built.
            fit_combo_popup(combo)
            combo.blockSignals(False)

    def _on_slot_device_changed(self, row: int) -> None:
        """React to a slot's gamepad changing.

        Two rules beyond refreshing the configuration list:

        * **A physical pad belongs to one slot.** Enforced by disabling that
          entry in every other slot's dropdown (see
          :meth:`_refresh_device_availability`) rather than by taking it away
          from whoever had it, which was startling.
        """
        # Choosing "None" deliberately leaves the tick alone: it is the
        # player's statement about which controllers are theirs, and taking it
        # away while they are still picking a pad is startling.
        self._update_slot_availability()
        self._save_ui_into_config()
        # A controller in play can be swapped for another without dropping the
        # session: the loop is re-pointed and the server is re-told, so the
        # console keeps the same adapter under a different pad.
        self._resync_slots()

    def _slot_layout(self, row: int) -> str:
        """Which controller type this slot uses, falling back sensibly."""
        entry = self._config.controller(row)
        if entry.layout:
            return entry.layout
        configuration = (
            self._configurations.get(entry.configuration) if entry.configuration else None
        )
        return configuration.layout if configuration is not None else LAYOUTS[0].key

    def _apply_first_run_defaults(self) -> None:
        """Tick the first controller and give it something to drive.

        Only on a config nobody has touched: the test is that no slot is
        enabled and none names a gamepad. A returning player who deliberately
        left everything off must get that back, so this cannot be "if slot 1
        is empty".

        A real pad in preference to the keyboard, because the keyboard is the
        fallback for having no pad at all -- and it is offered second here for
        the same reason it is offered at all.
        """
        if self._first_run_defaults_done:
            return
        self._first_run_defaults_done = True

        controllers = [self._config.controller(row) for row in range(MAX_CONTROLLERS)]
        if any(entry.enabled or entry.guid for entry in controllers):
            return

        combo = self._controllers.device_combos[0]
        best = None
        for index in range(combo.count()):
            device = combo.itemData(index)
            if device is None:
                continue
            if not _is_shareable(device):      # a real pad, not the keyboard
                best = index
                break
            if best is None:
                best = index                   # the keyboard, if nothing else

        if best is not None:
            combo.setCurrentIndex(best)
        self._controllers.enable_boxes[0].setChecked(True)
        self._update_slot_availability()
        self._save_ui_into_config()

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
            Notice.warning(self, "Cannot connect", "\n".join(f"• {p}" for p in problems))
            return

        if self._backend is None:
            self._refresh_devices()
            if self._backend is None:
                return

        self.connect_button.setEnabled(False)
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
            self.connect_button.setEnabled(True)
            self._set_status("Connection failed")
            Notice.critical(self, "Connection failed", str(exc))
            return

        self._transport = transport
        self._connect_result = result

        # Only when it was *unexpected*. Someone who selected relay mode chose
        # this path and knows the trade; telling them again on every connect
        # turns a real warning into a dialog to click past.
        if result.is_relayed and result.fell_back:
            Notice.information(
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
            self.connect_button.setEnabled(True)
            self._set_status("No controllers enabled")
            Notice.warning(
                self,
                "No controllers",
                "Enable at least one controller with a gamepad selected.",
            )
            return

        transport.queue_control(
            ControlOp.SET_CONTROLLERS, self._controllers_message(slots)
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
        # Which controllers are in play is settled for the session now, so the
        # table re-decides what may still be edited.
        self._update_slot_availability()
        self.connect_button.setText("Disconnect")
        self.connect_button.setEnabled(True)
        mode = result.mode if result else "direct"
        self._set_status(
            f"Connected ({mode}) — streaming {len(slots)} controller(s)"
        )

    def _controllers_message(self, slots) -> dict:
        """The SET_CONTROLLERS body for a set of slots.

        One builder for the connect path and for every live edit, so a field
        added for one cannot be missing from the other -- which is how the
        server would come to draw the wrong pad on a card after a change that
        looked like it had worked.
        """
        return {
            "client_name": self._config.client_name,
            "controllers": [
                {
                    "slot": s.slot,
                    "username": s.username,
                    "device_name": s.device_name,
                    # Additive: the server reads keys by name and ignores ones
                    # it does not know, so an older server simply drops this
                    # and an older client sends nothing.
                    "layout": s.layout,
                }
                for s in slots
            ],
        }

    def _resync_slots(self) -> None:
        """Re-describe the live controllers to the server, without reconnecting.

        A player name, a controller type, or a different gamepad: all of them
        reach the console while a session is running, because the alternative
        is telling somebody to disconnect everybody to rename themselves.

        **Which slots are in use is not part of this.** The Use column is
        locked while connected -- changing it would add or drop a controller,
        which the server allocates adapters for at handshake time -- so the set
        here is always the set that was sent on connect, with its details
        brought up to date.

        Devices are acquired, never released. The input loop polls on its own
        thread from a list this swaps under a lock, so a pad closed here could
        be closed between that thread reading its handle and using it. An
        unused pad left open costs nothing: it is simply not polled, and
        `acquire` hands the same handle back if a slot picks it up again.
        """
        transport = self._transport
        if transport is None or not transport.is_connected or self._loop is None:
            return

        slots = self._build_slots(transport.server_capacity)
        self._loop.set_slots(slots)
        transport.queue_control(ControlOp.SET_CONTROLLERS,
                                self._controllers_message(slots))

    def _push_slot_settings(self, row: int) -> None:
        """Send one slot's settings after its Configure window closed.

        Rumble has its own control op and its own server-side gate, so it is
        pushed separately from the controller description.
        """
        transport = self._transport
        if transport is None or not transport.is_connected:
            return

        self._resync_slots()
        transport.set_rumble_enabled(
            self._config.rumble_enabled,
            {
                slot: self._config.controller(slot).rumble_enabled
                for slot in range(MAX_CONTROLLERS)
            },
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
                    username=self._players.username_edits[row].text().strip() or f"Player {row + 1}",
                    device_name=acquired.display_name(),
                    layout=self._slot_layout(row),
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

        self.connect_button.setText("Connect")
        self._set_status("Disconnected")
        self._update_slot_availability()

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
        op = body.get("op")
        if op == ControlOp.VIDEO_REGIONS:
            # Which part of a split screen this client owns. Stored here and
            # applied from the GUI tick, like the advert, because the decoder
            # may not exist yet -- and it is re-applied on every start, so an
            # assignment that arrived before the stream did is not lost.
            with self._video_lock:
                self._video_regions = list(body.get("crops") or ())
            return
        if op != ControlOp.VIDEO_SOURCE:
            return
        with self._video_lock:
            self._video_source = dict(body)

    def _pending_video_source(self) -> dict | None:
        with self._video_lock:
            return dict(self._video_source) if self._video_source else None

    def _pending_video_regions(self) -> list:
        with self._video_lock:
            return list(self._video_regions)

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
        self._status_before_video = self.statusBar().currentMessage()
        self._video_status_state = None
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
        self._video_status_state = None
        self._last_reported_path = 0
        with self._video_lock:
            self._video_source = None
        self.video_button.setEnabled(False)
        self.video_button.setText("Watch stream")

    def _on_watch_clicked(self) -> None:
        """Show or hide the picture."""
        if self._video_surface is not None:
            self._hide_video()
            return

        if self._video_receiver is None:
            self._start_video()
        if self._video_decoder is None or self._video_receiver is None:
            if self._video_unavailable:
                Notice.information(self, "Video unavailable", self._video_unavailable)
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
        surface.gpu_failed.connect(self._on_gpu_failed)
        self._stage.set_surface(surface)
        self._video_surface = surface
        self.video_button.setText("Hide video")
        # The surface is the thing the settings apply *to*, so a preference
        # chosen before the picture existed -- which is the ordinary order,
        # since the panel is reachable from the moment the app opens -- only
        # takes effect here. Without this it silently did nothing until the
        # player touched the control a second time.
        self._apply_video_settings()
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
        self.video_button.setText("Watch stream")

    def _tick_video(self) -> None:
        """Drive the video side once per GUI tick. Called from ``_tick``."""
        source = self._pending_video_source()
        available = bool(source and source.get("available"))

        if not available:
            if self._video_receiver is not None:
                self._stop_video()
            else:
                self.video_button.setEnabled(False)
                # Ask again now and then. The server pushes an advert when
                # things change, but that direction has no retransmit -- and
                # the common case is a client that connected while still
                # awaiting approval, whose one answer was "no video".
                self._maybe_requery_video()
            return

        self.video_button.setEnabled(True)

        if self._video_receiver is None:
            if self._config.video_enabled:
                self._start_video()
            return

        from client.net.video import VideoStreamState

        state = self._video_receiver.state
        if state is not self._video_status_state:
            self._video_status_state = state
            if state is VideoStreamState.STREAMING:
                # Hand the bar back to whatever it was saying. The stream has
                # its own indicators from here on -- the window, the Watch
                # button, the OSD -- so restating it forever adds nothing.
                self._set_status(self._status_before_video or "Video streaming")
            elif state is VideoStreamState.FAILED:
                self._set_status("Video stream failed — retrying")

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

        # Applied every tick rather than only when the message arrives. The
        # decoder is rebuilt whenever the stream restarts, and it comes back
        # showing the whole picture -- so a client that had been cropped would
        # silently start seeing everybody else's screen after a reconnect.
        # `set_regions` returns immediately when nothing has changed.
        decoder = self._video_decoder
        if decoder is not None:
            decoder.set_regions(self._pending_video_regions())

        surface = self._video_surface
        if surface is not None:
            surface.set_controller_rtt(self._best_controller_rtt())
        self._report_upscaler_path()

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

        # The per-slot switches live in each slot's Configure window now, so
        # the config is the only place they are held. It is also where that
        # window writes them, which is why this reads rather than collects.
        slots = {
            row: self._config.controller(row).rumble_enabled
            for row in range(MAX_CONTROLLERS)
        }

        # The client-wide switch is deliberately never disabled: it stays
        # settable at any time, connected or not.

        if self._transport is not None and self._transport.is_connected:
            self._transport.set_rumble_enabled(enabled, slots)

    def _on_slot_toggled(self) -> None:
        self._update_slot_availability()

    def _on_username_typed(self) -> None:
        """A name is being typed. Push it shortly after they stop.

        **`editingFinished` alone was the bug.** It fires on Enter or on focus
        leaving the field -- so a player typed a name, looked at the server,
        and saw the old one, because the field they were still in had never
        lost focus. Reported as the server never being updated at all.

        Debounced rather than sent per keystroke: "Spencer" would otherwise be
        seven control messages and seven log lines on the server, six of them
        describing a name nobody has.
        """
        if self._loading:
            # Seeding the fields from the config emits this too, and arming a
            # push for values that came *from* disk is work for nothing.
            return
        self._username_push.start()

    def _on_username_changed(self) -> None:
        """Save the names, and push them without needing a reconnect."""
        self._username_push.stop()
        self._save_ui_into_config()

        if self._transport is None or not self._transport.is_connected:
            return

        for row, edit in enumerate(self._players.username_edits):
            # **The same fallback the connect path uses.** `_build_slots` sends
            # "Player 1" for an empty box; sending "" from here instead would
            # blank a name the server had been given seconds earlier, and it
            # would do it for every *other* slot on any one slot's edit.
            username = edit.text().strip() or f"Player {row + 1}"
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
        connected = self._transport is not None and self._transport.is_connected

        for row in range(MAX_CONTROLLERS):
            has_device = self._controllers.device_combos[row].currentData() is not None
            within_capacity = capacity == 0 or row < capacity
            in_use = self._controllers.enable_boxes[row].isChecked()

            # **Only capacity can take a tick away.** It used to need a gamepad
            # as well, so choosing "None" silently unticked the row -- and the
            # tick is the player saying "this controller is mine", which is a
            # thing to decide before the pad is plugged in, not after. A ticked
            # row with no pad simply streams nothing and says so in Status.
            if not within_capacity and in_use:
                self._controllers.enable_boxes[row].setChecked(False)
                in_use = False

            # **While connected, only the controllers in play can be edited.**
            #
            # The server allocates a Bluetooth adapter per controller at
            # handshake time, so which slots are in use is settled for the
            # session -- a tick here would change nothing on the console, which
            # is worse than a control that says it cannot. Everything about a
            # controller that *is* in play stays editable and is pushed live:
            # its player name, its gamepad, and everything in its Configure
            # window.
            #
            # A slot that is not in play is locked as a set. Its settings would
            # otherwise look like they were doing something while the server
            # had never been told the slot exists at all.
            editable = within_capacity and (in_use or not connected)

            # Choosing a controller must stay possible as long as the slot
            # exists at all -- an earlier version greyed the whole row out
            # whenever "None" was selected, which left no way to pick one.
            self._controllers.device_combos[row].setEnabled(editable)
            self._controllers.type_combos[row].setEnabled(editable)
            self._players.username_edits[row].setEnabled(editable)
            self._controllers.configure_buttons[row].setEnabled(editable)
            self._controllers.enable_boxes[row].setEnabled(
                within_capacity and not connected)
            # A row out of play for this session reads as switched off: Qt's
            # disabled state only dims text, which against this backdrop is a
            # difference of a few percent.
            self._controllers.set_row_locked(row, connected and not in_use)

            item = self._controllers.table.item(row, COL_STATUS)
            if connected and not in_use:
                self._controllers.enable_boxes[row].setToolTip(
                    "Disconnect to bring another controller into play: the "
                    "server assigns an adapter to each one when the session "
                    "starts."
                )
            elif connected:
                self._controllers.enable_boxes[row].setToolTip(
                    "In play. Disconnect to take it out."
                )
            elif not within_capacity:
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

    def _build_theme_menu(self) -> QMenu:
        """The colour-scheme picker.

        A menu of exclusive checkable actions rather than a combo box: it is a
        preference someone sets once, and a combo in the header would compete
        for attention with the connection state beside it.
        """
        menu = QMenu(self)
        self._theme_actions = QActionGroup(menu)
        self._theme_actions.setExclusive(True)
        for name in theme_names():
            action = menu.addAction(THEME_LABELS.get(name, name.title()))
            action.setCheckable(True)
            action.setData(name)
            action.triggered.connect(lambda _=False, n=name: self._on_theme_chosen(n))
            self._theme_actions.addAction(action)
        return menu

    def _on_theme_chosen(self, name: str) -> None:
        self._apply_theme(name)
        self._config.theme = name
        client_config.save(self._config)

    def _apply_theme(self, name: str) -> None:
        """Re-theme the running application.

        **Only when the theme actually changes.** `apply_theme` sets the
        *application* stylesheet, and Qt re-polishes every widget that exists
        when it does -- so calling it from each window's constructor is
        quadratic in the number of windows. Measured: six successive
        `MainWindow`s took 896ms rising to 1996ms each, and the GUI test suite
        went from about a minute to seventy.

        The startup path is already themed by `run()`, so a window normally has
        nothing to do here but sync its own widgets.
        """
        if theme_needs_applying(name, QApplication.instance()):
            apply_theme(QApplication.instance(), name)
        self._sync_theme_ui()

    def _sync_theme_ui(self) -> None:
        """Refresh what this window caches of the palette.

        The latency cards write an inline stylesheet and the plot holds pens,
        so neither follows the application stylesheet on its own.
        """
        applied = active_theme()
        for action in self._theme_actions.actions():
            action.setChecked(action.data() == applied)
        for label in self._latency.cards:
            label.setStyleSheet(_latency_style(None))
        self._latency.plot.retheme()
        self._bar_latency.setStyleSheet("")
        self.update()

    def _on_drawer_clicked(self) -> None:
        self._set_drawer_open(not self._drawer.is_open())

    def _set_drawer_open(self, opened: bool) -> None:
        self._drawer.set_open(opened)
        # Checked means the panel is *showing*. It was inverted -- the button
        # lit up when the drawer was hidden -- which reads as the control being
        # out of step with what it did.
        self._drawer_button.setChecked(opened)
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

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

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QActionGroup,
    QDesktopServices,
    QColor,
    QFont,
    QImage,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QFrame,
    QScrollArea,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QSpinBox,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from common.video import DetectionTuning, VideoSettings
from videoserver import config as video_config
from videoserver.config import VideoServerConfig
from videoserver.assets import app_icon
from videoserver.playervision.overlay import (
    TONE_IDENTIFIED,
    TONE_WEAK,
    box_pixels,
    breakdown_lines,
    overlay_boxes,
)
from videoserver.levelmeter import LevelMeter
from videoserver.pipeline_strip import PipelineStrip

from qtui.shell import HeaderBar, default_window_size
from common.design.themes import LABELS as THEME_LABELS
from common.design.themes import active_theme, theme_names
from common.design.tokens import Radius, Space, Type
from qtui.backdrop import BackdropWidget
from qtui.buttons import IconButton
from qtui.theme import apply_theme, qcolor
from qtui.feedback import ConfirmDialog, Notice
from qtui.widgets import NoWheelComboBox, NoWheelDoubleSpinBox, NoWheelSpinBox

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

_PLAYER_COLUMNS = ("Player", "Confidence", "Identified by", "Region", "Track")

#: Tone -> the colour token its box and row are drawn in.
#:
#: Three states rather than two, and the middle one is the point: a label held
#: by continuity at 0.70 and one recognised by appearance at 0.95 are both
#: "identified", and an operator deciding whether to believe what they are
#: seeing needs them separable without reading the number.
_TONE_COLOURS = {
    TONE_IDENTIFIED: "success",
    TONE_WEAK: "warning",
}
_TONE_FALLBACK = "text-muted"


#: How much wider the preview is encoded once it has a window of its own.
#:
#: The inline preview stays at `PREVIEW_WIDTH_LOCAL`, because that is a
#: thumbnail beside a table and 640 is already generous for it. A popped-out
#: window is the operator asking for a bigger picture, and upscaling a
#: 640-wide JPEG into it would answer with a blurrier one -- the overlay's
#: text would survive (it is drawn afterwards, at a fixed size) while the game
#: underneath it turned to mush, which is exactly backwards for a view whose
#: job is judging what the capture card is seeing.
#:
#: Capped rather than unbounded: this is an MJPEG encode several times a
#: second on the machine that is also running the H.264 encoder, and
#: `PreviewEncoder._target_size` already clamps to the capture's own width, so
#: asking for more than the source has costs nothing and gains nothing.
PREVIEW_WIDTH_MAX = 1920

#: Requested widths are rounded to this before reaching the encoder.
#:
#: `PreviewEncoder._context` rebuilds its codec context whenever the size
#: changes, and a window being dragged to a new size changes width every
#: frame. Without the step that is a new MJPEG encoder per mouse movement.
PREVIEW_WIDTH_STEP = 160


class PreviewWindow(QMainWindow):
    """The preview, on its own and resizable.

    Exists because the identification overlay put real detail on a picture
    that was sized as a thumbnail: boxes, a player, a confidence and a
    sentence explaining a refusal, at 640 pixels beside a table.

    **Nothing opens this by itself.** The client's own video window had to
    grow a "dismissed" flag because its tick reopened it the instant it was
    closed; this is opened only by the button, so closing it stays closed with
    no extra state to get wrong.
    """

    closed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preview")
        self.setWindowIcon(app_icon())
        self.resize(960, 540)

        self._label = QLabel("No preview")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setMinimumSize(320, 180)
        self._label.setStyleSheet(
            f"background: {qcolor('video-backdrop').name()};"
            f" color: {qcolor('text-muted').name()};"
        )
        self.setCentralWidget(self._label)

    def surface_size(self):
        """Where the picture has to fit. Asked every frame rather than
        tracked on resize: the tick is the only thing that draws, so a size
        cached at resize time would be one event older than the pixmap."""
        return self._label.size()

    def show_frame(self, pixmap) -> None:
        self._label.setText("")
        self._label.setPixmap(pixmap)

    def clear(self, message: str = "No preview") -> None:
        self._label.setPixmap(QPixmap())
        self._label.setText(message)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        # Hidden rather than destroyed, and reused on the next press. Closing
        # is all that can safely be done to a Qt widget from outside its own
        # parent chain -- this file's own notes on the test suite say so -- and
        # reuse removes the lifetime question outright.
        self.closed.emit()
        super().closeEvent(event)


class VideoServerWindow(QMainWindow):
    #: A model download reports from its own thread; the GUI hears it here.
    _download_progress = Signal(int, int, str)
    _download_finished = Signal(str)

    def __init__(self, config: VideoServerConfig) -> None:
        super().__init__()
        self._config = config
        #: Tuning key -> the widget that edits it. One table, so loading,
        #: saving and Restore defaults cannot disagree about a field.
        self._tuning_widgets: dict[str, QWidget] = {}
        self._split_widgets: dict[str, QWidget] = {}
        self._downloading = False
        self._model_report: tuple[float, dict] | None = None
        self._app = None
        self._control = None
        self._preview = None
        self._beacon = None
        self._loading = True
        #: What the overlay draws, refreshed by `_update_players` on the slow
        #: timer and consumed by `_update_preview` on the fast one. Empty
        #: until identification produces something, so the preview is exactly
        #: what it was before this existed whenever the feature is off.
        self._overlay_boxes = ()
        #: The pop-out, built on first use and then reused. Never rebuilt, so
        #: it keeps the size and position the operator gave it.
        self._preview_window = None

        self.setWindowTitle("Remote Game Video Server")
        self.setWindowIcon(app_icon())
        # The same height band the client opens into, from the one rule both
        # applications read. It was a fixed 880x700, which had no relationship
        # to anything and became unusable when the identification panel made
        # the content taller than the window.
        self.resize(default_window_size(min_width=880, max_width=1200))

        self._download_progress.connect(self._on_download_progress)
        self._download_finished.connect(self._on_download_finished)

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
        """Pipeline-first: where it has stopped, then the settings.

        The three groups are the ones that were here before, built by the same
        methods. What is new above them is the strip that answers the question
        this window exists for.
        """
        central = BackdropWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._header = HeaderBar("RBGC Video Server")
        self._theme_button = IconButton("droplet", "Colour scheme")
        self._theme_button.setMenu(self._build_theme_menu())
        self._header.add_action(self._theme_button)
        root.addWidget(self._header)

        # Scrolled, because the content is taller than a laptop screen once
        # the identification panel is open -- and a window whose bottom cannot
        # be reached has no Apply button, which is not a cosmetic problem.
        #
        # The scroll area sits *inside* the backdrop and paints nothing of its
        # own, so the backdrop stays put and only the cards move. Its viewport
        # fills its background by default, which would paint a flat rectangle
        # over that backdrop.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.viewport().setAutoFillBackground(False)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")

        body_host = QWidget()
        body_host.setAutoFillBackground(False)
        body = QVBoxLayout(body_host)
        body.setContentsMargins(Space.LG, Space.LG, Space.LG, Space.LG)
        body.setSpacing(Space.MD)

        self._pipeline = PipelineStrip()
        # Seeded, not left blank. `_tick` returns early while nothing is
        # running, so without this the cards sat on their placeholder dashes
        # and said nothing about why.
        self._pipeline.update_from(None, streaming=False)
        body.addWidget(self._pipeline)

        body.addWidget(self._build_connection_group())
        body.addWidget(self._build_capture_group())
        body.addWidget(self._build_identification_group())
        body.addWidget(self._build_split_group())
        body.addWidget(self._build_status_group(), 1)
        body.addWidget(self._build_players_group())
        # No trailing stretch: the Status group is already added with one, and
        # a second would split the slack with it -- leaving a band of dead
        # space under the preview on a tall window.
        scroll.setWidget(body_host)
        root.addWidget(scroll, 1)

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

        self._media_port = NoWheelSpinBox()
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
        self._state_label.setProperty("role", "muted")
        buttons.addWidget(self._start_button)
        buttons.addWidget(self._state_label, 1)

        outer.addLayout(form)
        outer.addLayout(buttons)
        return group

    def _build_capture_group(self) -> QGroupBox:
        group = QGroupBox("Capture")
        form = QFormLayout(group)

        device_row = QHBoxLayout()
        self._device = NoWheelComboBox()
        self._device.setMinimumWidth(240)
        self._rescan = QPushButton("Rescan")
        self._rescan.clicked.connect(self._on_rescan)
        device_row.addWidget(self._device, 1)
        device_row.addWidget(self._rescan)
        form.addRow("Video device:", _wrap(device_row))

        self._audio_device = NoWheelComboBox()
        form.addRow("Audio device:", self._audio_device)

        self._resolution = NoWheelComboBox()
        for label, width, height in _RESOLUTIONS:
            self._resolution.addItem(label, (width, height))
        form.addRow("Resolution:", self._resolution)

        self._fps = NoWheelComboBox()
        for rate in (30, 60):
            self._fps.addItem(f"{rate} fps", rate)
        form.addRow("Frame rate:", self._fps)

        self._bitrate = NoWheelSpinBox()
        self._bitrate.setRange(500, 50000)
        self._bitrate.setSingleStep(500)
        self._bitrate.setSuffix(" kbps")
        form.addRow("Bitrate:", self._bitrate)

        self._encoder = NoWheelComboBox()
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

    def _build_identification_group(self) -> QGroupBox:
        """How identification runs *on this machine*.

        These belong here rather than only in the Bluetooth server's web GUI
        because they describe work done on the capture machine -- which model,
        how sure it has to be, how often it looks -- and in external mode that
        machine is somebody else's. Same division the capture and encoding
        settings already follow: the server asks for labels, this end decides
        how they are produced. `server/video.py:SOURCE_OWNED_FIELDS` is where
        that is enforced, so a push from the server cannot revert them.

        The Bluetooth server's own copy of these is for **embedded** mode,
        where the video server is a headless subprocess with no window.
        """
        group = QGroupBox("Player identification")
        form = QFormLayout(group)

        self._allow_player_id = QCheckBox(
            "Allow the Bluetooth server to identify players on this computer"
        )
        self._allow_player_id.setToolTip(
            "Lets the Bluetooth server run player identification here, so "
            "each player's name can be drawn above their character. It still "
            "has to ask; this is only this computer's permission to load a "
            "vision model, because this is the computer with the GPU."
        )
        form.addRow("", self._allow_player_id)

        # The model, where the backend dropdown used to be. There is one
        # backend now -- the model -- so the useful question is whether this
        # machine has one, and the way to get one.
        self._model_status = QLabel("—")
        self._model_status.setWordWrap(True)
        self._model_status.setProperty("role", "muted")
        self._model_download = QPushButton("Download model")
        self._model_download.setToolTip(
            "Fetches YOLOX-Tiny (Megvii) and MobileNetV2 (ONNX Model Zoo), both "
            "Apache-2.0, about 34 MB. Only when you press it, and each file is "
            "checked against a pinned SHA-256. General-purpose models are not "
            "trained on game graphics, so how well they find a given game's "
            "characters has to be seen, not assumed."
        )
        self._model_download.clicked.connect(self._on_download_model)
        self._model_folder = QPushButton("Open folder")
        self._model_folder.setToolTip("Where the model files live on this computer.")
        self._model_folder.clicked.connect(self._on_open_model_folder)
        model_row = QHBoxLayout()
        model_row.addWidget(self._model_status, 1)
        model_row.addWidget(self._model_download)
        model_row.addWidget(self._model_folder)
        form.addRow("Model:", _wrap(model_row))

        # What identification is actually doing, as opposed to what was asked
        # for. There was no such line, so a worker that could not start --
        # every packaged build, for a while -- looked from here exactly like
        # one quietly finding nobody.
        self._player_state = QLabel("—")
        self._player_state.setWordWrap(True)
        self._player_state.setProperty("role", "muted")
        form.addRow("State:", self._player_state)

        self._player_confidence = NoWheelDoubleSpinBox()
        self._player_confidence.setRange(0.05, 0.99)
        self._player_confidence.setSingleStep(0.05)
        self._player_confidence.setDecimals(2)
        self._player_confidence.setToolTip(
            "Below this a character is tracked but no name is attached.\n\n"
            "A wrong name is worse than no name: it is a confident claim in "
            "clean text over somebody's game, and it looks just as "
            "authoritative when it is wrong. Learning can raise it when two "
            "characters look alike; it never lowers it."
        )
        form.addRow("Confidence to publish a name:", self._player_confidence)

        self._player_hz = NoWheelDoubleSpinBox()
        self._player_hz.setRange(0.5, 15.0)
        self._player_hz.setSingleStep(0.5)
        self._player_hz.setDecimals(1)
        self._player_hz.setToolTip(
            "How often a frame is analysed. It runs on the control thread "
            "rather than the encode path, so this costs the stream nothing -- "
            "but it is real work on this machine."
        )
        form.addRow("Samples per second:", self._player_hz)

        tuning = QWidget()
        tuning_form = QFormLayout(tuning)
        tuning_form.setContentsMargins(0, 0, 0, 0)

        anchor_x = _spin(0.0, 1.0, 0.05, 2)
        anchor_y = _spin(0.0, 1.0, 0.05, 2)
        for spin, axis in ((anchor_x, "across"), (anchor_y, "down")):
            spin.setToolTip(
                f"How far {axis} each player's own viewport the camera keeps "
                "them, 0 to 1. A chase camera -- racing games, most third-person "
                "games -- keeps the player low in the middle, about 0.5 across "
                "and 0.7 down. The middle of the view is the road ahead, which "
                "is where every other kart is."
            )
        anchor_row = QHBoxLayout()
        anchor_row.addWidget(QLabel("across"))
        anchor_row.addWidget(anchor_x)
        anchor_row.addWidget(QLabel("down"))
        anchor_row.addWidget(anchor_y)
        tuning_form.addRow("Where the camera keeps each player:", _wrap(anchor_row))
        anchor_auto = QCheckBox("Learn it during play")
        anchor_auto.setToolTip(
            "Moves each viewport's anchor to where its player actually sits, "
            "once something besides position agrees who that is. Relearned "
            "every session."
        )
        tuning_form.addRow("", anchor_auto)
        self._anchor_learned = _readout()
        tuning_form.addRow("", self._anchor_learned)

        radius = _spin(0.05, 1.0, 0.05, 2)
        radius.setToolTip(
            "As a fraction of the viewport. Anything further from the anchor is "
            "never taken to be that viewport's player -- no name beats the "
            "wrong one."
        )
        tuning_form.addRow("How far from it a player may be:", radius)
        margin = _spin(0.0, 0.3, 0.01, 2)
        margin.setToolTip(
            "The band round each viewport's edge where lap counters, item "
            "boxes and maps live. Nothing centred in it can be a viewport's "
            "player."
        )
        tuning_form.addRow("Ignore this close to a viewport's edge:", margin)

        score = _spin(0.01, 0.5, 0.01, 2)
        score.setToolTip(
            "How sure the model has to be that anything is there at all. This "
            "decides what is tracked, never what is named."
        )
        tuning_form.addRow("Detector score needed to track something:", score)
        score_auto = QCheckBox("Learn it during play")
        score_auto.setToolTip(
            "Starts low and settles just under what the model scores the "
            "players at. A general model can be unsure of game graphics, and a "
            "floor it never clears means it never learns anything."
        )
        tuning_form.addRow("", score_auto)
        self._score_learned = _readout()
        tuning_form.addRow("", self._score_learned)

        hits = NoWheelSpinBox()
        hits.setRange(1, 60)
        hits.setToolTip(
            "How many samples something must be seen in before it can be a "
            "viewport's player."
        )
        tuning_form.addRow("Samples before owning a viewport:", hits)
        correlation = _spin(0.05, 0.99, 0.05, 2)
        correlation.setToolTip(
            "Controller input is used alongside the model: it settles which of "
            "two players it is when they look alike, and names what the model "
            "had nothing to say about. This is how closely motion on screen "
            "has to follow that player's stick."
        )
        tuning_form.addRow("Stick match needed to name a player:", correlation)

        self._tuning_widgets.update({
            "pid_anchor_x": anchor_x,
            "pid_anchor_y": anchor_y,
            "pid_anchor_auto": anchor_auto,
            "pid_anchor_radius": radius,
            "pid_edge_margin": margin,
            "pid_score_floor": score,
            "pid_score_auto": score_auto,
            "pid_viewport_hits": hits,
            "pid_correlation_floor": correlation,
        })
        form.addRow(_disclosure("Identification tuning", tuning))

        return group

    def _build_split_group(self) -> QGroupBox:
        """How this machine recognises a split screen.

        The split detector runs here, so its measuring settings are this
        machine's -- `SOURCE_OWNED_FIELDS` in `server/video.py` stops a
        Bluetooth server's push reverting them. What it does *not* own is
        whether detection runs, forcing a layout, and trimming bars: those
        decide what each player is cropped to, and live on the Bluetooth
        server's Controllers page.
        """
        group = QGroupBox("Split-screen detection")
        form = QFormLayout(group)

        hint = QLabel(
            "Switched on, and a layout forced, from the Bluetooth server's "
            "Controllers page."
        )
        hint.setProperty("role", "muted")
        hint.setWordWrap(True)
        form.addRow(hint)

        self._layout_readout = _readout()
        _say(self._layout_readout, "Starts when streaming.")
        form.addRow("Now:", self._layout_readout)

        confidence = _spin(0.05, 0.99, 0.01, 2)
        confidence.setToolTip(
            "How far a seam has to stand out from the rest of the picture before "
            "a split is believed. This is what keeps menus and busy scenery "
            "from reading as a split, so it asks a lot."
        )
        form.addRow("Confidence to switch to split:", confidence)

        hold = _spin(0.05, 0.95, 0.01, 2)
        hold.setToolTip(
            "Once a split is confirmed the seam only has to stay this strong -- "
            "deliberately less than entering, because the question is only "
            "whether it is still there."
        )
        form.addRow("Confidence to stay split:", hold)
        hold_auto = QCheckBox("Learn it during play")
        hold_auto.setToolTip(
            "Settles between what the seam scores and what ordinary picture "
            "scores in this game. Relearned every session."
        )
        form.addRow("", hold_auto)
        self._hold_learned = _readout()
        form.addRow("", self._hold_learned)

        hz = _spin(0.2, 10.0, 0.5, 1)
        form.addRow("Checks per second:", hz)

        activate = NoWheelSpinBox()
        activate.setRange(1, 60)
        self._activate_seconds = _readout()
        self._activate_seconds.setVisible(True)
        activate_row = QHBoxLayout()
        activate_row.addWidget(activate)
        activate_row.addWidget(self._activate_seconds, 1)
        form.addRow("Checks before switching to split:", _wrap(activate_row))

        deactivate = NoWheelSpinBox()
        deactivate.setRange(1, 60)
        deactivate.setToolTip(
            "How many checks in a row must find no seam before a split is "
            "dropped."
        )
        self._deactivate_seconds = _readout()
        self._deactivate_seconds.setVisible(True)
        deactivate_row = QHBoxLayout()
        deactivate_row.addWidget(deactivate)
        deactivate_row.addWidget(self._deactivate_seconds, 1)
        form.addRow("Checks before going back to full screen:", _wrap(deactivate_row))
        leave_auto = QCheckBox("Wait longer after gaps that recover")
        leave_auto.setToolTip(
            "Grows the wait to twice the longest gap the seam has recovered "
            "from this session, up to 20 seconds, and never below the number "
            "above."
        )
        form.addRow("", leave_auto)
        self._leave_learned = _readout()
        form.addRow("", self._leave_learned)

        more = QWidget()
        more_form = QFormLayout(more)
        more_form.setContentsMargins(0, 0, 0, 0)
        smoothing = _spin(0.0, 10.0, 0.5, 1)
        smoothing.setToolTip(
            "The seam never moves and scene edges do, so averaging over a "
            "couple of seconds is what lets the seam stand out. 0 judges every "
            "frame alone."
        )
        more_form.addRow("Average over (seconds):", smoothing)
        tolerance = _spin(0.0, 0.25, 0.005, 3)
        tolerance.setToolTip(
            "As a fraction of the picture. Players are only ever cropped to "
            "exact halves and quarters, so a boundary well off the middle is "
            "not one this can use -- and menus put their bars there."
        )
        more_form.addRow("How far off the middle a seam may be:", tolerance)
        edge = NoWheelSpinBox()
        edge.setRange(4, 96)
        edge.setToolTip(
            "On a 0-255 scale. Lower suits a very dark game with soft "
            "boundaries; higher ignores noise and gradients."
        )
        more_form.addRow("Brightness step that counts as an edge:", edge)
        width = NoWheelSpinBox()
        width.setRange(160, 640)
        width.setSingleStep(2)
        more_form.addRow("Analysis width (pixels):", width)
        form.addRow(_disclosure("More detection settings", more))

        buttons = QHBoxLayout()
        reset = QPushButton("Reset learning")
        reset.setToolTip(
            "Forget what this session learned about the game -- thresholds, "
            "where each camera keeps its player -- and keep the players."
        )
        reset.clicked.connect(self._on_reset_learning)
        defaults = QPushButton("Restore defaults")
        defaults.setToolTip("Fills in the defaults. Nothing changes until Apply.")
        defaults.clicked.connect(self._on_restore_defaults)
        buttons.addWidget(reset)
        buttons.addWidget(defaults)
        buttons.addStretch(1)
        form.addRow(_wrap(buttons))

        self._split_widgets = {
            "split_detect_confidence": confidence,
            "split_detect_hz": hz,
            "split_detect_activate": activate,
            "split_detect_deactivate": deactivate,
            "split_detect_tolerance": tolerance,
            "split_detect_width": width,
        }
        self._tuning_widgets.update({
            "split_hold": hold,
            "split_hold_auto": hold_auto,
            "split_leave_auto": leave_auto,
            "split_smoothing_s": smoothing,
            "split_edge_delta": edge,
        })
        for spin in (hz, activate, deactivate):
            spin.valueChanged.connect(self._update_check_seconds)
        return group

    def _update_check_seconds(self, *_args) -> None:
        """Say in seconds what a number of checks means at this rate."""
        widgets = getattr(self, "_split_widgets", None)
        if not widgets:
            return
        hz = max(float(widgets["split_detect_hz"].value()), 0.05)
        self._activate_seconds.setText(
            f"= {widgets['split_detect_activate'].value() / hz:.1f} s"
        )
        self._deactivate_seconds.setText(
            f"= {widgets['split_detect_deactivate'].value() / hz:.1f} s"
        )

    def _build_status_group(self) -> QGroupBox:
        group = QGroupBox("Status")
        layout = QVBoxLayout(group)

        self._summary = QLabel("Not streaming")
        self._summary.setProperty("role", "muted")
        summary_font = self._summary.font()
        summary_font.setFamilies(list(Type.FAMILIES_MONO))
        self._summary.setFont(summary_font)
        layout.addWidget(self._summary)

        body = QHBoxLayout()

        self._preview_label = QLabel("No preview")
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setMinimumSize(320, 180)
        self._preview_label.setProperty("surface", "sunken")
        self._preview_label.setStyleSheet(
            f"background: {qcolor('video-backdrop').name()};"
            f" color: {qcolor('text-muted').name()};"
            f" border: 1px solid {qcolor('border-subtle', over='background-base').name()};"
            f" border-radius: {Radius.CARD}px;"
        )
        preview_column = QVBoxLayout()
        preview_column.addWidget(self._preview_label, 1)

        self._popout_button = QPushButton("Open preview in a window")
        self._popout_button.setToolTip(
            "A resizable window showing the same picture, and the same "
            "identification overlay.\n\n"
            "The picture is encoded larger while it is open, so a bigger "
            "window shows more rather than the same picture enlarged."
        )
        self._popout_button.clicked.connect(self._toggle_preview_window)
        preview_column.addWidget(self._popout_button)
        body.addLayout(preview_column, 1)

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

    def _build_players_group(self) -> QGroupBox:
        """What identification is seeing, and why.

        **Hidden until identification is actually running**, rather than
        greyed out or left empty: this window is the capture machine's control
        panel and most of the people who open it never turn this on. An empty
        table captioned "Player identification" reads as a broken feature
        rather than an unused one.
        """
        group = QGroupBox("Identification detail")
        layout = QVBoxLayout(group)

        self._players_summary = QLabel("Not running")
        self._players_summary.setProperty("role", "muted")
        summary_font = self._players_summary.font()
        summary_font.setFamilies(list(Type.FAMILIES_MONO))
        self._players_summary.setFont(summary_font)
        layout.addWidget(self._players_summary)

        body = QHBoxLayout()

        self._players_table = QTableWidget(0, len(_PLAYER_COLUMNS))
        self._players_table.setHorizontalHeaderLabels(_PLAYER_COLUMNS)
        self._players_table.verticalHeader().setVisible(False)
        self._players_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self._players_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._players_table.setMinimumHeight(110)
        body.addWidget(self._players_table, 1)

        # The breakdown is a plain monospaced label rather than a table: it is
        # ragged by nature -- a track may carry one signal or five, each with
        # a sentence explaining itself -- and a table of it would be mostly
        # empty cells with the sentences elided.
        self._players_detail = QLabel("")
        self._players_detail.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self._players_detail.setWordWrap(False)
        self._players_detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        detail_font = self._players_detail.font()
        detail_font.setFamilies(list(Type.FAMILIES_MONO))
        self._players_detail.setFont(detail_font)
        self._players_detail.setMinimumWidth(360)
        body.addWidget(self._players_detail, 1)

        layout.addLayout(body, 1)
        group.setVisible(False)
        self._players_group = group
        return group

    def _update_players(self, app) -> None:
        """Fill the table and the breakdown, or hide the group.

        Reads the source's own rows, **not** anything the Bluetooth server is
        distributing. Labels only reach a player when that client has opted in
        and the server is broadcasting them, and the question this panel
        answers -- is identification working -- must be answerable when none of
        that is true.
        """
        rows = app.player_rows()
        judgements = app.player_judgements()
        stats = app.player_id_stats()
        running = bool(stats) or bool(rows)

        self._players_group.setVisible(running)
        if not running:
            self._overlay_boxes = ()
            return

        backend = stats.get("backend") or {}
        name = backend.get("backend") if isinstance(backend, dict) else backend
        identified = sum(1 for row in rows if row.identified)
        failed = stats.get("failed") or ""
        self._players_summary.setText(
            f"{name or 'backend'}   "
            f"{len(rows)} tracked, {identified} identified   "
            f"layout {stats.get('layout', '?')}   "
            f"players known {stats.get('players', 0)}"
            + (f"   STOPPED: {failed}" if failed else "")
        )

        # Kept for the overlay, which is painted on the preview's own timer --
        # four times a second here against fifteen there, so recomputing them
        # in the paint path would cost more and change nothing.
        self._overlay_boxes = overlay_boxes(rows, judgements)

        table = self._players_table
        table.setRowCount(len(self._overlay_boxes))
        for index, entry in enumerate(self._overlay_boxes):
            # Read off the box's own fields rather than taken apart from the
            # text drawn on the picture: a column recovered from a display
            # string empties itself the next time somebody rewords the label.
            values = (
                f"Player {entry.player_id}" if entry.identified else "—",
                f"{entry.confidence:.2f}" if entry.confidence else "—",
                entry.source if entry.identified else entry.detail,
                entry.region or "whole screen",
                f"#{entry.track_id}",
            )
            colour = qcolor(_TONE_COLOURS.get(entry.tone, _TONE_FALLBACK))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setForeground(colour)
                table.setItem(index, column, item)

        self._players_detail.setText(
            "\n".join(
                line
                for judgement in judgements
                for line in (*breakdown_lines(judgement), "")
            )
            or "Nothing tracked in the last sample."
        )

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
        self._allow_player_id.setChecked(
            bool(getattr(self._config, "playervision_allowed", False))
        )
        self._player_confidence.setValue(float(settings.player_id_confidence))
        self._player_hz.setValue(float(settings.player_id_hz))
        for key, widget in self._split_widgets.items():
            _set_value(widget, getattr(settings, key))
        self._load_tuning(getattr(self._config, "tuning", None) or DetectionTuning())
        self._update_check_seconds()

        self._populate_encoders()
        self._refresh_devices()

    def _save_ui_into_config(self) -> None:
        cfg = self._config
        cfg.password = self._password.text()
        cfg.save_password = self._save_password.isChecked()
        cfg.discoverable = self._discoverable.isChecked()
        cfg.media_port = self._media_port.value()
        cfg.name = self._name.text().strip() or cfg.name
        # Local to this installation, like the password and the port, and
        # never part of `VideoSettings` -- a source adopts whatever is pushed
        # at it, so consent living in the pushed block would be handed back to
        # this machine as its own choice and could never be withdrawn.
        cfg.playervision_allowed = self._allow_player_id.isChecked()
        cfg.settings = self._settings_from_ui()
        cfg.tuning = self._tuning_from_ui()

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
                "player_id_confidence": self._player_confidence.value(),
                "player_id_hz": self._player_hz.value(),
                **{
                    key: widget.value()
                    for key, widget in self._split_widgets.items()
                },
            }
        )
        return VideoSettings(**values).clamped()

    def _tuning_from_ui(self) -> DetectionTuning:
        values = {key: _get_value(widget) for key, widget in self._tuning_widgets.items()}
        return DetectionTuning.from_dict(values).clamped()

    def _load_tuning(self, tuning: DetectionTuning) -> None:
        values = tuning.to_dict()
        for key, widget in self._tuning_widgets.items():
            if key in values:
                _set_value(widget, values[key])

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
            Notice.warning(
                self, "Cannot start", "\n".join(f"• {p}" for p in problems)
            )
            return
        if not self._config.password:
            Notice.warning(
                self, "Cannot start", "Set the password your players use."
            )
            return

        try:
            from videoserver.control import ControlResponder
            from videoserver.pipeline import VideoServerApp
            from videoserver.preview import PreviewEncoder
        except ImportError as exc:
            Notice.critical(
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
        # The pop-out is fed only while a pipeline is running, so without this
        # it would sit on its last frame indefinitely -- a picture of a stream
        # that stopped, captioned as though it were live.
        if self._preview_window is not None:
            self._preview_window.clear("Not streaming")

    def _on_apply(self) -> None:
        self._save_ui_into_config()
        if self._app is not None:
            self._app.apply_config(self._config.settings)
            self._app.apply_tuning(self._config.tuning)
            self._set_status("Settings applied")

    def _on_reset_learning(self) -> None:
        if self._app is None:
            self._set_status("Nothing learned yet — nothing is running")
            return
        self._app.reset_learning()
        self._set_status("Learning reset")

    def _on_restore_defaults(self) -> None:
        """Fill in the defaults; nothing changes until Apply."""
        self._load_tuning(DetectionTuning())
        defaults = VideoSettings()
        for key, widget in self._split_widgets.items():
            _set_value(widget, getattr(defaults, key))
        self._update_check_seconds()
        self._set_status("Defaults filled in — press Apply to use them")

    # -- the model -----------------------------------------------------------

    def _on_download_model(self) -> None:
        from videoserver.playervision import models

        megabytes = models.total_size() / 1_000_000
        lines = [f"Download about {megabytes:.0f} MB to this computer?", ""]
        for item in models.MODELS:
            lines.append(f"{item.title} — {item.source} ({item.licence})")
        lines += [
            "",
            "Each file is checked against a pinned SHA-256 and refused if it "
            "does not match. Any model already in the folder is kept, renamed, "
            "rather than overwritten.",
        ]
        if not ConfirmDialog.ask(
            self, "Download model", "\n".join(lines), confirm_text="Download"
        ):
            return

        import threading

        self._downloading = True
        self._model_download.setEnabled(False)

        def run() -> None:
            try:
                models.download(
                    progress=lambda done, total, what:
                        self._download_progress.emit(done, total, what)
                )
            except models.DownloadError as exc:
                self._download_finished.emit(str(exc))
                return
            except Exception as exc:  # noqa: BLE001 -- said, never raised at the GUI
                self._download_finished.emit(f"{type(exc).__name__}: {exc}")
                return
            self._download_finished.emit("")

        threading.Thread(target=run, name="model-download", daemon=True).start()

    def _on_download_progress(self, done: int, total: int, what: str) -> None:
        percent = done * 100 // max(total, 1)
        self._model_status.setText(f"Downloading {what} — {percent}%")

    def _on_download_finished(self, error: str) -> None:
        self._downloading = False
        self._model_download.setEnabled(True)
        self._model_report = None
        if error:
            self._set_status(f"Model not installed: {error}")
            self._model_status.setText(f"Download failed: {error}")
        else:
            self._set_status(
                "Model installed — identification picks it up the next time "
                "it starts"
            )
            self._update_model_status()

    def _on_open_model_folder(self) -> None:
        from videoserver.playervision.backends.onnx import model_dir

        directory = model_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def _update_model_status(self) -> None:
        """What is in the model folder. Two stats, every couple of seconds."""
        if self._downloading:
            return
        import time as _time

        now = _time.monotonic()
        cached = self._model_report
        if cached is None or now - cached[0] > 2.0:
            from videoserver.playervision import models

            report = models.status()
            try:
                import importlib.util

                report["runtime"] = importlib.util.find_spec("onnxruntime") is not None
            except (ImportError, ValueError):
                report["runtime"] = False
            self._model_report = cached = (now, report)
        self._model_status.setText(_model_sentence(cached[1]))
        self._model_download.setText(
            "Download again" if cached[1].get("detector") else "Download model"
        )

    def _update_detection_readouts(self, app) -> None:
        """The layout's per-axis strength, and what this session learned."""
        snapshot = app.layout_snapshot() if app is not None else {}
        learned = app.learned() if app is not None else {}
        split = learned.get("split") or {}
        identity = learned.get("identity") or {}
        if not self._config.settings.split_detect_enabled:
            self._layout_readout.setText(
                "Detection is off — the Bluetooth server switches it on."
            )
        elif snapshot:
            text = str(snapshot.get("mode", "FULL"))
            if "v" in snapshot:
                text += (
                    f"   vertical {snapshot['v']:.2f}, horizontal {snapshot['h']:.2f}"
                )
            if split:
                text += (
                    f"   stay above {float(split.get('hold_in_force', 0.0)):.2f}, "
                    f"leave after {split.get('leave_in_force', 0)} checks"
                )
            self._layout_readout.setText(text)
        for label, what in (
            (self._hold_learned, "hold"),
            (self._leave_learned, "leave"),
            (self._anchor_learned, "anchor"),
            (self._score_learned, "score"),
        ):
            _say(label, _learned_sentence(what, split, identity))

    def _on_rescan(self) -> None:
        self._refresh_devices()
        self._set_status("Rescanned capture devices")

    # -- polling -----------------------------------------------------------

    def _tick(self) -> None:
        app = self._app
        self._update_model_status()
        if app is None:
            self._pipeline.update_from(None, streaming=False)
            self._player_state.setText("The video server is stopped.")
            return
        self._update_detection_readouts(app)

        status = app.status()
        self._player_state.setText(
            _player_state_sentence(
                status.get("player_id"),
                bool(app.playervision_allowed),
                bool(app.settings.player_id_enabled),
            )
        )
        self._pipeline.update_from(status, streaming=bool(status.get("streaming")))
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
        self._update_players(app)

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

    def _toggle_preview_window(self) -> None:
        """Open the pop-out, or close it if it is already up."""
        window = self._preview_window
        if window is not None and window.isVisible():
            window.close()
            return

        if window is None:
            window = PreviewWindow(self)
            # Qt.Window rather than a child: parented so it closes with the
            # main window and inherits the theme, top-level so it has its own
            # frame and can be resized and moved independently.
            window.setWindowFlag(Qt.WindowType.Window, True)
            window.closed.connect(self._on_preview_window_closed)
            self._preview_window = window
        if self._preview is None:
            window.clear("Not streaming")
        window.show()
        window.raise_()
        self._popout_button.setText("Close preview window")

    def _on_preview_window_closed(self) -> None:
        self._popout_button.setText("Open preview in a window")

    def _preview_surfaces(self) -> list:
        """Every live place a picture has to be put, largest first.

        A list rather than one target because both can be open at once, and
        the encode width is chosen from the largest of them -- so the inline
        thumbnail never drags the pop-out's picture back down to 640.
        """
        surfaces = [(self._preview_label.size(), self._preview_label.setPixmap)]
        window = self._preview_window
        if window is not None and window.isVisible():
            surfaces.append((window.surface_size(), window.show_frame))
        surfaces.sort(key=lambda item: item[0].width(), reverse=True)
        return surfaces

    def _wanted_preview_width(self, surfaces) -> int:
        """How wide to encode, for the surfaces currently on screen.

        Rounded up to `PREVIEW_WIDTH_STEP` because the encoder rebuilds its
        codec context on any size change, and a window being dragged changes
        width every frame -- without the step that is a fresh MJPEG encoder
        per mouse movement.
        """
        widest = max((size.width() for size, _ in surfaces), default=0)
        wanted = max(PREVIEW_WIDTH_LOCAL, widest)
        wanted = min(wanted, PREVIEW_WIDTH_MAX)
        step = PREVIEW_WIDTH_STEP
        return ((wanted + step - 1) // step) * step

    def _update_preview(self, app) -> None:
        if self._preview is None:
            return
        surfaces = self._preview_surfaces()
        self._preview.width = self._wanted_preview_width(surfaces)

        # Through the app, never straight at the frame: the responder encodes
        # its own preview from the same object, and reformatting it from both
        # threads at once wedges one of them -- here, the GUI thread.
        jpeg, _captured = app.encode_preview(self._preview)
        if not jpeg:
            return

        image = QImage.fromData(jpeg, "JPEG")
        if image.isNull():
            return
        source = QPixmap.fromImage(image)
        self._preview_label.setText("")
        for size, show in surfaces:
            # Scaled per surface. One pixmap shared between two differently
            # sized labels would be drawn at one of their sizes and stretched
            # at the other, and the overlay painted into it would stretch with
            # it -- boxes off the entities they annotate, which is the one
            # thing this view must never do.
            pixmap = source.scaled(
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            # Drawn on the *scaled* pixmap, not on the frame before it.
            # Painting into the encoded picture and then shrinking it would
            # shrink the text with it, and this is the one part that has to
            # stay readable whatever size the window is.
            self._paint_overlay(pixmap)
            show(pixmap)

    def _paint_overlay(self, pixmap: QPixmap) -> None:
        """Draw a box and a two-line tag for every track. Never raises.

        The preview is a monitoring picture and this is an operator's debug
        view; a surprise here must cost the annotation, not the picture
        underneath it, so a failure leaves the frame exactly as it arrived.
        """
        boxes = self._overlay_boxes
        if not boxes:
            return

        painter = QPainter()
        if not painter.begin(pixmap):
            return
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            font = QFont(painter.font())
            font.setPointSizeF(max(7.5, font.pointSizeF() - 1.0))
            painter.setFont(font)
            metrics = painter.fontMetrics()

            width = pixmap.width()
            height = pixmap.height()
            for entry in boxes:
                colour = qcolor(_TONE_COLOURS.get(entry.tone, _TONE_FALLBACK))
                x, y, w, h = box_pixels(entry.box, width, height)

                pen = QPen(colour)
                pen.setWidth(2)
                # Dashed for a track with no player. The difference has to
                # survive a greyscale screenshot and a colour-blind reader,
                # which a colour alone does not.
                if not entry.identified:
                    pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(x, y, w, h)

                self._paint_tag(
                    painter, metrics, colour, entry, x, y, width, height
                )
        except Exception:  # noqa: BLE001 -- the annotation, never the picture
            log.debug("Could not draw the identification overlay", exc_info=True)
        finally:
            painter.end()

    def _paint_tag(
        self, painter, metrics, colour, entry, x: int, y: int, width: int,
        height: int,
    ) -> None:
        """The two-line label for one box, kept inside the picture.

        A tag is an annotation on a monitoring image, so it may never leave
        the frame: an unidentified track carries a whole sentence explaining
        itself, which at a plausible box position runs past the right edge and
        is simply cut off -- taking the half that names the fault with it.
        """
        pad = 4
        line_h = metrics.height()
        lines = [entry.title, entry.detail]

        # Elided against the *frame*, not against the box: the box may be
        # narrow and the picture wide, and there is no reason to throw away
        # text that fits on screen.
        room = max(40, width - 2 * pad)
        lines = [
            metrics.elidedText(line, Qt.TextElideMode.ElideRight, room)
            for line in lines
        ]
        text_w = max(metrics.horizontalAdvance(line) for line in lines)
        tag_w = min(text_w + pad * 2, width)
        tag_h = line_h * len(lines) + pad * 2

        # Above the box normally, inside it when the box is against the top of
        # the frame -- an entity near the top edge is exactly where a tag
        # drawn above would fall off the picture entirely.
        tag_y = y - tag_h - 2
        if tag_y < 0:
            tag_y = min(y + 2, max(0, height - tag_h))
        # Pushed left rather than clipped when it would overrun the edge.
        tag_x = max(0, min(x, width - tag_w))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 170))
        painter.drawRect(tag_x, tag_y, tag_w, tag_h)

        painter.setPen(QPen(colour))
        for index, line in enumerate(lines):
            painter.drawText(
                tag_x + pad,
                tag_y + pad + metrics.ascent() + index * line_h,
                line,
            )

    def _build_theme_menu(self) -> QMenu:
        """The colour-scheme picker, the same control the client carries."""
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
        video_config.save(self._config)

    def _apply_theme(self, name: str) -> None:
        """Re-theme the running application.

        The strip and the level meter cache colours of their own, so both are
        told; everything else is rebuilt by `apply_theme`.
        """
        # Only when it actually changes: `apply_theme` sets the *application*
        # stylesheet and Qt re-polishes every existing widget, so doing it per
        # window is quadratic. See the client's `_apply_theme`.
        if name != active_theme() or not QApplication.instance().styleSheet():
            apply_theme(QApplication.instance(), name)
        applied = active_theme()
        for action in self._theme_actions.actions():
            action.setChecked(action.data() == applied)
        self._pipeline.retheme()
        self._audio_meter.update()
        self.update()

    def _set_status(self, text: str) -> None:
        self.statusBar().showMessage(text)
        self._state_label.setText(text)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._save_ui_into_config()
        self._stop()
        # Parented, so Qt would take it anyway -- closed explicitly so the
        # application quits on the last window rather than being held open by
        # a preview nobody can see.
        if self._preview_window is not None:
            self._preview_window.close()
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


def _spin(low: float, high: float, step: float, decimals: int) -> QDoubleSpinBox:
    spin = NoWheelDoubleSpinBox()
    spin.setRange(low, high)
    spin.setSingleStep(step)
    spin.setDecimals(decimals)
    return spin


def _readout() -> QLabel:
    label = QLabel("")
    label.setProperty("role", "muted")
    label.setWordWrap(True)
    # Hidden until it has something to say, so an empty readout does not
    # leave a blank line under every Auto switch.
    label.setVisible(False)
    return label


def _say(label: QLabel, text: str) -> None:
    label.setText(text)
    label.setVisible(bool(text))


def _disclosure(title: str, content: QWidget) -> QWidget:
    """A heading that folds its content away. Qt has no <details>.

    Folded by default: most operators want the switches, and a dozen spin
    boxes open at once reads as a machine that needs configuring before it
    works.
    """
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    button = QToolButton()
    button.setText(title)
    button.setCheckable(True)
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    button.setArrowType(Qt.ArrowType.RightArrow)
    button.setAutoRaise(True)
    content.setVisible(False)

    def toggled(opened: bool) -> None:
        content.setVisible(opened)
        button.setArrowType(Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow)

    button.toggled.connect(toggled)
    layout.addWidget(button)
    layout.addWidget(content)
    return holder


def _set_value(widget, value) -> None:
    if isinstance(widget, QCheckBox):
        widget.setChecked(bool(value))
    elif isinstance(widget, QSpinBox):
        widget.setValue(int(value))
    elif isinstance(widget, QDoubleSpinBox):
        widget.setValue(float(value))


def _get_value(widget):
    if isinstance(widget, QCheckBox):
        return widget.isChecked()
    return widget.value()


def _learned_sentence(what: str, split: dict, identity: dict) -> str:
    """One line beside an Auto switch. Pure, for the tests."""
    if what == "hold":
        hold = split.get("hold")
        if hold is None:
            seen = split.get("seam_samples")
            return f"Learning — {seen} of 30 samples of a seam so far." if seen is not None else ""
        return f"Learned {float(hold):.2f} this session."
    if what == "leave":
        dip = int(split.get("longest_dip") or 0)
        if "leave_in_force" not in split:
            return ""
        return (
            f"Leaving after {split['leave_in_force']} checks"
            + (f"; longest recovered gap {dip}." if dip else ".")
        )
    if what == "anchor":
        anchors = identity.get("anchors") or {}
        known = [(r, v) for r, v in sorted(anchors.items()) if isinstance(v, (list, tuple))]
        if not known:
            return "Learning where each camera keeps its player." if "anchors" in identity else ""
        return "Learned: " + "   ".join(
            f"{region.replace('_', ' ')} {float(v[0]):.2f}, {float(v[1]):.2f}"
            for region, v in known
        )
    if what == "score":
        floor = identity.get("score_floor")
        if floor is None:
            now = identity.get("score_floor_in_force")
            return f"Learning — tracking from {float(now):.2f} meanwhile." if now is not None else ""
        return f"Learned {float(floor):.2f} this session."
    return ""


def _model_sentence(report: dict) -> str:
    """What is in the model folder, in a sentence. Pure, for the tests."""
    if not report.get("runtime"):
        return (
            "Needs the onnxruntime package: "
            'pip install "remote-bluetooth-game-control[playervision]"'
        )
    if report.get("detector"):
        files = {entry.get("file"): entry for entry in report.get("files", [])}
        if (files.get("embedder.onnx") or {}).get("present"):
            return "Detector and appearance model installed."
        return (
            "Detector installed; no appearance model, so players are not "
            "recognised in other viewports."
        )
    megabytes = round(int(report.get("download_bytes") or 0) / 1_000_000)
    return f"No model yet — Download model fetches about {megabytes} MB."


def _player_state_sentence(report: dict | None, allowed: bool, asked: bool) -> str:
    """What identification is doing right now, in a sentence. Pure, for the tests.

    Ordered from the switch furthest away to the fault nearest, so the sentence
    names the first thing actually in the way -- the vaguest true statement
    ("not running") would otherwise mask the useful one.
    """
    if not allowed:
        return "Off — not allowed on this computer (the box above)."
    if not asked:
        return (
            "Off — the Bluetooth server has not asked for it (Identify players, "
            "on its Controllers page)."
        )
    if not report:
        return "Asked for — waiting for the first frame."
    if not report.get("available"):
        return f"Not running: {report.get('reason') or 'no reason given'}"
    if report.get("starting"):
        return "Starting the model…"
    if report.get("failed"):
        return f"Stopped after repeated failures: {report['failed']}"
    where = f" on {report['device']}" if report.get("device") else ""
    return f"Running{where} — {int(report.get('samples') or 0)} frames analysed."


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
    # Fusion plus the product stylesheet, the same call the client makes, so
    # the two applications are the same material and the same colours.
    apply_theme(app, config.theme)
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

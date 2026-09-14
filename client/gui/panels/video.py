"""Optional GPU video enhancement, and the hardware decoder.

Construction only, like the other panels: every handler stays on the window.

WHAT THIS PANEL HAS TO GET RIGHT, and it is not the layout
-----------------------------------------------------------
**A control that cannot work must not be selectable**, and it must say why in
a sentence written for a player. "Requires a supported NVIDIA RTX GPU" is
useful; an HRESULT is not, and neither is silence -- a greyed-out radio with
no explanation reads as the application being broken rather than the hardware
being wrong.

**The scan is not free.** Working out what this machine can do means creating
a graphics device, asking it questions, tearing it down, and encoding and
decoding a test stream: 50-150 ms. On the GUI thread that is a visible stall
at startup, so the controls begin disabled and saying so, and the window fills
them in when the answer lands from a worker thread.

**The player's choice outlives hardware that cannot honour it.** Nothing here
writes the config when a mode turns out to be unavailable; the saved value
stays, the mode runs as Off, and it comes back when the client is opened on a
machine that can do it. See `client.media.upscale.effective_mode`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from client.media.upscale import MODE_LABELS
from qtui.widgets import NoWheelComboBox

__all__ = ["VideoPanel"]


class ModeRow(QWidget):
    """One upscaler choice: a radio, and a line saying what it costs or why not."""

    def __init__(self, mode: str, label: str, parent=None) -> None:
        super().__init__(parent)
        self.mode = mode

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 2)
        column.setSpacing(0)

        self.radio = QRadioButton(label)
        column.addWidget(self.radio)

        # Indented under the radio so it reads as belonging to it rather than
        # as a separate setting.
        self.detail = QLabel("Detecting...")
        self.detail.setWordWrap(True)
        self.detail.setProperty("role", "caption")
        self.detail.setContentsMargins(22, 0, 0, 0)
        column.addWidget(self.detail)

    def set_state(self, *, available: bool, detail: str) -> None:
        self.radio.setEnabled(available)
        self.detail.setText(detail)
        # Dimmed rather than hidden: the reason is the useful part, and a
        # control that vanishes tells the player nothing about why.
        self.detail.setEnabled(available)


class VideoPanel(QGroupBox):
    """The video group: hardware decoding, upscaling, and FSR's sharpness."""

    def __init__(self, window, parent=None) -> None:
        super().__init__("Video", parent)
        outer = QVBoxLayout(self)

        # -- hardware decoding ---------------------------------------------
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.hw_decode = NoWheelComboBox()
        self.hw_decode.addItem("Off (decode on the CPU)", "off")
        self.hw_decode.addItem("Automatic (decode on the GPU)", "auto")
        self.hw_decode.setEnabled(False)
        form.addRow("Hardware decoding", self.hw_decode)

        self.hw_detail = QLabel("Detecting...")
        self.hw_detail.setWordWrap(True)
        self.hw_detail.setProperty("role", "caption")
        form.addRow("", self.hw_detail)
        outer.addLayout(form)

        # -- the upscaler ---------------------------------------------------
        heading = QLabel("Video upscaling")
        heading.setProperty("role", "caption")
        outer.addWidget(heading)

        # One button group, so RTX VSR and FSR 1 can never both be active --
        # the requirement, enforced by construction rather than by a check.
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.rows: dict[str, ModeRow] = {}
        for mode, label in MODE_LABELS:
            row = ModeRow(mode, label)
            self.rows[mode] = row
            self.group.addButton(row.radio)
            outer.addWidget(row)

        # -- sharpness ------------------------------------------------------
        sharpness = QHBoxLayout()
        self.sharpness_label = QLabel("FSR sharpness")
        sharpness.addWidget(self.sharpness_label)
        self.sharpness = QSlider(Qt.Orientation.Horizontal)
        self.sharpness.setRange(0, 100)
        self.sharpness.setFixedWidth(180)
        sharpness.addWidget(self.sharpness)
        self.sharpness_value = QLabel("50%")
        self.sharpness_value.setFixedWidth(44)
        sharpness.addWidget(self.sharpness_value)
        sharpness.addStretch(1)
        outer.addLayout(sharpness)

        # -- what the renderer is actually doing -----------------------------
        #
        # Not what was asked for. A mode that has quietly fallen back is
        # otherwise indistinguishable from one that is working, which is the
        # failure this project keeps rediscovering.
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setProperty("role", "caption")
        outer.addWidget(self.status)

        self.set_detecting()

        self.hw_decode.currentIndexChanged.connect(window._on_hw_decode_changed)
        for row in self.rows.values():
            row.radio.toggled.connect(window._on_upscaler_changed)
        self.sharpness.valueChanged.connect(window._on_sharpness_changed)

    # -- state -------------------------------------------------------------

    def set_detecting(self) -> None:
        """Before the scan lands. Only Off is selectable, because only Off is
        known to work on every machine."""
        for mode, row in self.rows.items():
            if mode == "off":
                row.set_state(available=True,
                              detail="No enhancement - the picture as it arrives")
            else:
                row.set_state(available=False, detail="Detecting...")
        self.sharpness.setEnabled(False)
        self.sharpness_label.setEnabled(False)

    def apply_capabilities(self, caps) -> None:
        """Fill in what this machine can do, once the scan has run."""
        for mode, row in self.rows.items():
            row.set_state(available=caps.supports(mode), detail=caps.detail(mode))

        self.hw_decode.setEnabled(caps.hw_decode_ok)
        if caps.hw_decode_ok:
            self.hw_detail.setText(f"Available - {caps.hw_decode_device}")
        else:
            self.hw_detail.setText(
                caps.reason_hw_decode or "Not available on this computer")
            # Forced back to Off rather than left showing a selection the
            # machine cannot honour.
            self.hw_decode.setCurrentIndex(0)

    def selected_mode(self) -> str:
        for mode, row in self.rows.items():
            if row.radio.isChecked():
                return mode
        return "off"

    def select(self, mode: str) -> None:
        row = self.rows.get(mode) or self.rows["off"]
        row.radio.setChecked(True)

    def sync_sharpness_enabled(self) -> None:
        """The slider belongs to FSR 1 and is meaningless anywhere else."""
        enabled = self.selected_mode() == "fsr1" and self.rows["fsr1"].radio.isEnabled()
        self.sharpness.setEnabled(enabled)
        self.sharpness_label.setEnabled(enabled)
        self.sharpness_value.setEnabled(enabled)

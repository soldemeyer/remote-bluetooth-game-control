"""The controller table: which gamepad drives which slot, and how.

Nine columns, four rows, and a control in almost every cell. The column
indices live here rather than in the window because they describe this
table -- the window needs only the two it writes into from its tick.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from client.config import MAX_CONTROLLERS
from client.gui.controller_layouts import LAYOUTS
from common.design.tokens import Space

__all__ = [
    "COL_CONFIG", "COL_CONFIGURE", "COL_COUNT", "COL_GAMEPAD", "COL_NAME",
    "COL_RUMBLE", "COL_SLOT", "COL_STATUS", "COL_TYPE", "COL_USE",
    "ControllersPanel",
]

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


def _center(widget) -> QWidget:
    """A checkbox centred in its cell.

    A bare checkbox as a cell widget sits hard against the left edge, which
    reads as belonging to the column before it.
    """
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(widget)
    return container


class ControllersPanel(QGroupBox):
    """The controllers group.

    `window` supplies the handlers: every signal here is connected to a method
    that already lived on the window, so this move changes where the widgets
    are built and nothing about what they do.
    """

    def __init__(self, window, parent=None) -> None:
        super().__init__("Controllers", parent)
        layout = QVBoxLayout(self)

        hint = QLabel(
            "Enable a controller, give it a player name, and pick which gamepad "
            "it uses. Slots beyond the server's capacity are disabled."
        )
        hint.setWordWrap(True)
        hint.setProperty("role", "muted")
        layout.addWidget(hint)

        self.table = QTableWidget(MAX_CONTROLLERS, COL_COUNT)
        self.table.setHorizontalHeaderLabels(
            [
                "Use", "Slot", "Player name", "Gamepad",
                "Configuration", "Controller type", "", "Rumble", "Status",
            ]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)

        header = self.table.horizontalHeader()
        for column in (COL_USE, COL_SLOT, COL_CONFIGURE, COL_RUMBLE, COL_STATUS):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        # Interactive, not Stretch: Stretch sizes to the viewport, so the
        # columns would be squeezed to fit however narrow the drawer is and
        # the sideways scrollbar could never appear. These get the width their
        # contents need and the table scrolls.
        for column in (COL_NAME, COL_GAMEPAD, COL_CONFIG, COL_TYPE):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)

        self.enable_boxes: list[QCheckBox] = []
        self.username_edits: list[QLineEdit] = []
        self.device_combos: list[QComboBox] = []
        self.config_combos: list[QComboBox] = []
        self.type_combos: list[QComboBox] = []
        self.rumble_boxes: list[QCheckBox] = []

        for row in range(MAX_CONTROLLERS):
            enable = QCheckBox()
            enable.stateChanged.connect(window._on_slot_toggled)
            self.table.setCellWidget(row, COL_USE, _center(enable))
            self.enable_boxes.append(enable)

            slot_item = QTableWidgetItem(str(row))
            slot_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, COL_SLOT, slot_item)

            username = QLineEdit()
            username.setPlaceholderText(f"Player {row + 1}")
            username.editingFinished.connect(window._on_username_changed)
            self.table.setCellWidget(row, COL_NAME, username)
            self.username_edits.append(username)

            combo = QComboBox()
            combo.currentIndexChanged.connect(
                lambda _=0, r=row: window._on_slot_device_changed(r)
            )
            self.table.setCellWidget(row, COL_GAMEPAD, combo)
            self.device_combos.append(combo)

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
                lambda _=0, r=row: window._on_configuration_changed(r)
            )
            self.table.setCellWidget(row, COL_CONFIG, configuration)
            self.config_combos.append(configuration)

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
                lambda _=0, r=row: window._on_type_changed(r)
            )
            self.table.setCellWidget(row, COL_TYPE, controller_type)
            self.type_combos.append(controller_type)

            configure = QPushButton("Configure…")
            configure.clicked.connect(lambda _=False, r=row: window._on_configure_slot(r))
            self.table.setCellWidget(row, COL_CONFIGURE, configure)

            rumble = QCheckBox()
            rumble.setToolTip(
                "Play console rumble on this controller.\n\n"
                "The client-wide switch still applies: a slot cannot opt in "
                "while rumble is off for the whole client."
            )
            rumble.stateChanged.connect(window._on_rumble_toggled)
            self.table.setCellWidget(row, COL_RUMBLE, _center(rumble))
            self.rumble_boxes.append(rumble)

            status_item = QTableWidgetItem("—")
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, COL_STATUS, status_item)

        # Rows do not grow to fit the widgets put inside them: a table keeps
        # its default section height whatever `setCellWidget` is handed, so a
        # control taller than that is clipped -- checkboxes reduced to a
        # sliver, a button reading "onfigure.". It only became visible when the
        # theme gave controls their proper touch height, but the table was
        # always one stylesheet away from it.
        self.table.resizeRowsToContents()
        self.table.resizeColumnsToContents()
        # Nine columns do not fit beside the picture, and squeezing them makes
        # every one useless rather than one of them absent. The table scrolls
        # sideways instead -- so the drawer stays a fixed, predictable width
        # and the columns keep the sizes that make them readable.
        header.setMinimumSectionSize(48)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Tall enough for every row plus the header and the sideways scrollbar,
        # so the panel never puts a vertical scrollbar over four rows and hides
        # the fourth controller.
        self.table.setFixedHeight(
            self.table.horizontalHeader().height()
            + sum(self.table.rowHeight(r) for r in range(MAX_CONTROLLERS))
            + self.table.horizontalScrollBar().sizeHint().height()
            + 2 * self.table.frameWidth()
        )

        layout.addWidget(self.table)

        actions = QHBoxLayout()
        refresh = QPushButton("Refresh gamepad list")
        refresh.clicked.connect(window._refresh_devices)

        self.capture = QCheckBox("Capture keyboard")
        self.capture.setToolTip(
            "Send keystrokes to the controller instead of typing them.\n\n"
            "Armed, every key goes to whichever slot uses the Keyboard, and "
            "nothing can be typed into this window. Press Esc to release.\n\n"
            "Gamepads never need this -- they work in the background."
        )
        self.capture.toggled.connect(window._on_capture_toggled)

        self.capture_hint = QLabel("Keys type normally")
        self.capture_hint.setProperty("role", "muted")

        manage_configs = QPushButton("Manage configurations…")
        manage_configs.setToolTip(
            "Edit, rename, delete, export or import your saved controller "
            "configurations."
        )
        manage_configs.clicked.connect(window._on_manage_configurations)

        self.rumble = QCheckBox("Rumble")
        self.rumble.setToolTip(
            "Play rumble sent back from the console.\n\n"
            "Turning this off tells the server to stop sending it, so no rumble "
            "data crosses the network at all -- it is not a local mute.\n\n"
            "Each controller has its own switch too, and the server has one; "
            "all of them must be on."
        )
        self.rumble.stateChanged.connect(window._on_rumble_toggled)

        # Two rows, not one. Five controls in a single line needed 743px of
        # minimum width -- more than the whole drawer -- so the panel could not
        # shrink to fit beside the picture and quietly clipped its own right
        # edge instead. Same controls, same order, one wrap.
        actions.addWidget(refresh)
        actions.addWidget(manage_configs)
        actions.addStretch(1)
        self.capacity_label = QLabel("")
        self.capacity_label.setProperty("role", "muted")
        actions.addWidget(self.capacity_label)
        layout.addLayout(actions)

        toggles = QHBoxLayout()
        toggles.addWidget(self.capture)
        toggles.addWidget(self.capture_hint)
        toggles.addSpacing(Space.LG)
        toggles.addWidget(self.rumble)
        toggles.addStretch(1)
        layout.addLayout(toggles)

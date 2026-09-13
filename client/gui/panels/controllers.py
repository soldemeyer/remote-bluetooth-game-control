"""The controller table: which gamepad drives which slot.

**Six columns, and the count is the point.** It had nine, which needed 1363px
of column width in a drawer whose viewport is 594 -- so the table scrolled
sideways, and three of its columns were reachable only by dragging a scrollbar
that sat under the fourth row.

Three of those nine were setup rather than identity: which saved configuration
a slot loads, which controller type its bindings are laid out for, and whether
it plays rumble. All three belong to the slot's setup, are changed rarely, and
now live in the Configure window -- which is the one place that already showed
what they do, since it draws the pad and lists the bindings the type decides.

What is left is what the table is for: is this slot in use, which player, which
gamepad, is it working, and a way in to the rest.

`window` supplies the handlers: every signal here is connected to a method on
the window, so this file decides what the table looks like and nothing about
what it does.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from client.config import MAX_CONTROLLERS
from common.design.tokens import Space
from qtui.buttons import IconButton

__all__ = [
    "COL_CONFIGURE", "COL_COUNT", "COL_GAMEPAD", "COL_NAME", "COL_SLOT",
    "COL_STATUS", "COL_USE", "ControllersPanel",
]

COL_USE = 0
COL_SLOT = 1
COL_NAME = 2
COL_GAMEPAD = 3
COL_STATUS = 4
COL_CONFIGURE = 5
COL_COUNT = 6


class _SlotTable(QTableWidget):
    """A table exactly as tall as its rows, recomputed whenever they change.

    The height used to be measured once in the constructor, with
    `resizeRowsToContents()` and a sum of `rowHeight()`. That runs before the
    theme's stylesheet reaches the widget, so every row was measured at its
    unstyled height and the table came out short -- the fourth controller was
    cut in half, and it was the *fourth*, which is the one nobody has plugged
    in yet when they are checking whether the window works.

    A size hint is asked for again after every style and font change, so this
    cannot go stale the way a number written once can.
    """

    def _content_height(self) -> int:
        rows = self.rowCount()
        chrome = self.horizontalHeader().height() + 2 * self.frameWidth()
        if rows == 0:
            return chrome
        # The *actual* bottom of the last row, not a sum of ideal row heights:
        # `sizeHintForRow` is what a row would like, and the rows are already
        # at whatever `resizeRowsToContents` gave them, so summing hints left a
        # blank strip under the fourth controller.
        last = rows - 1
        return chrome + self.rowViewportPosition(last) + self.rowHeight(last)

    def sizeHint(self):  # noqa: N802 - Qt naming
        hint = super().sizeHint()
        hint.setHeight(self._content_height())
        return hint

    def minimumSizeHint(self):  # noqa: N802 - Qt naming
        # The same height, so a layout under pressure shrinks something else.
        # Without it the table is the first thing squeezed, and what that
        # looks like is the last row disappearing.
        hint = super().minimumSizeHint()
        hint.setHeight(self._content_height())
        return hint

    def changeEvent(self, event):  # noqa: N802 - Qt naming
        """Re-measure the rows whenever what decides their height changes.

        The rows are first sized in the constructor, which runs before the
        theme's stylesheet reaches this widget -- so they are measured
        unstyled, and the table ends up too short for its own contents. A
        style or font change is exactly the event that invalidates them.
        """
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.StyleChange, QEvent.Type.FontChange,
            QEvent.Type.ApplicationFontChange,
        ):
            self.resizeRowsToContents()
            self.updateGeometry()


def _center(widget) -> QWidget:
    """A control centred in its cell.

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
    """The controllers group."""

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

        self.table = _SlotTable(MAX_CONTROLLERS, COL_COUNT)
        self.table.setHorizontalHeaderLabels(
            ["Use", "Slot", "Player name", "Gamepad", "Status", ""]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)

        header = self.table.horizontalHeader()
        for column in (COL_USE, COL_SLOT, COL_STATUS, COL_CONFIGURE):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        # **Stretch, where the nine-column table used Interactive.** With nine
        # columns nothing could make them fit, so each took the width its
        # contents wanted and the table scrolled. Six fit, so these two divide
        # whatever is left over -- which is the whole reason for the count.
        for column in (COL_NAME, COL_GAMEPAD):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)

        self.enable_boxes: list[QCheckBox] = []
        self.username_edits: list[QLineEdit] = []
        self.device_combos: list[QComboBox] = []
        self.configure_buttons: list[IconButton] = []

        for row in range(MAX_CONTROLLERS):
            enable = QCheckBox()
            enable.stateChanged.connect(window._on_slot_toggled)
            self.table.setCellWidget(row, COL_USE, _center(enable))
            self.enable_boxes.append(enable)

            # **1 to 4, not 0 to 3.** The row index is ours; the player counts
            # from one, and so do the placeholder in the next column and the
            # "Player 1" the server prints on its adapter card. On the wire it
            # is still slot 0 -- this is the only place the two differ, which
            # is why it is written down rather than left to be noticed.
            slot_item = QTableWidgetItem(str(row + 1))
            slot_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, COL_SLOT, slot_item)

            username = QLineEdit()
            username.setPlaceholderText(f"Player {row + 1}")
            username.editingFinished.connect(window._on_username_changed)
            self.table.setCellWidget(row, COL_NAME, username)
            self.username_edits.append(username)

            combo = QComboBox()
            # Pad names are long and this column is not. Without a cap the
            # combo's own size hint sets the table's width and the table sets
            # the drawer's, so the name elides here and lives in full in the
            # tooltip.
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(8)
            combo.currentIndexChanged.connect(
                lambda _=0, r=row: window._on_slot_device_changed(r)
            )
            self.table.setCellWidget(row, COL_GAMEPAD, combo)
            self.device_combos.append(combo)

            status_item = QTableWidgetItem("—")
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, COL_STATUS, status_item)

            # An icon, because "Configure…" was 185px of a table with none to
            # spare. The tooltip is required rather than decorative: it is also
            # the accessible name, and an icon alone says nothing to a screen
            # reader.
            configure = IconButton(
                "settings",
                f"Set up controller {row + 1} — bindings, controller type, "
                "which saved configuration it uses, and rumble",
            )
            configure.clicked.connect(lambda _=False, r=row: window._on_configure_slot(r))
            self.table.setCellWidget(row, COL_CONFIGURE, _center(configure))
            self.configure_buttons.append(configure)

        # Rows do not grow to fit the widgets put inside them: a table keeps
        # its default section height whatever `setCellWidget` is handed, so a
        # control taller than that is clipped -- checkboxes reduced to a
        # sliver, a button reading "onfigure.". It only became visible when the
        # theme gave controls their proper touch height, but the table was
        # always one stylesheet away from it.
        self.table.resizeRowsToContents()
        header.setMinimumSectionSize(40)
        # **Neither scrollbar, ever.** Six columns fit and the table is given
        # exactly the height its rows need, so a scrollbar appearing here would
        # mean something above it is wrong -- not that the player has more to
        # look at.
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # A table's default hint is wide enough for every column at its
        # contents width, which is the thing that used to make the drawer's
        # cards wider than the drawer. It is told to ask for nothing.
        self.table.setSizeAdjustPolicy(
            QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored
        )
        # Fixed *vertically* only -- the width still follows the card, and it
        # is `_SlotTable` that decides the height.
        self.table.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
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
            "Each controller has its own switch too, in its Configure window, "
            "and the server has one; all of them must be on."
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

"""The controller table: which gamepad drives which slot.

**Six columns, and the count is the point.** It had nine, which needed 1363px
of column width in a drawer whose viewport is 594 -- so the table scrolled
sideways, and three of its columns were reachable only by dragging a scrollbar
that sat under the fourth row.

Which saved configuration a slot loads and whether it plays rumble moved into
the Configure window, which is the one place that already showed what they do.
The player names moved into their own card: a name is the person rather than a
property of the slot, and it needed a field wide enough to type into.

**The controller type stayed.** It was tried in the Configure window with the
other two and came back, because it is not the same kind of setting: it decides
what the pad in this row *is*, it is the thing somebody changes when swapping a
pad between games, and it belongs beside the gamepad it describes.

What is left is what the table is for: is this slot in use, which gamepad,
what kind of controller it is, is it working, and a way in to the rest.

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
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from client.config import MAX_CONTROLLERS
from client.gui.controller_layouts import LAYOUTS
from common.design.tokens import Space
from PySide6.QtGui import QBrush

from qtui.buttons import IconButton
from qtui.theme import qcolor
from qtui.widgets import NoWheelComboBox, cap_combo_width

__all__ = [
    "COL_CONFIGURE", "COL_COUNT", "COL_GAMEPAD", "COL_SLOT", "COL_STATUS",
    "COL_TYPE", "COL_USE", "ControllersPanel",
]

COL_USE = 0
COL_SLOT = 1
COL_GAMEPAD = 2
COL_TYPE = 3
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


#: Breathing room around a control inside a table cell, horizontal and
#: vertical. An item view puts a `setCellWidget` widget in the item's whole
#: rect, so without this the control's border *is* the cell border and the row
#: reads as a solid block of controls with the grid drawn through it. The
#: margin cannot go on `QTableWidget::item` -- padding there shrinks the
#: widget by twice its value and clips the label, which the theme records.
_CELL_PAD_H = 6
_CELL_PAD_V = 4


def _cell(widget, *, center: bool = False) -> QWidget:
    """A control inside a cell, with room around it.

    Centring as well for the narrow columns: a bare checkbox as a cell widget
    sits hard against the left edge, which reads as belonging to the column
    before it.
    """
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(_CELL_PAD_H, _CELL_PAD_V, _CELL_PAD_H, _CELL_PAD_V)
    if center:
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(widget)
    return container


class ControllersPanel(QGroupBox):
    """The controllers group."""

    def __init__(self, window, parent=None) -> None:
        super().__init__("Controllers", parent)
        layout = QVBoxLayout(self)

        hint = QLabel(
            "Enable a controller, pick which gamepad it uses, and say what "
            "kind of controller it should behave as. Slots beyond the "
            "server's capacity are disabled."
        )
        hint.setWordWrap(True)
        hint.setProperty("role", "muted")
        layout.addWidget(hint)

        self.table = _SlotTable(MAX_CONTROLLERS, COL_COUNT)
        self.table.setHorizontalHeaderLabels(
            ["Use", "Slot", "Gamepad", "Controller type", "Status", ""]
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
        for column in (COL_GAMEPAD, COL_TYPE):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)

        self.enable_boxes: list[QCheckBox] = []
        self.device_combos: list[QComboBox] = []
        self.type_combos: list[QComboBox] = []
        self.configure_buttons: list[IconButton] = []

        for row in range(MAX_CONTROLLERS):
            enable = QCheckBox()
            enable.stateChanged.connect(window._on_slot_toggled)
            self.table.setCellWidget(row, COL_USE, _cell(enable, center=True))
            self.enable_boxes.append(enable)

            # **1 to 4, not 0 to 3.** The row index is ours; the player counts
            # from one, and so do the placeholder in the next column and the
            # "Player 1" the server prints on its adapter card. On the wire it
            # is still slot 0 -- this is the only place the two differ, which
            # is why it is written down rather than left to be noticed.
            slot_item = QTableWidgetItem(str(row + 1))
            slot_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, COL_SLOT, slot_item)

            combo = NoWheelComboBox()
            # Pad names are long and this column is not. Without a cap the
            # combo's own size hint sets the table's width and the table sets
            # the drawer's, so the name elides here and lives in full in the
            # tooltip.
            cap_combo_width(combo, 8)
            combo.currentIndexChanged.connect(
                lambda _=0, r=row: window._on_slot_device_changed(r)
            )
            self.table.setCellWidget(row, COL_GAMEPAD, _cell(combo))
            self.device_combos.append(combo)

            # Which controller type's bindings this slot uses. Per slot rather
            # than on the configuration: slots reference configurations by
            # name, so storing it there meant two slots sharing one fought
            # over the setting.
            controller_type = NoWheelComboBox()
            controller_type.setToolTip(
                "Which controller this slot's bindings are laid out for.\n\n"
                "Changes what the buttons are called and what the preview "
                "shows. It does not change what the server emulates."
            )
            # Capped for the same reason as the gamepad list beside it: this
            # one holds "Nintendo Switch 2", and an uncapped combo's own hint
            # sets the table's width and the table sets the card's.
            for entry in LAYOUTS:
                controller_type.addItem(entry.name, entry.key)
            cap_combo_width(controller_type, 8)
            controller_type.currentIndexChanged.connect(
                lambda _=0, r=row: window._on_type_changed(r)
            )
            self.table.setCellWidget(row, COL_TYPE, _cell(controller_type))
            self.type_combos.append(controller_type)

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
            self.table.setCellWidget(row, COL_CONFIGURE, _cell(configure, center=True))
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

        #: Every widget that should look switched off when its row is locked
        #: out. Collected once: walking the cells on each status tick would be
        #: 24 lookups ten times a second for a flag that changes twice a
        #: session.
        self._row_widgets: list[list[QWidget]] = [
            [
                self.table.cellWidget(row, column)
                for column in range(COL_COUNT)
                if self.table.cellWidget(row, column) is not None
            ]
            + [self.device_combos[row], self.type_combos[row],
               self.configure_buttons[row]]
            for row in range(MAX_CONTROLLERS)
        ]

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

        # **Three rows, and the third one is why.** Measured with a server
        # found and a pad chosen -- which is the state the fault was reported
        # in, and not the state an empty window is in:
        #
        #   Refresh gamepad list             174
        #   Manage configurations...         198
        #   Server capacity: 4 controller(s) 176
        #                                    --- 560, in a 604px viewport
        #
        # The capacity label is **empty until a server is found**, which is
        # exactly why measuring a freshly opened window says this fits. It does
        # not: the card was 612 wide and the drawer clipped it.
        #
        # The two readouts share the last line and wrap, so neither can widen
        # the card again as its text changes.
        actions.addWidget(refresh)
        actions.addWidget(manage_configs)
        actions.addStretch(1)
        layout.addLayout(actions)

        toggles = QHBoxLayout()
        toggles.addWidget(self.capture)
        toggles.addStretch(1)
        toggles.addWidget(self.rumble)
        layout.addLayout(toggles)

        self.capture_hint.setWordWrap(True)
        self.capacity_label = QLabel("")
        self.capacity_label.setProperty("role", "muted")
        # **Not word-wrapped.** A wrapping label's `sizeHint` aims for a
        # squarish box rather than one line, so this one broke "Server
        # capacity: 4 controller(s)" across two lines in a row with 250px
        # spare. Wrapping was how it was stopped from *widening the card*, and
        # sharing a row with nothing but the capture hint does that instead:
        # 110 + 176 against a 604px viewport.
        # The stretch goes on the *left* label rather than between them: a
        # wrapping label whose minimum is one word long will be squeezed to it
        # by a stretch item, so "Server capacity: 4 controller(s)" wrapped
        # across two lines in a card with 250px to spare.
        readouts = QHBoxLayout()
        readouts.addWidget(self.capture_hint, 1)
        readouts.addWidget(self.capacity_label, 0)
        layout.addLayout(readouts)

    def set_row_locked(self, row: int, locked: bool) -> None:
        """Mark a row as out of play for this session.

        Qt's disabled state only dims text, which against this backdrop is a
        difference of a few percent -- so a controller the session cannot use
        looked identical to one it could. The property drives a stylesheet
        rule; the unpolish/polish pair is what makes Qt re-evaluate it, since
        a dynamic property change does not restyle a widget by itself.
        """
        for widget in self._row_widgets[row]:
            if widget.property("locked") == locked:
                continue
            widget.setProperty("locked", locked)
            widget.style().unpolish(widget)
            widget.style().polish(widget)

        # Slot and Status hold *items*, not widgets, so no stylesheet reaches
        # them -- and with only the widget cells darkened the row came out
        # striped, which reads as a rendering fault rather than as a state.
        # The same token the rule uses, so the row is one colour.
        fill = qcolor("background-sunken") if locked else QBrush()
        for column in (COL_SLOT, COL_STATUS):
            item = self.table.item(row, column)
            if item is not None:
                item.setBackground(fill)


"""Who is playing: one name per controller slot.

Split out of the controller table, which had to carry a text field wide enough
to type a name into next to four other columns. A name is not really a property
of a *slot* in the way a gamepad is -- it is the person -- and it is set once at
the start of an evening and then left alone, where the rest of that table is
what somebody fiddles with while setting a console up.

The slot numbering is the table's: **1 to 4**, matching the Slot column, the
placeholder text, and the "Player 1" the server prints on its adapter card. On
the wire the same controller is still slot 0.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from client.config import MAX_CONTROLLERS

__all__ = ["PlayersPanel"]

#: Slots per row. Two columns of two: four stacked rows is a tall card for four
#: short fields, and the drawer's height is the scarce thing here -- one card
#: open at a time is what makes a whole card visible without scrolling.
_COLUMNS = 2


class PlayersPanel(QGroupBox):
    """The player names.

    `window` supplies the handler, as every other panel does: the field's
    `editingFinished` is the window's, so a name typed during a live session
    reaches the server without a reconnect.
    """

    def __init__(self, window, parent=None) -> None:
        super().__init__("Players", parent)
        layout = QVBoxLayout(self)

        hint = QLabel(
            "The name each controller plays under. The server shows it beside "
            "the adapter that controller is using."
        )
        hint.setWordWrap(True)
        hint.setProperty("role", "muted")
        layout.addWidget(hint)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.username_edits: list[QLineEdit] = []

        for slot in range(MAX_CONTROLLERS):
            row, column = divmod(slot, _COLUMNS)

            label = QLabel(f"{slot + 1}")
            label.setProperty("role", "label")
            grid.addWidget(label, row, column * 2)

            edit = QLineEdit()
            edit.setPlaceholderText(f"Player {slot + 1}")
            edit.setToolTip(
                f"The name controller {slot + 1} plays under.\n\n"
                "It can be changed while a session is running: the server is "
                "told without a reconnect."
            )
            # **`editingFinished` only** -- Enter, or the field losing focus.
            # Pushing on every keystroke was tried and is worse: a name is
            # typed letter by letter, so the server would be told about six
            # names nobody has on the way to the one they do.
            #
            # The slot travels with it. Without that the handler had to guess
            # which name changed, and what it did instead was send all four --
            # see `_on_username_changed`.
            edit.editingFinished.connect(
                lambda s=slot: window._on_username_changed(s)
            )
            grid.addWidget(edit, row, column * 2 + 1)
            self.username_edits.append(edit)

        # The name fields take the space; the numbers beside them take none.
        for column in range(_COLUMNS):
            grid.setColumnStretch(column * 2 + 1, 1)

        layout.addLayout(grid)

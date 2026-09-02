"""Round-trip time: one card per slot, and the rolling plot."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QVBoxLayout

from client.gui.latency_plot import LatencyPlot

__all__ = ["LatencyPanel"]


class LatencyPanel(QGroupBox):
    """The latency group.

    ``cards`` is one label per slot and ``plot`` is the rolling graph; the
    window writes to both from its tick.
    """

    def __init__(self, slots: int, card_style, parent=None) -> None:
        super().__init__("Latency", parent)
        layout = QVBoxLayout(self)

        note = QLabel(
            "Round-trip time to the server. The Bluetooth hop to the console adds "
            "a further 5–15 ms that cannot be measured from here."
        )
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        layout.addWidget(note)

        #: One card per slot. The window fills them in; `card_style` is passed
        #: in rather than imported so the panel does not have to know how a
        #: reading is turned into a colour.
        self.cards: list[QLabel] = []
        row = QHBoxLayout()
        for slot in range(slots):
            label = QLabel(f"Slot {slot}\n—")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setFont(QFont("", 10))
            label.setStyleSheet(card_style(None))
            row.addWidget(label)
            self.cards.append(label)
        layout.addLayout(row)

        self.plot = LatencyPlot()
        layout.addWidget(self.plot, 1)

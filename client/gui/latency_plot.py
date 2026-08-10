"""Live latency plot.

Uses pyqtgraph when available -- it is built for streaming data and redraws far
faster than QtCharts at these update rates. Falls back to a compact text
summary if pyqtgraph is missing, so the GUI never fails to open over a missing
optional dependency.
"""

from __future__ import annotations

import logging
from collections import deque

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

log = logging.getLogger(__name__)

try:
    import pyqtgraph as pg
except ImportError:  # pragma: no cover - exercised only without pyqtgraph
    pg = None

#: Samples retained per slot. At ~10 Hz refresh this is roughly 30 seconds,
#: which is long enough to see a pattern and short enough to stay readable.
HISTORY = 300

#: One colour per slot, matching the server GUI's ordering.
SLOT_COLOURS = ("#4c8dff", "#3ecf8e", "#f5a623", "#c678dd")


class LatencyPlot(QWidget):
    """Rolling RTT plot, one series per controller slot."""

    def __init__(self, slots: int = 4) -> None:
        super().__init__()

        self._slots = slots
        self._history: dict[int, deque[float]] = {
            slot: deque(maxlen=HISTORY) for slot in range(slots)
        }
        self._dirty = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if pg is None:
            self._plot = None
            self._fallback = QLabel(
                "Install pyqtgraph for live latency graphs:\n"
                '    pip install -e ".[client]"'
            )
            self._fallback.setStyleSheet("color: #888; padding: 16px;")
            layout.addWidget(self._fallback)
            return

        pg.setConfigOptions(antialias=True)

        self._plot = pg.PlotWidget()
        self._plot.setBackground("#1a1d26")
        self._plot.showGrid(x=False, y=True, alpha=0.2)
        self._plot.setLabel("left", "RTT", units="ms")
        self._plot.setLabel("bottom", "samples")
        self._plot.addLegend(offset=(-10, 10))
        # Latency is never negative, and pinning the floor stops the view
        # jumping around as values change.
        self._plot.setYRange(0, 60)
        self._plot.setMouseEnabled(x=False, y=False)

        self._curves = {}
        for slot in range(slots):
            self._curves[slot] = self._plot.plot(
                [],
                pen=pg.mkPen(SLOT_COLOURS[slot % len(SLOT_COLOURS)], width=2),
                name=f"Slot {slot}",
            )

        layout.addWidget(self._plot)

    def add_sample(self, slot: int, rtt_ms: float) -> None:
        """Record a sample. Cheap -- drawing is deferred to refresh()."""
        if slot not in self._history or rtt_ms <= 0:
            return
        self._history[slot].append(rtt_ms)
        self._dirty = True

    def refresh(self) -> None:
        """Redraw if anything changed.

        Separating this from add_sample means several slots' samples in one
        tick produce a single redraw instead of four.
        """
        if self._plot is None or not self._dirty:
            return
        self._dirty = False

        highest = 0.0
        for slot, samples in self._history.items():
            if not samples:
                continue
            self._curves[slot].setData(list(samples))
            highest = max(highest, max(samples))

        # Grow the axis to fit spikes, but keep a sane floor so an idle LAN
        # connection does not render as a flat line filling the whole plot.
        self._plot.setYRange(0, max(60.0, highest * 1.15))

    def reset(self) -> None:
        for samples in self._history.values():
            samples.clear()
        if self._plot is not None:
            for curve in self._curves.values():
                curve.setData([])

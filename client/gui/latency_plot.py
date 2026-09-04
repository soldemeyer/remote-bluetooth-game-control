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

from common.design.tokens import Space
from qtui.theme import qcolor

log = logging.getLogger(__name__)

try:
    import pyqtgraph as pg
except ImportError:  # pragma: no cover - exercised only without pyqtgraph
    pg = None

#: Samples retained per slot. At ~10 Hz refresh this is roughly 30 seconds,
#: which is long enough to see a pattern and short enough to stay readable.
HISTORY = 300

#: One colour per slot, matching the server GUI's ordering. These were four
#: hand-copied hex values, three of which were the accent, success and warning
#: colours spelled out again -- so a palette change reached the panels and left
#: the plot behind.
SLOT_TOKENS = ("series-1", "series-2", "series-3", "series-4")


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
            self._fallback.setProperty("role", "muted")
            self._fallback.setContentsMargins(Space.LG, Space.LG, Space.LG, Space.LG)
            layout.addWidget(self._fallback)
            return

        pg.setConfigOptions(antialias=True)

        self._plot = pg.PlotWidget()
        # A splitter hands out space by minimum size, and a plot has none --
        # so a neighbouring panel growing (as the controller table did once
        # its rows fitted their controls) squeezed this to a few pixels of
        # axis with no curve visible at all.
        self._plot.setMinimumHeight(150)
        self._plot.setBackground(qcolor("surface-solid"))
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
                pen=pg.mkPen(qcolor(SLOT_TOKENS[slot % len(SLOT_TOKENS)]), width=2),
                name=f"Slot {slot}",
            )

        layout.addWidget(self._plot)

    def retheme(self) -> None:
        """Rebuild everything that cached a colour. Called on a theme change.

        pyqtgraph resolves a pen once, at creation: without this the curves
        keep the previous scheme's colours on a themed background, which looks
        like the plot failed to update rather than like a stale pen.
        """
        if self._plot is None:
            return
        self._plot.setBackground(qcolor("surface-solid"))
        for slot, curve in self._curves.items():
            curve.setPen(pg.mkPen(qcolor(SLOT_TOKENS[slot % len(SLOT_TOKENS)]), width=2))

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

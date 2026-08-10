"""HID output sink: where generated reports actually go.

Two implementations:

  * :class:`MockSink` -- records reports in memory. Lets the entire pipeline be
    developed and tested on any machine with no Bluetooth hardware, and gives
    the latency harness a ground truth.
  * ``L2CAPSink`` (server/bt/hid.py) -- the real Bluetooth path.

The datapath only ever talks to this interface, so ``--mock-bt`` is a one-line
substitution rather than a special code path threaded through the server.
"""

from __future__ import annotations

import abc
import logging
import threading
from collections import deque
from dataclasses import dataclass

from common.timing import LatencyStats, now_ns, ns_to_ms

log = logging.getLogger(__name__)


class HIDSink(abc.ABC):
    """Destination for generated HID input reports."""

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool:
        """True when a target is connected and will accept reports."""

    @abc.abstractmethod
    def send_input_report(self, report: bytes | bytearray | memoryview) -> bool:
        """Write one report. Returns False if it could not be delivered.

        Called from the datapath thread for every input packet, so it must not
        block. A full transmit queue should drop and return False rather than
        wait -- the next state supersedes this one anyway.
        """

    @abc.abstractmethod
    def close(self) -> None:
        """Tear down. Must be idempotent."""


@dataclass(slots=True)
class RecordedReport:
    """One report captured by the mock sink."""

    timestamp_ns: int
    data: bytes


class MockSink(HIDSink):
    """In-memory sink for testing and for ``--mock-bt``.

    Thread-safe: the datapath writes while tests and the web GUI read.
    """

    def __init__(self, *, name: str = "mock", history: int = 256,
                 simulate_latency_ms: float = 0.0) -> None:
        self._name = name
        self._connected = True
        self._lock = threading.Lock()
        self._reports: deque[RecordedReport] = deque(maxlen=history)
        self._count = 0
        self._write_stats = LatencyStats()

        #: Optional artificial delay, so the harness can model the real
        #: Bluetooth interval without hardware. Off by default -- an
        #: accidentally-enabled sleep on the datapath would be a nasty bug.
        self._simulate_latency_ms = simulate_latency_ms

    @property
    def is_connected(self) -> bool:
        return self._connected

    def send_input_report(self, report: bytes | bytearray | memoryview) -> bool:
        if not self._connected:
            return False

        start = now_ns()

        if self._simulate_latency_ms > 0:
            import time

            time.sleep(self._simulate_latency_ms / 1000.0)

        with self._lock:
            self._reports.append(RecordedReport(start, bytes(report)))
            self._count += 1
            self._write_stats.add(ns_to_ms(now_ns() - start))

        return True

    def close(self) -> None:
        self._connected = False

    # -- inspection --------------------------------------------------------

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def last_report(self) -> RecordedReport | None:
        with self._lock:
            return self._reports[-1] if self._reports else None

    def reports(self) -> list[RecordedReport]:
        with self._lock:
            return list(self._reports)

    def clear(self) -> None:
        with self._lock:
            self._reports.clear()
            self._count = 0
            self._write_stats.clear()

    def set_connected(self, connected: bool) -> None:
        """Simulate a console connecting or dropping."""
        self._connected = connected

    def write_stats(self) -> dict[str, float | int]:
        with self._lock:
            return self._write_stats.snapshot()

    def __repr__(self) -> str:
        return f"<MockSink {self._name} reports={self.count} connected={self._connected}>"


class NullSink(HIDSink):
    """Discards everything. Used for an adapter slot with no target assigned."""

    @property
    def is_connected(self) -> bool:
        return False

    def send_input_report(self, report: bytes | bytearray | memoryview) -> bool:
        return False

    def close(self) -> None:
        return None

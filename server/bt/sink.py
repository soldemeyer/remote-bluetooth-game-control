"""HID output sink: where generated reports actually go.

Two implementations:

  * :class:`MockSink` -- records reports in memory. Lets the entire pipeline be
    developed and tested on any machine with no Bluetooth hardware, and gives
    the latency harness a ground truth.
  * ``L2CAPSink`` (server/bt/hid.py) -- the real Bluetooth path.

The datapath only ever talks to this interface, so ``--mock-bt`` is a one-line
substitution rather than a special code path threaded through the server.

:class:`DelayLine` also lives here, because it is the shared half of sync
latency: stdlib only, no BlueZ and no D-Bus, so the rule can be tested on any
machine while the two real sinks each hold one and release from the loop they
already run.
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

    # -- sync latency ------------------------------------------------------
    #
    # Concrete rather than abstract, so NullSink and anything written later keep
    # working untouched and the datapath can call them without a hasattr dance.
    # A sink that does not implement them simply has the feature off, which is
    # the right way round for a fairness preference.

    def set_sync_delay_ns(self, delay_ns: int) -> None:
        """Hold every report this long before transmitting it.

        Zero restores the original path exactly -- the implementations drop the
        delay line object rather than keeping one set to zero, so "off" is the
        code that was there before this existed plus one `is None` test.
        """
        return None

    def discard_delayed(self) -> None:
        """Throw away anything held, without transmitting it.

        Called before a neutral report is written for a departing player: a
        neutral queued *behind* their held state would leave the console
        latching their last input for the length of the delay.
        """
        return None

    def sync_stats(self) -> dict[str, float | int] | None:
        """What the delay line is doing, or None when there is not one."""
        return None


#: Reports per second one slot can offer, for sizing a delay line's ring. A
#: client polls at up to 500 Hz and sends on change, so 1000 is generous.
_DELAY_LINE_RATE_HZ = 1000

#: Consumer gap the ring must survive without overflowing, on top of the delay
#: itself.
#:
#: **Measured, and the first sizing was wrong.** Holding `delay x rate` entries
#: looks sufficient -- steady-state depth really is `delay x offer rate`, 12
#: entries against 56 slots on the reference Pi -- but the consumer does not run
#: on a metronome. The BLE emitter paces at 100 Hz, writes to a socket, and
#: tolerates a backlogged bus for `_STALL_TICKS` before giving up; any gap longer
#: than the ring's span overflows it. Against a live Analogue 3D that was **1149
#: of 7355 states dropped in twenty seconds**, at a steady ~15%.
#:
#: And overflow is not harmless here, which is why this is sized rather than
#: merely counted. The entry dropped is the *oldest*, which is the one closest to
#: being due -- so the next release picks a newer state and comes out **younger
#: than the delay asked for**. A bigger ring instead lets those stale entries be
#: passed over as `coalesced`, which is what they are, and keeps the released
#: state exactly `delay` old.
#:
#: 250 ms at 1000 Hz is 250 slots of 64 bytes -- 16 kB per adapter, 64 kB for
#: four. Cheap enough that the honest choice is generosity.
_DELAY_LINE_GAP_S = 0.25

#: Bytes per ring slot. A generic pad's report is 10 bytes and a Switch Pro's is
#: 11; 64 leaves room for a profile nobody has written yet, and the ring grows
#: once (with a log line) if one ever exceeds it.
_DELAY_LINE_SLOT_BYTES = 64


class DelayLine:
    """A short history of states, released on a delay. Latest due wins.

    **Why a history rather than a held state.** Every sink here coalesces:
    it keeps the newest state and transmits that. Holding the newest state until
    `arrived + D` does *not* add latency -- at the moment it fires, the newest
    state is milliseconds old, so the console gets fresh input at 1/D Hz instead
    of delayed input at the rate it was sent. The update rate collapses and the
    latency does not move. To actually delay, the thing transmitted at time T
    has to be the state that was current at `T - D`, and that means remembering
    the states in between.

    The history is short: `D x offer rate`, which at the 300 ms ceiling and
    1000 Hz is a few hundred entries of 64 bytes. One flat preallocated buffer,
    so the datapath's half is a `memcpy` into a slot and two integer stores --
    **no allocation in the packet path**, per the datapath's own contract.

    Release is *newest due wins*: everything older than the newest due entry is
    counted as ``coalesced``, never ``dropped``, because a superseded state is
    the design rather than a failure. That is also what makes this compose with
    a paced sink -- a 500 Hz offer decimates to the pacing rate while the *age*
    of each released report stays at D instead of growing, which is the hidden
    queue this project has had to find twice.

    Arrival times are stored rather than due times. A constant offset on a
    monotone series stays monotone, so changing the delay can never reorder the
    queue, and *lowering* it releases what is already held rather than stranding
    it for the old duration.

    Its own lock, and it is a leaf: nothing here calls out, so nesting inside
    ``L2CAPSink._io_lock`` or ``BLESink._lock`` cannot deadlock, and three call
    sites with different lock conventions cannot get it wrong.
    """

    __slots__ = (
        "_delay_ns", "_buf", "_at", "_len", "_head", "_count", "_slots",
        "_slot_bytes", "_lock", "released", "coalesced", "dropped",
    )

    def __init__(self, *, delay_ns: int = 0, slots: int = 0,
                 slot_bytes: int = _DELAY_LINE_SLOT_BYTES) -> None:
        self._delay_ns = max(0, int(delay_ns))
        self._slots = slots if slots > 0 else self._slots_for(self._delay_ns)
        self._slot_bytes = slot_bytes
        self._buf = bytearray(self._slots * slot_bytes)
        #: Arrival timestamp per slot.
        self._at = [0] * self._slots
        #: Report length per slot.
        self._len = [0] * self._slots
        #: Index of the oldest entry.
        self._head = 0
        self._count = 0
        self._lock = threading.Lock()

        self.released = 0
        self.coalesced = 0
        self.dropped = 0

    @staticmethod
    def _slots_for(delay_ns: int) -> int:
        """Ring depth for a delay, plus headroom for a consumer gap.

        Never fewer than two, because a line with one slot cannot hold a history
        at all and the whole point is that the state released now is not the
        newest one. See `_DELAY_LINE_GAP_S` for why the headroom is not a
        multiple of the delay -- it is a property of the consumer, not of D.
        """
        span_s = delay_ns / 1e9 + _DELAY_LINE_GAP_S
        needed = int(span_s * _DELAY_LINE_RATE_HZ) + 2
        return max(2, min(4096, needed))

    # -- control plane -----------------------------------------------------

    @property
    def delay_ns(self) -> int:
        return self._delay_ns

    def set_delay_ns(self, delay_ns: int) -> bool:
        """Change the delay. True when it moved.

        Grows the ring when a longer delay needs more depth, **carrying whatever
        is held across it**. Discarding would be simpler and is wrong: on BLE
        there is no keepalive, so a state dropped here is one the console never
        receives, and it would sit on the previous one until the player next
        moved. This runs when an operator moves a slider, so the copy costs
        nothing worth saving.
        """
        delay_ns = max(0, int(delay_ns))
        with self._lock:
            if delay_ns == self._delay_ns:
                return False
            self._delay_ns = delay_ns
            wanted = self._slots_for(delay_ns)
            if wanted > self._slots:
                self._regrow_locked(wanted, self._slot_bytes)
            return True

    def _regrow_locked(self, slots: int, slot_bytes: int) -> None:
        """Move the held entries into a bigger ring, oldest first.

        Carrying them across rather than starting empty, because on BLE there is
        no keepalive: a state dropped here is one the console never receives, and
        it would sit on the previous one until the player next moved.
        """
        buf = bytearray(slots * slot_bytes)
        at = [0] * slots
        lengths = [0] * slots
        for position in range(self._count):
            source = (self._head + position) % self._slots
            length = self._len[source]
            src = source * self._slot_bytes
            dst = position * slot_bytes
            buf[dst:dst + length] = self._buf[src:src + length]
            at[position] = self._at[source]
            lengths[position] = length
        self._slots = slots
        self._slot_bytes = slot_bytes
        self._buf = buf
        self._at = at
        self._len = lengths
        self._head = 0

    def clear(self) -> None:
        """Forget everything held, counting nothing.

        Used where a state must not outlive the thing it belonged to: a console
        disconnecting, a player leaving. These are not drops -- nobody was ever
        going to see them -- so they are not counted as such.
        """
        with self._lock:
            self._head = 0
            self._count = 0

    # -- the datapath half -------------------------------------------------

    def offer(self, report: bytes | bytearray | memoryview, now_ns: int) -> bool:
        """Take one state. Returns True when the consumer needs waking.

        "Needs waking" is *the line was empty*. Arrival times are monotone, so a
        non-empty line means the consumer is already sleeping on an earlier
        deadline and will find this entry when it gets there -- which is what
        keeps a 500 Hz burst down to one wake rather than five hundred.
        """
        length = len(report)
        with self._lock:
            if length > self._slot_bytes:
                # Truncating would shift every field of a HID report with
                # nothing anywhere to say so, which is the quietest failure this
                # project knows. Grow once and say it happened.
                log.warning(
                    "Delay line slot is %d bytes and a report is %d; growing. "
                    "Raise _DELAY_LINE_SLOT_BYTES.",
                    self._slot_bytes, length,
                )
                self._regrow_locked(self._slots, length)

            was_empty = self._count == 0
            if self._count == self._slots:
                # Full. The oldest is the least useful thing here, and losing it
                # is a real drop rather than a supersede, so it is counted as
                # one: a saturated line means the ring is mis-sized or the
                # consumer has stalled, and both are worth seeing.
                self._head = (self._head + 1) % self._slots
                self._count -= 1
                self.dropped += 1

            index = (self._head + self._count) % self._slots
            start = index * self._slot_bytes
            self._buf[start:start + length] = report
            self._at[index] = now_ns
            self._len[index] = length
            self._count += 1
            return was_empty

    # -- the consumer half -------------------------------------------------

    def take_due_into(self, out: bytearray, offset: int, now_ns: int) -> int:
        """Copy the newest due state into ``out`` at ``offset``. 0 if none.

        Writes into the caller's buffer rather than returning a view of the
        ring: a view stays valid only until the producer wraps onto that slot,
        and a report torn halfway through being transmitted is indistinguishable
        on the wire from a report we meant to send.

        ``offset`` is there because the Classic path keeps its 0xA1 transaction
        header in byte 0 of the same buffer.
        """
        cutoff = now_ns - self._delay_ns
        with self._lock:
            due = 0
            while due < self._count and self._at[(self._head + due) % self._slots] <= cutoff:
                due += 1
            if due == 0:
                return 0

            index = (self._head + due - 1) % self._slots
            length = self._len[index]
            start = index * self._slot_bytes
            out[offset:offset + length] = self._buf[start:start + length]

            self._head = (self._head + due) % self._slots
            self._count -= due
            self.released += 1
            self.coalesced += due - 1
            return length

    def take_newest_into(self, out: bytearray, offset: int) -> int:
        """Copy the newest held state, due or not, and empty the line.

        For switching the delay **off**. Dropping the history instead would leave
        the console holding the state from D ago until the player next moves --
        and on BLE, which is send-on-change with no keepalive, "next moves" can
        be a long time. The ones this passes are superseded rather than dropped,
        which is what they are.
        """
        with self._lock:
            if self._count == 0:
                return 0
            index = (self._head + self._count - 1) % self._slots
            length = self._len[index]
            start = index * self._slot_bytes
            out[offset:offset + length] = self._buf[start:start + length]
            self.released += 1
            self.coalesced += self._count - 1
            self._head = 0
            self._count = 0
            return length

    def next_due_ns(self) -> int:
        """When the oldest held state becomes due. 0 when there is none."""
        with self._lock:
            if self._count == 0:
                return 0
            return self._at[self._head] + self._delay_ns

    @property
    def depth(self) -> int:
        with self._lock:
            return self._count

    def stats(self) -> dict[str, float | int]:
        with self._lock:
            return {
                "delay_ms": round(self._delay_ns / 1e6, 2),
                "depth": self._count,
                "slots": self._slots,
                "released": self.released,
                "coalesced": self.coalesced,
                "dropped": self.dropped,
            }


@dataclass(slots=True)
class RecordedReport:
    """One report captured by the mock sink."""

    timestamp_ns: int
    data: bytes


class MockSink(HIDSink):
    """In-memory sink for testing and for ``--mock-bt``.

    Thread-safe: the datapath writes while tests and the web GUI read.

    **Its delay line is pumped by hand.** The real sinks have a loop -- a writer
    thread on Classic, an asyncio task on BLE -- and this has neither, so a
    steady stream self-drains by pumping on the way in and anything else needs
    an explicit :meth:`pump`. That makes this the right tool for checking
    ordering and arithmetic and the wrong one for measuring jitter: the release
    happens when somebody calls, not when it is due.
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

        #: None when sync latency is off, which is the ordinary case.
        self._line: DelayLine | None = None
        self._scratch = bytearray(_DELAY_LINE_SLOT_BYTES)

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

        line = self._line
        if line is not None:
            # Pump first, so a steady stream drains on its own: what goes out
            # now is whatever became due since the last offer.
            self.pump(start)
            line.offer(report, start)
            return True

        self._record(report, start)
        return True

    def _record(self, report: bytes | bytearray | memoryview, start: int) -> None:
        with self._lock:
            self._reports.append(RecordedReport(start, bytes(report)))
            self._count += 1
            self._write_stats.add(ns_to_ms(now_ns() - start))

    def pump(self, at_ns: int | None = None) -> int:
        """Release whatever is due. Returns how many reports went out.

        Loops rather than releasing once, so a test can advance a long way in
        one call -- each pass takes the newest state due at that moment, and the
        ones it passes are counted as superseded by the line itself.
        """
        line = self._line
        if line is None:
            return 0
        now = now_ns() if at_ns is None else at_ns
        sent = 0
        while True:
            due = line.next_due_ns()
            if due == 0 or due > now:
                return sent
            length = line.take_due_into(self._scratch, 0, now)
            if length == 0:
                return sent
            self._record(memoryview(self._scratch)[:length], now)
            sent += 1

    # -- sync latency ------------------------------------------------------

    def set_sync_delay_ns(self, delay_ns: int) -> None:
        if delay_ns <= 0:
            line, self._line = self._line, None
            if line is not None:
                # Hand over the newest held state, exactly as the real sinks do:
                # switching off must not leave the console on a state from D ago.
                length = line.take_newest_into(self._scratch, 0)
                if length:
                    self._record(memoryview(self._scratch)[:length], now_ns())
            return
        line = self._line
        if line is None:
            self._line = DelayLine(delay_ns=delay_ns)
        else:
            line.set_delay_ns(delay_ns)

    def discard_delayed(self) -> None:
        line = self._line
        if line is not None:
            line.clear()

    def sync_stats(self) -> dict[str, float | int] | None:
        line = self._line
        return line.stats() if line is not None else None

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

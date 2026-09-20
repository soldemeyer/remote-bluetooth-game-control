"""The L2CAP write path: coalescing, keepalive, and link teardown.

The two ends of this path run at completely different rates. A client polls at
up to 500 Hz and sends the instant anything changes; the link drains at whatever
rate the *console* schedules, typically a fraction of that. Writing every
arriving packet straight to the socket builds a queue of stale reports that each
new report waits behind, and the queue is invisible from every counter: writes
succeed, nothing is dropped, and latency simply grows with how hard the player
is moving the stick.

These tests pin the behaviour that replaced it. They drive the sink's internals
directly rather than through a socket, so the latest-wins rule is checked
exactly rather than inferred from timing.
"""

from __future__ import annotations

import errno
import threading
import time

import pytest

hid = pytest.importorskip(
    "server.bt.hid",
    reason="AF_BLUETOOTH sockets are Linux-only",
)

from server.bt.link import LinkPolicy  # noqa: E402
from server.bt.profiles import create_profile  # noqa: E402


class FakeInterrupt:
    """A socket whose transmit queue we control.

    ``send`` either accepts the bytes, raises ``BlockingIOError`` to stand in
    for a full radio queue, or raises whatever error the test asks for.
    """

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.blocking = False
        self.error: OSError | None = None
        self.closed = False

    def send(self, data) -> int:
        if self.error is not None:
            raise self.error
        if self.blocking:
            raise BlockingIOError(errno.EAGAIN, "would block")
        self.writes.append(bytes(data))
        return len(data)

    def setblocking(self, _flag) -> None:
        pass

    def setsockopt(self, *_args) -> None:
        pass

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def sink():
    """A sink with a live fake link, without starting the writer thread.

    Attaching for real would spawn the writer, whose timing would make the
    latest-wins assertions racy. The socket is installed directly so each
    transmit happens exactly when the test says it does.
    """
    s = hid.L2CAPSink(create_profile("generic"), "AA:BB:CC:DD:EE:FF")
    s._interrupt = FakeInterrupt()
    s._peer = "11:22:33:44:55:66"
    return s


def _payload(sock: FakeInterrupt, index: int = -1) -> bytes:
    """The report body of one write, with the HID transaction header stripped."""
    data = sock.writes[index]
    assert data[0] == hid.HID_DATA_INPUT
    return data[1:]


class TestTheOrdinaryWrite:
    def test_the_report_goes_out_with_the_transaction_header(self):
        s = hid.L2CAPSink(create_profile("generic"), "AA:BB:CC:DD:EE:FF")
        s._interrupt = FakeInterrupt()
        assert s.send_input_report(b"\x01\x02\x03") is True
        assert s._interrupt.writes == [bytes([hid.HID_DATA_INPUT, 0x01, 0x02, 0x03])]

    def test_nothing_is_coalesced_when_the_link_keeps_up(self, sink):
        for i in range(5):
            sink.send_input_report(bytes([i]))
        assert len(sink._interrupt.writes) == 5
        assert sink.writes_coalesced == 0

    def test_a_report_larger_than_the_buffer_grows_it(self, sink):
        big = bytes(range(200))
        assert sink.send_input_report(big) is True
        assert _payload(sink._interrupt) == big

    def test_writing_with_no_link_fails_rather_than_raising(self):
        s = hid.L2CAPSink(create_profile("generic"), "AA:BB:CC:DD:EE:FF")
        assert s.send_input_report(b"\x01") is False


class TestCoalescing:
    """A saturated link must cost the *stale* report, never the newest one."""

    def test_a_blocked_write_is_still_accepted(self, sink):
        sink._interrupt.blocking = True
        # True, because the state has been accepted: it is the newest thing we
        # know and the writer will transmit it. Reporting this as a failure is
        # what made a link that was working perfectly look broken.
        assert sink.send_input_report(b"\x01") is True
        assert sink.writes_coalesced == 1
        assert sink._interrupt.writes == []

    def test_the_newest_state_wins_and_the_stale_ones_are_dropped(self, sink):
        sink._interrupt.blocking = True
        for i in range(1, 6):
            sink.send_input_report(bytes([i]))

        assert sink._interrupt.writes == []
        assert sink.writes_coalesced == 5

        # The link drains. Exactly one report goes out, and it is the last
        # state offered -- not the first, and not all five.
        sink._interrupt.blocking = False
        sink._pump(keepalive=False)

        assert len(sink._interrupt.writes) == 1
        assert _payload(sink._interrupt) == b"\x05"

    def test_a_blocked_write_leaves_the_link_dirty(self, sink):
        sink._interrupt.blocking = True
        sink.send_input_report(b"\x07")
        assert sink._dirty is True

        sink._interrupt.blocking = False
        sink._pump(keepalive=False)
        assert sink._dirty is False

    def test_the_writer_is_woken_when_a_write_blocks(self, sink):
        assert not sink._wake.is_set()
        sink._interrupt.blocking = True
        sink.send_input_report(b"\x01")
        # Otherwise the coalesced state waits for the next input packet to
        # retry, which on a stick that has just stopped moving may never come.
        assert sink._wake.is_set()


class TestKeepalive:
    """Re-sending an unchanged state is what makes a flushed report survivable.

    The automatic flush timeout set in server/bt/link.py discards a report that
    cannot get through quickly. On its own that is worse than the jitter it
    fixes: with send-on-change alone, the console holds the stale state until
    the player next changes something, which is a stuck button. The keepalive
    bounds that to one interval.
    """

    def test_it_re_sends_the_current_state_once_the_interval_elapses(self, sink):
        sink.send_input_report(b"\x42")
        assert len(sink._interrupt.writes) == 1

        # Pretend the interval has passed.
        sink._last_tx_ns -= int(sink._policy.keepalive_interval_s * 2e9)
        sink._pump(keepalive=True)

        assert len(sink._interrupt.writes) == 2
        assert _payload(sink._interrupt) == b"\x42"
        assert sink.keepalives_sent == 1

    def test_it_does_not_fire_early(self, sink):
        sink.send_input_report(b"\x42")
        # A spurious wake must not turn the keepalive into a spin: the interval
        # has not elapsed, so there is nothing to say.
        sink._pump(keepalive=True)
        assert len(sink._interrupt.writes) == 1
        assert sink.keepalives_sent == 0

    def test_it_is_silent_before_any_state_exists(self, sink):
        # Nothing has been offered yet, so there is no state to repeat. Sending
        # a zeroed buffer here would tell the console every button is released
        # before the player has touched anything.
        sink._last_tx_ns = 0
        sink._pump(keepalive=True)
        assert sink._interrupt.writes == []

    def test_it_can_be_switched_off(self):
        s = hid.L2CAPSink(
            create_profile("generic"),
            "AA:BB:CC:DD:EE:FF",
            policy=LinkPolicy(keepalive_hz=0),
        )
        s._interrupt = FakeInterrupt()
        s.send_input_report(b"\x42")
        s._last_tx_ns = 0
        s._pump(keepalive=True)
        assert len(s._interrupt.writes) == 1


class TestLinkTeardown:
    """A dead link must be noticed, and noticing it must not wedge the datapath."""

    @pytest.mark.parametrize("code", [errno.ECONNRESET, errno.EPIPE, errno.ENOTCONN, errno.EBADF])
    def test_a_dropped_link_detaches(self, sink, code):
        sink._interrupt.error = OSError(code, "gone")
        assert sink.send_input_report(b"\x01") is False
        assert sink.is_connected is False

    def test_teardown_from_the_datapath_does_not_deadlock(self, sink):
        """The reason the teardown happens outside the I/O lock.

        ``send_input_report`` holds ``_io_lock`` around the write, and
        ``detach()`` needs that same lock to close the socket.
        ``threading.Lock`` is not reentrant, so tearing the link down in place
        would deadlock the datapath thread on the first console disconnect --
        and the datapath thread going away takes every other controller with
        it, not just this one.
        """
        sink._interrupt.error = OSError(errno.ECONNRESET, "gone")

        done = threading.Event()

        def write() -> None:
            sink.send_input_report(b"\x01")
            done.set()

        thread = threading.Thread(target=write, daemon=True)
        thread.start()
        assert done.wait(timeout=5.0), "send_input_report deadlocked on teardown"

    def test_an_unexpected_error_is_counted_but_keeps_the_link(self, sink):
        # Not every write failure means the console has gone. Dropping a live
        # link over a transient error would cost a reconnect for nothing.
        sink._interrupt.error = OSError(errno.EINVAL, "nonsense")
        assert sink.send_input_report(b"\x01") is False
        assert sink.write_failures == 1
        assert sink.is_connected is True

    def test_detach_is_idempotent(self, sink):
        sink.detach()
        sink.detach()
        assert sink.is_connected is False


class TestStatsDistinguishSaturationFromFailure:
    """Coalescing is healthy; a failed write is not. They must not share a counter."""

    def test_a_saturated_link_reports_no_failures(self, sink):
        sink._interrupt.blocking = True
        for i in range(3):
            sink.send_input_report(bytes([i]))
        stats = sink.stats()
        assert stats["writes_coalesced"] == 3
        assert stats["write_failures"] == 0

    def test_stats_survive_having_no_link_report(self, sink):
        # Nothing tuned this link -- no HCI channel, or a dev machine. The web
        # GUI still has to render it.
        assert sink.stats()["link"] is None


class TestSyncDelay:
    """Holding input back to level the playing field.

    **The existing tests in this file are the real regression gate**: they run
    with no delay line at all and must keep passing unmodified, because "sync
    latency off" has to be the write path that was here before.
    """

    def held(self, sink, delay_ms=50.0):
        sink.set_sync_delay_ns(int(delay_ms * 1_000_000))
        return sink

    def test_nothing_is_written_inline(self, sink):
        """The whole point: the datapath's write no longer reaches the radio."""
        self.held(sink)

        assert sink.send_input_report(b"\x01\x02") is True
        assert sink._interrupt.writes == []

    def test_the_newest_due_state_goes_out(self, sink):
        self.held(sink, delay_ms=2.0)
        for index in range(4):
            sink.send_input_report(bytes([1, index]))
        time.sleep(0.01)

        sink._pump(keepalive=False)

        assert _payload(sink._interrupt) == bytes([1, 3])

    def test_the_keepalive_resends_the_last_released_state(self, sink):
        """**The invariant everything else rests on.** Only the release path
        writes `_tx`, so `_tx` holds the state the console actually has -- which
        is what keeps re-sending it self-healing, and what stops it leaking a
        state the player has not waited out yet."""
        self.held(sink, delay_ms=2.0)
        sink.send_input_report(bytes([1, 0xAA]))
        time.sleep(0.01)
        sink._pump(keepalive=False)
        released = len(sink._interrupt.writes)

        # A newer state arrives and is still being held.
        self.held(sink, delay_ms=5000.0)
        sink.send_input_report(bytes([1, 0xBB]))
        sink._last_tx_ns = 0
        sink._pump(keepalive=True)

        assert len(sink._interrupt.writes) == released + 1
        assert _payload(sink._interrupt) == bytes([1, 0xAA])

    def test_one_wake_per_burst_not_one_per_report(self, sink):
        """Arrival times are monotone, so a non-empty line means the writer is
        already asleep on an earlier deadline."""
        self.held(sink)
        sink._wake.clear()

        sink.send_input_report(bytes([1, 0]))
        assert sink._wake.is_set()
        sink._wake.clear()
        for index in range(1, 20):
            sink.send_input_report(bytes([1, index]))

        assert not sink._wake.is_set()

    def test_the_radio_coalescing_counter_is_untouched(self, sink):
        """`writes_coalesced` diagnoses the radio pushing back. A state
        superseded while being held on purpose is a different thing, and merging
        the two would destroy that diagnosis."""
        self.held(sink, delay_ms=2.0)
        for index in range(5):
            sink.send_input_report(bytes([1, index]))
        time.sleep(0.01)
        sink._pump(keepalive=False)

        assert sink.writes_coalesced == 0
        assert sink.sync_stats()["coalesced"] == 4

    def test_detaching_discards_what_was_held(self, sink):
        """A new console must not inherit whatever the last one had not been
        given yet."""
        self.held(sink)
        sink.send_input_report(bytes([1, 0xCC]))

        sink.detach()

        assert sink.sync_stats()["depth"] == 0

    def test_switching_off_hands_over_the_newest_state(self, sink):
        """Rather than leaving the console on a state from D ago until the player
        next moves something."""
        self.held(sink)
        sink.send_input_report(bytes([1, 0xDD]))
        assert sink._interrupt.writes == []

        sink.set_sync_delay_ns(0)

        assert _payload(sink._interrupt) == bytes([1, 0xDD])

    def test_off_is_no_line_at_all(self, sink):
        """Not a line set to zero: off has to be the original code path."""
        self.held(sink)
        sink.set_sync_delay_ns(0)

        assert sink.sync_stats() is None
        assert sink.send_input_report(b"\x01\x02") is True
        assert _payload(sink._interrupt) == b"\x01\x02"

    def test_the_writer_wakes_for_the_line_before_the_keepalive(self, sink):
        """The loop already waits on a deadline, so this costs no second timer
        and no second thread -- but it has to take the *earlier* of the two."""
        import inspect

        source = inspect.getsource(hid.L2CAPSink._writer_loop)
        assert "next_due_ns()" in source
        assert "line_due < due" in source

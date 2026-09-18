"""Sync latency: the ring, the arithmetic, and the property everything rests on.

No hardware and no sockets, except the one class that drives a real server over
loopback because the interesting failure is a *loop* -- added delay inflating the
measurement that decides the delay -- and that cannot be seen in either half
alone.

Measured on the reference desktop while this was written, through the real path
with a second client impaired by 25 ms each way (`tools/impair.py`):

    sync off          age at the sink p50  0.14 ms   p99  0.30
    sync on  (+25.6)                      27.56 ms   p99 27.95
    sync on, cap 10                       12.33 ms   p99 12.64
    off again                              0.13 ms   p99  0.28

p99 within half a millisecond of p50, and `depth` of 1 -- the age does not grow
with time, which is the hidden queue this project has had to find twice.
"""

from __future__ import annotations

import struct
import threading
import time

import pytest

from client.net.transport import ClientTransport
from common.protocol import ControlOp
from common.state import ControllerState
from common.timing import now_ns
from server.bt.profiles import create_profile
from server.bt.sink import DelayLine, MockSink, NullSink
from server.datapath import Datapath
from server.router import OutputChannel, Router
from server.sessions import SessionManager
from server.sync_latency import (
    DEFAULT_CAP_MS,
    MAX_CAP_MS,
    Participant,
    SyncGovernor,
    clamp_cap_ms,
)

MS = 1_000_000


def line(delay_ms: float = 10.0, **kwargs) -> DelayLine:
    return DelayLine(delay_ns=int(delay_ms * MS), **kwargs)


def take(dl: DelayLine, at_ms: float, size: int = 16) -> bytes:
    out = bytearray(size)
    length = dl.take_due_into(out, 0, int(at_ms * MS))
    return bytes(out[:length])


class TestTheRingHoldsAHistory:
    """A single held state cannot add latency -- at release it is the newest one,
    so the console gets fresh input at 1/D Hz and the delay never appears. The
    thing transmitted at T has to be the state that was current at T - D, which
    means remembering the ones in between."""

    def test_nothing_comes_out_before_the_delay(self):
        dl = line(10.0)
        dl.offer(b"first", 0)

        assert take(dl, 9.9) == b""
        assert dl.next_due_ns() == 10 * MS

    def test_and_it_does_afterwards(self):
        dl = line(10.0)
        dl.offer(b"first", 0)

        assert take(dl, 10.0) == b"first"
        assert dl.released == 1

    def test_the_newest_due_state_wins(self):
        dl = line(10.0)
        for index, payload in enumerate((b"a", b"b", b"c")):
            dl.offer(payload, index * MS)

        assert take(dl, 12.0) == b"c"

    def test_the_ones_it_passes_are_coalesced_not_dropped(self):
        """A superseded state is the design rather than a failure, and counting
        it as a drop is what made a saturated but healthy link look broken on the
        Classic side."""
        dl = line(10.0)
        for index in range(5):
            dl.offer(bytes([index]), index * MS)

        take(dl, 15.0)

        assert dl.coalesced == 4
        assert dl.dropped == 0

    def test_an_undue_state_is_left_behind(self):
        dl = line(10.0)
        dl.offer(b"early", 0)
        dl.offer(b"late", 5 * MS)

        assert take(dl, 10.0) == b"early"
        assert dl.depth == 1
        assert take(dl, 15.0) == b"late"

    def test_the_age_of_a_released_state_is_the_delay(self):
        """The property the whole feature is: not "something waited" but "what
        comes out is D old"."""
        dl = line(10.0, slots=64)
        for index in range(40):
            dl.offer(struct.pack("<i", index), index * MS)

        out = take(dl, 25.0, size=4)

        assert struct.unpack("<i", out)[0] == 15      # 25 ms - 10 ms of delay

    def test_nothing_is_due_when_empty(self):
        assert line().next_due_ns() == 0


class TestOverflowIsCountedNotSilent:
    def test_the_oldest_goes_and_is_counted_as_a_drop(self):
        """Saturation means the ring is mis-sized or the consumer has stalled.
        Both are worth seeing, and neither is a supersede."""
        dl = line(10.0, slots=4)
        for index in range(6):
            dl.offer(bytes([index]), index * MS)

        assert dl.dropped == 2
        assert dl.depth == 4
        assert take(dl, 100.0) == bytes([5])

    def test_the_ring_covers_the_cap_at_a_realistic_offer_rate(self):
        """1000 Hz for the length of the delay, with headroom. A client polls at
        up to 500 Hz and sends on change."""
        dl = line(DEFAULT_CAP_MS)
        for index in range(int(DEFAULT_CAP_MS)):
            dl.offer(b"x", index * MS)

        assert dl.dropped == 0

    def test_an_oversized_report_is_delivered_whole(self):
        """Truncating would shift every field of a HID report with nothing
        anywhere to say so."""
        dl = line(10.0, slot_bytes=8)
        big = bytes(range(24))
        dl.offer(big, 0)

        assert take(dl, 10.0, size=64) == big


class TestWakingTheConsumer:
    def test_only_the_first_offer_asks_for_a_wake(self):
        """Arrival times are monotone, so a non-empty line means the consumer is
        already asleep on an earlier deadline. Without this a 500 Hz burst would
        cost five hundred wakes."""
        dl = line(10.0)

        assert dl.offer(b"a", 0) is True
        assert [dl.offer(b"x", i * MS) for i in range(1, 5)] == [False] * 4

    def test_and_again_once_it_has_emptied(self):
        dl = line(10.0)
        dl.offer(b"a", 0)
        take(dl, 10.0)

        assert dl.offer(b"b", 11 * MS) is True


class TestChangingTheDelay:
    def test_lowering_it_releases_what_is_held(self):
        """Rather than stranding it for the duration it was offered under --
        which is why arrival times are stored and due times are not."""
        dl = line(50.0)
        dl.offer(b"held", 0)
        assert take(dl, 10.0) == b""

        dl.set_delay_ns(5 * MS)

        assert take(dl, 10.0) == b"held"

    def test_raising_it_holds_longer(self):
        dl = line(5.0)
        dl.offer(b"held", 0)
        dl.set_delay_ns(40 * MS)

        assert take(dl, 10.0) == b""
        assert take(dl, 40.0) == b"held"

    def test_a_change_never_reorders(self):
        """A constant offset on a monotone series stays monotone."""
        dl = line(5.0, slots=64)
        for index in range(10):
            dl.offer(bytes([index]), index * MS)
        dl.set_delay_ns(1 * MS)

        seen = []
        for at in range(0, 20):
            out = take(dl, float(at))
            if out:
                seen.append(out[0])

        assert seen == sorted(seen)

    def test_it_reports_whether_it_moved(self):
        dl = line(10.0)

        assert dl.set_delay_ns(20 * MS) is True
        assert dl.set_delay_ns(20 * MS) is False

    def test_clearing_counts_nothing(self):
        """Nobody was ever going to see those states, so they are not drops."""
        dl = line(10.0)
        for index in range(3):
            dl.offer(b"x", index * MS)

        dl.clear()

        assert dl.depth == 0
        assert dl.dropped == 0
        assert dl.coalesced == 0

    def test_taking_the_newest_empties_it(self):
        """How switching off hands the console something current, instead of
        leaving it on a state from D ago until the player next moves."""
        dl = line(50.0)
        for index, payload in enumerate((b"a", b"b", b"c")):
            dl.offer(payload, index * MS)

        out = bytearray(16)
        length = dl.take_newest_into(out, 0)

        assert bytes(out[:length]) == b"c"
        assert dl.depth == 0
        assert dl.coalesced == 2


class TestTheGovernorArithmetic:
    def a_and_b(self, a_ms, b_ms, *, samples=50, **kwargs):
        governor = SyncGovernor(**kwargs)
        verdicts = governor.compute(
            [Participant("a", a_ms, samples), Participant("b", b_ms, samples)],
            enabled=True,
        )
        return governor, {k: (round(v.delay_ms, 2), v.state) for k, v in verdicts.items()}

    def test_half_the_difference_goes_to_the_quicker_one(self):
        """Half, because what a player feels is one way and a round trip measures
        two -- the convention the client's combined figure already uses."""
        _, verdicts = self.a_and_b(10.0, 50.0)

        assert verdicts == {"a": (20.0, "levelled"), "b": (0.0, "levelled")}

    def test_the_slowest_is_never_given_any(self):
        _, verdicts = self.a_and_b(50.0, 10.0)

        assert verdicts["a"] == (0.0, "levelled")

    def test_the_cap_binds_and_says_so(self):
        governor, verdicts = self.a_and_b(10.0, 300.0)

        assert verdicts["a"] == (DEFAULT_CAP_MS, "capped")
        assert governor.report()["capped"] is True

    def test_under_the_cap_it_does_not_claim_to_be_capped(self):
        governor, _ = self.a_and_b(10.0, 50.0)

        assert governor.report()["capped"] is False

    def test_one_player_is_not_a_playing_field(self):
        governor = SyncGovernor()

        verdicts = governor.compute([Participant("a", 10.0, 50)], enabled=True)

        assert verdicts["a"].delay_ns == 0
        assert verdicts["a"].state == "off"

    def test_disabled_zeroes_everything(self):
        governor = SyncGovernor()
        governor.compute(
            [Participant("a", 10.0, 50), Participant("b", 50.0, 50)], enabled=True
        )

        verdicts = governor.compute(
            [Participant("a", 10.0, 50), Participant("b", 50.0, 50)], enabled=False
        )

        assert all(v.delay_ns == 0 and v.state == "off" for v in verdicts.values())

    def test_and_forgets_what_was_in_force(self):
        """So switching back on starts clean rather than from a deadband
        comparison against a stale value."""
        governor = SyncGovernor()
        governor.compute(
            [Participant("a", 10.0, 50), Participant("b", 50.0, 50)], enabled=True
        )
        governor.compute([], enabled=False)

        verdicts = governor.compute(
            [Participant("a", 10.0, 50), Participant("b", 20.0, 50)], enabled=True
        )

        assert round(verdicts["a"].delay_ms, 1) == 5.0

    def test_the_pacer_and_the_spread_are_reported(self):
        governor, _ = self.a_and_b(10.0, 50.0)
        report = governor.report()

        assert report["pacer"] == "b"
        assert report["pacer_rtt_ms"] == 50.0
        assert report["spread_ms"] == 40.0

    def test_forget_drops_the_remembered_value(self):
        governor, _ = self.a_and_b(10.0, 50.0)

        governor.forget("a")

        assert "a" not in governor._applied_ms


class TestAClientStillBeingMeasured:
    """**The one that matters most.** A client three noisy samples old must not
    set the target for everybody, and its first samples are taken while its
    handshake and first control messages are still in flight."""

    def test_it_does_not_define_the_slowest_connection(self):
        governor = SyncGovernor()

        verdicts = governor.compute([
            Participant("a", 10.0, 50),
            Participant("b", 20.0, 50),
            Participant("new", 400.0, 3),
        ], enabled=True)

        # Levelled to b at 20 ms, not to the unmeasured 400 ms client.
        assert round(verdicts["a"].delay_ms, 1) == 5.0

    def test_and_gets_nothing_itself_meanwhile(self):
        governor = SyncGovernor()

        verdicts = governor.compute([
            Participant("a", 10.0, 50),
            Participant("b", 20.0, 50),
            Participant("new", 400.0, 3),
        ], enabled=True)

        assert verdicts["new"].delay_ns == 0

    def test_and_says_which_it_is(self):
        """Otherwise a player who is genuinely not being levelled looks identical
        to one who needs no levelling."""
        governor = SyncGovernor()

        verdicts = governor.compute([
            Participant("a", 10.0, 50), Participant("new", 400.0, 3),
        ], enabled=True)

        assert verdicts["new"].state == "measuring"


class TestTheDeadband:
    def test_a_small_move_is_not_applied(self):
        governor = SyncGovernor()
        governor.compute(
            [Participant("a", 10.0, 50), Participant("b", 50.0, 50)], enabled=True
        )

        verdicts = governor.compute(
            [Participant("a", 10.0, 50), Participant("b", 52.0, 50)], enabled=True
        )

        assert round(verdicts["a"].delay_ms, 1) == 20.0

    def test_a_real_move_is(self):
        governor = SyncGovernor()
        governor.compute(
            [Participant("a", 10.0, 50), Participant("b", 50.0, 50)], enabled=True
        )

        verdicts = governor.compute(
            [Participant("a", 10.0, 50), Participant("b", 60.0, 50)], enabled=True
        )

        assert round(verdicts["a"].delay_ms, 1) == 25.0

    def test_it_compares_against_what_is_in_force(self):
        """Not against the last value computed, or a slow drift never crosses the
        threshold and the delay sits where it was first set."""
        governor = SyncGovernor()
        governor.compute(
            [Participant("a", 10.0, 50), Participant("b", 50.0, 50)], enabled=True
        )

        for rtt in (51.0, 52.0, 53.0, 54.0, 55.0, 56.0):
            verdicts = governor.compute(
                [Participant("a", 10.0, 50), Participant("b", rtt, 50)], enabled=True
            )

        # 20.0 held until the wanted value reached 22.0, then held again at 22.
        # Stepping rather than tracking is the point: a delay that twitches is
        # worse than one that is a millisecond out.
        assert round(verdicts["a"].delay_ms, 1) == 22.0

    def test_zero_is_always_applied_exactly(self):
        """Or a residual millisecond survives the slowest player leaving, and
        nothing would ever clear it."""
        governor = SyncGovernor()
        governor.compute(
            [Participant("a", 10.0, 50), Participant("b", 11.5, 50)], enabled=True
        )

        verdicts = governor.compute(
            [Participant("a", 10.0, 50), Participant("b", 10.0, 50)], enabled=True
        )

        assert verdicts["a"].delay_ns == 0


class TestTheCapIsOperatorSettable:
    def test_a_browser_number_survives(self):
        assert clamp_cap_ms("120") == 120.0

    def test_nonsense_falls_back(self):
        assert clamp_cap_ms("not a number") == DEFAULT_CAP_MS
        assert clamp_cap_ms(None) == DEFAULT_CAP_MS
        assert clamp_cap_ms(float("nan")) == DEFAULT_CAP_MS

    def test_it_is_bounded(self):
        assert clamp_cap_ms(-5) == 0.0
        assert clamp_cap_ms(99999) == MAX_CAP_MS

    def test_the_governor_honours_it(self):
        governor = SyncGovernor(cap_ms=8.0)

        verdicts = governor.compute(
            [Participant("a", 10.0, 50), Participant("b", 200.0, 50)], enabled=True
        )

        assert round(verdicts["a"].delay_ms, 1) == 8.0
        assert verdicts["a"].state == "capped"


class TestEverySinkTakesTheHooks:
    """Concrete rather than abstract on HIDSink, so a sink written later keeps
    working and the datapath needs no hasattr dance. The failure mode for one
    that does not implement them is "the feature is off there", which is the
    right way round."""

    @pytest.mark.parametrize("sink", [NullSink(), MockSink()])
    def test_the_three_methods_exist(self, sink):
        sink.set_sync_delay_ns(0)
        sink.discard_delayed()
        sink.sync_stats()

    def test_off_means_no_line_at_all(self):
        """Not a line set to zero: off has to be the path that was there before
        this existed."""
        sink = MockSink()
        sink.set_sync_delay_ns(20 * MS)
        assert sink.sync_stats() is not None

        sink.set_sync_delay_ns(0)

        assert sink.sync_stats() is None

    def test_switching_off_hands_over_the_newest_state(self):
        """Otherwise the console holds a state from D ago until the player next
        moves -- and on BLE, which has no keepalive, that can be a long time."""
        sink = MockSink()
        sink.set_sync_delay_ns(500 * MS)
        sink.send_input_report(b"\x01held")
        assert sink.count == 0

        sink.set_sync_delay_ns(0)

        assert sink.count == 1
        assert sink.last_report().data == b"\x01held"

    def test_a_held_state_is_not_delivered_early(self):
        sink = MockSink()
        sink.set_sync_delay_ns(500 * MS)

        sink.send_input_report(b"\x01a")
        sink.send_input_report(b"\x01b")

        assert sink.count == 0

    def test_discarding_does_not_deliver(self):
        sink = MockSink()
        sink.set_sync_delay_ns(500 * MS)
        sink.send_input_report(b"\x01a")

        sink.discard_delayed()
        sink.pump(now_ns() + 10**9)

        assert sink.count == 0


def _make_server(password, adapters=2):
    router = Router()
    sinks = {}
    for index in range(adapters):
        bd = f"00:00:00:00:00:{index:02X}"
        sink = MockSink(name=f"mock{index}", history=4096)
        sinks[bd] = sink
        router.add_channel(OutputChannel(
            bd_addr=bd, hci_name=f"test{index}",
            profile=create_profile("generic"), sink=sink,
        ))
    sessions = SessionManager(password, auto_approve=True)
    datapath = Datapath(
        sessions, router, bind_host="127.0.0.1", bind_port=0, realtime=False
    )
    return datapath, router, sessions, sinks


@pytest.fixture
def server():
    datapath, router, sessions, sinks = _make_server("sync-latency-test")
    datapath.start()
    time.sleep(0.05)
    yield datapath, router, sessions, sinks
    datapath.stop()


def _connect(datapath, name):
    transport = ClientTransport("sync-latency-test", client_name=name)
    transport.connect("127.0.0.1", datapath.port, timeout_ns=20_000_000_000)
    transport.queue_control(ControlOp.SET_CONTROLLERS, {
        "controllers": [{"slot": 0, "username": name, "device_name": "synthetic"}]
    })
    return transport


def _wait_until(predicate, timeout=6.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestTheServerMeasuresItsOwnRoundTrip:
    """It never did. `ControllerSlot.rtt` was declared and written by nothing, so
    the web GUI's Latency column showed "-" for the life of the project.

    The probe needs no protocol change and no client change: HEARTBEAT and its
    ack have always been documented "either direction", and the client already
    answers an incoming one unconditionally.
    """

    def test_a_round_trip_appears(self, server):
        datapath, _, sessions, _ = server
        transport = _connect(datapath, "measured")
        try:
            def sampled():
                transport.service()
                return any(s.sync_rtt.count for s in sessions.all_sessions())

            assert _wait_until(sampled), "no round trip was ever measured"
        finally:
            transport.close()

    def test_and_reaches_the_snapshot(self, server):
        datapath, _, sessions, _ = server
        transport = _connect(datapath, "measured")
        try:
            def sampled():
                transport.service()
                return any(s.sync_rtt.count for s in sessions.all_sessions())

            assert _wait_until(sampled)
            snap = sessions.snapshot()[0]

            assert snap["rtt_ms"]["count"] > 0
            for slot in snap["slots"]:
                assert slot["rtt_ms"]["count"] > 0
        finally:
            transport.close()

    def test_an_unknown_sequence_is_ignored(self, server):
        """A doctored or replayed ack must not become a sample."""
        datapath, _, sessions, _ = server
        transport = _connect(datapath, "measured")
        try:
            _wait_until(lambda: bool(sessions.all_sessions()))
            session = next(iter(sessions.all_sessions()))
            session.sync_rtt.clear()
            before = session.sync_rtt.count

            from common import protocol as proto

            buf = bytearray(32)
            size = proto.encode_heartbeat_ack_into(buf, 0, 0xDEAD, 1)
            datapath._handle_heartbeat_ack(bytes(buf[:size]), session, now_ns())

            assert session.sync_rtt.count == before
        finally:
            transport.close()

    def test_a_slot_is_consumed_once(self, server):
        datapath, _, sessions, _ = server
        transport = _connect(datapath, "measured")
        try:
            _wait_until(lambda: bool(sessions.all_sessions()))
            session = next(iter(sessions.all_sessions()))
            session.sync_rtt.clear()

            from common import protocol as proto

            seq = session.next_probe_seq()
            session.note_probe_sent(seq, now_ns() - 5 * MS)
            buf = bytearray(32)
            size = proto.encode_heartbeat_ack_into(buf, 0, seq, 1)
            packet = bytes(buf[:size])

            datapath._handle_heartbeat_ack(packet, session, now_ns())
            datapath._handle_heartbeat_ack(packet, session, now_ns())

            assert session.sync_rtt.count == 1
        finally:
            transport.close()

    def test_the_echoed_timestamp_is_not_believed(self, server):
        """The client echoes *our* bytes, so by the time they come back they are
        client-supplied content. A doctored echo would fabricate a two-second
        round trip and impose the cap on every other player."""
        datapath, _, sessions, _ = server
        transport = _connect(datapath, "measured")
        try:
            _wait_until(lambda: bool(sessions.all_sessions()))
            session = next(iter(sessions.all_sessions()))
            session.sync_rtt.clear()

            from common import protocol as proto

            seq = session.next_probe_seq()
            session.note_probe_sent(seq, now_ns() - 3 * MS)
            buf = bytearray(32)
            # A timestamp claiming the probe left an hour ago.
            size = proto.encode_heartbeat_ack_into(buf, 0, seq, 1)
            datapath._handle_heartbeat_ack(bytes(buf[:size]), session, now_ns())

            assert session.sync_rtt.count == 1
            assert session.sync_rtt.last_ms < 100.0
        finally:
            transport.close()

    def test_an_absurd_sample_is_rejected(self, server):
        datapath, _, sessions, _ = server
        transport = _connect(datapath, "measured")
        try:
            _wait_until(lambda: bool(sessions.all_sessions()))
            session = next(iter(sessions.all_sessions()))
            session.sync_rtt.clear()

            from common import protocol as proto

            seq = session.next_probe_seq()
            session.note_probe_sent(seq, now_ns() - 30 * 10**9)
            buf = bytearray(32)
            size = proto.encode_heartbeat_ack_into(buf, 0, seq, 1)
            datapath._handle_heartbeat_ack(bytes(buf[:size]), session, now_ns())

            assert session.sync_rtt.count == 0
        finally:
            transport.close()


class TestTheMeasurementCannotChaseItself:
    """**The runaway this feature had to be built around.** If added delay showed
    up in the round trip that decides the delay, more delay would mean a higher
    measurement would mean more delay.

    It cannot, structurally: the probe is its own packet and never goes near a
    sink. This pins that rather than trusting it.
    """

    def test_the_probe_reaches_no_sink(self, server):
        datapath, _, sessions, sinks = server
        transport = _connect(datapath, "probed")
        try:
            _wait_until(lambda: bool(sessions.all_sessions()))
            for sink in sinks.values():
                sink.clear()

            datapath._send_sync_probes(now_ns())

            assert all(sink.count == 0 for sink in sinks.values())
        finally:
            transport.close()

    def test_the_ack_path_reaches_no_sink(self, server):
        datapath, _, sessions, sinks = server
        transport = _connect(datapath, "probed")
        try:
            _wait_until(lambda: bool(sessions.all_sessions()))
            session = next(iter(sessions.all_sessions()))
            for sink in sinks.values():
                sink.clear()

            from common import protocol as proto

            seq = session.next_probe_seq()
            session.note_probe_sent(seq, now_ns() - MS)
            buf = bytearray(32)
            size = proto.encode_heartbeat_ack_into(buf, 0, seq, 1)
            datapath._handle_heartbeat_ack(bytes(buf[:size]), session, now_ns())

            assert all(sink.count == 0 for sink in sinks.values())
        finally:
            transport.close()

    def test_a_delay_at_the_cap_does_not_inflate_the_measurement(self, server):
        """Driven end to end: a large delay in force, and the round trip still
        reads what the network costs."""
        datapath, router, sessions, _ = server
        quick = _connect(datapath, "quick")
        other = _connect(datapath, "other")
        try:
            def both_measured():
                quick.service()
                other.service()
                return all(
                    s.sync_rtt.count >= 25 for s in sessions.all_sessions()
                ) and len(sessions.all_sessions()) == 2

            assert _wait_until(both_measured, timeout=10.0)

            session = next(
                s for s in sessions.all_sessions() if s.client_name == "quick"
            )
            baseline = session.sync_rtt.p50

            # Force a large delay rather than waiting for a slow peer: what is
            # under test is whether the delay feeds back, not the arithmetic.
            for channel in router.channels():
                channel.sink.set_sync_delay_ns(200 * MS)

            state = ControllerState()
            for index in range(400):
                state.left_x = index * 37 % 20000 - 10000
                quick.send_input(0, state, request_ack=(index % 5 == 0))
                quick.service()
                other.service()
                time.sleep(0.004)

            assert session.sync_rtt.p50 < baseline + 20.0, (
                f"round trip climbed from {baseline:.2f} to "
                f"{session.sync_rtt.p50:.2f} ms with 200 ms of delay in force"
            )
        finally:
            quick.close()
            other.close()


class TestTheDelayReachesTheConsole:
    """Not "a sink was configured" but "the bytes came out later", measured from
    when the state was sent."""

    def test_a_report_arrives_the_delay_late(self, server):
        datapath, router, sessions, sinks = server
        transport = _connect(datapath, "solo")
        try:
            assert _wait_until(lambda: any(
                c.is_assigned for c in router.channels()
            ) and bool(sessions.all_sessions()))

            channel = next(c for c in router.channels() if c.is_assigned)
            sink = channel.sink
            # Directly, because the governor needs a second player to level
            # against and the arithmetic is covered above.
            sink.set_sync_delay_ns(40 * MS)
            sink.clear()

            state = ControllerState()
            sent: dict[int, int] = {}
            for index in range(200):
                marker = index + 1
                state.left_x = marker
                sent[marker] = now_ns()
                transport.send_input(0, state, request_ack=False)
                transport.service()
                time.sleep(0.003)
            # One more offer so MockSink, which has no loop, releases what is due.
            time.sleep(0.1)
            state.left_x = 0
            transport.send_input(0, state, request_ack=False)
            time.sleep(0.1)

            ages = []
            for report in sink.reports():
                marker = struct.unpack_from("<h", report.data, 1)[0]
                when = sent.get(marker)
                if when is not None:
                    ages.append((report.timestamp_ns - when) / 1e6)

            assert ages, "nothing identifiable reached the sink"
            ages.sort()
            median = ages[len(ages) // 2]
            assert 40.0 <= median <= 70.0, f"median age {median:.1f} ms, wanted ~40"
        finally:
            transport.close()

    def test_and_the_age_does_not_grow(self):
        """The hidden queue. Offering far faster than the consumer drains must
        cost throughput, never latency -- the failure this project has had to
        find twice, once at L2CAP and once at the BLE notification."""
        dl = line(20.0, slots=512)
        released_ages = []
        # 1000 Hz offered, drained at 100 Hz: ten offers per take.
        for tick in range(500):
            dl.offer(struct.pack("<i", tick), tick * MS)
            if tick % 10 == 0:
                out = take(dl, float(tick), size=8)
                if out:
                    origin = struct.unpack("<i", out)[0]
                    released_ages.append(tick - origin)

        assert released_ages
        assert max(released_ages) <= 21, (
            f"age grew to {max(released_ages)} ms against a 20 ms delay"
        )


class TestTurningItOffLeavesNothingBehind:
    def test_the_sinks_go_back_to_no_line(self, server):
        datapath, router, _, _ = server
        datapath.set_sync_latency(True)
        for channel in router.channels():
            channel.sink.set_sync_delay_ns(30 * MS)

        datapath.set_sync_latency(False)
        datapath._run_sync_governor(now_ns())

        assert all(c.sink.sync_stats() is None for c in router.channels())

    def test_a_departing_client_does_not_leave_a_held_state(self, server):
        """`_release_channels_for` writes a neutral report. Offered into a live
        line it would queue *behind* the departing player's held states, so the
        console would latch their last input for the length of the delay and only
        then go neutral -- the stuck button that release exists to prevent."""
        datapath, router, sessions, _ = server
        transport = _connect(datapath, "leaver")
        try:
            assert _wait_until(lambda: any(c.is_assigned for c in router.channels()))
            channel = next(c for c in router.channels() if c.is_assigned)
            channel.sink.set_sync_delay_ns(500 * MS)

            state = ControllerState(left_x=12345)
            transport.send_input(0, state, request_ack=False)
            time.sleep(0.2)
            channel.sink.clear()

            datapath._release_channels_for(channel.assigned_client)

            reports = channel.sink.reports()
            assert reports, "the neutral report never went out"
            # Neutral, not the held 12345.
            assert struct.unpack_from("<h", reports[-1].data, 1)[0] == 0
            assert channel.sink.sync_stats() is None
        finally:
            transport.close()


class TestTheClientIsTold:
    """With the delay applied downstream of the ack, the client's own readout is
    by construction an under-report -- 12 ms shown against 60 ms felt. Sending
    the number is the honest fix; inflating an existing statistic is not."""

    def test_it_learns_its_added_delay_and_who_set_the_pace(self, server):
        datapath, router, sessions, _ = server
        quick = _connect(datapath, "quick")
        slow = _connect(datapath, "slow")
        try:
            def measured():
                quick.service()
                slow.service()
                return (
                    len(sessions.all_sessions()) == 2
                    and all(s.sync_rtt.count >= 25 for s in sessions.all_sessions())
                )

            assert _wait_until(measured, timeout=10.0)

            # Make "slow" genuinely slower, without a network to impair.
            slow_session = next(
                s for s in sessions.all_sessions() if s.client_name == "slow"
            )
            for _ in range(200):
                slow_session.sync_rtt.add(80.0)

            datapath.set_sync_latency(True)
            datapath._run_sync_governor(now_ns())

            def told():
                quick.service()
                return quick.sync_snapshot()["added_ms"] > 0

            assert _wait_until(told, timeout=5.0), "the client was never told"
            snapshot = quick.sync_snapshot()
            assert snapshot["pacer"] == "slow"
            assert snapshot["pacer_rtt_ms"] > 0
        finally:
            quick.close()
            slow.close()

    def test_an_older_client_is_unaffected(self):
        """The op is additive: both dispatchers ack before they dispatch and
        neither has an else branch."""
        from common import protocol as proto

        packet = proto.encode_control(1, ControlOp.SYNC_LATENCY, {"added_ms": 5.0})
        seq, body = proto.decode_control(packet, 0)

        assert seq == 1
        assert body["op"] == ControlOp.SYNC_LATENCY
        assert body["added_ms"] == 5.0

"""The BLE sink's coalescing, pacing and isolation contract.

This is the fix for the reported failure: a second player degraded the first.
Measured on the reference Pi before the change, two independent 500 Hz sessions
against a real console --

    player 1 alone          p99 RTT   4.38 ms
    + player 2 connected    p99 RTT   4.38 ms     (idle: costs nothing)
    + player 2 playing      p99 RTT  20.47 ms     (+368%)

-- because every input report was marshalled into a D-Bus signal on the single
datapath thread (188 us of pure Python each) and queued into an unbounded
buffer that no counter reported.

What these tests pin, in order of how badly it would hurt to lose it:

1. the datapath thread does no D-Bus work and never touches the event loop;
2. two sinks are independent -- one player's load cannot reach another's;
3. states coalesce latest-wins, and the newest state always arrives;
4. a departing player's state is never inherited by the next one.
"""

from __future__ import annotations

import asyncio

import pytest

from server.bt.ble.peripheral import BLESink
from server.bt.profiles import create_profile


class FakeCharacteristic:
    """Stands in for the GATT report characteristic.

    Records what was notified, in order, so a test can assert both *what*
    arrived and *how much* did.
    """

    def __init__(self) -> None:
        self.notifying = True
        self.sent: list[bytes] = []

    def notify(self, payload) -> bool:
        self.sent.append(bytes(payload))
        return True


def make_sink(max_hz: int = 250, profile: str = "generic") -> BLESink:
    return BLESink(create_profile(profile), "CC:28:AA:6D:BA:C0", max_hz=max_hz)


def live(sink: BLESink, characteristic: FakeCharacteristic) -> None:
    sink.attach(characteristic)
    sink.set_link(True, "A8:ED:71:F3:ED:FD")


async def settle(predicate, timeout: float = 2.0) -> bool:
    """Wait for the emitter to catch up, without sleeping a fixed amount."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.002)
    return predicate()


class TestTheDatapathDoesNoDbusWork:
    """The whole point of the change.

    ``send_input_report`` runs on the datapath thread, which has a
    sub-millisecond budget shared by every player. It used to marshal a D-Bus
    message there and call ``loop.add_writer`` -- from the wrong thread, which
    is additionally not something asyncio permits.
    """

    def test_it_accepts_a_report_with_no_event_loop_at_all(self):
        """No loop, no emitter, no D-Bus -- and it still takes the state.

        If this ever needs a running loop again, the marshalling has moved back
        onto the datapath thread and the regression is complete.
        """
        sink = make_sink()
        characteristic = FakeCharacteristic()
        live(sink, characteristic)

        assert sink.send_input_report(b"\x01\x02\x03") is True
        assert sink.reports_offered == 1
        # Nothing was transmitted, because nothing may be: transmitting is the
        # loop's job and there is no loop here.
        assert characteristic.sent == []

    def test_it_refuses_when_there_is_no_characteristic(self):
        sink = make_sink()
        assert sink.send_input_report(b"\x01\x02") is False


class TestCoalescing:
    async def test_a_burst_collapses_to_the_newest_state(self):
        """Latest-wins, exactly as the Classic sink does it.

        The console cannot see a state that existed for less than one
        connection interval, so transmitting superseded states buys nothing and
        costs a queue.
        """
        sink = make_sink(max_hz=50)          # 20 ms, slow enough to force it
        characteristic = FakeCharacteristic()
        live(sink, characteristic)
        sink.start_emitter(bus=None)
        try:
            for value in range(1, 21):
                sink.send_input_report(bytes([0x01, value]))

            await settle(lambda: characteristic.sent)
            # Whatever did go out, the last thing sent is the newest state.
            assert characteristic.sent[-1] == b"\x14"
            # And far fewer transmits than offers.
            assert len(characteristic.sent) < 20
            assert sink.reports_offered == 20
            assert sink.states_superseded > 0
        finally:
            await sink.stop_emitter()

    async def test_the_final_state_is_never_left_unsent(self):
        """Coalescing may drop intermediate states. It must never drop the last
        one, or a released button stays held on the console forever."""
        sink = make_sink(max_hz=200)
        characteristic = FakeCharacteristic()
        live(sink, characteristic)
        sink.start_emitter(bus=None)
        try:
            for value in range(1, 11):
                sink.send_input_report(bytes([0x01, value]))
            sink.send_input_report(b"\x01\x00")      # the release

            assert await settle(lambda: characteristic.sent[-1:] == [b"\x00"])
        finally:
            await sink.stop_emitter()

    async def test_a_superseded_state_is_not_counted_as_a_failure(self):
        """It is a healthy saturated link, not a fault. Counting it as a drop
        is what made a working Classic link look broken."""
        sink = make_sink(max_hz=50)
        characteristic = FakeCharacteristic()
        live(sink, characteristic)
        sink.start_emitter(bus=None)
        try:
            for value in range(1, 31):
                sink.send_input_report(bytes([0x01, value]))
            await settle(lambda: characteristic.sent)
            assert sink.notify_failures == 0
            assert sink.states_superseded > 0
        finally:
            await sink.stop_emitter()


class TestTheReportIdIsStripped:
    """HOGP carries the report id in the Report Reference descriptor, so it
    must not also be in the payload -- leaving it shifts every field by one
    byte and nothing errors at either end."""

    async def test_the_leading_id_byte_is_removed(self):
        profile = create_profile("generic")
        report_id = profile.descriptor.input_report_id
        assert report_id, "this test is meaningless for a profile with no id"

        sink = make_sink()
        characteristic = FakeCharacteristic()
        live(sink, characteristic)
        sink.start_emitter(bus=None)
        try:
            sink.send_input_report(bytes([report_id, 0xAA, 0xBB]))
            assert await settle(lambda: characteristic.sent)
            assert characteristic.sent[0] == b"\xaa\xbb"
        finally:
            await sink.stop_emitter()

    async def test_a_body_that_merely_starts_with_the_id_value_is_kept(self):
        """Only strip when byte 0 really is the id. A profile that declares no
        report id writes none, and removing a data byte there would corrupt
        every field with nothing to indicate it."""
        sink = BLESink(create_profile("generic"), "AA:BB:CC:DD:EE:FF")
        sink._report_id = None
        characteristic = FakeCharacteristic()
        live(sink, characteristic)
        sink.start_emitter(bus=None)
        try:
            sink.send_input_report(b"\x01\xaa\xbb")
            assert await settle(lambda: characteristic.sent)
            assert characteristic.sent[0] == b"\x01\xaa\xbb"
        finally:
            await sink.stop_emitter()


class TestPlayersAreIsolated:
    """The reported bug, as a test.

    Two adapters, two players. One holding a stick at full rate must not change
    what the other one delivers.
    """

    async def test_one_players_load_does_not_starve_another(self):
        busy = make_sink(max_hz=100)
        quiet = make_sink(max_hz=100)
        busy_chr, quiet_chr = FakeCharacteristic(), FakeCharacteristic()
        live(busy, busy_chr)
        live(quiet, quiet_chr)
        busy.start_emitter(bus=None)
        quiet.start_emitter(bus=None)
        try:
            # The busy player floods; the quiet one sends a single state.
            for value in range(1, 201):
                busy.send_input_report(bytes([0x01, value & 0xFF]))
            quiet.send_input_report(b"\x01\x42")

            # The quiet player's one state still arrives, promptly.
            assert await settle(lambda: quiet_chr.sent == [b"\x42"], timeout=1.0)
        finally:
            await busy.stop_emitter()
            await quiet.stop_emitter()

    async def test_neither_sink_ever_sees_the_others_payload(self):
        """Distinct signatures, the way the real reproduction drives them."""
        one, two = make_sink(max_hz=200), make_sink(max_hz=200)
        one_chr, two_chr = FakeCharacteristic(), FakeCharacteristic()
        live(one, one_chr)
        live(two, two_chr)
        one.start_emitter(bus=None)
        two.start_emitter(bus=None)
        try:
            for _ in range(50):
                one.send_input_report(b"\x01\xa1")
                two.send_input_report(b"\x01\xb2")
                await asyncio.sleep(0.001)

            await settle(lambda: one_chr.sent and two_chr.sent)
            assert set(one_chr.sent) == {b"\xa1"}
            assert set(two_chr.sent) == {b"\xb2"}
        finally:
            await one.stop_emitter()
            await two.stop_emitter()

    async def test_counters_are_per_sink(self):
        one, two = make_sink(), make_sink()
        live(one, FakeCharacteristic())
        live(two, FakeCharacteristic())
        for _ in range(5):
            one.send_input_report(b"\x01\x01")
        assert one.reports_offered == 5
        assert two.reports_offered == 0


class TestStateDoesNotSurviveASession:
    """A disconnected player's last input must not be inherited by whoever is
    assigned that adapter next -- that is a stuck button with no cause."""

    def test_detach_discards_the_pending_state(self):
        sink = make_sink()
        characteristic = FakeCharacteristic()
        live(sink, characteristic)
        sink.send_input_report(b"\x01\xff")
        assert sink._dirty is True

        sink.detach()
        assert sink._dirty is False
        assert sink._pending_len == 0

    def test_losing_the_link_discards_the_pending_state(self):
        sink = make_sink()
        characteristic = FakeCharacteristic()
        live(sink, characteristic)
        sink.send_input_report(b"\x01\xff")

        sink.set_link(False)
        assert sink._dirty is False
        assert sink._pending_len == 0

    async def test_nothing_queued_before_a_link_leaks_into_the_next_one(self):
        sink = make_sink(max_hz=200)
        first = FakeCharacteristic()
        live(sink, first)
        sink.send_input_report(b"\x01\xff")     # queued, never transmitted
        sink.detach()

        second = FakeCharacteristic()
        live(sink, second)
        sink.start_emitter(bus=None)
        try:
            await asyncio.sleep(0.05)
            assert second.sent == [], "the previous session's state was inherited"
        finally:
            await sink.stop_emitter()


class TestEmitterLifecycle:
    async def test_stopping_is_idempotent(self):
        sink = make_sink()
        live(sink, FakeCharacteristic())
        sink.start_emitter(bus=None)
        await sink.stop_emitter()
        await sink.stop_emitter()          # must not raise

    async def test_starting_twice_keeps_one_task(self):
        sink = make_sink()
        live(sink, FakeCharacteristic())
        sink.start_emitter(bus=None)
        task = sink._task
        sink.start_emitter(bus=None)
        try:
            assert sink._task is task
        finally:
            await sink.stop_emitter()

    async def test_stop_leaves_no_running_task(self):
        """A leaked emitter would keep a reference to a dead characteristic and
        go on running for the life of the process."""
        sink = make_sink()
        live(sink, FakeCharacteristic())
        sink.start_emitter(bus=None)
        task = sink._task
        await sink.stop_emitter()
        assert task.done()
        assert sink._task is None


class TestStatsAreReportable:
    def test_the_snapshot_carries_what_diagnoses_a_backlog(self):
        """These are the numbers that were missing. A backlog was invisible:
        reports climbed, nothing failed, and latency grew."""
        sink = make_sink()
        live(sink, FakeCharacteristic())
        sink.send_input_report(b"\x01\x01")

        stats = sink.stats()
        for key in (
            "reports_offered",
            "reports_sent",
            "states_superseded",
            "notify_failures",
            "notify_ms",
            "dbus_backlog",
        ):
            assert key in stats, f"{key} missing from the sink snapshot"


@pytest.mark.parametrize("max_hz", [50, 125, 250])
async def test_pacing_bounds_the_transmit_rate(max_hz):
    """One player must not be able to drive the shared bus as hard as it likes.

    Generous slack: this asserts an order of magnitude, not a scheduler's
    precision, and a loaded CI box is allowed to be late.
    """
    sink = make_sink(max_hz=max_hz)
    characteristic = FakeCharacteristic()
    live(sink, characteristic)
    sink.start_emitter(bus=None)
    try:
        deadline = asyncio.get_running_loop().time() + 0.4
        offered = 0
        while asyncio.get_running_loop().time() < deadline:
            sink.send_input_report(bytes([0x01, offered & 0xFF]))
            offered += 1
            await asyncio.sleep(0.001)

        assert offered > len(characteristic.sent), "no coalescing happened at all"
        assert len(characteristic.sent) <= max_hz * 0.4 * 2 + 10
    finally:
        await sink.stop_emitter()


class TestTheEmitterSurvivesTrouble:
    """A dead emitter is the worst outcome available here.

    It leaves a live, encrypted, subscribed link carrying no input at all while
    ``is_connected`` stays True and every counter looks healthy -- the same
    shape as the woken-controller bug, arrived at from a different direction.
    """

    async def test_an_exception_from_notify_does_not_kill_the_task(self):
        sink = make_sink(max_hz=500)
        characteristic = FakeCharacteristic()

        calls = {"n": 0}

        def exploding(payload):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise RuntimeError("bluetoothd went away")
            return characteristic.notify(payload)

        characteristic_proxy = FakeCharacteristic()
        characteristic_proxy.notify = exploding

        sink.attach(characteristic_proxy)
        sink.set_link(True, "A8:ED:71:F3:ED:FD")
        sink.start_emitter(bus=None)
        try:
            for value in range(1, 30):
                sink.send_input_report(bytes([0x01, value]))
                await asyncio.sleep(0.004)

            assert await settle(lambda: characteristic.sent), (
                "the emitter died on the first exception and never recovered"
            )
            assert sink.notify_failures >= 3
        finally:
            await sink.stop_emitter()

    async def test_an_unexpected_error_in_the_loop_is_survived(self):
        """Not just notify -- anything. The outer guard is what makes a dead
        adapter impossible rather than merely unlikely."""
        sink = make_sink(max_hz=500)
        characteristic = FakeCharacteristic()
        live(sink, characteristic)

        boom = {"n": 0}
        real = sink._backlogged

        def flaky():
            boom["n"] += 1
            if boom["n"] <= 2:
                raise RuntimeError("something nobody predicted")
            return real()

        sink._backlogged = flaky
        sink.start_emitter(bus=None)
        try:
            for value in range(1, 20):
                sink.send_input_report(bytes([0x01, value]))
                await asyncio.sleep(0.005)

            assert await settle(lambda: characteristic.sent), (
                "an unexpected error killed the emitter permanently"
            )
            assert not sink._task.done(), "the task exited"
        finally:
            await sink.stop_emitter()

    async def test_a_wedged_bus_does_not_stall_forever(self):
        """A permanently backed-up writer must give up the drain and say so,
        not spin silently holding the player's input."""
        sink = make_sink(max_hz=500)
        characteristic = FakeCharacteristic()
        live(sink, characteristic)
        sink._backlogged = lambda: True          # never drains
        sink.start_emitter(bus=None)
        try:
            sink.send_input_report(b"\x01\x42")

            # It must abandon the drain rather than looping on it forever.
            deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < deadline:
                if sink._stall_logged:
                    break
                await asyncio.sleep(0.01)

            assert sink._stall_logged, "a wedged bus stalled with nothing logged"
            assert not sink._task.done(), "the emitter gave up entirely"

            # And it recovers the moment the bus drains.
            sink._backlogged = lambda: False
            sink.send_input_report(b"\x01\x43")
            assert await settle(lambda: characteristic.sent)
        finally:
            await sink.stop_emitter()

    async def test_a_state_queued_before_the_emitter_started_still_goes(self):
        """send_input_report accepts a report whether or not an emitter exists.
        If the emitter then starts and waits for a *new* report, a final
        release would sit unsent -- a held button with nothing to explain it."""
        sink = make_sink(max_hz=500)
        characteristic = FakeCharacteristic()
        live(sink, characteristic)

        sink.send_input_report(b"\x01\x77")     # queued with no loop at all
        assert sink._dirty is True

        sink.start_emitter(bus=None)
        try:
            assert await settle(lambda: characteristic.sent == [b"\x77"]), (
                "the state queued before start was never transmitted"
            )
        finally:
            await sink.stop_emitter()


class TestTheNotificationSocket:
    """bluetoothd can hand us a socket to write notifications into, instead of
    taking them as ``PropertiesChanged`` signals.

    Two things about it are worth pinning, and they pull in opposite
    directions:

    * It is **much cheaper** -- a socket write has no D-Bus marshalling.
      Measured on the reference Pi: 22 us per report against 320 us.
    * It gives **no backpressure**, which is the opposite of what it looks
      like. bluetoothd reads the pipe as fast as we write and queues
      downstream: measured 8647 writes with *zero* EAGAIN, and a 30-second
      backlog on air. So the socket path is paced exactly like the property
      path, and the EAGAIN handling below is defensive rather than load
      bearing.
    """

    def _pair(self):
        """A connected pair, portably.

        The real one is AF_UNIX/SOCK_SEQPACKET -- bluetoothd needs message
        boundaries so two reports never run together. Windows has neither, and
        what these tests exercise is the sink's handling rather than the
        socket family, so the default pair is enough here.
        """
        import socket

        ours, theirs = socket.socketpair()
        ours.setblocking(False)
        return ours, theirs

    async def test_reports_go_to_the_socket_not_the_characteristic(self):
        sink = make_sink(max_hz=500)
        characteristic = FakeCharacteristic()
        live(sink, characteristic)
        ours, theirs = self._pair()
        sink.attach_notify_socket(ours, 517)
        sink.start_emitter(bus=None)
        try:
            sink.send_input_report(bytes([0x01, 0xAB, 0xCD]))
            assert await settle(lambda: theirs.recv(64) if _readable(theirs) else None)
        finally:
            await sink.stop_emitter()
            ours.close()
            theirs.close()

    async def test_the_payload_reaches_the_socket_with_the_id_stripped(self):
        import socket as _s

        sink = make_sink(max_hz=500)
        characteristic = FakeCharacteristic()
        live(sink, characteristic)
        ours, theirs = self._pair()
        theirs.setblocking(False)
        sink.attach_notify_socket(ours, 517)
        sink.start_emitter(bus=None)
        try:
            profile_id = sink._report_id
            sink.send_input_report(bytes([profile_id, 0xAA, 0xBB]))

            got = None
            for _ in range(200):
                await asyncio.sleep(0.005)
                try:
                    got = theirs.recv(64)
                    break
                except (BlockingIOError, _s.error):
                    continue
            assert got == b"\xaa\xbb", f"got {got!r}"
            # And nothing went the property route.
            assert characteristic.sent == []
        finally:
            await sink.stop_emitter()
            ours.close()
            theirs.close()

    async def test_a_closed_socket_falls_back_to_the_property_path(self):
        """bluetoothd closes its end when the host unsubscribes. Input must
        keep flowing rather than disappearing into a dead descriptor."""
        sink = make_sink(max_hz=500)
        characteristic = FakeCharacteristic()
        live(sink, characteristic)
        ours, theirs = self._pair()
        sink.attach_notify_socket(ours, 517)
        sink.start_emitter(bus=None)
        try:
            theirs.close()                      # bluetoothd goes away
            for _ in range(20):
                sink.send_input_report(b"\x01\x42")
                await asyncio.sleep(0.005)

            assert await settle(lambda: characteristic.sent), (
                "input stopped when the notification socket closed"
            )
            assert sink.stats()["notify_path"] == "properties"
        finally:
            await sink.stop_emitter()
            ours.close()

    def test_the_stats_say_which_path_is_live(self):
        sink = make_sink()
        live(sink, FakeCharacteristic())
        assert sink.stats()["notify_path"] == "properties"

        ours, theirs = self._pair()
        try:
            sink.attach_notify_socket(ours, 517)
            assert sink.stats()["notify_path"] == "socket"
            assert sink.stats()["notify_mtu"] == 517
        finally:
            ours.close()
            theirs.close()

    def test_detach_releases_the_socket(self):
        sink = make_sink()
        live(sink, FakeCharacteristic())
        ours, theirs = self._pair()
        try:
            sink.attach_notify_socket(ours, 517)
            sink.detach()
            assert sink.stats()["notify_path"] == "properties"
        finally:
            ours.close()
            theirs.close()


def _readable(sock):
    import select

    return bool(select.select([sock], [], [], 0)[0])

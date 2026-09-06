"""Two players, end to end, over real sockets: nothing of one reaches the other.

The reported failure was "player 1 gets worse when player 2 connects", and the
suspicion was that player 1's input was reaching player 2's adapter. The
performance half is fixed in ``server/bt/ble/peripheral.py`` and measured by
``tools/multiclient_harness.py``; this file pins the *routing* half, which is
the part a test can hold forever.

Two genuine sessions -- separate client ids, separate crypto, separate sockets,
separate adapters -- driving distinguishable patterns, so a crossed report is
visible in the bytes rather than inferred.
"""

from __future__ import annotations

import time

import pytest

from client.net.transport import ClientTransport
from common.state import Button, ControllerState
from server.bt.profiles import create_profile
from server.bt.sink import MockSink
from server.datapath import Datapath
from server.router import OutputChannel, Router
from server.sessions import SessionManager

PASSWORD = "isolation-test-pw"


@pytest.fixture
def server():
    """Two channels, so two players can be kept apart and observed."""
    router = Router()
    sinks = []
    for index in range(2):
        sink = MockSink(name=f"iso{index}")
        sinks.append(sink)
        router.add_channel(
            OutputChannel(
                bd_addr=f"00:00:00:00:00:{index:02X}",
                hci_name=f"iso{index}",
                profile=create_profile("generic"),
                sink=sink,
            )
        )

    sessions = SessionManager(PASSWORD, auto_approve=True)
    datapath = Datapath(
        sessions, router, bind_host="127.0.0.1", bind_port=0, realtime=False
    )
    datapath.start()
    time.sleep(0.05)

    yield datapath, router, sessions, sinks

    datapath.stop()


def connect(datapath, name):
    """A real client, recording every rumble packet it is handed.

    ``received_rumble`` is attached here rather than inspected inside a test so
    the collector is the transport's real ``on_rumble`` path -- the same one the
    GUI uses -- and not a reimplementation of it.
    """
    received: list[tuple[int, int, int, int]] = []
    transport = ClientTransport(
        PASSWORD,
        client_name=name,
        on_rumble=lambda slot, low, high, ms: received.append((slot, low, high, ms)),
    )
    transport.connect("127.0.0.1", datapath.port, timeout_ns=5_000_000_000)
    transport.received_rumble = received
    return transport


def settle(sessions, transports, timeout=3.0):
    """Block until the server has processed each client's opening control burst.

    ``connect`` returns on ACCEPT, but SET_CONTROLLERS and SET_RUMBLE follow on
    the reliable channel and are acted on by the datapath thread. Auto-assign
    runs off the first of those, so anything that forces router state before it
    lands is simply overwritten -- silently, and only sometimes.
    """
    deadline = time.monotonic() + timeout
    wanted = {t._client_id.hex() for t in transports}
    while time.monotonic() < deadline:
        for transport in transports:
            transport.service()
        live = {
            s.client_id
            for s in sessions.all_sessions()
            if s.is_approved and s.rumble_enabled
        }
        if wanted <= live:
            return True
        time.sleep(0.01)
    return False


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def pump(transports, seconds=0.3):
    """Service the clients so acks and control traffic flow."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for transport in transports:
            transport.service()
        time.sleep(0.005)


class TestTwoPlayersStayApart:
    def test_each_players_input_reaches_only_its_own_adapter(self, server):
        """Distinct button signatures, the way the reproduction drives them.

        Player 1 presses A, player 2 presses B. Adapter 1 must never emit a
        report with B set, and adapter 2 never one with A.
        """
        datapath, router, _sessions, sinks = server
        one = connect(datapath, "player-one")
        two = connect(datapath, "player-two")
        try:
            router.assign("00:00:00:00:00:00", one._client_id.hex(), 0, "one")
            router.assign("00:00:00:00:00:01", two._client_id.hex(), 0, "two")

            for _ in range(25):
                one.send_input(0, ControllerState(buttons=Button.A), request_ack=False)
                two.send_input(0, ControllerState(buttons=Button.B), request_ack=False)
                time.sleep(0.004)

            assert wait_until(lambda: sinks[0].count >= 20 and sinks[1].count >= 20)

            profile = create_profile("generic")
            buf = bytearray(64)
            a_only = bytes(
                buf[: profile.build_input_report(
                    ControllerState(buttons=Button.A), buf
                )]
            )
            b_only = bytes(
                buf[: profile.build_input_report(
                    ControllerState(buttons=Button.B), buf
                )]
            )
            assert a_only != b_only, "the two signatures must be distinguishable"

            first = {r.data for r in sinks[0].reports()}
            second = {r.data for r in sinks[1].reports()}

            assert b_only not in first, "player 2's input reached player 1's adapter"
            assert a_only not in second, "player 1's input reached player 2's adapter"
            assert first == {a_only}
            assert second == {b_only}
        finally:
            one.close()
            two.close()

    def test_the_same_slot_number_routes_to_different_adapters(self, server):
        """Both players use slot 0. The routing key is (client_id, slot), so
        the shared slot number must not collapse them onto one channel."""
        datapath, router, _sessions, sinks = server
        one = connect(datapath, "player-one")
        two = connect(datapath, "player-two")
        try:
            router.assign("00:00:00:00:00:00", one._client_id.hex(), 0, "one")
            router.assign("00:00:00:00:00:01", two._client_id.hex(), 0, "two")

            assert router.resolve(one._client_id.hex(), 0) is not router.resolve(
                two._client_id.hex(), 0
            )
        finally:
            one.close()
            two.close()

    def test_player_two_leaving_does_not_disturb_player_one(self, server):
        """Disconnect cleanup must not reach across to the other player."""
        datapath, router, _sessions, sinks = server
        one = connect(datapath, "player-one")
        two = connect(datapath, "player-two")
        try:
            router.assign("00:00:00:00:00:00", one._client_id.hex(), 0, "one")
            router.assign("00:00:00:00:00:01", two._client_id.hex(), 0, "two")
            channel_before = router.resolve(one._client_id.hex(), 0)

            two.close()
            pump([one], 0.3)

            channel_after = router.resolve(one._client_id.hex(), 0)
            assert channel_after is channel_before, "player 1 was re-routed"
            assert channel_after.assigned_client == one._client_id.hex()

            sinks[0].clear()
            for _ in range(10):
                one.send_input(0, ControllerState(buttons=Button.A), request_ack=False)
                time.sleep(0.004)
            assert wait_until(lambda: sinks[0].count >= 5), "player 1 stopped working"
        finally:
            one.close()


class TestTheRoutingInvariant:
    """`Router.assign` mutates the channel before rebuilding the route table,
    so a packet in flight can arrive at a channel that has just been handed to
    somebody else. The datapath refuses it rather than driving the wrong
    console."""

    def test_a_reassigned_channel_refuses_the_previous_owner(self, server):
        datapath, router, sessions, sinks = server
        one = connect(datapath, "player-one")
        two = connect(datapath, "player-two")
        try:
            assert settle(sessions, [one, two])

            channel = router.channel("00:00:00:00:00:00")
            router.assign("00:00:00:00:00:00", one._client_id.hex(), 0, "one")

            # Reproduce the window exactly: the channel has been handed over,
            # but the route table still names the old owner.
            channel.assigned_client = two._client_id.hex()
            router._routes = {(one._client_id.hex(), 0): channel}

            sinks[0].clear()
            before = datapath.packets_misrouted
            for _ in range(10):
                one.send_input(0, ControllerState(buttons=Button.A), request_ack=False)
                time.sleep(0.004)

            assert wait_until(lambda: datapath.packets_misrouted > before)
            assert sinks[0].count == 0, "a report was written to the wrong player"
            # If auto-assign had rebuilt the table underneath us the test would
            # have proved nothing, so check the window was still open.
            assert router.resolve(one._client_id.hex(), 0) is channel
            assert channel.assigned_client == two._client_id.hex()
        finally:
            one.close()
            two.close()

    def test_the_counter_is_reported(self, server):
        datapath, _router, _sessions, _sinks = server
        assert "packets_misrouted" in datapath.stats_snapshot()


class TestRumbleDoesNotCrossPlayers:
    """`send_rumble` is the `on_rumble` callback handed to **every** adapter and
    fires on each adapter's own Bluetooth thread. It used to encode into one
    shared buffer with no lock, so two consoles rumbling at once could hand
    player A a packet carrying player B's slot and amplitudes."""

    def test_concurrent_rumble_from_two_adapters_stays_separate(self, server):
        import threading

        datapath, router, sessions, _sinks = server
        one = connect(datapath, "player-one")
        two = connect(datapath, "player-two")
        try:
            assert settle(sessions, [one, two]), "rumble was never announced"
            router.assign("00:00:00:00:00:00", one._client_id.hex(), 0, "one")
            router.assign("00:00:00:00:00:01", two._client_id.hex(), 1, "two")

            class Command:
                is_stop = False

                def __init__(self, low, high, duration):
                    self.low_freq = low
                    self.high_freq = high
                    self.duration_ms = duration

            errors = []

            def hammer(bd_addr, low, high):
                try:
                    for _ in range(300):
                        datapath.send_rumble(bd_addr, Command(low, high, 100))
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            threads = [
                threading.Thread(
                    target=hammer, args=("00:00:00:00:00:00", 0x11, 0x22)
                ),
                threading.Thread(
                    target=hammer, args=("00:00:00:00:00:01", 0xAA, 0xBB)
                ),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            assert not errors, errors
            pump([one, two], 0.3)

            # Each client may only ever have been handed its own slot, and
            # its own adapter's amplitudes. A torn buffer shows up as either.
            one_got = set(one.received_rumble)
            two_got = set(two.received_rumble)
            assert one_got, "player 1 received no rumble at all"
            assert two_got, "player 2 received no rumble at all"

            assert {slot for slot, *_ in one_got} == {0}, (
                f"player 1 received rumble for another player's slot: {one_got}"
            )
            assert {slot for slot, *_ in two_got} == {1}, (
                f"player 2 received rumble for another player's slot: {two_got}"
            )
            assert {(low, high) for _s, low, high, _d in one_got} == {(0x11, 0x22)}, (
                f"player 1 received another adapter's amplitudes: {one_got}"
            )
            assert {(low, high) for _s, low, high, _d in two_got} == {(0xAA, 0xBB)}, (
                f"player 2 received another adapter's amplitudes: {two_got}"
            )
        finally:
            one.close()
            two.close()

    def test_a_departed_client_leaves_no_rumble_state(self, server):
        datapath, router, sessions, _sinks = server
        one = connect(datapath, "player-one")
        try:
            assert settle(sessions, [one]), "rumble was never announced"
            router.assign("00:00:00:00:00:00", one._client_id.hex(), 0, "one")

            class Command:
                is_stop = False
                low_freq = 1
                high_freq = 2
                duration_ms = 50

            datapath.send_rumble("00:00:00:00:00:00", Command())
            assert datapath._last_rumble_ns, "nothing was recorded to clean up"

            datapath._forget_rumble_state(one._client_id.hex())
            assert datapath._last_rumble_ns == {}
        finally:
            one.close()

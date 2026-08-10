"""End-to-end tests over a real UDP socket.

Runs a real server and a real client in-process with a mock Bluetooth sink.
This is the test that would catch a protocol framing mistake, a handshake
regression, or a routing bug -- the sort of thing unit tests on either half
happily miss.
"""

from __future__ import annotations

import time

import pytest

from client.input.synthetic import SyntheticBackend
from client.loop import InputLoop, SlotRuntime
from client.net.transport import ClientTransport, TransportError
from common.protocol import ControlOp
from common.state import Button, ControllerState
from server.bt.profiles import create_profile
from server.bt.sink import MockSink
from server.datapath import Datapath
from server.router import OutputChannel, Router
from server.sessions import SessionManager

PASSWORD = "integration-test-pw"


@pytest.fixture
def server():
    """A running server on an ephemeral port with mock Bluetooth."""
    router = Router()
    sinks = []
    for index in range(2):
        sink = MockSink(name=f"test{index}")
        sinks.append(sink)
        router.add_channel(
            OutputChannel(
                bd_addr=f"00:00:00:00:00:{index:02X}",
                hci_name=f"test{index}",
                profile=create_profile("generic"),
                sink=sink,
            )
        )

    sessions = SessionManager(PASSWORD, auto_approve=True)
    # Port 0 lets the OS pick, so parallel test runs never collide.
    datapath = Datapath(sessions, router, bind_host="127.0.0.1", bind_port=0, realtime=False)
    datapath.start()

    # Give the thread a moment to bind before handing out the port.
    time.sleep(0.05)

    yield datapath, router, sessions, sinks

    datapath.stop()


def connect_client(datapath, password=PASSWORD, name="test-client") -> ClientTransport:
    transport = ClientTransport(password, client_name=name)
    transport.connect("127.0.0.1", datapath.port, timeout_ns=20_000_000_000)
    return transport


def _button_a_held(sink):
    """None-safe predicate for 'the sink's latest report has A pressed'.

    Returns a callable so it can be polled -- and so it reads False rather than
    raising when no report has arrived yet.
    """

    def check() -> bool:
        report = sink.last_report()
        return report is not None and bool(report.data[11] & 0x10)

    return check


def wait_until(predicate, timeout=3.0, interval=0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestHandshake:
    def test_correct_password_connects(self, server):
        datapath, _, sessions, _ = server
        transport = connect_client(datapath)
        try:
            assert transport.is_connected
            assert sessions.count == 1
        finally:
            transport.close()

    def test_server_advertises_its_capacity(self, server):
        """This is what makes the client GUI grey out unusable slots."""
        datapath, router, _, _ = server
        transport = connect_client(datapath)
        try:
            assert transport.server_capacity == router.capacity == 2
        finally:
            transport.close()

    def test_wrong_password_is_rejected(self, server):
        datapath, _, sessions, _ = server
        with pytest.raises(TransportError, match="[Ii]ncorrect password"):
            connect_client(datapath, password="wrong-password")
        assert sessions.count == 0

    def test_unreachable_server_gives_a_useful_error(self):
        transport = ClientTransport(PASSWORD)
        with pytest.raises(TransportError, match="did not respond"):
            # Port 1 is reserved and nothing listens there.
            transport.connect("127.0.0.1", 1, timeout_ns=1_000_000_000)

    def test_reconnect_replaces_the_old_session(self, server):
        """A crashed client must be able to come straight back rather than
        being told the server is full."""
        datapath, _, sessions, _ = server

        first = connect_client(datapath)
        client_id = first._client_id
        first._sock.close()  # simulate a hard crash, no graceful DISCONNECT

        second = ClientTransport(PASSWORD, client_name="test-client")
        second._client_id = client_id
        try:
            second.connect("127.0.0.1", datapath.port, timeout_ns=20_000_000_000)
            assert second.is_connected
            assert sessions.count == 1
        finally:
            second.close()


class TestInputFlow:
    def test_input_reaches_the_bluetooth_sink(self, server):
        datapath, router, _, sinks = server
        transport = connect_client(datapath)

        try:
            session = datapath._sessions.all_sessions()[0]
            router.assign(
                "00:00:00:00:00:00", session.client_id, 0, "alice"
            )

            state = ControllerState(buttons=Button.A, left_x=12345)
            transport.send_input(0, state, request_ack=True)

            assert wait_until(lambda: sinks[0].count > 0), "no report reached the sink"

            report = sinks[0].last_report()
            assert int.from_bytes(report.data[1:3], "little", signed=True) == 12345
            assert report.data[11] & 0x10, "button A bit not set"
        finally:
            transport.close()

    def test_latency_is_measured(self, server):
        datapath, router, _, _ = server
        transport = connect_client(datapath)

        try:
            session = datapath._sessions.all_sessions()[0]
            router.assign("00:00:00:00:00:00", session.client_id, 0)

            for _ in range(10):
                transport.send_input(0, ControllerState(left_x=1), request_ack=True)
                time.sleep(0.01)
                transport.service()

            assert wait_until(
                lambda: (transport.service() or True)
                and transport.latency_snapshot().get(0, {}).get("rtt", {}).get("count", 0) > 0
            )

            rtt = transport.latency_snapshot()[0]["rtt"]
            assert rtt["count"] > 0
            # Loopback: generous ceiling, but a broken clock would blow past it.
            assert rtt["p50"] < 100.0
        finally:
            transport.close()

    def test_unassigned_input_is_counted_not_crashed(self, server):
        datapath, _, _, sinks = server
        transport = connect_client(datapath)

        try:
            # Slot 3 is never auto-assigned: the fixture has 2 adapters, and
            # auto-approve only places slot 0 at session creation.
            transport.send_input(3, ControllerState(buttons=Button.A), request_ack=False)
            assert wait_until(lambda: datapath.packets_unroutable > 0)
            assert sinks[1].count == 0
        finally:
            transport.close()

    def test_two_controllers_route_independently(self, server):
        datapath, router, _, sinks = server
        transport = connect_client(datapath)

        try:
            session = datapath._sessions.all_sessions()[0]
            router.assign("00:00:00:00:00:00", session.client_id, 0, "alice")
            router.assign("00:00:00:00:00:01", session.client_id, 1, "bob")

            transport.send_input(0, ControllerState(left_x=1111), request_ack=False)
            transport.send_input(1, ControllerState(left_x=2222), request_ack=False)

            assert wait_until(lambda: sinks[0].count > 0 and sinks[1].count > 0)

            assert int.from_bytes(sinks[0].last_report().data[1:3], "little", signed=True) == 1111
            assert int.from_bytes(sinks[1].last_report().data[1:3], "little", signed=True) == 2222
        finally:
            transport.close()

    def test_encrypted_packets_are_not_mistaken_for_handshakes(self, server):
        """Regression: the encrypted datagram used to start with the nonce
        counter, so a packet with counter 1 was dispatched as a plaintext
        HELLO. Sending well past counter 5 exercises every colliding value."""
        datapath, router, _, sinks = server
        transport = connect_client(datapath)

        try:
            session = datapath._sessions.all_sessions()[0]
            router.assign("00:00:00:00:00:00", session.client_id, 0)

            for index in range(40):
                transport.send_input(0, ControllerState(left_x=index), request_ack=False)
                time.sleep(0.002)

            assert wait_until(lambda: sinks[0].count >= 35)
            assert datapath.decrypt_failures == 0
        finally:
            transport.close()


class TestControlChannel:
    def test_usernames_reach_the_server(self, server):
        datapath, _, sessions, _ = server
        transport = connect_client(datapath)

        try:
            transport.queue_control(ControlOp.SET_USERNAME, {"slot": 0, "username": "alice"})

            def arrived() -> bool:
                transport.service()
                session = sessions.all_sessions()[0]
                return session.slot(0).username == "alice"

            assert wait_until(arrived)
        finally:
            transport.close()

    def test_graceful_disconnect_frees_the_slot(self, server):
        datapath, _, sessions, _ = server
        transport = connect_client(datapath)
        transport.close()

        assert wait_until(lambda: sessions.count == 0)


class TestFullLoop:
    def test_input_loop_streams_to_the_sink(self, server):
        """The real client stack: synthetic gamepad -> poll loop -> transport
        -> server -> HID report."""
        datapath, router, _, sinks = server

        backend = SyntheticBackend(count=1)
        backend.open()
        backend.acquire(0)

        transport = connect_client(datapath)
        loop = InputLoop(backend, transport, poll_hz=200, axis_deadband=0)

        try:
            session = datapath._sessions.all_sessions()[0]
            router.assign("00:00:00:00:00:00", session.client_id, 0, "alice")

            loop.set_slots([SlotRuntime(slot=0, instance_id=0, username="alice")])
            loop.start()

            backend.press(0, Button.A)

            assert wait_until(lambda: sinks[0].count > 0, timeout=3.0)
            assert wait_until(
                lambda: bool(sinks[0].last_report().data[11] & 0x10), timeout=2.0
            ), "button press did not reach the sink"
        finally:
            loop.stop()
            transport.close()
            backend.close()

    def test_disconnecting_a_controller_releases_held_input(self, server):
        """Otherwise a player dropping out mid-press leaves the character
        running into a wall until the session times out."""
        datapath, router, _, sinks = server

        backend = SyntheticBackend(count=1)
        backend.open()
        backend.acquire(0)

        transport = connect_client(datapath)
        loop = InputLoop(backend, transport, poll_hz=200, axis_deadband=0)

        try:
            session = datapath._sessions.all_sessions()[0]
            router.assign("00:00:00:00:00:00", session.client_id, 0)

            loop.set_slots([SlotRuntime(slot=0, instance_id=0)])
            loop.start()

            backend.press(0, Button.A)
            assert wait_until(_button_a_held(sinks[0])), "press never reached the sink"

            backend.detach(0)

            assert wait_until(
                lambda: not _button_a_held(sinks[0])(), timeout=3.0
            ), "held button was never released after disconnect"
        finally:
            loop.stop()
            transport.close()
            backend.close()

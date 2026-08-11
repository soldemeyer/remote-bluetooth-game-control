"""Rumble feedback: extraction, routing, and the two independent toggles.

The requirement is specific: rumble is transmitted only when **both** the
server and the client have it enabled, and disabling either side stops the
data being *sent* -- not merely ignored on arrival. Several tests below assert
the packet never leaves, not just that it has no effect.
"""

from __future__ import annotations

import time

import pytest

from client.net.transport import ClientTransport
from common import protocol
from common.state import Button, ControllerState
from server.bt.profiles import create_profile
from server.bt.profiles.base import RumbleCommand
from server.bt.sink import MockSink
from server.datapath import Datapath
from server.router import OutputChannel, Router
from server.sessions import SessionManager

PASSWORD = "rumble-test-password"


class TestWireFormat:
    def test_round_trip(self):
        buf = bytearray(32)
        size = protocol.encode_feedback_into(buf, 0, 2, 200, 17, 500)

        assert buf[0] == protocol.PacketType.FEEDBACK
        assert size == protocol.FEEDBACK_SIZE
        assert protocol.decode_feedback(buf, 0) == (2, 200, 17, 500)

    def test_values_are_clamped_not_wrapped(self):
        """A duration over u16 must saturate; wrapping would turn a long rumble
        into a very short one."""
        buf = bytearray(32)
        protocol.encode_feedback_into(buf, 0, 0, 300, 300, 99999)
        _, low, high, duration = protocol.decode_feedback(buf, 0)

        assert low == 300 & 0xFF
        assert duration == 0xFFFF

    def test_truncated_packet_rejected(self):
        with pytest.raises(ValueError, match="too short"):
            protocol.decode_feedback(bytearray(2), 0)

    def test_packet_stays_small(self):
        """Feedback shares the socket with input; it must not be bulky."""
        assert protocol.FEEDBACK_SIZE <= 8


class TestGenericExtraction:
    @pytest.fixture
    def profile(self):
        return create_profile("generic")

    def test_two_byte_report_is_decoded(self, profile):
        command = profile.extract_rumble(bytes([200, 100]))
        assert command is not None
        assert (command.low_freq, command.high_freq) == (200, 100)

    def test_leading_report_id_is_tolerated(self, profile):
        command = profile.extract_rumble(bytes([0x01, 200, 100]))
        assert command is not None
        assert (command.low_freq, command.high_freq) == (200, 100)

    def test_unknown_length_is_ignored(self, profile):
        """Guessing at an unfamiliar layout could turn an LED command into a
        rumble burst."""
        assert profile.extract_rumble(bytes([1, 2, 3, 4, 5, 6])) is None
        assert profile.extract_rumble(b"") is None

    def test_zero_is_a_stop(self, profile):
        command = profile.extract_rumble(bytes([0, 0]))
        assert command is not None and command.is_stop


class TestSwitchExtraction:
    @pytest.fixture
    def profile(self):
        return create_profile("switch_pro")

    def test_neutral_keepalive_is_not_an_effect(self, profile):
        """The Switch sends a neutral rumble block constantly. Treating it as
        an effect would make the pad buzz permanently."""
        neutral = bytes([0x00, 0x01, 0x40, 0x40])
        report = bytes([0x10, 0x00]) + neutral + neutral

        command = profile.extract_rumble(report)
        assert command is not None and command.is_stop

    def test_real_amplitude_is_decoded(self, profile):
        strong = bytes([0x00, 0x80, 0x40, 0x72])
        report = bytes([0x10, 0x00]) + strong + strong

        command = profile.extract_rumble(report)
        assert command is not None
        assert not command.is_stop
        assert 0 < command.low_freq <= 255
        assert 0 < command.high_freq <= 255

    def test_wrong_report_id_ignored(self, profile):
        assert profile.extract_rumble(bytes([0x30] + [0] * 12)) is None

    def test_short_report_ignored(self, profile):
        assert profile.extract_rumble(bytes([0x10, 0x00])) is None


# --------------------------------------------------------------------------
# End-to-end: the toggles
# --------------------------------------------------------------------------


@pytest.fixture
def server():
    router = Router()
    sink = MockSink(name="rumble")
    router.add_channel(
        OutputChannel(
            bd_addr="00:00:00:00:00:00",
            hci_name="rumble0",
            profile=create_profile("generic"),
            sink=sink,
        )
    )
    sessions = SessionManager(PASSWORD, auto_approve=True)
    datapath = Datapath(sessions, router, bind_host="127.0.0.1", bind_port=0, realtime=False)
    datapath.start()
    time.sleep(0.05)

    yield datapath, router, sessions

    datapath.stop()


def _connect(datapath, *, rumble_enabled=True):
    received: list[tuple] = []
    transport = ClientTransport(
        PASSWORD,
        client_name="rumble-client",
        rumble_enabled=rumble_enabled,
        on_rumble=lambda *args: received.append(args),
    )
    transport.connect("127.0.0.1", datapath.port, timeout_ns=20_000_000_000)

    # Let the SET_RUMBLE announcement reach the server.
    for _ in range(40):
        transport.service()
        time.sleep(0.02)

    return transport, received


def _assign(datapath, router, sessions):
    session = sessions.all_sessions()[0]
    router.assign("00:00:00:00:00:00", session.client_id, 0, "player")
    return session


class TestBothSidesEnabled:
    def test_rumble_reaches_the_client(self, server):
        datapath, router, sessions = server
        transport, received = _connect(datapath)
        try:
            _assign(datapath, router, sessions)
            datapath.send_rumble("00:00:00:00:00:00", RumbleCommand(200, 100))

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not received:
                transport.service()
                time.sleep(0.02)

            assert received, "rumble never arrived"
            slot, low, high, _duration = received[0]
            assert (slot, low, high) == (0, 200, 100)
        finally:
            transport.close()


class TestServerSideToggle:
    def test_nothing_is_transmitted_when_the_server_disables_it(self, server):
        """Must not send at all -- not send-and-let-the-client-discard."""
        datapath, router, sessions = server
        transport, received = _connect(datapath)
        try:
            _assign(datapath, router, sessions)
            datapath.rumble_enabled = False

            before = datapath.rumble_sent
            datapath.send_rumble("00:00:00:00:00:00", RumbleCommand(200, 100))

            for _ in range(30):
                transport.service()
                time.sleep(0.02)

            assert datapath.rumble_sent == before, "server transmitted despite being off"
            assert not received
        finally:
            transport.close()

    def test_re_enabling_resumes_transmission(self, server):
        datapath, router, sessions = server
        transport, received = _connect(datapath)
        try:
            _assign(datapath, router, sessions)

            datapath.rumble_enabled = False
            datapath.send_rumble("00:00:00:00:00:00", RumbleCommand(200, 100))
            datapath.rumble_enabled = True
            datapath.send_rumble("00:00:00:00:00:00", RumbleCommand(150, 50))

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not received:
                transport.service()
                time.sleep(0.02)

            assert received
            assert received[0][1] == 150, "received the packet sent while disabled"
        finally:
            transport.close()


class TestClientSideToggle:
    def test_server_does_not_transmit_to_an_opted_out_client(self, server):
        """The client's preference reaches the server, so the packet is never
        built -- a disabled toggle costs zero bandwidth."""
        datapath, router, sessions = server
        transport, received = _connect(datapath, rumble_enabled=False)
        try:
            session = _assign(datapath, router, sessions)
            assert session.rumble_enabled is False, "client preference did not arrive"

            before = datapath.rumble_sent
            datapath.send_rumble("00:00:00:00:00:00", RumbleCommand(200, 100))

            for _ in range(30):
                transport.service()
                time.sleep(0.02)

            assert datapath.rumble_sent == before
            assert not received
        finally:
            transport.close()

    def test_session_starts_with_rumble_off(self, server):
        """Fail-safe: never push feedback at a client that has not asked."""
        datapath, router, sessions = server
        from server.sessions import Session

        assert Session.__dataclass_fields__["rumble_enabled"].default is False

    def test_live_toggle_stops_transmission_without_reconnecting(self, server):
        datapath, router, sessions = server
        transport, received = _connect(datapath)
        try:
            session = _assign(datapath, router, sessions)
            assert session.rumble_enabled is True

            transport.set_rumble_enabled(False)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and session.rumble_enabled:
                transport.service()
                time.sleep(0.02)

            assert session.rumble_enabled is False, "server was not told"

            before = datapath.rumble_sent
            datapath.send_rumble("00:00:00:00:00:00", RumbleCommand(200, 100))
            assert datapath.rumble_sent == before
        finally:
            transport.close()

    def test_client_discards_feedback_when_locally_disabled(self, server):
        """Belt and braces: even a misbehaving server cannot buzz a pad whose
        owner turned rumble off."""
        datapath, router, sessions = server
        transport, received = _connect(datapath)
        try:
            _assign(datapath, router, sessions)
            transport.rumble_enabled = False   # local only, server not told

            datapath.send_rumble("00:00:00:00:00:00", RumbleCommand(200, 100))
            for _ in range(30):
                transport.service()
                time.sleep(0.02)

            assert not received
        finally:
            transport.close()


class TestRoutingAndThrottle:
    def test_unassigned_adapter_sends_nothing(self, server):
        """No controller is driving this adapter, so its rumble has nowhere to
        go. auto-approve assigns slot 0 on connect, so clear that first."""
        datapath, router, sessions = server
        transport, received = _connect(datapath)
        try:
            router.unassign("00:00:00:00:00:00")
            assert router.channel("00:00:00:00:00:00").is_assigned is False

            before = datapath.rumble_sent
            datapath.send_rumble("00:00:00:00:00:00", RumbleCommand(200, 100))
            assert datapath.rumble_sent == before
        finally:
            transport.close()

    def test_unknown_adapter_is_ignored(self, server):
        datapath, _, _ = server
        datapath.send_rumble("AA:BB:CC:DD:EE:FF", RumbleCommand(200, 100))  # must not raise

    def test_rapid_updates_are_throttled(self, server):
        """A console can emit rumble far faster than a player can feel it."""
        datapath, router, sessions = server
        transport, _ = _connect(datapath)
        try:
            _assign(datapath, router, sessions)

            before = datapath.rumble_sent
            for _ in range(50):
                datapath.send_rumble("00:00:00:00:00:00", RumbleCommand(200, 100))

            sent = datapath.rumble_sent - before
            assert sent < 10, f"{sent} of 50 rapid updates sent; throttle ineffective"
        finally:
            transport.close()

    def test_stop_is_never_throttled(self, server):
        """Dropping a 'stop' would leave the pad buzzing after the effect ended
        -- far worse than dropping a 'start'."""
        datapath, router, sessions = server
        transport, _ = _connect(datapath)
        try:
            _assign(datapath, router, sessions)

            datapath.send_rumble("00:00:00:00:00:00", RumbleCommand(200, 100))
            before = datapath.rumble_sent
            datapath.send_rumble("00:00:00:00:00:00", RumbleCommand(0, 0))

            assert datapath.rumble_sent == before + 1, "a stop was throttled away"
        finally:
            transport.close()

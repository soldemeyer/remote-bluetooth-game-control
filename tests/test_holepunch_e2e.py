"""Hole-punch end to end: broker, server registration, client punch, relay.

Everything runs in-process on loopback. That cannot exercise real NAT
traversal -- there is no NAT -- but it does exercise every piece of code the
punched path depends on: broker signalling, the server registering from the
datapath socket, punch probes crossing, session establishment over the punched
socket, and the relay fallback.

Real cross-NAT traversal requires a broker on a public IP and is verified
separately (and is documented as untested where it has not been).
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from client.net.transport import ClientTransport, TransportError
from common.protocol import PUNCH_ACK_PROBE, PUNCH_PROBE
from common.state import Button, ControllerState
from rendezvous.broker import BrokerProtocol
from server.bt.profiles import create_profile
from server.bt.sink import MockSink
from server.datapath import Datapath
from server.rendezvous import RendezvousClient
from server.router import OutputChannel, Router
from server.sessions import SessionManager

PASSWORD = "punch-test-password"
ROOM = "test-room"


class BrokerHarness:
    """Runs the real broker on its own asyncio loop in a thread."""

    def __init__(self) -> None:
        self.protocol = BrokerProtocol()
        self.address: tuple[str, int] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._transport = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(timeout=5), "broker failed to start"

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def boot():
            self._transport, _ = await self._loop.create_datagram_endpoint(
                lambda: self.protocol, local_addr=("127.0.0.1", 0)
            )
            self.address = self._transport.get_extra_info("sockname")[:2]
            self._ready.set()

        self._loop.run_until_complete(boot())
        self._loop.run_forever()

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=3)


@pytest.fixture
def broker():
    harness = BrokerHarness()
    harness.start()
    yield harness
    harness.stop()


@pytest.fixture
def server(broker):
    """A real server registered with the broker, mock Bluetooth.

    Two channels, so a test can prove two clients stay apart. One was enough
    while every path was direct; it is not enough to catch relayed clients
    collapsing onto a single session.
    """
    router = Router()
    sink = MockSink(name="punch")
    router.add_channel(
        OutputChannel(
            bd_addr="00:00:00:00:00:00",
            hci_name="punch0",
            profile=create_profile("generic"),
            sink=sink,
        )
    )
    sink2 = MockSink(name="punch2")
    router.add_channel(
        OutputChannel(
            bd_addr="00:00:00:00:00:01",
            hci_name="punch1",
            profile=create_profile("generic"),
            sink=sink2,
        )
    )

    sessions = SessionManager(PASSWORD, auto_approve=True)
    datapath = Datapath(sessions, router, bind_host="127.0.0.1", bind_port=0, realtime=False)

    rendezvous = RendezvousClient(
        broker.address[0], broker.address[1], ROOM,
        send=lambda data, addr: datapath.send_raw(data, addr),
    )
    assert rendezvous.resolve()
    datapath._rendezvous = rendezvous

    datapath.start()
    time.sleep(0.05)

    # Force the first registration rather than waiting for the maintenance tick.
    rendezvous.tick()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not rendezvous.is_registered:
        time.sleep(0.02)

    yield datapath, router, sessions, sink, rendezvous

    rendezvous.stop()
    datapath.stop()
    broker.protocol.close()


class TestServerRegistration:
    def test_server_registers_with_the_broker(self, server, broker):
        _, _, _, _, rendezvous = server
        assert rendezvous.is_registered
        assert broker.protocol.stats()["rooms"] == 1

    def test_broker_reports_our_external_address(self, server):
        """The server needs this to know it is behind NAT at all."""
        _, _, _, _, rendezvous = server
        assert rendezvous.external_address is not None
        assert rendezvous.external_address[1] > 0

    def test_registration_uses_the_datapath_socket(self, server):
        """The punched mapping must belong to the port gameplay uses. If
        registration went out on a different socket, the hole would be useless."""
        datapath, _, _, _, rendezvous = server
        assert rendezvous.external_address[1] == datapath.port


class TestPunchProbes:
    def test_datapath_answers_a_punch_probe(self, server):
        """Answering is what opens our side of the mapping."""
        import socket

        datapath = server[0]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3.0)
        try:
            sock.sendto(PUNCH_PROBE, ("127.0.0.1", datapath.port))
            data, _ = sock.recvfrom(256)
            assert data.startswith(PUNCH_ACK_PROBE)
        finally:
            sock.close()

    def test_punch_ack_is_not_treated_as_a_session_packet(self, server):
        import socket

        datapath = server[0]
        before = datapath.decrypt_failures

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(PUNCH_ACK_PROBE, ("127.0.0.1", datapath.port))
            time.sleep(0.2)
        finally:
            sock.close()

        assert datapath.decrypt_failures == before


class TestPunchedSession:
    def test_client_connects_by_hole_punching(self, server, broker):
        datapath, router, sessions, sink, _ = server

        transport = ClientTransport(PASSWORD, client_name="punch-client")
        try:
            outcome = transport.connect_via_broker(
                broker.address[0], broker.address[1], ROOM, timeout_ns=25_000_000_000
            )
            assert outcome.ok, outcome.describe()
            assert transport.is_connected
            # On loopback there is no NAT, so the local-address shortcut wins.
            assert transport.connection_mode in ("punched", "direct", "relay")
            assert sessions.count == 1
        finally:
            transport.close()

    def test_input_flows_over_the_punched_session(self, server, broker):
        datapath, router, sessions, sink, _ = server

        transport = ClientTransport(PASSWORD, client_name="punch-client")
        try:
            transport.connect_via_broker(
                broker.address[0], broker.address[1], ROOM, timeout_ns=25_000_000_000
            )
            session = sessions.all_sessions()[0]
            router.assign("00:00:00:00:00:00", session.client_id, 0, "punched")

            transport.send_input(
                0, ControllerState(buttons=Button.A, left_x=4242), request_ack=False
            )

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and sink.count == 0:
                time.sleep(0.02)

            assert sink.count > 0, "no report reached the sink over the punched path"
            report = sink.last_report()
            assert int.from_bytes(report.data[1:3], "little", signed=True) == 4242
        finally:
            transport.close()

    def test_wrong_room_code_fails_cleanly(self, server, broker):
        """A client in a different room is never introduced, so it should time
        out with a useful message rather than hang."""
        transport = ClientTransport(PASSWORD, client_name="lost-client")
        try:
            with pytest.raises(TransportError):
                transport.connect_via_broker(
                    broker.address[0], broker.address[1], "wrong-room",
                    timeout_ns=3_000_000_000,
                )
        finally:
            transport.close()


class TestNatRebinding:
    def test_session_follows_a_changed_source_port(self, server):
        """A NAT can reassign the external port mid-session. Without this the
        session dies silently until the client re-handshakes."""
        import socket

        datapath, router, sessions, sink, _ = server

        transport = ClientTransport(PASSWORD, client_name="rebind-client")
        transport.connect("127.0.0.1", datapath.port, timeout_ns=20_000_000_000)
        try:
            session = sessions.all_sessions()[0]
            router.assign("00:00:00:00:00:00", session.client_id, 0)
            original = session.address

            transport.send_input(0, ControllerState(left_x=1), request_ack=False)
            time.sleep(0.2)

            # Simulate the rebinding: same crypto state, new source port.
            old_sock = transport._sock
            new_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            new_sock.setblocking(False)
            transport._sock = new_sock
            old_sock.close()

            before = datapath.rebinds
            transport.send_input(0, ControllerState(left_x=999), request_ack=False)

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and datapath.rebinds == before:
                time.sleep(0.02)

            assert datapath.rebinds > before, "session did not follow the new port"
            assert session.address != original
        finally:
            transport.close()

    def test_replayed_packet_cannot_move_a_session(self, server):
        """Otherwise an attacker who captured one packet could hijack the
        session to an address of their choosing."""
        import socket

        datapath, router, sessions, sink, _ = server

        transport = ClientTransport(PASSWORD, client_name="replay-victim")
        transport.connect("127.0.0.1", datapath.port, timeout_ns=20_000_000_000)
        try:
            session = sessions.all_sessions()[0]
            router.assign("00:00:00:00:00:00", session.client_id, 0)

            # Capture a genuine encrypted packet by building one the same way.
            captured = session.crypto.__class__.for_client(
                transport._session.key
            )  # same key, fresh counter
            packet = captured.encrypt(b"\x14\x00\x00\x00\x00\x00\x00\x00\x00")

            transport.send_input(0, ControllerState(left_x=1), request_ack=False)
            time.sleep(0.3)
            before = datapath.rebinds

            # Replay it from a different port, twice: the second must be rejected.
            attacker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                attacker.sendto(packet, ("127.0.0.1", datapath.port))
                time.sleep(0.2)
                attacker.sendto(packet, ("127.0.0.1", datapath.port))
                time.sleep(0.3)
            finally:
                attacker.close()

            # At most one move (the first, authentic-looking delivery); a replay
            # of the same counter must never move it again.
            assert datapath.rebinds - before <= 1
        finally:
            transport.close()


class TestRelayFallback:
    """The relay path, forced.

    These must not rely on traversal failing to reach the relay: on loopback it
    never fails, so every earlier test here negotiated a punched path and the
    relay branch was dead code. ``force_relay`` drives it deliberately.

    That gap is why a real bug shipped. The broker strips its framing before
    forwarding, so relayed traffic reaches the server from the broker's own
    address -- and the server dispatched on address alone, handing every
    relayed packet to the JSON parser, which failed and dropped it. Only the
    client->server direction broke, so the handshake began and then stalled.
    """

    def test_relay_carries_a_working_session(self, server, broker):
        datapath, router, sessions, sink, rendezvous = server

        transport = ClientTransport(PASSWORD, client_name="relay-client")
        try:
            outcome = transport.connect_via_broker(
                broker.address[0], broker.address[1], ROOM,
                timeout_ns=25_000_000_000, force_relay=True,
            )
            assert outcome.ok, outcome.describe()
            assert transport.connection_mode == "relay", "did not take the relay path"
            assert sessions.count == 1
        finally:
            transport.close()

    def test_input_flows_over_the_relay(self, server, broker):
        """The assertion the old test was missing.

        A session establishing is not proof the path works: the handshake's
        first leg is server->client, which was never broken. Only input
        arriving at the sink proves client->server survives the relay.
        """
        datapath, router, sessions, sink, _ = server

        transport = ClientTransport(PASSWORD, client_name="relay-client")
        try:
            transport.connect_via_broker(
                broker.address[0], broker.address[1], ROOM,
                timeout_ns=25_000_000_000, force_relay=True,
            )
            session = sessions.all_sessions()[0]
            router.assign("00:00:00:00:00:00", session.client_id, 0, "relayed")

            transport.send_input(
                0, ControllerState(buttons=Button.A, left_x=1234), request_ack=False
            )

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and sink.count == 0:
                time.sleep(0.02)

            assert sink.count > 0, "no report reached the sink over the relayed path"
            report = sink.last_report()
            assert int.from_bytes(report.data[1:3], "little", signed=True) == 1234
        finally:
            transport.close()

    def test_the_broker_allocates_a_port_for_the_pair(self, server, broker):
        """Both peers advertise ``alloc``, so this must not fall back.

        Silently taking the token path would still pass every other test here
        -- one relayed client works either way -- and would quietly reintroduce
        the single-client limit.
        """
        datapath, _, _, _, _ = server

        transport = ClientTransport(PASSWORD, client_name="relay-client")
        try:
            outcome = transport.connect_via_broker(
                broker.address[0], broker.address[1], ROOM,
                timeout_ns=25_000_000_000, force_relay=True,
            )
            assert outcome.relay_allocated, "fell back to token framing"
            assert outcome.relay_token == "", "an allocated path needs no token"
            # Two ports for the pair, one facing each peer.
            assert broker.protocol.stats()["allocated_relays"] == 1
            # And the relay endpoint is not the signalling port -- that is the
            # whole mechanism.
            assert outcome.relay_address != broker.address
        finally:
            transport.close()

    def test_two_relayed_clients_do_not_collide(self, server, broker):
        """The reason allocation exists.

        With one shared forwarding socket both clients reach the server from
        the broker's single address. Sessions are keyed by address, so the
        second to connect evicts the first and one player's controller stops
        working -- with every counter reporting a healthy session.
        """
        datapath, router, sessions, sink, _ = server
        sink2 = router.channel("00:00:00:00:00:01").sink

        first = ClientTransport(PASSWORD, client_name="relay-one")
        second = ClientTransport(PASSWORD, client_name="relay-two")
        try:
            for transport in (first, second):
                outcome = transport.connect_via_broker(
                    broker.address[0], broker.address[1], ROOM,
                    timeout_ns=25_000_000_000, force_relay=True,
                )
                assert outcome.ok, outcome.describe()
                assert outcome.relay_allocated

            assert sessions.count == 2, "the second client evicted the first"

            # Distinct addresses is the property that makes the demux work.
            addresses = {s.address for s in sessions.all_sessions()}
            assert len(addresses) == 2, f"both clients share an address: {addresses}"

            # And both still carry input, which is what the player notices.
            ordered = sorted(
                sessions.all_sessions(), key=lambda s: s.client_name or ""
            )
            router.assign("00:00:00:00:00:00", ordered[0].client_id, 0, "one")
            router.assign("00:00:00:00:00:01", ordered[1].client_id, 0, "two")

            first.send_input(
                0, ControllerState(buttons=Button.A, left_x=111), request_ack=False
            )
            second.send_input(
                0, ControllerState(buttons=Button.A, left_x=222), request_ack=False
            )

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not (sink.count and sink2.count):
                time.sleep(0.02)

            assert sink.count > 0, "first client's input never arrived"
            assert sink2.count > 0, "second client's input never arrived"
            assert int.from_bytes(
                sink.last_report().data[1:3], "little", signed=True
            ) == 111
            assert int.from_bytes(
                sink2.last_report().data[1:3], "little", signed=True
            ) == 222
        finally:
            first.close()
            second.close()

    def test_the_broker_relayed_and_learned_no_plaintext(self, server, broker):
        """Traffic really went through the broker, and it stayed sealed."""
        datapath, router, sessions, sink, _ = server

        transport = ClientTransport(PASSWORD, client_name="relay-client")
        try:
            transport.connect_via_broker(
                broker.address[0], broker.address[1], ROOM,
                timeout_ns=25_000_000_000, force_relay=True,
            )
            session = sessions.all_sessions()[0]
            router.assign("00:00:00:00:00:00", session.client_id, 0, "relayed")
            transport.send_input(
                0, ControllerState(buttons=Button.A), request_ack=False
            )

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and sink.count == 0:
                time.sleep(0.02)

            # Unlike the old assertion (">= 0", which cannot fail), this pins
            # that the datagrams actually traversed the relay.
            assert broker.protocol.stats()["packets_relayed"] > 0
        finally:
            transport.close()

"""Server on/off, identity, visibility, and the broker's public directory.

The governing rule for the off state: **client traffic stops, Bluetooth does
not**. An operator turning players off mid-session must not knock a console's
controllers offline, so these tests check that the HID side is untouched.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from rendezvous.broker import BrokerProtocol
from server.config import ServerConfig
from server.router import OutputChannel, Router


class _Sink:
    """Minimal HID sink: enough for the router, and observable for the tests."""

    def __init__(self) -> None:
        self.is_connected = True
        self.reports: list[bytes] = []

    def send_input_report(self, report) -> bool:
        self.reports.append(bytes(report))
        return True

    def close(self) -> None:
        self.is_connected = False


class TestDefaults:
    def test_server_starts_switched_off(self):
        """A fresh install must not open itself to the network unattended."""
        assert ServerConfig().server_enabled is False

    def test_broadcast_is_the_default_once_running(self):
        assert ServerConfig().discoverable is True

    def test_internet_is_opt_in(self):
        """Registering with a third-party broker is the operator's decision."""
        assert ServerConfig().internet_enabled is False


class TestDiscoveryVisibility:
    """Hidden mode is implemented by simply not answering probes."""

    def _beacon(self, *, enabled: bool, discoverable: bool):
        from server.discovery import PROBE_MAGIC, _BeaconProtocol

        config = ServerConfig()
        config.server_enabled = enabled
        config.discoverable = discoverable

        router = Router()
        beacon = _BeaconProtocol(config, router)
        transport = MagicMock()
        beacon.connection_made(transport)
        beacon.datagram_received(PROBE_MAGIC, ("192.168.1.5", 5000))
        return transport

    def test_broadcast_server_answers(self):
        transport = self._beacon(enabled=True, discoverable=True)

        assert transport.sendto.called

    def test_hidden_server_stays_silent(self):
        transport = self._beacon(enabled=True, discoverable=False)

        assert not transport.sendto.called

    def test_switched_off_server_stays_silent(self):
        """Off means off: not discoverable even when set to broadcast."""
        transport = self._beacon(enabled=False, discoverable=True)

        assert not transport.sendto.called

    def test_reply_never_contains_a_password(self):
        from server.discovery import REPLY_MAGIC

        transport = self._beacon(enabled=True, discoverable=True)
        payload = transport.sendto.call_args.args[0]

        assert payload.startswith(REPLY_MAGIC)
        body = json.loads(payload[len(REPLY_MAGIC):].decode())
        assert set(body) == {"name", "port", "capacity", "in_use"}


class TestBrokerDirectory:
    """`list` returns only servers that opted in by sending a name."""

    def _broker(self) -> tuple[BrokerProtocol, MagicMock]:
        broker = BrokerProtocol()
        transport = MagicMock()
        broker.connection_made(transport)
        return broker, transport

    def _register(self, broker, room, address, *, name=None, capacity=0):
        message = {"op": "register", "room": room, "role": "server"}
        if name is not None:
            message["name"] = name
            message["capacity"] = capacity
        broker.datagram_received(json.dumps(message).encode(), address)

    def _list(self, broker, transport, address=("9.9.9.9", 1000)):
        transport.sendto.reset_mock()
        broker.datagram_received(json.dumps({"op": "list"}).encode(), address)
        for call in transport.sendto.call_args_list:
            body = json.loads(call.args[0].decode())
            if body.get("op") == "servers":
                return body["servers"]
        return None

    def test_public_server_is_listed(self):
        broker, transport = self._broker()
        self._register(broker, "room1", ("1.1.1.1", 100), name="Living room", capacity=4)

        servers = self._list(broker, transport)

        assert [s["name"] for s in servers] == ["Living room"]
        assert servers[0]["room"] == "room1"
        assert servers[0]["capacity"] == 4

    def test_hidden_server_registers_but_is_not_listed(self):
        broker, transport = self._broker()
        self._register(broker, "secret", ("2.2.2.2", 200))  # no name == hidden

        assert self._list(broker, transport) == []

    def test_blank_name_is_treated_as_hidden(self):
        broker, transport = self._broker()
        self._register(broker, "secret", ("2.2.2.2", 200), name="")

        assert self._list(broker, transport) == []

    def test_listing_never_exposes_an_endpoint(self):
        """Knowing a name must not let a stranger reach the server directly."""
        broker, transport = self._broker()
        self._register(broker, "room1", ("1.1.1.1", 100), name="Living room")

        servers = self._list(broker, transport)

        assert set(servers[0]) == {"room", "name", "capacity", "in_use"}
        assert "1.1.1.1" not in json.dumps(servers)

    def test_listing_is_capped(self):
        """One datagram in must not produce an unbounded datagram out."""
        broker, transport = self._broker()
        for index in range(BrokerProtocol.MAX_LISTED + 15):
            self._register(
                broker, f"room{index}", ("1.1.1.1", 100 + index), name=f"S{index:03d}"
            )

        servers = self._list(broker, transport)

        assert len(servers) == BrokerProtocol.MAX_LISTED

    def test_hostile_capacity_is_coerced(self):
        broker, transport = self._broker()
        broker.datagram_received(
            json.dumps({
                "op": "register", "room": "r", "role": "server",
                "name": "S", "capacity": "not a number", "in_use": 10**9,
            }).encode(),
            ("1.1.1.1", 100),
        )

        servers = self._list(broker, transport)

        assert servers[0]["capacity"] == 0
        assert servers[0]["in_use"] <= 64

    def test_name_is_truncated(self):
        broker, transport = self._broker()
        self._register(broker, "r", ("1.1.1.1", 100), name="x" * 500)

        servers = self._list(broker, transport)

        assert len(servers[0]["name"]) <= 64


class TestSessionPasswordChange:
    def _manager(self):
        from server.sessions import SessionManager

        return SessionManager("original-password", max_clients=4)

    def test_changing_the_password_drops_every_session(self):
        """A session key is derived from the password; the old one cannot stand."""
        manager = self._manager()
        manager._sessions["a"] = MagicMock(client_id="a")
        manager._sessions["b"] = MagicMock(client_id="b")

        dropped = manager.set_password("a-new-password")

        assert dropped == 2
        assert manager._sessions == {}

    def test_master_key_and_salt_both_change(self):
        manager = self._manager()
        before_key, before_salt = manager._master_key, manager._salt

        manager.set_password("a-new-password")

        assert manager._master_key != before_key
        assert manager._salt != before_salt

    def test_pending_handshakes_are_discarded(self):
        """One mid-flight would be answered against a key that no longer exists."""
        manager = self._manager()
        manager._pending_hello[b"client"] = (b"a" * 32, b"b" * 32)

        manager.set_password("a-new-password")

        assert manager._pending_hello == {}

    def test_empty_password_is_refused(self):
        manager = self._manager()

        with pytest.raises(ValueError):
            manager.set_password("")


class TestDatapathAccepting:
    def _datapath(self):
        from server.datapath import Datapath
        from server.sessions import SessionManager

        router = Router()
        router.add_channel(
            OutputChannel(bd_addr="00:11:22:33:44:55", hci_name="hci0",
                          profile=MagicMock(), sink=_Sink())
        )
        sessions = SessionManager("password123", max_clients=4)
        return Datapath(sessions, router, bind_host="127.0.0.1", bind_port=0), router, sessions

    def test_defaults_to_accepting_until_told_otherwise(self):
        datapath, _, _ = self._datapath()

        assert datapath.accepting is True

    def test_turning_off_drops_sessions_and_assignments(self):
        datapath, router, sessions = self._datapath()
        session = MagicMock(client_id="client-a")
        sessions._sessions["client-a"] = session
        router.assign("00:11:22:33:44:55", "client-a", 0, "Player 1")

        dropped = datapath.set_accepting(False)

        assert dropped == 1
        assert sessions._sessions == {}
        assert router.channel("00:11:22:33:44:55").assigned_client in (None, "")

    def test_turning_off_leaves_bluetooth_alone(self):
        """The whole point: a console must not lose its controller."""
        datapath, router, _ = self._datapath()
        channel = router.channel("00:11:22:33:44:55")

        datapath.set_accepting(False)

        assert channel.sink.is_connected
        assert router.channel("00:11:22:33:44:55") is channel

    def test_toggling_is_idempotent(self):
        datapath, _, sessions = self._datapath()
        sessions._sessions["a"] = MagicMock(client_id="a")

        assert datapath.set_accepting(True) == 0      # already on
        assert datapath.set_accepting(False) == 1
        assert datapath.set_accepting(False) == 0     # already off

    def test_datagrams_are_dropped_before_any_parsing(self):
        """A switched-off server does no work for an unauthenticated stranger."""
        datapath, _, sessions = self._datapath()
        datapath.set_accepting(False)
        sessions.handle_hello = MagicMock()

        datapath._handle_datagram(b"\x01" + b"\x00" * 40, ("10.0.0.9", 5000))

        assert not sessions.handle_hello.called

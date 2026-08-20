"""Relaying without trusting the source address.

Relay was routed on the address the broker observed, which is exactly what a
proxy in front of it destroys: two peers arriving from one address collapse
into a single route, and their traffic is misdelivered rather than merely slow.

A token issued at registration names the flow by content instead. Framed
packets are ``RELAY_MAGIC || token || payload``; the broker strips the header
and forwards, learning each peer's current address as it goes so a NAT rebind
(or a proxy re-flowing) does not break the route.

Un-upgraded peers still relay the old way, so the two must coexist.
"""

from __future__ import annotations

import json
import socket

import pytest

from common import protocol
from rendezvous import broker as broker_mod
from tests.test_broker_deployment import BrokerHarness, Peer, UdpForwarder


@pytest.fixture
def broker():
    harness = BrokerHarness()
    yield harness
    harness.stop()


def _register(peer: Peer, room: str, role: str) -> str:
    """Register and return the relay token the broker issued."""
    peer.send({"op": "register", "room": room, "role": role})
    return peer.receive("registered")["token"]


def _framed(token: str, payload: bytes) -> bytes:
    return protocol.RELAY_MAGIC + bytes.fromhex(token) + payload


class TestTheConstantsCannotDrift:
    """The broker defines these itself so its image needs only the stdlib."""

    def test_the_two_definitions_agree(self):
        assert broker_mod.RELAY_MAGIC == protocol.RELAY_MAGIC
        assert broker_mod.RELAY_TOKEN_BYTES == protocol.RELAY_TOKEN_BYTES
        assert broker_mod._RELAY_HEADER == protocol.RELAY_HEADER_BYTES

    def test_the_broker_still_imports_nothing_of_ours(self):
        """Its container image is Python plus `rendezvous/`, and no more."""
        import pathlib

        source = pathlib.Path(broker_mod.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            if line.startswith(("import ", "from ")):
                assert not line.split()[1].startswith(
                    ("common", "client", "server", "videoserver")
                ), line

    def test_the_prefix_is_distinguishable_from_everything_else(self):
        """It is checked before any session exists, so it must be unambiguous."""
        assert not protocol.RELAY_MAGIC.startswith(protocol.PUNCH_PROBE[:4])
        assert not protocol.RELAY_MAGIC.startswith(b"{")
        assert protocol.RELAY_MAGIC[0] not in {t.value for t in protocol.PacketType}


class TestTokensAreIssuedAndKept:
    def test_registration_hands_one_out(self, broker):
        peer = Peer(broker.port)
        try:
            token = _register(peer, "abc", "server")
            assert len(token) == protocol.RELAY_TOKEN_BYTES * 2      # hex
            bytes.fromhex(token)                                     # parses
        finally:
            peer.close()

    def test_it_survives_re_registration(self, broker):
        """Peers renew every 20 s; a new token each time would drop the route."""
        peer = Peer(broker.port)
        try:
            first = _register(peer, "abc", "server")
            second = _register(peer, "abc", "server")
            assert first == second
        finally:
            peer.close()

    def test_two_peers_get_different_tokens(self, broker):
        server = Peer(broker.port)
        client = Peer(broker.port)
        try:
            assert _register(server, "abc", "server") != _register(client, "abc", "client")
        finally:
            server.close()
            client.close()


class TestRelayingByToken:
    def _relaying_pair(self, broker):
        server = Peer(broker.port)
        client = Peer(broker.port)
        server_token = _register(server, "abc", "server")
        client_token = _register(client, "abc", "client")
        server.receive("peer")
        client.receive("peer")

        client.send({"op": "relay", "peer": list(server.address)})
        # Both sides are told, so both have a JSON message queued. Draining it
        # matters: the relayed payloads asserted below arrive on the same
        # socket, and an undrained signalling message would be read instead.
        client.receive("relaying")
        server.receive("relaying")
        return server, client, server_token, client_token

    def test_a_framed_packet_reaches_the_peer(self, broker):
        server, client, _server_token, client_token = self._relaying_pair(broker)
        try:
            client.sock.sendto(
                _framed(client_token, b"hello over the relay"), client.target
            )
            data, source = server.sock.recvfrom(65535)

            assert data == b"hello over the relay", "the header was not stripped"
            assert source[:2] == ("127.0.0.1", broker.port)
        finally:
            server.close()
            client.close()

    def test_it_works_in_both_directions(self, broker):
        server, client, server_token, client_token = self._relaying_pair(broker)
        try:
            client.sock.sendto(_framed(client_token, b"up"), client.target)
            assert server.sock.recvfrom(65535)[0] == b"up"

            server.sock.sendto(_framed(server_token, b"down"), server.target)
            assert client.sock.recvfrom(65535)[0] == b"down"
        finally:
            server.close()
            client.close()

    def test_an_unknown_token_is_dropped(self, broker):
        server, client, _server_token, _client_token = self._relaying_pair(broker)
        try:
            client.sock.sendto(_framed("aa" * 16, b"nobody"), client.target)
            server.sock.settimeout(0.5)
            with pytest.raises(socket.timeout):
                server.sock.recvfrom(65535)
        finally:
            server.close()
            client.close()

    def test_a_truncated_frame_is_dropped(self, broker):
        server, client, _server_token, client_token = self._relaying_pair(broker)
        try:
            client.sock.sendto(protocol.RELAY_MAGIC + b"\x00\x01", client.target)
            server.sock.settimeout(0.5)
            with pytest.raises(socket.timeout):
                server.sock.recvfrom(65535)
        finally:
            server.close()
            client.close()

    def test_the_old_address_keyed_path_still_works(self, broker):
        """An un-upgraded peer must keep relaying."""
        server, client, _server_token, _client_token = self._relaying_pair(broker)
        try:
            client.sock.sendto(b"unframed payload", client.target)
            assert server.sock.recvfrom(65535)[0] == b"unframed payload"
        finally:
            server.close()
            client.close()


class TestRelayingSurvivesAProxy:
    """The case the whole change exists for.

    Behind a proxy every peer reaches the broker from one address, so the
    address-keyed table has nothing to tell them apart -- one route overwrites
    the other and traffic goes to the wrong peer. Tokens are unaffected.
    """

    def test_two_peers_behind_one_forwarder_are_not_confused(self, broker):
        forwarder = UdpForwarder(broker.port)
        server = Peer(broker.port)
        client = Peer(broker.port, via_port=forwarder.port)
        try:
            server_token = _register(server, "xyz", "server")
            client_token = _register(client, "xyz", "client")
            server.receive("peer")
            client.receive("peer")

            client.send({"op": "relay", "peer": list(server.address)})
            client.receive("relaying")
            server.receive("relaying")

            client.sock.sendto(_framed(client_token, b"from the client"), client.target)
            assert server.sock.recvfrom(65535)[0] == b"from the client"

            server.sock.sendto(_framed(server_token, b"from the server"), server.target)
            assert client.sock.recvfrom(65535)[0] == b"from the server"
        finally:
            forwarder.stop()
            server.close()
            client.close()

    def test_a_peer_that_changes_address_keeps_its_route(self, broker):
        """A NAT rebind, or a proxy opening a new flow, must not end the relay."""
        server, client, _server_token, client_token = self._pair(broker)
        try:
            client.sock.sendto(_framed(client_token, b"first"), client.target)
            assert server.sock.recvfrom(65535)[0] == b"first"

            # Same token, brand new socket -- which is what a rebind looks like
            # from the broker's side.
            moved = Peer(broker.port)
            try:
                moved.sock.sendto(_framed(client_token, b"after moving"), moved.target)
                assert server.sock.recvfrom(65535)[0] == b"after moving"

                # And the reply must follow it to the new address.
                server.sock.sendto(_framed(_token_of(server), b"reply"), server.target)
                assert moved.sock.recvfrom(65535)[0] == b"reply"
            finally:
                moved.close()
        finally:
            server.close()
            client.close()

    def _pair(self, broker):
        server = Peer(broker.port)
        client = Peer(broker.port)
        server._token = _register(server, "abc", "server")
        client_token = _register(client, "abc", "client")
        server.receive("peer")
        client.receive("peer")
        client.send({"op": "relay", "peer": list(server.address)})
        client.receive("relaying")
        server.receive("relaying")
        return server, client, server._token, client_token


def _token_of(peer: Peer) -> str:
    return peer._token

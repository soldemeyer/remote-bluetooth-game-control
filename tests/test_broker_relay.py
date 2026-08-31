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
import time

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


class TestAllocatedRelayEndpoints:
    """A dedicated port per peer, which is the frps UDP-proxy model.

    Token framing carries one relayed pair correctly and no more: the broker
    forwards from its single signalling socket, so every relayed peer arrives at
    the far side from one address. A server keying sessions by address then sees
    one client however many are connected, and the second to join evicts the
    first -- with every counter still reporting a healthy session.

    Allocation is opt-in from *both* peers, because it changes the address each
    of them sees the other at. An older peer would keep framing to the
    signalling port and never be heard.
    """

    def _register_alloc(self, peer: Peer, room: str, role: str) -> str:
        peer.send({"op": "register", "room": room, "role": role, "alloc": True})
        return peer.receive("registered")["token"]

    def _allocated_pair(self, broker):
        server = Peer(broker.port)
        client = Peer(broker.port)
        self._register_alloc(server, "abc", "server")
        self._register_alloc(client, "abc", "client")
        server.receive("peer")
        client.receive("peer")

        client.send({"op": "relay", "peer": list(server.address), "alloc": True})
        client_reply = client.receive("relaying")
        server_reply = server.receive("relaying")
        return server, client, server_reply, client_reply

    def test_both_peers_are_given_a_port(self, broker):
        server, client, server_reply, client_reply = self._allocated_pair(broker)
        try:
            assert "relay_port" in client_reply
            assert "relay_port" in server_reply
            # One each, not one shared -- sharing is the bug being fixed.
            assert client_reply["relay_port"] != server_reply["relay_port"]
            for reply in (client_reply, server_reply):
                assert (
                    broker_mod.RELAY_PORT_MIN
                    <= reply["relay_port"]
                    <= broker_mod.RELAY_PORT_MAX
                )
        finally:
            server.close()
            client.close()

    def test_only_the_port_travels(self, broker):
        """The broker cannot know which of its addresses a peer reaches it at.

        Any address it named for itself would be the wrong one exactly when it
        sits behind a proxy -- so the peer keeps the address it already uses.
        """
        server, client, _server_reply, client_reply = self._allocated_pair(broker)
        try:
            assert "relay_host" not in client_reply
            assert isinstance(client_reply["relay_port"], int)
        finally:
            server.close()
            client.close()

    def test_traffic_crosses_unframed(self, broker):
        server, client, server_reply, client_reply = self._allocated_pair(broker)
        try:
            endpoint = (broker.host, client_reply["relay_port"])
            client.sock.sendto(b"no framing needed", endpoint)

            data, source = server.sock.recvfrom(65535)
            assert data == b"no framing needed"
            # And it arrives from *our* allocated port, not the signalling one.
            assert source[1] == server_reply["relay_port"]
            assert source[1] != broker.port
        finally:
            server.close()
            client.close()

    def test_the_reply_comes_back(self, broker):
        server, client, server_reply, client_reply = self._allocated_pair(broker)
        try:
            client.sock.sendto(b"out", (broker.host, client_reply["relay_port"]))
            server.sock.recvfrom(65535)

            server.sock.sendto(b"back", (broker.host, server_reply["relay_port"]))
            data, source = client.sock.recvfrom(65535)
            assert data == b"back"
            assert source[1] == client_reply["relay_port"]
        finally:
            server.close()
            client.close()

    def test_two_clients_reach_the_server_at_different_addresses(self, broker):
        """The property the whole mechanism exists for."""
        server = Peer(broker.port)
        first = Peer(broker.port)
        second = Peer(broker.port)
        try:
            self._register_alloc(server, "abc", "server")
            self._register_alloc(first, "abc", "client")
            self._register_alloc(second, "abc", "client")
            for peer in (server, first, second):
                peer.drain()

            sources = set()
            for client in (first, second):
                client.send({"op": "relay", "peer": list(server.address)})
                reply = client.receive("relaying")
                server.receive("relaying")
                client.sock.sendto(b"hello", (broker.host, reply["relay_port"]))
                sources.add(server.sock.recvfrom(65535)[1][1])

            assert len(sources) == 2, "both clients arrived from one address"
        finally:
            server.close()
            first.close()
            second.close()

    def test_a_peer_that_cannot_allocate_still_relays(self, broker):
        """One old peer must not cost the pair its relay."""
        server = Peer(broker.port)
        client = Peer(broker.port)
        try:
            _register(server, "abc", "server")               # no "alloc"
            self._register_alloc(client, "abc", "client")
            server.receive("peer")
            client.receive("peer")

            client.send({"op": "relay", "peer": list(server.address)})
            reply = client.receive("relaying")
            server.receive("relaying")

            assert "relay_port" not in reply, "allocated despite an old peer"
        finally:
            server.close()
            client.close()

    def test_a_repeated_request_does_not_allocate_twice(self, broker):
        """`_request_relay` resends every second until answered."""
        server, client, _server_reply, client_reply = self._allocated_pair(broker)
        try:
            client.send({"op": "relay", "peer": list(server.address)})
            again = client.receive("relaying")

            assert again["relay_port"] == client_reply["relay_port"]
            assert broker.protocol.stats()["allocated_relays"] == 1
        finally:
            server.close()
            client.close()

    def test_saying_goodbye_releases_the_ports(self, broker):
        server, client, _server_reply, _client_reply = self._allocated_pair(broker)
        try:
            assert broker.protocol.stats()["allocated_relays"] == 1

            client.send({"op": "bye"})
            broker.wait_for(
                lambda: broker.protocol.stats()["allocated_relays"] == 0
            )
        finally:
            server.close()
            client.close()


class TestTokenTablesDoNotLeak:
    """They were cleaned nowhere at all, so they grew for the process's life.

    Note the expiry has to do it. Once a token relay is wired, ``_relay_routes``
    forwards *everything* arriving from that address -- a ``bye`` included, which
    is relayed to the peer rather than acted on. That is correct for an opaque
    relay and it means the idle timeout is the only thing that can reap these.
    """

    def test_the_idle_sweep_forgets_the_token_route(self, broker):
        server = Peer(broker.port)
        client = Peer(broker.port)
        try:
            _register(server, "abc", "server")
            _register(client, "abc", "client")
            server.receive("peer")
            client.receive("peer")
            client.send({"op": "relay", "peer": list(server.address)})
            client.receive("relaying")
            server.receive("relaying")

            broker.wait_for(lambda: bool(broker.protocol._token_routes))
            assert broker.protocol._tokens

            # Age the session past the idle limit rather than waiting for it.
            protocol = broker.protocol
            stale = time.monotonic() - broker_mod.RELAY_TTL_S - 1
            for address in list(protocol._relay_seen):
                protocol._relay_seen[address] = stale
            protocol._prune_all()

            assert not protocol._token_routes, "token route survived expiry"
            assert not protocol._tokens, "token survived expiry"
        finally:
            server.close()
            client.close()

    def test_an_expired_allocation_gives_its_ports_back(self, broker):
        """Each holds a bound socket, so leaking one exhausts the range."""
        server = Peer(broker.port)
        client = Peer(broker.port)
        try:
            for peer, role in ((server, "server"), (client, "client")):
                peer.send({"op": "register", "room": "abc", "role": role,
                           "alloc": True})
                peer.receive("registered")
            server.receive("peer")
            client.receive("peer")
            client.send({"op": "relay", "peer": list(server.address)})
            client.receive("relaying")
            server.receive("relaying")

            protocol = broker.protocol
            broker.wait_for(lambda: protocol.stats()["allocated_relays"] == 1)
            free_before = protocol.stats()["relay_ports_free"]

            stale = time.monotonic() - broker_mod.RELAY_TTL_S - 1
            for allocations in protocol._allocations.values():
                for allocation in allocations:
                    allocation.last_seen = stale
            protocol._prune_all()

            assert protocol.stats()["allocated_relays"] == 0
            assert protocol.stats()["relay_ports_free"] > free_before
        finally:
            server.close()
            client.close()

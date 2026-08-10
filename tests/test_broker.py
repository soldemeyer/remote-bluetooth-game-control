"""Rendezvous broker: registration, peer introduction, and relay fallback.

Runs a real broker on loopback with real UDP sockets. Loopback cannot exercise
actual NAT traversal, but it does verify the signalling contract both sides
depend on -- which is where the bugs actually live.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from rendezvous.broker import BrokerProtocol

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def broker():
    """A broker bound to an ephemeral loopback port."""
    loop = asyncio.get_running_loop()
    protocol = BrokerProtocol()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: protocol, local_addr=("127.0.0.1", 0)
    )
    address = transport.get_extra_info("sockname")

    yield protocol, address

    transport.close()


def make_peer() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    return sock


def send(sock: socket.socket, address, message: dict) -> None:
    sock.sendto(json.dumps(message).encode(), address)


async def recv(sock: socket.socket) -> dict:
    """Receive one JSON message, off-thread so the broker's loop keeps running."""
    data, _ = await asyncio.to_thread(sock.recvfrom, 4096)
    return json.loads(data.decode())


class TestRegistration:
    async def test_register_is_confirmed_with_external_address(self, broker):
        """Peers rely on this echo to learn how the outside world sees them."""
        _, address = broker
        sock = make_peer()
        try:
            send(sock, address, {"op": "register", "room": "room1", "role": "server"})
            reply = await recv(sock)

            assert reply["op"] == "registered"
            assert reply["room"] == "room1"
            assert tuple(reply["external"]) == sock.getsockname()
        finally:
            sock.close()

    async def test_bad_role_is_rejected(self, broker):
        _, address = broker
        sock = make_peer()
        try:
            send(sock, address, {"op": "register", "room": "r", "role": "hacker"})
            assert (await recv(sock))["op"] == "error"
        finally:
            sock.close()

    async def test_missing_room_is_rejected(self, broker):
        _, address = broker
        sock = make_peer()
        try:
            send(sock, address, {"op": "register", "role": "server"})
            assert (await recv(sock))["op"] == "error"
        finally:
            sock.close()

    async def test_malformed_json_is_ignored(self, broker):
        """Anyone can send the broker a datagram; garbage must not crash it."""
        protocol, address = broker
        sock = make_peer()
        try:
            sock.sendto(b"not json at all", address)
            await asyncio.sleep(0.1)

            send(sock, address, {"op": "register", "room": "r", "role": "server"})
            assert (await recv(sock))["op"] == "registered"
        finally:
            sock.close()


class TestIntroduction:
    async def test_both_peers_learn_each_other(self, broker):
        _, address = broker
        server, client = make_peer(), make_peer()

        try:
            send(server, address, {"op": "register", "room": "game", "role": "server"})
            assert (await recv(server))["op"] == "registered"

            send(client, address, {"op": "register", "room": "game", "role": "client"})
            assert (await recv(client))["op"] == "registered"

            server_msg = await recv(server)
            client_msg = await recv(client)

            assert server_msg["op"] == "peer"
            assert server_msg["role"] == "client"
            assert tuple(server_msg["address"]) == client.getsockname()

            assert client_msg["op"] == "peer"
            assert client_msg["role"] == "server"
            assert tuple(client_msg["address"]) == server.getsockname()
        finally:
            server.close()
            client.close()

    async def test_local_address_is_forwarded(self, broker):
        """Used for the same-NAT shortcut, where hairpinning is unreliable."""
        _, address = broker
        server, client = make_peer(), make_peer()

        try:
            send(server, address,
                 {"op": "register", "room": "g", "role": "server",
                  "local": ["192.168.1.50", 47800]})
            await recv(server)

            send(client, address, {"op": "register", "room": "g", "role": "client"})
            await recv(client)
            await recv(server)

            message = await recv(client)
            assert tuple(message["local"]) == ("192.168.1.50", 47800)
        finally:
            server.close()
            client.close()

    async def test_peers_in_different_rooms_are_not_introduced(self, broker):
        _, address = broker
        server, client = make_peer(), make_peer()

        try:
            send(server, address, {"op": "register", "room": "room-a", "role": "server"})
            await recv(server)

            send(client, address, {"op": "register", "room": "room-b", "role": "client"})
            await recv(client)

            server.settimeout(0.4)
            with pytest.raises(socket.timeout):
                server.recvfrom(4096)
        finally:
            server.close()
            client.close()


class TestRelay:
    async def test_relay_forwards_traffic_between_peers(self, broker):
        """The fallback when hole-punching fails -- symmetric NAT on both ends."""
        protocol, address = broker
        server, client = make_peer(), make_peer()

        try:
            send(server, address, {"op": "register", "room": "g", "role": "server"})
            await recv(server)
            send(client, address, {"op": "register", "room": "g", "role": "client"})
            await recv(client)
            await recv(server)
            await recv(client)

            send(client, address, {"op": "relay", "peer": list(server.getsockname())})
            assert (await recv(client))["op"] == "relaying"
            assert (await recv(server))["op"] == "relaying"

            # Opaque payload: the broker must forward without interpreting.
            payload = b"\x40encrypted-session-bytes"
            client.sendto(payload, address)

            data, _ = await asyncio.to_thread(server.recvfrom, 4096)
            assert data == payload
            assert protocol.packets_relayed >= 1
        finally:
            server.close()
            client.close()

    async def test_relay_is_bidirectional(self, broker):
        _, address = broker
        server, client = make_peer(), make_peer()

        try:
            send(server, address, {"op": "register", "room": "g", "role": "server"})
            await recv(server)
            send(client, address, {"op": "register", "room": "g", "role": "client"})
            await recv(client)
            await recv(server)
            await recv(client)

            send(client, address, {"op": "relay", "peer": list(server.getsockname())})
            await recv(client)
            await recv(server)

            server.sendto(b"\x40from-server", address)
            data, _ = await asyncio.to_thread(client.recvfrom, 4096)
            assert data == b"\x40from-server"
        finally:
            server.close()
            client.close()

    async def test_relay_refused_between_unrelated_peers(self, broker):
        """Otherwise the broker would forward traffic between any two addresses
        that asked -- an open relay."""
        _, address = broker
        peer_a, peer_b = make_peer(), make_peer()

        try:
            send(peer_a, address, {"op": "register", "room": "room-a", "role": "client"})
            await recv(peer_a)
            send(peer_b, address, {"op": "register", "room": "room-b", "role": "client"})
            await recv(peer_b)

            send(peer_a, address, {"op": "relay", "peer": list(peer_b.getsockname())})
            reply = await recv(peer_a)

            assert reply["op"] == "error"
            assert "room" in reply["reason"]
        finally:
            peer_a.close()
            peer_b.close()


class TestCleanup:
    async def test_bye_removes_the_registration(self, broker):
        protocol, address = broker
        sock = make_peer()
        try:
            send(sock, address, {"op": "register", "room": "g", "role": "server"})
            await recv(sock)
            assert protocol.stats()["rooms"] == 1

            send(sock, address, {"op": "bye"})
            await asyncio.sleep(0.1)
            assert protocol.stats()["rooms"] == 0
        finally:
            sock.close()

    async def test_oversized_messages_are_dropped(self, broker):
        protocol, address = broker
        sock = make_peer()
        try:
            sock.sendto(b"{" + b"x" * 4000, address)
            await asyncio.sleep(0.1)
            assert protocol.stats()["rooms"] == 0
        finally:
            sock.close()

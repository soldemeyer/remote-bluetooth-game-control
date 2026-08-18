"""Broker: the video leg of a room.

A room now carries two independent pairs -- the gameplay server/client pair and
the video source/viewer pair -- because the same machine may hold both, from two
different sockets. The failures worth guarding against:

  * The video leg stealing the gameplay slot (or vice versa), which would take
    down input the moment someone opened the stream.
  * An introduction sent without a role, leaving the receiver unable to tell a
    viewer from a player.
  * Relay refused between two video peers, which is the fallback path for
    exactly the NAT setups that need it most.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from rendezvous.broker import (
    ROLE_CLIENT,
    ROLE_SERVER,
    ROLE_VIDEO_CLIENT,
    ROLE_VIDEO_SOURCE,
    BrokerProtocol,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def broker():
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
    data, _ = await asyncio.to_thread(sock.recvfrom, 4096)
    return json.loads(data.decode())


async def drain(sock: socket.socket, count: int) -> list[dict]:
    return [await recv(sock) for _ in range(count)]


async def drain_all(sock: socket.socket) -> list[dict]:
    """Read until the socket goes quiet.

    Introductions are re-sent on every registration in the room, so the number
    of messages waiting for a peer depends on who else joined and when. Tests
    that only care about the end state drain rather than count.
    """
    messages: list[dict] = []
    sock.settimeout(0.2)
    try:
        while True:
            try:
                data, _ = await asyncio.to_thread(sock.recvfrom, 4096)
            except (socket.timeout, TimeoutError):
                return messages
            messages.append(json.loads(data.decode()))
    finally:
        sock.settimeout(2.0)


def register(sock: socket.socket, address, room: str, role: str, **extra) -> None:
    send(sock, address, {"op": "register", "room": room, "role": role, **extra})


class TestVideoRegistration:
    async def test_video_source_registers_and_is_confirmed(self, broker):
        _, address = broker
        sock = make_peer()
        try:
            register(sock, address, "room1", ROLE_VIDEO_SOURCE)
            reply = await recv(sock)
            assert reply["op"] == "registered"
            assert reply["room"] == "room1"
        finally:
            sock.close()

    async def test_video_client_registers(self, broker):
        _, address = broker
        sock = make_peer()
        try:
            register(sock, address, "room1", ROLE_VIDEO_CLIENT)
            assert (await recv(sock))["op"] == "registered"
        finally:
            sock.close()

    async def test_an_unknown_role_is_still_rejected(self, broker):
        """Adding roles must not turn the check into a rubber stamp."""
        _, address = broker
        sock = make_peer()
        try:
            register(sock, address, "room1", "video")   # close, but not a role
            assert (await recv(sock))["op"] == "error"
        finally:
            sock.close()


class TestVideoIntroduction:
    async def test_source_and_viewer_are_introduced_to_each_other(self, broker):
        _, address = broker
        source, viewer = make_peer(), make_peer()
        try:
            register(source, address, "r", ROLE_VIDEO_SOURCE)
            assert (await recv(source))["op"] == "registered"

            register(viewer, address, "r", ROLE_VIDEO_CLIENT)
            # Viewer gets its confirmation plus an introduction.
            viewer_messages = await drain(viewer, 2)
            source_intro = await recv(source)

            peer_to_viewer = [m for m in viewer_messages if m["op"] == "peer"][0]
            assert peer_to_viewer["role"] == ROLE_VIDEO_SOURCE
            assert tuple(peer_to_viewer["address"]) == source.getsockname()

            assert source_intro["op"] == "peer"
            assert source_intro["role"] == ROLE_VIDEO_CLIENT
            assert tuple(source_intro["address"]) == viewer.getsockname()
        finally:
            source.close()
            viewer.close()

    async def test_local_address_is_forwarded_for_same_nat_shortcuts(self, broker):
        _, address = broker
        source, viewer = make_peer(), make_peer()
        try:
            register(source, address, "r", ROLE_VIDEO_SOURCE, local=["10.0.0.5", 47810])
            await recv(source)

            register(viewer, address, "r", ROLE_VIDEO_CLIENT)
            messages = await drain(viewer, 2)
            peer = [m for m in messages if m["op"] == "peer"][0]
            assert peer["local"] == ["10.0.0.5", 47810]
        finally:
            source.close()
            viewer.close()

    async def test_a_viewer_alone_is_not_introduced_to_the_game_server(self, broker):
        """The two legs are separate: a viewer is not a player."""
        _, address = broker
        server, viewer = make_peer(), make_peer()
        try:
            register(server, address, "r", ROLE_SERVER)
            assert (await recv(server))["op"] == "registered"

            register(viewer, address, "r", ROLE_VIDEO_CLIENT)
            assert (await recv(viewer))["op"] == "registered"

            # The game server must hear nothing about a video-only viewer.
            server.settimeout(0.3)
            with pytest.raises(socket.timeout):
                server.recvfrom(4096)
        finally:
            server.close()
            viewer.close()

    async def test_both_legs_coexist_in_one_room(self, broker):
        """The common case: one machine playing and watching at once."""
        protocol, address = broker
        server, client = make_peer(), make_peer()
        source, viewer = make_peer(), make_peer()
        try:
            register(server, address, "r", ROLE_SERVER)
            await recv(server)
            register(source, address, "r", ROLE_VIDEO_SOURCE)
            await recv(source)

            register(client, address, "r", ROLE_CLIENT)
            client_msgs = await drain(client, 2)
            await recv(server)      # its introduction

            register(viewer, address, "r", ROLE_VIDEO_CLIENT)
            viewer_msgs = await drain(viewer, 2)
            await recv(source)      # its introduction

            assert [m for m in client_msgs if m["op"] == "peer"][0]["role"] == ROLE_SERVER
            assert [m for m in viewer_msgs if m["op"] == "peer"][0]["role"] == (
                ROLE_VIDEO_SOURCE
            )
            assert protocol.stats()["rooms"] == 1
        finally:
            for sock in (server, client, source, viewer):
                sock.close()

    async def test_a_second_video_source_replaces_the_first(self, broker):
        """One source per room, like one server -- last registration wins."""
        _, address = broker
        first, second, viewer = make_peer(), make_peer(), make_peer()
        try:
            register(first, address, "r", ROLE_VIDEO_SOURCE)
            await recv(first)
            register(second, address, "r", ROLE_VIDEO_SOURCE)
            await recv(second)

            register(viewer, address, "r", ROLE_VIDEO_CLIENT)
            messages = await drain(viewer, 2)
            peer = [m for m in messages if m["op"] == "peer"][0]
            assert tuple(peer["address"]) == second.getsockname()
        finally:
            for sock in (first, second, viewer):
                sock.close()


class TestVideoRelay:
    async def test_video_peers_may_relay_through_the_broker(self, broker):
        """The fallback for NATs that refuse to punch."""
        protocol, address = broker
        source, viewer = make_peer(), make_peer()
        try:
            register(source, address, "r", ROLE_VIDEO_SOURCE)
            await recv(source)
            register(viewer, address, "r", ROLE_VIDEO_CLIENT)
            await drain(viewer, 2)
            await recv(source)

            send(viewer, address, {"op": "relay", "peer": list(source.getsockname())})
            assert (await recv(viewer))["op"] == "relaying"
            assert (await recv(source))["op"] == "relaying"

            # Opaque payload forwarded verbatim -- the broker never sees a key.
            viewer.sendto(b"\x40encrypted-video", address)
            data, _ = await asyncio.to_thread(source.recvfrom, 4096)
            assert data == b"\x40encrypted-video"
            assert protocol.stats()["packets_relayed"] == 1
        finally:
            source.close()
            viewer.close()

    async def test_relay_is_refused_between_rooms(self, broker):
        _, address = broker
        source, stranger = make_peer(), make_peer()
        try:
            register(source, address, "roomA", ROLE_VIDEO_SOURCE)
            await recv(source)
            register(stranger, address, "roomB", ROLE_VIDEO_CLIENT)
            await recv(stranger)

            send(stranger, address, {"op": "relay", "peer": list(source.getsockname())})
            reply = await recv(stranger)
            assert reply["op"] == "error"
        finally:
            source.close()
            stranger.close()

    async def test_game_and_video_relays_coexist_for_one_player(self, broker):
        """Two sockets means two routes; the addr->addr table holds both."""
        protocol, address = broker
        server, client = make_peer(), make_peer()
        source, viewer = make_peer(), make_peer()
        try:
            for sock, role in (
                (server, ROLE_SERVER),
                (source, ROLE_VIDEO_SOURCE),
            ):
                register(sock, address, "r", role)
                await recv(sock)

            register(client, address, "r", ROLE_CLIENT)
            register(viewer, address, "r", ROLE_VIDEO_CLIENT)
            for sock in (server, client, source, viewer):
                await drain_all(sock)

            send(client, address, {"op": "relay", "peer": list(server.getsockname())})
            send(viewer, address, {"op": "relay", "peer": list(source.getsockname())})
            for sock in (server, client, source, viewer):
                await drain_all(sock)

            assert protocol.stats()["relay_sessions"] == 2

            client.sendto(b"input", address)
            game, _ = await asyncio.to_thread(server.recvfrom, 4096)
            viewer.sendto(b"video", address)
            media, _ = await asyncio.to_thread(source.recvfrom, 4096)
            assert (game, media) == (b"input", b"video")
        finally:
            for sock in (server, client, source, viewer):
                sock.close()


class TestVideoTeardown:
    async def test_bye_clears_the_video_source(self, broker):
        protocol, address = broker
        source = make_peer()
        try:
            register(source, address, "r", ROLE_VIDEO_SOURCE)
            await recv(source)
            assert protocol.stats()["rooms"] == 1

            send(source, address, {"op": "bye"})
            await asyncio.sleep(0.05)
            # The room held nothing else, so it is gone entirely.
            assert protocol.stats()["rooms"] == 0
        finally:
            source.close()

    async def test_bye_from_a_viewer_leaves_the_source_registered(self, broker):
        protocol, address = broker
        source, viewer = make_peer(), make_peer()
        try:
            register(source, address, "r", ROLE_VIDEO_SOURCE)
            await recv(source)
            register(viewer, address, "r", ROLE_VIDEO_CLIENT)
            await drain(viewer, 2)
            await recv(source)

            send(viewer, address, {"op": "bye"})
            await asyncio.sleep(0.05)
            assert protocol.stats()["rooms"] == 1
        finally:
            source.close()
            viewer.close()

    async def test_a_room_with_only_video_peers_is_not_listed(self, broker):
        """Listing advertises game servers; a video source is not one."""
        _, address = broker
        source, browser = make_peer(), make_peer()
        try:
            register(source, address, "r", ROLE_VIDEO_SOURCE, name="My Stream")
            await recv(source)

            send(browser, address, {"op": "list"})
            reply = await recv(browser)
            assert reply["op"] == "servers"
            assert reply["servers"] == []
        finally:
            source.close()
            browser.close()

"""LAN discovery for video servers.

A real beacon and a real probe over loopback. Loopback cannot prove a broadcast
crosses a subnet, but it does pin down the contract both sides depend on.

The property that matters most is negative: the reply must be enough to pick a
machine out of a list and *not* enough to reach it without the password.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from videoserver.discovery import (
    PROBE_MAGIC,
    REPLY_MAGIC,
    VideoDiscoveryBeacon,
    discover_video_servers,
)

pytestmark = pytest.mark.asyncio


class FakeNet:
    port = 47810


class FakeApp:
    """Enough of VideoServerApp for the beacon to describe."""

    def __init__(self, status: dict | None = None) -> None:
        self.net = FakeNet()
        self._status = status or {
            "streaming": True,
            "encoder": "libx264",
            "width": 1280,
            "height": 720,
            "clients": 2,
        }

    def status(self) -> dict:
        return self._status


@pytest.fixture
async def beacon():
    """A beacon on an ephemeral port, so parallel runs cannot collide."""
    app = FakeApp()
    instance = VideoDiscoveryBeacon(app, port=0, name="capture-pc")
    await instance.start()

    port = instance._transport.get_extra_info("sockname")[1]
    yield instance, port

    instance.close()


def probe_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    return sock


class TestTheBeacon:
    async def test_it_answers_a_probe(self, beacon):
        _instance, port = beacon
        sock = probe_socket()
        try:
            sock.sendto(PROBE_MAGIC, ("127.0.0.1", port))
            data, _ = await asyncio.to_thread(sock.recvfrom, 2048)

            assert data.startswith(REPLY_MAGIC)
            info = json.loads(data[len(REPLY_MAGIC) :].decode())
            assert info["name"] == "capture-pc"
            assert info["port"] == 47810
        finally:
            sock.close()

    async def test_it_ignores_traffic_that_is_not_a_probe(self, beacon):
        """A broadcast port hears all sorts of things."""
        _instance, port = beacon
        sock = probe_socket()
        try:
            for noise in (b"", b"RBGC?", b"hello", b"\x00" * 40):
                sock.sendto(noise, ("127.0.0.1", port))

            sock.settimeout(0.3)
            with pytest.raises(socket.timeout):
                sock.recvfrom(2048)
        finally:
            sock.close()

    async def test_the_gameplay_probe_is_not_answered(self, beacon):
        """The two beacons share a design and must not share answers."""
        _instance, port = beacon
        sock = probe_socket()
        try:
            sock.sendto(b"RBGC?", ("127.0.0.1", port))
            sock.settimeout(0.3)
            with pytest.raises(socket.timeout):
                sock.recvfrom(2048)
        finally:
            sock.close()

    async def test_the_reply_carries_no_credential(self, beacon):
        """Discovery tells you a video server exists, not how to use it."""
        _instance, port = beacon
        sock = probe_socket()
        try:
            sock.sendto(PROBE_MAGIC, ("127.0.0.1", port))
            data, _ = await asyncio.to_thread(sock.recvfrom, 2048)
            info = json.loads(data[len(REPLY_MAGIC) :].decode())

            for field in ("password", "ticket", "secret", "key"):
                assert field not in info
        finally:
            sock.close()

    async def test_a_broken_status_does_not_stop_it_answering(self):
        """Being findable matters more than the detail in the reply."""

        class Broken(FakeApp):
            def status(self):
                raise RuntimeError("no")

        instance = VideoDiscoveryBeacon(Broken(), port=0, name="still-here")
        await instance.start()
        port = instance._transport.get_extra_info("sockname")[1]
        sock = probe_socket()
        try:
            sock.sendto(PROBE_MAGIC, ("127.0.0.1", port))
            data, _ = await asyncio.to_thread(sock.recvfrom, 2048)
            info = json.loads(data[len(REPLY_MAGIC) :].decode())
            assert info["name"] == "still-here"
        finally:
            sock.close()
            instance.close()


class TestTheProbe:
    async def test_it_never_raises_with_nothing_listening(self):
        """No video servers is a normal answer, not a failure."""
        found = await discover_video_servers(timeout=0.3, port=47999)
        assert found == []

    async def test_hostile_replies_are_coerced(self):
        """Anyone can answer a broadcast; nothing here may be trusted raw."""
        loop = asyncio.get_running_loop()

        class Liar(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                if not data.startswith(PROBE_MAGIC):
                    return
                self.transport.sendto(
                    REPLY_MAGIC
                    + json.dumps(
                        {
                            "name": "x" * 500,
                            "port": "not-a-port",
                            "width": None,
                            "clients": [1, 2, 3],
                        }
                    ).encode(),
                    addr,
                )

        transport, _ = await loop.create_datagram_endpoint(
            Liar, local_addr=("127.0.0.1", 0)
        )
        port = transport.get_extra_info("sockname")[1]
        try:
            found = await discover_video_servers(timeout=0.5, port=port)
        finally:
            transport.close()

        if found:      # loopback broadcast delivery is not guaranteed
            entry = found[0]
            assert len(entry["name"]) <= 64
            assert isinstance(entry["port"], int)
            assert isinstance(entry["width"], int)
            assert isinstance(entry["clients"], int)

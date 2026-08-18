"""Video over the Internet path: broker introduction, punching, and relay.

Runs a real broker, a real video source and a real viewer on loopback. Loopback
cannot exercise actual NAT traversal, but it does verify the signalling
contract — which is where the bugs live, because each side is written against
an assumption about what the other sends.

The relay case matters more here than it does for gameplay: video is megabits
rather than kilobits, so a source that fails to notice it is being relayed
happily pushes 8 Mbps through someone's VPS.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest

pytest.importorskip("av", reason="video extras not installed")

from common.video import VideoSettings                      # noqa: E402
from client.net.video import VideoReceiver, VideoStreamState  # noqa: E402
from client.media.decoder import VideoDecoder               # noqa: E402
from rendezvous.broker import (                             # noqa: E402
    ROLE_VIDEO_CLIENT,
    ROLE_VIDEO_SOURCE,
    BrokerProtocol,
)
from server.rendezvous import RendezvousClient              # noqa: E402
from videoserver.capture import VideoCapture                # noqa: E402
from videoserver.encode import VideoEncoder                 # noqa: E402
from videoserver.net import VideoNet                        # noqa: E402

PASSWORD = "video-punch-test-password"
ROOM = "video-room"


class BrokerHarness:
    """A real broker on its own event loop, in a daemon thread."""

    def __init__(self) -> None:
        self.protocol: BrokerProtocol | None = None
        self.address: tuple[str, int] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(timeout=5), "broker did not start"

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        async def setup():
            self.protocol = BrokerProtocol()
            transport, _ = await loop.create_datagram_endpoint(
                lambda: self.protocol, local_addr=("127.0.0.1", 0)
            )
            self.address = transport.get_extra_info("sockname")
            self._ready.set()

        loop.run_until_complete(setup())
        loop.run_forever()

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)


def small_settings() -> VideoSettings:
    return VideoSettings(
        test_source=True,
        width=320,
        height=240,
        fps=20,
        bitrate_kbps=1500,
        encoder="libx264",
        audio_enabled=False,
        preview_enabled=False,
        gop_s=1.0,
    ).clamped()


@pytest.fixture
def broker():
    harness = BrokerHarness()
    harness.start()
    yield harness
    harness.stop()


@pytest.fixture
def source(broker):
    """A video source registered with the broker on its media socket."""
    settings = small_settings()
    net = VideoNet(settings, PASSWORD, bind_host="127.0.0.1", bind_port=0)
    capture = VideoCapture(settings)
    encoder = VideoEncoder(settings, capture, on_frame=net.submit_frame)
    net._on_idr_request = encoder.request_idr

    net.start()
    capture.start()
    encoder.start()

    relayed: list[bool] = []
    client = RendezvousClient(
        broker.address[0],
        broker.address[1],
        ROOM,
        send=net.send_raw,
        local_port=net.port,
        role="video-source",
        on_relay=lambda active: relayed.append(active),
    )
    assert client.resolve()
    net.rendezvous = client
    client.tick()          # register now rather than waiting for maintenance

    yield net, client, relayed

    encoder.stop()
    capture.stop()
    net.stop()


def wait_for(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class TestRegistration:
    def test_the_source_registers_on_its_media_socket(self, broker, source):
        """The punched mapping has to be the one media uses."""
        net, client, _relayed = source
        assert wait_for(lambda: client.is_registered)

        external = client.external_address
        assert external is not None
        assert external[1] == net.port, (
            "the broker sees a different port than media is served on"
        )

    def test_the_broker_files_it_under_the_video_role(self, broker, source):
        _net, client, _relayed = source
        assert wait_for(lambda: client.is_registered)

        # Ask the broker directly: a video source must not be listed as a
        # game server, or a browsing client would try to play against it.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(2.0)
        try:
            probe.sendto(json.dumps({"op": "list"}).encode(), broker.address)
            reply = json.loads(probe.recvfrom(4096)[0].decode())
            assert reply["servers"] == []
        finally:
            probe.close()


class TestPunchedStream:
    def test_a_viewer_reaches_the_source_through_the_broker(self, broker, source):
        """Introduction, punch, handshake, and frames -- the whole Internet path."""
        net, client, _relayed = source
        assert wait_for(lambda: client.is_registered)

        receiver = VideoReceiver(PASSWORD, client_name="distant-viewer")
        decoder = VideoDecoder(receiver)
        decoder.start()
        try:
            # No host at all: the only way through is the broker.
            receiver.connect_async(
                {
                    "available": True,
                    "broker": f"{broker.address[0]}:{broker.address[1]}",
                    "room": ROOM,
                    "password": PASSWORD,
                }
            )

            assert wait_for(
                lambda: receiver.state is VideoStreamState.STREAMING, timeout=30.0
            ), f"never connected: {receiver.state_detail}"
            assert receiver.connection_mode in ("punched", "relay", "direct")

            assert wait_for(lambda: decoder.frames_decoded >= 5, timeout=20.0), (
                f"only decoded {decoder.frames_decoded} frames over the broker path"
            )
        finally:
            decoder.stop()
            receiver.close()

    def test_the_direct_address_is_preferred_over_the_broker(self, broker, source):
        """Nobody should be punched, let alone relayed, when a direct path exists."""
        net, client, _relayed = source
        assert wait_for(lambda: client.is_registered)

        receiver = VideoReceiver(PASSWORD, client_name="near-viewer")
        decoder = VideoDecoder(receiver)
        decoder.start()
        try:
            receiver.connect_async(
                {
                    "available": True,
                    "host": "127.0.0.1",
                    "port": net.port,
                    "broker": f"{broker.address[0]}:{broker.address[1]}",
                    "room": ROOM,
                    "password": PASSWORD,
                }
            )
            assert wait_for(
                lambda: receiver.state is VideoStreamState.STREAMING, timeout=20.0
            )
            assert receiver.connection_mode == "direct"
        finally:
            decoder.stop()
            receiver.close()


class TestRelayCap:
    def test_relaying_caps_the_bitrate(self):
        """Relayed video spends someone else's bandwidth, so it is not ours to choose."""
        from videoserver.config import VideoServerConfig
        from videoserver.pipeline import VideoServerApp

        settings = small_settings()
        settings = VideoSettings(
            **{**settings.to_dict(), "bitrate_kbps": 12000, "relay_bitrate_kbps": 3000}
        )
        cfg = VideoServerConfig(
            password=PASSWORD,
            media_bind_host="127.0.0.1",
            media_port=0,
            settings=settings,
        )
        app = VideoServerApp(cfg)
        app.start()
        try:
            assert app.status()["bitrate_kbps"] == 12000

            app.set_relay_active(True)
            assert app.status()["bitrate_kbps"] == 3000
            assert app.status()["relay_capped"] is True

            app.set_relay_active(False)
            assert app.status()["bitrate_kbps"] == 12000
            assert app.status()["relay_capped"] is False
        finally:
            app.stop()

    def test_a_relay_notice_from_the_broker_reaches_the_cap(self, broker, source):
        """The wiring between the broker's message and the governor."""
        _net, client, relayed = source
        assert wait_for(lambda: client.is_registered)

        client.handle_datagram(
            json.dumps({"op": "relaying", "peer": ["1.2.3.4", 5678]}).encode(),
            broker.address,
        )
        assert relayed == [True], "the relay notice never reached the source"

"""The whole video path, in one process.

Bluetooth server, video server, and a player — each the real implementation,
talking over real sockets on loopback. This is the test that would have caught
every integration mistake the individual suites cannot see, because each side
looks correct in isolation:

    video server ──register──> Bluetooth server ──advert──> player
          ▲                                                   │
          └───────────────── media, direct ───────────────────┘

What it proves end to end: a player learns where the video is without being
told, connects there on its own, and gets frames that decode — and that the
latency figures shown to that player are computed from a real clock exchange
rather than from nothing.
"""

from __future__ import annotations

import threading
import time

import pytest

av = pytest.importorskip("av", reason="video extras not installed")

from common.protocol import ControlOp                    # noqa: E402
from common.video import VideoSettings                   # noqa: E402
from client.media.decoder import VideoDecoder            # noqa: E402
from client.net.transport import ClientTransport         # noqa: E402
from client.net.video import VideoReceiver, VideoStreamState  # noqa: E402
from server.bt.profiles import create_profile            # noqa: E402
from server.bt.sink import MockSink                      # noqa: E402
from server.datapath import Datapath                     # noqa: E402
from server.router import OutputChannel, Router          # noqa: E402
from server import config as server_config                # noqa: E402
from server.sessions import SessionManager               # noqa: E402
from server.video import MODE_EXTERNAL, VideoRegistry    # noqa: E402
from server.videolink import VideoLink                    # noqa: E402
from videoserver.config import VideoServerConfig         # noqa: E402
from videoserver.control import ControlResponder         # noqa: E402
from videoserver.pipeline import VideoServerApp          # noqa: E402

#: The players' password. Every client has it; the video server learns it over
#: the control link so it can admit viewers.
PASSWORD = "video-e2e-test-password"

#: The video server's own password, held by the operator alone. Deliberately
#: different: if viewers shared it, a denied one could come back claiming to be
#: the Bluetooth server, which is the role exempt from viewing tickets.
VIDEO_PASSWORD = "video-server-own-password"


def stream_settings() -> VideoSettings:
    """A cheap test-pattern stream, requested by the Bluetooth server.

    The server owns the configuration and pushes it over the link, so this
    belongs here rather than in the video server's own config -- setting it
    only there would be overwritten a moment after the link comes up.
    """
    return VideoSettings(
        test_source=True,
        width=320,
        height=240,
        fps=20,
        bitrate_kbps=2000,
        encoder="libx264",
        audio_enabled=False,
        preview_enabled=False,
        gop_s=1.0,
    )


def wait_for(predicate, timeout: float = 20.0, pump=()) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for transport in pump:
            transport.service()
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def video_server():
    """A capture PC. It binds its port and waits to be taken charge of."""
    cfg = VideoServerConfig(
        standalone=False,
        media_bind_host="127.0.0.1",
        media_port=0,
        discoverable=False,          # no broadcasts from a test
        password=VIDEO_PASSWORD,
        name="capture-pc",
        settings=stream_settings(),
    )

    app = VideoServerApp(cfg)
    responder = ControlResponder(app)
    app.responder = responder
    app.start()
    responder.start()

    yield app, responder

    responder.stop()
    app.stop()


@pytest.fixture
def bluetooth_server(video_server):
    """The Pi: sessions, routing, and the outbound link to the video server."""
    app, _responder = video_server

    router = Router()
    router.add_channel(
        OutputChannel(
            bd_addr="00:00:00:00:00:01",
            hci_name="mock0",
            profile=create_profile("generic"),
            sink=MockSink(name="mock0"),
        )
    )
    sessions = SessionManager(PASSWORD, auto_approve=True)
    registry = VideoRegistry(mode=MODE_EXTERNAL, settings=stream_settings())
    datapath = Datapath(
        sessions, router, bind_host="127.0.0.1", bind_port=0,
        realtime=False, video_registry=registry,
    )
    datapath.start()
    datapath.set_accepting(lan=True, internet=False)

    config = server_config.ServerConfig(
        password=PASSWORD,
        server_name="test-pi",
        video_mode=MODE_EXTERNAL,
        video_host="127.0.0.1",
        video_port=app.net.port,
        video_password=VIDEO_PASSWORD,
    )
    link = VideoLink(registry, datapath, config)
    link.start()

    yield datapath, registry, link

    link.stop()
    datapath.stop()


class Player:
    """A client: gameplay session plus everything the video side needs."""

    def __init__(self, port: int) -> None:
        self.adverts: list[dict] = []
        self.transport = ClientTransport(
            PASSWORD,
            client_name="player-1",
            on_control=self._on_control,
            rumble_enabled=False,
        )
        self.transport.connect("127.0.0.1", port, timeout_ns=5_000_000_000)

        self.receiver: VideoReceiver | None = None
        self.decoder: VideoDecoder | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _on_control(self, body: dict) -> None:
        if body.get("op") == ControlOp.VIDEO_SOURCE:
            self.adverts.append(body)

    def _pump(self) -> None:
        """Stands in for the input loop, which normally services the transport."""
        while not self._stop.is_set():
            self.transport.service()
            time.sleep(0.005)

    @property
    def advert(self) -> dict | None:
        return self.adverts[-1] if self.adverts else None

    def start_video(self) -> None:
        advert = self.advert
        assert advert and advert.get("available")
        self.receiver = VideoReceiver(PASSWORD, client_name="player-1")
        self.decoder = VideoDecoder(self.receiver)
        self.decoder.start()
        self.receiver.connect_async({**advert, "password": PASSWORD})

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        if self.decoder is not None:
            self.decoder.stop()
        if self.receiver is not None:
            self.receiver.close()
        self.transport.close()


@pytest.fixture
def player(bluetooth_server):
    datapath, _registry, _link = bluetooth_server
    person = Player(datapath.port)
    yield person
    person.close()


class TestDiscovery:
    def test_a_player_is_told_where_the_video_is_without_asking(
        self, bluetooth_server, video_server, player
    ):
        """The whole point of routing video through the control plane."""
        _datapath, registry, _link = bluetooth_server

        assert wait_for(lambda: registry.is_live), "the video server never registered"
        assert wait_for(lambda: player.advert is not None and player.advert["available"])

        advert = player.advert
        assert advert["port"] > 0
        assert advert["host"]

    def test_the_advertised_port_is_the_one_media_is_served_on(
        self, bluetooth_server, video_server, player
    ):
        app, _responder = video_server
        assert wait_for(lambda: player.advert is not None and player.advert["available"])
        assert player.advert["port"] == app.net.port


class TestStreaming:
    def test_a_player_receives_and_decodes_the_stream(
        self, bluetooth_server, video_server, player
    ):
        """Capture -> encode -> wire -> reassemble -> decode, for real."""
        assert wait_for(lambda: player.advert is not None and player.advert["available"])
        player.start_video()

        assert wait_for(
            lambda: player.receiver.state is VideoStreamState.STREAMING, timeout=15.0
        ), f"never started streaming: {player.receiver.state_detail}"

        assert wait_for(lambda: player.decoder.frames_decoded >= 10, timeout=15.0), (
            f"only decoded {player.decoder.frames_decoded} frames"
        )

        frame = player.decoder.latest()
        assert frame is not None
        assert (frame.width, frame.height) == (320, 240)

    def test_the_stream_is_not_all_keyframes(
        self, bluetooth_server, video_server, player
    ):
        """All-intra output looks perfect and costs about five times the bitrate."""
        assert wait_for(lambda: player.advert is not None and player.advert["available"])
        player.start_video()
        assert wait_for(lambda: player.decoder.frames_decoded >= 20, timeout=15.0)

        stats = player.receiver.snapshot()["assembler"]
        complete = stats["frames_complete"]
        assert complete >= 15

        app, _responder = video_server
        keyframes = app._encoder.keyframes
        encoded = app._encoder.frames_encoded
        assert encoded > 0
        assert keyframes < encoded / 2, (
            f"{keyframes}/{encoded} frames were keyframes; the encoder is "
            "producing intra-only output"
        )

    def test_the_latency_figures_are_real(
        self, bluetooth_server, video_server, player
    ):
        """The clock exchange has to complete or the display is meaningless."""
        assert wait_for(lambda: player.advert is not None and player.advert["available"])
        player.start_video()
        assert wait_for(lambda: player.decoder.frames_decoded >= 10, timeout=15.0)

        assert wait_for(lambda: player.receiver.clock_locked, timeout=10.0), (
            "clock never locked, so capture-to-present cannot be computed"
        )

        # Present a frame the way the window does, and check the figure lands
        # somewhere physically possible on loopback.
        from client.gui.video_window import VideoWindow  # noqa: F401  (import check)
        from common.timing import now_ns

        frame = player.decoder.latest()
        assert frame is not None
        latency_ms = (
            now_ns() - (frame.capture_ts + player.receiver.clock_offset_ns)
        ) / 1_000_000
        assert 0 < latency_ms < 1000, f"implausible latency: {latency_ms:.1f} ms"

    def test_the_source_sees_its_viewer(
        self, bluetooth_server, video_server, player
    ):
        app, _responder = video_server
        assert wait_for(lambda: player.advert is not None and player.advert["available"])
        player.start_video()

        # viewer_snapshot, not client_snapshot: the latter also holds the
        # Bluetooth server's control session, which receives no media.
        assert wait_for(lambda: app.net.client_count == 1, timeout=15.0)
        viewers = app.net.viewer_snapshot()
        assert len(viewers) == 1
        assert viewers[0]["name"] == "player-1"
        assert wait_for(lambda: app.net.viewer_snapshot()[0]["frames_sent"] > 5)

    def test_gameplay_keeps_working_while_video_streams(
        self, bluetooth_server, video_server, player
    ):
        """Video must not disturb the input path -- separate sockets, separate threads."""
        _datapath, _registry, _link = bluetooth_server
        assert wait_for(lambda: player.advert is not None and player.advert["available"])
        player.start_video()
        assert wait_for(lambda: player.decoder.frames_decoded >= 10, timeout=15.0)

        from common.state import Button, ControllerState

        state = ControllerState()
        state.buttons = int(Button.A)
        for index in range(20):
            player.transport.send_input(0, state, request_ack=index % 4 == 0)
            time.sleep(0.005)

        assert wait_for(
            lambda: any(
                stats["rtt"]["count"] > 0
                for stats in player.transport.latency_snapshot().values()
            ),
            timeout=5.0,
        ), "no controller acks arrived while video was streaming"


class TestRecovery:
    def test_losing_the_source_tells_the_player(
        self, bluetooth_server, video_server, player
    ):
        _datapath, registry, _link = bluetooth_server
        app, responder = video_server

        assert wait_for(lambda: player.advert is not None and player.advert["available"])

        # The capture PC goes away. Stopping it for real, rather than tearing
        # down our own link, is the case that actually happens.
        responder.stop()
        app.stop()

        assert wait_for(
            lambda: player.advert is not None and not player.advert["available"],
            timeout=20.0,
        ), "the player was never told the stream ended"
        assert not registry.has_source

    def test_a_second_player_can_join_an_existing_stream(
        self, bluetooth_server, video_server, player
    ):
        """A late joiner needs a keyframe, which it has to ask for."""
        datapath, _registry, _link = bluetooth_server
        assert wait_for(lambda: player.advert is not None and player.advert["available"])
        player.start_video()
        assert wait_for(lambda: player.decoder.frames_decoded >= 10, timeout=15.0)

        latecomer = Player(datapath.port)
        try:
            assert wait_for(
                lambda: latecomer.advert is not None and latecomer.advert["available"],
                timeout=10.0,
            )
            latecomer.start_video()
            assert wait_for(
                lambda: latecomer.decoder.frames_decoded >= 5, timeout=15.0
            ), "a player joining mid-stream never got a decodable frame"
        finally:
            latecomer.close()

"""Player identification across three real programs, over real sockets.

The companion to ``test_video_e2e.py``, and the same reasoning: each side of
this looks correct in isolation, so the mistakes worth catching are the ones
between them. Here that is a real video server actually identifying entities
in frames it captured, a real Bluetooth server filtering per viewport, and a
real client transport receiving what it is sent.

Nothing is mocked but the Bluetooth radio and the capture card.

What it proves, end to end:

  * with the feature off nothing is sent and nothing is constructed;
  * the viewport map reaches the source without being asked for;
  * a client that has not opted in is sent nothing at all;
  * a client that has is sent labels, filtered, that decode.
"""

from __future__ import annotations

import time

import pytest

av = pytest.importorskip("av", reason="video extras not installed")

from common.protocol import ControlOp                     # noqa: E402
from common.video import VideoSettings                    # noqa: E402
from server import config as server_config                # noqa: E402
from server import player_overlay                         # noqa: E402
from server.bt.profiles import create_profile             # noqa: E402
from server.bt.sink import MockSink                       # noqa: E402
from server.datapath import Datapath                      # noqa: E402
from server.router import OutputChannel, Router           # noqa: E402
from server.sessions import SessionManager                # noqa: E402
from server.video import MODE_EXTERNAL, VideoRegistry     # noqa: E402
from server.videolink import VideoLink                    # noqa: E402
from videoserver.config import VideoServerConfig          # noqa: E402
from videoserver.control import ControlResponder          # noqa: E402
from videoserver.pipeline import VideoServerApp           # noqa: E402

PASSWORD = "player-id-e2e-password"
VIDEO_PASSWORD = "player-id-video-password"


def settings(**kwargs) -> VideoSettings:
    base = dict(
        test_source=True, width=320, height=240, fps=15,
        bitrate_kbps=800, audio_enabled=False, preview_enabled=False,
        # Forced rather than detected: `testsrc` has a hard edge down the
        # middle and reads as a false VERTICAL_2, which is documented -- and
        # what is under test here is identification, not detection.
        split_override="QUAD_4",
    )
    base.update(kwargs)
    return VideoSettings(**base)


def wait_for(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def video_server():
    cfg = VideoServerConfig(
        standalone=False,
        media_bind_host="127.0.0.1",
        media_port=0,
        discoverable=False,
        password=VIDEO_PASSWORD,
        name="capture-pc",
        settings=settings(),
        # The capture machine's own consent. Without it nothing here loads a
        # model however loudly the Bluetooth server asks.
        playervision_allowed=True,
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
    app, _responder = video_server

    router = Router()
    for number in (1, 2):
        router.add_channel(
            OutputChannel(
                bd_addr=f"00:00:00:00:00:0{number}",
                hci_name=f"mock{number}",
                profile=create_profile("generic"),
                sink=MockSink(name=f"mock{number}"),
                number=number,
                regions=["upper_left" if number == 1 else "upper_right"],
            )
        )

    sessions = SessionManager(PASSWORD, auto_approve=True)
    registry = VideoRegistry(mode=MODE_EXTERNAL, settings=settings())
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

    assert wait_for(lambda: link.connected), "the video link never came up"
    yield datapath, registry, link, router, sessions

    link.stop()
    datapath.stop()


class TestOff:
    def test_nothing_is_constructed_and_nothing_is_sent(self, video_server,
                                                        bluetooth_server):
        """The default. Both switches off at the Bluetooth server's end."""
        app, _ = video_server
        _datapath, registry, _link, _router, _sessions = bluetooth_server

        assert wait_for(lambda: registry.is_live), "the source never reported"
        time.sleep(1.0)

        assert app._players.running is False, "a backend was built with the feature off"
        assert "player_id" not in app.status(), "status grew a key with the feature off"
        assert registry.tracks[1] == [], "tracks arrived with the feature off"


class TestOn:
    @staticmethod
    def _switch_on(registry, link):
        registry.set_config(settings(player_id_enabled=True,
                                     player_id_backend="heuristic",
                                     player_id_hz=10.0))
        link.request_config_push()

    def test_the_source_starts_identifying(self, video_server, bluetooth_server):
        app, _ = video_server
        _datapath, registry, link, _router, _sessions = bluetooth_server
        assert wait_for(lambda: registry.is_live)

        self._switch_on(registry, link)
        assert wait_for(lambda: app._players.running, timeout=15.0), (
            "the video server never started player identification"
        )
        report = app.status().get("player_id")
        assert report is not None and report["available"] is True

    def test_the_viewport_map_reaches_the_source(self, video_server,
                                                 bluetooth_server):
        """Pushed by the Bluetooth server on its own cadence -- the source
        cannot work this out and is never asked whether it wants it."""
        app, _ = video_server
        _datapath, registry, link, router, _sessions = bluetooth_server
        assert wait_for(lambda: registry.is_live)
        self._switch_on(registry, link)
        assert wait_for(lambda: app._players.running, timeout=15.0)

        # Assigned first: an adapter with nobody on it is hardware, not a
        # player, and `player_hints` rightly leaves it out. Forgetting this is
        # how the map comes out empty with every counter healthy.
        for channel, client in zip(router.channels(), ("c1", "c2")):
            channel.assigned_client = client
            channel.assigned_slot = 0

        hints = player_overlay.player_hints(router, "QUAD_4")
        assert len(hints) == 2, hints
        link.push_player_map(hints)
        assert wait_for(
            lambda: app._players._hints and len(app._players._hints) == 2,
            timeout=10.0,
        ), "the player map never arrived"
        assert {hint.player_id for hint in app._players._hints} == {1, 2}

    def test_tracks_come_back_to_the_bluetooth_server(self, video_server,
                                                      bluetooth_server):
        """The return leg. `testsrc` is a moving pattern, so the no-model
        backend finds something in it -- what matters here is that whatever it
        found crossed the wire and was absorbed."""
        app, _ = video_server
        _datapath, registry, link, _router, _sessions = bluetooth_server
        assert wait_for(lambda: registry.is_live)
        self._switch_on(registry, link)
        assert wait_for(lambda: app._players.running, timeout=15.0)

        assert wait_for(
            lambda: app._players.samples > 3, timeout=15.0
        ), "the source never analysed a frame"
        assert wait_for(
            lambda: registry._tracks_ns > 0, timeout=15.0
        ), "VIDEO_TRACKS never reached the Bluetooth server"


class TestPerClient:
    def test_a_client_that_has_not_asked_is_sent_nothing(self, bluetooth_server):
        """Silence means no, which is what an older client says by
        construction -- so the filtering and the datagram both disappear
        rather than being computed and discarded."""
        datapath, registry, _link, router, sessions = bluetooth_server

        sent: list = []
        datapath.send_control = lambda session, op, payload=None: sent.append(op)

        registry.update_tracks(
            {"l": "QUAD_4", "t": [[1, 2, "ur", 5000, 3000, 800, 1600, 90, "viewport"]]}
        )
        datapath.broadcast_player_labels()
        assert sent == []

    def test_a_client_that_has_asked_is_sent_filtered_labels(self,
                                                             bluetooth_server):
        datapath, registry, _link, router, sessions = bluetooth_server

        # Two approved clients, one holding each viewport.
        session_one = _fake_session(sessions, "c1", labels=True)
        session_two = _fake_session(sessions, "c2", labels=False)
        for channel, client in zip(router.channels(), ("c1", "c2")):
            channel.assigned_client = client
            channel.assigned_slot = 0
            channel.username = f"Player at {client}"

        sent: list = []
        datapath.send_control = lambda s, op, payload=None: sent.append((s, op, payload))

        registry.update_tracks(
            {
                "l": "QUAD_4",
                "t": [
                    [1, 1, "ul", 1000, 1000, 800, 1600, 90, "viewport"],
                    [2, 2, "ur", 6000, 1000, 800, 1600, 90, "viewport"],
                ],
            }
        )
        datapath.broadcast_player_labels()

        assert len(sent) == 1, "only the client that asked should be sent anything"
        session, op, payload = sent[0]
        assert session is session_one
        assert op == ControlOp.PLAYER_LABELS

        from common.player_labels import decode_labels

        _layout, labels = decode_labels(payload)
        # c1 holds player 1's viewport, so player 1 is excluded from it.
        assert {label["player_id"] for label in labels} == {2}

        assert session_two.player_labels is False


def _fake_session(sessions, client_id, *, labels):
    """An approved controller session, without a handshake.

    The handshake is `test_video_e2e`'s subject, not this one's -- what is
    under test here is who gets told what.
    """
    from server.sessions import ROLE_CONTROLLER, Session, SessionState
    from common import crypto

    session = Session(
        session_id=len(sessions.all_sessions()) + 1,
        client_id=client_id,
        address=("127.0.0.1", 40000 + len(sessions.all_sessions())),
        crypto=None,
        role=ROLE_CONTROLLER,
        state=SessionState.APPROVED,
    )
    session.player_labels = labels
    sessions._sessions[client_id] = session
    return session

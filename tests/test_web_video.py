"""Web GUI: the video panel's endpoints.

The video routes carry two things the rest of the API does not: a binary
response (the preview) and settings that reach a *different machine*. The
failures worth pinning down:

  * A preview endpoint reachable without logging in would let anyone on the
    network watch the console through the admin UI.
  * Settings that are accepted and silently clamped, with the operator left
    thinking the control is broken.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from common.video import MediaCodec, SliceFlags, VideoSettings, encode_video_slice_into
from server import config as server_config
from server.bt.profiles import create_profile
from server.bt.sink import MockSink
from server.datapath import Datapath
from server.router import OutputChannel, Router
from server.sessions import SessionManager
from server.video import MODE_EMBEDDED, MODE_EXTERNAL, MODE_OFF, VideoRegistry
from server.web.app import create_app

ADMIN_PASSWORD = "web-video-admin-password"


def build_config() -> server_config.ServerConfig:
    return server_config.ServerConfig(
        password="client-password",
        admin_password=ADMIN_PASSWORD,
        tls_enabled=False,
        video_mode=MODE_EXTERNAL,
    )


@pytest.fixture
async def client():
    cfg = build_config()
    router = Router()
    router.add_channel(
        OutputChannel(
            bd_addr="00:00:00:00:00:01",
            hci_name="mock0",
            profile=create_profile("generic"),
            sink=MockSink(name="mock0"),
        )
    )
    sessions = SessionManager(cfg.password, auto_approve=True)
    registry = VideoRegistry(mode=MODE_EXTERNAL)
    datapath = Datapath(
        sessions, router, bind_host="127.0.0.1", bind_port=0,
        realtime=False, video_registry=registry,
    )

    app = create_app(cfg, sessions, router, datapath, video_registry=registry)
    server = TestServer(app)
    test_client = TestClient(server)
    await test_client.start_server()

    yield test_client, registry, cfg

    await test_client.close()


async def login(test_client: TestClient) -> None:
    response = await test_client.post("/api/login", json={"password": ADMIN_PASSWORD})
    assert response.status == 200


def preview_slice(payload: bytes) -> tuple:
    """One complete MJPEG frame, as the source would send it."""
    from common import video as video_wire

    buf = bytearray(4096)
    size = encode_video_slice_into(
        buf, 0, 1, 0, 1, SliceFlags.KEYFRAME, MediaCodec.MJPEG, 0, payload
    )
    return video_wire.decode_video_slice(bytes(buf[:size]), 0)


class TestAuth:
    async def test_every_video_endpoint_needs_a_login(self, client):
        test_client, _registry, _cfg = client
        for method, path in (
            ("get", "/api/video/preview"),
            ("post", "/api/video/mode"),
            ("post", "/api/video/config"),
            ("post", "/api/video/probe"),
        ):
            response = await getattr(test_client, method)(path, json={})
            assert response.status == 401, f"{path} was reachable without logging in"

    async def test_the_preview_is_not_public(self, client):
        """It is a live picture of the operator's screen."""
        test_client, registry, _cfg = client
        registry.feed_preview_slice(preview_slice(b"\xff\xd8jpeg\xff\xd9"))

        response = await test_client.get("/api/video/preview")
        assert response.status == 401


class TestMode:
    async def test_mode_can_be_switched(self, client):
        test_client, registry, cfg = client
        await login(test_client)

        response = await test_client.post("/api/video/mode", json={"mode": MODE_OFF})
        assert response.status == 200
        assert registry.mode == MODE_OFF
        assert cfg.video_mode == MODE_OFF

    async def test_an_unknown_mode_is_refused(self, client):
        test_client, registry, _cfg = client
        await login(test_client)

        response = await test_client.post("/api/video/mode", json={"mode": "broadcast"})
        assert response.status == 400
        assert registry.mode == MODE_EXTERNAL

    async def test_embedded_mode_arms_the_loopback_allowance(self, client):
        """The allowance exists only for a child we started ourselves."""
        test_client, _registry, _cfg = client
        await login(test_client)
        state = test_client.app["state"]

        await test_client.post("/api/video/mode", json={"mode": MODE_EMBEDDED})
        assert state.datapath.allow_loopback_video is True

        await test_client.post("/api/video/mode", json={"mode": MODE_OFF})
        assert state.datapath.allow_loopback_video is False

    async def test_video_needs_a_password_first(self, client):
        test_client, registry, cfg = client
        await login(test_client)
        cfg.password = ""
        registry.mode = MODE_OFF

        response = await test_client.post("/api/video/mode", json={"mode": MODE_EXTERNAL})
        assert response.status == 400
        assert registry.mode == MODE_OFF


class TestConfig:
    async def test_settings_round_trip_and_persist_into_the_config(self, client):
        test_client, registry, cfg = client
        await login(test_client)

        response = await test_client.post(
            "/api/video/config",
            json={"width": 1920, "height": 1080, "fps": 60, "bitrate_kbps": 12000},
        )
        assert response.status == 200
        assert registry.settings.width == 1920
        assert registry.settings.fps == 60
        assert cfg.video_config["bitrate_kbps"] == 12000

    async def test_a_partial_update_leaves_other_settings_alone(self, client):
        test_client, registry, _cfg = client
        await login(test_client)
        registry.set_config(VideoSettings(width=1280, height=720, bitrate_kbps=9000))

        await test_client.post("/api/video/config", json={"bitrate_kbps": 4000})
        assert registry.settings.bitrate_kbps == 4000
        assert registry.settings.width == 1280, "an unrelated setting was reset"

    async def test_the_sequence_advances_so_the_source_notices(self, client):
        test_client, registry, _cfg = client
        await login(test_client)
        before = registry.cfg_seq

        await test_client.post("/api/video/config", json={"fps": 30})
        assert registry.cfg_seq > before
        assert registry.needs_config_push() is False, "no source attached to push to"

    async def test_clamping_is_reported_rather_than_silent(self, client):
        """An operator who asks for 1080p60 and gets 720p30 deserves to know."""
        test_client, registry, _cfg = client
        await login(test_client)
        registry.mode = MODE_EMBEDDED

        response = await test_client.post(
            "/api/video/config",
            json={"width": 1920, "height": 1080, "fps": 60, "bitrate_kbps": 20000},
        )
        assert response.status == 200
        body = await response.json()
        assert "limited" in body["message"]
        assert registry.settings.width <= 1280

    async def test_probe_needs_a_source(self, client):
        test_client, _registry, _cfg = client
        await login(test_client)

        response = await test_client.post("/api/video/probe", json={})
        assert response.status == 400


class TestPreview:
    async def test_no_preview_yet_is_a_204(self, client):
        """Not a placeholder image: the browser keeps the last good frame."""
        test_client, _registry, _cfg = client
        await login(test_client)

        response = await test_client.get("/api/video/preview")
        assert response.status == 204

    async def test_a_fed_preview_comes_back_as_a_jpeg(self, client):
        test_client, registry, _cfg = client
        await login(test_client)

        payload = b"\xff\xd8" + b"pretend-jpeg" * 20 + b"\xff\xd9"
        registry.feed_preview_slice(preview_slice(payload))

        response = await test_client.get("/api/video/preview")
        assert response.status == 200
        assert response.content_type == "image/jpeg"
        assert await response.read() == payload
        assert response.headers["Cache-Control"] == "no-store"

    async def test_a_stale_preview_is_withheld(self, client):
        """Showing a minute-old frame as if it were live is worse than nothing."""
        test_client, registry, _cfg = client
        await login(test_client)

        registry.feed_preview_slice(preview_slice(b"\xff\xd8old\xff\xd9"))
        assert registry.preview() is not None

        from common.timing import now_ns
        from server.video import PREVIEW_STALE_NS

        registry._preview_ns = now_ns() - PREVIEW_STALE_NS - 1
        response = await test_client.get("/api/video/preview")
        assert response.status == 204


class TestStatusPayload:
    async def test_status_carries_the_video_block(self, client):
        test_client, _registry, _cfg = client
        await login(test_client)

        response = await test_client.get("/api/status")
        body = await response.json()
        assert "video" in body
        assert body["video"]["mode"] == MODE_EXTERNAL
        assert "settings" in body["video"]

    async def test_the_status_never_carries_a_password(self, client):
        test_client, _registry, _cfg = client
        await login(test_client)

        response = await test_client.get("/api/status")
        text = await response.text()
        assert ADMIN_PASSWORD not in text
        assert "client-password" not in text

    async def test_csp_allows_the_preview_blob(self, client):
        test_client, _registry, _cfg = client
        response = await test_client.get("/")
        csp = response.headers["Content-Security-Policy"]
        assert "blob:" in csp
        # The rest of the policy must stay tight.
        assert "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp


class TestWithoutVideo:
    async def test_a_server_built_without_video_says_so(self):
        """Not a crash: the registry is optional, like adapter_manager."""
        cfg = build_config()
        router = Router()
        sessions = SessionManager(cfg.password, auto_approve=True)
        datapath = Datapath(
            sessions, router, bind_host="127.0.0.1", bind_port=0, realtime=False
        )
        app = create_app(cfg, sessions, router, datapath)
        server = TestServer(app)
        test_client = TestClient(server)
        await test_client.start_server()
        try:
            await login(test_client)

            status = await (await test_client.get("/api/status")).json()
            assert status["video"] is None

            response = await test_client.post("/api/video/mode", json={"mode": "off"})
            assert response.status == 404

            preview = await test_client.get("/api/video/preview")
            assert preview.status == 204
        finally:
            await test_client.close()


class TestRuntimeVersusPersistedSettings:
    """Two settings sit next to each other in the same handler and must behave
    differently, so the distinction is easy to lose:

      * rumble is a preference and has to survive a restart;
      * auto-approve is a security posture and deliberately must not.

    The bug worth guarding is subtler than either: auto-approve used to be
    mirrored into the config without ever being saved, so it reached disk
    whenever some *unrelated* change happened to persist. It survived a restart
    or not depending on what the operator touched next.
    """

    async def test_rumble_is_persisted(self, client):
        test_client, _registry, cfg = client
        await login(test_client)

        await test_client.post("/api/settings", json={"rumble_enabled": False})
        assert cfg.rumble_enabled is False

    async def test_auto_approve_applies_immediately(self, client):
        test_client, _registry, _cfg = client
        await login(test_client)
        state = test_client.app["state"]

        await test_client.post("/api/settings", json={"auto_approve": False})
        assert state.sessions.auto_approve is False

        await test_client.post("/api/settings", json={"auto_approve": True})
        assert state.sessions.auto_approve is True

    async def test_auto_approve_never_reaches_the_saved_config(self, client):
        """Not even indirectly, via a later save of something else."""
        test_client, _registry, cfg = client
        await login(test_client)
        cfg.auto_approve = False

        await test_client.post("/api/settings", json={"auto_approve": True})
        assert cfg.auto_approve is False, "auto-approve was mirrored into the config"

        # A later, unrelated change that *does* persist must not carry it along.
        await test_client.post("/api/video/config", json={"fps": 30})
        assert cfg.auto_approve is False, (
            "auto-approve leaked to the config through an unrelated save"
        )

    async def test_the_status_reports_the_live_value_not_the_config(self, client):
        """The GUI must show what is in force, not what will apply next boot."""
        test_client, _registry, cfg = client
        await login(test_client)
        cfg.auto_approve = False

        await test_client.post("/api/settings", json={"auto_approve": True})

        status = await (await test_client.get("/api/status")).json()
        assert status["server"]["auto_approve"] is True


class TestVideoConnection:
    """Pointing the server at a video server: address, password, discovery.

    The password is the operator's alone -- it must reach the config and never
    the status feed, which every open browser receives ten times a second.
    """

    async def test_the_address_is_saved(self, client):
        test_client, _registry, cfg = client
        await login(test_client)

        response = await test_client.post(
            "/api/video/connection", json={"host": "192.168.1.20", "port": 47810}
        )
        assert response.status == 200
        assert cfg.video_host == "192.168.1.20"
        assert cfg.video_port == 47810

    async def test_a_host_and_port_typed_together_are_split(self):
        """People will type it that way whatever the form says."""
        cfg = build_config()
        router, sessions, registry, datapath = _stack(cfg)
        app = create_app(cfg, sessions, router, datapath, video_registry=registry)
        test_client = TestClient(TestServer(app))
        await test_client.start_server()
        try:
            await login(test_client)
            await test_client.post(
                "/api/video/connection", json={"host": "10.0.0.4:49000"}
            )
            assert cfg.video_host == "10.0.0.4"
            assert cfg.video_port == 49000
        finally:
            await test_client.close()

    async def test_the_password_is_saved_but_never_broadcast(self, client):
        test_client, _registry, cfg = client
        await login(test_client)

        await test_client.post(
            "/api/video/connection",
            json={"host": "192.168.1.20", "password": "video-server-secret"},
        )
        assert cfg.video_password == "video-server-secret"

        status = await test_client.get("/api/status")
        text = await status.text()
        assert "video-server-secret" not in text, "the video password reached a browser"

        body = await (await test_client.get("/api/status")).json()
        assert body["video"]["connection"]["has_password"] is True

    async def test_a_short_password_is_refused(self, client):
        test_client, _registry, cfg = client
        await login(test_client)

        response = await test_client.post(
            "/api/video/connection", json={"password": "abc"}
        )
        assert response.status == 400
        assert cfg.video_password == ""

    async def test_the_connection_endpoint_needs_a_login(self, client):
        test_client, _registry, _cfg = client
        for path in ("/api/video/connection", "/api/video/detect"):
            response = await test_client.post(path, json={})
            assert response.status == 401, f"{path} was reachable without logging in"

    async def test_detect_returns_a_list(self, client):
        """Finding nothing is a normal answer, not an error."""
        test_client, _registry, _cfg = client
        await login(test_client)

        response = await test_client.post("/api/video/detect", json={})
        assert response.status == 200
        body = await response.json()
        assert isinstance(body["servers"], list)
        assert body["message"]

    async def test_the_status_carries_the_address_for_the_form(self, client):
        test_client, _registry, cfg = client
        await login(test_client)
        cfg.video_host = "10.1.1.1"
        cfg.video_port = 47899

        body = await (await test_client.get("/api/status")).json()
        connection = body["video"]["connection"]
        assert connection["host"] == "10.1.1.1"
        assert connection["port"] == 47899


def _stack(cfg):
    """A minimal server stack for a one-off app."""
    router = Router()
    sessions = SessionManager(cfg.password, auto_approve=True)
    registry = VideoRegistry(mode=MODE_EXTERNAL)
    datapath = Datapath(
        sessions, router, bind_host="127.0.0.1", bind_port=0,
        realtime=False, video_registry=registry,
    )
    return router, sessions, registry, datapath


class TestTheLinkIsBuiltWhenVideoIsTurnedOn:
    """A server that booted with video off had no link, and turning video on in
    the GUI created none: the address and password were saved, "Connecting..."
    was reported, and nothing dialled out. It read as the video server refusing
    us, and only a restart fixed it -- which nobody would think to try.
    """

    async def test_no_link_exists_while_video_is_off(self, client):
        test_client, registry, _cfg = client
        await login(test_client)
        registry.mode = MODE_OFF
        await test_client.post("/api/video/mode", json={"mode": MODE_OFF})

        assert test_client.app["state"].video_link is None

    async def test_choosing_external_builds_the_link(self, client):
        test_client, _registry, _cfg = client
        await login(test_client)
        state = test_client.app["state"]

        await test_client.post("/api/video/mode", json={"mode": MODE_EXTERNAL})
        try:
            assert state.video_link is not None, "turning video on created no link"
            assert state.video_link.is_running
        finally:
            if state.video_link is not None:
                state.video_link.stop()

    async def test_entering_an_address_builds_the_link_too(self, client):
        """The operator may fill the address in before touching the mode."""
        test_client, registry, _cfg = client
        await login(test_client)
        state = test_client.app["state"]
        registry.mode = MODE_EXTERNAL
        state.video_link = None

        await test_client.post(
            "/api/video/connection",
            json={"host": "192.168.1.30", "port": 47810, "password": "video-password"},
        )
        try:
            assert state.video_link is not None
        finally:
            if state.video_link is not None:
                state.video_link.stop()

    async def test_switching_off_stops_the_link(self, client):
        test_client, _registry, _cfg = client
        await login(test_client)
        state = test_client.app["state"]

        await test_client.post("/api/video/mode", json={"mode": MODE_EXTERNAL})
        assert state.video_link is not None

        await test_client.post("/api/video/mode", json={"mode": MODE_OFF})
        assert state.video_link is None, "the link kept dialling with video off"

    async def test_an_address_saved_while_off_says_so(self, client):
        """Rather than reporting "Connecting..." when nothing will."""
        test_client, registry, _cfg = client
        await login(test_client)
        registry.mode = MODE_OFF

        response = await test_client.post(
            "/api/video/connection",
            json={"host": "192.168.1.30", "password": "video-password"},
        )
        body = await response.json()
        assert "switched off" in body["message"]

    async def test_embedded_mode_invents_a_password(self, client):
        """There is nobody to agree one with for a local subprocess."""
        test_client, _registry, cfg = client
        await login(test_client)
        state = test_client.app["state"]
        cfg.video_password = ""

        await test_client.post("/api/video/mode", json={"mode": MODE_EMBEDDED})
        try:
            assert cfg.video_password, "the child would have had no credential"
            assert cfg.video_host == "127.0.0.1"
        finally:
            if state.embedded_video is not None:
                await state.embedded_video.stop()
            if state.video_link is not None:
                state.video_link.stop()

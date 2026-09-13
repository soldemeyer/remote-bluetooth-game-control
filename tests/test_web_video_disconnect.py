"""Letting go of a video server.

There was no way to do it short of switching video off entirely -- which also
stops the source being advertised to clients, and is a different intent. So
Connect became the only half of a pair.

The address and the password are kept on purpose: Disconnect is not Forget, and
pressing it should leave Connect able to work with nothing retyped. That is also
what makes it safe to offer without a confirmation -- it costs the picture and
nothing else.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from server import config as server_config
from server.bt.profiles import create_profile
from server.bt.sink import MockSink
from server.datapath import Datapath
from server.router import OutputChannel, Router
from server.sessions import SessionManager
from server.video import MODE_EXTERNAL, VideoRegistry
from server.web.app import create_app
from tests.webjs import needs_node, run_node

STATIC = Path(__file__).resolve().parent.parent / "server" / "web" / "static"
ADMIN_PASSWORD = "video-disconnect-admin"


@pytest.fixture
async def client():
    cfg = server_config.ServerConfig(
        password="client-password",
        admin_password=ADMIN_PASSWORD,
        tls_enabled=False,
        video_mode=MODE_EXTERNAL,
        video_host="192.168.1.20",
        video_password="the-video-password",
    )
    router = Router()
    router.add_channel(
        OutputChannel(
            bd_addr="00:00:00:00:00:01", hci_name="mock0",
            profile=create_profile("generic"), sink=MockSink(name="mock0"),
        )
    )
    sessions = SessionManager(cfg.password, auto_approve=True)
    registry = VideoRegistry(mode=MODE_EXTERNAL)
    datapath = Datapath(
        sessions, router, bind_host="127.0.0.1", bind_port=0,
        realtime=False, video_registry=registry,
    )
    app = create_app(cfg, sessions, router, datapath, video_registry=registry)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    assert (await test_client.post(
        "/api/login", json={"password": ADMIN_PASSWORD})).status == 200

    yield test_client, app["state"], cfg

    await test_client.close()


class TestTheEndpoint:
    async def test_it_needs_a_login(self):
        cfg = server_config.ServerConfig(
            password="p", admin_password=ADMIN_PASSWORD, tls_enabled=False)
        router = Router()
        sessions = SessionManager(cfg.password)
        datapath = Datapath(
            sessions, router, bind_host="127.0.0.1", bind_port=0, realtime=False)
        app = create_app(cfg, sessions, router, datapath,
                         video_registry=VideoRegistry(mode=MODE_EXTERNAL))
        test_client = TestClient(TestServer(app))
        await test_client.start_server()
        try:
            response = await test_client.post("/api/video/disconnect", json={})
            assert response.status == 401
        finally:
            await test_client.close()

    async def test_it_stops_the_link(self, client):
        test_client, state, _cfg = client
        await test_client.post(
            "/api/video/connection",
            json={"host": "192.168.1.20", "port": 47810, "password": "abcdefgh"})
        assert state.video_link is not None

        response = await test_client.post("/api/video/disconnect", json={})

        assert response.status == 200
        assert state.video_link is None

    async def test_it_keeps_the_address_and_the_password(self, client):
        """Disconnect is not Forget. Connect must work again with nothing
        retyped, which is also what makes it safe to offer unconfirmed."""
        test_client, state, cfg = client
        await test_client.post(
            "/api/video/connection",
            json={"host": "192.168.1.20", "port": 47810, "password": "abcdefgh"})

        await test_client.post("/api/video/disconnect", json={})

        assert cfg.video_host == "192.168.1.20"
        assert cfg.video_port == 47810
        assert cfg.video_password == "abcdefgh"

    async def test_connecting_again_rebuilds_the_link(self, client):
        test_client, state, _cfg = client
        await test_client.post(
            "/api/video/connection",
            json={"host": "192.168.1.20", "port": 47810, "password": "abcdefgh"})
        await test_client.post("/api/video/disconnect", json={})
        assert state.video_link is None

        await test_client.post(
            "/api/video/connection", json={"host": "192.168.1.20", "port": 47810})

        assert state.video_link is not None

    async def test_nothing_puts_it_back_on_its_own(self, client):
        """No latch is needed here, unlike the adapter Sleep button whose
        invariant would otherwise restore the radio -- but that is only true
        while the link is built by deliberate acts. A maintenance tick that
        called `_ensure_video_link` would undo this within seconds, silently."""
        test_client, state, _cfg = client
        await test_client.post(
            "/api/video/connection",
            json={"host": "192.168.1.20", "port": 47810, "password": "abcdefgh"})
        await test_client.post("/api/video/disconnect", json={})

        for _ in range(5):
            await test_client.get("/api/status")

        assert state.video_link is None, "something rebuilt the link unasked"

    async def test_disconnecting_twice_is_not_an_error(self, client):
        test_client, _state, _cfg = client
        first = await test_client.post("/api/video/disconnect", json={})
        second = await test_client.post("/api/video/disconnect", json={})
        assert first.status == 200
        assert second.status == 200

    async def test_the_advert_is_withdrawn(self, client):
        """Clients are told where the video is; leaving the old advert standing
        would have them dial a source this server is no longer talking to."""
        import inspect

        from server.web import app as web_app

        source = inspect.getsource(web_app.handle_video_disconnect)
        assert "broadcast_video_source" in source


#: Drives the real `renderVideoConnection` through both link states.
BUTTON_BODY = """
    const nop = () => {};
    function node() {
      const el = {
        value: '', textContent: '', title: '', disabled: false, dataset: {},
        _classes: new Set(),
        setAttribute: nop, removeAttribute: nop, getAttribute: () => null,
      };
      el.classList = {
        add: (c) => el._classes.add(c),
        remove: (c) => el._classes.delete(c),
        toggle: (c, on) => (on ? el._classes.add(c) : el._classes.delete(c)),
        contains: (c) => el._classes.has(c),
      };
      return el;
    }
    const ids = ['video-connection', 'video-host', 'video-port',
                 'video-advertise-host', 'video-advertise-port',
                 'video-password-hint', 'video-connect'];
    const nodes = {};
    for (const id of ids) nodes[id] = node();
    globalThis.document = {
      documentElement: node(), getElementById: (id) => nodes[id] || null,
      querySelector: () => null, querySelectorAll: () => [],
      addEventListener: nop, dispatchEvent: nop,
    };
    globalThis.addEventListener = nop;

    const video = await import(BASE + '/js/sections/video.js');
    const button = nodes['video-connect'];
    const read = () => ({ label: button.textContent, action: button.dataset.action,
                          secondary: button.classList.contains('secondary'),
                          title: button.title });

    const base = () => ({ mode: 'external', connection: {
      host: '192.168.1.20', port: 47810, has_password: true,
      advertise_host: '', advertise_port: 0, link: { connected: false } } });

    let v = base();
    video.renderVideoConnection(v);
    const off = read();

    v = base(); v.connection.link = { connected: true };
    video.renderVideoConnection(v);
    const on = read();

    // ...and back, because a button that only ever goes one way is half a fix.
    v = base();
    video.renderVideoConnection(v);
    const again = read();

    console.log(JSON.stringify({ off, on, again }));
"""


@needs_node
class TestTheButtonFlips:
    """Driven through the real renderer rather than read off the source: the
    question is what the operator ends up looking at, and a grep cannot tell an
    intention from a behaviour."""

    def states(self):
        import json as _json
        return _json.loads(run_node(BUTTON_BODY))

    def test_it_offers_connect_while_disconnected(self):
        off = self.states()["off"]
        assert off["label"] == "Connect"
        assert off["action"] == "video-connect"

    def test_it_offers_disconnect_once_connected(self):
        on = self.states()["on"]
        assert on["label"] == "Disconnect"
        assert on["action"] == "video-disconnect"

    def test_it_goes_back(self):
        """A button that only ever changes one way is half a fix -- and the
        way back is the one an operator hits after pressing Disconnect."""
        again = self.states()["again"]
        assert again["label"] == "Connect"
        assert again["action"] == "video-connect"

    def test_disconnect_is_not_dressed_as_the_primary_action(self):
        states = self.states()
        assert states["on"]["secondary"] is True
        assert states["off"]["secondary"] is False

    def test_it_says_what_disconnecting_costs(self):
        """Specifically that the address and password are kept, since the
        obvious fear is having to type them again."""
        assert "kept" in self.states()["on"]["title"]


#: Every state the line above the button can be in, driven through the real
#: renderer. The button was tested and the line beside it was not, which is how
#: "Connecting..." came to sit over a server nobody was connecting to.
HINT_BODY = """
    const nop = () => {};
    function node() {
      const el = { value: '', textContent: '', title: '', disabled: false,
                   dataset: {}, _classes: new Set(),
                   setAttribute: nop, removeAttribute: nop,
                   getAttribute: () => null };
      el.classList = { add: (c) => el._classes.add(c),
                       remove: (c) => el._classes.delete(c),
                       toggle: (c, on) => (on ? el._classes.add(c) : el._classes.delete(c)),
                       contains: (c) => el._classes.has(c) };
      return el;
    }
    const ids = ['video-connection', 'video-host', 'video-port',
                 'video-advertise-host', 'video-advertise-port',
                 'video-password-hint', 'video-connect'];
    const nodes = {};
    for (const id of ids) nodes[id] = node();
    globalThis.document = {
      documentElement: node(), getElementById: (id) => nodes[id] || null,
      querySelector: () => null, querySelectorAll: () => [],
      addEventListener: nop, dispatchEvent: nop,
    };
    globalThis.addEventListener = nop;

    const video = await import(BASE + '/js/sections/video.js');
    const cases = JSON.parse(process.env.RBGC_CASES);
    const out = {};
    for (const [name, connection] of Object.entries(cases)) {
      video.renderVideoConnection({ mode: 'external', connection });
      out[name] = { hint: nodes['video-password-hint'].textContent,
                    label: nodes['video-connect'].textContent };
    }
    console.log(JSON.stringify(out));
"""

CASES = {
    # Disconnected on purpose: there is no link object at all.
    "disconnected": {"host": "192.168.1.20", "port": 47810, "has_password": True,
                     "advertise_host": "", "advertise_port": 0, "link": None},
    "connected": {"host": "192.168.1.20", "port": 47810, "has_password": True,
                  "advertise_host": "", "advertise_port": 0,
                  "link": {"connected": True}},
    "trying": {"host": "192.168.1.20", "port": 47810, "has_password": True,
               "advertise_host": "", "advertise_port": 0,
               "link": {"connected": False, "last_error": ""}},
    "failing": {"host": "192.168.1.20", "port": 47810, "has_password": True,
                "advertise_host": "", "advertise_port": 0,
                "link": {"connected": False, "last_error": "Incorrect password."}},
    "no_address": {"host": "", "port": 47810, "has_password": True,
                   "advertise_host": "", "advertise_port": 0, "link": None},
    "no_password": {"host": "192.168.1.20", "port": 47810, "has_password": False,
                    "advertise_host": "", "advertise_port": 0, "link": None},
}


@needs_node
class TestTheLineAboveTheButton:
    """Reported: after pressing Disconnect it still read "Connecting...".

    `connection.link` is **null** when there is no link object -- and since
    Disconnect exists, that is a state the operator can deliberately put the
    server in. Folding it into `{}` made it indistinguishable from a link that
    exists and has not connected yet, so the line described an attempt that was
    not happening. Every other state was right, which is why the button looked
    finished.
    """

    def states(self):
        import json as _json
        return _json.loads(run_node(HINT_BODY, {"RBGC_CASES": _json.dumps(CASES)}))

    def test_disconnected_does_not_claim_to_be_connecting(self):
        hint = self.states()["disconnected"]["hint"]
        assert "Connecting" not in hint, (
            "the line describes an attempt that is not being made"
        )
        assert "Not connected" in hint

    def test_disconnected_says_how_to_come_back(self):
        assert "Press Connect" in self.states()["disconnected"]["hint"]

    def test_connected_says_where(self):
        hint = self.states()["connected"]["hint"]
        assert "Connected to 192.168.1.20:47810" in hint

    def test_a_live_attempt_still_says_connecting(self):
        """The state the null case was being mistaken for, and it is real: a
        link object exists and has not reached the source yet."""
        assert self.states()["trying"]["hint"] == "Connecting…"

    def test_a_failure_says_what_went_wrong(self):
        assert self.states()["failing"]["hint"] == "Incorrect password."

    def test_the_earlier_states_still_take_precedence(self):
        states = self.states()
        assert "type its address" in states["no_address"]["hint"]
        assert "password shown on the video server" in states["no_password"]["hint"]

    def test_the_button_agrees_with_the_line(self):
        """Both read the same `connected`, so they cannot disagree -- which is
        the shape of the bug being fixed, one of the pair updated and not the
        other."""
        states = self.states()
        assert states["connected"]["label"] == "Disconnect"
        for name in ("disconnected", "trying", "failing"):
            assert states[name]["label"] == "Connect", name


class TestTheButton:
    def page(self) -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    def video_js(self) -> str:
        return (STATIC / "js" / "sections" / "video.js").read_text(encoding="utf-8")

    def test_there_is_one_button_not_two(self):
        """Two side by side would need one of them disabled at all times, and a
        disabled button is a worse way of saying "not now" than the button
        simply being the other thing."""
        page = self.page()
        assert page.count('id="video-connect"') == 1
        assert 'data-action="video-disconnect"' not in page, (
            "the second action belongs on the same button, set at render time"
        )

    def test_it_is_rewritten_in_place(self):
        """Replacing the node between mousedown and mouseup eats the click --
        the failure this GUI already records for the adapter Wake/Sleep
        button."""
        body = self.video_js()
        block = body[body.index("const button = $('video-connect')"):]
        block = block[: block.index("\n}")]
        assert "setText(button" in block
        assert "button.dataset.action =" in block
        assert "innerHTML" not in block

    def test_it_is_not_written_while_the_operator_is_on_it(self):
        body = self.video_js()
        assert "if (button && !busy(button))" in body

    def test_both_actions_are_handled(self):
        app_js = (STATIC / "app.js").read_text(encoding="utf-8")
        assert "action === 'video-connect'" in app_js
        assert "action === 'video-disconnect'" in app_js

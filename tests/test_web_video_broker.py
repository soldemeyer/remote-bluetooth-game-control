"""The video leg of the room, and the half of a fixed bug that stayed broken.

Reported from the field: connecting over the Internet -- by hole-punch *and* by
relay -- gave working controller input and a picture that sat on "Connecting"
forever.

The cause is one this repository has already written a section about. The
gameplay leg used to be read from the config once, at startup, so saving a
broker in the web GUI wrote a file and changed nothing else; `_ensure_rendezvous`
fixes that. **The video leg keeps its own copy of the same two settings**, on
the registry, and it was still only ever written at startup.

Both halves of video-over-Internet hang off those two fields:

* `config_message` carries them to the source, which has no other way to learn
  where to register its own leg of the room;
* `source_advert` carries them to the client, whose connection ladder has no
  broker step without them -- so it can only try the source's LAN address,
  which is exactly what is not reachable from the Internet.

Which is why the symptom separates so cleanly: the controller works, because
that is the gameplay leg and it *was* reconciled, and the video never connects
at all. Nothing reports it, because from every counter's point of view there is
simply no broker configured for video.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from common.video import VideoSettings
from server import config as server_config
from server.bt.profiles import create_profile
from server.bt.sink import MockSink
from server.datapath import Datapath
from server.router import OutputChannel, Router
from server.sessions import SessionManager
from server.video import VideoRegistry
from server.web.app import create_app

ADMIN_PASSWORD = "broker-admin-password"
BROKER_HOST = "broker.invalid.example"
BROKER_PORT = 47900
ROOM = "room-abc123"


@pytest.fixture
async def client(monkeypatch):
    """A server that started with the Internet path **off**.

    The situation the report came from, and the one that matters: an operator
    turns Internet on in the web GUI on a server that is already running.
    """
    cfg = server_config.ServerConfig(
        password="client-password",
        admin_password=ADMIN_PASSWORD,
        tls_enabled=False,
        internet_enabled=False,
        broker_host="",
        room_code="",
    )

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
    datapath = Datapath(
        sessions, router, bind_host="127.0.0.1", bind_port=0, realtime=False
    )

    registry = VideoRegistry(
        mode="external", settings=VideoSettings(), configured=True
    )
    # Exactly what server/main.py does at startup, with Internet off.
    registry.broker = ""
    registry.room = ""

    # `resolve` is the only part of registration that touches the network.
    import server.rendezvous as rendezvous_module

    monkeypatch.setattr(
        rendezvous_module.RendezvousClient, "resolve", lambda self: True
    )

    app = create_app(cfg, sessions, router, datapath, video_registry=registry)
    server = TestServer(app)
    test_client = TestClient(server)
    await test_client.start_server()

    yield test_client, cfg, datapath, registry

    await test_client.close()


def attach_and_acknowledge(registry: VideoRegistry) -> None:
    """A source that has connected and acknowledged the current configuration.

    The state the advert needs before it will say `available`: a session, a
    reported media port, and a cfg_seq matching ours.
    """
    registry.attach_source_endpoint("10.0.0.5", 47810)
    registry.update_status_from_link(
        {
            "cfg_seq": registry.config_message()["cfg_seq"],
            "media_port": 47810,
            "lan_host": "10.0.0.5",
            "status": {"streaming": True, "width": 1280, "height": 720},
        }
    )


async def login(test_client: TestClient) -> None:
    assert (
        await test_client.post("/api/login", json={"password": ADMIN_PASSWORD})
    ).status == 200


async def turn_the_internet_on(test_client, cfg, datapath) -> None:
    """What the operator does: set a room, accept Internet, save a broker."""
    cfg.room_code = ROOM
    cfg.internet_enabled = True
    datapath.set_accepting(internet=True)
    response = await test_client.post(
        "/api/server/visibility",
        json={"broker": f"{BROKER_HOST}:{BROKER_PORT}", "internet_discoverable": True},
    )
    assert response.status == 200


class TestBothLegsAreReconciledTogether:
    async def test_the_video_leg_follows_the_gameplay_one(self, client):
        """The reported fault, in one assertion.

        Before the fix the first of these passed and the second did not, which
        is the whole shape of the report: the controller works and the picture
        does not.
        """
        test_client, cfg, datapath, registry = client
        await login(test_client)
        await turn_the_internet_on(test_client, cfg, datapath)

        gameplay = getattr(datapath, "_rendezvous", None)
        assert gameplay is not None, "the gameplay leg did not register"
        assert gameplay._room == ROOM

        assert registry.broker == f"{BROKER_HOST}:{BROKER_PORT}", (
            "the video leg was left with the broker it had at startup -- "
            "the controller works and video never connects"
        )
        assert registry.room == ROOM

    async def test_the_source_is_told_where_to_register(self, client):
        """It has no other way to learn. Without this the video source never
        registers its leg of the room, so the client's broker attempt waits
        for a peer that is not there and times out."""
        test_client, cfg, datapath, registry = client
        await login(test_client)
        await turn_the_internet_on(test_client, cfg, datapath)

        message = registry.config_message()
        assert message["broker"] == f"{BROKER_HOST}:{BROKER_PORT}"
        assert message["room"] == ROOM

    async def test_the_source_is_actually_re_pushed(self, client):
        """The load-bearing half.

        Updating the fields without bumping the sequence would fix the
        client's half and leave the source holding the empty broker it was
        first told about -- so video would still never connect, for a reason
        one step further from the symptom.
        """
        test_client, cfg, datapath, registry = client
        await login(test_client)

        # A source attaches and acknowledges the configuration as it stands.
        attach_and_acknowledge(registry)
        assert not registry.needs_config_push()

        await turn_the_internet_on(test_client, cfg, datapath)

        assert registry.needs_config_push(), (
            "the broker changed and the source was never told again"
        )

    async def test_the_client_is_given_a_broker_to_try(self, client):
        """Without one, the connection ladder has only the source's LAN
        address -- which is precisely what a remote player cannot reach."""
        test_client, cfg, datapath, registry = client
        await login(test_client)

        attach_and_acknowledge(registry)
        await turn_the_internet_on(test_client, cfg, datapath)

        advert = registry.source_advert()
        assert advert["broker"] == f"{BROKER_HOST}:{BROKER_PORT}"
        assert advert["room"] == ROOM


class TestTurningItOffReachesVideoToo:
    async def test_clearing_the_internet_path_clears_the_video_leg(self, client):
        """The other direction. A registry left pointing at a broker the
        operator has switched off would keep advertising a path nobody is
        listening on."""
        test_client, cfg, datapath, registry = client
        await login(test_client)
        await turn_the_internet_on(test_client, cfg, datapath)
        assert registry.broker

        cfg.internet_enabled = False
        datapath.set_accepting(internet=False)
        response = await test_client.post(
            "/api/server/visibility", json={"internet_discoverable": False}
        )
        assert response.status == 200

        assert registry.broker == ""
        assert registry.room == ""


class TestItCostsNothingWhenNothingChanged:
    async def test_an_unchanged_broker_does_not_re_push(self, client):
        """Saving the same settings again must not churn the source.

        `set_broker` returning False on a no-op is what keeps this cheap
        enough to call on every visibility change -- which is what stops the
        two legs drifting apart again.
        """
        test_client, cfg, datapath, registry = client
        await login(test_client)
        await turn_the_internet_on(test_client, cfg, datapath)

        attach_and_acknowledge(registry)
        assert not registry.needs_config_push()

        # Save the very same thing again.
        response = await test_client.post(
            "/api/server/visibility",
            json={
                "broker": f"{BROKER_HOST}:{BROKER_PORT}",
                "internet_discoverable": True,
            },
        )
        assert response.status == 200
        assert not registry.needs_config_push(), "an unchanged save churned the source"

    async def test_set_broker_reports_whether_it_moved(self, client):
        _test_client, _cfg, _datapath, registry = client
        assert registry.set_broker("host:1", "room") is True
        assert registry.set_broker("host:1", "room") is False
        assert registry.set_broker("", "") is True


class TestAServerWithNoVideoAtAll:
    async def test_reconciling_is_harmless_without_a_registry(self, monkeypatch):
        """`video_registry` is optional -- `--video-mode off` passes None --
        and a visibility save must not fail because of it."""
        cfg = server_config.ServerConfig(
            password="p", admin_password=ADMIN_PASSWORD, tls_enabled=False
        )
        router = Router()
        sessions = SessionManager(cfg.password, auto_approve=True)
        datapath = Datapath(
            sessions, router, bind_host="127.0.0.1", bind_port=0, realtime=False
        )
        app = create_app(cfg, sessions, router, datapath)   # no registry
        server = TestServer(app)
        test_client = TestClient(server)
        await test_client.start_server()
        try:
            await login(test_client)
            response = await test_client.post(
                "/api/server/visibility", json={"internet_discoverable": False}
            )
            assert response.status == 200
        finally:
            await test_client.close()


class TestTheClientSaysWhyThereWasNoInternetAttempt:
    """The other half of the report: nothing pointed at the cause.

    The failure detail *is* drawn and logged -- but with no broker in the
    advert it listed only the two LAN timeouts, and said nothing about the
    third rung that was never attempted. A remote player sees two addresses on
    a network they are not on, time out, and no explanation for why there was
    no Internet attempt.
    """

    def test_a_missing_broker_is_named_in_the_failure(self):
        from client.net.video import VideoReceiver, VideoStreamState

        receiver = VideoReceiver(password="p", client_name="c")
        assert receiver._connect({"lan_host": "10.0.0.5", "port": 47810}) is None
        assert receiver.state is VideoStreamState.FAILED
        assert "no broker configured for video" in receiver.state_detail
        # And it points at where to fix it, not just at what is wrong.
        assert "web GUI" in receiver.state_detail

    def test_a_broker_that_is_present_is_not_complained_about(self):
        """The note must not appear when there *was* an Internet path -- it
        would then be a false statement in the one place somebody is reading
        carefully."""
        from client.net.video import VideoReceiver

        receiver = VideoReceiver(password="p", client_name="c")
        receiver._connect({
            "lan_host": "10.0.0.5",
            "port": 47810,
            "broker": "broker.invalid.example:47900",
            "room": "room-abc123",
        })
        assert "no broker configured" not in receiver.state_detail


class TestTheOperatorCanSeeIt:
    """The durable half of the fix.

    The gameplay leg got a `broker_status` after the same class of fault. The
    video leg had nothing at all, which is how a broker that reached
    hole-punching for the controller and never reached video went unnoticed.
    """

    async def status(self, test_client) -> dict:
        response = await test_client.get("/api/status")
        assert response.status == 200
        return (await response.json())["video"]["broker_status"]

    async def test_it_says_when_the_two_copies_have_drifted(self, client):
        """Before the fix this was the live state and nothing showed it:
        configured everywhere the gameplay leg looks, and absent here."""
        test_client, cfg, datapath, registry = client
        await login(test_client)

        cfg.broker_host = BROKER_HOST
        cfg.room_code = ROOM
        cfg.internet_enabled = True
        datapath.set_accepting(internet=True)
        # ...and the registry left as it was at startup, which is the bug.
        registry.broker = ""
        registry.room = ""

        assert (await self.status(test_client))["state"] == "not_applied"

    async def test_it_tells_the_unconfigured_cases_apart(self, client):
        """One bit could not distinguish these, and the true one is always the
        one nobody would guess."""
        test_client, cfg, datapath, registry = client
        await login(test_client)

        assert (await self.status(test_client))["state"] == "unconfigured"

        cfg.broker_host = BROKER_HOST
        assert (await self.status(test_client))["state"] == "no_room"

        # `Datapath` accepts Internet by default and `server/main.py` gates it
        # from the config, so a test has to say so explicitly.
        cfg.room_code = ROOM
        datapath.set_accepting(internet=False)
        assert (await self.status(test_client))["state"] == "internet_off"

    async def test_it_reports_a_working_video_leg(self, client):
        test_client, cfg, datapath, registry = client
        await login(test_client)
        await turn_the_internet_on(test_client, cfg, datapath)

        assert (await self.status(test_client))["state"] == "no_source"

        attach_and_acknowledge(registry)
        state = await self.status(test_client)
        assert state["state"] == "acknowledged"
        assert state["broker"] == f"{BROKER_HOST}:{BROKER_PORT}"
        assert state["room"] == ROOM

    async def test_it_does_not_claim_the_source_registered(self, client):
        """That happens on the source and it does not report back. The honest
        claim is that the configuration carrying the broker was acknowledged,
        which is what the state is named for."""
        test_client, cfg, datapath, registry = client
        await login(test_client)
        await turn_the_internet_on(test_client, cfg, datapath)
        attach_and_acknowledge(registry)

        state = await self.status(test_client)
        assert "registered" not in str(state).lower()


class TestTheWebGuiActuallyShowsIt:
    """A status nothing renders is a status nobody reads.

    The same trap this repository records for split-screen: every piece of
    that feature worked and there was no control anywhere to switch it on,
    because each half was tested against the other rather than against the
    operator.
    """

    @staticmethod
    def _static(name: str) -> str:
        from pathlib import Path as P

        root = P(__file__).resolve().parent.parent / "server" / "web" / "static"
        return (root / name).read_text(encoding="utf-8")

    def test_the_markup_has_somewhere_to_put_it(self):
        assert 'id="video-broker"' in self._static("index.html")

    def test_the_renderer_fills_it_in(self):
        source = self._static("js/sections/video.js")
        assert "video-broker" in source
        assert "describeVideoBroker" in source

    def test_every_state_the_server_can_report_has_words(self):
        """A state with no case falls to the default, which would tell an
        operator video is unconfigured while the server says otherwise."""
        source = self._static("js/sections/video.js")
        for state in ("acknowledged", "pending", "no_source", "not_applied",
                      "no_room", "internet_off"):
            assert f"'{state}'" in source, state

    def test_it_does_not_claim_the_source_registered(self):
        """It cannot know. The source registers and does not report back."""
        source = self._static("js/sections/video.js")
        start = source.index("export function describeVideoBroker")
        body = source[start:]
        assert "Registered with" not in body

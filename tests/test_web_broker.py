"""Web GUI: the rendezvous broker, and saying what it is actually doing.

Four faults reported together, with one cause between three of them. The broker
was wired up **once, at startup**, from the config as it stood then. Saving one
on the Visibility page wrote the file and nothing else, so hole-punching stayed
dead until somebody restarted the service.

Measured in the field: a config written **22 hours after** the process started,
a server that therefore never registered, a broker holding no rooms, and a
client whose Search consequently found nothing and said only "No servers found
-- use Custom", which points the player at their own settings when the fault is
entirely on the server. None of the four symptoms pointed at the cause.

The fourth was separate and equally misleading: with LAN accepting switched
off, the server drops every datagram before parsing it and never answers a
discovery probe -- yet saving Visibility reported "On this network: visible",
which is not a nuance but a false statement about the transport.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from server import config as server_config
from server.bt.profiles import create_profile
from server.bt.sink import MockSink
from server.datapath import Datapath
from server.router import OutputChannel, Router
from server.sessions import SessionManager
from server.web.app import create_app

ADMIN_PASSWORD = "broker-admin-password"
BROKER_HOST = "broker.invalid.example"
ROOM = "room-abc123"


def build_config(**overrides) -> server_config.ServerConfig:
    base = dict(
        password="client-password",
        admin_password=ADMIN_PASSWORD,
        tls_enabled=False,
    )
    base.update(overrides)
    return server_config.ServerConfig(**base)


@pytest.fixture
async def client(request):
    cfg = getattr(request, "param", None) or build_config()
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

    app = create_app(cfg, sessions, router, datapath)
    server = TestServer(app)
    test_client = TestClient(server)
    await test_client.start_server()

    yield test_client, cfg, datapath

    await test_client.close()


async def login(test_client: TestClient) -> None:
    response = await test_client.post("/api/login", json={"password": ADMIN_PASSWORD})
    assert response.status == 200


async def status_of(test_client: TestClient) -> dict:
    response = await test_client.get("/api/status")
    assert response.status == 200
    return await response.json()


class TestTheBrokerAppliesWithoutARestart:
    """The reported fault: saving a broker changed a file and nothing else."""

    async def test_saving_a_broker_registers_without_a_restart(
        self, client, monkeypatch
    ):
        test_client, cfg, datapath = client
        await login(test_client)

        cfg.room_code = ROOM
        cfg.internet_enabled = True
        datapath.set_accepting(internet=True)

        # `resolve` is the only part that touches the network.
        import server.rendezvous as rendezvous_module

        monkeypatch.setattr(
            rendezvous_module.RendezvousClient, "resolve", lambda self: True
        )

        assert getattr(datapath, "_rendezvous", None) is None

        response = await test_client.post(
            "/api/server/visibility",
            json={"broker": f"{BROKER_HOST}:47900", "internet_discoverable": True},
        )
        assert response.status == 200

        client_obj = getattr(datapath, "_rendezvous", None)
        assert client_obj is not None, (
            "the broker was saved but never applied -- the exact fault reported"
        )
        assert client_obj._broker_host == BROKER_HOST
        assert client_obj._room == ROOM

    async def test_turning_internet_off_stops_registering(self, client, monkeypatch):
        test_client, cfg, datapath = client
        await login(test_client)

        cfg.room_code = ROOM
        cfg.internet_enabled = True
        datapath.set_accepting(internet=True)

        import server.rendezvous as rendezvous_module

        monkeypatch.setattr(
            rendezvous_module.RendezvousClient, "resolve", lambda self: True
        )

        await test_client.post(
            "/api/server/visibility", json={"broker": f"{BROKER_HOST}:47900"}
        )
        assert getattr(datapath, "_rendezvous", None) is not None

        response = await test_client.post(
            "/api/server/state", json={"internet": False}
        )
        assert response.status == 200
        assert getattr(datapath, "_rendezvous", None) is None, (
            "still registered with the broker after Internet was switched off"
        )

    async def test_an_unresolvable_broker_says_so_rather_than_pretending(
        self, client, monkeypatch
    ):
        test_client, cfg, datapath = client
        await login(test_client)

        cfg.room_code = ROOM
        cfg.internet_enabled = True
        datapath.set_accepting(internet=True)

        import server.rendezvous as rendezvous_module

        monkeypatch.setattr(
            rendezvous_module.RendezvousClient, "resolve", lambda self: False
        )

        response = await test_client.post(
            "/api/server/visibility", json={"broker": f"{BROKER_HOST}:47900"}
        )
        payload = await response.json()

        assert getattr(datapath, "_rendezvous", None) is None
        assert "could not be resolved" in payload["message"]

    async def test_resaving_the_same_broker_keeps_the_registration(
        self, client, monkeypatch
    ):
        """Replacing it would drop a live registration for no reason."""
        test_client, cfg, datapath = client
        await login(test_client)

        cfg.room_code = ROOM
        cfg.internet_enabled = True
        datapath.set_accepting(internet=True)

        import server.rendezvous as rendezvous_module

        monkeypatch.setattr(
            rendezvous_module.RendezvousClient, "resolve", lambda self: True
        )

        await test_client.post(
            "/api/server/visibility", json={"broker": f"{BROKER_HOST}:47900"}
        )
        first = getattr(datapath, "_rendezvous", None)

        await test_client.post(
            "/api/server/visibility",
            json={"broker": f"{BROKER_HOST}:47900", "internet_discoverable": False},
        )
        second = getattr(datapath, "_rendezvous", None)

        assert second is first, "a no-op save tore down the live registration"
        assert second._public_name == "", "hiding did not reach the broker"


class TestTheStatusSaysWhichStateItIsIn:
    """One bit could not tell "unset" from "set but inert" from "working"."""

    async def test_no_broker_reads_as_unconfigured(self, client):
        test_client, _cfg, _datapath = client
        await login(test_client)

        status = await status_of(test_client)
        assert status["server"]["broker_status"]["state"] == "unconfigured"

    async def test_a_broker_with_no_room_says_so(self, client):
        test_client, cfg, _datapath = client
        await login(test_client)

        cfg.broker_host = BROKER_HOST
        cfg.room_code = ""

        status = await status_of(test_client)
        assert status["server"]["broker_status"]["state"] == "no_room"

    async def test_a_broker_with_internet_off_says_so(self, client):
        test_client, cfg, datapath = client
        await login(test_client)

        cfg.broker_host = BROKER_HOST
        cfg.room_code = ROOM
        # The Datapath object opens its gates by default; the *config* defaults
        # are off and main.py applies them. Set it explicitly rather than
        # relying on either.
        datapath.set_accepting(internet=False)

        status = await status_of(test_client)
        assert status["server"]["broker_status"]["state"] == "internet_off"

    async def test_a_live_registration_reports_its_room(self, client, monkeypatch):
        test_client, cfg, datapath = client
        await login(test_client)

        cfg.room_code = ROOM
        cfg.internet_enabled = True
        datapath.set_accepting(internet=True)

        import server.rendezvous as rendezvous_module

        monkeypatch.setattr(
            rendezvous_module.RendezvousClient, "resolve", lambda self: True
        )
        await test_client.post(
            "/api/server/visibility", json={"broker": f"{BROKER_HOST}:47900"}
        )

        # Registration is acknowledged by the broker, which is not present here.
        datapath._rendezvous._registered = True

        status = await status_of(test_client)
        broker = status["server"]["broker_status"]
        assert broker["state"] == "registered"
        assert broker["room"] == ROOM


class TestVisibilityDoesNotClaimTheImpossible:
    async def test_lan_off_is_not_reported_as_visible(self, client):
        """The reported fault: "visible" on a transport that is switched off.

        With LAN accepting off the datapath drops every datagram before parsing
        it and never answers a discovery probe, so nothing on that network can
        find the server whatever `lan_discoverable` says.
        """
        test_client, _cfg, datapath = client
        await login(test_client)

        datapath.set_accepting(lan=False)

        response = await test_client.post(
            "/api/server/visibility", json={"lan_discoverable": True}
        )
        message = (await response.json())["message"]

        assert "visible" not in message.split("Over the Internet")[0], (
            f"claimed LAN visibility while LAN accepting is off: {message!r}"
        )
        assert "not accepting" in message

    async def test_lan_on_still_reports_visibility_normally(self, client):
        test_client, _cfg, datapath = client
        await login(test_client)

        datapath.set_accepting(lan=True)

        response = await test_client.post(
            "/api/server/visibility", json={"lan_discoverable": True}
        )
        message = (await response.json())["message"]
        assert "On this network: visible" in message

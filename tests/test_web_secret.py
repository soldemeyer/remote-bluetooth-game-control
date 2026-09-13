"""Revealing the client password, and the rule it deliberately does not break.

`build_status` has never carried a password, not even a masked one, and that
stays true: the status snapshot reaches every open browser ten times a second,
so a secret in it would sit in every frame of every socket for the life of the
session. The eye icon on the Identity card is the opposite shape -- one read,
when the operator asks for it, and a line in the log saying it happened.

There is no second password prompt, on purpose. The same session can already
*change* this password outright through `/api/server/identity`, so gating the
read behind a higher bar than the write would be theatre. And where no separate
admin password is set, the admin password *is* the client password -- the one
the operator typed to get in here, so there would be nothing to reveal.

What the log line buys is the case that actually happens: a browser left signed
in on a shelf. That becomes visible afterwards rather than not at all.
"""

from __future__ import annotations

import json
import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer

from server import config as server_config
from server.bt.profiles import create_profile
from server.bt.sink import MockSink
from server.datapath import Datapath
from server.router import OutputChannel, Router
from server.sessions import SessionManager
from server.web.app import create_app

ADMIN_PASSWORD = "admin-password-long"
CLIENT_PASSWORD = "the-players-password"


@pytest.fixture
async def client():
    cfg = server_config.ServerConfig(
        password=CLIENT_PASSWORD,
        admin_password=ADMIN_PASSWORD,
        tls_enabled=False,
    )
    router = Router()
    router.add_channel(
        OutputChannel(
            bd_addr="00:00:00:00:00:01", hci_name="mock0",
            profile=create_profile("generic"), sink=MockSink(name="mock0"),
        )
    )
    sessions = SessionManager(cfg.password, auto_approve=True)
    datapath = Datapath(
        sessions, router, bind_host="127.0.0.1", bind_port=0, realtime=False,
    )
    app = create_app(cfg, sessions, router, datapath)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()

    yield test_client, cfg

    await test_client.close()


async def login(test_client: TestClient) -> None:
    response = await test_client.post("/api/login", json={"password": ADMIN_PASSWORD})
    assert response.status == 200


class TestItIsGated:
    async def test_it_needs_a_session(self, client):
        test_client, _cfg = client
        response = await test_client.post(
            "/api/server/secret", json={"what": "client_password"})
        assert response.status == 401

    async def test_an_unknown_secret_is_refused_rather_than_guessed(self, client):
        """The body names what it wants so the endpoint can grow -- the video
        password, the room code. An unrecognised name must not fall through to
        whichever one happens to be first."""
        test_client, _cfg = client
        await login(test_client)

        response = await test_client.post(
            "/api/server/secret", json={"what": "admin_password"})
        assert response.status == 400


class TestItAnswersWithTheLiveValue:
    async def test_it_returns_the_configured_password(self, client):
        test_client, _cfg = client
        await login(test_client)

        response = await test_client.post(
            "/api/server/secret", json={"what": "client_password"})

        assert response.status == 200
        assert (await response.json())["password"] == CLIENT_PASSWORD

    async def test_it_follows_a_change_rather_than_caching(self, client):
        test_client, _cfg = client
        await login(test_client)
        await test_client.post(
            "/api/server/identity", json={"password": "a-new-password"})

        response = await test_client.post(
            "/api/server/secret", json={"what": "client_password"})

        assert (await response.json())["password"] == "a-new-password"

    async def test_it_is_not_cached_by_the_browser(self, client):
        test_client, _cfg = client
        await login(test_client)

        response = await test_client.post(
            "/api/server/secret", json={"what": "client_password"})

        assert "no-store" in response.headers.get("Cache-Control", "")


class TestTheStatusStillCarriesNothing:
    async def test_no_password_appears_anywhere_in_the_status(self, client):
        """The rule this endpoint exists *not* to break.

        Checked against the serialised payload rather than named fields: a
        password reaching the status through some new key would pass a
        field-by-field check and still be in every socket frame.
        """
        test_client, _cfg = client
        await login(test_client)

        response = await test_client.get("/api/status")
        body = json.dumps(await response.json())

        assert CLIENT_PASSWORD not in body
        assert ADMIN_PASSWORD not in body

    async def test_it_stays_absent_after_a_reveal(self, client):
        test_client, _cfg = client
        await login(test_client)
        await test_client.post("/api/server/secret", json={"what": "client_password"})

        body = json.dumps(await (await test_client.get("/api/status")).json())
        assert CLIENT_PASSWORD not in body


class TestItIsRecorded:
    async def test_a_reveal_leaves_a_log_line(self, client, caplog):
        """The realistic exposure here is a browser left open, not an
        attacker. A line in the journal is what makes that visible
        afterwards."""
        test_client, _cfg = client
        await login(test_client)

        with caplog.at_level(logging.INFO, logger="server.web.app"):
            await test_client.post(
                "/api/server/secret", json={"what": "client_password"})

        assert any("revealed" in record.message.lower() for record in caplog.records), (
            "a credential left the process with nothing recording it"
        )

    async def test_the_log_line_does_not_contain_the_password(self, client, caplog):
        test_client, _cfg = client
        await login(test_client)

        with caplog.at_level(logging.INFO):
            await test_client.post(
                "/api/server/secret", json={"what": "client_password"})

        assert all(CLIENT_PASSWORD not in r.getMessage() for r in caplog.records)

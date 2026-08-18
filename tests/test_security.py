"""Regression tests for the findings in SECURITY.md.

Each test names the finding it guards. They exist so a future refactor cannot
quietly reintroduce a fixed vulnerability -- which is exactly how security
regressions normally happen.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from common import crypto
from common.protocol import PROTOCOL_VERSION, PacketType
from server import config as server_config
from server.bt.profiles import create_profile
from server.bt.sink import MockSink
from server.datapath import Datapath
from server.router import OutputChannel, Router
from server.sessions import MAX_PENDING_HELLO, SessionManager
from server.web.app import SECURITY_HEADERS, create_app

PASSWORD = "security-test-password"
ADMIN_PASSWORD = "different-admin-password"


@pytest.fixture
def web_env():
    """A web app wired to a real server stack, on a loopback test client."""
    cfg = server_config.ServerConfig()
    cfg.password = PASSWORD
    cfg.server_name = "sec-test"

    router = Router()
    router.add_channel(
        OutputChannel(
            bd_addr="00:00:00:00:00:00",
            hci_name="sec0",
            profile=create_profile("generic"),
            sink=MockSink(name="sec0"),
        )
    )
    sessions = SessionManager(PASSWORD, auto_approve=False)
    datapath = Datapath(sessions, router, bind_host="127.0.0.1", bind_port=0, realtime=False)

    return cfg, create_app(cfg, sessions, router, datapath)


async def _client(app) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


class TestFinding2LoginRateLimiting:
    """Finding 2 (High): web login had no rate limit, only a per-request delay
    that parallel connections bypassed entirely."""

    async def test_repeated_failures_are_rate_limited(self, web_env):
        _, app = web_env
        client = await _client(app)
        try:
            statuses = []
            for _ in range(8):
                resp = await client.post("/api/login", json={"password": "wrong"})
                statuses.append(resp.status)

            assert 429 in statuses, (
                "no request was rate limited; unlimited guessing is possible"
            )
        finally:
            await client.close()

    async def test_parallel_attempts_cannot_bypass_the_limiter(self, web_env):
        """The original defence was a sleep, which concurrency defeated."""
        _, app = web_env
        client = await _client(app)
        try:
            results = await asyncio.gather(
                *[client.post("/api/login", json={"password": "wrong"}) for _ in range(12)]
            )
            statuses = [r.status for r in results]
            assert 429 in statuses, "parallel guessing was not limited"
        finally:
            await client.close()

    async def test_correct_password_still_works_before_lockout(self, web_env):
        _, app = web_env
        client = await _client(app)
        try:
            resp = await client.post("/api/login", json={"password": PASSWORD})
            assert resp.status == 200
        finally:
            await client.close()


class TestFinding3SeparateAdminPassword:
    """Finding 3 (Medium): one password granted both player and operator access."""

    async def test_admin_password_is_used_when_set(self, web_env):
        cfg, app = web_env
        cfg.admin_password = ADMIN_PASSWORD

        client = await _client(app)
        try:
            resp = await client.post("/api/login", json={"password": ADMIN_PASSWORD})
            assert resp.status == 200
        finally:
            await client.close()

    async def test_client_password_is_rejected_once_admin_password_is_set(self, web_env):
        """The whole point: a player who knows the game password must not get
        operator access."""
        cfg, app = web_env
        cfg.admin_password = ADMIN_PASSWORD

        client = await _client(app)
        try:
            resp = await client.post("/api/login", json={"password": PASSWORD})
            assert resp.status == 401
        finally:
            await client.close()

    async def test_falls_back_to_client_password_when_unset(self, web_env):
        """Existing deployments must keep working."""
        _, app = web_env
        client = await _client(app)
        try:
            resp = await client.post("/api/login", json={"password": PASSWORD})
            assert resp.status == 200
        finally:
            await client.close()

    def test_admin_password_is_never_persisted(self, tmp_path):
        path = tmp_path / "server.json"
        cfg = server_config.ServerConfig(
            password="client-secret", admin_password="admin-secret"
        )
        server_config.save(cfg, path)

        text = path.read_text()
        assert "admin-secret" not in text
        assert "client-secret" not in text


class TestFinding4SecurityHeaders:
    """Finding 4 (Medium): no headers, so the approve/deny UI could be framed."""

    async def test_headers_present_on_success(self, web_env):
        _, app = web_env
        client = await _client(app)
        try:
            resp = await client.get("/api/status")
            for header in ("X-Frame-Options", "Content-Security-Policy",
                           "X-Content-Type-Options", "Referrer-Policy"):
                assert header in resp.headers, f"{header} missing"
            assert resp.headers["X-Frame-Options"] == "DENY"
        finally:
            await client.close()

    async def test_headers_present_on_auth_failure_too(self, web_env):
        """Error responses are responses; omitting headers there is a gap."""
        _, app = web_env
        client = await _client(app)
        try:
            resp = await client.post("/api/login", json={"password": "wrong"})
            assert resp.headers.get("X-Frame-Options") == "DENY"
        finally:
            await client.close()

    def test_csp_forbids_framing_and_external_script(self):
        csp = SECURITY_HEADERS["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in csp
        assert "script-src 'self'" in csp
        assert "default-src 'self'" in csp


class TestFinding5MiddlewareFailsClosed:
    """Finding 5 (Medium): anything outside /api was public by default."""

    async def test_unknown_non_api_path_requires_auth(self, web_env):
        _, app = web_env

        async def secret(request):
            return web.json_response({"secret": True})

        app.router.add_get("/internal/secret", secret)

        client = await _client(app)
        try:
            resp = await client.get("/internal/secret")
            assert resp.status == 401, (
                "a non-/api path was reachable without authentication -- "
                "the middleware is failing open again"
            )
        finally:
            await client.close()

    async def test_api_still_requires_auth(self, web_env):
        _, app = web_env
        client = await _client(app)
        try:
            assert (await client.get("/api/status")).status == 401
        finally:
            await client.close()

    async def test_public_paths_remain_reachable(self, web_env):
        _, app = web_env
        client = await _client(app)
        try:
            # Login must work unauthenticated or nobody can ever log in.
            resp = await client.post("/api/login", json={"password": "wrong"})
            assert resp.status in (401, 429)
        finally:
            await client.close()


class TestFinding9PendingHelloEviction:
    """Finding 9 (Low): the pending-HELLO table was cleared wholesale, letting
    an attacker discard legitimate in-flight handshakes with 64 cheap packets."""

    def test_table_is_bounded(self):
        manager = SessionManager(PASSWORD)
        for _ in range(MAX_PENDING_HELLO * 3):
            _hello(manager, ("10.9.9.9", 5000), crypto.new_client_id())
        assert len(manager._pending_hello) <= MAX_PENDING_HELLO

    def test_flood_does_not_evict_the_newest_handshake(self):
        """Under the old clear-everything behaviour a flood wiped every entry,
        including one about to complete."""
        manager = SessionManager(PASSWORD)

        for _ in range(MAX_PENDING_HELLO - 1):
            _hello(manager, ("10.9.9.9", 5000), crypto.new_client_id())

        victim = crypto.new_client_id()
        _hello(manager, ("10.9.9.9", 5000), victim)
        assert victim in manager._pending_hello

        # More traffic arrives; the newest entry must survive a few evictions.
        for _ in range(5):
            _hello(manager, ("10.9.9.9", 5000), crypto.new_client_id())

        assert victim in manager._pending_hello, (
            "a flood evicted a recent handshake -- eviction is not oldest-first"
        )


class TestVerifiedProperties:
    """Properties SECURITY.md records as sound. Guarded so they stay that way."""

    def test_argon2_runs_once_not_per_handshake(self):
        """If key derivation moved into the handshake, a flood would become CPU
        exhaustion. The master key must be derived at construction."""
        manager = SessionManager(PASSWORD)
        assert manager._master_key is not None
        assert len(manager._master_key) == crypto.KEY_SIZE

    def test_directions_cannot_share_a_nonce(self):
        session_key, _ = crypto.derive_session_keys(
            crypto.derive_master_key(PASSWORD, crypto.new_salt()),
            crypto.new_random(),
            crypto.new_random(),
        )
        client = crypto.SessionCrypto.for_client(session_key)
        server = crypto.SessionCrypto.for_server(session_key)
        assert client.encrypt(b"same") != server.encrypt(b"same")

    def test_encrypted_packet_cannot_masquerade_as_a_handshake(self):
        """The SESSION outer tag exists because the nonce counter's low byte
        would otherwise collide with HELLO/CHALLENGE/AUTH tags."""
        session_key, _ = crypto.derive_session_keys(
            crypto.derive_master_key(PASSWORD, crypto.new_salt()),
            crypto.new_random(),
            crypto.new_random(),
        )
        client = crypto.SessionCrypto.for_client(session_key)
        for _ in range(10):
            assert client.encrypt(b"x")[0] == PacketType.SESSION

    def test_auth_proof_is_bound_to_version(self):
        master = crypto.derive_master_key(PASSWORD, crypto.new_salt())
        _, proof_key = crypto.derive_session_keys(
            master, crypto.new_random(), crypto.new_random()
        )
        cid = crypto.new_client_id()
        proof = crypto.compute_auth_proof(proof_key, cid, PROTOCOL_VERSION)
        assert not crypto.verify_auth_proof(proof_key, cid, PROTOCOL_VERSION + 1, proof)

    def test_web_bind_defaults_to_loopback(self):
        """Finding 8: the admin surface should not be on every interface by
        default while its password is still cleartext."""
        assert server_config.ServerConfig().web_host == "127.0.0.1"


def _hello(manager: SessionManager, address, client_id: bytes):
    hello = (
        bytes([PacketType.HELLO])
        + PROTOCOL_VERSION.to_bytes(2, "little")
        + client_id
        + crypto.new_random()
    )
    return manager.handle_hello(hello, address)

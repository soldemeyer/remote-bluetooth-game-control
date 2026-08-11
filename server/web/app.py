"""Server web GUI: aiohttp app, REST endpoints, and a WebSocket status feed.

Runs on the asyncio thread, never on the datapath. The status feed is pushed at
a fixed 10 Hz rather than on every packet -- at 1000 packets/s per controller,
per-packet updates would swamp the browser and, worse, put GUI work in the way
of input.

Auth is the same shared password clients use, exchanged for a session cookie.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from common.protocol import ControlOp
from server.bt.profiles import available_profiles
from server.sessions import _RateLimiter

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: Status push cadence. Fast enough to feel live, slow enough to stay off the
#: datapath's back.
STATUS_INTERVAL_S = 0.1

COOKIE_NAME = "rbgc_session"
SESSION_TTL_S = 12 * 3600


class WebState:
    """Shared state for the web layer."""

    def __init__(
        self, config, sessions, router, datapath, adapter_manager=None, config_path=None
    ) -> None:
        self.config = config
        self.sessions = sessions
        self.router = router
        self.datapath = datapath
        self.adapter_manager = adapter_manager

        #: Where to write settings changed from the GUI. None keeps them in
        #: memory only, which is what the tests use.
        self.config_path = config_path

        #: token -> expiry. In-memory only; restarting the server logs
        #: operators out, which is the right trade for a headless appliance.
        self._tokens: dict[str, float] = {}
        self._websockets: set[web.WebSocketResponse] = set()

        #: Same limiter the UDP handshake uses -- 5 attempts per 60 s, then a
        #: 300 s lockout, keyed on source address.
        self.login_limiter = _RateLimiter()

        #: Set by create_runner once TLS is actually established. Drives the
        #: Secure cookie flag and the HSTS header, both of which are wrong to
        #: send over plain HTTP.
        self.tls_enabled = False

    # -- admin password ----------------------------------------------------

    @property
    def admin_password(self) -> str:
        """The password the web GUI accepts.

        Falls back to the shared client password when no separate admin
        password is set, so existing deployments keep working -- but the two
        are distinct concepts: a player who can connect a controller should not
        automatically be able to approve clients or re-pair adapters.
        """
        return getattr(self.config, "admin_password", "") or self.config.password

    # -- auth --------------------------------------------------------------

    def issue_token(self) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = time.monotonic() + SESSION_TTL_S
        return token

    def check_token(self, token: str | None) -> bool:
        if not token:
            return False
        expiry = self._tokens.get(token)
        if expiry is None:
            return False
        if time.monotonic() > expiry:
            del self._tokens[token]
            return False
        return True

    def revoke(self, token: str | None) -> None:
        if token:
            self._tokens.pop(token, None)

    def revoke_all(self) -> int:
        """Invalidate every browser session. Returns how many were dropped.

        Used when the admin password changes: existing tokens were issued
        against the old one, so leaving them valid would mean a rotated password
        did not actually lock anyone out.
        """
        count = len(self._tokens)
        self._tokens.clear()
        return count

    def check_password(self, password: str) -> bool:
        # Constant-time: this endpoint is reachable by anyone who can see the port.
        return hmac.compare_digest(password or "", self.admin_password)

    # -- status ------------------------------------------------------------

    def build_status(self) -> dict:
        """One complete snapshot for the GUI."""
        return {
            "server": {
                "name": self.config.server_name,
                "capacity": self.router.capacity,
                "max_clients": self.config.max_clients,
                "auto_approve": self.sessions.auto_approve,
                "rumble_enabled": self.datapath.rumble_enabled,
                "client_port": self.config.port,
                # Never send either password, not even masked: this snapshot
                # goes to every connected browser ten times a second.
                "enabled": getattr(self.datapath, "accepting", True),
                "discoverable": getattr(self.config, "discoverable", True),
                "internet_enabled": getattr(self.config, "internet_enabled", False),
                "has_password": bool(self.config.password),
                "has_admin_password": bool(getattr(self.config, "admin_password", "")),
                "broker": (
                    f"{self.config.broker_host}:{self.config.broker_port}"
                    if self.config.broker_host
                    else ""
                ),
            },
            "datapath": self.datapath.stats_snapshot(),
            "adapters": self.router.snapshot(),
            "hardware": (
                self.adapter_manager.snapshot() if self.adapter_manager else []
            ),
            "clients": self.sessions.snapshot(),
            "profiles": available_profiles(),
        }

    async def broadcast(self) -> None:
        """Push status to every connected browser."""
        if not self._websockets:
            return

        payload = json.dumps({"type": "status", "data": self.build_status()})
        dead = []

        for ws in self._websockets:
            try:
                await ws.send_str(payload)
            except (ConnectionResetError, RuntimeError):
                dead.append(ws)

        for ws in dead:
            self._websockets.discard(ws)

    def add_socket(self, ws: web.WebSocketResponse) -> None:
        self._websockets.add(ws)

    def remove_socket(self, ws: web.WebSocketResponse) -> None:
        self._websockets.discard(ws)


# --------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------

#: Paths reachable without a session. Everything else requires one.
#:
#: This is an allow-list on purpose. The previous version let through anything
#: not under /api, which meant a new endpoint added outside /api would be
#: public with no visible mistake at the point of addition -- fail-open. Now a
#: new path is protected by default and has to be listed here to be exposed.
PUBLIC_PATHS = frozenset(
    {"/api/login", "/", "/index.html", "/app.js", "/style.css", "/favicon.ico"}
)

#: Applied to every response. The UI loads no external resources, so a strict
#: CSP costs nothing and blocks injected script outright.
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",                     # no framing the approve/deny controls
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "Cache-Control": "no-store",
}


#: Only sent over TLS. Advertising HSTS on a plain-HTTP origin is ignored by
#: browsers, and pinning a host to HTTPS before a certificate exists would lock
#: the operator out of their own appliance.
HSTS_HEADER = ("Strict-Transport-Security", "max-age=31536000")


@web.middleware
async def security_headers_middleware(request: web.Request, handler):
    """Attach hardening headers to every response, including errors."""
    state: WebState = request.app["state"]
    headers = dict(SECURITY_HEADERS)
    if state.tls_enabled:
        headers[HSTS_HEADER[0]] = HSTS_HEADER[1]

    try:
        response = await handler(request)
    except web.HTTPException as exc:
        for key, value in headers.items():
            exc.headers.setdefault(key, value)
        raise

    for key, value in headers.items():
        response.headers.setdefault(key, value)
    return response


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path in PUBLIC_PATHS:
        return await handler(request)

    state: WebState = request.app["state"]
    if not state.check_token(request.cookies.get(COOKIE_NAME)):
        return web.json_response({"error": "Not authenticated"}, status=401)

    return await handler(request)


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


async def handle_index(request: web.Request) -> web.Response:
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return web.Response(text="Web UI assets are missing.", status=500)
    return web.FileResponse(index)


async def handle_login(request: web.Request) -> web.Response:
    state: WebState = request.app["state"]

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Malformed request"}, status=400)

    remote = request.remote or "unknown"

    # Rate limit *before* checking, and reuse the same limiter the UDP handshake
    # uses. A fixed sleep alone was not a control: it is per-request, so an
    # attacker opening connections in parallel got unlimited guesses per second.
    if not state.login_limiter.check(remote):
        log.warning("Web login from %s is rate limited", remote)
        return web.json_response(
            {"error": "Too many failed attempts. Try again later."}, status=429
        )

    if not state.check_password(body.get("password", "")):
        state.login_limiter.record_failure(remote)
        # Still delay, so a single attacker gains nothing from serial guessing
        # either. The limiter is what bounds parallel guessing.
        await asyncio.sleep(1.0)
        log.warning("Failed web login from %s", remote)
        return web.json_response({"error": "Incorrect password"}, status=401)

    state.login_limiter.record_success(remote)
    token = state.issue_token()
    response = web.json_response({"ok": True, "tls": state.tls_enabled})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL_S,
        httponly=True,
        samesite="Strict",
        # Only under TLS: setting Secure on a plain-HTTP deployment would make
        # browsers refuse to send the cookie at all, locking the operator out.
        secure=state.tls_enabled,
    )
    return response


async def handle_logout(request: web.Request) -> web.Response:
    state: WebState = request.app["state"]
    state.revoke(request.cookies.get(COOKIE_NAME))
    response = web.json_response({"ok": True})
    response.del_cookie(COOKIE_NAME)
    return response


async def handle_status(request: web.Request) -> web.Response:
    state: WebState = request.app["state"]
    return web.json_response(state.build_status())


async def handle_approve(request: web.Request) -> web.Response:
    state: WebState = request.app["state"]
    body = await request.json()
    client_id = body.get("client_id", "")

    if not state.sessions.approve(client_id):
        return web.json_response({"error": "Unknown client"}, status=404)

    session = state.sessions.by_client_id(client_id)
    if session is not None:
        state.datapath.send_control(
            session, ControlOp.APPROVED, {"capacity": state.router.capacity}
        )

    await state.broadcast()
    return web.json_response({"ok": True})


async def handle_deny(request: web.Request) -> web.Response:
    state: WebState = request.app["state"]
    body = await request.json()
    client_id = body.get("client_id", "")

    session = state.sessions.by_client_id(client_id)
    if session is not None:
        state.datapath.send_control(
            session, ControlOp.KICKED, {"reason": "Denied by the server operator"}
        )

    state.sessions.deny(client_id)
    state.router.unassign_client(client_id)
    state.sessions.drop(client_id)

    await state.broadcast()
    return web.json_response({"ok": True})


async def handle_assign(request: web.Request) -> web.Response:
    """Route a client's controller slot to a Bluetooth adapter."""
    state: WebState = request.app["state"]
    body = await request.json()

    bd_addr = body.get("bd_addr", "")
    client_id = body.get("client_id")
    slot = body.get("slot")

    if client_id is None or slot is None:
        state.router.unassign(bd_addr)
        await state.broadcast()
        return web.json_response({"ok": True, "message": f"{bd_addr} unassigned"})

    session = state.sessions.by_client_id(client_id)
    if session is None:
        return web.json_response({"error": "Unknown client"}, status=404)

    username = session.slot(int(slot)).username
    if not state.router.assign(bd_addr, client_id, int(slot), username):
        return web.json_response({"error": f"Could not assign {bd_addr}"}, status=400)

    await state.broadcast()
    return web.json_response({"ok": True})


async def handle_adapter_enable(request: web.Request) -> web.Response:
    """Enable or disable a physical adapter.

    Changing this changes server capacity, so connected clients are told
    immediately -- their GUIs grey out or re-enable slots without reconnecting.
    """
    state: WebState = request.app["state"]
    if state.adapter_manager is None:
        return web.json_response(
            {"error": "Adapter management is unavailable in mock mode"}, status=400
        )

    body = await request.json()
    ok, message = await state.adapter_manager.set_enabled(
        body.get("bd_addr", ""), bool(body.get("enabled", True))
    )

    if ok:
        state.datapath.broadcast_capacity()
        await state.broadcast()

    return web.json_response({"ok": ok, "message": message}, status=200 if ok else 400)


async def handle_adapter_profile(request: web.Request) -> web.Response:
    state: WebState = request.app["state"]
    if state.adapter_manager is None:
        return web.json_response(
            {"error": "Adapter management is unavailable in mock mode"}, status=400
        )

    body = await request.json()
    ok, message = await state.adapter_manager.set_profile(
        body.get("bd_addr", ""), body.get("profile", "generic")
    )

    if ok:
        await state.broadcast()

    return web.json_response({"ok": ok, "message": message}, status=200 if ok else 400)


async def handle_adapter_pair(request: web.Request) -> web.Response:
    """Put an adapter into connection mode so a console can find it."""
    state: WebState = request.app["state"]
    if state.adapter_manager is None:
        return web.json_response(
            {"error": "Pairing is unavailable in mock mode"}, status=400
        )

    body = await request.json()
    ok, message = await state.adapter_manager.set_pairable(
        body.get("bd_addr", ""),
        bool(body.get("pairable", True)),
        int(body.get("duration", 120)),
        # Defaults on: a host that has forgotten us generates a fresh link key,
        # and a leftover bond on our side then fails authentication with no
        # diagnostic beyond "couldn't connect" on the host.
        forget_bonds=bool(body.get("forget_bonds", True)),
    )

    return web.json_response({"ok": ok, "message": message}, status=200 if ok else 400)


async def handle_rescan(request: web.Request) -> web.Response:
    state: WebState = request.app["state"]
    if state.adapter_manager is None:
        return web.json_response({"ok": True, "message": "Mock mode; nothing to rescan"})

    await state.adapter_manager.rescan()
    state.datapath.broadcast_capacity()
    await state.broadcast()
    return web.json_response({"ok": True, "message": "Rescanned adapters"})


async def handle_settings(request: web.Request) -> web.Response:
    state: WebState = request.app["state"]
    body = await request.json()

    if "rumble_enabled" in body:
        state.datapath.rumble_enabled = bool(body["rumble_enabled"])
        state.config.rumble_enabled = state.datapath.rumble_enabled
        log.info(
            "Rumble forwarding %s",
            "enabled" if state.datapath.rumble_enabled else "disabled",
        )

    if "auto_approve" in body:
        state.sessions.auto_approve = bool(body["auto_approve"])
        state.config.auto_approve = state.sessions.auto_approve
        log.info("Auto-approve %s", "enabled" if state.sessions.auto_approve else "disabled")

    await state.broadcast()
    return web.json_response({"ok": True})


async def handle_server_state(request: web.Request) -> web.Response:
    """Turn client access on or off.

    Bluetooth is deliberately untouched: adapters stay registered and paired
    consoles stay connected, so an operator can stop accepting players without
    a console noticing.
    """
    state: WebState = request.app["state"]
    body = await request.json()

    if "enabled" not in body:
        return web.json_response({"error": "enabled is required"}, status=400)

    enabled = bool(body["enabled"])
    if enabled and not state.config.password:
        return web.json_response(
            {"error": "Set a client password before accepting connections."},
            status=400,
        )

    dropped = state.datapath.set_accepting(enabled)
    state.config.server_enabled = enabled
    _persist(state)

    if enabled:
        message = "Server is accepting client connections."
    else:
        message = "Server stopped accepting clients."
        if dropped:
            message += f" {dropped} client(s) disconnected."
        message += " Bluetooth controllers stay connected."

    await state.broadcast()
    return web.json_response({"ok": True, "enabled": enabled, "message": message})


async def handle_server_identity(request: web.Request) -> web.Response:
    """Set the server name and passwords.

    Changing the client password drops every session: the session key is derived
    from it, so an existing session is by definition using the old one and
    cannot silently continue.
    """
    state: WebState = request.app["state"]
    body = await request.json()

    changed: list[str] = []
    reauth = False

    name = body.get("name")
    if isinstance(name, str) and name.strip() and name.strip() != state.config.server_name:
        state.config.server_name = name.strip()[:64]
        changed.append("name")

    password = body.get("password")
    if isinstance(password, str) and password:
        problem = _password_problem(password)
        if problem:
            return web.json_response({"error": problem}, status=400)

        state.config.password = password
        state.sessions.set_password(password)
        dropped = state.datapath.set_accepting(False)
        state.datapath.set_accepting(state.config.server_enabled)
        changed.append(f"client password ({dropped} client(s) disconnected)")

    admin = body.get("admin_password")
    if isinstance(admin, str) and admin:
        problem = _password_problem(admin)
        if problem:
            return web.json_response({"error": problem}, status=400)

        state.config.admin_password = admin
        # Every existing browser session was authenticated against the old
        # password, so none of them may survive the change.
        state.revoke_all()
        changed.append("admin password")
        reauth = True

    if not changed:
        return web.json_response({"ok": True, "message": "Nothing to change."})

    _persist(state)
    log.info("Server identity updated: %s", ", ".join(changed))

    await state.broadcast()
    return web.json_response({
        "ok": True,
        "reauth": reauth,
        "message": "Updated " + ", ".join(changed) + ".",
    })


async def handle_server_visibility(request: web.Request) -> web.Response:
    """Broadcast vs hidden, and Internet reachability."""
    state: WebState = request.app["state"]
    body = await request.json()

    if "discoverable" in body:
        state.config.discoverable = bool(body["discoverable"])
    if "internet_enabled" in body:
        state.config.internet_enabled = bool(body["internet_enabled"])

    broker = body.get("broker")
    if isinstance(broker, str):
        host, port, problem = _parse_broker(broker)
        if problem:
            return web.json_response({"error": problem}, status=400)
        state.config.broker_host = host
        state.config.broker_port = port

    # The rendezvous client advertises a name only when we are discoverable;
    # sending no name is exactly what keeps a hidden server out of listings.
    rendezvous = getattr(state.datapath, "_rendezvous", None)
    if rendezvous is not None and hasattr(rendezvous, "set_public_name"):
        rendezvous.set_public_name(
            state.config.server_name
            if (state.config.discoverable and state.config.internet_enabled)
            else ""
        )

    _persist(state)
    log.info(
        "Visibility: %s on the LAN, Internet %s",
        "broadcast" if state.config.discoverable else "hidden",
        "enabled" if state.config.internet_enabled else "disabled",
    )

    await state.broadcast()
    return web.json_response({
        "ok": True,
        "message": (
            f"{'Broadcasting' if state.config.discoverable else 'Hidden'} on this network; "
            f"Internet {'enabled' if state.config.internet_enabled else 'disabled'}."
        ),
    })


def _password_problem(password: str) -> str | None:
    """Reject passwords too weak to be worth the Argon2id around them."""
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if len(password) > 128:
        return "Password must be at most 128 characters."
    return None


def _parse_broker(value: str) -> tuple[str, int, str | None]:
    """Parse "host:port". Empty clears the broker."""
    value = value.strip()
    if not value:
        return "", 47900, None

    host, _, port_text = value.rpartition(":")
    if not host:
        return value, 47900, None
    try:
        port = int(port_text)
    except ValueError:
        return "", 0, f"Not a valid broker port: {port_text!r}"
    if not 1 <= port <= 65535:
        return "", 0, f"Broker port out of range: {port}"
    return host, port, None


def _persist(state: WebState) -> None:
    """Best effort: a failed write must not undo a change already applied."""
    path = getattr(state, "config_path", None)
    if path is None:
        return
    try:
        from server import config as server_config

        server_config.save(state.config, path)
    except Exception:
        log.warning("Could not persist server config", exc_info=True)


async def handle_websocket(request: web.Request) -> web.WebSocketResponse:
    state: WebState = request.app["state"]

    if not state.check_token(request.cookies.get(COOKIE_NAME)):
        return web.json_response({"error": "Not authenticated"}, status=401)

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    state.add_socket(ws)

    try:
        await ws.send_str(json.dumps({"type": "status", "data": state.build_status()}))
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        state.remove_socket(ws)

    return ws


async def _status_pusher(app: web.Application) -> None:
    """Push status at a fixed rate, independent of packet traffic."""
    state: WebState = app["state"]
    try:
        while True:
            await asyncio.sleep(STATUS_INTERVAL_S)
            await state.broadcast()
    except asyncio.CancelledError:
        pass


async def _start_background(app: web.Application) -> None:
    app["status_task"] = asyncio.create_task(_status_pusher(app))


async def _stop_background(app: web.Application) -> None:
    task = app.get("status_task")
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def create_app(
    config, sessions, router, datapath, adapter_manager=None, config_path=None
) -> web.Application:
    app = web.Application(
        middlewares=[security_headers_middleware, auth_middleware]
    )
    app["state"] = WebState(config, sessions, router, datapath, adapter_manager, config_path)

    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_websocket)

    app.router.add_post("/api/login", handle_login)
    app.router.add_post("/api/logout", handle_logout)
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/approve", handle_approve)
    app.router.add_post("/api/deny", handle_deny)
    app.router.add_post("/api/assign", handle_assign)
    app.router.add_post("/api/adapter/enable", handle_adapter_enable)
    app.router.add_post("/api/adapter/profile", handle_adapter_profile)
    app.router.add_post("/api/adapter/pair", handle_adapter_pair)
    app.router.add_post("/api/rescan", handle_rescan)
    app.router.add_post("/api/settings", handle_settings)
    app.router.add_post("/api/server/state", handle_server_state)
    app.router.add_post("/api/server/identity", handle_server_identity)
    app.router.add_post("/api/server/visibility", handle_server_visibility)

    if STATIC_DIR.exists():
        app.router.add_static("/", STATIC_DIR, name="static")

    app.on_startup.append(_start_background)
    app.on_cleanup.append(_stop_background)

    return app


async def create_runner(
    config, sessions, router, datapath, adapter_manager=None, config_path=None
):
    """Start the web GUI, over TLS unless explicitly disabled.

    Returns the runner for later cleanup.
    """
    app = create_app(config, sessions, router, datapath, adapter_manager, config_path)
    state: WebState = app["state"]

    ssl_context = None
    fingerprint = ""

    if getattr(config, "tls_enabled", True):
        ssl_context, fingerprint = _setup_tls(config)
        state.tls_enabled = ssl_context is not None

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()

    site = web.TCPSite(runner, config.web_host, config.web_port, ssl_context=ssl_context)
    await site.start()

    scheme = "https" if ssl_context else "http"
    log.info("Web GUI at %s://%s:%d", scheme, config.web_host, config.web_port)

    if ssl_context is None:
        log.warning(
            "Web GUI is running WITHOUT encryption. The admin password will cross "
            "the network in cleartext. Keep it on localhost and reach it through an "
            "SSH tunnel:  ssh -L %d:127.0.0.1:%d <user>@<host>",
            config.web_port,
            config.web_port,
        )
    elif fingerprint:
        # The operator has to click through a browser warning once. Showing the
        # fingerprint is what lets them confirm the certificate is ours rather
        # than an interceptor's -- without this check, self-signed TLS stops
        # only passive eavesdropping.
        log.info("Certificate SHA-256 fingerprint: %s", fingerprint)

    return runner


def _setup_tls(config) -> tuple[object | None, str]:
    """Prepare an SSL context. Returns ``(context or None, fingerprint)``.

    Never fatal: a server that refuses to start because of a certificate
    problem is worse than one that starts loudly unencrypted on loopback.
    """
    try:
        from server.web import tls
    except ImportError as exc:
        log.error("TLS support unavailable (%s); falling back to HTTP", exc)
        return None, ""

    if not tls.is_available():
        log.error(
            "TLS needs the 'cryptography' package, which is not installed.\n"
            "  Install it:  pip install -e \".[server]\"\n"
            "  Falling back to HTTP for now."
        )
        return None, ""

    from server import config as server_config

    try:
        cert, key, fingerprint = tls.ensure_certificate(
            server_config.config_dir(),
            Path(config.tls_cert) if getattr(config, "tls_cert", "") else None,
            Path(config.tls_key) if getattr(config, "tls_key", "") else None,
        )
        return tls.build_ssl_context(cert, key), fingerprint
    except tls.TLSError as exc:
        log.error("Could not enable TLS (%s); falling back to HTTP", exc)
        return None, ""

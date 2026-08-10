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

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: Status push cadence. Fast enough to feel live, slow enough to stay off the
#: datapath's back.
STATUS_INTERVAL_S = 0.1

COOKIE_NAME = "rbgc_session"
SESSION_TTL_S = 12 * 3600


class WebState:
    """Shared state for the web layer."""

    def __init__(self, config, sessions, router, datapath, adapter_manager=None) -> None:
        self.config = config
        self.sessions = sessions
        self.router = router
        self.datapath = datapath
        self.adapter_manager = adapter_manager

        #: token -> expiry. In-memory only; restarting the server logs
        #: operators out, which is the right trade for a headless appliance.
        self._tokens: dict[str, float] = {}
        self._websockets: set[web.WebSocketResponse] = set()

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

    def check_password(self, password: str) -> bool:
        # Constant-time: this endpoint is reachable by anyone who can see the port.
        return hmac.compare_digest(password or "", self.config.password)

    # -- status ------------------------------------------------------------

    def build_status(self) -> dict:
        """One complete snapshot for the GUI."""
        return {
            "server": {
                "name": self.config.server_name,
                "capacity": self.router.capacity,
                "max_clients": self.config.max_clients,
                "auto_approve": self.sessions.auto_approve,
                "client_port": self.config.port,
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

PUBLIC_PATHS = {"/api/login", "/", "/index.html", "/app.js", "/style.css"}


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path in PUBLIC_PATHS or not request.path.startswith("/api"):
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

    if not state.check_password(body.get("password", "")):
        # Deliberate delay: this endpoint is reachable by anyone who can see
        # the port, so slow down online guessing.
        await asyncio.sleep(1.0)
        log.warning("Failed web login from %s", request.remote)
        return web.json_response({"error": "Incorrect password"}, status=401)

    token = state.issue_token()
    response = web.json_response({"ok": True})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL_S,
        httponly=True,
        samesite="Strict",
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

    if "auto_approve" in body:
        state.sessions.auto_approve = bool(body["auto_approve"])
        state.config.auto_approve = state.sessions.auto_approve
        log.info("Auto-approve %s", "enabled" if state.sessions.auto_approve else "disabled")

    await state.broadcast()
    return web.json_response({"ok": True})


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


def create_app(config, sessions, router, datapath, adapter_manager=None) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["state"] = WebState(config, sessions, router, datapath, adapter_manager)

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

    if STATIC_DIR.exists():
        app.router.add_static("/", STATIC_DIR, name="static")

    app.on_startup.append(_start_background)
    app.on_cleanup.append(_stop_background)

    return app


async def create_runner(config, sessions, router, datapath, adapter_manager=None):
    """Start the web GUI. Returns the runner for later cleanup."""
    app = create_app(config, sessions, router, datapath, adapter_manager)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()

    site = web.TCPSite(runner, config.web_host, config.web_port)
    await site.start()

    log.info("Web GUI at http://%s:%d", config.web_host, config.web_port)
    return runner

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
from common.video import VideoSettings
from server import video as video_registry
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
        self,
        config,
        sessions,
        router,
        datapath,
        adapter_manager=None,
        config_path=None,
        video_registry=None,
        embedded_video=None,
        video_link=None,
    ) -> None:
        self.config = config
        self.sessions = sessions
        self.router = router
        self.datapath = datapath
        self.adapter_manager = adapter_manager

        #: Video control plane. None when the server was built without one,
        #: which the handlers treat as "video is unavailable" rather than
        #: failing -- the same shape as adapter_manager in mock mode.
        self.video = video_registry
        self.embedded_video = embedded_video

        #: Our outbound control link to the video server. None when video is
        #: off or the server was built without it.
        self.video_link = video_link

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
                "lan_enabled": getattr(self.datapath, "accepting_lan", True),
                "internet_enabled": getattr(self.datapath, "accepting_internet", False),
                "lan_discoverable": getattr(self.config, "lan_discoverable", True),
                "internet_discoverable": getattr(
                    self.config, "internet_discoverable", True
                ),
                "broker_ready": getattr(self.datapath, "_rendezvous", None) is not None,
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
            "video": self._video_status(),
        }

    def _video_status(self) -> dict | None:
        """The video block, including how our link to the source is doing.

        The address is sent; the password never is -- this snapshot reaches
        every open browser ten times a second. ``has_password`` is enough for
        the UI to show whether one is set.
        """
        if self.video is None:
            return None

        snapshot = self.video.snapshot()
        snapshot["connection"] = {
            "host": getattr(self.config, "video_host", ""),
            "port": getattr(self.config, "video_port", 0),
            "has_password": bool(getattr(self.config, "video_password", "")),
            "link": self.video_link.snapshot() if self.video_link is not None else None,
        }
        return snapshot

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
        # blob: covers the video preview, which is fetched with credentials and
        # shown through an object URL -- swapping the <img> src to a blob keeps
        # the last good frame on screen when a fetch returns 204, where a plain
        # URL would flash a broken image.
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data: blob:; connect-src 'self' ws: wss:; "
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
        # Approval is when they become entitled to watch, so it is also when
        # they need the endpoint and a ticket. They were told "no video" while
        # pending, and nothing else would revisit that.
        state.datapath.broadcast_video_source()
        _push_video_config(state)

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

    # Withdraw the right to watch as well. Denying a client that is mid-stream
    # otherwise takes their controller away and leaves the picture running,
    # which is not what the operator pressed the button for. The video server
    # learns of it on the next config push and drops them.
    if state.video is not None and state.video.revoke_ticket(client_id):
        _push_video_config(state)

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
        # Persisted here rather than left to whatever the operator changes
        # next: it is an ordinary preference, and reverting on restart with no
        # explanation is exactly the kind of thing nobody thinks to re-check.
        _persist(state)

    if "auto_approve" in body:
        # Runtime only, and deliberately so: a server that silently resumed
        # auto-approving strangers after a reboot -- because someone enabled it
        # for one evening months ago -- is a security posture nobody chose.
        # The persisted value is the *startup* default, set from the config
        # file or --auto-approve.
        #
        # Note what is NOT here: mirroring into state.config. It used to, and
        # since this handler never saves, the value then leaked to disk
        # whenever some *unrelated* change did call _persist. So it persisted
        # or not depending on whether the operator later touched a toggle --
        # which is worse than either answer, and made "runtime only" a claim
        # the code did not actually keep.
        state.sessions.auto_approve = bool(body["auto_approve"])
        log.info("Auto-approve %s", "enabled" if state.sessions.auto_approve else "disabled")

    await state.broadcast()
    return web.json_response({"ok": True})


async def handle_video_mode(request: web.Request) -> web.Response:
    """Switch video off, to an external source, or to an embedded one."""
    state: WebState = request.app["state"]
    if state.video is None:
        return web.json_response({"error": "video is not available"}, status=404)

    body = await request.json()
    mode = str(body.get("mode", ""))
    if mode not in video_registry.MODES:
        return web.json_response(
            {"error": f"mode must be one of {', '.join(video_registry.MODES)}"}, status=400
        )

    if mode != "off" and not state.config.password:
        return web.json_response(
            {"error": "Set a client password before enabling video."}, status=400
        )

    # Deliberately no early return when the mode is unchanged. Selecting the
    # current mode should *reconcile* -- start whatever is missing -- because
    # that is what an operator is doing when they press it again after
    # something looked wrong. Everything below is idempotent.
    state.video.mode = mode
    state.config.video_mode = mode

    # The loopback allowance exists only to let a child we started reach us,
    # so it must track the mode exactly.
    state.datapath.allow_loopback_video = mode == "embedded"

    message = await _apply_video_mode(state, mode)
    _persist(state)
    state.datapath.broadcast_video_source()
    await state.broadcast()
    return web.json_response({"ok": True, "message": message})


def _ensure_video_link(state: WebState) -> None:
    """Make sure a link to the video server exists and is running.

    The link used to be built only at startup, and only when the mode was
    *already* on. A server that booted with video off therefore had none, and
    turning video on in the GUI created nothing: the address and password were
    saved, "Connecting to ..." was reported, and absolutely nothing dialled
    out. It looked like the video server was refusing us. Only a restart fixed
    it, which is not a thing anyone would think to try.
    """
    if state.video_link is not None or state.video is None:
        return

    from server.videolink import VideoLink

    state.video_link = VideoLink(state.video, state.datapath, state.config)
    state.video_link.start()


def _stop_video_link(state: WebState) -> None:
    link, state.video_link = state.video_link, None
    if link is not None:
        link.stop()


async def _apply_video_mode(state: WebState, mode: str) -> str:
    """Start or stop the subprocess and the link to match the new mode."""
    embedded = state.embedded_video

    if mode == "embedded":
        if embedded is None:
            try:
                from server.videohost import EmbeddedVideoServer
            except ImportError as exc:
                return f"Embedded video unavailable: {exc}"
            if not state.config.video_password:
                # Our own child on this machine: there is nobody to agree a
                # password with, so inventing one beats asking the operator to.
                state.config.video_password = secrets.token_urlsafe(24)
            state.config.video_host = "127.0.0.1"
            embedded = EmbeddedVideoServer(state.config, state.video)
            state.embedded_video = embedded
        await embedded.start()
        _ensure_video_link(state)
        return "Embedded video server starting"

    if embedded is not None:
        await embedded.stop()
        state.embedded_video = None

    if mode == "external":
        _ensure_video_link(state)
        return "Waiting for a video server address and password"

    _stop_video_link(state)
    return "Video off"


async def handle_video_connection(request: web.Request) -> web.Response:
    """Point the server at a video server, and give it the password to use.

    The address and password are what the operator was asked for; everything
    else about the video server is configured from here afterwards, over the
    link this establishes.
    """
    state: WebState = request.app["state"]
    if state.video is None:
        return web.json_response({"error": "video is not available"}, status=404)

    body = await request.json()
    if not isinstance(body, dict):
        return web.json_response({"error": "expected an object"}, status=400)

    if "host" in body:
        host = str(body.get("host", "")).strip()
        port = body.get("port")
        # Accept "host:port" typed into one box, because people will.
        if ":" in host and port is None:
            host, _, typed = host.rpartition(":")
            if typed.isdigit():
                port = int(typed)
        state.config.video_host = host[:128]
        if isinstance(port, int) and 1 <= port <= 65535:
            state.config.video_port = port

    if "password" in body:
        password = str(body.get("password", ""))
        if password and len(password) < 6:
            return web.json_response(
                {"error": "The video server's password must be at least 6 characters."},
                status=400,
            )
        state.config.video_password = password

    _persist(state)

    # Build the link if there is not one yet: the operator may be entering an
    # address on a server that booted with video off.
    if state.video.mode != video_registry.MODE_OFF:
        _ensure_video_link(state)

    # Reconnect with the new details rather than waiting for the current
    # attempt to time out -- the operator has just told us the old ones were
    # wrong, and watching it retry those would be baffling.
    link = state.video_link
    if link is not None:
        link.reconnect()

    await state.broadcast()

    if state.video.mode == video_registry.MODE_OFF:
        return web.json_response(
            {
                "ok": True,
                "message": (
                    "Saved, but video is switched off — choose a source above to connect"
                ),
            }
        )
    if not state.config.video_host:
        return web.json_response({"ok": True, "message": "Video server address cleared"})
    if not state.config.video_password:
        return web.json_response(
            {"ok": True, "message": "Address saved — the video server's password is still needed"}
        )
    return web.json_response(
        {
            "ok": True,
            "message": (
                f"Connecting to {state.config.video_host}:{state.config.video_port}"
            ),
        }
    )


async def handle_video_detect(request: web.Request) -> web.Response:
    """Look for video servers on the LAN.

    Returns what answered, for the operator to choose from. Finding nothing is
    a normal answer, not an error: a video server on another subnet, or one
    with discovery switched off, has to be typed in by hand.
    """
    state: WebState = request.app["state"]
    if state.video is None:
        return web.json_response({"error": "video is not available"}, status=404)

    try:
        from videoserver.discovery import discover_video_servers
    except ImportError as exc:
        return web.json_response(
            {"error": f"Discovery unavailable: {exc}"}, status=503
        )

    try:
        found = await discover_video_servers(timeout=1.5)
    except Exception:
        log.debug("Video discovery failed", exc_info=True)
        found = []

    return web.json_response(
        {
            "ok": True,
            "servers": found,
            "message": (
                f"Found {len(found)} video server(s)"
                if found
                else "No video servers answered — enter the address by hand"
            ),
        }
    )


async def handle_video_config(request: web.Request) -> web.Response:
    """Apply capture and encode settings to whichever source is attached."""
    state: WebState = request.app["state"]
    if state.video is None:
        return web.json_response({"error": "video is not available"}, status=404)

    body = await request.json()
    if not isinstance(body, dict):
        return web.json_response({"error": "expected an object"}, status=400)

    merged = {**state.video.settings.to_dict(), **body}
    merged.pop("probe_devices", None)     # a one-shot action, not a setting

    settings = VideoSettings.from_dict(merged)
    seq = state.video.set_config(settings)
    applied = state.video.settings.to_dict()
    state.config.video_config = applied

    # Push straight away rather than waiting for the maintenance tick to
    # notice: the operator just moved a slider and expects to see it take.
    _push_video_config(state)

    _persist(state)
    await state.broadcast()

    message = f"Settings applied (seq {seq})"
    if _was_reduced(merged, applied):
        # Say so rather than silently serving something else -- an operator who
        # asks for 1080p60 and gets 720p30 with no explanation reasonably
        # concludes the setting is broken.
        message += " — limited to what this device can encode"
    return web.json_response({"ok": True, "message": message})


def _was_reduced(requested: dict, applied: dict) -> bool:
    """True if clamping cut anything the operator asked for."""
    keys = ("width", "height", "fps", "bitrate_kbps")
    for key in keys:
        try:
            if int(requested.get(key, 0)) > int(applied.get(key, 0)):
                return True
        except (TypeError, ValueError):
            continue
    return False


async def handle_video_probe(request: web.Request) -> web.Response:
    """Ask the attached source to re-enumerate its capture devices."""
    state: WebState = request.app["state"]
    if state.video is None:
        return web.json_response({"error": "video is not available"}, status=404)
    if not state.video.has_source:
        return web.json_response({"error": "no video source is connected"}, status=400)

    state.video.request_probe()
    _push_video_config(state)
    await state.broadcast()
    return web.json_response({"ok": True, "message": "Scanning for capture devices"})


async def handle_video_preview(request: web.Request) -> web.Response:
    """The newest preview frame, as a plain JPEG.

    204 rather than a placeholder when there is nothing fresh: the browser can
    then keep showing the last good frame instead of flashing a broken image
    every time one is missed.
    """
    state: WebState = request.app["state"]
    if state.video is None:
        return web.Response(status=204)

    frame = state.video.preview()
    if not frame:
        return web.Response(status=204)

    return web.Response(
        body=frame,
        content_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


async def handle_server_state(request: web.Request) -> web.Response:
    """Turn client access on or off.

    Bluetooth is deliberately untouched: adapters stay registered and paired
    consoles stay connected, so an operator can stop accepting players without
    a console noticing.
    """
    state: WebState = request.app["state"]
    body = await request.json()

    lan = body.get("lan")
    internet = body.get("internet")
    if lan is None and internet is None:
        return web.json_response(
            {"error": "lan and/or internet is required"}, status=400
        )

    if (lan or internet) and not state.config.password:
        return web.json_response(
            {"error": "Set a client password before accepting connections."},
            status=400,
        )

    lan = None if lan is None else bool(lan)
    internet = None if internet is None else bool(internet)

    dropped = state.datapath.set_accepting(lan=lan, internet=internet)

    if lan is not None:
        state.config.lan_enabled = lan
    if internet is not None:
        state.config.internet_enabled = internet
        # The rendezvous client only exists when Internet was enabled at
        # startup; say so rather than silently doing nothing.
        if internet and getattr(state.datapath, "_rendezvous", None) is None:
            _persist(state)
            await state.broadcast()
            return web.json_response({
                "ok": True,
                "message": (
                    "Internet connections enabled, but no rendezvous broker is "
                    "configured or reachable. Set a broker and room code, then "
                    "restart the server."
                ),
            })

    _persist(state)

    parts = []
    if lan is not None:
        parts.append(f"LAN connections {'on' if lan else 'off'}")
    if internet is not None:
        parts.append(f"Internet connections {'on' if internet else 'off'}")
    message = ", ".join(parts) + "."
    if dropped:
        message += f" {dropped} client(s) disconnected."
    message += " Bluetooth controllers stay connected."

    await state.broadcast()
    return web.json_response({"ok": True, "message": message})


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
        # Release the adapters first: set_password drops the sessions, and a
        # channel still pointing at a departed client would leave the console
        # holding whatever state that player left behind.
        for session in state.sessions.all_sessions():
            state.router.unassign_client(session.client_id)
        dropped = state.sessions.set_password(password)
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

    if "lan_discoverable" in body:
        state.config.lan_discoverable = bool(body["lan_discoverable"])
    if "internet_discoverable" in body:
        state.config.internet_discoverable = bool(body["internet_discoverable"])

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
        # Sending no name is exactly what keeps a hidden server out of listings.
        rendezvous.set_public_name(
            state.config.server_name if state.config.internet_discoverable else ""
        )

    _persist(state)
    log.info(
        "Visibility: LAN %s, Internet %s",
        "visible" if state.config.lan_discoverable else "hidden",
        "listed" if state.config.internet_discoverable else "hidden",
    )

    await state.broadcast()
    return web.json_response({
        "ok": True,
        "message": (
            f"On this network: {'visible' if state.config.lan_discoverable else 'hidden'}. "
            f"Over the Internet: {'listed' if state.config.internet_discoverable else 'hidden'}."
        ),
    })


def _push_video_config(state: WebState) -> None:
    """Ask the video link to send the configuration now.

    Only the link may do this. `needs_config_push()` records that a push
    happened, so any second caller that consumes it without sending swallows
    the link's next attempt -- which is exactly how a freshly minted ticket
    used to go missing.
    """
    if state.video_link is not None:
        state.video_link.request_config_push()


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
    config,
    sessions,
    router,
    datapath,
    adapter_manager=None,
    config_path=None,
    video_registry=None,
    embedded_video=None,
    video_link=None,
) -> web.Application:
    app = web.Application(
        middlewares=[security_headers_middleware, auth_middleware]
    )
    app["state"] = WebState(
        config,
        sessions,
        router,
        datapath,
        adapter_manager,
        config_path,
        video_registry=video_registry,
        embedded_video=embedded_video,
        video_link=video_link,
    )

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
    app.router.add_post("/api/video/mode", handle_video_mode)
    app.router.add_post("/api/video/connection", handle_video_connection)
    app.router.add_post("/api/video/detect", handle_video_detect)
    app.router.add_post("/api/video/config", handle_video_config)
    app.router.add_post("/api/video/probe", handle_video_probe)
    app.router.add_get("/api/video/preview", handle_video_preview)

    if STATIC_DIR.exists():
        app.router.add_static("/", STATIC_DIR, name="static")

    app.on_startup.append(_start_background)
    app.on_cleanup.append(_stop_background)

    return app


async def create_runner(
    config,
    sessions,
    router,
    datapath,
    adapter_manager=None,
    config_path=None,
    video_registry=None,
    embedded_video=None,
    video_link=None,
):
    """Start the web GUI, over TLS unless explicitly disabled.

    Returns the runner for later cleanup.
    """
    app = create_app(
        config,
        sessions,
        router,
        datapath,
        adapter_manager,
        config_path,
        video_registry=video_registry,
        embedded_video=embedded_video,
        video_link=video_link,
    )
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

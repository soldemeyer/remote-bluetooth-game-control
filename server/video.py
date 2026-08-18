"""Tracking the video source, and telling clients where it is.

A sibling of :class:`server.router.Router`, not an extension of it. The router
maps controllers onto Bluetooth adapters and is capped at four by the radio
hardware; video has nothing to do with any of that. Bolting one onto the other
would tie two unrelated lifetimes together.

The Bluetooth server is a **control plane only** for video. Media never passes
through here -- clients connect straight to the source. What this holds is the
handful of facts needed to introduce them: where the source is, what it is
doing, and what the operator wants it to do.

Threading: written from the datapath thread (control messages arrive there) and
from the asyncio thread (the web GUI), read at 10 Hz by the status push. One
lock covers all of it; nothing here is on a hot path.
"""

from __future__ import annotations

import logging
import secrets
import threading

from common.timing import now_ns
from common.video import DEFAULT_VIDEO_PORT, FrameAssembler, MediaCodec, VideoSettings

log = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_EXTERNAL = "external"
MODE_EMBEDDED = "embedded"
MODES = (MODE_OFF, MODE_EXTERNAL, MODE_EMBEDDED)

#: A source that has not reported in this long is treated as gone even if its
#: session is technically still alive.
STATUS_STALE_NS = 5_000_000_000

#: How often to re-push a configuration the source has not acknowledged. The
#: server -> client direction has no retransmit, so this is the retry.
CONFIG_REPUSH_NS = 2_000_000_000

#: Preview frames older than this are not worth showing; the web GUI gets a
#: 204 instead of a stale picture presented as current.
PREVIEW_STALE_NS = 3_000_000_000

#: How long a preview request keeps the preview flowing. Comfortably longer
#: than the browser's poll interval, so an open panel never stutters, and short
#: enough that a closed one stops costing the datapath promptly.
PREVIEW_DEMAND_NS = 5_000_000_000

#: A preview JPEG is a few tens of kilobytes. The cap is protection against a
#: hostile slice count, not a working limit.
#: Kept in step with `videoserver.preview.MAX_PREVIEW_BYTES` -- the reassembler
#: must accept anything the source considers acceptable to send, or a large
#: preview frame is silently dropped here after crossing the network.
MAX_PREVIEW_BYTES = 1024 * 1024

#: Embedded mode runs on the Bluetooth server itself -- a Raspberry Pi, where
#: the Pi 5 has no hardware H.264 encoder at all and must encode in software.
#: These are what a Pi can actually sustain while still serving input.
EMBEDDED_MAX_WIDTH = 1280
EMBEDDED_MAX_HEIGHT = 720
EMBEDDED_MAX_FPS = 30
EMBEDDED_MAX_BITRATE_KBPS = 6000


class VideoRegistry:
    """What the server knows about video."""

    def __init__(
        self,
        mode: str = MODE_OFF,
        settings: VideoSettings | None = None,
        configured: bool | None = None,
    ) -> None:
        self._lock = threading.Lock()

        self.mode = mode if mode in MODES else MODE_OFF
        self._settings = (settings or VideoSettings()).clamped()

        #: Has anyone actually *chosen* these settings, or are they defaults?
        #:
        #: A video server is configured in front of the machine it captures on,
        #: and it may well be running before this server ever hears of it.
        #: Pushing our defaults at it the moment we connect would silently undo
        #: that -- the operator sets 1080p on the video server, connects, and
        #: watches it drop to whatever this server happened to have. So until
        #: the operator configures video *here*, we adopt what the source is
        #: already doing instead of overwriting it.
        #:
        #: Defaults to "settings were handed to us on purpose" so a caller that
        #: passes them keeps pushing them; the server overrides it with whether
        #: anything was actually saved to disk.
        self._configured = (settings is not None) if configured is None else configured
        self._cfg_seq = 1
        self._applied_seq = 0
        self._last_pushed_ns = 0

        self._source_client_id: str | None = None
        self._source_address: tuple[str, int] | None = None
        self._media_port = DEFAULT_VIDEO_PORT
        self._lan_host = ""
        self._status: dict = {}
        self._status_ns = 0
        self._devices: list[dict] = []

        #: Broker details handed to clients so they can reach a source that is
        #: not directly routable. Set from the server's own config.
        self.broker = ""
        self.room = ""

        #: Substituted for the source address in embedded mode, where the
        #: session comes from 127.0.0.1 and that is useless to a client.
        self.advertise_host = ""

        self._preview = FrameAssembler(max_frame_size=MAX_PREVIEW_BYTES)
        self._preview_data: bytes | None = None
        self._preview_ns = 0
        #: When the web GUI last asked for a preview frame. Drives
        #: `preview_wanted`, and through it whether the source sends any.
        self._preview_asked_ns = 0
        #: The `preview_enabled` we last told the source, so a change in demand
        #: can trigger a push on its own.
        self._preview_pushed: bool | None = None

        #: client_id -> viewing ticket, for clients the operator approved. The
        #: source refuses anyone without a current one, which is what makes
        #: "denied" mean denied rather than "denied a controller, but do watch".
        self._tickets: dict[str, str] = {}

        #: The tickets the source has actually acknowledged. A client is told
        #: video is available only once *its own* ticket is in here.
        #:
        #: Without that the advert races the configuration carrying its ticket:
        #: the client acts on the advert the instant it arrives, the source has
        #: never heard of that ticket, and the handshake is refused with "the
        #: operator denied this connection" -- alarming, and untrue.
        #:
        #: Tracked as a set rather than a single in-sync flag on purpose. A
        #: newcomer's ticket bumps the configuration, and a global flag would
        #: make video briefly unavailable for *everyone* -- which the client
        #: reacts to by tearing down a perfectly good stream.
        self._acked_tickets: set[str] = set()

        #: Set by the supervisor in embedded mode.
        self.embedded_state: dict = {}

    # -- source lifecycle --------------------------------------------------

    def attach_source_endpoint(self, host: str, port: int) -> None:
        """Note that our outbound link to a video server is up.

        The counterpart to :meth:`attach_source` for the direction the system
        actually uses now: we connect *to* the video server, so there is no
        inbound session to hang this off. ``client_id`` is a fixed marker
        because there is only ever one such link.
        """
        with self._lock:
            self._source_client_id = "video-link"
            self._source_address = (host, port)
            self._media_port = port
            self._status = {}
            self._status_ns = 0
            self._applied_seq = 0
            self._last_pushed_ns = 0
            # A source we have only just reached knows nothing, whatever the
            # previous one acknowledged. Keeping the old set would advertise
            # video to clients holding tickets this source has never seen.
            self._acked_tickets.clear()
            self._preview_pushed = None
        log.info("Video link attached to %s:%d", host, port)

    def update_status_from_link(self, body: dict) -> bool:
        """Absorb a VIDEO_STATUS arriving over the outbound link."""
        with self._lock:
            if self._source_client_id != "video-link":
                return False

            before = self._advert_key_locked()

            applied = body.get("cfg_seq")
            if isinstance(applied, int):
                self._applied_seq = applied
                if self._applied_seq == self._cfg_seq:
                    # It has the current configuration, so it has every ticket
                    # in it. Those clients can now be told to connect.
                    self._acked_tickets = set(self._tickets.values())

            port = body.get("media_port")
            if isinstance(port, int) and 1 <= port <= 65535:
                self._media_port = port

            lan_host = body.get("lan_host")
            if isinstance(lan_host, str) and lan_host:
                self._lan_host = lan_host

            status = body.get("status")
            if isinstance(status, dict):
                self._status = status
                self._status_ns = now_ns()

            devices = body.get("devices")
            if isinstance(devices, list):
                self._devices = [d for d in devices if isinstance(d, dict)][:64]

            self._adopt_settings_locked(body.get("settings"))

            return self._advert_key_locked() != before

    def attach_source(self, session) -> None:
        with self._lock:
            self._source_client_id = session.client_id
            self._source_address = session.address
            self._status = {}
            self._status_ns = 0
            # A new source has not seen our configuration, whatever the
            # previous one acknowledged.
            self._applied_seq = 0
            self._last_pushed_ns = 0
        log.info("Video source attached from %s:%d", session.address[0], session.address[1])

    def detach_source(self, client_id: str) -> bool:
        """Forget the source. Returns True if this actually was the source."""
        with self._lock:
            if self._source_client_id != client_id:
                return False
            self._source_client_id = None
            self._source_address = None
            self._status = {}
            self._status_ns = 0
            self._preview_data = None
            self._preview.reset()
        log.info("Video source detached")
        return True

    @property
    def source_client_id(self) -> str | None:
        return self._source_client_id

    @property
    def has_source(self) -> bool:
        return self._source_client_id is not None

    @property
    def is_live(self) -> bool:
        """A source that is attached *and* still reporting."""
        if self._source_client_id is None or not self._status_ns:
            return False
        return now_ns() - self._status_ns < STATUS_STALE_NS

    # -- inbound from the source -------------------------------------------

    def update_status(self, session, body: dict) -> bool:
        """Absorb a VIDEO_STATUS. Returns True if the advert changed materially."""
        with self._lock:
            if self._source_client_id != session.client_id:
                return False

            before = self._advert_key_locked()

            applied = body.get("cfg_seq")
            if isinstance(applied, int):
                self._applied_seq = applied

            port = body.get("media_port")
            if isinstance(port, int) and 1 <= port <= 65535:
                self._media_port = port

            lan_host = body.get("lan_host")
            if isinstance(lan_host, str):
                self._lan_host = lan_host[:64]

            status = body.get("status")
            if isinstance(status, dict):
                self._status = status
                self._status_ns = now_ns()

            devices = body.get("devices")
            if isinstance(devices, list):
                self._devices = [d for d in devices if isinstance(d, dict)][:64]

            self._adopt_settings_locked(body.get("settings"))

            return self._advert_key_locked() != before

    def _adopt_settings_locked(self, reported: object) -> None:
        """Take the source's own settings as ours, once, if we have none.

        Only runs while ``_configured`` is false, and it latches immediately
        after, so this can never fight the operator: the moment they change
        anything here, their choice becomes authoritative and is pushed as
        normal.
        """
        if self._configured or not isinstance(reported, dict):
            return
        self._settings = VideoSettings.from_dict(reported).clamped()
        self._configured = True
        log.info(
            "Adopted the video server's own settings (%dx%d @ %d fps, %d kbps%s); "
            "nothing was configured here to override them",
            self._settings.width,
            self._settings.height,
            self._settings.fps,
            self._settings.bitrate_kbps,
            f", device {self._settings.device!r}" if self._settings.device else "",
        )

    def feed_preview_slice(self, parsed: tuple) -> None:
        """Absorb one slice of a preview JPEG.

        Caller must have checked the session's role: this is the one path where
        a client's bytes are kept rather than acted on and discarded, so it must
        never be reachable by an ordinary controller session.
        """
        frame_id, index, count, flags, codec, capture_ts, payload = parsed
        if codec != MediaCodec.MJPEG:
            return
        completed = self._preview.add(
            frame_id, index, count, flags, codec, capture_ts, payload
        )
        if completed is None:
            return
        with self._lock:
            self._preview_data = completed.data
            self._preview_ns = now_ns()

    def preview(self) -> bytes | None:
        """The newest preview JPEG, or None when there is nothing fresh.

        Asking counts as watching -- see :meth:`preview_wanted`.
        """
        with self._lock:
            self._preview_asked_ns = now_ns()
            if self._preview_data is None:
                return None
            if now_ns() - self._preview_ns > PREVIEW_STALE_NS:
                return None
            return self._preview_data

    def preview_wanted(self) -> bool:
        """Is anybody actually looking at the preview right now?

        Preview slices are decoded and reassembled on the **datapath thread**,
        the one with a sub-millisecond budget, so an unwatched preview is pure
        cost paid against the controllers. It used to stream continuously from
        the moment a source connected, whether or not a browser had the panel
        open -- which is also why it had to be kept small and slow to be
        affordable at all.

        Gating on demand is what buys the resolution back: nothing flows until
        the web GUI starts fetching, and it stops again a few seconds after it
        stops. The browser polls several times a second while the panel is
        open, so the window is generous next to the poll interval.
        """
        with self._lock:
            if not self._preview_asked_ns:
                return False
            return now_ns() - self._preview_asked_ns <= PREVIEW_DEMAND_NS

    # -- outbound to clients -----------------------------------------------

    def ticket_for(self, client_id: str) -> str:
        """The viewing ticket for an approved client, minting one if needed.

        Stable per client so a reconnect does not need a fresh round trip, and
        unguessable so holding the password is not by itself enough to watch.

        Minting one marks the configuration stale and clears the re-push
        timer. The source has to learn a new ticket *before* the client tries
        to use it, and the client will try the moment it gets the advert --
        leaving it to the ordinary 2 s cadence means the first attempt is
        refused and the player waits out a retry for no reason.
        """
        with self._lock:
            ticket = self._tickets.get(client_id)
            if ticket is not None:
                return ticket

            ticket = secrets.token_urlsafe(24)
            self._tickets[client_id] = ticket
            self._cfg_seq += 1
            self._applied_seq = 0
            self._last_pushed_ns = 0
            return ticket

    def revoke_ticket(self, client_id: str) -> bool:
        """Withdraw a client's ticket. Returns True if it had one."""
        with self._lock:
            removed = self._tickets.pop(client_id, None) is not None
            if removed:
                log.debug("Revoked the viewing ticket for %s", client_id[:8])
                # Bump so the source is told promptly rather than at the next
                # unrelated settings change.
                self._cfg_seq += 1
                self._applied_seq = 0
                self._last_pushed_ns = 0
        return removed

    def valid_tickets(self) -> set[str]:
        with self._lock:
            return set(self._tickets.values())

    def source_advert(self, client_id: str | None = None) -> dict:
        """The VIDEO_SOURCE body: everything a client needs to find the stream.

        Not available until the source has reported at least once. A session
        exists from the moment it authenticates, but the media port it actually
        bound arrives with the first status -- advertising before then hands
        every client the *default* port and they all fail to connect, which
        looks like a broken video server rather than a premature advert.

        ``client_id`` names the client this advert is for, so it can carry that
        client's ticket. Omitting it produces an advert with no ticket, which
        the source will refuse -- correct for anyone not approved.
        """
        with self._lock:
            if (
                self.mode == MODE_OFF
                or self._source_address is None
                or not self._status_ns
            ):
                return {"available": False}

            ticket = self._tickets.get(client_id, "") if client_id is not None else ""
            if client_id is not None and ticket not in self._acked_tickets:
                # Their ticket has not reached the source yet. Saying "available"
                # now would send them straight into a refusal.
                return {"available": False}

            host = self.advertise_host or self._source_address[0]
            # In embedded mode the session arrives over loopback, which is
            # meaningless to anyone else -- fall back to whatever the source
            # reported for itself before advertising 127.0.0.1.
            if host.startswith("127.") and self._lan_host:
                host = self._lan_host

            advert = {
                "available": True,
                "host": host,
                "lan_host": self._lan_host,
                "port": self._media_port,
                "broker": self.broker,
                "room": self.room,
            }
            if client_id is not None:
                advert["ticket"] = ticket
            return advert

    def _advert_key_locked(self) -> tuple:
        """What clients would notice changing. Compared to decide on a re-push.

        ``bool(self._status_ns)`` is in here because it is what flips the
        advert from unavailable to available. Without it, a source whose real
        media port happened to equal the default would produce an identical
        key and no broadcast, and nobody would be told the stream had started.
        """
        return (
            self._source_address is not None,
            bool(self._status_ns),
            self._media_port,
            self._lan_host,
            self.mode,
            # Newly acknowledged tickets flip clients from "no video" to
            # "available", so the set is part of what clients would notice.
            frozenset(self._acked_tickets),
        )

    # -- configuration -----------------------------------------------------

    @property
    def settings(self) -> VideoSettings:
        return self._settings

    @property
    def cfg_seq(self) -> int:
        return self._cfg_seq

    def set_config(self, settings: VideoSettings) -> int:
        """Adopt new settings and bump the sequence. Returns the new seq."""
        clamped = settings.clamped()
        if self.mode == MODE_EMBEDDED:
            clamped = self.cap_for_embedded(clamped)

        with self._lock:
            self._settings = clamped
            # The operator has now chosen, so these are authoritative and are
            # pushed from here on -- no more adopting from the source.
            self._configured = True
            self._cfg_seq += 1
            self._applied_seq = 0
            self._last_pushed_ns = 0
            return self._cfg_seq

    @staticmethod
    def cap_for_embedded(settings: VideoSettings) -> VideoSettings:
        """Hold embedded settings to what a Pi can actually encode.

        The Pi 5 has no H.264 hardware encoder, so embedded mode is software
        encoding on a machine that is also serving the input datapath. Letting
        an operator ask for 1080p60 there does not produce 1080p60; it produces
        a stuttering stream and, worse, competition for the CPU the controllers
        depend on.
        """
        values = settings.to_dict()
        values["width"] = min(int(values["width"]), EMBEDDED_MAX_WIDTH)
        values["height"] = min(int(values["height"]), EMBEDDED_MAX_HEIGHT)
        values["fps"] = min(int(values["fps"]), EMBEDDED_MAX_FPS)
        values["bitrate_kbps"] = min(int(values["bitrate_kbps"]), EMBEDDED_MAX_BITRATE_KBPS)
        return VideoSettings(**values).clamped()

    def config_message(self) -> dict:
        """The VIDEO_CONFIG body.

        Carries the broker details as well as the capture settings: the source
        needs them to register its own leg of the room, and it has no other way
        to learn them -- the operator configures the broker here, not there.
        """
        with self._lock:
            message = {
                "cfg_seq": self._cfg_seq,
                "broker": self.broker,
                "room": self.room,
                # Who is allowed to watch. Sorted so an unchanged set produces
                # an identical message and nothing churns.
                "tickets": sorted(self._tickets.values()),
            }
            # Settings are omitted entirely until something here has been
            # chosen, so the tickets and the password still get through while
            # the source keeps the configuration it was set up with. Sending
            # our defaults instead would reset the operator's capture device
            # and resolution the moment we connected.
            # Whether anyone is looking *right now*. Deliberately its own field
            # rather than an override of `config["preview_enabled"]`.
            #
            # Overriding the setting looked tidier and was a trap: the source
            # adopts whatever we push, reports it back in its status, and a
            # server with nothing saved adopts the source's settings on
            # connect. So a restart while nobody had the panel open adopted
            # `preview_enabled: false` as the operator's own choice, and the
            # preview could never be turned on again -- with no control in the
            # web GUI to undo it. A transient signal must not round-trip
            # through a persisted one.
            wanted = self._preview_wanted_locked()
            message["preview_wanted"] = wanted
            self._preview_pushed = wanted

            if self._configured:
                message["config"] = self._settings.to_dict()
            return message

    def _preview_wanted_locked(self) -> bool:
        if not self._preview_asked_ns:
            return False
        return now_ns() - self._preview_asked_ns <= PREVIEW_DEMAND_NS

    def needs_config_push(self) -> bool:
        """True when the source has not acknowledged the current settings."""
        with self._lock:
            if self._source_client_id is None:
                return False

            stale = self._applied_seq != self._cfg_seq
            # Someone opened or closed the preview panel. It carries no new
            # cfg_seq -- it is not an operator change -- so without this the
            # source would not hear about it until something else moved.
            preview_changed = self._preview_wanted_locked() != self._preview_pushed
            if not stale and not preview_changed:
                return False

            now = now_ns()
            if now - self._last_pushed_ns < CONFIG_REPUSH_NS:
                return False
            self._last_pushed_ns = now
            return True

    def request_probe(self) -> int:
        """Ask the source to re-enumerate its devices."""
        values = self._settings.to_dict()
        values["probe_devices"] = True
        return self.set_config(VideoSettings(**values))

    # -- introspection -----------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            status = dict(self._status)
            stale = bool(
                self._source_client_id is not None
                and self._status_ns
                and now_ns() - self._status_ns > STATUS_STALE_NS
            )
            return {
                "mode": self.mode,
                "connected": self._source_client_id is not None,
                "live": (
                    self._source_client_id is not None
                    and bool(self._status_ns)
                    and now_ns() - self._status_ns < STATUS_STALE_NS
                ),
                "stale": stale,
                "source": (
                    f"{self._source_address[0]}:{self._source_address[1]}"
                    if self._source_address
                    else ""
                ),
                "media_port": self._media_port,
                "lan_host": self._lan_host,
                "status": status,
                "settings": self._settings.to_dict(),
                "cfg_seq": self._cfg_seq,
                "applied_seq": self._applied_seq,
                "config_pending": self._applied_seq != self._cfg_seq,
                "devices": list(self._devices),
                "has_preview": (
                    self._preview_data is not None
                    and now_ns() - self._preview_ns <= PREVIEW_STALE_NS
                ),
                "embedded": dict(self.embedded_state),
                "embedded_caps": {
                    "width": EMBEDDED_MAX_WIDTH,
                    "height": EMBEDDED_MAX_HEIGHT,
                    "fps": EMBEDDED_MAX_FPS,
                    "bitrate_kbps": EMBEDDED_MAX_BITRATE_KBPS,
                },
            }

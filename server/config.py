"""Server configuration.

Adapter selections are keyed by **BD_ADDR, never hciX index**. Index numbering
is assignment-order dependent and reshuffles across reboots and replugs -- a
config that remembered "hci1 drives the Switch" would silently start driving a
different console after someone moved a dongle to another USB port.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from common.video import DEFAULT_VIDEO_PORT as _DEFAULT_VIDEO_PORT

log = logging.getLogger(__name__)

APP_NAME = "rbgc"
CONFIG_FILENAME = "server.json"

DEFAULT_PORT = 47800
DEFAULT_WEB_PORT = 8080
DEFAULT_DISCOVERY_PORT = 47801

#: Imported rather than duplicated: three components have to agree on this one
#: (server, video server, client), and common/video.py is stdlib-only so there
#: is no dependency cost to taking it from the single place that defines it.
DEFAULT_VIDEO_PORT = _DEFAULT_VIDEO_PORT

MAX_ADAPTERS = 4
MAX_CLIENTS = 4


def config_dir() -> Path:
    """Config directory.

    Prefers /etc when running as root, since the server normally runs as a
    system service and root's home is not where an operator would look.
    """
    if os.name == "posix" and os.geteuid() == 0:
        return Path("/etc") / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


#: Public STUN servers, used only to learn our own address. They never see
#: a password, a room code or any session traffic -- a binding request carries
#: nothing but a random transaction ID. Point these at your own server
#: (coturn, or anything speaking RFC 5389) to avoid a third party entirely.
DEFAULT_STUN_SERVERS = ("stun.l.google.com:19302", "stun.cloudflare.com:3478")


@dataclass(slots=True)
class AdapterConfig:
    """Persisted settings for one Bluetooth adapter."""

    #: Stable identity across reboots and replug.
    bd_addr: str

    enabled: bool = True
    profile: str = "generic"

    #: Console/receiver we previously paired with, so "reconnect" can skip the
    #: pairing flow entirely.
    paired_target: str = ""

    #: Friendly label the operator sets, e.g. "Living room Switch". Overrides
    #: the generated "RBGC Gamepad N" name when set.
    label: str = ""

    #: 1-4. Assigned once when the adapter is first enabled and persisted, so
    #: the name a console remembers stays with the same physical dongle across
    #: reboots and replugs. 0 means "not yet assigned".
    number: int = 0


@dataclass(slots=True)
class ServerConfig:
    """Everything the server remembers between runs."""

    # Networking
    bind_host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    #: Admin interface. Defaults to loopback: it is an admin surface, and
    #: until TLS is configured its password crosses the wire in the clear.
    #: Set explicitly (or use an SSH tunnel) to reach it from elsewhere.
    web_host: str = "127.0.0.1"
    web_port: int = DEFAULT_WEB_PORT

    #: HTTPS for the admin interface, on by default. A self-signed
    #: certificate is generated on first run if none is configured --
    #: a headless LAN appliance has no route to a public CA, and
    #: self-signed still defeats the passive interception that is the
    #: real threat here.
    tls_enabled: bool = True
    tls_cert: str = ""
    tls_key: str = ""

    # -- who may connect, and how they find us -----------------------------
    #
    # Two independent transports, each with its own on/off and its own
    # visibility. Both default off: a freshly installed server should not open
    # itself to anything until the operator has set a password and deliberately
    # switched a path on.
    #
    # Toggling either leaves Bluetooth alone -- adapters stay paired and
    # consoles stay connected -- so turning players off never disturbs a game.

    #: Accept clients that connect straight to us (same LAN, VPN, port forward).
    lan_enabled: bool = False

    #: Answer LAN discovery probes. Hidden still accepts clients that already
    #: know the address; it just stops announcing.
    lan_discoverable: bool = True

    #: Accept clients introduced by the rendezvous broker, and register with it.
    #: Off by default: talking to a third-party host is the operator's decision.
    #: The broker only ever sees endpoints and, when listed, the server name --
    #: never the password and never controller input.
    internet_enabled: bool = False

    #: Appear in the broker's public listing. Hidden servers still register, so
    #: anyone holding the room code reaches them, but are never enumerated.
    internet_discoverable: bool = True

    # Discovery / NAT traversal
    discovery_enabled: bool = True
    discovery_port: int = DEFAULT_DISCOVERY_PORT
    server_name: str = ""

    #: Which controller every adapter emulates -- the HID report layout the
    #: console receives.
    #:
    #: Server-wide for the same reason as `controller_identity` below: BlueZ
    #: publishes one HID service record per machine, so the descriptor a host is
    #: told to expect is shared. This used to be per adapter in the web GUI,
    #: which could only ever produce a controller sending one format while
    #: advertising another.
    controller_profile: str = "generic"

    #: What the adapters claim to be: advertised name plus the vendor and
    #: product ids in the DeviceID record. See server/bt/identities.py.
    #:
    #: **Server-wide, not per adapter**, and not by choice: BlueZ keeps one SDP
    #: database for the whole machine, so there is exactly one DeviceID record
    #: however many dongles are plugged in -- the same constraint that stops
    #: adapters running different profiles. The *name* is per adapter (each
    #: appends its own number), so only the vendor half is shared.
    controller_identity: str = "generic"

    broker_host: str = ""
    broker_port: int = 47900

    #: Where to ask what our own public address is, so it can be reported to the
    #: broker rather than left for the broker to observe.
    #:
    #: That distinction is what lets a broker sit behind a reverse proxy, an frp
    #: tunnel or Docker's userland proxy: all of them re-originate the datagram,
    #: so what the broker observes is the proxy and punching at it fails. A
    #: directly-reachable STUN server still sees the real mapping.
    #:
    #: **Empty disables it** and restores the observe-only behaviour, which is
    #: correct whenever the broker is directly reachable -- and is the setting
    #: for anyone unwilling to involve a third party at all.
    stun_servers: list[str] = field(
        default_factory=lambda: list(DEFAULT_STUN_SERVERS)
    )
    room_code: str = ""

    # Access control. Passwords are never persisted -- see save().
    password: str = field(default="", repr=False)

    #: Separate password for the web GUI. Empty means "reuse the client
    #: password", which keeps existing setups working but conflates two trust
    #: levels: a player who can connect a controller should not automatically
    #: be able to approve clients or re-pair adapters.
    admin_password: str = field(default="", repr=False)

    auto_approve: bool = False
    max_clients: int = MAX_CLIENTS

    # Behaviour
    realtime: bool = True

    #: Forward console rumble back to clients. Both this and the client's
    #: own setting must be on for anything to be transmitted.
    rumble_enabled: bool = True
    adapters: list[AdapterConfig] = field(default_factory=list)

    # Video.
    #
    #: off      -- no video at all
    #: external -- a video server elsewhere registers with us
    #: embedded -- we run one ourselves as a subprocess
    #:
    #: Off by default, like every other transport here: streaming the console's
    #: picture off the machine is the operator's decision to make.
    video_mode: str = "off"

    #: Where to find the video server. In embedded mode this is our own child,
    #: so the host is loopback and the port is what we told it to bind.
    video_host: str = ""
    video_port: int = DEFAULT_VIDEO_PORT

    #: The video server's own password, which we use to connect to it. Not the
    #: players' password: keeping them apart is what stops a denied client
    #: coming back as the control peer. Never persisted -- see save().
    video_password: str = field(default="", repr=False)

    #: Capture/encode settings, as a plain dict so this module keeps no
    #: dependency on the video layer. Shape is common.video.VideoSettings.
    video_config: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.server_name:
            self.server_name = _default_server_name()

    def adapter(self, bd_addr: str) -> AdapterConfig | None:
        for entry in self.adapters:
            if entry.bd_addr.upper() == bd_addr.upper():
                return entry
        return None

    def upsert_adapter(self, adapter: AdapterConfig) -> None:
        existing = self.adapter(adapter.bd_addr)
        if existing is None:
            self.adapters.append(adapter)
        else:
            existing.enabled = adapter.enabled
            existing.profile = adapter.profile
            existing.paired_target = adapter.paired_target
            existing.label = adapter.label
            # Never regress an assigned number back to 0: the name would
            # change under a console that has already paired.
            if adapter.number:
                existing.number = adapter.number

    def enabled_adapters(self) -> list[AdapterConfig]:
        return [a for a in self.adapters if a.enabled]

    def validate(self) -> list[str]:
        problems: list[str] = []

        if not self.password:
            problems.append("A server password is required.")
        elif len(self.password) < 6:
            problems.append("Password must be at least 6 characters.")

        if not 1 <= self.port <= 65535:
            problems.append("Server port must be between 1 and 65535.")
        if not 1 <= self.web_port <= 65535:
            problems.append("Web port must be between 1 and 65535.")
        if self.port == self.web_port:
            problems.append("Server port and web port must differ.")

        if not 1 <= self.max_clients <= 8:
            problems.append("Max clients must be between 1 and 8.")

        if self.video_mode not in ("off", "external", "embedded"):
            problems.append(
                f"Video mode must be off, external or embedded (got {self.video_mode!r})."
            )
        if not 1 <= self.video_port <= 65535:
            problems.append("Video port must be between 1 and 65535.")
        if self.video_port in (self.port, self.web_port):
            problems.append("Video port must differ from the server and web ports.")

        enabled = self.enabled_adapters()
        if len(enabled) > MAX_ADAPTERS:
            problems.append(
                f"At most {MAX_ADAPTERS} adapters can be enabled "
                f"({len(enabled)} are currently selected)."
            )

        seen = set()
        for adapter in self.adapters:
            key = adapter.bd_addr.upper()
            if key in seen:
                problems.append(f"Duplicate adapter entry for {adapter.bd_addr}.")
            seen.add(key)

        return problems


def _default_server_name() -> str:
    import socket

    try:
        return socket.gethostname() or "rbgc-server"
    except OSError:
        return "rbgc-server"


def config_path() -> Path:
    return config_dir() / CONFIG_FILENAME


def load(path: Path | None = None) -> ServerConfig:
    """Load config, falling back to defaults on any problem.

    A corrupt config must not prevent startup -- the server is headless, so the
    operator would have no way to recover except by editing files over SSH.
    """
    target = path or config_path()
    if not target.exists():
        return ServerConfig()

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read config at %s (%s); using defaults", target, exc)
        return ServerConfig()

    adapters = [
        AdapterConfig(
            bd_addr=str(entry.get("bd_addr", "")).upper(),
            enabled=bool(entry.get("enabled", True)),
            profile=str(entry.get("profile", "generic")),
            paired_target=str(entry.get("paired_target", "")),
            label=str(entry.get("label", "")),
            number=int(entry.get("number", 0)),
        )
        for entry in raw.get("adapters", [])
        if entry.get("bd_addr")
    ]

    known = {f.name for f in ServerConfig.__dataclass_fields__.values()}
    kwargs = {k: v for k, v in raw.items() if k in known and k != "adapters"}
    kwargs["adapters"] = adapters

    # One on/off switch and one visibility flag became two of each, so an
    # existing config carries the old names. Map them across rather than
    # silently reverting the operator's choices to the defaults.
    if "server_enabled" in raw and "lan_enabled" not in raw:
        kwargs["lan_enabled"] = bool(raw["server_enabled"])
    if "discoverable" in raw and "lan_discoverable" not in raw:
        kwargs["lan_discoverable"] = bool(raw["discoverable"])
        kwargs.setdefault("internet_discoverable", bool(raw["discoverable"]))

    try:
        return ServerConfig(**kwargs)
    except TypeError as exc:
        log.warning("Config at %s has unexpected fields (%s); using defaults", target, exc)
        return ServerConfig()


def save(config: ServerConfig, path: Path | None = None) -> None:
    """Persist config atomically.

    The password is never written: the server is typically reachable over the
    network, and a plaintext shared secret in a config file is a bad default.
    It is supplied per-run via ``--password`` or ``RBGC_PASSWORD``.
    """
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    # Deny-list, not an allow-list: every secret has to be named here
    # explicitly, so adding a field without thinking writes it to disk.
    data = asdict(config)
    data["password"] = ""
    data["admin_password"] = ""
    data["video_password"] = ""

    temp = target.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp.replace(target)
    except OSError as exc:
        log.error("Could not save config to %s: %s", target, exc)
        temp.unlink(missing_ok=True)

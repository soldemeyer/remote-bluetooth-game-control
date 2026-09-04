"""Persisted settings for the standalone video server.

Follows the client's config discipline exactly: a corrupt file falls back to
defaults rather than blocking startup, unknown keys are dropped so an older
build can read a newer file, saves are atomic, and the password is not written
unless the operator asked for it.

The capture/encode settings themselves live in :class:`common.video.VideoSettings`
rather than here, because the Bluetooth server's web GUI edits the same shape and
sends it over the wire as VIDEO_CONFIG. This module holds only what is local to
*this installation*: where the server is, which port to serve media on, and the
credentials.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from common.video import DEFAULT_VIDEO_PORT, VideoSettings
from videoserver.discovery import DEFAULT_DISCOVERY_PORT

log = logging.getLogger(__name__)

APP_NAME = "rbgc"
CONFIG_FILENAME = "video.json"

DEFAULT_BROKER_PORT = 47900


def config_dir() -> Path:
    """Per-user config directory, following platform convention."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


#: Public STUN servers, used only to learn our own address. They never see
#: a password, a room code or any session traffic -- a binding request carries
#: nothing but a random transaction ID. Point these at your own server
#: (coturn, or anything speaking RFC 5389) to avoid a third party entirely.
DEFAULT_STUN_SERVERS = ("stun.l.google.com:19302", "stun.cloudflare.com:3478")


@dataclass(slots=True)
class VideoServerConfig:
    """Everything the video server remembers between runs."""

    #: Serve anyone who authenticates, with no Bluetooth server involved. For
    #: testing and for a LAN with no Pi yet; in normal use the Bluetooth server
    #: connects in and decides who may watch.
    standalone: bool = False

    #: Where media is served from. Binding all interfaces is the norm -- both
    #: the Bluetooth server and the clients connect here, which is the entire
    #: point of the architecture.
    media_bind_host: str = "0.0.0.0"
    media_port: int = DEFAULT_VIDEO_PORT

    #: This video server's own password. The operator sets it here and enters
    #: the same one in the Bluetooth server's web GUI.
    #:
    #: Deliberately *not* the players' password. Players never learn this one,
    #: so a client the operator denied cannot come back claiming to be the
    #: Bluetooth server -- the one role that is exempt from viewing tickets.
    #: The players' password arrives over the authenticated control link.
    password: str = field(default="", repr=False)
    save_password: bool = False

    #: Announce ourselves on the LAN so the Bluetooth server's operator can
    #: find this machine instead of typing its address.
    discoverable: bool = True
    discovery_port: int = DEFAULT_DISCOVERY_PORT

    #: Name shown in the Bluetooth server's web GUI.
    name: str = ""

    #: Broker for the Internet path. Empty leaves video LAN-only; the Bluetooth
    #: server can also supply these over VIDEO_CONFIG.
    broker_host: str = ""
    broker_port: int = DEFAULT_BROKER_PORT

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

    #: Colour scheme, shared with the client so the two applications match.
    #: One of `common.design.themes.THEMES`.
    theme: str = "amber"

    #: Capture and encode settings.
    settings: VideoSettings = field(default_factory=VideoSettings)

    def __post_init__(self) -> None:
        if not self.name:
            import socket

            self.name = socket.gethostname()

    def validate(self) -> list[str]:
        """Human-readable problems, empty when the config is usable."""
        problems: list[str] = []

        if self.password and len(self.password) < 6:
            problems.append("Password must be at least 6 characters.")
        # 0 means "let the OS choose". Usable because the port is not something
        # anyone has to know in advance: the Bluetooth server learns the real
        # one from the status message and passes it on to clients.
        if not 0 <= self.media_port <= 65535:
            problems.append(f"Media port {self.media_port} is out of range.")
        if not 1 <= self.discovery_port <= 65535:
            problems.append(f"Discovery port {self.discovery_port} is out of range.")
        if self.media_port and self.media_port == self.discovery_port:
            problems.append("Media port and discovery port must differ.")
        if self.broker_host and not self.room_code:
            problems.append("A broker needs a room code.")

        return problems


def config_path() -> Path:
    return config_dir() / CONFIG_FILENAME


def load(path: Path | None = None) -> VideoServerConfig:
    """Load config, falling back to defaults on any problem."""
    target = path or config_path()
    if not target.exists():
        return VideoServerConfig()

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read config at %s (%s); using defaults", target, exc)
        return VideoServerConfig()

    if not isinstance(raw, dict):
        log.warning("Config at %s is not an object; using defaults", target)
        return VideoServerConfig()

    known = {f.name for f in VideoServerConfig.__dataclass_fields__.values()}
    kwargs = {k: v for k, v in raw.items() if k in known and k != "settings"}
    kwargs["settings"] = VideoSettings.from_dict(raw.get("settings")).clamped()

    try:
        return VideoServerConfig(**kwargs)
    except TypeError as exc:
        log.warning("Config at %s has unexpected fields (%s); using defaults", target, exc)
        return VideoServerConfig()


def save(config: VideoServerConfig, path: Path | None = None) -> None:
    """Persist config. Writes atomically so a crash cannot truncate the file."""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    data = asdict(config)
    if not config.save_password:
        data["password"] = ""

    temp = target.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp.replace(target)
    except OSError as exc:
        log.error("Could not save config to %s: %s", target, exc)
        temp.unlink(missing_ok=True)

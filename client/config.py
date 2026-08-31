"""Client configuration: load, save, and defaults.

Stored as JSON in the user's config directory so the GUI and the headless mode
share one source of truth.

Controllers are remembered by SDL ``guid`` (hardware model) rather than by
instance id, because instance ids are reassigned on every replug -- binding to
them would shuffle players between slots whenever someone unplugged a pad.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "rbgc"
CONFIG_FILENAME = "client.json"

#: Poll rate default. 500 Hz is deliberately above typical USB gamepad report
#: rates (125-1000 Hz) so we never add a sampling delay of our own, while
#: costing far less CPU than 1000 Hz for no measurable benefit.
DEFAULT_POLL_HZ = 500

#: Suppresses packets from analog stick drift. 256 of 65536 is ~0.4% of full
#: scale -- below what any game reacts to, well above worn-stick jitter.
DEFAULT_AXIS_DEADBAND = 256

MAX_CONTROLLERS = 4

#: Must match server.config.DEFAULT_PORT.
#:
#: Defined as a module constant rather than read off ``ClientConfig.port``:
#: the dataclass uses ``slots=True``, so the class attribute is a member
#: descriptor, not the default value.
DEFAULT_PORT = 47800
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
class ControllerConfig:
    """One controller slot's persisted settings."""

    slot: int
    username: str = ""
    guid: str = ""
    device_name: str = ""
    enabled: bool = False

    #: Play rumble for *this* controller. The client-wide switch still gates
    #: everything -- a slot cannot opt in when the client has rumble off.
    rumble_enabled: bool = True

    #: Name of the controller configuration this slot uses (bindings + the
    #: controller type they were designed for). Empty means the default for
    #: whichever gamepad is selected.
    configuration: str = ""

    #: Which controller type's bindings, within that configuration, this slot
    #: uses. Per slot rather than per configuration: slots share configurations
    #: by name, so storing the active type on the configuration meant two slots
    #: using one configuration fought over it. Empty means the configuration's
    #: own default.
    layout: str = ""


@dataclass(slots=True)
class ClientConfig:
    """Everything the client remembers between runs.

    The password is deliberately *not* persisted by default -- see
    ``save_password``. Writing a shared secret to a plaintext JSON file is a
    bad default even for a LAN party tool.
    """

    # Connection
    #: auto | direct | tunnel | punch | relay
    #:
    #: ``tunnel`` is direct at the socket level, aimed at a public endpoint that
    #: fronts the server (frp, a port forward, a mesh VPN). ``relay`` goes
    #: through the broker without punching first -- for a network already known
    #: not to traverse, where the punch cannot succeed and only costs ~9.5 s.
    mode: str = "auto"
    host: str = ""
    port: int = DEFAULT_PORT
    room_code: str = ""                # hole-punch rendezvous identifier
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

    password: str = field(default="", repr=False)
    save_password: bool = False

    # Identity
    client_name: str = ""

    # Input
    poll_hz: int = DEFAULT_POLL_HZ
    axis_deadband: int = DEFAULT_AXIS_DEADBAND

    #: Persisted backend preference. The GUI only ever sets ``auto``; the
    #: fabricated ``synthetic`` backend is reachable through --backend alone.
    input_backend: str = "auto"

    #: Backend forced by ``--backend`` for **this run only**, never written to
    #: disk. It used to overwrite ``input_backend``, so a single
    #: ``--backend synthetic`` invocation permanently switched the GUI to fake
    #: controllers and hid every real gamepad. A one-off flag must not change
    #: saved settings.
    backend_override: str = field(default="", repr=False)

    #: Play rumble sent back from the console. Both this and the server's
    #: setting must be on for anything to be transmitted -- turning it off
    #: here tells the server to stop sending, it is not a local mute.
    rumble_enabled: bool = True

    controllers: list[ControllerConfig] = field(default_factory=list)

    #: Button/axis bindings, keyed by device GUID so they follow the physical
    #: hardware rather than a slot or an instance id (which changes on replug).
    #: Values are :meth:`client.input.mapping.DeviceMapping.to_dict` payloads,
    #: kept as plain dicts here so this module stays free of input-layer imports.
    mappings: dict = field(default_factory=dict)

    #: Which controller the mapping screen draws by default. Cosmetic: it
    #: changes the picture only, never what the server emulates.
    preview_layout: str = "xbox"

    #: Named controller configurations, each bundling a controller type with the
    #: bindings designed for it. Stored as plain dicts so this module keeps no
    #: dependency on the input layer; see client/gui/controller_config.py.
    configurations: list = field(default_factory=list)

    #: Open the video stream automatically once the server says one exists.
    #: Off means the player opens it from the Watch button instead.
    video_enabled: bool = True

    #: Open the video window straight into fullscreen.
    video_fullscreen: bool = False

    #: Play the stream's audio. Separate from the stream itself so someone
    #: using a capture card's audio elsewhere can mute ours without losing
    #: the picture.
    video_audio_enabled: bool = True

    #: Output level, 0-100, and a mute that keeps it. Mute is its own field so
    #: unmuting returns to the level the player chose rather than to full.
    video_volume: int = 100
    video_muted: bool = False

    def __post_init__(self) -> None:
        if not self.client_name:
            self.client_name = _default_client_name()
        if not self.controllers:
            self.controllers = [ControllerConfig(slot=i) for i in range(MAX_CONTROLLERS)]

    def effective_backend(self) -> str:
        """Which input backend to actually build.

        The per-run ``--backend`` override wins, but is never saved, so the
        stored preference survives a one-off test run untouched.
        """
        return self.backend_override or self.input_backend

    def controller(self, slot: int) -> ControllerConfig:
        for entry in self.controllers:
            if entry.slot == slot:
                return entry
        entry = ControllerConfig(slot=slot)
        self.controllers.append(entry)
        return entry

    def enabled_controllers(self) -> list[ControllerConfig]:
        return [c for c in self.controllers if c.enabled]

    def validate(self) -> list[str]:
        """Return human-readable problems. Empty means good to connect."""
        problems: list[str] = []

        if not 1 <= self.poll_hz <= 1000:
            problems.append("Poll rate must be between 1 and 1000 Hz.")
        if not 0 <= self.axis_deadband <= 8000:
            problems.append("Axis deadband must be between 0 and 8000.")
        if not 1 <= self.port <= 65535:
            problems.append("Server port must be between 1 and 65535.")

        if self.mode in ("direct", "auto") and not self.host and self.mode == "direct":
            problems.append("Direct mode needs a server address.")
        if self.mode == "tunnel" and not self.host:
            problems.append("Tunnel mode needs the public address of the tunnel.")
        if self.mode in ("punch", "relay"):
            what = "Relay" if self.mode == "relay" else "Hole-punch"
            if not self.room_code:
                problems.append(f"{what} mode needs a room code.")
            if not self.broker_host:
                problems.append(f"{what} mode needs a rendezvous broker address.")

        if not self.password:
            problems.append("A server password is required.")

        enabled = self.enabled_controllers()
        if not enabled:
            problems.append("Enable at least one controller.")
        if len(enabled) > MAX_CONTROLLERS:
            problems.append(f"At most {MAX_CONTROLLERS} controllers can be enabled.")

        slots = [c.slot for c in enabled]
        if len(slots) != len(set(slots)):
            problems.append("Two controllers are assigned to the same slot.")

        return problems


def _default_client_name() -> str:
    import socket

    try:
        return socket.gethostname() or "client"
    except OSError:
        return "client"


def config_path() -> Path:
    return config_dir() / CONFIG_FILENAME


def load(path: Path | None = None) -> ClientConfig:
    """Load config, falling back to defaults on any problem.

    A corrupt config must never prevent the app from starting -- the user would
    have no way to fix it through the GUI.
    """
    target = path or config_path()
    if not target.exists():
        return ClientConfig()

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read config at %s (%s); using defaults", target, exc)
        return ClientConfig()

    controllers = [
        ControllerConfig(
            slot=int(entry.get("slot", index)),
            username=str(entry.get("username", "")),
            guid=str(entry.get("guid", "")),
            device_name=str(entry.get("device_name", "")),
            enabled=bool(entry.get("enabled", False)),
            rumble_enabled=bool(entry.get("rumble_enabled", True)),
            configuration=str(entry.get("configuration", "")),
            layout=str(entry.get("layout", "")),
        )
        for index, entry in enumerate(raw.get("controllers", []))
    ]

    known = {f.name for f in ClientConfig.__dataclass_fields__.values()}
    kwargs = {k: v for k, v in raw.items() if k in known and k != "controllers"}
    kwargs["controllers"] = controllers

    try:
        config = ClientConfig(**kwargs)
    except TypeError as exc:
        log.warning("Config at %s has unexpected fields (%s); using defaults", target, exc)
        return ClientConfig()

    _migrate(config)
    return config


def _migrate(config: ClientConfig) -> None:
    """Repair configs written by older versions.

    ``synthetic`` is a **test** backend: it fabricates controllers and cannot
    see real hardware. It used to be persisted whenever anyone passed
    ``--backend synthetic`` for one run, and from then on every launch silently
    used fake controllers and showed no real gamepads at all -- which reads as
    "my controller isn't detected" rather than as a stuck setting.

    A stored value here is therefore always a mistake, never a preference, so it
    is repaired rather than respected. The flag itself no longer persists (see
    :attr:`ClientConfig.backend_override`); this cleans up configs already
    poisoned by it.
    """
    if config.input_backend == "synthetic":
        log.warning(
            "Config had input_backend='synthetic' (a test-only backend that hides "
            "real controllers); resetting to 'auto'"
        )
        config.input_backend = "auto"

    for entry in config.controllers:
        if entry.guid.startswith("synthetic-") or entry.device_name.startswith(
            "Synthetic Controller"
        ):
            entry.guid = ""
            entry.device_name = ""


def save(config: ClientConfig, path: Path | None = None) -> None:
    """Persist config. Writes atomically so a crash cannot truncate the file."""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    data = asdict(config)
    if not config.save_password:
        data["password"] = ""

    # Run-only. Persisting this is what turned a one-off `--backend synthetic`
    # into a permanent setting that hid every real controller.
    data.pop("backend_override", None)

    temp = target.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp.replace(target)
    except OSError as exc:
        log.error("Could not save config to %s: %s", target, exc)
        temp.unlink(missing_ok=True)

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

#: Which controller type a slot is when nothing has chosen one.
#:
#: Spelled out rather than imported from ``client.gui.controller_layouts``,
#: which is where it is defined, because this module deliberately carries no
#: dependency on the input or GUI layers -- ``configurations`` is a list of
#: plain dicts for the same reason. `tests/test_web_controller_art.py` pins the
#: two together so the copy cannot drift.
DEFAULT_LAYOUT = "xbox"

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


#: What `ClientConfig.video_hw_decode` may be.
HW_DECODE_MODES: tuple[str, ...] = ("off", "auto")

#: What `ClientConfig.video_upscaler` may be, in the order the GUI lists them.
#:
#: ``gpu`` is not an enhancement -- it presents through the GPU and scales with
#: a plain high-quality filter. It earns its place as the control: without it
#: "is FSR better?" and "what does RTX VSR cost?" are both unanswerable,
#: because Off differs from them in how it *presents* as well as in what it
#: does to the pixels.
UPSCALERS: tuple[str, ...] = ("off", "gpu", "fsr1", "rtx_vsr")


def _one_of(value: object, allowed: tuple[str, ...], fallback: str) -> str:
    """``value`` if it is one of ``allowed``, else ``fallback``.

    The same shape as `common.video._one_of`. Deliberately tolerant of
    unhashable input: this reads a JSON file, and ``value in frozenset`` raises
    TypeError on a list -- a trap this project has already been caught by once.
    """
    try:
        return value if value in allowed else fallback
    except TypeError:
        return fallback


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

    #: Decode on the GPU when the machine can. ``off`` | ``auto``.
    #:
    #: Off by default, and that is the point: with this and `video_upscaler`
    #: both at their defaults the video path is exactly what it has always
    #: been. Hardware decode changes which code produces the pixels even when
    #: nothing is being upscaled, so it is opt-in rather than "on if it works".
    video_hw_decode: str = "off"

    #: GPU enhancement. ``off`` | ``gpu`` | ``fsr1`` | ``rtx_vsr``.
    #:
    #: ``off`` takes the existing QPainter path untouched -- no device is
    #: created, no library is loaded, nothing is imported. ``gpu`` presents
    #: through the GPU with a plain high-quality scale and no enhancement,
    #: which is the control the other two are measured against.
    #:
    #: An unknown value falls back to ``off``, like `theme`, so a config
    #: written by a later version does not stop this one starting. The stored
    #: value is **never** overwritten when the hardware cannot do it: the
    #: player keeps their preference and it comes back when they return to the
    #: machine that can, which is why this is a preference and
    #: `effective_upscaler` is the answer.
    video_upscaler: str = "off"

    #: RCAS sharpening for FSR 1, 0-100, mapped to FidelityFX's own constant
    #: by the backend. Conservative by default: this is compressed video with
    #: block artifacts, not clean engine output, and oversharpening amplifies
    #: exactly the artifacts the encoder left behind.
    video_fsr_sharpness: int = 50

    #: Whether the controls drawer is open. Remembered because the two ways of
    #: using this window are different sittings: setting a session up, and then
    #: playing, where every pixel not showing the game is wasted.
    controls_open: bool = True

    #: Which drawer cards are unfolded, by key. Four cards ask for 1949px in a
    #: 774px drawer, so folding is how a player sees a whole one; which ones
    #: they left open is a view preference and belongs beside `controls_open`.
    #:
    #: **Only Controllers starts open**, which is a measurement rather than a
    #: taste: Controllers and Connection together are 765px of a 774px
    #: viewport before headers and spacing, so opening both puts the Connect
    #: button below the fold on a 900px-tall window. One card open is the only
    #: default that is scroll-free on the screens this runs on, and it is the
    #: card the work starts in.
    drawer_sections: dict[str, bool] = field(
        default_factory=lambda: {
            "controllers": True, "connection": False,
            "video": False, "latency": False,
        }
    )

    #: Colour scheme. One of `common.design.themes.THEMES`; an unknown name
    #: falls back to the default rather than failing, so a config written by a
    #: later version does not stop this one starting.
    theme: str = "amber"

    def __post_init__(self) -> None:
        if not self.client_name:
            self.client_name = _default_client_name()
        # Fall back rather than raise, the same way `theme` does. These arrive
        # from a JSON file that a later version may have written, and a value
        # this build does not know about must cost the player a setting, not
        # the ability to start.
        self.video_hw_decode = _one_of(
            self.video_hw_decode, HW_DECODE_MODES, "off"
        )
        self.video_upscaler = _one_of(self.video_upscaler, UPSCALERS, "off")
        try:
            self.video_fsr_sharpness = min(100, max(0, int(self.video_fsr_sharpness)))
        except (TypeError, ValueError):
            self.video_fsr_sharpness = 50
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

    def controller_layout(self, slot: int) -> str:
        """Which controller type this slot is configured as.

        The slot's own choice, then the named configuration's, then the
        default. Per slot first because slots share configurations by name, so
        storing the active type only on the configuration meant two slots
        fought over it.

        It exists here, rather than only in the GUI where it started, because
        the *server* is told this now -- it draws the matching pad on the
        adapter card, so an operator can see which player is holding what. A
        headless client with no copy of this logic would have reported nothing
        and every card would have shown the generic shell.

        `configurations` is a list of plain dicts on purpose: this module has
        no dependency on the input layer, and reading two keys out of a dict is
        cheaper than acquiring one.
        """
        entry = self.controller(slot)
        if entry.layout:
            return entry.layout
        if entry.configuration:
            for configuration in self.configurations:
                if not isinstance(configuration, dict):
                    continue
                if configuration.get("name") == entry.configuration:
                    return str(configuration.get("layout") or DEFAULT_LAYOUT)
        return DEFAULT_LAYOUT

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

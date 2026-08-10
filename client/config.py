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


@dataclass(slots=True)
class ControllerConfig:
    """One controller slot's persisted settings."""

    slot: int
    username: str = ""
    guid: str = ""
    device_name: str = ""
    enabled: bool = False


@dataclass(slots=True)
class ClientConfig:
    """Everything the client remembers between runs.

    The password is deliberately *not* persisted by default -- see
    ``save_password``. Writing a shared secret to a plaintext JSON file is a
    bad default even for a LAN party tool.
    """

    # Connection
    mode: str = "auto"                 # auto | direct | punch
    host: str = ""
    port: int = DEFAULT_PORT
    room_code: str = ""                # hole-punch rendezvous identifier
    broker_host: str = ""
    broker_port: int = DEFAULT_BROKER_PORT

    password: str = field(default="", repr=False)
    save_password: bool = False

    # Identity
    client_name: str = ""

    # Input
    poll_hz: int = DEFAULT_POLL_HZ
    axis_deadband: int = DEFAULT_AXIS_DEADBAND
    input_backend: str = "auto"

    controllers: list[ControllerConfig] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.client_name:
            self.client_name = _default_client_name()
        if not self.controllers:
            self.controllers = [ControllerConfig(slot=i) for i in range(MAX_CONTROLLERS)]

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
        if self.mode == "punch":
            if not self.room_code:
                problems.append("Hole-punch mode needs a room code.")
            if not self.broker_host:
                problems.append("Hole-punch mode needs a rendezvous broker address.")

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
        )
        for index, entry in enumerate(raw.get("controllers", []))
    ]

    known = {f.name for f in ClientConfig.__dataclass_fields__.values()}
    kwargs = {k: v for k, v in raw.items() if k in known and k != "controllers"}
    kwargs["controllers"] = controllers

    try:
        return ClientConfig(**kwargs)
    except TypeError as exc:
        log.warning("Config at %s has unexpected fields (%s); using defaults", target, exc)
        return ClientConfig()


def save(config: ClientConfig, path: Path | None = None) -> None:
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

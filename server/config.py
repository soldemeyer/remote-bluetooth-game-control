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

log = logging.getLogger(__name__)

APP_NAME = "rbgc"
CONFIG_FILENAME = "server.json"

DEFAULT_PORT = 47800
DEFAULT_WEB_PORT = 8080
DEFAULT_DISCOVERY_PORT = 47801

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

    #: Friendly label the operator sets, e.g. "Living room Switch".
    label: str = ""


@dataclass(slots=True)
class ServerConfig:
    """Everything the server remembers between runs."""

    # Networking
    bind_host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    web_host: str = "0.0.0.0"
    web_port: int = DEFAULT_WEB_PORT

    # Discovery / NAT traversal
    discovery_enabled: bool = True
    discovery_port: int = DEFAULT_DISCOVERY_PORT
    server_name: str = ""
    broker_host: str = ""
    broker_port: int = 47900
    room_code: str = ""

    # Access control. Password is never persisted -- see save().
    password: str = field(default="", repr=False)
    auto_approve: bool = False
    max_clients: int = MAX_CLIENTS

    # Behaviour
    realtime: bool = True
    adapters: list[AdapterConfig] = field(default_factory=list)

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
        )
        for entry in raw.get("adapters", [])
        if entry.get("bd_addr")
    ]

    known = {f.name for f in ServerConfig.__dataclass_fields__.values()}
    kwargs = {k: v for k, v in raw.items() if k in known and k != "adapters"}
    kwargs["adapters"] = adapters

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

    data = asdict(config)
    data["password"] = ""

    temp = target.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp.replace(target)
    except OSError as exc:
        log.error("Could not save config to %s: %s", target, exc)
        temp.unlink(missing_ok=True)

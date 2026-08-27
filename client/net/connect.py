"""Connection strategy: pick and execute a path to the server.

Three modes, one ladder:

* ``direct``     -- straight to a known host:port. LAN, VPN, port-forwarded.
* ``punch``      -- NAT hole-punching via the rendezvous broker.
* ``auto``       -- try direct, then LAN discovery, then hole-punch.

``auto`` is ordered by latency, not convenience: a direct path is always faster
than a punched one, and a punched one is always faster than a relay. Each rung
is only attempted when the one above it cannot work.

Kept out of both `client/main.py` and the GUI so the two share exactly one
implementation -- an earlier version had the GUI silently ignore the mode
selector and always connect directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from client.net.transport import ClientTransport, TransportError

log = logging.getLogger(__name__)


class broker_reachable:  # noqa: N801 - a namespace, not a class to instantiate
    """Whether the last `list_broker_servers` call got an answer.

    Deliberately a module-level namespace rather than a changed return type:
    two callers already unpack the list directly, and a broker that did not
    answer is not an error either of them should have to handle.
    """

    answered: bool = False
    detail: str = ""


def list_broker_servers(
    broker_host: str, broker_port: int, timeout: float = 2.0
) -> list[dict]:
    """Ask the broker which public servers it knows about.

    Returns ``[{"room", "name", "capacity", "in_use"}, ...]``. Only servers that
    opted in to being listed appear; a hidden server is reachable but never
    enumerated, so the player must be told its room code out of band.

    Never raises. A broker being unreachable is a normal condition (no Internet,
    wrong address, service down) and must not stop the player connecting by
    another route.

    **An empty list is two different answers**, and telling them apart is the
    difference between a five second fix and an evening: the broker answered
    and knows of no listed rooms, or it never answered at all. Both used to
    surface as "No servers found -- use Custom", which points the player at
    their own settings when the fault may be a server that never registered.
    `broker_reachable` carries that out of band, so this function keeps its
    signature and its never-raises contract.
    """
    import json
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(
                json.dumps({"op": "list"}).encode("utf-8"),
                (broker_host, broker_port),
            )
            data, _ = sock.recvfrom(8192)
    except (OSError, socket.timeout) as exc:
        log.debug("Broker listing failed: %s", exc)
        broker_reachable.answered = False
        broker_reachable.detail = str(exc)
        return []

    broker_reachable.answered = True
    broker_reachable.detail = ""

    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []

    if not isinstance(message, dict) or message.get("op") != "servers":
        return []

    servers = message.get("servers")
    if not isinstance(servers, list):
        return []

    # The broker is not trusted to send well-formed entries; drop anything that
    # is not a usable row rather than letting it into the GUI.
    return [
        entry
        for entry in servers
        if isinstance(entry, dict) and entry.get("room") and entry.get("name")
    ]


@dataclass(slots=True)
class ConnectResult:
    """What actually happened, for display and for tests."""

    mode: str                      # direct | punched | relay
    host: str
    port: int
    detail: str = ""
    attempts: tuple[str, ...] = ()

    @property
    def is_relayed(self) -> bool:
        return self.mode == "relay"

    def describe(self) -> str:
        if self.mode == "direct":
            return f"Direct connection to {self.host}:{self.port}"
        if self.mode == "punched":
            return f"NAT traversal succeeded -- direct to {self.host}:{self.port}"
        return (
            f"Relaying via {self.host}:{self.port} -- NAT traversal failed. "
            "Expect noticeably higher latency."
        )


def connect(
    transport: ClientTransport,
    config,
    *,
    discover_timeout: float = 1.5,
) -> ConnectResult:
    """Connect ``transport`` according to ``config.mode``.

    Raises TransportError with a combined explanation if every rung fails --
    reporting only the last failure would hide, for example, that direct was
    never attempted because no host was configured.
    """
    mode = (config.mode or "auto").lower()

    if mode == "direct":
        return _connect_direct(transport, config)
    if mode == "punch":
        return _connect_punch(transport, config)
    if mode != "auto":
        raise TransportError(f"Unknown connection mode {mode!r}")

    failures: list[str] = []

    # 1. A configured address is the fastest path and needs no third party.
    if config.host:
        try:
            return _connect_direct(transport, config, attempts=("direct",))
        except TransportError as exc:
            failures.append(f"direct to {config.host}:{config.port}: {exc}")

    # 2. LAN discovery -- same speed as direct, just without a typed address.
    found = _discover(discover_timeout)
    for server in found:
        try:
            result = _connect_direct(
                transport,
                config,
                host=server["host"],
                port=server["port"],
                attempts=("direct", "discovery"),
            )
            log.info("Connected to discovered server '%s'", server.get("name", ""))
            return result
        except TransportError as exc:
            failures.append(f"discovered {server['host']}: {exc}")
    if not found:
        failures.append("LAN discovery: no servers replied")

    # 3. Hole-punching -- the only option across the internet, and the slowest
    #    to establish, so it goes last.
    if config.broker_host and config.room_code:
        try:
            return _connect_punch(transport, config, attempts=("direct", "discovery", "punch"))
        except TransportError as exc:
            failures.append(f"hole-punch: {exc}")
    else:
        failures.append("hole-punch: no broker address or room code configured")

    raise TransportError(
        "Could not connect. Tried:\n  - " + "\n  - ".join(failures)
    )


def _connect_direct(
    transport: ClientTransport,
    config,
    *,
    host: str | None = None,
    port: int | None = None,
    attempts: tuple[str, ...] = ("direct",),
) -> ConnectResult:
    target_host = host or config.host
    target_port = port or config.port

    if not target_host:
        raise TransportError("Direct mode needs a server address")

    transport.connect(target_host, target_port)
    return ConnectResult(
        mode="direct", host=target_host, port=target_port, attempts=attempts
    )


def _connect_punch(
    transport: ClientTransport,
    config,
    *,
    attempts: tuple[str, ...] = ("punch",),
) -> ConnectResult:
    if not config.broker_host:
        raise TransportError("Hole-punch mode needs a rendezvous broker address")
    if not config.room_code:
        raise TransportError("Hole-punch mode needs a room code")

    outcome = transport.connect_via_broker(
        config.broker_host, config.broker_port, config.room_code
    )

    peer = outcome.relay_address if outcome.is_relayed else outcome.peer_address
    return ConnectResult(
        mode="relay" if outcome.is_relayed else "punched",
        host=peer[0] if peer else config.broker_host,
        port=peer[1] if peer else config.broker_port,
        detail=outcome.describe(),
        attempts=attempts,
    )


def _discover(timeout: float) -> list[dict]:
    """LAN discovery, best effort.

    Never raises: discovery failing is not a reason to block a connection that
    could still succeed by another route.
    """
    try:
        import asyncio

        from server.discovery import discover_servers

        return asyncio.run(discover_servers(timeout=timeout))
    except Exception as exc:
        log.debug("LAN discovery unavailable: %s", exc)
        return []

"""Client entry point.

Two modes:

  * ``--headless`` -- no GUI. Used for testing, for automation, and for
    running on a machine with no display.
  * default -- the PySide6 GUI.

The GUI is imported lazily so headless mode works on a machine without Qt, and
so a Qt import failure produces a clear message rather than a traceback at
startup.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time

from client import config as client_config
from client.input import InputBackendError, create_backend
from client.loop import InputLoop, SlotRuntime
from client.net.connect import connect as connect_to_server
from client.net.transport import ClientTransport, ConnectionState, TransportError
from common.protocol import ControlOp

log = logging.getLogger("rbgc.client")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rbgc-client",
        description="Remote Bluetooth game control client.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # GUI
  rbgc-client

  # Headless, first controller, direct to a LAN server
  rbgc-client --headless --direct 192.168.1.50 --password test123

  # Headless smoke test with no gamepad attached
  rbgc-client --headless --direct 127.0.0.1 --password test123 \\
              --backend synthetic --controllers 0,1

The password may also be supplied via RBGC_PASSWORD.
""",
    )

    parser.add_argument("--config", type=str, help="Path to a config file")
    parser.add_argument("--headless", action="store_true", help="Run without a GUI")

    conn = parser.add_argument_group("connection")
    conn.add_argument("--direct", metavar="HOST", help="Connect directly to HOST")
    conn.add_argument("--port", type=int, default=None, help="Server UDP port")
    conn.add_argument(
        "--mode",
        choices=["auto", "direct", "punch"],
        default=None,
        help="Connection mode (default from config, usually auto)",
    )
    conn.add_argument(
        "--broker",
        metavar="HOST[:PORT]",
        help="Rendezvous broker for NAT hole-punching",
    )
    conn.add_argument("--room", metavar="CODE", help="Rendezvous room code")
    conn.add_argument("--password", default=None, help="Server password. Prefer RBGC_PASSWORD.")
    conn.add_argument("--name", default=None, help="Client name shown on the server")

    inp = parser.add_argument_group("input")
    inp.add_argument(
        "--backend",
        choices=["auto", "sdl2", "synthetic"],
        default=None,
        help="Input backend (default auto)",
    )
    inp.add_argument(
        "--controllers",
        default=None,
        help="Comma-separated controller indices to stream, e.g. 0,1",
    )
    inp.add_argument(
        "--usernames",
        default=None,
        help="Comma-separated usernames matching --controllers",
    )
    inp.add_argument("--poll-hz", type=int, default=None, help="Input poll rate")
    inp.add_argument(
        "--no-rumble",
        action="store_true",
        help="Do not receive rumble (tells the server to stop sending it)",
    )
    inp.add_argument("--deadband", type=int, default=None, help="Analog stick deadband")
    inp.add_argument(
        "--list-controllers",
        action="store_true",
        help="List detected controllers and exit",
    )

    misc = parser.add_argument_group("misc")
    misc.add_argument(
        "--stats-interval",
        type=float,
        default=5.0,
        help="Seconds between latency reports in headless mode (0 to disable)",
    )
    misc.add_argument("-v", "--verbose", action="count", default=0)

    return parser


def configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def apply_overrides(cfg: client_config.ClientConfig, args) -> None:
    if args.direct:
        cfg.mode = "direct"
        cfg.host = args.direct
    if args.mode:
        # An explicit --mode wins over the implicit "direct" that --direct sets.
        cfg.mode = args.mode
    if args.broker:
        host, _, port = args.broker.partition(":")
        cfg.broker_host = host
        if port.isdigit():
            cfg.broker_port = int(port)
        if not args.mode and not args.direct:
            cfg.mode = "punch"
    if args.room:
        cfg.room_code = args.room
    if args.port is not None:
        cfg.port = args.port
    if args.name:
        cfg.client_name = args.name
    if args.backend:
        cfg.input_backend = args.backend
    if args.poll_hz is not None:
        cfg.poll_hz = args.poll_hz
    if args.deadband is not None:
        cfg.axis_deadband = args.deadband
    if args.no_rumble:
        cfg.rumble_enabled = False

    cfg.password = args.password or os.environ.get("RBGC_PASSWORD", "") or cfg.password

    if args.controllers:
        indices = _parse_int_list(args.controllers)
        usernames = args.usernames.split(",") if args.usernames else []

        for entry in cfg.controllers:
            entry.enabled = False

        for position, index in enumerate(indices):
            entry = cfg.controller(position)
            entry.enabled = True
            entry.username = (
                usernames[position].strip()
                if position < len(usernames)
                else f"Player {position + 1}"
            )
            # Stash the backend index; resolved against real devices at startup.
            entry.device_name = str(index)


def _parse_int_list(value: str) -> list[int]:
    result = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            log.warning("Ignoring non-numeric controller index %r", part)
    return result


def list_controllers(backend_kind: str) -> int:
    try:
        backend = create_backend(backend_kind)
        backend.open()
    except InputBackendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        devices = backend.list_devices()
        if not devices:
            print("No game controllers detected.")
            print("\nCheck that the controller is connected and recognized by the OS.")
            return 0

        print(f"\n  {len(devices)} controller(s) detected:\n")
        needs_mapping = False
        for device in devices:
            note = device.status_note()
            print(f"    [{device.instance_id}]  {device.display_name()}"
                  f"{'  (' + note + ')' if note else ''}")
            print(f"          guid: {device.guid}")
            if device.axis_count or device.button_count:
                print(f"          {device.axis_count} axes, {device.button_count} buttons, "
                      f"{device.hat_count} hat(s)")
            needs_mapping |= not device.is_mapped
        print()

        if needs_mapping:
            # Worth saying explicitly: a pad marked this way works fine, it just
            # is not in SDL's mapping database. An earlier version hid such
            # devices entirely, which read as "controller not detected".
            print("  A controller above has no built-in layout. It still works --")
            print("  open the client and use 'Configure controls...' to bind it.\n")
        return 0
    finally:
        backend.close()


def run_headless(cfg: client_config.ClientConfig, args) -> int:
    problems = cfg.validate()
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 2

    try:
        backend = create_backend(
            cfg.input_backend,
            **({"count": 4} if cfg.input_backend == "synthetic" else {}),
        )
        backend.open()
    except InputBackendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    transport = ClientTransport(
        cfg.password,
        client_name=cfg.client_name,
        rumble_enabled=cfg.rumble_enabled,
        on_state_change=lambda state, detail: log.info("Connection: %s %s", state.name, detail),
    )

    try:
        slots = _build_slots(backend, cfg)
    except InputBackendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        backend.close()
        return 1

    if not slots:
        print("error: no usable controllers. Try --list-controllers.", file=sys.stderr)
        backend.close()
        return 1

    print(f"\n  Connecting ({cfg.mode} mode) ...")
    try:
        result = connect_to_server(transport, cfg)
    except TransportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        backend.close()
        return 1

    print(f"  {result.describe()}")
    if result.is_relayed:
        print("  WARNING: traffic is being relayed; latency will be higher than direct.")
    print(f"  Connected. Server capacity: {transport.server_capacity} controller(s)")

    usable = [s for s in slots if s.slot < max(1, transport.server_capacity)]
    if len(usable) < len(slots):
        print(
            f"  Note: server has only {transport.server_capacity} adapter(s); "
            f"streaming {len(usable)} of {len(slots)} controller(s)."
        )

    transport.queue_control(
        ControlOp.SET_CONTROLLERS,
        {
            "client_name": cfg.client_name,
            "controllers": [
                {"slot": s.slot, "username": s.username, "device_name": s.device_name}
                for s in usable
            ],
        },
    )

    loop = InputLoop(
        backend,
        transport,
        poll_hz=cfg.poll_hz,
        axis_deadband=cfg.axis_deadband,
    )
    # Rumble arrives on the transport's receive path, which runs on the
    # input loop's thread -- so it can call straight into the backend.
    transport._on_rumble = loop.play_rumble

    loop.set_slots(usable)
    loop.start()

    for entry in usable:
        print(f"    slot {entry.slot}: {entry.device_name}  ({entry.username or 'no name'})")
    print("\n  Streaming. Press Ctrl+C to stop.\n")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with_term = hasattr(signal, "SIGTERM")
    if with_term:
        signal.signal(signal.SIGTERM, lambda *_: stop.set())

    try:
        _headless_monitor(stop, loop, transport, args.stats_interval)
    finally:
        loop.stop()
        transport.close()
        backend.close()
        print("\n  Stopped.")

    return 0


def _headless_monitor(stop, loop, transport, stats_interval: float) -> None:
    last_report = time.monotonic()

    while not stop.is_set():
        time.sleep(0.2)

        if transport.state in (ConnectionState.DISCONNECTED, ConnectionState.FAILED):
            print(f"\n  Connection lost: {transport.state_detail}")
            return

        if stats_interval <= 0:
            continue
        if time.monotonic() - last_report < stats_interval:
            continue
        last_report = time.monotonic()

        latency = transport.latency_snapshot()
        if not latency:
            idle = transport.idle_latency_snapshot()
            if idle["count"]:
                print(f"  idle RTT  p50 {idle['p50']:.1f} ms   p99 {idle['p99']:.1f} ms")
            continue

        for entry in loop.slots():
            stats = latency.get(entry.slot)
            if not stats or not stats["rtt"]["count"]:
                continue
            rtt = stats["rtt"]
            bt = stats["bt_write"]
            print(
                f"  slot {entry.slot} ({entry.username or '-'}):  "
                f"RTT p50 {rtt['p50']:.1f} ms  p99 {rtt['p99']:.1f} ms   "
                f"BT write p50 {bt['p50']:.2f} ms   "
                f"sent {entry.packets_sent}"
            )


def _build_slots(backend, cfg: client_config.ClientConfig) -> list[SlotRuntime]:
    """Resolve configured controllers against what is actually connected."""
    devices = {d.instance_id: d for d in backend.list_devices()}
    if not devices:
        return []

    ordered = sorted(devices)
    slots: list[SlotRuntime] = []

    for entry in cfg.enabled_controllers():
        # device_name holds a backend index when set via --controllers.
        instance_id = None
        if entry.device_name.isdigit():
            wanted = int(entry.device_name)
            instance_id = wanted if wanted in devices else None
            if instance_id is None and wanted < len(ordered):
                instance_id = ordered[wanted]
        elif entry.guid:
            for device in devices.values():
                if device.guid == entry.guid:
                    instance_id = device.instance_id
                    break

        if instance_id is None:
            position = len(slots)
            if position >= len(ordered):
                log.warning("No device available for slot %d", entry.slot)
                continue
            instance_id = ordered[position]

        device = backend.acquire(instance_id)
        slots.append(
            SlotRuntime(
                slot=entry.slot,
                instance_id=instance_id,
                username=entry.username,
                device_name=device.display_name(),
            )
        )

    return slots


def run_gui(cfg: client_config.ClientConfig, args) -> int:
    try:
        from client.gui.app import run
    except ImportError as exc:
        print(f"error: GUI unavailable ({exc})", file=sys.stderr)
        print('Install the client extras:  pip install -e ".[client]"', file=sys.stderr)
        print("Or run headless:  rbgc-client --headless --direct HOST", file=sys.stderr)
        return 1

    return run(cfg, args)


def _attach_console_if_needed() -> None:
    """Restore usable stdio on Windows for the windowed packaged build.

    The executable is built windowed (``console=False``) so double-clicking it
    does not flash a console behind the GUI. A windowed process starts with
    ``sys.stdout``/``sys.stderr`` set to None, so ``--headless``,
    ``--list-controllers`` and ``--help`` would otherwise print into the void
    -- or crash on ``None.write``.

    Three cases, in order:

    1. Output is redirected to a file or pipe -- fds 1/2 are already valid and
       we just need Python objects wrapping them.
    2. Launched from a terminal -- ``AttachConsole(ATTACH_PARENT_PROCESS)``
       borrows that console, then the fds become valid.
    3. Launched from Explorer with no redirection -- there is nowhere to write,
       so bind a null sink to keep ``print`` from raising.

    A normal Python run takes the early return and is unaffected.
    """
    if sys.platform != "win32" or (sys.stdout is not None and sys.stderr is not None):
        return

    import ctypes

    def _bind(fileno: int):
        try:
            return open(fileno, "w", buffering=1, errors="replace", closefd=False)
        except OSError:
            return None

    stdout, stderr = _bind(1), _bind(2)

    if stdout is None and stderr is None:
        # Nothing valid yet -- try to borrow the launching terminal's console.
        ATTACH_PARENT_PROCESS = -1
        if ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            stdout, stderr = _bind(1), _bind(2)

    sys.stdout = stdout or _NullWriter()
    sys.stderr = stderr or _NullWriter()


class _NullWriter:
    """Discards output. Keeps ``print`` working when there is nowhere to write."""

    def write(self, _data: str) -> int:
        return 0

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("no underlying stream")


def main(argv: list[str] | None = None) -> int:
    # Before parse_args: argparse writes --help and usage errors to stdio, which
    # a windowed build does not have until we attach.
    _attach_console_if_needed()

    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)

    if args.list_controllers:
        return list_controllers(args.backend or "auto")

    cfg = client_config.load(
        client_config.Path(args.config) if args.config else None
    )
    apply_overrides(cfg, args)

    if args.headless:
        return run_headless(cfg, args)
    return run_gui(cfg, args)


if __name__ == "__main__":
    sys.exit(main())

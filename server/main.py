"""Server entry point.

Wires together the datapath (hot path, own thread), the router, session
management, the Bluetooth layer, and the web GUI.

``--mock-bt`` substitutes an in-memory sink for real Bluetooth, which is what
makes the whole system developable and testable on any machine. It is a
one-line substitution rather than a special code path, because the datapath
only ever talks to the HIDSink interface.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys

from server import config as server_config
from server.bt.profiles import DEFAULT_PROFILE, PROFILES, create_profile
from server.bt.sink import MockSink
from server.datapath import Datapath
from server.router import MAX_OUTPUTS, OutputChannel, Router
from server.sessions import SessionManager, generate_password

log = logging.getLogger("rbgc.server")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rbgc-server",
        description="Remote Bluetooth game control server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Development: no Bluetooth hardware needed
  rbgc-server --mock-bt --mock-adapters 4 --password test123

  # Real hardware, headless with web GUI on port 8080
  sudo rbgc-server --password "$(cat /etc/rbgc/password)"

The password may also be supplied via the RBGC_PASSWORD environment variable,
which keeps it out of the process list.
""",
    )

    parser.add_argument("--config", type=str, help="Path to a config file")

    net = parser.add_argument_group("network")
    net.add_argument("--host", default=None, help="Bind address (default 0.0.0.0)")
    net.add_argument("--port", type=int, default=None, help="UDP port for clients")
    net.add_argument("--web-port", type=int, default=None, help="Web GUI port")
    net.add_argument("--no-web", action="store_true", help="Disable the web GUI")
    net.add_argument(
        "--no-discovery", action="store_true", help="Disable the LAN discovery beacon"
    )
    net.add_argument(
        "--no-tls",
        action="store_true",
        help="Serve the web GUI over plain HTTP (not recommended over a network)",
    )
    net.add_argument("--tls-cert", default=None, help="Path to a TLS certificate (PEM)")
    net.add_argument("--tls-key", default=None, help="Path to the matching private key")
    net.add_argument(
        "--broker",
        metavar="HOST[:PORT]",
        help="Rendezvous broker, to accept NAT hole-punched clients",
    )
    net.add_argument("--room", metavar="CODE", help="Rendezvous room code")

    access = parser.add_argument_group("access control")
    access.add_argument(
        "--password",
        default=None,
        help="Shared password clients must present. Prefer RBGC_PASSWORD.",
    )
    access.add_argument(
        "--generate-password",
        action="store_true",
        help="Generate a random password, print it, and use it for this run",
    )
    access.add_argument(
        "--admin-password",
        default=None,
        help=(
            "Separate password for the web GUI. Defaults to the client password, "
            "which also grants operator access -- set this to separate the two."
        ),
    )
    access.add_argument(
        "--auto-approve",
        action="store_true",
        help="Skip operator approval and assign controllers automatically",
    )
    access.add_argument(
        "--accept-clients",
        dest="accept_clients",
        action="store_true",
        default=None,
        help=(
            "Accept client connections immediately. A server that has never been "
            "configured starts switched off, so nothing is exposed to the network "
            "until someone turns it on in the web GUI -- this overrides that for "
            "a scripted or headless run."
        ),
    )
    access.add_argument(
        "--no-accept-clients",
        dest="accept_clients",
        action="store_false",
        help="Start switched off even if the saved config says otherwise",
    )
    access.add_argument("--max-clients", type=int, default=None, help="Max client PCs")

    bt = parser.add_argument_group("bluetooth")
    bt.add_argument(
        "--mock-bt",
        action="store_true",
        help="Use in-memory sinks instead of real Bluetooth (no hardware needed)",
    )
    bt.add_argument(
        "--mock-adapters",
        type=int,
        default=4,
        help="How many fake adapters to create with --mock-bt (default 4)",
    )
    bt.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        default=DEFAULT_PROFILE,
        help=f"Default target profile for new adapters (default {DEFAULT_PROFILE})",
    )

    misc = parser.add_argument_group("misc")
    misc.add_argument(
        "--no-rumble",
        action="store_true",
        help="Never send console rumble back to clients",
    )
    misc.add_argument(
        "--no-realtime",
        action="store_true",
        help="Skip SCHED_FIFO and GC tuning (useful when debugging)",
    )
    misc.add_argument("-v", "--verbose", action="count", default=0, help="-v, -vv")

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
    # These are chatty at DEBUG and rarely what you are looking for.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def resolve_password(args: argparse.Namespace, cfg: server_config.ServerConfig) -> str:
    """Resolve the password, preferring sources that stay out of the process list."""
    if args.generate_password:
        password = generate_password()
        print(f"\n  Generated server password: {password}\n", flush=True)
        return password

    return (
        args.password
        or os.environ.get("RBGC_PASSWORD", "")
        or cfg.password
        or ""
    )


def create_mock_channels(router: Router, count: int, profile_name: str) -> None:
    """Populate the router with fake adapters.

    Deliberately respects MAX_OUTPUTS so ``--mock-adapters 8`` exercises the
    same capacity ceiling the real hardware path enforces.
    """
    count = max(0, min(count, MAX_OUTPUTS))
    for index in range(count):
        bd_addr = f"00:00:00:00:00:{index:02X}"
        channel = OutputChannel(
            bd_addr=bd_addr,
            hci_name=f"mock{index}",
            profile=create_profile(profile_name),
            sink=MockSink(name=f"mock{index}"),
        )
        router.add_channel(channel)

    log.info("Created %d mock adapter(s)", count)


async def run_server(args: argparse.Namespace) -> int:
    cfg = server_config.load(
        server_config.Path(args.config) if args.config else None
    )

    # CLI overrides config.
    if args.host is not None:
        cfg.bind_host = args.host
    if args.port is not None:
        cfg.port = args.port
    if args.web_port is not None:
        cfg.web_port = args.web_port
    if args.max_clients is not None:
        cfg.max_clients = args.max_clients
    if args.auto_approve:
        cfg.auto_approve = True
    if args.accept_clients is not None:
        cfg.server_enabled = args.accept_clients
    if args.no_realtime:
        cfg.realtime = False
    if args.no_rumble:
        cfg.rumble_enabled = False
    if args.no_discovery:
        cfg.discovery_enabled = False
    if args.no_tls:
        cfg.tls_enabled = False
    if args.tls_cert:
        cfg.tls_cert = args.tls_cert
    if args.tls_key:
        cfg.tls_key = args.tls_key
    if args.broker:
        host, _, port = args.broker.partition(":")
        cfg.broker_host = host
        if port.isdigit():
            cfg.broker_port = int(port)
    if args.room:
        cfg.room_code = args.room

    cfg.password = resolve_password(args, cfg)
    cfg.admin_password = (
        args.admin_password or os.environ.get("RBGC_ADMIN_PASSWORD", "") or cfg.admin_password
    )

    if args.password:
        # argv is world-readable via ps on a multi-user system.
        log.warning(
            "--password was given on the command line, where any local user can read "
            "it with 'ps'. Prefer RBGC_PASSWORD or the systemd password file."
        )
    if not cfg.admin_password:
        log.warning(
            "No separate admin password set: the web GUI accepts the same password "
            "clients use, so any player can approve clients and re-pair adapters. "
            "Set --admin-password or RBGC_ADMIN_PASSWORD to separate them."
        )

    problems = cfg.validate()
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        if not cfg.password:
            print(
                "\nSet one with --password, RBGC_PASSWORD, or --generate-password.",
                file=sys.stderr,
            )
        return 2

    router = Router()
    sessions = SessionManager(
        cfg.password,
        max_clients=cfg.max_clients,
        auto_approve=cfg.auto_approve,
    )

    # Rendezvous registration shares the datapath's socket on purpose: the NAT
    # mapping it opens must be the one gameplay traffic uses. The send callable
    # closes over `datapath`, which is created just below -- it is never invoked
    # before then.
    rendezvous = None
    if cfg.broker_host and cfg.room_code and cfg.internet_enabled:
        from server.rendezvous import RendezvousClient

        rendezvous = RendezvousClient(
            cfg.broker_host,
            cfg.broker_port,
            cfg.room_code,
            send=lambda data, addr: datapath.send_raw(data, addr),
            local_port=cfg.port,
            # A name is sent only when the operator chose to be discoverable;
            # without one the broker registers us but never lists us.
            public_name=cfg.server_name if cfg.discoverable else "",
            describe=lambda: (
                router.capacity,
                sum(1 for c in router.channels() if c.is_assigned),
            ),
        )
        if not rendezvous.resolve():
            log.error("Broker unreachable; hole-punching disabled for this run")
            rendezvous = None

    # Built before the Bluetooth layer because the HID servers need somewhere to
    # deliver rumble, and started afterwards so no packet arrives before the
    # adapters that would serve it exist.
    datapath = Datapath(
        sessions,
        router,
        bind_host=cfg.bind_host,
        bind_port=cfg.port,
        realtime=cfg.realtime,
        rendezvous=rendezvous,
        rumble_enabled=cfg.rumble_enabled,
    )

    adapter_manager = None
    if args.mock_bt:
        create_mock_channels(router, args.mock_adapters, args.profile)
    else:
        adapter_manager = await setup_real_bluetooth(
            router, cfg, args.profile, on_rumble=datapath.send_rumble
        )
        if adapter_manager is None or router.capacity == 0:
            log.warning(
                "No Bluetooth adapters are available. The server will run and accept "
                "clients, but no controller can be routed until an adapter appears."
            )

    datapath.start()

    # Start gated to whatever the operator last chose. A fresh install defaults
    # to off, so a new server never opens itself to the network before someone
    # has set a password and deliberately switched it on.
    datapath.set_accepting(cfg.server_enabled)
    if not cfg.server_enabled:
        log.warning(
            "Server is NOT accepting clients. Turn it on in the web GUI "
            "(Server -> Accept client connections)."
        )

    # The adapter manager needs the datapath so capacity changes reach clients
    # live -- enabling a dongle re-enables a slot in every client GUI without
    # anyone reconnecting.
    if adapter_manager is not None:
        adapter_manager.on_change = datapath.broadcast_capacity

    web_runner = None
    if not args.no_web:
        web_runner = await start_web_gui(
            cfg, sessions, router, datapath, adapter_manager,
            config_path=server_config.config_path(),
        )

    discovery = None
    if cfg.discovery_enabled:
        discovery = await start_discovery_beacon(cfg, router)

    _print_banner(cfg, router, args)

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    try:
        await stop_event.wait()
    finally:
        log.info("Shutting down...")
        if rendezvous is not None:
            # Free the broker room promptly rather than waiting for the TTL.
            with contextlib.suppress(Exception):
                rendezvous.stop()
        if discovery is not None:
            discovery.close()
        if web_runner is not None:
            await web_runner.cleanup()
        datapath.stop()
        for channel in router.channels():
            with contextlib.suppress(Exception):
                channel.sink.close()

    return 0


async def setup_real_bluetooth(router: Router, cfg, profile_name: str, *, on_rumble=None):
    """Discover adapters and bring up the enabled ones.

    Imported lazily so the server still starts on a machine without BlueZ --
    ``--mock-bt`` must work anywhere. Returns the manager, or None.
    """
    try:
        from server.bt.adapter import AdapterManager
        from server.bt.sdp import check_bluetooth_daemon
    except ImportError as exc:
        log.error("Bluetooth support unavailable (%s). Try --mock-bt.", exc)
        return None

    # Diagnose the two classic misconfigurations up front, so they surface as
    # advice rather than as an opaque EADDRINUSE later.
    for problem in check_bluetooth_daemon():
        log.error("%s", problem)

    manager = AdapterManager(
        router,
        cfg,
        default_profile=profile_name,
        on_rumble=on_rumble,
        # So a learned reconnect target is written back and survives a restart.
        config_path=server_config.config_path(),
    )
    await manager.start()
    return manager


async def start_web_gui(cfg, sessions, router, datapath, adapter_manager=None, config_path=None):
    """Start the aiohttp web GUI. Lazily imported so --no-web needs no aiohttp."""
    try:
        from server.web.app import create_runner
    except ImportError as exc:
        log.error("Web GUI unavailable (%s). Install: pip install -e '.[server]'", exc)
        return None

    return await create_runner(cfg, sessions, router, datapath, adapter_manager, config_path)


async def start_discovery_beacon(cfg, router):
    try:
        from server.discovery import DiscoveryBeacon
    except ImportError as exc:
        log.warning("LAN discovery unavailable: %s", exc)
        return None

    beacon = DiscoveryBeacon(cfg, router)
    await beacon.start()
    return beacon


def _print_banner(cfg, router: Router, args: argparse.Namespace) -> None:
    mode = "MOCK Bluetooth" if args.mock_bt else "Bluetooth"
    print()
    print("  Remote Bluetooth Game Control -- server")
    print(f"    clients      udp://{cfg.bind_host}:{cfg.port}")
    if not args.no_web:
        scheme = "https" if cfg.tls_enabled else "http"
        print(f"    web GUI      {scheme}://{cfg.web_host}:{cfg.web_port}")
        if cfg.web_host in ("127.0.0.1", "localhost", "::1"):
            print(
                f"                 (loopback only -- tunnel with: "
                f"ssh -L {cfg.web_port}:127.0.0.1:{cfg.web_port} user@host)"
            )
    print(f"    {mode:<12} {router.capacity} adapter(s) -> capacity {router.capacity}")
    print(f"    approval     {'automatic' if cfg.auto_approve else 'manual (via web GUI)'}")
    if router.capacity == 0:
        print("    WARNING      no adapters; clients can connect but cannot play")
    print()


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            # Windows does not support add_signal_handler for these.
            signal.signal(sig, lambda *_: stop_event.set())


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)

    try:
        return asyncio.run(run_server(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

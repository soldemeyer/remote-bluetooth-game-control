"""Video server entry point: `rbgc-video`.

Mirrors the client's startup discipline -- console reattachment before argument
parsing (a windowed build has no stdio, and argparse writes usage errors to
stderr), GUI by default with a soft fallback to a clear message when the Qt
extras are missing, and a run-only override kept out of the persisted config.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from common.timing import high_resolution_timers
from common.video import DEFAULT_VIDEO_PORT, VideoSettings
from videoserver import config as video_config
from videoserver.config import VideoServerConfig

log = logging.getLogger("rbgc.videoserver")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rbgc-video",
        description="Stream a capture device to remote-play clients.",
    )
    parser.add_argument("--config", metavar="PATH", help="config file to use")
    parser.add_argument(
        "--headless", action="store_true", help="run without the GUI"
    )

    connection = parser.add_argument_group("connection")
    connection.add_argument(
        "--standalone",
        action="store_true",
        help=(
            "serve anyone with the password, without waiting for a Bluetooth "
            "server to take charge (testing)"
        ),
    )
    connection.add_argument(
        "--media-bind",
        metavar="HOST:PORT",
        help=f"where to serve media from (default 0.0.0.0:{DEFAULT_VIDEO_PORT})",
    )
    connection.add_argument(
        "--password",
        help="this video server's own password (or RBGC_PASSWORD)",
    )
    connection.add_argument(
        "--no-discovery",
        action="store_true",
        help="do not announce this machine on the LAN",
    )
    connection.add_argument("--broker", metavar="HOST[:PORT]", help="rendezvous broker")
    connection.add_argument("--room", metavar="CODE", help="broker room code")

    source = parser.add_argument_group("source")
    source.add_argument("--device", help="capture device name or path")
    source.add_argument("--audio-device", help="audio capture device")
    source.add_argument(
        "--backend", choices=["auto", "dshow", "v4l2", "lavfi"], help="capture backend"
    )
    source.add_argument(
        "--test-source",
        action="store_true",
        help="synthesize a test pattern instead of capturing (needs no hardware)",
    )
    source.add_argument("--width", type=int)
    source.add_argument("--height", type=int)
    source.add_argument("--fps", type=int)
    source.add_argument("--bitrate", type=int, metavar="KBPS")
    source.add_argument("--encoder", help="force an encoder (default: auto-detect)")
    source.add_argument(
        "--no-audio", action="store_true", help="do not capture or stream audio"
    )

    misc = parser.add_argument_group("misc")
    misc.add_argument(
        "--config-stdin",
        action="store_true",
        help="read one JSON settings document from stdin (used by the embedded host)",
    )
    misc.add_argument(
        "--supervised-by",
        type=int,
        metavar="PID",
        help=(
            "exit when this process goes away (used by the embedded host, so a "
            "server that is killed does not leave an encoder running)"
        ),
    )
    misc.add_argument(
        "--list-devices", action="store_true", help="print capture devices and exit"
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


def _split_host_port(value: str, default_port: int) -> tuple[str, int]:
    if ":" in value:
        host, _, port = value.rpartition(":")
        try:
            return host, int(port)
        except ValueError:
            return value, default_port
    return value, default_port


def apply_overrides(cfg: VideoServerConfig, args: argparse.Namespace) -> None:
    """Fold CLI arguments into the config for this run.

    Everything here is applied in memory only. The config is saved by the GUI
    when the operator changes something, never as a side effect of a flag --
    the client learned that lesson the hard way with `--backend synthetic`.
    """
    if args.standalone:
        cfg.standalone = True
    if args.no_discovery:
        cfg.discoverable = False
    if args.media_bind:
        cfg.media_bind_host, cfg.media_port = _split_host_port(
            args.media_bind, DEFAULT_VIDEO_PORT
        )
    if args.broker:
        cfg.broker_host, cfg.broker_port = _split_host_port(
            args.broker, video_config.DEFAULT_BROKER_PORT
        )
    if args.room:
        cfg.room_code = args.room

    password = args.password or os.environ.get("RBGC_PASSWORD")
    if password:
        cfg.password = password
    if args.password:
        print(
            "Warning: passing --password puts it in your shell history and the "
            "process list. RBGC_PASSWORD is safer.",
            file=sys.stderr,
        )

    settings = cfg.settings
    if args.backend:
        settings.backend = args.backend
    if args.device:
        settings.device = args.device
    if args.audio_device:
        settings.audio_device = args.audio_device
    if args.test_source:
        settings.test_source = True
    if args.width:
        settings.width = args.width
    if args.height:
        settings.height = args.height
    if args.fps:
        settings.fps = args.fps
    if args.bitrate:
        settings.bitrate_kbps = args.bitrate
    if args.encoder:
        settings.encoder = args.encoder
    if args.no_audio:
        settings.audio_enabled = False

    cfg.settings = settings.clamped()


def _read_stdin_settings(cfg: VideoServerConfig) -> None:
    """Take one JSON settings document from stdin, then stop reading it.

    How the Bluetooth server hands an embedded instance its initial config:
    a pipe rather than argv or a file, so nothing sensitive lands in the
    process list or on disk. Later changes arrive over the control channel, so
    embedded and external instances are configured through exactly one path.
    """
    try:
        raw = sys.stdin.read()
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read settings from stdin: %s", exc)
        return
    if not raw.strip():
        return
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("Settings on stdin were not valid JSON: %s", exc)
        return
    if isinstance(body, dict):
        cfg.settings = VideoSettings.from_dict(body.get("settings", body)).clamped()
        log.info("Applied settings from stdin")


def process_is_alive(pid: int) -> bool:
    """Whether ``pid`` still exists. Errs towards True if it cannot be told.

    Used only to notice a *departed* parent, so being wrong in the optimistic
    direction merely means we keep running -- which is the status quo, not a
    new failure.
    """
    if pid <= 0:
        return True

    if sys.platform == "win32":
        import ctypes

        SYNCHRONIZE = 0x00100000
        WAIT_OBJECT_0 = 0x0
        kernel32 = ctypes.windll.kernel32

        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            # Signalled means the process has exited.
            return kernel32.WaitForSingleObject(handle, 0) != WAIT_OBJECT_0
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)          # signal 0 only checks existence
    except ProcessLookupError:
        return False
    except PermissionError:
        return True              # exists, just not ours to signal
    return True


def _attach_console_if_needed() -> None:
    """Restore stdio for a windowed build. See client/main.py for the full why."""
    if sys.platform != "win32":
        return
    if sys.stdout is not None and sys.stderr is not None:
        return

    import ctypes

    try:
        ctypes.windll.kernel32.AttachConsole(-1)
    except Exception:
        pass

    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            try:
                stream = open("CONOUT$", "w", buffering=1)
            except OSError:
                stream = open(os.devnull, "w")
            setattr(sys, name, stream)


def run_headless(cfg: VideoServerConfig, args: argparse.Namespace) -> int:
    """Run the pipeline with no GUI, until interrupted."""
    from videoserver.control import ControlResponder
    from videoserver.pipeline import VideoServerApp

    problems = cfg.validate()
    if problems:
        for problem in problems:
            print(f"Config problem: {problem}", file=sys.stderr)
        return 2
    if not cfg.password:
        print(
            "No password set. Use --password, RBGC_PASSWORD, or the GUI.",
            file=sys.stderr,
        )
        return 2

    app = VideoServerApp(cfg)
    responder = ControlResponder(app)
    app.responder = responder

    stopping = False

    def handle_signal(_signum, _frame):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):
            pass

    beacon_loop = _start_beacon(app, cfg) if cfg.discoverable else None

    with high_resolution_timers():
        app.start()
        responder.start()

        if cfg.standalone:
            log.info("Standalone: serving anyone with the password")
        else:
            log.info(
                "Waiting for a Bluetooth server to connect on port %d", app.net.port
            )

        last_report = 0.0
        try:
            while not stopping:
                time.sleep(0.2)
                app.tick_governor()

                if args.supervised_by and not process_is_alive(args.supervised_by):
                    # Our supervisor is gone. Exiting rather than carrying on
                    # is the whole point: an orphaned encoder pins a core and
                    # keeps the media port bound, and the next server to start
                    # finds a stranger already answering on it -- which looks
                    # like a video server that streams undecodable rubbish,
                    # because it is an older build of one.
                    log.warning("Supervisor %d has gone; exiting", args.supervised_by)
                    break

                now = time.monotonic()
                if args.verbose and now - last_report >= 5.0:
                    last_report = now
                    status = app.status()
                    log.info(
                        "%s | %s | %d viewer(s) | %.1f fps | %d kbps | encode p50 %.1f ms",
                        status["encoder"] or "starting",
                        "controlled" if responder.connected else "unclaimed",
                        status["clients"],
                        status["fps"],
                        status["bitrate_kbps"],
                        status["encode_p50_ms"] or 0.0,
                    )
        except KeyboardInterrupt:
            return 130
        finally:
            responder.stop()
            app.stop()
            if beacon_loop is not None:
                beacon_loop()

    return 0


def _start_beacon(app, cfg: VideoServerConfig):
    """Run the LAN discovery beacon on its own event loop, in a thread.

    A thread rather than folding it into the main loop: the beacon is asyncio
    and everything else here is threads, and one small loop of its own is far
    less machinery than making the rest asyncio-aware.

    Returns a callable that shuts it down, or None if it could not start.
    """
    import asyncio
    import threading as _threading

    from videoserver.discovery import VideoDiscoveryBeacon

    loop = asyncio.new_event_loop()
    ready = _threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        beacon = VideoDiscoveryBeacon(app, port=cfg.discovery_port, name=cfg.name)
        loop.run_until_complete(beacon.start())
        ready.set()
        try:
            loop.run_forever()
        finally:
            beacon.close()
            loop.close()

    thread = _threading.Thread(target=run, name="vs-beacon", daemon=True)
    thread.start()
    if not ready.wait(timeout=5):
        log.warning("Discovery beacon did not start in time")

    def shutdown() -> None:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=3)

    return shutdown


def run_gui(cfg: VideoServerConfig, args: argparse.Namespace) -> int:
    try:
        from videoserver.gui import run
    except ImportError as exc:
        print(f"Could not start the GUI ({exc}).", file=sys.stderr)
        print("Install the client extras:  pip install -e '.[client,video]'", file=sys.stderr)
        print("Or run without a GUI:  rbgc-video --headless", file=sys.stderr)
        return 1
    return run(cfg, args)


def main(argv: list[str] | None = None) -> int:
    # Before parse_args: argparse writes usage errors to stderr, which a
    # windowed build does not have until this runs.
    _attach_console_if_needed()

    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    if args.list_devices:
        from videoserver.capture import enumerate_devices

        devices = enumerate_devices()
        if not devices:
            print("No capture devices found.")
        for entry in devices:
            print(f"  [{entry['kind']}] {entry['name']}")
        return 0

    path = Path(args.config) if args.config else None
    cfg = video_config.load(path)
    apply_overrides(cfg, args)

    if args.config_stdin:
        _read_stdin_settings(cfg)

    if args.headless:
        return run_headless(cfg, args)
    return run_gui(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())

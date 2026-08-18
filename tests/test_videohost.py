"""Embedded video: supervising the video server subprocess.

Spawns the real thing against a real server stack. The subprocess boundary is
not an implementation detail here -- it is the fix for a specific hazard (the
datapath disables the cyclic collector process-wide, and PyAV allocates per
frame), so the wiring across it is worth testing rather than assuming.

Failures this catches:

  * A child that cannot reach its own parent because the loopback allowance is
    not armed, which looks exactly like "video is broken".
  * A password that reaches the child through argv, where any local user can
    read it out of the process list.
  * A crash that is never noticed, leaving the operator with a dead stream and
    a UI that still says "embedded".
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

pytest.importorskip("av", reason="video extras not installed")

from server import config as server_config          # noqa: E402
from server.bt.profiles import create_profile       # noqa: E402
from server.bt.sink import MockSink                 # noqa: E402
from server.datapath import Datapath                # noqa: E402
from server.router import OutputChannel, Router     # noqa: E402
from server.sessions import SessionManager          # noqa: E402
from server.video import MODE_EMBEDDED, VideoRegistry  # noqa: E402
from server.videohost import EmbeddedVideoServer    # noqa: E402
from server.videolink import VideoLink              # noqa: E402
from common.video import VideoSettings              # noqa: E402

PASSWORD = "embedded-video-test-password"


@pytest.fixture
async def stack():
    """A server with the loopback allowance armed, as embedded mode leaves it."""
    router = Router()
    router.add_channel(
        OutputChannel(
            bd_addr="00:00:00:00:00:01",
            hci_name="mock0",
            profile=create_profile("generic"),
            sink=MockSink(name="mock0"),
        )
    )
    sessions = SessionManager(PASSWORD, auto_approve=True)
    registry = VideoRegistry(
        mode=MODE_EMBEDDED,
        settings=VideoSettings(
            test_source=True, width=320, height=240, fps=15,
            bitrate_kbps=1500, audio_enabled=False, preview_enabled=True,
            preview_fps=4,
        ),
    )
    datapath = Datapath(
        sessions, router, bind_host="127.0.0.1", bind_port=0,
        realtime=False, video_registry=registry,
    )
    datapath.start()
    # Both gates shut: embedded video must work on a server accepting nobody.
    datapath.set_accepting(lan=False, internet=False)
    datapath.allow_loopback_video = True

    # A real port, chosen by binding and releasing one: the parent connects to
    # the child now, so it has to know where the child will be. Ephemeral (0)
    # would leave the parent with nothing to dial.
    cfg = server_config.ServerConfig(
        password=PASSWORD,
        port=datapath.port,
        video_mode=MODE_EMBEDDED,
        video_host="127.0.0.1",
        video_port=_free_port(),
        video_password="embedded-video-password",
    )

    yield cfg, registry, datapath, sessions

    datapath.stop()


def _free_port() -> int:
    """A port nothing is using right now.

    Racy in principle, harmless in practice: the child binds it within a second
    and a collision only fails one test run.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def wait_for(predicate, timeout: float = 25.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return False


class TestArgv:
    def test_the_password_never_appears_on_the_command_line(self, stack):
        """argv is readable by any local user through ps."""
        cfg, registry, _datapath, _sessions = stack
        argv = EmbeddedVideoServer(cfg, registry).build_argv()
        assert PASSWORD not in " ".join(argv)

    def test_it_runs_our_own_interpreter(self, stack):
        """A console script may not be on PATH, or may be a different venv."""
        cfg, registry, _datapath, _sessions = stack
        argv = EmbeddedVideoServer(cfg, registry).build_argv()
        assert argv[0] == sys.executable
        assert argv[1:3] == ["-m", "videoserver.main"]

    def test_the_child_is_told_where_to_serve_media(self, stack):
        """It no longer dials us -- we dial it -- so the port is what matters."""
        cfg, registry, _datapath, _sessions = stack
        argv = EmbeddedVideoServer(cfg, registry).build_argv()
        assert f"0.0.0.0:{cfg.video_port}" in argv
        assert "--config-stdin" in argv
        assert "--headless" in argv

    def test_the_child_does_not_advertise_itself(self, stack):
        """It is ours and local; offering it on the LAN would only confuse."""
        cfg, registry, _datapath, _sessions = stack
        assert "--no-discovery" in EmbeddedVideoServer(cfg, registry).build_argv()

    def test_the_video_password_is_used_not_the_players(self, stack):
        cfg, registry, _datapath, _sessions = stack
        argv = EmbeddedVideoServer(cfg, registry).build_argv()
        assert PASSWORD not in " ".join(argv)
        assert cfg.video_password not in " ".join(argv)


class _Embedded:
    """Both halves of embedded mode: the child, and our link to it.

    server/main.py starts these together, and neither is much use alone -- the
    child streams to nobody, and the link has nothing to dial. Tests that care
    about the contract between them want both.
    """

    def __init__(self, cfg, registry, datapath):
        self.host = EmbeddedVideoServer(cfg, registry)
        self.link = VideoLink(registry, datapath, cfg)

    async def start(self):
        await self.host.start()
        self.link.start()

    async def stop(self):
        self.link.stop()
        await self.host.stop()

    def __getattr__(self, name):
        # Supervision state lives on the host half.
        return getattr(self.host, name)


class TestSupervision:
    async def test_the_child_connects_back_and_reports(self, stack):
        """The whole embedded contract, end to end."""
        cfg, registry, _datapath, sessions = stack
        embedded = _Embedded(cfg, registry, _datapath)
        await embedded.start()
        try:
            assert await wait_for(lambda: registry.has_source), (
                "the embedded video server never connected to its parent"
            )
            assert await wait_for(lambda: registry.is_live), (
                "the source connected but never reported status"
            )
            # Status arrives before the encoder has finished opening, so wait
            # for the stream itself rather than the first report.
            assert await wait_for(
                lambda: registry.snapshot()["status"].get("streaming")
            ), "the source never started streaming"

            status = registry.snapshot()["status"]
            assert status["encoder"], "no encoder was chosen"
            assert sessions.controller_count == 0, "the source took a player slot"
        finally:
            await embedded.stop()

    async def test_it_applies_the_settings_it_was_handed_on_stdin(self, stack):
        cfg, registry, _datapath, _sessions = stack
        embedded = _Embedded(cfg, registry, _datapath)
        await embedded.start()
        try:
            assert await wait_for(lambda: registry.is_live)
            status = registry.snapshot()["status"]
            assert status["width"] == 320
            assert status["height"] == 240
        finally:
            await embedded.stop()

    async def test_the_preview_reaches_the_server(self, stack):
        """The one media path that legitimately crosses the Bluetooth server."""
        cfg, registry, _datapath, _sessions = stack
        embedded = _Embedded(cfg, registry, _datapath)
        await embedded.start()
        try:
            assert await wait_for(lambda: registry.preview() is not None), (
                "no preview frame arrived"
            )
            frame = registry.preview()
            assert frame.startswith(b"\xff\xd8"), "preview is not a JPEG"
            assert frame.endswith(b"\xff\xd9")
        finally:
            await embedded.stop()

    async def test_a_crash_is_noticed_and_restarted(self, stack):
        cfg, registry, _datapath, _sessions = stack
        embedded = _Embedded(cfg, registry, _datapath)
        await embedded.start()
        try:
            assert await wait_for(lambda: embedded.is_running)
            first_pid = embedded.snapshot()["pid"]

            embedded.host._process.kill()
            assert await wait_for(lambda: embedded.restarts >= 1, timeout=20.0), (
                "the supervisor did not notice the child dying"
            )
            assert await wait_for(
                lambda: embedded.is_running
                and embedded.snapshot()["pid"] != first_pid,
                timeout=20.0,
            ), "it was never restarted"
        finally:
            await embedded.stop()

    async def test_stop_leaves_no_process_behind(self, stack):
        cfg, registry, _datapath, _sessions = stack
        embedded = _Embedded(cfg, registry, _datapath)
        await embedded.start()
        assert await wait_for(lambda: embedded.is_running)

        await embedded.stop()
        assert embedded.is_running is False
        assert registry.embedded_state["running"] is False

    async def test_stopping_detaches_the_source(self, stack):
        cfg, registry, _datapath, _sessions = stack
        embedded = _Embedded(cfg, registry, _datapath)
        await embedded.start()
        try:
            assert await wait_for(lambda: registry.is_live)
        finally:
            await embedded.stop()

        # The session reaper drops it once the child stops heartbeating.
        assert await wait_for(lambda: not registry.is_live, timeout=20.0)


class TestTheChildDoesNotOutliveItsParent:
    """stop() only runs on a graceful shutdown. A SIGKILL, a crash, or anything
    at all on Windows leaves the child running -- encoding forever, pinning a
    core, and holding the media port.

    That is not a tidy-up nicety: the next server to start finds a stranger
    already bound to its port. On Windows, where SO_REUSEADDR lets both bind,
    clients are then served by whichever process wins -- observed for real as a
    stream of undecodable video, because the orphan was an older build.
    """

    def test_the_child_is_told_who_supervises_it(self, stack):
        cfg, registry, _datapath, _sessions = stack
        argv = EmbeddedVideoServer(cfg, registry).build_argv()

        assert "--supervised-by" in argv
        assert argv[argv.index("--supervised-by") + 1] == str(os.getpid())

    def test_a_live_process_reads_as_alive(self):
        from videoserver.main import process_is_alive

        assert process_is_alive(os.getpid()) is True

    def test_a_departed_process_reads_as_gone(self):
        import subprocess
        import sys as _sys

        from videoserver.main import process_is_alive

        # Popen rather than run(), so the pid is available and the liveness
        # check can be exercised on both sides of the exit.
        process = subprocess.Popen([_sys.executable, "-c", "import sys; sys.stdin.read()"],
                                   stdin=subprocess.PIPE)
        try:
            assert process_is_alive(process.pid) is True
        finally:
            process.stdin.close()
            process.wait(timeout=10)

        assert process_is_alive(process.pid) is False

    def test_an_implausible_pid_does_not_kill_us(self):
        """Erring towards 'alive' keeps a bad answer from stopping the stream."""
        from videoserver.main import process_is_alive

        assert process_is_alive(0) is True
        assert process_is_alive(-1) is True

    async def test_the_child_exits_when_its_supervisor_disappears(self, stack):
        """The behaviour itself, with a real orphaned process."""
        import asyncio as _asyncio
        import subprocess
        import sys as _sys

        cfg, _registry, _datapath, _sessions = stack

        # A stand-in supervisor that exits on demand, so the video server is
        # genuinely orphaned rather than merely told it was.
        supervisor = subprocess.Popen(
            [_sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
        )

        child = await _asyncio.create_subprocess_exec(
            _sys.executable, "-m", "videoserver.main",
            "--headless", "--standalone", "--test-source",
            "--media-bind", "127.0.0.1:0",
            "--width", "320", "--height", "240", "--fps", "15",
            "--supervised-by", str(supervisor.pid),
            env={**os.environ, "RBGC_PASSWORD": PASSWORD},
            stdout=_asyncio.subprocess.DEVNULL,
            stderr=_asyncio.subprocess.DEVNULL,
        )
        try:
            # Let it get going, and confirm it stays up while supervised.
            await _asyncio.sleep(3.0)
            assert child.returncode is None, "it exited while its supervisor was alive"

            supervisor.stdin.close()
            supervisor.wait(timeout=10)

            await _asyncio.wait_for(child.wait(), timeout=20)
            assert child.returncode is not None
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
            if supervisor.poll() is None:
                supervisor.kill()
                supervisor.wait()

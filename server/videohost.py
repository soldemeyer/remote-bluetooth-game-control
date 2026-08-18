"""Supervising an embedded video server.

Embedded mode runs the *same* program a standalone install runs, as a child
process. That is a deliberate constraint rather than an implementation detail:

  * **The datapath disables the cyclic garbage collector** for the life of this
    process (see ``configure_gc_for_realtime``). PyAV allocates per frame, so
    encoding in-process would accumulate uncollectable cycles indefinitely.
  * An encoder pinning a core is a problem for whatever shares its process;
    a child can be scheduled, niced and killed independently.
  * One code path. An embedded source and an external one are configured,
    authenticated and monitored identically -- the only difference is who
    started the process.

The child connects back to us over loopback like any other video server, which
is why the datapath grows a narrow loopback allowance in embedded mode.

Everything here runs on the server's existing asyncio thread. Nothing touches
the datapath thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys

log = logging.getLogger("rbgc.videohost")

#: Restart backoff. Reset once the child has been healthy for a while, so a
#: process that crashes nightly does not creep up to the longest delay.
_RESTART_DELAYS = (1.0, 2.0, 5.0, 30.0)
_HEALTHY_AFTER_S = 60.0

#: How long to wait for a polite exit before killing it.
_TERM_TIMEOUT_S = 5.0


class EmbeddedVideoServer:
    """Starts, watches and stops the video server subprocess."""

    def __init__(self, cfg, registry) -> None:
        self._cfg = cfg
        self._registry = registry

        self._process: asyncio.subprocess.Process | None = None
        self._supervisor: asyncio.Task | None = None
        self._readers: list[asyncio.Task] = []
        self._stopping = False

        self.restarts = 0
        self.last_exit_code: int | None = None
        self.started_at: float = 0.0

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._supervisor is not None:
            return
        self._stopping = False
        self._supervisor = asyncio.create_task(self._supervise(), name="video-supervisor")
        log.info("Embedded video server starting")

    async def stop(self) -> None:
        self._stopping = True

        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor

        await self._terminate()
        self._publish_state()
        log.info("Embedded video server stopped")

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    # -- the supervisor ----------------------------------------------------

    async def _supervise(self) -> None:
        attempt = 0
        while not self._stopping:
            started = asyncio.get_running_loop().time()
            try:
                await self._spawn()
            except Exception as exc:  # noqa: BLE001
                log.error("Could not start the video server: %s", exc)
                log.debug("Spawn failed", exc_info=True)
                self._publish_state(error=str(exc))
                if await self._wait_backoff(attempt):
                    return
                attempt += 1
                continue

            self._publish_state()
            code = await self._process.wait() if self._process else -1
            self.last_exit_code = code

            if self._stopping:
                return

            ran_for = asyncio.get_running_loop().time() - started
            if ran_for >= _HEALTHY_AFTER_S:
                attempt = 0      # it was healthy; treat this as a first failure

            self.restarts += 1
            if ran_for < 2.0:
                # Too fast to have been running. Almost always a configuration
                # the child refused, and restarting will not fix it -- so say
                # so at a level the operator will actually see, and point at
                # the child's own output, which carries the real reason.
                log.error(
                    "Video server exited immediately (code %s). It is being restarted, "
                    "but this is usually a configuration problem -- see the 'video:' "
                    "lines above for what it objected to.",
                    code,
                )
            else:
                log.warning(
                    "Video server exited with code %s after %.0fs; restarting",
                    code, ran_for,
                )
            self._publish_state()
            if await self._wait_backoff(attempt):
                return
            attempt += 1

    async def _wait_backoff(self, attempt: int) -> bool:
        """Sleep before the next attempt. True if we were cancelled meanwhile."""
        delay = _RESTART_DELAYS[min(attempt, len(_RESTART_DELAYS) - 1)]
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return True
        return self._stopping

    async def _spawn(self) -> None:
        argv = self.build_argv()
        env = dict(os.environ)
        # The *video* password, not the players' one: we connect to the child
        # as its controller, exactly as we would to a video server on another
        # machine. Passed via the environment, never argv, because the process
        # list is readable by any local user.
        if self._cfg.video_password:
            env["RBGC_PASSWORD"] = self._cfg.video_password

        log.info("Launching: %s", " ".join(argv[1:]))
        self._process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self.started_at = asyncio.get_running_loop().time()

        await self._send_settings()
        self._readers = [
            asyncio.create_task(self._pump_output(self._process.stdout, logging.INFO)),
            # stderr at WARNING: this is where the child reports a config it
            # refused, and that has to reach an operator who is not running the
            # server with -v.
            asyncio.create_task(self._pump_output(self._process.stderr, logging.WARNING)),
        ]

    def build_argv(self) -> list[str]:
        """The child's command line.

        ``sys.executable -m`` rather than the console script: the script may
        not be on PATH, and this guarantees the child runs in the same
        interpreter and virtualenv we do.
        """
        return [
            sys.executable,
            "-m",
            "videoserver.main",
            "--headless",
            "--media-bind",
            f"0.0.0.0:{self._cfg.video_port}",
            # It is our own child on this machine; announcing it to the LAN
            # would only offer the operator a second way to find something they
            # already have.
            "--no-discovery",
            "--config-stdin",
            # So the child exits if we are killed outright. terminate() and
            # stop() only run on a graceful shutdown; a SIGKILL, a crash, or
            # anything on Windows leaves the child running -- encoding forever
            # and holding the media port, where the next server to start finds
            # a stranger already answering on it.
            "--supervised-by",
            str(os.getpid()),
            "-v",
        ]

    async def _send_settings(self) -> None:
        """Hand over the initial configuration on stdin, then close it.

        A pipe rather than argv or a file: nothing sensitive reaches the
        process list or the disk. Everything after this arrives over the normal
        control channel, so there is exactly one configuration path.
        """
        process = self._process
        if process is None or process.stdin is None:
            return

        settings = self._registry.settings.to_dict()
        document = json.dumps({"mode": "embedded", "settings": settings})
        try:
            process.stdin.write(document.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            log.debug("Video server closed stdin before settings were sent")
        finally:
            with contextlib.suppress(Exception):
                process.stdin.close()

    async def _pump_output(self, stream, level: int) -> None:
        """Re-log the child's output so it appears in the server's own log."""
        if stream is None:
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    log.log(level, "video: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("Error reading video server output", exc_info=True)

    async def _terminate(self) -> None:
        for reader in self._readers:
            reader.cancel()
        self._readers.clear()

        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return

        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=_TERM_TIMEOUT_S)
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("Video server did not exit; killing it")
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()

    # -- introspection -----------------------------------------------------

    def _publish_state(self, error: str = "") -> None:
        """Mirror our state into the registry so the web GUI can show it."""
        self._registry.embedded_state = self.snapshot(error)

    def snapshot(self, error: str = "") -> dict:
        process = self._process
        return {
            "running": self.is_running,
            "pid": process.pid if process is not None else None,
            "restarts": self.restarts,
            "last_exit_code": self.last_exit_code,
            "error": error,
        }

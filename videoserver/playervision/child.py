"""The vision worker, as its own process.

``python -m videoserver.playervision.child`` -- spawned by ``ProcessRunner``,
never run by hand except to debug one. A packaged video server has no ``-m``:
there ``sys.executable`` is the video server itself, so it is started with
:data:`WORKER_FLAG` first and ``videoserver.main`` hands straight to
:func:`main` -- see ``runner.worker_command``.

WHY THIS IS A PROCESS AND NOT A THREAD
----------------------------------------
Everything above this is written so it cannot disturb the stream: the worker
never raises at its caller, the backend is given up on after repeated
failures, and the sampler returns before touching a frame when the feature is
off. None of that survives a **CUDA kernel fault or a driver reset**, which
does not raise -- it takes the process down, and in external mode nothing
restarts a video server somebody else's machine is running.

So a model backend gets a process boundary. What it buys, precisely:

  * a fault kills this and not the stream, and the supervisor restarts it;
  * ON to OFF releases device memory by exiting, which no framework
    reliably does on session close;
  * the heavy dependency is not in the video server's import graph at all.

THE SHAPE, AND THE ONE RULE
-----------------------------
Frames arrive through a shared-memory slot -- latest-wins, never a queue, and
the parent never waits on us. Configuration arrives as JSON lines on stdin.
Results leave as JSON lines on stdout. Logs go to stderr and the parent
re-logs them.

**Nothing here may ever block the parent.** It cannot: the parent only ever
writes the slot, which overwrites, and drains our stdout on its own thread. If
this process hangs, the stream does not notice and the supervisor eventually
does.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

__all__ = ["WORKER_FLAG", "main", "run"]

#: The first argument that turns a packaged video server into this worker.
#:
#: **Here, not in the runner**, because this module imports nothing heavy and
#: `videoserver.main` reads it before it has decided whether it is a GUI, a
#: headless server or a worker. The packaged build used to be launched as
#: ``rbgc-video.exe -m videoserver.playervision.child``, which the video
#: server's own argument parser refused with a usage error -- so identification
#: never ran in any packaged build, and the parent's wait for it froze the
#: status message the Bluetooth server depends on. See `runner.worker_command`.
WORKER_FLAG = "--playervision-worker"

#: How often the slot is checked for a new frame.
#:
#: Frames arrive at `player_id_hz` -- six a second by default -- so nearly
#: every poll finds nothing and costs two integer reads. Short enough that a
#: frame is picked up promptly, long enough that an idle worker is invisible
#: in a process list.
POLL_S = 0.004

#: How often the parent is checked for still being alive, in poll ticks.
#:
#: The same guarantee `--supervised-by` gives the embedded video server: a
#: parent that is killed outright runs no teardown, and a worker holding a GPU
#: session forever afterwards is exactly the orphan this project has already
#: had to chase once.
PARENT_CHECK_TICKS = 250


def _parent_alive(pid: int) -> bool:
    if not pid:
        return True
    if os.name == "nt":
        import ctypes

        # A handle we cannot open is a process we cannot see; treat that as
        # gone rather than staying alive forever on a permissions quirk.
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259            # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return True
    return True


class _Inbox:
    """Configuration from the parent, read off stdin on its own thread.

    A thread because the main loop is polling shared memory and must not sit
    in a blocking read: a parent that goes quiet for a second would otherwise
    stall every frame in that second.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[dict] = []
        self.closed = False
        self._thread = threading.Thread(target=self._run, name="pv-stdin", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                with self._lock:
                    self._pending.append(message)
        self.closed = True

    def drain(self) -> list[dict]:
        with self._lock:
            pending, self._pending = self._pending, []
        return pending


def _emit(payload: dict) -> bool:
    """One JSON line to the parent. ``False`` once the pipe has gone."""
    try:
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return True
    except (BrokenPipeError, ValueError, OSError):
        return False


def run(slot_name: str, backend_name: str, *, confidence: float = 0.6,
        model_dir: str = "", parent_pid: int = 0, backend_module: str = "") -> int:
    """The worker loop. Returns a process exit code."""
    import logging

    from .backends.base import SampleFrame
    from .service import resolve_backend
    from .shm import FrameSlot
    from .types import InputTrace, PlayerHint
    from .worker import VisionWorker

    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("rbgc.playervision.child")

    if model_dir:
        from .backends.onnx import ENV_MODEL_DIR

        os.environ[ENV_MODEL_DIR] = model_dir

    if backend_module:
        # A backend registered in the parent -- in practice only the tests'
        # stand-in detector. This process has its own, empty registry, so it
        # is imported and registered again here. The argument comes from the
        # parent's argv, never from settings or the wire.
        _register(backend_module, log)

    backend, caps = resolve_backend(backend_name)
    if caps.available:
        try:
            caps = backend.start() or caps
        except Exception as exc:  # noqa: BLE001
            log.error("Backend %s would not start: %s", backend.name, exc)
            _emit({"t": "caps", "caps": {
                "backend": backend.name, "available": False,
                "reason": str(exc), "device": "", "embeddings": False}})
            return 3

    # Sent whether or not it worked: the parent has to be able to tell "not
    # available, here is why" from "the child never spoke", and only one of
    # those is worth restarting.
    _emit({"t": "caps", "caps": caps.as_dict()})
    if not caps.available:
        return 4

    worker = VisionWorker(backend, confidence=confidence)
    slot = FrameSlot(slot_name)
    inbox = _Inbox()
    inbox.start()

    ticks = 0
    try:
        while True:
            ticks += 1
            if ticks % PARENT_CHECK_TICKS == 0 and not _parent_alive(parent_pid):
                log.info("Parent %s has gone; exiting", parent_pid)
                return 0
            if inbox.closed:
                # stdin closed: the parent is shutting us down politely.
                return 0

            applied = False
            for message in inbox.drain():
                if message.get("t") == "stop":
                    return 0
                _apply(worker, message, PlayerHint, InputTrace)
                applied = True
            if applied:
                # Reported straight away rather than waiting for the next
                # frame. After a restart the parent replays everything it had
                # told us, and its view of this worker would otherwise stay
                # empty until frames happened to flow again -- which reads as
                # the configuration not having arrived.
                _emit({"t": "state", "s": worker.snapshot()})

            frame = slot.read()
            if frame is None:
                time.sleep(POLL_S)
                continue

            payload, width, height, stride, channels, capture_ts = frame
            rows = worker.process(
                SampleFrame(
                    memoryview(payload), width, height, stride,
                    capture_ts=capture_ts,
                    pixel_format="rgb24" if channels == 3 else "gray",
                ),
                capture_ts,
            )
            state = worker.snapshot()
            # Our side of the slot, which is the half that can tell a worker
            # falling behind from one that is not being given frames. The
            # parent cannot see it: it only ever writes.
            state["slot_reads"] = slot.reads
            state["slot_torn"] = slot.torn
            if not _emit({
                "t": "rows",
                "pts": capture_ts,
                "r": [_row(row) for row in rows],
                "j": [_judgement(j) for j in worker.judgements()],
                "s": state,
            }):
                return 0
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            backend.stop()
        except Exception:  # noqa: BLE001
            pass
        slot.close()


def _register(spec: str, log) -> None:
    import importlib

    from .service import register_backend

    module_name, _, class_name = spec.partition(":")
    try:
        register_backend(getattr(importlib.import_module(module_name), class_name))
    except Exception as exc:  # noqa: BLE001 -- reported as unavailable instead
        log.error("Could not import backend %s: %s", spec, exc)


def _apply(worker, message: dict, PlayerHint, InputTrace) -> None:
    """Fold one configuration message in. Never raises: a malformed one costs
    that update and nothing else."""
    try:
        kwargs: dict = {}
        if "layout" in message:
            kwargs["layout"] = str(message["layout"])
        if "confidence" in message:
            kwargs["confidence"] = float(message["confidence"])
        if "hints" in message:
            kwargs["hints"] = tuple(
                PlayerHint(player_id=int(pid), regions=tuple(regions))
                for pid, regions in message["hints"]
            )
        if "traces" in message:
            kwargs["traces"] = tuple(
                InputTrace(
                    player_id=int(pid), hz=float(hz),
                    samples=tuple((float(dx), float(dy)) for dx, dy in samples),
                )
                for pid, hz, samples in message["traces"]
            )
        if "active" in message:
            kwargs["active"] = tuple(float(value) for value in message["active"])[:4]
        if isinstance(message.get("tuning"), dict):
            kwargs["tuning"] = message["tuning"]
        if message.get("reset_learning"):
            kwargs["reset_learning"] = True
        if kwargs:
            worker.configure(**kwargs)
        if "hints" in kwargs:
            worker.forget_absent_players()
    except Exception:  # noqa: BLE001
        pass


def _judgement(judgement) -> list:
    """One track's reasoning, compactly. The parent rebuilds a Judgement.

    This is the one thing crossing here that is *not* on a byte budget: it
    goes down a pipe to our own parent, never onto the wire, so it carries the
    losing scores in full. `Judgement`'s docstring says why that distinction
    matters.
    """
    return [
        judgement.track_id, judgement.player_id,
        round(judgement.confidence, 4), judgement.source, judgement.region,
        judgement.note,
        [
            [s.signal, s.player_id, round(s.score, 4), 1 if s.used else 0, s.note]
            for s in judgement.scores
        ],
    ]


def _row(row) -> list:
    """One published row, compactly. The parent rebuilds a TrackedPlayer."""
    box = row.box
    return [
        row.track_id, row.player_id, row.region,
        round(box.x, 5), round(box.y, 5), round(box.width, 5), round(box.height, 5),
        round(row.confidence, 4), row.source,
    ]


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="videoserver.playervision.child",
        description="The player-identification worker. Spawned by the video "
                    "server; not meant to be run by hand.",
    )
    parser.add_argument("--slot", required=True, help="shared-memory slot name")
    parser.add_argument("--backend", default="auto")
    parser.add_argument("--confidence", type=float, default=0.6)
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--supervised-by", type=int, default=0, metavar="PID")
    parser.add_argument("--backend-module", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    return run(
        args.slot, args.backend,
        confidence=args.confidence,
        model_dir=args.model_dir,
        parent_pid=args.supervised_by,
        backend_module=args.backend_module,
    )


if __name__ == "__main__":
    raise SystemExit(main())

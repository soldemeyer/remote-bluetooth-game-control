"""Two ways to drive one worker, and how the choice is made.

``VisionWorker`` is pure: frame in, published rows out, no sockets and no
threads. This is what feeds it -- either on the calling thread, or in another
process -- and the choice is the **backend's**, through ``isolated``.

WHY THE BACKEND DECIDES
-------------------------
Nothing in the no-model backend can fault a GPU driver, so isolating it would
buy a process boundary, a shared-memory segment and a supervisor in exchange
for nothing, on a feature that is off by default. Nothing in a model backend
can be *relied on* not to: a CUDA kernel fault or a driver reset does not
raise, it takes the process down -- and in external mode the video server is
on somebody else's machine, where nothing restarts it.

So ``isolated`` is a property of the backend rather than a setting, and the
service reads it. One flag, one place, and no way for the two to disagree.

THE CONTRACT BOTH HONOUR
--------------------------
``submit`` never blocks and never waits for a result. Inline that is trivially
true -- the work happens and the rows come back. Across a process it means the
rows returned belong to some *earlier* frame, and that is correct rather than
a compromise: these are identities, they are already several frames old by the
time a client draws them, and a submit that waited would put a model's
scheduling on the thread that sends the status message.

``latest`` is therefore the real accessor, and ``submit`` returns the same
thing for the convenience of a caller that has just handed a frame over.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time

from common.screen_regions import Rect

from .backends.base import Capabilities, SampleFrame
from .shm import MAX_PAYLOAD
from .types import Judgement, SignalScore, TrackedPlayer

log = logging.getLogger(__name__)

__all__ = ["InlineRunner", "ProcessRunner", "Runner", "make_runner"]

#: Restart backoff for a worker that exits. The same ladder
#: ``server/videohost.py`` uses, and for the same reason: a process that dies
#: nightly should not creep up to the longest delay.
RESTART_DELAYS = (1.0, 2.0, 5.0, 15.0)

#: Ran for at least this long before exiting? Treat the next failure as a
#: first failure.
HEALTHY_AFTER_S = 60.0

#: Exits this quickly, this many times in a row, and it is a configuration
#: fault rather than a crash. Restarting will not fix it, and retrying for
#: ever would spend the machine's time producing nothing while every counter
#: reads healthy.
IMMEDIATE_S = 3.0
MAX_IMMEDIATE_FAILURES = 4

#: How long a polite shutdown is given before the worker is killed.
TERM_TIMEOUT_S = 3.0


class Runner:
    """What the service talks to. Neither half may raise at it."""

    def start(self, backend_name: str, confidence: float) -> Capabilities:
        raise NotImplementedError

    def configure(self, **kwargs) -> None:
        raise NotImplementedError

    def submit(self, frame: SampleFrame, now_ns: int) -> list[TrackedPlayer]:
        raise NotImplementedError

    def latest(self) -> list[TrackedPlayer]:
        raise NotImplementedError

    def judgements(self) -> list[Judgement]:
        """Why the last round came out as it did. Local to this machine.

        Empty is a legitimate answer for a runner that cannot produce it, so
        this is concrete rather than abstract -- a debug view must degrade to
        showing less, never to raising.
        """
        return []

    def stop(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict[str, object]:
        raise NotImplementedError


class InlineRunner(Runner):
    """The worker on the calling thread. For backends with nothing to isolate."""

    def __init__(self, backend, worker) -> None:
        self._backend = backend
        self._worker = worker
        self._rows: list[TrackedPlayer] = []

    def start(self, backend_name: str, confidence: float) -> Capabilities:
        return self._backend.start()

    def configure(self, **kwargs) -> None:
        self._worker.configure(**kwargs)
        if kwargs.get("hints") is not None:
            self._worker.forget_absent_players()

    def submit(self, frame: SampleFrame, now_ns: int) -> list[TrackedPlayer]:
        self._rows = self._worker.process(frame, now_ns)
        return self._rows

    def latest(self) -> list[TrackedPlayer]:
        return list(self._rows)

    def judgements(self) -> list[Judgement]:
        return self._worker.judgements()

    def stop(self) -> None:
        try:
            self._backend.stop()
        except Exception:  # noqa: BLE001
            log.debug("Backend stop failed", exc_info=True)

    def snapshot(self) -> dict[str, object]:
        report = dict(self._worker.snapshot())
        report["runner"] = "inline"
        return report


class ProcessRunner(Runner):
    """The worker in its own process, supervised.

    Frames cross through a shared-memory slot that always holds the newest and
    never queues; results come back as JSON lines on the child's stdout,
    drained by a reader thread so nothing here ever waits on it.
    """

    def __init__(self, *, model_dir: str = "") -> None:
        self._model_dir = model_dir
        self._slot = None
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._errors: threading.Thread | None = None

        self._lock = threading.Lock()
        self._rows: list[TrackedPlayer] = []
        self._judgements: list[Judgement] = []
        self._child_snapshot: dict[str, object] = {}
        self._caps = Capabilities()
        self._caps_seen = threading.Event()

        #: What the child has been told, replayed after every restart. A
        #: worker that came back knowing nothing would identify nobody until
        #: the Bluetooth server's next slow push -- which is the same bug the
        #: service already carries this state to avoid.
        self._config: dict[str, object] = {}

        self._backend_name = "auto"
        self._confidence = 0.6
        self._stopping = False
        self._attempt = 0
        self._immediate = 0
        self._started_at = 0.0
        #: When the last worker was noticed to have died. The backoff is
        #: timed from here rather than from when it started, or a worker that
        #: ran for an hour would be restarted instantly and one that died at
        #: once would never be restarted at all.
        self._dead_at = 0.0
        self.restarts = 0
        self.failed = ""
        #: Set once if a frame was ever too large for the slot. A string
        #: rather than a flag, because the useful part is the size.
        self.oversized_reason = ""

    # -- lifecycle ---------------------------------------------------------

    def start(self, backend_name: str, confidence: float) -> Capabilities:
        from .shm import FrameSlot

        self._backend_name = backend_name
        self._confidence = confidence
        self._stopping = False
        try:
            self._slot = FrameSlot(create=True)
        except Exception as exc:  # noqa: BLE001
            return Capabilities(
                backend=backend_name, available=False,
                reason=f"could not create the frame slot: {exc}",
            )

        if not self._spawn():
            return self._caps

        # Waited for exactly once, and only here: the operator has just asked
        # for this and an answer of "starting..." that never resolves is worse
        # than a few seconds. Every later restart is silent.
        if not self._caps_seen.wait(timeout=30.0):
            self.stop()
            return Capabilities(
                backend=backend_name, available=False,
                reason="the worker did not report within 30 s",
            )
        return self._caps

    def _spawn(self) -> bool:
        argv = [
            sys.executable, "-m", "videoserver.playervision.child",
            "--slot", self._slot.name,
            "--backend", self._backend_name,
            "--confidence", str(self._confidence),
            # So a worker outlives neither a graceful shutdown nor a kill:
            # `stop()` only runs on the first, and a GPU session held for ever
            # afterwards is the orphan this project has already chased once.
            "--supervised-by", str(os.getpid()),
        ]
        if self._model_dir:
            argv += ["--model-dir", self._model_dir]

        try:
            self._process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # No console window for the child on Windows: every stream is
                # piped, so it has nothing to show one for.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # noqa: BLE001
            self._caps = Capabilities(
                backend=self._backend_name, available=False,
                reason=f"could not start the worker: {exc}",
            )
            self._caps_seen.set()
            return False

        self._started_at = time.monotonic()
        self._dead_at = 0.0
        self._reader = threading.Thread(
            target=self._read_stdout, name="pv-rows", daemon=True
        )
        self._reader.start()
        self._errors = threading.Thread(
            target=self._read_stderr, name="pv-log", daemon=True
        )
        self._errors.start()
        self._replay_config()
        return True

    def stop(self) -> None:
        self._stopping = True
        process, self._process = self._process, None
        if process is not None:
            # stdin closing is the polite signal; the child returns from its
            # loop on it. Killing follows if it does not.
            try:
                if process.stdin:
                    process.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                process.wait(timeout=TERM_TIMEOUT_S)
            except Exception:  # noqa: BLE001
                try:
                    process.kill()
                    process.wait(timeout=TERM_TIMEOUT_S)
                except Exception:  # noqa: BLE001
                    pass

        slot, self._slot = self._slot, None
        if slot is not None:
            slot.close()
        with self._lock:
            self._rows = []
            self._judgements = []

    # -- configuration -----------------------------------------------------

    def configure(self, **kwargs) -> None:
        message: dict[str, object] = {}
        if kwargs.get("layout") is not None:
            message["layout"] = kwargs["layout"]
        if kwargs.get("confidence") is not None:
            message["confidence"] = float(kwargs["confidence"])
        if kwargs.get("hints") is not None:
            message["hints"] = [
                [hint.player_id, list(hint.regions)] for hint in kwargs["hints"]
            ]
        if kwargs.get("traces") is not None:
            message["traces"] = [
                [trace.player_id, trace.hz, [list(s) for s in trace.samples]]
                for trace in kwargs["traces"]
            ]
        if not message:
            return

        # Remembered before it is sent, so a restart replays it. Merged rather
        # than replaced: the three fields arrive on three different messages
        # at three different rates.
        self._config.update(message)
        self._send(message)

    def _replay_config(self) -> None:
        if self._config:
            self._send(dict(self._config))

    def _send(self, message: dict) -> None:
        process = self._process
        if process is None or process.stdin is None:
            return
        try:
            process.stdin.write(
                (json.dumps(message, separators=(",", ":")) + "\n").encode()
            )
            process.stdin.flush()
        except Exception:  # noqa: BLE001 -- a dead child is the supervisor's problem
            log.debug("Could not configure the worker", exc_info=True)

    # -- the work ----------------------------------------------------------

    def submit(self, frame: SampleFrame, now_ns: int) -> list[TrackedPlayer]:
        """Publish a frame and hand back whatever the worker last said.

        The rows belong to an earlier frame, which is correct: waiting would
        put a model's scheduling on the thread that sends the status message,
        and these are identities rather than positions -- already several
        frames old by the time a client draws them.
        """
        self._reap()
        slot = self._slot
        if slot is not None and not slot.write(
            frame.data, frame.width, frame.height, frame.stride,
            frame.channels, now_ns,
        ) and not self.oversized_reason:
            # The frame does not fit the slot, so the worker is being given
            # nothing at all -- and every other counter reads healthy while
            # that happens, which is the trap `shm.py`'s own docstring warns
            # about. Said **once**: this runs several times a second, the same
            # discipline `_reap` documents just below.
            self.oversized_reason = (
                f"frames are {frame.width}x{frame.height} "
                f"({frame.stride * frame.height} bytes), larger than the "
                f"worker's {MAX_PAYLOAD}-byte buffer"
            )
            log.error(
                "Player identification is receiving no frames: %s. Video, "
                "audio and controllers are unaffected.", self.oversized_reason,
            )
        return self.latest()

    def latest(self) -> list[TrackedPlayer]:
        with self._lock:
            return list(self._rows)

    def judgements(self) -> list[Judgement]:
        with self._lock:
            return list(self._judgements)

    # -- supervision -------------------------------------------------------

    def _reap(self) -> None:
        """Notice a worker that has exited, and decide whether to try again.

        Called from ``submit``, so it runs many times a second -- which means
        **an exit must be counted once, not once per call**. The first version
        incremented the failure count on every poll while waiting out the
        backoff, so one killed worker looked like four crashes in four
        milliseconds and the subsystem gave up on a restart that had not been
        attempted yet. An exit is therefore recorded, the process handle
        dropped, and the backoff timed from the death rather than the birth.
        """
        if self._stopping:
            return

        process = self._process
        if process is not None:
            if process.poll() is None:
                return
            self._note_exit(process)
            return

        # No worker. Either we have given up, or we are waiting out a backoff.
        if self.failed or not self._dead_at:
            return
        delay = RESTART_DELAYS[min(self._attempt, len(RESTART_DELAYS) - 1)]
        if time.monotonic() - self._dead_at < delay:
            return
        self._attempt += 1
        log.warning("Restarting the player identification worker")
        self._spawn()

    def _note_exit(self, process) -> None:
        """Record one exit. Runs exactly once per worker."""
        ran_for = time.monotonic() - self._started_at
        code = process.returncode
        self.restarts += 1
        if ran_for >= HEALTHY_AFTER_S:
            # It was healthy; treat this as a first failure rather than
            # creeping up the ladder over a night of nightly crashes.
            self._attempt = 0
            self._immediate = 0
        elif ran_for < IMMEDIATE_S:
            self._immediate += 1

        self._process = None
        self._dead_at = time.monotonic()
        with self._lock:
            self._rows = []
            self._judgements = []

        if self._immediate >= MAX_IMMEDIATE_FAILURES:
            # Not a crash: something it cannot get past. Restarting will not
            # fix it, and saying so once beats a log line every few seconds.
            self.failed = (
                f"the worker exited immediately {self._immediate} times "
                f"(last code {code})"
            )
            log.error(
                "Player identification gave up: %s. Video, audio and "
                "controllers are unaffected.", self.failed,
            )
            return

        log.warning(
            "Player identification worker exited with code %s after %.1fs",
            code, ran_for,
        )

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for raw in process.stdout:
            try:
                message = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            kind = message.get("t")
            if kind == "rows":
                rows = _rows_from(message.get("r") or [])
                judgements = _judgements_from(message.get("j") or [])
                with self._lock:
                    self._rows = rows
                    self._judgements = judgements
                    self._child_snapshot = message.get("s") or {}
            elif kind == "state":
                with self._lock:
                    self._child_snapshot = message.get("s") or {}
            elif kind == "caps":
                self._caps = _caps_from(message.get("caps") or {})
                self._caps_seen.set()

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for raw in process.stderr:
            text = raw.decode("utf-8", "replace").rstrip()
            if text:
                # Re-logged, so a worker's complaint appears in the video
                # server's own log rather than in a pipe nobody reads.
                log.info("worker: %s", text)

    # -- introspection -----------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        process = self._process
        with self._lock:
            report = dict(self._child_snapshot)
        report.update({
            "runner": "process",
            "pid": process.pid if process is not None else None,
            "alive": bool(process is not None and process.poll() is None),
            "restarts": self.restarts,
            "failed": self.failed or report.get("failed", ""),
        })
        if self._slot is not None:
            # **Only the write-side counters.** This process writes the slot
            # and never reads it, so reporting `reads` from here would be a
            # number that is structurally always zero -- the same trap this
            # project recorded for `reports_sent`, where the counter that
            # looked healthy could not have answered the question being
            # asked. The child reports its own reads in `slot_reads`.
            stats = self._slot.snapshot()
            report["slot"] = {
                "writes": stats["writes"],
                "oversized": stats["oversized"],
            }
            if self.oversized_reason:
                report["oversized_reason"] = self.oversized_reason
        return report


def _rows_from(raw: list) -> list[TrackedPlayer]:
    rows: list[TrackedPlayer] = []
    for entry in raw:
        try:
            rows.append(
                TrackedPlayer(
                    track_id=int(entry[0]),
                    player_id=int(entry[1]),
                    region=str(entry[2]),
                    box=Rect(float(entry[3]), float(entry[4]),
                             float(entry[5]), float(entry[6])),
                    confidence=float(entry[7]),
                    source=str(entry[8]),
                )
            )
        except (TypeError, ValueError, IndexError):
            continue
    return rows


def _judgements_from(raw: list) -> list[Judgement]:
    """Rebuild the reasoning the child sent.

    Field by field and tolerant of a malformed entry, the same discipline
    `_rows_from` follows: a debug view that raised on one bad record would
    take out the display of every good one beside it.
    """
    out: list[Judgement] = []
    for entry in raw:
        try:
            scores = tuple(
                SignalScore(
                    signal=str(item[0]),
                    player_id=int(item[1]),
                    score=float(item[2]),
                    used=bool(item[3]),
                    note=str(item[4]),
                )
                for item in (entry[6] or [])
            )
            out.append(
                Judgement(
                    track_id=int(entry[0]),
                    player_id=int(entry[1]),
                    confidence=float(entry[2]),
                    source=str(entry[3]),
                    region=str(entry[4]),
                    note=str(entry[5]),
                    scores=scores,
                )
            )
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _caps_from(raw: dict) -> Capabilities:
    return Capabilities(
        backend=str(raw.get("backend", "none")),
        available=bool(raw.get("available")),
        reason=str(raw.get("reason", "")),
        device=str(raw.get("device", "")),
        embeddings=bool(raw.get("embeddings")),
        # Rebuilt field by field, so a field added to the dataclass and to
        # `as_dict` but forgotten here reads as its default in the parent --
        # zero, which is indistinguishable from "the model did not say" and
        # would silently keep the old sample size for ever.
        input_width=int(raw.get("input_width") or 0),
        input_height=int(raw.get("input_height") or 0),
    )


def make_runner(backend, worker, *, model_dir: str = "") -> Runner:
    """Inline, or a process, as the backend requires.

    The backend decides rather than a setting, so there is one answer and no
    way for a configuration and a capability to disagree about whether a model
    is loaded in this process.
    """
    if getattr(backend, "isolated", False):
        return ProcessRunner(model_dir=model_dir)
    return InlineRunner(backend, worker)

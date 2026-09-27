"""What the video server actually holds.

The seam between a PyAV frame and everything pure above it. It owns the
downscale, the backend lifecycle, and the answer to "is this even switched
on" -- and it is the only module here that knows PyAV exists.

OFF IS THE ORIGINAL PATH
-------------------------
``sample`` returns ``None`` on a plain boolean test before it touches a frame,
a lock, a reformatter or an import. No backend is constructed, no model is
loaded, no GPU is opened, and ``status()`` grows no key. That is the same
shape ``VideoServerApp.sample_layout`` uses for the split detector, and it is
the requirement this feature is judged on.

**Two switches and two questions.** ``player_id_enabled`` is the Bluetooth
server asking for labels; ``playervision_allowed`` is this machine consenting
to run a model. Both must be true, and they are deliberately different
questions -- in external mode the capture card is on somebody else's computer.

ITS OWN REFORMATTER, NEVER ``frame.reformat()``
------------------------------------------------
``frame.reformat()`` runs through a scaler cached **on the frame**, and one
``CapturedFrame`` is handed to the encoder and both previews at once. Two
threads inside that cached scaler wedge one of them permanently, with no
exception and nothing logged. This is the fourth consumer of ``capture.latest``
and it owns a private ``VideoReformatter`` like the other three.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from common.screen_regions import FULL

from .backends.base import Capabilities, SampleFrame, NullBackend, PlayerVisionBackend
from .shm import MAX_SAMPLE_WIDTH
from .types import Judgement, InputTrace, PlayerHint, TrackedPlayer
from .runner import Runner, make_runner
from .worker import VisionWorker

log = logging.getLogger(__name__)

__all__ = [
    "PlayerVisionService",
    "register_backend",
    "resolve_backend",
    "unregister_backend",
]

#: Width the frame is reduced to before detection when neither the model nor
#: the backend has said what it wants. Height follows the aspect. A detector
#: resizes its input to a fixed size as its first act, so feeding it 1080p only
#: pays to throw pixels away twice.
SAMPLE_WIDTH = 320

#: Backends added at runtime, by name, tried by ``auto`` before the model.
#:
#: **Empty in a running server.** Nothing in the product registers one, and
#: nothing arriving over the wire can name one: ``player_id_backend`` is
#: clamped to the known values before it gets here. It exists so the tests can
#: drive the whole chain with a stand-in detector -- there is no model-free
#: backend any more, and a model is an optional extra plus a download.
_REGISTERED: dict[str, type[PlayerVisionBackend]] = {}


def register_backend(backend_class: type[PlayerVisionBackend]) -> None:
    _REGISTERED[backend_class.name] = backend_class


def unregister_backend(name: str) -> None:
    _REGISTERED.pop(name, None)


def registered_module(backend: PlayerVisionBackend) -> str:
    """``module:Class`` for a registered backend, so a child can import it."""
    cls = type(backend)
    if _REGISTERED.get(cls.name) is not cls:
        return ""
    return f"{cls.__module__}:{cls.__qualname__}"


def resolve_backend(preference: str) -> tuple[PlayerVisionBackend, Capabilities]:
    """Pick a backend, and say what happened.

    ``auto`` walks the ladder best-first and takes the first that reports
    itself available. Anything else is a *request*: honoured when it can be,
    and reported with a reason when it cannot -- never silently downgraded,
    because a mode that quietly fell back is indistinguishable from one that
    is working, which is the failure this project keeps rediscovering.

    Imports are inside, so a machine with the feature off never loads a
    backend module, and a machine without the optional extra never sees an
    ImportError from merely having this file on disk.
    """
    ladder: list[str] = (
        [*_REGISTERED, "onnx"] if preference == "auto" else [preference]
    )

    reasons: list[str] = []
    for name in ladder:
        backend_class = _backend_class(name)
        if backend_class is None:
            reasons.append(f"{name}: not a backend this build knows about")
            continue
        try:
            caps = backend_class.probe()
        except Exception as exc:  # noqa: BLE001 -- a probe may not take the server down
            reasons.append(f"{name}: probe failed ({exc})")
            log.debug("Backend probe failed for %s", name, exc_info=True)
            continue
        if caps.available:
            return backend_class(), caps
        reasons.append(f"{name}: {caps.reason}")

    return NullBackend(), Capabilities(
        backend="none",
        available=False,
        reason="; ".join(reasons) or "no backend available",
    )


def _backend_class(name: str) -> type[PlayerVisionBackend] | None:
    if name in _REGISTERED:
        return _REGISTERED[name]
    if name == "onnx":
        try:
            from .backends.onnx import OnnxBackend
        except ImportError:
            # The optional extra is not installed. An ordinary answer on a
            # machine that never asked for it, not a fault.
            return None
        return OnnxBackend
    if name == "none":
        return NullBackend
    return None


class PlayerVisionService:
    """Samples frames and produces published rows. One caller, one thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: What drives the worker: on this thread, or in its own process.
        #: The **backend** decides, through `isolated` -- see `runner.py`.
        #: Held rather than a `VisionWorker` directly, because for a model
        #: backend there is no worker in this process to hold.
        self._runner: Runner | None = None
        self._backend: PlayerVisionBackend | None = None
        self._caps = Capabilities()
        self._reformatter: Any = None
        self._wanted: tuple[str, float] = ("", 0.0)

        #: What we have been told, held whether or not a worker exists yet.
        #:
        #: The Bluetooth server pushes the player map on its own periodic
        #: message, which routinely lands *before* the first frame -- so a
        #: service that only forwarded configuration to a live worker would
        #: drop the roster and then identify nobody, with every counter
        #: healthy. Held here and applied when the worker is built.
        self._layout = FULL
        self._hints: tuple[PlayerHint, ...] = ()
        self._traces: tuple[InputTrace, ...] = ()
        self._confidence = 0.6
        self._active: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
        self._tuning: dict = {}
        #: Passed to an isolated worker, which is a different process and so
        #: does not inherit a directory chosen at runtime. Empty means the
        #: child works it out for itself, which is the ordinary case.
        self._model_dir = ""

        self._rows: list[TrackedPlayer] = []
        self._rows_ns = 0
        #: The last refusal we said out loud, so an unavailable backend is
        #: reported when it changes rather than on every sample.
        self._last_refusal = ""
        self._last_sample_ns = 0

        self.samples = 0
        self.skipped = 0

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._runner is not None

    @property
    def capabilities(self) -> Capabilities:
        return self._caps

    def stop(self) -> None:
        """Release everything. Safe to call twice, and after a failure.

        Called when the operator switches the feature off, when the capture
        machine withdraws consent, and on shutdown. Dropping the worker drops
        the tracker and every gallery with it, which is the point: identity is
        a property of a session, and resuming with a stale one would carry a
        claim about a game that ended.
        """
        with self._lock:
            backend, self._backend = self._backend, None
            runner, self._runner = self._runner, None
            self._rows = []
            self._rows_ns = 0
            self._reformatter = None
            self._wanted = ("", 0.0)
            # `_layout`, `_hints` and `_traces` deliberately survive: they are
            # what we were *told*, not what we worked out, and the Bluetooth
            # server re-pushes them on its own slow cadence. Dropping them
            # would leave a restarted worker blind until the next push.
        # The runner owns the backend's lifetime -- inline it calls `stop`,
        # and across a process it closes the child, which is what actually
        # releases device memory. Calling `backend.stop()` here as well would
        # be stopping a backend this process never started.
        if runner is not None:
            try:
                runner.stop()
            except Exception:  # noqa: BLE001
                log.debug("Runner stop failed", exc_info=True)
        elif backend is not None:
            try:
                backend.stop()
            except Exception:  # noqa: BLE001
                log.debug("Backend stop failed", exc_info=True)

    # -- configuration -----------------------------------------------------

    def configure(
        self,
        *,
        layout: str | None = None,
        hints: tuple[PlayerHint, ...] | None = None,
        traces: tuple[InputTrace, ...] | None = None,
        confidence: float | None = None,
        active: tuple[float, float, float, float] | None = None,
        tuning: dict | None = None,
        reset_learning: bool = False,
    ) -> None:
        # Recorded first and unconditionally, so it survives a worker that
        # does not exist yet, one that is rebuilt when the backend changes,
        # and one that has not started because the feature was off.
        if layout is not None:
            self._layout = layout
        if hints is not None:
            self._hints = tuple(hints)
        if traces is not None:
            self._traces = tuple(traces)
        if confidence is not None:
            self._confidence = float(confidence)
        if active is not None:
            self._active = tuple(active)  # type: ignore[assignment]
        if tuning is not None:
            self._tuning = dict(tuning)

        runner = self._runner
        if runner is None:
            return
        runner.configure(
            layout=layout, hints=hints, traces=traces, confidence=confidence,
            active=active, tuning=tuning, reset_learning=reset_learning,
        )

    # -- the work ----------------------------------------------------------

    def due(self, settings: Any, allowed: bool, now_ns: int) -> bool:
        """Is a sample wanted right now? The cheapest possible question.

        Asked before the frame lock is taken, so a tick that is not due costs
        nothing at all -- and with the feature off it is one attribute read
        and a boolean test.
        """
        if not allowed or not getattr(settings, "player_id_enabled", False):
            return False
        hz = max(0.5, float(getattr(settings, "player_id_hz", 6.0) or 6.0))
        interval = int(1_000_000_000 / hz)
        return not self._last_sample_ns or now_ns - self._last_sample_ns >= interval

    def sample(
        self, frame: Any, settings: Any, allowed: bool, now_ns: int
    ) -> list[TrackedPlayer] | None:
        """Analyse one PyAV frame. ``None`` when nothing was done.

        ``None`` rather than an empty list, deliberately: "we did not look" and
        "we looked and there is nobody" want different answers upstream. The
        first should leave the last published rows alone; the second should
        replace them, which is what takes a departed player's label away.
        """
        if not self.due(settings, allowed, now_ns):
            return None
        self._last_sample_ns = now_ns

        runner = self._ensure_worker(settings)
        if runner is None or frame is None:
            return None

        sample = self._to_sample(frame, now_ns)
        if sample is None:
            self.skipped += 1
            return None

        # Never waits for a result. Inline that is the rows for this frame;
        # across a process it is the newest the worker has produced, which is
        # correct rather than a compromise -- these are identities, already
        # several frames old by the time a client draws them, and waiting
        # would put a model's scheduling on the thread that sends the status.
        rows = runner.submit(sample, now_ns)
        self.samples += 1
        with self._lock:
            self._rows = rows
            self._rows_ns = now_ns
        return rows

    def rows(self) -> list[TrackedPlayer]:
        """The newest rows, from whichever side produced them.

        Asked of the runner rather than the last `sample` return, because an
        isolated worker produces results between samples: its answer for the
        frame handed over two ticks ago arrives whenever it arrives, and a
        cached copy would hold labels a tick or two staler than necessary.
        """
        runner = self._runner
        if runner is not None:
            try:
                return runner.latest()
            except Exception:  # noqa: BLE001
                log.debug("Could not read the worker's rows", exc_info=True)
        with self._lock:
            return list(self._rows)

    def judgements(self) -> list[Judgement]:
        """Why the newest rows came out as they did. Empty when idle.

        Asked of the runner for the same reason `rows` is, and it has to be
        the *same* runner in the same call order -- a breakdown read from one
        round and rows from the next would put a name in the table beside the
        reasoning that refused it.
        """
        runner = self._runner
        if runner is None:
            return []
        try:
            return runner.judgements()
        except Exception:  # noqa: BLE001
            log.debug("Could not read the worker's reasoning", exc_info=True)
            return []

    # -- internals ---------------------------------------------------------

    def _ensure_worker(self, settings: Any) -> Runner | None:
        """Build or rebuild the runner when what was asked for has changed."""
        preference = str(getattr(settings, "player_id_backend", "auto") or "auto")
        confidence = float(getattr(settings, "player_id_confidence", 0.6) or 0.6)
        wanted = (preference, confidence)

        runner = self._runner
        if runner is not None and self._wanted == wanted:
            return runner
        if runner is not None and self._wanted[0] == preference:
            # Only the threshold moved. Rebuilding would throw away every
            # gallery -- and, for a model backend, reload the model -- for a
            # number the identity manager can simply be told.
            runner.configure(confidence=confidence)
            self._wanted = wanted
            return runner

        self.stop()
        backend, caps = resolve_backend(preference)
        self._caps = caps
        if not caps.available:
            # **Said once, not once per sample.** `stop()` clears `_wanted`,
            # so an unavailable backend falls through this branch on every
            # tick -- measured at ~5 warnings a second, 213 of them in 43
            # seconds, which on the reference Pi is a journal nobody can read
            # and the real messages buried in it.
            #
            # The *probe* still runs each time, deliberately: it is a file
            # stat, and an operator who drops a model in while the stream is
            # up should not have to toggle anything to be noticed. Only the
            # saying is suppressed, and only while the answer is identical.
            refusal = "; ".join(caps.describe())
            if refusal != self._last_refusal:
                self._last_refusal = refusal
                for line in caps.describe():
                    log.warning("%s", line)
            return None
        self._last_refusal = ""

        # A model backend gets its own process; a cheap one does not. The
        # backend decides, so a configuration and a capability cannot
        # disagree about whether a model is loaded in *this* process.
        worker = VisionWorker(backend, confidence=confidence)
        runner = make_runner(
            backend, worker, model_dir=self._model_dir,
            backend_module=registered_module(backend),
        )

        try:
            caps = runner.start(preference, confidence) or caps
        except Exception as exc:  # noqa: BLE001
            log.error("Player identification backend %s would not start: %s",
                      backend.name, exc)
            log.debug("Backend start failed", exc_info=True)
            self._caps = Capabilities(
                backend=backend.name, available=False, reason=str(exc)
            )
            try:
                runner.stop()
            except Exception:  # noqa: BLE001
                pass
            return None

        if not caps.available:
            # The child reported *why*, which is the useful half -- no models
            # in the directory, no provider, a session that would not build.
            for line in caps.describe():
                log.warning("%s", line)
            self._caps = caps
            try:
                runner.stop()
            except Exception:  # noqa: BLE001
                pass
            return None

        self._caps = caps
        self._backend = backend
        self._runner = runner
        # Everything we were told before this existed. Layout *and* roster:
        # a worker that started with the layout but no players would find
        # entities and attribute none of them, which reads as detection being
        # broken rather than as the roster never having arrived.
        runner.configure(
            layout=self._layout, hints=self._hints,
            traces=self._traces, confidence=confidence,
            active=self._active, tuning=self._tuning,
        )
        self._wanted = wanted
        for line in caps.describe():
            log.info("%s", line)
        return runner

    def _to_sample(self, frame: Any, now_ns: int) -> SampleFrame | None:
        """Downscale to what the backend asked for.

        Luma by default; ``rgb24`` for a backend that sets ``wants_colour``.
        Colour is three times the bytes to scale and to copy, so it is the
        backend's decision rather than the default -- but appearance matching
        without it throws away the single most useful thing for telling two
        players apart, which is that one of them is the red one.

        Own reformatter; never ``frame.reformat()``.
        """
        try:
            width = int(getattr(frame, "width", 0) or 0)
            height = int(getattr(frame, "height", 0) or 0)
            if width <= 0 or height <= 0:
                return None

            target_w = min(self.sample_width(), width)
            # Even dimensions: a 4:2:0 source cannot be scaled to an odd one,
            # and the failure is an exception from deep inside swscale.
            target_w -= target_w % 2
            target_h = max(2, int(height * target_w / max(1, width)))
            target_h -= target_h % 2

            scaler = self._scaler()
            if scaler is None:
                return None
            backend = self._backend
            pixel_format = (
                "rgb24"
                if backend is not None and getattr(backend, "wants_colour", False)
                else "gray"
            )
            reduced = scaler.reformat(
                frame, width=target_w, height=target_h, format=pixel_format
            )
            plane = reduced.planes[0]
            return SampleFrame(
                data=memoryview(plane),
                width=target_w,
                height=target_h,
                stride=plane.line_size,
                capture_ts=now_ns,
                pixel_format=pixel_format,
            )
        except Exception:  # noqa: BLE001 -- a bad frame is not a fault
            log.debug("Could not reduce a frame for player vision", exc_info=True)
            return None

    def sample_width(self) -> int:
        """How wide to reduce a frame to, for the backend we have.

        Three answers, "declared always wins", the order `resolve_layout`
        already uses:

        1. what the **loaded model** said, which arrived on `Capabilities`
           from the child. Best, because it comes from the file the operator
           actually supplied.
        2. the backend's **class** attribute. Forced to be class-level: for
           an isolated backend the instance held here is never started, so
           there is nothing else to read.
        3. `SAMPLE_WIDTH`, which is what the no-model backend wants and what
           this was before any of it.

        Clamped against the *same* constant the shared-memory slot is sized
        from, so an oversized frame cannot happen by construction rather than
        being caught afterwards -- a refused write moves only `oversized`
        while every other counter reads healthy.
        """
        wanted = int(self._caps.input_width or 0)
        if wanted <= 0:
            backend = self._backend
            wanted = int(getattr(backend, "wants_width", 0) or 0)
        if wanted <= 0:
            wanted = SAMPLE_WIDTH
        return max(2, min(wanted, MAX_SAMPLE_WIDTH))

    def _scaler(self) -> Any:
        if self._reformatter is None:
            try:
                from av.video.reformatter import VideoReformatter
            except ImportError:
                log.debug("PyAV is not available; player vision cannot sample")
                return None
            self._reformatter = VideoReformatter()
        return self._reformatter

    # -- introspection -----------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """What an operator needs, small enough to cross the wire.

        Observed, never configured. It travels beside the settings rather than
        inside them, for the reason the preview-demand post-mortem records: a
        source adopts whatever is pushed at it, so a detected value living in
        the settings would be adopted back as the operator's own choice and
        could never be undone.

        **Deliberately slim, and that is a bug fix rather than tidiness.**
        This block rides ``VIDEO_STATUS``, which has a hard 1200-byte ceiling
        that ``encode_control`` enforces by refusing the **whole message** --
        so a source that grows one field too many stops reporting entirely
        while streaming perfectly. The full report reached 1187 of 1195 usable
        bytes with an isolated backend running, and three ordinary states went
        over: an hour of counters, a backend that had given up, and a provider
        that was unavailable with its reason. The last two are exactly when a
        status is worth having.

        Everything bulky is in :meth:`debug_snapshot`, which rides the slow
        message and only when the operator asks for it.
        """
        runner = self._runner
        caps = self._caps.as_dict()
        # **Named, not splatted.** `as_dict` also feeds the child-to-parent
        # capability channel, which carries things this message has no room
        # for -- the model's input size went on it and silently cost 33 bytes
        # of headroom here the moment it was added. Naming the fields is what
        # stops the next one doing the same.
        report: dict[str, object] = {
            "running": runner is not None,
            "samples": self.samples,
            "backend": caps["backend"],
            "available": caps["available"],
            "reason": caps["reason"],
            "embeddings": caps["embeddings"],
        }
        # Only when there is one. An unavailable backend has no device, and
        # `"device":""` spent eleven bytes saying so on the one message that
        # refuses whole -- in exactly the state a model-less machine is now in
        # by default. Every reader already treats absence as "none".
        if caps["device"]:
            report["device"] = caps["device"]
        if runner is not None:
            detail = runner.snapshot()
            # Hand-picked rather than filtered, so a counter added to a runner
            # cannot silently re-enter the message and eat the headroom back.
            for key in ("runner", "alive"):
                if key in detail:
                    report[key] = detail[key]

            # **Only when they have something to say.** `"restarts":0` and
            # `"failed":""` cost 25 bytes on every message to report that
            # nothing is wrong, and this message refuses whole when it runs
            # out of room. Absence reads as the healthy value at every
            # consumer, which is what makes that safe rather than merely
            # smaller.
            for key in ("restarts", "failed"):
                if detail.get(key):
                    report[key] = detail[key]
            slot = detail.get("slot")
            if isinstance(slot, dict) and slot.get("oversized"):
                # Same rule: frames too large for the worker's buffer is a
                # fault worth a field, and a zero is not.
                report["oversized"] = slot["oversized"]
        return report

    def learned(self) -> dict[str, object]:
        """What identification has learned this session, for the readouts.

        Read out of the worker's snapshot, which both runners put at the top
        level -- so it works the same whether the worker is on this thread or
        in its own process. Empty while nothing runs.
        """
        runner = self._runner
        if runner is None:
            return {}
        detail = runner.snapshot()
        identity = detail.get("identity")
        learned = dict((identity or {}).get("learned") or {}) if isinstance(identity, dict) else {}
        if "score_floor" in detail:
            learned["score_floor_in_force"] = detail["score_floor"]
        return learned

    def debug_snapshot(self) -> dict[str, object]:
        """Everything, for the developer view and the local GUI.

        Not on the status message: the tracker, identity and slot counters are
        a few hundred bytes that only somebody working on this reads, and the
        status has no room for them. They ride ``player_id_stats`` on the 5 s
        slow-state message, and only while ``player_id_debug`` is on.
        """
        runner = self._runner
        report: dict[str, object] = {
            "running": runner is not None,
            "samples": self.samples,
            "skipped": self.skipped,
            **self._caps.as_dict(),
        }
        if runner is not None:
            report.update(runner.snapshot())
        return report

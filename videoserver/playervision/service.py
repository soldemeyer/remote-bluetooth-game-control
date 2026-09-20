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

from .backends.base import Capabilities, GrayFrame, NullBackend, PlayerVisionBackend
from .types import InputTrace, PlayerHint, TrackedPlayer
from .worker import VisionWorker

log = logging.getLogger(__name__)

__all__ = ["PlayerVisionService", "resolve_backend"]

#: Width the frame is reduced to before detection. Height follows the aspect.
#:
#: Small on purpose, and for two reasons rather than one: it is what makes the
#: no-model backend affordable in pure Python, and it is what a model backend
#: will want anyway -- a detector resizes its input to a fixed size as its
#: first act, so feeding it 1080p only pays to throw pixels away twice.
SAMPLE_WIDTH = 320


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
    ladder: list[str] = ["onnx", "heuristic"] if preference == "auto" else [preference]

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
    if name == "heuristic":
        from .backends.heuristic import HeuristicBackend

        return HeuristicBackend
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
        self._worker: VisionWorker | None = None
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

        self._rows: list[TrackedPlayer] = []
        self._rows_ns = 0
        self._last_sample_ns = 0

        self.samples = 0
        self.skipped = 0

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._worker is not None

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
            self._worker = None
            self._rows = []
            self._rows_ns = 0
            self._reformatter = None
            self._wanted = ("", 0.0)
            # `_layout`, `_hints` and `_traces` deliberately survive: they are
            # what we were *told*, not what we worked out, and the Bluetooth
            # server re-pushes them on its own slow cadence. Dropping them
            # would leave a restarted worker blind until the next push.
        if backend is not None:
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

        worker = self._worker
        if worker is None:
            return
        worker.configure(
            layout=layout, hints=hints, traces=traces, confidence=confidence
        )
        if hints is not None:
            worker.forget_absent_players()

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

        worker = self._ensure_worker(settings)
        if worker is None or frame is None:
            return None

        gray = self._to_gray(frame, now_ns)
        if gray is None:
            self.skipped += 1
            return None

        rows = worker.process(gray, now_ns)
        self.samples += 1
        with self._lock:
            self._rows = rows
            self._rows_ns = now_ns
        return rows

    def rows(self) -> list[TrackedPlayer]:
        with self._lock:
            return list(self._rows)

    # -- internals ---------------------------------------------------------

    def _ensure_worker(self, settings: Any) -> VisionWorker | None:
        """Build or rebuild the worker when what was asked for has changed."""
        preference = str(getattr(settings, "player_id_backend", "auto") or "auto")
        confidence = float(getattr(settings, "player_id_confidence", 0.6) or 0.6)
        wanted = (preference, confidence)

        worker = self._worker
        if worker is not None and self._wanted == wanted:
            return worker
        if worker is not None and self._wanted[0] == preference:
            # Only the threshold moved. Rebuilding would throw away every
            # gallery for a number the identity manager can simply be told.
            worker.configure(confidence=confidence)
            self._wanted = wanted
            return worker

        self.stop()
        backend, caps = resolve_backend(preference)
        self._caps = caps
        if not caps.available:
            for line in caps.describe():
                log.warning("%s", line)
            return None

        try:
            caps = backend.start() or caps
        except Exception as exc:  # noqa: BLE001
            log.error("Player identification backend %s would not start: %s",
                      backend.name, exc)
            log.debug("Backend start failed", exc_info=True)
            self._caps = Capabilities(
                backend=backend.name, available=False, reason=str(exc)
            )
            return None

        self._caps = caps
        self._backend = backend
        worker = VisionWorker(backend, confidence=confidence)
        # Everything we were told before this existed. Layout *and* roster:
        # a worker that started with the layout but no players would find
        # entities and attribute none of them, which reads as detection
        # being broken rather than as the roster never having arrived.
        worker.configure(
            layout=self._layout, hints=self._hints, traces=self._traces
        )
        self._worker = worker
        self._wanted = wanted
        for line in caps.describe():
            log.info("%s", line)
        return worker

    def _to_gray(self, frame: Any, now_ns: int) -> GrayFrame | None:
        """Downscale to luma. Own reformatter; never ``frame.reformat()``."""
        try:
            width = int(getattr(frame, "width", 0) or 0)
            height = int(getattr(frame, "height", 0) or 0)
            if width <= 0 or height <= 0:
                return None

            target_w = min(SAMPLE_WIDTH, width)
            # Even dimensions: a 4:2:0 source cannot be scaled to an odd one,
            # and the failure is an exception from deep inside swscale.
            target_w -= target_w % 2
            target_h = max(2, int(height * target_w / max(1, width)))
            target_h -= target_h % 2

            scaler = self._scaler()
            if scaler is None:
                return None
            reduced = scaler.reformat(
                frame, width=target_w, height=target_h, format="gray"
            )
            plane = reduced.planes[0]
            return GrayFrame(
                data=memoryview(plane),
                width=target_w,
                height=target_h,
                stride=plane.line_size,
                capture_ts=now_ns,
            )
        except Exception:  # noqa: BLE001 -- a bad frame is not a fault
            log.debug("Could not reduce a frame for player vision", exc_info=True)
            return None

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
        """What this is doing. Observed, never configured.

        It travels beside the settings rather than inside them, for the reason
        the preview-demand post-mortem records: a source adopts whatever is
        pushed at it, so a detected value living in the settings would be
        adopted back as the operator's own choice and could never be undone.
        """
        worker = self._worker
        report: dict[str, object] = {
            "running": worker is not None,
            "samples": self.samples,
            "skipped": self.skipped,
            **self._caps.as_dict(),
        }
        if worker is not None:
            report.update(worker.snapshot())
        return report

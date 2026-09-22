"""The driver: a frame and some evidence in, published rows out.

Pure in the sense that matters -- it holds a backend, a tracker and an identity
manager, and touches nothing else. No sockets, no PyAV, no shared memory, no
threads. That is what lets the same object be driven from a worker thread
today and from a subprocess later without any of the logic moving.

**It never raises.** A backend having a bad frame, a malformed hint, an
arithmetic surprise in a scoring function -- none of them may reach the caller,
because the caller is the video server and the whole promise of this feature is
that it cannot disturb the stream. Failures are counted and reported; the
stream carries on with no labels, which is what it did before this existed.
"""

from __future__ import annotations

import logging

from common.screen_regions import FULL, normalise_layout

from .backends.base import SampleFrame, PlayerVisionBackend
from .identity import PlayerIdentityManager
from .tracking import EntityTracker
from .types import Evidence, InputTrace, Judgement, PlayerHint, TrackedPlayer

log = logging.getLogger(__name__)

__all__ = ["VisionWorker"]

#: Consecutive backend failures before the worker gives up on it.
#:
#: A backend that has failed this many times in a row is not having a bad
#: frame, it is broken -- and retrying it every sample would spend the
#: machine's time producing nothing, forever, while reporting healthy counters.
MAX_CONSECUTIVE_FAILURES = 10


class VisionWorker:
    """One backend, one tracker, one identity manager. Not thread-safe."""

    def __init__(
        self, backend: PlayerVisionBackend, *, confidence: float = 0.6
    ) -> None:
        self._backend = backend
        self._tracker = EntityTracker()
        self._identity = PlayerIdentityManager(confidence=confidence)

        self._layout = FULL
        self._hints: tuple[PlayerHint, ...] = ()
        self._traces: tuple[InputTrace, ...] = ()

        self.frames = 0
        self.failures = 0
        self.consecutive_failures = 0
        self.failed = ""

    # -- configuration -----------------------------------------------------

    def configure(
        self,
        *,
        layout: str | None = None,
        hints: tuple[PlayerHint, ...] | None = None,
        traces: tuple[InputTrace, ...] | None = None,
        confidence: float | None = None,
    ) -> None:
        """Absorb what the Bluetooth server has told us.

        Each field is optional and only what is given is changed: the layout
        arrives from our own detector at its rate, the hints on their own
        periodic message, and the input traces on theirs. A call that names
        one must not silently reset the others.

        **A layout change deliberately does not reset the tracker.** A player
        is the same player whether the picture is split two ways or four, and
        every track's region is recomputed from its box on each update anyway.
        Dropping continuity there would cost everybody their label for a
        second at exactly the moment the game changed mode.
        """
        if layout is not None:
            self._layout = normalise_layout(layout)
        if hints is not None:
            self._hints = tuple(hints)
        if traces is not None:
            self._traces = tuple(traces)
        if confidence is not None:
            self._identity.confidence = max(0.05, min(0.99, float(confidence)))

    def forget_absent_players(self) -> None:
        """Drop galleries for players no longer in the roster.

        The same leak ``_forget_rumble_state`` and ``SyncGovernor.forget``
        exist to fix. Without it a player who left an hour ago goes on
        competing for every track, and the first thing that happens when
        somebody new takes their adapter is that they are mistaken for them.
        """
        present = {hint.player_id for hint in self._hints}
        for player_id in list(self._identity.snapshot()["exemplars"]):
            if int(player_id) not in present:
                self._identity.forget(int(player_id))

    def reset(self) -> None:
        """Forget everything. A stream restart, or a new game."""
        self._tracker.reset()
        self._identity.reset()

    # -- the work ----------------------------------------------------------

    def process(self, frame: SampleFrame, now_ns: int) -> list[TrackedPlayer]:
        """One frame. Returns what to publish, and never raises."""
        if self.failed:
            return []

        try:
            detections = self._backend.detect(frame)
        except Exception as exc:  # noqa: BLE001 -- the whole point
            return self._note_failure(exc)

        try:
            tracks = self._tracker.update(detections, self._layout, now_ns)
            evidence = Evidence(
                layout=self._layout, hints=self._hints, traces=self._traces
            )
            rows = self._identity.assign(tracks, evidence, now_ns)
        except Exception as exc:  # noqa: BLE001
            return self._note_failure(exc)

        self.frames += 1
        self.consecutive_failures = 0
        return rows

    def _note_failure(self, exc: Exception) -> list[TrackedPlayer]:
        self.failures += 1
        self.consecutive_failures += 1
        log.debug("Player vision sample failed", exc_info=True)
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            # Said once, at a level the operator will see: a subsystem that
            # has switched itself off must say so, or "no labels" is
            # indistinguishable from "nobody has been identified yet".
            self.failed = f"{type(exc).__name__}: {exc}"
            log.error(
                "Player identification stopped after %d consecutive failures: %s. "
                "Video, audio and controllers are unaffected.",
                self.consecutive_failures,
                self.failed,
            )
        return []

    def judgements(self) -> list[Judgement]:
        """Why the last round came out as it did. For the operator, locally.

        Read straight from the identity manager rather than cached here: it
        rebuilds them every round, and a copy kept alongside would be one more
        thing to reset on a restart and to forget to.
        """
        return self._identity.judgements()

    # -- introspection -----------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        return {
            "frames": self.frames,
            "failures": self.failures,
            "failed": self.failed,
            "layout": self._layout,
            "players": len(self._hints),
            "tracks": self._tracker.snapshot(),
            "identity": self._identity.snapshot(),
            "backend": self._backend.snapshot(),
        }

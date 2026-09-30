"""What each player's thumb has been doing lately.

The one identity signal that survives a shared screen. With no split there is
no viewport to attribute a character to, and appearance cannot separate two
players who picked the same one -- but their sticks are not doing the same
thing, and what moves on screen when a stick moves is the strongest evidence
available that the two belong together.

Stdlib only, no sockets, no image processing: this records a shape and hands
it over. The correlation itself happens on the machine that has the frames.

WHY IT IS SAMPLED RATHER THAN STREAMED
---------------------------------------
The datapath already mirrors live stick values onto ``ControllerSlot`` for the
web GUI, before the approval gate, on every packet. Reading those at a fixed
low rate costs a few integer loads on the asyncio thread; forwarding the real
input -- up to 500 packets a second per player -- would mean putting the
gameplay stream onto the control channel and through a second machine, which
is the bandwidth this whole architecture exists to avoid.

A correlation over a second does not need the packets. It needs the *shape*,
and sixteen samples of it describe a shape perfectly well.

WHAT IT IS NOT
---------------
It is evidence, weighted, and never decisive on its own. A camera-relative
control scheme rotates the mapping between stick and screen; a fixed camera
breaks it entirely; in menus and many minigames the stick does not move the
avatar at all. The identity manager treats a correlation as one signal among
several and refuses a match that does not clear its runner-up, which is what
stops this being confidently wrong in exactly the games it cannot read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

__all__ = ["MotionRecorder", "SAMPLE_HZ", "WINDOW_SAMPLES"]

#: How often a stick position is recorded.
#:
#: Matched to the asyncio status tick rather than given a timer of its own:
#: this reads a handful of integers that another thread has already mirrored,
#: and a second timer to keep in step with is a cost with no benefit.
SAMPLE_HZ = 10

#: How many samples one trace carries. Sixteen at 10 Hz is 1.6 seconds --
#: long enough to contain a turn, short enough that a player who changed
#: direction is not still being correlated against what they did before.
WINDOW_SAMPLES = 16

#: Full-scale stick deflection, from the wire format.
_AXIS_MAX = 32767.0

#: Below this, a stick is at rest and the sample is recorded as zero.
#:
#: A resting stick does not sit exactly at centre, and a trace full of small
#: nonsense correlates weakly with everything -- which is worse than
#: correlating with nothing, because it can still win a comparison against
#: another player who is genuinely still.
_DEADBAND = 0.08


@dataclass(slots=True)
class _Trace:
    player_id: int
    samples: list[tuple[float, float]] = field(default_factory=list)

    def add(self, dx: float, dy: float) -> None:
        self.samples.append((dx, dy))
        if len(self.samples) > WINDOW_SAMPLES:
            del self.samples[: len(self.samples) - WINDOW_SAMPLES]

    @property
    def moving(self) -> bool:
        """Has this player done anything worth correlating against?

        A trace of nothing but zeros is not evidence that somebody is standing
        still -- it is evidence of nothing, and sending it invites a match
        against whatever else on screen happens to be motionless.
        """
        return any(dx or dy for dx, dy in self.samples)


class MotionRecorder:
    """Per-player stick history. One caller: the asyncio thread."""

    def __init__(self) -> None:
        self._traces: dict[int, _Trace] = {}

    def sample(self, router, sessions) -> None:
        """Record where every assigned player's left stick is right now.

        Reads the slot state the datapath already mirrors. A player whose
        session or slot has gone is dropped, which is the same leak
        ``_forget_rumble_state`` and ``SyncGovernor.forget`` exist to close --
        without it, a trace for somebody who left an hour ago goes on offering
        itself as a match for every character on screen.
        """
        by_client = {
            session.client_id: session for session in sessions.all_sessions()
        }

        present: set[int] = set()
        for channel in router.channels():
            number = getattr(channel, "number", 0)
            if not channel.is_assigned or not number:
                continue
            session = by_client.get(channel.assigned_client)
            if session is None:
                continue
            slot = session.slots.get(channel.assigned_slot)
            if slot is None:
                continue

            present.add(number)
            trace = self._traces.get(number)
            if trace is None:
                trace = _Trace(player_id=number)
                self._traces[number] = trace
            trace.add(*_stick(slot))

        for player_id in list(self._traces):
            if player_id not in present:
                del self._traces[player_id]

    def traces(self) -> list[_Trace]:
        """Traces worth sending: full windows, from players who moved.

        A partial window is not withheld out of tidiness -- correlating a
        four-sample window against a sixteen-sample one compares a fragment of
        a gesture against a whole one, and the score means nothing.
        """
        return [
            trace
            for trace in self._traces.values()
            if len(trace.samples) >= WINDOW_SAMPLES and trace.moving
        ]

    def forget(self, player_id: int) -> None:
        self._traces.pop(player_id, None)

    def clear(self) -> None:
        self._traces.clear()

    def __len__(self) -> int:
        return len(self._traces)


def _stick(slot) -> tuple[float, float]:
    """One slot's left stick as ``(dx, dy)`` in -1..1, screen convention.

    ``dy`` is **not** flipped. The stick's own Y is already down-positive
    (``client/input/mapping.py`` binds W to -1 and S to +1), so it agrees with
    normalised frame coordinates -- and a flip added "for safety" would invert
    every correlation, turning the one signal that separates two identical
    characters into the thing that swaps them.
    """
    try:
        dx = float(getattr(slot, "left_x", 0) or 0) / _AXIS_MAX
        dy = float(getattr(slot, "left_y", 0) or 0) / _AXIS_MAX
    except (TypeError, ValueError):
        return 0.0, 0.0

    if (dx * dx + dy * dy) ** 0.5 < _DEADBAND:
        return 0.0, 0.0
    return max(-1.0, min(1.0, dx)), max(-1.0, min(1.0, dy))

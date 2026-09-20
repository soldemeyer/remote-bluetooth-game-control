"""Levelling the playing field: how much delay each client gets.

A player on the LAN and a player across the internet are not playing the same
game. Sync latency holds the quick ones back so everybody feels the slowest
connection -- an operator switch, off by default, because it is a deliberate
trade of *everyone's* latency for fairness and only the operator knows whether
that is what the group wants.

This module is the arithmetic and nothing else: no sockets, no sinks, no
sessions. It takes what each client's round trip measures and answers how long
each one's input should be held. That keeps the part most likely to be subtly
wrong testable with tuples, and keeps the datapath's copy down to gathering the
inputs and applying the answers.

**Half the difference, not all of it.** What a player feels is one way -- their
press reaching the console -- and a round trip measures two, so levelling a 40 ms
RTT gap means adding 20 ms. Same convention the client's combined latency figure
already uses.

**What this cannot do anything about.** It levels the *input* path. Video
latency differs between players too and is not equalised, so the claim is about
the controller and the GUI copy says so.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: Default ceiling on the delay added to any one client, in milliseconds.
#:
#: Levels against a peer at roughly twice this in round trip -- 120 ms RTT, which
#: covers the WAN band this project measures (10-60 ms each way). About 3.6
#: frames at 60 Hz.
#:
#: There has to be a ceiling. Matching a player on a 300 ms link makes the game
#: unplayable for everybody, and at that point the honest answer is not "level
#: it" but "that connection is too bad to play against" -- so the cap binds, the
#: GUI says it is binding, and the operator gets to make that call rather than
#: having it made silently. It also bounds the delay line's ring, and bounds what
#: a client reporting nonsense can do to everyone else.
DEFAULT_CAP_MS = 60.0

#: The operator may raise the ceiling this far. Past here it is not a game.
MAX_CAP_MS = 300.0

#: Round trips a client must have contributed before it may *define* the
#: slowest connection. 2 s at the probe rate.
#:
#: Without it a client three noisy samples old sets the target for everybody,
#: and the first of those samples is taken while its handshake and its first
#: control messages are still in flight.
MIN_SAMPLES = 20

#: Change below this is not applied.
#:
#: Two reasons, and the second is the real one. A delay that twitches is worse
#: than a delay that is slightly wrong -- the whole point is a steady feel. And
#: the measurement itself has a floor of a few milliseconds: the client answers
#: our probe from its input loop, so a measured round trip carries up to one poll
#: period of bias (2 ms at 500 Hz, 8 ms at 125 Hz) and *that bias differs between
#: clients*, which is exactly the comparison made here. Chasing precision below
#: the noise would only be chasing the noise.
DEADBAND_MS = 2.0


@dataclass(frozen=True, slots=True)
class Participant:
    """One client in the running, as the governor needs to see it."""

    client_id: str
    rtt_p50_ms: float
    samples: int


@dataclass(frozen=True, slots=True)
class Verdict:
    """How long to hold this client's input, and why."""

    delay_ns: int
    #: ``off`` nothing to do, ``measuring`` not enough samples yet,
    #: ``levelled`` matched to the slowest, ``capped`` matched as far as the
    #: ceiling allows.
    state: str

    @property
    def delay_ms(self) -> float:
        return self.delay_ns / 1e6


class SyncGovernor:
    """Decides each client's added delay, once a second.

    Stateful only for the deadband, which has to compare against the value
    actually *in force* rather than the last one computed -- otherwise a slow
    drift never crosses the threshold and the delay sits where it was set months
    ago.
    """

    __slots__ = ("cap_ms", "min_samples", "deadband_ms", "_applied_ms",
                 "_capped", "_pacer", "_pacer_rtt_ms", "_spread_ms")

    def __init__(self, *, cap_ms: float = DEFAULT_CAP_MS,
                 min_samples: int = MIN_SAMPLES,
                 deadband_ms: float = DEADBAND_MS) -> None:
        self.cap_ms = cap_ms
        self.min_samples = min_samples
        self.deadband_ms = deadband_ms
        self._applied_ms: dict[str, float] = {}
        self._capped = False
        self._pacer: str | None = None
        self._pacer_rtt_ms = 0.0
        self._spread_ms = 0.0

    def forget(self, client_id: str) -> None:
        """Drop a departed client. Called from the reap path.

        Without this the map grows for the life of the process as players come
        and go -- the same leak `_forget_rumble_state` exists to fix.
        """
        self._applied_ms.pop(client_id, None)

    def compute(
        self, participants: Sequence[Participant], *, enabled: bool
    ) -> dict[str, Verdict]:
        """Each participant's verdict, keyed by client id."""
        if not enabled:
            # Forget what was in force, so switching back on starts clean rather
            # than from a deadband comparison against a stale value.
            self._applied_ms.clear()
            self._capped = False
            self._pacer = None
            self._pacer_rtt_ms = 0.0
            self._spread_ms = 0.0
            return {p.client_id: Verdict(0, "off") for p in participants}

        qualified = [p for p in participants if p.samples >= self.min_samples]

        if len(qualified) < 2:
            # Nothing to level against. One player is not a playing field, and a
            # client still being measured must not be levelled against itself.
            self._capped = False
            self._pacer = None
            self._pacer_rtt_ms = 0.0
            self._spread_ms = 0.0
            return {
                p.client_id: Verdict(0, "off" if p in qualified else "measuring")
                for p in participants
            }

        slowest = max(qualified, key=lambda p: p.rtt_p50_ms)
        quickest = min(qualified, key=lambda p: p.rtt_p50_ms)
        self._pacer = slowest.client_id
        self._pacer_rtt_ms = slowest.rtt_p50_ms
        self._spread_ms = slowest.rtt_p50_ms - quickest.rtt_p50_ms

        verdicts: dict[str, Verdict] = {}
        capped_any = False
        for participant in participants:
            if participant.samples < self.min_samples:
                verdicts[participant.client_id] = Verdict(0, "measuring")
                continue

            raw_ms = (slowest.rtt_p50_ms - participant.rtt_p50_ms) / 2.0
            wanted_ms = min(max(raw_ms, 0.0), self.cap_ms)
            capped = raw_ms > self.cap_ms
            capped_any = capped_any or capped

            applied_ms = self._settle(participant.client_id, wanted_ms)
            verdicts[participant.client_id] = Verdict(
                int(applied_ms * 1e6), "capped" if capped else "levelled"
            )

        self._capped = capped_any
        return verdicts

    def _settle(self, client_id: str, wanted_ms: float) -> float:
        """Apply the deadband and return the value now in force.

        Zero is always applied exactly, however small the move: otherwise a
        residual millisecond or two survives the slowest player leaving, and a
        delay nobody asked for that nothing will ever clear is worse than one
        that twitches.
        """
        current = self._applied_ms.get(client_id)
        if current is None or wanted_ms == 0.0:
            self._applied_ms[client_id] = wanted_ms
            return wanted_ms
        if abs(wanted_ms - current) < self.deadband_ms:
            return current
        self._applied_ms[client_id] = wanted_ms
        return wanted_ms

    def report(self) -> dict[str, object]:
        """What the GUIs show.

        The cap binding is reported rather than left to be inferred: "the delay
        stopped growing" is otherwise indistinguishable from "the connections got
        better", and the decision it should prompt -- look at that one player's
        link -- never gets prompted.
        """
        return {
            "capped": self._capped,
            "cap_ms": round(self.cap_ms, 1),
            "pacer": self._pacer,
            "pacer_rtt_ms": round(self._pacer_rtt_ms, 2),
            "spread_ms": round(self._spread_ms, 2),
            "levelled": sum(1 for ms in self._applied_ms.values() if ms > 0),
        }


def clamp_cap_ms(value: object, *, default: float = DEFAULT_CAP_MS) -> float:
    """Coerce an operator-supplied ceiling. Survives whatever a form sent."""
    try:
        cap = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if cap != cap:  # NaN
        return default
    return min(max(cap, 0.0), MAX_CAP_MS)

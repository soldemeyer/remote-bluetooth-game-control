"""The vocabulary player identification is described in.

Stdlib only: no PyAV, no models, no sockets, no numpy. Everything that crosses
the process boundary or the wire is shaped here, so the parts most likely to
be subtly wrong -- the identity arithmetic, the coordinate conventions -- are
testable with tuples on any machine.

**Rectangles are ``common.screen_regions.Rect``**, reused rather than
redefined. A detection box and a screen region are the same thing measured
differently, both normalised 0..1 against the whole frame, and a second
rectangle type would be one more place for two conventions to drift.
Normalised for the reason ``Rect`` already gives: the capture resolution can
change under us mid-session, and a box in pixels would be silently wrong the
moment it did.

**Coordinates are always against the whole frame**, never against a viewport,
even for an entity found inside one. The client owns several views and has to
decide which of them a label belongs in; handing it a box already expressed
inside a viewport would mean also telling it *which*, and the region names a
client holds do not map one-to-one onto the crops it draws -- ``resolve``
merges regions that touch. Whole-frame coordinates plus "which cell this was
found in" lets every consumer answer its own question with no join.

It is the whole *capture* frame, not the letterboxed picture inside it. The
bars are trimmed on the Bluetooth server, after the merge, by
``screen_state.regions_message``; doing it here as well would trim twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from common.screen_regions import FULL, Rect

__all__ = [
    "ASSIGNMENT_SOURCES",
    "Detection",
    "Evidence",
    "InputTrace",
    "PlayerHint",
    "Track",
    "TrackedPlayer",
    "UNIDENTIFIED",
    "centre_of",
    "region_of",
]

#: What a track carries when no player has been attached to it.
#:
#: Zero rather than ``None`` because it crosses the wire as a number -- and
#: because ``AdapterConfig.number``, which is what a real player id is, starts
#: at one and uses zero for "not numbered yet". An adapter that was never
#: enabled has number 0, so the two meanings agree and every consumer can
#: apply one rule: zero means draw nothing.
UNIDENTIFIED = 0

#: Which signal produced an assignment. Published so the debug view can show
#: it, and so a post-mortem can tell "we recognised them" from "we assumed
#: they were still where they were".
ASSIGNMENT_SOURCES = (
    "viewport",     # this entity owns the viewport it is in
    "appearance",   # matched against that player's gallery
    "input",        # its motion tracks that player's stick
    "continuity",   # it was this player a moment ago and nothing says otherwise
    "none",         # not identified; published without a player
)


def centre_of(box: Rect) -> tuple[float, float]:
    """The middle of a box, in the same normalised space."""
    return box.x + box.width / 2.0, box.y + box.height / 2.0


def region_of(box: Rect, layout: str) -> str:
    """Which cell of ``layout`` this box's centre falls in. Empty for FULL.

    The centre, rather than any overlap: an entity straddling a seam belongs
    to one viewport for our purposes, and which one is a question with a
    single defensible answer. Overlap would put a character walking across the
    middle of the screen in two viewports at once, and every rule downstream
    -- above all "do not show a player their own name" -- would then have to
    decide what that means.
    """
    from common.screen_regions import _CELL, _GRID, regions_for_layout

    names = regions_for_layout(layout)
    if not names:
        return ""

    columns, rows = _GRID[layout]
    cx, cy = centre_of(box)
    # Clamped rather than trusted: a backend may report a box whose centre
    # sits a hair outside the frame, and an index error here would take out
    # the whole sample rather than one detection.
    column = min(max(int(cx * columns), 0), columns - 1)
    row = min(max(int(cy * rows), 0), rows - 1)
    for name in names:
        if _CELL[name] == (column, row):
            return name
    return ""


@dataclass(frozen=True, slots=True)
class Detection:
    """One entity a backend found in one frame.

    No identity and no history -- those are the tracker's job and then the
    identity manager's. Keeping detection this dumb is what lets a backend be
    swapped without touching anything that reasons about players.
    """

    box: Rect
    score: float = 1.0
    #: Optional appearance vector. ``None`` from a backend with no embedding
    #: model, which is an ordinary case rather than a degraded one: in a split
    #: screen, viewport ownership identifies a player with no appearance
    #: matching at all.
    embedding: tuple[float, ...] | None = None


@dataclass(slots=True)
class Track:
    """A detection followed across frames.

    Mutable, unlike everything else here, because it *is* the history: a
    frozen track would mean rebuilding the object every frame for every
    entity, which is allocation on the one path in this subsystem that runs at
    rate.
    """

    track_id: int
    box: Rect
    region: str = ""
    first_ns: int = 0
    last_ns: int = 0
    hits: int = 0
    #: Normalised units per second -- the same space a stick vector is
    #: reported in, so controller correlation compares like with like.
    velocity: tuple[float, float] = (0.0, 0.0)
    embedding: tuple[float, ...] | None = None
    #: Recent ``(ns, cx, cy)``, newest last. Bounded by the tracker.
    history: list[tuple[int, float, float]] = field(default_factory=list)

    @property
    def age_ns(self) -> int:
        return max(0, self.last_ns - self.first_ns)


@dataclass(frozen=True, slots=True)
class TrackedPlayer:
    """One published row: where a player is, and how sure we are.

    A ``player_id`` of ``UNIDENTIFIED`` is a real answer, not a failure. It
    says "something is here and we do not know whose it is", which is what the
    debug view shows and what the player-facing overlay declines to draw.
    """

    track_id: int
    box: Rect
    player_id: int = UNIDENTIFIED
    confidence: float = 0.0
    region: str = ""
    source: str = "none"

    @property
    def identified(self) -> bool:
        return self.player_id != UNIDENTIFIED


@dataclass(frozen=True, slots=True)
class PlayerHint:
    """A player id, and which part of the screen they own.

    Pushed down from the Bluetooth server, which is the only party that knows
    it. The source derives no player identity of its own -- it is *told* the
    viewport map and uses it as a signal, the same way it is told the viewing
    tickets and the players' password.

    **No name.** In external mode the capture machine belongs to somebody else
    and has no business learning who is playing; ids travel down, ids come
    back up, and names are resolved where they already live.
    """

    player_id: int
    regions: tuple[str, ...] = ()

    def owns(self, region: str, layout: str) -> bool:
        """True if this player owns ``region`` in ``layout``.

        Filtered against the layout because a controller carries every
        assignment it might need -- ``upper_left`` for four players *and*
        ``left`` for two -- and only the one belonging to the picture on
        screen means anything right now.
        """
        if not region:
            return False
        from common.screen_regions import regions_for_layout

        return region in self.regions and region in regions_for_layout(layout)


@dataclass(frozen=True, slots=True)
class InputTrace:
    """A short window of one player's stick motion, newest last.

    Each sample is ``(dx, dy)`` in -1..1 at ``hz``. Deliberately small and
    deliberately lossy: this is a *shape* to correlate against, not a replay
    of the input, and the whole point is that a second of it for four players
    fits in a control message beside everything else.
    """

    player_id: int
    hz: float = 20.0
    samples: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True, slots=True)
class Evidence:
    """Everything the identity manager knows that did not come from a frame.

    One object rather than a widening parameter list, so a new signal is a
    field here and a branch in the manager -- and so a caller with none of it
    passes ``Evidence()`` and gets the viewport-and-continuity behaviour,
    which is the whole design with controller correlation switched off.
    """

    layout: str = FULL
    hints: tuple[PlayerHint, ...] = ()
    traces: tuple[InputTrace, ...] = ()

    def hint(self, player_id: int) -> PlayerHint | None:
        for candidate in self.hints:
            if candidate.player_id == player_id:
                return candidate
        return None

    def owner_of(self, region: str) -> int:
        """Which player owns ``region`` in this layout. Zero if nobody does."""
        if not region:
            return UNIDENTIFIED
        for candidate in self.hints:
            if candidate.owns(region, self.layout):
                return candidate.player_id
        return UNIDENTIFIED

    def trace(self, player_id: int) -> InputTrace | None:
        for candidate in self.traces:
            if candidate.player_id == player_id:
                return candidate
        return None

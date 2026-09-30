"""Which player labels a client should be shown.

The join between what the video server saw and who is playing, and
deliberately the only place it happens -- the same three-layer separation
``server/screen_state.py`` documents, of which this is the second half:

  * ``videoserver/playervision`` finds entities and decides which player each
    one is. It knows the layout and is told the viewport map. It knows nothing
    about clients, sessions or adapters.
  * ``common/screen_regions`` resolves region names to rectangles. It knows
    nothing about either.
  * this module knows about both, and about nothing else -- no image
    processing, no sockets, no Qt.

THE RULE, AND WHY IT IS PER VIEWPORT
-------------------------------------
In a split screen a player is not shown their own name over their own
character. They know who they are; the label is there to tell them who
everybody *else* is.

But that exclusion is per **viewport**, not per client, and the difference is
the whole reason this is not a one-liner. One client may hold up to four
controllers and be drawing several viewports at once. A client showing player
1's view and player 2's view must have player 1 hidden in player 1's view and
player 2 hidden in player 2's -- and both of them shown in the *other* view,
because seeing your opponent's name over their character in their own viewport
is exactly the thing this is for.

On a shared screen there is no viewport to own, so every label is shown to
everybody. That is not a fallback -- it is the useful case: with one camera
and four characters, being told which is which is the entire point, and there
is no "your own view" to exclude yourself from.

With one player there is nobody to identify. No labels.

WHAT IS AUTHORITATIVE, AND WHAT IS NOT
---------------------------------------
Whether this is a multiplayer game is answered from the **controller
registry**, never inferred from the video. A shared-screen four-player game
looks exactly like a one-player game to a split-screen detector, so
``split_screen == multiplayer`` is the fragile assumption this has to avoid.
"""

from __future__ import annotations

import logging

from common.screen_regions import FULL, normalise_layout, regions_for_layout

log = logging.getLogger(__name__)

__all__ = [
    "MAX_LABELS",
    "labels_for_client",
    "multiplayer",
    "nothing_to_show",
    "player_hints",
    "player_names",
]

#: The most labels one message will ever carry.
#:
#: The control channel refuses an oversized message **whole** rather than
#: truncating it, so an unbounded list would mean a busy scene silently
#: costing a client every label rather than some. Four players plus a little
#: headroom for entities a backend reports but has not identified.
MAX_LABELS = 8

#: How long a player's own name may be in a label. Anything longer is the
#: same budget problem one field along; names come from clients and are not
#: ours to trust for length.
MAX_NAME = 24


def multiplayer(router) -> bool:
    """Is more than one person playing?

    From the controller registry, which is the authoritative answer, rather
    than from the picture. Two assigned adapters is two players: an adapter
    with nobody on it is hardware, not a person.
    """
    return sum(1 for channel in router.channels() if channel.is_assigned) >= 2


def player_hints(router, layout: str) -> list[dict[str, object]]:
    """The viewport map to push to the video source. Ids only, never names.

    In external mode the capture machine belongs to somebody else and has no
    business learning who is playing. Ids go down, ids come back up in the
    tracks, and names are resolved here, where they already live.

    Only adapters that are both numbered and assigned: an unnumbered adapter
    has no identity to give anybody, and an unassigned one is hardware with
    nobody behind it.
    """
    normalised = normalise_layout(layout)
    allowed = regions_for_layout(normalised)

    hints: list[dict[str, object]] = []
    for channel in router.channels():
        if not channel.is_assigned or not channel.number:
            continue
        regions = sorted(name for name in channel.regions if name in allowed)
        hints.append({"id": channel.number, "r": regions})
    hints.sort(key=lambda hint: hint["id"])
    return hints


def player_names(router) -> dict[int, str]:
    """Player number to the name to draw. Assigned, numbered players only.

    ``username`` is what the player typed into their own client and is what
    they expect to see. ``Player N`` is the fallback rather than the adapter's
    own display name: an operator's label like "spare dongle" is a note about
    hardware, and rendering it over somebody's character would be worse than
    the generic answer.
    """
    names: dict[int, str] = {}
    for channel in router.channels():
        if not channel.is_assigned or not channel.number:
            continue
        name = (channel.username or "").strip()[:MAX_NAME]
        names[channel.number] = name or f"Player {channel.number}"
    return names


def nothing_to_show(layout: str = FULL) -> dict[str, object]:
    """What to send when there are no labels.

    An explicit message rather than silence, for the reason
    ``everyone_full_screen`` gives: a client that *was* drawing labels has to
    be told to stop, and silence cannot say that. It is the answer for a
    single-player game, for a source that has gone quiet, for a client that
    asked not to be sent labels, and for anything that could not be worked
    out.
    """
    return {"layout": normalise_layout(layout), "labels": []}


def labels_for_client(
    router,
    client_id: str,
    layout: str,
    tracks: list[dict[str, object]],
    names: dict[int, str] | None = None,
) -> dict[str, object]:
    """The body of a PLAYER_LABELS message for one client.

    ``tracks`` is what the source reported, already decoded: each one carries
    a player id, a whole-frame normalised box, the region it was found in, and
    a confidence. Coordinates are passed through **untouched** -- see the note
    in ``videoserver/playervision/types``: converting them here would create a
    second thing that has to agree with the crops the client actually applied.
    """
    normalised = normalise_layout(layout)
    if not client_id or not tracks:
        return nothing_to_show(normalised)

    if not multiplayer(router):
        # Nobody to identify. Not an error, and the commonest case of all.
        return nothing_to_show(normalised)

    if names is None:
        names = player_names(router)

    # Which viewports does this client own? Only these need an exclusion --
    # and only in a split, because a shared screen has no viewport to own.
    owned = _regions_owned_by(router, client_id, normalised)

    labels: list[dict[str, object]] = []
    for track in tracks:
        player_id = _int(track.get("p"))
        if player_id <= 0:
            # Unidentified. The source publishes these so its own debug view
            # can show that something is there; there is no name to draw.
            continue
        name = names.get(player_id)
        if not name:
            # A player the source still remembers but who is no longer
            # assigned -- they left, or their adapter was disabled. Their
            # label goes away with them.
            continue

        region = str(track.get("r") or "")
        if normalised != FULL and region and owned.get(region) == player_id:
            # This is that player, in their own viewport. The one exclusion
            # this whole module exists for.
            continue

        labels.append(
            {
                "p": player_id,
                "n": name,
                "t": _int(track.get("t")),
                "x": _unit(track.get("x")),
                "y": _unit(track.get("y")),
                "w": _unit(track.get("w")),
                "h": _unit(track.get("h")),
                "c": round(_float(track.get("c")), 3),
                "r": region,
            }
        )
        if len(labels) >= MAX_LABELS:
            break

    return {"layout": normalised, "labels": labels}


# -- internals -------------------------------------------------------------


def _regions_owned_by(router, client_id: str, layout: str) -> dict[str, int]:
    """Region name -> the player number owning it, for this client's channels.

    Keyed by region rather than collected into a set of players, because the
    exclusion is per viewport: a client holding players 1 and 2 must hide 1 in
    1's viewport and 2 in 2's, not hide both everywhere.
    """
    allowed = regions_for_layout(layout)
    owned: dict[str, int] = {}
    for channel in router.channels():
        if channel.assigned_client != client_id or not channel.number:
            continue
        for name in channel.regions:
            if name in allowed:
                owned[name] = channel.number
    return owned


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    # NaN fails every comparison, so it would sail through a range check and
    # land in the client's geometry as a box that cannot be drawn.
    return result if result == result else 0.0


def _unit(value: object) -> float:
    """A coordinate, clamped into 0..1 and rounded for the wire.

    Clamped rather than rejected: a box a hair outside the frame is an
    ordinary rounding artefact, and dropping the label would be a worse answer
    than moving it a pixel. Three decimals is about a pixel at 1080p, and it
    is what keeps the message small enough to carry four of them.
    """
    return round(min(1.0, max(0.0, _float(value))), 3)

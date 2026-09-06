"""What one client should be shown, given the layout and its controllers.

The join between the two halves of split-screen, and deliberately the only
place it happens:

  * ``videoserver/layout.py`` decides how the picture is divided. It knows
    nothing about controllers, clients or who is watching.
  * ``common/screen_regions.py`` decides which rectangles a set of region names
    resolves to. It knows nothing about adapters or sessions.
  * this module knows about both, and about nothing else -- no image
    processing, no sockets, no Qt.

Keeping the three apart is what makes the middle one testable without a
capture device and the first one testable without a router.

**A client may hold several controllers**, up to four, so it may own several
regions -- and they are gathered here rather than anywhere downstream. Handing
a client one region and quietly dropping its second controller's would show a
player half of what they are entitled to, which looks like a rendering bug
rather than a routing one.
"""

from __future__ import annotations

import logging

from common.screen_regions import FULL, Rect, normalise_layout, resolve

log = logging.getLogger(__name__)


def regions_for_client(router, client_id: str, layout: str) -> list[str]:
    """Every region name this client's controllers hold, in a stable order.

    Order is the vocabulary's, not the router's, so two calls that find the
    same set produce the same list -- which is what lets the caller decide
    whether anything actually changed without comparing sets.
    """
    if not client_id:
        return []

    wanted: set[str] = set()
    for channel in router.channels():
        if channel.assigned_client != client_id:
            continue
        wanted.update(channel.regions)

    if not wanted:
        return []

    # Filtered against the live layout here as well as inside `resolve`, so
    # the message a client receives says what applies *now* rather than
    # everything the operator ever assigned. A client told it holds
    # `upper_left` during a two-player vertical game would have no way to know
    # that name means nothing at the moment.
    from common.screen_regions import regions_for_layout

    allowed = regions_for_layout(normalise_layout(layout))
    return sorted(name for name in wanted if name in allowed)


def crops_for_client(router, client_id: str, layout: str) -> list[Rect]:
    """The rectangles that client should draw. Empty means the whole picture.

    The merge -- and in particular the refusal to merge regions that do not
    touch -- lives in ``common.screen_regions.resolve``. See the note there:
    a bounding box over two opposite quadrants is the whole screen, and taking
    it would hand a player both opponents' views while looking entirely
    correct.
    """
    return resolve(layout, regions_for_client(router, client_id, layout))


def regions_message(router, client_id: str, layout: str) -> dict[str, object]:
    """The body of a VIDEO_REGIONS control message.

    Carries the layout as well as the regions because the client needs both to
    reason about what it was told: the regions alone cannot distinguish "you
    hold nothing in this layout" from "detection is off", and those want the
    same picture but very different diagnostics.

    Sending the rectangles too, rather than only the names, is deliberate. The
    merge rules are the security-relevant part of this feature, and resolving
    them in one place -- on the machine that knows the assignments -- means a
    client cannot get them wrong. The names ride along for the player's own
    display and for logging.
    """
    normalised = normalise_layout(layout)
    names = regions_for_client(router, client_id, normalised)
    rects = resolve(normalised, names)
    return {
        "layout": normalised,
        "regions": names,
        # Normalised 0..1 so the client needs no idea what resolution the
        # source is running, and so a resolution change mid-session does not
        # invalidate what it was told.
        "crops": [
            {"x": r.x, "y": r.y, "w": r.width, "h": r.height} for r in rects
        ],
    }


def everyone_full_screen() -> dict[str, object]:
    """What to send when there is nothing to crop to.

    Used when video detection is off, when there is no source, and as the
    answer to anything that could not be worked out. An explicit message
    rather than silence: a client that was cropping needs to be told to stop.
    """
    return {"layout": FULL, "regions": [], "crops": []}

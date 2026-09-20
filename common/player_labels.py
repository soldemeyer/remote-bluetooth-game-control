"""The wire format for player identification, shared by all three ends.

Stdlib only, no Qt, no PyAV, no sockets -- like ``common/screen_regions.py``
beside it, and for the same reason: three programs have to agree about these
bytes exactly, and the cheapest way to guarantee that is for all three to call
the same function.

Three messages travel, and each is a *complete* statement rather than a delta.
The control channel has no retransmit, so a lost message must cost one update
rather than leaving somebody permanently out of step -- the same discipline
``VIDEO_REGIONS`` and the input path already use.

WHY INTEGERS
-------------
Coordinates go on the wire as integers in 0..``SCALE``, not as floats. The
client decodes these on its **input-loop thread** -- the 500 Hz one this whole
project is built around -- because ``transport.service()`` is called once per
tick. Measured on a realistic four-label message:

    floats   262 B   decode 3.53 us
    ints     238 B   decode 2.72 us

Neither is large. The integer form is 24% cheaper and 9% smaller for one
conversion at a single boundary, so there is no reason to prefer the other.
1/10000 is 0.19 px at 1920 wide, which is finer than anything a label needs.

Region names are sent as two-character codes for the same reason: the
vocabulary is fixed and closed, and ``upper_left`` costs twelve bytes a label
in a message with a hard ceiling.

**The policy layer above this speaks in floats**, and deliberately:
``server/player_overlay`` decides who sees what in normalised 0..1, which is
what the client and the source both reason in, and the integer form exists
only between ``encode`` and ``decode``.
"""

from __future__ import annotations

from common.screen_regions import REGIONS, normalise_layout

__all__ = [
    "MAX_LABELS",
    "MAX_TRACKS",
    "REGION_CODES",
    "SCALE",
    "decode_labels",
    "decode_player_map",
    "decode_tracks",
    "decode_traces",
    "encode_labels",
    "encode_player_map",
    "encode_tracks",
    "encode_traces",
]

#: Fixed-point denominator for every normalised coordinate on the wire.
SCALE = 10000

#: Confidence is 0..100 rather than 0..SCALE: two digits is already finer than
#: any threshold anybody will set, and it saves two bytes per label.
CONFIDENCE_SCALE = 100

#: Two-character codes for the region vocabulary.
#:
#: Written out rather than derived, and that is the second attempt. Taking the
#: initials of each word looks tidier and **collides**: ``lower`` and ``left``
#: both give ``l``, so one of them decodes as the other -- a label silently
#: attributed to the wrong half of the screen, which is the exact leak this
#: whole feature is built to avoid. Caught by round-tripping the vocabulary
#: rather than by reading the code.
#:
#: A hand-written table has its own failure -- drifting from ``REGIONS`` -- so
#: ``tests/test_player_labels_wire.py`` asserts every region has a code, that
#: no two share one, and that every one round-trips.
REGION_CODES: dict[str, str] = {
    "upper_left": "ul",
    "upper_right": "ur",
    "lower_left": "ll",
    "lower_right": "lr",
    "upper": "up",
    "lower": "lo",
    "left": "le",
    "right": "ri",
}
_REGION_NAMES: dict[str, str] = {code: name for name, code in REGION_CODES.items()}

#: The most tracks a source will report in one message, and the most labels a
#: client will be sent. Bounded because ``encode_control`` refuses an
#: oversized message **whole** rather than truncating it: an unbounded list
#: means a busy scene silently costing somebody every label rather than some.
MAX_TRACKS = 12
MAX_LABELS = 8

#: The most input samples one player's trace carries. A second at 20 Hz is
#: what correlation needs; more is a bigger message for no more signal.
MAX_TRACE_SAMPLES = 24


# -- video source -> Bluetooth server --------------------------------------


def encode_tracks(rows, layout: str, pts: int = 0) -> dict[str, object]:
    """``VIDEO_TRACKS``: where each identified entity is.

    ``rows`` are ``TrackedPlayer``-shaped: anything with ``track_id``,
    ``player_id``, ``box``, ``confidence``, ``region`` and ``source``.
    Duck-typed rather than imported, because this module is shared with the
    Bluetooth server and must not reach into the video server's packages.
    """
    body: list[list[object]] = []
    for row in rows:
        if len(body) >= MAX_TRACKS:
            break
        box = row.box
        body.append(
            [
                int(row.track_id) & 0xFFFF,
                int(row.player_id),
                REGION_CODES.get(row.region, ""),
                _to_fixed(box.x),
                _to_fixed(box.y),
                _to_fixed(box.width),
                _to_fixed(box.height),
                _clamp_int(round(float(row.confidence) * CONFIDENCE_SCALE), 0, 100),
                str(row.source or "none")[:12],
            ]
        )
    return {"l": normalise_layout(layout), "pts": int(pts) & 0x7FFFFFFFFFFF, "t": body}


def decode_tracks(body: dict) -> tuple[str, int, list[dict[str, object]]]:
    """Read a ``VIDEO_TRACKS`` body. Never raises; a bad row is skipped.

    The rows come back as plain dicts in normalised floats, which is what
    ``server/player_overlay`` reasons in. A malformed message costs the labels
    it carried and nothing else -- this arrives over the network from a
    machine the operator configured, and a parser that threw would take the
    control session down with it.
    """
    layout = normalise_layout(body.get("l"))
    try:
        pts = int(body.get("pts") or 0)
    except (TypeError, ValueError):
        pts = 0

    rows: list[dict[str, object]] = []
    raw = body.get("t")
    if not isinstance(raw, list):
        return layout, pts, rows

    for entry in raw[:MAX_TRACKS]:
        if not isinstance(entry, list) or len(entry) < 8:
            continue
        try:
            rows.append(
                {
                    "t": int(entry[0]),
                    "p": int(entry[1]),
                    "r": _REGION_NAMES.get(str(entry[2]), ""),
                    "x": _from_fixed(entry[3]),
                    "y": _from_fixed(entry[4]),
                    "w": _from_fixed(entry[5]),
                    "h": _from_fixed(entry[6]),
                    "c": _clamp_int(int(entry[7]), 0, 100) / CONFIDENCE_SCALE,
                    "s": str(entry[8])[:12] if len(entry) > 8 else "none",
                }
            )
        except (TypeError, ValueError):
            continue
    return layout, pts, rows


# -- Bluetooth server -> video source --------------------------------------


def encode_player_map(hints) -> dict[str, object]:
    """``PLAYER_MAP``: which player owns which viewport.

    **Ids only, never names.** In external mode the capture machine belongs to
    somebody else and has no business learning who is playing; ids go down,
    ids come back up, and names are resolved on the machine that already knows
    them.
    """
    body: list[list[object]] = []
    for hint in hints:
        player_id = int(hint.get("id", 0) if isinstance(hint, dict) else hint.player_id)
        regions = hint.get("r", ()) if isinstance(hint, dict) else hint.regions
        if player_id <= 0:
            continue
        body.append(
            [player_id, [REGION_CODES[r] for r in regions if r in REGION_CODES]]
        )
    return {"p": body}


def decode_player_map(body: dict) -> list[tuple[int, tuple[str, ...]]]:
    """Read a ``PLAYER_MAP`` body into ``(player_id, regions)`` pairs."""
    out: list[tuple[int, tuple[str, ...]]] = []
    raw = body.get("p")
    if not isinstance(raw, list):
        return out
    for entry in raw[:16]:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        try:
            player_id = int(entry[0])
        except (TypeError, ValueError):
            continue
        if player_id <= 0:
            continue
        codes = entry[1] if isinstance(entry[1], list) else []
        names = tuple(
            _REGION_NAMES[str(code)] for code in codes if str(code) in _REGION_NAMES
        )
        out.append((player_id, names))
    return out


def encode_traces(traces) -> dict[str, object]:
    """``VIDEO_PLAYER_INPUT``: a short window of each player's stick motion.

    **Screen convention**: ``dx`` positive is rightwards and ``dy`` positive
    is *downwards*, matching normalised frame coordinates -- so a correlation
    against on-screen motion compares like with like, and the video server
    never has to know it is looking at a thumbstick.

    No flip is applied, and that is checked rather than assumed: the stick's
    own Y is already down-positive (``client/input/mapping.py`` binds W to -1
    and S to +1), so it agrees with the frame. A flip "for safety" here would
    invert every correlation and turn the one signal that separates two
    identical characters into the thing that swaps them.

    Each sample is one signed byte per axis, which is about 0.8% of full
    deflection: far finer than a correlation over a second of samples can use.
    """
    body: dict[str, list[int]] = {}
    for trace in traces:
        player_id = int(trace.player_id)
        if player_id <= 0:
            continue
        flat: list[int] = []
        for dx, dy in tuple(trace.samples)[-MAX_TRACE_SAMPLES:]:
            flat.append(_clamp_int(round(float(dx) * 127), -127, 127))
            flat.append(_clamp_int(round(float(dy) * 127), -127, 127))
        if flat:
            body[str(player_id)] = flat
    return {"hz": 20, "p": body}


def decode_traces(body: dict) -> list[tuple[int, float, tuple[tuple[float, float], ...]]]:
    """Read ``VIDEO_PLAYER_INPUT`` into ``(player_id, hz, samples)``."""
    try:
        hz = float(body.get("hz") or 20.0)
    except (TypeError, ValueError):
        hz = 20.0
    hz = min(max(hz, 1.0), 120.0)

    out: list[tuple[int, float, tuple[tuple[float, float], ...]]] = []
    raw = body.get("p")
    if not isinstance(raw, dict):
        return out
    for key, flat in list(raw.items())[:16]:
        try:
            player_id = int(key)
        except (TypeError, ValueError):
            continue
        if player_id <= 0 or not isinstance(flat, list):
            continue
        pairs: list[tuple[float, float]] = []
        for index in range(0, min(len(flat), MAX_TRACE_SAMPLES * 2) - 1, 2):
            try:
                pairs.append((int(flat[index]) / 127.0, int(flat[index + 1]) / 127.0))
            except (TypeError, ValueError):
                continue
        if pairs:
            out.append((player_id, hz, tuple(pairs)))
    return out


# -- Bluetooth server -> client --------------------------------------------


def encode_labels(body: dict) -> dict[str, object]:
    """``PLAYER_LABELS``: what this client should draw.

    Takes what ``server/player_overlay.labels_for_client`` produced -- plain
    dicts in normalised floats -- and puts it on the wire. Names travel once
    in their own table rather than repeated per label, because a player
    appearing in two viewports would otherwise carry their name twice.
    """
    labels: list[list[object]] = []
    names: dict[str, str] = {}
    for label in body.get("labels", [])[:MAX_LABELS]:
        player_id = int(label.get("p", 0))
        if player_id <= 0:
            continue
        names.setdefault(str(player_id), str(label.get("n", ""))[:24])
        labels.append(
            [
                player_id,
                int(label.get("t", 0)) & 0xFFFF,
                REGION_CODES.get(str(label.get("r", "")), ""),
                _to_fixed(label.get("x")),
                _to_fixed(label.get("y")),
                _to_fixed(label.get("w")),
                _to_fixed(label.get("h")),
                _clamp_int(round(_float(label.get("c")) * CONFIDENCE_SCALE), 0, 100),
            ]
        )
    return {
        "l": normalise_layout(body.get("layout")),
        "b": labels,
        "n": names,
    }


def decode_labels(body: dict) -> tuple[str, list[dict[str, object]]]:
    """Read a ``PLAYER_LABELS`` body. Never raises; a bad label is skipped.

    Runs on the client's input-loop thread, so it allocates one dict per label
    and nothing else -- and it fails *open to fewer labels*, never to an
    exception, because an exception here would reach the 500 Hz loop.
    """
    layout = normalise_layout(body.get("l"))
    names = body.get("n")
    if not isinstance(names, dict):
        names = {}

    out: list[dict[str, object]] = []
    raw = body.get("b")
    if not isinstance(raw, list):
        return layout, out

    for entry in raw[:MAX_LABELS]:
        if not isinstance(entry, list) or len(entry) < 8:
            continue
        try:
            player_id = int(entry[0])
            out.append(
                {
                    "player_id": player_id,
                    "track_id": int(entry[1]),
                    "name": str(names.get(str(player_id), ""))[:24],
                    "region": _REGION_NAMES.get(str(entry[2]), ""),
                    "x": _from_fixed(entry[3]),
                    "y": _from_fixed(entry[4]),
                    "w": _from_fixed(entry[5]),
                    "h": _from_fixed(entry[6]),
                    "confidence": _clamp_int(int(entry[7]), 0, 100) / CONFIDENCE_SCALE,
                }
            )
        except (TypeError, ValueError):
            continue
    return layout, out


# -- internals -------------------------------------------------------------


def _float(value: object) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    # NaN fails every comparison, so it sails through a range check and lands
    # in somebody's geometry as a box that cannot be drawn.
    return result if result == result else 0.0


def _clamp_int(value: int, low: int, high: int) -> int:
    return min(high, max(low, int(value)))


def _to_fixed(value: object) -> int:
    """A normalised 0..1 float to its integer form, clamped."""
    return _clamp_int(round(_float(value) * SCALE), 0, SCALE)


def _from_fixed(value: object) -> float:
    try:
        raw = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return _clamp_int(raw, 0, SCALE) / SCALE

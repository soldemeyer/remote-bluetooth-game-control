"""One icon family, as path data.

Stroke geometry on a 24x24 grid, no fills, round caps and joins -- so every
icon reads at the same weight beside every other one, and a single stroke
width keeps them coherent at the sizes actually used (16-20 px).

Path data rather than files because both consumers want something different
from the same source: the web wants one inline `<symbol>` sprite so there is
no second request and no flash of missing icons, and Qt wants a standalone SVG
document per icon to hand to `QSvgRenderer` -- which this project already uses
for the controller artwork. Shipping only files would mean the web fetching 29
of them; shipping only a sprite would mean Qt parsing one to extract each.

Stdlib only, and no Qt import: this sits in `common/` and is read by the web
generator as well as by the client.
"""

from __future__ import annotations

__all__ = ["ICONS", "STROKE_WIDTH", "VIEWBOX", "icon_names", "render_svg", "sprite"]

#: Everything is drawn on this grid. Changing it invalidates every path.
VIEWBOX = 24

#: One weight for the whole family. 1.75 reads cleanly at 16 px without going
#: spindly at 20 -- 1.5 is faint on a dark background, 2 looks heavy next to
#: 13 px label text.
STROKE_WIDTH = 1.75

#: name -> path data. Grouped by what they are for rather than alphabetically,
#: because that is how they get chosen.
ICONS: dict[str, str] = {
    # -- connection ------------------------------------------------------
    "power": "M12 3.5v8.5 M7.4 7a8 8 0 1 0 9.2 0",
    "link": (
        "M10.6 13.4a4 4 0 0 0 5.7 0l2.8-2.8a4 4 0 0 0-5.7-5.7l-1.6 1.6 "
        "M13.4 10.6a4 4 0 0 0-5.7 0l-2.8 2.8a4 4 0 0 0 5.7 5.7l1.6-1.6"
    ),
    "link-off": (
        "M10.6 13.4a4 4 0 0 0 5.7 0l2.8-2.8a4 4 0 0 0-5.7-5.7l-1.6 1.6 "
        "M13.4 10.6a4 4 0 0 0-5.7 0l-2.8 2.8a4 4 0 0 0 5.7 5.7l1.6-1.6 "
        "M3.5 3.5l17 17"
    ),
    "wifi": (
        "M2.5 9.2a15 15 0 0 1 19 0 M6 12.7a10 10 0 0 1 12 0 "
        "M9.3 16.1a5 5 0 0 1 5.4 0 M12 19.3h.01"
    ),
    "bluetooth": "M7.5 7.5l9 9-4.5 3.5V3.5l4.5 3.5-9 9",
    # -- media -----------------------------------------------------------
    "play": "M8.5 5.4l10.5 6.6-10.5 6.6z",
    "stop": "M7.5 7.5h9v9h-9z",
    "video": (
        "M3.5 7.5a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-7a2 2 0 0 1-2-2z "
        "M14.5 10.7l6-3.4v9.4l-6-3.4z"
    ),
    "video-off": (
        "M3.5 7.5a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-7a2 2 0 0 1-2-2z "
        "M14.5 10.7l6-3.4v9.4l-6-3.4z M3.5 3.5l17 17"
    ),
    "volume": (
        "M4 9.5h3.4L12 5.6v12.8L7.4 14.5H4z "
        "M15.6 9.6a3.7 3.7 0 0 1 0 4.8 M18.2 7.2a7.3 7.3 0 0 1 0 9.6"
    ),
    "volume-off": "M4 9.5h3.4L12 5.6v12.8L7.4 14.5H4z M16 10l5 4 M21 10l-5 4",
    "mic": (
        "M12 3.5a2.7 2.7 0 0 1 2.7 2.7v5.6a2.7 2.7 0 0 1-5.4 0V6.2A2.7 2.7 0 0 1 12 3.5z "
        "M6.5 11.3a5.5 5.5 0 0 0 11 0 M12 16.8v3.7 M9.2 20.5h5.6"
    ),
    "fullscreen": (
        "M8.5 3.5H5a1.5 1.5 0 0 0-1.5 1.5v3.5 M15.5 3.5H19a1.5 1.5 0 0 1 1.5 1.5v3.5 "
        "M8.5 20.5H5a1.5 1.5 0 0 1-1.5-1.5v-3.5 M15.5 20.5H19a1.5 1.5 0 0 0 1.5-1.5v-3.5"
    ),
    "fullscreen-exit": (
        "M3.5 8.5H7a1.5 1.5 0 0 0 1.5-1.5V3.5 M20.5 8.5H17a1.5 1.5 0 0 1-1.5-1.5V3.5 "
        "M3.5 15.5H7a1.5 1.5 0 0 1 1.5 1.5v3.5 M20.5 15.5H17a1.5 1.5 0 0 0-1.5 1.5v3.5"
    ),
    # -- hardware --------------------------------------------------------
    "gamepad": (
        "M8 11.5H5 M6.5 10v3 M15.5 11h.01 M18 13h.01 "
        "M8.2 6.5h7.6a4.5 4.5 0 0 1 4.4 3.6l1 5a3 3 0 0 1-5.4 2.4l-1.1-1.5H9.3l-1.1 1.5"
        "a3 3 0 0 1-5.4-2.4l1-5a4.5 4.5 0 0 1 4.4-3.6z"
    ),
    "server": (
        "M4 4.5h16v5H4z M4 14.5h16v5H4z M7.2 7h.01 M7.2 17h.01"
    ),
    "monitor": "M3.5 5h17v11h-17z M9 20.5h6 M12 16v4.5",
    "capture": (
        "M3.5 7.5a2 2 0 0 1 2-2h2l1.2-2h6.6l1.2 2h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-15"
        "a2 2 0 0 1-2-2z M12 8.8a3.4 3.4 0 1 0 0 6.8 3.4 3.4 0 0 0 0-6.8z"
    ),
    "battery": "M2.5 8h15v8h-15z M20 10.5v3 M5 10.5v3",
    # -- measurement -----------------------------------------------------
    "activity": "M2.5 12h4l3-8 4.5 16 3-8h4.5",
    "gauge": "M12 14.5l4-4.5 M12 20.5a8.5 8.5 0 1 1 8.5-8.5",
    "signal": "M4 19v-3 M9.3 19v-7 M14.7 19v-11 M20 19V5",
    # -- actions ---------------------------------------------------------
    # A gear, not a sunburst. The previous drawing was a centre circle with
    # eight radial spokes, which is the universal *brightness* glyph -- it read
    # as a display control sitting in a settings button. The outline is
    # 7 teeth, tip radius 9.4, root 7.2, tooth width 0.44 of the pitch;
    # regenerate from those if it ever needs redrawing.
    "settings": (
        "M19.06 10.59 L21.22 10.16 L21.22 13.84 L19.06 13.41 L17.51 16.64 "
        "L17.51 16.64 L19.19 18.06 L16.31 20.36 L15.30 18.40 L11.81 19.20 "
        "L11.81 19.20 L11.75 21.40 L8.15 20.58 L9.05 18.57 L6.25 16.34 L6.25 "
        "16.34 L4.50 17.66 L2.90 14.34 L5.03 13.79 L5.03 10.21 L5.03 10.21 "
        "L2.90 9.66 L4.50 6.34 L6.25 7.66 L9.05 5.43 L9.05 5.43 L8.15 3.42 "
        "L11.75 2.60 L11.81 4.80 L15.30 5.60 L15.30 5.60 L16.31 3.64 L19.19 "
        "5.94 L17.51 7.36 L19.06 10.59 Z "
        "M12 8.8a3.2 3.2 0 1 0 0 6.4 3.2 3.2 0 0 0 0-6.4z"
    ),
    "refresh": "M20.5 12a8.5 8.5 0 1 0-2.6 6.1 M20.5 6.5v5.5h-5.5",
    "copy": (
        "M9 9h10.5v10.5H9z M5.5 15H4.5A1.5 1.5 0 0 1 3 13.5v-9A1.5 1.5 0 0 1 4.5 3h9"
        "A1.5 1.5 0 0 1 15 4.5v1"
    ),
    "edit": "M4 20h4L19.2 8.8a2.1 2.1 0 0 0-3-3L5 17z",
    "trash": "M4 6.5h16 M9.5 6.5V4h5v2.5 M6.5 6.5L7.5 20h9l1-13.5",
    "search": "M11 4.2a6.8 6.8 0 1 0 0 13.6 6.8 6.8 0 0 0 0-13.6z M16 16l4.5 4.5",
    "plus": "M12 5v14 M5 12h14",
    "close": "M6 6l12 12 M18 6L6 18",
    "menu": "M4 7h16 M4 12h16 M4 17h16",
    # -- feedback --------------------------------------------------------
    "check": "M4.5 12.5l5 5 10-11",
    "alert": "M12 4.2L2.6 20.4h18.8z M12 10v4.6 M12 17.6h.01",
    "info": "M12 3.2a8.8 8.8 0 1 0 0 17.6 8.8 8.8 0 0 0 0-17.6z M12 11v5.6 M12 7.6h.01",
    "chevron-right": "M9.5 5l7 7-7 7",
    "chevron-down": "M5 9.5l7 7 7-7",
    "chevron-up": "M5 14.5l7-7 7 7",
    "eye": (
        "M2.5 12s3.6-6.4 9.5-6.4S21.5 12 21.5 12s-3.6 6.4-9.5 6.4S2.5 12 2.5 12z "
        "M12 9.3a2.7 2.7 0 1 0 0 5.4 2.7 2.7 0 0 0 0-5.4z"
    ),
}


def icon_names() -> tuple[str, ...]:
    """Every icon in the family, in declaration order."""
    return tuple(ICONS)


def render_svg(name: str, *, color: str = "currentColor", size: int | None = None) -> str:
    """One icon as a standalone SVG document.

    For Qt: `QSvgRenderer` needs a complete document, and it will not resolve
    `currentColor` -- there is no cascade to inherit from -- so a caller
    rendering for a widget must pass a real colour.
    """
    try:
        path = ICONS[name]
    except KeyError:
        raise KeyError(f"unknown icon {name!r}; have {', '.join(sorted(ICONS))}") from None

    dimensions = f' width="{size}" height="{size}"' if size else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {VIEWBOX} {VIEWBOX}"'
        f'{dimensions} fill="none" stroke="{color}" stroke-width="{STROKE_WIDTH}"'
        f' stroke-linecap="round" stroke-linejoin="round">'
        f'<path d="{path}"/></svg>'
    )


def sprite() -> str:
    """Every icon as one inline `<symbol>` sprite for the web page.

    Inlined into the document rather than fetched: it is a few kilobytes, and
    a separate request means icons pop in after first paint.
    """
    symbols = "".join(
        f'<symbol id="icon-{name}" viewBox="0 0 {VIEWBOX} {VIEWBOX}">'
        f'<path d="{path}"/></symbol>'
        for name, path in ICONS.items()
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" aria-hidden="true" focusable="false"'
        ' style="position:absolute;width:0;height:0;overflow:hidden">'
        f"<defs>{symbols}</defs></svg>"
    )

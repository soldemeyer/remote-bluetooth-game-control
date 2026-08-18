"""Generate the controller artwork in client/gui/assets/controllers/.

    python -m tools.build_controller_art

**Style: flat silhouette.** A solid body with the controls as light cut-outs,
in the manner of a vinyl-cut or icon-set illustration. No gradients, no bevels,
no fake lighting. An earlier attempt used gradients and highlight crescents to
suggest moulded plastic; it read as cheap 3D clip-art. Flat shapes are honest
about being diagrams, they stay legible when the widget is small, and -- the
practical part -- a cut-out is the ideal target for a press highlight, because
filling it reads instantly as "this control is down".

Quality therefore lives almost entirely in the **silhouettes**. Each family gets
its own shell path rather than sharing a generic blob: an Xbox pad has a dipped
centre and offset sticks, a DualSense is symmetric with a touchpad, an N64 has
three prongs. Get the outline right and the rest is placement.

Why generated rather than eight hand-written files: the eight share their
primitives, and hand-authoring meant tuning each one differently. Here a change
to a primitive improves all of them. The output is **committed**, so the app
needs no tooling at runtime.

Contract with the preview: every control is a group ``id="c_<name>"``.
:mod:`client.gui.controller_layouts` maps those names to logical buttons, and
the preview resolves each one with ``QSvgRenderer.boundsOnElement`` -- art and
hit boxes cannot drift apart.

Qt renders a subset of SVG: shapes, paths, and gradients. Filters, masks and CSS
are silently ignored, which is another reason the flat style suits us.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "client" / "gui" / "assets" / "controllers"

VIEW_W = 460
VIEW_H = 300

FONT = "Segoe UI, Helvetica, Arial, sans-serif"


@dataclass(frozen=True)
class Palette:
    body: str            #: the silhouette
    outline: str         #: body edge; same as body for a pure silhouette
    cut: str             #: control cut-outs
    cut_edge: str
    ink: str             #: label colour on a cut-out
    recess: str          #: stick wells, touchpad


SLATE = Palette(
    body="#232a33", outline="#39424e", cut="#e9eef5", cut_edge="#232a33",
    ink="#1b2129", recess="#151a20",
)


def palette(**overrides) -> Palette:
    return Palette(**{**SLATE.__dict__, **overrides})


# --------------------------------------------------------------------------
# Control specs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Round:
    name: str
    x: float
    y: float
    r: float
    label: str = ""
    fill: str = ""        #: overrides the cut-out colour (coloured face buttons)
    ink: str = ""
    font: float = 0.0
    ring: bool = False    #: hollow, for stick wells and decorative rims


@dataclass(frozen=True)
class Capsule:
    name: str
    x: float
    y: float
    w: float
    h: float
    label: str = ""
    fill: str = ""
    font: float = 11.0


@dataclass(frozen=True)
class Stick:
    name: str
    x: float
    y: float
    r: float = 30.0


@dataclass(frozen=True)
class DPad:
    x: float
    y: float
    arm: float = 19.0
    thickness: float = 19.0


@dataclass(frozen=True)
class Plate:
    """Decoration only: touchpads, vents, logos. Never lights."""

    d: str
    fill: str = ""
    opacity: float = 1.0


@dataclass(frozen=True)
class Spec:
    key: str
    shell: str
    pal: Palette
    controls: list = field(default_factory=list)
    behind: list = field(default_factory=list)


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _label(x: float, y: float, text: str, size: float, colour: str) -> str:
    if not text:
        return ""
    return (
        f'<text x="{x:g}" y="{y + size * 0.35:g}" font-family="{FONT}" '
        f'font-size="{size:g}" font-weight="700" fill="{colour}" '
        f'text-anchor="middle">{_esc(text)}</text>'
    )


def _round(c: Round, pal: Palette) -> str:
    if c.ring:
        return (
            f'<g id="c_{c.name}">'
            f'<circle cx="{c.x:g}" cy="{c.y:g}" r="{c.r:g}" fill="none" '
            f'stroke="{c.fill or pal.cut}" stroke-width="{max(3.0, c.r * 0.22):g}"/>'
            f"</g>"
        )

    fill = c.fill or pal.cut
    size = c.font or (c.r * 1.0)
    return (
        f'<g id="c_{c.name}">'
        f'<circle cx="{c.x:g}" cy="{c.y:g}" r="{c.r:g}" fill="{fill}"/>'
        f'{_label(c.x, c.y, c.label, size, c.ink or pal.ink)}'
        f"</g>"
    )


def _capsule(c: Capsule, pal: Palette) -> str:
    x, y = c.x - c.w / 2, c.y - c.h / 2
    return (
        f'<g id="c_{c.name}">'
        f'<rect x="{x:g}" y="{y:g}" width="{c.w:g}" height="{c.h:g}" '
        f'rx="{c.h / 2:g}" fill="{c.fill or pal.cut}"/>'
        f'{_label(c.x, c.y, c.label, c.font, pal.ink)}'
        f"</g>"
    )


def _stick(c: Stick, pal: Palette) -> str:
    """A ring for the well with a solid cap inside.

    The preview draws its own cap offset by the live stick position, so this is
    what you see at rest. Both shapes are inside the group, so the element
    bounds cover the whole assembly.
    """
    return (
        f'<g id="c_{c.name}">'
        f'<circle cx="{c.x:g}" cy="{c.y:g}" r="{c.r:g}" fill="{pal.recess}" '
        f'stroke="{pal.cut}" stroke-width="{max(3.0, c.r * 0.16):g}"/>'
        f'<circle cx="{c.x:g}" cy="{c.y:g}" r="{c.r * 0.52:g}" fill="{pal.cut}"/>'
        f"</g>"
    )


def _dpad(d: DPad, pal: Palette) -> str:
    """One cross, with each arm separately addressable.

    Drawn as a single continuous outline so it looks like one moulded part;
    four visibly separate pads look wrong. The per-arm ids ride on invisible
    rectangles, which is all the preview needs to place a highlight.
    """
    x, y, a, t = d.x, d.y, d.arm, d.thickness / 2

    cross = (
        f'<path d="M {x - t:g} {y - a - t:g} h {2 * t:g} v {a:g} h {a:g} '
        f'v {2 * t:g} h {-a:g} v {a:g} h {-2 * t:g} v {-a:g} h {-a:g} '
        f'v {-2 * t:g} h {a:g} z" fill="{pal.cut}" stroke-linejoin="round"/>'
    )
    arms = "".join(
        f'<g id="c_{name}"><rect x="{rx:g}" y="{ry:g}" width="{rw:g}" '
        f'height="{rh:g}" fill="#ffffff" opacity="0"/></g>'
        for name, rx, ry, rw, rh in (
            ("dup", x - t, y - a - t, 2 * t, a),
            ("ddown", x - t, y + t, 2 * t, a),
            ("dleft", x - a - t, y - t, a, 2 * t),
            ("dright", x + t, y - t, a, 2 * t),
        )
    )
    return cross + arms


def _plate(p: Plate, pal: Palette) -> str:
    return (
        f'<path d="{p.d}" fill="{p.fill or pal.recess}" '
        f'stroke="{pal.cut}" stroke-width="3" opacity="{p.opacity:g}"/>'
    )


def _draw(items, pal: Palette) -> str:
    out = []
    for item in items:
        if isinstance(item, Round):
            out.append(_round(item, pal))
        elif isinstance(item, Capsule):
            out.append(_capsule(item, pal))
        elif isinstance(item, Stick):
            out.append(_stick(item, pal))
        elif isinstance(item, DPad):
            out.append(_dpad(item, pal))
        elif isinstance(item, Plate):
            out.append(_plate(item, pal))
        elif isinstance(item, str):
            out.append(item)
    return "\n  ".join(out)


def render(spec: Spec) -> str:
    pal = spec.pal
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {VIEW_W} {VIEW_H}" width="{VIEW_W}" height="{VIEW_H}">
  <!-- GENERATED by tools/build_controller_art.py. Do not edit by hand; edit the
       spec in that file and re-run it.
       Flat silhouette style: solid body, controls as light cut-outs. Every
       control is a group id="c_name" that the preview resolves with
       QSvgRenderer.boundsOnElement to place its pressed highlight. -->
  {_draw(spec.behind, pal)}

  <path d="{spec.shell}" fill="{pal.body}" stroke="{pal.outline}" stroke-width="3" stroke-linejoin="round"/>

  {_draw(spec.controls, pal)}
</svg>
"""


# --------------------------------------------------------------------------
# Silhouettes
#
# These carry the recognisability, so each family gets its own rather than
# sharing a generic two-grip blob.
# --------------------------------------------------------------------------

#: Xbox: dipped centre with a raised guide boss, offset sticks, tapered grips.
XBOX_SHELL = (
    "M 230 40 C 214 40 202 47 196 58 L 190 70 "
    "C 182 81 166 85 146 85 "
    "C 100 85 66 114 54 155 "
    "C 42 196 38 236 46 264 "
    "C 55 292 84 300 108 287 "
    "C 129 276 145 252 156 232 "
    "C 168 220 184 215 204 214 "
    "L 256 214 "
    "C 276 215 292 220 304 232 "
    "C 315 252 331 276 352 287 "
    "C 376 300 405 292 414 264 "
    "C 422 236 418 196 406 155 "
    "C 394 114 360 85 314 85 "
    "C 294 85 278 81 270 70 L 264 58 "
    "C 258 47 246 40 230 40 Z"
)

#: DualSense: symmetric, flat top, wide shoulders, longer parallel grips.
PS_SHELL = (
    "M 230 60 C 198 60 172 63 154 68 "
    "C 112 79 82 108 68 150 "
    "C 54 192 48 234 58 264 "
    "C 68 294 98 302 122 288 "
    "C 143 276 158 254 168 234 "
    "C 178 219 192 213 210 212 "
    "L 250 212 "
    "C 268 213 282 219 292 234 "
    "C 302 254 317 276 338 288 "
    "C 362 302 392 294 402 264 "
    "C 412 234 406 192 392 150 "
    "C 378 108 348 79 306 68 "
    "C 288 63 262 60 230 60 Z"
)

#: Switch Pro: rounder shoulders, higher grips, gentler waist.
SWITCH_SHELL = (
    "M 230 50 C 202 50 178 55 160 62 "
    "C 114 79 82 110 68 152 "
    "C 54 194 50 236 62 266 "
    "C 74 296 106 304 130 288 "
    "C 150 275 164 252 174 232 "
    "C 184 219 198 214 214 213 "
    "L 246 213 "
    "C 262 214 276 219 286 232 "
    "C 296 252 310 275 330 288 "
    "C 354 304 386 296 398 266 "
    "C 410 236 406 194 392 152 "
    "C 378 110 346 79 300 62 "
    "C 282 55 258 50 230 50 Z"
)

#: N64: three prongs.
N64_SHELL = (
    "M 132 58 L 328 58 "
    "C 352 58 366 74 368 96 L 372 122 "
    "C 400 128 416 152 416 182 C 416 220 394 248 366 248 "
    "C 340 248 324 226 320 200 L 310 140 L 268 140 L 274 200 "
    "C 278 234 256 260 230 260 C 204 260 182 234 186 200 L 192 140 L 150 140 "
    "L 140 200 C 136 226 120 248 94 248 C 66 248 44 220 44 182 "
    "C 44 152 60 128 88 122 L 92 96 C 94 74 108 58 132 58 Z"
)

#: SNES: rounded rectangle with pronounced side lobes.
SNES_SHELL = (
    "M 92 104 C 62 104 40 122 40 150 L 40 200 C 40 228 62 246 92 246 "
    "L 368 246 C 398 246 420 228 420 200 L 420 150 C 420 122 398 104 368 104 Z"
)

#: NES: a plain brick.
NES_SHELL = (
    "M 52 112 C 44 112 38 118 38 126 L 38 224 C 38 232 44 238 52 238 "
    "L 408 238 C 416 238 422 232 422 224 L 422 126 C 422 118 416 112 408 112 Z"
)

#: Genesis six-button: rounded, wider at the button end.
GENESIS_SHELL = (
    "M 96 110 C 64 110 42 130 42 158 L 42 198 C 42 226 64 246 96 246 "
    "L 356 246 C 396 246 420 224 420 190 L 420 162 C 420 130 396 110 356 110 Z"
)


def _modern_controls(
    *, faces: tuple[tuple[str, str, str], ...],
    lstick: tuple[float, float], rstick: tuple[float, float],
    dpad: tuple[float, float],
    face_centre: tuple[float, float],
    meta: tuple[tuple[str, str, float, float], ...],
    face_r: float = 17.0,
    face_gap: float = 30.0,
) -> list:
    """Face diamond, two sticks, a D-pad and the centre cluster."""
    north, east, south, west = faces
    fx, fy = face_centre

    return [
        Stick("lstick", *lstick),
        DPad(*dpad, 19, 19),
        Round(north[0], fx, fy - face_gap, face_r, north[1], north[2]),
        Round(east[0], fx + face_gap, fy, face_r, east[1], east[2]),
        Round(west[0], fx - face_gap, fy, face_r, west[1], west[2]),
        Round(south[0], fx, fy + face_gap, face_r, south[1], south[2]),
        Stick("rstick", *rstick),
        *[Round(name, x, y, r, label) for name, label, x, y, r in
          [(m[0], m[1], m[2], m[3], 10.0) for m in meta]],
    ]


SPECS: list[Spec] = [
    Spec(
        key="xbox",
        shell=XBOX_SHELL,
        pal=palette(body="#20262d", outline="#3b4550"),
        behind=[
            Capsule("lt", 128, 28, 76, 26, "LT", font=13),
            Capsule("rt", 332, 28, 76, 26, "RT", font=13),
            Capsule("lb", 136, 60, 86, 22, "LB", font=12),
            Capsule("rb", 324, 60, 86, 22, "RB", font=12),
        ],
        controls=[
            Stick("lstick", 130, 122, 29),
            DPad(146, 186, 18, 18),
            Round("y", 338, 108, 17, "Y", "#f2c33c"),
            Round("b", 368, 138, 17, "B", "#e0503f"),
            Round("x", 308, 138, 17, "X", "#3f7fd4"),
            Round("a", 338, 168, 17, "A", "#54ab54"),
            Stick("rstick", 264, 180, 29),
            Round("back", 198, 132, 11),
            Round("start", 262, 132, 11),
            Round("guide", 230, 86, 16),
        ],
    ),
    Spec(
        key="ps5",
        shell=PS_SHELL,
        pal=palette(body="#1c222a", outline="#e9eef5"),
        behind=[
            Capsule("lt", 132, 26, 76, 26, "L2", font=13),
            Capsule("rt", 328, 26, 76, 26, "R2", font=13),
            Capsule("lb", 140, 58, 86, 22, "L1", font=12),
            Capsule("rb", 320, 58, 86, 22, "R1", font=12),
        ],
        controls=[
            Plate("M 188 104 h 84 a 8 8 0 0 1 8 8 v 50 a 8 8 0 0 1 -8 8 h -84 "
                  "a 8 8 0 0 1 -8 -8 v -50 a 8 8 0 0 1 8 -8 z"),
            DPad(124, 134, 18, 18),
            Round("y", 340, 104, 16, "△"),
            Round("b", 370, 134, 16, "○"),
            Round("x", 310, 134, 16, "□"),
            Round("a", 340, 164, 16, "✕"),
            Stick("lstick", 180, 180, 29),
            Stick("rstick", 282, 180, 29),
            Round("back", 152, 104, 9),
            Round("start", 310, 104, 9),
            Round("guide", 231, 188, 11),
            Round("capture", 231, 160, 9),
        ],
    ),
    Spec(
        key="switch",
        shell=SWITCH_SHELL,
        pal=palette(body="#242a33", outline="#3f4a57"),
        behind=[
            Capsule("lt", 140, 24, 74, 26, "ZL", font=13),
            Capsule("rt", 320, 24, 74, 26, "ZR", font=13),
            Capsule("lb", 148, 56, 84, 22, "L", font=12),
            Capsule("rb", 312, 56, 84, 22, "R", font=12),
        ],
        controls=[
            Stick("lstick", 138, 124, 29),
            DPad(158, 190, 18, 18),
            Round("y", 338, 110, 17, "X"),
            Round("b", 368, 140, 17, "A"),
            Round("x", 308, 140, 17, "Y"),
            Round("a", 338, 170, 17, "B"),
            Stick("rstick", 272, 186, 29),
            Round("back", 196, 122, 11, "−"),
            Round("start", 268, 122, 11, "+"),
            Round("guide", 210, 160, 10),
            Round("capture", 254, 160, 10),
        ],
    ),
    Spec(
        key="switch2",
        shell=SWITCH_SHELL,
        pal=palette(body="#1a1f26", outline="#4b5663"),
        behind=[
            Capsule("lt", 140, 24, 74, 26, "ZL", font=13),
            Capsule("rt", 320, 24, 74, 26, "ZR", font=13),
            Capsule("lb", 148, 56, 84, 22, "L", font=12),
            Capsule("rb", 312, 56, 84, 22, "R", font=12),
        ],
        controls=[
            Stick("lstick", 138, 124, 29),
            DPad(158, 190, 18, 18),
            Round("y", 338, 110, 17, "X"),
            Round("b", 368, 140, 17, "A"),
            Round("x", 308, 140, 17, "Y"),
            Round("a", 338, 170, 17, "B"),
            Stick("rstick", 272, 186, 29),
            Round("back", 196, 122, 11, "−"),
            Round("start", 268, 122, 11, "+"),
            Round("guide", 200, 160, 10),
            Round("capture", 231, 160, 10),
            Round("c", 262, 160, 10, "C"),
        ],
    ),
    Spec(
        key="n64",
        shell=N64_SHELL,
        pal=palette(body="#23292f", outline="#454f59"),
        behind=[
            Capsule("lb", 152, 48, 66, 24, "L", font=13),
            Capsule("rb", 308, 48, 66, 24, "R", font=13),
        ],
        controls=[
            DPad(114, 104, 17, 17),
            Stick("lstick", 230, 196, 32),
            Capsule("lt", 230, 254, 54, 24, "Z", font=13),
            Round("start", 230, 100, 15, "S", "#e05244", ink="#ffffff", font=14),
            Round("b", 268, 92, 15, "B", "#3fbf6a", ink="#10231a"),
            Round("a", 300, 122, 19, "A", "#4a7fe0", ink="#0d1a33"),
            Round("cup", 356, 84, 11, "C", "#f2c200", ink="#4a3a06", font=10),
            Round("cright", 380, 108, 11, "C", "#f2c200", ink="#4a3a06", font=10),
            Round("cleft", 332, 108, 11, "C", "#f2c200", ink="#4a3a06", font=10),
            Round("cdown", 356, 132, 11, "C", "#f2c200", ink="#4a3a06", font=10),
        ],
    ),
    Spec(
        key="snes",
        shell=SNES_SHELL,
        pal=palette(body="#262b33", outline="#48505c"),
        behind=[
            Capsule("lb", 108, 92, 84, 24, "L", font=13),
            Capsule("rb", 352, 92, 84, 24, "R", font=13),
        ],
        controls=[
            DPad(110, 176, 20, 20),
            Round("y", 336, 146, 16, "X", "#5f6bd6"),
            Round("b", 368, 178, 16, "A", "#9a5ad0"),
            Round("x", 304, 178, 16, "Y", "#9a5ad0"),
            Round("a", 336, 210, 16, "B", "#5f6bd6"),
            Capsule("back", 198, 196, 54, 18, "SELECT", font=9),
            Capsule("start", 262, 196, 54, 18, "START", font=9),
        ],
    ),
    Spec(
        key="nes",
        shell=NES_SHELL,
        pal=palette(body="#2a2f36", outline="#4d5560"),
        controls=[
            DPad(108, 176, 20, 20),
            Round("b", 306, 178, 19, "B", "#d0483c", ink="#ffffff"),
            Round("a", 360, 178, 19, "A", "#d0483c", ink="#ffffff"),
            Capsule("back", 200, 182, 58, 19, "SELECT", font=9),
            Capsule("start", 266, 182, 58, 19, "START", font=9),
        ],
    ),
    Spec(
        key="genesis",
        shell=GENESIS_SHELL,
        pal=palette(body="#22262c", outline="#464e59"),
        controls=[
            DPad(112, 178, 20, 20),
            Round("x", 278, 208, 16, "A"),
            Round("a", 322, 208, 16, "B"),
            Round("b", 366, 208, 16, "C"),
            Round("y", 278, 160, 16, "X"),
            Round("lb", 322, 160, 16, "Y"),
            Round("rb", 366, 160, 16, "Z"),
            Capsule("back", 196, 152, 54, 19, "MODE", font=10),
            Capsule("start", 196, 204, 60, 20, "START", font=10),
        ],
    ),
]


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for spec in SPECS:
        (OUT_DIR / f"{spec.key}.svg").write_text(render(spec), encoding="utf-8")
    print(f"wrote {len(SPECS)} controller SVGs to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

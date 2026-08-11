"""Per-system controller layouts for the mapping preview.

The preview answers one question while a player is binding controls: *is the
button I just pressed the one I think it is?* So each layout places its controls
where they sit on the real hardware, and lights them from our logical
:class:`~common.state.Button` bits.

**These are visualisations, not emulation.** Choosing "N64" here changes the
picture; the server still presents the generic HID gamepad (or Switch Pro)
profile configured per adapter. Nothing about the wire protocol changes.

**Where the labels come from.** Each layout maps our logical buttons to that
system's names the way an **8BitDo Bluetooth dongle** does, which is what a
player using this rig will actually be holding. That convention is positional:
the *bottom* face button on your pad drives the bottom face button on the
target, regardless of what either is called. It is the reason Nintendo layouts
look "swapped" -- our ``A`` (bottom) is the Switch's ``B``, and our ``B``
(right) is the Switch's ``A``. Naming them positionally rather than by letter is
what keeps muscle memory intact across systems.

Geometry lives in one place so the widget stays a dumb renderer. Coordinates are
in a fixed 400x260 space and scaled to fit at paint time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from common.state import Button

#: Design space. Every layout is authored against this and scaled on paint.
VIEW_W = 400.0
VIEW_H = 260.0


@dataclass(frozen=True, slots=True)
class Control:
    """One drawable control.

    ``button`` is the logical bit that lights it. Zero means decoration (a
    stick well, a cartridge slot) that never lights.
    """

    x: float
    y: float
    shape: str = "circle"          # circle | rect | dpad | capsule | stick
    button: int = 0
    w: float = 0.0
    h: float = 0.0
    r: float = 12.0
    label: str = ""
    #: Face colour when idle. Empty uses the layout's default.
    color: str = ""
    #: Text colour for the label.
    text: str = ""


@dataclass(frozen=True, slots=True)
class Layout:
    """One system's controller."""

    key: str
    name: str
    #: SVG path for the shell, in the 400x260 design space.
    body: str
    controls: tuple[Control, ...] = field(default_factory=tuple)
    shell: str = "#2b3440"
    shell_edge: str = "#151b23"
    idle: str = "#4a5766"
    lit: str = "#38bdf8"
    #: Shown under the picture: how this system's names line up with ours.
    note: str = ""


def _dpad(x: float, y: float, arm: float = 15.0) -> tuple[Control, ...]:
    """Four directional pads around a centre, as separate lightable controls."""
    return (
        Control(x, y - arm, "rect", Button.DPAD_UP, w=arm, h=arm),
        Control(x, y + arm, "rect", Button.DPAD_DOWN, w=arm, h=arm),
        Control(x - arm, y, "rect", Button.DPAD_LEFT, w=arm, h=arm),
        Control(x + arm, y, "rect", Button.DPAD_RIGHT, w=arm, h=arm),
    )


#: A wide, modern two-grip shell (Xbox / PS5 / Switch Pro class).
_MODERN_BODY = (
    "M 118 52 L 282 52 C 330 52 362 88 370 140 L 382 210 "
    "C 388 244 362 262 334 258 C 314 255 300 242 292 224 L 274 184 "
    "L 126 184 L 108 224 C 100 242 86 255 66 258 C 38 262 12 244 18 210 "
    "L 30 140 C 38 88 70 52 118 52 Z"
)

#: A flat rectangular pad (SNES / NES / Genesis class).
_FLAT_BODY = (
    "M 60 78 C 44 78 34 90 34 106 L 34 178 C 34 194 44 206 60 206 "
    "L 340 206 C 356 206 366 194 366 178 L 366 106 C 366 90 356 78 340 78 Z"
)

#: The N64's unmistakable three-pronged shell: a wide top deck with outer grips
#: swept down and a centre handle hanging between them.
_N64_BODY = (
    "M 92 62 L 308 62 "
    "C 330 62 344 76 346 96 L 350 118 "
    "C 372 122 386 140 386 166 C 386 200 368 224 344 224 "
    "C 322 224 308 206 304 182 L 296 136 L 244 136 L 250 186 "
    "C 254 216 232 238 200 238 C 168 238 146 216 150 186 L 156 136 L 104 136 "
    "L 96 182 C 92 206 78 224 56 224 C 32 224 14 200 14 166 "
    "C 14 140 28 122 50 118 L 54 96 C 56 76 70 62 92 62 Z"
)


def _modern_common(
    *,
    face: tuple[tuple[int, str, str], ...],
    shoulder_labels: tuple[str, str, str, str],
    meta: tuple[tuple[int, str], ...],
    sticks_high: bool = True,
) -> tuple[Control, ...]:
    """Shared furniture for the modern two-grip shells.

    ``face`` is ((button, label, colour), ...) in the order
    top, right, bottom, left -- positional, so each system's names land where
    that system actually prints them.
    """
    top, right, bottom, left = face
    lb, rb, lt, rt = shoulder_labels

    controls: list[Control] = [
        # Shoulders and triggers, along the top edge.
        Control(96, 44, "capsule", Button.LEFT_TRIGGER, w=46, h=17, label=lt),
        Control(304, 44, "capsule", Button.RIGHT_TRIGGER, w=46, h=17, label=rt),
        Control(104, 66, "capsule", Button.LEFT_BUMPER, w=52, h=16, label=lb),
        Control(296, 66, "capsule", Button.RIGHT_BUMPER, w=52, h=16, label=rb),

        # Face diamond, right side.
        Control(300, 96, "circle", top[0], r=15, label=top[1], color=top[2]),
        Control(332, 124, "circle", right[0], r=15, label=right[1], color=right[2]),
        Control(268, 124, "circle", left[0], r=15, label=left[1], color=left[2]),
        Control(300, 152, "circle", bottom[0], r=15, label=bottom[1], color=bottom[2]),
    ]

    controls.extend(_dpad(108, 150) if sticks_high else _dpad(108, 132))
    controls.append(Control(160, 96, "stick", Button.LEFT_STICK, r=25))
    controls.append(Control(252, 168, "stick", Button.RIGHT_STICK, r=25))

    for index, (bit, label) in enumerate(meta):
        controls.append(
            Control(168 + index * 32, 128, "circle", bit, r=9, label=label)
        )

    return tuple(controls)


LAYOUTS: tuple[Layout, ...] = (
    Layout(
        key="xbox",
        name="Xbox",
        body=_MODERN_BODY,
        shell="#2f3b33",
        idle="#525f57",
        lit="#7cd44f",
        note="Our A/B/X/Y match Xbox exactly; this is the reference layout.",
        controls=_modern_common(
            face=(
                (Button.Y, "Y", "#f0c020"),
                (Button.B, "B", "#e04a3a"),
                (Button.A, "A", "#5cb85c"),
                (Button.X, "X", "#3b7dd8"),
            ),
            shoulder_labels=("LB", "RB", "LT", "RT"),
            meta=((Button.BACK, "▣"), (Button.GUIDE, "⬤"), (Button.START, "≡")),
        ),
    ),
    Layout(
        key="ps5",
        name="PlayStation 5",
        body=_MODERN_BODY,
        shell="#e8ebef",
        shell_edge="#9aa3ad",
        idle="#4b5563",
        lit="#2f6df6",
        note="Cross is our A, Circle our B, Square our X, Triangle our Y.",
        controls=_modern_common(
            face=(
                (Button.Y, "△", ""),
                (Button.B, "○", ""),
                (Button.A, "✕", ""),
                (Button.X, "□", ""),
            ),
            shoulder_labels=("L1", "R1", "L2", "R2"),
            meta=(
                (Button.CAPTURE, "CRE"),
                (Button.GUIDE, "PS"),
                (Button.START, "OPT"),
            ),
        ),
    ),
    Layout(
        key="switch",
        name="Nintendo Switch",
        body=_MODERN_BODY,
        shell="#33383f",
        idle="#555c66",
        lit="#e60012",
        note=(
            "Nintendo prints A/B and X/Y mirrored. Positionally our A is Switch B, "
            "our B is Switch A, our X is Switch Y, our Y is Switch X."
        ),
        controls=_modern_common(
            face=(
                (Button.Y, "X", ""),
                (Button.B, "A", ""),
                (Button.A, "B", ""),
                (Button.X, "Y", ""),
            ),
            shoulder_labels=("L", "R", "ZL", "ZR"),
            meta=(
                (Button.BACK, "−"),
                (Button.GUIDE, "⌂"),
                (Button.START, "+"),
                (Button.CAPTURE, "◉"),
            ),
        ),
    ),
    Layout(
        key="switch2",
        name="Nintendo Switch 2",
        body=_MODERN_BODY,
        shell="#2a2f36",
        idle="#565d68",
        lit="#e60012",
        note=(
            "Same mapping as Switch. The extra C button has no logical equivalent "
            "of its own and is shown unlit."
        ),
        controls=_modern_common(
            face=(
                (Button.Y, "X", ""),
                (Button.B, "A", ""),
                (Button.A, "B", ""),
                (Button.X, "Y", ""),
            ),
            shoulder_labels=("L", "R", "ZL", "ZR"),
            meta=(
                (Button.BACK, "−"),
                (Button.GUIDE, "⌂"),
                (Button.START, "+"),
                (Button.CAPTURE, "◉"),
            ),
        ) + (Control(232, 200, "circle", 0, r=9, label="C"),),
    ),
    Layout(
        key="n64",
        name="Nintendo 64",
        body=_N64_BODY,
        shell="#c9c6bd",
        shell_edge="#8e8b82",
        idle="#6b6f76",
        lit="#4cc9f0",
        note=(
            "The C buttons follow the right stick on an 8BitDo dongle, so they "
            "light from right-stick direction rather than from face buttons. "
            "Z is our left trigger."
        ),
        controls=(
            # Left prong: D-pad. Right prong: C cluster. Centre: analog stick.
            # Positions are kept clear of the shell edge and of each other --
            # an overlapping control lights its neighbour and misleads exactly
            # the person trying to verify a binding.
            *_dpad(86, 104, 14),
            Control(200, 176, "stick", Button.LEFT_STICK, r=26),
            # Face buttons on the right deck: A large and blue, B green above it.
            Control(268, 128, "circle", Button.A, r=16, label="A", color="#2f5fd0"),
            Control(244, 98, "circle", Button.B, r=13, label="B", color="#1fa04a"),
            # C cluster. On an 8BitDo dongle these follow the right stick, so
            # they light from its directions rather than from face buttons --
            # which is why the labels here are C and not our button names.
            Control(316, 84, "circle", Button.Y, r=9, label="C↑", color="#f2c200"),
            Control(336, 104, "circle", Button.B, r=9, label="C→", color="#f2c200"),
            Control(296, 104, "circle", Button.X, r=9, label="C←", color="#f2c200"),
            Control(316, 124, "circle", Button.A, r=9, label="C↓", color="#f2c200"),
            # Shoulders on the top edge, Z under the centre prong.
            Control(96, 54, "capsule", Button.LEFT_BUMPER, w=48, h=15, label="L"),
            Control(304, 54, "capsule", Button.RIGHT_BUMPER, w=48, h=15, label="R"),
            Control(200, 222, "capsule", Button.LEFT_TRIGGER, w=42, h=16, label="Z"),
            Control(200, 100, "circle", Button.START, r=13, label="S", color="#d23c2e"),
        ),
    ),
    Layout(
        key="snes",
        name="Super Nintendo",
        body=_FLAT_BODY,
        shell="#d9d6d0",
        shell_edge="#a5a29b",
        idle="#6f6a78",
        lit="#7b5cd6",
        note="Positional: our A is SNES B, our B is SNES A, our X is Y, our Y is X.",
        controls=(
            *_dpad(96, 142, 15),
            Control(300, 116, "circle", Button.Y, r=13, label="X", color="#5a63c8"),
            Control(330, 142, "circle", Button.B, r=13, label="A", color="#8f4fc9"),
            Control(270, 142, "circle", Button.X, r=13, label="Y", color="#8f4fc9"),
            Control(300, 168, "circle", Button.A, r=13, label="B", color="#5a63c8"),
            Control(148, 74, "capsule", Button.LEFT_BUMPER, w=54, h=16, label="L"),
            Control(252, 74, "capsule", Button.RIGHT_BUMPER, w=54, h=16, label="R"),
            Control(178, 166, "capsule", Button.BACK, w=34, h=13, label="SEL"),
            Control(222, 166, "capsule", Button.START, w=34, h=13, label="STA"),
        ),
    ),
    Layout(
        key="nes",
        name="NES",
        body=_FLAT_BODY,
        shell="#b9b3a7",
        shell_edge="#7d786e",
        idle="#5c5750",
        lit="#d23c2e",
        note="Two buttons only: our A is NES A, our B is NES B. Everything else is unbound.",
        controls=(
            *_dpad(96, 142, 15),
            Control(268, 142, "circle", Button.B, r=15, label="B", color="#c0392b"),
            Control(318, 142, "circle", Button.A, r=15, label="A", color="#c0392b"),
            Control(178, 150, "capsule", Button.BACK, w=38, h=14, label="SELECT"),
            Control(226, 150, "capsule", Button.START, w=38, h=14, label="START"),
        ),
    ),
    Layout(
        key="genesis",
        name="Sega Genesis",
        body=_FLAT_BODY,
        shell="#2e3136",
        shell_edge="#17191c",
        idle="#565b63",
        lit="#e8a33d",
        note=(
            "Six-button pad. Our X/A/B drive A/B/C; our Y and the bumpers drive "
            "X/Y/Z, matching how an 8BitDo dongle presents it."
        ),
        controls=(
            *_dpad(96, 142, 15),
            Control(252, 160, "circle", Button.X, r=13, label="A", color="#4a4f57"),
            Control(288, 160, "circle", Button.A, r=13, label="B", color="#4a4f57"),
            Control(324, 160, "circle", Button.B, r=13, label="C", color="#4a4f57"),
            Control(252, 124, "circle", Button.Y, r=13, label="X", color="#4a4f57"),
            Control(288, 124, "circle", Button.LEFT_BUMPER, r=13, label="Y", color="#4a4f57"),
            Control(324, 124, "circle", Button.RIGHT_BUMPER, r=13, label="Z", color="#4a4f57"),
            Control(180, 120, "capsule", Button.BACK, w=34, h=13, label="MODE"),
            Control(180, 160, "capsule", Button.START, w=42, h=14, label="START"),
        ),
    ),
)

LAYOUTS_BY_KEY = {layout.key: layout for layout in LAYOUTS}

DEFAULT_LAYOUT = "xbox"


def get_layout(key: str) -> Layout:
    return LAYOUTS_BY_KEY.get(key, LAYOUTS_BY_KEY[DEFAULT_LAYOUT])

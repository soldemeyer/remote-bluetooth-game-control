"""Built-in controller configurations, and how they reach a real device.

A preset says *"the bottom face button drives our A"*. It deliberately does not
say *"raw joystick button 0 drives our A"*, because a raw index is a property of
one device on one platform -- the same 8BitDo pad enumerates differently over
Bluetooth than over USB, and differently again on the Pi than on Windows.
Shipping a table of raw indices would therefore be wrong on a good fraction of
setups, and CLAUDE.md's standing rule applies: a wrong table entry is
indistinguishable from a broken controller.

So presets are **symbolic**, and are resolved against the pad actually in front
of us at the moment they are applied:

1. **SDL's controller database.** ``SDL_GameControllerGetBindForButton`` reports
   exactly where a named control sits on this device, on this platform. That is
   the same database SDL uses itself, so it is right by construction. Exposed by
   the backend as ``pad_bindings()``.
2. **A measured table**, for the one family whose pad was actually measured
   (the 8BitDo N64 Mod Kit): raw indices read off a real one by hand-binding
   every control. Used only where SDL has no entry, like the heuristic.
3. **The existing heuristic**, for pads SDL has no entry for -- the 8BitDo 64,
   the mod kits, most no-name USB pads. :func:`default_joystick_mapping` already
   guesses the usual arrangement; we invert it into the same shape as step 1 and
   run one code path. Results are flagged ``approximate`` so the UI can say so.

Nothing here touches the hot path. Resolution produces an ordinary
:class:`DeviceMapping` with raw indices, exactly as if the player had bound
every control by hand, and ``compile()`` flattens it the same way.

**Per-type, not per-preset.** One preset covers every controller type, because
:class:`ControllerConfiguration` already stores a mapping per type. Choosing
"Xbox Controller" and then "Nintendo 64" gives you the N64 bindings built for
an Xbox pad.

**Position first.** The control names are SDL's, and SDL names *positions*
after an Xbox pad -- ``a`` is the bottom face button on every controller. The
SDL backend turns off SDL2's label-based reporting for Nintendo pads so that
stays true (see ``sdl2_backend.open``). Face buttons therefore map by where they
sit, not by what they print: a Switch Pro pad's B (bottom) drives an Xbox
target's A (bottom). :data:`FAMILIES` records what each pad prints, and
``docs/controller_mapping_matrix.md`` (``python -m tools.build_mapping_matrix``)
lays every default out as a table.

**How a binding is chosen**, most specific last:

1. identity -- the physical control with the target control's own name;
2. :data:`_LAYOUT_OVERRIDES` -- a target that wants something else from every
   pad (the N64's C buttons from the right stick);
3. :data:`_FAMILY_OVERRIDES` -- a pad that needs something else for one target
   (an N64 mod kit has no right face button to give an SNES its A).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from client.gui.controller_config import ControllerConfiguration
from client.gui.controller_layouts import LAYOUTS, Layout
from client.input.mapping import (
    PAD_AXES,
    PAD_BUTTON_BITS,
    AxisBinding,
    DeviceMapping,
    InputSource,
    SourceKind,
    default_joystick_mapping,
    guid_vendor_product,
)
from common.state import Button

log = logging.getLogger(__name__)

#: Control name -> logical bit, inverted once for the identity rule below.
_BIT_TO_CONTROL: dict[int, str] = {
    bit: name for name, bit in PAD_BUTTON_BITS.items()
}


def _hat(mask: int) -> InputSource:
    return InputSource(SourceKind.HAT, 0, mask)


def _btn(index: int) -> InputSource:
    return InputSource(SourceKind.BUTTON, index)


@dataclass(frozen=True, slots=True)
class MeasuredPad:
    """Raw indices read off one real pad, in ``pad_bindings()`` shape.

    The measurement belongs to one device on one platform, which is exactly
    what the module docstring warns about -- so it is used only where SDL has
    no entry, the same place the heuristic would otherwise guess, and a pad
    whose USB ids differ from the one measured is still labelled approximate.
    """

    vendor: int
    product: int
    buttons: dict = field(default_factory=dict)
    axes: dict = field(default_factory=dict)
    #: Where and how it was measured, for the matrix and for the next reader.
    provenance: str = ""

    def bindings(self) -> dict:
        return {"buttons": dict(self.buttons), "axes": dict(self.axes)}

    def matches(self, guid: str) -> bool:
        ids = guid_vendor_product(guid)
        return ids is not None and ids[:2] == (self.vendor, self.product)


@dataclass(frozen=True, slots=True)
class PadFamily:
    """A family of physical gamepads the same preset suits.

    Deliberately *not* a capability list. An earlier version described each
    family by which controls it has, and pruned bindings accordingly -- which
    dropped the N64 kit's C cluster and analog stick because the family was
    written down as stickless, and dropped the DIY kits' Guide button on shells
    that have one. Whether a control exists is decided once, at resolution,
    from SDL's database, a measurement, or the heuristic reading the actual
    device.

    What is left is identity and documentation: the name the player picks, the
    note explaining how that pad is laid out, what it prints on each control,
    and where that knowledge came from.
    """

    key: str
    name: str
    note: str = ""
    #: Control name (SDL's positional vocabulary) -> what this pad prints on
    #: it. Documentation only: nothing resolves through it. Missing entries
    #: fall back to :data:`_DEFAULT_LABELS`.
    labels: dict = field(default_factory=dict)
    #: Where the knowledge in this entry came from, most authoritative first.
    sources: tuple[str, ...] = ()
    measured: MeasuredPad | None = None

    def label(self, control: str) -> str:
        return self.labels.get(control) or _DEFAULT_LABELS.get(control, control)


#: What a control is called when a family says nothing more specific.
_DEFAULT_LABELS: dict[str, str] = {
    "a": "Bottom face button",
    "b": "Right face button",
    "x": "Left face button",
    "y": "Top face button",
    "lb": "Left shoulder",
    "rb": "Right shoulder",
    "left_trigger": "Left trigger",
    "right_trigger": "Right trigger",
    "back": "Select",
    "start": "Start",
    "guide": "Home",
    "misc1": "Capture",
    "touchpad": "Touchpad click",
    "lstick": "Left stick click",
    "rstick": "Right stick click",
    "dpad_up": "D-pad up",
    "dpad_down": "D-pad down",
    "dpad_left": "D-pad left",
    "dpad_right": "D-pad right",
    "left_x": "Left stick X",
    "left_y": "Left stick Y",
    "right_x": "Right stick X",
    "right_y": "Right stick Y",
    "left_x-": "Left stick left",
    "left_x+": "Left stick right",
    "left_y-": "Left stick up",
    "left_y+": "Left stick down",
    "right_x-": "Right stick left",
    "right_x+": "Right stick right",
    "right_y-": "Right stick up",
    "right_y+": "Right stick down",
}

_SDL_DB = (
    "SDL2 game controller database via SDL_GameControllerGetBindForButton -- "
    "positional names, a = bottom face button"
)
_SDL_LABELS_HINT = (
    "SDL2 SDL_hints.h: SDL_GAMECONTROLLER_USE_BUTTON_LABELS defaults to 1 for "
    "Nintendo pads (report by label); the client sets 0 so they report by "
    "position like every other pad"
)

#: The 8BitDo N64 Mod Kit, measured.
#:
#: Every index below was read off a real kit (USB, D-input mode, Windows 11,
#: SDL 2.32.10) by binding each control by hand in the mapping screen -- the
#: player's own "8BitDo DIY Mod Kit - N64" configuration, which this preset
#: reproduces for anyone else with the kit. Two things it records that the
#: generic heuristic gets wrong:
#:
#: * L, R, Z and Start are buttons 6, 7, 8 and 11, not the 4-5-6-7 run every
#:   modern pad uses.
#: * **Axis 2 is a copy of the stick's vertical axis**, not a trigger. The
#:   heuristic binds it as the left trigger, which is how pushing the stick
#:   down once pulled Z -- see ``tests/test_n64_modkit_z.py``. It is absent
#:   here on purpose.
#:
#: The C cluster reports as axes 3 and 4 at full deflection, i.e. it *is* a
#: right stick as far as the host can tell, so it is registered as one. N64 B
#: is registered as the left face button ``x``: on an N64 it sits up and to
#: the left of A, and that is where every N64 emulator puts it on a modern pad.
#: Home, Star and Pair were not measured and are left out rather than guessed.
N64_MODKIT = MeasuredPad(
    vendor=0x2DC8,
    product=0x2869,
    buttons={
        "a": _btn(0),
        "x": _btn(1),
        "lb": _btn(6),
        "rb": _btn(7),
        "left_trigger": _btn(8),
        "start": _btn(11),
        "dpad_up": _hat(0x01),
        "dpad_right": _hat(0x02),
        "dpad_down": _hat(0x04),
        "dpad_left": _hat(0x08),
    },
    axes={
        "left_x": AxisBinding(0),
        "left_y": AxisBinding(1),
        "right_x": AxisBinding(3),
        "right_y": AxisBinding(4),
    },
    provenance=(
        "Hand-bound on a real 8BitDo N64 Mod Kit, USB, D-input mode, "
        "Windows 11, SDL 2.32.10 (GUID 030095fcc82d00006928000000000000)"
    ),
)


FAMILIES: tuple[PadFamily, ...] = (
    PadFamily(
        key="xbox",
        name="Xbox Controller",
        note=(
            "Xbox 360, One, Series and compatible XInput pads. Our A/B/X/Y "
            "match the printed labels exactly. The Share button exists only on "
            "Series pads and is dropped on older ones."
        ),
        labels={
            "a": "A", "b": "B", "x": "X", "y": "Y",
            "lb": "LB", "rb": "RB", "left_trigger": "LT", "right_trigger": "RT",
            "back": "View", "start": "Menu", "guide": "Xbox button",
            "misc1": "Share (Series only)", "lstick": "LS click", "rstick": "RS click",
        },
        sources=(_SDL_DB,),
    ),
    PadFamily(
        key="playstation",
        name="PlayStation Controller",
        note=(
            "DualShock 3/4 and DualSense. Bound positionally, so Cross is our "
            "A, Circle our B, Square our X and Triangle our Y. Create/Share is "
            "Back, Options is Start, the PS button is Guide. The touchpad "
            "click becomes a Switch's Capture."
        ),
        labels={
            "a": "Cross", "b": "Circle", "x": "Square", "y": "Triangle",
            "lb": "L1", "rb": "R1", "left_trigger": "L2", "right_trigger": "R2",
            "back": "Create / Share", "start": "Options", "guide": "PS button",
            "misc1": "Mute (DualSense)", "touchpad": "Touchpad click",
            "lstick": "L3", "rstick": "R3",
        },
        sources=(
            _SDL_DB,
            "SDL2 HIDAPI PS4/PS5 drivers: Create=back, Options=start, "
            "PS=guide, microphone mute=misc1, touchpad click=touchpad",
            "8BitDo Wireless USB Adapter 2 convention: DualShock/DualSense "
            "touchpad click = Switch Capture",
        ),
    ),
    PadFamily(
        key="switch_pro",
        name="Switch Pro Controller",
        note=(
            "Switch and Switch 2 Pro Controllers. Bound by position, so the "
            "physical B button -- the bottom one -- comes through as our A, "
            "matching every other pad. SDL2 reports these by label unless told "
            "otherwise, and the client tells it otherwise."
        ),
        labels={
            "a": "B", "b": "A", "x": "Y", "y": "X",
            "lb": "L", "rb": "R", "left_trigger": "ZL", "right_trigger": "ZR",
            "back": "−", "start": "+", "guide": "Home", "misc1": "Capture",
            "lstick": "Left stick press", "rstick": "Right stick press",
        },
        sources=(_SDL_DB, _SDL_LABELS_HINT),
    ),
    PadFamily(
        key="8bitdo_ultimate",
        name="8BitDo Ultimate",
        note=(
            "Ultimate and Ultimate 2, in XInput mode -- where they present as "
            "an Xbox pad. The rear paddles are not bound: they mirror other "
            "buttons in 8BitDo's own software rather than reporting separately."
        ),
        labels={
            "a": "A", "b": "B", "x": "X", "y": "Y",
            "lb": "LB", "rb": "RB", "left_trigger": "LT", "right_trigger": "RT",
            "back": "View / −", "start": "Menu / +", "guide": "Home",
            "lstick": "LS click", "rstick": "RS click",
        },
        sources=(
            _SDL_DB,
            "8BitDo Ultimate manual: XInput mode presents as an Xbox 360 pad",
        ),
    ),
    PadFamily(
        key="8bitdo_bluetooth",
        name="8BitDo Bluetooth Gamepad",
        note=(
            "SN30 Pro, SN30 Pro+, Pro 2, Lite and relatives. These have several "
            "pairing modes and SDL sees a different device in each; if the "
            "bindings look shuffled, check which mode the pad booted into. "
            "Bound by position in every mode: the bottom button is our A "
            "whatever the pad prints."
        ),
        labels={
            "a": "B", "b": "A", "x": "Y", "y": "X",
            "lb": "L", "rb": "R", "left_trigger": "L2", "right_trigger": "R2",
            "back": "Select", "start": "Start", "guide": "Home",
            "misc1": "Star (mode-dependent)", "lstick": "L3", "rstick": "R3",
        },
        sources=(_SDL_DB, _SDL_LABELS_HINT),
    ),
    PadFamily(
        key="8bitdo_diy",
        name="8BitDo DIY Mod Kit",
        note=(
            "Mod kits fitted into original NES, SNES, Mega Drive, N64 and "
            "similar shells. What the shell has is what gets bound -- an N64 "
            "kit keeps its stick and C cluster, an NES kit has neither. SDL "
            "rarely recognises these, so the bindings are usually approximate; "
            "check them against the preview before playing. For an N64 kit, "
            "the 8BitDo N64 Mod Kit preset is measured rather than guessed."
        ),
        labels={
            "back": "Select (if fitted)", "guide": "Home (if fitted)",
        },
        sources=(
            _SDL_DB,
            "Otherwise the generic heuristic, default_joystick_mapping",
        ),
    ),
    PadFamily(
        key="generic_usb",
        name="Generic USB Controller",
        note=(
            "Any pad without a specific entry. Assumes the common arrangement: "
            "face buttons first, sticks on the low axes, D-pad on a hat. Treat "
            "it as a starting point and correct it against the preview."
        ),
        labels={
            "a": "Button 0", "b": "Button 1", "x": "Button 2", "y": "Button 3",
            "lb": "Button 4", "rb": "Button 5", "back": "Button 6",
            "start": "Button 7", "guide": "Button 8",
            "lstick": "Button 9", "rstick": "Button 10",
            "left_trigger": "Axis 2 (six-axis pads)",
            "right_trigger": "Axis 5 (six-axis pads)",
        },
        sources=(
            _SDL_DB,
            "Otherwise the generic heuristic, default_joystick_mapping",
        ),
    ),
    PadFamily(
        key="8bitdo_n64_modkit",
        name="8BitDo N64 Mod Kit",
        note=(
            "An original N64 controller fitted with 8BitDo's mod kit. Its own "
            "controls drive an N64 target directly -- A, B, the C buttons, L, "
            "R, Z, Start, stick and D-pad. On a modern target the C buttons "
            "are the right stick and Z is the left trigger; there is no right "
            "or top face button to give. Measured on a real kit in D-input "
            "mode; Home, Star and Pair are not bound."
        ),
        labels={
            "a": "A", "x": "B", "lb": "L", "rb": "R", "left_trigger": "Z",
            "start": "Start",
            "left_x": "Analog stick X", "left_y": "Analog stick Y",
            "right_x": "C left / C right", "right_y": "C up / C down",
            "right_x-": "C left", "right_x+": "C right",
            "right_y-": "C up", "right_y+": "C down",
        },
        sources=(
            N64_MODKIT.provenance,
            "8BitDo N64 Mod Kit manual: Star = Switch screenshot; Pair holds "
            "for pairing mode (neither measured, so neither bound)",
            "Where SDL has an entry for the kit, SDL's database wins",
        ),
        measured=N64_MODKIT,
    ),
)

FAMILIES_BY_KEY: dict[str, PadFamily] = {family.key: family for family in FAMILIES}


#: Bindings a target wants from *every* pad that are not a straight
#: name-for-name match.
#:
#: * **N64** -- the C cluster follows the right stick, the way an 8BitDo dongle,
#:   a real 8BitDo 64 and Switch Online's N64 app all present it. N64 B is on
#:   the left face button: on the N64 it sits up and left of A, and RetroArch's
#:   Mupen64Plus core puts it there (A = RetroPad B, B = RetroPad Y). The
#:   right face button is kept as a second source for B, below.
#: * **NES** -- A is the right-hand button and B the left, so A comes from the
#:   right face button and B from the bottom one, as RetroArch's NES cores and
#:   Switch Online's NES app both do. The layout's bits are untouched: only
#:   which physical button drives them.
#: * **Genesis** -- the top row reads X, Y, Z left to right, so X comes from the
#:   left bumper and Y from the top face button (Genesis Plus GX: X = L1,
#:   Y = RetroPad X; the 8BitDo M30 in X-input mode reports the same way).
#: * **GameCube** needs nothing: its layout is already positional -- A bottom,
#:   B left, X right, Y top -- so identity is the positional mapping.
_LAYOUT_OVERRIDES: dict[str, dict[int, str]] = {
    "n64": {
        Button.C_UP: "right_y-",
        Button.C_DOWN: "right_y+",
        Button.C_LEFT: "right_x-",
        Button.C_RIGHT: "right_x+",
        Button.B: "x",
    },
    "nes": {
        Button.A: "b",
        Button.B: "a",
    },
    "genesis": {
        Button.Y: "lb",           # Genesis X
        Button.LEFT_BUMPER: "y",  # Genesis Y
    },
}

#: A second physical control for the same target control -- both fire it.
#:
#: The N64's B from the right face button as well as the left: the N64 has
#: only two face buttons, so the right one would otherwise do nothing, and the
#: built-in used to put B there -- a player used to that keeps it.
_LAYOUT_ALTERNATES: dict[str, dict[int, str]] = {
    "n64": {Button.B: "b"},
}

#: Bindings one family of pads needs for one target, over the rules above.
#:
#: * **PlayStation -> Switch** -- a DualShock or DualSense has no Capture
#:   button; its touchpad click is the conventional stand-in (the 8BitDo
#:   Wireless USB Adapter maps it so). The Mute button stays on our Capture
#:   bit for the PS5 target, where that bit *is* Mute.
#: * **N64 mod kit** -- an N64 controller has A, B and four C buttons where a
#:   modern pad has four face buttons. Targets with a right stick take the C
#:   buttons there. Targets without one borrow C buttons for the face buttons
#:   the kit lacks, each from the C button pointing the same way: right face
#:   from C-right, top face from C-up. NES A and B come from N64 A and B, which
#:   sit in the NES's own arrangement (B left of A). A GameCube target follows
#:   Nintendo's own N64-to-GameCube ports -- Ocarina of Time's Z-targeting
#:   moved to L -- so Z drives L, R stays R, and L becomes Z.
_FAMILY_OVERRIDES: dict[str, dict[str, dict[int, str]]] = {
    "playstation": {
        "switch": {Button.CAPTURE: "touchpad"},
        "switch2": {Button.CAPTURE: "touchpad"},
    },
    "8bitdo_n64_modkit": {
        "nes": {Button.A: "a", Button.B: "x"},
        "snes": {Button.B: "right_x+", Button.Y: "right_y-"},
        "genesis": {Button.B: "right_x+", Button.LEFT_BUMPER: "right_y-"},
        "gamecube": {Button.RIGHT_TRIGGER: "rb", Button.RIGHT_BUMPER: "lb"},
    },
}

#: Trigger bits are bound like any other, via the ``left_trigger`` /
#: ``right_trigger`` control names.
#:
#: They need one piece of care downstream: ``apply_trigger_buttons`` recomputes
#: both bits from the analog values on every poll, so a plain button binding
#: would be cleared between polls -- it would look bound and never fire. The
#: source is therefore the trigger control itself (its axis' pressed half where
#: there is analog travel), and ``CompiledMapping.left_trigger_is_analog`` tells
#: the poll path to synthesize a full-scale value where there is not.
TRIGGER_BITS = frozenset({Button.LEFT_TRIGGER, Button.RIGHT_TRIGGER})

#: Trigger axis name -> the logical bit it also sets.
_TRIGGER_BIT_OF: dict[str, int] = {
    "left_trigger": Button.LEFT_TRIGGER,
    "right_trigger": Button.RIGHT_TRIGGER,
}


@dataclass(slots=True)
class LayoutPreset:
    """What one controller type wants, for one family of pads."""

    layout: str
    #: logical bit -> control expression ("a", or "right_y-" for an axis half).
    buttons: dict[int, str] = field(default_factory=dict)
    #: ControllerState axis names to bind straight through.
    axes: tuple[str, ...] = ()
    #: logical bit -> a second control expression that also fires it.
    alternates: dict[int, str] = field(default_factory=dict)


def build_layout_preset(family: PadFamily, layout: Layout) -> LayoutPreset:
    """Work out which physical control drives each of a layout's buttons.

    Mostly identity, because the layouts already carry the per-system renaming
    -- an SNES pad's "A" is our ``Button.B`` because that is the right-hand
    face button on both. What is left is the two override tables.

    **Every button a layout offers gets a source**, optional ones included.
    Whether the pad in front of us actually has that control is decided once,
    during resolution, from SDL's database, a measurement or the heuristic.
    Pruning here as well meant doing the same job twice from worse
    information: a preset was dropping the N64's C cluster and analog stick
    because the *family* was described as stickless, even for a mod kit
    fitted to an N64 shell that plainly has both.
    """
    overrides = {
        **_LAYOUT_OVERRIDES.get(layout.key, {}),
        **_FAMILY_OVERRIDES.get(family.key, {}).get(layout.key, {}),
    }
    buttons: dict[int, str] = {}

    for bit, _label in layout.bindable():
        control = overrides.get(bit) or _BIT_TO_CONTROL.get(bit)
        if control is not None:
            buttons[bit] = control

    alternates = {
        bit: control
        for bit, control in _LAYOUT_ALTERNATES.get(layout.key, {}).items()
        if bit in buttons and buttons[bit] != control
    }

    axes = tuple(name for name in PAD_AXES if layout.has_axis(name))

    return LayoutPreset(
        layout=layout.key, buttons=buttons, axes=axes, alternates=alternates
    )


def _split_axis(control: str) -> tuple[str | None, int]:
    """``"right_y-"`` -> ``("right_y", -1)``; a plain name -> ``(None, 0)``."""
    if control.endswith(("+", "-")):
        return control[:-1], (1 if control.endswith("+") else -1)
    return None, 0


def build_preset(family: PadFamily) -> tuple[LayoutPreset, ...]:
    """Every controller type's bindings for one family of pads."""
    return tuple(build_layout_preset(family, layout) for layout in LAYOUTS)


# -- resolution ------------------------------------------------------------


def bindings_from_mapping(mapping: DeviceMapping) -> dict:
    """Read a DeviceMapping back as a control-name table.

    Lets the heuristic fallback feed the same resolver as SDL's database, so
    there is one code path rather than two that can drift.
    """
    buttons: dict[str, InputSource] = {}
    for name, bit in PAD_BUTTON_BITS.items():
        source = mapping.buttons.get(bit)
        if source is not None:
            buttons[name] = source

    axes = {name: binding for name, binding in mapping.axes.items()}

    # default_joystick_mapping never binds the trigger *bits* -- it only knows
    # which axis carries the trigger. Derive the digital source from that axis'
    # pressed half, so a layout that asks for LT/RT gets one.
    for name in ("left_trigger", "right_trigger"):
        binding = axes.get(name)
        if name not in buttons and binding is not None:
            buttons[name] = InputSource(
                SourceKind.AXIS, binding.index, -1 if binding.invert else 1
            )

    return {"buttons": buttons, "axes": axes}


def resolve(
    preset: tuple[LayoutPreset, ...],
    device,
    bindings: dict | None,
    measured: MeasuredPad | None = None,
) -> tuple[dict[str, DeviceMapping], bool]:
    """Turn a symbolic preset into real per-type mappings for one device.

    ``bindings`` comes from ``SDL2Backend.pad_bindings()``. When it is None the
    pad is not in SDL's database: a family's ``measured`` table stands in if it
    has one, otherwise the heuristic does. Either is reported back as
    ``approximate=True`` unless the measurement was taken on this very model.
    """
    approximate = False

    if bindings is None and measured is not None:
        approximate = not measured.matches(getattr(device, "guid", ""))
        bindings = measured.bindings()
    elif bindings is None:
        approximate = True
        bindings = bindings_from_mapping(
            default_joystick_mapping(
                device.guid,
                device.name,
                axes=getattr(device, "axis_count", 0),
                buttons=getattr(device, "button_count", 0),
                hats=getattr(device, "hat_count", 0),
            )
        )

    pad_buttons: dict[str, InputSource] = bindings.get("buttons") or {}
    pad_axes: dict[str, AxisBinding] = bindings.get("axes") or {}

    mappings: dict[str, DeviceMapping] = {}
    for entry in preset:
        mapping = DeviceMapping(guid=device.guid, name=device.name)

        for bit, control in entry.buttons.items():
            source = _resolve_control(control, pad_buttons, pad_axes)
            if source is not None:
                mapping.buttons[bit] = source

        for bit, control in entry.alternates.items():
            source = _resolve_control(control, pad_buttons, pad_axes)
            if source is not None and source != mapping.buttons.get(bit):
                # Promotes the alternate to primary where the pad lacks the
                # primary -- an N64 kit has no right face button to be B's
                # second source, but a pad with *only* that one still gets B.
                mapping.bind_button_alt(bit, source)

        for name in entry.axes:
            # A trigger a per-pad rule drives from a plain button -- an N64
            # kit's R standing in for a GameCube's R -- must not also get the
            # pad's trigger axis: compile() would then call it analog, and the
            # poll would recompute the bit from an axis nobody is pulling.
            if name in _TRIGGER_BIT_OF and entry.buttons.get(_TRIGGER_BIT_OF[name]) != name:
                continue
            binding = pad_axes.get(name)
            if binding is not None:
                mapping.axes[name] = binding

        # A stick read only as a source -- the N64's C cluster -- must not be
        # bound as an axis too, or the console would see a right stick the N64
        # does not have.
        mappings[entry.layout] = mapping

    return mappings, approximate


def _resolve_control(
    control: str,
    pad_buttons: dict[str, InputSource],
    pad_axes: dict[str, AxisBinding],
) -> InputSource | None:
    axis, half = _split_axis(control)
    if axis is None:
        return pad_buttons.get(control)

    binding = pad_axes.get(axis)
    if binding is None:
        return None
    # An inverted axis flips which half means "pushed that way".
    return InputSource(SourceKind.AXIS, binding.index, -half if binding.invert else half)


def builtin_configurations() -> list[ControllerConfiguration]:
    """The shipped presets, as markers.

    Deliberately without bindings. A preset is symbolic until it meets a device,
    and the same preset produces different raw indices for different pads, so
    baking one device's indices in at seed time would be wrong for every other
    pad. :func:`mappings_for` does the resolution when the slot is applied.
    """
    return [
        ControllerConfiguration(
            name=family.name,
            layout=LAYOUTS[0].key,
            mappings={},
            builtin=True,
            family=family.key,
        )
        for family in FAMILIES
    ]


def mappings_for(
    configuration: ControllerConfiguration,
    device,
    bindings: dict | None,
) -> tuple[dict[str, DeviceMapping], bool]:
    """The per-type mappings a configuration should install for ``device``.

    An ordinary configuration already holds them. A built-in resolves its
    family's preset against this particular pad, every time -- cheap, and it
    means plugging in a different controller does the right thing without the
    player touching anything.
    """
    if not configuration.builtin:
        return configuration.mappings, configuration.approximate

    family = FAMILIES_BY_KEY.get(configuration.family)
    if family is None:
        log.warning(
            "Built-in configuration %r names unknown family %r",
            configuration.name, configuration.family,
        )
        return {}, False

    return resolve(build_preset(family), device, bindings, family.measured)


def materialise(
    configuration: ControllerConfiguration,
    device,
    bindings: dict | None,
    name: str | None = None,
    *,
    keep_builtin: bool = False,
) -> ControllerConfiguration:
    """Give a configuration real bindings for ``device``, ready to edit.

    A built-in stores none -- it is a rule until it meets a pad -- so the editor
    cannot open one directly. This resolves it into a working copy.

    ``keep_builtin`` decides what the copy *is*. Opening a built-in to look at
    it keeps the flag, so the editor knows to offer only "Save as..." and the
    shipped preset stays intact. Taking a copy under a new name clears it: the
    result belongs to the player and is theirs to overwrite.
    """
    mappings, approximate = mappings_for(configuration, device, bindings)

    return ControllerConfiguration(
        name=name or configuration.name,
        layout=configuration.layout,
        mappings={
            key: DeviceMapping.from_dict(m.to_dict()) for key, m in mappings.items()
        },
        device_guid=getattr(device, "guid", ""),
        device_name=getattr(device, "name", ""),
        approximate=approximate,
        builtin=keep_builtin and configuration.builtin,
        family=configuration.family if keep_builtin else "",
    )

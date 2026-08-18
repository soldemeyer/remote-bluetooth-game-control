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
2. **The existing heuristic**, for pads SDL has no entry for -- the 8BitDo 64,
   the mod kits, most no-name USB pads. :func:`default_joystick_mapping` already
   guesses the usual arrangement; we invert it into the same shape as step 1 and
   run one code path. Results are flagged ``approximate`` so the UI can say so.

Nothing here touches the hot path. Resolution produces an ordinary
:class:`DeviceMapping` with raw indices, exactly as if the player had bound
every control by hand, and ``compile()`` flattens it the same way.

**Per-type, not per-preset.** One preset covers all eight controller types,
because :class:`ControllerConfiguration` already stores a mapping per type.
Choosing "Xbox Controller" and then "Nintendo 64" gives you the N64 bindings
built for an Xbox pad.
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
)
from common.state import Button

log = logging.getLogger(__name__)

#: Control name -> logical bit, inverted once for the identity rule below.
_BIT_TO_CONTROL: dict[int, str] = {
    bit: name for name, bit in PAD_BUTTON_BITS.items()
}


@dataclass(frozen=True, slots=True)
class PadFamily:
    """A family of physical gamepads the same preset suits.

    Deliberately *not* a capability list. An earlier version described each
    family by which controls it has, and pruned bindings accordingly -- which
    dropped the N64 kit's C cluster and analog stick because the family was
    written down as stickless, and dropped the DIY kits' Guide button on shells
    that have one. Whether a control exists is decided once, at resolution,
    from SDL's database or the heuristic reading the actual device.

    What is left is identity and documentation: the name the player picks, and
    the note explaining how that pad is laid out.
    """

    key: str
    name: str
    note: str = ""


FAMILIES: tuple[PadFamily, ...] = (
    PadFamily(
        key="xbox",
        name="Xbox Controller",
        note=(
            "Xbox 360, One, Series and compatible XInput pads. Our A/B/X/Y "
            "match the printed labels exactly. The Share button exists only on "
            "Series pads and is dropped on older ones."
        ),
    ),
    PadFamily(
        key="playstation",
        name="PlayStation Controller",
        note=(
            "DualShock 3/4 and DualSense. Bound positionally, so Cross is our "
            "A, Circle our B, Square our X and Triangle our Y. Create/Share is "
            "Back, Options is Start, the PS button is Guide."
        ),
    ),
    PadFamily(
        key="switch_pro",
        name="Switch Pro Controller",
        note=(
            "Switch and Switch 2 Pro Controllers. SDL already reports these "
            "positionally, so the physical B button -- the bottom one -- comes "
            "through as our A, matching every other pad."
        ),
    ),
    PadFamily(
        key="8bitdo_ultimate",
        name="8BitDo Ultimate",
        note=(
            "Ultimate and Ultimate 2, in XInput mode -- where they present as "
            "an Xbox pad. The rear paddles are not bound: they mirror other "
            "buttons in 8BitDo's own software rather than reporting separately."
        ),
    ),
    PadFamily(
        key="8bitdo_bluetooth",
        name="8BitDo Bluetooth Gamepad",
        note=(
            "SN30 Pro, SN30 Pro+, Pro 2, Lite and relatives. These have several "
            "pairing modes and SDL sees a different device in each; if the "
            "bindings look shuffled, check which mode the pad booted into."
        ),
    ),
    PadFamily(
        key="8bitdo_diy",
        name="8BitDo DIY Mod Kit",
        note=(
            "Mod kits fitted into original NES, SNES, Mega Drive, N64 and "
            "similar shells. What the shell has is what gets bound -- an N64 "
            "kit keeps its stick and C cluster, an NES kit has neither. SDL "
            "rarely recognises these, so the bindings are usually approximate; "
            "check them against the preview before playing."
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
    ),
)

FAMILIES_BY_KEY: dict[str, PadFamily] = {family.key: family for family in FAMILIES}


#: Bindings a layout wants that are not a straight name-for-name match.
#:
#: The N64 is the only one. Its C cluster follows the right stick, the way an
#: 8BitDo dongle presents it, so each C direction is bound to one half of one
#: stick axis. The C buttons have their own logical bits (see
#: controller_layouts.py), so this does not collide with A and B.
_LAYOUT_OVERRIDES: dict[str, dict[int, str]] = {
    "n64": {
        Button.CAPTURE: "right_y-",      # C up
        Button.RIGHT_STICK: "right_y+",  # C down
        Button.GUIDE: "right_x-",        # C left
        Button.BACK: "right_x+",         # C right
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


@dataclass(slots=True)
class LayoutPreset:
    """What one controller type wants, for one family of pads."""

    layout: str
    #: logical bit -> control expression ("a", or "right_y-" for an axis half).
    buttons: dict[int, str] = field(default_factory=dict)
    #: ControllerState axis names to bind straight through.
    axes: tuple[str, ...] = ()


def build_layout_preset(family: PadFamily, layout: Layout) -> LayoutPreset:
    """Work out which physical control drives each of a layout's buttons.

    Almost all of it is identity, because the layouts already carry the
    per-system renaming -- an SNES pad's "A" is our ``Button.B`` because that is
    the right-hand face button on both. What is left is the override table.

    **Every button a layout offers gets a source.** Whether the pad in front of
    us actually has that control is decided once, during resolution, from SDL's
    database or the heuristic. Pruning here as well meant doing the same job
    twice from worse information: a preset was dropping the N64's C cluster and
    analog stick because the *family* was described as stickless, even for a
    mod kit fitted to an N64 shell that plainly has both.
    """
    overrides = _LAYOUT_OVERRIDES.get(layout.key, {})
    buttons: dict[int, str] = {}

    for bit, _label in layout.bindable():
        control = overrides.get(bit) or _BIT_TO_CONTROL.get(bit)
        if control is not None:
            buttons[bit] = control

    axes = tuple(name for name in PAD_AXES if layout.has_axis(name))

    # An override that reads a stick the layout itself does not expose still
    # needs that stick bound as a *source*. The N64 has no right stick of its
    # own, but its C buttons read one.
    return LayoutPreset(layout=layout.key, buttons=buttons, axes=axes)


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
) -> tuple[dict[str, DeviceMapping], bool]:
    """Turn a symbolic preset into real per-type mappings for one device.

    ``bindings`` comes from ``SDL2Backend.pad_bindings()``. When it is None the
    pad is not in SDL's database and we fall back to the heuristic, which is
    reported back as ``approximate=True`` so the UI can label it.
    """
    approximate = False

    if bindings is None:
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

        for name in entry.axes:
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
    """The seven shipped presets, as markers.

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

    return resolve(build_preset(family), device, bindings)


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

"""Physical input -> logical controller mapping.

Every backend ultimately answers one question: *which physical control produces
which logical button*. Expressing that uniformly, rather than burying it in each
backend, buys three things at once:

  * **Unmapped gamepads work.** SDL's GameController database does not know every
    pad. An 8BitDo 64 enumerates as a perfectly good 18-button joystick with no
    mapping, and the old code discarded it entirely -- which looked to the user
    like the controller was undetected.
  * **Remapping is possible.** The player can move any logical button onto any
    physical control, for mapped and unmapped pads alike.
  * **The keyboard is just another source.** A key is a source kind like any
    other, so a keyboard needs no special case downstream.

Hot-path note: :meth:`DeviceMapping.compile` flattens the mapping into plain
tuples once, at acquire time. ``poll()`` then walks those tuples with no dict
lookups and no allocation, because it runs up to 1000 times a second per
controller (see the allocation-sensitivity rule in CLAUDE.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from common.state import Button

#: Axis fields on ControllerState that a stick can drive.
STICK_AXES: tuple[str, ...] = ("left_x", "left_y", "right_x", "right_y")

#: Trigger fields. Kept separate because they are unsigned 0..255, not signed.
TRIGGER_AXES: tuple[str, ...] = ("left_trigger", "right_trigger")

#: Every logical button a player can bind, in the order the mapping UI shows
#: them. GUIDE is deliberately included -- some pads have no home button, and
#: leaving it unbound must be allowed rather than assumed.
BINDABLE_BUTTONS: tuple[tuple[int, str], ...] = (
    (Button.A, "A / Cross / B(Nin)"),
    (Button.B, "B / Circle / A(Nin)"),
    (Button.X, "X / Square / Y(Nin)"),
    (Button.Y, "Y / Triangle / X(Nin)"),
    (Button.LEFT_BUMPER, "Left bumper"),
    (Button.RIGHT_BUMPER, "Right bumper"),
    (Button.LEFT_TRIGGER, "Left trigger"),
    (Button.RIGHT_TRIGGER, "Right trigger"),
    (Button.BACK, "Back / Select / Minus"),
    (Button.START, "Start / Options / Plus"),
    (Button.GUIDE, "Guide / Home"),
    (Button.CAPTURE, "Capture / Share"),
    (Button.LEFT_STICK, "Left stick click"),
    (Button.RIGHT_STICK, "Right stick click"),
    (Button.DPAD_UP, "D-pad up"),
    (Button.DPAD_DOWN, "D-pad down"),
    (Button.DPAD_LEFT, "D-pad left"),
    (Button.DPAD_RIGHT, "D-pad right"),
)

_BUTTON_NAMES = {bit: label for bit, label in BINDABLE_BUTTONS}

#: Neutral names for the controls a modern gamepad physically has.
#:
#: Presets are written against *these* rather than raw indices, because a raw
#: index is a property of one device on one platform. The resolver turns a name
#: into whatever index that control occupies on the pad actually plugged in --
#: see :mod:`client.gui.controller_presets`.
#:
#: The value is the logical bit the control drives in a straight identity
#: mapping. Presets override that freely: the N64's C cluster, for instance,
#: drives four bits from the right stick's four directions.
PAD_BUTTON_BITS: dict[str, int] = {
    "a": Button.A,
    "b": Button.B,
    "x": Button.X,
    "y": Button.Y,
    "lb": Button.LEFT_BUMPER,
    "rb": Button.RIGHT_BUMPER,
    "back": Button.BACK,
    "start": Button.START,
    "guide": Button.GUIDE,
    "lstick": Button.LEFT_STICK,
    "rstick": Button.RIGHT_STICK,
    "dpad_up": Button.DPAD_UP,
    "dpad_down": Button.DPAD_DOWN,
    "dpad_left": Button.DPAD_LEFT,
    "dpad_right": Button.DPAD_RIGHT,
    "misc1": Button.CAPTURE,
    # Whatever produces the trigger -- its analog axis' pressed half on a
    # modern pad, a plain button on a retro one. Both end up driving the same
    # logical bit, so presets refer to it by one name.
    "left_trigger": Button.LEFT_TRIGGER,
    "right_trigger": Button.RIGHT_TRIGGER,
}

#: Physical control names for the analog axes, in the order the UI lists them.
PAD_AXES: tuple[str, ...] = STICK_AXES + TRIGGER_AXES


class SourceKind(IntEnum):
    """Where a binding reads from."""

    BUTTON = 0   #: joystick button index
    AXIS = 1     #: joystick axis pushed past a threshold in one direction
    HAT = 2      #: joystick hat (D-pad) direction
    KEY = 3      #: keyboard key


#: An axis must travel this far from centre before it counts as a button press.
#: Well past any resting drift, well short of a full deflection.
AXIS_PRESS_THRESHOLD = 16384


@dataclass(frozen=True, slots=True)
class InputSource:
    """One physical control, possibly a direction of one."""

    kind: SourceKind
    index: int
    #: AXIS: +1 or -1 for the half of travel that counts.
    #: HAT: the direction bitmask (SDL_HAT_UP etc).
    #: BUTTON/KEY: unused.
    value: int = 0

    def to_dict(self) -> dict:
        return {"kind": int(self.kind), "index": self.index, "value": self.value}

    @classmethod
    def from_dict(cls, data: dict) -> InputSource:
        return cls(
            kind=SourceKind(int(data.get("kind", SourceKind.BUTTON))),
            index=int(data.get("index", 0)),
            value=int(data.get("value", 0)),
        )

    def describe(self) -> str:
        if self.kind is SourceKind.BUTTON:
            return f"Button {self.index}"
        if self.kind is SourceKind.AXIS:
            return f"Axis {self.index}{'+' if self.value >= 0 else '-'}"
        if self.kind is SourceKind.HAT:
            return f"Hat {self.index} {_hat_name(self.value)}"
        return f"Key {self.index}"


@dataclass(frozen=True, slots=True)
class AxisBinding:
    """A physical axis driving a logical stick or trigger."""

    index: int
    invert: bool = False

    def to_dict(self) -> dict:
        return {"index": self.index, "invert": self.invert}

    @classmethod
    def from_dict(cls, data: dict) -> AxisBinding:
        return cls(index=int(data.get("index", 0)), invert=bool(data.get("invert", False)))

    def describe(self) -> str:
        return f"Axis {self.index}{' (inverted)' if self.invert else ''}"


@dataclass(frozen=True, slots=True)
class KeyAxisBinding:
    """Two keys driving one analog axis.

    A keyboard has no analog travel, so a stick axis needs a key per direction.
    Held keys produce full deflection; both or neither produce centre.
    """

    negative: int = 0
    positive: int = 0

    def to_dict(self) -> dict:
        return {"negative": self.negative, "positive": self.positive}

    @classmethod
    def from_dict(cls, data: dict) -> KeyAxisBinding:
        return cls(
            negative=int(data.get("negative", 0)),
            positive=int(data.get("positive", 0)),
        )


@dataclass(slots=True)
class DeviceMapping:
    """How one device's physical controls map onto logical controller state.

    Keyed by GUID rather than instance id: an instance id changes on every
    replug, a GUID identifies the hardware model and persists.
    """

    guid: str = ""
    name: str = ""
    #: logical Button bit -> the physical control that produces it.
    buttons: dict[int, InputSource] = field(default_factory=dict)
    #: An optional *second* control for the same logical button, so a player
    #: can drive one action from two places -- a shoulder button and a paddle,
    #: or a face button and a key. Kept as its own dict rather than making
    #: ``buttons`` hold lists: the poll loop ORs bits, so two sources need no
    #: special handling downstream, and every existing config still loads.
    buttons_alt: dict[int, InputSource] = field(default_factory=dict)
    #: ControllerState axis field name -> physical axis.
    axes: dict[str, AxisBinding] = field(default_factory=dict)
    #: ControllerState axis field name -> key pair. Keyboard sources only.
    key_axes: dict[str, KeyAxisBinding] = field(default_factory=dict)

    def bind_button(self, button: int, source: InputSource | None) -> None:
        """Set the primary source. ``None`` clears both."""
        if source is None:
            self.buttons.pop(button, None)
            self.buttons_alt.pop(button, None)
        else:
            self.buttons[button] = source

    def bind_button_alt(self, button: int, source: InputSource | None) -> None:
        """Set the second source, which fires the same logical button."""
        if source is None:
            self.buttons_alt.pop(button, None)
        elif button in self.buttons:
            self.buttons_alt[button] = source
        else:
            # Nothing to be second to; promote it rather than storing an
            # alternate that the primary lookup would never reach.
            self.buttons[button] = source

    def sources_for(self, button: int) -> list[InputSource]:
        """Every control bound to one logical button, primary first."""
        found = []
        primary = self.buttons.get(button)
        if primary is not None:
            found.append(primary)
        alt = self.buttons_alt.get(button)
        if alt is not None:
            found.append(alt)
        return found

    def bind_axis(self, name: str, binding: AxisBinding | None) -> None:
        if binding is None:
            self.axes.pop(name, None)
        else:
            self.axes[name] = binding

    def is_empty(self) -> bool:
        return not self.buttons and not self.axes and not self.key_axes

    def to_dict(self) -> dict:
        return {
            "guid": self.guid,
            "name": self.name,
            "buttons": {str(bit): src.to_dict() for bit, src in self.buttons.items()},
            "buttons_alt": {
                str(bit): src.to_dict() for bit, src in self.buttons_alt.items()
            },
            "axes": {name: b.to_dict() for name, b in self.axes.items()},
            "key_axes": {name: b.to_dict() for name, b in self.key_axes.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> DeviceMapping:
        mapping = cls(guid=str(data.get("guid", "")), name=str(data.get("name", "")))
        for bit, src in (data.get("buttons") or {}).items():
            try:
                mapping.buttons[int(bit)] = InputSource.from_dict(src)
            except (TypeError, ValueError):
                continue  # tolerate a hand-edited config rather than refusing to start
        for bit, src in (data.get("buttons_alt") or {}).items():
            try:
                mapping.buttons_alt[int(bit)] = InputSource.from_dict(src)
            except (TypeError, ValueError):
                continue
        for name, binding in (data.get("axes") or {}).items():
            if name in STICK_AXES or name in TRIGGER_AXES:
                try:
                    mapping.axes[name] = AxisBinding.from_dict(binding)
                except (TypeError, ValueError):
                    continue
        for name, binding in (data.get("key_axes") or {}).items():
            if name in STICK_AXES or name in TRIGGER_AXES:
                try:
                    mapping.key_axes[name] = KeyAxisBinding.from_dict(binding)
                except (TypeError, ValueError):
                    continue
        return mapping

    def compile(self) -> CompiledMapping:
        """Flatten into tuples for allocation-free polling."""
        return CompiledMapping(
            # Both sources for a bit are emitted side by side. poll() ORs the
            # bits it finds, so a second source needs nothing further: whichever
            # control is pressed sets the same button.
            buttons=tuple(
                (src.kind, src.index, src.value, bit)
                for table in (self.buttons, self.buttons_alt)
                for bit, src in table.items()
            ),
            sticks=tuple(
                (name, self.axes[name].index, self.axes[name].invert)
                for name in STICK_AXES
                if name in self.axes
            ),
            triggers=tuple(
                (name, self.axes[name].index, self.axes[name].invert)
                for name in TRIGGER_AXES
                if name in self.axes
            ),
            left_trigger_is_analog="left_trigger" in self.axes,
            right_trigger_is_analog="right_trigger" in self.axes,
        )


@dataclass(frozen=True, slots=True)
class CompiledMapping:
    """Poll-ready form of a DeviceMapping. Immutable, no dicts, no allocation."""

    buttons: tuple[tuple[int, int, int, int], ...]
    sticks: tuple[tuple[str, int, bool], ...]
    triggers: tuple[tuple[str, int, bool], ...]

    #: Whether an analog axis drives each trigger.
    #:
    #: When one does not -- a retro pad whose Z is a plain button, an N64 kit --
    #: the digital bit alone is not enough: ``apply_trigger_buttons`` recomputes
    #: both trigger bits from the analog values on every poll and would clear
    #: it again immediately. The poll path uses these flags to synthesize a
    #: full-scale analog value instead, so the binding survives and the server
    #: sees a pressed trigger rather than nothing.
    left_trigger_is_analog: bool = True
    right_trigger_is_analog: bool = True


def button_label(bit: int) -> str:
    return _BUTTON_NAMES.get(bit, f"Button {bit:#x}")


def _hat_name(mask: int) -> str:
    # SDL hat bits: 1=up 2=right 4=down 8=left.
    names = []
    if mask & 0x01:
        names.append("up")
    if mask & 0x02:
        names.append("right")
    if mask & 0x04:
        names.append("down")
    if mask & 0x08:
        names.append("left")
    return "+".join(names) or "centre"


def default_joystick_mapping(
    guid: str, name: str, *, axes: int, buttons: int, hats: int
) -> DeviceMapping:
    """A best-effort starting point for a pad SDL has no mapping for.

    This is a **guess**, not a verified per-device layout, and the UI says so.
    Most pads follow the same rough convention -- face buttons first, sticks on
    axes 0/1 and 2/3 (or 3/4), D-pad on hat 0 -- so this usually lands close
    enough to be recognisable, and the player corrects the rest in the mapping
    screen with the live preview showing exactly what each control does.

    Deliberately not a lookup table of specific devices: a wrong table entry is
    indistinguishable from a broken controller, whereas an obviously-approximate
    default invites the player to check it.
    """
    mapping = DeviceMapping(guid=guid, name=name)

    # Face buttons, in the order nearly every pad reports them.
    for index, bit in enumerate((Button.A, Button.B, Button.X, Button.Y)):
        if index < buttons:
            mapping.buttons[bit] = InputSource(SourceKind.BUTTON, index)

    # Shoulders, then the usual back/guide/start cluster.
    for index, bit in (
        (4, Button.LEFT_BUMPER),
        (5, Button.RIGHT_BUMPER),
        (6, Button.BACK),
        (7, Button.START),
        (8, Button.GUIDE),
        (9, Button.LEFT_STICK),
        (10, Button.RIGHT_STICK),
    ):
        if index < buttons:
            mapping.buttons[bit] = InputSource(SourceKind.BUTTON, index)

    if hats > 0:
        for mask, bit in (
            (0x01, Button.DPAD_UP),
            (0x02, Button.DPAD_RIGHT),
            (0x04, Button.DPAD_DOWN),
            (0x08, Button.DPAD_LEFT),
        ):
            mapping.buttons[bit] = InputSource(SourceKind.HAT, 0, mask)

    if axes >= 2:
        mapping.axes["left_x"] = AxisBinding(0)
        mapping.axes["left_y"] = AxisBinding(1)
    if axes >= 4:
        # Pads with 6 axes almost always put the triggers on 2 and 5, with the
        # right stick on 3/4. Four-axis pads put the right stick on 2/3.
        if axes >= 6:
            mapping.axes["right_x"] = AxisBinding(3)
            mapping.axes["right_y"] = AxisBinding(4)
            mapping.axes["left_trigger"] = AxisBinding(2)
            mapping.axes["right_trigger"] = AxisBinding(5)
        else:
            mapping.axes["right_x"] = AxisBinding(2)
            mapping.axes["right_y"] = AxisBinding(3)

    return mapping


#: Default keyboard layout. WASD moves, arrows are the right stick, and the face
#: buttons sit under the right hand. Qt key codes are resolved lazily so this
#: module stays importable without Qt (``common/`` neighbours must run headless).
DEFAULT_KEYBOARD_BINDINGS: tuple[tuple[int, str], ...] = (
    (Button.A, "Space"),
    (Button.B, "F"),
    (Button.X, "E"),
    (Button.Y, "R"),
    (Button.LEFT_BUMPER, "Q"),
    (Button.RIGHT_BUMPER, "Tab"),
    (Button.LEFT_TRIGGER, "1"),
    (Button.RIGHT_TRIGGER, "3"),
    (Button.BACK, "Backspace"),
    (Button.START, "Return"),
    (Button.GUIDE, "Escape"),
    (Button.DPAD_UP, "Up"),
    (Button.DPAD_DOWN, "Down"),
    (Button.DPAD_LEFT, "Left"),
    (Button.DPAD_RIGHT, "Right"),
)

#: Keyboard "stick" keys: which keys push which axis, and in which direction.
DEFAULT_KEYBOARD_AXES: tuple[tuple[str, str, int], ...] = (
    ("left_y", "W", -1),
    ("left_y", "S", +1),
    ("left_x", "A", -1),
    ("left_x", "D", +1),
)

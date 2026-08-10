"""Normalized controller state.

This is the vendor-neutral representation that flows across the wire. Client
input backends translate whatever the OS gives them *into* this; server BT
profiles translate this *out* into target-specific HID reports.

Keeping the wire format independent of both ends means a DualShock on the client
can drive a Switch Pro Controller on the server without either side knowing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntFlag


class Button(IntFlag):
    """Logical buttons, laid out in a vendor-neutral way.

    Names follow the Xbox convention because it is the most widely understood,
    but these are logical positions: ``A`` is always the bottom face button
    regardless of what the physical pad prints on it.
    """

    NONE = 0

    A = 1 << 0          # bottom face
    B = 1 << 1          # right face
    X = 1 << 2          # left face
    Y = 1 << 3          # top face

    LEFT_BUMPER = 1 << 4
    RIGHT_BUMPER = 1 << 5

    BACK = 1 << 6       # select / minus / share
    START = 1 << 7      # menu / plus / options
    GUIDE = 1 << 8      # home / PS / Xbox button

    LEFT_STICK = 1 << 9
    RIGHT_STICK = 1 << 10

    DPAD_UP = 1 << 11
    DPAD_DOWN = 1 << 12
    DPAD_LEFT = 1 << 13
    DPAD_RIGHT = 1 << 14

    CAPTURE = 1 << 15   # Switch capture / PS create button

    # Triggers also surface as digital buttons so profiles targeting pads with
    # digital-only triggers do not have to invent a threshold themselves.
    LEFT_TRIGGER = 1 << 16
    RIGHT_TRIGGER = 1 << 17


#: Axis values are int16. SDL2 uses this range natively, so the common case is a
#: straight copy with no rescaling.
AXIS_MIN = -32768
AXIS_MAX = 32767

#: Trigger values are uint8. SDL2 reports triggers as 0..32767; we scale down
#: because 8 bits is more than any target profile actually consumes, and it
#: keeps the packet small.
TRIGGER_MIN = 0
TRIGGER_MAX = 255

#: Threshold above which an analog trigger also sets its digital Button bit.
TRIGGER_DIGITAL_THRESHOLD = 128


@dataclass(slots=True)
class ControllerState:
    """A complete snapshot of one controller.

    Deliberately a full snapshot rather than a delta: a dropped packet is then
    self-healing, since the next packet supersedes it entirely. See CLAUDE.md.

    ``slots=True`` because these are created at up to 1000 Hz and the dict-free
    layout measurably reduces allocation churn.
    """

    buttons: int = 0
    left_x: int = 0
    left_y: int = 0
    right_x: int = 0
    right_y: int = 0
    left_trigger: int = 0
    right_trigger: int = 0

    def is_neutral(self) -> bool:
        """True if nothing is pressed and all sticks are centered.

        Used to decide whether a freshly-connected controller needs an initial
        report sent.
        """
        return (
            self.buttons == 0
            and self.left_x == 0
            and self.left_y == 0
            and self.right_x == 0
            and self.right_y == 0
            and self.left_trigger == 0
            and self.right_trigger == 0
        )

    def copy_into(self, other: ControllerState) -> None:
        """Copy this state into ``other`` without allocating.

        The input loop keeps two long-lived ControllerState objects and swaps
        between them rather than allocating per poll.
        """
        other.buttons = self.buttons
        other.left_x = self.left_x
        other.left_y = self.left_y
        other.right_x = self.right_x
        other.right_y = self.right_y
        other.left_trigger = self.left_trigger
        other.right_trigger = self.right_trigger

    def differs_from(self, other: ControllerState, *, axis_deadband: int = 0) -> bool:
        """Change detection for send-on-change.

        ``axis_deadband`` suppresses packets from analog stick jitter on worn
        hardware. It is applied per-axis, not to the vector magnitude, which is
        cheaper and adequate for the purpose. Buttons always compare exactly --
        a dropped button press is far worse than an extra packet.
        """
        if self.buttons != other.buttons:
            return True
        if axis_deadband <= 0:
            return (
                self.left_x != other.left_x
                or self.left_y != other.left_y
                or self.right_x != other.right_x
                or self.right_y != other.right_y
                or self.left_trigger != other.left_trigger
                or self.right_trigger != other.right_trigger
            )
        return (
            abs(self.left_x - other.left_x) > axis_deadband
            or abs(self.left_y - other.left_y) > axis_deadband
            or abs(self.right_x - other.right_x) > axis_deadband
            or abs(self.right_y - other.right_y) > axis_deadband
            or abs(self.left_trigger - other.left_trigger) > axis_deadband
            or abs(self.right_trigger - other.right_trigger) > axis_deadband
        )

    def clear(self) -> None:
        """Reset to neutral.

        Sent when a controller disconnects so the console does not latch the
        last-held input -- otherwise a player dropping out mid-press leaves the
        character running into a wall forever.
        """
        self.buttons = 0
        self.left_x = 0
        self.left_y = 0
        self.right_x = 0
        self.right_y = 0
        self.left_trigger = 0
        self.right_trigger = 0

    def apply_trigger_buttons(self) -> None:
        """Derive the digital trigger bits from the analog values."""
        if self.left_trigger >= TRIGGER_DIGITAL_THRESHOLD:
            self.buttons |= Button.LEFT_TRIGGER
        else:
            self.buttons &= ~Button.LEFT_TRIGGER
        if self.right_trigger >= TRIGGER_DIGITAL_THRESHOLD:
            self.buttons |= Button.RIGHT_TRIGGER
        else:
            self.buttons &= ~Button.RIGHT_TRIGGER


@dataclass(slots=True)
class ControllerInfo:
    """Descriptive metadata about a controller slot.

    Travels on the reliable control channel at connect time and whenever the
    user edits it -- never on the hot path.
    """

    slot: int
    username: str = ""
    device_name: str = ""
    enabled: bool = True

    #: Set by the server: which BT adapter this controller is routed to, or None.
    assigned_adapter: str | None = field(default=None, compare=False)


def clamp_axis(value: int) -> int:
    """Clamp to int16 range so a misbehaving backend cannot corrupt the packet."""
    if value < AXIS_MIN:
        return AXIS_MIN
    if value > AXIS_MAX:
        return AXIS_MAX
    return value


def clamp_trigger(value: int) -> int:
    """Clamp to uint8 range."""
    if value < TRIGGER_MIN:
        return TRIGGER_MIN
    if value > TRIGGER_MAX:
        return TRIGGER_MAX
    return value


def scale_sdl_trigger(value: int) -> int:
    """Convert SDL2's 0..32767 trigger range to our 0..255.

    Right shift by 7 rather than a divide: exact for the range, and avoids
    float conversion on the hot path.
    """
    if value <= 0:
        return 0
    scaled = value >> 7
    return 255 if scaled > 255 else scaled

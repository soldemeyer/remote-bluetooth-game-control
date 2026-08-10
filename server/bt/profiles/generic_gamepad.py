"""Generic Bluetooth HID gamepad profile.

The most broadly compatible target: anything that speaks standard Bluetooth HID
will accept this. Works with 8BitDo/Mayflash-class wireless adapters, PCs,
Android, and Steam Deck.

The report descriptor deliberately mirrors the layout Windows, Linux and
Android all map cleanly without a per-device quirk entry:

    X, Y, Z, Rz   -- two analog sticks, 16-bit signed
    Rx, Ry        -- analog triggers, 8-bit unsigned
    Hat switch    -- 4-bit D-pad, values 0-7 clockwise from north, 8 = centered
    Buttons 1-14  -- digital

Report layout, 11 bytes after the report id:

    offset 0-1   left X    int16 LE
    offset 2-3   left Y    int16 LE
    offset 4-5   right X   int16 LE
    offset 6-7   right Y   int16 LE
    offset 8     left trigger   uint8
    offset 9     right trigger  uint8
    offset 10    hat (low nibble) | buttons 1-4 (high nibble)
    offset 11-12 buttons 5-14
"""

from __future__ import annotations

import struct

from common.state import Button, ControllerState
from server.bt.profiles.base import ProfileDescriptor, TargetProfile

REPORT_ID = 0x01

# fmt: off
_REPORT_DESCRIPTOR = bytes([
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x05,        # Usage (Game Pad)
    0xA1, 0x01,        # Collection (Application)
    0x85, REPORT_ID,   #   Report ID (1)

    # --- Sticks: 4 axes, 16-bit signed ---
    0x09, 0x01,        #   Usage (Pointer)
    0xA1, 0x00,        #   Collection (Physical)
    0x09, 0x30,        #     Usage (X)
    0x09, 0x31,        #     Usage (Y)
    0x09, 0x32,        #     Usage (Z)
    0x09, 0x35,        #     Usage (Rz)
    0x16, 0x00, 0x80,  #     Logical Minimum (-32768)
    0x26, 0xFF, 0x7F,  #     Logical Maximum (32767)
    0x75, 0x10,        #     Report Size (16)
    0x95, 0x04,        #     Report Count (4)
    0x81, 0x02,        #     Input (Data, Var, Abs)
    0xC0,              #   End Collection

    # --- Triggers: 2 axes, 8-bit unsigned ---
    0x09, 0x33,        #   Usage (Rx)
    0x09, 0x34,        #   Usage (Ry)
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x00,  #   Logical Maximum (255)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x02,        #   Report Count (2)
    0x81, 0x02,        #   Input (Data, Var, Abs)

    # --- D-pad as a hat switch ---
    0x05, 0x01,        #   Usage Page (Generic Desktop)
    0x09, 0x39,        #   Usage (Hat switch)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x07,        #   Logical Maximum (7)
    0x35, 0x00,        #   Physical Minimum (0)
    0x46, 0x3B, 0x01,  #   Physical Maximum (315 degrees)
    0x65, 0x14,        #   Unit (English Rotation: degrees)
    0x75, 0x04,        #   Report Size (4)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x42,        #   Input (Data, Var, Abs, Null State)
    0x65, 0x00,        #   Unit (None)

    # --- Buttons 1-14, plus 4 bits of padding to byte-align ---
    0x05, 0x09,        #   Usage Page (Button)
    0x19, 0x01,        #   Usage Minimum (Button 1)
    0x29, 0x0E,        #   Usage Maximum (Button 14)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x0E,        #   Report Count (14)
    0x81, 0x02,        #   Input (Data, Var, Abs)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x06,        #   Report Count (6)  -- padding
    0x81, 0x03,        #   Input (Const, Var, Abs)

    0xC0,              # End Collection
])
# fmt: on

#: Button order in the report. Index == HID button number - 1.
#: Chosen to match the layout Windows and Android expect from an Xbox-style
#: pad, so games with hardcoded button numbers behave sensibly.
_BUTTON_ORDER: tuple[int, ...] = (
    Button.A,
    Button.B,
    Button.X,
    Button.Y,
    Button.LEFT_BUMPER,
    Button.RIGHT_BUMPER,
    Button.LEFT_TRIGGER,
    Button.RIGHT_TRIGGER,
    Button.BACK,
    Button.START,
    Button.LEFT_STICK,
    Button.RIGHT_STICK,
    Button.GUIDE,
    Button.CAPTURE,
)

#: D-pad bitmask -> hat value. 8 means centered.
#: Built once at import; the hot path is a dict lookup, not a branch tree.
_HAT_CENTERED = 8
_DPAD_MASK = Button.DPAD_UP | Button.DPAD_DOWN | Button.DPAD_LEFT | Button.DPAD_RIGHT
_HAT_TABLE: dict[int, int] = {
    0: _HAT_CENTERED,
    Button.DPAD_UP: 0,
    Button.DPAD_UP | Button.DPAD_RIGHT: 1,
    Button.DPAD_RIGHT: 2,
    Button.DPAD_DOWN | Button.DPAD_RIGHT: 3,
    Button.DPAD_DOWN: 4,
    Button.DPAD_DOWN | Button.DPAD_LEFT: 5,
    Button.DPAD_LEFT: 6,
    Button.DPAD_UP | Button.DPAD_LEFT: 7,
    # Opposing pairs cancel to centered rather than picking a direction --
    # some pads can report both when passing through the diagonal.
    Button.DPAD_UP | Button.DPAD_DOWN: _HAT_CENTERED,
    Button.DPAD_LEFT | Button.DPAD_RIGHT: _HAT_CENTERED,
}

_REPORT_STRUCT = struct.Struct("<hhhhBB")

#: 1 report-ID byte, then 10 bytes of axes/triggers, then 3 bytes holding hat
#: (4 bits) + buttons (14 bits) + padding (6 bits). Must match the report
#: descriptor exactly or the host misparses every field after the mismatch --
#: and if the report ID itself is wrong, the host drops the report entirely.
REPORT_SIZE = 1 + _REPORT_STRUCT.size + 3  # 14


class GenericGamepadProfile(TargetProfile):
    """Standard Bluetooth HID gamepad."""

    def __init__(self, device_name: str = "RBGC Gamepad") -> None:
        self._descriptor = ProfileDescriptor(
            device_name=device_name,
            device_class=0x002508,  # peripheral, gamepad
            report_descriptor=_REPORT_DESCRIPTOR,
            # Generic/unregistered VID:PID. Deliberately not impersonating a
            # real vendor -- some hosts apply vendor-specific quirks that would
            # break a device that does not actually behave like that hardware.
            vendor_id=0x1D6B,   # Linux Foundation
            product_id=0x0246,
            input_report_id=REPORT_ID,
        )

    @property
    def name(self) -> str:
        return "generic"

    @property
    def display_name(self) -> str:
        return "Generic Bluetooth Gamepad"

    @property
    def descriptor(self) -> ProfileDescriptor:
        return self._descriptor

    def build_input_report(self, state: ControllerState, buf: bytearray) -> int:
        """Pack state into a HID input report. Allocation-free.

        Byte 0 is the report ID. The descriptor declares ``Report ID (1)``, so
        the host parses the first byte of every report as that ID -- omitting it
        makes the host read an axis byte as the ID, fail to match, and silently
        discard the whole report. (Verified against Windows on real hardware:
        reports were delivered over L2CAP and dropped by the host.)
        """
        buf[0] = REPORT_ID

        _REPORT_STRUCT.pack_into(
            buf,
            1,
            state.left_x,
            state.left_y,
            state.right_x,
            state.right_y,
            state.left_trigger,
            state.right_trigger,
        )

        buttons = state.buttons
        hat = _HAT_TABLE.get(buttons & _DPAD_MASK, _HAT_CENTERED)

        bits = 0
        for index, button in enumerate(_BUTTON_ORDER):
            if buttons & button:
                bits |= 1 << index

        # Byte 11: hat in the low nibble, buttons 1-4 in the high nibble.
        buf[11] = (hat & 0x0F) | ((bits & 0x0F) << 4)
        # Bytes 12-13: buttons 5-14.
        buf[12] = (bits >> 4) & 0xFF
        buf[13] = (bits >> 12) & 0x03

        return REPORT_SIZE

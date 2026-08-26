"""The 8BitDo 64 Bluetooth Controller, reproduced from the real device.

Every field here was read off a physical pad rather than guessed, which matters
because the console this exists for does not explain itself: an Analogue 3D
connects to a controller it recognises and silently ignores one it does not.

Where the numbers came from
---------------------------
The Pi was bonded to a real pad over BLE and its GATT database read directly.
The **report descriptor below is the pad's own 114 bytes, verbatim** -- not a
reconstruction that parses the same way. An earlier version here was rebuilt
from the capabilities Windows reported, which was equivalent to a parser and
six bytes longer; a console that filters on what a controller claims to be is
exactly the audience this profile exists for, so being byte-identical costs
nothing and removes a whole class of doubt.

The button numbering came from a live capture: each control pressed in turn
while HOGP notifications were logged. That capture is also what settled the
trigger order, which the earlier idle-only capture could not see because both
triggers read zero.

The idle report, as the pad sends it over HOGP (10 bytes, no report id)::

    00 00 f0 7f 7f 7f 7f 00 00 60

    bytes 0-1   buttons 1-16
    byte 2      buttons 17-18 (bits 0-1), 2 unused bits, hat (bits 4-7)
    bytes 3-6   X, Y, Z, Rz               8-bit, centre 0x7f
    byte 7      Accelerator               Simulation page -- the RIGHT trigger
    byte 8      Brake                     Simulation page -- the LEFT trigger
    byte 9      battery                   Generic Device page, 0..100

Add one to every index for the Classic layout, where byte 0 is the report id.

The hat sits in the **high** nibble: an idle report reads ``0xf0``, not ``0x0f``.
Null is 0xf rather than the 8 the generic profile uses.

Two things the live capture overturned
--------------------------------------
**The triggers are the other way round.** The descriptor declares
``Accelerator (0xC4)`` before ``Brake (0xC5)``, so the first of the two bytes
is the *right* trigger. This profile had them swapped, which is invisible in an
idle capture because both read zero, and invisible in testing unless somebody
pulls one trigger and watches the other move.

**The C buttons are not buttons.** Pressing C-up drives ``Rz`` to 0x00, C-down
to 0xff, C-left drives ``Z`` to 0x00 and C-right to 0xff -- the C cluster is
wired to the right stick axes, exactly as a real N64 C-pad behaves under an
adapter. Sending them as button bits would leave a console watching the axes
seeing nothing at all, with every counter on our side reporting success.

Measured button numbers
-----------------------
=====  ==================  ================================
HID #  control             notes
=====  ==================  ================================
1      A
2      B
7      L
8      R
9      Z (left)            also drives Brake to 0xff
10     Z (right)           also drives Accelerator to 0xff
11     Home                no logical button maps here
12     Start
18     Star / screenshot   no logical button maps here
=====  ==================  ================================

Buttons 3-6 and 13-17 were never observed: the pad has no control that sets
them. They are left unmapped rather than filled in with the conventional
DirectInput ordering, because a console reading a button the pad cannot
physically send is a worse failure than a control we simply do not offer.

Home and Star have no bit here because the client's N64 layout spends
``GUIDE``, ``BACK``, ``CAPTURE`` and ``RIGHT_STICK`` on the C cluster. That is
the right trade for this profile's one job -- an N64 pad has no Select, and
losing Home costs less than losing C.

The output report (id 0x05, four magnitude bytes on the Physical Interface
Device page) is declared so rumble has somewhere to arrive. It is not yet
decoded into a RumbleCommand.
"""

from __future__ import annotations

from common.state import Button, ControllerState
from server.bt.profiles.base import ProfileDescriptor, RumbleCommand, TargetProfile

REPORT_ID = 0x01
OUTPUT_REPORT_ID = 0x05

#: 1 report-id byte plus the 10-byte body measured off the real pad.
REPORT_SIZE = 11

#: Axis centre. The pad idles at 0x7f on all four, not 0x80.
AXIS_CENTRE = 0x7F

#: Hat value meaning "not pressed". The real pad sends 0xf, where the generic
#: profile uses 8 -- both are legal null values for a 0..7 hat, and a host that
#: compares against the descriptor's null state accepts either, but matching
#: the device costs nothing.
HAT_NULL = 0x0F

#: Battery percentage reported to the host. A real pad sends its actual charge
#: (the one measured read 0x60, 96%); we are mains-powered, so a full battery
#: is the honest answer for a device that cannot go flat.
BATTERY_LEVEL = 100

# fmt: off
#: **The real pad's descriptor, byte for byte.** Read from its Report Map
#: characteristic (0x2a4b) over an encrypted LE link. Do not "tidy" this into
#: an equivalent form -- equivalence is a property of parsers, and the reason
#: this profile exists is a console that may not be parsing.
_REPORT_DESCRIPTOR = bytes([
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x05,        # Usage (Game Pad)
    0xA1, 0x01,        # Collection (Application)
    0x85, REPORT_ID,   #   Report ID (1)

    # --- 18 button usages spread over 20 bits ---
    # The count is 20 against a usage maximum of 18: the pad declares the two
    # spare bits inside the same item rather than as a separate padding item.
    # Same bytes on the wire, different descriptor.
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x35, 0x00,        #   Physical Minimum (0)
    0x45, 0x01,        #   Physical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x14,        #   Report Count (20)
    0x05, 0x09,        #   Usage Page (Button)
    0x19, 0x01,        #   Usage Minimum (Button 1)
    0x29, 0x12,        #   Usage Maximum (Button 18)
    0x81, 0x02,        #   Input (Data, Var, Abs)

    # --- D-pad as a hat, in the high nibble of the same byte ---
    0x05, 0x01,        #   Usage Page (Generic Desktop)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x07,        #   Logical Maximum (7)
    0x46, 0x3B, 0x01,  #   Physical Maximum (315 degrees)
    0x75, 0x04,        #   Report Size (4)
    0x95, 0x01,        #   Report Count (1)
    0x65, 0x14,        #   Unit (English Rotation: degrees)
    0x09, 0x39,        #   Usage (Hat switch)
    0x81, 0x42,        #   Input (Data, Var, Abs, Null State)

    # --- Both sticks: four 8-bit axes, unsigned, centred at 0x7f ---
    # Z and Rz are the right stick, which is also where the C cluster lands.
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x00,  #   Logical Maximum (255)
    0x09, 0x30,        #   Usage (X)
    0x09, 0x31,        #   Usage (Y)
    0x09, 0x32,        #   Usage (Z)
    0x09, 0x35,        #   Usage (Rz)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x04,        #   Report Count (4)
    0x81, 0x02,        #   Input (Data, Var, Abs)

    # --- Triggers: Accelerator FIRST, then Brake. Order decides the bytes. ---
    0x05, 0x02,        #   Usage Page (Simulation Controls)
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x00,  #   Logical Maximum (255)
    0x09, 0xC4,        #   Usage (Accelerator)  -- right trigger
    0x09, 0xC5,        #   Usage (Brake)        -- left trigger
    0x95, 0x02,        #   Report Count (2)
    0x75, 0x08,        #   Report Size (8)
    0x81, 0x02,        #   Input (Data, Var, Abs)

    # --- Battery strength, 0-100 ---
    0x05, 0x06,        #   Usage Page (Generic Device Controls)
    0x09, 0x20,        #   Usage (Battery Strength)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x64,        #   Logical Maximum (100)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x02,        #   Input (Data, Var, Abs)

    # --- Output: four rumble magnitudes, 0-100 ---
    0x05, 0x0F,        #   Usage Page (Physical Interface Device)
    0x09, 0x70,        #   Usage (Magnitude)
    0x85, OUTPUT_REPORT_ID,  #   Report ID (5)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x64,        #   Logical Maximum (100)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x04,        #   Report Count (4)
    0x91, 0x02,        #   Output (Data, Var, Abs)

    0xC0,              # End Collection
])
# fmt: on

#: Measured button order. Index == HID button number - 1. A zero means the pad
#: has no control that sets that bit, or that no logical button maps to it --
#: see the table in the module docstring for which is which.
_BUTTON_ORDER: tuple[int, ...] = (
    Button.A, Button.B, 0, 0,
    0, 0, Button.LEFT_BUMPER, Button.RIGHT_BUMPER,
    Button.LEFT_TRIGGER, Button.RIGHT_TRIGGER, 0, Button.START,
    0, 0, 0, 0,
    0, 0,
)

#: The C cluster, as the real pad wires it: right-stick deflection, not button
#: bits. Maps a logical button to (byte offset within the report, value).
#: Offsets are into the 11-byte Classic buffer, so Z is 6 and Rz is 7.
_C_AXIS_OVERRIDES: tuple[tuple[int, int, int], ...] = (
    (Button.GUIDE, 6, 0x00),         # C-left  -> Z minimum
    (Button.BACK, 6, 0xFF),          # C-right -> Z maximum
    (Button.CAPTURE, 7, 0x00),       # C-up    -> Rz minimum
    (Button.RIGHT_STICK, 7, 0xFF),   # C-down  -> Rz maximum
)

#: D-pad bitmask -> hat value, clockwise from north. Opposing pairs cancel to
#: null rather than picking a direction: a pad passing through a diagonal can
#: briefly report both.
_HAT_TABLE: dict[int, int] = {
    0: HAT_NULL,
    Button.DPAD_UP: 0,
    Button.DPAD_UP | Button.DPAD_RIGHT: 1,
    Button.DPAD_RIGHT: 2,
    Button.DPAD_DOWN | Button.DPAD_RIGHT: 3,
    Button.DPAD_DOWN: 4,
    Button.DPAD_DOWN | Button.DPAD_LEFT: 5,
    Button.DPAD_LEFT: 6,
    Button.DPAD_UP | Button.DPAD_LEFT: 7,
    Button.DPAD_UP | Button.DPAD_DOWN: HAT_NULL,
    Button.DPAD_LEFT | Button.DPAD_RIGHT: HAT_NULL,
}
_DPAD_MASK = Button.DPAD_UP | Button.DPAD_DOWN | Button.DPAD_LEFT | Button.DPAD_RIGHT


def _axis8(value: int) -> int:
    """Our signed 16-bit axis to the pad's unsigned 8-bit one.

    Centre maps to 0x7f, not 0x80, because that is what the real pad idles at
    and a console applying a dead zone around its own centre would read a
    permanent slight offset otherwise.
    """
    scaled = ((value + 32768) * 255) // 65535
    return 0 if scaled < 0 else 255 if scaled > 255 else scaled


class EightBitDo64Profile(TargetProfile):
    """Presents as an 8BitDo 64 Bluetooth Controller."""

    def __init__(self, battery: int = BATTERY_LEVEL) -> None:
        self._battery = max(0, min(100, battery))
        self._descriptor = ProfileDescriptor(
            device_name="8BitDo 64 BT",
            device_class=0x002508,
            report_descriptor=_REPORT_DESCRIPTOR,
            # Read off the real pad's PnP ID: 02 c8 2d 19 30 01 00.
            vendor_id=0x2DC8,
            product_id=0x3019,
            version=0x0001,
            input_report_id=REPORT_ID,
        )

    @property
    def name(self) -> str:
        return "8bitdo_64"

    @property
    def display_name(self) -> str:
        return "8BitDo 64 (Analogue 3D)"

    @property
    def descriptor(self) -> ProfileDescriptor:
        return self._descriptor

    def build_input_report(self, state: ControllerState, buf: bytearray) -> int:
        """Pack state into the pad's 11-byte report. Allocation-free.

        Byte 0 is the report id. Over Classic that byte goes on the wire; over
        HOGP the transport strips it, because the id lives in the Report
        Reference descriptor instead. Both paths start from this same buffer --
        see server/bt/ble/hogp.py:build_ble_payload.
        """
        buttons = state.buttons

        bits = 0
        for index, button in enumerate(_BUTTON_ORDER):
            if button and (buttons & button):
                bits |= 1 << index

        hat = _HAT_TABLE.get(buttons & _DPAD_MASK, HAT_NULL)

        buf[0] = REPORT_ID
        buf[1] = bits & 0xFF
        buf[2] = (bits >> 8) & 0xFF
        # Buttons 17-18 in the low two bits, the hat in the high nibble. The
        # two bits between them are the spare part of the pad's 20-bit item.
        buf[3] = ((bits >> 16) & 0x03) | ((hat & 0x0F) << 4)

        buf[4] = _axis8(state.left_x)
        buf[5] = _axis8(state.left_y)
        buf[6] = _axis8(state.right_x)
        buf[7] = _axis8(state.right_y)

        # The C cluster rides the right stick on the real pad, so it is applied
        # over whatever the stick itself contributed. A player driving both at
        # once is asking for the same axis twice; full deflection wins, which
        # is what an adapter's C-pad does.
        for button, offset, value in _C_AXIS_OVERRIDES:
            if buttons & button:
                buf[offset] = value

        # Accelerator first, then Brake -- the order the descriptor declares,
        # which puts the RIGHT trigger in the lower byte. Getting this backwards
        # is invisible until somebody pulls one and watches the other move.
        buf[8] = state.right_trigger & 0xFF
        buf[9] = state.left_trigger & 0xFF
        buf[10] = self._battery

        return REPORT_SIZE

    def extract_rumble(self, data: bytes) -> RumbleCommand | None:
        """Decode the pad's output report: id 0x05, four magnitudes 0-100.

        Scaled to the 0-255 the rest of this codebase uses, so a profile change
        does not change how hard a controller buzzes.
        """
        payload = bytes(data)
        if payload and payload[0] == OUTPUT_REPORT_ID:
            payload = payload[1:]
        if len(payload) < 2:
            return None

        low = min(100, payload[0]) * 255 // 100
        high = min(100, payload[1]) * 255 // 100
        return RumbleCommand(low_freq=low, high_freq=high).clamped()

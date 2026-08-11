"""Nintendo Switch Pro Controller profile.

Substantially harder than generic HID: the Switch does not simply consume input
reports. It runs a subcommand handshake first -- asking for the controller's
MAC and firmware, setting the input report mode, enabling the IMU/vibration,
assigning a player LED -- and it ignores 0x30 input reports until that
completes. Sending input too early can wedge pairing entirely, which is why
:attr:`is_ready` gates the datapath.

Protocol reference: the community reverse-engineering effort documented at
https://github.com/dekuNukem/Nintendo_Switch_Reverse_Engineering, which is also
what `joycontrol` implements.

Report 0x30 layout (the "standard full" mode we use):

    0        report ID (0x30)
    1        timer          rolls over every 256 reports
    2        connection/battery nibbles
    3        buttons, right side  (Y X B A, SR SL R ZR)
    4        buttons, shared      (minus plus rstick lstick home capture)
    5        buttons, left side   (down up right left, SR SL L ZL)
    6-8      left stick,  3 bytes packing two 12-bit values
    9-11     right stick, 3 bytes packing two 12-bit values
    12       vibrator report byte
    13-49    IMU samples (zeroed -- we have no gyro to report)

The report ID must be byte 0: the host parses it first and drops any report
whose ID it does not recognise. This was found the hard way on real hardware --
reports were delivered over L2CAP and silently discarded by the host.
"""

from __future__ import annotations

import logging
import struct

from common.state import Button, ControllerState
from server.bt.profiles.base import ProfileDescriptor, RumbleCommand, TargetProfile

log = logging.getLogger(__name__)

INPUT_REPORT_ID = 0x30

#: 1 report-ID byte + the 49-byte 0x30 report body.
REPORT_SIZE = 1 + 49

#: Sticks are 12-bit unsigned, centered at 2048.
_STICK_CENTER = 2048
_STICK_MAX = 4095

# fmt: off
# The Pro Controller advertises a vendor-defined descriptor. The Switch does not
# actually parse it -- it identifies the controller by VID/PID and name -- but a
# well-formed descriptor is required for the SDP record to be accepted.
_REPORT_DESCRIPTOR = bytes([
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x05,        # Usage (Game Pad)
    0xA1, 0x01,        # Collection (Application)
    0x06, 0x01, 0xFF,  #   Usage Page (Vendor Defined 0xFF01)
    0x85, 0x21,        #   Report ID (0x21)
    0x09, 0x21,        #   Usage (0x21)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x30,        #   Report Count (48)
    0x81, 0x02,        #   Input (Data, Var, Abs)
    0x85, 0x30,        #   Report ID (0x30)
    0x09, 0x30,        #   Usage (0x30)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x30,        #   Report Count (48)
    0x81, 0x02,        #   Input (Data, Var, Abs)
    0x85, 0x10,        #   Report ID (0x10)
    0x09, 0x10,        #   Usage (0x10)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x30,        #   Report Count (48)
    0x91, 0x02,        #   Output (Data, Var, Abs)
    0x85, 0x01,        #   Report ID (0x01)
    0x09, 0x01,        #   Usage (0x01)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x30,        #   Report Count (48)
    0x91, 0x02,        #   Output (Data, Var, Abs)
    0xC0,              # End Collection
])
# fmt: on


class _ButtonBits:
    """Bit positions within the three button bytes of report 0x30."""

    # Byte 2 -- right side
    Y = 0x01
    X = 0x02
    B = 0x04
    A = 0x08
    R = 0x40
    ZR = 0x80

    # Byte 3 -- shared
    MINUS = 0x01
    PLUS = 0x02
    RSTICK = 0x04
    LSTICK = 0x08
    HOME = 0x10
    CAPTURE = 0x20

    # Byte 4 -- left side
    DOWN = 0x01
    UP = 0x02
    RIGHT = 0x04
    LEFT = 0x08
    L = 0x40
    ZL = 0x80


class SwitchProProfile(TargetProfile):
    """Emulates a Nintendo Switch Pro Controller."""

    def __init__(self, bd_addr: str = "00:00:00:00:00:00", player_number: int = 1) -> None:
        self._descriptor = ProfileDescriptor(
            device_name="Pro Controller",   # the Switch matches on this exactly
            device_class=0x002508,
            report_descriptor=_REPORT_DESCRIPTOR,
            vendor_id=0x057E,               # Nintendo
            product_id=0x2009,              # Pro Controller
            input_report_id=INPUT_REPORT_ID,
        )
        self._bd_addr = bd_addr
        self._player_number = max(1, min(4, player_number))

        self._timer = 0
        self._handshake_done = False
        self._report_mode_set = False
        self._player_led = 0

    @property
    def name(self) -> str:
        return "switch_pro"

    @property
    def display_name(self) -> str:
        return "Nintendo Switch Pro Controller"

    @property
    def descriptor(self) -> ProfileDescriptor:
        return self._descriptor

    @property
    def is_ready(self) -> bool:
        """The Switch ignores input until it has set the report mode.

        Gating on this is not an optimization -- sending 0x30 reports before the
        handshake completes can leave pairing stuck.
        """
        return self._report_mode_set

    def on_connected(self) -> None:
        self._timer = 0
        self._handshake_done = False
        self._report_mode_set = False
        log.info("Switch Pro profile connected; awaiting subcommand handshake")

    def on_disconnected(self) -> None:
        self._report_mode_set = False
        log.info("Switch Pro profile disconnected")

    def build_input_report(self, state: ControllerState, buf: bytearray) -> int:
        """Build a 0x30 standard full-state report. Allocation-free.

        Byte 0 is the report ID, matching the ``0x21`` prefix that
        :meth:`_subcommand_reply` already emits. The host parses the first byte
        of every report as the ID; omitting it makes the report unparseable and
        it is silently dropped.
        """
        # Zero the whole report: the IMU section must be present and quiet,
        # and stale bytes there make the Switch see phantom motion.
        for i in range(REPORT_SIZE):
            buf[i] = 0

        buf[0] = INPUT_REPORT_ID

        buf[1] = self._timer & 0xFF
        self._timer = (self._timer + 1) & 0xFF

        # Connection info + battery. 0x90 = full battery, wired/BT connected.
        # Reporting full avoids a low-battery warning the player cannot fix.
        buf[2] = 0x90

        buttons = state.buttons

        right = 0
        if buttons & Button.Y:
            right |= _ButtonBits.Y
        if buttons & Button.X:
            right |= _ButtonBits.X
        if buttons & Button.B:
            right |= _ButtonBits.B
        if buttons & Button.A:
            right |= _ButtonBits.A
        if buttons & Button.RIGHT_BUMPER:
            right |= _ButtonBits.R
        if buttons & Button.RIGHT_TRIGGER:
            right |= _ButtonBits.ZR
        buf[3] = right

        shared = 0
        if buttons & Button.BACK:
            shared |= _ButtonBits.MINUS
        if buttons & Button.START:
            shared |= _ButtonBits.PLUS
        if buttons & Button.RIGHT_STICK:
            shared |= _ButtonBits.RSTICK
        if buttons & Button.LEFT_STICK:
            shared |= _ButtonBits.LSTICK
        if buttons & Button.GUIDE:
            shared |= _ButtonBits.HOME
        if buttons & Button.CAPTURE:
            shared |= _ButtonBits.CAPTURE
        buf[4] = shared

        left = 0
        if buttons & Button.DPAD_DOWN:
            left |= _ButtonBits.DOWN
        if buttons & Button.DPAD_UP:
            left |= _ButtonBits.UP
        if buttons & Button.DPAD_RIGHT:
            left |= _ButtonBits.RIGHT
        if buttons & Button.DPAD_LEFT:
            left |= _ButtonBits.LEFT
        if buttons & Button.LEFT_BUMPER:
            left |= _ButtonBits.L
        if buttons & Button.LEFT_TRIGGER:
            left |= _ButtonBits.ZL
        buf[5] = left

        # Y is inverted relative to our convention: SDL reports +Y as down,
        # the Switch expects +Y as up.
        _pack_stick(buf, 6, _to_12bit(state.left_x), _to_12bit(-state.left_y))
        _pack_stick(buf, 9, _to_12bit(state.right_x), _to_12bit(-state.right_y))

        buf[12] = 0x00  # vibrator ack

        return REPORT_SIZE

    def extract_rumble(self, data: bytes) -> RumbleCommand | None:
        """Decode Switch HD rumble into two plain motor amplitudes.

        Reports 0x10 (rumble only) and 0x01 (rumble + subcommand) both carry an
        8-byte rumble block at offset 2: four bytes per side, encoding frequency
        and amplitude for a linear resonant actuator.

        The full encoding is a pair of logarithmic frequency/amplitude tables --
        HD rumble is genuinely richer than the two eccentric-mass motors a PC
        gamepad has. There is no exact conversion, so this extracts the
        amplitude and discards frequency, which is the part a conventional
        rumble motor can actually reproduce.

        Layout per side (from the community reverse-engineering docs):
            byte 0-1  high-band frequency + high-band amplitude
            byte 2-3  low-band frequency + low-band amplitude

        Amplitude lives in bits 1-6 of byte 1 (high band) and the low 7 bits of
        byte 3 (low band).
        """
        if len(data) < 10:
            return None
        if data[0] not in (0x10, 0x01):
            return None

        left = data[2:6]
        right = data[6:10]

        # Neutral rumble is 0x00 0x01 0x40 0x40 -- the Switch sends this
        # constantly as a keepalive, so treating it as an effect would make the
        # pad buzz permanently.
        neutral = bytes([0x00, 0x01, 0x40, 0x40])
        if left == neutral and right == neutral:
            return RumbleCommand(0, 0)

        high = max(_switch_high_amplitude(left), _switch_high_amplitude(right))
        low = max(_switch_low_amplitude(left), _switch_low_amplitude(right))

        return RumbleCommand(low_freq=low, high_freq=high).clamped()

    def on_output_report(self, data: bytes) -> bytes | None:
        """Handle the Switch's subcommand handshake.

        Off the hot path -- these arrive only during pairing and occasionally
        afterwards for rumble.
        """
        if len(data) < 2:
            return None

        report_id = data[0]

        if report_id == 0x80:
            return self._handle_pairing_request(data)
        if report_id == 0x01 and len(data) >= 11:
            return self._handle_subcommand(data)
        if report_id == 0x10:
            return None  # rumble-only; nothing to reply

        return None

    def _handle_pairing_request(self, data: bytes) -> bytes | None:
        """Reports 0x80 xx -- the initial USB-style handshake the Switch sends."""
        sub = data[1]

        if sub == 0x01:
            # Request controller MAC + type. Type 0x03 == Pro Controller.
            response = bytearray(8)
            response[0] = 0x81
            response[1] = 0x01
            response[2] = 0x00
            response[3] = 0x03
            mac = _parse_mac(self._bd_addr)
            response[4:8] = mac[:4]
            return bytes(response) + mac[4:]

        if sub in (0x02, 0x03):
            # Handshake / baud rate. Acknowledge and move on.
            self._handshake_done = True
            return bytes([0x81, sub])

        if sub in (0x04, 0x05):
            return None  # enable/disable timeout; no reply expected

        return bytes([0x81, sub])

    def _handle_subcommand(self, data: bytes) -> bytes | None:
        """Report 0x01 -- rumble + subcommand. Byte 10 is the subcommand id."""
        subcommand = data[10]
        args = data[11:]

        if subcommand == 0x02:
            # Request device info.
            return self._subcommand_reply(0x82, subcommand, self._device_info())
        if subcommand == 0x03:
            # Set input report mode. This is the gate: once the Switch has
            # asked for 0x30 mode, it is ready to consume our input.
            if args and args[0] == 0x30:
                self._report_mode_set = True
                log.info("Switch set input report mode 0x30; controller is live")
            return self._subcommand_reply(0x80, subcommand, b"")
        if subcommand == 0x08:
            # Set shipment low-power state.
            return self._subcommand_reply(0x80, subcommand, b"")
        if subcommand == 0x10:
            # Read SPI flash -- calibration data lives here.
            return self._handle_spi_read(args)
        if subcommand == 0x30:
            # Set player LEDs. Tells us which player number we were assigned.
            if args:
                self._player_led = args[0]
                log.info("Switch assigned player LED pattern 0x%02x", args[0])
            return self._subcommand_reply(0x80, subcommand, b"")
        if subcommand in (0x04, 0x40, 0x41, 0x48):
            # Trigger buttons elapsed / IMU enable / IMU sensitivity / vibration.
            # Acknowledged but not implemented: we have no motion hardware, and
            # the Switch is content with a plain ack.
            return self._subcommand_reply(0x80, subcommand, b"")

        log.debug("Unhandled Switch subcommand 0x%02x", subcommand)
        return self._subcommand_reply(0x80, subcommand, b"")

    def _subcommand_reply(self, ack: int, subcommand: int, payload: bytes) -> bytes:
        """Build a 0x21 subcommand-reply report.

        Byte 0 is the report ID, matching :meth:`build_input_report`. Then the
        standard input-report prefix, the ack byte, the subcommand id, and the
        subcommand-specific payload.
        """
        buf = bytearray(REPORT_SIZE)
        buf[0] = 0x21

        buf[1] = self._timer & 0xFF
        self._timer = (self._timer + 1) & 0xFF
        buf[2] = 0x90

        # Neutral sticks in the prefix so a reply mid-handshake does not look
        # like stick movement.
        _pack_stick(buf, 6, _STICK_CENTER, _STICK_CENTER)
        _pack_stick(buf, 9, _STICK_CENTER, _STICK_CENTER)

        buf[13] = ack
        buf[14] = subcommand
        end = min(15 + len(payload), REPORT_SIZE)
        buf[15:end] = payload[: end - 15]

        return bytes(buf)

    def _device_info(self) -> bytes:
        """Subcommand 0x02 payload: firmware version, type, MAC."""
        mac = _parse_mac(self._bd_addr)
        return bytes(
            [
                0x03, 0x48,        # firmware 3.72
                0x03,              # Pro Controller
                0x02,
                *reversed(mac),    # MAC, little-endian
                0x01,
                0x02,              # colors read from SPI
            ]
        )

    def _handle_spi_read(self, args: bytes) -> bytes:
        """Subcommand 0x10: read SPI flash.

        The Switch reads stick calibration and colour data from here. We return
        factory-default calibration, which is what an uncalibrated real
        controller would report -- the Switch accepts it and the sticks behave.
        """
        if len(args) < 5:
            return self._subcommand_reply(0x80, 0x10, b"")

        address = int.from_bytes(args[0:4], "little")
        length = args[4]

        data = _SPI_DEFAULTS.get(address)
        payload = data[:length] if data else bytes(length)

        return self._subcommand_reply(0x90, 0x10, args[:5] + payload.ljust(length, b"\xff"))


#: Factory-default SPI flash contents the Switch reads during setup.
#: Values are the standard neutral calibration a real Pro Controller ships with.
_SPI_DEFAULTS: dict[int, bytes] = {
    # Left/right stick factory calibration.
    0x603D: bytes(
        [
            0x00, 0x08, 0x80, 0x00, 0x08, 0x80, 0x00, 0x08, 0x80,   # left
            0x00, 0x08, 0x80, 0x00, 0x08, 0x80, 0x00, 0x08, 0x80,   # right
        ]
    ),
    # Controller colours: body / buttons / left grip / right grip.
    0x6050: bytes([0x32, 0x32, 0x32, 0xFF, 0xFF, 0xFF, 0x32, 0x32, 0x32, 0x32, 0x32, 0x32]),
    # User stick calibration -- 0xFF means "absent", so factory values are used.
    0x8010: bytes([0xFF] * 24),
    # Six-axis (IMU) factory calibration. Zeroed: we report no motion.
    0x6020: bytes(24),
}


def _to_12bit(value: int) -> int:
    """Map an int16 axis onto the Switch's 12-bit unsigned range."""
    scaled = _STICK_CENTER + ((value * _STICK_CENTER) // 32768)
    if scaled < 0:
        return 0
    if scaled > _STICK_MAX:
        return _STICK_MAX
    return scaled


def _pack_stick(buf: bytearray, offset: int, x: int, y: int) -> None:
    """Pack two 12-bit values into three bytes, little-endian nibble order."""
    buf[offset] = x & 0xFF
    buf[offset + 1] = ((x >> 8) & 0x0F) | ((y & 0x0F) << 4)
    buf[offset + 2] = (y >> 4) & 0xFF


def _switch_high_amplitude(side: bytes) -> int:
    """High-band amplitude from a 4-byte rumble block, scaled to 0-255.

    Bits 1-6 of byte 1 hold a 0-0x64 amplitude index. Approximated linearly:
    the real table is logarithmic, but the result drives a simple motor that
    cannot reproduce the curve anyway.
    """
    if len(side) < 2:
        return 0
    amplitude = (side[1] & 0xFE) >> 1
    return min(255, int(amplitude * 255 / 0x64)) if amplitude else 0


def _switch_low_amplitude(side: bytes) -> int:
    """Low-band amplitude from a 4-byte rumble block, scaled to 0-255.

    Byte 3's low 7 bits hold the amplitude, offset by 0x40 -- values at or
    below the 0x40 baseline mean silence.
    """
    if len(side) < 4:
        return 0
    raw = side[3] & 0x7F
    if raw <= 0x40:
        return 0
    return min(255, int((raw - 0x40) * 255 / 0x32))


def _parse_mac(bd_addr: str) -> bytes:
    """'AA:BB:CC:DD:EE:FF' -> 6 bytes, big-endian as written."""
    try:
        return bytes(int(part, 16) for part in bd_addr.split(":"))
    except (ValueError, AttributeError):
        return bytes(6)

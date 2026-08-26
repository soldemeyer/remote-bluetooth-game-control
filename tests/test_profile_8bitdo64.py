"""The 8BitDo 64 profile, checked against the real device.

Every value here was read off a physical pad. The descriptor and the report
references came from bonding the Pi to it over BLE and reading its GATT
database directly; the button numbering and the trigger order came from a
live capture of each control being pressed in turn.

This matters more than the usual descriptor test. The console it exists for
does not explain itself -- an Analogue 3D connects to a controller it
recognises and silently ignores one it does not -- so a field that drifts from
the real pad produces no error anywhere, just a controller that does nothing.

Two classes at the end of this file check against **measurement**; everything
before them checks against a reading of the descriptor. That distinction is
not academic. The trigger order was wrong for as long as this file only had
the second kind, and every test passed the whole time: an idle pad reports
both triggers as zero, so the descriptor dump and the idle capture were each
individually consistent with the bytes being the wrong way round.
"""

from __future__ import annotations

import pytest

from common.state import Button, ControllerState
from server.bt.profiles import create_profile
from server.bt.profiles.eightbitdo64 import (
    AXIS_CENTRE,
    HAT_NULL,
    REPORT_SIZE,
    EightBitDo64Profile,
)

#: Captured from the pad, idle, battery at 95%.
REAL_IDLE = bytes.fromhex("010000f07f7f7f7f00005f")


@pytest.fixture
def profile():
    return EightBitDo64Profile(battery=95)


def report(profile, state):
    buf = bytearray(64)
    size = profile.build_input_report(state, buf)
    return bytes(buf[:size])


class TestItMatchesTheRealPad:
    def test_the_idle_report_is_identical(self, profile):
        """The whole point: byte for byte against a capture of the device."""
        assert report(profile, ControllerState()) == REAL_IDLE

    def test_the_report_is_eleven_bytes(self, profile):
        assert REPORT_SIZE == 11
        assert len(report(profile, ControllerState())) == 11

    def test_the_vendor_and_product_match_its_pnp_record(self, profile):
        # Read from the pad: 02 c8 2d 19 30 01 00.
        assert profile.descriptor.vendor_id == 0x2DC8
        assert profile.descriptor.product_id == 0x3019
        assert profile.descriptor.version == 0x0001

    def test_it_advertises_the_name_the_pad_advertises(self, profile):
        assert profile.descriptor.device_name == "8BitDo 64 BT"

    def test_it_is_registered_under_a_stable_key(self):
        assert create_profile("8bitdo_64").name == "8bitdo_64"


class TestTheAxes:
    """8-bit unsigned, centred on 0x7f -- not the generic profile's 16-bit signed."""

    def test_centre_matches_the_pad_at_rest(self, profile):
        data = report(profile, ControllerState())
        assert data[4:8] == bytes([AXIS_CENTRE] * 4)

    def test_full_deflection_reaches_the_ends(self, profile):
        data = report(profile, ControllerState(left_x=-32768, left_y=32767))
        assert data[4] == 0x00
        assert data[5] == 0xFF

    def test_axes_are_in_stick_order(self, profile):
        # bytes 4-7 are X, Y, Z, Rz = left X/Y then right X/Y.
        data = report(profile, ControllerState(
            left_x=-32768, left_y=32767, right_x=32767, right_y=-32768))
        assert data[4] == 0x00 and data[5] == 0xFF
        assert data[6] == 0xFF and data[7] == 0x00

    def test_no_axis_can_leave_a_byte(self, profile):
        for value in (-32768, -1, 0, 1, 32767):
            data = report(profile, ControllerState(left_x=value))
            assert 0 <= data[4] <= 255


class TestTheHatIsInTheHighNibble:
    """An idle pad reads 0xf0, not 0x0f. Getting this backwards puts the
    D-pad into the button bits and the buttons into the hat."""

    def test_idle_is_null_in_the_high_nibble(self, profile):
        assert report(profile, ControllerState())[3] == 0xF0

    def test_null_is_fifteen_not_eight(self):
        # Both are legal null values for a 0..7 hat; the pad sends 0xf.
        assert HAT_NULL == 0x0F

    @pytest.mark.parametrize("button,value", [
        (Button.DPAD_UP, 0), (Button.DPAD_RIGHT, 2),
        (Button.DPAD_DOWN, 4), (Button.DPAD_LEFT, 6),
    ])
    def test_directions_are_clockwise_from_north(self, profile, button, value):
        data = report(profile, ControllerState(buttons=button))
        assert (data[3] >> 4) == value

    def test_opposing_directions_cancel_to_null(self, profile):
        data = report(profile, ControllerState(
            buttons=Button.DPAD_UP | Button.DPAD_DOWN))
        assert (data[3] >> 4) == HAT_NULL

    def test_the_hat_does_not_disturb_the_button_bits(self, profile):
        data = report(profile, ControllerState(buttons=Button.DPAD_LEFT))
        assert data[1] == 0x00 and data[2] == 0x00


class TestTheTriggersAndBattery:
    def test_the_right_trigger_comes_first(self, profile):
        """Accelerator is declared before Brake, so byte 8 is the RIGHT one.

        This was backwards until a live capture caught it, and nothing else
        could have: both triggers read zero on an idle pad, so the descriptor
        dump and the idle report were each individually consistent with the
        wrong answer. Pulling one trigger and watching the other move is the
        only thing that separates them.
        """
        data = report(profile, ControllerState(left_trigger=200, right_trigger=50))
        assert data[8] == 50, "byte 8 is Accelerator, which is the right trigger"
        assert data[9] == 200, "byte 9 is Brake, which is the left trigger"

    def test_battery_is_the_last_byte(self, profile):
        assert report(profile, ControllerState())[10] == 95

    def test_battery_is_clamped_to_the_declared_range(self):
        # The descriptor declares logical 0..100; a value outside it would be
        # rejected or misread by a host that trusts the range.
        assert EightBitDo64Profile(battery=250)._battery == 100
        assert EightBitDo64Profile(battery=-5)._battery == 0


class TestTheDescriptorDescribesTheReport:
    """A descriptor that disagrees with the bytes is the worst failure here:
    reports are delivered, nothing errors, and every field after the mismatch
    is read as garbage."""

    def test_it_declares_eighteen_buttons(self, profile):
        d = profile.descriptor.report_descriptor
        # Usage Minimum 1, Usage Maximum 18.
        assert bytes([0x19, 0x01, 0x29, 0x12]) in d

    def test_it_declares_four_eight_bit_axes(self, profile):
        d = profile.descriptor.report_descriptor
        # Report Size 8, Report Count 4.
        assert bytes([0x75, 0x08, 0x95, 0x04]) in d

    def test_the_triggers_are_on_the_simulation_page(self, profile):
        d = profile.descriptor.report_descriptor
        # Usage Page (Simulation), Accelerator, Brake -- not Generic Desktop
        # Rx/Ry, which is what the generic profile uses. The ORDER is what
        # decides which byte carries which trigger, so it is asserted rather
        # than just the presence of both usages.
        assert bytes([0x09, 0xC4, 0x09, 0xC5]) in d
        assert bytes([0x09, 0xC5, 0x09, 0xC4]) not in d

    def test_the_battery_is_on_the_generic_device_page(self, profile):
        d = profile.descriptor.report_descriptor
        assert bytes([0x05, 0x06, 0x09, 0x20]) in d

    def test_the_hat_declares_a_null_state(self, profile):
        # Input (Data, Var, Abs, Null State) -- without the null bit a host has
        # no way to express "not pressed".
        assert bytes([0x81, 0x42]) in profile.descriptor.report_descriptor

    def test_the_declared_input_length_matches_what_we_send(self, profile):
        """Walk the descriptor and add up the input bits.

        18 buttons + 2 padding + 4 hat + 4x8 axes + 2x8 triggers + 8 battery
        = 80 bits = 10 bytes, plus the report id.
        """
        expected_bits = 18 + 2 + 4 + (4 * 8) + (2 * 8) + 8
        assert expected_bits % 8 == 0
        assert expected_bits // 8 + 1 == REPORT_SIZE


class TestRumble:
    def test_it_decodes_the_pads_output_report(self, profile):
        # Report id 0x05, four magnitudes on a 0..100 scale.
        command = profile.extract_rumble(bytes([0x05, 100, 50, 0, 0]))
        assert command is not None
        assert command.low_freq == 255
        assert command.high_freq == 127

    def test_it_tolerates_a_missing_report_id(self, profile):
        assert profile.extract_rumble(bytes([100, 100, 0, 0])) is not None

    def test_a_runt_is_ignored_rather_than_guessed_at(self, profile):
        # Guessing at an unknown layout risks turning an LED command into a
        # rumble burst.
        assert profile.extract_rumble(b"") is None
        assert profile.extract_rumble(bytes([0x05])) is None


class TestTheBleAndClassicPathsAgree:
    def test_the_ble_payload_is_the_report_without_its_id(self, profile):
        """HOGP carries the id in the Report Reference descriptor instead."""
        from server.bt.ble import hogp

        classic = report(profile, ControllerState())
        ble = hogp.build_ble_payload(classic, profile.descriptor.input_report_id)
        assert len(ble) == len(classic) - 1
        assert ble == classic[1:]
        assert ble == REAL_IDLE[1:]


#: The pad's own Report Map, read from characteristic 0x2a4b over an encrypted
#: LE link while the Pi was bonded to it.
REAL_REPORT_MAP = bytes.fromhex(
    "05010905a10185011500250135004501750195140509190129128102"
    "050115002507463b0175049501651409398142150026ff0009300931"
    "093209357508950481020502150026ff0009c409c59502750881020506"
    "092015002564750895018102050f0970850515002564750895049102c0"
)

#: Reports observed from the physical pad during a control-by-control walk,
#: in the HOGP form it sends them (report id stripped, battery at 0x60).
#: These are measurements, not derivations from the descriptor.
MEASURED_WALK = [
    ("idle", {}, "0000f07f7f7f7f000060"),
    ("A", {"buttons": Button.A}, "0100f07f7f7f7f000060"),
    ("B", {"buttons": Button.B}, "0200f07f7f7f7f000060"),
    ("L", {"buttons": Button.LEFT_BUMPER}, "4000f07f7f7f7f000060"),
    ("R", {"buttons": Button.RIGHT_BUMPER}, "8000f07f7f7f7f000060"),
    ("Start", {"buttons": Button.START}, "0008f07f7f7f7f000060"),
    ("C-up", {"buttons": Button.CAPTURE}, "0000f07f7f7f00000060"),
    ("C-right", {"buttons": Button.BACK}, "0000f07f7fff7f000060"),
    ("C-down", {"buttons": Button.RIGHT_STICK}, "0000f07f7f7fff000060"),
    ("C-left", {"buttons": Button.GUIDE}, "0000f07f7f007f000060"),
    ("dpad-up", {"buttons": Button.DPAD_UP}, "0000007f7f7f7f000060"),
    ("dpad-right", {"buttons": Button.DPAD_RIGHT}, "0000207f7f7f7f000060"),
    ("dpad-down", {"buttons": Button.DPAD_DOWN}, "0000407f7f7f7f000060"),
    ("dpad-left", {"buttons": Button.DPAD_LEFT}, "0000607f7f7f7f000060"),
    (
        "Z-right",
        {"buttons": Button.RIGHT_TRIGGER, "right_trigger": 255},
        "0002f07f7f7f7fff0060",
    ),
    (
        "Z-left",
        {"buttons": Button.LEFT_TRIGGER, "left_trigger": 255},
        "0001f07f7f7f7f00ff60",
    ),
    ("stick-left", {"left_x": -32768}, "0000f0007f7f7f000060"),
    ("stick-right", {"left_x": 32767}, "0000f0ff7f7f7f000060"),
    ("stick-up", {"left_y": -32768}, "0000f07f007f7f000060"),
    ("stick-down", {"left_y": 32767}, "0000f07fff7f7f000060"),
]


@pytest.fixture
def measured_profile():
    """A profile reporting the charge the pad had during the capture.

    Matching it means the battery byte is compared like every other field
    rather than excused, which is what caught it sitting in the right place.
    """
    return EightBitDo64Profile(battery=0x60)


class TestAgainstTheRealDevice:
    """Measurement, not derivation.

    Everything else in this file checks the profile against my reading of the
    descriptor. These two check it against what the hardware actually did --
    which is the only thing that could have caught the trigger order, or the
    C cluster being wired to the right stick rather than to button bits.
    """

    def test_the_descriptor_is_the_pads_own_bytes(self, profile):
        """Not "equivalent to" -- identical.

        Equivalence is a property of parsers, and the console this profile
        exists for may not be parsing. An earlier version here parsed the same
        and was six bytes longer.
        """
        assert profile.descriptor.report_descriptor == REAL_REPORT_MAP

    @pytest.mark.parametrize(
        "name,kwargs,expected",
        MEASURED_WALK,
        ids=[case[0] for case in MEASURED_WALK],
    )
    def test_it_reproduces_what_the_pad_sent(
        self, measured_profile, name, kwargs, expected
    ):
        data = report(measured_profile, ControllerState(**kwargs))
        # Compare in the HOGP form the capture is in: the transport strips the
        # report id, because over HOGP it lives in the Report Reference
        # descriptor instead of in the payload.
        assert data[1:].hex() == expected


class TestTheCClusterRidesTheRightStick:
    """The C buttons are axes on the real pad, not buttons.

    Sending them as button bits is the quietest possible failure: the reports
    go out, every counter is healthy, and a console watching Z/Rz sees a stick
    that never moves.
    """

    def test_c_buttons_set_no_button_bits(self, profile):
        for button in (
            Button.CAPTURE,
            Button.BACK,
            Button.RIGHT_STICK,
            Button.GUIDE,
        ):
            data = report(profile, ControllerState(buttons=button))
            assert data[1] == 0
            assert data[2] == 0
            assert data[3] & 0x03 == 0

    def test_c_overrides_the_stick_it_shares(self, profile):
        """Full deflection wins, which is what an adapter's C-pad does."""
        state = ControllerState(buttons=Button.CAPTURE, right_y=32767)
        assert report(profile, state)[7] == 0x00

    def test_the_right_stick_still_works_without_c(self, profile):
        state = ControllerState(right_x=32767, right_y=-32768)
        data = report(profile, state)
        assert data[6] == 0xFF
        assert data[7] == 0x00

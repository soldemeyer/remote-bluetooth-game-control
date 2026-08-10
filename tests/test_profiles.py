"""HID report generation for each target profile.

These check the exact bytes a console will receive. Getting a bit position
wrong here produces a controller that "works" but has the wrong buttons, which
is much harder to debug from the console side than from a test.
"""

from __future__ import annotations

import pytest

from common.state import Button, ControllerState
from server.bt.profiles import PROFILES, available_profiles, create_profile
from server.bt.profiles.generic_gamepad import REPORT_ID, REPORT_SIZE as GENERIC_SIZE
from server.bt.profiles.switch_pro import (
    INPUT_REPORT_ID,
    REPORT_SIZE as SWITCH_SIZE,
)


def test_registry_has_the_supported_targets():
    assert "generic" in PROFILES
    assert "switch_pro" in PROFILES


def test_create_unknown_profile_is_a_clear_error():
    with pytest.raises(ValueError, match="Unknown profile"):
        create_profile("xbox_one")


def test_available_profiles_shape():
    for entry in available_profiles():
        assert entry["name"] and entry["display_name"]


class TestGenericGamepad:
    @pytest.fixture
    def profile(self):
        return create_profile("generic")

    @pytest.fixture
    def buf(self):
        return bytearray(64)

    def test_report_starts_with_the_report_id(self, profile, buf):
        """The descriptor declares Report ID (1), so byte 0 must be that ID.

        Regression: this was missing, and Windows silently discarded every
        report -- they arrived over L2CAP and were dropped by the host because
        the first byte parsed as an unknown report ID.
        """
        profile.build_input_report(ControllerState(), buf)
        assert buf[0] == REPORT_ID

    def test_report_size_matches_descriptor(self, profile, buf):
        """1 report-ID byte + 10 bytes axes/triggers + 3 bytes hat/buttons."""
        assert profile.build_input_report(ControllerState(), buf) == GENERIC_SIZE
        assert GENERIC_SIZE == 14

    def test_neutral_state(self, profile, buf):
        profile.build_input_report(ControllerState(), buf)
        assert buf[1:11] == bytearray(10)
        assert buf[11] & 0x0F == 8       # hat centered
        assert buf[11] >> 4 == 0         # no buttons
        assert buf[12] == 0 and buf[13] == 0

    def test_axes_are_little_endian_int16(self, profile, buf):
        state = ControllerState(left_x=-32768, left_y=32767, right_x=256, right_y=-256)
        profile.build_input_report(state, buf)

        assert int.from_bytes(buf[1:3], "little", signed=True) == -32768
        assert int.from_bytes(buf[3:5], "little", signed=True) == 32767
        assert int.from_bytes(buf[5:7], "little", signed=True) == 256
        assert int.from_bytes(buf[7:9], "little", signed=True) == -256

    def test_triggers(self, profile, buf):
        profile.build_input_report(ControllerState(left_trigger=200, right_trigger=17), buf)
        assert buf[9] == 200
        assert buf[10] == 17

    @pytest.mark.parametrize(
        "button,byte_index,bit",
        [
            (Button.A, 11, 4),                # button 1 -> high nibble of byte 11
            (Button.B, 11, 5),
            (Button.X, 11, 6),
            (Button.Y, 11, 7),
            (Button.LEFT_BUMPER, 12, 0),      # button 5 -> byte 12 bit 0
            (Button.BACK, 12, 4),             # button 9
            (Button.GUIDE, 13, 0),            # button 13
            (Button.CAPTURE, 13, 1),          # button 14
        ],
    )
    def test_button_bit_positions(self, profile, buf, button, byte_index, bit):
        profile.build_input_report(ControllerState(buttons=button), buf)
        assert buf[byte_index] & (1 << bit), f"{button.name} landed in the wrong bit"

    @pytest.mark.parametrize(
        "dpad,expected",
        [
            (Button.NONE, 8),
            (Button.DPAD_UP, 0),
            (Button.DPAD_UP | Button.DPAD_RIGHT, 1),
            (Button.DPAD_RIGHT, 2),
            (Button.DPAD_DOWN, 4),
            (Button.DPAD_LEFT, 6),
            (Button.DPAD_UP | Button.DPAD_LEFT, 7),
        ],
    )
    def test_hat_values(self, profile, buf, dpad, expected):
        profile.build_input_report(ControllerState(buttons=dpad), buf)
        assert buf[11] & 0x0F == expected

    def test_opposing_dpad_directions_center(self, profile, buf):
        """Some pads report both when passing through a diagonal. Picking one
        arbitrarily would make the character stutter."""
        profile.build_input_report(
            ControllerState(buttons=Button.DPAD_UP | Button.DPAD_DOWN), buf
        )
        assert buf[11] & 0x0F == 8

    def test_is_ready_immediately(self, profile):
        """Generic HID needs no handshake, unlike the Switch."""
        assert profile.is_ready

    def test_descriptor_identifies_as_a_gamepad(self, profile):
        descriptor = profile.descriptor
        assert descriptor.device_class == 0x002508
        assert descriptor.report_descriptor[:4] == bytes([0x05, 0x01, 0x09, 0x05])


class TestSwitchPro:
    @pytest.fixture
    def profile(self):
        return create_profile("switch_pro", bd_addr="AA:BB:CC:DD:EE:FF")

    @pytest.fixture
    def buf(self):
        return bytearray(128)

    def test_report_starts_with_the_report_id(self, profile, buf):
        """Same regression as the generic profile: byte 0 must be the ID."""
        profile.build_input_report(ControllerState(), buf)
        assert buf[0] == INPUT_REPORT_ID

    def test_report_size(self, profile, buf):
        assert profile.build_input_report(ControllerState(), buf) == SWITCH_SIZE
        assert SWITCH_SIZE == 50

    def test_not_ready_until_report_mode_is_set(self, profile):
        """The Switch ignores input until its subcommand handshake completes,
        and sending early can wedge pairing -- so the datapath gates on this."""
        assert not profile.is_ready

        # Subcommand 0x03 = set input report mode, argument 0x30.
        request = bytes([0x01] + [0] * 9 + [0x03, 0x30])
        profile.on_output_report(request)

        assert profile.is_ready

    def test_identifies_as_a_pro_controller(self, profile):
        descriptor = profile.descriptor
        assert descriptor.device_name == "Pro Controller"  # the Switch matches exactly
        assert descriptor.vendor_id == 0x057E              # Nintendo
        assert descriptor.product_id == 0x2009

    def test_timer_advances_and_wraps(self, profile, buf):
        profile.build_input_report(ControllerState(), buf)
        first = buf[1]
        profile.build_input_report(ControllerState(), buf)
        assert buf[1] == (first + 1) & 0xFF

        for _ in range(300):
            profile.build_input_report(ControllerState(), buf)
        assert 0 <= buf[1] <= 255

    def test_battery_reports_full(self, profile, buf):
        """A low-battery warning the player cannot act on is worse than useless."""
        profile.build_input_report(ControllerState(), buf)
        assert buf[2] == 0x90

    @pytest.mark.parametrize(
        "button,byte_index,mask",
        [
            (Button.Y, 3, 0x01),
            (Button.X, 3, 0x02),
            (Button.B, 3, 0x04),
            (Button.A, 3, 0x08),
            (Button.RIGHT_BUMPER, 3, 0x40),
            (Button.RIGHT_TRIGGER, 3, 0x80),
            (Button.BACK, 4, 0x01),
            (Button.START, 4, 0x02),
            (Button.GUIDE, 4, 0x10),
            (Button.CAPTURE, 4, 0x20),
            (Button.DPAD_DOWN, 5, 0x01),
            (Button.DPAD_UP, 5, 0x02),
            (Button.LEFT_BUMPER, 5, 0x40),
            (Button.LEFT_TRIGGER, 5, 0x80),
        ],
    )
    def test_button_mapping(self, profile, buf, button, byte_index, mask):
        profile.build_input_report(ControllerState(buttons=button), buf)
        assert buf[byte_index] & mask, f"{button.name} landed in the wrong bit"

    def test_sticks_center_at_2048(self, profile, buf):
        profile.build_input_report(ControllerState(), buf)
        x, y = _unpack_stick(buf, 6)
        assert x == pytest.approx(2048, abs=2)
        assert y == pytest.approx(2048, abs=2)

    def test_stick_extremes_stay_in_12_bit_range(self, profile, buf):
        profile.build_input_report(
            ControllerState(left_x=32767, left_y=-32768, right_x=-32768, right_y=32767), buf
        )
        for offset in (6, 9):
            x, y = _unpack_stick(buf, offset)
            assert 0 <= x <= 4095
            assert 0 <= y <= 4095

    def test_y_axis_is_inverted(self, profile, buf):
        """SDL reports +Y as down; the Switch expects +Y as up."""
        profile.build_input_report(ControllerState(left_y=30000), buf)
        _, y = _unpack_stick(buf, 6)
        assert y < 2048

    def test_imu_section_is_zeroed(self, profile, buf):
        """Stale bytes here make the Switch see phantom motion."""
        buf[:] = bytearray(b"\xff" * len(buf))
        profile.build_input_report(ControllerState(), buf)
        assert buf[13:50] == bytearray(37)

    def test_device_info_subcommand_replies(self, profile):
        request = bytes([0x01] + [0] * 9 + [0x02])
        response = profile.on_output_report(request)
        assert response is not None
        assert response[0] == 0x21   # subcommand reply report

    def test_unknown_subcommand_is_acknowledged(self, profile):
        """Silence can leave the Switch waiting forever mid-pairing."""
        request = bytes([0x01] + [0] * 9 + [0x7F])
        assert profile.on_output_report(request) is not None

    def test_short_output_report_is_ignored(self, profile):
        assert profile.on_output_report(b"\x01") is None

    def test_disconnect_resets_readiness(self, profile):
        profile.on_output_report(bytes([0x01] + [0] * 9 + [0x03, 0x30]))
        assert profile.is_ready

        profile.on_disconnected()
        assert not profile.is_ready


def _unpack_stick(buf: bytearray, offset: int) -> tuple[int, int]:
    """Inverse of the 12-bit stick packing."""
    x = buf[offset] | ((buf[offset + 1] & 0x0F) << 8)
    y = ((buf[offset + 1] >> 4) & 0x0F) | (buf[offset + 2] << 4)
    return x, y


def test_report_generation_does_not_allocate_per_call():
    """The datapath calls this at up to 1 kHz per controller. A buffer that
    grows each call would be an allocation leak on the hot path."""
    profile = create_profile("generic")
    buf = bytearray(64)
    state = ControllerState(buttons=Button.A, left_x=1000)

    for _ in range(1000):
        profile.build_input_report(state, buf)

    assert len(buf) == 64

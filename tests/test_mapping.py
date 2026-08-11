"""Input mapping: physical controls to logical buttons.

The mapping layer exists because SDL's GameController database does not know
every pad. An 8BitDo 64 enumerates as a working 18-button joystick with no
mapping, and the backend used to discard exactly those devices -- which looked
to the user like the controller was undetected. These tests pin that behaviour
down along with the serialisation the config depends on.
"""

from __future__ import annotations

import pytest

from client.input.base import DeviceInfo
from client.input.mapping import (
    AXIS_PRESS_THRESHOLD,
    BINDABLE_BUTTONS,
    STICK_AXES,
    TRIGGER_AXES,
    AxisBinding,
    DeviceMapping,
    InputSource,
    KeyAxisBinding,
    SourceKind,
    button_label,
    default_joystick_mapping,
)
from common.state import Button

#: The real pad that exposed the bug: SDL sees it, SDL has no layout for it.
EIGHTBITDO_GUID = "0300f094c82d00001930000000000000"


class TestDeviceInfo:
    def test_unmapped_device_is_flagged_not_hidden(self):
        """Hiding unmapped pads is what made a working controller look absent."""
        device = DeviceInfo(
            instance_id=0, name="8BitDo 64", guid=EIGHTBITDO_GUID, is_mapped=False
        )

        assert device.status_note() == "needs mapping"

    def test_mapped_connected_device_has_no_note(self):
        device = DeviceInfo(instance_id=0, name="Xbox", guid="x", is_mapped=True)

        assert device.status_note() == ""

    def test_disconnected_beats_unmapped(self):
        device = DeviceInfo(
            instance_id=0, name="x", guid="x", is_mapped=False, is_connected=False
        )

        assert device.status_note() == "disconnected"


class TestSerialisation:
    def _mapping(self) -> DeviceMapping:
        mapping = DeviceMapping(guid=EIGHTBITDO_GUID, name="8BitDo 64")
        mapping.bind_button(Button.A, InputSource(SourceKind.BUTTON, 1))
        mapping.bind_button(Button.DPAD_UP, InputSource(SourceKind.HAT, 0, 0x01))
        mapping.bind_button(Button.LEFT_TRIGGER, InputSource(SourceKind.AXIS, 2, -1))
        mapping.bind_axis("left_x", AxisBinding(0))
        mapping.bind_axis("left_y", AxisBinding(1, invert=True))
        mapping.key_axes["right_x"] = KeyAxisBinding(negative=4, positive=6)
        return mapping

    def test_round_trip(self):
        original = self._mapping()
        restored = DeviceMapping.from_dict(original.to_dict())

        assert restored.guid == original.guid
        assert restored.buttons == original.buttons
        assert restored.axes == original.axes
        assert restored.key_axes == original.key_axes

    def test_survives_a_hand_edited_config(self):
        """A broken entry must not stop the client starting."""
        payload = {
            "guid": "g",
            "buttons": {"not-a-number": {"kind": 0, "index": 1}, "1": {"kind": 0, "index": 2}},
            "axes": {"left_x": {"index": 0}, "bogus_axis": {"index": 9}},
        }

        mapping = DeviceMapping.from_dict(payload)

        assert mapping.buttons == {1: InputSource(SourceKind.BUTTON, 2)}
        assert "bogus_axis" not in mapping.axes
        assert "left_x" in mapping.axes

    def test_empty_mapping_is_detected(self):
        assert DeviceMapping(guid="g").is_empty()
        assert not self._mapping().is_empty()


class TestCompile:
    """poll() runs up to 1000x/s per controller and must not allocate."""

    def test_compiles_to_plain_tuples(self):
        mapping = DeviceMapping(guid="g")
        mapping.bind_button(Button.A, InputSource(SourceKind.BUTTON, 3))
        mapping.bind_axis("left_x", AxisBinding(0))
        mapping.bind_axis("left_trigger", AxisBinding(4))

        compiled = mapping.compile()

        assert compiled.buttons == ((int(SourceKind.BUTTON), 3, 0, int(Button.A)),)
        assert compiled.sticks == (("left_x", 0, False),)
        assert compiled.triggers == (("left_trigger", 4, False),)
        # Tuples, not dicts: the poll loop indexes them without hashing.
        assert isinstance(compiled.buttons, tuple)

    def test_sticks_and_triggers_are_kept_apart(self):
        """Sticks are signed and triggers unsigned; conflating them inverts one."""
        mapping = DeviceMapping(guid="g")
        for name in STICK_AXES + TRIGGER_AXES:
            mapping.bind_axis(name, AxisBinding(0))

        compiled = mapping.compile()

        assert len(compiled.sticks) == len(STICK_AXES)
        assert len(compiled.triggers) == len(TRIGGER_AXES)


class TestDefaultJoystickMapping:
    """A guess, clearly labelled as such -- but a useful one."""

    @pytest.fixture
    def mapping(self) -> DeviceMapping:
        # The 8BitDo 64's real shape, from SDL.
        return default_joystick_mapping(
            EIGHTBITDO_GUID, "8BitDo 64", axes=6, buttons=18, hats=1
        )

    def test_binds_the_face_buttons(self, mapping):
        for index, bit in enumerate((Button.A, Button.B, Button.X, Button.Y)):
            assert mapping.buttons[bit] == InputSource(SourceKind.BUTTON, index)

    def test_dpad_comes_from_the_hat(self, mapping):
        assert mapping.buttons[Button.DPAD_UP] == InputSource(SourceKind.HAT, 0, 0x01)
        assert mapping.buttons[Button.DPAD_RIGHT] == InputSource(SourceKind.HAT, 0, 0x02)
        assert mapping.buttons[Button.DPAD_DOWN] == InputSource(SourceKind.HAT, 0, 0x04)
        assert mapping.buttons[Button.DPAD_LEFT] == InputSource(SourceKind.HAT, 0, 0x08)

    def test_six_axis_pad_puts_triggers_on_2_and_5(self, mapping):
        """The near-universal layout for a six-axis pad."""
        assert mapping.axes["left_x"] == AxisBinding(0)
        assert mapping.axes["left_y"] == AxisBinding(1)
        assert mapping.axes["right_x"] == AxisBinding(3)
        assert mapping.axes["right_y"] == AxisBinding(4)
        assert mapping.axes["left_trigger"] == AxisBinding(2)
        assert mapping.axes["right_trigger"] == AxisBinding(5)

    def test_four_axis_pad_has_no_analog_triggers(self):
        mapping = default_joystick_mapping("g", "pad", axes=4, buttons=12, hats=1)

        assert mapping.axes["right_x"] == AxisBinding(2)
        assert "left_trigger" not in mapping.axes

    def test_no_hat_means_no_dpad_binding(self):
        mapping = default_joystick_mapping("g", "pad", axes=2, buttons=4, hats=0)

        assert Button.DPAD_UP not in mapping.buttons

    def test_does_not_invent_buttons_the_pad_lacks(self):
        mapping = default_joystick_mapping("g", "pad", axes=2, buttons=4, hats=0)

        assert set(mapping.buttons) == {Button.A, Button.B, Button.X, Button.Y}


class TestBindableSet:
    def test_every_bindable_button_has_a_label(self):
        for bit, label in BINDABLE_BUTTONS:
            assert label
            assert button_label(bit) == label

    def test_no_duplicate_bits(self):
        bits = [bit for bit, _ in BINDABLE_BUTTONS]
        assert len(bits) == len(set(bits))

    def test_threshold_is_inside_axis_range(self):
        assert 0 < AXIS_PRESS_THRESHOLD < 32767


class TestSourceDescriptions:
    """These strings are what the mapping screen shows for each binding."""

    @pytest.mark.parametrize(
        "source,expected",
        [
            (InputSource(SourceKind.BUTTON, 3), "Button 3"),
            (InputSource(SourceKind.AXIS, 2, 1), "Axis 2+"),
            (InputSource(SourceKind.AXIS, 2, -1), "Axis 2-"),
            (InputSource(SourceKind.HAT, 0, 0x01), "Hat 0 up"),
            (InputSource(SourceKind.HAT, 0, 0x06), "Hat 0 right+down"),
        ],
    )
    def test_describe(self, source, expected):
        assert source.describe() == expected

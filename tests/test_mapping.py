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


class TestSecondBinding:
    """One logical button can be driven by two physical controls.

    Kept as a separate dict rather than making ``buttons`` hold lists: the poll
    loop ORs bits, so a second source needs no handling downstream, and every
    config written before this still loads.
    """

    def test_both_sources_compile(self):
        mapping = DeviceMapping(guid="g")
        mapping.bind_button(Button.A, InputSource(SourceKind.BUTTON, 3))
        mapping.bind_button_alt(Button.A, InputSource(SourceKind.BUTTON, 11))

        compiled = mapping.compile()
        bits = [entry[3] for entry in compiled.buttons]

        assert bits == [int(Button.A), int(Button.A)]
        assert {entry[1] for entry in compiled.buttons} == {3, 11}

    def test_sources_for_lists_primary_first(self):
        mapping = DeviceMapping(guid="g")
        mapping.bind_button(Button.A, InputSource(SourceKind.BUTTON, 3))
        mapping.bind_button_alt(Button.A, InputSource(SourceKind.BUTTON, 11))

        assert [s.index for s in mapping.sources_for(Button.A)] == [3, 11]

    def test_an_alt_with_no_primary_is_promoted(self):
        """Otherwise it would sit in a table the primary lookup never reads."""
        mapping = DeviceMapping(guid="g")
        mapping.bind_button_alt(Button.A, InputSource(SourceKind.BUTTON, 5))

        assert mapping.buttons[Button.A].index == 5
        assert Button.A not in mapping.buttons_alt

    def test_clearing_the_primary_clears_the_alt(self):
        """A cleared button must not keep firing from its second source."""
        mapping = DeviceMapping(guid="g")
        mapping.bind_button(Button.A, InputSource(SourceKind.BUTTON, 3))
        mapping.bind_button_alt(Button.A, InputSource(SourceKind.BUTTON, 11))

        mapping.bind_button(Button.A, None)

        assert mapping.sources_for(Button.A) == []

    def test_the_alt_can_be_cleared_alone(self):
        mapping = DeviceMapping(guid="g")
        mapping.bind_button(Button.A, InputSource(SourceKind.BUTTON, 3))
        mapping.bind_button_alt(Button.A, InputSource(SourceKind.BUTTON, 11))

        mapping.bind_button_alt(Button.A, None)

        assert [s.index for s in mapping.sources_for(Button.A)] == [3]

    def test_it_round_trips(self):
        mapping = DeviceMapping(guid="g")
        mapping.bind_button(Button.A, InputSource(SourceKind.BUTTON, 3))
        mapping.bind_button_alt(Button.A, InputSource(SourceKind.HAT, 0, 0x01))

        restored = DeviceMapping.from_dict(mapping.to_dict())

        assert restored.buttons_alt[Button.A] == InputSource(SourceKind.HAT, 0, 0x01)

    def test_a_config_without_alts_still_loads(self):
        """Every configuration written before this lacks the key."""
        restored = DeviceMapping.from_dict(
            {"guid": "g", "buttons": {"1": {"kind": 0, "index": 3, "value": 0}}}
        )

        assert restored.buttons[Button.A].index == 3
        assert restored.buttons_alt == {}


class TestDigitalTriggers:
    """A trigger with no analog travel still has to reach the console.

    ``apply_trigger_buttons`` derives both trigger bits from the analog values
    on **every** poll. A pad whose Z or LT is a plain button therefore sets the
    bit during mapping and has it cleared again microseconds later -- the
    binding reads as correct in the mapping screen and nothing happens in the
    game, with no error anywhere. The compiled flags tell the poll path to
    synthesize a full-scale analog value instead.
    """

    def test_an_analog_trigger_is_flagged_as_analog(self):
        mapping = DeviceMapping(guid="g")
        mapping.bind_axis("left_trigger", AxisBinding(4))
        mapping.bind_axis("right_trigger", AxisBinding(5))

        compiled = mapping.compile()

        assert compiled.left_trigger_is_analog is True
        assert compiled.right_trigger_is_analog is True

    def test_a_trigger_with_no_axis_is_flagged(self):
        mapping = DeviceMapping(guid="g")
        mapping.bind_button(Button.LEFT_TRIGGER, InputSource(SourceKind.BUTTON, 6))

        compiled = mapping.compile()

        assert compiled.left_trigger_is_analog is False
        assert compiled.right_trigger_is_analog is False

    def test_each_trigger_is_flagged_independently(self):
        """A pad can have one of each -- an N64 kit's Z beside a real trigger."""
        mapping = DeviceMapping(guid="g")
        mapping.bind_axis("right_trigger", AxisBinding(5))

        compiled = mapping.compile()

        assert compiled.left_trigger_is_analog is False
        assert compiled.right_trigger_is_analog is True

    def test_the_bit_would_be_lost_without_the_synthesized_value(self):
        """The failure this exists to prevent, stated directly."""
        from common.state import ControllerState

        state = ControllerState()
        state.buttons = Button.LEFT_TRIGGER | Button.A
        state.apply_trigger_buttons()

        assert not state.buttons & Button.LEFT_TRIGGER

    def test_the_bit_survives_once_the_value_is_synthesized(self):
        from common.state import ControllerState

        state = ControllerState()
        state.buttons = Button.LEFT_TRIGGER | Button.A
        state.left_trigger = 255       # what the poll path now does
        state.apply_trigger_buttons()

        assert state.buttons & Button.LEFT_TRIGGER
        assert state.buttons & Button.A


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


class TestASecondSourceNeverOutlivesItsPrimary:
    """Reported as "a second button gets assigned without pressing +", and as
    bindings changing on their own.

    ``buttons_alt`` was added after ``buttons`` and several places that
    maintain one were never taught about the other. An alternate that outlives
    the primary it was a second source for is invisible in the editor -- rows
    are built from the layout, and ``_populate_bindings`` only builds one per
    bindable bit -- while ``compile()`` still emits it, so it keeps reaching
    the console. It also round-trips through the config file, which is why it
    came back long after whatever set it.
    """

    def _source(self, index):
        from client.input.mapping import InputSource, SourceKind

        return InputSource(SourceKind.BUTTON, index)

    def _mapping(self):
        from client.input.mapping import DeviceMapping

        return DeviceMapping(guid="pad", name="Test Pad")

    def test_rebinding_the_primary_drops_the_second_source(self):
        """The reported bug, directly: bind, add a second, rebind, and the old
        second source must not still be attached."""
        from common.state import Button

        mapping = self._mapping()
        mapping.bind_button(Button.A, self._source(1))
        mapping.bind_button_alt(Button.A, self._source(2))
        assert mapping.sources_for(Button.A) == [self._source(1), self._source(2)]

        mapping.bind_button(Button.A, self._source(3))

        assert mapping.sources_for(Button.A) == [self._source(3)], (
            "rebinding kept a second source the player never asked for"
        )

    def test_clearing_still_clears_both(self):
        from common.state import Button

        mapping = self._mapping()
        mapping.bind_button(Button.A, self._source(1))
        mapping.bind_button_alt(Button.A, self._source(2))
        mapping.bind_button(Button.A, None)
        assert mapping.sources_for(Button.A) == []

    def test_the_plus_flow_still_works(self):
        """The guard must not cost the feature it protects."""
        from common.state import Button

        mapping = self._mapping()
        mapping.bind_button(Button.A, self._source(1))
        mapping.bind_button_alt(Button.A, self._source(2))
        assert len(mapping.sources_for(Button.A)) == 2
        assert len(mapping.compile().buttons) == 2

    def test_an_alternate_alone_is_not_an_empty_mapping(self):
        """is_empty drives two things that both misfire on a false positive:
        MappingDialog replaces an "empty" mapping with generated defaults, and
        configured_layouts hides the type -- while compile() still emits it."""
        from common.state import Button

        mapping = self._mapping()
        mapping.buttons_alt[int(Button.A)] = self._source(2)

        assert not mapping.is_empty()
        assert mapping.compile().buttons, "it is emitted, so it is not empty"

    def test_a_truly_empty_mapping_still_reports_empty(self):
        assert self._mapping().is_empty()


class TestTrimmingCoversBothTables:
    """``_trim_to_layout`` drops bindings for controls the target system does
    not have, because "a binding the list does not show is not inert: it still
    reaches the console". That applied to the primaries only."""

    def test_out_of_layout_alternates_are_dropped(self):
        from client.gui.mapping_dialog import _trim_to_layout
        from client.input.mapping import DeviceMapping, InputSource, SourceKind
        from common.state import Button

        mapping = DeviceMapping(guid="pad", name="Test Pad")
        # An NES pad has no stick click, on either table.
        mapping.buttons[int(Button.LEFT_STICK)] = InputSource(SourceKind.BUTTON, 9)
        mapping.buttons_alt[int(Button.LEFT_STICK)] = InputSource(SourceKind.BUTTON, 10)
        mapping.buttons[int(Button.A)] = InputSource(SourceKind.BUTTON, 0)

        _trim_to_layout(mapping, "nes")

        assert int(Button.LEFT_STICK) not in mapping.buttons
        assert int(Button.LEFT_STICK) not in mapping.buttons_alt, (
            "an out-of-layout second source survived, invisible and live"
        )
        assert int(Button.A) in mapping.buttons

    def test_in_layout_alternates_survive(self):
        from client.gui.mapping_dialog import _trim_to_layout
        from client.input.mapping import DeviceMapping, InputSource, SourceKind
        from common.state import Button

        mapping = DeviceMapping(guid="pad", name="Test Pad")
        mapping.buttons[int(Button.A)] = InputSource(SourceKind.BUTTON, 0)
        mapping.buttons_alt[int(Button.A)] = InputSource(SourceKind.BUTTON, 1)

        _trim_to_layout(mapping, "nes")

        assert int(Button.A) in mapping.buttons_alt

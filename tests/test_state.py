"""Controller state semantics."""

from __future__ import annotations

import pytest

from common.state import (
    AXIS_MAX,
    AXIS_MIN,
    TRIGGER_DIGITAL_THRESHOLD,
    Button,
    ControllerState,
    clamp_axis,
    clamp_trigger,
    scale_sdl_trigger,
)


def test_default_state_is_neutral():
    assert ControllerState().is_neutral()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"buttons": Button.A},
        {"left_x": 1},
        {"right_y": -1},
        {"left_trigger": 1},
        {"right_trigger": 255},
    ],
)
def test_any_activity_is_not_neutral(kwargs):
    assert not ControllerState(**kwargs).is_neutral()


def test_copy_into_replicates_all_fields():
    src = ControllerState(Button.B, 1, 2, 3, 4, 5, 6)
    dst = ControllerState()
    src.copy_into(dst)
    assert dst == src


def test_copy_into_is_a_snapshot_not_an_alias():
    """The input loop reuses two objects, so mutating the source afterwards
    must not retroactively change the copy."""
    src = ControllerState(left_x=100)
    dst = ControllerState()
    src.copy_into(dst)
    src.left_x = 999
    assert dst.left_x == 100


def test_clear_resets_everything():
    state = ControllerState(Button.A | Button.START, 1, 2, 3, 4, 5, 6)
    state.clear()
    assert state.is_neutral()


class TestChangeDetection:
    def test_identical_states_do_not_differ(self):
        assert not ControllerState(Button.A, 100).differs_from(ControllerState(Button.A, 100))

    def test_button_change_detected(self):
        assert ControllerState(buttons=Button.A).differs_from(ControllerState(buttons=Button.B))

    def test_axis_change_detected(self):
        assert ControllerState(left_x=100).differs_from(ControllerState(left_x=101))

    def test_deadband_suppresses_small_axis_drift(self):
        """Worn analog sticks jitter constantly; without a deadband that would
        flood the network with meaningless packets."""
        a = ControllerState(left_x=100)
        b = ControllerState(left_x=110)
        assert a.differs_from(b)
        assert not a.differs_from(b, axis_deadband=32)

    def test_deadband_never_suppresses_buttons(self):
        """A dropped button press is far worse than an extra packet."""
        a = ControllerState(buttons=Button.A, left_x=100)
        b = ControllerState(buttons=Button.NONE, left_x=101)
        assert a.differs_from(b, axis_deadband=1000)

    def test_deadband_applies_to_triggers(self):
        a = ControllerState(left_trigger=100)
        b = ControllerState(left_trigger=105)
        assert not a.differs_from(b, axis_deadband=10)


class TestTriggerButtons:
    def test_above_threshold_sets_bit(self):
        state = ControllerState(left_trigger=TRIGGER_DIGITAL_THRESHOLD)
        state.apply_trigger_buttons()
        assert state.buttons & Button.LEFT_TRIGGER

    def test_below_threshold_clears_bit(self):
        state = ControllerState(buttons=Button.LEFT_TRIGGER, left_trigger=0)
        state.apply_trigger_buttons()
        assert not state.buttons & Button.LEFT_TRIGGER

    def test_triggers_are_independent(self):
        state = ControllerState(left_trigger=255, right_trigger=0)
        state.apply_trigger_buttons()
        assert state.buttons & Button.LEFT_TRIGGER
        assert not state.buttons & Button.RIGHT_TRIGGER

    def test_does_not_disturb_other_buttons(self):
        state = ControllerState(buttons=Button.A | Button.START, left_trigger=255)
        state.apply_trigger_buttons()
        assert state.buttons & Button.A
        assert state.buttons & Button.START


@pytest.mark.parametrize(
    "value,expected",
    [(0, 0), (100, 100), (AXIS_MAX + 1000, AXIS_MAX), (AXIS_MIN - 1000, AXIS_MIN)],
)
def test_clamp_axis(value, expected):
    assert clamp_axis(value) == expected


@pytest.mark.parametrize("value,expected", [(-5, 0), (0, 0), (128, 128), (300, 255)])
def test_clamp_trigger(value, expected):
    assert clamp_trigger(value) == expected


@pytest.mark.parametrize(
    "sdl_value,expected",
    [(0, 0), (-100, 0), (32767, 255), (16384, 128)],
)
def test_scale_sdl_trigger(sdl_value, expected):
    assert scale_sdl_trigger(sdl_value) == expected


def test_scale_sdl_trigger_stays_in_uint8():
    for value in range(0, 32768, 97):
        assert 0 <= scale_sdl_trigger(value) <= 255


def test_button_flags_are_unique():
    values = [b.value for b in Button if b is not Button.NONE]
    assert len(values) == len(set(values))


def test_button_flags_fit_in_uint32():
    """The wire format packs buttons into a u32."""
    for button in Button:
        assert button.value <= 0xFFFFFFFF

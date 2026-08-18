"""The SDL2 backend's polling, driven against a stubbed SDL.

These exercise the real ``_poll_joystick`` / ``_poll_mapped_with_override``
code by replacing the ``sdl2`` functions the module calls, so the arithmetic
that turns raw readings into a ControllerState is covered without hardware.

The digital-trigger case is the one that needs it. A trigger bound to a plain
button sets its bit, and ``apply_trigger_buttons`` then recomputes that same bit
from the analog value -- which is zero, because a button has no travel. Without
the synthesized full-scale value the binding reads as correct everywhere and
never reaches the console.
"""

from __future__ import annotations

import pytest

sdl2 = pytest.importorskip("sdl2", reason="PySDL2 not installed")

from client.input import sdl2_backend  # noqa: E402
from client.input.mapping import (  # noqa: E402
    AxisBinding,
    DeviceMapping,
    InputSource,
    SourceKind,
)
from common.state import Button, ControllerState  # noqa: E402


class FakePad:
    """Stand-in for an SDL joystick handle."""

    def __init__(self, axes=6, buttons=16, hats=1):
        self.axes = [0] * axes
        self.buttons = [False] * buttons
        self.hats = [0] * hats


@pytest.fixture
def sdl(monkeypatch):
    """Point the module's sdl2 calls at whichever FakePad is passed as a handle."""
    monkeypatch.setattr(sdl2, "SDL_JoystickGetAttached", lambda pad: True)
    monkeypatch.setattr(
        sdl2, "SDL_JoystickGetButton", lambda pad, i: pad.buttons[i]
    )
    monkeypatch.setattr(sdl2, "SDL_JoystickGetAxis", lambda pad, i: pad.axes[i])
    monkeypatch.setattr(sdl2, "SDL_JoystickGetHat", lambda pad, i: pad.hats[i])
    return sdl2


def _backend(mapping: DeviceMapping, pad: FakePad):
    backend = sdl2_backend.SDL2Backend()
    backend._joysticks[0] = pad
    backend._compiled[0] = mapping.compile()
    return backend


def _poll(backend, pad) -> ControllerState:
    state = ControllerState()
    assert backend._poll_joystick(0, backend._compiled[0], state)
    return state


class TestDigitalTrigger:
    """A button bound to a trigger: full pull pressed, nothing released."""

    def _mapping(self) -> DeviceMapping:
        mapping = DeviceMapping(guid="g", name="Retro Pad")
        mapping.bind_button(Button.LEFT_TRIGGER, InputSource(SourceKind.BUTTON, 6))
        return mapping

    def test_pressing_it_is_a_full_pull(self, sdl):
        pad = FakePad()
        backend = _backend(self._mapping(), pad)
        pad.buttons[6] = True

        state = _poll(backend, pad)

        assert state.left_trigger == 255
        assert state.buttons & Button.LEFT_TRIGGER

    def test_releasing_it_is_no_pull(self, sdl):
        pad = FakePad()
        backend = _backend(self._mapping(), pad)
        pad.buttons[6] = False

        state = _poll(backend, pad)

        assert state.left_trigger == 0
        assert not state.buttons & Button.LEFT_TRIGGER

    def test_it_survives_apply_trigger_buttons(self, sdl):
        """The bug this guards: the bit is set, then recomputed from 0 and lost."""
        pad = FakePad()
        backend = _backend(self._mapping(), pad)
        pad.buttons[6] = True

        state = _poll(backend, pad)
        state.apply_trigger_buttons()

        assert state.buttons & Button.LEFT_TRIGGER

    def test_the_other_trigger_is_unaffected(self, sdl):
        pad = FakePad()
        backend = _backend(self._mapping(), pad)
        pad.buttons[6] = True

        state = _poll(backend, pad)

        assert state.right_trigger == 0
        assert not state.buttons & Button.RIGHT_TRIGGER

    def test_a_hat_bound_trigger_works_too(self, sdl):
        """Some retro pads report shoulder buttons on a hat."""
        mapping = DeviceMapping(guid="g")
        mapping.bind_button(Button.RIGHT_TRIGGER, InputSource(SourceKind.HAT, 0, 0x02))
        pad = FakePad()
        backend = _backend(mapping, pad)
        pad.hats[0] = 0x02

        state = _poll(backend, pad)

        assert state.right_trigger == 255
        assert state.buttons & Button.RIGHT_TRIGGER


class TestAnalogTrigger:
    """An axis-bound trigger keeps its travel; nothing is synthesized."""

    def _mapping(self) -> DeviceMapping:
        mapping = DeviceMapping(guid="g", name="Modern Pad")
        mapping.bind_axis("left_trigger", AxisBinding(2))
        return mapping

    @pytest.mark.parametrize(
        "reading,expected",
        [(0, 0), (16384, 128), (32767, 255)],
    )
    def test_travel_is_reported(self, sdl, reading, expected):
        pad = FakePad()
        backend = _backend(self._mapping(), pad)
        pad.axes[2] = reading

        state = _poll(backend, pad)

        assert state.left_trigger == expected

    def test_a_half_pull_does_not_read_as_full(self, sdl):
        """The synthesized value must not leak into the analog path."""
        pad = FakePad()
        backend = _backend(self._mapping(), pad)
        pad.axes[2] = 12000

        state = _poll(backend, pad)

        assert 0 < state.left_trigger < 255

    def test_the_digital_bit_follows_the_travel(self, sdl):
        pad = FakePad()
        backend = _backend(self._mapping(), pad)

        pad.axes[2] = 32767
        assert _poll(backend, pad).buttons & Button.LEFT_TRIGGER

        pad.axes[2] = 0
        assert not _poll(backend, pad).buttons & Button.LEFT_TRIGGER


class TestStickAxes:
    def test_an_inverted_axis_is_flipped(self, sdl):
        mapping = DeviceMapping(guid="g")
        mapping.bind_axis("left_x", AxisBinding(0, invert=True))
        pad = FakePad()
        backend = _backend(mapping, pad)
        pad.axes[0] = 20000

        state = _poll(backend, pad)

        assert state.left_x == -20000

    def test_an_upright_axis_passes_through(self, sdl):
        mapping = DeviceMapping(guid="g")
        mapping.bind_axis("left_x", AxisBinding(0))
        pad = FakePad()
        backend = _backend(mapping, pad)
        pad.axes[0] = 20000

        state = _poll(backend, pad)

        assert state.left_x == 20000

    def test_an_axis_half_can_drive_a_button(self, sdl):
        """How the N64's C cluster rides the right stick."""
        mapping = DeviceMapping(guid="g")
        mapping.bind_button(Button.BACK, InputSource(SourceKind.AXIS, 3, 1))
        pad = FakePad()
        backend = _backend(mapping, pad)

        pad.axes[3] = 30000
        assert _poll(backend, pad).buttons & Button.BACK

        pad.axes[3] = -30000
        assert not _poll(backend, pad).buttons & Button.BACK

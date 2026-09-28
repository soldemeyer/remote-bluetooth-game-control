"""The N64's Z, on a pad that reports its stick twice.

Reported as "I cannot bind the Z button -- it is being assigned to the down
joystick". The mapping screen was innocent: it bound Z to its button every
time. What undid it was a binding the screen never showed.

A new configuration started from the generic six-axis guess, which binds
``left_trigger`` to axis 2, and `default_configuration` kept all of it -- unlike
the mapping screen's own starting guess, which is trimmed to the type. The N64's
Z is a plain switch, so that type has no analog row to show or clear it. And the
8BitDo N64 Modkit reports its stick's vertical axis on **axes 1 and 2 at once**,
identical on every sample (recorded from the operator's pad through the client's
own backend). So with axis 2 bound as the trigger:

* pressing Z set the bit, and ``apply_trigger_buttons`` cleared it again from an
  analog value of zero -- the full-scale value a button-bound trigger gets is
  only synthesized when *no* axis drives it;
* pushing the stick down drove axis 2, and so pulled Z.

The readings below are the recorded ones.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("sdl2", reason="PySDL2 not installed")

from client.gui.controller_config import (  # noqa: E402
    ControllerConfiguration,
    default_configuration,
)
from client.input.mapping import InputSource, SourceKind  # noqa: E402
from common.state import Button  # noqa: E402
from tests.test_sdl2_backend import FakePad, _backend, _poll, sdl  # noqa: E402,F401

Z = int(Button.LEFT_TRIGGER)

MODKIT = SimpleNamespace(
    guid="030095fcc82d00006928000000000000", name="8BitDo N64 Modkit",
    axis_count=6, button_count=15, hat_count=1,
    display_name=lambda: "8BitDo N64 Modkit",
)

#: Recorded: the stick at rest, and pushed fully down. Axis 2 follows axis 1.
AT_REST = [0, -512, -512, 0, 0, 0]
STICK_DOWN = [4162, 30947, 30947, 0, 0, 0]
#: Recorded: Z is button 8, and moves no axis at all.
Z_BUTTON = 8


def modkit(axes=AT_REST, z=False) -> FakePad:
    pad = FakePad(axes=6, buttons=15, hats=1)
    pad.axes = list(axes)
    pad.buttons[Z_BUTTON] = z
    return pad


def bound_by_the_player(configuration: ControllerConfiguration):
    """What the mapping screen does when Z is pressed at its prompt."""
    mapping = configuration.mapping
    mapping.bind_button(Z, InputSource(SourceKind.BUTTON, Z_BUTTON))
    return mapping


def poll(mapping, pad):
    return _poll(_backend(mapping, pad), pad)


class TestANewN64Configuration:
    def test_has_no_analog_z(self):
        configuration = default_configuration(MODKIT, "n64")

        assert "left_trigger" not in configuration.mapping.axes
        assert not configuration.mapping.compile().left_trigger_is_analog

    def test_pressing_z_pulls_z(self, sdl):
        mapping = bound_by_the_player(default_configuration(MODKIT, "n64"))

        state = poll(mapping, modkit(z=True))

        assert state.buttons & Z
        assert state.left_trigger == 255

    def test_pushing_the_stick_down_does_not(self, sdl):
        mapping = bound_by_the_player(default_configuration(MODKIT, "n64"))

        state = poll(mapping, modkit(STICK_DOWN))

        assert not state.buttons & Z
        assert state.left_y > 0, "the stick itself must still read down"

    def test_the_untrimmed_guess_is_the_report(self, sdl):
        """The control: the same pad and the same player binding, with the
        guess left whole, reproduces both halves of what was reported."""
        configuration = default_configuration(MODKIT, "xbox")
        configuration.layout = "n64"
        configuration.mappings = {"n64": configuration.mappings["xbox"]}
        mapping = bound_by_the_player(configuration)

        assert not poll(mapping, modkit(z=True)).buttons & Z
        assert poll(mapping, modkit(STICK_DOWN)).buttons & Z

    def test_a_type_with_analog_triggers_keeps_them(self):
        """Trimming removes what a type lacks, not triggers in general."""
        configuration = default_configuration(MODKIT, "xbox")

        assert configuration.mapping.axes["left_trigger"].index == 2
        assert configuration.mapping.axes["right_trigger"].index == 5


#: The operator's saved configuration, as the client wrote it: the untrimmed
#: guess, plus the C buttons, shoulders, Z and Start bound by hand.
SAVED = {
    "name": "8BitDo N64 Modkit — Nintendo 64",
    "layout": "n64",
    "device_guid": MODKIT.guid,
    "device_name": MODKIT.name,
    "mappings": {"n64": {
        "guid": MODKIT.guid, "name": MODKIT.name,
        "buttons": {
            "1": {"kind": 0, "index": 0, "value": 0},
            "2": {"kind": 0, "index": 1, "value": 0},
            "4": {"kind": 0, "index": 2, "value": 0},
            "8": {"kind": 0, "index": 3, "value": 0},
            "16": {"kind": 0, "index": 6, "value": 0},
            "32": {"kind": 0, "index": 7, "value": 0},
            "64": {"kind": 1, "index": 3, "value": 1},
            "128": {"kind": 0, "index": 11, "value": 0},
            "256": {"kind": 1, "index": 3, "value": -1},
            "512": {"kind": 0, "index": 9, "value": 0},
            "1024": {"kind": 1, "index": 4, "value": 1},
            "2048": {"kind": 2, "index": 0, "value": 1},
            "16384": {"kind": 2, "index": 0, "value": 2},
            "4096": {"kind": 2, "index": 0, "value": 4},
            "8192": {"kind": 2, "index": 0, "value": 8},
            "32768": {"kind": 1, "index": 4, "value": -1},
            "65536": {"kind": 0, "index": 8, "value": 0},
        },
        "buttons_alt": {},
        "axes": {
            "left_x": {"index": 0, "invert": False},
            "left_y": {"index": 1, "invert": False},
            "right_x": {"index": 3, "invert": False},
            "right_y": {"index": 4, "invert": False},
            "left_trigger": {"index": 2, "invert": False},
            "right_trigger": {"index": 5, "invert": False},
        },
        "key_axes": {},
    }},
}


class TestASavedConfigurationIsRepairedOnLoad:
    def _loaded(self):
        return ControllerConfiguration.from_dict(SAVED).mapping

    def test_the_bindings_the_n64_lacks_are_gone(self):
        mapping = self._loaded()

        assert set(mapping.axes) == {"left_x", "left_y"}
        assert int(Button.X) not in mapping.buttons
        assert int(Button.Y) not in mapping.buttons
        # The N64 stick does not click.
        assert int(Button.LEFT_STICK) not in mapping.buttons

    def test_everything_the_player_bound_survives(self):
        mapping = self._loaded()

        assert mapping.buttons[Z] == InputSource(SourceKind.BUTTON, Z_BUTTON)
        assert mapping.buttons[int(Button.START)] == InputSource(SourceKind.BUTTON, 11)
        # The C cluster, which rides the right stick's axes as button sources.
        assert mapping.buttons[int(Button.BACK)] == InputSource(SourceKind.AXIS, 3, 1)
        assert mapping.buttons[int(Button.CAPTURE)] == InputSource(SourceKind.AXIS, 4, -1)

    def test_z_works_and_the_stick_no_longer_pulls_it(self, sdl):
        mapping = self._loaded()

        assert poll(mapping, modkit(z=True)).buttons & Z
        assert not poll(mapping, modkit(STICK_DOWN)).buttons & Z

    def test_it_says_what_it_dropped(self, caplog):
        with caplog.at_level(logging.INFO, logger="client.gui.controller_config"):
            ControllerConfiguration.from_dict(SAVED)

        # X, Y and a stick click; the right stick's two axes; both triggers.
        assert "dropped 7 binding(s) the Nintendo 64 type" in caplog.text

    def test_a_type_this_build_does_not_know_is_left_alone(self):
        """Trimming against the fallback type would strip it wholesale."""
        data = dict(SAVED, layout="vectrex", mappings={"vectrex": SAVED["mappings"]["n64"]})

        mapping = ControllerConfiguration.from_dict(data).mapping

        assert "left_trigger" in mapping.axes
        assert int(Button.X) in mapping.buttons

    def test_a_clean_configuration_says_nothing(self, caplog):
        configuration = default_configuration(MODKIT, "n64")

        with caplog.at_level(logging.INFO, logger="client.gui.controller_config"):
            ControllerConfiguration.from_dict(configuration.to_dict())

        assert "dropped" not in caplog.text

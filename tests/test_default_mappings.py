"""Built-in default mappings: every physical preset against every virtual one.

What these protect is the *meaning* of a default, not its spelling. A preset
names a position (``a`` is the bottom face button on every pad), so the checks
here are phrased the way a player would: "the Switch Pro pad's B drives the
Xbox target's A", "the N64's Z and ZR are different buttons". Anything that
turns the rule back into printed labels, or re-merges two controls, fails
here with a sentence saying which.

The per-family and per-layout rules live in ``client/gui/controller_presets.py``;
``docs/controller_mapping_matrix.md`` lays the whole thing out as tables and is
pinned against the rule at the bottom of this file.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from client.gui.controller_config import (
    CONFIG_FORMAT,
    FILE_VERSION,
    ConfigurationStore,
    ControllerConfiguration,
)
from client.gui.controller_layouts import LAYOUTS, LAYOUTS_BY_KEY
from client.gui.controller_presets import (
    FAMILIES,
    FAMILIES_BY_KEY,
    N64_MODKIT,
    build_layout_preset,
    build_preset,
    builtin_configurations,
    mappings_for,
    materialise,
    resolve,
)
from client.input.mapping import (
    PAD_AXES,
    PAD_BUTTON_CONTROLS,
    STICK_AXES,
    AxisBinding,
    DeviceMapping,
    InputSource,
    SourceKind,
    canonical_name,
    guid_vendor_product,
)
from common.state import Button, ControllerState

#: The kit the measured table was taken from.
MODKIT_GUID = "030095fcc82d00006928000000000000"


def _device(guid: str = "pad-guid", **kwargs):
    base = dict(
        guid=guid, name="Test Pad", instance_id=0,
        axis_count=6, button_count=12, hat_count=1,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _full_pad() -> dict:
    """Every control SDL can name, as ``pad_bindings()`` would report it."""
    names = [n for n in PAD_BUTTON_CONTROLS if not n.startswith("dpad_")]
    buttons = {
        name: InputSource(SourceKind.BUTTON, index)
        for index, name in enumerate(n for n in names if n not in ("left_trigger", "right_trigger"))
    }
    buttons.update(
        dpad_up=InputSource(SourceKind.HAT, 0, 0x01),
        dpad_right=InputSource(SourceKind.HAT, 0, 0x02),
        dpad_down=InputSource(SourceKind.HAT, 0, 0x04),
        dpad_left=InputSource(SourceKind.HAT, 0, 0x08),
        left_trigger=InputSource(SourceKind.AXIS, 4, 1),
        right_trigger=InputSource(SourceKind.AXIS, 5, 1),
    )
    axes = {name: AxisBinding(index) for index, name in enumerate(PAD_AXES)}
    return {"buttons": buttons, "axes": axes}


def _preset(family_key: str, layout_key: str):
    return build_layout_preset(FAMILIES_BY_KEY[family_key], LAYOUTS_BY_KEY[layout_key])


def _bit_for(layout_key: str, label: str) -> int:
    """The logical bit a layout labels ``label`` ("A", "Start / +", "Home")."""
    for bit, text in LAYOUTS_BY_KEY[layout_key].bindable():
        if text == label:
            return bit
    raise AssertionError(f"{layout_key} has no control labelled {label!r}")


def _driver(family_key: str, layout_key: str, label: str) -> str:
    """What the player presses, by the pad's own printing, for a target control."""
    family = FAMILIES_BY_KEY[family_key]
    control = _preset(family_key, layout_key).buttons[_bit_for(layout_key, label)]
    return family.label(control)


def _resolved(family_key: str, bindings: dict | None = None, guid: str = "pad-guid"):
    family = FAMILIES_BY_KEY[family_key]
    return resolve(build_preset(family), _device(guid), bindings, family.measured)


# ---------------------------------------------------------------------------
# Coverage: every preset, every target
# ---------------------------------------------------------------------------


class TestEveryPresetMeetsEveryTarget:
    @pytest.mark.parametrize("family", FAMILIES, ids=lambda f: f.key)
    def test_every_target_gets_a_mapping(self, family):
        mappings, _ = resolve(build_preset(family), _device(), _full_pad(), family.measured)

        assert set(mappings) == {layout.key for layout in LAYOUTS}

    @pytest.mark.parametrize("family", FAMILIES, ids=lambda f: f.key)
    def test_no_rule_names_a_control_that_does_not_exist(self, family):
        """A rule naming a control nothing reports resolves to nothing, forever,
        and looks exactly like a pad that lacks the control."""
        halves = {f"{axis}{sign}" for axis in STICK_AXES for sign in "+-"}
        vocabulary = set(PAD_BUTTON_CONTROLS) | halves

        for entry in build_preset(family):
            for control in [*entry.buttons.values(), *entry.alternates.values()]:
                assert control in vocabulary, f"{family.key}/{entry.layout}: {control!r}"
            for axis in entry.axes:
                assert axis in PAD_AXES

    @pytest.mark.parametrize("family", [f for f in FAMILIES if f.measured is None],
                             ids=lambda f: f.key)
    @pytest.mark.parametrize("layout", LAYOUTS, ids=lambda l: l.key)
    def test_a_full_pad_leaves_no_required_output_unbound(self, family, layout):
        """Every output the original controller has is bound on a pad that has
        every control. Optional Switch controls are bound too, here -- a full
        pad has a Home and a Select to give them."""
        mappings, _ = resolve(build_preset(family), _device(), _full_pad())
        bound = mappings[layout.key]
        missing = [
            label for bit, label in layout.bindable() if not bound.sources_for(bit)
        ]

        assert not missing, f"{family.key} -> {layout.key}: {missing}"

    @pytest.mark.parametrize("layout", LAYOUTS, ids=lambda l: l.key)
    def test_every_binding_the_measured_kit_gets_exists_on_the_kit(self, layout):
        """No raw index outside what was actually measured."""
        mappings, _ = _resolved("8bitdo_n64_modkit", guid=MODKIT_GUID)
        measured = set(N64_MODKIT.buttons.values())
        axes = {binding.index for binding in N64_MODKIT.axes.values()}

        mapping = mappings[layout.key]
        for source in [*mapping.buttons.values(), *mapping.buttons_alt.values()]:
            if source.kind is SourceKind.AXIS:
                assert source.index in axes
            else:
                assert source in measured, source
        for binding in mapping.axes.values():
            assert binding.index in axes


# ---------------------------------------------------------------------------
# Position first
# ---------------------------------------------------------------------------


class TestFaceButtonsFollowPosition:
    """The bottom button drives the bottom button, whatever either prints."""

    @pytest.mark.parametrize(
        "family,expected",
        [
            ("xbox", {"A": "A", "B": "B", "X": "X", "Y": "Y"}),
            ("playstation", {"A": "Cross", "B": "Circle", "X": "Square", "Y": "Triangle"}),
            # Nintendo prints A/B and X/Y mirrored: its B is at the bottom.
            ("switch_pro", {"A": "B", "B": "A", "X": "Y", "Y": "X"}),
            ("8bitdo_bluetooth", {"A": "B", "B": "A", "X": "Y", "Y": "X"}),
        ],
    )
    def test_onto_an_xbox_target(self, family, expected):
        driven = {label: _driver(family, "xbox", label) for label in expected}

        assert driven == expected

    def test_an_xbox_pad_onto_a_switch_target(self):
        """The Switch's B is its bottom button, so the Xbox pad's A drives it."""
        assert _driver("xbox", "switch", "B") == "A"
        assert _driver("xbox", "switch", "A") == "B"
        assert _driver("xbox", "switch", "Y") == "X"
        assert _driver("xbox", "switch", "X") == "Y"

    def test_a_playstation_pad_onto_a_switch_2_target(self):
        assert _driver("playstation", "switch2", "B") == "Cross"
        assert _driver("playstation", "switch2", "A") == "Circle"

    def test_a_switch_pad_onto_a_playstation_target(self):
        assert _driver("switch_pro", "ps5", "Cross") == "B"
        assert _driver("switch_pro", "ps5", "Circle") == "A"
        assert _driver("switch_pro", "ps5", "Triangle") == "X"

    def test_snes_is_positional(self):
        """SNES prints the same diamond as the Switch."""
        assert _driver("xbox", "snes", "B") == "A"
        assert _driver("xbox", "snes", "A") == "B"
        assert _driver("xbox", "snes", "Y") == "X"
        assert _driver("xbox", "snes", "X") == "Y"

    def test_nes_a_is_the_right_hand_button(self):
        """NES A is to the right of B, so it comes from the right face button
        (RetroArch's NES cores, Switch Online's NES app)."""
        assert _driver("xbox", "nes", "A") == "B"
        assert _driver("xbox", "nes", "B") == "A"
        assert _driver("switch_pro", "nes", "A") == "A"
        assert _driver("switch_pro", "nes", "B") == "B"

    def test_genesis_follows_its_two_rows(self):
        """Bottom row A B C across west, south, east; top row X Y Z across the
        left bumper, the top button and the right bumper (Genesis Plus GX)."""
        assert [_driver("xbox", "genesis", k) for k in ("A", "B", "C")] == ["X", "A", "B"]
        assert [_driver("xbox", "genesis", k) for k in ("X", "Y", "Z")] == ["LB", "Y", "RB"]

    def test_n64_a_is_the_bottom_button_and_b_the_left(self):
        """RetroArch's Mupen64Plus: A = RetroPad B, B = RetroPad Y."""
        assert _driver("xbox", "n64", "A") == "A"
        assert _driver("xbox", "n64", "B") == "X"
        assert _preset("xbox", "n64").alternates == {Button.B: "b"}

    def test_gamecube_follows_its_geometry(self):
        """Big A at the bottom, B to its left, X to its right, Y above."""
        assert _driver("xbox", "gamecube", "A") == "A"
        assert _driver("xbox", "gamecube", "B") == "X"
        assert _driver("xbox", "gamecube", "X") == "B"
        assert _driver("xbox", "gamecube", "Y") == "Y"
        assert _driver("switch_pro", "gamecube", "B") == "Y"

    def test_canonical_names_say_position(self):
        assert canonical_name("a") == "FACE_SOUTH"
        assert canonical_name("b") == "FACE_EAST"
        assert canonical_name("x") == "FACE_WEST"
        assert canonical_name("y") == "FACE_NORTH"
        assert canonical_name("right_y-") == "R_STICK_UP"


# ---------------------------------------------------------------------------
# The Switch's system buttons
# ---------------------------------------------------------------------------


class TestSwitchSystemButtons:
    @pytest.mark.parametrize("layout", ["switch", "switch2"])
    def test_a_switch_pad_drives_them_one_for_one(self, layout):
        assert _driver("switch_pro", layout, "Plus") == "+"
        assert _driver("switch_pro", layout, "Minus") == "−"
        assert _driver("switch_pro", layout, "Home") == "Home"
        assert _driver("switch_pro", layout, "Capture") == "Capture"

    @pytest.mark.parametrize("layout", ["switch", "switch2"])
    def test_an_xbox_pad_drives_them_from_its_own(self, layout):
        assert _driver("xbox", layout, "Plus") == "Menu"
        assert _driver("xbox", layout, "Minus") == "View"
        assert _driver("xbox", layout, "Home") == "Xbox button"
        assert _driver("xbox", layout, "Capture") == "Share (Series only)"

    @pytest.mark.parametrize("layout", ["switch", "switch2"])
    def test_a_playstation_pad_captures_with_its_touchpad(self, layout):
        assert _driver("playstation", layout, "Plus") == "Options"
        assert _driver("playstation", layout, "Minus") == "Create / Share"
        assert _driver("playstation", layout, "Home") == "PS button"
        assert _driver("playstation", layout, "Capture") == "Touchpad click"

    def test_the_ps5_target_keeps_mute_on_mute(self):
        """The touchpad rule is for a Switch; a PS5 target's capture bit *is*
        Mute."""
        assert _driver("playstation", "ps5", "Mute") == "Mute (DualSense)"


class TestOptionalSwitchControlsOnRetroTargets:
    @pytest.mark.parametrize("layout", ["nes", "snes", "genesis", "n64", "gamecube"])
    def test_home_is_an_optional_row_of_its_own(self, layout):
        layout_ = LAYOUTS_BY_KEY[layout]

        assert dict(layout_.bindable())[Button.GUIDE] == "Home"
        assert Button.GUIDE in layout_.optional_bits()
        assert _preset("xbox", layout).buttons[Button.GUIDE] == "guide"

    @pytest.mark.parametrize("layout,select", [("nes", "Select"), ("snes", "Select"),
                                               ("genesis", "Mode")])
    def test_plus_and_minus_are_the_start_and_select_they_already_are(self, layout, select):
        """One output, two names: on a Switch Pro target our Start *is* Plus,
        so a second binding would send the same thing."""
        labels = dict(LAYOUTS_BY_KEY[layout].bindable())

        assert labels[Button.START] == "Start / +"
        assert labels[Button.BACK] == f"{select} / −"
        assert Button.START not in LAYOUTS_BY_KEY[layout].optional_bits()
        assert Button.BACK not in LAYOUTS_BY_KEY[layout].optional_bits()

    @pytest.mark.parametrize("layout", ["n64", "gamecube"])
    def test_minus_is_its_own_row_where_there_is_no_select(self, layout):
        layout_ = LAYOUTS_BY_KEY[layout]

        assert dict(layout_.bindable())[Button.BACK] == "−"
        assert Button.BACK in layout_.optional_bits()
        assert dict(layout_.bindable())[Button.START] == "Start / +"

    def test_optional_controls_have_no_artwork(self):
        """Drawing a Home button on an NES would misdescribe the pad."""
        for layout in LAYOUTS:
            for control in layout.controls:
                if control.optional:
                    assert control.element == "", f"{layout.key}: {control.label}"

    def test_the_modern_targets_have_no_optional_rows(self):
        for key in ("xbox", "ps5", "switch", "switch2"):
            assert LAYOUTS_BY_KEY[key].optional_bits() == frozenset()

    def test_a_pad_without_a_home_leaves_it_unbound(self):
        """Optional means optional: nothing is invented to fill it."""
        bindings = _full_pad()
        del bindings["buttons"]["guide"]

        mappings, _ = resolve(build_preset(FAMILIES_BY_KEY["8bitdo_diy"]), _device(), bindings)

        assert Button.GUIDE not in mappings["nes"].buttons
        assert Button.START in mappings["nes"].buttons


# ---------------------------------------------------------------------------
# The N64
# ---------------------------------------------------------------------------


class TestN64:
    def test_c_buttons_are_the_right_stick(self):
        buttons = _preset("xbox", "n64").buttons

        assert buttons[Button.C_UP] == "right_y-"
        assert buttons[Button.C_DOWN] == "right_y+"
        assert buttons[Button.C_LEFT] == "right_x-"
        assert buttons[Button.C_RIGHT] == "right_x+"

    def test_c_buttons_have_their_own_bits(self):
        """Not Back, Guide, Capture or a stick click -- which a Switch Pro
        target reads as Minus, Home, a screenshot and L3."""
        c_bits = {Button.C_UP, Button.C_DOWN, Button.C_LEFT, Button.C_RIGHT}
        borrowed = {Button.BACK, Button.GUIDE, Button.CAPTURE, Button.RIGHT_STICK}
        labels = dict(LAYOUTS_BY_KEY["n64"].bindable())

        assert c_bits <= labels.keys()
        assert not {bit for bit in borrowed if labels.get(bit, "").startswith("C ")}

    def test_z_and_zr_are_distinct(self):
        """Two bits, two sources, and only ZR is optional."""
        layout = LAYOUTS_BY_KEY["n64"]
        labels = dict(layout.bindable())
        buttons = _preset("xbox", "n64").buttons

        assert labels[Button.LEFT_TRIGGER] == "Z"
        assert labels[Button.RIGHT_TRIGGER] == "ZR"
        assert buttons[Button.LEFT_TRIGGER] == "left_trigger"
        assert buttons[Button.RIGHT_TRIGGER] == "right_trigger"
        assert Button.RIGHT_TRIGGER in layout.optional_bits()
        assert Button.LEFT_TRIGGER not in layout.optional_bits()

    def test_z_and_zr_are_both_digital(self):
        """Neither takes the sticks-and-triggers row an analog trigger would."""
        layout = LAYOUTS_BY_KEY["n64"]

        assert not layout.has_axis("left_trigger")
        assert not layout.has_axis("right_trigger")

    def test_z_and_zr_fire_independently_on_a_pad(self):
        mappings, _ = resolve(build_preset(FAMILIES_BY_KEY["xbox"]), _device(), _full_pad())
        n64 = mappings["n64"]

        assert n64.buttons[Button.LEFT_TRIGGER] != n64.buttons[Button.RIGHT_TRIGGER]
        compiled = n64.compile()
        # No trigger axis on an N64, so each bit synthesises its own full pull
        # rather than being recomputed from travel -- see the SDL backend.
        assert compiled.left_trigger_is_analog is False
        assert compiled.right_trigger_is_analog is False

    def test_the_n64_has_no_right_stick_output(self):
        """The C buttons read the right stick; the N64 must not report one."""
        mappings, _ = resolve(build_preset(FAMILIES_BY_KEY["xbox"]), _device(), _full_pad())

        assert set(mappings["n64"].axes) == {"left_x", "left_y"}


class TestTheMeasuredN64Kit:
    """The player's own hand-bound mapping, reproduced by the built-in."""

    def test_its_own_controls_drive_an_n64_directly(self):
        mappings, approximate = _resolved("8bitdo_n64_modkit", guid=MODKIT_GUID)
        n64 = mappings["n64"]

        assert approximate is False
        assert n64.buttons[Button.A] == InputSource(SourceKind.BUTTON, 0)
        assert n64.buttons[Button.B] == InputSource(SourceKind.BUTTON, 1)
        assert n64.buttons[Button.LEFT_BUMPER] == InputSource(SourceKind.BUTTON, 6)
        assert n64.buttons[Button.RIGHT_BUMPER] == InputSource(SourceKind.BUTTON, 7)
        assert n64.buttons[Button.LEFT_TRIGGER] == InputSource(SourceKind.BUTTON, 8)
        assert n64.buttons[Button.START] == InputSource(SourceKind.BUTTON, 11)
        assert n64.buttons[Button.C_UP] == InputSource(SourceKind.AXIS, 4, -1)
        assert n64.buttons[Button.C_DOWN] == InputSource(SourceKind.AXIS, 4, 1)
        assert n64.buttons[Button.C_LEFT] == InputSource(SourceKind.AXIS, 3, -1)
        assert n64.buttons[Button.C_RIGHT] == InputSource(SourceKind.AXIS, 3, 1)
        assert n64.axes == {"left_x": AxisBinding(0), "left_y": AxisBinding(1)}

    def test_what_was_not_measured_is_not_bound(self):
        mappings, _ = _resolved("8bitdo_n64_modkit", guid=MODKIT_GUID)
        n64 = mappings["n64"]

        for bit in (Button.GUIDE, Button.BACK, Button.RIGHT_TRIGGER):
            assert bit not in n64.buttons

    def test_axis_2_is_never_a_trigger(self):
        """It is a copy of the stick's vertical axis on this kit."""
        mappings, _ = _resolved("8bitdo_n64_modkit", guid=MODKIT_GUID)

        for mapping in mappings.values():
            assert all(binding.index != 2 for binding in mapping.axes.values())

    def test_a_different_pad_is_labelled_approximate(self):
        """The measurement belongs to one model; on anything else it is a guess."""
        _, approximate = _resolved("8bitdo_n64_modkit", guid="pad-guid")

        assert approximate is True

    def test_sdl_wins_where_it_knows_the_pad(self):
        _, approximate = _resolved("8bitdo_n64_modkit", bindings=_full_pad(), guid=MODKIT_GUID)

        assert approximate is False

    def test_on_a_modern_target_c_is_the_right_stick_and_z_the_left_trigger(self):
        mappings, _ = _resolved("8bitdo_n64_modkit", guid=MODKIT_GUID)
        xbox = mappings["xbox"]

        assert xbox.axes["right_x"] == AxisBinding(3)
        assert xbox.axes["right_y"] == AxisBinding(4)
        assert xbox.buttons[Button.A] == InputSource(SourceKind.BUTTON, 0)
        assert xbox.buttons[Button.X] == InputSource(SourceKind.BUTTON, 1)
        assert xbox.buttons[Button.LEFT_TRIGGER] == InputSource(SourceKind.BUTTON, 8)
        assert xbox.compile().left_trigger_is_analog is False
        # An N64 pad has no right or top face button to give.
        assert Button.B not in xbox.buttons
        assert Button.Y not in xbox.buttons

    def test_retro_targets_borrow_c_buttons_for_missing_face_buttons(self):
        mappings, _ = _resolved("8bitdo_n64_modkit", guid=MODKIT_GUID)

        # NES: A and B from N64 A and B, which already sit B-left-of-A.
        assert mappings["nes"].buttons[Button.A] == InputSource(SourceKind.BUTTON, 0)
        assert mappings["nes"].buttons[Button.B] == InputSource(SourceKind.BUTTON, 1)
        # SNES A (right face) from C-right, X (top face) from C-up.
        assert mappings["snes"].buttons[Button.B] == InputSource(SourceKind.AXIS, 3, 1)
        assert mappings["snes"].buttons[Button.Y] == InputSource(SourceKind.AXIS, 4, -1)
        # Genesis C from C-right, Y from C-up; X and Z from L and R.
        genesis = mappings["genesis"]
        assert genesis.buttons[Button.B] == InputSource(SourceKind.AXIS, 3, 1)
        assert genesis.buttons[Button.LEFT_BUMPER] == InputSource(SourceKind.AXIS, 4, -1)
        assert genesis.buttons[Button.Y] == InputSource(SourceKind.BUTTON, 6)
        assert genesis.buttons[Button.RIGHT_BUMPER] == InputSource(SourceKind.BUTTON, 7)

    def test_a_gamecube_target_follows_nintendos_own_ports(self):
        """Ocarina of Time's Z-targeting moved to L on the GameCube."""
        mappings, _ = _resolved("8bitdo_n64_modkit", guid=MODKIT_GUID)
        gamecube = mappings["gamecube"]

        assert gamecube.buttons[Button.LEFT_TRIGGER] == InputSource(SourceKind.BUTTON, 8)
        assert gamecube.buttons[Button.RIGHT_TRIGGER] == InputSource(SourceKind.BUTTON, 7)
        assert gamecube.buttons[Button.RIGHT_BUMPER] == InputSource(SourceKind.BUTTON, 6)
        assert gamecube.axes["right_x"] == AxisBinding(3)     # C-stick
        compiled = gamecube.compile()
        assert compiled.left_trigger_is_analog is False
        assert compiled.right_trigger_is_analog is False


# ---------------------------------------------------------------------------
# GameCube
# ---------------------------------------------------------------------------


class TestGameCube:
    def test_the_c_stick_is_the_right_stick(self):
        mappings, _ = resolve(build_preset(FAMILIES_BY_KEY["xbox"]), _device(), _full_pad())
        gamecube = mappings["gamecube"]

        assert gamecube.axes["right_x"] == _full_pad()["axes"]["right_x"]
        assert gamecube.axes["right_y"] == _full_pad()["axes"]["right_y"]

    def test_neither_stick_clicks(self):
        """A binding for a click the pad does not have would still reach the
        console."""
        bits = dict(LAYOUTS_BY_KEY["gamecube"].bindable())

        assert Button.LEFT_STICK not in bits
        assert Button.RIGHT_STICK not in bits

    def test_l_and_r_keep_their_analog_travel(self):
        layout = LAYOUTS_BY_KEY["gamecube"]
        mappings, _ = resolve(build_preset(FAMILIES_BY_KEY["xbox"]), _device(), _full_pad())

        assert layout.has_axis("left_trigger") and layout.has_axis("right_trigger")
        assert mappings["gamecube"].compile().left_trigger_is_analog is True
        assert mappings["gamecube"].compile().right_trigger_is_analog is True

    def test_z_is_the_right_bumper(self):
        assert dict(LAYOUTS_BY_KEY["gamecube"].bindable())[Button.RIGHT_BUMPER] == "Z"
        assert _driver("xbox", "gamecube", "Z") == "RB"

    def test_there_is_no_left_bumper(self):
        assert Button.LEFT_BUMPER not in dict(LAYOUTS_BY_KEY["gamecube"].bindable())


# ---------------------------------------------------------------------------
# Defaults are templates: nothing the player made is overwritten
# ---------------------------------------------------------------------------


class TestUserConfigurationsAreNeverOverwritten:
    def test_the_players_kit_configuration_survives_the_new_builtin(self):
        """The built-in is named differently from the player's own, so both
        exist and theirs is untouched."""
        mine = ControllerConfiguration(
            name="8BitDo DIY Mod Kit - N64", layout="n64",
            mappings={"n64": DeviceMapping(buttons={int(Button.A): InputSource(SourceKind.BUTTON, 5)})},
        )
        store = ConfigurationStore.from_config(SimpleNamespace(configurations=[mine.to_dict()]))

        kept = store.get("8BitDo DIY Mod Kit - N64")
        assert kept.builtin is False
        assert kept.mappings["n64"].buttons[int(Button.A)] == InputSource(SourceKind.BUTTON, 5)
        assert store.get("8BitDo N64 Mod Kit").builtin is True

    def test_a_clashing_name_keeps_the_players_version(self):
        mine = ControllerConfiguration(name="8BitDo N64 Mod Kit", layout="snes")
        store = ConfigurationStore.from_config(SimpleNamespace(configurations=[mine.to_dict()]))

        assert store.get("8BitDo N64 Mod Kit").builtin is False
        assert store.get("8BitDo N64 Mod Kit").layout == "snes"

    def test_a_custom_configuration_is_installed_verbatim(self):
        mapping = DeviceMapping(buttons={int(Button.A): InputSource(SourceKind.BUTTON, 9)})
        mine = ControllerConfiguration(name="Mine", mappings={"n64": mapping})

        mappings, _ = mappings_for(mine, _device(), _full_pad())

        assert mappings["n64"] is mapping

    def test_editing_a_materialised_copy_does_not_touch_the_rule(self):
        builtin = next(e for e in builtin_configurations() if e.family == "xbox")
        copy = materialise(builtin, _device(), _full_pad(), "My pad")
        copy.mappings["n64"].bind_button(int(Button.A), InputSource(SourceKind.BUTTON, 30))

        again, _ = mappings_for(builtin, _device(), _full_pad())

        assert again["n64"].buttons[Button.A] != InputSource(SourceKind.BUTTON, 30)
        assert builtin.mappings == {}

    def test_builtins_are_never_persisted(self):
        config = SimpleNamespace(configurations=[])
        store = ConfigurationStore.from_config(config)

        store.into_config(config)

        assert config.configurations == []


# ---------------------------------------------------------------------------
# Older saved configurations
# ---------------------------------------------------------------------------


#: The N64 mapping from the player's own "8BitDo DIY Mod Kit - N64", exactly as
#: the client saved it before the C buttons had bits of their own.
PLAYERS_SAVED_N64 = {
    "guid": MODKIT_GUID, "name": "8BitDo N64 Modkit",
    "buttons": {
        "1": {"kind": 0, "index": 0, "value": 0},
        "2": {"kind": 0, "index": 1, "value": 0},
        "32768": {"kind": 1, "index": 4, "value": -1},
        "64": {"kind": 1, "index": 3, "value": 1},
        "256": {"kind": 1, "index": 3, "value": -1},
        "1024": {"kind": 1, "index": 4, "value": 1},
        "16": {"kind": 0, "index": 6, "value": 0},
        "32": {"kind": 0, "index": 7, "value": 0},
        "65536": {"kind": 0, "index": 8, "value": 0},
        "128": {"kind": 0, "index": 11, "value": 0},
        "2048": {"kind": 2, "index": 0, "value": 1},
        "4096": {"kind": 2, "index": 0, "value": 4},
        "8192": {"kind": 2, "index": 0, "value": 8},
        "16384": {"kind": 2, "index": 0, "value": 2},
    },
    "buttons_alt": {},
    "axes": {"left_x": {"index": 0, "invert": False}, "left_y": {"index": 1, "invert": False}},
    "key_axes": {},
}


def _saved(mappings: dict, *, guid: str = MODKIT_GUID, **extra) -> dict:
    return {
        "name": "8BitDo DIY Mod Kit - N64", "layout": "n64",
        "device_guid": guid, "device_name": "8BitDo N64 Modkit",
        "mappings": mappings, **extra,
    }


class TestOlderConfigurationsStillLoad:
    def test_the_players_c_buttons_move_to_their_own_bits(self):
        loaded = ControllerConfiguration.from_dict(_saved({"n64": PLAYERS_SAVED_N64}))
        n64 = loaded.mappings["n64"]

        assert n64.buttons[int(Button.C_UP)] == InputSource(SourceKind.AXIS, 4, -1)
        assert n64.buttons[int(Button.C_DOWN)] == InputSource(SourceKind.AXIS, 4, 1)
        assert n64.buttons[int(Button.C_LEFT)] == InputSource(SourceKind.AXIS, 3, -1)
        assert n64.buttons[int(Button.C_RIGHT)] == InputSource(SourceKind.AXIS, 3, 1)
        # Home and Minus are new and unbound; nothing else moved.
        assert int(Button.GUIDE) not in n64.buttons
        assert int(Button.BACK) not in n64.buttons
        assert n64.buttons[int(Button.LEFT_TRIGGER)] == InputSource(SourceKind.BUTTON, 8)
        assert n64.buttons[int(Button.START)] == InputSource(SourceKind.BUTTON, 11)

    def test_it_matches_the_new_builtin_exactly(self):
        """The built-in reproduces what the player bound by hand."""
        loaded = ControllerConfiguration.from_dict(_saved({"n64": PLAYERS_SAVED_N64}))
        builtin, _ = _resolved("8bitdo_n64_modkit", guid=MODKIT_GUID)

        assert loaded.mappings["n64"].buttons == builtin["n64"].buttons
        assert loaded.mappings["n64"].axes == builtin["n64"].axes

    def test_alternates_move_too(self):
        payload = dict(PLAYERS_SAVED_N64, buttons_alt={"256": {"kind": 0, "index": 20, "value": 0}})

        n64 = ControllerConfiguration.from_dict(_saved({"n64": payload})).mappings["n64"]

        assert n64.buttons_alt[int(Button.C_LEFT)] == InputSource(SourceKind.BUTTON, 20)
        assert int(Button.GUIDE) not in n64.buttons_alt

    def test_only_the_n64_type_is_migrated(self):
        """Guide on an Xbox type was always Guide."""
        xbox = {"buttons": {"256": {"kind": 0, "index": 8, "value": 0}}}

        loaded = ControllerConfiguration.from_dict(_saved({"xbox": xbox}))

        assert loaded.mappings["xbox"].buttons[int(Button.GUIDE)] == InputSource(SourceKind.BUTTON, 8)

    def test_a_current_file_is_not_migrated_again(self):
        """Format 2's Guide on an N64 is Home. Reading it as C-left would move
        the player's Home binding onto a C button."""
        n64 = {"buttons": {"256": {"kind": 0, "index": 12, "value": 0}}}

        loaded = ControllerConfiguration.from_dict(_saved({"n64": n64}, format=2))

        assert loaded.mappings["n64"].buttons[int(Button.GUIDE)] == InputSource(SourceKind.BUTTON, 12)

    def test_saving_stamps_the_format(self):
        configuration = ControllerConfiguration(name="Mine")

        assert configuration.to_dict()["format"] == CONFIG_FORMAT == 2
        assert FILE_VERSION == 2

    def test_a_round_trip_is_stable(self):
        loaded = ControllerConfiguration.from_dict(_saved({"n64": PLAYERS_SAVED_N64}))

        again = ControllerConfiguration.from_dict(loaded.to_dict())

        assert again.mappings["n64"].buttons == loaded.mappings["n64"].buttons

    def test_it_says_what_it_moved(self, caplog):
        with caplog.at_level(logging.INFO, logger="client.gui.controller_config"):
            ControllerConfiguration.from_dict(_saved({"n64": PLAYERS_SAVED_N64}))

        assert "moved 4 N64 C-button binding(s)" in caplog.text

    def test_a_version_1_export_imports_and_migrates(self, tmp_path):
        import json

        path = tmp_path / "old.json"
        path.write_text(json.dumps({
            "version": 1, "kind": "rbgc-controller-configurations",
            "configurations": [_saved({"n64": PLAYERS_SAVED_N64})],
        }), encoding="utf-8")
        store = ConfigurationStore()

        added = store.import_from_file(path)

        n64 = store.get(added[0]).mappings["n64"]
        assert int(Button.C_UP) in n64.buttons


#: A Switch Pro Controller as SDL's HIDAPI driver names it: bus, CRC, vendor
#: 057e, product 2009, version, then driver signature 'h'.
PRO_CONTROLLER_HIDAPI = "0500" "1234" "7e05" "0000" "0920" "0000" "0100" "6800"
PRO_CONTROLLER_OTHER_DRIVER = "0500" "1234" "7e05" "0000" "0920" "0000" "0100" "0000"


class TestNintendoPadsReportedByLabel:
    """Configurations bound while SDL2 reported Nintendo pads by label.

    The client now asks for positions, which changes the raw indices a HIDAPI
    Nintendo pad reports. Without re-indexing, a player's saved bindings would
    swap A with B and X with Y on the next launch.
    """

    def _face(self, guid: str) -> DeviceMapping:
        mapping = {"buttons": {
            str(int(Button.A)): {"kind": 0, "index": 1, "value": 0},
            str(int(Button.B)): {"kind": 0, "index": 0, "value": 0},
            str(int(Button.X)): {"kind": 0, "index": 3, "value": 0},
            str(int(Button.LEFT_BUMPER)): {"kind": 0, "index": 9, "value": 0},
        }}
        data = _saved({"xbox": mapping}, guid=guid, layout="xbox")
        return ControllerConfiguration.from_dict(data).mappings["xbox"]

    def test_the_guid_is_read(self):
        assert guid_vendor_product(PRO_CONTROLLER_HIDAPI) == (0x057E, 0x2009, ord("h"))
        assert guid_vendor_product(MODKIT_GUID) == (0x2DC8, 0x2869, 0)
        assert guid_vendor_product("not a guid") is None
        assert guid_vendor_product("rbgc-keyboard") is None

    def test_a_hidapi_pro_controller_is_reindexed(self):
        mapping = self._face(PRO_CONTROLLER_HIDAPI)

        assert mapping.buttons[int(Button.A)] == InputSource(SourceKind.BUTTON, 0)
        assert mapping.buttons[int(Button.B)] == InputSource(SourceKind.BUTTON, 1)
        assert mapping.buttons[int(Button.X)] == InputSource(SourceKind.BUTTON, 2)
        # Not a face-button index: untouched.
        assert mapping.buttons[int(Button.LEFT_BUMPER)] == InputSource(SourceKind.BUTTON, 9)

    def test_another_driver_is_left_alone(self):
        """Only HIDAPI honoured the hint by renumbering its buttons."""
        mapping = self._face(PRO_CONTROLLER_OTHER_DRIVER)

        assert mapping.buttons[int(Button.A)] == InputSource(SourceKind.BUTTON, 1)

    def test_another_vendor_is_left_alone(self):
        mapping = self._face(MODKIT_GUID)

        assert mapping.buttons[int(Button.A)] == InputSource(SourceKind.BUTTON, 1)

    def test_a_current_file_is_left_alone(self):
        mapping = {"buttons": {str(int(Button.A)): {"kind": 0, "index": 1, "value": 0}}}
        data = _saved({"xbox": mapping}, guid=PRO_CONTROLLER_HIDAPI, layout="xbox", format=2)

        loaded = ControllerConfiguration.from_dict(data).mappings["xbox"]

        assert loaded.buttons[int(Button.A)] == InputSource(SourceKind.BUTTON, 1)


# ---------------------------------------------------------------------------
# The input side
# ---------------------------------------------------------------------------


class TestSDLReportsNintendoPadsByPosition:
    @pytest.fixture
    def sdl(self, monkeypatch):
        sdl2 = pytest.importorskip("sdl2", reason="PySDL2 not installed")
        from client.input import sdl2_backend

        hints: dict[bytes, bytes] = {}
        monkeypatch.setattr(sdl2, "SDL_Init", lambda flags: 0)
        monkeypatch.setattr(sdl2, "SDL_SetHint", lambda name, value: hints.__setitem__(name, value))
        monkeypatch.setattr(sdl2, "SDL_GameControllerEventState", lambda state: 0)
        monkeypatch.setattr(sdl2_backend, "_sdl_version", lambda: "SDL test")
        return sdl2_backend, hints

    def test_the_label_hint_is_turned_off(self, sdl, monkeypatch):
        sdl2_backend, hints = sdl
        monkeypatch.delenv("SDL_GAMECONTROLLER_USE_BUTTON_LABELS", raising=False)

        sdl2_backend.SDL2Backend().open()

        import os
        assert os.environ["SDL_GAMECONTROLLER_USE_BUTTON_LABELS"] == "0"
        assert hints[b"SDL_GAMECONTROLLER_USE_BUTTON_LABELS"] == b"0"

    def test_an_explicit_choice_in_the_environment_is_respected(self, sdl, monkeypatch):
        sdl2_backend, hints = sdl
        monkeypatch.setenv("SDL_GAMECONTROLLER_USE_BUTTON_LABELS", "1")

        sdl2_backend.SDL2Backend().open()

        assert hints[b"SDL_GAMECONTROLLER_USE_BUTTON_LABELS"] == b"1"

    def test_the_touchpad_is_a_nameable_control(self, sdl):
        sdl2_backend, _ = sdl
        import sdl2

        sdl2_backend._build_maps()

        if hasattr(sdl2, "SDL_CONTROLLER_BUTTON_TOUCHPAD"):
            assert sdl2_backend._PAD_BUTTON_SDL["touchpad"] == sdl2.SDL_CONTROLLER_BUTTON_TOUCHPAD


class TestAKeyboardCanHoldADigitalTrigger:
    """The N64's Z and ZR on keys. apply_trigger_buttons() recomputes both
    trigger bits from travel on every poll, so a key-held bit needs a full
    pull behind it or it is cleared before it leaves."""

    def _poll(self, held: set[int]) -> ControllerState:
        from client.input.keyboard_backend import KEYBOARD_GUID, KEYBOARD_INSTANCE_ID, KeyboardBackend

        mapping = DeviceMapping(guid=KEYBOARD_GUID, buttons={
            int(Button.LEFT_TRIGGER): InputSource(SourceKind.KEY, 90),    # Z
            int(Button.RIGHT_TRIGGER): InputSource(SourceKind.KEY, 88),   # ZR
        })
        backend = KeyboardBackend(mapping)
        backend.open()
        backend.acquire(KEYBOARD_INSTANCE_ID)
        for key in held:
            backend.set_key(key, True)
        state = ControllerState()
        assert backend.poll(KEYBOARD_INSTANCE_ID, state)
        return state

    def test_z_alone(self):
        state = self._poll({90})

        assert state.buttons & Button.LEFT_TRIGGER
        assert not state.buttons & Button.RIGHT_TRIGGER
        assert state.left_trigger == 255 and state.right_trigger == 0

    def test_zr_alone(self):
        state = self._poll({88})

        assert state.buttons & Button.RIGHT_TRIGGER
        assert not state.buttons & Button.LEFT_TRIGGER

    def test_released(self):
        state = self._poll(set())

        assert not state.buttons & (Button.LEFT_TRIGGER | Button.RIGHT_TRIGGER)
        assert state.left_trigger == state.right_trigger == 0


# ---------------------------------------------------------------------------
# The server side of the C buttons
# ---------------------------------------------------------------------------


class TestEveryProfileSendsCAsTheRightStick:
    def _generic(self, state: ControllerState) -> bytes:
        from server.bt.profiles.generic_gamepad import REPORT_SIZE, GenericGamepadProfile

        buf = bytearray(REPORT_SIZE)
        GenericGamepadProfile().build_input_report(state, buf)
        return bytes(buf)

    def _switch(self, state: ControllerState) -> bytes:
        from server.bt.profiles.switch_pro import REPORT_SIZE, SwitchProProfile

        buf = bytearray(REPORT_SIZE)
        SwitchProProfile().build_input_report(state, buf)
        return bytes(buf)

    @staticmethod
    def _generic_right(report: bytes) -> tuple[int, int]:
        import struct

        return struct.unpack_from("<hh", report, 5)

    @staticmethod
    def _switch_right(report: bytes) -> tuple[int, int]:
        x = report[9] | ((report[10] & 0x0F) << 8)
        y = (report[10] >> 4) | (report[11] << 4)
        return x, y

    @pytest.mark.parametrize("bit,expected", [
        (Button.C_UP, (0, -32768)),
        (Button.C_DOWN, (0, 32767)),
        (Button.C_LEFT, (-32768, 0)),
        (Button.C_RIGHT, (32767, 0)),
    ])
    def test_generic(self, bit, expected):
        report = self._generic(ControllerState(buttons=bit))

        assert self._generic_right(report) == expected
        # And no HID button: C used to be buttons 9/12/13/14 here.
        assert report[11] >> 4 == 0 and report[12] == 0 and report[13] == 0

    def test_generic_without_c_is_the_stick(self):
        report = self._generic(ControllerState(right_x=1234, right_y=-4321))

        assert self._generic_right(report) == (1234, -4321)

    @pytest.mark.parametrize("bit,expected", [
        (Button.C_UP, (2048, 4095)),     # the Switch wants up positive
        (Button.C_DOWN, (2048, 0)),
        (Button.C_LEFT, (0, 2048)),
        (Button.C_RIGHT, (4095, 2048)),
    ])
    def test_switch_pro(self, bit, expected):
        report = self._switch(ControllerState(buttons=bit))

        assert self._switch_right(report) == expected
        # Not Home, not Capture, not Minus: the shared byte stays empty.
        assert report[4] == 0

    def test_n64_home_is_the_switch_home(self):
        report = self._switch(ControllerState(buttons=Button.GUIDE))

        assert report[4] == 0x10

    def test_opposing_c_buttons_agree_across_profiles(self):
        """Right beats left and down beats up, as the measured 8BitDo 64
        profile always resolved them."""
        both = Button.C_LEFT | Button.C_RIGHT | Button.C_UP | Button.C_DOWN

        assert self._generic_right(self._generic(ControllerState(buttons=both))) == (32767, 32767)


# ---------------------------------------------------------------------------
# The generated matrix
# ---------------------------------------------------------------------------


class TestTheMatrixIsCurrent:
    def test_the_committed_matrix_matches_the_rule(self):
        from tools.build_mapping_matrix import OUTPUT, document

        committed = OUTPUT.read_text(encoding="utf-8")

        assert committed == document(), (
            "docs/controller_mapping_matrix.md is stale -- rerun "
            "'python -m tools.build_mapping_matrix'"
        )

    def test_it_covers_every_preset_and_every_target(self):
        from tools.build_mapping_matrix import document

        text = document()
        for layout in LAYOUTS:
            assert f"(`{layout.key}`)" in text
        for family in FAMILIES:
            assert family.name in text

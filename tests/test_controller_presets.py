"""Built-in controller presets, and how they resolve onto a real pad.

The thing worth protecting here is the *indirection*. A preset says "the bottom
face button", never "raw button 0", because a raw index is a property of one
device on one platform. If someone later flattens that into a table of indices
it will work perfectly on the machine they tested and be wrong elsewhere, with
no error anywhere -- the same failure mode CLAUDE.md warns about for
``default_joystick_mapping``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from client.gui.controller_config import (
    ConfigurationStore,
    ControllerConfiguration,
)
from client.gui.controller_layouts import LAYOUTS, LAYOUTS_BY_KEY
from client.gui.controller_presets import (
    FAMILIES,
    FAMILIES_BY_KEY,
    build_preset,
    builtin_configurations,
    mappings_for,
    materialise,
    resolve,
)
from client.input.mapping import AxisBinding, InputSource, SourceKind
from common.state import Button


def _device(**kwargs):
    base = dict(
        guid="pad-guid", name="Test Pad", instance_id=0,
        axis_count=6, button_count=11, hat_count=1,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _sdl_bindings(**overrides) -> dict:
    """A plausible SDL bind table, of the shape ``pad_bindings()`` returns."""
    buttons = {
        "a": InputSource(SourceKind.BUTTON, 0),
        "b": InputSource(SourceKind.BUTTON, 1),
        "x": InputSource(SourceKind.BUTTON, 2),
        "y": InputSource(SourceKind.BUTTON, 3),
        "lb": InputSource(SourceKind.BUTTON, 9),
        "rb": InputSource(SourceKind.BUTTON, 10),
        "back": InputSource(SourceKind.BUTTON, 4),
        "start": InputSource(SourceKind.BUTTON, 6),
        "guide": InputSource(SourceKind.BUTTON, 5),
        "lstick": InputSource(SourceKind.BUTTON, 7),
        "rstick": InputSource(SourceKind.BUTTON, 8),
        # Capture/Share/Mute. Present on Switch Pro, DualSense and Xbox Series
        # pads; absent on a 360, which is what the omission test removes.
        "misc1": InputSource(SourceKind.BUTTON, 11),
        "dpad_up": InputSource(SourceKind.HAT, 0, 0x01),
        "dpad_down": InputSource(SourceKind.HAT, 0, 0x04),
        "dpad_left": InputSource(SourceKind.HAT, 0, 0x08),
        "dpad_right": InputSource(SourceKind.HAT, 0, 0x02),
        # Triggers are offered as digital sources too, exactly as
        # pad_bindings() does: the pressed half of the analog axis.
        "left_trigger": InputSource(SourceKind.AXIS, 4, 1),
        "right_trigger": InputSource(SourceKind.AXIS, 5, 1),
    }
    axes = {
        "left_x": AxisBinding(0), "left_y": AxisBinding(1),
        "right_x": AxisBinding(2), "right_y": AxisBinding(3),
        "left_trigger": AxisBinding(4), "right_trigger": AxisBinding(5),
    }
    buttons.update(overrides.pop("buttons", {}))
    axes.update(overrides.pop("axes", {}))
    return {"buttons": buttons, "axes": axes}


class TestCoverage:
    def test_every_family_covers_every_controller_type(self):
        for family in FAMILIES:
            covered = {entry.layout for entry in build_preset(family)}
            assert covered == {layout.key for layout in LAYOUTS}, family.key

    def test_every_bindable_button_gets_a_source(self):
        """No layout button may be left without one, for any family.

        Whether the pad *has* that control is decided at resolution, from the
        real device. Pruning here as well used to drop the N64 kit's C cluster
        and analog stick because the family was written down as stickless.
        """
        for family in FAMILIES:
            for entry in build_preset(family):
                layout = LAYOUTS_BY_KEY[entry.layout]
                missing = [
                    label for bit, label in layout.bindable()
                    if bit not in entry.buttons
                ]

                assert not missing, f"{family.key}/{entry.layout}: {missing}"

    def test_triggers_are_bound_through_their_own_control(self):
        """Bound, but never to an unrelated button.

        apply_trigger_buttons() rewrites both bits from the analog values every
        poll, so the source has to be the trigger itself or the binding would
        look correct and never fire.
        """
        for family in FAMILIES:
            for entry in build_preset(family):
                for bit, control in entry.buttons.items():
                    if bit in (Button.LEFT_TRIGGER, Button.RIGHT_TRIGGER):
                        assert control == Button(bit).name.lower(), (
                            f"{family.key}/{entry.layout}: {control!r}"
                        )

    def test_an_analog_trigger_binds_its_axis(self):
        xbox = next(
            e for e in build_preset(FAMILIES_BY_KEY["xbox"]) if e.layout == "xbox"
        )

        assert "left_trigger" in xbox.axes
        assert xbox.buttons[Button.LEFT_TRIGGER] == "left_trigger"

    def test_a_digital_trigger_binds_only_its_bit(self):
        """The N64's Z is a switch: no axis, so nothing to report travel on."""
        n64 = next(
            e for e in build_preset(FAMILIES_BY_KEY["xbox"]) if e.layout == "n64"
        )

        assert "left_trigger" not in n64.axes
        assert n64.buttons[Button.LEFT_TRIGGER] == "left_trigger"

    def test_stickless_layouts_bind_no_axes(self):
        """An SNES pad has no analog travel to report, whatever drives it."""
        for family in FAMILIES:
            for entry in build_preset(family):
                if entry.layout in ("snes", "nes", "genesis"):
                    assert entry.axes == (), f"{family.key}/{entry.layout}"


class TestSameControllerMapsOneToOne:
    """A pad driving its own layout must bind every single control.

    This is the user-facing check: choosing "Xbox Controller" and then the Xbox
    type should leave nothing blank in the mapping screen, because the input and
    the target are the same hardware. Anything unbound there is a bug in the
    preset, not a property of the pad.
    """

    #: Which controller type each family's own hardware is.
    SAME_SHAPE = {
        "xbox": "xbox",
        "playstation": "ps5",
        "switch_pro": "switch",
        "8bitdo_ultimate": "xbox",
        "8bitdo_bluetooth": "xbox",
        "generic_usb": "xbox",
    }

    @pytest.mark.parametrize("family_key,layout_key", sorted(SAME_SHAPE.items()))
    def test_symbolic_preset_is_complete(self, family_key, layout_key):
        entry = next(
            e for e in build_preset(FAMILIES_BY_KEY[family_key])
            if e.layout == layout_key
        )
        layout = LAYOUTS_BY_KEY[layout_key]

        missing = [lbl for bit, lbl in layout.bindable() if bit not in entry.buttons]

        assert not missing

    @pytest.mark.parametrize("family_key,layout_key", sorted(SAME_SHAPE.items()))
    def test_resolves_completely_against_a_full_pad(self, family_key, layout_key):
        """The end-to-end version: nothing blank after resolution either."""
        mappings, _ = resolve(
            build_preset(FAMILIES_BY_KEY[family_key]), _device(), _sdl_bindings()
        )
        layout = LAYOUTS_BY_KEY[layout_key]
        bound = mappings[layout_key].buttons

        missing = [lbl for bit, lbl in layout.bindable() if bit not in bound]

        assert not missing, f"{family_key} -> {layout_key}: {missing}"

    def test_an_n64_shaped_pad_covers_the_n64_completely(self):
        """An 8BitDo 64 reports its C cluster as a right stick, so a mod kit in
        an N64 shell has every control the N64 layout asks for."""
        mappings, _ = resolve(
            build_preset(FAMILIES_BY_KEY["8bitdo_diy"]), _device(), _sdl_bindings()
        )
        layout = LAYOUTS_BY_KEY["n64"]
        bound = mappings["n64"].buttons

        missing = [lbl for bit, lbl in layout.bindable() if bit not in bound]

        assert not missing, missing
        assert mappings["n64"].axes.keys() >= {"left_x", "left_y"}

    def test_retro_shells_cover_their_own_system(self):
        for layout_key in ("nes", "snes", "genesis"):
            mappings, _ = resolve(
                build_preset(FAMILIES_BY_KEY["8bitdo_diy"]), _device(), _sdl_bindings()
            )
            layout = LAYOUTS_BY_KEY[layout_key]
            bound = mappings[layout_key].buttons

            missing = [lbl for bit, lbl in layout.bindable() if bit not in bound]

            assert not missing, f"{layout_key}: {missing}"


class TestN64CCluster:
    def _n64(self, family_key="xbox"):
        return next(
            e for e in build_preset(FAMILIES_BY_KEY[family_key]) if e.layout == "n64"
        )

    def test_c_buttons_follow_the_right_stick(self):
        buttons = self._n64().buttons

        assert buttons[Button.CAPTURE] == "right_y-"       # C up
        assert buttons[Button.RIGHT_STICK] == "right_y+"   # C down
        assert buttons[Button.GUIDE] == "right_x-"         # C left
        assert buttons[Button.BACK] == "right_x+"          # C right

    def test_face_buttons_are_independent_of_the_c_cluster(self):
        """The whole point of giving C its own bits."""
        buttons = self._n64().buttons

        assert buttons[Button.A] == "a"
        assert buttons[Button.B] == "b"
        assert len(set(buttons.values())) == len(buttons), "two bits share a control"

    def test_a_pad_without_a_right_stick_leaves_c_unbound(self):
        """Decided by the *device*, not by how the family was described.

        The preset always asks for the C cluster; resolution drops it when the
        pad in hand has no right stick to read it from.
        """
        bindings = _sdl_bindings()
        del bindings["axes"]["right_x"]
        del bindings["axes"]["right_y"]

        mappings, _ = resolve(
            build_preset(FAMILIES_BY_KEY["8bitdo_diy"]), _device(), bindings
        )

        for bit in (Button.CAPTURE, Button.RIGHT_STICK, Button.GUIDE, Button.BACK):
            assert bit not in mappings["n64"].buttons
        # The rest of the N64 still works.
        assert mappings["n64"].buttons[Button.A] == InputSource(SourceKind.BUTTON, 0)

    def test_the_right_stick_is_a_source_but_not_an_output(self):
        """The N64 has no right stick, so nothing should report one."""
        entry = self._n64()

        assert "right_x" not in entry.axes
        assert "right_y" not in entry.axes


class TestResolve:
    def test_sdl_bindings_are_used_verbatim(self):
        mappings, approximate = resolve(
            build_preset(FAMILIES_BY_KEY["xbox"]), _device(), _sdl_bindings()
        )

        assert approximate is False
        xbox = mappings["xbox"]
        assert xbox.buttons[Button.A] == InputSource(SourceKind.BUTTON, 0)
        assert xbox.buttons[Button.LEFT_BUMPER] == InputSource(SourceKind.BUTTON, 9)
        assert xbox.axes["left_x"] == AxisBinding(0)

    def test_hat_bindings_survive(self):
        mappings, _ = resolve(
            build_preset(FAMILIES_BY_KEY["xbox"]), _device(), _sdl_bindings()
        )

        assert mappings["nes"].buttons[Button.DPAD_UP] == InputSource(
            SourceKind.HAT, 0, 0x01
        )

    def test_a_control_the_pad_lacks_is_omitted_not_guessed(self):
        """BINDTYPE_NONE arrives as a missing key. Inventing one would be worse."""
        bindings = _sdl_bindings()
        del bindings["buttons"]["guide"]

        mappings, _ = resolve(
            build_preset(FAMILIES_BY_KEY["xbox"]), _device(), bindings
        )

        assert Button.GUIDE not in mappings["xbox"].buttons

    def test_axis_halves_become_thresholded_sources(self):
        mappings, _ = resolve(
            build_preset(FAMILIES_BY_KEY["xbox"]), _device(), _sdl_bindings()
        )
        n64 = mappings["n64"]

        # C right is the positive half of the right stick's X axis (index 2).
        assert n64.buttons[Button.BACK] == InputSource(SourceKind.AXIS, 2, 1)
        assert n64.buttons[Button.GUIDE] == InputSource(SourceKind.AXIS, 2, -1)

    def test_an_inverted_axis_flips_which_half_counts(self):
        bindings = _sdl_bindings(axes={"right_x": AxisBinding(2, invert=True)})

        mappings, _ = resolve(
            build_preset(FAMILIES_BY_KEY["xbox"]), _device(), bindings
        )

        assert mappings["n64"].buttons[Button.BACK] == InputSource(
            SourceKind.AXIS, 2, -1
        )

    def test_no_sdl_entry_falls_back_and_says_so(self):
        """A guess that looks authoritative is worse than one that admits it."""
        mappings, approximate = resolve(
            build_preset(FAMILIES_BY_KEY["generic_usb"]), _device(), None
        )

        assert approximate is True
        assert mappings["xbox"].buttons[Button.A] == InputSource(SourceKind.BUTTON, 0)

    def test_a_pad_reporting_no_controls_yields_empty_mappings(self):
        """Nothing to bind is not a crash -- the slot stays selectable."""
        device = _device(axis_count=0, button_count=0, hat_count=0)

        mappings, approximate = resolve(
            build_preset(FAMILIES_BY_KEY["generic_usb"]), device, None
        )

        assert approximate is True
        assert all(m.is_empty() for m in mappings.values())

    def test_resolution_is_not_a_table_of_raw_indices(self):
        """The same preset on two pads must produce different indices.

        This is the property the whole design exists for: if someone replaces
        the resolver with a fixed table, this fails.
        """
        preset = build_preset(FAMILIES_BY_KEY["xbox"])

        first, _ = resolve(preset, _device(), _sdl_bindings())
        second, _ = resolve(
            preset, _device(),
            _sdl_bindings(buttons={"a": InputSource(SourceKind.BUTTON, 4)}),
        )

        assert first["xbox"].buttons[Button.A].index == 0
        assert second["xbox"].buttons[Button.A].index == 4


class TestBuiltins:
    def test_seven_presets_ship(self):
        assert len(builtin_configurations()) == len(FAMILIES) == 7

    def test_builtins_carry_no_bindings(self):
        """They are markers: a preset is symbolic until it meets a device."""
        for entry in builtin_configurations():
            assert entry.builtin is True
            assert entry.family
            assert entry.mappings == {}

    def test_builtins_report_every_controller_type(self):
        entry = builtin_configurations()[0]

        assert set(entry.configured_layouts()) == {layout.key for layout in LAYOUTS}

    def test_mappings_for_resolves_a_builtin_against_the_device(self):
        entry = next(e for e in builtin_configurations() if e.family == "xbox")

        mappings, approximate = mappings_for(entry, _device(), _sdl_bindings())

        assert approximate is False
        assert mappings["n64"].buttons[Button.A] == InputSource(SourceKind.BUTTON, 0)

    def test_mappings_for_passes_an_ordinary_configuration_through(self):
        entry = ControllerConfiguration(name="Mine", mappings={"xbox": "sentinel"})

        mappings, approximate = mappings_for(entry, _device(), _sdl_bindings())

        assert mappings == {"xbox": "sentinel"}
        assert approximate is False

    def test_an_unknown_family_does_not_raise(self):
        entry = ControllerConfiguration(name="Odd", builtin=True, family="nonexistent")

        mappings, approximate = mappings_for(entry, _device(), _sdl_bindings())

        assert mappings == {}
        assert approximate is False

    def test_materialise_produces_an_editable_copy(self):
        entry = next(e for e in builtin_configurations() if e.family == "xbox")

        copy = materialise(entry, _device(), _sdl_bindings(), "My Xbox Pad")

        assert copy.builtin is False
        assert copy.name == "My Xbox Pad"
        assert copy.device_guid == "pad-guid"
        assert copy.mappings["n64"].buttons[Button.A] == InputSource(
            SourceKind.BUTTON, 0
        )

    def test_copy_as_never_inherits_builtin(self):
        """Otherwise editing a copy could destroy the shipped preset."""
        entry = builtin_configurations()[0]

        assert entry.copy_as("Mine").builtin is False


class TestSeeding:
    def test_a_fresh_store_gains_every_preset(self):
        config = SimpleNamespace(configurations=[])

        store = ConfigurationStore.from_config(config)

        assert {e.name for e in store} == {f.name for f in FAMILIES}

    def test_builtins_are_not_written_to_disk(self):
        """They are re-seeded on load, so persisting them would only let a
        stale copy outlive an improved one."""
        config = SimpleNamespace(configurations=[])
        store = ConfigurationStore.from_config(config)
        store.upsert(ControllerConfiguration(name="Mine"))

        store.into_config(config)

        assert [entry["name"] for entry in config.configurations] == ["Mine"]

    def test_a_deleted_preset_comes_back(self):
        config = SimpleNamespace(configurations=[])
        store = ConfigurationStore.from_config(config)
        store.remove("Xbox Controller")
        store.into_config(config)

        restored = ConfigurationStore.from_config(config)

        assert restored.get("Xbox Controller") is not None

    def test_a_user_configuration_keeps_a_clashing_name(self):
        """Theirs is the one they edited; ours must not overwrite it."""
        mine = ControllerConfiguration(name="Xbox Controller", layout="snes")
        config = SimpleNamespace(configurations=[mine.to_dict()])

        store = ConfigurationStore.from_config(config)
        entry = store.get("Xbox Controller")

        assert entry.builtin is False
        assert entry.layout == "snes"
        assert len(store) == len(FAMILIES)


class TestGeneratedFiles:
    """The committed JSON must match the rule that produced it.

    The files are documentation, not runtime data, so a stale one misleads a
    reader rather than breaking the client -- which is exactly the kind of rot
    that goes unnoticed without a test.
    """

    def _dir(self) -> Path:
        return (
            Path(__file__).resolve().parent.parent
            / "client" / "gui" / "assets" / "presets"
        )

    def test_one_file_per_family(self):
        names = {path.stem for path in self._dir().glob("*.json")}

        assert names == {family.key for family in FAMILIES}

    def test_files_match_the_current_rule(self):
        from tools.build_controller_presets import preset_document

        for family in FAMILIES:
            path = self._dir() / f"{family.key}.json"
            committed = json.loads(path.read_text(encoding="utf-8"))

            assert committed == preset_document(family), (
                f"{path.name} is stale -- rerun "
                f"'python -m tools.build_controller_presets'"
            )

"""Client config: the backend override, migrations, and configuration storage.

The migration tests exist because of a real incident. Suggesting
``--backend synthetic`` for a one-off test wrote that value into the saved
config, and every launch afterwards used fabricated controllers and showed no
real gamepads -- which the user experienced as "my controller isn't detected",
not as "a setting is stuck". A transient CLI flag must never change saved state.
"""

from __future__ import annotations

import json

import pytest

from client.config import ClientConfig, load, save
from client.gui.controller_config import (
    ConfigurationStore,
    ControllerConfiguration,
)
from client.input.base import DeviceInfo
from client.input.mapping import DeviceMapping, InputSource, SourceKind
from common.state import Button


class TestBackendOverride:
    def test_override_wins_for_this_run(self):
        config = ClientConfig(input_backend="auto", backend_override="synthetic")

        assert config.effective_backend() == "synthetic"

    def test_stored_preference_used_without_an_override(self):
        config = ClientConfig(input_backend="auto")

        assert config.effective_backend() == "auto"

    def test_override_is_never_persisted(self, tmp_path):
        """The whole point: one test run must not change saved settings."""
        path = tmp_path / "client.json"
        save(ClientConfig(input_backend="auto", backend_override="synthetic"), path)

        raw = json.loads(path.read_text(encoding="utf-8"))

        assert "backend_override" not in raw
        assert raw["input_backend"] == "auto"

    def test_reloading_keeps_the_real_preference(self, tmp_path):
        path = tmp_path / "client.json"
        save(ClientConfig(input_backend="auto", backend_override="synthetic"), path)

        assert load(path).effective_backend() == "auto"


class TestMigration:
    def _write(self, tmp_path, payload: dict):
        path = tmp_path / "client.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_stored_synthetic_backend_is_repaired(self, tmp_path):
        path = self._write(tmp_path, {"input_backend": "synthetic"})

        assert load(path).input_backend == "auto"

    def test_synthetic_controller_entries_are_cleared(self, tmp_path):
        path = self._write(tmp_path, {
            "input_backend": "synthetic",
            "controllers": [
                {"slot": 0, "guid": "synthetic-0000",
                 "device_name": "Synthetic Controller 0", "enabled": True},
                {"slot": 1, "guid": "0300f094c82d0000",
                 "device_name": "8BitDo 64", "enabled": True},
            ],
        })

        config = load(path)

        assert config.controllers[0].guid == ""
        assert config.controllers[0].device_name == ""
        # A real device must be left alone.
        assert config.controllers[1].device_name == "8BitDo 64"

    def test_a_real_backend_preference_is_respected(self, tmp_path):
        path = self._write(tmp_path, {"input_backend": "sdl2"})

        assert load(path).input_backend == "sdl2"

    def test_per_slot_fields_round_trip(self, tmp_path):
        path = tmp_path / "client.json"
        config = ClientConfig()
        config.controller(1).rumble_enabled = False
        config.controller(1).configuration = "8BitDo — N64"
        save(config, path)

        restored = load(path)

        assert restored.controller(1).rumble_enabled is False
        assert restored.controller(1).configuration == "8BitDo — N64"


class TestDeviceNotes:
    def test_loopback_device_is_flagged(self):
        """Our own emulated pad, paired back to this PC, is a feedback loop."""
        device = DeviceInfo(
            instance_id=0, name="RBGC Gamepad 2", guid="g", is_loopback=True
        )

        assert "do not use" in device.status_note()

    def test_loopback_outranks_needs_mapping(self):
        device = DeviceInfo(
            instance_id=0, name="RBGC Gamepad 2", guid="g",
            is_loopback=True, is_mapped=False,
        )

        assert "do not use" in device.status_note()


class TestConfigurationStore:
    def _configuration(self, name="Pad — N64", layout="n64") -> ControllerConfiguration:
        mapping = DeviceMapping(guid="pad-guid", name="Pad")
        mapping.bind_button(Button.A, InputSource(SourceKind.BUTTON, 2))
        return ControllerConfiguration(
            name=name, layout=layout, mappings={layout: mapping},
            device_guid="pad-guid",
        )

    def test_round_trips_through_config(self):
        store = ConfigurationStore([self._configuration()])
        config = ClientConfig()
        store.into_config(config)

        restored = ConfigurationStore.from_config(config)
        entry = restored.get("Pad — N64")

        assert entry is not None
        assert entry.layout == "n64"
        assert entry.mapping.buttons[Button.A] == InputSource(SourceKind.BUTTON, 2)

    def test_upsert_replaces_by_name(self):
        store = ConfigurationStore([self._configuration()])
        store.upsert(self._configuration(layout="snes"))

        assert len(store) == 1
        assert store.get("Pad — N64").layout == "snes"

    def test_unique_name_avoids_collisions(self):
        store = ConfigurationStore([self._configuration()])

        assert store.unique_name("Pad — N64") == "Pad — N64 (2)"
        assert store.unique_name("Something else") == "Something else"

    def test_for_device_puts_matching_pads_first(self):
        mine = self._configuration(name="Mine")
        theirs = self._configuration(name="Theirs")
        theirs.device_guid = "other-guid"
        store = ConfigurationStore([theirs, mine])

        assert [e.name for e in store.for_device("pad-guid")] == ["Mine", "Theirs"]

    def test_unreadable_entry_does_not_break_loading(self):
        config = ClientConfig()
        config.configurations = [{"totally": "wrong"}, self._configuration().to_dict()]

        store = ConfigurationStore.from_config(config)

        # The bad entry degrades to a named default rather than taking the
        # good one down with it.
        assert store.get("Pad — N64") is not None

    def test_export_import_round_trip(self, tmp_path):
        store = ConfigurationStore([self._configuration()])
        path = tmp_path / "controls.json"

        assert store.export_to_file(path) == 1

        empty = ConfigurationStore()
        added = empty.import_from_file(path)

        assert added == ["Pad — N64"]
        assert empty.get("Pad — N64").layout == "n64"

    def test_import_does_not_overwrite_a_local_configuration(self, tmp_path):
        """Losing working local bindings to an import would be a nasty surprise."""
        store = ConfigurationStore([self._configuration()])
        path = tmp_path / "controls.json"
        store.export_to_file(path)

        added = store.import_from_file(path)

        assert added == ["Pad — N64 (2)"]
        assert len(store) == 2

    def test_rejects_a_foreign_file(self, tmp_path):
        path = tmp_path / "other.json"
        path.write_text(json.dumps({"kind": "something-else"}), encoding="utf-8")

        with pytest.raises(ValueError):
            ConfigurationStore().import_from_file(path)

    def test_rejects_a_newer_format(self, tmp_path):
        path = tmp_path / "future.json"
        path.write_text(
            json.dumps({
                "kind": "rbgc-controller-configurations",
                "version": 999,
                "configurations": [],
            }),
            encoding="utf-8",
        )

        with pytest.raises(ValueError):
            ConfigurationStore().import_from_file(path)


class TestControllerLayouts:
    """The layouts are now metadata over SVG art, not geometry.

    Geometry lives in client/gui/assets/controllers/*.svg. What Python owns is
    the mapping from SVG element ids to logical buttons, so these check that
    contract rather than coordinates.
    """

    def test_every_element_exists_in_its_artwork(self):
        """A typo in an element id silently disables that button's highlight."""
        import xml.etree.ElementTree as ET

        from client.gui.assets import assets_dir
        from client.gui.controller_layouts import LAYOUTS

        for layout in LAYOUTS:
            path = assets_dir() / "controllers" / layout.svg
            assert path.exists(), f"{layout.key}: missing {layout.svg}"

            ids = {
                element.get("id")
                for element in ET.parse(path).iter()
                if element.get("id")
            }
            for control in layout.controls:
                assert control.element in ids, (
                    f"{layout.key}: {control.element} is not in {layout.svg}"
                )

    def test_artwork_is_well_formed(self):
        """A double hyphen in an XML comment silently invalidates the whole file."""
        import xml.etree.ElementTree as ET

        from client.gui.assets import assets_dir
        from client.gui.controller_layouts import LAYOUTS

        for layout in LAYOUTS:
            ET.parse(assets_dir() / "controllers" / layout.svg)

    def test_bindable_sets_match_the_hardware(self):
        """Each type offers exactly the buttons it has."""
        from client.gui.controller_layouts import LAYOUTS_BY_KEY
        from common.state import Button

        nes = LAYOUTS_BY_KEY["nes"].bindable()
        bits = {bit for bit, _ in nes}
        assert Button.A in bits and Button.B in bits
        # An NES pad has no shoulders, no sticks and no X/Y.
        assert Button.LEFT_BUMPER not in bits
        assert Button.LEFT_STICK not in bits
        assert Button.X not in bits

        xbox = {bit for bit, _ in LAYOUTS_BY_KEY["xbox"].bindable()}
        assert Button.LEFT_BUMPER in xbox and Button.RIGHT_STICK in xbox

    def test_n64_c_buttons_have_their_own_bits(self):
        """A and C down must be independently usable.

        They used to share ``Button.A``, which made a preset unable to drive
        both -- pushing the C stick down was indistinguishable from pressing A.
        The C cluster now borrows four bits the N64 has no other use for.
        """
        from client.gui.controller_layouts import LAYOUTS_BY_KEY
        from common.state import Button

        bindable = LAYOUTS_BY_KEY["n64"].bindable()
        bits = [bit for bit, _ in bindable]
        labels = dict(bindable)

        assert len(bits) == len(set(bits)), "a logical button is listed twice"

        assert labels[Button.A] == "A"
        assert labels[Button.B] == "B"
        assert labels[Button.RIGHT_STICK] == "C down"
        assert labels[Button.BACK] == "C right"
        assert labels[Button.CAPTURE] == "C up"
        assert labels[Button.GUIDE] == "C left"

    def test_n64_c_buttons_avoid_the_trigger_bits(self):
        """apply_trigger_buttons() rewrites both trigger bits on every poll.

        Anything else bound there is erased between polls, so the C cluster
        must not use them.
        """
        from client.gui.controller_layouts import LAYOUTS_BY_KEY
        from common.state import Button

        c_bits = {
            control.button
            for control in LAYOUTS_BY_KEY["n64"].controls
            if control.label.startswith("C ")
        }

        assert Button.LEFT_TRIGGER not in c_bits
        assert Button.RIGHT_TRIGGER not in c_bits

    def test_axes_are_only_offered_where_they_exist(self):
        from client.gui.controller_layouts import LAYOUTS_BY_KEY

        assert LAYOUTS_BY_KEY["xbox"].has_axis("right_x")
        assert not LAYOUTS_BY_KEY["snes"].has_axis("left_x")
        assert LAYOUTS_BY_KEY["n64"].has_axis("left_x")
        # The N64 has one stick, so no right-stick axes.
        assert not LAYOUTS_BY_KEY["n64"].has_axis("right_x")

    def test_every_layout_has_a_note(self):
        """The note explains that system's naming; a blank one is an omission."""
        from client.gui.controller_layouts import LAYOUTS

        for layout in LAYOUTS:
            assert layout.note.strip(), f"{layout.key} has no note"

    def test_colours_are_parseable(self):
        """A typo like '#f5b942x' renders as black and is easy to miss."""
        from PySide6.QtGui import QColor

        from client.gui.controller_layouts import LAYOUTS

        for layout in LAYOUTS:
            assert QColor(layout.lit).isValid(), f"{layout.key}.lit = {layout.lit!r}"



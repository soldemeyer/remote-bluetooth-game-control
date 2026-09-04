"""The client's controller table.

Two classes of bug live here, and both are silent.

**Column indices.** Every widget in a row is addressed by column number. The
status column has moved twice; each time a surviving literal pointed at a cell
that holds a *widget*, where ``QTableWidget.item()`` returns None and writing
text does nothing at all -- no exception, no log line, just a status that never
updates. The constants are checked against the actual cell contents here.

**Shared configurations.** Slots reference a configuration by name, so two slots
can hold the same one. The active controller type therefore cannot live on the
configuration; if it does, changing one slot's type silently changes the other's.

These run offscreen and never touch the real config file -- ``save`` is patched
out, because MainWindow writes to ``%APPDATA%/rbgc/client.json`` on nearly every
interaction and a test run must not overwrite a player's setup.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6", reason="client GUI extras not installed")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (  # noqa: E402
    QCheckBox, QComboBox, QLineEdit, QPushButton, QTableWidgetItem,
)

from client import config as client_config  # noqa: E402
from client.gui import app as gui_app  # noqa: E402
from client.gui.app import MainWindow  # noqa: E402
from client.gui.panels import (  # noqa: E402
    COL_CONFIG, COL_CONFIGURE, COL_COUNT, COL_GAMEPAD, COL_NAME,
    COL_RUMBLE, COL_SLOT, COL_STATUS, COL_TYPE, COL_USE,
)
from client.gui.controller_layouts import LAYOUTS  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, monkeypatch, tmp_path):
    """A live MainWindow that cannot reach the user's real config.

    ``real_save`` is captured *before* patching: re-importing it later would
    just pick the patched attribute back up, and the test would silently assert
    against a file nobody wrote.
    """
    real_save = client_config.save
    written: list = []
    monkeypatch.setattr(
        client_config, "save", lambda config, path=None: written.append(config)
    )

    config = client_config.ClientConfig(backend_override="synthetic")
    win = MainWindow(config)
    win.saved = written
    win.real_save = real_save
    try:
        yield win
    finally:
        win.close()


def _unwrap(widget):
    """Cell widgets that are centred sit inside a wrapper layout."""
    if widget is None:
        return None
    for kind in (QCheckBox, QComboBox, QLineEdit, QPushButton):
        if isinstance(widget, kind):
            return widget
    children = widget.findChildren(QCheckBox) + widget.findChildren(QComboBox)
    return children[0] if children else widget


class TestColumns:
    def test_header_matches_the_constants(self, window):
        table = window._controllers.table

        assert table.columnCount() == COL_COUNT
        assert [
            table.horizontalHeaderItem(c).text() for c in range(COL_COUNT)
        ] == [
            "Use", "Slot", "Player name", "Gamepad",
            "Configuration", "Controller type", "", "Rumble", "Status",
        ]

    @pytest.mark.parametrize(
        "column,kind",
        [
            (COL_USE, QCheckBox),
            (COL_NAME, QLineEdit),
            (COL_GAMEPAD, QComboBox),
            (COL_CONFIG, QComboBox),
            (COL_TYPE, QComboBox),
            (COL_CONFIGURE, QPushButton),
            (COL_RUMBLE, QCheckBox),
        ],
    )
    def test_each_widget_column_holds_what_its_constant_says(
        self, window, column, kind
    ):
        widget = _unwrap(window._controllers.table.cellWidget(0, column))

        assert isinstance(widget, kind), f"column {column} holds {type(widget)}"

    @pytest.mark.parametrize("column", [COL_SLOT, COL_STATUS])
    def test_text_columns_are_items_not_widgets(self, window, column):
        """Writing text to a cell that holds a widget fails silently.

        That is exactly how the status column stopped updating twice.
        """
        assert window._controllers.table.cellWidget(0, column) is None
        assert isinstance(window._controllers.table.item(0, column), QTableWidgetItem)

    def test_status_text_is_reachable(self, window):
        item = window._controllers.table.item(0, COL_STATUS)
        item.setText("streaming")

        assert window._controllers.table.item(0, COL_STATUS).text() == "streaming"


class TestControllerType:
    def test_every_layout_is_offered(self, window):
        combo = window._controllers.type_combos[0]

        assert [combo.itemData(i) for i in range(combo.count())] == [
            layout.key for layout in LAYOUTS
        ]

    def test_the_seven_presets_are_selectable(self, window):
        combo = window._controllers.config_combos[0]
        labels = [combo.itemText(i) for i in range(combo.count())]

        assert labels[0] == "Default for this gamepad"
        assert "Xbox Controller (built-in)" in labels

    def test_unconfigured_types_say_so(self, window):
        """An empty type must not look identical to a working one."""
        combo = window._controllers.type_combos[0]

        assert all(
            "(not configured)" in combo.itemText(i) for i in range(combo.count())
        )

    def test_a_builtin_configures_every_type(self, window):
        config_combo = window._controllers.config_combos[0]
        config_combo.setCurrentIndex(config_combo.findData("Xbox Controller"))

        type_combo = window._controllers.type_combos[0]

        assert not any(
            "(not configured)" in type_combo.itemText(i)
            for i in range(type_combo.count())
        )

    def test_selecting_a_type_records_it_on_the_slot(self, window):
        combo = window._controllers.type_combos[2]
        combo.setCurrentIndex(combo.findData("n64"))

        assert window._config.controller(2).layout == "n64"

    def test_two_slots_can_share_a_configuration_with_different_types(self, window):
        """The reason the type is per slot and not on the configuration."""
        for row in (0, 1):
            combo = window._controllers.config_combos[row]
            combo.setCurrentIndex(combo.findData("Xbox Controller"))

        window._controllers.type_combos[0].setCurrentIndex(
            window._controllers.type_combos[0].findData("n64")
        )
        window._controllers.type_combos[1].setCurrentIndex(
            window._controllers.type_combos[1].findData("snes")
        )

        assert window._config.controller(0).configuration == "Xbox Controller"
        assert window._config.controller(1).configuration == "Xbox Controller"
        assert window._config.controller(0).layout == "n64"
        assert window._config.controller(1).layout == "snes"

    def test_changing_one_slots_type_leaves_the_other_alone(self, window):
        for row in (0, 1):
            combo = window._controllers.config_combos[row]
            combo.setCurrentIndex(combo.findData("Xbox Controller"))
        window._controllers.type_combos[0].setCurrentIndex(
            window._controllers.type_combos[0].findData("n64")
        )

        window._controllers.type_combos[1].setCurrentIndex(
            window._controllers.type_combos[1].findData("genesis")
        )

        assert window._config.controller(0).layout == "n64"

    def test_slot_layout_falls_back_to_the_configuration(self, window):
        window._config.controller(3).layout = ""
        window._config.controller(3).configuration = "Xbox Controller"

        assert window._slot_layout(3) == LAYOUTS[0].key


class TestPersistence:
    def test_per_slot_type_round_trips(self, window, tmp_path):
        combo = window._controllers.config_combos[1]
        combo.setCurrentIndex(combo.findData("PlayStation Controller"))
        window._controllers.type_combos[1].setCurrentIndex(
            window._controllers.type_combos[1].findData("switch")
        )

        path = tmp_path / "client.json"
        window.real_save(window._config, path)
        restored = client_config.load(path)

        assert restored.controller(1).configuration == "PlayStation Controller"
        assert restored.controller(1).layout == "switch"

    def test_builtins_are_not_persisted(self, window, tmp_path):
        path = tmp_path / "client.json"
        window._configurations.into_config(window._config)
        window.real_save(window._config, path)

        assert client_config.load(path).configurations == []

    def test_the_real_config_path_is_never_written(self, window):
        """Guards the fixture itself: a leak here overwrites a player's setup."""
        combo = window._controllers.config_combos[0]
        combo.setCurrentIndex(combo.findData("Xbox Controller"))

        assert window.saved, "save() was expected to be called"
        assert gui_app.client_config.save is not client_config.load


# -- the mapping editor and the manage dialog ------------------------------


@pytest.fixture
def pad():
    from types import SimpleNamespace

    return SimpleNamespace(
        guid="pad-guid", name="Test Pad", instance_id=0, is_mapped=True,
        axis_count=6, button_count=12, hat_count=1,
        display_name=lambda: "Test Pad",
    )


@pytest.fixture
def bindings():
    from client.input.mapping import AxisBinding, InputSource, SourceKind

    buttons = {
        name: InputSource(SourceKind.BUTTON, index)
        for index, name in enumerate(
            ["a", "b", "x", "y", "lb", "rb", "back", "start",
             "guide", "lstick", "rstick", "misc1"]
        )
    }
    for name, mask in [("dpad_up", 1), ("dpad_right", 2),
                       ("dpad_down", 4), ("dpad_left", 8)]:
        buttons[name] = InputSource(SourceKind.HAT, 0, mask)
    buttons["left_trigger"] = InputSource(SourceKind.AXIS, 4, 1)
    buttons["right_trigger"] = InputSource(SourceKind.AXIS, 5, 1)

    axes = {
        name: AxisBinding(index)
        for index, name in enumerate(
            ["left_x", "left_y", "right_x", "right_y",
             "left_trigger", "right_trigger"]
        )
    }
    return {"buttons": buttons, "axes": axes}


@pytest.fixture
def fake_backend(pad):
    class Backend:
        def acquire(self, instance_id):
            return pad

        def release(self, instance_id):
            pass

        def raw_snapshot(self, instance_id):
            return None

        def set_mapping(self, guid, mapping):
            pass

    return Backend()


@pytest.fixture
def fast_capture(monkeypatch):
    """Remove both timed waits so capture tests are not wall-clock bound.

    Set to zero rather than bypassed: ``_capture_held`` and ``_hold_axis`` both
    still run, so the re-baseline on the way out and the restart-on-direction-
    change are still exercised. The durations themselves are covered separately
    in TestCaptureSettleDelay and TestAnalogHoldToConfirm.
    """
    from client.gui import mapping_dialog

    monkeypatch.setattr(mapping_dialog, "_HOLD_TO_BIND_S", 0.0)


@pytest.fixture
def store():
    from types import SimpleNamespace

    from client.gui.controller_config import ConfigurationStore

    return ConfigurationStore.from_config(SimpleNamespace(configurations=[]))


def _editor(store, backend, pad, bindings, name, qt_app):
    from client.gui.controller_presets import materialise
    from client.gui.mapping_dialog import MappingDialog

    working = materialise(store.get(name), pad, bindings, keep_builtin=True)
    return MappingDialog(backend, pad, working, None, store=store)


class TestEditingABuiltin:
    """A built-in is regenerated from a rule on every launch.

    Writing to one would be undone at the next start, taking the player's edits
    with it -- so Save is refused and "Save as..." is the way out.
    """

    def test_save_is_disabled(self, qt_app, store, fake_backend, pad, bindings):
        dialog = _editor(store, fake_backend, pad, bindings, "Xbox Controller", qt_app)

        assert dialog._save_button.isEnabled() is False
        assert dialog._save_as_button.isEnabled() is True
        assert "built-in" in dialog._save_button.toolTip().lower()

    def test_it_says_which_configuration_is_open(
        self, qt_app, store, fake_backend, pad, bindings
    ):
        dialog = _editor(store, fake_backend, pad, bindings, "Xbox Controller", qt_app)

        assert "Xbox Controller" in dialog._name_label.text()
        assert "built-in" in dialog._name_label.text()

    def test_saving_anyway_is_refused(
        self, qt_app, store, fake_backend, pad, bindings
    ):
        """Reachable through the dialog's default button even while disabled."""
        dialog = _editor(store, fake_backend, pad, bindings, "Xbox Controller", qt_app)

        dialog._on_save()

        assert dialog.result() == 0, "dialog must not have accepted"
        assert "Save as" in dialog._status.text()

    def test_a_builtin_opens_with_real_bindings(
        self, qt_app, store, fake_backend, pad, bindings
    ):
        """It stores none of its own, so they have to be resolved to be edited."""
        dialog = _editor(store, fake_backend, pad, bindings, "Xbox Controller", qt_app)

        assert dialog.configuration.mappings["xbox"].buttons


class TestSaveAs:
    def _saved_as(self, store, backend, pad, bindings, qt_app, name="My Pad"):
        dialog = _editor(store, backend, pad, bindings, "Xbox Controller", qt_app)
        dialog._ask_for_name = lambda: name
        dialog._on_save_as()
        return dialog

    def test_the_copy_is_stored_immediately(
        self, qt_app, store, fake_backend, pad, bindings
    ):
        """It has to exist while the dialog is still open, to keep editing it."""
        self._saved_as(store, fake_backend, pad, bindings, qt_app)

        assert store.get("My Pad") is not None

    def test_editing_continues_in_the_copy(
        self, qt_app, store, fake_backend, pad, bindings
    ):
        dialog = self._saved_as(store, fake_backend, pad, bindings, qt_app)

        assert dialog.configuration.name == "My Pad"
        assert dialog.configuration.builtin is False
        assert dialog._save_button.isEnabled() is True

    def test_later_edits_land_on_the_copy_not_the_original(
        self, qt_app, store, fake_backend, pad, bindings
    ):
        """The mappings are deep-copied, so the editor must repoint at them."""
        from client.input.mapping import InputSource, SourceKind

        dialog = self._saved_as(store, fake_backend, pad, bindings, qt_app)
        dialog._mapping.buttons[1] = InputSource(SourceKind.BUTTON, 99)
        dialog._on_save()

        assert store.get("My Pad").mappings["xbox"].buttons[1].index == 99

    def test_the_builtin_is_left_untouched(
        self, qt_app, store, fake_backend, pad, bindings
    ):
        self._saved_as(store, fake_backend, pad, bindings, qt_app)
        builtin = store.get("Xbox Controller")

        assert builtin.builtin is True
        assert builtin.mappings == {}

    def test_created_copy_is_reported(
        self, qt_app, store, fake_backend, pad, bindings
    ):
        """The caller must keep the copy even if the dialog is then cancelled."""
        dialog = self._saved_as(store, fake_backend, pad, bindings, qt_app)
        dialog.reject()

        assert dialog.created_copy is True
        assert store.get("My Pad") is not None

    def test_declining_the_name_prompt_changes_nothing(
        self, qt_app, store, fake_backend, pad, bindings
    ):
        dialog = _editor(store, fake_backend, pad, bindings, "Xbox Controller", qt_app)
        dialog._ask_for_name = lambda: None

        dialog._on_save_as()

        assert dialog.created_copy is False
        assert dialog.configuration.builtin is True


class TestGuidedBinding:
    """The wizard walks every control of a type, asking for one at a time.

    It drives the same capture machinery as the individual Bind buttons, so the
    risk is not in capturing -- it is in the sequencing: switching controller
    type mid-run must swap which mapping is being written, and the manual
    controls must not stay live and steal a press.
    """

    def _dialog(self, qt_app, layout="nes"):
        from types import SimpleNamespace

        from client.gui.controller_config import ControllerConfiguration
        from client.gui.mapping_dialog import MappingDialog

        pad = SimpleNamespace(
            guid="pad", name="Test Pad", instance_id=0, is_mapped=True,
            axis_count=6, button_count=20, hat_count=1,
            display_name=lambda: "Test Pad",
        )

        class Backend:
            def __init__(self):
                self.axes = [0] * 6
                self.buttons = [False] * 20
                self.hats = [0]

            def pump(self):
                pass

            def poll(self, instance_id, out):
                return True

            def set_mapping(self, guid, mapping):
                pass

            def raw_snapshot(self, instance_id):
                return {
                    "axes": list(self.axes),
                    "buttons": list(self.buttons),
                    "hats": list(self.hats),
                }

        backend = Backend()
        configuration = ControllerConfiguration(name="Test", layout=layout)
        return MappingDialog(backend, pad, configuration, None), backend, configuration

    def _press_through(self, dialog, backend, limit=400):
        """Satisfy every step until the wizard ends.

        Which input to fake depends on what is being asked for: a button press
        does nothing for an axis step, so driving buttons alone stalls forever
        on the first stick. Axis steps alternate the sign so each one differs
        from the previous baseline by more than the capture threshold.
        """
        step = 0
        while dialog._wizard is not None and step < limit:
            # Release, settle, then press -- a real player lets go between
            # controls, and the settle delay re-baselines on whatever is held.
            backend.buttons = [False] * 20
            backend.axes = [0] * 6
            dialog._tick()
            if dialog._wizard is None:
                break

            if dialog._axis_capture:
                axes = [0] * 6
                if not dialog._axis_rearmed:
                    pass  # let the stick fall back to centre between halves
                elif dialog._axis_capture in ("left_trigger", "right_trigger"):
                    # One direction only, so alternate: two triggers in a row
                    # would otherwise show no movement from the last baseline.
                    axes[0] = 30000 if step % 2 else -30000
                elif dialog._axis_stage == "negative":
                    axes[0] = -30000
                else:
                    axes[0] = 30000
                backend.axes = axes
            else:
                backend.buttons[step % 20] = True
            dialog._tick()
            step += 1

        assert step < limit, (
            f"wizard did not finish in {limit} steps; stuck at "
            f"{dialog._wizard[dialog._wizard_index] if dialog._wizard else None}"
        )
        return step

    def test_it_starts_idle(self, qt_app):
        dialog, _, _ = self._dialog(qt_app)

        assert dialog._wizard is None
        assert dialog._wizard_start_row.isVisibleTo(dialog)
        assert not dialog._wizard_row.isVisibleTo(dialog)

    def test_one_step_per_control(self, qt_app):
        from client.gui.controller_layouts import LAYOUTS_BY_KEY

        dialog, _, _ = self._dialog(qt_app)
        dialog._start_wizard(False)

        assert len(dialog._wizard) == len(LAYOUTS_BY_KEY["nes"].bindable())

    def test_it_names_the_control_and_the_step(self, qt_app):
        dialog, _, _ = self._dialog(qt_app)
        dialog._start_wizard(False)

        text = dialog._wizard_label.text()

        assert "Step 1 of 8" in text
        assert "NES" in text
        assert ">A<" in text

    def test_the_target_is_ringed_on_the_artwork(self, qt_app):
        """Text alone makes the player hunt; the picture is right there."""
        from common.state import Button

        dialog, _, _ = self._dialog(qt_app)
        dialog._start_wizard(False)

        assert dialog._preview._highlight == Button.A

    def test_manual_controls_are_locked_while_running(self, qt_app):
        dialog, _, _ = self._dialog(qt_app)
        dialog._start_wizard(False)

        assert not dialog._layout_combo.isEnabled()
        assert all(not b.isEnabled() for b in dialog._bind_buttons)

    def test_a_press_binds_and_advances(self, qt_app, fast_capture):
        from common.state import Button

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._start_wizard(False)

        backend.buttons[5] = True
        dialog._tick()

        assert configuration.mappings["nes"].buttons[Button.A].index == 5
        assert dialog._wizard_index == 1

    def test_walking_it_binds_every_control(self, qt_app, fast_capture):
        from client.gui.controller_layouts import LAYOUTS_BY_KEY

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._start_wizard(False)
        self._press_through(dialog, backend)

        bound = configuration.mappings["nes"].buttons

        assert len(bound) == len(LAYOUTS_BY_KEY["nes"].bindable())

    def test_it_binds_nothing_the_type_does_not_have(self, qt_app, fast_capture):
        """An NES configuration must not quietly send bumpers and stick clicks."""
        from client.gui.controller_layouts import LAYOUTS_BY_KEY
        from common.state import Button

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._start_wizard(False)
        self._press_through(dialog, backend)

        allowed = {bit for bit, _ in LAYOUTS_BY_KEY["nes"].bindable()}
        bound = set(configuration.mappings["nes"].buttons)

        assert bound <= allowed
        assert Button.LEFT_BUMPER not in bound

    def test_finishing_restores_the_manual_controls(self, qt_app, fast_capture):
        dialog, backend, _ = self._dialog(qt_app)
        dialog._start_wizard(False)
        self._press_through(dialog, backend)

        assert dialog._wizard is None
        assert dialog._layout_combo.isEnabled()
        assert dialog._wizard_start_row.isVisibleTo(dialog)
        assert "finished" in dialog._status.text()

    def test_skip_leaves_it_unbound_and_moves_on(self, qt_app):
        from common.state import Button

        dialog, _, configuration = self._dialog(qt_app)
        dialog._start_wizard(False)

        dialog._wizard_skip()

        assert dialog._wizard_index == 1
        assert Button.A not in configuration.mappings["nes"].buttons

    def test_back_returns_to_the_previous_control(self, qt_app):
        dialog, _, _ = self._dialog(qt_app)
        dialog._start_wizard(False)
        dialog._wizard_skip()
        dialog._wizard_skip()

        dialog._wizard_back()

        assert dialog._wizard_index == 1

    def test_back_stops_at_the_first_step(self, qt_app):
        dialog, _, _ = self._dialog(qt_app)
        dialog._start_wizard(False)

        dialog._wizard_back()

        assert dialog._wizard_index == 0

    def test_stopping_keeps_what_was_already_bound(self, qt_app, fast_capture):
        from common.state import Button

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._start_wizard(False)
        backend.buttons[3] = True
        dialog._tick()

        dialog._stop_wizard()

        assert dialog._wizard is None
        assert configuration.mappings["nes"].buttons[Button.A].index == 3
        assert dialog._layout_combo.isEnabled()

    def test_the_summary_counts_skips(self, qt_app):
        dialog, _, _ = self._dialog(qt_app)
        dialog._start_wizard(False)
        while dialog._wizard is not None:
            dialog._wizard_skip()

        assert "8 skipped" in dialog._status.text()


class TestTriggersAppearOnce:
    """A trigger is one physical control, so it gets one row.

    It used to be listed twice -- as a button *and* as an axis -- which read as
    two separate things to bind, and binding the button half was pointless:
    apply_trigger_buttons derives that bit from the analog value anyway.
    """

    def _dialog(self, qt_app, layout="xbox"):
        return TestGuidedBinding()._dialog(qt_app, layout=layout)

    def test_triggers_are_not_in_the_button_list(self, qt_app):
        from common.state import Button

        dialog, _, _ = self._dialog(qt_app)

        assert Button.LEFT_TRIGGER not in dialog._rows
        assert Button.RIGHT_TRIGGER not in dialog._rows

    def test_triggers_are_in_the_axis_list(self, qt_app):
        from client.gui.mapping_dialog import _axis_key

        dialog, _, _ = self._dialog(qt_app)

        assert _axis_key("left_trigger") in dialog._rows
        assert _axis_key("right_trigger") in dialog._rows

    def test_other_buttons_are_untouched(self, qt_app):
        from common.state import Button

        dialog, _, _ = self._dialog(qt_app)

        assert Button.LEFT_BUMPER in dialog._rows
        assert Button.A in dialog._rows

    @pytest.mark.parametrize(
        "layout_key,expected",
        [("xbox", "LT"), ("ps5", "L2"), ("switch", "ZL"), ("n64", "Z")],
    )
    def test_a_trigger_uses_the_name_its_system_prints(
        self, qt_app, layout_key, expected
    ):
        """Generic "Left trigger" hid that the N64 calls this one Z."""
        from client.gui.controller_layouts import get_layout
        from client.gui.mapping_dialog import _axis_label

        assert _axis_label("left_trigger", False, get_layout(layout_key)) == expected

    def test_an_analog_trigger_binds_as_an_axis(self, qt_app, fast_capture):
        from common.state import Button

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._start_axis_capture("left_trigger")
        backend.axes[2] = 30000
        dialog._tick()

        mapping = configuration.mappings["xbox"]

        assert mapping.axes["left_trigger"].index == 2
        assert Button.LEFT_TRIGGER not in mapping.buttons

    def test_a_digital_trigger_binds_as_a_button(self, qt_app, fast_capture):
        """A retro pad's Z has no travel, so the bit is the only source."""
        from common.state import Button

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._start_axis_capture("left_trigger")
        backend.buttons[6] = True
        dialog._tick()

        mapping = configuration.mappings["xbox"]

        assert mapping.buttons[Button.LEFT_TRIGGER].index == 6
        assert "left_trigger" not in mapping.axes

    def test_rebinding_analog_over_digital_drops_the_button(self, qt_app, fast_capture):
        """Two sources for one control conflict; the later one wins outright."""
        from common.state import Button

        dialog, backend, configuration = self._dialog(qt_app)

        dialog._start_axis_capture("left_trigger")
        backend.buttons[6] = True
        dialog._tick()

        # Let go and let the settle delay re-baseline at rest, as a player
        # would, before pushing the analog trigger instead.
        backend.buttons[6] = False
        dialog._start_axis_capture("left_trigger")
        dialog._tick()
        backend.axes[2] = 30000
        dialog._tick()

        mapping = configuration.mappings["xbox"]

        assert "left_trigger" in mapping.axes
        assert Button.LEFT_TRIGGER not in mapping.buttons


class TestAnalogCaptureThreshold:
    """An axis must be pushed near its extent to count.

    At a third of travel, brushing a stick on the way to a face button was
    enough to bind it -- and afterwards the binding looked deliberate.
    """

    def _dialog(self, qt_app):
        return TestGuidedBinding()._dialog(qt_app, layout="xbox")

    def test_the_threshold_is_near_full_travel(self, qt_app):
        from client.gui import mapping_dialog

        assert mapping_dialog._AXIS_CAPTURE_DELTA / 32767 > 0.75

    @pytest.mark.parametrize("reading", [5000, 12000, 20000, 25000])
    def test_a_partial_push_is_ignored(self, qt_app, fast_capture, reading):
        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_x", None)
        dialog._start_axis_capture("left_x")

        backend.axes[0] = -reading
        for _ in range(3):
            dialog._tick()

        assert dialog._axis_stage == "negative", "a nudge should not count"
        assert "left_x" not in configuration.mappings["xbox"].axes

    def test_a_decisive_push_is_taken(self, qt_app, fast_capture):
        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_x", None)
        dialog._start_axis_capture("left_x")

        backend.axes[0] = -30000
        dialog._tick()

        assert dialog._axis_stage == "positive"

    def test_a_nudge_does_not_bind_a_button_either(self, qt_app, fast_capture):
        """Axis halves can drive buttons, so the same guard has to apply."""
        from common.state import Button

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_button(Button.A, None)
        dialog._start_capture(Button.A)

        backend.axes[3] = 20000
        for _ in range(3):
            dialog._tick()

        assert Button.A not in configuration.mappings["xbox"].buttons


class TestSticksOfferDirectionsOnly:
    """The N64's stick does not click, so it is bound by direction alone.

    The artwork needs a stick element to anchor the axes to, but that element
    also carried a bindable button -- offering "Analog stick" beside the four
    directions, asking for a press the pad cannot make.
    """

    def test_the_n64_stick_is_not_bindable_as_a_button(self):
        from client.gui.controller_layouts import LAYOUTS_BY_KEY
        from common.state import Button

        bindable = {bit for bit, _ in LAYOUTS_BY_KEY["n64"].bindable()}

        assert Button.LEFT_STICK not in bindable

    def test_but_its_axes_are_still_offered(self):
        from client.gui.controller_layouts import LAYOUTS_BY_KEY

        layout = LAYOUTS_BY_KEY["n64"]

        assert layout.has_axis("left_x")
        assert layout.has_axis("left_y")

    def test_pads_whose_sticks_do_click_keep_the_binding(self):
        """L3/R3 are real buttons on a modern pad; removing them would lose one."""
        from client.gui.controller_layouts import LAYOUTS_BY_KEY
        from common.state import Button

        for key in ("xbox", "ps5", "switch"):
            bindable = {bit for bit, _ in LAYOUTS_BY_KEY[key].bindable()}

            assert Button.LEFT_STICK in bindable, key
            assert Button.RIGHT_STICK in bindable, key

    def test_the_n64_row_is_gone_from_the_dialog(self, qt_app):
        from common.state import Button

        dialog, _, _ = TestGuidedBinding()._dialog(qt_app, layout="n64")

        assert Button.LEFT_STICK not in dialog._rows

    def test_the_stick_still_lights_in_the_preview(self, qt_app):
        """It is drawn from the axes, which are unaffected."""
        from client.gui.controller_layouts import KIND_STICK, LAYOUTS_BY_KEY

        sticks = [
            c for c in LAYOUTS_BY_KEY["n64"].controls if c.kind == KIND_STICK
        ]

        assert len(sticks) == 1
        assert sticks[0].element == "c_lstick"


class TestHoldingThroughTheSettleDelay:
    """A control already held when the pause ends must still be bindable.

    The pause re-baselines so a leftover button press cannot answer the next
    prompt. Re-reading the *axes* there was a stall: whatever the player was
    already pushing became the resting point, and a stick or trigger held
    through the pause had no further travel left to detect. The wizard sat on
    one prompt forever.
    """

    def _dialog(self, qt_app, layout="xbox"):
        return TestGuidedBinding()._dialog(qt_app, layout=layout)

    def test_a_stick_held_through_the_pause_still_binds(self, qt_app, fast_capture):
        from common.state import Button

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_x", None)

        # Bind something, then push the stick before the next prompt settles.
        dialog._start_capture(Button.A)
        backend.buttons[0] = True
        dialog._tick()

        dialog._start_axis_capture("left_x")
        backend.axes[0] = -30000       # pushed during the pause, and held
        for _ in range(5):
            dialog._tick()

        assert dialog._axis_stage == "positive", "the held push was swallowed"

    def test_a_trigger_held_through_the_pause_still_binds(self, qt_app, fast_capture):
        from common.state import Button

        dialog, backend, configuration = self._dialog(qt_app)

        dialog._start_capture(Button.A)
        backend.buttons[0] = True
        dialog._tick()

        dialog._start_axis_capture("left_trigger")
        backend.axes[4] = 30000
        for _ in range(5):
            dialog._tick()

        assert configuration.mappings["xbox"].axes["left_trigger"].index == 4

    def test_a_held_button_is_still_absorbed(self, qt_app, fast_capture):
        """The behaviour the pause exists for must survive the fix."""
        from common.state import Button

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_button(Button.B, None)

        dialog._start_capture(Button.A)
        backend.buttons[0] = True
        dialog._tick()

        dialog._start_capture(Button.B)      # button 0 still held
        for _ in range(5):
            dialog._tick()

        assert Button.B not in configuration.mappings["xbox"].buttons


class TestEachControlAskedOnce:
    """The wizard asks for every control exactly once.

    LT and RT used to be queued twice -- once as a button bit, once as the
    trigger axis -- and the second answer just overwrote the first. The axis
    step is the one that survives, since it accepts analog and digital alike.
    """

    @pytest.mark.parametrize("layout_key", ["xbox", "ps5", "switch", "switch2"])
    def test_analog_trigger_bits_are_not_queued(self, qt_app, layout_key):
        """Their axis step captures them; the bit would be a second ask."""
        from common.state import Button

        dialog, _, _ = TestGuidedBinding()._dialog(qt_app, layout=layout_key)
        targets = [t for _, t in dialog._wizard_targets(layout_key)]

        assert Button.LEFT_TRIGGER not in targets
        assert Button.RIGHT_TRIGGER not in targets

    def test_a_digital_trigger_bit_is_queued_as_a_button(self, qt_app):
        """The N64's Z is a switch, so it has no axis step to capture it."""
        from common.state import Button

        dialog, _, _ = TestGuidedBinding()._dialog(qt_app, layout="n64")
        targets = [t for _, t in dialog._wizard_targets("n64")]

        assert Button.LEFT_TRIGGER in targets
        assert "left_trigger" not in targets

    @pytest.mark.parametrize(
        "layout_key",
        ["xbox", "ps5", "switch", "switch2", "n64", "snes", "nes", "genesis"],
    )
    def test_no_target_repeats(self, qt_app, layout_key):
        dialog, _, _ = TestGuidedBinding()._dialog(qt_app, layout=layout_key)
        targets = dialog._wizard_targets(layout_key)

        assert len(targets) == len(set(targets)), layout_key

    def test_the_trigger_axis_step_is_still_there(self, qt_app):
        """Dropping the bit must not drop the trigger entirely."""
        dialog, _, _ = TestGuidedBinding()._dialog(qt_app, layout="xbox")
        targets = [t for _, t in dialog._wizard_targets("xbox")]

        assert "left_trigger" in targets
        assert "right_trigger" in targets


class TestButtonHoldToConfirm:
    """Buttons and hats are held to confirm, exactly like axes.

    A button clipped while reaching past it looks identical to a deliberate
    press for one frame; only persistence separates them.
    """

    def _dialog(self, qt_app):
        return TestGuidedBinding()._dialog(qt_app, layout="xbox")

    def test_a_momentary_press_is_not_enough(self, qt_app):
        from common.state import Button

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_button(Button.A, None)
        dialog._start_capture(Button.A)

        backend.buttons[7] = True
        dialog._tick()

        assert Button.A not in configuration.mappings["xbox"].buttons

    def test_holding_it_long_enough_binds(self, qt_app, monkeypatch):
        from client.gui import mapping_dialog
        from common.state import Button

        monkeypatch.setattr(mapping_dialog, "_HOLD_TO_BIND_S", 0.02)

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._start_capture(Button.A)

        backend.buttons[7] = True
        dialog._tick()
        time.sleep(0.15)
        dialog._tick()

        assert configuration.mappings["xbox"].buttons[Button.A].index == 7

    def test_releasing_early_resets_the_hold(self, qt_app):
        from common.state import Button

        dialog, backend, _ = self._dialog(qt_app)
        dialog._start_capture(Button.A)

        backend.buttons[7] = True
        dialog._tick()
        assert dialog._input_hold is not None

        backend.buttons[7] = False
        dialog._tick()

        assert dialog._input_hold is None

    def test_pressing_a_different_button_restarts_the_hold(self, qt_app):
        from common.state import Button

        dialog, backend, _ = self._dialog(qt_app)
        dialog._start_capture(Button.A)

        backend.buttons[7] = True
        dialog._tick()
        first = dialog._input_hold

        backend.buttons[7] = False
        backend.buttons[8] = True
        dialog._tick()

        assert dialog._input_hold is not None
        assert dialog._input_hold[0] != first[0]

    def test_a_hat_must_be_held_too(self, qt_app):
        from common.state import Button

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_button(Button.DPAD_UP, None)
        dialog._start_capture(Button.DPAD_UP)

        backend.hats[0] = 0x01
        dialog._tick()

        assert Button.DPAD_UP not in configuration.mappings["xbox"].buttons
        assert dialog._input_hold is not None

    def test_the_ring_sits_on_the_button_being_bound(self, qt_app):
        """Not on the stick -- the progress helper serves both."""
        from common.state import Button

        dialog, backend, _ = self._dialog(qt_app)
        dialog._start_capture(Button.X)

        backend.buttons[7] = True
        dialog._tick()          # hold starts; no elapsed time yet
        time.sleep(0.15)
        dialog._tick()

        assert dialog._preview._highlight == Button.X
        assert dialog._preview._highlight_progress > 0

    def test_keyboard_keys_stay_instant(self, qt_app):
        """Typing is already deliberate; a hold per key would be a chore."""
        from client.gui.mapping_dialog import MappingDialog
        from client.gui.controller_config import ControllerConfiguration
        from client.input.keyboard_backend import KEYBOARD_GUID
        from common.state import Button

        keyboard = SimpleNamespace(
            guid=KEYBOARD_GUID, name="Keyboard", instance_id=0, is_mapped=True,
            axis_count=0, button_count=0, hat_count=0,
            display_name=lambda: "Keyboard",
        )

        class KB:
            pressed_keys = frozenset()

            def pump(self): pass
            def poll(self, i, o): return True
            def set_mapping(self, g, m): pass
            def raw_snapshot(self, i): return None

        backend = KB()
        configuration = ControllerConfiguration(name="k", layout="xbox")
        dialog = MappingDialog(backend, keyboard, configuration, None)
        dialog._start_capture(Button.A)

        backend.pressed_keys = frozenset({65})
        dialog._tick()

        assert configuration.mappings["xbox"].buttons[Button.A].index == 65


class TestStickDirectionsAreSeparateSteps:
    """Each stick direction is its own wizard step.

    They used to be one step with an internal two-half flow, which meant Skip
    and Esc dropped the axis's *other* direction as well -- skipping "Left"
    silently skipped "Right", so the walk-through appeared never to ask for it.
    """

    def _dialog(self, qt_app, layout="xbox"):
        return TestGuidedBinding()._dialog(qt_app, layout=layout)

    @pytest.mark.parametrize(
        "layout_key,expected_sticks", [("xbox", 2), ("ps5", 2), ("n64", 1)]
    )
    def test_four_steps_per_stick(self, qt_app, layout_key, expected_sticks):
        dialog, _, _ = self._dialog(qt_app, layout=layout_key)
        targets = dialog._wizard_targets(layout_key)

        halves = [t for _, t in targets if isinstance(t, tuple)]

        assert len(halves) == 4 * expected_sticks

    def test_both_directions_of_each_axis_are_queued(self, qt_app):
        dialog, _, _ = self._dialog(qt_app)
        halves = [t for _, t in dialog._wizard_targets("xbox") if isinstance(t, tuple)]

        for axis in ("left_x", "left_y", "right_x", "right_y"):
            stages = [t[2] for t in halves if t[1] == axis]
            assert stages == ["negative", "positive"], axis

    def test_triggers_stay_a_single_step(self, qt_app):
        dialog, _, _ = self._dialog(qt_app)
        targets = [t for _, t in dialog._wizard_targets("xbox")]

        assert "left_trigger" in targets
        assert not any(
            isinstance(t, tuple) and t[1] == "left_trigger" for t in targets
        )

    def test_skipping_one_direction_leaves_the_other_asked(
        self, qt_app, fast_capture
    ):
        """The actual regression: Skip used to take both directions with it."""
        dialog, _, _ = self._dialog(qt_app)
        dialog._start_wizard(False)

        # Advance to the first stick half.
        while not isinstance(dialog._wizard[dialog._wizard_index][1], tuple):
            dialog._wizard_skip()

        first = dialog._wizard[dialog._wizard_index][1]
        dialog._wizard_skip()
        second = dialog._wizard[dialog._wizard_index][1]

        assert first[1] == second[1], "same axis"
        assert first[2] == "negative" and second[2] == "positive"

    def test_the_positive_half_can_stand_alone(self, qt_app, fast_capture):
        """If the negative half was skipped, one push still binds the axis."""
        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_x", None)

        dialog._start_axis_capture("left_x", stage="positive")
        backend.axes[0] = 30000
        dialog._tick()

        binding = configuration.mappings["xbox"].axes["left_x"]

        assert binding.index == 0
        assert binding.invert is False

    def test_a_backwards_axis_is_caught_from_the_positive_half(
        self, qt_app, fast_capture
    ):
        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_x", None)

        dialog._start_axis_capture("left_x", stage="positive")
        backend.axes[0] = -30000     # "Right" reads negative: wired backwards
        dialog._tick()

        assert configuration.mappings["xbox"].axes["left_x"].invert is True

    def test_the_positive_step_verifies_against_the_negative_one(
        self, qt_app, fast_capture
    ):
        """It recovers what the first step bound, so the check still runs."""
        from client.input.mapping import AxisBinding

        dialog, _, _ = self._dialog(qt_app)
        # What the negative step leaves behind: axis 0, pushed left, upright.
        dialog._mapping.bind_axis("left_x", AxisBinding(0, invert=False))

        dialog._start_axis_capture("left_x", stage="positive")

        assert dialog._axis_pending == (0, -1)
        assert dialog._axis_rearmed is False, "must re-centre between halves"

    def test_recovery_reads_an_inverted_axis_correctly(self, qt_app):
        from client.input.mapping import AxisBinding

        dialog, _, _ = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_x", AxisBinding(3, invert=True))

        dialog._start_axis_capture("left_x", stage="positive")

        assert dialog._axis_pending == (3, 1)


class TestNoSettleDelay:
    """The cooldown between bindings is gone.

    Hold-to-confirm covers what it protected against: a press held from the
    previous binding is already in the new prompt's baseline, and a fresh press
    has to persist for 0.6 s. The cooldown only added a window in which input
    was silently ignored.
    """

    def _dialog(self, qt_app):
        return TestGuidedBinding()._dialog(qt_app, layout="xbox")

    def test_the_constant_is_gone(self):
        from client.gui import mapping_dialog

        assert not hasattr(mapping_dialog, "_CAPTURE_HOLD_S")

    def test_a_new_press_binds_immediately_after_the_previous_one(
        self, qt_app, monkeypatch
    ):
        """The bug that prompted removing it: no dead window."""
        from client.gui import mapping_dialog
        from common.state import Button

        monkeypatch.setattr(mapping_dialog, "_HOLD_TO_BIND_S", 0.0)

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_button(Button.B, None)

        dialog._start_capture(Button.A)
        backend.buttons[7] = True
        dialog._tick()
        backend.buttons[7] = False

        # Straight into the next prompt, with no waiting.
        dialog._start_capture(Button.B)
        backend.buttons[9] = True
        dialog._tick()

        assert configuration.mappings["xbox"].buttons[Button.B].index == 9

    def test_a_button_held_from_the_previous_binding_is_still_ignored(
        self, qt_app, monkeypatch
    ):
        """The protection the cooldown gave must survive without it.

        The new prompt's baseline already contains the held button, so it
        never reads as a fresh press.
        """
        from client.gui import mapping_dialog
        from common.state import Button

        monkeypatch.setattr(mapping_dialog, "_HOLD_TO_BIND_S", 0.0)

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_button(Button.B, None)

        dialog._start_capture(Button.A)
        backend.buttons[7] = True
        dialog._tick()          # binds A; button 7 stays down

        dialog._start_capture(Button.B)
        for _ in range(5):
            dialog._tick()

        assert Button.B not in configuration.mappings["xbox"].buttons


class TestDigitalTriggerLayouts:
    """A trigger with no analog travel is a button, not an axis.

    The N64's Z is a switch. Listing it under "Sticks and triggers" asked the
    player to pull something part-way that only clicks, and bound it as an
    axis that the pad can never report travel on.
    """

    def test_the_n64_z_is_not_an_axis(self):
        from client.gui.controller_layouts import LAYOUTS_BY_KEY

        assert not LAYOUTS_BY_KEY["n64"].has_axis("left_trigger")

    def test_the_n64_z_is_in_the_button_list(self, qt_app):
        from common.state import Button

        dialog, _, _ = TestGuidedBinding()._dialog(qt_app, layout="n64")

        assert Button.LEFT_TRIGGER in dialog._rows

    def test_the_n64_z_has_no_axis_row(self, qt_app):
        from client.gui.mapping_dialog import _axis_key

        dialog, _, _ = TestGuidedBinding()._dialog(qt_app, layout="n64")

        assert _axis_key("left_trigger") not in dialog._rows

    @pytest.mark.parametrize("layout_key", ["xbox", "ps5", "switch", "switch2"])
    def test_analog_triggers_are_unaffected(self, qt_app, layout_key):
        from client.gui.mapping_dialog import _axis_key
        from common.state import Button

        dialog, _, _ = TestGuidedBinding()._dialog(qt_app, layout=layout_key)

        assert _axis_key("left_trigger") in dialog._rows
        assert Button.LEFT_TRIGGER not in dialog._rows

    def test_binding_z_stores_a_button_not_an_axis(self, qt_app, monkeypatch):
        from client.gui import mapping_dialog
        from common.state import Button

        monkeypatch.setattr(mapping_dialog, "_HOLD_TO_BIND_S", 0.0)

        dialog, backend, configuration = TestGuidedBinding()._dialog(
            qt_app, layout="n64"
        )
        dialog._start_capture(Button.LEFT_TRIGGER)
        backend.buttons[9] = True
        dialog._tick()

        mapping = configuration.mappings["n64"]

        assert mapping.buttons[Button.LEFT_TRIGGER].index == 9
        assert "left_trigger" not in mapping.axes

    def test_a_pressed_z_reaches_the_console(self, qt_app):
        """With no analog axis the bit alone must survive the poll path."""
        from client.input.mapping import DeviceMapping, InputSource, SourceKind
        from common.state import Button, ControllerState

        mapping = DeviceMapping(guid="g")
        mapping.bind_button(Button.LEFT_TRIGGER, InputSource(SourceKind.BUTTON, 9))
        compiled = mapping.compile()

        assert compiled.left_trigger_is_analog is False

        state = ControllerState()
        state.buttons = Button.LEFT_TRIGGER
        state.left_trigger = 255        # what the poll path synthesizes
        state.apply_trigger_buttons()

        assert state.buttons & Button.LEFT_TRIGGER


class TestTriggerIgnoresAStickReturningToCentre:
    """A released stick must not read as a trigger pull.

    Walking the N64 wizard bound Z to the left stick's Y axis: the trigger
    prompt opened while the stick was still deflected from the step before,
    and its return to centre was a large enough change to look like a pull.
    """

    def _dialog(self, qt_app, layout="n64"):
        return TestGuidedBinding()._dialog(qt_app, layout=layout)

    def test_a_stick_falling_back_is_not_a_pull(self, qt_app):
        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_trigger", None)

        # Prompt opens while the stick is held over.
        backend.axes[1] = -30000
        dialog._start_axis_capture("left_trigger")

        # Player lets go: a 30000 change, but landing on nothing.
        backend.axes[1] = 0
        for _ in range(5):
            dialog._tick()

        assert "left_trigger" not in configuration.mappings["n64"].axes

    def test_the_z_button_still_binds_afterwards(self, qt_app, monkeypatch):
        from client.gui import mapping_dialog
        from common.state import Button

        monkeypatch.setattr(mapping_dialog, "_HOLD_TO_BIND_S", 0.0)

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_trigger", None)

        backend.axes[1] = -30000
        dialog._start_axis_capture("left_trigger")
        backend.axes[1] = 0
        dialog._tick()

        backend.buttons[6] = True
        dialog._tick()

        assert configuration.mappings["n64"].buttons[Button.LEFT_TRIGGER].index == 6

    def test_a_real_analog_pull_is_still_taken(self, qt_app, monkeypatch):
        from client.gui import mapping_dialog

        monkeypatch.setattr(mapping_dialog, "_HOLD_TO_BIND_S", 0.0)

        dialog, backend, configuration = self._dialog(qt_app, layout="xbox")
        dialog._mapping.bind_axis("left_trigger", None)
        dialog._start_axis_capture("left_trigger")

        backend.axes[4] = 30000
        dialog._tick()

        assert configuration.mappings["xbox"].axes["left_trigger"].index == 4

    def test_a_trigger_resting_at_full_negative_still_works(
        self, qt_app, monkeypatch
    ):
        """Some raw joysticks rest a trigger at -32767 rather than 0."""
        from client.gui import mapping_dialog

        monkeypatch.setattr(mapping_dialog, "_HOLD_TO_BIND_S", 0.0)

        dialog, backend, configuration = self._dialog(qt_app, layout="xbox")
        dialog._mapping.bind_axis("left_trigger", None)

        backend.axes[4] = -32000        # resting
        dialog._start_axis_capture("left_trigger")
        backend.axes[4] = 32000         # pulled
        dialog._tick()

        assert configuration.mappings["xbox"].axes["left_trigger"].index == 4


class TestClearAndSecondBinding:
    """Per-row clear, and a second control for the same logical button."""

    def _dialog(self, qt_app):
        return TestGuidedBinding()._dialog(qt_app, layout="xbox")

    def test_clearing_removes_the_binding(self, qt_app):
        from common.state import Button

        dialog, _, configuration = self._dialog(qt_app)
        assert Button.A in configuration.mappings["xbox"].buttons

        dialog._clear_binding(Button.A)

        assert Button.A not in configuration.mappings["xbox"].buttons

    def test_clearing_removes_both_sources(self, qt_app):
        from client.input.mapping import InputSource, SourceKind
        from common.state import Button

        dialog, _, configuration = self._dialog(qt_app)
        dialog._mapping.bind_button_alt(Button.A, InputSource(SourceKind.BUTTON, 9))

        dialog._clear_binding(Button.A)

        mapping = configuration.mappings["xbox"]
        assert Button.A not in mapping.buttons
        assert Button.A not in mapping.buttons_alt

    def test_clearing_an_axis_row(self, qt_app):
        dialog, _, configuration = self._dialog(qt_app)
        assert "left_x" in configuration.mappings["xbox"].axes

        dialog._clear_axis("left_x")

        assert "left_x" not in configuration.mappings["xbox"].axes

    def test_clearing_a_trigger_row_takes_its_digital_binding_too(self, qt_app):
        """A digital trigger lives in buttons, not axes; leaving it would
        keep firing after the row was cleared."""
        from client.input.mapping import InputSource, SourceKind
        from common.state import Button

        dialog, _, configuration = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_trigger", None)
        dialog._mapping.buttons[Button.LEFT_TRIGGER] = InputSource(
            SourceKind.BUTTON, 6
        )

        dialog._clear_axis("left_trigger")

        assert Button.LEFT_TRIGGER not in configuration.mappings["xbox"].buttons

    def test_a_second_binding_is_added_not_replaced(self, qt_app, monkeypatch):
        from client.gui import mapping_dialog
        from common.state import Button

        monkeypatch.setattr(mapping_dialog, "_HOLD_TO_BIND_S", 0.0)

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_button(Button.A, None)

        dialog._start_capture(Button.A)
        backend.buttons[3] = True
        dialog._tick()
        backend.buttons[3] = False

        dialog._start_capture(Button.A, alt=True)
        backend.buttons[11] = True
        dialog._tick()

        mapping = configuration.mappings["xbox"]
        assert mapping.buttons[Button.A].index == 3
        assert mapping.buttons_alt[Button.A].index == 11

    def test_both_sources_are_shown_in_the_row(self, qt_app):
        from client.input.mapping import InputSource, SourceKind
        from common.state import Button

        dialog, _, _ = self._dialog(qt_app)
        dialog._mapping.bind_button(Button.A, InputSource(SourceKind.BUTTON, 3))
        dialog._mapping.bind_button_alt(Button.A, InputSource(SourceKind.BUTTON, 11))
        dialog._refresh_rows()

        assert dialog._rows[Button.A].text() == "Button 3 + Button 11"

    def test_an_empty_row_reads_as_unbound(self, qt_app):
        from common.state import Button

        dialog, _, _ = self._dialog(qt_app)
        dialog._clear_binding(Button.A)

        assert dialog._rows[Button.A].text() == "—"


class TestStickDirectionIndicator:
    """"Click the stick" and "push the stick up" must not look identical.

    Both ring the same artwork element, so without a direction cue the player
    had only the text to tell them apart.
    """

    def _dialog(self, qt_app):
        return TestGuidedBinding()._dialog(qt_app, layout="xbox")

    def test_a_stick_click_has_no_direction(self, qt_app):
        from common.state import Button

        dialog, _, _ = self._dialog(qt_app)
        dialog._start_capture(Button.LEFT_STICK)

        assert dialog._preview._highlight_direction is None

    @pytest.mark.parametrize(
        "axis,stage,expected",
        [
            ("left_x", "negative", (-1, 0)),
            ("left_x", "positive", (1, 0)),
            ("left_y", "negative", (0, -1)),
            ("left_y", "positive", (0, 1)),
            ("right_x", "negative", (-1, 0)),
            ("right_y", "positive", (0, 1)),
        ],
    )
    def test_each_direction_points_the_right_way(
        self, qt_app, axis, stage, expected
    ):
        dialog, _, _ = self._dialog(qt_app)
        dialog._start_axis_capture(axis, stage=stage)

        assert dialog._preview._highlight_direction == expected

    def test_a_trigger_has_no_direction(self, qt_app):
        dialog, _, _ = self._dialog(qt_app)
        dialog._start_axis_capture("left_trigger")

        assert dialog._preview._highlight_direction is None

    def test_the_arrow_follows_the_stage_mid_capture(self, qt_app, fast_capture):
        """The manual flow moves to the second half without restarting."""
        dialog, backend, _ = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_x", None)
        dialog._start_axis_capture("left_x")
        assert dialog._preview._highlight_direction == (-1, 0)

        backend.axes[0] = -30000
        dialog._tick()

        assert dialog._axis_stage == "positive"
        assert dialog._preview._highlight_direction == (1, 0)

    def test_the_direction_clears_when_capture_ends(self, qt_app):
        dialog, _, _ = self._dialog(qt_app)
        dialog._start_axis_capture("left_y")
        dialog._cancel_capture()

        assert dialog._preview._highlight_direction is None

    def test_a_direction_draws_differently_from_a_click(self, qt_app):
        """The whole point: the two must not render the same."""
        from PySide6.QtGui import QImage
        from common.state import Button

        dialog, _, _ = self._dialog(qt_app)
        preview = dialog._preview
        preview.resize(600, 420)

        def shot():
            image = QImage(600, 420, QImage.Format.Format_ARGB32)
            image.fill(0)
            preview.render(image)
            return image

        preview.set_highlight(Button.LEFT_STICK, lit=True)
        click = shot()
        preview.set_highlight(Button.LEFT_STICK, lit=True, direction=(0, -1))
        up = shot()
        preview.set_highlight(Button.LEFT_STICK, lit=True, direction=(0, 1))
        down = shot()

        def differing(a, b):
            return sum(
                1
                for y in range(0, 420, 2)
                for x in range(0, 600, 2)
                if a.pixelColor(x, y) != b.pixelColor(x, y)
            )

        assert differing(click, up) > 0, "direction looks like a click"
        assert differing(up, down) > 0, "up looks like down"


class TestAnalogHoldToConfirm:
    """An analog control must be held at its extent to be taken.

    A stick brushed on the way past reads identically to one pushed on purpose
    for a single frame, so the threshold alone cannot separate them. Requiring
    the deflection to persist can, and a deliberate push pays nothing for it.
    """

    def _dialog(self, qt_app):
        return TestGuidedBinding()._dialog(qt_app, layout="xbox")

    def test_a_momentary_push_is_not_enough(self, qt_app):
        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_x", None)
        dialog._start_axis_capture("left_x")

        backend.axes[0] = -30000
        dialog._tick()

        assert dialog._axis_stage == "negative"
        assert "left_x" not in configuration.mappings["xbox"].axes

    def test_holding_it_long_enough_takes_it(self, qt_app, monkeypatch):
        from client.gui import mapping_dialog

        monkeypatch.setattr(mapping_dialog, "_HOLD_TO_BIND_S", 0.02)

        dialog, backend, _ = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_x", None)
        dialog._start_axis_capture("left_x")

        backend.axes[0] = -30000
        dialog._tick()
        time.sleep(0.15)
        dialog._tick()

        assert dialog._axis_stage == "positive"

    def test_releasing_early_resets_the_hold(self, qt_app):
        dialog, backend, _ = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_x", None)
        dialog._start_axis_capture("left_x")

        backend.axes[0] = -30000
        dialog._tick()
        assert dialog._input_hold is not None

        backend.axes[0] = 0
        dialog._tick()

        assert dialog._input_hold is None

    def test_changing_direction_restarts_the_hold(self, qt_app):
        """A stick swept through a direction must not accumulate time in it."""
        dialog, backend, _ = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_x", None)
        dialog._start_axis_capture("left_x")

        backend.axes[0] = -30000
        dialog._tick()
        first = dialog._input_hold

        backend.axes[0] = 30000
        dialog._tick()

        assert dialog._input_hold is not None
        assert dialog._input_hold[:2] != first[:2]

    def test_sweeping_across_axes_restarts_the_hold(self, qt_app):
        dialog, backend, _ = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_x", None)
        dialog._start_axis_capture("left_x")

        backend.axes[0] = -30000
        dialog._tick()
        backend.axes[0] = 0
        backend.axes[1] = -30000
        dialog._tick()

        # Identity is (axis index, sign) for a deflection.
        assert dialog._input_hold[0] == (1, -1)

    def test_a_trigger_must_be_held_too(self, qt_app):
        dialog, backend, configuration = self._dialog(qt_app)
        # Clear the seeded guess, so "still unbound" means the hold blocked it.
        dialog._mapping.bind_axis("left_trigger", None)
        dialog._start_axis_capture("left_trigger")

        backend.axes[4] = 30000
        dialog._tick()

        assert dialog._input_hold is not None
        assert configuration.mappings["xbox"].axes.get("left_trigger") is None

    def test_a_digital_trigger_must_be_held_too(self, qt_app):
        """This path was instant, so a retro pad's Z bound on the lightest brush."""
        from common.state import Button

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_trigger", None)
        dialog._start_axis_capture("left_trigger")

        backend.buttons[6] = True
        dialog._tick()

        assert Button.LEFT_TRIGGER not in configuration.mappings["xbox"].buttons
        assert dialog._input_hold is not None

    def test_a_held_digital_trigger_binds(self, qt_app, monkeypatch):
        from client.gui import mapping_dialog
        from common.state import Button

        monkeypatch.setattr(mapping_dialog, "_HOLD_TO_BIND_S", 0.02)

        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_trigger", None)
        dialog._start_axis_capture("left_trigger")

        backend.buttons[6] = True
        dialog._tick()
        time.sleep(0.15)
        dialog._tick()

        assert configuration.mappings["xbox"].buttons[Button.LEFT_TRIGGER].index == 6


class TestHoldProgressIndicator:
    """The wait is deliberate, so it has to look deliberate.

    A control that sits unresponsive for most of a second reads as broken
    unless something visibly counts it out.
    """

    def _dialog(self, qt_app):
        return TestGuidedBinding()._dialog(qt_app, layout="xbox")

    def test_progress_starts_at_zero(self, qt_app):
        dialog, _, _ = self._dialog(qt_app)
        dialog._start_axis_capture("left_x")

        assert dialog._preview._highlight_progress == 0.0

    def test_progress_advances_while_held(self, qt_app, monkeypatch):
        from client.gui import mapping_dialog

        monkeypatch.setattr(mapping_dialog, "_HOLD_TO_BIND_S", 0.5)

        dialog, backend, _ = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_x", None)
        dialog._start_axis_capture("left_x")

        backend.axes[0] = -30000
        dialog._tick()
        time.sleep(0.1)
        dialog._tick()

        assert 0.0 < dialog._preview._highlight_progress < 1.0

    def test_progress_clears_on_release(self, qt_app, monkeypatch):
        from client.gui import mapping_dialog

        monkeypatch.setattr(mapping_dialog, "_HOLD_TO_BIND_S", 0.5)

        dialog, backend, _ = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_x", None)
        dialog._start_axis_capture("left_x")

        backend.axes[0] = -30000
        dialog._tick()
        time.sleep(0.1)
        dialog._tick()
        backend.axes[0] = 0
        dialog._tick()

        assert dialog._preview._highlight_progress == 0.0

    def test_the_indicator_is_on_the_stick_being_bound(self, qt_app):
        from common.state import Button

        dialog, _, _ = self._dialog(qt_app)

        dialog._start_axis_capture("right_y")

        assert dialog._preview._highlight == Button.RIGHT_STICK

    def test_progress_is_clamped(self, qt_app):
        dialog, _, _ = self._dialog(qt_app)
        dialog._preview.set_highlight(1, progress=5.0)

        assert dialog._preview._highlight_progress == 1.0


class TestWizardShowsTheControlBeingSet:
    """The picture must show the control being configured, not the one the
    player's press happens to drive today.

    Mid-rebind those are different controls, and lighting the old one tells the
    player their press went somewhere it did not.
    """

    def _dialog(self, qt_app):
        return TestGuidedBinding()._dialog(qt_app, layout="xbox")

    def test_the_target_is_drawn_as_pressed_during_the_wizard(self, qt_app):
        dialog, _, _ = self._dialog(qt_app)
        dialog._start_wizard(False)

        assert dialog._preview._highlight_lit is True

    def test_it_is_only_ringed_when_binding_by_hand(self, qt_app):
        """Outside the wizard the live preview is the useful thing."""
        from common.state import Button

        dialog, _, _ = self._dialog(qt_app)
        dialog._start_capture(Button.A)

        assert dialog._preview._highlight == Button.A
        assert dialog._preview._highlight_lit is False

    def test_live_input_is_suppressed_during_the_wizard(self, qt_app, fast_capture):
        from common.state import Button

        dialog, _, _ = self._dialog(qt_app)
        dialog._start_wizard(False)

        # What the backend would have reported for a press.
        dialog._state.buttons = Button.B
        dialog._tick()

        assert not dialog._preview._state.buttons & Button.B

    def test_live_input_still_shows_outside_the_wizard(self, qt_app):
        from common.state import Button

        dialog, _, _ = self._dialog(qt_app)

        dialog._state.buttons = Button.B
        dialog._tick()

        assert dialog._preview._state.buttons & Button.B

    def test_the_highlight_clears_when_the_wizard_ends(self, qt_app):
        dialog, _, _ = self._dialog(qt_app)
        dialog._start_wizard(False)
        dialog._stop_wizard()

        assert dialog._preview._highlight == 0
        assert dialog._preview._highlight_lit is False


class TestEveryTypesStickPrompts:
    """Left, Right, Up, Down -- once each, on every type that has a stick."""

    def _walk(self, qt_app, layout_key):
        import re

        dialog, backend, _ = TestGuidedBinding()._dialog(qt_app, layout=layout_key)
        dialog._start_wizard(False)

        index_of = {
            "left_x": 0, "left_y": 1, "right_x": 2, "right_y": 3,
            "left_trigger": 4, "right_trigger": 5,
        }
        prompts, headers, guard, last = [], [], 0, None
        while dialog._wizard is not None and guard < 600:
            guard += 1
            # Both places a prompt appears. The axis-letter regression showed
            # up in the wizard label while the status line was already clean,
            # so auditing only one of them proves nothing about the other.
            text = re.sub("<[^>]+>", "", dialog._status.text())
            if dialog._axis_capture and text != last:
                prompts.append(text)
                headers.append(re.sub("<[^>]+>", "", dialog._wizard_label.text()))
                last = text

            if dialog._axis_capture:
                backend.axes = [0] * 6
                if dialog._axis_rearmed:
                    backend.axes[index_of[dialog._axis_capture]] = (
                        -30000 if dialog._axis_stage == "negative" else 30000
                    )
            else:
                backend.buttons = [False] * 20
                backend.buttons[guard % 20] = True
            dialog._tick()

        assert dialog._wizard is None, f"{layout_key} stalled after {guard} ticks"
        return prompts + headers

    @pytest.mark.parametrize(
        "layout_key", ["xbox", "ps5", "switch", "switch2", "n64"]
    )
    def test_each_stick_is_asked_left_right_up_down(
        self, qt_app, fast_capture, layout_key
    ):
        prompts = self._walk(qt_app, layout_key)

        per_stick: dict[str, list[str]] = {}
        for prompt in prompts:
            # Status lines start with the stick's name; wizard headers start
            # with "Step" and are audited by the axis-letter test instead.
            if not prompt.startswith(("Left analog stick", "Right analog stick")):
                continue
            name, rest = prompt.split(":", 1)
            per_stick.setdefault(name.strip(), []).append(
                rest.split("push ")[1].split(".")[0].strip()
            )

        assert per_stick, f"{layout_key} offered no stick prompts"
        for stick, order in per_stick.items():
            assert order == ["Left", "Right", "Up", "Down"], f"{layout_key}/{stick}"

    @pytest.mark.parametrize(
        "layout_key", ["xbox", "ps5", "switch", "switch2", "n64"]
    )
    def test_no_prompt_mentions_an_axis_letter(
        self, qt_app, fast_capture, layout_key
    ):
        """"Left stick X" asks the player to translate a letter into a push."""
        prompts = self._walk(qt_app, layout_key)

        for prompt in prompts:
            assert "stick X" not in prompt, prompt
            assert "stick Y" not in prompt, prompt

    @pytest.mark.parametrize("layout_key", ["snes", "nes", "genesis"])
    def test_stickless_types_ask_for_no_axes(
        self, qt_app, fast_capture, layout_key
    ):
        assert self._walk(qt_app, layout_key) == []

    def test_the_n64_has_one_stick_only(self, qt_app, fast_capture):
        prompts = self._walk(qt_app, "n64")
        sticks = {
            p.split(":")[0]
            for p in prompts
            if p.startswith(("Left analog stick", "Right analog stick"))
        }

        assert sticks == {"Left analog stick"}


class TestStickDirections:
    """Each stick direction is asked for by name.

    "Move the axis you want" is not something a person can act on, and a single
    push cannot tell a backwards-wired stick from a player pushing the other
    way. Asking for Left then Right settles both.
    """

    def _dialog(self, qt_app):
        return TestGuidedBinding()._dialog(qt_app, layout="xbox")

    def test_it_asks_for_a_direction_by_name(self, qt_app):
        dialog, _, _ = self._dialog(qt_app)

        dialog._start_axis_capture("left_x")

        assert "Left" in dialog._status.text()
        assert "X+" not in dialog._status.text()

    @pytest.mark.parametrize(
        "axis,first,second",
        [
            ("left_x", "Left", "Right"),
            ("right_x", "Left", "Right"),
            ("left_y", "Up", "Down"),
            ("right_y", "Up", "Down"),
        ],
    )
    def test_both_halves_are_named(self, qt_app, axis, first, second):
        from client.gui.mapping_dialog import _axis_directions

        assert _axis_directions(axis) == (first, second)

    def test_a_trigger_has_no_directions(self, qt_app):
        from client.gui.mapping_dialog import _axis_directions

        assert _axis_directions("left_trigger") is None

    def test_the_second_half_is_asked_for_after_the_first(self, qt_app, fast_capture):
        dialog, backend, _ = self._dialog(qt_app)
        dialog._start_axis_capture("left_x")

        backend.axes[0] = -30000
        dialog._tick()

        assert dialog._axis_stage == "positive"
        assert "Right" in dialog._status.text()

    def test_it_waits_for_the_stick_to_return_to_centre(self, qt_app, fast_capture):
        """Releasing a deflected stick is itself a big move the other way."""
        dialog, backend, configuration = self._dialog(qt_app)
        # The starting mapping already guesses this axis; clear it so "still
        # unbound" means the capture has genuinely not completed.
        dialog._mapping.bind_axis("left_x", None)
        dialog._start_axis_capture("left_x")

        backend.axes[0] = -30000
        dialog._tick()
        assert not dialog._axis_rearmed

        # Still held: the release must not be mistaken for the second push.
        dialog._tick()
        assert "left_x" not in configuration.mappings["xbox"].axes

        backend.axes[0] = 0
        dialog._tick()
        assert dialog._axis_rearmed
        assert "left_x" not in configuration.mappings["xbox"].axes

    def test_a_normal_stick_is_not_inverted(self, qt_app, fast_capture):
        dialog, backend, configuration = self._dialog(qt_app)
        dialog._start_axis_capture("left_x")

        backend.axes[0] = -30000   # Left reads negative: the usual wiring
        dialog._tick()
        backend.axes[0] = 0
        dialog._tick()
        backend.axes[0] = 30000
        dialog._tick()

        binding = configuration.mappings["xbox"].axes["left_x"]

        assert binding.index == 0
        assert binding.invert is False

    def test_a_backwards_stick_is_recorded_as_inverted(self, qt_app, fast_capture):
        """Better than the player discovering it mid-game."""
        dialog, backend, configuration = self._dialog(qt_app)
        dialog._start_axis_capture("left_x")

        backend.axes[3] = 30000    # Left reads positive: wired backwards
        dialog._tick()
        backend.axes[3] = 0
        dialog._tick()
        backend.axes[3] = -30000
        dialog._tick()

        binding = configuration.mappings["xbox"].axes["left_x"]

        assert binding.index == 3
        assert binding.invert is True

    def test_two_different_axes_restart_the_pair(self, qt_app, fast_capture):
        """One of the two pushes was misread; binding it would not work."""
        dialog, backend, configuration = self._dialog(qt_app)
        dialog._mapping.bind_axis("left_x", None)
        dialog._start_axis_capture("left_x")

        backend.axes[0] = -30000
        dialog._tick()
        backend.axes[0] = 0
        dialog._tick()
        backend.axes[1] = 30000    # a different stick entirely
        dialog._tick()

        assert dialog._axis_stage == "negative"
        assert "left_x" not in configuration.mappings["xbox"].axes
        assert "again" in dialog._status.text()


class TestGuidedBindingAllTypes:
    def _dialog(self, qt_app):
        return TestGuidedBinding()._dialog(qt_app, layout="xbox")

    def test_it_queues_every_type(self, qt_app):
        from client.gui.controller_layouts import LAYOUTS
        from client.input.mapping import STICK_AXES, TRIGGER_AXES
        from common.state import Button

        dialog, _, _ = self._dialog(qt_app)
        dialog._start_wizard(True)

        trigger_axis = {
            Button.LEFT_TRIGGER: "left_trigger",
            Button.RIGHT_TRIGGER: "right_trigger",
        }
        expected = sum(
            # An analog trigger is asked for as an axis, so its bit is not a
            # step; a digital one (the N64's Z) is a button step like any other.
            len([
                bit
                for bit, _ in layout.bindable()
                if bit not in trigger_axis or not layout.has_axis(trigger_axis[bit])
            ])
            # A stick axis is two steps, one per direction; a trigger is one.
            + 2 * len([n for n in STICK_AXES if layout.has_axis(n)])
            + len([n for n in TRIGGER_AXES if layout.has_axis(n)])
            for layout in LAYOUTS
        )

        assert len(dialog._wizard) == expected

    def test_it_visits_the_types_in_order(self, qt_app):
        from client.gui.controller_layouts import LAYOUTS

        dialog, _, _ = self._dialog(qt_app)
        dialog._start_wizard(True)

        seen = []
        while dialog._wizard is not None:
            seen.append(dialog._wizard[dialog._wizard_index][0])
            dialog._wizard_skip()

        order = [k for i, k in enumerate(seen) if i == 0 or seen[i - 1] != k]

        assert order == [layout.key for layout in LAYOUTS]

    def test_bindings_land_on_the_type_being_asked_for(self, qt_app, fast_capture):
        """Switching type swaps the mapping being written.

        Getting this wrong would write every type's bindings into whichever
        mapping happened to be active when the wizard started.
        """
        dialog, backend, configuration = self._dialog(qt_app)
        dialog._start_wizard(True)
        TestGuidedBinding()._press_through(dialog, backend, limit=400)

        from client.gui.controller_layouts import LAYOUTS
        from common.state import Button

        trigger_axis = {
            Button.LEFT_TRIGGER: "left_trigger",
            Button.RIGHT_TRIGGER: "right_trigger",
        }

        for layout in LAYOUTS:
            mapping = configuration.mappings[layout.key]
            bound = set(mapping.buttons)

            # An *analog* trigger is captured as an axis, so its bit is not in
            # the button table. A digital one (the N64's Z) is a plain button.
            allowed = {
                bit
                for bit, _ in layout.bindable()
                if bit not in trigger_axis or not layout.has_axis(trigger_axis[bit])
            }

            assert bound == allowed, layout.key

            for name in ("left_trigger", "right_trigger"):
                if layout.has_axis(name):
                    assert name in mapping.axes, f"{layout.key}/{name}"

    def test_the_type_selector_follows_along(self, qt_app):
        dialog, _, _ = self._dialog(qt_app)
        dialog._start_wizard(True)

        while dialog._wizard is not None and dialog._wizard[
            dialog._wizard_index
        ][0] == "xbox":
            dialog._wizard_skip()

        assert dialog._layout_combo.currentData() == "ps5"


class TestManageDialog:
    def _dialog(self, store, backend, pad, on_changed=None):
        from client.gui.configurations_dialog import ConfigurationsDialog

        return ConfigurationsDialog(
            store, backend, [pad], None, on_changed=on_changed
        )

    def test_builtins_are_not_listed(self, qt_app, store, fake_backend, pad):
        """There is nothing to manage: they come back on the next launch."""
        dialog = self._dialog(store, fake_backend, pad)

        assert dialog._list.count() == 0

    def test_custom_configurations_are_listed(self, qt_app, store, fake_backend, pad):
        from client.gui.controller_config import ControllerConfiguration

        store.upsert(ControllerConfiguration(name="Mine"))
        dialog = self._dialog(store, fake_backend, pad)

        assert dialog._list.count() == 1
        assert "Mine" in dialog._list.item(0).text()

    def test_the_empty_state_replaces_the_list(self, qt_app, store, fake_backend, pad):
        dialog = self._dialog(store, fake_backend, pad)

        assert dialog._empty.isVisibleTo(dialog)
        assert not dialog._list.isVisibleTo(dialog)

    def test_actions_need_a_selection(self, qt_app, store, fake_backend, pad):
        from client.gui.controller_config import ControllerConfiguration

        dialog = self._dialog(store, fake_backend, pad)
        assert not dialog._edit_button.isEnabled()

        store.upsert(ControllerConfiguration(name="Mine"))
        dialog._reload()

        assert dialog._edit_button.isEnabled()
        assert dialog._delete_button.isEnabled()
        assert dialog._export_button.isEnabled()

    def test_export_then_import_round_trips(
        self, qt_app, store, fake_backend, pad, tmp_path
    ):
        from client.gui.controller_config import ControllerConfiguration
        from client.input.mapping import InputSource, SourceKind

        mine = ControllerConfiguration(name="Mine")
        mine.mapping_for("n64").bind_button(1, InputSource(SourceKind.BUTTON, 7))
        store.upsert(mine)

        path = tmp_path / "mine.json"
        store.export_to_file(path, names=["Mine"])
        added = store.import_from_file(path)

        assert added == ["Mine (2)"], "an import must not overwrite local work"
        assert store.get("Mine (2)").mappings["n64"].buttons[1].index == 7

    def test_deleting_notifies_the_owner(self, qt_app, store, fake_backend, pad):
        from client.gui.controller_config import ControllerConfiguration

        store.upsert(ControllerConfiguration(name="Mine"))
        seen = []
        dialog = self._dialog(store, fake_backend, pad, on_changed=lambda: seen.append(1))

        store.remove("Mine")
        dialog._changed()

        assert seen == [1]
        assert dialog._list.count() == 0


class TestSlotsFollowDeletions:
    def test_a_slot_using_a_deleted_configuration_falls_back(self, window):
        """Leaving a dangling name would keep stale bindings on the slot."""
        from client.gui.controller_config import ControllerConfiguration

        window._configurations.upsert(ControllerConfiguration(name="Doomed"))
        window._config.controller(0).configuration = "Doomed"

        window._configurations.remove("Doomed")
        window._configurations_changed()

        assert window._config.controller(0).configuration == ""

    def test_an_intact_configuration_is_left_alone(self, window):
        window._config.controller(1).configuration = "Xbox Controller"

        window._configurations_changed()

        assert window._config.controller(1).configuration == "Xbox Controller"


class TestDiscoveryDoesNotClobberASavedAddress:
    """Discovery runs by itself at startup, so a careless selection is
    destructive: it overwrites the host and port fields, and the next save
    persists the substitution. A server reachable only over a VPN, or one set
    to hidden, would be silently replaced by whichever machine answered a
    broadcast first -- and the address the player typed would be gone.
    """

    def test_a_configured_host_survives_a_search_that_does_not_find_it(self, window):
        window._connection.host.setText("10.8.0.1")            # a VPN address
        window._connection.port.setValue(47800)

        window._populate_server_list(
            [{"host": "192.168.1.77", "port": 47800, "name": "Someone else's Pi"}],
            "direct",
        )

        assert window._connection.host.text() == "10.8.0.1", "the saved address was overwritten"
        assert window._connection.server_list.currentData() == window.CUSTOM_SERVER

    def test_the_saved_host_is_selected_when_discovery_finds_it(self, window):
        window._connection.host.setText("192.168.1.50")

        window._populate_server_list(
            [
                {"host": "192.168.1.77", "port": 47800, "name": "Other"},
                {"host": "192.168.1.50", "port": 47800, "name": "Mine"},
            ],
            "direct",
        )

        data = window._connection.server_list.currentData()
        assert isinstance(data, dict)
        assert data["host"] == "192.168.1.50"
        assert window._connection.host.text() == "192.168.1.50"

    def test_a_fresh_install_still_gets_the_first_result(self, window):
        """Nothing to lose, so being helpful beats being cautious."""
        window._connection.host.setText("")

        window._populate_server_list(
            [{"host": "192.168.1.77", "port": 47800, "name": "The only Pi"}], "direct"
        )

        assert window._connection.host.text() == "192.168.1.77"

    def test_a_configured_room_survives_a_broker_listing(self, window):
        window._connection.room.setText("my-private-room")

        window._populate_server_list([{"room": "someone-else", "name": "Public"}], "punch")

        assert window._connection.room.text() == "my-private-room"
        assert window._connection.server_list.currentData() == window.CUSTOM_SERVER

    def test_finding_nothing_leaves_the_fields_editable(self, window):
        window._connection.host.setText("10.8.0.1")

        window._populate_server_list([], "direct")

        assert window._connection.host.text() == "10.8.0.1"
        assert not window._connection.host.isReadOnly(), "Custom must stay editable"

    def test_the_substitution_is_not_persisted(self, window):
        """The actual harm: a save after discovery wrote the wrong host to disk."""
        window._connection.host.setText("10.8.0.1")
        window._populate_server_list(
            [{"host": "192.168.1.77", "port": 47800, "name": "Other"}], "direct"
        )

        window._save_ui_into_config()

        assert window._config.host == "10.8.0.1"


class TestDrawerScrollbarStaysInsideTheCard:
    """The scrollbar is drawn at the scroll area's edge.

    With the scroll area inset exactly to the glass, the bar rode the card's
    border and crossed the rounded corner -- visible whenever the list was
    scrolled to either end.
    """

    def test_the_scroll_area_is_inset_inside_the_glass(self, window, qt_app):
        from client.gui.shell import DRAWER_INSET, SCROLL_INSET

        drawer = window._drawer
        area = drawer._scroll.geometry()

        glass_left = DRAWER_INSET
        glass_top = DRAWER_INSET
        glass_right = drawer.width() - DRAWER_INSET
        glass_bottom = drawer.height() - DRAWER_INSET

        assert area.x() - glass_left >= SCROLL_INSET
        assert area.y() - glass_top >= SCROLL_INSET
        assert glass_right - (area.x() + area.width()) >= SCROLL_INSET
        assert glass_bottom - (area.y() + area.height()) >= SCROLL_INSET

    def test_the_inset_clears_the_corner_radius(self, window, qt_app):
        """Geometry, not eyeballing: how far the arc cuts in at that height.

        A smaller inset than the corner's intrusion puts the bar back on the
        curve, which is what the original looked like.
        """
        import math

        from common.design.tokens import Radius
        from client.gui.shell import SCROLL_INSET

        radius = Radius.PANEL
        intrusion = radius - math.sqrt(max(0.0, radius**2 - (radius - SCROLL_INSET) ** 2))
        assert SCROLL_INSET > intrusion, (
            f"the corner cuts {intrusion:.1f}px in at {SCROLL_INSET}px down, "
            "so the scrollbar would sit on the curve"
        )


class TestThemingIsNotQuadratic:
    """`apply_theme` sets the *application* stylesheet.

    Qt re-polishes every widget that exists when it does, so calling it from
    each window's constructor costs more the more windows exist. Measured with
    an isolated benchmark: six successive `MainWindow`s took 896ms rising to
    1996ms each, and flat at ~300ms once the constructor stopped re-applying a
    theme that was already active.

    **Tested through the decision, not through Qt.** The first version of these
    tests drove the real `apply_theme` and so re-polished every window the
    module had accumulated -- 717s and 352s of a 1221s run, which measured the
    session's widget count and not this logic at all.
    """

    def test_the_same_theme_needs_no_restyle(self, qt_app):
        from client.gui.app import theme_needs_applying
        from common.design.themes import active_theme

        themed = SimpleNamespace(styleSheet=lambda: "QWidget { }")
        assert not theme_needs_applying(active_theme(), themed)

    def test_a_different_theme_does(self, qt_app):
        from client.gui.app import theme_needs_applying
        from common.design.themes import active_theme

        themed = SimpleNamespace(styleSheet=lambda: "QWidget { }")
        other = "green" if active_theme() != "green" else "blue"
        assert theme_needs_applying(other, themed)

    def test_an_unstyled_application_always_does(self, qt_app):
        """A window built outside `run()` must still style itself."""
        from client.gui.app import theme_needs_applying
        from common.design.themes import active_theme

        bare = SimpleNamespace(styleSheet=lambda: "")
        assert theme_needs_applying(active_theme(), bare)
        assert theme_needs_applying(active_theme(), None)

    def test_the_constructor_asks_before_it_applies(self, qt_app, monkeypatch):
        """The wiring: construction must go through the decision.

        `apply_theme` is stubbed, so this costs nothing and still fails if the
        constructor is changed back to applying unconditionally.
        """
        from client.gui import app as gui_app

        asked: list[str] = []
        monkeypatch.setattr(
            gui_app, "theme_needs_applying",
            lambda name, app: (asked.append(name), False)[1],
        )
        monkeypatch.setattr(
            gui_app, "apply_theme",
            lambda app, name=None: pytest.fail("applied without asking"),
        )
        monkeypatch.setattr(client_config, "save", lambda config, path=None: None)
        gui_app.MainWindow(client_config.ClientConfig())
        assert asked, "the constructor never consulted the decision"

"""A configured controller has to work the moment the client opens.

Reported from the field: "I have to go in and save the configuration after
opening the client for the controller to sense button presses."

The cause was an ordering one. ``_refresh_devices`` calls ``_ensure_backend``
at the top, and that pushed each slot's mapping into the backend -- *before*
the rest of ``_refresh_devices`` filled the device combos.
``_apply_saved_mappings`` reads those combos to find which pad a slot holds,
so on that first pass every row read ``None``, the named-configuration loop
skipped every one, and nothing was installed. Opening the mapping screen and
pressing Save was the only other thing that called it, which is exactly the
workaround that got found.

These build a window the way a returning player's client starts: the
configuration is written into the config **before** ``MainWindow`` is
constructed, and nothing touches the UI afterwards. If a mapping has not
reached the backend by the time the window is up, the pad is dead.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="client GUI extras not installed")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from client import config as client_config  # noqa: E402
from client.gui.app import MainWindow  # noqa: E402
from client.gui.controller_config import ControllerConfiguration  # noqa: E402
from client.input.mapping import DeviceMapping, InputSource, SourceKind  # noqa: E402

#: What the synthetic backend calls its first pad. Pinned by a test below, so
#: a rename shows up there rather than as every other test quietly proving
#: nothing.
SYNTHETIC_GUID = "synthetic-0000"

PUSHED: list[tuple[str, object]] = []


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def saved_client_config(layout: str = "xbox") -> client_config.ClientConfig:
    """A config as an earlier session would have left it on disk.

    The binding is explicit rather than resolved from a preset: the synthetic
    pad reports zero axes and zero buttons, so a preset resolves to an *empty*
    mapping, and ``_apply_saved_mappings`` rightly declines to push one of
    those. A preset here would make the test pass or fail for a reason that
    has nothing to do with what it is checking.
    """
    mapping = DeviceMapping(guid=SYNTHETIC_GUID, name="Synthetic Controller 0")
    mapping.bind_button(1, InputSource(kind=SourceKind.BUTTON, index=0))

    configuration = ControllerConfiguration(name="Saved setup", layout=layout)
    configuration.mappings[layout] = mapping

    config = client_config.ClientConfig(backend_override="synthetic")
    config.configurations = [configuration.to_dict()]
    config.controller(0).guid = SYNTHETIC_GUID
    config.controller(0).configuration = configuration.name
    return config


@pytest.fixture
def launch(qt_app, monkeypatch):
    """Build windows and record every mapping pushed at the backend.

    ``set_mapping`` is patched on the **class**. Wrapping the backend object
    instead was tried and segfaulted Qt on teardown -- a proxy changes what
    holds the backend alive, which is the "do not disturb lifetimes from a
    fixture" hazard the GUI suite already documents.
    """
    monkeypatch.setattr(client_config, "save", lambda config, path=None: None)

    from client.input.composite import CompositeBackend

    real = CompositeBackend.set_mapping

    def recording(self, guid, mapping):
        PUSHED.append((guid, mapping))
        return real(self, guid, mapping)

    monkeypatch.setattr(CompositeBackend, "set_mapping", recording)

    windows = []

    def build(config=None):
        PUSHED.clear()
        window = MainWindow(config if config is not None else saved_client_config())
        windows.append(window)
        return window

    try:
        yield build
    finally:
        for window in windows:
            window.close()


class TestThePreconditions:
    """If these are wrong, everything below passes for the wrong reason."""

    def test_the_synthetic_pad_is_still_called_this(self, launch):
        window = launch(client_config.ClientConfig(backend_override="synthetic"))
        assert any(d.guid == SYNTHETIC_GUID for d in window._devices)

    def test_the_saved_slot_actually_claims_the_pad(self, launch):
        """The window has to end up holding the pad the config named, or there
        is nothing for a mapping to be pushed *for*."""
        held = launch()._controllers.device_combos[0].currentData()
        assert held is not None and held.guid == SYNTHETIC_GUID


class TestOpeningTheClientIsEnough:
    def test_a_saved_configuration_reaches_the_backend_on_launch(self, launch):
        """The reported bug. Nobody opens the mapping screen; nobody saves."""
        launch()
        assert PUSHED, (
            "no mapping reached the backend at startup -- the pad produces "
            "nothing until the player opens the mapping screen and saves"
        )

    def test_it_is_pushed_for_the_pad_the_slot_holds(self, launch):
        launch()
        assert any(guid == SYNTHETIC_GUID for guid, _ in PUSHED)

    def test_the_pushed_mapping_is_not_empty(self, launch):
        """An empty mapping installs nothing, so "a push happened" is not on
        its own the thing that makes the pad work."""
        launch()
        for guid, mapping in PUSHED:
            if guid == SYNTHETIC_GUID and mapping is not None and not mapping.is_empty():
                return
        pytest.fail("nothing usable was pushed for the saved pad")

    def test_a_client_with_nothing_saved_pushes_nothing(self, launch):
        """The other direction: a fresh install must not invent bindings."""
        launch(client_config.ClientConfig(backend_override="synthetic"))
        assert not [g for g, _ in PUSHED if g == SYNTHETIC_GUID]


class TestRefreshingKeepsItWorking:
    def test_refreshing_pushes_again(self, launch):
        """The fix lives at the end of ``_refresh_devices`` rather than in
        ``__init__`` precisely so a pad plugged in later, or a "Refresh gamepad
        list", gets its configuration too."""
        window = launch()
        PUSHED.clear()
        window._refresh_devices()
        assert any(guid == SYNTHETIC_GUID for guid, _ in PUSHED)

    def test_refreshing_twice_leaves_the_slot_alone(self, launch):
        window = launch()
        window._refresh_devices()
        window._refresh_devices()
        held = window._controllers.device_combos[0].currentData()
        assert held is not None and held.guid == SYNTHETIC_GUID

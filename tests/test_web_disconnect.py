"""The Disconnect button.

There was no way to drop a live console short of restarting the server. The
button shares a slot with "Connection mode" -- pair when nothing is attached,
disconnect when something is -- because those are the two things an operator
wants from that spot and never both at once.

Asserted against the shipped JavaScript because that is where the behaviour
lives; the server half is exercised through AdapterManager.
"""

from __future__ import annotations

from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "server" / "web" / "static"
SERVER = Path(__file__).resolve().parent.parent / "server"


@pytest.fixture(scope="module")
def app_js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def web_app() -> str:
    return (SERVER / "web" / "app.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def adapter_py() -> str:
    return (SERVER / "bt" / "adapter.py").read_text(encoding="utf-8")


class TestTheButtonSwapsWithTheLinkState:
    def test_it_offers_disconnect_when_a_console_is_attached(self, app_js):
        assert "linked ? 'disconnect' : 'pair'" in app_js
        assert "linked ? 'Disconnect' : 'Connection mode'" in app_js

    def test_the_link_state_is_the_link_not_the_subscription(self, app_js):
        """`channel.connected` is far stricter than "a host is attached".

        On BLE it means the host has *subscribed to notifications*, which is
        several steps after connecting. A console that had connected but not
        yet bonded left the button reading "Connection mode" with no way to
        drop the link -- exactly when Disconnect is most wanted.
        """
        assert "hw.peer || hw.phase === 'linked'" in app_js

    def test_it_still_counts_a_flowing_channel_as_linked(self, app_js):
        # Belt and braces: if reports are flowing, a host is certainly there.
        assert "channel.connected" in app_js

    def test_it_is_written_in_place_not_rebuilt(self, app_js):
        """Replacing the node between mousedown and mouseup ate button presses.

        The card renderer exists precisely to avoid that, so the swap must be a
        write to an existing element rather than new markup.
        """
        assert "field('pair-button')" in app_js
        assert "data-field=\"pair-button\"" in app_js

    def test_it_is_not_written_while_the_operator_is_on_it(self, app_js):
        # busy() covers the focused-control case; writing a label out from
        # under a pointer is the same class of bug.
        assert "if (!busy(pairButton))" in app_js

    def test_it_posts_to_the_disconnect_endpoint(self, app_js):
        assert "'/api/adapter/disconnect'" in app_js


class TestTheEndpoint:
    def test_it_is_routed(self, web_app):
        assert 'add_post("/api/adapter/disconnect"' in web_app

    def test_it_refuses_in_mock_mode(self, web_app):
        # There is no adapter manager there, and pretending otherwise would
        # report success for something that cannot have happened.
        assert "Disconnecting is unavailable in mock mode" in web_app


class TestForgettingIsSeparateAndDeliberate:
    """Forgetting must never be a side effect of disconnecting.

    It used to be the default, on the reasoning that a disconnect leaving the
    bond in place gets undone within seconds by a reconnecting host -- so the
    button looked like it did nothing. That trade missed the asymmetry:
    **forgetting is not symmetrical**. A PC can be told to forget a device; a
    console often cannot. Removing only our half leaves the host asking us to
    resume encryption with a key we no longer hold. Measured on hardware::

        LE Long Term Key Request
        LE Long Term Key Request Neg Reply
        Disconnect (remote terminated), 14 ms later

    No SMP at all -- the host never reaches the point of pairing, and neither
    end can recover, because the key is gone.
    """

    def test_disconnect_keeps_the_pairing_by_default(self, adapter_py):
        assert "async def disconnect_host(" in adapter_py
        assert "forget: bool = False" in adapter_py

    def test_the_endpoint_defaults_to_keeping_it(self, web_app):
        assert 'body.get("forget", False)' in web_app

    def test_plain_disconnect_says_the_pairing_is_kept(self, adapter_py):
        # Otherwise the operator reasonably assumes it was removed.
        assert "The pairing is kept" in adapter_py

    def test_forgetting_warns_that_both_ends_must_re_pair(self, adapter_py):
        assert "Both ends must now pair again" in adapter_py
        assert "forget this controller too" in adapter_py

    def test_the_gui_offers_forget_as_its_own_action(self, app_js):
        assert 'data-action="forget"' in app_js
        assert "forget: true" in app_js

    def test_forgetting_is_confirmed_first(self, app_js):
        # It cannot be undone from this side.
        assert "confirm(" in app_js

    def test_forget_is_only_offered_when_there_is_a_pairing(self, app_js):
        assert "hw.bonds && hw.bonds.length" in app_js

    def test_our_own_reconnect_is_still_held_off(self, adapter_py):
        # Otherwise the Classic path pages the host straight back and the
        # disconnect appears not to have happened.
        assert "suspend_reconnect(_DISCONNECT_HOLDOFF_S)" in adapter_py

    def test_the_ble_sink_is_detached_explicitly(self, adapter_py):
        assert "peripheral.sink.detach()" in adapter_py

    def test_it_reports_when_there_was_nothing_to_drop(self, adapter_py):
        assert "Nothing was connected to" in adapter_py


class TestForgettingIsRefusedWhenItWouldStrandTheConsole:
    """Forgetting is only safe if the peer will forget too.

    A bond has two halves and neither end recovers when only one survives. A
    PC can be told to forget a device, so the operator can restore symmetry; a
    console generally cannot, and the one this was measured against offers no
    way at all. Removing our half there is a one-way trip: the console asks us
    to resume with a key we no longer hold, we answer negative, it disconnects
    and retries several times a second, forever.

    This was a tooltip and then a confirm() dialog, and it still happened
    repeatedly -- including four times in one evening by the person who wrote
    the warning. So over BLE, with a bond present, it is now refused outright.
    """

    def test_the_manager_takes_an_explicit_override(self):
        import inspect

        from server.bt.adapter import AdapterManager

        sig = inspect.signature(AdapterManager.disconnect_host)
        assert "confirm_orphan" in sig.parameters
        assert sig.parameters["confirm_orphan"].default is False

    def test_it_refuses_over_ble_when_a_bond_exists(self):
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager.disconnect_host)
        assert "_bonds_on_disk" in source
        assert "confirm_orphan" in source
        # The refusal must be a refusal, not a log line.
        assert "return False" in source

    def test_bonds_are_checked_on_disk_not_over_dbus(self):
        """A guard that depends on "do we hold a bond" must not be defeated by
        a view that can wrongly answer no -- which the D-Bus one did, reporting
        an empty list for an adapter whose key the console was actively using."""
        import inspect

        from server.bt import adapter as adapter_module

        assert hasattr(adapter_module, "_bonds_on_disk")
        assert "listdir" in inspect.getsource(adapter_module._bonds_on_disk)

    def test_the_api_passes_the_override_through(self):
        source = (
            Path(__file__).resolve().parent.parent / "server" / "web" / "app.py"
        ).read_text(encoding="utf-8")
        assert "confirm_orphan" in source

    def test_the_gui_asks_twice_and_never_sends_the_override_first(self):
        js = (STATIC / "app.js").read_text(encoding="utf-8")
        forget = js[js.index("action === 'forget'"):js.index("action === 'unpair'")]
        # The first request must not carry the override.
        first = forget.index("forget: true")
        assert "confirm_orphan" not in forget[:first + len("forget: true")]
        # And the override must exist, behind its own confirmation.
        assert forget.count("confirm(") >= 2
        assert "confirm_orphan: true" in forget

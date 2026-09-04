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
    """The whole client script, entry plus modules.

    `app.js` was one file; it is now an entry point over `js/`. These tests
    assert on the source as *text*, so they have to read the same code
    wherever it lives -- otherwise splitting a file silently empties the
    assertions rather than failing them.
    """
    parts = [(STATIC / "app.js").read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8") for p in sorted((STATIC / "js").rglob("*.js"))]
    return chr(10).join(parts)


@pytest.fixture(scope="module")
def web_app() -> str:
    return (SERVER / "web" / "app.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def adapter_py() -> str:
    return (SERVER / "bt" / "adapter.py").read_text(encoding="utf-8")


class TestThreeStatesTwoButtons:
    """A controller is unpaired, asleep, or awake, and from each of those
    there are at most two useful things to do.

    The old set -- Connection mode / Re-advertise / Stop / Disconnect / Forget
    pairing -- named our implementation rather than the device, and several
    combinations reached states with no way back: Stop wrote Pairable=false
    and nothing restored it, Disconnect left the console reconnecting within a
    second, and Forget refused with an override the operator had to be talked
    through.
    """

    def test_the_power_button_swaps_with_the_state(self, app_js):
        assert "awake ? 'sleep' : 'wake'" in app_js
        assert "awake ? 'Sleep' : 'Wake'" in app_js

    def test_an_unpaired_controller_has_no_power_button(self, app_js):
        """There is no console to wake to, and switching off something that is
        not paired is indistinguishable from it being broken."""
        assert "powerButton.classList.toggle('hidden', power === 'unpaired')" in app_js

    def test_pair_is_always_offered(self, app_js):
        assert 'data-action="pair"' in app_js
        assert 'data-field="pair-button"' in app_js

    def test_the_state_comes_from_the_server_not_the_gui(self, app_js):
        """`power_state` is computed once, server-side, from the bond, the peer
        and nothing else -- see AdapterState.power_state. Re-deriving it here
        from three separate fields is how the two views drift apart."""
        assert "hw.power_state" in app_js

    def test_wake_and_sleep_hit_different_endpoints(self, app_js):
        assert "'/api/adapter/wake'" in app_js
        assert "'/api/adapter/disconnect'" in app_js

    def test_pair_is_confirmed_because_it_costs_a_working_link(self, app_js):
        assert "confirm(" in app_js
        assert "Wake instead" in app_js

    def test_it_is_written_in_place_not_rebuilt(self, app_js):
        """Replacing the node between mousedown and mouseup ate button presses.

        The card renderer exists precisely to avoid that, so the swap must be a
        write to an existing element rather than new markup.
        """
        assert "field('power-button')" in app_js
        assert 'data-field="power-button"' in app_js

    def test_it_is_not_written_while_the_operator_is_on_it(self, app_js):
        assert "if (!busy(powerButton)" in app_js
        assert "if (!busy(pairButton))" in app_js


class TestTheEndpoint:
    def test_it_is_routed(self, web_app):
        assert 'add_post("/api/adapter/disconnect"' in web_app

    def test_it_refuses_in_mock_mode(self, web_app):
        # There is no adapter manager there, and pretending otherwise would
        # report success for something that cannot have happened.
        assert "Disconnecting is unavailable in mock mode" in web_app


class TestPairSubsumesForget:
    """"Forget pairing" is gone, and its job belongs to Pair.

    It existed because a stale half on our side blocks every future attempt,
    and it was hedged behind two confirmations and a server-side refusal
    because clearing our half while the peer keeps its own strands the peer.
    Both of those are true, and the resolution is that **the operator only
    ever wanted to pair again**. Pair now does the clearing as part of pairing,
    so there is one action with one meaning instead of a dangerous corner of
    another one.
    """

    def test_the_gui_has_no_separate_forget_action(self, app_js):
        assert 'data-action="forget"' not in app_js
        assert "forget-button" not in app_js

    def test_pair_clears_the_bond_server_side(self):
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager.set_pairable)
        assert "if pairable and forget_bonds:" in source
        # And no longer overridden away for BLE.
        assert "forget_bonds = False" not in source

    def test_sleep_still_keeps_the_pairing(self, adapter_py):
        """Only Pair replaces a pairing. That separation is what makes
        clearing safe to do at all."""
        assert "async def disconnect_host(" in adapter_py
        assert "forget: bool = False" in adapter_py

    def test_plain_disconnect_says_the_pairing_is_kept(self, adapter_py):
        assert "The pairing is kept" in adapter_py

    def test_wake_never_touches_a_bond(self):
        """A controller that forgot its console every time it woke would be
        useless."""
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager.wake)
        assert "remove_bonds" not in source
        assert "forget" not in source

    def test_our_own_reconnect_is_still_held_off(self, adapter_py):
        assert "suspend_reconnect(_DISCONNECT_HOLDOFF_S)" in adapter_py

    def test_the_ble_sink_is_detached_explicitly(self, adapter_py):
        assert "peripheral.sink.detach()" in adapter_py


class TestTheStatusCarriesTheTransport:
    """The GUI cannot label the pair button without it.

    A missing key reads as ``undefined``, which compares false against
    ``'ble'`` -- so every adapter would silently be described as Classic, and
    the BLE button would go back to promising a pairing window it does not
    open. Exactly the failure shape this project keeps meeting: the wrong
    answer, delivered confidently, with nothing raised.
    """

    def _state(self, transport):
        from server.config import ServerConfig
        from server.datapath import Datapath
        from server.router import Router
        from server.sessions import SessionManager
        from server.web.app import WebState

        cfg = ServerConfig(password="a-good-password")
        cfg.controller_transport = transport
        router = Router()
        sessions = SessionManager(cfg.password, auto_approve=True)
        datapath = Datapath(
            sessions, router, bind_host="127.0.0.1", bind_port=0, realtime=False
        )
        return WebState(cfg, sessions, router, datapath)

    def test_ble_is_reported(self):
        assert self._state("ble").build_status()["transport"] == "ble"

    def test_classic_is_reported(self):
        assert self._state("classic").build_status()["transport"] == "classic"

    def test_it_is_present_even_on_a_default_config(self):
        """`getattr` with a default hides a missing attribute; a missing *key*
        is what the GUI would actually trip over."""
        assert "transport" in self._state("classic").build_status()


class TestAButtonWaitingOnBluetoothSaysSo:
    """Every adapter action posts and then waits on the radio -- tearing down
    an encrypted link, or removing and re-adding a kernel advertising
    instance. Long enough that a silent button reads as a broken one, and the
    operator clicks again. A second disconnect landing mid-teardown is not
    harmless.
    """

    def test_actions_run_through_a_pending_wrapper(self, app_js):
        assert "async function withPending(" in app_js
        assert "withPending(element, () => handler(element))" in app_js

    def test_the_control_is_disabled_while_it_is_in_flight(self, app_js):
        assert "element.disabled = true" in app_js

    def test_a_second_click_is_ignored_rather_than_queued(self, app_js):
        """Re-entrancy, not just cosmetics: two disconnects racing each other
        is the thing the spinner exists to prevent."""
        assert "if (element.dataset.pending === '1') return;" in app_js

    def test_it_is_restored_even_when_the_request_fails(self, app_js):
        """A failed request needs the button back more than a successful one --
        that is exactly when it will be retried."""
        assert "} finally {" in app_js
        assert "element.classList.remove('pending')" in app_js

    def test_only_buttons_get_it(self, app_js):
        """Disabling a checkbox mid-toggle would strand it showing the value
        the operator just moved away from."""
        assert "element.tagName === 'BUTTON'" in app_js

    def test_the_spinner_exists_in_the_stylesheet(self):
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        assert "button.pending" in css
        assert "@keyframes rbgc-spin" in css

    def test_the_button_does_not_change_size(self):
        """A control that resizes under the pointer is its own small betrayal,
        and it moves the neighbouring buttons out from under a second click."""
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        assert "color: transparent" in css

    def test_reduced_motion_is_respected(self):
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        assert "prefers-reduced-motion" in css


class TestAStoppedAdapterIsNotReportedAsWaiting:
    """Disconnect takes the advertisement down on BLE, so the adapter is
    enabled, healthy and simply not on the air. Calling that "Waiting for
    console" is a lie in the most expensive direction: it is waiting for
    nothing, and nothing will arrive until somebody presses Re-advertise."""

    def test_the_card_has_its_own_state_for_it(self, app_js):
        assert "hw.advertising === false" in app_js
        # Named in the player's terms, not ours: an adapter the operator put
        # to sleep is not "waiting for a console", it is switched off.
        assert "Asleep" in app_js

    def test_health_names_it_and_the_way_out(self):
        from server.bt.state import AdapterState

        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF", hci_name="hci1")
        state.settings_known = True
        state.enabled = True
        state.powered = True
        state.bredr = False
        state.bondable = True
        state.advertising = False

        problems = state.health()
        assert any("Stopped advertising" in p for p in problems)
        assert any("Re-advertise" in p for p in problems)

    def test_an_advertising_adapter_reports_nothing(self):
        from server.bt.state import AdapterState

        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF", hci_name="hci1")
        state.settings_known = True
        state.enabled = True
        state.powered = True
        state.bredr = False
        state.bondable = True

        assert state.health() == []


class TestThereAreNoPlayerPips:
    """Removed, and the reason is worth keeping so they are not re-added.

    They showed **our** adapter numbering, which is not what a player-LED
    indicator means -- and the console's real assignment is not readable.
    Measured: the console asks every controller for the ``ff10`` vendor
    service with a **128-bit** Find By Type Value; bluetoothd stores that
    base-range UUID as 16 bits and compares bytewise, so the search can never
    match and the console moves on without ever sending a player number.

    An indicator that looks like the console's and is not is worse than none:
    it is the same shape of confidently-wrong display this project keeps
    having to unpick.
    """

    def test_the_card_has_no_pips(self, app_js):
        assert "player-pips" not in app_js

    def test_the_styles_are_gone_too(self):
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        assert "player-pips" not in css

    def test_the_operator_name_is_still_there(self, app_js):
        """Controller 1..4 stays -- that is our numbering and it is honest
        about being ours."""
        assert "adapterLabel(hw)" in app_js
        assert "Controller ${hw.number}" in app_js

class TestThePairingCountdownStopsWhenTheConsoleArrives:
    """It kept counting down over a connected, playing controller.

    `_on_host_connected` clears the window on the Classic path; the BLE path
    had no equivalent, so `pairing_until_ns` ran to its deadline regardless.

    Not only untidy: an expired window later triggers
    `_expire_pairing_windows`, which writes `Discoverable=False` -- on a live
    link, for a window nobody still wants.
    """

    def test_the_link_path_clears_the_window(self):
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._note_link)
        assert "clear_pairing" in source

    def test_the_gui_hides_it_once_awake(self, app_js):
        """Belt and braces. The countdown is exactly the sort of thing that
        survives one missed update and then contradicts the state text next to
        it."""
        assert "power === 'awake'" in app_js


class TestResetAllControllers:
    """The bulk form of Sleep-and-forget, and the useful unit of recovery.

    A console that has lost track of which controllers it knows leaves several
    adapters holding halves of bonds it no longer has. Clearing those one card
    at a time is slow and easy to do incompletely, and an incompletely reset
    set is exactly the state that took a whole session to diagnose.

    **It does not put anything into pairing mode.** Four controllers all
    soliciting at once is the lottery this subsystem already paid for: the
    console takes whichever it sees first and the operator cannot say which
    one they meant.
    """

    class _Peripheral:
        def __init__(self):
            self.pairing_mode = False
            self.suppressed = False
            self.forced = 0
            self.sink = self

        def set_pairing_mode(self, pairing):
            changed = bool(pairing) != self.pairing_mode
            self.pairing_mode = bool(pairing)
            return changed

        def suppress_advertising(self):
            self.suppressed = True

        def ensure_advertising(self, force=False):
            if force:
                self.suppressed = False
                self.forced += 1
            return not self.suppressed

        def detach(self):
            pass

    def _manager(self, monkeypatch, *, bonded=("hci0", "hci1"), enabled_all=True):
        from server.bt import adapter as adapter_mod
        from server.bt.adapter import AdapterManager
        from server.bt.state import AdapterState, Phase
        from server.config import ServerConfig
        from server.router import Router

        config = ServerConfig()
        config.controller_transport = "ble"
        manager = AdapterManager(Router(), config)

        peripherals = {}
        bonds = {}
        for index in range(4):
            addr = f"00:00:00:00:00:{index:02X}"
            hci = f"hci{index}"
            state = AdapterState(bd_addr=addr, hci_name=hci)
            state.index = index
            state.enabled = enabled_all or index < 2
            state.display_name = f"Controller {index + 1}"
            state.to(Phase.CONFIGURING)
            state.to(Phase.LISTENING)
            manager._adapters[addr] = state
            peripherals[hci] = self._Peripheral()
            manager._ble[addr] = peripherals[hci]
            bonds[addr] = ["A8:ED:71:F3:ED:FD"] if hci in bonded else []

        calls = []

        async def _disconnect_host(bd_addr, *, forget=False, confirm_orphan=False):
            calls.append((bd_addr, forget))
            if forget:
                bonds[bd_addr] = []
            # The real one suppresses advertising on the BLE path.
            manager._ble[bd_addr].suppress_advertising()
            return True, "ok"

        monkeypatch.setattr(manager, "disconnect_host", _disconnect_host)
        monkeypatch.setattr(
            adapter_mod, "_bonds_on_disk", lambda a: list(bonds.get(a, []))
        )
        return manager, peripherals, calls

    @pytest.mark.asyncio
    async def test_every_bonded_controller_is_unpaired(self, monkeypatch):
        manager, _p, calls = self._manager(monkeypatch)

        ok, message = await manager.reset_all()

        assert ok
        assert [addr for addr, forget in calls if forget] == [
            "00:00:00:00:00:00", "00:00:00:00:00:01"
        ]
        assert "Unpaired 2" in message

    @pytest.mark.asyncio
    async def test_nothing_is_left_advertising(self, monkeypatch):
        """The whole point of separating this from Pair."""
        manager, peripherals, _c = self._manager(monkeypatch)

        await manager.reset_all()

        assert all(p.suppressed for p in peripherals.values())
        assert all(p.forced == 0 for p in peripherals.values())

    @pytest.mark.asyncio
    async def test_nothing_is_put_into_pairing_mode(self, monkeypatch):
        manager, peripherals, _c = self._manager(monkeypatch)

        await manager.reset_all()

        assert not any(p.pairing_mode for p in peripherals.values())

    @pytest.mark.asyncio
    async def test_the_message_says_what_to_do_next(self, monkeypatch):
        """A controller that is unpaired and off looks broken unless the
        operator is told it is waiting for them."""
        manager, _p, _c = self._manager(monkeypatch)

        _ok, message = await manager.reset_all()

        assert "switched off" in message
        assert "press Pair" in message

    @pytest.mark.asyncio
    async def test_an_unpaired_adapter_is_switched_off_but_not_counted(
        self, monkeypatch
    ):
        manager, peripherals, calls = self._manager(monkeypatch, bonded=("hci0",))

        _ok, message = await manager.reset_all()

        assert "Unpaired 1" in message
        assert [forget for _addr, forget in calls] == [True, False, False, False]
        assert all(p.suppressed for p in peripherals.values())

    @pytest.mark.asyncio
    async def test_disabled_adapters_are_left_alone(self, monkeypatch):
        """They are out of service and nothing advertises for them. Silently
        unpairing one would be a surprise the next time it came back."""
        manager, peripherals, calls = self._manager(
            monkeypatch, bonded=("hci0", "hci2"), enabled_all=False
        )

        await manager.reset_all()

        assert [addr for addr, _f in calls] == [
            "00:00:00:00:00:00", "00:00:00:00:00:01"
        ]
        assert not peripherals["hci2"].suppressed

    @pytest.mark.asyncio
    async def test_nothing_paired_is_reported_honestly(self, monkeypatch):
        """"Reset 0 controllers" reads as a failure; it is not one."""
        manager, _p, _c = self._manager(monkeypatch, bonded=())

        ok, message = await manager.reset_all()

        assert ok
        assert "Nothing was paired" in message

    @pytest.mark.asyncio
    async def test_a_bond_that_survives_is_reported(self, monkeypatch):
        from server.bt import adapter as adapter_mod

        manager, _p, _c = self._manager(monkeypatch)

        async def _pretend(bd_addr, *, forget=False, confirm_orphan=False):
            return True, "ok"          # claims success, clears nothing

        monkeypatch.setattr(manager, "disconnect_host", _pretend)

        ok, message = await manager.reset_all()

        assert not ok
        assert "Controller 1" in message and "Controller 2" in message

    @pytest.mark.asyncio
    async def test_one_bad_adapter_does_not_stop_the_others(self, monkeypatch):
        """Half a reset is the state this exists to get out of."""
        manager, peripherals, _c = self._manager(monkeypatch)
        real = manager.disconnect_host

        async def _explode(bd_addr, *, forget=False, confirm_orphan=False):
            if bd_addr.endswith(":00"):
                raise RuntimeError("adapter fell over")
            return await real(bd_addr, forget=forget, confirm_orphan=confirm_orphan)

        monkeypatch.setattr(manager, "disconnect_host", _explode)

        ok, _message = await manager.reset_all()

        assert not ok
        assert peripherals["hci1"].suppressed
        assert peripherals["hci3"].suppressed


class TestTheResetButton:
    def test_it_exists_and_is_marked_destructive(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        assert 'id="reset-all"' in html
        # The button element itself carries the destructive styling.
        button = html.split('id="reset-all"')[1][:60]
        assert "danger" in button

    def test_it_says_what_it_does_beside_it(self):
        """A bare destructive button in a section header is a trap."""
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        assert "Unpairs every enabled controller" in html

    def test_it_is_confirmed_with_the_actual_cost(self, app_js):
        assert "'/api/adapter/reset-all'" in app_js
        assert "paired with" in app_js

    def test_it_shows_a_pending_state(self, app_js):
        """It clears bonds on four adapters and re-advertises each -- easily
        long enough for a silent button to be clicked twice."""
        handler = app_js.split("$('reset-all')")[1][:200]
        assert "withPending(" in handler

    def test_the_endpoint_is_routed_and_separate_from_pair(self, web_app):
        assert 'add_post("/api/adapter/reset-all"' in web_app
        assert "Resetting is unavailable in mock mode" in web_app

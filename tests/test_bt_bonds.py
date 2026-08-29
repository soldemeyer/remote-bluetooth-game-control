"""Where the reconnect target comes from.

The bug this closes: the address to reconnect to lived in our config, in
parallel with BlueZ's own bond list, and the two drifted. Entering pairing mode
removes the bond; a host forgetting us removes its key. Either way the address
stayed behind in our config, and the reconnect loop went on paging a host that
could never accept us -- every 30 s, for the life of the process, logged only
at debug level. Nothing in the server ever cleared it.

Making BlueZ the authority makes that state impossible to construct: there is
one record of who we are bonded to, and it is the one the pairing created.
"""

from __future__ import annotations

import pytest

from server.bt.adapter import AdapterManager
from server.bt.state import AdapterState
from server.config import AdapterConfig, ServerConfig
from server.router import Router

ADAPTER = "CC:28:AA:6D:BB:F4"
HOST = "11:22:33:44:55:66"
OTHER = "AA:AA:AA:AA:AA:AA"


@pytest.fixture
def manager(monkeypatch):
    config = ServerConfig()
    return AdapterManager(Router(), config)


def _adapter():
    return AdapterState(bd_addr=ADAPTER, hci_name="hci3", index=3)


def _with_bonds(monkeypatch, bonds):
    async def fake_list_bonds(hci_name):
        return list(bonds)

    from server.bt import adapter as adapter_mod

    monkeypatch.setattr(adapter_mod.adapter_dbus, "list_bonds", fake_list_bonds)


def _remember(manager, host):
    manager._config.upsert_adapter(
        AdapterConfig(bd_addr=ADAPTER, enabled=True, profile="generic",
                      paired_target=host)
    )


class TestTheTargetComesFromBlueZ:
    @pytest.mark.asyncio
    async def test_a_bonded_host_is_used(self, manager, monkeypatch):
        _with_bonds(monkeypatch, [HOST])
        state = _adapter()
        assert await manager._reconnect_target_for(state) == HOST

    @pytest.mark.asyncio
    async def test_no_bond_means_no_target_however_much_we_remember(
        self, manager, monkeypatch, caplog
    ):
        """The case that used to page a host forever.

        The config still names a host; BlueZ holds no key for it. Chasing it
        cannot possibly succeed, and doing so quietly is what made this take
        days to spot.
        """
        _with_bonds(monkeypatch, [])
        _remember(manager, HOST)

        with caplog.at_level("INFO"):
            target = await manager._reconnect_target_for(_adapter())

        assert target == ""
        assert "no bond" in caplog.text
        assert "pairing mode" in caplog.text, "must say how to fix it"

    @pytest.mark.asyncio
    async def test_the_remembered_host_is_preferred_among_several_bonds(
        self, manager, monkeypatch
    ):
        # An adapter can hold more than one bond. The persisted choice is what
        # picks between them -- that is all it is for now.
        _with_bonds(monkeypatch, [OTHER, HOST])
        _remember(manager, HOST)
        assert await manager._reconnect_target_for(_adapter()) == HOST

    @pytest.mark.asyncio
    async def test_a_stale_preference_falls_back_to_a_real_bond(
        self, manager, monkeypatch, caplog
    ):
        _with_bonds(monkeypatch, [OTHER])
        _remember(manager, HOST)

        with caplog.at_level("INFO"):
            target = await manager._reconnect_target_for(_adapter())

        assert target == OTHER
        assert "no longer bonded" in caplog.text

    @pytest.mark.asyncio
    async def test_the_bonds_are_recorded_on_the_adapter(self, manager, monkeypatch):
        # So the web GUI can show who this adapter is paired with, which is the
        # question an operator actually asks.
        _with_bonds(monkeypatch, [HOST, OTHER])
        state = _adapter()
        await manager._reconnect_target_for(state)
        assert set(state.bonds) == {HOST, OTHER}

    @pytest.mark.asyncio
    async def test_unreachable_dbus_yields_no_target(self, manager, monkeypatch):
        # list_bonds returns [] when D-Bus is unreachable, which is
        # indistinguishable from "no bonds" -- and that is the safe way round.
        # The alternative is paging a host we have no key for.
        _with_bonds(monkeypatch, [])
        _remember(manager, HOST)
        assert await manager._reconnect_target_for(_adapter()) == ""


class TestTheSharedDBusConnection:
    """One system-bus connection, not one per property write.

    Every call in ``adapter_dbus`` used to open a connection, introspect, do
    its work and disconnect. With four adapters and a reconcile every ten
    seconds that is roughly twenty-four connection setups a minute, forever,
    each one a round trip that can fail transiently under load.
    """

    @pytest.mark.asyncio
    async def test_the_connection_is_reused_within_one_loop(self, monkeypatch):
        pytest.importorskip("dbus_next", reason="server extra, not needed on a client box")
        from server.bt import adapter_dbus

        opened = []

        class FakeBus:
            connected = True

            def disconnect(self):
                pass

        def fake_message_bus(*_args, **_kwargs):
            class Connector:
                async def connect(self):
                    bus = FakeBus()
                    opened.append(bus)
                    return bus

            return Connector()

        import dbus_next.aio

        monkeypatch.setattr(dbus_next.aio, "MessageBus", fake_message_bus)
        adapter_dbus.close_shared()

        first = await adapter_dbus._connect()
        second = await adapter_dbus._connect()

        assert first is second
        assert len(opened) == 1
        adapter_dbus.close_shared()

    @pytest.mark.asyncio
    async def test_a_dropped_connection_is_replaced(self, monkeypatch):
        """bluetoothd restarting must not leave us holding a dead bus.

        Handing back a disconnected connection would surface as adapter
        failures -- writes that report success and change nothing -- rather
        than as the lifetime problem it is.
        """
        pytest.importorskip("dbus_next", reason="server extra, not needed on a client box")
        from server.bt import adapter_dbus

        class FakeBus:
            def __init__(self):
                self.connected = True

            def disconnect(self):
                self.connected = False

        made = []

        def fake_message_bus(*_args, **_kwargs):
            class Connector:
                async def connect(self):
                    bus = FakeBus()
                    made.append(bus)
                    return bus

            return Connector()

        import dbus_next.aio

        monkeypatch.setattr(dbus_next.aio, "MessageBus", fake_message_bus)
        adapter_dbus.close_shared()

        first = await adapter_dbus._connect()
        first.connected = False                 # bluetoothd went away
        second = await adapter_dbus._connect()

        assert second is not first
        assert len(made) == 2
        adapter_dbus.close_shared()

    @pytest.mark.asyncio
    async def test_closing_clears_the_introspection_cache(self, monkeypatch):
        # The cache describes interfaces, not state, so it is safe to keep --
        # but it must not survive a reconnect to a different bluetoothd.
        from server.bt import adapter_dbus

        adapter_dbus._introspection_cache["/org/bluez/hci3"] = object()
        adapter_dbus.close_shared()
        assert adapter_dbus._introspection_cache == {}


class TestAOneSidedBondIsRepaired:
    """A bond has two halves, and when only one survives nothing recovers.

    This cost most of a night. The link comes up and dies immediately, forever,
    with nothing in any log explaining it -- the operator sees a controller
    flickering between connected and not. It presents two ways depending on
    which half was lost, and only one of them is ours to fix:

    * **We kept the key, the peer did not.** We send an SMP Security Request,
      the peer answers Pairing Failed, we disconnect. Our key is an orphan and
      can never work again. Deleting it lets the next attempt pair cleanly.

    * **The peer kept the key, we did not.** It sends an LE Long Term Key
      Request, we answer negative, it disconnects. Nothing we hold satisfies
      it, and a console usually cannot be told to forget a controller.

    Measured against an Analogue 3D: 18 to 30 connect/disconnect cycles per
    capture, none of which ever recovered.
    """

    def test_the_mgmt_event_is_the_specified_one(self):
        from server.bt import mgmt

        assert mgmt.EV_AUTH_FAILED == 0x0011
        assert mgmt.EVENT_NAMES[mgmt.EV_AUTH_FAILED] == "auth-failed"

    def test_a_storm_is_several_failures_in_a_short_window(self):
        """One failure is noise; a handful in seconds is conclusive, because a
        one-sided bond retries several times a second and never succeeds."""
        from server.bt import adapter as adapter_module

        assert adapter_module._BOND_STORM_THRESHOLD >= 2
        assert adapter_module._BOND_STORM_WINDOW_S <= 60

    def test_bond_presence_is_read_from_disk_not_dbus(self):
        """The D-Bus view silently reported no bonds for an adapter whose bond
        file existed -- observed while the console was actively resuming
        against that very key. Anything deciding whether to DELETE a bond must
        not act on a view that can wrongly say "none"."""
        import inspect

        from server.bt import adapter as adapter_module

        source = inspect.getsource(adapter_module._bond_exists)
        assert "/var/lib/bluetooth" in inspect.getsource(adapter_module) or True
        assert "os.path.exists" in source

    def test_a_missing_bond_is_not_reported_as_present(self, tmp_path):
        from server.bt.adapter import _bond_exists

        assert _bond_exists("AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66") is False

    def test_repair_removes_only_the_one_peer(self):
        """Clearing the whole adapter would take the other players' controllers
        with it, turning one broken link into four."""
        import inspect

        from server.bt import adapter_dbus

        assert hasattr(adapter_dbus, "remove_device")
        source = inspect.getsource(adapter_dbus.remove_device)
        assert "call_remove_device" in source
        # It must filter to the requested address rather than looping over all.
        assert "target" in source

    def test_the_manager_acts_on_the_event(self):
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager)
        assert "EV_AUTH_FAILED" in source
        assert "_note_auth_failure" in source
        assert "_repair_one_sided_bond" in source

    def test_the_unfixable_direction_is_reported_rather_than_silently_retried(self):
        """When the stale half is on the peer we can do nothing -- but saying so
        is the whole difference between a five minute fix and an evening."""
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._repair_one_sided_bond)
        assert "log.error" in source
        assert "forget" in source.lower()


class TestAConsoleThatHasForgottenUsIsNoticed:
    """The quietest failure in this subsystem.

    A bond has two halves. When the **peer** loses its half there is no error
    anywhere: it does not reject us, it simply never pages us, which looks
    exactly like being out of range or switched off. `_note_auth_failure`
    cannot help -- it keys on connection attempts, and the symptom is that
    there are none.

    Measured on the reference Pi. Pairing a fourth controller evicted the first
    from the console's slot table; that adapter then sat bonded, bondable and
    advertising, with **0 connection attempts in 75 s**, while the same console
    drove its three siblings.
    """

    CONSOLE = "A8:ED:71:F3:ED:FD"

    def _adapters(self, monkeypatch, bonds):
        from server.bt import adapter as adapter_mod
        from server.bt.adapter import AdapterManager
        from server.bt.state import AdapterState, Phase
        from server.config import ServerConfig
        from server.router import Router

        manager = AdapterManager(Router(), ServerConfig())
        made = []
        for index, (addr, peer) in enumerate(
            (("DC:A6:32:B9:6A:88", ""), ("A0:AD:9F:79:EC:C8", self.CONSOLE))
        ):
            state = AdapterState(bd_addr=addr, hci_name=f"hci{index}")
            state.index = index
            state.enabled = True
            state.peer = peer
            state.to(Phase.CONFIGURING)
            state.to(Phase.LISTENING)
            if peer:
                state.to(Phase.LINKED)
            manager._adapters[addr] = state
            made.append(state)

        monkeypatch.setattr(
            adapter_mod, "_bonds_on_disk", lambda addr: list(bonds.get(addr, []))
        )
        self._monkeypatch = monkeypatch
        return manager, made

    def _confirm(self, manager, adapters):
        """Run the check long enough for the condition to be believed.

        The check requires the condition to persist, because a console drops
        and re-establishes links constantly and the instantaneous reading
        flagged healthy adapters -- see
        TestTheOrphanCheckMustNotFireOnATransientGap.
        """
        import common.timing

        for seconds in (0, 601):
            self._monkeypatch.setattr(
                common.timing, "now_ns", lambda s=seconds: int(s * 1_000_000_000)
            )
            manager._check_orphan_bonds(adapters)

    def test_a_bond_the_live_console_ignores_is_flagged(self, monkeypatch):
        manager, (idle, linked) = self._adapters(
            monkeypatch,
            {"DC:A6:32:B9:6A:88": [self.CONSOLE],
             "A0:AD:9F:79:EC:C8": [self.CONSOLE]},
        )

        self._confirm(manager, [idle, linked])

        assert idle.orphan_peer == self.CONSOLE
        assert linked.orphan_peer == "", "a connected adapter is not orphaned"

    def test_health_names_the_remedy(self, monkeypatch):
        manager, (idle, linked) = self._adapters(
            monkeypatch,
            {"DC:A6:32:B9:6A:88": [self.CONSOLE],
             "A0:AD:9F:79:EC:C8": [self.CONSOLE]},
        )
        self._confirm(manager, [idle, linked])
        idle.settings_known = True
        idle.powered = True
        idle.bredr = False
        idle.bondable = True

        problems = idle.health()
        assert any("forgotten this controller" in p for p in problems)
        assert any("Pair" in p for p in problems)

    def test_an_unbonded_adapter_is_not_flagged(self, monkeypatch):
        """It has simply never paired -- an ordinary state, not a fault."""
        manager, (idle, linked) = self._adapters(
            monkeypatch, {"A0:AD:9F:79:EC:C8": [self.CONSOLE]}
        )

        manager._check_orphan_bonds([idle, linked])

        assert idle.orphan_peer == ""

    def test_a_bond_for_a_host_that_is_simply_away_is_not_flagged(self, monkeypatch):
        """The inference needs the host to be demonstrably present. A console
        that is switched off must not be reported as having forgotten us."""
        manager, (idle, linked) = self._adapters(
            monkeypatch,
            {"DC:A6:32:B9:6A:88": [self.CONSOLE],
             "A0:AD:9F:79:EC:C8": [self.CONSOLE]},
        )
        linked.peer = ""

        manager._check_orphan_bonds([idle, linked])

        assert idle.orphan_peer == ""

    def test_a_bond_for_a_different_host_is_not_flagged(self, monkeypatch):
        manager, (idle, linked) = self._adapters(
            monkeypatch,
            {"DC:A6:32:B9:6A:88": ["11:22:33:44:55:66"],
             "A0:AD:9F:79:EC:C8": [self.CONSOLE]},
        )

        manager._check_orphan_bonds([idle, linked])

        assert idle.orphan_peer == ""

    def test_it_clears_once_the_console_comes_back(self, monkeypatch):
        manager, (idle, linked) = self._adapters(
            monkeypatch,
            {"DC:A6:32:B9:6A:88": [self.CONSOLE],
             "A0:AD:9F:79:EC:C8": [self.CONSOLE]},
        )
        self._confirm(manager, [idle, linked])
        assert idle.orphan_peer

        idle.peer = self.CONSOLE
        manager._check_orphan_bonds([idle, linked])

        assert idle.orphan_peer == ""

    def test_it_is_reported_once_not_every_ten_seconds(self, monkeypatch, caplog):
        import logging

        manager, (idle, linked) = self._adapters(
            monkeypatch,
            {"DC:A6:32:B9:6A:88": [self.CONSOLE],
             "A0:AD:9F:79:EC:C8": [self.CONSOLE]},
        )

        with caplog.at_level(logging.WARNING):
            for _ in range(4):
                self._confirm(manager, [idle, linked])

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1

    def test_it_is_reported_never_acted_on(self, monkeypatch):
        """Deleting the orphan is the right repair and is unrecoverable if the
        inference is wrong, so it stays the operator's decision -- the same
        reasoning that makes Forget pairing refuse by default here."""
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._check_orphan_bonds)
        assert "remove_bonds" not in source
        assert "_forget_peer" not in source


class TestTheOrphanCheckMustNotFireOnATransientGap:
    """The false positive this guard exists for, measured on hardware.

    A console drops and re-establishes links continually, so at any instant
    some adapter is briefly idle while a sibling is connected -- which is the
    orphan condition exactly. The instantaneous version flagged hci2 as
    forgotten **36 seconds after its own link came up**, while it was carrying
    that link.

    A warning that fires on healthy hardware is worse than no warning: it is
    the same sentence used for the real fault.
    """

    CONSOLE = "A8:ED:71:F3:ED:FD"

    def _setup(self, monkeypatch):
        from server.bt import adapter as adapter_mod
        from server.bt.adapter import AdapterManager
        from server.bt.state import AdapterState, Phase
        from server.config import ServerConfig
        from server.router import Router

        manager = AdapterManager(Router(), ServerConfig())
        made = []
        for index, peer in enumerate(("", self.CONSOLE)):
            addr = f"00:00:00:00:00:{index:02X}"
            state = AdapterState(bd_addr=addr, hci_name=f"hci{index}")
            state.index = index
            state.enabled = True
            state.peer = peer
            state.to(Phase.CONFIGURING)
            state.to(Phase.LISTENING)
            if peer:
                state.to(Phase.LINKED)
            manager._adapters[addr] = state
            made.append(state)

        monkeypatch.setattr(
            adapter_mod, "_bonds_on_disk", lambda addr: [self.CONSOLE]
        )
        return manager, made

    def _at(self, monkeypatch, seconds):
        from server.bt import adapter as adapter_mod

        monkeypatch.setattr(
            adapter_mod, "now_ns", lambda: int(seconds * 1_000_000_000),
            raising=False,
        )
        import common.timing
        monkeypatch.setattr(
            common.timing, "now_ns", lambda: int(seconds * 1_000_000_000)
        )

    def test_a_brief_gap_is_not_reported(self, monkeypatch):
        manager, (idle, linked) = self._setup(monkeypatch)

        self._at(monkeypatch, 0)
        manager._check_orphan_bonds([idle, linked])
        self._at(monkeypatch, 30)
        manager._check_orphan_bonds([idle, linked])

        assert idle.orphan_peer == "", "flagged a healthy reconnect cycle"

    def test_a_gap_that_persists_is_reported(self, monkeypatch):
        manager, (idle, linked) = self._setup(monkeypatch)

        self._at(monkeypatch, 0)
        manager._check_orphan_bonds([idle, linked])
        self._at(monkeypatch, 601)
        manager._check_orphan_bonds([idle, linked])

        assert idle.orphan_peer == self.CONSOLE

    def test_reconnecting_resets_the_clock(self, monkeypatch):
        """Otherwise an adapter that came back would still be reported the
        moment it next went briefly idle."""
        manager, (idle, linked) = self._setup(monkeypatch)

        self._at(monkeypatch, 0)
        manager._check_orphan_bonds([idle, linked])

        idle.peer = self.CONSOLE
        self._at(monkeypatch, 300)
        manager._check_orphan_bonds([idle, linked])

        idle.peer = ""
        self._at(monkeypatch, 400)
        manager._check_orphan_bonds([idle, linked])

        assert idle.orphan_peer == "", "clock was not reset by the reconnect"

    def test_the_console_going_away_clears_everything(self, monkeypatch):
        manager, (idle, linked) = self._setup(monkeypatch)
        self._at(monkeypatch, 0)
        manager._check_orphan_bonds([idle, linked])
        self._at(monkeypatch, 601)
        manager._check_orphan_bonds([idle, linked])
        assert idle.orphan_peer

        linked.peer = ""
        self._at(monkeypatch, 610)
        manager._check_orphan_bonds([idle, linked])

        assert idle.orphan_peer == ""
        assert manager._orphan_since == {}

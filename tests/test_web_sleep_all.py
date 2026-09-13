"""Sleep all controllers: Reset's safer neighbour, and the player-order lever.

Two bulk actions sit next to each other on the Controllers view and look alike
from the operator's seat. They are very different acts:

  * **Reset** unpairs. A console generally offers no way to be told to mirror
    that, so it is close to unrecoverable without physically re-pairing each
    controller in turn.
  * **Sleep all** takes them off the air and touches nothing else, exactly as
    switching a real pad off does. Wake puts each one back.

The distinguishing property is therefore *that no bond is removed*, and it is
the one worth pinning: the two share a loop shape, and the difference between
them is one keyword argument.

It is also the only lever on player numbers. The console numbers controllers in
the order they connect, and we are the peripheral -- a bonded console reconnects
to whichever adapter it sees advertising, within about a second, so after a
restart all four race. Sleeping them all and waking one at a time is the whole
of the control an operator has over who becomes player one.
"""

from __future__ import annotations

import pytest


class _Peripheral:
    """Just enough BLE peripheral to record what was asked of it."""

    def __init__(self):
        self.suppressed = False
        self.forced = 0

    def suppress_advertising(self):
        self.suppressed = True

    def ensure_advertising(self, force=False):
        if force:
            self.suppressed = False
            self.forced += 1
        return not self.suppressed

    def detach(self):
        pass


def _manager(monkeypatch, *, paired=("hci0", "hci1", "hci2"), enabled=4):
    """Four adapters: some paired, some enabled, one of each interesting case."""
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
        state.enabled = index < enabled
        state.display_name = f"Controller {index + 1}"
        # `power_state` reads `bonds`, which is what separates "unpaired" from
        # "asleep" -- and that distinction is what this loop skips on.
        state.bonds = ["A8:ED:71:F3:ED:FD"] if hci in paired else []
        state.to(Phase.CONFIGURING)
        state.to(Phase.LISTENING)
        manager._adapters[addr] = state
        peripherals[hci] = _Peripheral()
        manager._ble[addr] = peripherals[hci]
        bonds[addr] = list(state.bonds)

    calls = []

    async def _disconnect_host(bd_addr, *, forget=False, confirm_orphan=False):
        calls.append((bd_addr, forget, confirm_orphan))
        if forget:
            bonds[bd_addr] = []
            manager._adapters[bd_addr].bonds = []
        manager._ble[bd_addr].suppress_advertising()
        # The real one answers False when nothing was *connected*, which is
        # the ordinary case for an adapter that is already asleep.
        return False, "Nothing was connected to it"

    monkeypatch.setattr(manager, "disconnect_host", _disconnect_host)
    monkeypatch.setattr(
        adapter_mod, "_bonds_on_disk", lambda a: list(bonds.get(a, []))
    )
    return manager, peripherals, calls, bonds


class TestItSwitchesThemOffAndNothingElse:
    @pytest.mark.asyncio
    async def test_every_paired_enabled_controller_goes_off_the_air(self, monkeypatch):
        manager, peripherals, _calls, _bonds = _manager(monkeypatch)

        ok, _message = await manager.sleep_all()

        assert ok
        assert [hci for hci, p in peripherals.items() if p.suppressed] == [
            "hci0", "hci1", "hci2"
        ]

    @pytest.mark.asyncio
    async def test_no_bond_is_removed(self, monkeypatch):
        """**The property that separates this from Reset.**

        One keyword argument apart, and getting it wrong strands every
        controller: the console keeps its half of a bond we have just deleted,
        and generally cannot be told to forget it.
        """
        manager, _p, calls, bonds = _manager(monkeypatch)

        await manager.sleep_all()

        assert all(forget is False for _addr, forget, _c in calls)
        for index in range(3):
            assert bonds[f"00:00:00:00:00:{index:02X}"], (
                f"hci{index} lost its pairing"
            )

    @pytest.mark.asyncio
    async def test_confirm_orphan_is_not_passed(self, monkeypatch):
        """It is only consulted under `forget`, so passing it would describe
        the call as something it is not to anyone reading it."""
        manager, _p, calls, _bonds = _manager(monkeypatch)

        await manager.sleep_all()

        assert all(confirm is False for _a, _f, confirm in calls)


class TestItSkipsWhatItShould:
    @pytest.mark.asyncio
    async def test_an_unpaired_adapter_is_left_alone(self, monkeypatch):
        """There is no console to sleep from. Switching one off with nothing
        to wake back to is indistinguishable from broken -- the same reason
        its card has no power button."""
        manager, peripherals, calls, _bonds = _manager(monkeypatch)

        await manager.sleep_all()

        assert peripherals["hci3"].suppressed is False
        assert "00:00:00:00:00:03" not in [addr for addr, _f, _c in calls]

    @pytest.mark.asyncio
    async def test_a_disabled_adapter_is_left_alone(self, monkeypatch):
        manager, peripherals, _calls, _bonds = _manager(monkeypatch, enabled=2)

        await manager.sleep_all()

        assert peripherals["hci2"].suppressed is False

    @pytest.mark.asyncio
    async def test_nothing_paired_is_reported_rather_than_claimed(self, monkeypatch):
        manager, _p, _calls, _bonds = _manager(monkeypatch, paired=())

        ok, message = await manager.sleep_all()

        assert ok
        assert "No paired controllers" in message


class TestOneFailureDoesNotStopTheRest:
    @pytest.mark.asyncio
    async def test_a_raising_adapter_is_reported_and_skipped(self, monkeypatch):
        manager, peripherals, _calls, _bonds = _manager(monkeypatch)

        real = manager.disconnect_host

        async def _sometimes(bd_addr, **kwargs):
            if bd_addr.endswith(":01"):
                raise OSError("the dongle went away")
            return await real(bd_addr, **kwargs)

        monkeypatch.setattr(manager, "disconnect_host", _sometimes)

        ok, message = await manager.sleep_all()

        assert ok is False
        assert "Controller 2" in message
        # The others still went off the air.
        assert peripherals["hci0"].suppressed
        assert peripherals["hci2"].suppressed

    @pytest.mark.asyncio
    async def test_a_false_return_is_not_a_failure(self, monkeypatch):
        """`disconnect_host` answers False when nothing was *connected*, which
        is the ordinary case for an adapter that is already asleep. The part
        that matters -- taking the advertisement down -- happens either way."""
        manager, _p, _calls, _bonds = _manager(monkeypatch)

        ok, message = await manager.sleep_all()

        assert ok
        assert "3 controller(s)" in message


class TestTheEndpoint:
    @pytest.mark.asyncio
    async def test_it_needs_a_login(self):
        client, _state = await _web_client()
        try:
            response = await client.post("/api/adapter/sleep-all", json={})
            assert response.status == 401
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_mock_mode_says_so_rather_than_pretending(self):
        client, _state = await _web_client()
        try:
            await client.post("/api/login", json={"password": "admin-password-1"})
            response = await client.post("/api/adapter/sleep-all", json={})
            assert response.status == 400
            assert "mock" in (await response.json())["error"].lower()
        finally:
            await client.close()


async def _web_client():
    from aiohttp.test_utils import TestClient, TestServer

    from server import config as server_config
    from server.bt.profiles import create_profile
    from server.bt.sink import MockSink
    from server.datapath import Datapath
    from server.router import OutputChannel, Router
    from server.sessions import SessionManager
    from server.web.app import create_app

    cfg = server_config.ServerConfig(
        password="client-password", admin_password="admin-password-1",
        tls_enabled=False,
    )
    router = Router()
    router.add_channel(
        OutputChannel(
            bd_addr="00:00:00:00:00:01", hci_name="mock0",
            profile=create_profile("generic"), sink=MockSink(name="mock0"),
        )
    )
    sessions = SessionManager(cfg.password, auto_approve=True)
    datapath = Datapath(
        sessions, router, bind_host="127.0.0.1", bind_port=0, realtime=False,
    )
    app = create_app(cfg, sessions, router, datapath)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, app["state"]


class TestSleepDoesNotReportTheControllerAsUnpaired:
    """**A display error that steers the operator into the destructive action.**

    `power_state` is derived from `bonds`, and `disconnect_host` used to clear
    that list whether or not it had forgotten anything. The keys on disk were
    never touched -- the reconcile put the list back ten seconds later -- but
    for those ten seconds the card said *Not paired*, which does two things:

      * it hides Wake, the one control that brings the controller back;
      * it leaves Pair, which clears the bond for real.

    Nobody had met it while Disconnect was a per-adapter button. Sleep all does
    it to four at once, and four cards reading "Not paired -- press Pair" is
    an invitation to unpair a console that cannot be told to forget.
    """

    @pytest.mark.asyncio
    async def test_a_plain_disconnect_keeps_the_bond_reading(self, monkeypatch):
        from server.bt import adapter as adapter_mod
        from server.bt.adapter import AdapterManager
        from server.bt.state import AdapterState, Phase
        from server.config import ServerConfig
        from server.router import Router

        config = ServerConfig()
        config.controller_transport = "ble"
        manager = AdapterManager(Router(), config)

        addr = "00:00:00:00:00:AA"
        state = AdapterState(bd_addr=addr, hci_name="hci0")
        state.enabled = True
        state.bonds = ("A8:ED:71:F3:ED:FD",)
        state.peer = "A8:ED:71:F3:ED:FD"
        state.to(Phase.CONFIGURING)
        state.to(Phase.LISTENING)
        manager._adapters[addr] = state
        manager._ble[addr] = _Peripheral()
        manager._ble[addr].sink = type("S", (), {"detach": lambda self: None})()

        async def _none(*a, **k):
            return []

        monkeypatch.setattr(adapter_mod.adapter_dbus, "connected_devices", _none)
        monkeypatch.setattr(adapter_mod, "_bonds_on_disk", lambda a: ["A8:ED:71:F3:ED:FD"])

        await manager.disconnect_host(addr, forget=False)

        assert state.bonds, "a plain Sleep reported the controller as unpaired"
        assert state.power_state == "asleep", (
            "the card would hide Wake and offer Pair, which clears the bond"
        )

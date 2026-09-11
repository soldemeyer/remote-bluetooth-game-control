"""A console that reconnects to a restarted server may never re-subscribe.

Reported as: after restarting the Pi, every adapter shows connected, the web
GUI shows the controller's inputs arriving at the server, and the console
receives nothing -- until one controller is re-paired, after which *all* of
them work.

The mechanism, measured on the reference Pi with four adapters and one
console:

    phase    peer                connected  subscribed
    linked   A8:ED:71:F3:ED:FD   True       True      <- hci0
    linked   A8:ED:71:F3:ED:FD   True       False     <- hci1
    linked   A8:ED:71:F3:ED:FD   True       True      <- hci2
    linked   A8:ED:71:F3:ED:FD   True       False     <- hci3

A GATT subscription is not persisted -- bluetoothd keeps an ``info`` file per
bond and nothing else -- so a restart rebuilds the database with every CCCD
clear. The bonded console reconnects within a second but does not necessarily
re-subscribe; here it re-subscribed to two adapters and never went back for
the other two.

Nothing reports it. The link is up, encrypted and authenticated, ``notify``
raises nothing, and bluetoothd silently declines to forward to an
unsubscribed host. Re-pairing fixes every adapter because it drops every
link, which is the same repair this now performs deliberately.
"""

from __future__ import annotations

import asyncio

import pytest

from common.timing import now_ns
from server.bt.adapter import (
    _MAX_RESUBSCRIBE_TRIES,
    _SUBSCRIBE_GRACE_NS,
    AdapterManager,
)
from server.bt.state import Phase


class FakeSink:
    def __init__(self, subscribed: bool) -> None:
        self.is_subscribed = subscribed


class FakePeripheral:
    def __init__(self, subscribed: bool) -> None:
        self.sink = FakeSink(subscribed)


class FakeAdapter:
    def __init__(self, bd_addr: str, hci_name: str, *, peer="AA:BB:CC:DD:EE:FF",
                 phase=Phase.LINKED) -> None:
        self.bd_addr = bd_addr
        self.hci_name = hci_name
        self.peer = peer
        self.phase = phase


@pytest.fixture()
def manager(monkeypatch):
    """A bare AdapterManager with the two dicts the repair keeps state in.

    Constructed with ``__new__`` deliberately and *only* for the fields this
    one method touches -- a real one wants D-Bus. The attributes are named
    here so that if the method grows a dependency, this fails loudly rather
    than passing against a half-built object.
    """
    mgr = AdapterManager.__new__(AdapterManager)
    mgr._ble = {}
    mgr._unsubscribed_since = {}
    mgr._resubscribe_tries = {}

    dropped: list[str] = []

    async def connected_devices(hci_name):
        return [f"/org/bluez/{hci_name}/dev_AA_BB_CC_DD_EE_FF"]

    async def disconnect_device(path):
        dropped.append(path)
        return True

    from server.bt import adapter as adapter_module

    monkeypatch.setattr(adapter_module.adapter_dbus, "connected_devices", connected_devices)
    monkeypatch.setattr(adapter_module.adapter_dbus, "disconnect_device", disconnect_device)

    mgr.dropped = dropped
    return mgr


def run(mgr, adapters):
    asyncio.run(mgr._ensure_ble_subscribed(adapters))


def age(mgr, bd_addr, seconds):
    """Pretend the adapter has been unsubscribed for this long."""
    mgr._unsubscribed_since[bd_addr] = now_ns() - int(seconds * 1e9)


class TestItLeavesHealthyLinksAlone:
    def test_a_subscribed_adapter_is_never_dropped(self, manager):
        manager._ble["A"] = FakePeripheral(subscribed=True)
        adapters = [FakeAdapter("A", "hci0")]
        for _ in range(5):
            run(manager, adapters)
            age(manager, "A", 3600)
        assert manager.dropped == []

    def test_an_idle_adapter_is_never_dropped(self, manager):
        """No peer means nothing to subscribe, not a fault to repair."""
        manager._ble["A"] = FakePeripheral(subscribed=False)
        adapters = [FakeAdapter("A", "hci0", peer="", phase=Phase.LISTENING)]
        run(manager, adapters)
        age(manager, "A", 3600)
        run(manager, adapters)
        assert manager.dropped == []

    def test_a_classic_adapter_is_ignored(self, manager):
        """No BLE peripheral, so no subscription concept at all."""
        adapters = [FakeAdapter("A", "hci0")]
        run(manager, adapters)
        assert manager.dropped == []


class TestTheGracePeriod:
    def test_nothing_happens_immediately(self, manager):
        """The adapters that did subscribe took about 20 s. Acting sooner
        disconnects a console that was going to subscribe anyway."""
        manager._ble["A"] = FakePeripheral(subscribed=False)
        adapters = [FakeAdapter("A", "hci0")]
        run(manager, adapters)
        run(manager, adapters)
        assert manager.dropped == []

    def test_it_fires_once_the_grace_has_passed(self, manager):
        manager._ble["A"] = FakePeripheral(subscribed=False)
        adapters = [FakeAdapter("A", "hci0")]
        run(manager, adapters)
        age(manager, "A", _SUBSCRIBE_GRACE_NS / 1e9 + 1)
        run(manager, adapters)
        assert len(manager.dropped) == 1

    def test_the_reconnect_gets_its_own_grace(self, manager):
        """Dropping restarts the clock, or the very next reconcile would drop
        the link again before the console had a chance to come back."""
        manager._ble["A"] = FakePeripheral(subscribed=False)
        adapters = [FakeAdapter("A", "hci0")]
        run(manager, adapters)
        age(manager, "A", _SUBSCRIBE_GRACE_NS / 1e9 + 1)
        run(manager, adapters)
        run(manager, adapters)
        assert len(manager.dropped) == 1


class TestItGivesUp:
    def test_it_stops_after_the_budget(self, manager):
        """A console that genuinely never subscribes must not be disconnected
        every reconcile forever -- that is worse than the fault."""
        manager._ble["A"] = FakePeripheral(subscribed=False)
        adapters = [FakeAdapter("A", "hci0")]
        for _ in range(_MAX_RESUBSCRIBE_TRIES + 5):
            age(manager, "A", _SUBSCRIBE_GRACE_NS / 1e9 + 1)
            run(manager, adapters)
        assert len(manager.dropped) == _MAX_RESUBSCRIBE_TRIES

    def test_subscribing_restores_the_full_budget(self, manager):
        """A link that drops and comes back later must not inherit a spent
        budget from an earlier fault."""
        peripheral = FakePeripheral(subscribed=False)
        manager._ble["A"] = peripheral
        adapters = [FakeAdapter("A", "hci0")]

        for _ in range(_MAX_RESUBSCRIBE_TRIES):
            age(manager, "A", _SUBSCRIBE_GRACE_NS / 1e9 + 1)
            run(manager, adapters)
        assert len(manager.dropped) == _MAX_RESUBSCRIBE_TRIES

        peripheral.sink.is_subscribed = True
        run(manager, adapters)
        peripheral.sink.is_subscribed = False

        age(manager, "A", _SUBSCRIBE_GRACE_NS / 1e9 + 1)
        run(manager, adapters)
        assert len(manager.dropped) == _MAX_RESUBSCRIBE_TRIES + 1


class TestTheRealScenario:
    def test_two_of_four_unsubscribed_drops_only_those_two(self, manager):
        """Exactly what the reference Pi showed after a restart."""
        states = {"A": True, "B": False, "C": True, "D": False}
        for name, subscribed in states.items():
            manager._ble[name] = FakePeripheral(subscribed=subscribed)
        adapters = [FakeAdapter(name, f"hci{i}") for i, name in enumerate(states)]

        run(manager, adapters)
        for name in states:
            age(manager, name, _SUBSCRIBE_GRACE_NS / 1e9 + 1)
        run(manager, adapters)

        assert len(manager.dropped) == 2
        assert "hci1" in " ".join(manager.dropped)
        assert "hci3" in " ".join(manager.dropped)
        assert "hci0" not in " ".join(manager.dropped)
        assert "hci2" not in " ".join(manager.dropped)


class TestTheSubscriptionCheckItself:
    """The check reads two different things bluetoothd does, and looking at
    only one of them reported two healthy adapters as broken on the first run
    of this."""

    def sink(self, *, sock=None, notifying=False):
        from server.bt.ble.peripheral import BLESink

        instance = BLESink.__new__(BLESink)
        instance._notify_sock = sock
        instance._characteristic = type("C", (), {"notifying": notifying})()
        return instance

    def test_the_socket_path_counts_as_subscribed(self):
        """bluetoothd calls AcquireNotify *instead of* StartNotify, so an
        adapter on the socket has `notifying` False and is subscribed."""
        assert self.sink(sock=object(), notifying=False).is_subscribed is True

    def test_the_properties_path_counts_when_notifying(self):
        assert self.sink(sock=None, notifying=True).is_subscribed is True

    def test_neither_is_not_subscribed(self):
        assert self.sink(sock=None, notifying=False).is_subscribed is False

    def test_no_characteristic_is_not_subscribed(self):
        from server.bt.ble.peripheral import BLESink

        instance = BLESink.__new__(BLESink)
        instance._notify_sock = None
        instance._characteristic = None
        assert instance.is_subscribed is False

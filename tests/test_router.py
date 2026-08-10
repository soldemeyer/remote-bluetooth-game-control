"""Router: adapter channels, assignment, and capacity."""

from __future__ import annotations

import pytest

from server.bt.profiles import create_profile
from server.bt.sink import MockSink
from server.router import MAX_OUTPUTS, OutputChannel, Router


def make_channel(index: int) -> OutputChannel:
    return OutputChannel(
        bd_addr=f"00:00:00:00:00:{index:02X}",
        hci_name=f"hci{index}",
        profile=create_profile("generic"),
        sink=MockSink(name=f"mock{index}"),
    )


@pytest.fixture
def router() -> Router:
    return Router()


class TestCapacity:
    def test_starts_empty(self, router):
        assert router.capacity == 0

    def test_capacity_tracks_channel_count(self, router):
        """Capacity is derived from hardware, never hardcoded -- this is what
        makes client GUIs grey out unusable slots."""
        for index in range(3):
            router.add_channel(make_channel(index))
            assert router.capacity == index + 1

    def test_enforces_the_adapter_ceiling(self, router):
        for index in range(MAX_OUTPUTS):
            router.add_channel(make_channel(index))

        with pytest.raises(ValueError, match="ceiling"):
            router.add_channel(make_channel(MAX_OUTPUTS))

    def test_replacing_an_existing_address_is_allowed_at_the_ceiling(self, router):
        for index in range(MAX_OUTPUTS):
            router.add_channel(make_channel(index))

        # Same BD_ADDR: a replacement, not an addition.
        router.add_channel(make_channel(0))
        assert router.capacity == MAX_OUTPUTS

    def test_removing_reduces_capacity(self, router):
        router.add_channel(make_channel(0))
        router.add_channel(make_channel(1))
        router.remove_channel("00:00:00:00:00:00")
        assert router.capacity == 1

    def test_removing_unknown_address_is_a_noop(self, router):
        """Hot-plug races are normal; this must never raise."""
        router.remove_channel("AA:BB:CC:DD:EE:FF")


class TestAssignment:
    def test_resolve_returns_none_when_unassigned(self, router):
        router.add_channel(make_channel(0))
        assert router.resolve("client-a", 0) is None

    def test_assign_then_resolve(self, router):
        router.add_channel(make_channel(0))
        assert router.assign("00:00:00:00:00:00", "client-a", 0, "alice")

        channel = router.resolve("client-a", 0)
        assert channel is not None
        assert channel.username == "alice"

    def test_assign_to_unknown_adapter_fails(self, router):
        assert not router.assign("AA:BB:CC:DD:EE:FF", "client-a", 0)

    def test_one_controller_drives_one_adapter(self, router):
        """Reassigning a slot must release the adapter it previously held,
        otherwise the old channel keeps receiving input forever."""
        router.add_channel(make_channel(0))
        router.add_channel(make_channel(1))

        router.assign("00:00:00:00:00:00", "client-a", 0, "alice")
        router.assign("00:00:00:00:00:01", "client-a", 0, "alice")

        assert router.channel("00:00:00:00:00:00").assigned_client is None
        assert router.resolve("client-a", 0).bd_addr == "00:00:00:00:00:01"

    def test_different_slots_can_use_different_adapters(self, router):
        router.add_channel(make_channel(0))
        router.add_channel(make_channel(1))

        router.assign("00:00:00:00:00:00", "client-a", 0)
        router.assign("00:00:00:00:00:01", "client-a", 1)

        assert router.resolve("client-a", 0).bd_addr == "00:00:00:00:00:00"
        assert router.resolve("client-a", 1).bd_addr == "00:00:00:00:00:01"

    def test_unassign_clears_the_route(self, router):
        router.add_channel(make_channel(0))
        router.assign("00:00:00:00:00:00", "client-a", 0)
        router.unassign("00:00:00:00:00:00")
        assert router.resolve("client-a", 0) is None

    def test_unassign_client_clears_all_its_slots(self, router):
        router.add_channel(make_channel(0))
        router.add_channel(make_channel(1))
        router.assign("00:00:00:00:00:00", "client-a", 0)
        router.assign("00:00:00:00:00:01", "client-a", 1)

        router.unassign_client("client-a")

        assert router.resolve("client-a", 0) is None
        assert router.resolve("client-a", 1) is None

    def test_unassign_client_leaves_others_alone(self, router):
        router.add_channel(make_channel(0))
        router.add_channel(make_channel(1))
        router.assign("00:00:00:00:00:00", "client-a", 0)
        router.assign("00:00:00:00:00:01", "client-b", 0)

        router.unassign_client("client-a")

        assert router.resolve("client-a", 0) is None
        assert router.resolve("client-b", 0) is not None

    def test_removing_an_assigned_adapter_orphans_the_controller(self, router):
        """Unplugging a dongle mid-session must unassign, not crash."""
        router.add_channel(make_channel(0))
        router.assign("00:00:00:00:00:00", "client-a", 0)

        router.remove_channel("00:00:00:00:00:00")

        assert router.resolve("client-a", 0) is None
        assert router.capacity == 0


class TestAutoAssign:
    def test_places_each_slot_on_a_free_adapter(self, router):
        router.add_channel(make_channel(0))
        router.add_channel(make_channel(1))

        placed = router.auto_assign("client-a", [0, 1], {0: "alice", 1: "bob"})

        assert placed == 2
        assert router.resolve("client-a", 0) is not None
        assert router.resolve("client-a", 1) is not None

    def test_stops_when_adapters_run_out(self, router):
        router.add_channel(make_channel(0))
        placed = router.auto_assign("client-a", [0, 1, 2], {})
        assert placed == 1

    def test_is_idempotent(self, router):
        """Called twice per client -- at session creation and again on
        SET_CONTROLLERS. The second pass must not move a working slot onto a
        different adapter and orphan the first."""
        router.add_channel(make_channel(0))
        router.add_channel(make_channel(1))

        router.auto_assign("client-a", [0], {0: "alice"})
        first = router.resolve("client-a", 0).bd_addr

        placed = router.auto_assign("client-a", [0, 1], {0: "alice", 1: "bob"})

        assert placed == 1                                   # only slot 1 was new
        assert router.resolve("client-a", 0).bd_addr == first
        assert router.resolve("client-a", 1) is not None


def test_set_username_updates_the_channel(router):
    router.add_channel(make_channel(0))
    router.assign("00:00:00:00:00:00", "client-a", 0, "alice")

    router.set_username("client-a", 0, "alice2")

    assert router.channel("00:00:00:00:00:00").username == "alice2"


def test_channel_is_live_only_when_connected_and_ready(router):
    channel = make_channel(0)
    router.add_channel(channel)

    assert channel.is_live

    channel.sink.set_connected(False)
    assert not channel.is_live


def test_snapshot_shape(router):
    router.add_channel(make_channel(0))
    snapshot = router.snapshot()

    assert len(snapshot) == 1
    for key in ("bd_addr", "hci", "profile", "connected", "ready", "username"):
        assert key in snapshot[0]

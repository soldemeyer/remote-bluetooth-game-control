"""Multi-adapter behaviour: shared SDP registration and independent routing.

The SDP tests guard a bug found only on real hardware with four dongles: the
HID profile was registered once *per adapter*, but BlueZ's ProfileManager1
keeps a single system-wide SDP database, so every registration after the first
failed with "UUID already registered". Three of four adapters ended up inert,
and which one survived depended on dict ordering -- so it moved between
restarts.

D-Bus is mocked here; the real behaviour is verified on hardware.
"""

from __future__ import annotations

import asyncio

import pytest

from server import config as server_config
from server.bt.profiles import create_profile
from server.bt.sink import MockSink
from server.router import MAX_OUTPUTS, OutputChannel, Router


class FakeBus:
    """Stands in for a dbus-next MessageBus."""

    def __init__(self) -> None:
        self.disconnected = False


class FakeAdapterManager:
    """The SDP-sharing logic of AdapterManager, without Linux or D-Bus.

    Mirrors the real ``_ensure_sdp`` / ``_release_sdp`` contract so the
    registration accounting can be tested anywhere.
    """

    def __init__(self) -> None:
        self.registrations = 0
        self.unregistrations = 0
        self._sdp_bus = None
        self._sdp_profile_name = ""
        self._hid_servers: dict[str, object] = {}
        self.warnings: list[str] = []

    async def ensure_sdp(self, profile) -> bool:
        if self._sdp_bus is not None:
            if self._sdp_profile_name and self._sdp_profile_name != profile.name:
                self.warnings.append(
                    f"{profile.name} requested but {self._sdp_profile_name} registered"
                )
            return True

        self.registrations += 1
        self._sdp_bus = FakeBus()
        self._sdp_profile_name = profile.name
        return True

    async def release_sdp(self) -> None:
        if self._sdp_bus is None:
            return
        self.unregistrations += 1
        self._sdp_bus = None
        self._sdp_profile_name = ""

    async def start_hid(self, bd_addr: str, profile) -> bool:
        if not await self.ensure_sdp(profile):
            return False
        self._hid_servers[bd_addr] = object()
        return True

    async def stop_hid(self, bd_addr: str) -> None:
        self._hid_servers.pop(bd_addr, None)
        if not self._hid_servers:
            await self.release_sdp()


class TestSharedSDPRegistration:
    async def test_four_adapters_register_sdp_once(self):
        """The regression: four adapters, one registration. Registering per
        adapter made BlueZ reject three of them."""
        manager = FakeAdapterManager()
        profile = create_profile("generic")

        for index in range(4):
            assert await manager.start_hid(f"00:00:00:00:00:{index:02X}", profile)

        assert manager.registrations == 1, (
            f"registered {manager.registrations} times; BlueZ allows one per UUID"
        )
        assert len(manager._hid_servers) == 4, "all four adapters must serve"

    async def test_every_adapter_gets_a_hid_server(self):
        manager = FakeAdapterManager()
        profile = create_profile("generic")
        addresses = [f"00:00:00:00:00:{i:02X}" for i in range(4)]

        for address in addresses:
            await manager.start_hid(address, profile)

        assert sorted(manager._hid_servers) == sorted(addresses)

    async def test_sdp_survives_removing_one_adapter(self):
        """Dropping the shared record while another adapter is still serving
        would make that adapter undiscoverable."""
        manager = FakeAdapterManager()
        profile = create_profile("generic")

        for index in range(3):
            await manager.start_hid(f"00:00:00:00:00:{index:02X}", profile)

        await manager.stop_hid("00:00:00:00:00:00")

        assert manager.unregistrations == 0, "SDP released while adapters remained"
        assert manager._sdp_bus is not None

    async def test_sdp_released_with_the_last_adapter(self):
        manager = FakeAdapterManager()
        profile = create_profile("generic")

        for index in range(3):
            await manager.start_hid(f"00:00:00:00:00:{index:02X}", profile)
        for index in range(3):
            await manager.stop_hid(f"00:00:00:00:00:{index:02X}")

        assert manager.unregistrations == 1
        assert manager._sdp_bus is None

    async def test_re_registers_after_a_full_teardown(self):
        manager = FakeAdapterManager()
        profile = create_profile("generic")

        await manager.start_hid("00:00:00:00:00:00", profile)
        await manager.stop_hid("00:00:00:00:00:00")
        await manager.start_hid("00:00:00:00:00:00", profile)

        assert manager.registrations == 2

    async def test_mixed_profiles_are_flagged_not_silently_wrong(self):
        """One SDP record machine-wide means adapters cannot advertise
        different descriptors. Silently serving the wrong one would produce a
        controller that advertises one layout and reports another."""
        manager = FakeAdapterManager()

        await manager.start_hid("00:00:00:00:00:00", create_profile("generic"))
        await manager.start_hid("00:00:00:00:00:01", create_profile("switch_pro"))

        assert manager.warnings, "mixed profiles were not reported"
        assert "switch_pro" in manager.warnings[0]


class TestRoutingAcrossAdapters:
    """Each adapter must receive only its own controller's input."""

    @pytest.fixture
    def router(self):
        router = Router()
        self.sinks = {}
        for index in range(MAX_OUTPUTS):
            bd_addr = f"00:00:00:00:00:{index:02X}"
            sink = MockSink(name=f"m{index}")
            self.sinks[bd_addr] = sink
            router.add_channel(
                OutputChannel(
                    bd_addr=bd_addr,
                    hci_name=f"hci{index}",
                    profile=create_profile("generic"),
                    sink=sink,
                )
            )
        return router

    def test_four_slots_resolve_to_four_distinct_adapters(self, router):
        for slot in range(4):
            router.assign(f"00:00:00:00:00:{slot:02X}", "client", slot)

        resolved = [router.resolve("client", slot) for slot in range(4)]

        assert all(channel is not None for channel in resolved)
        assert len({channel.bd_addr for channel in resolved}) == 4, "adapters collided"

    def test_reassigning_a_slot_frees_its_previous_adapter(self, router):
        """Otherwise the old adapter keeps receiving that player's input
        forever -- two consoles driven by one controller."""
        router.assign("00:00:00:00:00:00", "client", 0)
        router.assign("00:00:00:00:00:01", "client", 0)

        assert router.channel("00:00:00:00:00:00").assigned_slot is None
        assert router.resolve("client", 0).bd_addr == "00:00:00:00:00:01"

    def test_capacity_reflects_all_four(self, router):
        assert router.capacity == 4

    def test_two_clients_can_share_the_adapters(self, router):
        """Four adapters, two client PCs with two controllers each."""
        router.assign("00:00:00:00:00:00", "alice-pc", 0)
        router.assign("00:00:00:00:00:01", "alice-pc", 1)
        router.assign("00:00:00:00:00:02", "bob-pc", 0)
        router.assign("00:00:00:00:00:03", "bob-pc", 1)

        assert router.resolve("alice-pc", 0).bd_addr == "00:00:00:00:00:00"
        assert router.resolve("bob-pc", 0).bd_addr == "00:00:00:00:00:02"
        # Same slot number, different client -- must not collide.
        assert router.resolve("alice-pc", 0) is not router.resolve("bob-pc", 0)

    def test_losing_one_adapter_leaves_the_others_routing(self, router):
        """Unplugging a dongle must not disturb the other three."""
        for slot in range(4):
            router.assign(f"00:00:00:00:00:{slot:02X}", "client", slot)

        router.remove_channel("00:00:00:00:00:02")

        assert router.resolve("client", 2) is None
        assert router.capacity == 3
        for slot in (0, 1, 3):
            assert router.resolve("client", slot) is not None


class TestAdapterConfigAtScale:
    def test_four_enabled_adapters_validate(self):
        cfg = server_config.ServerConfig(password="a-good-password")
        for index in range(4):
            cfg.upsert_adapter(
                server_config.AdapterConfig(bd_addr=f"00:00:00:00:00:{index:02X}")
            )
        assert cfg.validate() == []

    def test_five_enabled_adapters_are_rejected(self):
        cfg = server_config.ServerConfig(password="a-good-password")
        for index in range(5):
            cfg.upsert_adapter(
                server_config.AdapterConfig(bd_addr=f"00:00:00:00:00:{index:02X}")
            )
        assert any("At most" in problem for problem in cfg.validate())

    def test_paired_targets_are_per_adapter(self):
        """Each adapter remembers its own console, so four adapters can drive
        four different consoles and each reconnects to the right one."""
        cfg = server_config.ServerConfig(password="a-good-password")
        for index in range(4):
            cfg.upsert_adapter(
                server_config.AdapterConfig(
                    bd_addr=f"00:00:00:00:00:{index:02X}",
                    paired_target=f"AA:BB:CC:DD:EE:{index:02X}",
                )
            )

        for index in range(4):
            adapter = cfg.adapter(f"00:00:00:00:00:{index:02X}")
            assert adapter.paired_target == f"AA:BB:CC:DD:EE:{index:02X}"

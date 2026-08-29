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


class TestFourAdaptersAreTellableApartInTheGui:
    """The operator-facing name must be unique even when the on-air one is not.

    Setting ``exact_name`` on the 8BitDo identity was correct and measured: an
    Analogue 3D paired with the adapter advertising ``8BitDo 64 BT`` and would
    not connect at all to the one beside it advertising ``8BitDo 64 BT 1``. The
    consequence is that **every** adapter advertises the same string.

    The web GUI titled each card with that string, so four cards and four
    assignment dropdown entries all read ``8BitDo 64 BT`` and there was no way
    to tell which card belonged to which player. Two names, two questions: what
    the console matches on, and what the operator calls it.
    """

    def _manager(self, count=4, identity="8bitdo"):
        from server.bt.adapter import AdapterManager
        from server.bt.state import AdapterState

        cfg = server_config.ServerConfig()
        cfg.controller_identity = identity
        manager = AdapterManager(Router(), cfg)

        for index in range(count):
            addr = f"00:00:00:00:00:{index:02X}"
            cfg.upsert_adapter(
                server_config.AdapterConfig(bd_addr=addr, number=index + 1)
            )
            manager._adapters[addr] = AdapterState(
                bd_addr=addr, hci_name=f"hci{index}"
            )
        return manager

    def test_every_adapter_advertises_the_identical_name(self):
        """The premise. If this ever stops being true the console breaks, so
        the fix had to be on the display side rather than here."""
        manager = self._manager()

        names = {row["name"] for row in manager.snapshot()}
        assert names == {"8BitDo 64 BT"}

    def test_the_operator_sees_controller_one_to_four(self):
        manager = self._manager()

        labels = [row["display_name"] for row in manager.snapshot()]
        assert labels == ["Controller 1", "Controller 2",
                          "Controller 3", "Controller 4"]

    def test_they_are_in_number_order_left_to_right(self):
        """The cards render in snapshot order, so this *is* the left-to-right
        order the operator sees. Numbers are persisted per BD_ADDR, so they
        must not follow the hciX indices, which reshuffle across reboots."""
        manager = self._manager(count=0)

        from server.bt.state import AdapterState

        # Deliberately reversed: hci0 holds Controller 4.
        for index, number in enumerate((4, 3, 2, 1)):
            addr = f"00:00:00:00:00:{index:02X}"
            manager._config.upsert_adapter(
                server_config.AdapterConfig(bd_addr=addr, number=number)
            )
            manager._adapters[addr] = AdapterState(
                bd_addr=addr, hci_name=f"hci{index}"
            )

        rows = manager.snapshot()
        assert [row["display_name"] for row in rows] == [
            "Controller 1", "Controller 2", "Controller 3", "Controller 4"
        ]
        assert [row["hci"] for row in rows] == ["hci3", "hci2", "hci1", "hci0"]

    def test_an_operator_label_wins_over_the_generated_one(self):
        manager = self._manager()
        addr = "00:00:00:00:00:01"
        manager._config.upsert_adapter(
            server_config.AdapterConfig(
                bd_addr=addr, number=2, label="Living room"
            )
        )

        row = next(r for r in manager.snapshot() if r["bd_addr"] == addr)
        assert row["display_name"] == "Living room"

    def test_an_unnumbered_adapter_falls_back_to_hci(self):
        """A detected-but-never-enabled adapter has no number, and a blank
        title is worse than a diagnostic one."""
        manager = self._manager(count=0)

        from server.bt.state import AdapterState

        addr = "00:00:00:00:00:09"
        manager._adapters[addr] = AdapterState(bd_addr=addr, hci_name="hci9")

        assert manager.snapshot()[0]["display_name"] == "hci9"

    def test_the_generic_identity_still_numbers_its_advertised_name(self):
        """Nothing about the on-air naming changes. The generic identity is not
        impersonating anyone, so its adapters stay distinguishable to a host as
        well -- which is what `exact_name` being opt-in preserves."""
        manager = self._manager(identity="generic")

        names = [row["name"] for row in manager.snapshot()]
        assert names == ["RBGC Gamepad 1", "RBGC Gamepad 2",
                         "RBGC Gamepad 3", "RBGC Gamepad 4"]


class TestALabelMustNotBreakAnExactNameIdentity:
    """The adapter number, arriving by a different route.

    Measured on the reference Pi with four adapters enabled: hci2 carried the
    label ``RBGC spare 1`` from an earlier debugging session, so it advertised
    that, and the Analogue 3D never paged it. Every other reading was perfect
    -- powered, connectable, bondable, LE-only, one advertising instance -- and
    nothing anywhere said the name was the problem.

    That is exactly the failure ``exact_name`` was added to stop, so honouring
    a label over it was the same bug with a nicer origin story. The label's
    real job is naming the adapter for the operator, and that is
    ``adapter_display_name`` now.
    """

    def _manager(self, identity, label):
        from server.bt.adapter import AdapterManager
        from server.bt.state import AdapterState

        cfg = server_config.ServerConfig()
        cfg.controller_identity = identity
        manager = AdapterManager(Router(), cfg)

        addr = "00:00:00:00:00:01"
        cfg.upsert_adapter(
            server_config.AdapterConfig(bd_addr=addr, number=4, label=label)
        )
        manager._adapters[addr] = AdapterState(bd_addr=addr, hci_name="hci2")
        manager._label_warned = set()
        return manager, addr

    def test_the_advertised_name_stays_exact(self):
        manager, addr = self._manager("8bitdo", "RBGC spare 1")

        assert manager.adapter_name(addr) == "8BitDo 64 BT"

    def test_the_operator_still_sees_their_label(self):
        """It is not discarded -- it moves to the field whose job it is."""
        manager, addr = self._manager("8bitdo", "RBGC spare 1")

        assert manager.adapter_display_name(addr) == "RBGC spare 1"

    def test_ignoring_it_is_reported_once(self, caplog):
        """Silently overriding an explicit operator choice is its own trap."""
        import logging

        manager, addr = self._manager("8bitdo", "RBGC spare 1")

        with caplog.at_level(logging.WARNING):
            manager.adapter_name(addr)
            manager.adapter_name(addr)
            manager.adapter_name(addr)

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1, "runs on every reconcile; must not repeat"
        assert "RBGC spare 1" in caplog.text
        assert "never paged" in caplog.text

    def test_a_label_equal_to_the_exact_name_is_not_warned_about(self):
        """Harmless and common -- an operator typing the name they were told."""
        import logging

        manager, addr = self._manager("8bitdo", "8BitDo 64 BT")

        assert manager.adapter_name(addr) == "8BitDo 64 BT"
        assert manager._label_warned == set()

    def test_a_label_still_wins_under_a_generic_identity(self):
        """Nothing is impersonated there, so no host is matching on the name
        and an explicit choice should beat a generated one."""
        manager, addr = self._manager("generic", "Living room")

        assert manager.adapter_name(addr) == "Living room"
        assert manager.adapter_display_name(addr) == "Living room"

    def test_the_generic_identity_still_numbers_an_unlabelled_adapter(self):
        manager, addr = self._manager("generic", "")

        assert manager.adapter_name(addr) == "RBGC Gamepad 4"

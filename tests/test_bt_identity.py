"""What the adapters claim to be, and the record that says so.

Some consoles have no pairing list: you press their button and they connect to
whatever they recognise as a controller. A controller they do not recognise is
never connected to -- there is no error, no entry in a list, nothing to see. It
is indistinguishable from being out of range.

Two things decide whether we are recognised, at two different moments:

  * **During inquiry**, before any connection, only the class of device and the
    advertised name are visible. So the name is the discriminator.
  * **After connecting**, the host can read SDP, including the DeviceID record
    that carries the vendor and product ids.

We published no DeviceID record at all, and the vendor/product ids the profiles
carried were handed to the record builder and then dropped. These tests pin both
halves.
"""

from __future__ import annotations

import pathlib
import xml.etree.ElementTree as ET

import pytest

from server.bt import identities, sdp


def _attributes(record_xml: str) -> dict[str, str]:
    """Attribute id -> the value of its single child, for easy assertions."""
    root = ET.fromstring(record_xml)
    found: dict[str, str] = {}
    for attribute in root.findall("attribute"):
        children = list(attribute)
        if len(children) == 1 and "value" in children[0].attrib:
            found[attribute.attrib["id"].lower()] = children[0].attrib["value"]
    return found


class TestTheDeviceIdRecord:
    def test_it_is_valid_xml(self):
        ET.fromstring(sdp.build_device_id_record(0x2DC8, 0x3106))

    def test_the_vendor_and_product_actually_appear(self):
        """The bug: these were accepted as parameters and never used."""
        values = _attributes(sdp.build_device_id_record(0x2DC8, 0x3106))

        assert values["0x0201"] == "0x2dc8", "VendorID missing from the record"
        assert values["0x0202"] == "0x3106", "ProductID missing from the record"

    def test_it_declares_the_pnp_service_class(self):
        """Without 0x1200 in 0x0001 a host reads these as HID attributes.

        DeviceID reuses attribute ids the HID record already uses for other
        things -- 0x0201 is VendorID here and HIDParserVersion there -- so the
        service class is the only thing that disambiguates them.
        """
        root = ET.fromstring(sdp.build_device_id_record(0x045E, 0x0B13))
        service_class = root.find("./attribute[@id='0x0001']/sequence/uuid")

        assert service_class is not None
        assert service_class.attrib["value"] == "0x1200"

    def test_the_vendor_source_is_carried(self):
        """A host comparing a USB id against one declared as SIG will not match."""
        usb = _attributes(sdp.build_device_id_record(0x2DC8, 0x3106, vendor_source=0x0002))
        sig = _attributes(sdp.build_device_id_record(0x2DC8, 0x3106, vendor_source=0x0001))

        assert usb["0x0205"] == "0x0002"
        assert sig["0x0205"] == "0x0001"

    def test_it_marks_itself_the_primary_record(self):
        assert _attributes(sdp.build_device_id_record(1, 2))["0x0204"] == "true"

    def test_it_is_a_separate_record_from_the_hid_one(self):
        """They collide on attribute ids, so they cannot be merged."""
        hid = _attributes(
            sdp.build_hid_record("Pad", b"\\x05\\x01", 0x2DC8, 0x3106)
        )
        device_id = _attributes(sdp.build_device_id_record(0x2DC8, 0x3106))

        # Same id, different meaning: HIDParserVersion vs VendorID.
        assert hid["0x0201"] == "0x0111"
        assert device_id["0x0201"] == "0x2dc8"


class TestTheIdentityTable:
    def test_the_default_is_the_honest_one(self):
        """Impersonation is opt-in: a host may apply vendor quirks we do not honour."""
        assert identities.DEFAULT_IDENTITY == "generic"
        assert identities.get_identity("generic").vendor_id == 0x1D6B

    def test_an_unknown_key_falls_back_rather_than_raising(self):
        """A stale config must not stop Bluetooth coming up at all."""
        assert identities.get_identity("no-such-pad").key == "generic"
        assert identities.get_identity("").key == "generic"

    def test_every_identity_is_complete_and_plausible(self):
        for identity in identities.IDENTITIES:
            assert identity.key and identity.display_name and identity.device_name
            assert 0 < identity.vendor_id <= 0xFFFF, identity.key
            assert 0 <= identity.product_id <= 0xFFFF, identity.key
            assert identity.vendor_source in (0x0001, 0x0002), identity.key
            assert identity.note, f"{identity.key} has no guidance for the operator"

    def test_the_keys_are_unique(self):
        keys = [identity.key for identity in identities.IDENTITIES]
        assert len(keys) == len(set(keys))

    def test_the_vendors_are_the_real_ones(self):
        """A wrong vendor id is the whole point of failure, so pin them."""
        expected = {
            "8bitdo": 0x2DC8,
            "xbox": 0x045E,
            "ps5": 0x054C,
            "ps4": 0x054C,
            "switch_pro": 0x057E,
            "razer": 0x1532,
        }
        for key, vendor in expected.items():
            assert identities.get_identity(key).vendor_id == vendor, key

    def test_the_console_friendly_one_is_offered_early(self):
        """It is the reason this table exists; it should not be buried."""
        keys = [identity.key for identity in identities.IDENTITIES]
        assert keys.index("8bitdo") <= 2

    def test_playstation_advertises_the_name_a_real_one_does(self):
        """A console matching on the name wants exactly this, oddly generic string."""
        assert identities.get_identity("ps5").device_name == "Wireless Controller"

    def test_the_choices_carry_what_the_gui_shows(self):
        for entry in identities.identity_choices():
            assert set(entry) == {"key", "name", "device_name", "vendor", "note"}
            assert ":" in entry["vendor"]


class TestIdentityIsSeparateFromProfile:
    """Changing who we claim to be must not change what we send."""

    def test_the_report_descriptor_is_untouched_by_identity(self):
        from server.bt.profiles import create_profile

        descriptor = create_profile("generic").descriptor.report_descriptor
        for identity in identities.IDENTITIES:
            # The identity carries no report descriptor at all; this is the
            # structural guarantee, asserted so nobody adds one later.
            assert not hasattr(identity, "report_descriptor"), identity.key
        assert descriptor, "the profile still owns the report layout"


class TestTheServerRemembersTheChoice:
    def test_the_config_field_exists_and_defaults_to_generic(self):
        from server.config import ServerConfig

        assert ServerConfig().controller_identity == "generic"

    def test_it_survives_a_save_and_load(self, tmp_path):
        from server import config as server_config

        path = tmp_path / "server.json"
        cfg = server_config.ServerConfig(controller_identity="8bitdo")
        server_config.save(cfg, path)

        assert server_config.load(path).controller_identity == "8bitdo"


class TestTheProfileIsServerWide:
    """The per-adapter Emulate dropdown was removed, not merely relocated.

    `channel.profile.build_input_report()` really is per channel, so setting one
    adapter to a different profile genuinely changed the bytes it sent -- while
    BlueZ published one HID service record for the whole machine, so the console
    was still told to expect the other descriptor. A controller sending reports
    in a format nothing advertised, with a log line as the only trace.
    """

    def test_the_config_field_exists_and_defaults_to_generic(self):
        from server.config import ServerConfig

        assert ServerConfig().controller_profile == "generic"

    def test_it_survives_a_save_and_load(self, tmp_path):
        from server import config as server_config

        path = tmp_path / "server.json"
        server_config.save(
            server_config.ServerConfig(controller_profile="switch_pro"), path
        )
        assert server_config.load(path).controller_profile == "switch_pro"

    def test_the_per_adapter_route_is_gone(self):
        """Deleted deliberately -- a regression should fail, not reappear."""
        import server.web.app as web_app

        assert not hasattr(web_app, "handle_adapter_profile")
        assert hasattr(web_app, "handle_bluetooth_profile")

    def test_the_per_adapter_control_is_gone_from_the_page(self):
        """The dropdown itself, not just the endpoint behind it."""
        static = (
            pathlib.Path(web_app_file()).resolve().parent / "static" / "app.js"
        )
        source = static.read_text(encoding="utf-8")

        assert 'data-action="profile"' not in source
        assert "/api/adapter/profile" not in source
        assert "/api/bluetooth/profile" in source, "no server-wide control replaced it"


def web_app_file() -> str:
    import server.web.app as web_app

    return web_app.__file__


class TestTheFlagDoesNotOverrideTheSavedChoice:
    """Same trap as --backend on the client: a flag with a concrete default
    always carries a value, so it silently wins over what the GUI saved and the
    setting reverts on every restart."""

    def test_the_flag_has_no_default(self):
        from server.main import build_parser

        assert build_parser().parse_args(["--mock-bt"]).profile is None

    def test_the_saved_profile_is_used_when_the_flag_is_absent(self):
        from server.config import ServerConfig
        from server.main import build_parser

        args = build_parser().parse_args(["--mock-bt"])
        cfg = ServerConfig(controller_profile="switch_pro")

        assert (args.profile or cfg.controller_profile) == "switch_pro"

    def test_the_flag_wins_for_that_run(self):
        from server.config import ServerConfig
        from server.main import build_parser

        args = build_parser().parse_args(["--mock-bt", "--profile", "generic"])
        cfg = ServerConfig(controller_profile="switch_pro")

        assert (args.profile or cfg.controller_profile) == "generic"

    def test_nothing_saved_and_no_flag_still_yields_a_profile(self):
        from server.bt.profiles import DEFAULT_PROFILE
        from server.config import ServerConfig
        from server.main import build_parser

        args = build_parser().parse_args(["--mock-bt"])
        cfg = ServerConfig()

        assert (args.profile or cfg.controller_profile or DEFAULT_PROFILE)


class TestApplyingAProfileToEveryAdapter:
    """`set_profile_all` is what the single control drives."""

    def _manager_with_channels(self, count: int = 3):
        from server.bt.adapter import AdapterManager
        from server.bt.profiles import create_profile
        from server.config import ServerConfig
        from server.router import OutputChannel, Router

        router = Router()
        for index in range(count):
            router.add_channel(
                OutputChannel(
                    bd_addr=f"00:00:00:00:00:{index:02X}",
                    hci_name=f"mock{index}",
                    profile=create_profile("generic"),
                    sink=_QuietSink(),
                )
            )
        return AdapterManager(router, ServerConfig()), router

    @pytest.mark.asyncio
    async def test_every_channel_changes_together(self):
        manager, router = self._manager_with_channels()

        ok, message = await manager.set_profile_all("switch_pro")

        assert ok, message
        names = {channel.profile.name for channel in router.channels()}
        assert names == {"switch_pro"}, (
            "adapters were left on different profiles, which is the state the "
            "per-adapter control used to allow"
        )

    @pytest.mark.asyncio
    async def test_the_choice_is_saved_for_adapters_plugged_in_later(self):
        manager, _router = self._manager_with_channels()

        await manager.set_profile_all("switch_pro")

        assert manager._config.controller_profile == "switch_pro"
        assert manager._default_profile == "switch_pro"

    @pytest.mark.asyncio
    async def test_with_no_adapters_it_says_so(self):
        """Rather than reporting success over an empty loop."""
        from server.bt.adapter import AdapterManager
        from server.config import ServerConfig
        from server.router import Router

        ok, message = await AdapterManager(Router(), ServerConfig()).set_profile_all(
            "generic"
        )

        assert ok is False
        assert "adapter" in message.lower()

    @pytest.mark.asyncio
    async def test_an_unknown_profile_is_refused(self):
        manager, router = self._manager_with_channels()

        ok, _message = await manager.set_profile_all("no-such-controller")

        assert ok is False
        assert {c.profile.name for c in router.channels()} == {"generic"}, (
            "a rejected name still changed something"
        )


class _QuietSink:
    """Minimal HIDSink stand-in: enough for a channel to exist."""

    name = "mock"
    is_connected = True

    def send_input_report(self, data) -> None:
        pass

    def close(self) -> None:
        pass

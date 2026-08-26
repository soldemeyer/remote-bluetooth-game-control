"""HID over GATT: the wire format a BLE console actually sees.

This transport exists because measurement forced it. The Analogue 3D's
controller advertises `BR/EDR Not Supported` and HID `0x1812`, so that console
cannot see the Classic stack at all -- captured from the air, see "The Analogue
3D is BLE" in CLAUDE.md.

The D-Bus half needs dbus-next and a running bluetoothd. What is covered here is
the half that can be got wrong on any machine, and the payload rule in
particular: it fails silently and in the opposite direction from the Classic
report-ID trap.
"""

from __future__ import annotations

import struct

import pytest

from server.bt.ble import hogp


class TestTheReportIdIsNotInThePayload:
    """The HOGP counterpart of the Classic report-ID bug, inverted.

    Over Classic, every input report begins with its report ID and omitting it
    makes the host discard the report. Over HOGP the ID lives in the Report
    Reference descriptor, so the notification value is the body *without* it.
    Leaving it in shifts every field by one byte: axes read as garbage, buttons
    land on the wrong bits, and nothing reports an error at either end.
    """

    def test_the_leading_report_id_is_stripped(self):
        assert hogp.build_ble_payload(b"\x01\xaa\xbb", 0x01) == b"\xaa\xbb"

    def test_a_profile_with_no_report_id_is_left_alone(self):
        # Removing a real data byte here would shift every field by one, with
        # nothing to indicate it.
        assert hogp.build_ble_payload(b"\xaa\xbb", None) == b"\xaa\xbb"

    def test_a_first_byte_that_is_not_the_id_is_left_alone(self):
        # Defensive: only strip what we actually put there.
        assert hogp.build_ble_payload(b"\x30\xaa", 0x01) == b"\x30\xaa"

    def test_an_empty_report_does_not_raise(self):
        assert hogp.build_ble_payload(b"", 0x01) == b""

    def test_the_ble_payload_is_one_byte_shorter_than_the_classic_one(self):
        """Against the real generic profile, not a hand-made buffer."""
        from common.state import ControllerState
        from server.bt.profiles import create_profile

        profile = create_profile("generic")
        buf = bytearray(64)
        size = profile.build_input_report(ControllerState(), buf)
        classic = bytes(buf[:size])

        ble = hogp.build_ble_payload(classic, profile.descriptor.input_report_id)

        assert classic[0] == profile.descriptor.input_report_id
        assert len(ble) == len(classic) - 1
        assert ble == classic[1:]

    def test_the_switch_profile_strips_its_own_id(self):
        from common.state import ControllerState
        from server.bt.profiles import create_profile

        profile = create_profile("switch_pro")
        buf = bytearray(64)
        size = profile.build_input_report(ControllerState(), buf)
        classic = bytes(buf[:size])

        ble = hogp.build_ble_payload(classic, profile.descriptor.input_report_id)
        assert len(ble) == len(classic) - 1


class TestThePnPIdRecord:
    """The BLE counterpart of the Classic DeviceID SDP record."""

    def test_the_layout_is_source_then_three_little_endian_words(self):
        value = hogp.build_pnp_id(0x2DC8, 0x3106, 0x0100, hogp.PNP_SOURCE_USB)
        assert len(value) == 7
        source, vendor, product, version = struct.unpack("<BHHH", value)
        assert source == hogp.PNP_SOURCE_USB
        assert (vendor, product, version) == (0x2DC8, 0x3106, 0x0100)

    def test_usb_is_the_default_source(self):
        # Controller vendors register a USB vendor id and reuse it over
        # Bluetooth; a host comparing against a USB id will not match one
        # declared as a SIG assignment.
        assert hogp.build_pnp_id(1, 2)[0] == hogp.PNP_SOURCE_USB


class TestHidInformation:
    def test_it_matches_the_real_pad(self):
        """Read off the real 8BitDo 64: ``01 01 00 03``.

        bcdHID 0x0101, not the 0x0111 most HOGP examples use. Matching costs
        nothing and removes one more field a console could compare.
        """
        assert hogp.HID_INFORMATION.hex() == "01010003"
        bcd, _, _ = struct.unpack("<HBB", hogp.HID_INFORMATION)
        assert bcd == 0x0101

    def test_it_is_not_localised(self):
        _, country, _ = struct.unpack("<HBB", hogp.HID_INFORMATION)
        assert country == 0x00

    def test_it_is_normally_connectable_and_remote_wake(self):
        # NormallyConnectable is what lets the host reconnect whenever it
        # likes, the BLE counterpart of the Classic SDP attribute. Without it
        # a console may only ever connect immediately after pairing.
        _, _, flags = struct.unpack("<HBB", hogp.HID_INFORMATION)
        assert flags & 0x01, "RemoteWake"
        assert flags & 0x02, "NormallyConnectable"


class TestTheUuidsMatchWhatTheConsoleAdvertised:
    """Pinned against the capture of the real 8BitDo 64 that pairs with an
    Analogue 3D, so a typo in a UUID cannot pass."""

    def test_the_hid_service_is_1812(self):
        assert hogp.HID_SERVICE_UUID.startswith("00001812-")

    def test_the_report_map_is_2a4b(self):
        assert hogp.REPORT_MAP_UUID.startswith("00002a4b-")

    def test_the_report_characteristic_is_2a4d(self):
        assert hogp.REPORT_UUID.startswith("00002a4d-")

    def test_the_report_reference_descriptor_is_2908(self):
        assert hogp.REPORT_REFERENCE_UUID.startswith("00002908-")

    def test_input_and_output_report_types_are_distinct(self):
        assert hogp.REPORT_TYPE_INPUT == 0x01
        assert hogp.REPORT_TYPE_OUTPUT == 0x02


class TestTheAdvertisement:
    """What a scanning console sees before anything connects.

    Pinned against a capture of the real 8BitDo 64 that pairs with an Analogue
    3D. Its ADV_IND is 25 bytes and carries **everything**::

        02 01 05                 flags: limited discoverable, BR/EDR not supported
        03 19 c4 03              appearance: gamepad
        03 03 12 18              16-bit service UUIDs: HID (0x1812)
        0d 09 38 42 69 74 ...    complete local name: "8BitDo 64 BT"

    Ours carried seven bytes -- flags and a bare HID UUID -- with the name and
    appearance in the scan response. **A passive scanner never sends a scan
    request and so never sees a scan response**, so a console filtering on name
    or appearance had nothing from us to match on.
    """

    def test_the_advertisement_carries_appearance_uuid_and_name(self):
        data = hogp.build_advertising_data("8BitDo 64 BT")
        assert data.hex() == "0319c403030312180d0938426974446f203634204254"

    def test_it_is_the_same_length_as_the_real_pad(self):
        # 25 bytes on air, once the kernel prepends its 3-byte Flags.
        assert len(hogp.build_advertising_data("8BitDo 64 BT")) + 3 == 25

    def test_the_advertisement_has_no_flags_structure(self):
        """The kernel adds Flags itself, and rejects the lot if we also do.

        Not a duplicate-and-harmless case: ``tlv_data_is_valid`` fails the whole
        advertisement, with the same Invalid Parameters status a length problem
        gives, so nothing says which field was wrong.
        """
        data = hogp.build_advertising_data("8BitDo 64 BT")
        i, types = 0, set()
        while i < len(data):
            types.add(data[i + 1])
            i += data[i] + 1
        assert hogp.AD_TYPE_FLAGS not in types

    def test_the_scan_response_still_carries_the_name(self):
        # For active scanners, which is what a real pad also answers with.
        assert b"8BitDo 64 BT" in hogp.build_scan_response("8BitDo 64 BT")

    def test_both_halves_fit_their_budget(self):
        adv = hogp.build_advertising_data("8BitDo 64 BT")
        response = hogp.build_scan_response("8BitDo 64 BT")
        assert len(adv) + 3 <= hogp.MAX_AD_BYTES     # 3 = the kernel's flags
        assert len(response) <= hogp.MAX_AD_BYTES

    def test_a_long_name_is_truncated_rather_than_overflowing(self):
        # An overflow is dropped silently, and the name is the field a console
        # is most likely to match on -- so it is cut to fit deliberately.
        adv = hogp.build_advertising_data("x" * 100)
        assert len(adv) + 3 <= hogp.MAX_AD_BYTES

    def test_a_truncated_advertisement_still_tiles_exactly(self):
        adv = hogp.build_advertising_data("x" * 100)
        i = 0
        while i < len(adv):
            assert adv[i] > 0
            i += adv[i] + 1
        assert i == len(adv), "AD structures must tile the buffer exactly"


class TestAdvertisingFlags:
    def test_connectable_and_discoverable_are_distinct_bits(self):
        assert hogp.ADV_FLAG_CONNECTABLE != hogp.ADV_FLAG_DISCOVERABLE

    def test_managed_flags_is_what_makes_the_kernel_add_the_flags_field(self):
        # 0x0008 in MGMT's advertising flags. Without it there is no Flags
        # structure at all, and a scanner filtering on LE General Discoverable
        # never sees us.
        assert hogp.ADV_FLAG_MANAGED_FLAGS == 0x0008


class TestTheRadioModeMustMatchTheTransport:
    """A dual-mode adapter cannot advertise "BR/EDR Not Supported".

    That flag is not cosmetic: it is how a BLE-only host tells a controller it
    can drive from one it cannot. Measured on hardware -- with BR/EDR enabled
    our advertisement carried flags 0x1a ("Simultaneous LE and BR/EDR"), and
    with the adapter switched to LE-only it carried 0x06 ("BR/EDR Not
    Supported"), everything else identical. The real 8BitDo 64 that pairs with
    an Analogue 3D advertises 0x05.

    Nothing we put in the advertising data can produce that bit: the kernel
    derives the flags from the controller's capabilities.
    """

    def test_ssp_is_not_demanded_of_an_le_only_adapter(self):
        """SSP is a BR/EDR concept and does not exist on an LE-only radio.

        Reporting it missing tells the operator that hosts will be prompted for
        a PIN, on a radio with no PIN pairing to fall back to -- a fault that
        cannot happen, on a path that is working.
        """
        from server.bt.adapter import _ensure_pairing_settings, _pairing_settings_ok

        # The LE-only settings measured on hci3 after switching modes.
        le_only = {"powered", "connectable", "le", "secure-conn", "wide-band-speech"}
        assert "br/edr" not in le_only
        # It would otherwise fail the check, which is the point.
        assert not _pairing_settings_ok(le_only)
        assert callable(_ensure_pairing_settings)

    def test_a_dual_mode_adapter_is_still_checked(self):
        from server.bt.adapter import _pairing_settings_ok

        healthy = {"powered", "connectable", "ssp", "br/edr", "le", "secure-conn"}
        assert _pairing_settings_ok(healthy)

    def test_the_transport_setting_exists_and_defaults_to_classic(self):
        # Classic reaches PCs, the Switch and 8BitDo/Mayflash receivers, which
        # is the larger set; BLE is opted into for a console that needs it.
        from server.config import ServerConfig

        assert ServerConfig().controller_transport == "classic"


class TestABlePeripheralMustBeBondable:
    """The Classic resting state is fatal on LE.

    Classic holds ``Pairable`` false outside a bounded pairing window, which is
    correct there: a Classic controller is reachable by page scan without being
    bondable, and the window is what invites a host to bond.

    HOGP has no such split. The Report characteristic requires an encrypted
    link, encryption requires a bond, and the advertisement *is* the invitation.
    An advertising peripheral that refuses bonding accepts a connection and can
    then do nothing with it.

    Measured: a host connected 21 times in three minutes and dropped the link
    every time, with ``Pairable=false`` the only thing in the way.
    """

    def test_the_input_report_requires_an_encrypted_link(self):
        """Which is what makes bonding mandatory rather than optional.

        Read as text: ``hid_service`` needs dbus-next, which is a server extra
        and absent on a client machine.
        """
        from pathlib import Path

        source = (
            Path(__file__).resolve().parent.parent
            / "server" / "bt" / "ble" / "hid_service.py"
        ).read_text(encoding="utf-8")
        assert "encrypt-read" in source

    def test_starting_a_ble_peripheral_sets_pairable(self):
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._start_ble)
        assert "pairable=True" in source

    def test_the_classic_path_still_scopes_pairable_to_a_window(self):
        # The Classic policy must not be widened by this. There, Pairable is
        # armed by set_pairable for a bounded window and cleared afterwards.
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager.set_pairable)
        assert "pairable=pairable" in source


class TestTheBlePathCanActuallyPair:
    """Two things a BLE peripheral needs that the Classic path got for free.

    Measured against the console: it sent an ordinary SMP Pairing Request
    (NoInputNoOutput, Bonding, No MITM) and our side answered **Pairing Failed,
    reason 0x05 "Pairing not supported"**. It then dropped the link, reconnected
    unencrypted, and could never subscribe to the Report characteristic -- so
    reports flowed to a host that was not listening.
    """

    def test_the_ble_path_registers_a_pairing_agent(self):
        """Without one, bluetoothd refuses pairing outright.

        The Classic path registers an agent from _start_hid. The BLE path had
        no equivalent, and the failure is silent on our side: the refusal is
        generated inside bluetoothd and nothing reaches our log.
        """
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._start_ble)
        assert "_ensure_agent()" in source

    def test_pairable_is_pinned_rather_than_left_to_expire(self):
        """BlueZ reverts Pairable on its PairableTimeout.

        This project *sets* that timeout when arming a Classic pairing window,
        so a leftover value turned a BLE peripheral unbondable minutes after
        start-up while it carried on advertising as though it could be paired.
        A BLE HID peripheral advertises continuously and the advertisement is
        the invitation to bond -- there is no window for it to sit outside.
        """
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._start_ble)
        assert "timeout_s=0" in source
        assert "pairable=True" in source

    def test_the_agent_is_registered_before_advertising_starts(self):
        # A console can connect the instant the advertisement goes out, so the
        # agent has to be in place first or the first attempt is refused.
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._start_ble)
        assert source.index("_ensure_agent()") < source.index("peripheral.start()")


class TestTheSinkFollowsTheLinkNotTheSubscription:
    """A bonded host does not re-subscribe when it reconnects.

    The CCCD is persistent for a bonded device, so ``StartNotify`` fires once
    and never again. Gating on it meant that after the first disconnect every
    report was dropped inside our own process while the link sat live and
    encrypted.

    Measured against the console: 223 reports delivered on the first
    connection, then **1226 dropped** across a reconnect, with the link showing
    ``PERIPHERAL AUTH ENCRYPT`` throughout.
    """

    def _sink(self):
        from server.bt.ble.peripheral import BLESink
        from server.bt.profiles import create_profile

        return BLESink(create_profile("8bitdo_64"), "CC:28:AA:6D:BB:F4")

    class _Chr:
        def __init__(self):
            self.notifying = False
            self.sent = []

        def notify(self, payload):
            self.sent.append(bytes(payload))
            return True

    def test_a_reconnected_bonded_host_still_receives(self):
        sink = self._sink()
        chrc = self._Chr()
        sink.attach(chrc)
        # The console reconnects: link up, but no StartNotify this time.
        sink.set_link(True, "A8:ED:71:F3:ED:FD")
        assert chrc.notifying is False, "the flag stays false -- that is the point"
        assert sink.is_connected is True

    def test_it_is_not_connected_before_a_link_exists(self):
        sink = self._sink()
        sink.attach(self._Chr())
        assert sink.is_connected is False

    def test_a_subscription_alone_still_counts(self):
        # The first connection, where StartNotify does fire.
        sink = self._sink()
        chrc = self._Chr()
        chrc.notifying = True
        sink.attach(chrc)
        assert sink.is_connected is True

    def test_losing_the_link_stops_it(self):
        sink = self._sink()
        sink.attach(self._Chr())
        sink.set_link(True, "A8:ED:71:F3:ED:FD")
        sink.set_link(False)
        assert sink.is_connected is False
        assert sink.peer == ""

    def test_reports_reach_the_characteristic_over_a_bare_link(self):
        from common.state import ControllerState

        sink = self._sink()
        chrc = self._Chr()
        sink.attach(chrc)
        sink.set_link(True, "A8:ED:71:F3:ED:FD")

        buf = bytearray(64)
        size = sink._profile.build_input_report(ControllerState(), buf)
        assert sink.send_input_report(bytes(buf[:size])) is True
        assert len(chrc.sent) == 1
        # And with the report id stripped, as HOGP requires.
        assert len(chrc.sent[0]) == size - 1

    def test_detach_clears_the_link(self):
        sink = self._sink()
        sink.attach(self._Chr())
        sink.set_link(True, "A8:ED:71:F3:ED:FD")
        sink.detach()
        assert sink.is_connected is False


class TestAPairingWindowMustNotUnpairABlePeripheral:
    """The Classic window teardown clears Pairable. BLE cannot survive that.

    A Classic controller is reachable by page scan without being bondable, so
    false is the correct resting state there. A BLE peripheral advertises
    continuously and the advertisement *is* the invitation to bond -- clearing
    Pairable leaves it accepting connections it can do nothing with.

    Observed on hardware: "Pairing window on hci1 expired; clearing
    discoverable" a couple of minutes after a window was armed, which quietly
    disabled bonding from then on. Long enough after the fact to look like an
    intermittent console fault rather than a timer.
    """

    def test_expiry_leaves_pairable_alone_on_ble(self):
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._expire_pairing_windows)
        assert "pairable=None if ble else False" in source

    def test_the_classic_path_still_clears_it(self):
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._expire_pairing_windows)
        # The Classic branch must still exist -- this is a per-transport
        # difference, not a blanket removal.
        assert "else False" in source


class TestSecureConnectionsIsOffOnTheBleTransport:
    """SC is not negotiable downward, and the console asks for Legacy.

    Secure Connections is the stronger pairing method and the right default in
    general, but a peer requesting **Legacy** pairing is refused outright
    rather than falling back. Measured against the console, whose Pairing
    Request reads "Bonding, No MITM, Legacy": with SC enabled every bond ended
    in ``bonding_attempt_complete ... status 0x5`` (authentication failed) and
    the link was dropped; with it off the bond completed and reports flowed.

    The trade is deliberate and worth stating: this weakens pairing. It is
    acceptable here because access control lives at the RBGC layer -- password
    plus operator approval -- not at Bluetooth pairing, which is the same
    reasoning the Classic agent already documents. The Classic transport is
    untouched.
    """

    def test_the_ble_switch_clears_secure_connections(self):
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._ensure_radio_mode)
        assert '"sc", "off" if want_ble else "on"' in source

    def test_classic_puts_it_back(self):
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._ensure_radio_mode)
        # Selecting Classic again must restore it rather than leaving every
        # adapter permanently weakened.
        assert 'else "on"' in source

    def test_an_already_le_only_adapter_still_gets_sc_cleared(self):
        """The early-return has to consider both halves.

        Checking only BR/EDR meant an adapter that was already LE-only -- every
        adapter, after the first run -- returned before Secure Connections was
        ever looked at, and stayed unpairable by the console.
        """
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._ensure_radio_mode)
        assert "has_sc" in source
        assert "has_bredr != want_ble and has_sc != want_ble" in source


class TestBondabilityMustNotExpireOnBle:
    """A pairing window bounds *discoverability*, never *bondability*.

    BlueZ has one timeout for each, and this code used to set both from the
    same value. On the BLE transport that is wrong: the peripheral advertises
    continuously and the advertisement is itself the invitation to bond, so an
    expiring Pairable turns it into a device that accepts connections and can
    do nothing with them.

    Observed on hardware: three adapters read `connectable discoverable
    bondable le` while the fourth -- the only one a window had been armed on,
    and the one the console was using -- read `connectable le`, with the link
    dropping to `PERIPHERAL` (no AUTH, no ENCRYPT). Nothing logged it; the flag
    was simply absent hours later.
    """

    def test_the_two_timeouts_are_set_independently(self):
        import inspect

        from server.bt import adapter_dbus

        source = inspect.getsource(adapter_dbus.set_properties)
        assert "pairable_timeout_s" in source

    def test_ble_pins_the_pairable_timeout_to_never(self):
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager.set_pairable)
        assert 'pairable_timeout_s=0 if self._transport() == "ble" else None' in source

    def test_classic_behaviour_is_unchanged(self):
        """Classic still lets both halves follow the window.

        There, Pairable false outside a window is the correct resting state --
        a Classic controller is reachable by page scan without being bondable.
        """
        import inspect

        from server.bt import adapter_dbus

        source = inspect.getsource(adapter_dbus.set_properties)
        assert "else timeout_s" in source


class TestArmingAWindowMustNotWipeABleBond:
    """The third and last path that forgot bonds behind the operator's back.

    Clearing bonds when a pairing window opens is correct on Classic and is
    documented there: a host that forgot us generates a fresh link key while we
    keep the old one, and authentication then fails with nothing useful to show
    for it.

    On BLE it is the opposite. A console usually cannot be told to forget, so
    removing only our half leaves it demanding a Long Term Key we no longer
    hold. Measured after a single window was armed: **54 LTK requests, 54
    negative replies in 18 seconds, zero SMP** -- the console retrying about
    three times a second, forever, with no path back for either side.
    """

    def test_ble_keeps_bonds_when_a_window_opens(self):
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager.set_pairable)
        assert 'if self._transport() == "ble":' in source
        assert "forget_bonds = False" in source

    def test_classic_still_clears_them(self):
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager.set_pairable)
        assert "if pairable and forget_bonds:" in source

    def test_no_ble_path_forgets_a_bond_without_being_asked(self):
        """The whole class of bug, stated once.

        Three separate paths removed bonds by default: disconnect, the pairing
        window, and the disconnect API. Each looked locally reasonable and each
        produced the same unrecoverable asymmetry.
        """
        import inspect

        from server.bt.adapter import AdapterManager

        assert "forget: bool = False" in inspect.getsource(
            AdapterManager.disconnect_host
        )
        assert "forget_bonds = False" in inspect.getsource(
            AdapterManager.set_pairable
        )


class TestDeviceInformationIsComplete:
    """All five characteristics, because a console that wants one we omit
    does not tell us.

    Measured against an Analogue 3D over a real LE link: it connects, encrypts,
    enumerates Device Information, finds only PnP ID and Manufacturer Name, and
    then issues ``Read Request`` on **handle 0x0000** -- a null handle, which is
    what a client does when its lookup for a required characteristic came back
    empty. We answer Invalid Handle, correctly, and it drops the link. The whole
    exchange takes under a second, our own counters report a healthy connection
    throughout, and there is no error anywhere to follow.

    Asserted against the stdlib half so it runs without dbus-next.
    ``build_application`` iterates this same tuple, so the built service cannot
    drift from it.
    """

    def test_it_lists_all_five(self):
        assert set(hogp.DEVICE_INFO_CHARACTERISTICS) == {
            hogp.MANUFACTURER_NAME_UUID,
            hogp.PNP_ID_UUID,
            hogp.FIRMWARE_REVISION_UUID,
            hogp.SERIAL_NUMBER_UUID,
            hogp.MODEL_NUMBER_UUID,
        }

    def test_they_are_in_the_order_the_real_pad_lists_them(self):
        """Cheap to match, and a client taking the Nth entry would care.

        Read from the pad's own GATT database: handles 0x0033, 0x0035, 0x0037,
        0x0039, 0x003b carry manufacturer, PnP, firmware, serial, model.
        """
        assert hogp.DEVICE_INFO_CHARACTERISTICS == (
            hogp.MANUFACTURER_NAME_UUID,
            hogp.PNP_ID_UUID,
            hogp.FIRMWARE_REVISION_UUID,
            hogp.SERIAL_NUMBER_UUID,
            hogp.MODEL_NUMBER_UUID,
        )

    def test_the_uuids_are_the_assigned_sixteen_bit_ones(self):
        for uuid, short in (
            (hogp.MODEL_NUMBER_UUID, "2a24"),
            (hogp.SERIAL_NUMBER_UUID, "2a25"),
            (hogp.FIRMWARE_REVISION_UUID, "2a26"),
            (hogp.MANUFACTURER_NAME_UUID, "2a29"),
            (hogp.PNP_ID_UUID, "2a50"),
        ):
            assert uuid == f"0000{short}-0000-1000-8000-00805f9b34fb"


class TestTheBuiltServiceMatchesThatList:
    """The integration half, skipped where dbus-next is absent."""

    def _info_service(self):
        pytest.importorskip("dbus_next")
        from server.bt.ble.hid_service import build_application

        app, _ = build_application("/test", bytes([0x05, 0x01, 0xC0]), 0x1234, 0x5678)
        for service in app.services:
            if service.uuid == hogp.DEVICE_INFO_SERVICE_UUID:
                return service
        raise AssertionError("no Device Information service was built")

    def test_it_builds_exactly_the_listed_characteristics(self):
        service = self._info_service()
        assert [c.uuid for c in service.characteristics] == list(
            hogp.DEVICE_INFO_CHARACTERISTICS
        )

    def test_every_one_of_them_is_readable_and_non_empty(self):
        """An empty value reads back as a zero-length string.

        That is not obviously better than the characteristic being absent, and
        it is the state an identity with nothing measured would fall into
        without the defaults in build_application.
        """
        for char in self._info_service().characteristics:
            assert "read" in char.flags
            assert char.value, f"{char.uuid} publishes nothing"


class TestTheVendorService:
    """The console asks for the 8BitDo vendor service by UUID.

    Measured on a live link: it discovers Device Information and HID
    successfully, then issues ``Find By Type Value`` for ``0xff10``, gets
    ``Attribute Not Found``, and immediately reads **handle 0x0000** -- the
    same null-handle signature that identified the missing Device Information
    characteristics. It goes on to subscribe and receive reports, then drops
    the link about 34.5 s later, on every adapter tried.

    What the service carries is unknown and proprietary. This reproduces its
    *shape* from the real pad's GATT database, which is what makes discovery
    succeed; whether the console also needs a meaningful exchange over it is
    the open question, and why writes are logged rather than ignored.
    """

    def test_the_uuids_are_the_pads(self):
        assert hogp.VENDOR_SERVICE_UUID == "0000ff10-0000-1000-8000-00805f9b34fb"
        assert hogp.VENDOR_RX_UUID == "0000ff11-0000-1000-8000-00805f9b34fb"
        assert hogp.VENDOR_TX_UUID == "0000ff12-0000-1000-8000-00805f9b34fb"

    def test_it_is_published(self):
        pytest.importorskip("dbus_next")
        from server.bt.ble.hid_service import build_application

        app, _ = build_application("/t", bytes([0x05, 0x01, 0xC0]), 0x2DC8, 0x3019)
        uuids = [s.uuid for s in app.services]
        assert hogp.VENDOR_SERVICE_UUID in uuids, (
            "the console searches for this service explicitly"
        )

    def test_the_characteristics_match_the_real_pad(self):
        """ff11 is readable, ff12 is not -- copied from the pad, not invented."""
        pytest.importorskip("dbus_next")
        from server.bt.ble.hid_service import build_application

        app, _ = build_application("/t", bytes([0x05, 0x01, 0xC0]), 0x2DC8, 0x3019)
        vendor = next(
            s for s in app.services if s.uuid == hogp.VENDOR_SERVICE_UUID
        )
        by_uuid = {c.uuid: c for c in vendor.characteristics}

        rx = by_uuid[hogp.VENDOR_RX_UUID]
        assert "read" in rx.flags
        assert "notify" in rx.flags
        assert "write" in rx.flags

        tx = by_uuid[hogp.VENDOR_TX_UUID]
        assert "read" not in tx.flags
        assert "notify" in tx.flags
        assert "write" in tx.flags

    def test_writes_are_surfaced_rather_than_swallowed(self):
        """The protocol is unknown, so the only way to learn it is to look."""
        pytest.importorskip("dbus_next")
        from server.bt.ble.hid_service import build_application

        seen = []
        app, _ = build_application(
            "/t", bytes([0x05, 0x01, 0xC0]), 0x2DC8, 0x3019,
            on_vendor_write=lambda which, data: seen.append((which, bytes(data))),
        )
        vendor = next(
            s for s in app.services if s.uuid == hogp.VENDOR_SERVICE_UUID
        )
        for char in vendor.characteristics:
            assert char._on_write is not None

        by_uuid = {c.uuid: c for c in vendor.characteristics}
        by_uuid[hogp.VENDOR_RX_UUID]._on_write(b"\x01\x02")
        assert seen == [("ff11", b"\x01\x02")]

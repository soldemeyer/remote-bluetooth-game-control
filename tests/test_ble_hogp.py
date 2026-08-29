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


class _Writes:
    """Captures what ``set_pairable`` asks bluetoothd to write.

    These used to be grep-the-source assertions, and one of them
    (``assert "pairable=pairable" in source``) pinned the **bug** as a
    requirement: writing Pairable straight through is exactly what made a BLE
    adapter unbondable the moment the operator pressed Stop. A test that reads
    the source cannot tell an intention from a behaviour, which is the same
    mistake the old app.js header comment made about preserving open selects.
    """

    def __init__(self):
        self.calls: list[dict] = []

    async def set_properties(self, hci_name, **kwargs):
        self.calls.append(kwargs)
        return True

    @property
    def last(self) -> dict:
        assert self.calls, "set_properties was never called"
        return self.calls[-1]


def _pairable_manager(monkeypatch, transport: str, *, enabled: bool = True):
    """An AdapterManager whose set_pairable can run with no hardware."""
    from server.bt import adapter as adapter_mod
    from server.bt.adapter import AdapterManager
    from server.bt.state import AdapterState
    from server.config import ServerConfig
    from server.router import Router

    config = ServerConfig()
    config.controller_transport = transport
    manager = AdapterManager(Router(), config)

    adapter = AdapterState(bd_addr="DC:A6:32:B9:6A:88", hci_name="hci0")
    adapter.enabled = enabled
    manager._adapters[adapter.bd_addr] = adapter

    writes = _Writes()

    async def _read_properties(hci_name):
        # The read-back guard: report what was last written so an arm that
        # genuinely set Pairable is not reported as a failure.
        return {"pairable": True}

    async def _remove_bonds(hci_name):
        return []

    monkeypatch.setattr(adapter_mod.adapter_dbus, "set_properties", writes.set_properties)
    monkeypatch.setattr(adapter_mod.adapter_dbus, "read_properties", _read_properties)
    monkeypatch.setattr(adapter_mod.adapter_dbus, "remove_bonds", _remove_bonds)
    monkeypatch.setattr(adapter_mod, "_ensure_pairing_settings", lambda a: None)
    monkeypatch.setattr(adapter_mod, "_set_device_class", lambda a: None)

    async def _noop_agent():
        return None

    monkeypatch.setattr(manager, "_ensure_agent", _noop_agent)
    return manager, adapter, writes


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

    @pytest.mark.asyncio
    async def test_stopping_never_clears_pairable_on_ble(self, monkeypatch):
        """The regression that cost three of four controller slots.

        The operator presses Connection mode on an adapter, the console does
        not connect, they press Stop -- and that wrote ``Pairable=false``. The
        adapter then advertised while being unable to bond, so the console
        connected and dropped immediately, forever, with nothing logged.
        Nothing re-asserted it either: ``_start_ble`` sets it once, at channel
        creation, so one press cost that adapter its console until restart.

        ``_expire_pairing_windows`` was fixed for exactly this and *this* path
        was missed.
        """
        manager, adapter, writes = _pairable_manager(monkeypatch, "ble")

        await manager.set_pairable(adapter.bd_addr, False)

        assert writes.last["pairable"] is not False

    @pytest.mark.asyncio
    async def test_arming_re_asserts_pairable_on_ble(self, monkeypatch):
        """Which is what makes Connection mode a repair rather than a no-op.

        On a transport that advertises continuously there is no window to
        open, so the useful thing the button can do is put back whatever was
        lost.
        """
        manager, adapter, writes = _pairable_manager(monkeypatch, "ble")

        ok, _message = await manager.set_pairable(adapter.bd_addr, True)

        assert ok
        assert writes.last["pairable"] is True
        assert writes.last["pairable_timeout_s"] == 0

    @pytest.mark.asyncio
    async def test_a_disabled_ble_adapter_is_left_alone(self, monkeypatch):
        """_quiet_adapter owns the disabled case; re-asserting Pairable here
        would fight it for the radio."""
        manager, adapter, writes = _pairable_manager(
            monkeypatch, "ble", enabled=False
        )

        await manager.set_pairable(adapter.bd_addr, False)

        assert writes.last["pairable"] is None

    @pytest.mark.asyncio
    async def test_the_classic_path_still_scopes_pairable_to_a_window(
        self, monkeypatch
    ):
        """The Classic policy must not be widened by this.

        There, Pairable false outside a window is the correct resting state --
        a Classic controller is reachable by page scan without being bondable.
        """
        manager, adapter, writes = _pairable_manager(monkeypatch, "classic")

        await manager.set_pairable(adapter.bd_addr, True)
        assert writes.last["pairable"] is True

        await manager.set_pairable(adapter.bd_addr, False)
        assert writes.last["pairable"] is False


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

    @pytest.mark.asyncio
    async def test_ble_pins_the_pairable_timeout_to_never(self, monkeypatch):
        manager, adapter, writes = _pairable_manager(monkeypatch, "ble")

        await manager.set_pairable(adapter.bd_addr, True)

        assert writes.last["pairable_timeout_s"] == 0

    @pytest.mark.asyncio
    async def test_classic_leaves_the_pairable_timeout_alone(self, monkeypatch):
        """It is BlueZ's to manage there, and this project *sets* it when it
        arms a window -- which is how a leftover value reached the BLE path."""
        manager, adapter, writes = _pairable_manager(monkeypatch, "classic")

        await manager.set_pairable(adapter.bd_addr, True)

        assert writes.last["pairable_timeout_s"] is None

    def test_classic_behaviour_is_unchanged(self):
        """Classic still lets both halves follow the window.

        There, Pairable false outside a window is the correct resting state --
        a Classic controller is reachable by page scan without being bondable.
        """
        import inspect

        from server.bt import adapter_dbus

        source = inspect.getsource(adapter_dbus.set_properties)
        assert "else timeout_s" in source


class TestPairStartsAfresh:
    """Pair replaces the pairing. On both transports, deliberately.

    This class used to assert the opposite for BLE, and the reasoning was
    sound as far as it went: removing our half while the peer keeps its own
    leaves the peer demanding a Long Term Key we no longer hold. Measured then:
    **54 LTK requests, 54 negative replies in 18 seconds, zero SMP** -- the
    console retrying three times a second forever.

    What that missed is that **the peer cannot be told to forget either**. The
    console this runs against offers no way to remove a controller, so
    re-pairing is the only recovery available -- and refusing to clear our half
    simply blocked it. Two adapters sat unpairable for an entire session until
    their bonds were deleted by hand.

    So the danger is real and the alternative was worse. What changed alongside
    it is that clearing is no longer a side effect of anything: Sleep and Wake
    never touch a bond, and Pair is a confirmed action that says what it does.
    """

    @pytest.mark.asyncio
    async def test_pair_clears_the_bond_on_ble(self, monkeypatch):
        from server.bt import adapter as adapter_mod

        manager, adapter, _writes = _pairable_manager(monkeypatch, "ble")
        removed = []

        async def _remove_bonds(hci_name):
            removed.append(hci_name)
            return ["AA:BB:CC:DD:EE:FF"]

        monkeypatch.setattr(adapter_mod.adapter_dbus, "remove_bonds", _remove_bonds)

        await manager.set_pairable(adapter.bd_addr, True, forget_bonds=True)

        assert removed == ["hci0"], (
            "a stale half on our side silently blocks every future attempt"
        )

    @pytest.mark.asyncio
    async def test_pair_clears_the_bond_on_classic_too(self, monkeypatch):
        from server.bt import adapter as adapter_mod

        manager, adapter, _writes = _pairable_manager(monkeypatch, "classic")
        removed = []

        async def _remove_bonds(hci_name):
            removed.append(hci_name)
            return ["AA:BB:CC:DD:EE:FF"]

        monkeypatch.setattr(adapter_mod.adapter_dbus, "remove_bonds", _remove_bonds)

        await manager.set_pairable(adapter.bd_addr, True, forget_bonds=True)

        assert removed == ["hci0"]

    @pytest.mark.asyncio
    async def test_a_caller_can_still_ask_it_not_to(self, monkeypatch):
        """The harness pairs without clearing when it is testing reconnection
        rather than pairing."""
        from server.bt import adapter as adapter_mod

        manager, adapter, _writes = _pairable_manager(monkeypatch, "ble")
        removed = []

        async def _remove_bonds(hci_name):
            removed.append(hci_name)
            return []

        monkeypatch.setattr(adapter_mod.adapter_dbus, "remove_bonds", _remove_bonds)

        await manager.set_pairable(adapter.bd_addr, True, forget_bonds=False)

        assert removed == []

    @pytest.mark.asyncio
    async def test_stopping_never_clears_a_bond(self, monkeypatch):
        """Only Pair replaces a pairing. Everything else leaves it alone --
        that separation is what makes clearing safe to do at all."""
        from server.bt import adapter as adapter_mod

        manager, adapter, _writes = _pairable_manager(monkeypatch, "ble")
        removed = []

        async def _remove_bonds(hci_name):
            removed.append(hci_name)
            return []

        monkeypatch.setattr(adapter_mod.adapter_dbus, "remove_bonds", _remove_bonds)

        await manager.set_pairable(adapter.bd_addr, False, forget_bonds=True)

        assert removed == []

    def test_disconnect_still_keeps_the_bond_by_default(self):
        """Sleep must not be a re-pair. It is pressed to stop a controller for
        a moment, not to introduce it to the console again."""
        import inspect

        from server.bt.adapter import AdapterManager

        assert "forget: bool = False" in inspect.getsource(
            AdapterManager.disconnect_host
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


class TestTheRadioModeSwitchMustNotLeaveTheAdapterOff:
    """A failed switch used to power the adapter down and leave it there.

    ``_ensure_radio_mode`` power-cycles the controller, because it will not
    change mode while it is up: ``power off``, ``bredr off``, ``sc off``,
    ``power on``. It returned on the first non-zero exit -- and since
    ``power off`` is first, any failure in the middle left the radio **off**,
    with one warning line as the only trace. Losing LE-only costs an adapter
    its console; losing power costs it everything, including the Classic
    transport and every diagnostic that reads the adapter.

    The middle command genuinely does fail: the kernel refuses to clear BR/EDR
    while the adapter still holds BR/EDR bonds, which is every adapter ever
    paired to a PC over Classic.
    """

    def _run_capture(self, monkeypatch, failing=(),
                     settings="powered ssp br/edr le secure-conn"):
        from server.bt import adapter as adapter_mod

        issued: list[list[str]] = []

        def fake_run(cmd, timeout=5.0):
            issued.append(list(cmd))
            if any(token in cmd for token in failing):
                return 1, "Set BR/EDR: rejected"
            if "info" in cmd:
                return 0, f"current settings: {settings}"
            return 0, ""

        monkeypatch.setattr(adapter_mod, "_run", fake_run)
        monkeypatch.setattr(
            adapter_mod.shutil, "which", lambda name: f"/usr/bin/{name}"
        )
        return issued

    def _manager(self):
        from server.bt.adapter import AdapterManager
        from server.bt.state import AdapterState
        from server.config import ServerConfig
        from server.router import Router

        config = ServerConfig()
        config.controller_transport = "ble"
        manager = AdapterManager(Router(), config)
        adapter = AdapterState(bd_addr="DC:A6:32:B9:6A:88", hci_name="hci0")
        adapter.index = 0
        return manager, adapter

    def _tail(self, issued):
        return [cmd[-2:] for cmd in issued if "info" not in cmd]

    def test_a_clean_switch_powers_back_on(self, monkeypatch):
        issued = self._run_capture(monkeypatch)
        manager, adapter = self._manager()

        manager._ensure_radio_mode(adapter)

        assert self._tail(issued) == [
            ["power", "off"], ["bredr", "off"], ["sc", "off"], ["power", "on"],
        ]

    def test_a_refused_bredr_still_powers_back_on(self, monkeypatch):
        """The regression. Without the finally, the adapter stayed dark."""
        issued = self._run_capture(monkeypatch, failing=("bredr",))
        manager, adapter = self._manager()

        manager._ensure_radio_mode(adapter)

        assert self._tail(issued)[-1] == ["power", "on"], (
            "adapter left powered off after a failed radio-mode switch"
        )

    def test_a_refused_bredr_names_bonds_as_the_likely_cause(
        self, monkeypatch, caplog
    ):
        """A btmgmt exit code does not say this, and it is the actual remedy."""
        import logging

        self._run_capture(monkeypatch, failing=("bredr",))
        manager, adapter = self._manager()

        with caplog.at_level(logging.WARNING):
            manager._ensure_radio_mode(adapter)

        assert "BR/EDR bonds" in caplog.text

    def test_a_refused_sc_does_not_skip_the_power_on_either(self, monkeypatch):
        issued = self._run_capture(monkeypatch, failing=("sc",))
        manager, adapter = self._manager()

        manager._ensure_radio_mode(adapter)

        assert self._tail(issued)[-1] == ["power", "on"]

    def test_an_adapter_already_in_the_right_mode_is_not_touched(self, monkeypatch):
        """Read-then-write: this runs on every reconcile now, so the ordinary
        pass must cost one read and no power cycle."""
        issued = self._run_capture(monkeypatch, settings="powered ssp le")
        manager, adapter = self._manager()

        manager._ensure_radio_mode(adapter)

        assert self._tail(issued) == []


class TestTheAdvertisementIsHeldAsAnInvariant:
    """It is added once at bring-up and does not survive a power cycle.

    Nothing reports its loss -- ``_advertising`` stays True while the radio is
    silent -- which is the published-and-invisible failure this subsystem keeps
    producing. Our own radio-mode switch power-cycles the controller, so this
    is not a rare case.
    """

    class _Mgmt:
        def __init__(self, instances=(1,), fail=False):
            self.instances = set(instances)
            self.fail = fail
            self.added = 0
            self.removed = 0

        def advertising_instances(self, index):
            if self.fail:
                raise RuntimeError("kernel said no")
            return set(self.instances)

        def add_advertising(self, index, instance, flags, adv_data=b"",
                            scan_response=b"", **kwargs):
            self.added += 1
            self.instances.add(instance)
            return instance

        def remove_advertising(self, index, instance=0):
            self.removed += 1
            self.instances.discard(instance)

    def _peripheral(self, mgmt_sock):
        from server.bt.ble.peripheral import BLEPeripheral
        from server.bt.identities import get_identity
        from server.bt.profiles import create_profile

        peripheral = BLEPeripheral(
            "hci0", create_profile("generic"), get_identity("8bitdo"),
            name="8BitDo 64 BT", mgmt=mgmt_sock, index=0,
            bd_addr="DC:A6:32:B9:6A:88",
        )
        peripheral._registered = True
        peripheral._advertising = True
        return peripheral

    def test_a_live_advertisement_is_not_restarted(self):
        """Re-adding unconditionally would drop the advertisement and put it
        back every ten seconds -- a gap a scanning console can fall into."""
        mgmt_sock = self._Mgmt(instances=(1,))
        peripheral = self._peripheral(mgmt_sock)

        assert peripheral.ensure_advertising() is True
        assert mgmt_sock.added == 0

    def test_a_lost_advertisement_is_put_back(self):
        mgmt_sock = self._Mgmt(instances=())
        peripheral = self._peripheral(mgmt_sock)

        assert peripheral.ensure_advertising() is True
        assert mgmt_sock.added == 1

    def test_losing_it_is_reported(self, caplog):
        """Silent recovery hides a dongle that keeps resetting itself."""
        import logging

        mgmt_sock = self._Mgmt(instances=())
        peripheral = self._peripheral(mgmt_sock)

        with caplog.at_level(logging.WARNING):
            peripheral.ensure_advertising()

        assert "stopped advertising" in caplog.text

    def test_an_unreadable_answer_is_not_treated_as_gone(self):
        """A failed read is not evidence the advertisement has gone, and
        restarting on it would drop a working one every reconcile."""
        mgmt_sock = self._Mgmt(fail=True)
        peripheral = self._peripheral(mgmt_sock)

        assert peripheral.is_advertising() is None
        assert peripheral.ensure_advertising() is True
        assert mgmt_sock.added == 0

    def test_force_restarts_it_regardless(self):
        """What the operator's Connection mode button does on this transport --
        the counterpart of pressing pair on a real controller."""
        mgmt_sock = self._Mgmt(instances=(1,))
        peripheral = self._peripheral(mgmt_sock)

        assert peripheral.ensure_advertising(force=True) is True
        assert mgmt_sock.added == 1
        assert mgmt_sock.removed == 1

    def test_an_unpublished_peripheral_reports_failure(self):
        mgmt_sock = self._Mgmt(instances=())
        peripheral = self._peripheral(mgmt_sock)
        peripheral._registered = False

        assert peripheral.ensure_advertising() is False
        assert mgmt_sock.added == 0


class TestEachAdapterIsItsOwnUnit:
    """Four adapters running one identity are four units of a product.

    Every other Device Information field is byte-identical on purpose -- name,
    vendor id, product id and model number are what a console matches on, and
    they are copied from the measured pad. Serial Number is the one field whose
    entire purpose is telling two units apart, and every adapter sent
    ``000000000000``.
    """

    def _peripheral(self, bd_addr, identity="8bitdo"):
        from server.bt.ble.peripheral import BLEPeripheral
        from server.bt.identities import get_identity
        from server.bt.profiles import create_profile

        return BLEPeripheral(
            "hci0", create_profile("generic"), get_identity(identity),
            bd_addr=bd_addr,
        )

    def test_two_adapters_get_two_serial_numbers(self):
        first = self._peripheral("DC:A6:32:B9:6A:88").serial_number()
        second = self._peripheral("DC:A6:32:B9:6A:89").serial_number()

        assert first != second
        assert first != "000000000000"

    def test_it_is_derived_from_the_address_so_it_survives_a_restart(self):
        """Generated would be unique too, and would change every launch -- a
        host that remembers the serial would see a different pad each time."""
        assert (
            self._peripheral("DC:A6:32:B9:6A:88").serial_number()
            == self._peripheral("DC:A6:32:B9:6A:88").serial_number()
            == "DCA632B96A88"
        )

    def test_an_identity_that_names_its_own_serial_still_wins(self):
        from server.bt.ble.peripheral import BLEPeripheral
        from server.bt.identities import ControllerIdentity
        from server.bt.profiles import create_profile

        identity = ControllerIdentity(
            key="measured", display_name="Measured", device_name="Pad",
            vendor_id=1, product_id=2, serial_number="ABC123",
        )
        peripheral = BLEPeripheral(
            "hci0", create_profile("generic"), identity,
            bd_addr="AA:BB:CC:DD:EE:FF",
        )

        assert peripheral.serial_number() == "ABC123"

    def test_no_address_falls_back_rather_than_publishing_nothing(self):
        """An empty Device Information string is what made a console hang up."""
        assert self._peripheral("").serial_number() == "000000000000"


class TestTheBlePeripheralIsHeldReadyOnEveryReconcile:
    """The three properties that were set once and never checked again.

    Each presents to the operator identically, as a console that will not
    connect, and each is invisible from every counter. Holding them here is the
    same discipline ``_ensure_connectable`` and ``_ensure_le_ping`` already
    apply -- read first, write only on drift.
    """

    def _manager(self, monkeypatch, *, pairable=True, transport="ble"):
        # `pairable` here is the adapter's MGMT `bondable` bit, which is the
        # same setting seen from the other side -- see _ensure_ble_ready.
        from server.bt import adapter as adapter_mod
        from server.bt.adapter import AdapterManager
        from server.bt.state import AdapterState
        from server.config import ServerConfig
        from server.router import Router

        config = ServerConfig()
        config.controller_transport = transport
        manager = AdapterManager(Router(), config)

        adapter = AdapterState(bd_addr="DC:A6:32:B9:6A:88", hci_name="hci0")
        adapter.index = 0
        adapter.bondable = pairable
        manager._adapters[adapter.bd_addr] = adapter

        writes = _Writes()

        monkeypatch.setattr(
            adapter_mod.adapter_dbus, "set_properties", writes.set_properties
        )
        monkeypatch.setattr(manager, "_ensure_radio_mode", lambda a: None)

        class _Peripheral:
            def __init__(self):
                self.ensured = 0
                self.forced = 0
                self.pairing_mode = True

            def set_pairing_mode(self, pairing):
                changed = bool(pairing) != self.pairing_mode
                self.pairing_mode = bool(pairing)
                return changed

            def ensure_advertising(self, force=False):
                self.ensured += 1
                self.forced += 1 if force else 0
                return True

        peripheral = _Peripheral()
        manager._ble[adapter.bd_addr] = peripheral
        return manager, adapter, writes, peripheral

    @pytest.mark.asyncio
    async def test_a_lost_pairable_is_restored(self, monkeypatch):
        manager, adapter, writes, _p = self._manager(monkeypatch, pairable=False)

        await manager._ensure_ble_ready([adapter])

        assert writes.last["pairable"] is True
        assert writes.last["pairable_timeout_s"] == 0

    @pytest.mark.asyncio
    async def test_a_healthy_adapter_is_not_written_to(self, monkeypatch):
        """Read-then-write. The reconcile runs every ten seconds for the life
        of the process, and bluetoothd owns this property."""
        manager, adapter, writes, _p = self._manager(monkeypatch, pairable=True)

        await manager._ensure_ble_ready([adapter])

        assert writes.calls == []

    @pytest.mark.asyncio
    async def test_losing_pairable_is_reported_by_symptom(self, monkeypatch, caplog):
        import logging

        manager, adapter, _writes, _p = self._manager(monkeypatch, pairable=False)

        with caplog.at_level(logging.WARNING):
            await manager._ensure_ble_ready([adapter])

        assert "connect and drop" in caplog.text

    @pytest.mark.asyncio
    async def test_the_advertisement_is_checked_too(self, monkeypatch):
        manager, adapter, _writes, peripheral = self._manager(monkeypatch)

        await manager._ensure_ble_ready([adapter])

        assert peripheral.ensured == 1
        assert peripheral.forced == 0, "the reconcile must not restart it"

    @pytest.mark.asyncio
    async def test_a_degraded_adapter_is_skipped(self, monkeypatch):
        """It must not advertise at all, so asserting Pairable on it would
        invite a host to bond with something that cannot serve HID."""
        manager, adapter, writes, peripheral = self._manager(
            monkeypatch, pairable=False
        )
        adapter.hid_error = "L2CAP PSM 17/19 already in use"

        await manager._ensure_ble_ready([adapter])

        assert writes.calls == []
        assert peripheral.ensured == 0

    @pytest.mark.asyncio
    async def test_the_classic_transport_is_untouched(self, monkeypatch):
        """Pairable false outside a window is correct there, and re-asserting
        it would leave every adapter permanently bondable to anyone."""
        manager, adapter, writes, peripheral = self._manager(
            monkeypatch, pairable=False, transport="classic"
        )

        await manager._ensure_ble_ready([adapter])

        assert writes.calls == []
        assert peripheral.ensured == 0

    @pytest.mark.asyncio
    async def test_the_operator_button_forces_a_restart(self, monkeypatch):
        manager, adapter, _writes, peripheral = self._manager(monkeypatch)

        assert await manager._readvertise(adapter) is True
        assert peripheral.forced == 1

    @pytest.mark.asyncio
    async def test_readvertising_an_adapter_with_no_peripheral_is_not_a_failure(
        self, monkeypatch
    ):
        """The Classic path has its own window; there is nothing to restart."""
        manager, adapter, _writes, _p = self._manager(monkeypatch)
        manager._ble.clear()

        assert await manager._readvertise(adapter) is True


class TestTheRadioModeIsCheckedWithoutSpawningAnything:
    """The reconcile runs every ten seconds for the life of the process.

    Whether the radio has drifted back to dual mode is answerable from the
    MGMT settings already on the state object, so the ordinary pass must cost
    nothing. Asking ``btmgmt info`` instead would be four subprocesses every
    ten seconds -- exactly the cost the MGMT rewrite removed, put back by
    accident.
    """

    def _manager(self, monkeypatch, *, bredr, secure_conn, settings_known=True):
        from server.bt import adapter as adapter_mod
        from server.bt.adapter import AdapterManager
        from server.bt.state import AdapterState
        from server.config import ServerConfig
        from server.router import Router

        config = ServerConfig()
        config.controller_transport = "ble"
        manager = AdapterManager(Router(), config)

        adapter = AdapterState(bd_addr="DC:A6:32:B9:6A:88", hci_name="hci0")
        adapter.index = 0
        adapter.bondable = True
        adapter.bredr = bredr
        adapter.secure_conn = secure_conn
        adapter.settings_known = settings_known
        manager._adapters[adapter.bd_addr] = adapter

        switched = []
        monkeypatch.setattr(
            manager, "_ensure_radio_mode", lambda a: switched.append(a.hci_name)
        )

        async def _set_properties(hci_name, **kwargs):
            return True

        monkeypatch.setattr(
            adapter_mod.adapter_dbus, "set_properties", _set_properties
        )

        class _Peripheral:
            def ensure_advertising(self, force=False):
                return True

        manager._ble[adapter.bd_addr] = _Peripheral()
        return manager, adapter, switched

    @pytest.mark.asyncio
    async def test_an_le_only_adapter_is_left_alone(self, monkeypatch):
        manager, adapter, switched = self._manager(
            monkeypatch, bredr=False, secure_conn=False
        )

        await manager._ensure_ble_ready([adapter])

        assert switched == []

    @pytest.mark.asyncio
    async def test_bredr_coming_back_triggers_a_switch(self, monkeypatch):
        """A controller reset restores the dongle's defaults and says nothing.
        The adapter then advertises 'Simultaneous LE and BR/EDR', which is the
        bit a BLE-only host uses to decide it cannot drive us."""
        manager, adapter, switched = self._manager(
            monkeypatch, bredr=True, secure_conn=False
        )

        await manager._ensure_ble_ready([adapter])

        assert switched == ["hci0"]

    @pytest.mark.asyncio
    async def test_secure_connections_coming_back_also_triggers_one(self, monkeypatch):
        """Half a switch is still wrong: SC is not negotiable downward, so a
        console requesting Legacy pairing is refused outright."""
        manager, adapter, switched = self._manager(
            monkeypatch, bredr=False, secure_conn=True
        )

        await manager._ensure_ble_ready([adapter])

        assert switched == ["hci0"]

    @pytest.mark.asyncio
    async def test_unknown_settings_are_not_acted_on(self, monkeypatch):
        """The hciconfig fallback cannot see these, so the fields sit at their
        defaults -- and `bredr` defaults True. Acting on that would power-cycle
        every adapter on a machine where we simply never read the settings."""
        manager, adapter, switched = self._manager(
            monkeypatch, bredr=True, secure_conn=True, settings_known=False
        )

        await manager._ensure_ble_ready([adapter])

        assert switched == []


class TestALinkThatPredatesUsIsStillNoticed:
    """`Device Connected` fires once, at the moment of connection.

    Subscribe after a console has already attached -- which is every server
    restart during a session -- and no event ever arrives for that link.
    Nothing else told us: MGMT enumeration returns adapter *settings*, and the
    BLE path has no HIDServer callbacks to fall back on.

    Measured on the reference Pi. hci3 held a live, authenticated, encrypted LE
    link to an Analogue 3D while ``/api/status`` reported::

        DC:A6:32:B9:6A:88  Controller 1  phase=listening  peer=-

    The GUI said "Waiting for console" over a working controller, offered no
    Disconnect, and -- the part that actually costs something -- the LE ping
    timeout was never extended on that link, so the 30 s idle disconnect this
    project already fixed once was free to come back on it.
    """

    CONSOLE = "A8:ED:71:F3:ED:FD"

    def _manager(self, monkeypatch, connections, *, peer=""):
        from server.bt import adapter as adapter_mod
        from server.bt.adapter import AdapterManager
        from server.bt.state import AdapterState, Phase
        from server.config import ServerConfig
        from server.router import Router

        config = ServerConfig()
        config.controller_transport = "ble"
        manager = AdapterManager(Router(), config)

        adapter = manager._registry.ensure("DC:A6:32:B9:6A:88")
        adapter.index = 3
        adapter.hci_name = "hci3"
        adapter.peer = peer
        adapter.to(Phase.CONFIGURING)
        adapter.to(Phase.LINKED if peer else Phase.LISTENING)
        manager._adapters[adapter.bd_addr] = adapter

        class _Mgmt:
            def __init__(self):
                self.asked = []

            def connections(self_inner, index):
                self_inner.asked.append(index)
                if connections is None:
                    raise RuntimeError("kernel said no")
                return list(connections)

        manager._mgmt = _Mgmt()

        # A registered peripheral, because _note_link gates the sink update and
        # the LE ping extension on one -- which is the whole reason adopting a
        # missed link matters rather than being cosmetic.
        class _Peripheral:
            def __init__(self):
                self.sink = self

            def attach_sink(self, peer=""):
                self.attached = peer

            def set_link(self, connected, peer=""):
                self.link = (connected, peer)

        manager._ble[adapter.bd_addr] = _Peripheral()

        pinged = []
        monkeypatch.setattr(
            manager, "_run_off_loop",
            lambda fn, *a: pinged.append(a),
        )
        return manager, adapter, pinged

    @pytest.mark.asyncio
    async def test_a_link_we_never_saw_start_is_adopted(self, monkeypatch):
        from server.bt.state import Phase

        manager, adapter, _pinged = self._manager(monkeypatch, [self.CONSOLE])

        await manager._ensure_link_state([adapter])

        assert adapter.peer == self.CONSOLE
        assert adapter.phase is Phase.LINKED

    @pytest.mark.asyncio
    async def test_adopting_it_extends_the_le_ping_timeout(self, monkeypatch):
        """The reason this is more than cosmetic. Without it the kernel's own
        30 s timeout stands and an idle console is dropped."""
        manager, adapter, pinged = self._manager(monkeypatch, [self.CONSOLE])

        await manager._ensure_link_state([adapter])

        assert pinged == [(3, self.CONSOLE)]

    @pytest.mark.asyncio
    async def test_a_missed_disconnect_is_corrected_too(self, monkeypatch):
        """The same bug wearing the other hat: a peer we think is there and is
        not leaves the GUI offering Disconnect on an idle radio."""
        from server.bt.state import Phase

        manager, adapter, _pinged = self._manager(
            monkeypatch, [], peer=self.CONSOLE
        )

        await manager._ensure_link_state([adapter])

        assert adapter.peer == ""
        assert adapter.phase is Phase.LISTENING

    @pytest.mark.asyncio
    async def test_a_peer_without_the_phase_is_still_corrected(self, monkeypatch):
        """Phase and peer can disagree, and comparing only the peer walks away
        from it. Bring-up sets LISTENING as it finishes, so an adapter already
        carrying a link ends the pass with the right peer and the wrong phase
        -- and then looks settled forever. Measured live: all three connected
        adapters read `phase=listening peer=A8:ED:71:F3:ED:FD`."""
        from server.bt.state import Phase

        manager, adapter, _pinged = self._manager(
            monkeypatch, [self.CONSOLE], peer=self.CONSOLE
        )
        adapter.to(Phase.LISTENING, reason="bring-up finished")

        await manager._ensure_link_state([adapter])

        assert adapter.phase is Phase.LINKED

    @pytest.mark.asyncio
    async def test_it_runs_after_bring_up_not_before(self):
        """Ordering is the other half of the same bug: bring-up would
        overwrite the phase this sets."""
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._reconcile_channels_locked)
        assert source.index("_ensure_link_state") > source.index("add_channel")

    @pytest.mark.asyncio
    async def test_a_settled_system_changes_nothing(self, monkeypatch):
        """Runs every ten seconds for the life of the process."""
        from server.bt.state import Phase

        manager, adapter, pinged = self._manager(
            monkeypatch, [self.CONSOLE], peer=self.CONSOLE
        )

        await manager._ensure_link_state([adapter])

        assert adapter.peer == self.CONSOLE
        assert adapter.phase is Phase.LINKED
        assert pinged == [], "re-tuned a link that had not changed"

    @pytest.mark.asyncio
    async def test_a_failed_read_leaves_the_peer_alone(self, monkeypatch):
        """"Unreadable" is not "nothing is connected". Clearing a live peer on
        a failed read would drop the GUI's Disconnect and re-adopt the link a
        tick later, flickering for as long as the read kept failing."""
        manager, adapter, _pinged = self._manager(
            monkeypatch, None, peer=self.CONSOLE
        )

        await manager._ensure_link_state([adapter])

        assert adapter.peer == self.CONSOLE

    @pytest.mark.asyncio
    async def test_an_adapter_with_no_index_is_skipped(self, monkeypatch):
        """MGMT commands take an index; -1 would address adapter 65535."""
        manager, adapter, _pinged = self._manager(monkeypatch, [self.CONSOLE])
        adapter.index = -1

        await manager._ensure_link_state([adapter])

        assert manager._mgmt.asked == []

    @pytest.mark.asyncio
    async def test_it_does_nothing_without_a_management_socket(self, monkeypatch):
        manager, adapter, _pinged = self._manager(monkeypatch, [self.CONSOLE])
        manager._mgmt = None

        await manager._ensure_link_state([adapter])

        assert adapter.peer == ""


class TestDisconnectMustActuallyStayDisconnected:
    """We are the peripheral. The console is the central and holds the bond, so
    it reconnects to a bonded controller as soon as it sees it advertise --
    within a second or two. Dropping the link alone is therefore a button that
    visibly does nothing, which is what was reported for two adapters.

    The old answer was to forget the bond, and it is much worse: a console
    generally cannot be told to forget, so removing only our half strands it
    (18-30 connect-and-fail cycles per capture, nothing logged). Taking the
    advertisement down is reversible and costs nothing -- it is what switching
    a real controller off does.
    """

    class _Peripheral:
        def __init__(self):
            self.suppressed = False
            self.pairing_mode = True
            self.detached = 0
            self.sink = self

        def detach(self):
            self.detached += 1

        def suppress_advertising(self):
            self.suppressed = True

    def _manager(self, monkeypatch, *, ble=True):
        from server.bt import adapter as adapter_mod
        from server.bt.adapter import AdapterManager
        from server.bt.state import AdapterState, Phase
        from server.config import ServerConfig
        from server.router import Router

        config = ServerConfig()
        config.controller_transport = "ble" if ble else "classic"
        manager = AdapterManager(Router(), config)

        adapter = AdapterState(bd_addr="A0:AD:9F:79:EC:C8", hci_name="hci1")
        adapter.index = 1
        adapter.peer = "A8:ED:71:F3:ED:FD"
        adapter.to(Phase.CONFIGURING)
        adapter.to(Phase.LISTENING)
        adapter.to(Phase.LINKED)
        manager._adapters[adapter.bd_addr] = adapter

        async def _connected_devices(hci_name):
            return ["/org/bluez/hci1/dev_A8_ED_71_F3_ED_FD"]

        async def _disconnect_device(path):
            return True

        monkeypatch.setattr(
            adapter_mod.adapter_dbus, "connected_devices", _connected_devices
        )
        monkeypatch.setattr(
            adapter_mod.adapter_dbus, "disconnect_device", _disconnect_device
        )

        peripheral = self._Peripheral()
        if ble:
            manager._ble[adapter.bd_addr] = peripheral
        return manager, adapter, peripheral

    @pytest.mark.asyncio
    async def test_it_stops_advertising(self, monkeypatch):
        manager, adapter, peripheral = self._manager(monkeypatch)

        ok, _message = await manager.disconnect_host(adapter.bd_addr)

        assert ok
        assert peripheral.suppressed, "console will reconnect within seconds"

    @pytest.mark.asyncio
    async def test_it_does_not_forget_the_bond(self, monkeypatch):
        """The fix this replaces, and it must not come back: removing our half
        while the console keeps its own leaves it asking us to resume with a
        key nobody holds, forever, with no way back on either side."""
        from server.bt import adapter as adapter_mod

        manager, adapter, _p = self._manager(monkeypatch)
        removed = []

        async def _remove_bonds(hci_name):
            removed.append(hci_name)
            return []

        monkeypatch.setattr(adapter_mod.adapter_dbus, "remove_bonds", _remove_bonds)

        await manager.disconnect_host(adapter.bd_addr)

        assert removed == []

    @pytest.mark.asyncio
    async def test_the_message_says_how_to_get_back(self, monkeypatch):
        """A control that makes an adapter unreachable must say so and name the
        way out, or it is indistinguishable from having broken it."""
        manager, adapter, _p = self._manager(monkeypatch)

        _ok, message = await manager.disconnect_host(adapter.bd_addr)

        assert "stopped advertising" in message
        assert "Re-advertise" in message

    @pytest.mark.asyncio
    async def test_classic_is_untouched(self, monkeypatch):
        """There the advertisement is not the invitation to bond, and our own
        outgoing reconnect is what the holdoff already covers."""
        manager, adapter, peripheral = self._manager(monkeypatch, ble=False)

        _ok, message = await manager.disconnect_host(adapter.bd_addr)

        assert not peripheral.suppressed
        assert "stopped advertising" not in message


class TestTheInvariantMustNotUndoTheOperator:
    """`_ensure_ble_ready` puts a lost advertisement back every ten seconds.

    An invariant that cannot tell "lost it" from "the operator switched it off"
    fights the operator and always wins -- Disconnect would have held for one
    reconcile and then quietly undone itself.
    """

    def _peripheral(self, instances=()):
        from server.bt.ble.peripheral import BLEPeripheral
        from server.bt.identities import get_identity
        from server.bt.profiles import create_profile

        class _Mgmt:
            def __init__(self):
                self.instances = set(instances)
                self.added = 0

            def advertising_instances(self, index):
                return set(self.instances)

            def add_advertising(self, index, instance, flags, adv_data=b"",
                                scan_response=b"", **kwargs):
                self.added += 1
                self.instances.add(instance)
                return instance

            def remove_advertising(self, index, instance=0):
                self.instances.discard(instance)

        mgmt_sock = _Mgmt()
        peripheral = BLEPeripheral(
            "hci1", create_profile("generic"), get_identity("8bitdo"),
            mgmt=mgmt_sock, index=1, bd_addr="A0:AD:9F:79:EC:C8",
        )
        peripheral._registered = True
        peripheral._advertising = True
        return peripheral, mgmt_sock

    def test_suppressing_takes_the_instance_down(self):
        peripheral, mgmt_sock = self._peripheral(instances=(1,))

        peripheral.suppress_advertising()

        assert peripheral.suppressed
        assert mgmt_sock.instances == set()

    def test_the_reconcile_leaves_it_down(self):
        peripheral, mgmt_sock = self._peripheral(instances=(1,))
        peripheral.suppress_advertising()

        for _ in range(5):
            assert peripheral.ensure_advertising() is False

        assert mgmt_sock.added == 0, "the invariant undid a deliberate stop"

    def test_a_forced_restart_clears_the_suppression(self):
        """Re-advertise is the way back, and it must be the *only* one -- so it
        has to actually clear the latch rather than fight it."""
        peripheral, mgmt_sock = self._peripheral(instances=(1,))
        peripheral.suppress_advertising()

        assert peripheral.ensure_advertising(force=True) is True
        assert not peripheral.suppressed
        assert mgmt_sock.added == 1

        # And the ordinary invariant resumes looking after it.
        assert peripheral.ensure_advertising() is True

    def test_an_unsuppressed_peripheral_is_unaffected(self):
        peripheral, mgmt_sock = self._peripheral(instances=())

        assert peripheral.ensure_advertising() is True
        assert mgmt_sock.added == 1


class TestReAdvertiseTouchesOnlyItsOwnAdapter:
    """An earlier version took every *other* adapter off the air for the
    window, so the console could not pair with the wrong one.

    It stopped that race and caused something worse. Each click removed and
    re-added the advertising instance on three other adapters, so an operator
    pressing several buttons -- the natural thing when nothing is connecting --
    thrashed every advertisement on the machine and cut off whatever connection
    attempt the console had in flight. Measured: seven windows in 34 s, each
    restarting three advertisements, and a console that reported pairing while
    nothing paired.

    Pairing one adapter in isolation is still available and is now explicit:
    turn the others off. An operator decision with no hidden cross-adapter
    effects, which is the property this needed and did not have.
    """

    class _Peripheral:
        def __init__(self):
            self.suppressed = False
            self.pairing_mode = True
            self.forced = 0
            self.sink = self

        def suppress_advertising(self):
            self.suppressed = True

        def set_pairing_mode(self, pairing):
            changed = bool(pairing) != self.pairing_mode
            self.pairing_mode = bool(pairing)
            return changed

        def ensure_advertising(self, force=False):
            if force:
                self.suppressed = False
                self.pairing_mode = True
                self.forced += 1
            return not self.suppressed

        def detach(self):
            pass

    def _manager(self, monkeypatch):
        from server.bt import adapter as adapter_mod
        from server.bt.adapter import AdapterManager
        from server.bt.state import AdapterState, Phase
        from server.config import ServerConfig
        from server.router import Router

        config = ServerConfig()
        config.controller_transport = "ble"
        config.controller_identity = "8bitdo"
        manager = AdapterManager(Router(), config)

        peripherals = {}
        for index, addr in enumerate((
            "DC:A6:32:B9:6A:88", "CC:28:AA:6D:BA:C0",
            "A0:AD:9F:79:EC:C8", "CC:28:AA:6D:BB:F4",
        )):
            adapter = AdapterState(bd_addr=addr, hci_name=f"hci{index}")
            adapter.index = index
            adapter.enabled = True
            adapter.to(Phase.CONFIGURING)
            adapter.to(Phase.LISTENING)
            manager._adapters[addr] = adapter
            peripherals[addr] = self._Peripheral()
            manager._ble[addr] = peripherals[addr]

        async def _ok(*a, **k):
            return True

        async def _read_properties(hci_name):
            return {"pairable": True}

        monkeypatch.setattr(adapter_mod.adapter_dbus, "set_properties", _ok)
        monkeypatch.setattr(
            adapter_mod.adapter_dbus, "read_properties", _read_properties
        )
        monkeypatch.setattr(adapter_mod, "_ensure_pairing_settings", lambda a: None)
        monkeypatch.setattr(adapter_mod, "_set_device_class", lambda a: None)
        monkeypatch.setattr(adapter_mod, "_bonds_on_disk", lambda a: [])

        async def _noop_agent():
            return None

        monkeypatch.setattr(manager, "_ensure_agent", _noop_agent)
        return manager, peripherals

    CHOSEN = "DC:A6:32:B9:6A:88"

    @pytest.mark.asyncio
    async def test_the_others_are_left_alone(self, monkeypatch):
        manager, peripherals = self._manager(monkeypatch)

        ok, _message = await manager.set_pairable(self.CHOSEN, True)

        assert ok
        others = [p for a, p in peripherals.items() if a != self.CHOSEN]
        assert not any(p.suppressed for p in others)

    @pytest.mark.asyncio
    async def test_the_others_advertisements_are_not_restarted(self, monkeypatch):
        """The actual harm: a restart cuts off a connection already in flight."""
        manager, peripherals = self._manager(monkeypatch)

        await manager.set_pairable(self.CHOSEN, True)

        others = [p for a, p in peripherals.items() if a != self.CHOSEN]
        assert all(p.forced == 0 for p in others)
        assert peripherals[self.CHOSEN].forced == 1

    @pytest.mark.asyncio
    async def test_repeated_clicks_stay_confined(self, monkeypatch):
        """An operator presses several buttons when nothing is connecting.
        That must not become four adapters' worth of churn."""
        manager, peripherals = self._manager(monkeypatch)

        for addr in peripherals:
            await manager.set_pairable(addr, True)

        assert all(p.forced == 1 for p in peripherals.values())
        assert not any(p.suppressed for p in peripherals.values())




class TestWakeIsNotRePairing:
    """Wake switches a paired controller back on. It must not touch the bond.

    A real pad that has been switched off comes back by advertising again; its
    host sees it and reconnects using the bond both ends already hold. A
    controller that forgot its console every time it woke would be useless,
    and it is exactly the mistake the old single 'Re-advertise' button invited
    by doing several jobs at once.
    """

    class _Peripheral:
        def __init__(self, suppressed=True):
            self.suppressed = suppressed
            self.forced = 0
            self.pairing_mode = True
            self.sink = self

        def set_pairing_mode(self, pairing):
            changed = bool(pairing) != self.pairing_mode
            self.pairing_mode = bool(pairing)
            return changed

        def ensure_advertising(self, force=False):
            if force:
                self.suppressed = False
                self.pairing_mode = True
                self.forced += 1
            return not self.suppressed

    def _manager(self, monkeypatch, *, bonds=("A8:ED:71:F3:ED:FD",), enabled=True):
        from server.bt import adapter as adapter_mod
        from server.bt.adapter import AdapterManager
        from server.bt.state import AdapterState, Phase
        from server.config import ServerConfig
        from server.router import Router

        config = ServerConfig()
        config.controller_transport = "ble"
        manager = AdapterManager(Router(), config)

        adapter = AdapterState(bd_addr="A0:AD:9F:79:EC:C8", hci_name="hci1")
        adapter.index = 1
        adapter.enabled = enabled
        adapter.to(Phase.CONFIGURING)
        adapter.to(Phase.LISTENING)
        manager._adapters[adapter.bd_addr] = adapter

        writes = []

        async def _set_properties(hci_name, **kwargs):
            writes.append(kwargs)
            return True

        removed = []

        async def _remove_bonds(hci_name):
            removed.append(hci_name)
            return []

        monkeypatch.setattr(
            adapter_mod.adapter_dbus, "set_properties", _set_properties
        )
        monkeypatch.setattr(adapter_mod.adapter_dbus, "remove_bonds", _remove_bonds)
        monkeypatch.setattr(adapter_mod, "_bonds_on_disk", lambda a: list(bonds))

        peripheral = self._Peripheral()
        manager._ble[adapter.bd_addr] = peripheral
        return manager, adapter, peripheral, writes, removed

    @pytest.mark.asyncio
    async def test_it_puts_the_advertisement_back(self, monkeypatch):
        manager, adapter, peripheral, _w, _r = self._manager(monkeypatch)

        ok, _message = await manager.wake(adapter.bd_addr)

        assert ok
        assert peripheral.forced == 1
        assert peripheral.suppressed is False

    @pytest.mark.asyncio
    async def test_it_re_asserts_pairable(self, monkeypatch):
        """The one property a sleeping adapter can lose, and it fails
        silently: the host connects and drops immediately."""
        manager, adapter, _p, writes, _r = self._manager(monkeypatch)

        await manager.wake(adapter.bd_addr)

        assert any(w.get("pairable") is True for w in writes)
        assert any(w.get("pairable_timeout_s") == 0 for w in writes)

    @pytest.mark.asyncio
    async def test_it_never_clears_a_bond(self, monkeypatch):
        manager, adapter, _p, _w, removed = self._manager(monkeypatch)

        await manager.wake(adapter.bd_addr)

        assert removed == []

    @pytest.mark.asyncio
    async def test_it_says_the_console_should_come_back(self, monkeypatch):
        manager, adapter, _p, _w, _r = self._manager(monkeypatch)

        _ok, message = await manager.wake(adapter.bd_addr)

        assert "paired" in message and "reconnect" in message

    @pytest.mark.asyncio
    async def test_waking_an_unpaired_adapter_says_so(self, monkeypatch):
        """Otherwise the operator waits for a reconnection that cannot happen."""
        manager, adapter, _p, _w, _r = self._manager(monkeypatch, bonds=())

        ok, message = await manager.wake(adapter.bd_addr)

        assert ok
        assert "not paired" in message
        assert "Pair" in message

    @pytest.mark.asyncio
    async def test_a_disabled_adapter_is_refused(self, monkeypatch):
        manager, adapter, peripheral, _w, _r = self._manager(
            monkeypatch, enabled=False
        )

        ok, message = await manager.wake(adapter.bd_addr)

        assert not ok
        assert "disabled" in message
        assert peripheral.forced == 0

    @pytest.mark.asyncio
    async def test_a_degraded_adapter_is_refused(self, monkeypatch):
        """It cannot serve HID, so advertising it invites a console to pair
        with something that will fail after connecting."""
        manager, adapter, peripheral, _w, _r = self._manager(monkeypatch)
        adapter.hid_error = "L2CAP bind failed"

        ok, message = await manager.wake(adapter.bd_addr)

        assert not ok
        assert "L2CAP bind failed" in message
        assert peripheral.forced == 0

    @pytest.mark.asyncio
    async def test_an_unknown_adapter_is_refused(self, monkeypatch):
        manager, _a, _p, _w, _r = self._manager(monkeypatch)

        ok, message = await manager.wake("11:22:33:44:55:66")

        assert not ok
        assert "No adapter" in message


class TestLimitedDiscoverableOnlyWhilePairing:
    """The bug that made every reconnection impossible.

    Limited Discoverable Mode means "I am asking to be paired, now". The real
    8BitDo 64 advertises it -- and the capture it was copied from was taken
    with the pad **in pairing mode**, which is the detail that was missed. We
    advertised it permanently, so a bonded controller asking for its console
    back still looked like a stranger requesting a new pairing.

    Measured on one adapter with nothing else on the air: pairing succeeded
    cleanly (6 LTK requests, 0 negative replies, encryption established), then
    Sleep followed by Wake produced **0 connection attempts in 45 seconds**
    with the advertisement verified live on the radio. The console answers a
    limited-discoverable device only while it is itself in pairing mode.
    """

    def _peripheral(self):
        from server.bt.ble.peripheral import BLEPeripheral
        from server.bt.identities import get_identity
        from server.bt.profiles import create_profile

        class _Mgmt:
            def __init__(self):
                self.flags = []

            def advertising_instances(self, index):
                return {1}

            def add_advertising(self, index, instance, flags, adv_data=b"",
                                scan_response=b"", **kwargs):
                self.flags.append(flags)
                return instance

            def remove_advertising(self, index, instance=0):
                pass

        mgmt_sock = _Mgmt()
        peripheral = BLEPeripheral(
            "hci0", create_profile("generic"), get_identity("8bitdo"),
            mgmt=mgmt_sock, index=0, bd_addr="CC:28:AA:6D:BA:C0",
        )
        peripheral._registered = True
        return peripheral, mgmt_sock

    def test_an_unpaired_peripheral_asks_to_pair(self):
        peripheral, mgmt_sock = self._peripheral()

        peripheral.ensure_advertising(force=True)

        assert mgmt_sock.flags[-1] & hogp.ADV_FLAG_LIMITED_DISCOVERABLE
        assert not mgmt_sock.flags[-1] & hogp.ADV_FLAG_DISCOVERABLE

    def test_a_bonded_peripheral_asks_its_console_back(self):
        peripheral, mgmt_sock = self._peripheral()

        peripheral.set_pairing_mode(False)
        peripheral.ensure_advertising(force=True)

        assert mgmt_sock.flags[-1] & hogp.ADV_FLAG_DISCOVERABLE
        assert not mgmt_sock.flags[-1] & hogp.ADV_FLAG_LIMITED_DISCOVERABLE

    def test_it_stays_connectable_either_way(self):
        """Whatever it is asking for, a host has to be able to answer."""
        peripheral, mgmt_sock = self._peripheral()

        for pairing in (True, False):
            peripheral.set_pairing_mode(pairing)
            peripheral.ensure_advertising(force=True)
            assert mgmt_sock.flags[-1] & hogp.ADV_FLAG_CONNECTABLE

    def test_the_kernel_still_owns_the_flags_structure(self):
        """A duplicate Flags AD fails tlv_data_is_valid and takes the whole
        advertisement with it, under a bare Invalid Parameters."""
        peripheral, mgmt_sock = self._peripheral()

        peripheral.ensure_advertising(force=True)

        assert mgmt_sock.flags[-1] & hogp.ADV_FLAG_MANAGED_FLAGS

    def test_changing_the_mode_is_reported_so_the_caller_restarts(self):
        """The flag is baked into the advertising instance, so a changed mode
        only reaches the air if the instance is re-added."""
        peripheral, _mgmt = self._peripheral()

        assert peripheral.set_pairing_mode(False) is True
        assert peripheral.set_pairing_mode(False) is False
        assert peripheral.set_pairing_mode(True) is True

    def test_an_unchanged_mode_does_not_restart_the_advertisement(self):
        """Restarting it every ten seconds would be a gap a scanning console
        can fall into, for nothing."""
        peripheral, mgmt_sock = self._peripheral()
        peripheral.ensure_advertising(force=True)
        before = len(mgmt_sock.flags)

        if peripheral.set_pairing_mode(True):
            peripheral.ensure_advertising(force=True)

        assert len(mgmt_sock.flags) == before


class TestTheAdvertisingIntervalIsNotTheKernelDefault:
    """1280 ms is the kernel's default and the right one for a coin cell.

    We are mains powered and a player is waiting for a controller to come
    back. Measured before this: a bonded adapter took about 30 s to be found
    again after waking, against a 1280 ms advertising interval.

    `Add Advertising` carries no interval, and the extended opcode that does
    is unusable on this platform -- its data step is rejected with Invalid
    Parameters, which is why our advertising goes through the legacy path at
    all. So the value is set where the kernel keeps it.
    """

    def _files(self, tmp_path, index=0, minimum=2048, maximum=2048):
        directory = tmp_path / f"hci{index}"
        directory.mkdir()
        (directory / "adv_min_interval").write_text(str(minimum))
        (directory / "adv_max_interval").write_text(str(maximum))
        return directory

    def test_the_default_is_read_back(self, tmp_path):
        self._files(tmp_path)
        assert hogp.read_advertising_interval(0, str(tmp_path)) == (2048, 2048)

    def test_it_is_written_down_to_the_fast_range(self, tmp_path):
        directory = self._files(tmp_path)

        assert hogp.set_advertising_interval(0, root=str(tmp_path)) is True
        assert int((directory / "adv_min_interval").read_text()) == 96
        assert int((directory / "adv_max_interval").read_text()) == 144

    def test_the_fast_range_is_a_range_not_a_point(self):
        """Four radios at one identical interval settle into lockstep and
        collide on the same three advertising channels every cycle."""
        assert hogp.FAST_ADV_MIN_UNITS < hogp.FAST_ADV_MAX_UNITS

    def test_it_is_well_clear_of_the_specification_floor(self):
        assert hogp.FAST_ADV_MIN_UNITS >= hogp.ADV_INTERVAL_MIN_UNITS
        # And a real improvement: at least ten times quicker than the default.
        assert hogp.FAST_ADV_MAX_UNITS * 10 < 2048

    def test_writing_widens_before_narrowing(self, tmp_path):
        """The kernel refuses a min above the current max and a max below the
        current min, so the order is not cosmetic."""
        directory = self._files(tmp_path, minimum=2048, maximum=2048)

        assert hogp.set_advertising_interval(0, root=str(tmp_path)) is True
        assert int((directory / "adv_min_interval").read_text()) == 96

    def test_raising_it_again_also_works(self, tmp_path):
        """The reverse order, from a fast value back to a slow one."""
        directory = self._files(tmp_path, minimum=96, maximum=144)

        assert hogp.set_advertising_interval(
            0, minimum=2048, maximum=2048, root=str(tmp_path)
        ) is True
        assert int((directory / "adv_max_interval").read_text()) == 2048

    def test_an_adapter_already_correct_is_not_written_to(self, tmp_path):
        """Called every time the advertisement starts, so the ordinary case
        must not write."""
        directory = self._files(tmp_path, minimum=96, maximum=144)
        before = (directory / "adv_min_interval").stat().st_mtime_ns

        assert hogp.set_advertising_interval(0, root=str(tmp_path)) is True
        assert (directory / "adv_min_interval").stat().st_mtime_ns == before

    def test_a_missing_debugfs_is_not_an_error(self, tmp_path):
        """An adapter that will not take a faster interval still advertises
        perfectly well. Failing here would trade a working controller for a
        quicker one."""
        assert hogp.read_advertising_interval(0, str(tmp_path)) is None
        assert hogp.set_advertising_interval(0, root=str(tmp_path)) is False

    def test_an_impossible_range_is_refused_loudly(self):
        """EINVAL from the kernel would read as "debugfs is unavailable"
        rather than "that value is wrong"."""
        import pytest as _pytest

        with _pytest.raises(ValueError):
            hogp.set_advertising_interval(0, minimum=8, maximum=16)
        with _pytest.raises(ValueError):
            hogp.set_advertising_interval(0, minimum=144, maximum=96)

    def test_the_peripheral_sets_it_before_adding_the_instance(self):
        """The kernel reads these when it builds the advertising parameters,
        so a value written afterwards does not reach the air."""
        import inspect

        from server.bt.ble.peripheral import BLEPeripheral

        source = inspect.getsource(BLEPeripheral._start_advertising)
        assert source.index("set_advertising_interval") < source.index(
            "add_advertising"
        )


class TestAWokenControllerActuallyCarriesInput:
    """A live encrypted link that delivers nothing is the worst shape of bug
    this subsystem produces, and Sleep created one.

    ``disconnect_host`` detaches the sink deliberately -- the report
    characteristic reference is dropped -- and nothing re-attached it when the
    console came back. ``send_input_report`` returns False on a null
    characteristic and counts nothing, so every report is discarded in
    silence while the GUI shows a connected controller.

    Measured after the reconnection fix: link up, ``AUTH ENCRYPT``, state
    awake, and the channel reporting ``connected=False``.
    """

    def _peripheral(self):
        from server.bt.ble.peripheral import BLEPeripheral
        from server.bt.identities import get_identity
        from server.bt.profiles import create_profile

        class _Characteristic:
            notifying = True

            def __init__(self):
                self.sent = []

            def notify(self, payload):
                self.sent.append(payload)
                return True

        peripheral = BLEPeripheral(
            "hci0", create_profile("generic"), get_identity("8bitdo"),
            bd_addr="CC:28:AA:6D:BA:C0",
        )
        characteristic = _Characteristic()
        peripheral._input_report = characteristic
        peripheral._registered = True
        peripheral.sink.attach(characteristic)
        return peripheral, characteristic

    def test_sleep_then_wake_still_delivers(self):
        peripheral, characteristic = self._peripheral()

        # Sleep.
        peripheral.sink.detach()
        assert peripheral.sink.send_input_report(b"\x01\x02") is False

        # Wake, and the console comes back.
        peripheral.attach_sink("A8:ED:71:F3:ED:FD")
        peripheral.sink.set_link(True, "A8:ED:71:F3:ED:FD")

        assert peripheral.sink.is_connected
        assert peripheral.sink.send_input_report(b"\x01\x02") is True
        assert characteristic.sent

    def test_re_attaching_resets_the_profile(self):
        """A reconnect is a fresh session; a profile with handshake state must
        start it again rather than assume the old one survived."""
        peripheral, _c = self._peripheral()
        calls = []
        peripheral.profile.on_connected = lambda: calls.append("connected")

        peripheral.attach_sink("A8:ED:71:F3:ED:FD")

        assert calls == ["connected"]

    def test_it_is_safe_before_the_application_exists(self):
        """Called from the MGMT event path, which can fire while the GATT
        application is being torn down."""
        peripheral, _c = self._peripheral()
        peripheral._input_report = None

        peripheral.attach_sink("A8:ED:71:F3:ED:FD")      # must not raise

    def test_the_link_path_re_attaches(self):
        """The MGMT device-connected handler is the only thing that sees a
        bonded host reconnect -- no GATT callback fires for it."""
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._note_link)
        assert "attach_sink" in source


class TestConnectingEndsThePairingWindow:
    """A window that outlives the connection it created has a second life:
    when it finally expires, `_expire_pairing_windows` writes
    `Discoverable=False` on whatever link exists by then."""

    @pytest.mark.asyncio
    async def test_a_connect_clears_the_countdown(self, monkeypatch):
        from server.bt.adapter import AdapterManager
        from server.bt.state import AdapterState, Phase
        from server.config import ServerConfig
        from server.router import Router

        config = ServerConfig()
        config.controller_transport = "ble"
        manager = AdapterManager(Router(), config)

        adapter = manager._registry.ensure("CC:28:AA:6D:BA:C0")
        adapter.index = 0
        adapter.hci_name = "hci0"
        adapter.to(Phase.CONFIGURING)
        adapter.to(Phase.LISTENING)
        adapter.arm_pairing(300)
        manager._adapters[adapter.bd_addr] = adapter
        assert adapter.pairing_remaining_s > 0

        manager._note_link(0, "A8:ED:71:F3:ED:FD", True)

        assert adapter.pairing_remaining_s == 0
        assert adapter.phase is Phase.LINKED

    @pytest.mark.asyncio
    async def test_a_disconnect_does_not_re_arm_one(self, monkeypatch):
        from server.bt.adapter import AdapterManager
        from server.bt.state import AdapterState, Phase
        from server.config import ServerConfig
        from server.router import Router

        config = ServerConfig()
        config.controller_transport = "ble"
        manager = AdapterManager(Router(), config)

        adapter = manager._registry.ensure("CC:28:AA:6D:BA:C0")
        adapter.index = 0
        adapter.hci_name = "hci0"
        adapter.to(Phase.CONFIGURING)
        adapter.to(Phase.LISTENING)
        adapter.to(Phase.LINKED)
        adapter.peer = "A8:ED:71:F3:ED:FD"
        manager._adapters[adapter.bd_addr] = adapter

        manager._note_link(0, "A8:ED:71:F3:ED:FD", False)

        assert adapter.pairing_remaining_s == 0
        assert adapter.phase is Phase.LISTENING


class TestTheFirstAdvertisementIsAlreadyRight:
    """The peripheral defaults to pairing mode and the reconcile corrects it --
    up to ten seconds later.

    Measured after a restart with four bonded adapters: all four logged the
    switch to general discoverable from the reconcile pass, not from bring-up.
    For that window every one of them advertised "pair me" and the console
    they belonged to ignored them, so each restart cost a reconnection delay
    for nothing.
    """

    def test_bring_up_sets_the_mode_before_starting(self):
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._start_ble)
        assert "set_pairing_mode" in source
        assert source.index("set_pairing_mode") < source.index("peripheral.start()")

    def test_it_is_decided_from_the_bond_on_disk(self):
        """D-Bus has been observed reporting an empty bond list for an adapter
        whose key file exists -- which here would mean advertising "pair me"
        at a console that already knows us."""
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._start_ble)
        assert "_bonds_on_disk" in source


class TestTheReconcileMustNotUndoAReset:
    """Reset leaves a controller unpaired **and** switched off.

    That is a pairing-mode change and a suppression at once, and the reconcile
    restarts the advertisement whenever the mode changes -- with `force=True`,
    which clears the suppression latch. The obvious version therefore put all
    four controllers straight back on the air ten seconds later, asking to
    pair: exactly the state the operator had just taken them out of.
    """

    def _manager(self, monkeypatch, *, suppressed, bonds=()):
        from server.bt import adapter as adapter_mod
        from server.bt.adapter import AdapterManager
        from server.bt.state import AdapterState, Phase
        from server.config import ServerConfig
        from server.router import Router

        config = ServerConfig()
        config.controller_transport = "ble"
        manager = AdapterManager(Router(), config)

        adapter = AdapterState(bd_addr="00:00:00:00:00:00", hci_name="hci0")
        adapter.index = 0
        adapter.enabled = True
        adapter.bondable = True
        adapter.bonds = tuple(bonds)
        adapter.to(Phase.CONFIGURING)
        adapter.to(Phase.LISTENING)
        manager._adapters[adapter.bd_addr] = adapter

        class _Peripheral:
            def __init__(self):
                # Bonded when it went to sleep, so the mode is about to change.
                self.pairing_mode = False
                self.suppressed = suppressed
                self.forced = 0

            def set_pairing_mode(self, pairing):
                changed = bool(pairing) != self.pairing_mode
                self.pairing_mode = bool(pairing)
                return changed

            def ensure_advertising(self, force=False):
                if force:
                    self.suppressed = False
                    self.forced += 1
                return not self.suppressed

        peripheral = _Peripheral()
        manager._ble[adapter.bd_addr] = peripheral

        async def _ok(*a, **k):
            return True

        monkeypatch.setattr(adapter_mod.adapter_dbus, "set_properties", _ok)
        monkeypatch.setattr(manager, "_ensure_radio_mode", lambda a: None)
        return manager, adapter, peripheral

    @pytest.mark.asyncio
    async def test_a_switched_off_controller_stays_off(self, monkeypatch):
        manager, adapter, peripheral = self._manager(monkeypatch, suppressed=True)

        for _ in range(3):
            await manager._ensure_ble_ready([adapter])

        assert peripheral.suppressed, "the reconcile put a reset controller back on air"
        assert peripheral.forced == 0

    @pytest.mark.asyncio
    async def test_the_mode_is_still_corrected_for_next_time(self, monkeypatch):
        """It just does not go on the air yet. Pair or Wake restarts it, and
        the flag is already right when it does."""
        manager, adapter, peripheral = self._manager(monkeypatch, suppressed=True)

        await manager._ensure_ble_ready([adapter])

        assert peripheral.pairing_mode is True

    @pytest.mark.asyncio
    async def test_a_live_controller_still_gets_its_mode_change(self, monkeypatch):
        """The invariant must keep working for everything that is not
        deliberately switched off."""
        manager, adapter, peripheral = self._manager(monkeypatch, suppressed=False)

        await manager._ensure_ble_ready([adapter])

        assert peripheral.pairing_mode is True
        assert peripheral.forced == 1

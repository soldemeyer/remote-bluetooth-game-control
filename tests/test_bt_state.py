"""The adapter state machine.

The bug this replaces: ``rescan()`` rebuilt every ``AdapterInfo`` from scratch
on a 10 s timer, so anything transient held on the old object was silently
lost. That is why the pairing countdown read zero a few seconds after the
operator armed it, and why a degraded adapter looked healthy again between
rescans. The patch at the time hand-copied two fields across the rebuild, which
works exactly until someone adds a third field and forgets.

So the property under test is not "the fields are right" but "**the object
survives**".
"""

from __future__ import annotations

from server.bt import mgmt
from server.bt.state import AdapterRegistry, AdapterState, Phase


def _settings(index=3, addr="CC:28:AA:6D:BB:F4", current=0x00020AC3, cod=0x002508):
    return mgmt.AdapterSettings(
        index=index, bd_addr=addr, manufacturer=0x000F,
        supported=0x0003FFFF, current=current, device_class=cod,
        name="controller-server", short_name="",
    )


class TestStateSurvivesRescan:
    def test_the_same_object_is_returned_every_time(self):
        registry = AdapterRegistry()
        first = registry.ensure("AA:BB:CC:DD:EE:FF")
        assert registry.ensure("aa:bb:cc:dd:ee:ff") is first

    def test_transient_state_survives_a_sync(self):
        """The whole point. Arm a window, resync, find it still armed."""
        registry = AdapterRegistry()
        state = registry.ensure("CC:28:AA:6D:BB:F4")
        state.arm_pairing(120)
        state.hid_error = "PSM 17 in use"
        state.number = 2

        registry.sync({"CC:28:AA:6D:BB:F4": _settings()})

        after = registry.get("CC:28:AA:6D:BB:F4")
        assert after is state
        assert after.pairing_remaining_s > 0
        assert after.hid_error == "PSM 17 in use"
        assert after.number == 2

    def test_a_field_added_later_cannot_be_dropped(self):
        """There is no reconstruction, so nothing needs copying across one."""
        registry = AdapterRegistry()
        state = registry.ensure("CC:28:AA:6D:BB:F4")
        state.peer = "11:22:33:44:55:66"
        state.bonds = ("11:22:33:44:55:66",)
        state.link_report = object()

        for _ in range(5):
            registry.sync({"CC:28:AA:6D:BB:F4": _settings()})

        assert state.peer == "11:22:33:44:55:66"
        assert state.bonds == ("11:22:33:44:55:66",)
        assert state.link_report is not None

    def test_sync_reports_what_moved(self):
        registry = AdapterRegistry()
        added, removed, changed = registry.sync({"CC:28:AA:6D:BB:F4": _settings()})
        assert added == ["CC:28:AA:6D:BB:F4"] and removed == [] and changed

        # A repeat with identical settings must be quiet: the status feed runs
        # at 10 Hz and pushing on every tick is what makes a GUI unusable.
        added, removed, changed = registry.sync({"CC:28:AA:6D:BB:F4": _settings()})
        assert added == [] and removed == [] and not changed

    def test_a_real_change_is_reported(self):
        registry = AdapterRegistry()
        registry.sync({"CC:28:AA:6D:BB:F4": _settings(current=0x00020AC1)})
        _, _, changed = registry.sync(
            {"CC:28:AA:6D:BB:F4": _settings(current=0x00020AC3)}
        )
        assert changed

    def test_an_unplugged_adapter_is_removed(self):
        registry = AdapterRegistry()
        registry.sync({"CC:28:AA:6D:BB:F4": _settings()})
        added, removed, _ = registry.sync({})
        assert removed == ["CC:28:AA:6D:BB:F4"]
        assert registry.get("CC:28:AA:6D:BB:F4") is None

    def test_a_replugged_adapter_starts_over(self):
        # Correct: its HID stack is gone and its phase genuinely restarts.
        registry = AdapterRegistry()
        state = registry.ensure("CC:28:AA:6D:BB:F4")
        state.to(Phase.CONFIGURING)
        state.to(Phase.LISTENING)
        registry.sync({})
        registry.sync({"CC:28:AA:6D:BB:F4": _settings()})
        assert registry.get("CC:28:AA:6D:BB:F4").phase is Phase.DETECTED


class TestPairingWindow:
    def test_arming_moves_to_pairing_and_starts_a_countdown(self):
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF")
        state.to(Phase.CONFIGURING)
        state.to(Phase.LISTENING)
        state.arm_pairing(120)
        assert state.phase is Phase.PAIRING
        assert 118 <= state.pairing_remaining_s <= 120

    def test_an_expired_window_is_detectable(self):
        """BlueZ will not tell you. Somebody has to notice.

        Its own DiscoverableTimeout stops reporting the adapter as discoverable
        but never writes scan enable back down, so the radio goes on answering
        inquiries and the gamepad keeps appearing in a host's "Add a device"
        list long after the window closed.
        """
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF")
        state.to(Phase.CONFIGURING)
        state.to(Phase.LISTENING)
        state.arm_pairing(120)
        assert not state.pairing_expired

        state.pairing_until_ns = 1
        assert state.pairing_expired
        assert state.pairing_remaining_s == 0

    def test_a_window_that_was_never_armed_has_not_expired(self):
        # 0 means "no window", and reading that as "expired" would make the
        # reconcile pass tear down a window that never existed, on every tick.
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF")
        assert not state.pairing_expired

    def test_clearing_returns_to_listening(self):
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF")
        state.to(Phase.CONFIGURING)
        state.to(Phase.LISTENING)
        state.arm_pairing(120)
        state.clear_pairing()
        assert state.phase is Phase.LISTENING
        assert state.pairing_remaining_s == 0

    def test_clearing_does_not_disturb_a_linked_adapter(self):
        # A console connecting ends the window, and it must land in LINKED
        # rather than being dragged back to LISTENING.
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF")
        state.to(Phase.CONFIGURING)
        state.to(Phase.LISTENING)
        state.arm_pairing(120)
        state.to(Phase.LINKED, reason="console connected")
        state.clear_pairing()
        assert state.phase is Phase.LINKED


class TestHealthNamesTheActualFault:
    """Every one of these presents to the operator as "it will not pair"."""

    def _adapter(self, **kwargs):
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF", enabled=True)
        state.settings_known = True
        state.powered = True
        state.connectable = True
        state.ssp = True
        state.device_class = 0x002508
        state.configured = True
        for key, value in kwargs.items():
            setattr(state, key, value)
        return state

    def test_a_healthy_adapter_reports_nothing(self):
        assert self._adapter().health() == []

    def test_page_scan_off_is_called_out(self):
        problems = self._adapter(connectable=False).health()
        assert any("Page scan is off" in p for p in problems)
        # The trap is that it cannot open itself: unreachable means it can never
        # gain the bond that would have made BlueZ keep it connectable.
        assert any("cannot open itself" in p for p in problems)

    def test_link_security_is_called_out(self):
        problems = self._adapter(link_security=True).health()
        assert any("legacy PIN" in p for p in problems)

    def test_ssp_off_is_called_out(self):
        assert any("Secure Simple Pairing is off" in p
                   for p in self._adapter(ssp=False).health())

    def test_the_wrong_class_is_called_out(self):
        problems = self._adapter(device_class=0x000000).health()
        assert any("never offer this as a controller" in p for p in problems)

    def test_the_service_class_bits_are_not_confused_with_the_device_class(self):
        """0x000508 and 0x002508 are the same controller.

        They differ only in the Limited Discoverable service bit, which
        bluetoothd toggles by itself. Measured on the reference Pi: two
        adapters read 0x002508 and two read 0x000508 at the same instant, all
        four peripheral/gamepad and all four fine. Flagging that difference
        would send someone chasing a class-of-device problem that is not there.
        """
        assert self._adapter(device_class=0x000508).health() == []

    def test_a_hid_failure_is_reported_verbatim(self):
        problems = self._adapter(hid_error="PSM 17/19 already in use").health()
        assert any("PSM 17/19 already in use" in p for p in problems)

    def test_an_adapter_we_never_configured_is_not_judged_on_its_class(self):
        # One the operator never enabled is left completely untouched, so its
        # class is none of our business.
        state = self._adapter(device_class=0x000000)
        state.configured = False
        assert not any("controller" in p for p in state.health())


class TestPhaseTransitions:
    def test_an_unexpected_transition_is_logged_but_taken(self, caplog):
        """Refusing would turn a bookkeeping problem into a dead adapter.

        Hardware does surprising things and two code paths racing over one
        radio is the usual cause, so it must be visible -- but the adapter
        still has to end up somewhere.
        """
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF")
        with caplog.at_level("WARNING"):
            assert state.to(Phase.LINKED) is True
        assert state.phase is Phase.LINKED
        assert "not an expected transition" in caplog.text

    def test_an_expected_transition_is_quiet(self, caplog):
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF")
        with caplog.at_level("WARNING"):
            state.to(Phase.CONFIGURING)
            state.to(Phase.LISTENING)
        assert caplog.text == ""

    def test_moving_nowhere_reports_no_change(self):
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF")
        assert state.to(Phase.DETECTED) is False

    def test_degraded_is_reachable_from_every_working_phase(self):
        # A bind can fail at any point -- a dongle pulled mid-session, or
        # bluetoothd's input plugin reclaiming a PSM after a daemon restart.
        from server.bt.state import _EXPECTED

        for phase in (Phase.CONFIGURING, Phase.LISTENING, Phase.PAIRING, Phase.LINKED):
            assert Phase.DEGRADED in _EXPECTED[phase]

    def test_quiet_is_reachable_from_everywhere(self):
        # The operator can disable an adapter whatever it is doing, and an
        # adapter armed for pairing and then disabled must have a way back --
        # otherwise the only path that clears Discoverable is locked out and it
        # advertises until something else resets it.
        from server.bt.state import _EXPECTED

        for phase, allowed in _EXPECTED.items():
            if phase is Phase.QUIET:
                continue
            assert Phase.QUIET in allowed, phase


class TestLiveness:
    def test_only_a_linked_adapter_is_live(self):
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF")
        state.to(Phase.CONFIGURING)
        state.to(Phase.LISTENING)
        assert not state.is_live
        state.to(Phase.LINKED)
        assert state.is_live

    def test_usable_is_broader_than_live(self):
        # An adapter waiting for a console is usable but not live, and the
        # difference is what the capacity figure is built on.
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF")
        state.to(Phase.CONFIGURING)
        state.to(Phase.LISTENING)
        assert state.is_usable and not state.is_live

    def test_a_degraded_adapter_is_not_usable(self):
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF")
        state.to(Phase.CONFIGURING)
        state.hid_error = "EADDRINUSE"
        state.to(Phase.DEGRADED)
        assert not state.is_usable

    def test_a_disabled_adapter_is_not_usable(self):
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF", enabled=False)
        state.to(Phase.CONFIGURING)
        state.to(Phase.LISTENING)
        assert not state.is_usable


class TestSnapshotKeepsTheGUIContract:
    def test_the_existing_keys_are_all_present(self):
        # The web GUI renders these by name; dropping one silently blanks a
        # field rather than raising.
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF", hci_name="hci3")
        snap = state.snapshot()
        for key in ("bd_addr", "hci", "manufacturer", "up", "enabled",
                    "number", "name", "display_name", "pairing_s", "hid_error"):
            assert key in snap

    def test_display_name_falls_back_rather_than_going_blank(self):
        """It titles the card, so an empty string leaves an unnamed adapter.

        `name` and `display_name` answer different questions -- what a console
        sees versus what the operator calls it -- and only the second is
        unique. Neither is always populated, so the fallback chain matters.
        """
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF", hci_name="hci3")
        assert state.snapshot()["display_name"] == "hci3"

        state.name = "8BitDo 64 BT"
        assert state.snapshot()["display_name"] == "8BitDo 64 BT"

        state.display_name = "Controller 2"
        assert state.snapshot()["display_name"] == "Controller 2"

    def test_the_advertised_name_is_still_reported_separately(self):
        """The GUI shows both: the operator needs the label to tell four
        adapters apart, and the advertised name to know what the console is
        matching on."""
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF", hci_name="hci3")
        state.name = "8BitDo 64 BT"
        state.display_name = "Controller 2"

        snap = state.snapshot()
        assert snap["name"] == "8BitDo 64 BT"
        assert snap["display_name"] == "Controller 2"

    def test_numbered_adapters_sort_before_unnumbered_ones(self):
        registry = AdapterRegistry()
        for addr, number, hci in (
            ("AA:AA:AA:AA:AA:01", 0, "hci9"),
            ("AA:AA:AA:AA:AA:02", 2, "hci1"),
            ("AA:AA:AA:AA:AA:03", 1, "hci7"),
        ):
            state = registry.ensure(addr)
            state.number = number
            state.hci_name = hci
        assert [row["number"] for row in registry.snapshot()] == [1, 2, 0]


class TestHealthWillNotReportWhatItCannotSee:
    """The fallback enumerator parses hciconfig, which cannot see any of this.

    Page scan, SSP and link security are all invisible to it, so those fields
    sit at their defaults -- and every one of those defaults happens to look
    exactly like a fault. Reporting them would send the operator to fix
    something that is not broken, on a machine where we never looked.
    """

    def test_an_adapter_with_unread_settings_reports_nothing(self):
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF", enabled=True)
        state.powered = True
        state.configured = True
        # connectable/ssp False, class 0 -- every check below would fire.
        assert state.settings_known is False
        assert state.health() == []

    def test_the_same_adapter_reports_once_the_settings_are_known(self):
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF", enabled=True)
        state.powered = True
        state.configured = True
        state.settings_known = True
        assert state.health() != []

    def test_a_hid_failure_is_reported_either_way(self):
        # It is ours, not the radio's, so it does not depend on having read
        # anything from MGMT.
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF", enabled=True)
        state.hid_error = "L2CAP PSM 17/19 already in use"
        assert any("PSM 17/19" in p for p in state.health())


class TestFlagSetsFollowTheAdapter:
    """A flag kept beside an adapter outlives it; one kept on it does not.

    ``configured`` and ``quieted`` were two ``set[str]`` on the manager, and
    nothing removed an address when the dongle was unplugged. Replugging it
    handed the fresh adapter the old one's flags -- so a disabled adapter that
    had already been silenced was skipped by the pass that would have silenced
    it again, and went on advertising to hosts that could never connect.
    """

    def _sets(self):
        from server.bt.state import FlagSet

        registry = AdapterRegistry()
        return registry, FlagSet(registry, "configured"), FlagSet(registry, "quieted")

    def test_it_behaves_like_a_set(self):
        registry, configured, _ = self._sets()
        assert "AA:BB:CC:DD:EE:FF" not in configured
        configured.add("AA:BB:CC:DD:EE:FF")
        assert "AA:BB:CC:DD:EE:FF" in configured
        assert configured == {"AA:BB:CC:DD:EE:FF"}
        assert len(configured) == 1
        configured.discard("AA:BB:CC:DD:EE:FF")
        assert configured == set()

    def test_the_flag_is_stored_on_the_adapter(self):
        registry, configured, _ = self._sets()
        configured.add("AA:BB:CC:DD:EE:FF")
        assert registry.get("AA:BB:CC:DD:EE:FF").configured is True

    def test_unplugging_takes_the_flags_with_it(self):
        registry, configured, quieted = self._sets()
        configured.add("AA:BB:CC:DD:EE:FF")
        quieted.add("AA:BB:CC:DD:EE:FF")

        registry.sync({})               # dongle pulled

        assert "AA:BB:CC:DD:EE:FF" not in configured
        assert "AA:BB:CC:DD:EE:FF" not in quieted

    def test_a_replugged_adapter_does_not_inherit_them(self):
        registry, configured, quieted = self._sets()
        configured.add("CC:28:AA:6D:BB:F4")
        quieted.add("CC:28:AA:6D:BB:F4")
        registry.sync({})
        registry.sync({"CC:28:AA:6D:BB:F4": _settings()})

        # It must look untouched, because it is: nothing has been written to
        # this radio since it came back.
        assert "CC:28:AA:6D:BB:F4" not in configured
        assert "CC:28:AA:6D:BB:F4" not in quieted

    def test_discarding_an_unknown_adapter_is_harmless(self):
        _, configured, _ = self._sets()
        configured.discard("00:00:00:00:00:00")

    def test_two_flags_do_not_alias_each_other(self):
        registry, configured, quieted = self._sets()
        configured.add("AA:BB:CC:DD:EE:FF")
        assert "AA:BB:CC:DD:EE:FF" not in quieted


class TestTheIndexIsNotOptional:
    """``hci_name`` is derived from ``index``, so an observation must carry one.

    Caught on hardware, not by a test: enumeration set ``hci_name`` and left
    ``index`` at its ``-1`` default, so every adapter came back as **hci-1**.
    All four then failed to power on with "Can't get device info: No such
    device" and the server reported capacity 0 -- from a change that looked
    like pure bookkeeping and passed the whole suite.
    """

    def test_a_real_index_drives_the_name(self):
        state = AdapterState(bd_addr="CC:28:AA:6D:BB:F4")
        state.apply_settings(_settings(index=3))
        assert state.hci_name == "hci3"
        assert state.index == 3

    def test_a_missing_index_does_not_invent_a_device(self):
        """The bluetoothctl fallback reports no index at all.

        Overwriting its address-shaped name with "hci-1" turns a usable
        identifier into one that matches no device on the system.
        """
        state = AdapterState(
            bd_addr="CC:28:AA:6D:BB:F4", hci_name="CC:28:AA:6D:BB:F4", index=-1
        )
        state.apply_settings(_settings(index=-1))
        assert state.hci_name == "CC:28:AA:6D:BB:F4"
        assert "hci-1" not in state.hci_name

    def test_enumeration_sets_the_index(self):
        """The regression itself: what _enumerate hands the registry."""
        from server.bt.adapter import _index_of

        assert _index_of("hci3") == 3
        assert _index_of("hci0") == 0
        assert _index_of("CC:28:AA:6D:BB:F4") == -1

    def test_index_zero_is_a_real_index(self):
        # hci0 is the built-in adapter on every Pi, and 0 is falsy -- a truth
        # test rather than a >= 0 test would skip exactly that one.
        state = AdapterState(bd_addr="DC:A6:32:B9:6A:88")
        state.apply_settings(_settings(index=0))
        assert state.hci_name == "hci0"


class TestMgmtDeviceEventsDriveTheLinkState:
    """The only signal the BLE path has that a host attached.

    HIDServer reports connect and disconnect for Classic. Nothing did for BLE,
    so an adapter carrying a live console still read `phase=listening` with no
    peer -- and the web GUI, which keys its Disconnect button on that, never
    offered a way to drop the link.
    """

    def test_a_connect_event_marks_the_adapter_linked(self):
        from server.bt.adapter import AdapterManager
        from server.config import ServerConfig
        from server.router import Router

        manager = AdapterManager(Router(), ServerConfig())
        state = manager._registry.ensure("CC:28:AA:6D:BB:F4")
        state.index = 1
        state.to(Phase.CONFIGURING)
        state.to(Phase.LISTENING)

        manager._note_link(1, "A8:ED:71:F3:ED:FD", True)

        assert state.phase is Phase.LINKED
        assert state.peer == "A8:ED:71:F3:ED:FD"

    def test_a_disconnect_event_clears_it(self):
        from server.bt.adapter import AdapterManager
        from server.config import ServerConfig
        from server.router import Router

        manager = AdapterManager(Router(), ServerConfig())
        state = manager._registry.ensure("CC:28:AA:6D:BB:F4")
        state.index = 1
        state.to(Phase.CONFIGURING)
        state.to(Phase.LISTENING)
        manager._note_link(1, "A8:ED:71:F3:ED:FD", True)

        manager._note_link(1, "A8:ED:71:F3:ED:FD", False)

        assert state.phase is Phase.LISTENING
        assert state.peer == ""

    def test_an_event_for_an_unknown_index_is_ignored(self):
        # Hot-plug races are normal; an event can arrive for an adapter we have
        # not enumerated yet.
        from server.bt.adapter import AdapterManager
        from server.config import ServerConfig
        from server.router import Router

        manager = AdapterManager(Router(), ServerConfig())
        manager._note_link(99, "A8:ED:71:F3:ED:FD", True)

    def test_the_address_parser_handles_both_event_shapes(self):
        from server.bt import mgmt

        # Device Connected: addr(6) type(1) flags(4) eir_len(2) ...
        connected = bytes.fromhex("fdedf371eda8") + b"\x01" + b"\x00" * 6
        # Device Disconnected: addr(6) type(1) reason(1)
        disconnected = bytes.fromhex("fdedf371eda8") + b"\x01\x03"

        assert mgmt.parse_device_event(connected) == "A8:ED:71:F3:ED:FD"
        assert mgmt.parse_device_event(disconnected) == "A8:ED:71:F3:ED:FD"
        assert mgmt.parse_device_event(b"\x00\x01") is None


class TestHealthDoesNotAskBrEdrQuestionsOfAnLeOnlyRadio:
    """`health()` is the operator's only explanation of "it will not connect",
    so a wrong entry is worse than none -- it sends them to fix something that
    cannot be broken.

    Every check but one is a BR/EDR question. Page scan, Secure Simple Pairing
    and the class of device have no meaning on a radio with no Classic half,
    and the SSP entry actively misleads: it tells the operator hosts will be
    prompted for a PIN, on a transport with no PIN pairing to fall back to.
    """

    def _le_only(self, **kwargs):
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF", hci_name="hci3")
        state.settings_known = True
        state.enabled = True
        state.powered = True
        state.bredr = False
        state.bondable = True
        for key, value in kwargs.items():
            setattr(state, key, value)
        return state

    def test_a_healthy_le_adapter_reports_nothing(self):
        assert self._le_only().health() == []

    def test_ssp_is_not_demanded_of_an_le_radio(self):
        """SSP is a BR/EDR concept. Firing here reports a fault that cannot
        exist, and names PIN pairing as the consequence on a transport that has
        none."""
        problems = self._le_only(ssp=False).health()

        assert not any("Secure Simple Pairing" in p for p in problems)

    def test_page_scan_is_not_demanded_of_an_le_radio(self):
        problems = self._le_only(connectable=False).health()

        assert not any("Page scan" in p for p in problems)

    def test_the_class_of_device_is_not_checked_on_an_le_radio(self):
        """A BLE host filters on the advertisement's Appearance, not on the
        Classic class of device."""
        state = self._le_only(device_class=0x000000)
        state.configured = True

        assert not any("Class of device" in p for p in state.health())

    def test_an_unbondable_le_adapter_is_reported(self):
        """The one BLE fault, and the one nothing reported. HID over GATT needs
        an encrypted link and encryption needs a bond, so the console connects
        and drops immediately -- with nothing on either side to say why."""
        problems = self._le_only(bondable=False).health()

        assert any("not bondable" in p for p in problems)

    def test_a_failed_hid_stack_is_still_reported_on_le(self):
        """The transport gate must not swallow the checks that apply to both."""
        problems = self._le_only(hid_error="L2CAP bind failed").health()

        assert any("HID stack is not running" in p for p in problems)

    def test_the_host_config_warning_still_reaches_le(self):
        """It is *about* the BLE path -- bluetoothd's GATT client -- so hiding
        it there would hide it everywhere it matters."""
        state = self._le_only(host_config_warning="input stops every ~35 seconds")

        assert "input stops every ~35 seconds" in state.health()

    def test_a_classic_adapter_is_still_fully_checked(self):
        """The gate keys on the radio, not on the configured transport: an
        adapter that *failed* to switch is still a Classic adapter, and these
        questions really do apply to it."""
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF", hci_name="hci3")
        state.settings_known = True
        state.enabled = True
        state.powered = True
        state.bredr = True
        state.connectable = False
        state.ssp = False

        problems = state.health()
        assert any("Page scan" in p for p in problems)
        assert any("Secure Simple Pairing" in p for p in problems)

    def test_bredr_defaults_to_on_so_nothing_is_silenced_by_accident(self):
        """An adapter whose settings could not be read must not have its
        Classic checks quietly disabled."""
        assert AdapterState(bd_addr="AA:BB:CC:DD:EE:FF", hci_name="hci3").bredr is True


class TestEveryGatingFieldIsActuallyRead:
    """`_enumerate` builds an observation and `sync` copies its fields onto the
    long-lived state. A field it forgets does not come through as missing -- it
    comes through as the **dataclass default**, which is a plausible value that
    nothing can distinguish from a real reading.

    Measured on hardware: `bredr` was left at its default of True on all four
    adapters, every one of which was genuinely LE-only, so the reconcile
    announced that all four had drifted back to dual mode. `_ensure_radio_mode`
    read the radio itself and correctly did nothing, so no damage was done --
    but the startup log warned about four faults that did not exist, which is
    how a warning stops being worth reading.
    """

    def _settings(self, current):
        return mgmt.AdapterSettings(
            index=0, bd_addr="CC:28:AA:6D:BA:C0", manufacturer=0x005D,
            supported=0x0003FFFF, current=current, device_class=0x002508,
            name="controller-server", short_name="",
        )

    def _observe(self, current):
        """One adapter through the real _enumerate path, MGMT mocked."""
        from server.bt.adapter import AdapterManager
        from server.config import ServerConfig
        from server.router import Router

        class _Mgmt:
            def read_all(self_inner):
                return {"CC:28:AA:6D:BA:C0": self._settings(current)}

        manager = AdapterManager(Router(), ServerConfig())
        manager._mgmt = _Mgmt()
        manager._manufacturers = {}
        return manager._enumerate()[0]

    #: powered | connectable | bondable | le  -- an LE-only adapter, as read
    #: off the reference Pi.
    LE_ONLY = 0x0000_0213
    #: the same plus br/edr and secure-conn.
    DUAL = LE_ONLY | mgmt.SETTING_BREDR | mgmt.SETTING_SECURE_CONN

    def test_an_le_only_adapter_is_observed_as_le_only(self):
        observed = self._observe(self.LE_ONLY)

        assert observed.bredr is False
        assert observed.secure_conn is False

    def test_a_dual_mode_adapter_is_observed_as_dual_mode(self):
        observed = self._observe(self.DUAL)

        assert observed.bredr is True
        assert observed.secure_conn is True

    def test_the_reading_survives_onto_the_long_lived_state(self):
        """The registry copies fields rather than replacing objects, so a field
        can be dropped on either side of that copy."""
        registry = AdapterRegistry()
        state = registry.ensure("CC:28:AA:6D:BA:C0")
        registry.sync({"CC:28:AA:6D:BA:C0": self._observe(self.LE_ONLY)})

        assert state.bredr is False
        assert state.secure_conn is False

    def test_switching_to_le_only_is_seen_as_a_change(self):
        """It gates health() and a power cycle, so it has to move the GUI."""
        registry = AdapterRegistry()
        registry.sync({"CC:28:AA:6D:BA:C0": self._observe(self.DUAL)})

        _added, _removed, changed = registry.sync(
            {"CC:28:AA:6D:BA:C0": self._observe(self.LE_ONLY)}
        )

        assert changed

    def test_sync_refuses_an_observation_missing_the_field(self):
        """A `getattr(..., default)` here is what hid the original miss. A
        field that is not there must raise, not resolve to something plausible.
        """
        import pytest

        class _Incomplete:
            index = 0
            bd_addr = "CC:28:AA:6D:BA:C0"
            manufacturer = ""
            powered = connectable = discoverable = bondable = ssp = True
            link_security = False
            device_class = 0x002508
            settings_known = True

        state = AdapterState(bd_addr="CC:28:AA:6D:BA:C0")
        with pytest.raises(AttributeError):
            state.sync(_Incomplete())


class TestPowerStateIsThePlayersView:
    """Three states, because that is what a controller has.

    Computed once, server-side, from the bond and the peer. The GUI derives
    nothing: re-deriving it there from three separate fields is how two views
    of one thing drift apart, and the buttons depend on it being right.
    """

    def _state(self, *, bonds=(), peer="", advertising=True):
        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF", hci_name="hci1")
        state.bonds = tuple(bonds)
        state.peer = peer
        state.advertising = advertising
        return state

    def test_no_bond_is_unpaired(self):
        assert self._state().power_state == "unpaired"

    def test_bonded_with_no_link_is_asleep(self):
        assert self._state(bonds=("A8:ED:71:F3:ED:FD",)).power_state == "asleep"

    def test_bonded_with_a_link_is_awake(self):
        state = self._state(bonds=("A8:ED:71:F3:ED:FD",), peer="A8:ED:71:F3:ED:FD")
        assert state.power_state == "awake"

    def test_advertising_does_not_make_it_awake(self):
        """An adapter that is bonded and advertising but has no host is still
        asleep from the player's point of view -- nothing is driving it.
        Folding the radio state in would produce a fourth case that means
        nothing to the operator and two identical buttons."""
        assert self._state(
            bonds=("A8:ED:71:F3:ED:FD",), advertising=True
        ).power_state == "asleep"
        assert self._state(
            bonds=("A8:ED:71:F3:ED:FD",), advertising=False
        ).power_state == "asleep"

    def test_a_link_without_a_bond_is_not_reported_awake(self):
        """Encryption needs a bond, so this should not happen -- and if it
        does, the honest answer is that there is nothing paired here."""
        assert self._state(peer="A8:ED:71:F3:ED:FD").power_state == "unpaired"

    def test_it_reaches_the_gui(self):
        assert "power_state" in self._state().snapshot()

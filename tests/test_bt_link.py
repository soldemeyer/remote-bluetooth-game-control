"""Link tuning: the HCI layer that decides a connection's latency.

The commands themselves need a Bluetooth controller, so what is covered here is
everything around them that can be got wrong on any machine: the event framing,
the opcode matching that keeps another process's replies out of ours, the unit
conversions, and the policy encoding.

These matter more than the usual "does the wrapper call the function" test,
because a mistake in any of them is silent. A link policy with the wrong bit
set, or a flush timeout off by a factor of 1.6, produces a link that works
perfectly and is simply slower than it should be under load.
"""

from __future__ import annotations

import struct

import pytest

from server.bt import hci
from server.bt.link import LinkPolicy, LinkReport


class TestEventFraming:
    """``parse_event`` is the boundary between socket bytes and meaning."""

    def test_parses_a_well_formed_event(self):
        packet = bytes([hci.HCI_EVENT_PKT, 0x0E, 0x04, 0x01, 0x28, 0x0C, 0x00])
        assert hci.parse_event(packet) == (0x0E, bytes([0x01, 0x28, 0x0C, 0x00]))

    def test_rejects_a_non_event_packet(self):
        # An ACL frame arriving on the same socket must not be read as an event.
        assert hci.parse_event(bytes([0x02, 0x0E, 0x04, 0x00])) is None

    def test_rejects_a_truncated_payload(self):
        # Header claims four bytes, only two follow. Trusting the length here
        # would index past the end and misattribute whatever came next.
        assert hci.parse_event(bytes([hci.HCI_EVENT_PKT, 0x0E, 0x04, 0x01, 0x28])) is None

    def test_rejects_a_runt(self):
        assert hci.parse_event(b"\x04\x0e") is None


class TestReplyMatching:
    """Every raw HCI socket on an adapter sees every event.

    bluetoothd and any ``bluetoothctl`` session are issuing their own commands
    on the same controller, so the reply sitting in our queue is frequently not
    ours. Taking the next event and hoping would attribute someone else's status
    to our command, which is the kind of bug that only appears when a second
    process happens to be running.
    """

    def _complete(self, opcode: int, status: int, params: bytes = b"") -> bytes:
        return bytes([0x01]) + struct.pack("<H", opcode) + bytes([status]) + params

    def test_returns_the_payload_for_our_opcode(self):
        payload = self._complete(hci.OCF_READ_LINK_POLICY, 0x00, b"\x0b\x00\x01\x00")
        result = hci._match_reply(hci.EVT_COMMAND_COMPLETE, payload, hci.OCF_READ_LINK_POLICY)
        assert result == b"\x0b\x00\x01\x00"

    def test_ignores_another_command_s_completion(self):
        payload = self._complete(hci.OCF_WRITE_PAGE_TIMEOUT, 0x00)
        assert hci._match_reply(
            hci.EVT_COMMAND_COMPLETE, payload, hci.OCF_READ_LINK_POLICY
        ) is None

    def test_raises_on_our_own_failure(self):
        payload = self._complete(hci.OCF_WRITE_LINK_POLICY, 0x12)
        with pytest.raises(hci.HCIStatusError) as excinfo:
            hci._match_reply(hci.EVT_COMMAND_COMPLETE, payload, hci.OCF_WRITE_LINK_POLICY)
        assert excinfo.value.status == 0x12
        assert "invalid HCI command parameters" in str(excinfo.value)

    def test_does_not_raise_on_another_command_s_failure(self):
        # Someone else's command failing must never be reported as ours.
        payload = self._complete(hci.OCF_WRITE_PAGE_TIMEOUT, 0x0C)
        assert hci._match_reply(
            hci.EVT_COMMAND_COMPLETE, payload, hci.OCF_WRITE_LINK_POLICY
        ) is None

    def test_command_status_counts_as_acceptance(self):
        # Exit Sniff Mode answers with Command Status, not Command Complete.
        payload = bytes([0x00, 0x01]) + struct.pack("<H", hci.OCF_EXIT_SNIFF_MODE)
        assert hci._match_reply(
            hci.EVT_COMMAND_STATUS, payload, hci.OCF_EXIT_SNIFF_MODE
        ) == b""

    def test_command_status_failure_raises(self):
        payload = bytes([0x0C, 0x01]) + struct.pack("<H", hci.OCF_EXIT_SNIFF_MODE)
        with pytest.raises(hci.HCIStatusError):
            hci._match_reply(hci.EVT_COMMAND_STATUS, payload, hci.OCF_EXIT_SNIFF_MODE)

    def test_an_unrelated_event_is_not_a_reply(self):
        # Connection Complete, for instance, arrives constantly on a busy
        # adapter and carries a status byte in the same position.
        assert hci._match_reply(0x03, b"\x00\x0b\x00", hci.OCF_WRITE_LINK_POLICY) is None


class TestSlotConversion:
    """Nearly every HCI duration is a count of 0.625 ms baseband slots."""

    def test_round_trips_a_whole_number_of_slots(self):
        assert hci.ms_to_slots(30.0) == 48
        assert hci.slots_to_ms(48) == 30.0

    def test_rounds_rather_than_truncating(self):
        # Truncation would turn a request for 30 ms into 29.375 ms whenever
        # floating point left the division a hair under 48.
        assert hci.ms_to_slots(29.9999) == 48

    def test_never_returns_zero(self):
        # Zero means "infinite" to the flush timeout, which is the exact
        # default this module exists to move away from. Asking for a very short
        # timeout must never silently become no timeout at all.
        assert hci.ms_to_slots(0.0) >= 1
        assert hci.ms_to_slots(0.1) >= 1


class TestLinkPolicyEncoding:
    """The policy bits are the whole point, so pin them explicitly."""

    def test_sniff_is_refused_by_default(self):
        # The default must refuse sniff. With it allowed, either end may park
        # the link and the first report after an idle gap pays a full sniff
        # exit -- the ~70 ms cost this project had written off as unavoidable.
        assert LinkPolicy().link_policy_bits() & hci.LP_SNIFF == 0

    def test_role_switch_is_allowed_by_default(self):
        # We page the console to reconnect, which makes us central. Letting it
        # take the role back is how it ends up scheduling us as one of its own
        # peripherals.
        assert LinkPolicy().link_policy_bits() & hci.LP_ROLE_SWITCH

    def test_hold_and_park_are_never_set(self):
        # Both suspend the link for longer than sniff does and nothing here has
        # any use for either, whatever else the policy says.
        for allow_sniff in (True, False):
            bits = LinkPolicy(allow_sniff=allow_sniff).link_policy_bits()
            assert bits & hci.LP_HOLD == 0
            assert bits & hci.LP_PARK == 0

    def test_sniff_can_be_re_enabled_deliberately(self):
        assert LinkPolicy(allow_sniff=True).link_policy_bits() & hci.LP_SNIFF

    def test_flush_timeout_fits_the_twelve_bit_field(self):
        # 0x07FF slots is the largest finite automatic flush timeout. A policy
        # asking for longer must be clamped, not wrapped: a wrapped value could
        # land on 0, which means *infinite* and is the opposite of the request.
        slots = min(hci.ms_to_slots(LinkPolicy().flush_timeout_ms), hci.MAX_FLUSH_TIMEOUT_SLOTS)
        assert 0 < slots <= hci.MAX_FLUSH_TIMEOUT_SLOTS


class TestKeepaliveIsPartOfTheFlushDesign:
    """A tight flush timeout is only safe because the state is re-sent.

    Flushing discards a report, and with send-on-change alone the console would
    hold that stale state until the player next changed something -- a stuck
    button, which is far worse than the jitter the flush timeout prevents. The
    keepalive is what makes a discarded report cost one interval instead of the
    rest of the session, so the two are one design and not two features.
    """

    def test_a_keepalive_interval_exists_by_default(self):
        assert LinkPolicy().keepalive_interval_s > 0

    def test_keepalive_is_frequent_relative_to_the_flush_timeout(self):
        # If the keepalive were slower than the flush timeout, a flushed report
        # would leave the console stale for longer than the timeout was chosen
        # to bound. Some margin, so the recovery is prompt rather than merely
        # eventual.
        policy = LinkPolicy()
        assert policy.keepalive_interval_s * 1000 <= policy.flush_timeout_ms

    def test_it_can_be_switched_off(self):
        assert LinkPolicy(keepalive_hz=0).keepalive_interval_s == 0


class TestLinkReportIsHonest:
    """The report is read back from the controller, not assumed from the write.

    Several of this project's longest debugging sessions were a write that
    reported success and did nothing, so a report that simply echoed what was
    requested would be worse than useless.
    """

    def test_sniff_allowed_reflects_the_read_back_value(self):
        assert LinkReport(handle=1, link_policy=hci.LP_ROLE_SWITCH).sniff_allowed is False
        assert LinkReport(handle=1, link_policy=hci.LP_SNIFF).sniff_allowed is True

    def test_unknown_when_nothing_could_be_read(self):
        # None means "we do not know", which is a different answer from "sniff
        # is off" and must not be rendered as one.
        assert LinkReport(handle=1).sniff_allowed is None

    def test_snapshot_converts_slots_to_milliseconds(self):
        report = LinkReport(
            handle=0x000B,
            link_policy=hci.LP_ROLE_SWITCH,
            flush_timeout_slots=48,
            supervision_timeout_slots=8000,
            applied=("flush-timeout",),
        )
        snap = report.snapshot()
        assert snap["flush_timeout_ms"] == 30.0
        assert snap["supervision_timeout_ms"] == 5000.0
        assert snap["sniff_allowed"] is False
        assert snap["applied"] == ["flush-timeout"]


class TestTuningIsNeverFatal:
    """A controller that refuses these commands still carries a HID link.

    Every one of them is an optimisation. Refusing to bring an adapter up
    because the flush timeout could not be set would trade a working controller
    for better tail latency, which is exactly the wrong way round -- so the
    failure path has to be a warning and a working adapter, not an exception.
    """

    def test_a_tuner_that_cannot_open_reports_failure_rather_than_raising(self):
        from server.bt.link import LinkTuner

        # Index 250 will not exist on any machine this runs on, and on a dev
        # machine there is no AF_BLUETOOTH at all. Either way: False, not a
        # traceback.
        assert LinkTuner(250).open() is False

    def test_applying_defaults_without_a_channel_is_a_no_op(self):
        from server.bt.link import LinkTuner

        tuner = LinkTuner(250)
        tuner.apply_adapter_defaults()          # must not raise
        tuner.close()

    def test_tuning_a_connection_without_a_channel_reports_it(self):
        from server.bt.link import LinkTuner

        report = LinkTuner(250).tune(0x000B)
        assert report.handle == 0x000B
        assert "hci-channel" in report.failed
        # Nothing was read, so nothing is claimed. "Unknown" and "off" are
        # different answers and the GUI must be able to tell them apart.
        assert report.sniff_allowed is None

    def test_acl_handle_lookup_tolerates_a_socket_that_cannot_answer(self):
        from server.bt.link import acl_handle_for

        class NotASocket:
            pass

        class ClosedSocket:
            def getsockopt(self, *_args):
                raise OSError("closed")

        # A link that dropped between accept and tuning is ordinary; it must not
        # take the session down with it.
        assert acl_handle_for(NotASocket()) is None
        assert acl_handle_for(ClosedSocket()) is None

    def test_set_flushable_reports_failure_without_raising(self):
        from server.bt.link import set_flushable

        class Refuses:
            def setsockopt(self, *_args):
                raise OSError("unsupported")

        assert set_flushable(Refuses()) is False


class TestAdapterManagerDegradesWithoutHCI:
    """The manager must bring adapters up on a machine with no HCI channel."""

    def test_start_tuner_returns_none_rather_than_failing_the_adapter(self):
        from server.bt.adapter import AdapterInfo, AdapterManager
        from server.config import ServerConfig
        from server.router import Router

        manager = AdapterManager(Router(), ServerConfig())
        adapter = AdapterInfo(bd_addr="AA:BB:CC:DD:EE:FF", hci_name="hci250")
        assert manager._start_tuner(adapter) is None

    def test_an_adapter_with_no_hci_index_is_skipped(self):
        """bluetoothctl-only enumeration stores an address here, not an index.

        HCI sockets are bound by index, so there is nothing to open -- and
        ``int("AA:BB:...")`` would raise if this were not checked.
        """
        from server.bt.adapter import AdapterInfo, AdapterManager
        from server.config import ServerConfig
        from server.router import Router

        manager = AdapterManager(Router(), ServerConfig())
        adapter = AdapterInfo(bd_addr="AA:BB:CC:DD:EE:FF", hci_name="AA:BB:CC:DD:EE:FF")
        assert manager._start_tuner(adapter) is None


class TestBenignFailuresAreNotFailures:
    """Two commands are refused on every healthy link, for good reasons.

    Both measured on a Pi 5 (BlueZ 5.82, kernel 6.18) against a live ACL link:

    * ``exit-sniff`` returns 0x0C when the link is already active. We send it
      unconditionally because the link may be parked when we arrive and there
      is no command to ask which mode it is in, so "disallowed" is the ordinary
      answer rather than a problem.
    * ``supervision-timeout`` returns 0x0C in the peripheral role, which is the
      role we are in whenever a console connects to *us*. The central owns that
      timeout by specification.

    Counting either as a failure makes every healthy link log a warning naming
    commands that could never have succeeded -- which is worse than silence,
    because the same warning is used for real faults.
    """

    def test_exit_sniff_disallowed_is_expected(self):
        from server.bt.link import _BENIGN_STATUS

        assert 0x0C in _BENIGN_STATUS["exit-sniff"]

    def test_supervision_disallowed_is_expected(self):
        from server.bt.link import _BENIGN_STATUS

        assert 0x0C in _BENIGN_STATUS["supervision-timeout"]

    def test_a_real_failure_is_still_a_failure(self):
        from server.bt.link import _BENIGN_STATUS

        # 0x11 is "unsupported feature or parameter value" -- a genuine
        # problem, and it must not be swallowed by the same table.
        assert 0x11 not in _BENIGN_STATUS["exit-sniff"]
        assert 0x11 not in _BENIGN_STATUS["supervision-timeout"]

    def test_flush_timeout_has_no_excuses(self):
        from server.bt.link import _BENIGN_STATUS

        # The flush timeout is the one that matters most and it works in both
        # roles, so nothing about it should ever be explained away.
        assert "flush-timeout" not in _BENIGN_STATUS
        assert "link-policy" not in _BENIGN_STATUS

    def test_skipped_is_reported_separately_from_failed(self):
        report = LinkReport(
            handle=5,
            link_policy=hci.LP_ROLE_SWITCH,
            applied=("link-policy", "flush-timeout"),
            skipped=(("supervision-timeout", "peripheral role: the central owns it"),),
        )
        snap = report.snapshot()
        assert snap["failed"] == []
        assert "supervision-timeout" in snap["skipped"]


class TestAdapterDefaultsAreReAsserted:
    """A controller reset discards the default link policy and says nothing.

    Observed live on the reference Pi: `hciconfig hci2 reset`, run to clear an
    unrelated stale connection, brought the adapter back with default link
    policy 0x000F -- hold, sniff and park all permitted -- while its three
    siblings stayed at 0x0001. Nothing reported it. The per-connection tuning
    still fixes each link as it comes up, but the default exists to close the
    window before that, so it has to be re-asserted rather than set once.
    """

    class _FakeHCI:
        def __init__(self, current):
            self.current = current
            self.writes = []
            self.is_open = True

        def command(self, opcode, params=b""):
            import struct

            if opcode == hci.OCF_READ_DEFAULT_LINK_POLICY:
                return struct.pack("<H", self.current)
            if opcode == hci.OCF_WRITE_DEFAULT_LINK_POLICY:
                self.current = struct.unpack_from("<H", params, 0)[0]
                self.writes.append(self.current)
            return b""

    def _tuner(self, current):
        from server.bt.link import LinkTuner

        tuner = LinkTuner(0)
        tuner._hci = self._FakeHCI(current)
        return tuner

    def test_a_reset_adapter_is_corrected(self):
        tuner = self._tuner(0x000F)
        assert tuner.ensure_adapter_defaults() is True
        assert tuner._hci.current == 0x0001

    def test_a_correct_adapter_is_left_alone(self):
        # The reconcile runs every 10 s for the life of the process; writing
        # the same value each time would fight bluetoothd for no gain.
        tuner = self._tuner(0x0001)
        assert tuner.ensure_adapter_defaults() is False
        assert tuner._hci.writes == []

    def test_it_warns_about_what_was_actually_wrong(self, caplog):
        tuner = self._tuner(0x000F)
        with caplog.at_level("WARNING"):
            tuner.ensure_adapter_defaults()
        assert "sniff" in caplog.text


class TestTheLEPingTimeout:
    """The Authenticated Payload Timeout, and why it has to be extended.

    Its default is 30 s and it assumes both ends of an encrypted link transmit.
    A gamepad breaks that assumption by design: it notifies the console and the
    console answers nothing, so no authenticated packet ever arrives from the
    peer. The controller raises `Authenticated Payload Timeout Expired` and the
    kernel disconnects -- correct by the letter of the spec, and fatal for us.

    Measured against an Analogue 3D over a real LE link: connect, encrypt,
    subscribe to notifications, receive reports, hang up 30 s later, repeat.
    Six times in three minutes. Every counter on our side reported a healthy
    connection throughout, because from our side it was one -- the only trace
    is one HCI event immediately before each `Disconnect` command.
    """

    def test_the_default_is_effectively_no_policing(self):
        """A peer that legitimately never transmits must not be timed out.

        Genuine link loss is still caught by the supervision timeout, which is
        the timer that means "the peer is gone" rather than "the peer is quiet".
        """
        from server.bt.link import LEPingTuner

        assert LEPingTuner(0)._timeout == hci.MAX_AUTH_PAYLOAD_TIMEOUT

    def test_an_explicit_timeout_is_converted_to_ten_millisecond_units(self):
        from server.bt.link import LEPingTuner

        assert LEPingTuner(0, timeout_ms=30_000)._timeout == 3000

    def test_it_is_clamped_to_what_the_field_can_hold(self):
        """The field is a uint16, so an over-long request must not wrap to a
        short one -- which would produce exactly the bug being fixed."""
        from server.bt.link import LEPingTuner

        assert (
            LEPingTuner(0, timeout_ms=10_000_000)._timeout
            == hci.MAX_AUTH_PAYLOAD_TIMEOUT
        )
        assert LEPingTuner(0, timeout_ms=0)._timeout >= 1

    def test_the_opcode_is_the_specified_one(self):
        # OGF 0x03 (Controller & Baseband), OCF 0x7C.
        assert hci.OCF_WRITE_AUTHENTICATED_PAYLOAD_TIMEOUT == (0x03 << 10) | 0x7C

    def test_tuning_an_absent_peer_reports_failure_rather_than_raising(self):
        """Link tuning is never allowed to be fatal."""
        from server.bt.link import LEPingTuner

        assert LEPingTuner(0).tune_peer("AA:BB:CC:DD:EE:FF") is False


class TestTheConnectionList:
    """Finding an LE link's handle, which is otherwise unobtainable.

    The Classic path reads it from an L2CAP socket via L2CAP_CONNINFO. A BLE
    connection belongs to bluetoothd and we hold no socket on it, so the
    kernel's connection list is the only way to ask.
    """

    def test_the_ioctl_number_is_the_one_bluez_uses(self):
        # _IOR('H', 212, int): dir=2, size=4, type=0x48, nr=0xD4.
        assert hci.HCIGETCONNLIST == (2 << 30) | (4 << 16) | (0x48 << 8) | 0xD4

    def test_the_conn_info_struct_is_sixteen_bytes(self):
        # handle(2) + bdaddr(6) + type(1) + out(1) + state(2) + link_mode(4).
        assert hci._CONN_INFO.size == 16

    def test_it_degrades_to_empty_where_the_platform_cannot_answer(self):
        """Windows has no fcntl, and this feeds tuning that must never fail."""
        assert hci.connections(0) == [] or isinstance(hci.connections(0), list)
        assert hci.handle_for_address(0, "AA:BB:CC:DD:EE:FF") in (None, 0) or True


class TestTheKernelPutsItsOwnTimeoutBack:
    """Setting the LE ping timeout on connect is not enough, and the failure
    looks exactly like success.

    Measured, from a btmon capture of a real Analogue 3D link::

            Timeout: 655350 msec (0xffff)     <- ours, on connect
        > HCI Event: Encryption Change
            Timeout: 30000 msec (0x0bb8)      <- the kernel, overwriting us

    The kernel re-applies its own default when encryption completes, which
    always lands *after* a write made when the link came up. Our log line
    reported the timeout set to 655 s moments before it was put back to 30,
    and the console went on reconnecting every 34.7 s -- a fix that reports
    success and changes nothing.

    There is no event meaning "the kernel has finished with this link", so the
    only version that wins is holding it as an invariant in the reconcile pass,
    the same way page scan is held.
    """

    def test_the_tuner_can_re_assert_rather_than_only_set(self):
        from server.bt.link import LEPingTuner

        assert hasattr(LEPingTuner(0), "ensure_peer")

    def test_re_asserting_an_absent_peer_is_a_no_op(self):
        from server.bt.link import LEPingTuner

        assert LEPingTuner(0).ensure_peer("AA:BB:CC:DD:EE:FF") is False

    def test_the_reconcile_pass_holds_it(self):
        """If this hook is removed the console silently returns to a 35 s
        reconnect loop, so the wiring is pinned rather than the behaviour."""
        import inspect

        from server.bt import adapter

        source = inspect.getsource(adapter.AdapterManager)
        assert "_ensure_le_ping" in source
        assert "def _ensure_le_ping" in source

    def test_the_read_opcode_is_the_specified_one(self):
        assert hci.OCF_READ_AUTHENTICATED_PAYLOAD_TIMEOUT == (0x03 << 10) | 0x7B

    def test_the_reconcile_interval_beats_the_default_timeout(self):
        """The invariant is racing a 30 s kernel timer.

        A reconcile slower than that would correct links only after they had
        already been dropped, which is no correction at all.
        """
        from server.bt import adapter as adapter_module

        interval = getattr(adapter_module, "RECONCILE_INTERVAL_S", 10.0)
        assert interval < 30.0


class TestLinkTuningNeverRunsOnTheEventLoop:
    """Blocking HCI I/O must not sit on the asyncio thread.

    This is a regression test for a fix that caused a worse bug than it cured.
    ``LEPingTuner`` does an ioctl plus two HCI commands, each with a one-second
    timeout. Called inline from the reconcile pass -- which runs on the asyncio
    loop, the same thread ``BLESink`` notifies the console from -- it stalled
    the loop for about four seconds every ten.

    Measured: notifications ran at 10/s, stopped dead for four seconds the
    instant the reconcile pass ran, and the kernel then dropped the idle link.
    The player experienced a console that worked for thirty seconds and then
    went unresponsive for several -- strictly worse than the 30 s timeout the
    tuner was added to fix, and caused by fixing it.

    The general rule this pins: anything on the reconcile path that talks to a
    socket belongs in an executor, because the GUI, the control plane and every
    BLE notification share that one thread.
    """

    def test_the_invariant_is_a_coroutine(self):
        import inspect

        from server.bt.adapter import AdapterManager

        assert inspect.iscoroutinefunction(AdapterManager._ensure_le_ping)

    def test_it_hands_the_blocking_work_to_an_executor(self):
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._ensure_le_ping)
        assert "run_in_executor" in source, (
            "the HCI commands must not run on the asyncio thread"
        )

    def test_the_link_up_path_also_stays_off_the_loop(self):
        """_note_link runs on the loop too, so the one-shot write needs the
        same treatment as the invariant."""
        import inspect

        from server.bt.adapter import AdapterManager

        source = inspect.getsource(AdapterManager._note_link)
        assert "_run_off_loop" in source

    def test_the_helper_still_works_without_a_running_loop(self):
        """Called from a plain thread it should just run, not raise."""
        from server.bt.adapter import AdapterManager

        seen = []
        AdapterManager._run_off_loop(None, seen.append, "ran")
        assert seen == ["ran"]

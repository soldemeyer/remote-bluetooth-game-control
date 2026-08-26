"""Bluetooth adapter setup: SDP registration options and pairing-state checks.

Every test here is a regression for a defect that produced **no error anywhere**.
That is the common thread: BlueZ accepts a wrong `RegisterProfile` option, a
grep against the wrong line always matches, and a legacy PIN fallback logged at
info level. Each looked healthy right up until pairing quietly misbehaved.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.bt.profiles import create_profile
from server.bt.sink import MockSink

from server.bt.adapter import (
    _pairing_settings_ok,
    _read_mgmt_settings,
)

# dbus-next is a server-side (Linux) dependency, so the classes that touch
# ProfileManager1 and Agent1 only run where it is installed. The MGMT settings
# tests below are pure parsing and run everywhere.
try:
    import dbus_next  # noqa: F401

    HAS_DBUS = True
except ImportError:
    HAS_DBUS = False

needs_dbus = pytest.mark.skipif(not HAS_DBUS, reason="dbus-next not installed (server dependency)")

# Real `btmgmt info` output from the Pi. The point of using it verbatim is the
# `supported settings:` line -- it contains `link-security` and `ssp` on an
# adapter where neither is *currently* set, which is exactly what defeated the
# original check.
BTMGMT_INFO_HEALTHY = """hci0:\tPrimary controller
\taddr DC:A6:32:B9:6A:88 version 10 manufacturer 305 class 0x002508
\tsupported settings: powered connectable fast-connectable discoverable bondable link-security ssp br/edr le advertising secure-conn debug-keys privacy configuration static-addr phy-configuration wide-band-speech
\tcurrent settings: powered bondable ssp br/edr le secure-conn
\tname controller-server
\tshort name
"""

BTMGMT_INFO_LINKSEC_ON = """hci0:\tPrimary controller
\taddr DC:A6:32:B9:6A:88 version 10 manufacturer 305 class 0x002508
\tsupported settings: powered connectable discoverable bondable link-security ssp br/edr le
\tcurrent settings: powered bondable link-security ssp br/edr
\tname controller-server
"""

BTMGMT_INFO_NO_SSP = """hci0:\tPrimary controller
\tsupported settings: powered connectable discoverable bondable link-security ssp br/edr le
\tcurrent settings: powered bondable br/edr le
"""


class TestCurrentSettingsParsing:
    """The original bug: `if "link-security" in output`.

    That string is in the *supported* settings line on essentially every
    adapter, so the check matched unconditionally -- it warned constantly and
    verified nothing, and it never looked at `ssp` at all.
    """

    def _read(self, output: str):
        with patch("server.bt.adapter._run", return_value=(0, output)):
            return _read_mgmt_settings("0")

    def test_reads_current_not_supported(self):
        settings = self._read(BTMGMT_INFO_HEALTHY)

        assert "ssp" in settings
        assert "bondable" in settings
        # Present in `supported settings:`, absent from `current settings:`.
        assert "link-security" not in settings

    def test_the_resting_state_of_a_working_adapter_passes(self):
        """No `bondable` is normal, not a fault.

        Copied verbatim from a Pi serving four live controllers. `bondable`
        follows `Adapter1.Pairable`, which is false outside a pairing window,
        so requiring it made this check fail on every healthy adapter -- and
        its "correction" would have written bondable on behind bluetoothd.
        """
        settings = self._read(
            "hci4:\tPrimary controller\n"
            "\tsupported settings: powered connectable discoverable bondable "
            "link-security ssp br/edr le secure-conn\n"
            "\tcurrent settings: powered connectable ssp br/edr le secure-conn\n"
        )

        assert "bondable" not in settings
        assert _pairing_settings_ok(settings)

    def test_healthy_adapter_passes(self):
        assert _pairing_settings_ok(self._read(BTMGMT_INFO_HEALTHY))

    def test_link_security_enabled_is_caught(self):
        settings = self._read(BTMGMT_INFO_LINKSEC_ON)

        assert "link-security" in settings
        assert not _pairing_settings_ok(settings)

    def test_ssp_disabled_is_caught(self):
        settings = self._read(BTMGMT_INFO_NO_SSP)

        assert "ssp" not in settings
        assert not _pairing_settings_ok(settings)

    def test_unreadable_returns_none(self):
        with patch("server.bt.adapter._run", return_value=(1, "no such index")):
            assert _read_mgmt_settings("9") is None

    def test_missing_current_line_returns_none(self):
        with patch("server.bt.adapter._run", return_value=(0, "hci0:\tPrimary controller\n")):
            assert _read_mgmt_settings("0") is None


class TestPairingSettingsPredicate:
    @pytest.mark.parametrize(
        "settings,expected",
        [
            ({"powered", "bondable", "ssp", "br/edr"}, True),
            ({"powered", "bondable", "ssp", "link-security"}, False),  # forbidden present
            # bondable absent is the normal resting state -- it tracks
            # Adapter1.Pairable, which we hold false outside a pairing window.
            ({"powered", "ssp"}, True),
            ({"powered", "bondable"}, False),                          # ssp missing
            (set(), False),
        ],
    )
    def test_predicate(self, settings, expected):
        assert _pairing_settings_ok(settings) is expected


class TestEnsurePairingSettings:
    """bluetoothd owns these settings; btmgmt is a second MGMT client.

    So we read first and only write when something is genuinely wrong --
    otherwise two owners fight over one piece of state.
    """

    def _adapter(self):
        from server.bt.adapter import AdapterInfo

        return AdapterInfo(bd_addr="DC:A6:32:B9:6A:88", hci_name="hci0")

    def test_healthy_adapter_is_not_written_to(self):
        from server.bt.adapter import _ensure_pairing_settings

        with patch("server.bt.adapter.shutil.which", return_value="/usr/bin/btmgmt"), \
             patch("server.bt.adapter._run", return_value=(0, BTMGMT_INFO_HEALTHY)) as run:
            _ensure_pairing_settings(self._adapter())

        # Reads only -- `info`, never `linksec`/`ssp`/`bondable`.
        written = [c.args[0] for c in run.call_args_list if "info" not in c.args[0]]
        assert not written, f"wrote to a healthy adapter: {written}"

    def test_broken_adapter_is_corrected(self):
        from server.bt.adapter import _ensure_pairing_settings

        outputs = [
            (0, BTMGMT_INFO_LINKSEC_ON),  # initial read: broken
            (0, ""), (0, ""),             # the two corrections
            (0, BTMGMT_INFO_HEALTHY),     # re-read: fixed
        ]
        with patch("server.bt.adapter.shutil.which", return_value="/usr/bin/btmgmt"), \
             patch("server.bt.adapter._run", side_effect=outputs) as run:
            _ensure_pairing_settings(self._adapter())

        commands = [" ".join(c.args[0]) for c in run.call_args_list]
        assert any("linksec off" in c for c in commands)
        assert any("ssp on" in c for c in commands)
        # Not bondable: that is bluetoothd's, moved via Adapter1.Pairable.
        # Writing it from a second MGMT client desynchronises the two.
        assert not any("bondable" in c for c in commands), commands

    def test_uncorrectable_adapter_logs_an_error(self, caplog):
        from server.bt.adapter import _ensure_pairing_settings

        outputs = [
            (0, BTMGMT_INFO_NO_SSP),
            (1, "Failed"), (1, "Failed"),
            (0, BTMGMT_INFO_NO_SSP),  # still broken
        ]
        with patch("server.bt.adapter.shutil.which", return_value="/usr/bin/btmgmt"), \
             patch("server.bt.adapter._run", side_effect=outputs), \
             caplog.at_level(logging.ERROR):
            _ensure_pairing_settings(self._adapter())

        assert "Secure Simple Pairing" in caplog.text

    def test_no_btmgmt_is_not_fatal(self):
        from server.bt.adapter import _ensure_pairing_settings

        with patch("server.bt.adapter.shutil.which", return_value=None):
            _ensure_pairing_settings(self._adapter())  # must not raise


class TestHelpersNeverInheritStdin:
    """A helper must never be handed the service's stdin.

    Under systemd that is `/dev/null` (`StandardInput=null` is the default),
    and **btmgmt hangs on /dev/null forever**: it is built on BlueZ's
    `bt_shell`, which watches stdin even for a one-shot command, and
    `/dev/null` is permanently read-ready without ever delivering the EOF
    event that would end it. Every btmgmt call the server made timed out --
    invisibly, because the failure only reached `log.debug`.

    It cannot be reproduced portably: it needs Linux, /dev/null and a real
    btmgmt. So this pins the invariant that prevents it instead -- `_run`
    always supplies its own empty stdin, whatever it was launched with.
    """

    def test_run_supplies_an_empty_stdin(self):
        from server.bt.adapter import _run

        with patch("server.bt.adapter.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            _run(["btmgmt", "--index", "4", "info"])

        assert run.call_args.kwargs.get("input") == "", (
            "btmgmt would inherit the service's /dev/null stdin and hang"
        )

    def test_a_child_that_reads_stdin_still_terminates(self):
        """End-to-end on a real process, since the point is not hanging."""
        from server.bt.adapter import _run

        code, output = _run(
            [sys.executable, "-c", "import sys; sys.stdout.write(repr(sys.stdin.read()))"],
            timeout=30,
        )

        assert code == 0
        assert output == "''"


@needs_dbus
class TestRegisterProfileOptions:
    """`Channel` is the RFCOMM channel; HID is L2CAP.

    Passing the control PSM as `Channel` described us to BlueZ as an RFCOMM
    profile on channel 17. No error is reported -- hosts connect to the fixed
    PSMs regardless, so it worked by luck.
    """

    @pytest.fixture
    def options(self):
        from server.bt.profiles import create_profile
        from server.bt.sdp import register_hid_profile

        descriptor = create_profile("generic").descriptor
        captured = {}

        manager = MagicMock()
        manager.call_register_profile = AsyncMock()

        bus = MagicMock()
        bus.introspect = AsyncMock()
        bus.get_proxy_object.return_value.get_interface.return_value = manager

        async def connect(self):
            return bus

        with patch("dbus_next.aio.MessageBus.connect", connect):
            import asyncio

            asyncio.run(
                register_hid_profile(
                    descriptor.device_name,
                    descriptor.report_descriptor,
                    descriptor.vendor_id,
                    descriptor.product_id,
                )
            )

        captured.update(manager.call_register_profile.call_args.args[2])
        return captured

    def test_does_not_pass_rfcomm_channel(self, options):
        assert "Channel" not in options, (
            "Channel is the RFCOMM channel number -- HID runs over L2CAP"
        )

    def test_does_not_pass_psm_either(self, options):
        """We bind PSM 17/19 ourselves, per adapter."""
        assert "PSM" not in options

    def test_declares_the_hid_service_class(self, options):
        from server.bt.sdp import HID_UUID

        assert options["Service"].value == HID_UUID

    def test_registers_in_the_server_role(self, options):
        assert options["Role"].value == "server"

    def test_does_not_require_authentication(self, options):
        assert options["RequireAuthentication"].value is False
        assert options["RequireAuthorization"].value is False

    def test_carries_the_manual_service_record(self, options):
        assert "ServiceRecord" in options
        assert "0x1124" in options["ServiceRecord"].value

    def test_no_autoconnect(self, options):
        """Documented as applying to client UUIDs; we register as a server."""
        assert "AutoConnect" not in options


@needs_dbus
class TestAgentLoudness:
    """A PIN request means SSP was not used.

    This was logged at info and read past for eight rounds of debugging while
    the answer sat in the journal.
    """

    # Note: dbus-next's @method() decorator wraps these for D-Bus dispatch, so
    # calling them directly returns None regardless of the body. The reply value
    # is covered separately by asserting the constant; what these tests pin down
    # is the log level, which is the part that was wrong.

    def test_pin_request_logs_a_warning(self, caplog):
        from server.bt.agent import PairingAgent

        agent = PairingAgent()
        with caplog.at_level(logging.WARNING):
            agent.RequestPinCode("/org/bluez/hci0/dev_AA_BB")

        assert caplog.records, "a legacy PIN request must not be silent"
        assert caplog.records[0].levelno >= logging.WARNING
        assert "Secure Simple Pairing was NOT used" in caplog.text

    def test_passkey_request_logs_a_warning(self, caplog):
        from server.bt.agent import PairingAgent

        agent = PairingAgent()
        with caplog.at_level(logging.WARNING):
            agent.RequestPasskey("/org/bluez/hci0/dev_AA_BB")

        assert caplog.records
        assert caplog.records[0].levelno >= logging.WARNING
        assert "Secure Simple Pairing was NOT used" in caplog.text

    def test_legacy_pin_is_the_conventional_value(self):
        """Hosts that fall back to legacy pairing try 0000 by default."""
        from server.bt.agent import LEGACY_PIN

        assert LEGACY_PIN == "0000"

    def test_just_works_confirmation_stays_quiet(self, caplog):
        """The SSP path is the normal one and must not warn."""
        from server.bt.agent import PairingAgent

        agent = PairingAgent()
        with caplog.at_level(logging.WARNING):
            agent.RequestConfirmation("/org/bluez/hci0/dev_AA_BB", 123456)

        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def _noop_async(*args, **kwargs):
    return None


async def _always_true(*args, **kwargs):
    return True


def _manager():
    """An AdapterManager with no hardware behind it, for state-only checks."""
    from server.bt.adapter import AdapterManager
    from server.config import ServerConfig
    from server.router import Router

    return AdapterManager(Router(), ServerConfig())


class TestDegradedAdapter:
    """An adapter whose HID stack failed must stop advertising as healthy.

    ``_start_hid`` deliberately leaves a failed adapter registered and named --
    a vanished adapter is harder to diagnose than a broken one. But it used to
    keep *advertising* too, so a host would discover it, pair, and then fail the
    interrupt connect with nothing to explain it. Three adapters ran that way
    for three days: the one ERROR line naming the cause went to a terminal that
    had since closed.
    """

    def _adapter(self, **kwargs):
        from server.bt.adapter import AdapterInfo

        return AdapterInfo(bd_addr="DC:A6:32:B9:6A:88", hci_name="hci0", **kwargs)

    def test_a_healthy_adapter_reports_no_error(self):
        assert self._adapter().hid_error == ""
        assert self._adapter().snapshot()["hid_error"] == ""

    def test_the_failure_reaches_the_gui(self):
        """The web GUI is the only place an operator would ever see this."""
        adapter = self._adapter(hid_error="L2CAP PSM 17/19 already in use")

        assert adapter.snapshot()["hid_error"] == "L2CAP PSM 17/19 already in use"

    @pytest.mark.asyncio
    async def test_pairing_is_refused_on_a_degraded_adapter(self):
        manager = _manager()
        adapter = self._adapter(hid_error="L2CAP PSM 17/19 already in use")
        manager._adapters[adapter.bd_addr] = adapter

        ok, message = await manager.set_pairable(adapter.bd_addr, True)

        assert not ok
        assert "HID is not running" in message

    @pytest.mark.asyncio
    async def test_the_reason_is_included_so_it_is_actionable(self):
        manager = _manager()
        adapter = self._adapter(hid_error="Permission denied binding L2CAP")
        manager._adapters[adapter.bd_addr] = adapter

        _ok, message = await manager.set_pairable(adapter.bd_addr, True)

        assert "Permission denied binding L2CAP" in message

    @pytest.mark.asyncio
    async def test_stopping_pairing_is_still_allowed(self):
        """Turning it *off* must never be blocked, whatever state we are in."""
        manager = _manager()
        adapter = self._adapter(hid_error="L2CAP PSM 17/19 already in use")
        manager._adapters[adapter.bd_addr] = adapter

        ok, _message = await manager.set_pairable(adapter.bd_addr, False)

        assert ok or "HID is not running" not in _message

    @pytest.mark.asyncio
    async def test_rescan_keeps_the_failure(self, monkeypatch):
        """rescan() rebuilds every AdapterInfo, and the hot-plug watcher runs it
        every 10 s. Losing the failure there would make a dead adapter look
        healthy again seconds later -- exactly how the pairing countdown was
        being wiped before it was carried across."""
        from server.bt import adapter as adapter_mod

        manager = _manager()
        broken = self._adapter(hid_error="L2CAP PSM 17/19 already in use")
        manager._adapters[broken.bd_addr] = broken

        # The radio is still present, so rescan builds a fresh, clean object.
        monkeypatch.setattr(
            adapter_mod, "_enumerate_adapters", lambda: [self._adapter()]
        )
        monkeypatch.setattr(manager, "_reconcile_channels", _noop_async)

        await manager.rescan()

        assert manager._adapters[broken.bd_addr].hid_error == (
            "L2CAP PSM 17/19 already in use"
        )

    @pytest.mark.asyncio
    async def test_rescan_does_not_invent_a_failure(self, monkeypatch):
        from server.bt import adapter as adapter_mod

        manager = _manager()
        manager._adapters[self._adapter().bd_addr] = self._adapter()

        monkeypatch.setattr(
            adapter_mod, "_enumerate_adapters", lambda: [self._adapter()]
        )
        monkeypatch.setattr(manager, "_reconcile_channels", _noop_async)

        await manager.rescan()

        assert manager._adapters[self._adapter().bd_addr].hid_error == ""


class TestQuietingADisabledAdapter:
    """Disabling an adapter must stop it advertising.

    Tearing down the L2CAP listeners left the radio still broadcasting our
    name, the gamepad class and the HID UUID with nothing behind it -- so a
    host would find a controller and get no answer. That is what Windows
    reports as "We didn't get any response from the device", and it is why a
    disabled Gamepad 2 kept appearing in Add-a-device.
    """

    def _manager_with(self, bd_addr="DC:A6:32:B9:6A:88", hci="hci0"):
        from server.bt.adapter import AdapterInfo

        manager = _manager()
        adapter = AdapterInfo(bd_addr=bd_addr, hci_name=hci)
        manager._adapters[bd_addr] = adapter
        return manager, adapter

    @pytest.mark.asyncio
    async def test_an_adapter_we_configured_is_quieted(self, monkeypatch):
        from server.bt import adapter_dbus

        calls = []

        async def fake_set_properties(hci, **kwargs):
            calls.append((hci, kwargs))
            return True

        monkeypatch.setattr(adapter_dbus, "set_properties", fake_set_properties)

        manager, adapter = self._manager_with()
        manager._configured.add(adapter.bd_addr)

        await manager._quiet_adapter(adapter.bd_addr)

        # `connectable` is the one that actually silences the radio: it makes
        # bluetoothd write scan enable 0x00, stopping inquiry *and* page scan.
        # Pairable/Discoverable alone are no-ops whenever BlueZ already thinks
        # the adapter is undiscoverable, which is the state hci4 was found in --
        # every management layer reporting "off" while it went on answering.
        assert calls == [
            (
                "hci0",
                {"connectable": False, "pairable": False, "discoverable": False},
            )
        ]

    @pytest.mark.asyncio
    async def test_an_adapter_we_never_touched_is_left_alone(self, monkeypatch):
        """The guarantee the operator relies on when the Pi uses a dongle for
        something else."""
        from server.bt import adapter_dbus

        calls = []

        async def fake_set_properties(hci, **kwargs):
            calls.append((hci, kwargs))
            return True

        monkeypatch.setattr(adapter_dbus, "set_properties", fake_set_properties)

        manager, adapter = self._manager_with()
        # Never enabled by us, so never in _configured.

        await manager._quiet_adapter(adapter.bd_addr)

        assert calls == []

    @pytest.mark.asyncio
    async def test_quieting_clears_any_pairing_countdown(self, monkeypatch):
        from server.bt import adapter_dbus

        monkeypatch.setattr(
            adapter_dbus, "set_properties", _always_true
        )

        manager, adapter = self._manager_with()
        manager._configured.add(adapter.bd_addr)
        adapter.pairing_until_ns = 1 << 60

        await manager._quiet_adapter(adapter.bd_addr)

        assert adapter.pairing_until_ns == 0

    @pytest.mark.asyncio
    async def test_quieting_twice_is_harmless(self, monkeypatch):
        from server.bt import adapter_dbus

        calls = []

        async def fake_set_properties(hci, **kwargs):
            calls.append(hci)
            return True

        monkeypatch.setattr(adapter_dbus, "set_properties", fake_set_properties)

        manager, adapter = self._manager_with()
        manager._configured.add(adapter.bd_addr)

        await manager._quiet_adapter(adapter.bd_addr)
        await manager._quiet_adapter(adapter.bd_addr)

        assert calls == ["hci0"], "second call should be a no-op"

    @pytest.mark.asyncio
    async def test_an_adapter_left_advertising_by_a_past_run_is_quieted(
        self, monkeypatch
    ):
        """The in-memory set is empty after a restart, so a disabled adapter
        stranded by an earlier session would advertise forever. A persisted
        number is durable proof we brought it up once."""
        from server.bt import adapter_dbus
        from server.config import AdapterConfig

        calls = []

        async def fake_set_properties(hci, **kwargs):
            calls.append(hci)
            return True

        monkeypatch.setattr(adapter_dbus, "set_properties", fake_set_properties)

        manager, adapter = self._manager_with(
            bd_addr="CC:28:AA:6D:BA:C0", hci="hci4"
        )
        adapter.enabled = False
        manager._config.upsert_adapter(
            AdapterConfig(
                bd_addr=adapter.bd_addr, enabled=False,
                profile="generic", number=2,
            )
        )
        assert manager._configured == set(), "fresh process"

        await manager._reconcile_channels()

        assert calls == ["hci4"]

    @pytest.mark.asyncio
    async def test_an_adapter_we_never_numbered_is_still_left_alone(
        self, monkeypatch
    ):
        """No number means we never brought it up -- it could be the operator's
        own dongle, and writing to it is the thing the rule protects against."""
        from server.bt import adapter_dbus
        from server.config import AdapterConfig

        calls = []

        async def fake_set_properties(hci, **kwargs):
            calls.append(hci)
            return True

        monkeypatch.setattr(adapter_dbus, "set_properties", fake_set_properties)

        manager, adapter = self._manager_with(
            bd_addr="CC:28:AA:6D:BA:C0", hci="hci4"
        )
        adapter.enabled = False
        manager._config.upsert_adapter(
            AdapterConfig(
                bd_addr=adapter.bd_addr, enabled=False,
                profile="generic", number=0,
            )
        )

        await manager._reconcile_channels()

        assert calls == []

    @pytest.mark.asyncio
    async def test_stopping_pairing_is_allowed_on_a_disabled_adapter(self):
        """Arming pairing then disabling used to leave no way to stop it: the
        one path that clears Discoverable refused disabled adapters."""
        manager, adapter = self._manager_with()
        adapter.enabled = False

        ok, message = await manager.set_pairable(adapter.bd_addr, False)

        assert "disabled" not in message.lower() or ok

    @pytest.mark.asyncio
    async def test_starting_pairing_is_still_refused_when_disabled(self):
        manager, adapter = self._manager_with()
        adapter.enabled = False

        ok, message = await manager.set_pairable(adapter.bd_addr, True)

        assert not ok
        assert "disabled" in message.lower()


class TestForgettingAReconnectTarget:
    """Clearing bonds must also stop us paging the host we just forgot.

    ``remove_bonds`` deletes the link key; the address stayed in config and in
    the live HID server, so the reconnect loop went on paging a host that could
    no longer authenticate us -- every 30 s, forever, logged at debug. No code
    path in the server cleared ``paired_target`` at all.
    """

    def _manager_with_target(self, target="68:EC:C5:82:6F:2B"):
        from server.bt.adapter import AdapterInfo
        from server.config import AdapterConfig

        manager = _manager()
        adapter = AdapterInfo(bd_addr="DC:A6:32:B9:6A:88", hci_name="hci0")
        manager._adapters[adapter.bd_addr] = adapter
        manager._config.upsert_adapter(
            AdapterConfig(
                bd_addr=adapter.bd_addr, enabled=True,
                profile="generic", paired_target=target,
            )
        )
        return manager, adapter

    def test_the_remembered_host_is_cleared(self):
        manager, adapter = self._manager_with_target()

        manager._forget_reconnect_target(adapter)

        assert manager._config.adapter(adapter.bd_addr).paired_target == ""

    def test_the_live_server_stops_chasing_it(self):
        class FakeServer:
            def __init__(self):
                self.target = "68:EC:C5:82:6F:2B"

            def set_reconnect_target(self, value):
                self.target = value

        manager, adapter = self._manager_with_target()
        server = FakeServer()
        manager._hid_servers[adapter.bd_addr] = server

        manager._forget_reconnect_target(adapter)

        assert server.target is None

    def test_it_clears_even_when_no_bond_was_found(self):
        """The case that strands an adapter. If the bond had already gone but
        the address stayed, we page a host we can never authenticate to -- and
        clearing only on a successful removal never fired."""
        manager, adapter = self._manager_with_target()

        manager._forget_reconnect_target(adapter)   # nothing was removed

        assert manager._config.adapter(adapter.bd_addr).paired_target == ""

    def test_no_target_is_a_noop(self):
        manager, adapter = self._manager_with_target(target="")

        manager._forget_reconnect_target(adapter)

        assert manager._config.adapter(adapter.bd_addr).paired_target == ""


class TestQuietingHappensOnce:
    """The hot-plug watcher reconciles every 10 s.

    Without a guard, a disabled adapter was re-quieted on every pass -- a D-Bus
    write and a log line every ten seconds, forever, for a state that had not
    changed since the first one.
    """

    def _manager(self, monkeypatch):
        from server.bt import adapter_dbus
        from server.bt.adapter import AdapterInfo
        from server.config import AdapterConfig

        calls = []

        async def fake_set_properties(hci, **kwargs):
            calls.append(hci)
            return True

        monkeypatch.setattr(adapter_dbus, "set_properties", fake_set_properties)

        manager = _manager()
        adapter = AdapterInfo(bd_addr="CC:28:AA:6D:BA:C0", hci_name="hci4")
        adapter.enabled = False
        manager._adapters[adapter.bd_addr] = adapter
        manager._config.upsert_adapter(
            AdapterConfig(
                bd_addr=adapter.bd_addr, enabled=False,
                profile="generic", number=2,
            )
        )
        return manager, adapter, calls

    @pytest.mark.asyncio
    async def test_repeated_reconciles_quiet_only_once(self, monkeypatch):
        manager, _adapter, calls = self._manager(monkeypatch)

        for _ in range(5):
            await manager._reconcile_channels()

        assert calls == ["hci4"]

    @pytest.mark.asyncio
    async def test_re_enabling_allows_quieting_again_later(self, monkeypatch):
        """Otherwise an adapter enabled and disabled twice would stay
        advertising the second time."""
        manager, adapter, calls = self._manager(monkeypatch)

        await manager._reconcile_channels()
        assert calls == ["hci4"]

        # Enable clears the latch, as _reconcile_channels does on the up path.
        manager._quieted.discard(adapter.bd_addr)
        adapter.enabled = False

        await manager._reconcile_channels()

        assert calls == ["hci4", "hci4"]


class TestReconcileIsSerialised:
    """Two reconcile passes must not race to bind the same adapter.

    ``_reconcile_channels`` awaits inside its loop and is driven from two
    independent places: the operator enabling an adapter in the web GUI, and
    the hot-plug watcher rescanning every 10 s. Overlapping, both saw "no
    channel yet" and both called ``_start_hid``; the second hit EADDRINUSE
    against our own listener, which reads exactly like bluetoothd holding the
    HID role.

    That is how three adapters ended up bound to PSM 17 with no PSM 19 -- one
    pass won both, the other won control and lost interrupt.
    """

    def _manager(self, monkeypatch):
        from server.bt import adapter as adapter_mod
        from server.bt.adapter import AdapterInfo
        from server.config import AdapterConfig

        manager = _manager()
        adapter = AdapterInfo(
            bd_addr="CC:28:AA:6D:BA:C0", hci_name="hci4", powered=True
        )
        manager._adapters[adapter.bd_addr] = adapter
        manager._config.upsert_adapter(
            AdapterConfig(
                bd_addr=adapter.bd_addr, enabled=True, profile="generic", number=2
            )
        )
        monkeypatch.setattr(adapter_mod, "_bring_up_adapter", lambda a: True)
        monkeypatch.setattr(adapter_mod, "_ensure_pairing_settings", lambda a: None)
        monkeypatch.setattr(adapter_mod, "_set_device_class", lambda a: None)
        monkeypatch.setattr(adapter_mod.adapter_dbus, "set_properties", _always_true)
        return manager, adapter

    @pytest.mark.asyncio
    async def test_concurrent_passes_start_hid_once(self, monkeypatch):
        manager, adapter = self._manager(monkeypatch)
        starts = []

        async def slow_start_hid(a, profile):
            # Await inside, exactly as the real one does -- that is what let a
            # second pass interleave.
            starts.append(a.bd_addr)
            await asyncio.sleep(0.02)
            manager._hid_servers[a.bd_addr] = object()
            return MockSink()

        monkeypatch.setattr(manager, "_start_hid", slow_start_hid)

        await asyncio.gather(
            manager._reconcile_channels(),
            manager._reconcile_channels(),
        )

        assert starts == ["CC:28:AA:6D:BA:C0"], f"started {len(starts)} times"

    @pytest.mark.asyncio
    async def test_start_hid_refuses_a_second_start(self, monkeypatch):
        """Belt and braces behind the lock: even called directly, a second
        start must not bind over a live listener."""
        manager, adapter = self._manager(monkeypatch)
        manager._hid_servers[adapter.bd_addr] = object()

        result = await manager._start_hid(adapter, create_profile("generic"))

        assert result is None


class _FakeAdapterProxy:
    """A BlueZ Adapter1 that behaves like the real one on a no-op write.

    Setting a property to the value it already holds is **rejected** -- and
    with an empty `DBusError('')`, so the message tells you nothing. Measured
    against BlueZ 5.82 on hci2 and hci4:

        set_connectable(True)  -> DBusError('')      (already true)
        set_pairable(True)     -> OK                 (was false)
        set_discoverable(True) -> OK                 (was false)
    """

    def __init__(self, **state):
        self.state = {
            "alias": "RBGC Gamepad 2",
            "connectable": True,
            "pairable": False,
            "discoverable": False,
            "discoverable_timeout": 120,
            "pairable_timeout": 120,
            **state,
        }
        self.writes: list[tuple[str, object]] = []

    def __getattr__(self, name):
        if name.startswith("get_"):
            prop = name[4:]

            async def getter():
                return self.state[prop]

            return getter
        if name.startswith("set_"):
            prop = name[4:]

            async def setter(value):
                self.writes.append((prop, value))
                if self.state[prop] == value:
                    raise _EmptyDBusError()
                self.state[prop] = value

            return setter
        raise AttributeError(name)


class _EmptyDBusError(Exception):
    """Stands in for dbus-next's `DBusError('')`.

    Deliberately a plain exception rather than the real class, so these tests
    run on a dev machine without dbus-next -- which is Windows, where most of
    this work happens. `set_properties` catches `Exception`, so the type is not
    what is under test; the empty message is, since that is what made the
    original failure unreadable.
    """

    def __str__(self) -> str:
        return ""


class TestSetPropertiesSurvivesARejectedNoOp:
    """One rejected write must not cancel the others.

    All six properties shared a single try block, so the first exception
    skipped everything after it. `Connectable` is set first and is normally
    *already* true -- so BlueZ rejected the no-op, and `Pairable` and
    `Discoverable`, the entire point of the call, were never written. Every
    pairing window opened on an already-connectable adapter did nothing, and
    the only clue was `Could not configure hci2 over D-Bus:` with an empty
    message after the colon.
    """

    def _patch(self, monkeypatch, proxy):
        from server.bt import adapter_dbus

        async def fake_connect():
            return object()

        async def fake_interface(bus, hci_name):
            return proxy

        monkeypatch.setattr(adapter_dbus, "_connect", fake_connect)
        monkeypatch.setattr(adapter_dbus, "_adapter_interface", fake_interface)
        # No _disconnect to stub: the system-bus connection is shared across
        # every call in that module now and released once, from
        # AdapterManager.stop(), rather than torn down per property write.

    @pytest.mark.asyncio
    async def test_arming_a_window_on_a_connectable_adapter_works(self, monkeypatch):
        from server.bt import adapter_dbus

        proxy = _FakeAdapterProxy(connectable=True)
        self._patch(monkeypatch, proxy)

        ok = await adapter_dbus.set_properties(
            "hci4", connectable=True, pairable=True, discoverable=True
        )

        assert ok
        assert proxy.state["pairable"] is True, "Pairable never written"
        assert proxy.state["discoverable"] is True, "Discoverable never written"

    @pytest.mark.asyncio
    async def test_a_no_op_write_is_never_attempted(self, monkeypatch):
        from server.bt import adapter_dbus

        proxy = _FakeAdapterProxy(connectable=True)
        self._patch(monkeypatch, proxy)

        await adapter_dbus.set_properties("hci4", connectable=True, pairable=True)

        assert ("connectable", True) not in proxy.writes, (
            "wrote a value the property already held; BlueZ rejects that"
        )

    @pytest.mark.asyncio
    async def test_a_real_change_is_still_written(self, monkeypatch):
        from server.bt import adapter_dbus

        proxy = _FakeAdapterProxy(connectable=False)
        self._patch(monkeypatch, proxy)

        await adapter_dbus.set_properties("hci4", connectable=True)

        assert proxy.state["connectable"] is True

    @pytest.mark.asyncio
    async def test_a_genuine_failure_still_reports_false(self, monkeypatch):
        """Strictness is the point of the return value -- an adapter that
        would not accept Pairable must not look like a successful arm."""
        from server.bt import adapter_dbus

        proxy = _FakeAdapterProxy(pairable=False)

        async def refuse(value):
            raise RuntimeError("bluetoothd said no")

        monkeypatch.setattr(proxy, "set_pairable", refuse, raising=False)
        self._patch(monkeypatch, proxy)

        ok = await adapter_dbus.set_properties("hci4", pairable=True)

        assert ok is False


class TestAnEnabledAdapterIsConnectable:
    """An adapter serving a HID listener must answer pages.

    `Connectable` is the property behind *page scan*. BlueZ only keeps an
    adapter connectable on its own when it has bonded devices that might
    reconnect -- so three already-paired adapters looked fine while a fresh
    one sat with `Connectable=false` and scan enable `0x00`, unable to accept
    any connection. Measured on hci4 against hci0/2/3 (true / `0x02`).

    It is a trap that cannot open itself: the adapter cannot accept a
    connection, so it can never gain the bond that would have made BlueZ keep
    it connectable. All the host ever says is "We didn't get any response from
    the device", which is also what half a dozen unrelated faults say.
    """

    def _manager(self, monkeypatch):
        from server.bt import adapter as adapter_mod
        from server.bt.adapter import AdapterInfo
        from server.config import AdapterConfig

        calls = []

        async def fake_set_properties(hci, **kwargs):
            calls.append((hci, kwargs))
            return True

        manager = _manager()
        adapter = AdapterInfo(
            bd_addr="CC:28:AA:6D:BA:C0", hci_name="hci4", powered=True
        )
        manager._adapters[adapter.bd_addr] = adapter
        manager._config.upsert_adapter(
            AdapterConfig(
                bd_addr=adapter.bd_addr, enabled=True, profile="generic", number=2
            )
        )
        monkeypatch.setattr(adapter_mod, "_bring_up_adapter", lambda a: True)
        monkeypatch.setattr(adapter_mod, "_ensure_pairing_settings", lambda a: None)
        monkeypatch.setattr(adapter_mod, "_set_device_class", lambda a: None)
        monkeypatch.setattr(
            adapter_mod.adapter_dbus, "set_properties", fake_set_properties
        )

        async def fake_start_hid(a, profile):
            manager._hid_servers[a.bd_addr] = SimpleNamespace(
                suspend_reconnect=lambda _s: None,
                set_reconnect_target=lambda _t: None,
            )
            return MockSink()

        monkeypatch.setattr(manager, "_start_hid", fake_start_hid)
        return manager, adapter, calls

    @pytest.mark.asyncio
    async def test_bringing_an_adapter_up_makes_it_connectable(self, monkeypatch):
        manager, _adapter, calls = self._manager(monkeypatch)

        await manager._reconcile_channels()

        assert any(
            kwargs.get("connectable") is True for _hci, kwargs in calls
        ), f"never set Connectable; the radio will not answer pages: {calls}"

    @pytest.mark.asyncio
    async def test_ending_a_pairing_window_leaves_it_connectable(self, monkeypatch):
        """Stopping pairing must not switch page scan off.

        A host that has just bonded reconnects by paging us, so clearing
        Connectable at the end of the window would undo the pairing the window
        existed to create.
        """
        manager, adapter, calls = self._manager(monkeypatch)
        await manager._reconcile_channels()
        calls.clear()

        await manager.set_pairable(adapter.bd_addr, False)

        cleared = [
            kwargs for _hci, kwargs in calls if kwargs.get("connectable") is False
        ]
        assert not cleared, f"stopping pairing switched page scan off: {cleared}"


class TestEnableIsPersisted:
    """Enabling an adapter must survive a restart.

    ``set_enabled`` updated the in-memory config and reconciled, but never
    wrote the file. The operator enabled an adapter, watched it come up, and
    found it disabled again after the next restart with nothing to explain it.
    """

    def _manager(self, tmp_path, monkeypatch):
        from server.bt import adapter as adapter_mod
        from server.bt.adapter import AdapterInfo, AdapterManager
        from server.config import ServerConfig
        from server.router import Router

        path = tmp_path / "server.json"
        manager = AdapterManager(Router(), ServerConfig(), config_path=path)
        adapter = AdapterInfo(
            bd_addr="CC:28:AA:6D:BA:C0", hci_name="hci4", powered=True
        )
        adapter.enabled = False
        manager._adapters[adapter.bd_addr] = adapter
        monkeypatch.setattr(manager, "_reconcile_channels", _noop_async)
        monkeypatch.setattr(adapter_mod.adapter_dbus, "set_properties", _always_true)
        return manager, adapter, path

    @pytest.mark.asyncio
    async def test_enabling_writes_the_file(self, tmp_path, monkeypatch):
        from server import config as server_config

        manager, adapter, path = self._manager(tmp_path, monkeypatch)

        ok, _ = await manager.set_enabled(adapter.bd_addr, True)
        assert ok

        reloaded = server_config.load(path)
        entry = reloaded.adapter(adapter.bd_addr)

        assert entry is not None and entry.enabled is True

    @pytest.mark.asyncio
    async def test_disabling_writes_the_file_too(self, tmp_path, monkeypatch):
        from server import config as server_config

        manager, adapter, path = self._manager(tmp_path, monkeypatch)
        await manager.set_enabled(adapter.bd_addr, True)

        await manager.set_enabled(adapter.bd_addr, False)

        entry = server_config.load(path).adapter(adapter.bd_addr)
        assert entry is not None and entry.enabled is False

    @pytest.mark.asyncio
    async def test_the_reconnect_target_is_not_lost(self, tmp_path, monkeypatch):
        """Toggling an adapter must not forget the console it paired with."""
        from server import config as server_config
        from server.config import AdapterConfig

        manager, adapter, path = self._manager(tmp_path, monkeypatch)
        manager._config.upsert_adapter(
            AdapterConfig(
                bd_addr=adapter.bd_addr, enabled=False,
                profile="generic", paired_target="68:EC:C5:82:6F:2B",
            )
        )

        await manager.set_enabled(adapter.bd_addr, True)

        entry = server_config.load(path).adapter(adapter.bd_addr)
        assert entry.paired_target == "68:EC:C5:82:6F:2B"


class TestExpiredPairingWindowStopsAdvertising:
    """BlueZ ends a discoverable window in MGMT but never writes scan enable
    back down: the controller stays at 0x03, still answering inquiries, so the
    gamepad keeps appearing in the host's *Add a device* list long after the
    window closed. Nothing in MGMT or D-Bus reports this.

    Re-asserting Discoverable=False through bluetoothd is what actually
    rewrites scan enable -- the same call the operator's "stop pairing" button
    makes. These tests pin down that the timeout now behaves like the button.
    """

    def _armed(self, manager, *, remaining_ns: int):
        from common.timing import now_ns
        from server.bt.adapter import AdapterInfo

        adapter = AdapterInfo(bd_addr="DC:A6:32:B9:6A:88", hci_name="hci0", enabled=True)
        adapter.pairing_until_ns = now_ns() + remaining_ns
        manager._adapters[adapter.bd_addr] = adapter
        return adapter

    def _record_calls(self, monkeypatch):
        from server.bt import adapter_dbus

        calls: list[dict] = []

        async def fake_set_properties(hci_name, **kwargs):
            calls.append({"hci": hci_name, **kwargs})
            return True

        monkeypatch.setattr(adapter_dbus, "set_properties", fake_set_properties)
        return calls

    @pytest.mark.asyncio
    async def test_an_expired_window_clears_discoverable(self, monkeypatch):
        manager = _manager()
        adapter = self._armed(manager, remaining_ns=-1)      # already past
        calls = self._record_calls(monkeypatch)

        changed = await manager._expire_pairing_windows()

        assert changed is True
        assert calls, "nothing was written to the adapter"
        assert calls[0]["discoverable"] is False
        assert adapter.pairing_until_ns == 0

    @pytest.mark.asyncio
    async def test_page_scan_is_re_asserted(self, monkeypatch):
        """A host that bonded during the window reconnects by paging us.

        This used to assert the opposite -- that Connectable was left alone --
        on the reasoning that we should never clear it here. We never did.
        **BlueZ does it for us**: it only keeps an adapter connectable on its
        own while it holds a bond that might reconnect, so a window ending on
        an adapter that did not manage to bond drops page scan to 0x00.

        Measured live on hci3: stop pairing with no bonds, and the radio reads
        scan enable 0x00 with nothing in any log to say so. The adapter is then
        unreachable, and it is the trap that cannot open itself -- it cannot
        accept a connection, so it can never gain the bond that would have kept
        it connectable.

        Not clearing it is not enough. It has to be re-asserted.
        """
        manager = _manager()
        self._armed(manager, remaining_ns=-1)
        calls = self._record_calls(monkeypatch)

        await manager._expire_pairing_windows()

        assert calls[0].get("connectable") is True

    @pytest.mark.asyncio
    async def test_a_disabled_adapter_is_not_made_connectable(self, monkeypatch):
        """The one case where silence is right.

        Disabling an adapter is the operator saying "stop using this radio",
        and _quiet_adapter clears Connectable deliberately. A window expiring
        afterwards must not turn page scan back on behind their back.
        """
        manager = _manager()
        adapter = self._armed(manager, remaining_ns=-1)
        adapter.enabled = False
        calls = self._record_calls(monkeypatch)

        await manager._expire_pairing_windows()

        assert calls[0].get("connectable") is None

    @pytest.mark.asyncio
    async def test_a_live_window_is_untouched(self, monkeypatch):
        manager = _manager()
        adapter = self._armed(manager, remaining_ns=60 * 1_000_000_000)
        calls = self._record_calls(monkeypatch)

        changed = await manager._expire_pairing_windows()

        assert changed is False
        assert calls == []
        assert adapter.pairing_until_ns != 0

    @pytest.mark.asyncio
    async def test_an_adapter_that_never_paired_is_untouched(self, monkeypatch):
        manager = _manager()
        adapter = self._armed(manager, remaining_ns=-1)
        adapter.pairing_until_ns = 0
        calls = self._record_calls(monkeypatch)

        assert await manager._expire_pairing_windows() is False
        assert calls == []

    @pytest.mark.asyncio
    async def test_it_only_fires_once_per_window(self, monkeypatch):
        """The reconcile pass runs every 10 s; re-clearing forever is noise."""
        manager = _manager()
        self._armed(manager, remaining_ns=-1)
        calls = self._record_calls(monkeypatch)

        await manager._expire_pairing_windows()
        await manager._expire_pairing_windows()
        await manager._expire_pairing_windows()

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_a_failed_write_still_clears_the_window(self, monkeypatch):
        """The window is over either way; leaving it armed would retry forever."""
        from server.bt import adapter_dbus

        manager = _manager()
        adapter = self._armed(manager, remaining_ns=-1)

        async def failing(hci_name, **kwargs):
            return False

        monkeypatch.setattr(adapter_dbus, "set_properties", failing)

        await manager._expire_pairing_windows()

        assert adapter.pairing_until_ns == 0

    @pytest.mark.asyncio
    async def test_reconnects_resume_after_the_window(self, monkeypatch):
        """Outgoing pages were suspended only for the window's duration."""
        manager = _manager()
        adapter = self._armed(manager, remaining_ns=-1)
        self._record_calls(monkeypatch)

        resumed: list[int] = []

        class _Server:
            def suspend_reconnect(self, seconds):
                resumed.append(seconds)

        manager._hid_servers[adapter.bd_addr] = _Server()

        await manager._expire_pairing_windows()

        assert resumed == [0]


class TestClassOfDeviceRevertIsReported:
    """hciconfig writes raw HCI, below MGMT, and bluetoothd recomputes the
    class through MGMT whenever it likes -- so the write reverts with no error
    from anything. A console filters its pairing list on this value, so the
    only symptom is never being offered as a controller, which reads as a
    pairing fault and sends people looking at SSP and bonds.

    The write cannot be made to stick from here (the fix is bluetoothd's own
    config, a system file we should not edit on the operator's behalf), so the
    requirement is simply that it stops being silent.
    """

    def _adapter(self):
        from server.bt.adapter import AdapterInfo

        return AdapterInfo(bd_addr="DC:A6:32:B9:6A:88", hci_name="hci0")

    def _patch(self, monkeypatch, *, readback: str, write_rc: int = 0):
        from server.bt import adapter as adapter_mod

        monkeypatch.setattr(adapter_mod.shutil, "which", lambda name: "/usr/bin/hciconfig")
        adapter_mod._class_warned.clear()

        def fake_run(cmd, timeout=5.0):
            if "class" in cmd and len(cmd) > 3:      # the write
                return write_rc, ""
            return 0, readback                        # the read-back

        monkeypatch.setattr(adapter_mod, "_run", fake_run)

    def test_a_reverted_class_is_warned_about(self, monkeypatch, caplog):
        from server.bt import adapter as adapter_mod

        # bluetoothd put back a generic computer class.
        self._patch(monkeypatch, readback="hci0:\n\tClass: 0x0c0104\n")

        with caplog.at_level("WARNING"):
            adapter_mod._set_device_class(self._adapter())

        assert "reverted" in caplog.text
        assert "main.conf" in caplog.text, "the warning must say how to fix it"
        assert "0x002508" in caplog.text

    def test_a_class_that_stuck_says_nothing(self, monkeypatch, caplog):
        from server.bt import adapter as adapter_mod

        self._patch(monkeypatch, readback="hci0:\n\tClass: 0x002508\n")

        with caplog.at_level("WARNING"):
            adapter_mod._set_device_class(self._adapter())

        assert caplog.text == ""

    def test_it_warns_only_once(self, monkeypatch, caplog):
        """This runs every reconcile; the remedy is a one-time file edit."""
        from server.bt import adapter as adapter_mod

        self._patch(monkeypatch, readback="hci0:\n\tClass: 0x0c0104\n")

        with caplog.at_level("WARNING"):
            for _ in range(5):
                adapter_mod._set_device_class(self._adapter())

        assert caplog.text.count("reverted") == 1

    def test_it_warns_again_after_recovering(self, monkeypatch, caplog):
        from server.bt import adapter as adapter_mod

        self._patch(monkeypatch, readback="hci0:\n\tClass: 0x0c0104\n")
        with caplog.at_level("WARNING"):
            adapter_mod._set_device_class(self._adapter())

        # It sticks for a while...
        self._patch(monkeypatch, readback="hci0:\n\tClass: 0x002508\n")
        adapter_mod._set_device_class(self._adapter())

        # ...then reverts again, which is worth hearing about.
        self._patch(monkeypatch, readback="hci0:\n\tClass: 0x0c0104\n")
        caplog.clear()
        with caplog.at_level("WARNING"):
            adapter_mod._set_device_class(self._adapter())

        assert "reverted" in caplog.text

    def test_an_unreadable_class_is_not_treated_as_a_revert(self, monkeypatch, caplog):
        """Absence of evidence is not evidence of a wrong class."""
        from server.bt import adapter as adapter_mod

        self._patch(monkeypatch, readback="hci0:\n\tType: Primary\n")

        with caplog.at_level("WARNING"):
            adapter_mod._set_device_class(self._adapter())

        assert caplog.text == ""

    def test_a_failed_write_does_not_claim_a_revert(self, monkeypatch, caplog):
        from server.bt import adapter as adapter_mod

        self._patch(monkeypatch, readback="", write_rc=1)

        with caplog.at_level("WARNING"):
            adapter_mod._set_device_class(self._adapter())

        assert "reverted" not in caplog.text


class TestTheGattClientMustBeDisabledForBLE:
    """bluetoothd acting as a GATT client costs the console its input.

    We are a peripheral. bluetoothd otherwise creates a GATT client for every
    LE connection and sends an Exchange MTU Request the moment encryption
    completes. An Analogue 3D never answers it -- measured 23 sent, 0 answered
    over 15 minutes -- and ATT closes the bearer 30 seconds after an
    unanswered transaction. Every link died 34.3 s after Encryption Change,
    with notifications stopping at exactly 30.000 s.

    The failure is invisible from every counter we have: the console pairs,
    plays, and stops, and the GUI shows a healthy link right up to the drop.
    So the setting is checked at startup and the warning names the symptom.
    """

    def test_a_disabled_setting_is_recognised(self):
        from server.bt.adapter import _config_bool

        text = "[General]\nReverseServiceDiscovery = false\n"
        assert _config_bool(text, "General", "ReverseServiceDiscovery") is True

    def test_absent_is_not_the_same_as_false(self):
        """bluetoothd defaults it to true, so absent means the client is ON --
        which is exactly the case to warn about. Returning False here would
        make the checker silently approve a broken host."""
        from server.bt.adapter import _config_bool

        assert _config_bool("[General]\nName = x\n", "General",
                            "ReverseServiceDiscovery") is None

    def test_a_commented_line_does_not_count(self):
        from server.bt.adapter import _config_bool

        text = "[General]\n#ReverseServiceDiscovery = false\n"
        assert _config_bool(text, "General", "ReverseServiceDiscovery") is None

    def test_true_is_reported_as_not_disabled(self):
        from server.bt.adapter import _config_bool

        text = "[General]\nReverseServiceDiscovery = true\n"
        assert _config_bool(text, "General", "ReverseServiceDiscovery") is False

    def test_sections_are_honoured(self):
        """main.conf repeats key names across sections -- Client exists under
        both [GATT] and [CSIS] -- so a section-blind search reads the wrong
        one."""
        from server.bt.adapter import _config_bool

        text = "[GATT]\nReverseServiceDiscovery = false\n"
        assert _config_bool(text, "General", "ReverseServiceDiscovery") is None

    def test_duplicate_keys_across_sections_do_not_break_it(self):
        """configparser rejects this file outright, which is why the parser is
        hand-rolled."""
        from server.bt.adapter import _config_bool

        text = "[GATT]\nClient = false\n\n[CSIS]\nClient = true\n"
        assert _config_bool(text, "GATT", "Client") is True
        assert _config_bool(text, "CSIS", "Client") is False

    def test_whitespace_and_case_are_tolerated(self):
        from server.bt.adapter import _config_bool

        text = "[general]\n  reverseservicediscovery   =   FALSE  \n"
        assert _config_bool(text, "General", "ReverseServiceDiscovery") is True

    def test_an_unreadable_file_says_nothing_rather_than_guessing(self):
        """A container or an unusual distribution may not have the file. A
        warning about a setting we cannot see is noise."""
        from server.bt.adapter import _reverse_discovery_disabled

        assert _reverse_discovery_disabled("/nonexistent/main.conf") is None

    def test_the_warning_names_the_symptom(self):
        """The setting name alone is useless to somebody watching a console
        drop out; the searchable fact is the ~35 second interval."""
        from server.bt.adapter import _REVERSE_DISCOVERY_WARNING

        assert "35 second" in _REVERSE_DISCOVERY_WARNING
        assert "ReverseServiceDiscovery" in _REVERSE_DISCOVERY_WARNING
        assert "main.conf" in _REVERSE_DISCOVERY_WARNING

    def test_it_is_checked_only_on_the_ble_path(self):
        """On Classic the option merely disables reverse SDP, which upstream
        describes as needed for qualification and which costs us nothing."""
        import inspect

        from server.bt.adapter import AdapterManager

        assert "_check_reverse_discovery" in inspect.getsource(
            AdapterManager._start_ble)
        assert "_check_reverse_discovery" not in inspect.getsource(
            AdapterManager._start_hid)

    def test_it_surfaces_in_the_gui(self):
        from server.bt.state import AdapterState

        adapter = AdapterState(bd_addr="AA:BB:CC:DD:EE:FF", hci_name="hci0")
        adapter.host_config_warning = "something is wrong"
        assert "something is wrong" in adapter.health()
        assert "something is wrong" in adapter.snapshot()["health"]

    def test_the_packaged_snippet_carries_both_settings(self):
        """So the shipped file and the checker cannot drift apart."""
        import pathlib

        text = (
            pathlib.Path(__file__).resolve().parent.parent
            / "packaging" / "bluetooth-main.conf.snippet"
        ).read_text(encoding="utf-8")
        from server.bt.adapter import _config_bool

        assert _config_bool(text, "General", "ReverseServiceDiscovery") is True
        assert _config_bool(text, "GATT", "Client") is True

"""Bluetooth adapter setup: SDP registration options and pairing-state checks.

Every test here is a regression for a defect that produced **no error anywhere**.
That is the common thread: BlueZ accepts a wrong `RegisterProfile` option, a
grep against the wrong line always matches, and a legacy PIN fallback logged at
info level. Each looked healthy right up until pairing quietly misbehaved.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
            ({"powered", "ssp"}, False),                               # bondable missing
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
            (0, ""), (0, ""), (0, ""),    # the three corrections
            (0, BTMGMT_INFO_HEALTHY),     # re-read: fixed
        ]
        with patch("server.bt.adapter.shutil.which", return_value="/usr/bin/btmgmt"), \
             patch("server.bt.adapter._run", side_effect=outputs) as run:
            _ensure_pairing_settings(self._adapter())

        commands = [" ".join(c.args[0]) for c in run.call_args_list]
        assert any("linksec off" in c for c in commands)
        assert any("ssp on" in c for c in commands)
        assert any("bondable on" in c for c in commands)

    def test_uncorrectable_adapter_logs_an_error(self, caplog):
        from server.bt.adapter import _ensure_pairing_settings

        outputs = [
            (0, BTMGMT_INFO_NO_SSP),
            (1, "Failed"), (1, "Failed"), (1, "Failed"),
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

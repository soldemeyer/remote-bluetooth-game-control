"""The Bluetooth management socket.

The socket itself needs Linux and a controller, so what is covered here is the
wire format and the policy around it. Both matter more than they look:

* A parser offset that is wrong produces plausible nonsense -- a valid-looking
  address, a settings bitmap that decodes to real flag names -- rather than an
  error. The captured bytes below are from a real Pi 5 running BlueZ 5.82.
* The read-only allowlist is the thing standing between this module and the
  two-clients-writing-one-setting fight that has bitten this project before.
"""

from __future__ import annotations

import struct

import pytest

from server.bt import mgmt


def _read_info_reply(
    addr: bytes = bytes.fromhex("C0BA6DAA28CC"),
    settings: int = 0x00020AC1,
    device_class: int = 0x002508,
    name: bytes = b"controller-server",
) -> bytes:
    """A READ_INFO reply in the layout the kernel actually sends."""
    return (
        addr
        + bytes([0x0C])                                  # bluetooth version
        + struct.pack("<H", 0x000F)                      # manufacturer
        + struct.pack("<II", 0x0003FFFF, settings)       # supported, current
        + device_class.to_bytes(3, "little")
        + name.ljust(249, b"\x00")
        + b"\x00" * 11                                   # short name
    )


class TestReadInfoParsing:
    """Captured from hci4 on the reference Pi: CC:28:AA:6D:BA:C0, 0x00020AC1."""

    def test_address_is_decoded_little_endian(self):
        info = mgmt.parse_read_info(_read_info_reply())
        assert info is not None
        assert info.bd_addr == "CC:28:AA:6D:BA:C0"

    def test_class_of_device_is_decoded(self):
        info = mgmt.parse_read_info(_read_info_reply())
        assert info.device_class == 0x002508

    def test_name_stops_at_the_first_nul(self):
        info = mgmt.parse_read_info(_read_info_reply())
        assert info.name == "controller-server"

    def test_a_truncated_reply_is_rejected(self):
        # Guessing at a short reply would invent an address and a settings
        # bitmap that both look entirely reasonable.
        assert mgmt.parse_read_info(b"\x00" * 12) is None


class TestSettingsDecoding:
    """0x00020AC1 is what the reference hardware reports for a working adapter."""

    def test_the_reference_bitmap_decodes_as_measured(self):
        assert mgmt.describe_settings(0x00020AC1) == (
            "powered ssp br/edr le secure-conn"
        )

    def test_that_adapter_is_not_connectable(self):
        # Two of four adapters on the reference Pi are in exactly this state:
        # powered and healthy-looking, with page scan off, so no host can reach
        # them at all.
        info = mgmt.parse_read_info(_read_info_reply(settings=0x00020AC1))
        assert info.powered is True
        assert info.connectable is False

    def test_connectable_is_seen_when_present(self):
        info = mgmt.parse_read_info(_read_info_reply(settings=0x00020AC3))
        assert info.connectable is True

    def test_link_security_is_surfaced(self):
        # Must be off: with it on, pairing degrades to the legacy PIN flow and
        # never reaches the SSP exchange.
        clean = mgmt.parse_read_info(_read_info_reply(settings=0x00020AC1))
        dirty = mgmt.parse_read_info(_read_info_reply(settings=0x00020AE1))
        assert clean.link_security is False
        assert dirty.link_security is True

    def test_bondable_off_is_the_correct_resting_state(self):
        # Pairable is held false outside a pairing window, so an adapter with
        # no bondable bit is healthy, not broken. Requiring it is what once
        # made a check that could only ever fail.
        info = mgmt.parse_read_info(_read_info_reply(settings=0x00020AC1))
        assert info.bondable is False


class TestFraming:
    def test_a_header_is_split_into_event_index_and_params(self):
        data = struct.pack("<HHH", mgmt.EV_NEW_SETTINGS, 3, 4) + b"\xc1\x0a\x02\x00"
        assert mgmt.parse_header(data) == (mgmt.EV_NEW_SETTINGS, 3, b"\xc1\x0a\x02\x00")

    def test_a_runt_is_rejected(self):
        assert mgmt.parse_header(b"\x01\x00") is None

    def test_a_truncated_body_is_rejected(self):
        # Header claims eight bytes, two follow.
        assert mgmt.parse_header(struct.pack("<HHH", 6, 3, 8) + b"\x01\x02") is None

    def test_index_list_is_decoded(self):
        # The reference Pi reports [4, 3, 2, 0] -- note it is not sorted, and
        # not in hciX order.
        payload = struct.pack("<H", 4) + struct.pack("<HHHH", 4, 3, 2, 0)
        assert mgmt.parse_index_list(payload) == [4, 3, 2, 0]

    def test_index_list_does_not_read_past_the_buffer(self):
        # A count larger than the data present must not index off the end.
        payload = struct.pack("<H", 9) + struct.pack("<HH", 4, 3)
        assert mgmt.parse_index_list(payload) == [4, 3]


class TestWritesAreRefused:
    """bluetoothd owns adapter state; we are a second client that only reads.

    Two MGMT clients writing the same setting is the desynchronisation behind
    several of this project's longest debugging sessions. The allowlist makes
    adding a write an explicit decision rather than something that merely was
    not forbidden yet.
    """

    def test_a_settings_write_is_rejected(self):
        sock = mgmt.MGMTSocket()
        with pytest.raises(mgmt.MGMTError, match="allowlist"):
            sock.command(0x0007)            # SET_POWERED

    def test_the_refusal_names_the_alternative(self):
        sock = mgmt.MGMTSocket()
        with pytest.raises(mgmt.MGMTError, match="org.bluez.Adapter1"):
            sock.command(0x0009)            # SET_CONNECTABLE

    def test_reads_are_allowed(self):
        # Rejected for being closed, not for being forbidden.
        sock = mgmt.MGMTSocket()
        with pytest.raises(mgmt.MGMTError, match="not open"):
            sock.command(mgmt.OP_READ_INDEX_LIST)


class TestAdvertisingIsAllowedAndSettingsAreNot:
    """The one place this module writes, and why it is not a fudge.

    The rule is "do not write adapter **settings**" -- shared state bluetoothd
    owns, where two writers desynchronise and the symptom appears much later.
    An advertising instance is not that: the kernel records which socket added
    it and removes it when that socket closes, so it is a per-client resource
    the kernel arbitrates rather than a setting anyone else can observe.

    It is allowed at all because bluetoothd's own LEAdvertisingManager1 cannot
    publish an advertisement on this platform -- it takes the extended path and
    the kernel rejects the data with Invalid Parameters, measured on both the
    built-in adapter and the dongles, and with a minimal advertisement.
    """

    def test_add_advertising_is_allowed(self):
        assert mgmt.OP_ADD_ADVERTISING in mgmt._ALLOWED_OPCODES

    def test_remove_advertising_is_allowed(self):
        assert mgmt.OP_REMOVE_ADVERTISING in mgmt._ALLOWED_OPCODES

    def test_no_settings_opcode_crept_onto_the_list(self):
        # The ones that would actually desynchronise us from bluetoothd:
        # SET_POWERED, SET_CONNECTABLE, SET_DISCOVERABLE, SET_BONDABLE,
        # SET_LINK_SECURITY, SET_SSP, SET_LOCAL_NAME, SET_DEV_CLASS.
        for opcode in (0x0005, 0x0006, 0x0007, 0x0008, 0x0009,
                       0x000A, 0x000B, 0x000C, 0x000D, 0x000F, 0x001F):
            assert opcode not in mgmt._ALLOWED_OPCODES, hex(opcode)

    def test_the_read_only_set_is_still_only_reads(self):
        assert mgmt._READ_ONLY_OPCODES == {
            mgmt.OP_READ_VERSION, mgmt.OP_READ_INDEX_LIST, mgmt.OP_READ_INFO
        }

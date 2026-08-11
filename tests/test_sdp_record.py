"""HID SDP service record structure.

These exist because a malformed record fails **silently and confusingly**: the
host reads it, does not recognise a HID device, never attempts PSM 17/19, and
falls back to generic pairing -- which the user sees as an unexplained "enter
the PIN" prompt with no error anywhere on either side.

Found on hardware via btmon: Windows connected to PSM 1 (SDP), read the record,
and never touched the HID PSMs at all.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

from server.bt.profiles import create_profile
from server.bt.sdp import PSM_CONTROL, PSM_INTERRUPT, build_hid_record

# Attribute ids defined by the Bluetooth HID profile. Mapping them by name
# here is the point: the original bug was using 0x020b (HIDProfileVersion) as
# if it were the supervision timeout.
HID_PARSER_VERSION = 0x0201
HID_DEVICE_SUBCLASS = 0x0202
HID_COUNTRY_CODE = 0x0203
HID_VIRTUAL_CABLE = 0x0204
HID_RECONNECT_INITIATE = 0x0205
HID_DESCRIPTOR_LIST = 0x0206
HID_LANGID_BASE_LIST = 0x0207
HID_PROFILE_VERSION = 0x020B
HID_SUPERVISION_TIMEOUT = 0x020C
HID_BOOT_DEVICE = 0x020E


@pytest.fixture
def record() -> str:
    profile = create_profile("generic")
    descriptor = profile.descriptor
    return build_hid_record(
        descriptor.device_name,
        descriptor.report_descriptor,
        descriptor.vendor_id,
        descriptor.product_id,
    )


@pytest.fixture
def attribute_ids(record: str) -> list[int]:
    return [int(m, 16) for m in re.findall(r'<attribute id="(0x[0-9a-fA-F]+)"', record)]


class TestStructure:
    def test_record_is_well_formed_xml(self, record):
        """BlueZ parses this string; malformed XML is rejected outright."""
        ET.fromstring(record.split("?>", 1)[1] if "?>" in record else record)

    def test_no_duplicate_attribute_ids(self, attribute_ids):
        """Regression: 0x0201 was declared twice."""
        duplicates = {a for a in attribute_ids if attribute_ids.count(a) > 1}
        assert not duplicates, f"duplicated attribute ids: {[hex(a) for a in duplicates]}"

    def test_attribute_ids_ascend(self, attribute_ids):
        """SDP requires ascending attribute ids.

        Regression: stray 0x0200/0x0201 entries sat after 0x0210, and Windows
        discarded the record rather than reporting an error.
        """
        assert attribute_ids == sorted(attribute_ids), (
            "attribute ids are out of order: "
            f"{[hex(a) for a in attribute_ids]}"
        )


class TestMandatoryAttributes:
    @pytest.mark.parametrize(
        "attribute_id,name",
        [
            (HID_PARSER_VERSION, "HIDParserVersion"),
            (HID_DEVICE_SUBCLASS, "HIDDeviceSubclass"),
            (HID_COUNTRY_CODE, "HIDCountryCode"),
            (HID_VIRTUAL_CABLE, "HIDVirtualCable"),
            (HID_RECONNECT_INITIATE, "HIDReconnectInitiate"),
            (HID_DESCRIPTOR_LIST, "HIDDescriptorList"),
            (HID_LANGID_BASE_LIST, "HIDLANGIDBaseList"),
            (HID_PROFILE_VERSION, "HIDProfileVersion"),
            (HID_BOOT_DEVICE, "HIDBootDevice"),
        ],
    )
    def test_present(self, attribute_ids, attribute_id, name):
        assert attribute_id in attribute_ids, f"{name} ({hex(attribute_id)}) missing"

    def test_profile_version_is_a_plausible_version(self, record):
        """Regression: this held 0x0c80 -- a supervision timeout in the slot
        that declares which HID profile version we speak. A host reading
        "profile version 12.128" discards the record."""
        match = re.search(
            rf'<attribute id="0x{HID_PROFILE_VERSION:04x}"[^>]*>\s*'
            r'<uint16 value="(0x[0-9a-fA-F]+)"',
            record,
        )
        assert match, "HIDProfileVersion not found"

        version = int(match.group(1), 16)
        major, minor = version >> 8, version & 0xFF
        assert 1 <= major <= 2, f"implausible HID profile major version {major}"
        assert minor <= 0x99, f"implausible HID profile minor version {minor}"

    def test_supervision_timeout_has_its_own_id(self, attribute_ids):
        assert HID_SUPERVISION_TIMEOUT in attribute_ids, (
            "HIDSupervisionTimeout (0x020c) missing -- it was previously "
            "written into 0x020b, which is HIDProfileVersion"
        )

    def test_device_subclass_matches_a_gamepad(self, record):
        """Must agree with the class of device, or hosts categorise us wrongly."""
        match = re.search(
            rf'<attribute id="0x{HID_DEVICE_SUBCLASS:04x}"[^>]*>\s*'
            r'<uint8 value="(0x[0-9a-fA-F]+)"',
            record,
        )
        assert match
        subclass = int(match.group(1), 16)

        # Bits 6-7 select keyboard/pointing; both clear means neither.
        assert not subclass & 0xC0, "subclass claims keyboard or pointing device"
        # Bits 2-5 are the device type; 0b0010 is gamepad.
        assert (subclass >> 2) & 0x0F == 0b0010, f"not a gamepad subclass: {subclass:#04x}"

    def test_not_advertised_as_a_boot_device(self, record):
        """Boot protocol is for keyboards and mice only."""
        match = re.search(
            rf'<attribute id="0x{HID_BOOT_DEVICE:04x}"[^>]*>\s*'
            r'<boolean value="(true|false)"',
            record,
        )
        assert match and match.group(1) == "false"


class TestProtocolDescriptors:
    def test_advertises_the_control_psm(self, record):
        assert f'value="0x{PSM_CONTROL:04x}"' in record

    def test_advertises_the_interrupt_psm(self, record):
        assert f'value="0x{PSM_INTERRUPT:04x}"' in record

    def test_service_class_is_hid(self, record):
        """0x1124 = HumanInterfaceDeviceService."""
        assert 'uuid value="0x1124"' in record

    def test_report_descriptor_is_embedded(self, record):
        profile = create_profile("generic")
        assert profile.descriptor.report_descriptor.hex() in record


class TestNameHandling:
    def test_device_name_appears(self):
        record = build_hid_record("RBGC Gamepad 3", b"\x05\x01", 0x1D6B, 0x0246)
        assert "RBGC Gamepad 3" in record

    def test_name_is_xml_escaped(self):
        """An operator label with an ampersand would otherwise produce invalid
        XML and BlueZ would reject the registration."""
        record = build_hid_record("Pad & <Test>", b"\x05\x01", 0x1D6B, 0x0246)

        assert "&amp;" in record
        assert "&lt;Test&gt;" in record
        ET.fromstring(record.split("?>", 1)[1])


def test_switch_profile_record_is_also_valid():
    """Both profiles go through the same builder, so both must validate."""
    profile = create_profile("switch_pro")
    descriptor = profile.descriptor
    record = build_hid_record(
        descriptor.device_name,
        descriptor.report_descriptor,
        descriptor.vendor_id,
        descriptor.product_id,
    )

    ids = [int(m, 16) for m in re.findall(r'<attribute id="(0x[0-9a-fA-F]+)"', record)]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))

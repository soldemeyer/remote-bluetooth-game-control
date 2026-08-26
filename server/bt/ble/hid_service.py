"""The D-Bus objects that publish a HOGP gamepad.

DO NOT add ``from __future__ import annotations`` -- see gatt.py.

The wire format constants and payload helpers live in ``hogp.py``, which is
stdlib-only. This module is the half that needs dbus-next, and it does nothing
but assemble the object tree.
"""

import logging

from server.bt.ble import hogp
from server.bt.ble.gatt import Application, Characteristic, Descriptor, Service

log = logging.getLogger(__name__)


def build_application(
    root,
    report_descriptor,
    vendor_id,
    product_id,
    *,
    version=0x0100,
    pnp_source=hogp.PNP_SOURCE_USB,
    input_report_id=0x01,
    manufacturer="RBGC",
    model_number="RBGC Gamepad",
    serial_number="000000000000",
    firmware_revision="1.00",
    battery_level=100,
    on_control_point=None,
    on_protocol_mode=None,
    on_output_report=None,
    on_vendor_write=None,
):
    """Assemble the whole GATT tree for one adapter.

    Returns ``(application, input_report_characteristic)``. The caller notifies
    on that characteristic to deliver input; everything else is static or
    handled by the callbacks.

    The tree must be complete before ``RegisterApplication``: BlueZ reads it
    once via ``GetManagedObjects`` and never looks again.
    """
    app = Application(root)

    hid = app.add_service(Service(f"{root}/service0", hogp.HID_SERVICE_UUID))

    # Report Map: the same HID report descriptor the Classic side puts in its
    # SDP record. The report *layout* is identical between the two transports;
    # only how it is carried differs.
    report_map = Characteristic(
        f"{hid.path}/char0", hogp.REPORT_MAP_UUID, hid.path,
        ["read"], value=report_descriptor,
    )

    hid_info = Characteristic(
        f"{hid.path}/char1", hogp.HID_INFORMATION_UUID, hid.path,
        ["read"], value=hogp.HID_INFORMATION,
    )

    # Write-without-response by specification. Declaring plain "write" makes a
    # host wait for a response that never comes.
    control_point = Characteristic(
        f"{hid.path}/char2", hogp.HID_CONTROL_POINT_UUID, hid.path,
        ["write-without-response"], on_write=on_control_point,
    )

    protocol_mode = Characteristic(
        f"{hid.path}/char3", hogp.PROTOCOL_MODE_UUID, hid.path,
        ["read", "write-without-response"],
        value=bytes([hogp.PROTOCOL_MODE_REPORT]),
        on_write=on_protocol_mode,
    )

    # The input report. "encrypt-read" rather than plain "read": HOGP requires
    # an encrypted link for report access, and a host that finds it readable
    # unencrypted may refuse to treat us as a HID device at all.
    input_report = Characteristic(
        f"{hid.path}/char4", hogp.REPORT_UUID, hid.path,
        ["read", "notify", "encrypt-read"],
    )
    input_report.descriptors.append(
        Descriptor(
            f"{input_report.path}/desc0", hogp.REPORT_REFERENCE_UUID,
            input_report.path,
            bytes([input_report_id or 0x00, hogp.REPORT_TYPE_INPUT]),
        )
    )

    # An output report, so a host can drive rumble and player LEDs. Optional
    # for the host, but its absence means those can never arrive.
    output_report = Characteristic(
        f"{hid.path}/char5", hogp.REPORT_UUID, hid.path,
        ["read", "write", "write-without-response"],
        on_write=on_output_report,
    )
    output_report.descriptors.append(
        Descriptor(
            f"{output_report.path}/desc0", hogp.REPORT_REFERENCE_UUID,
            output_report.path,
            bytes([input_report_id or 0x00, hogp.REPORT_TYPE_OUTPUT]),
        )
    )

    hid.characteristics += [
        report_map, hid_info, control_point, protocol_mode,
        input_report, output_report,
    ]

    # Device Information: the BLE counterpart of the Classic DeviceID record,
    # and what a console checks when it only pairs with pads it recognises.
    #
    # All five characteristics, in the order the real pad lists them. A console
    # that requires one we omit does not say so -- see the note on
    # MODEL_NUMBER_UUID for what an Analogue 3D does instead, which is to read
    # handle 0x0000 and hang up. Publishing the full set costs a few dozen
    # bytes and removes the entire question.
    info = app.add_service(
        Service(f"{root}/service1", hogp.DEVICE_INFO_SERVICE_UUID)
    )
    info_values = {
        hogp.MANUFACTURER_NAME_UUID: manufacturer.encode("utf-8"),
        hogp.PNP_ID_UUID: hogp.build_pnp_id(
            vendor_id, product_id, version, pnp_source
        ),
        hogp.FIRMWARE_REVISION_UUID: firmware_revision.encode("utf-8"),
        hogp.SERIAL_NUMBER_UUID: serial_number.encode("utf-8"),
        hogp.MODEL_NUMBER_UUID: model_number.encode("utf-8"),
    }
    info.characteristics += [
        Characteristic(
            f"{info.path}/char{index}", uuid, info.path, ["read"],
            value=info_values[uuid],
        )
        for index, uuid in enumerate(hogp.DEVICE_INFO_CHARACTERISTICS)
    ]

    # Battery: a real gamepad has one, and a host that shows controller battery
    # looks for this service before deciding what we are.
    battery = app.add_service(
        Service(f"{root}/service2", hogp.BATTERY_SERVICE_UUID)
    )
    battery.characteristics.append(
        Characteristic(
            f"{battery.path}/char0", hogp.BATTERY_LEVEL_UUID, battery.path,
            ["read", "notify"], value=bytes([max(0, min(100, battery_level))]),
        )
    )

    # The 8BitDo vendor service. The console searches for it explicitly and,
    # not finding it, reads a null handle -- the same signature that identified
    # the missing Device Information characteristics. See hogp.VENDOR_*.
    #
    # The shape is copied from the real pad; the protocol carried over it is
    # unknown and proprietary. Every write is handed to on_vendor_write so we
    # can see what the console expects rather than guess at it.
    vendor = app.add_service(
        Service(f"{root}/service3", hogp.VENDOR_SERVICE_UUID)
    )
    vendor.characteristics += [
        Characteristic(
            f"{vendor.path}/char0", hogp.VENDOR_RX_UUID, vendor.path,
            ["read", "write-without-response", "write", "notify"],
            value=b"",
            on_write=(lambda data: on_vendor_write("ff11", data))
            if on_vendor_write else None,
        ),
        Characteristic(
            f"{vendor.path}/char1", hogp.VENDOR_TX_UUID, vendor.path,
            ["write-without-response", "write", "notify"],
            value=b"",
            on_write=(lambda data: on_vendor_write("ff12", data))
            if on_vendor_write else None,
        ),
    ]

    return app, input_report

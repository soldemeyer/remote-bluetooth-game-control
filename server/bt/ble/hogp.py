"""HID over GATT: the wire format, with no D-Bus dependency.

The constants and payload helpers live here, apart from the D-Bus objects that
publish them (``hid_service.py``), for the same reason ``common/video.py`` holds
the media wire format apart from PyAV: this half is stdlib-only, so it can be
tested on any machine, and a dev box without the server extras still imports it.

Three services, and a HOGP host expects all three. Omitting Device Information
or Battery does not fail loudly; the host simply decides the device is not a
HID peripheral it wants, which is the same silent refusal that makes every
problem in this area expensive to find.

  * **Human Interface Device (0x1812)** -- Report Map, HID Information,
    HID Control Point, Protocol Mode, and one Report characteristic per report.
  * **Device Information (0x180A)** -- PnP ID carries the vendor and product
    ids. This is the BLE equivalent of the Classic DeviceID SDP record, and it
    is what a console checks when it only pairs with controllers it recognises.
  * **Battery (0x180F)** -- a real gamepad has a battery, and a host that shows
    controller battery level looks for this.

The report ID is NOT in the payload
-----------------------------------
This is the HOGP counterpart of the Classic report-ID trap, and it fails the
same silent way -- in the opposite direction.

Over Classic, every input report begins with its report ID byte; omitting it
makes the host discard the report. Over HOGP the report ID is carried in the
**Report Reference descriptor (0x2908)** attached to the characteristic, so the
notification value is the report body *without* it. Leaving the ID in shifts
every field by one byte: axes read as garbage, buttons land on the wrong bits,
and nothing anywhere reports an error.

``build_ble_payload`` strips it, and there is a test asserting the BLE payload
is exactly one byte shorter than the Classic one.
"""

import logging
import struct

log = logging.getLogger(__name__)

HID_SERVICE_UUID = "00001812-0000-1000-8000-00805f9b34fb"
DEVICE_INFO_SERVICE_UUID = "0000180a-0000-1000-8000-00805f9b34fb"
BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"

REPORT_MAP_UUID = "00002a4b-0000-1000-8000-00805f9b34fb"
HID_INFORMATION_UUID = "00002a4a-0000-1000-8000-00805f9b34fb"
HID_CONTROL_POINT_UUID = "00002a4c-0000-1000-8000-00805f9b34fb"
REPORT_UUID = "00002a4d-0000-1000-8000-00805f9b34fb"
PROTOCOL_MODE_UUID = "00002a4e-0000-1000-8000-00805f9b34fb"
REPORT_REFERENCE_UUID = "00002908-0000-1000-8000-00805f9b34fb"

PNP_ID_UUID = "00002a50-0000-1000-8000-00805f9b34fb"
MANUFACTURER_NAME_UUID = "00002a29-0000-1000-8000-00805f9b34fb"

#: The rest of the Device Information service a real pad publishes.
#:
#: These are not decoration. Measured against an Analogue 3D: it encrypts the
#: link, enumerates Device Information, and when it finds only PnP ID and
#: Manufacturer Name it issues ``Read Request`` on **handle 0x0000** -- a null
#: handle, the signature of a client whose lookup for a required characteristic
#: returned nothing -- takes the Invalid Handle error and drops the link. The
#: whole connection lasts under a second and nothing on our side reports a
#: fault, because answering an invalid handle with an error IS correct
#: behaviour. The real 8BitDo 64 publishes all five.
MODEL_NUMBER_UUID = "00002a24-0000-1000-8000-00805f9b34fb"
SERIAL_NUMBER_UUID = "00002a25-0000-1000-8000-00805f9b34fb"
FIRMWARE_REVISION_UUID = "00002a26-0000-1000-8000-00805f9b34fb"

#: The 8BitDo vendor service, and the console asks for it by name.
#:
#: Measured: an Analogue 3D connects, discovers Device Information and HID
#: successfully, then issues ``Find By Type Value`` for ``0xff10`` -- and on
#: getting ``Attribute Not Found`` follows it with a read of **handle 0x0000**,
#: the same null-handle signature that identified the missing Device
#: Information characteristics. It then subscribes to input reports, receives
#: them correctly, and drops the link about 34.5 seconds later, every time,
#: on any adapter.
#:
#: What these two characteristics carry is **not known**. They are proprietary
#: to 8BitDo and this is a reproduction of their shape, taken from the real
#: pad's GATT database, not of their protocol. Publishing them makes discovery
#: succeed; whether the console also requires a meaningful exchange over them
#: is the next thing to find out, which is why every write is logged.
VENDOR_SERVICE_UUID = "0000ff10-0000-1000-8000-00805f9b34fb"

#: Read, write, write-without-response and notify on the real pad.
VENDOR_RX_UUID = "0000ff11-0000-1000-8000-00805f9b34fb"

#: Write, write-without-response and notify -- no read.
VENDOR_TX_UUID = "0000ff12-0000-1000-8000-00805f9b34fb"

#: The Device Information service, in the order the real 8BitDo 64 lists it.
#:
#: Kept here rather than inline in ``hid_service`` so it is reachable without
#: dbus-next -- this module is the stdlib half precisely so facts like this can
#: be asserted on any machine. ``build_application`` iterates this tuple, so
#: the built service and this list cannot drift apart.
DEVICE_INFO_CHARACTERISTICS: tuple[str, ...] = (
    MANUFACTURER_NAME_UUID,
    PNP_ID_UUID,
    FIRMWARE_REVISION_UUID,
    SERIAL_NUMBER_UUID,
    MODEL_NUMBER_UUID,
)
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

#: Report Reference descriptor types.
REPORT_TYPE_INPUT = 0x01
REPORT_TYPE_OUTPUT = 0x02
REPORT_TYPE_FEATURE = 0x03

#: Protocol Mode values. Report mode is what a gamepad uses; boot mode exists
#: only for keyboards and mice, and a host that asks for it on a gamepad has
#: misidentified us.
PROTOCOL_MODE_BOOT = 0x00
PROTOCOL_MODE_REPORT = 0x01

#: HID Information, read off the real 8BitDo 64: ``01 01 00 03``.
#:
#: bcdHID **0x0101** (HID 1.01), country code 0 (not localised), flags 0x03 =
#: RemoteWake | NormallyConnectable. NormallyConnectable is what tells the host
#: it may reconnect whenever it likes -- the BLE counterpart of the Classic
#: HIDNormallyConnectable SDP attribute.
#:
#: This declared 0x0111 (HID 1.11) before, which is the version most HOGP
#: examples use. Matching the pad costs nothing and removes one more field a
#: console could be comparing.
HID_INFORMATION = struct.pack("<HBB", 0x0101, 0x00, 0x03)

#: PnP ID vendor id source: 0x01 Bluetooth SIG, 0x02 USB. Controller vendors
#: register a USB id and reuse it, exactly as on the Classic side.
PNP_SOURCE_BLUETOOTH_SIG = 0x01
PNP_SOURCE_USB = 0x02


def build_pnp_id(vendor_id, product_id, version=0x0100, source=PNP_SOURCE_USB):
    """The PnP ID characteristic value: 7 bytes, little-endian after the source."""
    return struct.pack("<BHHH", source, vendor_id, product_id, version)


def build_ble_payload(report, report_id):
    """Strip the leading report ID from a Classic-shaped report.

    ``TargetProfile.build_input_report`` writes the report ID at byte 0 because
    that is what the Classic interrupt channel needs. HOGP carries the id in
    the Report Reference descriptor instead, so the notification value must be
    the body alone.

    Only strips when byte 0 actually is the id: a profile whose descriptor
    declares no report ids writes none, and removing a real data byte there
    would shift every field by one with nothing to indicate it.
    """
    data = bytes(report)
    if report_id is not None and data and data[0] == report_id:
        return data[1:]
    return data


# -- advertising data ------------------------------------------------------
#
# Built here rather than left to BlueZ because BlueZ's LEAdvertisingManager1
# cannot publish it on this platform: it takes the *extended* advertising path
# (MGMT Add Extended Advertising Parameters/Data, 0x0054/0x0055) and the kernel
# rejects the data with Invalid Parameters. Measured on a Pi 5, kernel 6.18,
# BlueZ 5.82, on both the built-in Broadcom adapter and the Realtek dongles --
# and with a *minimal* advertisement, so it is not our data that is at fault.
#
# The legacy single-step path (MGMT Add Advertising, 0x003e) accepts the very
# same bytes. That is what `btmgmt add-adv` uses, and it is what we use.

#: Bluetooth SIG Appearance for a gamepad. The real 8BitDo 64 that pairs with
#: an Analogue 3D advertises exactly this, and a console filtering on it can do
#: so without connecting.
APPEARANCE_GAMEPAD = 0x03C4

#: AD structure type codes.
AD_TYPE_FLAGS = 0x01
AD_TYPE_UUID16_COMPLETE = 0x03
AD_TYPE_NAME_COMPLETE = 0x09
AD_TYPE_APPEARANCE = 0x19

#: MGMT advertising flags (``MGMT_ADV_FLAG_*``).
ADV_FLAG_CONNECTABLE = 0x0001
ADV_FLAG_DISCOVERABLE = 0x0002
ADV_FLAG_LIMITED_DISCOVERABLE = 0x0004
ADV_FLAG_MANAGED_FLAGS = 0x0008
ADV_FLAG_TX_POWER = 0x0010

#: A scan response is 31 bytes, and so is the advertisement.
MAX_AD_BYTES = 31


def ad_structure(ad_type, payload):
    """One AD structure: length, type, payload."""
    return bytes([len(payload) + 1, ad_type]) + bytes(payload)


def build_advertising_data(name, service_uuid16=0x1812, appearance=APPEARANCE_GAMEPAD):
    """The ADV_IND payload: appearance, the HID service UUID, and the name.

    **All three go in the advertisement itself**, which is what the real
    8BitDo 64 does. Its ADV_IND is 25 bytes:

        02 01 05                    flags: limited discoverable, BR/EDR not supported
        03 19 c4 03                 appearance: gamepad
        03 03 12 18                 16-bit service UUIDs: HID (0x1812)
        0d 09 38 42 69 74 ...       complete local name: "8BitDo 64 BT"

    This matters because a **passive** scanner never sends a scan request and
    so never sees a scan response. A console that filters on name or appearance
    while passively scanning sees all of it from a real pad, and from an
    advertisement carrying only flags and a bare HID UUID it sees nothing to
    match on. Ours was seven bytes: `02 01 05 03 03 12 18`.

    The **Flags** structure is still omitted: the kernel adds it under
    ``ADV_FLAG_MANAGED_FLAGS``, and a duplicate fails ``tlv_data_is_valid``,
    taking the whole advertisement with it under a bare Invalid Parameters.

    Budget: 3 (kernel flags) + 4 + 4 + 2 + len(name) must fit 31, which allows
    a name of 18 characters. Longer names are truncated rather than silently
    overflowing the advertisement.
    """
    data = ad_structure(AD_TYPE_APPEARANCE, struct.pack("<H", appearance))
    data += ad_structure(AD_TYPE_UUID16_COMPLETE, struct.pack("<H", service_uuid16))

    encoded = name.encode("utf-8")
    budget = MAX_AD_BYTES - 3 - len(data) - 2      # 3 = the kernel's flags
    if len(encoded) > budget:
        encoded = encoded[:budget]
    return data + ad_structure(AD_TYPE_NAME_COMPLETE, encoded)


def build_scan_response(name, appearance=APPEARANCE_GAMEPAD):
    """The SCAN_RSP payload: the name again, for active scanners.

    Duplicated deliberately. The advertisement already carries it, which is
    what a passive scanner needs; repeating it here costs nothing and matches
    what a real pad sends to a scanner that does ask.
    """
    encoded = name.encode("utf-8")
    budget = MAX_AD_BYTES - 2
    if len(encoded) > budget:
        encoded = encoded[:budget]
    return ad_structure(AD_TYPE_NAME_COMPLETE, encoded)


# -- advertising interval --------------------------------------------------
#
# `Add Advertising` (MGMT 0x003e) carries no interval, so the kernel uses
# `hdev->le_adv_min_interval` / `le_adv_max_interval`. Those default to 2048
# units = **1280 ms**, which is the slowest sensible value and the right
# default for a coin-cell peripheral. We are mains powered and a player is
# waiting, so it is the wrong one here.
#
# The extended path (`Add Extended Advertising Parameters`, 0x0054) does take
# an interval, and is not usable: the kernel rejects its data step with
# Invalid Parameters on this platform -- measured, and the reason our
# advertising goes through the legacy opcode at all. So the interval is set
# where the kernel keeps it, through debugfs.

#: Advertising interval unit, in milliseconds.
ADV_INTERVAL_UNIT_MS = 0.625

#: 60 ms and 90 ms. A standard "fast connectable" range, 14-21x quicker than
#: the kernel default, and well clear of the 20 ms floor the specification
#: sets for connectable undirected advertising.
#:
#: A **range**, not one value, and that matters with four adapters: the
#: controller picks each interval within the range and adds its own 0-10 ms
#: delay, so four radios do not settle into lockstep and collide on the three
#: advertising channels every cycle.
#:
#: An advertising event is roughly 1.1 ms of airtime across the three
#: channels, so 60 ms is about 2% duty per adapter and 8% for four -- enough
#: headroom left for the connection events those same radios are carrying,
#: which is the thing not to trade away for discovery speed.
FAST_ADV_MIN_UNITS = 96
FAST_ADV_MAX_UNITS = 144

#: Where the kernel exposes them. Not an ABI, so every access is best-effort:
#: an adapter that will not take a faster interval still advertises perfectly
#: well, just at the kernel's pace.
DEBUGFS_BLUETOOTH = "/sys/kernel/debug/bluetooth"

#: The specification's floor for connectable undirected advertising, and the
#: kernel's ceiling. Writing outside this is refused with EINVAL, which would
#: read as "debugfs is unavailable" rather than "that value is wrong".
ADV_INTERVAL_MIN_UNITS = 0x0020
ADV_INTERVAL_MAX_UNITS = 0x4000


def read_advertising_interval(index, root=DEBUGFS_BLUETOOTH):
    """``(min, max)`` in interval units, or None if they cannot be read."""
    import os

    values = []
    for name in ("adv_min_interval", "adv_max_interval"):
        path = os.path.join(root, f"hci{index}", name)
        try:
            with open(path) as handle:
                values.append(int(handle.read().strip()))
        except (OSError, ValueError):
            return None
    return tuple(values)


def set_advertising_interval(index, minimum=FAST_ADV_MIN_UNITS,
                             maximum=FAST_ADV_MAX_UNITS,
                             root=DEBUGFS_BLUETOOTH):
    """Ask this adapter to advertise faster. True if it now holds the value.

    Read-then-write, like every other invariant here: this is called each time
    the advertisement starts, and the ordinary case must not write.

    **Never fatal.** A kernel without debugfs mounted, or an adapter that
    refuses the value, still advertises -- at 1280 ms rather than 60. Losing
    discovery speed is a worse experience; losing the advertisement would be a
    dead controller.

    The value only reaches the air when the advertising instance is next
    started, because the kernel reads these when it builds the parameters.
    Callers set it *before* adding the instance for that reason.
    """
    import os

    if not (ADV_INTERVAL_MIN_UNITS <= minimum <= maximum <= ADV_INTERVAL_MAX_UNITS):
        raise ValueError(
            f"advertising interval {minimum}-{maximum} is outside "
            f"{ADV_INTERVAL_MIN_UNITS}-{ADV_INTERVAL_MAX_UNITS} units"
        )

    current = read_advertising_interval(index, root)
    if current == (minimum, maximum):
        return True
    if current is None:
        log.debug("hci%s: advertising interval is not readable", index)
        return False

    # Order matters: the kernel rejects a min above the current max, and a max
    # below the current min. Widening first is always accepted.
    writes = (
        ("adv_max_interval", maximum) if maximum >= current[1]
        else ("adv_min_interval", minimum),
        ("adv_min_interval", minimum) if maximum >= current[1]
        else ("adv_max_interval", maximum),
    )
    for name, value in writes:
        path = os.path.join(root, f"hci{index}", name)
        try:
            with open(path, "w") as handle:
                handle.write(str(value))
        except OSError as exc:
            log.warning(
                "hci%s: could not set %s to %d (%s). It will advertise at the "
                "kernel default of %.0f ms, so a console takes longer to find "
                "it.",
                index, name, value, exc,
                current[0] * ADV_INTERVAL_UNIT_MS,
            )
            return False

    log.info(
        "hci%s advertising interval %.0f-%.0f ms (was %.0f-%.0f ms)",
        index,
        minimum * ADV_INTERVAL_UNIT_MS, maximum * ADV_INTERVAL_UNIT_MS,
        current[0] * ADV_INTERVAL_UNIT_MS, current[1] * ADV_INTERVAL_UNIT_MS,
    )
    return True

"""SDP service record registration for the HID device role.

A console will not connect to us unless it can discover a HID service record
describing what we are. Registration goes through BlueZ's D-Bus
``org.bluez.ProfileManager1`` interface.

Two things bite people here, both handled below:

1. **`sdptool add` does not work on modern BlueZ.** It was removed along with
   the deprecated SDP daemon interface. D-Bus is the only supported path.

2. **`bluetoothd` must run with the input plugin disabled.** That plugin claims
   the HID role for itself, so our L2CAP binds on PSM 17/19 fail with EADDRINUSE
   and the failure looks like a permissions problem. Run it with
   ``--noplugin=input``; :func:`check_bluetooth_daemon` detects the misconfig
   and says so explicitly rather than letting it surface as a confusing error.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)

HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"
HID_PROFILE_PATH = "/rbgc/profile/hid"

#: Standard HID L2CAP PSMs. Fixed by the HID profile spec -- many hosts ignore
#: the SDP-advertised values and simply connect to these.
PSM_CONTROL = 17
PSM_INTERRUPT = 19


class SDPError(RuntimeError):
    """Service record could not be registered."""


def build_hid_record(
    device_name: str,
    report_descriptor: bytes,
    vendor_id: int,
    product_id: int,
    version: int = 0x0100,
) -> str:
    """Build the HID SDP service record XML.

    The structure follows the Bluetooth HID profile specification. Values that
    matter in practice:

      * ``HIDDescriptorList`` carries the report descriptor -- the host parses
        this to learn our report layout.
      * ``HIDReconnectInitiate`` true lets us re-establish the link after a
        drop, which is what makes "reconnect" work without re-pairing.
      * ``HIDNormallyConnectable`` true means the host may connect to us at any
        time, rather than only right after pairing.
    """
    descriptor_hex = report_descriptor.hex()

    return f"""<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001">
    <sequence><uuid value="0x1124" /></sequence>
  </attribute>
  <attribute id="0x0004">
    <sequence>
      <sequence>
        <uuid value="0x0100" />
        <uint16 value="0x{PSM_CONTROL:04x}" />
      </sequence>
      <sequence><uuid value="0x0011" /></sequence>
    </sequence>
  </attribute>
  <attribute id="0x0005">
    <sequence><uuid value="0x1002" /></sequence>
  </attribute>
  <attribute id="0x0006">
    <sequence>
      <uint16 value="0x656e" />
      <uint16 value="0x006a" />
      <uint16 value="0x0100" />
    </sequence>
  </attribute>
  <attribute id="0x0009">
    <sequence>
      <sequence>
        <uuid value="0x1124" />
        <uint16 value="0x0100" />
      </sequence>
    </sequence>
  </attribute>
  <attribute id="0x000d">
    <sequence>
      <sequence>
        <sequence>
          <uuid value="0x0100" />
          <uint16 value="0x{PSM_INTERRUPT:04x}" />
        </sequence>
        <sequence><uuid value="0x0011" /></sequence>
      </sequence>
    </sequence>
  </attribute>
  <attribute id="0x0100"><text value="{escape(device_name)}" /></attribute>
  <attribute id="0x0101"><text value="{escape(device_name)}" /></attribute>
  <attribute id="0x0102"><text value="RBGC" /></attribute>

  <!-- HID attributes, in strictly ascending id order.
       SDP requires ascending attribute ids, and a host that hits an
       out-of-order or duplicated id may reject the whole record. Getting this
       wrong produces no error anywhere: Windows reads the record, fails to
       recognise a HID device, never attempts PSM 17/19, and falls back to
       generic pairing, which the user sees as an unexplained PIN prompt. -->

  <!-- 0x0201 HIDParserVersion -->
  <attribute id="0x0201"><uint16 value="0x0111" /></attribute>
  <!-- 0x0202 HIDDeviceSubclass: 0x08 = gamepad, matching the class of device -->
  <attribute id="0x0202"><uint8 value="0x08" /></attribute>
  <!-- 0x0203 HIDCountryCode: 0x00 = not localized -->
  <attribute id="0x0203"><uint8 value="0x00" /></attribute>
  <!-- 0x0204 HIDVirtualCable -->
  <attribute id="0x0204"><boolean value="false" /></attribute>
  <!-- 0x0205 HIDReconnectInitiate: lets us re-establish after a drop -->
  <attribute id="0x0205"><boolean value="true" /></attribute>
  <!-- 0x0206 HIDDescriptorList: the report descriptor itself -->
  <attribute id="0x0206">
    <sequence>
      <sequence>
        <uint8 value="0x22" />
        <text encoding="hex" value="{descriptor_hex}" />
      </sequence>
    </sequence>
  </attribute>
  <!-- 0x0207 HIDLANGIDBaseList -->
  <attribute id="0x0207">
    <sequence>
      <sequence>
        <uint16 value="0x0409" />
        <uint16 value="0x0100" />
      </sequence>
    </sequence>
  </attribute>
  <!-- 0x0208 HIDSDPDisable: keep SDP available after connecting -->
  <attribute id="0x0208"><boolean value="false" /></attribute>
  <!-- 0x0209 HIDBatteryPower -->
  <attribute id="0x0209"><boolean value="true" /></attribute>
  <!-- 0x020a HIDRemoteWake -->
  <attribute id="0x020a"><boolean value="true" /></attribute>
  <!-- 0x020b HIDProfileVersion: HID 1.0. NOT the supervision timeout; putting
       a timeout value here advertises a nonsensical profile version and the
       host discards the whole record. -->
  <attribute id="0x020b"><uint16 value="0x0100" /></attribute>
  <!-- 0x020c HIDSupervisionTimeout -->
  <attribute id="0x020c"><uint16 value="0x0c80" /></attribute>
  <!-- 0x020d HIDNormallyConnectable -->
  <attribute id="0x020d"><boolean value="true" /></attribute>
  <!-- 0x020e HIDBootDevice: false, since boot protocol is keyboards and mice -->
  <attribute id="0x020e"><boolean value="false" /></attribute>
  <!-- 0x020f HIDSSRHostMaxLatency -->
  <attribute id="0x020f"><uint16 value="0x0640" /></attribute>
  <!-- 0x0210 HIDSSRHostMinTimeout -->
  <attribute id="0x0210"><uint16 value="0x0320" /></attribute>
</record>
"""


async def register_hid_profile(
    device_name: str,
    report_descriptor: bytes,
    vendor_id: int,
    product_id: int,
    *,
    path: str = HID_PROFILE_PATH,
) -> object:
    """Register the HID profile with BlueZ over D-Bus.

    Returns a handle that must be kept alive: BlueZ unregisters the profile
    when the owning D-Bus connection drops.
    """
    try:
        from dbus_next import BusType, Variant
        from dbus_next.aio import MessageBus

        # Lives in its own module because dbus_next cannot tolerate PEP 563
        # annotations -- see that module's docstring.
        from server.bt._dbus_profile import HIDProfile
    except ImportError as exc:
        raise SDPError(
            "dbus-next is required for Bluetooth support. "
            'Install it with: pip install -e ".[server]"'
        ) from exc

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    profile = HIDProfile()
    bus.export(path, profile)

    introspection = await bus.introspect("org.bluez", "/org/bluez")
    obj = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
    manager = obj.get_interface("org.bluez.ProfileManager1")

    record = build_hid_record(device_name, report_descriptor, vendor_id, product_id)

    options = {
        "Name": Variant("s", device_name),
        "Role": Variant("s", "server"),
        # The primary service class, as BlueZ expects when the profile UUID and
        # the advertised service class are the same thing.
        "Service": Variant("s", HID_UUID),
        "ServiceRecord": Variant("s", record),
        # Consoles do not authenticate a controller, and requiring it here
        # makes pairing fail with an opaque error.
        "RequireAuthentication": Variant("b", False),
        "RequireAuthorization": Variant("b", False),
        #
        # Deliberately NOT set:
        #
        #   Channel -- this is the **RFCOMM** channel number. HID runs over
        #     L2CAP, so passing the control PSM here (which this code did for a
        #     long time) asks BlueZ to stand up an RFCOMM server on channel 17
        #     and describes us as an RFCOMM profile. The L2CAP equivalent is
        #     `PSM`, and even that is unnecessary: we bind PSM 17/19 ourselves
        #     per adapter (server/bt/hid.py), which is what makes several
        #     dongles several independent controllers. Neither joycontrol nor
        #     any other working BlueZ HID-device implementation passes either.
        #
        #   AutoConnect -- documented as applying to *client* UUIDs, to force
        #     channel connection when a remote connects. We register as a
        #     server, so it has no meaning here.
    }

    try:
        await manager.call_register_profile(path, HID_UUID, options)
    except Exception as exc:
        raise SDPError(f"BlueZ rejected the HID profile registration: {exc}") from exc

    log.info("Registered HID profile '%s' with BlueZ", device_name)
    return bus


async def unregister_hid_profile(bus: object, path: str = HID_PROFILE_PATH) -> None:
    """Unregister and drop the D-Bus connection."""
    try:
        introspection = await bus.introspect("org.bluez", "/org/bluez")  # type: ignore[attr-defined]
        obj = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)  # type: ignore[attr-defined]
        manager = obj.get_interface("org.bluez.ProfileManager1")
        await manager.call_unregister_profile(path)
    except Exception as exc:
        log.debug("Could not cleanly unregister HID profile: %s", exc)
    finally:
        try:
            bus.disconnect()  # type: ignore[attr-defined]
        except Exception:
            pass


def check_bluetooth_daemon() -> list[str]:
    """Check for the misconfigurations that break HID emulation.

    Returns human-readable problems. Run at startup so the operator gets a
    clear diagnosis instead of an EADDRINUSE that looks like a permissions bug.
    """
    problems: list[str] = []

    if shutil.which("bluetoothctl") is None and shutil.which("hciconfig") is None:
        problems.append(
            "BlueZ tools not found. Install with: sudo apt install bluez"
        )
        return problems

    # Is the input plugin loaded? It claims PSM 17/19 for itself.
    try:
        result = subprocess.run(
            ["systemctl", "show", "bluetooth.service", "-p", "ExecStart"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            # Never let a helper inherit the service's stdin; see _run() in
            # adapter.py for the btmgmt hang that cost us this lesson.
            input="",
        )
        exec_start = result.stdout
        if exec_start and "noplugin" not in exec_start:
            problems.append(
                "bluetoothd is running with the input plugin enabled, which claims "
                "the HID role and will make our L2CAP binds fail.\n"
                "  Fix with:\n"
                "    sudo mkdir -p /etc/systemd/system/bluetooth.service.d\n"
                "    printf '[Service]\\nExecStart=\\n"
                "ExecStart=/usr/libexec/bluetooth/bluetoothd --noplugin=input\\n' \\\n"
                "      | sudo tee /etc/systemd/system/bluetooth.service.d/rbgc.conf\n"
                "    sudo systemctl daemon-reload && sudo systemctl restart bluetooth"
            )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        # Not systemd, or systemctl unavailable. Not fatal.
        log.debug("Could not inspect bluetooth.service")

    return problems

"""One adapter acting as a BLE HID gamepad.

DO NOT add ``from __future__ import annotations`` -- see gatt.py.

Ties the GATT application and the advertisement to one adapter, and exposes a
:class:`~server.bt.sink.HIDSink` so the router and the datapath drive it exactly
as they drive the Classic path. That is the whole reason the sink interface
exists: adding a second transport should not reach above this layer, and it
does not.

What is genuinely different from Classic
----------------------------------------
* **Per adapter, not per machine.** ``GattManager1`` and
  ``LEAdvertisingManager1`` both live on the adapter object, so four dongles
  are four independent peripherals with their own services and names. The
  Classic side cannot do this -- one SDP database per machine.
* **No sniff, no flush timeout.** Latency is governed by the connection
  interval and peripheral latency, and the **central grants both**. We can ask
  (a Connection Parameter Update Request) and it can refuse. None of the
  levers in ``server/bt/link.py`` apply here.
* **Notifications can be dropped on the floor.** A host subscribes to the
  report characteristic as the last step of connecting; anything notified
  before that goes nowhere and reports success.
"""

import logging

from server.bt.ble import hogp
from server.bt.sink import HIDSink

log = logging.getLogger(__name__)

def _index_of(hci_name):
    """``hci3`` -> ``3``. -1 when there is no index to be had."""
    suffix = str(hci_name).removeprefix("hci")
    return int(suffix) if suffix.isdigit() else -1


BLUEZ = "org.bluez"
GATT_MANAGER_IFACE = "org.bluez.GattManager1"
LE_ADVERTISING_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"


class BLESink(HIDSink):
    """Delivers input reports as GATT notifications.

    Called from the datapath, so it must not block. A notification is an
    ``emit_properties_changed`` on the report characteristic, which dbus-next
    turns into a message write -- no round trip, no reply awaited.
    """

    def __init__(self, profile, bd_addr):
        self._profile = profile
        self._bd_addr = bd_addr
        self._characteristic = None
        self._peer = ""
        self._link_up = False

        #: The report id to strip. HOGP carries it in the Report Reference
        #: descriptor, so it must not also be in the payload -- see
        #: hogp.build_ble_payload.
        self._report_id = profile.descriptor.input_report_id

        self.reports_sent = 0
        self.notify_failures = 0

    @property
    def is_connected(self):
        """True when a host is attached and can be notified.

        Based on the **link**, not on StartNotify having been called. The
        subscription flag looks like the stricter, more honest test and is not:
        for a bonded device the CCCD is persistent, so a console that
        subscribed once never writes it again, the flag stays false after the
        first reconnect, and every report is dropped while the link is live and
        encrypted.
        """
        if self._characteristic is None:
            return False
        return self._link_up or self._characteristic.notifying

    def set_link(self, connected, peer=""):
        """Record that a host attached or left, from the MGMT event stream.

        bluetoothd owns the LE link, so nothing in this module would otherwise
        know: the GATT callbacks only fire for reads, writes and subscription
        changes, none of which happen on a plain reconnect by a bonded host.
        """
        self._link_up = bool(connected)
        if connected:
            self._peer = peer or self._peer
        else:
            self._peer = ""

    @property
    def peer(self):
        return self._peer

    def attach(self, characteristic, peer=""):
        self._characteristic = characteristic
        self._peer = peer
        self._profile.on_connected()

    def detach(self):
        self._characteristic = None
        self._link_up = False
        peer, self._peer = self._peer, ""
        if peer:
            self._profile.on_disconnected()

    def send_input_report(self, report):
        characteristic = self._characteristic
        if characteristic is None:
            return False

        payload = hogp.build_ble_payload(report, self._report_id)
        try:
            if not characteristic.notify(payload):
                # The host has not subscribed. Ordinary right up until it does.
                return False
        except Exception:
            self.notify_failures += 1
            log.debug("BLE notify failed on %s", self._bd_addr, exc_info=True)
            return False

        self.reports_sent += 1
        return True

    def close(self):
        self.detach()


class BLEPeripheral:
    """One adapter published as a BLE HID gamepad.

    Owns the GATT application, the advertisement, and the sink the router
    writes to. Everything is per adapter, so several of these coexist without
    the system-wide constraint the Classic SDP record imposes.
    """

    def __init__(self, hci_name, profile, identity, *, name=None, bus=None,
                 mgmt=None, index=None, instance=1):
        self.hci_name = hci_name
        self.profile = profile
        self.identity = identity
        self.name = name or identity.device_name
        self.sink = BLESink(profile, hci_name)

        self._bus = bus
        self._owns_bus = bus is None
        self._app = None
        self._input_report = None
        self._registered = False

        #: Advertising goes through MGMT, not through bluetoothd.
        #:
        #: ``org.bluez.LEAdvertisingManager1`` cannot publish on this platform:
        #: it takes the extended path (Add Extended Advertising
        #: Parameters/Data) and the kernel rejects the data with Invalid
        #: Parameters -- measured on the built-in adapter and the dongles, with
        #: a minimal advertisement, so it is not our payload at fault. The
        #: legacy single-step Add Advertising accepts the identical bytes.
        #:
        #: The GATT half still goes through bluetoothd, which works.
        self._mgmt = mgmt
        self._index = index if index is not None else _index_of(hci_name)
        self._instance = instance
        self._advertising = False

    @property
    def root(self):
        return f"/rbgc/ble/{self.hci_name}"

    @property
    def adapter_path(self):
        return f"/org/bluez/{self.hci_name}"

    async def start(self):
        """Publish the services and begin advertising.

        Raises RuntimeError with an actionable message; the caller decides
        whether a BLE failure should stop the adapter, and it should not --
        the Classic path on the same radio is unaffected.
        """
        from dbus_next import BusType
        from dbus_next.aio import MessageBus

        from server.bt.ble.hid_service import build_application

        if self._registered:
            return

        if self._bus is None:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

        descriptor = self.profile.descriptor
        self._app, self._input_report = build_application(
            self.root,
            descriptor.report_descriptor,
            self.identity.vendor_id,
            self.identity.product_id,
            version=self.identity.version,
            input_report_id=descriptor.input_report_id,
            # Device Information, all five characteristics. A console that
            # wants one we omit does not ask twice -- see hogp.MODEL_NUMBER_UUID.
            manufacturer=self.identity.manufacturer or "RBGC",
            model_number=self.identity.model_number or self.identity.device_name,
            serial_number=self.identity.serial_number or "000000000000",
            firmware_revision=self.identity.firmware_revision or "1.00",
            on_control_point=self._on_control_point,
            on_protocol_mode=self._on_protocol_mode,
            on_output_report=self._on_output_report,
            on_vendor_write=self._on_vendor_write,
        )
        self._app.export(self._bus)

        introspection = await self._bus.introspect(BLUEZ, self.adapter_path)
        adapter = self._bus.get_proxy_object(BLUEZ, self.adapter_path, introspection)

        gatt = adapter.get_interface(GATT_MANAGER_IFACE)
        try:
            await gatt.call_register_application(self.root, {})
        except Exception as exc:
            await self._unexport()
            raise RuntimeError(
                f"BlueZ refused the GATT application on {self.hci_name}: {exc}"
            ) from exc

        try:
            self._start_advertising()
        except Exception as exc:
            # The services are registered but nothing can find them. Unwind
            # rather than leave a peripheral that is published and invisible --
            # the failure mode this whole subsystem keeps producing.
            try:
                await gatt.call_unregister_application(self.root)
            except Exception:
                log.debug("Could not unwind the GATT registration", exc_info=True)
            await self._unexport()
            raise RuntimeError(
                f"Could not advertise on {self.hci_name}: {exc}"
            ) from exc

        self._registered = True
        self.sink.attach(self._input_report)
        log.info(
            "BLE gamepad live on %s as '%s' (HID over GATT, %04X:%04X)",
            self.hci_name, self.name,
            self.identity.vendor_id, self.identity.product_id,
        )

    def _start_advertising(self):
        """Publish the advertisement through our own MGMT socket."""
        if self._mgmt is None:
            raise RuntimeError(
                "no management socket, and bluetoothd cannot advertise on this "
                "platform"
            )
        if self._index < 0:
            raise RuntimeError(f"no adapter index for {self.hci_name}")

        flags = (
            hogp.ADV_FLAG_CONNECTABLE
            # **Limited**, not general. This is what "in pairing mode" means on
            # LE, and it is what the real 8BitDo 64 advertises (flags 0x05). A
            # console scanning for a controller to pair has every reason to
            # filter on it -- limited discoverable is the bit that separates a
            # pad waiting to be paired from one merely powered on.
            | hogp.ADV_FLAG_LIMITED_DISCOVERABLE
            # The kernel adds the Flags AD structure itself. Ours must not also
            # be there: it rejects the whole advertisement for a duplicate,
            # with the same Invalid Parameters it gives for a length problem.
            | hogp.ADV_FLAG_MANAGED_FLAGS
        )
        # Clear our instance number first, so start-up is idempotent.
        #
        # An instance belongs to the socket that added it and normally dies
        # with that socket -- but not always: a leftover from a crashed run, or
        # from a `btmgmt add-adv` during debugging, survives and makes the add
        # fail with a bare MGMT status 0x03. The adapter then advertises
        # somebody else's data while our peripheral reports itself unpublished,
        # which is the published-and-invisible failure this subsystem keeps
        # producing. Removing something that is not there is free.
        self._mgmt.remove_advertising(self._index, self._instance)

        self._mgmt.add_advertising(
            self._index, self._instance, flags,
            adv_data=hogp.build_advertising_data(self.name),
            scan_response=hogp.build_scan_response(self.name),
        )
        self._advertising = True

    def _stop_advertising(self):
        if not self._advertising or self._mgmt is None:
            return
        self._mgmt.remove_advertising(self._index, self._instance)
        self._advertising = False

    async def stop(self):
        """Unregister and stop advertising. Safe to call more than once."""
        if not self._registered:
            await self._unexport()
            return

        self.sink.detach()
        self._stop_advertising()

        try:
            introspection = await self._bus.introspect(BLUEZ, self.adapter_path)
            adapter = self._bus.get_proxy_object(
                BLUEZ, self.adapter_path, introspection
            )
            try:
                gatt = adapter.get_interface(GATT_MANAGER_IFACE)
                await gatt.call_unregister_application(self.root)
            except Exception:
                log.debug("Could not unregister the GATT application", exc_info=True)
        except Exception:
            log.debug("Could not reach %s to unregister", self.hci_name, exc_info=True)

        self._registered = False
        await self._unexport()
        log.info("BLE gamepad on %s stopped", self.hci_name)

    async def _unexport(self):
        """Drop our objects from the bus, and the bus if we opened it."""
        if self._bus is None:
            return
        try:
            if self._app is not None:
                self._app.unexport(self._bus)
        except Exception:
            log.debug("Could not unexport BLE objects", exc_info=True)

        self._app = None
        self._input_report = None

        if self._owns_bus:
            try:
                self._bus.disconnect()
            except Exception:
                pass
            self._bus = None

    # -- host callbacks ----------------------------------------------------

    def _on_control_point(self, data):
        """HID Control Point: suspend / exit suspend.

        0x00 is Suspend and 0x01 Exit Suspend. A host suspends when it stops
        wanting reports; continuing to notify through a suspend wastes the
        radio and, on some hosts, is treated as misbehaviour.
        """
        if not data:
            return
        log.debug(
            "%s HID control point: %s",
            self.hci_name, "suspend" if data[0] == 0x00 else "exit suspend",
        )

    def _on_protocol_mode(self, data):
        """Boot vs report protocol.

        Boot mode exists only for keyboards and mice. A host asking a gamepad
        for it has misidentified us, and the reports it then expects are not
        the ones our descriptor declares -- worth a warning rather than
        silently continuing to send report-mode data.
        """
        if not data:
            return
        if data[0] == hogp.PROTOCOL_MODE_BOOT:
            log.warning(
                "%s was put into boot protocol mode, which has no meaning for a "
                "gamepad. The host has probably misidentified this device; "
                "reports will keep using the report protocol.",
                self.hci_name,
            )

    def _on_vendor_write(self, which, data):
        """Log whatever the console writes to the 8BitDo vendor service.

        The protocol here is proprietary and unknown. Logging at INFO is
        deliberate and temporary: this is the only way to learn what the
        console expects, and a handful of bytes on connect is not a hot path.
        If it turns out the console writes continuously this must drop to
        debug -- it sits on bluetoothd's callback, not ours.
        """
        payload = bytes(data)
        log.info(
            "Vendor write on %s to %s: %s (%d bytes)",
            self.hci_name, which, payload.hex() or "<empty>", len(payload),
        )

    def _on_output_report(self, data):
        """An output report from the host: rumble, player LEDs.

        Handed to the profile, which is the same code the Classic path uses --
        the report bytes are identical between transports.
        """
        try:
            self.profile.on_output_report(bytes(data))
        except Exception:
            log.debug("Output report handling failed", exc_info=True)

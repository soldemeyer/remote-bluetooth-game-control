"""D-Bus GATT primitives: the objects BlueZ expects a peripheral to export.

DO NOT add ``from __future__ import annotations`` to this file. dbus-next reads
method annotations at decoration time and requires them to be string constants
holding D-Bus type signatures; PEP 563 stores the source text instead and turns
every signature into nonsense. See server/bt/_dbus_profile.py for the full note
-- the errors surface far from their cause.

How BlueZ takes a GATT application
----------------------------------
You export an object tree and hand BlueZ the root path. The root implements
``org.freedesktop.DBus.ObjectManager``; BlueZ calls ``GetManagedObjects`` once
and reads the whole hierarchy from the reply:

    /rbgc/ble/hci3                      ObjectManager
      /service0                         org.bluez.GattService1
        /service0/char0                 org.bluez.GattCharacteristic1
          /service0/char0/desc0         org.bluez.GattDescriptor1

The tree is read **once, at registration**. Adding a characteristic afterwards
is invisible until the application is unregistered and registered again.

``GattManager1`` lives on the adapter object, not on the daemon, so each
adapter gets its own GATT database. That is a real difference from Classic:
``ProfileManager1`` keeps one SDP database for the whole machine, which is why
every adapter there must advertise the same HID record. Here four dongles can
be four genuinely independent peripherals.
"""

import logging

from dbus_next import Variant
from dbus_next.constants import PropertyAccess
from dbus_next.service import ServiceInterface, dbus_property, method

log = logging.getLogger(__name__)

GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHRC_IFACE = "org.bluez.GattCharacteristic1"
GATT_DESC_IFACE = "org.bluez.GattDescriptor1"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"


class Descriptor(ServiceInterface):
    """A GATT descriptor with a fixed value."""

    def __init__(self, path, uuid, characteristic_path, value, flags=("read",)):
        super().__init__(GATT_DESC_IFACE)
        self.path = path
        self._uuid = uuid
        self._characteristic = characteristic_path
        self._value = bytearray(value)
        self._flags = list(flags)

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":  # noqa: N802
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Characteristic(self) -> "o":  # noqa: N802
        return self._characteristic

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":  # noqa: N802
        return self._flags

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":  # noqa: N802
        return bytes(self._value)

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}"):  # noqa: N802
        self._value = bytearray(value)

    def properties(self):
        return {
            "UUID": Variant("s", self._uuid),
            "Characteristic": Variant("o", self._characteristic),
            "Flags": Variant("as", self._flags),
        }


class Characteristic(ServiceInterface):
    """A GATT characteristic.

    Reads are served from ``_value`` unless ``on_read`` is supplied. Writes go
    to ``on_write`` when given, which is how the HID Control Point and Protocol
    Mode are handled.

    Notifications are the interesting part: BlueZ watches
    ``PropertiesChanged`` on this interface for the ``Value`` property and
    turns each change into an ATT notification. There is no "send notification"
    method -- emitting the signal *is* sending it.
    """

    def __init__(
        self, path, uuid, service_path, flags,
        value=b"", on_read=None, on_write=None,
    ):
        super().__init__(GATT_CHRC_IFACE)
        self.path = path
        self.descriptors = []
        self._uuid = uuid
        self._service = service_path
        self._flags = list(flags)
        self._value = bytearray(value)
        self._on_read = on_read
        self._on_write = on_write
        self._notifying = False

    # -- properties --------------------------------------------------------

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":  # noqa: N802
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":  # noqa: N802
        return self._service

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":  # noqa: N802
        return self._flags

    @dbus_property(access=PropertyAccess.READ)
    def Notifying(self) -> "b":  # noqa: N802
        return self._notifying

    @dbus_property()
    def Value(self) -> "ay":  # noqa: N802
        return bytes(self._value)

    @Value.setter
    def Value(self, value: "ay"):  # noqa: N802
        self._value = bytearray(value)

    # -- methods -----------------------------------------------------------

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":  # noqa: N802
        if self._on_read is not None:
            return bytes(self._on_read())
        return bytes(self._value)

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}"):  # noqa: N802
        data = bytes(value)
        self._value = bytearray(data)
        if self._on_write is not None:
            try:
                self._on_write(data)
            except Exception:
                # A host writing nonsense to a control point must not take the
                # link down; every HOGP host writes at least Protocol Mode.
                log.debug("Write handler failed on %s", self._uuid, exc_info=True)

    @method()
    def StartNotify(self):  # noqa: N802
        self._notifying = True
        log.debug("Notifications enabled on %s", self._uuid)

    @method()
    def StopNotify(self):  # noqa: N802
        self._notifying = False
        log.debug("Notifications disabled on %s", self._uuid)

    # -- notification ------------------------------------------------------

    @property
    def notifying(self):
        return self._notifying

    def notify(self, payload):
        """Push a new value to the host as an ATT notification.

        Emitting ``PropertiesChanged`` for ``Value`` is what BlueZ turns into
        the notification -- there is no separate send call.

        **Deliberately not gated on ``_notifying``.** That flag is set by
        BlueZ calling StartNotify, which happens when a client *writes* the
        CCCD -- and for a **bonded** device the CCCD is persistent, so a
        console that subscribed once does not write it again on reconnect. Our
        flag then stays false forever after the first disconnect and every
        report is discarded before it leaves the process.

        Measured: 223 reports delivered on the first connection, then 1226
        dropped across the reconnect, with the link live and encrypted the
        whole time.

        BlueZ already knows which clients are subscribed and forwards only to
        those, so emitting unconditionally is correct rather than merely
        harmless -- the filtering belongs where the CCCD state actually lives.
        """
        self._value = bytearray(payload)
        self.emit_properties_changed({"Value": bytes(self._value)})
        return True

    def properties(self):
        props = {
            "UUID": Variant("s", self._uuid),
            "Service": Variant("o", self._service),
            "Flags": Variant("as", self._flags),
        }
        if "notify" in self._flags:
            props["Notifying"] = Variant("b", self._notifying)
        return props


class Service(ServiceInterface):
    """A primary GATT service."""

    def __init__(self, path, uuid, primary=True):
        super().__init__(GATT_SERVICE_IFACE)
        self.path = path
        self.characteristics = []
        self._uuid = uuid
        self._primary = primary

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":  # noqa: N802
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":  # noqa: N802
        return self._primary

    def properties(self):
        return {
            "UUID": Variant("s", self._uuid),
            "Primary": Variant("b", self._primary),
        }


class Application(ServiceInterface):
    """The root object BlueZ reads the whole tree from.

    ``GetManagedObjects`` is called **once**, at registration, so the tree must
    be complete before ``RegisterApplication``. A characteristic added later is
    simply not there as far as the host is concerned, with nothing logged
    anywhere to say so.
    """

    def __init__(self, path):
        super().__init__(OBJECT_MANAGER_IFACE)
        self.path = path
        self.services = []

    def add_service(self, service):
        self.services.append(service)
        return service

    def export(self, bus):
        """Export every object in the tree onto the bus, root last.

        Order matters only in that everything must be exported before BlueZ
        calls GetManagedObjects; exporting the root last keeps it impossible to
        answer that call with a partial tree.
        """
        for service in self.services:
            for characteristic in service.characteristics:
                for descriptor in characteristic.descriptors:
                    bus.export(descriptor.path, descriptor)
                bus.export(characteristic.path, characteristic)
            bus.export(service.path, service)
        bus.export(self.path, self)

    def unexport(self, bus):
        bus.unexport(self.path)
        for service in self.services:
            for characteristic in service.characteristics:
                for descriptor in characteristic.descriptors:
                    bus.unexport(descriptor.path)
                bus.unexport(characteristic.path)
            bus.unexport(service.path)

    @method()
    def GetManagedObjects(self) -> "a{oa{sa{sv}}}":  # noqa: N802
        objects = {}
        for service in self.services:
            objects[service.path] = {GATT_SERVICE_IFACE: service.properties()}
            for characteristic in service.characteristics:
                objects[characteristic.path] = {
                    GATT_CHRC_IFACE: characteristic.properties()
                }
                for descriptor in characteristic.descriptors:
                    objects[descriptor.path] = {
                        GATT_DESC_IFACE: descriptor.properties()
                    }
        return objects

"""The LE advertisement: what a scanning console sees before anything connects.

DO NOT add ``from __future__ import annotations`` -- see gatt.py.

This is the BLE counterpart of the Classic class-of-device plus EIR, and it is
the whole of what a console filters on when it scans and connects to whatever
it recognises. The real 8BitDo 64 that pairs with an Analogue 3D advertises
exactly this shape, captured from the air:

    Flags: 0x05  (LE Limited Discoverable Mode, BR/EDR Not Supported)
    Appearance: Gamepad (0x03c4)
    16-bit Service UUIDs (complete): Human Interface Device (0x1812)
    Name (complete): 8BitDo 64 BT

``Flags`` is set by BlueZ from ``Discoverable`` and the adapter's mode rather
than by us, so the three fields below are the ones we control.
"""

import logging

from dbus_next import Variant
from dbus_next.constants import PropertyAccess
from dbus_next.service import ServiceInterface, dbus_property, method

from server.bt.ble import hogp

log = logging.getLogger(__name__)

LE_ADVERTISEMENT_IFACE = "org.bluez.LEAdvertisement1"

#: Bluetooth SIG Appearance value for a gamepad. A console that filters on
#: appearance -- cheap for it to do, since it is in the advertisement itself --
#: sees this before it ever connects.
APPEARANCE_GAMEPAD = 0x03C4


class Advertisement(ServiceInterface):
    """An ``org.bluez.LEAdvertisement1`` for a HID peripheral."""

    def __init__(self, path, local_name, appearance=APPEARANCE_GAMEPAD,
                 service_uuids=(hogp.HID_SERVICE_UUID,), on_release=None):
        super().__init__(LE_ADVERTISEMENT_IFACE)
        self.path = path
        self._local_name = local_name
        self._appearance = appearance
        self._service_uuids = list(service_uuids)
        self._on_release = on_release
        self._tx_power = 0

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":  # noqa: N802
        # "peripheral" is connectable and general-discoverable. "broadcast"
        # would advertise without ever accepting a connection, which is a
        # beacon rather than a controller.
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> "as":  # noqa: N802
        return self._service_uuids

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":  # noqa: N802
        return self._local_name

    @dbus_property(access=PropertyAccess.READ)
    def Appearance(self) -> "q":  # noqa: N802
        return self._appearance

    @dbus_property(access=PropertyAccess.READ)
    def Discoverable(self) -> "b":  # noqa: N802
        return True

    # IncludeTxPower is deliberately absent.
    #
    # Declaring it -- even as False -- makes BlueZ go on to read a `TxPower`
    # property, and dbus-next answers an undeclared property with an error that
    # BlueZ treats as fatal: "Failed to register advertisement", with the real
    # cause only visible as an UNKNOWN_PROPERTY traceback from our own bus.
    #
    # Nothing here wants TX power anyway. It costs three bytes of a 31-byte
    # advertisement, and the budget matters: flags, appearance, the service
    # UUID and a 12-character name already come to 25.

    @dbus_property()
    def TxPower(self) -> "n":  # noqa: N802
        """Transmit power to advertise, in dBm.

        **Read-write, because BlueZ writes it back.** It reads the value we
        ask for, then sets the property to the power the controller actually
        selected. Declaring it read-only fails registration with "the property
        is readonly" -- and BlueZ reports only "Failed to register
        advertisement", so the useful half of that exchange is visible solely
        as a traceback on our own bus.

        Declared because **BlueZ reads it whether or not we want it**, and
        dbus-next answers an undeclared property with an error BlueZ treats as
        fatal: registration fails with "Failed to register advertisement", and
        the only trace of the real cause is an UNKNOWN_PROPERTY traceback on
        our own bus. Omitting the deprecated IncludeTxPower does not stop the
        probe -- measured both ways.

        Zero rather than a real figure: we do not know this dongle's output
        power, and a wrong value is worse than a neutral one for any host doing
        path-loss estimation from it.
        """
        return self._tx_power

    @TxPower.setter
    def TxPower(self, value: "n"):  # noqa: N802
        # What the controller actually chose. Kept so it can be reported, not
        # acted on.
        self._tx_power = int(value)

    @dbus_property(access=PropertyAccess.READ)
    def Includes(self) -> "as":  # noqa: N802
        """Which optional fields BlueZ should add to the advertisement.

        Empty on purpose. The 31-byte budget is nearly spent: flags,
        appearance, the 16-bit service UUID and a 12-character name already
        come to about 25, and anything that overflows silently truncates the
        name -- which is one of the two things a console matches on.
        """
        return []

    @method()
    def Release(self):  # noqa: N802
        log.debug("BlueZ released advertisement %s", self.path)
        if self._on_release is not None:
            self._on_release()

    def properties(self):
        return {
            "Type": Variant("s", "peripheral"),
            "ServiceUUIDs": Variant("as", self._service_uuids),
            "LocalName": Variant("s", self._local_name),
            "Appearance": Variant("q", self._appearance),
            "Discoverable": Variant("b", True),
        }

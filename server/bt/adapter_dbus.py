"""Per-adapter BlueZ control over D-Bus.

Everything here exists because **`bluetoothctl` operates on the default adapter
only**. Running `bluetoothctl pairable on` in a multi-adapter system silently
configures exactly one radio and leaves the rest untouched -- which showed up on
real hardware as two of four dongles being discoverable but refusing to pair,
with the host reporting only "We didn't get any response from the device".

`hciconfig` reaches the right adapter but only the HCI layer. It cannot set
`Adapter1.Pairable`, and bluetoothd rejects pairing regardless of scan mode when
that property is false.

D-Bus is the only interface that is both per-adapter and reaches the properties
that matter: each adapter is its own object at ``/org/bluez/hciX``.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"


class DBusUnavailable(RuntimeError):
    """dbus-next is missing, or the system bus cannot be reached."""


async def _connect():
    try:
        from dbus_next import BusType
        from dbus_next.aio import MessageBus
    except ImportError as exc:
        raise DBusUnavailable(
            "dbus-next is required for adapter control. "
            'Install it with: pip install -e ".[server]"'
        ) from exc

    try:
        return await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as exc:
        raise DBusUnavailable(f"Could not reach the system bus: {exc}") from exc


async def _adapter_interface(bus, hci_name: str):
    path = f"/{BLUEZ.replace('.', '/')}/{hci_name}"
    introspection = await bus.introspect(BLUEZ, path)
    obj = bus.get_proxy_object(BLUEZ, path, introspection)
    return obj.get_interface(ADAPTER_IFACE)


async def set_properties(
    hci_name: str,
    *,
    alias: str | None = None,
    pairable: bool | None = None,
    discoverable: bool | None = None,
    timeout_s: int | None = None,
) -> bool:
    """Configure one adapter. Returns True on success.

    Ordering matters: timeouts are set **before** the flags they govern.
    BlueZ applies the current timeout when a flag is switched on, so setting
    `Discoverable` first and the timeout second leaves the adapter running on
    the old timeout -- usually the 180 s default, which expires mid-pairing.
    """
    try:
        bus = await _connect()
    except DBusUnavailable as exc:
        log.error("%s", exc)
        return False

    try:
        adapter = await _adapter_interface(bus, hci_name)

        if alias is not None:
            await adapter.set_alias(alias)

        if timeout_s is not None:
            # 0 means "no timeout" to BlueZ, which is what we want when the
            # operator asks for a long pairing window.
            value = max(0, int(timeout_s))
            await adapter.set_discoverable_timeout(value)
            await adapter.set_pairable_timeout(value)

        if pairable is not None:
            await adapter.set_pairable(bool(pairable))

        if discoverable is not None:
            await adapter.set_discoverable(bool(discoverable))

        return True
    except Exception as exc:
        log.error("Could not configure %s over D-Bus: %s", hci_name, exc)
        return False
    finally:
        _disconnect(bus)


async def read_properties(hci_name: str) -> dict[str, object]:
    """Read back what an adapter is actually advertising.

    Used for diagnostics: "discoverable but not pairable" is invisible from
    `hciconfig`, which is what made the original bug hard to see.
    """
    try:
        bus = await _connect()
    except DBusUnavailable:
        return {}

    try:
        adapter = await _adapter_interface(bus, hci_name)
        return {
            "alias": await adapter.get_alias(),
            "address": await adapter.get_address(),
            "powered": await adapter.get_powered(),
            "pairable": await adapter.get_pairable(),
            "discoverable": await adapter.get_discoverable(),
        }
    except Exception as exc:
        log.debug("Could not read properties for %s: %s", hci_name, exc)
        return {}
    finally:
        _disconnect(bus)


async def remove_bonds(hci_name: str) -> int:
    """Remove every pairing on **this adapter only**. Returns how many.

    Scoped per adapter deliberately: clearing bonds machine-wide when the
    operator puts one adapter into pairing mode would disconnect consoles that
    are happily playing on the others.
    """
    try:
        bus = await _connect()
    except DBusUnavailable:
        return 0

    try:
        adapter_path = f"/{BLUEZ.replace('.', '/')}/{hci_name}"
        adapter = await _adapter_interface(bus, hci_name)

        introspection = await bus.introspect(BLUEZ, "/")
        root = bus.get_proxy_object(BLUEZ, "/", introspection)
        manager = root.get_interface(OBJECT_MANAGER)
        objects = await manager.call_get_managed_objects()

        removed = 0
        for path, interfaces in objects.items():
            if DEVICE_IFACE not in interfaces:
                continue
            # Devices live beneath their adapter, so the prefix identifies
            # which radio a bond belongs to.
            if not path.startswith(adapter_path + "/"):
                continue
            try:
                await adapter.call_remove_device(path)
                removed += 1
                log.info("Removed pairing %s from %s", path.rsplit("/", 1)[-1], hci_name)
            except Exception as exc:
                log.debug("Could not remove %s: %s", path, exc)

        return removed
    except Exception as exc:
        log.debug("Could not enumerate bonds on %s: %s", hci_name, exc)
        return 0
    finally:
        _disconnect(bus)


async def set_device_trusted(device_path: str, trusted: bool = True) -> bool:
    """Mark a paired device trusted.

    An untrusted device must be authorized on every connect. For a controller
    that means unattended reconnection after a restart would stall waiting for
    an operator who is not watching.
    """
    try:
        bus = await _connect()
    except DBusUnavailable:
        return False

    try:
        introspection = await bus.introspect(BLUEZ, device_path)
        obj = bus.get_proxy_object(BLUEZ, device_path, introspection)
        device = obj.get_interface(DEVICE_IFACE)
        await device.set_trusted(bool(trusted))
        log.info("Marked %s trusted", device_path.rsplit("/", 1)[-1])
        return True
    except Exception as exc:
        log.debug("Could not set Trusted on %s: %s", device_path, exc)
        return False
    finally:
        _disconnect(bus)


def _disconnect(bus) -> None:
    try:
        bus.disconnect()
    except Exception:
        pass


def is_available() -> bool:
    try:
        import dbus_next  # noqa: F401

        return True
    except ImportError:
        return False

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
    connectable: bool | None = None,
    pairable: bool | None = None,
    discoverable: bool | None = None,
    timeout_s: int | None = None,
) -> bool:
    """Configure one adapter. Returns True on success.

    Ordering matters twice over.

    **Timeouts are set before the flags they govern.** BlueZ applies the current
    timeout when a flag is switched on, so setting `Discoverable` first and the
    timeout second leaves the adapter running on the old timeout -- usually the
    180 s default, which expires mid-pairing.

    **`Connectable` is set before `Discoverable`.** It is the property behind
    *page scan*; an adapter with it false does not answer pages at all, and a
    host trying to reach one reports "We didn't get any response from the
    device" -- the same sentence a dozen unrelated faults produce. BlueZ will
    not hold `Discoverable` on a non-connectable adapter either, so the wrong
    order silently drops the discoverable request.
    """
    try:
        bus = await _connect()
    except DBusUnavailable as exc:
        log.error("%s", exc)
        return False

    failed: list[str] = []

    async def write(name: str, value, *, current=None) -> None:
        """Set one property, unless it already holds `value`.

        **Skipping the no-op is required, not an optimisation.** BlueZ rejects
        a write of the value a property already has -- `Connectable` certainly,
        and with an *empty* `DBusError('')`, so the log line reads
        "Could not configure hci2 over D-Bus:" and stops there.

        Read-then-write also keeps us from fighting bluetoothd for state it
        owns, the same discipline `_ensure_pairing_settings` follows.
        """
        if current is not None and current == value:
            return
        try:
            await getattr(adapter, f"set_{name}")(value)
        except Exception as exc:
            failed.append(name)
            log.error(
                "Could not set %s=%r on %s: %s",
                name, value, hci_name, exc or type(exc).__name__,
            )

    try:
        adapter = await _adapter_interface(bus, hci_name)
    except Exception as exc:
        log.error("Could not reach %s over D-Bus: %s", hci_name, exc)
        _disconnect(bus)
        return False

    try:
        if alias is not None:
            await write("alias", alias, current=await adapter.get_alias())

        if timeout_s is not None:
            # 0 means "no timeout" to BlueZ, which is what we want when the
            # operator asks for a long pairing window.
            value = max(0, int(timeout_s))
            await write(
                "discoverable_timeout", value,
                current=await adapter.get_discoverable_timeout(),
            )
            await write(
                "pairable_timeout", value,
                current=await adapter.get_pairable_timeout(),
            )

        # Each property is written independently. They were one try block, so
        # the first failure skipped the rest -- a rejected no-op write of
        # `Connectable` silently cancelled the `Pairable` and `Discoverable`
        # that were the entire point of the call, and every pairing window
        # opened on an already-connectable adapter did nothing at all.
        if connectable is not None:
            await write(
                "connectable", bool(connectable),
                current=await adapter.get_connectable(),
            )

        if pairable is not None:
            await write(
                "pairable", bool(pairable), current=await adapter.get_pairable()
            )

        if discoverable is not None:
            await write(
                "discoverable", bool(discoverable),
                current=await adapter.get_discoverable(),
            )

        return not failed
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


async def remove_bonds(hci_name: str) -> list[str]:
    """Remove every pairing on **this adapter only**. Returns the addresses.

    Scoped per adapter deliberately: clearing bonds machine-wide when the
    operator puts one adapter into pairing mode would disconnect consoles that
    are happily playing on the others.

    Returns *which* hosts were forgotten, not just how many. The caller has to
    stop trying to reconnect to them: dropping the link key while still holding
    the address means paging a host that can no longer accept us, forever.
    """
    try:
        bus = await _connect()
    except DBusUnavailable:
        return []

    try:
        adapter_path = f"/{BLUEZ.replace('.', '/')}/{hci_name}"
        adapter = await _adapter_interface(bus, hci_name)

        introspection = await bus.introspect(BLUEZ, "/")
        root = bus.get_proxy_object(BLUEZ, "/", introspection)
        manager = root.get_interface(OBJECT_MANAGER)
        objects = await manager.call_get_managed_objects()

        removed: list[str] = []
        for path, interfaces in objects.items():
            if DEVICE_IFACE not in interfaces:
                continue
            # Devices live beneath their adapter, so the prefix identifies
            # which radio a bond belongs to.
            if not path.startswith(adapter_path + "/"):
                continue
            try:
                await adapter.call_remove_device(path)
                # dev_AA_BB_CC_DD_EE_FF -> AA:BB:CC:DD:EE:FF
                leaf = path.rsplit("/", 1)[-1]
                removed.append(leaf.removeprefix("dev_").replace("_", ":").upper())
                log.info("Removed pairing %s from %s", leaf, hci_name)
            except Exception as exc:
                log.debug("Could not remove %s: %s", path, exc)

        return removed
    except Exception as exc:
        log.debug("Could not enumerate bonds on %s: %s", hci_name, exc)
        return []
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

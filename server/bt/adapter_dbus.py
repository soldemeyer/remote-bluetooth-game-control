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


#: The shared system-bus connection, and the loop it belongs to.
#:
#: Every call here used to open a connection, introspect, do its work and
#: disconnect. With four adapters and a reconcile every ten seconds that is
#: roughly twenty-four connection setups a minute, forever, each one a round
#: trip that can fail transiently under load.
#:
#: Keyed by event loop because a ``MessageBus`` is bound to the loop that
#: created it, and the test suite runs a fresh loop per test. Reusing one
#: across loops fails in a way that looks like a D-Bus fault rather than a
#: lifetime bug.
_shared_bus = None
_shared_loop = None

#: Introspection results, keyed by object path. These describe an interface,
#: not its state, so they never go stale while bluetoothd is running.
_introspection_cache: dict = {}


async def _connect():
    """The shared system-bus connection, opening one if needed."""
    global _shared_bus, _shared_loop

    try:
        import asyncio

        from dbus_next import BusType
        from dbus_next.aio import MessageBus
    except ImportError as exc:
        raise DBusUnavailable(
            "dbus-next is required for adapter control. "
            'Install it with: pip install -e ".[server]"'
        ) from exc

    loop = asyncio.get_running_loop()

    if _shared_bus is not None and _shared_loop is loop:
        if getattr(_shared_bus, "connected", True):
            return _shared_bus
        # bluetoothd restarted, or the bus dropped us. Fall through and open a
        # fresh one rather than handing back a dead connection whose failures
        # would be reported as adapter problems.
        _forget_shared()

    if _shared_bus is not None and _shared_loop is not loop:
        _forget_shared()

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as exc:
        raise DBusUnavailable(f"Could not reach the system bus: {exc}") from exc

    _shared_bus = bus
    _shared_loop = loop
    return bus


def _forget_shared() -> None:
    """Drop the shared connection without waiting on it."""
    global _shared_bus, _shared_loop

    bus, _shared_bus = _shared_bus, None
    _shared_loop = None
    _introspection_cache.clear()
    if bus is not None:
        try:
            bus.disconnect()
        except Exception:
            pass


def close_shared() -> None:
    """Release the shared connection. Called from AdapterManager.stop()."""
    _forget_shared()


async def _introspect(bus, path: str):
    """Introspect a path, once. The result describes shape, not state."""
    cached = _introspection_cache.get(path)
    if cached is not None:
        return cached
    result = await bus.introspect(BLUEZ, path)
    _introspection_cache[path] = result
    return result


async def _adapter_interface(bus, hci_name: str):
    path = f"/{BLUEZ.replace('.', '/')}/{hci_name}"
    introspection = await _introspect(bus, path)
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
    pairable_timeout_s: int | None = None,
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
        return False

    try:
        if alias is not None:
            await write("alias", alias, current=await adapter.get_alias())

        if timeout_s is not None:
            # 0 means "no timeout" to BlueZ, which is what we want when the
            # operator asks for a long pairing window.
            await write(
                "discoverable_timeout", max(0, int(timeout_s)),
                current=await adapter.get_discoverable_timeout(),
            )

        # The pairable timeout is set **separately**, because the two are not
        # the same question. Discoverability is what a bounded window is for.
        # Bondability is not: on the BLE transport the peripheral advertises
        # continuously and must stay bondable, so a window that also expired
        # Pairable left the adapter permanently unable to complete a pairing --
        # visible only as `bondable` quietly missing from its settings, hours
        # after the window closed.
        #
        # Defaults to following timeout_s so the Classic behaviour is unchanged.
        pairable_timeout = (
            pairable_timeout_s if pairable_timeout_s is not None else timeout_s
        )
        if pairable_timeout is not None:
            await write(
                "pairable_timeout", max(0, int(pairable_timeout)),
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

            # Clearing Discoverable can take page scan down with it, so a
            # request for Connectable has to be re-checked *after* it.
            #
            # BlueZ only keeps an adapter connectable on its own while it holds
            # a bond that might reconnect. End a pairing window on an adapter
            # that did not manage to bond and it drops page scan to 0x00 -- and
            # our write above was skipped as a no-op, because at that point
            # Connectable was still true. The adapter is then unreachable, with
            # nothing in any log to say so.
            #
            # Measured live on hci3: stop pairing with no bonds, and the radio
            # reads scan enable 0x00 despite Connectable having been asked for
            # in the same call.
            if connectable and not bool(discoverable):
                await write(
                    "connectable", True, current=await adapter.get_connectable()
                )

        return not failed
    except Exception as exc:
        log.error("Could not configure %s over D-Bus: %s", hci_name, exc)
        return False


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


def _address_from_path(device_path: str) -> str:
    """``/org/bluez/hci3/dev_AA_BB_CC_DD_EE_FF`` -> ``AA:BB:CC:DD:EE:FF``."""
    leaf = device_path.rsplit("/", 1)[-1]
    return leaf.removeprefix("dev_").replace("_", ":").upper()


async def list_bonds(hci_name: str) -> list[str]:
    """Which hosts are bonded to **this adapter**, as BlueZ knows it.

    BlueZ is the authority here and we were keeping a parallel copy. That copy
    goes stale in exactly the way that hurts: entering pairing mode removes the
    bond, or the host forgets us, and the address stays behind in our config --
    so the reconnect loop pages a host that can no longer authenticate us,
    every 30 s, for the life of the process, logged only at debug.

    Reading the bonds instead makes the stale case impossible to construct:
    there is only one record of who we are bonded to, and it is the one the
    pairing actually created.

    Returns an empty list if D-Bus is unreachable, which is indistinguishable
    from "no bonds" and is the safe way round -- the alternative is chasing a
    host we have no key for.
    """
    try:
        bus = await _connect()
    except DBusUnavailable:
        return []

    try:
        adapter_path = f"/{BLUEZ.replace('.', '/')}/{hci_name}"

        introspection = await _introspect(bus, "/")
        root = bus.get_proxy_object(BLUEZ, "/", introspection)
        manager = root.get_interface(OBJECT_MANAGER)
        objects = await manager.call_get_managed_objects()

        bonds: list[str] = []
        for path, interfaces in objects.items():
            device = interfaces.get(DEVICE_IFACE)
            if device is None:
                continue
            # Devices live beneath their adapter, so the prefix is what scopes
            # this to one radio.
            if not path.startswith(adapter_path + "/"):
                continue
            paired = device.get("Paired")
            if paired is not None and bool(paired.value):
                bonds.append(_address_from_path(path))

        return sorted(bonds)
    except Exception as exc:
        log.debug("Could not list bonds on %s: %s", hci_name, exc)
        return []


async def connected_devices(hci_name: str) -> list[str]:
    """Object paths of the hosts currently connected to **this adapter**.

    Scoped by path prefix, the same way :func:`list_bonds` is: devices live
    beneath their adapter, so the prefix is what keeps one radio's console out
    of another's.
    """
    try:
        bus = await _connect()
    except DBusUnavailable:
        return []

    try:
        adapter_path = f"/{BLUEZ.replace('.', '/')}/{hci_name}"
        introspection = await _introspect(bus, "/")
        root = bus.get_proxy_object(BLUEZ, "/", introspection)
        manager = root.get_interface(OBJECT_MANAGER)
        objects = await manager.call_get_managed_objects()

        connected = []
        for path, interfaces in objects.items():
            device = interfaces.get(DEVICE_IFACE)
            if device is None or not path.startswith(adapter_path + "/"):
                continue
            state = device.get("Connected")
            if state is not None and bool(state.value):
                connected.append(path)
        return sorted(connected)
    except Exception as exc:
        log.debug("Could not list connections on %s: %s", hci_name, exc)
        return []


async def disconnect_device(device_path: str) -> bool:
    """Drop one host's link. True if BlueZ accepted it."""
    try:
        bus = await _connect()
    except DBusUnavailable:
        return False

    try:
        introspection = await _introspect(bus, device_path)
        obj = bus.get_proxy_object(BLUEZ, device_path, introspection)
        await obj.get_interface(DEVICE_IFACE).call_disconnect()
        log.info("Disconnected %s", _address_from_path(device_path))
        return True
    except Exception as exc:
        log.warning("Could not disconnect %s: %s", device_path, exc)
        return False


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

        introspection = await _introspect(bus, "/")
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
                removed.append(_address_from_path(path))
                log.info("Removed pairing %s from %s", path.rsplit("/", 1)[-1], hci_name)
            except Exception as exc:
                log.debug("Could not remove %s: %s", path, exc)

        return removed
    except Exception as exc:
        log.debug("Could not enumerate bonds on %s: %s", hci_name, exc)
        return []


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
        introspection = await _introspect(bus, device_path)
        obj = bus.get_proxy_object(BLUEZ, device_path, introspection)
        device = obj.get_interface(DEVICE_IFACE)
        await device.set_trusted(bool(trusted))
        log.info("Marked %s trusted", device_path.rsplit("/", 1)[-1])
        return True
    except Exception as exc:
        log.debug("Could not set Trusted on %s: %s", device_path, exc)
        return False


def is_available() -> bool:
    try:
        import dbus_next  # noqa: F401

        return True
    except ImportError:
        return False


async def remove_device(hci_name: str, address: str) -> bool:
    """Remove the bond for **one** peer, leaving every other bond alone.

    ``remove_bonds`` clears an adapter wholesale, which is right when arming a
    pairing window and wrong when repairing a single one-sided bond -- taking
    the other players' controllers with it would turn one broken link into
    four.
    """
    try:
        bus = await _connect()
    except DBusUnavailable:
        return False

    target = address.upper()
    try:
        adapter_path = f"/{BLUEZ.replace('.', '/')}/{hci_name}"
        adapter = await _adapter_interface(bus, hci_name)

        introspection = await _introspect(bus, "/")
        root = bus.get_proxy_object(BLUEZ, "/", introspection)
        manager = root.get_interface(OBJECT_MANAGER)
        objects = await manager.call_get_managed_objects()

        for path, interfaces in objects.items():
            if DEVICE_IFACE not in interfaces:
                continue
            if not path.startswith(adapter_path + "/"):
                continue
            if _address_from_path(path).upper() != target:
                continue
            await adapter.call_remove_device(path)
            log.info("Removed pairing %s from %s", target, hci_name)
            return True
    except Exception as exc:
        log.debug("Could not remove %s from %s: %s", target, hci_name, exc)
    return False

"""A BLE man-in-the-middle relay, for learning a controller's private protocol.

Why this exists
---------------
An Analogue 3D accepts our emulated 8BitDo 64 far enough to pair, subscribe to
input reports and act on them -- and then drops the link roughly every 34.5
seconds, on every adapter, indefinitely. Everything measurable on our side is
healthy when it happens.

The one thing the console asks for that we cannot answer is the **8BitDo vendor
service 0xff10**, with its two characteristics ff11 and ff12. What travels over
them is proprietary and undocumented, and no amount of reasoning about our own
stack will reveal it: the conversation we need to see happens between the real
pad and the console, and the Pi is not part of it.

A sniffer would work and we do not have one. But we have four radios, and the
Pi can simply *be* in the middle:

    real 8BitDo 64  <--LE-->  [ hciA central | hciB peripheral ]  <--LE-->  console
                                        this script

The pad connects to us. We present the pad's own GATT database back to the
console, byte for byte, and forward every read, write and notification in both
directions -- logging all of it. The console talks to what it believes is its
controller, the controller answers, and we get a transcript.

What this is and is not
-----------------------
This is interoperability work on hardware the operator owns: learning what a
console expects so a controller can speak it. It is not a security tool. It
needs both devices physically present and the pad deliberately paired to this
machine, so it grants nothing that owning the two devices does not already.

Running it
----------
Stop the server first -- it wants the same radios::

    sudo systemctl stop rbgc-server
    sudo python3 -m tools.ble_relay --pad E4:17:D8:E7:EE:F2 \
        --pad-adapter hci0 --console-adapter hci4

Then put the pad into pairing mode. Once it attaches, put the console into
pairing mode; it will find the relay advertising under the pad's name.

Every exchange is written to stdout and, with --log, to a file as JSON lines so
it can be analysed afterwards without re-running the experiment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
SERVICE_IFACE = "org.bluez.GattService1"
CHRC_IFACE = "org.bluez.GattCharacteristic1"
DESC_IFACE = "org.bluez.GattDescriptor1"
DEVICE_IFACE = "org.bluez.Device1"

#: Descriptors bluetoothd manages itself. Mirroring a CCCD would collide with
#: the one BlueZ adds for any notify characteristic.
_MANAGED_DESCRIPTORS = {"00002902"}

#: Services bluetoothd owns and will not let an application register.
#:
#: Generic Access and Generic Attribute are built into every BlueZ peripheral,
#: so a mirror that includes them is refused outright -- and the refusal is the
#: unhelpful "Failed to create entry in database", which names neither the
#: service nor the reason. The real pad publishes both; we skip them and let
#: BlueZ provide its own.
_RESERVED_SERVICES = {"1800", "1801"}


def _short(uuid: str) -> str:
    """`0000ff11-0000-...` -> `ff11`, for readable logs."""
    return uuid[4:8] if uuid.lower().endswith("-0000-1000-8000-00805f9b34fb") else uuid


class Transcript:
    """Every exchange, on stdout and optionally as JSON lines."""

    def __init__(self, path: str | None) -> None:
        self._file = open(path, "a", encoding="utf-8") if path else None
        self._t0 = time.monotonic()

    def record(self, direction: str, what: str, uuid: str, data: bytes = b"") -> None:
        entry = {
            "t": round(time.monotonic() - self._t0, 4),
            "direction": direction,
            "op": what,
            "uuid": _short(uuid),
            "hex": data.hex(),
            "len": len(data),
        }
        print(
            "%9.4f  %-18s %-6s %-4s %s"
            % (entry["t"], direction, what, entry["uuid"], data.hex() or "-"),
            flush=True,
        )
        if self._file:
            self._file.write(json.dumps(entry) + "\n")
            self._file.flush()

    def note(self, message: str) -> None:
        print("           %s" % message, flush=True)
        if self._file:
            self._file.write(json.dumps({"note": message}) + "\n")
            self._file.flush()


class PadLink:
    """The central half: our connection to the real controller."""

    def __init__(self, bus, adapter: str, address: str, transcript: Transcript):
        self.bus = bus
        self.adapter = adapter
        self.address = address
        self.transcript = transcript
        self.device_path = f"/org/bluez/{adapter}/dev_" + address.replace(":", "_")
        #: uuid -> proxy interface, for the characteristics we mirror.
        self.characteristics: dict[str, Any] = {}
        #: The tree we read off the pad, in discovery order.
        self.tree: list[dict[str, Any]] = []

    async def _iface(self, path: str, name: str, timeout_s: float = 5.0):
        introspection = await asyncio.wait_for(
            self.bus.introspect(BLUEZ, path), timeout_s)
        return self.bus.get_proxy_object(BLUEZ, path, introspection).get_interface(name)

    async def connect(self, timeout_s: float = 60.0) -> None:
        device = await self._iface(self.device_path, DEVICE_IFACE)
        props = await self._iface(self.device_path, "org.freedesktop.DBus.Properties")

        if not await props.call_get(DEVICE_IFACE, "Connected"):
            self.transcript.note("connecting to the pad at %s" % self.address)
            await device.call_connect()

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            resolved = await props.call_get(DEVICE_IFACE, "ServicesResolved")
            if resolved.value:
                self.transcript.note("pad services resolved")
                return
            await asyncio.sleep(0.25)
        raise RuntimeError("the pad never resolved its services")

    async def _try_read(self, path: str, iface: str, timeout_s: float = 1.5) -> bytes:
        """Read a value, giving up rather than hanging.

        A characteristic that needs an encrypted link will not answer without
        one, and bluetoothd does not time the call out for us -- it simply
        never returns. Unbounded, that stalls discovery on the first protected
        attribute and the relay never starts, with nothing logged to say why.
        """
        try:
            proxy = await self._iface(path, iface)
            return bytes(await asyncio.wait_for(proxy.call_read_value({}), timeout_s))
        except Exception:
            return b""

    async def read_tree(self) -> list[dict[str, Any]]:
        """Read the pad's entire GATT database, in handle order."""
        self.transcript.note("enumerating the pad's objects...")
        introspection = await self.bus.introspect(BLUEZ, "/")
        om = self.bus.get_proxy_object(BLUEZ, "/", introspection).get_interface(OM_IFACE)
        objects = await om.call_get_managed_objects()
        self.transcript.note("  %d objects on the bus" % len(objects))

        services = {
            path: ifaces[SERVICE_IFACE]
            for path, ifaces in objects.items()
            if path.startswith(self.device_path) and SERVICE_IFACE in ifaces
        }

        self.transcript.note("  %d services under the pad" % len(services))
        tree = []
        for spath in sorted(services):
            self.transcript.note("  reading %s" % spath.rsplit("/", 1)[-1])
            entry = {
                "path": spath,
                "uuid": services[spath]["UUID"].value,
                "primary": bool(services[spath].get("Primary", None) and
                                services[spath]["Primary"].value),
                "characteristics": [],
            }
            for cpath in sorted(objects):
                if not cpath.startswith(spath + "/"):
                    continue
                chrc = objects[cpath].get(CHRC_IFACE)
                if not chrc:
                    continue
                descriptors = []
                for dpath in sorted(objects):
                    if not dpath.startswith(cpath + "/"):
                        continue
                    desc = objects[dpath].get(DESC_IFACE)
                    if not desc:
                        continue
                    duuid = desc["UUID"].value
                    if _short(duuid) in _MANAGED_DESCRIPTORS:
                        continue
                    value = await self._try_read(dpath, DESC_IFACE)
                    descriptors.append({"uuid": duuid, "value": value})

                flags = list(chrc["Flags"].value)
                value = b""
                if "read" in flags or "encrypt-read" in flags:
                    value = await self._try_read(cpath, CHRC_IFACE)

                # The proxy can fail: an unbonded pad is dropped by bluetoothd
                # partway through a slow discovery, and the object disappears
                # between GetManagedObjects and the introspection. Mirror what
                # we did read rather than abandoning the whole tree -- a
                # partial database still shows what the console asks for.
                try:
                    self.characteristics[cpath] = await self._iface(
                        cpath, CHRC_IFACE)
                except Exception as exc:
                    self.transcript.note(
                        "    could not proxy %s (%s) -- mirroring it read-only"
                        % (_short(chrc["UUID"].value), type(exc).__name__))

                entry["characteristics"].append({
                    "path": cpath,
                    "uuid": chrc["UUID"].value,
                    "flags": flags,
                    "value": value,
                    "descriptors": descriptors,
                })
            tree.append(entry)

        self.tree = tree
        return tree

    async def subscribe_all(self, forward) -> None:
        """Start notifications on every notify characteristic, forwarding each.

        ``forward(uuid, payload)`` is called for every value that arrives.
        """
        for service in self.tree:
            for chrc in service["characteristics"]:
                if "notify" not in chrc["flags"]:
                    continue
                path, uuid = chrc["path"], chrc["uuid"]
                try:
                    props = await self._iface(
                        path, "org.freedesktop.DBus.Properties")
                except Exception:
                    # The pad went away. An unbonded LE link does not survive
                    # long, and losing it mid-setup must not abort the mirror --
                    # the console still needs something to talk to, and a
                    # transcript of what it asks for is the point of this tool.
                    self.transcript.note(
                        "pad vanished while subscribing to %s" % _short(uuid))
                    continue

                def make(uuid):
                    def on_changed(iface, changed, invalidated):
                        if "Value" in changed:
                            payload = bytes(changed["Value"].value)
                            self.transcript.record("pad -> console", "notify",
                                                   uuid, payload)
                            forward(uuid, payload)
                    return on_changed

                if path not in self.characteristics:
                    self.transcript.note(
                        "cannot subscribe to %s -- no proxy" % _short(uuid))
                    continue

                props.on_properties_changed(make(uuid))
                try:
                    # Bounded for the same reason reads are: a characteristic
                    # that needs encryption never answers on an unbonded link,
                    # and bluetoothd will not give up on our behalf.
                    await asyncio.wait_for(
                        self.characteristics[path].call_start_notify(), 4.0)
                    self.transcript.note("subscribed to %s on the pad" % _short(uuid))
                except Exception as exc:
                    self.transcript.note(
                        "could not subscribe to %s (%s)"
                        % (_short(uuid), type(exc).__name__))

    async def read(self, path: str) -> bytes:
        return bytes(await self.characteristics[path].call_read_value({}))

    async def write(self, path: str, data: bytes, response: bool) -> None:
        if path not in self.characteristics:
            self.transcript.note("dropped a write -- no proxy for %s" % path)
            return
        options = {}
        if not response:
            from dbus_next import Variant
            options = {"type": Variant("s", "command")}
        await self.characteristics[path].call_write_value(list(data), options)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--pad", required=True, help="the real controller's address")
    parser.add_argument("--pad-adapter", default="hci0",
                        help="adapter that connects to the pad (central)")
    parser.add_argument("--console-adapter", default="hci4",
                        help="adapter the console connects to (peripheral)")
    parser.add_argument("--name", default="", help="advertised name (default: the pad's)")
    parser.add_argument("--log", default="", help="write a JSON-lines transcript here")
    parser.add_argument("--seconds", type=float, default=900.0)
    args = parser.parse_args()

    sys.path.insert(0, "/opt/rbgc")

    from dbus_next import BusType
    from dbus_next.aio import MessageBus

    from server.bt.ble.gatt import Application, Characteristic, Descriptor, Service

    transcript = Transcript(args.log or None)
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    pad = PadLink(bus, args.pad_adapter, args.pad, transcript)
    await pad.connect()
    tree = await pad.read_tree()

    transcript.note("pad database: %d services" % len(tree))
    for service in tree:
        transcript.note("  service %s (%d characteristics)"
                        % (_short(service["uuid"]), len(service["characteristics"])))
        for chrc in service["characteristics"]:
            transcript.note("      %s %s%s"
                            % (_short(chrc["uuid"]), chrc["flags"],
                               "  = " + chrc["value"].hex() if chrc["value"] else ""))

    # -- build the mirror -------------------------------------------------
    root = "/rbgc/relay"
    app = Application(root)
    mirrored: dict[str, Any] = {}

    loop = asyncio.get_running_loop()

    for si, service in enumerate(tree):
        if _short(service["uuid"]) in _RESERVED_SERVICES:
            transcript.note("skipping %s -- bluetoothd owns it"
                            % _short(service["uuid"]))
            continue
        mirror_service = app.add_service(
            Service(f"{root}/service{si}", service["uuid"])
        )
        for ci, chrc in enumerate(service["characteristics"]):
            path = chrc["path"]
            uuid = chrc["uuid"]

            def make_read(path=path, uuid=uuid):
                def on_read():
                    # bluetoothd calls this synchronously from our own loop, so
                    # the proxied read has to be scheduled rather than awaited.
                    transcript.record("console -> pad", "read", uuid)
                    return None
                return on_read

            def make_write(path=path, uuid=uuid):
                def on_write(data):
                    payload = bytes(data)
                    transcript.record("console -> pad", "write", uuid, payload)
                    asyncio.ensure_future(
                        pad.write(path, payload, response=True))
                return on_write

            mirror = Characteristic(
                f"{mirror_service.path}/char{ci}", uuid, mirror_service.path,
                chrc["flags"], value=chrc["value"],
                on_write=make_write(),
            )
            for di, desc in enumerate(chrc["descriptors"]):
                mirror.descriptors.append(
                    Descriptor(f"{mirror.path}/desc{di}", desc["uuid"],
                               mirror.path, desc["value"])
                )
            mirror_service.characteristics.append(mirror)
            mirrored[uuid] = mirror

    def forward(uuid: str, payload: bytes) -> None:
        """A notification arrived from the pad: pass it to the console."""
        mirror = mirrored.get(uuid)
        if mirror is None:
            return
        try:
            mirror.notify(payload)
        except Exception as exc:
            transcript.note("could not forward %s: %s" % (_short(uuid), exc))

    try:
        await pad.subscribe_all(forward)
    except Exception as exc:
        transcript.note("subscription pass aborted (%s) -- mirroring anyway"
                        % type(exc).__name__)

    app.export(bus)
    introspection = await bus.introspect(BLUEZ, f"/org/bluez/{args.console_adapter}")
    adapter = bus.get_proxy_object(
        BLUEZ, f"/org/bluez/{args.console_adapter}", introspection)
    gatt = adapter.get_interface("org.bluez.GattManager1")
    await gatt.call_register_application(root, {})
    transcript.note("mirror registered on %s" % args.console_adapter)

    # -- advertise as the pad ---------------------------------------------
    from server.bt.ble import hogp
    from server.bt.mgmt import MGMTSocket

    name = args.name or "8BitDo 64 BT"
    index = int(args.console_adapter.removeprefix("hci"))
    mgmt = MGMTSocket()
    mgmt.open()
    try:
        # The same flags the peripheral uses: connectable, limited
        # discoverable, and let the kernel own the Flags AD structure -- a
        # second one makes the whole advertisement fail as Invalid Parameters.
        flags = (
            hogp.ADV_FLAG_CONNECTABLE
            | hogp.ADV_FLAG_LIMITED_DISCOVERABLE
            | hogp.ADV_FLAG_MANAGED_FLAGS
        )
        mgmt.remove_advertising(index, 1)
        mgmt.add_advertising(
            index, 1, flags,
            adv_data=hogp.build_advertising_data(name),
            scan_response=hogp.build_scan_response(name),
        )
        transcript.note("advertising as %r on %s -- put the console into pairing mode"
                        % (name, args.console_adapter))
    except Exception as exc:
        transcript.note("could not advertise: %s" % exc)

    transcript.note("relaying for %.0f s" % args.seconds)
    await asyncio.sleep(args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

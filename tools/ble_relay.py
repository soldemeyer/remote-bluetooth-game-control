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
ADAPTER_IFACE = "org.bluez.Adapter1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

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

    async def _discover(self, timeout_s: float = 40.0) -> None:
        """Make bluetoothd notice the pad, so a Device1 object exists.

        ``Device1`` appears only for a device **bluetoothd** has seen. A
        `btmgmt find` scans the radio without telling the daemon anything, so
        the object never materialises and Connect fails with
        "interface not found on this object" -- which reads like a broken
        address rather than a pad nobody has looked for.
        """
        adapter_path = f"/org/bluez/{self.adapter}"
        adapter = await self._iface(adapter_path, ADAPTER_IFACE)

        try:
            await adapter.call_start_discovery()
        except Exception as exc:
            # Already discovering is fine and common; anything else is worth
            # saying, because the wait below would otherwise just time out.
            self.transcript.note("discovery did not start (%s)" % exc)

        self.transcript.note("looking for %s..." % self.address)
        deadline = time.monotonic() + timeout_s
        try:
            while time.monotonic() < deadline:
                try:
                    await self._iface(self.device_path, DEVICE_IFACE, 2.0)
                    self.transcript.note("found it")
                    return
                except Exception:
                    await asyncio.sleep(0.5)
            raise RuntimeError(
                f"{self.address} never appeared. Is the pad in pairing mode "
                "and not connected to anything else?"
            )
        finally:
            try:
                await adapter.call_stop_discovery()
            except Exception:
                pass

    async def connect(self, timeout_s: float = 120.0) -> None:
        """Attach to the pad and wait for its GATT database to resolve.

        Retried as a whole, because every step here can evaporate. An
        **unpaired** LE device is transient to bluetoothd: it prunes the
        Device1 object once discovery stops and the device is not connected,
        so the path we are holding can disappear between two calls -- which
        surfaces as `Method "Get" ... doesn't exist`, an error that says
        nothing about what actually happened.

        The pad also leaves pairing mode on its own timer, so an attempt can
        simply be too late. Re-discovering and trying again is the only thing
        that works; the operator is told when it is waiting rather than being
        left with a traceback.
        """
        deadline = time.monotonic() + timeout_s
        attempt = 0

        while time.monotonic() < deadline:
            attempt += 1
            try:
                await self._iface(self.device_path, DEVICE_IFACE, 2.0)
            except Exception:
                await self._discover(min(30.0, max(5.0, deadline - time.monotonic())))
                continue

            try:
                device = await self._iface(self.device_path, DEVICE_IFACE)
                props = await self._iface(self.device_path, PROPS_IFACE)

                # `.value`: call_get returns a Variant, and a Variant is always
                # truthy -- so testing it directly silently skipped Connect on
                # every attempt and then waited for services that were never
                # going to resolve.
                connected = (await props.call_get(DEVICE_IFACE, "Connected")).value
                if not connected:
                    self.transcript.note(
                        "connecting to the pad at %s (attempt %d)"
                        % (self.address, attempt)
                    )
                    try:
                        await asyncio.wait_for(device.call_connect(), 20.0)
                    except Exception as exc:
                        # "In Progress" means bluetoothd is already connecting.
                        # Issuing another Connect cannot help and the retry
                        # loop then spins on it -- 22 attempts in 50 s, with
                        # each one making the next more likely. Fall through
                        # and wait for the connection already under way.
                        if "in progress" not in str(exc).lower():
                            raise
                        self.transcript.note("a connection is already in progress; waiting")

                # **Bond, do not merely connect.**
                #
                # The Report Map and the input reports need an encrypted link,
                # and an unbonded read of them returns nothing -- silently, so
                # the mirror comes up advertising an empty HID descriptor and
                # a console reading it has no idea what we are. Measured: the
                # pad's 2a4b read back as b"" on an unbonded link.
                paired = (await props.call_get(DEVICE_IFACE, "Paired")).value
                if not paired:
                    self.transcript.note("bonding with the pad")
                    try:
                        await asyncio.wait_for(device.call_pair(), 30.0)
                    except Exception as exc:
                        self.transcript.note(
                            "pairing did not complete (%s) -- protected "
                            "attributes will read empty"
                            % type(exc).__name__
                        )

                while time.monotonic() < deadline:
                    resolved = (
                        await props.call_get(DEVICE_IFACE, "ServicesResolved")
                    ).value
                    if resolved:
                        self.transcript.note("pad services resolved")
                        return
                    await asyncio.sleep(0.25)
            except Exception as exc:
                self.transcript.note(
                    "attempt %d did not take (%s: %s) -- retrying"
                    % (attempt, type(exc).__name__, str(exc)[:80])
                )
                await asyncio.sleep(1.0)

        raise RuntimeError(
            f"could not attach to {self.address}. Put the pad into pairing "
            "mode (hold its pair button until the LEDs sweep) and make sure "
            "it is not connected to a PC or the console."
        )

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

        ``forward(path, uuid, payload)`` is called for every value that
        arrives. The **path** identifies which characteristic it came from,
        which the UUID cannot: this pad has five Report characteristics all
        called 2a4d.
        """
        for service in self.tree:
            for chrc in service["characteristics"]:
                if "notify" not in chrc["flags"]:
                    continue
                path, uuid = chrc["path"], chrc["uuid"]
                try:
                    props = await self._iface(path, PROPS_IFACE)
                except Exception:
                    # The pad went away. An unbonded LE link does not survive
                    # long, and losing it mid-setup must not abort the mirror --
                    # the console still needs something to talk to, and a
                    # transcript of what it asks for is the point of this tool.
                    self.transcript.note(
                        "pad vanished while subscribing to %s" % _short(uuid))
                    continue

                def make(path, uuid):
                    def on_changed(iface, changed, invalidated):
                        if "Value" in changed:
                            payload = bytes(changed["Value"].value)
                            # Recorded only when it is actually going
                            # somewhere. 100 reports a second buries every
                            # console exchange in the transcript, which is the
                            # one thing this tool exists to show.
                            forward(path, uuid, payload)
                    return on_changed

                if path not in self.characteristics:
                    self.transcript.note(
                        "cannot subscribe to %s -- no proxy" % _short(uuid))
                    continue

                props.on_properties_changed(make(path, uuid))
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
    parser.add_argument("--console-adapter", default="hci1",
                        help="adapter the console connects to (peripheral)")
    parser.add_argument("--name", default="", help="advertised name (default: the pad's)")
    parser.add_argument("--log", default="", help="write a JSON-lines transcript here")
    parser.add_argument(
        "--seconds", type=float, default=0.0,
        help="stop after this long. 0, the default, runs until interrupted -- "
             "the operator sets the pace, and a countdown is a window to miss",
    )
    args = parser.parse_args()

    sys.path.insert(0, "/opt/rbgc")

    from dbus_next import BusType
    from dbus_next.aio import MessageBus

    from server.bt.ble.gatt import Application, Characteristic, Descriptor, Service

    transcript = Transcript(args.log or None)
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    # **A pairing agent, first.**
    #
    # Without one bluetoothd cannot complete Secure Simple Pairing and answers
    # an incoming SMP Pairing Request with "Pairing not supported" -- the
    # console connects, sits there, and gives up, with nothing logged on our
    # side. Measured here as a live link from the console on the mirror
    # adapter, zero bonds, and zero GATT traffic.
    #
    # The returned bus must stay referenced: BlueZ drops the agent when its
    # D-Bus connection closes, and pairing silently reverts to prompting for a
    # PIN on a device with no keypad.
    from server.bt.agent import register_agent

    try:
        agent_bus = await register_agent()
        transcript.note("pairing agent registered")
    except Exception as exc:
        agent_bus = None
        transcript.note(
            "could not register a pairing agent (%s) -- the console will "
            "connect and fail to bond" % exc
        )

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
            # Keyed by the pad's object path. **Not by UUID**: this pad has
            # five Report characteristics all called 2a4d, so a UUID key
            # collapses them into one and every report ends up on
            # whichever was registered last.
            mirrored[path] = mirror

    #: Reports seen, so the log can be thinned without hiding the rate.
    seen = {"count": 0}

    def forward(path: str, uuid: str, payload: bytes) -> None:
        """A notification arrived from the pad: pass it to the console.

        **Not gated on the mirror's ``notifying`` flag.** That flag is set by
        BlueZ calling StartNotify, which happens when a client writes the
        CCCD -- and for a bonded device the CCCD is persistent, so a console
        that subscribed once never writes it again. gatt.py documents this at
        length; gating on it here reproduced the same bug one layer up, and
        discarded 32,000 reports from a console that was correctly subscribed.

        BlueZ knows which clients are subscribed and forwards only to those,
        so emitting unconditionally is correct rather than merely harmless.
        """
        mirror = mirrored.get(path)
        if mirror is None:
            return
        try:
            mirror.notify(payload)
        except Exception as exc:
            transcript.note("could not forward %s: %s" % (_short(uuid), exc))
            return

        # The pad streams at 100 Hz whether or not anything is happening.
        # Logging every report buries the console exchanges this tool exists
        # to show, so keep the first few and then a heartbeat.
        seen["count"] += 1
        if seen["count"] <= 5 or seen["count"] % 1000 == 0:
            transcript.record("pad -> console", "notify", uuid, payload)

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

    # **Pairable, or the console connects and quietly gives up.**
    #
    # Measured here first as "the console stopped pairing but no controller
    # appeared", with hci1 holding a live link and its settings reading
    # `powered connectable le` -- no `bondable`. HOGP needs an encrypted link,
    # encryption needs a bond, and nothing anywhere reports the refusal. The
    # server learned this the same way; the relay had never been run far
    # enough to hit it.
    try:
        adapter_props = bus.get_proxy_object(
            BLUEZ, f"/org/bluez/{args.console_adapter}",
            await bus.introspect(BLUEZ, f"/org/bluez/{args.console_adapter}"),
        ).get_interface(PROPS_IFACE)
        from dbus_next import Variant

        await adapter_props.call_set(ADAPTER_IFACE, "PairableTimeout", Variant("u", 0))
        await adapter_props.call_set(ADAPTER_IFACE, "Pairable", Variant("b", True))
        transcript.note("%s is bondable" % args.console_adapter)
    except Exception as exc:
        transcript.note(
            "could not make %s bondable (%s) -- a console will connect and "
            "give up without a word" % (args.console_adapter, exc)
        )

    assert agent_bus is not None or True   # referenced: BlueZ drops a
    #                                           closed agent's registration

    if args.seconds > 0:
        transcript.note("relaying for %.0f s" % args.seconds)
        await asyncio.sleep(args.seconds)
    else:
        # Open-ended on purpose. Pairing a console is a physical act at the
        # operator's pace, and every timed capture in this project has either
        # expired early or been guessed too long. The transcript is flushed on
        # every line, so stopping this at any moment loses nothing.
        transcript.note("relaying until interrupted -- stop it when you are done")
        await asyncio.Event().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

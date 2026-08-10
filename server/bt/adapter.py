"""Bluetooth adapter discovery, selection, and hot-plug tracking.

Capacity is derived from hardware, never hardcoded: the server enumerates what
is actually present and the rest of the system follows. Fewer than four dongles
means fewer usable controller slots (and client GUIs grey out the difference);
more than four means the operator chooses which to use.

Adapters are identified by **BD_ADDR throughout**. The `hciX` index is
assignment-order dependent -- it reshuffles when dongles are replugged or the
Pi reboots with devices enumerating in a different order. Keying off it would
silently move a player's controller to a different console.

Linux-only. Imported lazily by ``server/main.py`` so ``--mock-bt`` works
everywhere.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass

from server.bt.profiles import create_profile
from server.bt.sink import NullSink
from server.router import MAX_OUTPUTS, OutputChannel, Router

log = logging.getLogger(__name__)

#: Class of Device for a gamepad: peripheral major class, gamepad minor.
#: Consoles filter their pairing list on this, so it must be right or we never
#: appear as a candidate controller.
GAMEPAD_CLASS_OF_DEVICE = 0x002508

_HCI_LINE = re.compile(r"^(hci\d+):\s+Type:", re.MULTILINE)
_BDADDR_LINE = re.compile(r"BD Address:\s*([0-9A-Fa-f:]{17})")
_MANUFACTURER_LINE = re.compile(r"Manufacturer:\s*(.+?)\s*$", re.MULTILINE)


@dataclass(slots=True)
class AdapterInfo:
    """A physical Bluetooth adapter."""

    bd_addr: str
    hci_name: str
    manufacturer: str = ""
    is_up: bool = False

    #: False when the operator has deselected it. A disabled adapter is left
    #: completely untouched -- no SDP registration, no L2CAP bind -- so an
    #: adapter the Pi uses for something else is never hijacked.
    enabled: bool = True

    def snapshot(self) -> dict[str, object]:
        return {
            "bd_addr": self.bd_addr,
            "hci": self.hci_name,
            "manufacturer": self.manufacturer,
            "up": self.is_up,
            "enabled": self.enabled,
        }


class AdapterManager:
    """Enumerates adapters, applies the operator's selection, watches hot-plug."""

    def __init__(self, router: Router, config, *, default_profile: str = "generic") -> None:
        self._router = router
        self._config = config
        self._default_profile = default_profile

        self._adapters: dict[str, AdapterInfo] = {}
        self._watch_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

        #: bd_addr -> running HIDServer, and bd_addr -> the D-Bus connection
        #: holding its SDP registration. BlueZ drops a profile when the owning
        #: connection closes, so the bus object must be kept alive for as long
        #: as the adapter is enabled.
        self._hid_servers: dict[str, object] = {}
        self._profile_buses: dict[str, object] = {}

        #: Called after any change so the web GUI can push an update and the
        #: datapath can broadcast new capacity to clients.
        self.on_change = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> bool:
        """Discover adapters and bring up the enabled ones. Returns True if any are live."""
        if not _have_bluetooth_tools():
            log.error(
                "Bluetooth tools not found (need bluetoothctl or hciconfig). "
                "Install bluez, or run with --mock-bt."
            )
            return False

        await self.rescan()

        if not self._adapters:
            log.warning("No Bluetooth adapters detected")
            return False

        self._stop.clear()
        self._watch_task = asyncio.create_task(self._watch_hotplug())
        return self._router.capacity > 0

    async def stop(self) -> None:
        self._stop.set()
        if self._watch_task is not None:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            self._watch_task = None

        for bd_addr in list(self._hid_servers):
            await self._stop_hid(bd_addr)

    # -- discovery ---------------------------------------------------------

    async def rescan(self) -> list[AdapterInfo]:
        """Re-enumerate adapters and reconcile the router against what is present."""
        discovered = await asyncio.to_thread(_enumerate_adapters)
        found = {a.bd_addr: a for a in discovered}

        # Apply persisted enable/disable choices, keyed by BD_ADDR.
        for adapter in found.values():
            saved = self._config.adapter(adapter.bd_addr)
            if saved is not None:
                adapter.enabled = saved.enabled

        # If nothing has been configured yet, enable up to the ceiling so a
        # fresh install works without the operator touching anything.
        if not self._config.adapters:
            for index, adapter in enumerate(found.values()):
                adapter.enabled = index < MAX_OUTPUTS

        added = set(found) - set(self._adapters)
        removed = set(self._adapters) - set(found)

        for bd_addr in removed:
            gone = self._adapters.pop(bd_addr)
            log.warning("Adapter %s (%s) disappeared", bd_addr, gone.hci_name)
            self._router.remove_channel(bd_addr)

        for bd_addr in added:
            log.info("Adapter %s (%s) detected", bd_addr, found[bd_addr].hci_name)

        self._adapters = found
        await self._reconcile_channels()

        if (added or removed) and self.on_change:
            self.on_change()

        return list(self._adapters.values())

    async def _reconcile_channels(self) -> None:
        """Make the router's channels match the enabled adapter set."""
        enabled = [a for a in self._adapters.values() if a.enabled][:MAX_OUTPUTS]
        enabled_addrs = {a.bd_addr for a in enabled}

        for channel in self._router.channels():
            if channel.bd_addr not in enabled_addrs:
                # Tear the HID stack down before dropping the channel, so the
                # L2CAP listeners are released and the adapter can be re-enabled
                # later without hitting EADDRINUSE against our own sockets.
                await self._stop_hid(channel.bd_addr)
                self._router.remove_channel(channel.bd_addr)

        for adapter in enabled:
            if self._router.channel(adapter.bd_addr) is not None:
                continue

            saved = self._config.adapter(adapter.bd_addr)
            profile_name = saved.profile if saved else self._default_profile

            if not await asyncio.to_thread(_bring_up_adapter, adapter):
                log.error("Could not bring up %s (%s)", adapter.bd_addr, adapter.hci_name)
                continue

            try:
                profile = create_profile(
                    profile_name,
                    **({"bd_addr": adapter.bd_addr} if profile_name == "switch_pro" else {}),
                )
            except ValueError as exc:
                log.error("Bad profile for %s: %s", adapter.bd_addr, exc)
                continue

            sink = await self._start_hid(adapter, profile)

            self._router.add_channel(
                OutputChannel(
                    bd_addr=adapter.bd_addr,
                    hci_name=adapter.hci_name,
                    profile=profile,
                    # Falls back to NullSink if the HID stack could not start:
                    # capacity still reflects the adapter, and the web GUI shows
                    # it as not-connected rather than the adapter vanishing.
                    sink=sink or NullSink(),
                )
            )

    async def _start_hid(self, adapter: AdapterInfo, profile) -> object | None:
        """Bring up the real Bluetooth HID stack for one adapter.

        Two pieces, in order:

        1. Register the HID service record with BlueZ, so a console browsing us
           over SDP sees a gamepad. Without this the L2CAP listeners are up but
           nothing knows what we are.
        2. Bind L2CAP PSM 17/19 to *this adapter's* BD_ADDR and start accepting.

        Returns the sink to attach to the router channel, or None if either step
        failed -- in which case the adapter stays visible but inert, which is
        far easier to diagnose than a silently missing adapter.
        """
        from server.bt.hid import HIDServer, L2CAPSink, is_supported
        from server.bt.sdp import SDPError, register_hid_profile

        if not is_supported():
            log.error("This platform has no AF_BLUETOOTH support; cannot serve HID")
            return None

        descriptor = profile.descriptor

        try:
            bus = await register_hid_profile(
                descriptor.device_name,
                descriptor.report_descriptor,
                descriptor.vendor_id,
                descriptor.product_id,
            )
            self._profile_buses[adapter.bd_addr] = bus
        except SDPError as exc:
            log.error("SDP registration failed for %s: %s", adapter.bd_addr, exc)
            return None

        sink = L2CAPSink(profile, adapter.bd_addr)
        server = HIDServer(adapter.bd_addr, profile, sink)

        try:
            await asyncio.to_thread(server.start)
        except OSError as exc:
            # start() already turns the two classic failures -- EADDRINUSE from
            # bluetoothd's input plugin, and EPERM from missing privileges --
            # into actionable messages. Surface them verbatim.
            log.error("HID server failed on %s: %s", adapter.bd_addr, exc)
            await self._release_profile(adapter.bd_addr)
            return None

        self._hid_servers[adapter.bd_addr] = server
        log.info(
            "HID stack live on %s as '%s' (%s)",
            adapter.hci_name,
            descriptor.device_name,
            profile.display_name,
        )
        return sink

    async def _stop_hid(self, bd_addr: str) -> None:
        """Tear down the HID stack for one adapter."""
        server = self._hid_servers.pop(bd_addr, None)
        if server is not None:
            await asyncio.to_thread(server.stop)
        await self._release_profile(bd_addr)

    async def _release_profile(self, bd_addr: str) -> None:
        bus = self._profile_buses.pop(bd_addr, None)
        if bus is None:
            return
        from server.bt.sdp import unregister_hid_profile

        try:
            await unregister_hid_profile(bus)
        except Exception:
            log.debug("Could not cleanly unregister SDP profile for %s", bd_addr)

    # -- operator controls -------------------------------------------------

    async def set_enabled(self, bd_addr: str, enabled: bool) -> tuple[bool, str]:
        """Enable or disable an adapter. Returns ``(ok, message)``."""
        adapter = self._adapters.get(bd_addr)
        if adapter is None:
            return False, f"No adapter {bd_addr}"

        if enabled:
            already = sum(1 for a in self._adapters.values() if a.enabled and a.bd_addr != bd_addr)
            if already >= MAX_OUTPUTS:
                return False, (
                    f"Already using the maximum of {MAX_OUTPUTS} adapters. "
                    "Disable one first."
                )

        adapter.enabled = enabled

        from server.config import AdapterConfig

        saved = self._config.adapter(bd_addr)
        self._config.upsert_adapter(
            AdapterConfig(
                bd_addr=bd_addr,
                enabled=enabled,
                profile=saved.profile if saved else self._default_profile,
                paired_target=saved.paired_target if saved else "",
                label=saved.label if saved else "",
            )
        )

        await self._reconcile_channels()
        if self.on_change:
            self.on_change()

        return True, f"Adapter {bd_addr} {'enabled' if enabled else 'disabled'}"

    async def set_profile(self, bd_addr: str, profile_name: str) -> tuple[bool, str]:
        """Change which controller an adapter emulates.

        Requires a re-pair: the console identifies the controller by name and
        VID/PID, both of which change with the profile.
        """
        channel = self._router.channel(bd_addr)
        if channel is None:
            return False, f"Adapter {bd_addr} is not enabled"

        try:
            profile = create_profile(
                profile_name,
                **({"bd_addr": bd_addr} if profile_name == "switch_pro" else {}),
            )
        except ValueError as exc:
            return False, str(exc)

        channel.profile = profile

        from server.config import AdapterConfig

        saved = self._config.adapter(bd_addr)
        self._config.upsert_adapter(
            AdapterConfig(
                bd_addr=bd_addr,
                enabled=True,
                profile=profile_name,
                paired_target=saved.paired_target if saved else "",
                label=saved.label if saved else "",
            )
        )

        if self.on_change:
            self.on_change()

        return True, f"{bd_addr} now emulates {profile.display_name}. Re-pair to apply."

    async def set_pairable(
        self,
        bd_addr: str,
        pairable: bool,
        duration_s: int = 120,
        *,
        forget_bonds: bool = True,
    ) -> tuple[bool, str]:
        """Put an adapter into (or out of) connection mode.

        Makes it discoverable and pairable so a console can find it. For the
        Switch this is what the "Change Grip/Order" screen looks for.

        ``forget_bonds`` clears existing pairings on this adapter first, and
        defaults on for a reason: if the *host* forgets us (removing the device
        in Windows' Bluetooth settings, or a console being reset) it generates a
        fresh link key, while we keep the old one. Authentication then fails and
        the host reports only "Couldn't connect", with nothing on either side
        pointing at the stale bond. Entering pairing mode is exactly the moment
        the operator means "start fresh", so clearing is the right default --
        and it mirrors what a real controller does when put into pairing mode.
        """
        adapter = self._adapters.get(bd_addr)
        if adapter is None:
            return False, f"No adapter {bd_addr}"
        if not adapter.enabled:
            return False, f"Adapter {bd_addr} is disabled"

        channel = self._router.channel(bd_addr)
        name = channel.profile.descriptor.device_name if channel else "Gamepad"

        cleared = 0
        if pairable and forget_bonds:
            cleared = await asyncio.to_thread(_forget_bonds)

        ok = await asyncio.to_thread(
            _set_discoverable, adapter, pairable, duration_s, name
        )
        if not ok:
            return False, f"Could not change pairing mode on {adapter.hci_name}"

        if pairable:
            note = f" Cleared {cleared} previous pairing(s)." if cleared else ""
            return True, (
                f"{adapter.hci_name} is discoverable as '{name}' for {duration_s}s.{note} "
                "Put the console into pairing mode now."
            )
        return True, f"{adapter.hci_name} is no longer discoverable"

    def adapters(self) -> list[AdapterInfo]:
        return list(self._adapters.values())

    def snapshot(self) -> list[dict[str, object]]:
        return [a.snapshot() for a in self._adapters.values()]

    # -- hot-plug ----------------------------------------------------------

    async def _watch_hotplug(self) -> None:
        """Watch for dongles appearing and disappearing.

        Prefers pyudev for immediate notification; falls back to polling so the
        feature still works without it. Either way a periodic reconcile runs,
        because udev events can be missed when the daemon restarts.
        """
        monitor = _try_udev_monitor()

        if monitor is None:
            log.info("pyudev unavailable; falling back to 10 s adapter polling")
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=10.0)
                    return
                except asyncio.TimeoutError:
                    await self.rescan()
            return

        log.info("Watching for Bluetooth adapter hot-plug via udev")
        loop = asyncio.get_running_loop()

        while not self._stop.is_set():
            device = await loop.run_in_executor(None, _poll_udev, monitor)
            if device is None:
                # Timeout -- also serves as the periodic reconcile.
                await self.rescan()
                continue

            log.info("udev reported Bluetooth device %s", device)
            # Settle: the adapter is not usable the instant udev sees it.
            await asyncio.sleep(1.0)
            await self.rescan()


# --------------------------------------------------------------------------
# Shell-outs to bluez tooling.
#
# We deliberately use hciconfig/bluetoothctl rather than raw ioctls: they are
# present on every bluez install, they handle kernel-version differences for
# us, and none of this is on the hot path -- it runs at startup and when the
# operator clicks something.
# --------------------------------------------------------------------------


def _have_bluetooth_tools() -> bool:
    return shutil.which("hciconfig") is not None or shutil.which("bluetoothctl") is not None


def _run(cmd: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return result.returncode, result.stdout + result.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.debug("Command %s failed: %s", cmd[0], exc)
        return 1, str(exc)


def _enumerate_adapters() -> list[AdapterInfo]:
    """List all present adapters via ``hciconfig -a``."""
    if shutil.which("hciconfig") is None:
        return _enumerate_via_bluetoothctl()

    code, output = _run(["hciconfig", "-a"])
    if code != 0 or not output.strip():
        return []

    adapters: list[AdapterInfo] = []
    # Split on the "hciN:" headers so each block belongs to one adapter.
    blocks = re.split(r"^(?=hci\d+:)", output, flags=re.MULTILINE)

    for block in blocks:
        header = _HCI_LINE.search(block)
        if not header:
            continue
        addr_match = _BDADDR_LINE.search(block)
        if not addr_match:
            continue

        manufacturer = ""
        man_match = _MANUFACTURER_LINE.search(block)
        if man_match:
            manufacturer = man_match.group(1).strip()

        adapters.append(
            AdapterInfo(
                bd_addr=addr_match.group(1).upper(),
                hci_name=header.group(1),
                manufacturer=manufacturer,
                is_up="UP RUNNING" in block,
            )
        )

    return adapters


def _enumerate_via_bluetoothctl() -> list[AdapterInfo]:
    """Fallback enumeration for systems without hciconfig (newer bluez)."""
    code, output = _run(["bluetoothctl", "list"])
    if code != 0:
        return []

    adapters = []
    for line in output.splitlines():
        # "Controller AA:BB:CC:DD:EE:FF hostname [default]"
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Controller":
            adapters.append(
                AdapterInfo(
                    bd_addr=parts[1].upper(),
                    hci_name=parts[1],
                    manufacturer=" ".join(parts[2:]) if len(parts) > 2 else "",
                    is_up=True,
                )
            )
    return adapters


def _bring_up_adapter(adapter: AdapterInfo) -> bool:
    """Bring an adapter up and set its class of device to 'gamepad'."""
    if shutil.which("hciconfig") is None:
        adapter.is_up = True
        return True

    code, output = _run(["hciconfig", adapter.hci_name, "up"])
    if code != 0:
        if "rf-kill" in output.lower() or _is_rfkill_blocked(adapter.hci_name):
            # Raspberry Pi OS ships with Bluetooth soft-blocked in several
            # configurations, so this is common and the raw errno is useless.
            log.error(
                "Adapter %s (%s) is blocked by rfkill. Unblock it with:\n"
                "    sudo rfkill unblock bluetooth\n"
                "(the rfkill binary lives in /usr/sbin, which is often not on a "
                "non-root PATH)",
                adapter.bd_addr,
                adapter.hci_name,
            )
        else:
            log.error("hciconfig %s up failed: %s", adapter.hci_name, output.strip())
        return False

    # Class of device -- consoles filter their pairing list on this.
    code, output = _run(
        ["hciconfig", adapter.hci_name, "class", f"0x{GAMEPAD_CLASS_OF_DEVICE:06x}"]
    )
    if code != 0:
        log.warning(
            "Could not set class of device on %s: %s", adapter.hci_name, output.strip()
        )

    adapter.is_up = True
    log.info("Adapter %s (%s) is up", adapter.bd_addr, adapter.hci_name)
    return True


def _forget_bonds() -> int:
    """Remove every existing pairing. Returns how many were cleared.

    A stale bond is invisible from the host's side -- it just says "couldn't
    connect" -- so this is worth doing proactively when entering pairing mode
    rather than leaving the operator to discover it.
    """
    code, output = _run(["bluetoothctl", "devices", "Paired"])
    if code != 0:
        return 0

    cleared = 0
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Device":
            if _run(["bluetoothctl", "remove", parts[1]])[0] == 0:
                cleared += 1
                log.info("Cleared stale pairing with %s", parts[1])
    return cleared


def _bluetoothctl(args: list[str]) -> bool:
    """Run a bluetoothctl command non-interactively.

    Used for the Adapter1 properties that `hciconfig` cannot reach --
    principally ``Pairable``, without which bluetoothd refuses pairing even on a
    fully discoverable adapter.
    """
    code, output = _run(["bluetoothctl", "--", *args])
    if code != 0:
        log.debug("bluetoothctl %s failed: %s", " ".join(args), output.strip())
    return code == 0


def _is_rfkill_blocked(hci_name: str) -> bool:
    """True if this adapter is soft- or hard-blocked by rfkill.

    Read straight from sysfs rather than shelling out: the ``rfkill`` binary
    lives in /usr/sbin and is frequently absent from a non-root PATH, which is
    exactly the situation where this check matters.
    """
    import glob
    from pathlib import Path

    for entry in glob.glob("/sys/class/rfkill/rfkill*"):
        path = Path(entry)
        try:
            if path.joinpath("name").read_text().strip() != hci_name:
                continue
            soft = path.joinpath("soft").read_text().strip()
            hard = path.joinpath("hard").read_text().strip()
            return soft == "1" or hard == "1"
        except OSError:
            continue
    return False


def _set_discoverable(
    adapter: AdapterInfo, enabled: bool, duration_s: int, device_name: str
) -> bool:
    """Toggle discoverable + pairable, and set the advertised name."""
    if shutil.which("hciconfig") is None:
        return True

    if enabled:
        # The console matches on this name -- the Switch specifically looks for
        # "Pro Controller".
        _run(["hciconfig", adapter.hci_name, "name", device_name])
        _run(["hciconfig", adapter.hci_name, "class", f"0x{GAMEPAD_CLASS_OF_DEVICE:06x}"])

        # HCI-level scan modes: page scan (connectable) + inquiry scan
        # (discoverable).
        code, output = _run(["hciconfig", adapter.hci_name, "piscan"])

        # ...and BlueZ's own properties, which are separate. Setting scan mode
        # alone leaves Adapter1.Pairable false, and bluetoothd then *rejects*
        # the pairing attempt even though the device is plainly visible to the
        # host -- which looks like the host is at fault. Discoverable also has
        # its own timeout that defaults to 180s regardless of scan mode.
        _bluetoothctl(["pairable", "on"])
        _bluetoothctl(["discoverable-timeout", str(max(0, duration_s))])
        _bluetoothctl(["discoverable", "on"])
    else:
        code, output = _run(["hciconfig", adapter.hci_name, "noscan"])
        _bluetoothctl(["discoverable", "off"])
        _bluetoothctl(["pairable", "off"])

    if code != 0:
        log.error("Could not change scan mode on %s: %s", adapter.hci_name, output.strip())
        return False

    log.info(
        "%s is %s (name '%s')",
        adapter.hci_name,
        "discoverable and pairable" if enabled else "hidden",
        device_name,
    )
    return True


def _try_udev_monitor():
    """Build a udev monitor for bluetooth devices, or None if unavailable."""
    try:
        import pyudev
    except ImportError:
        return None

    try:
        context = pyudev.Context()
        monitor = pyudev.Monitor.from_netlink(context)
        monitor.filter_by(subsystem="bluetooth")
        monitor.start()
        return monitor
    except Exception as exc:
        log.debug("Could not start udev monitor: %s", exc)
        return None


def _poll_udev(monitor):
    """Block for a udev event. Returns the device, or None on timeout.

    The timeout doubles as the periodic reconcile trigger -- udev events can be
    missed if the daemon restarts, so we never rely on them exclusively.
    """
    try:
        device = monitor.poll(timeout=10.0)
        return str(device) if device is not None else None
    except Exception:
        return None

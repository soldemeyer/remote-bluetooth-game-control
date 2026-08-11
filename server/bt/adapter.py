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

from server.bt import adapter_dbus
from server.bt.profiles import create_profile
from server.bt.sink import NullSink
from server.router import MAX_OUTPUTS, OutputChannel, Router

log = logging.getLogger(__name__)

#: Class of Device for a gamepad: peripheral major class, gamepad minor.
#: Consoles filter their pairing list on this, so it must be right or we never
#: appear as a candidate controller.
GAMEPAD_CLASS_OF_DEVICE = 0x002508

#: Base name adapters advertise. A number is appended so four adapters are
#: distinguishable in a host's Bluetooth list -- identical names leave the host
#: to disambiguate, which is how they ended up shown as "controller-server #2".
BASE_DEVICE_NAME = "RBGC Gamepad"

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

    def __init__(
        self,
        router: Router,
        config,
        *,
        default_profile: str = "generic",
        config_path=None,
        on_rumble=None,
    ) -> None:
        self._router = router
        self._config = config
        self._default_profile = default_profile

        #: Where to write config when a reconnect target is learned. None means
        #: keep it in memory only (used by tests).
        self._config_path = config_path

        #: Delivers console rumble back to the datapath, which decides
        #: whether to transmit it.
        self._on_rumble = on_rumble

        self._adapters: dict[str, AdapterInfo] = {}
        self._watch_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

        #: bd_addr -> running HIDServer. Genuinely per-adapter: each owns its
        #: own L2CAP listeners bound to that radio.
        self._hid_servers: dict[str, object] = {}

        #: The single D-Bus connection holding our system-wide HID SDP
        #: registration, and the profile it advertises. BlueZ drops a profile
        #: when its owning connection closes, so this must stay alive for as
        #: long as any adapter is serving.
        self._sdp_bus: object | None = None
        self._sdp_profile_name: str = ""

        #: The pairing agent's D-Bus connection. Also system-wide: BlueZ
        #: drops the agent when this closes, and pairing silently reverts
        #: to prompting for a PIN.
        self._agent_bus: object | None = None

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
        await self._release_sdp()
        await self._release_agent()

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
        # Sorted by hci name so first-time numbering is intuitive (hci0 ->
        # 'Gamepad 1'). Numbers are persisted per BD_ADDR afterwards, so
        # later reshuffles of hciX indices do not rename anything.
        enabled = sorted(
            (a for a in self._adapters.values() if a.enabled),
            key=lambda a: a.hci_name,
        )[:MAX_OUTPUTS]
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

            number = self._assign_number(adapter.bd_addr)
            name = self.adapter_name(adapter.bd_addr)

            # Set the advertised name and gamepad class as the adapter comes
            # up, not only during a pairing window -- a host scanning at any
            # other moment should still see the right name.
            await asyncio.to_thread(_ensure_pairing_settings, adapter)
            await asyncio.to_thread(_set_device_class, adapter)
            await adapter_dbus.set_properties(adapter.hci_name, alias=name)
            log.info("Adapter %s advertises as '%s'", adapter.hci_name, name)

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

    async def _ensure_agent(self) -> None:
        """Register the pairing agent, once, before any adapter is pairable.

        Without an agent bluetoothd cannot complete Secure Simple Pairing and
        falls back to legacy PIN entry -- the host then prompts for a PIN on a
        device that has no keypad. Registering with NoInputNoOutput capability
        selects "Just Works", which is what a real gamepad does.

        Like the SDP record this is system-wide, not per-adapter.
        """
        if self._agent_bus is not None:
            return

        try:
            from server.bt.agent import register_agent

            self._agent_bus = await register_agent(on_paired=self._trust_device)
        except Exception as exc:
            # Non-fatal, but pairing will prompt for a PIN, so say so plainly.
            log.error(
                "Could not register the Bluetooth pairing agent (%s). Hosts will "
                "be prompted for a PIN; enter %s if asked.",
                exc,
                "0000",
            )

    def _trust_device(self, device_path: str) -> None:
        """Mark a freshly-paired device trusted.

        Untrusted devices need service authorization on every connect, which
        would break unattended reconnection after a restart.
        """
        import asyncio as _asyncio

        async def _set_trusted() -> None:
            try:
                from server.bt import adapter_dbus

                await adapter_dbus.set_device_trusted(device_path, True)
            except Exception:
                log.debug("Could not mark %s trusted", device_path, exc_info=True)

        try:
            _asyncio.get_running_loop().create_task(_set_trusted())
        except RuntimeError:
            # Called from a non-async context; the device still pairs, it just
            # may re-authorize on reconnect.
            log.debug("No running loop; skipping trust for %s", device_path)

    async def _ensure_sdp(self, profile) -> bool:
        """Register the HID service record with BlueZ. Once, for all adapters.

        BlueZ's ``ProfileManager1`` owns a **single system-wide SDP database**:
        a UUID can be registered once, and bluetoothd then serves that record on
        every adapter. Registering per-adapter -- which is what this used to do
        -- fails the second and subsequent attempts with "UUID already
        registered", leaving all but one adapter inert. Which one survived
        depended on dict ordering, so the failure moved between restarts.

        Consequence worth knowing: **every adapter advertises the same HID
        descriptor.** Four adapters all emulating the same profile is fine, and
        is the normal case. Mixing profiles is not supported by this API -- the
        mismatch is logged rather than silently producing a controller that
        advertises one thing and reports another.
        """
        from server.bt.sdp import SDPError, register_hid_profile

        if self._sdp_bus is not None:
            if self._sdp_profile_name and self._sdp_profile_name != profile.name:
                log.warning(
                    "Adapter requests profile '%s' but the system-wide SDP record "
                    "already advertises '%s'. BlueZ allows only one HID record for "
                    "the whole machine, so this adapter will advertise '%s'. Use the "
                    "same profile on every adapter, or run one adapter at a time.",
                    profile.name,
                    self._sdp_profile_name,
                    self._sdp_profile_name,
                )
            return True

        descriptor = profile.descriptor
        try:
            self._sdp_bus = await register_hid_profile(
                descriptor.device_name,
                descriptor.report_descriptor,
                descriptor.vendor_id,
                descriptor.product_id,
            )
        except SDPError as exc:
            log.error("SDP registration failed: %s", exc)
            return False

        self._sdp_profile_name = profile.name
        log.info(
            "HID SDP record registered for '%s' (shared by all adapters)",
            profile.display_name,
        )
        return True

    async def _start_hid(self, adapter: AdapterInfo, profile) -> object | None:
        """Bring up the real Bluetooth HID stack for one adapter.

        Two pieces:

        1. The SDP record, so a console browsing us sees a gamepad. Shared
           across adapters -- see :meth:`_ensure_sdp`.
        2. L2CAP PSM 17/19 bound to *this adapter's* BD_ADDR. This part is
           genuinely per-adapter, and is what makes four dongles four
           independent controllers.

        Returns the sink to attach to the router channel, or None if either step
        failed -- in which case the adapter stays visible but inert, which is
        far easier to diagnose than a silently missing adapter.
        """
        from server.bt.hid import HIDServer, L2CAPSink, is_supported

        if not is_supported():
            log.error("This platform has no AF_BLUETOOTH support; cannot serve HID")
            return None

        await self._ensure_agent()

        if not await self._ensure_sdp(profile):
            return None

        sink = L2CAPSink(profile, adapter.bd_addr)
        server = HIDServer(
            adapter.bd_addr,
            profile,
            sink,
            on_host_connected=lambda host: self._remember_host(adapter.bd_addr, host),
            on_rumble=self._on_rumble,
        )

        try:
            await asyncio.to_thread(server.start)
        except OSError as exc:
            # start() already turns the two classic failures -- EADDRINUSE from
            # bluetoothd's input plugin, and EPERM from missing privileges --
            # into actionable messages. Surface them verbatim.
            log.error("HID server failed on %s: %s", adapter.bd_addr, exc)
            return None

        self._hid_servers[adapter.bd_addr] = server

        # Restore the last host we were connected to, so the link comes back on
        # its own after a restart instead of needing someone to click Connect.
        saved = self._config.adapter(adapter.bd_addr)
        target = saved.paired_target if saved else ""
        if target:
            server.set_reconnect_target(target)

        log.info(
            "HID stack live on %s (%s) as '%s' (%s)%s",
            adapter.hci_name,
            adapter.bd_addr,
            self.adapter_name(adapter.bd_addr),
            profile.display_name,
            f", reconnecting to {target}" if target else "",
        )
        return sink

    def _remember_host(self, bd_addr: str, host_bd_addr: str) -> None:
        """Persist which host connected, so reconnect survives a restart.

        Called from the HID server's accept thread, so it only touches the
        config object -- no async work, no I/O on that thread.
        """
        saved = self._config.adapter(bd_addr)
        if saved is not None and saved.paired_target == host_bd_addr:
            return

        from server.config import AdapterConfig

        self._config.upsert_adapter(
            AdapterConfig(
                bd_addr=bd_addr,
                enabled=True,
                profile=saved.profile if saved else self._default_profile,
                paired_target=host_bd_addr,
                label=saved.label if saved else "",
            )
        )
        log.info("Remembered host %s for adapter %s", host_bd_addr, bd_addr)

        if self._config_path is not None:
            try:
                from server import config as server_config

                server_config.save(self._config, self._config_path)
            except Exception:
                log.debug("Could not persist reconnect target", exc_info=True)

    async def _stop_hid(self, bd_addr: str) -> None:
        """Tear down one adapter's HID stack.

        The SDP record is shared, so it is released only when the last adapter
        goes away -- dropping it while another adapter is still serving would
        make that adapter undiscoverable.
        """
        server = self._hid_servers.pop(bd_addr, None)
        if server is not None:
            await asyncio.to_thread(server.stop)

        # The SDP registration and pairing agent are deliberately NOT released
        # here, even when this was the last adapter.
        #
        # Unregistering the profile makes BlueZ remove the HID UUID from every
        # adapter's Extended Inquiry Response and reset the class of device to
        # 0x000000 (Miscellaneous). A host scanning at that moment sees a
        # nameless generic device rather than a gamepad, and falls back to
        # legacy PIN pairing.
        #
        # That is not hypothetical: adapter hot-plug at startup makes
        # _reconcile_channels run several times, and a transient empty moment
        # was enough to strip the UUID and leave every subsequent pairing
        # attempt prompting for a PIN.
        #
        # Both registrations are process-wide and harmless to keep, so they are
        # released only in stop().

    async def _release_agent(self) -> None:
        """Drop the pairing agent registration."""
        if self._agent_bus is None:
            return

        from server.bt.agent import unregister_agent

        bus, self._agent_bus = self._agent_bus, None
        try:
            await unregister_agent(bus)
        except Exception:
            log.debug("Could not cleanly unregister the pairing agent", exc_info=True)

    async def _release_sdp(self) -> None:
        """Drop the shared SDP registration."""
        if self._sdp_bus is None:
            return

        from server.bt.sdp import unregister_hid_profile

        bus, self._sdp_bus = self._sdp_bus, None
        self._sdp_profile_name = ""
        try:
            await unregister_hid_profile(bus)
        except Exception:
            log.debug("Could not cleanly unregister the SDP profile", exc_info=True)

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

        name = self.adapter_name(bd_addr)

        if pairable:
            await self._ensure_agent()

        cleared = 0
        if pairable and forget_bonds:
            # Scoped to this adapter: clearing machine-wide would disconnect
            # consoles happily playing on the other three.
            cleared = await adapter_dbus.remove_bonds(adapter.hci_name)

        # Re-assert both on every pairing window: these are adapter state and
        # anything on the system can have flipped them since startup.
        await asyncio.to_thread(_ensure_pairing_settings, adapter)
        await asyncio.to_thread(_set_device_class, adapter)

        ok = await adapter_dbus.set_properties(
            adapter.hci_name,
            alias=name,
            pairable=pairable,
            discoverable=pairable,
            timeout_s=duration_s if pairable else None,
        )
        if not ok:
            return False, f"Could not change pairing mode on {adapter.hci_name}"

        if pairable:
            # Read back rather than trusting the write. The original bug was
            # exactly this: the adapter looked discoverable while Pairable was
            # quietly false, and nothing surfaced it.
            actual = await adapter_dbus.read_properties(adapter.hci_name)
            if actual and not actual.get("pairable"):
                return False, (
                    f"{adapter.hci_name} would not accept Pairable; "
                    "hosts will see it but fail to pair"
                )

            note = f" Cleared {cleared} previous pairing(s)." if cleared else ""
            return True, (
                f"{adapter.hci_name} is discoverable as '{name}' for {duration_s}s.{note} "
                "Put the console into pairing mode now."
            )
        return True, f"{adapter.hci_name} is no longer discoverable"

    def _persist(self) -> None:
        """Write config back, if we were given somewhere to write it.

        Best effort: failing to save an adapter number must never stop the
        adapter coming up. The cost of losing it is a renumber on next boot.
        """
        if self._config_path is None:
            return
        try:
            from server import config as server_config

            server_config.save(self._config, self._config_path)
        except Exception:
            log.debug("Could not persist adapter config", exc_info=True)

    def adapter_name(self, bd_addr: str) -> str:
        """The name this adapter advertises to consoles.

        Numbered so four adapters are tellable apart in a host's Bluetooth list
        -- they would otherwise all appear as the same string, and a host that
        deduplicates by name shows something like "controller-server #2"
        instead. An operator-set label wins over the generated name.
        """
        saved = self._config.adapter(bd_addr)
        if saved is not None and saved.label:
            return saved.label

        number = saved.number if saved is not None else 0
        return f"{BASE_DEVICE_NAME} {number}" if number else BASE_DEVICE_NAME

    def _assign_number(self, bd_addr: str) -> int:
        """Give an adapter the lowest free number, and persist it.

        Persisted per BD_ADDR so the name follows the physical dongle: a
        console that paired with "RBGC Gamepad 2" keeps seeing that name after
        a reboot, even if the hciX indices reshuffle.
        """
        saved = self._config.adapter(bd_addr)
        if saved is not None and saved.number:
            return saved.number

        taken = {a.number for a in self._config.adapters if a.number}
        number = next((n for n in range(1, MAX_OUTPUTS + 1) if n not in taken), 0)

        from server.config import AdapterConfig

        self._config.upsert_adapter(
            AdapterConfig(
                bd_addr=bd_addr,
                enabled=True,
                profile=saved.profile if saved else self._default_profile,
                paired_target=saved.paired_target if saved else "",
                label=saved.label if saved else "",
                number=number,
            )
        )
        self._persist()
        return number

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


#: MGMT settings that decide whether a host can use Secure Simple Pairing.
#: These must appear in *current settings*...
_REQUIRED_MGMT_SETTINGS = frozenset({"ssp", "bondable"})
#: ...and this must not.
_FORBIDDEN_MGMT_SETTINGS = frozenset({"link-security"})


def _read_mgmt_settings(index: str) -> set[str] | None:
    """Parse an adapter's **current** MGMT settings. None if unreadable.

    ``btmgmt info`` prints two settings lines::

        supported settings: powered connectable ... bondable link-security ssp ...
        current settings: powered bondable ssp br/edr le secure-conn

    Only the second describes reality. The check this replaced grepped the whole
    output for ``link-security`` -- which is listed under *supported* settings on
    essentially every adapter -- so it fired unconditionally and verified nothing.
    """
    code, output = _run(["btmgmt", "--index", index, "info"])
    if code != 0:
        return None

    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("current settings:"):
            return set(stripped.split(":", 1)[1].split())
    return None


def _pairing_settings_ok(settings: set[str]) -> bool:
    return _REQUIRED_MGMT_SETTINGS <= settings and not (_FORBIDDEN_MGMT_SETTINGS & settings)


def _ensure_pairing_settings(adapter: AdapterInfo) -> None:
    """Verify -- and only if genuinely wrong, correct -- the MGMT settings that
    let a host use Secure Simple Pairing.

    * ``link-security`` (HCI Write_Authentication_Enable) must be **off**. With
      it on, the controller demands authentication as the link comes up and
      pairing degrades to the legacy flow -- ``Link Key Request`` then
      ``PIN Code Request`` -- never reaching the SSP IO-capability exchange.
    * ``ssp`` and ``bondable`` must be **on**, or there is no SSP to negotiate.

    Read-then-write rather than write-unconditionally: bluetoothd owns these and
    sets them correctly on its own, and ``btmgmt`` is a *second* MGMT client, so
    writing every time means two owners fighting over one piece of state. We
    only intervene when the adapter is actually misconfigured, and we report the
    result at warning level either way -- a silent failure here resurfaces much
    later as an inexplicable PIN prompt with nothing in the log.

    Note this covers **our** half only. SSP also requires the *host* to
    advertise support; when it does not, every adapter here can be perfect and
    the host will still demand a PIN. See "When a host demands a PIN" in
    CLAUDE.md for how to tell the two apart in one measurement.
    """
    if shutil.which("btmgmt") is None:
        log.debug("btmgmt not available; cannot verify pairing settings")
        return

    index = adapter.hci_name.removeprefix("hci")

    settings = _read_mgmt_settings(index)
    if settings is None:
        log.warning(
            "Could not read MGMT settings for %s; unable to confirm it can do "
            "Secure Simple Pairing",
            adapter.hci_name,
        )
        return

    if _pairing_settings_ok(settings):
        log.debug("%s pairing settings OK (%s)", adapter.hci_name, " ".join(sorted(settings)))
        return

    log.warning(
        "%s has settings that force legacy PIN pairing (current: %s); correcting",
        adapter.hci_name,
        " ".join(sorted(settings)) or "none",
    )

    for setting, value in (("linksec", "off"), ("ssp", "on"), ("bondable", "on")):
        code, output = _run(["btmgmt", "--index", index, setting, value])
        if code != 0:
            log.warning(
                "Could not set %s=%s on %s: %s",
                setting,
                value,
                adapter.hci_name,
                output.strip(),
            )

    settings = _read_mgmt_settings(index)
    if settings is None or not _pairing_settings_ok(settings):
        log.error(
            "%s still cannot offer Secure Simple Pairing (current: %s). Hosts will "
            "be prompted for a PIN.",
            adapter.hci_name,
            " ".join(sorted(settings)) if settings else "unknown",
        )


def _set_device_class(adapter: AdapterInfo) -> None:
    """Set the gamepad class of device.

    Stays on hciconfig because BlueZ exposes no writable Class property, and
    consoles filter their pairing list on this value -- get it wrong and we
    never appear as a candidate controller at all.

    Everything else about an adapter (alias, pairable, discoverable) goes
    through D-Bus instead: see server/bt/adapter_dbus.py for why bluetoothctl
    is unusable in a multi-adapter system.
    """
    if shutil.which("hciconfig") is None:
        return

    code, output = _run(
        ["hciconfig", adapter.hci_name, "class", f"0x{GAMEPAD_CLASS_OF_DEVICE:06x}"]
    )
    if code != 0:
        log.debug("Could not set class on %s: %s", adapter.hci_name, output.strip())


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

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
import contextlib
import logging
import re
import shutil
import subprocess
import time


from server.bt import adapter_dbus
from server.bt.profiles import create_profile
from server.bt.sink import NullSink
from server.bt.state import AdapterRegistry, AdapterState, FlagSet, Phase
from server.router import MAX_OUTPUTS, OutputChannel, Router

log = logging.getLogger(__name__)

#: How long to hold off our own outgoing reconnect after the operator presses
#: Disconnect. Long enough that the link does not come straight back while they
#: are still looking at the button, short enough that it recovers by itself if
#: How long an adapter must look orphaned before we say so.
#:
#: A console drops and re-establishes links continually, so at any instant some
#: adapter is idle while a sibling is connected. Ten minutes is far longer than
#: any reconnect cycle observed (the longest measured gap was under a minute)
#: and far shorter than an operator's patience with a controller that will not
#: come back.
_ORPHAN_CONFIRM_NS = 600 * 1_000_000_000

#: they change their mind.
_DISCONNECT_HOLDOFF_S = 60.0

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


#: A physical Bluetooth adapter.
#:
#: This *is* :class:`~server.bt.state.AdapterState`. The two used to be separate
#: types describing the same thing, which is how adapter state ended up spread
#: across an ``AdapterInfo``, two sets on the manager, the HID server's own view
#: and the config file, with each of them able to disagree with the others.
#:
#: The name survives because it reads better at the call sites that mean "one
#: adapter" rather than "the state machine for one adapter", and because it
#: keeps this rename out of the diff for every test that constructs one.
AdapterInfo = AdapterState


#: A one-sided bond produces a connect/disconnect cycle several times a second,
#: so a handful of authentication failures inside a few seconds is conclusive
#: rather than merely suspicious. Measured: 18 to 30 cycles per capture, none
#: of which ever recovered.
_BOND_STORM_THRESHOLD = 4
_BOND_STORM_WINDOW_S = 20.0

#: Where BlueZ keeps its keys. Read directly because the D-Bus view is not
#: reliable here: an adapter with a bond file on disk reported an empty bond
#: list over ``org.bluez`` -- observed on hci3 while the console was actively
#: resuming against that very key. Anything deciding whether to delete a bond
#: must not act on the view that can silently say "none".
_BLUEZ_STATE_DIR = "/var/lib/bluetooth"


#: bluetoothd's configuration file, and the two settings the BLE transport
#: depends on. Shipped as packaging/bluetooth-main.conf.snippet.
_BLUEZ_MAIN_CONF = "/etc/bluetooth/main.conf"

#: Missing this costs the operator an input dropout every ~35 seconds, and the
#: link looks perfectly healthy from every counter we have.
_REVERSE_DISCOVERY_WARNING = (
    "bluetoothd is acting as a GATT client toward the console. It sends an "
    "Exchange MTU Request the console never answers, and ATT closes the bearer "
    "30 s later -- so input stops every ~35 seconds and the link is dropped. "
    "Add to /etc/bluetooth/main.conf:  [General] ReverseServiceDiscovery = "
    "false  and  [GATT] Client = false, then restart bluetooth. See "
    "packaging/bluetooth-main.conf.snippet."
)


def _config_bool(text: str, section: str, key: str) -> bool | None:
    """Read one boolean from an ini-style file, honouring sections.

    Returns None when the key is absent or commented out, which is a different
    answer from False and is the case we warn about: bluetoothd's default is
    true. Written by hand rather than with configparser because main.conf has
    duplicate keys across sections and configparser rejects the file outright.
    """
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().lower()
            continue
        if current != section.lower() or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip().lower() != key.lower():
            continue
        return value.strip().strip('"').lower() in ("false", "no", "0", "off")
    return None


def _reverse_discovery_disabled(path: str = _BLUEZ_MAIN_CONF) -> bool | None:
    """True if bluetoothd's LE GATT client is switched off. None if unreadable.

    None rather than a guess: on a machine where we cannot read the file --
    a container, an unusual distribution -- warning about a setting we cannot
    see would be noise, and staying silent is the honest answer.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    return bool(_config_bool(text, "General", "ReverseServiceDiscovery"))


def _bonds_on_disk(bd_addr: str) -> list[str]:
    """Every peer BlueZ holds a key for on this adapter, per the filesystem.

    Read from disk for the same reason as :func:`_bond_exists`: the D-Bus view
    has been observed reporting none while the key was plainly there and in
    use. A refusal that depends on "do we hold a bond" must not be defeated by
    a view that can wrongly answer no.
    """
    import os

    root = os.path.join(_BLUEZ_STATE_DIR, bd_addr.upper())
    try:
        return [
            name for name in os.listdir(root)
            if os.path.exists(os.path.join(root, name, "info"))
        ]
    except OSError:
        return []


def _bond_exists(bd_addr: str, peer: str) -> bool:
    """True if BlueZ holds a key for ``peer`` on ``bd_addr``, per the filesystem."""
    import os

    path = os.path.join(_BLUEZ_STATE_DIR, bd_addr.upper(), peer.upper(), "info")
    try:
        return os.path.exists(path)
    except OSError:
        return False


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
        link_policy=None,
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

        #: Every adapter seen this run. Objects are created once and mutated
        #: in place -- see server/bt/state.py for the rebuild bug this closes.
        self._registry = AdapterRegistry()

        #: BD_ADDRs this process has configured -- alias, gamepad class, MGMT
        #: pairing settings. Only these are reverted when an adapter is
        #: disabled, so an adapter we never enabled is never written to.
        self._configured = FlagSet(self._registry, "configured")

        #: BD_ADDRs already stopped from advertising. Separate from
        #: ``_configured`` because the hot-plug watcher reconciles every 10 s:
        #: without it, a disabled adapter was re-quieted on every pass, writing
        #: over D-Bus and logging forever instead of once.
        self._quieted = FlagSet(self._registry, "quieted")

        #: (adapter, peer) -> recent authentication failure times, for detecting
        #: a one-sided bond. See _note_auth_failure.
        self._auth_failures: dict[tuple[str, str], list[float]] = {}

        #: bd_addr -> when this adapter first looked orphaned. The condition
        #: has to hold for a while before it means anything; see
        #: _check_orphan_bonds.
        self._orphan_since: dict[str, int] = {}

        #: Serialises _reconcile_channels. See its docstring: overlapping
        #: passes raced to bind the same adapter's L2CAP PSMs.
        self._reconcile_lock = asyncio.Lock()
        self._watch_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

        #: bd_addr -> running HIDServer. Genuinely per-adapter: each owns its
        #: own L2CAP listeners bound to that radio.
        self._hid_servers: dict[str, object] = {}

        #: bd_addr -> LinkTuner. Also genuinely per-adapter: an HCI command
        #: channel is bound to one controller, and the settings it writes --
        #: flush timeout, link policy, supervision timeout -- are per
        #: connection. See server/bt/link.py for why none of this can go
        #: through BlueZ.
        self._tuners: dict[str, object] = {}

        #: bd_addr -> BLEPeripheral, when the operator has chosen the BLE
        #: transport. Mutually exclusive with an entry in _hid_servers: an
        #: adapter presents a controller on one radio or the other.
        self._ble: dict[str, object] = {}

        #: The latency policy applied to every link. One policy for the whole
        #: server: the adapters are four radios serving one console, so tuning
        #: them differently would only make one player's controller worse.
        self._link_policy = link_policy

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

        #: The management socket, when one could be opened. It replaces the
        #: `btmgmt` subprocess calls and, far more usefully, delivers adapter
        #: state changes as events instead of leaving us to discover them on a
        #: ten-second timer.
        self._mgmt = None

        #: Set from the MGMT reader thread when something changed, so the
        #: watcher reconciles immediately instead of at the next tick.
        self._mgmt_wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        #: bd_addr -> manufacturer string, read once from hciconfig. MGMT gives
        #: only a numeric company id, and this is display-only.
        self._manufacturers: dict[str, str] | None = None

    @property
    def _adapters(self) -> dict[str, AdapterState]:
        """The registry's live mapping, by BD_ADDR.

        A property rather than a field so there is exactly one place adapters
        live. It is the real dict, not a copy: assigning into it is how tests
        seed a specific adapter, and a copy would make that a silent no-op.
        """
        return self._registry.states

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> bool:
        """Discover adapters and bring up the enabled ones. Returns True if any are live."""
        if not _have_bluetooth_tools():
            log.error(
                "Bluetooth tools not found (need bluetoothctl or hciconfig). "
                "Install bluez, or run with --mock-bt."
            )
            return False

        self._loop = asyncio.get_running_loop()
        self._mgmt_wake = asyncio.Event()
        await self._start_mgmt()

        await self.rescan()

        if not self._adapters:
            log.warning("No Bluetooth adapters detected")
            return False

        self._stop.clear()
        self._watch_task = asyncio.create_task(self._watch_hotplug())
        return self._router.capacity > 0

    async def _start_mgmt(self) -> None:
        """Subscribe to adapter state changes. Best effort.

        Without it everything still works on the ten-second reconcile, just
        late -- so a failure here is a warning and a slower server, never a
        refusal to start.
        """
        try:
            from server.bt import mgmt
        except ImportError:
            return

        if not mgmt.is_supported():
            return

        socket_ = mgmt.MGMTSocket()
        try:
            socket_.open()
        except mgmt.MGMTError as exc:
            log.warning(
                "No Bluetooth management socket (%s). Adapter changes will be "
                "picked up by the 10 s reconcile instead of immediately.",
                exc,
            )
            return

        socket_.add_listener(self._on_mgmt_event)
        socket_.start()
        self._mgmt = socket_

        version = socket_.version()
        log.info(
            "Watching adapter state over the management socket%s",
            f" (MGMT {version[0]}.{version[1]})" if version else "",
        )

    def _on_mgmt_event(self, event: int, index: int, params: bytes) -> None:
        """Handle one MGMT event. Runs on the reader thread.

        Deliberately does almost nothing: it flips an asyncio event and
        returns. The reconcile it triggers touches D-Bus, the router and the
        HID servers, none of which belong on a socket reader thread -- and a
        listener that blocks stalls delivery for every other listener.
        """
        from server.bt import mgmt

        if event not in (
            mgmt.EV_INDEX_ADDED,
            mgmt.EV_INDEX_REMOVED,
            mgmt.EV_NEW_SETTINGS,
            mgmt.EV_CLASS_OF_DEV_CHANGED,
            mgmt.EV_DEVICE_CONNECTED,
            mgmt.EV_DEVICE_DISCONNECTED,
            mgmt.EV_NEW_LINK_KEY,
            mgmt.EV_DEVICE_UNPAIRED,
            mgmt.EV_AUTH_FAILED,
        ):
            return

        log.debug(
            "MGMT %s on index %d", mgmt.EVENT_NAMES.get(event, hex(event)), index
        )

        # A host attaching or leaving is the one event worth acting on here
        # rather than merely reconciling for. It is also the **only** signal the
        # BLE path has: HIDServer reports it for Classic, and nothing did for
        # BLE, so an adapter carrying a live console still showed as
        # "listening" with no peer -- and the GUI never offered Disconnect.
        if event == mgmt.EV_AUTH_FAILED:
            peer = mgmt.parse_device_event(params)
            loop = self._loop
            if peer is not None and loop is not None:
                try:
                    loop.call_soon_threadsafe(self._note_auth_failure, index, peer)
                except RuntimeError:
                    pass

        if event in (mgmt.EV_DEVICE_CONNECTED, mgmt.EV_DEVICE_DISCONNECTED):
            peer = mgmt.parse_device_event(params)
            if peer is not None:
                loop, _ = self._loop, None
                if loop is not None:
                    try:
                        loop.call_soon_threadsafe(
                            self._note_link, index, peer,
                            event == mgmt.EV_DEVICE_CONNECTED,
                        )
                    except RuntimeError:
                        pass

        loop, wake = self._loop, self._mgmt_wake
        if loop is None or wake is None:
            return
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            # Loop already closed; we are shutting down.
            pass

    async def stop(self) -> None:
        self._stop.set()
        if self._watch_task is not None:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            self._watch_task = None

        if self._mgmt is not None:
            self._mgmt.close()
            self._mgmt = None

        for bd_addr in list(self._hid_servers):
            await self._stop_hid(bd_addr)

        # Sweep any tuner whose HID server never started -- _stop_hid only
        # reaches the ones that did, and a tuner outlives its adapter as an
        # open socket against hardware that may already be gone.
        for tuner in self._tuners.values():
            tuner.close()
        self._tuners.clear()

        # Any BLE peripheral whose _stop_hid never ran. Its advertisement dies
        # with the MGMT socket either way -- instances are owned by the socket
        # that added them -- but the GATT registration is bluetoothd's and
        # would outlive us.
        for peripheral in list(self._ble.values()):
            with contextlib.suppress(Exception):
                await peripheral.stop()
        self._ble.clear()

        await self._release_sdp()
        await self._release_agent()

        # The D-Bus connection is shared across every call in adapter_dbus, so
        # it is released here rather than by whichever call happened to be last.
        adapter_dbus.close_shared()

    # -- discovery ---------------------------------------------------------

    async def rescan(self) -> list[AdapterInfo]:
        """Re-enumerate adapters and reconcile the router against what is present.

        The observations returned by :meth:`_enumerate` are **thrown away**.
        Their fields are copied onto the long-lived :class:`AdapterState`
        objects the registry owns, which is the whole point: this used to
        replace every adapter object wholesale, so anything transient held on
        one -- the pairing countdown, a HID failure -- was silently lost every
        ten seconds. Two fields were hand-copied across the rebuild to paper
        over it, which works exactly until someone adds a third.
        """
        observed = await asyncio.to_thread(self._enumerate)
        found = {a.bd_addr: a for a in observed}

        added, removed, changed = self._registry.sync(found)

        for bd_addr in removed:
            self._router.remove_channel(bd_addr)

        # Apply persisted enable/disable choices, keyed by BD_ADDR.
        for state in self._registry.all():
            saved = self._config.adapter(state.bd_addr)
            if saved is not None:
                state.enabled = saved.enabled
                state.number = saved.number

        # If nothing has been configured yet, enable up to the ceiling so a
        # fresh install works without the operator touching anything.
        if not self._config.adapters:
            for index, state in enumerate(self._registry.all()):
                state.enabled = index < MAX_OUTPUTS

        await self._reconcile_channels()
        expired = await self._expire_pairing_windows()

        if (added or removed or changed or expired) and self.on_change:
            self.on_change()

        return self._registry.all()

    def _enumerate(self) -> list[AdapterInfo]:
        """List the adapters present, preferring the management socket.

        MGMT is authoritative and free: the kernel hands over each adapter's
        index, address, powered state and class in one round trip, with no
        subprocess, no output parsing and no timeout to burn. ``hciconfig``
        remains the fallback for a machine where the socket could not be
        opened.

        Running through MGMT also removes the last reason for this to be slow.
        The old path spawned ``hciconfig -a`` and scraped its output on every
        reconcile -- every ten seconds, for the life of the process.
        """
        if self._mgmt is None:
            return _enumerate_adapters()

        try:
            found = self._mgmt.read_all()
        except Exception:
            log.debug("MGMT enumeration failed; falling back to hciconfig", exc_info=True)
            return _enumerate_adapters()

        if not found:
            return _enumerate_adapters()

        # hciconfig is the only source of a human-readable manufacturer string,
        # and it is display-only, so it is worth one call but not worth failing
        # over. Cached across rescans because it never changes for a given
        # dongle.
        if self._manufacturers is None:
            self._manufacturers = {
                a.bd_addr: a.manufacturer for a in _enumerate_adapters()
            }

        return [
            AdapterInfo(
                bd_addr=settings.bd_addr,
                # `index` is the field that matters: AdapterState derives
                # hci_name from it, so an observation that sets only the name
                # produces "hci-1" from the default index and every subsequent
                # hciconfig call fails against a device that does not exist.
                index=settings.index,
                hci_name=f"hci{settings.index}",
                manufacturer=self._manufacturers.get(settings.bd_addr, ""),
                powered=settings.powered,
                connectable=settings.connectable,
                discoverable=settings.discoverable,
                bondable=settings.bondable,
                ssp=settings.ssp,
                link_security=settings.link_security,
                device_class=settings.device_class,
                # Both of these gate real actions -- health() skips its BR/EDR
                # checks on an LE-only radio, and _ensure_ble_ready switches a
                # drifted one back, which is a power cycle. Omitting them here
                # left the dataclass defaults (br/edr on) standing in for a
                # reading, so all four adapters were power-cycled at every
                # startup for a drift that had not happened. Every field on
                # this observation must be *read*, never defaulted.
                bredr=settings.bredr,
                secure_conn=settings.secure_conn,
                settings_known=True,
            )
            for settings in found.values()
        ]

    async def _expire_pairing_windows(self) -> bool:
        """Take an adapter out of discoverable mode once its window has run out.

        BlueZ's own ``DiscoverableTimeout`` ends the window from MGMT's point of
        view -- it emits ``New Settings`` without ``discoverable`` -- but it
        never writes scan enable back down. The controller stays at ``0x03``,
        still answering inquiries, so the gamepad keeps appearing in the host's
        *Add a device* list long after the window closed. Nothing in MGMT or
        D-Bus reports this; the only honest reading is
        ``hcitool -i hciX cmd 0x03 0x0019``.

        Re-asserting ``Discoverable=False`` through bluetoothd is what actually
        rewrites scan enable, and it is the same call the operator's own "stop
        pairing" button makes -- a path already known to work. Doing it here
        just means the timeout behaves like the button.

        ``Connectable`` is deliberately left alone: a host that bonded during
        the window reconnects by paging us, and clearing page scan here would
        undo the pairing the window existed to create.

        Returns True if any adapter changed, so callers can refresh the GUI.
        """
        from common.timing import now_ns

        now = now_ns()
        changed = False

        for adapter in list(self._adapters.values()):
            if not adapter.pairing_until_ns or now < adapter.pairing_until_ns:
                continue

            # Clear the deadline before awaiting, so a slow D-Bus call cannot
            # let the next reconcile pass start a second teardown for the same
            # window.
            adapter.clear_pairing(reason="window expired")
            changed = True

            log.info(
                "Pairing window on %s expired; clearing discoverable",
                adapter.hci_name,
            )
            # Pairable is cleared only on the Classic transport.
            #
            # A Classic controller is reachable by page scan without being
            # bondable, so false is the correct resting state there and the
            # window is what opens it. **BLE has no such split**: the
            # advertisement is the invitation to bond, it never stops, and a
            # peripheral that is advertising-but-unbondable accepts a
            # connection and can then do nothing with it.
            #
            # Clearing it here therefore quietly disabled BLE pairing a couple
            # of minutes after any window was armed -- long enough that it
            # looked like an intermittent console fault rather than a timer.
            ble = self._transport() == "ble"
            ok = await adapter_dbus.set_properties(
                adapter.hci_name,
                # Re-asserted for the same reason as in set_pairable: BlueZ
                # drops page scan by itself when a window ends without a bond,
                # leaving the adapter unreachable.
                connectable=True if adapter.enabled else None,
                discoverable=False,
                pairable=None if ble else False,
            )
            if not ok:
                # Not fatal: the window is over either way, and the adapter is
                # still reachable. Worth a line, because the symptom otherwise
                # is a controller that stays visible with nothing to explain it.
                log.warning(
                    "Could not clear discoverable on %s; it may keep answering "
                    "inquiries until something else resets it",
                    adapter.hci_name,
                )

            server = self._hid_servers.get(adapter.bd_addr)
            if server is not None:
                # Let outgoing reconnects resume: they were held off only for
                # the duration of the window.
                server.suspend_reconnect(0)

        return changed

    async def _reconcile_channels(self) -> None:
        """Make the router's channels match the enabled adapter set.

        Serialised, because two passes can otherwise interleave and fight over
        the same radio. This coroutine awaits inside its loop, and it is driven
        from two independent places -- the operator enabling an adapter in the
        web GUI, and the hot-plug watcher rescanning every 10 s. Both would see
        "no channel for this adapter yet", both would call :meth:`_start_hid`,
        and the second would hit EADDRINUSE against *our own* listener.

        That is not theoretical: it is how three adapters ended up bound to
        PSM 17 with no PSM 19. One pass won both PSMs, the other won control
        and lost interrupt -- and before the leak was fixed, the loser's
        control socket stayed bound for the life of the process, holding the
        PSM so no retry could ever succeed and leaving the adapter advertising
        a HID service it could not serve.
        """
        async with self._reconcile_lock:
            await self._reconcile_channels_locked()

    async def _reconcile_channels_locked(self) -> None:
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

        # Stop every adapter we configured but are not using from advertising.
        # Dropping the listeners alone left the radio broadcasting our name, the
        # gamepad class and the HID UUID with nothing behind it, so a host would
        # find a controller and get no answer.
        #
        # Covers adapters disabled in *this* run and ones left advertising by a
        # previous one -- a persisted number is durable proof we brought it up
        # once, and survives the restart that clears the in-memory set.
        for adapter in self._adapters.values():
            if adapter.bd_addr in enabled_addrs or adapter.bd_addr in self._quieted:
                continue
            saved = self._config.adapter(adapter.bd_addr)
            if saved is not None and saved.number:
                self._configured.add(adapter.bd_addr)
            await self._quiet_adapter(adapter.bd_addr)

        # Re-assert the link policy on adapters that are already running. A
        # controller reset discards it and says nothing, so an adapter can be
        # serving happily with sniff permitted again while its siblings are
        # correct. Read-then-write, so the ordinary pass writes nothing.
        for adapter in enabled:
            tuner = self._tuners.get(adapter.bd_addr)
            if tuner is not None:
                await asyncio.to_thread(tuner.ensure_adapter_defaults)

        # And that every enabled adapter is still answering pages.
        #
        # A safety net rather than the mechanism: the paths that can drop page
        # scan are fixed where they occur. But BlueZ takes Connectable away on
        # its own whenever an adapter has no bond -- which is every adapter
        # until someone pairs -- and an unreachable adapter is the one failure
        # that cannot recover by itself. It cannot accept a connection, so it
        # can never gain the bond that would have kept it connectable, and the
        # host reports only "We didn't get any response from the device".
        #
        # Read-then-write, so the ordinary pass writes nothing.
        await self._ensure_connectable(enabled)

        # Same shape of problem, different layer: the kernel re-applies its own
        # 30 s LE ping timeout when encryption completes, which lands *after*
        # our write on connect and silently undoes it. Holding it here as an
        # invariant is the only version that wins, because there is no event
        # for "the kernel has finished setting up this link".
        await self._ensure_le_ping(enabled)

        # And the three things a BLE peripheral needs that were each set once
        # at bring-up and never checked again. Same shape as the two above.
        await self._ensure_ble_ready(enabled)

        for adapter in enabled:
            if self._router.channel(adapter.bd_addr) is not None:
                continue

            # The **server-wide** setting wins over the per-adapter one.
            #
            # The profile is server-wide by necessity, not preference: BlueZ
            # publishes one HID service record per machine, so the descriptor a
            # console is told to expect is shared. `AdapterConfig.profile` is a
            # leftover from when the web GUI offered a per-adapter dropdown,
            # and leaving it authoritative meant changing the server-wide
            # profile silently did nothing -- every adapter kept whatever was
            # written against its address, and the only symptom was a console
            # being handed the wrong descriptor.
            saved = self._config.adapter(adapter.bd_addr)
            profile_name = (
                getattr(self._config, "controller_profile", "")
                or (saved.profile if saved else "")
                or self._default_profile
            )

            adapter.to(Phase.CONFIGURING, reason="bring-up")

            if not await asyncio.to_thread(_bring_up_adapter, adapter):
                log.error("Could not bring up %s (%s)", adapter.bd_addr, adapter.hci_name)
                adapter.hid_error = "adapter would not power on"
                adapter.to(Phase.DEGRADED, reason="bring-up failed")
                continue

            try:
                profile = create_profile(
                    profile_name,
                    **({"bd_addr": adapter.bd_addr} if profile_name == "switch_pro" else {}),
                )
            except ValueError as exc:
                log.error("Bad profile for %s: %s", adapter.bd_addr, exc)
                adapter.hid_error = f"bad profile: {exc}"
                adapter.to(Phase.DEGRADED, reason="bad profile")
                continue

            # Called for its side effect: it persists the adapter's number,
            # which adapter_name() then reads back out of the config.
            self._assign_number(adapter.bd_addr)
            name = self.adapter_name(adapter.bd_addr)

            # Set the advertised name and gamepad class as the adapter comes
            # up, not only during a pairing window -- a host scanning at any
            # other moment should still see the right name.
            await asyncio.to_thread(_ensure_pairing_settings, adapter)
            await asyncio.to_thread(_set_device_class, adapter)
            # `connectable=True` is what makes the radio answer pages at all.
            # Without it an adapter that has never been bonded sits with page
            # scan off: BlueZ only keeps an adapter connectable on its own when
            # it has bonded devices that might reconnect, so the three that are
            # already paired look fine and a fresh one is unreachable. That is a
            # trap that cannot open itself -- it cannot accept a connection, so
            # it can never gain the bond that would have made it connectable --
            # and the host reports only "We didn't get any response from the
            # device". Measured on hci4: Connectable=false, scan enable 0x00,
            # while the three bonded adapters read true / 0x02.
            await adapter_dbus.set_properties(
                adapter.hci_name, alias=name, connectable=True
            )
            # Remember that *we* configured this radio, so disabling it later
            # reverts only what we applied. An adapter we never touched is
            # never written to -- which is the guarantee the operator relies on
            # when the Pi uses one for something else.
            self._configured.add(adapter.bd_addr)
            self._quieted.discard(adapter.bd_addr)
            log.info("Adapter %s advertises as '%s'", adapter.hci_name, name)

            if self._transport() == "ble":
                await asyncio.to_thread(self._ensure_radio_mode, adapter)
                sink = await self._start_ble(adapter, profile)
            else:
                sink = await self._start_hid(adapter, profile)

            # DEGRADED rather than LISTENING when the HID stack could not
            # start. The adapter stays visible with its name and number so the
            # operator can see which dongle is unwell, but it must never go on
            # advertising: a host that finds a controller and then cannot
            # complete the connection reports only "try connecting your device
            # again", with the real reason in a log line written days earlier.
            adapter.to(
                Phase.LISTENING if sink is not None else Phase.DEGRADED,
                reason="HID stack started" if sink is not None else adapter.hid_error,
            )

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

        self._check_orphan_bonds(enabled)

        # Last, deliberately. Bring-up sets an adapter LISTENING as it finishes,
        # so anything that decided the adapter was LINKED earlier in this pass
        # would be overwritten -- and the peer-only comparison below would then
        # see nothing to correct and leave it wrong for good.
        await self._ensure_link_state(enabled)

    async def _ensure_connectable(self, adapters: list[AdapterState]) -> None:
        """Put page scan back on any enabled adapter that has lost it."""
        for adapter in adapters:
            if adapter.hid_error or adapter.phase is Phase.PAIRING:
                # A degraded adapter must not advertise, and one mid-window is
                # already being driven by set_pairable.
                continue
            if adapter.connectable:
                continue

            log.warning(
                "%s (%s) is not answering pages; restoring page scan. BlueZ "
                "drops Connectable on an adapter with no bond, which leaves it "
                "unreachable and unable to gain one.",
                adapter.hci_name, adapter.bd_addr,
            )
            await adapter_dbus.set_properties(adapter.hci_name, connectable=True)

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

        # The identity supplies who we claim to be; the profile supplies what we
        # can do. Keeping them separate matters: switching identity to satisfy a
        # picky console must not quietly change the report layout underneath it.
        identity = self._identity()
        descriptor = profile.descriptor
        try:
            self._sdp_bus = await register_hid_profile(
                self._base_device_name(),
                descriptor.report_descriptor,
                identity.vendor_id,
                identity.product_id,
                version=identity.version,
                vendor_source=identity.vendor_source,
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

    def _ensure_radio_mode(self, adapter: AdapterState) -> None:
        """Match the adapter's radio mode to the chosen transport.

        **A dual-mode adapter cannot advertise "BR/EDR Not Supported"**, and
        that flag is not cosmetic: it is how a BLE-only host tells a controller
        it can drive from one it cannot. With BR/EDR still enabled our
        advertisement reads "Simultaneous LE and BR/EDR", and the real 8BitDo
        64 that pairs with an Analogue 3D reads "BR/EDR Not Supported".

        Measured: with BR/EDR on, flags 0x1a; with it off, flags 0x06 -- and
        everything else about the advertisement identical. Nothing else we
        could change produced that bit, because the kernel derives the flags
        from the controller's capabilities rather than from anything we send.

        This is the one adapter *setting* the server writes, and it is written
        because the transport choice is meaningless without it. It is
        read-then-write and reversible: selecting the Classic transport puts
        BR/EDR back.

        The power cycle is required -- the controller will not change mode
        while it is up.
        """
        if shutil.which("btmgmt") is None or adapter.index < 0:
            return

        want_ble = self._transport() == "ble"
        index = str(adapter.index)

        settings = _read_mgmt_settings(index)
        if settings is None:
            return

        has_bredr = "br/edr" in settings
        has_sc = "secure-conn" in settings
        if has_bredr != want_ble and has_sc != want_ble:
            return          # already the mode we want, both halves

        log.info(
            "Switching %s to %s so the advertisement matches the transport",
            adapter.hci_name, "LE only" if want_ble else "dual mode",
        )
        # Secure Connections goes off with it, for the BLE transport only.
        #
        # SC is the stronger pairing method and normally the right default, but
        # it is not negotiable downward: a peer that asks for **Legacy** pairing
        # is refused outright. Measured against the console, which requests
        # "Bonding, No MITM, Legacy": with SC enabled every bond ended in
        # `bonding_attempt_complete ... status 0x5` (authentication failed) and
        # the link was torn down; with it off the bond completed and reports
        # flowed.
        #
        # The trade is a real reduction in pairing strength, and it is
        # deliberate: this is a game controller whose access control lives at
        # the RBGC layer -- password plus operator approval -- not at Bluetooth
        # pairing. The Classic transport is untouched.
        #
        # **The power cycle must complete even when the switch fails.** This
        # used to `return` on the first non-zero exit, which -- since `power
        # off` is the first command -- left the adapter switched *off*, with
        # one warning line as the only trace. Losing LE-only costs an adapter
        # its console; losing power costs it everything, including the Classic
        # transport and every diagnostic that reads it.
        #
        # And the middle command genuinely does fail: the kernel refuses to
        # clear BR/EDR while BR/EDR bonds exist, so any adapter previously
        # paired to a PC over Classic hits this on its first BLE bring-up.
        # The power cycle is required: a controller will not change mode while
        # it is up. It is also why the failure path below matters so much.
        code, output = _run(["btmgmt", "--index", index, "power", "off"])
        if code != 0:
            log.warning(
                "Could not power %s down to switch radio mode: %s. Leaving it "
                "on the current mode.",
                adapter.hci_name, output.strip(),
            )
            return

        try:
            for command in (
                ["btmgmt", "--index", index, "bredr", "off" if want_ble else "on"],
                ["btmgmt", "--index", index, "sc", "off" if want_ble else "on"],
            ):
                code, output = _run(command)
                if code == 0:
                    continue

                log.warning(
                    "Could not switch %s radio mode (%s): %s. A BLE-only host "
                    "may refuse a controller still advertising BR/EDR "
                    "support.%s",
                    adapter.hci_name, " ".join(command[-2:]), output.strip(),
                    (
                        " The kernel refuses to clear BR/EDR while the adapter "
                        "still holds BR/EDR bonds; clear them and retry."
                        if command[-2] == "bredr" and want_ble else ""
                    ),
                )
                break
        finally:
            # Unconditional, and in a finally: whatever happened above, the
            # adapter must come back up.
            code, output = _run(["btmgmt", "--index", index, "power", "on"])
            if code != 0:
                log.error(
                    "%s would not power back on after a radio-mode switch: %s. "
                    "The adapter is off and serves nothing.",
                    adapter.hci_name, output.strip(),
                )

    def _transport(self) -> str:
        """Which radio the adapters present a controller on."""
        return getattr(self._config, "controller_transport", "classic") or "classic"

    def _check_reverse_discovery(self, adapter: AdapterState) -> None:
        """Warn if bluetoothd will act as a GATT client on this transport.

        BLE only: on Classic the option merely disables reverse SDP, which is
        described upstream as needed for qualification and costs us nothing.

        The warning names the **symptom**, not the setting, because that is
        what makes it findable. This cost an evening precisely because the
        failure looks like a Bluetooth range or pairing problem: the console
        pairs, plays, and drops input every ~35 seconds while every counter on
        both sides stays healthy.
        """
        disabled = _reverse_discovery_disabled()
        if disabled is None or disabled:
            # Unreadable, or already correct. Nothing useful to say either way.
            adapter.host_config_warning = ""
            return

        adapter.host_config_warning = _REVERSE_DISCOVERY_WARNING
        log.warning("%s: %s", adapter.hci_name, _REVERSE_DISCOVERY_WARNING)

    async def _start_ble(self, adapter: AdapterState, profile) -> object | None:
        """Publish this adapter as a BLE HID gamepad instead of a Classic one.

        A different radio, not a different setting: a console that pairs with a
        BLE controller cannot see a Classic one at all, and the reverse. The
        two share the profile layer -- the report descriptor and the bytes of
        each report are identical -- and nothing else.

        Returns the sink, or None if it could not be published. Non-fatal for
        the same reason the Classic path is: the adapter stays visible and
        inert rather than vanishing.
        """
        if adapter.bd_addr in self._ble:
            return self._ble[adapter.bd_addr].sink

        # Check the host's bluetoothd configuration before publishing anything.
        # This is the same discipline as _set_device_class reading the class
        # back: an external prerequisite is verified and reported, never
        # silently assumed. Warn only -- an untuned link still carries reports,
        # and refusing to start would trade a mostly-working controller for
        # none.
        self._check_reverse_discovery(adapter)

        try:
            from server.bt.ble.peripheral import BLEPeripheral
        except ImportError as exc:
            adapter.hid_error = f"BLE support unavailable: {exc}"
            log.error("BLE support unavailable (%s)", exc)
            return None

        # Without an agent, bluetoothd cannot complete pairing and answers an
        # incoming SMP Pairing Request with **"Pairing not supported"**. The
        # Classic path registers one from _start_hid; the BLE path did not, so
        # a console would connect, request bonding in the ordinary way, be
        # refused, and drop the link -- with nothing on our side logged.
        #
        # Measured: the console sent Pairing Request (NoInputNoOutput, Bonding,
        # No MITM) and got Pairing Failed (0x05) back.
        await self._ensure_agent()

        peripheral = BLEPeripheral(
            adapter.hci_name,
            profile,
            self._identity(),
            name=self.adapter_name(adapter.bd_addr),
            mgmt=self._mgmt,
            index=adapter.index,
            # Four adapters running one identity are four units of the same
            # product, not one pad seen four times -- see serial_number().
            bd_addr=adapter.bd_addr,
        )
        # Decide what the **first** advertisement asks for, before it goes out.
        #
        # The peripheral defaults to pairing mode, and the reconcile corrects
        # it -- but that is up to ten seconds later, during which a bonded
        # adapter advertises "pair me" and the console it belongs to ignores
        # it entirely. Every restart cost a reconnection delay for no reason.
        peripheral.set_pairing_mode(not _bonds_on_disk(adapter.bd_addr))

        try:
            await peripheral.start()
        except Exception as exc:
            adapter.hid_error = str(exc).splitlines()[0]
            log.error("Could not publish a BLE gamepad on %s: %s", adapter.hci_name, exc)
            return None

        # A BLE HID peripheral must be bondable for as long as it advertises.
        #
        # This is the opposite of the Classic policy, and deliberately so.
        # There, Pairable is held **false** outside a bounded pairing window --
        # the correct resting state, because a Classic controller is reachable
        # by page scan without being bondable.
        #
        # HOGP has no such split. The Report characteristic requires an
        # encrypted link, encryption requires a bond, and the advertisement
        # *is* the invitation to bond. An advertising peripheral that refuses
        # bonding is not a controller waiting to be paired; it is a device that
        # accepts a connection and then cannot do anything with it.
        #
        # Measured: a host connected 21 times in three minutes, each time
        # dropping the link immediately, with `Pairable=false` the only thing
        # standing in the way.
        #
        # The trade is the same one the Classic agent already makes, and is
        # stated there: any host may bond with a controller that is
        # advertising. Access control lives at the RBGC layer -- password plus
        # operator approval -- not at Bluetooth pairing.
        # timeout_s=0 means "never expires". Pairable otherwise reverts on
        # BlueZ's PairableTimeout, and this project *sets* that timeout when it
        # arms a Classic pairing window -- so a leftover value silently turned
        # a BLE peripheral unbondable a couple of minutes after start-up, while
        # it carried on advertising as though it could be paired.
        #
        # A BLE HID peripheral advertises continuously and the advertisement is
        # the invitation to bond, so there is no window for it to sit outside.
        ok = await adapter_dbus.set_properties(
            adapter.hci_name, pairable=True, timeout_s=0
        )
        if not ok:
            log.warning(
                "%s is advertising a BLE gamepad but would not accept Pairable. "
                "Hosts will connect and drop straight away: HID over GATT needs "
                "an encrypted link, and encryption needs a bond.",
                adapter.hci_name,
            )

        adapter.hid_error = ""
        self._ble[adapter.bd_addr] = peripheral
        return peripheral.sink

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

        if adapter.bd_addr in self._hid_servers:
            # Already serving. Belt and braces behind the reconcile lock: a
            # second bind here fails with EADDRINUSE against our own listener,
            # which reads exactly like bluetoothd holding the HID role.
            log.debug("HID already running on %s", adapter.bd_addr)
            return None

        if not is_supported():
            log.error("This platform has no AF_BLUETOOTH support; cannot serve HID")
            return None

        await self._ensure_agent()

        if not await self._ensure_sdp(profile):
            return None

        tuner = self._start_tuner(adapter)
        sink = L2CAPSink(
            profile,
            adapter.bd_addr,
            policy=tuner.policy if tuner else None,
            tuner=tuner,
        )
        server = HIDServer(
            adapter.bd_addr,
            profile,
            sink,
            on_host_connected=lambda host, addr=adapter.bd_addr: (
                self._remember_host(addr, host),
                self._on_host_connected(addr, host),
            ),
            on_host_disconnected=lambda host, addr=adapter.bd_addr: (
                self._on_host_gone(addr, host)
            ),
            on_rumble=self._on_rumble,
        )

        try:
            await asyncio.to_thread(server.start)
        except OSError as exc:
            # start() already turns the two classic failures -- EADDRINUSE from
            # bluetoothd's input plugin, and EPERM from missing privileges --
            # into actionable messages. Surface them verbatim.
            log.error("HID server failed on %s: %s", adapter.bd_addr, exc)
            # Record it on the adapter too. The log line above is the whole
            # diagnosis, and it goes nowhere if the server was started from a
            # terminal that has since closed -- which is precisely what
            # happened, leaving three adapters advertising a HID service they
            # could not serve.
            adapter.hid_error = str(exc).splitlines()[0]
            return None

        adapter.hid_error = ""
        self._hid_servers[adapter.bd_addr] = server

        # Restore the host to reconnect to, so the link comes back on its own
        # after a restart instead of needing someone to click Connect.
        #
        # **BlueZ is the authority, not our config.** The saved address is only
        # a preference for *which* bond to prefer when an adapter has several;
        # if BlueZ holds no bond for this adapter there is nothing to reconnect
        # to, whatever the config says. That is the difference between a host
        # that is merely switched off and one whose key we no longer have, and
        # it is what stops the reconnect loop paging a host that can never
        # accept us -- previously forever, at debug level.
        target = await self._reconnect_target_for(adapter)
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

    def _start_tuner(self, adapter: AdapterInfo):
        """Open this adapter's HCI command channel and set its link defaults.

        Non-fatal by design. A controller that refuses these commands, or a
        kernel that will not give us a raw HCI socket, still carries a perfectly
        usable HID link -- it just runs on BlueZ's defaults, which means an
        infinite flush timeout and sniff mode permitted. Refusing to bring the
        adapter up over that would trade a working controller for better tail
        latency, which is the wrong way round.

        The failure is reported rather than swallowed, because "this adapter is
        untuned" is otherwise indistinguishable from "this adapter is fine" and
        the difference only shows up as jitter days later.
        """
        existing = self._tuners.get(adapter.bd_addr)
        if existing is not None:
            return existing

        try:
            from server.bt.link import LinkPolicy, LinkTuner
        except ImportError:
            return None

        if not adapter.hci_name.startswith("hci"):
            # bluetoothctl-only enumeration stores the address here rather than
            # an index, and HCI sockets are bound by index. Without a real one
            # there is nothing to open.
            log.debug("No hciX index for %s; link tuning unavailable", adapter.bd_addr)
            return None

        try:
            index = int(adapter.hci_name.removeprefix("hci"))
        except ValueError:
            return None

        tuner = LinkTuner(index, self._link_policy or LinkPolicy())
        if not tuner.open():
            return None

        # Stamp the policy onto the controller before any connection exists, so
        # an incoming link is already correct at the instant it comes up rather
        # than being sniff-capable until we get round to tuning it.
        tuner.apply_adapter_defaults()
        self._tuners[adapter.bd_addr] = tuner
        return tuner

    async def _reconnect_target_for(self, adapter: AdapterState) -> str:
        """Which host this adapter should try to reconnect to, if any.

        Bonds come from BlueZ. The persisted ``paired_target`` only chooses
        between them when an adapter has more than one, and is ignored when it
        names a host BlueZ has no key for.
        """
        bonds = await adapter_dbus.list_bonds(adapter.hci_name)
        adapter.bonds = tuple(bonds)

        saved = self._config.adapter(adapter.bd_addr)
        preferred = (saved.paired_target if saved else "").upper()

        if not bonds:
            if preferred:
                log.info(
                    "%s remembers host %s but BlueZ holds no bond for it, so there "
                    "is nothing to reconnect to. Enter pairing mode to bond again.",
                    adapter.hci_name, preferred,
                )
            return ""

        if preferred in bonds:
            return preferred

        if preferred:
            log.info(
                "%s remembered host %s, which is no longer bonded; using %s instead",
                adapter.hci_name, preferred, bonds[0],
            )
        return bonds[0]

    def _note_link(self, index: int, peer: str, connected: bool) -> None:
        """Record that a host attached to, or left, the adapter at ``index``.

        Driven from MGMT so it covers **both** transports. The Classic path
        also reports this through HIDServer's callbacks, which carry more
        (they know when the HID session itself is up); this is the only source
        for BLE, where bluetoothd owns the link and nothing else would tell us.
        """
        adapter = next(
            (a for a in self._registry.all() if a.index == index), None
        )
        if adapter is None:
            return

        if connected:
            adapter.peer = peer
            # The window has done its job, so stop counting.
            #
            # ``_on_host_connected`` does this for Classic; the BLE path had no
            # equivalent, so a paired controller went on showing "Waiting for a
            # console to connect... Ns left" while that console was attached
            # and playing. Not only untidy: an expired window later triggers
            # ``_expire_pairing_windows``, which writes ``Discoverable=False``
            # on a live link for a window nobody still wants.
            adapter.clear_pairing(reason=f"{peer} connected")
            adapter.to(Phase.LINKED, reason=f"{peer} connected")
        else:
            adapter.peer = ""
            if adapter.phase is Phase.LINKED:
                adapter.to(Phase.LISTENING, reason=f"{peer} disconnected")

        # Tell the BLE sink too. It has no other way to learn this: bluetoothd
        # owns the LE link, and the GATT callbacks only fire for reads, writes
        # and subscription changes -- none of which a bonded host performs when
        # it simply reconnects.
        peripheral = self._ble.get(adapter.bd_addr)
        if peripheral is not None:
            if connected:
                # Before set_link, and unconditionally: Sleep detaches the
                # sink, so a woken controller would otherwise come back with a
                # live encrypted link that carries no input -- reports
                # discarded in silence, GUI showing it connected.
                peripheral.attach_sink(peer)
            peripheral.sink.set_link(connected, peer)
            if connected:
                # In a thread: this is blocking socket I/O and _note_link runs
                # on the asyncio loop, which is also the thread BLESink
                # notifies from. See _ensure_le_ping.
                self._run_off_loop(self._extend_le_ping, index, peer)

        if self.on_change:
            self.on_change()

    def _note_auth_failure(self, index: int, peer: str) -> None:
        """Repair a one-sided bond, which neither end recovers from alone.

        A bond has two halves. When only one survives, the link comes up and
        dies immediately, forever, and **nothing in any log says why** -- the
        operator sees a controller flickering between connected and not.

        Which half survived decides what it looks like, and only one of the two
        is ours to fix:

        * **We kept the key, the peer did not.** We send an SMP Security
          Request, the peer answers Pairing Failed, we disconnect. Our key is
          an orphan: it can never work again, because the peer will not
          re-pair while we keep asserting a bond it has no half of. Deleting
          it lets the next attempt pair cleanly. **This we can fix.**

        * **The peer kept the key, we did not.** It sends an LE Long Term Key
          Request, we answer negative, it disconnects. Nothing we hold can
          satisfy it and a console usually offers no way to forget a
          controller, so all we can do is say so plainly instead of letting
          the operator watch it flicker.

        Deleting the orphan is safe *because it is proven orphaned* -- the peer
        refused it seconds ago. That is the opposite of deleting a live bond,
        which strands a console permanently and is the mistake this whole
        mechanism exists to stop being repeated by hand.
        """
        adapter = next(
            (a for a in self._registry.all() if a.index == index), None
        )
        if adapter is None:
            return

        now = time.monotonic()
        key = (adapter.bd_addr, peer)
        recent = [t for t in self._auth_failures.get(key, ()) if now - t < _BOND_STORM_WINDOW_S]
        recent.append(now)
        self._auth_failures[key] = recent

        if len(recent) < _BOND_STORM_THRESHOLD:
            return

        # Only act once per storm; a repair takes effect on the next attempt.
        self._auth_failures[key] = []
        self._run_off_loop(self._repair_one_sided_bond, adapter.bd_addr, peer)

    def _repair_one_sided_bond(self, bd_addr: str, peer: str) -> None:
        """Drop our orphaned half, or explain that the peer holds the orphan."""
        import asyncio as _asyncio

        held = _bond_exists(bd_addr, peer)
        if not held:
            log.error(
                "%s and %s cannot agree on a bond: it keeps asking us to resume "
                "with a key we do not have, so the link dies immediately and "
                "retries forever. We hold nothing to delete -- the stale half is "
                "on the other device, and it must forget this controller before "
                "it can pair again.",
                bd_addr, peer,
            )
            return

        log.warning(
            "%s holds a bond for %s that %s no longer has -- it refused to "
            "encrypt with it. Removing our orphaned half so the next attempt "
            "can pair cleanly.",
            bd_addr, peer, peer,
        )
        loop = self._loop
        if loop is None:
            return
        try:
            _asyncio.run_coroutine_threadsafe(
                self._forget_peer(bd_addr, peer), loop
            ).result(timeout=20)
        except Exception:
            log.debug("Could not remove the orphaned bond", exc_info=True)

    async def _forget_peer(self, bd_addr: str, peer: str) -> None:
        adapter = self._adapters.get(bd_addr)
        if adapter is None:
            return
        from server.bt import adapter_dbus

        await adapter_dbus.remove_device(adapter.hci_name, peer)
        adapter.bonds = [b for b in adapter.bonds if b.upper() != peer.upper()]
        if self.on_change:
            self.on_change()

    def _extend_le_ping(self, index: int, peer: str) -> None:
        """Stop the kernel hanging up on a console that never transmits.

        The Authenticated Payload Timeout defaults to 30 s and assumes both
        ends of an encrypted link talk. A console does not talk to a gamepad --
        it subscribes and listens -- so nothing authenticated ever arrives from
        it, and the kernel tears down a link that was carrying reports
        perfectly. Measured against an Analogue 3D, six times in three minutes.

        **This call alone is not enough, and that is not a bug here.** The
        kernel writes its own 30 s value when encryption completes, which is
        after this runs, so this write is routinely overwritten seconds later.
        Doing it anyway costs one command and covers the case where encryption
        was already up; :meth:`_ensure_le_ping` from the reconcile pass is what
        actually holds the line.

        Never fatal: an untuned link still carries reports, it just gets
        dropped periodically.
        """
        from server.bt.link import LEPingTuner

        try:
            LEPingTuner(index).tune_peer(peer)
        except Exception:
            log.debug("LE ping tuning failed for %s on hci%d", peer, index,
                      exc_info=True)

    def _run_off_loop(self, fn, *args) -> None:
        """Run blocking work in a thread without waiting for it.

        For callers on the asyncio loop that must not block it even briefly.
        Failures are swallowed by the callee; nothing here is load-bearing
        enough to justify propagating an exception into the loop.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            fn(*args)
            return
        loop.run_in_executor(None, fn, *args)

    async def _ensure_le_ping(self, adapters: list[AdapterState]) -> None:
        """Re-assert the LE ping timeout on every live BLE link.

        The kernel resets it to 30 s on every encryption change, and there is
        no event that means "the kernel has finished with this link" -- so the
        only reliable version is to hold it as an invariant, exactly as
        ``_ensure_connectable`` does for page scan.

        Measured: without this the console reconnected every 34.7 s forever,
        with our own log line cheerfully reporting the timeout set to 655 s
        moments before the kernel put it back.

        Read-then-write, so the ordinary pass issues one command and writes
        nothing. The 10 s reconcile is comfortably inside the 30 s window it is
        racing, so a link is corrected long before the timeout could fire.
        """
        from server.bt.link import LEPingTuner

        targets = [
            (a.index, a.peer)
            for a in adapters
            if a.phase is Phase.LINKED and a.peer and a.bd_addr in self._ble
        ]
        if not targets:
            return

        def _apply() -> None:
            for index, peer in targets:
                try:
                    LEPingTuner(index).ensure_peer(peer)
                except Exception:
                    log.debug(
                        "Could not re-assert the LE ping timeout on hci%d",
                        index, exc_info=True,
                    )

        # **Never inline.** This is an ioctl plus two HCI commands, each with a
        # one-second timeout, and the reconcile pass runs on the asyncio loop --
        # which is the same thread BLESink notifies the console from. Running it
        # inline stalled the loop for ~4 s every 10 s: notifications stopped
        # dead, and the kernel then dropped the idle link. Measured as a console
        # that worked for 30 s at a time and then went unresponsive for several
        # seconds, which is a far worse symptom than the timeout this fixes.
        await asyncio.get_running_loop().run_in_executor(None, _apply)

    async def _ensure_ble_ready(self, adapters: list[AdapterState]) -> None:
        """Hold the three things a BLE peripheral needs but cannot keep.

        Each was set **once**, at channel creation, and never checked again --
        the shape of failure this subsystem keeps reproducing. Anything that
        disturbs one wins permanently, and all three present to the operator
        identically, as a console that will not connect:

        * **Pairable.** HOGP has no split between reachable and bondable: the
          Report characteristic needs an encrypted link, encryption needs a
          bond, and the advertisement is the invitation to bond. Cleared, the
          console connects and drops immediately, forever.
        * **The radio mode.** A dual-mode adapter advertises "Simultaneous LE
          and BR/EDR" where a BLE-only host is looking for "BR/EDR Not
          Supported", and a controller reset silently puts BR/EDR back.
        * **The advertising instance.** It does not survive a controller power
          cycle, and nothing reports its loss.

        Every check is read-then-write, so the ordinary pass writes nothing.
        Never fatal: a peripheral that refuses one of these is still carrying
        whatever link it already has, and failing the reconcile would take
        three working controllers down over the fourth.
        """
        if self._transport() != "ble":
            return

        targets = [a for a in adapters if a.bd_addr in self._ble and not a.hid_error]
        if not targets:
            return

        for adapter in targets:
            # `bondable` is the MGMT face of org.bluez.Adapter1.Pairable, and
            # it is refreshed from the event stream on every pass -- so this
            # costs nothing, where a D-Bus read_properties would be a round
            # trip per adapter every ten seconds for the life of the process.
            # Same reasoning as _ensure_connectable reading adapter.connectable.
            if adapter.bondable:
                continue

            log.warning(
                "%s (%s) is advertising a BLE gamepad but is not bondable. "
                "Hosts will connect and drop straight away: HID over GATT "
                "needs an encrypted link, and encryption needs a bond. "
                "Restoring Pairable.",
                adapter.hci_name, adapter.bd_addr,
            )
            await adapter_dbus.set_properties(
                adapter.hci_name, pairable=True, pairable_timeout_s=0
            )

        # Whether the radio mode has drifted is answerable from the MGMT
        # settings already on the state object, so the ordinary pass spawns no
        # subprocess at all. Asking btmgmt would be four `btmgmt info` children
        # every ten seconds for the life of the process -- the cost the MGMT
        # rewrite removed, quietly put back.
        drifted = [
            a for a in targets if a.settings_known and (a.bredr or a.secure_conn)
        ]

        # Both of these are blocking: btmgmt subprocesses for the radio mode,
        # an MGMT round trip for the advertisement. **Never inline** -- see the
        # note in _ensure_le_ping, whose inline version stalled the loop the
        # console's notifications are written from.
        def _apply() -> None:
            for adapter in drifted:
                # No log line here. _ensure_radio_mode reads the radio itself
                # and reports only when it actually switches -- announcing the
                # drift first meant every startup warned about four adapters
                # that turned out to be fine, which is how a warning stops
                # being read.
                try:
                    self._ensure_radio_mode(adapter)
                except Exception:
                    log.debug(
                        "Could not re-check the radio mode on %s",
                        adapter.hci_name, exc_info=True,
                    )

            for adapter in targets:
                peripheral = self._ble.get(adapter.bd_addr)
                if peripheral is None:
                    continue
                try:
                    # An adapter that has gained a bond must stop asking to be
                    # paired and start asking its console back, and one that
                    # lost its bond must do the reverse. Neither is announced
                    # by anything, and the flag is baked into the advertising
                    # instance -- so a change means restarting it, which is
                    # what the force here is for.
                    #
                    # A window that is still open keeps pairing mode: the
                    # operator asked for it and it has not expired yet.
                    pairing = not adapter.bonds
                    if adapter.pairing_until_ns:
                        pairing = True
                    changed = peripheral.set_pairing_mode(pairing)

                    # A forced restart clears the suppression latch, so it
                    # must not be reached for an adapter the operator switched
                    # off. Reset leaves every controller unpaired *and* off,
                    # which is a mode change and a suppression at once -- the
                    # obvious version put all four straight back on the air
                    # ten seconds later, asking to pair.
                    if changed and not peripheral.suppressed:
                        peripheral.ensure_advertising(force=True)
                    else:
                        peripheral.ensure_advertising()
                except Exception:
                    log.debug(
                        "Could not re-check advertising on %s",
                        adapter.hci_name, exc_info=True,
                    )

        await asyncio.get_running_loop().run_in_executor(None, _apply)

    async def _ensure_link_state(self, adapters: list[AdapterState]) -> None:
        """Reconcile who is connected against what the radio says.

        **Device Connected fires once, at the moment of connection.** Subscribe
        after a console has already attached -- which is every server restart
        during a session -- and no event ever arrives for that link. Nothing
        else told us either: MGMT enumeration returns adapter *settings*, and
        the BLE path has no HIDServer callbacks to fall back on.

        Measured on the reference Pi: hci3 held a live, authenticated,
        encrypted LE link to the console while the server reported it
        ``listening`` with no peer. The GUI said "Waiting for console" over a
        working controller, offered no Disconnect, and the LE ping timeout was
        never extended on that link -- so the 30 s idle disconnect this project
        already fixed once was free to come back on it.

        Corrects both directions, because a missed disconnect is the same bug
        wearing the other hat, and read-then-write so a settled system does
        nothing.
        """
        if self._mgmt is None:
            return

        targets = [a for a in adapters if a.index >= 0]
        if not targets:
            return

        def _read() -> dict[str, str]:
            found: dict[str, str] = {}
            for adapter in targets:
                try:
                    peers = self._mgmt.connections(adapter.index)
                except Exception:
                    log.debug(
                        "Could not read connections on %s",
                        adapter.hci_name, exc_info=True,
                    )
                    continue
                # "" is a real answer here -- nothing is connected -- and it is
                # the half that clears a stale peer. An adapter whose read
                # *failed* is simply absent, so it is left alone.
                found[adapter.bd_addr] = peers[0] if peers else ""
            return found

        observed = await asyncio.get_running_loop().run_in_executor(None, _read)

        for adapter in targets:
            if adapter.bd_addr not in observed:
                continue
            peer = observed[adapter.bd_addr]
            # Both halves, not just the peer. Phase and peer can disagree --
            # bring-up sets LISTENING on an adapter already carrying a link --
            # and a peer-only comparison calls that settled and walks away.
            linked = adapter.phase is Phase.LINKED
            if peer == adapter.peer and bool(peer) == linked:
                continue

            log.info(
                "%s: link state was %s, radio says %s. Correcting -- a "
                "connection that predates our MGMT subscription raises no "
                "event, so this is the only thing that would notice it.",
                adapter.hci_name,
                f"connected to {adapter.peer}" if adapter.peer else "idle",
                f"connected to {peer}" if peer else "idle",
            )
            self._note_link(adapter.index, peer or adapter.peer, bool(peer))

    def _check_orphan_bonds(self, adapters: list[AdapterState]) -> None:
        """Notice an adapter whose console has forgotten it.

        A bond has two halves. When the **peer** loses its half there is no
        error to find: it does not reject us, it simply never pages us, which
        looks exactly like being out of range or switched off.
        ``_note_auth_failure`` cannot help -- it keys on connection attempts,
        and the whole symptom is that there are none.

        The signal we do have is comparative. If a host is connected to one of
        our adapters while another adapter holds a bond for that same host and
        it has never come near it, the second bond is one-sided. Measured on
        the reference Pi after a console evicted a controller to make room for
        a fourth: 0 connection attempts in 75 s on a perfectly healthy radio.

        Reported, never acted on. Deleting the orphan is the right repair and
        it is also unrecoverable if this inference is wrong, so it stays the
        operator's decision -- the same reasoning that makes "Forget pairing"
        refuse by default on this transport.

        Run from the reconcile rather than from ``snapshot``: reading the bond
        directory is filesystem I/O and the GUI snapshot runs at 10 Hz.
        """
        from common.timing import now_ns

        now = now_ns()
        # Refresh the bond list from **disk** first, and for everyone.
        #
        # ``adapter.bonds`` is otherwise filled from D-Bus by
        # _reconnect_target_for, and org.bluez has been observed reporting an
        # empty list for an adapter whose key file exists and which a console
        # was actively resuming against. The operator's buttons now depend on
        # this -- an adapter wrongly reported unpaired offers Pair where it
        # should offer Wake -- so it has to come from the source that cannot
        # answer "none" by mistake.
        for adapter in adapters:
            try:
                adapter.bonds = tuple(_bonds_on_disk(adapter.bd_addr))
            except Exception:
                log.debug(
                    "Could not read bonds for %s", adapter.bd_addr, exc_info=True
                )

        live = {a.peer for a in adapters if a.peer}
        if not live:
            for adapter in adapters:
                adapter.orphan_peer = ""
                self._orphan_since.pop(adapter.bd_addr, None)
            return

        for adapter in adapters:
            if adapter.peer or not adapter.enabled or adapter.hid_error:
                adapter.orphan_peer = ""
                self._orphan_since.pop(adapter.bd_addr, None)
                continue

            held = set(adapter.bonds)
            absent = sorted(held & live)
            if not absent:
                adapter.orphan_peer = ""
                self._orphan_since.pop(adapter.bd_addr, None)
                continue

            # **It has to persist.** A console drops and re-establishes links
            # constantly, so at any instant some adapter is briefly idle while
            # a sibling is connected -- which is this condition exactly.
            # Firing on the instantaneous reading called a healthy, connected
            # adapter orphaned within seconds of it reconnecting. Measured:
            # hci2 was flagged 36 s after its own link came up, and was
            # carrying that link at the time.
            first = self._orphan_since.setdefault(adapter.bd_addr, now)
            if now - first < _ORPHAN_CONFIRM_NS:
                continue

            was, adapter.orphan_peer = adapter.orphan_peer, absent[0]
            if adapter.orphan_peer and adapter.orphan_peer != was:
                log.warning(
                    "%s (%s) is bonded to %s, which is connected to another "
                    "adapter and has never tried to reach this one. The "
                    "console has almost certainly forgotten this controller "
                    "-- put it into pairing mode and press Pair. "
                    "Nothing errors here because the console never contacts "
                    "us at all.",
                    adapter.hci_name, adapter.display_name or adapter.bd_addr,
                    adapter.orphan_peer,
                )

    async def _readvertise(self, adapter: AdapterState) -> bool:
        """Restart one adapter's advertisement."""
        peripheral = self._ble.get(adapter.bd_addr)
        if peripheral is None:
            # Not published on this transport; nothing to restart, and nothing
            # is wrong. The Classic path has its own window.
            return True

        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: peripheral.ensure_advertising(force=True)
        )

    async def reset_all(self) -> tuple[bool, str]:
        """Unpair every controller and leave them switched off.

        The bulk form of Sleep-and-forget, and it exists because the useful
        unit of recovery is usually **all of them**: a console that has lost
        track of which controllers it knows leaves several adapters holding
        halves of bonds it no longer has, and clearing those one card at a
        time is slow and easy to do incompletely.

        **It does not put anything into pairing mode.** That is the point of
        separating it from Pair. Four controllers all soliciting a pairing at
        once is the lottery this subsystem already paid for -- the console
        takes whichever it sees first, and the operator cannot say which one
        they meant. After a reset every controller is unpaired and idle, and
        each is introduced to the console deliberately, one at a time.

        Destructive by design, and the caller is expected to have confirmed
        it. Disabled adapters are skipped: they are out of service, nothing
        advertises for them, and silently unpairing one would be a surprise
        the next time it came back.
        """
        cleared: list[str] = []
        failed: list[str] = []

        for adapter in sorted(
            self._adapters.values(), key=lambda a: a.hci_name
        ):
            if not adapter.enabled:
                continue

            label = adapter.display_name or adapter.hci_name
            had_bond = bool(_bonds_on_disk(adapter.bd_addr))
            try:
                # Drops the link, detaches the sink and stops advertising --
                # all three, which is exactly what "reset" should leave behind.
                # `forget` only where there is something to forget, so an
                # already-unpaired adapter is not reported as cleared.
                await self.disconnect_host(
                    adapter.bd_addr, forget=had_bond, confirm_orphan=True
                )
            except Exception as exc:
                log.warning("Could not reset %s: %s", adapter.hci_name, exc)
                failed.append(label)
                continue

            if had_bond:
                if _bonds_on_disk(adapter.bd_addr):
                    failed.append(label)
                else:
                    cleared.append(label)

        if self.on_change:
            self.on_change()

        if failed:
            return False, (
                f"Reset {len(cleared)} controller(s), but {', '.join(failed)} "
                "would not clear. Check the log."
            )

        tail = (
            " All are switched off -- press Pair on one to introduce it to "
            "the console."
        )
        if not cleared:
            return True, "Nothing was paired." + tail
        return True, (
            f"Unpaired {len(cleared)} controller(s): {', '.join(cleared)}."
            + tail
        )

    async def wake(self, bd_addr: str) -> tuple[bool, str]:
        """Switch a paired controller back on. The counterpart of Sleep.

        A real pad that has been switched off comes back by advertising again;
        its host sees it and reconnects, using the bond both ends already
        hold. That is exactly what this does, and it is why **it must not
        touch the bond** -- waking is not re-pairing, and a controller that
        forgot its console every time it woke would be useless.

        Also re-asserts Pairable, because that is the one property a woken
        adapter can have lost while it was asleep and it fails silently: the
        host connects and drops immediately, with nothing logged.
        """
        adapter = self._adapters.get(bd_addr)
        if adapter is None:
            return False, f"No adapter {bd_addr}"
        if not adapter.enabled:
            return False, f"{adapter.hci_name} is disabled; switch it on first"
        if adapter.hid_error:
            return False, f"HID is not running on this adapter: {adapter.hid_error}"

        bonded = bool(_bonds_on_disk(bd_addr))

        # Which question the advertisement asks. A bonded controller waking up
        # wants its own console back, and saying "limited discoverable" there
        # means "I want to be paired" -- which this console answers only while
        # it is itself in pairing mode. Measured: 0 connection attempts in 45 s
        # after a clean pair, sleep and wake, with the advertisement verified
        # live on the radio.
        peripheral = self._ble.get(bd_addr)
        if peripheral is not None:
            peripheral.set_pairing_mode(not bonded)

        if not await self._readvertise(adapter):
            return False, (
                f"{adapter.hci_name} would not start advertising, so no host "
                "can find it"
            )

        await adapter_dbus.set_properties(
            adapter.hci_name, connectable=True, pairable=True, pairable_timeout_s=0
        )

        if self.on_change:
            self.on_change()

        return True, (
            f"{adapter.hci_name} is awake and advertising."
            + (
                " It is paired, so the console should reconnect within a few "
                "seconds."
                if bonded else
                " It is not paired with anything yet -- press Pair to bond it."
            )
        )

    def _on_host_connected(self, bd_addr: str, host: str) -> None:
        """A console attached. Runs on the HID server's own thread.

        Only bookkeeping: the pairing window has done its job, and the adapter
        is carrying a link. Nothing here may block or touch D-Bus -- this is
        the accept thread, and the console is waiting on the handshake.
        """
        adapter = self._adapters.get(bd_addr)
        if adapter is None:
            return
        adapter.pairing_until_ns = 0
        adapter.peer = host
        adapter.to(Phase.LINKED, reason=f"{host} connected")
        if self.on_change:
            self.on_change()

    def _on_host_gone(self, bd_addr: str, host: str) -> None:
        """A console detached, however it went.

        Without this the only signal was the sink reporting itself
        disconnected, which nothing watched -- so the web GUI showed an adapter
        as connected long after the console had been switched off.
        """
        adapter = self._adapters.get(bd_addr)
        if adapter is None:
            return
        adapter.peer = ""
        if adapter.phase is Phase.LINKED:
            adapter.to(Phase.LISTENING, reason=f"{host} disconnected")
        if self.on_change:
            self.on_change()

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

    def _forget_reconnect_target(self, adapter: AdapterInfo) -> None:
        """Stop chasing the host we were paired to, on entering pairing mode.

        Entering pairing mode removes every bond on this adapter, but the
        address was kept in both the config and the live HID server -- so the
        reconnect loop went on paging a host that could no longer authenticate
        us, every 30 s, forever, logged only at debug. Nothing else in the
        server ever cleared ``paired_target``.

        Called whenever bonds are cleared, whether or not any were found. A
        bond that had *already* gone leaves exactly the same stale address
        behind, and that is the case that strands an adapter: no key, no
        chance of connecting, and no way for the operator to stop it.
        """
        saved = self._config.adapter(adapter.bd_addr)
        target = saved.paired_target if saved else ""
        if not target:
            return

        from server.config import AdapterConfig

        self._config.upsert_adapter(
            AdapterConfig(
                bd_addr=adapter.bd_addr,
                enabled=saved.enabled if saved else True,
                profile=saved.profile if saved else self._default_profile,
                paired_target="",
                label=saved.label if saved else "",
            )
        )

        server = self._hid_servers.get(adapter.bd_addr)
        if server is not None:
            server.set_reconnect_target(None)

        log.info(
            "Forgot host %s on %s; no longer trying to reconnect to it",
            target, adapter.bd_addr,
        )

        if self._config_path is not None:
            try:
                from server import config as server_config

                server_config.save(self._config, self._config_path)
            except Exception:
                log.debug("Could not persist cleared reconnect target", exc_info=True)

    async def _stop_hid(self, bd_addr: str) -> None:
        """Tear down one adapter's HID stack.

        The SDP record is shared, so it is released only when the last adapter
        goes away -- dropping it while another adapter is still serving would
        make that adapter undiscoverable.
        """
        peripheral = self._ble.pop(bd_addr, None)
        if peripheral is not None:
            await peripheral.stop()

        server = self._hid_servers.pop(bd_addr, None)
        if server is not None:
            await asyncio.to_thread(server.stop)

        # The HCI channel belongs to this adapter, so it goes with it. Left
        # open it would hold a socket against a dongle that may have been
        # unplugged, and its reads would fail forever.
        tuner = self._tuners.pop(bd_addr, None)
        if tuner is not None:
            tuner.close()

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

    async def _quiet_adapter(self, bd_addr: str) -> None:
        """Stop an adapter we configured from advertising itself.

        Only the *scan* state is reverted, not the alias, class or MGMT
        settings. Those are inert on a radio nothing can reach, and rewriting
        them fights bluetoothd for no gain -- the class in particular is
        recomputed by bluetoothd via MGMT and would silently come back.

        Scoped to adapters this process configured. One we never enabled is
        left completely alone, which is the promise the operator relies on when
        the Pi uses a dongle for something else.
        """
        if bd_addr not in self._configured:
            return

        adapter = self._adapters.get(bd_addr)
        if adapter is None:
            return

        # `connectable=False` is the one that actually silences the radio.
        # Pairable and Discoverable alone left the controller still answering
        # inquiries whenever BlueZ believed it was already undiscoverable --
        # the state hci4 was found in, where every management layer said "off"
        # and the radio went on advertising to hosts that could never connect.
        # Clearing Connectable makes bluetoothd write scan enable 0x00, killing
        # inquiry *and* page scan in one go, and it goes through the owner of
        # that state rather than a second MGMT client.
        ok = await adapter_dbus.set_properties(
            adapter.hci_name, connectable=False, pairable=False, discoverable=False
        )
        adapter.pairing_until_ns = 0
        adapter.peer = ""
        adapter.to(Phase.QUIET, reason="disabled by the operator")
        self._configured.discard(bd_addr)
        self._quieted.add(bd_addr)

        if ok:
            log.info(
                "Adapter %s (%s) is no longer advertising",
                adapter.hci_name, bd_addr,
            )
        else:
            # Worth a warning: the visible symptom is a host offering a
            # controller that cannot connect, which reads as a pairing bug.
            log.warning(
                "Could not stop %s (%s) advertising; it may still appear to hosts",
                adapter.hci_name, bd_addr,
            )

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
        # Write it out. Without this the choice lived only in memory: the
        # operator enabled an adapter, watched it come up, and found it
        # disabled again after the next restart with nothing to explain why.
        self._persist()

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

    async def set_profile_all(self, profile_name: str) -> tuple[bool, str]:
        """Change what every adapter emulates.

        Server-wide, for the same reason the identity is: BlueZ publishes one
        HID service record for the machine, so the report descriptor a console
        is told to expect is shared. Setting this per adapter -- which the web
        GUI used to offer -- let an adapter send reports in a format nothing had
        advertised, and the mismatch was only ever a log line.
        """
        channels = list(self._router.channels())
        if not channels:
            return False, "No adapters are enabled"

        applied = 0
        last_message = ""
        for channel in channels:
            ok, message = await self.set_profile(channel.bd_addr, profile_name)
            if ok:
                applied += 1
            last_message = message

        if not applied:
            return False, last_message or "Could not change the profile"

        # So an adapter plugged in later comes up matching, rather than
        # reverting to whatever the command line said at startup.
        self._default_profile = profile_name
        self._config.controller_profile = profile_name
        return True, (
            f"All {applied} adapter(s) now emulate {profile_name}. "
            "Restart the server to publish the matching descriptor, then re-pair."
        )

    async def set_identity(self, key: str) -> tuple[bool, str]:
        """Change what every adapter claims to be.

        Server-wide by necessity, not by preference: BlueZ keeps one SDP
        database for the whole machine, so there is a single DeviceID record
        however many dongles are plugged in. The advertised *name* is per
        adapter (each appends its number), so only the vendor half is shared.

        Takes effect on the next bring-up, and needs a re-pair: a console
        remembers the controller it bonded with by name and vendor, so changing
        either makes us a different device as far as it is concerned.
        """
        from server.bt.identities import get_identity

        identity = get_identity(key)
        if identity.key != key:
            return False, f"Unknown controller identity '{key}'"

        if getattr(self._config, "controller_identity", "") == identity.key:
            return True, f"Already presenting as {identity.display_name}"

        self._config.controller_identity = identity.key

        # Rename the live adapters now, so the change is visible without a
        # restart. The SDP record cannot be re-registered in place -- BlueZ
        # allows one per UUID and it is already held -- so the vendor half
        # follows on the next start.
        renamed = 0
        for bd_addr in list(self._adapters):
            try:
                if await self._apply_name(bd_addr):
                    renamed += 1
            except Exception:
                log.debug("Could not rename %s", bd_addr, exc_info=True)

        if self.on_change:
            self.on_change()

        return True, (
            f"Now presenting as {identity.display_name}"
            f" ({identity.vendor_id:04X}:{identity.product_id:04X})."
            f" Renamed {renamed} adapter(s); restart the server to publish the"
            f" new vendor id, then re-pair."
        )

    async def _apply_name(self, bd_addr: str) -> bool:
        """Push the current name for one adapter to BlueZ. True if it changed."""
        adapter = self._adapters.get(bd_addr)
        if adapter is None or not adapter.enabled:
            return False
        return bool(
            await adapter_dbus.set_properties(
                adapter.hci_name, alias=self.adapter_name(bd_addr)
            )
        )

    async def disconnect_host(
        self, bd_addr: str, *, forget: bool = False, confirm_orphan: bool = False
    ) -> tuple[bool, str]:
        """Drop the console currently attached to this adapter.

        ``forget`` also removes the bond, and defaults **off**. It used to
        default on, for a reason that seemed good and was wrong: a bonded
        console is the central on BLE and reconnects within seconds, so a
        disconnect that left the bond looked like a button doing nothing.

        The trade that reasoning missed is that **forgetting is not
        symmetrical**. A PC can be told to forget a device; a console often
        cannot. Removing only our half leaves the host asking us to resume
        encryption with a key we no longer hold -- measured on hardware:

            LE Long Term Key Request
            LE Long Term Key Request Neg Reply
            Disconnect (remote terminated), 14 ms later

        No SMP at all, because the host never reaches the point of pairing. It
        has no recovery path, and neither do we: the key is gone. That is a far
        worse failure than a button that appears not to work, so forgetting is
        now something the operator asks for explicitly and is told the cost of.
        """
        adapter = self._adapters.get(bd_addr)
        if adapter is None:
            return False, f"No adapter {bd_addr}"

        peer = adapter.peer
        dropped = 0

        # Stop our own outgoing reconnect first, or the Classic path pages the
        # host straight back and undoes this before the operator sees it.
        server = self._hid_servers.get(bd_addr)
        if server is not None:
            server.suspend_reconnect(_DISCONNECT_HOLDOFF_S)

        for path in await adapter_dbus.connected_devices(adapter.hci_name):
            if await adapter_dbus.disconnect_device(path):
                dropped += 1

        # The BLE sink tracks subscription rather than connection, so it has to
        # be told explicitly; nothing else will.
        peripheral = self._ble.get(bd_addr)
        if peripheral is not None:
            peripheral.sink.detach()

            # **And stop advertising, or the console is back within seconds.**
            #
            # We are the peripheral. The console is the central, it holds the
            # bond, and it reconnects to a bonded controller as soon as it sees
            # it advertise -- so dropping the link alone is a button that
            # visibly does nothing, which is what the operator reported for
            # Controllers 3 and 4.
            #
            # The old answer to this was to forget the bond, and it is much
            # worse: a console generally cannot be told to forget, so removing
            # only our half strands it (see the note above). Taking the
            # advertisement down is reversible, costs nothing, and is the same
            # thing switching a real controller off does.
            #
            # Latched inside the peripheral so _ensure_ble_ready can tell
            # "lost it" from "the operator turned it off" -- without that the
            # invariant would put it straight back on the next reconcile.
            await asyncio.get_running_loop().run_in_executor(
                None, peripheral.suppress_advertising
            )

        if server is not None:
            server.set_reconnect_target(None)

        cleared: list[str] = []
        if forget:
            # **Forgetting is only safe if the peer will forget too.**
            #
            # A bond has two halves and neither end recovers when only one
            # survives: the peer asks us to resume with a key we no longer
            # hold, we answer negative, it disconnects, and it retries several
            # times a second forever. Measured on hardware, 18 to 30 cycles per
            # capture, with nothing in any log to explain it.
            #
            # A PC can be told to forget a device, so the operator can restore
            # symmetry. A console generally cannot -- the one this was measured
            # against offers no way at all -- so removing our half there is a
            # one-way trip that strands it. That is why this refuses rather
            # than warns: the tooltip warned, and it still happened repeatedly.
            #
            # ``confirm_orphan`` is the escape hatch for the case that IS safe:
            # the peer has already lost its half (it refused to encrypt), so our
            # key is a proven orphan and deleting it is the repair rather than
            # the damage. That is also what _repair_one_sided_bond does
            # automatically, so reaching for this by hand should be rare.
            transport = str(getattr(self._config, "controller_transport", "classic"))
            held = _bonds_on_disk(bd_addr)
            if transport == "ble" and held and not confirm_orphan:
                return False, (
                    f"Refusing to forget {', '.join(held)} on {adapter.hci_name}: "
                    "over BLE the console keeps its half of the bond and will "
                    "ask us to resume with a key we would no longer have, which "
                    "leaves it reconnecting and failing forever with no way back. "
                    "Clear the controller on the console first, or use the "
                    "override if the console has already lost its half."
                )

            cleared = await adapter_dbus.remove_bonds(adapter.hci_name)
            self._forget_reconnect_target(adapter)

        adapter.peer = ""
        adapter.bonds = ()
        if adapter.phase is Phase.LINKED:
            adapter.to(Phase.LISTENING, reason="operator disconnected the console")

        if self.on_change:
            self.on_change()

        if not dropped and not cleared:
            return False, f"Nothing was connected to {adapter.hci_name}"

        who = peer or (cleared[0] if cleared else "the console")
        if cleared:
            return True, (
                f"Disconnected {who} from {adapter.hci_name} and forgot the "
                "pairing. Both ends must now pair again -- tell the console to "
                "forget this controller too, or it will keep trying to resume "
                "with a key neither side has."
            )
        if peripheral is not None:
            return True, (
                f"Disconnected {who} from {adapter.hci_name} and stopped "
                "advertising, so it will not reconnect. The pairing is kept -- "
                "press Re-advertise to let it back."
            )
        return True, (
            f"Disconnected {who} from {adapter.hci_name}. The pairing is kept, "
            "so a host that wants to may reconnect on its own."
        )

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
        if pairable and not adapter.enabled:
            # Turning it *off* stays allowed whatever the state. Refusing that
            # too meant an adapter armed for pairing and then disabled had no
            # way back: the only code path that clears Discoverable was locked
            # out, and it advertised until something else happened to reset it.
            return False, f"Adapter {bd_addr} is disabled"
        if pairable and adapter.hid_error:
            # Refuse rather than advertise a controller that cannot complete a
            # connection. A host would pair and then fail the interrupt channel,
            # reporting only "try again" -- with the real reason sitting in a
            # log line written days earlier.
            return False, f"HID is not running on this adapter: {adapter.hid_error}"

        name = self.adapter_name(bd_addr)

        if pairable:
            await self._ensure_agent()

        # Record the window so the GUI can show a countdown. Cleared when the
        # operator stops it, and read as expired once the deadline passes.
        if pairable:
            adapter.arm_pairing(duration_s)
        else:
            adapter.clear_pairing(reason="operator stopped pairing")

        # Keep our own outgoing pages off this radio while it is meant to be
        # listening for the console. Both use the same antenna, and _connect
        # binds to this adapter deliberately, so the contention lands exactly
        # where it hurts.
        server = self._hid_servers.get(bd_addr)
        if server is not None:
            server.suspend_reconnect(duration_s if pairable else 0)

        # **Pair means pair afresh, on both transports.**
        #
        # If the host forgot us it generates a new key while we keep the old
        # one, and authentication then fails with no useful diagnostic on
        # either side. Clearing on "start fresh" is what a real controller's
        # pair button does, and it is the only way out of that state.
        #
        # This was disabled for BLE for a while, and the reasoning was sound
        # as far as it went: removing our half while the peer keeps its own
        # leaves the peer demanding an LTK we no longer hold -- measured, 54
        # Long Term Key Requests and 54 negative replies in 18 seconds. What
        # that reasoning missed is that **the peer here cannot be told to
        # forget either**. This console offers no way to remove a controller,
        # so re-pairing is the only recovery available, and refusing to clear
        # our half simply blocked it: two adapters sat unpairable for a whole
        # session until the bond was deleted by hand.
        #
        # So the danger is real and the alternative is worse. The operator
        # presses Pair to mean "start again", and both halves of that are now
        # actually done. Sleep and Wake never touch a bond.
        ble = self._transport() == "ble"

        cleared: list[str] = []
        if pairable and forget_bonds:
            # Scoped to this adapter: clearing machine-wide would disconnect
            # consoles happily playing on the other three.
            cleared = await adapter_dbus.remove_bonds(adapter.hci_name)
            # Unconditionally, not just when a bond was removed. After this
            # call the adapter has *no* bonds at all, so any remembered host is
            # stale by definition -- and the case that matters most is exactly
            # the one where nothing was removed: the bond had already gone
            # while the address stayed behind, leaving us paging a host we
            # could never authenticate to.
            self._forget_reconnect_target(adapter)

        # Re-assert both on every pairing window: these are adapter state and
        # anything on the system can have flipped them since startup.
        await asyncio.to_thread(_ensure_pairing_settings, adapter)
        await asyncio.to_thread(_set_device_class, adapter)

        # On BLE, Pairable is an **invariant**, not a window.
        #
        # The advertisement is itself the invitation to bond, so a peripheral
        # that is advertising-but-unbondable accepts a connection and can then
        # do nothing with it -- measured here as 21 connect-and-drop cycles in
        # three minutes. _expire_pairing_windows was fixed for this and *this*
        # path was missed, so pressing Stop cost an adapter its console until
        # the server restarted, silently.
        #
        # Arming therefore re-asserts it rather than merely leaving it alone,
        # which is what makes "Connection mode" a repair for an adapter that
        # somehow lost it. None while disabled: _quiet_adapter owns that case.
        want_pairable = (True if adapter.enabled else None) if ble else pairable

        ok = await adapter_dbus.set_properties(
            adapter.hci_name,
            alias=name,
            # Asserted **both** when arming and when stopping, as long as the
            # adapter is still enabled.
            #
            # Leaving it alone on stop was not enough, and the reason is worth
            # keeping: we never cleared Connectable, but *BlueZ does it for
            # us*. It only keeps an adapter connectable on its own while it has
            # a bond that might reconnect, so ending a window on an adapter
            # that did not manage to bond drops page scan to 0x00 -- and the
            # adapter is then unreachable, which is the trap that cannot open
            # itself: it cannot accept a connection, so it can never gain the
            # bond that would have kept it connectable.
            #
            # Measured live: stop pairing on hci3 with no bonds, and the radio
            # goes to scan enable 0x00 with nothing in any log to say so.
            # Only _quiet_adapter, for an adapter the operator disabled,
            # deliberately clears it.
            connectable=True if adapter.enabled else None,
            pairable=want_pairable,
            discoverable=pairable,
            timeout_s=duration_s if pairable else None,
            # Never let the *pairing* half expire on the BLE transport. The
            # advertisement is continuous and is itself the invitation to bond,
            # so an expiring Pairable silently turns the peripheral into one
            # that accepts connections and can do nothing with them.
            pairable_timeout_s=0 if ble else None,
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

            note = f" Cleared {len(cleared)} previous pairing(s)." if cleared else ""

            if ble:
                peripheral = self._ble.get(bd_addr)
                if peripheral is not None:
                    # Say so on the air. This is the one time limited
                    # discoverable is the right answer.
                    peripheral.set_pairing_mode(True)

                # **This adapter only.** An earlier version also took the other
                # three off the air, on the reasoning that identical
                # advertisements make the console's choice a race. It did stop
                # the race, and it caused something worse: every click removed
                # and re-added the advertising instance on three other
                # adapters, so an operator pressing several buttons -- the
                # natural thing to do when nothing is connecting -- thrashed
                # every advertisement on the machine and cut off whatever
                # connection attempt the console had in flight. Measured: seven
                # windows opened in 34 s, each one restarting three
                # advertisements.
                #
                # Pairing one adapter in isolation is still available and is
                # now explicit: turn the others off with the enable toggle.
                # That is an operator decision with no hidden cross-adapter
                # effects, which is the property this needed and did not have.
                #
                # Restart the advertisement, which is what pressing pair on a
                # real pad does. A BLE peripheral is always discoverable and
                # always bondable, so there is no window to open -- but the
                # advertising instance is the one thing a console genuinely
                # cannot see through, and it does not survive a controller
                # power cycle. Giving the operator a way to put it back is the
                # only useful meaning this button has on this transport.
                restarted = await self._readvertise(adapter)
                if not restarted:
                    return False, (
                        f"{adapter.hci_name} is bondable but its advertisement "
                        "could not be restarted; a console will not see it"
                    )
                # Two different situations, and telling the operator to do
                # the wrong one wastes a pairing window: a bonded console
                # reconnects on its own within seconds, an unbonded one needs
                # its pairing button.
                bonded = bool(_bonds_on_disk(bd_addr))
                return True, (
                    f"{adapter.hci_name} is advertising as '{name}' and is "
                    f"bondable.{note} "
                    + (
                        "It is already paired, so the console should reconnect "
                        "on its own within a few seconds."
                        if bonded else
                        "Put the console into pairing mode now."
                    )
                )

            return True, (
                f"{adapter.hci_name} is discoverable as '{name}' for {duration_s}s.{note} "
                "Put the console into pairing mode now."
            )
        if ble:
            # Nothing was taken away, so do not claim otherwise. Discoverable
            # is meaningless here; the advertisement carries on regardless.
            return True, f"{adapter.hci_name} is still advertising and bondable"
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

        Numbered so several adapters are tellable apart in a host's Bluetooth
        list -- they would otherwise all appear as the same string, and a host
        that deduplicates by name shows something like "controller-server #2"
        instead.

        **Unless the identity is impersonating a named product**, in which case
        the number is exactly the thing standing between us and a console. An
        Analogue 3D paired happily with the adapter advertising ``8BitDo 64 BT``
        and would not connect at all to the one beside it advertising
        ``8BitDo 64 BT 1`` -- silently, which is how this class of fault always
        presents. See ``ControllerIdentity.exact_name``.

        An operator-set label wins -- **except under an exact-name identity**,
        where it is the same footgun the adapter number was, arriving by a
        different route. Measured on the reference Pi: hci2 carried the label
        "RBGC spare 1" from earlier debugging, so it advertised that, and the
        console never paged it. Nothing said why; the adapter looked healthy
        from every angle.

        The label's real job -- telling four adapters apart for the operator --
        is now :meth:`adapter_display_name`, which still honours it. So under
        an impersonating identity the label costs a working controller and buys
        nothing, and the on-air name stays exact.
        """
        saved = self._config.adapter(bd_addr)
        label = saved.label if saved is not None else ""
        base = self._base_device_name()

        if self._identity().exact_name:
            if label and label != base:
                self._warn_label_ignored(bd_addr, label, base)
            return base

        if label:
            return label

        number = saved.number if saved is not None else 0
        return f"{base} {number}" if number else base

    #: Adapters already warned about an ignored label. Latched: this is read on
    #: every reconcile and every status snapshot, and the remedy is one edit.
    _label_warned: set[str] = set()

    def _warn_label_ignored(self, bd_addr: str, label: str, base: str) -> None:
        if bd_addr in self._label_warned:
            return
        self._label_warned.add(bd_addr)
        log.warning(
            "Ignoring the label %r on %s for the advertised name: the %r "
            "identity is impersonating a named product, and a console matching "
            "it wants %r character for character -- an adapter advertising "
            "anything else is simply never paged, with nothing to say so. The "
            "label still names this adapter in the web GUI.",
            label, bd_addr, self._identity().key, base,
        )

    def _identity(self):
        """The configured controller identity, or the generic one."""
        from server.bt.identities import get_identity

        return get_identity(getattr(self._config, "controller_identity", ""))

    def _base_device_name(self) -> str:
        """Advertised name before the per-adapter number is appended.

        This is the half of the identity a console can see *during inquiry*,
        before any connection exists -- so for a console that scans and connects
        to whatever it recognises, rather than offering a list, this is the
        first thing that has to match.
        """
        return self._identity().device_name

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
        """Adapters as the web GUI sees them, in player order.

        Number and name are filled in here rather than stored on AdapterInfo,
        because both live in config keyed by BD_ADDR and an adapter can be
        re-enumerated (hciX reshuffles) without either changing.

        Sorted by number so the GUI shows Gamepad 1..4 left to right. Unnumbered
        adapters (disabled, never brought up) sort last rather than to the front.
        """
        rows = []
        for adapter in self._adapters.values():
            saved = self._config.adapter(adapter.bd_addr)
            adapter.number = saved.number if saved else 0
            adapter.name = self.adapter_name(adapter.bd_addr)
            adapter.display_name = self.adapter_display_name(adapter.bd_addr)
            peripheral = self._ble.get(adapter.bd_addr)
            adapter.advertising = (
                not peripheral.suppressed if peripheral is not None else True
            )
            rows.append(adapter.snapshot())

        rows.sort(key=lambda row: (row["number"] or 99, row["hci"]))
        return rows

    def adapter_display_name(self, bd_addr: str) -> str:
        """What the **operator** calls this adapter: "Controller 1".

        Deliberately not :meth:`adapter_name`, which is what goes on the air.
        An identity impersonating a named product sends the same string from
        every adapter -- it has to, since a console matching on the name wants
        it character for character -- so using the advertised name here left
        all four cards and all four assignment dropdowns reading
        "8BitDo 64 BT", with nothing to tell the operator which card was which
        player.

        Numbering is the persisted per-BD_ADDR number, so "Controller 2" stays
        with the same physical dongle across reboots and hciX reshuffles, and
        ``snapshot`` sorts on it -- which is what makes the cards read 1..4
        left to right.
        """
        saved = self._config.adapter(bd_addr)
        if saved is not None and saved.label:
            # An explicit operator choice beats a generated one, exactly as it
            # does for the advertised name.
            return saved.label

        number = saved.number if saved is not None else 0
        if number:
            return f"Controller {number}"

        adapter = self._adapters.get(bd_addr)
        return adapter.hci_name if adapter is not None else bd_addr

    # -- hot-plug ----------------------------------------------------------

    #: How long to let MGMT events settle before reconciling. A single
    #: operator action produces a burst -- setting one adapter discoverable
    #: emits several New Settings events -- and reconciling on each one would
    #: run the whole D-Bus and HID pass several times for one change.
    _MGMT_DEBOUNCE_S = 0.25

    #: The reconcile that runs even when nothing was reported. MGMT is
    #: reliable, but a missed event would otherwise leave the server wrong
    #: until the next operator action, and this is cheap.
    _RECONCILE_INTERVAL_S = 10.0

    async def _watch_via_mgmt(self) -> None:
        """React to adapter changes as they happen, not on a timer.

        The 10 s reconcile survives underneath as a safety net rather than as
        the mechanism: losing a dongle used to take up to ten seconds to
        notice, during which a player's controller was routed to a radio that
        no longer existed.
        """
        assert self._mgmt_wake is not None

        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._mgmt_wake.wait(), timeout=self._RECONCILE_INTERVAL_S
                )
            except asyncio.TimeoutError:
                await self.rescan()
                continue

            self._mgmt_wake.clear()
            if self._stop.is_set():
                return

            # Let the burst finish before doing the expensive part.
            await asyncio.sleep(self._MGMT_DEBOUNCE_S)
            self._mgmt_wake.clear()
            await self.rescan()

    async def _watch_hotplug(self) -> None:
        """Watch for dongles appearing and disappearing.

        Prefers pyudev for immediate notification; falls back to polling so the
        feature still works without it. Either way a periodic reconcile runs,
        because udev events can be missed when the daemon restarts.
        """
        if self._mgmt is not None and self._mgmt_wake is not None:
            await self._watch_via_mgmt()
            return

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
    """Run a helper binary and return ``(returncode, stdout + stderr)``.

    ``input=""`` is load-bearing, not tidiness. It hands the child an empty
    pipe that closes immediately; without it the child inherits *our* stdin,
    which under systemd is ``/dev/null`` (``StandardInput=null`` is the
    default), and **``btmgmt`` hangs forever on ``/dev/null``**. It is built on
    BlueZ's ``bt_shell``, which watches stdin even for a one-shot command;
    ``/dev/null`` is permanently read-ready and never delivers the EOF event
    that would make it quit, so it waits for input that cannot arrive.

    That single detail made *every* ``btmgmt`` call from the service time out --
    silently, because the failure only ever reached ``log.debug`` here. It is
    invisible from a shell, where stdin is a terminal or a socket and the same
    command returns in under a millisecond. Measured on the Pi:

        stdin=DEVNULL      TIMEOUT after 5.00s
        stdin=<pipe, EOF>  rc=0 in 0.00s

    An empty pipe is also strictly safer for the rest: none of these tools want
    stdin, so any that unexpectedly reads gets EOF instead of blocking us for
    the full timeout.
    """
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False, input=""
        )
        return result.returncode, result.stdout + result.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.debug("Command %s failed: %s", cmd[0], exc)
        return 1, str(exc)


def _index_of(hci_name: str) -> int:
    """``hci3`` -> ``3``. -1 when there is no index to be had.

    AdapterState derives ``hci_name`` from ``index``, so an observation that
    carries only the name silently becomes ``hci-1`` and every helper that
    shells out to hciconfig then fails against a device that does not exist.
    """
    suffix = hci_name.removeprefix("hci")
    return int(suffix) if suffix.isdigit() else -1


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

        hci_name = header.group(1)
        adapters.append(
            AdapterInfo(
                bd_addr=addr_match.group(1).upper(),
                index=_index_of(hci_name),
                hci_name=hci_name,
                manufacturer=manufacturer,
                powered="UP RUNNING" in block,
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
                    # bluetoothctl reports no index at all, so there is nothing
                    # to put here. Everything that needs one -- HCI sockets,
                    # link tuning, hciconfig -- checks for this and skips.
                    index=-1,
                    hci_name=parts[1],
                    manufacturer=" ".join(parts[2:]) if len(parts) > 2 else "",
                    powered=True,
                )
            )
    return adapters


def _bring_up_adapter(adapter: AdapterInfo) -> bool:
    """Bring an adapter up and set its class of device to 'gamepad'."""
    if shutil.which("hciconfig") is None:
        adapter.powered = True
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

    adapter.powered = True
    log.info("Adapter %s (%s) is up", adapter.bd_addr, adapter.hci_name)
    return True


#: MGMT settings that decide whether a host can use Secure Simple Pairing.
#: These must appear in *current settings*...
_REQUIRED_MGMT_SETTINGS = frozenset({"ssp"})
#: ...and this must not.
_FORBIDDEN_MGMT_SETTINGS = frozenset({"link-security"})

# `bondable` deliberately is *not* required, though it is genuinely needed to
# create a bond. It is the MGMT face of `org.bluez.Adapter1.Pairable`, which we
# hold **false** except inside a pairing window -- so off is the correct resting
# state, not a fault, and every adapter here reads `powered connectable ssp
# br/edr le secure-conn` with no `bondable` while working perfectly.
#
# Requiring it meant this check could only ever fail, and its "correction"
# would have written `bondable on` through btmgmt behind bluetoothd's back on
# every reconcile -- the two-owners-for-one-setting fight the docstring below
# warns about, permanently leaving all four adapters bondable to anyone. That
# never happened only because the btmgmt hang (see `_run`) stopped the check
# reaching its own verdict; fixing the hang without this would have shipped it.
#
# The right lever for bondable is `set_pairable`, which sets Pairable over
# D-Bus and lets bluetoothd -- the owner -- move MGMT itself.


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

    # Secure Simple Pairing is a BR/EDR concept. On an adapter running the BLE
    # transport there is no BR/EDR to pair over, so demanding SSP there reports
    # a fault that cannot exist and tells the operator hosts will be prompted
    # for a PIN -- on a radio that has no PIN pairing to fall back to.
    if settings is not None and "br/edr" not in settings:
        log.debug("%s is LE only; SSP does not apply", adapter.hci_name)
        return
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

    # Only what we actually diagnosed as wrong. `bondable on` used to be written
    # here too; it is bluetoothd's to set via Pairable (see above), and writing
    # it from a second MGMT client is how the two desynchronise.
    for setting, value in (("linksec", "off"), ("ssp", "on")):
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


#: Adapters already warned about a class of device that would not stick.
#: Latched because this runs on every reconcile and every pairing window, and
#: the remedy is a one-time edit to a file -- repeating it every ten seconds
#: would bury the line that matters.
_class_warned: set[str] = set()


def _set_device_class(adapter: AdapterInfo) -> None:
    """Set the gamepad class of device, and check that it stuck.

    Stays on hciconfig because BlueZ exposes no writable Class property, and
    consoles filter their pairing list on this value -- get it wrong and we
    never appear as a candidate controller at all.

    Everything else about an adapter (alias, pairable, discoverable) goes
    through D-Bus instead: see server/bt/adapter_dbus.py for why bluetoothctl
    is unusable in a multi-adapter system.

    **The write can silently do nothing.** hciconfig writes raw HCI, below
    MGMT, and bluetoothd recomputes the class through MGMT whenever it feels
    like it -- so the value reverts with no error from anything. The symptom is
    that the console simply never lists us as a candidate controller, which
    reads as a pairing problem and sends people looking at SSP and bonds.

    Reading it back is the only way to know, so this does. The fix is to put
    the class in bluetoothd's own config rather than fighting it, which is a
    system file we should not be editing on the operator's behalf -- so say so
    once and let them decide.
    """
    if shutil.which("hciconfig") is None:
        return

    code, output = _run(
        ["hciconfig", adapter.hci_name, "class", f"0x{GAMEPAD_CLASS_OF_DEVICE:06x}"]
    )
    if code != 0:
        log.debug("Could not set class on %s: %s", adapter.hci_name, output.strip())
        return

    actual = _read_device_class(adapter.hci_name)
    if actual is None or actual == GAMEPAD_CLASS_OF_DEVICE:
        _class_warned.discard(adapter.hci_name)
        return

    if adapter.hci_name in _class_warned:
        return
    _class_warned.add(adapter.hci_name)
    log.warning(
        "Class of device on %s reverted to 0x%06x (wanted 0x%06x). bluetoothd "
        "recomputes it through MGMT and overwrites what hciconfig wrote. A "
        "console filters its pairing list on this value, so it may never offer "
        "us as a controller. Set it in bluetoothd's own config instead:\n"
        "    /etc/bluetooth/main.conf  ->  [General]  Class = 0x%06x\n"
        "then: sudo systemctl restart bluetooth",
        adapter.hci_name,
        actual,
        GAMEPAD_CLASS_OF_DEVICE,
        GAMEPAD_CLASS_OF_DEVICE,
    )


def _read_device_class(hci_name: str) -> int | None:
    """Read the adapter's class of device, or None if it cannot be determined.

    Reads over HCI rather than trusting MGMT's cached view: raw hciconfig
    writes desynchronise the two, so MGMT will happily report a value the radio
    is not using.
    """
    code, output = _run(["hciconfig", hci_name, "class"])
    if code != 0:
        return None
    match = re.search(r"Class:\s*0x([0-9a-fA-F]{6})", output)
    return int(match.group(1), 16) if match else None


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

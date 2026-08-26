"""One adapter's state, as an object that outlives every rescan.

Why this exists
---------------
Adapter state used to be spread across four places that disagreed: fields on an
``AdapterInfo``, two sets on the manager (``_configured`` and ``_quieted``), the
HID server's own view, and the config file. Worse, ``rescan()`` **replaced every
AdapterInfo with a fresh object** on a 10 s timer, so anything transient held on
the old one was silently lost -- which is why the pairing countdown read zero a
few seconds after the operator armed it, and why a degraded adapter looked
healthy again between rescans. The fix at the time was to hand-copy two fields
across the rebuild, which works exactly until someone adds a third.

So: **one object per BD_ADDR, created once, mutated in place, never rebuilt.**
Rescan updates fields; it does not construct. A new piece of per-adapter state
added here cannot go missing, because there is no rebuild to forget it in.

The phases
----------
::

    DETECTED ──► CONFIGURING ──► LISTENING ⇄ PAIRING
                                     │  ▲       │
                                     ▼  │       ▼
                                   LINKED ◄─────┘
                      DEGRADED   the HID stack could not start here
                      QUIET      the operator disabled it

``DEGRADED`` and ``QUIET`` are deliberately distinct, and neither is an error
state. A degraded adapter is *present and visible in the GUI but inert* -- it
keeps its name and number so the operator can see which dongle is unwell -- and
it must never advertise, because a host that finds a controller and then cannot
complete the connection reports only "try again". A quiet adapter is one the
operator switched off, and its radio is silenced properly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from common.timing import now_ns

log = logging.getLogger(__name__)


class Phase(Enum):
    """What an adapter is currently doing."""

    #: Enumerated, nothing written to it yet. An adapter the operator has never
    #: enabled stays here forever, untouched -- which is the promise that lets
    #: the Pi use a dongle for something else.
    DETECTED = "detected"

    #: We are writing its name, class and scan settings.
    CONFIGURING = "configuring"

    #: HID is bound and the radio is connectable. Waiting for a host.
    LISTENING = "listening"

    #: A bounded pairing window is open: discoverable and bondable.
    PAIRING = "pairing"

    #: A host is connected and reports are flowing.
    LINKED = "linked"

    #: Enabled, but the HID stack could not start. Visible and inert.
    DEGRADED = "degraded"

    #: Disabled by the operator; the radio has been silenced.
    QUIET = "quiet"


#: Which phases may follow which. Not enforced as an assertion -- hardware does
#: surprising things and refusing a transition would be worse than taking it --
#: but an unexpected one is logged, because it usually means two code paths are
#: fighting over the same adapter.
_EXPECTED: dict[Phase, frozenset[Phase]] = {
    Phase.DETECTED: frozenset({Phase.CONFIGURING, Phase.QUIET}),
    Phase.CONFIGURING: frozenset({Phase.LISTENING, Phase.DEGRADED, Phase.QUIET}),
    Phase.LISTENING: frozenset({Phase.PAIRING, Phase.LINKED, Phase.DEGRADED, Phase.QUIET}),
    Phase.PAIRING: frozenset({Phase.LISTENING, Phase.LINKED, Phase.DEGRADED, Phase.QUIET}),
    Phase.LINKED: frozenset({Phase.LISTENING, Phase.PAIRING, Phase.DEGRADED, Phase.QUIET}),
    Phase.DEGRADED: frozenset({Phase.CONFIGURING, Phase.LISTENING, Phase.QUIET}),
    Phase.QUIET: frozenset({Phase.DETECTED, Phase.CONFIGURING}),
}


@dataclass(slots=True)
class AdapterState:
    """Everything known about one physical adapter.

    Identified by BD_ADDR throughout. ``index`` and ``hci_name`` are cached
    because the kernel APIs take an index, but they are *derived* -- they
    reshuffle across reboots and replugs and must never be used as identity.
    """

    bd_addr: str
    index: int = -1
    hci_name: str = ""
    manufacturer: str = ""

    phase: Phase = Phase.DETECTED

    #: The operator's choice. Distinct from the phase: a disabled adapter that
    #: has not been silenced yet is ``enabled=False`` and still LISTENING.
    enabled: bool = True

    #: 1-4, assigned per BD_ADDR and persisted, so the name follows the
    #: physical dongle across reboots.
    number: int = 0
    name: str = ""

    # -- MGMT-derived, refreshed from events rather than polled -------------
    powered: bool = False
    connectable: bool = False
    discoverable: bool = False
    bondable: bool = False
    ssp: bool = False
    link_security: bool = False
    device_class: int = 0

    #: True once these came from the management socket.
    #:
    #: The fallback enumerator parses ``hciconfig`` output, which cannot see
    #: page scan, SSP or link security at all -- so those fields sit at their
    #: defaults, and every one of those defaults happens to look like a fault.
    #: Without this flag :meth:`health` would confidently report "page scan is
    #: off" on a machine where we simply never read it, which is worse than
    #: saying nothing: it sends the operator to fix something that is not
    #: broken.
    settings_known: bool = False

    # -- ours ---------------------------------------------------------------

    #: Why the HID stack could not start, or "" when healthy.
    hid_error: str = ""

    #: Set when a host-wide prerequisite for this transport is not in place.
    #:
    #: Kept as text rather than a flag because the only useful form of this is
    #: the sentence naming the symptom -- the operator sees a console that
    #: works and then stops, not a configuration file. Filled in by
    #: AdapterManager, which is the layer that can read /etc/bluetooth.
    host_config_warning: str = ""

    #: Monotonic deadline (ns) while a pairing window is open, else 0.
    pairing_until_ns: int = 0

    #: The host currently connected, or "".
    peer: str = ""

    #: What the link tuning achieved, from server/bt/link.py.
    link_report: object | None = None

    #: BD_ADDRs we have bonded with, as BlueZ reports them. Read from
    #: org.bluez rather than kept in our own config: a parallel copy goes
    #: stale, and a stale reconnect target means paging a host that can no
    #: longer authenticate us, forever.
    bonds: tuple[str, ...] = ()

    #: True once this process has written anything to the radio. Only these are
    #: reverted when the adapter is disabled, so one we never enabled is never
    #: written to.
    configured: bool = False

    #: Set once the radio has been silenced, so the reconcile pass does not
    #: re-silence it every ten seconds forever.
    quieted: bool = False

    _observers: list = field(default_factory=list, repr=False)

    # -- phase ---------------------------------------------------------------

    def to(self, phase: Phase, *, reason: str = "") -> bool:
        """Move to a new phase. Returns True if anything changed.

        An unexpected transition is logged rather than refused. Hardware does
        surprising things, and a state machine that raised would turn a
        cosmetic bookkeeping problem into a dead adapter -- but an unexpected
        transition is nearly always two code paths fighting over one radio, so
        it must not pass silently either.
        """
        if phase is self.phase:
            return False

        if phase not in _EXPECTED.get(self.phase, frozenset()):
            log.warning(
                "Adapter %s moved %s -> %s, which is not an expected transition%s. "
                "This usually means two code paths are driving the same adapter.",
                self.bd_addr, self.phase.value, phase.value,
                f" ({reason})" if reason else "",
            )
        else:
            log.debug(
                "Adapter %s %s -> %s%s",
                self.bd_addr, self.phase.value, phase.value,
                f" ({reason})" if reason else "",
            )

        self.phase = phase
        return True

    @property
    def is_live(self) -> bool:
        """True when this adapter can actually carry input to a console."""
        return self.phase is Phase.LINKED

    @property
    def is_usable(self) -> bool:
        """True when it is enabled and the HID stack is running.

        Not the same as :attr:`is_live`: an adapter waiting for a console to
        connect is usable but not live.
        """
        return self.enabled and not self.hid_error and self.phase in (
            Phase.LISTENING, Phase.PAIRING, Phase.LINKED
        )

    # -- pairing window ------------------------------------------------------

    @property
    def pairing_remaining_s(self) -> int:
        """Seconds left in the pairing window, or 0 when not pairing."""
        if not self.pairing_until_ns:
            return 0
        return max(0, int((self.pairing_until_ns - now_ns()) / 1e9))

    @property
    def pairing_expired(self) -> bool:
        """True when a window was armed and its deadline has passed.

        A window that ends on its own does **not** clean up after itself:
        BlueZ's own DiscoverableTimeout stops reporting the adapter as
        discoverable but never writes scan enable back down, so the radio keeps
        answering inquiries and the gamepad keeps appearing in a host's
        "Add a device" list long after the window closed. Nothing in MGMT or
        D-Bus reports this. Whoever owns the reconcile pass has to notice.
        """
        return bool(self.pairing_until_ns) and now_ns() >= self.pairing_until_ns

    def arm_pairing(self, duration_s: float) -> None:
        self.pairing_until_ns = now_ns() + int(duration_s * 1e9)
        self.to(Phase.PAIRING, reason=f"{duration_s:.0f}s window")

    def clear_pairing(self, *, reason: str = "") -> None:
        """End the window, without deciding what comes next.

        The caller chooses the destination phase, because "the operator pressed
        stop" and "a console connected" both end a window and land somewhere
        different.
        """
        self.pairing_until_ns = 0
        if self.phase is Phase.PAIRING:
            self.to(Phase.LISTENING, reason=reason or "pairing window ended")

    # -- MGMT ----------------------------------------------------------------

    def apply_settings(self, settings) -> bool:
        """Refresh from an :class:`~server.bt.mgmt.AdapterSettings`.

        Returns True if anything the GUI shows changed, so callers can push an
        update only when there is something to say -- the status feed runs at
        10 Hz and a no-op refresh on every tick is what makes a web GUI
        unusable while an adapter is being configured.
        """
        before = (
            self.powered, self.connectable, self.discoverable,
            self.bondable, self.ssp, self.link_security, self.device_class,
        )

        self.index = settings.index
        # Only derive the name from a real index. The bluetoothctl fallback has
        # none, and overwriting its address-shaped name with "hci-1" would turn
        # a usable identifier into one that matches no device.
        if settings.index >= 0:
            self.hci_name = f"hci{settings.index}"
        elif getattr(settings, "hci_name", ""):
            self.hci_name = settings.hci_name
        # Only when the observation has one: MGMT reports a numeric company id,
        # so the readable string comes from hciconfig and must not be wiped by
        # the next MGMT-only refresh.
        manufacturer = getattr(settings, "manufacturer", "")
        if isinstance(manufacturer, str) and manufacturer:
            self.manufacturer = manufacturer
        self.settings_known = getattr(settings, "settings_known", True)
        self.powered = settings.powered
        self.connectable = settings.connectable
        self.discoverable = settings.discoverable
        self.bondable = settings.bondable
        self.ssp = settings.ssp
        self.link_security = settings.link_security
        self.device_class = settings.device_class

        after = (
            self.powered, self.connectable, self.discoverable,
            self.bondable, self.ssp, self.link_security, self.device_class,
        )
        return before != after

    # -- diagnostics ---------------------------------------------------------

    def health(self) -> list[str]:
        """Problems a host would notice, in plain language.

        Every one of these has cost this project a debugging session, and every
        one presents to the operator as the same thing: a console that will not
        pair. Saying which is which on screen is the whole point.
        """
        problems: list[str] = []

        if self.hid_error:
            problems.append(f"HID stack is not running: {self.hid_error}")

        # Ahead of the settings_known gate below: this one is about the host's
        # bluetoothd configuration, not about anything the adapter can report,
        # so returning early would hide it on exactly the adapters that have it.
        if self.host_config_warning:
            problems.append(self.host_config_warning)

        if not self.settings_known:
            # Nothing below can be answered from hciconfig output, and a
            # confident wrong answer is worse than none.
            return problems

        if self.enabled and self.powered and not self.connectable:
            problems.append(
                "Page scan is off, so no host can connect to this adapter. BlueZ "
                "only keeps an adapter connectable on its own once it has a bond, "
                "and it cannot gain one while unreachable -- a trap that cannot "
                "open itself. The host reports only 'We didn't get any response "
                "from the device'."
            )

        if self.link_security:
            problems.append(
                "Link security is on, which forces legacy PIN pairing: the "
                "controller demands authentication as the link comes up and never "
                "reaches the Secure Simple Pairing exchange."
            )

        if self.enabled and self.powered and not self.ssp:
            problems.append(
                "Secure Simple Pairing is off, so the specification requires "
                "legacy PIN pairing."
            )

        major = (self.device_class >> 8) & 0x1F
        minor = (self.device_class >> 2) & 0x3F
        if self.configured and (major, minor) != (0x05, 0x02):
            problems.append(
                f"Class of device is 0x{self.device_class:06X} (major {major}, "
                f"minor {minor}), not peripheral/gamepad. A console that filters "
                "its pairing list on the class will never offer this as a "
                "controller."
            )

        return problems

    def snapshot(self) -> dict[str, object]:
        """What the web GUI renders. Key names are the existing contract."""
        return {
            "bd_addr": self.bd_addr,
            "hci": self.hci_name,
            "manufacturer": self.manufacturer,
            "up": self.powered,
            "enabled": self.enabled,
            "number": self.number,
            "name": self.name,
            "pairing_s": self.pairing_remaining_s,
            "hid_error": self.hid_error,
            # New in the event-driven rewrite.
            "phase": self.phase.value,
            "connectable": self.connectable,
            "discoverable": self.discoverable,
            "peer": self.peer,
            "bonds": list(self.bonds),
            "health": self.health(),
            "link": (
                self.link_report.snapshot()
                if self.link_report is not None
                else None
            ),
        }


class AdapterRegistry:
    """Every adapter we have ever seen this run, keyed by BD_ADDR.

    The guarantee this class exists to provide: **an AdapterState is created
    once and never replaced.** Refreshing from the hardware updates fields on
    the existing objects. Nothing held on one can be lost by a rescan, because
    there is no rescan-time reconstruction to lose it in.

    An adapter that is unplugged is *removed*; one that is plugged back in gets
    a fresh object, which is correct -- its HID stack is gone and its phase
    genuinely starts over.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, AdapterState] = {}

    def ensure(self, bd_addr: str) -> AdapterState:
        """Get the state for an address, creating it only the first time."""
        bd_addr = bd_addr.upper()
        state = self._adapters.get(bd_addr)
        if state is None:
            state = AdapterState(bd_addr=bd_addr)
            self._adapters[bd_addr] = state
        return state

    def get(self, bd_addr: str) -> AdapterState | None:
        return self._adapters.get(bd_addr.upper())

    @property
    def states(self) -> dict[str, AdapterState]:
        """The live mapping, by BD_ADDR.

        Returned rather than copied so a caller holding it sees later changes
        and can seed an adapter directly. The "created once" guarantee is about
        :meth:`sync` never reconstructing what it already has, not about
        forbidding anyone else from putting an adapter in.
        """
        return self._adapters

    def remove(self, bd_addr: str) -> AdapterState | None:
        return self._adapters.pop(bd_addr.upper(), None)

    def all(self) -> list[AdapterState]:
        return list(self._adapters.values())

    def __len__(self) -> int:
        return len(self._adapters)

    def __contains__(self, bd_addr: object) -> bool:
        return isinstance(bd_addr, str) and bd_addr.upper() in self._adapters

    def sync(self, settings_by_addr: dict) -> tuple[list[str], list[str], bool]:
        """Reconcile against what MGMT currently reports.

        Returns ``(added, removed, changed)`` -- the two address lists, and
        whether any surviving adapter's visible state moved. Callers push a GUI
        update on any of the three and stay quiet otherwise.
        """
        present = {addr.upper() for addr in settings_by_addr}
        known = set(self._adapters)

        added = sorted(present - known)
        removed = sorted(known - present)
        changed = False

        for addr in removed:
            gone = self._adapters.pop(addr)
            log.warning(
                "Adapter %s (%s) disappeared", addr, gone.hci_name or "unknown"
            )

        for addr, settings in settings_by_addr.items():
            state = self.ensure(addr)
            if state.apply_settings(settings):
                changed = True

        for addr in added:
            state = self._adapters[addr]
            log.info("Adapter %s (%s) detected", addr, state.hci_name)

        return added, removed, changed

    def snapshot(self) -> list[dict[str, object]]:
        """Adapters as the web GUI sees them, in player order.

        Unnumbered adapters -- disabled, or never brought up -- sort last
        rather than to the front, so the numbered ones read 1..4 left to right.
        """
        rows = [state.snapshot() for state in self._adapters.values()]
        rows.sort(key=lambda row: (row["number"] or 99, row["hci"]))
        return rows


class FlagSet:
    """A set-like view over one boolean field across every adapter.

    ``configured`` and ``quieted`` used to be two ``set[str]`` on the manager,
    sitting beside the adapter objects they described. That had the usual
    consequence of state kept next to its subject rather than on it: **nothing
    removed an address when the dongle was unplugged.** Replug it and the fresh
    adapter inherited the old one's flags, so a disabled adapter that had
    already been silenced was skipped by the pass that would have silenced it
    again -- and went on advertising.

    Keeping the flag on :class:`AdapterState` fixes that by construction: the
    flag is gone when the adapter is. This view exists so the call sites, which
    read naturally as set membership, do not have to change.
    """

    __slots__ = ("_registry", "_field")

    def __init__(self, registry: "AdapterRegistry", field_name: str) -> None:
        self._registry = registry
        self._field = field_name

    def add(self, bd_addr: str) -> None:
        setattr(self._registry.ensure(bd_addr), self._field, True)

    def discard(self, bd_addr: str) -> None:
        state = self._registry.get(bd_addr)
        if state is not None:
            setattr(state, self._field, False)

    def __contains__(self, bd_addr: object) -> bool:
        if not isinstance(bd_addr, str):
            return False
        state = self._registry.get(bd_addr)
        return state is not None and bool(getattr(state, self._field))

    def __iter__(self):
        return (
            s.bd_addr for s in self._registry.all() if getattr(s, self._field)
        )

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (set, frozenset, FlagSet)):
            return set(self) == set(other)
        return NotImplemented

    def __repr__(self) -> str:
        return f"<FlagSet {self._field}={sorted(self)}>"

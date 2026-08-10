"""Routes (client, slot) pairs to Bluetooth adapter outputs.

The mapping is operator-controlled from the web GUI: which player's controller
drives which console. It changes rarely but is read on every input packet, so
reads must be fast and must never block on the writer.

Concurrency approach: the authoritative mapping lives in a dict that is only
ever *replaced*, never mutated in place. The datapath reads the current
reference without a lock; dict rebinding is atomic under CPython's GIL, so a
reader either sees the old complete mapping or the new complete mapping, never
a half-updated one. Writers (the web GUI) build a new dict under a lock and
swap it in.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from common.timing import LatencyStats
from server.bt.profiles.base import TargetProfile
from server.bt.sink import HIDSink

log = logging.getLogger(__name__)

#: Design ceiling. Even with more dongles present, we never drive more than
#: this many controllers -- consoles do not accept more, and it bounds the
#: datapath's per-tick work.
MAX_OUTPUTS = 4


@dataclass(slots=True)
class OutputChannel:
    """One Bluetooth adapter acting as one emulated controller."""

    #: Stable identity. BD_ADDR, never hciX -- index numbering reshuffles
    #: across reboots and would silently remap players to the wrong console.
    bd_addr: str

    hci_name: str
    profile: TargetProfile
    sink: HIDSink

    #: Which client+slot currently feeds this channel, if any.
    assigned_client: str | None = None
    assigned_slot: int | None = None

    #: Assigned username, mirrored here so the web GUI can render the table
    #: without joining against session state.
    username: str = ""

    reports_sent: int = 0
    reports_dropped: int = 0
    write_stats: LatencyStats = field(default_factory=LatencyStats)

    #: Preallocated report buffer, written by the datapath each time it builds
    #: a report for this channel. Public because the datapath owns it during a
    #: packet; exclusive because exactly one thread ever touches a given
    #: channel's buffer.
    report_buf: bytearray = field(default_factory=lambda: bytearray(64), repr=False)

    @property
    def is_assigned(self) -> bool:
        return self.assigned_client is not None and self.assigned_slot is not None

    @property
    def is_live(self) -> bool:
        """True when this channel can actually deliver input to a console."""
        return self.sink.is_connected and self.profile.is_ready

    def snapshot(self) -> dict[str, object]:
        return {
            "bd_addr": self.bd_addr,
            "hci": self.hci_name,
            "profile": self.profile.name,
            "profile_display": self.profile.display_name,
            "connected": self.sink.is_connected,
            "ready": self.profile.is_ready,
            "assigned_client": self.assigned_client,
            "assigned_slot": self.assigned_slot,
            "username": self.username,
            "reports_sent": self.reports_sent,
            "reports_dropped": self.reports_dropped,
            "write_ms": self.write_stats.snapshot(),
        }


class Router:
    """Maps controller inputs to output channels."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

        #: bd_addr -> OutputChannel. Replaced wholesale on change.
        self._channels: dict[str, OutputChannel] = {}

        #: (client_id, slot) -> OutputChannel. The datapath's lookup table.
        #: Replaced wholesale; never mutated in place.
        self._routes: dict[tuple[str, int], OutputChannel] = {}

    # -- datapath (hot) ----------------------------------------------------

    def resolve(self, client_id: str, slot: int) -> OutputChannel | None:
        """Find the channel for a controller. Lock-free by design.

        Reads the current dict reference exactly once, so a concurrent swap
        cannot produce a torn read.
        """
        return self._routes.get((client_id, slot))

    # -- channel management ------------------------------------------------

    def add_channel(self, channel: OutputChannel) -> None:
        with self._lock:
            if len(self._channels) >= MAX_OUTPUTS and channel.bd_addr not in self._channels:
                raise ValueError(
                    f"Cannot add {channel.bd_addr}: already at the {MAX_OUTPUTS}-adapter ceiling"
                )
            channels = dict(self._channels)
            channels[channel.bd_addr] = channel
            self._channels = channels
            self._rebuild_routes_locked()
        log.info("Output channel added: %s (%s)", channel.bd_addr, channel.hci_name)

    def remove_channel(self, bd_addr: str) -> None:
        """Remove a channel -- typically because its dongle was unplugged.

        Any controller assigned to it becomes unassigned. This must never raise
        for an unknown address: hot-plug races are normal.
        """
        with self._lock:
            channels = dict(self._channels)
            channel = channels.pop(bd_addr, None)
            if channel is None:
                return
            self._channels = channels
            self._rebuild_routes_locked()

        if channel.is_assigned:
            log.warning(
                "Adapter %s removed while assigned to %s slot %s; controller is now unassigned",
                bd_addr,
                channel.assigned_client,
                channel.assigned_slot,
            )
        try:
            channel.sink.close()
        except Exception:
            log.exception("Error closing sink for %s", bd_addr)

    def channels(self) -> list[OutputChannel]:
        return list(self._channels.values())

    def channel(self, bd_addr: str) -> OutputChannel | None:
        return self._channels.get(bd_addr)

    @property
    def capacity(self) -> int:
        """How many controllers the server can currently drive.

        This is what clients are told, and what makes the client GUI grey out
        slots it cannot use.
        """
        return len(self._channels)

    # -- assignment --------------------------------------------------------

    def assign(self, bd_addr: str, client_id: str, slot: int, username: str = "") -> bool:
        """Route a client's controller slot to an adapter.

        An adapter drives exactly one controller, and a controller drives
        exactly one adapter, so this clears any conflicting assignment first.
        """
        with self._lock:
            channel = self._channels.get(bd_addr)
            if channel is None:
                log.warning("Cannot assign: no adapter %s", bd_addr)
                return False

            for other in self._channels.values():
                if other is not channel and (
                    other.assigned_client == client_id and other.assigned_slot == slot
                ):
                    other.assigned_client = None
                    other.assigned_slot = None
                    other.username = ""

            channel.assigned_client = client_id
            channel.assigned_slot = slot
            channel.username = username
            self._rebuild_routes_locked()

        log.info("Assigned %s slot %d (%s) -> %s", client_id, slot, username or "-", bd_addr)
        return True

    def unassign(self, bd_addr: str) -> None:
        with self._lock:
            channel = self._channels.get(bd_addr)
            if channel is None:
                return
            channel.assigned_client = None
            channel.assigned_slot = None
            channel.username = ""
            self._rebuild_routes_locked()
        log.info("Unassigned adapter %s", bd_addr)

    def unassign_client(self, client_id: str) -> None:
        """Drop every assignment for a client. Called when it disconnects."""
        with self._lock:
            changed = False
            for channel in self._channels.values():
                if channel.assigned_client == client_id:
                    channel.assigned_client = None
                    channel.assigned_slot = None
                    channel.username = ""
                    changed = True
            if changed:
                self._rebuild_routes_locked()

    def set_username(self, client_id: str, slot: int, username: str) -> None:
        with self._lock:
            for channel in self._channels.values():
                if channel.assigned_client == client_id and channel.assigned_slot == slot:
                    channel.username = username
                    break

    def auto_assign(self, client_id: str, slots: list[int], usernames: dict[int, str]) -> int:
        """Assign a client's controllers to whatever adapters are free.

        Used when the operator has enabled auto-accept, so a returning player
        does not have to be hand-assigned every session. Returns how many were
        newly placed.

        Slots that already have a channel are skipped. This is called more than
        once per client -- at session creation and again when SET_CONTROLLERS
        arrives -- and without the check the second pass would move an already
        working slot onto a different adapter, silently orphaning the first.
        """
        assigned = 0
        for slot in slots:
            if self.resolve(client_id, slot) is not None:
                continue
            free = self._first_free_channel()
            if free is None:
                break
            if self.assign(free, client_id, slot, usernames.get(slot, "")):
                assigned += 1
        return assigned

    def _first_free_channel(self) -> str | None:
        for bd_addr, channel in self._channels.items():
            if not channel.is_assigned:
                return bd_addr
        return None

    def _rebuild_routes_locked(self) -> None:
        """Rebuild the datapath lookup table. Caller must hold the lock.

        Builds a fresh dict and swaps it in so readers never observe a partial
        update.
        """
        routes: dict[tuple[str, int], OutputChannel] = {}
        for channel in self._channels.values():
            if channel.assigned_client is not None and channel.assigned_slot is not None:
                routes[(channel.assigned_client, channel.assigned_slot)] = channel
        self._routes = routes

    # -- reporting ---------------------------------------------------------

    def snapshot(self) -> list[dict[str, object]]:
        return [channel.snapshot() for channel in self._channels.values()]

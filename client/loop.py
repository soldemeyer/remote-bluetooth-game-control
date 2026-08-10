"""The client input loop -- the client's hot path.

Runs on its own thread. Each tick:

    1. pump the input backend once (one device read for all controllers)
    2. poll each enabled controller
    3. send state that changed since last tick
    4. service the transport (acks, heartbeats, control retries)

Send-on-change is the whole point: an idle controller costs one heartbeat every
20 ms, while a moving stick is transmitted the moment it moves. Polling faster
than we transmit is deliberate -- we want to *notice* the change immediately,
not to send redundant packets.

Design rules for this file:
  * No allocation inside the tick. Buffers and state objects are preallocated.
  * No logging per packet. Only on state transitions.
  * Never raise out of the tick -- a single bad controller must not kill the loop.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from client.input.base import InputBackend
from client.net.transport import ClientTransport
from common.state import ControllerState
from common.timing import (
    LatencyStats,
    RateLimiter,
    high_resolution_timers,
    now_ns,
    ns_to_ms,
)

log = logging.getLogger(__name__)

#: Force a packet at least this often per controller even with no change, so
#: the server can distinguish "idle" from "client died" per slot, and so
#: latency stats stay fresh while a player holds still.
_KEEPALIVE_INTERVAL_NS = 100_000_000  # 100 ms

#: Request a latency ack on roughly this cadence. Every packet would double
#: return traffic for no extra insight.
_ACK_INTERVAL_NS = 50_000_000  # 50 ms


@dataclass(slots=True)
class SlotRuntime:
    """Live state for one controller slot. Preallocated -- reused every tick."""

    slot: int
    instance_id: int
    username: str = ""
    device_name: str = ""

    current: ControllerState = field(default_factory=ControllerState)
    last_sent: ControllerState = field(default_factory=ControllerState)

    last_send_ns: int = 0
    last_ack_request_ns: int = 0
    was_connected: bool = True
    packets_sent: int = 0

    #: Time from backend sample to socket write. Isolates *our* overhead from
    #: network and Bluetooth cost.
    encode_stats: LatencyStats = field(default_factory=LatencyStats)


class InputLoop:
    """Polls controllers and streams their state to the server."""

    def __init__(
        self,
        backend: InputBackend,
        transport: ClientTransport,
        *,
        poll_hz: int = 500,
        axis_deadband: int = 256,
    ) -> None:
        self._backend = backend
        self._transport = transport
        self._poll_hz = poll_hz
        self._axis_deadband = axis_deadband

        self._slots: list[SlotRuntime] = []
        self._slots_lock = threading.Lock()

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._tick_stats = LatencyStats()

    # -- slot management ---------------------------------------------------

    def set_slots(self, slots: list[SlotRuntime]) -> None:
        """Replace the active slot list.

        Swaps the whole list under a lock rather than mutating in place, so the
        tick always sees a coherent set -- never a half-updated one.
        """
        with self._slots_lock:
            self._slots = slots
        log.info("Now streaming %d controller(s)", len(slots))

    def slots(self) -> list[SlotRuntime]:
        with self._slots_lock:
            return list(self._slots)

    def set_username(self, slot: int, username: str) -> None:
        with self._slots_lock:
            for entry in self._slots:
                if entry.slot == slot:
                    entry.username = username
                    break

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="input-loop", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- the loop ----------------------------------------------------------

    def _run(self) -> None:
        # Raising Windows timer resolution is what makes the configured poll
        # rate real rather than aspirational. See common/timing.py.
        with high_resolution_timers():
            limiter = RateLimiter(self._poll_hz)
            log.info("Input loop started at %d Hz", self._poll_hz)

            while not self._stop.is_set():
                try:
                    self._tick()
                except Exception:
                    # A backend hiccup must not take down input for everyone.
                    log.exception("Error in input tick; continuing")
                limiter.wait()

        self._send_neutral_on_exit()
        log.info("Input loop stopped")

    def _tick(self) -> None:
        tick_start = now_ns()

        self._backend.pump()

        with self._slots_lock:
            slots = self._slots

        for entry in slots:
            self._service_slot(entry, tick_start)

        self._transport.service()
        self._tick_stats.add(ns_to_ms(now_ns() - tick_start))

    def _service_slot(self, entry: SlotRuntime, now: int) -> None:
        connected = self._backend.poll(entry.instance_id, entry.current)

        if not connected:
            if entry.was_connected:
                # Send one neutral state so the console does not latch whatever
                # was held when the pad vanished -- otherwise the character runs
                # into a wall forever.
                entry.was_connected = False
                entry.current.clear()
                entry.current.copy_into(entry.last_sent)
                self._transport.send_input(
                    entry.slot, entry.current, request_ack=False, disconnected=True
                )
                log.warning("Controller in slot %d disconnected", entry.slot)
            return

        if not entry.was_connected:
            entry.was_connected = True
            log.info("Controller in slot %d reconnected", entry.slot)
            # Force a send so the server sees current state immediately.
            entry.last_send_ns = 0

        changed = entry.current.differs_from(
            entry.last_sent, axis_deadband=self._axis_deadband
        )
        keepalive_due = now - entry.last_send_ns >= _KEEPALIVE_INTERVAL_NS

        if not (changed or keepalive_due):
            return

        request_ack = now - entry.last_ack_request_ns >= _ACK_INTERVAL_NS
        if request_ack:
            entry.last_ack_request_ns = now

        self._transport.send_input(entry.slot, entry.current, request_ack=request_ack)

        entry.current.copy_into(entry.last_sent)
        entry.last_send_ns = now
        entry.packets_sent += 1
        entry.encode_stats.add(ns_to_ms(now_ns() - now))

    def _send_neutral_on_exit(self) -> None:
        """Release every input on shutdown.

        Without this, quitting mid-press leaves the console holding that button
        until the session times out.
        """
        if not self._transport.is_connected:
            return
        for entry in self.slots():
            entry.current.clear()
            try:
                self._transport.send_input(
                    entry.slot, entry.current, request_ack=False, disconnected=True
                )
            except Exception:
                log.debug("Could not send neutral state for slot %d", entry.slot)

    # -- stats -------------------------------------------------------------

    def stats_snapshot(self) -> dict[str, object]:
        """Loop health for the GUI and the latency harness."""
        return {
            "poll_hz": self._poll_hz,
            "tick_ms": self._tick_stats.snapshot(),
            "slots": [
                {
                    "slot": entry.slot,
                    "username": entry.username,
                    "device": entry.device_name,
                    "connected": entry.was_connected,
                    "packets_sent": entry.packets_sent,
                    "encode_ms": entry.encode_stats.snapshot(),
                }
                for entry in self.slots()
            ],
        }

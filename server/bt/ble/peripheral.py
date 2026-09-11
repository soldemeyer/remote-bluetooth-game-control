"""One adapter acting as a BLE HID gamepad.

DO NOT add ``from __future__ import annotations`` -- see gatt.py.

Ties the GATT application and the advertisement to one adapter, and exposes a
:class:`~server.bt.sink.HIDSink` so the router and the datapath drive it exactly
as they drive the Classic path. That is the whole reason the sink interface
exists: adding a second transport should not reach above this layer, and it
does not.

What is genuinely different from Classic
----------------------------------------
* **Per adapter, not per machine.** ``GattManager1`` and
  ``LEAdvertisingManager1`` both live on the adapter object, so four dongles
  are four independent peripherals with their own services and names. The
  Classic side cannot do this -- one SDP database per machine.
* **No sniff, no flush timeout.** Latency is governed by the connection
  interval and peripheral latency, and the **central grants both**. We can ask
  (a Connection Parameter Update Request) and it can refuse. None of the
  levers in ``server/bt/link.py`` apply here.
* **Notifications can be dropped on the floor.** A host subscribes to the
  report characteristic as the last step of connecting; anything notified
  before that goes nowhere and reports success.
"""

import asyncio
import logging
import threading

from common.timing import LatencyStats, now_ns, ns_to_ms
from server.bt.ble import hogp
from server.bt.ble._dbus import writer_backlog
from server.bt.sink import HIDSink

log = logging.getLogger(__name__)

def _index_of(hci_name):
    """``hci3`` -> ``3``. -1 when there is no index to be had."""
    suffix = str(hci_name).removeprefix("hci")
    return int(suffix) if suffix.isdigit() else -1


BLUEZ = "org.bluez"
GATT_MANAGER_IFACE = "org.bluez.GattManager1"
LE_ADVERTISING_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"


class BLESink(HIDSink):
    """Delivers input reports as GATT notifications, coalesced and paced.

    Why this is not simply ``characteristic.notify(report)``
    -------------------------------------------------------
    It used to be, and that is what made a second player degrade the first.
    Measured on the reference Pi, two independent 500 Hz sessions:

        player 1 alone          p99 RTT   4.38 ms
        + player 2 connected    p99 RTT   4.38 ms   (idle: no cost at all)
        + player 2 playing      p99 RTT  20.47 ms   (+368%)

    Three things were wrong, and they compound:

    * **A notification is a D-Bus signal, and marshalling it is expensive.**
      188 us per report of pure-Python work on the datapath thread -- against a
      1 ms whole-system budget, and 0.03-0.09 ms for the entire Classic path.
      See ``server/bt/ble/_dbus.py`` for the breakdown.
    * **Nothing bounded the queue.** Both bindings buffer outbound messages in
      an unbounded queue and report success the instant a message is *queued*.
      Reports piled up with every counter reporting health -- the same failure
      the Classic path documents at the L2CAP boundary, one layer further out.
    * **We offered far more than the radio can carry.** A client polls at
      500 Hz; an LE peripheral delivers roughly one notification per connection
      event, and the minimum interval the specification allows is 7.5 ms. So
      most reports were queued only to be superseded before the radio looked.

    So this class does what ``L2CAPSink`` already does on the Classic side, for
    the same reasons: keep **only the newest state**, and let a separate worker
    transmit it. The shape is deliberately the same so the two read alike.

    What is different from ``L2CAPSink``, and why
    --------------------------------------------
    The Classic sink writes inline on the datapath because the write is a
    single non-blocking ``send()`` on a socket it owns. We cannot: emitting a
    signal reaches into the bus writer and calls ``loop.add_writer`` and
    ``loop.create_future``, and **asyncio loops are not thread-safe**. Doing
    that from the datapath thread races the loop own selector bookkeeping.

    So the datapath half is reduced to "copy bytes, set a flag, poke the loop"
    -- the poke being ``call_soon_threadsafe``, the one loop method that is
    thread-safe -- and the emit happens on the loop thread, where the binding
    expects to be called.

    On button semantics
    -------------------
    Latest-wins can in principle swallow a press that begins and ends between
    two transmits. It cannot swallow one the console could have seen: we pace
    at ``max_hz`` (default 250 Hz, 4 ms), faster than the fastest legal LE
    connection interval, so any state coalesced away is one the radio would
    never have sampled either. ``states_superseded`` counts them, so if that
    assumption stops holding it is visible rather than silent.
    """

    #: Transmits per second. **This must not exceed what the link drains**, and
    #: that is the whole point of the number rather than a detail of it.
    #:
    #: There is no backpressure on this path. ``emit_properties_changed`` hands
    #: the notification to bluetoothd and returns; bluetoothd and the kernel
    #: accept everything and queue what the radio cannot yet carry. Nothing
    #: reports the depth of that queue -- not our counters, not the D-Bus
    #: writer backlog (measured at 0 throughout), and not RTT, because the
    #: datapath acks the client the moment this call returns. So offering more
    #: than the link drains does not cost throughput, it costs **latency**,
    #: silently and without bound.
    #:
    #: Measured on the reference Pi against an Analogue 3D, by sending for 20 s
    #: and then watching the air with ``btmon`` after the client stopped. What
    #: is still going out at that point is backlog, and it is exactly the lag a
    #: player feels:
    #:
    #:     offered ~500/s, no pacing    117/s on air   3505 late   30.05 s
    #:     paced 250 Hz                 108/s on air    245 late    2.31 s
    #:     paced 110 Hz                  96/s on air      1 late    0.00 s
    #:
    #: The console's connection interval is 7.50 ms (the LE minimum) with a
    #: peripheral latency of 10, which works out to ~110-120 notifications a
    #: second actually delivered. 100 Hz sits under that with margin, and it is
    #: also about what real controller firmware reports at.
    #:
    #: The earlier reasoning here -- that pacing *faster* than the radio is
    #: safe because the extra is harmlessly superseded -- was wrong, and it is
    #: worth stating why: it is true only where the thing below you drops what
    #: it cannot carry. Here it queues instead.
    #:
    #: A console that negotiates a slower interval will need a lower value.
    #: Re-run the drain measurement rather than guessing; the proper fix is
    #: BlueZ's ``AcquireNotify``, which hands back a file descriptor and gives
    #: real ``EAGAIN`` backpressure, the same discipline ``L2CAPSink`` has.
    DEFAULT_MAX_HZ = 100

    #: Outcomes of one attempted socket write.
    _SENT = 0
    _COALESCED = 1
    _NO_SOCKET = 2

    #: Paced ticks to keep waiting on a backed-up bus before abandoning the
    #: drain and letting the next report re-trigger it. Bounded because an
    #: unbounded wait here is a stall with nothing logged, which is the one
    #: outcome worse than dropping a state.
    _STALL_TICKS = 60

    def __init__(self, profile, bd_addr, *, max_hz=DEFAULT_MAX_HZ):
        self._profile = profile
        self._bd_addr = bd_addr
        self._characteristic = None
        self._peer = ""
        self._link_up = False

        #: The report id to strip. HOGP carries it in the Report Reference
        #: descriptor, so it must not also be in the payload -- see
        #: hogp.build_ble_payload, whose logic is inlined into
        #: send_input_report to keep the datapath free of its two allocations.
        self._report_id = profile.descriptor.input_report_id

        #: The newest state, and whether it has been transmitted. Exactly
        #: ``L2CAPSink._tx`` / ``_dirty``: one report in flight, never a stale
        #: one. Guarded because the datapath writes it and the loop reads it.
        self._lock = threading.Lock()
        self._pending = bytearray(64)
        self._pending_len = 0
        self._dirty = False

        #: bluetoothd's notification socket, once it has acquired one.
        #:
        #: When this exists it is the whole transmit path, and it is the only
        #: thing on this transport that pushes back: a write the link cannot
        #: absorb returns EAGAIN, so we coalesce instead of queueing into a
        #: buffer nobody can see. See Characteristic.AcquireNotify.
        self._notify_sock = None
        self._notify_mtu = 0

        #: Guards the socket against being closed underneath a write, and the
        #: pending buffer against the datapath and the emitter at once. Same
        #: role as L2CAPSink._io_lock, and affordable for the same reason: it
        #: wraps a syscall we were already making.
        self._io_lock = threading.Lock()

        #: Set once the peripheral has started, on the loop that owns the bus.
        self._loop = None
        self._bus = None
        self._wake = None
        self._task = None
        self._min_interval = 1.0 / max(1, max_hz)

        #: How many paced ticks to keep waiting on a backed-up bus before
        #: giving up the drain. At the default that is about a quarter second.
        self._stall_logged = False

        self.reports_sent = 0
        self.notify_failures = 0

        #: Offered by the datapath vs actually put on the bus. The gap is
        #: states_superseded, and a healthy saturated link shows a large one:
        #: the newest state still went out on time. Counting a superseded state
        #: as a drop is what made a working link look broken on the Classic
        #: side, so it is deliberately not called one here.
        self.reports_offered = 0
        self.states_superseded = 0

        #: How long the emit itself took, on the loop thread.
        self.notify_stats = LatencyStats()

    @property
    def is_connected(self):
        """True when a host is attached and can be notified.

        Based on the **link**, not on StartNotify having been called. The
        subscription flag looks like the stricter, more honest test and is not:
        for a bonded device the CCCD is persistent, so a console that
        subscribed once never writes it again, the flag stays false after the
        first reconnect, and every report is dropped while the link is live and
        encrypted.
        """
        if self._characteristic is None:
            return False
        return self._link_up or self._characteristic.notifying

    def set_link(self, connected, peer=""):
        """Record that a host attached or left, from the MGMT event stream.

        bluetoothd owns the LE link, so nothing in this module would otherwise
        know: the GATT callbacks only fire for reads, writes and subscription
        changes, none of which happen on a plain reconnect by a bonded host.
        """
        self._link_up = bool(connected)
        if connected:
            self._peer = peer or self._peer
        else:
            self._peer = ""
            # A link that has gone must not leave a state queued for the next
            # one to inherit. Whoever connects next gets fresh input, not the
            # last thing the previous player happened to be holding.
            self._discard_pending()

    @property
    def peer(self):
        return self._peer

    def attach(self, characteristic, peer=""):
        self._characteristic = characteristic
        self._peer = peer
        self._profile.on_connected()

    def detach(self):
        self._characteristic = None
        self._link_up = False
        self.release_notify_socket()
        self._discard_pending()
        peer, self._peer = self._peer, ""
        if peer:
            self._profile.on_disconnected()

    def _discard_pending(self):
        with self._lock:
            self._dirty = False
            self._pending_len = 0

    # -- the notification socket -------------------------------------------

    def attach_notify_socket(self, sock, mtu):
        """Take bluetoothd's notification socket. Called on the loop thread."""
        with self._io_lock:
            self._notify_sock = sock
            self._notify_mtu = mtu
        log.info(
            "%s now writing notifications to bluetoothd's socket (MTU %d) -- "
            "backpressure is available on this link",
            self._bd_addr, mtu,
        )
        self._poke()

    def release_notify_socket(self):
        with self._io_lock:
            self._notify_sock = None
            self._notify_mtu = 0

    def _write_socket_locked(self, payload):
        """Try one write. Caller holds _io_lock. Returns an outcome."""
        sock = self._notify_sock
        if sock is None:
            return BLESink._NO_SOCKET
        try:
            sock.send(payload)
        except BlockingIOError:
            # The link is behind. Keep only the newest state; the emitter
            # sends it when the socket drains. This is the whole point.
            return BLESink._COALESCED
        except OSError as exc:
            # bluetoothd closed its end: the host unsubscribed or the link
            # went. Fall back to the property path rather than losing input.
            log.info(
                "Notification socket on %s closed (%s); falling back",
                self._bd_addr, exc,
            )
            self._notify_sock = None
            return BLESink._NO_SOCKET
        return BLESink._SENT

    # -- the datapath half -------------------------------------------------

    def send_input_report(self, report):
        """Take one report. Called on the datapath thread -- never blocks.

        Does no D-Bus work at all: a bounded copy under an uncontended lock,
        then a wake. Everything expensive happens on the loop thread in
        :meth:`_emit_loop`.

        Returns True when the *state* has been accepted -- either it will be
        transmitted, or it is already superseded by a newer one, which is the
        design rather than a failure.
        """
        if self._characteristic is None:
            return False

        self.reports_offered += 1

        # Strip the report id without allocating. hogp.build_ble_payload does
        # the same thing with bytes(report) plus a slice; both allocations are
        # avoidable here, and this is the one place worth avoiding them.
        offset = (
            1
            if (
                self._report_id is not None
                and len(report)
                and report[0] == self._report_id
            )
            else 0
        )
        length = len(report) - offset

        with self._lock:
            if self._dirty:
                self.states_superseded += 1
            if length > len(self._pending):
                self._pending = bytearray(length)
            self._pending[:length] = report[offset:]
            self._pending_len = length
            self._dirty = True

        self._poke()
        return True

    def _poke(self):
        """Ask the loop to transmit. The only cross-thread call in the path.

        ``call_soon_threadsafe`` is the one event-loop method that may be
        called from another thread. Everything the binding does internally --
        ``add_writer``, ``create_future`` -- may not, which is exactly what the
        old code did, on every report, from the datapath thread.
        """
        loop, wake = self._loop, self._wake
        if loop is None or wake is None:
            return
        if wake.is_set():
            # Already pending. A spurious extra callback would be harmless but
            # this is a 500 Hz path, so not queueing one is worth the check.
            return
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            # Loop closed underneath us during shutdown. Ordinary.
            pass

    # -- the loop half -----------------------------------------------------

    def start_emitter(self, bus):
        """Begin transmitting. Must be called on the loop that owns ``bus``."""
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._bus = bus
        self._wake = asyncio.Event()
        self._task = self._loop.create_task(
            self._emit_loop(), name=f"ble-emit-{self._bd_addr}"
        )
        # A report can arrive between attach and start -- send_input_report
        # accepts it and pokes a loop that is not there yet. Without this the
        # state would sit until the *next* report displaced it, which for a
        # final release means a button held on the console with nothing to say
        # why.
        if self._dirty:
            self._wake.set()

    async def stop_emitter(self):
        """Stop transmitting. Safe to call more than once."""
        task, self._task = self._task, None
        self._loop = None
        self._wake = None
        self._bus = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.debug("BLE emitter for %s ended badly", self._bd_addr, exc_info=True)

    async def _emit_loop(self):
        """Transmit the newest state, at most ``max_hz`` times a second.

        One task per adapter, so a console that stops draining slows only its
        own controller. That is the isolation property this class exists for:
        before it, every adapter reports were marshalled on the single datapath
        thread, and one player traffic delayed every other player.
        """
        while True:
            try:
                await self._wake.wait()
                self._wake.clear()
                await self._drain()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let this task die. It is the only thing that transmits
                # for this adapter, and a dead one leaves a live, encrypted,
                # subscribed link carrying no input at all while every counter
                # and the GUI report a healthy controller -- the exact shape of
                # failure this subsystem keeps producing. Log and carry on.
                log.exception(
                    "BLE emitter for %s hit an unexpected error; continuing",
                    self._bd_addr,
                )
                await asyncio.sleep(self._min_interval)

    async def _drain(self):
        """Send pending states until there are none, pacing as we go."""
        stalled = 0

        while True:
            characteristic = self._characteristic
            if characteristic is None:
                return

            # Backpressure. A non-empty writer queue means our previous message
            # has not reached the bus socket yet, so adding another would only
            # deepen a queue nobody can see. Leaving the state pending is
            # strictly better: the next one supersedes it, and the console gets
            # fresher input when the link drains.
            if self._backlogged():
                stalled += 1
                if stalled > self._STALL_TICKS:
                    # Bounded on purpose. Waiting here forever would be a
                    # silent stall, and the next report re-pokes us anyway --
                    # so give up the drain, say so once, and let the ordinary
                    # path try again.
                    if not self._stall_logged:
                        self._stall_logged = True
                        log.warning(
                            "BLE writer for %s has not drained in %.0f ms; "
                            "input is being held. This is the D-Bus socket to "
                            "bluetoothd backing up, not the radio.",
                            self._bd_addr,
                            self._STALL_TICKS * self._min_interval * 1000,
                        )
                    return
                await asyncio.sleep(self._min_interval)
                continue

            stalled = 0
            self._stall_logged = False

            with self._lock:
                if not self._dirty:
                    return
                payload = bytes(self._pending[: self._pending_len])
                self._dirty = False

            started = now_ns()
            if self._notify_sock is not None:
                # The socket is a cheaper pipe, not a flow-controlled one.
                # Measured: bluetoothd reads it as fast as we write -- 8647
                # writes, **zero** EAGAIN -- and queues downstream, so the
                # backlog is exactly what it was without it (30 s). It still
                # earns its place because a socket write costs no D-Bus
                # marshalling, but it is paced identically. EAGAIN handling
                # stays in case a future BlueZ does push back.
                with self._io_lock:
                    outcome = self._write_socket_locked(memoryview(payload))
                if outcome is BLESink._SENT:
                    self.reports_sent += 1
                elif outcome is BLESink._COALESCED:
                    with self._lock:
                        self._dirty = True      # try again on the next pass
                self.notify_stats.add(ns_to_ms(now_ns() - started))
                if outcome is not BLESink._NO_SOCKET:
                    await asyncio.sleep(self._min_interval)
                    continue
                # Socket went away mid-drain; fall through to the property path.

            try:
                characteristic.notify(payload)
                self.reports_sent += 1
            except Exception:
                self.notify_failures += 1
                log.debug("BLE notify failed on %s", self._bd_addr, exc_info=True)
            self.notify_stats.add(ns_to_ms(now_ns() - started))

            # Pacing is only needed on the property path, which has no
            # backpressure of its own. The socket path returned above.
            await asyncio.sleep(self._min_interval)

    async def _wait_writable(self, timeout=0.25):
        """Wait until bluetoothd can take another notification.

        Bounded, so a wedged socket cannot strand the emitter -- the next
        report re-pokes it either way.
        """
        sock = self._notify_sock
        if sock is None:
            return
        loop = self._loop
        if loop is None:
            return
        waiter = loop.create_future()

        def ready():
            if not waiter.done():
                waiter.set_result(None)

        try:
            loop.add_writer(sock.fileno(), ready)
        except (OSError, ValueError):
            return
        try:
            await asyncio.wait_for(waiter, timeout)
        except (asyncio.TimeoutError, Exception):
            pass
        finally:
            try:
                loop.remove_writer(sock.fileno())
            except (OSError, ValueError):
                pass

    def _backlogged(self):
        bus = self._bus
        if bus is None:
            return False
        return writer_backlog(bus) > 0

    def close(self):
        self.detach()

    # -- reporting ---------------------------------------------------------

    def stats(self):
        """What this sink is doing, for the web GUI and the harness."""
        return {
            "reports_offered": self.reports_offered,
            "reports_sent": self.reports_sent,
            "states_superseded": self.states_superseded,
            "notify_failures": self.notify_failures,
            "notify_ms": self.notify_stats.snapshot(),
            "dbus_backlog": writer_backlog(self._bus) if self._bus else 0,
            # Which transmit path is live. "socket" has backpressure and needs
            # no pacing; "properties" is the fallback and does.
            "notify_path": "socket" if self._notify_sock is not None else "properties",
            "notify_mtu": self._notify_mtu,
            # Has the host actually asked for notifications?
            #
            # **Not a gate**, and deliberately so -- gating on it discarded
            # 32,000 reports from a correctly-subscribed console once already,
            # which is why `notify` ignores it. But it is the one thing that
            # distinguishes "sending into a live link" from "sending into a
            # link bluetoothd is not forwarding", and without it here the two
            # are indistinguishable from every counter above.
            "subscribed": bool(
                self._characteristic is not None
                and getattr(self._characteristic, "notifying", False)
            ),
        }


class BLEPeripheral:
    """One adapter published as a BLE HID gamepad.

    Owns the GATT application, the advertisement, and the sink the router
    writes to. Everything is per adapter, so several of these coexist without
    the system-wide constraint the Classic SDP record imposes.
    """

    def __init__(self, hci_name, profile, identity, *, name=None, bus=None,
                 mgmt=None, index=None, instance=1, bd_addr=""):
        self.hci_name = hci_name
        self.profile = profile
        self.identity = identity
        self.name = name or identity.device_name
        self.bd_addr = bd_addr
        self.sink = BLESink(profile, hci_name)

        self._bus = bus
        self._owns_bus = bus is None
        self._app = None
        self._input_report = None
        self._registered = False

        #: Advertising goes through MGMT, not through bluetoothd.
        #:
        #: ``org.bluez.LEAdvertisingManager1`` cannot publish on this platform:
        #: it takes the extended path (Add Extended Advertising
        #: Parameters/Data) and the kernel rejects the data with Invalid
        #: Parameters -- measured on the built-in adapter and the dongles, with
        #: a minimal advertisement, so it is not our payload at fault. The
        #: legacy single-step Add Advertising accepts the identical bytes.
        #:
        #: The GATT half still goes through bluetoothd, which works.
        self._mgmt = mgmt
        self._index = index if index is not None else _index_of(hci_name)
        self._instance = instance
        self._advertising = False

        #: Set when the **operator** stopped the advertisement, as opposed to
        #: it having been lost. ensure_advertising() must be able to tell those
        #: apart or the reconcile invariant puts back what the operator just
        #: took away, ten seconds later, forever. Cleared by a forced restart.
        self._suppressed = False

        #: The last output report seen, so an identical one is not logged
        #: again. Rumble repeats; a player-LED assignment does not.
        self._last_output_report = None

        #: Whether we are asking to be paired, as opposed to asking a console
        #: we already know to come back. It picks the discoverable flag, and
        #: getting it wrong is silent in the worst way -- see
        #: _start_advertising. Starts True because an unbonded peripheral has
        #: nothing to reconnect to.
        self._pairing_mode = True

    def serial_number(self):
        """The Serial Number characteristic: one per adapter, not one per build.

        Every other Device Information field is deliberately byte-identical
        across adapters -- name, vendor id, product id and model number are
        what a console matches on, and they are copied from the measured pad.
        Serial Number is the one field whose entire purpose is to tell two
        units of the same product apart, and we were sending ``000000000000``
        from all four. A host that keys its controller slots on it sees one pad
        four times.

        Derived from the BD_ADDR rather than generated, so it is stable across
        restarts and follows the physical dongle -- the same property the
        persisted adapter number has, and for the same reason.
        """
        if self.identity.serial_number:
            return self.identity.serial_number

        digits = "".join(c for c in str(self.bd_addr) if c in "0123456789abcdefABCDEF")
        return digits.upper() if digits else "000000000000"

    @property
    def root(self):
        return f"/rbgc/ble/{self.hci_name}"

    @property
    def adapter_path(self):
        return f"/org/bluez/{self.hci_name}"

    async def start(self):
        """Publish the services and begin advertising.

        Raises RuntimeError with an actionable message; the caller decides
        whether a BLE failure should stop the adapter, and it should not --
        the Classic path on the same radio is unaffected.
        """
        from server.bt.ble._dbus import BusType, MessageBus
        from server.bt.ble.hid_service import build_application

        if self._registered:
            return

        if self._bus is None:
            # negotiate_unix_fd is what lets AcquireNotify hand bluetoothd a
            # real file descriptor. Without it dbus-fast marshals the 'h' as a
            # bare index with no SCM_RIGHTS attached, and bluetoothd answers
            # "Invalid AcquirNotify response" -- on its own bus, where we never
            # see it -- then silently falls back to the property path.
            self._bus = await MessageBus(
                bus_type=BusType.SYSTEM, negotiate_unix_fd=True
            ).connect()

        descriptor = self.profile.descriptor
        self._app, self._input_report = build_application(
            self.root,
            descriptor.report_descriptor,
            self.identity.vendor_id,
            self.identity.product_id,
            version=self.identity.version,
            input_report_id=descriptor.input_report_id,
            # Device Information, all five characteristics. A console that
            # wants one we omit does not ask twice -- see hogp.MODEL_NUMBER_UUID.
            manufacturer=self.identity.manufacturer or "RBGC",
            model_number=self.identity.model_number or self.identity.device_name,
            serial_number=self.serial_number(),
            firmware_revision=self.identity.firmware_revision or "1.00",
            on_control_point=self._on_control_point,
            on_protocol_mode=self._on_protocol_mode,
            on_output_report=self._on_output_report,
            on_vendor_write=self._on_vendor_write,
            on_notify_acquired=self.sink.attach_notify_socket,
        )
        self._app.export(self._bus)

        introspection = await self._bus.introspect(BLUEZ, self.adapter_path)
        adapter = self._bus.get_proxy_object(BLUEZ, self.adapter_path, introspection)

        gatt = adapter.get_interface(GATT_MANAGER_IFACE)
        try:
            await gatt.call_register_application(self.root, {})
        except Exception as exc:
            await self._unexport()
            raise RuntimeError(
                f"BlueZ refused the GATT application on {self.hci_name}: {exc}"
            ) from exc

        try:
            self._start_advertising()
        except Exception as exc:
            # The services are registered but nothing can find them. Unwind
            # rather than leave a peripheral that is published and invisible --
            # the failure mode this whole subsystem keeps producing.
            try:
                await gatt.call_unregister_application(self.root)
            except Exception:
                log.debug("Could not unwind the GATT registration", exc_info=True)
            await self._unexport()
            raise RuntimeError(
                f"Could not advertise on {self.hci_name}: {exc}"
            ) from exc

        self._registered = True
        self.sink.attach(self._input_report)

        # One emitter task per adapter, owning this adapter's transmits alone.
        # Started here rather than at attach_sink() because it needs the bus,
        # and because a sink whose emitter never started silently accepts
        # reports and transmits none -- the failure mode this subsystem
        # specialises in.
        self.sink.start_emitter(self._bus)
        log.info(
            "BLE gamepad live on %s as '%s' (HID over GATT, %04X:%04X)",
            self.hci_name, self.name,
            self.identity.vendor_id, self.identity.product_id,
        )

    def _start_advertising(self):
        """Publish the advertisement through our own MGMT socket."""
        if self._mgmt is None:
            raise RuntimeError(
                "no management socket, and bluetoothd cannot advertise on this "
                "platform"
            )
        if self._index < 0:
            raise RuntimeError(f"no adapter index for {self.hci_name}")

        # **Limited discoverable only while pairing. General once bonded.**
        #
        # Limited Discoverable Mode means "I am asking to be paired, now". It
        # is what the real 8BitDo 64 advertises -- and the capture it was
        # copied from was taken with the pad **in pairing mode**, which is the
        # detail that was missed. We advertised it permanently, so a bonded
        # controller asking for its console back still looked like a stranger
        # requesting a new pairing.
        #
        # Measured, one adapter, nothing else on the air: pairing succeeded
        # (6 LTK requests, 0 negative replies, encryption established), then
        # Sleep followed by Wake produced **0 connection attempts in 45 s**
        # with the advertisement verified live. The console will pair with a
        # limited-discoverable device while it is in pairing mode, and ignores
        # one the rest of the time -- which is exactly what the flag is for.
        flags = (
            hogp.ADV_FLAG_CONNECTABLE
            | (
                hogp.ADV_FLAG_LIMITED_DISCOVERABLE
                if self._pairing_mode else
                hogp.ADV_FLAG_DISCOVERABLE
            )
            # The kernel adds the Flags AD structure itself. Ours must not also
            # be there: it rejects the whole advertisement for a duplicate,
            # with the same Invalid Parameters it gives for a length problem.
            | hogp.ADV_FLAG_MANAGED_FLAGS
        )
        # Set the interval **before** adding the instance: the kernel reads
        # these when it builds the advertising parameters, so a value written
        # afterwards would not reach the air until something else restarted
        # the advertisement.
        hogp.set_advertising_interval(self._index)

        # Clear our instance number first, so start-up is idempotent.
        #
        # An instance belongs to the socket that added it and normally dies
        # with that socket -- but not always: a leftover from a crashed run, or
        # from a `btmgmt add-adv` during debugging, survives and makes the add
        # fail with a bare MGMT status 0x03. The adapter then advertises
        # somebody else's data while our peripheral reports itself unpublished,
        # which is the published-and-invisible failure this subsystem keeps
        # producing. Removing something that is not there is free.
        self._mgmt.remove_advertising(self._index, self._instance)

        self._mgmt.add_advertising(
            self._index, self._instance, flags,
            adv_data=hogp.build_advertising_data(self.name),
            scan_response=hogp.build_scan_response(self.name),
        )
        self._advertising = True

    def is_advertising(self):
        """Whether the kernel still carries our advertising instance.

        ``self._advertising`` is only what we *believe*, and it is wrong in the
        one case that matters: an advertising instance does not survive a
        controller power cycle -- a replug, an `hciconfig reset`, or our own
        radio-mode switch -- and nothing reports its loss. The flag stays True
        while the radio is silent, which is the published-and-invisible failure
        this module keeps producing.

        None when the question cannot be answered (no socket, no index, or a
        kernel that refuses the read), so a caller can tell "not advertising"
        from "cannot tell" rather than tearing down a working peripheral over
        a failed read.
        """
        if self._mgmt is None or self._index < 0:
            return None
        try:
            return self._instance in self._mgmt.advertising_instances(self._index)
        except Exception:
            log.debug(
                "Could not read advertising instances on %s",
                self.hci_name, exc_info=True,
            )
            return None

    def attach_sink(self, peer=""):
        """Point the sink back at the report characteristic.

        ``detach`` drops that reference, and Sleep detaches deliberately -- so
        without this a woken controller comes back with a live, authenticated,
        encrypted link that carries **no input at all**. ``send_input_report``
        returns False on a null characteristic and counts nothing, so every
        report is discarded in silence and the GUI shows a connected
        controller doing nothing.

        The characteristic itself is stable for as long as the GATT
        application is registered, which is why re-attaching is enough and the
        peripheral does not have to be rebuilt.
        """
        if self._input_report is not None:
            self.sink.attach(self._input_report, peer)

    def set_pairing_mode(self, pairing):
        """Ask to be paired, or ask a known console to come back.

        Returns True if the mode changed, so the caller knows whether the
        advertisement has to be restarted -- the flag is baked into the
        advertising instance, so changing it means removing and re-adding.
        """
        pairing = bool(pairing)
        if pairing == self._pairing_mode:
            return False
        self._pairing_mode = pairing
        log.info(
            "%s now advertising as %s", self.hci_name,
            "limited discoverable (asking to pair)" if pairing
            else "general discoverable (asking its console to reconnect)",
        )
        return True

    def ensure_advertising(self, force=False):
        """Put the advertisement back if it has gone. Returns True if it is up.

        Read-then-write: the ordinary reconcile costs one MGMT read and writes
        nothing. ``force`` restarts it regardless, which is what Wake and Pair
        do -- and is required after ``set_pairing_mode``, because the flag
        lives in the instance rather than being read at transmit time.
        """
        if not self._registered:
            return False

        if force:
            self._suppressed = False
        else:
            if self._suppressed:
                # Deliberately off. An invariant that cannot tell "lost" from
                # "switched off" fights the operator and always wins.
                return False

            live = self.is_advertising()
            if live is None or live:
                # Unreadable counts as up: a failed read is not evidence the
                # advertisement has gone, and restarting on it would drop a
                # working one every ten seconds.
                return True
            log.warning(
                "%s stopped advertising as '%s' -- a console cannot see it. "
                "An advertising instance does not survive a controller power "
                "cycle. Restarting it.",
                self.hci_name, self.name,
            )

        try:
            self._start_advertising()
        except Exception:
            log.warning(
                "Could not restart advertising on %s", self.hci_name, exc_info=True
            )
            self._advertising = False
            return False
        return True

    def suppress_advertising(self):
        """Take the advertisement down and keep it down until asked otherwise.

        The only way to stay disconnected on this transport. **We are the
        peripheral**: the console is the central, it holds the bond, and it
        reconnects to a bonded controller within a second or two of seeing it
        advertise. So dropping the link alone reads as a button that does
        nothing -- which is exactly what the operator reported.

        Deliberately *not* paired with forgetting the bond. That was the old
        answer to this and it is far worse: a console generally cannot be told
        to forget, so removing our half strands it. See disconnect_host.
        """
        self._suppressed = True
        self._stop_advertising()

    @property
    def suppressed(self):
        """Whether the operator has stopped this adapter advertising."""
        return self._suppressed

    def _stop_advertising(self):
        if self._mgmt is None:
            return
        # Not gated on self._advertising: that is only what we *believe*, and
        # the whole reason is_advertising() exists is that it can be wrong.
        # Removing an instance that is not there is free.
        self._mgmt.remove_advertising(self._index, self._instance)
        self._advertising = False

    async def stop(self):
        """Unregister and stop advertising. Safe to call more than once."""
        if not self._registered:
            await self._unexport()
            return

        await self.sink.stop_emitter()
        self.sink.detach()
        self._stop_advertising()

        try:
            introspection = await self._bus.introspect(BLUEZ, self.adapter_path)
            adapter = self._bus.get_proxy_object(
                BLUEZ, self.adapter_path, introspection
            )
            try:
                gatt = adapter.get_interface(GATT_MANAGER_IFACE)
                await gatt.call_unregister_application(self.root)
            except Exception:
                log.debug("Could not unregister the GATT application", exc_info=True)
        except Exception:
            log.debug("Could not reach %s to unregister", self.hci_name, exc_info=True)

        self._registered = False
        await self._unexport()
        log.info("BLE gamepad on %s stopped", self.hci_name)

    async def _unexport(self):
        """Drop our objects from the bus, and the bus if we opened it."""
        if self._bus is None:
            return
        try:
            if self._app is not None:
                self._app.unexport(self._bus)
        except Exception:
            log.debug("Could not unexport BLE objects", exc_info=True)

        self._app = None
        self._input_report = None

        if self._owns_bus:
            try:
                self._bus.disconnect()
            except Exception:
                pass
            self._bus = None

    # -- host callbacks ----------------------------------------------------

    def _on_control_point(self, data):
        """HID Control Point: suspend / exit suspend.

        0x00 is Suspend and 0x01 Exit Suspend. A host suspends when it stops
        wanting reports; continuing to notify through a suspend wastes the
        radio and, on some hosts, is treated as misbehaviour.
        """
        if not data:
            return
        log.debug(
            "%s HID control point: %s",
            self.hci_name, "suspend" if data[0] == 0x00 else "exit suspend",
        )

    def _on_protocol_mode(self, data):
        """Boot vs report protocol.

        Boot mode exists only for keyboards and mice. A host asking a gamepad
        for it has misidentified us, and the reports it then expects are not
        the ones our descriptor declares -- worth a warning rather than
        silently continuing to send report-mode data.
        """
        if not data:
            return
        if data[0] == hogp.PROTOCOL_MODE_BOOT:
            log.warning(
                "%s was put into boot protocol mode, which has no meaning for a "
                "gamepad. The host has probably misidentified this device; "
                "reports will keep using the report protocol.",
                self.hci_name,
            )

    def _on_vendor_write(self, which, data):
        """Log whatever the console writes to the 8BitDo vendor service.

        The protocol here is proprietary and unknown. Logging at INFO is
        deliberate and temporary: this is the only way to learn what the
        console expects, and a handful of bytes on connect is not a hot path.
        If it turns out the console writes continuously this must drop to
        debug -- it sits on bluetoothd's callback, not ours.
        """
        payload = bytes(data)
        log.info(
            "Vendor write on %s to %s: %s (%d bytes)",
            self.hci_name, which, payload.hex() or "<empty>", len(payload),
        )

    def _on_output_report(self, data):
        """An output report from the host: rumble, player LEDs.

        Handed to the profile, which is the same code the Classic path uses --
        the report bytes are identical between transports.

        **The raw bytes are logged**, like ``_on_vendor_write``, and for the
        same reason: this is the only channel on which a console can tell us
        something, and until now it was handed straight to a profile that may
        ignore it, leaving no trace that anything arrived. That is how the
        player-indicator question stayed unanswerable -- HID standardises
        player LEDs (Usage Page 0x08), so a console may well send one, and we
        could not have seen it.

        Logged once per distinct payload rather than per report: a console that
        sends rumble continuously would otherwise fill the log from a callback
        on bluetoothd's thread.
        """
        payload = bytes(data)
        if payload != self._last_output_report:
            self._last_output_report = payload
            log.info(
                "Output report on %s: %s (%d bytes)",
                self.hci_name, payload.hex() or "<empty>", len(payload),
            )

        try:
            self.profile.on_output_report(payload)
        except Exception:
            log.debug("Output report handling failed", exc_info=True)

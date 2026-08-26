"""L2CAP HID server -- the real Bluetooth path.

Serves the two sockets a Bluetooth HID device must provide:

  * **PSM 17 (control)** -- SET_REPORT/GET_REPORT and the profile handshake
  * **PSM 19 (interrupt)** -- input reports, the actual gameplay data

Each socket is bound to a **specific adapter's BD_ADDR**. That bind is how a
particular USB dongle is selected: with four dongles we run four independent
HID servers, each pinned to its own radio, each pretending to be a separate
controller.

The write path
--------------
Reports are **coalesced, latest-wins**, which is the same discipline the UDP
input path and the video frame assembler already use, finally applied at the
L2CAP boundary too. It matters because the two ends of this path run at
completely different rates: a client polls at up to 500 Hz and sends the instant
anything changes, while the link drains at whatever rate the *console* schedules
-- typically a fraction of that. Writing every arriving packet straight to the
socket, which is what this used to do, builds a queue of stale reports that each
new report has to wait behind. The queue is invisible from every counter: writes
succeed, nothing is dropped, and latency simply grows with how hard the player
is moving the stick.

So: try the write inline on the datapath, exactly as before, because that is the
cheapest thing that can happen and it is what happens whenever the link is
keeping up. If the socket is full, keep only the newest state and let this
adapter's writer thread transmit it when the link drains. One report in flight,
never a stale one, and no scheduling hop in the common case.

The writer thread also **re-sends the current state at ``keepalive_hz``**. That
is not busywork: it is what makes a flushed or lost report self-healing, and it
is a precondition for the tight automatic flush timeout set in
``server/bt/link.py``. Without it, discarding a report would leave the console
holding a stale button state until the player happened to change something else.
Real controller firmware streams continuously for the same reason.

Threading: accept and control-channel handling run on a background thread per
adapter, and each adapter owns one writer thread. The interrupt-channel write
happens inline on the datapath thread, because that write is the last step of
the latency path and handing it to another thread would add a scheduling hop for
no benefit.

Linux-only.
"""

from __future__ import annotations

import errno
import logging
import selectors
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from common.timing import LatencyStats, now_ns, ns_to_ms
from server.bt import link as bt_link
from server.bt.link import LinkPolicy, LinkTuner
from server.bt.profiles.base import TargetProfile
from server.bt.sdp import PSM_CONTROL, PSM_INTERRUPT
from server.bt.sink import HIDSink

log = logging.getLogger(__name__)

#: HID transaction header for a DATA/INPUT report on the interrupt channel.
#: Every input report is prefixed with this byte.
HID_DATA_INPUT = 0xA1

#: Control-channel transaction types (high nibble of the first byte).
_HID_HANDSHAKE = 0x00
_HID_CONTROL = 0x10
_HID_GET_REPORT = 0x40
_HID_SET_REPORT = 0x50
_HID_DATA = 0xA0

_HANDSHAKE_SUCCESSFUL = 0x00
_HANDSHAKE_ERR_UNSUPPORTED = 0x03

#: BlueZ exposes these but Python's socket module does not name them.
BTPROTO_L2CAP = 0

#: Send buffer for the interrupt channel, in bytes.
#:
#: Deliberately tiny. The kernel doubles the request and enforces its own floor,
#: so the effective value is larger than this and must be **measured on the
#: target** rather than assumed -- but asking for a small buffer is what makes
#: ``EAGAIN`` arrive while the backlog is still one or two reports deep. With
#: the default buffer the socket accepts tens of milliseconds of reports before
#: it ever pushes back, and by then the coalescing below has nothing left to
#: save: the stale reports are already committed to the kernel's queue.
INTERRUPT_SNDBUF_BYTES = 2048

#: How long a host may take to open the second channel after the first before we
#: give up on it and close the orphan. The HID profile has the host connect
#: control then interrupt back to back, so this is generous.
_HALF_OPEN_TIMEOUT_S = 5.0


class L2CAPSink(HIDSink):
    """Writes HID input reports to a connected console over L2CAP.

    One of these per adapter. The datapath calls :meth:`send_input_report`
    directly, so that method must never block.
    """

    def __init__(
        self,
        profile: TargetProfile,
        bd_addr: str,
        *,
        policy: LinkPolicy | None = None,
        tuner: LinkTuner | None = None,
    ) -> None:
        self._profile = profile
        self._bd_addr = bd_addr
        self._policy = policy or LinkPolicy()
        self._tuner = tuner

        self._interrupt: socket.socket | None = None
        self._control: socket.socket | None = None
        self._peer: str = ""

        #: Guards every use of the interrupt socket, including closing it.
        #:
        #: This wraps a syscall we were already making, not a queue, so the
        #: uncontended acquire costs tens of nanoseconds against a send of tens
        #: of microseconds. It buys correctness that is otherwise unreachable:
        #: without it ``detach()`` can close the socket while the datapath is
        #: inside ``send()`` on that same descriptor, and if the descriptor
        #: number is reused in between we write a HID report into whatever
        #: unrelated socket now owns it.
        self._io_lock = threading.Lock()

        #: The framed report -- 0xA1 transaction header, then the profile's
        #: bytes -- as one buffer, so header and payload go out in one syscall.
        #: Also *is* the retransmit buffer: a coalesced write and a keepalive
        #: re-send are both "transmit the current contents of this".
        self._tx = bytearray(128)
        self._tx[0] = HID_DATA_INPUT
        self._tx_len = 0

        #: True when ``_tx`` holds a state the console has not been given yet.
        self._dirty = False

        #: Wakes the writer thread when a coalesced report is waiting.
        self._wake = threading.Event()
        self._writer: threading.Thread | None = None
        self._stop = threading.Event()

        self._last_tx_ns = 0

        self.write_stats = LatencyStats()
        self.write_failures = 0

        #: Reports superseded before the link could carry them. This is the
        #: number that says the link is saturated, and it is a healthy sign
        #: rather than an error: the newest state still went out on time.
        #: It used to be counted as a dropped report, which made a link that was
        #: working perfectly look broken.
        self.writes_coalesced = 0

        #: Keepalive re-sends of an unchanged state.
        self.keepalives_sent = 0

        #: What the link actually looked like after tuning, for the web GUI.
        self.link_report = None

    @property
    def is_connected(self) -> bool:
        return self._interrupt is not None

    @property
    def peer(self) -> str:
        return self._peer

    # -- attach / detach ---------------------------------------------------

    def attach(self, control: socket.socket, interrupt: socket.socket, peer: str) -> None:
        """Take ownership of a live link and start the writer thread."""
        self._prepare_interrupt(interrupt)

        with self._io_lock:
            self._control = control
            self._interrupt = interrupt
            self._peer = peer
            self._dirty = False
            self._tx_len = 0
            self._last_tx_ns = now_ns()

        self._profile.on_connected()

        self._stop.clear()
        self._wake.clear()
        self._writer = threading.Thread(
            target=self._writer_loop,
            name=f"hid-writer-{self._bd_addr}",
            daemon=True,
        )
        self._writer.start()

        log.info("HID connection established with %s on %s", peer, self._bd_addr)

    def _prepare_interrupt(self, interrupt: socket.socket) -> None:
        """Apply everything that decides this link's latency, before first use.

        Order matters: the socket options are set while the socket is ours alone
        and before any report can be queued on it, and the link tuning follows
        because it needs the ACL handle, which only exists once connected.
        """
        interrupt.setblocking(False)

        try:
            interrupt.setsockopt(
                socket.SOL_SOCKET, socket.SO_SNDBUF, INTERRUPT_SNDBUF_BYTES
            )
        except OSError as exc:
            # Not fatal -- coalescing still works, it just starts pushing back
            # later, so the backlog it protects against is deeper.
            log.debug("Could not size the interrupt send buffer: %s", exc)

        # Half of a pair: without this the flush timeout below is inert, because
        # non-flushable ACL packets are retransmitted regardless of it. Neither
        # half does anything alone, and the socket option is the half that fails
        # silently. See server/bt/link.py.
        bt_link.set_flushable(interrupt, True)

        if self._tuner is not None:
            handle = bt_link.acl_handle_for(interrupt)
            if handle is None:
                log.debug("No ACL handle for the interrupt channel on %s", self._bd_addr)
            else:
                self.link_report = self._tuner.tune(handle)

    def detach(self) -> None:
        """Tear the link down. Idempotent, and safe against a concurrent write."""
        self._stop.set()
        self._wake.set()

        writer, self._writer = self._writer, None
        if writer is not None and writer is not threading.current_thread():
            writer.join(timeout=1.0)

        with self._io_lock:
            for sock in (self._interrupt, self._control):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self._interrupt = None
            self._control = None
            self._dirty = False
            peer, self._peer = self._peer, ""

        if peer:
            self._profile.on_disconnected()
            log.info("HID connection with %s closed", peer)

    def close(self) -> None:
        self.detach()

    # -- the hot path ------------------------------------------------------

    #: Outcomes of one attempted transmit. Returned rather than acted on inside
    #: the lock, because tearing the link down needs that same lock and
    #: ``threading.Lock`` is not reentrant -- doing it in place would deadlock
    #: the datapath on the first console disconnect.
    _SENT = 0
    _COALESCED = 1
    _FAILED = 2
    _LINK_DEAD = 3

    def send_input_report(self, report: bytes | bytearray | memoryview) -> bool:
        """Offer one report to the console. Called on the datapath -- never blocks.

        Returns True when the *state* has been accepted: either it went out on
        the wire, or it is the newest state and the writer thread will transmit
        it as soon as the link drains. A superseded report is not a failure --
        it is the design, and counting it as a drop is what made a saturated but
        perfectly healthy link look broken.
        """
        outcome = self._offer(report)

        if outcome is L2CAPSink._LINK_DEAD:
            log.warning("Console dropped the HID link on %s", self._bd_addr)
            self.detach()
            return False
        return outcome is not L2CAPSink._FAILED

    def _offer(self, report: bytes | bytearray | memoryview) -> int:
        length = len(report)

        with self._io_lock:
            sock = self._interrupt
            if sock is None:
                return L2CAPSink._FAILED

            if length + 1 > len(self._tx):
                self._tx = bytearray(length + 1)
                self._tx[0] = HID_DATA_INPUT
            self._tx[1 : length + 1] = report
            self._tx_len = length + 1
            self._dirty = True

            outcome = self._transmit_locked(sock)

        if outcome is L2CAPSink._COALESCED:
            # The link is behind. Ask the writer to push the newest state out
            # the moment the socket has room, rather than waiting for the next
            # input packet to try again.
            self._wake.set()

        return outcome

    def _transmit_locked(self, sock: socket.socket) -> int:
        """Write the current contents of ``_tx``. Caller holds ``_io_lock``."""
        start = now_ns()
        try:
            sock.send(memoryview(self._tx)[: self._tx_len])
        except BlockingIOError:
            # The radio's transmit queue is full. The state stays in ``_tx`` and
            # the next one overwrites it: the console gets the newest state a
            # moment later rather than a backlog of stale ones.
            self.writes_coalesced += 1
            return L2CAPSink._COALESCED
        except OSError as exc:
            if exc.errno in (errno.ECONNRESET, errno.EPIPE, errno.ENOTCONN, errno.EBADF):
                return L2CAPSink._LINK_DEAD
            log.warning("HID write failed on %s: %s", self._bd_addr, exc)
            self.write_failures += 1
            return L2CAPSink._FAILED

        self._dirty = False
        self._last_tx_ns = now_ns()
        self.write_stats.add(ns_to_ms(self._last_tx_ns - start))
        return L2CAPSink._SENT

    # -- writer thread -----------------------------------------------------

    def _writer_loop(self) -> None:
        """Flush coalesced reports, and keep the current state fresh.

        Two jobs, and they are the same operation: transmit whatever ``_tx``
        currently holds.

        * **Coalesced flush.** The datapath hit ``EAGAIN``, so the newest state
          is sitting in ``_tx`` unsent. Wait for the socket to become writable
          and send it.
        * **Keepalive.** Nothing has changed for a while, so re-send the state
          anyway. This is what makes a lost or flushed report self-healing --
          without it a report discarded by the automatic flush timeout would
          leave the console holding a stale button until the player changed
          something else, which is a far worse failure than the jitter the
          flush timeout exists to prevent. It also denies the peer an idle
          period to park the link in.
        """
        interval = self._policy.keepalive_interval_s

        while not self._stop.is_set():
            sock = self._interrupt
            if sock is None:
                break

            if self._dirty:
                # Block until there is room, but bounded, so a wedged link
                # cannot strand this thread and the keepalive still runs.
                self._wait_writable(sock, min(interval or 0.05, 0.05))
            else:
                if interval <= 0:
                    self._wake.wait(timeout=0.1)
                    self._wake.clear()
                    continue
                due = self._last_tx_ns + int(interval * 1e9)
                delay = (due - now_ns()) / 1e9
                if delay > 0:
                    self._wake.wait(timeout=delay)
                    self._wake.clear()
                    if self._stop.is_set():
                        break

            self._pump(keepalive=not self._dirty)

    def _pump(self, *, keepalive: bool) -> None:
        """One transmit attempt from the writer thread."""
        if keepalive and self._policy.keepalive_interval_s <= 0:
            return

        with self._io_lock:
            sock = self._interrupt
            if sock is None or self._tx_len == 0:
                return
            if keepalive:
                # Nothing new to say. Only re-send once the interval has
                # genuinely elapsed -- a spurious wake must not turn the
                # keepalive into a spin.
                due = self._last_tx_ns + int(self._policy.keepalive_interval_s * 1e9)
                if now_ns() < due:
                    return
            outcome = self._transmit_locked(sock)

        if outcome is L2CAPSink._SENT and keepalive:
            self.keepalives_sent += 1
        elif outcome is L2CAPSink._LINK_DEAD:
            log.warning("Console dropped the HID link on %s", self._bd_addr)
            self.detach()

    @staticmethod
    def _wait_writable(sock: socket.socket, timeout: float) -> None:
        """Wait for room in the transmit queue. Never raises."""
        try:
            with selectors.DefaultSelector() as sel:
                sel.register(sock, selectors.EVENT_WRITE)
                sel.select(timeout=timeout)
        except (OSError, ValueError, KeyError):
            # The socket was closed underneath us -- ordinary at teardown.
            time.sleep(min(timeout, 0.01))

    # -- control channel ---------------------------------------------------

    def send_control(self, data: bytes) -> bool:
        """Write to the control channel. Off the hot path."""
        sock = self._control
        if sock is None:
            return False
        try:
            sock.send(data)
            return True
        except OSError as exc:
            log.debug("Control write failed on %s: %s", self._bd_addr, exc)
            return False

    # -- reporting ---------------------------------------------------------

    def stats(self) -> dict[str, object]:
        """What this link is doing, for the web GUI and the probe tool."""
        return {
            "connected": self.is_connected,
            "peer": self._peer,
            "write_ms": self.write_stats.snapshot(),
            "write_failures": self.write_failures,
            "writes_coalesced": self.writes_coalesced,
            "keepalives_sent": self.keepalives_sent,
            "link": self.link_report.snapshot() if self.link_report else None,
        }


#: Reconnect backoff, in seconds. A host that is switched off should not be
#: hammered, but a host that just rebooted should be picked up quickly.
_RECONNECT_DELAYS = (2.0, 2.0, 4.0, 8.0, 15.0, 30.0)

#: Consecutive failures before saying so out loud -- about five minutes at the
#: 30 s ceiling. Long enough that a reboot or a quick power cycle stays quiet.
_RECONNECT_COMPLAIN_AFTER = 10

#: Per-PSM timeout for an outgoing connect. Long enough for a host that is
#: awake but slow to answer, short enough that a powered-off host does not
#: stall the retry loop.
_CONNECT_TIMEOUT_S = 8.0


@dataclass(slots=True)
class _HalfOpen:
    """One side of a connection whose other side has not arrived yet.

    The HID profile has the host open control and then interrupt, so a lone
    socket is normal for a few milliseconds and abnormal for longer. Holding
    them here with a deadline is what stops a host that opens control and
    vanishes from wedging the adapter -- which is exactly what a blocking
    ``accept()`` on the second listener used to do, permanently and with nothing
    logged.
    """

    deadline: float
    control: socket.socket | None = None
    interrupt: socket.socket | None = None

    @property
    def complete(self) -> bool:
        return self.control is not None and self.interrupt is not None

    def close(self) -> None:
        _close_quietly(self.control, self.interrupt)
        self.control = None
        self.interrupt = None


class HIDServer:
    """Serves one adapter's HID connection, in both directions.

    Two ways a link comes up:

    * **Incoming** -- the host connects to our PSM 17/19 listeners. This is what
      happens during pairing.
    * **Outgoing** -- we connect to the host's PSM 17/19. This is how a real
      controller reconnects after either end restarts, and it is the only
      option here: ``bluetoothctl connect`` fails with
      ``br-connection-profile-unavailable`` because we run bluetoothd with
      ``--noplugin=input``, so BlueZ has no HID profile to connect *with*.
      Doing it ourselves over raw L2CAP sidesteps BlueZ entirely, and is
      exactly what the ``HIDReconnectInitiate`` SDP attribute advertises.

    Both paths converge on :meth:`_serve_session`.
    """

    def __init__(
        self,
        bd_addr: str,
        profile: TargetProfile,
        sink: L2CAPSink,
        *,
        on_host_connected: "Callable[[str], None] | None" = None,
        on_host_disconnected: "Callable[[str], None] | None" = None,
        on_rumble: "Callable[[str, object], None] | None" = None,
    ) -> None:
        self._bd_addr = bd_addr
        self._profile = profile
        self._sink = sink
        self._on_host_connected = on_host_connected

        #: Called when a session ends, however it ended. Without this the only
        #: signal that a console had gone was the sink reporting itself
        #: disconnected, which nothing was watching -- so an adapter stayed
        #: shown as LINKED long after the host had vanished.
        self._on_host_disconnected = on_host_disconnected

        #: Called with (bd_addr, RumbleCommand) when the console asks for
        #: rumble. Fired from this adapter's control thread, off the hot path.
        self._on_rumble = on_rumble

        self._control_listener: socket.socket | None = None
        self._interrupt_listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._reconnect_thread: threading.Thread | None = None
        self._stop = threading.Event()

        #: Host to reconnect to, learned on connect and restored from config.
        self._reconnect_target: str | None = None

        #: Serializes the two paths so an incoming and an outgoing connection
        #: cannot both attach at once.
        self._session_lock = threading.Lock()

        #: Wakes the reconnect loop early -- on disconnect, or on shutdown.
        self._retry_now = threading.Event()

        #: Monotonic deadline until which outgoing pages are held off, to keep
        #: them off a radio that is supposed to be listening for the console.
        #: A *deadline* rather than a flag because a pairing window expires on
        #: its own timer -- nobody calls back to say it ended, and a flag would
        #: have left reconnect suspended for the life of the process.
        self._reconnect_suspended_until = 0.0

        #: Consecutive failed reconnects, so a host that is refusing us can be
        #: reported once instead of silently retried forever at debug level.
        self._reconnect_failures = 0

    def suspend_reconnect(self, duration_s: float) -> None:
        """Hold off outgoing connection attempts for ``duration_s``.

        Used for the length of a pairing window. Reconnecting and pairing both
        want the radio: an outgoing page is a transmit, and this adapter is
        meant to be listening for the console.

        Time-bounded on purpose. A pairing window ends by timeout as often as
        by the operator pressing Stop, so a suspension that needed clearing by
        hand would silently become permanent.
        """
        if duration_s > 0:
            self._reconnect_suspended_until = time.monotonic() + duration_s
            return

        self._reconnect_suspended_until = 0.0
        # Do not wait out the backoff before resuming -- a host that was
        # waiting for us should be picked up immediately.
        self._retry_now.set()

    def set_reconnect_target(self, host_bd_addr: str | None) -> None:
        """Remember which host to reconnect to, and try immediately.

        Called with the persisted address at startup, and again whenever a host
        connects, so the target survives a restart of either end.
        """
        if host_bd_addr:
            host_bd_addr = host_bd_addr.upper()
        if host_bd_addr == self._reconnect_target:
            return

        self._reconnect_target = host_bd_addr
        if host_bd_addr:
            log.info("Reconnect target for %s is %s", self._bd_addr, host_bd_addr)
            self._retry_now.set()

    @property
    def reconnect_target(self) -> str | None:
        return self._reconnect_target

    def start(self) -> None:
        """Bind both PSMs and start accepting.

        Raises OSError with an actionable message on the two common failures:
        missing privileges and the BlueZ input plugin holding the PSMs.
        """
        # Every failure path below releases whatever already bound. Without it a
        # failure on the *second* PSM left the first socket bound for the life
        # of the process: nothing owned it, stop() was never called, and it held
        # PSM 17 so no retry could rebind. Worse, the half-open adapter went on
        # advertising, so a host would find a controller whose interrupt channel
        # could never connect -- which is exactly what it looked like from
        # Windows: pairs, then "Try connecting your device again".
        try:
            self._control_listener = self._listen(PSM_CONTROL)
            self._interrupt_listener = self._listen(PSM_INTERRUPT)
        except PermissionError as exc:
            self._release_listeners()
            raise PermissionError(
                f"Permission denied binding L2CAP on {self._bd_addr}. "
                "Run as root, or grant CAP_NET_RAW+CAP_NET_BIND_SERVICE:\n"
                "  sudo setcap 'cap_net_raw,cap_net_bind_service+eip' $(readlink -f $(which python3))"
            ) from exc
        except OSError as exc:
            self._release_listeners()
            if exc.errno == errno.EADDRINUSE:
                raise OSError(
                    f"L2CAP PSM {PSM_CONTROL}/{PSM_INTERRUPT} already in use on "
                    f"{self._bd_addr}. Two causes, and they look identical:\n"
                    "  1. Another HID server is already bound to this adapter -- "
                    "check whether one of ours is still running.\n"
                    "  2. bluetoothd's input plugin has claimed the HID role. "
                    "Restart it with --noplugin=input "
                    "(see server/bt/sdp.py:check_bluetooth_daemon)."
                ) from exc
            raise

        self._stop.clear()
        self._retry_now.clear()

        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name=f"hid-accept-{self._bd_addr}",
            daemon=True,
        )
        self._accept_thread.start()

        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop,
            name=f"hid-reconnect-{self._bd_addr}",
            daemon=True,
        )
        self._reconnect_thread.start()

        log.info("HID server listening on %s (PSM %d/%d)", self._bd_addr,
                 PSM_CONTROL, PSM_INTERRUPT)

    def stop(self) -> None:
        self._stop.set()
        self._retry_now.set()          # unblock the reconnect loop's wait

        self._release_listeners()
        self._sink.detach()

        for thread in (self._accept_thread, self._reconnect_thread):
            if thread is not None:
                thread.join(timeout=3.0)
        self._accept_thread = None
        self._reconnect_thread = None

    def _release_listeners(self) -> None:
        """Close both listening sockets, whichever exist.

        Shared by ``stop()`` and by ``start()``'s failure paths, because a
        half-bound server is exactly as damaging as a stopped one that never
        let go: the surviving socket keeps its PSM and blocks every retry.
        """
        for sock in (self._control_listener, self._interrupt_listener):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._control_listener = None
        self._interrupt_listener = None

    def _listen(self, psm: int) -> socket.socket:
        """Bind a listening L2CAP socket to this adapter's BD_ADDR.

        Binding to the specific address (rather than BDADDR_ANY) is what pins
        this HID server to one physical dongle.
        """
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._bd_addr, psm))
        # A short backlog rather than 1: a host that retries a connection while
        # we are still pairing up the previous attempt should queue rather than
        # be refused.
        sock.listen(2)
        sock.setblocking(False)
        return sock

    def _accept_loop(self) -> None:
        """Accept incoming connections, pairing the two channels by peer.

        The HID profile has the host connect control first, then interrupt, and
        the link is only up once both are. Two things about that used to be
        wrong, and both were silent:

        * Accepting control and then **blocking** on the interrupt listener
          meant a host that opened control and went away wedged this loop
          forever. Nothing else could connect on that adapter for the life of
          the process, and no log line said so.
        * The interrupt peer address was discarded, so a *second* host opening
          interrupt while the first was mid-connect got spliced onto the first
          host's control channel.

        Both are answered by never blocking on an accept: sockets are collected
        as they arrive, filed under the peer that opened them, and a session
        starts only when one peer has supplied both. Halves that never find
        their partner are closed on a deadline.
        """
        assert self._control_listener and self._interrupt_listener

        half_open: dict[str, _HalfOpen] = {}

        try:
            with selectors.DefaultSelector() as sel:
                sel.register(self._control_listener, selectors.EVENT_READ, PSM_CONTROL)
                sel.register(self._interrupt_listener, selectors.EVENT_READ, PSM_INTERRUPT)

                while not self._stop.is_set():
                    for key, _ in sel.select(timeout=0.5):
                        self._accept_one(key.fileobj, key.data, half_open)

                    self._expire_half_open(half_open)

                    ready = self._take_complete(half_open)
                    if ready is not None:
                        peer, pair = ready
                        self._serve_session(
                            pair.control, pair.interrupt, peer, incoming=True
                        )
        except OSError as exc:
            if not self._stop.is_set():
                log.warning("Accept loop failed on %s: %s", self._bd_addr, exc)
        finally:
            for pair in half_open.values():
                pair.close()

    def _accept_one(
        self, listener: socket.socket, psm: int, half_open: dict[str, _HalfOpen]
    ) -> None:
        """Take one pending connection and file it under its peer."""
        try:
            sock, addr = listener.accept()
        except BlockingIOError:
            return          # spurious readiness; nothing queued after all
        except OSError as exc:
            if not self._stop.is_set():
                log.warning("Accept failed on %s: %s", self._bd_addr, exc)
            return

        peer = str(addr[0]).upper()
        pair = half_open.get(peer)
        if pair is None:
            pair = _HalfOpen(deadline=time.monotonic() + _HALF_OPEN_TIMEOUT_S)
            half_open[peer] = pair

        if psm == PSM_CONTROL:
            existing, pair.control = pair.control, sock
            log.info("Incoming control channel from %s on %s", peer, self._bd_addr)
        else:
            existing, pair.interrupt = pair.interrupt, sock
            log.debug("Incoming interrupt channel from %s on %s", peer, self._bd_addr)

        if existing is not None:
            # The same peer reopened a channel it already had pending. The new
            # socket is the one it will use; the old one is abandoned.
            _close_quietly(existing)

    @staticmethod
    def _take_complete(
        half_open: dict[str, _HalfOpen]
    ) -> tuple[str, _HalfOpen] | None:
        """Remove and return the first peer that has supplied both channels."""
        peer = next((p for p, pair in half_open.items() if pair.complete), None)
        if peer is None:
            return None
        return peer, half_open.pop(peer)

    def _expire_half_open(self, half_open: dict[str, _HalfOpen]) -> None:
        """Close connections whose other half never arrived."""
        now = time.monotonic()
        for peer in [p for p, pair in half_open.items() if now >= pair.deadline]:
            pair = half_open.pop(peer)
            which = "control" if pair.control is not None else "interrupt"
            log.info(
                "%s opened only the %s channel on %s and never completed the "
                "connection; closing it",
                peer, which, self._bd_addr,
            )
            pair.close()

    def _reconnect_loop(self) -> None:
        """Reconnect to the remembered host after either end restarts.

        This is what makes the link come back on its own. BlueZ cannot do it
        for us -- with the input plugin disabled it has no HID profile, and
        ``ConnectProfile`` fails with ``br-connection-profile-unavailable`` --
        so we open the L2CAP channels ourselves, exactly as real controller
        firmware does.
        """
        attempt = 0

        while not self._stop.is_set():
            # Wait out the backoff. The event lets a disconnect or a freshly
            # learned target cut the wait short.
            delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
            self._retry_now.wait(timeout=delay)
            self._retry_now.clear()

            if self._stop.is_set():
                return

            remaining = self._reconnect_suspended_until - time.monotonic()
            if remaining > 0:
                # A pairing window is open. Paging out competes with inquiry
                # and page scan on this same radio -- and _connect binds to
                # this adapter's BD_ADDR precisely so it leaves on this dongle,
                # the one the console is trying to reach. Sit the window out
                # rather than waking every couple of seconds to do nothing.
                self._retry_now.wait(timeout=remaining)
                self._retry_now.clear()
                continue

            target = self._reconnect_target
            if not target or self._sink.is_connected:
                # Nothing to do, or the host beat us to it. Reset the backoff so
                # the next real outage starts fast.
                attempt = 0
                continue

            control = interrupt = None
            try:
                log.debug("Reconnect attempt %d to %s from %s",
                          attempt + 1, target, self._bd_addr)
                control = self._connect(target, PSM_CONTROL)
                interrupt = self._connect(target, PSM_INTERRUPT)
            except OSError as exc:
                _close_quietly(control, interrupt)
                attempt += 1
                self._reconnect_failures += 1
                # Expected while the host is off or asleep; debug, not warning,
                # or the log fills with noise overnight.
                log.debug("Reconnect to %s failed: %s", target, exc)
                if self._reconnect_failures == _RECONNECT_COMPLAIN_AFTER:
                    # Said once, then back to debug. A host that is merely off
                    # looks identical to one that has deleted our link key, and
                    # the second case used to be invisible: we paged it every
                    # 30 s indefinitely with nothing in the log to say so.
                    log.warning(
                        "%s has refused %d reconnect attempts from %s. If the host "
                        "forgot this controller, enter pairing mode to clear the "
                        "stale bond -- we cannot tell that apart from a host that "
                        "is simply switched off.",
                        target, self._reconnect_failures, self._bd_addr,
                    )
                continue

            log.info("Reconnected to %s from %s", target, self._bd_addr)
            attempt = 0
            self._reconnect_failures = 0
            self._serve_session(control, interrupt, target, incoming=False)

    def _connect(self, host_bd_addr: str, psm: int) -> socket.socket:
        """Open an outgoing L2CAP channel to ``host_bd_addr``.

        Bound to our own adapter's BD_ADDR first so that, with several dongles
        present, the connection leaves on the right radio. Local PSM 0 means
        "any free one" -- only the destination PSM is fixed.
        """
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP)
        try:
            sock.bind((self._bd_addr, 0))
            sock.settimeout(_CONNECT_TIMEOUT_S)
            sock.connect((host_bd_addr, psm))
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
            raise
        return sock

    def _serve_session(
        self, control: socket.socket, interrupt: socket.socket, peer: str, *, incoming: bool
    ) -> None:
        """Run one connected session to completion, from either direction."""
        # Only one session at a time: an incoming connection and a reconnect
        # attempt can otherwise race and both try to attach.
        if not self._session_lock.acquire(blocking=False):
            log.debug("Session already active on %s; dropping duplicate", self._bd_addr)
            _close_quietly(control, interrupt)
            return

        try:
            # attach() applies the socket options and the link tuning that
            # decide this connection's latency, and starts the writer thread.
            # It has to happen before any report can be offered.
            self._sink.attach(control, interrupt, peer)

            # Remember who this was, so we can reconnect next time. Doing it for
            # incoming connections too is the point -- that is how we learn the
            # address in the first place.
            self.set_reconnect_target(peer)
            if incoming and self._on_host_connected:
                try:
                    self._on_host_connected(peer)
                except Exception:
                    log.exception("on_host_connected callback failed")

            self._serve_control(control)
        finally:
            self._sink.detach()
            if self._on_host_disconnected:
                try:
                    self._on_host_disconnected(peer)
                except Exception:
                    log.exception("on_host_disconnected callback failed")
            self._session_lock.release()
            # Try to get the link back promptly rather than waiting out a full
            # backoff step.
            self._retry_now.set()

    def _serve_control(self, control: socket.socket) -> None:
        """Handle control-channel traffic until the console disconnects.

        Runs on this adapter's own thread, off the hot path -- control traffic
        is pairing handshakes and the occasional rumble, not gameplay input.
        """
        control.setblocking(True)
        control.settimeout(1.0)

        while not self._stop.is_set():
            try:
                data = control.recv(1024)
            except TimeoutError:
                continue
            except OSError:
                return

            if not data:
                return  # console closed the link

            try:
                self._handle_control_message(data)
            except Exception:
                log.exception("Error handling HID control message on %s", self._bd_addr)

    def _extract_rumble(self, payload: bytes) -> None:
        """Forward any rumble in an output report to the datapath.

        Never raises: a malformed or unrecognised output report must not
        disturb the HID link. Rumble is a nice-to-have; the controller working
        is not.
        """
        if self._on_rumble is None:
            return
        try:
            command = self._profile.extract_rumble(payload)
        except Exception:
            log.debug("Rumble extraction failed on %s", self._bd_addr, exc_info=True)
            return

        if command is not None:
            self._on_rumble(self._bd_addr, command)

    def _handle_control_message(self, data: bytes) -> None:
        transaction = data[0] & 0xF0

        if transaction == _HID_GET_REPORT:
            # The host is asking us for a report. Profiles that need this
            # (Switch) answer via on_output_report; others get a polite
            # "unsupported" rather than silence, which some hosts wait on.
            response = self._profile.on_output_report(data[1:])
            if response:
                self._sink.send_control(bytes([_HID_DATA | 0x01]) + response)
            else:
                self._sink.send_control(bytes([_HANDSHAKE_ERR_UNSUPPORTED]))

        elif transaction == _HID_SET_REPORT:
            self._extract_rumble(data[1:])
            response = self._profile.on_output_report(data[1:])
            self._sink.send_control(bytes([_HANDSHAKE_SUCCESSFUL]))
            if response:
                self._sink.send_input_report(response)

        elif transaction == _HID_DATA:
            # Output report from the console -- rumble, LEDs, and for the
            # Switch the whole subcommand handshake.
            self._extract_rumble(data[1:])
            response = self._profile.on_output_report(data[1:])
            if response:
                self._sink.send_input_report(response)

        elif transaction == _HID_CONTROL:
            # Suspend / exit-suspend / virtual-cable-unplug.
            if (data[0] & 0x0F) == 0x05:  # VIRTUAL_CABLE_UNPLUG
                log.info("Console unplugged the virtual cable on %s", self._bd_addr)
                self._sink.detach()

        elif transaction == _HID_HANDSHAKE:
            pass  # acknowledgement; nothing to do

        else:
            log.debug("Unhandled HID transaction 0x%02x on %s", data[0], self._bd_addr)


def _close_quietly(*socks: socket.socket | None) -> None:
    """Close sockets, ignoring errors. Used on partially-established links."""
    for sock in socks:
        if sock is None:
            continue
        try:
            sock.close()
        except OSError:
            pass


def is_supported() -> bool:
    """True if this platform can do L2CAP Bluetooth sockets at all."""
    return hasattr(socket, "AF_BLUETOOTH")

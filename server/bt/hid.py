"""L2CAP HID server -- the real Bluetooth path.

Serves the two sockets a Bluetooth HID device must provide:

  * **PSM 17 (control)** -- SET_REPORT/GET_REPORT and the profile handshake
  * **PSM 19 (interrupt)** -- input reports, the actual gameplay data

Each socket is bound to a **specific adapter's BD_ADDR**. That bind is how a
particular USB dongle is selected: with four dongles we run four independent
HID servers, each pinned to its own radio, each pretending to be a separate
controller.

Threading: accept and control-channel handling run on a background thread per
adapter. The interrupt-channel *write* happens inline on the datapath thread,
because that write is the last step of the latency path and handing it to
another thread would add a scheduling hop for no benefit.

Linux-only.
"""

from __future__ import annotations

import errno
import logging
import socket
import threading
import time
from collections.abc import Callable

from common.timing import LatencyStats, now_ns, ns_to_ms
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
SOL_L2CAP = 6
L2CAP_OPTIONS = 0x01


class L2CAPSink(HIDSink):
    """Writes HID input reports to a connected console over L2CAP.

    One of these per adapter. The datapath calls :meth:`send_input_report`
    directly, so that method must never block.
    """

    def __init__(self, profile: TargetProfile, bd_addr: str) -> None:
        self._profile = profile
        self._bd_addr = bd_addr

        self._interrupt: socket.socket | None = None
        self._control: socket.socket | None = None
        self._peer: str = ""

        #: Prefixed to every report, so we can write header+payload in one
        #: syscall instead of two.
        self._tx = bytearray(128)
        self._tx[0] = HID_DATA_INPUT

        self._lock = threading.Lock()
        self.write_stats = LatencyStats()
        self.write_failures = 0

    @property
    def is_connected(self) -> bool:
        return self._interrupt is not None

    @property
    def peer(self) -> str:
        return self._peer

    def attach(self, control: socket.socket, interrupt: socket.socket, peer: str) -> None:
        with self._lock:
            self._control = control
            self._interrupt = interrupt
            self._peer = peer
        self._profile.on_connected()
        log.info("HID connection established with %s on %s", peer, self._bd_addr)

    def detach(self) -> None:
        with self._lock:
            for sock in (self._interrupt, self._control):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self._interrupt = None
            self._control = None
            peer, self._peer = self._peer, ""

        if peer:
            self._profile.on_disconnected()
            log.info("HID connection with %s closed", peer)

    def send_input_report(self, report: bytes | bytearray | memoryview) -> bool:
        """Write one input report. Called on the datapath -- must not block."""
        sock = self._interrupt
        if sock is None:
            return False

        length = len(report)
        if length + 1 > len(self._tx):
            self._tx = bytearray(length + 1)
            self._tx[0] = HID_DATA_INPUT

        self._tx[1 : length + 1] = report

        start = now_ns()
        try:
            sock.send(memoryview(self._tx)[: length + 1])
        except BlockingIOError:
            # The radio's transmit queue is full. Dropping is correct: the next
            # state supersedes this one, and blocking would stall every other
            # controller sharing this thread.
            self.write_failures += 1
            return False
        except OSError as exc:
            if exc.errno in (errno.ECONNRESET, errno.EPIPE, errno.ENOTCONN):
                log.warning("Console dropped the HID link on %s", self._bd_addr)
                self.detach()
            else:
                log.warning("HID write failed on %s: %s", self._bd_addr, exc)
            self.write_failures += 1
            return False

        self.write_stats.add(ns_to_ms(now_ns() - start))
        return True

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

    def close(self) -> None:
        self.detach()


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
        on_rumble: "Callable[[str, object], None] | None" = None,
    ) -> None:
        self._bd_addr = bd_addr
        self._profile = profile
        self._sink = sink
        self._on_host_connected = on_host_connected

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
        sock.listen(1)
        return sock

    def _accept_loop(self) -> None:
        """Accept incoming connections from a host.

        The HID profile has the host connect control first, then interrupt. We
        accept in that order and only consider the link up once both are.
        """
        assert self._control_listener and self._interrupt_listener

        while not self._stop.is_set():
            control = interrupt = None
            try:
                control, control_addr = self._control_listener.accept()
                peer = control_addr[0]
                log.info("Incoming control channel from %s on %s", peer, self._bd_addr)

                interrupt, _ = self._interrupt_listener.accept()
                self._serve_session(control, interrupt, peer, incoming=True)

            except OSError as exc:
                _close_quietly(control, interrupt)
                if self._stop.is_set():
                    return
                log.warning("Accept failed on %s: %s", self._bd_addr, exc)

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
            # Non-blocking writes: a stalled radio must never block the
            # datapath thread that calls send_input_report().
            interrupt.setblocking(False)
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
            self._session_lock.release()
            # Try to get the link back promptly rather than waiting out a full
            # backoff step.
            self._retry_now.set()

    def _serve_control(self, control: socket.socket) -> None:
        """Handle control-channel traffic until the console disconnects.

        Runs on this adapter's own thread, off the hot path -- control traffic
        is pairing handshakes and the occasional rumble, not gameplay input.
        """
        control.settimeout(1.0)

        while not self._stop.is_set():
            try:
                data = control.recv(1024)
            except socket.timeout:
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

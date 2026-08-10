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


class HIDServer:
    """Accepts a console's L2CAP connections for one adapter."""

    def __init__(self, bd_addr: str, profile: TargetProfile, sink: L2CAPSink) -> None:
        self._bd_addr = bd_addr
        self._profile = profile
        self._sink = sink

        self._control_listener: socket.socket | None = None
        self._interrupt_listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        """Bind both PSMs and start accepting.

        Raises OSError with an actionable message on the two common failures:
        missing privileges and the BlueZ input plugin holding the PSMs.
        """
        try:
            self._control_listener = self._listen(PSM_CONTROL)
            self._interrupt_listener = self._listen(PSM_INTERRUPT)
        except PermissionError as exc:
            raise PermissionError(
                f"Permission denied binding L2CAP on {self._bd_addr}. "
                "Run as root, or grant CAP_NET_RAW+CAP_NET_BIND_SERVICE:\n"
                "  sudo setcap 'cap_net_raw,cap_net_bind_service+eip' $(readlink -f $(which python3))"
            ) from exc
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise OSError(
                    f"L2CAP PSM {PSM_CONTROL}/{PSM_INTERRUPT} already in use on "
                    f"{self._bd_addr}. This almost always means bluetoothd's input "
                    "plugin has claimed the HID role.\n"
                    "  Restart bluetoothd with --noplugin=input "
                    "(see server/bt/sdp.py:check_bluetooth_daemon)."
                ) from exc
            raise

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._accept_loop,
            name=f"hid-{self._bd_addr}",
            daemon=True,
        )
        self._thread.start()
        log.info("HID server listening on %s (PSM %d/%d)", self._bd_addr,
                 PSM_CONTROL, PSM_INTERRUPT)

    def stop(self) -> None:
        self._stop.set()
        for sock in (self._control_listener, self._interrupt_listener):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._control_listener = None
        self._interrupt_listener = None
        self._sink.detach()

        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

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
        """Accept control then interrupt, then serve the control channel.

        The HID profile has the host connect control first, then interrupt.
        We accept in that order and only consider the device connected once
        both are up.
        """
        assert self._control_listener and self._interrupt_listener

        while not self._stop.is_set():
            try:
                control, control_addr = self._control_listener.accept()
                log.info("Control channel from %s on %s", control_addr[0], self._bd_addr)

                interrupt, _ = self._interrupt_listener.accept()

                # Non-blocking writes: a stalled radio must never block the
                # datapath thread that calls send_input_report().
                interrupt.setblocking(False)

                self._sink.attach(control, interrupt, control_addr[0])
                self._serve_control(control)

            except OSError as exc:
                if self._stop.is_set():
                    return
                log.warning("Accept failed on %s: %s", self._bd_addr, exc)
                continue
            finally:
                self._sink.detach()

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
            response = self._profile.on_output_report(data[1:])
            self._sink.send_control(bytes([_HANDSHAKE_SUCCESSFUL]))
            if response:
                self._sink.send_input_report(response)

        elif transaction == _HID_DATA:
            # Output report from the console -- rumble, LEDs, and for the
            # Switch the whole subcommand handshake.
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


def is_supported() -> bool:
    """True if this platform can do L2CAP Bluetooth sockets at all."""
    return hasattr(socket, "AF_BLUETOOTH")

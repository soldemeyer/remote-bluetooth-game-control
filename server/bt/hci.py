"""Raw HCI command channel -- the layer BlueZ exposes no interface for.

Everything that decides the *latency characteristics* of a Bluetooth Classic HID
link lives in per-connection HCI commands: the automatic flush timeout, the link
policy that governs whether the link may be parked in sniff mode, and the
supervision timeout that decides how fast a dead link is noticed. None of these
has a D-Bus property, a ``bluetoothctl`` verb, or an MGMT opcode. Before this
module the entire over-the-air behaviour of this project was whatever BlueZ's
defaults happened to be -- and BlueZ's default automatic flush timeout is
*infinite*, which is the mechanism behind the latency spikes this subsystem was
rewritten to fix.

Why a raw socket is safe here
-----------------------------
``HCI_CHANNEL_RAW`` is the same channel ``hcitool cmd`` uses, and it coexists
with a running ``bluetoothd``: it does **not** take exclusive ownership of the
adapter the way ``HCI_CHANNEL_USER`` does. We send a small, fixed set of
commands and read their replies. We never bring the adapter up or down, never
touch pairing, and never write anything bluetoothd considers its own state --
that two-owners-for-one-setting fight is documented at length in CLAUDE.md and
this module deliberately stays out of it.

**Command Complete events are broadcast to every raw socket on the adapter**, so
a reply to somebody else's command lands in our queue too.
:meth:`HCISocket.command` therefore matches on opcode and discards anything
else, rather than assuming the next event is ours.

Requires ``CAP_NET_RAW`` (root). We already require it for the L2CAP binds, so
this adds no new privilege.

Linux-only. Every entry point degrades to a logged no-op elsewhere, so the
server still runs on a dev machine.
"""

from __future__ import annotations

import logging
import struct
import threading
import time

log = logging.getLogger(__name__)

#: Linux ``AF_BLUETOOTH`` protocol number for the HCI transport.
BTPROTO_HCI = 1

#: ``sockaddr_hci.hci_channel`` values. RAW is the command/event channel that
#: coexists with bluetoothd; USER would seize the adapter and is deliberately
#: not used here.
HCI_CHANNEL_RAW = 0
HCI_CHANNEL_USER = 1
HCI_CHANNEL_CONTROL = 3

#: setsockopt level/name for the kernel-side event filter.
SOL_HCI = 0
HCI_FILTER = 2

#: ``struct hci_ufilter`` is ``__u32 type_mask; __u32 event_mask[2]; __u16
#: opcode`` -- 14 bytes of fields, but **16 with the trailing padding**, and the
#: kernel rejects anything shorter than the full structure with ``EINVAL``.
#:
#: Packing the 14 significant bytes and passing those is the obvious thing to
#: do, and it fails on every adapter with an error that says only "Invalid
#: argument" -- indistinguishable from a bad device index or a permissions
#: problem. Measured on a Pi 5, kernel 6.18: len 14 rejected, 16 and 18 accepted.
HCI_FILTER_SIZE = 16

#: HCI packet type indicators, as they appear as the first byte on the socket.
HCI_COMMAND_PKT = 0x01
HCI_EVENT_PKT = 0x04

#: Events we care about.
EVT_COMMAND_COMPLETE = 0x0E
EVT_COMMAND_STATUS = 0x0F

#: Opcode = OGF << 10 | OCF. Named here so callers never open-code one; the
#: values are from the Core specification's HCI tables and match
#: ``bluetooth/hci.h``.
#:
#: OGF 0x01 -- Link Control
OCF_READ_REMOTE_EXT_FEATURES = 0x041C
#: OGF 0x02 -- Link Policy
OCF_EXIT_SNIFF_MODE = 0x0804
OCF_QOS_SETUP = 0x0807
OCF_READ_LINK_POLICY = 0x080C
OCF_WRITE_LINK_POLICY = 0x080D
OCF_READ_DEFAULT_LINK_POLICY = 0x080E
OCF_WRITE_DEFAULT_LINK_POLICY = 0x080F
OCF_SNIFF_SUBRATING = 0x0811
#: OGF 0x03 -- Controller & Baseband
OCF_WRITE_PAGE_TIMEOUT = 0x0C18
OCF_READ_SCAN_ENABLE = 0x0C19
OCF_READ_AUTHENTICATION_ENABLE = 0x0C1F
OCF_READ_CLASS_OF_DEVICE = 0x0C23
OCF_READ_AUTOMATIC_FLUSH_TIMEOUT = 0x0C27
OCF_WRITE_AUTOMATIC_FLUSH_TIMEOUT = 0x0C28
OCF_READ_LINK_SUPERVISION_TIMEOUT = 0x0C36
OCF_WRITE_LINK_SUPERVISION_TIMEOUT = 0x0C37
OCF_READ_SIMPLE_PAIRING_MODE = 0x0C55

#: Authenticated Payload Timeout -- the LE Ping timer, and the reason a
#: perfectly healthy gamepad link is torn down every ~35 seconds.
#:
#: Once a link is encrypted the controller expects to receive an authenticated
#: packet from the peer within this window. A gamepad streams notifications
#: *outward* and a console has no reason to send anything back, so nothing ever
#: arrives, LE Ping does not save us if the peer does not answer it, and the
#: controller raises `Authenticated Payload Timeout Expired` (0x57). The kernel
#: then disconnects -- correctly, by the letter of the spec, and fatally for us.
#:
#: Measured against an Analogue 3D: the console connected, encrypted, subscribed
#: to notifications, received reports, and was hung up on 30 s later, over and
#: over. `> HCI Event: Authenticated Payl.. (0x57)` immediately precedes every
#: `< HCI Command: Disconnect`. Nothing in any log above HCI mentions it.
#:
#: The default is 30 s (0x0BB8 in 10 ms units).
OCF_READ_AUTHENTICATED_PAYLOAD_TIMEOUT = 0x0C7B
OCF_WRITE_AUTHENTICATED_PAYLOAD_TIMEOUT = 0x0C7C

#: Authenticated Payload Timeout is a uint16 of 10 ms units, so this ceiling is
#: about 10.9 minutes -- effectively "do not police this", which is the right
#: answer for a link whose peer legitimately never transmits. Genuine link loss
#: is still caught by the supervision timeout, which is the timer that actually
#: means "the peer is gone" rather than "the peer is quiet".
MAX_AUTH_PAYLOAD_TIMEOUT = 0xFFFF

#: ``HCIGETCONNLIST``: _IOR('H', 212, int). Used to find the ACL handle for a
#: peer address, which is otherwise unobtainable for an LE link -- the Classic
#: path reads it from an L2CAP socket via L2CAP_CONNINFO, and BLE has no socket
#: of ours to ask.
HCIGETCONNLIST = 0x800448D4

#: ``struct hci_conn_info``: handle, bdaddr[6], type, out, state, link_mode.
_CONN_INFO = struct.Struct("<H6sBBHI")

#: Link policy bits (``HCI_LP_*``). Sniff is the one that matters: with it set,
#: either end may park the link, and the first report after an idle gap then
#: pays a full sniff-exit negotiation.
LP_ROLE_SWITCH = 0x0001
LP_HOLD = 0x0002
LP_SNIFF = 0x0004
LP_PARK = 0x0008

#: Baseband slot time. Nearly every HCI duration is a count of these.
SLOT_MS = 0.625

#: Automatic flush timeout is a 12-bit field: 0 means infinite (the default,
#: and the thing we are here to change), 0x07FF is the largest finite value.
MAX_FLUSH_TIMEOUT_SLOTS = 0x07FF

#: Supervision timeout bounds. The spec allows more at the bottom end than is
#: ever sensible for a link crossing a room.
MIN_SUPERVISION_SLOTS = 0x0190      # 250 ms
MAX_SUPERVISION_SLOTS = 0xFFFF


class HCIError(RuntimeError):
    """An HCI command failed, or the channel could not be opened."""


class HCIStatusError(HCIError):
    """The controller accepted the command and returned a non-zero status."""

    def __init__(self, opcode: int, status: int) -> None:
        super().__init__(
            f"HCI command 0x{opcode:04X} failed with status 0x{status:02X} "
            f"({status_name(status)})"
        )
        self.opcode = opcode
        self.status = status


#: The handful of status codes that actually turn up here, so a log line says
#: something useful instead of a bare number.
_STATUS_NAMES = {
    0x00: "success",
    0x01: "unknown HCI command",
    0x02: "unknown connection identifier",
    0x0C: "command disallowed",
    0x11: "unsupported feature or parameter value",
    0x12: "invalid HCI command parameters",
}


def status_name(status: int) -> str:
    return _STATUS_NAMES.get(status, "unspecified")


def ms_to_slots(milliseconds: float) -> int:
    """Convert a duration to baseband slots, rounding to nearest.

    Rounds rather than truncating: a caller asking for 30 ms should not quietly
    get 29.375 ms because floating-point drift left the division just under 48.
    """
    return max(1, round(milliseconds / SLOT_MS))


def slots_to_ms(slots: int) -> float:
    return slots * SLOT_MS


def is_supported() -> bool:
    """True if this platform can open an HCI socket at all."""
    import socket as _socket

    return hasattr(_socket, "AF_BLUETOOTH") and hasattr(_socket, "SOCK_RAW")


def _build_filter() -> bytes:
    """The kernel-side event filter: HCI events only, all event codes.

    ``struct hci_filter`` is ``__u32 type_mask; __u32 event_mask[2]; __le16
    opcode``. The opcode field is left at zero -- it filters *Command Complete
    for one opcode only*, and one socket serves every command for an adapter,
    so matching has to happen in :meth:`HCISocket.command` regardless.
    """
    type_mask = 1 << HCI_EVENT_PKT
    # The two trailing pad bytes are required; see HCI_FILTER_SIZE.
    return struct.pack("<IIIH2x", type_mask, 0xFFFFFFFF, 0xFFFFFFFF, 0x0000)


def parse_event(data: bytes) -> tuple[int, bytes] | None:
    """Split one socket read into ``(event_code, payload)``.

    Returns None for anything that is not a well-formed HCI event packet, so a
    stray frame or a truncated read is skipped rather than misparsed. Pure, so
    the framing is testable without a Bluetooth adapter.
    """
    if len(data) < 3 or data[0] != HCI_EVENT_PKT:
        return None

    event_code = data[1]
    length = data[2]
    payload = data[3 : 3 + length]
    if len(payload) != length:
        return None
    return event_code, payload


class HCISocket:
    """A synchronous HCI command channel bound to one adapter.

    One instance per adapter, opened when the adapter comes up and closed when
    it goes away. Calls are serialised by an internal lock and block for the
    controller's reply, so this must **never** be called from the datapath
    thread -- it is startup and connection-event work only.
    """

    def __init__(self, index: int, *, timeout_s: float = 1.0) -> None:
        #: The ``hciX`` number. HCI sockets are bound by index, not BD_ADDR --
        #: the one place in this codebase where an index is the correct
        #: identifier, because it is what the kernel API takes. Callers resolve
        #: BD_ADDR to index and pass it in.
        self.index = index
        self._timeout_s = timeout_s
        self._sock = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Bind the raw channel. Raises HCIError with an actionable message."""
        import socket

        if self._sock is not None:
            return

        if not is_supported():
            raise HCIError("This platform has no AF_BLUETOOTH support")

        try:
            sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
        except OSError as exc:
            raise HCIError(f"Could not create an HCI socket: {exc}") from exc

        try:
            # Bind first: the filter is per socket and the kernel rejects
            # HCI_FILTER on a socket whose channel is not yet settled.
            #
            # The address is a **one-element** tuple. CPython 3.13 accepts only
            # ``(device_id,)`` here and rejects ``(device_id, channel)`` with
            # "bind(): wrong format" -- which is why anything needing a channel
            # other than RAW (MGMT, on HCI_CHANNEL_CONTROL) cannot use
            # ``socket.bind`` at all and goes through ctypes instead. RAW is the
            # default channel, which is what this socket wants.
            sock.bind((self.index,))
            sock.setsockopt(SOL_HCI, HCI_FILTER, _build_filter())
            sock.settimeout(self._timeout_s)
        except PermissionError as exc:
            sock.close()
            raise HCIError(
                f"Permission denied opening the HCI channel for hci{self.index}. "
                "Raw HCI needs CAP_NET_RAW -- the same privilege the L2CAP binds "
                "already require, so if HID works this should too. Run as root, or:\n"
                "  sudo setcap 'cap_net_raw,cap_net_admin,cap_net_bind_service+eip' "
                "$(readlink -f $(which python3))"
            ) from exc
        except OSError as exc:
            sock.close()
            raise HCIError(
                f"Could not bind the HCI channel for hci{self.index}: {exc}"
            ) from exc

        self._sock = sock
        log.debug("HCI command channel open on hci%d", self.index)

    def close(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def __enter__(self) -> "HCISocket":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- commands ----------------------------------------------------------

    def command(self, opcode: int, params: bytes = b"") -> bytes:
        """Send one command and return its Command Complete return parameters.

        The leading status byte is consumed: a non-zero status raises
        :class:`HCIStatusError`, so callers only ever see the payload.

        A command that answers with Command Status rather than Command Complete
        -- the Link Policy commands that complete asynchronously, such as Exit
        Sniff Mode -- returns ``b""`` once the controller has accepted it. The
        completion event is not awaited; nothing here needs to block on a mode
        change actually finishing.
        """
        if self._sock is None:
            raise HCIError(f"HCI channel for hci{self.index} is not open")

        if len(params) > 255:
            raise HCIError(f"HCI command 0x{opcode:04X} parameters too long")

        packet = struct.pack("<BHB", HCI_COMMAND_PKT, opcode, len(params)) + params

        with self._lock:
            sock = self._sock
            if sock is None:
                raise HCIError(f"HCI channel for hci{self.index} closed")

            try:
                sock.send(packet)
            except OSError as exc:
                raise HCIError(
                    f"Could not send HCI command 0x{opcode:04X} on hci{self.index}: {exc}"
                ) from exc

            return self._await_reply(sock, opcode)

    def _await_reply(self, sock, opcode: int) -> bytes:
        """Read events until ours turns up, or the deadline passes.

        Every raw socket on the adapter receives every event, so replies to
        commands issued by bluetoothd or by a ``bluetoothctl`` session arrive
        here too. Discarding by opcode is the only correct way to tell them
        apart -- taking the next event and hoping would misattribute somebody
        else's status to our command.
        """
        deadline = time.monotonic() + self._timeout_s

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HCIError(
                    f"Timed out waiting for HCI command 0x{opcode:04X} on hci{self.index}"
                )
            sock.settimeout(remaining)

            try:
                data = sock.recv(260)
            except TimeoutError as exc:
                raise HCIError(
                    f"Timed out waiting for HCI command 0x{opcode:04X} on hci{self.index}"
                ) from exc
            except OSError as exc:
                raise HCIError(f"HCI channel error on hci{self.index}: {exc}") from exc

            parsed = parse_event(data)
            if parsed is None:
                continue

            event_code, payload = parsed
            result = _match_reply(event_code, payload, opcode)
            if result is not None:
                return result


def _match_reply(event_code: int, payload: bytes, opcode: int) -> bytes | None:
    """Decode one event into this command's reply, or None if it is not ours.

    Split out and pure so the opcode-matching -- the part that goes wrong when
    another process is talking to the same adapter -- can be tested directly.
    Raises :class:`HCIStatusError` when the event *is* ours and reports failure.
    """
    if event_code == EVT_COMMAND_COMPLETE:
        # ncmd(1), opcode(2), return parameters...
        if len(payload) < 3:
            return None
        if int.from_bytes(payload[1:3], "little") != opcode:
            return None
        body = payload[3:]
        if not body:
            # A Command Complete with no return parameters is malformed for
            # every opcode we issue; treat it as success rather than indexing
            # past the end.
            return b""
        if body[0] != 0x00:
            raise HCIStatusError(opcode, body[0])
        return body[1:]

    if event_code == EVT_COMMAND_STATUS:
        # status(1), ncmd(1), opcode(2)
        if len(payload) < 4:
            return None
        if int.from_bytes(payload[2:4], "little") != opcode:
            return None
        if payload[0] != 0x00:
            raise HCIStatusError(opcode, payload[0])
        return b""

    return None


def connections(index: int) -> list[tuple[int, str, int]]:
    """Every ACL connection on ``hciX``, as ``(handle, bd_addr, type)``.

    ``type`` is 0 for SCO, 1 for ACL (BR/EDR) and 128 for LE.

    This exists because an LE link's connection handle is otherwise out of
    reach. The Classic path gets it from an L2CAP socket via ``L2CAP_CONNINFO``,
    but for BLE the connection belongs to bluetoothd and we own no socket on it
    -- so we ask the kernel's connection list directly, which is what
    ``hcitool con`` does.

    Returns an empty list rather than raising if the ioctl is unavailable: this
    feeds link tuning, which is never allowed to be fatal.
    """
    # Imported here, not at module scope, for the same reason the rest of this
    # module does it: fcntl does not exist on Windows and the server must still
    # import on a dev machine. ImportError is caught alongside OSError because
    # a missing module and a refused socket are the same thing to the caller --
    # no connections we can see, and nothing worth raising over.
    try:
        import fcntl
        import socket
    except ImportError:
        return []

    try:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
    except OSError as exc:
        log.debug("Could not open a socket to list connections: %s", exc)
        return []

    try:
        # struct hci_conn_list_req { uint16 dev_id; uint16 conn_num; info[] }
        # The kernel fills conn_num with how many it actually wrote, so ask for
        # a generous fixed number and read back the count.
        want = 16
        buf = bytearray(4 + _CONN_INFO.size * want)
        struct.pack_into("<HH", buf, 0, index, want)
        try:
            fcntl.ioctl(sock.fileno(), HCIGETCONNLIST, buf)
        except OSError as exc:
            log.debug("HCIGETCONNLIST failed for hci%d: %s", index, exc)
            return []

        count = struct.unpack_from("<H", buf, 2)[0]
        out: list[tuple[int, str, int]] = []
        for i in range(min(count, want)):
            handle, raw, ctype, _out, _state, _mode = _CONN_INFO.unpack_from(
                buf, 4 + i * _CONN_INFO.size
            )
            # bdaddr is little-endian on the wire, as everywhere in HCI.
            addr = ":".join(f"{b:02X}" for b in reversed(raw))
            out.append((handle, addr, ctype))
        return out
    finally:
        sock.close()


def handle_for_address(index: int, bd_addr: str) -> int | None:
    """The ACL handle for a peer on ``hciX``, or None if it is not connected."""
    target = bd_addr.upper()
    for handle, addr, _ctype in connections(index):
        if addr == target:
            return handle
    return None

"""The Bluetooth management socket: adapter state without shelling out.

Two jobs, and the second is the one that matters.

**Reading settings.** This replaces every ``btmgmt`` subprocess call. Those cost
a 5 s timeout each when they went wrong, and they went wrong constantly: under
systemd ``btmgmt`` inherits ``/dev/null`` on stdin and hangs forever on it, so
*every* invocation the server ever made burned its full timeout and returned
failure. That is documented at length in CLAUDE.md. A socket has no stdin, no
argv, and no 5 s anything.

**Watching events.** This is the real prize. MGMT broadcasts every adapter
state transition -- an index appearing or disappearing, settings changing, a
device connecting or disconnecting, a new link key being stored. The adapter
manager currently discovers all of that by re-enumerating on a 10 s timer, so
it is up to ten seconds late and it rebuilds every ``AdapterInfo`` object each
time. Subscribing means a dongle being unplugged, or a console connecting, is
known in milliseconds.

Read and observe only
---------------------
``bluetoothd`` owns adapter state. We are a *second* MGMT client, which the
kernel permits -- it is how ``btmgmt`` coexists with the daemon -- but two
clients writing the same setting is precisely the desynchronisation that has
bitten this project repeatedly. So nothing here writes: settings are read, and
changes go through ``org.bluez.Adapter1`` over D-Bus where bluetoothd can see
them. :meth:`MGMTSocket.command` will refuse an opcode that is not on the
read-only allowlist.

Binding needs ctypes
--------------------
``sockaddr_hci`` carries a channel, and MGMT lives on ``HCI_CHANNEL_CONTROL``.
CPython's ``socket.bind`` for ``BTPROTO_HCI`` accepts only a one-element
``(device_id,)`` tuple on current versions -- ``(device_id, channel)`` is
rejected outright with "bind(): wrong format" -- so there is no way to reach any
channel but the default ``HCI_CHANNEL_RAW`` through it. ``libc.bind`` with a
hand-packed address is the only route. Verified on a Pi 5, kernel 6.18,
Python 3.13.

Linux-only.
"""

from __future__ import annotations

import ctypes
import logging
import socket
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

AF_BLUETOOTH = 31
BTPROTO_HCI = 1
HCI_CHANNEL_CONTROL = 3

#: Binding to this index rather than a specific adapter is what makes one
#: socket serve every adapter, and -- more importantly -- receive the
#: Index Added / Index Removed events for adapters that do not exist yet.
MGMT_INDEX_NONE = 0xFFFF

#: Every MGMT command and event begins with the same six-byte header.
_HEADER = struct.Struct("<HHH")

#: Commands. Reads, plus the two per-client advertising ops -- see
#: ``_ALLOWED_OPCODES``.
OP_READ_VERSION = 0x0001
OP_READ_INDEX_LIST = 0x0003
OP_READ_INFO = 0x0004
OP_GET_CONNECTIONS = 0x0015
OP_READ_ADV_FEATURES = 0x003D
OP_ADD_ADVERTISING = 0x003E
OP_REMOVE_ADVERTISING = 0x003F

#: Replies to our own commands.
EV_CMD_COMPLETE = 0x0001
EV_CMD_STATUS = 0x0002

#: Broadcast state changes -- the reason this module exists.
EV_CONTROLLER_ERROR = 0x0003
EV_INDEX_ADDED = 0x0004
EV_INDEX_REMOVED = 0x0005
EV_NEW_SETTINGS = 0x0006
EV_CLASS_OF_DEV_CHANGED = 0x0007
EV_NEW_LINK_KEY = 0x0009
EV_DEVICE_CONNECTED = 0x000B
EV_DEVICE_DISCONNECTED = 0x000C
EV_CONNECT_FAILED = 0x000D
EV_DEVICE_UNPAIRED = 0x0010

#: Authentication Failed. The precise signal for a **one-sided bond**: one end
#: holds a key the other threw away, so encryption can never complete.
#:
#: This is worth acting on rather than merely logging, because neither end
#: recovers by itself. Measured against an Analogue 3D, it presents two ways
#: depending on which half survived::
#:
#:    peer has the key, we do not:  peer sends LE Long Term Key Request,
#:                                  we answer negative, peer disconnects
#:    we have the key, peer does not: we send SMP Security Request,
#:                                  peer answers Pairing Failed, we disconnect
#:
#: Either way the link comes up and dies in well under a second, forever -- 18
#: to 30 cycles per capture. The GUI shows a controller flickering between
#: connected and not, and nothing in any log says why.
EV_AUTH_FAILED = 0x0011
EV_DISCOVERING = 0x0013

EVENT_NAMES = {
    EV_CONTROLLER_ERROR: "controller-error",
    EV_INDEX_ADDED: "index-added",
    EV_INDEX_REMOVED: "index-removed",
    EV_NEW_SETTINGS: "new-settings",
    EV_CLASS_OF_DEV_CHANGED: "class-changed",
    EV_NEW_LINK_KEY: "new-link-key",
    EV_DEVICE_CONNECTED: "device-connected",
    EV_DEVICE_DISCONNECTED: "device-disconnected",
    EV_CONNECT_FAILED: "connect-failed",
    EV_DEVICE_UNPAIRED: "device-unpaired",
    EV_AUTH_FAILED: "auth-failed",
    EV_DISCOVERING: "discovering",
}

#: Opcodes this module is willing to send. An allowlist rather than a
#: denylist, because the damage from writing a setting bluetoothd owns is
#: silent and long-lived, and a new opcode should have to be considered on
#: purpose rather than merely not be forbidden yet.
_READ_ONLY_OPCODES = frozenset({
    OP_READ_VERSION, OP_READ_INDEX_LIST, OP_READ_INFO, OP_READ_ADV_FEATURES,
    OP_GET_CONNECTIONS,
})

#: The two writes that are allowed, and why the distinction is not a fudge.
#:
#: The rule this module holds is "do not write adapter **settings**", because
#: those are shared state bluetoothd owns and two writers desynchronise them --
#: the failure mode documented throughout CLAUDE.md.
#:
#: An advertising instance is not shared state. The kernel tracks **which
#: socket added it** and removes it when that socket closes, so adding one is
#: not a write anybody else can observe as a changed setting; it is claiming a
#: per-client resource the kernel arbitrates. `btmgmt add-adv` does exactly
#: this alongside a running bluetoothd.
#:
#: It is here at all because bluetoothd's own ``LEAdvertisingManager1`` cannot
#: publish an advertisement on this platform -- it takes the extended path and
#: the kernel rejects the data. See the note in ``ble/hogp.py``.
#:
#: The ownership is a feature: our advertisement dies with our process, which
#: is the lifecycle we want and one we would otherwise have to arrange.
_ADVERTISING_OPCODES = frozenset({OP_ADD_ADVERTISING, OP_REMOVE_ADVERTISING})

_ALLOWED_OPCODES = _READ_ONLY_OPCODES | _ADVERTISING_OPCODES

#: Adapter settings bits, from the MGMT specification.
SETTING_POWERED = 0x00000001
SETTING_CONNECTABLE = 0x00000002
SETTING_FAST_CONNECTABLE = 0x00000004
SETTING_DISCOVERABLE = 0x00000008
SETTING_BONDABLE = 0x00000010
SETTING_LINK_SECURITY = 0x00000020
SETTING_SSP = 0x00000040
SETTING_BREDR = 0x00000080
SETTING_LE = 0x00000200
SETTING_SECURE_CONN = 0x00000800

SETTING_NAMES = (
    (SETTING_POWERED, "powered"),
    (SETTING_CONNECTABLE, "connectable"),
    (SETTING_FAST_CONNECTABLE, "fast-connectable"),
    (SETTING_DISCOVERABLE, "discoverable"),
    (SETTING_BONDABLE, "bondable"),
    (SETTING_LINK_SECURITY, "link-security"),
    (SETTING_SSP, "ssp"),
    (SETTING_BREDR, "br/edr"),
    (SETTING_LE, "le"),
    (SETTING_SECURE_CONN, "secure-conn"),
)


def describe_settings(settings: int) -> str:
    """Render a settings bitmap the way ``btmgmt info`` does."""
    return " ".join(name for bit, name in SETTING_NAMES if settings & bit) or "none"


class MGMTError(RuntimeError):
    """The management socket could not be opened, or a command failed."""


class MGMTStatusError(MGMTError):
    """A command was answered with a non-zero status."""

    def __init__(self, opcode: int, status: int) -> None:
        super().__init__(f"MGMT command 0x{opcode:04X} failed with status 0x{status:02X}")
        self.opcode = opcode
        self.status = status


@dataclass(slots=True)
class AdapterSettings:
    """One adapter as MGMT sees it."""

    index: int
    bd_addr: str
    manufacturer: int
    supported: int
    current: int
    device_class: int
    name: str
    short_name: str

    @property
    def powered(self) -> bool:
        return bool(self.current & SETTING_POWERED)

    @property
    def connectable(self) -> bool:
        """Page scan. An adapter without it cannot be connected to at all.

        The trap CLAUDE.md documents: BlueZ only keeps an adapter connectable on
        its own when it has bonded devices that might reconnect, so a fresh
        dongle sits unreachable and the host reports only "We didn't get any
        response from the device".
        """
        return bool(self.current & SETTING_CONNECTABLE)

    @property
    def discoverable(self) -> bool:
        return bool(self.current & SETTING_DISCOVERABLE)

    @property
    def bondable(self) -> bool:
        return bool(self.current & SETTING_BONDABLE)

    @property
    def ssp(self) -> bool:
        return bool(self.current & SETTING_SSP)

    @property
    def bredr(self) -> bool:
        """Whether the Classic radio is enabled at all.

        The gate on every BR/EDR-only question. Secure Simple Pairing, page
        scan and the class of device are all Classic concepts, and reporting
        them as faults on an LE-only adapter names a problem that cannot exist
        -- while telling the operator hosts will be prompted for a PIN, on a
        transport with no PIN pairing to fall back to.
        """
        return bool(self.current & SETTING_BREDR)

    @property
    def le(self) -> bool:
        return bool(self.current & SETTING_LE)

    @property
    def secure_conn(self) -> bool:
        """LE Secure Connections.

        Off for the BLE transport, deliberately: it is not negotiable downward,
        so a console requesting Legacy pairing is refused outright. Measured
        against one -- every bond ended `status 0x5` with it on.
        """
        return bool(self.current & SETTING_SECURE_CONN)

    @property
    def link_security(self) -> bool:
        """Authentication forced at connection setup.

        Must be **off**: with it on the controller demands authentication as the
        link comes up and pairing degrades to the legacy flow, never reaching
        the SSP IO-capability exchange. The host then shows a PIN prompt for a
        device with no keypad.
        """
        return bool(self.current & SETTING_LINK_SECURITY)

    def snapshot(self) -> dict[str, object]:
        return {
            "index": self.index,
            "bd_addr": self.bd_addr,
            "powered": self.powered,
            "connectable": self.connectable,
            "discoverable": self.discoverable,
            "bondable": self.bondable,
            "ssp": self.ssp,
            "bredr": self.bredr,
            "le": self.le,
            "link_security": self.link_security,
            "class_of_device": self.device_class,
            "settings": describe_settings(self.current),
        }


def _format_addr(raw: bytes) -> str:
    """A BD_ADDR crosses this interface little-endian."""
    return ":".join(f"{byte:02X}" for byte in reversed(raw))


def parse_read_info(params: bytes) -> AdapterSettings | None:
    """Decode a READ_INFO reply.

    Pure, so the layout can be tested against captured bytes on any machine --
    which matters because getting an offset wrong here produces plausible
    nonsense rather than an error.
    """
    # address(6) version(1) manufacturer(2) supported(4) current(4)
    # class(3) name(249) short_name(11)
    if len(params) < 20:
        return None

    bd_addr = _format_addr(params[0:6])
    manufacturer = struct.unpack_from("<H", params, 7)[0]
    supported, current = struct.unpack_from("<II", params, 9)
    device_class = int.from_bytes(params[17:20], "little")

    name = params[20:269].split(b"\x00", 1)[0].decode("utf-8", "replace")
    short_name = params[269:280].split(b"\x00", 1)[0].decode("utf-8", "replace")

    return AdapterSettings(
        index=-1,
        bd_addr=bd_addr,
        manufacturer=manufacturer,
        supported=supported,
        current=current,
        device_class=device_class,
        name=name,
        short_name=short_name,
    )


def parse_index_list(params: bytes) -> list[int]:
    """Decode a READ_INDEX_LIST reply."""
    if len(params) < 2:
        return []
    count = struct.unpack_from("<H", params, 0)[0]
    available = (len(params) - 2) // 2
    return [
        struct.unpack_from("<H", params, 2 + 2 * i)[0]
        for i in range(min(count, available))
    ]


def parse_device_event(params: bytes) -> str | None:
    """The BD_ADDR out of a Device Connected / Disconnected event.

    Both begin with a six-byte little-endian address and a one-byte address
    type; everything after that differs and is not needed here.
    """
    if len(params) < 7:
        return None
    return _format_addr(params[0:6])


def parse_header(data: bytes) -> tuple[int, int, bytes] | None:
    """Split one datagram into ``(event, index, params)``.

    Returns None for a runt or a truncated body rather than guessing, for the
    same reason the HCI framing does: a misparsed header produces a plausible
    index and a plausible event code.
    """
    if len(data) < _HEADER.size:
        return None
    event, index, length = _HEADER.unpack_from(data, 0)
    params = data[_HEADER.size : _HEADER.size + length]
    if len(params) != length:
        return None
    return event, index, params


#: ``(event, index, params)``.
EventCallback = Callable[[int, int, bytes], None]


class MGMTSocket:
    """A read-only MGMT client with an event stream.

    One per process, bound to :data:`MGMT_INDEX_NONE` so it sees every adapter
    including ones plugged in later.

    Commands are synchronous and serialised. Events are delivered on a reader
    thread to registered callbacks, which therefore must be quick and must not
    raise -- a callback that blocks stalls every other listener, and one that
    throws would kill the reader and silently end all event delivery.
    """

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._listeners: list[EventCallback] = []
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()

        #: Replies for a command in flight, filled by the reader thread.
        self._pending_opcode: int | None = None
        self._pending_reply: tuple[int, bytes] | None = None
        self._reply_ready = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Bind the management channel. Raises MGMTError with a usable message."""
        if self._sock is not None:
            return

        if not hasattr(socket, "AF_BLUETOOTH"):
            raise MGMTError("This platform has no AF_BLUETOOTH support")

        try:
            sock = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
        except OSError as exc:
            raise MGMTError(f"Could not create a management socket: {exc}") from exc

        # socket.bind cannot express a channel; see the module docstring.
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        addr = struct.pack("<HHH", AF_BLUETOOTH, MGMT_INDEX_NONE, HCI_CHANNEL_CONTROL)
        if libc.bind(sock.fileno(), addr, len(addr)) != 0:
            err = ctypes.get_errno()
            sock.close()
            if err in (1, 13):          # EPERM / EACCES
                raise MGMTError(
                    "Permission denied opening the Bluetooth management socket. "
                    "It needs CAP_NET_ADMIN -- the L2CAP binds already require "
                    "comparable privilege, so if HID works this should too."
                )
            raise MGMTError(
                f"Could not bind the management socket: errno {err}"
            )

        self._sock = sock
        log.debug("Management socket open")

    def start(self) -> None:
        """Begin delivering events. Safe to call without any listeners."""
        if self._sock is None:
            raise MGMTError("Management socket is not open")
        if self._reader is not None:
            return

        self._stop.clear()
        self._reader = threading.Thread(
            target=self._read_loop, name="mgmt-events", daemon=True
        )
        self._reader.start()

    def close(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

        reader, self._reader = self._reader, None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2.0)

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def add_listener(self, callback: EventCallback) -> None:
        self._listeners.append(callback)

    def __enter__(self) -> "MGMTSocket":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- commands ----------------------------------------------------------

    def command(
        self, opcode: int, index: int = MGMT_INDEX_NONE, params: bytes = b"",
        *, timeout: float = 2.0,
    ) -> bytes:
        """Send one command and return its reply parameters.

        Refuses any opcode not on the read-only allowlist. That is not
        defensiveness for its own sake: bluetoothd owns adapter state, and a
        second MGMT client writing it produces a disagreement that surfaces
        much later as an adapter behaving unlike what every management layer
        reports. Changes belong on ``org.bluez.Adapter1``.
        """
        if opcode not in _ALLOWED_OPCODES:
            raise MGMTError(
                f"MGMT opcode 0x{opcode:04X} is not on the allowlist. This module "
                "reads adapter state and manages its own advertising instances; it "
                "does not write adapter settings -- use org.bluez.Adapter1 over "
                "D-Bus so bluetoothd stays in agreement."
            )

        with self._lock:
            sock = self._sock
            if sock is None:
                raise MGMTError("Management socket is not open")

            self._pending_opcode = opcode
            self._pending_reply = None
            self._reply_ready.clear()

            try:
                sock.send(_HEADER.pack(opcode, index, len(params)) + params)
            except OSError as exc:
                self._pending_opcode = None
                raise MGMTError(
                    f"Could not send MGMT command 0x{opcode:04X}: {exc}"
                ) from exc

            try:
                if self._reader is not None:
                    reply = self._await_via_reader(opcode, timeout)
                else:
                    reply = self._await_inline(sock, opcode, timeout)
            finally:
                self._pending_opcode = None

        status, body = reply
        if status != 0x00:
            raise MGMTStatusError(opcode, status)
        return body

    def _await_via_reader(self, opcode: int, timeout: float) -> tuple[int, bytes]:
        if not self._reply_ready.wait(timeout=timeout):
            raise MGMTError(f"Timed out waiting for MGMT command 0x{opcode:04X}")
        reply = self._pending_reply
        if reply is None:
            raise MGMTError(f"No reply for MGMT command 0x{opcode:04X}")
        return reply

    def _await_inline(self, sock: socket.socket, opcode: int, timeout: float) -> tuple[int, bytes]:
        """Read replies directly, when no reader thread is running.

        Events seen while waiting are still dispatched rather than dropped:
        this path is used at startup, which is exactly when an adapter may be
        appearing, and silently discarding that would leave the manager's view
        wrong from its first moment.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MGMTError(f"Timed out waiting for MGMT command 0x{opcode:04X}")
            sock.settimeout(remaining)
            try:
                data = sock.recv(1024)
            except TimeoutError as exc:
                raise MGMTError(
                    f"Timed out waiting for MGMT command 0x{opcode:04X}"
                ) from exc
            except OSError as exc:
                raise MGMTError(f"Management socket error: {exc}") from exc

            if self._handle(data) and self._pending_reply is not None:
                return self._pending_reply

    # -- events ------------------------------------------------------------

    def _read_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        sock.settimeout(0.5)

        while not self._stop.is_set():
            try:
                data = sock.recv(1024)
            except TimeoutError:
                continue
            except OSError:
                if not self._stop.is_set():
                    log.debug("Management socket closed", exc_info=True)
                return
            self._handle(data)

    def _handle(self, data: bytes) -> bool:
        """Route one datagram. Returns True if it answered a pending command."""
        parsed = parse_header(data)
        if parsed is None:
            return False
        event, index, params = parsed

        if event in (EV_CMD_COMPLETE, EV_CMD_STATUS):
            return self._handle_reply(event, params)

        for listener in list(self._listeners):
            try:
                listener(event, index, params)
            except Exception:
                # One bad listener must not end event delivery for the others,
                # nor kill the reader thread and silently stop everything.
                log.exception("MGMT event listener failed for %s", EVENT_NAMES.get(event, event))
        return False

    def _handle_reply(self, event: int, params: bytes) -> bool:
        if len(params) < 3:
            return False
        opcode, status = struct.unpack_from("<HB", params, 0)
        if opcode != self._pending_opcode:
            # A reply to a command we are no longer waiting on -- a timeout that
            # arrived late. Dropping it is right; treating it as the current
            # answer would attribute one command's result to another.
            return False

        body = params[3:] if event == EV_CMD_COMPLETE else b""
        self._pending_reply = (status, body)
        self._reply_ready.set()
        return True

    # -- convenience -------------------------------------------------------

    def index_list(self) -> list[int]:
        """Every adapter index the kernel knows about."""
        return parse_index_list(self.command(OP_READ_INDEX_LIST))

    def read_info(self, index: int) -> AdapterSettings | None:
        """Full settings for one adapter, or None if it vanished mid-read.

        A dongle being unplugged between the index list and this call is
        ordinary, not exceptional -- hot-plug is the normal case here -- so it
        returns None rather than raising.
        """
        try:
            params = self.command(OP_READ_INFO, index=index)
        except MGMTStatusError:
            return None
        except MGMTError as exc:
            log.debug("Could not read MGMT info for index %d: %s", index, exc)
            return None

        settings = parse_read_info(params)
        if settings is not None:
            settings.index = index
        return settings

    def read_all(self) -> dict[str, AdapterSettings]:
        """Every present adapter, keyed by BD_ADDR.

        Keyed by address rather than index for the reason the rest of this
        codebase is: ``hciX`` numbering is assignment-order dependent and
        reshuffles across reboots and replugs.
        """
        found: dict[str, AdapterSettings] = {}
        for index in self.index_list():
            settings = self.read_info(index)
            if settings is not None:
                found[settings.bd_addr] = settings
        return found

    def version(self) -> tuple[int, int] | None:
        """MGMT protocol version, as ``(major, minor)``."""
        try:
            params = self.command(OP_READ_VERSION)
        except MGMTError:
            return None
        if len(params) < 3:
            return None
        major = params[0]
        minor = struct.unpack_from("<H", params, 1)[0]
        return major, minor

    def connections(self, index: int) -> list[str]:
        """Which hosts are connected to this adapter, right now.

        The **only** way to learn about a link that was already up before we
        subscribed. ``Device Connected`` fires at the moment of connection and
        never again, so a server restart while a console is attached leaves us
        permanently blind to it: the GUI says "waiting for console" with a live
        encrypted link on the radio, no Disconnect is offered, and the LE ping
        timeout is never extended on it.

        Reply is ``conn_count u16`` then that many ``address[6] + type[1]``.
        The type distinguishes BR/EDR from LE public and random; the address is
        all this needs, so it is parsed and discarded.
        """
        params = self.command(OP_GET_CONNECTIONS, index=index)
        if len(params) < 2:
            raise MGMTError(
                f"Get Connections returned {len(params)} bytes, expected at least 2"
            )

        count = struct.unpack_from("<H", params, 0)[0]
        found: list[str] = []
        for i in range(count):
            start = 2 + i * 7
            if start + 7 > len(params):
                # Trust the bytes present over the count, as advertising_instances
                # does: a short reply must not index off the end.
                break
            found.append(_format_addr(params[start:start + 6]))
        return found

    # -- advertising -------------------------------------------------------

    def advertising_instances(self, index: int) -> set[int]:
        """Which advertising instances this adapter currently carries.

        Read so the reconcile pass can be **read-then-write**, like every other
        invariant here. Re-adding unconditionally would take the advertisement
        down and put it back every ten seconds, which is a gap a scanning
        console can fall into for no reason at all.

        The reply is ``supported_flags u32, max_adv_data u8, max_scan_rsp u8,
        max_instances u8, num_instances u8`` followed by one byte per live
        instance. The count and the list are read separately because a kernel
        that reports more instances than it lists would otherwise index off the
        end -- and this runs against whatever kernel the operator has.

        Note the instances are the **adapter's**, not ours: bluetoothd's own
        would appear here too. That is the right answer for the question being
        asked, which is "is instance N present", not "who put it there".
        """
        params = self.command(OP_READ_ADV_FEATURES, index=index)
        if len(params) < 8:
            raise MGMTError(
                f"Read Advertising Features returned {len(params)} bytes, "
                "expected at least 8"
            )

        count = params[7]
        return set(params[8:8 + count])

    def add_advertising(
        self, index: int, instance: int, flags: int,
        adv_data: bytes = b"", scan_response: bytes = b"",
        *, duration: int = 0, timeout: int = 0,
    ) -> int:
        """Publish an advertising instance on one adapter.

        Returns the instance number the kernel assigned.

        The instance belongs to **this socket**: close it and the kernel takes
        the advertisement down. That is the lifecycle we want, and it means
        this does not fight bluetoothd -- see ``_ADVERTISING_OPCODES``.

        ``adv_data`` must not contain a Flags structure when ``flags``
        includes the discoverable or managed-flags bits. The kernel adds one
        itself and rejects the whole request if it finds a second, with the
        same Invalid Parameters status it gives for every other malformed
        field, so a duplicated Flags looks exactly like a length problem.
        """
        params = struct.pack(
            "<BIHHBB", instance, flags, duration, timeout,
            len(adv_data), len(scan_response),
        ) + bytes(adv_data) + bytes(scan_response)

        reply = self.command(OP_ADD_ADVERTISING, index=index, params=params)
        return reply[0] if reply else instance

    def remove_advertising(self, index: int, instance: int = 0) -> None:
        """Take down one instance, or every instance of ours when 0."""
        try:
            self.command(
                OP_REMOVE_ADVERTISING, index=index,
                params=struct.pack("<B", instance),
            )
        except MGMTError as exc:
            # Removing something already gone is ordinary at shutdown, and at
            # shutdown there is nobody left to act on a failure anyway.
            log.debug("Could not remove advertising instance %d: %s", instance, exc)


def is_supported() -> bool:
    """True if a management socket could plausibly be opened here."""
    import sys

    return sys.platform.startswith("linux") and hasattr(socket, "AF_BLUETOOTH")

"""Wire protocol: packet framing, encode/decode, and control messages.

Two classes of traffic share one UDP socket (one NAT mapping to punch and keep
alive -- see CLAUDE.md):

  * **Input packets** -- unreliable, ~32 bytes, up to 1000/s per controller.
    Full-state snapshots, so loss is self-healing and we never retransmit.
  * **Control messages** -- reliable, JSON, low rate. Handshake, usernames,
    capacity updates, assignment changes.

Every datagram starts with a 1-byte type tag so the receiver can dispatch
before doing any other work.

All multi-byte fields are little-endian: both ends are LE in practice, so this
avoids pointless byte swapping on the hot path.
"""

from __future__ import annotations

import json
import struct
from enum import IntEnum, StrEnum
from typing import Any

from common.state import ControllerState

#: Bumped on any incompatible change to framing or the handshake. Checked during
#: HELLO so a version mismatch produces a clear error instead of a decrypt
#: failure that looks like a wrong password.
PROTOCOL_VERSION = 1

#: Conservative cap that stays under the smallest realistic path MTU (IPv6
#: minimum 1280, minus IPv6+UDP headers, minus tunnel overhead for VPN users).
#: Control messages larger than this are rejected rather than fragmented --
#: fragmentation across NAT is a reliability problem we do not want.
MAX_DATAGRAM = 1200


class PacketType(IntEnum):
    """First byte of every datagram."""

    # --- Unencrypted, pre-session. Only valid during the handshake. ---
    HELLO = 0x01           # client -> server: version, client_id, nonce
    CHALLENGE = 0x02       # server -> client: salt, server_random, kdf params
    AUTH = 0x03            # client -> server: proof + encrypted client info
    ACCEPT = 0x04          # server -> client: session_id, capacity
    REJECT = 0x05          # server -> client: reason (bad password, full, ...)

    # --- Outer tag for every encrypted datagram. ---
    #
    # Required because an encrypted datagram begins with the 8-byte nonce
    # counter, whose low byte is arbitrary. Without an explicit outer tag, a
    # packet whose counter happened to be 1 would be indistinguishable from a
    # plaintext HELLO and would be dispatched to the handshake handler.
    # Framing is therefore:  [SESSION][counter:8][ciphertext][tag:16]
    SESSION = 0x40

    # --- Encrypted, in-session. These appear *inside* a SESSION datagram. ---
    INPUT = 0x10           # client -> server: controller state snapshot
    INPUT_ACK = 0x11       # server -> client: latency echo
    CONTROL = 0x12         # either direction: reliable JSON message
    CONTROL_ACK = 0x13     # either direction: ack for a CONTROL seq
    HEARTBEAT = 0x14       # either direction: keepalive + latency probe
    HEARTBEAT_ACK = 0x15
    DISCONNECT = 0x16      # graceful teardown
    FEEDBACK = 0x17        # server -> client: rumble from the console

    # --- Media channel (video source <-> clients). ---
    #
    # Carried inside SESSION datagrams on a *separate* socket from gameplay
    # input: video slices are 40x the size of an input packet and must never
    # queue in front of one. The only exception is the preview path, where the
    # video source sends VIDEO_FRAME slices of a small JPEG to the Bluetooth
    # server over its control session so the web GUI has something to show.
    #
    # An older peer drops these silently -- both dispatchers ignore unknown
    # kinds -- so adding them needs no PROTOCOL_VERSION bump. Wire formats live
    # in common/video.py.
    VIDEO_FRAME = 0x18         # source -> client: one slice of an encoded frame
    AUDIO_FRAME = 0x19         # source -> client: one Opus packet
    MEDIA_HEARTBEAT = 0x1A     # client -> source: clock-sync probe
    MEDIA_HEARTBEAT_ACK = 0x1B  # source -> client: clock-sync reply
    IDR_REQUEST = 0x1C         # client -> source: send a keyframe now
    MEDIA_REPORT = 0x1D        # client -> source: receiver stats

    # --- Rendezvous broker traffic. Never reaches the session layer. ---
    PUNCH = 0x20           # NAT hole-punching probe
    PUNCH_ACK = 0x21


#: Hole-punching probes, sent as raw byte strings rather than tagged packets.
#:
#: They must be distinguishable from both handshake packets and encrypted
#: session datagrams *before* any session exists, since punching happens first.
#: 'R' (0x52) collides with no PacketType, so a prefix check is unambiguous.
#: Both peers need these, which is why they live here rather than in the
#: client's hole-punching module.
PUNCH_PROBE = b"RBGC-PUNCH"
PUNCH_ACK_PROBE = b"RBGC-PUNCHED"

assert PUNCH_PROBE[0] not in {t.value for t in PacketType}, (
    "punch probe prefix must not collide with a packet type tag"
)


#: crypto.py duplicates this value as a literal to avoid a circular import.
#: Asserted here so the two can never drift apart silently.
assert PacketType.SESSION == 0x40, "SESSION tag must stay in sync with crypto.SESSION_TAG"


#: Inclusive tag range reserved for the media channel. Dispatchers test the
#: range rather than each tag so a receiver can hand the whole class to the
#: media layer in one branch -- and so 0x1E/0x1F stay available without
#: touching every dispatch site again.
MEDIA_TAG_MIN = 0x18
MEDIA_TAG_MAX = 0x1F

assert MEDIA_TAG_MIN <= PacketType.VIDEO_FRAME <= MEDIA_TAG_MAX
assert MEDIA_TAG_MIN <= PacketType.MEDIA_REPORT <= MEDIA_TAG_MAX


def is_media_tag(kind: int) -> bool:
    """True for any packet type belonging to the media channel."""
    return MEDIA_TAG_MIN <= kind <= MEDIA_TAG_MAX


class RejectReason(IntEnum):
    """Why a connection was refused. Surfaced verbatim in the client GUI."""

    BAD_PASSWORD = 1
    VERSION_MISMATCH = 2
    SERVER_FULL = 3
    NOT_APPROVED = 4       # operator denied it in the web GUI
    MALFORMED = 5
    RATE_LIMITED = 6

    def message(self) -> str:
        return {
            RejectReason.BAD_PASSWORD: "Incorrect password.",
            RejectReason.VERSION_MISMATCH: (
                "Protocol version mismatch -- client and server are different versions."
            ),
            RejectReason.SERVER_FULL: "Server has no free controller slots.",
            RejectReason.NOT_APPROVED: "The server operator denied this connection.",
            RejectReason.MALFORMED: "Malformed handshake.",
            RejectReason.RATE_LIMITED: "Too many attempts; try again shortly.",
        }[self]


class ControlOp(StrEnum):
    """Control-channel message kinds (the ``op`` field of the JSON body)."""

    # client -> server
    SET_USERNAME = "set_username"
    SET_CONTROLLERS = "set_controllers"    # slot list with names/usernames
    CONTROLLER_GONE = "controller_gone"    # unplugged client-side
    SET_RUMBLE = "set_rumble"              # client's rumble opt-in, toggleable live

    # server -> client
    CAPACITY = "capacity"                  # live adapter capacity update
    ASSIGNMENT = "assignment"              # slot -> adapter mapping changed
    APPROVED = "approved"                  # operator approved a pending client
    KICKED = "kicked"
    SERVER_STATUS = "server_status"

    # --- Video control plane ---
    #
    # The server -> client direction has no retransmit (see datapath), so
    # VIDEO_SOURCE is re-answered to every VIDEO_QUERY and VIDEO_CONFIG is
    # re-pushed until its cfg_seq comes back in a VIDEO_STATUS. That is the
    # same "full state, latest wins, ask again if unsure" discipline the input
    # path uses, rather than a second reliability mechanism.
    VIDEO_QUERY = "video_query"            # client -> server: where is the video?
    VIDEO_SOURCE = "video_source"          # server -> client: endpoint or "none"
    VIDEO_CONFIG = "video_config"          # server -> video source: desired settings
    VIDEO_STATUS = "video_status"          # video source -> server: what it is doing


# --------------------------------------------------------------------------
# Input packet
# --------------------------------------------------------------------------
#
# Layout (little-endian), 30 bytes after the 1-byte type tag:
#
#   seq              u32   monotonically increasing per session
#   client_send_ts   u64   client monotonic nanoseconds, echoed back for RTT
#   slot             u8    which of the client's controllers (0..3)
#   flags            u8    see InputFlags
#   left_x           i16
#   left_y           i16
#   right_x          i16
#   right_y          i16
#   left_trigger     u8
#   right_trigger    u8
#   buttons          u32
#
_INPUT_STRUCT = struct.Struct("<IQBBhhhhBBI")
INPUT_BODY_SIZE = _INPUT_STRUCT.size          # 28 (4+8+1+1+2+2+2+2+1+1+4)
INPUT_PACKET_SIZE = 1 + INPUT_BODY_SIZE       # 29 plaintext, 53 on the wire after AEAD

# '<' means no alignment padding, so this is exact. Asserted because a silent
# size change here would quietly alter the wire format.
assert INPUT_BODY_SIZE == 28, f"unexpected input body size {INPUT_BODY_SIZE}"


class InputFlags(IntEnum):
    NONE = 0
    CONTROLLER_DISCONNECTED = 1 << 0   # state is neutral because the pad went away
    REQUEST_ACK = 1 << 1               # ask the server to echo for a latency sample


def encode_input_into(
    buf: bytearray,
    offset: int,
    seq: int,
    client_send_ts: int,
    slot: int,
    flags: int,
    state: ControllerState,
) -> int:
    """Serialize an input packet into ``buf`` at ``offset``. Returns bytes written.

    Writes in place via ``pack_into`` so the hot path allocates nothing. The
    caller owns a long-lived bytearray and reuses it for every packet.
    """
    buf[offset] = PacketType.INPUT
    _INPUT_STRUCT.pack_into(
        buf,
        offset + 1,
        seq & 0xFFFFFFFF,
        client_send_ts & 0xFFFFFFFFFFFFFFFF,
        slot,
        flags,
        state.left_x,
        state.left_y,
        state.right_x,
        state.right_y,
        state.left_trigger,
        state.right_trigger,
        state.buttons & 0xFFFFFFFF,
    )
    return INPUT_PACKET_SIZE


def decode_input_into(
    data: bytes | bytearray | memoryview,
    offset: int,
    state: ControllerState,
) -> tuple[int, int, int, int]:
    """Deserialize an input packet body into ``state`` (mutated in place).

    ``offset`` points at the type tag. Returns ``(seq, client_send_ts, slot, flags)``.

    Raises ValueError if the buffer is too short -- callers must treat that as a
    malformed packet and drop it, never as a fatal error, since anyone can send
    us a UDP datagram.
    """
    if len(data) - offset < INPUT_PACKET_SIZE:
        raise ValueError(f"input packet too short: {len(data) - offset} bytes")

    (
        seq,
        client_send_ts,
        slot,
        flags,
        state.left_x,
        state.left_y,
        state.right_x,
        state.right_y,
        state.left_trigger,
        state.right_trigger,
        state.buttons,
    ) = _INPUT_STRUCT.unpack_from(data, offset + 1)

    return seq, client_send_ts, slot, flags


# --------------------------------------------------------------------------
# Input ack -- the latency echo
# --------------------------------------------------------------------------
#
#   seq             u32   the input seq being acked
#   client_send_ts  u64   echoed verbatim; client subtracts to get RTT
#   server_recv_ts  u64   server monotonic ns at receive
#   server_bt_ts    u64   server monotonic ns after the BT write returned
#   slot            u8
#
# Echoing the client's own timestamp means we never need clock synchronization
# between the two machines -- the client computes RTT purely against its own
# clock. server_recv_ts and server_bt_ts are only ever subtracted from each
# other, so they stay meaningful on the server's independent clock.
#
_INPUT_ACK_STRUCT = struct.Struct("<IQQQB")
INPUT_ACK_SIZE = 1 + _INPUT_ACK_STRUCT.size


def encode_input_ack_into(
    buf: bytearray,
    offset: int,
    seq: int,
    client_send_ts: int,
    server_recv_ts: int,
    server_bt_ts: int,
    slot: int,
) -> int:
    buf[offset] = PacketType.INPUT_ACK
    _INPUT_ACK_STRUCT.pack_into(
        buf,
        offset + 1,
        seq & 0xFFFFFFFF,
        client_send_ts & 0xFFFFFFFFFFFFFFFF,
        server_recv_ts & 0xFFFFFFFFFFFFFFFF,
        server_bt_ts & 0xFFFFFFFFFFFFFFFF,
        slot,
    )
    return INPUT_ACK_SIZE


def decode_input_ack(
    data: bytes | bytearray | memoryview, offset: int
) -> tuple[int, int, int, int, int]:
    """Returns ``(seq, client_send_ts, server_recv_ts, server_bt_ts, slot)``."""
    if len(data) - offset < INPUT_ACK_SIZE:
        raise ValueError("input ack too short")
    return _INPUT_ACK_STRUCT.unpack_from(data, offset + 1)


# --------------------------------------------------------------------------
# Heartbeat -- keepalive and idle latency probe
# --------------------------------------------------------------------------
#
# Runs even when the player is not touching the controller. Two jobs: keep the
# NAT mapping from expiring (common timeout is 30s, so we send well under that)
# and keep latency stats fresh so the GUI is not blank at idle.
#
_HEARTBEAT_STRUCT = struct.Struct("<IQ")
HEARTBEAT_SIZE = 1 + _HEARTBEAT_STRUCT.size


def encode_heartbeat_into(buf: bytearray, offset: int, seq: int, send_ts: int) -> int:
    buf[offset] = PacketType.HEARTBEAT
    _HEARTBEAT_STRUCT.pack_into(buf, offset + 1, seq & 0xFFFFFFFF, send_ts)
    return HEARTBEAT_SIZE


def encode_heartbeat_ack_into(buf: bytearray, offset: int, seq: int, send_ts: int) -> int:
    buf[offset] = PacketType.HEARTBEAT_ACK
    _HEARTBEAT_STRUCT.pack_into(buf, offset + 1, seq & 0xFFFFFFFF, send_ts)
    return HEARTBEAT_SIZE


def decode_heartbeat(data: bytes | bytearray | memoryview, offset: int) -> tuple[int, int]:
    """Returns ``(seq, original_send_ts)``."""
    if len(data) - offset < HEARTBEAT_SIZE:
        raise ValueError("heartbeat too short")
    return _HEARTBEAT_STRUCT.unpack_from(data, offset + 1)


# --------------------------------------------------------------------------
# Feedback -- rumble travelling server -> client
# --------------------------------------------------------------------------
#
#   slot         u8    which of the client's controllers
#   low_freq     u8    heavy/low-frequency motor, 0-255
#   high_freq    u8    light/high-frequency motor, 0-255
#   duration_ms  u16   how long to run; 0 means "until superseded"
#
# Unreliable on purpose, like input. Rumble is a continuous effect: a dropped
# update is corrected by the next one, and retransmitting a stale one would
# make the pad buzz after the explosion finished. Latest-wins throughout.
#
_FEEDBACK_STRUCT = struct.Struct("<BBBH")
FEEDBACK_SIZE = 1 + _FEEDBACK_STRUCT.size


def encode_feedback_into(
    buf: bytearray,
    offset: int,
    slot: int,
    low_freq: int,
    high_freq: int,
    duration_ms: int,
) -> int:
    """Serialize a rumble update. Allocation-free."""
    buf[offset] = PacketType.FEEDBACK
    _FEEDBACK_STRUCT.pack_into(
        buf,
        offset + 1,
        slot & 0xFF,
        low_freq & 0xFF,
        high_freq & 0xFF,
        min(duration_ms, 0xFFFF),
    )
    return FEEDBACK_SIZE


def decode_feedback(
    data: bytes | bytearray | memoryview, offset: int
) -> tuple[int, int, int, int]:
    """Returns ``(slot, low_freq, high_freq, duration_ms)``."""
    if len(data) - offset < FEEDBACK_SIZE:
        raise ValueError("feedback packet too short")
    return _FEEDBACK_STRUCT.unpack_from(data, offset + 1)


# --------------------------------------------------------------------------
# Control channel -- reliable JSON
# --------------------------------------------------------------------------
#
#   seq   u32   per-direction control sequence, acked explicitly
#   body  JSON, UTF-8
#
# Reliability is a simple stop-and-resend keyed on seq. Control traffic is rare
# and ordering barely matters, so anything more sophisticated would be wasted
# complexity.
#
_CONTROL_HEADER = struct.Struct("<I")


def encode_control(seq: int, op: str, payload: dict[str, Any] | None = None) -> bytes:
    """Build a control message. Allocates -- never called on the hot path."""
    body = {"op": op}
    if payload:
        body.update(payload)
    encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")

    if len(encoded) + 1 + _CONTROL_HEADER.size > MAX_DATAGRAM:
        raise ValueError(f"control message too large: {len(encoded)} bytes")

    return bytes([PacketType.CONTROL]) + _CONTROL_HEADER.pack(seq & 0xFFFFFFFF) + encoded


def decode_control(data: bytes | bytearray | memoryview, offset: int) -> tuple[int, dict[str, Any]]:
    """Returns ``(seq, body)``. Raises ValueError on anything malformed."""
    header_end = offset + 1 + _CONTROL_HEADER.size
    if len(data) < header_end:
        raise ValueError("control message too short")

    (seq,) = _CONTROL_HEADER.unpack_from(data, offset + 1)
    raw = bytes(data[header_end:])

    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"control message not valid JSON: {exc}") from exc

    if not isinstance(body, dict) or "op" not in body:
        raise ValueError("control message missing 'op'")

    return seq, body


def encode_control_ack(seq: int) -> bytes:
    return bytes([PacketType.CONTROL_ACK]) + _CONTROL_HEADER.pack(seq & 0xFFFFFFFF)


def decode_control_ack(data: bytes | bytearray | memoryview, offset: int) -> int:
    if len(data) - offset < 1 + _CONTROL_HEADER.size:
        raise ValueError("control ack too short")
    (seq,) = _CONTROL_HEADER.unpack_from(data, offset + 1)
    return seq


# --------------------------------------------------------------------------
# Sequence helpers
# --------------------------------------------------------------------------

_SEQ_MODULUS = 1 << 32
_SEQ_HALF = 1 << 31


def seq_is_newer(candidate: int, reference: int) -> bool:
    """Wrap-safe sequence comparison.

    At 1000 packets/s a u32 wraps after ~50 days of continuous play. Unlikely,
    but a naive ``>`` would stall the stream permanently if it ever happened, so
    compare via signed modular distance instead.

    Equal sequences are *not* newer -- distance 0 is excluded, otherwise a
    duplicate would be accepted as fresh.
    """
    distance = (candidate - reference) % _SEQ_MODULUS
    return 0 < distance < _SEQ_HALF


class ReplayWindow:
    """Sliding-window replay detector over a 64-packet history.

    Guards the encrypted channel: without this, an attacker who captured a
    packet could replay a button press. Duplicates also occur benignly on some
    NAT paths, and dropping them keeps the console from seeing a stale state.
    """

    __slots__ = ("_highest", "_bitmap")

    WINDOW = 64

    def __init__(self) -> None:
        self._highest = -1
        self._bitmap = 0

    def check_and_update(self, seq: int) -> bool:
        """True if ``seq`` is fresh (and records it); False if replayed or too old."""
        if self._highest < 0:
            self._highest = seq
            self._bitmap = 1
            return True

        if seq == self._highest:
            return False

        if seq_is_newer(seq, self._highest):
            shift = (seq - self._highest) % _SEQ_MODULUS
            if shift >= self.WINDOW:
                self._bitmap = 1
            else:
                self._bitmap = ((self._bitmap << shift) | 1) & ((1 << self.WINDOW) - 1)
            self._highest = seq
            return True

        # Older than the highest seen: accept only if inside the window and unseen.
        age = (self._highest - seq) % _SEQ_MODULUS
        if age >= self.WINDOW:
            return False
        mask = 1 << age
        if self._bitmap & mask:
            return False
        self._bitmap |= mask
        return True

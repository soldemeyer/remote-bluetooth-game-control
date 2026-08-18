"""Media wire format: frame slicing, reassembly, and clock synchronization.

Shared by the video server and the client, so this module follows the same
rules as the rest of ``common/``: standard library only, no PyAV, no Qt. It
describes *bytes on the wire* and nothing about codecs.

Three ideas carry the design, all inherited from the input path:

  * **Full frames are self-healing, not retransmitted.** A frame too large for
    one datagram is sliced; a slice lost is a frame lost. The receiver asks for
    a keyframe (``IDR_REQUEST``) rather than asking for the missing piece,
    because by the time a retransmission arrived the frame would be stale.
  * **Latest wins.** A newer ``frame_id`` supersedes an incomplete older one
    immediately. Holding a partial frame in the hope its stragglers arrive only
    adds latency to the frame after it.
  * **Every slice is self-contained.** ``capture_ts`` rides in each one, so
    losing slice 0 does not cost the timestamp the whole latency measurement
    depends on.

All multi-byte fields are little-endian, matching the rest of the protocol.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from common import protocol
from common.protocol import PacketType, seq_is_newer

# --------------------------------------------------------------------------
# Video frame slices
# --------------------------------------------------------------------------
#
# Layout (little-endian), after the 1-byte type tag:
#
#   frame_id      u32   per-stream counter, wrap-safe via seq_is_newer
#   slice_index   u16   0-based position within the frame
#   slice_count   u16   how many slices this frame was cut into (>= 1)
#   flags         u8    see SliceFlags
#   codec         u8    see MediaCodec
#   capture_ts    u64   source monotonic ns at capture -- in *every* slice
#
_VIDEO_SLICE_STRUCT = struct.Struct("<IHHBBQ")
VIDEO_SLICE_HEADER_SIZE = 1 + _VIDEO_SLICE_STRUCT.size

#: Payload bytes per slice. Sized so a full slice plus its header plus AEAD
#: overhead stays inside MAX_DATAGRAM: fragmenting across NAT is the exact
#: reliability problem the 1200-byte cap exists to avoid.
VIDEO_SLICE_PAYLOAD = 1150

#: SESSION tag (1) + nonce counter (8) + Poly1305 tag (16). Duplicated from
#: crypto rather than imported so this module stays dependency-free; asserted
#: against the real framing below.
_AEAD_OVERHEAD = 25

assert VIDEO_SLICE_HEADER_SIZE == 19, f"unexpected slice header size {VIDEO_SLICE_HEADER_SIZE}"
assert (
    VIDEO_SLICE_HEADER_SIZE + VIDEO_SLICE_PAYLOAD + _AEAD_OVERHEAD <= protocol.MAX_DATAGRAM
), "a full video slice must fit in one datagram after encryption"

#: Refuse to assemble anything larger. A keyframe at 1080p high bitrate is
#: comfortably under this; the cap exists so a hostile or corrupt slice_count
#: cannot make us allocate without bound.
MAX_FRAME_SIZE = 1_048_576

#: Slices per frame is bounded by the same reasoning.
MAX_SLICE_COUNT = (MAX_FRAME_SIZE // VIDEO_SLICE_PAYLOAD) + 1


class SliceFlags:
    """Bit flags in the slice header."""

    NONE = 0
    KEYFRAME = 1 << 0


class MediaCodec:
    """What the assembled payload is."""

    H264 = 1     # Annex B, in-band SPS/PPS on every IDR
    MJPEG = 2    # preview path only: one JPEG per frame


def encode_video_slice_into(
    buf: bytearray,
    offset: int,
    frame_id: int,
    slice_index: int,
    slice_count: int,
    flags: int,
    codec: int,
    capture_ts: int,
    payload: bytes | bytearray | memoryview,
) -> int:
    """Serialize one slice into ``buf`` at ``offset``. Returns bytes written.

    Writes through ``pack_into`` on a caller-owned buffer so the send loop
    allocates nothing per slice.
    """
    buf[offset] = PacketType.VIDEO_FRAME
    _VIDEO_SLICE_STRUCT.pack_into(
        buf,
        offset + 1,
        frame_id & 0xFFFFFFFF,
        slice_index & 0xFFFF,
        slice_count & 0xFFFF,
        flags & 0xFF,
        codec & 0xFF,
        capture_ts & 0xFFFFFFFFFFFFFFFF,
    )
    end = offset + VIDEO_SLICE_HEADER_SIZE
    buf[end : end + len(payload)] = payload
    return VIDEO_SLICE_HEADER_SIZE + len(payload)


def decode_video_slice(
    data: bytes | bytearray | memoryview, offset: int
) -> tuple[int, int, int, int, int, int, memoryview]:
    """Returns ``(frame_id, index, count, flags, codec, capture_ts, payload)``.

    The payload is a memoryview over ``data`` -- no copy. Raises ValueError on
    anything malformed; callers drop the datagram rather than failing, since
    anyone can send us one.
    """
    if len(data) - offset < VIDEO_SLICE_HEADER_SIZE:
        raise ValueError("video slice too short")

    frame_id, index, count, flags, codec, capture_ts = _VIDEO_SLICE_STRUCT.unpack_from(
        data, offset + 1
    )

    if count == 0 or count > MAX_SLICE_COUNT:
        raise ValueError(f"implausible slice count {count}")
    if index >= count:
        raise ValueError(f"slice index {index} outside count {count}")

    view = memoryview(data)[offset + VIDEO_SLICE_HEADER_SIZE :]
    return frame_id, index, count, flags, codec, capture_ts, view


def slice_count_for(size: int) -> int:
    """How many slices a payload of ``size`` bytes needs (at least one)."""
    if size <= 0:
        return 1
    return (size + VIDEO_SLICE_PAYLOAD - 1) // VIDEO_SLICE_PAYLOAD


@dataclass(slots=True)
class CompletedFrame:
    """One fully reassembled frame, ready to decode."""

    frame_id: int
    keyframe: bool
    codec: int
    capture_ts: int
    data: bytes


class FrameAssembler:
    """Reassembles sliced frames, latest-wins.

    Holds exactly one in-progress frame. A slice belonging to a newer frame
    discards whatever was in progress -- waiting for stragglers would delay
    every frame behind them, and the encoder has already moved on.
    """

    __slots__ = (
        "_frame_id",
        "_started",
        "_delivered",
        "_slices",
        "_received",
        "_count",
        "_flags",
        "_codec",
        "_capture_ts",
        "_max_frame_size",
        "_max_slices",
        "_gap",
        "frames_complete",
        "frames_dropped",
        "slices_received",
        "slices_lost",
    )

    def __init__(self, max_frame_size: int = MAX_FRAME_SIZE) -> None:
        self._max_frame_size = max_frame_size
        self._max_slices = slice_count_for(max_frame_size)
        self._frame_id = 0
        self._started = False

        #: Whether _frame_id names a frame already handed over, so late
        #: slices of it can be told apart from the start of a new one.
        self._delivered = False
        self._slices: list[bytes | None] = []
        self._received = 0
        self._count = 0
        self._flags = 0
        self._codec = 0
        self._capture_ts = 0
        self._gap = False

        self.frames_complete = 0
        self.frames_dropped = 0
        self.slices_received = 0
        self.slices_lost = 0

    def add(
        self,
        frame_id: int,
        index: int,
        count: int,
        flags: int,
        codec: int,
        capture_ts: int,
        payload: bytes | bytearray | memoryview,
    ) -> CompletedFrame | None:
        """Absorb one slice. Returns the frame when this slice completes it."""
        # Compare slice *counts*, not the bytes they could hold. The last
        # slice is usually partial, so multiplying out over-estimates by up to
        # a full slice and silently rejects frames a little under the cap --
        # at a high bitrate that is a real 1080p keyframe, discarded with no
        # gap recorded and no keyframe requested. A permanently black screen
        # with every counter looking healthy.
        if count > self._max_slices:
            return None

        self.slices_received += 1

        if not self._started:
            # A straggler or duplicate from the frame just delivered would
            # otherwise start a fresh reassembly and deliver it a second time.
            if self._delivered and not seq_is_newer(frame_id, self._frame_id):
                return None
            self._begin(frame_id, count, flags, codec, capture_ts)
        elif frame_id != self._frame_id:
            if not seq_is_newer(frame_id, self._frame_id):
                # A straggler from a frame we already gave up on, or a
                # duplicate of one already delivered. Nothing to do with it.
                return None
            # Newer frame: abandon the incomplete one.
            self.frames_dropped += 1
            self.slices_lost += self._count - self._received
            self._gap = True
            self._begin(frame_id, count, flags, codec, capture_ts)

        if index >= self._count or self._slices[index] is not None:
            return None    # out of range for this frame, or a duplicate slice

        self._slices[index] = bytes(payload)
        self._received += 1
        # Any slice may carry the keyframe flag; trust the first one that does.
        self._flags |= flags

        if self._received != self._count:
            return None

        frame = CompletedFrame(
            frame_id=self._frame_id,
            keyframe=bool(self._flags & SliceFlags.KEYFRAME),
            codec=self._codec,
            capture_ts=self._capture_ts,
            data=b"".join(s for s in self._slices if s is not None),
        )
        self.frames_complete += 1
        self._started = False
        self._delivered = True
        self._slices = []
        return frame

    def _begin(self, frame_id: int, count: int, flags: int, codec: int, capture_ts: int) -> None:
        self._frame_id = frame_id
        self._count = count
        self._flags = flags
        self._codec = codec
        self._capture_ts = capture_ts
        self._slices = [None] * count
        self._received = 0
        self._started = True
        self._delivered = False

    def gap_detected(self) -> bool:
        """True once since the last call if a frame was lost. Clears the flag.

        Drives keyframe requests: the caller asks for an IDR when this trips,
        rate-limited on its own side.
        """
        gap = self._gap
        self._gap = False
        return gap

    def reset(self) -> None:
        """Forget the in-progress frame, keeping counters."""
        self._started = False
        self._delivered = False
        self._slices = []
        self._received = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "frames_complete": self.frames_complete,
            "frames_dropped": self.frames_dropped,
            "slices_received": self.slices_received,
            "slices_lost": self.slices_lost,
        }


# --------------------------------------------------------------------------
# Audio frames
# --------------------------------------------------------------------------
#
#   seq          u32   per-stream audio counter, for gap detection
#   capture_ts   u64   source monotonic ns of the packet's first sample
#
# One Opus packet per datagram, never sliced -- a 10 ms frame is a couple of
# hundred bytes at most.
#
_AUDIO_FRAME_STRUCT = struct.Struct("<IQ")
AUDIO_FRAME_HEADER_SIZE = 1 + _AUDIO_FRAME_STRUCT.size


def encode_audio_frame_into(
    buf: bytearray,
    offset: int,
    seq: int,
    capture_ts: int,
    payload: bytes | bytearray | memoryview,
) -> int:
    buf[offset] = PacketType.AUDIO_FRAME
    _AUDIO_FRAME_STRUCT.pack_into(
        buf, offset + 1, seq & 0xFFFFFFFF, capture_ts & 0xFFFFFFFFFFFFFFFF
    )
    end = offset + AUDIO_FRAME_HEADER_SIZE
    buf[end : end + len(payload)] = payload
    return AUDIO_FRAME_HEADER_SIZE + len(payload)


def decode_audio_frame(
    data: bytes | bytearray | memoryview, offset: int
) -> tuple[int, int, memoryview]:
    """Returns ``(seq, capture_ts, payload)``; payload is a view, not a copy."""
    if len(data) - offset < AUDIO_FRAME_HEADER_SIZE:
        raise ValueError("audio frame too short")
    seq, capture_ts = _AUDIO_FRAME_STRUCT.unpack_from(data, offset + 1)
    return seq, capture_ts, memoryview(data)[offset + AUDIO_FRAME_HEADER_SIZE :]


# --------------------------------------------------------------------------
# Clock synchronization
# --------------------------------------------------------------------------
#
# The two machines have independent monotonic clocks, so "how old is this
# frame?" needs an offset between them. The exchange is the NTP one, cut to
# its essentials:
#
#   client sends t0                    (MEDIA_HEARTBEAT)
#   source records t1 on receive,
#          records t2 on send back     (MEDIA_HEARTBEAT_ACK)
#   client records t3 on receive
#
#   offset = ((t1 - t0) + (t2 - t3)) / 2      # source clock - client clock
#   rtt    = (t3 - t0) - (t2 - t1)
#
# The estimate is only as good as the path is symmetric, so samples taken on a
# congested path are discarded rather than averaged in -- see ClockSync.
#
_MEDIA_HB_STRUCT = struct.Struct("<IQ")
MEDIA_HEARTBEAT_SIZE = 1 + _MEDIA_HB_STRUCT.size

_MEDIA_HB_ACK_STRUCT = struct.Struct("<IQQQ")
MEDIA_HEARTBEAT_ACK_SIZE = 1 + _MEDIA_HB_ACK_STRUCT.size


def encode_media_heartbeat_into(buf: bytearray, offset: int, seq: int, t0: int) -> int:
    buf[offset] = PacketType.MEDIA_HEARTBEAT
    _MEDIA_HB_STRUCT.pack_into(buf, offset + 1, seq & 0xFFFFFFFF, t0 & 0xFFFFFFFFFFFFFFFF)
    return MEDIA_HEARTBEAT_SIZE


def decode_media_heartbeat(data: bytes | bytearray | memoryview, offset: int) -> tuple[int, int]:
    """Returns ``(seq, t0)``."""
    if len(data) - offset < MEDIA_HEARTBEAT_SIZE:
        raise ValueError("media heartbeat too short")
    return _MEDIA_HB_STRUCT.unpack_from(data, offset + 1)


def encode_media_heartbeat_ack_into(
    buf: bytearray, offset: int, seq: int, t0: int, t1: int, t2: int
) -> int:
    buf[offset] = PacketType.MEDIA_HEARTBEAT_ACK
    _MEDIA_HB_ACK_STRUCT.pack_into(
        buf,
        offset + 1,
        seq & 0xFFFFFFFF,
        t0 & 0xFFFFFFFFFFFFFFFF,
        t1 & 0xFFFFFFFFFFFFFFFF,
        t2 & 0xFFFFFFFFFFFFFFFF,
    )
    return MEDIA_HEARTBEAT_ACK_SIZE


def decode_media_heartbeat_ack(
    data: bytes | bytearray | memoryview, offset: int
) -> tuple[int, int, int, int]:
    """Returns ``(seq, t0, t1, t2)``."""
    if len(data) - offset < MEDIA_HEARTBEAT_ACK_SIZE:
        raise ValueError("media heartbeat ack too short")
    return _MEDIA_HB_ACK_STRUCT.unpack_from(data, offset + 1)


class ClockSync:
    """Tracks the offset between the source's clock and ours.

    Filters on round-trip time rather than averaging everything: a sample taken
    while a burst was in flight has an asymmetric path, and asymmetry maps
    directly into offset error. The lowest-RTT samples are the ones least
    contaminated, so anything much worse than the best seen is discarded, and
    what survives is smoothed.
    """

    __slots__ = ("_offset_ns", "_rtt_samples", "_best_rtt", "_accepted", "_last_rtt_ns")

    #: How far above the best observed RTT a sample may be and still count.
    TOLERANCE_NS = 2_000_000        # 2 ms
    #: Weight of each new accepted sample.
    ALPHA = 0.1
    #: Samples kept for the rolling minimum, so the estimate can follow a route
    #: change instead of anchoring forever to one lucky early packet.
    WINDOW = 32
    #: Accepted samples before the offset is worth showing to a user.
    LOCK_AFTER = 3

    def __init__(self) -> None:
        self._offset_ns = 0
        self._rtt_samples: list[int] = []
        self._best_rtt = 0
        self._accepted = 0
        self._last_rtt_ns = 0

    def add_sample(self, t0: int, t1: int, t2: int, t3: int) -> bool:
        """Feed one completed exchange. Returns True if it was accepted."""
        rtt = (t3 - t0) - (t2 - t1)
        if rtt < 0:
            return False    # clocks stepped, or a forged reply

        self._last_rtt_ns = rtt
        self._rtt_samples.append(rtt)
        if len(self._rtt_samples) > self.WINDOW:
            self._rtt_samples.pop(0)
        self._best_rtt = min(self._rtt_samples)

        if self._accepted and rtt > self._best_rtt + self.TOLERANCE_NS:
            return False

        offset = ((t1 - t0) + (t2 - t3)) // 2
        if self._accepted == 0:
            self._offset_ns = offset
        else:
            self._offset_ns += int(self.ALPHA * (offset - self._offset_ns))
        self._accepted += 1
        return True

    @property
    def offset_ns(self) -> int:
        """Add to a source timestamp to express it on our clock."""
        return self._offset_ns

    @property
    def rtt_ms(self) -> float:
        return self._last_rtt_ns / 1_000_000

    @property
    def best_rtt_ms(self) -> float:
        return self._best_rtt / 1_000_000

    @property
    def locked(self) -> bool:
        return self._accepted >= self.LOCK_AFTER

    def reset(self) -> None:
        """Drop everything. Offsets are only valid within one media session."""
        self._offset_ns = 0
        self._rtt_samples.clear()
        self._best_rtt = 0
        self._accepted = 0
        self._last_rtt_ns = 0

    def snapshot(self) -> dict[str, float | int | bool]:
        return {
            "offset_ms": round(self._offset_ns / 1_000_000, 3),
            "rtt_ms": round(self.rtt_ms, 3),
            "best_rtt_ms": round(self.best_rtt_ms, 3),
            "locked": self.locked,
            "samples": self._accepted,
        }


# --------------------------------------------------------------------------
# Keyframe requests
# --------------------------------------------------------------------------
#
#   last_frame_id  u32   highest frame fully received, for the source's logs
#   reason         u8    see IdrReason
#
_IDR_REQUEST_STRUCT = struct.Struct("<IB")
IDR_REQUEST_SIZE = 1 + _IDR_REQUEST_STRUCT.size


class IdrReason:
    LOSS = 1
    JOIN = 2
    DECODER_ERROR = 3


def encode_idr_request_into(buf: bytearray, offset: int, last_frame_id: int, reason: int) -> int:
    buf[offset] = PacketType.IDR_REQUEST
    _IDR_REQUEST_STRUCT.pack_into(buf, offset + 1, last_frame_id & 0xFFFFFFFF, reason & 0xFF)
    return IDR_REQUEST_SIZE


def decode_idr_request(data: bytes | bytearray | memoryview, offset: int) -> tuple[int, int]:
    """Returns ``(last_frame_id, reason)``."""
    if len(data) - offset < IDR_REQUEST_SIZE:
        raise ValueError("idr request too short")
    return _IDR_REQUEST_STRUCT.unpack_from(data, offset + 1)


# --------------------------------------------------------------------------
# Receiver reports
# --------------------------------------------------------------------------
#
# Sent once a second, fire-and-forget. Feeds the source's per-client display
# and its bitrate governor: the source cannot see loss on the far side of the
# path, so the receiver has to tell it.
#
#   frames_complete   u32
#   frames_dropped    u32
#   slices_received   u32
#   slices_lost       u32
#   decode_p50_x10    u16   milliseconds x 10
#   vlat_p50_x10      u16   capture -> present, milliseconds x 10
#   vlat_p99_x10      u16
#   audio_underruns   u16
#
_MEDIA_REPORT_STRUCT = struct.Struct("<IIIIHHHH")
MEDIA_REPORT_SIZE = 1 + _MEDIA_REPORT_STRUCT.size

_U16_MAX = 0xFFFF


def _ms_x10(value_ms: float) -> int:
    """Milliseconds to the wire's fixed-point, saturating rather than wrapping."""
    scaled = int(round(value_ms * 10))
    if scaled < 0:
        return 0
    return min(scaled, _U16_MAX)


def encode_media_report_into(
    buf: bytearray,
    offset: int,
    frames_complete: int,
    frames_dropped: int,
    slices_received: int,
    slices_lost: int,
    decode_p50_ms: float,
    vlat_p50_ms: float,
    vlat_p99_ms: float,
    audio_underruns: int,
) -> int:
    buf[offset] = PacketType.MEDIA_REPORT
    _MEDIA_REPORT_STRUCT.pack_into(
        buf,
        offset + 1,
        frames_complete & 0xFFFFFFFF,
        frames_dropped & 0xFFFFFFFF,
        slices_received & 0xFFFFFFFF,
        slices_lost & 0xFFFFFFFF,
        _ms_x10(decode_p50_ms),
        _ms_x10(vlat_p50_ms),
        _ms_x10(vlat_p99_ms),
        min(audio_underruns, _U16_MAX),
    )
    return MEDIA_REPORT_SIZE


def decode_media_report(data: bytes | bytearray | memoryview, offset: int) -> dict[str, float | int]:
    """Returns the report as a plain dict, milliseconds already unscaled."""
    if len(data) - offset < MEDIA_REPORT_SIZE:
        raise ValueError("media report too short")
    (
        frames_complete,
        frames_dropped,
        slices_received,
        slices_lost,
        decode_p50,
        vlat_p50,
        vlat_p99,
        audio_underruns,
    ) = _MEDIA_REPORT_STRUCT.unpack_from(data, offset + 1)
    return {
        "frames_complete": frames_complete,
        "frames_dropped": frames_dropped,
        "slices_received": slices_received,
        "slices_lost": slices_lost,
        "decode_p50_ms": decode_p50 / 10,
        "vlat_p50_ms": vlat_p50 / 10,
        "vlat_p99_ms": vlat_p99 / 10,
        "audio_underruns": audio_underruns,
    }


# --------------------------------------------------------------------------
# Video configuration
# --------------------------------------------------------------------------


#: Default UDP port for the media socket. Declared here so the video server,
#: the Bluetooth server and the client all agree; each config module mirrors it
#: with a "must match" comment, as with DEFAULT_PORT.
DEFAULT_VIDEO_PORT = 47810


@dataclass(slots=True)
class VideoSettings:
    """The settings a video source accepts, wherever it is running.

    One shape for both modes: the web GUI edits this, it travels as the
    ``config`` body of VIDEO_CONFIG, and the standalone app persists it. That
    is why it lives in ``common/`` rather than in either app.
    """

    backend: str = "auto"          # auto | dshow | v4l2 | lavfi
    device: str = ""               # capture device name/path; blank means first
    audio_device: str = ""
    test_source: bool = False      # synthesize a test pattern instead of capturing
    width: int = 1280
    height: int = 720
    fps: int = 60
    bitrate_kbps: int = 8000
    encoder: str = "auto"
    gop_s: float = 2.0
    intra_refresh: bool = False
    audio_enabled: bool = True
    audio_bitrate_kbps: int = 96
    preview_enabled: bool = True
    preview_fps: int = 10
    #: Width of the web preview. Height follows the source's aspect ratio.
    #:
    #: This is the one setting that costs the *Bluetooth server* rather than
    #: the video server: preview slices are reassembled on the datapath thread,
    #: the one with a sub-millisecond budget. Affordable because the preview is
    #: only sent while somebody has it open -- see `preview_enabled`.
    preview_width: int = 640
    relay_bitrate_kbps: int = 3000
    probe_devices: bool = False

    def to_dict(self) -> dict[str, object]:
        from dataclasses import asdict

        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object] | None) -> VideoSettings:
        """Build from an untrusted dict, dropping unknown keys.

        Same discipline as the config loaders: a field we do not recognise is
        ignored rather than fatal, so an older peer can talk to a newer one.
        """
        if not isinstance(data, dict):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def clamped(self) -> VideoSettings:
        """A copy with every field forced into a sane range.

        Applied on receipt rather than on send: the sender may be a browser
        form, and an encoder handed a 0x0 frame size fails in ways that are
        much harder to read than a clamp.
        """
        return VideoSettings(
            backend=self.backend if self.backend in _BACKENDS else "auto",
            device=str(self.device)[:256],
            audio_device=str(self.audio_device)[:256],
            test_source=bool(self.test_source),
            # Even dimensions: every H.264 profile we use is 4:2:0 subsampled.
            width=_clamp_even(self.width, 160, 3840),
            height=_clamp_even(self.height, 120, 2160),
            fps=_clamp_int(self.fps, 5, 120),
            bitrate_kbps=_clamp_int(self.bitrate_kbps, 500, 50_000),
            encoder=str(self.encoder)[:32] or "auto",
            gop_s=min(max(float(self.gop_s), 0.25), 10.0),
            intra_refresh=bool(self.intra_refresh),
            audio_enabled=bool(self.audio_enabled),
            audio_bitrate_kbps=_clamp_int(self.audio_bitrate_kbps, 16, 320),
            preview_enabled=bool(self.preview_enabled),
            preview_fps=_clamp_int(self.preview_fps, 1, 30),
            # Capped at 1280: beyond that a JPEG stops fitting the frame cap at
            # any sensible quality, and it is a monitoring picture, not the
            # stream -- players get the real thing over their own socket.
            preview_width=_clamp_even(self.preview_width, 160, 1280),
            relay_bitrate_kbps=_clamp_int(self.relay_bitrate_kbps, 500, 20_000),
            probe_devices=bool(self.probe_devices),
        )


_BACKENDS = frozenset({"auto", "dshow", "v4l2", "lavfi"})


def _clamp_int(value: object, low: int, high: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return low
    return min(max(number, low), high)


def _clamp_even(value: object, low: int, high: int) -> int:
    return _clamp_int(value, low, high) & ~1


@dataclass(slots=True)
class MediaStats:
    """Rolling per-client counters kept by a video source."""

    frames_sent: int = 0
    slices_sent: int = 0
    bytes_sent: int = 0
    send_failures: int = 0
    idr_requests: int = 0
    last_report: dict[str, float | int] = field(default_factory=dict)

    def snapshot(self) -> dict[str, object]:
        return {
            "frames_sent": self.frames_sent,
            "slices_sent": self.slices_sent,
            "bytes_sent": self.bytes_sent,
            "send_failures": self.send_failures,
            "idr_requests": self.idr_requests,
            "report": dict(self.last_report),
        }

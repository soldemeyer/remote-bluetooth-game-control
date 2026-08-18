"""Media wire format: slicing, reassembly, and clock synchronization.

The bugs this guards against are the ones that look like something else:

  * A frame that reassembles in the wrong order decodes to garbage, which
    reads as "the encoder is broken" rather than "the assembler is".
  * An assembler that waits for stragglers adds latency to every frame behind
    the lost one, so the stream is smooth but late -- the exact failure this
    whole project exists to avoid.
  * A clock-sync estimate poisoned by one congested sample makes the latency
    display confidently wrong, which is worse than showing nothing.
"""

from __future__ import annotations

import pytest

from common import protocol
from common.video import (
    MAX_SLICE_COUNT,
    VIDEO_SLICE_HEADER_SIZE,
    VIDEO_SLICE_PAYLOAD,
    ClockSync,
    FrameAssembler,
    IdrReason,
    MediaCodec,
    SliceFlags,
    VideoSettings,
    decode_audio_frame,
    decode_idr_request,
    decode_media_heartbeat,
    decode_media_heartbeat_ack,
    decode_media_report,
    decode_video_slice,
    encode_audio_frame_into,
    encode_idr_request_into,
    encode_media_heartbeat_ack_into,
    encode_media_heartbeat_into,
    encode_media_report_into,
    encode_video_slice_into,
    slice_count_for,
)

# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def test_media_tags_occupy_their_reserved_range():
    for tag in (
        protocol.PacketType.VIDEO_FRAME,
        protocol.PacketType.AUDIO_FRAME,
        protocol.PacketType.MEDIA_HEARTBEAT,
        protocol.PacketType.MEDIA_HEARTBEAT_ACK,
        protocol.PacketType.IDR_REQUEST,
        protocol.PacketType.MEDIA_REPORT,
    ):
        assert protocol.is_media_tag(tag)

    # The tags that existed before must not have wandered into the range.
    for tag in (
        protocol.PacketType.INPUT,
        protocol.PacketType.FEEDBACK,
        protocol.PacketType.SESSION,
        protocol.PacketType.PUNCH,
    ):
        assert not protocol.is_media_tag(tag)


def test_a_full_slice_fits_one_datagram_after_encryption():
    # 25 bytes of AEAD framing: SESSION tag, nonce counter, Poly1305 tag.
    assert VIDEO_SLICE_HEADER_SIZE + VIDEO_SLICE_PAYLOAD + 25 <= protocol.MAX_DATAGRAM


def test_video_slice_round_trip():
    buf = bytearray(protocol.MAX_DATAGRAM)
    payload = bytes(range(256)) * 4

    size = encode_video_slice_into(
        buf, 0, 7, 2, 5, SliceFlags.KEYFRAME, MediaCodec.H264, 123_456_789, payload
    )
    assert size == VIDEO_SLICE_HEADER_SIZE + len(payload)
    assert buf[0] == protocol.PacketType.VIDEO_FRAME

    frame_id, index, count, flags, codec, capture_ts, view = decode_video_slice(
        bytes(buf[:size]), 0
    )
    assert (frame_id, index, count) == (7, 2, 5)
    assert flags & SliceFlags.KEYFRAME
    assert codec == MediaCodec.H264
    assert capture_ts == 123_456_789
    assert bytes(view) == payload


@pytest.mark.parametrize(
    "size, expected",
    [
        (0, 1),
        (1, 1),
        (VIDEO_SLICE_PAYLOAD, 1),
        (VIDEO_SLICE_PAYLOAD + 1, 2),
        (VIDEO_SLICE_PAYLOAD * 3, 3),
        (VIDEO_SLICE_PAYLOAD * 3 + 1, 4),
    ],
)
def test_slice_count_boundaries(size, expected):
    assert slice_count_for(size) == expected


def test_truncated_slice_raises_rather_than_crashing():
    with pytest.raises(ValueError):
        decode_video_slice(b"\x18\x00\x00", 0)


def test_implausible_slice_count_is_rejected():
    buf = bytearray(protocol.MAX_DATAGRAM)
    encode_video_slice_into(buf, 0, 1, 0, MAX_SLICE_COUNT + 1, 0, MediaCodec.H264, 0, b"x")
    with pytest.raises(ValueError):
        decode_video_slice(bytes(buf[:64]), 0)


def test_slice_index_outside_its_count_is_rejected():
    buf = bytearray(protocol.MAX_DATAGRAM)
    encode_video_slice_into(buf, 0, 1, 5, 3, 0, MediaCodec.H264, 0, b"x")
    with pytest.raises(ValueError):
        decode_video_slice(bytes(buf[:64]), 0)


# --------------------------------------------------------------------------
# Reassembly
# --------------------------------------------------------------------------


def _feed(assembler: FrameAssembler, frame_id: int, payload: bytes, *, keyframe=False,
          order=None, capture_ts=1000):
    """Slice ``payload`` and feed it, optionally in a given slice order."""
    count = slice_count_for(len(payload))
    indices = list(range(count)) if order is None else order
    result = None
    for index in indices:
        chunk = payload[index * VIDEO_SLICE_PAYLOAD : (index + 1) * VIDEO_SLICE_PAYLOAD]
        completed = assembler.add(
            frame_id,
            index,
            count,
            SliceFlags.KEYFRAME if keyframe else SliceFlags.NONE,
            MediaCodec.H264,
            capture_ts,
            chunk,
        )
        if completed is not None:
            result = completed
    return result


def test_single_slice_frame_completes_immediately():
    assembler = FrameAssembler()
    frame = _feed(assembler, 1, b"hello", keyframe=True)
    assert frame is not None
    assert frame.data == b"hello"
    assert frame.keyframe
    assert frame.capture_ts == 1000
    assert assembler.frames_complete == 1


def test_multi_slice_frame_reassembles_in_order():
    assembler = FrameAssembler()
    payload = bytes(i % 251 for i in range(VIDEO_SLICE_PAYLOAD * 3 + 17))
    frame = _feed(assembler, 42, payload)
    assert frame is not None
    assert frame.data == payload


def test_out_of_order_slices_still_reassemble_correctly():
    assembler = FrameAssembler()
    payload = bytes(i % 251 for i in range(VIDEO_SLICE_PAYLOAD * 4))
    frame = _feed(assembler, 9, payload, order=[3, 0, 2, 1])
    assert frame is not None
    assert frame.data == payload


def test_duplicate_slices_are_ignored():
    assembler = FrameAssembler()
    payload = b"a" * (VIDEO_SLICE_PAYLOAD + 10)
    frame = _feed(assembler, 3, payload, order=[0, 0, 0, 1])
    assert frame is not None
    assert frame.data == payload


def test_a_newer_frame_supersedes_an_incomplete_one():
    """Waiting for stragglers would delay every frame behind the lost one."""
    assembler = FrameAssembler()
    payload = b"z" * (VIDEO_SLICE_PAYLOAD * 3)

    # Frame 1: deliver only its first slice.
    count = slice_count_for(len(payload))
    assembler.add(1, 0, count, 0, MediaCodec.H264, 500, payload[:VIDEO_SLICE_PAYLOAD])
    assert assembler.frames_complete == 0

    # Frame 2 arrives whole; frame 1 is abandoned rather than held.
    frame = _feed(assembler, 2, payload, capture_ts=600)
    assert frame is not None
    assert frame.frame_id == 2
    assert frame.capture_ts == 600
    assert assembler.frames_dropped == 1
    assert assembler.gap_detected()


def test_stragglers_from_an_abandoned_frame_do_not_corrupt_the_next():
    assembler = FrameAssembler()
    payload = b"q" * (VIDEO_SLICE_PAYLOAD * 2)
    count = slice_count_for(len(payload))

    assembler.add(10, 0, count, 0, MediaCodec.H264, 1, payload[:VIDEO_SLICE_PAYLOAD])
    # Start frame 11, then let a late slice of frame 10 arrive.
    assembler.add(11, 0, count, 0, MediaCodec.H264, 2, payload[:VIDEO_SLICE_PAYLOAD])
    assert assembler.add(10, 1, count, 0, MediaCodec.H264, 1, payload[VIDEO_SLICE_PAYLOAD:]) is None

    frame = assembler.add(11, 1, count, 0, MediaCodec.H264, 2, payload[VIDEO_SLICE_PAYLOAD:])
    assert frame is not None
    assert frame.frame_id == 11
    assert frame.data == payload


def test_gap_detected_clears_after_reading():
    assembler = FrameAssembler()
    payload = b"w" * (VIDEO_SLICE_PAYLOAD * 2)
    count = slice_count_for(len(payload))
    assembler.add(1, 0, count, 0, MediaCodec.H264, 0, payload[:VIDEO_SLICE_PAYLOAD])
    _feed(assembler, 2, payload)

    assert assembler.gap_detected() is True
    assert assembler.gap_detected() is False


def test_a_clean_stream_reports_no_gap():
    assembler = FrameAssembler()
    for frame_id in range(1, 6):
        assert _feed(assembler, frame_id, b"frame") is not None
    assert assembler.gap_detected() is False
    assert assembler.frames_dropped == 0


def test_frame_id_wraparound_is_handled():
    """A u32 wrap must not stall the stream permanently."""
    assembler = FrameAssembler()
    high = 0xFFFFFFFE
    assert _feed(assembler, high, b"before") is not None
    frame = _feed(assembler, 1, b"after")     # wrapped past 0xFFFFFFFF
    assert frame is not None
    assert frame.data == b"after"


def test_oversized_frames_are_refused():
    assembler = FrameAssembler(max_frame_size=4096)
    assert assembler.add(1, 0, 500, 0, MediaCodec.H264, 0, b"x") is None
    assert assembler.frames_complete == 0


def test_a_frame_that_exactly_fills_the_cap_is_accepted():
    """The rejection test must compare slice counts, not the bytes they could
    hold: the last slice is usually partial, so multiplying out over-estimates
    and silently drops frames just under the limit. At a high bitrate that is a
    real 1080p keyframe, discarded with every counter looking healthy."""
    from common.video import MAX_FRAME_SIZE

    assembler = FrameAssembler()
    payload = b"\x5a" * MAX_FRAME_SIZE
    frame = _feed(assembler, 1, payload)
    assert frame is not None
    assert len(frame.data) == MAX_FRAME_SIZE


def test_a_duplicate_of_a_delivered_frame_is_not_delivered_twice():
    assembler = FrameAssembler()
    assert _feed(assembler, 5, b"only-frame") is not None
    assert _feed(assembler, 5, b"only-frame") is None
    assert assembler.frames_complete == 1


def test_a_straggler_after_delivery_does_not_start_a_new_frame():
    assembler = FrameAssembler()
    payload = b"m" * (VIDEO_SLICE_PAYLOAD * 2)
    count = slice_count_for(len(payload))
    assembler.add(7, 0, count, 0, MediaCodec.H264, 0, payload[:VIDEO_SLICE_PAYLOAD])
    frame = assembler.add(7, 1, count, 0, MediaCodec.H264, 0, payload[VIDEO_SLICE_PAYLOAD:])
    assert frame is not None

    # A retransmitted slice of the frame we just handed over.
    assert assembler.add(
        7, 0, count, 0, MediaCodec.H264, 0, payload[:VIDEO_SLICE_PAYLOAD]
    ) is None
    assert assembler.frames_complete == 1


def test_a_newer_frame_still_starts_after_a_delivery():
    """The duplicate guard must not block the next real frame."""
    assembler = FrameAssembler()
    assert _feed(assembler, 1, b"first") is not None
    assert _feed(assembler, 2, b"second") is not None
    assert assembler.frames_complete == 2


def test_keyframe_flag_survives_a_reordered_first_slice():
    """The flag rides every slice; whichever arrives first must set it."""
    assembler = FrameAssembler()
    payload = b"k" * (VIDEO_SLICE_PAYLOAD * 2)
    count = slice_count_for(len(payload))
    assembler.add(
        1, 1, count, SliceFlags.KEYFRAME, MediaCodec.H264, 0, payload[VIDEO_SLICE_PAYLOAD:]
    )
    frame = assembler.add(
        1, 0, count, SliceFlags.KEYFRAME, MediaCodec.H264, 0, payload[:VIDEO_SLICE_PAYLOAD]
    )
    assert frame is not None
    assert frame.keyframe


# --------------------------------------------------------------------------
# Audio, heartbeats, requests, reports
# --------------------------------------------------------------------------


def test_audio_frame_round_trip():
    buf = bytearray(512)
    opus = bytes(range(120))
    size = encode_audio_frame_into(buf, 0, 99, 777, opus)
    seq, capture_ts, payload = decode_audio_frame(bytes(buf[:size]), 0)
    assert (seq, capture_ts) == (99, 777)
    assert bytes(payload) == opus


def test_media_heartbeat_round_trip():
    buf = bytearray(64)
    size = encode_media_heartbeat_into(buf, 0, 5, 111)
    assert decode_media_heartbeat(bytes(buf[:size]), 0) == (5, 111)

    size = encode_media_heartbeat_ack_into(buf, 0, 5, 111, 222, 333)
    assert decode_media_heartbeat_ack(bytes(buf[:size]), 0) == (5, 111, 222, 333)


def test_idr_request_round_trip():
    buf = bytearray(64)
    size = encode_idr_request_into(buf, 0, 4242, IdrReason.LOSS)
    assert decode_idr_request(bytes(buf[:size]), 0) == (4242, IdrReason.LOSS)


def test_media_report_round_trip():
    buf = bytearray(64)
    size = encode_media_report_into(buf, 0, 100, 3, 900, 12, 4.25, 18.5, 44.1, 2)
    report = decode_media_report(bytes(buf[:size]), 0)
    assert report["frames_complete"] == 100
    assert report["frames_dropped"] == 3
    assert report["slices_lost"] == 12
    assert report["decode_p50_ms"] == pytest.approx(4.2, abs=0.11)
    assert report["vlat_p99_ms"] == pytest.approx(44.1, abs=0.11)
    assert report["audio_underruns"] == 2


def test_media_report_saturates_rather_than_wrapping():
    """A pathological latency must not come back as a small number."""
    buf = bytearray(64)
    size = encode_media_report_into(buf, 0, 0, 0, 0, 0, 99_999.0, 0.0, 0.0, 99_999)
    report = decode_media_report(bytes(buf[:size]), 0)
    assert report["decode_p50_ms"] == pytest.approx(6553.5)
    assert report["audio_underruns"] == 0xFFFF


@pytest.mark.parametrize(
    "decoder, payload",
    [
        (decode_audio_frame, b"\x19\x00"),
        (decode_media_heartbeat, b"\x1a"),
        (decode_media_heartbeat_ack, b"\x1b\x00\x00"),
        (decode_idr_request, b"\x1c"),
        (decode_media_report, b"\x1d\x00"),
    ],
)
def test_truncated_media_packets_raise_value_error(decoder, payload):
    with pytest.raises(ValueError):
        decoder(payload, 0)


# --------------------------------------------------------------------------
# Clock synchronization
# --------------------------------------------------------------------------


def _exchange(sync: ClockSync, offset_ns: int, one_way_ns: int) -> bool:
    """One symmetric exchange with a known offset, in client-clock terms."""
    t0 = 1_000_000_000
    t1 = t0 + one_way_ns + offset_ns     # source clock at receive
    t2 = t1 + 1_000                      # source turnaround
    t3 = t0 + 2 * one_way_ns + 1_000     # client clock at reply
    return sync.add_sample(t0, t1, t2, t3)


def test_clock_sync_recovers_a_known_offset():
    sync = ClockSync()
    offset = 5_000_000_000
    for _ in range(5):
        assert _exchange(sync, offset, 1_000_000)
    assert sync.locked
    assert sync.offset_ns == pytest.approx(offset, abs=100_000)


def test_clock_sync_needs_several_samples_before_locking():
    sync = ClockSync()
    assert not sync.locked
    _exchange(sync, 0, 1_000_000)
    assert not sync.locked
    _exchange(sync, 0, 1_000_000)
    _exchange(sync, 0, 1_000_000)
    assert sync.locked


def test_a_congested_sample_is_discarded():
    """Asymmetric delay maps straight into offset error, so filter on RTT."""
    sync = ClockSync()
    for _ in range(4):
        _exchange(sync, 1_000_000_000, 1_000_000)
    good = sync.offset_ns

    # Same offset, but 50 ms of queueing -- far outside the tolerance.
    assert _exchange(sync, 1_000_000_000, 50_000_000) is False
    assert sync.offset_ns == good


def test_a_negative_round_trip_is_rejected():
    sync = ClockSync()
    # t3 before t0 cannot happen on one monotonic clock: forged or stepped.
    assert sync.add_sample(1000, 0, 0, 500) is False
    assert not sync.locked


def test_reset_clears_the_estimate():
    sync = ClockSync()
    for _ in range(4):
        _exchange(sync, 7_000_000, 1_000_000)
    assert sync.locked
    sync.reset()
    assert not sync.locked
    assert sync.offset_ns == 0


def test_clock_sync_snapshot_is_json_friendly():
    sync = ClockSync()
    _exchange(sync, 1_000_000, 500_000)
    snap = sync.snapshot()
    assert set(snap) == {"offset_ms", "rtt_ms", "best_rtt_ms", "locked", "samples"}
    assert isinstance(snap["locked"], bool)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def test_settings_round_trip_through_a_dict():
    settings = VideoSettings(width=1920, height=1080, fps=30, bitrate_kbps=12_000)
    restored = VideoSettings.from_dict(settings.to_dict())
    assert restored == settings


def test_unknown_settings_keys_are_dropped():
    """A newer peer's extra field must not break an older one."""
    restored = VideoSettings.from_dict({"width": 640, "future_thing": "???"})
    assert restored.width == 640


def test_settings_from_garbage_gives_defaults():
    assert VideoSettings.from_dict(None) == VideoSettings()
    assert VideoSettings.from_dict("not a dict") == VideoSettings()  # type: ignore[arg-type]


def test_clamping_forces_sane_encoder_inputs():
    clamped = VideoSettings(
        width=99999, height=0, fps=1000, bitrate_kbps=-5, backend="nonsense"
    ).clamped()
    assert clamped.width == 3840
    assert clamped.height == 120
    assert clamped.fps == 120
    assert clamped.bitrate_kbps == 500
    assert clamped.backend == "auto"


def test_clamped_dimensions_stay_even():
    """4:2:0 subsampling cannot represent an odd dimension."""
    clamped = VideoSettings(width=1281, height=721).clamped()
    assert clamped.width % 2 == 0
    assert clamped.height % 2 == 0

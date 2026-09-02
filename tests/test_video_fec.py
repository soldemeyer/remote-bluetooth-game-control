"""Phase 9: one parity slice per frame, and what it can and cannot save.

Measured before building it, 1280x720@60 at ~11 slices a frame:

    slice loss   decoded fps   keyframes in 25 s   decoder resets
      0.0%          60.0             13                 0
      0.5%          51.6             40                72
      1.0%          45.7             47               121
      2.0%          29.7             49               295

Half a percent of slice loss cost **14% of the frame rate**. A frame dies to a
single missing slice, the broken reference chain kills the frames behind it,
and the keyframe requested to repair it is the largest and most loss-prone
frame there is -- so loss begets loss.

XOR parity was chosen over retransmission because it needs **no round trip**:
it works identically at 1 ms and at 100 ms of RTT, where a NACK's usefulness
depends entirely on whether the reply beats the frame's display deadline.
"""

from __future__ import annotations

import os

from common.video import (
    VIDEO_SLICE_PAYLOAD,
    FrameAssembler,
    MediaCodec,
    SliceFlags,
    build_parity,
    slice_count_for,
)


def _slices(payload: bytes) -> list[bytes]:
    count = slice_count_for(len(payload))
    return [
        payload[i * VIDEO_SLICE_PAYLOAD : (i + 1) * VIDEO_SLICE_PAYLOAD]
        for i in range(count)
    ]


def _deliver(
    assembler: FrameAssembler,
    payload: bytes,
    *,
    drop: set[int] | None = None,
    frame_id: int = 1,
    protect: bool = True,
):
    """Push one frame through, optionally losing some slices."""
    drop = drop or set()
    data = _slices(payload)
    flags = SliceFlags.KEYFRAME | (SliceFlags.FEC if protect else 0)

    parts = list(data)
    if protect:
        parts.append(build_parity(payload, len(data)))

    result = None
    for index, chunk in enumerate(parts):
        if index in drop:
            continue
        got = assembler.add(
            frame_id, index, len(parts), flags, MediaCodec.H264, 12345, chunk
        )
        if got is not None:
            result = got
    return result


class TestParityRecoversOneLostSlice:
    def test_a_middle_slice_comes_back_byte_for_byte(self):
        payload = os.urandom(VIDEO_SLICE_PAYLOAD * 5 + 137)
        assembler = FrameAssembler()
        frame = _deliver(assembler, payload, drop={2})
        assert frame is not None, "a single lost slice should have been rebuilt"
        assert frame.data == payload
        assert assembler.recovered == 1

    def test_the_first_slice_comes_back(self):
        payload = os.urandom(VIDEO_SLICE_PAYLOAD * 4 + 900)
        assembler = FrameAssembler()
        frame = _deliver(assembler, payload, drop={0})
        assert frame is not None and frame.data == payload

    def test_the_last_data_slice_comes_back_at_its_true_length(self):
        """The one case that needs the length carried in the parity header.

        Every data slice is full length except the last, and XOR needs equal
        operands -- so the parity is computed over padded slices. Rebuilding
        the last one without its true length appends zeros and corrupts the end
        of the frame, which decodes to garbage rather than failing.
        """
        payload = os.urandom(VIDEO_SLICE_PAYLOAD * 3 + 11)   # a very short tail
        data = _slices(payload)
        assembler = FrameAssembler()
        frame = _deliver(assembler, payload, drop={len(data) - 1})
        assert frame is not None
        assert len(frame.data) == len(payload), "the rebuilt tail was the wrong length"
        assert frame.data == payload

    def test_losing_only_the_parity_costs_nothing(self):
        payload = os.urandom(VIDEO_SLICE_PAYLOAD * 4 + 5)
        data = _slices(payload)
        assembler = FrameAssembler()
        frame = _deliver(assembler, payload, drop={len(data)})   # the parity slice
        assert frame is not None and frame.data == payload
        assert assembler.recovered == 0, "nothing needed rebuilding"

    def test_a_single_slice_frame_still_works(self):
        payload = b"tiny"
        assembler = FrameAssembler()
        frame = _deliver(assembler, payload, drop={0})
        assert frame is not None and frame.data == payload


class TestItDoesNotClaimMoreThanItCan:
    """A wrong frame is worse than a missing one: it decodes to garbage."""

    def test_two_lost_data_slices_are_not_recoverable(self):
        payload = os.urandom(VIDEO_SLICE_PAYLOAD * 5 + 40)
        assembler = FrameAssembler()
        assert _deliver(assembler, payload, drop={1, 3}) is None
        assert assembler.recovered == 0

    def test_a_lost_data_slice_and_the_parity_are_not_recoverable(self):
        payload = os.urandom(VIDEO_SLICE_PAYLOAD * 4 + 40)
        data = _slices(payload)
        assembler = FrameAssembler()
        assert _deliver(assembler, payload, drop={1, len(data)}) is None
        assert assembler.recovered == 0

    def test_an_unprotected_frame_still_needs_every_slice(self):
        payload = os.urandom(VIDEO_SLICE_PAYLOAD * 3 + 40)
        assembler = FrameAssembler()
        assert _deliver(assembler, payload, drop={1}, protect=False) is None


class TestTheUnprotectedPathIsUnchanged:
    def test_a_clean_unprotected_frame_assembles(self):
        payload = os.urandom(VIDEO_SLICE_PAYLOAD * 3 + 77)
        assembler = FrameAssembler()
        frame = _deliver(assembler, payload, protect=False)
        assert frame is not None and frame.data == payload
        assert assembler.recovered == 0

    def test_protected_and_unprotected_frames_can_interleave(self):
        """The flag is per frame, so switching FEC on mid-stream must be safe."""
        assembler = FrameAssembler()
        first = os.urandom(VIDEO_SLICE_PAYLOAD * 2 + 10)
        second = os.urandom(VIDEO_SLICE_PAYLOAD * 2 + 10)
        third = os.urandom(VIDEO_SLICE_PAYLOAD * 2 + 10)

        assert _deliver(assembler, first, frame_id=1, protect=False).data == first
        assert _deliver(assembler, second, frame_id=2, protect=True).data == second
        assert _deliver(assembler, third, frame_id=3, protect=False).data == third


class TestTheParityItself:
    def test_it_fits_in_one_datagram(self):
        from common import protocol
        from common.video import VIDEO_SLICE_HEADER_SIZE

        parity = build_parity(os.urandom(VIDEO_SLICE_PAYLOAD * 8), 8)
        on_the_wire = VIDEO_SLICE_HEADER_SIZE + len(parity) + 25   # AEAD
        assert on_the_wire <= protocol.MAX_DATAGRAM

    def test_it_is_the_xor_of_the_data(self):
        a = bytes([0b1010] * VIDEO_SLICE_PAYLOAD)
        b = bytes([0b0110] * VIDEO_SLICE_PAYLOAD)
        parity = build_parity(a + b, 2)
        body = parity[2:]                       # past the length header
        assert body[0] == 0b1100

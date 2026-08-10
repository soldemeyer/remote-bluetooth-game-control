"""Wire protocol round-trip and edge-case tests.

Everything downstream depends on these codecs being exactly right, so this
covers the boundary values (int16 extremes, u32 wrap) rather than just the
happy path.
"""

from __future__ import annotations

import pytest

from common import protocol
from common.protocol import (
    INPUT_PACKET_SIZE,
    InputFlags,
    PacketType,
    RejectReason,
    ReplayWindow,
    decode_control,
    decode_control_ack,
    decode_heartbeat,
    decode_input_ack,
    decode_input_into,
    encode_control,
    encode_control_ack,
    encode_heartbeat_ack_into,
    encode_heartbeat_into,
    encode_input_ack_into,
    encode_input_into,
    seq_is_newer,
)
from common.state import AXIS_MAX, AXIS_MIN, Button, ControllerState


def test_input_round_trip_preserves_all_fields():
    state = ControllerState(
        buttons=Button.A | Button.DPAD_LEFT | Button.RIGHT_BUMPER,
        left_x=-12345,
        left_y=987,
        right_x=32767,
        right_y=-32768,
        left_trigger=200,
        right_trigger=17,
    )
    buf = bytearray(64)
    written = encode_input_into(buf, 0, 42, 1234567890123, 2, InputFlags.REQUEST_ACK, state)
    assert written == INPUT_PACKET_SIZE
    assert buf[0] == PacketType.INPUT

    out = ControllerState()
    seq, ts, slot, flags = decode_input_into(buf, 0, out)

    assert (seq, ts, slot, flags) == (42, 1234567890123, 2, InputFlags.REQUEST_ACK)
    assert out == state


def test_input_round_trip_at_axis_extremes():
    """int16 boundaries are where a wrong struct format shows up."""
    state = ControllerState(
        left_x=AXIS_MIN, left_y=AXIS_MAX, right_x=AXIS_MIN, right_y=AXIS_MAX,
        left_trigger=0, right_trigger=255, buttons=0xFFFFFFF,
    )
    buf = bytearray(64)
    encode_input_into(buf, 0, 0, 0, 0, 0, state)
    out = ControllerState()
    decode_input_into(buf, 0, out)
    assert out == state


def test_input_encode_at_nonzero_offset():
    """The transport writes packets after a crypto header, so offset must work."""
    state = ControllerState(buttons=Button.Y, left_x=100)
    buf = bytearray(64)
    encode_input_into(buf, 8, 7, 99, 1, 0, state)

    out = ControllerState()
    seq, ts, slot, _ = decode_input_into(buf, 8, out)
    assert (seq, ts, slot) == (7, 99, 1)
    assert out.buttons == Button.Y
    assert out.left_x == 100


def test_decode_input_rejects_truncated_packet():
    with pytest.raises(ValueError, match="too short"):
        decode_input_into(bytearray(10), 0, ControllerState())


def test_seq_field_wraps_without_error():
    """u32 masking must not raise on overflow."""
    state = ControllerState()
    buf = bytearray(64)
    encode_input_into(buf, 0, 2**32 + 5, 0, 0, 0, state)
    seq, _, _, _ = decode_input_into(buf, 0, ControllerState())
    assert seq == 5


def test_input_ack_round_trip():
    buf = bytearray(64)
    n = encode_input_ack_into(buf, 0, 99, 111, 222, 333, 3)
    assert buf[0] == PacketType.INPUT_ACK
    assert n == 1 + 8 + 8 + 8 + 4 + 1

    seq, cts, rts, bts, slot = decode_input_ack(buf, 0)
    assert (seq, cts, rts, bts, slot) == (99, 111, 222, 333, 3)


def test_heartbeat_round_trip():
    buf = bytearray(32)
    encode_heartbeat_into(buf, 0, 5, 123456)
    assert buf[0] == PacketType.HEARTBEAT
    assert decode_heartbeat(buf, 0) == (5, 123456)

    encode_heartbeat_ack_into(buf, 0, 6, 654321)
    assert buf[0] == PacketType.HEARTBEAT_ACK
    assert decode_heartbeat(buf, 0) == (6, 654321)


def test_control_round_trip():
    packet = encode_control(3, "set_username", {"slot": 1, "username": "spence"})
    assert packet[0] == PacketType.CONTROL

    seq, body = decode_control(packet, 0)
    assert seq == 3
    assert body == {"op": "set_username", "slot": 1, "username": "spence"}


def test_control_handles_unicode_usernames():
    packet = encode_control(1, "set_username", {"username": "Ω plâyer 日本"})
    _, body = decode_control(packet, 0)
    assert body["username"] == "Ω plâyer 日本"


def test_control_rejects_oversized_message():
    with pytest.raises(ValueError, match="too large"):
        encode_control(1, "set_username", {"username": "x" * 2000})


@pytest.mark.parametrize("payload", [b"\x12\x00\x00\x00\x00not json", b"\x12\x00\x00\x00\x00[]"])
def test_decode_control_rejects_malformed(payload):
    """Anyone can send us a datagram; malformed input must raise, not crash."""
    with pytest.raises(ValueError):
        decode_control(payload, 0)


def test_control_ack_round_trip():
    assert decode_control_ack(encode_control_ack(77), 0) == 77


def test_reject_reasons_all_have_messages():
    for reason in RejectReason:
        assert reason.message()


class TestSeqComparison:
    def test_basic_ordering(self):
        assert seq_is_newer(5, 4)
        assert not seq_is_newer(4, 5)
        assert not seq_is_newer(5, 5)

    def test_wraparound(self):
        """At the u32 boundary a naive > would stall the stream permanently."""
        assert seq_is_newer(0, 2**32 - 1)
        assert seq_is_newer(5, 2**32 - 10)
        assert not seq_is_newer(2**32 - 1, 0)


class TestReplayWindow:
    def test_accepts_fresh_sequences(self):
        w = ReplayWindow()
        for seq in range(10):
            assert w.check_and_update(seq), f"seq {seq} should be fresh"

    def test_rejects_exact_replay(self):
        w = ReplayWindow()
        assert w.check_and_update(5)
        assert not w.check_and_update(5)

    def test_accepts_out_of_order_within_window(self):
        """UDP reorders routinely; in-window old packets are legitimate."""
        w = ReplayWindow()
        assert w.check_and_update(10)
        assert w.check_and_update(8)
        assert w.check_and_update(9)
        assert not w.check_and_update(8)

    def test_rejects_too_old(self):
        w = ReplayWindow()
        assert w.check_and_update(1000)
        assert not w.check_and_update(1000 - ReplayWindow.WINDOW)
        assert not w.check_and_update(1)

    def test_large_jump_resets_window(self):
        w = ReplayWindow()
        assert w.check_and_update(1)
        assert w.check_and_update(10_000)
        assert not w.check_and_update(5000)
        assert w.check_and_update(10_001)

    def test_survives_wraparound(self):
        w = ReplayWindow()
        assert w.check_and_update(2**32 - 2)
        assert w.check_and_update(2**32 - 1)
        assert w.check_and_update(0)
        assert w.check_and_update(1)
        assert not w.check_and_update(0)


def test_packet_type_values_are_unique():
    values = [t.value for t in PacketType]
    assert len(values) == len(set(values))


def test_input_packet_is_small_enough_to_be_cheap():
    """Guards against someone growing the hot-path packet carelessly."""
    assert INPUT_PACKET_SIZE <= 32
    assert protocol.MAX_DATAGRAM <= 1200

"""Turning a decoded Opus frame into bytes the sink can play.

The bug this exists for: the client did ``bytes(frame.planes[0])``.

FFmpeg sizes an audio plane for the **largest frame the codec can produce**,
and for Opus that is 120 ms -- 23040 bytes at 48 kHz stereo s16. A 10 ms packet
carries 1920 bytes of audio inside that buffer, so handing the whole plane to
the sink played 10 ms of sound followed by 110 ms of padding, forever.

Nothing raises, no counter moves, and the padding is normally zeroed, so the
only symptom is that the audio is wrong: mostly silence, with fragments. It was
reported as "I hear nothing or just crackling".
"""

from __future__ import annotations

import pytest

pytest.importorskip("av", reason="video extras not installed")

from client.media.audio import (  # noqa: E402
    BYTES_PER_FRAME,
    SAMPLE_RATE,
    AudioPlayout,
)

LAYOUT = "stereo"
PACKET_MS = 10
SAMPLES_PER_PACKET = SAMPLE_RATE * PACKET_MS // 1000       # 480


def opus_packets(count: int = 12) -> list[bytes]:
    """Real Opus packets, made the way the video server makes them."""
    import av

    encoder = av.CodecContext.create("libopus", "w")
    encoder.sample_rate = SAMPLE_RATE
    encoder.format = "s16"
    encoder.layout = LAYOUT
    encoder.bit_rate = 96000
    encoder.options = {"application": "lowdelay", "frame_duration": "10"}
    encoder.open()

    resampler = av.AudioResampler(format="s16", layout=LAYOUT, rate=SAMPLE_RATE)
    container = av.open("sine=frequency=440:sample_rate=48000", format="lavfi")
    stream = container.streams.audio[0]

    packets: list[bytes] = []
    try:
        for packet in container.demux(stream):
            for frame in packet.decode():
                for out in resampler.resample(frame):
                    out.pts = None
                    for encoded in encoder.encode(out):
                        packets.append(bytes(encoded))
            if len(packets) >= count:
                break
    finally:
        container.close()
    return packets[:count]


def _playout() -> AudioPlayout:
    """A playout with its decoder built, but no thread and no device."""
    import av

    playout = AudioPlayout(sink=object())
    playout._decoder = av.CodecContext.create("libopus", "r")
    playout._decoder.sample_rate = SAMPLE_RATE
    playout._decoder.format = "s16"
    playout._decoder.layout = LAYOUT
    return playout


class TestOnlyRealAudioIsBuffered:
    def test_a_packet_yields_exactly_its_own_samples(self):
        playout = _playout()

        playout.feed(opus_packets(1)[0], capture_ts=0)

        expected = SAMPLES_PER_PACKET * BYTES_PER_FRAME      # 1920
        assert playout._buffered_bytes == expected, (
            f"buffered {playout._buffered_bytes} bytes for {PACKET_MS} ms of "
            f"audio; the plane is sized for Opus's 120 ms maximum, so anything "
            f"larger than {expected} is padding being played as sound"
        )

    def test_a_second_of_packets_buffers_a_second_of_audio(self):
        playout = _playout()
        playout._buffer.clear()

        packets = opus_packets(10)
        total = 0
        for data in packets:
            playout._buffer.clear()
            playout._buffered_bytes = 0
            playout.feed(data, capture_ts=0)
            total += playout._buffered_bytes

        expected = len(packets) * SAMPLES_PER_PACKET * BYTES_PER_FRAME
        assert total == expected, f"{total} bytes for {expected} expected"

    def test_nothing_is_dropped_on_the_floor_either(self):
        """The slice must not be shorter than the audio, which would gap it."""
        playout = _playout()
        playout.feed(opus_packets(1)[0], capture_ts=0)

        assert playout._buffered_bytes >= SAMPLES_PER_PACKET * BYTES_PER_FRAME

    def test_decoding_reports_no_errors(self):
        playout = _playout()
        for data in opus_packets(6):
            playout.feed(data, capture_ts=0)
        assert playout.decode_errors == 0


class TestTheRawPlaneIsNotUsable:
    """Guards the reason `_pcm_from` exists, so it is not simplified back."""

    def test_the_plane_is_far_larger_than_the_frame(self):
        import av

        decoder = av.CodecContext.create("libopus", "r")
        decoder.sample_rate = SAMPLE_RATE
        decoder.format = "s16"
        decoder.layout = LAYOUT

        for data in opus_packets(1):
            for frame in decoder.decode(av.Packet(data)):
                plane = len(bytes(frame.planes[0]))
                real = frame.samples * BYTES_PER_FRAME
                assert plane > real, (
                    "this build sizes the plane to the frame, so the original "
                    "code would have worked and this comment is now wrong"
                )
                # Roughly 12x, being 120 ms of capacity for 10 ms of audio.
                assert plane >= real * 4
                return
        pytest.fail("the decoder produced no frames")


class TestAnUnexpectedLayoutIsConverted:
    def test_planar_input_is_made_packed(self):
        """libopus gives packed s16 today; a planar frame must not be halved."""
        import av

        playout = _playout()
        frame = av.AudioFrame(format="s16p", layout="stereo", samples=480)
        for plane in frame.planes:
            plane.update(b"\x01" * plane.buffer_size)
        frame.sample_rate = SAMPLE_RATE

        pcm = playout._pcm_from(frame)

        assert len(pcm) == 480 * BYTES_PER_FRAME, (
            "a planar frame yielded one channel's worth, which plays at half "
            "speed in the wrong ear"
        )

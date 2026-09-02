"""Audio loss concealment: a lost packet must not shorten the timeline.

Video gained parity-based recovery in Phase 9; audio had **nothing**. A lost
Opus packet simply did not arrive, the gap counter went up, and the playout
carried on with the next packet -- so 10 ms of audio vanished and everything
after it moved 10 ms earlier. Two faults from one packet: an audible
discontinuity, and a timeline that shortens on every loss, dragging audio
steadily ahead of video over a lossy session.

Opus's own mechanisms were measured and found unreachable through PyAV:
`fec=1` is accepted by the encoder but using it needs `decode_fec`, which is
not exposed; and decoding an empty packet returns no frames, so libopus is
never asked to conceal. Packet-level redundancy was rejected separately -- the
copy arrives after the packet that follows it, and reordering this byte deque
would cost a permanent 10 ms to fix a once-a-second event.
"""

from __future__ import annotations

from client.media.audio import (
    BYTES_PER_MS,
    AudioPlayout,
    _MAX_CONCEAL_PACKETS,
    _ms_to_bytes,
)

FRAME_MS = 10
FRAME_BYTES = _ms_to_bytes(FRAME_MS)


class _Sink:
    def bytes_free(self) -> int:
        return 0

    def bytes_queued(self) -> int:
        return 0

    def write(self, data: bytes) -> None:
        pass


def _playout() -> AudioPlayout:
    playout = AudioPlayout(sink=_Sink(), target_ms=30)
    # A frame of real-looking audio to conceal from.
    playout._last_pcm = bytes([0x40, 0x10] * (FRAME_BYTES // 2))
    return playout


class TestAGapIsFilled:
    def test_one_lost_packet_is_covered(self):
        playout = _playout()
        playout._note_seq(10)
        before = playout.buffered_ms

        missing = playout._note_seq(12)          # 11 never arrived
        assert missing == 1
        playout._conceal(missing)

        assert playout.buffered_ms > before, "the gap was left in the stream"
        assert playout.concealed_ms == FRAME_MS

    def test_the_timeline_keeps_its_length(self):
        """The point of concealing at all: audio must not run early.

        Without this, every lost packet moves all following audio 10 ms
        earlier -- invisible to every counter, and cumulative.
        """
        playout = _playout()
        playout._note_seq(0)
        for index in range(1, 20):
            missing = playout._note_seq(index * 2)   # lose every other packet
            playout._conceal(missing)
            playout._enqueue(playout._last_pcm)

        # 19 delivered packets and 19 concealed ones.
        assert playout.packets_lost == 19
        assert playout.concealed_ms == 19 * FRAME_MS
        assert playout.buffered_ms == 38 * FRAME_MS

    def test_it_fades_rather_than_repeating_at_full_volume(self):
        """A fragment repeated three times at full volume is its own artifact."""
        playout = _playout()
        playout._conceal(3)

        import array

        chunks = list(playout._buffer)
        assert len(chunks) == 3
        peaks = []
        for chunk in chunks:
            samples = array.array("h")
            samples.frombytes(chunk)
            peaks.append(max(abs(value) for value in samples))
        assert peaks[0] > peaks[1] > peaks[2], f"not fading: {peaks}"
        assert peaks[-1] < peaks[0]


class TestItDoesNotInventTooMuch:
    def test_a_long_outage_is_not_papered_over(self):
        """Filling a long gap would add exactly as much latency as it invented.

        A long outage genuinely is a discontinuity; pretending otherwise trades
        a gap the listener already heard for permanent delay.
        """
        playout = _playout()
        playout._note_seq(0)
        missing = playout._note_seq(500)          # a five-second hole
        playout._conceal(missing)

        assert playout.concealed_ms == _MAX_CONCEAL_PACKETS * FRAME_MS
        assert playout.gaps_too_long == 1

    def test_nothing_is_invented_before_any_audio_has_played(self):
        """There is nothing to conceal *from* at the start of a stream."""
        playout = AudioPlayout(sink=_Sink(), target_ms=30)
        playout._conceal(2)
        assert playout.buffered_ms == 0
        assert playout.concealed_ms == 0

    def test_an_intact_stream_conceals_nothing(self):
        playout = _playout()
        for index in range(50):
            missing = playout._note_seq(index)
            assert missing == 0
            playout._conceal(missing)
        assert playout.concealed_ms == 0
        assert playout.gaps_too_long == 0


class TestTheSequenceLogicStillHolds:
    """Concealment keys off `_note_seq`, so its edge cases now matter more."""

    def test_a_duplicate_conceals_nothing(self):
        playout = _playout()
        playout._note_seq(5)
        assert playout._note_seq(5) == 0
        assert playout.duplicates == 1

    def test_a_reordered_packet_conceals_nothing(self):
        """Otherwise a late arrival would invent audio to cover itself."""
        playout = _playout()
        playout._note_seq(10)
        assert playout._note_seq(9) == 0
        assert playout.reordered == 1

    def test_it_survives_the_counter_wrapping(self):
        playout = _playout()
        playout._note_seq(0xFFFFFFFE)
        assert playout._note_seq(0) == 1          # 0xFFFFFFFF was lost
        assert playout.packets_lost == 1


class TestItIsReported:
    def test_the_snapshot_carries_the_concealment(self):
        playout = _playout()
        playout._note_seq(1)
        playout._conceal(playout._note_seq(4))
        snap = playout.snapshot()
        assert snap["concealed_ms"] > 0
        assert "gaps_too_long" in snap
        # "the path is lossy" must be distinguishable from "the path is fine"
        # without inferring it from how the audio sounds.
        assert snap["packets_lost"] == 2

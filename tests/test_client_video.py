"""Client playback: decode, present, and the latency the player is shown.

Two failure classes worth guarding, both of which look like a broken stream
rather than a broken client:

  * A QImage built with the wrong stride. The scaler pads rows, so a picture
    built on width*3 shears diagonally -- it looks like corruption on the wire.
  * A decoder that never recovers from an error. One bad frame freezes the
    picture permanently unless a fresh keyframe is requested.
"""

from __future__ import annotations

import os
import time

import pytest

av = pytest.importorskip("av", reason="video extras not installed")
pytest.importorskip("PySide6", reason="client GUI extras not installed")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from common.timing import LatencyStats, now_ns  # noqa: E402
from common.video import CompletedFrame, MediaCodec  # noqa: E402
from client.media.decoder import VideoDecoder  # noqa: E402
from client.net.video import VideoStreamState  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def encode_frames(count: int = 12, width: int = 320, height: int = 240) -> list[bytes]:
    """Produce a short real H.264 stream, Annex B with in-band parameter sets."""
    from fractions import Fraction

    source = av.open(
        f"testsrc=size={width}x{height}:rate=30", format="lavfi"
    )
    pictures = []
    for frame in source.decode(source.streams.video[0]):
        pictures.append(frame.reformat(format="yuv420p"))
        if len(pictures) >= count:
            break

    ctx = av.CodecContext.create("libx264", "w")
    ctx.width, ctx.height = width, height
    ctx.pix_fmt = "yuv420p"
    ctx.framerate = 30
    ctx.time_base = Fraction(1, 30)
    ctx.gop_size = 30
    ctx.max_b_frames = 0
    ctx.options = {"preset": "ultrafast", "tune": "zerolatency"}
    ctx.open()

    packets: list[bytes] = []
    for index, picture in enumerate(pictures):
        picture.pts = index
        picture.time_base = ctx.time_base
        picture.pict_type = av.video.frame.PictureType.NONE
        for packet in ctx.encode(picture):
            packets.append(bytes(packet))
    for packet in ctx.encode(None):
        packets.append(bytes(packet))
    return packets


class FakeReceiver:
    """Stands in for VideoReceiver: hands out frames, records requests."""

    def __init__(self, payloads: list[bytes]) -> None:
        self._payloads = list(payloads)
        self.decode_stats = LatencyStats()
        self.present_stats = LatencyStats()
        self.idr_requests = 0
        self.clock_offset_ns = 0
        self.clock_locked = True
        self.connection_mode = "direct"
        self.state = VideoStreamState.STREAMING
        self.state_detail = ""
        self._frame_id = 0

    def get_frame(self, timeout: float = 0.1):
        if not self._payloads:
            time.sleep(0.01)
            return None
        data = self._payloads.pop(0)
        self._frame_id += 1
        return CompletedFrame(
            frame_id=self._frame_id,
            keyframe=self._frame_id == 1,
            codec=MediaCodec.H264,
            capture_ts=now_ns(),
            data=data,
        )

    def request_idr(self, reason: int = 0) -> None:
        self.idr_requests += 1


def drain(decoder: VideoDecoder, want: int, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if decoder.frames_decoded >= want:
            return True
        time.sleep(0.02)
    return False


class TestDecoder:
    def test_frames_decode_and_are_published(self):
        receiver = FakeReceiver(encode_frames())
        decoder = VideoDecoder(receiver)
        decoder.start()
        try:
            assert drain(decoder, 5), "decoder produced nothing"
            frame = decoder.latest()
            assert frame is not None
            assert (frame.width, frame.height) == (320, 240)
            assert decoder.version > 0
        finally:
            decoder.stop()

    def test_the_published_stride_matches_the_buffer(self):
        """A QImage built on width*3 instead of the real stride shears."""
        receiver = FakeReceiver(encode_frames())
        decoder = VideoDecoder(receiver)
        decoder.start()
        try:
            assert drain(decoder, 3)
            frame = decoder.latest()
            assert frame is not None
            assert frame.stride >= frame.width * 3
            assert len(frame.data) >= frame.stride * frame.height
        finally:
            decoder.stop()

    def test_version_advances_with_each_decoded_frame(self):
        """The window repaints only when this changes, so it must track frames.

        Both checkpoints are fixed numbers well inside what was encoded. An
        earlier version asked for "two more than we have now", which is
        unreachable once the decoder has already drained the queue -- and under
        a loaded machine it always had, so the test passed alone and failed in
        a full run.
        """
        payloads = encode_frames(count=12)
        receiver = FakeReceiver(payloads)
        decoder = VideoDecoder(receiver)
        decoder.start()
        try:
            assert drain(decoder, 3)
            first_version = decoder.version
            assert first_version > 0

            assert drain(decoder, 9)
            assert decoder.version > first_version
        finally:
            decoder.stop()

        # Compared only once the thread has stopped. The two counters are
        # bumped a couple of statements apart, so reading them while decoding
        # continues can legitimately catch them one out of step -- an
        # assertion made mid-flight fails at random.
        assert decoder.version == decoder.frames_decoded

    def test_frames_that_decode_to_nothing_provoke_a_keyframe_request(self):
        """The silent failure: no exception, no picture, and a frozen window.

        A P-frame whose reference is gone decodes to nothing without raising,
        so watching only for exceptions leaves nothing to break the deadlock.
        """
        # Junk that parses as a NAL but references a keyframe we never sent.
        junk = [b"\x00\x00\x00\x01\x41" + b"\x9a\x2b\x7c\x11" * 100 for _ in range(6)]
        receiver = FakeReceiver(junk)
        decoder = VideoDecoder(receiver)
        decoder.start()
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and receiver.idr_requests == 0:
                time.sleep(0.02)
            assert receiver.idr_requests > 0, "never asked for a keyframe"
        finally:
            decoder.stop()

    def test_decoding_resumes_once_a_keyframe_arrives(self):
        """Recovery has to actually work, not just be requested."""
        payloads = [b"\x00\x00\x00\x01\x41" + b"\x77\x31\x0f\x5c" * 80 for _ in range(4)]
        payloads += encode_frames()          # a fresh stream, starting on an IDR

        receiver = FakeReceiver(payloads)
        decoder = VideoDecoder(receiver)
        decoder.start()
        try:
            assert drain(decoder, 4), "decoding never recovered after a keyframe"
            frame = decoder.latest()
            assert frame is not None
            assert (frame.width, frame.height) == (320, 240)
        finally:
            decoder.stop()

    def test_decode_timing_is_recorded(self):
        receiver = FakeReceiver(encode_frames())
        decoder = VideoDecoder(receiver)
        decoder.start()
        try:
            assert drain(decoder, 5)
            assert decoder.decode.count > 0
            assert decoder.decode.p50 >= 0
        finally:
            decoder.stop()


class TestVideoWindow:
    def test_it_paints_a_decoded_frame(self, qapp):
        from client.gui.video_window import VideoWindow

        receiver = FakeReceiver(encode_frames())
        decoder = VideoDecoder(receiver)
        decoder.start()
        try:
            assert drain(decoder, 4)
            window = VideoWindow(decoder, receiver)
            window.resize(640, 360)
            window.show()
            qapp.processEvents()

            window._check_for_frame()
            qapp.processEvents()
            assert window._image is not None
            assert not window._image.isNull()

            window.close()
        finally:
            decoder.stop()

    def test_latency_is_measured_at_presentation(self, qapp):
        from client.gui.video_window import VideoWindow

        receiver = FakeReceiver(encode_frames())
        decoder = VideoDecoder(receiver)
        decoder.start()
        try:
            assert drain(decoder, 4)
            window = VideoWindow(decoder, receiver)
            window._check_for_frame()
            assert receiver.present_stats.count > 0, "no capture-to-present sample"
            assert receiver.present_stats.p50 > 0
            window.close()
        finally:
            decoder.stop()

    def test_the_overlay_reports_all_three_figures(self, qapp):
        from client.gui.video_window import VideoWindow

        receiver = FakeReceiver([])
        receiver.present_stats.add(18.0)
        decoder = VideoDecoder(receiver)
        window = VideoWindow(decoder, receiver)
        window.set_controller_rtt(24.0)

        lines = window.osd_lines()
        text = "\n".join(lines)
        assert "video" in text
        assert "controller" in text
        assert "combined" in text
        # Half the round trip plus one-way video: 12 + 18.
        assert "30.0" in text
        assert "excl. console" in text
        window.close()

    def test_the_overlay_says_so_before_the_clock_locks(self, qapp):
        from client.gui.video_window import VideoWindow

        receiver = FakeReceiver([])
        receiver.clock_locked = False
        decoder = VideoDecoder(receiver)
        window = VideoWindow(decoder, receiver)
        assert "syncing" in window.osd_lines()[0]
        window.close()

    def test_a_relayed_path_is_called_out(self, qapp):
        from client.gui.video_window import VideoWindow

        receiver = FakeReceiver([])
        receiver.connection_mode = "relay"
        decoder = VideoDecoder(receiver)
        window = VideoWindow(decoder, receiver)
        assert any("relayed" in line for line in window.osd_lines())
        window.close()

    def test_fullscreen_toggles(self, qapp):
        from client.gui.video_window import VideoWindow

        receiver = FakeReceiver([])
        decoder = VideoDecoder(receiver)
        window = VideoWindow(decoder, receiver)
        window.show()
        qapp.processEvents()

        window.toggle_fullscreen()
        qapp.processEvents()
        assert window.isFullScreen()

        window.toggle_fullscreen()
        qapp.processEvents()
        assert not window.isFullScreen()
        window.close()

    def test_the_overlay_can_be_hidden(self, qapp):
        from client.gui.video_window import VideoWindow

        receiver = FakeReceiver([])
        decoder = VideoDecoder(receiver)
        window = VideoWindow(decoder, receiver)
        assert window._show_osd is True
        window.toggle_osd()
        assert window._show_osd is False
        window.close()

    def test_it_shows_the_stream_state_before_any_frame(self, qapp):
        from client.gui.video_window import VideoWindow

        receiver = FakeReceiver([])
        receiver.state = VideoStreamState.CONNECTING
        decoder = VideoDecoder(receiver)
        window = VideoWindow(decoder, receiver)
        window.show()
        qapp.processEvents()
        # No frame yet, so nothing to paint but the state message.
        assert window._image is None
        window.close()


class TestAudioPlayout:
    def test_the_buffer_holds_back_its_target(self):
        """Playing everything immediately defeats the point of a jitter buffer."""
        from client.media.audio import AudioPlayout, _ms_to_bytes

        playout = AudioPlayout(sink=object(), target_ms=30)
        playout._enqueue(b"\x00" * _ms_to_bytes(10))
        assert playout._take(9999) is None, "played audio it should have held"

        playout._enqueue(b"\x00" * _ms_to_bytes(40))
        chunk = playout._take(9999)
        assert chunk is not None and len(chunk) > 0

    def test_filling_the_buffer_is_not_an_underrun(self):
        """Counting it as one injects silence throughout normal playback.

        The buffer sits below its target every time it is refilling, which is
        most of the time. Treating that as a fault drives it to the cap (where
        real audio is dropped) and reports a loss rate back to the source.
        """
        from client.media.audio import AudioPlayout, _ms_to_bytes

        playout = AudioPlayout(sink=object(), target_ms=30)
        playout._enqueue(b"\x00" * _ms_to_bytes(10))

        assert playout._take(9999) is None
        assert playout._is_empty() is False, (
            "a partially filled buffer must not look empty"
        )

    def test_a_genuinely_empty_buffer_reads_as_empty(self):
        from client.media.audio import AudioPlayout

        playout = AudioPlayout(sink=object(), target_ms=30)
        assert playout._is_empty() is True

    def test_an_overrun_drops_the_oldest_audio(self):
        """Only once the burst headroom is gone -- not at the target.

        The cap used to be MAX_TARGET_MS, which left nothing above the largest
        target to absorb a late burst; +/-40 ms of jitter then discarded 17% of
        the audio. It is now target + BURST_HEADROOM_MS, so far more has to
        pile up before anything is thrown away.
        """
        from client.media.audio import (
            BURST_HEADROOM_MS,
            DEFAULT_TARGET_MS,
            AudioPlayout,
            _ms_to_bytes,
        )

        cap_ms = DEFAULT_TARGET_MS + BURST_HEADROOM_MS
        playout = AudioPlayout(sink=object())
        for _ in range(int(cap_ms / 10) + 10):
            playout._enqueue(b"\x00" * _ms_to_bytes(10))

        assert playout.overruns > 0, "the buffer grew without bound"
        assert playout.buffered_ms <= cap_ms + 10

    def test_a_burst_of_late_audio_is_kept_not_dropped(self):
        """The regression that made the stream choppy on a real network.

        A WiFi stall delivers its backlog all at once. Discarding it is heard
        as chopping, and it was measured at 17% of the audio.
        """
        from client.media.audio import AudioPlayout, _ms_to_bytes

        playout = AudioPlayout(sink=object(), target_ms=30)
        for _ in range(10):                      # 100 ms arriving together
            playout._enqueue(b"\x00" * _ms_to_bytes(10))

        assert playout.overruns == 0
        assert playout.buffered_ms >= 95

    def test_the_governor_shortens_the_buffer_when_audio_lags(self):
        from client.media.audio import AudioPlayout

        playout = AudioPlayout(sink=object(), target_ms=40)
        playout._audio_latency_ms = 120.0        # audio well behind video
        playout.tick_sync(video_latency_ms=20.0)
        assert playout.target_ms < 40

    def test_the_governor_ignores_drift_inside_the_audible_threshold(self):
        from client.media.audio import AudioPlayout

        playout = AudioPlayout(sink=object(), target_ms=30)
        playout._audio_latency_ms = 45.0
        playout.tick_sync(video_latency_ms=30.0)
        assert playout.target_ms == 30

    def test_the_buffer_never_leaves_its_bounds(self):
        from client.media.audio import MAX_TARGET_MS, MIN_TARGET_MS, AudioPlayout

        playout = AudioPlayout(sink=object(), target_ms=30)
        playout._audio_latency_ms = 500.0
        for _ in range(50):
            playout._last_governor_ns = 0
            playout.tick_sync(video_latency_ms=10.0)
        assert playout.target_ms >= MIN_TARGET_MS

        playout._audio_latency_ms = 5.0
        for _ in range(50):
            playout._last_governor_ns = 0
            playout.tick_sync(video_latency_ms=200.0)
        assert playout.target_ms <= MAX_TARGET_MS

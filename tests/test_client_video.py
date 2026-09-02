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

    def __init__(self, payloads: list[bytes], pace_s: float = 0.0) -> None:
        self._payloads = list(payloads)
        #: Seconds to hold each frame back. Zero for tests that just want
        #: frames out fast; non-zero where a test needs the decoder not to
        #: outrun its own checkpoints -- see
        #: test_version_advances_with_each_decoded_frame.
        self._pace_s = pace_s
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
        if self._pace_s:
            time.sleep(self._pace_s)
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
            assert len(frame.pixels) >= frame.stride * frame.height
            # The pixels must be a view onto the frame that owns them, not a
            # copy: copying 6.22 MB at 1080p holds the GIL long enough to
            # disturb the 500 Hz input loop in this same process.
            assert frame.owner is not None
            assert isinstance(frame.pixels, memoryview)
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
        # Paced, so the decoder cannot drain the whole queue before the
        # first checkpoint is read. Without this the test asserts that a
        # counter advances between two points it has already run past --
        # which is a property of how fast the decoder happens to be, not of
        # the behaviour under test. It surfaced the moment the decoder got
        # faster (the per-frame RGB copy was removed), having previously
        # passed for no better reason than that it was slow enough.
        payloads = encode_frames(count=12)
        receiver = FakeReceiver(payloads, pace_s=0.02)
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


class TestScalingHappensInTheDecoder:
    """The scale belongs where the GIL is released, not where it is held.

    ``QPainter.drawImage`` holds the GIL while it scales; swscale does not.
    Measured against a 500 Hz canary at the input loop's own rate, painting
    1080p into a 1280x720 window cost that loop 1.81 ms at p99 against 0.51 ms
    for a 1:1 blit. The client runs its input loop in this same process, so
    that difference lands directly on controller tail latency.
    """

    def test_frames_arrive_at_the_requested_size(self):
        receiver = FakeReceiver(encode_frames())
        decoder = VideoDecoder(receiver)
        decoder.set_viewport(160, 120)
        decoder.start()
        try:
            assert drain(decoder, 3)
            frame = decoder.latest()
            assert frame is not None
            assert (frame.width, frame.height) == (160, 120)
        finally:
            decoder.stop()

    def test_the_aspect_ratio_is_preserved(self):
        """Fit inside the viewport, never fill it -- the window letterboxes."""
        receiver = FakeReceiver(encode_frames())      # 320x240, 4:3
        decoder = VideoDecoder(receiver)
        decoder.set_viewport(800, 240)                # much wider than 4:3
        decoder.start()
        try:
            assert drain(decoder, 3)
            frame = decoder.latest()
            assert frame is not None
            assert frame.height == 240
            # 4:3 inside an 800x240 box is 320 wide, not 800.
            assert frame.width == 320
        finally:
            decoder.stop()

    def test_no_viewport_means_the_streams_own_size(self):
        receiver = FakeReceiver(encode_frames())
        decoder = VideoDecoder(receiver)
        decoder.start()
        try:
            assert drain(decoder, 3)
            frame = decoder.latest()
            assert frame is not None
            assert (frame.width, frame.height) == (320, 240)
        finally:
            decoder.stop()

    def test_clearing_the_viewport_goes_back_to_native(self):
        """Closing the window must not leave the stream scaled to it."""
        # Paced, so there are still frames left to decode after the viewport
        # is cleared -- otherwise this asserts on a queue already drained.
        receiver = FakeReceiver(encode_frames(count=16), pace_s=0.02)
        decoder = VideoDecoder(receiver)
        decoder.set_viewport(160, 120)
        decoder.start()
        try:
            assert drain(decoder, 3)
            assert decoder.latest().width == 160
            decoder.set_viewport(0, 0)
            before = decoder.frames_decoded
            assert drain(decoder, before + 3)
            assert decoder.latest().width == 320
        finally:
            decoder.stop()

    def test_the_window_tells_the_decoder_its_size(self, qapp):
        """End to end: a sized window must produce a 1:1 paint."""
        from client.gui.video_window import VideoWindow

        receiver = FakeReceiver(encode_frames(count=16), pace_s=0.02)
        decoder = VideoDecoder(receiver)
        window = VideoWindow(decoder, receiver)
        # Comfortably above the window's 320x180 minimum: asking for less than
        # that is silently clamped, and the frame would then correctly be
        # larger than the size requested.
        window.resize(640, 360)
        window.show()
        qapp.processEvents()

        decoder.start()
        try:
            assert drain(decoder, 4)
            frame = decoder.latest()
            assert frame is not None
            ratio = window._device_ratio()
            # Physical pixels, so at ratio 1 this is the widget's own size.
            # Fits inside it, and fills one axis -- which is what makes the
            # paint a blit rather than a scale.
            assert frame.width <= int(window.width() * ratio) + 2
            assert frame.height <= int(window.height() * ratio) + 2
            assert (
                frame.width >= int(window.width() * ratio) - 2
                or frame.height >= int(window.height() * ratio) - 2
            ), "the frame should fill one axis of the viewport"
        finally:
            decoder.stop()
            window.close()


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

            window._on_frame_ready()
            qapp.processEvents()
            assert window._image is not None
            assert not window._image.isNull()

            window.close()
        finally:
            decoder.stop()

    def test_latency_is_measured_at_the_paint_not_at_pickup(self, qapp):
        """The sample must appear when the frame is *drawn*, never before.

        This is the defect this test exists for. The old window stamped
        capture-to-present inside the timer callback, before ``update()`` was
        even called, so the one end-to-end figure the player is shown, and the
        one the receiver report carries back to the source, excluded the paint,
        the backing-store flush and the compositor entirely.

        Asserting "a sample exists after taking the frame up" is exactly what
        let that ship, so the assertion is the other way round now: taking the
        frame up must record nothing, and painting must record it.
        """
        from client.gui.video_window import VideoWindow

        receiver = FakeReceiver(encode_frames())
        decoder = VideoDecoder(receiver)
        decoder.start()
        try:
            assert drain(decoder, 4)
            window = VideoWindow(decoder, receiver)
            window.resize(320, 240)

            window._on_frame_ready()
            assert receiver.present_stats.count == 0, (
                "a latency sample was recorded before the frame was painted"
            )

            window.show()
            qapp.processEvents()
            window.repaint()

            assert receiver.present_stats.count > 0, "no capture-to-paint sample"
            assert receiver.present_stats.p50 > 0
            assert window.paint_stats.count > 0, "the paint itself was not timed"
            window.close()
        finally:
            decoder.stop()

    def test_a_repaint_without_a_new_frame_is_not_a_latency_sample(self, qapp):
        """A resize or an expose is real work but not a new picture.

        Counting them would fill the window with samples measuring how old a
        picture that had not changed was, which drags the median toward the
        repaint rate rather than the frame rate.
        """
        from client.gui.video_window import VideoWindow

        receiver = FakeReceiver(encode_frames())
        decoder = VideoDecoder(receiver)
        decoder.start()
        try:
            assert drain(decoder, 4)
            window = VideoWindow(decoder, receiver)
            window.resize(320, 240)
            window.show()
            qapp.processEvents()

            window._on_frame_ready()
            window.repaint()
            after_first = receiver.present_stats.count
            assert after_first > 0

            # No new frame taken up; just paint again.
            window.repaint()
            window.repaint()
            assert receiver.present_stats.count == after_first
            # The paint cost is still measured -- that work really happened.
            assert window.paint_stats.count > after_first
            window.close()
        finally:
            decoder.stop()

    def test_the_decoder_notifies_instead_of_being_polled(self, qapp):
        """A published frame must reach the window without waiting for a timer.

        The safety timer runs at 100 ms; if delivery depended on it, a frame
        would wait up to that long. The signal is what makes it prompt, so the
        test drives the decoder and lets only the event loop run.
        """
        from client.gui.video_window import VideoWindow

        receiver = FakeReceiver(encode_frames())
        decoder = VideoDecoder(receiver)
        window = VideoWindow(decoder, receiver)
        window.resize(320, 240)
        window.show()
        qapp.processEvents()

        assert decoder._listener is not None, "the window did not subscribe"

        decoder.start()
        try:
            assert drain(decoder, 4)
            deadline = time.monotonic() + 2.0
            while window._image is None and time.monotonic() < deadline:
                qapp.processEvents()
            assert window._image is not None, "the frame never arrived by signal"
        finally:
            decoder.stop()

        # Closing must hand the listener back, because the decoder outlives
        # the window and would otherwise keep a way to call into it.
        window.close()
        assert decoder._listener is None

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


class _SilentSink:
    """A sink with room and nothing queued, so `_pump_once` reaches its body.

    The older doubles answer `bytes_free()` with 0, which makes the playout
    loop return on its first branch -- which is why nothing in this file ever
    exercised the loop at all.
    """

    def bytes_free(self) -> int:
        return 1 << 20

    def bytes_queued(self) -> int:
        return 0

    def write(self, data: bytes) -> None:
        pass


class TestAudioPlayout:
    def test_the_reserve_is_held_where_the_device_can_reach_it(self):
        """Replaces a test that pinned the bug it was meant to prevent.

        It used to assert `_take` returned None while the deque held less than
        the target -- which is exactly the hold-back that put the reserve in
        the deque, where the audio device cannot reach it. `_buffered_bytes`
        then had a floor it could never cross, so the buffer never read empty,
        the underrun counter never moved again, and on real hardware the device
        sat at 0 ms twice a second while `buffered_ms` reported a healthy 30.

        `_take` now hands over what it is asked for; how much to ask for is
        `_pump_once`'s decision, because only it knows what the device already
        holds. `tests/test_client_audio_playout.py` measures the result against
        a sink that actually drains.
        """
        from client.media.audio import AudioPlayout, _ms_to_bytes

        playout = AudioPlayout(sink=object(), target_ms=30)
        playout._enqueue(bytes(_ms_to_bytes(10)))

        chunk = playout._take(_ms_to_bytes(10))
        assert chunk is not None and len(chunk) == _ms_to_bytes(10), (
            "the buffer withheld audio the device was asking for"
        )
        assert playout._is_empty(), "the buffer cannot be drained to empty"

    def test_the_buffer_primes_before_it_plays(self):
        """Withholding is right in exactly one place: before playback starts.

        There is nothing to play yet, and starting early only guarantees
        running out again a moment later. It is a one-time state, not a
        permanent subtraction from every read.
        """
        from client.media.audio import AudioPlayout, _ms_to_bytes

        playout = AudioPlayout(sink=object(), target_ms=30)
        assert playout._priming is True

        playout._enqueue(bytes(_ms_to_bytes(10)))
        playout._pump_once(_SilentSink())
        assert playout._priming is True, "primed on less than the target"

        playout._enqueue(bytes(_ms_to_bytes(40)))
        playout._pump_once(_SilentSink())
        assert playout._priming is False, "never left priming despite a full buffer"

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

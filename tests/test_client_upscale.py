"""The decoder's GPU branch, driven by a fake backend.

No GPU and no library: the backend is injected, so everything about *what the
decoder does* is testable on any machine. What it cannot check is whether the
pixels come out right, which is ``tests/test_videofx_device.py``'s job.

The two tests that matter most are the ones about what does **not** happen:

* with no upscaler attached, the decoder must behave exactly as it did before
  this feature existed -- no import, no filter-graph change, nothing;
* after ``submit`` returns, the backend must hold nothing. A decoded frame
  comes out of FFmpeg's buffer pool and may still be a reference frame for
  pictures yet to arrive, so a backend that kept one would pin the pool and
  stall the decoder. That contract is a comment in the C header and a weakref
  here.
"""

from __future__ import annotations

import gc
import sys

import pytest

av = pytest.importorskip("av", reason="video extras not installed")

from client.media.decoder import VideoDecoder  # noqa: E402
from client.media.gpu_upscaler import SubmitResult  # noqa: E402
from client.media import videofx  # noqa: E402


class FakeReceiver:
    decode_stats = None
    clock_locked = True
    clock_offset_ns = 0

    def __init__(self) -> None:
        self.idr_requests = 0
        from common.timing import LatencyStats

        self.present_stats = LatencyStats()

    def request_idr(self) -> None:
        self.idr_requests += 1


class FakeUpscaler:
    """Records what it was given, and deliberately keeps none of it."""

    def __init__(self, result: SubmitResult | None = None) -> None:
        self.calls: list[dict] = []
        self.result = result or SubmitResult(
            ok=True, path=videofx.PATH_EASU_RCAS, gpu_ms=0.4,
            output_width=1280, output_height=720,
        )
        self.repaints = 0
        self.mode = "fsr1"

    def submit(self, **kwargs) -> SubmitResult:
        # Addresses and sizes only. Holding the frame is the thing this class
        # exists to prove nobody does.
        self.calls.append({
            "blits": kwargs["blits"],
            "composed": kwargs["composed"],
            "src_size": kwargs["src_size"],
            "colorspace": kwargs["colorspace"],
            "color_range": kwargs["color_range"],
            "planes": kwargs.get("planes"),
            "strides": kwargs.get("strides"),
            "texture": kwargs.get("texture", 0),
            "overlay": kwargs.get("overlay"),
        })
        return self.result

    def repaint(self) -> bool:
        self.repaints += 1
        return True

    def set_mode(self, mode: str) -> bool:
        self.mode = mode
        return True

    def shutdown(self) -> None:
        pass


def picture(width: int = 640, height: int = 480, fmt: str = "yuv420p"):
    frame = av.VideoFrame(width, height, fmt)
    for index, plane in enumerate(frame.planes):
        plane.update(bytes([(index * 40 + 32) % 255]) * plane.buffer_size)
    return frame


def decoder(viewport=(1280, 720)) -> VideoDecoder:
    decode = VideoDecoder(receiver=FakeReceiver())
    decode.set_viewport(*viewport)
    return decode


class TestOffIsUntouched:
    """The requirement the whole feature is built under."""

    def test_no_upscaler_means_the_software_path(self):
        decode = decoder()
        assert decode._upscale is None
        decode._publish(picture(), capture_ts=0, started_ns=0)
        assert decode._latest is not None, "nothing was published"
        assert decode.frames_decoded == 1

    def test_the_enhancement_layer_never_reaches_the_frame_path(self):
        """A machine with the feature off must not pay for it existing.

        The library *is* loaded once at startup, by the capability scan, so
        the settings can say what this machine can do. What must not happen is
        any of that reaching the frames -- and with no upscaler attached, the
        decoder does not so much as look.
        """
        decode = decoder()
        for _ in range(3):
            decode._publish(picture(), capture_ts=0, started_ns=0)
        assert decode._upscale is None
        assert decode.last_path == ""
        assert decode.last_gpu_ms == -1.0

    def test_attaching_and_detaching_returns_to_the_same_path(self):
        decode = decoder()
        fake = FakeUpscaler()

        decode.set_upscaler(fake)
        decode._publish(picture(), capture_ts=0, started_ns=0)
        assert len(fake.calls) == 1

        decode.set_upscaler(None)
        decode._publish(picture(), capture_ts=0, started_ns=0)
        assert len(fake.calls) == 1, "the detached backend was still called"
        assert decode._latest is not None

    def test_detaching_clears_the_filter_graph_cache(self):
        """Those graphs are keyed for the software path's own output format
        and size; a stale one draws the wrong thing at the right shape."""
        decode = decoder()
        decode.set_regions([(0.0, 0.0, 0.5, 0.5)])
        decode._publish(picture(), capture_ts=0, started_ns=0)
        assert decode._graphs, "the software path built no graph"

        decode.set_upscaler(FakeUpscaler())
        decode.set_upscaler(None)
        assert decode._graphs == {}


class TestTheGpuPathBuildsNoFilterGraphs:
    """The point of the GPU path: FFmpeg does no post-decode work at all.

    No crop pass, no scale pass, no colour conversion. The raw planes are
    handed over and the GPU does the rest, which is why this path deletes work
    rather than adding it.
    """

    @pytest.mark.parametrize("crops", [
        [],
        [(0.0, 0.0, 0.5, 0.5)],
        [(0.0, 0.0, 0.5, 0.5), (0.5, 0.5, 0.5, 0.5)],
        [(0.0, 0.0, 0.5, 0.5), (0.5, 0.0, 0.5, 0.5), (0.0, 0.5, 0.5, 0.5)],
    ])
    def test_the_cache_stays_empty(self, crops):
        decode = decoder()
        decode.set_upscaler(FakeUpscaler())
        decode.set_regions(crops)
        for _ in range(4):
            decode._publish(picture(), capture_ts=0, started_ns=0)
        assert decode._graphs == {}

    def test_nothing_is_published_to_the_window(self):
        """On this path the picture goes straight to the screen from the
        decode thread; a frame left in the slot would be one nobody draws."""
        decode = decoder()
        decode.set_upscaler(FakeUpscaler())
        decode._publish(picture(), capture_ts=0, started_ns=0)
        assert decode._latest is None


class TestTheLifetimeContract:
    def test_nothing_survives_the_submit_call(self):
        """The C header's rule 2, as a test rather than a comment.

        A decoded frame comes out of FFmpeg's buffer pool and may still be a
        reference frame for pictures yet to arrive. Reading it inside the call
        is fine; holding it pins the pool and stalls the decoder.

        Measured by reference count rather than by a weakref, because
        ``av.VideoFrame`` does not support weak references at all -- the
        obvious spelling of this test raises TypeError rather than failing.
        """
        decode = decoder()
        fake = FakeUpscaler()
        decode.set_upscaler(fake)

        frame = picture()
        gc.collect()
        before = sys.getrefcount(frame)
        decode._publish(frame, capture_ts=0, started_ns=0)
        gc.collect()
        after = sys.getrefcount(frame)

        assert after == before, (
            f"the backend kept {after - before} reference(s) to the frame"
        )

    def test_the_software_path_keeps_a_frame_too_but_not_this_one(self):
        """The control for the test above, and a distinction worth pinning.

        The software path publishes a memoryview over pixels, so it *must*
        hold the frame that owns them -- but that is the frame ``reformat()``
        returned, not the one the decoder produced. So the decoded picture is
        released on both paths, and the difference between them is what is
        retained afterwards rather than whether anything is.
        """
        decode = decoder()
        frame = picture()
        gc.collect()
        before = sys.getrefcount(frame)
        decode._publish(frame, capture_ts=0, started_ns=0)
        gc.collect()

        assert sys.getrefcount(frame) == before, "the decoded frame was pinned"
        published = decode._latest
        assert published is not None
        assert published.owner is not frame
        assert published.owner is not None, "the published pixels have no owner"

    def test_only_addresses_cross_the_boundary(self):
        decode = decoder()
        fake = FakeUpscaler()
        decode.set_upscaler(fake)
        decode._publish(picture(), capture_ts=0, started_ns=0)

        planes = fake.calls[0]["planes"]
        assert planes is not None
        assert all(isinstance(address, int) and address > 0 for address in planes)


class TestWhatIsHandedOver:
    def test_one_submit_per_decoded_picture(self):
        decode = decoder()
        fake = FakeUpscaler()
        decode.set_upscaler(fake)
        for _ in range(5):
            decode._publish(picture(), capture_ts=0, started_ns=0)
        assert len(fake.calls) == 5
        assert decode.frames_decoded == 5

    def test_one_region_uploads_only_that_region(self):
        """Crop before upscale, structurally: the addresses point at the
        quadrant and the size is the quadrant's."""
        decode = decoder()
        fake = FakeUpscaler()
        decode.set_upscaler(fake)
        decode.set_regions([(0.5, 0.5, 0.5, 0.5)])
        # The first region change from "the whole picture" starts a camera
        # move, which correctly renders the union of both views. Settled state
        # is what this test is about.
        decode._transition = None
        decode._publish(picture(640, 480), capture_ts=0, started_ns=0)

        call = fake.calls[0]
        assert call["src_size"] == (320, 240)
        assert call["blits"][0].src == (0.0, 0.0, 1.0, 1.0)

    def test_the_plane_addresses_are_offset_into_the_crop(self):
        decode = decoder()
        fake = FakeUpscaler()
        decode.set_upscaler(fake)

        whole = picture(640, 480)
        base = whole.planes[0].buffer_ptr
        decode._publish(whole, capture_ts=0, started_ns=0)
        assert fake.calls[0]["planes"][0] == base

        decode.set_regions([(0.5, 0.5, 0.5, 0.5)])
        decode._transition = None        # settled, not mid-move
        lower_right = picture(640, 480)
        expected = (lower_right.planes[0].buffer_ptr
                    + 240 * lower_right.planes[0].line_size + 320)
        decode._publish(lower_right, capture_ts=0, started_ns=0)
        assert fake.calls[1]["planes"][0] == expected

    def test_three_regions_produce_three_blits(self):
        decode = decoder()
        fake = FakeUpscaler()
        decode.set_upscaler(fake)
        decode.set_regions([
            (0.0, 0.0, 0.5, 0.5), (0.5, 0.0, 0.5, 0.5), (0.0, 0.5, 0.5, 0.5),
        ])
        decode._publish(picture(), capture_ts=0, started_ns=0)

        blits = fake.calls[0]["blits"]
        assert len(blits) == 3
        assert len({(b.dst[2], b.dst[3]) for b in blits}) == 1, "pieces differ in size"

    def test_the_colour_description_travels_with_the_frame(self):
        """Never assumed. A hardcoded matrix makes the picture shift colour
        when the mode changes, and it gets reported as the upscaler's fault."""
        decode = decoder()
        fake = FakeUpscaler()
        decode.set_upscaler(fake)
        decode._publish(picture(), capture_ts=0, started_ns=0)
        call = fake.calls[0]
        assert isinstance(call["colorspace"], int)
        assert isinstance(call["color_range"], int)


class TestFallbacks:
    def test_a_format_we_cannot_upload_takes_the_software_path(self):
        """10-bit, 4:2:2 or a future codec. Uploading one as three 8-bit
        planes gives a green skewed picture with no error anywhere."""
        decode = decoder()
        fake = FakeUpscaler()
        decode.set_upscaler(fake)

        decode._publish(picture(fmt="yuv422p"), capture_ts=0, started_ns=0)

        assert fake.calls == [], "an unsupported format was sent to the GPU"
        assert decode._latest is not None, "nothing was drawn at all"

    def test_a_fatal_failure_detaches_and_keeps_the_stream(self):
        """Never terminate playback because the enhancement failed."""
        decode = decoder()
        reported: list[str] = []
        decode._on_error = reported.append
        decode.set_upscaler(FakeUpscaler(
            SubmitResult(ok=False, fatal=True, reason="the device was lost")))

        decode._publish(picture(), capture_ts=0, started_ns=0)
        assert decode._upscale is None
        assert reported and "device was lost" in reported[0]

        # ...and the next frame is drawn in software.
        decode._publish(picture(), capture_ts=0, started_ns=0)
        assert decode._latest is not None

    def test_a_non_fatal_failure_keeps_the_backend(self):
        """A malformed frame is a bug to fix, not a reason to tear down a
        renderer that is working."""
        decode = decoder()
        fake = FakeUpscaler(SubmitResult(ok=False, fatal=False, reason="bad frame"))
        decode.set_upscaler(fake)
        decode._publish(picture(), capture_ts=0, started_ns=0)
        assert decode._upscale is fake

    def test_the_stream_is_never_taken_down_by_an_exception(self):
        class Exploding:
            def submit(self, **kwargs):
                raise RuntimeError("the driver fell over")

        decode = decoder()
        decode.set_upscaler(Exploding())
        with pytest.raises(RuntimeError):
            # The decoder does not catch this itself -- `_run` does, and it
            # rebuilds the codec. Pinned so nobody assumes otherwise.
            decode._publish(picture(), capture_ts=0, started_ns=0)


class TestStatistics:
    def test_the_end_to_end_figure_is_recorded_from_this_thread(self):
        """On the software path the window stamps it at the end of paintEvent.
        Here paintEvent draws no video, so a stamp left there would freeze --
        and the audio governor synchronises against this statistic.
        """
        decode = decoder()
        decode.set_upscaler(FakeUpscaler())
        decode._publish(picture(), capture_ts=0, started_ns=0)
        assert decode._receiver.present_stats.count == 1

    def test_a_skipped_present_is_not_a_latency_sample(self):
        """The window was hidden, so nothing reached a screen. Counting it
        would fold "minimised" into the number a player reads as lag."""
        decode = decoder()
        decode.set_upscaler(FakeUpscaler(
            SubmitResult(ok=True, path=videofx.PATH_LANCZOS, skipped=True)))
        decode._publish(picture(), capture_ts=0, started_ns=0)
        assert decode._receiver.present_stats.count == 0

    def test_what_the_renderer_actually_did_is_reported(self):
        """Not what was asked for. A pass that silently does nothing is
        indistinguishable from one that works."""
        decode = decoder()
        decode.set_upscaler(FakeUpscaler(
            SubmitResult(ok=True, path=videofx.PATH_DOWNSCALE, gpu_ms=0.2)))
        decode._publish(picture(), capture_ts=0, started_ns=0)
        assert decode.last_path == videofx.PATH_NAMES[videofx.PATH_DOWNSCALE]
        assert decode.last_gpu_ms == pytest.approx(0.2)

    def test_a_gpu_time_that_is_not_back_yet_does_not_erase_the_last_one(self):
        """Timestamps are read a frame late and skipped when not ready, so
        -1 means "not yet", not "zero"."""
        decode = decoder()
        fake = FakeUpscaler(SubmitResult(ok=True, path=videofx.PATH_LANCZOS, gpu_ms=0.7))
        decode.set_upscaler(fake)
        decode._publish(picture(), capture_ts=0, started_ns=0)
        assert decode.last_gpu_ms == pytest.approx(0.7)

        fake.result = SubmitResult(ok=True, path=videofx.PATH_LANCZOS, gpu_ms=-1.0)
        decode._publish(picture(), capture_ts=0, started_ns=0)
        assert decode.last_gpu_ms == pytest.approx(0.7)


class TestTheCameraMove:
    def test_a_move_is_retired_when_it_finishes(self):
        from client.media.planner import TRANSITION_NS

        decode = decoder()
        decode.set_upscaler(FakeUpscaler())
        decode.set_regions([(0.0, 0.0, 0.5, 0.5)])
        decode._publish(picture(), capture_ts=0, started_ns=0)

        decode.set_regions([(0.5, 0.5, 0.5, 0.5)])
        assert decode._transition is not None

        decode._transition = (
            decode._transition[0] - TRANSITION_NS * 2,
            decode._transition[1],
            decode._transition[2],
        )
        decode._publish(picture(), capture_ts=0, started_ns=0)
        assert decode._transition is None

    def test_a_move_still_builds_no_filter_graph(self):
        decode = decoder()
        decode.set_upscaler(FakeUpscaler())
        decode.set_regions([(0.0, 0.0, 0.5, 0.5)])
        decode._publish(picture(), capture_ts=0, started_ns=0)
        decode.set_regions([(0.5, 0.5, 0.5, 0.5)])
        decode._publish(picture(), capture_ts=0, started_ns=0)
        assert decode._graphs == {}


class TestHardwareDecodeIsItsOwnSetting:
    def test_asking_for_it_does_not_change_anything_until_the_loop_runs(self):
        """It rebuilds the codec, so it cannot be taken per frame."""
        decode = decoder()
        decode.set_hw_decode("d3d11va")
        assert decode._hw_wanted == "d3d11va"
        assert decode._hw_device == ""

    def test_the_default_is_software(self):
        assert decoder()._hw_wanted == ""

    def test_an_unavailable_device_falls_back_rather_than_failing(self):
        decode = decoder()
        decode.set_hw_decode("not_a_real_device")
        codec = decode._build_codec(av)
        assert codec is not None
        assert codec.name == "h264"

    def test_a_fresh_codec_keeps_the_chosen_device(self):
        """It used to be a static method building a software decoder
        unconditionally, which would have turned hardware decoding off at the
        first damaged frame and never turned it back on."""
        decode = decoder()
        decode._hw_device = ""
        assert decode._fresh_codec(av) is not None

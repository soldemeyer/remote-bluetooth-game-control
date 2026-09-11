"""Hardware H.264 decoding, and whether the picture stays on the GPU.

Split the way the module is: which decoders *work* is a question only a real
decode can answer, and where the frame *ends up* is the question the rest of
the GPU path depends on.

Most of this runs anywhere -- a machine with no GPU takes the "nothing is
available" branch, which is itself a case worth testing, since it is what most
machines running this client will do. The handful that need real hardware skip
cleanly, in the idiom the rest of the suite uses.
"""

from __future__ import annotations

import pytest

pytest.importorskip("av", reason="video extras not installed")

from client.media import hwdecode  # noqa: E402


@pytest.fixture(autouse=True)
def clean_cache():
    hwdecode.reset_cache()
    yield
    hwdecode.reset_cache()


def _has_hardware() -> bool:
    return hwdecode.probe().available


needs_gpu = pytest.mark.skipif(
    not _has_hardware(), reason="no usable hardware H.264 decoder on this machine"
)


class TestItAnswersRatherThanRaising:
    """Every entry point is called on machines with no GPU at all."""

    def test_probe_always_returns_an_answer(self):
        caps = hwdecode.probe()
        assert isinstance(caps, hwdecode.DecoderCaps)
        assert isinstance(caps.available, bool)

    def test_an_unavailable_machine_explains_itself(self):
        caps = hwdecode.probe()
        if not caps.available:
            assert caps.reason, "unavailable with no reason is unhelpable"

    def test_the_built_in_list_never_raises(self):
        assert isinstance(hwdecode.built_in(), tuple)

    def test_a_nonsense_device_is_declined_not_raised(self):
        assert hwdecode.make_codec("not_a_real_device") is None

    def test_the_answer_is_cached(self):
        first = hwdecode.probe()
        assert hwdecode.probe() is first


class TestBuiltInIsNotEvidence:
    """FFmpeg ships D3D11VA, CUDA, QSV and AMF support whatever silicon is
    present, so the build list says nothing about this machine.

    The same distinction ``videoserver/encode.py`` draws between
    ``available_encoders`` and ``usable_encoders``, and it exists because the
    GUI offering something that cannot open reads as the setting being ignored.
    """

    def test_the_list_is_a_subset_of_the_platform_chain(self):
        assert set(hwdecode.built_in()) <= set(hwdecode.default_chain())

    def test_being_listed_does_not_make_it_usable(self):
        caps = hwdecode.probe()
        if hwdecode.built_in() and not caps.available:
            assert caps.reason  # exactly the case this distinction exists for

    def test_qsv_is_deliberately_not_offered(self):
        """It reported is_hwaccel=False and handed back ordinary yuv420p on the
        reference machine -- a silent software fallback, which is worse than
        not offering it because the GUI would claim hardware decoding that is
        not happening."""
        assert "qsv" not in hwdecode.default_chain()


class TestTheSampleStream:
    def test_it_produces_decodable_h264(self):
        import av

        data = hwdecode._sample_stream()
        assert len(data) > 0

        codec = av.CodecContext.create("h264", "r")
        frames = [f for packet in codec.parse(data) for f in codec.decode(packet)]
        assert frames, "the probe's own test stream does not decode in software"
        assert frames[0].width == 320 and frames[0].height == 240


class TestGpuHandles:
    def test_a_software_frame_has_none(self):
        """The important half. Reading `buffer_ptr` off a software plane would
        hand the renderer a pointer to system memory and call it a texture."""
        import av

        assert hwdecode.gpu_handles(av.VideoFrame(64, 64, "yuv420p")) is None
        assert hwdecode.gpu_handles(av.VideoFrame(64, 64, "rgb24")) is None

    def test_nonsense_is_declined(self):
        assert hwdecode.gpu_handles(None) is None
        assert hwdecode.gpu_handles(object()) is None

    @needs_gpu
    def test_a_hardware_frame_carries_a_texture_and_a_slice(self):
        caps = hwdecode.probe()
        codec = hwdecode.make_codec(caps.device_type)
        assert codec is not None

        data = hwdecode._sample_stream()
        frame = None
        for packet in codec.parse(data):
            for decoded in codec.decode(packet):
                frame = decoded
                break
            if frame is not None:
                break

        assert frame is not None
        handles = hwdecode.gpu_handles(frame)
        assert handles is not None
        texture, slice_index = handles
        assert texture > 0, "a null texture pointer is not a frame"
        assert slice_index >= 0


@needs_gpu
class TestZeroCopyOnRealHardware:
    def test_the_frame_never_reaches_system_memory(self):
        """The whole point of ``is_hw_owned``. Without it PyAV downloads every
        frame to RAM, and the "zero-copy" path would be a GPU round trip
        through system memory -- worse than software decoding, not better.
        """
        caps = hwdecode.probe()
        assert caps.zero_copy, caps.reason

        codec = hwdecode.make_codec(caps.device_type)
        data = hwdecode._sample_stream()
        frame = None
        for packet in codec.parse(data):
            for decoded in codec.decode(packet):
                frame = decoded
                break
            if frame is not None:
                break

        assert frame.format.name in hwdecode.HARDWARE_FORMATS
        # A hardware plane has no bytes behind it; a downloaded one does.
        assert frame.planes[0].buffer_size == 0

    def test_the_reported_size_is_the_picture_not_the_texture(self):
        """The decoder allocates in macroblocks, so a 1080-tall picture lives
        in a 1088-tall texture. Every rectangle must come from the frame, and
        using the texture's height shows eight rows of somebody else's memory.
        """
        caps = hwdecode.probe()
        codec = hwdecode.make_codec(caps.device_type)
        data = hwdecode._sample_stream()
        frame = None
        for packet in codec.parse(data):
            for decoded in codec.decode(packet):
                frame = decoded
                break
            if frame is not None:
                break
        assert (frame.width, frame.height) == (320, 240)

    def test_the_label_says_which_and_how(self):
        caps = hwdecode.probe()
        assert caps.device_type in caps.label
        assert "zero-copy" in caps.label

"""The ONNX backend, driven with models built here rather than downloaded.

**No model is shipped and none is fetched.** Real detector and embedder
weights are the operator's, and their licence is the operator's to accept --
so the models under test are tiny ONNX graphs constructed in the test itself.
That covers everything this backend actually owns: provider selection, the
input contract, preprocessing, output parsing, NMS, batched embedding, and
every way it is allowed to be unavailable.

What it deliberately does not cover is whether a *real* detector finds
characters in a *real* game. Nothing runnable here can answer that, and
pretending otherwise with a hand-made model would be worse than saying so.

The output-shape tests are the ones worth keeping. Export formats vary more
than any sniffing can honestly cover, and a shape read the wrong way round
produces confident boxes in the wrong places -- which downstream turns into a
name over the wrong character, the one outcome this feature must not have.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy", reason="the playervision extra is not installed")

from videoserver.playervision.backends.base import SampleFrame   # noqa: E402
from videoserver.playervision.backends.onnx import (             # noqa: E402
    DETECTOR_META,
    DETECTOR_NAME,
    EMBEDDER_NAME,
    ENV_MODEL_DIR,
    LAYOUT_AUTO,
    LAYOUT_POST_NMS,
    LAYOUT_YOLO,
    MAX_DETECTIONS,
    SCORE_FLOOR,
    YOLO_MIN_ANCHORS,
    OnnxBackend,
    detector_layout,
    model_dir,
    parse_detections,
    resolve_layout,
)


# -- building throwaway models --------------------------------------------


def _constant_detector(path, boxes):
    """A model that ignores its input and returns ``boxes``.

    Enough to exercise everything between the session and a `Detection`: the
    input contract, the preprocessing, the parse and the NMS. What the weights
    would have done is not this backend's business.
    """
    onnx = pytest.importorskip("onnx", reason="onnx is needed to build a test model")
    from onnx import TensorProto, helper, numpy_helper

    array = np.asarray(boxes, dtype="float32")
    const = helper.make_node(
        "Constant", [], ["output"],
        value=numpy_helper.from_array(array, name="boxes"),
    )
    graph = helper.make_graph(
        [const], "detector",
        [helper.make_tensor_value_info(
            "images", TensorProto.FLOAT, [1, 3, 64, 64])],
        [helper.make_tensor_value_info(
            "output", TensorProto.FLOAT, list(array.shape))],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 9
    onnx.save(model, str(path))


def _mean_embedder(path, size=32):
    """Mean over each crop's channels: a real batched call, trivial maths.

    A different vector per crop is the point -- a constant would pass an
    appearance test that a broken batch dimension should fail.
    """
    onnx = pytest.importorskip("onnx", reason="onnx is needed to build a test model")
    from onnx import TensorProto, helper

    node = helper.make_node(
        "ReduceMean", ["crops"], ["vectors"], axes=[2, 3], keepdims=0
    )
    graph = helper.make_graph(
        [node], "embedder",
        [helper.make_tensor_value_info(
            "crops", TensorProto.FLOAT, [None, 3, size, size])],
        [helper.make_tensor_value_info("vectors", TensorProto.FLOAT, [None, 3])],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 9
    onnx.save(model, str(path))


def _frame(width=64, height=64, *, colour=True, pad=0):
    """A frame with a bright square, and optionally a padded stride.

    The padding is not decoration: a scaler pads rows, and reading as though
    it did not shears the picture diagonally.
    """
    channels = 3 if colour else 1
    stride = width * channels + pad
    buf = bytearray(stride * height)
    for row in range(height // 4, height // 2):
        base = row * stride
        for col in range(width // 4, width // 2):
            for c in range(channels):
                buf[base + col * channels + c] = 200
    return SampleFrame(
        memoryview(bytes(buf)), width, height, stride,
        pixel_format="rgb24" if colour else "gray",
    )


@pytest.fixture
def models(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_MODEL_DIR, str(tmp_path))
    return tmp_path


# -- what this backend owns ------------------------------------------------


class TestModelDiscovery:
    def test_the_environment_wins(self, tmp_path, monkeypatch):
        """So a machine with the models somewhere unusual needs no config
        edit, and a test can point this anywhere."""
        monkeypatch.setenv(ENV_MODEL_DIR, str(tmp_path))
        assert model_dir() == tmp_path

    def test_without_models_it_says_where_it_looked(self, models):
        """A sentence somebody can act on, rather than 'unavailable'."""
        caps = OnnxBackend.probe()
        assert caps.available is False
        assert str(models) in caps.reason
        assert DETECTOR_NAME in caps.reason

    def test_it_says_that_nothing_is_downloaded(self, models):
        """The licence is the operator's to accept, so the absence has to say
        that rather than looking like a failed fetch."""
        assert "not shipped" in OnnxBackend.probe().reason

    def test_a_detector_alone_is_available(self, models):
        _constant_detector(models / DETECTOR_NAME, [[0, 0, 10, 10, 0.9, 0]])
        caps = OnnxBackend.probe()
        assert caps.available is True
        assert caps.embeddings is False, "claimed appearance matching with no embedder"

    def test_an_embedder_beside_it_is_reported(self, models):
        _constant_detector(models / DETECTOR_NAME, [[0, 0, 10, 10, 0.9, 0]])
        _mean_embedder(models / EMBEDDER_NAME)
        assert OnnxBackend.probe().embeddings is True

    def test_probing_loads_nothing(self, models):
        """Building a session compiles kernels and, on CUDA, allocates device
        memory. Doing that to answer 'is this available' would make a disabled
        feature cost GPU memory, which is the one promise it cannot break."""
        _constant_detector(models / DETECTOR_NAME, [[0, 0, 10, 10, 0.9, 0]])
        backend = OnnxBackend()
        OnnxBackend.probe()
        assert backend._detector is None
        assert backend._embedder is None


class TestProviders:
    def test_it_asks_only_for_providers_that_exist(self):
        """An install without CUDA must report CPU, not fail -- and the
        operator sees which in the web GUI rather than wondering why four
        players cost them the stream."""
        import onnxruntime as ort

        from videoserver.playervision.backends.onnx import _preferred_providers

        have = set(ort.get_available_providers())
        assert set(_preferred_providers()) <= have

    def test_cpu_is_always_last(self):
        """A provider that loads and then fails on the first real inference is
        a behaviour this project has already been bitten by with hardware
        encoders. The fallback keeps it a slower answer, not no answer."""
        from videoserver.playervision.backends.onnx import _preferred_providers

        providers = _preferred_providers()
        if "CPUExecutionProvider" in providers:
            assert providers[-1] == "CPUExecutionProvider"

    def test_the_chosen_one_is_reported(self, models):
        _constant_detector(models / DETECTOR_NAME, [[0, 0, 10, 10, 0.9, 0]])
        backend = OnnxBackend(models)
        caps = backend.start()
        assert caps.device, "did not say what it is running on"
        assert backend.snapshot()["provider"] == caps.device


class TestOutputShapes:
    """A shape read the wrong way round gives confident boxes in the wrong
    places, which downstream is a name over the wrong character."""

    def test_post_nms_rows(self):
        found = parse_detections(
            np.array([[10, 20, 30, 60, 0.9, 0]], dtype="float32"), 100, 100
        )
        assert len(found) == 1
        box = found[0].box
        assert (box.x, box.y) == pytest.approx((0.1, 0.2))
        assert (box.width, box.height) == pytest.approx((0.2, 0.4))
        assert found[0].score == pytest.approx(0.9)

    def test_post_nms_with_a_batch_dimension(self):
        found = parse_detections(
            np.array([[[10, 20, 30, 60, 0.9, 0]]], dtype="float32"), 100, 100
        )
        assert len(found) == 1

    def test_post_nms_without_a_class_column(self):
        assert len(parse_detections(
            np.array([[10, 20, 30, 60, 0.9]], dtype="float32"), 100, 100)) == 1

    def test_raw_yolo_channels_last(self):
        """``[1, N, 4+C]`` -- cx, cy, w, h then one score per class."""
        raw = np.zeros((1, 3, 6), dtype="float32")
        raw[0, 0] = [50, 50, 20, 40, 0.9, 0.1]
        # Declared: at two classes this shape is also a valid post-NMS
        # output, and nothing can tell them apart. That is the whole reason
        # the sidecar exists.
        found = parse_detections(raw, 100, 100, layout=LAYOUT_YOLO)
        assert len(found) == 1
        assert found[0].box.x == pytest.approx(0.4)
        assert found[0].box.width == pytest.approx(0.2)

    def test_raw_yolo_channels_first(self):
        """``[1, 4+C, N]`` -- the same thing transposed, which several
        exporters produce and which must not be read as N detections of 4+C
        numbers each.

        The orientation test is "is the short axis wide enough to *be* four
        box values and a score", not "is it shorter" -- `[3, 6]` is three
        anchors and `[6, 40]` is six channels, and only that clause gets both
        right."""
        raw = np.zeros((1, 6, 40), dtype="float32")
        raw[0, :, 0] = [50, 50, 20, 40, 0.9, 0.1]
        found = parse_detections(raw, 100, 100, layout=LAYOUT_YOLO)
        assert len(found) == 1
        assert found[0].box.x == pytest.approx(0.4)

    def test_the_class_is_ignored(self):
        """A detector trained on COCO calls a kart a car and a sprite nothing
        at all. No class vocabulary survives contact with an arbitrary game,
        and the moment one mattered somebody would maintain it per title."""
        raw = np.zeros((1, 2, 8), dtype="float32")
        raw[0, 0] = [50, 50, 20, 40, 0.0, 0.0, 0.95, 0.0]   # class 2
        raw[0, 1] = [20, 20, 10, 10, 0.93, 0.0, 0.0, 0.0]   # class 0
        assert len(parse_detections(raw, 100, 100, layout=LAYOUT_YOLO)) == 2

    def test_an_unreadable_shape_is_refused(self):
        """Rather than producing plausible boxes in the wrong places."""
        with pytest.raises(ValueError):
            parse_detections(np.zeros((2, 3, 4, 5), dtype="float32"), 100, 100)


class TestTheAmbiguity:
    """`[N, 6]` is either six post-NMS columns or four box values and two
    class scores. Read the wrong way every box lands somewhere plausible and
    wrong, which downstream is a name over the wrong character."""

    def test_a_declaration_always_wins(self):
        assert resolve_layout((8400, 84), LAYOUT_POST_NMS) == LAYOUT_POST_NMS
        assert resolve_layout((12, 6), LAYOUT_YOLO) == LAYOUT_YOLO

    def test_a_real_yolo_head_is_recognised_by_its_anchor_count(self):
        """8400 for v8 at 640, 25200 for v5. Thinning is what NMS is for, so
        a post-NMS output never has thousands of rows."""
        assert resolve_layout((8400, 84)) == LAYOUT_YOLO
        assert resolve_layout((84, 8400)) == LAYOUT_YOLO
        assert resolve_layout((25200, 85)) == LAYOUT_YOLO

    def test_a_thinned_output_is_recognised_by_being_short(self):
        assert resolve_layout((12, 6)) == LAYOUT_POST_NMS
        assert resolve_layout((1, 6)) == LAYOUT_POST_NMS

    def test_the_threshold_is_where_it_says(self):
        assert resolve_layout((YOLO_MIN_ANCHORS, 6)) == LAYOUT_YOLO
        assert resolve_layout((YOLO_MIN_ANCHORS - 1, 6)) == LAYOUT_POST_NMS

    def test_the_same_numbers_read_two_ways_give_different_boxes(self):
        """The reason this cannot be guessed: one tensor, two readings, two
        completely different answers."""
        # Numbers that are a valid box read either way, or one reading drops
        # them as inside-out and the point is lost.
        raw = np.array([[10, 10, 50, 60, 0.9, 0.1]], dtype="float32")
        as_nms = parse_detections(raw, 100, 100, layout=LAYOUT_POST_NMS)[0].box
        as_yolo = parse_detections(raw, 100, 100, layout=LAYOUT_YOLO)[0].box
        assert (as_nms.x, as_nms.width) != (as_yolo.x, as_yolo.width)

    def test_the_sidecar_is_read(self, models):
        (models / DETECTOR_META).write_text('{"output": "yolo"}')
        assert detector_layout(models) == LAYOUT_YOLO

    def test_no_sidecar_is_auto(self, models):
        assert detector_layout(models) == LAYOUT_AUTO

    def test_a_broken_sidecar_is_auto_rather_than_a_refusal(self, models):
        """Refusing to load a working detector over a stray comma would trade
        the feature for tidiness."""
        (models / DETECTOR_META).write_text("{not json at all")
        assert detector_layout(models) == LAYOUT_AUTO

    def test_an_unknown_declaration_is_auto(self, models):
        (models / DETECTOR_META).write_text('{"output": "banana"}')
        assert detector_layout(models) == LAYOUT_AUTO

    def test_the_backend_reports_which_it_took(self, models):
        """So an operator can see it, rather than inferring it from the boxes
        being in the wrong places."""
        _constant_detector(models / DETECTOR_NAME, [[0, 0, 20, 20, 0.9, 0]])
        (models / DETECTOR_META).write_text('{"output": "post_nms"}')
        backend = OnnxBackend(models)
        backend.start()
        assert backend.snapshot()["layout"] == LAYOUT_POST_NMS


class TestFiltering:
    def test_low_scores_are_dropped(self):
        raw = np.array([
            [10, 10, 20, 20, SCORE_FLOOR - 0.01, 0],
            [50, 50, 60, 60, 0.9, 0],
        ], dtype="float32")
        assert len(parse_detections(raw, 100, 100)) == 1

    def test_overlapping_boxes_are_one_thing(self):
        raw = np.array([
            [10, 10, 50, 50, 0.9, 0],
            [12, 12, 52, 52, 0.8, 0],
            [11, 11, 51, 51, 0.7, 0],
        ], dtype="float32")
        assert len(parse_detections(raw, 100, 100)) == 1

    def test_the_best_of_an_overlapping_group_survives(self):
        raw = np.array([
            [10, 10, 50, 50, 0.6, 0],
            [12, 12, 52, 52, 0.95, 0],
        ], dtype="float32")
        found = parse_detections(raw, 100, 100)
        assert len(found) == 1 and found[0].score == pytest.approx(0.95)

    def test_separate_boxes_both_survive(self):
        raw = np.array([
            [10, 10, 30, 30, 0.9, 0],
            [60, 60, 90, 90, 0.9, 0],
        ], dtype="float32")
        assert len(parse_detections(raw, 100, 100)) == 2

    def test_the_count_is_bounded(self):
        """An export with a broken score head can otherwise hand back eight
        thousand boxes, and the tracker would pay for every one."""
        raw = np.array([
            [i * 3, i * 3, i * 3 + 2, i * 3 + 2, 0.9, 0] for i in range(300)
        ], dtype="float32")
        assert len(parse_detections(raw, 1000, 1000)) <= MAX_DETECTIONS

    def test_coordinates_are_clamped_not_dropped(self):
        """A box slightly outside the input is an ordinary artefact of a
        regression head; dropping it would lose a real entity to rounding."""
        raw = np.array([[-20, -20, 120, 120, 0.9, 0]], dtype="float32")
        found = parse_detections(raw, 100, 100)
        assert len(found) == 1
        assert (found[0].box.x, found[0].box.y) == (0.0, 0.0)
        assert found[0].box.width == pytest.approx(1.0)

    def test_an_inside_out_box_is_dropped(self):
        assert parse_detections(
            np.array([[60, 60, 10, 10, 0.9, 0]], dtype="float32"), 100, 100) == []

    def test_nothing_found_is_not_an_error(self):
        assert parse_detections(np.zeros((0, 6), dtype="float32"), 100, 100) == []


class TestEndToEnd:
    def test_a_detection_comes_back_normalised(self, models):
        _constant_detector(models / DETECTOR_NAME, [[16, 16, 32, 48, 0.9, 0]])
        backend = OnnxBackend(models)
        backend.start()
        found = backend.detect(_frame())
        assert len(found) == 1
        assert found[0].box.x == pytest.approx(0.25)
        assert found[0].box.height == pytest.approx(0.5)

    def test_it_asks_for_colour(self, models):
        """Two karts that differ only in colour are identical in luma, and
        that is exactly the case a gallery has to separate."""
        assert OnnxBackend.wants_colour is True

    def test_a_padded_stride_does_not_shear_the_picture(self, models):
        """The stride is not width * channels. Reading as though it were
        shears the picture diagonally -- and the detections would still look
        plausible."""
        _constant_detector(models / DETECTOR_NAME, [[0, 0, 32, 32, 0.9, 0]])
        backend = OnnxBackend(models)
        backend.start()
        assert backend.detect(_frame(pad=13)) != []

    def test_a_gray_frame_still_works(self, models):
        """A backend that wants colour should still survive being handed
        luma -- the service decides the format, and a mismatch must cost
        accuracy rather than raising on the frame path."""
        _constant_detector(models / DETECTOR_NAME, [[0, 0, 32, 32, 0.9, 0]])
        backend = OnnxBackend(models)
        backend.start()
        assert backend.detect(_frame(colour=False)) != []

    def test_embeddings_are_attached(self, models):
        _constant_detector(models / DETECTOR_NAME, [
            [0, 0, 20, 20, 0.9, 0], [40, 40, 60, 60, 0.9, 0],
        ])
        _mean_embedder(models / EMBEDDER_NAME)
        backend = OnnxBackend(models)
        assert backend.start().embeddings is True
        found = backend.detect(_frame())
        assert len(found) == 2
        assert all(d.embedding for d in found)
        assert len(found[0].embedding) == 3

    def test_each_crop_gets_its_own_vector(self, models):
        """A broken batch dimension would give every entity the same
        appearance, which is the one thing a gallery cannot survive."""
        _constant_detector(models / DETECTOR_NAME, [
            [0, 0, 30, 30, 0.9, 0],       # over the bright square
            [40, 40, 60, 60, 0.9, 0],     # over the dark background
        ])
        _mean_embedder(models / EMBEDDER_NAME)
        backend = OnnxBackend(models)
        backend.start()
        found = backend.detect(_frame())
        assert found[0].embedding != found[1].embedding

    def test_no_embedder_still_detects(self, models):
        """A split screen is identified from the operator's own region
        assignment and needs no appearance matching at all."""
        _constant_detector(models / DETECTOR_NAME, [[0, 0, 20, 20, 0.9, 0]])
        backend = OnnxBackend(models)
        assert backend.start().embeddings is False
        found = backend.detect(_frame())
        assert len(found) == 1 and found[0].embedding is None

    def test_a_broken_embedder_does_not_cost_the_detector(self, models):
        """A detector alone is a working feature on a split screen, so a bad
        appearance model must not take the whole subsystem with it."""
        _constant_detector(models / DETECTOR_NAME, [[0, 0, 20, 20, 0.9, 0]])
        (models / EMBEDDER_NAME).write_bytes(b"not an onnx file")
        backend = OnnxBackend(models)
        caps = backend.start()
        assert caps.available is True
        assert caps.embeddings is False
        assert len(backend.detect(_frame())) == 1


class TestLifecycle:
    def test_detect_before_start_returns_nothing(self, models):
        assert OnnxBackend(models).detect(_frame()) == []

    def test_stop_releases_the_sessions(self, models):
        _constant_detector(models / DETECTOR_NAME, [[0, 0, 20, 20, 0.9, 0]])
        _mean_embedder(models / EMBEDDER_NAME)
        backend = OnnxBackend(models)
        backend.start()
        backend.stop()
        assert backend._detector is None
        assert backend._embedder is None
        assert backend.embeddings is False

    def test_stop_is_safe_twice(self, models):
        backend = OnnxBackend(models)
        backend.stop()
        backend.stop()

    def test_starting_without_a_detector_is_reported_not_raised(self, models):
        caps = OnnxBackend(models).start()
        assert caps.available is False and DETECTOR_NAME in caps.reason

    def test_a_degenerate_frame_is_not_an_error(self, models):
        _constant_detector(models / DETECTOR_NAME, [[0, 0, 20, 20, 0.9, 0]])
        backend = OnnxBackend(models)
        backend.start()
        assert backend.detect(SampleFrame(memoryview(b""), 0, 0, 0)) == []

    def test_it_declares_that_it_needs_its_own_process(self):
        """A CUDA kernel fault cannot be caught by `except`, and the one thing
        this feature must not do is take the stream down."""
        assert OnnxBackend.isolated is True

    def test_the_snapshot_reports_what_it_did(self, models):
        _constant_detector(models / DETECTOR_NAME, [[0, 0, 20, 20, 0.9, 0]])
        backend = OnnxBackend(models)
        backend.start()
        backend.detect(_frame())
        report = backend.snapshot()
        assert report["frames"] == 1 and report["detections"] == 1

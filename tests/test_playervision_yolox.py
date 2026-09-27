"""YOLOX's raw head, and how a model wants its pixels.

Both are silent when wrong. A raw YOLOX output read as a decoded one puts every
box within a few pixels of the top-left corner; the same model fed 0..1 instead
of 0..255 finds nothing at all -- measured on YOLOX's own demo photograph,
where 0..255 finds the bicycle, the truck and the dog and 0..1 finds none of
them. So both are *declared* in a sidecar, and these tests pin the arithmetic
with synthetic arrays rather than a model file.
"""

from __future__ import annotations

import json
import math

import pytest

np = pytest.importorskip("numpy", reason="the playervision extra is not installed")

from videoserver.playervision.backends.onnx import (  # noqa: E402
    LAYOUT_YOLOX,
    Preprocess,
    detector_layout,
    detector_preprocess,
    embedder_preprocess,
    parse_detections,
    preprocess_from,
    resolve_layout,
    to_tensor,
)

SIZE = 416
ANCHORS = (52 * 52) + (26 * 26) + (13 * 13)   # 3549, the real Tiny head
CLASSES = 80


def head() -> "np.ndarray":
    return np.zeros((1, ANCHORS, 5 + CLASSES), dtype="float32")


def put(out, *, stride, gx, gy, dx=0.5, dy=0.5, w=32.0, h=16.0, obj=0.9, cls=0.9):
    """One anchor predicting a box centred in grid cell (gx, gy)."""
    offset = {8: 0, 16: 52 * 52, 32: 52 * 52 + 26 * 26}[stride]
    across = SIZE // stride
    row = offset + gy * across + gx
    out[0, row, 0:4] = (dx, dy, math.log(w / stride), math.log(h / stride))
    out[0, row, 4] = obj
    out[0, row, 5 + 3] = cls


class TestDecodingTheRawHead:
    def test_one_anchor_becomes_one_box_in_the_right_place(self):
        out = head()
        put(out, stride=8, gx=10, gy=5)                # centre (84, 44)
        found = parse_detections(out, SIZE, SIZE, layout=LAYOUT_YOLOX)
        assert len(found) == 1
        box = found[0].box
        assert (box.x + box.width / 2) * SIZE == pytest.approx(84.0, abs=0.01)
        assert (box.y + box.height / 2) * SIZE == pytest.approx(44.0, abs=0.01)
        assert box.width * SIZE == pytest.approx(32.0, abs=0.01)
        assert box.height * SIZE == pytest.approx(16.0, abs=0.01)

    def test_the_score_is_objectness_times_the_best_class(self):
        out = head()
        put(out, stride=16, gx=3, gy=7, obj=0.8, cls=0.5)
        found = parse_detections(out, SIZE, SIZE, layout=LAYOUT_YOLOX, score_floor=0.1)
        assert found[0].score == pytest.approx(0.4, abs=0.001)

    def test_each_stride_uses_its_own_grid(self):
        out = head()
        put(out, stride=32, gx=11, gy=11, w=64.0, h=64.0)   # centre (368, 368)
        box = parse_detections(out, SIZE, SIZE, layout=LAYOUT_YOLOX)[0].box
        assert (box.x + box.width / 2) * SIZE == pytest.approx(368.0, abs=0.01)

    def test_objectness_alone_is_not_a_detection(self):
        out = head()
        put(out, stride=8, gx=1, gy=1, obj=0.95, cls=0.05)
        assert parse_detections(out, SIZE, SIZE, layout=LAYOUT_YOLOX) == []

    def test_a_grid_that_does_not_match_the_input_is_refused(self):
        """Decoding against the wrong grid puts every box somewhere plausible
        and false. The anchor count is what catches a wrong declaration."""
        with pytest.raises(ValueError, match="anchors"):
            parse_detections(head(), 640, 640, layout=LAYOUT_YOLOX)

    def test_it_is_never_inferred(self):
        """Same shape as a decoded head, so only a declaration can say."""
        assert resolve_layout((ANCHORS, 85)) != LAYOUT_YOLOX
        assert resolve_layout((ANCHORS, 85), LAYOUT_YOLOX) == LAYOUT_YOLOX


class TestDeclaringPreprocessing:
    def test_the_default_is_rgb_scaled_to_one(self):
        """What this backend always did, so a model with no sidecar is fed
        exactly as before."""
        prep = preprocess_from({})
        assert prep == Preprocess(divide=255.0, channels="rgb")

    def test_yolox_declares_raw_bgr(self):
        prep = preprocess_from({"input_range": "0-255", "channels": "bgr"})
        pixels = np.array([[[[10, 20, 30]]]], dtype="uint8")
        tensor = to_tensor(pixels, prep)
        assert tensor.shape == (1, 3, 1, 1)
        assert tensor[0, :, 0, 0].tolist() == [30.0, 20.0, 10.0]

    def test_imagenet_normalisation_is_applied_after_scaling(self):
        prep = preprocess_from({
            "mean": [0.5, 0.5, 0.5], "std": [0.25, 0.25, 0.25],
        })
        tensor = to_tensor(np.full((1, 1, 1, 3), 255, dtype="uint8"), prep)
        assert tensor[0, :, 0, 0].tolist() == pytest.approx([2.0, 2.0, 2.0])

    @pytest.mark.parametrize("raw", [
        {"channels": "cmyk"},
        {"mean": [0.5, 0.5]},
        {"mean": [0.5, 0.5, 0.5], "std": [0.0, 1.0, 1.0]},
        {"mean": "nonsense", "std": [1, 1, 1]},
        {"input_range": 7},
    ])
    def test_a_malformed_field_costs_that_field_not_the_model(self, raw):
        prep = preprocess_from(raw)
        assert prep.channels in ("rgb", "bgr")
        assert not prep.mean or len(prep.mean) == 3

    def test_the_sidecars_are_read(self, tmp_path):
        (tmp_path / "detector.json").write_text(json.dumps(
            {"output": "yolox", "input_range": "0-255", "channels": "bgr"}
        ))
        (tmp_path / "embedder.json").write_text(json.dumps(
            {"size": 224, "mean": [0.485, 0.456, 0.406], "std": [0.229, 0.224, 0.225]}
        ))
        assert detector_layout(tmp_path) == LAYOUT_YOLOX
        assert detector_preprocess(tmp_path).divide == 1.0
        prep, size = embedder_preprocess(tmp_path)
        assert size == 224 and prep.mean

    def test_no_sidecar_is_the_default(self, tmp_path):
        assert detector_preprocess(tmp_path) == Preprocess()
        assert embedder_preprocess(tmp_path) == (Preprocess(), 0)

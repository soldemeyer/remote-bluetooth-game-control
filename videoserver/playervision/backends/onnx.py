"""Detection and appearance through ONNX Runtime.

One dependency covering every vendor: its execution providers give
NVIDIA/CUDA, AMD and Intel through DirectML, and CPU, from the same code and
the same model files. That is why this rather than a framework -- a torch
build is a three-gigabyte CUDA install that is NVIDIA-first in practice, and
the abstraction in ``base.py`` means adding one later is a file in this
directory rather than a redesign.

WHAT IT NEEDS, AND WHAT HAPPENS WITHOUT IT
--------------------------------------------
Two model files, in a directory the operator provides:

    detector.onnx   required -- where the entities are
    embedder.onnx   optional -- what each one looks like

**Nothing is shipped and nothing is downloaded.** The files are the
operator's, and so is their licence. With none present this reports itself
unavailable with the path it looked in, which is a sentence somebody can act
on; ``auto`` then resolves to the no-model backend and everything downstream
carries on.

Without the embedder, detection and viewport ownership still work: a split
screen is identified from the operator's own region assignment and needs no
appearance matching at all. What is lost is finding a player inside somebody
*else's* viewport, which is the half the model exists for.

THE MODEL CONTRACT, AND THE AMBIGUITY IN IT
---------------------------------------------
Two output layouts are supported:

  * **post-NMS**, ``[N, 6]`` or ``[1, N, 6]`` -- ``x1, y1, x2, y2, score,
    class``. Preferred: export with NMS included and there is nothing here to
    get wrong.
  * **raw YOLO**, ``[1, 4+C, N]`` or ``[1, N, 4+C]`` -- ``cx, cy, w, h`` then
    one score per class. NMS is applied here.

**Those two are the same shape when C is 1 or 2**, and no amount of
inspection separates them: ``[N, 6]`` is either six post-NMS columns or four
box values and two class scores. Read the wrong way, ``x1, y1, x2, y2``
becomes ``cx, cy, w, h`` and every box lands somewhere plausible and wrong --
which downstream is a name over the wrong character.

So it is **declared, not sniffed**. A sidecar ``detector.json`` beside the
model says which::

    {"output": "yolo"}          or          {"output": "post_nms"}

Without one, ``auto`` decides on the only thing that genuinely separates real
exports: **the anchor count**. A raw YOLO head emits thousands of rows (8400
for v8 at 640, 25200 for v5); a post-NMS output has at most a few dozen. The
threshold is ``YOLO_MIN_ANCHORS``, and which layout was taken is reported in
the snapshot -- so an operator can see it rather than inferring it from the
boxes being wrong.

**The class is ignored, deliberately.** A detector trained on COCO calls a
kart a "car" and a sprite nothing at all, and no class vocabulary survives
contact with an arbitrary game. What is wanted is "something is here", and
the score alone answers that. This is what keeps the feature game-independent
-- the moment a class list mattered, somebody would have to maintain one per
title.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from common.screen_regions import Rect

from ..types import Detection
from .base import Capabilities, PlayerVisionBackend, SampleFrame

log = logging.getLogger(__name__)

__all__ = [
    "Fit",
    "OnnxBackend",
    "available_providers",
    "declared_input",
    "detector_layout",
    "model_dir",
    "letterbox",
    "parse_detections",
    "resolve_layout",
    "unletterbox",
]

#: Providers we ask for, best first. Only those ONNX Runtime actually has are
#: requested, so an install without CUDA does not fail -- it reports CPU, and
#: the operator can see that in the web GUI rather than wondering why four
#: players cost them the stream.
PROVIDER_LADDER = (
    "TensorrtExecutionProvider",
    "CUDAExecutionProvider",
    "DmlExecutionProvider",
    "ROCMExecutionProvider",
    "OpenVINOExecutionProvider",
    "CPUExecutionProvider",
)

#: Where the models live, unless told otherwise.
ENV_MODEL_DIR = "RBGC_PLAYERVISION_MODELS"

DETECTOR_NAME = "detector.onnx"
EMBEDDER_NAME = "embedder.onnx"
#: Optional sidecar naming the detector's output layout. See the module note:
#: the two layouts collide at small class counts and cannot be told apart.
DETECTOR_META = "detector.json"

#: Output layouts, and what ``auto`` may resolve to.
LAYOUT_AUTO = "auto"
LAYOUT_POST_NMS = "post_nms"
LAYOUT_YOLO = "yolo"

#: Rows above which an output is a raw head rather than a thinned one.
#:
#: The only thing that genuinely separates the two ambiguous layouts on real
#: exports. A YOLO head emits thousands (8400 at 640 for v8, 25200 for v5); a
#: post-NMS output has at most a few dozen, because thinning is what NMS is
#: for. Anything between is a model this cannot place, and declaring it beats
#: having this guess.
YOLO_MIN_ANCHORS = 64

#: Below this a detection is not published at all.
#:
#: Separate from ``player_id_confidence``, which is about *identity*: this is
#: about whether there is anything there. Generous on purpose -- a missed
#: entity cannot be labelled at all, while a spurious one costs a track that
#: the identity manager will decline to name.
SCORE_FLOOR = 0.25

#: Boxes overlapping more than this are the same thing seen twice.
NMS_IOU = 0.45

#: The most detections taken from one frame, highest score first. The tracker
#: is bounded downstream anyway, and an export with a broken score head can
#: otherwise hand back eight thousand boxes.
MAX_DETECTIONS = 24

#: Square each crop is resized to before the embedder sees it, unless the
#: model names its own size.
#:
#: The crop is **stretched**, not letterboxed, and that is deliberate.
#: Galleries are session-lived, so a crop is only ever compared against other
#: crops, and a transform applied consistently to both sides of a comparison
#: cancels. Padding one would add grey bars whose *area varies with the box's
#: aspect* -- a signal the embedding would learn and then match on.
EMBED_SIZE = 128

#: What a letterbox pads with.
#:
#: 114, because that is what ultralytics trains against, so it is what these
#: models have seen. Black reads as content and the detector spends capacity
#: on the border it makes.
PAD_VALUE = 114

#: The most threads the detector may take, however large the machine.
#:
#: Measured scaling flattens past this -- 19.9 ms at four against 15.7 at
#: eight on an 8.8 GFLOP model -- so the rest of a big machine is better left
#: to the encoder than spent for a millisecond.
MAX_THREADS = 4


def detector_threads() -> int:
    """How many threads the detector may use here. At least one.

    A quarter of the machine: enough to matter, and it leaves the encoder --
    which has a frame deadline this does not -- three quarters. On a four-core
    capture PC this is 1, which is what the original hard-coded value was
    chosen for.
    """
    return max(1, min(MAX_THREADS, (os.cpu_count() or 1) // 4))


def model_dir() -> Path:
    """Where to look for the models.

    The environment wins, so a machine with them somewhere unusual needs no
    config edit -- and so a test can point this anywhere without touching the
    operator's install.
    """
    override = os.environ.get(ENV_MODEL_DIR, "").strip()
    if override:
        return Path(override)
    from videoserver.config import config_dir

    return config_dir() / "playervision"


def available_providers() -> list[str]:
    """What this onnxruntime build ships. Never raises.

    **A build manifest, not a hardware fact.** An ``onnxruntime-gpu`` wheel
    lists CUDA and TensorRT on a machine with no NVIDIA card, no driver and no
    cuDNN -- exactly the relationship ``encode.available_encoders`` has to
    ``usable_encoders``, and ``hwdecode.built_in`` to ``hwdecode.probe``.

    Nothing here proves a provider can run anything. See the note on
    ``OnnxBackend.start`` about what the reported device does and does not
    mean.
    """
    try:
        import onnxruntime as ort

        return list(ort.get_available_providers())
    except Exception:  # noqa: BLE001 -- probing must never raise
        return []


#: The old private name, kept because it reads better at the call sites here.
_available_providers = available_providers


def _preferred_providers() -> list[str]:
    have = set(_available_providers())
    chosen = [name for name in PROVIDER_LADDER if name in have]
    # Always end on CPU. A provider that loads and then fails on the first
    # real inference is a documented hardware-encoder behaviour this project
    # has already been bitten by, and the fallback is what keeps it a slower
    # answer rather than no answer.
    if "CPUExecutionProvider" in have and "CPUExecutionProvider" not in chosen:
        chosen.append("CPUExecutionProvider")
    return chosen


def detector_layout(directory) -> str:
    """The declared output layout, or ``auto``.

    A missing or unreadable sidecar is ``auto`` rather than an error: the file
    is optional, and refusing to load a working detector over a stray comma
    would trade the feature for tidiness.
    """
    import json

    path = Path(directory) / DETECTOR_META
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        declared = str(raw.get("output", LAYOUT_AUTO)).strip().lower()
    except (OSError, ValueError, AttributeError):
        return LAYOUT_AUTO
    if declared in (LAYOUT_POST_NMS, LAYOUT_YOLO):
        return declared
    if declared != LAYOUT_AUTO:
        log.warning(
            "%s declares output %r, which is not one of %s; using auto",
            path.name, declared, (LAYOUT_POST_NMS, LAYOUT_YOLO),
        )
    return LAYOUT_AUTO


def declared_input(directory) -> tuple[int, int]:
    """``(width, height)`` from the sidecar, or ``(0, 0)``.

    The escape hatch for a model with a **dynamic** input axis, where
    `_input_shape` reports 0. Without a declaration, "feed me anything" and
    "we are feeding it the wrong thing" are the same reading -- and a
    dynamic export *trained* at 640 fed a 320 sample runs perfectly and is
    quietly much worse.
    """
    import json

    path = Path(directory) / DETECTOR_META
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        size = raw.get("input")
    except (OSError, ValueError, AttributeError):
        return 0, 0
    try:
        width, height = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return 0, 0
    if width <= 0 or height <= 0:
        return 0, 0
    return width, height


def resolve_layout(shape, declared: str = LAYOUT_AUTO) -> str:
    """Which layout to read ``shape`` as. Pure, so the rule is testable.

    A declaration always wins -- that is the point of having one. ``auto``
    falls back on the anchor count, which is the only thing that separates the
    two on a real export.
    """
    if declared in (LAYOUT_POST_NMS, LAYOUT_YOLO):
        return declared
    rows, columns = shape
    if max(rows, columns) >= YOLO_MIN_ANCHORS and min(rows, columns) >= 5:
        return LAYOUT_YOLO
    if columns in (5, 6) or rows in (5, 6):
        return LAYOUT_POST_NMS
    return LAYOUT_YOLO


def parse_detections(
    output,
    width: int,
    height: int,
    *,
    score_floor: float = SCORE_FLOOR,
    layout: str = LAYOUT_AUTO,
) -> list[Detection]:
    """One detector output to normalised boxes. Pure, so it tests anywhere.

    ``width``/``height`` are the size the model was *fed*, which is what its
    coordinates are in -- not the capture's. Confusing the two puts every box
    in the wrong place by the letterbox ratio, and the picture still looks
    plausible.

    Normalised coordinates are clamped rather than rejected: a box a little
    outside the input is an ordinary artefact of a regression head, and
    dropping it would lose a real entity over a rounding error.
    """
    import numpy as np

    array = np.asarray(output)
    if array.ndim == 3 and array.shape[0] == 1:
        array = array[0]
    if array.ndim != 2:
        raise ValueError(f"detector output has shape {np.shape(output)}")

    rows, columns = array.shape
    if resolve_layout((rows, columns), layout) == LAYOUT_POST_NMS:
        # The short axis holds the columns. Several exporters transpose.
        boxes, scores = _from_post_nms(array if columns in (5, 6) else array.T)
    else:
        boxes, scores = _from_yolo(array)

    keep = scores >= score_floor
    boxes, scores = boxes[keep], scores[keep]
    if len(scores) == 0:
        return []

    order = np.argsort(-scores)
    boxes, scores = boxes[order][:MAX_DETECTIONS * 4], scores[order][:MAX_DETECTIONS * 4]
    kept = _nms(boxes, scores)

    scale_x = 1.0 / max(width, 1)
    scale_y = 1.0 / max(height, 1)
    found: list[Detection] = []
    for index in kept[:MAX_DETECTIONS]:
        x1, y1, x2, y2 = boxes[index]
        left = min(max(float(x1) * scale_x, 0.0), 1.0)
        top = min(max(float(y1) * scale_y, 0.0), 1.0)
        right = min(max(float(x2) * scale_x, 0.0), 1.0)
        bottom = min(max(float(y2) * scale_y, 0.0), 1.0)
        if right <= left or bottom <= top:
            continue
        found.append(
            Detection(
                box=Rect(left, top, right - left, bottom - top),
                score=round(float(scores[index]), 3),
            )
        )
    return found


def _from_post_nms(array):
    """``x1, y1, x2, y2, score[, class]`` -- already thinned by the exporter."""
    return array[:, :4].astype("float32"), array[:, 4].astype("float32")


def _from_yolo(array):
    """``cx, cy, w, h`` then one score per class, either way round."""
    import numpy as np

    rows, columns = array.shape
    # Which axis is the channels? The short one -- but only if it is wide
    # enough to *be* channels, which is four box values and at least one
    # score. That last clause is what separates `[6, 40]` (six channels,
    # forty anchors: transpose) from `[3, 6]` (three anchors, six channels:
    # leave it), and neither "rows < columns" nor an anchor-count threshold
    # gets both right.
    if rows < columns and rows >= 5:
        array = array.T
        rows, columns = array.shape
    if columns < 5:
        raise ValueError(
            f"detector output has shape {array.shape}, too narrow for "
            f"cx, cy, w, h and at least one class score"
        )

    centres = array[:, :4].astype("float32")
    # Highest class score, because the class itself is deliberately ignored.
    scores = np.max(array[:, 4:].astype("float32"), axis=1)

    half_w = centres[:, 2] / 2.0
    half_h = centres[:, 3] / 2.0
    boxes = np.stack(
        [
            centres[:, 0] - half_w,
            centres[:, 1] - half_h,
            centres[:, 0] + half_w,
            centres[:, 1] + half_h,
        ],
        axis=1,
    )
    return boxes, scores


def _nms(boxes, scores, threshold: float = NMS_IOU) -> list[int]:
    """Greedy non-maximum suppression. Boxes are ``x1, y1, x2, y2``."""
    import numpy as np

    areas = (boxes[:, 2] - boxes[:, 0]).clip(0) * (boxes[:, 3] - boxes[:, 1]).clip(0)
    order = np.argsort(-scores)
    kept: list[int] = []
    while order.size:
        best = int(order[0])
        kept.append(best)
        if order.size == 1:
            break
        rest = order[1:]
        left = np.maximum(boxes[best, 0], boxes[rest, 0])
        top = np.maximum(boxes[best, 1], boxes[rest, 1])
        right = np.minimum(boxes[best, 2], boxes[rest, 2])
        bottom = np.minimum(boxes[best, 3], boxes[rest, 3])
        overlap = (right - left).clip(0) * (bottom - top).clip(0)
        union = areas[best] + areas[rest] - overlap
        iou = np.where(union > 0, overlap / np.maximum(union, 1e-9), 0.0)
        order = rest[iou <= threshold]
    return kept


class OnnxBackend(PlayerVisionBackend):
    """A detector, and optionally an embedder, through ONNX Runtime."""

    name = "onnx"
    #: A model backend runs in its own process. A CUDA kernel fault or a
    #: driver reset cannot be caught by ``except``, and the one thing this
    #: feature must not be able to do is take the stream down with it.
    isolated = True
    #: Appearance matching without colour throws away the most useful thing
    #: there is for telling two players apart.
    wants_colour = True
    #: The size to reduce a frame to before this sees it, when the model has
    #: not said otherwise. 640 because that is what nearly every detector
    #: export is trained at -- and because feeding a 640 model a 320 sample
    #: throws away everything between the two and then pays full price to
    #: fake it back. The model's own declared size wins over this once the
    #: session is open; see `Capabilities.input_width`.
    wants_width = 640

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = Path(directory) if directory else model_dir()
        self._detector = None
        self._embedder = None
        self._detector_input = ("", 0, 0)
        self._embedder_input = ("", 0)
        self._layout = LAYOUT_AUTO
        self._last_fit: Fit | None = None
        self._declared: tuple[int, int] = (0, 0)
        self._provider = ""
        self._provider_options: list[str] = []
        self.embeddings = False
        self.frames = 0
        self.detections = 0
        self.embed_failures = 0

    # -- capability --------------------------------------------------------

    @classmethod
    def probe(cls) -> Capabilities:
        """Can this run here? Loads nothing -- see the note on cost.

        Deliberately does not open a session. Building one compiles kernels
        and, on CUDA, allocates device memory; doing that to answer "is this
        available" would make a *disabled* feature cost GPU memory, which is
        the one promise it cannot break.
        """
        # The model directory is checked **first**, and that order matters.
        # Importing onnxruntime costs a second and a hundred megabytes; doing
        # it to discover there are no models would make `auto` pay for a
        # library it is about to decide it cannot use, on every start, on a
        # machine that never asked for it. A file stat answers the commoner
        # question for nothing.
        directory = model_dir()
        if not (directory / DETECTOR_NAME).is_file():
            return Capabilities(
                backend=cls.name,
                available=False,
                reason=(
                    f"no {DETECTOR_NAME} in {directory}. Models are not shipped "
                    f"or downloaded; put one there, or set {ENV_MODEL_DIR}"
                ),
            )

        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            return Capabilities(
                backend=cls.name,
                available=False,
                reason=(
                    "onnxruntime is not installed -- "
                    'pip install "remote-bluetooth-game-control[playervision]"'
                ),
            )

        providers = _preferred_providers()
        return Capabilities(
            backend=cls.name,
            available=bool(providers),
            reason="" if providers else "onnxruntime reports no execution provider",
            device=providers[0] if providers else "",
            embeddings=(directory / EMBEDDER_NAME).is_file(),
        )

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> Capabilities:
        # The model file is checked **before** the import, the same order and
        # for the same reason `probe` states: importing onnxruntime costs a
        # second and a hundred megabytes, and a stat answers the commoner
        # question for nothing. The order is also what makes this method
        # keep the promise its name makes -- a missing model is *reported*
        # rather than buried under a ModuleNotFoundError from the line above
        # it, on exactly the machine that has neither.
        detector_path = self._dir / DETECTOR_NAME
        if not detector_path.is_file():
            return Capabilities(
                backend=self.name, available=False,
                reason=f"no {DETECTOR_NAME} in {self._dir}",
            )

        import onnxruntime as ort

        options = ort.SessionOptions()
        # Scaled to the machine, which is a scheduling decision rather than a
        # performance one: this shares a box with an encoder that has a frame
        # deadline, and a detector helping itself to every core would cost the
        # stream far more than it gains.
        #
        # It was a hard 1 until it was measured. That is right on a four-core
        # capture PC and expensive on anything larger -- an 8.8 GFLOP
        # YOLOv8n-class detector at 640x640 on a 32-core desktop:
        #
        #     1 thread   67.8 ms    40.7% of a core at 6 Hz
        #     2 threads  34.6 ms    20.8%
        #     4 threads  19.9 ms    11.9%
        #     8 threads  15.7 ms     9.4%
        #
        # A quarter of the machine, capped at four. The cap is where the
        # scaling flattens, so a 64-core box gains nothing from sixteen
        # threads and the encoder keeps the rest; the quarter is what reduces
        # to one thread on a small machine, which is the case the original
        # decision was made for.
        options.intra_op_num_threads = detector_threads()
        options.inter_op_num_threads = 1
        options.log_severity_level = 3

        providers = _preferred_providers()
        self._detector = ort.InferenceSession(
            str(detector_path), sess_options=options, providers=providers
        )
        # **Registered, not necessarily executed.** `get_providers` returns
        # what ORT was asked to register, in priority order -- not what ran
        # the graph. ORT places nodes it cannot put on a provider onto a later
        # one, silently, so a session built on CUDA can report CUDA first and
        # have run every node on CPU.
        #
        # Proving execution needs profiling: `enable_profiling`, one warm-up
        # run, and reading `args["provider"]` per node out of the trace. That
        # is deliberately not done here -- see the limits in CLAUDE.md -- so
        # nothing in this file claims more than "registered".
        self._provider = (self._detector.get_providers() or ["unknown"])[0]
        self._provider_options = _provider_options(self._detector)
        self._detector_input = _input_shape(self._detector)
        self._layout = detector_layout(self._dir)
        self._declared = declared_input(self._dir)

        embedder_path = self._dir / EMBEDDER_NAME
        if embedder_path.is_file():
            try:
                self._embedder = ort.InferenceSession(
                    str(embedder_path), sess_options=options, providers=providers
                )
                name, _h, w = _input_shape(self._embedder)
                self._embedder_input = (name, w or EMBED_SIZE)
                self.embeddings = True
            except Exception as exc:  # noqa: BLE001
                # A detector alone is a working feature on a split screen, so
                # a bad embedder must not cost the whole subsystem. Said at
                # warning level because "no appearance matching" and "the
                # appearance model is broken" want different responses.
                log.warning("Could not load %s: %s", embedder_path.name, exc)
                self._embedder = None
                self.embeddings = False

        _name, model_h, model_w = self._detector_input
        declared_w, declared_h = declared_input(self._dir)
        return Capabilities(
            backend=self.name, available=True, reason="",
            device=self._provider, embeddings=self.embeddings,
            # What the parent should reduce frames to. The model's fixed axis
            # first; then the sidecar, which is the only way to tell a
            # genuinely *dynamic* model from one we are feeding wrongly; then
            # nothing, and the class attribute stands.
            input_width=model_w or declared_w,
            input_height=model_h or declared_h,
        )

    def stop(self) -> None:
        self._detector = None
        self._embedder = None
        self.embeddings = False

    # -- the work ----------------------------------------------------------

    def detect(self, frame: SampleFrame) -> list[Detection]:
        session = self._detector
        if session is None or frame.width <= 0 or frame.height <= 0:
            return []

        import numpy as np

        picture = _as_array(frame)
        name, target_h, target_w = self._detector_input
        fed, fit = letterbox(
            picture,
            target_w or self._declared[0] or frame.width,
            target_h or self._declared[1] or frame.height,
        )
        batch = np.ascontiguousarray(
            fed.transpose(2, 0, 1)[None].astype("float32") / 255.0
        )

        outputs = session.run(None, {name: batch})
        # `parse_detections` keeps meaning "normalised against the tensor it
        # was fed" -- which is why all of its tests survive this change
        # untouched. `unletterbox` is the separate step that takes those
        # coordinates back to the frame.
        found = parse_detections(
            outputs[0], fed.shape[1], fed.shape[0], layout=self._layout
        )
        found = unletterbox(found, fit)
        self.frames += 1
        self.detections += len(found)
        self._last_fit = fit

        if self._embedder is not None and found:
            found = self._embed(picture, found)
        return found

    def _embed(self, picture, found: list[Detection]) -> list[Detection]:
        """Attach an appearance vector to each detection.

        One batched call rather than one per box: the per-call overhead
        dominates at this size, and a frame with four players would otherwise
        pay it four times.

        A failure here costs the appearance signal for one frame and nothing
        else -- the detections are returned unchanged, and identity falls back
        on viewport ownership and continuity, which is the whole design with
        no embedder at all.
        """
        import numpy as np

        name, size = self._embedder_input
        crops = []
        for detection in found:
            crop = _crop(picture, detection.box)
            if crop is None:
                crops.append(np.zeros((size, size, 3), dtype="uint8"))
            else:
                crops.append(_resize(crop, size, size))

        try:
            batch = np.ascontiguousarray(
                np.stack(crops).transpose(0, 3, 1, 2).astype("float32") / 255.0
            )
            vectors = np.asarray(self._embedder.run(None, {name: batch})[0])
            vectors = vectors.reshape(vectors.shape[0], -1)
        except Exception:  # noqa: BLE001
            self.embed_failures += 1
            log.debug("Embedding failed for one frame", exc_info=True)
            return found

        return [
            Detection(
                box=detection.box,
                score=detection.score,
                embedding=tuple(float(value) for value in vectors[index]),
            )
            for index, detection in enumerate(found)
        ]

    def snapshot(self) -> dict[str, object]:
        return {
            "backend": self.name,
            "provider": self._provider,
            # Which providers ORT actually *created*, as opposed to which the
            # wheel advertises. Weaker than proving execution, and stronger
            # than the build list: a provider with no options entry was never
            # instantiated at all.
            "created": self._provider_options,
            # Reported so an operator can see which layout was taken, rather
            # than inferring it from the boxes being in the wrong places.
            "layout": self._layout,
            # What was actually fed, so "no fixed size" and "we fed it the
            # wrong size" are not the same reading from outside.
            "input": (
                f"{self._last_fit.width}x{self._last_fit.height}"
                if self._last_fit is not None else "unknown"
            ),
            "frames": self.frames,
            "detections": self.detections,
            "embed_failures": self.embed_failures,
        }


# -- image helpers ---------------------------------------------------------
#
# numpy rather than a second image library: it is already the extra's
# dependency through onnxruntime, and a nearest-neighbour resize of a 320-wide
# frame is two index arrays. Pulling in OpenCV for this would add ~60 MB to an
# optional extra to do what a slice does.


def _as_array(frame: SampleFrame):
    """The frame as ``H x W x 3`` uint8, honouring the stride.

    The stride is not ``width * channels``: a scaler pads rows, and reading as
    though it did not shears the picture diagonally -- the same trap the
    client's ``QImage`` notes record.
    """
    import numpy as np

    channels = frame.channels
    flat = np.frombuffer(frame.data, dtype="uint8")
    rows = flat[: frame.stride * frame.height].reshape(frame.height, frame.stride)
    picture = rows[:, : frame.width * channels].reshape(
        frame.height, frame.width, channels
    )
    if channels == 1:
        # A model wants three channels whatever we sampled.
        picture = np.repeat(picture, 3, axis=2)
    return picture


@dataclass(frozen=True, slots=True)
class Fit:
    """How a picture was placed on a model's square input.

    Carried from ``letterbox`` to ``unletterbox`` so boxes can be mapped back
    without either function knowing what the other did to get there.
    """

    scale: float
    pad_x: int
    pad_y: int
    #: The placed picture's size, inside the padded canvas.
    inner_width: int
    inner_height: int
    #: The canvas the model was fed.
    width: int
    height: int


def letterbox(picture, width: int, height: int, fill: int = PAD_VALUE):
    """Place a picture on a ``width`` x ``height`` canvas, aspect preserved.

    **This is the accuracy fix.** Without it a 16:9 frame handed to a square
    model is *stretched*: 320x180 into 640x640 scales 3.56x vertically against
    2.0x horizontally, so the aspect comes out **1.78x** wrong. Every common
    export is trained on aspect-preserved, padded input. The boxes still map
    back self-consistently, so the damage shows as missed and mis-sized
    detections rather than as anything visibly wrong, which is why it can sit
    there unnoticed.

    ``fill`` is 114, not 0. That is the value ultralytics pads with, so it is
    what these models have seen; a hard black border reads as content and the
    detector spends capacity on its edges.
    """
    import numpy as np

    source_h, source_w = picture.shape[0], picture.shape[1]
    width = max(1, int(width))
    height = max(1, int(height))

    scale = min(width / max(source_w, 1), height / max(source_h, 1))
    inner_w = max(1, min(width, int(round(source_w * scale))))
    inner_h = max(1, min(height, int(round(source_h * scale))))
    pad_x = (width - inner_w) // 2
    pad_y = (height - inner_h) // 2

    placed = _resize(picture, inner_w, inner_h)
    if inner_w == width and inner_h == height:
        # Exactly fills it: no canvas, no copy. This is the common case once
        # the sample width matches the model -- a 640-wide sample of a 16:9
        # capture is 640x360 into 640x640, so `scale` is 1.0 and the only
        # work is the pad below.
        canvas = placed
    else:
        canvas = np.full((height, width, picture.shape[2]), fill, dtype="uint8")
        canvas[pad_y:pad_y + inner_h, pad_x:pad_x + inner_w] = placed

    return canvas, Fit(
        scale=scale, pad_x=pad_x, pad_y=pad_y,
        inner_width=inner_w, inner_height=inner_h,
        width=width, height=height,
    )


def unletterbox(found: list[Detection], fit: Fit) -> list[Detection]:
    """Boxes normalised against the padded canvas, against the frame instead.

    A detection wholly inside the padding maps to nothing and is dropped: it
    is the model finding something in the grey bars, which is not part of the
    picture and has no position in it.
    """
    if fit.inner_width <= 0 or fit.inner_height <= 0:
        return found

    out: list[Detection] = []
    for detection in found:
        box = detection.box
        # Canvas-normalised -> canvas pixels -> picture pixels -> normalised.
        left = (box.x * fit.width - fit.pad_x) / fit.inner_width
        top = (box.y * fit.height - fit.pad_y) / fit.inner_height
        right = ((box.x + box.width) * fit.width - fit.pad_x) / fit.inner_width
        bottom = ((box.y + box.height) * fit.height - fit.pad_y) / fit.inner_height

        left = min(max(left, 0.0), 1.0)
        top = min(max(top, 0.0), 1.0)
        right = min(max(right, 0.0), 1.0)
        bottom = min(max(bottom, 0.0), 1.0)
        if right <= left or bottom <= top:
            continue
        out.append(
            Detection(
                box=Rect(left, top, right - left, bottom - top),
                score=detection.score,
                embedding=detection.embedding,
            )
        )
    return out


def _resize(picture, width: int, height: int):
    """Nearest-neighbour resize. Good enough, and it allocates once."""
    import numpy as np

    height = max(1, int(height))
    width = max(1, int(width))
    if picture.shape[0] == height and picture.shape[1] == width:
        return picture
    rows = (np.arange(height) * (picture.shape[0] / height)).astype("int32")
    columns = (np.arange(width) * (picture.shape[1] / width)).astype("int32")
    rows = np.clip(rows, 0, picture.shape[0] - 1)
    columns = np.clip(columns, 0, picture.shape[1] - 1)
    return picture[rows][:, columns]


def _crop(picture, box: Rect):
    """The pixels inside a normalised box, or ``None`` if there are none."""
    height, width = picture.shape[0], picture.shape[1]
    x0 = max(0, min(int(box.x * width), width - 1))
    y0 = max(0, min(int(box.y * height), height - 1))
    x1 = max(x0 + 1, min(int((box.x + box.width) * width), width))
    y1 = max(y0 + 1, min(int((box.y + box.height) * height), height))
    crop = picture[y0:y1, x0:x1]
    return crop if crop.size else None


def _provider_options(session) -> list[str]:
    """Providers ORT reports options for, i.e. ones it really created.

    Not evidence that any node ran on them, but better than the build list:
    a provider the session never instantiated has no options entry.
    """
    try:
        return sorted(session.get_provider_options())
    except Exception:  # noqa: BLE001
        return []


def _input_shape(session) -> tuple[str, int, int]:
    """``(name, height, width)`` of a session's first input.

    A dynamic axis comes back as a string or ``None``, and is reported as 0 --
    "whatever it is given" -- rather than being guessed at.
    """
    meta = session.get_inputs()[0]
    shape = list(meta.shape or [])

    def fixed(value) -> int:
        return int(value) if isinstance(value, int) and value > 0 else 0

    if len(shape) == 4:
        return meta.name, fixed(shape[2]), fixed(shape[3])
    return meta.name, 0, 0

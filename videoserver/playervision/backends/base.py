"""What a vision backend must do, and how to ask whether it can.

The only place in this subsystem where a model is mentioned at all. Everything
above -- tracking, identity, the join on the Bluetooth server, the client's
overlay -- is written against ``Detection`` and never against a framework, so
swapping ONNX Runtime for something else is a file in this directory.

TWO THINGS THE SHAPE HERE IS COPIED FROM, AND WHY
--------------------------------------------------
``Capabilities`` is ``client/media/upscale.py``'s: an availability flag paired
with a **reason**, because a control that cannot work has to say why. "Not
available on this computer" is useful; silence reads as the application being
broken, and an exception in a log the operator never opens is worse than both.

``NullBackend.detect`` **raises**, exactly as ``NullUpscaler.submit`` does.
This is not a pass-through and must never become one: with the feature off the
service branches on the backend being absent long before anything gets here,
and a polite null that quietly returned no detections would make that branch
untestable -- Off would have stopped being the original path and nothing would
say so. The ``AssertionError`` turns that regression into a test failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..types import Detection

log = logging.getLogger(__name__)

__all__ = ["Capabilities", "GrayFrame", "PlayerVisionBackend", "NullBackend"]


@dataclass(frozen=True, slots=True)
class GrayFrame:
    """One downscaled luma frame, and when it was captured.

    Gray rather than colour because every backend here starts by reducing to
    luma anyway, and because it is a quarter of the bytes to copy across a
    process boundary. A backend that genuinely needs colour takes it up with
    the service rather than having every other backend pay for it.

    ``data`` is a ``memoryview`` and is **only valid for the duration of the
    call**. A backend that wants to keep a frame must copy it: the service
    reuses one buffer, which is what keeps this path allocation-free.

    ``stride`` is the row length in bytes and is not ``width`` -- the same
    trap ``PresentFrame.stride`` documents on the client. A scaler pads rows.
    """

    data: memoryview
    width: int
    height: int
    stride: int
    capture_ts: int = 0


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What this machine can actually do, and why not where it cannot."""

    backend: str = "none"
    available: bool = False
    reason: str = "player identification is not configured"
    #: What is doing the work -- an execution provider, a device name. Shown
    #: to the operator, because "available" without it cannot distinguish a
    #: GPU from a CPU fallback that will never keep up.
    device: str = ""
    #: Does this backend produce appearance vectors? Without them identity
    #: works from viewport ownership and continuity alone, which is the whole
    #: design on a split screen and nothing at all on a shared one.
    embeddings: bool = False

    def describe(self) -> list[str]:
        """The one-shot startup log block. Never per frame."""
        if not self.available:
            return [f"Player identification: unavailable -- {self.reason}"]
        lines = [f"Player identification: {self.backend}"]
        if self.device:
            lines.append(f"  running on {self.device}")
        lines.append(
            "  appearance matching: "
            + ("yes" if self.embeddings else "no (viewport and motion only)")
        )
        return lines

    def as_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "available": self.available,
            "reason": self.reason,
            "device": self.device,
            "embeddings": self.embeddings,
        }


class PlayerVisionBackend:
    """Find the things in a frame that might be players.

    Five calls, and **none of them may raise**. A backend that cannot do its
    job reports it through ``probe`` and through returning nothing; letting an
    exception out would put a model's bad day on the path that is meant to be
    unable to disturb the stream.

    Detection only. No identity, no tracking, no memory between frames beyond
    whatever the backend needs internally -- those are somebody else's job, and
    keeping this narrow is what makes a backend swappable.
    """

    #: Shown to the operator and matched against ``player_id_backend``.
    name = "none"

    #: Does this need its own process? True for anything that loads a model:
    #: a CUDA kernel fault or a driver reset cannot be caught by ``except``,
    #: and the one requirement this feature cannot compromise on is that it
    #: must not be able to take the stream down.
    isolated = False

    #: Does ``detect`` fill in ``Detection.embedding``?
    embeddings = False

    @classmethod
    def probe(cls) -> Capabilities:
        """Can this backend run here? Must not raise, and must not load."""
        return Capabilities(backend=cls.name)

    def start(self) -> Capabilities:
        """Load whatever is needed. The expensive call; never on a hot path."""
        raise NotImplementedError

    def detect(self, frame: GrayFrame) -> list[Detection]:
        """Everything that might be a player in this frame."""
        raise NotImplementedError

    def stop(self) -> None:
        """Release everything. Must be safe to call twice, and after a failure."""
        raise NotImplementedError

    def snapshot(self) -> dict[str, object]:
        return {"backend": self.name}


class NullBackend(PlayerVisionBackend):
    """The answer when nothing is available. Never on the frame path.

    **Not a pass-through, and must never become one.** The service branches on
    having no backend before anything reaches here; if this ever appears in a
    detection, Off has stopped being the original path.
    """

    name = "none"

    @classmethod
    def probe(cls) -> Capabilities:
        return Capabilities(
            backend="none",
            available=False,
            reason="no vision backend is available on this computer",
        )

    def start(self) -> Capabilities:
        return self.probe()

    def detect(self, frame: GrayFrame) -> list[Detection]:
        raise AssertionError(
            "NullBackend.detect was called: player identification being off "
            "must bypass the detection path entirely, not route through it"
        )

    def stop(self) -> None:
        return None

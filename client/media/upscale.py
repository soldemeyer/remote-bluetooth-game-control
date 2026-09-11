"""Optional GPU enhancement: one abstraction over several backends, and Off.

Four modes reach the player, and only one of them is a code path:

======== ====================================================================
off      The existing software path, untouched. **Nothing here runs.** No
         device is created, no library is loaded, and ``client.media.videofx``
         is never imported. See `NullUpscaler` for why that is a rule rather
         than an optimisation.
gpu      Present through the GPU with a plain high-quality scale and no
         enhancement. The control -- without it, "is FSR better?" and "what
         does RTX VSR cost?" are unanswerable, because Off differs from the
         other modes in how it *presents* as well as in what it does to the
         pixels.
fsr1     AMD FidelityFX Super Resolution 1: EASU then RCAS.
rtx_vsr  NVIDIA RTX Video Super Resolution, through the Direct3D 11 video
         processor extension. No NVIDIA SDK and nothing redistributed.
======== ====================================================================

A preference is not a capability
--------------------------------
The player's choice is stored and **never** overwritten by hardware that
cannot honour it. Move the client to a machine with no RTX card and the mode
runs as Off with the reason shown; move it back and the choice returns. That
is the same shape as ``ClientConfig.backend_override`` -- a stored preference
plus a resolver -- and :func:`effective_mode` is the resolver.

The scan is not free
--------------------
:func:`capabilities` creates a graphics device, asks it questions, tears it
down, and encodes and decodes a small test stream. 50-150 ms, once. It belongs
on a worker thread; the GUI shows its controls disabled and reading
"detecting..." until the answer lands.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from client.media import hwdecode

log = logging.getLogger(__name__)

OFF = "off"
GPU = "gpu"
FSR1 = "fsr1"
RTX_VSR = "rtx_vsr"

#: Display names, and the order the GUI lists them in.
MODE_LABELS: tuple[tuple[str, str], ...] = (
    (OFF, "Off"),
    (GPU, "GPU present (Lanczos)"),
    (RTX_VSR, "NVIDIA RTX Video Super Resolution"),
    (FSR1, "FSR 1 - EASU + RCAS"),
)


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Everything the GUI needs to draw the settings, in one object."""

    gpu_name: str = ""
    backend: str = ""
    driver: str = ""
    vendor: str = ""

    gpu_ok: bool = False
    fsr1_ok: bool = False
    rtx_vsr_ok: bool = False
    hw_decode_ok: bool = False
    hw_decode_zero_copy: bool = False
    hw_decode_device: str = ""

    reason_gpu: str = ""
    reason_fsr1: str = ""
    reason_rtx_vsr: str = ""
    reason_hw_decode: str = ""

    def supports(self, mode: str) -> bool:
        if mode == OFF:
            return True
        if mode == GPU:
            return self.gpu_ok
        if mode == FSR1:
            return self.fsr1_ok
        if mode == RTX_VSR:
            return self.rtx_vsr_ok
        return False

    def reason(self, mode: str) -> str:
        """Why ``mode`` is unavailable, phrased for a player, or ""."""
        if self.supports(mode):
            return ""
        return {
            GPU: self.reason_gpu,
            FSR1: self.reason_fsr1,
            RTX_VSR: self.reason_rtx_vsr,
        }.get(mode, "not supported on this computer")

    def detail(self, mode: str) -> str:
        """The line under a mode in the GUI, available or not."""
        if mode == OFF:
            return "No enhancement - the picture exactly as it arrives"
        if not self.supports(mode):
            return self.reason(mode)
        if mode == RTX_VSR:
            return f"Available - {self.gpu_name}" if self.gpu_name else "Available"
        return f"Available - {self.backend}" if self.backend else "Available"

    def describe(self) -> list[str]:
        """The one-shot startup log block. Never per frame."""
        lines = ["Video enhancement capability scan"]
        lines.append(f"  GPU                {self.gpu_name or 'not detected'}")
        lines.append(f"  Renderer           {self.backend or 'unavailable'}")
        if self.driver:
            lines.append(f"  Driver             {self.driver}")
        rows = (
            ("Hardware decode", self.hw_decode_ok, self.reason_hw_decode,
             self.hw_decode_device),
            ("GPU present", self.gpu_ok, self.reason_gpu, ""),
            ("RTX Video SR", self.rtx_vsr_ok, self.reason_rtx_vsr, ""),
            ("FSR 1", self.fsr1_ok, self.reason_fsr1, ""),
        )
        for label, ok, why, extra in rows:
            state = "SUPPORTED" if ok else "UNSUPPORTED"
            suffix = f"  ({extra})" if ok and extra else ""
            lines.append(f"  {label:<18} {state}{suffix}")
            if not ok and why:
                lines.append(f"  {'':<18} reason: {why}")
        return lines


_cache: Capabilities | None = None
_lock = threading.Lock()


def capabilities(force: bool = False) -> Capabilities:
    """Scan this machine. Cached, thread-safe, and it never raises.

    Safe to call from a worker thread, which is where it belongs -- see the
    module docstring.
    """
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache

    # Imported here, not at module scope. A client with the feature off must
    # never load the enhancement library, and an import at the top would do it
    # the moment anything touched this module.
    from client.media import videofx

    caps = videofx.probe(force=force)
    decoder = hwdecode.probe(force=force)

    resolved = Capabilities(
        gpu_name=caps.gpu_name,
        backend=caps.backend,
        driver=caps.driver,
        vendor=caps.vendor,
        gpu_ok=caps.usable,
        fsr1_ok=caps.fsr1,
        rtx_vsr_ok=caps.rtx_vsr,
        hw_decode_ok=decoder.available,
        hw_decode_zero_copy=decoder.zero_copy,
        hw_decode_device=decoder.label if decoder.available else "",
        reason_gpu=caps.reason_usable,
        reason_fsr1=caps.reason_fsr1,
        reason_rtx_vsr=caps.reason_rtx_vsr,
        reason_hw_decode=decoder.reason,
    )

    with _lock:
        _cache = resolved
    return resolved


def cached() -> Capabilities | None:
    """The scan's answer if it has already been made, else None.

    Lets a caller skip starting a worker thread for a question that has
    already been settled -- which is every window after the first, and is the
    difference between one probe and hundreds of threads in a test suite that
    builds a lot of windows.
    """
    with _lock:
        return _cache


def reset_cache() -> None:
    """For tests. Hardware does not change without a restart otherwise."""
    global _cache
    from client.media import videofx

    with _lock:
        _cache = None
    videofx.reset_cache()
    hwdecode.reset_cache()


def effective_mode(configured: str, caps: Capabilities | None = None) -> str:
    """The mode to actually run, given what the player chose.

    Falls back to `OFF` when the hardware cannot honour the choice, **without
    touching the stored preference**. A player who set FSR on their desktop and
    opened the client on a laptop gets Off with an explanation, and gets FSR
    back when they go home. Silently rewriting the config would lose that, and
    silently *attempting* it would be worse still.
    """
    if configured == OFF:
        return OFF
    caps = caps if caps is not None else capabilities()
    if caps.supports(configured):
        return configured
    if configured not in (GPU, FSR1, RTX_VSR):
        log.info("Unknown video upscaler %r; using Off", configured)
    else:
        log.info(
            "Video upscaler %r is not available here (%s); using Off",
            configured,
            caps.reason(configured) or "unsupported",
        )
    return OFF


def hw_decode_device(configured: str, caps: Capabilities | None = None) -> str:
    """Which hardware decoder to use, or "" for software.

    ``configured`` is ``ClientConfig.video_hw_decode``: ``off`` or ``auto``.
    """
    if configured != "auto":
        return ""
    caps = caps if caps is not None else capabilities()
    if not caps.hw_decode_ok:
        log.info(
            "Hardware decoding is unavailable here (%s); decoding in software",
            caps.reason_hw_decode or "unsupported",
        )
        return ""
    return hwdecode.probe().device_type


# -- the backends -----------------------------------------------------------


class VideoUpscaler:
    """What a backend must do. Four calls, and none of them may raise.

    Every implementation returns a status rather than throwing, because the
    caller's answer to any failure is the same -- drop to Off and keep the
    stream running -- and an exception crossing the decode thread would take
    the picture down instead.
    """

    #: What the debug overlay calls this.
    name = "none"

    def initialize(self, native_window: int, mode: str) -> bool:
        raise NotImplementedError

    def set_mode(self, mode: str) -> bool:
        raise NotImplementedError

    def set_sharpness(self, percent: int) -> None:
        raise NotImplementedError

    def submit(self, frame) -> object:
        raise NotImplementedError

    def shutdown(self) -> None:
        raise NotImplementedError


class NullUpscaler(VideoUpscaler):
    """Off.

    **This is not a pass-through and must never become one.** The decoder
    branches on ``upscaler is None`` *before* any of this, and takes its
    existing software path with no extra call, no extra object and no extra
    render step. This class exists so the capability and settings layers have
    one type to talk about; if it ever appears on the frame path, Off has
    stopped being the baseline and the requirement this feature was built
    under has been broken.
    """

    name = "off"

    def initialize(self, native_window: int, mode: str) -> bool:
        return False

    def set_mode(self, mode: str) -> bool:
        return False

    def set_sharpness(self, percent: int) -> None:
        return None

    def submit(self, frame) -> object:
        raise AssertionError(
            "NullUpscaler.submit was called: Off must bypass the enhancement "
            "path entirely, not route through it"
        )

    def shutdown(self) -> None:
        return None


def sharpness_to_attenuation(percent: int) -> float:
    """A 0-100 slider as the RCAS constant FidelityFX actually wants.

    RCAS attenuation runs 0 (sharpest) to 2 (softest), which is backwards from
    how a sharpness slider reads, and is not a number to put in front of
    anyone. 50% maps to 0.5 rather than to the FSR sample's 0.25: this is
    compressed video with block artifacts rather than clean engine output, and
    over-sharpening amplifies precisely what the encoder threw away.
    """
    percent = min(100, max(0, int(percent)))
    # 0% -> 2.0 (softest), 50% -> 0.5, 100% -> 0.0 (sharpest).
    if percent <= 50:
        return 2.0 - (percent / 50.0) * 1.5
    return 0.5 - ((percent - 50) / 50.0) * 0.5

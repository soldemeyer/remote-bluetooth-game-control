"""Decoding H.264 on the GPU, when this machine actually can.

Two separate things live here and they are worth keeping apart:

* **which hardware decoders work**, answered by decoding a real stream rather
  than by reading a list -- the same distinction ``videoserver/encode.py``
  draws between ``available_encoders`` and ``usable_encoders``, and for the
  same reason. FFmpeg ships D3D11VA, CUDA, QSV and AMF support regardless of
  what silicon is present, so the build list is not evidence.

* **where the decoded picture ends up**, which is the part that matters to the
  rest of the video path. PyAV downloads a hardware frame to system memory
  unless it is asked not to, and asking not to is what makes the GPU path
  zero-copy instead of a round trip through RAM.

Measured on an RTX 5080, PyAV 18.0.0::

    HWAccel("d3d11va", is_hw_owned=False)   format nv12,  planes in RAM
    HWAccel("d3d11va", is_hw_owned=True)    format d3d11, no CPU memory at all
        planes[0].buffer_ptr -> ID3D11Texture2D*
        planes[1].buffer_ptr -> array slice
        texture 1920x1088, ArraySize 20, DXGI_FORMAT_NV12,
        BindFlags 0x200 = D3D11_BIND_DECODER

Three consequences the rest of this feature is built on, all measured:

**The texture is taller than the frame.** 1088 for a 1080 picture, because the
decoder allocates in macroblocks. Every rectangle must be computed against the
frame's own height; using the texture's shows eight rows of whatever the
decoder last had there.

**It is `BIND_DECODER` only**, so no shader resource view can be created on it.
The renderer reads it through a video processor instead -- which is not a
detour, because that is also what converts NV12 to RGB and applies the crop.

**The device belongs to FFmpeg.** The renderer adopts it rather than making its
own, which removes the need for shared handles entirely *and* makes the
decoder-texture lifetime safe for free: one device means D3D11's own
per-resource dependency tracking stops FFmpeg recycling an array slice while
the GPU is still reading it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: Candidates, best first.
#:
#: D3D11VA leads because it is vendor-neutral -- NVIDIA, AMD and Intel all
#: expose it -- and because its output is the ``ID3D11Texture2D`` the renderer
#: already wants. CUDA works and is NVIDIA-only; DXVA2 is the D3D9 predecessor
#: and is kept only as a last resort on old drivers.
#:
#: QSV is deliberately absent. It reported ``is_hwaccel=False`` and handed back
#: ordinary ``yuv420p`` on the reference machine, i.e. it silently fell back to
#: software -- which is worse than not offering it, because the GUI would then
#: claim hardware decoding that is not happening.
WINDOWS_CHAIN: tuple[str, ...] = ("d3d11va", "cuda", "dxva2")

#: Linux. VAAPI is the vendor-neutral one; ``vulkan`` decodes straight into a
#: ``VkImage``, which is the zero-copy path for the Vulkan renderer.
LINUX_CHAIN: tuple[str, ...] = ("vulkan", "vaapi", "cuda")

#: Pixel formats whose planes really are a hardware handle rather than memory.
HARDWARE_FORMATS = frozenset({"d3d11", "d3d11va_vld", "cuda", "vaapi", "vulkan", "dxva2_vld"})


@dataclass(frozen=True, slots=True)
class DecoderCaps:
    """Which hardware decoder to use, if any, and what it will hand back."""

    available: bool = False
    device_type: str = ""
    #: True when frames stay on the GPU -- the zero-copy path. False means the
    #: decoder runs on the GPU but PyAV downloads each frame to system memory,
    #: which still saves CPU but cannot feed the renderer directly.
    zero_copy: bool = False
    reason: str = ""

    @property
    def label(self) -> str:
        if not self.available:
            return "software"
        suffix = " (zero-copy)" if self.zero_copy else " (downloads to RAM)"
        return f"{self.device_type}{suffix}"


def default_chain() -> tuple[str, ...]:
    import sys

    if sys.platform == "win32":
        return WINDOWS_CHAIN
    if sys.platform == "darwin":
        return ("videotoolbox",)
    return LINUX_CHAIN


def built_in() -> tuple[str, ...]:
    """Device types this PyAV build knows about. Never raises.

    **Built in, not usable.** See the module docstring: this is the list, not
    the evidence.
    """
    try:
        from av.codec.hwaccel import hwdevices_available
    except Exception:  # noqa: BLE001 - no PyAV, or too old to have hwaccel
        return ()
    try:
        present = set(hwdevices_available())
    except Exception:  # noqa: BLE001
        return ()
    return tuple(name for name in default_chain() if name in present)


def _sample_stream() -> bytes:
    """A few frames of real H.264, in memory.

    Encoded rather than checked in: a committed fixture is another binary to
    keep in step with the encoder, and libx264 is already a hard dependency of
    the video extras.
    """
    import io

    import av

    buf = io.BytesIO()
    container = av.open(buf, "w", format="h264")
    try:
        stream = container.add_stream("libx264", rate=30)
        stream.width, stream.height = 320, 240
        stream.pix_fmt = "yuv420p"
        stream.options = {
            "preset": "ultrafast",
            "tune": "zerolatency",
            "x264-params": "keyint=4",
        }
        for _ in range(6):
            frame = av.VideoFrame(320, 240, "yuv420p")
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()
    return buf.getvalue()


def _try_device(device_type: str, data: bytes) -> DecoderCaps | None:
    """Decode a real frame on this device, or return None.

    ``allow_software_fallback`` is **False** on purpose. With it True a device
    that cannot decode quietly succeeds in software, and the GUI then reports
    hardware decoding that is not happening -- the exact shape of failure this
    project keeps writing sections about.
    """
    import av
    from av.codec.hwaccel import HWAccel

    try:
        accel = HWAccel(
            device_type=device_type,
            allow_software_fallback=False,
            is_hw_owned=True,
        )
        codec = av.CodecContext.create("h264", "r", hwaccel=accel)
    except Exception as exc:  # noqa: BLE001
        log.debug("hwaccel %s could not be created: %s", device_type, exc)
        return None

    try:
        for packet in codec.parse(data):
            for frame in codec.decode(packet):
                name = frame.format.name
                return DecoderCaps(
                    available=True,
                    device_type=device_type,
                    zero_copy=name in HARDWARE_FORMATS,
                    reason="" if name in HARDWARE_FORMATS else (
                        f"{device_type} decodes on the GPU but hands back "
                        f"{name} in system memory"
                    ),
                )
    except Exception as exc:  # noqa: BLE001
        log.debug("hwaccel %s decoded nothing: %s", device_type, exc)
        return None
    return None


_cached: DecoderCaps | None = None


def probe(force: bool = False) -> DecoderCaps:
    """Which hardware decoder works here. Cached; never raises.

    Costs a real encode and decode, so it belongs on a worker thread with the
    rest of the capability scan. The answer cannot change without new hardware
    or a driver change and a restart.
    """
    global _cached
    if _cached is not None and not force:
        return _cached

    try:
        import av  # noqa: F401
    except ImportError as exc:
        _cached = DecoderCaps(reason=f"video playback needs PyAV: {exc}")
        return _cached

    candidates = built_in()
    if not candidates:
        _cached = DecoderCaps(
            reason="this build of FFmpeg has no hardware decoder for this platform"
        )
        return _cached

    try:
        data = _sample_stream()
    except Exception as exc:  # noqa: BLE001
        _cached = DecoderCaps(reason=f"could not build a test stream: {exc}")
        return _cached

    for device_type in candidates:
        caps = _try_device(device_type, data)
        if caps is not None and caps.zero_copy:
            _cached = caps
            return _cached
        if caps is not None:
            # Works, but downloads. Keep looking for a better one and settle
            # for this if nothing else answers.
            _cached = caps

    if _cached is None:
        _cached = DecoderCaps(
            reason="no hardware decoder on this machine could decode a test stream"
        )
    return _cached


def reset_cache() -> None:
    """For tests. The answer cannot change without a restart otherwise."""
    global _cached
    _cached = None


def make_codec(device_type: str):
    """A hardware decoder handing back GPU frames, or None if it will not.

    Separate from :func:`probe` because the decode thread builds one of these
    per stream and must not pay for a probe each time.

    **An unknown device type is not an error to PyAV.** Measured: constructing
    ``HWAccel(device_type="not_a_real_device")`` succeeds, ``CodecContext.create``
    succeeds, and the context reports ``is_hwaccel = True`` -- and then decodes
    in software. So neither the constructor nor that flag can be trusted here;
    ``is_hwaccel`` only becomes meaningful after a frame has come out.

    The name is therefore checked against the list FFmpeg actually knows,
    before anything is built. Without that, a typo or a stale config value
    produces a client reporting hardware decoding that is not happening, which
    is precisely the "untuned and fine are indistinguishable" failure this
    project keeps rediscovering.
    """
    import av
    from av.codec.hwaccel import HWAccel, hwdevices_available

    try:
        known = set(hwdevices_available())
    except Exception:  # noqa: BLE001
        known = set()
    if device_type not in known:
        log.warning(
            "Hardware decoding (%s) is unavailable: this FFmpeg build knows only %s",
            device_type,
            ", ".join(sorted(known)) or "no hardware devices",
        )
        return None

    try:
        accel = HWAccel(
            device_type=device_type,
            allow_software_fallback=False,
            is_hw_owned=True,
        )
        codec = av.CodecContext.create("h264", "r", hwaccel=accel)
    except Exception as exc:  # noqa: BLE001
        log.warning("Hardware decoding (%s) is unavailable: %s", device_type, exc)
        return None
    return codec


def gpu_handles(frame) -> tuple[int, int] | None:
    """``(texture_pointer, array_slice)`` for a hardware frame, else None.

    D3D11VA puts the ``ID3D11Texture2D*`` in ``data[0]`` and the slice index in
    ``data[1]``; PyAV exposes both as ``planes[n].buffer_ptr``. Anything whose
    format is not a hardware format is a software frame and belongs on the
    other path entirely.
    """
    try:
        if frame.format.name not in HARDWARE_FORMATS:
            return None
        planes = frame.planes
        if len(planes) < 2:
            return None
        return int(planes[0].buffer_ptr), int(planes[1].buffer_ptr)
    except Exception:  # noqa: BLE001
        return None

"""Loading the optional GPU enhancement library, or deciding we cannot.

This is the only module that knows ``rbgc_videofx`` exists. Everything above
it -- ``upscale.py``, the decoder, the GUI -- sees a capability answer and a
handle, never a shared library.

**The library is optional in the strongest sense.** A machine with no library,
the wrong architecture, no GPU, or a driver that refuses must run the client
exactly as it ran before this feature was written. So every failure here is an
answer rather than an exception: :func:`probe` returns a `Caps` saying no and
why, and the GUI shows that sentence next to a disabled control.

Two things in here are load-bearing and easy to undo by accident
---------------------------------------------------------------
**``WinDLL``/``CDLL``, never ``PyDLL``.** The first two release the GIL around
the foreign call and the third does not. Every call this module makes crosses
into code that copies megabytes and waits on a driver; holding the GIL through
that would land directly on the 500 Hz input loop this whole project is built
around, and the only symptom would be its p99. There is no error, no log line
and no test that fails -- which is why it is written down here.

**Every function declares ``argtypes`` and ``restype``.** ctypes defaults an
undeclared argument to C ``int``, which truncates a 64-bit pointer to 32 bits.
The crash lands somewhere else entirely, usually inside the driver, and reads
as a GPU fault. ``tests/test_videofx_loader.py`` walks the table and asserts
every entry is declared, which catches the whole class with no GPU and no
library present.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Bumped when the ABI in ``native/videofx/videofx.h`` changes incompatibly.
#: A library reporting anything else is refused rather than called, because a
#: struct-layout mismatch is not a crash you can debug from the traceback.
ABI_MAJOR = 1

NAME_MAX = 128
SHORT_MAX = 40
REASON_MAX = 160

# -- status and mode, mirroring videofx.h -----------------------------------

OK = 0
ERR_ARG = 1
ERR_UNSUPPORTED = 2
ERR_DEVICE = 3
ERR_DEVICE_LOST = 4
ERR_SHADER = 5
ERR_RESOURCE = 6
ERR_WINDOW = 7
ERR_INTERNAL = 8

STATUS_NAMES = {
    OK: "ok",
    ERR_ARG: "bad argument",
    ERR_UNSUPPORTED: "not supported on this machine",
    ERR_DEVICE: "could not create the graphics device",
    ERR_DEVICE_LOST: "the graphics device was lost",
    ERR_SHADER: "a shader failed to load",
    ERR_RESOURCE: "a GPU resource could not be created",
    ERR_WINDOW: "the window went away",
    ERR_INTERNAL: "internal error",
}

MODE_LANCZOS = 0
MODE_FSR1 = 1
MODE_RTX_VSR = 2

#: ``ClientConfig.video_upscaler`` values to the library's mode numbers.
#: ``off`` is absent on purpose: off never reaches this module at all.
MODE_FOR_SETTING = {"gpu": MODE_LANCZOS, "fsr1": MODE_FSR1, "rtx_vsr": MODE_RTX_VSR}

PATH_NONE = 0
PATH_COPY = 1
PATH_LANCZOS = 2
PATH_EASU_RCAS = 3
PATH_VSR = 4
PATH_DOWNSCALE = 5

PATH_NAMES = {
    PATH_NONE: "none",
    PATH_COPY: "copy (1:1)",
    PATH_LANCZOS: "Lanczos",
    PATH_EASU_RCAS: "FSR 1 EASU+RCAS",
    # "requested", not "active": the driver accepts the extension and then
    # never tells anyone whether it ran. See `Caps.rtx_vsr`.
    PATH_VSR: "RTX VSR (requested)",
    PATH_DOWNSCALE: "downscale (no SR)",
}

VENDOR_NAMES = {0x10DE: "NVIDIA", 0x1002: "AMD", 0x8086: "Intel", 0x1414: "Microsoft"}


# -- structs ----------------------------------------------------------------


class CCaps(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("usable", ctypes.c_int32),
        ("fsr1", ctypes.c_int32),
        ("rtx_vsr", ctypes.c_int32),
        ("hw_decode", ctypes.c_int32),
        ("vendor_id", ctypes.c_uint32),
        ("device_id", ctypes.c_uint32),
        ("driver_version", ctypes.c_uint64),
        ("gpu_name", ctypes.c_char * NAME_MAX),
        ("backend", ctypes.c_char * SHORT_MAX),
        ("driver", ctypes.c_char * SHORT_MAX),
        ("reason_usable", ctypes.c_char * REASON_MAX),
        ("reason_fsr1", ctypes.c_char * REASON_MAX),
        ("reason_rtx_vsr", ctypes.c_char * REASON_MAX),
        ("reason_hw_decode", ctypes.c_char * REASON_MAX),
    ]


class CBlit(ctypes.Structure):
    _fields_ = [("src", ctypes.c_float * 4), ("dst", ctypes.c_int32 * 4)]


class CFrame(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("plane", ctypes.c_void_p * 3),
        ("stride", ctypes.c_int32 * 3),
        ("hw_texture", ctypes.c_void_p),
        ("hw_slice", ctypes.c_uint32),
        ("src_width", ctypes.c_int32),
        ("src_height", ctypes.c_int32),
        ("colorspace", ctypes.c_int32),
        ("color_range", ctypes.c_int32),
        ("composed_width", ctypes.c_int32),
        ("composed_height", ctypes.c_int32),
        ("blits", ctypes.POINTER(CBlit)),
        ("blit_count", ctypes.c_int32),
        ("overlay", ctypes.c_void_p),
        ("overlay_stride", ctypes.c_int32),
        ("overlay_x", ctypes.c_int32),
        ("overlay_y", ctypes.c_int32),
        ("overlay_width", ctypes.c_int32),
        ("overlay_height", ctypes.c_int32),
        ("overlay_version", ctypes.c_uint32),
    ]


class CResult(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("path", ctypes.c_int32),
        ("present_skipped", ctypes.c_int32),
        ("gpu_ms", ctypes.c_float),
        ("output_width", ctypes.c_int32),
        ("output_height", ctypes.c_int32),
    ]


#: Every exported function, as (name, restype, argtypes).
#:
#: One table rather than a run of assignments, so the "is everything declared?"
#: test is a loop over data instead of a promise. See the module docstring for
#: what an undeclared argtype costs.
EXPORTS: tuple[tuple[str, object, tuple], ...] = (
    ("rbgc_version", ctypes.c_char_p, ()),
    ("rbgc_probe", ctypes.c_int32, (ctypes.POINTER(CCaps),)),
    (
        "rbgc_create",
        ctypes.c_int32,
        (ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(ctypes.c_void_p)),
    ),
    ("rbgc_set_mode", ctypes.c_int32, (ctypes.c_void_p, ctypes.c_int32)),
    ("rbgc_set_sharpness", ctypes.c_int32, (ctypes.c_void_p, ctypes.c_float)),
    ("rbgc_set_backdrop", ctypes.c_int32, (ctypes.c_void_p, ctypes.c_uint32)),
    (
        "rbgc_submit",
        ctypes.c_int32,
        (ctypes.c_void_p, ctypes.POINTER(CFrame), ctypes.POINTER(CResult)),
    ),
    ("rbgc_repaint", ctypes.c_int32, (ctypes.c_void_p,)),
    ("rbgc_last_error", ctypes.c_char_p, (ctypes.c_void_p,)),
    ("rbgc_destroy", None, (ctypes.c_void_p,)),
)


@dataclass(frozen=True, slots=True)
class Caps:
    """What this machine can do, and in the player's words when it cannot."""

    usable: bool = False
    fsr1: bool = False
    #: The driver *accepted* the extension. There is no API that confirms it
    #: ran, so nothing anywhere should say "active" -- see `PATH_NAMES`.
    rtx_vsr: bool = False
    hw_decode: bool = False

    gpu_name: str = ""
    vendor: str = ""
    vendor_id: int = 0
    driver: str = ""
    backend: str = ""

    reason_usable: str = ""
    reason_fsr1: str = ""
    reason_rtx_vsr: str = ""
    reason_hw_decode: str = ""

    #: Why there is no library at all, when that is the answer.
    load_error: str = ""

    def describe(self) -> list[str]:
        """The startup log block. One scan, once -- never per frame."""
        lines = ["Video enhancement capability scan"]
        lines.append(f"  GPU                {self.gpu_name or 'unknown'}")
        lines.append(f"  Renderer           {self.backend or 'unavailable'}")
        for label, ok, why in (
            ("Hardware decode", self.hw_decode, self.reason_hw_decode),
            ("RTX Video SR", self.rtx_vsr, self.reason_rtx_vsr),
            ("FSR 1", self.fsr1, self.reason_fsr1),
        ):
            state = "SUPPORTED" if ok else "UNSUPPORTED"
            lines.append(f"  {label:<18} {state}")
            if not ok and why:
                lines.append(f"  {'':<18} reason: {why}")
        return lines


def _unavailable(reason: str) -> Caps:
    """No library, so nothing is supported -- and the reason is the same one
    for every feature, because none of them got as far as being asked."""
    return Caps(
        load_error=reason,
        reason_usable=reason,
        reason_fsr1=reason,
        reason_rtx_vsr=reason,
        reason_hw_decode=reason,
    )


# -- finding the library ----------------------------------------------------


def library_name() -> str:
    if sys.platform == "win32":
        return "rbgc_videofx.dll"
    if sys.platform == "darwin":
        return "librbgc_videofx.dylib"
    return f"librbgc_videofx-{platform.machine()}.so"


def candidate_paths() -> list[Path]:
    """Everywhere the library could be, most specific first.

    Four shapes, because this project ships in four: a source checkout, a
    PyInstaller onedir bundle (``sys._MEIPASS``), a Nuitka/AppImage tree, and
    an environment variable for somebody testing a build they just made.
    """
    name = library_name()
    here = Path(__file__).resolve().parent
    paths: list[Path] = []

    override = os.environ.get("RBGC_VIDEOFX")
    if override:
        candidate = Path(override)
        paths.append(candidate / name if candidate.is_dir() else candidate)

    paths.append(here / "fx" / name)

    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        paths.append(Path(bundle) / "client" / "media" / "fx" / name)
        paths.append(Path(bundle) / name)

    # Nuitka puts data beside the entry binary rather than in a temp tree.
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve().parent
        paths.append(exe / "client" / "media" / "fx" / name)
        paths.append(exe / name)

    seen: set[Path] = set()
    unique = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


@dataclass
class _Loaded:
    lib: object = None
    path: str = ""
    error: str = ""
    version: str = ""


_loaded: _Loaded | None = None


def _load() -> _Loaded:
    """Find and bind the library. Cached; never raises."""
    global _loaded
    if _loaded is not None:
        return _loaded

    if struct.calcsize("P") != 8:
        _loaded = _Loaded(error="a 64-bit Python is required for GPU enhancement")
        return _loaded

    # "It is not there" and "it is there and would not load" are different
    # answers and want different sentences. The first is the ordinary case on
    # a machine that simply has no GPU build, and a player reading it should
    # not be shown a developer's file path.
    tried: list[str] = []
    found_any = False
    for path in candidate_paths():
        if not path.exists():
            continue
        found_any = True
        try:
            # WinDLL/CDLL, never PyDLL -- see the module docstring. This one
            # word is the difference between "off the GIL" and a tail on the
            # 500 Hz input loop that nothing reports.
            loader = ctypes.WinDLL if sys.platform == "win32" else ctypes.CDLL
            lib = loader(str(path))
        except OSError as exc:
            # WinError 193 is "not a valid Win32 application", which on a
            # 64-bit Python means a 32-bit build. It reads as a corrupt
            # download unless somebody says so.
            hint = ""
            if getattr(exc, "winerror", None) == 193:
                hint = " (this looks like a 32-bit build; a 64-bit one is needed)"
            tried.append(f"{path}: {exc}{hint}")
            continue

        try:
            _bind(lib)
            version = (lib.rbgc_version() or b"").decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            tried.append(f"{path}: {exc}")
            continue

        major = version.split(".")[0] if version else ""
        if major != str(ABI_MAJOR):
            tried.append(
                f"{path}: built against ABI {version or '?'}, this client needs "
                f"{ABI_MAJOR}.x"
            )
            continue

        log.debug("Loaded GPU enhancement library %s (%s)", path, version)
        _loaded = _Loaded(lib=lib, path=str(path), version=version)
        return _loaded

    if not found_any:
        error = "GPU video enhancement is not installed in this build"
    else:
        error = "; ".join(tried[-2:])
    _loaded = _Loaded(error=error)
    log.debug("No GPU enhancement library: %s", _loaded.error)
    return _loaded


def _bind(lib) -> None:
    """Declare every signature. Missing symbols are a fatal mismatch."""
    for name, restype, argtypes in EXPORTS:
        fn = getattr(lib, name)
        fn.restype = restype
        fn.argtypes = list(argtypes)


def reset_cache() -> None:
    """Forget the load result. For tests, and for nothing else -- the answer
    cannot change without a new file on disk and a restart."""
    global _loaded, _probed
    _loaded = None
    _probed = None


def is_available() -> bool:
    return _load().lib is not None


def load_error() -> str:
    """Why there is no library, or "" when there is one."""
    return _load().error


def library_path() -> str:
    return _load().path


def library_version() -> str:
    return _load().version


# -- probing ----------------------------------------------------------------

_probed: Caps | None = None


def probe(force: bool = False) -> Caps:
    """What this machine can do. Cached.

    Costs 50-150 ms: it creates a graphics device, asks it questions and tears
    it down. **Call it from a worker thread.** The answer cannot change without
    new hardware or a driver change and a restart, which is why caching it is
    honest rather than merely convenient.

    Never raises. A library that crashes on probe is a library we do not use.
    """
    global _probed
    if _probed is not None and not force:
        return _probed

    loaded = _load()
    if loaded.lib is None:
        _probed = _unavailable(loaded.error)
        return _probed

    caps = CCaps()
    caps.struct_size = ctypes.sizeof(CCaps)
    try:
        status = loaded.lib.rbgc_probe(ctypes.byref(caps))
    except Exception as exc:  # noqa: BLE001
        log.warning("GPU capability probe failed: %s", exc, exc_info=True)
        _probed = _unavailable(f"the enhancement library failed to start: {exc}")
        return _probed

    if status != OK:
        reason = STATUS_NAMES.get(status, f"error {status}")
        _probed = _unavailable(f"capability probe failed: {reason}")
        return _probed

    def text(raw: bytes) -> str:
        return raw.decode("utf-8", "replace").strip()

    _probed = Caps(
        usable=bool(caps.usable),
        fsr1=bool(caps.fsr1),
        rtx_vsr=bool(caps.rtx_vsr),
        hw_decode=bool(caps.hw_decode),
        gpu_name=text(caps.gpu_name),
        vendor=VENDOR_NAMES.get(caps.vendor_id, ""),
        vendor_id=int(caps.vendor_id),
        driver=text(caps.driver),
        backend=text(caps.backend),
        reason_usable=text(caps.reason_usable),
        reason_fsr1=text(caps.reason_fsr1),
        reason_rtx_vsr=text(caps.reason_rtx_vsr),
        reason_hw_decode=text(caps.reason_hw_decode),
    )
    return _probed


def library():
    """The bound library, or None. For `upscale.py`; nothing else should ask."""
    return _load().lib

"""Build the optional GPU video enhancement library.

    python -m tools.build_videofx            # shaders + library, for this host
    python -m tools.build_videofx --shaders  # shaders only (no compiler needed)
    python -m tools.build_videofx --check    # compile shaders, write nothing

Output goes to ``client/media/fx/`` and is **committed**, the same way
``tools/build_icon.py`` and ``tools/build_controller_art.py`` commit theirs.
A fresh checkout therefore has working GPU enhancement with no toolchain
installed, and nothing about running or packaging the client needs a compiler.

Shaders are compiled **offline** into a C header of byte arrays. Compiling at
runtime would make ``d3dcompiler_47.dll`` a redistributable dependency for a
feature that is meant to be optional, and would put a compiler on the startup
path of a video player.

What this needs, and only to *rebuild*:

  Windows   MSVC (any edition with the C++ tools) and a Windows SDK, for
            ``cl.exe`` and ``fxc.exe``. Located through vswhere and the
            registry-standard SDK layout rather than requiring a developer
            prompt, so it runs from an ordinary shell.

  Linux     gcc, the Vulkan headers and loader, libwayland-dev and libxcb-dev,
            plus ``dxc`` for HLSL -> SPIR-V. ``packaging/linux/provision.sh``
            installs the set.

One shader source, two targets: FidelityFX's headers are written to compile as
both HLSL and GLSL, and dxc emits SPIR-V from the same files fxc turns into
DXBC. So the FSR maths is compiled from one place for both backends rather
than transcribed into a second dialect.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NATIVE = ROOT / "native" / "videofx"
SHADERS = NATIVE / "shaders"
OUT_DIR = ROOT / "client" / "media" / "fx"
GENERATED = NATIVE / "shaders_generated.h"

#: (source, entry point, profile, symbol). The symbol is what the C++ names.
SHADER_TARGETS: tuple[tuple[str, str, str, str], ...] = (
    ("convert.hlsl", "main", "cs_5_0", "kConvertYuv420"),
    ("convert.hlsl", "main_nv12", "cs_5_0", "kConvertNv12"),
    ("easu.hlsl", "main", "cs_5_0", "kEasu"),
    ("rcas.hlsl", "main", "cs_5_0", "kRcas"),
    ("lanczos.hlsl", "main", "cs_5_0", "kLanczos"),
    ("overlay.hlsl", "vs_main", "vs_5_0", "kOverlayVS"),
    ("overlay.hlsl", "ps_main", "ps_5_0", "kOverlayPS"),
)


def _fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


# -- locating the Windows toolchain -----------------------------------------


def find_fxc() -> Path | None:
    """The newest ``fxc.exe`` in any installed Windows SDK.

    Newest rather than first: an old SDK's fxc predates shader model features
    the FidelityFX headers use, and the failure is a compile error deep inside
    a vendored file rather than anything pointing at the SDK.
    """
    found = shutil.which("fxc")
    if found:
        return Path(found)

    candidates: list[Path] = []
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        import os

        base = os.environ.get(env)
        if not base:
            continue
        bin_dir = Path(base) / "Windows Kits" / "10" / "bin"
        if not bin_dir.is_dir():
            continue
        for version in bin_dir.iterdir():
            exe = version / "x64" / "fxc.exe"
            if exe.exists():
                candidates.append(exe)
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: p.parent.parent.name)[-1]


def find_msvc() -> tuple[Path, Path] | None:
    """``(vcvars64.bat, install root)`` for the newest MSVC with C++ tools."""
    import os

    program_files = os.environ.get("ProgramFiles(x86)") or os.environ.get("ProgramFiles")
    if not program_files:
        return None
    vswhere = Path(program_files) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.exists():
        return None
    try:
        out = subprocess.run(
            [
                str(vswhere), "-latest", "-products", "*",
                "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "-property", "installationPath",
            ],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None
    if not out:
        return None
    root = Path(out.splitlines()[0])
    vcvars = root / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    return (vcvars, root) if vcvars.exists() else None


# -- shaders ----------------------------------------------------------------


def compile_shaders(fxc: Path, out_dir: Path) -> dict[str, bytes]:
    """Every shader, as DXBC. Raises SystemExit naming the first failure."""
    out_dir.mkdir(parents=True, exist_ok=True)
    blobs: dict[str, bytes] = {}

    for source, entry, profile, symbol in SHADER_TARGETS:
        obj = out_dir / f"{Path(source).stem}_{entry}.cso"
        command = [
            str(fxc),
            "/nologo",
            "/T", profile,
            "/E", entry,
            "/O3",
            # Row-major matches how the constants are written on the CPU side.
            # The default is column-major, and the mismatch shows up as a
            # colour matrix that is silently transposed.
            "/Zpr",
            "/Fo", str(obj),
            str(SHADERS / source),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stdout, file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            _fail(f"{source}:{entry} did not compile")
        blobs[symbol] = obj.read_bytes()
        print(f"  {source:<16} {entry:<12} {profile:<8} {len(blobs[symbol]):>7} bytes")

    return blobs


def write_generated_header(blobs: dict[str, bytes], path: Path) -> None:
    """The shaders as C arrays.

    Generated rather than committed as .cso files next to the DLL: a loose
    shader blob is another file the packaging has to carry and another way a
    bundle can be half-installed. Compiled in, they cannot go missing.
    """
    lines = [
        "// GENERATED by tools/build_videofx.py -- do not edit.",
        "//",
        "// Shader bytecode, compiled offline. Runtime compilation would make",
        "// d3dcompiler_47.dll a redistributable dependency of an optional",
        "// feature, and would put a compiler on a video player's startup path.",
        "",
        "#pragma once",
        "#include <stdint.h>",
        "",
    ]
    for symbol, blob in blobs.items():
        lines.append(f"static const uint8_t {symbol}[] = {{")
        for start in range(0, len(blob), 16):
            chunk = blob[start:start + 16]
            lines.append("    " + "".join(f"0x{byte:02x}," for byte in chunk))
        lines.append("};")
        lines.append(f"static const size_t {symbol}Size = sizeof({symbol});")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    total = sum(len(b) for b in blobs.values())
    print(f"  -> {path.relative_to(ROOT)}  ({total} bytes of bytecode)")


# -- the library ------------------------------------------------------------


def build_windows_library(vcvars: Path) -> Path:
    """Compile and link ``rbgc_videofx.dll``.

    Static CRT (``/MT``). With ``/MD`` the DLL drags VCRUNTIME140.dll and
    MSVCP140.dll onto every user's machine and produces the classic "works on
    my machine": the build host has them, a clean Windows install may not, and
    the failure is a dialog naming a DLL nobody has heard of. 100 KB of size
    removes the whole class.
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dll = OUT_DIR / "rbgc_videofx.dll"
    build_dir = ROOT / "build" / "videofx"
    build_dir.mkdir(parents=True, exist_ok=True)

    sources = [
        NATIVE / "videofx.cpp",
        NATIVE / "d3d11_backend.cpp",
        NATIVE / "d3d11_source.cpp",
        NATIVE / "d3d11_render.cpp",
    ]
    for source in sources:
        if not source.exists():
            _fail(f"missing source {source.relative_to(ROOT)}")

    compile_args = " ".join(
        [
            "/nologo", "/c", "/EHsc", "/MT", "/O2", "/W3", "/GS-",
            # windows.h defines min and max as macros, which turns every
            # std::max( in this codebase into a syntax error pointing at
            # the "::" rather than at the macro. NOMINMAX is the fix and
            # it has to be global, not per-file, because the collision
            # happens wherever a Windows header is pulled in first.
            "/DNOMINMAX", "/DWIN32_LEAN_AND_MEAN",
            "/std:c++17",
            f"/I{NATIVE}",
            f"/Fo{build_dir}\\",
            *(str(s) for s in sources),
        ]
    )
    link_args = " ".join(
        [
            "/nologo", "/DLL", "/OPT:REF", "/OPT:ICF",
            f"/OUT:{dll}",
            f"{build_dir}\\*.obj",
            "d3d11.lib", "dxgi.lib", "dxguid.lib", "user32.lib",
        ]
    )
    # Through a batch file rather than `cmd /c "..."`. cmd strips one layer of
    # quotes from its argument, so a vcvars path containing spaces -- which is
    # every default install, "C:\Program Files\..." -- is torn in half and
    # reported as an unrecognised command naming half a path. A file has no
    # quoting layer to lose.
    batch = build_dir / "build.bat"
    steps = [
        "@echo off",
        f'call "{vcvars}" >nul',
        "if errorlevel 1 exit /b 1",
        f"cl {compile_args}",
        "if errorlevel 1 exit /b 1",
        f"link {link_args}",
    ]
    # Joined with plain newlines; write_text translates them to CRLF on
    # Windows, which is what cmd wants of a batch file.
    batch.write_text(chr(10).join(steps), encoding="utf-8")
    result = subprocess.run([str(batch)], capture_output=True, text=True, shell=True)
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        _fail("the library did not build")

    print(f"  -> {dll.relative_to(ROOT)}  ({dll.stat().st_size} bytes)")
    return dll


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shaders", action="store_true", help="compile shaders only"
    )
    parser.add_argument(
        "--check", action="store_true",
        help="compile shaders to a temporary directory and write nothing",
    )
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        _fail(
            "the Direct3D 11 backend builds on Windows. The Vulkan backend for "
            "Linux is built by the same script on a Linux host; see "
            "packaging/linux/provision.sh for what it needs."
        )

    print(f"rbgc_videofx for {platform.machine()}")

    fxc = find_fxc()
    if fxc is None:
        _fail(
            "fxc.exe was not found. Install a Windows SDK (the 'Desktop "
            "development with C++' workload includes one)."
        )
    print(f"  fxc: {fxc}")

    if args.check:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            compile_shaders(fxc, Path(tmp))
        print("shaders compile cleanly")
        return 0

    blobs = compile_shaders(fxc, ROOT / "build" / "videofx" / "shaders")
    write_generated_header(blobs, GENERATED)

    if args.shaders:
        return 0

    msvc = find_msvc()
    if msvc is None:
        _fail(
            "MSVC was not found. Install Visual Studio with the C++ tools, or "
            "pass --shaders to regenerate the shader header only."
        )
    print(f"  msvc: {msvc[1]}")
    build_windows_library(msvc[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

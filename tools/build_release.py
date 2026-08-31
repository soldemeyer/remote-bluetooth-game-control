"""Build and package the client and video server for distribution.

    python -m tools.build_release                 # everything this host can make
    python -m tools.build_release --target client
    python -m tools.build_release --only windows  # skip the WSL half
    python -m tools.build_release --setup-wsl     # provision the Linux side once

Output lands in ``dist/release/``: one ``.zip`` per app per platform, each
holding the program, a short README written for whoever receives it, and a
``SHA256SUMS`` covering the whole folder.

Two toolchains, and that is deliberate
--------------------------------------
Windows builds with PyInstaller from ``packaging/*.spec``; Linux builds with
Nuitka into an AppImage. The reasoning is in CLAUDE.md and does not need
repeating here -- what matters for this script is that **neither cross-compiles**.
A Windows host cannot emit an ELF binary and Nuitka emits native code, so the
Linux half runs inside WSL, which is a real Linux kernel on this machine rather
than an emulation of one.

The architecture still follows the host. WSL on an x86_64 PC produces an
x86_64 AppImage and nothing else; an aarch64 build for a Raspberry Pi has to
happen on a Pi. ``--collect`` merges artifacts built elsewhere into one release
folder so the checksums still cover the whole set.

Why the Linux build does not happen in the source tree
-----------------------------------------------------
Under WSL the repo lives on a DrvFs mount (``/mnt/f/...``). Nuitka does many
thousands of small writes and is an order of magnitude slower there, and
appimagetool cannot reliably set the executable bit on a file it creates on
that filesystem. So the work happens under ``$HOME`` inside WSL and only the
finished AppImage is copied back.

That last point is also why the packaging step sets the mode bits explicitly
rather than trusting what it reads off disk: an AppImage that arrives without
+x fails with a bare "permission denied", which reads as a corrupt download.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("rbgc.build")

BASE = Path(__file__).resolve().parent.parent
DIST = BASE / "dist"
RELEASE = DIST / "release"
BUILD = BASE / "build"

#: Where the Linux half does its work, inside WSL's own filesystem.
WSL_WORK = "$HOME/.cache/rbgc-build"

#: An optional virtualenv in WSL holding Nuitka and the runtime dependencies.
#: Preferred over the system interpreter when present -- ``--setup-wsl`` makes
#: it, and a system-wide pip install is increasingly refused on Debian-likes.
WSL_VENV = "$HOME/.rbgc-build-venv"


@dataclass(frozen=True)
class App:
    """One shippable program, and everything the two toolchains need for it."""

    key: str                 # --target name
    binary: str              # the executable's stem, both platforms
    product: str             # human name, and the archive's stem
    spec: str                # PyInstaller spec, relative to packaging/
    appimage_target: str     # argument to build-appimage.sh
    summary: str
    config_file: str
    console: bool            # whether it is useful from a terminal
    blurb: str               # the README body


APPS: tuple[App, ...] = (
    App(
        key="client",
        binary="rbgc-client",
        product="RBGC-Client",
        spec="client.spec",
        appimage_target="client",
        summary="Play a console remotely -- gamepad client",
        config_file="client.json",
        console=False,
        blurb="""\
Plug your gamepads into this PC and they drive a console somewhere else.

Getting connected
  1. Start it. Choose how to reach the server under "Connect":

       On this network         the server's address, for a LAN or a VPN
       Through a tunnel        a public address that forwards to the server
                               (frp, a router port forward, Tailscale)
       Over the Internet       a rendezvous broker introduces the two of you,
         (hole-punch)          then connects you directly where it can
       Over the Internet       everything goes through the broker. Slower, but
         (relay via broker)    it works where hole-punching cannot -- and it
                               skips the ~10 s of probing that would fail

  2. Enter the password the server's operator gave you.
  3. Press Search to find servers, or Custom to type the details yourself.
  4. Enable the controllers you want to stream, then Connect.

If hole-punching never succeeds, that is usually the network rather than a
setting: some connections cannot be traversed at all. Switch to relay mode.

Controller setup
  Each slot has a Configuration and a Controller type. "Configure..." opens the
  binding screen, where "Bind all types..." walks you through every button.
  Presets are a starting point, not a finished answer -- check them against the
  live preview, especially on an adapter or a mod kit.

Video
  If the operator has a video server running you get the picture automatically.
  Nothing to configure at this end.
""",
    ),
    App(
        key="video",
        binary="rbgc-video",
        product="RBGC-Video",
        spec="videoserver.spec",
        appimage_target="video",
        summary="Capture and stream a console's video to players",
        config_file="video.json",
        console=True,
        blurb="""\
Captures a console through a capture card and streams it to the players.

Run this on the machine the capture card is plugged into.

Getting started
  1. Start it and pick your capture device and resolution.
  2. Set a password. This is the VIDEO server's own password, and it is NOT the
     one the players use -- it authorises the controller server to configure
     this machine, nothing else. Keep them different.
  3. Leave it running. It waits; the controller server dials in to it.

  Then, in the controller server's web GUI, open the Video panel and either
  press Detect or type this machine's address, with the password from step 2.

Headless
  It runs with no window at all, which is what you want on a machine tucked
  behind a TV:

      rbgc-video --headless --media-bind 0.0.0.0:47810

  Set the password through the RBGC_PASSWORD environment variable rather than
  on the command line -- the process list is readable by anyone on the machine.

No picture reaching anyone?
  "Streaming" means frames are actually flowing, not that a thread is alive. If
  it says the device is open but nothing is streaming, the capture device is
  usually the cause rather than the network. --list-devices shows what this
  machine can see.
""",
    ),
)


# --------------------------------------------------------------------------
# small helpers


def repo_version() -> str:
    """The version from pyproject, which is the only place it is written."""
    text = (BASE / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "0.0.0"


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> int:
    """Run a command, letting its output through to the terminal.

    Not captured on purpose: these are builds that take minutes, and a silent
    terminal followed by a wall of text at the end is far worse to watch than
    the compiler's own progress.
    """
    log.info("$ %s", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(cwd or BASE))
    if check and result.returncode != 0:
        raise BuildError(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return result.returncode


class BuildError(RuntimeError):
    """Something the operator has to fix. Reported without a traceback."""


def host_arch() -> str:
    """Normalised, because the two platforms spell the same chip differently."""
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "aarch64"
    return machine or "unknown"


# --------------------------------------------------------------------------
# Windows: PyInstaller


def _clear_output(folder: Path, timeout_s: float = 90.0) -> None:
    """Remove a previous build, waiting out a scanner that holds a file open.

    PyInstaller cleans its own output directory and dies outright if it cannot
    -- ``PermissionError: [WinError 5]`` on an .exe with no process running it.
    That is antivirus reading a freshly written binary, and on Windows it is
    routine rather than exceptional: the tree is >100 MB of executables, and it
    is scanned right after the previous app wrote it.

    The budget is generous on purpose. A short one *looks* fine on an idle
    machine and then fails a twenty-minute build on a busy one -- measured: five
    seconds was enough until a full test run was sharing the CPU, and then it
    was not. Waiting a minute costs nothing next to what it protects.
    """
    import time

    deadline = time.monotonic() + timeout_s
    reported = False

    while True:
        if not folder.exists():
            return
        try:
            # An error handler rather than a bare rmtree: it clears the
            # read-only bit and retries the individual file, so one locked
            # entry does not prevent removing everything else on this pass.
            shutil.rmtree(folder, **{_RMTREE_HANDLER: _force_remove})
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise BuildError(
                    f"cannot clear {folder} after {timeout_s:.0f}s: {exc}\n"
                    + _who_holds(folder)
                ) from exc
            if not reported:
                log.info("  %s is locked (antivirus?); waiting", folder.name)
                reported = True
            time.sleep(1.0)


#: `onexc` replaced `onerror` in 3.12. The two callbacks take the same three
#: positional arguments and differ only in the third's type, which this handler
#: ignores -- so one function serves both, and the project keeps working on the
#: 3.11 it declares support for.
_RMTREE_HANDLER = "onexc" if sys.version_info >= (3, 12) else "onerror"


def _force_remove(func, path, _exc) -> None:
    """rmtree error handler: drop the read-only bit and try once more."""
    import stat
    import time

    os.chmod(path, stat.S_IWRITE)
    time.sleep(0.2)
    func(path)


def _who_holds(folder: Path) -> str:
    """Name the process holding a file in ``folder``, if we can find it.

    Worth the effort. "Something is holding a file open" is true and useless:
    the two causes -- a scanner mid-pass, and a copy of the app still running --
    need opposite responses, and only one of them goes away on its own.

    Measured: a build failed after waiting 90 s on a lock that was an orphaned
    ``rbgc-client.exe`` left behind by an earlier test run. It would never have
    cleared, and the message sent the operator looking at antivirus.
    """
    if sys.platform != "win32":
        return (
            "  Something is holding a file open. Close any running copy of the "
            "app.\n  `lsof +D " + str(folder) + "` will name it."
        )

    # tasklist rather than psutil: no dependency, and this path runs once, on
    # the way to failing.
    try:
        listing = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        listing = ""

    wanted = {p.name.lower() for p in folder.glob("*.exe")}
    running = []
    for line in listing.splitlines():
        parts = [f.strip('"') for f in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() in wanted:
            running.append(f"{parts[0]} (PID {parts[1]})")

    if running:
        return (
            "  Still running: " + ", ".join(running) + "\n"
            "  That is the lock -- it will not clear on its own. Close it, or:\n"
            "    taskkill /F /IM " + sorted(wanted)[0]
        )
    return (
        "  No matching process is running, so this is most likely antivirus\n"
        "  scanning dist/. Retry, or exclude the build directory."
    )


def build_windows(app: App) -> Path:
    """Build one app with PyInstaller. Returns the onedir folder."""
    if not _have_module("PyInstaller"):
        raise BuildError(
            "PyInstaller is not installed in this interpreter.\n"
            '  pip install -e ".[client,video,dev]"'
        )

    spec = BASE / "packaging" / app.spec
    if not spec.is_file():
        raise BuildError(f"missing spec: {spec}")

    log.info("building %s for Windows", app.product)
    # Ahead of PyInstaller, which would do the same thing and give up on the
    # first refusal.
    _clear_output(DIST / app.binary)
    run([
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--distpath", str(DIST),
        "--workpath", str(BUILD / "pyinstaller"),
        str(spec),
    ])

    out = DIST / app.binary
    if not out.is_dir():
        raise BuildError(f"PyInstaller reported success but {out} is missing")
    return out


def _have_module(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


# --------------------------------------------------------------------------
# Linux: Nuitka, through WSL


def wsl_distro() -> str:
    """The distro to build in. RBGC_WSL_DISTRO overrides the default."""
    return os.environ.get("RBGC_WSL_DISTRO", "").strip()


def _wsl(args: list[str], *, check: bool = True, capture: bool = False):
    cmd = ["wsl"]
    distro = wsl_distro()
    if distro:
        cmd += ["-d", distro]
    cmd += ["--"] + args
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    log.info("$ %s", " ".join(cmd))
    result = subprocess.run(cmd)
    if check and result.returncode != 0:
        raise BuildError(f"WSL command failed ({result.returncode})")
    return result


def wsl_probe() -> tuple[bool, str]:
    """Is WSL usable? Returns (ok, an explanation worth printing).

    Worth being specific. A broken WSL service and an unprovisioned distro
    present identically at the call site -- the build simply does not happen --
    and the fix for each is completely different.
    """
    if shutil.which("wsl") is None:
        return False, (
            "wsl.exe not found. Install WSL with:  wsl --install -d Ubuntu"
        )

    probe = _wsl(["uname", "-m"], capture=True)
    if probe.returncode != 0:
        detail = ((probe.stderr or probe.stdout or "").replace("\x00", "").strip())
        hint = ""
        if "E_UNEXPECTED" in detail or "catastrophic" in detail.lower():
            # Seen on a current Windows build with an old WSL: the service
            # starts and immediately fails, and --shutdown does not help.
            hint = (
                "\n  This usually means the WSL package is older than Windows.\n"
                "  Fix with:   wsl --update      (then reopen the terminal)"
            )
        return False, f"WSL is installed but will not start.{hint}\n  {detail}"

    arch = probe.stdout.strip()
    return True, arch


def wsl_path(win_path: Path) -> str:
    """Translate a Windows path for WSL, asking WSL rather than guessing."""
    result = _wsl(["wslpath", "-a", str(win_path).replace("\\", "/")], capture=True)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    # Fallback for the drive-letter case, which is all we ever pass.
    text = str(win_path).replace("\\", "/")
    if len(text) > 2 and text[1] == ":":
        return f"/mnt/{text[0].lower()}{text[2:]}"
    raise BuildError(f"cannot translate {win_path} for WSL")


def _write_sh(path: Path, body: str) -> None:
    """Write a shell script with LF endings.

    Non-negotiable: bash reads a CRLF script's shebang as ``/bin/bash\\r`` and
    fails with "bad interpreter", which names a file that plainly exists.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)


def setup_script(repo: str) -> str:
    """The provisioning script, as a pure function so it can be inspected.

    Separate from :func:`setup_wsl` because what matters is the *text that
    runs*, and a test asserting against this module's own source would trip
    over the comments explaining it.
    """
    return f"""#!/usr/bin/env bash
set -euo pipefail
cd "{repo}"

echo "==> system packages and appimagetool (needs sudo)"
# Not `sudo -E`: many sudoers configurations refuse to preserve the whole
# environment and warn about it, and provision.sh sets what it needs itself.
sudo bash packaging/linux/provision.sh

echo "==> build virtualenv at {WSL_VENV}"
python3 -m venv "{WSL_VENV}" 2>/dev/null || true
"{WSL_VENV}/bin/python3" -m pip install --upgrade pip wheel

# Installed non-editable and from a copy of the tree: an editable install would
# point at /mnt/f, and Nuitka would then compile every import across the DrvFs
# mount on every run.
"{WSL_VENV}/bin/python3" -m pip install ".[client,video,linux-build]"

echo "==> verifying"
# `python -m nuitka --version`, not `nuitka.__version__` -- the module does not
# define that attribute, so importing it and reading it raises AttributeError.
# Under `set -e` that failed the whole provisioning run *after* it had
# completely succeeded, which is the worst way round to get this wrong.
"{WSL_VENV}/bin/python3" -m nuitka --version | head -1
command -v appimagetool

# The three dynamic-import blind spots, checked here rather than discovered
# after a twenty-minute compile: each is loaded in a way static analysis
# cannot see, so a missing one builds cleanly and dies at first use.
"{WSL_VENV}/bin/python3" - <<'PY'
import av, nacl.bindings, sdl2, PySide6
print("imports OK: av, nacl, sdl2, PySide6")
PY

echo "==> done"
"""


def setup_wsl() -> None:
    """Provision the WSL side: system packages, appimagetool, and a venv."""
    ok, detail = wsl_probe()
    if not ok:
        raise BuildError(detail)
    log.info("provisioning WSL (%s). sudo may prompt for your password.", detail)

    script = BUILD / "wsl-setup.sh"
    _write_sh(script, setup_script(wsl_path(BASE)))
    _wsl(["bash", wsl_path(script)])


def build_script(repo: str, targets: str) -> str:
    """The AppImage build script. Pure, for the same reason as above."""
    return f"""#!/usr/bin/env bash
set -euo pipefail
cd "{repo}"

# Prefer the build venv when it exists. A bare `python3` on a modern Debian
# refuses to pip install anything, so this is the normal case rather than the
# exception.
if [ -x "{WSL_VENV}/bin/python3" ]; then
  export PATH="{WSL_VENV}/bin:$PATH"
fi

if ! command -v appimagetool >/dev/null 2>&1; then
  echo "appimagetool missing -- run: python -m tools.build_release --setup-wsl" >&2
  exit 1
fi
if ! python3 -c 'import nuitka' >/dev/null 2>&1; then
  echo "nuitka missing -- run: python -m tools.build_release --setup-wsl" >&2
  exit 1
fi

# Off the DrvFs mount: see the module docstring. Both the speed and the
# executable bit depend on this.
export RBGC_WORK_DIR="{WSL_WORK}/work"
export RBGC_OUT_DIR="{WSL_WORK}/out"
mkdir -p "$RBGC_WORK_DIR" "$RBGC_OUT_DIR"

for target in {targets}; do
  bash packaging/linux/build-appimage.sh "$target"
done

mkdir -p dist/linux
cp -f "$RBGC_OUT_DIR"/*.AppImage dist/linux/
ls -lh dist/linux/*.AppImage
"""


def build_linux(apps: list[App]) -> list[Path]:
    """Build AppImages inside WSL. Returns what landed in dist/linux."""
    ok, detail = wsl_probe()
    if not ok:
        raise BuildError(detail)

    log.info("building AppImages in WSL (%s)", detail)

    targets = " ".join(app.appimage_target for app in apps)
    script = BUILD / "wsl-build.sh"
    _write_sh(script, build_script(wsl_path(BASE), targets))
    _wsl(["bash", wsl_path(script)])

    produced = sorted((DIST / "linux").glob("*.AppImage"))
    if not produced:
        raise BuildError("WSL reported success but no AppImage appeared")
    return produced


# --------------------------------------------------------------------------
# packaging


def readme_for(app: App, version: str, kind: str, arch: str) -> str:
    """The note that ships beside the program.

    Written for whoever is handed the zip, not for whoever built it -- so it
    says how to start the thing and where its settings live, and nothing about
    how it was compiled.
    """
    if kind == "windows":
        launch = (
            f"  Unzip anywhere, open the {app.binary} folder, and run "
            f"{app.binary}.exe.\n\n"
            "  Windows SmartScreen will warn about an unrecognised publisher --\n"
            "  the build is not code-signed. \"More info\" then \"Run anyway\".\n"
        )
        config = f"  %APPDATA%\\rbgc\\{app.config_file}"
    else:
        launch = (
            f"  chmod +x {app.product}-{arch}.AppImage\n"
            f"  ./{app.product}-{arch}.AppImage\n\n"
            "  The chmod is needed because a zip does not always carry the\n"
            "  executable bit across platforms.\n\n"
            "  An AppImage needs FUSE 2. On a distro that ships only FUSE 3 it\n"
            "  exits with an error about libfuse.so.2, which looks like a bad\n"
            "  download but is not:\n"
            "      sudo apt install libfuse2      (or libfuse2t64)\n"
        )
        config = f"  ~/.config/rbgc/{app.config_file}"

    return f"""{app.product} {version}
{'=' * (len(app.product) + len(version) + 1)}

{app.summary}

{app.blurb}
Running it
{launch}
Where its settings live
{config}

  Delete that file to start over. It holds no password unless you asked for
  one to be remembered.

Verifying the download
  SHA256SUMS sits beside this archive:
      sha256sum -c SHA256SUMS              (Linux)
      Get-FileHash <file> -Algorithm SHA256 (Windows PowerShell)

Everything travels encrypted
  Input and video are sealed end to end with a key derived from the password.
  A rendezvous broker or a relay in the middle forwards bytes it cannot read.
"""


def _zip_add(archive: zipfile.ZipFile, source: Path, arcname: str, *, executable: bool) -> None:
    """Add one file, setting its mode deliberately.

    Whatever the source filesystem reported is not to be trusted here: a file
    copied out of WSL onto DrvFs routinely loses +x, and an AppImage without it
    fails on the recipient's machine with a bare "permission denied".
    """
    info = zipfile.ZipInfo.from_file(source, arcname)
    info.compress_type = zipfile.ZIP_DEFLATED
    # Unix mode lives in the high 16 bits of external_attr. Regular file plus
    # 0755 or 0644; unzip on Linux restores it, Windows ignores it.
    info.external_attr = ((0o100000 | (0o755 if executable else 0o644)) << 16)
    with open(source, "rb") as handle:
        archive.writestr(info, handle.read())


def _zip_text(
    archive: zipfile.ZipFile, arcname: str, text: str, *, crlf: bool = False
) -> None:
    """Add a generated text file with a sane mode and the right line endings.

    ``writestr`` with a plain name leaves external_attr at 0, which some
    extractors render as 0600 -- readable by whoever unzipped it and nobody
    else, which is wrong for a README meant to be handed on.

    ``crlf`` for the Windows archive: a LF-only README opens as one unbroken
    line in a fair number of Windows text viewers still in use.
    """
    info = zipfile.ZipInfo(arcname)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o100644 << 16)
    if crlf:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    archive.writestr(info, text)


def package_windows(app: App, folder: Path, version: str, arch: str) -> Path:
    RELEASE.mkdir(parents=True, exist_ok=True)
    name = f"{app.product}-{version}-windows-{arch}.zip"
    target = RELEASE / name
    log.info("packaging %s", name)

    readme = readme_for(app, version, "windows", arch)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                arc = f"{app.binary}/{path.relative_to(folder).as_posix()}"
                _zip_add(archive, path, arc, executable=path.suffix.lower() == ".exe")
        _zip_text(archive, f"{app.binary}/README.txt", readme, crlf=True)
    return target


def package_linux(app: App, appimage: Path, version: str, arch: str) -> Path:
    RELEASE.mkdir(parents=True, exist_ok=True)
    name = f"{app.product}-{version}-linux-{arch}.zip"
    target = RELEASE / name
    log.info("packaging %s", name)

    readme = readme_for(app, version, "linux", arch)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        _zip_add(archive, appimage, appimage.name, executable=True)
        _zip_text(archive, "README.txt", readme)
    return target


def write_checksums(folder: Path) -> Path | None:
    """One SHA256SUMS covering every archive present.

    Rewritten from what is on disk rather than appended to, so a --collect run
    that adds another platform's artifacts produces a file describing the whole
    set instead of half of it.
    """
    archives = sorted(p for p in folder.glob("*.zip") if p.is_file())
    if not archives:
        return None

    lines = []
    for path in archives:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}")

    # LF, explicitly. Path.write_text on Windows translates to CRLF, and
    # `sha256sum -c` then fails on every line with "No such file or directory"
    # naming a filename with a stray \r on the end -- a file that plainly
    # exists. This file is read on Linux almost by definition.
    target = folder / "SHA256SUMS"
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    return target


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checksums(folder: Path, *, retries: int = 3) -> list[str]:
    """Re-read every archive and confirm it still matches. Returns mismatches.

    Not paranoia -- measured. On the first full run an archive was modified
    **ten seconds after** it was hashed, by something outside this script
    (a scanner finishing its pass over a large archive full of executables is
    the likely culprit on Windows). The checksum file was already written, so
    the release shipped with one entry that could never verify.

    That is a bad failure: whoever downloads it concludes the file is corrupt,
    or worse, tampered with. Verifying here turns it into an error the operator
    sees, and re-hashing fixes it in place -- the content is fine, only the
    record of it was stale.
    """
    import time

    for attempt in range(retries):
        recorded = {}
        for line in (folder / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            if line.strip():
                digest, _, name = line.partition("  ")
                recorded[name] = digest

        bad = [
            name for name, digest in recorded.items()
            if (folder / name).is_file() and _digest(folder / name) != digest
        ]
        if not bad:
            return []

        if attempt == retries - 1:
            return bad

        # Settle, then re-record. Whatever touched it has almost always
        # finished by now, and re-hashing is cheaper than a wrong release.
        log.warning(
            "  %s changed after hashing; re-recording", ", ".join(sorted(bad))
        )
        time.sleep(2)
        write_checksums(folder)

    return []


def collect(source: Path) -> int:
    """Merge artifacts built on another machine into this release folder."""
    if not source.is_dir():
        raise BuildError(f"not a directory: {source}")

    RELEASE.mkdir(parents=True, exist_ok=True)
    found = 0
    for path in sorted(source.glob("*.zip")):
        shutil.copy2(path, RELEASE / path.name)
        log.info("collected %s", path.name)
        found += 1
    if not found:
        log.warning("no .zip archives found in %s", source)
    return found


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.build_release",
        description="Build and package the client and video server for distribution.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Neither toolchain cross-compiles, so the Linux half runs inside WSL and follows
this machine's architecture. An aarch64 AppImage for a Raspberry Pi has to be
built on a Pi; use --collect to fold it in afterwards.
""",
    )
    parser.add_argument(
        "--target", choices=[app.key for app in APPS] + ["both"], default="both",
        help="which app to build (default: both)",
    )
    parser.add_argument(
        "--only", choices=["windows", "linux"], default=None,
        help="build for one platform only",
    )
    parser.add_argument(
        "--setup-wsl", action="store_true",
        help="provision the WSL side (system packages, appimagetool, venv), then exit",
    )
    parser.add_argument(
        "--collect", metavar="DIR", default=None,
        help="copy archives built on another machine into dist/release, then re-checksum",
    )
    parser.add_argument(
        "--skip-build", action="store_true",
        help="package what is already in dist/ without rebuilding",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="remove dist/ and build/ first",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    try:
        return _run(args)
    except BuildError as exc:
        # A build problem is the operator's to fix, so it gets a sentence and
        # not a traceback pointing into this file.
        log.error("\nBuild failed: %s", exc)
        return 1
    except KeyboardInterrupt:
        log.error("\nInterrupted.")
        return 130


def _run(args) -> int:
    version = repo_version()
    arch = host_arch()

    if args.setup_wsl:
        setup_wsl()
        log.info("\nWSL is ready. Now run:  python -m tools.build_release")
        return 0

    if args.collect:
        collect(Path(args.collect))
        checksums = write_checksums(RELEASE)
        if checksums:
            log.info("wrote %s", checksums)
            if verify_checksums(RELEASE):
                raise BuildError("SHA256SUMS does not match the archives")
        _report(version)
        return 0

    if args.clean:
        for folder in (DIST, BUILD):
            if folder.exists():
                log.info("removing %s", folder)
                # Not ignore_errors: that prints "removing" and then silently
                # leaves a locked tree behind, so the next step meets a
                # directory this run claims to have deleted.
                _clear_output(folder)

    apps = [a for a in APPS if args.target in (a.key, "both")]
    windows = args.only in (None, "windows")
    linux = args.only in (None, "linux")

    # A Windows PyInstaller build only makes sense on Windows. On Linux the
    # same script builds AppImages natively and skips WSL entirely.
    on_windows = sys.platform == "win32"
    if windows and not on_windows:
        log.info("skipping the Windows build: PyInstaller cannot cross-compile")
        windows = False

    made: list[Path] = []
    problems: list[str] = []

    if windows:
        for app in apps:
            folder = DIST / app.binary if args.skip_build else build_windows(app)
            if not folder.is_dir():
                problems.append(f"{app.product}: nothing at {folder}")
                continue
            made.append(package_windows(app, folder, version, arch))

    if linux:
        try:
            if args.skip_build:
                images = sorted((DIST / "linux").glob("*.AppImage"))
                if not images:
                    raise BuildError("no AppImage in dist/linux to package")
            elif on_windows:
                images = build_linux(apps)
            else:
                run(["bash", "packaging/linux/build-appimage.sh", args.target])
                images = sorted((DIST / "linux").glob("*.AppImage"))

            for app in apps:
                match = [p for p in images if p.name.startswith(app.product)]
                if not match:
                    problems.append(f"{app.product}: no AppImage produced")
                    continue
                made.append(package_linux(app, match[0], version, arch))
        except BuildError as exc:
            # Not fatal to the run. A finished Windows archive is worth having
            # even when the Linux side needs attention, and saying so here is
            # more use than failing the whole build with one message.
            problems.append(f"Linux build skipped: {exc}")

    checksums = write_checksums(RELEASE)
    if checksums:
        # An archive touched after hashing ships a checksum nobody can verify,
        # and the recipient reasonably concludes the download is corrupt.
        stale = verify_checksums(RELEASE)
        if stale:
            problems.append(
                "SHA256SUMS does not match: " + ", ".join(sorted(stale))
                + " (the archives are intact; something rewrote them after "
                  "hashing -- re-run with --skip-build to re-record)"
            )

    log.info("")
    for path in made:
        log.info("  %s  (%.1f MB)", path.name, path.stat().st_size / 1e6)
    if checksums:
        log.info("  %s", checksums.name)

    if problems:
        log.warning("\nNot everything was produced:")
        for problem in problems:
            log.warning("  - %s", problem)

    _report(version)
    return 1 if problems and not made else 0


def _report(version: str) -> None:
    log.info("\nRelease %s in %s", version, RELEASE)


if __name__ == "__main__":
    raise SystemExit(main())

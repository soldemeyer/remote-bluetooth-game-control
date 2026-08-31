#!/usr/bin/env bash
# Build a distributable AppImage of the client or the video server.
#
#     packaging/linux/build-appimage.sh client
#     packaging/linux/build-appimage.sh video
#     packaging/linux/build-appimage.sh both
#
# Output: dist/linux/RBGC-Client-<arch>.AppImage (and RBGC-Video-...).
#
# Nuitka rather than PyInstaller
# -----------------------------
# The Windows builds use PyInstaller and stay that way. This is deliberately a
# second toolchain rather than a port: PyInstaller's onedir layout is awkward
# to wrap in an AppDir, and Nuitka's --standalone output is already the shape
# AppImage wants -- a self-contained tree with a single entry binary.
#
# What has to be forced, and why static analysis misses it
# -------------------------------------------------------
# The same three blind spots the PyInstaller spec documents, in Nuitka's
# spelling. All three fail at *runtime*, in a bundle that built cleanly:
#
#   * **SDL2** ships inside pysdl2-dll and is loaded through ctypes, so nothing
#     imports it in a way a compiler can see.
#   * **libsodium** is reached by PyNaCl through cffi; the generated binding
#     module is loaded dynamically.
#   * **PyAV** is a large set of Cython extensions that import each other
#     dynamically, and carries the FFmpeg shared libraries inside its wheel.
#
# Qt is handled by Nuitka's pyside6 plugin, which knows about the platform
# plugins and the QML/Qt resource layout. Do not hand-copy Qt libraries around
# it; the plugin also patches the plugin search path, which a manual copy does
# not.
set -euo pipefail

TARGET=${1:-both}
BASE=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
ARCH=$(uname -m)

# Overridable so a driver can put the heavy work somewhere better than the
# source tree. Under WSL that is not a preference: the repo lives on a DrvFs
# mount, where Nuitka's many thousands of small writes run an order of
# magnitude slower, and appimagetool cannot reliably set the executable bit on
# what it produces there.
OUT="${RBGC_OUT_DIR:-${BASE}/dist/linux}"
WORK="${RBGC_WORK_DIR:-${BASE}/build/linux}"

# Single source of truth: pyproject. A second copy of the version here drifts,
# silently, and only shows up in file properties nobody reads.
#
# `tr -d '\r'` is load-bearing, not defensive. A repo checked out on Windows and
# built through WSL has CRLF line endings, so the capture keeps a trailing
# carriage return -- and Nuitka writes the value straight into a C header:
#
#     #define NUITKA_FILE_VERSION "0.1.0
#     "
#
# which fails the build with "missing terminating \" character" in generated
# code, naming nothing that appears in this file.
VERSION=$(sed -n 's/^version = "\([^"]*\)".*/\1/p' "${BASE}/pyproject.toml" \
          | head -1 | tr -d '\r')
VERSION=${VERSION:-0.0.0}

command -v appimagetool >/dev/null 2>&1 || {
  echo "appimagetool not found -- run packaging/linux/provision.sh first" >&2
  exit 1
}
python3 -c 'import nuitka' 2>/dev/null || {
  echo "nuitka not installed -- pip install nuitka in the build venv" >&2
  exit 1
}

mkdir -p "$OUT" "$WORK"

build_one() {
  local name="$1" entry="$2" appname="$3" icon="$4" comment="$5"

  # Nuitka names its output directory after the **entry script**, not after
  # --output-filename: `client/main.py` produces `main.dist` holding a binary
  # called `rbgc-client`. Assuming otherwise looked right and failed with
  # `cp: cannot stat '.../rbgc-client.dist/.'` after a full compile.
  #
  # Both apps' entry points are `main.py`, so a shared --output-dir would also
  # have the second build overwrite the first. Giving each its own subdirectory
  # fixes the naming and the collision together.
  local entry_base
  entry_base=$(basename "$entry" .py)
  local outdir="${WORK}/${name}"
  local dist="${outdir}/${entry_base}.dist"
  local appdir="${WORK}/${name}.AppDir"

  echo "==> compiling ${appname} with Nuitka (this takes a while)"
  rm -rf "$outdir" "$appdir"

  # --include-package for the three dynamic-import blind spots above.
  # --include-package-data for the shared objects that live *inside* those
  # packages: without it Nuitka collects the Python and leaves the .so behind,
  # which fails at first use rather than at import.
  # --include-package-data=sdl2dll is NOT enough for libSDL2 itself.
  #
  # Measured: it collects sdl2dll's *dependencies* (libopus, libwebp, libogg,
  # ...) and leaves out `libSDL2-2.0.so`, because Nuitka classifies that one as
  # a DLL rather than package data and expects whoever imports it to pull it in.
  # Nothing does -- PySDL2 dlopens it through ctypes at runtime, which no static
  # analysis can see.
  #
  # The bundle then compiles cleanly, starts, prints sdl2dll's own "Using SDL2
  # binaries from pysdl2-dll" banner, and *then* fails `import sdl2` -- which
  # the client reports as "PySDL2 is not installed", on a build that ships it.
  local sdl_dlls
  sdl_dlls=$(python3 -c 'import os, sdl2dll; print(os.path.join(os.path.dirname(sdl2dll.__file__), "dll"))')
  if [ ! -f "${sdl_dlls}/libSDL2-2.0.so" ]; then
    echo "libSDL2-2.0.so not found under ${sdl_dlls}" >&2
    exit 1
  fi

  python3 -m nuitka \
    --standalone \
    --assume-yes-for-downloads \
    --output-dir="$outdir" \
    --output-filename="$name" \
    --enable-plugin=pyside6 \
    --include-package=sdl2 \
    --include-package=sdl2dll \
    --include-package-data=sdl2dll \
    --include-data-files="${sdl_dlls}/libSDL2*.so*=sdl2dll/dll/" \
    --include-package=nacl \
    --include-package-data=nacl \
    --include-package=cffi \
    --include-module=_cffi_backend \
    --include-package=av \
    --include-package-data=av \
    --include-data-dir="${BASE}/client/gui/assets=client/gui/assets" \
    --include-data-dir="${BASE}/videoserver/assets=videoserver/assets" \
    --nofollow-import-to=pytest \
    --nofollow-import-to=aiohttp \
    --nofollow-import-to=dbus_next \
    --nofollow-import-to=pyudev \
    --company-name=RBGC \
    --product-name="$appname" \
    --file-version="$VERSION" \
    "$entry"

  # Fail with the reason rather than a bare `cp: cannot stat`. If Nuitka ever
  # changes how it names the output directory this is the line that notices,
  # and it says what to look at.
  if [ ! -d "$dist" ]; then
    echo "Nuitka produced no '${dist}'. It names that directory after the" >&2
    echo "entry script, so check what actually appeared:" >&2
    ls -d "${outdir}"/*.dist 2>/dev/null >&2 || ls -A "$outdir" >&2
    exit 1
  fi
  if [ ! -x "${dist}/${name}" ]; then
    echo "No executable '${name}' inside '${dist}'." >&2
    exit 1
  fi

  echo "==> assembling ${name}.AppDir"
  mkdir -p "$appdir/usr/bin" "$appdir/usr/share/applications" \
           "$appdir/usr/share/icons/hicolor/256x256/apps"
  cp -a "$dist"/. "$appdir/usr/bin/"
  cp "$icon" "$appdir/usr/share/icons/hicolor/256x256/apps/${name}.png"
  cp "$icon" "$appdir/${name}.png"

  cat > "$appdir/usr/share/applications/${name}.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=${appname}
Comment=${comment}
Exec=${name}
Icon=${name}
Categories=Game;Utility;
Terminal=false
DESKTOP
  cp "$appdir/usr/share/applications/${name}.desktop" "$appdir/${name}.desktop"

  # AppRun, not a symlink to the binary.
  #
  # Nuitka's standalone tree resolves its own libraries relative to the
  # executable, and an AppImage is mounted at a path that changes every run.
  # Entering the directory first is what makes that relative resolution hold;
  # a symlink leaves the process's cwd wherever the user launched it from and
  # the app dies looking for its own .so files.
  cat > "$appdir/AppRun" <<APPRUN
#!/bin/sh
HERE=\$(dirname "\$(readlink -f "\$0")")
cd "\$HERE/usr/bin" || exit 1
exec ./${name} "\$@"
APPRUN
  chmod +x "$appdir/AppRun"

  echo "==> packing ${appname}"
  # ARCH is read from the environment by appimagetool; it cannot infer it from
  # a directory and refuses to guess.
  ARCH="$ARCH" appimagetool "$appdir" "${OUT}/${appname// /-}-${ARCH}.AppImage"
}

if [ "$TARGET" = client ] || [ "$TARGET" = both ]; then
  build_one rbgc-client "${BASE}/client/main.py" "RBGC-Client" \
    "${BASE}/client/gui/assets/icon.png" \
    "Play a console remotely -- gamepad client"
fi

if [ "$TARGET" = video ] || [ "$TARGET" = both ]; then
  build_one rbgc-video "${BASE}/videoserver/main.py" "RBGC-Video" \
    "${BASE}/videoserver/assets/icon.png" \
    "Capture and stream a console's video to players"
fi

echo
echo "==> built:"
ls -lh "$OUT"/*.AppImage 2>/dev/null || echo "  (nothing -- check the log above)"

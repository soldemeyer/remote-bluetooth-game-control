#!/usr/bin/env bash
# Prepare a Linux host to build the RBGC AppImages.
#
# Run once per build host:
#
#     sudo packaging/linux/provision.sh
#
# What it installs and why
# ------------------------
# * build-essential / patchelf -- Nuitka compiles the app to C and then
#   rewrites the RPATHs of everything it collects. Without patchelf the
#   standalone tree references libraries by absolute path and only runs on the
#   machine that built it.
# * libgl1 / libegl1 / xkbcommon / fontconfig -- Qt's platform plugins link
#   these. They are not needed to *build*, but appimagetool bundles what the
#   binaries actually reference, so they must be present at build time or the
#   AppImage is missing them and dies with "could not load the Qt platform
#   plugin xcb" on a clean machine.
# * FUSE 2 -- what an AppImage mounts itself with at runtime. Modern distros
#   ship FUSE 3, and an AppImage on a FUSE-3-only host fails with a bare
#   "dlopen(): error loading libfuse.so.2", which reads as a corrupt download.
#
# The architecture you build on is the architecture you get: Nuitka emits
# native code and AppImages are not portable across ISAs. Build x86_64 on
# x86_64, aarch64 on aarch64.
set -euo pipefail

APPIMAGETOOL_DIR=${APPIMAGETOOL_DIR:-/usr/local/bin}
ARCH=$(uname -m)

echo "==> provisioning an ${ARCH} AppImage build host"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
    build-essential patchelf ccache \
    python3 python3-venv python3-dev python3-pip \
    libfuse2t64 file desktop-file-utils zsync wget ca-certificates \
    libgl1 libegl1 libxkbcommon0 libxkbcommon-x11-0 libfontconfig1 \
    libdbus-1-3 libxcb-cursor0 libxcb-xinerama0 libxcb-icccm4 \
    libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
    libasound2t64 libpulse0 \
  || apt-get install -y --no-install-recommends libfuse2 libasound2

# appimagetool is distributed only as an AppImage, so fetch the one matching
# this host's architecture rather than assuming x86_64.
case "$ARCH" in
  x86_64)  TOOL_ARCH=x86_64 ;;
  aarch64) TOOL_ARCH=aarch64 ;;
  *) echo "unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

if ! command -v appimagetool >/dev/null 2>&1; then
  echo "==> fetching appimagetool-${TOOL_ARCH}"
  wget -q -O "${APPIMAGETOOL_DIR}/appimagetool" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${TOOL_ARCH}.AppImage"
  chmod +x "${APPIMAGETOOL_DIR}/appimagetool"
fi

echo "==> done. appimagetool: $(command -v appimagetool)"

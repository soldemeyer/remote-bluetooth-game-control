"""Bundled assets for the video server, and the loader for them.

Mirrors ``client/gui/assets`` deliberately, including resolving paths at
runtime: a PyInstaller bundle relocates everything under ``sys._MEIPASS``, so a
path computed at build time would point at the developer's source tree.
"""

from __future__ import annotations

import sys
from pathlib import Path


def assets_dir() -> Path:
    """Where the asset files live, source checkout or frozen bundle alike."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "videoserver" / "assets"
    return Path(__file__).resolve().parent


def app_icon():
    """The window and taskbar icon.

    Returns an empty ``QIcon`` if the file is missing rather than raising: a
    missing icon should cost a decoration, not prevent the app starting.
    """
    from PySide6.QtGui import QIcon

    for name in ("icon.ico", "icon.png"):
        path = assets_dir() / name
        if path.exists():
            return QIcon(str(path))
    return QIcon()

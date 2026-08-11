"""Bundled GUI assets and the loaders for them.

Paths are resolved at runtime rather than baked in, because a PyInstaller bundle
relocates everything under ``sys._MEIPASS`` and a path computed at build time
would point at the developer's source tree.
"""

from __future__ import annotations

import sys
from pathlib import Path


def assets_dir() -> Path:
    """Where the asset files live, source checkout or frozen bundle alike."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "client" / "gui" / "assets"
    return Path(__file__).resolve().parent


def app_icon():
    """The window and taskbar icon.

    Returns an empty ``QIcon`` if the file is missing rather than raising: a
    missing icon should cost a decoration, not prevent the client starting.
    """
    from PySide6.QtGui import QIcon

    for name in ("icon.ico", "icon.png"):
        path = assets_dir() / name
        if path.exists():
            return QIcon(str(path))
    return QIcon()

"""Render client/gui/assets/icon.svg to the PNG and ICO the app and installer need.

Run after editing the SVG:

    python -m tools.build_icon

Kept as a build step with committed outputs rather than rendering at startup:
PyInstaller needs a real ``.ico`` file path at build time, and the packaged app
should not depend on QtSvg being present just to draw its own window icon.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Must be set before Qt initialises. Without it Qt tries to reach a window
#: system it does not need, which is pointless for offscreen rasterising and
#: fails outright over SSH or in CI.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

#: Module-level so the application outlives every render call. A QGuiApplication
#: bound to a local is garbage-collected when that function returns, and Qt then
#: segfaults (0xC0000005) inside the next QPainter.
_APP = None

ASSETS = Path(__file__).resolve().parent.parent / "client" / "gui" / "assets"
SVG = ASSETS / "icon.svg"
PNG = ASSETS / "icon.png"
ICO = ASSETS / "icon.ico"

#: Sizes Windows picks between. 16 and 32 are the taskbar and title bar, where a
#: too-detailed icon turns to mush -- check those two after any art change.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def render_png(size: int) -> bytes:
    """Rasterise the SVG at one size using Qt, which is already a client dep."""
    global _APP

    from PySide6.QtCore import QBuffer, QByteArray, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    if _APP is None:
        _APP = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])

    renderer = QSvgRenderer(str(SVG))
    if not renderer.isValid():
        raise SystemExit(f"Could not parse {SVG}")

    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()

    # The QByteArray must be held in a local: QBuffer only borrows it, so
    # passing a temporary lets Python free it while Qt is still writing --
    # another 0xC0000005, and one that only bites on the second call.
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def main() -> int:
    if not SVG.exists():
        raise SystemExit(f"Missing {SVG}")

    from io import BytesIO

    from PIL import Image

    PNG.write_bytes(render_png(256))
    print(f"wrote {PNG.relative_to(Path.cwd())}")

    # Build the .ico from individually rendered sizes rather than downscaling
    # one big raster: the SVG has size-appropriate weight and Qt's renderer
    # produces a cleaner 16x16 than any resampling filter does.
    frames = [Image.open(BytesIO(render_png(size))) for size in ICO_SIZES]
    frames[-1].save(ICO, format="ICO", sizes=[(s, s) for s in ICO_SIZES], append_images=frames[:-1])
    print(f"wrote {ICO.relative_to(Path.cwd())}  ({', '.join(str(s) for s in ICO_SIZES)})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

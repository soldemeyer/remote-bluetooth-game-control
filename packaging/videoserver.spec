# PyInstaller spec for the standalone video server.
#
#     pyinstaller packaging/videoserver.spec      -> dist/rbgc-video/
#
# Same shape as client.spec: onedir, no UPX. See that file for why.
#
# Two differences worth knowing:
#
#   * console=True. This is an operator tool that is genuinely useful headless
#     (`rbgc-video --headless --server ...` on a machine with no display), and
#     a windowed build would leave that mode with nowhere to print.
#   * No SDL2. The video server reads no gamepads.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

BASE = Path(SPECPATH).parent

block_cipher = None

# FFmpeg ships inside the PyAV wheel; libsodium inside PyNaCl. Both are loaded
# at runtime rather than imported, so static analysis cannot see either.
binaries = collect_dynamic_libs("av") + collect_dynamic_libs("nacl")

hidden_imports = [
    # See client.spec: without these the bundle dies on import with
    # "No module named '_cffi_backend'".
    "cffi",
    "_cffi_backend",
    "nacl",
    "nacl._sodium",
    "nacl.bindings",
] + collect_submodules("av")

excludes = [
    # No gamepad input here.
    "sdl2", "sdl2dll", "PySDL2",
    # No plotting: the video server shows numbers, not graphs.
    "pyqtgraph", "matplotlib", "scipy", "pandas",
    # Qt modules this window never touches.
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras", "PySide6.Qt3DLogic",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQml",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",   # it captures, never plays
    "PySide6.QtSvg", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtCharts", "PySide6.QtDataVisualization",
    # Server-only dependencies. NB: server.sessions and server.rendezvous *are*
    # imported (the media socket reuses them), but they need none of these.
    "aiohttp", "dbus_next", "pyudev",
    "tkinter", "IPython", "pytest",
]

a = Analysis(
    [str(BASE / "videoserver" / "main.py")],
    pathex=[str(BASE)],
    binaries=binaries,
    # Its own assets, for its own icon -- loaded at runtime from this path,
    # so the layout inside the bundle has to match what assets_dir() expects.
    datas=[(str(BASE / "videoserver" / "assets"), "videoserver/assets")],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="rbgc-video",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-compressed binaries trip antivirus far too often
    console=True,       # useful headless; see the header comment
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # A display, not the client's gamepad. Regenerate with
    # `python -m tools.build_icon` after editing the SVG.
    icon=str(BASE / "videoserver" / "assets" / "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="rbgc-video",
)

# PyInstaller spec for the standalone client.
#
# Produces a folder the user can double-click into with no Python installed.
#
#     pyinstaller packaging/client.spec          -> dist/rbgc-client/
#
# Built as onedir rather than onefile deliberately: onefile unpacks to a temp
# directory on every launch, which adds seconds of startup and has a habit of
# tripping antivirus heuristics. A folder starts instantly and is easier to
# zip and hand to someone.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

BASE = Path(SPECPATH).parent

block_cipher = None

# SDL2's shared library ships inside pysdl2-dll and is loaded at runtime by
# ctypes, so PyInstaller's static analysis cannot see it. Same story for
# libsodium, which PyNaCl loads through cffi.
binaries = collect_dynamic_libs("sdl2dll") + collect_dynamic_libs("nacl")

hidden_imports = [
    "sdl2",
    "sdl2.dll",
    "sdl2dll",
    # PyNaCl reaches libsodium through cffi. Both the compiled backend and the
    # generated binding module are loaded dynamically, so PyInstaller's static
    # analysis misses them and the bundle fails at import with
    # "No module named '_cffi_backend'".
    "cffi",
    "_cffi_backend",
    "nacl",
    "nacl._sodium",
    "nacl.bindings",
    "pyqtgraph",
]

# Qt modules we never touch. Excluding them cuts roughly 60 MB off the bundle.
excludes = [
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras", "PySide6.Qt3DLogic",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQml",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtCharts", "PySide6.QtDataVisualization",
    # Server-only dependencies -- the client never imports these.
    "aiohttp", "dbus_next", "pyudev",
    "matplotlib", "scipy", "pandas", "tkinter", "IPython", "pytest",
]

a = Analysis(
    [str(BASE / "client" / "main.py")],
    pathex=[str(BASE)],
    binaries=binaries,
    datas=[],
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
    name="rbgc-client",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-compressed binaries trip antivirus far too often
    console=False,      # GUI app: no console window on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="rbgc-client",
)

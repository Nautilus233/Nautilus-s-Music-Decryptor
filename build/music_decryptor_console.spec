# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one-file, windowed, custom icon.

Build with:
    python -m PyInstaller --noconfirm --clean ^
        --distpath dist --workpath build/work build/music_decryptor.spec
(or just run build/build.ps1)
"""
import os

# SPECPATH (the folder holding this spec) is injected by PyInstaller.
_build_dir = globals().get("SPECPATH") or os.getcwd()
_build_dir = os.path.abspath(_build_dir)
ROOT = os.path.dirname(_build_dir)

ICON = os.path.join(ROOT, "assets", "icon.ico")

# Everything except QtCore/QtGui/QtWidgets is dropped to keep the exe small.
EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtQml", "PySide6.QtQmlModels", "PySide6.QtQmlWorkerScript",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtUiTools",
    "PySide6.QtHelp", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSvg", "PySide6.QtSvgWidgets", "PySide6.QtNetwork",
    "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtBluetooth",
    "PySide6.QtNfc", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtSensors", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech",
    "PySide6.QtSerialPort", "PySide6.QtDBus", "PySide6.QtSerialBus",
    "tkinter", "unittest", "pydoc", "doctest",
    "numpy", "pandas", "matplotlib", "scipy",
]

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[(ICON, "assets")],
    hiddenimports=[
        "mutagen", "mutagen.id3", "mutagen.flac", "mutagen.mp4",
        "mutagen.oggvorbis", "mutagen.ogg", "mutagen.easyid3",
        "mutagen.asf", "mutagen.monkeysaudio", "mutagen.apev2",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
# --- strip ICU DLLs -------------------------------------------------------
# PyInstaller resolves binary dependencies by *name*, and while resolving
# Qt6Core.dll's "icuuc.dll" it picked an unrelated ICU build off PATH
# (Poppler's).  That copy only exports versioned symbols (ucnv_open_78) while
# Qt6Core.dll imports the *unversioned* ucnv_open that only the Windows system
# icuuc.dll provides - so bundling it shadows the system DLL and makes
# "import PySide6.QtCore" fail with ERROR_PROC_NOT_FOUND.  Not bundling ICU is
# also correct: on Windows 10 1703+ the OS ships ICU in System32.
a.binaries = [b for b in a.binaries
              if not os.path.basename(b[0]).lower().startswith("icu")]
# --------------------------------------------------------------------------

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MusicDecryptorConsole",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    icon=ICON,
    version=None,
)
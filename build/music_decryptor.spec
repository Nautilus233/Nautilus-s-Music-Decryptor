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
#: 日语/中文内置表（构建脚本 tools/build_jp_tables.py 生成，运行时不联网）
DATA = os.path.join(ROOT, "mmusic", "data")

# --- 可选转码引擎：PySide6 自带的 FFmpeg 库（LGPL，动态链接） ------------------
# 只打包音频编解码需要的四个库（名字带 FFmpeg 主版本号，用 glob 匹配以免升级后失效）。
# 运行时由 mmusic/transcode.py 从 sys._MEIPASS 或 PySide6 目录加载；缺失则静默回退。
import glob
_MEDIA_DLLS: "list" = []
try:
    import PySide6 as _PS
    _ps_dir = os.path.dirname(_PS.__file__)
    for _pat in ("avcodec-*.dll", "avformat-*.dll", "avutil-*.dll", "swresample-*.dll"):
        _MEDIA_DLLS += [(p, ".") for p in glob.glob(os.path.join(_ps_dir, _pat))]
except Exception:
    _MEDIA_DLLS = []

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
    binaries=_MEDIA_DLLS,
    datas=[(ICON, "assets"), (DATA, "mmusic/data")],
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
# --- 体积裁剪（2026-10-22，依据 = 对 PySide6 目录做 PE 导入表扫描）--------------
# 扫描结论（可用 tools/pe_deps.py 复现）：Qt6Gui → Qt6Core；Qt6Widgets → Qt6Gui；
# 其余 Qt6*.dll **无人静态导入**，而本程序的代码路径里没有 QML/Quick/PDF/OpenGL/
# Network/Svg（spec.EXCLUDES 也早就把这些 Python 模块排除了）。
# 打包出来却有 145 MB(未压缩) 二进制，其中重复与用不到的部分约 60 MB：
#   * avcodec/avutil/swresample 被 Qt hook 放进 PySide6/，我们自己的 spec 又放了一份在根目录
#     —— 只保留**根目录那一份**（mmusic/transcode.py 的 _lib_dir() 先找根目录；avformat
#     本来就只有根目录有，四件套必须在同一目录才认）；
#   * Qt6Qml/Quick/QmlModels/QmlMeta/OpenGL/Pdf/Network/Svg/DBus… 全删（保留 Core/Gui/Widgets）；
#   * opengl32sw.dll（20.6 MB 软件 OpenGL 回退）：没有任何 DLL 静态导入它，Qt 只在**真的用到
#     OpenGL** 时才动态加载 —— 本程序是纯 QWidgets + QSS（光栅绘制），不会走 GL；
#     **用户 2026-10-22 明确决定：保持删除**（接受残余风险；回退=删掉下面那一行判断，+20.6 MB）。
#     佐证：QT_OPENGL=software 下 --check-ui 仍 exit 0；离屏平台插件可加载；批量真转码通过。
#   * 用不到的插件：虚拟键盘、TUIO 触摸、direct2d 平台后端、以及除 qico 外的图像格式插件
#     （UI 只加载 assets/icon.ico）。
# 保留：platforms/qwindows.dll（主平台）、qoffscreen/qminimal（无头自检用）、
#       styles/qmodernwindowsstyle.dll（控件度量，UI 自适应测试依赖它）、imageformats/qico.dll。
_KEEP_QT6 = {"qt6core.dll", "qt6gui.dll", "qt6widgets.dll"}

# 构建期护栏（2026-10-22）：裁剪掉的那些 Qt 模块，**源码里也不许出现**。
# 否则将来有人加了 QML/SVG/网络图形功能，产物会"静默缺件"（运行时才炸）——
# 这里让它在构建时就报错，并给出该怎么办。
_DROPPED_QT_MODULES = ("QtQml", "QtQuick", "QtNetwork", "QtSvg", "QtPdf", "QtOpenGL",
                       "QtMultimedia", "QtWebEngine", "QtCharts", "QtSql", "QtTest")
_src_hits = []
_py_files = (glob.glob(os.path.join(ROOT, "ui", "**", "*.py"), recursive=True)
             + glob.glob(os.path.join(ROOT, "mmusic", "**", "*.py"), recursive=True)
             + [os.path.join(ROOT, "main.py")])
for _py in _py_files:
    with open(_py, "r", encoding="utf-8", errors="ignore") as _fh:
        _text = _fh.read()
    for _mod in _DROPPED_QT_MODULES:
        if "PySide6." + _mod in _text:
            _src_hits.append(os.path.relpath(_py, ROOT) + " → PySide6." + _mod)
if _src_hits:
    raise SystemExit(
        "打包裁剪与源码不一致：下面这些文件用到了已被裁掉的 Qt 模块，"
        "产物会缺件。要么改用 QtCore/QtGui/QtWidgets，要么把该模块从 spec 的 "
        "EXCLUDES/裁剪列表里放出来：\n  - " + "\n  - ".join(_src_hits))
_DROP_PLUGIN_PREFIXES = ("pyside6/plugins/platforminputcontexts/", "pyside6/plugins/generic/",
                         "pyside6/plugins/iconengines/", "pyside6/plugins/platforms/qdirect2d.dll")


def _keep_binary(dest: str) -> bool:
    low = (dest or "").replace("\\", "/").lower()
    base = low.rsplit("/", 1)[-1]
    if base.startswith(("avcodec-", "avformat-", "avutil-", "swresample-")):
        return "/" not in low                      # FFmpeg 四件套只留根目录那一份
    if base.startswith("qt6") and base not in _KEEP_QT6:
        return False
    if base == "opengl32sw.dll":
        return False
    if low.startswith(_DROP_PLUGIN_PREFIXES):
        return False
    if low.startswith("pyside6/plugins/imageformats/") and not base.startswith("qico"):
        return False
    return True


a.binaries = [b for b in a.binaries if _keep_binary(b[0])]
# --------------------------------------------------------------------------

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MusicDecryptor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=ICON,
    version=None,
)
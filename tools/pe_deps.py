"""打包体积审计工具（开发期用，纯标准库，不进产物）。

    python tools/pe_deps.py deps     # 扫 PySide6 目录里 DLL 的静态依赖
    python tools/pe_deps.py bundle   # 列出 dist\\MusicDecryptor.exe 内嵌条目与体积

2026-10-22 用它找到并去掉了约 60 MB（未压缩）冗余：Qt hook 重复带的 FFmpeg 四件套、
Qt 里用不到的模块（Qml/Quick/OpenGL/Pdf/Network/Svg…）与软件 OpenGL 回退库。结论与
取舍记录在 DESIGN.md §2.6.1；裁剪规则在 build/music_decryptor.spec 的 `_keep_binary()`。
"""
from __future__ import annotations

import re
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pe_imports(path: Path) -> "list[str]":
    """返回一个 PE 文件**静态导入**的 DLL 名（够用就好，不做完整 PE 解析）。"""
    b = path.read_bytes()
    pe = struct.unpack_from("<I", b, 0x3C)[0]
    if b[pe:pe + 4] != b"PE\0\0":
        return []
    nsec = struct.unpack_from("<H", b, pe + 6)[0]
    opt_size = struct.unpack_from("<H", b, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", b, opt)[0]
    dd = opt + (96 if magic == 0x10B else 112)
    imp_rva = struct.unpack_from("<I", b, dd + 8)[0]     # DataDirectory[1] = import
    sh = opt + opt_size
    secs = []
    for i in range(nsec):
        o = sh + i * 40
        vsz, va, rawsz, rawptr = struct.unpack_from("<IIII", b, o + 8)
        secs.append((va, max(vsz, rawsz), rawptr, rawsz))

    def off(rva: int):
        for va, span, rawptr, rawsz in secs:
            if va <= rva < va + span and rva - va < rawsz:
                return rawptr + (rva - va)
        return None

    base = off(imp_rva) if imp_rva else None
    if base is None:
        return []
    names = []
    for k in range(512):
        ent = b[base + k * 20: base + k * 20 + 20]
        if len(ent) < 20 or ent == b"\0" * 20:
            break
        name_rva = struct.unpack_from("<I", ent, 12)[0]
        no = off(name_rva) if name_rva else None
        if no is None:
            continue
        end = b.find(b"\0", no, no + 256)
        if end > 0:
            names.append(b[no:end].decode("ascii", "replace"))
    return names


def cmd_deps() -> int:
    import PySide6
    ps = Path(PySide6.__file__).parent
    targets = ["Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll", "Qt6Network.dll", "Qt6Qml.dll",
               "Qt6Quick.dll", "Qt6QmlModels.dll", "Qt6OpenGL.dll", "Qt6Pdf.dll", "Qt6Svg.dll",
               "opengl32sw.dll", "avcodec-63.dll", "avformat-63.dll", "avutil-61.dll"]
    print(f"PySide6: {ps}")
    for name in targets:
        p = ps / name
        if not p.exists():
            continue
        deps = [d for d in pe_imports(p)
                if d.lower().startswith(("qt6", "av", "sw", "opengl"))]
        print(f"{name:24} {p.stat().st_size / 1e6:6.2f} MB  依赖: {', '.join(deps) or '(无)'}")
    print("\n结论要点：Qt6Gui→Core、Qt6Widgets→Gui；其余 Qt6*.dll 无人静态导入；"
          "opengl32sw 无人静态导入（动态回退用）。")
    return 0


def cmd_bundle() -> int:
    exe = ROOT / "dist" / "MusicDecryptor.exe"
    if not exe.is_file():
        print(f"找不到 {exe}（先构建）")
        return 2
    out = subprocess.run([sys.executable, "-m", "PyInstaller.utils.cliutils.archive_viewer",
                          "-l", str(exe)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    rows = []
    for line in out.splitlines():
        m = re.match(r"\s*\d+,\s*\d+,\s*(\d+),\s*\d+,\s*'(\w)',\s*'(.*)'\s*$", line)
        if m:
            rows.append((int(m.group(1)), m.group(3)))
    total = sum(r[0] for r in rows)
    print(f"{exe.name}: {len(rows)} 个条目，未压缩合计 {total / 1e6:.1f} MB")
    for size, name in sorted(rows, reverse=True)[:20]:
        print(f"  {size / 1e6:8.2f} MB  {name}")
    return 0


def main() -> int:
    what = (sys.argv[1] if len(sys.argv) > 1 else "deps").lower()
    if what == "bundle":
        return cmd_bundle()
    return cmd_deps()


if __name__ == "__main__":
    raise SystemExit(main())

"""QQ Music "musicex" container tail (2026+, ``.mgg`` / ``.mflac`` / ``.mmp4``).

新版布局 = 加密音频 + **未加密尾部**（常见 192 字节；B 变体 200 字节）。尾部以
``musicex\0`` 结尾、长度写在“文件末尾往前 16 字节”（u32 LE），内含 UTF-16LE 的
``song_mid`` 与原始文件名 —— **尾部不含密钥**。

ekey 是随账号在线获取的每文件密钥（约 22 小时有效），离线解码必须在本地准备
（例如社区 ``fetch_ekey.py`` 生成的 ``ekeys/<mid>__<filename>.txt``）。本模块只做
两件纯解析的事：读取尾部字段、查找用户自行放置的 ekey 文本；解密与密钥展开仍由
``qmc.py`` 完成。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..constants import QMC_TAG_MUSICEX

__all__ = ["MusicexTail", "parse_tail", "find_ekey_text"]

#: (mid 起点, mid 终点, 文件名起点, 文件名终点, 音频后额外的前缀字节数)
#: A 变体：tail=192，audio = size - tail；B 变体：tail=200，audio = size - tail - 16
_LAYOUTS = (
    (12, 72, 72, 168, 0),
    (28, 88, 88, 184, 16),
)


@dataclass(frozen=True)
class MusicexTail:
    song_mid: str
    filename: str
    audio_size: int
    tail_size: int


def _utf16(raw: bytes) -> str:
    return raw.decode("utf-16le", errors="ignore").rstrip("\0").strip()


def parse_tail(tail: bytes, file_size: int) -> "MusicexTail | None":
    """从文件尾部的原始字节解析 musicex 信息；不是 musicex 返回 None。

    ``tail`` 只需包含文件最后若干 KB（≥16 字节，且覆盖完整尾部）。
    """
    if len(tail) < 16 or not tail.endswith(QMC_TAG_MUSICEX):
        return None
    tail_size = int.from_bytes(tail[-16:-12], "little")
    if tail_size <= 16 or tail_size > 4096 or tail_size > len(tail) or tail_size > file_size:
        return None
    view = tail[-tail_size:]
    for mid_s, mid_e, name_s, name_e, prefix in _LAYOUTS:
        if name_e > tail_size:
            continue
        song_mid = _utf16(view[mid_s:mid_e])
        filename = _utf16(view[name_s:name_e])
        audio_size = file_size - tail_size - prefix
        if song_mid.startswith("00") and "." in filename and audio_size > 0:
            return MusicexTail(song_mid, filename, audio_size, tail_size)
    return None


def find_ekey_text(src_path: "str | Path | None", song_mid: str,
                   filename: str) -> "str | None":
    """在加密文件附近查找用户自行准备的 ekey 文本；没有则返回 None。

    查找顺序：
      1. ``<dir>/ekeys/<song_mid>__<filename>.txt``（社区 fetch_ekey 默认布局）
      2. ``<dir>/ekeys/<song_mid>.txt``
      3. ``<dir>/ekeys/<song_mid>*.txt``（按文件名排序取第一个非空）
      4. ``<song.mgg>.ekey`` 或 ``<song>.ekey``（同名旁挂文件）
    """
    if not src_path:
        return None
    try:
        src = Path(src_path)
        parent = src.parent
    except (TypeError, OSError):
        return None
    candidates: "list[Path]" = []
    ekeys_dir = parent / "ekeys"
    candidates.append(ekeys_dir / f"{song_mid}__{filename}.txt")
    candidates.append(ekeys_dir / f"{song_mid}.txt")
    try:
        candidates.extend(sorted(ekeys_dir.glob(f"{song_mid}*.txt")))
    except OSError:
        pass
    candidates.append(parent / f"{src.name}.ekey")
    candidates.append(parent / f"{src.stem}.ekey")
    seen: "set[str]" = set()
    for cand in candidates:
        marker = str(cand)
        if marker in seen:
            continue
        seen.add(marker)
        try:
            if cand.is_file():
                text = cand.read_text("utf-8", errors="ignore").strip()
                if text:
                    return text
        except OSError:
            continue
    return None

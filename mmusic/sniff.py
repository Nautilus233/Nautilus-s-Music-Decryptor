"""Format identification from head/tail bytes + file extension."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .constants import (AUDIO_MAGICS, M4A_FTYP, NCM_MAGIC, QMC_EXT_MAP,
                        QMC_TAG_MUSICEX, QMC_TAG_QTAG, QMC_TAG_STAG, QMC_V2_EXTS)
from .crypto.musicex import (find_ekey_path, find_ekey_text,
                             parse_tail as parse_musicex_tail)
from .ekeys import VALID_SECONDS

__all__ = ["FormatInfo", "looks_like_audio", "sniff", "sniff_path"]

_PLAIN_EXTS = {"mp3", "flac", "ogg", "m4a", "aac", "wav", "ape", "wma", "dff", "mmp4"}


def _ekey_is_fresh(path: "str | Path") -> bool:
    """本地 ekey 是否仍在有效期内（22 小时）。过期即视同“没有”。"""
    try:
        return (time.time() - Path(path).stat().st_mtime) < VALID_SECONDS
    except OSError:
        return False


def looks_like_audio(head: bytes) -> "str | None":
    """Return an extension if `head` starts with a known audio signature."""
    for magic, ext in AUDIO_MAGICS.items():
        if head.startswith(magic):
            return ext
    if len(head) >= 8 and head[4:8] == M4A_FTYP:
        return "m4a"
    return None


@dataclass
class FormatInfo:
    kind: str            # "ncm" | "qmc" | "plain" | "unknown"
    ext: str             # source extension, lower-case, no dot
    out_ext: str         # expected output extension
    needs_online: bool = False
    note: str = ""
    trailing: bytes = b""

    @property
    def decryptable(self) -> bool:
        return self.kind in ("ncm", "qmc", "plain") and not self.needs_online


def sniff(head: bytes, tail: bytes, ext: str) -> FormatInfo:
    ext = (ext or "").lower().lstrip(".")

    if head.startswith(NCM_MAGIC) or ext == "ncm":
        return FormatInfo("ncm", ext or "ncm", "mp3")

    if ext in QMC_EXT_MAP:
        _family, out_ext = QMC_EXT_MAP[ext]
        if len(tail) >= 8 and tail[-8:] == QMC_TAG_MUSICEX:
            return FormatInfo("qmc", ext, out_ext, needs_online=True,
                              note="QQ音乐新版 musicex 加密：文件内无密钥，"
                                   "需要账号 ekey（放入 ekeys 目录后可离线解码）",
                              trailing=tail[-8:])
        trailing = tail[-4:] if len(tail) >= 4 else tail
        if trailing == QMC_TAG_STAG:
            return FormatInfo("qmc", ext, out_ext, needs_online=True,
                              note="QQ音乐新版加密（STag：文件内无密钥，需联网/客户端密钥）",
                              trailing=trailing)
        return FormatInfo("qmc", ext, out_ext, trailing=trailing)

    if ext in ("kwm", "kwd"):
        from .crypto.kwm import is_kwm
        if is_kwm(head):
            return FormatInfo("kwm", ext, "mp3")
        # 没有 kwm 魔数：少数下载其实没加密（例如裸 AAC），能认出就直接当明文
        plain = looks_like_audio(head)
        if plain:
            return FormatInfo("plain", ext, plain)
        return FormatInfo("unknown", ext, ext)

    audio_ext = looks_like_audio(head)
    if audio_ext:
        return FormatInfo("plain", ext or audio_ext, audio_ext)

    return FormatInfo("unknown", ext, ext or "mp3")


def sniff_path(path: "str | Path") -> FormatInfo:
    p = Path(path)
    size = p.stat().st_size
    with open(p, "rb") as f:
        head = f.read(16)
        if size > 8:
            f.seek(-min(size, 4096), 2)
            tail = f.read()
        else:
            tail = head
    info = sniff(head, tail, p.suffix)
    # musicex 尾部本身无密钥：只有当本地 ekey **仍在有效期**时才放宽为“可离线解码”。
    # 过期（或缺失）一律保持 needs_online=True，交给流水线按需重取 / 转备用链路 ——
    # 否则一份陈旧的 ekey 会让软件误以为能离线解，绕过重取直接产出损坏结果。
    if info.kind == "qmc" and len(tail) >= 8 and tail[-8:] == QMC_TAG_MUSICEX:
        mus = parse_musicex_tail(tail, size)
        if mus is not None:
            ekey_path = find_ekey_path(p, mus.song_mid, mus.filename)
            if ekey_path is not None and _ekey_is_fresh(ekey_path):
                return FormatInfo("qmc", info.ext, info.out_ext, needs_online=False,
                                  note=f"musicex 新版加密（已找到本地 ekey：{mus.song_mid}）",
                                  trailing=tail[-8:])
            if ekey_path is not None:
                return FormatInfo("qmc", info.ext, info.out_ext, needs_online=True,
                                  note=f"musicex 新版加密（本地 ekey 已过期，将自动重新获取："
                                       f"{mus.song_mid}）",
                                  trailing=tail[-8:])
    return info

"""Format identification from head/tail bytes + file extension."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .constants import (AUDIO_MAGICS, M4A_FTYP, NCM_MAGIC, QMC_EXT_MAP,
                        QMC_TAG_QTAG, QMC_TAG_STAG, QMC_V2_EXTS)

__all__ = ["FormatInfo", "looks_like_audio", "sniff", "sniff_path"]

_PLAIN_EXTS = {"mp3", "flac", "ogg", "m4a", "aac", "wav", "ape", "wma", "dff", "mmp4"}


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
        trailing = tail[-4:] if len(tail) >= 4 else tail
        if trailing == QMC_TAG_STAG:
            return FormatInfo("qmc", ext, out_ext, needs_online=True,
                              note="QQ音乐新版加密（STag：文件内无密钥，需联网/客户端密钥）",
                              trailing=trailing)
        return FormatInfo("qmc", ext, out_ext, trailing=trailing)

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
            f.seek(-8, 2)
            tail = f.read(8)
        else:
            tail = head
    return sniff(head, tail, p.suffix)
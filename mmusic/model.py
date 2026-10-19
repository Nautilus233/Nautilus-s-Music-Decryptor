"""Shared result/value types."""
from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["AudioResult", "Track"]


@dataclass
class AudioResult:
    """The output of a decoder: plain audio bytes plus whatever tags we
    could recover without touching the network."""

    audio: bytes
    ext: str
    title: str = ""
    artist: str = ""
    album: str = ""
    cover_url: str = ""
    cover: bytes = b""
    meta: dict = field(default_factory=dict)


@dataclass
class Track:
    """One search hit from a fallback source."""

    source: str
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: int = 0          # seconds, 0 == unknown
    bitrate: int = 0           # kbps, 0 == unknown
    ext: str = "mp3"
    size: int = 0              # bytes, 0 == unknown
    actual_duration: "float | None" = None   # seconds, measured from the audio itself
    url: str = ""              # direct link (may be filled lazily)
    raw: dict = field(default_factory=dict)
    score: float = 0.0
    #: 直链形态上一次就看得出的"疑似短资源"（试听/铃声/提示音）。目前由酷我适配器
    #: 依据 CDN 路径判定；只用于**降权**（少花探测/下载预算），绝不硬性排除。
    short_asset: bool = False
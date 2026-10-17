from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..model import Track

QUALITY_MP3 = "mp3"
QUALITY_FLAC = "flac"
QUALITY_WAV = "wav"
VALID_QUALITIES = (QUALITY_MP3, QUALITY_FLAC, QUALITY_WAV)


class SearchSource(ABC):
    """A searchable/downloadable source of audio."""

    name = "base"
    label = "base"
    offline = False          # True for sources that never touch the network

    #: True 表示"用户显式指定"，可跳过歌名/歌手门槛（仍走下载校验）
    trusted_links = False

    def __init__(self, http, settings: Optional[dict] = None, spec=None) -> None:
        self.http = http
        self.settings = settings or {}
        self.spec = spec

    @abstractmethod
    def search(self, query: str, *, quality: str = QUALITY_MP3, limit: int = 10) -> list[Track]:
        """Return candidate tracks for `query`, best-effort, never raising
        for a simple "no results" (return an empty list instead)."""

    def resolve(self, track: Track, *, quality: str = QUALITY_MP3) -> Track:
        """Fill in ``track.url`` (and, if known, ``ext``/``bitrate``/``size``)."""
        return track
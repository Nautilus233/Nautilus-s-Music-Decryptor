"""Manually supplied links / local paths ("人工补链").

The guaranteed escape hatch: when every source fails, the user pastes a link
(or a path to their own file) and it is used as-is.  Because the user chose it
explicitly, the title/artist gates are **skipped** for these entries - but the
download is still validated (real Content-Type + file header + bitrate).
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ..match import similarity
from ..model import Track
from .base import SearchSource

__all__ = ["ManualLinkSource"]


class ManualLinkSource(SearchSource):
    name = "manual"
    label = "手动补链"
    #: 用户显式指定 -> 跳过"歌名/歌手"门槛（其余校验照旧）
    trusted_links = True

    def _entries(self) -> list[str]:
        raw = self.settings.get("manual_links") or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(x).strip() for x in raw if str(x).strip()]

    def search(self, query: str, *, quality: str = "mp3", limit: int = 10) -> list[Track]:
        entries = self._entries()
        if not entries:
            return []
        tracks: list[Track] = []
        for entry in entries[:20]:
            name = Path(entry.split("?", 1)[0]).name
            stem = Path(name).stem or name
            # 与当前歌曲最像的先排前面；不相似也保留（用户既然给了就用）
            score = similarity(query, stem)
            tracks.append(Track(source=self.name, title=stem or query, artist="",
                                ext=(Path(name).suffix.lstrip(".") or "mp3").lower(),
                                url=entry, raw={"manual": entry, "manual_score": score}))
        tracks.sort(key=lambda t: t.raw.get("manual_score", 0.0), reverse=True)
        return tracks[:limit]

    def resolve(self, track: Track, *, quality: str = "mp3") -> Track:
        entry = (track.raw or {}).get("manual") or track.url
        if not entry:
            from ..errors import NetworkError
            raise NetworkError("补链条目为空")
        track.url = entry
        return track
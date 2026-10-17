"""Internet Archive (archive.org) - public-domain / CC audio.

Why it is here: it is the one large, *open* catalogue that needs no key, no
login and no payment, and its API explicitly allows this use.  It is a good
fit for the coverage gap - classical, live, old and otherwise "cold" tracks
that the Chinese streaming platforms simply do not carry.

Two calls per result: ``advancedsearch.php`` for the identifier, then
``metadata/{id}`` for the concrete audio file.  The metadata also carries a
real ``length``, which feeds the existing duration verification.
"""
from __future__ import annotations

from urllib.parse import quote

from ..model import Track
from .base import SearchSource

__all__ = ["InternetArchiveSource"]

SEARCH = ("https://archive.org/advancedsearch.php?q={q}"
          "&fl%5B%5D=identifier&fl%5B%5D=title&fl%5B%5D=creator&fl%5B%5D=year"
          "&rows={n}&page=1&output=json")
META = "https://archive.org/metadata/{ident}"
_PREFERRED = ("flac", "vbr mp3", "mp3", "ogg vorbis", "ogg")


def _seconds(value) -> int:
    """archive.org reports `length` as seconds ("451.54") or "m:ss"."""
    try:
        text = str(value or "").strip()
        if not text:
            return 0
        if ":" in text:
            parts = [int(p) for p in text.split(":")[:3]]
            while len(parts) < 3:
                parts.insert(0, 0)
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return int(float(text))
    except (TypeError, ValueError):
        return 0


class InternetArchiveSource(SearchSource):
    name = "internet_archive"
    label = "互联网档案馆(PD/CC)"

    def search(self, query: str, *, quality: str = "mp3", limit: int = 10) -> list[Track]:
        url = SEARCH.format(q=quote(f'({query}) AND mediatype:audio'), n=max(1, min(limit, 20)))
        data = self.http.get_json(url)
        docs = ((data or {}).get("response") or {}).get("docs") or []
        tracks: list[Track] = []
        for doc in docs:
            ident = str(doc.get("identifier") or "").strip()
            if not ident:
                continue
            creator = doc.get("creator")
            if isinstance(creator, list):
                creator = " / ".join(str(c) for c in creator)
            tracks.append(Track(source=self.name, title=str(doc.get("title") or ident),
                                artist=str(creator or ""), ext="mp3",
                                raw={"identifier": ident}))
        return tracks

    def resolve(self, track: Track, *, quality: str = "mp3") -> Track:
        ident = (track.raw or {}).get("identifier") or ""
        if not ident:
            from ..errors import NetworkError
            raise NetworkError("该结果缺少 archive.org 标识")
        meta = self.http.get_json(META.format(ident=quote(ident)))
        files = [f for f in ((meta or {}).get("files") or []) if isinstance(f, dict)]
        best, best_rank = None, 99
        for item in files:
            name = str(item.get("name") or "")
            low = name.lower()
            if not low.endswith((".mp3", ".flac", ".ogg", ".m4a")):
                continue
            if low.endswith(("_sample.mp3", "_sample.ogg")) or "/__ia_thumb" in low:
                continue
            fmt = str(item.get("format") or "").lower()
            rank = next((i for i, tag in enumerate(_PREFERRED) if tag in fmt), 50)
            if low.endswith(".flac"):
                rank = 0
            elif low.endswith(".mp3"):
                rank = min(rank, 2)
            if rank < best_rank:
                best, best_rank = item, rank
        if best is None:
            from ..errors import NetworkError
            raise NetworkError("archive.org 该条目没有可用的音频文件", detail=ident)
        name = str(best.get("name"))
        track.url = f"https://archive.org/download/{quote(ident)}/{quote(name)}"
        track.ext = name.rsplit(".", 1)[-1].lower()
        track.bitrate = int(best.get("bitrate") or 0) if str(best.get("bitrate", "")).isdigit() else 0
        track.size = int(best.get("size") or 0) if str(best.get("size", "")).isdigit() else 0
        seconds = _seconds(best.get("length"))
        if seconds:
            track.duration = track.duration or seconds
            track.actual_duration = float(seconds)      # 官方元数据里的真实时长
        return track
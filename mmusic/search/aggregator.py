"""Aggregator source (MyFreeMP3 / NetEase-proxy style).

Talks to a configurable ``api.php``-style endpoint (default: the public
``music-api.gdstudio.xyz`` gateway, which proxies several platforms and
returns direct CDN links).  This is the "MyFreeMP3" role in the chain:
one endpoint, many back-ends.
"""
from __future__ import annotations

import urllib.parse

from ..errors import NetworkError
from ..model import Track
from .base import SearchSource

DEFAULT_BASE = "https://music-api.gdstudio.xyz/api.php"
#: 实测（2026-10-20）该网关比主流源慢 2~3 倍（检索中位 1.5s、抖动到 6s），
#: 而它只是**备选**：单次请求超时收到 8s（与探测一致），别让它拖住整链。
TIMEOUT = 8.0
#: 音质档位（实测语义）：999/740 都返回**同一个 FLAC**（740 是 999 的别名），
#: 320/192/128 返回对应码率的真实 mp3。因此降级链去掉冗余档：
#: 无损目标 999→320→128（最多 3 次，原为 5 次）；mp3 目标 320→128（最多 2 次，原为 3 次）。
BR_CHAIN_LOSSLESS = (999, 320, 128)
BR_CHAIN_LOSSY = (320, 128)


class AggregatorSource(SearchSource):
    name = "aggregator"
    label = "全网聚合(MyFreeMP3)"

    def __init__(self, http, settings=None, spec=None) -> None:
        super().__init__(http, settings, spec)
        params = dict(getattr(spec, "params", None) or {})
        self.base = (params.get("base") or self.settings.get("aggregator_base")
                     or DEFAULT_BASE).strip()
        self.provider = (params.get("provider")
                         or self.settings.get("aggregator_provider") or "netease").strip()

    def search(self, query: str, *, quality: str = "mp3", limit: int = 10) -> list[Track]:
        url = (f"{self.base}?types=search&source={urllib.parse.quote(self.provider)}"
               f"&name={urllib.parse.quote(query)}&count={max(1, limit)}&pages=1")
        data = self.http.get_json(url, timeout=TIMEOUT)
        if not isinstance(data, list):
            return []
        tracks = []
        for it in data:
            artists = it.get("artist") or []
            if isinstance(artists, list):
                artist = " / ".join(str(a) for a in artists)
            else:
                artist = str(artists)
            tracks.append(Track(
                source=self.name,
                title=str(it.get("name", "")),
                artist=artist,
                album=str(it.get("album", "")),
                ext="mp3",
                raw=it,
            ))
        return tracks

    def resolve(self, track: Track, *, quality: str = "mp3") -> Track:
        ident = str(track.raw.get("id", ""))
        if not ident:
            raise NetworkError("聚合条目缺少 id")
        for br in (BR_CHAIN_LOSSLESS if quality in ("flac", "wav") else BR_CHAIN_LOSSY):
            url = (f"{self.base}?types=url&source={urllib.parse.quote(self.provider)}"
                   f"&id={urllib.parse.quote(ident)}&br={br}")
            try:
                data = self.http.get_json(url, timeout=TIMEOUT)
            except NetworkError:
                continue
            if isinstance(data, dict) and data.get("url"):
                track.url = data["url"]
                track.bitrate = int(data.get("br") or br)
                track.size = int(data.get("size") or 0)
                track.ext = "flac" if br >= 740 else "mp3"
                return track
        raise NetworkError("聚合接口无可用直链", detail=track.title)

    def fetch_pic(self, pic_id: str, size: int = 500) -> str:
        url = (f"{self.base}?types=pic&source={urllib.parse.quote(self.provider)}"
               f"&id={urllib.parse.quote(pic_id)}&size={size}")
        try:
            data = self.http.get_json(url, timeout=TIMEOUT)
        except NetworkError:
            return ""
        return (data or {}).get("url", "") if isinstance(data, dict) else ""

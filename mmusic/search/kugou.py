"""Kugou (酷狗音乐) — search + direct-link resolve.

Search works unauthenticated.  The ``m.kugou.com`` playInfo endpoint
returns a direct URL for freely-available tracks and an empty ``url`` for
paid/region-locked ones, which the pipeline simply skips.
"""
from __future__ import annotations

import urllib.parse

from ..errors import NetworkError
from ..model import Track
from .base import SearchSource


class KugouSource(SearchSource):
    name = "kugou"
    label = "酷狗音乐"

    SEARCH = ("https://songsearch.kugou.com/song_search_v2?keyword={q}&page=1&pagesize={n}"
              "&platform=WebFilter&userid=-1&clientver=2000&iscorrection=1&privilege_filter=0")
    INFO = "https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash={h}"

    def search(self, query: str, *, quality: str = "mp3", limit: int = 10) -> list[Track]:
        url = self.SEARCH.format(q=urllib.parse.quote(query), n=max(1, limit))
        data = self.http.get_json(url, headers={"Referer": "https://www.kugou.com/"})
        lists = (((data or {}).get("data") or {}).get("lists")) or []
        tracks = []
        for it in lists:
            tracks.append(Track(
                source=self.name,
                title=it.get("SongName", ""),
                artist=(it.get("SingerName", "") or "").replace("、", " / "),
                album=it.get("AlbumName", ""),
                duration=int(it.get("Duration") or 0),
                bitrate=320 if it.get("HQFileHash") else 128,
                ext="flac" if it.get("SQFileHash") else "mp3",
                raw=it,
            ))
        return tracks

    def resolve(self, track: Track, *, quality: str = "mp3") -> Track:
        it = track.raw
        if quality == "flac":
            hashes = [it.get("SQFileHash"), it.get("HQFileHash"), it.get("FileHash")]
        else:
            hashes = [it.get("HQFileHash"), it.get("FileHash"), it.get("SQFileHash")]
        last = ""
        for h in hashes:
            if not h:
                continue
            try:
                data = self.http.get_json(
                    self.INFO.format(h=h),
                    headers={"Referer": "https://m.kugou.com/"})
            except NetworkError as exc:
                last = str(exc)
                continue
            url = (data or {}).get("url")
            if url:
                track.url = url
                bitrate = int(data.get("bitRate") or 0)
                if bitrate:
                    track.bitrate = bitrate
                ext = (data.get("extName") or "").lstrip(".").lower()
                if ext:
                    track.ext = "m4a" if ext == "m4a" else ext
                track.size = int(data.get("fileSize") or 0)
                return track
        raise NetworkError("酷狗该曲目无免费直链", detail=last or track.title)
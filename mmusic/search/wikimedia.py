"""Wikimedia Commons - freely licensed audio (CC / public domain).

Open MediaWiki API, no key.  Mostly useful for classical / PD recordings and
for tracks the streaming platforms do not carry at all.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from ..model import Track
from .base import SearchSource

__all__ = ["WikimediaCommonsSource"]

API = ("https://commons.wikimedia.org/w/api.php?action=query&format=json"
       "&generator=search&gsrsearch={q}&gsrlimit={n}&gsrnamespace=6"
       "&prop=imageinfo&iiprop=url|mime|size|extmetadata")
_TAG = re.compile(r"<[^>]+>")


def _plain(value) -> str:
    return _TAG.sub("", str(value or "")).strip()


class WikimediaCommonsSource(SearchSource):
    name = "wikimedia"
    label = "维基共享(CC/PD)"

    def search(self, query: str, *, quality: str = "mp3", limit: int = 10) -> list[Track]:
        url = API.format(q=quote(f"filetype:audio {query}"), n=max(1, min(limit, 20)))
        data = self.http.get_json(url, headers={"User-Agent": "MusicDecryptor/1.0 (personal use)"})
        pages = ((data or {}).get("query") or {}).get("pages") or {}
        tracks: list[Track] = []
        for page in pages.values():
            info = (page.get("imageinfo") or [{}])[0]
            link = str(info.get("url") or "")
            if not link:
                continue
            title = str(page.get("title") or "").removeprefix("File:")
            title = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", title)
            meta = info.get("extmetadata") or {}
            artist = _plain((meta.get("Artist") or {}).get("value"))
            tracks.append(Track(source=self.name, title=title, artist=artist,
                                ext=(link.rsplit(".", 1)[-1].lower() if "." in link else "ogg"),
                                size=int(info.get("size") or 0), url=link,
                                raw={"page": link}))
        return tracks

    def resolve(self, track: Track, *, quality: str = "mp3") -> Track:
        url = (track.raw or {}).get("page") or track.url
        if not url:
            from ..errors import NetworkError
            raise NetworkError("该结果缺少文件地址")
        track.url = url
        return track
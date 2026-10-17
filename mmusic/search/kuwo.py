"""Kuwo (酷我音乐) — search + direct-link resolve.

Verified live: search returns a pseudo-JSON blob (single quotes); the
resolve endpoint ``antiserver.kuwo.cn/anti.s`` returns a real CDN mp3/flac
URL.  This is the most reliable of the built-in sources.
"""
from __future__ import annotations

import re
import urllib.parse

from ..errors import NetworkError
from ..model import Track
from .base import SearchSource

_FIELD = re.compile(r"'([A-Za-z_0-9]+)':'(.*?)'(?=,\s*'|\s*\})", re.S)
_UNESC = re.compile(r"\\+u([0-9a-fA-F]{4})")


def _unescape(value: str) -> str:
    return _UNESC.sub(lambda m: chr(int(m.group(1), 16)), value)


def _split_items(text: str) -> list[str]:
    """Return the top-level ``{...}`` objects of the ``abslist`` array.

    Kuwo items contain *nested* objects, so a naive ``\\{[^{}]*\\}`` grabs the
    wrong level; walk the text with a depth counter instead, skipping
    anything inside single-quoted strings."""
    start = text.find("abslist")
    if start < 0:
        return []
    i = text.find("[", start)
    if i < 0:
        return []
    items: list[str] = []
    depth = 0
    begin = -1
    in_str = False
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "'":
                in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "{":
            if depth == 0:
                begin = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and begin >= 0:
                items.append(text[begin:i + 1])
                begin = -1
            elif depth < 0:
                break
        elif ch == "]" and depth == 0:
            break
        i += 1
    return items


def _parse_items(text: str) -> list[dict]:
    out = []
    for chunk in _split_items(text):
        item = {m.group(1): _unescape(m.group(2)) for m in _FIELD.finditer(chunk)}
        if item.get("MUSICRID"):
            out.append(item)
    return out


def _best_bitrate(minfo: str, fmt: str) -> int:
    best = 0
    for entry in (minfo or "").split(";"):
        m = re.search(r"bitrate:(\d+),format:(\w+)", entry)
        if m and m.group(2).lower() == fmt:
            best = max(best, int(m.group(1)))
    return best


class KuwoSource(SearchSource):
    name = "kuwo"
    label = "酷我音乐"

    SEARCH = ("http://search.kuwo.cn/r.s?all={q}&ft=music&itemset=web_2013&client=kt"
              "&pn=0&rn={n}&rformat=json&encoding=utf8")
    RESOLVE = "http://antiserver.kuwo.cn/anti.s?type=convert_url3&rid={rid}&format={fmt}&response=url"

    def search(self, query: str, *, quality: str = "mp3", limit: int = 10) -> list[Track]:
        url = self.SEARCH.format(q=urllib.parse.quote(query), n=max(1, limit))
        status, body, _ = self.http.request("GET", url)
        if status >= 400:
            raise NetworkError(f"酷我搜索 HTTP {status}", detail=url)
        tracks = []
        for it in _parse_items(body.decode("utf-8", "replace")):
            tracks.append(Track(
                source=self.name,
                title=it.get("NAME", "").replace("&nbsp;", " "),
                artist=it.get("ARTIST", "").replace("&nbsp;", " "),
                album=it.get("ALBUM", "").replace("&nbsp;", " "),
                duration=int(it.get("DURATION") or 0),
                bitrate=_best_bitrate(it.get("MINFO", ""), "mp3"),
                ext="mp3",
                raw=it,
            ))
        return tracks

    def resolve(self, track: Track, *, quality: str = "mp3") -> Track:
        rid = track.raw.get("MUSICRID") or ""
        if not rid:
            raise NetworkError("酷我条目缺少 MUSICRID")
        fmt = "flac" if quality == "flac" else "mp3"
        data = self.http.get_json(self.RESOLVE.format(rid=urllib.parse.quote(rid), fmt=fmt))
        if not (isinstance(data, dict) and data.get("url")):
            if fmt == "flac":
                return self.resolve(track, quality="mp3")
            raise NetworkError("酷我无可用直链", detail=rid)
        track.url = data["url"]
        track.ext = fmt
        if fmt == "flac":
            track.bitrate = track.bitrate or 0
        return track
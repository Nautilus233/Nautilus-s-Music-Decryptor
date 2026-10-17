"""Generic declarative ``html_scrape`` source.

Driven entirely by a :class:`~mmusic.search.pack.SourceSpec`:

    search  : GET params.search_url (with {query}/{limit}) -> candidate pages
    resolve : PageResolver on the chosen page -> media URL

No per-site code: a new html-based site is a pack entry, not a release.
"""
from __future__ import annotations

from urllib.parse import quote

from ..model import Track
from .base import SearchSource
from .sniffer import PageResolver, extract_page_links, login_wall_reason

__all__ = ["HtmlScrapeSource"]


class HtmlScrapeSource(SearchSource):
    def __init__(self, http, settings, spec) -> None:
        super().__init__(http, settings)
        self.spec = spec
        self.name = spec.id
        self.label = spec.label
        self.offline = spec.offline
        self._params = dict(spec.params or {})
        self._link_re = self._params.get("link_regex") or None
        self._referer = spec.home or ""
        self._resolver = PageResolver(http, referer=self._referer) if http is not None else None

    # ------------------------------------------------------------------ #
    def _url(self, template: str, query: str, limit: int) -> str:
        try:
            return template.format(query=quote(query), limit=int(limit))
        except (KeyError, ValueError):
            return template.replace("{query}", quote(query))

    def search(self, query: str, *, quality: str = "mp3", limit: int = 10) -> list[Track]:
        if self.http is None:
            return []
        search_url = self._url(self._params.get("search_url", ""), query, limit)
        if not search_url:
            return []
        html = self.http.get_text(search_url, headers={"Referer": self._referer}
                                  if self._referer else {})
        reason = login_wall_reason(html)
        if reason:
            from ..errors import NetworkError
            raise NetworkError(f"检索页需要{reason}，按规则放弃（不做绕过）", detail=search_url)
        tracks: list[Track] = []
        for page_url, text in extract_page_links(html, search_url, self._link_re):
            tracks.append(Track(source=self.name, title=text, artist="", ext="mp3",
                                raw={"page": page_url}))
            if len(tracks) >= max(1, limit):
                break
        return tracks

    def resolve(self, track: Track, *, quality: str = "mp3") -> Track:
        page = (track.raw or {}).get("page") or track.raw.get("url") or ""
        if not page:
            from ..errors import NetworkError
            raise NetworkError("该结果没有详情页地址")
        if self._resolver is None:
            from ..errors import NetworkError
            raise NetworkError("缺少 HTTP 客户端，无法解析详情页")
        url = self._resolver.resolve(page)
        track.url = url
        from .sniffer import classify_url
        track.ext = classify_url(url) or "mp3"
        return track
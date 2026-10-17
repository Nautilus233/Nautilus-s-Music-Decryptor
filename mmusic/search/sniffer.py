"""Page-level media extraction ("模拟嗅探") - pure standard library.

What this is **not**: a headless browser, and never a way around a paywall.
It only reads publicly served HTML/JSON and pulls out media URLs in a strict
priority order:

    JSON-LD (AudioObject.contentUrl / audio)  ->  og:audio / twitter:player:stream
      ->  <audio>/<source>/<video> markup      ->  media-extension regex

When a page shows a login form / captcha / members-only wall it **gives up
with a reason** instead of trying to defeat it.  Final acceptance is still
decided by :func:`mmusic.downloader.validate` (real Content-Type and file
header), so a wrong guess from here costs nothing but a failed candidate.

Uses only ``html.parser``, ``json``, ``re`` and ``urllib.parse``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse

from ..errors import NetworkError

__all__ = ["MediaHit", "EXTRACT_PRIORITY", "MEDIA_EXTS", "classify_url",
           "extract_media_urls", "extract_page_links", "login_wall_reason",
           "PageResolver"]

MEDIA_EXTS = ("mp3", "flac", "m4a", "aac", "ogg", "opus", "wav", "ape", "wma",
              "m4s", "mp4")
EXTRACT_PRIORITY = ("json-ld", "og:audio", "twitter:player:stream", "media-tag", "regex")

_EXT_RE = re.compile(r"\.(" + "|".join(MEDIA_EXTS) + r")(?=[?#/]|$)", re.IGNORECASE)
_BARE_URL_RE = re.compile(r"https?://[^\s\"'<>()\\,\]]+", re.IGNORECASE)
_MEDIA_ATTRS = ("src", "data-src", "data-url", "data-file", "href")
_LD_KEYS = ("contenturl", "embedurl", "audio", "associatedmedia")
_WALL_CN = (("需要登录", ("请先登录", "登录后", "需要登录", "登陆后")),
            ("人机验证", ("人机验证", "安全验证", "验证码", "滑动验证")),
            ("需要会员/付费", ("开通会员", "仅限会员", "升级vip", "付费", "购买后")))
_WALL_EN = (("login", ("sign in to", "please log in", "login required", "log in to continue")),
            ("captcha", ("captcha", "are you human")),
            ("members only", ("members only", "premium only", "subscribe to continue")))


@dataclass
class MediaHit:
    url: str
    kind: str          # one of EXTRACT_PRIORITY
    rank: int          # lower == higher confidence


def classify_url(url: str) -> "str | None":
    """Media extension of `url`, if it clearly is a media link."""
    if not url:
        return None
    path = urlparse(url).path or url
    match = _EXT_RE.search(path)
    return match.group(1).lower() if match else None


def _absolute(base: str, url: str) -> str:
    url = (url or "").strip()
    if not url or url.startswith("data:"):
        return ""
    if url.startswith("//"):
        scheme = urlparse(base).scheme or "https"
        return f"{scheme}:{url}"
    return urljoin(base, url) if base else url


class _PageParser(HTMLParser):
    """Collect the handful of things that can carry a media URL."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: "dict[str, str]" = {}
        self.ldjson: list[str] = []
        self.media: list[str] = []
        self.links: "list[tuple[str, str]]" = []
        self.has_password = False
        self.text_parts: list[str] = []
        self._ld = False
        self._buf: list[str] = []
        self._anchor: "str | None" = None
        self._anchor_text: list[str] = []

    # -- tags ---------------------------------------------------------- #
    def handle_starttag(self, tag: str, attrs) -> None:
        data = {k.lower(): (v or "") for k, v in attrs if k}
        if tag == "meta":
            key = (data.get("property") or data.get("name") or "").lower()
            if key and data.get("content"):
                self.meta.setdefault(key, data["content"])
        elif tag == "script":
            if (data.get("type") or "").lower() == "application/ld+json":
                self._ld, self._buf = True, []
        elif tag in ("audio", "source", "video", "embed"):
            for attr in _MEDIA_ATTRS:
                if data.get(attr):
                    self.media.append(data[attr])
                    break
        elif tag == "input" and (data.get("type") or "").lower() == "password":
            self.has_password = True
        elif tag == "a" and data.get("href"):
            self._anchor = data["href"]
            self._anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._ld:
            self.ldjson.append("".join(self._buf))
            self._ld = False
        elif tag == "a" and self._anchor is not None:
            self.links.append((self._anchor, "".join(self._anchor_text).strip()))
            self._anchor = None

    def handle_data(self, data: str) -> None:
        if self._ld:
            self._buf.append(data)
        elif self._anchor is not None:
            self._anchor_text.append(data)
        elif data.strip():
            self.text_parts.append(data)


def _ld_urls(raw: str) -> list[str]:
    """Media URLs found inside one JSON-LD block."""
    text = raw.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001 - JSON-LD in the wild is often sloppy
        return []
    found: list[str] = []

    def walk(node, *, authoritative: bool) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                kl = str(key).lower()
                if kl in _LD_KEYS and isinstance(value, str):
                    url = value.strip()
                    if url and (authoritative or classify_url(url) or kl == "contenturl"):
                        found.append(url)
                elif kl in ("audio", "associatedmedia") and isinstance(value, dict):
                    walk(value, authoritative=True)
                elif kl == "url" and isinstance(value, str) and classify_url(value):
                    found.append(value.strip())
                else:
                    walk(value, authoritative=authoritative)
        elif isinstance(node, list):
            for item in node:
                walk(item, authoritative=authoritative)

    walk(data, authoritative=False)
    return found


def extract_media_urls(html: str, base_url: str = "") -> list[MediaHit]:
    """Ranked media URLs from one page. Higher-confidence sources come first."""
    if not html:
        return []
    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup must not raise
        pass

    ordered: "list[tuple[str, str, str]]" = []          # (kind, raw_url, rank_hint)
    for block in parser.ldjson:
        for url in _ld_urls(block):
            ordered.append(("json-ld", url, "0"))
    if parser.meta.get("og:audio"):
        ordered.append(("og:audio", parser.meta["og:audio"], "1"))
    if parser.meta.get("twitter:player:stream"):
        ordered.append(("twitter:player:stream", parser.meta["twitter:player:stream"], "1"))
    ordered += [("media-tag", url, "2") for url in parser.media]
    for match in _BARE_URL_RE.finditer(html):
        if classify_url(match.group(0)):
            ordered.append(("regex", match.group(0), "3"))

    hits: list[MediaHit] = []
    seen: set[str] = set()
    for kind, raw, hint in ordered:
        url = _absolute(base_url, raw)
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        hits.append(MediaHit(url=url, kind=kind, rank=EXTRACT_PRIORITY.index(kind)))
    hits.sort(key=lambda h: h.rank)
    return hits


def extract_page_links(html: str, base_url: str = "",
                       pattern: "str | None" = None) -> "list[tuple[str, str]]":
    """`(absolute_url, anchor_text)` for candidate detail pages.

    `pattern` is an optional regex; when given, only matching hrefs are kept
    (this is how a declarative pack narrows a search page to real results).
    """
    parser = _PageParser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:  # noqa: BLE001
        pass
    rx = None
    if pattern:
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = None
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, text in parser.links:
        url = _absolute(base_url, href)
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        if rx is not None and not (rx.search(url) or rx.search(href)):
            continue
        seen.add(url)
        out.append((url, text))
    return out


def login_wall_reason(html: str) -> str:
    """'' when the page looks usable, else a short reason (never a bypass)."""
    if not html:
        return ""
    parser = _PageParser()
    try:
        parser.feed(html[:400000])
        parser.close()
    except Exception:  # noqa: BLE001
        pass
    if extract_media_urls(html, ""):
        return ""                       # it already exposes media - not a wall
    if parser.has_password:
        return "需要登录"
    text = " ".join(parser.text_parts).lower()
    for label, keys in _WALL_CN:
        if any(k in text for k in keys):
            return label
    for label, keys in _WALL_EN:
        if any(k in text for k in keys):
            return label
    return ""


class PageResolver:
    """Detail page -> media URL, using the extractor above."""

    def __init__(self, http, *, referer: str = "") -> None:
        self.http = http
        self.referer = referer

    def _headers(self) -> dict:
        return {"Referer": self.referer} if self.referer else {}

    def resolve_all(self, page_url: str) -> list[MediaHit]:
        html = self.http.get_text(page_url, headers=self._headers())
        return extract_media_urls(html, page_url)

    def resolve(self, page_url: str) -> str:
        """Best media URL, or a readable error (login walls are *not* bypassed)."""
        hits = self.resolve_all(page_url)
        if hits:
            return hits[0].url
        reason = login_wall_reason(self.http.get_text(page_url, headers=self._headers()))
        if reason:
            raise NetworkError(f"页面需要{reason}，按规则放弃（不做绕过）", detail=page_url)
        raise NetworkError("页面内未找到可用的媒体直链", detail=page_url)
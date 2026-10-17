"""Query expansion (P3): the same song, written several ways.

A single keyword string misses a lot of cold/awkward titles, so we generate a
small, ordered list of variants and let the caller retry the *next* variant
only when the current one produced nothing usable.  Every variant carries the
reason it exists, so the log can say which phrasing actually hit.

Pure standard library; reuses the existing normalisation in ``mmusic.match``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..match import clean_text, normalize_for_version

__all__ = ["QueryVariant", "expand"]

#: hard cap on variants actually attempted per source (cost control)
MAX_PER_SOURCE = 2
#: hard cap on the variant list itself
MAX_VARIANTS = 5


@dataclass(frozen=True)
class QueryVariant:
    text: str
    reason: str


def expand(query: str, readings: "Sequence[tuple[str, str]]" = (), *,
           limit: int = MAX_VARIANTS) -> list[QueryVariant]:
    """Ordered, de-duplicated search phrasings for one song.

    Order = cheapest/least surprising first:

    1. ``原样``      the filename as written
    2. ``去噪``      brackets/quality tags removed
    3. ``歌名 歌手`` the explicit reading (helps when the filename order is odd)
    4. ``仅歌名``    title alone (finds uploads whose artist field is empty)
    5. ``仅歌手``    artist alone (last resort: the upload's title differs)
    """
    variants: list[QueryVariant] = []
    seen: set[str] = set()

    def add(text: str, reason: str) -> None:
        text = (text or "").strip()
        key = normalize_for_version(text)
        if not text or not key or key in seen:
            return
        seen.add(key)
        variants.append(QueryVariant(text=text, reason=reason))

    add(query, "原样")
    clean = clean_text(query)
    add(clean, "去噪")
    titles = [t for t, _a in readings if t and t.strip()]
    artists = [a for _t, a in readings if a and a.strip()]
    if titles and artists:
        add(f"{titles[0]} {artists[0]}", "歌名 歌手")
    for title in titles:
        add(title, "仅歌名")
    for artist in artists:
        add(artist, "仅歌手")
    return variants[: max(1, limit)]
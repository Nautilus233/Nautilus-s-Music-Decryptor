"""Bounded, offline search hints from existing song records; never acceptance.

The caller may append these strings to a failed-search message. A hint must
not change the query, relax a download gate or trigger another request.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from itertools import islice
from typing import Iterable, Sequence

from . import jp, match
from .model import Track

__all__ = ["query_suggestions"]

_JUNK = re.compile(
    r"<[^>]*>|https?://|www\.|&(?:lt|gt|#\d+|#x[0-9a-f]+);"
    r"|(?:^|[：:])\s*(?:搜索结果|搜索词|查询词|歌词|歌詞|lyrics|query|search results)"
    r"|(?:^|\s)(?:作词|作曲|編曲|作詞)\s*[:：]", re.I)
_ARTIST_SEP = re.compile(r"[、,;，；/\\&＋+|]|\s(?:feat\.?|ft\.?|with)\s", re.I)


def _text(value, maximum=120):
    if not isinstance(value, str) or len(value) > maximum:
        return ""
    if any(unicodedata.category(ch).startswith("C") for ch in value) or _JUNK.search(value):
        return ""
    return " ".join(value.split())


def _key(value):
    # Local written-form folding only: no reading dictionary/search expansion.
    return jp.title_alias_key(value)


def _known_artist(value):
    return (bool(_key(value))
            and match.normalize_for_version(value) not in match.ARTIST_UNKNOWN)


def _artist_identity(wanted, track):
    """Do not mistake artist_match_score's 'unknown = 1' for evidence."""
    if not _known_artist(track.artist) or not _known_artist(wanted):
        return 0.0
    score = match.artist_match_score([("", wanted)], track)
    if score >= .999:
        return 1.0
    # Collaborations are accepted only via an actual shared performer token.
    left = {_key(p) for p in _ARTIST_SEP.split(wanted) if _known_artist(p)}
    right = {_key(p) for p in _ARTIST_SEP.split(track.artist) if _known_artist(p)}
    if score >= .85 and left.intersection(right):
        return .85
    return 0.0


def _within_edits(a, b, maximum):
    """Small bounded Levenshtein check, after a high-similarity prefilter."""
    if abs(len(a) - len(b)) > maximum:
        return False
    previous = list(range(len(b) + 1))
    for i, char in enumerate(a, 1):
        current = [i]
        for j, other in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (char != other)))
        if min(current) > maximum:
            return False
        previous = current
    return previous[-1] <= maximum


def _title_similarity(wanted, actual):
    a, b = _key(wanted), _key(actual)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Never turn two/three-character names into loose edit-distance matches.
    if min(len(a), len(b)) < 6:
        return 0.0
    maximum = 2 if min(len(a), len(b)) >= 14 else 1
    if abs(len(a) - len(b)) > maximum:
        return 0.0
    ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
    return ratio if ratio >= .86 and _within_edits(a, b, maximum) else 0.0


def _query_echo(track):
    raw = track.raw
    if not isinstance(raw, dict):
        return False
    # These are explicit invalid-record flags, not normal query provenance.
    for key in ("query_echo", "queryEcho", "_query_echo", "is_query_echo"):
        value = raw.get(key)
        if value is True or value == 1 or value in ("true", "1"):
            return True
    return str(raw.get("type", "")).casefold() in {"lyrics", "lyric", "query", "query_echo"}


def query_suggestions(readings: Sequence[tuple[str, str]], candidates: Iterable[Track],
                      *, limit: int = 3, requested=()) -> list[str]:
    """Return at most three real ``原名（歌手）`` hints, without I/O or mutation.

    Titles and artists are compared within the SAME reading, never pooled
    across interpretations. If any explicit artist exists, artistless backup
    readings cannot bypass it. ``required_artist`` (when supplied by the query
    parser) remains binding. ``requested`` contains match.version_markers for
    an explicitly requested recording; the default suggests studio versions.
    All candidate/alias counts and text lengths have fixed cost limits.
    """
    if not isinstance(limit, int) or limit <= 0:
        return []
    limit = min(limit, 3)
    required = _text(getattr(readings, "required_artist", ""), 80)
    pairs = []
    for row in islice(readings, 8):
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            continue
        title, artist = _text(row[0]), _text(row[1], 80)
        if _key(title) and (not row[1] or _known_artist(artist)):
            pairs.append((title, artist))
    if any(artist for _, artist in pairs):
        pairs = [(title, artist) for title, artist in pairs if artist]
    if not pairs:
        return []
    requested = tuple(requested or ())
    ranked = {}
    for index, track in enumerate(islice(candidates, 256)):
        if not isinstance(track, Track):
            continue
        title, artist = _text(track.title), _text(track.artist, 80)
        if (not _key(title) or not _known_artist(artist) or _query_echo(track)
                or not match.recording_version_ok(track, requested)):
            continue
        if required and not _artist_identity(required, track):
            continue
        # Reuse the canonical-bound whitelist; lyrics/query echoes/other songs
        # are not aliases. Do not recursively inspect arbitrary raw strings.
        aliases = tuple(a for a in match.title_alias_evidence(track)
                        if _text(a) and _key(a))[:24]
        best = (0.0, 0.0)
        for wanted, performer in pairs:
            identity = _artist_identity(performer, track) if performer else 0.0
            if performer and not identity:
                continue
            similarity = _title_similarity(wanted, title)
            similarity = max(similarity, max(
                (jp.title_alias_similarity(wanted, alias) for alias in aliases), default=0.0))
            if similarity:
                best = max(best, (identity, similarity))
        if not best[1]:
            continue
        label = f"{title}（{artist}）"
        if len(label) > 160:
            continue
        key = (_key(title), _key(artist))
        value = (*best, -index, label)
        if key not in ranked or value[:3] > ranked[key][:3]:
            ranked[key] = value
    return [item[3] for item in sorted(ranked.values(), reverse=True)[:limit]]

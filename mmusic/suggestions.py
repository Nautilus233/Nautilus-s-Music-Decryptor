"""Bounded, offline search hints from existing song records; never acceptance.

The caller may append these strings to a failed-search message. A hint must
not change the query, relax a download gate or trigger another request.
"""
from __future__ import annotations

from dataclasses import replace
import re
import unicodedata
from difflib import SequenceMatcher
from itertools import islice
from typing import Iterable, Sequence

from . import jp, match
from .model import Track

__all__ = ["query_suggestions", "query_suggestion_choices"]

_JUNK = re.compile(
    r"<[^>]*>|https?://|www\.|&(?:lt|gt|#\d+|#x[0-9a-f]+);"
    r"|(?:^|[：:])\s*(?:搜索结果|搜索词|查询词|歌词|歌詞|lyrics|query|search results)"
    r"|(?:^|\s)(?:作词|作曲|編曲|作詞)\s*[:：]", re.I)
_ARTIST_SEP = re.compile(r"[、,;，；/\\&＋+|]|\s(?:feat\.?|ft\.?|with)\s", re.I)

# Only whole, explicit recording annotations can expose a base song name.
# Unknown annotations (including arbitrary API subtitles) are never erased.
_QUALIFIER = re.compile(
    r"(?:原版|原曲|录音室|正式版|完整版|伴奏|伴唱|纯伴奏|翻唱|现场|版|"
    r"ライブ|ライヴ|カバー|インスト|カラオケ|"
    r"original|studio|official|instrumental|karaoke|live|cover|remix|"
    r"off[ -]?vocal|version|ver\.?|inst|demo|acoustic|[\s/+、-])+", re.I)
_SUFFIX = re.compile(r"^(.*?)\s*(?:\(([^()]+)\)|【([^【】]+)】|\s+-\s+(.+))\s*$")
_LIVE = {"live", "concert", "现场", "現場", "演唱会", "演唱會", "巡演",
         "ライブ", "ライヴ", "ライブ版", "ライヴ版", "ライブバージョン", "ライヴバージョン"}
_INSTRUMENTAL = {"伴奏", "伴唱", "纯伴奏", "伴奏曲", "instrumental", "inst",
                 "karaoke", "カラオケ", "インスト", "インストゥルメンタル",
                 "off vocal", "offvocal", "minus one", "minusone"}
_COVER = {"cover", "翻唱", "カバー", "カバー版", "翻唱专辑", "翻唱專輯", "カバーアルバム"}
_CONTAINERS = {"metadata", "meta", "tags", "tag", "song", "track", "data",
               "songinfo", "trackinfo", "recordingtags", "transparam", "album",
               "albums", "albuminfo", "al"}
_FIELDS = {"name", "title", "songname", "song_name", "orisongname", "originalname",
           "filename", "file", "filehashname", "text", "suffix", "songnamesuffix",
           "subtitle", "subtitles", "albumname", "albumtitle", "专辑", "唱片集",
           "alias", "aliases", "alia", "tns", "transnames", "transname",
           "translatedname", "translationname"}
_METADATA_KEYS = _FIELDS | _CONTAINERS
_MAX_COMPARISONS = 8192


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
    """Banded Levenshtein: O(maximum * text length), not a full matrix."""
    if abs(len(a) - len(b)) > maximum:
        return False
    previous = {j: j for j in range(min(len(b), maximum) + 1)}
    for i, char in enumerate(a, 1):
        current = {0: i} if i <= maximum else {}
        for j in range(max(1, i - maximum), min(len(b), i + maximum) + 1):
            current[j] = min(current.get(j - 1, maximum + 1) + 1,
                             previous.get(j, maximum + 1) + 1,
                             previous.get(j - 1, maximum + 1) + (char != b[j - 1]))
        if min(current.values(), default=maximum + 1) > maximum:
            return False
        previous = current
    return previous.get(len(b), maximum + 1) <= maximum


def _title_similarity(wanted, actual, *, alias=False):
    a, b = _key(wanted), _key(actual)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Short edits are only hypotheses here. Evidence is checked AFTER source
    # aggregation, for this exact reading and canonical song/artist identity.
    if min(len(a), len(b)) < 6:
        # A query that is merely a proper prefix/suffix of a longer short
        # title is too ambiguous (夜行 -> 夜行者). Interior omissions and a
        # query's extra character remain eligible for evidence checks.
        if (alias or min(len(a), len(b)) < 2
                or (len(a) < len(b) and (b.startswith(a) or b.endswith(a)))
                or not _within_edits(a, b, 1)):
            return 0.0
        return SequenceMatcher(None, a, b, autojunk=False).ratio()
    maximum = 2 if min(len(a), len(b)) >= 14 else 1
    if abs(len(a) - len(b)) > maximum:
        return 0.0
    if not _within_edits(a, b, maximum):
        return 0.0
    ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
    return ratio if ratio >= (.92 if alias else .86) else 0.0


def _query_echo(track):
    raw = track.raw
    if not isinstance(raw, dict):
        return False
    # These are explicit invalid-record flags, not normal query provenance.
    for key in ("query_echo", "queryEcho", "_query_echo", "is_query_echo"):
        value = raw.get(key)
        if value is True or value == 1 or value in ("true", "1"):
            return True
    value = raw.get("type", "")
    return isinstance(value, str) and value.casefold() in {"lyrics", "lyric", "query", "query_echo"}


def _metadata(raw, title):
    """Bounded projection for shared match helpers; never copy arbitrary raw.

    32 nodes, depth <= 4, 128 fields/container, 24 list items and <= 120
    characters/string. Invalid recognized metadata rejects the record, rather
    than silently dropping a possibly important version/injection marker.
    """
    remaining = 32

    base_key = _key(_base_title(title))

    def visit(obj, depth, in_album=False):
        nonlocal remaining
        if depth > 4 or remaining <= 0:
            raise ValueError
        remaining -= 1
        if isinstance(obj, str):
            text = _text(obj)
            if obj and not text:
                raise ValueError
            return text
        if isinstance(obj, dict):
            if _query_echo(Track("", raw=obj)):
                raise ValueError
            out = {}
            for count, (key, value) in enumerate(islice(obj.items(), 129)):
                if count == 128:
                    raise ValueError
                if not isinstance(key, str) or len(key) > 40:
                    continue
                normalized = re.sub(r"[_\s-]", "", key.casefold())
                if normalized not in _METADATA_KEYS:
                    continue
                album = in_album or normalized in {"album", "albums", "albuminfo", "al"}
                if (not album and normalized in {"name", "title", "songname", "orisongname", "originalname"}
                        and isinstance(value, str)
                        and _key(_base_title(_text(value))) != base_key):
                    # Another song's name cannot supply aliases OR versions.
                    raise ValueError
                if isinstance(value, (str, dict, list, tuple)):
                    out[normalized] = visit(value, depth + 1, album)
            return out
        if isinstance(obj, (list, tuple)):
            if len(obj) > 24:
                raise ValueError
            return [visit(value, depth + 1, in_album) for value in obj[:24]
                    if isinstance(value, (str, dict, list, tuple))]
        return {}

    try:
        return visit(raw, 0) if isinstance(raw, dict) else {}
    except ValueError:
        return None


def _base_title(title):
    base = unicodedata.normalize("NFKC", title)
    for _ in range(3):
        found = _SUFFIX.fullmatch(base)
        if not found:
            break
        qualifier = next((s for s in found.groups()[1:] if s), "").strip()
        if not _QUALIFIER.fullmatch(qualifier) or not _key(found[1]):
            break
        base = found[1].strip()
    return base


def _version_label(markers):
    return sorted({"Live" if m in _LIVE else "伴奏" if m in _INSTRUMENTAL
                   else "翻唱" if m in _COVER else m for m in markers})


def _choice_title(track, markers):
    # Never present a metadata-only Live/cover/instrumental hit as studio.
    missing = set(_version_label(markers)) - set(_version_label(match.version_markers(track.title)))
    return track.title + (" (" + " / ".join(sorted(missing)) + ")" if missing else "")


def query_suggestion_choices(readings: Sequence[tuple[str, str]], candidates: Iterable[Track],
                             *, limit: int = 3, requested=()) -> list[tuple[str, str]]:
    """Return <= 3 (title, artist) identities for an EXPLICIT new search only.

    Titles and artists are compared within the SAME reading, never pooled
    across interpretations. If any explicit artist exists, artistless backup
    readings cannot bypass it. ``required_artist`` (when supplied by the query
    parser) remains binding. Nonempty ``requested`` still binds the recording.
    Otherwise version-filtered records are allowed, with their real title and
    any metadata-only version markers appended, never invented studio titles.

    One short insertion/deletion needs an explicit artist OR a nonempty source
    ID attesting the displayed title + artist. Source agreement ranks optional
    identities; it is not a download gate. Substitution needs an
    explicit artist, plus two sources for two-character titles. One-character
    edits, prefix/suffix extensions and short fuzzy aliases are forbidden.
    Equally close different work identities remain explicit choices, without
    selecting an artist or claiming a correction. Source agreement proves a catalog
    identity, NOT user intent, recording availability, or source independence.

    Cost caps: 8 readings, 256 candidates, 24 aliases/record, 8192 comparisons,
    bounded metadata projection and bounded text/edit checks; no I/O/mutation.
    """
    if not isinstance(limit, int) or limit <= 0:
        return []
    limit = min(limit, 3)
    raw_required = getattr(readings, "required_artist", "")
    required = _text(raw_required, 80)
    if raw_required and not _known_artist(required):
        return []
    pairs, has_explicit_artist = [], False
    for row in islice(readings, 8):
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            continue
        has_explicit_artist |= bool(row[1])
        if not all(isinstance(value, str) for value in row):
            continue
        title, artist = _text(row[0]), _text(row[1], 80)
        if _key(title) and (not row[1] or _known_artist(artist)):
            pairs.append((title, artist))
    if has_explicit_artist:
        pairs = [(title, artist) for title, artist in pairs if artist]
    if not pairs:
        return []
    requested = tuple(islice(requested or (), 33))
    if len(requested) > 32 or any(not _text(marker, 40) for marker in requested):
        return []
    records, support = [], {}
    comparisons = 0
    for index, track in enumerate(islice(candidates, 256)):
        if not isinstance(track, Track):
            continue
        title, artist = _text(track.title), _text(track.artist, 80)
        album = _text(track.album)
        raw = _metadata(track.raw, title)
        if (not _key(title) or not _known_artist(artist) or raw is None
                or (track.album and not album)):
            continue
        track = replace(track, title=title, artist=artist, album=album, raw=raw)
        if requested and not match.recording_version_ok(track, requested):
            continue
        if required and not _artist_identity(required, track):
            continue
        markers = match.track_version_markers(track)
        choice_title = _choice_title(track, markers)
        if len(choice_title) + len(artist) + 2 > 160:
            continue
        base = _base_title(title)
        work = (_key(base), _key(artist))
        source = _text(track.source, 80).casefold()
        if source in {"unknown", "none", "未知"}:
            source = ""
        # Reuse the canonical-bound whitelist; lyrics/query echoes/other songs
        # are not aliases. Do not recursively inspect arbitrary raw strings.
        aliases = tuple(a for a in match.title_alias_evidence(track)
                        if _text(a) and _key(a))[:24]
        hits = []
        for row, (wanted, performer) in enumerate(pairs):
            identity = _artist_identity(performer or required, track) if performer or required else 0.0
            if performer and not identity:
                continue
            wanted = _base_title(wanted)
            if comparisons >= _MAX_COMPARISONS:
                break
            comparisons += 1
            similarity = _title_similarity(wanted, base)
            a, b = _key(wanted), _key(base)
            short = ("indel" if len(a) != len(b) else "sub2" if len(a) == 2 else "sub") \
                if similarity and similarity < 1 and min(len(a), len(b)) < 6 else ""
            for alias in aliases:
                if comparisons >= _MAX_COMPARISONS:
                    break
                comparisons += 1
                score = _title_similarity(wanted, alias, alias=True)
                if score > similarity:
                    similarity, short = score, ""
            if similarity:
                hits.append((row, identity, similarity, short))
                if source:
                    support.setdefault((row, work), set()).add(source)
        if hits:
            records.append((index, work, (choice_title, artist), hits))

    eligible, closest = [], {}
    for index, work, choice, hits in records:
        for row, identity, similarity, short in hits:
            sources = len(support.get((row, work), ()))
            if short == "indel" and not (identity or sources):
                continue
            if short in {"sub", "sub2"} and not (identity and (short != "sub2" or sources >= 2)):
                continue
            score = (identity, similarity)
            eligible.append((row, work, choice, short, score, index))
            closest[row] = max(closest.get(row, (-1.0, -1.0)), score)

    ranked = {}
    for row, work, choice, short, score, index in eligible:
        best = closest[row]
        if short and score < best:
            continue
        key = (_key(choice[0]), _key(choice[1]))
        sources = len(support.get((row, work), ()))
        value = (*score, sources, -index, choice)
        if key not in ranked or value[:4] > ranked[key][:4]:
            ranked[key] = value
    return [item[4] for item in sorted(ranked.values(), reverse=True)[:limit]]


def query_suggestions(readings: Sequence[tuple[str, str]], candidates: Iterable[Track],
                      *, limit: int = 3, requested=()) -> list[str]:
    """Compatible labels from exactly the same ranked structured identities."""
    return [f"{title}（{artist}）" for title, artist in query_suggestion_choices(
        readings, candidates, limit=limit, requested=requested)]

"""Song-name normalisation and best-match ranking."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Sequence

from .model import Track

__all__ = ["clean_text", "filename_parts", "filename_query",
           "is_original_version", "normalize_for_version", "original_markers",
           "parse_song_filename", "parse_song_filename_candidates",
           "rank_tracks", "rank_tracks_multi", "reading_scores",
           "similarity", "split_originals", "tokenize",
           "track_version_markers", "version_markers"]

# noise that streaming platforms sprinkle into filenames
_BRACKET_NOISE = re.compile(r"[\[(（【][^\[\]()（）【】]*[\]）】)]")
_NOISE_WORDS = re.compile(
    r"\b(mqms\d*|flac|mp3|320k|hires|hifi|sq|hq|live|remaster(ed)?|version|"
    r"explicit|clean|official|mv|lyric)\b", re.IGNORECASE)
_SEP = re.compile(r"\s*[-–—_|/]\s*")
_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+")


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _BRACKET_NOISE.sub(" ", text)
    text = text.replace("&nbsp;", " ").replace("\u3000", " ")
    text = _NOISE_WORDS.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> list[str]:
    return [t for t in _PUNCT.split(clean_text(text).lower()) if t]


# --------------------------------------------------------------------- #
# 版本过滤：原曲 vs 伴奏 / 改编版（用户要求的"歌名字段强匹配"）
# --------------------------------------------------------------------- #
# 中文/日文标记按「子串」匹配（它们不会出现在别的常用词内部）；
# 拉丁标记按「词元」匹配，避免 Alive 命中 live、Instant 命中 inst 这类误伤。
# 注意：这里检查的是**原始字符串**，括号内容不会被去掉 —— 与 clean_text 不同。
_BLOCK_SUBSTRINGS: "tuple[str, ...]" = (
    "伴奏", "伴唱", "纯音乐", "无人声", "无伴奏", "卡拉ok", "カラオケ",
    "翻唱", "现场", "试听", "不插电", "剪辑",
    "演唱会", "巡演", "串烧", "联唱", "演出版",      # 现场/串烧类，同样不是录音室原曲
    "off vocal", "offvocal", "minus one", "minusone",
)
_BLOCK_WORDS = frozenset({
    "instrumental", "inst", "karaoke", "remix", "cover",
    "live", "demo", "acoustic", "edit", "mix", "concert", "medley",
})
# 明确的原曲标记 —— 同曲多版本时用于"录音室版优先"
_ORIGINAL_WORDS = frozenset({
    "studio", "original", "official", "原版", "原曲", "录音室", "正式版", "完整版",
})
_SEP_NORM = re.compile(r"[._\-+/\\|,:;!?\"'\u201c\u201d\u2018\u2019()\[\]{}<>\u00b7~@#$%^&*]+")
_BRACKET = re.compile(r"[\[(\uff08\u3010]")
_RAW_NAME_KEYS = frozenset({
    "name", "title", "songname", "song_name", "filename", "file",
    "filehashname", "tags", "tag", "text",
})


def normalize_for_version(text: str) -> str:
    """小写 + NFKC 归一化，并把所有分隔符/标点折叠成空格。"""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = _SEP_NORM.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def version_markers(text: str) -> "set[str]":
    """返回 `text` 中命中的伴奏/改编标记（作用于原始串，不剥离括号内容）。"""
    norm = normalize_for_version(text)
    if not norm:
        return set()
    tokens = set(norm.split())
    hits = {w for w in _BLOCK_WORDS if w in tokens}
    hits |= {p for p in _BLOCK_SUBSTRINGS if p in norm}
    return hits


def original_markers(text: str) -> "set[str]":
    """明确的原曲/录音室标记。"""
    norm = normalize_for_version(text)
    if not norm:
        return set()
    tokens = set(norm.split())
    return {w for w in _ORIGINAL_WORDS if w in tokens or w in norm}


def track_version_markers(track: Track) -> "set[str]":
    """歌曲名/文件名/标签一类字段里命中的版本标记合集。"""
    fields = [track.title]
    for key, value in (track.raw or {}).items():
        if isinstance(value, str) and str(key).casefold() in _RAW_NAME_KEYS:
            fields.append(value)
    hits: "set[str]" = set()
    for field in fields:
        hits |= version_markers(field)
    return hits


def is_original_version(track: Track) -> bool:
    """该候选是否"干净"（不含任何伴奏/改编标记）。"""
    return not track_version_markers(track)


def split_originals(tracks: "Sequence[Track]") -> "tuple[list[Track], list[tuple[Track, set[str]]]]":
    """拆成 (原曲, [(被排除的改编版, 命中的标记), ...])。"""
    originals: list[Track] = []
    dropped: list[tuple[Track, set[str]]] = []
    for t in tracks:
        markers = track_version_markers(t)
        if markers:
            dropped.append((t, markers))
        else:
            originals.append(t)
    return originals, dropped


def filename_parts(stem: str) -> list[str]:
    """Split a filename stem into its non-empty parts (order preserved)."""
    stem = clean_text(stem)
    if not stem:
        return []
    return [p.strip() for p in _SEP.split(stem) if p.strip()]


def parse_song_filename_candidates(stem: str) -> list[tuple[str, str]]:
    """Every plausible ``(title, artist)`` reading of a filename stem.

    Two orders are equally common in the wild: ``Artist - Title`` (what music
    apps tend to write) and ``Title - Artist`` (how people often name files by
    hand).  A bare filename cannot tell them apart, so we return *both*
    readings and let the ranking pick whichever fits the search results
    better.  That is a deterministic choice between two stated readings - it
    never guesses at the content itself.
    """
    parts = filename_parts(stem)
    if not parts:
        return []
    if len(parts) == 1:
        return [(parts[0], "")]
    first, last = parts[0], parts[-1]
    if first == last:
        return [(first, "")]
    return [(last, first),      # "Artist - Title"
            (first, last)]      # "Title - Artist"


def parse_song_filename(stem: str) -> tuple[str, str]:
    """The single most likely ``(title, artist)`` reading (best guess first)."""
    candidates = parse_song_filename_candidates(stem)
    return candidates[0] if candidates else ("", "")


def filename_query(stem: str) -> str:
    """Keyword query for a filename: its parts joined with spaces.

    Order is preserved so the phrasing stays natural under either convention
    ("周杰伦 稻香" / "晴天 周杰伦").  Search back-ends treat the terms as a
    keyword bag, so a single query covers every reading - no extra requests.
    """
    parts = filename_parts(stem)
    return " ".join(parts) if parts else clean_text(stem)



def similarity(a: str, b: str) -> float:
    a, b = clean_text(a).lower(), clean_text(b).lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    # token-overlap bonus so "song (live)" still matches "song"
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if ta and tb:
        overlap = len(ta & tb) / max(len(ta), len(tb))
        ratio = max(ratio, 0.5 * ratio + 0.5 * overlap)
    return ratio


def _score_one(title: str, artist: str, track: Track, duration: int, prefer_ext: str) -> float:
    score = 0.72 * similarity(title, track.title) + 0.28 * similarity(artist, track.artist)
    if duration and track.duration:
        delta = abs(duration - track.duration)
        score += 0.12 * max(0.0, 1.0 - delta / 15.0)          # <=15s counts as a match
    if prefer_ext and track.ext:
        if track.ext.lower() == prefer_ext.lower():
            score += 0.06
        elif prefer_ext == "wav" and track.ext.lower() == "flac":
            score += 0.04
    # 同曲多版本时的偏好：明确的原曲/录音室标记加分；完全没有修饰词（无括号）再加一点
    if original_markers(track.title):
        score += 0.03
    if track.title and not _BRACKET.search(track.title):
        score += 0.02
    return score


def reading_scores(readings: Sequence[tuple[str, str]], track: Track,
                   *, duration: int = 0, prefer_ext: str = "") -> list[tuple[float, tuple[str, str]]]:
    """Score one track under every filename reading, best reading first."""
    scored = [(_score_one(t, a, track, duration, prefer_ext), (t, a)) for t, a in readings]
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


def rank_tracks_multi(readings: Sequence[tuple[str, str]], tracks: Sequence[Track],
                      *, duration: int = 0, prefer_ext: str = "") -> list[Track]:
    """Rank `tracks` by each track's *best* score across all filename readings."""
    readings = [r for r in readings if r[0] or r[1]] or [("", "")]
    for t in tracks:
        t.score = round(max(_score_one(rt, ra, t, duration, prefer_ext) for rt, ra in readings), 4)
    return sorted(tracks, key=lambda t: t.score, reverse=True)


def rank_tracks(query_title: str, query_artist: str, tracks: Sequence[Track],
                *, duration: int = 0, prefer_ext: str = "") -> list[Track]:
    """Rank against a single ``(title, artist)`` reading (kept for simple callers)."""
    return rank_tracks_multi([(query_title, query_artist)], tracks,
                             duration=duration, prefer_ext=prefer_ext)

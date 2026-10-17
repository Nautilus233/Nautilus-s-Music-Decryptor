"""Candidate fusion: content dedupe + low-confidence marking.

This layer sits **on top of** the existing scoring in :mod:`mmusic.match`
and deliberately never changes a score.  It only:

* collapses candidates that are the *same resource* reached through several
  sources (so we don't burn a download attempt on a duplicate), and
* flags low-confidence candidates so the log / UI can warn about them.

Normalisation is intentionally **conservative**: only case and whitespace are
unified, bracket qualifiers are *kept* (``晴天`` and ``晴天 (Live)`` must stay
distinct, otherwise we could silently drop a version the user explicitly
asked for).  A missed dedupe merely costs one extra attempt - a wrong one
would be a correctness bug.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Sequence

from ..model import Track

__all__ = ["LOW_CONFIDENCE", "DURATION_BUCKET", "dedupe_key",
           "collapse_duplicates", "mark_low_confidence", "is_low_confidence"]

#: below this score a candidate is only *flagged*, never auto-rejected
#: (the hard accept/reject policy stays in pipeline.py)
LOW_CONFIDENCE = 0.45
#: declared durations within this many seconds count as the same version
DURATION_BUCKET = 2

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("&nbsp;", " ").replace("\u3000", " ")
    return _WS.sub(" ", text).strip().lower()


def dedupe_key(track: Track) -> str:
    """Identity of a *resource* (song+version), not of a search hit.

    The declared duration is rounded into :data:`DURATION_BUCKET`-second
    buckets so that two platforms reporting 3:55 and 3:56 still collide,
    while a clearly shortened version (3:55 vs 2:22) does not."""
    duration = int(track.duration or 0)
    bucket = int(round(duration / DURATION_BUCKET)) if duration else 0
    return f"{_norm(track.title)}|{_norm(track.artist)}|{bucket}"


def collapse_duplicates(tracks: Sequence[Track]) -> "tuple[list[Track], list[tuple[Track, Track]]]":
    """Keep the first (best-ranked) occurrence of each key.

    Returns ``(kept, dropped)`` where each dropped entry is
    ``(duplicate, keeper)``.  Input order is preserved, so ``tracks`` is
    expected to be already sorted best-first.
    """
    kept: list[Track] = []
    dropped: list[tuple[Track, Track]] = []
    seen: "dict[str, Track]" = {}
    for track in tracks:
        key = dedupe_key(track)
        keeper = seen.get(key)
        if keeper is None:
            seen[key] = track
            kept.append(track)
        else:
            dropped.append((track, keeper))
    return kept, dropped


def mark_low_confidence(tracks: Sequence[Track], *, threshold: float = LOW_CONFIDENCE) -> int:
    """Flag candidates scoring below `threshold`; returns how many were flagged."""
    flagged = 0
    for track in tracks:
        low = bool(track.score) and track.score < threshold
        if isinstance(track.raw, dict):
            track.raw["low_confidence"] = low
        flagged += int(low)
    return flagged


def is_low_confidence(track: Track) -> bool:
    return bool(track.raw.get("low_confidence")) if isinstance(track.raw, dict) else False
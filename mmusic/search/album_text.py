"""Album-only orthographic forms; display names and audio tags stay untouched."""
from __future__ import annotations

from functools import lru_cache

from ..paths import resource_path


@lru_cache(maxsize=1)
def _tables() -> tuple[dict[int, str], dict[int, str]]:
    pairs: dict[str, str] = {}
    try:
        for line in resource_path("mmusic", "data", "album_hanzi.tsv").read_text("utf-8").splitlines():
            if line.startswith("#") or "\t" not in line:
                continue
            original, simplified = line.split("\t", 1)
            if len(original) == len(simplified) == 1:
                pairs[original] = simplified
    except (OSError, ValueError):
        pass
    reverse: dict[str, list[str]] = {}
    for original, simplified in pairs.items():
        reverse.setdefault(simplified, []).append(original)
    # Ambiguous simplified characters never generate a guessed traditional form.
    return (str.maketrans(pairs), str.maketrans({
        simplified: originals[0] for simplified, originals in reverse.items()
        if len(originals) == 1}))


def folded(text: str) -> str:
    return (text or "").translate(_tables()[0])


def alternate(text: str) -> str:
    """One bounded search spelling, only for unambiguous character mappings."""
    simplified = folded(text)
    return simplified if simplified != text else (text or "").translate(_tables()[1])

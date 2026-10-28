# -*- coding: utf-8 -*-
"""模糊输入纠错（只影响「搜什么」，不放松「什么算命中」的硬门槛）。

* 拉丁串：相邻换位 → 邻键替换 → 漏字 → 重复字母（各限 1 次编辑），
  每条输入串最多产出 2 条候选；
* 中文串：内置混淆字对（同音/形近，构建期取自 pycorrector 数据）替换，
  用高频词表打分排序（「睛天→晴天」「夜取→夜曲」这类首选命中）；
* 全部为纯文本变换：零依赖、有上限、失败静默。

数据来源与许可：mmusic/data/SOURCES.md。
"""
from __future__ import annotations

import functools
import unicodedata
from itertools import islice

from .paths import resource_path

__all__ = ["corrections_for", "corrected_readings", "looks_typo", "suggestion_queries"]


def suggestion_queries(readings, limit=2):
    """Attested CJK deletion queries for FAILED-search hints only.

    Never feeds corrected_readings or audio acceptance. At most two identities,
    bounded to short CJK titles and existing vocabulary; no guessed insertion,
    translation or artist change. A real catalog response is still required.
    """
    if not isinstance(limit, int) or limit <= 0:
        return []
    words = _words()
    output = []
    for title, artist in islice(readings or (), 8):
        text = unicodedata.normalize("NFKC", title or "").strip()
        if not 3 <= len(text) <= 8 or not all("\u4e00" <= c <= "\u9fff" for c in text):
            continue
        if text in words:
            continue
        choices = {text[:i] + text[i+1:] for i in range(len(text))}
        ranked = sorted((v for v in choices if v in words), key=lambda v: (-words[v], v))
        for value in ranked:
            query = " ".join(p for p in (value, artist) if p).strip()
            if query not in output:
                output.append(query)
            if len(output) >= min(limit, 2):
                return output
    return output

_KEYBOARD = {
    "q": "wa", "w": "qeas", "e": "wrsd", "r": "etdf", "t": "ryfg",
    "y": "tugh", "u": "yihj", "i": "uojk", "o": "ipkl", "p": "ol",
    "a": "qwsz", "s": "awedxz", "d": "serfcx", "f": "drtgvc", "g": "ftyhbv",
    "h": "gyujnb", "j": "huikmn", "k": "jiolm", "l": "kop",
    "z": "asx", "x": "zsdc", "c": "xdfv", "v": "cfgb", "b": "vghn",
    "n": "bhjm", "m": "njk",
}
_MAX_LEN = 60


@functools.lru_cache(maxsize=1)
def _confusions() -> "dict[str, list[str]]":
    out: "dict[str, list[str]]" = {}
    try:
        with open(resource_path("mmusic", "data", "cjk_confusions.tsv"), encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 2 or len(parts[0]) != 1 or len(parts[1]) != 1:
                    continue
                a, b = parts[0], parts[1]
                for x, y in ((a, b), (b, a)):
                    bucket = out.setdefault(x, [])
                    if y not in bucket:
                        bucket.append(y)
    except OSError:
        return {}
    return out


@functools.lru_cache(maxsize=1)
def _words() -> "dict[str, int]":
    out: "dict[str, int]" = {}
    try:
        with open(resource_path("mmusic", "data", "cjk_words.txt"), encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\r\n").split("\t")
                w = parts[0].strip()
                if not (2 <= len(w) <= 6):
                    continue
                try:
                    count = int(parts[1]) if len(parts) > 1 else 1
                except ValueError:
                    count = 1
                out[w] = count
    except OSError:
        return {}
    return out


def _word_score(text: str) -> float:
    """命中词的**频率加权**得分：候选写法越像真实词、越常见，分越高。"""
    words = _words()
    if not words:
        return 0.0
    import math

    score = 0.0
    seen: "set[str]" = set()
    for i in range(len(text)):
        for length in (2, 3, 4):
            sub = text[i : i + length]
            if len(sub) == length and sub not in seen and sub in words:
                seen.add(sub)
                score += 1.0 + math.log10(1.0 + words[sub])
    return score


def _cjk_candidates(text: str) -> "list[str]":
    pairs = _confusions()
    if not pairs:
        return []
    scored: "list[tuple[float, str]]" = []
    for i, ch in enumerate(text):
        options = pairs.get(ch)
        if not options:
            continue
        for cand in options[:40]:
            variant = text[:i] + cand + text[i + 1 :]
            scored.append((_word_score(variant), variant))
    scored.sort(key=lambda item: (-item[0], item[1]))
    out: "list[str]" = []
    for _score, variant in scored:
        if variant != text and variant not in out:
            out.append(variant)
        if len(out) >= 6:
            break
    return out


def _latin_candidates(token: str) -> "list[str]":
    if not (4 <= len(token) <= 24) or not token.isascii() or not token.isalpha():
        return []
    out: "list[str]" = []
    for i in range(len(token) - 1):                     # 相邻换位
        if token[i] != token[i + 1]:
            out.append(token[:i] + token[i + 1] + token[i] + token[i + 2 :])
    for i, ch in enumerate(token):                      # 邻键替换
        for neighbour in _KEYBOARD.get(ch, ""):
            out.append(token[:i] + neighbour + token[i + 1 :])
    if len(token) >= 5:                                 # 漏字
        for i in range(len(token)):
            out.append(token[:i] + token[i + 1 :])
    for i in range(len(token) - 1):                     # 重复字母
        if token[i] == token[i + 1]:
            out.append(token[:i] + token[i + 1 :])
    deduped: "list[str]" = []
    for value in out:
        if value != token and value not in deduped:
            deduped.append(value)
    return deduped[:2]


def corrections_for(text: str, limit: int = 2) -> "list[str]":
    """给一段输入生成纠错候选（按可信度排序；最多 ``limit`` 条）。"""
    t = unicodedata.normalize("NFKC", text or "").strip()
    if not t or len(t) > _MAX_LEN:
        return []
    out: "list[str]" = []
    if any("\u4e00" <= ch <= "\u9fff" for ch in t):
        for value in _cjk_candidates(t):
            if value not in out:
                out.append(value)
            if len(out) >= limit:
                return out
    for token in t.split():
        if len(out) >= limit:
            break
        for value in _latin_candidates(token.lower()):
            replaced = " ".join(value if part == token else part for part in t.split())
            if replaced not in out and replaced != t:
                out.append(replaced)
            if len(out) >= limit:
                break
    return out[:limit]


def looks_typo(text: str, corrections: "list[str] | None" = None) -> bool:
    """输入是否**看起来是错字**：原串不像词、而纠错串明显更像词（词表加权判据）。

    只用于决定「纠错候选要不要提前进入检索变体」——判错的代价是少几个人工候选，
    不参与任何门槛判定。干净查询（原串本身就是词）不会掺入纠错变体，省下请求。
    """
    t = unicodedata.normalize("NFKC", text or "").strip()
    if not t or len(t) > _MAX_LEN:
        return False
    candidates = list(corrections) if corrections else corrections_for(t, limit=2)
    if not candidates:
        return False
    base = _word_score(t)
    best = 0.0
    for cand in candidates:
        best = max(best, _word_score(cand))
    return best > 0.0 and best >= base + 3.0


def corrected_readings(readings, limit: int = 3) -> "list[tuple[str, str]]":
    """给 (歌名, 歌手) 读法列表生成「纠错读法」——只在原读法失败时作为第二次机会。

    保持读法结构：字段分别纠正（只动被纠错的字段），最多 ``limit`` 条。
    """
    out: "list[tuple[str, str]]" = []
    for title, artist in readings or []:
        title = (title or "").strip()
        artist = (artist or "").strip()
        fixed_t = corrections_for(title, limit=1)
        fixed_a = corrections_for(artist, limit=1)
        if fixed_t:
            pair = (fixed_t[0], artist)
            if pair not in out and pair != (title, artist):
                out.append(pair)
        if fixed_a:
            pair = (title, fixed_a[0])
            if pair not in out and pair != (title, artist):
                out.append(pair)
        if fixed_t and fixed_a and len(out) < limit:
            pair = (fixed_t[0], fixed_a[0])
            if pair not in out:
                out.append(pair)
        if len(out) >= limit:
            break
    return out[:limit]

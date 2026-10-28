"""Query expansion: the same song, written several ways.

A single keyword string misses a lot of cold/awkward titles, so we generate a
small, ordered list of variants.  Every variant carries the reason it exists,
so the log can say which phrasing actually hit.

P4（2026-10-22）追加：
* **日语形态**：罗马音 → 平/片假名；词表与唯一词典读音可补查书写原名。实测（2026-10-22）：
  酷狗对片假名读音有索引（「グレンゲ→紅蓮華」「センボンザクラ→千本桜」「ヨルシカ」
  均可命中），罗马音则对艺术家字段有效（yorushika / kenshi yonezu）。
* **纠错候选**：模糊输入（邻键/换位/中文混淆字）纠错后的写法，排在常规变体之后，
  由管线的**后续 sweep** 消费（首轮每源 ≤2 个；仅当全部轮次零命中才追加下一层）。

Pure standard library; reuses the existing normalisation in ``mmusic.match``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .. import fuzzy, jp
from ..match import clean_text, normalize_for_version

__all__ = ["QueryVariant", "expand"]

#: hard cap on variants attempted per source **per sweep** (cost control)
MAX_PER_SOURCE = 2
#: hard cap on the variant list itself（分层 sweep 依次切片消费；失败路径封顶 2 层）
MAX_VARIANTS = 9

_LATIN_TOKEN = re.compile(r"[A-Za-z]{4,}")


@dataclass(frozen=True)
class QueryVariant:
    text: str
    reason: str


def expand(query: str, readings: "Sequence[tuple[str, str]]" = (), *,
           limit: int = MAX_VARIANTS) -> list[QueryVariant]:
    """Ordered, de-duplicated search phrasings for one song.

    顺序（越靠前越优先；每个源首轮消费前 2 个，全部无果才启用第二轮）：

    1. ``有依据的原名`` 表内原名 / 唯一词典书写形 / 精确简称（避免原样同音结果截断后续）
    2. ``原样/日语形态`` 用户原文及分别扩展的歌名、歌手平/片假名
    3. ``去噪``       括号/质量标签去掉后的写法
    4. ``歌名 歌手``  读法里第一组"两个字段都全"的组合
    5. ``仅歌名``     只有歌名的写法（找回"歌手字段是空的"上传物）
    6. ``纠错候选``   模糊输入的纠错写法（第二轮备用；单字段输入时紧随其后）
    7. ``仅歌手``     兜底
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

    # Expand fields independently. A composed query is not a Japanese word:
    # converting it as one string loses dictionary evidence and field identity.
    # Still at most four requests/source (two existing sweeps); the first four
    # slots prioritize originals, verified kanji, hiragana and katakana.
    jp_items: "list[tuple[str, str]]" = []
    orthographic_items: "list[tuple[str, str]]" = []
    for title, artist in list(readings)[:2] or [(query, "")]:
        titles = jp.search_variants(title, kind="song") if title else []
        artists = jp.search_variants(artist, kind="artist") if artist else []
        if artist:
            preferred_artist = next((value for value, reason in artists
                                     if reason == "表内原名"), "")
            if not preferred_artist:
                preferred_artist = next((value for value, reason in artists
                                         if "平假名" in reason), artist)
            for value, reason in titles:
                jp_items.append((f"{value} {preferred_artist}", reason))
            for value, reason in artists:
                jp_items.append((" ".join(x for x in (title, value) if x), reason))
        else:
            jp_items.extend(titles)
    if (not jp.has_kana(query) and not re.search(r"[A-Za-z]", query)
            and len(query) > 1 and not jp.title_aliases(query)):
        # Ordinary Chinese queries keep their existing cheap field order.
        orthographic_items = [item for item in jp_items if item[1] == "汉字书写形"]
        jp_items = [item for item in jp_items if item[1] != "汉字书写形"]
    # Fallback for legacy callers without parsed fields.
    if not jp_items and not readings:
        jp_items.extend(jp.search_variants(query))
    for value, reason in jp_items:
        if len(variants) >= MAX_PER_SOURCE * 2:
            break
        if len(value) <= 100:
            add(value, reason)

    # 纠错候选：只在「看起来是错字」（词表判据）时**提前**进入；
    # 干净查询干脆不掺入（省请求）；含较长拉丁词的查询保留在后位（罗马音拼错用）。
    corrections = fuzzy.corrections_for(query, limit=2)
    early = bool(corrections) and fuzzy.looks_typo(query, corrections)
    if early:
        for idx, value in enumerate(corrections):
            add(value, "纠错候选" if idx == 0 else "纠错候选2")

    add(clean_text(query), "去噪")

    pair = next(((t, a) for t, a in readings
                 if (t or "").strip() and (a or "").strip()), None)
    if pair:
        add(f"{pair[0]} {pair[1]}", "歌名 歌手")

    for title in [t for t, _a in readings if (t or "").strip()]:
        add(title, "仅歌名")

    for value, reason in orthographic_items:
        add(value, reason)

    if not early and corrections and _LATIN_TOKEN.search(query or ""):
        for idx, value in enumerate(corrections):
            add(value, "纠错候选" if idx == 0 else "纠错候选2")

    for artist in [a for _t, a in readings if (a or "").strip()]:
        add(artist, "仅歌手")
    # A raw romaji homonym can already return unrelated Latin-titled tracks.
    # Since the pipeline stops on a successful variant, attested written forms
    # must be queried first, not left unreachable behind that success.
    priority = {"表内原名", "词典原名", "表内简称原名"}
    variants.sort(key=lambda v: 0 if v.reason in priority else 1)
    return variants[: max(1, limit)]

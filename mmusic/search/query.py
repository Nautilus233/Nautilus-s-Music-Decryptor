"""Query expansion: the same song, written several ways.

A single keyword string misses a lot of cold/awkward titles, so we generate a
small, ordered list of variants.  Every variant carries the reason it exists,
so the log can say which phrasing actually hit.

P4（2026-10-22）追加：
* **日语形态**：罗马音 → 片假名读音；纯假名 → 片假名形 / 罗马音。实测（2026-10-22）：
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

    1. ``原样``       用户/文件名的原文
    2. ``日语形态``   歌手名读音 / 片假名读音 / 罗马音（有则插在最前，价值最高）
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

    # 日语形态：先「歌手名字段」（拿回整张歌手页），再整条读音。
    # 收紧（2026-10-22）：① 歌手名读音里若已有内置表认识的写法，就**只留认识的**
    # （避免「半个标题」被当成歌手名去搜）；② 整条读音形超过 16 字基本无命中，直接省掉。
    artist_items: "list[tuple[str, str]]" = []
    for _t, artist in readings:
        if (artist or "").strip():
            kana = jp.artist_katakana(artist)
            if kana:
                artist_items.append((kana, "日文歌手名读音"))
    known_artists = [(v, r) for v, r in artist_items if jp.known_kind(v)]
    artist_items = known_artists or artist_items[:1]
    jp_items: "list[tuple[str, str]]" = list(artist_items)
    for value, reason in jp.search_variants(query):
        # 拼接出的长串（多段空格/超过 16 字）命中率极低，直接省掉请求
        if len(value) <= 16 and value.count(" ") <= 1:
            jp_items.append((value, reason))
    jp_items.sort(key=lambda item: (0 if jp.known_kind(item[0]) else 1, -len(item[0])))
    inserted = 0
    jp_seen: set[str] = set()
    for value, reason in jp_items:
        key = normalize_for_version(value)
        if not key or key in jp_seen or key in seen:
            continue
        jp_seen.add(key)
        add(value, reason)
        inserted += 1
        if inserted >= 2:
            break

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

    if not early and corrections and _LATIN_TOKEN.search(query or ""):
        for idx, value in enumerate(corrections):
            add(value, "纠错候选" if idx == 0 else "纠错候选2")

    for artist in [a for _t, a in readings if (a or "").strip()]:
        add(artist, "仅歌手")
    return variants[: max(1, limit)]

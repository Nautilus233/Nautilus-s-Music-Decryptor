# -*- coding: utf-8 -*-
"""日语文本处理：假名⇄罗马音（确定性）、汉字读音（内置表）、比对形态、检索变体。

设计约束（AGENTS.md 红线）：
* **零依赖**：只用标准库；数据来自 mmusic/data 的内置表（懒加载，缺失时优雅降级）。
* **不猜测**：汉字读音只来自内置表（JMdict / 维基词条读音）；查不到就不转换，
  绝不按单个汉字臆断（例如「米津」不逐个汉字拼读音）。表命中的读音按排名取前几个
  作为**候选**参与比对（取最大相似度），不声明唯一正确答案。
* **成本可控**：所有生成器都有数量上限；默认只做一次转换尝试 + 少量候选。

词表来源与许可见 mmusic/data/SOURCES.md。
"""
from __future__ import annotations

import functools
import re
import unicodedata

from .paths import resource_path

__all__ = [
    "has_kana", "has_kanji", "hiraganize", "katakanaize",
    "romaji_to_kana", "kana_to_romaji", "looks_japanese",
    "match_forms", "reading_combos", "search_variants", "known_titles",
    "artist_katakana", "lookup_known", "known_kind",
]

_KANJI_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_KANA_RE = re.compile(r"[\u3041-\u309f\u30a0-\u30ff]")
_HIRA_RE = re.compile(r"[\u3041-\u309f]")
_KATA_RE = re.compile(r"[\u30a1-\u30f6]")

#: 罗马音 → 假名（Hepburn 为主，另收 Kunrei/常见拼法变体；一次生成两张表）
_SYLLABLES = [
    ("a", "あ"), ("i", "い"), ("u", "う"), ("e", "え"), ("o", "お"),
    ("ka", "か"), ("ki", "き"), ("ku", "く"), ("ke", "け"), ("ko", "こ"),
    ("sa", "さ"), ("shi", "し"), ("su", "す"), ("se", "せ"), ("so", "そ"),
    ("ta", "た"), ("chi", "ち"), ("tsu", "つ"), ("te", "て"), ("to", "と"),
    ("na", "な"), ("ni", "に"), ("nu", "ぬ"), ("ne", "ね"), ("no", "の"),
    ("ha", "は"), ("hi", "ひ"), ("fu", "ふ"), ("he", "へ"), ("ho", "ほ"),
    ("ma", "ま"), ("mi", "み"), ("mu", "む"), ("me", "め"), ("mo", "も"),
    ("ya", "や"), ("yu", "ゆ"), ("yo", "よ"),
    ("ra", "ら"), ("ri", "り"), ("ru", "る"), ("re", "れ"), ("ro", "ろ"),
    ("wa", "わ"), ("wo", "を"),
    ("ga", "が"), ("gi", "ぎ"), ("gu", "ぐ"), ("ge", "げ"), ("go", "ご"),
    ("za", "ざ"), ("ji", "じ"), ("zu", "ず"), ("ze", "ぜ"), ("zo", "ぞ"),
    ("da", "だ"), ("di", "ぢ"), ("du", "づ"), ("de", "で"), ("do", "ど"),
    ("ba", "ば"), ("bi", "び"), ("bu", "ぶ"), ("be", "べ"), ("bo", "ぼ"),
    ("pa", "ぱ"), ("pi", "ぴ"), ("pu", "ぷ"), ("pe", "ぺ"), ("po", "ぽ"),
    ("kya", "きゃ"), ("kyu", "きゅ"), ("kyo", "きょ"),
    ("sha", "しゃ"), ("shu", "しゅ"), ("sho", "しょ"),
    ("cha", "ちゃ"), ("chu", "ちゅ"), ("cho", "ちょ"),
    ("nya", "にゃ"), ("nyu", "にゅ"), ("nyo", "にょ"),
    ("hya", "ひゃ"), ("hyu", "ひゅ"), ("hyo", "ひょ"),
    ("mya", "みゃ"), ("myu", "みゅ"), ("myo", "みょ"),
    ("rya", "りゃ"), ("ryu", "りゅ"), ("ryo", "りょ"),
    ("gya", "ぎゃ"), ("gyu", "ぎゅ"), ("gyo", "ぎょ"),
    ("ja", "じゃ"), ("ju", "じゅ"), ("jo", "じょ"),
    ("bya", "びゃ"), ("byu", "びゅ"), ("byo", "びょ"),
    ("pya", "ぴゃ"), ("pyu", "ぴゅ"), ("pyo", "ぴょ"),
    ("vu", "ゔ"),
    ("fa", "ふぁ"), ("fi", "ふぃ"), ("fe", "ふぇ"), ("fo", "ふぉ"),
    ("wi", "うぃ"), ("we", "うぇ"),
    ("n", "ん"), ("nn", "ん"),
]
#: 拼法变体（输入备用）：Kunrei 式、省略式、打字习惯
_R2K_EXTRA = {
    "si": "し", "ti": "ち", "tu": "つ", "hu": "ふ", "zi": "じ",
    "sya": "しゃ", "syu": "しゅ", "syo": "しょ",
    "tya": "ちゃ", "tyu": "ちゅ", "tyo": "ちょ",
    "cya": "ちゃ", "cyu": "ちゅ", "cyo": "ちょ",
    "zya": "じゃ", "zyu": "じゅ", "zyo": "じょ",
    "jya": "じゃ", "jyu": "じゅ", "jyo": "じょ",
    "dya": "ぢゃ", "dyu": "ぢゅ", "dyo": "ぢょ",
    "la": "ら", "li": "り", "lu": "る", "le": "れ", "lo": "ろ",
    "lya": "りゃ", "lyu": "りゅ", "lyo": "りょ",
    "xtu": "っ", "xtsu": "っ", "ltu": "っ", "ltsu": "っ",
}
_R2K: "dict[str, str]" = {}
for _r, _k in _SYLLABLES:
    _R2K[_r] = _k
_R2K.update(_R2K_EXTRA)
_K2R: "dict[str, str]" = {}
for _r, _k in _SYLLABLES:
    _K2R.setdefault(_k, _r)
for _k, _r in _R2K_EXTRA.items():
    _K2R.setdefault(_R2K[_k], _r)
_K2R.update({"\u3093": "n", "\u30f3": "n",
             # Hepburn 标准：づ/ぢ 按 zu/ji 转写（键入者更常写 yonezu 而非 yonedu）
             "\u3065": "zu", "\u3062": "ji"})


def has_kanji(text: str) -> bool:
    return bool(_KANJI_RE.search(text or ""))


def has_kana(text: str) -> bool:
    return bool(_KANA_RE.search(text or ""))


def hiraganize(text: str) -> str:
    return "".join(chr(ord(c) - 0x60) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in (text or ""))


def katakanaize(text: str) -> str:
    return "".join(chr(ord(c) + 0x60) if 0x3041 <= ord(c) <= 0x3096 else c for c in (text or ""))


def romaji_to_kana(text: str) -> "str | None":
    """ASCII 罗马音 → 平假名（确定性，多拼法兼容）。含无法映射字符时返回 None。"""
    s = unicodedata.normalize("NFKC", text or "").casefold().replace("\u2019", "'")
    out: "list[str]" = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch in "'\u00b7":
            i += 1
            continue
        if ch.isspace() or ch in "-~–—":
            out.append(" ")
            i += 1
            continue
        if ch in ".,!?;:()[]{}\"":
            i += 1
            continue
        # 促音：双写辅音（n 与元音除外）；tch/cch 特例
        if i + 1 < n and ch == s[i + 1] and ch.isalpha() and ch not in "aeioun":
            out.append("っ")
            i += 1
            continue
        if ch in "tc" and s[i + 1 : i + 3] == "ch":
            out.append("っ")
            i += 1
            continue
        for length in (3, 2, 1):
            sub = s[i : i + length]
            kana = _R2K.get(sub)
            if kana:
                out.append(kana)
                i += length
                break
        else:
            return None
    result = "".join(out)
    return result.strip() or None


def kana_to_romaji(text: str) -> "str | None":
    """假名 → 罗马音（仅处理假名/ ASCII 标点；含汉字时返回 None 以免误用）。"""
    if has_kanji(text or ""):
        return None
    s = hiraganize(unicodedata.normalize("NFKC", text or "")).casefold()
    out: "list[str]" = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "っ":
            nxt = _next_romaji(s, i + 1)
            if nxt:
                out.append("t" if nxt.startswith("ch") else nxt[0])
            i += 1
            continue
        if ch == "ー":
            if out and out[-1] and out[-1][-1] in "aeiou":
                out.append(out[-1][-1])
            i += 1
            continue
        if ch == " " or ch == "\u3000":
            out.append(" ")
            i += 1
            continue
        if ch.isascii():
            if ch.isalnum():
                out.append(ch)
            i += 1
            continue
        two = s[i : i + 2]
        if len(two) == 2 and two in _K2R:
            out.append(_K2R[two])
            i += 2
            continue
        one = _K2R.get(ch)
        if one is not None:
            if one == "n":
                nxt = _next_romaji(s, i + 1)
                out.append("n'" if (nxt and nxt[0] in "aeiouy") else "n")
            else:
                out.append(one)
            i += 1
            continue
        i += 1
    return "".join(out).strip()


def _next_romaji(s: str, i: int) -> str:
    """看向下一个能产出罗马音的音节（跳过被跳过的字符）。"""
    while i < len(s):
        ch = s[i]
        if ch in " \u3000ー":
            i += 1
            continue
        two = s[i : i + 2]
        if len(two) == 2 and two in _K2R:
            return _K2R[two]
        one = _K2R.get(ch)
        if one is not None and one != "n":
            return one
        if ch.isascii() and ch.isalnum():
            return ch
        i += 1
    return ""


def _collapse(s: str) -> str:
    """比对用折叠：小写、去撇号、长音折叠（ou/oo/aa… → 单音）、wo→o、空白规整。"""
    s = (s or "").casefold().replace("'", "")
    s = " ".join("o" if tok == "wo" else tok for tok in s.split())
    s = re.sub(r"([aeiou])\1+", r"\1", s)
    for _ in range(2):
        s = s.replace("ou", "o")
        s = re.sub(r"([aeiou])\1+", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- #
# 内置表加载（懒加载；缺表时静默降级为空表）
# --------------------------------------------------------------------------- #
def _read_rows(name: str) -> "list[list[str]]":
    path = resource_path("mmusic", "data", name)
    rows: "list[list[str]]" = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if not line or line.startswith("#"):
                    continue
                rows.append(line.split("\t"))
    except OSError:
        pass
    return rows


_WORDS_MAXKEY = 0


def _words_maxkey() -> int:
    global _WORDS_MAXKEY
    if not _WORDS_MAXKEY:
        _WORDS_MAXKEY = max((len(k) for k in _words()), default=1)
    return _WORDS_MAXKEY


@functools.lru_cache(maxsize=1)
def _words() -> "dict[str, list[str]]":
    out: "dict[str, list[str]]" = {}
    for parts in _read_rows("jp_words.tsv"):
        if len(parts) >= 2 and parts[0]:
            out.setdefault(parts[0], [])
            for r in parts[1:]:
                if r and r not in out[parts[0]]:
                    out[parts[0]].append(r)
    return out


def _parse_named(name: str) -> "dict[str, str]":
    out: "dict[str, str]" = {}
    for parts in _read_rows(name):
        if len(parts) >= 2 and parts[0] and parts[1]:
            out.setdefault(_key(parts[0]), parts[1])
            out.setdefault(_key(hiraganize(parts[0])), parts[1])
    return out


@functools.lru_cache(maxsize=1)
def _songs() -> "dict[str, str]":
    return _parse_named("jp_songs.tsv")


@functools.lru_cache(maxsize=1)
def _artists() -> "dict[str, str]":
    return _parse_named("jp_artists.tsv")


# --------------------------------------------------------------------------- #
# 查表索引：键 → (规范假名读音, 书写形)
# 键含两族并加前缀区分：k:=假名键（去空白，含 wa/ha 助词变体）；r:=罗马音折叠键。
# 2026-10-23 用户实测修复：`dakarabokuha ongakuwo yameta`（不空格）与
# `dakara boku ha ongaku wo yameta`（按词空格）必须**同样命中**；旧实现用
# "按空白分词后整词 wo→o" 的折叠，导致两种写法的键不一致 —— 一个命中、一个落空。
# --------------------------------------------------------------------------- #
def _key(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"\s+", " ", t).strip()


_BRACKET_NOISE = re.compile(r"[\[(\uff08\u3010][^\[\]()\uff08\uff09\u3010\u3011]*[\])\uff09\u3011]")


def _ascii_convertible(text: str) -> bool:
    t = unicodedata.normalize("NFKC", text or "").strip()
    if len(t) < 2 or not re.fullmatch(r"[A-Za-z][A-Za-z' \-]*[A-Za-z]", t):
        return False
    return romaji_to_kana(t) is not None


def looks_japanese(text: str) -> bool:
    """文本是否呈日语倾向（可安全套用假名/罗马音变体与读音比对）。"""
    t = unicodedata.normalize("NFKC", text or "").strip()
    if not t:
        return False
    if has_kana(t):
        return True
    if has_kanji(t):
        return False          # 汉字本身不足以判定（可能是中文），由调用方按读法表另行处理
    return _ascii_convertible(t)


def _kana_bare(text: str) -> str:
    t = hiraganize(unicodedata.normalize("NFKC", text or ""))
    return t.replace(" ", "").replace("\u3000", "")


@functools.lru_cache(maxsize=1)
def _known_index() -> "dict[str, tuple[str, str]]":
    idx: "dict[str, tuple[str, str]]" = {}
    for name in ("jp_songs.tsv", "jp_artists.tsv"):
        for parts in _read_rows(name):
            if len(parts) < 2:
                continue
            written, reading = parts[0].strip(), parts[1].strip()
            if not written or not reading:
                continue
            entry = (reading, written)
            keys: "list[str]" = []
            bare = _kana_bare(reading)
            if bare:
                keys.append("k:" + bare)
            romaji = kana_to_romaji(reading)
            if romaji:
                c = _collapse(romaji)
                keys.append("r:" + c.replace(" ", ""))
                keys.append("r:" + " ".join(sorted(c.split())))
            for key in keys:
                idx.setdefault(key, entry)
    return idx


def _known_keys(text: str) -> "list[str]":
    """输入 → 查表键候选（假名键 + 罗马音折叠键；含 Hepburn 里 wa/ha 助词拼法变体）。"""
    t = _key(text)
    if not t:
        return []
    keys: "list[str]" = []

    def push(key: str) -> None:
        if key and key not in keys:
            keys.append(key)

    kana: "str | None" = None
    if has_kana(t):
        kana = t
    elif _ascii_convertible(t):
        kana = romaji_to_kana(t)
    if kana:
        bare = _kana_bare(kana)
        if bare:
            push("k:" + bare)
            # Hepburn 里常把助词は写成 wa：等价键再补一条（只用于查表，不做其它推断）
            if "\u308f" in bare:
                push("k:" + bare.replace("\u308f", "\u306f"))
        romaji = kana_to_romaji(kana)
        if romaji:
            c = _collapse(romaji)
            push("r:" + c.replace(" ", ""))
            push("r:" + " ".join(sorted(c.split())))
    return keys


def lookup_known(text: str) -> "str | None":
    """罗马音输入 → 内置表（歌曲/歌手）里的规范**假名读音**；查不到返回 None。"""
    t = _key(text)
    if not t or has_kana(t) or not _ascii_convertible(t):
        return None
    idx = _known_index()
    for key in _known_keys(text):
        entry = idx.get(key)
        if entry:
            return entry[0]
    return None


def known_titles(text: str) -> "list[str]":
    """罗马音/假名输入 → 内置表里的**书写形**（原名）。

    为什么需要（2026-10-23 用户实测）：四平台的专辑检索都按**书写形**索引，读音形
    （片假名/平假名读音）检索不到 —— 用"读音"去搜只会拿到钢琴版/翻奏。表里有书写形时
    直接搜原名才对：
        `dakarabokuha ongakuwo yameta` → `[だから僕は音楽を辞めた]`
        `ただきみにはれ`（平假名读音）→ `[ただ君に晴れ]`
    """
    idx = _known_index()
    out: "list[str]" = []
    for key in _known_keys(text):
        entry = idx.get(key)
        if entry and entry[1] not in out:
            out.append(entry[1])
    return out


def known_kind(text: str) -> "str | None":
    """内置表是否能正面认出这段名称（'artist' / 'song' / None，用于变体排序）。"""
    t = _key(text)
    if not t:
        return None
    for table, kind in ((_artists(), "artist"), (_songs(), "song")):
        if _key(t) in table or _key(hiraganize(t)) in table:
            return kind
    if lookup_known(t):
        return "artist"
    return None


# --------------------------------------------------------------------------- #
# 读音组合（DFS，候选 ≤ N；只在表全命中时成立）
# --------------------------------------------------------------------------- #
def reading_combos(text: str, limit: int = 6) -> "tuple[tuple[str, ...], ...]":
    if not text:
        return ()
    return _reading_combos_cached(_key(text), max(1, int(limit)))


@functools.lru_cache(maxsize=4096)
def _reading_combos_cached(text: str, limit: int) -> "tuple[tuple[str, ...], ...]":
    t = unicodedata.normalize("NFKC", text).strip()
    if not t:
        return ()

    def _override(value: str) -> "tuple[tuple[str, ...], ...] | None":
        for table in (_songs(), _artists()):
            hit = table.get(_key(value)) or table.get(_key(hiraganize(value)))
            if hit:
                tokens = tuple(tok for tok in hit.split(" ") if tok)
                return (tokens,) if tokens else ()
        return None

    # 表内整条命中（歌曲/歌手读音）优先：先原文，再剥掉括号说明（（电影主题曲）之类）
    hit = _override(t)
    if hit is not None:
        return hit
    stripped = _BRACKET_NOISE.sub(" ", t).strip()
    if stripped and stripped != t:
        hit = _override(stripped)
        if hit is not None:
            return hit
        t = stripped
    words = _words()
    maxkey = _words_maxkey()
    segments: "list[list[str]]" = []
    i, n = 0, len(t)
    while i < n:
        ch = t[i]
        if _KANA_RE.match(ch) or ch == "\u30fc":
            j = i
            while j < n and (_KANA_RE.match(t[j]) or t[j] == "\u30fc"):
                j += 1
            segments.append([hiraganize(t[i:j])])
            i = j
            continue
        hit_len = None
        for length in range(min(maxkey, n - i), 0, -1):
            sub = t[i : i + length]
            options = words.get(sub)
            if options:
                hit_len = (length, list(options[:3]))
                break
        if hit_len is None:
            if _KANJI_RE.match(ch):
                return ()                      # 未知汉字：不猜，整条放弃
            if ch.isalnum() and ch.isascii():
                j = i
                while j < n and t[j].isascii() and (t[j].isalnum() or t[j] in " -'."):
                    j += 1
                segments.append([t[i:j].casefold()])
                i = j
                continue
            i += 1                             # 标点等：跳过
            continue
        length, options = hit_len
        segments.append(options)
        i += length
    combos: "list[tuple[str, ...]]" = [()]
    for options in segments:
        grown: "list[tuple[str, ...]]" = []
        for combo in combos:
            for opt in options:
                if opt:
                    grown.append(combo + (opt,))
                if len(grown) >= limit * 2:
                    break
            if len(grown) >= limit * 2:
                break
        combos = grown[: limit * 2]
        if not combos:
            return ()
    return tuple(combos[:limit])


def match_forms(text: str) -> "tuple[str, ...]":
    """供相似度比对的确定性形态（罗马音折叠形）。无日文内容 / 无法转换时为空。"""
    if not text:
        return ()
    return _match_forms_cached(_key(text))


@functools.lru_cache(maxsize=4096)
def _match_forms_cached(t: str) -> "tuple[str, ...]":
    t = unicodedata.normalize("NFKC", t).strip()
    if not t:
        return ()
    forms: "list[str]" = []

    def _push(romaji: str) -> None:
        if not romaji:
            return
        c = _collapse(romaji)
        for value in (c.replace(" ", ""), c, " ".join(sorted(c.split()))):
            if value and value not in forms:
                forms.append(value)

    # 括号里的纯拉丁内容（「残酷な天使のテーゼ (Zankoku na Tenshi no Teze)」）也参与
    paren_latin = [m.group(1).strip() for m in re.finditer(r"[（(]([^（()）]{2,80})[)）]", t)
                   if all(ord(c) < 128 for c in m.group(1).strip())
                   and any(c.isalpha() for c in m.group(1))]
    if has_kana(t) or has_kanji(t):
        combos = reading_combos(t)
        if not combos:
            combos = ((hiraganize(t),),) if has_kana(t) else ()
        for combo in combos:
            _push(kana_to_romaji("".join(combo)) or "")
            _push(kana_to_romaji(" ".join(combo)) or "")
    elif _ascii_convertible(t):
        kana = romaji_to_kana(t)
        if kana:
            _push(t)
            _push(kana_to_romaji(kana) or "")
    for value in paren_latin:
        _push(value)
    return tuple(forms)


# --------------------------------------------------------------------------- #
# 检索变体 / 歌手名形态
# --------------------------------------------------------------------------- #
def search_variants(text: str) -> "list[tuple[str, str]]":
    """为检索生成「同一首歌的另一种文字形态」，按价值排序（不含原样）。

    * 含假名/汉字混合 → 整条读音的**片假名**形；
    * 纯假名 → 内置表的**书写形**（如 `ただきみにはれ`→`ただ君に晴れ`）＋片假名形＋罗马音；
    * 纯罗马音 → 内置表的**书写形**优先（`dakarabokuha ongakuwo yameta`→`だから僕は音楽を辞めた`），
      表外才退回片假名读音形；
    * 纯汉字 → 不生成（可能是中文，不做迷信转换）。
    """
    t = _key(text)
    if not t:
        return []
    out: "list[tuple[str, str]]" = []
    seen: "set[str]" = set()

    def add(value: "str | None", reason: str) -> None:
        if value and value != t and value not in seen:
            seen.add(value)
            out.append((value, reason))

    if has_kana(t):
        if has_kanji(t):
            combos = reading_combos(t, limit=1)
            if combos:
                add(katakanaize("".join(combos[0])), "日文片假名读音")
        else:
            # 纯假名：若这是内置表里的**读音**，先给"书写形"（汉字原名可直接检索）
            for title in known_titles(t):
                add(title, "表内原名")
            h = hiraganize(t)
            add(katakanaize(h), "片假名形")
            add(kana_to_romaji(h), "罗马音")
    elif not has_kanji(t) and _ascii_convertible(t):
        # 罗马音：内置表里的**书写形**优先（读音形平台检索不到，2026-10-23 实测修复）
        for title in known_titles(t):
            add(title, "表内原名")
        known = lookup_known(t)
        kana = known if known else romaji_to_kana(t)
        add(katakanaize(kana or ""), "日文片假名读音")
    return out


def artist_katakana(text: str) -> "str | None":
    """歌手名 → 检索用假名形（表内读音优先；查不到时按确定性转换）。"""
    t = _key(text)
    if not t:
        return None
    if has_kana(t):
        return katakanaize(hiraganize(t))
    if has_kanji(t) and not has_kana(t):
        for table in (_artists(), _songs()):
            hit = table.get(_key(t)) or table.get(_key(hiraganize(t)))
            if hit:
                return katakanaize(hit)
    if not has_kanji(t) and _ascii_convertible(t):
        known = lookup_known(t)
        if known:
            return katakanaize(known)
        kana = romaji_to_kana(t)
        return katakanaize(kana) if kana else None
    combos = reading_combos(t, limit=1)
    if combos:
        return katakanaize(" ".join(combos[0]))
    return None

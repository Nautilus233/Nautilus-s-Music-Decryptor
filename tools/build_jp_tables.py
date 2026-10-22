#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建日语检索/匹配内置表（联网一次，产物入库；运行时不联网）。

数据来源（全部公开、构建脚本可复跑）：
  * JMdict（jmdict-simplified, EDRDG）—— CC BY-SA 4.0
    词条 → 假名读音；派生出「词干形」「汉字压缩形」（打ち上げ→打上 等）。
  * 日语维基百科（ja.wikipedia API）—— CC BY-SA 4.0
    热门歌曲/歌手条目的读音（首段括号 / DEFAULTSORT / 信息框字段）。
    人工清单只决定「选哪些条目」；读音一律以抓取结果为准，抓不到就不写。
  * pycorrector 数据文件（Apache-2.0）
    same_pinyin / same_stroke（中文混淆字对）、word_freq（纠错候选排序词表）、
    common_char_set（常用字过滤）。
输出（写入 mmusic/data/，UTF-8，无 BOM）：
  jp_words.tsv       surface <TAB> reading1 [<TAB> reading2]
  jp_songs.tsv       title   <TAB> reading  <TAB> src(w=wiki, j=jmdict回退, m=人工)
  jp_artists.tsv     同上
  cjk_confusions.tsv a <TAB> b <TAB> type(p/p2/s)
  cjk_words.txt      一行一词（用于纠错候选的排序打分）
  SOURCES.md         来源与许可说明

用法：
  python tools/build_jp_tables.py [--cache DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "mmusic", "data")
JMDICT_TAG = "3.6.2+20261019180022"
UA = {"User-Agent": "MusicDecryptor-JPTableBuilder/1.0 (personal-use build script)"}
PYCORR = "https://raw.githubusercontent.com/shibing624/pycorrector/master/pycorrector"

KANJI_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
KANA_RE = re.compile(r"[\u3041-\u309f\u30a0-\u30ff]")
KANA_ONLY_RE = re.compile(r"^[\u3041-\u309f\u30a0-\u30ff\u30fc ]+$")
#: 标记为「罕见/不规则写法」的标签：不把这类词面当常规读音来源
BAD_TAGS = frozenset({"sk", "sK", "rK", "ok", "ik", "oK", "iK", "ateji", "gikun", "io", "rk"})


def has_kanji(text: str) -> bool:
    return bool(KANJI_RE.search(text or ""))


def hira(text: str) -> str:
    out = []
    for ch in text:
        o = ord(ch)
        out.append(chr(o - 0x60) if 0x30a1 <= o <= 0x30f6 else ch)
    return "".join(out)


def fetch(url: str, path: str, timeout: float = 300.0) -> str:
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    return path


def load_tgz_json(path: str) -> dict:
    with tarfile.open(path, "r:gz") as tf:
        member = tf.getmembers()[0]
        return json.load(tf.extractfile(member))


# --------------------------------------------------------------------------- #
# 1) JMdict → jp_words
# --------------------------------------------------------------------------- #
def build_word_table(words: list) -> dict:
    table: dict = {}
    for w in words:
        ks = w.get("kanji") or []
        vs = w.get("kana") or []
        if not ks or not vs:
            continue
        sanity = any(k.get("common") for k in ks) or any(v.get("common") for v in vs)
        for k in ks:
            kt = (k.get("text") or "").strip()
            if not kt or len(kt) > 12 or not has_kanji(kt):
                continue
            kbad = bool(set(k.get("tags") or []) & BAD_TAGS)
            for v in vs:
                vt = hira((v.get("text") or "").strip())
                if not vt or set(v.get("tags") or []) & BAD_TAGS:
                    continue
                if not re.fullmatch(r"[\u3041-\u309f\u30fc]+", vt):
                    continue
                rank = 0 if (k.get("common") and v.get("common")) else (
                    1 if k.get("common") else (2 if sanity else 3))
                if kbad:
                    rank = min(3, rank + 1)
                d = table.setdefault(kt, {})
                if d.get(vt, 99) > rank:
                    d[vt] = rank
    return table


def add_derived(table: dict) -> dict:
    extra: dict = {}

    def put(key: str, reading: str, rank: int) -> None:
        d = extra.setdefault(key, {})
        if d.get(reading, 99) > rank:
            d[reading] = rank

    for kt, readings in table.items():
        for vt, rank in readings.items():
            i = 0
            while i < len(vt) and i < len(kt) and vt[len(vt) - 1 - i] == kt[len(kt) - 1 - i]:
                i += 1
            if 0 < i < len(vt):
                k2, v2 = kt[: len(kt) - i], vt[: len(vt) - i]
                if k2 and has_kanji(k2):
                    put(k2, v2, rank)                       # 词干：駆ける → 駆/かけ
            if KANA_RE.search(kt):
                k3 = "".join(ch for ch in kt if not KANA_RE.match(ch))
                if k3 != kt and k3 and has_kanji(k3):
                    put(k3, vt, min(3, rank + 1))           # 压缩形：打ち上げ → 打上/うちあげ
    for k, rs in extra.items():
        target = table.setdefault(k, {})
        for v, r in rs.items():
            if target.get(v, 99) > r:
                target[v] = r
    return table


def prune_word_table(table: dict, mode: str) -> dict:
    out: dict = {}
    for k, rs in table.items():
        if len(k) > 10:
            continue
        best = sorted(rs.items(), key=lambda x: (x[1], len(x[0]), x[0]))[:2]
        if mode == "r1":
            keep = any(r <= 1 for _v, r in best)
        elif mode == "r2l2":
            keep = any(r <= 1 for _v, r in best) or (len(k) <= 2 and any(r <= 2 for _v, r in best))
        else:  # r2l3
            keep = any(r <= 1 for _v, r in best) or (len(k) <= 3 and any(r <= 2 for _v, r in best))
        if len(k) == 1:
            keep = keep or any(r <= 2 for _v, r in best)
        if keep:
            out[k] = [v for v, _r in best]
    return out


def simple_reading_of(text: str, table: dict) -> str:
    """构建期的简易回退读音（与运行时算法同构的贪心版）。"""
    maxlen = max((len(k) for k in table), default=1)
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append("".join(buf))
            buf.clear()

    i = 0
    while i < len(text):
        ch = text[i]
        if KANA_RE.match(ch) or ch == "\u30fc":
            buf.append(hira(ch))
            i += 1
            continue
        hit = None
        for L in range(min(maxlen, len(text) - i), 0, -1):
            sub = text[i : i + L]
            if sub in table:
                hit = (L, table[sub][0])
                break
        if hit is None:
            return None
        flush()
        out.append(hit[1])
        i += hit[0]
    flush()
    return " ".join(out)


# --------------------------------------------------------------------------- #
# 2) 热度清单（人工挑选"哪些条目"；读音由维基/词典提供）
# --------------------------------------------------------------------------- #
SONGS = [
    "残酷な天使のテーゼ", "魂のルフラン", "夜に駆ける", "群青", "怪物", "優しい彗星",
    "祝福", "勇者", "紅蓮華", "炎", "明け星", "白銀", "打上花火", "前前前世",
    "愛にできることはまだあるかい", "千本桜", "六兆年と一夜物語", "初音ミクの消失",
    "命に嫌われている", "天体観測", "世界に一つだけの花", "涙そうそう", "負けないで",
    "いい日旅立ち", "卒業写真", "少年時代", "川の流れのように", "上を向いて歩こう",
    "見上げてごらん夜の星を", "糸", "手紙～拝啓十五の君へ～", "桜坂", "田園", "島唄",
    "愛唄", "遥か", "栄光の架橋", "全力少年", "小さな恋のうた", "空も飛べるはず",
    "夜空ノムコウ", "夏祭り", "3月9日", "桜 (コブクロの曲)", "蕾", "愛をこめて花束を",
    "桜色舞うころ", "恋", "白日", "宿命", "感電", "馬と鹿", "灰色と青", "海の幽霊",
    "地球儀", "逆夢", "一途", "三文小説", "特殊", "怪獣の花唄", "踊り子", "不可幸力",
    "新時代", "唱", "死ぬのがいいわ", "愛を伝えたいだとか", "今夜このまま",
    "高嶺の花子さん", "水平線", "瞬き", "逢いたくていま", "さよならの向う側", "秋桜",
    "天城越え", "津軽海峡・冬景色", "北酒場", "時の流れに身をまかせ", "愛人",
    "また逢う日まで", "赤いスイートピー", "瑠璃色の地球", "天使のウィンク", "少女A",
    "難破船", "乾杯", "15の夜", "卒業 (尾崎豊の曲)", "悲しみにさよなら",
    "少年 (福山雅治の曲)", "家族になろうよ", "ひまわりの約束", "旅立ちの日に",
    "翼をください", "贈る言葉", "想い出がいっぱい", "学生街の喫茶店", "神田川",
    "22才の別れ", "なごり雪", "心もよう", "ルビーの指環", "勝手にしやがれ",
    "時の過ぎゆくままに", "大空と大地の中で", "銀河鉄道999", "宇宙戦艦ヤマト",
    "君の知らない物語", "紅蓮の弓矢", "明日があるさ", "今日の日はさようなら",
    "ただ君に晴れ", "又三郎", "斜陽", "老人と海", "花に亡霊", "だから僕は音楽を辞めた",
    "春を告げる", "夜撫でるメノウ",
]
ARTISTS = [
    "米津玄師", "宇多田ヒカル", "椎名林檎", "中島美嘉", "安室奈美恵", "浜崎あゆみ",
    "松任谷由実", "竹内まりや", "山口百恵", "美空ひばり", "坂本龍一", "久石譲",
    "福山雅治", "玉置浩二", "尾崎豊", "長渕剛", "井上陽水", "氷室京介", "大黒摩季",
    "島谷ひとみ", "高橋洋子", "中川翔子", "水樹奈々", "石川さゆり", "五輪真弓",
    "中森明菜", "工藤静香", "森高千里", "今井美樹", "倖田來未", "加藤ミリヤ",
    "西野カナ", "青山テルマ", "中島みゆき", "五木ひろし", "北島三郎", "氷川きよし",
    "加山雄三", "沢田研二", "西城秀樹", "郷ひろみ", "近藤真彦", "田原俊彦",
    "松田聖子", "森山直太朗", "平井堅", "星野源", "秦基博", "清水翔太", "桑田佳祐",
    "大塚愛", "鬼束ちひろ", "絢香", "菅田将暉", "槇原敬之", "小田和正", "山下達郎",
    "吉田拓郎", "谷村新司", "松山千春", "中村雅俊", "布施明", "八代亜紀", "小林幸子",
    "都はるみ", "藤圭子", "島倉千代子", "坂本冬美", "ヨルシカ", "YOASOBI", "あいみょん",
    "ずっと真夜中でいいのに。", "緑黄色社会", "女王蜂", "Official髭男dism",
]
#: 抓取失败后的手工补充（必须经过人工核实；仅填确定项）
MANUAL_SONGS: dict = {
    # 词表/缓存正文已核实的固定补充：
    # 「糸」— jp_words: 糸=いと；「少年」— jp_words: 少年=しょうねん；
    # 「逆夢」— 已抓到的单曲正文：「一途/逆夢」（いちず/さかゆめ）；
    # 「だから僕は音楽を辞めた」— 该专辑页 DEFAULTSORT たからほくはおんかくをやめた（排序键去浊规则还原）
    #   并与词表 辞める=やめる/音楽=おんがく/僕=ぼく 一致。
    "糸": "いと",
    "少年 (福山雅治の曲)": "しょうねん",
    "逆夢": "さかゆめ",
    "だから僕は音楽を辞めた": "だからぼくはおんがくをやめた",
}
MANUAL_ARTISTS: dict = {
    # 信息框「ふりがな = よねづ けんし」（聚焦抓取时已核实；带空格以保留姓氏/名字分段）
    "米津玄師": "よねづ けんし",
}

#: 已知标题别名（消歧义/波浪线写法差异）：抓取与匹配用别名，表里仍存原名
TITLE_ALIASES = {
    "手紙～拝啓十五の君へ～": "手紙 〜拝啓 十五の君へ〜",
    "感電": "感電 (米津玄師の曲)",
    "白日": "白日 (King Gnuの曲)",
    "秋桜": "秋桜 (山口百恵の曲)",
    "唱": "唱 (Adoの曲)",
    "難破船": "難破船 (曲)",
    "高橋洋子": "高橋洋子 (歌手)",
    "女王蜂": "女王蜂 (バンド)",
}


#: 全局开关（离线收尾模式）：只读缓存，不发网络请求
NO_FETCH = [False]


def wiki_summary(title: str) -> str:
    url = "https://ja.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title, safe="")
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            data = json.load(r)
        return data.get("extract", "") or ""
    except urllib.error.HTTPError as exc:
        raise exc
    except Exception:
        return ""


def wiki_wikitext(title: str, attempts: int = 3) -> str:
    url = ("https://ja.wikipedia.org/w/api.php?action=query&format=json&prop=revisions"
           "&rvprop=content&rvslots=main&redirects=1&titles=" + urllib.parse.quote(title, safe=""))
    for attempt in range(max(1, attempts)):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.load(r)
            pages = ((data.get("query") or {}).get("pages") or {})
            for p in pages.values():
                rev = (p.get("revisions") or [{}])[0]
                wt = (((rev.get("slots") or {}).get("main") or {}).get("*") or "")
                if wt:
                    return wt
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                time.sleep(6.0 * (attempt + 1))
                continue
        except Exception:
            pass
        time.sleep(1.5 * (attempt + 1))
    return ""


def normalize_reading(reading: str) -> str | None:
    if not reading:
        return None
    r = reading.replace("\u3000", " ").replace("・", "")
    r = re.sub(r"\s+", " ", r).strip()
    if not r or not KANA_ONLY_RE.match(r):
        return None
    if len(r.replace(" ", "")) < 2 or len(r) > 40:
        return None
    return r


def _flex_title(title: str) -> str:
    """标题 → 容许任意空白穿插的正则（应对维基用「米津 玄師」这类空格写法）。"""
    chars = [c for c in title.replace(" ", "").replace("\u3000", "")]
    parts = []
    for c in chars:
        if c in "\uff5e\u301c~":
            parts.append("[\uff5e\u301c~]")          # 全角/半角波浪线混用
        else:
            parts.append(re.escape(c))
    return r"\s*".join(parts)


def _title_variants(title: str) -> "list[str]":
    out = [title]
    for sep in (" (", "\uff08"):
        if sep in title:
            head = title.split(sep, 1)[0].strip()
            if head and head not in out:
                out.append(head)
    return out


_KANA_READ = r"[\u3041-\u309f\u30a0-\u30ff\u30fc\u30fb\s\u3000]{1,40}?"
_LQUOTE = r"[\s\u3000\u300c\u300e\"\u201c\u201d\u2018\u2019\'\*]*"
_RQUOTE = r"[\s\u3000\u300d\u300f\u3002\u3001\u30fb\"\u201c\u201d\u2018\u2019\'\*]*"


def extract_reading_from_summary(text: str, title: str) -> str | None:
    """摘要兜底：仅当 TITLE（读音）は … 这种「标题锚定」形式才采信（防错括号）。"""
    if not text:
        return None
    head = text[:800]
    for t in _title_variants(title):
        m = re.match("^" + _LQUOTE + _flex_title(t) + _RQUOTE +
                     r"[\uff08(]\s*(" + _KANA_READ + r")\s*[)\uff09]\s*は", head)
        if m:
            got = normalize_reading(m.group(1))
            if got:
                return got
    return None


def extract_reading_from_wikitext(wt: str, title: str) -> str | None:
    """正文提取（三段式）：严格标题锚定 → 信息框/模板 → 宽容兜底。

    严格形：「标题（读音、…）」或「标题（读音）」+ は/、 —— 几乎不会误配；
    宽容兜底只处理 读音后跟 <ref> / とは / 半角逗号 这类版面细节（放在最后，
    避免拿「（シングルヴァージョン）」这类概念词当读音）。
    """
    if not wt:
        return None
    text = wt[:60000]
    bridge = r"(?:/[^\uff08(\uff09)\n]{1,24})?"       # 双A面「A/B（…）」写法

    def _variants():
        for t in _title_variants(title):
            anchor = _LQUOTE + _flex_title(t) + _RQUOTE + bridge
            strict = (anchor + r"[\uff08(]\s*(" + _KANA_READ + r")\s*([)\uff09\u3001,\uff0c])")
            for m in re.finditer(strict, text):
                got = normalize_reading(m.group(1))
                if not got:
                    continue
                if m.group(2) in "\u3001,\uff0c":
                    return got
                tail = text[m.end(): m.end() + 10]
                if re.match(r"\s*[\u300d\u300f\u201d\"\']*\s*(\u306f|\u3001)", tail):
                    return got

    got = _variants()
    if got:
        return got

    # 信息框字段 / 読み仮名模板
    for pat in (r"\u8aad\u307f[\u4eee\u540d]?\s*[:：=]\s*([^\n\|\}]+)",
                r"\u3088\u307f[\u304c\u306a]?\s*[:：=]\s*([^\n\|\}]+)",
                r"\u3075\u308a\u304c\u306a\s*[:：=]\s*([^\n\|\}]+)",
                r"\u3088\u307f\u304c\u306a\s*[:：=]\s*([^\n\|\}]+)",
                r"\{\{\s*\u8aad\u307f\u4eee\u540d[^|\n]*\|[^|\n]*\|\s*([^\|\}\n]+)"):
        m = re.search(pat, text[:5000])
        if m:
            got = normalize_reading(m.group(1))
            if got:
                return got

    # 宽容兜底：读音后接 <ref> / 「とは」 / 半角逗号等（仍要求标题锚定）
    greedy = r"[\u3041-\u309f\u30a0-\u30ff\u30fc\u30fb\s\u3000]{1,40}"
    for t in _title_variants(title):
        relaxed = (_LQUOTE + _flex_title(t) + _RQUOTE + bridge +
                   r"[\uff08(]\s*(" + greedy + r")\s*([^\n]{0,24})")
        for m in re.finditer(relaxed, text):
            got = normalize_reading(m.group(1))
            after = m.group(2) or ""
            if not got or not after:
                continue
            if after[0] in "\u3001,\uff0c/":
                return got
            if after[0] == "<":
                return got
            if re.match(r"\s*[\u300d\u300f\u201d\"\'）)]*\s*(\u3068)?\u306f", after):
                return got
    return None


def disambig_targets(text: str, title: str) -> "list[str]":
    """从消歧义/概念页里找出「本名 (…の曲/曲/楽曲…)」链接（≤2 个，去重）。"""
    if not text:
        return []
    out: "list[str]" = []
    esc = re.escape(title.replace(" ", ""))
    loose = r"\s*".join(re.escape(c) for c in title.replace(" ", ""))
    for m in re.finditer(r"\[\[\s*(" + loose + r"[^\]\|]{2,60}?)(?:\|[^\]]*)?\]\]",
                         text[:20000]):
        target = re.sub(r"\s+", " ", m.group(1)).strip()
        if target == title or target in out:
            continue
        if ("\u306e\u66f2" in target or "(\u66f2)" in target or "\uff08\u66f2\uff09" in target
                or "\u697d\u66f2" in target or "\u30b7\u30f3\u30b0\u30eb" in target):
            out.append(target)
        if len(out) >= 2:
            break
    return out


def wiki_reading(title: str, cache: dict, save=None, *, refetch: bool = False
                 ) -> tuple[str, str]:
    """返回 (reading, source)，source ∈ {w, ''}；正文优先、摘要兜底，抓不到给空。"""
    key = "wiki:" + title
    entry = None if refetch else cache.get(key)
    if NO_FETCH[0]:
        entry = entry or {}
        got = extract_reading_from_wikitext(entry.get("wikitext", ""), title) or \
            extract_reading_from_summary(entry.get("summary", ""), title)
        return (got or ""), ("w" if got else "")
    if entry is None or len(entry.get("wikitext") or "") < 50:
        summary, wt = "", ""
        for attempt in range(3):
            try:
                summary = wiki_summary(title)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 2:
                    time.sleep(9.0 * (attempt + 1))
                    continue
                summary = ""
            except Exception:
                summary = ""
            time.sleep(0.4)
            wt = wiki_wikitext(title)
            if wt or summary:
                break
            time.sleep(3.0 * (attempt + 1))
        entry = {"summary": summary, "wikitext": wt[:60000]}
        cache[key] = entry
        if save is not None:
            try:
                save()
            except OSError:
                pass
        time.sleep(1.0)                     # 尊重 API 限流
    entry = entry or {}
    got = extract_reading_from_wikitext(entry.get("wikitext", ""), title) or \
        extract_reading_from_summary(entry.get("summary", ""), title)
    return (got or ""), ("w" if got else "")


# --------------------------------------------------------------------------- #
# 3) pycorrector → cjk_confusions / cjk_words
# --------------------------------------------------------------------------- #
def build_confusions(pinyin_txt: str, stroke_txt: str, common_txt: str) -> list:
    common = {l.strip() for l in common_txt.splitlines() if l.strip()}
    pairs: dict = {}

    def add(a: str, b: str, typ: str) -> None:
        if a == b or len(a) != 1 or len(b) != 1:
            return
        if a not in common or b not in common:
            return
        key = (a, b) if a <= b else (b, a)
        order = {"p": 0, "p2": 1, "s": 2}
        if key not in pairs or order[typ] < order[pairs[key]]:
            pairs[key] = typ

    for line in pinyin_txt.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        base = parts[0].strip()
        if len(base) != 1:
            continue
        for col, typ in ((1, "p"), (2, "p2")):
            if col < len(parts):
                for ch in parts[col].strip():
                    add(base, ch, typ)
    for line in stroke_txt.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        members = [p.strip() for p in line.split("\t") if p.strip()]
        if len(members) < 2 or len(members) > 16:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                add(a, b, "s")
    rows = sorted(pairs.items(),
                  key=lambda kv: ({"p": 0, "p2": 1, "s": 2}[kv[1]], kv[0][0], kv[0][1]))
    return [f"{a}\t{b}\t{t}" for (a, b), t in rows]


def build_cjk_words(word_freq_txt: str, extra: list) -> list:
    rows = []
    for line in word_freq_txt.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        w = parts[0]
        if not (2 <= len(w) <= 6) or not all(KANJI_RE.match(c) for c in w):
            continue
        try:
            count = int(parts[1])
        except ValueError:
            continue
        rows.append((count, w))
    best: dict = {}
    for c, w in rows:
        if w not in best or c > best[w]:
            best[w] = c
    rows = list(best.items())
    rows.sort(key=lambda kv: (-kv[1], kv[0]))
    words = [[w, c] for w, c in rows[:20000]]
    seen = {w for w, _c in words}
    for w in extra:
        if w and w not in seen and all(KANJI_RE.match(c) for c in w):
            words.append([w, 999999])     # 人工补充的曲名/高频词给最高权重（排序用）
            seen.add(w)
    return words


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.join(os.environ.get("TEMP", "."), "music-decryptor-jp-build"))
    ap.add_argument("--refetch", action="store_true",
                    help="忽略缓存、重新抓取所有条目正文（提取逻辑升级后用一次）")
    ap.add_argument("--no-fetch", action="store_true",
                    help="只用手头缓存离线出表（网络受限时收尾用）")
    args = ap.parse_args()
    NO_FETCH[0] = bool(args.no_fetch)
    cache_dir = args.cache
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    report: list[str] = []

    # ---- JMdict ----
    print("[1/4] JMdict ...")
    jmdict_path = os.path.join(cache_dir, "jmdict-eng.tgz")
    fetch(f"https://github.com/scriptin/jmdict-simplified/releases/download/{JMDICT_TAG}/jmdict-eng-{JMDICT_TAG}.json.tgz",
          jmdict_path)
    jmdict_cache = os.path.join(cache_dir, "jmdict.json")
    if os.path.exists(jmdict_cache) and os.path.getsize(jmdict_cache) > 1000:
        full = json.load(open(jmdict_cache, encoding="utf-8"))
    else:
        full = load_tgz_json(jmdict_path)
        json.dump(full, open(jmdict_cache, "w", encoding="utf-8"), ensure_ascii=False)
    table = build_word_table(full["words"])
    add_derived(table)
    print(f"  surfaces(all): {len(table)}")
    sizes = {}
    for mode in ("r1", "r2l2", "r2l3"):
        pruned = prune_word_table(table, mode)
        payload = "\n".join("\t".join([k] + v) for k, v in sorted(pruned.items()))
        sizes[mode] = (len(pruned), len(payload.encode("utf-8")), payload)
        print(f"  prune<{mode}>: {len(pruned)} surfaces, {sizes[mode][1] / 1024:.0f} KiB")
    chosen = "r2l3"
    for mode in ("r2l3", "r2l2", "r1"):
        if sizes[mode][1] <= 1_100_000:
            chosen = mode
            break
    print(f"  chosen prune: {chosen}")
    words_pruned = prune_word_table(table, chosen)
    words_payload = "\n".join("\t".join([k] + v) for k, v in sorted(words_pruned.items()))

    # ---- 维基读音（热门歌曲 / 歌手）----
    print("[2/4] ja.wikipedia 读音抓取 ...（有缓存则秒过；首次约数分钟）")
    wiki_cache_path = os.path.join(cache_dir, "wiki_cache.json")
    wiki_cache = json.load(open(wiki_cache_path, encoding="utf-8")) if os.path.exists(wiki_cache_path) else {}

    def _save_wiki() -> None:
        json.dump(wiki_cache, open(wiki_cache_path, "w", encoding="utf-8"), ensure_ascii=False)

    def collect(list_, manual, kind, allow_j):
        rows, misses = [], []
        for title in list_:
            if title in manual:
                rows.append(f"{title}\t{manual[title]}\tm")
                continue
            if re.fullmatch(r"[\u3041-\u309f\u30a0-\u30ff\u30fc]+", title):
                # 纯假名名称：读音 = 名称本身（确定性恒等，不是推断）
                rows.append(f"{title}\t{hira(title)}\tk")
                continue
            wtitle = TITLE_ALIASES.get(title, title)
            reading, src = wiki_reading(wtitle, wiki_cache, _save_wiki, refetch=args.refetch)
            if not reading:
                # 首段失败：先跟随消歧义页里的「本名 (…の曲)」链接，再试常规后缀
                extra_tries: "list[str]" = []
                entry = wiki_cache.get("wiki:" + wtitle) or {}
                for target in disambig_targets(entry.get("wikitext", ""), wtitle):
                    if target not in extra_tries:
                        extra_tries.append(target)
                suffixes = (" (曲)", " (楽曲)") if kind == "songs" else \
                           (" (歌手)", " (ミュージシャン)")
                for suffix in suffixes:
                    alt = f"{wtitle}{suffix}"
                    if alt not in extra_tries:
                        extra_tries.append(alt)
                for alt in extra_tries[:3]:
                    reading, src = wiki_reading(alt, wiki_cache, _save_wiki,
                                                refetch=args.refetch)
                    if reading:
                        break
            if not reading and allow_j:
                fb = simple_reading_of(title, words_pruned)
                if fb:
                    reading, src = fb, "j"
            if reading:
                rows.append(f"{title}\t{reading}\t{src}")
            else:
                misses.append(title)
        print(f"  {kind}: {len(rows)}/{len(list_)} 有读音；缺失: {misses}")
        report.append(f"{kind} missing: {misses}")
        return rows

    song_rows = collect(SONGS, MANUAL_SONGS, "songs", allow_j=True)
    artist_rows = collect(ARTISTS, MANUAL_ARTISTS, "artists", allow_j=False)
    json.dump(wiki_cache, open(wiki_cache_path, "w", encoding="utf-8"), ensure_ascii=False)

    # ---- pycorrector ----
    print("[3/4] pycorrector 混淆集/词频 ...")
    def pget(name: str) -> str:
        path = os.path.join(cache_dir, name.replace("/", "_"))
        fetch(f"{PYCORR}/{name}", path, timeout=120)
        return open(path, encoding="utf-8", errors="replace").read()

    pinyin_txt = pget("data/same_pinyin.txt")
    stroke_txt = pget("data/same_stroke.txt")
    common_txt = pget("data/common_char_set.txt")
    word_freq_txt = pget("data/word_freq.txt")
    conf_rows = build_confusions(pinyin_txt, stroke_txt, common_txt)
    print(f"  confusions: {len(conf_rows)} pairs")
    cjk_extra = ["夜曲", "晴天"] + [
        "后来", "童话", "演员", "泡沫", "传奇", "红豆", "浮夸", "海阔天空", "光辉岁月",
        "喜欢你", "告白气球", "稻香", "青花瓷", "小幸运", "那些年", "追光者", "起风了",
        "光年之外", "句号", "你的答案", "少年", "白月光", "红玫瑰", "十年", "好久不见",
        "富士山下", "爱情转移", "岁月神偷", "匆匆那年", "平凡之路", "倔强", "温柔",
        "知足", "星空", "天使", "洋葱", "宁夏", "勇气", "分手快乐", "可惜不是你",
        "隐形的翅膀", "梦里花", "不想长大", "中国话", "夜曲", "晴天", "菊花台", "千里之外",
        "青花", "东风破", "发如雪", "七里香", "简单爱", "说好不哭",
    ]
    cjk_words = build_cjk_words(word_freq_txt, cjk_extra)
    cjk_words_payload = "\n".join(f"{w}\t{c}" for w, c in cjk_words)
    print(f"  cjk_words: {len(cjk_words)} words")

    # ---- 写盘 ----
    print("[4/4] 写盘 ...")
    header_words = ("# jp_words.tsv — 汉字词→假名读音（≤2），来源: JMdict EDRDG (CC BY-SA 4.0)\n"
                    f"# dict: jmdict-simplified {JMDICT_TAG}; prune: {chosen}\n")
    header_list = ("# 来源: ja.wikipedia (CC BY-SA 4.0) 抓取；j=JMdict 回退；m=人工核实。列: 名称<TAB>读音<TAB>src\n")
    path_words = os.path.join(DATA_DIR, "jp_words.tsv")
    open(path_words, "w", encoding="utf-8", newline="\n").write(
        header_words + words_payload + "\n")
    open(os.path.join(DATA_DIR, "jp_songs.tsv"), "w", encoding="utf-8", newline="\n").write(
        header_list + "\n".join(song_rows) + "\n")
    open(os.path.join(DATA_DIR, "jp_artists.tsv"), "w", encoding="utf-8", newline="\n").write(
        header_list + "\n".join(artist_rows) + "\n")
    open(os.path.join(DATA_DIR, "cjk_confusions.tsv"), "w", encoding="utf-8", newline="\n").write(
        "# 来源: pycorrector (Apache-2.0) same_pinyin/same_stroke，按 common_char_set 过滤\n"
        + "\n".join(conf_rows) + "\n")
    open(os.path.join(DATA_DIR, "cjk_words.txt"), "w", encoding="utf-8", newline="\n").write(
        cjk_words_payload + "\n")
    sources_md = (
        "# 内置表来源与许可\n\n"
        "本目录下的数据文件由 `tools/build_jp_tables.py` 联网构建、可复跑，运行时只读本地文件。\n\n"
        "* `jp_words.tsv` — 派生自 **JMdict**（Electronic Dictionary Research and Development Group，"
        "via jmdict-simplified），许可 **CC BY-SA 4.0**。\n"
        "* `jp_songs.tsv` / `jp_artists.tsv` — 读音抓取自 **日语维基百科**（CC BY-SA 4.0，API 首段/信息框）；"
        "少量条目使用 JMdict 回退读音。罫线：只收录事实性读音，不收录歌词等受版权保护的文本。\n"
        "* `cjk_confusions.tsv` / `cjk_words.txt` — 派生自 **pycorrector**（Apache-2.0）的"
        "`same_pinyin` / `same_stroke` / `word_freq` / `common_char_set` 数据文件。\n"
    )
    open(os.path.join(DATA_DIR, "SOURCES.md"), "w", encoding="utf-8", newline="\n").write(sources_md)
    for name in ("jp_words.tsv", "jp_songs.tsv", "jp_artists.tsv", "cjk_confusions.tsv",
                 "cjk_words.txt", "SOURCES.md"):
        p = os.path.join(DATA_DIR, name)
        print(f"  {name:24s} {os.path.getsize(p) / 1024:8.1f} KiB")
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

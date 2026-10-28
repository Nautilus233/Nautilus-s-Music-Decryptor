"""按专辑找歌：专辑检索 + 曲目清单（纯标准库；下载仍走 `Pipeline.download_chosen`）。

数据来源（全部 2026-10-22 联网实测）：
* **咪咕**：`search_all.do`（album:1 检索专辑实体）＋**推导式曲目清单**——
  用歌曲搜索按 `albums[].id` 归集曲目；`resourceinfo.do` 取标称曲目数用于对照。
  为什么"推导"：官方专辑曲目端点已下线（"路由请求不支持"）；搜索侧实测可恢复
  专辑的**大部分**曲目（受限曲目不在搜索结果里，日志会如实写"已恢复 X/Y 首"）。
* **酷狗**：`mobileservice.kugou.com` /api/v3/search/album + /api/v3/album/song
  （https 可用；mobilecdn 主机证书异常，刻意避开）。
* **酷我**：`search.kuwo.cn` r.s（ft=album 与 stype=albuminfo，伪 JSON，复用 kuwo
  适配器的解析器）。
* **聚合网关**：不支持 `types=album`；改用"歌曲搜索结果按 `album` 字段分组"的推导
  路线（只收组成 ≥2 首的组，避免把单曲当专辑）。

红线与纪律：
* 零新增依赖、零新增开关；本模块**只做检索层**：下载、校验、完整度门禁、写盘全部
  复用既有 `Pipeline.download_chosen`（门槛一字不变）。
* 逐源尽力：某源失败/超时只影响它自己；每源请求数有硬上限（检索 ≤4、曲目推导 ≤6）。
* 日语适配（与单曲链路同源）：检索词带读音变体时追加一次变体检索；字段比对走
  `cross_similarity`（假名↔罗马音的确定性互通）——专辑名/歌手的模糊匹配与单曲一致。
* 曲目自带的"平台标注时长"（如有）在下载时作为完整度参考；咪咕搜索侧不提供时长，
  下载后仍走既有完整度门禁（测不出时如实标注）。
"""
from __future__ import annotations

import html
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Optional

from .. import jp
from ..errors import NetworkError
from ..match import ARTIST_UNKNOWN, artist_match_score, cross_similarity, similarity
from ..model import Track
from .aggregator import DEFAULT_BASE
from .album_alias import alias_artist_matches, local_album_alias, resolve_album_alias
from .album_text import alternate as alternate_hanzi, folded as folded_hanzi
from .kuwo import _FIELD, _best_bitrate, _split_items, _unescape

__all__ = ["AlbumHit", "AlbumGroup", "search_albums", "group_albums",
           "album_tracks", "group_tracks"]

TIMEOUT = 10.0
MAX_ALBUMS_PER_SOURCE = 8
MAX_TRACKS = 300
MAX_DERIVE_PAGES = 3              # 咪咕推导：每个检索词最多翻 3 页（每页 20 条）
#: 咪咕专辑搜索时补全"歌曲总数"的上限（resourceinfo.do 每张 1 次请求）
_MIGU_ENRICH_CAP = 6
_MAX_AGG_GROUPS = 3

#: 合并视图（界面不展示来源）：名字与检索词（含日语变体）的相似度地板
_ALBUM_NAME_FLOOR = 0.55
#: 参考歌单的专辑名相似度门槛：同歌手但不同专辑（精选集/加曲版）一律不许当参考
_REF_NAME_FLOOR = 0.9
#: 版本簇的标题集合包含度阈值（≥ 视为同一版本/同一张专辑）
_CLUSTER_OVERLAP = 0.9
#: 取曲目通道优先序（咪咕音源最全且有"参考歌单/补位"机制 → 先试；失败逐通道降级）
_FETCH_PREFERENCE = {"migu": 0, "kugou": 1, "kuwo": 2, "aggregator": 3}
#: 封面候选优先序：jpg 优先，聚合需要二次解析垫底，咪咕 webp 兜底
_COVER_PREFERENCE = {"kugou": 0, "kuwo": 1, "aggregator": 2, "migu": 3}
#: 来源可信度（只用于"原版置信度"排序与展示名选取的微小并列权重）
_SOURCE_TRUST = {"migu": 1.0, "kugou": 0.95, "kuwo": 0.90, "aggregator": 0.85}

Log = Optional[Callable[[str], None]]

_MIGU_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"),
    "Referer": "https://music.migu.cn/",
    "channel": "014021I",
    "subchannel": "014021I",
}


@dataclass
class AlbumHit:
    source: str
    album_id: str
    name: str
    artist: str = ""
    track_count: int = 0
    year: str = ""
    #: 别名/外文写法（咪咕 albumAliasName / 酷我 falbum）：参与检索词匹配，不展示
    alias: str = ""
    #: 封面直链（聚合没有直链，走 raw["_pic_id"]）
    cover_url: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        bits = [self.name or "（未命名专辑）"]
        if self.artist:
            bits.append(f"— {self.artist}")
        extra = []
        if self.track_count:
            extra.append(f"{self.track_count} 首")
        if self.year:
            extra.append(self.year)
        tail = f"（{'，'.join(extra)}）" if extra else ""
        return " ".join(bits) + tail


def _note(log: Log, message: str) -> None:
    if log is None:
        return
    try:
        log(message)
    except Exception:  # noqa: BLE001 - 日志回调绝不拖垮检索
        pass


def _clean(text: str) -> str:
    """字段清洗：解码 HTML 实体（&apos; / &amp; / &nbsp; 等）并去首尾空白。

    2026-10-23 实测：酷我曲名原样带 &apos;（Don&apos;t You Remember），既让同一首在
    去重时对不上（虚增曲目），又会在界面/文件名里留下难看的实体文本。
    """
    return html.unescape(text or "").replace("\xa0", " ").strip()


def _sim(a: str, b: str) -> float:
    """字段相似度（日语形态增强）：与单曲链路的 cross_similarity 同一套。"""
    try:
        return max(cross_similarity(a or "", b or ""),
                   similarity(folded_hanzi(a), folded_hanzi(b)))
    except Exception:  # noqa: BLE001 - 比对绝不能炸
        return similarity(a or "", b or "")


def _norm_key(text: str) -> str:
    t = unicodedata.normalize("NFKC", folded_hanzi(_clean(text))).casefold()
    return re.sub(r"\s+", " ", t).strip()


def _title_prefix(query: str, title: str) -> bool:
    q, name = _norm_key(query), _norm_key(title)
    minimum = 2 if re.search(r"[\u3400-\u9fff]", q) else 4
    return len(q) >= minimum and name != q and name.startswith(q)


_EDITION_MARK = re.compile(
    r"\b(deluxe|expanded|anniversary|bonus|special|piano|instrumental|live|cover)\b"
    r"|豪華|豪华|加長|加长|紀念|纪念|現場|现场|鋼琴|钢琴|伴奏", re.I)


def _edition(name: str) -> tuple[str, ...]:
    markers = {m.group().casefold() for m in _EDITION_MARK.finditer(name or "")}
    if re.search(r"taylor[’']s\s+version", name or "", re.I):
        markers.add("taylor's version")
    return tuple(sorted(markers))


def _alias_matches(hit: AlbumHit, alias, query: str = "") -> bool:
    """An album entity alias cannot certify a deluxe/live/cover edition."""
    if _edition(hit.name) or not alias_artist_matches(hit.artist, alias.artists):
        return False
    for value in (hit.name, hit.alias):
        if not value or _edition(value):
            continue
        names = [value]
        core = value
        for _ in range(4):
            next_core = _TRACK_BRACKETS.sub(" ", core).strip()
            if next_core == core:
                break
            core = next_core
        names.append(core)
        if any(_norm_key(n) == _norm_key(t) for n in names
               for t in (*alias.titles, "" if alias.prefix else query) if t):
            return True
    return False


def _query_texts(query: str, limit: int = 2) -> "list[str]":
    """检索文本：原样 + 首个日语读音变体（罗马音→假名），与单曲链路同源的确定性转换。

    已含假名的输入**不追加**读音形 —— 实测四平台都按"书写形"索引，读音形既不命中
    又要多打一轮请求（速度优先；单体量修正见 DESIGN §3.11 修订）。
    """
    q = _clean(query)
    out = [q] if q else []
    if q and limit > 1 and not (jp.has_kana(q) and jp.has_kanji(q)):
        try:
            for value, _reason in jp.search_variants(q)[: limit - 1]:
                value = _clean(str(value))
                if value and value not in out:
                    out.append(value)
        except Exception:  # noqa: BLE001 - 变体生成失败绝不拖垮检索
            pass
    return out


def _text_forms(title: str, artist: str = "", limit: int = 2) -> "list[str]":
    """"曲名 [歌手]"检索文本的形态：原样 + 首个读音变体（标题优先、歌手次之）。"""
    title, artist = _clean(title), _clean(artist)
    base = f"{title} {artist}".strip()
    out = [base] if base else []
    if limit > 1:
        try:
            for first, second in ((title, artist), (artist, title)):
                if not first or (jp.has_kana(first) and jp.has_kanji(first)):
                    continue
                for value, _reason in jp.search_variants(first)[:1]:
                    parts = f"{_clean(str(value))} {second}".strip()
                    if parts and parts not in out:
                        out.append(parts)
                if len(out) >= limit:
                    break
        except Exception:  # noqa: BLE001
            pass
    return out[:limit]


def _hit_covers(it: dict, source: str) -> str:
    """来源条目 → 封面直链（jpg/png 优先；聚合没有直链，走 raw["_pic_id"]）。"""
    if source == "migu":
        items = it.get("imgItems")
        if isinstance(items, list) and items:
            order = {"03": 0, "02": 1, "01": 2}
            usable = [x for x in items if isinstance(x, dict)
                      and (x.get("img") or x.get("webpImg"))]
            if usable:
                pick = min(usable, key=lambda x: order.get(str(x.get("imgSizeType") or ""), 9))
                return _clean(str(pick.get("img") or pick.get("webpImg") or ""))
        return ""
    if source == "kugou":
        url = _clean(str(it.get("imgurl") or ""))
        return url.replace("{size}", "480") if url else ""
    if source == "kuwo":
        return _clean(str(it.get("img") or it.get("hts_img") or ""))
    return ""


def _artist_matches(want: str, cand: str) -> bool:
    want_s, cand_s = _clean(want), _clean(cand)
    if not want_s or not cand_s:
        return False
    if _norm_key(cand_s) in ARTIST_UNKNOWN:
        return False
    return artist_match_score([("", want_s)], Track("", artist=cand_s)) >= 0.6


def _join_artists(value) -> str:
    if isinstance(value, list):
        names = []
        for entry in value:
            if isinstance(entry, dict):
                name = _clean(str(entry.get("name") or ""))
            else:
                name = _clean(str(entry))
            if name and name not in names:
                names.append(name)
        return " / ".join(names)
    return _clean(str(value or ""))


# --------------------------------------------------------------------------- #
# 咪咕（专辑实体 + 推导式曲目）
# --------------------------------------------------------------------------- #
def _migu_search(http, query: str, limit: int) -> "list[AlbumHit]":
    import json as _json

    url = ("https://app.c.nf.migu.cn/MIGUM2.0/v1.0/content/search_all.do?" + urllib.parse.urlencode({
        "isCorrect": "1", "isCopyright": "1", "pageNo": "1", "pageSize": str(max(1, limit)),
        "searchSwitch": _json.dumps({"song": 0, "album": 1, "singer": 0}),
        "text": query, "sort": "1", "ua": "Android_migu", "version": "6.8.8",
    }))
    data = http.get_json(url, headers=_MIGU_HEADERS, timeout=TIMEOUT)
    result = ((data or {}).get("albumResultData") or {}).get("result") or []
    out: "list[AlbumHit]" = []
    for it in result:
        album_id = str(it.get("id") or "").strip()
        if not album_id:
            continue
        published = _clean(str(it.get("publishDate") or ""))
        out.append(AlbumHit(
            source="migu", album_id=album_id,
            name=_clean(str(it.get("name") or "")),
            artist=_clean(str(it.get("singer") or "")),
            track_count=0,
            year=published[:4] if len(published) >= 4 else "",
            alias=_clean(str(it.get("albumAliasName") or "")),
            cover_url=_hit_covers(it, "migu"),
            raw=dict(it),
        ))
    # 「歌曲总数」补全（搜索接口不带；resourceinfo.do 有）：前 N 张各一次请求，失败留空
    for hit in out[:_MIGU_ENRICH_CAP]:
        total = _migu_total(http, hit.album_id)
        if total:
            hit.track_count = total
    return out


def _migu_total(http, album_id: str) -> int:
    """resourceinfo.do：专辑标称曲目数（用于"已恢复 X/Y 首"对照）；失败返回 0。"""
    try:
        url = ("https://app.c.nf.migu.cn/MIGUM2.0/v1.0/content/resourceinfo.do"
               f"?resourceId={urllib.parse.quote(album_id)}&resourceType=2003")
        data = http.get_json(url, headers=_MIGU_HEADERS, timeout=TIMEOUT)
        res = ((data or {}).get("resource") or [{}])[0]
        return int(res.get("totalCount") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _migu_song_page(http, text: str, page: int) -> "list[dict]":
    import json as _json

    url = ("https://app.c.nf.migu.cn/MIGUM2.0/v1.0/content/search_all.do?" + urllib.parse.urlencode({
        "isCorrect": "1", "isCopyright": "1", "pageNo": str(page), "pageSize": "20",
        "searchSwitch": _json.dumps({"song": 1, "album": 0, "singer": 0}),
        "text": text, "sort": "1", "ua": "Android_migu", "version": "6.8.8",
    }))
    data = http.get_json(url, headers=_MIGU_HEADERS, timeout=TIMEOUT)
    return ((data or {}).get("songResultData") or {}).get("result") or []


def _gate_artist(album_artist: str, track_artist: str) -> bool:
    """列表侧强约束：曲目歌手必须与专辑原唱一致（信息缺失时无法判定 → 放行）。"""
    album, track = _clean(album_artist), _clean(track_artist)
    if not album or not track:
        return True
    if album.casefold() in ARTIST_UNKNOWN:
        return True
    return _artist_matches(album, track)


def _item_track(item: dict, ref: "dict | None", hit: AlbumHit, source: str, note: str) -> Track:
    """来源条目 → Track；有参考清单时用参考的曲名/歌手/时长做**字段校正**。

    同名不同盘（补位搜到的是别的合辑版本等）也一律改写为「本专辑名 + 参考歌单的
    曲名/歌手」——写盘时即进入文件标签与文件名（用户 2026-10-22/23 要求）。
    """
    raw = dict(item)
    if note:
        raw["_album_note"] = note
    title = (ref or {}).get("title") or _clean(str(item.get("name") or ""))
    artist = (ref or {}).get("artist") or _join_artists(item.get("singers") or item.get("artist"))
    duration = int((ref or {}).get("duration") or 0)
    return Track(source=source, title=title, artist=artist, album=hit.name,
                 duration=duration, raw=raw)


# --------------------------------------------------------------------------- #
# 曲目表装配（多源并集）—— 2026-10-23 用户实测后的**范式重构**
#
# 旧范式（"某源字段=权威 + 其它源当参考、不符即弃"）的三个真实事故（地毯式真源实测）：
#   · 《盗作》官方 14 首：酷狗/酷我都是完整 14，咪咕是另一个 11 首英文版 →
#     预展示 1 首（目录计数众数并列时"取小"）、点击后 11 首（把 14 首的参考"不符"丢弃）；
#   · 《Racine carrée》官方 13：kugou 完整 13、migu 只 10 → 参考被弃后只剩 migu 的 4 首；
#   · 《1989》《Abbey Road》：来源里全是版本（Deluxe / Taylor's Version / Super Deluxe /
#     Radio Release Special 的"评论对"）—— 按"曲目数最多"会选到 26/40 首的版本。
#
# 新范式（**不丢曲目优先**）：
#   1. 每个来源只保留一个**最佳实体**（名字最干净 → 曲目最多）：歌级 1 首条目被压掉；
#   2. 各来源的曲目表**全部取回**（搜索期已过同名同歌手门槛，逐曲再过歌手门槛）；
#   3. 按**标题集合包含度**聚成"版本"，只保留占优版本（不同语译版/别版绝不相混）；
#   4. 同版本内做**并集**：主干 = 最长表（保序），其余表补缺；去重键 = 去括号说明的标题
#      （「昼鳶 (白日窃贼)」≡「昼鳶」；「Welcome To New York (Commentary)」≡ 正曲）；
#      → **任何通过校验的来源里有的曲子都必须在结果里**（"不丢曲目"的硬保证）；
#   5. 明显偏大的孤例（合辑/附赠版）按中位数参照排除；
#   6. 聚合通道只在**目录来源没有该专辑时**作为列表回退（其"归集数"永不作准）。
# --------------------------------------------------------------------------- #
_TRACK_BRACKETS = re.compile(r"[\[(（【][^\[\]()（）【】]*[\]）)】]")


#: 指向"另一条录音/另一个版本"的括号标记 —— 这些括号**不能**当说明剥掉（2026-10-23 实测：
#: 剥掉会把「ON (Feat. Sia)」并进「ON」，《MAP OF THE SOUL : 7》直接少一首）。
#: **按分词匹配**：平台译文括号里常出现 with/live 等词（「雨とカプチーノ (Rain with Cappuccino)」），
#: 整串子串匹配会误判 —— 只在括号内容**分词后**命中标记词才算"另一条录音"（with 已剔除）。
_DIFF_MARK_WORDS = frozenset({
    "feat", "featuring", "ft", "ver", "version", "remix", "remixes",
    "instrumental", "inst", "acoustic", "live", "demo", "memo", "voice", "cover",
    "伴奏", "合唱", "现场", "翻唱", "纯音乐", "演奏", "铃声", "片段",
})
_DIFF_SPLIT = re.compile(r"[^0-9A-Za-z\u3041-\u30ff\u4e00-\u9fff]+")


def _has_diff_mark(bracket: str) -> bool:
    """括号内容是否指向"另一条录音"（分词后命中标记词）。"""
    tokens = {tok.casefold() for tok in _DIFF_SPLIT.split(bracket or "") if tok}
    return bool(tokens & _DIFF_MARK_WORDS or
                ("mix" in tokens and bool(tokens & {
                    "prologue", "urban", "ballad", "alternative"})))


def _split_brackets(title: str) -> "tuple[str, list[str]]":
    """(去掉全部括号的核心, 括号内容列表) —— NFKC 归一后切分。"""
    t = unicodedata.normalize("NFKC", title or "")
    parts = _TRACK_BRACKETS.findall(t)
    core = _TRACK_BRACKETS.sub(" ", t)
    return core, [p.strip("[]()（）【】 ").strip() for p in parts]


def _fold(text: str) -> str:
    """比对用折叠：NFKD 去变音符（AQUÍ ≡ AQUI）+ 折叠大小写（2026-10-23 实测修复虚增）。"""
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.casefold()


def _track_key(title: str) -> str:
    """曲目去重键：折叠（变音符/大小写）+ 去**说明性**括号 + 去标点。

    但**保留** feat/version/live/demo/voice 等"另一条录音"的括号内容 —— 否则会把不同
    曲目误并（实测：《MAP OF THE SOUL : 7》的「ON (Feat. Sia)」被并进「ON」）。
    """
    core, brackets = _split_brackets(title)
    extra = "".join("|" + re.sub(r"\W+", "", _fold(b)) for b in brackets if _has_diff_mark(b))
    core_key = re.sub(r"[\s\.,·、，;；:：\-–—_~!！?？'\"]+", "", _fold(core))
    return core_key + extra


def _title_clean(title: str) -> str:
    """只去掉**说明性**括号并压空白：用于比较"哪个名字更干净"（不改变实际写入的标题）。"""
    core, brackets = _split_brackets(title)
    keep = [b for b in brackets if _has_diff_mark(b)]
    text = core + ((" " + " ".join(keep)) if keep else "")
    return re.sub(r"\s+", " ", text).strip()


def _title_rank(title: str) -> "tuple[int, int]":
    """候选名字的"干净度"排序键（越小越干净）：先看有没有**有意义的**括号（另一条录音），
    再看原名字长度 —— 平台常在同名曲上挂译文括号（「花に亡霊 (Ghost In A Flower)」），
    去重时应保留**官方干净名**（2026-10-23 实测）。"""
    _core, brackets = _split_brackets(title)
    has_mark = 1 if any(_has_diff_mark(b) for b in brackets) else 0
    return (has_mark, len(re.sub(r"\s+", " ", title or "").strip()))


def _name_rank(name: str) -> "tuple[int, int]":
    """名字"干净度"排序键：括号组越少、名字越短 → 越接近用户输入的专辑名。

    用户搜「1989」要的是正片，不是「(Big Machine Radio Release Special)」；搜「Abbey Road」
    要的是专辑，不是「(Super Deluxe Edition)」。**首选干净名，同级再比曲目数**。
    """
    n = unicodedata.normalize("NFKC", name or "")
    return (len(_TRACK_BRACKETS.findall(n)), len(n))


def _source_best(hits) -> "dict[str, AlbumHit]":
    """每个来源只留一个最佳实体：名字更干净优先，同级比曲目数（歌级 1 首条目被压掉）。"""
    best: "dict[str, AlbumHit]" = {}
    def rank(h: AlbumHit):
        if (h.raw or {}).get("_verified_alias_id"):
            # These spellings identify the same album, not different editions.
            # Title length cannot make a 1-track translated entry beat 14 tracks.
            return (0, -(h.track_count or 0), _name_rank(h.name))
        return (1, _name_rank(h.name), -(h.track_count or 0))
    for h in hits:
        cur = best.get(h.source)
        if cur is None or rank(h) < rank(cur):
            best[h.source] = h
    return best


def _catalog_counts(variants) -> "list[int]":
    """目录型来源（咪咕/酷狗/酷我）**最佳实体**的曲目数（聚合的"归集数"是推导值，不计入）。"""
    best = _source_best(variants)
    return [int(h.track_count or 0) for src, h in best.items()
            if src in ("migu", "kugou", "kuwo") and (h.track_count or 0) > 0]


def _catalog_total(variants) -> int:
    """目录来源里**最完整**的声明数（各源最佳实体取最大）；没有则 0。

    为什么不是"众数"（2026-10-23 实测）：《盗作》官方 14 首，酷狗/酷我声明 14、咪咕只 11，
    还有两条歌级 1 首条目 —— 众数并列再"取小"直接错成 1 首。
    """
    counts = _catalog_counts(variants)
    return max(counts) if counts else 0


def _display_count(variants) -> int:
    """预展示曲目数：目录最完整值；没有目录来源时才退回聚合归集数（界面会标注「归集」）。"""
    total = _catalog_total(variants)
    if total:
        return total
    return max((int(h.track_count or 0) for h in _source_best(variants).values()), default=0)


def _migu_derive(http, hit: AlbumHit, log: Log) -> "list[Track]":
    """咪咕**专辑内推导**（歌曲搜索按 ``albums[].id`` 归集）—— 并集范式里的咪咕一路。

    日文/欧美专辑在咪咕常是"歌级条目"或另一个语译版：推导结果可能很少、标题也不同，
    交给版本聚类处理 —— **不再假设咪咕的字段权威**（这正是旧范式最大的错）。
    """
    total = _migu_total(http, hit.album_id)
    texts = [t for t in (f"{hit.name} {hit.artist}".strip(), hit.name) if t]
    collected: "dict[str, dict]" = {}
    for text in texts:
        for page in range(1, MAX_DERIVE_PAGES + 1):
            try:
                songs = _migu_song_page(http, text, page)
            except Exception as exc:  # noqa: BLE001 - 推导失败给空，不抛整链
                _note(log, f"  · [migu] 推导页请求失败：{exc}")
                break
            new = 0
            for s in songs:
                albums = s.get("albums") or []
                if not any(str(a.get("id") or "") == hit.album_id for a in albums):
                    continue
                key = str(s.get("contentId") or s.get("id") or "")
                if key and key not in collected:
                    collected[key] = s
                    new += 1
            if not songs or new == 0:
                break
            if total and len(collected) >= total:
                break
        if total and len(collected) >= total:
            break
    return [_item_track(s, None, hit, "migu", "") for s in collected.values()]


def _dedupe_tracks(tracks: "list[Track]", hit: AlbumHit, log: Log) -> "list[Track]":
    """单源曲目表：过**逐曲歌手门槛** + 按去括号标题去重（保留更干净的名字），如实写日志。"""
    out: "list[Track]" = []
    index: "dict[str, Track]" = {}
    dropped_artist = dupes = 0
    for t in tracks:
        if not t.title:
            continue
        if not _gate_artist(hit.artist, t.artist):
            dropped_artist += 1
            continue
        key = _track_key(t.title)
        if not key:
            continue
        cur = index.get(key)
        if cur is None:
            index[key] = t
            out.append(t)
            continue
        dupes += 1
        if _title_rank(t.title) < _title_rank(cur.title):
            cur.title = t.title
        if not cur.duration and t.duration:
            cur.duration = t.duration
    if dropped_artist:
        _note(log, f"  · [{hit.source}] 已剔除 {dropped_artist} 首歌手与专辑原唱不符的曲目")
    if dupes:
        _note(log, f"  · [{hit.source}] 已去掉 {dupes} 首重复收录（去括号标题相同）")
    return out[:MAX_TRACKS]


def _fetch_source_lists(http, best: "dict[str, AlbumHit]", *, log: Log = None, settings=None,
                        allow_aggregator: bool = True
                        ) -> "list[tuple[AlbumHit, list[Track]]]":
    """逐来源取回曲目表（单源失败只记明细、不抛；返回 [(实体, 已去重曲目表)]）。"""
    out: "list[tuple[AlbumHit, list[Track]]]" = []
    for src in ("migu", "kugou", "kuwo", "aggregator"):
        hit = best.get(src)
        if hit is None:
            continue
        if src == "aggregator" and not allow_aggregator:
            _note(log, "  · [aggregator] 已有目录来源，其归集表不作曲目来源")
            continue
        try:
            if src == "migu":
                tracks = _migu_derive(http, hit, log)
            elif src == "kugou":
                tracks = _kugou_tracks(http, hit)
            elif src == "kuwo":
                tracks = _kuwo_tracks(http, hit)
            else:
                tracks = _aggregator_list(http, hit, settings, log)
        except Exception as exc:  # noqa: BLE001 - 单源失败只记明细
            _note(log, f"  · [{src}] 曲目表取用失败：{exc}")
            continue
        tracks = _dedupe_tracks(tracks, hit, log)
        if tracks:
            _note(log, f"  · [{src}] 曲目表 {len(tracks)} 首（{hit.name}）")
            out.append((hit, tracks))
    return out


def _dominant_cluster(lists: "list[tuple[AlbumHit, list[Track]]]", log: Log):
    """按标题集合包含度聚成版本，只保留占优版本（源数多 → 表长）。

    不同版本（咪咕的英文版《盗作》11 首 vs 官方日文版 14 首）标题零重叠 → 自然分簇，
    只留占优版本 —— **绝不把两种版本的曲目混成一张表**。
    """
    clusters: "list[list[tuple[AlbumHit, list[Track]]]]" = []
    for item in lists:
        keys = {_track_key(t.title) for t in item[1]}
        placed = False
        for cl in clusters:
            other = {_track_key(t.title) for _h, ts in cl for t in ts}
            inter = len(keys & other)
            # 阈值 0.9（2026-10-23 实测）：**部分英译版**（咪咕《エルマ》10/14 命中）必须在
            # 这里分开 —— 0.6 会把 4 首英译标题当成新曲混进并集（同曲算两首）。
            if inter and inter / max(1, min(len(keys), len(other))) >= _CLUSTER_OVERLAP:
                cl.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])
    expected = {int(h.raw.get("_verified_track_count") or 0) for h, _ts in lists
                if h.raw.get("_verified_track_count")}
    verified_count = next(iter(expected)) if len(expected) == 1 else 0
    def rank(cl):
        count = max(len(ts) for _h, ts in cl)
        # A verified release count can break a majority of incomplete translated
        # catalogs. It never fabricates or truncates a single track.
        agreement = -abs(count - verified_count) if verified_count else 0
        return (agreement, len(cl), count)
    clusters.sort(key=rank, reverse=True)
    for cl in clusters[1:]:
        _note(log, "  · 已忽略另一版本（标题集与本版本不同）：" + "，".join(
            f"{h.source}《{h.name}》{len(ts)} 首" for h, ts in cl))
    return clusters[0]


def _union_lists(cluster: "list[tuple[AlbumHit, list[Track]]]", group: "AlbumGroup", log: Log):
    """同版本内**并集**：主干 = 最长表（保序），其余表补缺；去重键 = 去括号标题。

    返回 ``(曲目表, 主干实体)``。这是"不丢曲目"的核心 —— 通过校验的任一来源里有的曲子
    都必须在结果里；明显偏大的孤例（合辑/附赠）按中位数参照排除。
    """
    counts = sorted(len(ts) for _h, ts in cluster)
    # 参照 = **上中位数**（偶数个来源取较大的中位）—— 2026-10-23 实测修复：
    # 取较小中位数会把"完整表"当"偏大"排除（《Racine carrée》只剩 4 首）。
    if not counts:
        ref = 0
    elif len(counts) % 2:
        ref = counts[len(counts) // 2]
    else:
        ref = max(counts[len(counts) // 2 - 1], counts[len(counts) // 2])
    keep: "list[tuple[AlbumHit, list[Track]]]" = []
    for h, ts in cluster:
        if len(counts) > 1 and len(ts) > ref + max(2, ref // 4):
            _note(log, f"  · [{h.source}] 曲目表 {len(ts)} 首明显偏大（参照 {ref} 首），"
                      "已按合辑/附赠版排除")
            continue
        keep.append((h, ts))
    if not keep:
        keep = cluster[:1]
    keep.sort(key=lambda item: (len(item[1]), -_FETCH_PREFERENCE.get(item[0].source, 9)),
              reverse=True)
    out: "list[Track]" = []
    index: "dict[str, Track]" = {}
    aligned = 0
    for list_index, (_h, ts) in enumerate(keep):
        # Build a one-to-one alignment against the current backbone before adding
        # any new titles. Equal-length catalogs often spell one track differently.
        aliases: "dict[str, Track]" = {}
        if list_index and len(ts) == len(out):
            current_keys = {_track_key(t.title) for t in out}
            incoming_keys = {_track_key(t.title) for t in ts}
            overlap = len(current_keys & incoming_keys)
            if overlap >= max(2, int(0.75 * len(ts))):
                unmatched_current = [(i, t) for i, t in enumerate(out)
                                     if _track_key(t.title) not in incoming_keys]
                unmatched_incoming = [(i, t) for i, t in enumerate(ts)
                                      if _track_key(t.title) not in current_keys]
                if len(unmatched_current) == len(unmatched_incoming):
                    current_durations = [int(t.duration or 0) for t in out]
                    incoming_durations = [int(t.duration or 0) for t in ts]
                    for (old_pos, old), (new_pos, new) in zip(
                            unmatched_current, unmatched_incoming):
                        duration = int(new.duration or 0)
                        old_marks = [_fold(b) for b in _split_brackets(old.title)[1]
                                     if _has_diff_mark(b)]
                        new_marks = [_fold(b) for b in _split_brackets(new.title)[1]
                                     if _has_diff_mark(b)]
                        if (old_pos == new_pos and duration > 0
                                and abs(duration - int(old.duration or 0)) <= 2
                                and old_marks == new_marks
                                and current_durations.count(int(old.duration)) == 1
                                and incoming_durations.count(duration) == 1):
                            aliases[_track_key(new.title)] = old
        for t in ts:
            key = _track_key(t.title)
            if not key:
                continue
            cur = index.get(key) or aliases.get(key)
            if cur is None:
                new = Track(source=t.source, title=t.title, artist=t.artist,
                            album=group.name or t.album, duration=t.duration,
                            bitrate=t.bitrate, ext=t.ext or "mp3", raw=dict(t.raw))
                index[key] = new
                out.append(new)
                continue
            if key in aliases:
                index[key] = cur
                aligned += 1
            # 同一首：保留更干净的名字；补时长；**能下的来源优先**（目录源 > 聚合）
            if _title_rank(t.title) < _title_rank(cur.title):
                cur.title = t.title
            if not cur.duration and t.duration:
                cur.duration = t.duration
            if cur.source == "aggregator" and t.source != "aggregator":
                cur.source = t.source
                cur.raw = dict(t.raw)
                cur.ext = t.ext or cur.ext
                cur.artist = cur.artist or t.artist
    if aligned:
        _note(log, f"  · 曲序与时长核对：{aligned} 首跨语言/异写标题合并为同一首")
    return out[:MAX_TRACKS], keep[0][0]


# --------------------------------------------------------------------------- #
# 酷狗
# --------------------------------------------------------------------------- #
def _kugou_search(http, query: str, limit: int) -> "list[AlbumHit]":
    url = ("https://mobileservice.kugou.com/api/v3/search/album?format=json"
           f"&keyword={urllib.parse.quote(query)}&page=1&pagesize={max(1, limit)}&showtype=1")
    data = http.get_json(url, headers={"Referer": "https://m.kugou.com/"}, timeout=TIMEOUT)
    info = ((data or {}).get("data") or {}).get("info") or []
    out: "list[AlbumHit]" = []
    for it in info:
        album_id = str(it.get("albumid") or "").strip()
        if not album_id:
            continue
        published = str(it.get("publishtime") or "")
        out.append(AlbumHit(
            source="kugou", album_id=album_id,
            name=_clean(str(it.get("albumname") or "")),
            artist=_clean(str(it.get("singername") or "")),
            track_count=int(it.get("songcount") or 0),
            year=published[:4] if len(published) >= 4 else "",
            cover_url=_hit_covers(it, "kugou"),
            raw=dict(it),
        ))
    return out


def _kugou_tracks(http, hit: AlbumHit) -> "list[Track]":
    url = ("https://mobileservice.kugou.com/api/v3/album/song?format=json"
           f"&albumid={urllib.parse.quote(str(hit.album_id))}&page=1&pagesize=-1&version=9108")
    data = http.get_json(url, headers={"Referer": "https://m.kugou.com/"}, timeout=TIMEOUT)
    songs = ((data or {}).get("data") or {}).get("info") or []
    tracks: "list[Track]" = []
    for s in songs[:MAX_TRACKS]:
        artist = _clean(str(s.get("singername") or ""))
        title = _clean(str(s.get("songname") or ""))
        filename = _clean(str(s.get("filename") or ""))
        if " - " in filename:
            f_artist, f_title = filename.split(" - ", 1)
            artist = artist or f_artist.strip()
            title = title or f_title.strip()
        else:
            title = title or filename
        if not title:
            continue
        tracks.append(Track(
            source="kugou", title=title, artist=artist, album=hit.name,
            duration=int(s.get("duration") or 0),
            raw={
                "FileHash": s.get("hash") or "",
                "HQFileHash": s.get("hqhash") or "",
                "SQFileHash": s.get("sqhash") or "",
                "filename": filename,
            },
        ))
    return tracks


# --------------------------------------------------------------------------- #
# 酷我
# --------------------------------------------------------------------------- #
def _kuwo_search(http, query: str, limit: int) -> "list[AlbumHit]":
    url = ("https://search.kuwo.cn/r.s?all=" + urllib.parse.quote(query) +
           f"&ft=album&itemset=web_2013&client=kt&pn=0&rn={max(1, limit)}&rformat=json&encoding=utf8")
    status, body, _ = http.request("GET", url)
    if status >= 400:
        raise NetworkError(f"酷我专辑搜索 HTTP {status}", detail=url)
    text = body.decode("utf-8", "replace")
    out: "list[AlbumHit]" = []
    for chunk in _split_items(text, "albumlist"):
        item = {m.group(1): _unescape(m.group(2)) for m in _FIELD.finditer(chunk)}
        album_id = _clean(str(item.get("albumid") or ""))
        if not album_id:
            continue
        name = _clean(str(item.get("name") or item.get("albumname") or ""))
        artist = _clean(str(item.get("artist") or item.get("aartist") or ""))
        try:
            count = int(item.get("musiccnt") or 0)
        except ValueError:
            count = 0
        year = _clean(str(item.get("showtime") or item.get("pub") or ""))[:4]
        if year in ("0000", "0"):
            year = ""
        out.append(AlbumHit(source="kuwo", album_id=album_id, name=name, artist=artist,
                            track_count=count, year=year,
                            alias=_clean(str(item.get("falbum") or "")),
                            cover_url=_hit_covers(item, "kuwo"), raw=dict(item)))
    return out


def _kuwo_tracks(http, hit: AlbumHit) -> "list[Track]":
    url = ("https://search.kuwo.cn/r.s?stype=albuminfo&albumid=" +
           urllib.parse.quote(str(hit.album_id)) + "&client=kt&rformat=json&encoding=utf8")
    status, body, _ = http.request("GET", url, headers={"Referer": "https://www.kuwo.cn/"})
    if status >= 400:
        raise NetworkError(f"酷我专辑详情 HTTP {status}", detail=url)
    text = body.decode("utf-8", "replace")
    tracks: "list[Track]" = []
    for chunk in _split_items(text, "musiclist")[:MAX_TRACKS]:
        item = {m.group(1): _unescape(m.group(2)) for m in _FIELD.finditer(chunk)}
        rid = _clean(str(item.get("musicrid") or item.get("id") or ""))
        if not rid:
            continue
        if not rid.upper().startswith("MUSIC_"):
            rid = "MUSIC_" + rid
        title = _clean(str(item.get("name") or item.get("songname") or ""))
        if not title:
            continue
        tracks.append(Track(
            source="kuwo", title=title,
            artist=_clean(str(item.get("artist") or "")),
            album=_clean(str(item.get("album") or hit.name)),
            duration=int(item.get("duration") or 0),
            bitrate=_best_bitrate(str(item.get("MINFO") or ""), "mp3"),
            raw={"MUSICRID": rid, "MINFO": str(item.get("MINFO") or ""),
                 "NAME": title, "ARTIST": _clean(str(item.get("artist") or ""))},
        ))
    return tracks


# --------------------------------------------------------------------------- #
# 聚合网关（歌曲搜索按 album 字段分组推导）
# --------------------------------------------------------------------------- #
def _aggregator_base(settings) -> "tuple[str, str]":
    base = (getattr(settings, "aggregator_base", "") or "").strip() or DEFAULT_BASE
    provider = (getattr(settings, "aggregator_provider", "") or "").strip() or "netease"
    return base, provider


def _agg_dominant_artist(items: "list[dict]") -> str:
    """组内出现最多的歌手（作为"专辑原唱"代表；用于强歌手门槛）。"""
    counts: "dict[str, int]" = {}
    for it in items:
        artist = _join_artists(it.get("artist"))
        if artist:
            counts[artist] = counts.get(artist, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _aggregator_search(http, query: str, limit: int, settings) -> "list[AlbumHit]":
    base, provider = _aggregator_base(settings)
    url = (f"{base}?types=search&source={urllib.parse.quote(provider)}"
           f"&name={urllib.parse.quote(query)}&count=50&pages=1")
    data = http.get_json(url, timeout=TIMEOUT)
    if not isinstance(data, list):
        return []
    groups: "dict[str, list[dict]]" = {}
    for it in data:
        album = _clean(str(it.get("album") or ""))
        if album:
            groups.setdefault(album, []).append(it)
    out: "list[AlbumHit]" = []
    for album, items in groups.items():
        sim = _sim(album, query)
        if len(items) < 2 and sim < 0.95:
            continue  # 单曲组不算专辑（除非名字几乎完全一致）
        if sim < 0.6:
            continue
        raw = {"items": items[:MAX_TRACKS], "_count_hint": "归集"}
        pic_id = ""
        for it in items:
            pid = str(it.get("pic_id") or "").strip()
            if pid:
                pic_id = pid
                break
        if pic_id:
            raw["_pic_id"] = pic_id
        out.append(AlbumHit(source="aggregator", album_id=album, name=album,
                            artist=_agg_dominant_artist(items), track_count=len(items),
                            raw=raw))
        if len(out) >= _MAX_AGG_GROUPS:
            break
    return out


def _aggregator_list(http, hit: AlbumHit, settings, log: Log) -> "list[Track]":
    """聚合的曲目表：分组条目（必要时重新归集一次）→ Track 列表。

    聚合**没有专辑实体** —— 它的"专辑"是按 `album` 字段从歌曲搜索里归集的推导结果：
    只在目录来源（咪咕/酷狗/酷我）没有该专辑时作为**列表回退**使用，且其"归集数"永不作准。
    """
    items = (hit.raw or {}).get("items") or []
    if not items:
        base, provider = _aggregator_base(settings)
        url = (f"{base}?types=search&source={urllib.parse.quote(provider)}"
               f"&name={urllib.parse.quote(hit.name)}&count=50&pages=1")
        try:
            data = http.get_json(url, timeout=TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            _note(log, f"  · [aggregator] 重新归集失败：{exc}")
            data = []
        items = [it for it in (data or []) if _clean(str(it.get("album") or "")) == hit.name]
    tracks: "list[Track]" = []
    for it in items:
        if not it.get("id"):
            continue
        tracks.append(_item_track(it, None, hit, "aggregator", ""))
    if not hit.artist:
        hit.artist = _agg_dominant_artist(items)
    (hit.raw or {}).pop("_count_hint", None)
    return tracks


def search_albums(http, query: str, *, artist: str = "", per_source: int = MAX_ALBUMS_PER_SOURCE,
                  log: Log = None, sources=None, settings=None) -> "list[AlbumHit]":
    """跨源专辑检索（咪咕 → 酷狗 → 酷我 → 聚合）；可选歌手做双重限定。

    日语适配（与单曲链路同源）：检索词能生成读音变体时（罗马音↔假名/片假名），
    每个来源**追加一次变体检索**并把两轮结果合并去重 —— 纯罗马音/纯假名输入
    也能找回日文原名的专辑。

    ``sources``：可用音源名集合（None = 全部）。某源失败只记日志、不抛错。
    """
    q = _clean(query)
    if not q:
        return []
    texts = _query_texts(q)
    allowed = set(sources) if sources is not None else {"migu", "kugou", "kuwo", "aggregator"}
    hits: "list[AlbumHit]" = []
    searched: "dict[str, set[str]]" = {}
    known = local_album_alias(q, artist.strip())
    searches = (
        ("migu", lambda t: _migu_search(http, t, per_source)),
        ("kugou", lambda t: _kugou_search(http, t, per_source)),
        ("kuwo", lambda t: _kuwo_search(http, t, per_source)),
        ("aggregator", lambda t: _aggregator_search(http, t, per_source, settings)),
    )

    def _collect(name: str, fn, note: str) -> None:
        seen: "set[str]" = set()
        kept = 0
        for idx, text in enumerate(texts):
            searched.setdefault(name, set()).add(text)
            try:
                part = fn(text)
            except Exception as exc:  # noqa: BLE001 - 单源失败不影响其它源
                _note(log, f"  · [{name}] {note}不可用：{exc}")
                break
            fresh = [h for h in part if h.album_id and h.album_id not in seen]
            for h in fresh:
                seen.add(h.album_id)
            if idx and fresh:
                _note(log, f"  · [{name}] 命中于检索变体「{text}」（{len(fresh)} 张）")
            hits.extend(fresh)
            kept += len(fresh)
        if kept:
            _note(log, f"  · [{name}] 命中 {kept} 张专辑")

    for source, search in searches:
        if source in allowed and (source != "aggregator" or settings is not None):
            _collect(source, search, "专辑检索")
    if artist.strip():
        kept_hits = [h for h in hits if _artist_matches(artist, h.artist)
                     or (known is not None and _alias_matches(h, known, q))]
        dropped = len(hits) - len(kept_hits)
        if dropped:
            _note(log, f"  · 按歌手「{artist.strip()}」过滤掉 {dropped} 张（演唱者不符）")
        hits = kept_hits

    alias = known
    if alias is None and re.search(r"[\u3400-\u9fff]", q) and not any(
            _group_name_sim(h, [q]) >= 0.9 for h in hits):
        alias = resolve_album_alias(http, q, artist.strip())
    if alias is not None:
        for h in hits:
            if _alias_matches(h, alias, q):
                h.raw["_verified_query_alias"] = q
                h.raw["_verified_alias_id"] = alias.entity_id
                h.raw["_verified_original_title"] = alias.titles[0]
                if alias.track_count:
                    h.raw["_verified_track_count"] = alias.track_count

    # A partial result can reveal a full title even outside the curated index.
    # Expand only one unambiguous (title, artist) observed in source metadata;
    # this is a search hint, never an authoritative album/track-count claim.
    observed = {}
    if alias is None:
        for h in hits:
            if (_title_prefix(q, h.name) and not _edition(h.name) and h.artist
                    and h.artist.casefold() not in ARTIST_UNKNOWN):
                observed.setdefault((_norm_key(h.name), _norm_key(h.artist)), h)
    discovered = next(iter(observed.values())) if len(observed) == 1 else None

    # Search eligibility is per source: one short catalog or a same-name cover
    # from another source must not suppress an original-title catalog lookup.
    for source, search in searches:
        if source not in allowed or (source == "aggregator" and settings is None):
            continue
        own = [h for h in hits if h.source == source]
        verified = [h for h in hits if alias is not None
                    and (h.raw or {}).get("_verified_alias_id") == alias.entity_id]
        own_verified = [h for h in verified if h.source == source]
        target = max([h.track_count for h in verified if h.source != "aggregator"]
                     + ([alias.track_count] if alias is not None else [0]))
        if source == "aggregator" and any(
                h.source != "aggregator" and h.track_count >= max(2, target) for h in verified):
            continue  # Inferred search results add no directory authority here.
        if alias is not None:
            own_count = max((h.track_count for h in own_verified), default=0)
            # No extra request for a source already giving the most complete
            # known entity. Counts only schedule a lookup; they never add tracks.
            if own_verified and own_count >= max(2, target):
                continue
            candidates = list(alias.titles[:2])
        else:
            missing_title = discovered is not None and not any(
                _norm_key(h.name) == _norm_key(discovered.name)
                and _artist_matches(discovered.artist, h.artist) for h in own)
            if not missing_title and any(_group_name_sim(h, [q]) >= 0.9 for h in own):
                continue
            candidates = [discovered.name] if missing_title else [alternate_hanzi(q)]
        extra = 0
        for title in candidates:
            if not title or title in searched.get(source, set()):
                continue
            # At most two alias lookups, or one orthographic lookup, per source.
            extra += 1
            searched.setdefault(source, set()).add(title)
            try:
                part = search(title)
            except Exception as exc:  # noqa: BLE001 - optional lookup is best-effort
                _note(log, f"  · [{source}] 原名补查失败：{exc}")
                break
            accepted = []
            for h in part:
                if alias is not None:
                    if not _alias_matches(h, alias, q):
                        continue
                    h.raw["_verified_query_alias"] = q
                    h.raw["_verified_alias_id"] = alias.entity_id
                    h.raw["_verified_original_title"] = alias.titles[0]
                    if alias.track_count:
                        h.raw["_verified_track_count"] = alias.track_count
                elif (_group_name_sim(h, [q]) < 0.9
                      or (artist.strip() and not _artist_matches(artist, h.artist))
                      or (discovered is not None and title == discovered.name and
                          (_norm_key(h.name) != _norm_key(discovered.name)
                           or not _artist_matches(discovered.artist, h.artist)))):
                    continue
                accepted.append(h)
            index = {(h.source, h.album_id): h for h in hits}
            for h in accepted:
                existing = index.get((h.source, h.album_id))
                if existing is None:
                    hits.append(h)
                    index[(h.source, h.album_id)] = h
                else:
                    existing.raw.update({k: v for k, v in h.raw.items() if k.startswith("_verified_")})
            if accepted:
                _note(log, f"  · [{source}] 原名/字形补查「{title}」命中 {len(accepted)} 张")
                if alias is None or (target >= 2 and max(
                        (h.track_count for h in accepted), default=0) >= target):
                    break
            if extra >= (2 if alias is not None else 1):
                break
    return hits


def album_tracks(http, hit: AlbumHit, *, log: Log = None, settings=None) -> "list[Track]":
    """取**单个来源**的曲目表（去重 + 逐曲歌手门槛）。

    界面的"取曲目"走的是 :func:`group_tracks`（多源并集）—— 本函数保留给直接调用与测试。
    """
    if hit.source == "migu":
        tracks = _migu_derive(http, hit, log)
    elif hit.source == "kugou":
        tracks = _kugou_tracks(http, hit)
    elif hit.source == "kuwo":
        tracks = _kuwo_tracks(http, hit)
    elif hit.source == "aggregator":
        tracks = _aggregator_list(http, hit, settings, log)
    else:
        raise NetworkError(f"暂不支持该来源的专辑详情：{hit.source}")
    tracks = _dedupe_tracks(tracks, hit, log)
    hit.track_count = len(tracks)
    return tracks


# --------------------------------------------------------------------------- #
# 合并视图（跨源去源名展示 + 原版置信度排序 + 逐通道取曲目）
# --------------------------------------------------------------------------- #
@dataclass
class AlbumGroup:
    """合并视图的一张"专辑卡片"：同一 (专辑名, 歌手) 的跨源条目合一，界面**不展示来源**。

    ``variants`` 是内部数据（取曲目/封面时按通道优先序尝试）；``primary`` 是实际取到
    曲目的那一条（取完后回写）；``track_count`` 展示为"总曲目数"（取曲目后刷新为权威值）。
    """

    name: str = ""
    artist: str = ""
    track_count: int = 0
    year: str = ""
    count_hint: str = ""
    variants: "list[AlbumHit]" = field(default_factory=list)
    primary: "AlbumHit | None" = None

    def cover_candidates(self) -> "list[tuple[str, str]]":
        """封面候选（("url", url) / ("pic_id", id)）：jpg 优先、聚合垫底、去重保序。"""
        ranked = sorted(self.variants,
                        key=lambda h: (_COVER_PREFERENCE.get(h.source, 9), -(h.track_count or 0)))
        out: "list[tuple[str, str]]" = []
        seen: "set[str]" = set()
        for h in ranked:
            url = _clean(h.cover_url)
            if url and url not in seen:
                seen.add(url)
                out.append(("url", url))
        for h in ranked:
            pid = str((h.raw or {}).get("_pic_id") or "").strip()
            if pid and pid not in seen:
                seen.add(pid)
                out.append(("pic_id", pid))
        return out


def _group_name_sim(hit: AlbumHit, texts: "list[str]") -> float:
    best = 0.0
    for t in texts:
        best = max(best, _sim(t, hit.name))
        if _title_prefix(t, hit.name):
            best = max(best, 0.9)  # Recall a full title; do not certify its release.
        if hit.alias:
            best = max(best, _sim(t, hit.alias))
        verified = (hit.raw or {}).get("_verified_query_alias")
        if verified:
            best = max(best, _sim(t, verified))
    return best


def _decorated(name: str) -> bool:
    """名字带括号修饰（「(Cover)」「（所以…）」等）：只用于并列时的微小减权，不硬删。"""
    return bool(re.search(r"[（(\[【]", name or ""))


def _cluster_by_name(variants: "list[AlbumHit]") -> "list[list[AlbumHit]]":
    """同一歌手键内，把**近同名**条目并成一组（cross ≥ 0.95）。

    典型：`Lemon` / `レモン` / `レモン (柠檬)` 是同一张专辑的三种写法（跨源）。只按名字
    聚，不按歌手聚 —— 不同歌手的同名版本照样并列显示（用户要求的"其它歌手名"）。
    """
    clusters: "list[list[AlbumHit]]" = []
    for h in variants:
        for cl in clusters:
            entity_id = (h.raw or {}).get("_verified_alias_id")
            if any((_edition(h.name) == _edition(other.name) and
                    _sim(h.name, other.name) >= 0.95) or
                   (entity_id and entity_id == (other.raw or {}).get("_verified_alias_id"))
                   for other in cl):
                cl.append(h)
                break
        else:
            clusters.append([h])
    return clusters


def _pick_display(variants: "list[AlbumHit]", texts: "list[str]") -> AlbumHit:
    """展示用条目：干净原名（无括号修饰）优先，其次与检索词最像，再次来源可信度。"""
    def key(h: AlbumHit):
        original = (h.raw or {}).get("_verified_original_title")
        original_name = bool(original and _norm_key(original) == _norm_key(h.name))
        return (original_name, not _decorated(h.name), round(_group_name_sim(h, texts), 4),
                _SOURCE_TRUST.get(h.source, 0.5), h.track_count or 0)
    return max(variants, key=key)


def _group_score(group: "AlbumGroup", texts: "list[str]", artist: str) -> float:
    best_hit, name_sim = None, 0.0
    for h in group.variants:
        ns = _group_name_sim(h, texts)
        if ns > name_sim or (ns == name_sim and ns > 0 and not _decorated(h.name)):
            best_hit, name_sim = h, ns
    if artist:
        artist_sim = max((_sim(artist, h.artist) for h in group.variants if _clean(h.artist)),
                         default=0.0)
    else:
        artist_sim = 0.5
    trust = max((_SOURCE_TRUST.get(h.source, 0.5) for h in group.variants), default=0.5)
    completeness = min(group.track_count, 20) / 20.0
    # 干净原名（近满相似且无括号修饰）微加 0.03：同分并列时压过「(Cover)」类同形名
    clean_bonus = 0.03 if (best_hit is not None and name_sim >= 0.9
                           and not _decorated(best_hit.name)) else 0.0
    edition_cost = 0.02 * min((len(_edition(h.name)) for h in group.variants), default=0)
    return (0.55 * name_sim + 0.25 * artist_sim + 0.10 * trust + 0.10 * completeness
            + clean_bonus - edition_cost)


def group_albums(hits, *, query: str = "", artist: str = "",
                 log: Log = None) -> "list[AlbumGroup]":
    """跨源条目 → 合并视图（界面不展示来源）。

    * 同一 (专辑名, 歌手) 合并为一张卡片；**近同名跨写法**（Lemon/レモン/レモン (柠檬)）
      在同一歌手键内合一；无歌手信息的条目并入"同名且唯一歌手"的分组
      （来源没给歌手 ≠ 另一个歌手；同名存在多个歌手时保持独立，宁可不并、不并错）；
    * **原版置信度**排序：名字越像（含日语读音形态）、指定歌手越吻合、曲目越全、
      来源越可信 → 越靠前；其它歌手名/翻唱版**并列**显示在后；
    * 名字与检索词（含变体）都不像的条目剔除 —— 平台模糊检索会带出无关专辑；
    * ``AlbumGroup.name/artist`` 取组内最可信来源的写法（展示用；不展示来源本身）。
    """
    texts = [t for t in (_query_texts(query) or [_clean(query)]) if t]
    artist_q = _clean(artist)
    buckets: "dict[tuple[str, str], list[AlbumHit]]" = {}
    for h in hits:
        if not _clean(h.name):
            continue
        entity_id = (h.raw or {}).get("_verified_alias_id")
        artist_key = f"verified:{entity_id}" if entity_id else _norm_key(h.artist)
        buckets.setdefault((_norm_key(h.name), artist_key), []).append(h)
    by_name: "dict[str, set]" = {}
    for (name_key, artist_key) in buckets:
        if artist_key:
            by_name.setdefault(name_key, set()).add(artist_key)
    for key in list(buckets):
        name_key, artist_key = key
        if artist_key:
            continue
        artists_here = by_name.get(name_key) or set()
        if len(artists_here) == 1:
            dst = (name_key, next(iter(artists_here)))
            buckets.setdefault(dst, []).extend(buckets.pop(key))
    # 近同名跨写法聚类（同歌手键内）：Lemon/レモン/レモン (柠檬) → 同一张卡片
    by_artist: "dict[str, list[AlbumHit]]" = {}
    for (name_key, artist_key), variants in buckets.items():
        by_artist.setdefault(artist_key, []).extend(variants)
    merged_buckets: "list[list[AlbumHit]]" = []
    for variants in by_artist.values():
        merged_buckets.extend(_cluster_by_name(variants))

    groups: "list[AlbumGroup]" = []
    dropped = 0
    for variants in merged_buckets:
        if texts and max((_group_name_sim(h, texts) for h in variants), default=0.0) < _ALBUM_NAME_FLOOR:
            dropped += len(variants)
            continue
        ranked = sorted(variants, key=lambda h: (_SOURCE_TRUST.get(h.source, 0.5), h.track_count or 0),
                        reverse=True)
        display = _pick_display(variants, texts)
        top = max(variants, key=lambda h: h.track_count or 0)
        groups.append(AlbumGroup(
            name=_clean(display.name),
            artist=_clean(display.artist) or _clean(variants[0].artist),
            track_count=_display_count(variants),
            year=next((h.year for h in ranked if h.year), ""),
            count_hint=str((top.raw or {}).get("_count_hint") or ""),
            variants=variants,
        ))
    groups.sort(key=lambda g: _group_score(g, texts, artist_q), reverse=True)
    if hits:
        _note(log, f"  · 合并视图：{len(hits)} 个来源条目 → {len(groups)} 张专辑"
                  f"（已剔除 {dropped} 个无关条目）")
    return groups


def group_tracks(http, group: AlbumGroup, *, log: Log = None, settings=None) -> "list[Track]":
    """合并已取回的同版目录；源端缺失时如实报告，不能据并集证明发行版完整。

    1. 每个来源取"最佳实体"（名字最干净 → 曲目最多），歌级 1 首条目自然被压掉；
    2. 各来源曲目表**全部取回**，按标题集合聚成版本，只保留占优版本（语译版/别版不相混）；
    3. 同版本内并集：主干保序、其余补缺 —— 任何通过校验的来源里有的曲子都会出现；
    4. 聚合通道仅在**目录来源没有该专辑**时作列表回退（其归集数不作准）；
    5. 全部来源都取不到时抛出可读错误（界面统一提示）。

    取完后回写 ``group.primary``（主干来源）与 ``group.track_count``（= 实际列表长度）。
    """
    best = _source_best(group.variants)
    has_catalog = any(src in best for src in ("migu", "kugou", "kuwo"))
    lists = _fetch_source_lists(http, best, log=log, settings=settings,
                                allow_aggregator=not has_catalog)
    if not lists and has_catalog and "aggregator" in best:
        # An unavailable catalog is not a usable catalog. Never mix inferred
        # results into an existing directory; allow fallback only when all failed.
        lists = _fetch_source_lists(http, {"aggregator": best["aggregator"]},
                                    log=log, settings=settings)
    if not lists:
        raise NetworkError("该专辑在全部可用通道都未能取到曲目，请稍后再试")
    cluster = _dominant_cluster(lists, log)
    merged, backbone = _union_lists(cluster, group, log)
    if not merged:
        raise NetworkError("该专辑的曲目表为空，请稍后再试")
    group.primary = backbone
    group.track_count = len(merged)                 # 列表长度与展示计数永远一致
    group.count_hint = ""
    declared = max([h.track_count for h, _tracks in cluster if h.source != "aggregator"]
                   + [int(h.raw.get("_verified_track_count") or 0) for h, _tracks in cluster],
                   default=0)
    if declared > len(merged):
        _note(log, f"  · 曲目目录未完整：所选目录标称 {declared} 首，实际获取 {len(merged)} 首；"
                  "未用其它专辑或同名资源凑数")
    _note(log, f"  · 并集装配：{len(lists)} 个来源 → {len(merged)} 首"
              f"（主干 {backbone.source} · 版本内不丢曲目）")
    return merged

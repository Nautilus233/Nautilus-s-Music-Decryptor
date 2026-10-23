"""专辑板块矩阵测试（离线矩阵 + 可选 --live / --wide 真源矩阵）。

为什么存在（2026-10-23 用户实测，用户明确这是模块可用性的底线）：
1. 《盗作》官方 14 首：预展示 1 首、点击后 11 首 —— 旧范式把"某一个源的字段"当权威，
   还按"众数并列取小"算展示数；把酷狗/酷我的完整 14 首参考当"不符"丢弃；
2. 《乐行者》11 首一度显示/下载 32 首 —— 参考歌单按"同歌手里曲目最多"选中精选集；
3. 《Racine carrée》官方 13 首只剩 4 首、《El Mal Querer》虚增到 14 首、
   《MAP OF THE SOUL : 7》少 1 首 —— 去重键/参照口径的真实缺陷。

范式（**多源并集**，实现在 `mmusic/search/album.py`）：
  每个来源取最佳实体 → 各源曲目表全部取回 → 按标题集合聚"版本"（语译版/别版不相混）
  → 同版本内并集（主干保序、其余补缺、去括号键去重）→ 计数 = 实际列表长度。
  硬保证：**任何通过校验的来源里有的曲子，都必须在结果里**（不丢曲目）；
  且**绝不超过通过校验的来源所能支撑的范围**（不膨胀）。

用法：
    python tests/test_album_matrix.py            # 离线矩阵（进常规门禁）
    python tests/test_album_matrix.py --live     # 附：精选真源断言（4 张）
    python tests/test_album_matrix.py --wide     # 附：多语种地毯式真源矩阵（23 张）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mmusic.config import Settings                                   # noqa: E402
from mmusic.model import Track                                       # noqa: E402
from mmusic.search.album import (AlbumHit, _clean, _display_count,   # noqa: E402
                                 _dominant_cluster, _query_texts, _source_best, _text_forms,
                                 _union_lists,
                                 _track_key, group_albums, group_tracks,
                                 search_albums)

PASSED: "list[str]" = []
FAILED: "list[str]" = []


def check(name: str, cond, extra: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   -> {extra}" if extra and not cond else ""))


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[\s\.,·、，（）()\[\]【】\-–—_]+", "", t)


def invariants(tracks, expected: int) -> "list[str]":
    """底线不变量：不重复、不为空、不等于"丢曲目"、不超过来源能支撑的范围。"""
    problems: "list[str]" = []
    titles = [t.title for t in tracks]
    if not titles or any(not t.strip() for t in titles):
        problems.append("空曲名")
    keys = [_track_key(t) for t in titles]
    if len(keys) != len(set(keys)):
        dup = sorted({k for k in keys if keys.count(k) > 1})
        problems.append(f"重复曲目 {dup[:3]}")
    if expected and len(tracks) < expected:
        problems.append(f"丢曲目 {len(tracks)} < {expected}")
    if expected and len(tracks) > expected + max(2, expected // 4):
        problems.append(f"膨胀 {len(tracks)} > {expected}")
    return problems


# --------------------------------------------------------------------------- #
# 伪造音源
# --------------------------------------------------------------------------- #
class FakeHttp:
    def __init__(self, routes):
        self.routes = routes

    def _payload(self, url: str):
        for match, payload in self.routes:
            try:
                hit = match(url) if callable(match) else (match in url)
            except Exception:  # noqa: BLE001
                hit = False
            if hit:
                return payload
        return {}

    def get_json(self, url, headers=None, timeout=None):
        return self._payload(url)

    def request(self, method, url, headers=None, timeout=None):
        payload = self._payload(url)
        if isinstance(payload, bytes):
            body = payload
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = json.dumps(payload).encode("utf-8")
        return 200, body, {}


def migu_album(aid, name, artist, year="2003"):
    return {"albumResultData": {"result": [
        {"id": aid, "name": name, "singer": artist, "publishDate": year}]}}


def migu_songs(items):
    return {"songResultData": {"result": [
        {"name": t, "contentId": cid, "singers": [{"name": a}],
         "albums": [{"id": aid, "name": "x"}]} for (t, a, aid, cid) in items]}}


def kugou_albums(items):
    return {"status": 1, "data": {"info": [
        {"albumid": aid, "albumname": n, "singername": a, "songcount": c,
         "publishtime": "2003-01-01 00:00:00"} for (aid, n, a, c) in items]}}


def kugou_songs(items):
    return {"status": 1, "data": {"info": [
        {"filename": f"{a} - {t}", "duration": d, "hash": f"H{i}"}
        for i, (t, a, d) in enumerate(items)]}}


def kuwo_albums(items):
    rows = "".join("{'albumid':'%s','name':'%s','artist':'%s','musiccnt':'%s'},"
                   % (aid, n, a, c) for (aid, n, a, c) in items)
    return "{'albumlist':[%s],'SHOW':'1'}" % rows


def kuwo_songs(items):
    rows = "".join("{'musicrid':'%d','name':'%s','artist':'%s','duration':'%d',"
                   "'MINFO':'level:h,bitrate:128,format:mp3,size:1.5Mb'},"
                   % (i + 1, t, a, d) for i, (t, a, d) in enumerate(items))
    return "{'musiclist':[%s]}" % rows


def run_flow(fake, hits, query, artist=""):
    notes: "list[str]" = []
    group = group_albums(hits, query=query, artist=artist, log=notes.append)[0]
    tracks = group_tracks(fake, group, log=notes.append, settings=Settings())
    return group, tracks, notes


# --------------------------------------------------------------------------- #
# 离线矩阵
# --------------------------------------------------------------------------- #
def m1_best_entity():
    """M1 单源最佳实体 + 展示计数（《盗作》的 1 首错值复刻）。"""
    print("\n[M1 单源最佳实体 / 展示计数]")
    hits = [
        AlbumHit(source="migu", album_id="M1", name="盗作", artist="ヨルシカ", track_count=11),
        AlbumHit(source="migu", album_id="M2", name="盗作", artist="ヨルシカ", track_count=1),
        AlbumHit(source="kugou", album_id="K1", name="盗作", artist="ヨルシカ", track_count=14),
        AlbumHit(source="kuwo", album_id="W1", name="盗作", artist="ヨルシカ", track_count=1),
        AlbumHit(source="aggregator", album_id="盗作", name="盗作", artist="ヨルシカ", track_count=14),
    ]
    best = _source_best(hits)
    check("M1: 同源多实体取曲目更多者", best["kugou"].track_count == 14, str(best["kugou"].track_count))
    check("M1: 展示计数 = 目录最完整值（不是 1、也不是聚合归集数）",
          _display_count(hits) == 14, str(_display_count(hits)))
    check("M1: 只有聚合时退回归集数", _display_count(
        [AlbumHit(source="aggregator", album_id="x", name="x", artist="y", track_count=18)]) == 18)


def m2_union_no_loss():
    """M2 并集不丢曲目（单源独有的一首也必须在）。"""
    print("\n[M2 并集不丢曲目]")
    hits = [
        AlbumHit(source="kugou", album_id="K", name="乐行者", artist="林俊杰", track_count=4),
        AlbumHit(source="kuwo", album_id="W", name="乐行者", artist="林俊杰", track_count=5),
    ]
    fake = FakeHttp([
        (lambda u: "album/song?" in u, kugou_songs([("就是我", "林俊杰", 194), ("会读书", "林俊杰", 208),
                                                   ("翅膀", "林俊杰", 221), ("星球", "林俊杰", 225)])),
        (lambda u: "stype=albuminfo" in u, kuwo_songs([("就是我", "林俊杰", 194), ("会读书", "林俊杰", 208),
                                                      ("翅膀", "林俊杰", 221), ("星球", "林俊杰", 225),
                                                      ("冻结", "林俊杰", 289)])),
    ])
    group, tracks, notes = run_flow(fake, hits, "乐行者", "林俊杰")
    titles = [t.title for t in tracks]
    check("M2: 并集 = 5 首（kuwo 独有的一首也进来了）",
          len(tracks) == 5 and "冻结" in titles, str(titles))
    check("M2: 计数与列表长度一致", group.track_count == len(tracks) == 5)
    check("M2: 明细写明每来源曲目表与并集结果",
          any("曲目表" in m for m in notes) and any("并集装配" in m for m in notes))


def m3_version_cluster():
    """M3 版本聚类：语译版（咪咕英文版 11 首）不得与官版 14 首混成一张表。"""
    print("\n[M3 版本聚类（盗作复刻）]")
    hits = [
        AlbumHit(source="migu", album_id="M1", name="盗作", artist="ヨルシカ", track_count=11),
        AlbumHit(source="kugou", album_id="K1", name="盗作", artist="ヨルシカ", track_count=14),
        AlbumHit(source="kuwo", album_id="W1", name="盗作", artist="ヨルシカ", track_count=14),
    ]
    EN = [("Plagiarism", "ヨルシカ", 200), ("Escape", "ヨルシカ", 200), ("Replicant", "ヨルシカ", 200)]
    JP = [(f"官曲{i}", "ヨルシカ", 200) for i in range(1, 15)]
    fake = FakeHttp([
        (lambda u: "search_all.do" in u and "song%22%3A+1" in u,
         migu_songs([(t, a, "M1", f"M{i}") for i, (t, a, _d) in enumerate(EN)])),
        (lambda u: "search_all.do" in u and "album%22%3A+1" in u, migu_album("M1", "盗作", "ヨルシカ")),
        (lambda u: "resourceinfo.do" in u, {"resource": [{"totalCount": 11}]}),
        (lambda u: "album/song?" in u, kugou_songs(JP)),
        (lambda u: "stype=albuminfo" in u, kuwo_songs(JP)),
    ])
    group, tracks, notes = run_flow(fake, hits, "盗作", "ヨルシカ")
    titles = [t.title for t in tracks]
    check("M3: 只保留官版 14 首（英文版 11 首不混入）",
          len(tracks) == 14 and all(t.startswith("官曲") for t in titles), str(titles[:3]))
    check("M3: 被忽略的版本在明细里如实写明",
          any("已忽略另一版本" in m for m in notes), " | ".join(m for m in notes if "版本" in m))


def m4_track_key():
    """M4 去重键：说明括号/变音符/HTML 实体折叠；feat/version 保留为另一首。"""
    print("\n[M4 去重键]")
    check("M4: 说明括号折叠（昼鳶 (白日窃贼) ≡ 昼鳶）",
          _track_key("昼鳶 (白日窃贼)") == _track_key("昼鳶"))
    check("M4: 变音符折叠（AQUÍ ≡ AQUI）",
          _track_key("DE AQUÍ NO SALES (Cap.4: Disputa)") ==
          _track_key("De Aqui No Sales (Cap 4 Disputa)"))
    check("M4: HTML 实体折叠（&apos; ≡ '）",
          _track_key(_clean("Don&apos;t You Remember")) == _track_key("Don't You Remember"))
    check("M4: Commentary 对折叠（1989 Radio Special 26 → 13）",
          _track_key("Welcome To New York (Commentary)") == _track_key("Welcome to New York"))
    check("M4: feat/mix 是另一条录音，不许误并",
          _track_key("ON") != _track_key("ON (Feat. Sia)")
          and _track_key("Butterfly") != _track_key("Butterfly (Prologue Mix)")
          and _track_key("I Need U") != _track_key("I Need U (Urban Mix)")
          and _track_key("Run (Ballad Mix)") != _track_key("Run (Alternative Mix)"))


def m5_outlier():
    """M5 明显偏大的孤例（合辑/加长版）按上中位数参照排除。"""
    print("\n[M5 偏大孤例排除]")
    hits = [
        AlbumHit(source="migu", album_id="M", name="Abbey Road", artist="The Beatles", track_count=34),
        AlbumHit(source="kugou", album_id="K", name="Abbey Road", artist="The Beatles", track_count=16),
        AlbumHit(source="kuwo", album_id="W", name="Abbey Road (2019 Mix)", artist="The Beatles",
                 track_count=17),
    ]
    fake = FakeHttp([
        (lambda u: "search_all.do" in u and "song%22%3A+1" in u,
         migu_songs([("Come Together (2019 Mix)", "The Beatles", "M", "m1"),
                     ("Something (2019 Mix)", "The Beatles", "M", "m2"),
                     ("Come And Get It (Studio Demo)", "The Beatles", "M", "m3"),
                     ("Old Brown Shoe (Take 2)", "The Beatles", "M", "m4"),
                     ("Mean Mr. Mustard (Take 20)", "The Beatles", "M", "m5"),
                     ("Polythene Pam (Take 27)", "The Beatles", "M", "m6")])),
        (lambda u: "search_all.do" in u and "album%22%3A+1" in u, migu_album("M", "Abbey Road", "The Beatles")),
        (lambda u: "resourceinfo.do" in u, {"resource": [{"totalCount": 34}]}),
        (lambda u: "album/song?" in u, kugou_songs(
            [("Come Together (Remastered 2009)", "The Beatles", 259),
             ("Something (Remastered 2009)", "The Beatles", 182)])),
        (lambda u: "stype=albuminfo" in u, kuwo_songs(
            [("Come Together (2019 Mix)", "The Beatles", 259),
             ("Something (2019 Mix)", "The Beatles", 182)])),
    ])
    group, tracks, notes = run_flow(fake, hits, "Abbey Road", "The Beatles")
    check("M5: 偏大孤例被排除（结果 2 首，不含 outtake/加长版曲目）",
          len(tracks) == 2 and not any("Studio Demo" in t.title or "Take" in t.title
                                       for t in tracks),
          str([t.title for t in tracks]))
    check("M5: 排除在明细里如实写明", any("明显偏大" in m for m in notes),
          " | ".join(m for m in notes if "偏大" in m))


def m6_aggregator_fallback():
    """M6 聚合回退：目录来源没有该专辑时才用聚合归集表（计数标注"归集"）。"""
    print("\n[M6 聚合回退]")
    hits = [AlbumHit(source="aggregator", album_id="某专辑", name="某专辑", artist="某歌手",
                     track_count=3, raw={"items": [
                         {"id": "1", "name": "甲", "artist": ["某歌手"], "album": "某专辑"},
                         {"id": "2", "name": "乙", "artist": ["某歌手"], "album": "某专辑"},
                         {"id": "2", "name": "乙", "artist": ["某歌手"], "album": "某专辑"},
                     ], "_count_hint": "归集"})]
    fake = FakeHttp([])
    group = group_albums(hits, query="某专辑", artist="某歌手")[0]
    check("M6: 取曲目前展示计数标注「归集」",
          group.count_hint == "归集" and group.track_count == 3,
          f"{group.count_hint}/{group.track_count}")
    tracks = group_tracks(fake, group, settings=Settings(), log=None)
    check("M6: 只有聚合时用其归集表，且重复条目被去掉",
          len(tracks) == 2 and [t.title for t in tracks] == ["甲", "乙"],
          str([t.title for t in tracks]))


def m7_no_catalog():
    """M7 权威未知（只有酷狗单源）→ 结果 = 该源曲目表。"""
    print("\n[M7 单目录来源]")
    hits = [AlbumHit(source="kugou", album_id="K", name="范特西", artist="周杰伦", track_count=10)]
    fake = FakeHttp([(lambda u: "album/song?" in u, kugou_songs(
        [(f"歌{i}", "周杰伦", 200) for i in range(1, 11)]))])
    group, tracks, notes = run_flow(fake, hits, "范特西", "周杰伦")
    check("M7: 单源结果 = 10 首，计数一致",
          len(tracks) == 10 and group.track_count == 10, f"{len(tracks)}/{group.track_count}")


def m8_cover_artist():
    """M8 同名但翻唱者：逐曲歌手门槛把非原唱的曲目挡住。"""
    print("\n[M8 同名翻唱不当来源]")
    hits = [AlbumHit(source="kugou", album_id="K", name="乐行者", artist="林俊杰", track_count=2)]
    fake = FakeHttp([(lambda u: "album/song?" in u, kugou_songs(
        [("就是我", "林俊杰", 194), ("江南", "某翻唱歌手", 267)]))])
    group, tracks, notes = run_flow(fake, hits, "乐行者", "林俊杰")
    check("M8: 翻唱者曲目被剔除、日志写明",
          [t.title for t in tracks] == ["就是我"] and any("歌手与专辑原唱不符" in m for m in notes),
          str([t.title for t in tracks]))


def m9_jp_written_form():
    """M9 日语：罗马音/读音 → 书写形变体（平台按书写形索引）。"""
    print("\n[M9 日语书写形]")
    check("M9: 罗马音 → 书写形变体",
          _query_texts("dakarabokuha ongakuwo yameta")
          == ["dakarabokuha ongakuwo yameta", "だから僕は音楽を辞めた"],
          str(_query_texts("dakarabokuha ongakuwo yameta")))
    check("M9: 已含假名的书写形不再追加变体（省请求）",
          _query_texts("だから僕は音楽を辞めた") == ["だから僕は音楽を辞めた"])
    check("M9: 罗马音专辑+歌手生成书写形检索文本",
          _text_forms("tada kimi ni hare", "yorushika")
          == ["tada kimi ni hare yorushika", "ただ君に晴れ yorushika"],
          str(_text_forms("tada kimi ni hare", "yorushika")))


def m10_japanese_aliases():
    """Equal-length catalogs must not turn translated titles into extra tracks."""
    print("\n[M10 日语同曲异名与反例]")
    group = group_albums([
        AlbumHit(source="kugou", album_id="K", name="CEREMONY", artist="King Gnu", track_count=12),
        AlbumHit(source="kuwo", album_id="W", name="CEREMONY", artist="King Gnu", track_count=12),
    ], query="CEREMONY", artist="King Gnu")[0]
    names = ["開会式", "どろん", "ティーンエイジャーフォーエバー", "ユーモア", "白日", "幕間",
             "飛行艇", "小さな惑星", "Overflow", "傘", "壇上", "閉会式"]
    durations = [48, 182, 189, 207, 276, 56, 260, 151, 201, 203, 340, 89]
    first = [Track(source="kugou", title=n, artist="King Gnu", duration=d)
             for n, d in zip(names, durations)]
    second = [Track(source="kuwo", title="オーバーフロウ" if i == 8 else n,
                    artist="King Gnu", duration=d)
              for i, (n, d) in enumerate(zip(names, durations))]
    notes = []
    result, _ = _union_lists([(group.variants[0], first), (group.variants[1], second)],
                             group, notes.append)
    check("M10: Overflow/オーバーフロウ 曲序和时长均吻合，只列一次",
          len(result) == 12
          and sum(t.title in ("Overflow", "オーバーフロウ") for t in result) == 1,
          str([t.title for t in result]))
    check("M10: 合并原因写进明细", any("曲序与时长核对" in m for m in notes), str(notes))

    shifted = [Track(source="kuwo", title="オーバーフロウ" if i == 7 else n,
                     artist="King Gnu", duration=d)
               for i, (n, d) in enumerate(zip(names, durations))]
    shifted[7].duration = 201
    shifted[8].title = names[8]
    result_shifted, _ = _union_lists([(group.variants[0], first), (group.variants[1], shifted)],
                                     group, None)
    check("M10: 曲序错位时不凭相同时长误并",
          len(result_shifted) == 13, str([t.title for t in result_shifted]))

    no_duration = [Track(source="kuwo", title=t.title, artist=t.artist, duration=t.duration)
                   for t in second]
    no_duration[8].duration = 0
    result_unknown, _ = _union_lists([(group.variants[0], first), (group.variants[1], no_duration)],
                                     group, None)
    check("M10: 无时长证据时不推断同曲", len(result_unknown) == 13)

    alternate_take = [Track(source="kuwo", title=t.title, artist=t.artist,
                            duration=t.duration) for t in second]
    alternate_take[8].title = "オーバーフロウ (Live)"
    result_alternate, _ = _union_lists(
        [(group.variants[0], first), (group.variants[1], alternate_take)], group, None)
    check("M10: Live/feat 等不同录音即使曲序与时长一致也不误并",
          len(result_alternate) == 13)

    ambiguous = [Track(source="kuwo", title=t.title, artist=t.artist,
                       duration=t.duration) for t in second]
    ambiguous[7].duration = 201
    result_ambiguous, _ = _union_lists(
        [(group.variants[0], first), (group.variants[1], ambiguous)], group, None)
    check("M10: 两首时长相近时不根据曲序猜测跨语言同曲",
          len(result_ambiguous) == 13)

    elma = group_albums([
        AlbumHit(source="migu", album_id="M", name="エルマ", artist="ヨルシカ", track_count=14),
        AlbumHit(source="kugou", album_id="K", name="エルマ", artist="ヨルシカ", track_count=14),
        AlbumHit(source="kuwo", album_id="W", name="エルマ", artist="ヨルシカ", track_count=14),
    ], query="エルマ", artist="ヨルシカ")[0]
    japanese = [Track(source=s, title=f"曲{i}", artist="ヨルシカ", duration=180 + i)
                for s in ("kugou", "kuwo") for i in range(14)]
    translated = [Track(source="migu", title=f"曲{i}" if i < 10 else f"Translation{i}",
                        artist="ヨルシカ") for i in range(14)]
    cluster = _dominant_cluster([
        (elma.variants[0], translated),
        (elma.variants[1], japanese[:14]),
        (elma.variants[2], japanese[14:]),
    ], None)
    merged, _ = _union_lists(cluster, elma, None)
    check("M10: 部分英译表和两份原文表分簇，结果保持 14 首",
          len(cluster) == 2 and len(merged) == 14
          and all(not t.title.startswith("Translation") for t in merged),
          str([t.title for t in merged]))


# --------------------------------------------------------------------------- #
# 真源矩阵
# --------------------------------------------------------------------------- #
#: 精选真源断言：(查询, 歌手, 期望曲目数, 必含, 禁止)
LIVE = [
    ("乐行者", "林俊杰", 11, ["就是我", "不懂"], ["江南", "曹操", "小酒窝"]),
    ("十一月的萧邦", "周杰伦", 12, ["夜曲", "发如雪"], ["晴天"]),
    ("盗作", "ヨルシカ", 14, ["花に亡霊"], []),
    ("dakarabokuha ongakuwo yameta", "", 14, ["花に亡霊"], []),
]

#: 多语种地毯式矩阵：(查询, 歌手, 官方曲目数) —— 官方数与平台一致时精确断言，否则只查不变量
WIDE = [
    ("乐行者", "林俊杰", 11), ("十一月的萧邦", "周杰伦", 12), ("范特西", "周杰伦", 10),
    ("叶惠美", "周杰伦", 11), ("第二天堂", "林俊杰", None), ("黑色柳丁", "陶喆", 13),
    ("盗作", "ヨルシカ", 14), ("だから僕は音楽を辞めた", "ヨルシカ", 14),
    ("エルマ", "ヨルシカ", 14), ("CEREMONY", "King Gnu", 12),
    ("幻燈", "ヨルシカ", 25), ("創作", "ヨルシカ", 5),
    ("狂言", "Ado", 14), ("瞬間的シックスセンス", "あいみょん", 12),
    ("BOOTLEG", "米津玄師", None), ("STRAY SHEEP", "米津玄師", 15),
    ("BORN PINK", "BLACKPINK", 8), ("MAP OF THE SOUL : 7", "BTS", 20),
    ("Palette", "IU", 10), ("1989", "Taylor Swift", None),
    ("21", "Adele", None), ("Thriller", "Michael Jackson", 9),
    ("Abbey Road", "The Beatles", None), ("Racine carree", "Stromae", 13),
    ("El Mal Querer", "ROSALIA", 11),
    ("所以我放弃了音乐", "Yorushika", 14),
    ("瞬间的第六感", "Aimyon", 12),
    ("花样年华 Young Forever", "BTS", 23),
    ("颤栗", "Michael Jackson", 9),
    ("艾比路", "The Beatles", None),
    ("幻灯", "yorushika", 25),
    ("盜作", "yorushika", 14),
    ("夏草", "", 7),
]

EXPECTED_TITLES = {
    "夏草": ["夏陰、ピアノを弾く", "カトレア", "言って。", "あの夏に咲け",
             "飛行", "靴の花火", "雲と幽霊"],
}


def _expected(counts: "list[int]") -> int:
    """目录最佳实体计数的上中位数参照 + 界内最大（与模块内口径一致）。"""
    if not counts:
        return 0
    s = sorted(counts)
    ref = s[len(s) // 2] if len(s) % 2 else max(s[len(s) // 2 - 1], s[len(s) // 2])
    return max((c for c in s if c <= ref + max(2, ref // 4)), default=ref)


def run_live(albums, *, exact: bool) -> None:
    from mmusic.net import HttpClient
    http = HttpClient(timeout=12, retries=2)
    loss = bloat = dupes = 0
    try:
        for query, artist, official in albums:
            label = f"{artist} - {query}".strip(" -")
            try:
                hits = search_albums(http, query, artist=artist, settings=Settings(), log=None)
                groups = group_albums(hits, query=query, artist=artist)
                if not groups:
                    check(f"live: {label} 能检索到专辑", False, "0 组")
                    continue
                group = groups[0]
                best = _source_best(group.variants)
                catalog = [h.track_count for src, h in best.items()
                           if src in ("migu", "kugou", "kuwo") and h.track_count]
                expected = _expected(catalog) if catalog else max(
                    (h.track_count or 0 for h in best.values()), default=0)
                tracks = group_tracks(http, group, settings=Settings(), log=None)
            except Exception as exc:  # noqa: BLE001
                check(f"live: {label} 链路不抛错", False, f"{type(exc).__name__}: {exc}")
                continue
            titles = [t.title for t in tracks]
            if query in EXPECTED_TITLES:
                check(f"live: {label} 逐曲名称与曲序",
                      [_track_key(t) for t in titles] ==
                      [_track_key(t) for t in EXPECTED_TITLES[query]], str(titles))
            problems = invariants(tracks, expected)
            if any("丢曲目" in p for p in problems):
                loss += 1
            if any("膨胀" in p for p in problems):
                bloat += 1
            if any("重复" in p for p in problems):
                dupes += 1
            check(f"live: {label} 不变量（不丢/不膨胀/不重复）", not problems, str(problems))
            if exact and official is not None:
                check(f"live: {label} 曲目数 = {official}",
                      len(tracks) == official, f"{len(tracks)} | {titles[:10]}")
                for bad in ():
                    pass
            print(f"   · {group.name} | {group.artist} | {len(tracks)} 首"
                  f" | 传输声明 {dict(sorted((s, h.track_count) for s, h in best.items()))}"
                  f" | 前 3：{titles[:3]}")
    finally:
        http.close()
    print(f"   —— 丢曲目 {loss} · 膨胀 {bloat} · 重复 {dupes}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="附：精选真源断言（含精确曲目数）")
    ap.add_argument("--wide", action="store_true", help="附：多语种地毯式真源矩阵")
    args = ap.parse_args()

    m1_best_entity()
    m2_union_no_loss()
    m3_version_cluster()
    m4_track_key()
    m5_outlier()
    m6_aggregator_fallback()
    m7_no_catalog()
    m8_cover_artist()
    m9_jp_written_form()
    m10_japanese_aliases()
    if args.live:
        print("\n[live 精选真源：精确曲目数]")
        run_live([(q, a, o) for q, a, o, _m, _f in LIVE], exact=True)
    if args.wide:
        print("\n[wide 多语种地毯式真源矩阵]")
        run_live(WIDE, exact=True)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for f in FAILED:
        print("  -", f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

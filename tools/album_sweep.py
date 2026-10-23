"""广域真源扫射：多语种专辑 × 多来源，逐张报告"来源声明数 / 我取到的曲目 / 判定"。

用途（2026-10-23，用户要求"地毯式真源实测"）：
* 摸清每个来源在不同语种上的字段形态（专辑实体 / 歌级条目 / 语译版 / 归集推导）；
* 抓出"曲目丢失"（我取到的 < 来源里最完整的）与"曲目膨胀"（我取到的 > 全部来源）；
* 作为重构前/后的对照证据（同一份清单跑两次，逐张对比）。

用法：python tools/album_sweep.py [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mmusic.config import Settings                      # noqa: E402
from mmusic.net import HttpClient                       # noqa: E402
from mmusic.search.album import (_source_best, _track_key, group_albums,  # noqa: E402
                                 group_tracks, search_albums)

# (查询, 歌手, 官方曲目数或 None)
ALBUMS = [
    # 华语
    ("乐行者", "林俊杰", 11),
    ("十一月的萧邦", "周杰伦", 12),
    ("范特西", "周杰伦", 10),
    ("叶惠美", "周杰伦", 11),
    ("第二天堂", "林俊杰", 12),
    ("黑色柳丁", "陶喆", 13),
    # 日语
    ("盗作", "ヨルシカ", 14),
    ("だから僕は音楽を辞めた", "ヨルシカ", 14),
    ("エルマ", "ヨルシカ", 14),
    ("CEREMONY", "King Gnu", 12),
    ("幻燈", "ヨルシカ", 25),
    ("狂言", "Ado", 14),
    ("BOOTLEG", "米津玄師", 16),
    ("STRAY SHEEP", "米津玄師", 15),
    # 韩语
    ("BORN PINK", "BLACKPINK", 8),
    ("MAP OF THE SOUL : 7", "BTS", 20),
    ("Palette", "IU", 10),
    # 欧美
    ("1989", "Taylor Swift", 13),
    ("21", "Adele", 11),
    ("Thriller", "Michael Jackson", 9),
    ("Abbey Road", "The Beatles", 17),
    # 小语种
    ("Racine carree", "Stromae", 13),
    ("El Mal Querer", "ROSALIA", 11),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="把逐张结果写到 JSON")
    args = ap.parse_args()

    http = HttpClient(timeout=12, retries=2)
    settings = Settings()
    results = []
    loss = bloat = dupes = 0
    print(f"{'专辑':32} {'来源声明数':28} {'取到':>4} {'官方':>4}  判定")
    print("-" * 100)
    try:
        for query, artist, official in ALBUMS:
            label = f"{artist} - {query}"
            row = {"query": query, "artist": artist, "official": official}
            try:
                hits = search_albums(http, query, artist=artist, settings=settings, log=None)
                groups = group_albums(hits, query=query, artist=artist)
                if not groups:
                    print(f"{label:32} {'(无分组)':28} {'-':>4} {official or '-':>4}  无法检索")
                    row["error"] = "no groups"
                    results.append(row)
                    continue
                group = groups[0]
                declared = {src: (h.track_count or 0)
                            for src, h in _source_best(group.variants).items()}
                tracks = group_tracks(http, group, settings=settings, log=None)
                titles = [t.title for t in tracks]
                keys = [_track_key(t) for t in titles]
                has_dup = len(keys) != len(set(keys))
                # 判定口径：只与**目录来源**（咪咕/酷狗/酷我）最佳实体比；参照取上中位数，
                # 与模块内"明显偏大才排除"一致 —— 聚合归集数不作准。
                catalog = sorted(v for s, v in declared.items() if s in ("migu", "kugou", "kuwo"))
                if catalog:
                    if len(catalog) % 2:
                        ref = catalog[len(catalog) // 2]
                    else:
                        ref = max(catalog[len(catalog) // 2 - 1], catalog[len(catalog) // 2])
                    best_source = max((c for c in catalog if c <= ref + max(2, ref // 4)),
                                      default=ref)
                else:
                    ref = best_source = max(declared.values()) if declared else 0
                verdict = []
                if len(tracks) < best_source:
                    verdict.append(f"丢曲目({len(tracks)}<{best_source})")
                    loss += 1
                if len(tracks) > best_source + max(2, best_source // 4):
                    verdict.append(f"膨胀({len(tracks)}>{best_source})")
                    bloat += 1
                if has_dup:
                    verdict.append("重复曲目")
                    dupes += 1
                if official and len(tracks) < official and len(tracks) >= best_source:
                    verdict.append(f"源不足({len(tracks)}<官方{official})")
                decl = " ".join(f"{k}:{v}" for k, v in sorted(declared.items()))
                print(f"{label:32} {decl:28} {len(tracks):>4} {official or '-':>4}  "
                      f"{' '.join(verdict) or 'OK'}")
                row.update({"declared": declared, "count": len(tracks),
                            "titles": titles, "verdict": verdict,
                            "group": [group.name, group.artist]})
            except Exception as exc:  # noqa: BLE001
                print(f"{label:32} {'-':28} {'-':>4} {official or '-':>4}  "
                      f"异常 {type(exc).__name__}: {str(exc)[:60]}")
                row["error"] = f"{type(exc).__name__}: {exc}"
            results.append(row)
    finally:
        http.close()

    print("-" * 100)
    print(f"丢曲目 {loss} 张 · 膨胀 {bloat} 张 · 重复 {dupes} 张 · 共 {len(results)} 张")
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), "utf-8")
        print("JSON ->", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

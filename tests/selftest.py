"""Offline + optional live self-tests.

    python tests/selftest.py            # offline only (no network)
    python tests/selftest.py --live     # also exercises the real fallback chain

Exit code 0 == every check passed.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import fixtures as fx  # noqa: E402
from mmusic.config import Settings  # noqa: E402
from mmusic.crypto.ncm import NcmDecoder  # noqa: E402
from mmusic.crypto.qmc import QmcDecoder  # noqa: E402
from mmusic.downloader import validate  # noqa: E402
from mmusic.duration import audio_duration, consensus, format_seconds  # noqa: E402
from mmusic.errors import NeedsOnlineError  # noqa: E402
from mmusic.match import (filename_query, title_match_score,  # noqa: E402
                        parse_song_filename, parse_song_filename_candidates,
                        rank_tracks, rank_tracks_multi, similarity,
                        split_originals, track_version_markers, version_markers)
from mmusic.model import Track  # noqa: E402
from mmusic.net import HttpClient  # noqa: E402
from mmusic.pipeline import (MIN_ACCEPT, MIN_TITLE_SIMILARITY,  # noqa: E402
                             MIN_TRACK_SECONDS, Pipeline, _plausible_max)
from mmusic.search.registry import build_sources  # noqa: E402
from mmusic.search.base import SearchSource  # noqa: E402
from mmusic.sniff import sniff, sniff_path  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   -> {extra}" if extra and not cond else ""))


def test_decoders() -> None:
    print("\n[decoders]")

    ncm = fx.build_ncm(fx.PAYLOAD_MP3)
    res = NcmDecoder(ncm).decode()
    check("NCM: payload byte-exact", res.audio == fx.PAYLOAD_MP3)
    check("NCM: title recovered", res.title == "测试歌曲", res.title)
    check("NCM: artist recovered", res.artist == "测试歌手", res.artist)
    check("NCM: album recovered", res.album == "测试专辑", res.album)
    check("NCM: cover url upgraded to https", res.cover_url.startswith("https://"), res.cover_url)
    check("NCM: ext sniffed as mp3", res.ext == "mp3", res.ext)

    ncm_flac = fx.build_ncm(
        fx.PAYLOAD_FLAC,
        cover=b"\xff\xd8\xff\xe0" + bytes(range(200)),
        meta={"musicName": "FLAC 歌曲", "artist": [["A"], ["B"]], "album": "专辑",
              "format": "flac", "albumPic": ""},
    )
    res2 = NcmDecoder(ncm_flac).decode()
    check("NCM: flac payload byte-exact", res2.audio == fx.PAYLOAD_FLAC)
    check("NCM: multi-artist joined", res2.artist == "A / B", res2.artist)
    check("NCM: ext sniffed as flac", res2.ext == "flac", res2.ext)

    q1 = fx.build_qmc_v1(fx.PAYLOAD_MP3)
    check("QMCv1: static cipher round-trip", QmcDecoder(q1, "qmc0").decode().audio == fx.PAYLOAD_MP3)

    key_map = bytes(range(32))                        # -> map cipher
    q2 = fx.build_qmc_v2(fx.PAYLOAD_FLAC, key_map)
    r2 = QmcDecoder(q2, "qmcflac").decode()
    check("QMCv2-map: round-trip", r2.audio == fx.PAYLOAD_FLAC)
    check("QMCv2-map: ext sniffed as flac", r2.ext == "flac", r2.ext)

    key_rc4 = bytes((i * 13 + 7) & 0xFF for i in range(512))   # -> RC4 cipher
    q3 = fx.build_qmc_v2(fx.PAYLOAD_OGG, key_rc4)
    check("QMCv2-rc4: round-trip", QmcDecoder(q3, "qmcogg").decode().audio == fx.PAYLOAD_OGG)

    stag = fx.build_qmc_stag(fx.PAYLOAD_FLAC)
    info = sniff(stag[:16], stag[-8:], "mflac")
    check("STag: sniffed as needs-online", info.needs_online and info.kind == "qmc", str(info))
    try:
        QmcDecoder(stag, "mflac").decode()
        check("STag: raises NeedsOnlineError", False, "no exception")
    except NeedsOnlineError:
        check("STag: raises NeedsOnlineError", True)


def test_validation() -> None:
    print("\n[validation]")
    ok = validate(fx.PAYLOAD_MP3 + b"\x00" * 40000, "mp3")
    check("validate: accepts a real mp3 header", ok.ok and ok.ext == "mp3", ok.reason)
    bad = validate(b"<!DOCTYPE html><html>blocked</html>", "mp3")
    check("validate: rejects HTML", not bad.ok, bad.reason)
    small = validate(b"fLaC" + b"\x00" * 40, "flac")
    check("validate: rejects tiny file", not small.ok, small.reason)
    html = validate(b"<html>nope</html>" + b"\x00" * 40000, "mp3")
    check("validate: rejects html-ish body", not html.ok, html.reason)


def test_matching() -> None:
    print("\n[matching]")
    t, a = parse_song_filename("周杰伦 - 稻香")
    check("filename: title parsed", t == "稻香", t)
    check("filename: artist parsed", a == "周杰伦", a)
    check("similarity: exact", similarity("稻香", "稻香") == 1.0)
    check("similarity: noisy variant high", similarity("稻香 (Live)", "稻香") > 0.6)
    tracks = [Track("x", "稻香", "周杰伦"), Track("x", "晴天", "周杰伦"), Track("x", "稻香", "其他")]
    ranked = rank_tracks("稻香", "周杰伦", tracks)
    check("ranking: best hit first", ranked[0].title == "稻香" and ranked[0].artist == "周杰伦", str(ranked[0]))

    # filename order: both conventions must be considered, and the correct
    # reading must clear the download threshold for "Title - Artist" files
    cands = parse_song_filename_candidates("晴天 - 周杰伦")
    check("filename: both orders offered",
          ("晴天", "周杰伦") in cands and ("周杰伦", "晴天") in cands, str(cands))
    check("filename: 'artist - title' reading stays first (regression)",
          parse_song_filename("周杰伦 - 稻香") == ("稻香", "周杰伦"), str(cands))
    check("filename: query keeps the file's word order",
          filename_query("晴天 - 周杰伦") == "晴天 周杰伦", filename_query("晴天 - 周杰伦"))
    perfect = Track("kuwo", "晴天", "周杰伦", duration=269, ext="mp3")
    rank_tracks_multi(cands, [perfect], prefer_ext="mp3")
    check("filename: 'title - artist' now clears the 0.34 threshold",
          perfect.score >= 0.34, f"score={perfect.score}")


def test_pipeline_offline() -> None:
    print("\n[pipeline/offline]")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "in"
        src.mkdir()
        (src / "song.ncm").write_bytes(fx.build_ncm(fx.PAYLOAD_MP3))
        (src / "track.qmc0").write_bytes(fx.build_qmc_v1(fx.PAYLOAD_MP3))
        (src / "plain.mp3").write_bytes(fx.PAYLOAD_MP3 + b"\x00" * 40000)
        (src / "weird.xyz").write_bytes(b"not audio at all" * 100)

        settings = Settings(output_dir=str(tmp / "out"), enable_fallback=False,
                            write_metadata=False, fetch_cover=False, keep_original=True)
        logs: list[str] = []
        pipe = Pipeline(settings, cache_path=tmp / "cache.json", on_log=logs.append)
        results = pipe.process_many(sorted(src.iterdir()))
        by_name = {r.name: r for r in results}
        check("pipeline: ncm decoded", by_name["song.ncm"].status == "decoded", by_name["song.ncm"].message)
        check("pipeline: qmc decoded", by_name["track.qmc0"].status == "decoded", by_name["track.qmc0"].message)
        check("pipeline: plain copied", by_name["plain.mp3"].status == "copied", by_name["plain.mp3"].message)
        check("pipeline: unknown failed", by_name["weird.xyz"].status == "failed", by_name["weird.xyz"].message)
        check("pipeline: 3 files produced", len(list((tmp / 'out').glob('*.mp3'))) == 3,
              str(list((tmp / 'out').glob('*.mp3'))))
        outs = list((tmp / "out").glob("*.mp3"))
        exact = [o.name for o in outs if o.read_bytes() == fx.PAYLOAD_MP3]
        check("pipeline: ncm/qmc outputs are byte-exact (tags off)",
              len(exact) >= 2, f"exact={exact} of {[o.name for o in outs]}")

        # dedup: re-run must skip the already-decoded ncm
        pipe2 = Pipeline(settings, cache_path=tmp / "cache.json", on_log=lambda _m: None)
        again = pipe2.process_file(src / "song.ncm")
        check("pipeline: hash cache skips re-decode", again.status == "skipped", again.status)


def test_title_artist_filename() -> None:
    """A "Title - Artist.mgg" file must reach the fallback *download*, not
    just the search.  Runs fully offline by pointing the source list at a
    local library folder."""
    print("\n[filename order: title - artist]")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        lib.mkdir()
        (lib / "晴天 - 周杰伦.mp3").write_bytes(
            b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 40000)

        src = tmp / "晴天 - 周杰伦.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")      # undecodable, STag -> offline fallback

        settings = Settings(output_dir=str(tmp / "out"), sources=["local"],
                            local_dirs=[str(lib)], enable_fallback=True,
                            write_metadata=False, fetch_cover=False,
                            remember_hashes=False)
        logs: list[str] = []
        pipe = Pipeline(settings, cache_path=tmp / "c.json", on_log=logs.append)
        res = pipe.process_file(src)
        tail = " | ".join(logs[-4:])

        check("fallback: an STag .mgg triggers the search path",
              res.status == "downloaded", f"{res.status} :: {tail}")
        check("fallback: matched through the configured source",
              res.source == "local", res.source)
        check("fallback: an output file was produced",
              bool(res.out_path) and Path(res.out_path).is_file(), str(res.out_path))
        check("fallback: log lists both filename readings",
              any("两种读法" in m for m in logs), tail)
        check("fallback: log states which reading was used",
              any("采用读法" in m for m in logs), tail)
        check("fallback: query preserves the file word order",
              any("晴天 周杰伦" in m for m in logs), tail)


def test_version_filter() -> None:
    print("\n[version filter: 原曲 vs 伴奏/改编]")
    blocked = ["晴天 (KTV版伴奏)", "晴天 伴奏", "晴天 纯音乐", "晴天 (无人声)", "晴天 (off vocal)",
               "晴天 minus one", "晴天 karaoke", "晴天 カラオケ", "晴天 (Instrumental)",
               "晴天 -inst", "晴天_inst", "晴天 inst.", "晴天 (Remix)", "晴天 (Cover)",
               "晴天 翻唱", "晴天 (Live)", "晴天 现场版", "晴天 (Demo)", "晴天 试听",
               "晴天 (Acoustic)", "晴天 不插电", "晴天 (剪辑版)", "晴天 (Edit)", "晴天 (Mix)",
               # 2026-10-19 三十首批量实测里真实"漏网"的（它们都能拿到直链）：
               "天空の城ラピュタ (网友改编)", "晴天 3D环绕版", "稻香 (2018 CCTV-15 现场版)",
               "遇见 (黑胶版)", "夜曲 (重生版)", "光年之外 热爱版", "晴天 (2020重制版)",
               "富士山下 (2010音乐万万岁现场)", "晴天 晚会版"]
    missed = [t for t in blocked if not version_markers(t)]
    check(f"filter: all {len(blocked)} variant markers are caught", not missed, f"漏掉: {missed}")

    kept = ["晴天", "周杰伦 - 晴天", "Alive", "Instant Crush", "Mixed Up",
            "Deliver", "Oliver", "Olivia", "Believer", "Studio 54", "Film",
            # 词表补词后**不得误伤**的正常标题（含"节目""重制"这类词根的普通用法）
            "Shape Of You", "Für Elise", "千本桜", "学猫叫", "海阔天空", "重生"]
    hurt = [(t, version_markers(t)) for t in kept if version_markers(t)]
    check("filter: no false positives on normal titles", not hurt, f"误伤: {hurt}")

    check("filter: '伴奏' only when the user asks for it is handled elsewhere",
          bool(version_markers("晴天 (伴奏) - 周杰伦")))

    # 字段级：歌名干净但 FileName/标签里带伴奏，也必须被拦下
    dirty = Track("kugou", "晴天", "周杰伦", raw={"FileName": "周杰伦 - 晴天 (KTV版伴奏)"})
    check("filter: checks file/name/tag fields, not just the title",
          not is_original_version(dirty) if False else bool(track_version_markers(dirty)),
          str(track_version_markers(dirty)))

    tracks = [Track("kuwo", "晴天", "周杰伦"),
              Track("kuwo", "晴天 (KTV版伴奏)", "周杰伦"),
              Track("kuwo", "晴天 (Live)", "周杰伦"),
              Track("kuwo", "晴天 (深情版)", "Lucky小爱"),   # 不在排除清单内，属正常候选
              Track("kuwo", "晴天 (Remix)", "DJ")]
    originals, dropped = split_originals(tracks)
    check("filter: split keeps only clean candidates",
          [t.title for t in originals] == ["晴天", "晴天 (深情版)"], str([t.title for t in originals]))
    check("filter: split reports what it dropped", len(dropped) == 3, str(dropped))


def test_variant_filter_e2e() -> None:
    """伴奏版必须被排除，即使它排在更前面 / 得分更高。"""
    print("\n[version filter: end-to-end]")
    ORIG = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x11" * 40000
    KTV = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x22" * 40000
    base = dict(enable_fallback=True, write_metadata=False, fetch_cover=False,
                remember_hashes=False, sources=["local"])

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        lib.mkdir()
        (lib / "晴天 - 周杰伦.mp3").write_bytes(ORIG)
        (lib / "晴天 - 周杰伦 (KTV版伴奏).mp3").write_bytes(KTV)

        src = tmp / "晴天 - 周杰伦.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")

        logs: list[str] = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out"), local_dirs=[str(lib)], **base),
                        cache_path=tmp / "c.json", on_log=logs.append)
        res = pipe.process_file(src)
        got = Path(res.out_path).read_bytes() if res.out_path else b""
        tail = " | ".join(logs[-5:])

        check("variant:伴奏 candidate is excluded",
              any("已排除" in m and "伴奏" in m for m in logs), tail)
        check("variant: the ORIGINAL audio was downloaded",
              got == ORIG and got != KTV, f"{res.out_path}")
        check("variant: output filename carries no variant marker",
              "伴奏" not in Path(res.out_path).name, Path(res.out_path).name)

    # 用户主动点名要伴奏 -> 不过滤，但要警告
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        lib.mkdir()
        (lib / "晴天 - 周杰伦 (KTV版伴奏).mp3").write_bytes(KTV)
        src = tmp / "晴天 (伴奏) - 周杰伦.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")

        logs2: list[str] = []
        pipe2 = Pipeline(Settings(output_dir=str(tmp / "out"), local_dirs=[str(lib)], **base),
                         cache_path=tmp / "c.json", on_log=logs2.append)
        res2 = pipe2.process_file(src)
        check("variant: explicitly requested variant is kept",
              res2.status == "downloaded", f"{res2.status} :: {' | '.join(logs2[-3:])}")
        check("variant: explicitly requested variant warns the user",
              any("版本标记" in m for m in logs2), " | ".join(logs2[-3:]))


def test_cross_source_best() -> None:
    """不再"第一个能用的音源就算"：所有音源的干净候选一起比，分高者先下。"""
    print("\n[cross-source: best match wins, not first source]")
    WEAK = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xaa" * 40000
    BEST = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xbb" * 40000

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wpath = tmp / "weak.mp3"
        wpath.write_bytes(WEAK)
        bpath = tmp / "best.mp3"
        bpath.write_bytes(BEST)

        class FakeSource(SearchSource):
            def __init__(self, name, label, title, artist, path):
                super().__init__(None, {})
                self.name = name
                self.label = label
                self._title = title
                self._artist = artist
                self._path = str(path)

            def search(self, query, *, quality="mp3", limit=10):
                return [Track(self.name, self._title, self._artist, ext="mp3",
                              raw={"path": self._path})]

            def resolve(self, track, *, quality="mp3"):
                track.url = Path(track.raw["path"]).resolve().as_uri()
                track.ext = "mp3"
                return track

        # 列在前面的音源分更低，必须仍然输给后面的高置信度原曲
        # 弱音源必须是"能过歌手门槛、但排名更低"的候选：
        # 用不同歌手的低质版本已不可行（那类现在会在歌手门槛处被直接淘汰）。
        weak = FakeSource("aaa", "弱音源", "晴天", "周杰伦 Jay Chou", wpath)
        best = FakeSource("zzz", "强音源", "晴天", "周杰伦", bpath)

        src = tmp / "晴天 - 周杰伦.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")
        logs: list[str] = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out"), enable_fallback=True,
                                 write_metadata=False, fetch_cover=False,
                                 remember_hashes=False),
                        cache_path=tmp / "c.json", on_log=logs.append,
                        sources=[weak, best])
        res = pipe.process_file(src)
        got = Path(res.out_path).read_bytes() if res.out_path else b""
        tail = " | ".join(logs[-5:])

        check("cross-source: higher-scoring candidate from the LATER source wins",
              got == BEST, f"got={len(got)}B {res.out_path} :: {tail}")
        check("cross-source: log reports the comparison",
              any("择优采用" in m for m in logs), tail)
        check("cross-source: log reports the per-source best",
              any("强音源" in m and "原曲候选" in m for m in logs), tail)


def test_duration_verify() -> None:
    print("\n[duration verification]")
    flac = fx.make_flac(269)
    check("duration: FLAC STREAMINFO -> exact seconds",
          abs((audio_duration(flac, len(flac)) or 0) - 269) < 0.01,
          str(audio_duration(flac, len(flac))))
    mp3 = fx.make_mp3_xing(215)
    check("duration: MP3 Xing header -> seconds",
          abs((audio_duration(mp3, len(mp3)) or 0) - 215) < 1.0,
          str(audio_duration(mp3, len(mp3))))
    check("duration: zero-filled data -> None (never trusted)",
          audio_duration(b"\x00" * 4096, 4096) is None)
    lone_sync = b"\xff\xfb\x90\x00" + b"\x00" * 4092      # 单个假帧头，不能骗过解析器
    check("duration: a lone fake MP3 sync word is rejected",
          audio_duration(lone_sync, 4096) is None, str(audio_duration(lone_sync, 4096)))
    check("duration: format_seconds", format_seconds(269) == "4:29", format_seconds(269))

    _, flags, _ = consensus([269.0, 271.0, 120.0, None])
    check("consensus: length outlier (bad remix) is rejected",
          flags == [True, True, False, False], str(flags))
    check("consensus: an unmeasured candidate is never trusted", flags[3] is False)
    ref2, flags2, _ = consensus([269.0, 120.0])
    check("consensus: two disagreeing candidates -> best-ranked length wins",
          flags2 == [True, False] and abs((ref2 or 0) - 269) < 0.1, f"{ref2} {flags2}")
    ref3, flags3, _ = consensus([None, None])
    check("consensus: nothing measured -> nothing trusted",
          ref3 is None and flags3 == [False, False], str(flags3))
    ref4, flags4, _ = consensus([200.0, None])
    check("consensus: single measured candidate passes alone", flags4 == [True, False], str(flags4))
    check("reference: prefers the most complete value",
          _plausible_max([142, 131, 235]) == 235, str(_plausible_max([142, 131, 235])))
    check("reference: an outlying long value is ignored",
          _plausible_max([142, 131, 235, 480]) == 235, str(_plausible_max([142, 131, 235, 480])))


def test_duration_cross_check_e2e() -> None:
    """标题/歌手都正确、但音频长度对不上的 remix，必须被淘汰。"""
    print("\n[duration cross-check: end-to-end]")
    real, wrong = fx.make_mp3_xing(269), fx.make_mp3_xing(120)
    fallback = dict(sources=["local"], enable_fallback=True, write_metadata=False,
                    fetch_cover=False, remember_hashes=False)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        lib.mkdir()
        (lib / "晴天 - 周杰伦.mp3").write_bytes(real)
        (lib / "晴天 - 周杰伦 (官方版).mp3").write_bytes(wrong)   # 名称无破绽，但只有 2:00
        src = tmp / "晴天 - 周杰伦.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")

        logs: list[str] = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out"), local_dirs=[str(lib)], **fallback),
                        cache_path=tmp / "c.json", on_log=logs.append)
        res = pipe.process_file(src)
        got = Path(res.out_path).read_bytes() if res.out_path else b""
        tail = " | ".join(logs[-8:])

        check("duration-e2e: the length outlier is excluded",
              any("时长异常已排除" in m or "歌名不符" in m for m in logs), tail)
        check("duration-e2e: the length-consistent candidate is downloaded",
              got == real, f"{res.out_path}")
        check("duration-e2e: log reports a reference duration",
              any("参考时长" in m for m in logs), tail)
        check("duration-e2e: log reports the measured length",
              any("实测时长" in m for m in logs), tail)

    # 只有一个候选、且拿不到时长 -> 必须警告，不得声称已通过交叉验证
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        lib.mkdir()
        (lib / "晴天 - 周杰伦.mp3").write_bytes(
            b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 40000)   # 无法解析时长
        src = tmp / "晴天 - 周杰伦.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")

        logs2: list[str] = []
        pipe2 = Pipeline(Settings(output_dir=str(tmp / "out"), local_dirs=[str(lib)], **fallback),
                         cache_path=tmp / "c2.json", on_log=logs2.append)
        res2 = pipe2.process_file(src)
        tail2 = " | ".join(logs2[-6:])

        check("duration-e2e: unmeasurable candidate is marked untrusted",
              any("视为不可信" in m for m in logs2), tail2)
        check("duration-e2e: result is declarative, never asks the user to act",
              "时长未" in res2.message and "请" not in res2.message and "确认" not in res2.message,
              res2.message)
        check("duration-e2e: still produces a file rather than refusing",
              res2.status == "downloaded", f"{res2.status} :: {tail2}")


def test_title_gate() -> None:
    print("\n[title field gate]")
    readings = parse_song_filename_candidates("晴天 - 周杰伦")
    check("title-gate: exact title passes",
          title_match_score(readings, Track("x", "晴天", "周杰伦")) >= MIN_TITLE_SIMILARITY)
    check("title-gate: local filename style passes",
          title_match_score(readings, Track("x", "晴天 - 周杰伦", "")) >= MIN_TITLE_SIMILARITY)
    check("title-gate: title inside a medley still passes (duration floor handles it)",
          title_match_score(readings, Track("x", "新歌前奏 + Julia + 晴天", "x")) >= MIN_TITLE_SIMILARITY)
    same_artist = [Track("x", "花海", "周杰伦"), Track("x", "听妈妈的话", "周杰伦"),
                   Track("x", "稻香", "周杰伦")]
    scores = [round(title_match_score(readings, t), 2) for t in same_artist]
    check("title-gate: 'same artist, different song' is rejected",
          all(sc < MIN_TITLE_SIMILARITY for sc in scores), str(scores))


def test_duration_floor_e2e() -> None:
    """只有 15 秒的"前奏/试听片段"必须被拦下，不能当原曲下载。"""
    print("\n[duration floor: end-to-end]")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        lib.mkdir()
        (lib / "晴天 - 周杰伦.mp3").write_bytes(fx.make_mp3_xing(15))     # 0:15 片段
        src = tmp / "晴天 - 周杰伦.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")
        logs: list[str] = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out"), sources=["local"],
                                 local_dirs=[str(lib)], enable_fallback=True,
                                 write_metadata=False, fetch_cover=False,
                                 remember_hashes=False),
                        cache_path=tmp / "c.json", on_log=logs.append)
        res = pipe.process_file(src)
        tail = " | ".join(logs[-6:])
        check("duration-floor: a 15s clip is excluded",
              any("时长过短已排除" in m for m in logs), tail)
        check("duration-floor: nothing is written from a clip",
              res.status == "failed" and not (tmp / "out").exists(),
              f"{res.status} :: {tail}")


def test_cross_platform_reference() -> None:
    """参考时长必须跨平台取最完整值：只信一个平台就会下到截短版。"""
    print("\n[cross-platform duration reference]")
    short, full = fx.make_mp3_xing(142), fx.make_mp3_xing(235)      # 2:22（截短） / 3:55（完整）

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        spath = tmp / "short.mp3"
        spath.write_bytes(short)
        fpath = tmp / "full.mp3"
        fpath.write_bytes(full)

        class Fake(SearchSource):
            def __init__(self, name, path, duration):
                super().__init__(None, {})
                self.name = name
                self.label = name
                self._path = str(path)
                self._duration = duration

            def search(self, query, *, quality="mp3", limit=10):
                return [Track(self.name, "最长的电影", "周杰伦", duration=self._duration,
                              ext="mp3", raw={"path": self._path})]

            def resolve(self, track, *, quality="mp3"):
                track.url = Path(track.raw["path"]).resolve().as_uri()
                track.ext = "mp3"
                return track

        truncating = Fake("kuwo_like", spath, 142)     # 平台标注 2:22
        complete = Fake("kugou_like", fpath, 235)      # 平台标注 3:55
        src = tmp / "周杰伦 - 最长的电影.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")
        logs: list[str] = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out"), enable_fallback=True,
                                 write_metadata=False, fetch_cover=False,
                                 remember_hashes=False),
                        cache_path=tmp / "c.json", on_log=logs.append,
                        sources=[truncating, complete])
        res = pipe.process_file(src)
        got = Path(res.out_path).read_bytes() if res.out_path else b""
        tail = " | ".join(logs[-6:])

        check("xplat: reference is the most complete platform's value",
              any("跨平台参考时长 3:55" in m for m in logs), tail)
        check("xplat: the truncating platform is excluded by its own metadata",
              any("标注时长偏短已排除" in m for m in logs), tail)
        check("xplat: the complete version is the one downloaded", got == full, f"{res.out_path}")


def test_verified_only_download() -> None:
    """已有参考时长时，测不出时长的候选绝不能被拿去下载。"""
    print("\n[only duration-verified resources are downloaded]")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        unmeasurable = tmp / "unmeasurable.mp3"
        unmeasurable.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 40000)

        class Fake(SearchSource):
            def __init__(self, path):
                super().__init__(None, {})
                self.name = self.label = "fake"
                self._path = str(path)

            def search(self, query, *, quality="mp3", limit=10):
                return [Track(self.name, "最长的电影", "周杰伦", duration=235,
                              ext="mp3", raw={"path": self._path})]

            def resolve(self, track, *, quality="mp3"):
                track.url = Path(track.raw["path"]).resolve().as_uri()
                track.ext = "mp3"
                return track

        src = tmp / "周杰伦 - 最长的电影.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")
        logs: list[str] = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out"), enable_fallback=True,
                                 write_metadata=False, fetch_cover=False,
                                 remember_hashes=False),
                        cache_path=tmp / "c.json", on_log=logs.append,
                        sources=[Fake(unmeasurable)])
        res = pipe.process_file(src)
        check("verified-only: nothing is written when no duration could be measured",
              res.status == "failed" and not (tmp / "out").exists(),
              f"{res.status} :: {' | '.join(logs[-3:])}")


def test_output_files() -> None:
    print("\n[output files]")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        lib.mkdir()
        src = lib / "a.ncm"
        src.write_bytes(fx.build_ncm(fx.PAYLOAD_MP3))

        # default settings: metadata ON (tags written), no sidecar files
        settings = Settings(output_dir="", enable_fallback=False,
                            fetch_cover=False, remember_hashes=False)
        pipe = Pipeline(settings, cache_path=tmp / "c.json")
        r = pipe.process_file(src)

        produced = sorted(p.name for p in lib.iterdir())
        check("output: no .meta.json sidecar written",
              not list(lib.glob("*.meta.json")), str(produced))
        check("output: exactly source + one audio file",
              len(produced) == 2 and produced[0] == "a.ncm", str(produced))
        check("output: metadata still written INTO the audio file",
              Path(r.out_path).read_bytes() != fx.PAYLOAD_MP3,
              "file is byte-identical to the payload -> tags were not written")


def test_output_dir_default() -> None:
    print("\n[output-dir default]")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        album = tmp / "albums" / "周杰伦"
        album.mkdir(parents=True)
        src = album / "song.ncm"
        src.write_bytes(fx.build_ncm(fx.PAYLOAD_MP3))
        elsewhere = tmp / "elsewhere"
        elsewhere.mkdir()

        base = dict(enable_fallback=False, write_metadata=False, fetch_cover=False,
                    remember_hashes=False)

        # default: output must land next to the SOURCE file, not the process cwd
        cwd = Path.cwd()
        os.chdir(elsewhere)
        try:
            pipe = Pipeline(Settings(output_dir="", **base), cache_path=tmp / "c1.json")
            r = pipe.process_file(src)
        finally:
            os.chdir(cwd)
        out = Path(r.out_path)
        check("output-dir: defaults to the source file's folder",
              out.parent.resolve() == album.resolve(), str(out.parent))
        check("output-dir: is NOT the process cwd (regression)",
              out.parent.resolve() != elsewhere.resolve(), str(out.parent))
        check("output-dir: source file untouched", src.exists())

        # explicit output_dir still wins
        pipe2 = Pipeline(Settings(output_dir=str(tmp / "chosen"), **base),
                         cache_path=tmp / "c2.json")
        r2 = pipe2.process_file(src)
        check("output-dir: explicit setting still honoured",
              Path(r2.out_path).parent.resolve() == (tmp / "chosen").resolve(), r2.out_path)

        # explicit output_dir equal to the source folder is a no-op clash-wise
        pipe3 = Pipeline(Settings(output_dir=str(album), **base), cache_path=tmp / "c3.json")
        r3 = pipe3.process_file(src)
        check("output-dir: same folder produces a de-duplicated name",
              Path(r3.out_path).parent.resolve() == album.resolve()
              and Path(r3.out_path).name != src.name, Path(r3.out_path).name)


def test_source_pack() -> None:
    print("\n[source pack: schema + registry equivalence]")
    from mmusic.search.pack import (PackError, builtin_specs, load_pack,
                                    pack_summary, validate_pack)
    from mmusic.search.registry import available_sources, build_sources

    specs = builtin_specs()
    check("pack: builtin pack lists the live sources in order",
          [s.id for s in specs][:5] == ["local", "kuwo", "kugou", "migu", "aggregator"],
          str([s.id for s in specs]))
    check("pack: demo/experimental entries are not built in",
          "example_open_audio" not in [s.id for s in specs],
          str([s.id for s in specs]))

    good = {"schema": 1, "sources": [
        {"id": "demo", "label": "示例源", "kind": "local", "impl": "local",
         "enabled_by_default": True}]}
    check("pack: a valid document validates", len(validate_pack(good)) == 1)

    def _rejects(name: str, doc: dict) -> None:
        try:
            validate_pack(doc)
            ok = False
        except PackError:
            ok = True
        check(f"pack: rejects {name}", ok)

    _rejects("an unknown kind", {"schema": 1, "sources": [{"id": "x1", "label": "x", "kind": "hack"}]})
    _rejects("a malformed id", {"schema": 1, "sources": [{"id": "Bad-Id", "label": "x", "kind": "local"}]})
    _rejects("an unknown impl", {"schema": 1, "sources": [{"id": "x2", "label": "x", "kind": "adapter", "impl": "evil"}]})
    _rejects("an unknown (exec-ish) field", {"schema": 1, "sources": [{"id": "x3", "label": "x", "kind": "local", "exec": "calc"}]})
    _rejects("a non-https search url", {"schema": 1, "sources": [{"id": "x4", "label": "x", "kind": "json_api", "params": {"search_url": "http://a/b"}}]})
    _rejects("an unsupported schema version", {"schema": 2, "sources": []})
    _rejects("duplicate source ids", {"schema": 1, "sources": [{"id": "x5", "label": "a", "kind": "local"}, {"id": "x5", "label": "b", "kind": "local"}]})

    ids = [s.id for s in load_pack({})]
    check("pack: default order is local-first and CC/PD last",
          ids[:5] == ["local", "kuwo", "kugou", "migu", "aggregator"]
          and {"wikimedia", "internet_archive"} <= set(ids)
          and ids[-1] == "internet_archive"
          and "aggregator_joox" not in ids, str(ids))
    check("pack: a stale legacy default list does not hide new sources",
          len(load_pack({"sources": ["local", "kuwo", "kugou", "aggregator"]})) > 4
          and "internet_archive" in [x.id for x in
                                     load_pack({"sources": ["local", "kuwo", "kugou",
                                                            "aggregator"]})],
          str([x.id for x in load_pack({"sources": ["local", "kuwo", "kugou", "aggregator"]})]))
    check("pack: a real user choice is still respected",
          [x.id for x in load_pack({"sources": ["kuwo", "local"]})] == ["kuwo", "local"])
    check("pack: the dead JOOX channel is not built in",
          "aggregator_joox" not in [x.id for x in builtin_specs()])
    check("pack: settings['sources'] selects and orders",
          [s.id for s in load_pack({"sources": ["kugou", "local"]})] == ["kugou", "local"])
    check("pack: unknown requested ids are ignored",
          [s.id for s in load_pack({"sources": ["nope", "local"]})] == ["local"])
    check("pack: summary renders every entry",
          len(pack_summary(specs).splitlines()) == len(specs))

    built = build_sources(None, {})
    names = [x.name for x in built]
    check("pack: registry builds the full chain, names unique",
          names[:5] == ["local", "kuwo", "kugou", "migu", "aggregator"]
          and len(set(names)) == len(names) and "internet_archive" in names, str(names))
    check("pack: available_sources grew, mainstream kept",
          {"kuwo", "kugou", "aggregator", "local"} <= set(available_sources())
          and {"wikimedia", "internet_archive", "manual"} <= set(available_sources()),
          str(available_sources()))
    check("pack: a broken/missing user pack never breaks startup",
          [s.id for s in load_pack({"source_pack_path": "Z:/definitely/missing.json"})]
          == [s.id for s in load_pack({})])


def test_fusion() -> None:
    print("\n[fusion: content dedupe + low-confidence]")
    from mmusic.search.fusion import (collapse_duplicates, dedupe_key,
                                      is_low_confidence, mark_low_confidence)

    a = Track("kuwo", "晴天", "周杰伦", duration=235)
    b = Track("kugou", "晴天 ", "周杰伦", duration=236)      # 空白 + ±1s 应归并
    live = Track("kuwo", "晴天 (Live)", "周杰伦", duration=235)
    short = Track("kuwo", "晴天", "周杰伦", duration=142)     # 明显截短 -> 不归并

    check("fusion: same song from two sources shares a key", dedupe_key(a) == dedupe_key(b))
    check("fusion: a different version keeps its own key", dedupe_key(a) != dedupe_key(live))
    check("fusion: a clearly shortened copy keeps its own key", dedupe_key(a) != dedupe_key(short))

    kept, dropped = collapse_duplicates([a, b, live, short])
    check("fusion: duplicates collapse, order preserved",
          [t.source for t in kept] == ["kuwo", "kuwo", "kuwo"] and len(dropped) == 1,
          f"kept={len(kept)} dropped={len(dropped)}")
    check("fusion: the keeper is the higher-ranked occurrence",
          bool(dropped) and dropped[0][1] is a and dropped[0][0] is b)

    rows = [Track("x", "a", "b", score=0.40), Track("x", "c", "d", score=0.80)]
    n = mark_low_confidence(rows)
    check("fusion: low scores flagged, good scores not",
          n == 1 and is_low_confidence(rows[0]) and not is_low_confidence(rows[1]))


def test_resource_index() -> None:
    print("\n[resource ledger: negative cache / health / degradation]")
    from mmusic.search.index import ResourceIndex

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ix = ResourceIndex(tmp / "r.db")
        check("ledger: opens a database", ix.available)
        check("ledger: an unseen query is not blocked", not ix.query_blocked("稻香 周杰伦", "kuwo"))

        ix.note_search("kuwo", "稻香 周杰伦", found=False)
        check("ledger: an empty result blocks it with a reason",
              ix.query_block_reason("稻香 周杰伦", "kuwo") == "无结果",
              ix.query_block_reason("稻香 周杰伦", "kuwo"))
        check("ledger: the block expires after its TTL",
              ix.query_blocked("稻香 周杰伦", "kuwo")
              and not ix.query_blocked("稻香 周杰伦", "kuwo", now=int(time.time()) + 7 * 3600))
        ix.note_search("kuwo", "稻香 周杰伦", found=True)
        check("ledger: a later hit unblocks the query",
              not ix.query_blocked("稻香 周杰伦", "kuwo"))

        ix.note_search("kugou", "稻香 周杰伦", found=False, error=True)
        check("ledger: an error blocks with its own reason",
              ix.query_block_reason("稻香 周杰伦", "kugou") == "上次出错")

        ix.note_resource("k", "kuwo", "https://cdn/ok.mp3", ext="mp3", bitrate=320, ok=True)
        ix.note_resource("k", "kuwo", "https://cdn/bad.mp3", ext="mp3", ok=False)
        check("ledger: a failed url is remembered as stale",
              ix.is_stale_url("kuwo", "https://cdn/bad.mp3")
              and not ix.is_stale_url("kuwo", "https://cdn/ok.mp3"))
        check("ledger: key stats aggregate across sources",
              ix.key_stats("k")["total"] == 2 and ix.key_stats("k")["verified"] == 1,
              str(ix.key_stats("k")))

        health = {r["source"]: r for r in ix.health()}
        check("ledger: health aggregates attempts/successes",
              health["kuwo"]["attempts"] == 2 and health["kuwo"]["successes"] == 1
              and health["kugou"]["failures"] == 1, str(health))
        check("ledger: stats count resources and queries",
              ix.stats()["resources"] == 2 and ix.stats()["queries"] == 2, str(ix.stats()))
        ix.close()
    print("  (ledger released its handle - the temp dir cleaned up)")

    off = ResourceIndex("Z:/definitely/missing/dir/r.db", enabled=False)
    check("ledger: a disabled index degrades to no-ops",
          not off.available and not off.query_blocked("q", "s")
          and off.health() == [] and off.stats()["resources"] == 0)


def test_fallback_dedupe_e2e() -> None:
    """P1：同一首歌经多个源命中时只下载一次（内容去重，需开启台账）。"""
    print("\n[dedupe: same song from two sources]")
    A = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xcc" * 40000
    B = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xdd" * 40000

    class FakeSource(SearchSource):
        def __init__(self, name, label, path):
            super().__init__(None, {})
            self.name, self.label = name, label
            self._path = str(path)

        def search(self, query, *, quality="mp3", limit=10):
            return [Track(self.name, "晴天", "周杰伦", ext="mp3", raw={"path": self._path})]

        def resolve(self, track, *, quality="mp3"):
            track.url = Path(track.raw["path"]).resolve().as_uri()
            track.ext = "mp3"
            return track

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pa, pb = tmp / "a.mp3", tmp / "b.mp3"
        pa.write_bytes(A)
        pb.write_bytes(B)
        src = tmp / "晴天 - 周杰伦.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")

        logs: list[str] = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out"), enable_fallback=True,
                                 write_metadata=False, fetch_cover=False,
                                 remember_hashes=True),
                        cache_path=tmp / "c.json", on_log=logs.append,
                        sources=[FakeSource("zaa", "源A", pa), FakeSource("zbb", "源B", pb)])
        res = pipe.process_file(src)
        produced = list((tmp / "out").glob("*.mp3"))
        tail = " | ".join(logs[-6:])
        pipe.close()

        check("dedupe: the duplicate is merged",
              any("重复资源已合并" in m for m in logs), tail)
        check("dedupe: log reports the surviving candidate count",
              any("去重后候选 1 个" in m for m in logs), tail)
        check("dedupe: exactly one file was produced",
              res.status == "downloaded" and len(produced) == 1, f"{res.status} {produced}")


def test_dependency_guard() -> None:
    """红线守卫：运行时代码只允许 PySide6 与可选 mutagen（AGENTS.md §1）。"""
    print("\n[dependency guard: zero runtime deps]")
    import ast
    import sys as _sys

    root = Path(__file__).resolve().parent.parent
    stdlib = set(_sys.stdlib_module_names)
    local = {"mmusic", "ui", "tests", "tools", "fixtures"}
    allowed = {"PySide6", "mutagen"}
    files = ([root / "main.py"] + sorted((root / "mmusic").rglob("*.py"))
             + sorted((root / "ui").rglob("*.py")))
    found: "dict[str, str]" = {}
    for f in files:
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for n in names:
                if n not in stdlib and n not in local:
                    found.setdefault(n, f.name)

    extra = sorted(set(found) - allowed)
    check("deps: no third-party runtime import beyond PySide6 / mutagen", not extra,
          f"unexpected: {extra}")
    check("deps: both allowed deps are the ones actually used",
          set(found) <= allowed and "PySide6" in found, str(sorted(found)))
    print(f"  (scanned {len(files)} files; third-party = {sorted(found)})")


def test_variant_filter_more() -> None:
    """过滤更彻底：电台版一类"看着像原曲但被剪短"的标记必须命中，且不误伤正常歌名。"""
    print("\n[variant filter: radio/tv/instrumental markers]")
    from mmusic.match import split_originals, version_markers

    must_hit = ["夜曲 (电台版)", "夜曲 电台版", "晴天 (TV版)", "稻香 铃声版", "晴天 片段版",
                "晴天 (钢琴版)", "晴天 吉他版", "晴天 抖音版", "晴天 升调版", "晴天 变速版",
                "晴天 清唱版", "晴天 合唱版", "晴天 交响版", "晴天 教学", "晴天 混剪",
                "晴天 (Radio Edit)", "晴天 (radio version)", "晴天 (Mashup)", "晴天 (A Cappella)",
                "晴天 (Inst.)", "晴天 off vocal", "晴天 (Remix)", "晴天 (Live)", "晴天 (伴奏)"]
    missed = [t for t in must_hit if not version_markers(t)]
    check("variant: every radio/tv/instrumental marker is caught", not missed, f"missed={missed}")

    # 误伤防护：这些是正常歌名，绝不能因为含"电台/吉他/TV"就被过滤
    must_not_hit = ["电台情歌", "吉他手", "TV", "夏日倾情", "小情歌", "生活倒影",
                    "Alive", "Instant Crush", "电音之王"]
    hurt = [t for t in must_not_hit if version_markers(t)]
    check("variant: normal song titles are never mis-blocked", not hurt, f"wrongly blocked={hurt}")

    tracks = [Track("s", "夜曲 (电台版)", "周杰伦"), Track("s", "夜曲", "周杰伦")]
    originals, dropped = split_originals(tracks)
    check("variant: the radio edit is dropped, the clean one kept",
          [t.title for t in originals] == ["夜曲"] and len(dropped) == 1,
          f"kept={[t.title for t in originals]} dropped={len(dropped)}")


def test_qualified_title_loses() -> None:
    """带修饰的标题必须让位给干净标题（相似度会剥掉括号，所以必须显式扣分）。"""
    print("\n[ranking: clean title beats a qualified one]")
    from mmusic.match import rank_tracks_multi

    tracks = [Track("s", "夜曲 (电台版)", "周杰伦"),
              Track("s", "夜曲 (钢琴版)", "周杰伦"),
              Track("s", "夜曲", "周杰伦")]
    ranked = rank_tracks_multi([("夜曲", "周杰伦")], tracks)     # 返回的是新列表
    check("ranking: the unqualified title ranks first",
          ranked[0].title == "夜曲", str([(t.title, t.score) for t in ranked]))
    check("ranking: qualified titles are scored lower",
          ranked[0].score > ranked[1].score, str([t.score for t in ranked]))


def test_duration_tolerance_tight() -> None:
    """旧容差(10%)会放过只短 10% 的电台版；新容差必须挡掉它，且端到端生效。"""
    print("\n[tolerance: a ~10% shorter cut is rejected]")
    from mmusic.duration import consensus

    _ref, flags, _why = consensus([228.0, 205.0])
    check("tolerance: a 205s cut fails against a 228s original", flags == [True, False], str(flags))
    _ref2, flags2, _why2 = consensus([228.0, 222.0])
    check("tolerance: a small (6s) difference is still tolerated", flags2 == [True, True], str(flags2))

    real = fx.make_mp3_xing(228)
    cut = fx.make_mp3_xing(205)

    class FakeSource(SearchSource):
        def __init__(self, name, path, duration):
            super().__init__(None, {})
            self.name, self.label = name, name
            self._path, self._duration = str(path), duration

        def search(self, query, *, quality="mp3", limit=10):
            # 两个候选的**标题都干净**（都叫"夜曲"），名字过滤帮不上忙，
            # 只能靠时长把 205s 的那个挡掉。
            return [Track(self.name, "夜曲", "周杰伦", duration=self._duration,
                          ext="mp3", raw={"path": self._path})]

        def resolve(self, track, *, quality="mp3"):
            track.url = Path(track.raw["path"]).resolve().as_uri()
            track.ext = "mp3"
            return track

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rp, cp = tmp / "real.mp3", tmp / "cut.mp3"
        rp.write_bytes(real)
        cp.write_bytes(cut)
        src = tmp / "夜曲 - 周杰伦.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")
        logs: list[str] = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out"), enable_fallback=True,
                                 write_metadata=False, fetch_cover=False,
                                 remember_hashes=False),
                        cache_path=tmp / "c.json", on_log=logs.append,
                        # 两个候选的平台标注**完全一致**（都声称 3:48），只有一个文件其实是 205s。
        # 名字过滤与"标注预筛"都帮不上忙 —— 只有实测才能识破它。
        sources=[FakeSource("zaa", rp, 228), FakeSource("zbb", cp, 228)])
        res = pipe.process_file(src)
        got = Path(res.out_path).read_bytes() if res.out_path else b""
        tail = " | ".join(logs[-6:])
        check("tolerance: the full-length file is the one downloaded", got == real, tail)
        check("tolerance: the mislabelled short file is rejected after measuring",
              any("时长异常已排除" in m for m in logs), tail)
        check("tolerance: log reports a measured-based reference",
              any("实测" in m and "参考时长" in m for m in logs), tail)


def test_mp3_vbr_estimate() -> None:
    """没有 Xing 头的 MP3 也要能估准时长（按真实帧扫描算平均码率，而非首帧）。"""
    print("\n[mp3: frame-scan estimate without a Xing header]")
    from mmusic.duration import audio_duration, mp3_scan_estimate

    # 60 个连续 CBR 帧（128kbps/44.1kHz -> 每帧 417 字节，含 1152 采样）
    frame = b"\xff\xfb\x90\x00" + b"\x00" * (417 - 4)
    head = frame * 60
    scanned_seconds = 60 * 1152 / 44100.0

    est_same = mp3_scan_estimate(head, len(head))          # 整文件 == 已知部分
    check("mp3: frame-scan reproduces the scanned length itself",
          est_same is not None and abs(est_same - scanned_seconds) < 0.05, str(est_same))

    est_4x = mp3_scan_estimate(head, len(head) * 4)        # 外推到 4 倍大小
    check("mp3: frame-scan scales with the advertised file size",
          est_4x is not None and abs(est_4x - scanned_seconds * 4) < 0.2, str(est_4x))

    check("mp3: no frames -> no estimate (never trusted)",
          mp3_scan_estimate(b"\x00" * 4096, 4096) is None)
    check("mp3: head-only estimate never returns a bogus value for silence",
          audio_duration(b"\x00" * 4096, 4096) is None)


def test_sniffer() -> None:
    """P2：页面级媒体提取（纯标准库）+ 登录墙识别（放弃而非绕过）。"""
    print("\n[sniffer: page media extraction]")
    from mmusic.search.sniffer import (classify_url, extract_media_urls,
                                       extract_page_links, login_wall_reason)

    html_ld = ('<html><head><script type="application/ld+json">'
               '{"@type":"AudioObject","contentUrl":"https://cdn/x.mp3"}</script></head></html>')
    html_og = '<html><head><meta property="og:audio" content="/media/a.flac"></head></html>'
    html_tw = ('<html><head><meta name="twitter:player:stream" '
               'content="https://cdn/t.m4a"></head></html>')
    html_tag = '<html><body><audio><source src="https://cdn/b.ogg"></audio></body></html>'
    html_rx = '<html><body><script>var f="https://cdn/c.mp3?token=1";</script></body></html>'
    html_wall = ('<html><body><form><input type="password" name="p"></form>'
                 '<p>请先登录后继续</p></body></html>')
    html_wall2 = '<html><body><div>开通会员即可下载</div></body></html>'
    html_plain = '<html><body><h1>Nothing here</h1></body></html>'

    hits = extract_media_urls(html_ld)
    check("sniffer: JSON-LD contentUrl is extracted",
          hits and hits[0].url == "https://cdn/x.mp3" and hits[0].kind == "json-ld",
          str(hits))
    hits = extract_media_urls(html_og, "https://site.example/page")
    check("sniffer: og:audio is extracted and made absolute",
          hits and hits[0].url == "https://site.example/media/a.flac"
          and hits[0].kind == "og:audio", str(hits))
    hits = extract_media_urls(html_tw)
    check("sniffer: twitter:player:stream is recognised",
          hits and hits[0].kind == "twitter:player:stream", str(hits))
    hits = extract_media_urls(html_tag)
    check("sniffer: <audio><source> markup is recognised",
          hits and hits[0].url == "https://cdn/b.ogg" and hits[0].kind == "media-tag", str(hits))
    hits = extract_media_urls(html_rx)
    check("sniffer: a bare media URL in a script is the last resort",
          hits and hits[0].url.startswith("https://cdn/c.mp3") and hits[0].kind == "regex",
          str(hits))

    both = ('<html><head><meta property="og:audio" content="https://cdn/og.mp3"></head>'
            '<body><script>var x="https://cdn/rx.mp3";</script></body></html>')
    hits = extract_media_urls(both)
    check("sniffer: metadata outranks the regex fallback",
          hits[0].url == "https://cdn/og.mp3" and len(hits) == 2, str([h.url for h in hits]))
    check("sniffer: duplicate URLs collapse",
          len(extract_media_urls(html_rx + html_rx)) == 1)

    check("sniffer: classify_url reads the extension",
          classify_url("https://x/a.MP3?t=1") == "mp3" and classify_url("https://x/p.html") is None)

    check("sniffer: a password form is a login wall",
          login_wall_reason(html_wall) == "需要登录", login_wall_reason(html_wall))
    check("sniffer: members-only text is a wall",
          login_wall_reason(html_wall2) == "需要会员/付费", login_wall_reason(html_wall2))
    check("sniffer: a normal page is not a wall", login_wall_reason(html_plain) == "")
    check("sniffer: a page that already exposes media is not a wall",
          login_wall_reason(html_ld) == "")

    links = extract_page_links('<a href="/track/abc">夜曲</a><a href="/about">关于</a>',
                               "https://demo.example/search", r"/track/")
    check("sniffer: page links honour the pack's regex",
          [u for u, _t in links] == ["https://demo.example/track/abc"], str(links))
    check("sniffer: link anchor text is kept as the title",
          links and links[0][1] == "夜曲", str(links))


def test_html_scrape_source() -> None:
    """P2：纯数据的 html_scrape 源端到端（检索页 -> 详情页 -> 媒体直链）。"""
    print("\n[html_scrape: declarative source end-to-end]")
    from mmusic.search.html_source import HtmlScrapeSource
    from mmusic.search.pack import validate_spec

    SEARCH = ('<html><body><a href="/track/abc123">夜曲 - 周杰伦</a>'
              '<a href="/about">关于我们</a></body></html>')
    DETAIL = ('<html><head><meta property="og:audio" '
              'content="https://cdn.example/n.mp3"></head></html>')
    WALL = '<html><body><form><input type="password"></form><p>请先登录</p></body></html>'

    class FakeHttp:
        def __init__(self, detail=DETAIL):
            self.calls: list[str] = []
            self.detail = detail

        def get_text(self, url, *, encoding="utf-8", headers=None, **kw):
            self.calls.append(url)
            return self.detail if "/track/" in url else SEARCH

    spec = validate_spec({"id": "demo_html", "label": "示例 HTML 源", "kind": "html_scrape",
                          "params": {"search_url": "https://demo.example/search?q={query}",
                                     "link_regex": r"/track/"}})
    http = FakeHttp()
    src = HtmlScrapeSource(http, {}, spec)
    tracks = src.search("夜曲 周杰伦", limit=5)
    check("html_scrape: only pattern-matching pages become results",
          len(tracks) == 1 and tracks[0].title == "夜曲 - 周杰伦", str([t.title for t in tracks]))
    check("html_scrape: the detail page is carried on the track",
          tracks and tracks[0].raw.get("page") == "https://demo.example/track/abc123",
          str(tracks[0].raw if tracks else {}))

    track = src.resolve(tracks[0])
    check("html_scrape: resolve digs the media URL out of the detail page",
          track.url == "https://cdn.example/n.mp3" and track.ext == "mp3", track.url)

    walled = HtmlScrapeSource(FakeHttp(WALL), {}, spec)
    try:
        walled.resolve(tracks[0])
        ok = False
    except Exception as exc:                       # noqa: BLE001
        ok = "放弃" in str(exc) or "登录" in str(exc)
    check("html_scrape: a login wall is given up on, never bypassed", ok)

    plain = validate_spec({"id": "demo_plain", "label": "p", "kind": "html_scrape",
                           "params": {"search_url": "https://demo.example/s?q={query}"}})
    check("html_scrape: without a link_regex every page link is a candidate",
          len(HtmlScrapeSource(FakeHttp(), {}, plain).search("x")) == 2)


def test_pack_html_scrape() -> None:
    """P2：用户清单里的一条 html_scrape 条目即可新增源（零代码）。"""
    print("\n[pack: html_scrape entries build without code]")
    import json as _json
    from mmusic.search.pack import PackError, builtin_specs, validate_pack
    from mmusic.search.registry import build_sources

    try:
        validate_pack({"schema": 1, "sources": [
            {"id": "bad_regex", "label": "x", "kind": "html_scrape",
             "params": {"search_url": "https://a.example/s", "link_regex": "["}}]})
        rejected = False
    except PackError:
        rejected = True
    check("pack: an invalid link_regex is rejected", rejected)

    with tempfile.TemporaryDirectory() as tmp:
        pack = Path(tmp) / "pack.json"
        pack.write_text(_json.dumps({"schema": 1, "sources": [
            {"id": "mine", "label": "我的源", "kind": "html_scrape",
             "enabled_by_default": True,
             "home": "https://mine.example/",
             "params": {"search_url": "https://mine.example/find?q={query}",
                        "link_regex": r"/song/"}}]}), encoding="utf-8")
        built = build_sources(None, {"sources": ["mine"], "source_pack_path": str(pack)})
        check("pack: a user html_scrape entry builds the generic source",
              len(built) == 1 and built[0].name == "mine"
              and type(built[0]).__name__ == "HtmlScrapeSource",
              str([type(b).__name__ for b in built]))


def test_artist_gate() -> None:
    """歌手硬门槛：同名翻唱（歌名一样、演唱者不同）绝不允许下载。"""
    print("\n[artist gate: a same-title cover is never downloaded]")
    from mmusic.match import artist_match_score

    readings = [("黑色毛衣", "周杰伦")]
    check("artist: the real singer passes",
          artist_match_score(readings, Track("x", "黑色毛衣", "周杰伦")) == 1.0)
    check("artist: a multi-artist string with the right person passes",
          artist_match_score(readings, Track("x", "黑色毛衣", "周杰伦/Jay Chou")) >= 0.85)
    check("artist: a different singer is rejected",
          artist_match_score(readings, Track("x", "黑色毛衣", "林志美_ 柏菲音乐")) < 0.60,
          str(artist_match_score(readings, Track("x", "黑色毛衣", "林志美_ 柏菲音乐"))))
    check("artist: blank/unknown singer cannot be judged",
          artist_match_score(readings, Track("x", "黑色毛衣", "")) == 1.0
          and artist_match_score(readings, Track("x", "黑色毛衣", "未知")) == 1.0)
    check("artist: CJK vs latin is now REJECTED (strong constraint)",
          artist_match_score(readings, Track("x", "黑色毛衣", "Jay Chou")) < 0.60,
          str(artist_match_score(readings, Track("x", "黑色毛衣", "Jay Chou"))))
    from mmusic.match import best_reading_match, parse_song_filename_candidates
    keys = parse_song_filename_candidates("周杰伦 - 黑色毛衣")
    check("artist: both fields must match under the SAME reading",
          not best_reading_match(keys, Track("x", "黑色毛衣 - 林志美", "黑色毛衣 - 林志美"),
                                 min_title=0.60, min_artist=0.60)[0],
          "两种读法各对一半时必须判为不匹配")
    check("artist: the same-reading rule still accepts the real one",
          best_reading_match(keys, Track("x", "黑色毛衣 - 周杰伦", "黑色毛衣 - 周杰伦"),
                             min_title=0.60, min_artist=0.60)[0])
    check("artist: a filename without any artist rejects nothing",
          artist_match_score([("黑色毛衣", "")], Track("x", "黑色毛衣", "林志美")) == 1.0)

    REAL = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x55" * 40000
    COVER = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x66" * 40000

    class FakeSource(SearchSource):
        def __init__(self, name, title, artist, path):
            super().__init__(None, {})
            self.name, self.label = name, name
            self._title, self._artist, self._path = title, artist, str(path)

        def search(self, query, *, quality="mp3", limit=10):
            return [Track(self.name, self._title, self._artist, ext="mp3",
                          raw={"path": self._path})]

        def resolve(self, track, *, quality="mp3"):
            track.url = Path(track.raw["path"]).resolve().as_uri()
            track.ext = "mp3"
            return track

    base = dict(enable_fallback=True, write_metadata=False, fetch_cover=False,
                remember_hashes=False)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rp, cp = tmp / "real.mp3", tmp / "cover.mp3"
        rp.write_bytes(REAL)
        cp.write_bytes(COVER)
        src = tmp / "周杰伦 - 黑色毛衣.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")

        logs: list[str] = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out1"), **base),
                        cache_path=tmp / "c1.json", on_log=logs.append,
                        sources=[FakeSource("zaa", "黑色毛衣", "林志美_ 柏菲音乐", cp),
                                 FakeSource("zbb", "黑色毛衣", "周杰伦", rp)])
        res = pipe.process_file(src)
        got = Path(res.out_path).read_bytes() if res.out_path else b""
        tail = " | ".join(logs[-6:])
        check("artist: the cover is dropped by the gate",
              any("歌手不符" in m for m in logs), tail)
        check("artist: the original singer's audio is the one downloaded", got == REAL, tail)

        logs2: list[str] = []
        pipe2 = Pipeline(Settings(output_dir=str(tmp / "out2"), **base),
                         cache_path=tmp / "c2.json", on_log=logs2.append,
                         sources=[FakeSource("zaa", "黑色毛衣", "林志美_ 柏菲音乐", cp)])
        res2 = pipe2.process_file(src)
        out2 = tmp / "out2"
        produced = list(out2.glob("*")) if out2.is_dir() else []
        check("artist: with only a cover available, nothing is written",
              res2.status == "failed" and not produced, f"{res2.status} {produced}")
        check("artist: the failure says why",
              "演唱者" in (res2.message or "") or "翻唱" in (res2.message or ""),
              res2.message)


def test_artist_gate_local() -> None:
    """本地曲库同样受歌手门槛保护；"只写歌名"的曲库不能被误杀。"""
    print("\n[artist gate: local library]")
    REAL = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x77" * 40000
    COVER = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x88" * 40000
    base = dict(enable_fallback=True, write_metadata=False, fetch_cover=False,
                remember_hashes=False)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "周杰伦 - 黑色毛衣.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")

        lib = tmp / "lib"
        lib.mkdir()
        (lib / "黑色毛衣 - 周杰伦.mp3").write_bytes(REAL)
        (lib / "黑色毛衣 - 林志美.mp3").write_bytes(COVER)
        logs: list[str] = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out"), sources=["local"],
                                 local_dirs=[str(lib)], **base),
                        cache_path=tmp / "c.json", on_log=logs.append)
        res = pipe.process_file(src)
        got = Path(res.out_path).read_bytes() if res.out_path else b""
        tail = " | ".join(logs[-6:])
        check("artist/local: the original singer's file is downloaded", got == REAL, tail)
        check("artist/local: the cover is never the result", got != COVER, tail)
        check("artist/local: output name is clean (stem not duplicated)",
              res.out_path and Path(res.out_path).name == "黑色毛衣 - 周杰伦.mp3",
              Path(res.out_path).name if res.out_path else "-")

        lib2 = tmp / "lib2"
        lib2.mkdir()
        (lib2 / "黑色毛衣.mp3").write_bytes(REAL)      # 只有歌名，没有任何歌手信息
        pipe2 = Pipeline(Settings(output_dir=str(tmp / "out2"), sources=["local"],
                                  local_dirs=[str(lib2)], **base),
                         cache_path=tmp / "c2.json", on_log=lambda _m: None)
        res2 = pipe2.process_file(src)
        check("artist/local: a bare song-name library still works",
              res2.status == "downloaded", res2.status)


def test_strong_gate() -> None:
    """强约束：歌名或歌手任何一项不匹配，都不得下载。

    复现实际反馈：源文件 `周杰伦 - 黑色毛衣.mgg`，音源返回的却是
    `黑色毛衣(R&B)` / 演唱者 `Haohao Music` —— 歌名只是"包含"、歌手完全不同，
    以前两者都能蒙混过关。
    """
    print("\n[strong gate: neither title nor artist may merely resemble]")
    from mmusic.match import best_reading_match, parse_song_filename_candidates

    keys = parse_song_filename_candidates("周杰伦 - 黑色毛衣")

    def gate(title: str, artist: str, *, strict: bool = True) -> bool:
        return best_reading_match(keys, Track("x", title, artist),
                                  min_title=0.60, min_artist=0.60,
                                  strict_title=strict)[0]

    check("strong: the real one passes", gate("黑色毛衣", "周杰伦"))
    check("strong: title-with-qualifier + different singer is rejected",
          not gate("黑色毛衣(R&B)", "Haohao Music"))
    check("strong: an identical title with a different singer is rejected",
          not gate("黑色毛衣", "Haohao Music"))
    check("strong: a qualifier in the title is rejected even with the right singer",
          not gate("黑色毛衣(R&B)", "周杰伦"))
    check("strong: artist embedded in the title is still accepted",
          gate("黑色毛衣 - 周杰伦", ""))
    check("strong: whitespace differences do not matter", gate("黑色毛衣 ", "周杰伦 "))
    check("strong: an explicitly requested variant is allowed through",
          gate("黑色毛衣(R&B)", "周杰伦", strict=False))

    REAL = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x33" * 40000
    BAD = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x44" * 40000

    class FakeSource(SearchSource):
        def __init__(self, name, title, artist, path):
            super().__init__(None, {})
            self.name, self.label = name, name
            self._title, self._artist, self._path = title, artist, str(path)

        def search(self, query, *, quality="mp3", limit=10):
            return [Track(self.name, self._title, self._artist, ext="mp3",
                          raw={"path": self._path})]

        def resolve(self, track, *, quality="mp3"):
            track.url = Path(track.raw["path"]).resolve().as_uri()
            track.ext = "mp3"
            return track

    base = dict(enable_fallback=True, write_metadata=False, fetch_cover=False,
                remember_hashes=False)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        rp, bp = tmp / "real.mp3", tmp / "bad.mp3"
        rp.write_bytes(REAL)
        bp.write_bytes(BAD)
        src = tmp / "周杰伦 - 黑色毛衣.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")

        logs: list[str] = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out1"), **base),
                        cache_path=tmp / "c1.json", on_log=logs.append,
                        sources=[FakeSource("zaa", "黑色毛衣(R&B)", "Haohao Music", bp),
                                 FakeSource("zbb", "黑色毛衣", "周杰伦", rp)])
        res = pipe.process_file(src)
        got = Path(res.out_path).read_bytes() if res.out_path else b""
        check("strong: the R&B cover is never the downloaded file", got == REAL,
              " | ".join(logs[-5:]))

        logs2: list[str] = []
        pipe2 = Pipeline(Settings(output_dir=str(tmp / "out2"), **base),
                         cache_path=tmp / "c2.json", on_log=logs2.append,
                         sources=[FakeSource("zaa", "黑色毛衣(R&B)", "Haohao Music", bp)])
        res2 = pipe2.process_file(src)
        out2 = tmp / "out2"
        check("strong: with only a mismatching source, nothing is written",
              res2.status == "failed" and not (out2.is_dir() and list(out2.glob("*"))),
              f"{res2.status} :: {res2.message}")


def test_query_expander() -> None:
    """P3：查询扩展 —— 同一首歌的多种写法，带出处，且去重。"""
    print("\n[query expansion]")
    from mmusic.match import parse_song_filename_candidates
    from mmusic.search.query import MAX_PER_SOURCE, expand

    readings = parse_song_filename_candidates("周杰伦 - 黑色毛衣")
    variants = expand("周杰伦 黑色毛衣", readings)
    reasons = [v.reason for v in variants]
    texts = [v.text for v in variants]
    check("query: variants are ordered cheapest-first",
          reasons[:2] == ["原样", "歌名 歌手"], str(reasons))
    check("query: the title-only variant exists", "仅歌名" in reasons, str(reasons))
    check("query: duplicates are collapsed", len(texts) == len(set(texts)), str(texts))
    check("query: capped by the caller limit", len(expand("x y", readings, limit=2)) == 2)
    check("query: per-source attempt cap is small", MAX_PER_SOURCE <= 3, str(MAX_PER_SOURCE))
    check("query: a bare filename still yields a usable variant",
          expand("黑色毛衣", [("黑色毛衣", "")])[0].text == "黑色毛衣")


def test_new_sources() -> None:
    """P3：互联网档案馆 / 维基共享 / 聚合通道 —— 离线夹具解析。"""
    print("\n[new sources: archive.org / wikimedia / joox aggregator]")
    from mmusic.search.aggregator import AggregatorSource
    from mmusic.search.internet_archive import InternetArchiveSource
    from mmusic.search.pack import validate_spec
    from mmusic.search.wikimedia import WikimediaCommonsSource

    class FakeIA:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_json(self, url, **kw):
            self.calls.append(url)
            if "advancedsearch" in url:
                return {"response": {"docs": [
                    {"identifier": "lp_x", "title": "Moonlight Sonata", "creator": "Beethoven"}]}}
            return {"files": [
                {"name": "x_sample.mp3", "format": "VBR MP3", "length": "30.0", "size": "100"},
                {"name": "disc/01 track.flac", "format": "Flac", "length": "451.54",
                 "size": "9000000"},
                {"name": "cover.jpg", "format": "JPEG"}]}

    ia_http = FakeIA()
    ia = InternetArchiveSource(ia_http, {})
    tracks = ia.search("Moonlight Sonata")
    check("IA: search maps identifier/title/creator",
          len(tracks) == 1 and tracks[0].title == "Moonlight Sonata"
          and tracks[0].raw["identifier"] == "lp_x", str(tracks))
    track = ia.resolve(tracks[0])
    check("IA: the sample/thumbnail are skipped, the flac is chosen",
          track.url.endswith(".flac") and track.ext == "flac", track.url)
    check("IA: the official `length` becomes a real measured duration",
          track.actual_duration == 451.0 and track.duration == 451,
          f"{track.actual_duration} / {track.duration}")

    class FakeWM:
        def get_json(self, url, **kw):
            return {"query": {"pages": {"1": {
                "title": "File:Beethoven Op 62.ogg",
                "imageinfo": [{"url": "https://upload.wikimedia.org/a/b/x.ogg",
                               "mime": "audio/ogg", "size": 123456,
                               "extmetadata": {"Artist": {"value": "<a href='#'>Beethoven</a>"}}}]}}}}

    wm = WikimediaCommonsSource(FakeWM(), {})
    wm_tracks = wm.search("Beethoven")
    check("Commons: file title is cleaned and the artist is de-HTML'd",
          wm_tracks and wm_tracks[0].title == "Beethoven Op 62"
          and wm_tracks[0].artist == "Beethoven", str(wm_tracks))
    check("Commons: the direct upload URL is used as-is",
          wm_tracks[0].url.endswith(".ogg")
          and wm.resolve(wm_tracks[0]).url == wm_tracks[0].url)

    spec = validate_spec({"id": "aggregator_joox", "label": "聚合(JOOX)", "kind": "adapter",
                          "impl": "aggregator", "params": {"provider": "joox"}})
    agg = AggregatorSource(None, {"aggregator_provider": "netease"}, spec)
    check("aggregator: the pack entry overrides the default provider",
          agg.provider == "joox" and agg.name == "aggregator", agg.provider)


def test_migu_source() -> None:
    """咪咕：离线夹具 —— 检索解析 + listen-url 取链 + 免登录降级逻辑。"""
    print("\n[migu: search + listen-url resolve]")
    from mmusic.search.migu import MiguSource

    SEARCH_JSON = {
        "code": "000000",
        "songResultData": {"result": [
            {"id": "1104716107", "contentId": "600929000005945142",
             "copyrightId": "69085303837", "resourceType": "2",
             "name": "光年之外",
             "singers": [{"id": "1", "name": "G.E.M.邓紫棋"}],
             "albums": [{"id": "2", "name": "光年之外"}],
             "newRateFormats": [{"formatType": "PQ", "size": "3769366",
                                 "fileType": "mp3"}]},
            {"id": "9", "contentId": "600", "copyrightId": "690",
             "resourceType": "2", "name": "光年之外(伴奏)",
             "singers": [{"name": "某某"}], "albums": []},
        ]},
    }
    OK_JSON = {"code": "000000", "info": "操作成功", "data": {
        "formatType": "PQ", "url": "https://freetyst.nf.migu.cn/x/y.mp3?Key=k"}}
    LOGIN_JSON = {"code": "000000", "info": "操作成功", "data": {
        "cannotCode": "440003", "dialogInfo": {"text": "登录后免费试听"}}}
    DEAD_JSON = {"code": "000000", "info": "操作成功", "data": {"cannotCode": "440004"}}

    class FakeHttp:
        def __init__(self, listen_replies):
            self.calls = []
            self.listen_replies = list(listen_replies)

        def get_json(self, url, *, headers=None, **kw):
            self.calls.append((url, dict(headers or {})))
            if "search_all.do" in url:
                return SEARCH_JSON
            return self.listen_replies.pop(0) if self.listen_replies else DEAD_JSON

    http = FakeHttp([OK_JSON])
    src = MiguSource(http, {})
    tracks = src.search("光年之外", limit=8)
    check("migu: search maps name/singers/album and keeps the raw item",
          len(tracks) == 2 and tracks[0].title == "光年之外"
          and tracks[0].artist == "G.E.M.邓紫棋" and tracks[0].album == "光年之外"
          and tracks[0].raw["contentId"] == "600929000005945142", str(tracks[:1]))
    check("migu: listen requests always carry the channel header",
          all("channel" in h for _u, h in http.calls), str(http.calls))

    track = src.resolve(tracks[0])
    check("migu: resolve returns the CDN url with the declared size",
          track.url.startswith("https://freetyst.nf.migu.cn/")
          and track.size == 3769366 and track.ext == "mp3" and track.bitrate == 128,
          "%s / %s" % (track.url, track.size))

    http2 = FakeHttp([LOGIN_JSON, OK_JSON])
    src2 = MiguSource(http2, {})
    t2 = src2.search("光年之外")[0]
    t2 = src2.resolve(t2, quality="flac")
    check("migu: a login-walled SQ degrades to PQ instead of giving up",
          t2.ext == "mp3" and t2.url.startswith("https://freetyst.nf.migu.cn/"),
          "%s / %s" % (t2.ext, t2.url))

    http3 = FakeHttp([DEAD_JSON, DEAD_JSON])
    src3 = MiguSource(http3, {})
    t3 = src3.search("光年之外")[0]
    try:
        src3.resolve(t3)
        ok = False
    except Exception as exc:                       # noqa: BLE001
        ok = "无免费直链" in str(exc)
    check("migu: no-free tracks raise a readable error", ok)


def test_language_prefilter() -> None:
    """华语预匹配（静默）：汉字检测 + 内置歌手名单；只影响排序，不参与校验。"""
    print("\n[language prefilter: chinese-likely detection]")
    from mmusic.lang import contains_han, is_chinese_likely

    check("lang: han detected in mixed text",
          contains_han("晴天") and contains_han("G.E.M.邓紫棋") and contains_han("大碟·测试"),
          "")
    check("lang: pure latin text has no han",
          not contains_han("Hotel California") and not contains_han("Jay Chou"), "")
    check("lang: chinese title -> chinese-likely", is_chinese_likely("晴天 - 周杰伦"))
    check("lang: romanized artist in the built-in list still matches",
          is_chinese_likely("Mojito", "Jay Chou")
          and is_chinese_likely("Light Years Away", "g.e.m."), "")
    check("lang: western songs stay non-chinese",
          not is_chinese_likely("Hotel California", "Eagles")
          and not is_chinese_likely("Shape of You", "Ed Sheeran")
          and not is_chinese_likely("Yesterday Once More", "Carpenters"), "")


def test_cjk_order_e2e() -> None:
    """华语歌端到端：咪咕先行、CC/PD 源按需检索；非华语歌维持原顺序。"""
    print("\n[cjk order: migu first + CC/PD deferral]")
    PAYLOAD = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xbb" * 40000

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        def run_case(name, filename, plan, source_names):
            base = tmp / name
            base.mkdir()
            media = base / "payload.mp3"
            media.write_bytes(PAYLOAD)
            calls: list[str] = []

            class FakeSource(SearchSource):
                def __init__(self, n):
                    super().__init__(None, {})
                    self.name = n
                    self.label = n

                def search(self, query, *, quality="mp3", limit=10):
                    calls.append(self.name)
                    hit = plan.get(self.name)
                    if not hit:
                        return []
                    title, artist = hit
                    return [Track(self.name, title, artist, ext="mp3",
                                  raw={"path": str(media)})]

                def resolve(self, track, *, quality="mp3"):
                    track.url = Path(track.raw["path"]).resolve().as_uri()
                    track.ext = "mp3"
                    return track

            src = base / filename
            src.write_bytes(os.urandom(8188) + b"STag")
            logs: list[str] = []
            pipe = Pipeline(Settings(output_dir=str(base / "out"), enable_fallback=True,
                                     write_metadata=False, fetch_cover=False,
                                     remember_hashes=False),
                            cache_path=base / "c.json", on_log=logs.append,
                            sources=[FakeSource(n) for n in source_names])
            res = pipe.process_file(src)
            return calls, logs, res

        ORDER = ["kuwo", "migu", "wikimedia", "internet_archive"]

        # ① 华语歌 + 主流命中：migu 提前；CC/PD 不再检索
        calls, logs, res = run_case(
            "sc1", "晴天 - 周杰伦.mgg",
            {"migu": ("晴天", "周杰伦"), "kuwo": ("晴天", "周杰伦")}, ORDER)
        check("cjk-order: migu is searched before kuwo",
              calls[:2] == ["migu", "kuwo"], str(calls))
        check("cjk-order: CC/PD sources are skipped once a candidate exists",
              "wikimedia" not in calls and "internet_archive" not in calls, str(calls))
        check("cjk-order: the song is still downloaded",
              res.status == "downloaded" and bool(res.out_path), res.status)

        # ② 华语歌 + 主流全空：CC/PD 仍作为最后兜底被检索
        calls, logs, res = run_case(
            "sc2", "晴天 - 周杰伦.mgg", {"wikimedia": ("晴天", "周杰伦")}, ORDER)
        check("cjk-order: CC/PD is searched when mainstream finds nothing",
              "wikimedia" in calls and calls.index("kuwo") < calls.index("wikimedia"),
              str(calls))
        check("cjk-order: after the CC source hits, the later CC/PD source is skipped",
              "internet_archive" not in calls, str(calls))

        # ③ 非华语歌：顺序与检索行为保持用户配置
        calls, logs, res = run_case(
            "sc3", "Hotel California - Eagles.mgg",
            {"kuwo": ("Hotel California", "Eagles")}, ORDER)
        check("cjk-order: non-chinese songs keep the configured order",
              calls[:2] == ["kuwo", "migu"], str(calls))
        check("cjk-order: non-chinese songs still consult CC/PD sources",
              "wikimedia" in calls and "internet_archive" in calls, str(calls))
        check("cjk-order: no chinese-prefilter log for western songs",
              not any("华语歌预匹配" in m for m in logs), " | ".join(logs[-3:]))


def test_manual_links() -> None:
    """P3：手动补链 —— 用户给的链接优先使用、跳过歌名/歌手门槛，但仍走下载校验。"""
    print("\n[manual links]")
    from ui.manual_links_dialog import split_entries

    check("manual: the dialog accepts one entry per line",
          split_entries("a.mp3\n b.flac ; c.ogg ,, a.mp3") == ["a.mp3", "b.flac", "c.ogg"],
          str(split_entries("a.mp3\n b.flac ; c.ogg ,, a.mp3")))

    PAYLOAD = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x99" * 40000
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        mine = tmp / "我自己的版本.mp3"
        mine.write_bytes(PAYLOAD)
        src = tmp / "周杰伦 - 黑色毛衣.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")

        logs: list[str] = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out"), sources=["manual"],
                                 manual_links=[str(mine)], enable_fallback=True,
                                 write_metadata=False, fetch_cover=False,
                                 remember_hashes=False),
                        cache_path=tmp / "c.json", on_log=logs.append)
        res = pipe.process_file(src)
        got = Path(res.out_path).read_bytes() if res.out_path else b""
        check("manual: the supplied file is what gets written", got == PAYLOAD,
              " | ".join(logs[-4:]))
        check("manual: skipping the gates is logged",
              any("跳过歌名/歌手门槛" in m for m in logs), " | ".join(logs[-4:]))
        check("manual: the source list puts it first",
              pipe.sources and pipe.sources[0].name == "manual",
              str([s.name for s in pipe.sources]))


def test_variant_retry_e2e() -> None:
    """P3：一个源在第一种写法下无结果时，会自动换下一种写法再试。"""
    print("\n[query variants: retry on the next phrasing]")
    PAYLOAD = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xab" * 40000

    class PickySource(SearchSource):
        """只在"歌名 歌手"这一种写法下返回结果。"""

        def __init__(self, path):
            super().__init__(None, {})
            self.name = self.label = "picky"
            self.calls: list[str] = []
            self._path = str(path)

        def search(self, query, *, quality="mp3", limit=10):
            self.calls.append(query)
            if query != "黑色毛衣 周杰伦":
                return []
            return [Track(self.name, "黑色毛衣", "周杰伦", ext="mp3", raw={"path": self._path})]

        def resolve(self, track, *, quality="mp3"):
            track.url = Path(track.raw["path"]).resolve().as_uri()
            track.ext = "mp3"
            return track

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        p = tmp / "a.mp3"
        p.write_bytes(PAYLOAD)
        src = tmp / "周杰伦 - 黑色毛衣.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")
        source = PickySource(p)
        logs: list[str] = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out"), enable_fallback=True,
                                 write_metadata=False, fetch_cover=False,
                                 remember_hashes=False),
                        cache_path=tmp / "c.json", on_log=logs.append, sources=[source])
        res = pipe.process_file(src)
        got = Path(res.out_path).read_bytes() if res.out_path else b""
        check("variants: a second phrasing is tried automatically", len(source.calls) >= 2,
              str(source.calls))
        check("variants: the retry hits and is reported",
              any("命中于检索变体" in m for m in logs), " | ".join(logs[-5:]))
        check("variants: the file is downloaded", got == PAYLOAD, " | ".join(logs[-3:]))


def test_import_all_modules() -> None:
    """每个模块都必须能导入。

    这类"名字写错 / 从错误的模块导入"的错最廉价也最致命（曾真实发生：
    ui.settings_dialog 从 pack 导入 registry 里的 specs_for，源码自检全绿、
    但打包后的程序一启动就弹错误框）。放在最便宜的自检里，先拦住它。
    """
    print("\n[import smoke: every module]")
    import importlib

    root = Path(__file__).resolve().parent.parent
    names = ["main"]
    for pkg in ("mmusic", "ui"):
        for f in sorted((root / pkg).rglob("*.py")):
            parts = [p for p in f.relative_to(root).with_suffix("").parts if p != "__init__"]
            if parts:
                names.append(".".join(parts))
    failures: list[str] = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:                       # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    check("imports: every module imports cleanly", not failures, "; ".join(failures[:3]))
    print(f"  (imported {len(names)} modules)")


def test_kwm_decoder() -> None:
    """P0-2：酷我 .kwm 离线解码（密钥内嵌，零网络）。"""
    print("\n[kwm: offline container]")
    import struct as _struct
    from mmusic.crypto.kwm import (KWM_HEADER_SIZE, KWM_MAGIC_TME, KwmDecoder,
                                   is_kwm, mask_from_key, pad_end)

    check("kwm: padEnd 语义（整串循环补齐）",
          pad_end("118983") == "11898311898311898311898311898311"
          and len(pad_end("1" * 20)) == 32 and len(pad_end("1" * 40)) == 32,
          f"{pad_end('118983')!r}")

    KEY_ID = 118983
    payload = b"ID3\x03\x00\x00\x00\x00\x00\x00" + bytes(range(256)) * 20
    key = _struct.pack("<Q", KEY_ID)
    mask = mask_from_key(key)
    body = bytes(payload[i] ^ mask[i % 32] for i in range(len(payload)))
    blob = bytearray(KWM_MAGIC_TME + b"\x00" * 8)
    blob[0x18:0x20] = key
    blob += b"\x00" * (KWM_HEADER_SIZE - len(blob))
    blob += body
    blob = bytes(blob)

    check("kwm: 魔数被识别", is_kwm(blob))
    res = KwmDecoder(blob).decode()
    check("kwm: 解出的音频与原文逐字节一致", res.audio == payload, f"{len(res.audio)} vs {len(payload)}")
    check("kwm: 扩展名按真实文件头判定为 mp3", res.ext == "mp3", res.ext)
    check("kwm: 密钥编号可读出", res.meta.get("kwm_key_id") == KEY_ID, str(res.meta))

    bad = bytearray(blob); bad[0x18] ^= 0xFF          # 改一个密钥字节
    bad_res = KwmDecoder(bytes(bad)).decode()
    check("kwm: 改密钥 -> 解密结果随之改变（证明密钥确实参与解密）",
          bad_res.audio != payload and bad_res.meta.get("kwm_key_id") != KEY_ID,
          f"{bad_res.meta}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "周杰伦-夜曲.kwm"
        src.write_bytes(blob)
        pipe = Pipeline(Settings(output_dir=str(tmp / "out"), enable_fallback=False,
                                 write_metadata=False, fetch_cover=False,
                                 remember_hashes=False), cache_path=tmp / "c.json")
        r = pipe.process_file(src)
        out = Path(r.out_path) if r.out_path else None
        check("kwm: 流水线走离线解密分支",
              r.status == "decoded" and r.mode == "offline", f"{r.status}/{r.mode}")
        check("kwm: 产物与原文一致",
              out is not None and out.read_bytes() == payload, str(out))


def test_musicex_container() -> None:
    """musicex（2026 新版 QQ）：尾部解析 / 无 ekey 报“需联网” / 有 ekey 完整解码。"""
    print("\n[musicex: 2026 QQ container]")
    from mmusic import ekeys
    from mmusic.crypto.qmc import QmcDecoder
    from mmusic.sniff import sniff_path

    key = bytes(range(32))                                     # -> map cipher
    key_big = bytes((i * 7 + 1) & 0xFF for i in range(512))    # -> rc4 cipher

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # ① 无 ekey：如实标记“需联网”，绝不再产出垃圾文件
        p1 = tmp / "无ekey.mgg"
        p1.write_bytes(fx.build_musicex(fx.PAYLOAD_OGG))
        info = sniff_path(p1)
        check("musicex: sniffed as needs-online",
              info.needs_online and info.kind == "qmc", str(info))
        try:
            QmcDecoder(p1.read_bytes(), "mgg", src_path=p1).decode()
            ok, msg = False, "no exception"
        except NeedsOnlineError as exc:
            ok, msg = ("ekey" in exc.message), exc.message
        check("musicex: decode without ekey raises a readable error", ok, msg)

        # ② 本地 ekeys/<mid>__<name>.txt：完整离线解码（map 密文）
        p2 = tmp / "有ekey.mgg"
        p2.write_bytes(fx.build_musicex(fx.PAYLOAD_OGG, key))
        (tmp / "ekeys").mkdir()
        (tmp / "ekeys" / "00TEST00000001__TEST00001.mgg.txt").write_text(
            fx.make_qmc_v2_key(key).decode("ascii"), encoding="utf-8")
        dec2 = QmcDecoder(p2.read_bytes(), "mgg", src_path=p2)
        check("musicex: tail fields parsed (mid/size)",
              dec2.song_mid == "00TEST00000001" and dec2.audio_size == len(fx.PAYLOAD_OGG),
              f"{dec2.song_mid} / {dec2.audio_size}")
        check("musicex: sniff_path sees the local ekey",
              not sniff_path(p2).needs_online, str(sniff_path(p2)))
        r2 = QmcDecoder(p2.read_bytes(), "mgg", src_path=p2).decode()
        check("musicex: payload decodes byte-exact with a local ekey",
              r2.audio == fx.PAYLOAD_OGG)
        check("musicex: success path reports the ekey in meta (log transparency)",
              "ekey" in (r2.meta or {}).get("note", ""), str(r2.meta))

        # ③ B 变体（tail=200）+ <mid>.txt 命名 + RC4 密钥
        p3 = tmp / "B变体.mflac"
        p3.write_bytes(fx.build_musicex(fx.PAYLOAD_FLAC, key_big, variant=200,
                                        song_mid="00TEST00000002",
                                        filename="TEST00002.mflac"))
        (tmp / "ekeys" / "00TEST00000002.txt").write_text(
            fx.make_qmc_v2_key(key_big).decode("ascii"), encoding="utf-8")
        r3 = QmcDecoder(p3.read_bytes(), "mflac", src_path=p3).decode()
        check("musicex: variant B (200) parses and decodes byte-exact",
              r3.audio == fx.PAYLOAD_FLAC)

        # ④ 本地 ekey 已过期（>22h）：不再假装“可离线解码”，保持 needs-online，
        #    以便流水线按需重取 / 转备用链路（陈旧 ekey 不得绕过重取）。
        stale_ekey = tmp / "ekeys" / "00TEST00000001__TEST00001.mgg.txt"
        old_ts = time.time() - (ekeys.VALID_SECONDS + 3600)
        os.utime(stale_ekey, (old_ts, old_ts))
        info4 = sniff_path(p2)
        check("musicex: an expired local ekey is no longer treated as offline-ready",
              info4.needs_online and "过期" in info4.note, str(info4))


def test_qmc_no_silent_garbage() -> None:
    """未知尾部不再静默产出垃圾文件：解码结果过不了音频头校验就明确报错。"""
    print("\n[qmc guard: no silent garbage]")
    from mmusic.crypto.qmc import QmcDecoder
    from mmusic.errors import DecryptionFailedError

    junk = bytearray(os.urandom(66000))
    junk[-4:] = b"\xff\xff\x00\x00"       # 末4字节 >= 0x400 → 走“静态加密”分支
    try:
        QmcDecoder(bytes(junk), "mgg").decode()
        ok, msg = False, "no exception"
    except DecryptionFailedError as exc:
        ok, msg = ("不是有效音频" in exc.message), exc.message
    check("qmc-guard: garbage never becomes a written file", ok, msg)


def test_target_format_matrix() -> None:
    """目标音质矩阵（纯逻辑）：MP3 全转 / 无损升级 / 有损降级 / 引擎缺失回退。"""
    print("\n[target quality: matrix + engine fallback]")
    from mmusic.transcode import plan, plan_effective

    check("quality: mp3 target converts everything (flac too)",
          plan("flac", "mp3") == ("mp3", "") and plan("ogg", "mp3")[0] == "mp3"
          and plan("wav", "mp3")[0] == "mp3")
    check("quality: already-matching format is a no-op",
          plan("mp3", "mp3") == ("keep", "") and plan("flac", "flac") == ("keep", ""))
    check("quality: 源已是 MP3 + 无损目标 → 保持原样（不做无效转码，体积不翻倍）",
          plan("mp3", "flac")[0] == "keep" and plan("mp3", "wav")[0] == "keep"
          and "无效转换" in plan("mp3", "flac")[1],
          f"{plan('mp3','flac')} {plan('mp3','wav')}")
    check("quality: flac target upgrades lossless, degrades lossy with a note",
          plan("wav", "flac") == ("flac", "")
          and plan("ogg", "flac")[0] == "mp3" and "有损" in plan("ogg", "flac")[1])
    check("quality: wav target keeps lossless as-is, degrades lossy",
          plan("wav", "wav") == ("keep", "") and plan("flac", "wav") == ("keep", "")
          and plan("m4a", "wav")[0] == "mp3")
    action, note = plan_effective("ogg", "mp3", engine_available=False)
    check("quality: engine missing -> keep original with a readable note",
          action == "keep" and "引擎不可用" in note, note)


def _make_test_wav(seconds: float = 1.0) -> bytes:
    import io as _io, math as _math, struct as _struct, wave as _wave
    frames = bytearray()
    for i in range(int(44100 * seconds)):
        v = int(9000 * _math.sin(2 * _math.pi * 440 * i / 44100))
        frames += _struct.pack("<hh", v, v)
    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def test_transcode_engine() -> None:
    """转码引擎端到端：stdlib 生成 WAV → MP3 / FLAC，并回环解码验证。"""
    print("\n[transcode engine: wav -> mp3 / flac / roundtrip]")
    from mmusic import transcode
    from mmusic.duration import audio_duration, flac_streaminfo, mp3_frame_info

    if not transcode.available():
        check("transcode: engine unavailable -> graceful skip", True,
              transcode.unavailable_reason())
        print("  （本机引擎不可用，跳过端到端用例）")
        return

    wav = _make_test_wav()
    mp3, ext = transcode.convert_bytes(wav, "wav", "mp3")
    info = mp3_frame_info(mp3)
    check("transcode: wav -> 320k mp3 (MPEG1 L3)",
          ext == "mp3" and bool(info) and info["bitrate"] == 320
          and info["sample_rate"] == 44100, str(info))
    check("transcode: wav -> mp3 duration ~1s",
          (audio_duration(mp3, len(mp3)) or 0) > 0.7, str(audio_duration(mp3, len(mp3))))
    flac, ext2 = transcode.convert_bytes(wav, "wav", "flac")
    si = flac_streaminfo(flac)
    check("transcode: wav -> flac (hand-built container)",
          ext2 == "flac" and flac[:4] == b"fLaC" and bool(si)
          and si["sample_rate"] == 44100 and si["channels"] == 2, str(si))
    back, _ = transcode.convert_bytes(flac, "flac", "mp3")
    check("transcode: our flac decodes back (container + frames valid)",
          (audio_duration(back, len(back)) or 0) > 0.7, str(audio_duration(back, len(back))))


def test_pipeline_quality_e2e() -> None:
    """流水线端到端：明文 WAV + 目标 MP3 → 产出 .mp3（目标音质真正生效）。"""
    print("\n[target quality: pipeline end-to-end]")
    from mmusic import transcode
    if not transcode.available():
        check("quality-e2e: engine unavailable -> skip", True)
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "song.wav"
        src.write_bytes(_make_test_wav())
        logs: "list[str]" = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out"), quality="mp3",
                                 enable_fallback=False, write_metadata=False,
                                 fetch_cover=False, remember_hashes=False),
                        cache_path=tmp / "c.json", on_log=logs.append, sources=[])
        res = pipe.process_file(src)
        out = Path(res.out_path) if res.out_path else None
        check("quality-e2e: plain wav + mp3 target -> .mp3 produced",
              out is not None and out.exists() and out.suffix == ".mp3"
              and out.stat().st_size > 1000, f"{res.status} {res.out_path}")


def test_ekey_fetch() -> None:
    """ekey 按需获取：请求体/响应解析/凭证归一化/缓存新鲜度/失败降级（全离线夹具）。"""
    print("\n[ekey: on-demand fetch (offline fixtures)]")
    import json as _json
    from mmusic import ekeys
    from mmusic.crypto.musicex import find_ekey_path

    body = ekeys.build_request("12345", "00TEST00000001", "TEST00001.mgg")
    check("ekey: request follows the GetEVkey shape",
          body["req_1"]["module"] == "music.vkey.GetEVkey"
          and body["req_1"]["method"] == "CgiGetEVkey"
          and body["req_1"]["param"]["songmid"] == ["00TEST00000001"]
          and body["req_1"]["param"]["filename"] == ["TEST00001.mgg"]
          and body["comm"]["uin"] == "12345", str(body)[:100])
    check("ekey: response parser returns the ekey",
          ekeys.parse_response({"req_1": {"code": 0,
                                          "data": {"midurlinfo": [{"ekey": "ABC123"}]}}}) == "ABC123")
    bad_ok = 0
    for bad in ({"req_1": {"code": 1000, "info": "未登录"}},
                {"req_1": {"code": 0, "data": {"midurlinfo": []}}}):
        try:
            ekeys.parse_response(bad)
        except ekeys.EkeyError:
            bad_ok += 1
    check("ekey: a bad response raises EkeyError", bad_ok == 2)
    check("ekey: a whole pasted cookie is normalised",
          ekeys.normalize_creds("", "qqmusic_key=K1; qqmusic_uin=777")
          == ("777", "qqmusic_key=K1; qqmusic_uin=777")
          and ekeys.normalize_creds("777", "K2")[1] == "qqmusic_key=K2; qqmusic_uin=777")

    class EkeyHttp:
        def __init__(self, payload):
            self.calls = []
            self.payload = payload

        def request(self, method, url, *, data=None, headers=None, **kw):
            self.calls.append({"method": method, "url": url,
                               "cookie": (headers or {}).get("Cookie", ""),
                               "body": _json.loads((data or b"{}").decode("utf-8"))})
            return 200, _json.dumps(self.payload).encode("utf-8"), {}

    ok_payload = {"req_1": {"code": 0, "data": {"midurlinfo": [{"ekey": "SYNTH-EKEY"}]}}}

    class Creds:
        qqmusic_uin = "12345"
        qqmusic_key = "KEY-XYZ"

    class NoCreds:
        qqmusic_uin = ""
        qqmusic_key = ""

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "song.mgg"
        src.write_bytes(fx.build_musicex(fx.PAYLOAD_OGG))
        http = EkeyHttp(ok_payload)
        ok, why = ekeys.ensure_ekey(http, NoCreds(), src, retry_delay=0.0)
        check("ekey: without credentials nothing is requested",
              ok is False and "凭证" in why and not http.calls, why)
        ok, why = ekeys.ensure_ekey(http, Creds(), src, retry_delay=0.0)
        written = find_ekey_path(src, "00TEST00000001", "TEST00001.mgg")
        check("ekey: the fetched ekey is written to ekeys/<mid>__<file>.txt",
              ok and written is not None
              and written.read_text("utf-8").strip() == "SYNTH-EKEY", f"{why} | {written}")
        calls = len(http.calls)
        ok2, why2 = ekeys.ensure_ekey(http, Creds(), src, retry_delay=0.0)
        check("ekey: a fresh cached ekey is reused (no extra request)",
              ok2 and len(http.calls) == calls, why2)
        ok_f, why_f = ekeys.ensure_ekey(http, Creds(), src, retry_delay=0.0, force=True)
        check("ekey: force=True re-fetches even while a fresh cache exists (self-heal)",
              ok_f is True and len(http.calls) > calls, why_f)
        check("ekey: the request carried the cookie to the official endpoint",
              bool(http.calls) and http.calls[0]["method"] == "POST"
              and http.calls[0]["url"] == ekeys.API
              and http.calls[0]["cookie"] == "qqmusic_key=KEY-XYZ; qqmusic_uin=12345"
              and http.calls[0]["body"]["req_1"]["param"]["filename"] == ["TEST00001.mgg"],
              str(http.calls[:1])[:110])
        bad_src = tmp / "song2.mgg"
        bad_src.write_bytes(fx.build_musicex(fx.PAYLOAD_OGG, song_mid="00TEST00000002",
                                            filename="TEST00002.mgg"))
        ok3, why3 = ekeys.ensure_ekey(EkeyHttp({"req_1": {"code": 1000, "info": "need login"}}),
                                      Creds(), bad_src, retry_delay=0.0)
        check("ekey: an API rejection degrades with a readable reason",
              ok3 is False and "失败" in why3, why3)


def test_ekey_pipeline_e2e() -> None:
    """流水线：musicex + 已配置凭证 + 假 HTTP → 自动取 ekey 并当次解码。"""
    print("\n[ekey: pipeline auto-fetch end-to-end]")
    import json as _json
    from mmusic import ekeys
    from mmusic.config import Settings

    key = bytes(range(32))
    ekey_text = fx.make_qmc_v2_key(key).decode("ascii")

    class EkeyHttp:
        def __init__(self):
            self.calls = 0

        def request(self, method, url, *, data=None, headers=None, **kw):
            self.calls += 1
            payload = {"req_1": {"code": 0, "data": {"midurlinfo": [{"ekey": ekey_text}]}}}
            return 200, _json.dumps(payload).encode("utf-8"), {}

        def close(self):
            pass

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "song.mgg"
        src.write_bytes(fx.build_musicex(fx.PAYLOAD_OGG, key))
        settings = Settings(output_dir=str(tmp / "out"), quality="mp3",
                            enable_fallback=False, write_metadata=False,
                            fetch_cover=False, remember_hashes=False,
                            timeout=5.0, retries=0, min_bitrate=0,
                            qqmusic_uin="12345", qqmusic_key="K")
        logs: "list[str]" = []
        pipe = Pipeline(settings, cache_path=tmp / "c.json", on_log=logs.append,
                        sources=[], http=EkeyHttp())
        res = pipe.process_file(src)
        check("ekey-e2e: the musicex file decodes after the silent fetch",
              res.status == "decoded", f"{res.status} {res.message}")
        check("ekey-e2e: the ekey file was written next to the source",
              (tmp / "ekeys" / "00TEST00000001__TEST00001.mgg.txt").is_file())
        check("ekey-e2e: the log reports the silent fetch",
              any("已静默获取 ekey" in m for m in logs), " | ".join(logs[-3:]))

        # ② 本地 ekey 陈旧且已失效 + 凭证有效：应静默重取一次并当次解码成功（自愈）
        stale_dir = tmp / "stale"
        stale_dir.mkdir()
        src2 = stale_dir / "song.mgg"
        src2.write_bytes(fx.build_musicex(fx.PAYLOAD_OGG, key))
        (stale_dir / "ekeys").mkdir()
        bad_ek = stale_dir / "ekeys" / "00TEST00000001__TEST00001.mgg.txt"
        bad_ek.write_text(fx.make_qmc_v2_key(bytes(range(1, 33))).decode("ascii"),
                          encoding="utf-8")
        old_ts = time.time() - (ekeys.VALID_SECONDS + 3600)
        os.utime(bad_ek, (old_ts, old_ts))
        settings2 = Settings(output_dir=str(stale_dir / "out"), quality="mp3",
                            enable_fallback=False, write_metadata=False,
                            fetch_cover=False, remember_hashes=False,
                            timeout=5.0, retries=0, min_bitrate=0,
                            qqmusic_uin="12345", qqmusic_key="K")
        assert sniff_path(src2).needs_online        # 过期即不再视作离线可解
        http2 = EkeyHttp()
        logs2: "list[str]" = []
        pipe2 = Pipeline(settings2, cache_path=stale_dir / "c.json",
                         on_log=logs2.append, sources=[], http=http2)
        res2 = pipe2.process_file(src2)
        check("ekey-e2e: a stale/failed local ekey self-heals via a silent re-fetch",
              res2.status == "decoded" and http2.calls > 0,
              f"{res2.status} calls={http2.calls}")


def test_source_tag_and_cover_carry() -> None:
    """离线解密：回读源标签（不把 TITLE 覆盖成文件名）+ 转码搬运内嵌封面（纯本地）。"""
    print("\n[offline decode: source tags + embedded cover carry-through]")
    import wave
    from mmusic import transcode
    from mmusic.transcode import EngineUnavailable
    from mutagen.flac import FLAC as _FLAC, Picture as _Picture

    if not transcode.available():
        print("  (skip) 转码引擎不可用，构造不出真实带封面的 FLAC 样本")
        return

    # 1) 纯 Python 生成 0.3s 真 WAV -> 引擎转成真 FLAC -> 用 mutagen 打标签 + 内嵌封面
    with tempfile.TemporaryDirectory() as t0:
        wav = Path(t0) / "a.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)           # MP3 编码器（mp3_mf）不接受 8k/16k
            w.writeframes(b"\x00\x00" * 13230)   # ~0.3s
        try:
            flac_bytes, _ = transcode.convert_bytes(wav.read_bytes(), "wav", "flac")
        except EngineUnavailable as exc:
            print(f"  (skip) 引擎不可用：{exc}")
            return

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128
    # mutagen 的 FLAC.save() 必须落在真文件上（写 BytesIO 会在头部自检时失败）
    with tempfile.TemporaryDirectory() as t1:
        tagged = Path(t1) / "tagged.flac"
        tagged.write_bytes(flac_bytes)
        f = _FLAC(str(tagged))
        f["title"] = ["夜曲"]
        f["artist"] = ["周杰伦"]
        f["album"] = ["十一月的萧邦"]
        pic = _Picture()
        pic.type = 3
        pic.mime = "image/png"
        pic.data = png
        f.add_picture(pic)
        f.save()
        payload_with = tagged.read_bytes()
    payload_without = flac_bytes            # 没封面、没标签的同一段真音频

    class CountingHttp:
        def __init__(self):
            self.calls = 0

        def request(self, *a, **kw):
            self.calls += 1
            raise AssertionError("离线路径不得联网")

        def close(self):
            pass

    # 引擎自己产出的**裸 FLAC**（无标签、单条流）也必须能转 MP3：
    # 实测缺陷（2026-10-19）—— 堆内存探测曾命中无关内存 → codec_id=0 →
    # 误报"没有可用的解码器"，转码被静默降级成"保留原格式"，且换顺序就好。
    try:
        bare_mp3, _ = transcode.convert_bytes(flac_bytes, "flac", "mp3")
        check("transcode: 引擎产出的裸 FLAC 也能转 MP3（探测不再命中无关内存）",
              len(bare_mp3) > 1000, f"{len(bare_mp3)}B")
    except Exception as exc:  # noqa: BLE001
        check("transcode: 引擎产出的裸 FLAC 也能转 MP3（探测不再命中无关内存）",
              False, f"{type(exc).__name__}: {exc}")

    key = bytes(range(32))
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        http = CountingHttp()

        def run(payload: bytes, quality: str, tag: str):
            src = tmp / f"{tag}.mflac"
            src.write_bytes(fx.build_qmc_v2(payload, key))
            info = sniff_path(src)
            settings = Settings(output_dir=str(tmp / ("out_" + tag)), quality=quality,
                                enable_fallback=False, write_metadata=True,
                                fetch_cover=True, remember_hashes=False,
                                timeout=5.0, retries=0, min_bitrate=0)
            logs: "list[str]" = []
            pipe = Pipeline(settings, cache_path=tmp / (tag + ".json"),
                            on_log=logs.append, sources=[], http=http)
            return info, pipe.process_file(src)

        # 2) 目标 MP3：标签与内嵌封面必须跟过去，标题不得被文件名覆盖
        info, r = run(payload_with, "mp3", "with")
        out = Path(r.out_path) if r.out_path else None
        check("tagcarry: 源 mflac 走离线解密（不依赖网络）",
              not info.needs_online and r.status == "decoded" and r.mode == "offline",
              f"{info.needs_online} {r.status}/{r.mode}")
        from mutagen.id3 import ID3 as _ID3
        frames = _ID3(str(out)) if out and out.exists() else {}
        apic = [k for k in frames if str(k).startswith("APIC")] if frames else []
        check("tagcarry: TITLE 用源文件的真歌名（未被文件名覆盖）",
              frames and str(frames.get("TIT2")) == "夜曲", str(frames.get("TIT2")) if frames else "no tags")
        check("tagcarry: ARTIST / ALBUM 从源文件回读",
              frames and str(frames.get("TPE1")) == "周杰伦" and str(frames.get("TALB")) == "十一月的萧邦",
              f"{frames.get('TPE1') if frames else None} / {frames.get('TALB') if frames else None}")
        check("tagcarry: 转码成 MP3 后内嵌封面未丢（APIC）",
              len(apic) == 1 and bytes(frames[apic[0]].data) == png,
              f"APIC={apic}")
        check("tagcarry: 产物文件名仍是「歌手 - 歌名」",
              out is not None and out.name == "周杰伦 - 夜曲.mp3", out.name if out else r.message)

        # 3) 目标 FLAC：不转码（keep），源内封面与标签原样保留
        info2, r2 = run(payload_with, "flac", "keep")
        out2 = Path(r2.out_path) if r2.out_path else None
        fa = _FLAC(str(out2)) if out2 and out2.exists() else None
        check("tagcarry: 目标 FLAC 走 keep，源内嵌封面保留",
              fa is not None and len(fa.pictures) == 1 and bytes(fa.pictures[0].data) == png,
              f"pictures={len(fa.pictures) if fa else None}")
        check("tagcarry: 目标 FLAC 标签保持源值",
              fa is not None and fa.get("title") == ["夜曲"] and fa.get("artist") == ["周杰伦"],
              f"{fa.get('title') if fa else None}")

        # 4) 源本身没有封面：不得凭空造、更不得联网找
        _, r3 = run(payload_without, "mp3", "nocover")
        out3 = Path(r3.out_path) if r3.out_path else None
        frames3 = _ID3(str(out3)) if out3 and out3.exists() else {}
        check("tagcarry: 源无封面时产物不加封面（不联网找图）",
              not [k for k in frames3 if str(k).startswith("APIC")] and http.calls == 0,
              f"http_calls={http.calls}")


def test_acquire_by_name() -> None:
    """以名找歌：不依赖任何输入文件，走与备用链路**完全相同**的选源与校验逻辑。"""
    print("\n[acquire by name: 以名找歌]")
    import mmusic.pipeline as pl
    from mmusic.errors import MusicDecryptorError
    from mmusic.search.base import SearchSource

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        full = tmp / "full.mp3"
        full.write_bytes(fx.make_mp3_xing(240))
        cut = tmp / "cut.mp3"
        cut.write_bytes(fx.make_mp3_xing(120))

        class Fake(SearchSource):
            def __init__(self, name, label, title, artist, path, duration):
                super().__init__(None, {})
                self.name = name
                self.label = label
                self._t, self._a, self._p, self._d = title, artist, str(path), duration

            def search(self, query, *, quality="mp3", limit=10):
                return [Track(self.name, self._t, self._a, duration=self._d, bitrate=320,
                              ext="mp3", raw={"path": self._p})]

            def resolve(self, track, *, quality="mp3"):
                track.url = Path(track.raw["path"]).resolve().as_uri()
                track.ext = "mp3"
                return track

        cut_src = Fake("cut1", "截短源", "夜曲", "周杰伦", cut, 120)
        full_src = Fake("full1", "完整源", "夜曲", "周杰伦", full, 240)

        def make_pipe(out_dir, sources):
            st = Settings(output_dir=str(out_dir), quality="mp3", enable_fallback=True,
                          write_metadata=True, fetch_cover=False, remember_hashes=False,
                          timeout=5.0, retries=0, min_bitrate=0)
            logs: "list[str]" = []
            return Pipeline(st, cache_path=tmp / "c.json", on_log=logs.append,
                            sources=sources), logs

        pipe, _logs = make_pipe(tmp / "out", [cut_src, full_src])

        # ① 只检索、不下载：一个文件都不许落盘
        cands = pipe.search_candidates("夜曲", artist="周杰伦")
        check("acquire: search_candidates 返回候选且不落盘",
              len(cands) == 2 and not (tmp / "out").exists(),
              f"{len(cands)} 个候选 / out 目录存在={(tmp / 'out').exists()}")

        # ② 自动挑最优：绕过 120s 截短版，下完整版
        res = pipe.acquire("夜曲", artist="周杰伦")
        out = Path(res.out_path) if res.out_path else None
        check("acquire: 无需输入文件即可下载（downloaded / fallback）",
              res.status == "downloaded" and res.mode == "fallback", f"{res.status}/{res.mode}")
        got_dur = audio_duration(out.read_bytes()) if out and out.exists() else None
        check("acquire: 落盘的是通过时长核验的那条（实测 ≈240s，120s 截短版被淘汰）",
              got_dur is not None and 235 <= got_dur <= 245, f"{got_dur} :: {out}")
        from mutagen.id3 import ID3 as _ID3
        frames = _ID3(str(out)) if out and out.exists() else {}
        check("acquire: 以名找歌的产物同样写入标签（歌名/歌手）",
              frames and str(frames.get("TIT2")) == "夜曲"
              and str(frames.get("TPE1")) == "周杰伦",
              f"{frames.get('TIT2') if frames else None} / {frames.get('TPE1') if frames else None}")
        check("acquire: 产物文件名用「歌手 - 歌名」",
              out is not None and out.name == "周杰伦 - 夜曲.mp3",
              out.name if out else res.message)
        check("acquire: JobResult.path 用 search:// 前缀（不是真实文件路径）",
              str(res.path).startswith(pl.SEARCH_PREFIX), res.path)
        check("acquire: 不删除任何文件", full.exists() and cut.exists())

        # ③ 人工点选"截短版"：必须如实拒绝，不许偷偷换成完整版
        chosen_cut = cut_src.search("夜曲")[0]
        try:
            r3 = pipe.acquire("夜曲", artist="周杰伦", chosen=chosen_cut)
            ok3, msg3 = False, r3.message
        except MusicDecryptorError as exc:
            ok3, msg3 = "未通过时长核验" in exc.message, exc.message
        check("acquire: 人工点选的候选没通过核验时如实拒绝（绝不换别的版本）", ok3, msg3)

        # ④ 人工点选完整版：跨 Pipeline 重新检索后必须按「音源 + 资源指纹」定位
        chosen_full = full_src.search("夜曲")[0]
        r4 = pipe.acquire("夜曲", artist="周杰伦", chosen=chosen_full)
        out4 = Path(r4.out_path) if r4.out_path else None
        got4 = audio_duration(out4.read_bytes()) if out4 and out4.exists() else None
        check("acquire: 人工点选（对象不同、靠指纹定位）能正常下载",
              r4.status == "downloaded" and got4 is not None and 235 <= got4 <= 245,
              f"{r4.status} {r4.out_path} 实测={got4}")

        # ⑤ 搜不到：批量入口给可读的失败结果，而不是抛异常或卡住
        rs = pipe.acquire_many(["这首歌不存在谢谢", ("也不存在呀", "某人")])
        check("acquire_many: 无结果时返回可读的失败结果",
              len(rs) == 2 and all(r.status == "failed" for r in rs)
              and all("搜索无结果" in (r.message or "") for r in rs),
              " | ".join(f"{r.status}:{r.message}" for r in rs))

        # ⑥ 未设输出目录 → default_search_dir()（绝不落到进程工作目录）
        orig_default = pl.default_search_dir
        pl.default_search_dir = lambda: tmp / "default_out"
        try:
            pipe2, _ = make_pipe("", [full_src])
            r6 = pipe2.acquire("夜曲", artist="周杰伦")
            out6 = Path(r6.out_path) if r6.out_path else None
            check("acquire: 未设输出目录时落到 default_search_dir（不是进程工作目录）",
                  out6 is not None and out6.parent == tmp / "default_out",
                  str(out6))
        finally:
            pl.default_search_dir = orig_default

        # ⑦ 只给歌名：按"多数演唱者"静默优先（只排序、不排除任何候选）
        ids, display, n, total = Pipeline._dominant_artist([
            Track("s1", "夜曲", "周杰伦"), Track("s2", "夜曲", "周杰伦 Jay Chou"),
            Track("s3", "夜曲", "Xai小爱")])
        check("acquire: 多数演唱者共识（写法差异按包含关系归并）",
              display == "周杰伦" and n == 2 and total == 3,
              f"{display} {n}/{total}")

        reshuffle = [Track("m", "夜曲", "Xai小爱"), Track("a1", "夜曲", "周杰伦"),
                     Track("a2", "夜曲", "周杰伦")]
        pipe._prefer_dominant_artist(reshuffle)
        check("acquire: 静默共识会真的重排（少数派从第 1 位挪到末尾）",
              [t.artist for t in reshuffle] == ["周杰伦", "周杰伦", "Xai小爱"],
              str([t.artist for t in reshuffle]))

        # 少数派音源排在前面时，候选列表仍必须以多数演唱者开头
        dup_a = Fake("dup_a", "同歌手源A", "夜曲", "周杰伦", full, 240)
        dup_b = Fake("dup_b", "同歌手源B", "夜曲", "周杰伦", full, 240)
        minority = Fake("min1", "少数派源", "夜曲", "Xai小爱", cut, 120)
        pipe3, _ = make_pipe(tmp / "out3", [minority, dup_a, dup_b])
        order3 = [t.artist for t in pipe3.search_candidates("夜曲")]
        check("acquire: 只给歌名时候选列表以多数演唱者开头（少数派排后）",
              order3[:2] == ["周杰伦", "周杰伦"] and order3[-1] == "Xai小爱", str(order3))


def test_query_sanitize() -> None:
    """手动字段搜索：序号/符号清洗 + 空格分隔读法（修"明明有资源却匹配不到"）。"""
    print("\n[query sanitize: 序号与符号]")
    from mmusic.match import sanitize_query_text, space_split_readings
    from mmusic.search.base import SearchSource

    table = [
        ("1. 夜曲", "夜曲"), ("2、发如雪", "发如雪"), ("三、发如雪", "发如雪"),
        ("（一）发如雪", "发如雪"), ("（二）夜曲", "夜曲"), ("【3】夜曲", "夜曲"),
        ("1)夜曲", "夜曲"), ("3．发如雪", "发如雪"), ("第1首 夜曲", "夜曲"),
        ("第 3 曲 发如雪", "发如雪"), ("★ 2.（一）发如雪", "发如雪"),
        ("• 夜曲", "夜曲"), ("《夜曲》", "夜曲"), ("01 夜曲", "夜曲"),
        (" 12- 夜曲", "夜曲"), ("夜曲（2）", "夜曲"), ("夜曲-2", "夜曲"),
        ("1. 周杰伦 - 夜曲", "周杰伦 - 夜曲"),
        # 行首中文说明前缀（白名单）
        ("点播：夜曲", "夜曲"), ("歌曲：夜曲", "夜曲"), ("歌名:夜曲", "夜曲"),
        ("点播 夜曲", "夜曲"), ("【点播】夜曲", "夜曲"), ("求：夜曲", "夜曲"),
        ("1. 点播：夜曲", "夜曲"), ("点播 → 夜曲", "夜曲"),
        ("推荐 - 周杰伦 - 夜曲", "周杰伦 - 夜曲"), ("试听：夜曲", "夜曲"),
        # 最后一轮：更多"值得忽略"的输入形态
        ("#1 夜曲", "夜曲"), ("No.3 夜曲", "夜曲"), ("song: 夜曲", "夜曲"),
        ("夜曲 - 点播", "夜曲"), ("周杰伦 - 夜曲 - 点播", "周杰伦 - 夜曲"),
        ("周杰伦《夜曲》", "周杰伦 - 夜曲"), ("周杰伦·夜曲", "周杰伦 - 夜曲"),
        ("歌手：周杰伦 - 夜曲", "周杰伦 - 夜曲"), ("【原唱：周杰伦】夜曲", "夜曲"),
        ("夜曲.mp3", "夜曲"), ("周杰伦 - 夜曲.mflac", "周杰伦 - 夜曲"),
    ]
    bad = [f"{src!r}->{sanitize_query_text(src)!r}" for src, want in table
           if sanitize_query_text(src) != want]
    check("sanitize: 序号/符号（前缀与后缀）全部清洗干净", not bad, "; ".join(bad))

    check("sanitize: 修复的正是「卡在硬门槛下」的情形（1. 夜曲 0.57 → 夜曲 1.00）",
          similarity("1. 夜曲", "夜曲") < MIN_TITLE_SIMILARITY
          <= similarity(sanitize_query_text("1. 夜曲"), "夜曲"),
          f"{similarity('1. 夜曲', '夜曲'):.2f} -> "
          f"{similarity(sanitize_query_text('1. 夜曲'), '夜曲'):.2f}")

    check("sanitize: 版本标记（Live/伴奏）原样保留（版本过滤依赖它们）",
          sanitize_query_text("夜曲 (Live)").lower() == "夜曲 (live)"
          and "伴奏" in sanitize_query_text("夜曲（伴奏）")
          and "live" in version_markers(sanitize_query_text("夜曲 (Live)")),
          f"{sanitize_query_text('夜曲 (Live)')!r}")
    check("sanitize: 数字开头是真歌名的不动（7 Years / 30 岁的女人 / 2002年的第一场雪）",
          sanitize_query_text("7 Years") == "7 Years"
          and sanitize_query_text("30 岁的女人") == "30 岁的女人"
          and sanitize_query_text("2002年的第一场雪") == "2002年的第一场雪")
    check("sanitize: 全是序号时绝不把输入擦成空串", sanitize_query_text("1.") == "1.")
    check("sanitize: 说明前缀用白名单，不误伤正常歌名（音乐会 / 点播）",
          sanitize_query_text("音乐会") == "音乐会"
          and sanitize_query_text("点播") == "点播",
          f"{sanitize_query_text('音乐会')!r} / {sanitize_query_text('点播')!r}")
    check("sanitize: 「伴奏：」「Live:」不会被当说明前缀删掉（点名版本仍生效）",
          "伴奏" in sanitize_query_text("伴奏：夜曲")
          and "live" in version_markers(sanitize_query_text("Live:夜曲")),
          f"{sanitize_query_text('伴奏：夜曲')!r}")
    check("sanitize: 零宽字符/全角空格被清掉",
          sanitize_query_text("夜\u200b曲\u3000") == "夜曲",
          repr(sanitize_query_text("夜\u200b曲\u3000")))

    from mmusic.match import looks_like_song_query, sanitize_query

    check("sanitize: 从「原唱/演唱/歌手：」里抠出歌手提示（无歧义形式）",
          sanitize_query("夜曲 原唱：周杰伦") == ("夜曲", "周杰伦")
          and sanitize_query("夜曲（原唱：周杰伦）") == ("夜曲", "周杰伦")
          and sanitize_query("【演唱：周杰伦】夜曲") == ("夜曲", "周杰伦"),
          f"{sanitize_query('夜曲 原唱：周杰伦')}")
    check("sanitize: 歧义形式（标签后既跟歌手又跟歌名）刻意不猜",
          sanitize_query("歌手：周杰伦 夜曲")[1] == "",
          f"{sanitize_query('歌手：周杰伦 夜曲')}")

    usable = [looks_like_song_query(x) for x in ("1.", "求：", "点播", "点播：", "  ")]
    check("筛选: 只有序号/说明前缀的行被判定为不可用",
          not any(ok for ok, _ in usable), str(usable))
    keep = [looks_like_song_query(x) for x in ("夜曲", "3、发如雪", "夜曲.mp3", "求：夜曲")]
    check("筛选: 有歌名的行全部保留",
          all(ok for ok, _ in keep), str(keep))

    # 回归：单段原文给出的"不含歌手"读法，不能靠"候选标题里含歌手名"绕过歌手门槛
    # （实测下到过「稳重的牧牛铃 - 周杰伦 - 发如雪」这种垃圾）
    from mmusic.match import best_reading_match as _brm
    _sr, rd5, _q5, _rr = Pipeline._seed_for("周杰伦·发如雪")
    check("seed: 分隔符归一后不再保留「无歌手」的原始读法（堵漏口）",
          bool(rd5) and all(a for _t, a in rd5), str(rd5))
    bad = Track("agg", "周杰伦 - 发如雪", "稳重的牧牛铃", duration=240)
    ok5, _rd, _ts, _as = _brm(rd5, bad, min_title=0.60, min_artist=0.60, strict_title=False)
    check("seed: 「周杰伦·发如雪」不会被“标题含歌手名”的垃圾候选蒙混过关", not ok5,
          f"ts={_ts:.2f} as={_as:.2f}")

    check("space-split: 「夜曲 周杰伦」补出 (歌名, 歌手) 读法",
          ("夜曲", "周杰伦") in space_split_readings("夜曲 周杰伦"))
    check("space-split: 单段输入不硬拆", space_split_readings("夜曲") == [])

    # 落到选择种子：查询词与读法都必须干净
    _s1, rd1, q1, _r1 = Pipeline._seed_for("1. 夜曲")
    check("seed: 带序号的歌名 → 发给音源的查询词被清洗为「夜曲」", q1 == "夜曲", q1)
    check("seed: 带序号的歌名 → 读法里含干净的 (夜曲, \"\")",
          ("夜曲", "") in rd1, str(rd1))
    _sp, _rdp, qp, wantp = Pipeline._seed_for("试听：夜曲")
    check("seed: 说明前缀「试听：」被清掉，且不被误当成\"点名要试听版\"",
          qp == "夜曲" and not wantp, f"{qp} {sorted(wantp)}")

    _s2, rd2, _q2, _r2 = Pipeline._seed_for("夜曲 周杰伦")
    check("seed: 空格分隔的「歌名 歌手」→ 读法含 (夜曲, 周杰伦)",
          ("夜曲", "周杰伦") in rd2, str(rd2))
    _rd3, q3, want3 = Pipeline._seed_from_name("夜曲 (Live)")
    check("seed: 点名版本标记不被清洗掉，仍接力给版本过滤",
          "live" in want3 and q3 == "夜曲", f"{sorted(want3)} {q3}")
    rd4, q4, _r4 = Pipeline._seed_from_name("1. 夜曲")
    check("seed: 文件名模式同样受益（拖入「1. 夜曲.mgg」也能匹配）",
          q4 == "夜曲" and ("夜曲", "") in rd4, f"{q4} {rd4}")

    # 端到端回归：带序号的手动输入必须真的能匹配到资源
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        audio = tmp / "song.mp3"
        audio.write_bytes(fx.make_mp3_xing(240))

        class Fake(SearchSource):
            def __init__(self):
                super().__init__(None, {})
                self.name = "f1"
                self.label = "测试源"

            def search(self, query, *, quality="mp3", limit=10):
                return [Track(self.name, "夜曲", "周杰伦", duration=240, bitrate=320,
                              ext="mp3", raw={"path": str(audio)})]

            def resolve(self, track, *, quality="mp3"):
                track.url = audio.resolve().as_uri()
                track.ext = "mp3"
                return track

        st = Settings(output_dir=str(tmp / "out"), quality="mp3", enable_fallback=True,
                      write_metadata=False, fetch_cover=False, remember_hashes=False,
                      timeout=5.0, retries=0, min_bitrate=0)
        logs: "list[str]" = []
        pipe = Pipeline(st, cache_path=tmp / "c.json", on_log=logs.append, sources=[Fake()])
        r_num = pipe.acquire("1. 夜曲", artist="周杰伦")
        check("acquire: 带序号的手动输入也能匹配并下载（端到端回归）",
              r_num.status == "downloaded",
              f"{r_num.status} {r_num.message}")
        r_sp = pipe.acquire("夜曲 周杰伦")
        check("acquire: 空格分隔的手动输入也能匹配并下载（端到端回归）",
              r_sp.status == "downloaded",
              f"{r_sp.status} {r_sp.message}")

        # 批量清单里混进"只有序号/说明前缀"的行：跳过而不是白跑一轮检索
        batch = pipe.acquire_many(["1.", "求：", "点播", "夜曲"])
        check("acquire_many: 只有序号/说明前缀的行被跳过（不白跑检索）",
              len(batch) == 4 and sum(1 for r in batch if r.status == "skipped") == 3
              and sum(1 for r in batch if r.status == "downloaded") == 1,
              " | ".join(f"{r.name}:{r.status}" for r in batch))


def test_placeholder_artist() -> None:
    """占位歌手（群星/未知/VA）不能挤掉"用户明确点名的歌手"（实测回归）。"""
    print("\n[placeholder artist: 群星/未知 不当证据]")
    import mmusic.pipeline as pl
    from mmusic.search.base import SearchSource

    def _ph(name: str) -> bool:
        return pl._placeholder_credit(Track("s", "夜曲", name))

    check("placeholder: 群星/VA/未知 被判为占位歌手",
          _ph("群星") and _ph("群星(合辑)") and _ph("VA") and _ph("Various Artists")
          and _ph("未知") and _ph("未知歌手"), "")
    check("placeholder: 空歌手不算占位（CC/PD、本地曲库没有歌手信息）", not _ph(""))
    check("placeholder: 正常歌手不算占位", not _ph("周杰伦") and not _ph("Jay Chou"), "")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        long_file = tmp / "long.mp3"
        long_file.write_bytes(fx.make_mp3_xing(276))       # 4:36 的合辑版（群星）
        good_file = tmp / "good.mp3"
        good_file.write_bytes(fx.make_mp3_xing(227))       # 3:47 的正片（周杰伦）

        class Fake(SearchSource):
            def __init__(self, name, label, artist, path, duration):
                super().__init__(None, {})
                self.name, self.label = name, label
                self._a, self._p, self._d = artist, str(path), duration

            def search(self, query, *, quality="mp3", limit=10):
                return [Track(self.name, "夜曲", self._a, duration=self._d, bitrate=320,
                              ext="mp3", raw={"path": self._p})]

            def resolve(self, track, *, quality="mp3"):
                track.url = Path(track.raw["path"]).resolve().as_uri()
                track.ext = "mp3"
                return track

        agg = Fake("agg", "聚合源", "群星", long_file, 276)
        migu = Fake("migu", "咪咕源", "周杰伦", good_file, 227)

        def run(sources, tag):
            st = Settings(output_dir=str(tmp / ("out_" + tag)), quality="mp3",
                          enable_fallback=True, write_metadata=False, fetch_cover=False,
                          remember_hashes=False, timeout=5.0, retries=0, min_bitrate=0)
            logs: "list[str]" = []
            pipe = Pipeline(st, cache_path=tmp / (tag + ".json"), on_log=logs.append,
                            sources=sources)
            return pipe, logs

        # ① 明确的歌手 vs 占位歌手：参考时长不能取自占位候选，最终必须下明确歌手那条
        pipe, logs = run([agg, migu], "both")
        res = pipe.acquire("周杰伦 - 夜曲")
        out = Path(res.out_path) if res.out_path else None
        got = audio_duration(out.read_bytes()) if out and out.exists() else None
        check("placeholder: 有明确歌手候选时，占位（群星 4:36）不参与参考时长",
              any("3:47" in m or "3:46" in m for m in logs), " | ".join(logs[:3])[:120])
        check("placeholder: 最终下载的是明确歌手那条（实测 ≈227s，不是群星 276s）",
              res.status == "downloaded" and got is not None and 222 <= got <= 232,
              f"{res.status} 实测={got} {out}")
        check("placeholder: 产物文件名用明确歌手",
              out is not None and out.name == "周杰伦 - 夜曲.mp3", out.name if out else res.message)

        # ② 只有占位候选时**照常下载**（只降权、不硬删）
        pipe2, _ = run([agg], "only")
        res2 = pipe2.acquire("周杰伦 - 夜曲")
        check("placeholder: 只有占位候选时仍然照常下载（不硬删）",
              res2.status == "downloaded", f"{res2.status} {res2.message}")


def test_settings_roundtrip() -> None:
    """设置项往返：凭证 / 自动开始 / 目标音质等字段不丢、类型正确。"""
    print("\n[settings: round-trip]")
    from mmusic.config import Settings
    s = Settings(quality="flac", qqmusic_uin="1", qqmusic_key="K", auto_start=False)
    back = Settings.from_dict(s.to_dict())
    check("settings: new fields round-trip (creds / auto-start / quality)",
          back.qqmusic_uin == "1" and back.qqmusic_key == "K"
          and back.auto_start is False and back.quality == "flac")


def test_lossless_first_source() -> None:
    """目标无损（FLAC/WAV）：聚合通道优先 —— 实测全链唯一能给真无损的源。"""
    print("\n[lossless target: aggregator-first policy]")
    from mmusic.search.base import SearchSource

    class Src(SearchSource):
        def __init__(self, name, artist, path, ext):
            super().__init__(None, {})
            self.name = self.label = name
            self._artist, self._path, self._ext = artist, str(path), ext

        def search(self, query, *, quality="mp3", limit=10):
            return [Track(self.name, "晴天", self._artist, ext=self._ext,
                          raw={"p": self._path})]

        def resolve(self, track, *, quality="mp3"):
            track.url = Path(track.raw["p"]).resolve().as_uri()
            track.ext = self._ext
            return track

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        mp3 = tmp / "migu.mp3"
        mp3.write_bytes(fx.make_mp3_xing(200, pad=40000))
        flac = tmp / "agg.flac"
        flac.write_bytes(fx.make_flac(200, pad=40000))
        # 聚合通道故意用"稍弱但过门槛"的歌手写法：默认排序下它不该天然取胜
        agg = Src("aggregator", "周杰伦 Jay Chou", flac, "flac")
        migu = Src("migu", "周杰伦", mp3, "mp3")

        def run(quality, tag):
            d = tmp / tag
            d.mkdir()
            src = d / "晴天 - 周杰伦.mgg"
            src.write_bytes(os.urandom(8192) + b"STag")
            logs: "list[str]" = []
            pipe = Pipeline(Settings(output_dir=str(d / "out"), quality=quality,
                                     enable_fallback=True, write_metadata=False,
                                     fetch_cover=False, remember_hashes=False, min_bitrate=0),
                            cache_path=d / "c.json", on_log=logs.append,
                            sources=[migu, agg])          # 用户配置顺序：咪咕在前
            res = pipe.process_file(src)
            out = Path(res.out_path) if res.out_path else None
            return res, out, (out.read_bytes() if out and out.exists() else b""), logs

        # ① 检索顺序：无损目标时聚合通道排到联网源最前；mp3 目标保持用户顺序
        order_flac = [x.name for x in Pipeline(
            Settings(quality="flac"), cache_path=tmp / "o1.json",
            sources=[migu, agg])._search_order(False)]
        order_mp3 = [x.name for x in Pipeline(
            Settings(quality="mp3"), cache_path=tmp / "o2.json",
            sources=[migu, agg])._search_order(False)]
        order_cjk = [x.name for x in Pipeline(
            Settings(quality="flac"), cache_path=tmp / "o3.json",
            sources=[migu, agg])._search_order(True)]
        check("无损优先: 目标 FLAC 时聚合通道排到最前",
              order_flac == ["aggregator", "migu"], str(order_flac))
        check("无损优先: 目标 MP3 时保持用户配置顺序",
              order_mp3 == ["migu", "aggregator"], str(order_mp3))
        check("无损优先: 华语 + 无损时聚合通道仍排在咪咕之前",
              order_cjk == ["aggregator", "migu"], str(order_cjk))

        # ② 端到端：同样的候选，只切换目标音质，产物必须不同
        r_flac, out_flac, got_flac, logs_flac = run("flac", "flac")
        check("无损优先: 目标 FLAC → 选中聚合通道的无损文件且不做降级转码",
              out_flac is not None and out_flac.suffix == ".flac"
              and got_flac == flac.read_bytes(),
              f"{r_flac.status} {out_flac.name if out_flac else None} {len(got_flac)}B")
        check("无损优先: 日志说明重排原因（不静默）",
              any("目标无损" in m for m in logs_flac), " | ".join(logs_flac[-4:]))

        r_mp3, out_mp3, got_mp3, _ = run("mp3", "mp3")
        check("无损优先: 目标 MP3 → 排序不受影响，仍按匹配度取咪咕那份",
              got_mp3 == mp3.read_bytes(),
              f"{r_mp3.status} {out_mp3.name if out_mp3 else None} {len(got_mp3)}B")

        # ③ 源已是有损 MP3 + 目标 FLAC（没有无损候选时）：必须"保持原样"。
        #    旧行为会把 128k mp3 重新编码成 320k mp3（实测体积 3.63MB → 9.07MB），
        #    体积翻倍却没有音质收益，还多一代损失。
        d3 = tmp / "keep"
        d3.mkdir()
        s3 = d3 / "晴天 - 周杰伦.mgg"
        s3.write_bytes(os.urandom(8192) + b"STag")
        logs3: "list[str]" = []
        pipe3 = Pipeline(Settings(output_dir=str(d3 / "out"), quality="flac",
                                  enable_fallback=True, write_metadata=False,
                                  fetch_cover=False, remember_hashes=False, min_bitrate=0),
                         cache_path=d3 / "c.json", on_log=logs3.append, sources=[migu])
        r3 = pipe3.process_file(s3)
        o3 = Path(r3.out_path) if r3.out_path else None
        got3 = o3.read_bytes() if o3 and o3.exists() else b""
        check("无损目标: 源已是 MP3 时保持原样（不重编码、体积不变）",
              o3 is not None and o3.suffix == ".mp3" and got3 == mp3.read_bytes(),
              f"{r3.status} {o3.name if o3 else None} {len(got3)}B vs 源 {len(mp3.read_bytes())}B")
        check("无损目标: 如实说明为何不是无损（不做无效转换）",
              any("无效转换" in m for m in logs3), " | ".join(logs3[-3:]))


def test_over_long_guard() -> None:
    """偏长防护：合辑/串烧（实测 IA 给过 25:29 / 435MB）不得通过核验与被下载。"""
    print("\n[too-long guard: compilations must not slip through]")
    from mmusic.downloader import check_completeness

    ok, why, _ = check_completeness(fx.make_mp3_xing(1529), reference=205.0, tolerance=10.0,
                                    min_seconds=45.0, over_tolerance=123.0)
    check("偏长: 下载后完整度门禁拦下 25:29 的合辑",
          not ok and "长于参考时长" in why, f"{ok} {why}")
    ok2, _, _ = check_completeness(fx.make_mp3_xing(205), reference=205.0, tolerance=10.0,
                                   min_seconds=45.0, over_tolerance=123.0)
    check("偏长: 正常时长的资源不受影响", ok2)
    ok3, _, _ = check_completeness(fx.make_mp3_xing(1529), reference=205.0, tolerance=10.0,
                                   min_seconds=45.0)
    check("偏长: 不传 over_tolerance 时行为与以前一致（向后兼容）", ok3)

    from mmusic.search.base import SearchSource

    class Src(SearchSource):
        def __init__(self, name, path, secs):
            super().__init__(None, {})
            self.name = self.label = name
            self._path, self._secs = str(path), secs

        def search(self, query, *, quality="mp3", limit=10):
            return [Track(self.name, "晴天", "周杰伦", duration=self._secs, ext="mp3",
                          raw={"p": self._path})]

        def resolve(self, track, *, quality="mp3"):
            track.url = Path(track.raw["p"]).resolve().as_uri()
            track.ext = "mp3"
            return track

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        normal = tmp / "normal.mp3"
        normal.write_bytes(fx.make_mp3_xing(200, pad=40000))
        normal2 = tmp / "normal2.mp3"
        normal2.write_bytes(fx.make_mp3_xing(205, pad=40000))
        long_file = tmp / "compilation.mp3"
        long_file.write_bytes(fx.make_mp3_xing(1529, pad=40000))

        src = tmp / "晴天 - 周杰伦.mgg"
        src.write_bytes(os.urandom(8192) + b"STag")
        logs: "list[str]" = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out"), enable_fallback=True,
                                 write_metadata=False, fetch_cover=False,
                                 remember_hashes=False, min_bitrate=0),
                        cache_path=tmp / "c.json", on_log=logs.append,
                        # 第三路模拟"给出合辑"的源（现实里是互联网档案馆；
                        # 这里用 aggregator 是为了让它进入候选池 —— IA 在华语场景
                        # 本就有"有候选时不检索"的保护，会走不到这条判据）
                        sources=[Src("kuwo", normal, 200), Src("migu", normal2, 205),
                                 Src("aggregator", long_file, 1529)])
        res = pipe.process_file(src)
        out = Path(res.out_path) if res.out_path else None
        got = out.read_bytes() if out and out.exists() else b""
        tail = " | ".join(logs[-5:])
        check("偏长: 合辑被排除、不会被下载",
              got and got != long_file.read_bytes() and got in (normal.read_bytes(), normal2.read_bytes()),
              f"{res.status} {out.name if out else None} :: {tail}")
        check("偏长: 日志如实说明为什么排除",
              any("时长异常偏长已排除" in m for m in logs), tail)


def test_log_levels() -> None:
    """日志分级：结论级恒显且精简；明细级保留；旧的一参数 sink 仍收到全部行。"""
    print("\n[log levels: result vs detail]")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        lib.mkdir()
        (lib / "晴天 - 周杰伦.mp3").write_bytes(fx.make_mp3_xing(200, pad=40000))
        src = tmp / "晴天 - 周杰伦.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")
        settings = Settings(output_dir=str(tmp / "out"), sources=["local"],
                            local_dirs=[str(lib)], enable_fallback=True,
                            write_metadata=False, fetch_cover=False,
                            remember_hashes=False, min_bitrate=0)

        two: "list[tuple[str, str]]" = []
        pipe = Pipeline(settings, cache_path=tmp / "c.json",
                        on_log=lambda m, lv="detail": two.append((m, lv)))
        res = pipe.process_file(src)
        results = [m for m, lv in two if lv == "result"]
        details = [m for m, lv in two if lv == "detail"]
        check("log-levels: 结论级存在且精简（单文件 ≤3 行）",
              0 < len(results) <= 3, f"{len(results)} 行: {results}")
        check("log-levels: 明细级仍然保留（可展开）",
              len(details) >= 2, f"{len(details)} 行")
        check("log-levels: 结论级包含最终结果行",
              any("✅" in m and "完成" in m for m in results), str(results))
        check("log-levels: 结论级不含逐音源排除明细",
              not any("已排除" in m for m in results), str(results))

        one: "list[str]" = []
        pipe2 = Pipeline(settings, cache_path=tmp / "c2.json", on_log=one.append)
        pipe2.process_file(src)
        check("log-levels: 一参数 sink（CLI / 旧调用方）仍收到全部日志",
              len(one) == len(two) and all(isinstance(m, str) for m in one),
              f"{len(one)} vs {len(two)}")
        check("log-levels: 结论级的行在所有日志里都能找到（前缀一致）",
              all(any(m == r for m, _ in two) for r in results), str(results))


def test_log_sink_contract() -> None:
    """接线契约：Qt 信号 emit（签名不可内省）也必须收到 (message, level)，绝不静默丢日志。"""
    print("\n[log sink contract: Qt signal wiring]")
    from mmusic.pipeline import _accepts_level
    check("log-sink: list.append 判为单参数", _accepts_level([].append) is False)
    check("log-sink: 两参数 lambda 判为带 level",
          _accepts_level(lambda m, lv="detail": None) is True)
    check("log-sink: 一参数 lambda 判为单参数",
          _accepts_level(lambda m: None) is False)
    try:
        from PySide6.QtCore import QObject, Signal
    except Exception as exc:  # noqa: BLE001
        print(f"  (skip) 无 PySide6：{exc}")
        return

    class W(QObject):
        logged = Signal(str, str)

    got: "list[tuple[str, str]]" = []
    w = W()
    w.logged.connect(lambda m, lv: got.append((m, lv)))
    check("log-sink: Qt 信号 emit 判为『不可内省』（曾被误判成单参数 → 日志全丢）",
          _accepts_level(w.logged.emit) is None, str(_accepts_level(w.logged.emit)))

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        lib = tmp / "lib"
        lib.mkdir()
        (lib / "晴天 - 周杰伦.mp3").write_bytes(fx.make_mp3_xing(200, pad=40000))
        src = tmp / "晴天 - 周杰伦.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")
        settings = Settings(output_dir=str(tmp / "out"), sources=["local"],
                            local_dirs=[str(lib)], enable_fallback=True,
                            write_metadata=False, fetch_cover=False,
                            remember_hashes=False, min_bitrate=0)
        # ← 出事的接线方式：直接把信号 emit 当 sink
        Pipeline(settings, cache_path=tmp / "c.json",
                 on_log=w.logged.emit).process_file(src)
    levels = {lv for _, lv in got}
    check("log-sink: 用信号 emit 接线时日志一条不丢，且两种级别都在",
          len(got) >= 3 and levels == {"result", "detail"}, f"{len(got)} 行 levels={sorted(levels)}")

    from ui.worker import make_log_sink
    sig_got: "list[tuple[str, str]]" = []

    class FakeSig:
        def emit(self, m, lv):
            sig_got.append((m, lv))

    sink = make_log_sink(FakeSig())
    check("log-sink: worker 的 make_log_sink 可被内省为两参数",
          _accepts_level(sink) is True, str(_accepts_level(sink)))
    sink("x", "detail")
    sink("y")
    check("log-sink: make_log_sink 完整转发 (message, level) 且 level 有默认值",
          sig_got == [("x", "detail"), ("y", "detail")], str(sig_got))


def test_last_resort_source() -> None:
    """末位来源（酷狗）：照常检索但**不为它预解析**直链，只在其它来源都没测到可用候选时才补测。"""
    print("\n[last-resort source: no wasted resolves]")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        good = tmp / "good.mp3"
        good.write_bytes(fx.make_mp3_xing(210, pad=40000))

        class Src(SearchSource):
            def __init__(self, name, title, artist, path, calls):
                super().__init__(None, {})
                self.name = self.label = name
                self._title, self._artist, self._path = title, artist, str(path)
                self.calls = calls

            def search(self, query, *, quality="mp3", limit=10):
                return [Track(self.name, self._title, self._artist, ext="mp3",
                              raw={"p": self._path})]

            def resolve(self, track, *, quality="mp3"):
                self.calls.append(self.name)
                track.url = Path(track.raw["p"]).resolve().as_uri()
                track.ext = "mp3"
                return track

        from mmusic.pipeline import _LAST_RESORT_SOURCES
        check("末位来源: 内置清单里包含 kugou", "kugou" in _LAST_RESORT_SOURCES)

        # 场景①：主来源有可用候选 → 末位来源**连解析都不做**
        calls_a: "list[str]" = []
        primary = Src("kuwo", "晴天", "周杰伦", good, calls_a)
        kugou = Src("kugou", "晴天", "周杰伦", good, calls_a)
        src = tmp / "晴天 - 周杰伦.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")
        logs_a: "list[str]" = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out"), enable_fallback=True,
                                 write_metadata=False, fetch_cover=False,
                                 remember_hashes=False, min_bitrate=0),
                        cache_path=tmp / "c.json", on_log=logs_a.append,
                        sources=[primary, kugou])
        res = pipe.process_file(src)
        check("末位来源: 主来源够用时，没有为末位来源解析直链",
              res.out_path is not None and "kugou" not in calls_a,
              f"resolved={calls_a} status={res.status}")
        check("末位来源: 日志如实说明降权",
              any("已降权" in m and "末位来源" in m for m in logs_a),
              " | ".join(logs_a[-4:]))

        # 场景②：只有末位来源能提供可用候选 → 仍必须被补测到、被下到（绝不误报没找到）
        calls_b: "list[str]" = []
        blank = tmp / "blank.mp3"
        blank.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 40000)
        empty = Src("kuwo", "晴天", "周杰伦", blank, calls_b)     # 测不出时长的候选
        kugou2 = Src("kugou", "晴天", "周杰伦", good, calls_b)
        second = tmp / "second"
        second.mkdir()
        src2 = second / "晴天 - 周杰伦.mgg"      # 与场景① 同名不同目录，读法一致
        src2.write_bytes(os.urandom(8192) + b"STag")
        logs_b: "list[str]" = []
        pipe2 = Pipeline(Settings(output_dir=str(second / "out"), enable_fallback=True,
                                  write_metadata=False, fetch_cover=False,
                                  remember_hashes=False, min_bitrate=0),
                         cache_path=second / "c.json", on_log=logs_b.append,
                         sources=[empty, kugou2])
        res2 = pipe2.process_file(src2)
        got = Path(res2.out_path).read_bytes() if res2.out_path else b""
        check("末位来源: 只有它能提供可用候选时，照样被补测并落盘",
              got == good.read_bytes(), f"{res2.status} got={len(got)}B :: {' | '.join(logs_b[-4:])}")


def test_source_link_upgrades() -> None:
    """P0：酷我直链形态判定（正片/短资源）+ 实测平均码率 + 酷狗 hash 顺序与返回值校正。"""
    print("\n[fallback sources: link heuristics]")
    from mmusic.pipeline import _avg_bitrate
    from mmusic.search.kugou import KugouSource
    from mmusic.search.kuwo import KuwoSource, classify_link

    # ① 酷我直链形态（URL 全部取自 2026-10-19 的真实实测样本）
    cases = [
        ("https://kw-er.kuwo.cn/f2319cb6fc8ff011660e68a9e527225a/6ad5ae9c/resource/30106/trackmedia/M500004H9KoF2Ms7hW.mp3", "full"),
        ("https://kw-bj.kuwo.cn/44c84912bff67654ea8714666eb1e4bd/6ad5ae9b/ev/resource/n2/39/48/2738742160.mp3", "short"),
        ("https://kw-bj.kuwo.cn/38be33aea26d130a68505f3e82e5f4fa/6ad5ae9c/lx/resource/n3/88/69/563182854.mp3", "short"),
        ("https://kw-bj.kuwo.cn/895d18d586195fe6df16456881f9a358/6ad5ae9d/gf/resource/n3/77/73/3643297523.mp3", "short"),
        ("https://kw-bj.kuwo.cn/4dc769751718ad0dba37714ab9aaf953/6ad5aeb9/nf/resource/x.mp3", "short"),
        ("https://example.com/song.mp3", ""),
        ("", ""),
    ]
    bad = [f"{classify_link(u)}!={w}" for u, w in cases if classify_link(u) != w]
    check("kuwo: CDN 路径能区分正片/短资源（不用下载）", not bad, " | ".join(bad))
    check("kuwo: trackmedia 形态判为正片",
          classify_link(cases[0][0]) == "full")

    # ② 平均码率用真实数字校准（体积 ÷ 实测时长，不用首帧码率）
    check("bitrate: 实测平均码率 3.69MB/230s -> 128k",
          _avg_bitrate(3690000, 230.0) == 128, str(_avg_bitrate(3690000, 230.0)))
    check("bitrate: 短资源也如实算出低码率 0.13MB/22s -> 47k",
          _avg_bitrate(130000, 22.0) == 47, str(_avg_bitrate(130000, 22.0)))
    check("bitrate: 没有时长就不猜", _avg_bitrate(1000, None) == 0)

    # ③ 酷我 resolve：短资源当场标记 + 解析入口是 HTTPS
    class KuwoHttp:
        def __init__(self, url):
            self.url = url

        def get_json(self, url, headers=None, **kw):
            return {"code": 200, "url": self.url}

    short_url = "https://kw-bj.kuwo.cn/aa/bb/lx/resource/n3/1/2/3.mp3"
    ks = KuwoSource(KuwoHttp(short_url), {})
    track = Track("kuwo", "夜曲", "周杰伦", raw={"MUSICRID": "MUSIC_1"})
    ks.resolve(track)
    check("kuwo: 短资源直链会被标记 short_asset（不下载即可判）",
          track.short_asset and track.url == short_url, f"{track.short_asset} {track.url}")
    full_url = "https://kw-er.kuwo.cn/aa/bb/resource/30106/trackmedia/M500004H9KoF2Ms7hW.mp3"
    ks2 = KuwoSource(KuwoHttp(full_url), {})
    track2 = Track("kuwo", "夜曲", "周杰伦", raw={"MUSICRID": "MUSIC_2"})
    ks2.resolve(track2)
    check("kuwo: 正片不会被误标", not track2.short_asset)
    check("kuwo: 解析入口已改为 HTTPS",
          KuwoSource.RESOLVE.startswith("https://"), KuwoSource.RESOLVE)

    # ④ 酷狗：hash 顺序（FileHash 优先）+ 用返回值校正声明字段
    class KugouHttp:
        def __init__(self):
            self.info_urls: list[str] = []

        def get_json(self, url, headers=None, **kw):
            if "song_search_v2" in url:
                return {"data": {"lists": [{
                    "SongName": "夜曲", "SingerName": "周杰伦", "AlbumName": "十一月的萧邦",
                    "Duration": 230, "FileHash": "FILEH",
                    "HQFileHash": "HQH", "SQFileHash": "SQH"}]}}
            self.info_urls.append(url)
            return {"url": "https://sharefs.kugou.com/x.mp3", "bitRate": 128,
                    "extName": "mp3", "fileSize": 3000000}

    h = KugouHttp()
    gs = KugouSource(h, {})
    t_mp3 = gs.search("夜曲", limit=3)[0]
    gs.resolve(t_mp3, quality="mp3")
    check("kugou: 常规目标先试 FileHash（实测命中率最高）",
          bool(h.info_urls) and "hash=FILEH" in h.info_urls[0], str(h.info_urls[:1]))
    check("kugou: 用返回值校正声明（声明 320k/flac -> 实测 128k/mp3）",
          t_mp3.bitrate == 128 and t_mp3.ext == "mp3", f"{t_mp3.bitrate}/{t_mp3.ext}")
    h2 = KugouHttp()
    gs2 = KugouSource(h2, {})
    t_flac = gs2.search("夜曲", limit=3)[0]
    gs2.resolve(t_flac, quality="flac")
    check("kugou: FLAC 目标仍先试 SQ（唯一可能给无损的档）",
          bool(h2.info_urls) and "hash=SQH" in h2.info_urls[0], str(h2.info_urls[:1]))


def test_short_asset_probe_priority() -> None:
    """疑似短资源不许占满探测预算：真原曲必须仍能被测到、被下到（此前会报"没找到"）。"""
    print("\n[short assets must not eat the probe budget]")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        clip1 = tmp / "clip1.mp3"
        clip1.write_bytes(fx.make_mp3_xing(5, pad=40000))
        clip2 = tmp / "clip2.mp3"
        clip2.write_bytes(fx.make_mp3_xing(8, pad=40000))
        full = tmp / "full.mp3"
        full.write_bytes(fx.make_mp3_xing(200, pad=40000))

        class Fake(SearchSource):
            def __init__(self):
                super().__init__(None, {})
                self.name = self.label = "fake"

            def search(self, query, *, quality="mp3", limit=10):
                # 前两个是"疑似短资源"（真实场景由酷我 CDN 路径判定），而且
                # 歌手字段完全一致 => 匹配分**更高**，会排在真原曲前面。
                return [Track(self.name, "晴天", "周杰伦", ext="mp3",
                              raw={"p": str(clip1)}),
                        Track(self.name, "晴天", "周杰伦", ext="mp3",
                              raw={"p": str(clip2)}),
                        Track(self.name, "晴天", "周杰伦 Jay Chou", ext="mp3",
                              raw={"p": str(full)})]

            def resolve(self, track, *, quality="mp3"):
                path = Path(track.raw["p"])
                track.url = path.resolve().as_uri()
                track.ext = "mp3"
                track.short_asset = path.name.startswith("clip")
                return track

        src = tmp / "晴天 - 周杰伦.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")
        logs: list[str] = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out"), enable_fallback=True,
                                 write_metadata=False, fetch_cover=False,
                                 remember_hashes=False, min_bitrate=0),
                        cache_path=tmp / "c.json", on_log=logs.append, sources=[Fake()])
        res = pipe.process_file(src)
        got = Path(res.out_path).read_bytes() if res.out_path else b""
        tail = " | ".join(logs[-6:])
        check("短资源降权: 探测预算没被片段占满，真原曲照样被测到并落盘",
              got == full.read_bytes(), f"{res.status} got={len(got)}B :: {tail}")
        check("短资源降权: 日志说明降权（不静默）",
              any("已降权" in m for m in logs), tail)
        check("短资源降权: 片段连探测都不占用（预算全给正片，更省请求）",
              not any("时长过短已排除" in m for m in logs), tail)


def test_portable_config_dir() -> None:
    """便携模式：程序目录里有 settings.json / portable 标记时配置随 exe 走，否则仍是 %APPDATA%。"""
    print("\n[portable mode: config beside the exe]")
    from mmusic.config import AppConfig, default_config_dir, resolve_config_dir

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        bare = tmp / "bare"
        bare.mkdir()
        check("portable: 没有标记时仍用 %APPDATA%",
              resolve_config_dir(bare) == default_config_dir(), str(resolve_config_dir(bare)))
        check("portable: 仓库根目录没有标记时也不会把配置写进仓库",
              resolve_config_dir() == default_config_dir(), str(resolve_config_dir()))

        with_settings = tmp / "with_settings"
        with_settings.mkdir()
        (with_settings / "settings.json").write_text("{}", encoding="utf-8")
        check("portable: 存在 settings.json → 配置目录 = 程序目录",
              resolve_config_dir(with_settings) == with_settings,
              str(resolve_config_dir(with_settings)))

        marker = tmp / "marker_file"
        marker.mkdir()
        (marker / "portable").write_text("", encoding="utf-8")
        check("portable: 空的 portable 标记文件同样启用",
              resolve_config_dir(marker) == marker, str(resolve_config_dir(marker)))

        marker_dir = tmp / "marker_dir"
        (marker_dir / "portable").mkdir(parents=True)
        check("portable: portable 文件夹同样启用",
              resolve_config_dir(marker_dir) == marker_dir, str(resolve_config_dir(marker_dir)))

        cfg = AppConfig(path=with_settings / "settings.json")
        cfg.settings.qqmusic_uin = "12345"
        cfg.settings.qqmusic_key = "K"
        cfg.settings.quality = "flac"
        cfg.save()
        back = AppConfig(path=with_settings / "settings.json").settings
        check("portable: 便携目录里的设置与凭证往返正常",
              back.qqmusic_uin == "12345" and back.qqmusic_key == "K"
              and back.quality == "flac", f"{back.qqmusic_uin}/{back.quality}")
        check("portable: 哈希缓存与配置同目录（一起搬走）",
              cfg.cache_path.parent == with_settings, str(cfg.cache_path))


def test_completeness_gate() -> None:
    """P0-1：下载完整度门禁——提示音/试听/截短绝不落盘。"""
    print("\n[completeness gate]")
    from mmusic.downloader import check_completeness

    full = fx.make_mp3_xing(228)
    short = fx.make_mp3_xing(11)

    ok, why, secs = check_completeness(full, reference=228, tolerance=10)
    check("complete: 完整文件通过", ok and 220 < secs < 236, f"{ok} {secs:.0f}")

    ok2, why2, secs2 = check_completeness(short, reference=228, tolerance=10)
    check("complete: 11 秒提示音被判废（下限）", not ok2 and "提示音" in why2, why2)

    ok3, why3, _ = check_completeness(short, reference=0, tolerance=0, min_seconds=10.0)
    ok4, why4, _ = check_completeness(short, reference=0, tolerance=0, min_seconds=10.0,
                                      measured=228.0)
    check("complete: 探测给好的、下载给差的也要判废", not ok4 and "不同文件" in why4, why4)

    ok5, why5, _ = check_completeness(fx.make_mp3_xing(100), reference=228, tolerance=10)
    check("complete: 明显短于参考时长判废", not ok5 and "短于参考" in why5, why5)

    ok6, _, _ = check_completeness(b"ID3" + b"\x00" * 64, reference=228, tolerance=10)
    check("complete: 测不出时长时不额外否决（交给未比对逻辑）", ok6)

    PROMPT = fx.make_mp3_xing(11)
    REAL = fx.make_mp3_xing(200)

    class FakeSource(SearchSource):
        def __init__(self, name, path):
            super().__init__(None, {})
            self.name = self.label = name
            self._path = str(path)

        def search(self, query, *, quality="mp3", limit=10):
            return [Track(self.name, "夜曲", "周杰伦", duration=228, ext="mp3",
                          raw={"path": self._path})]

        def resolve(self, track, *, quality="mp3"):
            track.url = Path(track.raw["path"]).resolve().as_uri()
            track.ext = "mp3"
            return track

    base = dict(enable_fallback=True, write_metadata=False, fetch_cover=False,
                remember_hashes=False)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pp, rp = tmp / "prompt.mp3", tmp / "real.mp3"
        pp.write_bytes(PROMPT)
        rp.write_bytes(REAL)
        src = tmp / "夜曲 - 周杰伦.mgg"
        src.write_bytes(os.urandom(8188) + b"STag")

        logs: list[str] = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "o1"), **base),
                        cache_path=tmp / "c1.json", on_log=logs.append,
                        sources=[FakeSource("zaa", pp)])
        r1 = pipe.process_file(src)
        o1 = tmp / "o1"
        check("complete: 只有提示音时绝不产出文件",
              r1.status == "failed" and not (o1.is_dir() and list(o1.glob("*"))),
              f"{r1.status} :: {r1.message}")

        logs2: list[str] = []
        pipe2 = Pipeline(Settings(output_dir=str(tmp / "o2"), **base),
                         cache_path=tmp / "c2.json", on_log=logs2.append,
                         sources=[FakeSource("zaa", pp), FakeSource("zbb", rp)])
        r2 = pipe2.process_file(src)
        got = Path(r2.out_path).read_bytes() if r2.out_path else b""
        check("complete: 同时有完整版时，产出的是完整版", got == REAL,
              " | ".join(logs2[-5:]))


def test_live() -> None:
    print("\n[fallback/live]")
    http = HttpClient(timeout=15, retries=2)
    settings = Settings(quality="mp3", remember_hashes=False, fetch_cover=False, write_metadata=False)
    sources = build_sources(http, settings.to_dict())
    query = "周杰伦 稻香"
    got_url = False
    for source in sources:
        if source.offline:
            continue
        try:
            tracks = source.search(query, limit=5)
        except Exception as exc:  # noqa: BLE001
            print(f"    · {source.label}: search error {exc}")
            continue
        print(f"    · {source.label}: {len(tracks)} results")
        if not tracks:
            continue
        for tr in tracks[:3]:
            try:
                tr = source.resolve(tr, quality="mp3")
            except Exception as exc:  # noqa: BLE001
                continue
            if not tr.url:
                continue
            try:
                data = http.download(tr.url, max_bytes=20 * 1024 * 1024)
            except Exception as exc:  # noqa: BLE001
                print(f"        download failed: {exc}")
                continue
            v = validate(data, tr.ext, expected_bitrate=tr.bitrate, duration=tr.duration, min_bitrate=96)
            print(f"        {tr.title!r} -> ext={v.ext} {v.bitrate}kbps ok={v.ok} ({len(data)} bytes)")
            if v.ok:
                got_url = True
                break
        if got_url:
            break
    check("live: obtained at least one validated direct link", got_url)
    http.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="exercise the real network fallback chain")
    args = ap.parse_args()

    test_decoders()
    test_validation()
    test_matching()
    test_pipeline_offline()
    test_output_dir_default()
    test_output_files()
    test_version_filter()
    test_variant_filter_e2e()
    test_cross_source_best()
    test_duration_verify()
    test_duration_cross_check_e2e()
    test_title_gate()
    test_duration_floor_e2e()
    test_cross_platform_reference()
    test_verified_only_download()
    test_title_artist_filename()
    test_source_pack()
    test_fusion()
    test_resource_index()
    test_fallback_dedupe_e2e()
    test_dependency_guard()
    test_variant_filter_more()
    test_qualified_title_loses()
    test_duration_tolerance_tight()
    test_mp3_vbr_estimate()
    test_sniffer()
    test_html_scrape_source()
    test_pack_html_scrape()
    test_artist_gate()
    test_artist_gate_local()
    test_strong_gate()
    test_query_expander()
    test_new_sources()
    test_migu_source()
    test_language_prefilter()
    test_cjk_order_e2e()
    test_manual_links()
    test_variant_retry_e2e()
    test_import_all_modules()
    test_kwm_decoder()
    test_musicex_container()
    test_qmc_no_silent_garbage()
    test_target_format_matrix()
    test_transcode_engine()
    test_pipeline_quality_e2e()
    test_ekey_fetch()
    test_ekey_pipeline_e2e()
    test_source_tag_and_cover_carry()
    test_acquire_by_name()
    test_query_sanitize()
    test_placeholder_artist()
    test_settings_roundtrip()
    test_portable_config_dir()
    test_source_link_upgrades()
    test_last_resort_source()
    test_log_levels()
    test_lossless_first_source()
    test_over_long_guard()
    test_log_sink_contract()
    test_short_asset_probe_priority()
    test_completeness_gate()
    if args.live:
        test_live()

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("failures:")
        for f in FAILED:
            print("  -", f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
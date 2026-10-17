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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import fixtures as fx  # noqa: E402
from mmusic.config import Settings  # noqa: E402
from mmusic.crypto.ncm import NcmDecoder  # noqa: E402
from mmusic.crypto.qmc import QmcDecoder  # noqa: E402
from mmusic.downloader import validate  # noqa: E402
from mmusic.errors import NeedsOnlineError  # noqa: E402
from mmusic.match import (filename_query,  # noqa: E402
                        parse_song_filename, parse_song_filename_candidates,
                        rank_tracks, rank_tracks_multi, similarity,
                        split_originals, track_version_markers, version_markers)
from mmusic.model import Track  # noqa: E402
from mmusic.net import HttpClient  # noqa: E402
from mmusic.pipeline import Pipeline  # noqa: E402
from mmusic.search.registry import build_sources  # noqa: E402
from mmusic.search.base import SearchSource  # noqa: E402
from mmusic.sniff import sniff  # noqa: E402

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
               "晴天 (Acoustic)", "晴天 不插电", "晴天 (剪辑版)", "晴天 (Edit)", "晴天 (Mix)"]
    missed = [t for t in blocked if not version_markers(t)]
    check(f"filter: all {len(blocked)} variant markers are caught", not missed, f"漏掉: {missed}")

    kept = ["晴天", "周杰伦 - 晴天", "Alive", "Instant Crush", "Mixed Up",
            "Deliver", "Oliver", "Olivia", "Believer", "Studio 54", "Film"]
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
        weak = FakeSource("aaa", "弱音源", "晴天 (深情版)", "Lucky小爱", wpath)
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
    test_title_artist_filename()
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
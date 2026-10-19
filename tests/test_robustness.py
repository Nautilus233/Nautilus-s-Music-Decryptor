"""Robustness matrix: boundary / negative / fuzz / fault-injection / memory.

    python tests/test_robustness.py            # 全部（约 1~2 分钟）
    python tests/test_robustness.py --quick    # 只跑关键子集（约 20 秒）

Exit code 0 == 所有检查通过。这一套的目标不是"功能对不对"，而是
**任何坏输入/坏网络/坏磁盘都不得让进程崩、卡或静默产出垃圾**。
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import fixtures as fx  # noqa: E402
from mmusic.config import AppConfig, Settings  # noqa: E402
from mmusic.constants import NCM_MAGIC, QMC_TAG_MUSICEX  # noqa: E402
from mmusic.crypto.kwm import KwmDecoder  # noqa: E402
from mmusic.crypto.musicex import parse_tail as parse_musicex_tail  # noqa: E402
from mmusic.crypto.ncm import NcmDecoder  # noqa: E402
from mmusic.crypto.qmc import QmcDecoder  # noqa: E402
from mmusic.downloader import check_completeness, validate  # noqa: E402
from mmusic.duration import audio_duration, flac_streaminfo, mp3_frame_info  # noqa: E402
from mmusic.errors import CancelledError, MusicDecryptorError, SearchNoResultError  # noqa: E402
from mmusic.match import parse_song_filename_candidates  # noqa: E402
from mmusic.model import Track  # noqa: E402
from mmusic.net import HttpClient  # noqa: E402
from mmusic.pipeline import Pipeline  # noqa: E402
from mmusic.search.base import SearchSource  # noqa: E402
from mmusic.sniff import looks_like_audio, sniff, sniff_path  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   -> {extra}" if extra and not cond else ""))


def rss_mb() -> float:
    """当前进程工作集（MB）；非 Windows 返回 0（内存检查自动跳过）。

    注意：必须显式声明 argtypes/restype —— 不声明时 ctypes 会把 64 位伪句柄
    截断，调用静默失败（返回 0），内存检查就会"假装通过"。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = k32.K32GetProcessMemoryInfo
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        fn.restype = wintypes.BOOL
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        pmc = PMC()
        pmc.cb = ctypes.sizeof(pmc)
        if not fn(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return 0.0
        return pmc.WorkingSetSize / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return 0.0


def _decode_bytes(container: bytes, ext: str):
    """在解码器上跑一遍；返回 (结果 or None, 异常 or None)。"""
    try:
        if ext == "ncm":
            return NcmDecoder(container).decode(), None
        if ext == "kwm":
            return KwmDecoder(container).decode(), None
        return QmcDecoder(container, ext).decode(), None
    except MusicDecryptorError as exc:
        return None, exc
    except Exception as exc:  # noqa: BLE001 - 非预期异常由调用方判失败
        return None, exc


# --------------------------------------------------------------------------- #
# 1) 边界
# --------------------------------------------------------------------------- #
def test_boundary(quick: bool) -> None:
    print("\n[boundary: 极小/极大/畸形尺寸]")
    # sniff 的输入边界
    for label, head, tail, ext in (
        ("空内容", b"", b"", ""),
        ("单字节", b"\x00", b"\x00", "mgg"),
        ("只有 NCM 魔数", NCM_MAGIC, b"", "ncm"),
        ("只有 musicex 尾巴", b"x" * 8, b"x" * 8 + QMC_TAG_MUSICEX, "mgg"),
        ("4KB 全 0xFF", b"\xff" * 4096, b"\xff" * 4096, "mgg"),
        ("扩展名是垃圾", b"\x00" * 16, b"\x00" * 16, "zzz"),
    ):
        try:
            info = sniff(head, tail, ext)
            ok = isinstance(info.ext, str) and info.kind in ("ncm", "qmc", "plain", "unknown", "kwm")
        except Exception as exc:  # noqa: BLE001
            ok = False
            info = f"{type(exc).__name__}: {exc}"
        check(f"boundary: sniff 不崩（{label}）", ok, str(info))

    # 0 字节 / 1 字节文件走完整识别
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for label, blob in (("0 字节", b""), ("1 字节", b"\x01"),
                            ("魔数+2 字节", NCM_MAGIC + b"\x01\x02")):
            f = tmp / f"f{len(blob)}.mgg"
            f.write_bytes(blob)
            try:
                info = sniff_path(f)
                ok = True
                extra = f"{info.kind}/{info.ext}"
            except Exception as exc:  # noqa: BLE001
                ok, extra = False, f"{type(exc).__name__}: {exc}"
            check(f"boundary: sniff_path 不崩（{label}）", ok, extra)

        # musicex 尾部尺寸字段的极端值
        good = fx.build_musicex(fx.PAYLOAD_OGG, bytes(range(32)))
        for label, patch_bytes in (
            ("tail_size=0", (0).to_bytes(4, "little")),
            ("tail_size=2^31", (2 ** 31).to_bytes(4, "little")),
            ("tail_size=2^32-1", (2 ** 32 - 1).to_bytes(4, "little")),
            ("tail_size=文件长度", len(good).to_bytes(4, "little")),
            ("tail_size=4097", (4097).to_bytes(4, "little")),
        ):
            blob = bytearray(good)
            blob[-16:-12] = patch_bytes
            try:
                got = parse_musicex_tail(bytes(blob), len(blob))
                ok = got is None or 0 < got.audio_size <= len(blob)
                extra = str(got)
            except Exception as exc:  # noqa: BLE001
                ok, extra = False, f"{type(exc).__name__}: {exc}"
            check(f"boundary: musicex 尾部尺寸异常不崩（{label}）", ok, extra)

    # 时长/码率的数值边界
    check("boundary: 0 字节给不出时长也不炸", audio_duration(b"", 0) is None)
    weird = fx.make_flac(1.0)[:42]
    weird = weird[:18] + (0).to_bytes(8, "big") + weird[26:]      # sample_rate=0
    check("boundary: FLAC 采样率=0 不抛异常", audio_duration(weird, len(weird)) is None,
          str(audio_duration(weird, len(weird))))
    check("boundary: 巨大声明体积不炸", isinstance(audio_duration(fx.make_mp3_xing(1), 2 ** 40), (float, type(None))))
    check("boundary: validate(0 字节) 给出可读原因",
          (lambda v: (not v.ok) and v.reason)(validate(b"")), "no reason")
    check("boundary: 正好 16KB 的随机数据不会被当成音频",
          not validate(os.urandom(16 * 1024)).ok)

    # 文件名解析边界
    for label, stem in (("单字符", "a"), ("300 字符", "嗯" * 300), ("emoji", "🎵 - 🎤"),
                        ("全是分隔符", " - - - "), ("点与空格", "  ..  "),
                        ("三段", "歌手 - 歌名 - 现场版"), ("无分隔", "未知文件")):
        try:
            parts = parse_song_filename_candidates(stem)
            ok = isinstance(parts, list)
            extra = str(parts)[:60]
        except Exception as exc:  # noqa: BLE001
            ok, extra = False, f"{type(exc).__name__}: {exc}"
        check(f"boundary: 文件名解析不崩（{label}）", ok, extra)

    # Windows 保留名 + 超长名
    name = Pipeline._safe_name("", "CON")
    check("boundary: Windows 保留名被改写（CON → CON_）", name != "CON" and name.startswith("CON"), name)
    long = Pipeline._safe_name("歌手" * 80, "歌名" * 80)
    check("boundary: 超长文件名被截断且不含非法字符", len(long) <= 150 and not set(long) & set('<>:"/\\|?*'), long[:40])

    # settings 被手改坏：类型不符必须忽略而不是崩
    s = Settings.from_dict({"quality": 5, "sources": "abc", "timeout": "x",
                            "min_bitrate": "128", "auto_start": "yes",
                            "remember_hashes": 0, "unknown_key": 1})
    check("boundary: 坏类型 settings 被忽略并保留默认值",
          s.quality == "mp3" and s.sources == [] and s.timeout == 12.0
          and s.min_bitrate == 128 and s.auto_start is True,
          f"{s.quality} {s.sources} {s.timeout} {s.min_bitrate} {s.auto_start}")
    s2 = Settings.from_dict({"timeout": 3, "min_bitrate": 320.0, "quality": "flac"})
    check("boundary: 合法数值（int→float / float→int）仍被接受",
          s2.timeout == 3 and s2.min_bitrate == 320.0 and s2.quality == "flac", str(s2.to_dict())[:60])

# --------------------------------------------------------------------------- #
# 2) 负面：坏容器 / 坏配置 / 坏补链
# --------------------------------------------------------------------------- #
def test_negative(quick: bool) -> None:
    print("\n[negative: 坏容器、坏配置、坏补链都必须给可读结果]")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # 坏容器：截断 / 改字节 / 追加垃圾 —— 必须"可读失败"或"解码成功且不是垃圾"
        seeds = [
            ("ncm", fx.build_ncm(fx.PAYLOAD_MP3), "ncm", "ncm"),
            ("qmc_v2", fx.build_qmc_v2(fx.PAYLOAD_MP3, bytes(range(32))), "mflac", "qmc"),
            ("musicex", fx.build_musicex(fx.PAYLOAD_OGG, bytes(range(32))), "mgg", "qmc"),
            ("stag", os.urandom(6000) + b"STag", "mgg", "qmc"),
            ("kwm", b"yeelion-kuwo-tme" + os.urandom(6000), "kwm", "kwm"),
        ]
        variants = {
            "截断到 1/3": lambda b: b[: max(1, len(b) // 3)],
            "头部翻位": lambda b: bytes([b[0] ^ 0xFF]) + b[1:],
            "尾部垃圾": lambda b: b + os.urandom(64),
            "中间清零": lambda b: b[: len(b) // 2] + b"\x00" * (len(b) // 2),
        }
        bad = []
        for name, seed, ext, kind in seeds:
            for vname, mutate in variants.items():
                res, exc = _decode_bytes(mutate(seed), ext)
                if exc is not None and not isinstance(exc, MusicDecryptorError):
                    bad.append(f"{name}/{vname}: {type(exc).__name__}")
                if res is not None and not looks_like_audio(res.audio[:16]):
                    bad.append(f"{name}/{vname}: 静默产出垃圾")
        check(f"negative: {len(seeds) * len(variants)} 个坏容器样本全部给出可读结果/无垃圾",
              not bad, "; ".join(bad[:4]))

        # 坏 ekey：内容乱码时不得解出垃圾
        key = bytes(range(32))
        src = tmp / "t.mgg"
        src.write_bytes(fx.build_musicex(fx.PAYLOAD_OGG, key))
        (tmp / "ekeys").mkdir(exist_ok=True)
        (tmp / "ekeys" / "00TEST00000001__TEST00001.mgg.txt").write_bytes(os.urandom(96))
        res, exc = _decode_bytes(src.read_bytes(), "mgg")
        garbage = res is not None and not looks_like_audio(res.audio[:16])
        check("negative: 乱码 ekey 不会静默解出垃圾",
              not garbage and (exc is None or isinstance(exc, MusicDecryptorError)),
              f"{type(exc).__name__ if exc else 'ok'} {garbage}")

        # 坏 settings.json / hashcache / 台账
        cfgdir = tmp / "cfg"
        cfgdir.mkdir()
        (cfgdir / "settings.json").write_text("{ not json", encoding="utf-8")
        cfg = AppConfig(path=cfgdir / "settings.json")
        check("negative: 坏 settings.json 回退默认值且不崩", cfg.settings.quality == "mp3")
        (cfgdir / "settings.json").write_text('{"quality": 7, "qqmusic_key": null}', encoding="utf-8")
        cfg2 = AppConfig(path=cfgdir / "settings.json")
        check("negative: 类型错误的 settings 不崩（键被忽略）",
              cfg2.settings.quality == "mp3" and cfg2.settings.qqmusic_key == "")
        from mmusic.hashcache import HashCache
        (cfgdir / "hashcache.json").write_text("不是 JSON", encoding="utf-8")
        hc = HashCache(cfgdir / "hashcache.json")
        ok = hc.get("deadbeef") is None
        hc.add("deadbeef", out_path="x", mode="offline", fmt="mp3")
        check("negative: 坏 hashcache 不崩且可继续写入", ok and hc.get("deadbeef") is not None)
        from mmusic.search.index import ResourceIndex
        (cfgdir / "resources.db").write_bytes(b"not a sqlite database" * 10)
        idx = ResourceIndex(cfgdir / "resources.db")
        try:
            idx.note_resource("k", "kuwo", "u", ext="mp3", bitrate=128, size=1, ok=True)
            idx.stats()
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok, _ = False, exc
        finally:
            idx.close()
        check("negative: 坏台账文件不崩（静默禁用/重建）", ok)

        # 坏源清单
        from mmusic.search.pack import PackError, load_user_pack, validate_pack
        for label, doc in (("schema 错", {"schema": 2, "sources": []}),
                           ("字段缺失", {"schema": 1, "sources": [{"id": "x"}]}),
                           ("id 重复", {"schema": 1, "sources": [{"id": "a", "label": "A", "kind": "adapter", "impl": "kuwo"},
                                                              {"id": "a", "label": "B", "kind": "adapter", "impl": "kuwo"}]})):
            try:
                validate_pack(doc)
                ok = False
            except PackError:
                ok = True
            except Exception as exc:  # noqa: BLE001
                ok = False
            check(f"negative: 坏源清单被拒绝（{label}）", ok)
        bad_pack = tmp / "pack.json"
        bad_pack.write_text("{ broken", encoding="utf-8")
        specs, problems = load_user_pack(bad_pack)
        check("negative: 坏源清单文件不崩（返回问题列表）", specs == [] and bool(problems), str(problems))

        # 坏补链：连不上/返回网页
        class JunkHttp(HttpClient):
            def __init__(self):  # noqa: D107
                super().__init__(timeout=3.0, retries=0)

            def download(self, url, **kw):  # noqa: D102
                return b"<!doctype html><html>not audio</html>" + b"x" * 40000

        src2 = tmp / "s2.mgg"
        src2.write_bytes(os.urandom(6000) + b"STag")
        logs: "list[str]" = []
        pipe = Pipeline(Settings(output_dir=str(tmp / "out2"), sources=["manual"],
                                 manual_links=["http://127.0.0.1:1/none.mp3"],
                                 enable_fallback=True, write_metadata=False, fetch_cover=False,
                                 remember_hashes=False, min_bitrate=0, timeout=2.0, retries=0),
                        cache_path=tmp / "c2.json", on_log=logs.append, http=JunkHttp())
        res = pipe.process_file(src2)
        check("negative: 坏补链（连不上）→ 可读失败，不产出文件",
              res.status == "failed" and not (tmp / "out2").exists() and bool(res.message),
              f"{res.status} {res.message[:60]}")
        check("negative: 失败原因写进了日志",
              any(("校验未通过" in m) or ("下载失败" in m) or ("直链不可用" in m) for m in logs),
              " | ".join(logs[-3:])[:120])

# --------------------------------------------------------------------------- #
# 3) Fuzz：变异容器 + 纯解析器随机输入
# --------------------------------------------------------------------------- #
def _mutate(data: bytes, rnd: random.Random) -> bytes:
    blob = bytearray(data)
    ops = rnd.randrange(1, 4)
    for _ in range(ops):
        if not blob:
            break
        kind = rnd.randrange(6)
        if kind == 0:                                  # 翻位
            for _ in range(rnd.randrange(1, 8)):
                i = rnd.randrange(len(blob))
                blob[i] ^= 1 << rnd.randrange(8)
        elif kind == 1:                                # 截断
            blob = blob[: rnd.randrange(1, len(blob) + 1)]
        elif kind == 2:                                # 插入随机字节
            i = rnd.randrange(len(blob))
            blob[i:i] = os.urandom(rnd.randrange(1, 32))
        elif kind == 3:                                # 复制一段
            i = rnd.randrange(len(blob))
            blob += blob[i: i + rnd.randrange(1, 64)]
        elif kind == 4:                                # 清零一段
            i = rnd.randrange(len(blob))
            blob[i: i + rnd.randrange(1, 64)] = b"\x00" * min(64, len(blob) - i)
        else:                                          # 交换两段
            i, j = rnd.randrange(len(blob)), rnd.randrange(len(blob))
            blob[i], blob[j] = blob[j], blob[i]
    return bytes(blob)


def test_fuzz(quick: bool, soak: bool = False, seed: int = 20261020) -> None:
    print(f"\n[fuzz: 容器变异 + 解析器随机输入 | seed={seed}]")
    rnd = random.Random(seed)
    iters = 3000 if soak else (12 if quick else 60)
    seeds = {
        "ncm": fx.build_ncm(fx.PAYLOAD_MP3),
        "qmc_v2": fx.build_qmc_v2(fx.PAYLOAD_MP3, bytes(range(32))),
        "qmc_rc4": fx.build_qmc_v2(fx.PAYLOAD_MP3, bytes((i * 7 + 1) & 0xFF for i in range(512))),
        "musicex": fx.build_musicex(fx.PAYLOAD_OGG, bytes(range(32))),
        "kwm": b"yeelion-kuwo-tme" + os.urandom(3000),
    }
    unexpected: "list[str]" = []
    silent_garbage: "list[str]" = []
    crashes = 0
    t0 = time.perf_counter()
    for name, seed in seeds.items():
        ext = {"ncm": "ncm", "kwm": "kwm"}.get(name, "mgg")
        for i in range(iters):
            blob = _mutate(seed, rnd)
            res, exc = _decode_bytes(blob, ext)
            if exc is not None and not isinstance(exc, MusicDecryptorError):
                unexpected.append(f"{name}#{i}: {type(exc).__name__}: {str(exc)[:40]}")
                crashes += 1
            if res is not None and not looks_like_audio(res.audio[:16]):
                silent_garbage.append(f"{name}#{i}")
    dt = time.perf_counter() - t0
    check(f"fuzz: {len(seeds) * iters} 个变异容器：无未预期异常、无静默垃圾（{dt:.1f}s）",
          not unexpected and not silent_garbage,
          "; ".join((unexpected + silent_garbage)[:4]))

    # 纯解析器：随机字节/字符串暴力投喂（必须"永不抛异常"）
    bad: "list[str]" = []
    n = 20000 if soak else (400 if quick else 2500)
    for i in range(n):
        blob = bytes(rnd.randrange(256) for _ in range(rnd.randrange(0, 260)))
        ext = rnd.choice(["mgg", "mflac", "ncm", "mp3", "kwm", "", "zzz"])
        try:
            sniff(blob[:16], blob, ext)
            parse_musicex_tail(blob, len(blob))
            looks_like_audio(blob[:16])
            audio_duration(blob, len(blob))
            mp3_frame_info(blob)
            flac_streaminfo(blob)
            validate(blob)
            check_completeness(blob, reference=100.0, tolerance=5.0, over_tolerance=60.0)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"#{i} {type(exc).__name__}: {str(exc)[:40]}")
    check(f"fuzz: {n} 组随机字节投喂全部解析器：无任何异常", not bad, "; ".join(bad[:3]))

    bad2: "list[str]" = []
    for i in range(4000 if soak else (200 if quick else 800)):
        stem = "".join(rnd.choice("abcABC  -_[]()（）🎵周杰伦的曰.mgg") for _ in range(rnd.randrange(0, 40)))
        try:
            parse_song_filename_candidates(stem)
        except Exception as exc:  # noqa: BLE001
            bad2.append(f"{type(exc).__name__}: {str(exc)[:30]}")
    check("fuzz: 随机文件名解析无异常", not bad2, "; ".join(bad2[:3]))


# --------------------------------------------------------------------------- #
# 4) 故障注入：网络 / 磁盘 / 取消
# --------------------------------------------------------------------------- #
class _FaultSource(SearchSource):
    """按 mode 制造不同的失败：检索挂、解析挂、下载挂、返回网页。"""

    def __init__(self, mode: str):
        super().__init__(None, {})
        self.name = self.label = f"fault_{mode}"
        self.mode = mode

    def search(self, query, *, quality="mp3", limit=10):
        from mmusic.errors import NetworkError
        if self.mode == "search":
            raise NetworkError("注入：检索超时", detail="timeout")
        return [Track(self.name, "晴天", "周杰伦", ext="mp3",
                      duration=200, raw={"u": f"http://127.0.0.1:1/{self.mode}.mp3"})]

    def resolve(self, track, *, quality="mp3"):
        from mmusic.errors import NetworkError
        if self.mode == "resolve":
            raise NetworkError("注入：无可用直链", detail="resolve")
        track.url = track.raw["u"]
        track.ext = "mp3"
        return track


class _FaultHttp:
    """下载/探测全按 mode 返回坏数据（不真联网）。"""

    def __init__(self, mode):
        self.mode = mode
        self.calls = 0

    def request(self, *a, **kw):
        self.calls += 1
        from mmusic.errors import NetworkError
        raise NetworkError("注入：连接被重置", detail="reset")

    def get_json(self, *a, **kw):
        from mmusic.errors import NetworkError
        raise NetworkError("注入：DNS 解析失败", detail="dns")

    def probe(self, *a, **kw):
        from mmusic.errors import NetworkError
        raise NetworkError("注入：探测超时", detail="probe")

    def probe_tail(self, *a, **kw):
        from mmusic.errors import NetworkError
        raise NetworkError("注入：探测超时", detail="probe_tail")

    def download(self, url, **kw):
        self.calls += 1
        if self.mode == "html":
            return b"<!doctype html><html><body>login</body></html>" + b" " * 40000
        if self.mode == "short":
            return b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 200
        if self.mode == "garbage":
            return os.urandom(40000)
        from mmusic.errors import NetworkError
        raise NetworkError("注入：下载失败", detail="download")

    def close(self):
        pass


def test_fault_injection() -> None:
    print("\n[fault injection: 坏网络/坏磁盘/取消]")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        def run(mode: str, tag: str):
            src = tmp / f"{tag}.mgg"
            src.write_bytes(os.urandom(6000) + b"STag")
            out = tmp / f"out_{tag}"
            logs: "list[str]" = []
            http = _FaultHttp(mode)
            pipe = Pipeline(Settings(output_dir=str(out), enable_fallback=True,
                                     write_metadata=False, fetch_cover=False,
                                     remember_hashes=False, min_bitrate=0),
                            cache_path=tmp / f"c_{tag}.json", on_log=logs.append,
                            sources=[_FaultSource("search"), _FaultSource("resolve"),
                                     _FaultSource(mode)], http=http)
            res = pipe.process_file(src)
            return res, out, logs, http

        for mode, label in (("download", "下载抛异常"), ("html", "返回网页"),
                            ("short", "返回过小文件"), ("garbage", "返回随机垃圾")):
            res, out, logs, http = run(mode, mode)
            ok = res.status == "failed" and not out.exists() and bool(res.message)
            check(f"fault: {label} → 可读失败、不产出文件、不崩", ok,
                  f"{res.status} {res.message[:50]} exists={out.exists()}")
            check(f"fault: {label} → 换源尝试被记录",
                  any("搜索不可用" in m or "直链不可用" in m or "下载失败" in m
                      or "校验未通过" in m for m in logs),
                  " | ".join(logs[-4:])[:120])

        # 全部源不可用 → 可读的整体失败
        res, out, logs, _ = run("download", "alldead")
        check("fault: 所有源都失败 → 可读提示（不抛异常）",
              res.status == "failed" and bool(res.message) and not out.exists(),
              f"{res.status} {res.message[:60]}")

        # 真网络：连不上的端口 / 不存在的域名 必须有界失败（不挂死）
        t0 = time.perf_counter()
        try:
            HttpClient(timeout=2.0, retries=0).request("GET", "http://127.0.0.1:9/x")
            ok = False
            extra = "未失败"
        except MusicDecryptorError as exc:
            ok = True
            extra = exc.message
        except Exception as exc:  # noqa: BLE001
            ok, extra = False, f"{type(exc).__name__}: {exc}"
        dt = time.perf_counter() - t0
        check(f"fault: 连接被拒 → NetworkError 且有界（{dt:.1f}s）", ok and dt < 10, extra)
        t0 = time.perf_counter()
        note = ""
        try:
            status, _body, _hdr = HttpClient(timeout=2.0, retries=0).request(
                "GET", "http://nonexistent.invalid/x")
            # 既有语义：4xx/5xx 作为**状态**返回（交给上层逐源切换），不抛异常。
            # 有代理的环境里 DNS 失败常以 4xx 呈现，因此两种结果都算"有界失败"。
            ok = status >= 400
            note = f"HTTP {status}"
        except MusicDecryptorError as exc:
            ok, note = True, exc.message
        except Exception as exc:  # noqa: BLE001
            ok, note = False, f"{type(exc).__name__}: {exc}"
        dt = time.perf_counter() - t0
        check(f"fault: 域名不可达 → 有界失败（{dt:.1f}s，{note}）", ok and dt < 15, note)

        # 磁盘：输出目录指向一个"文件"
        blocker = tmp / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        src = tmp / "disk.mgg"
        src.write_bytes(fx.build_ncm(fx.PAYLOAD_MP3))
        logs = []
        pipe = Pipeline(Settings(output_dir=str(blocker), enable_fallback=False,
                                 write_metadata=False, fetch_cover=False,
                                 remember_hashes=False),
                        cache_path=tmp / "c_disk.json", on_log=logs.append, sources=[])
        res = pipe.process_file(src)
        check("fault: 输出目录不可用 → 可读失败（不崩）",
              res.status == "failed" and bool(res.message), f"{res.status} {res.message[:60]}")

        # 源文件在处理前被删掉
        gone = tmp / "gone.mgg"
        gone.write_bytes(fx.build_ncm(fx.PAYLOAD_MP3))
        gone.unlink()
        res = pipe.process_file(gone)
        check("fault: 源文件不存在 → 可读失败（不崩）",
              res.status == "failed" and "读取" in res.message, f"{res.status} {res.message}")

        # 目录被当成输入文件
        res = pipe.process_file(tmp)
        check("fault: 传进来一个目录 → 可读失败（不崩）",
              res.status == "failed" and bool(res.message), f"{res.status} {res.message}")

        # 取消：必须在检查点优雅退出，且不留输出
        src2 = tmp / "cancel.mgg"
        src2.write_bytes(fx.build_ncm(fx.PAYLOAD_MP3))
        out2 = tmp / "out_cancel"
        pipe2 = Pipeline(Settings(output_dir=str(out2), enable_fallback=False,
                                  write_metadata=False, fetch_cover=False,
                                  remember_hashes=False), cache_path=tmp / "c_cancel.json",
                         sources=[])
        pipe2.cancel()
        try:
            pipe2.process_file(src2)
            ok, extra = False, "未取消"
        except CancelledError as exc:
            ok, extra = True, exc.message
        except Exception as exc:  # noqa: BLE001
            ok, extra = False, f"{type(exc).__name__}"
        check("fault: 取消后立即退出（CancelledError）且不产出文件",
              ok and not out2.exists(), extra)
        results = pipe2.process_many([src2, src2])
        check("fault: 取消状态下的批处理不崩（返回空结果）", results == [], str(results))

# --------------------------------------------------------------------------- #
# 4b) 极端注入：本地"坏服务器"（挂死 / 截断 / 巨大声明 / 500 / 网页）
# --------------------------------------------------------------------------- #
def test_extreme_server() -> None:
    print("\n[extreme injection: 本地坏服务器]")
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            mode = self.path.strip("/").split("?")[0]
            try:
                if mode == "hang":
                    time.sleep(20)
                    return
                if mode == "truncate":                       # 声明 1500，只发 1000
                    body = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 992
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body) + 500))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if mode == "huge":                           # 声明 4GB
                    self.send_response(200)
                    self.send_header("Content-Length", str(4 * 1024 ** 3))
                    self.end_headers()
                    self.wfile.write(b"x" * 128)
                    return
                if mode == "500":
                    self.send_error(500, "boom")
                    return
                if mode == "html":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(30 * 1000))
                    self.end_headers()
                    self.wfile.write(b"<html><body>login</body></html>" * 1000)
                    return
                if mode == "gzipbad":                        # 声称 gzip 但不是
                    self.send_response(200)
                    self.send_header("Content-Encoding", "gzip")
                    self.send_header("Content-Length", "20000")
                    self.end_headers()
                    self.wfile.write(os.urandom(20000))
                    return
                self.send_error(404)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):  # noqa: D102
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        # 挂死：必须在超时内失败（不无限等待）
        t0 = time.perf_counter()
        try:
            HttpClient(timeout=2.0, retries=0).request("GET", f"{base}/hang")
            ok, note = False, "未失败"
        except MusicDecryptorError as exc:
            ok, note = True, exc.message
        except Exception as exc:  # noqa: BLE001
            ok, note = False, f"{type(exc).__name__}"
        dt = time.perf_counter() - t0
        check(f"extreme: 服务端挂死 → 超时放弃且有界（{dt:.1f}s）", ok and dt < 8, note)

        # 截断：声明长度 > 实际发送 → 必须判"下载不完整"
        h = HttpClient(timeout=5.0, retries=0)
        try:
            h.download(f"{base}/truncate")
            ok, note = False, "未失败"
        except MusicDecryptorError as exc:
            ok, note = ("截断" in exc.message or "不完整" in exc.message), exc.message
        check("extreme: 响应被截断 → 判为不完整（不产出半个文件）", ok, note)

        # 4GB 声明：必须中止而不是傻等/爆内存
        t0 = time.perf_counter()
        try:
            h.download(f"{base}/huge")
            ok, note = False, "未失败"
        except MusicDecryptorError as exc:
            ok, note = True, exc.message
        dt = time.perf_counter() - t0
        check(f"extreme: 4GB 声明 → 有界中止（{dt:.1f}s）", ok and dt < 10, note)

        # 500：按既有语义作为状态返回（交给上层逐源切换）
        status, _b, _hd = h.request("GET", f"{base}/500")
        check("extreme: HTTP 500 → 返回状态而不是抛异常（可逐源切换）", status == 500, str(status))

        # 网页冒充音频：必须被认出并拒绝
        try:
            h.download(f"{base}/html")
            ok, note = False, "未失败"
        except MusicDecryptorError as exc:
            ok, note = ("网页" in exc.message or "不是音频" in exc.message), exc.message
        check("extreme: 网页冒充音频 → 明确拒绝", ok, note)

        # 伪造 gzip：不得崩（解压失败就按原样处理）
        try:
            data = h.get_bytes(f"{base}/gzipbad")
            ok = isinstance(data, bytes) and len(data) > 0
            note = f"{len(data)}B"
        except MusicDecryptorError as exc:
            ok, note = True, exc.message          # 可读失败也算过
        except Exception as exc:  # noqa: BLE001
            ok, note = False, f"{type(exc).__name__}: {exc}"
        check("extreme: 伪造 gzip 不崩（解压失败按原样/可读失败）", ok, note)

        # 两个实例同时用同一个台账文件（用户开了两份程序）
        from mmusic.search.index import ResourceIndex
        with tempfile.TemporaryDirectory() as tmp:
            a = ResourceIndex(Path(tmp) / "shared.db")
            b = ResourceIndex(Path(tmp) / "shared.db")
            try:
                for i in range(12):
                    a.note_resource(f"k{i}", "kuwo", "u", ext="mp3", bitrate=128, size=1, ok=True)
                    b.note_resource(f"k{i}", "migu", "u", ext="mp3", bitrate=128, size=1, ok=True)
                rows = a.health() + b.health()
                ok = True
            except Exception as exc:  # noqa: BLE001
                ok, rows = False, f"{type(exc).__name__}: {exc}"
            finally:
                a.close()
                b.close()
            check("extreme: 两个实例共用一个台账文件不崩（WAL 并发）", ok, str(rows)[:80])
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- #
# 5) 内存稳定性 / 泄漏
# --------------------------------------------------------------------------- #
def test_memory(quick: bool) -> None:
    print("\n[memory: 重复处理、句柄churn、日志上限]")
    if rss_mb() == 0:
        print("  (skip) 非 Windows 环境，跳过工作集测量")
        return
    iters = 40 if quick else 300
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "a.ncm"
        src.write_bytes(fx.build_ncm(fx.PAYLOAD_MP3))
        settings = Settings(output_dir=str(tmp / "out"), overwrite=True, enable_fallback=False,
                            write_metadata=False, fetch_cover=False, remember_hashes=False)
        logs: "list[str]" = []
        pipe = Pipeline(settings, cache_path=tmp / "c.json", on_log=logs.append, sources=[])
        base = rss_mb()
        marks: "list[float]" = []
        for i in range(iters):
            res = pipe.process_file(src)
            if res.status != "decoded":
                check("memory: 重复处理保持成功", False, f"#{i} {res.status} {res.message}")
                break
            if i % max(1, iters // 6) == 0:
                marks.append(rss_mb())
        pipe.close()
        final = rss_mb()
        growth = final - base
        first_half = (marks[len(marks) // 2] - marks[0]) if len(marks) > 2 else 0.0
        second_half = (marks[-1] - marks[len(marks) // 2]) if len(marks) > 2 else 0.0
        check(f"memory: {iters} 次重复处理工作集增长有界（{growth:+.1f} MB）",
              growth < 60, f"base={base:.1f} final={final:.1f}")
        check("memory: 没有持续泄漏趋势（后半段增长 ≤ 前半段 + 15MB）",
              second_half <= first_half + 15, f"first={first_half:+.1f} second={second_half:+.1f}")

        # 句柄 churn：HttpClient / 台账 / 哈希缓存反复创建销毁
        base2 = rss_mb()
        from mmusic.hashcache import HashCache
        from mmusic.search.index import ResourceIndex
        for _ in range(60 if quick else 200):
            h = HttpClient(timeout=1.0, retries=0)
            h.close()
            hc = HashCache(tmp / "h.json")
            hc.add(f"k{_}", out_path="o", mode="offline", fmt="mp3")
            idx = ResourceIndex(tmp / "r.db")
            idx.note_resource("a", "kuwo", "u", ext="mp3", bitrate=128, size=1, ok=True)
            idx.close()
            time.sleep(0)
        growth2 = rss_mb() - base2
        check(f"memory: 循环创建/销毁客户端与台账无泄漏（{growth2:+.1f} MB）",
              growth2 < 40, f"{growth2:+.1f} MB")

        # UI 日志上限：灌 2 万行（含明细）不得无限增长
        try:
            from PySide6.QtWidgets import QApplication
            from ui.main_window import MainWindow
            app = QApplication.instance() or QApplication([])
            cfg = AppConfig(path=tmp / "gui.json")
            cfg.settings.output_dir = str(tmp / "gui_out")
            win = MainWindow(cfg)
            base3 = rss_mb()
            for i in range(20000):
                win._log(f"明细 {i}", "detail")
                win._log(f"结论 {i}", "result")
            blocks = win.log.blockCount()
            growth3 = rss_mb() - base3
            win.chk_detail.setChecked(True)
            app.processEvents()
            check(f"memory: 2 万行日志后控件仍有上限（{blocks} 行）", blocks <= 4000, str(blocks))
            check(f"memory: 2 万行日志内存增长有界（{growth3:+.1f} MB）", growth3 < 80, f"{growth3:+.1f} MB")
            win.chk_detail.setChecked(False)
            win.close()
        except Exception as exc:  # noqa: BLE001
            check("memory: UI 日志上限检查可执行", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="只跑关键子集")
    ap.add_argument("--soak", action="store_true", help="深度浸泡：fuzz 万级变异（数分钟）")
    ap.add_argument("--seed", type=int, default=20261020, help="fuzz 随机种子（换种子=换探索面）")
    args = ap.parse_args()
    t0 = time.perf_counter()
    test_boundary(args.quick)
    test_negative(args.quick)
    test_fuzz(args.quick, args.soak, args.seed)
    test_fault_injection()
    test_extreme_server()
    test_memory(args.quick)
    dt = time.perf_counter() - t0
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed   （{dt:.1f}s）")
    if FAILED:
        print("failures:")
        for f in FAILED:
            print("  -", f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

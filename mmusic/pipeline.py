"""The orchestrator: offline decode first, download fallback second.

Control flow for every input file::

    sniff ─┬─ plain  ─────────────────────────────► copy + tag
           ├─ ncm    ── decrypt ── ok ───────────► write + tag
           ├─ qmc    ── decrypt ── ok ───────────► write + tag
           │                    └─ needs-online ─┐
           └─ unknown ───────────────────────────┤
                                                ▼
                         fallback: for each source -> search -> rank ->
                         resolve -> download -> validate -> write + tag
                         (all exhausted -> SearchNoResultError)
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import default_config_dir
from .crypto.kwm import KwmDecoder
from .crypto.ncm import NcmDecoder
from .crypto.qmc import QmcDecoder
from .downloader import check_completeness, validate
from .duration import TOL_ABS, TOL_REL, audio_duration, consensus, format_seconds
from .errors import (CancelledError, DecryptionFailedError, MusicDecryptorError,
                     NeedsOnlineError, NetworkError, SearchNoResultError,
                     UnsupportedFormatError)
from .hashcache import HashCache
from .match import (best_reading_match, filename_query, parse_song_filename_candidates,
                    rank_tracks_multi, reading_scores, split_originals,
                    version_markers)
from .metadata import write_tags
from .model import AudioResult, Track
from .net import HttpClient
from .search.fusion import (collapse_duplicates, dedupe_key,
                            mark_low_confidence)
from .search.index import ResourceIndex
from .search.query import MAX_PER_SOURCE as MAX_VARIANTS_PER_SOURCE
from .search.query import expand as expand_query
from .search.registry import build_sources
from .sniff import sniff_path
from .utils import sha256_file

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

MIN_ACCEPT = 0.34          # 低于此分不进入下载候选池
MIN_TITLE_SIMILARITY = 0.60  # 歌名字段硬门槛：歌名不像就直接淘汰（"同歌手另一首"不许混进来）
MIN_ARTIST_SIMILARITY = 0.60  # 歌手字段硬门槛：同名翻唱/不同演唱者一律淘汰
MIN_TRACK_SECONDS = 45.0     # 实测时长下限：低于此秒数视为试听片段/前奏，不是完整原曲
EARLY_STOP_SCORE = 0.95    # 已拿到高置信度原曲（歌名+歌手几乎完全一致）就不再打扰其它音源
MAX_DOWNLOADS = 6          # 单文件最多尝试的下载次数（按分数从高到低顺延）
MAX_PROBES = 2             # 最多探测几个候选（静默的置信度操作，成本必须封顶）
MAX_PROBES_WIDE = 4        # 平台标注互相矛盾时多测几个：只测前 2 个可能全是缩短版
PROBE_TIMEOUT = 8.0        # 探测用更短超时：慢就跳过，绝不拖慢主流程
PROBE_BYTES = 256 * 1024   # 探测只取文件头，绝不整文件下载
PROBE_TAIL_BYTES = 128 * 1024  # ogg / 非 faststart 的 m4a 需要读文件尾

__all__ = ["Pipeline", "JobResult"]


def _needs_tail(head: bytes) -> bool:
    """True when the container keeps its length at the end of the file."""
    if len(head) < 12:
        return False
    if head[:4] == b"OggS":
        return True                      # length = last page's granule position
    if head[4:8] == b"ftyp":
        return b"moov" not in head[:256 * 1024]   # moov not faststarted -> at the end
    return False


def _plausible_max(values) -> "float | None":
    """最完整但"不离群"的时长。

    取最大值是刻意的：同一首歌在不同平台出现长短不一时，短的那个几乎总是被截短/
    剪辑的版本（实测 周杰伦《最长的电影》：酷我 2:22、酷狗 3:55）。同时用"不超过
    中位数 2 倍"过滤掉个别平台标注异常偏大的离群值，避免把参考值抬飞。"""
    vals = sorted(v for v in values if v)
    if not vals:
        return None
    mid = len(vals) // 2
    median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    plausible = [v for v in vals if v <= 2 * median]
    return max(plausible) if plausible else vals[-1]


def _guarded(fn):
    """Wrap a caller-supplied callback so a broken console / sink can never
    abort a decode job (e.g. UnicodeEncodeError printing emoji on GBK)."""
    if fn is None:
        return lambda *_a, **_k: None

    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            return None

    return inner


@dataclass
class JobResult:
    path: str
    name: str
    status: str            # decoded | downloaded | copied | skipped | failed
    mode: str = ""         # offline | fallback | plain
    out_path: str = ""
    fmt: str = ""
    bitrate: int = 0
    source: str = ""
    message: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("decoded", "downloaded", "copied")


class Pipeline:
    def __init__(self, settings, *, cache_path: "str | Path | None" = None,
                 on_log: "Callable[[str], None] | None" = None,
                 on_progress: "Callable[[int, int], None] | None" = None,
                 on_file: "Callable[[JobResult], None] | None" = None,
                 sources: "list | None" = None,
                 index_path: "str | Path | None" = None) -> None:
        self.settings = settings
        self._log = _guarded(on_log)
        self._progress = _guarded(on_progress)
        self._on_file = _guarded(on_file)
        self.http = HttpClient(timeout=settings.timeout, retries=settings.retries)
        if cache_path:
            cache_file = Path(cache_path)
        elif settings.output_dir:
            cache_file = Path(settings.output_dir) / ".mmusic-hashcache.json"
        else:
            # never drop the ledger into the cwd (for a frozen exe that is its
            # own install folder); keep it beside the settings instead
            cache_file = default_config_dir() / "hashcache.json"
        self.cache = HashCache(cache_file)
        # 资源台账（内容去重记忆 / 负缓存 / 源健康）。与哈希缓存共用同一个开关：
        # 用户关掉“记住以免重复工作”时，两者一起停用。
        index_file = Path(index_path) if index_path else cache_file.parent / 'resources.db'
        self.index = ResourceIndex(index_file, enabled=bool(settings.remember_hashes))
        self.sources = list(sources) if sources is not None else build_sources(self.http, settings.to_dict())
        self._adapters = {s.name: s for s in self.sources}
        self._cancel = False

    # ------------------------------------------------------------------ #
    def cancel(self) -> None:
        self._cancel = True

    def close(self) -> None:
        self.http.close()
        try:
            self.index.close()
        except Exception:  # noqa: BLE001
            pass

    def _out_dir_for(self, src: "str | Path") -> Path:
        """Resolve where a produced file goes.

        An explicitly configured ``output_dir`` wins; otherwise the output is
        written **next to the source file** - exactly what the UI promises -
        never the process working directory (which for a double-clicked exe
        would be its own install folder, ``dist``).
        """
        configured = (self.settings.output_dir or "").strip()
        target = Path(configured).expanduser() if configured else Path(src).resolve().parent
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MusicDecryptorError("无法创建输出目录", detail=f"{target} :: {exc}") from exc
        if not os.access(target, os.W_OK):
            raise MusicDecryptorError(
                "输出目录不可写，请在“设置”里指定一个可写目录", detail=str(target))
        return target

    # ------------------------------------------------------------------ #
    def process_many(self, paths) -> list[JobResult]:
        paths = list(paths)
        total = len(paths)
        results: list[JobResult] = []
        for i, path in enumerate(paths):
            if self._cancel:
                self._log("⛔ 已取消")
                break
            try:
                res = self.process_file(path)
            except CancelledError:
                self._log("⛔ 已取消")
                break
            except MusicDecryptorError as exc:
                res = JobResult(str(path), Path(path).name, "failed",
                                message=exc.message, detail=exc.detail)
            except Exception as exc:  # noqa: BLE001 - never let one file kill the batch
                res = JobResult(str(path), Path(path).name, "failed",
                                message="未预期的错误", detail=str(exc))
            results.append(res)
            self._on_file(res)
            self._progress(i + 1, total)
        return results

    def process_file(self, path) -> JobResult:
        p = Path(path)
        name = p.name
        if self._cancel:
            raise CancelledError("已取消")

        try:
            fmt = sniff_path(p)
        except OSError as exc:
            return JobResult(str(p), name, "failed", message="无法读取文件", detail=str(exc))

        digest = ""
        if self.settings.remember_hashes:
            try:
                digest = sha256_file(p)
            except OSError:
                digest = ""
            rec = self.cache.get(digest) if digest else None
            if rec and rec.get("out") and Path(rec["out"]).exists():
                self._log(f"⏭ 已处理过（哈希命中），跳过：{name}")
                return JobResult(str(p), name, "skipped", mode=rec.get("mode", ""),
                                 out_path=rec["out"], fmt=rec.get("fmt", ""),
                                 message="哈希命中缓存，已跳过")

        try:
            if fmt.kind == "plain":
                result = AudioResult(audio=p.read_bytes(), ext=fmt.out_ext)
                mode = "plain"
            elif fmt.kind == "kwm":
                self._log(f"🔧 离线解密 KWM：{name}")
                result = KwmDecoder(p.read_bytes()).decode()
                if not result.meta.get("audio_sniffed", True):
                    self._log(f"    ⚠ {name}：KWM 解出的音频头无法识别（文件可能不完整）")
                mode = "offline"
            elif fmt.kind == "ncm":
                self._log(f"🔧 离线解密 NCM：{name}")
                result = NcmDecoder(p.read_bytes()).decode()
                mode = "offline"
            elif fmt.kind == "qmc":
                if fmt.needs_online:
                    raise NeedsOnlineError(fmt.note)
                self._log(f"🔧 离线解密 QMC：{name}")
                result = QmcDecoder(p.read_bytes(), fmt.ext).decode()
                mode = "offline"
            else:
                raise UnsupportedFormatError("无法识别的加密格式", detail=fmt.ext or "未知扩展名")
        except NeedsOnlineError as exc:
            self._log(f"🔒 需联网破解：{name} — {exc.message}")
            if not self.settings.enable_fallback:
                return JobResult(str(p), name, "failed", message=exc.message,
                                 detail="备用下载已禁用")
            return self._fallback_result(p, name, digest, note=exc.message)
        except (DecryptionFailedError, UnsupportedFormatError) as exc:
            self._log(f"⚠ 离线解密失败：{name} — {exc.message}")
            if not self.settings.enable_fallback:
                return JobResult(str(p), name, "failed", message=exc.message)
            return self._fallback_result(p, name, digest, note=exc.message)

        try:
            return self._write_decoded(p, name, result, mode, digest)
        except MusicDecryptorError as exc:
            self._log(f"❌ 写入失败：{name} — {exc}")
            return JobResult(str(p), name, "failed", message=exc.message, detail=exc.detail)
        except OSError as exc:
            self._log(f"❌ 写入失败：{name} — {exc}")
            return JobResult(str(p), name, "failed", message="写入输出文件失败", detail=str(exc))

    # ------------------------------------------------------------------ #
    def _write_decoded(self, src: Path, name: str, result: AudioResult, mode: str, digest: str) -> JobResult:
        out_dir = self._out_dir_for(src)
        title = result.title or src.stem
        artist = result.artist
        base = self._safe_name(artist, title)

        # an input that is *already* plain audio and would land on its own
        # filename needs no copy at all
        if (mode == "plain" and out_dir == src.resolve().parent
                and f"{base}.{result.ext}".lower() == src.name.lower()):
            self._log(f"⏭ 已是明文音频，无需处理：{src.name}")
            return JobResult(str(src), src.name, "skipped", mode="plain",
                             out_path=str(src), fmt=result.ext, message="已是明文，无需处理")

        out_path = self._unique(out_dir, base, result.ext)
        out_path.write_bytes(result.audio)

        cover = result.cover
        if not cover and self.settings.fetch_cover and result.cover_url:
            cover = self._fetch_cover(result.cover_url)
        if self.settings.write_metadata:
            write_tags(out_path, title=title, artist=artist, album=result.album, cover=cover)

        if digest and self.settings.remember_hashes:
            self.cache.add(digest, out_path=str(out_path), mode=mode, fmt=result.ext)
        if not self.settings.keep_original:
            try:
                src.unlink()
            except OSError:
                pass

        status = "copied" if mode == "plain" else "decoded"
        tag = "解密" if mode == "offline" else "复制"
        self._log(f"✅ {tag}完成：{name} → {out_path.name}  [{result.ext.upper()}]")
        return JobResult(str(src), name, status, mode=mode, out_path=str(out_path),
                         fmt=result.ext, message="完成")

    # ------------------------------------------------------------------ #
    def _fallback(self, src: Path, digest: str, *, note: str) -> JobResult:
        readings = parse_song_filename_candidates(src.stem) or [(src.stem, "")]
        query = filename_query(src.stem) or src.stem
        self._log(f"🔎 触发备用搜索：{query}")
        if len(readings) > 1:
            self._log("  · 文件名有两种读法：" +
                      " / ".join(f"歌名「{t}」歌手「{a}」" for t, a in readings))

        # 文件名自带版本标记 -> 用户主动点名要该版本：不过滤，但必须警告
        requested = version_markers(src.stem)
        if requested:
            self._log("  · ⚠ 文件名包含版本标记「" + "、".join(sorted(requested)) +
                      "」，本次将保留该版本；若你要的是原曲，请把文件名改干净后重试")

        adapters = self._adapters
        index = self.index
        pool: list[Track] = []          # 跨音源的"干净"候选池
        dropped_total = 0
        dropped_artist = 0

        # P3：查询扩展 —— 同一首歌多试几种写法，冷门/名字怪异的曲子命中率明显更高
        variants = expand_query(query, readings)
        if len(variants) > 1:
            self._log("  · 检索变体：" + " / ".join(f"{v.reason}「{v.text}」" for v in variants))

        for source in self.sources:
            if self._cancel:
                raise CancelledError("已取消")
            kept: "list[Track]" = []
            for variant in variants[:MAX_VARIANTS_PER_SOURCE]:
                kept, off_title_n, off_artist_n, dropped_ver = self._search_and_gate(
                    source, variant, readings, requested)
                dropped_total += dropped_ver
                dropped_artist += off_artist_n
                if kept:
                    if variant is not variants[0]:
                        self._log(f"  · [{source.label}] ✅ 命中于检索变体「{variant.reason}」")
                    break
            if not kept:
                continue
            tracks = kept

            rank_tracks_multi(readings, tracks, prefer_ext=self.settings.quality)
            best_t, best_a = reading_scores(readings, tracks[0],
                                            prefer_ext=self.settings.quality)[0][1]
            self._log(f"  · [{source.label}] {len(tracks)} 个原曲候选，最佳“{tracks[0].title}”"
                      f"({tracks[0].artist}) 匹配度 {tracks[0].score:.2f}"
                      f"（采用读法：歌名「{best_t}」/ 歌手「{best_a}」）")
            # 人工补链是用户的显式选择：连"匹配分门槛"也一并跳过
            # （文件名/标题当然不会与查询相似，但下载校验照旧）。
            threshold = 0.0 if getattr(source, "trusted_links", False) else MIN_ACCEPT
            pool.extend(t for t in tracks[:3] if t.score >= threshold)

        if not pool:
            if dropped_artist:
                raise SearchNoResultError(
                    "未找到原曲：仅找到同名但演唱者不同的版本（翻唱/翻录），已全部排除。",
                    detail=f"已排除 {dropped_artist} 个歌手不符的候选")
            if dropped_total and not requested:
                raise SearchNoResultError(
                    "未找到原曲，当前仅有伴奏/改编版，请确认是否需要下载。",
                    detail=f"已排除 {dropped_total} 个伴奏/改编版本")
            raise SearchNoResultError("搜索无结果，请检查网络或歌曲名")

        # 跨音源择优：不再"第一个能用的音源就算"，而是所有音源一起比，分高者先下
        pool.sort(key=lambda t: t.score, reverse=True)

        # ---- 时长核验（静默、跨平台）-------------------------------------- #
        # 参考时长绝不能只信"正在提供文件的这个平台"：实测同一首歌在不同平台的标注
        # 可能一个是被截短的（周杰伦《最长的电影》：酷我 2:22 / 酷狗 3:55，后者才对）。
        # 因此跨平台取"最完整且不离群"的标注作为参考，全程自动、不打扰用户。
        declared = ([t.duration for t in pool if t.duration and t.score >= EARLY_STOP_SCORE]
                    or [t.duration for t in pool if t.duration])
        reference = _plausible_max(declared)
        tolerance = max(TOL_ABS, TOL_REL * reference) if reference else TOL_ABS
        if reference:
            self._log(f"  · 跨平台参考时长 {format_seconds(reference)}"
                      f"（{len(declared)} 个平台标注中取最完整值，容差 ±{int(tolerance)}s）")

        # 免费预筛：标注就明显偏短的候选连文件都不用取（省一次请求）
        short_meta = [t for t in pool
                      if reference and t.duration and t.duration < reference - tolerance]
        for track in short_meta[:3]:
            self._log(f"    ✗ 标注时长偏短已排除：{track.source} “{track.title}” "
                      f"{format_seconds(track.duration)}（参考 {format_seconds(reference)}）")
        candidates = [t for t in pool if t not in short_meta]
        if not candidates:
            raise SearchNoResultError(
                "未找到原曲，当前仅有伴奏/改编版，请确认是否需要下载。",
                detail="各平台标注时长互相矛盾，偏短版本已全部排除")

        # 实测交叉验证：只取文件头（最多 MAX_PROBES 个、超时 8s、失败即跳过）
        self._log(f"  · 开始时长核验（探测前 {min(len(candidates), MAX_PROBES)} 个候选）")
        self._probe_durations(candidates)
        measured = [t.actual_duration for t in candidates if t.actual_duration]
        if len(measured) >= 2:
            # 有两个以上真实文件测出了时长 —— 这是"原曲原时长"最硬的一手证据，
            # 直接以它为准，平台标注只作旁证（标注本身可能就是被剪短版本的）。
            measured_ref = _plausible_max(measured)
            if measured_ref:
                reference = measured_ref
                tolerance = max(TOL_ABS, TOL_REL * reference)
                self._log(f"  · 参考时长 {format_seconds(reference)}"
                          f"（取自 {len(measured)} 个候选的**实测**值，容差 ±{int(tolerance)}s）")
        elif measured:
            # 只测到一个：与平台标注一起取"最完整且不离群"的值，
            # 避免唯一测到的那个恰好是缩短版、把参考值压低。
            extra = _plausible_max(list(measured) + list(declared))
            if extra and (reference is None or extra > reference):
                reference = extra
                self._log(f"  · 参考时长 {format_seconds(reference)}"
                          f"（实测 + 平台标注综合取值，容差 ±{int(TOL_REL * reference)}s）")
        tolerance = max(TOL_ABS, TOL_REL * reference) if reference else TOL_ABS

        shortened = [t for t in candidates
                     if t.actual_duration and reference and t.actual_duration < reference - tolerance]
        for track in shortened[:3]:
            self._log(f"    ✗ 时长异常已排除：{track.source} “{track.title}” "
                      f"实测 {format_seconds(track.actual_duration)}"
                      f"（参考 {format_seconds(reference)}）")
        too_short = [t for t in candidates
                     if t.actual_duration and t.actual_duration < MIN_TRACK_SECONDS]
        for track in too_short[:3]:
            self._log(f"    ✗ 时长过短已排除：{track.source} “{track.title}” "
                      f"仅 {format_seconds(track.actual_duration)}"
                      f"（< {int(MIN_TRACK_SECONDS)}s，疑似片段）")

        verified = [t for t in candidates
                    if t.actual_duration and t.actual_duration >= MIN_TRACK_SECONDS
                    and (not reference or t.actual_duration >= reference - tolerance)]
        unverified = [t for t in candidates if not t.actual_duration]

        note = ""
        if not verified:
            if shortened or too_short or short_meta:
                note = "时长存疑"
                self._log("  · ⚠ 可下载候选均短于参考时长，已按可疑版本处理")
            else:
                note = "时长未比对"
                self._log("  · 未能取得音频时长，本结果未做时长比对")
        elif len(verified) == 1 and len(candidates) == 1:
            note = "单一候选未比对"
            self._log("  · ⚠ 仅一个候选，无法交叉比对时长")
        else:
            self._log(f"  · 时长核验通过 {len(verified)} 个候选，将从最可信的开始下载")

        # 用户要求：**最终只下载通过时长验证的资源**。
        # 只要拿到了参考时长，测不出/对不上的候选就一律不作为可信选项，
        # 绝不用"没验证过的"去凑一个结果（宁可如实说没找到原曲）。
        known_bad = {id(t) for t in shortened} | {id(t) for t in too_short}
        if reference is not None:
            order = verified
            if not order:
                excluded = len(short_meta) + len(known_bad)
                if excluded:
                    raise SearchNoResultError(
                        "未找到原曲，当前仅有伴奏/改编版，请确认是否需要下载。",
                        detail=f"已排除 {excluded} 个时长偏短的候选")
                raise SearchNoResultError(
                    "候选音源均不可用（无版权/校验失败/反爬），请更换关键词或手动补全直链",
                    detail="已有参考时长，但候选都没能测出音频时长")
        else:
            # 连参考时长都取不到：如实标注"未比对"后再兜底，不让用户白等
            order = verified or [t for t in candidates if id(t) not in known_bad]

        # ---- 内容去重：同一首歌经多个源命中时只保留排名最高的一个 ---------- #
        order, duplicates = collapse_duplicates(order)
        if duplicates:
            for dup, keeper in duplicates[:3]:
                self._log(f"    ✗ 重复资源已合并：{dup.source} “{dup.title}” 与 "
                          f"{keeper.source} “{keeper.title}” 同曲，保留 {keeper.source}")
            self._log(f"  · 去重后候选 {len(order)} 个（合并 {len(duplicates)} 个重复）")
        flagged = mark_low_confidence(order)
        if flagged and flagged == len(order):
            self._log("  · ⚠ 所有候选匹配度都偏低，结果可能不准确")

        def _remember(track: Track, ok: bool) -> None:
            """把这次资源结果写进台账（失败也记，便于下次不再白试）。"""
            index.note_resource(dedupe_key(track), track.source, track.url or "",
                                ext=track.ext, bitrate=track.bitrate, size=track.size, ok=ok)

        tried = 0
        for track in order[:MAX_DOWNLOADS]:
            if self._cancel:
                raise CancelledError("已取消")
            adapter = adapters.get(track.source)
            if adapter is None:
                continue
            tried += 1
            if not track.url:
                try:
                    track = adapter.resolve(track, quality=self.settings.quality)
                except MusicDecryptorError as exc:
                    self._log(f"    - [{track.source}] 直链不可用：{exc.message}")
                    _remember(track, ok=False)
                    continue
            try:
                data = self._download(track)
            except MusicDecryptorError as exc:
                self._log(f"    - [{track.source}] 下载失败：{exc.message}")
                _remember(track, ok=False)
                continue
            v = validate(data, track.ext, expected_bitrate=track.bitrate,
                         duration=track.actual_duration or track.duration,
                         min_bitrate=self.settings.min_bitrate)
            if not v.ok:
                self._log(f"    - [{track.source}] 校验未通过：{v.reason}")
                _remember(track, ok=False)
                continue

            # 完整度门禁：下载回来的东西必须真的是一整首歌
            ok_len, why_len, got_secs = check_completeness(
                data, reference=reference or 0.0, tolerance=tolerance,
                min_seconds=MIN_TRACK_SECONDS, measured=track.actual_duration or 0.0)
            if not ok_len:
                self._log(f"    ✗ 完整度未通过：{track.source} “{track.title}” —— {why_len}")
                _remember(track, ok=False)
                continue
            if got_secs:
                track.actual_duration = track.actual_duration or got_secs
            runner = next((t for t in pool if t is not track), None)
            if runner is not None and runner.score < track.score:
                self._log(f"  · 择优采用：{track.source} “{track.title}”({track.artist}) "
                          f"{track.score:.2f} 优于 次选 {runner.source} “{runner.title}” {runner.score:.2f}")
            _remember(track, ok=True)
            return self._write_downloaded(src, track, data, v, digest, note=note)

        raise SearchNoResultError(
            "候选音源均不可用（无版权/校验失败/反爬），请更换关键词或手动补全直链",
            detail=note)

    def _write_downloaded(self, src: Path, track: Track, data: bytes, v, digest: str,
                          *, note: str = "") -> JobResult:
        out_dir = self._out_dir_for(src)
        base = self._safe_name(track.artist, track.title or src.stem)
        out_path = self._unique(out_dir, base, v.ext)
        out_path.write_bytes(data)

        cover = b""
        if self.settings.fetch_cover:
            pic_id = track.raw.get("pic_id")
            agg = self._aggregator()
            if pic_id and agg is not None and hasattr(agg, "fetch_pic"):
                pic_url = agg.fetch_pic(str(pic_id))
                if pic_url:
                    cover = self._fetch_cover(pic_url)
        if self.settings.write_metadata:
            write_tags(out_path, title=track.title, artist=track.artist,
                       album=track.album, cover=cover)

        if digest and self.settings.remember_hashes:
            self.cache.add(digest, out_path=str(out_path), mode="fallback", fmt=v.ext, source=track.source)
        if not self.settings.keep_original:
            try:
                src.unlink()
            except OSError:
                pass

        br = f"，{v.bitrate}kbps" if v.bitrate else ""
        dur = f"，时长 {format_seconds(track.actual_duration)}" if track.actual_duration else ""
        self._log(f"✅ 备用下载完成：{src.name} → {out_path.name}  [{v.ext.upper()}{br}{dur}] "
                  f"来源：{track.source}" + (f"  ⚠ {note}" if note else ""))
        return JobResult(str(src), src.name, "downloaded", mode="fallback", out_path=str(out_path),
                         fmt=v.ext, bitrate=v.bitrate, source=track.source,
                         message="完成" if not note else f"完成（{note}）")

    # ------------------------------------------------------------------ #
    def _aggregator(self):
        for s in self.sources:
            if s.name == "aggregator":
                return s
        return None

    @staticmethod
    def _local_path(url: str) -> "Path | None":
        """Filesystem path behind a ``file:`` URL, else None."""
        if not url:
            return None
        if not url.startswith("file:"):
            # tolerate a bare filesystem path from a custom/3rd-party source
            if "://" not in url:
                candidate = Path(url)
                if candidate.is_file():
                    return candidate
            return None
        from urllib.parse import urlparse
        from urllib.request import url2pathname
        parsed = urlparse(url)
        raw = url2pathname(parsed.path or parsed.netloc or "")
        return Path(raw) if raw else None

    def _probe_duration(self, track: Track) -> "float | None":
        """Actual audio length of a resolved candidate, from a header-only probe."""
        url = track.url or ""
        if not url:
            raise NetworkError("尚未解析出直链")
        local = self._local_path(url)
        if local is not None:
            if not local.is_file():
                raise NetworkError("本地文件不可读", detail=str(local))
            size = local.stat().st_size
            with open(local, "rb") as handle:
                head = handle.read(PROBE_BYTES)
            duration = audio_duration(head, size)
            if duration is None and _needs_tail(head) and size > PROBE_BYTES:
                with open(local, "rb") as handle:
                    handle.seek(max(0, size - PROBE_TAIL_BYTES))
                    tail = handle.read(PROBE_TAIL_BYTES)
                duration = audio_duration(head, size, tail)
            return duration
        headers = {"Referer": self._referer_for(track)}
        head, total = self.http.probe(url, limit=PROBE_BYTES, timeout=PROBE_TIMEOUT,
                                      headers=headers)
        duration = audio_duration(head, total)
        if duration is None and _needs_tail(head):
            try:
                tail, total2 = self.http.probe_tail(url, limit=PROBE_TAIL_BYTES,
                                                    timeout=PROBE_TIMEOUT, headers=headers)
                duration = audio_duration(head, total2 or total, tail)
            except MusicDecryptorError:
                pass
        return duration

    def _fallback_result(self, src: Path, name: str, digest: str, *, note: str) -> JobResult:
        """Run the fallback and turn any expected failure into a JobResult.

        Keeps ``process_file``'s contract uniform (it always *returns* a
        result, never raises for an expected failure) while still letting a
        cancellation propagate so the batch can stop."""
        try:
            return self._fallback(src, digest, note=note)
        except CancelledError:
            raise
        except MusicDecryptorError as exc:
            self._log(f"❌ {name}：{exc}")
            return JobResult(str(src), name, "failed", message=exc.message, detail=exc.detail)

    def _search_and_gate(self, source, variant, readings, requested):
        """检索一个源的一个查询变体，并跑完三道门槛。

        返回 ``(可用候选, 歌名淘汰数, 歌手淘汰数, 版本淘汰数)``；
        出错 / 无结果 / 全被淘汰时返回空列表，由调用方决定是否换下一个变体。
        """
        index = self.index
        text = variant.text
        blocked = index.query_block_reason(text, source.name)
        if blocked:
            self._log(f"  · [{source.label}] 已跳过（{blocked}，负缓存内）")
            return [], 0, 0, 0
        started = time.monotonic()
        try:
            tracks = source.search(text, quality=self.settings.quality, limit=8)
        except MusicDecryptorError as exc:
            index.note_search(source.name, text, found=False, error=True,
                              seconds=time.monotonic() - started)
            self._log(f"  · [{source.label}] 搜索不可用：{exc.message}")
            return [], 0, 0, 0
        index.note_search(source.name, text, found=bool(tracks),
                          seconds=time.monotonic() - started)
        tracks = [t for t in tracks if t.title or t.url]
        if not tracks:
            self._log(f"  · [{source.label}] 无结果")
            return [], 0, 0, 0

        # 人工补链：用户显式给的链接，跳过歌名/歌手门槛（下载仍会做完整校验）
        if getattr(source, "trusted_links", False):
            self._log(f"  · [{source.label}] 使用你提供的链接（已跳过歌名/歌手门槛）")
            return tracks, 0, 0, 0

        dropped_ver = 0
        # 强制过滤：排除伴奏 / 纯器乐 / 现场 / 改编版（除非用户在文件名里点名要）
        if not requested:
            originals, dropped = split_originals(tracks)
            if dropped:
                dropped_ver = len(dropped)
                sample = "、".join(f"“{t.title}”" for t, _m in dropped[:3])
                self._log(f"  · [{source.label}] 已排除 {len(dropped)} 个伴奏/改编版本：{sample}")
            tracks = originals
            if not tracks:
                self._log(f"  · [{source.label}] 无原曲（返回结果均为伴奏/改编版）")
                return [], 0, 0, dropped_ver

        # 歌名 + 歌手硬门槛（必须**同一个读法**同时达标）
        kept: "list[Track]" = []
        off_artist: "list[Track]" = []
        off_title: "list[Track]" = []
        for cand in tracks:
            ok, _reading, ts, as_ = best_reading_match(
                readings, cand, min_title=MIN_TITLE_SIMILARITY,
                min_artist=MIN_ARTIST_SIMILARITY,
                strict_title=not requested)
            if ok:
                kept.append(cand)
            elif ts >= MIN_TITLE_SIMILARITY and as_ < MIN_ARTIST_SIMILARITY:
                off_artist.append(cand)
            else:
                off_title.append(cand)
        if off_artist:
            self._log(f"  · [{source.label}] 已排除 {len(off_artist)} 个歌手不符的结果："
                      + "、".join(f"“{c.artist}”《{c.title}》" for c in off_artist[:3]))
        if off_title:
            self._log(f"  · [{source.label}] 已排除 {len(off_title)} 个歌名不符的结果："
                      + "、".join(f"“{c.title}”" for c in off_title[:3]))
        if not kept:
            self._log(f"  · [{source.label}] 没有歌名与歌手同时匹配的版本")
        return kept, len(off_title), len(off_artist), dropped_ver

    def _probe_durations(self, tracks: "list[Track]") -> None:
        """Resolve + measure the real length of the top candidates.

        A candidate whose length cannot be measured is left with
        ``actual_duration = None`` and is therefore **never trusted** (it can
        only be used as a last resort, with a warning)."""
        # 平台标注互不相同时多测几个候选：只测前 2 个很可能"全是缩短版"，
        # 真正完整的那个排在后面反而没被测到 —— 这是"原时长对不上"的主因之一。
        declared = [round(t.duration) for t in tracks[:MAX_PROBES_WIDE] if t.duration]
        limit = MAX_PROBES_WIDE if len(set(declared)) > 1 else MAX_PROBES
        for track in tracks[:limit]:
            if self._cancel:
                raise CancelledError("已取消")
            adapter = self._adapters.get(track.source)
            if adapter is None:
                continue
            try:
                if not track.url:
                    adapter.resolve(track, quality=self.settings.quality)
                track.actual_duration = self._probe_duration(track)
            except MusicDecryptorError as exc:
                self._log(f"    - [{track.source}] “{track.title}” 时长探测失败：{exc.message}")
                continue
            if not track.actual_duration:
                self._log(f"    - [{track.source}] “{track.title}” 无法解析出音频时长，视为不可信")
                continue
            self._log(f"    · [{track.source}] “{track.title}” 实测时长 "
                      f"{format_seconds(track.actual_duration)}"
                      f"（接口标注 {format_seconds(track.duration) if track.duration else '无'}）")
            if track.duration and abs(track.actual_duration - track.duration) > max(15.0, 0.10 * track.duration):
                self._log(f"      ⚠ 接口标注与实测相差 "
                          f"{int(abs(track.actual_duration - track.duration))}s，元数据可能不准")

    def _download(self, track: Track) -> bytes:
        url = track.url or ""
        local = self._local_path(url)
        if local is not None:
            if not local.is_file():
                raise NetworkError("本地文件不可读", detail=str(local))
            return local.read_bytes()
        if not url:
            raise NetworkError("该音源未返回直链")
        return self.http.download(url, headers={"Referer": self._referer_for(track)})

    @staticmethod
    def _referer_for(track: Track) -> str:
        return {"kuwo": "https://www.kuwo.cn/", "kugou": "https://www.kugou.com/",
                "migu": "https://music.migu.cn/"}.get(track.source, "")

    def _fetch_cover(self, url: str) -> bytes:
        try:
            data = self.http.get_bytes(url)
            return data if data[:2] in (b"\xff\xd8",) or data[:8] == b"\x89PNG\r\n\x1a\n" else b""
        except MusicDecryptorError:
            return b""

    # ------------------------------------------------------------------ #
    @staticmethod
    def _safe_name(artist: str, title: str, *, limit: int = 150) -> str:
        artist = (artist or "").strip()
        title = (title or "").strip()
        name = f"{artist} - {title}" if artist else title
        name = _UNSAFE.sub("_", name).strip(" .")
        name = re.sub(r"\s+", " ", name)
        if len(name) > limit:
            name = name[:limit].rstrip(" .")
        return name or "track"

    def _unique(self, directory: Path, base: str, ext: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        candidate = directory / f"{base}.{ext}"
        if self.settings.overwrite or not candidate.exists():
            return candidate
        i = 1
        while True:
            candidate = directory / f"{base} ({i}).{ext}"
            if not candidate.exists():
                return candidate
            i += 1

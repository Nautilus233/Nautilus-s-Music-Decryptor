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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import default_config_dir
from .crypto.ncm import NcmDecoder
from .crypto.qmc import QmcDecoder
from .downloader import validate
from .errors import (CancelledError, DecryptionFailedError, MusicDecryptorError,
                     NeedsOnlineError, NetworkError, SearchNoResultError,
                     UnsupportedFormatError)
from .hashcache import HashCache
from .match import (filename_query, parse_song_filename_candidates,
                    rank_tracks_multi, reading_scores, split_originals,
                    version_markers)
from .metadata import write_tags
from .model import AudioResult, Track
from .net import HttpClient
from .search.registry import build_sources
from .sniff import sniff_path
from .utils import sha256_file

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

MIN_ACCEPT = 0.34          # 低于此分不进入下载候选池
EARLY_STOP_SCORE = 0.95    # 已拿到高置信度原曲（歌名+歌手几乎完全一致）就不再打扰其它音源
MAX_DOWNLOADS = 6          # 单文件最多尝试的下载次数（按分数从高到低顺延）

__all__ = ["Pipeline", "JobResult"]


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
                 sources: "list | None" = None) -> None:
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
        self.sources = list(sources) if sources is not None else build_sources(self.http, settings.to_dict())
        self._cancel = False

    # ------------------------------------------------------------------ #
    def cancel(self) -> None:
        self._cancel = True

    def close(self) -> None:
        self.http.close()

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
            return self._fallback(p, digest, note=exc.message)
        except (DecryptionFailedError, UnsupportedFormatError) as exc:
            self._log(f"⚠ 离线解密失败：{name} — {exc.message}")
            if not self.settings.enable_fallback:
                return JobResult(str(p), name, "failed", message=exc.message)
            return self._fallback(p, digest, note=exc.message)

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

        adapters = {s.name: s for s in self.sources}
        pool: list[Track] = []          # 跨音源的"干净"候选池
        dropped_total = 0

        for source in self.sources:
            if self._cancel:
                raise CancelledError("已取消")
            try:
                tracks = source.search(query, quality=self.settings.quality, limit=8)
            except MusicDecryptorError as exc:
                self._log(f"  · [{source.label}] 搜索不可用：{exc.message}")
                continue
            tracks = [t for t in tracks if t.title]
            if not tracks:
                self._log(f"  · [{source.label}] 无结果")
                continue

            # 强制过滤：排除伴奏 / 纯器乐 / 现场 / 改编版（除非用户在文件名里点名要）
            if not requested:
                originals, dropped = split_originals(tracks)
                if dropped:
                    dropped_total += len(dropped)
                    sample = "、".join(f"“{t.title}”" for t, _m in dropped[:3])
                    self._log(f"  · [{source.label}] 已排除 {len(dropped)} 个伴奏/改编版本：{sample}")
                tracks = originals
                if not tracks:
                    self._log(f"  · [{source.label}] 无原曲（返回结果均为伴奏/改编版）")
                    continue

            rank_tracks_multi(readings, tracks, prefer_ext=self.settings.quality)
            best_t, best_a = reading_scores(readings, tracks[0],
                                            prefer_ext=self.settings.quality)[0][1]
            self._log(f"  · [{source.label}] {len(tracks)} 个原曲候选，最佳“{tracks[0].title}”"
                      f"({tracks[0].artist}) 匹配度 {tracks[0].score:.2f}"
                      f"（采用读法：歌名「{best_t}」/ 歌手「{best_a}」）")

            good = [t for t in tracks[:3] if t.score >= MIN_ACCEPT]
            pool.extend(good)
            if good and good[0].score >= EARLY_STOP_SCORE:
                self._log(f"  · 已获得高置信度原曲（{good[0].score:.2f}），跳过其余音源")
                break

        if not pool:
            if dropped_total and not requested:
                raise SearchNoResultError(
                    "未找到原曲，当前仅有伴奏/改编版，请确认是否需要下载。",
                    detail=f"已排除 {dropped_total} 个伴奏/改编版本")
            raise SearchNoResultError("搜索无结果，请检查网络或歌曲名")

        # 跨音源择优：不再"第一个能用的音源就算"，而是所有音源一起比，分高者先下
        pool.sort(key=lambda t: t.score, reverse=True)
        tried = 0
        for track in pool[:MAX_DOWNLOADS]:
            if self._cancel:
                raise CancelledError("已取消")
            adapter = adapters.get(track.source)
            if adapter is None:
                continue
            tried += 1
            try:
                track = adapter.resolve(track, quality=self.settings.quality)
            except MusicDecryptorError as exc:
                self._log(f"    - [{track.source}] 直链不可用：{exc.message}")
                continue
            try:
                data = self._download(track)
            except MusicDecryptorError as exc:
                self._log(f"    - [{track.source}] 下载失败：{exc.message}")
                continue
            v = validate(data, track.ext, expected_bitrate=track.bitrate,
                         duration=track.duration, min_bitrate=self.settings.min_bitrate)
            if not v.ok:
                self._log(f"    - [{track.source}] 校验未通过：{v.reason}")
                continue
            runner = next((t for t in pool if t is not track), None)
            if runner is not None and runner.score < track.score:
                self._log(f"  · 择优采用：{track.source} “{track.title}”({track.artist}) "
                          f"{track.score:.2f} 优于 次选 {runner.source} “{runner.title}” {runner.score:.2f}")
            return self._write_downloaded(src, track, data, v, digest)

        if tried == 0:
            raise SearchNoResultError("搜索无结果，请检查网络或歌曲名")
        raise SearchNoResultError(
            "候选音源均不可用（无版权/校验失败/反爬），请更换关键词或手动补全直链",
            detail=note)

    def _write_downloaded(self, src: Path, track: Track, data: bytes, v, digest: str) -> JobResult:
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
        self._log(f"✅ 备用下载完成：{src.name} → {out_path.name}  [{v.ext.upper()}{br}] 来源：{track.source}")
        return JobResult(str(src), src.name, "downloaded", mode="fallback", out_path=str(out_path),
                         fmt=v.ext, bitrate=v.bitrate, source=track.source, message="完成")

    # ------------------------------------------------------------------ #
    def _aggregator(self):
        for s in self.sources:
            if s.name == "aggregator":
                return s
        return None

    def _download(self, track: Track) -> bytes:
        url = track.url or ""
        if url.startswith("file:"):
            from urllib.parse import urlparse
            from urllib.request import url2pathname
            parsed = urlparse(url)
            raw = url2pathname(parsed.path or parsed.netloc or "")
            local = Path(raw) if raw else None
            if local is None or not local.is_file():
                raise NetworkError("本地文件不可读", detail=str(raw))
            return local.read_bytes()
        if not url:
            raise NetworkError("该音源未返回直链")
        return self.http.download(url, headers={"Referer": self._referer_for(track)})

    @staticmethod
    def _referer_for(track: Track) -> str:
        return {"kuwo": "https://www.kuwo.cn/", "kugou": "https://www.kugou.com/"}.get(track.source, "")

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

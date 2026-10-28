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

import inspect
import os
import re
import time
from contextlib import nullcontext
from itertools import chain, islice
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import resolve_config_dir
from .constants import QMC_TAG_MUSICEX
from . import ekeys
from .crypto.kwm import KwmDecoder
from .crypto.ncm import NcmDecoder
from .crypto.qmc import QmcDecoder
from .downloader import check_completeness, validate
from .duration import TOL_ABS, TOL_REL, audio_duration, consensus, format_seconds
from .errors import (CancelledError, DecryptionFailedError, MusicDecryptorError,
                     NeedsOnlineError, NetworkError, SearchNoResultError,
                     UnsupportedFormatError)
from .hashcache import HashCache
from . import fuzzy, jp
from .lang import is_chinese_likely
from .match import (ARTIST_UNKNOWN, artist_match_score, best_reading_match, clean_text, filename_query,
                    parse_song_filename_candidates, rank_tracks_multi, reading_scores,
                    looks_like_song_query, normalize_for_version, sanitize_query, sanitize_query_text,
                    space_split_readings, split_originals, version_markers,
                    album_version_markers, recording_version_ok, track_version_markers,
                    filter_title_alias_ambiguity)
from .metadata import read_source_meta, write_tags
from .model import AudioResult, Track
from .net import HttpClient
from .suggestions import query_suggestion_choices
from .search.fusion import (collapse_duplicates, dedupe_key,
                            mark_low_confidence)
from .search.index import ResourceIndex
from .search.query import MAX_PER_SOURCE as MAX_VARIANTS_PER_SOURCE
from .search.query import expand as expand_query
from .search.registry import build_sources
from .sniff import looks_like_audio, sniff_path
from . import transcode
from .utils import sha256_file

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

MIN_ACCEPT = 0.34          # 低于此分不进入下载候选池
MIN_TITLE_SIMILARITY = 0.60  # 歌名字段硬门槛：歌名不像就直接淘汰（"同歌手另一首"不许混进来）
MIN_ARTIST_SIMILARITY = 0.60  # 歌手字段硬门槛：同名翻唱/不同演唱者一律淘汰
MIN_TRACK_SECONDS = 45.0     # 实测时长下限：低于此秒数视为试听片段/前奏，不是完整原曲
EARLY_STOP_SCORE = 0.95    # 已拿到高置信度原曲（歌名+歌手几乎完全一致）就不再打扰其它音源
MAX_DOWNLOADS = 6          # 单文件最多尝试的下载次数（按分数从高到低顺延）


class _ExplicitArtistReadings(list):
    """Keep explicit UI field provenance without changing legacy tuple values."""

    def __init__(self, readings, artist):
        super().__init__(readings)
        self.required_artist = artist

#: 华语歌预匹配（mmusic/lang.py）：备用链路的动态排序与按需检索
def _add_reversed_reading(readings, name):
    """日语输入补一条反序读法（«歌手 歌名» 语序很常见）。只增不减、纯文本。"""
    if not jp.looks_japanese(name):
        return readings
    parts = [p for p in re.split(r"\s+", sanitize_query_text(name)) if p.strip()]
    if len(parts) < 2:
        return readings
    pair = (" ".join(parts[1:]), parts[0])
    if pair[0] and pair[1] and pair not in readings:
        return list(readings) + [pair]
    return readings


_CJK_FIRST_SOURCE = "migu"                      # 华语场景：提到联网源最前
_CJK_DEFER_SOURCES = frozenset({"wikimedia", "internet_archive"})  # …垫底，有候选时不检索
#: 目标无损（FLAC/WAV）时优先的源：实测 2026-10-20 —— **全链唯一能真给无损**的通道
#: （br=999 → 806kbps FLAC），而酷我/酷狗/咪咕在 flac 目标下都只给 128k mp3。
_LOSSLESS_FIRST_SOURCE = "aggregator"
#: 实测时长**偏长**的上限：超过 `max(参考×1.6, 参考+120s)` 视为合辑/串烧而非同一首
#: （实测：互联网档案馆给《致爱丽丝》的条目是 25:29 / 435MB 的合辑，会通过原先
#: "只判偏短"的逻辑被下载下来）。只作精选期硬判据，且**必须有参考时长**才生效。
TOL_OVER_ABS = 120.0
TOL_OVER_REL = 0.6
#: 「开放库例外」：CC/PD 库（维基共享 / 互联网档案馆）的文件常因容器特性测不出时长
#: （Ogg 要读尾页 granule、m4a 的 moov 在文件尾），按"只下载通过验证的资源"会被
#: 一律排除、等于白列。因此在**没有其它更可疑证据**时允许它们以
#: 「音频头合法 + 体积达标」一次性通过，并在结果里如实标注 `时长未比对（开放库文件）`。
_OPEN_LIBRARY_SOURCES = frozenset({"wikimedia", "internet_archive"})
MIN_OPEN_LIBRARY_BYTES = 1_000_000
#: 「末位来源」：实测（2026-10-19，30 首混合曲目）**没有任何一首是只有它才能拿到**，
#: 平均 7.1 请求/首（酷我/咪咕 2.0），且命中的多半是 DJ版/现场/网友改编这类准正版。
#: 因此它**照常检索**（保留兜底能力），但探测/下载排到最后，且**不为它预解析**直链；
#: 只有当其它来源都测不出可用候选时，才用独立额度回头补测它。
_LAST_RESORT_SOURCES = frozenset({"kugou"})
MAX_PROBES = 2             # 最多探测几个候选（静默的置信度操作，成本必须封顶）
MAX_LIST_CANDIDATES = 8    # 「先列候选」最多解析/展示几条（每条一次"取直链"请求，必须封顶）
MAX_PROBES_WIDE = 4        # 平台标注互相矛盾时多测几个：只测前 2 个可能全是缩短版
PROBE_TIMEOUT = 8.0        # 探测用更短超时：慢就跳过，绝不拖慢主流程
PROBE_BYTES = 256 * 1024   # 探测只取文件头，绝不整文件下载
PROBE_TAIL_BYTES = 128 * 1024  # ogg / 非 faststart 的 m4a 需要读文件尾
MAX_EXTRA_RESOLVES = 2     # 为"跳过疑似短资源"而多解析几个候选的上限（成本封顶）

__all__ = ["Pipeline", "JobResult", "default_search_dir", "SEARCH_PREFIX"]


def _needs_tail(head: bytes) -> bool:
    """True when the container keeps its length at the end of the file."""
    if len(head) < 12:
        return False
    if head[:4] == b"OggS":
        return True                      # length = last page's granule position
    if head[4:8] == b"ftyp":
        return b"moov" not in head[:256 * 1024]   # moov not faststarted -> at the end
    return False


_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)})


def _atomic_write(path: Path, data: bytes) -> None:
    """先写同目录临时文件、再 `os.replace` 原子替换。

    直接 `write_bytes` 到目标路径时，写到一半崩溃/断电会留下**半个文件**，
    而下次运行只会看到"同名冲突"再另存一份 —— 用户得到一个看起来正常、
    其实残缺的歌词文件。临时名以 `.` 开头且用 `.part` 结尾，便于识别与清理。
    """
    tmp = path.with_name(f".{path.name}.part")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _avg_bitrate(total: int, duration: "float | None") -> int:
    """整段文件的**平均**码率（kbps）。

    只认"体积 ÷ 实测时长"，不用首帧码率 —— VBR 的首帧可能是 32k，看着像低码率，
    实为误判。平台声明值已被实测证伪（酷我"声明 320 → 实际 128"，甚至 48k），
    所以日志/台账一律以这个实测值为准。
    """
    if not total or not duration or duration <= 0:
        return 0
    return int(round(total * 8.0 / duration / 1000.0))


def _over_tolerance(reference: "float | None") -> float:
    """“偏长”判据的容差；没有参考时长时返回 0（= 不判偏长，避免误杀本来就长的曲目）。"""
    if not reference:
        return 0.0
    return max(TOL_OVER_ABS, TOL_OVER_REL * reference)


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


def _recording_tags(data: bytes) -> dict:
    try:
        return _read_recording_tags(data)
    except (ValueError, UnicodeError, IndexError, OverflowError):
        return {}


def _read_recording_tags(data: bytes) -> dict:
    """Bounded, local text-tag read; no optional dependencies or cover decoding.

    The same parser accepts a probe prefix and a downloaded file. Incomplete
    frames/blocks are ignored, never guessed. Covers and audio frames are skipped.
    """
    data = memoryview(data)[:1024 * 1024]
    result = {}

    def text(key, payload, encoding="utf-8"):
        if len(payload) <= 16384:
            value = bytes(payload).decode(encoding, errors="replace").strip("\x00 \r\n")
            if value:
                # Multiple values must all participate in version checks.
                result[key] = value.replace("\x00", " ")

    def comments(block):
        if len(block) < 8:
            return
        pos = 4 + int.from_bytes(block[:4], "little")
        if pos + 4 > len(block):
            return
        count = min(int.from_bytes(block[pos:pos + 4], "little"), 256)
        pos += 4
        for _ in range(count):
            if pos + 4 > len(block):
                break
            size = int.from_bytes(block[pos:pos + 4], "little")
            pos += 4
            if pos + size > len(block):
                break
            if size <= 16384:
                entry = bytes(block[pos:pos + size])
                key, sep, value = entry.partition(b"=")
                if sep and key.lower() in (b"album", b"title"):
                    name = key.decode("ascii").lower()
                    previous = result.get(name, "")
                    text(name, value)
                    if previous:
                        result[name] = previous + " / " + result.get(name, "")
            pos += size

    if bytes(data[:3]) == b"ID3" and len(data) >= 10:
        version = data[3]
        if version not in (2, 3, 4) or any(x & 128 for x in data[6:10]):
            return result
        synchsafe = lambda b: sum(v << (7 * (len(b) - i - 1)) for i, v in enumerate(b))
        end = min(len(data), 10 + synchsafe(data[6:10]))
        body = data[10:end]
        if data[5] & 0x80:
            body = memoryview(bytes(body).replace(b"\xff\x00", b"\xff"))
        pos = 0
        if data[5] & 0x40:
            if version == 2 or len(body) < 4:
                return result
            pos = (synchsafe(body[:4]) if version == 4 else 4 + int.from_bytes(body[:4], "big"))
        header = 6 if version == 2 else 10
        for _ in range(256):
            if pos + header > len(body):
                break
            name = bytes(body[pos:pos + (3 if version == 2 else 4)])
            sizebytes = body[pos + (3 if version == 2 else 4):pos + header - (0 if version == 2 else 2)]
            size = synchsafe(sizebytes) if version == 4 else int.from_bytes(sizebytes, "big")
            if not size or pos + header + size > len(body):
                break
            payload = body[pos + header:pos + header + size]
            flags = body[pos + 9] if version != 2 else 0
            compressed = flags & (0x0d if version == 4 else 0xe0)
            if name in (b"TALB", b"TAL", b"TIT2", b"TT2") and not compressed and payload:
                enc = payload[0]
                if enc in (0, 1, 2, 3):
                    value = bytes(payload[1:])
                    if version == 4 and flags & 2:
                        value = value.replace(b"\xff\x00", b"\xff")
                    text("album" if name in (b"TALB", b"TAL") else "title", value,
                         ("latin-1", "utf-16", "utf-16-be", "utf-8")[enc])
            pos += header + size
    elif bytes(data[:4]) == b"fLaC":
        pos = 4
        for _ in range(128):
            if pos + 4 > len(data):
                break
            kind = data[pos]
            size = int.from_bytes(data[pos + 1:pos + 4], "big")
            pos += 4
            if pos + size > len(data):
                break
            if kind & 127 == 4:
                comments(data[pos:pos + size])
            pos += size
            if kind & 128:
                break
    elif bytes(data[:4]) == b"OggS":
        pos, packet, serial = 0, bytearray(), None
        for _ in range(128):
            if pos + 27 > len(data) or bytes(data[pos:pos + 4]) != b"OggS":
                break
            page_serial = bytes(data[pos + 14:pos + 18])
            if serial is None:
                serial = page_serial
            if page_serial != serial:
                break
            count = data[pos + 26]
            start = pos + 27 + count
            if start > len(data):
                break
            sizes = data[pos + 27:start]
            if start + sum(sizes) > len(data):
                break
            for size in sizes:
                packet.extend(data[start:start + size])
                start += size
                if size < 255:
                    if packet.startswith(b"\x03vorbis"):
                        comments(memoryview(packet)[7:])
                        return result
                    if packet.startswith(b"OpusTags"):
                        comments(memoryview(packet)[8:])
                        return result
                    packet = bytearray()
            pos = start
    return result


def _accepts_level(fn) -> "bool | None":
    """sink 的参数形态：``True`` 两参数 / ``False`` 单参数 / ``None`` 不可内省。

    旧调用方（`list.append`、`print`、CLI、绝大多数测试）只收一个参数，必须继续可用；
    UI 传两参数的 sink，好按 `level` 决定显不显示。

    为什么要三态：`inspect.signature` 对 **C 实现的可调用对象**会抛 ValueError ——
    PySide6 的 `SignalInstance.emit`（`logged(QString,QString)`）正是如此。把它误判成
    "单参数"就会调用 `emit(msg)` → `TypeError` → 被 `_guarded` 吞掉 →
    **整条日志链路静默失效**。实测事故（2026-10-20）：界面上只剩界面自己的 3 行，
    用户以为「显示详细信息」没作用。因此不可内省时返回 None，交给调用方"先试两参数"。
    """
    if fn is None:
        return False
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return None
    if any(x.kind == x.VAR_POSITIONAL for x in params):
        return True
    return len([x for x in params
                if x.kind in (x.POSITIONAL_ONLY, x.POSITIONAL_OR_KEYWORD)]) >= 2


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


#: 「以名找歌」的 ``JobResult.path`` 前缀：它不是真实文件路径，便于 UI / 日志区分
SEARCH_PREFIX = "search://"


#: 「群星 / 未知 / VA」这类**占位歌手**的关键词（空串不算 —— 那是"没有歌手信息"）
_PLACEHOLDER_HINTS = ("群星", "合辑", "未知")


def _placeholder_credit(track: Track) -> bool:
    """候选的歌手是不是"占位符"。

    背景（2026-10-21 实测）：``artist_match_score`` 对"候选没有歌手信息（空/未知/群星）"
    刻意返回 1.0（无法判定就不淘汰），于是「群星 - 夜曲」能过歌手硬门槛；再叠加参考时长
    "取最完整值"，一个 4:36 的合辑版本就把正确的 3:47 判成了"偏短"，最终下载了群星版 ——
    用户明确给了歌手却拿到别人的版本，明显不贴近预期。

    注意：**空串不算占位符**（CC/PD、本地曲库经常没有歌手信息，那些是"没有信息"而不是
    "写着别人"），所以这个判定只用来**给现场有明确歌手的候选让路**，绝不硬删。
    """
    name = (track.artist or "").strip().casefold()
    if not name:
        return False
    return name in ARTIST_UNKNOWN or any(hint in name for hint in _PLACEHOLDER_HINTS)


def default_search_dir() -> Path:
    """「以名找歌」在没有输出目录时的落点。

    以名找歌**没有源文件可依附**，而写入进程工作目录（双击 exe 时就是它自己的
    ``dist`` 目录）是历史事故；所以给一个固定、可预期、与音乐库同级的目录，
    并在界面上明示当前生效目录。
    """
    return Path.home() / "Music" / "MusicDecryptor"


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
    suggestions: tuple[tuple[str, str], ...] = ()  # failed-search identities, never download candidates

    @property
    def ok(self) -> bool:
        return self.status in ("decoded", "downloaded", "copied")


class Pipeline:
    @staticmethod
    def _bind_recording(track, requested):
        track.raw = dict(track.raw or {})
        track.raw["_requested_recording"] = sorted(requested)
        return track

    @staticmethod
    def _requested_recording(track):
        return set(track.raw.get("_requested_recording", track_version_markers(track)))

    def _guard_recording(self, track, requested, data=None):
        if data is not None:
            tags = _recording_tags(data)
            if tags:
                track.raw = dict(track.raw or {})
                track.raw["_recording_tags"] = tags
        if not recording_version_ok(track, requested):
            raise SearchNoResultError("录音版本不符：已排除现场/改编或其它非目标版本",
                                      detail=f"{track.source} “{track.title}” / {track.album}")

    def __init__(self, settings, *, cache_path: "str | Path | None" = None,
                 on_log: "Callable[[str], None] | None" = None,
                 on_progress: "Callable[[int, int], None] | None" = None,
                 on_file: "Callable[[JobResult], None] | None" = None,
                 sources: "list | None" = None,
                 index_path: "str | Path | None" = None,
                 http=None) -> None:
        self.settings = settings
        # 日志分级：`result` = 结论（同事必看，如"✅ 解密完成"/"❌ …"）；
        # `detail` = 过程明细（每个音源的排除统计、探测与降权说明…）。
        # UI 默认只渲染 result、勾选后展开 detail；CLI/测试仍拿到全部行。
        sink = on_log
        sink_levels = _accepts_level(sink)      # True / False / None（不可内省）

        def emit(message: str, level: str = "detail") -> None:
            if sink is None:
                return
            if sink_levels is True:
                sink(message, level)
            elif sink_levels is False:
                sink(message)
            else:
                # 签名不可知（Qt 信号 emit 等 C 可调用对象）：先试两参数，
                # 真不够再退一参数 —— 绝不因为"探测不准"把日志丢掉。
                try:
                    sink(message, level)
                except TypeError:
                    sink(message)

        self._log = _guarded(emit)
        self._progress = _guarded(on_progress)
        self._on_file = _guarded(on_file)
        self.http = http if http is not None else HttpClient(timeout=settings.timeout,
                                                             retries=settings.retries)
        if cache_path:
            cache_file = Path(cache_path)
        elif settings.output_dir:
            cache_file = Path(settings.output_dir) / ".mmusic-hashcache.json"
        else:
            # never drop the ledger into the cwd (for a frozen exe that is its
            # own install folder); keep it beside the settings instead
            cache_file = resolve_config_dir() / "hashcache.json"
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
        cancel_http = getattr(self.http, "cancel", None)
        if cancel_http is not None:
            cancel_http()

    def _check_cancelled(self) -> None:
        if self._cancel:
            raise CancelledError("已取消")

    def close(self) -> None:
        self.http.close()
        try:
            self.index.close()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _is_musicex_file(path: Path) -> bool:
        """便宜判断：文件尾部是否为 musicex 魔法（避免对 STag 等无谓尝试）。"""
        try:
            with open(path, "rb") as fh:
                fh.seek(-8, 2)
                return fh.read(8) == QMC_TAG_MUSICEX
        except OSError:
            return False

    def _out_dir_for(self, src: "str | Path | None" = None) -> Path:
        """Resolve where a produced file goes.

        An explicitly configured ``output_dir`` wins; otherwise the output is
        written **next to the source file** - exactly what the UI promises -
        never the process working directory (which for a double-clicked exe
        would be its own install folder, ``dist``).

        「以名找歌」没有源文件可依附（``src is None``）→ 落到
        :func:`default_search_dir`，同样绝不落到进程工作目录。
        """
        configured = (self.settings.output_dir or "").strip()
        if src is not None:
            target = Path(configured).expanduser() if configured else Path(src).resolve().parent
        else:
            # 「以名找歌」的落点，按需按优：**专用目录 → 主输出目录 → 默认目录**
            search_dir = (getattr(self.settings, "search_output_dir", "") or "").strip()
            if search_dir:
                target = Path(search_dir).expanduser()
                self._log(f"  · 输出目录（以名找歌专用）：{target}")
            elif configured:
                target = Path(configured).expanduser()
                self._log(f"  · 输出目录：跟随主输出目录 {target}")
            else:
                target = default_search_dir()
                self._log(f"  · 输出目录（默认）：{target}")
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
                self._log("⛔ 已取消", level="result")
                break
            try:
                res = self.process_file(path)
            except CancelledError:
                self._log("⛔ 已取消", level="result")
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
                self._log(f"⏭ 已处理过（哈希命中），跳过：{name}", level="result")
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
                musicex_file = self._is_musicex_file(p)
                if fmt.needs_online:
                    if musicex_file:
                        # 新版 musicex：按需静默拉一次 ekey（用户自带登录态，仅本机）
                        ok, why = ekeys.ensure_ekey(self.http, self.settings, p)
                        self._log(f"  · {why}")
                        if not ok:
                            raise NeedsOnlineError(f"{fmt.note}；{why}")
                    else:
                        raise NeedsOnlineError(fmt.note)
                self._log(f"🔧 离线解密 QMC：{name}")
                raw = p.read_bytes()
                try:
                    result = QmcDecoder(raw, fmt.ext, src_path=p).decode()
                except DecryptionFailedError:
                    # 本地 ekey 虽在“新鲜期”，却可能已经失效（换机器 / 被提前轮换）。
                    # 已配置凭证时静默重取一次再试（成本封顶：一次请求，短超时）；否则如实上报。
                    if not (musicex_file and ekeys.creds_configured(self.settings)):
                        raise
                    ok, why = ekeys.ensure_ekey(self.http, self.settings, p,
                                                force=True, timeout=8.0)
                    self._log(f"  · 本地 ekey 校验未通过，已尝试重新获取：{why}")
                    if not ok:
                        raise
                    result = QmcDecoder(raw, fmt.ext, src_path=p).decode()
                note = (result.meta or {}).get("note")
                if note:
                    self._log(f"  · {note}")
                mode = "offline"
            else:
                raise UnsupportedFormatError("无法识别的加密格式", detail=fmt.ext or "未知扩展名")
        except NeedsOnlineError as exc:
            self._log(f"🔒 需联网破解：{name} — {exc.message}", level="result")
            if not self.settings.enable_fallback:
                return JobResult(str(p), name, "failed", message=exc.message,
                                 detail="备用下载已禁用")
            return self._fallback_result(p, name, digest, note=exc.message)
        except (DecryptionFailedError, UnsupportedFormatError) as exc:
            self._log(f"⚠ 离线解密失败：{name} — {exc.message}", level="result")
            if not self.settings.enable_fallback:
                return JobResult(str(p), name, "failed", message=exc.message)
            return self._fallback_result(p, name, digest, note=exc.message)
        except OSError as exc:
            # 读取过程中文件被删掉/被独占/权限变化：如实报错，
            # 绝不把 OSError 冒到批处理之外（否则一个坏文件会打断整批）。
            self._log(f"❌ 读取失败：{name} — {exc}", level="result")
            return JobResult(str(p), name, "failed", message="无法读取文件", detail=str(exc))

        try:
            return self._write_decoded(p, name, result, mode, digest)
        except MusicDecryptorError as exc:
            self._log(f"❌ 写入失败：{name} — {exc}", level="result")
            return JobResult(str(p), name, "failed", message=exc.message, detail=exc.detail)
        except OSError as exc:
            self._log(f"❌ 写入失败：{name} — {exc}", level="result")
            return JobResult(str(p), name, "failed", message="写入输出文件失败", detail=str(exc))

    # ------------------------------------------------------------------ #
    def _final_format(self, audio: bytes, ext: str, name: str) -> "tuple[bytes, str, str]":
        """按「目标音质」决定最终产物格式；引擎缺失/失败时保留原格式并如实说明。"""
        quality = getattr(self.settings, "quality", "") or ""
        if not quality:
            return audio, ext, ""
        action, note = transcode.plan_effective(ext, quality)
        if action == "keep":
            return audio, ext, note
        try:
            new_audio, new_ext = transcode.convert_bytes(audio, ext, action)
            self._log(f"  · 目标音质 {quality.upper()}：{ext.upper()} → {new_ext.upper()} 转码完成")
            return new_audio, new_ext, note
        except transcode.EngineUnavailable as exc:  # noqa: BLE001
            return audio, ext, (f"{note}；" if note else "") + f"转码引擎不可用（{exc}），已保留原格式"
        except Exception as exc:  # noqa: BLE001 - 转码失败绝不丢文件
            return audio, ext, (f"{note}；" if note else "") + f"转码失败（{exc}），已保留原格式"

    def _write_decoded(self, src: Path, name: str, result: AudioResult, mode: str, digest: str) -> JobResult:
        out_dir = self._out_dir_for(src)
        # 离线解密结果不带标签时，回读**源文件自带**的 title/artist/album 与内嵌封面：
        # ① 不要把源里正确的标题覆盖成文件名；② 源里若有封面，转码后一并带过去。
        # 只解析头部（毫秒级）、只在本机做，失败即静默退回原行为。
        src_tags: dict = {}
        src_cover = b""
        if mode == "offline":
            try:
                src_tags, src_cover = read_source_meta(result.audio)
            except Exception:  # noqa: BLE001 - 标签永不阻断解密
                src_tags, src_cover = {}, b""
        title = result.title or src_tags.get("title") or src.stem
        artist = result.artist or src_tags.get("artist", "")
        album = result.album or src_tags.get("album", "")
        base = self._safe_name(artist, title)
        audio, out_ext, fmt_note = self._final_format(result.audio, result.ext, name)
        if fmt_note:
            # 转码"失败/引擎不可用"是用户必须知道的（产物不是他要的格式）→ 结论级；
            # 正常的"转码完成"只是过程说明 → 明细级。
            failed_note = ("失败" in fmt_note) or ("不可用" in fmt_note)
            self._log(f"  · {name}：{fmt_note}",
                      level="result" if failed_note else "detail")

        # an input that is *already* plain audio and would land on its own
        # filename needs no copy at all
        if (mode == "plain" and out_ext == result.ext and out_dir == src.resolve().parent
                and f"{base}.{out_ext}".lower() == src.name.lower()):
            self._log(f"⏭ 已是明文音频，无需处理：{src.name}", level="result")
            return JobResult(str(src), src.name, "skipped", mode="plain",
                             out_path=str(src), fmt=out_ext, message="已是明文，无需处理")

        out_path = self._unique(out_dir, base, out_ext)
        _atomic_write(out_path, audio)

        cover = result.cover or src_cover
        if not cover and self.settings.fetch_cover and result.cover_url:
            cover = self._fetch_cover(result.cover_url)
        if self.settings.write_metadata:
            write_tags(out_path, title=title, artist=artist, album=album, cover=cover)

        if digest and self.settings.remember_hashes:
            self.cache.add(digest, out_path=str(out_path), mode=mode, fmt=result.ext)
        if not self.settings.keep_original:
            try:
                src.unlink()
            except OSError:
                pass

        status = "copied" if mode == "plain" else "decoded"
        tag = "解密" if mode == "offline" else ("转码" if out_ext != result.ext else "复制")
        self._log(f"✅ {tag}完成：{name} → {out_path.name}  [{out_ext.upper()}]", level="result")
        return JobResult(str(src), name, status, mode=mode, out_path=str(out_path),
                         fmt=out_ext, message="完成")

    # ------------------------------------------------------------------ #
    def _want_lossless(self) -> bool:
        """用户是否明确要无损（FLAC / WAV 目标）。"""
        return (getattr(self.settings, "quality", "") or "").lower() in ("flac", "wav")

    def _search_order(self, chinese: bool) -> "list":
        """本次备用链路的信源顺序（静默、自动、零额外请求）。

        * **目标无损**：聚合通道提到联网源最前 —— 实测它是全链唯一能给真无损的源，
          否则前面的源只会给 128k mp3。
        * **华语**：离线/补链源保持最前；咪咕提到联网源最前；维基共享 / 互联网档案馆
          压到最后 —— 是否真正检索由 `_fallback` 决定（只有当此前没有任何通过
          门槛的候选时才去碰它们）。
        * 其它：原样返回用户配置顺序。
        """
        sources = list(self.sources)
        want_lossless = self._want_lossless()
        if not chinese and not want_lossless:
            return sources
        head = [s for s in sources
                if getattr(s, "offline", False) or getattr(s, "trusted_links", False)]
        online = [s for s in sources if s not in head]
        front: "list" = []
        if want_lossless:
            front += [s for s in online if s.name == _LOSSLESS_FIRST_SOURCE]
        if chinese:
            front += [s for s in online
                      if s.name == _CJK_FIRST_SOURCE and s not in front]
        defer = [s for s in online if chinese and s.name in _CJK_DEFER_SOURCES]
        middle = [s for s in online if s not in front and s not in defer]
        return head + front + middle + defer

    def _fallback(self, src: Path, digest: str, *, note: str) -> JobResult:
        """文件名模式：把文件名当查询词，走一遍「以名取资源」。"""
        readings, query, requested = self._seed_from_name(src.stem)
        self._log(f"🔎 触发备用搜索：{query}")
        consensus = self._nameless_artist(readings)
        pool = self._collect_pool(src.stem, readings, query, requested,
                                  consensus=consensus)
        return self._take_from_pool(pool, src=src, digest=digest, note=note,
                                    label=src.name, consensus=consensus)

    # ---------------- 以名找歌（不依赖任何输入文件）------------------- #
    @staticmethod
    @staticmethod
    def _nameless_artist(readings) -> bool:
        """所有读法里都没有歌手 → 没有可比对的歌手参照（用户只给了歌名）。"""
        return not any((a or "").strip() for _t, a in readings)

    @staticmethod
    def _dominant_artist(pool) -> "tuple[set, str, int, int]":
        """只给了歌名时的**静默共识**：候选里出现最多的演唱者。

        同一位歌手的多种写法（"周杰伦" / "周杰伦 Jay Chou" / "周杰伦、方文山"）按
        **包含关系**归并，免得被平台命名差异拆散。

        返回 ``(成员 id 集合, 展示名, 该组候选数, 有歌手的候选总数)``。
        """
        groups: "list[list]" = []          # [[归一化代表串, [Track, ...]], ...]
        for track in pool:
            name = (track.artist or "").strip()
            if not name:
                continue
            key = (clean_text(name) or name).casefold()
            for group in groups:
                rep = group[0]
                if key == rep or key in rep or rep in key:
                    group[1].append(track)
                    break
            else:
                groups.append([key, [track]])
        if not groups:
            return set(), "", 0, 0
        groups.sort(key=lambda g: len(g[1]), reverse=True)
        members = groups[0][1]
        raw = [t.artist.strip() for t in members]
        # 展示名必须**确定性**：票数并列时用 (最常见的 → 最短的 → 字典序) 兜底。
        # 曾经写成 max(set(raw), key=raw.count) —— 并列时依赖 set 的迭代顺序，
        # 而 Python 的字符串哈希每进程随机化，同一个输入会时对时错。
        display = sorted(set(raw), key=lambda nm: (-raw.count(nm), len(nm), nm))[0]
        return ({id(t) for t in members}, display, len(members),
                sum(len(g[1]) for g in groups))

    def _prefer_dominant_artist(self, pool) -> None:
        """只给了歌名时，把"候选里出现最多的演唱者"排到前面（静默、只排序）。

        **只调顺序，不删任何候选**：多数不一致时（例如三种写法各一个），它就等价于
        "分数最高的那个仍在前面"，不会凭空制造偏好；最终下载谁仍要过时长核验。
        """
        ids, display, n, total = self._dominant_artist(pool)
        if not ids:
            return
        pool.sort(key=lambda t: id(t) not in ids)
        self._log(f"  · 只给了歌名：按候选里最多的演唱者「{display}」优先"
                  f"（{n}/{total} 个候选，静默共识；不排除其它演唱者）")

    @staticmethod
    def _seed_from_name(seed: str) -> "tuple[list, str, set]":
        """把一个名字（文件名，或搜索框里的「歌手 - 歌名」）解析成
        ``(读法候选, 查询串, 点名版本标记)`` —— 与备用链路共用同一套解析。

        先做**序号/符号清洗**（:func:`match.sanitize_query_text`）：点播清单粘过来的
        ``1. 夜曲``／``2、发如雪`` 既污染发给音源的检索词、又过不了歌名 0.60 硬门槛
        （实测 ``1. 夜曲`` vs ``夜曲`` 只有 0.57）。清洗只动序号与装饰符号，
        **版本标记（Live / 伴奏…）原样保留**；清洗前的原读法作为额外读法保留 —— 只增不减。
        """
        cleaned, hint = sanitize_query(seed)
        readings = list(parse_song_filename_candidates(cleaned)) if cleaned else []
        if cleaned != (seed or "").strip():
            # 清洗前的原读法作为"安全网"保留（只增不减）—— 但有个漏口必须堵上：
            # 单段原文会给出**不含歌手**的读法（如「周杰伦·发如雪」→ ("周杰伦·发如雪","")），
            # 它能靠"候选标题里含歌手名"过标题门槛，而歌手门槛因为读法没歌手直接放行
            # （实测下到过「稳重的牧牛铃 - 周杰伦 - 发如雪」这种垃圾）。
            # 所以：清洗后**已经**有带歌手的读法时，就不再放回无歌手的宽松读法。
            cleaned_has_artist = any(a.strip() for _t, a in readings)
            for extra in parse_song_filename_candidates(seed):
                if extra in readings:
                    continue
                if cleaned_has_artist and not (extra[1] or "").strip():
                    continue
                readings.append(extra)
        if hint:
            # 「原唱：周杰伦」「歌手: 周杰伦」里抠出来的歌手 —— 补成读法，让歌手硬门槛有参照
            for title, _artist in list(readings) or [(cleaned or seed, "")]:
                if title and title != hint and (title, hint) not in readings:
                    readings.append((title, hint))
            # 既然已经知道歌手是谁，"无歌手"的读法就只是**更弱**的版本 → 去掉，
            # 否则别的演唱者（或同名翻唱）能顺着它绕过歌手硬门槛（实测 7 例诱饵误过）。
            with_artist = [r for r in readings if (r[1] or "").strip()]
            if with_artist:
                readings = with_artist
        if not readings:
            readings = [(cleaned or seed, "")]
        # 日语空格语序补充（文件名里没写「-」分隔符时的「歌手 歌名」写法）
        readings = _add_reversed_reading(readings, cleaned or seed)
        # 版本标记按**清洗后**的文本判定：`试听：夜曲` 里的「试听」是说明前缀，
        # 不该被当成"用户点名要试听版"（那会关掉伴奏/改编过滤）。
        return readings, (filename_query(cleaned) or cleaned or seed), \
            version_markers(cleaned or seed)

    @classmethod
    def _seed_for(cls, name: str, artist: str = "") -> "tuple[str, list, str, set]":
        """Explicit fields stay typed; ambiguous free text alone may be reversed."""
        name = (name or "").strip()
        artist = (artist or "").strip()
        seed = f"{artist} - {name}" if (artist and name) else (name or artist)
        if artist:
            # Never run a composed pair through the filename parser: its
            # reversed reading lets title=atarayo / artist=tayori pass a query
            # whose explicit artist is atarayo. Missing title stays missing,
            # so the normal title gate rejects artist-only download requests.
            title = sanitize_query_text(name) if name else ""
            performer = sanitize_query_text(artist)
            return seed, _ExplicitArtistReadings([(title, performer)], performer), " ".join(
                value for value in (title, performer) if value), version_markers(title)
        readings, query, requested = cls._seed_from_name(seed)
        if name and not artist:
            # 手动搜索框里「夜曲 周杰伦」这种空格写法很自然：补两种读法（只增不减，
            # 让歌手硬门槛有东西可比对，而不会把原本能过的匹配弄失败）。
            for extra in space_split_readings(sanitize_query_text(name)):
                if extra not in readings:
                    readings.append(extra)
            # 日语罗马音/假名输入常按「歌手 歌名」语序（yorushika tada kimi ni hare）：
            # 补一条反序读法，让"歌手目录型"检索拿回的候选有字段可比对。
            readings = _add_reversed_reading(readings, name)
        return seed, readings, query, requested

    def search_candidates(self, name: str, *, artist: str = "") -> "list[Track]":
        """**只检索、不下载**：返回已通过歌名/歌手硬门槛与版本过滤的候选（按匹配度排序）。

        给「以名找歌」的人工挑选用。硬门槛一个都不放松 —— 用户只在**已经过门槛**的
        候选里选，所以人工挑选不会削弱「歌名 + 歌手双重校验」这条红线。
        """
        return self.search_candidates_ex(name, artist=artist)[0]

    def search_candidates_ex(self, name: str, *, artist: str = "") -> "tuple[list[Track], float | None]":
        """同 :meth:`search_candidates`，另外给出**跨平台参考时长**。

        与"只检索"的差别：这里**会为每条候选解析一次直链**（一条一个请求，封顶
        :data:`MAX_LIST_CANDIDATES`），因为**列出来却下不了等于没列**（用户 2026-10-21 反馈：
        "酷狗该曲目无免费直链"的候选出现在可选列表里没有任何价值）。解析失败、直链形态就是
        试听/铃声、标注时长就低于下限的候选一律**剔除并在日志里写明原因**。

        参考时长只用平台标注算（``_plausible_max``），不探测音频、不下载音频；拿它去校验
        用户点选的那一条（见 :meth:`download_chosen`），这样"点名下载"不必再重跑一遍检索。
        """
        self._check_cancelled()
        seed, readings, query, requested = self._seed_for(name, artist)
        pool = self._collect_pool(seed, readings, query, requested,
                                  consensus=self._nameless_artist(readings))
        pool = [self._bind_recording(t, requested) for t in pool
                if recording_version_ok(t, requested)]
        listed: "list[Track]" = []
        dropped = 0
        for pos, track in enumerate(pool):
            self._check_cancelled()
            if len(listed) >= MAX_LIST_CANDIDATES:
                break
            if track.duration and track.duration < MIN_TRACK_SECONDS:
                dropped += 1
                self._log(f"  · 已剔除 {track.source} “{track.title}”：平台标注仅 "
                          f"{format_seconds(track.duration)}（疑似片段/铃声）")
                continue
            adapter = self._adapters.get(track.source)
            if adapter is None:
                dropped += 1
                continue
            try:
                if not track.url:
                    track = adapter.resolve(track, quality=self.settings.quality)
                pool[pos] = track
                self._bind_recording(track, requested)
                self._guard_recording(track, requested)
                if not track.url:
                    raise NetworkError("音源未返回直链")
            except CancelledError:
                raise
            except MusicDecryptorError as exc:
                dropped += 1
                self._log(f"  · 已剔除无法解析直链的候选：{track.source} “{track.title}”"
                          f"（{exc.message}）")
                continue
            if getattr(track, "short_asset", False):
                dropped += 1
                self._log(f"  · 已剔除疑似试听/铃声的候选：{track.source} “{track.title}”"
                          "（直链形态判定）")
                continue
            listed.append(track)
        self._log(f"  · 可下载候选 {len(listed)} 个（已逐条解析直链"
                  + (f"，剔除 {dropped} 个下不了/疑似片段的" if dropped else "") + "）")
        self._check_cancelled()
        evidence = [t for t in pool if recording_version_ok(t, requested)]
        strong = [t for t in evidence if t.duration and t.score >= EARLY_STOP_SCORE]
        reference = _plausible_max(t.duration for t in (strong or evidence))
        return listed, reference

    def acquire(self, name: str, *, artist: str = "", chosen: "Track | None" = None,
                reference: "float | None" = None, note: str = "") -> JobResult:
        """以名取资源：不依赖任何输入文件，走与备用链路**完全相同**的选源逻辑。

        ``chosen=None`` → 自动选最优（一键）；给定 ``chosen`` 时锁定该候选，
        仍要过时长核验与完整度门禁，失败如实报错（绝不偷偷换成别的版本）。
        """
        label = f"{artist} - {name}" if (artist and name) else (name or artist)
        if chosen is not None:
            # 用户点名的那一条：直接下，**不再检索**（见 download_chosen 的说明）
            _seed, _readings, _query, requested = self._seed_for(name, artist)
            self._bind_recording(chosen, requested)
            return self.download_chosen(chosen, reference=reference, label=label,
                                        note=note)
        seed, readings, query, requested = self._seed_for(name, artist)
        self._log(f"🔎 以名找歌：{query}"
                  + (f"（指定歌手：{artist}）" if artist else ""))
        consensus = self._nameless_artist(readings)
        pool = self._collect_pool(seed, readings, query, requested, consensus=consensus)
        return self._take_from_pool(pool, src=None, digest="", note=note, label=label,
                                    consensus=consensus)

    def download_chosen(self, track: Track, *, reference: "float | None" = None,
                        label: str = "", note: str = "", cover: bytes = b"",
                        track_no: int = 0, quiet_source: bool = False,
                        output_metadata: "tuple[str, str, str] | None" = None) -> JobResult:
        """下载**用户点名的那一条**：不再检索、不再重新比对跨平台时长。

        为什么这样设计（2026-10-21 用户反馈）：候选列表已经过全部硬门槛，用户点
        「下载选中」期望的就是"就下这条"。旧实现会**重新跑一遍检索**再按新算出来的参考
        时长筛一遍 —— 既多花一轮请求，又因为"没排上探测名额 → ``actual_duration`` 为空
        → 不在 verified 里"把用户看得见的候选判成"未通过核验"。

        现在的语义：解析直链 → 下载 → **用下载回来的真实文件**做校验与完整度门禁
        （参考时长由列表带过来；没有参考就退化成"最短时长 + 文件头/码率"兜底）。
        真的不合格（试听片段、坏文件）照样如实拒绝，只是不再拿"测不出时长"当理由。
        """
        display = label or f"{track.artist} - {track.title}".strip(" -")
        track, data, v = self._fetch_chosen(track, reference=reference)
        if output_metadata is not None:
            track.title, track.artist, track.album = output_metadata
        return self._write_downloaded(None, track, data, v, "", note=note, label=display,
                                      cover=cover, track_no=track_no,
                                      quiet_source=quiet_source)

    def _fetch_chosen(self, track: Track, *, reference: "float | None" = None):
        """Resolve, fetch and validate once; leave the output decision to the caller."""
        self._check_cancelled()
        requested = self._requested_recording(track)
        self._guard_recording(track, requested)
        adapter = self._adapters.get(track.source)
        if adapter is None:
            raise SearchNoResultError("该候选的音源当前不可用，请换一条",
                                      detail=track.source)
        self._log(f"⬇ 点名下载：{track.source} “{track.title}”（{track.artist}）")
        if not track.url:
            track = adapter.resolve(track, quality=self.settings.quality)
        self._bind_recording(track, requested)
        self._guard_recording(track, requested)
        try:
            data = self._download(track)
        except CancelledError:
            raise
        except MusicDecryptorError:
            # 列表里解析出来的直链可能已过期 → 重新解析一次（仍然**不重新检索**）
            self._log("  · 直链可能已过期，重新解析一次")
            track = adapter.resolve(track, quality=self.settings.quality)
            self._bind_recording(track, requested)
            self._guard_recording(track, requested)
            data = self._download(track)

        self._guard_recording(track, requested, data)
        v = validate(data, track.ext, expected_bitrate=track.bitrate,
                     duration=track.duration, min_bitrate=self.settings.min_bitrate)
        if not v.ok:
            raise SearchNoResultError(f"你选择的候选校验未通过：{v.reason}",
                                      detail=f"{track.source} “{track.title}”")
        tolerance = max(TOL_ABS, TOL_REL * reference) if reference else TOL_ABS
        ok_len, why_len, got_secs = check_completeness(
            data, reference=reference or 0.0, tolerance=tolerance,
            min_seconds=MIN_TRACK_SECONDS, measured=track.actual_duration or 0.0,
            over_tolerance=_over_tolerance(reference))
        if not ok_len:
            raise SearchNoResultError(f"你选择的候选未通过完整度门禁：{why_len}",
                                      detail=f"{track.source} “{track.title}”")
        if got_secs:
            track.actual_duration = track.actual_duration or got_secs
        return track, data, v

    #: 「专辑批量下载」重匹配纪律（用户 2026-10-23：稳定与速度优先）——
    #: 原生通道失败后只试这些通道（本地/补链最先，然后四个平台源）；
    #: CC/PD 库不参与（专辑场景命中率极低，失败路径不为它拖时间）。
    _ALBUM_REMATCH_SOURCES = ("local", "manual", "migu", "kugou", "kuwo", "aggregator")
    #: 重匹配最多真正尝试下载的候选数（原生通道之外）
    _ALBUM_REMATCH_ATTEMPTS = 2
    _ALBUM_LOSSLESS_ATTEMPTS = 2
    _ALBUM_LOSSLESS_RESOLVES = 6

    def download_album_track(self, track: Track, *, album: str = "", cover: bytes = b"",
                             track_no: int = 0, reference: "float | None" = None) -> JobResult:
        """专辑批量下载的单曲入口：原生通道 → 其它音源重匹配，**仅当全部失败才报错**。

        MP3 目标沿用原生通道先下、失败再重匹配。FLAC/WAV 目标则先解析原通道，
        若不是无损，在同曲候选中优先查无损（允许同一来源的其它专辑条目）；
        验证失败/预算耗尽后回到有损路径。版本/完整度门槛不放松；同专辑、
        同歌手且标注时长吻合时，歌名允许受限的单字差异。
        中间失败只写**明细**日志（避免提前宣告某来源不可用）；全部失败时抛出
        「所有来源均无法获取，建议自行获取」，由界面统一提示。
        """
        requested = version_markers(track.title) | album_version_markers(album or track.album)
        self._bind_recording(track, requested)
        track.raw["_target_album"] = album or track.album
        label = f"{track.artist} - {track.title}".strip(" -")
        target_metadata = (track.title, track.artist, album or track.album)
        ref = reference if reference is not None else (float(track.duration) if track.duration else None)
        problems: "list[str]" = []
        rematched: "list[Track]" = []
        lossless_tried: "set[int]" = set()
        prepared_lossy = None
        if self._want_lossless():
            # A successful native MP3 is not the best available result when the
            # user asked for FLAC/WAV. Resolve first; only download plausible
            # lossless candidates, with a strict bounded attempt count.
            self._log(f"  · 目标无损：为「{track.title}」优先查找同曲无损资源", level="detail")
            candidates = chain((track,), self._rematch_album_track(track, lossless_first=True))
            for candidate in islice(candidates, self._ALBUM_LOSSLESS_RESOLVES + 1):
                if self._cancel:
                    raise CancelledError("已取消")
                is_native = candidate is track
                if not is_native:
                    if (candidate.source == track.source and candidate.raw
                            and candidate.raw == track.raw):
                        continue
                adapter = self._adapters.get(candidate.source)
                if adapter is None:
                    continue
                try:
                    if not candidate.url:
                        candidate = adapter.resolve(candidate, quality=self.settings.quality)
                    self._bind_recording(candidate, requested)
                    self._guard_recording(candidate, requested)
                    if not candidate.url:
                        raise NetworkError("音源未返回直链")
                except CancelledError:
                    raise
                except MusicDecryptorError as exc:
                    problems.append(f"{candidate.source}: {exc.message}")
                    self._log(f"  · [{candidate.source}] 无损直链不可用：{exc.message}", level="detail")
                    continue
                except Exception as exc:  # noqa: BLE001 - one source cannot abort the album
                    problems.append(f"{candidate.source}: {str(exc)[:120]}")
                    self._log(f"  · [{candidate.source}] 无损解析异常：{str(exc)[:120]}", level="detail")
                    continue
                if not is_native:
                    rematched.append(candidate)
                if candidate.ext.lower() not in ("flac", "wav", "ape", "dff"):
                    continue
                if len(lossless_tried) >= self._ALBUM_LOSSLESS_ATTEMPTS:
                    continue
                lossless_tried.add(id(candidate))
                try:
                    fetched, data, validation = self._fetch_chosen(candidate, reference=ref)
                    if validation.ext.lower() not in ("flac", "wav", "ape", "dff"):
                        if prepared_lossy is None:
                            prepared_lossy = (fetched, data, validation)
                        self._log("  · 候选实际为有损格式，保留作回退，继续寻找无损", level="detail")
                        if len(lossless_tried) >= self._ALBUM_LOSSLESS_ATTEMPTS:
                            break
                        continue
                    fetched.title, fetched.artist, fetched.album = target_metadata
                    result = self._write_downloaded(
                        None, fetched, data, validation, "", label=label,
                        cover=cover, track_no=track_no, quiet_source=True)
                    if not is_native:
                        self._log(f"  · 已从其它专辑的同曲资源取得无损音频，元数据已统一为「{album}」",
                                  level="detail")
                    return result
                except CancelledError:
                    raise
                except MusicDecryptorError as exc:
                    problems.append(f"{candidate.source}: {exc.message}")
                    self._log(f"  · [{candidate.source}] 无损候选未通过：{exc.message}", level="detail")
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"{candidate.source}: {str(exc)[:120]}")
                    self._log(f"  · [{candidate.source}] 无损候选异常：{str(exc)[:120]}", level="detail")
                if len(lossless_tried) >= self._ALBUM_LOSSLESS_ATTEMPTS:
                    break
            self._log("  · 未取得可验证无损资源，按既有规则尝试有损回退", level="detail")
        try:
            if id(track) not in lossless_tried:
                return self.download_chosen(track, reference=ref, cover=cover, track_no=track_no,
                                            quiet_source=True, output_metadata=target_metadata)
        except CancelledError:
            raise
        except MusicDecryptorError as exc:
            problems.append(f"{track.source}: {exc.message}")
            self._log(f"  · 原通道未取到「{track.title}」：{exc.message}", level="detail")
        except Exception as exc:  # noqa: BLE001 - 一首失败不拖垮整批
            problems.append(f"{track.source}: {exc}")
            self._log(f"  · 原通道异常：{str(exc)[:120]}", level="detail")
        if prepared_lossy is not None:
            fetched, data, validation = prepared_lossy
            fetched.title, fetched.artist, fetched.album = target_metadata
            return self._write_downloaded(
                None, fetched, data, validation, "", label=label,
                cover=cover, track_no=track_no, quiet_source=True)
        tried = 0
        fallback = (chain(rematched, candidates) if self._want_lossless()
                    else self._rematch_album_track(track))
        seen_fallback: "set[tuple[str, str]]" = set()
        for cand in fallback:
            if tried >= self._ALBUM_REMATCH_ATTEMPTS:
                break
            if id(cand) in lossless_tried:
                continue
            if cand is track or (cand.source == track.source and cand.raw
                                 and cand.raw == track.raw):
                continue
            identity = (cand.source, str(cand.raw.get("id") or cand.raw.get("FileHash") or cand.title))
            if identity in seen_fallback:
                continue
            seen_fallback.add(identity)
            tried += 1
            # Preserve the source's version evidence until bytes are checked.
            self._bind_recording(cand, requested)
            try:
                res = self.download_chosen(cand, reference=ref, cover=cover, track_no=track_no,
                                           label=label, quiet_source=True,
                                           output_metadata=target_metadata)
                self._log(f"  · 已自动改用其它通道获取「{track.title}」（重匹配通过强门槛）",
                          level="detail")
                return res
            except CancelledError:
                raise
            except MusicDecryptorError as exc:
                problems.append(f"{cand.source}: {exc.message}")
                self._log(f"  · 重匹配候选未通过：{exc.message}", level="detail")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{cand.source}: {exc}")
                self._log(f"  · 重匹配候选异常：{str(exc)[:120]}", level="detail")
        raise SearchNoResultError("所有来源均无法获取，建议自行获取",
                                  detail="；".join(problems) or "无可用通道")

    def _rematch_album_track(self, track: Track, *, lossless_first: bool = False):
        """强门槛重匹配：普通逐源 1 条，无损目标逐源至多 2 条并允许原来源。

        歌名+歌手必须**同一读法**同时达标；独立的同专辑、同歌手与时长证据
        可支持受限的长歌名差异。版本过滤照常；日语输入追加一次读音变体检索
        （仅首轮无结果时，只改"搜什么"、不改门槛写法）。
        """
        native = track.source
        order = [n for n in self._ALBUM_REMATCH_SOURCES
                 if n in self._adapters and (n != native or lossless_first)]
        if lossless_first and _LOSSLESS_FIRST_SOURCE in order:
            order.remove(_LOSSLESS_FIRST_SOURCE)
            order.insert(0, _LOSSLESS_FIRST_SOURCE)
        readings = [(track.title, track.artist)]
        alt_readings: "list[tuple[str, str]]" = []
        try:
            for value, _reason in jp.search_variants(track.title)[:1]:
                alt_readings.append((value, track.artist))
            if not alt_readings:
                for value, _reason in jp.search_variants(track.artist)[:1]:
                    alt_readings.append((track.title, value))
        except Exception:  # noqa: BLE001
            alt_readings = []
        requested = self._requested_recording(track)
        for name in order:
            if self._cancel:
                raise CancelledError("已取消")
            adapter = self._adapters.get(name)
            if adapter is None:
                continue
            query = f"{track.title} {track.artist}".strip()
            try:
                tracks = adapter.search(query, quality=self.settings.quality, limit=8)
            except MusicDecryptorError as exc:
                self._log(f"  · [{name}] 重匹配检索不可用：{exc.message}", level="detail")
                continue
            except Exception as exc:  # noqa: BLE001
                self._log(f"  · [{name}] 重匹配检索异常：{str(exc)[:100]}", level="detail")
                continue
            context = dict(expected_album=track.raw.get("_target_album", track.album),
                           reference_duration=track.duration)
            best = self._pick_rematch_candidate(tracks, readings, alt_readings, requested, **context)
            if best is None and alt_readings:
                alt_query = " ".join(x for x in (alt_readings[0][0], alt_readings[0][1]) if x).strip()
                if alt_query and alt_query != query:
                    try:
                        tracks = adapter.search(alt_query, quality=self.settings.quality, limit=8)
                        best = self._pick_rematch_candidate(tracks, readings, alt_readings, requested, **context)
                        if best is not None:
                            self._log(f"  · [{name}] 命中于读音变体检索", level="detail")
                    except Exception:  # noqa: BLE001
                        pass
            if best is not None:
                if all(not best_reading_match(group, best,
                                              min_title=MIN_TITLE_SIMILARITY,
                                              min_artist=MIN_ARTIST_SIMILARITY,
                                              strict_title=not requested)[0]
                       for group in (readings, alt_readings) if group):
                    self._log("  · 歌名轻微差异已由歌手、专辑及标注时长共同支持；"
                              "下载后仍核验真实时长与完整度", level="detail")
                if lossless_first:
                    # The same provider may expose the lossless copy under a
                    # single/compilation rather than this album's resource id.
                    pool = self._rank_rematch_candidates(tracks, readings, alt_readings, requested, **context)
                    pool.sort(key=lambda t: (t.short_asset,
                              t.ext.lower() not in ("flac", "wav", "ape", "dff"), -t.score))
                    for candidate in pool[:2]:
                        yield self._bind_recording(candidate, requested)
                else:
                    yield self._bind_recording(best, requested)

    @staticmethod
    def _pick_rematch_candidate(tracks, readings, alt_readings, requested, **context) -> "Track | None":
        """重匹配候选的强门槛过滤 + 排序：取最佳（疑似短资源让位，绝不提前下载）。"""
        kept = Pipeline._rank_rematch_candidates(tracks, readings, alt_readings, requested, **context)
        return next((t for t in kept if not t.short_asset), kept[0] if kept else None)

    @staticmethod
    def _rank_rematch_candidates(tracks, readings, alt_readings, requested, *,
                                expected_album: str = "", reference_duration: float = 0) -> "list[Track]":
        """Use the existing song/artist/version gates before any metadata rewrite."""
        pool = [t for t in (tracks or []) if (t.title or t.url)]
        pool = [t for t in pool if recording_version_ok(t, requested)]
        kept: "list[Track]" = []
        supported: "list[Track]" = []
        for cand in pool:
            ok = best_reading_match(readings, cand,
                                    min_title=MIN_TITLE_SIMILARITY,
                                    min_artist=MIN_ARTIST_SIMILARITY,
                                    strict_title=not requested)[0]
            if not ok and alt_readings:
                ok = best_reading_match(alt_readings, cand,
                                        min_title=MIN_TITLE_SIMILARITY,
                                        min_artist=MIN_ARTIST_SIMILARITY,
                                        strict_title=not requested)[0]
            if ok:
                kept.append(cand)
            elif expected_album and not requested:
                # Never infer the target album from the candidate itself.
                if best_reading_match(readings, cand,
                                      min_title=MIN_TITLE_SIMILARITY,
                                      min_artist=MIN_ARTIST_SIMILARITY,
                                      expected_album=expected_album,
                                      reference_duration=reference_duration)[0]:
                    supported.append(cand)
        # Multiple distinct approximate titles are ambiguous; retain exact hits.
        if len({normalize_for_version(t.title) for t in supported}) <= 1:
            kept.extend(supported)
        if not kept:
            return []
        return rank_tracks_multi(readings, filter_title_alias_ambiguity(readings, kept), prefer_ext="")

    def acquire_many(self, items) -> "list[JobResult]":
        """批量以名找歌。``items`` 可以是 "歌名"、"歌手 - 歌名" 或 ``(歌名, 歌手)``。"""
        items = list(items)
        total = len(items)
        results: "list[JobResult]" = []
        for i, item in enumerate(items):
            if self._cancel:
                self._log("⛔ 已取消", level="result")
                break
            name, artist = item if isinstance(item, (tuple, list)) else (str(item), "")
            label = f"{artist} - {name}" if (artist and name) else (name or artist)
            # 筛选：清洗后没有歌名（只有序号/说明前缀，如「1.」「求：」「点播」）的行直接跳过，
            # 不白跑一轮音源检索；结果里如实标注，便于核对。
            usable, cleaned = looks_like_song_query(name)
            if not usable:
                res = JobResult(f"{SEARCH_PREFIX}{label}", label, "skipped",
                                message="已跳过（清洗后没有歌名，只剩序号/说明前缀）")
                results.append(res)
                self._on_file(res)
                self._progress(i + 1, total)
                continue
            try:
                res = self.acquire(name, artist=artist)
            except CancelledError:
                self._log("⛔ 已取消", level="result")
                break
            except MusicDecryptorError as exc:
                # 失败也要有可读日志（结果级）：之前的批量以名找歌是静默失败
                self._log(f"❌ {label}：{exc.message}", level="result")
                res = JobResult(f"{SEARCH_PREFIX}{label}", label, "failed",
                                message=exc.message, detail=exc.detail,
                                suggestions=getattr(exc, "suggestions", ()))
            except Exception as exc:  # noqa: BLE001 - 一条失败不拖垮整批
                self._log(f"❌ {label}：未预期的错误（{str(exc)[:120]}）", level="result")
                res = JobResult(f"{SEARCH_PREFIX}{label}", label, "failed",
                                message="未预期的错误", detail=str(exc))
            results.append(res)
            self._on_file(res)
            self._progress(i + 1, total)
        return results

    def _collect_pool(self, seed: str, readings, query: str, requested,
                      *, consensus: bool = False) -> "list[Track]":
        """检索 + 三道硬门槛 + 华语/无损排序 → 干净的候选池（按匹配度排序）。

        **不下载、不探测**；文件名模式与「以名找歌」共用这一个池子。"""
        # 华语歌预匹配（静默、自动）：决定本次备用链路的信源顺序，不打扰用户
        chinese = is_chinese_likely(seed, *[f"{t} {a}" for t, a in readings])
        search_order = self._search_order(chinese)
        if chinese and any(s.name == _CJK_FIRST_SOURCE or s.name in _CJK_DEFER_SOURCES
                           for s in search_order):
            self._log("  · 华语歌预匹配：咪咕优先；维基共享/互联网档案馆仅在无其他候选时检索")
        want_lossless = self._want_lossless()
        if want_lossless and any(s.name == _LOSSLESS_FIRST_SOURCE for s in search_order):
            self._log(f"  · 目标无损：{_LOSSLESS_FIRST_SOURCE} 优先"
                      "（实测全链唯一能给真无损的通道，其它源只会给 128k mp3）")
        if len(readings) > 1:
            self._log("  · 文件名有两种读法：" +
                      " / ".join(f"歌名「{t}」歌手「{a}」" for t, a in readings))

        # 文件名自带版本标记 -> 用户主动点名要该版本：不过滤，但必须警告
        requested = (requested if getattr(readings, "required_artist", "")
                     else version_markers(seed))
        if requested:
            self._log("  · ⚠ 文件名包含版本标记「" + "、".join(sorted(requested)) +
                      "」，本次将保留该版本；若你要的是原曲，请把文件名改干净后重试")

        adapters = self._adapters
        index = self.index
        pool: list[Track] = []          # 跨音源的"干净"候选池
        alias_observations: list[Track] = []
        suggestion_candidates: list[Track] = []
        query_counts: dict[str, int] = {}
        attempted: set[tuple[str, str]] = set()
        dropped_total = 0
        dropped_artist = 0

        # P3/P4：查询扩展 —— 同一首歌多试几种写法，冷门/名字怪异的曲子命中率明显更高。
        # 变体分两轮消费：首轮每源 ≤2 个；**只有全源首轮无果**时才启用第二轮（≤2 个，
        # 日语读音形与纠错候选都在这里垫后）。请求数上限仍是每源 2+2、失败路径才多花。
        variants = expand_query(query, readings)
        if len(variants) > 1:
            self._log("  · 检索变体：" + " / ".join(f"{v.reason}「{v.text}」" for v in variants))
        alt_readings = fuzzy.corrected_readings(readings)
        explicit_artist = getattr(readings, "required_artist", "")
        if explicit_artist:
            # Title typo recovery stays available. An explicitly supplied
            # performer cannot silently turn into another artist via typo edits.
            alt_readings = _ExplicitArtistReadings(
                [(t, a) for t, a in alt_readings if a == explicit_artist], explicit_artist)
        # 分层 sweep（2026-10-22 收紧）：首轮每源 ≤2 个；**仅当首轮全源零命中**才
        # 追加第二轮（再 ≤2 个）。失败路径每源封顶 4 个请求（原为 6），成功路径仍是
        # ≤2 个。变体表已按价值排序：日语读音形紧跟原样；纠错候选只在“疑似错字”时
        # 提前，干净查询不再掺入 —— 既省请求，又不牺牲「睛天→晴天」这类召回。
        sweeps = [(0, MAX_VARIANTS_PER_SOURCE)]
        if len(variants) > MAX_VARIANTS_PER_SOURCE:
            sweeps.append((MAX_VARIANTS_PER_SOURCE, MAX_VARIANTS_PER_SOURCE * 2))

        found_any = False   # 是否已有通过门槛的候选：华语场景下决定 CC/PD 源是否检索
        for sweep_idx, (start, stop) in enumerate(sweeps):
            if sweep_idx and found_any:
                break
            batch = variants[start:stop]
            if not batch:
                break
            if sweep_idx:
                self._log(f"  · 此前全部无果：启用第{'二三四'[min(sweep_idx, 2)]}轮变体重试 —— "
                          + " / ".join(f"{v.reason}「{v.text}」" for v in batch))
            for source in search_order:
                if self._cancel:
                    raise CancelledError("已取消")
                if chinese and source.name in _CJK_DEFER_SOURCES and found_any:
                    self._log(f"  · [{source.label}] 已跳过（华语歌：已有其他候选，"
                              "避免在低命中源上耗时）")
                    continue
                kept: "list[Track]" = []
                for variant in batch:
                    query_counts[source.name] = query_counts.get(source.name, 0) + 1
                    attempted.add((source.name, variant.text))
                    kept, off_title_n, off_artist_n, dropped_ver = self._search_and_gate(
                        source, variant, readings, requested, alt_readings=alt_readings,
                        suggestion_candidates=suggestion_candidates)
                    dropped_total += dropped_ver
                    dropped_artist += off_artist_n
                    if kept:
                        if variant is not variants[0]:
                            self._log(f"  · [{source.label}] ✅ 命中于检索变体「{variant.reason}」")
                        break
                if not kept:
                    continue
                tracks = kept
                alias_observations.extend(kept)
                found_any = True

                tracks = rank_tracks_multi(readings, tracks, prefer_ext=self.settings.quality)
                best_t, best_a = reading_scores(readings, tracks[0],
                                                prefer_ext=self.settings.quality)[0][1]
                self._log(f"  · [{source.label}] {len(tracks)} 个原曲候选，最佳“{tracks[0].title}”"
                          f"({tracks[0].artist}) 匹配度 {tracks[0].score:.2f}"
                          f"（采用读法：歌名「{best_t}」/ 歌手「{best_a}」）")
                # 人工补链是用户的显式选择：连"匹配分门槛"也一并跳过
                # （文件名/标题当然不会与查询相似，但下载校验照旧）。
                threshold = 0.0 if getattr(source, "trusted_links", False) else MIN_ACCEPT
                pool.extend(t for t in tracks[:3] if t.score >= threshold)

                if getattr(self.settings, "fast_mode", False):
                    # 极速模式（用户显式开启）：命中即收工，跳过其它音源的多源交叉核验。
                    # 门槛/时长下限/完整度门禁/下载校验全部照常 —— 只省"多源印证"。
                    self._log(f"  · ⚡ 极速模式：已在 [{source.label}] 命中，跳过其余音源"
                              "（不做跨平台时长核验；可在 设置→行为 关闭）")
                    break

        unambiguous_ids = {id(t) for t in filter_title_alias_ambiguity(readings, alias_observations)}
        pool = [t for t in pool if id(t) in unambiguous_ids]
        if not pool:
            # Retrieval can return no close title at all for one extra CJK
            # character. A tiny failure-only metadata rescue can expose an
            # attested name for suggestions, never for acceptance/download.
            choices = query_suggestion_choices(readings, suggestion_candidates, requested=requested)
            rescue = fuzzy.suggestion_queries(readings) if not choices else []
            rescue_budget = 2 if rescue else 0
            for source in search_order:
                if rescue_budget <= 0:
                    break
                if (source.name not in {"migu", "kugou", "kuwo", "aggregator"}
                        or query_counts.get(source.name, 0) >= MAX_VARIANTS_PER_SOURCE * 2):
                    continue
                text = next((q for q in rescue if (source.name, q) not in attempted), "")
                if not text:
                    continue
                self._check_cancelled()
                rescue_budget -= 1
                self._log(f"  · [{source.label}] 仅为检索建议补查「{text}」（不下载）")
                try:
                    client = getattr(source, "http", None)
                    budget_context = (client.request_limits(timeout=3.0, retries=0)
                                      if isinstance(client, HttpClient) else nullcontext())
                    with budget_context:
                        observed = source.search(text, quality=self.settings.quality, limit=8)
                except CancelledError:
                    raise
                except MusicDecryptorError:
                    continue
                except Exception as exc:  # Optional metadata must never turn failure into a crash.
                    self._log(f"  · [{source.label}] 建议补查已跳过（{type(exc).__name__}）")
                    continue
                suggestion_candidates.extend(observed[:max(0, 256 - len(suggestion_candidates))])
            self._check_cancelled()
            choices = query_suggestion_choices(readings, suggestion_candidates, requested=requested)
            labels = [f"{title}（{artist}）" for title, artist in choices]
            suffix = ("\n你可能想找的歌是：" + "；".join(labels) + "？"
                      "\n以上仅为检索建议，尚未通过下载校验。" if choices else "")
            if dropped_artist:
                raise SearchNoResultError(
                    "未找到符合要求的歌曲，歌手不符的候选已排除。" + suffix,
                    detail=f"已排除 {dropped_artist} 个歌手不符的候选", suggestions=choices)
            if dropped_total and not requested:
                raise SearchNoResultError(
                    "未找到符合要求的歌曲，伴奏、现场或其他非目标版本已排除。" + suffix,
                    detail=f"已排除 {dropped_total} 个非目标录音版本", suggestions=choices)
            raise SearchNoResultError("搜索无结果，请检查网络或歌曲名" + suffix,
                                      suggestions=choices)

        # 跨音源择优：不再"第一个能用的音源就算"，而是所有音源一起比，分高者先下
        pool = rank_tracks_multi(readings, pool, prefer_ext=self.settings.quality)
        if consensus:
            self._prefer_dominant_artist(pool)
        return [self._bind_recording(t, requested) for t in pool
                if recording_version_ok(t, requested)]

    def _take_from_pool(self, pool, *, src, digest, note, label,
                        consensus: bool = False) -> JobResult:
        """候选池 → 跨平台时长核验 → 下载 → 校验/完整度门禁 → 落盘（两种模式共用）。"""
        adapters = self._adapters
        index = self.index
        requested = set(next((t.raw["_requested_recording"] for t in pool
                              if "_requested_recording" in t.raw), ()))
        pool = [self._bind_recording(t, requested) for t in pool
                if recording_version_ok(t, requested)]
        if not pool:
            raise SearchNoResultError("没有符合目标录音版本的候选，现场/改编版已排除")
        consensus_ids = self._dominant_artist(pool)[0] if consensus else set()
        # 查询里**给了歌手**时，"群星/未知"这类占位候选不算可信证据：既不拿它当参考时长，
        # 也让位给"有明确歌手"的候选。一个明确歌手的候选都没有时才原样使用（不硬删）。
        placeholder_ids = ({id(t) for t in pool if _placeholder_credit(t)}
                           if not consensus else set())

        def _credited(tracks) -> "list[Track]":
            """丢掉占位歌手候选（仅当查询给了歌手、且还有其它候选可依据时）。"""
            items = list(tracks)
            if not placeholder_ids:
                return items
            kept = [t for t in items if id(t) not in placeholder_ids]
            return kept or items
        # 注：入参 `note`（文件名模式传进来的“需联网”原因）在此处被下面的
        # 时长核验结论覆盖 —— 这是**历史行为**，本次重构原样保留，
        # 以免改动既有结果文案。


        # ---- 时长核验（静默、跨平台）-------------------------------------- #
        # 参考时长绝不能只信"正在提供文件的这个平台"：实测同一首歌在不同平台的标注
        # 可能一个是被截短的（周杰伦《最长的电影》：酷我 2:22 / 酷狗 3:55，后者才对）。
        # 因此跨平台取"最完整且不离群"的标注作为参考，全程自动、不打扰用户。
        # Probe before the length gate: a long mislabelled Live file must not
        # eliminate the studio candidate before its embedded tags are inspected.
        candidates = list(pool)

        # 实测交叉验证：只取文件头（最多 MAX_PROBES 个、超时 8s、失败即跳过）
        self._log(f"  · 开始时长核验（探测前 {min(len(candidates), MAX_PROBES)} 个候选）")
        self._probe_durations(candidates)
        candidates = [t for t in candidates if recording_version_ok(t, requested)]
        if not candidates:
            raise SearchNoResultError("没有符合目标录音版本的候选，音频标签表明为其它版本")
        pool = candidates
        strong = [t for t in pool if t.duration and t.score >= EARLY_STOP_SCORE]
        declared = [t.duration for t in _credited(strong or [t for t in pool if t.duration])]
        reference = _plausible_max(declared)
        tolerance = max(TOL_ABS, TOL_REL * reference) if reference else TOL_ABS
        if reference:
            self._log(f"  · 跨平台参考时长 {format_seconds(reference)}"
                      f"（{len(declared)} 个同版本平台标注中取最完整值，容差 ±{int(tolerance)}s）")
        short_meta = [t for t in pool
                      if reference and t.duration and t.duration < reference - tolerance]
        for track in short_meta[:3]:
            self._log(f"    ✗ 标注时长偏短已排除：{track.source} “{track.title}” "
                      f"{format_seconds(track.duration)}（参考 {format_seconds(reference)}）")
        candidates = [t for t in pool if t not in short_meta]
        short_n = sum(1 for t in candidates if t.short_asset)
        last_n = sum(1 for t in candidates
                     if t.source in _LAST_RESORT_SOURCES and not t.short_asset)
        if short_n or last_n:
            bits = []
            if short_n:
                bits.append(f"{short_n} 个疑似短资源（直链形态判定）")
            if last_n:
                bits.append(f"{last_n} 个末位来源候选（{'、'.join(sorted(_LAST_RESORT_SOURCES))}）")
            self._log("  · 已降权 " + " + ".join(bits)
                      + "：先测其它来源/正片，仍未测到可用候选才回头补测；"
                        "全程只降权、不做硬性排除")
        measured = [t.actual_duration
                    for t in _credited([t for t in candidates if t.actual_duration])]
        if not declared and measured:
            # No compatible platform annotation: use measured evidence only.
            measured_ref = _plausible_max(measured)
            if measured_ref:
                reference = measured_ref
                tolerance = max(TOL_ABS, TOL_REL * reference)
                self._log(f"  · 参考时长 {format_seconds(reference)}"
                          f"（取自 {len(measured)} 个候选的**实测**值，容差 ±{int(tolerance)}s）")
        elif measured:
            self._log(f"  · 参考时长 {format_seconds(reference)}"
                      "（同版本平台标注；实测仅核验，不以最长文件覆盖参考）")
        tolerance = max(TOL_ABS, TOL_REL * reference) if reference else TOL_ABS
        over_tolerance = _over_tolerance(reference)

        too_long = [t for t in candidates
                    if over_tolerance and t.actual_duration
                    and t.actual_duration > reference + over_tolerance]
        for track in too_long[:3]:
            self._log(f"    ✗ 时长异常偏长已排除：{track.source} “{track.title}” "
                      f"实测 {format_seconds(track.actual_duration)}"
                      f"（参考 {format_seconds(reference)}，疑似合辑/串烧）")

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

        over_ids = {id(t) for t in too_long}
        verified = [t for t in candidates
                    if id(t) not in over_ids
                    and t.actual_duration and t.actual_duration >= MIN_TRACK_SECONDS
                    and (not reference or t.actual_duration >= reference - tolerance)]
        # 排序决定**最终下载哪一个**：默认匹配度第一，短资源/末位来源只作次序；
        # 但用户明确要无损时，把"唯一能给真无损"的源排到最前 —— 否则前面的源
        # 只会给 128k mp3，等于把"选 FLAC"变成空承诺。两者都只在该候选**已通过
        # 全部硬门槛与时长核验**之后再排，不影响正确性。
        # 只给了歌名时，"多数演唱者"先于其它演唱者（仍然全部要过时长核验）
        if self._want_lossless():
            verified.sort(key=lambda t: (id(t) in placeholder_ids,
                                         id(t) not in consensus_ids,
                                         t.source != _LOSSLESS_FIRST_SOURCE, -t.score,
                                         t.short_asset,
                                         t.source in _LAST_RESORT_SOURCES))
        else:
            verified.sort(key=lambda t: (id(t) in placeholder_ids,
                                         id(t) not in consensus_ids, -t.score,
                                         t.short_asset,
                                         t.source in _LAST_RESORT_SOURCES))
        unverified = [t for t in candidates if not t.actual_duration]
        # 「开放库例外」：只在**没有任何"偏短/偏长/短资源"证据**时才启用
        suspicious = bool(shortened or too_short or short_meta or too_long)
        trusted_unverified = [] if suspicious else [
            t for t in unverified
            if t.source in _OPEN_LIBRARY_SOURCES and t.header_ok
            and t.size >= MIN_OPEN_LIBRARY_BYTES]

        note = ""
        if not verified and trusted_unverified:
            note = "时长未比对（开放库文件）"
            self._log(f"  · CC/PD 开放库例外：{len(trusted_unverified)} 个候选音频头合法、"
                      "体积达标但容器测不出时长，按完整文件处理（结果里如实标注）")
        elif not verified:
            if shortened or too_short or short_meta or too_long:
                note = "时长存疑"
                self._log("  · ⚠ 部分候选时长与参考值不符，已按可疑资源处理")
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
        known_bad = {id(t) for t in shortened} | {id(t) for t in too_short} | over_ids
        if reference is not None:
            order = verified or trusted_unverified
            if not order:
                excluded = len(short_meta) + len(known_bad)
                if excluded:
                    raise SearchNoResultError(
                        "候选均未通过时长或完整度校验，未下载任何文件。",
                        detail=f"已排除 {excluded} 个时长不符的候选（偏短/偏长）")
                raise SearchNoResultError(
                    "候选音源均不可用（无版权/校验失败/反爬），请更换关键词或手动补全直链",
                    detail="已有参考时长，但候选都没能测出音频时长")
        else:
            # 连参考时长都取不到：如实标注"未比对"后再兜底，不让用户白等
            order = (verified or trusted_unverified
                     or [t for t in candidates if id(t) not in known_bad])

        # ---- 内容去重：同一首歌经多个源命中时只保留排名最高的一个 ---------- #
        order, duplicates = collapse_duplicates(order)
        if duplicates:
            for dup, keeper in duplicates[:3]:
                self._log(f"    ✗ 重复资源已合并：{dup.source} “{dup.title}” 与 "
                          f"{keeper.source} “{keeper.title}” 同曲，保留 {keeper.source}")
            self._log(f"  · 去重后候选 {len(order)} 个（合并 {len(duplicates)} 个重复）")
            # A keeper can reveal a hidden Live tag only after downloading.
            # Keep other URLs as delayed fallbacks, within the same attempt cap.
            order.extend(duplicate for duplicate, _keeper in duplicates)
        flagged = mark_low_confidence(order)
        if flagged and flagged == len(order):
            self._log("  · ⚠ 所有候选匹配度都偏低，结果可能不准确")

        # 「以名找歌」的人工挑选：只在**已经过全部硬门槛与时长核验**的候选里锁定，
        # 门槛一个都不放松；用户点名的那条若没通过核验，就如实报错，不偷偷换别的版本。
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
                    self._bind_recording(track, requested)
                    self._guard_recording(track, requested)
                except MusicDecryptorError as exc:
                    self._log(f"    - [{track.source}] 直链不可用：{exc.message}")
                    _remember(track, ok=False)
                    continue
            try:
                data = self._download(track)
                self._guard_recording(track, requested, data)
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
                min_seconds=MIN_TRACK_SECONDS, measured=track.actual_duration or 0.0,
                over_tolerance=over_tolerance)
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
            return self._write_downloaded(src, track, data, v, digest, note=note, label=label)

        raise SearchNoResultError(
            "候选音源均不可用（无版权/校验失败/反爬），请更换关键词或手动补全直链",
            detail=note)
    def _write_downloaded(self, src: "Path | None", track: Track, data: bytes, v,
                          digest: str, *, note: str = "", label: str = "",
                          cover: bytes = b"", track_no: int = 0,
                          quiet_source: bool = False) -> JobResult:
        """落盘。``src=None`` 即「以名找歌」：没有源文件可依附，也不删任何东西。"""
        self._check_cancelled()
        display = label or (src.name if src is not None else "")
        data, out_ext, fmt_note = self._final_format(data, v.ext, display)
        self._check_cancelled()
        note = "；".join(x for x in (note, fmt_note) if x)
        out_dir = self._out_dir_for(src)
        base = self._safe_name(track.artist,
                               track.title or (src.stem if src is not None else display))
        out_path = self._unique(out_dir, base, out_ext)
        _atomic_write(out_path, data)

        cover_bytes = cover
        if not cover_bytes and self.settings.fetch_cover:
            pic_id = track.raw.get("pic_id")
            agg = self._aggregator()
            if pic_id and agg is not None and hasattr(agg, "fetch_pic"):
                pic_url = agg.fetch_pic(str(pic_id))
                if pic_url:
                    cover_bytes = self._fetch_cover(pic_url)
        if self.settings.write_metadata:
            write_tags(out_path, title=track.title, artist=track.artist,
                       album=track.album, cover=cover_bytes, track_no=track_no)

        if digest and self.settings.remember_hashes:
            self.cache.add(digest, out_path=str(out_path), mode="fallback", fmt=out_ext, source=track.source)
        if src is not None and not self.settings.keep_original:
            try:
                src.unlink()
            except OSError:
                pass

        br = f"，{v.bitrate}kbps" if (v.bitrate and out_ext == v.ext) else ""
        dur = f"，时长 {format_seconds(track.actual_duration)}" if track.actual_duration else ""
        tag = "备用下载" if src is not None else "以名找歌"
        self._log(f"✅ {tag}完成：{display} → {out_path.name}  [{out_ext.upper()}{br}{dur}]"
                  + ("" if quiet_source else f" 来源：{track.source}")
                  + (f"  ⚠ {note}" if note else ""), level="result")
        if quiet_source:
            self._log(f"  · 来源：{track.source}", level="detail")
        return JobResult(str(src) if src is not None else f"{SEARCH_PREFIX}{display}",
                         display, "downloaded", mode="fallback", out_path=str(out_path),
                         fmt=out_ext, bitrate=v.bitrate, source=track.source,
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

    def _probe_audio(self, track: Track) -> "tuple[float | None, int]":
        """A resolved candidate's real length (s) and average bitrate (kbps).

        Header-only probe (never downloads the whole file).  The measured
        bitrate is what goes into the log / ledger, because platform-declared
        values proved unreliable (Kuwo declared 320 but served 128, and 0 for
        some entries)."""
        url = track.url or ""
        if not url:
            raise NetworkError("尚未解析出直链")
        local = self._local_path(url)
        if local is not None:
            if not local.is_file():
                raise NetworkError("本地文件不可读", detail=str(local))
            size = local.stat().st_size
            track.size = track.size or size
            with open(local, "rb") as handle:
                head = handle.read(PROBE_BYTES)
            track.header_ok = bool(looks_like_audio(head))
            self._guard_recording(track, self._requested_recording(track), head)
            duration = audio_duration(head, size)
            if duration is None and _needs_tail(head) and size > PROBE_BYTES:
                with open(local, "rb") as handle:
                    handle.seek(max(0, size - PROBE_TAIL_BYTES))
                    tail = handle.read(PROBE_TAIL_BYTES)
                duration = audio_duration(head, size, tail)
            return duration, _avg_bitrate(size, duration)
        headers = {"Referer": self._referer_for(track)}
        head, total = self.http.probe(url, limit=PROBE_BYTES, timeout=PROBE_TIMEOUT,
                                      headers=headers)
        track.size = track.size or total
        track.header_ok = bool(looks_like_audio(head))
        self._guard_recording(track, self._requested_recording(track), head)
        duration = audio_duration(head, total)
        if duration is None and _needs_tail(head):
            try:
                tail, total2 = self.http.probe_tail(url, limit=PROBE_TAIL_BYTES,
                                                    timeout=PROBE_TIMEOUT, headers=headers)
                duration = audio_duration(head, total2 or total, tail)
                total = total2 or total
            except MusicDecryptorError:
                pass
        return duration, _avg_bitrate(total, duration)

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
            self._log(f"❌ {name}：{exc}", level="result")
            return JobResult(str(src), name, "failed", message=exc.message, detail=exc.detail,
                             suggestions=getattr(exc, "suggestions", ()))

    def _search_and_gate(self, source, variant, readings, requested, alt_readings=(),
                         suggestion_candidates=None):
        """检索一个源的一个查询变体，并跑完三道门槛。

        返回 ``(可用候选, 歌名淘汰数, 歌手淘汰数, 版本淘汰数)``；
        出错 / 无结果 / 全被淘汰时返回空列表，由调用方决定是否换下一个变体。

        ``alt_readings``：模糊输入纠错后的「备用读法」。只对原读法**没通过**的
        候选再判一次（不放松任何门槛写法），静默、低成本。
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
        if suggestion_candidates is not None and len(suggestion_candidates) < 256:
            suggestion_candidates.extend(tracks[:256 - len(suggestion_candidates)])
        if not tracks:
            self._log(f"  · [{source.label}] 无结果")
            return [], 0, 0, 0

        # 人工补链：用户显式给的链接，跳过歌名/歌手门槛（下载仍会做完整校验）
        if getattr(source, "trusted_links", False):
            self._log(f"  · [{source.label}] 使用你提供的链接（已跳过歌名/歌手门槛）")
            kept = [self._bind_recording(t, requested) for t in tracks
                    if recording_version_ok(t, requested)]
            return kept, 0, 0, len(tracks) - len(kept)

        dropped_ver = 0
        # 强制过滤：排除伴奏 / 纯器乐 / 现场 / 改编版（除非用户在文件名里点名要）
        if tracks:
            originals = [t for t in tracks if recording_version_ok(t, requested)]
            dropped = [(t, track_version_markers(t)) for t in tracks if t not in originals]
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
        fuzzy_hits = 0
        required_artist = getattr(readings, "required_artist", "")
        for cand in tracks:
            ok, _reading, ts, as_ = best_reading_match(
                readings, cand, min_title=MIN_TITLE_SIMILARITY,
                min_artist=MIN_ARTIST_SIMILARITY,
                strict_title=not requested)
            if not ok and alt_readings:
                # 模糊输入的第二机会：只改「搜什么」（纠错读法），不改门槛写法
                ok2, _r2, ts2, as2 = best_reading_match(
                    alt_readings, cand, min_title=MIN_TITLE_SIMILARITY,
                    min_artist=MIN_ARTIST_SIMILARITY,
                    strict_title=not requested)
                if ok2:
                    ok, ts, as_ = ok2, ts2, as2
                    fuzzy_hits += 1
            # Enforce the original explicit artist independently of every
            # alternate/typo reading. Do this after title scoring so unrelated
            # titles still receive the correct no-result classification.
            if required_artist and (
                    normalize_for_version(cand.artist) in ARTIST_UNKNOWN
                    or artist_match_score([("", required_artist)], cand) < MIN_ARTIST_SIMILARITY):
                ok, as_ = False, 0.0
            if ok:
                kept.append(self._bind_recording(cand, requested))
            elif ts >= MIN_TITLE_SIMILARITY and as_ < MIN_ARTIST_SIMILARITY:
                off_artist.append(cand)
            else:
                off_title.append(cand)
        if fuzzy_hits:
            self._log(f"  · [{source.label}] 其中 {fuzzy_hits} 个候选由**纠错读法**命中"
                      "（原读法未过；门槛未放松）")
        if off_artist:
            self._log(f"  · [{source.label}] 已排除 {len(off_artist)} 个歌手不符的结果："
                      + "、".join(f"“{c.artist}”《{c.title}》" for c in off_artist[:3]))
        if off_title:
            self._log(f"  · [{source.label}] 已排除 {len(off_title)} 个歌名不符的结果："
                      + "、".join(f"“{c.title}”" for c in off_title[:3]))
        if not kept:
            self._log(f"  · [{source.label}] 没有歌名与歌手同时匹配的版本")
        kept = filter_title_alias_ambiguity(readings, kept)
        return kept, len(off_title), len(off_artist), dropped_ver

    def _probe_durations(self, tracks: "list[Track]") -> None:
        """Resolve + measure the real length of the top candidates.

        A candidate whose length cannot be measured is left with
        ``actual_duration = None`` and is therefore **never trusted** (it can
        only be used as a last resort, with a warning).

        **疑似短资源不占探测预算**：直链形态已经能判出"试听/铃声/提示音"的候选
        （``Track.short_asset``），先把预算花在疑似正片上，只有预算还有余才回头
        探测它们 —— 否则两个 5~22 秒的片段就可能把名额占光，让真正完整的候选根本
        没被测到（那是"明明有原曲却报没找到"的一个真实成因）。解析次数同样封顶。
        """
        # 平台标注互不相同时多测几个候选：只测前 2 个很可能"全是缩短版"，
        # 真正完整的那个排在后面反而没被测到 —— 这是"原时长对不上"的主因之一。
        declared = [round(t.duration) for t in tracks[:MAX_PROBES_WIDE] if t.duration]
        limit = MAX_PROBES_WIDE if len(set(declared)) > 1 else MAX_PROBES
        budget = limit
        resolves = limit + MAX_EXTRA_RESOLVES   # 为跳过短资源而多看几个，也要有上限
        deferred: "list[Track]" = []
        for track in tracks:
            if self._cancel:
                raise CancelledError("已取消")
            if budget <= 0:
                break
            adapter = self._adapters.get(track.source)
            if adapter is None:
                continue
            if track.source in _LAST_RESORT_SOURCES:
                deferred.append(track)          # 末位来源：连解析都不做，省下整笔成本
                continue
            if not track.url:
                if resolves <= 0:
                    break
                resolves -= 1
                try:
                    requested = self._requested_recording(track)
                    resolved = adapter.resolve(track, quality=self.settings.quality)
                    if resolved is not track:
                        track.__dict__.update(resolved.__dict__)
                    self._bind_recording(track, requested)
                    self._guard_recording(track, requested)
                except MusicDecryptorError as exc:
                    self._log(f"    - [{track.source}] “{track.title}” 时长探测失败：{exc.message}")
                    continue
            if track.short_asset:
                deferred.append(track)          # 疑似短资源：先让位，别占预算
                continue
            budget -= 1
            self._measure_and_log(track)
        # 还没测到任何"达到时长下限"的候选时，才用**独立额度**补测被降权的候选 ——
        # 既不为末位来源白花解析/探测成本，也绝不因为预算被坏候选吃光就误报"没找到"。
        if not any(t.actual_duration and t.actual_duration >= MIN_TRACK_SECONDS
                   for t in tracks):
            extra = MAX_EXTRA_RESOLVES
            for track in deferred:
                if self._cancel:
                    raise CancelledError("已取消")
                if extra <= 0:
                    break
                extra -= 1
                if not track.url:
                    adapter = self._adapters.get(track.source)
                    if adapter is None:
                        continue
                    try:
                        requested = self._requested_recording(track)
                        resolved = adapter.resolve(track, quality=self.settings.quality)
                        if resolved is not track:
                            track.__dict__.update(resolved.__dict__)
                        self._bind_recording(track, requested)
                        self._guard_recording(track, requested)
                    except MusicDecryptorError as exc:
                        self._log(f"    - [{track.source}] “{track.title}” 时长探测失败：{exc.message}")
                        continue
                self._measure_and_log(track)

    def _measure_and_log(self, track: Track) -> None:
        """探测一个**已解析**候选的真实时长/码率并写回 track（失败只记日志）。"""
        try:
            duration, measured_kbps = self._probe_audio(track)
        except MusicDecryptorError as exc:
            self._log(f"    - [{track.source}] “{track.title}” 时长探测失败：{exc.message}")
            return
        track.actual_duration = duration
        if not track.actual_duration:
            self._log(f"    - [{track.source}] “{track.title}” 无法解析出音频时长，视为不可信")
            return
        self._log(f"    · [{track.source}] “{track.title}” 实测时长 "
                  f"{format_seconds(track.actual_duration)}"
                  f"（接口标注 {format_seconds(track.duration) if track.duration else '无'}）")
        if measured_kbps and measured_kbps != track.bitrate:
            declared_kbps = f"{track.bitrate}k" if track.bitrate else "无"
            self._log(f"      · 实测码率 {measured_kbps}k（接口标注 {declared_kbps}），以实测为准")
            track.bitrate = measured_kbps
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
            if data[:2] == b"\xff\xd8" or data[:8] == b"\x89PNG\r\n\x1a\n":
                return data
            # 咪咕封面是 webp（ID3/FLAC 都允许带 mime）；无法识别的字节一律放弃
            if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                return data
            return b""
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
        # Windows 保留设备名（CON/PRN/NUL/COM1…）无法作为文件名创建：
        # 不加这一步，一首叫《CON》的歌会在写盘时抛 OSError（虽然会被兜住，
        # 但用户看到的是"写入失败"而不是"已解密"）。
        if name.split(".")[0].strip().upper() in _RESERVED_NAMES:
            name = f"{name}_"
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

"""后台线程：解密任务 / 以名找歌任务。

两者的生命周期纪律**完全一致**（2026-10-20 猴子测试挖出的崩溃形状）：
`completed` / `failed` 是在 `run()` **内部**发出的 —— 那一刻线程尚未返回，
宿主必须保住强引用、等 `QThread.finished` 再 `deleteLater()`（见
`MainWindow._retire_worker`）。所以两种 worker 都只发信号、不自杀。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from mmusic.errors import CancelledError, MusicDecryptorError
from mmusic.pipeline import JobResult, Pipeline, SEARCH_PREFIX


def make_log_sink(signal) -> "object":
    """``(message, level)`` → Qt 信号。

    必须显式写成**两参数**的可调用对象：`SignalInstance.emit` 的签名对 Python 不可见，
    让 Pipeline 靠猜参数个数是脆弱接线（曾因此整条日志静默失效）。这里给它一个
    可内省的 lambda，意图一目了然，并由自检/回归测试盯着。
    """
    return lambda message, level="detail": signal.emit(message, level)


class DecryptWorker(QThread):
    logged = Signal(str, str)          # (message, level) —— level ∈ {"result","detail"}
    progressed = Signal(int, int)
    file_done = Signal(object)
    completed = Signal(list)
    failed = Signal(str)

    def __init__(self, settings, paths, cache_path, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.paths = list(paths)
        self.cache_path = cache_path
        self._pipe = None

    def run(self) -> None:  # noqa: D102
        pipe = Pipeline(self.settings, cache_path=self.cache_path,
                        on_log=make_log_sink(self.logged),
                        on_progress=self.progressed.emit,
                        on_file=self.file_done.emit)
        self._pipe = pipe
        try:
            results = pipe.process_many(self.paths)
            self.completed.emit(results)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
        finally:
            pipe.close()
            self._pipe = None

    def cancel(self) -> None:
        if self._pipe is not None:
            self._pipe.cancel()


class NameSearchWorker(QThread):
    """「以名找歌」后台线程（三种模式）。

    * ``auto``：批量自动挑选最优（一键）；
    * ``list``：只检索、不下载，把候选发回界面供人工挑选；
    * ``pick``：对用户点选的那一条执行"**直接下载** + 校验 + 完整度门禁"（不再重搜）。
    """

    logged = Signal(str, str)
    progressed = Signal(int, int)
    file_done = Signal(object)
    completed = Signal(list)
    failed = Signal(str)
    candidates = Signal(str, object, object)   # (查询标签, list[Track], 跨平台参考时长)

    MODE_AUTO, MODE_LIST, MODE_PICK = "auto", "list", "pick"

    def __init__(self, settings, *, cache_path=None, mode: str = MODE_AUTO,
                 items=None, query: str = "", artist: str = "", chosen=None,
                 reference=None, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.cache_path = cache_path
        self.mode = mode
        self.items = list(items or [])
        self.query = query
        self.artist = artist
        self.chosen = chosen
        self.reference = reference      # 「先列候选」时带过来的跨平台参考时长
        self._pipe = None

    def run(self) -> None:  # noqa: D102
        pipe = Pipeline(self.settings, cache_path=self.cache_path,
                        on_log=make_log_sink(self.logged),
                        on_progress=self.progressed.emit,
                        on_file=self.file_done.emit)
        self._pipe = pipe
        try:
            if self.mode == self.MODE_LIST:
                tracks, reference = pipe.search_candidates_ex(self.query,
                                                             artist=self.artist)
                self.candidates.emit(self.query, tracks, reference)
            elif self.mode == self.MODE_PICK:
                # 点名的那一条：直接下载，**不重新检索**
                self.completed.emit([pipe.download_chosen(self.chosen,
                                                          reference=self.reference)])
            else:
                self.completed.emit(pipe.acquire_many(self.items))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
        finally:
            pipe.close()
            self._pipe = None

    def cancel(self) -> None:
        if self._pipe is not None:
            self._pipe.cancel()

class AlbumSearchWorker(QThread):
    """「按专辑找歌」后台线程（三种模式）。

    * ``search``：按专辑名（可加歌手）检索专辑；跨源条目合并为**合并视图**
      （不展示来源；原版置信度排序，其它歌手名的同名版本并列在后）；
    * ``list``：取某张专辑的曲目清单（**多源并集**：各来源曲目表全部取回 → 版本聚类 →
      同版本内并集去重；任何通过校验的来源里有的曲子都必须在结果里）；
    * ``download``：对勾选的曲目逐首执行 ``download_album_track``（原生通道失败
      自动在其它音源强门槛重匹配；仅当全部失败才提示"建议自行获取"；曲名/歌手/
      唱片集统一按曲目表校正，封面与 track number 一并写入）。
    """

    logged = Signal(str, str)
    progressed = Signal(int, int)
    file_done = Signal(object)
    completed = Signal(list)
    failed = Signal(str)
    albums = Signal(str, object)          # (查询串, list[AlbumHit])
    tracklist = Signal(object)            # list[Track]

    MODE_SEARCH, MODE_LIST, MODE_DOWNLOAD = "search", "list", "download"

    def __init__(self, settings, *, cache_path=None, mode: str = MODE_SEARCH,
                 query: str = "", artist: str = "", album=None,
                 chosen_tracks=None, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.cache_path = cache_path
        self.mode = mode
        self.query = query
        self.artist = artist
        self.album = album
        #: 下载模式：条目可以是 Track，也可以是 (Track, 曲序) —— 曲序来自**曲目表位置**
        self.chosen_tracks = [it if isinstance(it, (tuple, list)) else (it, 0)
                              for it in (chosen_tracks or [])]
        self._pipe = None
        self._cover_loaded = False
        self._cover_bytes = b""

    def run(self) -> None:  # noqa: D102
        from mmusic.search import album as album_mod

        pipe = Pipeline(self.settings, cache_path=self.cache_path,
                        on_log=make_log_sink(self.logged),
                        on_progress=self.progressed.emit,
                        on_file=self.file_done.emit)
        self._pipe = pipe
        try:
            log = make_log_sink(self.logged)
            # 尊重用户在设置里启用/停用的音源（与下载链路一致）
            sources = set(getattr(pipe, "_adapters", {}) or {})
            if self.mode == self.MODE_SEARCH:
                hits = album_mod.search_albums(pipe.http, self.query, artist=self.artist,
                                               log=log, sources=sources,
                                               settings=self.settings)
                groups = album_mod.group_albums(hits, query=self.query,
                                                artist=self.artist, log=log)
                self.albums.emit(self.query, groups)
            elif self.mode == self.MODE_LIST:
                tracks = album_mod.group_tracks(pipe.http, self.album, log=log,
                                                settings=self.settings)
                self.tracklist.emit(tracks)
            else:
                self._download(pipe)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
        finally:
            pipe.close()
            self._pipe = None

    def _download(self, pipe) -> None:
        results: "list" = []
        total = len(self.chosen_tracks)
        album_name = getattr(self.album, "name", "") or ""
        for i, item in enumerate(self.chosen_tracks):
            if getattr(pipe, "_cancel", False):
                self.logged.emit("⛔ 已取消", "result")
                break
            track, track_no = item if isinstance(item, (tuple, list)) else (item, 0)
            label = f"{track.artist} - {track.title}".strip(" -")
            try:
                # 专辑批量下载：原生通道 → 其它音源强门槛重匹配；全部失败才提示自行获取
                res = pipe.download_album_track(
                    track, album=album_name, cover=self._album_cover(pipe),
                    track_no=int(track_no or 0),
                    reference=float(track.duration) if track.duration else None)
            except CancelledError:
                self.logged.emit("⛔ 已取消", "result")
                break
            except MusicDecryptorError as exc:
                res = JobResult(f"{SEARCH_PREFIX}{label}", label, "failed",
                                message=exc.message, detail=exc.detail)
                self.logged.emit(f"❌ {label}：{exc.message}", "result")
            except Exception as exc:  # noqa: BLE001 - 一首失败不拖垮整批
                res = JobResult(f"{SEARCH_PREFIX}{label}", label, "failed",
                                message="未预期的错误", detail=str(exc))
                self.logged.emit(f"❌ {label}：未预期的错误（{str(exc)[:120]}）", "result")
            results.append(res)
            self.file_done.emit(res)
            self.progressed.emit(i + 1, total)
        self.completed.emit(results)

    def _album_cover(self, pipe) -> bytes:
        """专辑封面：每张专辑只抓一次，失败静默跳过（封面是附加项，不拖慢批量下载）。"""
        if self._cover_loaded:
            return self._cover_bytes
        self._cover_loaded = True
        self._cover_bytes = b""
        try:
            if not (self.settings.fetch_cover and self.settings.write_metadata):
                return b""
            group = self.album
            if group is None:
                return b""
            for kind, value in group.cover_candidates():
                try:
                    url = value
                    if kind == "pic_id":
                        agg = pipe._adapters.get("aggregator")
                        if agg is None or not hasattr(agg, "fetch_pic"):
                            continue
                        url = agg.fetch_pic(str(value)) or ""
                        if not url:
                            continue
                    data = pipe._fetch_cover(url)
                except Exception:  # noqa: BLE001 - 封面失败绝不拖垮下载
                    continue
                if data:
                    self._cover_bytes = data
                    self.logged.emit("  · 已获取专辑封面（写入整张）", "detail")
                    break
        except Exception:  # noqa: BLE001
            pass
        return self._cover_bytes

    def cancel(self) -> None:
        if self._pipe is not None:
            self._pipe.cancel()

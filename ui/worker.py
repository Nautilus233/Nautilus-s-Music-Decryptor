"""后台线程：解密任务 / 以名找歌任务。

两者的生命周期纪律**完全一致**（2026-10-20 猴子测试挖出的崩溃形状）：
`completed` / `failed` 是在 `run()` **内部**发出的 —— 那一刻线程尚未返回，
宿主必须保住强引用、等 `QThread.finished` 再 `deleteLater()`（见
`MainWindow._retire_worker`）。所以两种 worker 都只发信号、不自杀。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from mmusic.pipeline import Pipeline


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

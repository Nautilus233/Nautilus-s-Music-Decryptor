"""Background worker running the pipeline off the GUI thread."""
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
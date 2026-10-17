"""Background worker running the pipeline off the GUI thread."""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from mmusic.pipeline import Pipeline


class DecryptWorker(QThread):
    logged = Signal(str)
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
                        on_log=self.logged.emit,
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
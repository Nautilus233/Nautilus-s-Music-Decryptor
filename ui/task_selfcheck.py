"""Offline single-song task checks, also shipped in the frozen executable."""
from __future__ import annotations

import tempfile
import time
import wave
from pathlib import Path

from PySide6.QtCore import QEvent

from mmusic.config import AppConfig
from mmusic.model import Track
from mmusic.pipeline import Pipeline
from mmusic.search.base import SearchSource

from .name_search_dialog import NameSearchDialog
from .worker import NameSearchWorker


def run(app) -> int:
    """Exercise real list/pick workers and cancellation without network or music."""
    failures = []

    def check(name, passed):
        print(("PASS " if passed else "FAIL ") + name)
        if not passed:
            failures.append(name)

    def until(predicate, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            if predicate():
                return True
            time.sleep(.005)
        return bool(predicate())

    with tempfile.TemporaryDirectory(prefix="mmusic-name-worker-") as temp:
        root = Path(temp)
        library = root / "library"
        library.mkdir()
        # One minute of generated silence: no copyrighted test assets.
        with wave.open(str(library / "Test Artist - Worker Test.wav"), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\0\0" * (60 * 8000))
        cfg = AppConfig(path=root / "settings.json")
        cfg.settings.sources = ["local"]
        cfg.settings.local_dirs = [str(library)]
        cfg.settings.search_output_dir = str(root / "out")
        cfg.settings.quality = "wav"
        cfg.settings.write_metadata = False
        cfg.settings.fetch_cover = False
        cfg.settings.remember_hashes = False
        dialog = NameSearchDialog(cfg.settings, cfg)
        dialog.show()
        dialog.names.setPlainText("Worker Test")
        dialog.artist.setText("Test Artist")
        dialog.btn_search.click()
        listed = until(lambda: dialog._worker is None and bool(dialog._candidates))
        check("single song: list finishes and selected download is reachable",
              listed and dialog.btn_pick.isEnabled())
        check("single song: list does not write audio", not (root / "out").exists())
        if listed:
            dialog.table.selectRow(0)
            dialog.btn_pick.click()
            downloaded = until(lambda: dialog._worker is None)
            check("single song: selected candidate writes a valid output",
                  downloaded and bool(list((root / "out").glob("*.wav"))))
        dialog.close()
        until(lambda: dialog._worker is None and not dialog._retired)

        # cancel before run() must not disappear during Pipeline construction.
        worker = NameSearchWorker(cfg.settings, cache_path=cfg.cache_path,
                                  mode=NameSearchWorker.MODE_LIST, query="Worker Test")
        seen = []
        worker.candidates.connect(lambda *_: seen.append("candidates"))
        worker.cancelled.connect(lambda: seen.append("cancelled"))
        worker.cancel()
        worker.start()
        finished = until(lambda: not worker.isRunning())
        app.processEvents()
        check("single song: startup cancellation cannot emit stale candidates",
              finished and seen == ["cancelled"])
        if worker.isRunning():
            worker.cancel()
            worker.wait(2000)  # Test harness emergency cleanup, never a UI handler.
        worker.deleteLater()
        dialog.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()

        # Same title/artist, two different recordings: the concert duration
        # must never reject a complete studio file. Exercise the frozen core.
        studio_file = root / "studio.wav"
        with wave.open(str(studio_file), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\0\0" * (243 * 8000))

        class RecordingSource(SearchSource):
            name = "recording_fixture"
            label = "Generated recordings"
            offline = True

            def search(self, query, **kwargs):
                return [Track(self.name, "Worker Song", "Test Artist", duration=255,
                              album="Test Artist LIVE「Concert」", ext="wav"),
                        Track(self.name, "Worker Song", "Test Artist", duration=242,
                              album="Studio Album", ext="wav")]

            def resolve(self, track, **kwargs):
                track.url = studio_file.as_uri()
                return track

        pipe = Pipeline(cfg.settings, cache_path=root / "recording-cache.json",
                        sources=[RecordingSource(None)])
        try:
            tracks, reference = pipe.search_candidates_ex("Worker Song", artist="Test Artist")
            check("recording version: LIVE album cannot set studio reference",
                  reference == 242 and len(tracks) == 1 and tracks[0].album == "Studio Album")
            if tracks and reference == 242:
                result = pipe.download_chosen(tracks[0], reference=reference)
                check("recording version: complete 243s studio passes 242s reference",
                      result.ok and Path(result.out_path).is_file())
        finally:
            pipe.close()
    return len(failures)

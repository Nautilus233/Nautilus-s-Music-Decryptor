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

        # Failed search -> structured suggestion -> explicit click -> list only.
        # Do not let a source-only / frozen signal regression hide the hints.
        from . import worker as worker_module

        class HintSource(SearchSource):
            name = label = "hint_fixture"
            offline = True

            def search(self, query, **kwargs):
                return [Track(self.name, "Unfamiliar Long Melodi", "Test Artist",
                              duration=60, ext="wav")]

            def resolve(self, track, **kwargs):
                track.url = (library / "Test Artist - Worker Test.wav").as_uri()
                return track

        real_pipeline = worker_module.Pipeline
        worker_module.Pipeline = lambda settings, **kwargs: real_pipeline(
            settings, sources=[HintSource(None)], **kwargs)
        cfg.settings.search_output_dir = str(root / "hint-out")
        hint_dialog = NameSearchDialog(cfg.settings, cfg)
        try:
            hint_dialog.show()
            hint_dialog.names.setPlainText("Unfamiliar Long Melody")
            hint_dialog.artist.setText("Test Artist")
            hint_dialog.btn_search.click()
            failed_search = until(lambda: hint_dialog._worker is None)
            check("failed search: suggestion survives real queued worker signals",
                  failed_search and hint_dialog._suggestion_choices == [
                      ("Unfamiliar Long Melodi", "Test Artist")]
                  and hint_dialog._suggestion_buttons[0].isEnabled()
                  and not hint_dialog.btn_pick.isEnabled())
            check("failed search: no nonexistent candidate/download prompt",
                  hint_dialog.table.rowCount() == 0
                  and "是否需要下载" not in hint_dialog.log.toPlainText()
                  and "可换一条" not in hint_dialog.status.text())
            if hint_dialog._suggestion_choices:
                hint_dialog._suggestion_buttons[0].click()
                searched = until(lambda: hint_dialog._worker is None)
                check("suggestion click: fills exact fields and searches without downloading",
                      searched and hint_dialog.names.toPlainText() == "Unfamiliar Long Melodi"
                      and hint_dialog.artist.text() == "Test Artist"
                      and len(hint_dialog._candidates) == 1
                      and hint_dialog.btn_pick.isEnabled()
                      and not (root / "hint-out").exists())
        finally:
            hint_dialog.close()
            until(lambda: hint_dialog._worker is None)
            worker_module.Pipeline = real_pipeline
            hint_dialog.deleteLater()

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

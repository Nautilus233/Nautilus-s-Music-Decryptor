"""Real Qt widgets/signals for failure states; no public network or audio."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QObject, QPoint, QRect, Signal
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from mmusic.config import AppConfig
from mmusic.errors import SearchNoResultError
from mmusic.model import Track
from mmusic.pipeline import JobResult, Pipeline
from mmusic.search.base import SearchSource
from ui import geometry, theme
from ui.name_search_dialog import MIN_SIZE, NameSearchDialog


class SignalWorker(QObject):
    """Keep finished separate, as a real worker does during cleanup."""
    MODE_AUTO, MODE_LIST, MODE_PICK = "auto", "list", "pick"
    logged = Signal(str, str)
    progressed = Signal(int, int)
    file_done = Signal(object)
    completed = Signal(list)
    failed = Signal(str)
    candidates = Signal(str, object, object)
    suggestions = Signal(object)
    cancelled = Signal()
    finished = Signal()

    def __init__(self, settings=None, *, mode="list", parent=None, **kw):
        super().__init__(parent)
        self.mode, self.arguments = mode, kw
        self.cancel_requested = False
        self.running = False

    def start(self):
        self.running = True

    def cancel(self):
        self.cancel_requested = True

    def isRunning(self):
        return self.running


class SearchFailureUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.apply(cls.app, "light")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="search_failure_ui_")
        self.config = AppConfig(path=Path(self.tmp.name) / "settings.json")
        self.config.settings.output_dir = self.tmp.name
        self.dialog = NameSearchDialog(self.config.settings, self.config)
        self.dialog.names.setPlainText("夜曲子")
        self.dialog.artist.setText("旧歌手")
        self.workers = []
        self.dialog.show()
        self.pump()

    def tearDown(self):
        self.dialog.reject()
        if isinstance(self.dialog._worker, SignalWorker):
            self.finish(self.dialog._worker)
        self.assertTrue(self.wait(lambda: self.dialog._worker is None))
        self.dialog.deleteLater()
        self.pump()
        self.tmp.cleanup()

    def pump(self):
        for _ in range(3):
            self.app.processEvents()

    def wait(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.pump()
            time.sleep(.002)
        self.pump()
        return bool(predicate())

    def worker(self, mode="list"):
        worker = SignalWorker(mode=mode, parent=self.dialog)
        self.workers.append(worker)
        self.dialog._start_worker(worker)
        return worker

    def finish(self, worker):
        worker.running = False
        worker.finished.emit()
        self.pump()

    def seed_failure(self, choices=None):
        worker = self.worker()
        worker.suggestions.emit(choices if choices is not None else [("夜曲", "周杰伦")])
        worker.failed.emit("未找到目标歌曲，候选已排除。")
        self.finish(worker)

    def assert_no_choices(self):
        self.assertEqual(self.dialog._suggestion_choices, [])
        self.assertTrue(self.dialog._suggestion_area.isHidden())
        self.assertTrue(all(b.isHidden() and not b.isEnabled()
                            for b in self.dialog._suggestion_buttons))

    def assert_no_download_guidance(self):
        text = self.dialog.status.text() + "\n" + self.dialog.log.toPlainText()
        for fragment in ("可换一条候选", "请先在列表", "点「下载选中」", "请确认是否需要下载"):
            self.assertNotIn(fragment, text)
        self.assertFalse(self.dialog.btn_pick.isEnabled())

    def test_search_exception_empty_list_never_offers_a_candidate(self):
        worker = self.worker()
        worker.failed.emit("未找到目标歌曲，只有非目标录音版本，已排除。")
        self.assertEqual(self.dialog.table.rowCount(), 0)
        self.assert_no_download_guidance()
        self.finish(worker)
        self.assert_no_download_guidance()
        self.dialog.download_selected()
        self.assertEqual(self.dialog.status.text(), "没有可下载的候选")

    def test_empty_resolved_list_has_no_guidance_in_either_log_mode(self):
        worker = self.worker()
        worker.candidates.emit("夜曲子", [], None)
        worker.completed.emit([])
        self.finish(worker)
        self.assertEqual(self.dialog.status.text(), "没有可下载的候选")
        self.assert_no_choices()
        for checked in (False, True, False):
            self.dialog.chk_detail.setChecked(checked)
            self.assert_no_download_guidance()

    def test_suggestions_separate_capped_disabled_until_finished(self):
        worker = self.worker()
        choices = [("夜曲", "周杰伦"), ("原文", "歌手"), ("无歌手", ""), ("第四项", "")]
        worker.suggestions.emit(choices)
        worker.failed.emit("搜索失败")
        self.assertEqual(self.dialog._suggestion_choices, choices[:3])
        self.assertTrue(self.dialog._suggestion_area.isVisible())
        self.assertEqual(sum(b.isVisible() for b in self.dialog._suggestion_buttons), 3)
        self.assertTrue(all(not b.isEnabled() for b in self.dialog._suggestion_buttons))
        self.assertEqual(self.dialog.table.rowCount(), 0)
        self.assert_no_download_guidance()
        self.dialog._suggestion_buttons[0].click()
        self.assertEqual(self.dialog.names.toPlainText(), "夜曲子")
        self.finish(worker)
        self.assertTrue(all(b.isEnabled() for b in self.dialog._suggestion_buttons))

    def test_click_refills_both_fields_and_starts_list_only(self):
        choices = [("夜曲", "新歌手"), ("原文 & <tag>", ""), ("第三项", "歌手三")]
        for index, (title, artist) in enumerate(choices):
            with self.subTest(index=index):
                self.seed_failure(choices)
                captured = []

                class CaptureWorker(SignalWorker):
                    def __init__(inner, *args, **kw):
                        super().__init__(*args, **kw)
                        captured.append(inner)

                with patch("ui.name_search_dialog.NameSearchWorker", CaptureWorker):
                    self.dialog._suggestion_buttons[index].click()
                self.assertEqual(len(captured), 1)
                worker = captured[0]
                self.assertEqual(worker.mode, "list")
                self.assertEqual(worker.arguments["query"], title)
                self.assertEqual(worker.arguments["artist"], artist)
                self.assertNotIn("chosen", worker.arguments)
                self.assertNotIn("items", worker.arguments)
                self.assertEqual(self.dialog.names.toPlainText(), title)
                self.assertEqual(self.dialog.artist.text(), artist)
                self.assert_no_choices()
                self.assertNotIn("你可能想找", self.dialog.log.toPlainText())
                self.assertFalse(self.dialog.btn_pick.isEnabled())
                self.finish(worker)

    def test_metadata_literal_in_button_tooltip_and_compact_result(self):
        title, artist = '<b>A&B</b> "原文"', '<img src=x>&歌手'
        self.seed_failure([(title, artist)])
        button = self.dialog._suggestion_buttons[0]
        self.assertEqual(button.text(), (title + "\n" + artist).replace("&", "&&"))
        doc = QTextDocument()
        doc.setHtml(button.toolTip())
        self.assertIn(title, doc.toPlainText())
        self.assertIn(artist, doc.toPlainText())
        self.assertIn("仅列出候选，不会直接下载", doc.toPlainText())
        self.assertEqual(self.dialog.log.toPlainText().count("你可能想找的歌是："), 0)

    def test_details_stay_folded_and_persistent_failure_survives_buffer_limit(self):
        worker = self.worker()
        suffix = "你可能想找的歌是：<夜曲 & 原文>？"
        worker.failed.emit("未找到符合要求的歌曲")
        hint = self.dialog._failure_hint.text()
        worker.logged.emit("明细" * 500 + "\n" + suffix, "detail")
        self.assertNotIn(suffix, self.dialog.log.toPlainText())
        for i in range(4100):
            worker.logged.emit(f"后续明细 {i}", "detail")
        self.dialog.chk_detail.setChecked(True)
        self.dialog.chk_detail.setChecked(False)
        self.assertNotIn(suffix, self.dialog.log.toPlainText())
        self.assertEqual(self.dialog._failure_hint.text(), hint)
        self.assertTrue(self.dialog._failure_hint.isVisible())
        self.assertEqual(self.dialog._hidden_details,
                         sum(level != "result" for _, level in self.dialog._log_lines))
        self.assertLessEqual(len(self.dialog._log_lines), 4000)
        self.assert_no_choices()
        self.finish(worker)

    def test_auto_failures_keep_latest_choices_after_completed(self):
        worker = self.worker("auto")
        first, latest = [("第一首", "甲")], [("第二首", "乙")]
        results = []
        for i, choices in enumerate((first, latest)):
            worker.suggestions.emit(choices)
            result = JobResult(f"search:{i}", str(i), "failed", suggestions=tuple(choices))
            results.append(result)
            worker.file_done.emit(result)
        worker.completed.emit(results)
        self.assertEqual(self.dialog._suggestion_choices, latest)
        self.assertFalse(self.dialog._suggestion_buttons[0].isEnabled())
        self.finish(worker)
        self.assertEqual(self.dialog._suggestion_choices, latest)
        self.assertTrue(self.dialog._suggestion_buttons[0].isEnabled())
        self.assert_no_download_guidance()

    def test_auto_empty_suggestions_and_success_preserve_previous(self):
        worker = self.worker("auto")
        worker.suggestions.emit([("上一首", "歌手")])
        worker.file_done.emit(JobResult("search:first", "first", "failed", message="首曲失败",
                                        suggestions=(("上一首", "歌手"),)))
        hint = self.dialog._failure_hint.text()
        worker.suggestions.emit([])
        worker.file_done.emit(JobResult("search:x", "x", "failed"))
        worker.file_done.emit(JobResult("search:y", "y", "downloaded"))
        worker.completed.emit([JobResult("search:x", "x", "failed"),
                               JobResult("search:y", "y", "downloaded")])
        self.finish(worker)
        self.assertEqual(self.dialog._suggestion_choices, [("上一首", "歌手")])
        self.assertEqual(self.dialog._failure_hint.text(), hint)
        self.assertTrue(self.dialog._failure_hint.isVisible())
        self.assertTrue(self.dialog._suggestion_buttons[0].isEnabled())

    def test_pick_failure_preserves_candidates_and_other_choice(self):
        listing = self.worker()
        tracks = [Track("mock", title="夜曲", artist="甲"), Track("mock", title="夜曲", artist="乙")]
        listing.candidates.emit("夜曲", tracks, None)
        self.finish(listing)
        self.assertTrue(self.dialog.btn_pick.isEnabled())
        for result_signal in ("failed", "completed"):
            with self.subTest(signal=result_signal):
                worker = self.worker("pick")
                self.dialog._pick_row = 0
                if result_signal == "failed":
                    worker.failed.emit("下载校验未通过")
                else:
                    worker.completed.emit([JobResult("search:夜曲", "夜曲", "failed")])
                self.assertIn("可换一条候选再试", self.dialog.status.text())
                self.assertEqual(self.dialog.table.item(0, 0).text(), "失败")
                self.assertFalse(self.dialog.btn_pick.isEnabled())
                self.finish(worker)
                self.dialog.table.selectRow(1)
                self.assertTrue(self.dialog.btn_pick.isEnabled())
                self.assertEqual(self.dialog._candidates, tracks)

    def test_single_pick_candidate_never_offers_nonexistent_other(self):
        listing = self.worker()
        listing.candidates.emit("夜曲", [Track("mock", title="夜曲")], None)
        self.finish(listing)
        worker = self.worker("pick")
        self.dialog._pick_row = 0
        worker.failed.emit("下载失败")
        self.assertNotIn("可换一条", self.dialog.status.text())
        self.finish(worker)
        self.assertTrue(self.dialog.btn_pick.isEnabled())

    def test_research_and_both_input_edits_clear_previous(self):
        for field, value in ((self.dialog.names, "另一首"), (self.dialog.artist, "另一个歌手")):
            self.seed_failure()
            field.setPlainText(value) if field is self.dialog.names else field.setText(value)
            self.assert_no_choices()
            self.assertEqual(self.dialog.status.text(), "就绪")
        self.seed_failure()
        with patch("ui.name_search_dialog.NameSearchWorker", SignalWorker):
            self.dialog.btn_search.click()
        self.assert_no_choices()
        self.assertEqual(self.dialog.log.toPlainText(), "")
        self.assertEqual(self.dialog.table.rowCount(), 0)
        self.assertFalse(self.dialog.btn_pick.isEnabled())
        self.finish(self.dialog._worker)

    def test_stop_foreign_and_cancelled_signals_cannot_restore_choices(self):
        worker = self.worker()
        worker.suggestions.emit([("当前建议", "歌手")])
        foreign = SignalWorker()
        foreign.suggestions.connect(self.dialog._on_suggestions)
        foreign.failed.connect(self.dialog._on_failed)
        foreign.suggestions.emit([("过期建议", "旧任务")])
        foreign.failed.emit("过期错误")
        self.assertEqual(self.dialog._suggestion_choices, [("当前建议", "歌手")])
        self.dialog.btn_stop.click()
        self.assert_no_choices()
        worker.suggestions.emit([("取消后建议", "旧任务")])
        worker.logged.emit("你可能想找的歌是：取消后的建议？", "result")
        worker.failed.emit("迟到错误，可换一条候选")
        worker.candidates.emit("迟到", [Track("mock", title="迟到")], None)
        worker.completed.emit([])
        self.assertEqual(self.dialog.status.text(), "正在停止…")
        self.assert_no_choices()
        self.finish(worker)
        self.assertEqual(self.dialog.status.text(), "已停止")
        self.assertFalse(self.dialog.btn_pick.isEnabled())
        self.assertNotIn("取消后的建议", self.dialog.log.toPlainText())
        foreign.suggestions.emit([("结束后的过期建议", "旧任务")])
        self.assert_no_choices()
        foreign.deleteLater()

    def test_three_long_suggestions_fit_small_work_area(self):
        self.seed_failure([("长标题 & <原文>" * 16, "长歌手" * 10),
                           ("第二项" * 30, "歌手二"), ("第三项" * 30, "歌手三")])
        work_area = QRect(0, 0, 800, 600)
        with patch("ui.geometry.available_geometry", return_value=work_area):
            geometry.fit(self.dialog, preferred=MIN_SIZE, minimum=MIN_SIZE)
            self.pump()
        self.assertTrue(work_area.contains(self.dialog.frameGeometry()))
        for widget in [self.dialog._suggestion_area, *self.dialog._suggestion_buttons,
                       self.dialog._failure_hint,
                       self.dialog.btn_search, self.dialog.btn_pick, self.dialog.btn_stop,
                       self.dialog.log, self.dialog.findChild(QDialogButtonBox)]:
            with self.subTest(widget=type(widget).__name__):
                rect = QRect(widget.mapTo(self.dialog, QPoint()), widget.size())
                self.assertTrue(widget.isVisible())
                self.assertTrue(self.dialog.rect().contains(rect), str(rect))

    def test_real_worker_button_only_researches_without_audio(self):
        calls = []

        class PipelineStub:
            def __init__(self, *_args, **_kwargs):
                pass

            def search_candidates_ex(self, query, *, artist):
                calls.append((query, artist))
                if len(calls) == 1:
                    raise SearchNoResultError("搜索失败", suggestions=[("夜曲", "周杰伦")])
                return [Track("mock", title="夜曲", artist="周杰伦")], None

            def download_chosen(self, *_args, **_kwargs):
                raise AssertionError("suggestions must never download audio")

            acquire_many = download_chosen

            def close(self):
                pass

            def cancel(self):
                pass

        with patch("ui.worker.Pipeline", PipelineStub):
            self.dialog.btn_search.click()
            self.assertTrue(self.wait(lambda: self.dialog._worker is None))
            self.assertEqual(self.dialog._suggestion_choices, [("夜曲", "周杰伦")])
            self.dialog._suggestion_buttons[0].click()
            self.assertTrue(self.wait(lambda: self.dialog._worker is None))
        self.assertEqual(calls, [("夜曲子", "旧歌手"), ("夜曲", "周杰伦")])
        self.assert_no_choices()
        self.assertEqual(self.dialog.table.rowCount(), 1)
        self.assertTrue(self.dialog.btn_pick.isEnabled())

    def test_real_pipeline_flac_four_sources_default_compact_failure(self):
        """Real Pipeline/worker/Qt wiring, only provider metadata is fake."""
        calls = []
        provide_hints = False

        class MetadataOnlySource(SearchSource):
            def __init__(inner, name):
                super().__init__(None)
                inner.name = inner.label = name

            def search(inner, query, **kwargs):
                calls.append((inner.name, query, kwargs.get("quality")))
                if provide_hints:
                    return [Track(inner.name, "夜曲", "周杰伦")]
                return [Track(inner.name, "毫不相关的歌曲", "其他歌手")]

            def resolve(inner, *args, **kwargs):
                raise AssertionError("weak metadata must not resolve audio")

        sources = [MetadataOnlySource(name) for name in ("kuwo", "kugou", "migu", "aggregator")]
        self.config.settings.quality = "flac"
        self.config.settings.remember_hashes = False
        self.config.settings.fetch_cover = False
        self.dialog.artist.clear()
        self.assertFalse(self.dialog.chk_detail.isChecked())

        def factory(settings, **kwargs):
            return Pipeline(settings, sources=sources, **kwargs)

        with patch("ui.worker.Pipeline", side_effect=factory), \
             patch.object(Pipeline, "_download", side_effect=AssertionError("download")), \
             patch.object(Pipeline, "_probe_audio", side_effect=AssertionError("probe")):
            for provide_hints in (False, True):
                with self.subTest(hints=provide_hints):
                    calls.clear()
                    self.dialog.btn_search.click()
                    self.assertTrue(self.wait(lambda: self.dialog._worker is None))
                    self.assertFalse(self.dialog.chk_detail.isChecked())
                    self.assertTrue(self.dialog._failure_hint.isVisible())
                    self.assertEqual(self.dialog.table.rowCount(), 0)
                    self.assert_no_download_guidance()
                    self.assertEqual(calls[0][0], "aggregator")
                    self.assertEqual({name for name, _, _ in calls}, {s.name for s in sources})
                    self.assertTrue(all(quality == "flac" for _, _, quality in calls))
                    if provide_hints:
                        self.assertEqual(self.dialog._suggestion_choices, [("夜曲", "周杰伦")])
                        self.assertTrue(self.dialog._suggestion_buttons[0].isVisible())
                        self.assertTrue(self.dialog._suggestion_buttons[0].isEnabled())
                        self.assertEqual(self.dialog.log.toPlainText().count("你可能想找的歌是："), 1)
                    else:
                        self.assert_no_choices()
                        self.assertIn("未找到足够接近曲名", self.dialog._failure_hint.text())
                        self.assertIn("精简歌名或补充歌手", self.dialog._failure_hint.text())
                    for checked in (True, False):
                        self.dialog.chk_detail.setChecked(checked)
                        self.assertTrue(self.dialog._failure_hint.isVisible())
                        self.assertEqual(self.dialog._suggestion_area.isVisible(), provide_hints)
                        if provide_hints:
                            self.assertTrue(self.dialog._suggestion_buttons[0].isVisible())
                            self.assertTrue(self.dialog._suggestion_buttons[0].isEnabled())
        self.assertEqual(list(Path(self.tmp.name).glob("*.flac")), [])

    def test_failure_summary_plain_text_and_direct_slots(self):
        message = '<img src=x> & <b>原文</b>'
        self.dialog._on_failed(message)
        self.assertIn(message, self.dialog._failure_hint.text())
        doc = QTextDocument()
        doc.setHtml(self.dialog._failure_hint.toolTip())
        self.assertEqual(doc.toPlainText(), message)
        self.dialog._on_file_done(JobResult("search:x", "x", "failed", message=message))
        self.assertTrue(self.dialog._failure_hint.isVisible())


if __name__ == "__main__":
    unittest.main(verbosity=2)

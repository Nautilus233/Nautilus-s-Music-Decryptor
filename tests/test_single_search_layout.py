"""Real Qt layout regression; synthetic metadata, no network or audio files.

    python tests/test_single_search_layout.py          # offscreen
    python tests/test_single_search_layout.py --native # native platform/fonts

The matrix uses emulated work areas large enough for the requested client
sizes. All sizing goes through geometry.fit, including window decorations.
Only Pipeline is replaced: buttons, workers, queued signals, layouts, item
delegates and font metrics are real. No header resize policy is prescribed.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

if "--native" in sys.argv:
    sys.argv.remove("--native")
else:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialogButtonBox,
                               QLabel, QLineEdit, QPlainTextEdit, QPushButton)

from mmusic.config import AppConfig
from mmusic.errors import SearchNoResultError
from mmusic.model import Track
from mmusic.pipeline import JobResult, SEARCH_PREFIX
from ui import geometry, theme
from ui.name_search_dialog import MIN_SIZE, NameSearchDialog


CLIENT_SIZES = ((800, 600), (1024, 768), (880, 700))
LONG_TITLE = ('布局测试：很长的原始歌名 <原文 & 保留> "完整标题" ' * 5).rstrip()
CHOICES = ((LONG_TITLE, "测试歌手"), ("第二首建议", "歌手乙"),
           ("第三首建议", "歌手丙"))
DETAIL = "布局测试明细：静默核验已完成"
RESULT = "布局测试结果：检索已结束"


class SingleSearchLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.apply(cls.app, "light")
        print("LAYOUT platform:", cls.app.platformName(), flush=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="single_layout_")
        self.addCleanup(self.tmp.cleanup)
        self.config = AppConfig(path=Path(self.tmp.name) / "settings.json")
        self.config.settings.output_dir = self.tmp.name
        self.config.settings.fetch_cover = False
        self.config.settings.remember_hashes = False

    def pump(self):
        for _ in range(4):
            self.app.processEvents()

    def wait_idle(self, dialog):
        deadline = time.monotonic() + 5
        while dialog._worker is not None and time.monotonic() < deadline:
            self.pump()
            time.sleep(.002)
        self.pump()
        self.assertIsNone(dialog._worker, "worker did not finish/retire")

    @contextmanager
    def scenario(self, count):
        owner = self
        calls = []
        tracks = [Track("local", title=LONG_TITLE if i == 0 else f"候选歌曲 {i}",
                        artist="测试歌手", duration=181 + i, bitrate=320)
                  for i in range(count)]

        class OfflinePipeline:
            def __init__(self, *_args, on_log, **_kwargs):
                self.log = on_log

            def search_candidates_ex(self, query, *, artist):
                calls.append(("search", query, artist))
                self.log(DETAIL, "detail")
                self.log(RESULT, "result")
                if query == "输入有误的歌名":
                    raise SearchNoResultError("没有找到符合要求的歌曲，已排除不匹配版本。",
                                              suggestions=CHOICES)
                owner.assertEqual((query, artist), CHOICES[0])
                return tracks, 181 if tracks else None

            def download_chosen(self, track, *, reference):
                calls.append(("pick", track, reference))
                self.log(DETAIL, "detail")
                self.log(RESULT, "result")
                return JobResult(SEARCH_PREFIX + track.title, track.title, "downloaded")

            def close(self):
                pass

            def cancel(self):
                pass

        # Fail closed if an unexpected path ever attempts a real connection.
        with patch("ui.worker.Pipeline", OfflinePipeline), patch(
                "socket.socket.connect", side_effect=AssertionError("network forbidden")):
            dialog = NameSearchDialog(self.config.settings, self.config)
            try:
                dialog.names.setPlainText("输入有误的歌名")
                dialog.show()
                self.pump()
                yield dialog, tracks, calls
            finally:
                dialog.reject()
                self.wait_idle(dialog)
                dialog.deleteLater()
                self.pump()

    def fit_client(self, dialog, size):
        ml, mt, mr, mb = geometry.frame_margins(dialog)
        work_area = QRect(32, 32, size[0] + ml + mr + geometry.SAFETY,
                          size[1] + mt + mb + geometry.SAFETY)
        with patch.object(geometry, "available_geometry", return_value=work_area):
            geometry.fit(dialog, preferred=size, minimum=MIN_SIZE)
            self.pump()
        self.assertEqual(dialog.size(), QSize(*size), "layout forced a larger client")
        self.assertTrue(work_area.contains(dialog.frameGeometry()),
                        f"frame {dialog.frameGeometry()} outside {work_area}")

    @staticmethod
    def rect_in_dialog(widget, dialog):
        return QRect(widget.mapTo(dialog, QPoint()), widget.size())

    def assert_inside(self, dialog):
        widgets = [dialog.table]
        for kind in (QLabel, QPushButton, QCheckBox, QLineEdit,
                     QPlainTextEdit, QDialogButtonBox):
            widgets.extend(dialog.findChildren(kind))
        for widget in widgets:
            if not widget.isVisibleTo(dialog):
                continue
            if isinstance(widget, QLabel) and not widget.text():
                continue  # An empty detail-count label may legitimately have no width.
            rect = self.rect_in_dialog(widget, dialog)
            label = widget.objectName() or getattr(widget, "text", lambda: "")()
            self.assertGreater(rect.width(), 0, f"zero width: {label}")
            self.assertGreater(rect.height(), 0, f"zero height: {label}")
            self.assertTrue(dialog.rect().contains(rect),
                            f"{type(widget).__name__} {label!r}: {rect} outside {dialog.rect()}")
        # Being inside the window is insufficient if a neighbour covers it.
        blocks = [dialog.table, dialog.status, dialog.log]
        blocks.extend(dialog.findChildren(QDialogButtonBox))
        buttons = (dialog.btn_search, dialog.btn_pick, dialog.btn_start, dialog.btn_stop)
        for button in buttons:
            for block in blocks:
                self.assertFalse(self.rect_in_dialog(button, dialog).intersects(
                    self.rect_in_dialog(block, dialog)), "action overlaps table/status/log/footer")
        self.assertGreaterEqual(dialog.log.viewport().height(), dialog.log.fontMetrics().height(),
                                "log cannot display a complete text line")

    def assert_compact_actions(self, dialog):
        buttons = (dialog.btn_search, dialog.btn_pick, dialog.btn_start, dialog.btn_stop)
        rects = [self.rect_in_dialog(b, dialog) for b in buttons]
        action_height = max(r.bottom() for r in rects) - min(r.top() for r in rects) + 1
        # Allow style/font padding; reject the previous ~200px action strip.
        budget = max(b.sizeHint().height() for b in buttons) + dialog.fontMetrics().height()
        self.assertLessEqual(action_height, budget, "actions consume candidate-list space")
        status = dialog.status
        if status.text():
            self.assertGreaterEqual(status.width(),
                                    status.fontMetrics().horizontalAdvance("找到 12 条"))
            self.assertLessEqual(status.height(), status.fontMetrics().lineSpacing() * 3,
                                 "status wrapped into an excessively tall strip")
        return action_height

    def assert_complete_row(self, dialog):
        table = dialog.table
        self.assertGreater(table.rowCount(), 0)
        table.scrollToTop()
        self.pump()
        row = table.visualRect(table.model().index(0, 1))
        self.assertGreaterEqual(row.height(), table.fontMetrics().height(),
                                "row clips the actual font")
        viewport = table.viewport().rect()
        self.assertGreaterEqual(row.top(), viewport.top())
        self.assertLessEqual(row.bottom(), viewport.bottom(),
                             f"partial first row: row={row}, viewport={viewport}")

    def assert_columns_and_tooltip(self, dialog):
        table = dialog.table
        columns = {table.horizontalHeaderItem(c).text(): c for c in range(table.columnCount())}
        title = columns["歌名"]
        self.assertGreater(table.columnWidth(title),
                           max(table.columnWidth(columns[k]) for k in ("时长", "码率")),
                           "title is narrower than numeric auxiliary fields")
        total = sum(table.columnWidth(c) for c in range(table.columnCount())
                    if not table.isColumnHidden(c))
        width = table.viewport().width()
        self.assertGreaterEqual(total, width - 2, "unused space after the last column")
        if total > width + 2:
            bar = table.horizontalScrollBar()
            self.assertTrue(bar.isVisible() and bar.maximum() > bar.minimum(),
                            "overflow without usable horizontal scrolling")
            table.scrollToItem(table.item(0, table.columnCount() - 1))
            self.pump()
            last = table.visualRect(table.model().index(0, table.columnCount() - 1))
            self.assertLessEqual(last.right(), table.viewport().rect().right() + 1,
                                 "last column is unreachable")
            bar.setValue(bar.minimum())
            self.pump()
        item = table.item(0, title)
        self.assertEqual(item.text(), LONG_TITLE)
        self.assertGreater(table.fontMetrics().horizontalAdvance(item.text()),
                           table.columnWidth(title), "fixture must exercise an elided title")
        tooltip = item.toolTip()
        if tooltip.lstrip().startswith(("<qt>", "<html>")):
            doc = QTextDocument()
            doc.setHtml(tooltip)
            tooltip = doc.toPlainText()
        self.assertIn(LONG_TITLE, tooltip, "tooltip lost/truncated literal original metadata")

    def assert_log_modes(self, dialog, size, *, candidates):
        for expanded in (False, True, False):
            with self.subTest(details=expanded):
                dialog.chk_detail.setChecked(expanded)
                self.pump()
                self.assertEqual(DETAIL in dialog.log.toPlainText(), expanded)
                self.assertIn(RESULT, dialog.log.toPlainText())
                self.fit_client(dialog, size)
                self.assert_inside(dialog)
                self.assert_compact_actions(dialog)
                if candidates:
                    self.assert_complete_row(dialog)
                    self.assert_columns_and_tooltip(dialog)

    def seed_suggestions(self, dialog):
        self.assertFalse(dialog.chk_detail.isChecked(), "details must default to folded")
        dialog.btn_search.click()
        self.wait_idle(dialog)
        self.assertEqual(dialog.table.rowCount(), 0)
        visible = [b for b in dialog._suggestion_buttons if b.isVisibleTo(dialog)]
        self.assertEqual(len(visible), 3)
        self.assertTrue(all(b.isEnabled() for b in visible))
        self.assertFalse(dialog.btn_pick.isEnabled())

    def test_suggestion_research_layout_matrix(self):
        for size in CLIENT_SIZES:
            for count in (1, 0, 12):
                with self.subTest(client=size, candidates=count), self.scenario(count) as state:
                    dialog, tracks, calls = state
                    self.fit_client(dialog, size)
                    self.seed_suggestions(dialog)
                    # In particular: 600px client + failure + three suggestions.
                    self.assert_log_modes(dialog, size, candidates=False)
                    dialog._suggestion_buttons[0].click()
                    self.wait_idle(dialog)
                    self.assertEqual(calls, [("search", "输入有误的歌名", ""),
                                             ("search", *CHOICES[0])])
                    self.assertEqual(dialog.table.rowCount(), count)
                    self.assertFalse(any(b.isVisibleTo(dialog) for b in dialog._suggestion_buttons))
                    self.assertEqual(dialog.btn_pick.isEnabled(), bool(tracks))
                    self.assert_log_modes(dialog, size, candidates=bool(tracks))
                    if tracks:
                        dialog.table.selectRow(count - 1)
                        dialog.btn_pick.click()
                        self.wait_idle(dialog)
                        self.assertEqual(len(calls), 3, "picking must not search again")
                        self.assertEqual(calls[-1], ("pick", tracks[-1], 181))
                        self.assertEqual(dialog.table.item(count - 1, 0).text(), "已完成")
                        self.assert_log_modes(dialog, size, candidates=True)

    def test_extra_height_goes_to_candidate_viewport(self):
        with self.scenario(12) as (dialog, _tracks, _calls):
            self.seed_suggestions(dialog)
            dialog._suggestion_buttons[0].click()
            self.wait_idle(dialog)
            for expanded in (False, True):
                dialog.chk_detail.setChecked(expanded)
                measurements = []
                # Compare within the same comfortable density. Crossing the
                # compact breakpoint also restores input/log breathing room.
                for size in ((880, 660), (880, 760)):
                    self.fit_client(dialog, size)
                    self.assert_inside(dialog)
                    self.assert_complete_row(dialog)
                    measurements.append((dialog.table.viewport().height(),
                                         self.assert_compact_actions(dialog)))
                self.assertGreaterEqual(measurements[1][0] - measurements[0][0], 70,
                                        "extra 100px should mostly grow the candidate viewport")
                self.assertLessEqual(abs(measurements[1][1] - measurements[0][1]),
                                     dialog.fontMetrics().height(), "action strip grew with window")

    def test_short_work_area_includes_native_window_frame(self):
        with self.scenario(1) as (dialog, _tracks, _calls):
            work_area = QRect(32, 32, 800, 600)
            with patch.object(geometry, "available_geometry", return_value=work_area):
                geometry.fit(dialog, preferred=(880, 700), minimum=MIN_SIZE)
                self.seed_suggestions(dialog)
                self.pump()
                self.assertTrue(work_area.contains(dialog.frameGeometry()))
                self.assert_inside(dialog)
                dialog._suggestion_buttons[0].click()
                self.wait_idle(dialog)
                self.assertTrue(work_area.contains(dialog.frameGeometry()))
                self.assert_inside(dialog)
                self.assert_complete_row(dialog)


if __name__ == "__main__":
    unittest.main(verbosity=2)

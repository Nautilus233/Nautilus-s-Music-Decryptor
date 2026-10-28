"""Offline presentation matrix; --native uses actual Windows font metrics.

    python tests/test_album_presentation.py
    python tests/test_album_presentation.py --native
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if "--native" in sys.argv:
    sys.argv.remove("--native")
else:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialogButtonBox, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton)

from mmusic.config import AppConfig
from mmusic.model import Track
from ui import geometry, theme
from ui.album_search_dialog import AlbumSearchDialog, MIN_SIZE
from ui.manual_links_dialog import ManualLinksDialog


NAMES = ("很长的中文专辑名 <原文 & 保留> " * 8,
         "残酷な天使のテーゼ / Long album title <原文> " * 8)


class AlbumPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        print("PRESENTATION platform:", cls.app.platformName(), flush=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="album_presentation_")
        self.addCleanup(self.tmp.cleanup)
        self.config = AppConfig(path=Path(self.tmp.name) / "settings.json")
        self.config.settings.output_dir = self.tmp.name
        # Any accidental worker/network activity is a test failure.
        guard = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def pump(self):
        for _ in range(4):
            self.app.processEvents()

    def fit(self, dialog, size, minimum):
        ml, mt, mr, mb = geometry.frame_margins(dialog)
        area = QRect(0, 0, max(800, size[0] + ml + mr + geometry.SAFETY),
                     max(600, size[1] + mt + mb + geometry.SAFETY))
        with patch.object(geometry, "available_geometry", return_value=area):
            geometry.fit(dialog, preferred=size, minimum=minimum)
            self.pump()
        self.assertEqual(dialog.size(), QSize(*size), "layout enlarged the requested client")
        self.assertTrue(area.contains(dialog.frameGeometry()))

    def inside(self, dialog):
        for kind in (QLabel, QPushButton, QLineEdit, QPlainTextEdit, QCheckBox):
            for widget in dialog.findChildren(kind):
                if not widget.isVisibleTo(dialog) or widget.window() is not dialog:
                    continue  # Directory menu is a separate screen-aware popup.
                if isinstance(widget, QLabel) and not widget.text():
                    continue
                rect = QRect(widget.mapTo(dialog, QPoint()), widget.size())
                self.assertGreater(rect.width(), 0, getattr(widget, "text", lambda: "")())
                self.assertTrue(dialog.rect().contains(rect), str(rect))
        self.assertGreaterEqual(dialog.log.viewport().height(), dialog.log.fontMetrics().height())
        self.assertGreaterEqual(dialog.status.width(), 120)
        self.assertLessEqual(dialog.status.height(), dialog.status.fontMetrics().height() + 6)

    def tooltip_text(self, widget):
        doc = QTextDocument()
        doc.setHtml(widget.toolTip())
        return doc.toPlainText()

    def assert_table(self, table, title_column, name):
        table.scrollToTop()
        self.pump()
        first = table.visualRect(table.model().index(0, title_column))
        self.assertGreaterEqual(first.height(), table.fontMetrics().height() + 8)
        self.assertTrue(table.viewport().rect().contains(first),
                        f"incomplete first row: {first}, viewport {table.viewport().rect()}")
        self.assertGreater(table.columnWidth(title_column), 120)
        self.assertIn(name, self.tooltip_text(table.item(0, title_column)))
        self.assertFalse(table.wordWrap())
        self.assertEqual(sum(table.columnWidth(c) for c in range(table.columnCount())),
                         table.viewport().width())

    def test_album_layout_and_selection_matrix(self):
        for mode in ("light", "dark", "system"):
            theme.apply(self.app, mode)
            for name in NAMES:
                for size in (MIN_SIZE, (640, 560), (980, 780)):
                    with self.subTest(theme=mode, name=name[:16], size=size):
                        dialog = AlbumSearchDialog(self.config.settings, self.config)
                        try:
                            dialog.show()
                            self.pump()
                            self.fit(dialog, size, MIN_SIZE)
                            self.inside(dialog)  # Empty results must also fit.
                            album = SimpleNamespace(name=name, artist="歌手 / Artist",
                                                    track_count=12, count_hint="预估，点击加载", year="2026")
                            dialog._on_albums("test", [album] * 12)
                            dialog._current_album = album
                            dialog._current_album_row = 0
                            tracks = [Track("local", title=name, artist="歌手 / Artist", duration=182)
                                      for _ in range(12)]
                            dialog._on_tracklist(tracks)
                            dialog._log("结果始终可见")
                            dialog._log("折叠的处理详情", "detail")
                            dialog.status.setText("已记住要切换到这张专辑，当前任务完成后自动加载" * 4)
                            for details in (False, True, False):
                                dialog.chk_detail.setChecked(details)
                                self.pump()
                                self.fit(dialog, size, MIN_SIZE)
                                self.inside(dialog)
                                self.assert_table(dialog.album_table, 0, name)
                                self.assert_table(dialog.track_table, 1, name)
                                self.assertIn("结果始终可见", dialog.log.toPlainText())
                                self.assertEqual("折叠的处理详情" in dialog.log.toPlainText(), details)
                            self.assertEqual(dialog.album_count.text(), "共 12 张")
                            self.assertEqual(dialog.selection_hint.text(), "已选 12 / 共 12 首")
                            dialog.btn_none.click()
                            self.assertEqual(dialog.selection_hint.text(), "已选 0 / 共 12 首")
                            dialog.btn_all.click()
                            dialog.track_table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
                            self.assertEqual(dialog.selection_hint.text(), "已选 11 / 共 12 首")
                            self.assertEqual(dialog._checked_tracks(), tracks[1:])
                            # Failure detail remains available when unrelated cells update.
                            dialog._dl_rows = [0]
                            dialog._on_file_done(SimpleNamespace(status="failed", message="校验失败", detail="保留证据"))
                            self.assertIn("保留证据", dialog.track_table.item(0, 5).toolTip())
                            dialog.track_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
                            self.assertIn("保留证据", dialog.track_table.item(0, 5).toolTip())
                        finally:
                            dialog.close()
                            dialog.deleteLater()
                            self.pump()

    def test_directory_menu_and_settings_contract(self):
        dialog = AlbumSearchDialog(self.config.settings, self.config)
        try:
            dialog.show()
            self.pump()
            dialog.output_menu.popup(dialog.btn_location.mapToGlobal(QPoint(0, dialog.btn_location.height())))
            self.pump()
            for button in (dialog.btn_out_dir, dialog.btn_follow_main, dialog.btn_open_out):
                self.assertTrue(button.isVisible())
            with patch("ui.album_search_dialog.QFileDialog.getExistingDirectory", return_value=self.tmp.name), patch.object(self.config, "save") as save:
                dialog.btn_out_dir.click()
                self.assertEqual(self.config.settings.search_output_dir, self.tmp.name)
                dialog.btn_follow_main.click()
                self.assertEqual(self.config.settings.search_output_dir, "")
                self.assertEqual(save.call_count, 2)
            self.assertIn(self.tmp.name, self.tooltip_text(dialog.btn_location))
        finally:
            dialog.output_menu.close()
            dialog.close()
            dialog.deleteLater()
            self.pump()

    def test_manual_links_native_minimum_and_roundtrip(self):
        entries = ["https://example.com/song.mp3", r"D:\我的音乐\曲目.flac"]
        for mode in ("light", "dark", "system"):
            theme.apply(self.app, mode)
            dialog = ManualLinksDialog(entries)
            try:
                dialog.show()
                self.pump()
                for size in ((460, 300), (680, 460)):
                    self.fit(dialog, size, (460, 300))
                    self.assertGreaterEqual(dialog.editor.viewport().height(), dialog.editor.fontMetrics().height() * 2)
                    box = dialog.findChild(QDialogButtonBox)
                    self.assertTrue(dialog.rect().contains(QRect(box.mapTo(dialog, QPoint()), box.size())))
                self.assertEqual(dialog.entries(), entries)
                dialog.editor.setPlainText("\n".join(entries + entries) + "; 'extra' ,, ")
                self.assertEqual(dialog.entries(), entries + ["extra"])
                box.button(QDialogButtonBox.StandardButton.Ok).click()
                self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
            finally:
                dialog.close()
                dialog.deleteLater()
                self.pump()


if __name__ == "__main__":
    unittest.main(verbosity=2)

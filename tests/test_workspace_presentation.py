"""Workspace layout contracts, using native Qt unless the runner overrides it."""
from __future__ import annotations

import sys
import tempfile
import unittest
from html import escape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, QRect, QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QScrollArea

from mmusic.config import AppConfig
from ui import geometry, theme
from ui.main_window import MainWindow
from ui.settings_dialog import MIN_SIZE, SettingsDialog


class WorkspacePresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mmusic_presentation_")
        self.addCleanup(self.temp.cleanup)
        self.config = AppConfig(path=Path(self.temp.name) / "settings.json")
        self.config.settings.auto_start = False
        self.store = QSettings(str(Path(self.temp.name) / "window.ini"), QSettings.Format.IniFormat)
        self.store_patch = patch.object(MainWindow, "_settings_store", return_value=self.store)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)

    def settle(self):
        for _ in range(3):
            self.app.processEvents()

    def inside(self, child, parent):
        return parent.rect().contains(QRect(child.mapTo(parent, QPoint()), child.size()))

    def test_main_native_sizes_and_long_status_preserve_queue(self):
        for mode in ("light", "dark", "system"):
            theme.apply(self.app, mode)
            for width, height in ((1024, 720), (1280, 720), (840, 568)):
                with self.subTest(mode=mode, work_area=(width, height)):
                    self.store.clear()
                    win = MainWindow(self.config)
                    try:
                        with patch.object(geometry, "available_geometry", return_value=QRect(0, 0, width, height)):
                            win.show()
                            self.settle()
                            geometry.fit(win, preferred=(1080, 760),
                                         minimum=(win.minimumWidth(), win.minimumHeight()))
                            self.settle()
                            self.assertLessEqual(win.minimumWidth(), 820)
                            self.assertLessEqual(win.minimumHeight(), 520)
                            self.assertLessEqual(win.frameGeometry().width(), width)
                            self.assertLessEqual(win.frameGeometry().height(), height)
                            self.assertTrue(self.inside(win.btn_start, win))
                            self.assertTrue(self.inside(win.log, win))
                            self.assertGreaterEqual(win.table.viewport().height(), 36)
                            original_size, original_table = win.size(), win.table.height()
                            message = "QQ 登录态可能失效，<b>原始文字</b> & 长文件路径：" + "目录/" * 180
                            win.status_label.setText(message)
                            win.out_dir.setText("C:/" + "很长的输出目录/" * 100)
                            win._log("过程明细" * 300, "detail")
                            self.settle()
                            self.assertEqual(win.size(), original_size)
                            self.assertEqual(win.table.height(), original_table)
                            self.assertGreater(win.status_label.width(), win.width() * 0.8)
                            self.assertLessEqual(win.status_label.height(), win.status_label.fontMetrics().height() + 6)
                            self.assertEqual(win.status_label.text(), message)
                            self.assertEqual(win.status_label.accessibleDescription(), message)
                            self.assertEqual(win.status_label.toolTip(), "<qt>" + escape(message) + "</qt>")
                            self.assertFalse(win.status_label.wordWrap())
                    finally:
                        win.close()

    def test_empty_queue_and_result_tooltips(self):
        theme.apply(self.app, "light")
        win = MainWindow(self.config)
        try:
            win.show()
            self.settle()
            self.assertTrue(win.empty_state.isVisible())
            source = Path(self.temp.name) / "原始文件.ncm"
            win._add_row(source)
            self.settle()
            self.assertFalse(win.empty_state.isVisible())
            self.assertIn("1", win.queue_count.text())
            output = str(Path(self.temp.name) / "输出目录" / "歌曲.mp3")
            message = "<b>说明</b> & " * 300
            win._on_file_done(SimpleNamespace(path=str(source), status="decoded", fmt="mp3",
                                             out_path=output, message=message, source=""))
            self.assertEqual(win.table.item(0, 0).toolTip(), "<qt>" + escape(str(source)) + "</qt>")
            self.assertEqual(win.table.item(0, 3).toolTip(), "<qt>" + escape(output) + "</qt>")
            self.assertEqual(win.table.item(0, 4).toolTip(), "<qt>" + escape(message) + "</qt>")
            self.assertGreaterEqual(win.table.rowHeight(0), 32)
            win._clear()
            self.settle()
            self.assertTrue(win.empty_state.isVisible())
            self.assertIn("0", win.queue_count.text())
        finally:
            win.close()

    def test_settings_all_tabs_scroll_at_original_minimum(self):
        for mode in ("light", "dark", "system"):
            with self.subTest(mode=mode):
                theme.apply(self.app, mode)
                dialog = SettingsDialog(self.config.settings)
                try:
                    dialog.show()
                    self.settle()
                    geometry.fit(dialog, preferred=MIN_SIZE, minimum=MIN_SIZE)
                    self.settle()
                    self.assertEqual((dialog.minimumWidth(), dialog.minimumHeight()), MIN_SIZE)
                    footer = dialog.findChild(QDialogButtonBox)
                    footer_position = footer.mapTo(dialog, QPoint())
                    for index in range(dialog.tabs.count()):
                        dialog.tabs.setCurrentIndex(index)
                        self.settle()
                        area = dialog.tabs.widget(index)
                        self.assertIsInstance(area, QScrollArea)
                        self.assertEqual(area.horizontalScrollBar().maximum(), 0)
                        scroll = area.verticalScrollBar()
                        scroll.setValue(scroll.maximum())
                        self.settle()
                        self.assertTrue(self.inside(footer, dialog))
                        self.assertEqual(footer.mapTo(dialog, QPoint()), footer_position)
                        # Bottom content is reachable, including the final form row.
                        body_bottom = area.widget().mapTo(area.viewport(), QPoint(0, area.widget().height())).y()
                        self.assertLessEqual(body_bottom, area.viewport().height())
                        self.assertTrue(footer.button(QDialogButtonBox.StandardButton.Ok).isVisible())
                finally:
                    dialog.reject()

    def test_cookie_feedback_never_displays_credential_fragments(self):
        dialog = SettingsDialog(self.config.settings)
        try:
            for raw in ("synthetic_private_session_key", "uin=42; qqmusic_key=synthetic_private_session_key"):
                clipboard = SimpleNamespace(text=lambda: raw)
                with patch.object(QApplication, "clipboard", return_value=clipboard):
                    dialog._paste_cookie()
                self.assertEqual(dialog.qq_key.text(), raw)
                self.assertNotIn("session_key", dialog.qq_feedback.text())
                self.assertEqual(dialog.qq_key.echoMode(), dialog.qq_key.EchoMode.Password)
            self.assertEqual(dialog.qq_uin.text(), "42")
        finally:
            dialog.reject()


if __name__ == "__main__":
    unittest.main()

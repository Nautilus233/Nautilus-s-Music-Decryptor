"""Appearance preferences and theme changes must leave audio settings untouched."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from mmusic.config import Settings
from ui import theme
from ui.settings_dialog import SettingsDialog


class Hints(QObject):
    colorSchemeChanged = Signal(object)

    def __init__(self):
        super().__init__()
        self.scheme = Qt.ColorScheme.Light

    def colorScheme(self):
        return self.scheme

    def change(self, scheme):
        self.scheme = scheme
        self.colorSchemeChanged.emit(scheme)


class Surface:
    """Real Qt palette/style with a controllable OS scheme notification."""
    def __init__(self, app):
        self.app = app
        self.hints = Hints()
        self.updates = 0

    def styleHints(self):
        return self.hints

    def palette(self):
        return self.app.palette()

    def setPalette(self, palette):
        self.app.setPalette(palette)

    def setStyleSheet(self, text):
        self.updates += 1
        self.app.setStyleSheet(text)


class AppearanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_follow_system_changes_and_manual_override(self):
        surface = Surface(self.app)
        theme.apply(surface, "system")
        self.assertGreater(surface.palette().color(QPalette.ColorRole.Window).lightness(), 128)
        surface.hints.change(Qt.ColorScheme.Dark)
        self.assertLess(surface.palette().color(QPalette.ColorRole.Window).lightness(), 128)
        theme.apply(surface, "light")
        surface.hints.change(Qt.ColorScheme.Dark)
        self.assertGreater(surface.palette().color(QPalette.ColorRole.Window).lightness(), 128)
        theme.apply(surface, "system")
        self.assertLess(surface.palette().color(QPalette.ColorRole.Window).lightness(), 128)
        before = surface.updates
        surface.hints.change(Qt.ColorScheme.Light)
        self.assertEqual(surface.updates, before + 1)  # no duplicate signal wiring

    def test_preference_persistence_and_bad_value(self):
        with tempfile.TemporaryDirectory() as temp:
            store = QSettings(str(Path(temp) / "appearance.ini"), QSettings.Format.IniFormat)
            with patch.object(theme, "QSettings", return_value=store):
                self.assertEqual(theme.preference(), "system")
                theme.save_preference(self.app, "dark")
                self.assertEqual(theme.preference(), "dark")
                theme.save_preference(self.app, "invalid")
                self.assertEqual(theme.preference(), "system")

    def test_settings_page_does_not_change_audio_settings(self):
        settings = Settings(quality="flac", qqmusic_key="synthetic", qqmusic_uin="42")
        before = settings.to_dict()
        with patch.object(theme, "preference", return_value="system"):
            dialog = SettingsDialog(settings)
            for mode in ("light", "dark", "system"):
                dialog.appearance.setCurrentIndex(dialog.appearance.findData(mode))
                clone = Settings.from_dict(before)
                dialog.apply_to(clone)
                # Source checkboxes expand defaults explicitly, as in the old UI.
                actual = clone.to_dict()
                self.assertTrue(actual.pop("sources"))
                expected = dict(before)
                expected.pop("sources")
                self.assertEqual(actual, expected)
            dialog.reject()
            self.assertEqual(settings.to_dict(), before)



if __name__ == "__main__":
    unittest.main()

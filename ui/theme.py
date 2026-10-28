"""Light/dark widget themes, following the OS through Qt style hints."""
from __future__ import annotations

import weakref

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QPalette, QColor

MODES = (("浅色", "light"), ("深色", "dark"), ("跟随系统", "system"))


def preference() -> str:
    value = QSettings("MusicDecryptor", "MusicDecryptor").value("appearance", "system")
    return value if value in ("light", "dark", "system") else "system"


def save_preference(app, mode: str) -> None:
    mode = mode if mode in ("light", "dark", "system") else "system"
    QSettings("MusicDecryptor", "MusicDecryptor").setValue("appearance", mode)
    apply(app, mode)


def _colors(dark: bool) -> dict:
    if dark:
        return dict(bg="#16191f", surface="#20242c", field="#1b1f26", text="#edf1f7",
                    muted="#aab4c4", border="#353d49", hover="#2a313c", alt="#242a34",
                    selected="#263f61", accent="#93bfff", button="#276cc1", disabled="#818c9e",
                    danger="#ffabb4", danger_bg="#442a35")
    return dict(bg="#f5f6f8", surface="#ffffff", field="#ffffff", text="#202b3b",
                muted="#58677b", border="#dce2eb", hover="#edf3fc", alt="#f8fafc",
                selected="#e0edff", accent="#1e5ca5", button="#2467b3", disabled="#7a8798",
                danger="#a8263a", danger_bg="#fff0f1")


def stylesheet(dark: bool) -> str:
    c = _colors(dark)
    return """
* { font-family: "Microsoft YaHei", "SimSun"; font-size: 13px; }
QWidget { background: %(bg)s; color: %(text)s; }
QLabel { background: transparent; }
QLabel[role="title"] { font-size: 22px; font-weight: 700; }
QLabel[role="subtitle"], QLabel[role="muted"] { color: %(muted)s; }
QLabel[role="section"] { font-weight: 600; font-size: 14px; }
QLabel[role="badge"] { background: %(selected)s; color: %(accent)s; border: 1px solid %(border)s;
    border-radius: 6px; padding: 3px 9px; font-weight: 600; }
QLabel[role="summary"] { color: %(muted)s; }
QFrame[role="card"], QWidget[role="card"] { background: %(surface)s; border: 1px solid %(border)s; border-radius: 10px; }
QFrame[role="toolbar"], QWidget[role="toolbar"] { background: %(alt)s; border: 1px solid %(border)s; border-radius: 8px; }
QPushButton { background: %(surface)s; border: 1px solid %(border)s; border-radius: 6px;
              padding: 6px 11px; min-height: 18px; }
QPushButton:hover { background: %(hover)s; border-color: %(accent)s; }
QPushButton:pressed { background: %(selected)s; }
QPushButton:focus { border: 2px solid %(accent)s; padding: 5px 10px; }
QPushButton[role="quiet"] { background: transparent; border-color: transparent; color: %(accent)s; }
QPushButton[role="quiet"]:hover { background: %(hover)s; border-color: %(border)s; }
QPushButton:disabled { color: %(disabled)s; background: %(bg)s; border-color: %(border)s; }
QPushButton[role="primary"] { background: %(button)s; border-color: %(button)s; color: white; font-weight: 600; }
QPushButton[role="primary"]:hover { background: #185a9c; }
QPushButton[role="primary"]:disabled { background: %(border)s; border-color: %(border)s; color: %(disabled)s; }
QPushButton[role="navigation"] { text-align: left; padding: 10px 14px; font-weight: 600; border-radius: 9px; }
QPushButton[role="current"] { text-align: left; padding: 10px 14px; font-weight: 600; border-radius: 9px;
                              background: %(selected)s; color: %(accent)s; border-color: %(accent)s; }
QPushButton[role="danger"] { background: %(danger_bg)s; color: %(danger)s; }
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox { background: %(field)s; border: 1px solid %(border)s;
    border-radius: 6px; padding: 6px 8px; selection-background-color: %(selected)s; selection-color: %(text)s; }
QComboBox:focus, QLineEdit:focus, QPlainTextEdit:focus { border-color: %(accent)s; }
QComboBox QAbstractItemView { background: %(surface)s; color: %(text)s;
    selection-background-color: %(selected)s; selection-color: %(text)s; }
QTableWidget, QListWidget { background: %(surface)s; border: 1px solid %(border)s;
    border-radius: 7px; gridline-color: %(border)s; alternate-background-color: %(alt)s;
    selection-background-color: %(selected)s; selection-color: %(text)s; }
QHeaderView::section { background: %(alt)s; color: %(muted)s; padding: 7px 8px;
    border: none; border-bottom: 1px solid %(border)s; font-weight: 600; }
QTableWidget::item { padding: 4px 8px; }
QTableWidget::item:selected { background: %(selected)s; color: %(text)s; border: none; }
QTableWidget::item:focus { outline: none; }
QProgressBar { background: %(surface)s; border: 1px solid %(border)s; border-radius: 5px;
    min-height: 15px; text-align: center; }
QProgressBar::chunk { background: %(button)s; border-radius: 4px; }
QPlainTextEdit { background: %(field)s; border: 1px solid %(border)s; border-radius: 7px; padding: 5px;
    selection-background-color: %(selected)s; selection-color: %(text)s; }
QCheckBox { spacing: 7px; background: transparent; }
QCheckBox:disabled { color: %(disabled)s; }
QPushButton[role="danger"]:disabled { color: %(disabled)s; background: %(bg)s; }
QGroupBox { background: %(surface)s; border: 1px solid %(border)s; border-radius: 8px;
    margin-top: 12px; padding: 14px 10px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; color: %(muted)s; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: %(bg)s; color: %(muted)s; padding: 9px 16px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: %(accent)s; border-bottom-color: %(accent)s; font-weight: 600; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: %(border)s; border-radius: 4px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; }
QScrollBar::handle:horizontal { background: %(border)s; border-radius: 4px; min-width: 24px; }
QToolTip { background: %(surface)s; color: %(text)s; border: 1px solid %(border)s; padding: 4px; }
QMenu { background: %(surface)s; color: %(text)s; border: 1px solid %(border)s;
    border-radius: 8px; padding: 4px; }
QMenu::item { padding: 6px 12px; border-radius: 4px; }
QMenu::item:selected { background: %(selected)s; }
""" % c


def apply(app, mode: str | None = None) -> None:
    if app is None:
        return
    if mode is not None or not hasattr(app, "_music_theme_mode"):
        app._music_theme_mode = mode if mode in ("light", "dark", "system") else preference()
    if not getattr(app, "_music_theme_connected", False):
        app._music_theme_connected = True
        try:
            target = weakref.ref(app)

            def refresh(*_):
                current = target()
                if current is not None:
                    apply(current)

            app.styleHints().colorSchemeChanged.connect(refresh)
        except AttributeError:
            pass  # Older Qt: initial palette still provides a compatible fallback.
        app._music_initial_dark = app.palette().color(QPalette.ColorRole.Window).lightness() < 128
    active = app._music_theme_mode
    system_dark = getattr(app, "_music_initial_dark", False)
    try:
        scheme = app.styleHints().colorScheme()
        if scheme != Qt.ColorScheme.Unknown:
            system_dark = scheme == Qt.ColorScheme.Dark
    except AttributeError:
        pass
    dark = active == "dark" or (active == "system" and system_dark)
    colors = _colors(dark)
    palette = QPalette(app.palette())
    for role, key in ((QPalette.ColorRole.Window, "bg"), (QPalette.ColorRole.Base, "surface"),
                      (QPalette.ColorRole.Text, "text"), (QPalette.ColorRole.WindowText, "text"),
                      (QPalette.ColorRole.Button, "surface"), (QPalette.ColorRole.ButtonText, "text"),
                      (QPalette.ColorRole.Highlight, "selected"), (QPalette.ColorRole.HighlightedText, "text")):
        palette.setColor(role, QColor(colors[key]))
    app.setPalette(palette)
    app.setStyleSheet(stylesheet(dark))

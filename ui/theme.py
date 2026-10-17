"""Dark QSS theme."""
from __future__ import annotations

ACCENT = "#2f6bff"
ACCENT_HOVER = "#4d81ff"
OK = "#36c07a"
WARN = "#f0a83c"
ERR = "#ef5350"
MUTED = "#8b93a7"

QSS = f"""
* {{ font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif; font-size: 13px; }}
QWidget {{ background: #151824; color: #e6e9f2; }}
QMainWindow, QDialog {{ background: #10131d; }}

QLabel[role="title"] {{ font-size: 20px; font-weight: 700; color: #ffffff; }}
QLabel[role="subtitle"] {{ color: {MUTED}; }}
QLabel[role="muted"] {{ color: {MUTED}; }}

QFrame[role="card"] {{
    background: #1b1f2e; border: 1px solid #262b3d; border-radius: 12px;
}}

QPushButton {{
    background: #232838; border: 1px solid #313848; border-radius: 8px;
    padding: 7px 14px; color: #dfe3ee;
}}
QPushButton:hover {{ background: #2a3145; border-color: #3d4557; }}
QPushButton:pressed {{ background: #202634; }}
QPushButton:disabled {{ color: #5b6274; background: #1a1e2b; border-color: #242a38; }}
QPushButton[role="primary"] {{
    background: {ACCENT}; border: 1px solid {ACCENT}; color: #ffffff; font-weight: 600;
}}
QPushButton[role="primary"]:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[role="danger"] {{ background: #3a2130; border-color: #5a2b3c; color: #ffb3c0; }}

QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{
    background: #1b2030; border: 1px solid #2c3344; border-radius: 8px;
    padding: 6px 9px; color: #e6e9f2; selection-background-color: {ACCENT};
}}
QComboBox:focus, QLineEdit:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: #1b2030; border: 1px solid #2c3344; selection-background-color: {ACCENT};
    outline: none;
}}

QTableWidget {{
    background: #171b28; border: 1px solid #262b3d; border-radius: 10px;
    gridline-color: #222738; alternate-background-color: #1a1f2d;
    selection-background-color: #22305c; selection-color: #ffffff;
}}
QHeaderView::section {{
    background: #1c2130; color: {MUTED}; padding: 8px; border: none;
    border-right: 1px solid #242a38; border-bottom: 1px solid #262b3d; font-weight: 600;
}}
QTableWidget::item {{ padding: 5px 8px; }}

QProgressBar {{
    background: #1b2030; border: 1px solid #262b3d; border-radius: 8px;
    height: 16px; text-align: center; color: #cfd5e4;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 7px; }}

QPlainTextEdit {{
    background: #0d1017; border: 1px solid #242a38; border-radius: 10px;
    color: #c9d1e4; font-family: Consolas, "Cascadia Mono", monospace; font-size: 12px;
}}

QCheckBox {{ spacing: 7px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid #3a4256; background: #1b2030;
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #333b4e; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #404a63; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #333b4e; border-radius: 5px; min-width: 30px; }}

QToolTip {{ background: #232838; color: #e6e9f2; border: 1px solid #333b4e; }}
"""


def apply(app) -> None:
    app.setStyleSheet(QSS)
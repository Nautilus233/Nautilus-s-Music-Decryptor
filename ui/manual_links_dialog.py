"""手动补链对话框：粘贴直链或本地路径（每行一条）。"""
from __future__ import annotations

import re

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel,
                               QPlainTextEdit, QVBoxLayout)

from . import geometry

MIN_SIZE = (460, 300)
PREFERRED_SIZE = (680, 460)


def split_entries(text: str) -> list[str]:
    """一行一条；也允许用逗号/分号分隔。去掉空白与重复。"""
    out: list[str] = []
    for piece in re.split(r"[\r\n;,]+", text or ""):
        item = piece.strip().strip('"').strip("'")
        if item and item not in out:
            out.append(item)
    return out


class ManualLinksDialog(QDialog):
    def __init__(self, existing: "list[str] | None" = None, parent=None) -> None:
        super().__init__(parent)
        self._fitted = False
        self.setWindowTitle("提供音频链接")
        self.setMinimumSize(*MIN_SIZE)
        root = QVBoxLayout(self)
        tip = QLabel("粘贴你自己的音频链接或本地文件路径，每行一条。\n"
                     "处理文件时会优先使用这些资源，不核对歌名和歌手；文件及完整度校验仍会执行。")
        tip.setWordWrap(True)
        tip.setProperty("role", "muted")
        root.addWidget(tip)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("https://example.com/song.mp3\nD:\\我的音乐\\某首歌.flac")
        self.editor.setPlainText("\n".join(existing or []))
        root.addWidget(self.editor, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存链接")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        root.addWidget(buttons)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._fitted:
            self._fitted = True
            geometry.fit(self, preferred=PREFERRED_SIZE, minimum=MIN_SIZE,
                         center_on=self.parentWidget())

    def entries(self) -> list[str]:
        return split_entries(self.editor.toPlainText())
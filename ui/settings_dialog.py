"""Settings dialog.

The body lives inside a `QScrollArea` and the OK/Cancel footer is *pinned*
below it, so the buttons are reachable no matter how short the screen is -
a QDialog cannot shrink below its layout's minimum size, so without the
scroll area a tall form simply clipped its own footer.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QFrame, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QPushButton,
                               QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from . import geometry

_SOURCE_LABELS = {
    "local": "本地曲库（离线，优先）",
    "kuwo": "酷我音乐",
    "kugou": "酷狗音乐",
    "aggregator": "全网聚合（MyFreeMP3/Netease）",
}

MIN_SIZE = (460, 380)
PREFERRED_SIZE = (620, 720)


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._fitted = False
        self.setWindowTitle("设置")
        self.setMinimumSize(*MIN_SIZE)
        self._build()
        self._fill()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- scrollable body ----------------------------------------
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(14, 12, 14, 12)
        body.setSpacing(10)

        # -- sources ---------------------------------------------------
        gb_src = QGroupBox("备用音源（按顺序尝试）")
        v = QVBoxLayout(gb_src)
        self.source_boxes: "dict[str, QCheckBox]" = {}
        for name in _SOURCE_LABELS:
            cb = QCheckBox(_SOURCE_LABELS[name])
            self.source_boxes[name] = cb
            v.addWidget(cb)
        body.addWidget(gb_src)

        # -- network / generic ----------------------------------------
        gb_net = QGroupBox("网络与通用")
        form = QFormLayout(gb_net)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(3, 120)
        self.timeout.setSuffix(" 秒")
        self.retries = QSpinBox()
        self.retries.setRange(0, 8)
        self.min_bitrate = QSpinBox()
        self.min_bitrate.setRange(0, 1000)
        self.min_bitrate.setSuffix(" kbps")
        self.agg_base = QLineEdit()
        self.agg_provider = QLineEdit()
        form.addRow("请求超时", self.timeout)
        form.addRow("重试次数", self.retries)
        form.addRow("最低码率", self.min_bitrate)
        form.addRow("聚合接口地址", self.agg_base)
        form.addRow("聚合默认平台", self.agg_provider)
        body.addWidget(gb_net)

        # -- local dirs ------------------------------------------------
        gb_local = QGroupBox("本地曲库目录（可选，离线优先命中）")
        lv = QVBoxLayout(gb_local)
        self.local_list = QListWidget()
        self.local_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.local_list.setMinimumHeight(88)          # keeps the form compact
        lv.addWidget(self.local_list)
        row = QHBoxLayout()
        add = QPushButton("添加目录…")
        rm = QPushButton("移除所选")
        add.clicked.connect(self._add_local_dir)
        rm.clicked.connect(self._remove_local_dirs)
        row.addWidget(add)
        row.addWidget(rm)
        row.addStretch(1)
        lv.addLayout(row)
        body.addWidget(gb_local)

        # -- behaviour -------------------------------------------------
        gb_beh = QGroupBox("行为")
        bv = QVBoxLayout(gb_beh)
        self.chk_fallback = QCheckBox("解密失败时自动搜索下载备用音源")
        self.chk_meta = QCheckBox("写入元数据（标题/艺术家/专辑/封面）")
        self.chk_cover = QCheckBox("在线补齐封面")
        self.chk_overwrite = QCheckBox("同名文件直接覆盖（否则自动加序号）")
        self.chk_keep = QCheckBox("保留原始加密文件")
        self.chk_cache = QCheckBox("启用哈希去重缓存（避免重复解密/下载）")
        for cb in (self.chk_fallback, self.chk_meta, self.chk_cover,
                   self.chk_overwrite, self.chk_keep, self.chk_cache):
            bv.addWidget(cb)
        body.addWidget(gb_beh)

        body.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        # ---- pinned footer (always visible) --------------------------
        footer = QWidget()
        fv = QVBoxLayout(footer)
        fv.setContentsMargins(14, 8, 14, 12)
        fv.setSpacing(8)
        note = QLabel("仅限个人本地研究使用，请尊重版权，勿传播版权内容。")
        note.setProperty("role", "muted")
        note.setWordWrap(True)
        fv.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        fv.addWidget(buttons)
        root.addWidget(footer, 0)

    # ------------------------------------------------------------------ #
    def showEvent(self, event) -> None:  # noqa: N802
        # decorations are known only once shown, so size to the work area here
        super().showEvent(event)
        if not self._fitted:
            self._fitted = True
            geometry.fit(self, preferred=PREFERRED_SIZE, minimum=MIN_SIZE,
                         center_on=self.parentWidget())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)

    # ------------------------------------------------------------------ #
    def _fill(self) -> None:
        s = self.settings
        for name, cb in self.source_boxes.items():
            cb.setChecked(name in (s.sources or []))
        self.timeout.setValue(float(s.timeout))
        self.retries.setValue(int(s.retries))
        self.min_bitrate.setValue(int(s.min_bitrate))
        self.agg_base.setText(s.aggregator_base)
        self.agg_provider.setText(s.aggregator_provider)
        self.local_list.addItems(list(s.local_dirs or []))
        self.chk_fallback.setChecked(s.enable_fallback)
        self.chk_meta.setChecked(s.write_metadata)
        self.chk_cover.setChecked(s.fetch_cover)
        self.chk_overwrite.setChecked(s.overwrite)
        self.chk_keep.setChecked(s.keep_original)
        self.chk_cache.setChecked(s.remember_hashes)

    def _add_local_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择本地曲库目录")
        if d and not self.local_list.findItems(d, Qt.MatchFlag.MatchExactly):
            self.local_list.addItem(d)

    def _remove_local_dirs(self) -> None:
        for item in self.local_list.selectedItems():
            self.local_list.takeItem(self.local_list.row(item))

    def apply_to(self, settings) -> None:
        settings.sources = [n for n, cb in self.source_boxes.items() if cb.isChecked()]
        settings.timeout = float(self.timeout.value())
        settings.retries = int(self.retries.value())
        settings.min_bitrate = int(self.min_bitrate.value())
        settings.aggregator_base = self.agg_base.text().strip()
        settings.aggregator_provider = self.agg_provider.text().strip() or "netease"
        settings.local_dirs = [self.local_list.item(i).text() for i in range(self.local_list.count())]
        settings.enable_fallback = self.chk_fallback.isChecked()
        settings.write_metadata = self.chk_meta.isChecked()
        settings.fetch_cover = self.chk_cover.isChecked()
        settings.overwrite = self.chk_overwrite.isChecked()
        settings.keep_original = self.chk_keep.isChecked()
        settings.remember_hashes = self.chk_cache.isChecked()
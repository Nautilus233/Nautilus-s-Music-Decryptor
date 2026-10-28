"""Settings dialog.

The body lives inside a `QScrollArea` and the OK/Cancel footer is *pinned*
below it, so the buttons are reachable no matter how short the screen is -
a QDialog cannot shrink below its layout's minimum size, so without the
scroll area a tall form simply clipped its own footer.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog,
                               QComboBox, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QFrame, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QPushButton,
                               QScrollArea, QSizePolicy, QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from . import geometry, theme

from mmusic.search.pack import all_specs, load_pack   # 源列表由清单驱动，新增源自动可见

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

        # Heading and footer belong to the dialog, not any scrolling page.
        heading = QLabel("设置")
        heading.setProperty("role", "title")
        header = QHBoxLayout()
        header.setContentsMargins(16, 10, 16, 2)
        header.addWidget(heading)
        header.addStretch(1)
        root.addLayout(header)

        def note(text):
            label = QLabel(text)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setProperty("role", "summary")
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            return label

        def responsive_form(group):
            layout = QFormLayout(group)
            layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            layout.setHorizontalSpacing(12)
            layout.setVerticalSpacing(8)
            return layout

        appearance = QGroupBox("界面外观")
        appearance_form = responsive_form(appearance)
        self.appearance = QComboBox()
        for label, value in theme.MODES:
            self.appearance.addItem(label, value)
        self.appearance.setToolTip("跟随系统：Windows 切换深浅色时，界面自动同步；点击保存后生效。")
        appearance_form.addRow("界面颜色", self.appearance)

        # -- sources ---------------------------------------------------
        gb_src = QGroupBox("搜索来源")
        v = QVBoxLayout(gb_src)
        v.addWidget(note("用于单曲、专辑搜索与备用下载；本地解密不依赖在线来源。"))
        self.source_boxes: "dict[str, QCheckBox]" = {}
        self.specs = all_specs(self.settings.to_dict())
        for spec in self.specs:
            cb = QCheckBox(spec.label)
            if spec.license_note:
                cb.setToolTip(spec.license_note)
            self.source_boxes[spec.id] = cb
            v.addWidget(cb)

        # -- network / generic ----------------------------------------
        gb_net = QGroupBox("网络与接口（通常无需修改）")
        form = responsive_form(gb_net)
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
        self.agg_base.setMinimumWidth(0)
        self.agg_base.setPlaceholderText("留空使用默认接口")
        self.agg_provider.setMinimumWidth(0)
        self.agg_provider.setPlaceholderText("netease")
        self.timeout.setToolTip("每次网络请求的超时上限。")
        self.retries.setToolTip("请求失败后的重试次数。")
        self.min_bitrate.setToolTip("下载候选的最低码率要求；0 表示不限制。")
        form.addRow("请求超时", self.timeout)
        form.addRow("重试次数", self.retries)
        form.addRow("最低码率", self.min_bitrate)
        form.addRow("聚合接口地址", self.agg_base)
        form.addRow("聚合默认平台", self.agg_provider)

        # -- QQ 凭证（按需静默获取 ekey）--------------------------------
        gb_ekey = QGroupBox("QQ 音乐账号配置")
        gb_ekey.setCheckable(True)          # 保留原开关语义：未配置时禁用编辑区。
        gb_ekey.setChecked(False)
        self.gb_ekey = gb_ekey
        ef = responsive_form(gb_ekey)
        self.qq_uin = QLineEdit()
        self.qq_uin.setPlaceholderText("你的 QQ 号")
        self.qq_key = QLineEdit()
        self.qq_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.qq_key.setPlaceholderText("qqmusic_key（整条 Cookie 直接粘贴也行）")
        self.qq_uin.setMinimumWidth(0)
        self.qq_key.setMinimumWidth(0)
        self.qq_key.setToolTip("仅保存在本机 settings.json，用于官方取钥匙接口；留空关闭。")
        ef.addRow("QQ 号", self.qq_uin)
        ef.addRow("会话 Cookie", self.qq_key)
        btn_row = QHBoxLayout()
        btn_paste = QPushButton("从剪贴板粘贴 Cookie")
        btn_clear = QPushButton("清空")
        btn_clear.setProperty("role", "quiet")
        btn_paste.clicked.connect(self._paste_cookie)
        btn_clear.clicked.connect(self._clear_cookie)
        btn_row.addWidget(btn_paste)
        btn_row.addWidget(btn_clear)
        btn_row.addStretch(1)
        ef.addRow(btn_row)
        self.qq_feedback = note("")
        ef.addRow(self.qq_feedback)
        hint = note("可选：用于新版加密文件自动取钥匙。Cookie 仅存本机，日志不会输出；"
                    "改密或退出登录后可能失效。留空即关闭，不影响其他功能。")
        ef.addRow(hint)

        # -- local dirs ------------------------------------------------
        gb_local = QGroupBox("本地曲库")
        lv = QVBoxLayout(gb_local)
        lv.addWidget(note("可选目录，用于优先查找电脑里已有的歌曲。"))
        self.local_list = QListWidget()
        self.local_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.local_list.setMinimumHeight(88)          # keeps the form compact
        self.local_list.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.local_list.setToolTip("本地曲库的完整目录；支持多选移除。")
        lv.addWidget(self.local_list)
        row = QHBoxLayout()
        add = QPushButton("添加目录…")
        rm = QPushButton("移除所选")
        rm.setProperty("role", "quiet")
        add.clicked.connect(self._add_local_dir)
        rm.clicked.connect(self._remove_local_dirs)
        row.addWidget(add)
        row.addWidget(rm)
        row.addStretch(1)
        lv.addLayout(row)

        # -- behaviour -------------------------------------------------
        gb_beh = QGroupBox("处理方式")
        bv = QVBoxLayout(gb_beh)
        self.chk_fallback = QCheckBox("转换失败时，自动查找同一首歌")
        self.chk_meta = QCheckBox("保存歌名、歌手、专辑和封面信息")
        self.chk_cover = QCheckBox("在线补齐封面")
        self.chk_overwrite = QCheckBox("同名文件直接覆盖（否则自动加序号）")
        self.chk_keep = QCheckBox("保留原始加密文件")
        self.chk_cache = QCheckBox("跳过已经处理过的文件")
        self.chk_auto = QCheckBox("添加文件后自动开始")
        self.chk_fast = QCheckBox("极速模式（减少多来源交叉核对）")
        self.chk_fast.setToolTip("开启后取歌更快，但不再用其它平台的时长互相印证；\n"
                                 "默认关闭 = 精确优先。歌名/歌手门槛与下载校验不受影响。")
        for cb in (self.chk_auto, self.chk_fallback, self.chk_fast):
            bv.addWidget(cb)
        gb_files = QGroupBox("文件保留与重复处理")
        files_layout = QVBoxLayout(gb_files)
        for cb in (self.chk_keep, self.chk_cache, self.chk_overwrite):
            files_layout.addWidget(cb)
        gb_tags = QGroupBox("歌曲信息")
        tags_layout = QVBoxLayout(gb_tags)
        for cb in (self.chk_meta, self.chk_cover):
            tags_layout.addWidget(cb)

        self.tabs = QTabWidget()
        self.tabs.setUsesScrollButtons(True)
        for title, groups in (("常用设置", (appearance, gb_beh, gb_files, gb_tags)),
                              ("账号配置", (gb_ekey,)),
                              ("高级设置", (gb_src, gb_net, gb_local))):
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(14, 12, 14, 12)
            layout.setSpacing(10)
            for group in groups:
                layout.addWidget(group)
            layout.addStretch(1)
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.Shape.NoFrame)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            area.setWidget(page)
            self.tabs.addTab(area, title)
        root.addWidget(self.tabs, 1)

        # ---- pinned footer (always visible) --------------------------
        footer = QWidget()
        footer.setProperty("role", "toolbar")
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
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存设置")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setProperty("role", "primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
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
        self.appearance.setCurrentIndex(self.appearance.findData(theme.preference()))
        # 勾选状态直接反映"当前实际生效的源"（含清单默认开关）
        active = {spec.id for spec in load_pack(s.to_dict())}
        for name, cb in self.source_boxes.items():
            cb.setChecked(name in active)
        self.timeout.setValue(float(s.timeout))
        self.retries.setValue(int(s.retries))
        self.min_bitrate.setValue(int(s.min_bitrate))
        self.agg_base.setText(s.aggregator_base)
        self.agg_provider.setText(s.aggregator_provider)
        self.qq_uin.setText(s.qqmusic_uin or "")
        self.qq_key.setText(s.qqmusic_key or "")
        self.gb_ekey.setChecked(bool(s.qqmusic_key))   # 已配置则展开，便于管理员查看
        self.chk_auto.setChecked(bool(getattr(s, "auto_start", True)))
        self.chk_fast.setChecked(bool(getattr(s, "fast_mode", False)))
        self.local_list.addItems(list(s.local_dirs or []))
        self.chk_fallback.setChecked(s.enable_fallback)
        self.chk_meta.setChecked(s.write_metadata)
        self.chk_cover.setChecked(s.fetch_cover)
        self.chk_overwrite.setChecked(s.overwrite)
        self.chk_keep.setChecked(s.keep_original)
        self.chk_cache.setChecked(s.remember_hashes)

    # ------------------------------------------------------------------ #
    def _paste_cookie(self) -> None:
        """从剪贴板导入：整条 Cookie 或只有 qqmusic_key 都认（自动提取 QQ 号）。"""
        from PySide6.QtWidgets import QApplication  # 局部导入，避免动 import 区
        raw = (QApplication.clipboard().text() or "").strip()
        if not raw:
            self.qq_feedback.setText("剪贴板是空的")
            return
        self.gb_ekey.setChecked(True)
        if "=" in raw:                      # 整条 Cookie
            self.qq_key.setText(raw)
            for part in raw.split(";"):
                name, _, value = part.strip().partition("=")
                if name.strip().lower() in ("qqmusic_uin", "uin") and value.strip():
                    self.qq_uin.setText(value.strip())
            self.qq_feedback.setText("已粘贴整条 Cookie（保存后生效）")
        else:
            self.qq_key.setText(raw)
            self.qq_feedback.setText("已粘贴 qqmusic_key（保存后生效）")

    def _clear_cookie(self) -> None:
        self.qq_uin.clear()
        self.qq_key.clear()
        self.qq_feedback.setText("已清空（保存后自动获取 ekey 关闭）")

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
        settings.qqmusic_uin = self.qq_uin.text().strip()
        settings.qqmusic_key = self.qq_key.text().strip()
        settings.auto_start = self.chk_auto.isChecked()
        settings.fast_mode = self.chk_fast.isChecked()
        settings.local_dirs = [self.local_list.item(i).text() for i in range(self.local_list.count())]
        settings.enable_fallback = self.chk_fallback.isChecked()
        settings.write_metadata = self.chk_meta.isChecked()
        settings.fetch_cover = self.chk_cover.isChecked()
        settings.overwrite = self.chk_overwrite.isChecked()
        settings.keep_original = self.chk_keep.isChecked()
        settings.remember_hashes = self.chk_cache.isChecked()

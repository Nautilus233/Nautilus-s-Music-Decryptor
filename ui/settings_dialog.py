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
                               QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget)

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

        # ---- scrollable body ----------------------------------------
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(14, 12, 14, 12)
        body.setSpacing(10)

        heading = QLabel("设置")
        heading.setProperty("role", "title")
        body.addWidget(heading)
        appearance = QGroupBox("外观")
        appearance_form = QFormLayout(appearance)
        self.appearance = QComboBox()
        for label, value in theme.MODES:
            self.appearance.addItem(label, value)
        self.appearance.setToolTip("跟随系统：Windows 切换深浅色时，界面自动同步；点击保存后生效。")
        appearance_form.addRow("界面颜色", self.appearance)
        body.addWidget(appearance)

        # -- sources ---------------------------------------------------
        gb_src = QGroupBox("搜索来源")
        v = QVBoxLayout(gb_src)
        self.source_boxes: "dict[str, QCheckBox]" = {}
        self.specs = all_specs(self.settings.to_dict())
        for spec in self.specs:
            cb = QCheckBox(spec.label)
            if spec.license_note:
                cb.setToolTip(spec.license_note)
            self.source_boxes[spec.id] = cb
            v.addWidget(cb)
        body.addWidget(gb_src)

        # -- network / generic ----------------------------------------
        gb_net = QGroupBox("网络与接口（通常无需修改）")
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

        # -- QQ 凭证（按需静默获取 ekey）--------------------------------
        gb_ekey = QGroupBox("QQ 音乐账号配置")
        gb_ekey.setCheckable(True)          # 折叠：同事默认看不到内容，也不会误改
        gb_ekey.setChecked(False)
        self.gb_ekey = gb_ekey
        ef = QFormLayout(gb_ekey)
        self.qq_uin = QLineEdit()
        self.qq_uin.setPlaceholderText("你的 QQ 号")
        self.qq_key = QLineEdit()
        self.qq_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.qq_key.setPlaceholderText("qqmusic_key（整条 Cookie 直接粘贴也行）")
        ef.addRow("QQ 号", self.qq_uin)
        ef.addRow("会话 Cookie", self.qq_key)
        btn_row = QHBoxLayout()
        btn_paste = QPushButton("从剪贴板粘贴 Cookie")
        btn_clear = QPushButton("清空")
        btn_paste.clicked.connect(self._paste_cookie)
        btn_clear.clicked.connect(self._clear_cookie)
        btn_row.addWidget(btn_paste)
        btn_row.addWidget(btn_clear)
        btn_row.addStretch(1)
        ef.addRow(btn_row)
        self.qq_feedback = QLabel("")
        self.qq_feedback.setProperty("role", "muted")
        ef.addRow(self.qq_feedback)
        hint = QLabel("配置一次即可：之后拖入新版加密歌曲会自动取钥匙，无需再设置。Cookie 仅存本机、"
                      "日志不会输出；等价于登录凭证，改密/退出登录即失效。留空即关闭该功能。")
        hint.setProperty("role", "muted")
        hint.setWordWrap(True)
        ef.addRow(hint)
        body.addWidget(gb_ekey)

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
        gb_beh = QGroupBox("日常使用")
        bv = QVBoxLayout(gb_beh)
        self.chk_fallback = QCheckBox("本地文件无法转换时，自动查找同一首歌")
        self.chk_meta = QCheckBox("保存歌名、歌手、专辑和封面信息")
        self.chk_cover = QCheckBox("在线补齐封面")
        self.chk_overwrite = QCheckBox("同名文件直接覆盖（否则自动加序号）")
        self.chk_keep = QCheckBox("保留原始加密文件")
        self.chk_cache = QCheckBox("跳过已经处理过的文件")
        self.chk_auto = QCheckBox("添加文件后自动开始")
        self.chk_fast = QCheckBox("极速模式（更快，减少多来源交叉核对）")
        self.chk_fast.setToolTip("开启后取歌更快，但不再用其它平台的时长互相印证；\n"
                                 "默认关闭 = 精确优先。歌名/歌手门槛与下载校验不受影响。")
        for cb in (self.chk_fallback, self.chk_meta, self.chk_cover,
                   self.chk_overwrite, self.chk_keep, self.chk_cache, self.chk_auto,
                   self.chk_fast):
            bv.addWidget(cb)
        body.addWidget(gb_beh)

        # Move existing controls into pages; their values and save callbacks stay intact.
        for group in (gb_src, gb_net, gb_ekey, gb_local, gb_beh):
            body.removeWidget(group)
        body.addWidget(gb_beh)
        body.addStretch(1)
        scroll.setWidget(content)
        self.tabs = QTabWidget()
        self.tabs.addTab(scroll, "常用设置")
        for title, groups in (("账号配置", (gb_ekey,)),
                              ("高级设置", (gb_src, gb_net, gb_local))):
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(14, 12, 14, 12)
            if title == "账号配置":
                help_text = QLabel("仅管理员需要配置。已有登录信息可继续使用，无需重复填写。")
                help_text.setWordWrap(True)
                help_text.setProperty("role", "muted")
                layout.addWidget(help_text)
            for group in groups:
                layout.addWidget(group)
            layout.addStretch(1)
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.Shape.NoFrame)
            area.setWidget(page)
            self.tabs.addTab(area, title)
        root.addWidget(self.tabs, 1)

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
            self.qq_feedback.setText(f"✓ 已粘贴整条 Cookie（末尾 …{raw[-12:]}）")
        else:
            self.qq_key.setText(raw)
            self.qq_feedback.setText(f"✓ 已粘贴 qqmusic_key（末尾 …{raw[-8:]}）")

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

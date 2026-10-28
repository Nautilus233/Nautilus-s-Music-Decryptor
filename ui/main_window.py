"""The main application window."""
from __future__ import annotations

from html import escape
from pathlib import Path

from PySide6.QtCore import QRect, QSettings, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (QColor, QDesktopServices, QDragEnterEvent,
                           QDropEvent, QIcon, QPainter)
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QApplication, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton,
                               QSizePolicy,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from mmusic import __version__
from mmusic.config import AppConfig
from mmusic.constants import QMC_ALL_EXTS
from mmusic.paths import resource_path

from . import geometry, theme
from .manual_links_dialog import ManualLinksDialog
from .settings_dialog import SettingsDialog
from .worker import DecryptWorker

SUPPORTED_EXT = ({".ncm"} | {"." + e for e in QMC_ALL_EXTS}
                 | {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".aac", ".ape", ".wma", ".dff", ".opus"})

_STATUS = {
    "decoded":    ("已解密", "#238054"),
    "downloaded": ("备用下载", "#397ac2"),
    "copied":     ("已复制", "#238054"),
    "skipped":    ("已跳过", "#8b93a7"),
    "failed":     ("失败", "#ef5350"),
    "pending":    ("等待", "#8b93a7"),
    "working":    ("处理中", "#f0a83c"),
}


class _SummaryLabel(QLabel):
    """One bounded line; full status stays available even in a narrow window."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.setProperty("role", "summary")
        self.setText(text)

    def setText(self, text):  # noqa: N802
        super().setText(text)
        self.setToolTip("<qt>" + escape(text) + "</qt>")
        self.setAccessibleDescription(text)

    def sizeHint(self):  # noqa: N802
        return QSize(160, self.fontMetrics().height() + 4)

    def minimumSizeHint(self):  # noqa: N802
        return QSize(0, self.fontMetrics().height() + 4)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setPen(self.palette().windowText().color())
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight,
                                           self.contentsRect().width())
        painter.drawText(self.contentsRect(), Qt.AlignmentFlag.AlignLeft
                         | Qt.AlignmentFlag.AlignVCenter, text)


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig, initial_paths=None) -> None:
        super().__init__()
        self.config = config
        self.settings = config.settings
        self._rows: "dict[str, int]" = {}
        self._worker: "DecryptWorker | None" = None
        # 日志分级状态：全部行 (文本, level) + 被折叠的明细行数
        self._log_lines: "list[tuple[str, str]]" = []
        self._hidden_details = 0
        #: 跑完但"线程还没真正结束"的 worker：必须保住强引用直到 QThread.finished，
        #: 否则 Python GC 掉 C++ 对象会触发原生崩溃（见 _retire_worker）。
        self._retired: "list" = []

        self.setWindowTitle(f"音乐解密工具 · Music Decryptor v{__version__}")
        icon = resource_path("assets", "icon.ico")
        if icon.is_file():
            self.setWindowIcon(QIcon(str(icon)))
        # every control must stay reachable even on a short screen / tall
        # taskbar; the final size is applied in showEvent(), where the window
        # frame (title bar/borders) is known and can be kept fully on-screen.
        self.setMinimumSize(820, 500)
        self._geometry_ready = False
        self._compact_layout = None
        self.setAcceptDrops(True)

        self._build_ui()
        self._load_settings_into_ui()
        if initial_paths:
            self.add_paths(initial_paths)

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(6)

        # header
        header = QHBoxLayout()
        head = QVBoxLayout()
        head.setSpacing(2)
        title = QLabel("音乐工作台")
        title.setProperty("role", "title")
        subtitle = QLabel("转换已有文件，或按歌名、专辑获取音乐。")
        subtitle.setProperty("role", "subtitle")
        head.addWidget(title)
        head.addWidget(subtitle)
        header.addLayout(head, 1)
        self.btn_manual = QPushButton("提供音频链接…")
        self.btn_manual.setProperty("role", "quiet")
        self.btn_manual.setToolTip("高级操作：使用自己提供的链接或本地路径，优先于自动搜索")
        self.btn_settings = QPushButton("设置")
        header.addWidget(self.btn_manual)
        header.addWidget(self.btn_settings)
        outer.addLayout(header)

        navigation = QHBoxLayout()
        current = QPushButton("本地文件\n转换电脑里的音频或加密文件")
        current.setProperty("role", "current")
        current.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        current.clicked.connect(self._focus_file_list)
        self.btn_name_search = QPushButton("单曲下载\n输入歌名，自动查找音乐")
        self.btn_album_search = QPushButton("专辑下载\n选择专辑，整张或按曲下载")
        for button in (self.btn_name_search, self.btn_album_search):
            button.setProperty("role", "navigation")
        for button in (current, self.btn_name_search, self.btn_album_search):
            navigation.addWidget(button, 1)
        outer.addLayout(navigation)

        # Shared output preferences are secondary to the task entry points.
        output_tools = QFrame()
        output_tools.setProperty("role", "toolbar")
        options = QHBoxLayout(output_tools)
        options.setContentsMargins(10, 6, 10, 6)
        options.setSpacing(8)
        options.addWidget(QLabel("输出格式"))
        self.quality = QComboBox()
        self.quality.addItem("MP3 · 通用播放", "mp3")
        self.quality.addItem("FLAC · 优先无损", "flac")
        self.quality.addItem("WAV · 优先无损", "wav")
        self.quality.setMinimumContentsLength(11)
        self.quality.setToolTip("找不到无损源时保留有损格式，不进行无效升格；三个模式共用此选项。")
        options.addWidget(self.quality)
        options.addWidget(QLabel("保存到"))
        self.out_dir = QLineEdit()
        self.out_dir.setMinimumWidth(0)
        self.out_dir.setPlaceholderText("原文件所在文件夹（默认）")
        self.out_dir.setToolTip("留空时，本地文件保存在原文件夹；单曲和专辑下载沿用各自默认目录。")
        options.addWidget(self.out_dir, 1)
        self.btn_out_dir = QPushButton("更改位置…")
        self.btn_open_out = QPushButton("打开文件夹")
        self.btn_open_out.setProperty("role", "quiet")
        self.btn_open_out.setToolTip("打开上次输出或设置的文件夹")
        options.addWidget(self.btn_out_dir)
        options.addWidget(self.btn_open_out)
        outer.addWidget(output_tools)

        # toolbar
        bar = QHBoxLayout()
        bar.setSpacing(8)
        queue_title = QLabel("本地文件")
        queue_title.setProperty("role", "section")
        bar.addWidget(queue_title)
        self.queue_count = QLabel("0 个文件")
        self.queue_count.setProperty("role", "badge")
        bar.addWidget(self.queue_count)
        bar.addStretch(1)
        self.btn_add_files = QPushButton("添加文件")
        self.btn_add_dir = QPushButton("添加文件夹")
        self.btn_remove = QPushButton("移除所选")
        self.btn_clear = QPushButton("清空列表")
        self.btn_remove.setProperty("role", "quiet")
        self.btn_clear.setProperty("role", "quiet")
        for b in (self.btn_add_files, self.btn_add_dir, self.btn_remove, self.btn_clear):
            bar.addWidget(b)
        outer.addLayout(bar)

        # table
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["文件名", "状态", "格式", "输出文件", "说明"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.verticalHeader().setMinimumSectionSize(32)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setMinimumSectionSize(52)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 88)
        self.table.setColumnWidth(2, 64)
        for col, tip in enumerate(("原始文件名；悬停查看完整路径", "当前文件的处理状态",
                                   "实际输出格式", "输出文件名；悬停查看完整路径",
                                   "处理说明；悬停查看完整内容")):
            self.table.horizontalHeaderItem(col).setToolTip(tip)
        self.table.setMinimumHeight(100)
        outer.addWidget(self.table, 5)

        # An overlay leaves the empty queue the same size as the populated one.
        empty_layout = QVBoxLayout(self.table.viewport())
        empty_layout.setContentsMargins(12, 0, 12, 0)
        self.empty_state = QLabel("拖入文件或文件夹，或点击「添加文件」\n支持 NCM、QMC 及常见音频格式")
        self.empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_state.setProperty("role", "subtitle")
        self.empty_state.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        empty_layout.addWidget(self.empty_state)
        self.table.model().rowsInserted.connect(self._update_queue_presentation)
        self.table.model().rowsRemoved.connect(self._update_queue_presentation)
        self.table.model().modelReset.connect(self._update_queue_presentation)

        # progress
        self.status_label = _SummaryLabel("等待添加文件")
        self.status_label.setAccessibleName("处理状态")
        outer.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        # log（默认只显示"结论"，明细收进「显示详细信息」）
        log_row = QHBoxLayout()
        log_title = QLabel("处理结果")
        log_title.setProperty("role", "section")
        log_row.addWidget(log_title)
        self.chk_detail = QCheckBox("查看处理详情")
        self.chk_detail.setToolTip("展开每个音源的排除统计、时长探测、降权说明等过程明细")
        self.chk_detail.toggled.connect(self._on_detail_toggled)
        log_row.addWidget(self.chk_detail)
        log_row.addStretch(1)
        self.log_hint = _SummaryLabel()
        log_row.addWidget(self.log_hint, 1)
        outer.addLayout(log_row)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(4000)
        self.log.setPlaceholderText("每个文件的处理结果会显示在这里。")
        self.log.setMinimumHeight(60)
        self.log.setMaximumHeight(96)
        outer.addWidget(self.log, 1)

        # actions
        act = QHBoxLayout()
        act.addWidget(self.progress, 1)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setProperty("role", "danger")
        self.btn_stop.setEnabled(False)
        self.btn_start = QPushButton("开始处理")
        self.btn_start.setProperty("role", "primary")
        self.btn_start.setMinimumWidth(150)
        act.addWidget(self.btn_stop)
        act.addWidget(self.btn_start)
        outer.addLayout(act)

        # wiring
        self.btn_add_files.clicked.connect(self._pick_files)
        self.btn_add_dir.clicked.connect(self._pick_dir)
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_clear.clicked.connect(self._clear)
        self.btn_out_dir.clicked.connect(self._pick_out_dir)
        self.btn_settings.clicked.connect(self._open_settings)
        self.btn_manual.clicked.connect(self._open_manual_links)
        self.btn_name_search.clicked.connect(self._open_name_search)
        self.btn_album_search.clicked.connect(self._open_album_search)
        self.btn_start.clicked.connect(self.start_job)
        self.btn_open_out.clicked.connect(self._open_out_dir)
        self.btn_stop.clicked.connect(self.stop_job)

    def _update_queue_presentation(self, *_args) -> None:
        count = self.table.rowCount()
        self.queue_count.setText(f"{count} 个文件")
        self.empty_state.setVisible(count == 0)

    # ------------------------------------------------------------------ #
    def _load_settings_into_ui(self) -> None:
        idx = self.quality.findData(self.settings.quality)
        self.quality.setCurrentIndex(idx if idx >= 0 else 0)
        self.out_dir.setText(self.settings.output_dir or "")

    def _sync_settings_from_ui(self) -> None:
        self.settings.quality = self.quality.currentData()
        self.settings.output_dir = self.out_dir.text().strip()
        self.config.save()

    # ------------------------------------------------------------------ #
    def _available_geometry(self) -> QRect:
        """Work area of the current screen (taskbar excluded)."""
        return geometry.available_geometry(self)

    def _fit_to_screen(self) -> None:
        geometry.fit(self, preferred=(1080, 760),
                     minimum=(self.minimumWidth(), self.minimumHeight()))

    def _clamp_to_screen(self) -> None:
        geometry.clamp(self)

    # -- window geometry persistence ------------------------------------ #
    ORG_NAME = "MusicDecryptor"
    APP_NAME = "MusicDecryptor"

    @classmethod
    def _settings_store(cls) -> QSettings:
        return QSettings(cls.ORG_NAME, cls.APP_NAME)

    def _restore_geometry(self) -> bool:
        try:
            saved = self._settings_store().value("geometry")
        except Exception:  # noqa: BLE001
            return False
        if saved is None or not self.restoreGeometry(saved):
            return False
        self._clamp_to_screen()          # a saved size must never clip the UI
        return True

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._geometry_ready:
            self._geometry_ready = True
            if not self._restore_geometry():
                self._fit_to_screen()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        compact = self.height() < 620
        if compact != self._compact_layout:
            self._compact_layout = compact
            layout = self.centralWidget().layout()
            layout.setContentsMargins(12 if compact else 16, 8 if compact else 12,
                                      12 if compact else 16, 8 if compact else 12)
            layout.setSpacing(4 if compact else 6)

    def _focus_file_list(self) -> None:
        self.table.setFocus()

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self._settings_store().setValue("geometry", self.saveGeometry())
        except Exception:  # noqa: BLE001
            pass
        # 关窗时必须等线程收尾（协作式取消 + 有界等待）：线程活过窗口/进程，
        # 既可能崩在解释器退出，也会让"已写了一半的产物"停在不确定状态。
        pending = [w for w in ([self._worker] + list(self._retired)) if w is not None]
        for w in pending:
            try:
                w.cancel()
                if w.isRunning():
                    w.wait(5000)
            except Exception:  # noqa: BLE001
                pass
        for w in pending:
            self._release_worker(w)
        self._worker = None
        super().closeEvent(event)

    # ------------------------------------------------------------------ #
    def add_paths(self, paths) -> None:
        added = 0
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                for child in sorted(p.rglob("*")):
                    if child.is_file() and child.suffix.lower() in SUPPORTED_EXT:
                        added += self._add_row(child) or 0
            elif p.is_file():
                added += self._add_row(p) or 0
        if added:
            self._log(f"➕ 已添加 {added} 个文件（共 {self.table.rowCount()} 个）")
            # 简化：窗口可见时“拖入即自动开始”（同事零操作）；可在 设置→行为 关闭。
            # 启动时带路径（__init__ / --auto）不在此触发，避免与 --auto 重复。
            if (getattr(self.settings, "auto_start", True) and self.isVisible()
                    and not (self._worker is not None and self._worker.isRunning())):
                QTimer.singleShot(0, self.start_job)

    def _add_row(self, path: Path) -> int:
        key = str(path)
        if key in self._rows:
            return 0
        row = self.table.rowCount()
        self.table.insertRow(row)
        name_item = QTableWidgetItem(path.name)
        name_item.setToolTip("<qt>" + escape(key) + "</qt>")
        name_item.setData(Qt.ItemDataRole.UserRole, key)
        self.table.setItem(row, 0, name_item)
        self._set_cell(row, 1, "pending")
        self.table.setItem(row, 2, QTableWidgetItem(""))
        self.table.setItem(row, 3, QTableWidgetItem(""))
        self.table.setItem(row, 4, QTableWidgetItem(""))
        self._rows[key] = row
        return 1

    def _set_cell(self, row: int, col: int, status: str) -> None:
        text, color = _STATUS.get(status, (status, "#8b93a7"))
        item = QTableWidgetItem(text)
        item.setToolTip("<qt>" + escape(text) + "</qt>")
        item.setForeground(QColor(color))
        self.table.setItem(row, col, item)

    # ------------------------------------------------------------------ #
    def _pick_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择加密音频文件", "",
            "音频/加密文件 (*.ncm *.qmc* *.mgg*.mflac* *.bkc* *.tkm *.mmp4 *.mp3 *.flac *.ogg *.m4a *.wav);;所有文件 (*.*)")
        if files:
            self.add_paths(files)

    def _pick_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if d:
            self.add_paths([d])

    def _pick_out_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self.out_dir.setText(d)

    def _remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            key = self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
            self._rows.pop(key, None)
            self.table.removeRow(r)
        self._reindex()

    def _clear(self) -> None:
        self.table.setRowCount(0)
        self._rows.clear()

    def _reindex(self) -> None:
        self._rows.clear()
        for r in range(self.table.rowCount()):
            key = self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
            self._rows[key] = r

    # ------------------------------------------------------------------ #
    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            dlg.apply_to(self.settings)
            theme.save_preference(QApplication.instance(), dlg.appearance.currentData())
            self.config.save()
            self._load_settings_into_ui()
            self._log("⚙ 设置已保存")

    def _open_manual_links(self) -> None:
        dlg = ManualLinksDialog(self.settings.manual_links, self)
        if not dlg.exec():
            return
        self.settings.manual_links = dlg.entries()
        self.config.save()
        n = len(self.settings.manual_links)
        if n:
            self._log(f"🔗 已保存 {n} 条补链；下次「开始处理」时会**优先**用它们"
                      f"（跳过歌名/歌手门槛，其余校验照旧）")
        else:
            self._log("🔗 已清空补链列表")

    def _open_name_search(self) -> None:
        """「以名找歌」：不需要加密文件，直接按歌名走备用音源链路。"""
        from .name_search_dialog import NameSearchDialog

        self._sync_settings_from_ui()          # 音质 / 输出目录以主界面当前值为准
        dlg = NameSearchDialog(self.settings, self.config,
                               on_sync=self._sync_settings_from_ui, parent=self)
        dlg.exec()
        # 对话框里可能改过输出目录（或写进了产物）：回到主界面时刷新一下
        self._load_settings_into_ui()

    def _open_album_search(self) -> None:
        """「按专辑找歌」：按专辑检索 → 取曲目清单 → 下载一首/多首/整张。"""
        from .album_search_dialog import AlbumSearchDialog

        self._sync_settings_from_ui()          # 音质 / 输出目录以主界面当前值为准
        dlg = AlbumSearchDialog(self.settings, self.config,
                                on_sync=self._sync_settings_from_ui, parent=self)
        dlg.exec()
        self._load_settings_into_ui()

    # ------------------------------------------------------------------ #
    def start_job(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        if self.table.rowCount() == 0:
            QMessageBox.information(self, "提示", "请先添加要处理的文件。")
            return
        self._sync_settings_from_ui()
        paths = list(self._rows.keys())
        for r in range(self.table.rowCount()):
            self._set_cell(r, 1, "pending")
            self.table.item(r, 3).setText("")
            self.table.item(r, 4).setText("")
        self.progress.setValue(0)
        self.progress.setRange(0, max(1, len(paths)))
        self._log("=" * 46, level="detail")
        self._log(f"▶ 开始处理 {len(paths)} 个文件 | 音质：{self.settings.quality.upper()} | "
                  f"备用下载：{'开' if self.settings.enable_fallback else '关'}")

        self._worker = DecryptWorker(self.settings, paths, self.config.cache_path, self)
        self._worker.logged.connect(self._log)
        self._worker.progressed.connect(self._on_progress)
        self._worker.file_done.connect(self._on_file_done)
        self._worker.completed.connect(self._on_completed)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_add_files.setEnabled(False)
        self.btn_add_dir.setEnabled(False)

    def stop_job(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._log("⏹ 正在停止…")

    def _on_progress(self, done: int, total: int) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)
        self.status_label.setText(f"进度 {done}/{total}")

    def _on_file_done(self, res) -> None:
        row = self._rows.get(res.path)
        if row is None:
            return
        self._set_cell(row, 1, res.status)
        self.table.item(row, 2).setText((res.fmt or "").upper())
        self.table.item(row, 3).setText(Path(res.out_path).name if res.out_path else "")
        self.table.item(row, 4).setText(res.message + (f" · 来源 {res.source}" if res.source and res.status == "downloaded" else ""))
        self.table.item(row, 2).setToolTip("<qt>" + escape(self.table.item(row, 2).text()) + "</qt>")
        self.table.item(row, 3).setToolTip("<qt>" + escape(res.out_path or "") + "</qt>")
        self.table.item(row, 4).setToolTip("<qt>" + escape(self.table.item(row, 4).text()) + "</qt>")
        self.table.scrollToItem(self.table.item(row, 0))
        if res.out_path:
            self._last_out_dir = str(Path(res.out_path).parent)
        if "重新粘贴" in (res.message or ""):
            self.status_label.setText("⚠ QQ 登录态可能已失效：请管理员到 设置 → QQ 音乐凭证 重新粘贴")

    def _retire_worker(self, worker) -> None:
        """回收 worker：先摘掉 `_worker`（UI 可立即开新任务），再等线程**真正结束**才销毁。

        为什么必须这样：`completed` / `failed` 是在 `run()` **内部**发出的 —— 那一刻
        线程尚未返回。旧代码在槽里直接 `self._worker = None`，等于把 QThread 的最后
        引用丢在"仍在运行"的瞬间：Python 一侧一 GC，C++ 对象先于线程结束被销毁。
        实测（2026-10-20 猴子测试）：跑完一次任务后退出进程 → 退出码 0xC0000409
        （STATUS_STACK_BUFFER_OVERRUN），offline 平台下必现 —— 真实用户表现为
        "关窗退出时偶尔弹崩溃"。
        """
        if worker is None:
            return
        self._worker = None
        try:
            worker.finished.connect(lambda w=worker: self._release_worker(w))
            self._retired.append(worker)
        except Exception:  # noqa: BLE001 - 回收失败也不能影响 UI
            pass

    def _release_worker(self, worker) -> None:
        try:
            if worker in self._retired:
                self._retired.remove(worker)
        except Exception:  # noqa: BLE001
            pass
        try:
            worker.deleteLater()
        except Exception:  # noqa: BLE001
            pass

    def _on_completed(self, results) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_add_files.setEnabled(True)
        self.btn_add_dir.setEnabled(True)
        self.status_label.setText("完成")
        ok = sum(1 for r in results if r.ok)
        fail = sum(1 for r in results if r.status == "failed")
        skip = sum(1 for r in results if r.status == "skipped")
        self._log(f"■ 全部完成：成功 {ok} · 跳过 {skip} · 失败 {fail}")
        waiting = sum(1 for r in range(self.table.rowCount())
                      if self.table.item(r, 1) is not None
                      and self.table.item(r, 1).text() == "等待")
        if waiting:
            self._log(f"⏳ 列表里还有 {waiting} 个待处理文件：点「开始处理」即可继续")
        self._retire_worker(self._worker)

    def _on_failed(self, message: str) -> None:
        self._log(f"❌ 任务异常：{message}")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_add_files.setEnabled(True)
        self.btn_add_dir.setEnabled(True)
        self._retire_worker(self._worker)

    # ------------------------------------------------------------------ #
    # 日志分级：`result`（结论，同事必看）恒显示；`detail`（过程明细）默认折叠。
    def _log(self, message: str, level: str = "result") -> None:
        self._log_lines.append((message, level))
        if len(self._log_lines) > 4000:
            del self._log_lines[:len(self._log_lines) - 4000]
        if level == "result" or self.chk_detail.isChecked():
            self.log.appendPlainText(message)
        else:
            self._hidden_details += 1
        self._update_log_hint()

    def _on_detail_toggled(self, checked: bool) -> None:
        """切换「显示详细信息」：即时重渲染，不用重跑任务。"""
        if checked:
            self._hidden_details = 0
            self.log.setPlainText("\n".join(m for m, _ in self._log_lines))
        else:
            kept = [m for m, lv in self._log_lines if lv == "result"]
            self._hidden_details = len(self._log_lines) - len(kept)
            self.log.setPlainText("\n".join(kept))
        self._update_log_hint()

    def _update_log_hint(self) -> None:
        if self.chk_detail.isChecked() or not self._hidden_details:
            self.log_hint.setText("")
        else:
            self.log_hint.setText(f"已折叠 {self._hidden_details} 行详细过程")

    def _open_out_dir(self) -> None:
        """在文件管理器中打开上次输出（或设置里指定）的目录。"""
        target = self.settings.output_dir or getattr(self, "_last_out_dir", "")
        if not target and self._rows:
            target = str(Path(next(iter(self._rows))).parent)
        if not target or not Path(target).is_dir():
            self._log("📂 还没有可打开的输出目录（先处理一个文件，或在设置里指定输出目录）")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(target))))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.add_paths(paths)
            event.acceptProposedAction()

"""「按专辑找歌」对话框：按专辑检索（可加歌手限定）→ 取曲目清单 → 下载一首/多首/整张。

用法：
1. 填专辑名（必需，可加"歌手"做双重限定）→「检索专辑」；
2. 列表里选中一张 →「获取曲目」（咪咕专辑会自动合并**参考歌单**并对缺失曲目做
   补位搜索；补位命中会把曲名/歌手/专辑**校正**后写入文件标签与文件名）；
3. 勾选想下载的曲目 →「下载选中」；或直接「下载全部」。

纪律（与「以名找歌」完全一致）：
* 下载走的仍是既有 `Pipeline.download_chosen`：门槛、完整度门禁、写盘一字不变；
* 后台线程生命周期沿用同一套（completed 在 run() 内发出 → 宿主保引用、等
  finished 再释放；关窗前有界等待）；
* 一首失败不拖垮整批；一切结论只在日志与列表里**如实标注**。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from mmusic.duration import format_seconds
from mmusic.match import sanitize_query_text
from mmusic.pipeline import SEARCH_PREFIX, default_search_dir

from . import geometry
from .worker import AlbumSearchWorker

MIN_SIZE = (620, 560)
PREFERRED_SIZE = (980, 780)
ALBUM_COLUMNS = ["专辑", "歌手", "曲目数", "年份"]   # 合并视图：不展示来源
TRACK_COLUMNS = ["选择", "歌名", "歌手", "时长", "备注", "状态"]

def clean_album_query(text: str) -> str:
    """专辑检索词清洗：复用输入审计的序号/说明前缀清洗（只动噪声，不动名称本身）。"""
    return sanitize_query_text(text or "").strip()


class AlbumSearchDialog(QDialog):
    def __init__(self, settings, config, *, on_sync=None, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.config = config
        self._on_sync = on_sync
        self._fitted = False
        self._worker = None
        self._retired: "list" = []
        self._albums: "list" = []
        self._tracks: "list" = []
        self._track_row: "dict[int, int]" = {}
        self._dl_rows: "list[int]" = []
        self._dl_cursor = 0
        self._current_album = None
        self._current_album_row = -1
        self._loaded_row = -1          # 已加载曲目的专辑行（用于 ✓ 标记复位）
        self._pending_row = -1         # 忙时挂起的"换专辑"请求
        self._auto_token = 0           # 防抖令牌：快速换行只有最后一次生效
        self._log_lines: "list[tuple[str, str]]" = []
        self._hidden_details = 0

        self.setWindowTitle("按专辑找歌 · 备用音源")
        self.setMinimumSize(*MIN_SIZE)
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        tip = QLabel(
            "按专辑检索（可加歌手双重限定；支持日文假名/罗马音）→ **单击专辑行即自动加载曲目**"
            "（可随时换选）→ 勾选下载，或一键下载整张。\n"
            "曲目表由**多来源并集**装配（同一版本内不丢曲目）；下载走与「开始解密」相同的校验与完整度门禁，"
            "封面与曲序一并写入；某个通道取不到时自动换下一条，全部失败才如实提示。")
        tip.setWordWrap(True)
        tip.setProperty("role", "muted")
        root.addWidget(tip)

        form = QFormLayout()
        form.setSpacing(6)
        self.name = QLineEdit()
        self.name.setPlaceholderText("十一月的萧邦 / Lemon / 残酷な天使のテーゼ…")
        form.addRow("专辑名", self.name)
        self.artist = QLineEdit()
        self.artist.setPlaceholderText("可选；填了会过滤掉演唱者不符的专辑（双重限定）")
        form.addRow("歌手", self.artist)
        root.addLayout(form)

        opts = QHBoxLayout()
        self.btn_search = QPushButton("检索专辑")
        self.btn_search.setProperty("role", "primary")
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        opts.addWidget(self.btn_search)
        opts.addWidget(self.btn_stop)
        opts.addStretch(1)
        self.btn_out_dir = QPushButton("输出目录…")
        self.btn_out_dir.setToolTip("给「以名/按专辑找歌」单独指定一个目录（与「开始解密」互不影响）")
        self.btn_follow_main = QPushButton("跟随主设置")
        self.btn_follow_main.setToolTip("清掉专用目录，改回：主输出目录 → 默认目录（按需按优）")
        self.btn_open_out = QPushButton("打开输出目录")
        opts.addWidget(self.btn_out_dir)
        opts.addWidget(self.btn_follow_main)
        opts.addWidget(self.btn_open_out)
        root.addLayout(opts)

        self.out_hint = QLabel("")
        self.out_hint.setProperty("role", "muted")
        self.out_hint.setWordWrap(True)
        root.addWidget(self.out_hint)

        head_a = QHBoxLayout()
        head_a.addWidget(QLabel("专辑（单击选中即自动加载曲目）"))
        head_a.addStretch(1)
        self.btn_fetch = QPushButton("获取曲目")
        self.btn_fetch.setToolTip("单击专辑行即自动加载；此按钮用于**强制重新获取**当前专辑")
        self.btn_fetch.setEnabled(False)
        head_a.addWidget(self.btn_fetch)
        root.addLayout(head_a)

        self.album_table = QTableWidget(0, len(ALBUM_COLUMNS))
        self.album_table.setHorizontalHeaderLabels(ALBUM_COLUMNS)
        self.album_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.album_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.album_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.album_table.verticalHeader().setVisible(False)
        self.album_table.setMinimumHeight(88)     # 小屏(768p)可压缩；大屏由布局自动放大
        root.addWidget(self.album_table, 1)

        head_t = QHBoxLayout()
        self.track_header = QLabel("曲目（搜索后选专辑，单击即自动加载）")
        head_t.addWidget(self.track_header)
        head_t.addStretch(1)
        self.btn_all = QPushButton("全选")
        self.btn_none = QPushButton("全不选")
        self.btn_dl = QPushButton("下载选中")
        self.btn_dl_all = QPushButton("下载全部")
        self.btn_dl.setProperty("role", "primary")
        for b in (self.btn_all, self.btn_none, self.btn_dl, self.btn_dl_all):
            b.setEnabled(False)
            head_t.addWidget(b)
        root.addLayout(head_t)

        self.track_table = QTableWidget(0, len(TRACK_COLUMNS))
        self.track_table.setHorizontalHeaderLabels(TRACK_COLUMNS)
        self.track_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.track_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.track_table.verticalHeader().setVisible(False)
        self.track_table.setMinimumHeight(120)
        root.addWidget(self.track_table, 1)

        log_row = QHBoxLayout()
        log_row.addWidget(QLabel("运行日志"))
        self.chk_detail = QCheckBox("显示详细信息")
        log_row.addWidget(self.chk_detail)
        log_row.addStretch(1)
        self.log_hint = QLabel("")
        self.log_hint.setProperty("role", "muted")
        log_row.addWidget(self.log_hint)
        root.addLayout(log_row)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(86)
        root.addWidget(self.log)

        footer = QHBoxLayout()
        self.status = QLabel("就绪")
        self.status.setProperty("role", "muted")
        footer.addWidget(self.status)
        footer.addStretch(1)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        box.clicked.connect(lambda *_: self.reject())
        footer.addWidget(box)
        root.addLayout(footer)

        self.btn_search.clicked.connect(self.search)
        self.btn_stop.clicked.connect(self.stop)
        self.btn_fetch.clicked.connect(self.fetch_tracks)
        self.btn_all.clicked.connect(lambda: self._check_all(True))
        self.btn_none.clicked.connect(lambda: self._check_all(False))
        self.btn_dl.clicked.connect(self.download_checked)
        self.btn_dl_all.clicked.connect(self.download_all)
        self.btn_out_dir.clicked.connect(self._pick_out_dir)
        self.btn_follow_main.clicked.connect(self._follow_main_out_dir)
        self.btn_open_out.clicked.connect(self._open_out_dir)
        self.chk_detail.toggled.connect(self._on_detail_toggled)
        self.album_table.itemSelectionChanged.connect(self._on_album_selected)
        self.album_table.doubleClicked.connect(lambda *_: self.fetch_tracks())

        self._refresh_out_hint()

    # ---------------- 生命周期 ---------------- #
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._fitted:
            self._fitted = True
            geometry.fit(self, preferred=PREFERRED_SIZE, minimum=MIN_SIZE,
                         center_on=self.parentWidget())

    def closeEvent(self, event) -> None:  # noqa: N802
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

    def _retire_worker(self, worker) -> None:
        if worker is None:
            return
        self._worker = None
        try:
            worker.finished.connect(lambda w=worker: self._release_worker(w))
            self._retired.append(worker)
        except Exception:  # noqa: BLE001
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

    # ---------------- 输出目录 ---------------- #
    def effective_out_dir(self) -> Path:
        search_dir = (getattr(self.settings, "search_output_dir", "") or "").strip()
        if search_dir:
            return Path(search_dir).expanduser()
        configured = (self.settings.output_dir or "").strip()
        return Path(configured).expanduser() if configured else default_search_dir()

    def _refresh_out_hint(self) -> None:
        search_dir = (getattr(self.settings, "search_output_dir", "") or "").strip()
        configured = (self.settings.output_dir or "").strip()
        target = self.effective_out_dir()
        if search_dir:
            self.out_hint.setText(f"输出目录（本模式专用）：{target}")
        elif configured:
            self.out_hint.setText(f"输出目录：跟随主输出目录 {target}（想分开就点「输出目录…」）")
        else:
            self.out_hint.setText(f"输出目录（默认）：{target}（可在上方按钮里改成专用目录）")

    def _pick_out_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择输出目录",
                                                  str(self.effective_out_dir()))
        if chosen:
            self.settings.search_output_dir = chosen
            try:
                self.config.save()
            except Exception:  # noqa: BLE001
                pass
            self._refresh_out_hint()

    def _follow_main_out_dir(self) -> None:
        self.settings.search_output_dir = ""
        try:
            self.config.save()
        except Exception:  # noqa: BLE001
            pass
        self._refresh_out_hint()

    def _open_out_dir(self) -> None:
        target = self.effective_out_dir()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    # ---------------- 日志 ---------------- #
    def _on_detail_toggled(self, checked: bool) -> None:
        if checked:
            self._hidden_details = 0
            self.log.setPlainText("\n".join(m for m, _ in self._log_lines))
        else:
            kept = [m for m, lv in self._log_lines if lv == "result"]
            self._hidden_details = len(self._log_lines) - len(kept)
            self.log.setPlainText("\n".join(kept))
        self._update_hint()

    def _update_hint(self) -> None:
        if self.chk_detail.isChecked() or not self._hidden_details:
            self.log_hint.setText("")
        else:
            self.log_hint.setText(f"已折叠 {self._hidden_details} 行明细")

    def _log(self, message: str, level: str = "result") -> None:
        self._log_lines.append((message, level))
        if len(self._log_lines) > 4000:
            del self._log_lines[: len(self._log_lines) - 4000]
        if level == "result" or self.chk_detail.isChecked():
            self.log.appendPlainText(message)
        else:
            self._hidden_details += 1
        self._update_hint()

    # ---------------- 列表 ---------------- #
    def _set_album_row(self, key: str, **cells) -> int:
        row = self.album_table.rowCount()
        self.album_table.insertRow(row)
        for c in range(len(ALBUM_COLUMNS)):
            self.album_table.setItem(row, c, QTableWidgetItem(""))
        for name, value in cells.items():
            self.album_table.item(row, ALBUM_COLUMNS.index(name)).setText(str(value))
        return row

    def _set_track_row(self, index: int, track) -> None:
        row = self.track_table.rowCount()
        self.track_table.insertRow(row)
        check = QTableWidgetItem("")
        check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                       | Qt.ItemFlag.ItemIsSelectable)
        check.setCheckState(Qt.CheckState.Checked)
        self.track_table.setItem(row, 0, check)
        for c in range(1, len(TRACK_COLUMNS)):
            self.track_table.setItem(row, c, QTableWidgetItem(""))
        note = ""
        self.track_table.item(row, 1).setText(track.title)
        self.track_table.item(row, 2).setText(track.artist)
        self.track_table.item(row, 3).setText(format_seconds(track.duration) if track.duration else "")
        self.track_table.item(row, 4).setText(note)
        self.track_table.item(row, 5).setText("待下载")
        self._track_row[id(track)] = row

    def _check_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.track_table.rowCount()):
            item = self.track_table.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def _checked_tracks(self) -> "list":
        out = []
        for row, track in enumerate(self._tracks):
            item = self.track_table.item(row, 0) if row < self.track_table.rowCount() else None
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                out.append(track)
        return out

    # ---------------- 跑任务 ---------------- #
    def _mark_loaded(self, row: int, album, count: int) -> None:
        """给已加载的专辑行打 ✓（旧行复位），并在曲目区标题写明"当前是哪张专辑"。"""
        if 0 <= self._loaded_row < self.album_table.rowCount() \
                and self._loaded_row != row and 0 <= self._loaded_row < len(self._albums):
            old_item = self.album_table.item(self._loaded_row, 0)
            if old_item is not None:
                old_item.setText(self._albums[self._loaded_row].name)
                old_item.setToolTip("")
        self._loaded_row = row
        if album is not None and count and 0 <= row < self.album_table.rowCount():
            item = self.album_table.item(row, 0)
            if item is not None:
                item.setText(f"{album.name} ✓")
                item.setToolTip(f"已加载 {count} 首曲目（换选其它专辑会自动切换）")
            self.track_header.setText(
                f"曲目 · 当前：{album.name}（{count} 首；勾选要下载的）")
        elif album is not None and count == 0:
            self.track_header.setText(
                f"曲目 · 当前：{album.name}（未取到曲目）")

    def _refresh_fetch_button(self) -> None:
        has_pick = 0 <= self.album_table.currentRow() < len(self._albums)
        self.btn_fetch.setEnabled(has_pick and not self._busy())

    def _busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _set_busy(self, busy: bool) -> None:
        self.btn_search.setEnabled(not busy)
        self.btn_stop.setEnabled(busy)
        self.btn_fetch.setEnabled((not busy) and 0 <= self.album_table.currentRow() < len(self._albums))
        has_tracks = bool(self._tracks)
        self.btn_all.setEnabled(has_tracks and not busy)
        self.btn_none.setEnabled(has_tracks and not busy)
        self.btn_dl.setEnabled(has_tracks and not busy)
        self.btn_dl_all.setEnabled(has_tracks and not busy)

    def search(self) -> None:
        if self._busy():
            return
        if self._on_sync is not None:
            try:
                self._on_sync()
            except Exception:  # noqa: BLE001
                pass
        self._refresh_out_hint()
        query = clean_album_query(self.name.text())
        if not query:
            self.status.setText("请先填专辑名")
            return
        self._albums = []
        self._tracks = []
        self._track_row.clear()
        self._pending_row = -1
        self._auto_token += 1
        self._loaded_row = -1
        self.album_table.setRowCount(0)
        self.track_table.setRowCount(0)
        self.track_header.setText("曲目（搜索后选专辑，单击即自动加载）")
        self._log("=" * 46, "detail")
        self._log(f"▶ 检索专辑：{query}" + (f"（歌手：{self.artist.text().strip()}）" if self.artist.text().strip() else ""))
        self._start_worker(AlbumSearchWorker(self.settings, cache_path=self.config.cache_path,
                                             mode=AlbumSearchWorker.MODE_SEARCH,
                                             query=query, artist=self.artist.text().strip(),
                                             parent=self))

    def fetch_tracks(self) -> None:
        """手动「获取曲目」（= 强制重新获取当前选中的专辑）。"""
        if self._busy():
            if getattr(self._worker, "mode", "") != AlbumSearchWorker.MODE_DOWNLOAD:
                self._pending_row = self.album_table.currentRow()
            return
        row = self.album_table.currentRow()
        if row < 0 or row >= len(self._albums):
            self.status.setText("请先在专辑列表里选一张")
            return
        self._fetch_album(row)

    def _on_album_selected(self) -> None:
        """单击专辑行：自动加载曲目（防抖）；忙时挂起，等当前任务收尾后自动接管。"""
        self._refresh_fetch_button()
        row = self.album_table.currentRow()
        if row < 0 or row >= len(self._albums):
            return
        if self._busy():
            # 下载中不参与自动切换（否则会清空正在下载的曲目列表）
            if getattr(self._worker, "mode", "") != AlbumSearchWorker.MODE_DOWNLOAD:
                self._pending_row = row
                self.status.setText("已记住要切换到这张专辑，当前任务完成后自动加载")
            return
        self._auto_token += 1
        token = self._auto_token
        QTimer.singleShot(180, lambda: self._auto_fetch_if(token, row))

    def _auto_fetch_if(self, token: int, row: int) -> None:
        if token != self._auto_token:
            return                     # 已被更新的选择取代
        if row != self.album_table.currentRow():
            return
        if row < 0 or row >= len(self._albums):
            return
        if self._busy():
            if getattr(self._worker, "mode", "") != AlbumSearchWorker.MODE_DOWNLOAD:
                self._pending_row = row
            return
        album = self._albums[row]
        if album is self._current_album and self._tracks:
            return                     # 同一张已加载过：不重复请求（要重取请点按钮）
        self._fetch_album(row)

    def _fetch_album(self, row: int) -> None:
        album = self._albums[row]
        self._current_album = album
        self._current_album_row = row
        self._tracks = []
        self._track_row.clear()
        self.track_table.setRowCount(0)
        self._log(f"▶ 获取曲目：{album.name}（{album.artist or '未知歌手'}）")
        self._start_worker(AlbumSearchWorker(self.settings, cache_path=self.config.cache_path,
                                             mode=AlbumSearchWorker.MODE_LIST,
                                             album=album, parent=self))

    def _pump_pending_fetch(self) -> None:
        """当前任务收尾后，自动切换到用户最近挂起的那张专辑。"""
        pending = self._pending_row
        self._pending_row = -1
        if pending < 0 or pending >= len(self._albums):
            return
        album = self._albums[pending]
        if album is self._current_album and self._tracks:
            return
        self._fetch_album(pending)

    def download_checked(self) -> None:
        tracks = self._checked_tracks()
        if not tracks:
            self.status.setText("先勾选要下载的曲目（或点「下载全部」）")
            return
        self._begin_download(tracks)

    def download_all(self) -> None:
        if not self._tracks:
            return
        self._begin_download(list(self._tracks))

    def _begin_download(self, tracks: "list") -> None:
        if self._busy() or not tracks:
            return
        # 曲序 = 曲目表位置（不是勾选子集的位置）：整张下载/部分勾选都写正确的 track number
        numbers = {id(t): i + 1 for i, t in enumerate(self._tracks)}
        pairs = [(t, numbers.get(id(t), 0)) for t in tracks]
        self._dl_rows = [self._track_row.get(id(t), -1) for t in tracks]
        self._dl_cursor = 0
        for row in self._dl_rows:
            if row >= 0:
                self.track_table.item(row, 5).setText("排队中")
        self._log(f"▶ 开始下载 {len(tracks)} 首（目标音质 {self.settings.quality.upper()}）"
                  "—— 按曲目表顺序逐首下载；每首走完整校验与完整度门禁，"
                  "全部通道都取不到的才如实提示")
        self.status.setText(f"下载中 0/{len(tracks)}")
        self._start_worker(AlbumSearchWorker(self.settings, cache_path=self.config.cache_path,
                                             mode=AlbumSearchWorker.MODE_DOWNLOAD,
                                             album=self._current_album,
                                             chosen_tracks=pairs, parent=self))

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.status.setText("正在停止…")

    def _start_worker(self, worker) -> None:
        self._worker = worker
        worker.logged.connect(self._log)
        worker.progressed.connect(self._on_progress)
        worker.file_done.connect(self._on_file_done)
        worker.completed.connect(self._on_completed)
        worker.failed.connect(self._on_failed)
        worker.albums.connect(self._on_albums)
        worker.tracklist.connect(self._on_tracklist)
        self._set_busy(True)
        self.status.setText("运行中…")
        worker.start()

    # ---------------- 回调 ---------------- #
    def _on_albums(self, query: str, hits) -> None:
        self._albums = list(hits)
        self.album_table.setRowCount(0)
        for i, g in enumerate(self._albums):
            hint = getattr(g, "count_hint", "") or ""
            count = (f"{g.track_count}（{hint}）" if (g.track_count and hint)
                     else (g.track_count or ""))
            self._set_album_row(f"alb:{i}", **{
                "专辑": g.name, "歌手": g.artist,
                "曲目数": count, "年份": g.year or ""})
        # 新结果集生效：作废检索期间可能残留的"挂起换行"与防抖令牌（行索引已失效）
        self._pending_row = -1
        self._auto_token += 1
        if not self._albums:
            self._log("⚠ 没有找到匹配的专辑：可换关键词、补上歌手，或稍后再试（过程明细可展开查看）")
            self.status.setText("没有找到专辑")
        else:
            self._log(f"· 共 {len(self._albums)} 张专辑（跨源合并、按置信度排序；"
                      "单击一张即自动加载曲目）")
            self.status.setText(f"{len(self._albums)} 张专辑，单击一张继续")
        self._retire_worker(self._worker)
        self._set_busy(False)

    def _on_tracklist(self, tracks) -> None:
        self._tracks = list(tracks)
        self.track_table.setRowCount(0)
        self._track_row.clear()
        for i, t in enumerate(self._tracks):
            self._set_track_row(i, t)
        if not self._tracks:
            self._log("⚠ 没有取到曲目：可换一张专辑/音源再试（逐源原因见明细）")
            self.status.setText("没有取到曲目")
        else:
            self._log(f"· 共 {len(self._tracks)} 首曲目（默认全部勾选）；「补位」曲目已把"
                      "曲名/歌手/专辑按参考歌单校正", "detail")
            self.status.setText(f"{len(self._tracks)} 首曲目，勾选后下载")
        # 曲目数刷新为**权威值**（酷狗/酷我=曲目表；咪咕/聚合=参考歌单长度）
        album = getattr(self, "_current_album", None)
        row_idx = getattr(self, "_current_album_row", -1)
        if album is not None and 0 <= row_idx < self.album_table.rowCount():
            col = ALBUM_COLUMNS.index("曲目数")
            self.album_table.item(row_idx, col).setText(str(album.track_count or ""))
        self._mark_loaded(row_idx, album, len(self._tracks))
        self._retire_worker(self._worker)
        self._set_busy(False)
        self._pump_pending_fetch()

    def _on_progress(self, done: int, total: int) -> None:
        self.status.setText(f"进度 {done}/{total}")

    def _on_file_done(self, res) -> None:
        if self._dl_cursor < len(self._dl_rows):
            row = self._dl_rows[self._dl_cursor]
            if row >= 0:
                ok = getattr(res, "status", "") == "downloaded"
                self.track_table.item(row, 5).setText("已完成" if ok else "失败")
                if not ok:
                    msg = "；".join(x for x in (str(getattr(res, "message", "") or ""),
                                                str(getattr(res, "detail", "") or "")) if x)
                    self.track_table.item(row, 5).setToolTip(msg)
            self._dl_cursor += 1

    def _on_completed(self, results) -> None:
        self._retire_worker(self._worker)
        self._set_busy(False)
        ok = sum(1 for r in results if getattr(r, "status", "") == "downloaded")
        fail = sum(1 for r in results if getattr(r, "status", "") == "failed")
        self.status.setText(f"完成：成功 {ok} · 失败 {fail}")
        self._dl_rows = []
        self._dl_cursor = 0

    def _on_failed(self, message: str) -> None:
        self._retire_worker(self._worker)
        self._set_busy(False)
        self.status.setText("失败（原因见日志）")
        self._log(f"❌ {message}")
        self._pump_pending_fetch()
        if self._dl_cursor < len(self._dl_rows):
            row = self._dl_rows[self._dl_cursor]
            if row >= 0:
                self.track_table.item(row, 5).setText("失败")

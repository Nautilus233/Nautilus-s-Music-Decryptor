"""「以名找歌」对话框：不需要任何输入文件，直接按歌名/歌手走备用音源链路。

用的是**和「开始处理」完全相同**的那条选源链：同样的歌名 + 歌手硬门槛、
伴奏/改编版本过滤、华语预匹配、跨平台时长核验、下载校验与完整度门禁 ——
区别只在于入口不需要一个加密文件。用来替代"去资源站手动搜了再下"的日常动作。

两种用法：
* **自动挑选最优**（默认）：一行一首，批量直接下"匹配度最高且通过全部校验"的那条；
* **列出候选让我挑**：先只检索（不下载），把**已经过硬门槛**的候选列出来，
  你点一条，再对它跑同一套下载 + 校验。人工挑选不会放松任何门槛。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from mmusic.duration import format_seconds
from mmusic.pipeline import SEARCH_PREFIX, default_search_dir

from . import geometry
from .worker import NameSearchWorker

MIN_SIZE = (560, 460)
PREFERRED_SIZE = (880, 700)
COLUMNS = ["状态", "歌名", "歌手", "时长", "码率", "来源"]


def parse_lines(text: str) -> list[str]:
    """一行一首；去掉空白与重复（保留顺序）。"""
    out: list[str] = []
    for raw in (text or "").splitlines():
        item = raw.strip()
        if item and item not in out:
            out.append(item)
    return out


class NameSearchDialog(QDialog):
    def __init__(self, settings, config, *, on_sync=None, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.config = config
        self._on_sync = on_sync
        self._fitted = False
        self._worker = None
        self._pending_done = None
        self._retired: "list" = []
        self._rows: "dict[str, int]" = {}
        self._candidates: "list" = []
        self._candidate_reference: "float | None" = None
        self._pick_row = -1
        self._log_lines: "list[tuple[str, str]]" = []
        self._hidden_details = 0

        self.setWindowTitle("单曲下载")
        self.setMinimumSize(*MIN_SIZE)
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(6)

        title = QLabel("单曲下载")
        title.setProperty("role", "title")
        root.addWidget(title)
        tip = QLabel("输入歌名即可下载。每行一首，可一次粘贴多首。"
                     f"  输出格式：{self.settings.quality.upper()}（在主窗口调整）")
        tip.setWordWrap(True)
        tip.setProperty("role", "muted")
        root.addWidget(tip)

        form = QFormLayout()
        form.setSpacing(6)
        self.names = QPlainTextEdit()
        self.names.setPlaceholderText("夜曲\n周杰伦 - 发如雪\n光年之外")
        self.names.setFixedHeight(76)
        form.addRow("歌名", self.names)
        self.artist = QLineEdit()
        self.artist.setPlaceholderText("选填；批量下载时用于上面所有歌曲")
        form.addRow("歌手", self.artist)
        root.addLayout(form)

        # Keep the old mode interface for callers/selfchecks. Both workflows now
        # have a visible button; a checkbox is no longer needed to discover them.
        self.chk_auto = QCheckBox("自动选择并下载")
        self.chk_auto.setParent(self)
        self.chk_auto.setChecked(True)
        self.chk_auto.hide()
        mode_help = QLabel("先搜索查看结果并选择下载，或自动匹配下载（支持每行一首）。")
        mode_help.setWordWrap(True)
        mode_help.setProperty("role", "muted")
        root.addWidget(mode_help)
        save_row = QHBoxLayout()
        save_row.addWidget(QLabel("保存位置"))
        save_row.addStretch(1)
        self.btn_out_dir = QPushButton("更改位置…")
        self.btn_out_dir.setToolTip("单曲和专辑下载共用此保存位置，不影响本地文件的保存设置。")
        self.btn_open_out = QPushButton("打开文件夹")
        self.btn_follow_main = QPushButton("使用默认位置")
        self.btn_follow_main.setToolTip("清掉专用目录，改回：主输出目录 → 默认目录（按需按优）")
        save_row.addWidget(self.btn_out_dir)
        save_row.addWidget(self.btn_follow_main)
        save_row.addWidget(self.btn_open_out)
        root.addLayout(save_row)

        self.out_hint = QLabel("")
        self.out_hint.setProperty("role", "muted")
        self.out_hint.setWordWrap(True)
        root.addWidget(self.out_hint)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        root.addWidget(self.table, 3)

        act = QHBoxLayout()
        self.btn_search = QPushButton("搜索歌曲")
        self.btn_search.setProperty("role", "primary")
        self.btn_search.setToolTip("只搜索第一首歌并列出可下载结果，不下载音频。")
        self.btn_start = QPushButton("自动搜索并下载")
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_pick = QPushButton("下载选中")
        self.btn_pick.setToolTip("直接下载你选中的这一条（不再重新检索；仍做校验与完整度门禁）")
        self.btn_pick.setEnabled(False)
        act.addWidget(self.btn_search)
        act.addWidget(self.btn_pick)
        act.addWidget(self.btn_start)
        act.addWidget(self.btn_stop)
        act.addStretch(1)
        self.status = QLabel("就绪")
        self.status.setProperty("role", "muted")
        act.addWidget(self.status)
        root.addLayout(act)

        log_row = QHBoxLayout()
        log_row.addWidget(QLabel("下载结果"))
        self.chk_detail = QCheckBox("查看处理详情")
        log_row.addWidget(self.chk_detail)
        log_row.addStretch(1)
        self.log_hint = QLabel("")
        self.log_hint.setProperty("role", "muted")
        log_row.addWidget(self.log_hint)
        root.addLayout(log_row)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(72)
        root.addWidget(self.log)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.button(QDialogButtonBox.StandardButton.Close).setText("返回主窗口")
        box.rejected.connect(self.reject)
        box.clicked.connect(lambda *_: self.reject())
        root.addWidget(box)

        self.btn_start.clicked.connect(self.start)
        self.btn_search.clicked.connect(self.search)
        self.btn_stop.clicked.connect(self.stop)
        self.btn_pick.clicked.connect(self.download_selected)
        self.btn_out_dir.clicked.connect(self._pick_out_dir)
        self.btn_follow_main.clicked.connect(self._follow_main_out_dir)
        self.btn_open_out.clicked.connect(self._open_out_dir)
        self.chk_auto.toggled.connect(self._on_mode_toggled)
        self.chk_detail.toggled.connect(self._on_detail_toggled)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.names.textChanged.connect(self._invalidate_candidates)
        self.artist.textChanged.connect(self._invalidate_candidates)
        self._refresh_out_hint()

    # ---------------- 生命周期 ---------------- #
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._fitted:
            self._fitted = True
            geometry.fit(self, preferred=PREFERRED_SIZE, minimum=MIN_SIZE,
                         center_on=self.parentWidget())

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._worker is not None:
            event.ignore()
            self.done(QDialog.DialogCode.Rejected)
            return
        super().closeEvent(event)

    def done(self, result) -> None:
        # Escape / titlebar close / Return all use this asynchronous drain.
        # Keep the dialog and QThread alive until finished; never GUI wait().
        if self._worker is not None:
            self._pending_done = result
            self.stop()
            return
        super().done(result)

    def _retire_worker(self, worker) -> None:
        if worker is None:
            return
        if self._worker is worker:
            self._worker = None
        self._release_worker(worker)

    def _release_worker(self, worker) -> None:
        if worker.isRunning():
            return
        try:
            if worker in self._retired:
                self._retired.remove(worker)
        except Exception:  # noqa: BLE001
            pass
        try:
            worker.deleteLater()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 界面细节 ---------------- #
    def effective_out_dir(self) -> Path:
        """「以名找歌」的落点，按需按优：专用目录 → 主输出目录 → 默认目录。"""
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
            self.out_hint.setText(f"保存到：{target}（下载专用）")
        elif configured:
            self.out_hint.setText(f"保存到：{target}（使用主窗口位置）"
                                  "（可单独更改）")
        else:
            self.out_hint.setText(f"保存到：{target}（默认）"
                                  "（可更改）")

    def _pick_out_dir(self) -> None:
        start = str(self.effective_out_dir())
        chosen = QFileDialog.getExistingDirectory(self, "选择「以名找歌」的输出目录", start)
        if chosen:
            # 只写**本模式专用**的目录，不动「开始处理」的输出目录
            self.settings.search_output_dir = chosen
            try:
                self.config.save()
            except Exception:  # noqa: BLE001
                pass
            self._refresh_out_hint()

    def _follow_main_out_dir(self) -> None:
        """清掉专用目录 → 回到"主输出目录 → 默认目录"。"""
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
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _on_mode_toggled(self, auto: bool) -> None:
        self.btn_start.setText("自动搜索并下载" if auto else "搜索歌曲")
        self._update_actions()

    def _update_actions(self) -> None:
        idle = self._worker is None
        self.btn_search.setEnabled(idle)
        self.btn_start.setEnabled(idle)
        self.btn_pick.setEnabled(idle and 0 <= self.table.currentRow() < len(self._candidates))
        for widget in (self.names, self.artist, self.chk_auto, self.btn_out_dir,
                       self.btn_follow_main):
            widget.setEnabled(idle)

    def _invalidate_candidates(self) -> None:
        self._candidates = []
        self._candidate_reference = None
        self._pick_row = -1
        self.table.setRowCount(0)
        self._rows.clear()
        self._update_actions()

    def _accept_worker_signal(self) -> bool:
        sender = self.sender()
        return sender is None or (sender is self._worker and not sender.cancel_requested)

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
            del self._log_lines[:len(self._log_lines) - 4000]
        if level == "result" or self.chk_detail.isChecked():
            self.log.appendPlainText(message)
        else:
            self._hidden_details += 1
        self._update_hint()

    def _set_row(self, key: str, status: str, **cells) -> int:
        row = self._rows.get(key)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for c in range(len(COLUMNS)):
                self.table.setItem(row, c, QTableWidgetItem(""))
            self._rows[key] = row
        self.table.item(row, 0).setText(status)
        for name, value in cells.items():
            col = COLUMNS.index(name)
            self.table.item(row, col).setText(str(value))
        return row

    # ---------------- 跑任务 ---------------- #
    def start(self) -> None:
        self._begin(NameSearchWorker.MODE_AUTO if self.chk_auto.isChecked()
                    else NameSearchWorker.MODE_LIST)

    def search(self) -> None:
        """Always list first, independent of the legacy auto-mode checkbox."""
        self._begin(NameSearchWorker.MODE_LIST)

    def _begin(self, mode) -> None:
        if self._worker is not None:
            return
        if self._on_sync is not None:
            try:
                self._on_sync()          # 把主界面的音质 / 输出目录同步过来
            except Exception:  # noqa: BLE001
                pass
        self._refresh_out_hint()
        names = parse_lines(self.names.toPlainText())
        if not names:
            self.status.setText("请先填歌名")
            return
        artist = self.artist.text().strip()
        self._invalidate_candidates()
        if mode == NameSearchWorker.MODE_LIST:
            self._start_worker(NameSearchWorker(self.settings, cache_path=self.config.cache_path,
                                                mode=NameSearchWorker.MODE_LIST,
                                                query=names[0], artist=artist, parent=self))
            return
        items = [(n, artist) for n in names] if artist else names
        self._log("=" * 46, "detail")
        self._log(f"▶ 以名找歌 {len(items)} 首 | 目标音质：{self.settings.quality.upper()}")
        self._start_worker(NameSearchWorker(self.settings, cache_path=self.config.cache_path,
                                            mode=NameSearchWorker.MODE_AUTO,
                                            items=items, parent=self))

    def download_selected(self) -> None:
        if self._worker is not None:
            return
        row = self.table.currentRow()
        if row < 0 or row >= len(self._candidates):
            self.status.setText("请先在列表里选一条")
            return
        self._pick_row = row
        track = self._candidates[row]
        self._log(f"· 已选：{track.source} “{track.title}”（{track.artist}）—— 直接下载，不再重搜",
                  "detail")
        # 把列表里算好的"跨平台参考时长"带过去：下载后用**真实文件**做完整度门禁，
        # 而不是重新检索再筛一遍（旧的"重新检索"路径会把用户看得见的候选判成未通过）。
        self._start_worker(NameSearchWorker(self.settings, cache_path=self.config.cache_path,
                                            mode=NameSearchWorker.MODE_PICK,
                                            chosen=track, reference=self._candidate_reference,
                                            parent=self))

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.btn_stop.setEnabled(False)
            self.status.setText("正在停止…")

    def _start_worker(self, worker) -> None:
        self._worker = worker
        worker.logged.connect(self._on_worker_log)
        worker.progressed.connect(self._on_progress)
        worker.file_done.connect(self._on_file_done)
        worker.completed.connect(self._on_completed)
        worker.failed.connect(self._on_failed)
        worker.candidates.connect(self._on_candidates)
        worker.cancelled.connect(self._on_cancelled)
        worker.finished.connect(self._on_worker_finished)
        self._update_actions()
        self.btn_stop.setEnabled(True)
        self.status.setText("运行中…")
        worker.start()

    def _on_worker_log(self, message, level) -> None:
        if self._accept_worker_signal():
            self._log(message, level)

    def _on_cancelled(self) -> None:
        if self.sender() is not self._worker:
            return
        self.status.setText("已停止")
        self._mark_picked("已停止")
        self._pick_row = -1
        self._log("⛔ 已停止")

    def _on_worker_finished(self) -> None:
        worker = self.sender()
        if worker is not self._worker:
            return
        if worker.cancel_requested and self.status.text() != "已停止":
            self._on_cancelled()          # also covers cancel during final cleanup
        self._retire_worker(worker)
        self.btn_stop.setEnabled(False)
        self._update_actions()
        if self._pending_done is not None:
            result, self._pending_done = self._pending_done, None
            super().done(result)

    def _on_candidates(self, query: str, tracks, reference=None) -> None:
        if not self._accept_worker_signal():
            return
        self._candidates = list(tracks)
        self._candidate_reference = reference
        self.table.setRowCount(0)
        self._rows.clear()
        for i, t in enumerate(self._candidates):
            self._set_row(f"cand:{i}", "候选",
                          **{"歌名": t.title, "歌手": t.artist,
                             "时长": format_seconds(t.duration) if t.duration else "",
                             "码率": f"{t.bitrate}k" if t.bitrate else "",
                             "来源": t.source})
        ref = (f"，跨平台参考时长 {format_seconds(self._candidate_reference)}"
               if self._candidate_reference else "")
        if not self._candidates:
            self._log("⚠ 没有可下载的候选：直链解析失败，或已全部被剔除（逐条原因见上方明细）")
            self.status.setText("没有可下载的候选")
        self._log(f"· 已列出 {len(self._candidates)} 个**可下载**候选"
                  f"（已过硬门槛、已逐条解析直链、按匹配度排序{ref}）"
                  "；点「下载选中」＝直接下这一条（不再重搜）", "detail")
        if self._candidates:
            self.table.selectRow(0)
            self.status.setText(f"找到 {len(self._candidates)} 条；选中后点击「下载选中」")
        self._update_actions()

    def _on_progress(self, done: int, total: int) -> None:
        if not self._accept_worker_signal():
            return
        self.status.setText(f"进度 {done}/{total}")

    def _on_file_done(self, res) -> None:
        if not self._accept_worker_signal():
            return
        if self._pick_row >= 0 and str(res.path).startswith(SEARCH_PREFIX):
            self._mark_picked("已完成" if res.ok else "失败")
        else:
            self._set_row(res.path, res.status,
                          **{"歌名": getattr(res, "name", ""),
                             "来源": res.source or ""})

    def _mark_picked(self, tag: str) -> None:
        """把"点名下载"的结果标回那一行 —— 否则用户点完看不到任何反馈。"""
        if self._pick_row >= 0:
            self._set_row(f"cand:{self._pick_row}", tag)

    def _on_completed(self, results) -> None:
        if not self._accept_worker_signal():
            return
        if self._worker is not None and self._worker.mode == NameSearchWorker.MODE_LIST:
            return                      # keep the list count, not "success 0"
        ok = sum(1 for r in results if r.ok)
        fail = sum(1 for r in results if r.status == "failed")
        if self._pick_row >= 0 and results:
            # 「下载选中」走的是 completed（不是逐文件 file_done）→ 在这里把那一行标好
            self._mark_picked("已完成" if results[0].ok else "失败")
        skipped = sum(1 for r in results if r.status == "skipped")
        self.status.setText(f"完成：成功 {ok} · 跳过 {skipped} · 失败 {fail}"
                            if skipped else f"完成：成功 {ok} · 失败 {fail}")
        self._pick_row = -1

    def _on_failed(self, message: str) -> None:
        if not self._accept_worker_signal():
            return
        self.status.setText("失败（原因见日志；可换一条候选再试）")
        self._mark_picked("失败")
        self._log(f"❌ {message}")
        self._pick_row = -1

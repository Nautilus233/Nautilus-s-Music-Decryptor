"""「以名找歌」对话框：不需要任何输入文件，直接按歌名/歌手走备用音源链路。

用的是**和「开始解密」完全相同**的那条选源链：同样的歌名 + 歌手硬门槛、
伴奏/改编版本过滤、华语预匹配、跨平台时长核验、下载校验与完整度门禁 ——
区别只在于入口不需要一个加密文件。用来替代"去资源站手动搜了再下"的日常动作。

两种用法：
* **自动挑选最优**（默认）：一行一首，批量直接下"匹配度最高且通过全部校验"的那条；
* **列出候选让我挑**：先只检索（不下载），把**已经过硬门槛**的候选列出来，
  你点一条，再对它跑同一套下载 + 校验。人工挑选不会放松任何门槛。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
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
        self._retired: "list" = []
        self._rows: "dict[str, int]" = {}
        self._candidates: "list" = []
        self._pick_row = -1
        self._log_lines: "list[tuple[str, str]]" = []
        self._hidden_details = 0

        self.setWindowTitle("以名找歌 · 备用音源")
        self.setMinimumSize(*MIN_SIZE)
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        tip = QLabel(
            "不需要加密文件：直接填歌名（一行一首＝批量），走与「开始解密」**相同**的备用音源链路。\n"
            "歌名/歌手硬门槛、伴奏与改编版本过滤、时长核验、下载校验一律照旧 —— 只列出通过门槛的候选。")
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
        self.artist.setPlaceholderText("可选；填了就对每一行都用这个歌手（更准）")
        form.addRow("歌手", self.artist)
        root.addLayout(form)

        opts = QHBoxLayout()
        self.chk_auto = QCheckBox("自动挑选最优（取消勾选＝先列出候选让我挑）")
        self.chk_auto.setChecked(True)
        opts.addWidget(self.chk_auto)
        opts.addStretch(1)
        self.btn_out_dir = QPushButton("输出目录…")
        self.btn_open_out = QPushButton("打开输出目录")
        opts.addWidget(self.btn_out_dir)
        opts.addWidget(self.btn_open_out)
        root.addLayout(opts)

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
        self.btn_start = QPushButton("开始")
        self.btn_start.setProperty("role", "primary")
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_pick = QPushButton("下载选中")
        self.btn_pick.setEnabled(False)
        act.addWidget(self.btn_start)
        act.addWidget(self.btn_stop)
        act.addWidget(self.btn_pick)
        act.addStretch(1)
        self.status = QLabel("就绪")
        self.status.setProperty("role", "muted")
        act.addWidget(self.status)
        root.addLayout(act)

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
        self.log.setFixedHeight(96)
        root.addWidget(self.log)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        box.clicked.connect(lambda *_: self.reject())
        root.addWidget(box)

        self.btn_start.clicked.connect(self.start)
        self.btn_stop.clicked.connect(self.stop)
        self.btn_pick.clicked.connect(self.download_selected)
        self.btn_out_dir.clicked.connect(self._pick_out_dir)
        self.btn_open_out.clicked.connect(self._open_out_dir)
        self.chk_auto.toggled.connect(self._on_mode_toggled)
        self.chk_detail.toggled.connect(self._on_detail_toggled)
        self._refresh_out_hint()

    # ---------------- 生命周期 ---------------- #
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._fitted:
            self._fitted = True
            geometry.fit(self, preferred=PREFERRED_SIZE, minimum=MIN_SIZE,
                         center_on=self.parentWidget())

    def closeEvent(self, event) -> None:  # noqa: N802
        # 与主窗口同一条纪律：关窗前有界等待线程收尾，绝不让线程活过窗口/进程
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

    # ---------------- 界面细节 ---------------- #
    def effective_out_dir(self) -> Path:
        configured = (self.settings.output_dir or "").strip()
        return Path(configured).expanduser() if configured else default_search_dir()

    def _refresh_out_hint(self) -> None:
        configured = (self.settings.output_dir or "").strip()
        if configured:
            self.out_hint.setText(f"输出目录：{configured}")
        else:
            self.out_hint.setText(f"输出目录：{default_search_dir()}（默认；可在“设置”或上方按钮里改）")

    def _pick_out_dir(self) -> None:
        start = str(self.effective_out_dir())
        chosen = QFileDialog.getExistingDirectory(self, "选择输出目录", start)
        if chosen:
            self.settings.output_dir = chosen
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
        self.btn_pick.setEnabled(not auto and bool(self._candidates))
        if not auto:
            self._log("· 已切换为「先列出候选」：只检索、不下载；选中一条后再点「下载选中」", "detail")

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
        if self._worker is not None and self._worker.isRunning():
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
        if not self.chk_auto.isChecked():
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
        row = self.table.currentRow()
        if row < 0 or row >= len(self._candidates):
            self.status.setText("请先在列表里选一条")
            return
        self._pick_row = row
        track = self._candidates[row]
        artist = self.artist.text().strip()
        self._log(f"· 已选：{track.source} “{track.title}”（{track.artist}）", "detail")
        self._start_worker(NameSearchWorker(self.settings, cache_path=self.config.cache_path,
                                            mode=NameSearchWorker.MODE_PICK,
                                            query=track.title or parse_lines(self.names.toPlainText())[0],
                                            artist=artist, chosen=track, parent=self))

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
        worker.candidates.connect(self._on_candidates)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.status.setText("运行中…")
        worker.start()

    def _on_candidates(self, query: str, tracks) -> None:
        self._candidates = list(tracks)
        self.table.setRowCount(0)
        self._rows.clear()
        for i, t in enumerate(self._candidates):
            self._set_row(f"cand:{i}", "候选",
                          **{"歌名": t.title, "歌手": t.artist,
                             "时长": format_seconds(t.duration) if t.duration else "",
                             "码率": f"{t.bitrate}k" if t.bitrate else "",
                             "来源": t.source})
        self._log(f"· 已列出 {len(self._candidates)} 个通过硬门槛的候选（按匹配度排序）", "detail")
        self.btn_pick.setEnabled(bool(self._candidates))

    def _on_progress(self, done: int, total: int) -> None:
        self.status.setText(f"进度 {done}/{total}")

    def _on_file_done(self, res) -> None:
        if self._pick_row >= 0 and str(res.path).startswith(SEARCH_PREFIX):
            self._set_row(f"cand:{self._pick_row}",
                          "已完成" if res.ok else "失败")
        else:
            self._set_row(res.path, res.status,
                          **{"歌名": getattr(res, "name", ""),
                             "来源": res.source or ""})

    def _on_completed(self, results) -> None:
        self._retire_worker(self._worker)
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        ok = sum(1 for r in results if r.ok)
        fail = sum(1 for r in results if r.status == "failed")
        self.status.setText(f"完成：成功 {ok} · 失败 {fail}")
        self._pick_row = -1

    def _on_failed(self, message: str) -> None:
        self._retire_worker(self._worker)
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.status.setText("失败")
        self._log(f"❌ {message}")
        self._pick_row = -1

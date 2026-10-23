"""In-process UI geometry self-check.

Shared by `python main.py --check-ui` and `tests/test_ui_fit.py`, so the
*same* assertions validate both the source tree and the frozen .exe.  This
is what catches "the window/dialog is clipped on this screen" before release.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QDialogButtonBox, QScrollArea

from mmusic.config import AppConfig

from .main_window import MainWindow
from .album_search_dialog import MIN_SIZE as AS_MIN, AlbumSearchDialog
from .album_search_dialog import ALBUM_COLUMNS as AS_COLS
from .album_search_dialog import TRACK_COLUMNS as AS_TCOLS
from .album_search_dialog import clean_album_query
from .name_search_dialog import MIN_SIZE as NS_MIN, NameSearchDialog
from .name_search_dialog import parse_lines as ns_parse_lines
from .settings_dialog import MIN_SIZE, SettingsDialog

__all__ = ["run"]


def run(app, *, verbose: bool = True) -> int:
    """Return the number of failed checks (0 == all good)."""
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        (passed if cond else failed).append(name)
        if verbose:
            print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   -> {extra}" if extra and not cond else ""))

    # keep away from the user's real window-geometry store
    org, appn = MainWindow.ORG_NAME, MainWindow.APP_NAME
    MainWindow.ORG_NAME = MainWindow.APP_NAME = "MusicDecryptorSelfCheck"
    MainWindow._settings_store().clear()
    try:
        tmp = Path(tempfile.mkdtemp(prefix="mmusic_selfcheck_"))
        cfg = AppConfig(path=tmp / "settings.json")
        cfg.settings.output_dir = ""

        # ---------------- main window -------------------------------- #
        win = MainWindow(cfg)
        win.show()
        for _ in range(3):
            app.processEvents()
        avail = win._available_geometry()
        frame = win.frameGeometry()
        if verbose:
            print(f"[main window] work area {avail.width()}x{avail.height()} @({avail.x()},{avail.y()})"
                  f" | frame {frame.width()}x{frame.height()} @({frame.x()},{frame.y()})")
        check("main: minimum size fits the work area",
              win.minimumWidth() <= avail.width() and win.minimumHeight() <= avail.height(),
              f"min={win.minimumWidth()}x{win.minimumHeight()}")
        check("main: window frame inside the work area", avail.contains(frame), f"{frame} not in {avail}")
        bottom = win.btn_start.mapTo(win, QPoint(0, 0)).y() + win.btn_start.height()
        check("main: bottom action row not clipped",
              win.btn_start.isVisible() and bottom <= win.height(),
              f"row ends at y={bottom}, height={win.height()}")
        log_bottom = win.log.mapTo(win, QPoint(0, 0)).y() + win.log.height()
        check("main: log panel not clipped", log_bottom <= win.height())

        # 日志分级：同事默认只看到"结论"，过程明细折叠在「显示详细信息」后面
        win._log("✅ 结论行（应始终可见）")
        win._log("· 明细行（默认折叠）", "detail")
        shown = win.log.toPlainText()
        check("main: 日志默认只显示结论（过程明细已折叠）",
              "结论行" in shown and "明细行" not in shown, repr(shown[-90:]))
        check("main: 折叠时如实提示折了多少行",
              "已折叠" in win.log_hint.text(), win.log_hint.text())
        win.chk_detail.setChecked(True)
        app.processEvents()
        shown2 = win.log.toPlainText()
        check("main: 勾选「显示详细信息」后明细可见（且结论仍在）",
              "明细行" in shown2 and "结论行" in shown2, repr(shown2[-90:]))
        win.chk_detail.setChecked(False)
        app.processEvents()
        check("main: 取消勾选后重新折叠",
              "明细行" not in win.log.toPlainText())

        # 接线契约（2026-10-20 事故）：`SignalInstance.emit` 的签名不可内省，一旦
        # Pipeline 把它当成"单参数 sink"，`emit(msg)` 会抛 TypeError 并被 `_guarded`
        # 吞掉 —— 整条日志静默失效（界面只剩界面自己的几行，用户以为开关没用）。
        from PySide6.QtCore import QObject as _QObject, Signal as _Signal

        from mmusic.pipeline import _accepts_level

        from .worker import make_log_sink

        class _Holder(_QObject):
            logged = _Signal(str, str)

        check("logging: Qt 信号 emit 被判为『不可内省』（不误判成单参数）",
              _accepts_level(_Holder().logged.emit) is None,
              str(_accepts_level(_Holder().logged.emit)))

        forwarded: list = []

        class _FakeSig:
            def emit(self, m, lv): forwarded.append((m, lv))

        sink = make_log_sink(_FakeSig())
        check("logging: worker 的日志 sink 可内省为两参数（不会被静默丢掉）",
              _accepts_level(sink) is True, str(_accepts_level(sink)))
        sink("· 明细行", "detail")
        sink("结论行")
        check("logging: 该 sink 完整转发 (message, level)",
              forwarded == [("· 明细行", "detail"), ("结论行", "detail")], str(forwarded))

        # 真正走一遍"worker 信号 → 窗口"：结论可见、明细折叠
        holder = _Holder()
        holder.logged.connect(win._log)
        deliver = make_log_sink(holder.logged)
        deliver("✅ 结论行（经信号）", "result")
        deliver("· 明细行（经信号）", "detail")
        app.processEvents()
        shown3 = win.log.toPlainText()
        check("main: 经 worker 信号投递的日志同样分级显示",
              "结论行（经信号）" in shown3 and "明细行（经信号）" not in shown3,
              repr(shown3[-100:]))
        win.chk_detail.setChecked(True)
        app.processEvents()
        check("main: 展开后经信号投递的明细也看得到",
              "明细行（经信号）" in win.log.toPlainText())
        win.chk_detail.setChecked(False)
        app.processEvents()
        win.setGeometry(avail.x(), avail.y(), avail.width() + 600, avail.height() + 400)
        win._clamp_to_screen()
        app.processEvents()
        f2 = win.frameGeometry()
        check("main: oversized geometry is clamped",
              f2.width() <= avail.width() and f2.height() <= avail.height(),
              f"{f2.width()}x{f2.height()}")

        # ---------------- settings dialog ---------------------------- #
        dlg = SettingsDialog(cfg.settings, win)
        dlg.show()
        for _ in range(3):
            app.processEvents()
        dframe = dlg.frameGeometry()
        if verbose:
            print(f"[settings dialog] frame {dframe.width()}x{dframe.height()} @({dframe.x()},{dframe.y()})"
                  f" | minimum client {MIN_SIZE[0]}x{MIN_SIZE[1]}")
        check("dialog: frame inside the work area", avail.contains(dframe), f"{dframe} not in {avail}")
        box = dlg.findChild(QDialogButtonBox)
        box_bottom = box.mapTo(dlg, QPoint(0, 0)).y() + box.height()
        check("dialog: OK/Cancel footer not clipped",
              box is not None and box.isVisible() and box_bottom <= dlg.height(),
              f"footer ends at y={box_bottom}, client height={dlg.height()}")
        specs = getattr(dlg, "specs", [])
        ids = [x.id for x in specs]
        check("dialog: every available source is listed (disabled ones too)",
              "internet_archive" in ids and "manual" in ids
              and {"local", "kuwo", "kugou", "aggregator", "wikimedia"} <= set(ids),
              str(ids))
        check("dialog: the checkbox set matches the spec list",
              len(dlg.source_boxes) == len(ids), f"{len(dlg.source_boxes)} vs {len(ids)}")

        ok_btn = box.button(QDialogButtonBox.StandardButton.Ok)
        corner = QPoint(ok_btn.mapToGlobal(QPoint(0, 0)).x() + ok_btn.width(),
                        ok_btn.mapToGlobal(QPoint(0, 0)).y() + ok_btn.height())
        check("dialog: OK button inside the work area", avail.contains(corner), str(corner))
        sa = dlg.findChild(QScrollArea)
        need = sa.widget().sizeHint().height() if sa and sa.widget() else 0
        span = sa.verticalScrollBar().maximum() if sa else -1
        check("dialog: body is scrollable (nothing unreachable)",
              sa is not None and (need <= sa.viewport().height() or span > 0),
              f"needs {need}px vs viewport {sa.viewport().height() if sa else 0}px, scroll max {span}")
        dlg.resize(*MIN_SIZE)
        for _ in range(3):
            app.processEvents()
        check("dialog: footer still visible at minimum size",
              box.mapTo(dlg, QPoint(0, 0)).y() + box.height() <= dlg.height())

        # ---------------- name-search dialog ------------------------- #
        ns = NameSearchDialog(cfg.settings, cfg, parent=win)
        ns.show()
        for _ in range(3):
            app.processEvents()
        nframe = ns.frameGeometry()
        if verbose:
            print(f"[name-search dialog] frame {nframe.width()}x{nframe.height()} "
                  f"@({nframe.x()},{nframe.y()}) | minimum client {NS_MIN[0]}x{NS_MIN[1]}")
        check("dialog(以名找歌): frame inside the work area",
              avail.contains(nframe), f"{nframe} not in {avail}")
        nbox = ns.findChild(QDialogButtonBox)
        nb = nbox.mapTo(ns, QPoint(0, 0)).y() + nbox.height()
        check("dialog(以名找歌): 关闭按钮不被截断",
              nbox is not None and nbox.isVisible() and nb <= ns.height(),
              f"footer ends at y={nb}, client height={ns.height()}")
        row_bottom = ns.btn_start.mapTo(ns, QPoint(0, 0)).y() + ns.btn_start.height()
        check("dialog(以名找歌): 开始/停止/下载选中 一行不被截断",
              ns.btn_start.isVisible() and row_bottom <= ns.height(),
              f"row ends at y={row_bottom}, height={ns.height()}")
        check("dialog(以名找歌): 候选表格可见且有可用高度",
              ns.table.isVisible() and ns.table.height() >= 60, f"table h={ns.table.height()}")
        check("dialog(以名找歌): 日志区可见",
              ns.log.isVisible() and ns.log.height() >= 40, f"log h={ns.log.height()}")
        check("dialog(以名找歌): 未检索前「下载选中」不可用（不会误发请求）",
              not ns.btn_pick.isEnabled())
        check("dialog(以名找歌): 歌名解析会去重且保序",
              ns_parse_lines(" 夜曲 \n夜曲\n发如雪\n\n") == ["夜曲", "发如雪"],
              str(ns_parse_lines(" 夜曲 \n夜曲\n发如雪\n\n")))
        ns.names.setPlainText("夜曲\n周杰伦 - 发如雪")
        ns.chk_auto.setChecked(False)
        app.processEvents()
        check("dialog(以名找歌): 切换「先列出候选」不抛异常",
              not ns.btn_pick.isEnabled())
        ns.resize(*NS_MIN)
        for _ in range(3):
            app.processEvents()
        check("dialog(以名找歌): 最小尺寸下底部按钮仍可见",
              ns.btn_start.mapTo(ns, QPoint(0, 0)).y() + ns.btn_start.height() <= ns.height())
        ns.close()

        # ---------------- album-search dialog ------------------------- #
        al = AlbumSearchDialog(cfg.settings, cfg, parent=win)
        al.show()
        for _ in range(3):
            app.processEvents()
        aframe = al.frameGeometry()
        if verbose:
            print(f"[album-search dialog] frame {aframe.width()}x{aframe.height()} "
                  f"@({aframe.x()},{aframe.y()}) | minimum client {AS_MIN[0]}x{AS_MIN[1]}")
        check("dialog(按专辑找歌): frame inside the work area",
              avail.contains(aframe), f"{aframe} not in {avail}")
        abox = al.findChild(QDialogButtonBox)
        ab = abox.mapTo(al, QPoint(0, 0)).y() + abox.height()
        check("dialog(按专辑找歌): 关闭按钮不被截断",
              abox is not None and abox.isVisible() and ab <= al.height(),
              f"footer ends at y={ab}, client height={al.height()}")
        check("dialog(按专辑找歌): 专辑与曲目两张表格可见且有可用高度",
              al.album_table.isVisible() and al.album_table.height() >= 60
              and al.track_table.isVisible() and al.track_table.height() >= 60,
              f"album={al.album_table.height()} track={al.track_table.height()}")
        check("dialog(按专辑找歌): 未取到曲目前下载按钮不可用（不会误发请求）",
              not al.btn_dl.isEnabled() and not al.btn_dl_all.isEnabled()
              and not al.btn_fetch.isEnabled())
        check("dialog(按专辑找歌): 专辑名清洗会去掉序号/说明前缀",
              clean_album_query("1. 十一月的萧邦") == "十一月的萧邦"
              and clean_album_query("专辑：Lemon") == "Lemon"
              and clean_album_query("  ") == "",
              f"{clean_album_query('1. 十一月的萧邦')!r} / {clean_album_query('专辑：Lemon')!r}")
        check("dialog(按专辑找歌): 合并视图不展示来源（两张表都没有「来源」列）",
              "来源" not in AS_COLS and "来源" not in AS_TCOLS,
              f"{AS_COLS} / {AS_TCOLS}")
        # 合并视图渲染（喂合成数据，不联网）：专辑行/曲目行的列映射必须正确
        from mmusic.model import Track as _Track
        from mmusic.search.album import AlbumGroup as _Group, AlbumHit as _Hit
        _g = _Group(name="十一月的萧邦", artist="周杰伦", track_count=12, year="2005",
                    variants=[_Hit(source="migu", album_id="1", name="十一月的萧邦",
                                   artist="周杰伦", track_count=12)])
        al._on_albums("十一月的萧邦", [_g])
        app.processEvents()
        _cells = [al.album_table.item(0, c).text() for c in range(len(AS_COLS))]
        check("dialog(按专辑找歌): 合并视图渲染专辑行（专辑/歌手/曲目数/年份）",
              _cells[:4] == ["十一月的萧邦", "周杰伦", "12", "2005"], str(_cells))
        al._current_album, al._current_album_row = _g, 0
        al._on_tracklist([_Track(source="migu", title="夜曲", artist="周杰伦",
                                 album="十一月的萧邦", duration=226)])
        app.processEvents()
        check("dialog(按专辑找歌): 曲目行渲染（默认勾选、时长格式化、标题带 ✓）",
              al.track_table.rowCount() == 1
              and al.track_table.item(0, 0).checkState() == Qt.CheckState.Checked
              and al.track_table.item(0, 3).text() == "3:46"
              and al.album_table.item(0, 0).text().endswith("✓"),
              f"{al.track_table.item(0, 3).text()!r} / {al.album_table.item(0, 0).text()!r}")
        al.resize(*AS_MIN)
        for _ in range(3):
            app.processEvents()
        check("dialog(按专辑找歌): 最小尺寸下底部按钮仍可见",
              al.btn_search.mapTo(al, QPoint(0, 0)).y() + al.btn_search.height() <= al.height())
        al.close()

        dlg.close()
        win.close()
    finally:
        MainWindow.ORG_NAME, MainWindow.APP_NAME = org, appn

    if verbose:
        print(f"[ui self-check] {len(passed)} passed, {len(failed)} failed")
        for f in failed:
            print("  -", f)
    return len(failed)
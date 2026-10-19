"""UI monkey test: 随机乱点全部控件，验证"不崩、不卡、状态自洽"。

    python tests/test_monkey_ui.py                 # 默认 3 轮 × 400 次操作
    python tests/test_monkey_ui.py --iters 800 --seed 7
    python tests/test_monkey_ui.py --quick         # 1 轮 × 150 次

为什么要它：界面上的按钮/勾选/下拉/输入框加起来几十个，很多组合只在"人乱点"
时才出现（任务跑到一半又改设置、清空列表、连点开始、关窗时线程还在跑…）。
这里用**可复现的随机种子**把这些组合自动点一遍，并把任何异常/不自洽状态当失败。

设计要点：
* 全程离线：注入一个"永远失败且立即返回"的假 HTTP，并把备用下载强制关掉 ——
  既不联网、也不慢；备用链路的失败路径照样被渲染（表格出现"失败"行）。
* 模态对话框（QFileDialog / 设置 / 补链 / QMessageBox）在测试里被替换成非阻塞桩，
  真正的设置对话框与补链对话框在单独阶段**直接构造并乱点**。
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import fixtures as fx  # noqa: E402
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent  # noqa: E402
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,  # noqa: E402
                               QDialogButtonBox, QDoubleSpinBox, QFileDialog, QLineEdit,
                               QListWidget, QMessageBox, QPlainTextEdit, QPushButton,
                               QSpinBox, QTableWidget)

from mmusic.config import AppConfig, Settings  # noqa: E402
from mmusic.errors import MusicDecryptorError, NetworkError  # noqa: E402
from mmusic.pipeline import Pipeline  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.manual_links_dialog import ManualLinksDialog, split_entries  # noqa: E402
from ui.settings_dialog import SettingsDialog  # noqa: E402

PASSED: "list[str]" = []
FAILED: "list[str]" = []
EXCEPTIONS: "list[str]" = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   -> {extra}" if extra and not cond else ""))


# --------------------------------------------------------------------------- #
# 注入：离线 + 确定性
# --------------------------------------------------------------------------- #
class _JunkHttp:
    """永远失败的假 HTTP：让备用链路立刻走"失败"分支（不联网、不等待）。"""

    def request(self, *a, **kw):
        raise NetworkError("猴子测试：不联网", detail="monkey")

    def get_json(self, *a, **kw):
        raise NetworkError("猴子测试：不联网", detail="monkey")

    def get_bytes(self, *a, **kw):
        raise NetworkError("猴子测试：不联网", detail="monkey")

    def download(self, *a, **kw):
        raise NetworkError("猴子测试：不联网", detail="monkey")

    def probe(self, *a, **kw):
        raise NetworkError("猴子测试：不联网", detail="monkey")

    def probe_tail(self, *a, **kw):
        raise NetworkError("猴子测试：不联网", detail="monkey")

    def close(self):
        pass


class _MonkeyPipeline(Pipeline):
    """包一层：强制离线 + 假 HTTP；可选每文件延时（用于"关窗时线程还在跑"）。"""

    DELAY = 0.0
    CALLS = 0                       # 统计真正被处理的文件数（防"猴子空转"）

    def __init__(self, settings, **kw):
        import copy
        s = copy.deepcopy(settings)
        s.enable_fallback = False
        kw.setdefault("http", _JunkHttp())
        super().__init__(s, **kw)
        self._delay = _MonkeyPipeline.DELAY

    def process_file(self, path):
        if self._delay:
            time.sleep(self._delay)
        _MonkeyPipeline.CALLS += 1
        return super().process_file(path)


def _install_stubs(fixtures: "list[Path]", out_dir: Path) -> None:
    """把模态入口全部换成非阻塞桩（否则猴子会被对话框卡死）。"""
    QDialog.exec = lambda self, *a, **k: 0                        # 一律当"取消"
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    QDesktopServices.openUrl = staticmethod(lambda *a, **k: True)  # 别真去开资源管理器
    QFileDialog.getOpenFileNames = staticmethod(
        lambda *a, **k: ([str(p) for p in fixtures], ""))
    QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: str(out_dir))


# --------------------------------------------------------------------------- #
# 随机动作
# --------------------------------------------------------------------------- #
_NO_DRAG = os.environ.get("MONKEY_NO_DRAG") == "1"

_NASTY_TEXT = [
    "", "   ", "C:\\不存在的目录", "D:\\", "/root/x", "CON", "NUL", "aux",
    "a" * 300, "🎵🎤", "..\\..\\..", "x" * 5000, "设置", "<script>", "'; DROP TABLE--",
    "https://example.com/a.mp3", "ftp://x/y.mp3", "file:///C:/x.mp3",
]


def _random_action(win: MainWindow, rnd: random.Random, fixtures: "list[Path]",
                   out_dir: Path) -> str:
    """随机对一个真实控件做一次动作；返回动作描述（用于报错定位）。"""
    choices = ["btn_add_files", "btn_add_dir", "btn_remove", "btn_clear",
               "btn_out_dir", "btn_open_out", "btn_settings", "btn_manual",
               "btn_start", "btn_stop", "quality", "out_dir", "chk_detail",
               "table", "add_paths", "drop", "drag_enter"]
    if _NO_DRAG:                    # 排查用：连"动作名"一起剔除，否则会落到 click() 分支
        choices = [c for c in choices if c not in ("drop", "drag_enter")]
    what = rnd.choice(choices)
    if what == "btn_start":
        win.start_job()
    elif what == "btn_stop":
        win.stop_job()
    elif what == "quality":
        win.quality.setCurrentIndex(rnd.randrange(max(1, win.quality.count())))
    elif what == "out_dir":
        win.out_dir.setText(rnd.choice(_NASTY_TEXT + [str(out_dir)]))
    elif what == "chk_detail":
        win.chk_detail.setChecked(not win.chk_detail.isChecked())
    elif what == "table":
        if win.table.rowCount():
            win.table.selectRow(rnd.randrange(win.table.rowCount()))
        win.table.clearSelection()
    elif what == "add_paths":
        pool = [str(p) for p in fixtures] + [str(out_dir), "", "不存在的文件.mgg"]
        win.add_paths(rnd.sample(pool, k=rnd.randrange(0, 3)))
    elif what in ("drop", "drag_enter"):
        # 拖拽是同事的主要用法，必须一起猴子化。mime 里混入合法路径、目录、
        # 不存在的文件，以及"根本不是 URL"的文本，走真实的事件处理函数。
        mime = QMimeData()
        if rnd.random() < 0.85:
            picks = [str(p) for p in fixtures] + [str(out_dir), "不存在的文件.mgg"]
            mime.setUrls([QUrl.fromLocalFile(x)
                          for x in rnd.sample(picks, k=rnd.randrange(0, 3))])
        else:
            mime.setText("这不是文件，只是文本")
        if what == "drop":
            ev = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            win.dropEvent(ev)
        else:
            ev = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.CopyAction, mime,
                                 Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            win.dragEnterEvent(ev)
            if not mime.hasUrls():
                ev2 = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                                 Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
                win.dropEvent(ev2)
    else:
        getattr(win, what).click()
    return what


def _wait_worker(win: MainWindow, app: QApplication, limit: float = 15.0) -> bool:
    """等后台任务收尾；返回是否已收尾（False = 卡住）。"""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < limit:
        app.processEvents()
        if win._worker is None or not win._worker.isRunning():
            return True
        time.sleep(0.01)
    return False


def _invariants(win: MainWindow) -> "list[str]":
    """随时可查的自洽性检查；返回问题列表（空 = 正常）。"""
    problems = []
    if win.table.rowCount() != len(win._rows):
        problems.append(f"表格行数 {win.table.rowCount()} != 记录数 {len(win._rows)}")
    known = {"已解密", "备用下载", "已复制", "已跳过", "失败", "等待", "处理中"}
    for r in range(win.table.rowCount()):
        item = win.table.item(r, 1)
        if item is not None and item.text() not in known:
            problems.append(f"第 {r} 行状态异常：{item.text()!r}")
    if win.log.blockCount() > 4000:
        problems.append(f"日志控件超过上限：{win.log.blockCount()}")
    if win.progress.value() > max(1, win.progress.maximum()):
        problems.append(f"进度条越界：{win.progress.value()}/{win.progress.maximum()}")
    return problems


# --------------------------------------------------------------------------- #
# 阶段一：随机乱点
# --------------------------------------------------------------------------- #
def phase_click(win: MainWindow, app: QApplication, rnd: random.Random,
                fixtures: "list[Path]", out_dir: Path, iters: int) -> None:
    print(f"\n[monkey-1: 随机乱点 {iters} 次]")
    errors: "list[str]" = []
    for i in range(iters):
        try:
            what = _random_action(win, rnd, fixtures, out_dir)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"#{i} {what}: {type(exc).__name__}: {str(exc)[:60]}")
            continue
        if i % 25 == 0:
            app.processEvents()
        if i % 120 == 119:                      # 事件洪水：30 个动作不刷新
            for _ in range(30):
                try:
                    _random_action(win, rnd, fixtures, out_dir)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"flood: {type(exc).__name__}: {str(exc)[:60]}")
    app.processEvents()
    if not _wait_worker(win, app):
        win.stop_job()
        _wait_worker(win, app, 5.0)
    check("monkey: 随机乱点期间无异常", not errors, "; ".join(errors[:3]))
    problems = _invariants(win)
    check("monkey: 乱点后界面状态自洽", not problems, "; ".join(problems[:3]))


# --------------------------------------------------------------------------- #
# 阶段二：设置对话框 / 补链对话框乱点
# --------------------------------------------------------------------------- #
def phase_dialogs(cfg: AppConfig, win: MainWindow, app: QApplication,
                  rnd: random.Random, rounds: int) -> None:
    print(f"\n[monkey-2: 设置与补链对话框乱点 ×{rounds}]")
    errors: "list[str]" = []
    bad_values: "list[str]" = []
    for r in range(rounds):
        dlg = SettingsDialog(cfg.settings, win)
        editor = QPlainTextEdit()
        widgets: "list" = []
        widgets += dlg.findChildren(QCheckBox)
        widgets += dlg.findChildren(QComboBox)
        widgets += dlg.findChildren(QSpinBox)
        widgets += dlg.findChildren(QDoubleSpinBox)
        widgets += dlg.findChildren(QLineEdit)
        for _ in range(60):
            w = rnd.choice(widgets)
            try:
                if isinstance(w, QCheckBox):
                    w.setChecked(not w.isChecked())
                elif isinstance(w, QComboBox):
                    w.setCurrentIndex(rnd.randrange(max(1, w.count())))
                elif isinstance(w, QSpinBox):
                    w.setValue(rnd.choice([0, 1, 999999, -5]))
                elif isinstance(w, QDoubleSpinBox):
                    w.setValue(rnd.choice([0.0, 0.5, 1e9, -1.0]))
                elif isinstance(w, QLineEdit):
                    w.setText(rnd.choice(_NASTY_TEXT))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"设置控件 {type(w).__name__}: {type(exc).__name__}: {str(exc)[:50]}")
        # 目录列表 / 剪贴板按钮 / 底部按钮
        try:
            QApplication.clipboard().setText(rnd.choice(_NASTY_TEXT))
            for btn in dlg.findChildren(QPushButton):
                if rnd.random() < 0.5:
                    btn.click()
            box = dlg.findChild(QDialogButtonBox)
            if box is not None and rnd.random() < 0.5:
                btn = box.button(rnd.choice([QDialogButtonBox.StandardButton.Ok,
                                             QDialogButtonBox.StandardButton.Cancel]))
                if btn is not None:
                    btn.click()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"设置按钮: {type(exc).__name__}: {str(exc)[:50]}")

        # 应用后必须是"合法设置"（否则界面/流水线会在别处炸）
        probe = Settings()
        try:
            dlg.apply_to(probe)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"apply_to: {type(exc).__name__}: {str(exc)[:50]}")
            continue
        if probe.quality not in ("mp3", "flac", "wav"):
            bad_values.append(f"quality={probe.quality!r}")
        if not (0 <= int(probe.min_bitrate) <= 100000):
            bad_values.append(f"min_bitrate={probe.min_bitrate!r}")
        if float(probe.timeout) <= 0 or float(probe.timeout) > 1e6:
            bad_values.append(f"timeout={probe.timeout!r}")
        if not isinstance(probe.aggregator_base, str) or not isinstance(probe.sources, list):
            bad_values.append("aggregator_base/sources 类型异常")
        if not isinstance(probe.local_dirs, list) or not isinstance(probe.manual_links, list):
            bad_values.append("local_dirs/manual_links 类型异常")
        dlg.deleteLater()

        # 补链对话框：乱贴文本后 entries() 必须给出干净的字符串列表
        ml = ManualLinksDialog(rnd.sample(_NASTY_TEXT, k=3), win)
        ml.editor.setPlainText("\n".join(rnd.sample(_NASTY_TEXT, k=4)))
        try:
            entries = ml.entries()
            if not isinstance(entries, list) or not all(isinstance(x, str) for x in entries):
                bad_values.append("补链 entries 类型异常")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"补链 entries: {type(exc).__name__}: {str(exc)[:50]}")
        ml.deleteLater()
    check("monkey: 设置/补链对话框乱点无异常", not errors, "; ".join(errors[:3]))
    check("monkey: 乱点后设置仍是合法值", not bad_values, "; ".join(bad_values[:3]))

    # 设置必须能存盘并读回（乱点后重启不能丢/不能崩）
    try:
        cfg.save()
        back = AppConfig(path=cfg.path)
        ok = back.settings.quality in ("mp3", "flac", "wav")
        extra = f"{back.settings.quality}"
    except Exception as exc:  # noqa: BLE001
        ok, extra = False, f"{type(exc).__name__}: {exc}"
    check("monkey: 乱点后的设置能存盘并读回", ok, extra)
    check("monkey: split_entries 对畸形文本稳健",
          isinstance(split_entries(";;\n\n'x'\n\"y\";z,,z"), list))


# --------------------------------------------------------------------------- #
# 阶段三：任务乱启停 + 关窗时线程还在跑
# --------------------------------------------------------------------------- #
def phase_jobs(win: MainWindow, app: QApplication, rnd: random.Random,
               fixtures: "list[Path]", rounds: int) -> None:
    print(f"\n[monkey-3: 任务乱启停 ×{rounds}]")
    stuck: "list[str]" = []
    errors: "list[str]" = []
    for r in range(rounds):
        win.add_paths(rnd.sample([str(p) for p in fixtures], k=rnd.randrange(1, 4)))
        for _ in range(rnd.randrange(1, 4)):
            try:
                win.start_job()
                if rnd.random() < 0.4:
                    win.stop_job()
                win.quality.setCurrentIndex(rnd.randrange(win.quality.count()))
                win.chk_detail.setChecked(rnd.random() < 0.5)
                if rnd.random() < 0.3:
                    win.btn_clear.click()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"round{r}: {type(exc).__name__}: {str(exc)[:50]}")
        if not _wait_worker(win, app, 20.0):
            stuck.append(f"round{r}")
            win.stop_job()
            _wait_worker(win, app, 5.0)
    check("monkey: 任务乱启停无异常", not errors, "; ".join(errors[:3]))
    check("monkey: 任务都能收尾（没有卡死的后台线程）", not stuck, "; ".join(stuck))
    problems = _invariants(win)
    check("monkey: 任务阶段结束后状态自洽", not problems, "; ".join(problems[:3]))

    # 关窗时线程还在跑：必须被取消并在有限时间内收尾
    _MonkeyPipeline.DELAY = 0.03
    win.add_paths([str(p) for p in fixtures] * 6)
    win.start_job()
    app.processEvents()
    win.close()
    app.processEvents()
    ended = _wait_worker(win, app, 20.0)
    check("monkey: 关闭窗口时正在跑的任务会被取消并收尾", ended,
          "窗口已关但后台线程仍在运行")
    _MonkeyPipeline.DELAY = 0.0


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=400, help="阶段一随机动作数")
    ap.add_argument("--rounds", type=int, default=3, help="阶段二/三轮数")
    ap.add_argument("--seed", type=int, default=20261020)
    ap.add_argument("--quick", action="store_true", help="1 轮 × 150 次（约 5 秒）")
    ap.add_argument("--no-drag", action="store_true",
                    help="跳过手工构造的拖拽事件（排查用）")
    args = ap.parse_args()
    if args.quick:
        args.iters, args.rounds = 150, 1
    global _NO_DRAG
    _NO_DRAG = _NO_DRAG or args.no_drag

    rnd = random.Random(args.seed)
    app = QApplication.instance() or QApplication([])
    # 别把窗口几何写进用户真实的 QSettings（与 ui/selfcheck.py 同款隔离）
    MainWindow.ORG_NAME = MainWindow.APP_NAME = "MusicDecryptorMonkey"
    MainWindow._settings_store().clear()
    import ui.worker as worker_mod
    worker_mod.Pipeline = _MonkeyPipeline          # 注入：离线 + 确定性

    tmp = Path(tempfile.mkdtemp(prefix="mmusic_monkey_"))
    out_dir = tmp / "out"
    out_dir.mkdir()
    # 卫生要求：猴子会把"输出目录"输入框填成 `设置` / `🎵🎤` / `'; DROP TABLE--` 这类脏值，
    # 一旦任务带着**相对路径**跑起来，产物就会落在进程当前目录（= 仓库根！实测污染过 2900 个文件）。
    # 所以先把 CWD 切到临时目录，并在最后断言仓库根"零新增"。
    repo_root = ROOT
    os.chdir(tmp)
    before = {x.name for x in repo_root.iterdir()}

    # 夹具：好容器 / 坏容器 / 明文 / 不支持 / 目录 / 空文件
    fixtures: "list[Path]" = []
    (tmp / "ok.ncm").write_bytes(fx.build_ncm(fx.PAYLOAD_MP3))
    (tmp / "ok.mflac").write_bytes(fx.build_qmc_v2(fx.PAYLOAD_MP3, bytes(range(32))))
    ms = tmp / "ok.mgg"
    ms.write_bytes(fx.build_musicex(fx.PAYLOAD_OGG, bytes(range(32))))
    (tmp / "ekeys").mkdir(exist_ok=True)
    (tmp / "ekeys" / "00TEST00000001__TEST00001.mgg.txt").write_text(
        fx.make_qmc_v2_key(bytes(range(32))).decode("ascii"), encoding="utf-8")
    (tmp / "plain.mp3").write_bytes(fx.make_mp3_xing(200, pad=40000))
    (tmp / "broken.mgg").write_bytes(os.urandom(6000) + b"STag")
    (tmp / "empty.mgg").write_bytes(b"")
    (tmp / "notes.txt").write_text("not audio", encoding="utf-8")
    fixtures = [p for p in tmp.iterdir() if p.is_file()]
    _install_stubs(fixtures, out_dir)

    cfg = AppConfig(path=tmp / "settings.json")
    cfg.settings.output_dir = str(out_dir)
    cfg.settings.write_metadata = False
    cfg.settings.fetch_cover = False
    cfg.settings.remember_hashes = False
    win = MainWindow(cfg)
    win.show()
    app.processEvents()

    t0 = time.perf_counter()
    phase_click(win, app, rnd, fixtures, out_dir, args.iters)
    phase_dialogs(cfg, win, app, rnd, args.rounds)
    phase_jobs(win, app, rnd, fixtures, args.rounds)
    dt = time.perf_counter() - t0

    # 收尾：窗口仍可用？（关掉后重新开一个，模拟"同事再打开一次"）
    win2 = MainWindow(cfg)
    win2.add_paths([str(fixtures[0])])
    win2.start_job()
    ok = _wait_worker(win2, app, 20.0)
    check("monkey: 乱点之后仍能新建窗口并正常跑完一个任务", ok)
    win2.close()
    app.processEvents()

    check("monkey: 确实跑过任务（不是空转）", _MonkeyPipeline.CALLS >= 5,
          f"处理过 {_MonkeyPipeline.CALLS} 个文件")
    # 卫生自检：仓库根不得出现任何新文件/新目录（脏路径只能落在临时目录里）
    after = {x.name for x in repo_root.iterdir()}
    leaked = sorted(after - before)
    check("monkey: 没有污染仓库工作区（脏输出路径只落在临时目录）", not leaked,
          "新增：" + ", ".join(leaked[:5]))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed   （{dt:.1f}s, seed={args.seed}，"
          f"处理 {_MonkeyPipeline.CALLS} 个文件）")
    if FAILED:
        print("failures:")
        for f in FAILED:
            print("  -", f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

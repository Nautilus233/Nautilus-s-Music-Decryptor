"""Music Decryptor - entry point.

    python main.py                 # launch the GUI
    python main.py --cli FILE...   # headless batch
    python main.py --selftest      # run the self-tests
    python main.py FILE --auto     # GUI, start immediately
"""
from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run_gui(initial, auto: bool) -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    import ui.theme as theme
    from mmusic.config import AppConfig
    from mmusic.paths import resource_path
    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("MusicDecryptor")
    app.setApplicationDisplayName("音乐解密工具")
    theme.apply(app)
    icon = resource_path("assets", "icon.ico")
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))

    config = AppConfig()
    window = MainWindow(config, initial_paths=initial)
    window.show()
    if auto and initial:
        QTimer.singleShot(400, window.start_job)
    return app.exec()


def _ensure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def run_cli(paths, args) -> int:
    _ensure_utf8_console()
    from mmusic.config import AppConfig
    from mmusic.pipeline import Pipeline

    config = AppConfig()
    if args.output:
        config.settings.output_dir = args.output
    if args.quality:
        config.settings.quality = args.quality
    if args.no_fallback:
        config.settings.enable_fallback = False

    targets: list[str] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            targets += [str(c) for c in sorted(p.rglob("*")) if c.is_file()]
        else:
            targets.append(str(p))

    pipe = Pipeline(config.settings, cache_path=config.cache_path,
                    on_log=lambda m: print(m, flush=True))
    try:
        results = pipe.process_many(targets)
    finally:
        pipe.close()

    for r in results:
        line = f"[{r.status}] {r.name}"
        if r.out_path:
            line += f" -> {Path(r.out_path).name}"
        if r.message:
            line += f"  ({r.message})"
        if r.detail:
            line += f"  {r.detail}"
        print(line)
    ok = sum(1 for r in results if r.ok)
    fail = sum(1 for r in results if r.status == "failed")
    print(f"\n完成：成功 {ok} / 失败 {fail} / 共 {len(results)}")
    return 0 if fail == 0 else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="music-decryptor",
                                     description="本地优先的音乐解密工具（NCM / QMC / MGG）")
    parser.add_argument("paths", nargs="*", help="要处理的文件或文件夹")
    parser.add_argument("--cli", action="store_true", help="无界面批处理模式")
    parser.add_argument("--auto", action="store_true", help="GUI 启动后立即开始")
    parser.add_argument("--output", help="输出目录")
    parser.add_argument("--quality", choices=["mp3", "flac", "wav"], help="备用下载目标音质")
    parser.add_argument("--no-fallback", action="store_true", help="禁用备用搜索下载")
    parser.add_argument("--selftest", action="store_true", help="运行自检")
    parser.add_argument("--check-qt", action="store_true", help="自检：加载 Qt 并打印版本后退出")
    parser.add_argument("--check-ui", action="store_true", help="自检：校验主窗口与设置弹窗的屏幕自适应")
    args = parser.parse_args(argv)

    if args.check_qt:
        _ensure_utf8_console()
        from PySide6.QtCore import qVersion
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        print(f"Qt {qVersion()} OK")
        del app
        return 0
    if args.check_ui:
        _ensure_utf8_console()
        import ui.selfcheck as selfcheck
        import ui.theme as theme
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        theme.apply(app)
        failed = selfcheck.run(app)
        return 1 if failed else 0
    if args.selftest:
        sys.argv = ["selftest"]
        runpy.run_path(str(ROOT / "tests" / "selftest.py"), run_name="__main__")
        return 0
    if args.cli:
        return run_cli(args.paths, args)
    return run_gui(args.paths, args.auto)


if __name__ == "__main__":
    raise SystemExit(main())
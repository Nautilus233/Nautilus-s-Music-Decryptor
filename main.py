"""Music Decryptor - entry point.

    python main.py                 # launch the GUI
    python main.py --cli FILE...   # headless batch
    python main.py --selftest      # run the self-tests
    python main.py FILE --auto     # GUI, start immediately
"""
from __future__ import annotations

import argparse
import re
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
    links: list[str] = []
    if args.links:
        links += [x for x in re.split(r"[,\n;]+", args.links) if x.strip()]
    if args.links_file:
        try:
            links += [l.strip() for l in Path(args.links_file).read_text("utf-8").splitlines()
                      if l.strip()]
        except OSError as exc:
            print(f"读取补链文件失败：{exc}")
            return 2
    if links:
        config.settings.manual_links = links
        print(f"已载入 {len(links)} 条手动补链（将优先使用）")

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
    parser.add_argument("--check-worker", action="store_true",
                        help="自检：跑一次后台任务后正常退出（验证 QThread 生命周期）")
    parser.add_argument("--links", help="手动补链：直链或本地路径（逗号/换行分隔），优先于自动音源")
    parser.add_argument("--links-file", help="从文本文件读取补链（每行一条）")
    parser.add_argument("--sources-report", action="store_true",
                        help="打印音源清单与健康统计后退出")
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
    if args.check_worker:
        _ensure_utf8_console()
        import tempfile
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        from mmusic.config import AppConfig
        from ui.main_window import MainWindow

        # 这一条专门盯"任务线程生命周期"（2026-10-20 猴子测试挖出的真实崩溃）：
        # 旧代码在任务完成/关窗时把 QThread 的最后引用丢掉，进程退出时会原生崩溃
        # （0xC0000409，Windows 下必现）。这个形状（建窗 → 灌一批文件 → 起任务 →
        # 定时关窗 → app.exec 事件循环 → app.quit → 进程退出）能稳定复现，
        # 因此冻结产物也跑一遍：退出码非 0 就是回归。
        app = QApplication.instance() or QApplication([])
        tmp = Path(tempfile.mkdtemp(prefix="mmusic-worker-"))
        cfg = AppConfig(path=tmp / "settings.json")
        cfg.settings.output_dir = str(tmp / "out")
        cfg.settings.enable_fallback = False
        cfg.settings.write_metadata = False
        cfg.settings.fetch_cover = False
        cfg.settings.remember_hashes = False
        cfg.settings.overwrite = True
        for i in range(60):
            (tmp / f"sample{i}.mgg").write_bytes(b"\x00" * 2048)
        win = MainWindow(cfg)
        win.show()
        win.add_paths([str(p) for p in sorted(tmp.glob("*.mgg"))])
        win.start_job()
        QTimer.singleShot(20, win.close)          # 任务还在跑时关窗（用户最常见的动作）
        QTimer.singleShot(3000, app.quit)
        app.exec()
        print("worker lifecycle: OK（若进程退出码非 0，说明线程收尾有问题）")
        return 0
    if args.sources_report:
        _ensure_utf8_console()
        from mmusic.config import AppConfig, default_config_dir
        from mmusic.search.index import ResourceIndex
        from mmusic.search.pack import pack_summary
        from mmusic.search.registry import specs_for

        config = AppConfig()
        portable = config.dir != default_config_dir()
        print(f"配置目录：{config.dir}"
              f"（{'便携模式：配置随 exe 走' if portable else '默认：%APPDATA%'}）")
        print(f"哈希缓存：{config.cache_path}")
        print("配置的音源（按尝试顺序）：")
        print(pack_summary(specs_for(config.settings.to_dict())))
        ledger = ResourceIndex(config.cache_path.parent / "resources.db")
        print(f"\n台账：{ledger.stats()}")
        rows = ledger.health()
        print("音源健康统计：")
        if not rows:
            print("  （暂无记录：还没走过备用下载）")
        for row in rows:
            print(f"  {row['source']:11} 尝试 {row['attempts']:>4}  成功 {row['successes']:>4}"
                  f"  失败 {row['failures']:>4}  成功率 {row['success_rate'] * 100:.0f}%"
                  f"  平均 {row['avg_ms']} ms")
        ledger.close()
        return 0
    if args.selftest:
        sys.argv = ["selftest"]
        runpy.run_path(str(ROOT / "tests" / "selftest.py"), run_name="__main__")
        return 0
    if args.cli:
        return run_cli(args.paths, args)
    return run_gui(args.paths, args.auto)


if __name__ == "__main__":
    raise SystemExit(main())
"""Headless smoke-test of the Qt UI + worker integration."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import fixtures as fx  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import ui.theme as theme  # noqa: E402
from mmusic.config import AppConfig  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.settings_dialog import SettingsDialog  # noqa: E402


def main() -> int:
    app = QApplication.instance() or QApplication([])
    theme.apply(app)

    tmp = Path(tempfile.mkdtemp(prefix="mmusic_gui_"))
    cfg = AppConfig(path=tmp / "settings.json")
    cfg.settings.output_dir = str(tmp / "out")
    cfg.settings.enable_fallback = False       # keep the smoke test offline
    cfg.settings.write_metadata = True
    cfg.settings.fetch_cover = False
    cfg.settings.remember_hashes = False

    win = MainWindow(cfg)

    # settings dialog constructs and round-trips
    dlg = SettingsDialog(cfg.settings, win)
    dlg.apply_to(cfg.settings)
    dlg.deleteLater()

    src = tmp / "sample.ncm"
    src.write_bytes(fx.build_ncm(fx.PAYLOAD_MP3))
    win.add_paths([str(src)])
    assert win.table.rowCount() == 1, "row not added"
    win.start_job()

    state = {"finished": False}

    def on_thread_finished() -> None:
        state["finished"] = True
        app.quit()

    win._worker.finished.connect(on_thread_finished)
    QTimer.singleShot(25000, app.quit)
    app.exec()

    status = win.table.item(0, 1).text()
    out_name = win.table.item(0, 3).text()
    produced = list((tmp / "out").glob("*.mp3"))
    print(f"worker finished: {state['finished']}")
    print(f"row status     : {status}")
    print(f"output cell    : {out_name}")
    print(f"files produced : {[p.name for p in produced]}")

    ok = state["finished"] and status == "已解密" and len(produced) == 1
    print("GUI SMOKE TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
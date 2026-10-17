"""In-process UI geometry self-check.

Shared by `python main.py --check-ui` and `tests/test_ui_fit.py`, so the
*same* assertions validate both the source tree and the frozen .exe.  This
is what catches "the window/dialog is clipped on this screen" before release.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QDialogButtonBox, QScrollArea

from mmusic.config import AppConfig

from .main_window import MainWindow
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

        dlg.close()
        win.close()
    finally:
        MainWindow.ORG_NAME, MainWindow.APP_NAME = org, appn

    if verbose:
        print(f"[ui self-check] {len(passed)} passed, {len(failed)} failed")
        for f in failed:
            print("  -", f)
    return len(failed)
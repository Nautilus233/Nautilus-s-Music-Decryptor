"""Real-screen UI adaptation test.

Thin wrapper over `ui.selfcheck.run` so the *same* assertions validate the
source tree and the frozen .exe (`main.py --check-ui`).

    python tests/test_ui_fit.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import ui.theme as theme  # noqa: E402
from ui.selfcheck import run  # noqa: E402


def main() -> int:
    app = QApplication.instance() or QApplication([])
    theme.apply(app)
    failed = run(app)
    print("UI FIT:", "PASS" if failed == 0 else "FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
"""Runtime path helpers (source vs. PyInstaller-frozen)."""
from __future__ import annotations

import sys
from pathlib import Path


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def program_dir() -> Path:
    """程序所在目录：冻结后是 **exe 所在目录**（不是 ``_MEIPASS`` 解包临时目录），
    源码运行时是仓库根目录。便携模式的判断基准就是这个目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    return app_dir().joinpath(*parts)
"""SHA-256 based decode/download ledger.

Prevents (a) re-decrypting a file we already produced and (b) downloading
the same track twice.  Kept as a small JSON file next to the settings.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

__all__ = ["HashCache"]


class HashCache:
    def __init__(self, path: "str | Path") -> None:
        self.path = Path(path)
        self._data: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        try:
            if self.path.is_file():
                self._data = json.loads(self.path.read_text("utf-8"))
                if not isinstance(self._data, dict):
                    self._data = {}
        except Exception:  # noqa: BLE001
            self._data = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), "utf-8")
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001
            pass

    def get(self, digest: str) -> Optional[dict]:
        return self._data.get(digest)

    def add(self, digest: str, *, out_path: str, mode: str, fmt: str, source: str = "") -> None:
        self._data[digest] = {
            "out": out_path, "mode": mode, "fmt": fmt,
            "source": source, "at": int(time.time()),
        }
        self.save()

    def __len__(self) -> int:
        return len(self._data)
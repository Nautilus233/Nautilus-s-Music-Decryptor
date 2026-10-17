"""Persistent settings (JSON next to the user's AppData)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "MusicDecryptor"


def default_config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or str(Path.home())
    return Path(base) / APP_NAME


@dataclass
class Settings:
    output_dir: str = ""
    quality: str = "mp3"                                   # mp3 | flac | wav
    sources: list = field(default_factory=lambda: ["local", "kuwo", "kugou", "aggregator"])
    local_dirs: list = field(default_factory=list)
    aggregator_base: str = "https://music-api.gdstudio.xyz/api.php"
    aggregator_provider: str = "netease"
    timeout: float = 12.0
    retries: int = 3
    min_bitrate: int = 128
    write_metadata: bool = True
    fetch_cover: bool = True
    overwrite: bool = False
    keep_original: bool = True
    enable_fallback: bool = True
    remember_hashes: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        allowed = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (data or {}).items() if k in allowed})


class AppConfig:
    def __init__(self, path: "str | Path | None" = None) -> None:
        self.dir = default_config_dir()
        self.path = Path(path) if path else self.dir / "settings.json"
        self.cache_path = self.path.parent / "hashcache.json"
        self.settings = Settings()
        self.load()

    def load(self) -> None:
        try:
            if self.path.is_file():
                self.settings = Settings.from_dict(json.loads(self.path.read_text("utf-8")))
        except Exception:  # noqa: BLE001
            self.settings = Settings()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.settings.to_dict(), ensure_ascii=False, indent=2), "utf-8")
        except Exception:  # noqa: BLE001
            pass
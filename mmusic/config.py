"""Persistent settings (JSON in %APPDATA%, or beside the exe in portable mode)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .paths import program_dir

APP_NAME = "MusicDecryptor"

#: 便携模式的触发标记：程序目录下存在 `settings.json` 或 `portable` 即启用
PORTABLE_MARKERS = ("settings.json", "portable")


def default_config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or str(Path.home())
    return Path(base) / APP_NAME


def resolve_config_dir(program_dir_path: "str | Path | None" = None) -> Path:
    """配置目录：**便携模式优先**，否则 ``%APPDATA%\MusicDecryptor``。

    便携模式 = 程序目录（冻结后即 exe 所在目录）下已经有 ``settings.json``，
    或放了一个空的 ``portable`` 标记文件/文件夹。此时配置与哈希缓存都落在
    程序目录，**随 exe 一起搬走**（换机器 / 拷给同事时凭证与设置跟着走）。
    没有标记时行为与以前完全一致。
    """
    try:
        base = Path(program_dir_path) if program_dir_path is not None else program_dir()
    except (TypeError, OSError):
        return default_config_dir()
    try:
        if base.is_dir() and any((base / name).exists() for name in PORTABLE_MARKERS):
            return base
    except OSError:
        pass
    return default_config_dir()


@dataclass
class Settings:
    output_dir: str = ""
    quality: str = "mp3"                                   # mp3 | flac | wav
    # 空列表 = 使用源清单（pack.py）的默认顺序与开关。
    # 注意：这里**不能**写死一份名单 —— 否则每次新增音源都会被它静默排除。
    sources: list = field(default_factory=list)
    local_dirs: list = field(default_factory=list)
    # 手动补链：用户自行提供的直链或本地路径（不是开关，由 CLI/界面动作写入）
    manual_links: list = field(default_factory=list)
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
    # QQ 音乐登录态（仅本机保存）：用于“按需静默获取 musicex 的 ekey”。
    # 等价于登录凭证 —— 日志永不输出、只用于官方 GetEVkey 调用；留空则功能关闭。
    qqmusic_uin: str = ""
    qqmusic_key: str = ""
    # 拖入/添加文件后自动开始处理（同事最省事；可在 设置→行为 关闭）
    auto_start: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        allowed = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (data or {}).items() if k in allowed})


class AppConfig:
    def __init__(self, path: "str | Path | None" = None) -> None:
        self.dir = resolve_config_dir()
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
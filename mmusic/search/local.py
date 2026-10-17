"""Offline source: search a local library of own / CC0 audio files.

Complements the online sources: if a track is already in the user's local
folder (e.g. a public-domain / CC0 sound library, or previously downloaded
files) it is preferred, since it needs no network at all.
"""
from __future__ import annotations

from pathlib import Path

from ..match import similarity, split_filename_raw
from ..model import Track
from .base import SearchSource

_AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".aac", ".opus", ".wma", ".ape"}


class LocalSource(SearchSource):
    name = "local"
    label = "本地曲库(CC0/自建)"
    offline = True

    def _dirs(self) -> list[Path]:
        dirs = self.settings.get("local_dirs") or []
        if isinstance(dirs, str):
            dirs = [dirs]
        return [Path(d) for d in dirs if str(d).strip()]

    def search(self, query: str, *, quality: str = "mp3", limit: int = 10) -> list[Track]:
        results: list[Track] = []
        query_l = query.lower()
        for base in self._dirs():
            if not base.is_dir():
                continue
            for path in base.rglob("*"):
                if path.suffix.lower() not in _AUDIO_EXTS:
                    continue
                stem = path.stem
                if query_l in stem.lower() or similarity(query, stem) >= 0.55:
                    # 本地文件没有标签，只能从文件名拆（**保留括号**，否则
                    # "伴奏/电台版"这类版本标记会被剥掉、版本过滤就失效了）：
                    #   1) "歌手硬门槛"对本地曲库同样生效（同名翻唱会被挡掉）；
                    #   2) 输出名 artist + " - " + title 恰好还原原始文件名。
                    # 只有歌名的文件拆出 artist 为空 -> 不做歌手判断、不受影响。
                    parsed_title, parsed_artist = split_filename_raw(stem)
                    results.append(Track(
                        source=self.name,
                        title=parsed_title or stem,
                        artist=parsed_artist,
                        ext=path.suffix.lstrip(".").lower(),
                        size=path.stat().st_size,
                        url="",                       # filled by resolve() as a file: URI
                        raw={"path": str(path)},
                    ))
        results.sort(key=lambda t: similarity(query, t.title), reverse=True)
        return results[:limit]

    def resolve(self, track: Track, *, quality: str = "mp3") -> Track:
        path = track.raw.get("path") or track.url
        if not path or not Path(path).exists():
            from ..errors import NetworkError
            raise NetworkError("本地文件不存在", detail=str(path))
        track.raw["path"] = path
        track.url = Path(path).resolve().as_uri()      # 规范的 file:// URI
        return track
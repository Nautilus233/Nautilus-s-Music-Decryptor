"""Tag / cover read-write helpers (mutagen, optional).

Everything degrades gracefully: if `mutagen` is unavailable or a file
format is unsupported, the audio itself is still written untouched.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

try:
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TIT2, TPE1
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggvorbis import OggVorbis
    HAVE_MUTAGEN = True
except Exception:  # noqa: BLE001
    HAVE_MUTAGEN = False

__all__ = ["HAVE_MUTAGEN", "write_tags", "read_tags", "read_source_meta",
           "guess_image_mime"]


def guess_image_mime(data: bytes) -> str:
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def _split_artists(artist: str) -> list[str]:
    return [a.strip() for a in artist.replace("、", "/").split("/") if a.strip()]


def write_tags(path: "str | Path", *, title: str = "", artist: str = "",
               album: str = "", cover: bytes = b"") -> bool:
    if not HAVE_MUTAGEN:
        return False
    p = str(path)
    ext = Path(p).suffix.lower().lstrip(".")
    try:
        if ext == "mp3":
            try:
                tags = ID3(p)
            except ID3NoHeaderError:
                tags = ID3()
            if title:
                tags.add(TIT2(encoding=3, text=title))
            if artist:
                tags.add(TPE1(encoding=3, text=_split_artists(artist)))
            if album:
                tags.add(TALB(encoding=3, text=album))
            if cover:
                tags.add(APIC(encoding=0, mime=guess_image_mime(cover), type=3, desc="Cover", data=cover))
            tags.save(p)
        elif ext == "flac":
            audio = FLAC(p)
            if title:
                audio["title"] = [title]
            if artist:
                audio["artist"] = _split_artists(artist)
            if album:
                audio["album"] = [album]
            if cover:
                pic = Picture()
                pic.type = 3
                pic.mime = guess_image_mime(cover)
                pic.data = cover
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()
        elif ext in ("ogg", "opus"):
            audio = OggVorbis(p)
            if title:
                audio["title"] = [title]
            if artist:
                audio["artist"] = _split_artists(artist)
            if album:
                audio["album"] = [album]
            audio.save()
        elif ext in ("m4a", "mp4", "mmp4"):
            audio = MP4(p)
            if title:
                audio["\xa9nam"] = [title]
            if artist:
                audio["\xa9ART"] = _split_artists(artist)
            if album:
                audio["\xa9alb"] = [album]
            if cover:
                fmt = MP4Cover.FORMAT_PNG if guess_image_mime(cover) == "image/png" else MP4Cover.FORMAT_JPEG
                audio["covr"] = [MP4Cover(cover, imageformat=fmt)]
            audio.save()
        else:
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def _cover_of(parsed) -> bytes:
    """从已解析的 mutagen 对象里取第一张内嵌封面（没有则 b""）。"""
    try:
        if isinstance(parsed, FLAC):
            pics = getattr(parsed, "pictures", None) or []
            return bytes(pics[0].data) if pics else b""
        if isinstance(parsed, MP4):
            covr = (parsed.tags or {}).get("covr")
            return bytes(covr[0]) if covr else b""
        if isinstance(parsed, OggVorbis):
            blobs = parsed.get("metadata_block_picture") or []
            if blobs:
                return bytes(Picture(base64.b64decode(blobs[0])).data)
            return b""
        tags = getattr(parsed, "tags", None)
        if tags is not None:                      # ID3 系（mp3 / aiff …）
            for key in list(tags.keys()):
                if str(key).startswith("APIC"):
                    return bytes(tags[key].data)
    except Exception:  # noqa: BLE001 - 封面读不出不算错，静默放弃
        pass
    return b""


def read_source_meta(data: bytes) -> "tuple[dict, bytes]":
    """从**已解密的音频字节**里读出源文件自带的标签与封面（纯本地、不联网）。

    用途：离线解密路径不要把源里正确的 ``title`` 覆盖成文件名，源里若内嵌封面
    也要在转码后带过去。只做头部解析（毫秒级），任何失败都返回 ``({}, b"")`` —— 
    绝不因为标签问题影响解密产物。
    """
    if not HAVE_MUTAGEN or not data:
        return {}, b""
    tags: dict = {}
    try:
        easy = MutagenFile(io.BytesIO(data), easy=True)
        if easy is not None and easy.tags:
            for key in ("title", "artist", "album"):
                value = easy.get(key)
                if value:
                    text = str(value[0]).strip()
                    if text:
                        tags[key] = text
    except Exception:  # noqa: BLE001
        tags = {}
    try:
        parsed = MutagenFile(io.BytesIO(data))
    except Exception:  # noqa: BLE001
        return tags, b""
    return tags, (_cover_of(parsed) if parsed is not None else b"")


def read_tags(path: "str | Path") -> dict:
    if not HAVE_MUTAGEN:
        return {}
    try:
        f = MutagenFile(str(path), easy=True)
        if f is None:
            return {}
        out = {}
        for key in ("title", "artist", "album", "date", "genre"):
            if key in f and f[key]:
                out[key] = f[key][0]
        return out
    except Exception:  # noqa: BLE001
        return {}
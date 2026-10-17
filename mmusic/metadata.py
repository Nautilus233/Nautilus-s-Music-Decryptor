"""Tag / cover read-write helpers (mutagen, optional).

Everything degrades gracefully: if `mutagen` is unavailable or a file
format is unsupported, the audio itself is still written untouched.
"""
from __future__ import annotations

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

__all__ = ["HAVE_MUTAGEN", "write_tags", "read_tags", "guess_image_mime"]


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
"""Download + safety/quality validation of fallback audio.

A downloaded byte-stream is only accepted once it passes:
  * a minimum size,
  * a real audio magic-header check (rejects HTML/JSON error pages),
  * a bitrate estimate (from the MPEG frame header, FLAC STREAMINFO, or
    size/duration) meeting a configurable floor.

The *actual* format detected from the header always wins over whatever
extension the source claimed.
"""
from __future__ import annotations

from dataclasses import dataclass

from .duration import flac_streaminfo, mp3_frame_info
from .model import Track
from .sniff import looks_like_audio

__all__ = ["ValidationResult", "validate", "mp3_info", "flac_info"]

@dataclass
class ValidationResult:
    ok: bool
    ext: str = ""
    bitrate: int = 0
    size: int = 0
    reason: str = ""


def mp3_info(data: bytes) -> "dict | None":
    """MPEG frame header info (shared with the duration module)."""
    return mp3_frame_info(data)


def flac_info(data: bytes) -> "dict | None":
    """FLAC STREAMINFO plus the bitrate derived from the file length."""
    info = flac_streaminfo(data)
    if info is None:
        return None
    duration = info.get("duration") or 0
    return {
        "sample_rate": info["sample_rate"],
        "channels": info["channels"],
        "bits": info["bits"],
        "duration": int(duration),
        "bitrate": int(len(data) * 8 / duration / 1000) if duration else 0,
    }


def validate(data: bytes, expected_ext: str = "", *, expected_bitrate: int = 0,
             duration: int = 0, min_bitrate: int = 0, min_bytes: int = 16 * 1024) -> ValidationResult:
    size = len(data)
    if size < min_bytes:
        return ValidationResult(False, expected_ext, 0, size, f"文件过小（{size} 字节）")

    head = data[:16]
    if head[:1] in (b"<", b"{") or head[:5].lower() == b"<!doc":
        return ValidationResult(False, expected_ext, 0, size, "返回的是网页/JSON，不是音频")

    ext = looks_like_audio(head) or ""
    bitrate = 0
    if ext == "mp3":
        info = mp3_info(data)
        if info:
            bitrate = info["bitrate"]
    elif ext == "flac":
        info = flac_info(data)
        if info:
            bitrate = info["bitrate"]
            if info.get("duration"):
                duration = duration or info["duration"]

    if not ext:
        return ValidationResult(False, expected_ext, 0, size, "文件头不是已知的音频格式")

    if not bitrate and duration:
        bitrate = int(size * 8 / duration / 1000)
    if not bitrate and expected_bitrate:
        bitrate = expected_bitrate

    if min_bitrate and bitrate and bitrate < min_bitrate:
        return ValidationResult(False, ext, bitrate, size, f"码率过低（{bitrate} kbps < {min_bitrate}）")

    return ValidationResult(True, ext, bitrate, size, "")
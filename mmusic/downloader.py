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

from .model import Track
from .sniff import looks_like_audio

__all__ = ["ValidationResult", "validate", "mp3_info", "flac_info"]

_MP3_BR_V1_L3 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_MP3_BR_V2_L3 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
_MP3_SR = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


@dataclass
class ValidationResult:
    ok: bool
    ext: str = ""
    bitrate: int = 0
    size: int = 0
    reason: str = ""


def _skip_id3(data: bytes) -> int:
    if data[:3] == b"ID3" and len(data) >= 10:
        size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        return 10 + size
    return 0


def mp3_info(data: bytes) -> "dict | None":
    off = _skip_id3(data)
    limit = min(len(data) - 4, off + 200000)
    i = off
    while i < limit:
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            b1, b2 = data[i + 1], data[i + 2]
            ver = (b1 >> 3) & 0x03
            layer = (b1 >> 1) & 0x03
            brx = (b2 >> 4) & 0x0F
            srx = (b2 >> 2) & 0x03
            if ver != 1 and layer == 1 and brx not in (0, 15) and srx != 3:
                table = _MP3_BR_V1_L3 if ver == 3 else _MP3_BR_V2_L3
                return {
                    "bitrate": table[brx],
                    "sample_rate": _MP3_SR[ver][srx],
                    "mpeg": ver,
                    "layer": 3,
                }
        i += 1
    return None


def flac_info(data: bytes) -> "dict | None":
    if data[:4] != b"fLaC" or len(data) < 42:
        return None
    # first metadata block must be STREAMINFO (type 0), 34 bytes
    if (data[4] & 0x7F) != 0:
        return None
    s = data[8:42]
    packed = int.from_bytes(s[10:18], "big")
    sample_rate = (packed >> 44) & 0xFFFFF
    channels = ((packed >> 41) & 0x7) + 1
    bits = ((packed >> 36) & 0x1F) + 1
    total_samples = packed & 0xFFFFFFFFF
    duration = total_samples / sample_rate if sample_rate else 0
    bitrate = int(len(data) * 8 / duration / 1000) if duration else 0
    return {"sample_rate": sample_rate, "channels": channels, "bits": bits,
            "duration": int(duration), "bitrate": bitrate}


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
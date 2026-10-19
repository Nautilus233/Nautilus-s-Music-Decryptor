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
from .duration import audio_duration, format_seconds
from .model import Track
from .sniff import looks_like_audio

__all__ = ["ValidationResult", "validate", "check_completeness",
           "mp3_info", "flac_info"]

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

def check_completeness(data: bytes, *, reference: float = 0.0, tolerance: float = 0.0,
                       min_seconds: float = 45.0, measured: float = 0.0,
                       over_tolerance: float = 0.0
                       ) -> "tuple[bool, str, float]":
    """下载**之后**的完整度门禁：把提示音 / 试听 / 截短挡在播出门外。

    返回 ``(是否通过, 原因, 实测秒数)``。

    为什么必须在下载后再查一次：文件头与码率校验挡不住"这是一段 11 秒的
    提示音"——那是酷我对未授权请求返回的东西，它本身是合法 MPEG 文件。
    实测来源：免费接口对某首歌返回 177 KB / 11.1 秒的提示音，而同一首歌的
    完整文件是 4.2 MB。测不出时长时不额外否决（交给上层"未比对"逻辑），
    但只要测得出，就必须同时满足三条：不短于下限、不短于探测时测得的自身
    时长（防止服务端"探测给好的、下载给差的"）、不短于跨平台参考时长。
    """
    # Ogg 的时长写在**最后一页**的 granule、非 faststart 的 m4a 把 moov 放在文件尾：
    # 只传 data 会永远测不出（门禁形同虚设），所以把文件尾一并交给解析器。
    _tail = data[-131072:] if len(data) > 131072 else b""
    seconds = audio_duration(data, len(data), _tail) or 0.0
    if not seconds:
        return True, "", 0.0
    if seconds < min_seconds:
        return False, f"只有 {format_seconds(seconds)}，疑似提示音/试听片段", seconds
    if measured and seconds < measured - 5.0:
        return False, (f"下载结果 {format_seconds(seconds)} 短于探测时测得的 "
                       f"{format_seconds(measured)}（服务端返回了不同文件）"), seconds
    if reference and seconds < reference - tolerance:
        return False, (f"下载结果 {format_seconds(seconds)} 短于参考时长 "
                       f"{format_seconds(reference)}"), seconds
    if over_tolerance and reference and seconds > reference + over_tolerance:
        return False, (f"下载结果 {format_seconds(seconds)} 长于参考时长 "
                       f"{format_seconds(reference)}（疑似合辑/串烧）"), seconds
    return True, "", seconds


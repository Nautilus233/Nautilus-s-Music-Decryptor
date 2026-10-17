"""Netease Cloud Music `.ncm` container decoder.

Container layout (after the 8-byte magic ``CTENFDAM``)::

    2   unknown
    4   key length (u32 LE)      -> key blob, each byte XOR 0x64, AES-128-ECB(CORE_KEY)
    4   meta length (u32 LE)     -> meta blob, each byte XOR 0x63,
                                    "163 key(Don't modify):" + base64, AES-128-ECB(META_KEY)
    4   CRC32
    5   unknown
    4   image size (u32 LE)
    N   image (album art)
    ..  audio data, byte i XOR key_box[i & 0xff]

Because the audio stream cipher is *index dependent* we cannot blindly
scan for the audio signature - but we can test candidate offsets, since
only the correct one decrypts to a real audio header.  This makes the
decoder robust against the small layout differences between
implementations.
"""
from __future__ import annotations

import base64
import json
import struct

from ..constants import (NCM_CORE_KEY, NCM_KEY_XOR, NCM_MAGIC, NCM_META_KEY,
                         NCM_META_XOR)
from ..errors import DecryptionFailedError, UnsupportedFormatError
from ..model import AudioResult
from ..sniff import looks_like_audio
from ..utils import xor_repeating
from .aes import aes_ecb_decrypt, pkcs7_unpad

__all__ = ["NcmDecoder"]

_META_PREFIX_LEN = 22  # len("163 key(Don't modify):")


class NcmDecoder:
    KEYBOX_PERIOD = 256

    def __init__(self, data: bytes, *, scan_window: int = 96) -> None:
        if not data.startswith(NCM_MAGIC):
            raise UnsupportedFormatError("不是有效的 NCM 文件（缺少 CTENFDAM 头部）")
        self.raw = data
        self.offset = 10
        self._scan_window = scan_window

    # ------------------------------------------------------------------ #
    def _u32(self, at: int) -> int:
        return struct.unpack_from("<I", self.raw, at)[0]

    def _read_u32(self) -> int:
        value = self._u32(self.offset)
        self.offset += 4
        return value

    def _key_data(self) -> bytes:
        length = self._read_u32()
        if length <= 0 or length % 16 != 0 or self.offset + length > len(self.raw):
            raise DecryptionFailedError("NCM 密钥块长度异常")
        blob = bytes(b ^ NCM_KEY_XOR for b in self.raw[self.offset:self.offset + length])
        self.offset += length
        plain = pkcs7_unpad(aes_ecb_decrypt(NCM_CORE_KEY, blob))
        if len(plain) <= 17:
            raise DecryptionFailedError("NCM 密钥解析失败")
        return plain[17:]

    def _key_box(self) -> bytes:
        key = self._key_data()
        n = len(key)
        box = bytearray(range(256))
        j = 0
        for i in range(256):
            j = (box[i] + j + key[i % n]) & 0xFF
            box[i], box[j] = box[j], box[i]
        out = bytearray(256)
        for i in range(256):
            ii = (i + 1) & 0xFF
            si = box[ii]
            sj = box[(ii + si) & 0xFF]
            out[i] = box[(si + sj) & 0xFF]
        return bytes(out)

    def _meta(self) -> dict:
        length = self._read_u32()
        if length == 0:
            return {}
        blob = bytes(b ^ NCM_META_XOR for b in self.raw[self.offset:self.offset + length])
        self.offset += length
        try:
            text = blob.decode("utf-8", "ignore")
            b64 = text[_META_PREFIX_LEN:].strip()
            b64 += "=" * (-len(b64) % 4)
            plain = pkcs7_unpad(aes_ecb_decrypt(NCM_META_KEY, base64.b64decode(b64)))
            _, _, rest = plain.decode("utf-8", "ignore").partition(":")
            obj = json.loads(rest)
            if isinstance(obj, dict) and "mainMusic" in obj:      # DJ / podcast
                obj = obj["mainMusic"]
            if not isinstance(obj, dict):
                return {}
            pic = obj.get("albumPic")
            if pic and isinstance(pic, str):
                obj["albumPic"] = "https://" + pic.split("//", 1)[-1].lstrip("/") + "?param=500y500"
            return obj
        except Exception:
            return {}

    # ------------------------------------------------------------------ #
    def _audio_start(self, keybox: bytes) -> int:
        base = self.offset
        size = len(self.raw)

        def looks(off: int) -> bool:
            if off < base or off + 16 > size:
                return False
            head = bytes(self.raw[off + k] ^ keybox[k & 0xFF] for k in range(16))
            return looks_like_audio(head) is not None

        candidates = []
        if base + 9 <= size:
            candidates.append(base + 13 + self._u32(base + 5))   # class A layout
        if base + 13 <= size:
            candidates.append(base + 13 + self._u32(base + 9))   # class B layout
        for off in candidates:
            if looks(off):
                return off

        limit = min(base + self._scan_window, size - 16)
        for off in range(base, limit):
            if looks(off):
                return off

        for off in candidates:
            if base <= off < size:
                return off
        raise DecryptionFailedError("无法定位 NCM 音频数据区（文件可能已损坏）")

    # ------------------------------------------------------------------ #
    def decode(self) -> AudioResult:
        keybox = self._key_box()
        meta = self._meta()
        start = self._audio_start(keybox)
        audio = xor_repeating(self.raw[start:], keybox)

        sniffed = looks_like_audio(audio[:16])
        declared = str(meta.get("format") or "").lower()
        if declared == "mp4":
            declared = "m4a"
        ext = sniffed or declared or "mp3"

        artists: list[str] = []
        for entry in meta.get("artist") or []:
            if isinstance(entry, (list, tuple)) and entry:
                artists.append(str(entry[0]))
            elif isinstance(entry, str):
                artists.append(entry)

        return AudioResult(
            audio=audio,
            ext=ext,
            title=str(meta.get("musicName") or ""),
            artist=" / ".join(artists),
            album=str(meta.get("album") or ""),
            cover_url=str(meta.get("albumPic") or ""),
            meta=meta,
        )
"""QQ Music QMC / MGG / MFLAC decoder.

Three cipher families are covered, matching the public reference
implementation (``unlock-music`` ``qmc_cipher.ts`` / ``qmc.ts`` / ``qmc_key.ts``):

* **QMCv1** - static 256-byte stream cipher (``.bkc*``, ``.tkm`` and the
  hex-named variants).
* **QMCv2 map** - a keyed byte-map cipher (keys <= 300 bytes).
* **QMCv2 RC4** - a segmented keyed stream cipher (keys > 300 bytes).

The key lives in the file trailer:

* ``"QTag"`` trailer  -> embedded key + song id  (offline decryptable)
* ``"STag"`` trailer  -> *no* embedded key (needs the online / client key)
* ``"musicex\0"`` tail (2026) -> unencrypted tail with song_mid/filename
  but **no key**; decodes offline only when a local ekey is provided
  (``ekeys/<mid>__<file>.txt``), otherwise raises NeedsOnlineError
* legacy trailer      -> last 4 bytes = little-endian key length

Anything we cannot obtain a key for offline raises :class:`NeedsOnlineError`,
which the pipeline turns into the fallback-download flow.  Decoded results are
additionally checked against the audio magic table; a mismatch raises
:class:`DecryptionFailedError` instead of silently writing garbage.
"""
from __future__ import annotations

import base64
import math
import struct

from ..constants import (QMC_ENC_V2_PREFIX, QMC_EXT_MAP, QMC_MAP_MAX_KEY_LEN,
                         QMC_MIX_KEY1, QMC_MIX_KEY2, QMC_STATIC_CIPHER_BOX,
                         QMC_TAG_MUSICEX, QMC_TAG_QTAG, QMC_TAG_STAG)
from ..errors import DecryptionFailedError, NeedsOnlineError
from ..model import AudioResult
from ..sniff import looks_like_audio
from .musicex import find_ekey_text, parse_tail as parse_musicex_tail
from .tea import decrypt_tencent_tea

__all__ = ["QmcDecoder", "qmc_derive_key", "simple_make_key"]


# --------------------------------------------------------------------- #
# key derivation
# --------------------------------------------------------------------- #
def simple_make_key(salt: int, length: int) -> bytes:
    out = bytearray(length)
    for i in range(length):
        out[i] = int(abs(math.tan(salt + i * 0.1)) * 100.0) & 0xFF
    return bytes(out)


def _decrypt_v2_key(key: bytes) -> bytes:
    if len(key) < 18 or key[:18] != QMC_ENC_V2_PREFIX:
        return key
    out = decrypt_tencent_tea(key[18:], QMC_MIX_KEY1)
    out = decrypt_tencent_tea(out, QMC_MIX_KEY2)
    decoded = bytearray(base64.b64decode(bytes(out)))
    if len(decoded) < 16:
        raise DecryptionFailedError("EncV2 密钥解码失败")
    return bytes(decoded)


def qmc_derive_key(raw: bytes) -> bytes:
    """Turn the base64 key blob stored in the trailer into a cipher key."""
    try:
        raw_dec = bytearray(base64.b64decode(bytes(raw)))
    except Exception as exc:  # noqa: BLE001
        raise DecryptionFailedError("QMC 密钥 base64 解码失败") from exc
    if len(raw_dec) < 16:
        raise DecryptionFailedError("QMC 密钥长度不足")

    raw_dec = bytearray(_decrypt_v2_key(bytes(raw_dec)))

    simple = simple_make_key(106, 8)
    tea_key = bytearray(16)
    for i in range(8):
        tea_key[i << 1] = simple[i]
        tea_key[(i << 1) + 1] = raw_dec[i]
    try:
        sub = decrypt_tencent_tea(bytes(raw_dec[8:]), bytes(tea_key))
    except ValueError as exc:
        # TEA 的零校验失败意味着"这段 trailer 不是合法密钥"（文件被损坏/被改写/
        # 根本不是 QTag）。以前这里漏出 ValueError（非可预期异常），fuzz 一碰就中；
        # 现在统一成 DecryptionFailedError，交给上层如实报错或转备用搜索。
        raise DecryptionFailedError(f"QMC 密钥解析失败（密钥块不合法：{exc}）") from exc

    out = bytearray(8 + len(sub))
    out[:8] = raw_dec[:8]
    out[8:] = sub
    return bytes(out)


# --------------------------------------------------------------------- #
# stream ciphers
# --------------------------------------------------------------------- #
class QmcStaticCipher:
    """QMCv1 static cipher."""

    def decrypt(self, buf: bytearray, offset: int = 0) -> None:
        box = QMC_STATIC_CIPHER_BOX
        for j in range(len(buf)):
            i = offset + j
            if i > 0x7FFF:
                i %= 0x7FFF
            buf[j] ^= box[(i * i + 27) & 0xFF]


class QmcMapCipher:
    """QMCv2 byte-map cipher."""

    def __init__(self, key: bytes) -> None:
        if not key:
            raise DecryptionFailedError("QMC map 密钥为空")
        self.key = key
        self.n = len(key)

    @staticmethod
    def _rotate(value: int, bits: int) -> int:
        rotate = (bits + 4) % 8
        return (((value << rotate) | (value >> rotate)) & 0xFF)

    def _mask(self, offset: int) -> int:
        if offset > 0x7FFF:
            offset %= 0x7FFF
        idx = (offset * offset + 71214) % self.n
        return self._rotate(self.key[idx], idx & 0x7)

    def decrypt(self, buf: bytearray, offset: int = 0) -> None:
        for j in range(len(buf)):
            buf[j] ^= self._mask(offset + j)


class QmcRC4Cipher:
    """QMCv2 segmented RC4-style cipher."""

    FIRST_SEGMENT_SIZE = 0x80
    SEGMENT_SIZE = 5120

    def __init__(self, key: bytes) -> None:
        if not key:
            raise DecryptionFailedError("QMC RC4 密钥为空")
        self.key = key
        self.n = len(key)

        s = bytearray(self.n)
        for i in range(self.n):
            s[i] = i & 0xFF
        j = 0
        for i in range(self.n):
            j = (s[i] + j + key[i % self.n]) % self.n
            s[i], s[j] = s[j], s[i]
        self.s = bytes(s)

        h = 1
        for i in range(self.n):
            value = key[i]
            if value == 0:
                continue
            nxt = (h * value) & 0xFFFFFFFF
            if nxt == 0 or nxt <= h:
                break
            h = nxt
        self.hash = h

    def _segment_key(self, sid: int) -> int:
        seed = self.key[sid % self.n]
        if seed == 0:
            return 0
        idx = int((self.hash / ((sid + 1) * seed)) * 100.0)
        return idx % self.n

    def _enc_first(self, buf: bytearray, pos: int, length: int, offset: int) -> None:
        for i in range(length):
            buf[pos + i] ^= self.key[self._segment_key(offset + i)]

    def _enc_segment(self, buf: bytearray, pos: int, length: int, offset: int) -> None:
        s = bytearray(self.s)
        skip = (offset % self.SEGMENT_SIZE) + self._segment_key(offset // self.SEGMENT_SIZE)
        j = 0
        k = 0
        for i in range(-skip, length):
            j = (j + 1) % self.n
            k = (s[j] + k) % self.n
            s[j], s[k] = s[k], s[j]
            if i >= 0:
                buf[pos + i] ^= s[(s[j] + s[k]) % self.n]

    def decrypt(self, buf: bytearray, offset: int = 0) -> None:
        n = len(buf)
        pos = 0
        if offset < self.FIRST_SEGMENT_SIZE:
            length = min(n, self.FIRST_SEGMENT_SIZE - offset)
            self._enc_first(buf, pos, length, offset)
            pos += length
            offset += length
            if pos == n:
                return
        if offset % self.SEGMENT_SIZE != 0:
            length = min(self.SEGMENT_SIZE - (offset % self.SEGMENT_SIZE), n - pos)
            self._enc_segment(buf, pos, length, offset)
            pos += length
            offset += length
            if pos == n:
                return
        while n - pos > self.SEGMENT_SIZE:
            self._enc_segment(buf, pos, self.SEGMENT_SIZE, offset)
            pos += self.SEGMENT_SIZE
            offset += self.SEGMENT_SIZE
        if n - pos > 0:
            self._enc_segment(buf, pos, n - pos, offset)


# --------------------------------------------------------------------- #
# container handler
# --------------------------------------------------------------------- #
class QmcDecoder:
    def __init__(self, data: bytes, ext: str, *,
                 src_path: "str | None" = None) -> None:
        self.file = data
        self.size = len(data)
        self.ext = (ext or "").lower().lstrip(".")
        self.src_path = src_path
        self.cipher = None
        self.audio_size = 0
        self.song_id: "int | None" = None
        self.song_mid: "str | None" = None
        self.out_ext = QMC_EXT_MAP.get(self.ext, ("qmc2", "mp3"))[1]
        self.needs_online = False
        self.note = ""
        self._analyze()

    # ------------------------------------------------------------------ #
    def _make_cipher(self, key: bytes):
        if len(key) > QMC_MAP_MAX_KEY_LEN:
            return QmcRC4Cipher(key)
        return QmcMapCipher(key)

    def _analyze_musicex(self) -> None:
        """2026 新版 musicex 尾部：文件内无密钥；本地有 ekey 才能离线解码。"""
        info = parse_musicex_tail(self.file[-4096:], self.size)
        if info is None:
            raise DecryptionFailedError("musicex 尾部解析失败（结构异常）")
        self.audio_size = info.audio_size
        self.song_mid = info.song_mid
        ekey_text = find_ekey_text(self.src_path, info.song_mid, info.filename)
        if not ekey_text:
            self.needs_online = True
            self.note = ("QQ音乐新版 musicex 加密：文件内无密钥，需要账号 ekey"
                         "（在线获取后放入 ekeys 目录即可离线解码）")
            return
        try:
            key = qmc_derive_key(ekey_text.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 坏 ekey 要给出可读错误
            raise DecryptionFailedError(
                "musicex ekey 解析失败（密钥无效/已过期/被截断）",
                detail=str(exc)) from exc
        self.cipher = self._make_cipher(key)
        self.note = f"musicex（本地 ekey 已加载：{info.song_mid}）"

    def _analyze(self) -> None:
        if self.size < 16:
            raise DecryptionFailedError("QMC 文件过小")

        if self.file[-8:] == QMC_TAG_MUSICEX:
            self._analyze_musicex()
            return

        tail = self.file[-4:]
        if tail == QMC_TAG_STAG:
            self.needs_online = True
            self.note = "QQ音乐新版加密（STag：文件内无密钥，需联网/客户端密钥）"
            return

        if tail == QMC_TAG_QTAG:
            key_size = struct.unpack(">I", self.file[-8:-4])[0]
            audio_size = self.size - key_size - 8
            if audio_size <= 0:
                raise DecryptionFailedError("QTag 长度字段异常")
            raw_key = self.file[audio_size:self.size - 8]
            comma = raw_key.find(b",")
            if comma < 0:
                raise DecryptionFailedError("QTag 密钥解析失败")
            key = qmc_derive_key(raw_key[:comma])
            idbuf = raw_key[comma + 1:]
            end = idbuf.find(b",")
            if end >= 0:
                try:
                    self.song_id = int(idbuf[:end].decode("utf-8", "ignore"))
                except ValueError:
                    self.song_id = None
            self.audio_size = audio_size
            self.cipher = self._make_cipher(key)
            return

        # legacy trailer: last 4 bytes are the little-endian key length
        key_size = struct.unpack("<I", self.file[-4:])[0]
        if key_size < 0x400 and 0 < key_size + 4 < self.size:
            self.audio_size = self.size - key_size - 4
            raw_key = self.file[self.audio_size:self.size - 4]
            key = qmc_derive_key(raw_key)
            self.cipher = self._make_cipher(key)
        else:
            # no key present -> QMCv1 static cipher
            self.audio_size = self.size
            self.cipher = QmcStaticCipher()

    # ------------------------------------------------------------------ #
    def decode(self) -> AudioResult:
        if self.needs_online:
            raise NeedsOnlineError(self.note, detail="触发备用搜索下载")
        if self.cipher is None or self.audio_size <= 0:
            raise DecryptionFailedError("QMC 解码失败：找不到有效密钥或音频长度")

        buf = bytearray(self.file[:self.audio_size])
        self.cipher.decrypt(buf, 0)
        audio = bytes(buf)

        # 安全网：解密结果必须是可识别的音频头。错误密钥 / 未知新格式 / 被误判为
        # “静态加密”的文件都会在这里现形 —— 宁可直接失败，也绝不静默写出垃圾。
        sniffed = looks_like_audio(audio[:16])
        if sniffed is None:
            raise DecryptionFailedError(
                "解密结果不是有效音频（密钥或加密格式不匹配，未写出文件）",
                detail=f"head={audio[:8].hex()}")
        ext = sniffed
        if ext == "mmp4":
            ext = "m4a"
        meta: "dict" = {}
        if self.song_id:
            meta["songId"] = self.song_id
        if self.note:                 # 例如“musicex（本地 ekey 已加载…）”，由流水线写进日志
            meta["note"] = self.note
        return AudioResult(audio=audio, ext=ext, meta=meta)
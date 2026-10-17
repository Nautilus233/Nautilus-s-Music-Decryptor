"""Synthetic-encryption helpers used by the self-tests.

These *create* .ncm / .qmc containers around a fake payload so the decoder
paths can be exercised end-to-end without shipping any real audio.  They
implement the matching encryption side of the published algorithms.
"""
from __future__ import annotations

import base64
import json
import struct

from mmusic.constants import (NCM_CORE_KEY, NCM_KEY_XOR, NCM_MAGIC,
                              NCM_META_KEY, NCM_META_XOR, QMC_STATIC_CIPHER_BOX)
from mmusic.crypto.aes import aes_ecb_encrypt, pkcs7_pad
from mmusic.crypto.tea import SALT_LEN, ZERO_LEN, TeaCipher

PAYLOAD_MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + bytes(range(256)) * 8
PAYLOAD_FLAC = b"fLaC" + b"\x00" * 42 + bytes((i * 7) & 0xFF for i in range(512))
PAYLOAD_OGG = b"OggS\x00\x02" + bytes((i * 5) & 0xFF for i in range(512))


def make_flac(duration: float, *, sample_rate: int = 44100, channels: int = 2,
              bits: int = 16, pad: int = 40000) -> bytes:
    """A FLAC whose STREAMINFO declares `duration` seconds (exact, size-independent)."""
    total_samples = int(duration * sample_rate)
    packed = ((sample_rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36)
              | total_samples)
    # STREAMINFO = min/max block (2+2) + min/max frame (3+3) + packed (8) + MD5 (16)
    streaminfo = (4096).to_bytes(2, "big") * 2 + b"\x00" * 6 + packed.to_bytes(8, "big") \
                 + b"\x00" * 16
    assert len(streaminfo) == 34
    return b"fLaC" + bytes([0x00]) + (34).to_bytes(3, "big") + streaminfo + b"\x00" * pad


def make_mp3_xing(duration: float, *, sample_rate: int = 44100, pad: int = 40000) -> bytes:
    """An MP3 with a Xing header declaring exactly `duration` seconds."""
    spf = 1152
    frames = max(1, int(duration * sample_rate / spf))
    head = bytearray(36)                 # 4-byte frame header + 32-byte stereo side info
    head[0], head[1] = 0xFF, 0xFB        # MPEG1 / Layer III / no CRC
    head[2] = (9 << 4) | (0 << 2)        # 128 kbps @ 44100 Hz
    head[3] = 0x00                       # stereo
    xing = b"Xing" + (0x00000001).to_bytes(4, "big") + frames.to_bytes(4, "big")
    return bytes(head) + xing + b"\x00" * pad


# --------------------------------------------------------------------- #
# NCM
# --------------------------------------------------------------------- #
def _ncm_keybox(key: bytes) -> bytes:
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


def build_ncm(payload: bytes, *, key: bytes = b"0123456789ABCDEF",
              meta: dict | None = None, cover: bytes = b"") -> bytes:
    meta = meta or {"musicName": "测试歌曲", "artist": [["测试歌手"]], "album": "测试专辑",
                    "format": "mp3", "albumPic": "http://p1.music.126.net/xx.jpg"}

    key_plain = pkcs7_pad(b"neteasecloudmusic" + key)
    key_blob = bytes(b ^ NCM_KEY_XOR for b in aes_ecb_encrypt(NCM_CORE_KEY, key_plain))

    meta_plain = ("music:" + json.dumps(meta, ensure_ascii=False)).encode("utf-8")
    meta_body = base64.b64encode(aes_ecb_encrypt(NCM_META_KEY, pkcs7_pad(meta_plain)))
    meta_blob = bytes(b ^ NCM_META_XOR for b in (b"163 key(Don't modify):" + meta_body))

    box = _ncm_keybox(key)
    audio = bytes(payload[i] ^ box[i & 0xFF] for i in range(len(payload)))

    out = bytearray()
    out += NCM_MAGIC
    out += b"\x01\x70"
    out += struct.pack("<I", len(key_blob)) + key_blob
    out += struct.pack("<I", len(meta_blob)) + meta_blob
    out += struct.pack("<I", 0) + b"\x00" * 5          # crc32 + gap
    out += struct.pack("<I", len(cover)) + cover       # image size + image
    out += audio
    return bytes(out)


# --------------------------------------------------------------------- #
# QMC
# --------------------------------------------------------------------- #
def _qmc_static_mask(n: int) -> bytes:
    buf = bytearray(n)
    for i in range(n):
        k = i
        if k > 0x7FFF:
            k %= 0x7FFF
        buf[i] = QMC_STATIC_CIPHER_BOX[(k * k + 27) & 0xFF]
    return bytes(buf)


def build_qmc_v1(payload: bytes) -> bytes:
    """QMCv1 files carry no trailer - the whole file *is* the encrypted audio."""
    mask = _qmc_static_mask(len(payload))
    body = bytes(a ^ b for a, b in zip(payload, mask))
    # the legacy trailer check must see a key length >= 0x400 and pick the
    # static branch (true for any real payload; assert to keep the test honest)
    assert struct.unpack("<I", body[-4:])[0] >= 0x400
    return body


def encrypt_tencent_tea(plain: bytes, key: bytes, *, n_pad: int | None = None,
                        salt: bytes = b"\x11\x22") -> bytes:
    """Inverse of ``decrypt_tencent_tea`` (see tests for the derivation)."""
    blk = TeaCipher(key, 32)
    L = len(plain)
    if n_pad is None:
        n_pad = (-(1 + SALT_LEN + L + ZERO_LEN)) % 8
    inner = bytearray()
    inner += bytes([(n_pad & 0x7)])
    inner += b"\x00" * n_pad
    inner += salt
    inner += plain
    inner += b"\x00" * ZERO_LEN
    assert len(inner) % 8 == 0, "inner buffer must be block aligned"

    blocks = [bytes(inner[i:i + 8]) for i in range(0, len(inner), 8)]
    v = blocks                                  # desired value stream
    t_prev = v[0]
    c_prev = blk.encrypt_block(t_prev)
    out = [c_prev]
    for i in range(1, len(v)):
        t_i = bytes(a ^ b for a, b in zip(v[i], c_prev))
        c_i = bytes(a ^ b for a, b in zip(t_prev, blk.encrypt_block(t_i)))
        out.append(c_i)
        t_prev, c_prev = t_i, c_i
    return b"".join(out)


def make_qmc_v2_key(desired: bytes) -> bytes:
    """Produce the base64 key blob that ``qmc_derive_key`` turns back into
    ``desired``."""
    from mmusic.crypto.qmc import simple_make_key
    simple = simple_make_key(106, 8)
    tea_key = bytearray(16)
    for i in range(8):
        tea_key[i << 1] = simple[i]
        tea_key[(i << 1) + 1] = desired[i]
    wrapped = encrypt_tencent_tea(desired[8:], bytes(tea_key))
    return base64.b64encode(bytes(desired[:8]) + wrapped)


def build_qmc_v2(payload: bytes, key: bytes, *, song_id: int = 999) -> bytes:
    """QTag container: audio || key_blob || u32be(size) || "QTag"."""
    from mmusic.crypto.qmc import QmcMapCipher, QmcRC4Cipher
    buf = bytearray(payload)
    cipher = QmcRC4Cipher(key) if len(key) > 300 else QmcMapCipher(key)
    cipher.decrypt(buf, 0)
    audio = bytes(buf)
    blob = make_qmc_v2_key(key) + b"," + str(song_id).encode() + b","
    return audio + blob + struct.pack(">I", len(blob)) + b"QTag"


def build_qmc_stag(payload: bytes) -> bytes:
    mask = _qmc_static_mask(len(payload))
    body = bytes(a ^ b for a, b in zip(payload, mask))
    return body + b"STag"
"""TEA block cipher + Tencent's TEA-CBC key-wrapping variant.

Port of the public reference implementation (`unlock-music` tea.ts,
itself a port of golang.org/x/crypto/tea).  Used only to unwrap the
QQ-Music `EncV2` key blobs.
"""
from __future__ import annotations

import struct

__all__ = ["TeaCipher", "decrypt_tencent_tea"]

DELTA = 0x9E3779B9
_MASK32 = 0xFFFFFFFF
SALT_LEN = 2
ZERO_LEN = 7


class TeaCipher:
    """TEA with the standard 64-round schedule."""

    BLOCK_SIZE = 8
    KEY_SIZE = 16

    def __init__(self, key: bytes, rounds: int = 64) -> None:
        if len(key) != 16:
            raise ValueError("TEA key must be 16 bytes")
        if rounds % 2 != 0:
            raise ValueError("TEA rounds must be even")
        self.k0, self.k1, self.k2, self.k3 = struct.unpack(">4I", key)
        self.rounds = rounds

    def encrypt_block(self, block: bytes) -> bytes:
        v0, v1 = struct.unpack(">2I", block)
        k0, k1, k2, k3 = self.k0, self.k1, self.k2, self.k3
        s = 0
        for _ in range(self.rounds // 2):
            s = (s + DELTA) & _MASK32
            v0 = (v0 + ((((v1 << 4) & _MASK32) + k0) ^ ((v1 + s) & _MASK32) ^ ((v1 >> 5) + k1))) & _MASK32
            v1 = (v1 + ((((v0 << 4) & _MASK32) + k2) ^ ((v0 + s) & _MASK32) ^ ((v0 >> 5) + k3))) & _MASK32
        return struct.pack(">2I", v0, v1)

    def decrypt_block(self, block: bytes) -> bytes:
        v0, v1 = struct.unpack(">2I", block)
        k0, k1, k2, k3 = self.k0, self.k1, self.k2, self.k3
        s = (DELTA * (self.rounds // 2)) & _MASK32
        for _ in range(self.rounds // 2):
            v1 = (v1 - ((((v0 << 4) & _MASK32) + k2) ^ ((v0 + s) & _MASK32) ^ ((v0 >> 5) + k3))) & _MASK32
            v0 = (v0 - ((((v1 << 4) & _MASK32) + k0) ^ ((v1 + s) & _MASK32) ^ ((v1 >> 5) + k1))) & _MASK32
            s = (s - DELTA) & _MASK32
        return struct.pack(">2I", v0, v1)


def decrypt_tencent_tea(in_buf: bytes, key: bytes) -> bytes:
    """Tencent's TEA-CBC wrapping: PadLen(1) + Padding(0-7) + Salt(2) +
    Body + Zero(7).  Faithful port of the reference JS decoder."""
    if len(in_buf) % 8 != 0:
        raise ValueError("TencentTEA: input not a multiple of 8")
    if len(in_buf) < 16:
        raise ValueError("TencentTEA: input too small")

    blk = TeaCipher(key, 32)
    tmp = bytearray(blk.decrypt_block(in_buf[:8]))
    n_pad = tmp[0] & 0x7
    out_len = len(in_buf) - 1 - n_pad - SALT_LEN - ZERO_LEN
    if out_len < 0:
        raise ValueError("TencentTEA: negative output length")

    out = bytearray(out_len)
    iv_prev = bytes(8)
    iv_cur = in_buf[:8]
    in_pos = 8
    tmp_idx = 1 + n_pad

    def crypt_block() -> None:
        nonlocal iv_prev, iv_cur, in_pos, tmp_idx, tmp
        iv_prev = iv_cur
        iv_cur = in_buf[in_pos:in_pos + 8]
        for j in range(8):
            tmp[j] ^= iv_cur[j]
        tmp = bytearray(blk.decrypt_block(bytes(tmp)))
        in_pos += 8
        tmp_idx = 0

    i = 1
    while i <= SALT_LEN:            # skip salt
        if tmp_idx < 8:
            tmp_idx += 1
            i += 1
        else:
            crypt_block()

    out_pos = 0
    while out_pos < out_len:
        if tmp_idx < 8:
            out[out_pos] = tmp[tmp_idx] ^ iv_prev[tmp_idx]
            out_pos += 1
            tmp_idx += 1
        else:
            crypt_block()

    if tmp_idx < 8:                 # zero-check (mirrors reference exactly)
        for _ in range(ZERO_LEN):
            if tmp[tmp_idx] != iv_prev[tmp_idx]:
                raise ValueError("TencentTEA: zero check failed")
    return bytes(out)
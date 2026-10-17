"""Minimal, dependency-free AES-ECB decryption (128/192/256).

Only ECB *decryption* is implemented because that is all the NetEase
`.ncm` container needs.  The S-box is generated algorithmically instead
of being transcribed, which removes a whole class of copy errors; the
implementation is checked against the FIPS-197 test vector in
`tests/test_crypto.py`.
"""
from __future__ import annotations

__all__ = ["aes_ecb_decrypt", "aes_ecb_encrypt", "pkcs7_pad", "pkcs7_unpad"]


def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _gmul(a: int, b: int) -> int:
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r & 0xFF


def _rotl8(x: int, shift: int) -> int:
    return ((x << shift) | (x >> (8 - shift))) & 0xFF


def _build_sbox() -> tuple[list[int], list[int]]:
    """Generate the AES S-box and its inverse (FIPS-197 affine transform)."""
    sbox = [0] * 256
    p, q = 1, 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09
        xformed = q ^ _rotl8(q, 1) ^ _rotl8(q, 2) ^ _rotl8(q, 3) ^ _rotl8(q, 4)
        sbox[p] = (xformed ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    return sbox, inv


SBOX, INV_SBOX = _build_sbox()


def _expand_key(key: bytes):
    nk = len(key) // 4
    nr = nk + 6
    w = [bytearray(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        temp = bytearray(w[i - 1])
        if i % nk == 0:
            temp = bytearray((SBOX[temp[1]] ^ rcon, SBOX[temp[2]], SBOX[temp[3]], SBOX[temp[0]]))
            rcon = _xtime(rcon)
        elif nk > 6 and i % nk == 4:
            temp = bytearray(SBOX[b] for b in temp)
        w.append(bytearray(a ^ b for a, b in zip(w[i - nk], temp)))
    round_keys = [bytes(b for word in w[4 * r:4 * r + 4] for b in word) for r in range(nr + 1)]
    return round_keys, nr


def _decrypt_block(block: bytes, rk: list[bytes], nr: int) -> bytes:
    # state[r][c] = block[r + 4*c]  (AES column-major mapping)
    state = [[block[r + 4 * c] for c in range(4)] for r in range(4)]

    def add_round_key(rkey: bytes) -> None:
        for r in range(4):
            for c in range(4):
                state[r][c] ^= rkey[r + 4 * c]

    def inv_shift_rows() -> None:
        for r in range(1, 4):
            state[r] = state[r][-r:] + state[r][:-r]

    add_round_key(rk[nr])
    for rnd in range(nr - 1, 0, -1):
        inv_shift_rows()
        for r in range(4):
            for c in range(4):
                state[r][c] = INV_SBOX[state[r][c]]
        add_round_key(rk[rnd])
        for c in range(4):
            a0, a1, a2, a3 = state[0][c], state[1][c], state[2][c], state[3][c]
            state[0][c] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
            state[1][c] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
            state[2][c] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
            state[3][c] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)

    inv_shift_rows()
    for r in range(4):
        for c in range(4):
            state[r][c] = INV_SBOX[state[r][c]]
    add_round_key(rk[0])

    out = bytearray(16)
    for r in range(4):
        for c in range(4):
            out[r + 4 * c] = state[r][c]
    return bytes(out)


def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    """AES-ECB decrypt `data` (length must be a multiple of 16)."""
    if len(key) not in (16, 24, 32):
        raise ValueError("AES key must be 16/24/32 bytes")
    if len(data) % 16 != 0:
        raise ValueError("AES-ECB input must be a multiple of 16 bytes")
    rk, nr = _expand_key(key)
    return b"".join(_decrypt_block(data[i:i + 16], rk, nr) for i in range(0, len(data), 16))


def pkcs7_unpad(data: bytes) -> bytes:
    """Strip PKCS#7 padding; return `data` unchanged if it is not padded."""
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and data[-n:] == bytes([n]) * n:
        return data[:-n]
    return data

def _encrypt_block(block: bytes, rk: list[bytes], nr: int) -> bytes:
    state = [[block[r + 4 * c] for c in range(4)] for r in range(4)]

    def add_round_key(rkey: bytes) -> None:
        for r in range(4):
            for c in range(4):
                state[r][c] ^= rkey[r + 4 * c]

    def sub_bytes() -> None:
        for r in range(4):
            for c in range(4):
                state[r][c] = SBOX[state[r][c]]

    def shift_rows() -> None:
        for r in range(1, 4):
            state[r] = state[r][r:] + state[r][:r]

    def mix_columns() -> None:
        for c in range(4):
            a0, a1, a2, a3 = state[0][c], state[1][c], state[2][c], state[3][c]
            state[0][c] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
            state[1][c] = a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3
            state[2][c] = a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3)
            state[3][c] = _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)

    add_round_key(rk[0])
    for rnd in range(1, nr):
        sub_bytes()
        shift_rows()
        mix_columns()
        add_round_key(rk[rnd])
    sub_bytes()
    shift_rows()
    add_round_key(rk[nr])

    out = bytearray(16)
    for r in range(4):
        for c in range(4):
            out[r + 4 * c] = state[r][c]
    return bytes(out)


def aes_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    """AES-ECB encrypt `data` (length must be a multiple of 16).

    Only needed for tests / fixture generation, but kept here so encryption
    and decryption are verified as true inverses of one another."""
    if len(key) not in (16, 24, 32):
        raise ValueError("AES key must be 16/24/32 bytes")
    if len(data) % 16 != 0:
        raise ValueError("AES-ECB input must be a multiple of 16 bytes")
    rk, nr = _expand_key(key)
    return b"".join(_encrypt_block(data[i:i + 16], rk, nr) for i in range(0, len(data), 16))


def pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    pad = block - (len(data) % block)
    return data + bytes([pad]) * pad
"""Small helpers shared across the package."""
from __future__ import annotations

import hashlib
from pathlib import Path

__all__ = ["xor_repeating", "sha256_file", "sha256_bytes", "human_size"]


def xor_repeating(data: bytes, pattern: bytes) -> bytes:
    """XOR `data` with a repeating `pattern`, using C-speed big-int math.

    The stream ciphers here (NCM key-box, QMC static/map) all reduce to a
    byte-wise XOR against a short repeating key, so doing it as one big
    integer XOR turns an O(n) Python loop into a couple of C calls.
    """
    if not pattern:
        raise ValueError("xor pattern must not be empty")
    n = len(data)
    if n == 0:
        return b""
    rep = (pattern * (n // len(pattern) + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(rep, "big")).to_bytes(n, "big")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: "str | Path", *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def human_size(n: int) -> str:
    step = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if step < 1024 or unit == "GB":
            return f"{step:.1f} {unit}" if unit != "B" else f"{int(step)} B"
        step /= 1024
    return f"{step:.1f} GB"
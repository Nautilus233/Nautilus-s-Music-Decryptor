"""Audio duration parsing + candidate cross-verification.

Why this exists: on some platforms a result can carry the *correct* title and
artist while the actual audio is a completely different (badly made) remix.
Title-based filtering cannot see that - but duration can: a mislabelled cut
almost always differs noticeably in length from the real track.

Everything here is pure standard library: we parse the container header that
was fetched with a cheap ranged probe, so no extra dependency and no full
download is needed.
"""
from __future__ import annotations

import statistics

__all__ = ["TOL_ABS", "TOL_REL", "audio_duration", "consensus", "mp3_scan_estimate",
           "format_seconds", "flac_streaminfo", "mp3_frame_info"]

# 容差必须紧：电台版/剪辑版通常只短 10~15%，容差放到 10% 就等于放它们过关。
# 实测《夜曲》3:48(228s) 的电台版约 3:25(205s)，差 23s —— 旧值 15.0/0.10 正好放过它。
TOL_ABS = 10.0     # 时长比对：绝对容差（秒）
TOL_REL = 0.045    # 时长比对：相对容差（4.5%）

# MPEG Layer III bitrate / sample-rate tables (index 1..14)
_MP3_BR_V1_L3 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_MP3_BR_V2_L3 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
_MP3_SR = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def format_seconds(seconds: "float | None") -> str:
    if not seconds:
        return "?"
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _skip_id3(data: bytes) -> int:
    if data[:3] == b"ID3" and len(data) >= 10:
        size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) \
               | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        return 10 + size
    return 0


def _frame_length(ver: int, bitrate_kbps: int, sample_rate: int, padding: int) -> int:
    """Bytes in one MPEG Layer-III frame (1152 samples for MPEG1, 576 for MPEG2/2.5)."""
    if not bitrate_kbps or not sample_rate:
        return 0
    coeff = 144 if ver == 3 else 72
    return int(coeff * bitrate_kbps * 1000 / sample_rate) + padding


def _looks_like_frame(data: bytes, pos: int) -> "tuple | None":
    """Decode a plausible MPEG Layer-III frame header at `pos`."""
    if pos + 4 > len(data) or data[pos] != 0xFF or (data[pos + 1] & 0xE0) != 0xE0:
        return None
    b1, b2, b3 = data[pos + 1], data[pos + 2], data[pos + 3]
    ver = (b1 >> 3) & 0x03
    layer = (b1 >> 1) & 0x03
    brx = (b2 >> 4) & 0x0F
    srx = (b2 >> 2) & 0x03
    if ver == 1 or layer != 1 or brx in (0, 15) or srx == 3:
        return None
    channel_mode = (b3 >> 6) & 0x03
    side = (17 if channel_mode == 3 else 32) if ver == 3 else \
           (9 if channel_mode == 3 else 17)
    table = _MP3_BR_V1_L3 if ver == 3 else _MP3_BR_V2_L3
    return {
        "bitrate": table[brx],
        "sample_rate": _MP3_SR[ver][srx],
        "mpeg": ver,
        "layer": 3,
        "samples_per_frame": 1152 if ver == 3 else 576,
        "padding": (b2 >> 1) & 0x01,
        "side_info": side,
        "frames": 0,
        "total_samples": 0,
    }


def mp3_frame_info(data: bytes) -> "dict | None":
    """First MPEG Layer-III frame header, plus Xing/Info/VBRI frame count.

    A single sync word is *not* enough: random bytes hit 0xFF 0xEx about 86% of
    the time in a 4 KB buffer, which used to yield a nonsense duration.  So a
    frame is only accepted when it carries an explicit Xing/Info/VBRI tag, or
    when a *second* consecutive frame of the same version/layer/sample-rate
    follows it at the computed offset.
    """
    off = _skip_id3(data)
    limit = min(len(data) - 4, off + 200000)
    i = off
    while i < limit:
        info = _looks_like_frame(data, i)
        if info is None:
            i += 1
            continue

        tag = i + 4 + info["side_info"]
        has_xing = tag + 12 <= len(data) and data[tag:tag + 4] in (b"Xing", b"Info")
        has_vbri = i + 22 <= len(data) and data[i + 4:i + 8] == b"VBRI"

        if has_xing:
            flags = int.from_bytes(data[tag + 4:tag + 8], "big")
            if flags & 0x01:
                frames = int.from_bytes(data[tag + 8:tag + 12], "big")
                if frames > 0:
                    info["frames"] = frames
                    info["total_samples"] = frames * info["samples_per_frame"]
            return info
        if has_vbri:
            frames = int.from_bytes(data[i + 18:i + 22], "big")
            if frames > 0:
                info["frames"] = frames
                info["total_samples"] = frames * info["samples_per_frame"]
            return info

        # no explicit tag -> require two consecutive valid frames
        step = _frame_length(info["mpeg"], info["bitrate"], info["sample_rate"], info["padding"])
        nxt = i + step
        following = _looks_like_frame(data, nxt) if step > 0 else None
        if following and following["mpeg"] == info["mpeg"] \
                and following["layer"] == info["layer"] \
                and following["sample_rate"] == info["sample_rate"]:
            return info
        i += 1
    return None


def flac_streaminfo(data: bytes) -> "dict | None":
    """STREAMINFO from the first metadata block (exact duration, size-independent)."""
    if len(data) < 42 or data[:4] != b"fLaC" or (data[4] & 0x7F) != 0:
        return None
    packed = int.from_bytes(data[18:26], "big")
    sample_rate = (packed >> 44) & 0xFFFFF
    channels = ((packed >> 41) & 0x7) + 1
    bits = ((packed >> 36) & 0x1F) + 1
    total_samples = packed & 0xFFFFFFFFF
    if not sample_rate:
        return None
    return {"sample_rate": sample_rate, "channels": channels, "bits": bits,
            "total_samples": total_samples,
            "duration": total_samples / sample_rate if total_samples else 0.0}


def _wav_duration(data: bytes) -> "float | None":
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos, byte_rate, data_size = 12, 0, 0
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        body = pos + 8
        if cid == b"fmt " and size >= 16 and body + 16 <= len(data):
            byte_rate = int.from_bytes(data[body + 8:body + 12], "little")
        elif cid == b"data":
            data_size = size
            break
        pos = body + size + (size & 1)
    if byte_rate and data_size:
        return data_size / byte_rate
    return None


def _find_atom(buf: bytes, tag: bytes, start: int = 0, end: "int | None" = None):
    pos, end = start, (len(buf) if end is None else min(end, len(buf)))
    while pos + 8 <= end:
        size = int.from_bytes(buf[pos:pos + 4], "big")
        kind = buf[pos + 4:pos + 8]
        header = 8
        if size == 1 and pos + 16 <= end:
            size = int.from_bytes(buf[pos + 8:pos + 16], "big")
            header = 16
        if size < header:
            return None
        if kind == tag:
            return buf[pos + header:pos + size]
        pos += size
    return None


def _m4a_duration(data: bytes) -> "float | None":
    if len(data) < 16 or data[4:8] != b"ftyp":
        return None
    moov = _find_atom(data, b"moov")
    if not moov:
        return None
    mvhd = _find_atom(moov, b"mvhd")
    if not mvhd or len(mvhd) < 20:
        return None
    if mvhd[0] == 1 and len(mvhd) >= 36:
        timescale = int.from_bytes(mvhd[20:28], "big")
        duration = int.from_bytes(mvhd[28:36], "big")
    else:
        timescale = int.from_bytes(mvhd[12:16], "big")
        duration = int.from_bytes(mvhd[16:20], "big")
    return duration / timescale if timescale else None


def mp3_scan_estimate(data: bytes, total_size: int) -> "float | None":
    """Extrapolate the whole length from the frames actually present in `data`.

    Unlike the first-frame bitrate (which is wrong for VBR - and most CDN
    files are VBR), this measures the *observed* bytes-per-second by walking
    the real frames, then scales to the advertised file size.
    """
    if not total_size:
        return None
    off = _skip_id3(data)
    limit = len(data) - 4
    i = off
    frames = 0
    scanned = 0
    seconds = 0.0
    while i < limit and frames < 4000:
        info = _looks_like_frame(data, i)
        if info is None:
            if frames == 0:
                i += 1
                continue
            break
        step = _frame_length(info["mpeg"], info["bitrate"], info["sample_rate"], info["padding"])
        if step <= 0:
            break
        if frames == 0:
            scanned = 0
        frames += 1
        scanned += step
        seconds += info["samples_per_frame"] / info["sample_rate"]
        i += step
    if frames < 4 or seconds <= 0 or scanned <= 0:
        return None
    return total_size / (scanned / seconds)


def _ogg_rate(head: bytes) -> int:
    """Sample rate from the first Ogg page (Vorbis/Opus ident packet)."""
    try:
        seg = head.index(b"\x01vorbis")
        return int.from_bytes(head[seg + 12:seg + 16], "little") or 44100
    except ValueError:
        pass
    if b"OpusHead" in head[:4096]:
        return 48000                      # Opus granules are always 48 kHz
    return 44100


def _ogg_last_granule(tail: bytes) -> "int | None":
    """Granule position of the last *complete* Ogg page present in `tail`."""
    best = None
    pos = tail.find(b"OggS")
    while pos >= 0:
        if pos + 27 <= len(tail):
            granule = int.from_bytes(tail[pos + 6:pos + 14], "little")
            if granule not in (0, 0xFFFFFFFFFFFFFFFF):
                best = granule
        pos = tail.find(b"OggS", pos + 1)
    return best


def audio_duration(data: bytes, total_size: int = 0, tail: bytes = b"") -> "float | None":
    """Actual audio length in seconds, or None when it cannot be determined.

    `data` may be only the head of the file (a ranged probe); `tail` may carry
    the last bytes, for containers that store their length at the end (Ogg's
    final page granule, an m4a whose `moov` is not faststart).
    """
    if not data:
        return None
    try:
        if data[:4] == b"fLaC":
            info = flac_streaminfo(data)
            return info["duration"] if info and info["duration"] else None
        if data[:4] == b"OggS":
            granule = _ogg_last_granule(tail) if tail else None
            if granule is None:
                return None               # duration lives in the last page
            return granule / max(1, _ogg_rate(data))
        if data[:4] == b"RIFF":
            return _wav_duration(data)
        if data[4:8] == b"ftyp":
            got = _m4a_duration(data)
            if got is None and tail:
                got = _m4a_duration(b"\x00\x00\x00\x08ftyp" + tail)
            return got
        info = mp3_frame_info(data)
        if info:
            if info["total_samples"]:
                return info["total_samples"] / info["sample_rate"]
            est = mp3_scan_estimate(data, total_size)
            if est:
                return est
            if total_size and info["bitrate"]:
                return total_size * 8 / (info["bitrate"] * 1000)
        return None
    except (IndexError, ValueError, ZeroDivisionError):
        return None


def consensus(durations: "list[float | None]", *, tol_abs: float = TOL_ABS,
              tol_rel: float = TOL_REL) -> "tuple[float | None, list[bool], str]":
    """Cross-check candidates by length.

    Returns ``(reference_seconds, per_candidate_pass_flags, reason)``.

    The reference is the largest *mutually consistent* cluster of measured
    durations (a duration is in the cluster if it is within
    ``max(tol_abs, tol_rel * itself)`` of the cluster centre).  Anything
    outside that cluster is a suspect - a mislabelled cut/remix - and anything
    whose length could not be measured is **never** marked trusted.

    Clustering rather than a plain median matters for the 2-candidate case:
    with ``[269, 120]`` a median would be 194.5 and would reject *both*; with
    clustering the list order (highest score first) decides which one is the
    reference, so the better-ranked candidate wins and the other is rejected.
    """
    total = len(durations)
    known = [i for i, d in enumerate(durations) if d]
    if not known:
        return None, [False] * total, "未能获取任何候选的音频时长，无法交叉验证"

    if len(known) == 1:
        ref = durations[known[0]]
        tol = max(tol_abs, tol_rel * ref)
        flags = [bool(d) and abs(d - ref) <= tol for d in durations]
        return ref, flags, f"仅 1 个候选取到时长（{format_seconds(ref)}），无法交叉比对"

    best_cluster: "list[int]" = []
    for i in known:
        tol = max(tol_abs, tol_rel * durations[i])
        cluster = [j for j in known if abs(durations[j] - durations[i]) <= tol]
        if len(cluster) > len(best_cluster):
            best_cluster = cluster

    reference = statistics.median([durations[j] for j in best_cluster])
    tol = max(tol_abs, tol_rel * reference)
    flags = [bool(d) and abs(d - reference) <= tol for d in durations]

    if len(best_cluster) == 1:
        return reference, flags, (f"参考时长 {format_seconds(reference)}"
                                  f"（各候选时长互不印证，按排序最前者的取值，结论仅供参考）")
    return reference, flags, (f"参考时长 {format_seconds(reference)}"
                              f"（{len(best_cluster)} 个候选互相印证），容差 ±{int(tol)}s")

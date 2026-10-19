"""可选转码引擎：复用 PySide6 自带的 FFmpeg 库（LGPL，**零新增依赖**）。

配合「目标音质」把解密/下载得到的音频转成用户要的最终格式（用户 2026-10-18 确认）：

    * 目标 MP3 ：一律输出 MP3（无损源也转 —— 选择即承诺）；源已是 MP3 则零开销透传
    * 目标 FLAC：无损源（FLAC/WAV/APE/DFF）→ FLAC；有损源 → **降级 MP3**
    * 目标 WAV ：源 WAV 保持；无损源保持无损；有损源 → **降级 MP3**
    （有损源不做“无效升格”，日志明确说明）

引擎是**可选**的：库加载不到（被裁剪的 PySide6 构建 / 打包未附带 / 平台不符）时，
所有需要转码的场景**静默回退为保留原格式**并写一行日志；软件其余功能不受影响。

实现要点（为什么这么做）：

* Qt 6.8+ 的多媒体后端就是 FFmpeg，PySide6 因此自带 libav*（LGPL，与 Qt 同族）。
  这里按“窄路径”直接 ctypes 调 libav：解容器 + 解码 + swresample 换格式 + 编码器。
* 这份 FFmpeg 是新版（avcodec-63）：``AVCodecContext.sample_fmt`` 不再是 AVOption，
  ``AVFrame`` 连选项表都没有 —— 因此：
    - ctx 的字段偏移用 **AVOption 表枚举**得到（``ar``/``ch_layout``/``frame_size``，
      ``sample_fmt`` 位于 ``ar`` 与 ``frame_size`` 之间、默认值 -1，可靠定位）；
    - 编码帧的属性（pts/采样率/声道布局）用 **``av_frame_copy_props``** 从解码帧继承，
      布局非法（PCM 源常见 UNSPEC）时才用“与空白帧差分”临时定位并补齐。
    全部访问走 ``ReadProcessMemory`` 安全探测 / 单字段视图，跨 FFmpeg 版本不脆。
* 不重采样（保持源采样率与声道数）：MP3 编码器只接受 32/44.1/48kHz 与 1~2 声道，
  其它情况**如实放弃并保留原格式**（绝不硬转出可疑产物）。
* MP3 用 Windows 内核编码器 ``mp3_mf``（无需 libmp3lame，无 GPL/专利负担）；
  容器由我们自己写（MP3 = 裸帧流；FLAC = ``fLaC`` + STREAMINFO + 帧），不需要 libavformat 输出侧。
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional

__all__ = [
    "EngineUnavailable", "TranscodeFailed",
    "available", "unavailable_reason", "engine_info",
    "plan", "plan_effective", "convert_bytes",
]

# --------------------------------------------------------------------------- #
# 1) 纯逻辑：目标音质矩阵（不碰引擎，可离线测试）
# --------------------------------------------------------------------------- #
_LOSSLESS = frozenset({"flac", "wav", "ape", "dff"})

_MP3_BITRATE = 320_000
_MP3_FRAME = 1152
_FLAC_FRAME = 4096
_MP3_RATES = (32000, 44100, 48000)


class EngineUnavailable(RuntimeError):
    """PySide6 自带的 FFmpeg 库不可用。"""


class TranscodeFailed(RuntimeError):
    """引擎可用但本次转码失败（坏文件 / 解码器缺失 / 编码器拒绝）。"""


def plan(src_ext: str, target: str) -> "tuple[str, str]":
    """目标音质矩阵 → ``(action, note)``；action ∈ {"keep","mp3","flac"}。"""
    src = (src_ext or "").lower().lstrip(".")
    tgt = (target or "mp3").lower()
    if src == tgt:
        return "keep", ""
    if tgt == "mp3":
        return "mp3", ""
    if src in _LOSSLESS:
        return ("flac", "") if tgt == "flac" else ("keep", "")
    if src == "mp3":
        # 源已经是 MP3、用户要无损：**保持原样**。此前这里会走下面的 "mp3" 分支，
        # 于是 128k mp3 被重新编码成 320k mp3（实测 3.63MB → 9.07MB）—— 体积翻倍、
        # 音质没有任何提升，还凭空多一代编码损失。这正是用户明确要求过的
        # "本身是有损源，不进行无效转换"。
        return "keep", "源本身是有损 MP3，无法产出真无损：已保持原样，不做无效转换"
    return "mp3", "源本身是有损，无法产出真无损：已按目标降级为 MP3（未做无效升格）"


def plan_effective(src_ext: str, target: str,
                   engine_available: "Optional[bool]" = None) -> "tuple[str, str]":
    """在矩阵之上叠加“引擎可用性”：引擎缺失时静默回退为保留原格式。"""
    if engine_available is None:
        engine_available = available()
    action, note = plan(src_ext, target)
    if action != "keep" and not engine_available:
        why = "转码引擎不可用，已保留原格式"
        if unavailable_reason():
            why += f"（{unavailable_reason()}）"
        return "keep", (f"{note}；{why}" if note else why)
    return action, note


# --------------------------------------------------------------------------- #
# 2) 引擎：ctypes 直调 PySide6 自带 libav
# --------------------------------------------------------------------------- #
_LIB_NAMES = ("avutil", "avcodec", "avformat", "swresample")
_AUDIO = 1
_FRAME_NBSAMPLES, _FRAME_FORMAT = 112, 116          # AVFrame 前段（跨版本稳定）
_PKT_DATA, _PKT_SIZE = 24, 32                       # AVPacket 前段（跨版本稳定）
_LINEAR = ctypes.c_void_p


class AVOption(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char_p), ("help", ctypes.c_char_p),
                ("offset", ctypes.c_int), ("type", ctypes.c_int),
                ("default_val", ctypes.c_void_p), ("min", ctypes.c_double),
                ("max", ctypes.c_double), ("flags", ctypes.c_int), ("unit", ctypes.c_char_p)]


class ChLayout(ctypes.Structure):
    _fields_ = [("order", ctypes.c_int), ("nb_channels", ctypes.c_int), ("u", ctypes.c_uint64)]


def _lib_dir() -> "Optional[Path]":
    cands: "list[Path]" = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cands.append(Path(base))
    try:
        import PySide6  # noqa: PLC0415 - 延迟导入，保持“零依赖”语义
        cands.append(Path(PySide6.__file__).parent)
    except Exception:  # noqa: BLE001
        pass
    for d in cands:
        try:
            if all(any(d.glob(f"{n}-*.dll")) for n in _LIB_NAMES):
                return d
        except OSError:
            continue
    return None


class _Engine:
    def __init__(self, libs: "dict[str, ctypes.CDLL]") -> None:
        self.avutil, self.avcodec = libs["avutil"], libs["avcodec"]
        self.avformat, self.swr_lib = libs["avformat"], libs["swresample"]
        self._proto()
        self._ctx_offsets: "dict[str, int] | None" = None
        self._frame_offsets: "tuple[int, int] | None" = None
        k32 = ctypes.CDLL("kernel32")
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        self._proc = k32.GetCurrentProcess()
        k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        k32.ReadProcessMemory.restype = ctypes.c_int
        self._k32 = k32

    # ---------------- ctypes 基础设施 ---------------- #
    def _proto(self) -> None:
        cv, ci, cch = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
        P = ctypes.POINTER

        def pr(lib, name, res, args):
            f = getattr(lib, name)
            f.restype = res
            f.argtypes = args
            return f

        self.p_open_input = pr(self.avformat, "avformat_open_input", ci, [P(cv), cch, cv, cv])
        self.p_find_stream_info = pr(self.avformat, "avformat_find_stream_info", ci, [cv, cv])
        self.p_read_frame = pr(self.avformat, "av_read_frame", ci, [cv, cv])
        self.p_close_input = pr(self.avformat, "avformat_close_input", None, [P(cv)])
        self.p_alloc_ctx = pr(self.avcodec, "avcodec_alloc_context3", cv, [cv])
        self.p_params_to_ctx = pr(self.avcodec, "avcodec_parameters_to_context", ci, [cv, cv])
        self.p_open2 = pr(self.avcodec, "avcodec_open2", ci, [cv, cv, cv])
        self.p_find_decoder = pr(self.avcodec, "avcodec_find_decoder", cv, [ci])
        self.p_find_encoder = pr(self.avcodec, "avcodec_find_encoder_by_name", cv, [cch])
        self.p_send_packet = pr(self.avcodec, "avcodec_send_packet", ci, [cv, cv])
        self.p_receive_frame = pr(self.avcodec, "avcodec_receive_frame", ci, [cv, cv])
        self.p_send_frame = pr(self.avcodec, "avcodec_send_frame", ci, [cv, cv])
        self.p_receive_packet = pr(self.avcodec, "avcodec_receive_packet", ci, [cv, cv])
        self.p_pkt_alloc = pr(self.avcodec, "av_packet_alloc", cv, [])
        self.p_pkt_unref = pr(self.avcodec, "av_packet_unref", None, [cv])
        self.p_frame_alloc = pr(self.avutil, "av_frame_alloc", cv, [])
        self.p_frame_copy_props = pr(self.avutil, "av_frame_copy_props", ci, [cv, cv])
        self.p_frame_get_buffer = pr(self.avutil, "av_frame_get_buffer", ci, [cv, ci])
        self.p_layout_default = pr(self.avutil, "av_channel_layout_default", None, [P(ChLayout), ci])
        self.p_opt_set = pr(self.avutil, "av_opt_set", ci, [cv, cch, cch, ci])
        self.p_opt_set_int = pr(self.avutil, "av_opt_set_int", ci, [cv, cch, ctypes.c_int64, ci])
        self.p_opt_next = pr(self.avutil, "av_opt_next", cv, [cv, cv])
        self.p_swr_opts2 = pr(self.swr_lib, "swr_alloc_set_opts2", ci,
                              [P(cv), P(ChLayout), ci, ci, P(ChLayout), ci, ci, ci, cv])
        self.p_swr_init = pr(self.swr_lib, "swr_init", ci, [cv])
        self.p_swr_convert = pr(self.swr_lib, "swr_convert", ci, [cv, P(cv), ci, P(cv), ci])

    # ---------------- 安全内存访问 ---------------- #
    def _read(self, addr: int, n: int) -> "bytes | None":
        buf = (ctypes.c_char * n)()
        got = ctypes.c_size_t(0)
        ok = self._k32.ReadProcessMemory(self._proc, ctypes.c_void_p(addr),
                                         ctypes.cast(buf, ctypes.c_void_p), n, ctypes.byref(got))
        return bytes(buf) if (ok and got.value == n) else None

    def _i32(self, addr: int, off: int) -> "int | None":
        b = self._read(addr + off, 4)
        return ctypes.c_int32.from_buffer_copy(b).value if b else None

    def _u64(self, addr: int, off: int) -> "int | None":
        b = self._read(addr + off, 8)
        return ctypes.c_uint64.from_buffer_copy(b).value if b else None

    def _ptr(self, addr: int, off: int) -> "int | None":
        b = self._read(addr + off, 8)
        return ctypes.c_void_p.from_buffer_copy(b).value if b else None

    @staticmethod
    def _w32(addr: int, off: int, val: int) -> None:
        ctypes.c_int32.from_address(addr + off).value = val

    # ---------------- 偏移自发现 ---------------- #
    def _offsets(self) -> "dict[str, int]":
        """AVCodecContext 字段偏移：在全新 ctx 上枚举 AVOption 表（一次即得）。"""
        if self._ctx_offsets is not None:
            return self._ctx_offsets
        ctx = self.p_alloc_ctx(None)
        offs: "dict[str, int]" = {}
        opt = self.p_opt_next(ctx, None)
        guard = 0
        while opt and guard < 800:
            op = AVOption.from_address(opt)
            if op.name:
                offs[op.name.decode("utf-8", "ignore")] = op.offset
            opt = self.p_opt_next(ctx, ctypes.c_void_p(opt))
            guard += 1
        for need in ("ar", "frame_size", "ch_layout"):
            if need not in offs:
                raise EngineUnavailable(f"AVCodecContext 缺少选项 {need!r}（FFmpeg 版本不兼容）")
        sf = next((o for o in range(offs["ar"] + 4, offs["frame_size"], 4)
                   if self._i32(ctx, o) == -1), None)
        if sf is None:
            raise EngineUnavailable("无法定位 AVCodecContext.sample_fmt 偏移")
        offs["_sample_fmt"] = sf
        self._ctx_offsets = offs
        return offs

    def _frame_field_offsets(self, ref_frame: int, rate: int, nb: int) -> "tuple[int, int]":
        """定位 AVFrame 的 sample_rate / ch_layout 偏移（一次即得，进程内缓存）。

        ``sample_rate``：与空白帧差分 + 值必须等于已知采样率。
        ``ch_layout``：**写入探测** —— 在“零/小值槽位”逐个写入合法布局，用
        ``av_frame_get_buffer`` 是否成功来证明偏移正确（get_buffer 恰好校验该字段）。
        只碰零值槽位、每次用全新帧、有界搜索，因此安全；探测不成功则如实放弃。
        """
        if self._frame_offsets is not None:
            return self._frame_offsets
        sr = None
        pristine, probe = self.p_frame_alloc(), self.p_frame_alloc()
        if self.p_frame_copy_props(probe, ref_frame) >= 0:
            a, b = self._read(pristine, 1024), self._read(probe, 1024)
            if a and b:
                sr = next((o for o in range(0, 1020, 4)
                           if a[o:o + 4] != b[o:o + 4]
                           and ctypes.c_int32.from_buffer_copy(b, o).value == rate), None)
        lay = ChLayout()
        self.p_layout_default(ctypes.byref(lay), nb)
        ref = self._read(ref_frame, 1024) or b""
        cl = None
        for o in range(96, 900, 4):
            if o + 16 > len(ref):
                break
            if abs(ctypes.c_int32.from_buffer_copy(ref, o).value) > 8:
                continue
            if ctypes.c_uint64.from_buffer_copy(ref, o + 8).value > 8:
                continue
            ef = self.p_frame_alloc()
            self._w32(ef, _FRAME_NBSAMPLES, 1024)
            self._w32(ef, _FRAME_FORMAT, 1)
            self._w32(ef, o, lay.order)
            self._w32(ef, o + 4, lay.nb_channels)
            ctypes.c_uint64.from_address(ef + o + 8).value = lay.u
            if self.p_frame_get_buffer(ef, 0) == 0:
                cl = o
                break
        if cl is None:
            raise EngineUnavailable("无法定位 AVFrame 的 ch_layout 偏移")
        self._frame_offsets = (sr if sr is not None else -1, cl)
        return self._frame_offsets

    def _find_audio_stream(self, fmt: int) -> "int":
        """Locate the audio stream's ``codecpar``.

        这是"版本无关"的堆内存探测，**不能假设第一个命中就是真的音频流**：实测
        （2026-10-19）同一进程里，一个只有单条流的裸 FLAC 曾命中一段
        ``codec_type == 1`` 的无关内存，于是 ``codec_id = 0`` →
        误报"没有可用的解码器"，转码被静默降级成"保留原格式"（用户只看到
        "转码失败"，且换个文件顺序就又好了 —— 典型的偶发）。所以每个候选都用
        ``avcodec_find_decoder`` 校验：取第一个**确实存在解码器**的候选；
        一个都没有时退回第一个"音频类型"的候选，让调用方给出可读错误。
        """
        first_audio = 0
        for off in range(0, 160, 8):
            arr = self._ptr(fmt, off)
            if not arr:
                continue
            for k in range(0, 32, 8):
                stream = self._ptr(arr, k)
                if not stream:
                    break
                for so in range(0, 200, 8):
                    cp = self._ptr(stream, so)
                    if not cp:
                        continue
                    head = self._read(cp, 8)
                    if not head or ctypes.c_int32.from_buffer_copy(head, 0).value != _AUDIO:
                        continue
                    codec_id = ctypes.c_int32.from_buffer_copy(head, 4).value
                    if codec_id and self.p_find_decoder(codec_id):
                        return cp
                    first_audio = first_audio or cp
        if first_audio:
            return first_audio
        raise TranscodeFailed("输入中没有找到音频流")

    # ---------------- 转码主流程 ---------------- #
    def transcode(self, src: Path, dst: Path, action: str) -> None:
        """``action`` = "mp3" | "flac"；失败抛 TranscodeFailed。"""
        fmt = ctypes.c_void_p()
        try:
            self._inner(fmt, src, dst, action)
        finally:
            if fmt:
                self.p_close_input(ctypes.byref(fmt))

    def _inner(self, fmt: ctypes.c_void_p, src: Path, dst: Path, action: str) -> None:
        offs = self._offsets()
        if self.p_open_input(ctypes.byref(fmt), str(src).encode("utf-8"), None, None) != 0:
            raise TranscodeFailed(f"无法打开输入：{src.name}")
        if self.p_find_stream_info(fmt, None) < 0:
            raise TranscodeFailed("输入流信息解析失败")
        codecpar = self._find_audio_stream(fmt.value)
        codec_id = self._i32(codecpar, 4)
        dec = self.p_find_decoder(codec_id)
        if not dec:
            raise TranscodeFailed(f"没有可用的解码器（codec_id={codec_id}）")
        dec_ctx = self.p_alloc_ctx(dec)
        if self.p_params_to_ctx(dec_ctx, ctypes.c_void_p(codecpar)) < 0 or \
                self.p_open2(dec_ctx, dec, None) != 0:
            raise TranscodeFailed("解码器初始化失败")

        rate = self._i32(dec_ctx, offs["ar"]) or 0
        nb = self._i32(dec_ctx, offs["ch_layout"] + 4) or 0
        if not rate:
            raise TranscodeFailed("无法读取输入采样率")
        if nb not in (1, 2):
            raise TranscodeFailed(f"声道数不支持转码（{nb} 声道），已保留原格式")
        if action == "mp3" and rate not in _MP3_RATES:
            raise TranscodeFailed(f"采样率 {rate}Hz 不支持 MP3 编码，已保留原格式")

        in_lay = ChLayout()
        self.p_layout_default(ctypes.byref(in_lay), nb)        # 归一化（PCM 源常见 UNSPEC）
        encoder_name = b"flac" if action == "flac" else b"mp3_mf"
        frame_size = _FLAC_FRAME if action == "flac" else _MP3_FRAME
        enc = self.p_find_encoder(encoder_name)
        if not enc:
            raise TranscodeFailed(f"编码器不可用：{encoder_name.decode()}")
        enc_ctx = self.p_alloc_ctx(enc)
        if action == "mp3":
            self.p_opt_set_int(enc_ctx, b"b", _MP3_BITRATE, 0)
        self.p_opt_set_int(enc_ctx, b"ar", rate, 0)
        self.p_opt_set(enc_ctx, b"ch_layout", b"mono" if nb == 1 else b"stereo", 0)
        self.p_opt_set_int(enc_ctx, b"frame_size", frame_size, 0)
        self._w32(enc_ctx, offs["_sample_fmt"], 1)             # AV_SAMPLE_FMT_S16
        if self.p_open2(enc_ctx, enc, None) != 0:
            raise TranscodeFailed(f"编码器初始化失败：{encoder_name.decode()}")

        swr_ctx = ctypes.c_void_p()
        pkt, frame_obj, out_pkt = self.p_pkt_alloc(), self.p_frame_alloc(), self.p_pkt_alloc()
        pcm = bytearray()
        total_samples = 0
        f_sr, f_cl = self._frame_offsets if self._frame_offsets else (None, None)

        with open(dst, "wb") as fh:
            if action == "flac":
                fh.write(b"\x00" * 42)

            def drain() -> None:
                while self.p_receive_packet(enc_ctx, out_pkt) >= 0:
                    data, size = self._ptr(out_pkt, _PKT_DATA), self._i32(out_pkt, _PKT_SIZE)
                    if data and size:
                        fh.write(bytes((ctypes.c_char * size).from_address(data)))
                    self.p_pkt_unref(out_pkt)

            def emit(n_samples: int, payload: bytes) -> None:
                """从零构建编码帧（不用 copy_props —— 它会带上缓冲引用，get_buffer 会失败）。"""
                if f_cl is None:
                    raise TranscodeFailed("输入没有可解码的音频帧")
                ef = self.p_frame_alloc()
                self._w32(ef, _FRAME_NBSAMPLES, n_samples)
                self._w32(ef, _FRAME_FORMAT, 1)
                if f_sr is not None and f_sr >= 0:
                    self._w32(ef, f_sr, rate)
                self._w32(ef, f_cl, in_lay.order or 1)
                self._w32(ef, f_cl + 4, nb)
                ctypes.c_uint64.from_address(ef + f_cl + 8).value = in_lay.u
                if self.p_frame_get_buffer(ef, 0) != 0:
                    raise TranscodeFailed("音频帧缓冲分配失败")
                dst_ptr = self._ptr(ef, 0)
                ctypes.memmove(ctypes.c_void_p(dst_ptr), payload, len(payload))
                if self.p_send_frame(enc_ctx, ef) < 0:
                    raise TranscodeFailed("编码器拒绝该帧")
                drain()

            def pump() -> None:
                nonlocal total_samples
                while len(pcm) >= frame_size * 2 * nb:
                    n_bytes = frame_size * 2 * nb
                    chunk = bytes(pcm[:n_bytes])
                    del pcm[:n_bytes]
                    emit(frame_size, chunk)
                    total_samples += frame_size

            def absorb(fa: int) -> None:
                nonlocal swr_ctx, f_sr, f_cl
                f_nb = self._i32(fa, _FRAME_NBSAMPLES) or 0
                f_fmt = self._i32(fa, _FRAME_FORMAT)
                if f_nb <= 0 or f_fmt is None:
                    return
                if self._frame_offsets is None:
                    f_sr, f_cl = self._frame_field_offsets(fa, rate, nb)
                if not swr_ctx.value:
                    out_lay = ChLayout(in_lay.order or 1, nb, in_lay.u)
                    rc = self.p_swr_opts2(ctypes.byref(swr_ctx), ctypes.byref(out_lay), 1, rate,
                                          ctypes.byref(in_lay), f_fmt, rate, 0, None)
                    if rc < 0 or self.p_swr_init(swr_ctx) < 0:
                        raise TranscodeFailed("重采样器初始化失败")
                in_ptrs = (ctypes.c_void_p * 8)()
                for ch in range(min(nb, 8)):
                    in_ptrs[ch] = self._ptr(fa, ch * 8)
                ob = (ctypes.c_char * (f_nb * 2 * nb))()
                o_ptrs = (ctypes.c_void_p * 2)(ctypes.cast(ob, ctypes.c_void_p), None)
                got = self.p_swr_convert(swr_ctx, o_ptrs, f_nb, in_ptrs, f_nb)
                if got > 0:
                    pcm.extend(bytes(ob)[:got * 2 * nb])
                    pump()

            while True:
                if self.p_read_frame(fmt, pkt) < 0:
                    break
                self.p_send_packet(dec_ctx, pkt)
                self.p_pkt_unref(pkt)
                while self.p_receive_frame(dec_ctx, frame_obj) >= 0:
                    absorb(frame_obj)
            self.p_send_packet(dec_ctx, None)
            while self.p_receive_frame(dec_ctx, frame_obj) >= 0:
                absorb(frame_obj)
            if pcm:
                n_bytes = frame_size * 2 * nb
                tail = len(pcm) // (2 * nb)
                chunk = bytes(pcm) + b"\x00" * (n_bytes - len(pcm))
                emit(frame_size, chunk)
                total_samples += tail
                pcm.clear()
            self.p_send_frame(enc_ctx, None)
            drain()
            if action == "flac":
                fh.seek(0)
                fh.write(_flac_header(rate, nb, 16, total_samples, frame_size))


def _flac_header(rate: int, channels: int, bits: int, total: int, block: int) -> bytes:
    si = struct.pack(">HH", block, block) + b"\x00" * 3 + b"\x00" * 3
    packed = (rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36) | total
    si += packed.to_bytes(8, "big") + b"\x00" * 16
    assert len(si) == 34
    return b"fLaC" + bytes([0x80]) + (34).to_bytes(3, "big") + si


# --------------------------------------------------------------------------- #
# 3) 单例 + 对外 API
# --------------------------------------------------------------------------- #
_engine: "Optional[_Engine]" = None
_load_reason = ""
_lock = threading.Lock()


def _load() -> "Optional[_Engine]":
    global _engine, _load_reason
    if _engine is not None:
        return _engine
    if _load_reason:
        return None
    d = _lib_dir()
    if d is None:
        _load_reason = "未找到 PySide6 自带的 FFmpeg 库"
        return None
    try:
        try:
            os.add_dll_directory(str(d))
        except (AttributeError, OSError):
            pass
        libs = {}
        for name in _LIB_NAMES:
            hit = sorted(d.glob(f"{name}-*.dll"))
            if not hit:
                raise EngineUnavailable(f"缺少 {name}-*.dll")
            libs[name] = ctypes.CDLL(str(hit[0]))
        _engine = _Engine(libs)
    except Exception as exc:  # noqa: BLE001
        _load_reason = f"{type(exc).__name__}: {exc}"
        _engine = None
    return _engine


def available() -> bool:
    return _load() is not None


def unavailable_reason() -> str:
    _load()
    return _load_reason


def engine_info() -> str:
    eng = _load()
    if eng is None:
        return f"不可用（{_load_reason}）"
    return f"可用 · 库目录 {_lib_dir()} · 编码器 mp3_mf/flac"


def convert_bytes(data: bytes, src_ext: str, action: str) -> "tuple[bytes, str]":
    """内存音频 → ``action``（"mp3"/"flac"），返回 ``(新字节, 新扩展名)``。

    引擎不可用抛 :class:`EngineUnavailable`；本次转码失败抛 :class:`TranscodeFailed`
    —— 调用方应**保留原格式**并如实记录（绝不丢文件）。
    """
    eng = _load()
    if eng is None:
        raise EngineUnavailable(unavailable_reason() or "转码引擎不可用")
    with _lock:
        with tempfile.TemporaryDirectory(prefix="mmusic-tc-",
                                         ignore_cleanup_errors=True) as td:
            src = Path(td) / f"in.{src_ext or 'bin'}"
            dst = Path(td) / f"out.{action}"
            src.write_bytes(data)
            eng.transcode(src, dst, action)
            if not dst.exists() or dst.stat().st_size <= 0:
                raise TranscodeFailed("转码产物为空")
            return dst.read_bytes(), action

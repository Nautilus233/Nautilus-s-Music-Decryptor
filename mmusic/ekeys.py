"""QQ 音乐 ekey 的**按需静默获取**（用户自带登录态，仅本机保存）。

背景：musicex（2026 新版 ``.mgg`` / ``.mflac`` / ``.mmp4``）文件内不含密钥。
官方 ``music.vkey.GetEVkey`` 接口可以用**用户自己的**会话取到该文件的 ekey
（每个文件一个、约 22 小时有效）。本模块在“处理到 musicex 文件且缺 ekey”时
拉取一次，写入 ``<文件目录>/ekeys/<song_mid>__<文件名>.txt``（与社区 fetch_ekey
同款布局），于是该文件立刻变成“可离线解码”。

没有凭证 / 拉取失败时**静默降级**——完全维持原行为（如实报“需联网”并转备用搜索）。

设计约束（对齐社区格式笔记，避免打扰对方服务）：
* 单线程串行、每曲一次请求、瞬时失败约 3 秒后重试一次，**不并发、不循环**；
* ekey 以文件时间戳判断新鲜度（22 小时），过期才重拉；
* 凭证只存本机 ``settings.json``；**绝不写日志、不发给除 GetEVkey 之外的任何地址**。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .crypto import musicex

__all__ = ["EkeyError", "VALID_SECONDS", "API", "normalize_creds", "creds_configured",
           "build_request", "parse_response", "fetch_ekey", "ensure_ekey"]

API = "https://u.y.qq.com/cgi-bin/musicu.fcg"
VALID_SECONDS = 22 * 3600


class EkeyError(RuntimeError):
    """拉取 ekey 失败（未登录 / 风控 / 文件信息不匹配 / 网络）。"""


def normalize_creds(uin: str, key: str) -> "tuple[str, str]":
    """把用户输入整理成 ``(uin, cookie)``；支持直接粘贴**整条 Cookie**。"""
    uin = (uin or "").strip()
    key = (key or "").strip()
    if "=" in key:                                  # 用户粘的是整条 Cookie
        cookie = key
        if not uin:
            for part in key.split(";"):
                name, _, value = part.strip().partition("=")
                if name.strip().lower() in ("qqmusic_uin", "uin"):
                    uin = value.strip()
        return uin, cookie
    if not key:
        return uin, ""
    return uin, f"qqmusic_key={key}; qqmusic_uin={uin}"


def creds_configured(settings) -> bool:
    _, cookie = normalize_creds(getattr(settings, "qqmusic_uin", ""),
                                getattr(settings, "qqmusic_key", ""))
    return bool(cookie)


def build_request(uin: str, song_mid: str, filename: str) -> dict:
    """官方 GetEVkey 的请求体（纯函数，便于离线测试）。"""
    return {
        "comm": {"uin": uin, "format": "json", "ct": 24, "cv": 0,
                 "platform": "yqq.json", "needNewCode": 1, "tmeLoginType": 2},
        "req_1": {"module": "music.vkey.GetEVkey", "method": "CgiGetEVkey",
                  "param": {"filename": [filename], "songmid": [song_mid],
                            "songtype": [1], "uin": uin, "loginflag": 1,
                            "platform": "27", "ctx": 1, "guid": "10000"}},
    }


def parse_response(payload: dict) -> str:
    """从响应取 ekey；失败抛 :class:`EkeyError`（可读原因）。"""
    req = (payload or {}).get("req_1") or {}
    code = req.get("code", 0)
    if code not in (0, None):
        info = req.get("info") or req.get("subcode") or ""
        raise EkeyError(f"接口返回 code={code}{('：' + str(info)) if info else ''}")
    entries = ((req.get("data") or {}).get("midurlinfo")) or []
    ekey = ((entries[0] or {}).get("ekey") if entries else "") or ""
    if not ekey:
        raise EkeyError("接口未返回 ekey（可能未登录 / 被风控 / 文件信息不匹配）")
    return str(ekey)


def fetch_ekey(http, uin: str, cookie: str, song_mid: str, filename: str,
               *, retry_delay: float = 3.0, timeout: float = 20.0) -> str:
    """拉一次 ekey；瞬时失败按社区建议重试一次（默认 3 秒后）。"""
    body = json.dumps(build_request(uin, song_mid, filename)).encode("utf-8")
    headers = {"Content-Type": "application/json", "Cookie": cookie,
               "User-Agent": "QQMusic/21", "Referer": "https://y.qq.com/"}
    last = ""
    for attempt in (0, 1):
        try:
            status, raw, _ = http.request("POST", API, data=body, headers=headers,
                                          timeout=timeout)
            if status >= 400:
                last = f"HTTP {status}"
            else:
                try:
                    payload = json.loads(raw.decode("utf-8", "replace"))
                except ValueError as exc:
                    last = f"响应不是 JSON（{exc}）"
                else:
                    return parse_response(payload)
        except Exception as exc:  # noqa: BLE001 - 网络异常一律降级
            last = f"{type(exc).__name__}: {exc}"
        if attempt == 0:
            time.sleep(max(0.0, retry_delay))
    raise EkeyError(last or "拉取失败")


def ensure_ekey(http, settings, src_path: "str | Path",
                *, retry_delay: float = 3.0, timeout: float = 20.0,
                force: bool = False) -> "tuple[bool, str]":
    """确保 ``src_path``（musicex 文件）有可用 ekey。

    返回 ``(是否可用, 说明)``；说明用于日志，**不含任何凭证内容**。

    ``force=True`` 时忽略“本地已有新鲜 ekey”的缓存判断，强制重取一次 ——
    用于本地 ekey 虽在 22 小时窗口内、却已失效（换机 / 被提前轮换）的自愈场景。
    """
    src = Path(src_path)
    tail = musicex.parse_file(src)
    if tail is None:
        return False, "不是 musicex 文件（尾部无法解析）"
    existing = musicex.find_ekey_path(src, tail.song_mid, tail.filename)
    if existing is not None and not force:
        try:
            age = time.time() - existing.stat().st_mtime
            if age < VALID_SECONDS:
                return True, f"已有本地 ekey（约 {int(age // 3600)} 小时前获取）"
        except OSError:
            pass
    uin, cookie = normalize_creds(getattr(settings, "qqmusic_uin", ""),
                                  getattr(settings, "qqmusic_key", ""))
    if not cookie:
        return False, "未配置 QQ 凭证（设置页可填 qqmusic_key；留空则关闭自动获取）"
    try:
        ekey = fetch_ekey(http, uin, cookie, tail.song_mid, tail.filename,
                          retry_delay=retry_delay, timeout=timeout)
    except EkeyError as exc:
        return False, (f"自动获取 ekey 失败：{exc}"
                       "（若提示未登录/失效：请到 设置 → QQ 音乐凭证 重新粘贴一次）")
    try:
        target_dir = src.parent / "ekeys"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / f"{tail.song_mid}__{tail.filename}.txt").write_text(ekey, encoding="utf-8")
    except OSError as exc:
        return False, f"ekey 写入失败：{exc}"
    return True, f"已静默获取 ekey（{tail.song_mid}，约 22 小时内有效）"

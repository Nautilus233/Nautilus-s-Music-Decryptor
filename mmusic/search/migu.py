"""Migu (咪咕音乐) — search + direct-link resolve.

公开 H5 接口，免登录即可取得 PQ（128k mp3）直链；HQ/SQ 对多数曲目要求登录
（接口返回 cannotCode=440003“登录后免费试听”），适配器自动降级到 PQ，
绝不尝试绕过登录。

实测（2026-10-18，真实全链路）：
* 检索：``app.c.nf.migu.cn/MIGUM2.0/v1.0/content/search_all.do``
  —— ``searchSwitch`` 必填；返回 ``songResultData.result[]``，含
  contentId / copyrightId / resourceType / singers / albums / rateFormats。
* 播放：``c.musicapp.migu.cn/MIGUM2.0/v2.0/content/listen-url``
  —— ``channel`` 必须放在**请求头**里（放进查询参数会报“channel不能为空”）；
  成功时 ``data.url`` 指向 ``freetyst.nf.migu.cn`` CDN。
* 实测下载与官方声明字节数一致（《光年之外》PQ 3769366 B），说明拿到的是
  整曲；真正的完整度仍由流水线时长门禁复核，不依赖本模块的判断。
"""
from __future__ import annotations

import urllib.parse

from ..errors import NetworkError
from ..model import Track
from .base import SearchSource

_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1")
_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://music.migu.cn/",
    "channel": "014021I",
    "subchannel": "014021I",
}
_SEARCH_SWITCH = ('{"song":1,"album":0,"singer":0,"tagSong":1,'
                  '"mvSong":0,"songlist":0,"bestShow":1}')
#: 用户音质 -> toneFlag 尝试顺序。免费侧实际只会给到 PQ；SQ 失败自动降级 PQ。
_TONES = {"mp3": ("PQ",), "flac": ("SQ", "PQ"), "wav": ("SQ", "PQ")}
_EXT = {"SQ": "flac", "ZQ": "flac", "HQ": "mp3", "PQ": "mp3", "LQ": "mp3"}
_BITRATE = {"LQ": 48, "PQ": 128, "HQ": 320}


def _declared_size(item: dict, fmt: str) -> int:
    """官方声明的文件字节数（用于台账与日志，不是硬门槛）。"""
    for key in ("newRateFormats", "rateFormats"):
        for entry in item.get(key) or []:
            if str(entry.get("formatType") or "") == fmt:
                try:
                    return int(entry.get("size") or 0)
                except (TypeError, ValueError):
                    return 0
    return 0


class MiguSource(SearchSource):
    name = "migu"
    label = "咪咕音乐"

    SEARCH = "https://app.c.nf.migu.cn/MIGUM2.0/v1.0/content/search_all.do"
    LISTEN = "https://c.musicapp.migu.cn/MIGUM2.0/v2.0/content/listen-url"

    def search(self, query: str, *, quality: str = "mp3", limit: int = 10) -> list[Track]:
        url = self.SEARCH + "?" + urllib.parse.urlencode({
            "text": query,
            "pageNo": "1",
            "pageSize": str(max(1, min(limit, 20))),
            "sort": "1",
            "searchSwitch": _SEARCH_SWITCH,
        })
        data = self.http.get_json(url, headers=_HEADERS)
        result = ((data or {}).get("songResultData") or {}).get("result") or []
        tracks: list[Track] = []
        for item in result:
            singers = [s.get("name") or "" for s in (item.get("singers") or [])]
            albums = [a.get("name") or "" for a in (item.get("albums") or [])]
            tracks.append(Track(
                source=self.name,
                title=(item.get("name") or "").strip(),
                artist=" / ".join(n for n in singers if n),
                album=next((n for n in albums if n), ""),
                # 检索接口不带时长：真实时长由流水线对直链做头部探测得到
                duration=0,
                bitrate=0,
                ext="mp3",
                raw=item,
            ))
        return tracks

    def resolve(self, track: Track, *, quality: str = "mp3") -> Track:
        item = track.raw or {}
        content_id = str(item.get("contentId") or item.get("id") or "")
        copyright_id = str(item.get("copyrightId") or "")
        resource_type = str(item.get("resourceType") or "2")
        if not (content_id and copyright_id):
            raise NetworkError("咪咕条目缺少 id/copyrightId", detail=track.title)
        tones = _TONES.get(quality) or _TONES["mp3"]
        last = ""
        for tone in tones:
            url = self.LISTEN + "?" + urllib.parse.urlencode({
                "copyrightId": copyright_id,
                "contentId": content_id,
                "resourceType": resource_type,
                "netType": "01",
                "toneFlag": tone,
            })
            try:
                data = self.http.get_json(url, headers=_HEADERS)
            except NetworkError as exc:
                last = exc.message or str(exc)
                continue
            data = data or {}
            payload = data.get("data") or {}
            play_url = payload.get("url") or ""
            if data.get("code") == "000000" and play_url:
                fmt = str(payload.get("formatType") or tone)
                track.url = play_url
                track.ext = _EXT.get(fmt, _EXT.get(tone, "mp3"))
                track.bitrate = _BITRATE.get(fmt, _BITRATE.get(tone, 0))
                track.size = _declared_size(item, fmt) or track.size
                return track
            dialog = payload.get("dialogInfo") or {}
            last = (data.get("info") or dialog.get("text")
                    or payload.get("cannotCode") or "") or "无直链"
        raise NetworkError("咪咕该曲目无免费直链", detail=last or track.title)

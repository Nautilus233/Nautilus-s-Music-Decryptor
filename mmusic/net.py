"""A tiny, dependency-free HTTP client (urllib based).

Deliberately avoids `requests` so the frozen .exe stays small and free of
extra PyInstaller hooks / TLS-certificate packaging headaches.  Provides
timeouts, exponential-backoff retries, gzip decoding and a streamed
download with progress + size cap.
"""
from __future__ import annotations

import gzip
import json
import random
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterable, Optional

from .errors import NetworkError

__all__ = ["HttpClient", "DEFAULT_UA"]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 522, 524}


class HttpClient:
    def __init__(self, *, timeout: float = 12.0, retries: int = 3,
                 user_agent: str = DEFAULT_UA, headers: Optional[dict] = None,
                 proxy: str = "") -> None:
        self.timeout = timeout
        self.retries = retries
        self.base_headers = {"User-Agent": user_agent,
                             "Accept": "*/*",
                             "Accept-Encoding": "gzip"}
        if headers:
            self.base_headers.update(headers)
        if proxy:
            self._opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            self._opener = urllib.request.build_opener()
        self._ctx = ssl.create_default_context()

    # ------------------------------------------------------------------ #
    def _headers(self, extra: Optional[dict]) -> dict:
        h = dict(self.base_headers)
        if extra:
            h.update(extra)
        return h

    def request(self, method: str, url: str, *, data: "bytes | None" = None,
                headers: Optional[dict] = None, timeout: Optional[float] = None,
                retries: Optional[int] = None) -> tuple[int, bytes, dict]:
        """Return ``(status, body, headers)``.  Raises NetworkError only when
        every retry of a *transient* failure is exhausted.  Client errors
        (404/403/...) are returned so the caller can switch source."""
        attempts = self.retries if retries is None else retries
        to = self.timeout if timeout is None else timeout
        last: "Exception | None" = None

        for i in range(attempts + 1):
            req = urllib.request.Request(url, data=data, headers=self._headers(headers), method=method)
            try:
                with self._opener.open(req, timeout=to) as resp:
                    raw = resp.read()
                    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                        try:
                            raw = gzip.decompress(raw)
                        except OSError:
                            pass
                    return resp.status, raw, dict(resp.headers)
            except urllib.error.HTTPError as exc:
                body = b""
                try:
                    body = exc.read()
                except Exception:  # noqa: BLE001
                    pass
                if exc.code in _RETRYABLE_STATUS and i < attempts:
                    last = exc
                    time.sleep(self._backoff(i))
                    continue
                return exc.code, body, dict(exc.headers or {})
            except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError, OSError) as exc:
                last = exc
                if i < attempts:
                    time.sleep(self._backoff(i))
                    continue
                raise NetworkError("网络请求失败", detail=f"{url} :: {exc}") from exc

        raise NetworkError("网络请求失败", detail=f"{url} :: {last}")

    @staticmethod
    def _backoff(i: int) -> float:
        return min(6.0, (0.6 * (2 ** i)) + random.uniform(0, 0.3))

    # ------------------------------------------------------------------ #
    def get_bytes(self, url: str, *, headers: Optional[dict] = None, **kw) -> bytes:
        status, body, _ = self.request("GET", url, headers=headers, **kw)
        if status >= 400:
            raise NetworkError(f"HTTP {status}", detail=url)
        return body

    def get_text(self, url: str, *, encoding: str = "utf-8", headers: Optional[dict] = None, **kw) -> str:
        return self.get_bytes(url, headers=headers, **kw).decode(encoding, "replace")

    def get_json(self, url: str, *, headers: Optional[dict] = None, **kw):
        text = self.get_text(url, headers=headers, **kw)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise NetworkError("接口返回的不是合法 JSON", detail=str(exc)) from exc

    def download(self, url: str, *, on_progress: "Callable[[int, int], None] | None" = None,
                 max_bytes: int = 200 * 1024 * 1024, headers: Optional[dict] = None,
                 timeout: Optional[float] = None) -> bytes:
        to = self.timeout if timeout is None else timeout
        req = urllib.request.Request(url, headers=self._headers(headers), method="GET")
        try:
            with self._opener.open(req, timeout=to) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                total = int(resp.headers.get("Content-Length") or 0)
                chunks: list[bytes] = []
                got = 0
                while True:
                    block = resp.read(1 << 16)
                    if not block:
                        break
                    got += len(block)
                    if got > max_bytes:
                        raise NetworkError("文件超过大小上限，已中止", detail=url)
                    chunks.append(block)
                    if on_progress:
                        on_progress(got, total)
                data = b"".join(chunks)
                if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                    try:
                        data = gzip.decompress(data)
                    except OSError:
                        pass
                if "text/html" in ctype and not data[:1] in (b"{", b"["):
                    raise NetworkError("下载到的不是音频（返回了网页）", detail=url)
                return data
        except urllib.error.HTTPError as exc:
            raise NetworkError(f"下载失败 HTTP {exc.code}", detail=url) from exc
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
            raise NetworkError("下载失败", detail=f"{url} :: {exc}") from exc

    def probe_tail(self, url: str, *, limit: int = 128 * 1024,
                   headers: Optional[dict] = None, timeout: Optional[float] = None
                   ) -> tuple[bytes, int]:
        """Fetch just the *tail* of a media file plus its advertised size.

        Some containers keep the length at the end (Ogg's last page granule,
        an m4a whose `moov` is not "faststart"), so a head-only probe cannot
        measure them."""
        to = self.timeout if timeout is None else timeout
        hdr = {"Accept-Encoding": "identity", "Range": f"bytes=-{limit}"}
        if headers:
            hdr.update(headers)
        req = urllib.request.Request(url, headers=self._headers(hdr), method="GET")
        try:
            with self._opener.open(req, timeout=to) as resp:
                data = resp.read(limit)
                total = 0
                content_range = resp.headers.get("Content-Range") or ""
                if "/" in content_range:
                    try:
                        total = int(content_range.rsplit("/", 1)[1])
                    except ValueError:
                        total = 0
                if not total:
                    total = int(resp.headers.get("Content-Length") or 0)
                return data, total
        except urllib.error.HTTPError as exc:
            raise NetworkError(f"探测失败 HTTP {exc.code}", detail=url) from exc
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
            raise NetworkError("探测失败", detail=f"{url} :: {exc}") from exc

    def probe(self, url: str, *, limit: int = 256 * 1024,
              headers: Optional[dict] = None, timeout: Optional[float] = None
              ) -> tuple[bytes, int]:
        """Fetch just the *head* of a media file plus its advertised size.

        Used to read a container header (duration/bitrate) without downloading
        the whole thing: sends a Range request when the server honours it, and
        always stops reading after `limit` bytes.  Returns (head_bytes, total)."""
        to = self.timeout if timeout is None else timeout
        hdr = {"Accept-Encoding": "identity", "Range": f"bytes=0-{limit - 1}"}
        if headers:
            hdr.update(headers)
        req = urllib.request.Request(url, headers=self._headers(hdr), method="GET")
        try:
            with self._opener.open(req, timeout=to) as resp:
                data = resp.read(limit)
                total = 0
                content_range = resp.headers.get("Content-Range") or ""
                if "/" in content_range:
                    try:
                        total = int(content_range.rsplit("/", 1)[1])
                    except ValueError:
                        total = 0
                if not total:
                    total = int(resp.headers.get("Content-Length") or 0)
                return data, total
        except urllib.error.HTTPError as exc:
            raise NetworkError(f"探测失败 HTTP {exc.code}", detail=url) from exc
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
            raise NetworkError("探测失败", detail=f"{url} :: {exc}") from exc

    def close(self) -> None:
        try:
            self._opener.close()
        except Exception:  # noqa: BLE001
            pass
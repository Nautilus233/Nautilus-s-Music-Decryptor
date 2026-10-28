"""A tiny, dependency-free HTTP client (urllib based).

Deliberately avoids `requests` so the frozen .exe stays small and free of
extra PyInstaller hooks / TLS-certificate packaging headaches.  Provides
timeouts, exponential-backoff retries, gzip decoding and a streamed
download with progress + size cap.
"""
from __future__ import annotations

import gzip
import http.client
import json
import random
import socket
import ssl
import threading
from contextlib import contextmanager
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterable, Optional

from .errors import CancelledError, NetworkError

__all__ = ["HttpClient", "DEFAULT_UA"]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 522, 524}


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, client):
        super().__init__()
        self.client = client

    def http_open(self, req):
        return self.do_open(lambda host, **kw: self.client._connection(
            http.client.HTTPConnection, host, **kw), req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, client, context):
        super().__init__(context=context)
        self.client = client

    def https_open(self, req):
        return self.do_open(lambda host, **kw: self.client._connection(
            http.client.HTTPSConnection, host, **kw), req, context=self._context)


class HttpClient:
    def __init__(self, *, timeout: float = 12.0, retries: int = 3,
                 user_agent: str = DEFAULT_UA, headers: Optional[dict] = None,
                 proxy: str = "") -> None:
        self.timeout = timeout
        self.retries = retries
        self._cancelled = threading.Event()
        self._socket_lock = threading.Lock()
        self._sockets: set = set()
        self._request_state = threading.local()
        self.base_headers = {"User-Agent": user_agent,
                             "Accept": "*/*",
                             "Accept-Encoding": "gzip"}
        if headers:
            self.base_headers.update(headers)
        # NOTE: urllib 默认用**全局** SSL 上下文，因此必须显式装上
        # HTTPSHandler(context=...)，否则这里的上下文根本不会被使用。
        self._ctx = ssl.create_default_context()
        # Python 3.13+ 默认打开 VERIFY_X509_STRICT，会把一类"Basic Constraints
        # 未标 critical"的**合法**证书直接判死（实测 archive.org 就是如此，
        # 表现为 CERTIFICATE_VERIFY_FAILED）。链式校验与主机名校验全部保留，
        # 只关掉这条更严格的额外检查 —— 否则会凭空失去一整个音源。
        try:
            self._ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT   # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass

        handlers: list = [_HTTPHandler(self), _HTTPSHandler(self, self._ctx)]
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        self._opener = urllib.request.build_opener(*handlers)

    def check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise CancelledError("已取消")

    def cancel(self) -> None:
        """Wake socket reads and retry waits; never close a buffered reader here.

        HTTPResponse.close() can wait for its read lock. Instead, detach and
        close the transport handle (also wakes Windows' pending socket select).
        The worker alone owns buffered response cleanup.
        DNS resolution / a pending OS connect remain bounded by the OS/timeout.
        """
        self._cancelled.set()
        with self._socket_lock:
            sockets = tuple(self._sockets)
        for sock in sockets:
            self._abort_socket(sock)

    @staticmethod
    def _abort_socket(sock) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            handle = sock.detach()      # prevents later double-close / handle reuse
            if handle != -1:
                socket.close(handle)    # socket.close is required for Windows handles
        except OSError:
            pass

    def _track_socket(self, sock) -> None:
        with self._socket_lock:
            self._sockets.add(sock)
        self._request_state.sockets.append(sock)
        if self._cancelled.is_set():
            self._abort_socket(sock)
            self.check_cancelled()

    def _connection(self, cls, host, **kw):
        conn = cls(host, **kw)
        create = conn._create_connection

        def create_socket(*args, **kwargs):
            sock = create(*args, **kwargs)
            try:
                self._track_socket(sock)     # before proxy CONNECT / response headers
            except Exception:
                sock.close()
                raise
            return sock

        connect = conn.connect

        def connect_socket():
            connect()
            if conn.sock is not None:
                self._track_socket(conn.sock)  # TLS wraps (and detaches) raw socket

        conn._create_connection = create_socket
        conn.connect = connect_socket
        return conn

    @contextmanager
    def _open(self, req, timeout):
        self.check_cancelled()
        sockets = []
        self._request_state.sockets = sockets
        resp = None
        try:
            try:
                resp = self._opener.open(req, timeout=timeout)
            except urllib.error.HTTPError as exc:
                resp = exc              # keep error bodies cancellable too
            self.check_cancelled()
            yield resp
            self.check_cancelled()
        except Exception:
            self.check_cancelled()      # aborted sockets mean cancellation, not failure
            raise
        finally:
            if resp is not None:
                resp.close()
            for sock in sockets:
                sock.close()
            with self._socket_lock:
                self._sockets.difference_update(sockets)
            self._request_state.sockets = []

    def _read(self, resp, limit=None) -> bytes:
        chunks = []
        got = 0
        while limit is None or got < limit:
            self.check_cancelled()
            block = resp.read(min(65536, limit - got) if limit is not None else 65536)
            self.check_cancelled()
            if not block:
                break
            chunks.append(block)
            got += len(block)
        return b"".join(chunks)

    def _wait_retry(self, attempt):
        if self._cancelled.wait(self._backoff(attempt)):
            self.check_cancelled()

    @staticmethod
    def _raise_for_status(resp, url) -> None:
        if resp.status >= 400:
            if isinstance(resp, urllib.error.HTTPError):
                raise resp              # _open() closes the original error response
            exc = urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, None)
            exc.close()                 # synthetic HTTPError also owns a file wrapper
            raise exc

    # ------------------------------------------------------------------ #
    @contextmanager
    def request_limits(self, *, timeout=3.0, retries=0):
        """Thread-local limits for optional metadata work; restore on failure."""
        previous = getattr(self._request_state, "limits", None)
        self._request_state.limits = (timeout, retries)
        try:
            yield
        finally:
            self._request_state.limits = previous

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
        limits = getattr(self._request_state, "limits", None)
        if limits is not None:
            to, attempts = min(to, limits[0]), min(attempts, limits[1])
        last: "Exception | None" = None

        for i in range(attempts + 1):
            self.check_cancelled()
            req = urllib.request.Request(url, data=data, headers=self._headers(headers), method=method)
            try:
                with self._open(req, to) as resp:
                    raw = self._read(resp)
                    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                        try:
                            raw = gzip.decompress(raw)
                        except OSError:
                            pass
                    status, response_headers = resp.status, dict(resp.headers)
                if status in _RETRYABLE_STATUS and i < attempts:
                    self._wait_retry(i)
                    continue
                return status, raw, response_headers
            except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError, OSError) as exc:
                last = exc
                if i < attempts:
                    self._wait_retry(i)
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
                 timeout: Optional[float] = None, retries: "int | None" = None) -> bytes:
        # 与 request() 一致：瞬时失败要退避重试。
        # 实测 upload.wikimedia.org 会对突发请求返回 429，不重试就等于整条源失效。
        attempts = self.retries if retries is None else retries
        for attempt in range(attempts + 1):
            self.check_cancelled()
            try:
                return self._download_once(url, on_progress=on_progress, max_bytes=max_bytes,
                                           headers=headers, timeout=timeout)
            except urllib.error.HTTPError as exc:
                if exc.code in _RETRYABLE_STATUS and attempt < attempts:
                    self._wait_retry(attempt)
                    continue
                raise NetworkError(f"下载失败 HTTP {exc.code}", detail=url) from exc

    def _download_once(self, url: str, *, on_progress: "Callable[[int, int], None] | None" = None,
                       max_bytes: int = 200 * 1024 * 1024, headers: Optional[dict] = None,
                       timeout: Optional[float] = None) -> bytes:
        to = self.timeout if timeout is None else timeout
        req = urllib.request.Request(url, headers=self._headers(headers), method="GET")
        try:
            with self._open(req, to) as resp:
                self._raise_for_status(resp, url)
                ctype = (resp.headers.get("Content-Type") or "").lower()
                total = int(resp.headers.get("Content-Length") or 0)
                chunks: list[bytes] = []
                got = 0
                while True:
                    self.check_cancelled()
                    block = resp.read(1 << 16)
                    self.check_cancelled()
                    if not block:
                        break
                    got += len(block)
                    if got > max_bytes:
                        raise NetworkError("文件超过大小上限，已中止", detail=url)
                    chunks.append(block)
                    if on_progress:
                        on_progress(got, total)
                data = b"".join(chunks)
                # 截断检测：服务端声明了长度却给少了 -> 宁可判失败也不要半个文件
                if total and got < total:
                    raise NetworkError("下载不完整（疑似被截断）",
                                       detail=f"{got}/{total} bytes :: {url}")
                if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                    try:
                        data = gzip.decompress(data)
                    except OSError:
                        pass
                if "text/html" in ctype and not data[:1] in (b"{", b"["):
                    raise NetworkError("下载到的不是音频（返回了网页）", detail=url)
                return data
        except urllib.error.HTTPError as exc:
            raise  # download() owns status retries and the user-facing error
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
            with self._open(req, to) as resp:
                self._raise_for_status(resp, url)
                data = self._read(resp, limit)
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
            with self._open(req, to) as resp:
                self._raise_for_status(resp, url)
                data = self._read(resp, limit)
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

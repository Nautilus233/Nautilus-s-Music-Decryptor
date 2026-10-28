"""Real Qt signals + localhost slow HTTP cancellation; no public network needed."""
from __future__ import annotations

import gzip
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from mmusic.config import AppConfig
from mmusic.errors import CancelledError, NetworkError
from mmusic.model import Track
from mmusic.net import HttpClient
from mmusic.pipeline import JobResult, Pipeline
from ui.name_search_dialog import NameSearchDialog
from ui.worker import NameSearchWorker


def pump_until(app, predicate, timeout=3.0):
    """Use real queued Qt signals while keeping the event loop responsive."""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.002)
    app.processEvents()
    return bool(predicate())


class SlowHTTP:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                owner.calls += 1
                try:
                    if self.path == "/headers":
                        owner.entered.set()
                        owner.release.wait(5)
                    if self.path == "/redirect":
                        self.send_response(302)
                        self.send_header("Location", "/gzip")
                        self.end_headers()
                        return
                    if self.path == "/retry":
                        self.send_response(503)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        owner.entered.set()
                        return
                    if self.path in ("/missing", "/error-body"):
                        data = b'{"error":"missing"}'
                        self.send_response(404)
                    else:
                        data = gzip.compress(b"valid response") if self.path == "/gzip" else b"x" * 131072
                        self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    if self.path == "/gzip":
                        self.send_header("Content-Encoding", "gzip")
                    self.end_headers()
                    if self.path in ("/body", "/error-body"):
                        self.wfile.write(data[:1])
                        self.wfile.flush()
                        owner.entered.set()
                        owner.release.wait(5)
                        self.wfile.write(data[1:])
                    else:
                        self.wfile.write(data)
                except (ConnectionError, OSError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class SingleSearchCancellationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="single_cancel_")
        self.cfg = AppConfig(path=Path(self.tmp.name) / "settings.json")
        self.cfg.settings.output_dir = self.tmp.name
        self.cfg.settings.remember_hashes = False
        self.cfg.settings.fetch_cover = False
        self.dialogs = []

    def tearDown(self):
        for dlg in self.dialogs:
            dlg.reject()
            self.assertTrue(pump_until(self.app, lambda: dlg._worker is None, 6))
            dlg.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def dialog(self):
        dlg = NameSearchDialog(self.cfg.settings, self.cfg)
        self.dialogs.append(dlg)
        dlg.names.setPlainText("Example")
        return dlg

    def test_search_then_pick_no_research_and_no_early_release(self):
        calls = []
        cleanup = threading.Event()
        chosen = Track("local", title="Example", artist="Artist", duration=60)

        class TestPipe:
            def __init__(self, *_a, **_kw):
                pass

            def search_candidates_ex(self, query, *, artist):
                calls.append(("search", query, artist))
                return [chosen], 60

            def download_chosen(self, track, *, reference):
                calls.append(("pick", track, reference))
                return JobResult("search:Example", "Example", "downloaded")

            def close(self):
                cleanup.wait(3)

            def cancel(self):
                cleanup.set()

        dlg = self.dialog()
        with patch("ui.worker.Pipeline", TestPipe):
            dlg.btn_search.click()
            self.assertTrue(pump_until(self.app, lambda: bool(dlg._candidates)))
            # candidates/completed are emitted inside run(); cleanup still blocked.
            self.assertIsNotNone(dlg._worker)
            self.assertFalse(dlg.btn_pick.isEnabled())
            self.assertFalse(dlg.btn_search.isEnabled())
            cleanup.set()
            self.assertTrue(pump_until(self.app, lambda: dlg._worker is None))
            self.assertTrue(dlg.btn_pick.isEnabled())
            dlg.btn_pick.click()
            self.assertTrue(pump_until(self.app, lambda: dlg._worker is None))
        self.assertEqual([c[0] for c in calls], ["search", "pick"])
        self.assertIs(calls[1][1], chosen)
        self.assertEqual(calls[1][2], 60)
        self.assertEqual(dlg.table.item(0, 0).text(), "已完成")
        dlg.names.setPlainText("Other song")
        self.assertFalse(dlg.btn_pick.isEnabled())

    def test_startup_cancel_does_not_construct_pipeline(self):
        worker = NameSearchWorker(self.cfg.settings, mode="list", query="Example")
        events = []
        worker.cancelled.connect(lambda: events.append("cancelled"))
        worker.failed.connect(lambda *_: events.append("failed"))
        worker.candidates.connect(lambda *_: events.append("candidates"))
        worker.cancel()
        with patch("ui.worker.Pipeline", side_effect=AssertionError("must not construct")):
            worker.start()
            self.assertTrue(pump_until(self.app, lambda: not worker.isRunning()))
        self.assertEqual(events, ["cancelled"])
        worker.deleteLater()

    def test_cancel_during_pipeline_construction(self):
        constructing, release = threading.Event(), threading.Event()
        events = []

        class TestPipe:
            def __init__(self, *_a, **_kw):
                constructing.set()
                release.wait(3)

            def cancel(self):
                events.append("pipe-cancel")

            def close(self):
                events.append("close")

            def search_candidates_ex(self, *_a, **_kw):
                raise AssertionError("must not search after startup cancel")

        worker = NameSearchWorker(self.cfg.settings, mode="list", query="Example")
        worker.cancelled.connect(lambda: events.append("cancelled"))
        with patch("ui.worker.Pipeline", TestPipe):
            worker.start()
            self.assertTrue(pump_until(self.app, constructing.is_set))
            worker.cancel()
            release.set()
            self.assertTrue(pump_until(self.app, lambda: not worker.isRunning()))
        self.assertEqual(set(events), {"pipe-cancel", "close", "cancelled"})
        self.assertLess(events.index("pipe-cancel"), events.index("close"))
        worker.deleteLater()

    def test_stopped_or_foreign_signals_do_not_replace_current_results(self):
        entered, release = threading.Event(), threading.Event()

        class TestPipe:
            def __init__(self, *_a, **_kw):
                pass

            def search_candidates_ex(self, *_a, **_kw):
                entered.set()
                release.wait(3)
                return [Track("local", title="stale")], None

            def cancel(self):
                pass                    # simulate CPU/cleanup that cannot instantly yield

            def close(self):
                pass

        dlg = self.dialog()
        with patch("ui.worker.Pipeline", TestPipe):
            dlg.btn_search.click()
            self.assertTrue(pump_until(self.app, entered.is_set))
            worker = dlg._worker
            dlg.btn_stop.click()
            worker.candidates.emit("stale", [Track("local", title="stale")], None)
            worker.failed.emit("late failure")
            worker.completed.emit([])
            self.app.processEvents()
            self.assertFalse(dlg._candidates)
            self.assertEqual(dlg.status.text(), "正在停止…")
            self.assertIs(dlg._worker, worker)
            release.set()
            self.assertTrue(pump_until(self.app, lambda: dlg._worker is None))
        old = NameSearchWorker(self.cfg.settings)
        old.candidates.connect(dlg._on_candidates)
        old.failed.connect(dlg._on_failed)
        old.completed.connect(dlg._on_completed)
        old.candidates.emit("stale", [Track("local", title="stale")], None)
        old.failed.emit("late failure")
        old.completed.emit([])
        self.app.processEvents()
        self.assertFalse(dlg._candidates)
        self.assertEqual(dlg.status.text(), "已停止")
        old.deleteLater()

    def test_slow_http_cancel_headers_body_probes_and_retry(self):
        for method, path in (("get_bytes", "/headers"), ("get_bytes", "/body"),
                             ("get_bytes", "/error-body"),
                             ("download", "/body"), ("probe", "/body"),
                             ("probe_tail", "/body"), ("get_bytes", "/retry"),
                             ("download", "/retry")):
            with self.subTest(method=method, path=path):
                server = SlowHTTP()
                client = HttpClient(timeout=5, retries=3)
                client._backoff = lambda _i: 5
                errors, done = [], threading.Event()

                def request():
                    try:
                        getattr(client, method)(server.url + path)
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        done.set()

                thread = threading.Thread(target=request)
                try:
                    thread.start()
                    self.assertTrue(pump_until(self.app, server.entered.is_set))
                    # Give /retry time to enter its interruptible backoff.
                    if path == "/retry":
                        pump_until(self.app, lambda: False, .03)
                    started = time.monotonic()
                    client.cancel()
                    handler_ms = 1000 * (time.monotonic() - started)
                    self.assertLess(handler_ms, 250)
                    self.assertTrue(pump_until(self.app, done.is_set, 1.5))
                    finished_ms = 1000 * (time.monotonic() - started)
                    print(f"cancel {method}{path}: handler={handler_ms:.2f}ms finished={finished_ms:.2f}ms",
                          flush=True)
                    self.assertEqual(len(errors), 1)
                    self.assertIsInstance(errors[0], CancelledError)
                    self.assertEqual(server.calls, 1)
                    self.assertFalse(client._sockets)
                finally:
                    client.cancel()
                    server.close()
                    thread.join(6)
                    client.close()

    def test_ui_stop_and_close_nonblocking_during_real_http(self):
        for close in (False, True):
            with self.subTest(close=close):
                server = SlowHTTP()
                dlg = self.dialog()
                real_pipe = Pipeline

                class HTTPPipe(real_pipe):
                    def __init__(self, settings, **kw):
                        super().__init__(settings, sources=[], **kw)

                    def search_candidates_ex(self, *_a, **_kw):
                        self.http.get_bytes(server.url + "/body")
                        return [], None

                beats = []
                timer = QTimer()
                timer.setInterval(5)
                timer.timeout.connect(lambda: beats.append(time.monotonic()))
                timer.start()
                try:
                    with patch("ui.worker.Pipeline", HTTPPipe):
                        dlg.show()
                        dlg.btn_search.click()
                        self.assertTrue(pump_until(self.app, server.entered.is_set))
                        started = time.monotonic()
                        dlg.reject() if close else dlg.btn_stop.click()
                        handler_ms = 1000 * (time.monotonic() - started)
                        self.assertLess(handler_ms, 250)
                        self.assertTrue(pump_until(self.app, lambda: dlg._worker is None, 1.5))
                        finished_ms = 1000 * (time.monotonic() - started)
                        print(f"UI {'close' if close else 'stop'}: handler={handler_ms:.2f}ms finished={finished_ms:.2f}ms",
                              flush=True)
                        self.assertEqual(dlg.status.text(), "已停止")
                        self.assertTrue(beats)
                        self.assertFalse(dlg.btn_stop.isEnabled())
                        self.assertTrue(dlg.btn_search.isEnabled())
                        if close:
                            self.assertFalse(dlg.isVisible())
                finally:
                    timer.stop()
                    server.close()

    def test_http_status_redirect_gzip_and_retry_contract(self):
        server = SlowHTTP()
        client = HttpClient(timeout=2, retries=1)
        client._backoff = lambda _i: 0
        try:
            self.assertEqual(client.get_bytes(server.url + "/redirect"), b"valid response")
            status, body, _headers = client.request("GET", server.url + "/missing")
            self.assertEqual((status, body), (404, b'{"error":"missing"}'))
            with self.assertRaises(NetworkError):
                client.download(server.url + "/missing")
            calls = server.calls
            with self.assertRaises(NetworkError):
                client.download(server.url + "/retry")
            self.assertEqual(server.calls - calls, 2)
        finally:
            client.close()
            server.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

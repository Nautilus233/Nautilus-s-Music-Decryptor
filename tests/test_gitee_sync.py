"""Offline integration tests for release synchronization; no credentials needed."""
import copy
from email.parser import BytesParser
from email.policy import default
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

SPEC = importlib.util.spec_from_file_location("sync_gitee", Path(__file__).resolve().parents[1] / "tools/sync_gitee.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class Response(io.BytesIO):
    pass


class FakeService:
    def __init__(self):
        self.release = None
        self.files = []
        self.blobs = {"https://github.test/app": b"MZ-original-exe"}
        self.uploads = 0
        self.next_id = 1
        self.corrupt = False
        self.assets = [{"id": 12, "name": "MusicDecryptor.exe", "state": "uploaded",
                        "size": 15, "browser_download_url": "https://github.test/app"}]

    def open(self, req, timeout):
        url = req.full_url
        if url in self.blobs:
            return Response(self.blobs[url])
        parts = urllib.parse.urlsplit(url)
        method = req.get_method()
        path = parts.path
        result = None
        if parts.hostname == "gh.test":
            result = self.assets
        elif path.endswith("/tags/v1"):
            if self.release is None:
                raise urllib.error.HTTPError(url, 404, "missing", {}, None)
            result = self.release
        elif path.endswith("/attach_files"):
            if method == "GET":
                result = self.files
            else:
                data = req.data.read()
                assert len(data) == int(req.headers["Content-length"])
                message = BytesParser(policy=default).parsebytes(
                    ("Content-Type: " + req.headers["Content-type"] + "\r\n\r\n").encode() + data)
                fields = {p.get_param("name", header="content-disposition"): p for p in message.iter_parts()}
                assert fields["access_token"].get_payload(decode=True) == b"private-token"
                file = fields["file"]
                payload = file.get_payload(decode=True)
                self.uploads += 1
                item = {"id": self.next_id, "name": file.get_filename(), "size": len(payload),
                        "browser_download_url": f"https://gitee.test/file/{self.next_id}"}
                self.next_id += 1
                self.blobs[item["browser_download_url"]] = b"X" * len(payload) if self.corrupt else payload
                self.files.append(item)
                result = item
        elif "/attach_files/" in path and method == "DELETE":
            self.files = [x for x in self.files if str(x["id"]) != path.rsplit("/", 1)[1]]
        elif method in ("POST", "PATCH"):
            result = json.loads(req.data)
            if method == "POST":
                assert result["target_commitish"] == "v1"
            result["id"] = 9
            self.release = result
        else:
            raise AssertionError((method, path))
        return Response(json.dumps(result).encode() if result is not None else b"")


class ReleaseSyncTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.service.assets[0]["size"] = len(self.service.blobs["https://github.test/app"])
        self.release = {"id": 1, "tag_name": "v1", "name": "Version 1", "body": "Notes", "prerelease": True}
        self.gh = sync.API("https://gh.test", "github-token")
        self.gt = sync.API("https://gt.test", "private-token", gitee=True)
        self.net = patch.object(sync.urllib.request, "urlopen", side_effect=self.service.open)
        self.net.start()
        self.addCleanup(self.net.stop)

    def run_sync(self):
        sync.sync_release(self.gh, self.gt, "/repos/gh/repo/releases", "/repos/gt/repo/releases", self.release)

    def test_create_upload_and_idempotent_rerun(self):
        self.run_sync()
        self.assertTrue(self.service.release["prerelease"])
        self.assertEqual(self.service.uploads, 1)
        self.release["body"] = "Updated notes"
        self.run_sync()
        self.assertEqual(self.service.release["body"], "Updated notes")
        self.assertEqual(self.service.uploads, 1)
        self.assertEqual(len(self.service.files), 1)

    def test_changed_same_name_same_size_preserves_published_bytes(self):
        self.run_sync()
        data = self.service.blobs["https://github.test/app"]
        self.service.blobs["https://github.test/app"] = b"Z" * len(data)
        original = copy.deepcopy(self.service.files)
        with self.assertRaisesRegex(sync.SyncError, "Immutable release attachment differs"):
            self.run_sync()
        self.assertEqual(self.service.uploads, 1)
        self.assertEqual(self.service.files, original)
        self.assertEqual(len(self.service.files), 1)

    def test_unicode_filename_and_multiple_assets(self):
        asset = copy.deepcopy(self.service.assets[0])
        asset["name"] = "音乐解密工具.zip"
        self.service.assets.append(asset)
        self.run_sync()
        self.assertEqual({x["name"] for x in self.service.files}, {"MusicDecryptor.exe", "音乐解密工具.zip"})

    def test_bad_source_does_not_delete_existing_attachment(self):
        self.run_sync()
        self.service.assets[0]["size"] += 1
        with self.assertRaisesRegex(sync.SyncError, "size mismatch"):
            self.run_sync()
        self.assertEqual(len(self.service.files), 1)
        self.assertEqual(self.service.uploads, 1)

    def test_corrupt_upload_fails_and_is_removed(self):
        self.service.corrupt = True
        with self.assertRaisesRegex(sync.SyncError, "SHA-256 mismatch"):
            self.run_sync()
        self.assertEqual(self.service.files, [])

    def test_source_digest_and_unsafe_names_rejected(self):
        self.service.assets[0]["digest"] = "sha256:" + hashlib.sha256(b"wrong").hexdigest()
        with self.assertRaisesRegex(sync.SyncError, "SHA-256 mismatch"):
            self.run_sync()
        self.assertEqual(self.service.uploads, 0)
        for name in ("../app.exe", 'a"b.exe', "a\r\nb.exe"):
            with self.assertRaises(sync.SyncError):
                sync.safe_name(name)

    def test_draft_not_published(self):
        self.release["draft"] = True
        self.run_sync()
        self.assertIsNone(self.service.release)

    def test_auth_failure_redacts_token(self):
        def fail(req, **kwargs):
            raise urllib.error.HTTPError(req.full_url, 403, "private-token", {}, None)
        with patch.object(sync.urllib.request, "urlopen", side_effect=fail):
            with self.assertRaises(sync.SyncError) as error:
                self.run_sync()
        self.assertNotIn("private-token", str(error.exception))
        self.assertIn("403", str(error.exception))

    def test_pagination_does_not_stop_at_first_full_page(self):
        api = sync.API("https://gh.test", "token")
        with patch.object(api, "request", side_effect=[list(range(100)), [100]]) as call:
            self.assertEqual(api.pages("/assets"), list(range(101)))
        self.assertIn("page=2", call.call_args_list[-1].args[1])

    def test_manual_tag_and_all_releases(self):
        env = {"GITHUB_TOKEN": "gh", "GITEE_TOKEN": "gt", "GITHUB_REPOSITORY": "a/b",
               "GITEE_REPO": "c/d", "GITHUB_EVENT_NAME": "workflow_dispatch", "SYNC_TAG": "v1"}
        with patch.dict(os.environ, env, clear=True), patch.object(sync.API, "request", return_value=self.release) as req, patch.object(sync, "sync_release") as run:
            sync.main()
            self.assertTrue(req.call_args.args[1].endswith("/tags/v1"))
            self.assertEqual(run.call_count, 1)
        old = dict(self.release, tag_name="old", published_at="2020")
        new = dict(self.release, tag_name="new", published_at="2026")
        env["SYNC_TAG"] = ""
        with patch.dict(os.environ, env, clear=True), patch.object(sync.API, "pages", return_value=[new, old]), patch.object(sync, "sync_release") as run:
            sync.main()
            self.assertEqual([c.args[-1]["tag_name"] for c in run.call_args_list], ["old", "new"])

    def test_release_deletion_preserves_mirror_history(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            event_path.write_text(json.dumps({"action": "deleted", "release": self.release}), encoding="utf-8")
            env = {"GITHUB_TOKEN": "gh", "GITEE_TOKEN": "gt", "GITHUB_REPOSITORY": "a/b",
                   "GITEE_REPO": "c/d", "GITHUB_EVENT_NAME": "release", "GITHUB_EVENT_PATH": str(event_path)}
            with patch.dict(os.environ, env, clear=True), patch.object(sync.API, "request") as req:
                sync.main()
                req.assert_not_called()


if __name__ == "__main__":
    unittest.main()

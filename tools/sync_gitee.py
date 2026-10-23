"""CI-only GitHub -> Gitee release/attachment sync, using the standard library.

API reference: https://gitee.com/api/v5/doc_json (attach_files endpoints).
Never import this module from the desktop app or include it in the bundle.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid


class SyncError(RuntimeError):
    pass


class API:
    def __init__(self, base, token, gitee=False):
        self.base = base
        self.token = token
        self.gitee = gitee

    def request(self, method, path, payload=None, missing_ok=False):
        url = self.base + path
        headers = {"Accept": "application/json", "User-Agent": "MusicDecryptor-release-sync"}
        if self.gitee:
            sep = "&" if "?" in url else "?"
            url += sep + urllib.parse.urlencode({"access_token": self.token})
        else:
            headers["Authorization"] = "Bearer " + self.token
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 404 and missing_ok:
                return None
            # Do not print response bodies, URLs containing tokens, or tracebacks.
            raise SyncError(f"{method} {path.split('?')[0]}: HTTP {exc.code}") from None
        except (OSError, ValueError):
            raise SyncError(f"{method} {path.split('?')[0]}: network/JSON error") from None

    def pages(self, path):
        result = []
        for page in range(1, 1001):
            sep = "&" if "?" in path else "?"
            items = self.request("GET", f"{path}{sep}per_page=100&page={page}")
            if not isinstance(items, list):
                raise SyncError("Unexpected paginated API response")
            result.extend(items)
            if len(items) < 100:
                return result
        raise SyncError("Pagination limit exceeded")

    def upload(self, path, source, name):
        boundary = "sync-" + uuid.uuid4().hex
        # Build on disk and stream it; do not hold an exe plus multipart in RAM.
        with tempfile.TemporaryFile() as body:
            body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"access_token\"\r\n\r\n".encode())
            body.write(self.token.encode())
            body.write(f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode("utf-8"))
            with source.open("rb") as stream:
                shutil.copyfileobj(stream, body, 1024 * 1024)
            body.write(f"\r\n--{boundary}--\r\n".encode())
            length = body.tell()
            body.seek(0)
            req = urllib.request.Request(self.base + path, data=body, method="POST", headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(length),
                "User-Agent": "MusicDecryptor-release-sync",
            })
            try:
                with urllib.request.urlopen(req, timeout=600) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                exc.close()
                raise SyncError(f"Attachment upload: HTTP {exc.code}") from None
            except (OSError, ValueError):
                raise SyncError("Attachment upload failed; rerun the workflow") from None


def download(url, destination, expected_size=None):
    """Public attachment URL only: never forward either platform's token to storage."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise SyncError("Attachment URL must use HTTPS")
    digest = hashlib.sha256()
    size = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MusicDecryptor-release-sync"})
        with urllib.request.urlopen(req, timeout=120) as response, destination.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    except (OSError, ValueError):
        raise SyncError("Attachment download failed") from None
    if expected_size is not None and size != int(expected_size):
        raise SyncError("Attachment size mismatch")
    return digest.hexdigest()


def safe_name(name):
    if not name or name in (".", "..") or any(c in name for c in '/\\"\r\n\x00'):
        raise SyncError("Unsafe attachment filename")
    return name


def sync_release(gh, gt, gh_root, gt_root, release):
    if release.get("draft"):
        print("Skipping unpublished draft")
        return
    tag = release["tag_name"]
    existing = gt.request("GET", gt_root + "/tags/" + urllib.parse.quote(tag, safe=""), missing_ok=True)
    payload = {
        "tag_name": tag,
        "name": release.get("name") or tag,
        "body": release.get("body") or "",
        "prerelease": bool(release.get("prerelease")),
    }
    if existing is None:
        # Tag has already been pushed. Do not create it at an unrelated commit.
        payload["target_commitish"] = tag
        existing = gt.request("POST", gt_root, payload)
    else:
        existing = gt.request("PATCH", f"{gt_root}/{existing['id']}", payload)
    attachments_path = f"{gt_root}/{existing['id']}/attach_files"
    attached = gt.pages(attachments_path)
    # Query current assets, not an old release-event payload.
    assets = gh.pages(f"{gh_root}/{release['id']}/assets")
    for asset in assets:
        name = safe_name(asset["name"])
        if asset.get("state") != "uploaded":
            raise SyncError("GitHub attachment is not fully uploaded; rerun later")
        with tempfile.TemporaryDirectory(prefix="gitee-sync-") as tmp:
            source, check = Path(tmp) / "source", Path(tmp) / "check"
            sha = download(asset["browser_download_url"], source, asset["size"])
            upstream_digest = asset.get("digest")
            if upstream_digest and upstream_digest.startswith("sha256:") and upstream_digest != "sha256:" + sha:
                raise SyncError("GitHub attachment SHA-256 mismatch")
            matches = [item for item in attached if item["name"] == name]
            for item in matches:
                if (int(item["size"]) != source.stat().st_size
                        or download(item["browser_download_url"], check, item["size"]) != sha):
                    raise SyncError(f"Immutable release attachment differs: {name}; publish a new version")
            if matches:
                print(f"Unchanged attachment: {name}")
                continue
            # Published bytes are immutable: only add missing mirror attachments.
            uploaded = gt.upload(attachments_path, source, name)
            remote_sha = download(uploaded["browser_download_url"], check, source.stat().st_size)
            if remote_sha != sha:
                gt.request("DELETE", f"{attachments_path}/{uploaded['id']}")
                raise SyncError("Gitee attachment SHA-256 mismatch")
            print(f"Verified uploaded attachment: {name}")
    print(f"Synced release: {tag} ({len(assets)} attachments)")


def main():
    required = ("GITHUB_TOKEN", "GITEE_TOKEN", "GITHUB_REPOSITORY", "GITEE_REPO")
    if any(not os.environ.get(key) for key in required):
        raise SyncError("Missing required repository/token environment variables")
    gh = API("https://api.github.com", os.environ["GITHUB_TOKEN"])
    gt = API("https://gitee.com/api/v5", os.environ["GITEE_TOKEN"], gitee=True)
    gh_root = f"/repos/{os.environ['GITHUB_REPOSITORY']}/releases"
    gt_root = f"/repos/{os.environ['GITEE_REPO']}/releases"
    event = {}
    if os.environ.get("GITHUB_EVENT_PATH"):
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    if os.environ.get("GITHUB_EVENT_NAME") == "release":
        release = event["release"]
        if event.get("action") == "deleted":
            print("Preserving published Gitee release history (GitHub deletion ignored)")
            return
        # Ignore an event superseded by a deletion, and fetch fresh metadata.
        release = gh.request("GET", f"{gh_root}/{release['id']}", missing_ok=True)
        releases = [release] if release else []
    elif os.environ.get("SYNC_TAG", "").strip():
        tag = urllib.parse.quote(os.environ["SYNC_TAG"].strip(), safe="")
        releases = [gh.request("GET", gh_root + "/tags/" + tag)]
    else:
        # Oldest first so the newest version is created last on Gitee.
        releases = sorted(gh.pages(gh_root), key=lambda r: r.get("published_at") or "")
    for release in releases:
        sync_release(gh, gt, gh_root, gt_root, release)


if __name__ == "__main__":
    try:
        main()
    except (SyncError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, SyncError) else "Unexpected API/event response"
        print("Release sync failed: " + message, file=sys.stderr)
        sys.exit(1)

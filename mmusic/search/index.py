"""Resource ledger: dedupe memory / negative cache / source health.

Standard-library ``sqlite3`` only (WAL mode), stored next to the hash cache
as ``resources.db``.  Every method degrades to a **no-op** if the database
cannot be opened, so a locked or read-only folder can never break a job.

What it is used for:

* **negative cache** - remember that a source had no result (or errored) for
  a query, so we neither hammer a broken source nor repeat a useless request;
* **resource memory** - which (source, url) previously served a given song,
  and how often it failed;
* **source health** - attempts / successes / latency per source, surfaced by
  ``--sources-report`` and (later) the GUI health panel.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

__all__ = ["ResourceIndex"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resource (
    key        TEXT NOT NULL,
    source     TEXT NOT NULL,
    url        TEXT NOT NULL,
    ext        TEXT DEFAULT '',
    bitrate    INTEGER DEFAULT 0,
    size       INTEGER DEFAULT 0,
    ok         INTEGER DEFAULT 0,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    fail_count INTEGER DEFAULT 0,
    PRIMARY KEY (source, url)
);
CREATE INDEX IF NOT EXISTS idx_resource_key ON resource(key);

CREATE TABLE IF NOT EXISTS query_state (
    q      TEXT NOT NULL,
    source TEXT NOT NULL,
    state  INTEGER NOT NULL,     -- 1 有结果 / 0 无结果 / -1 出错
    at     INTEGER NOT NULL,
    PRIMARY KEY (q, source)
);

CREATE TABLE IF NOT EXISTS source_health (
    source    TEXT PRIMARY KEY,
    attempts  INTEGER DEFAULT 0,
    successes INTEGER DEFAULT 0,
    failures  INTEGER DEFAULT 0,
    total_ms  INTEGER DEFAULT 0,
    last_ok   INTEGER DEFAULT 0,
    last_fail INTEGER DEFAULT 0
);
"""

NO_RESULT_TTL = 6 * 3600      # 无结果：6 小时内不再重复问同一个源
ERROR_TTL = 15 * 60           # 出错/超时：15 分钟内不再重复问
STALE_URL_TTL = 24 * 3600     # 失效直链的统计窗口

_STATE_FOUND, _STATE_EMPTY, _STATE_ERROR = 1, 0, -1

#: paths whose schema/WAL setup already ran in this process (DDL is the
#: expensive part of a connect, so it is done exactly once per database)
_SCHEMA_READY: "set[str]" = set()


class ResourceIndex:
    """Connection-per-operation on purpose.

    Holding a persistent sqlite handle would keep ``resources.db`` open for
    the whole process lifetime - that blocks the user from deleting/moving a
    portable folder and breaks temp-dir cleanup.  A connect to a small local
    database costs well under a millisecond, which is nothing next to the
    network I/O this ledger accompanies, so we open, use and close each time.
    """

    def __init__(self, path: "str | Path", *, enabled: bool = True) -> None:
        self.path = str(path)
        self.enabled = bool(enabled)
        self._available: "bool | None" = None

    # ------------------------------------------------------------------ #
    def _connect(self) -> "sqlite3.Connection | None":
        if not self.enabled:
            return None
        try:
            fresh = self.path not in _SCHEMA_READY
            if fresh:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=5.0)
            if fresh:
                db.execute("PRAGMA journal_mode=WAL")
                # 这是一份可重建的缓存：崩溃最多丢几条统计，
                # 为它做 fsync 不值得，因此用 OFF 换取明显更快的写入。
                db.execute("PRAGMA synchronous=OFF")
                db.executescript(_SCHEMA)
                db.commit()
                _SCHEMA_READY.add(self.path)
            return db
        except Exception:  # noqa: BLE001 - degrade silently
            return None

    @property
    def available(self) -> bool:
        if not self.enabled:
            return False
        if self._available is None:
            db = self._connect()
            self._available = db is not None
            if db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass
        return bool(self._available)

    def close(self) -> None:
        """Nothing is held open; kept so callers can always call it."""
        return None

    # ------------------------------------------------------------------ #
    def _write(self, sql: str, args: tuple = ()) -> None:
        db = self._connect()
        if db is None:
            return
        try:
            db.execute(sql, args)
            db.commit()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    def _rows(self, sql: str, args: tuple = ()) -> list:
        db = self._connect()
        if db is None:
            return []
        try:
            return db.execute(sql, args).fetchall()
        except Exception:  # noqa: BLE001
            return []
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- negative query cache ---------------------------------------- #
    def note_query(self, query: str, source: str, *, found: bool, error: bool = False) -> None:
        state = _STATE_ERROR if error else (_STATE_FOUND if found else _STATE_EMPTY)
        self._write(
            "INSERT INTO query_state(q, source, state, at) VALUES(?,?,?,?) "
            "ON CONFLICT(q, source) DO UPDATE SET state=excluded.state, at=excluded.at",
            (query, source, state, int(time.time())),
        )

    def note_search(self, source: str, query: str, *, found: bool, error: bool = False,
                    seconds: float = 0.0) -> None:
        """Record a whole search outcome (health + query state) in ONE commit."""
        state = _STATE_ERROR if error else (_STATE_FOUND if found else _STATE_EMPTY)
        now = int(time.time())
        db = self._connect()
        if db is None:
            return
        try:
            db.execute(
                "INSERT INTO source_health(source, attempts, successes, failures, total_ms,"
                " last_ok, last_fail) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET attempts=source_health.attempts + 1,"
                " successes=source_health.successes + excluded.successes,"
                " failures=source_health.failures + excluded.failures,"
                " total_ms=source_health.total_ms + excluded.total_ms,"
                " last_ok=MAX(source_health.last_ok, excluded.last_ok),"
                " last_fail=MAX(source_health.last_fail, excluded.last_fail)",
                (source, 1, 1 if found else 0, 0 if found else 1,
                 int(max(0.0, seconds) * 1000), now if found else 0, 0 if found else now),
            )
            db.execute(
                "INSERT INTO query_state(q, source, state, at) VALUES(?,?,?,?) "
                "ON CONFLICT(q, source) DO UPDATE SET state=excluded.state, at=excluded.at",
                (query, source, state, now),
            )
            db.commit()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    def query_block_reason(self, query: str, source: str, *, now: "int | None" = None) -> str:
        """'' when the query should be attempted, else a short reason."""
        rows = self._rows("SELECT state, at FROM query_state WHERE q=? AND source=?",
                          (query, source))
        if not rows:
            return ""
        state, at = int(rows[0][0]), int(rows[0][1])
        age = (now if now is not None else int(time.time())) - at
        if state == _STATE_FOUND:
            return ""
        if state == _STATE_EMPTY:
            return "无结果" if age < NO_RESULT_TTL else ""
        return "上次出错" if age < ERROR_TTL else ""

    def query_blocked(self, query: str, source: str, *, now: "int | None" = None) -> bool:
        return bool(self.query_block_reason(query, source, now=now))

    # ---- resource memory --------------------------------------------- #
    def note_resource(self, key: str, source: str, url: str, *, ext: str = "",
                      bitrate: int = 0, size: int = 0, ok: bool = True) -> None:
        if not url:
            return
        now = int(time.time())
        self._write(
            "INSERT INTO resource(key, source, url, ext, bitrate, size, ok, first_seen,"
            " last_seen, fail_count) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source, url) DO UPDATE SET key=excluded.key, ext=excluded.ext,"
            " bitrate=excluded.bitrate, size=excluded.size, ok=excluded.ok,"
            " last_seen=excluded.last_seen,"
            " fail_count=CASE WHEN excluded.ok=1 THEN 0 ELSE resource.fail_count + 1 END",
            (key, source, url, ext, int(bitrate or 0), int(size or 0),
             1 if ok else 0, now, now, 0 if ok else 1),
        )

    def is_stale_url(self, source: str, url: str, *, now: "int | None" = None) -> bool:
        """True when this exact URL already failed recently (informational)."""
        if not url:
            return False
        rows = self._rows("SELECT ok, fail_count, last_seen FROM resource "
                          "WHERE source=? AND url=?", (source, url))
        if not rows:
            return False
        ok, fails, last = int(rows[0][0]), int(rows[0][1]), int(rows[0][2])
        age = (now if now is not None else int(time.time())) - last
        return ok == 0 and fails > 0 and age < STALE_URL_TTL

    def key_stats(self, key: str) -> "dict[str, int]":
        rows = self._rows("SELECT COUNT(*), SUM(ok), COUNT(DISTINCT source) "
                          "FROM resource WHERE key=?", (key,))
        if not rows:
            return {"total": 0, "verified": 0, "sources": 0}
        total, verified, sources = rows[0]
        return {"total": int(total or 0), "verified": int(verified or 0),
                "sources": int(sources or 0)}

    # ---- source health ----------------------------------------------- #
    def note_attempt(self, source: str, *, ok: bool, seconds: float = 0.0) -> None:
        now = int(time.time())
        self._write(
            "INSERT INTO source_health(source, attempts, successes, failures, total_ms,"
            " last_ok, last_fail) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(source) DO UPDATE SET attempts=source_health.attempts + 1,"
            " successes=source_health.successes + excluded.successes,"
            " failures=source_health.failures + excluded.failures,"
            " total_ms=source_health.total_ms + excluded.total_ms,"
            " last_ok=MAX(source_health.last_ok, excluded.last_ok),"
            " last_fail=MAX(source_health.last_fail, excluded.last_fail)",
            (source, 1, 1 if ok else 0, 0 if ok else 1,
             int(max(0.0, seconds) * 1000), now if ok else 0, 0 if ok else now),
        )

    def health(self) -> list[dict]:
        rows = self._rows("SELECT source, attempts, successes, failures, total_ms,"
                          " last_ok, last_fail FROM source_health ORDER BY attempts DESC")
        out = []
        for source, attempts, successes, failures, total_ms, last_ok, last_fail in rows:
            attempts = int(attempts or 0)
            successes = int(successes or 0)
            out.append({
                "source": source,
                "attempts": attempts,
                "successes": successes,
                "failures": int(failures or 0),
                "success_rate": round(successes / attempts, 3) if attempts else 0.0,
                "avg_ms": int(total_ms / attempts) if attempts else 0,
                "last_ok": int(last_ok or 0),
                "last_fail": int(last_fail or 0),
            })
        return out

    def stats(self) -> "dict[str, int]":
        def _one(sql: str) -> int:
            rows = self._rows(sql)
            return int(rows[0][0] or 0) if rows else 0

        return {
            "resources": _one("SELECT COUNT(*) FROM resource"),
            "resources_ok": _one("SELECT COUNT(*) FROM resource WHERE ok=1"),
            "queries": _one("SELECT COUNT(*) FROM query_state"),
            "queries_empty": _one("SELECT COUNT(*) FROM query_state WHERE state=0"),
            "sources": _one("SELECT COUNT(*) FROM source_health"),
        }
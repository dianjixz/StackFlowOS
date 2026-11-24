"""SQLite storage backend (single file, WAL, explicit migrations).

Schema (``PRAGMA user_version`` tracks the migration level):

``kv(namespace, key, value, updated_at)``
    Whole-value JSON objects; the primary key is ``(namespace, key)``.
``rows(namespace, row_id, data, created_at)``
    Insertion-ordered append rows (event log).  ``row_id`` is an
    ``INTEGER PRIMARY KEY AUTOINCREMENT`` so ids are never reused even after
    deletes, and ``ORDER BY row_id`` is the stable replay order.

Design notes
------------
* one connection per backend instance, ``check_same_thread=False`` plus an
  ``RLock`` around every statement: the event log is appended from worker
  threads while the asyncio loop reads it;
* WAL + ``synchronous=NORMAL`` keeps appends fast while remaining crash safe
  (a lost tail of committed transactions cannot corrupt the database; full
  durability is available by setting ``synchronous=FULL``);
* writes are grouped by :meth:`transaction`: ``BEGIN IMMEDIATE`` for a
  top-level transaction and a savepoint for nested ones, so callers can compose
  multi-namespace writes atomically;
* migrations are a plain ordered tuple of SQL scripts.  A file from the future
  (``user_version`` above the newest script) is rejected instead of being
  silently opened read/write.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..util.jsonio import _default as _json_default
from .backend import StorageError, validate_namespace

#: Bump this when adding a migration below.
SCHEMA_VERSION = 1

#: Ordered migrations; ``index + 1`` is the resulting ``user_version``.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS kv (
        namespace  TEXT NOT NULL,
        key        TEXT NOT NULL,
        value      TEXT NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (namespace, key)
    );
    CREATE TABLE IF NOT EXISTS rows (
        namespace  TEXT NOT NULL,
        row_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        data       TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_rows_namespace
        ON rows (namespace, row_id);
    """,
)


class SqliteBackend:
    """Full backend on one SQLite database file."""

    name = "sqlite"

    def __init__(self, path: Path | str, *, busy_timeout_ms: int = 5000,
                 synchronous: str = "NORMAL") -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._tx_depth = 0
        self._savepoint = 0
        try:
            self._conn = sqlite3.connect(
                str(self.path) if str(self.path) != ":memory:" else ":memory:",
                timeout=max(0.1, int(busy_timeout_ms) / 1000.0),
                isolation_level=None,   # explicit BEGIN/COMMIT below
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise StorageError(f"cannot open sqlite database {self.path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        try:
            self._configure(synchronous)
            self._migrate()
        except Exception:
            self._conn.close()
            self._closed = True
            raise

    # -- setup ------------------------------------------------------------
    def _configure(self, synchronous: str) -> None:
        sync = str(synchronous or "NORMAL").upper()
        if sync not in ("OFF", "NORMAL", "FULL", "EXTRA"):
            sync = "NORMAL"
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute(f"PRAGMA synchronous={sync}")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")

    def _migrate(self) -> None:
        with self._lock:
            current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if current > len(MIGRATIONS):
                raise StorageError(
                    f"database {self.path} has schema version {current}, newer "
                    f"than this build supports ({len(MIGRATIONS)})")
            for index in range(current, len(MIGRATIONS)):
                script = MIGRATIONS[index]
                try:
                    # ``executescript`` implicitly commits any pending
                    # transaction; every migration is written with
                    # ``IF NOT EXISTS`` so a crash mid-script simply re-runs on
                    # the next open, and ``user_version`` is stamped after the
                    # script completed.
                    self._conn.executescript(script)
                    self._conn.execute(f"PRAGMA user_version={index + 1}")
                except sqlite3.Error as exc:
                    raise StorageError(
                        f"sqlite migration to version {index + 1} failed: {exc}"
                    ) from exc

    def _check_open(self) -> None:
        if self._closed:
            raise StorageError("sqlite storage backend is closed")

    # -- KV ---------------------------------------------------------------
    def get(self, namespace: str, key: str, default: Any = None) -> Any:
        namespace = validate_namespace(namespace)
        with self._lock:
            self._check_open()
            try:
                row = self._conn.execute(
                    "SELECT value FROM kv WHERE namespace=? AND key=?",
                    (namespace, str(key))).fetchone()
            except sqlite3.Error as exc:
                raise StorageError(f"sqlite get failed: {exc}") from exc
            if row is None:
                return default
            return _decode(str(row["value"]), namespace, key)

    def set(self, namespace: str, key: str, value: Any) -> None:
        namespace = validate_namespace(namespace)
        encoded = _encode(value)
        with self._tx():
            try:
                self._conn.execute(
                    "INSERT INTO kv (namespace, key, value, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(namespace, key) DO UPDATE SET "
                    "value=excluded.value, updated_at=excluded.updated_at",
                    (namespace, str(key), encoded, int(time.time() * 1000)))
            except sqlite3.Error as exc:
                raise StorageError(f"sqlite set failed: {exc}") from exc

    def delete(self, namespace: str, key: str) -> bool:
        namespace = validate_namespace(namespace)
        with self._tx():
            try:
                cur = self._conn.execute(
                    "DELETE FROM kv WHERE namespace=? AND key=?",
                    (namespace, str(key)))
                return cur.rowcount > 0
            except sqlite3.Error as exc:
                raise StorageError(f"sqlite delete failed: {exc}") from exc

    def keys(self, namespace: str) -> list[str]:
        namespace = validate_namespace(namespace)
        with self._lock:
            self._check_open()
            try:
                rows = self._conn.execute(
                    "SELECT key FROM kv WHERE namespace=? ORDER BY key",
                    (namespace,)).fetchall()
            except sqlite3.Error as exc:
                raise StorageError(f"sqlite keys failed: {exc}") from exc
            return [str(row["key"]) for row in rows]

    def items(self, namespace: str) -> list[tuple[str, Any]]:
        namespace = validate_namespace(namespace)
        with self._lock:
            self._check_open()
            try:
                rows = self._conn.execute(
                    "SELECT key, value FROM kv WHERE namespace=? ORDER BY key",
                    (namespace,)).fetchall()
            except sqlite3.Error as exc:
                raise StorageError(f"sqlite items failed: {exc}") from exc
            return [(str(row["key"]), _decode(str(row["value"]), namespace,
                                              str(row["key"])))
                    for row in rows]

    def has_prefix(self, namespace: str, prefix: str) -> bool:
        namespace = validate_namespace(namespace)
        prefix = str(prefix)
        with self._lock:
            self._check_open()
            try:
                row = self._conn.execute(
                    "SELECT 1 FROM kv WHERE namespace=? AND key LIKE ? ESCAPE '\\' "
                    "LIMIT 1",
                    (namespace, _escape_like(prefix) + "%")).fetchone()
            except sqlite3.Error as exc:
                raise StorageError(f"sqlite has_prefix failed: {exc}") from exc
            return row is not None

    # -- append -----------------------------------------------------------
    def append(self, namespace: str, row: dict) -> int:
        namespace = validate_namespace(namespace)
        if not isinstance(row, dict):
            raise StorageError("append rows must be JSON objects")
        encoded = _encode(row)
        with self._tx():
            try:
                cur = self._conn.execute(
                    "INSERT INTO rows (namespace, data, created_at) VALUES (?, ?, ?)",
                    (namespace, encoded, int(time.time() * 1000)))
                return int(cur.lastrowid or 0)
            except sqlite3.Error as exc:
                raise StorageError(f"sqlite append failed: {exc}") from exc

    def iter_rows(self, namespace: str, *, reverse: bool = False,
                  limit: int | None = None) -> Iterator[dict]:
        namespace = validate_namespace(namespace)
        order = "DESC" if reverse else "ASC"
        sql = f"SELECT data FROM rows WHERE namespace=? ORDER BY row_id {order}"
        params: list[Any] = [namespace]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        with self._lock:
            self._check_open()
            try:
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                raise StorageError(f"sqlite iter_rows failed: {exc}") from exc
        for row in rows:
            yield _decode(str(row["data"]), namespace, "<row>")

    def last_row(self, namespace: str) -> dict | None:
        namespace = validate_namespace(namespace)
        with self._lock:
            self._check_open()
            try:
                row = self._conn.execute(
                    "SELECT data FROM rows WHERE namespace=? "
                    "ORDER BY row_id DESC LIMIT 1", (namespace,)).fetchone()
            except sqlite3.Error as exc:
                raise StorageError(f"sqlite last_row failed: {exc}") from exc
            if row is None:
                return None
            return _decode(str(row["data"]), namespace, "<last>")

    def count(self, namespace: str) -> int:
        namespace = validate_namespace(namespace)
        with self._lock:
            self._check_open()
            try:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM rows WHERE namespace=?",
                    (namespace,)).fetchone()
            except sqlite3.Error as exc:
                raise StorageError(f"sqlite count failed: {exc}") from exc
            return int(row["n"]) if row is not None else 0

    # -- transactions -----------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator["SqliteBackend"]:
        with self._tx():
            yield self

    @contextmanager
    def _tx(self) -> Iterator[None]:
        with self._lock:
            self._check_open()
            outer = self._tx_depth == 0
            savepoint = None
            try:
                if outer:
                    self._conn.execute("BEGIN IMMEDIATE")
                else:
                    savepoint = f"sfo_sp_{self._savepoint}"
                    self._savepoint += 1
                    self._conn.execute(f"SAVEPOINT {savepoint}")
                self._tx_depth += 1
                yield
            except BaseException:
                if outer:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                elif savepoint is not None:
                    try:
                        self._conn.execute(f"ROLLBACK TO {savepoint}")
                        self._conn.execute(f"RELEASE {savepoint}")
                    except sqlite3.Error:
                        pass
                raise
            else:
                if outer:
                    self._conn.execute("COMMIT")
                elif savepoint is not None:
                    self._conn.execute(f"RELEASE {savepoint}")
            finally:
                self._tx_depth -= 1

    # -- lifecycle --------------------------------------------------------
    def flush(self) -> None:
        with self._lock:
            self._check_open()
            try:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def health(self) -> dict:
        with self._lock:
            if self._closed:
                return {"backend": self.name, "path": str(self.path),
                        "writable": False, "closed": True}
            try:
                journal = str(self._conn.execute(
                    "PRAGMA journal_mode").fetchone()[0]).lower()
                version = int(self._conn.execute(
                    "PRAGMA user_version").fetchone()[0])
                events = int(self._conn.execute(
                    "SELECT COUNT(*) FROM rows WHERE namespace='events'"
                ).fetchone()[0])
            except sqlite3.Error as exc:
                return {"backend": self.name, "path": str(self.path),
                        "writable": False, "error": str(exc)}
            try:
                size = self.path.stat().st_size
            except OSError:
                size = 0
            return {
                "backend": self.name,
                "path": str(self.path),
                "writable": os.access(self.path.parent, os.W_OK),
                "journal_mode": journal,
                "schema_version": version,
                "events": events,
                "bytes": size,
            }


def _encode(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=_json_default,
                          separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise StorageError(f"value is not JSON serialisable: {exc}") from exc


def _decode(raw: str, namespace: str, key: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise StorageError(
            f"corrupt value in {namespace}/{key}: {exc}") from exc


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "SqliteBackend"]

"""JSON/JSONL storage backend (the historical on-disk layout).

KV namespaces live in ``<root>/<namespace>.json`` (whole file replaced
atomically via :func:`~stackflowos.util.jsonio.write_json`), append namespaces
in ``<root>/<namespace>.jsonl`` (one JSON object per line, flushed and
fsynced per append).

This backend is intentionally simple: it is what StackFlowOS has always used,
kept as the default so existing state directories keep working.  It is not a
database; use :mod:`stackflowos.storage.sqlite_backend` when you need
transactions across namespaces.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..util.jsonio import _default as _json_default
from ..util.jsonio import ensure_dir, iter_jsonl, read_json, write_json
from .backend import StorageError, validate_namespace


class JsonFileBackend:
    """Directory-backed KV + JSONL append store (thread safe)."""

    name = "json"

    def __init__(self, root: Path, *, fsync_append: bool = True) -> None:
        self.root = ensure_dir(Path(root))
        self.fsync_append = bool(fsync_append)
        self._lock = threading.RLock()
        self._kv_cache: dict[str, dict[str, Any]] = {}
        self._closed = False

    # -- paths ------------------------------------------------------------
    def _kv_path(self, namespace: str) -> Path:
        return self.root / f"{validate_namespace(namespace)}.json"

    def _append_path(self, namespace: str) -> Path:
        return self.root / f"{validate_namespace(namespace)}.jsonl"

    def _check_open(self) -> None:
        if self._closed:
            raise StorageError("json storage backend is closed")

    # -- KV ---------------------------------------------------------------
    def _load_kv(self, namespace: str) -> dict[str, Any]:
        if namespace in self._kv_cache:
            return self._kv_cache[namespace]
        path = self._kv_path(namespace)
        try:
            raw = read_json(path, default={})
        except ValueError as exc:
            # A corrupt KV file must not take the hub down; quarantine it so
            # the operator can inspect it, then start the namespace empty.
            bad = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
            try:
                os.replace(path, bad)
            except OSError:
                pass
            raise StorageError(
                f"corrupt KV file {path} (moved to {bad}): {exc}") from exc
        if not isinstance(raw, dict):
            raise StorageError(f"invalid KV file {path}: not a JSON object")
        self._kv_cache[namespace] = {str(key): value for key, value in raw.items()}
        return self._kv_cache[namespace]

    def get(self, namespace: str, key: str, default: Any = None) -> Any:
        with self._lock:
            self._check_open()
            return self._load_kv(validate_namespace(namespace)).get(str(key), default)

    def set(self, namespace: str, key: str, value: Any) -> None:
        namespace = validate_namespace(namespace)
        with self._lock:
            self._check_open()
            data = dict(self._load_kv(namespace))
            data[str(key)] = value
            write_json(self._kv_path(namespace), data)
            self._kv_cache[namespace] = data

    def delete(self, namespace: str, key: str) -> bool:
        namespace = validate_namespace(namespace)
        with self._lock:
            self._check_open()
            data = dict(self._load_kv(namespace))
            key = str(key)
            if key not in data:
                return False
            del data[key]
            write_json(self._kv_path(namespace), data)
            self._kv_cache[namespace] = data
            return True

    def keys(self, namespace: str) -> list[str]:
        with self._lock:
            self._check_open()
            return sorted(self._load_kv(validate_namespace(namespace)))

    def items(self, namespace: str) -> list[tuple[str, Any]]:
        with self._lock:
            self._check_open()
            data = self._load_kv(validate_namespace(namespace))
            return [(key, data[key]) for key in sorted(data)]

    def has_prefix(self, namespace: str, prefix: str) -> bool:
        prefix = str(prefix)
        with self._lock:
            self._check_open()
            return any(key.startswith(prefix)
                       for key in self._load_kv(validate_namespace(namespace)))

    # -- append -----------------------------------------------------------
    def append(self, namespace: str, row: dict) -> int:
        namespace = validate_namespace(namespace)
        if not isinstance(row, dict):
            raise StorageError("append rows must be JSON objects")
        with self._lock:
            self._check_open()
            path = self._append_path(namespace)
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(row, ensure_ascii=False, default=_json_default)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                if self.fsync_append:
                    os.fsync(fh.fileno())
            return self.count(namespace)

    def iter_rows(self, namespace: str, *, reverse: bool = False,
                  limit: int | None = None) -> Iterator[dict]:
        namespace = validate_namespace(namespace)
        with self._lock:
            self._check_open()
            rows = list(iter_jsonl(self._append_path(namespace)))
        if reverse:
            rows.reverse()
        if limit is not None:
            rows = rows[: max(0, int(limit))]
        yield from rows

    def last_row(self, namespace: str) -> dict | None:
        rows = list(self.iter_rows(namespace, reverse=True, limit=1))
        return rows[0] if rows else None

    def count(self, namespace: str) -> int:
        namespace = validate_namespace(namespace)
        with self._lock:
            self._check_open()
            total = 0
            try:
                with open(self._append_path(namespace), "r", encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            total += 1
            except FileNotFoundError:
                return 0
            return total

    # -- lifecycle --------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator["JsonFileBackend"]:
        # Write-through files cannot be rolled back; serialise callers so a
        # multi-write block is at least not interleaved with another thread.
        with self._lock:
            self._check_open()
            yield self

    def flush(self) -> None:
        # Every append/set is already flushed; nothing to do.
        with self._lock:
            self._check_open()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._kv_cache.clear()

    def health(self) -> dict:
        with self._lock:
            writable = os.access(self.root, os.W_OK) and not self._closed
            return {
                "backend": self.name,
                "root": str(self.root),
                "writable": bool(writable),
                "namespaces": sorted(
                    {path.stem for path in self.root.glob("*.json")}
                    | {path.stem for path in self.root.glob("*.jsonl")}),
            }


__all__ = ["JsonFileBackend"]

"""Small filesystem / time / id helpers used across StackFlowOS."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Iterator


def now_s() -> float:
    return time.time()


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ts_ms: int | None = None) -> str:
    ms = now_ms() if ts_ms is None else ts_ms
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ms / 1000))


def human_age(ts_ms: int | None) -> str:
    if not ts_ms:
        return "-"
    delta = max(0.0, time.time() - ts_ms / 1000)
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    return f"{int(delta // 86400)}d"


# ---------------------------------------------------------------------------
# ids
# ---------------------------------------------------------------------------

def new_id(prefix: str = "") -> str:
    token = uuid.uuid4().hex[:8]
    return f"{prefix}-{token}" if prefix else token


class Counter:
    """Monotonic counter, optionally persisted as a tiny JSON file."""

    def __init__(self, path: Path | None = None, start: int = 1) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        self._value = start - 1
        if self._path and self._path.exists():
            try:
                data = json.loads(self._path.read_text("utf-8"))
                self._value = int(data.get("value", start - 1))
            except (OSError, ValueError, TypeError):
                self._value = start - 1

    def next(self) -> int:
        with self._lock:
            self._value += 1
            value = self._value
            self._persist()
            return value

    def _persist(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps({"value": self._value}), "utf-8")
        os.replace(tmp, self._path)

    @property
    def value(self) -> int:
        return self._value


# ---------------------------------------------------------------------------
# json files
# ---------------------------------------------------------------------------

def write_json(path: Path, obj: Any) -> None:
    """Atomically replace ``path`` with the JSON representation of ``obj``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2, default=_default)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _default(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    raise TypeError(f"not JSON serialisable: {type(obj)!r}")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read JSON file {path}: {exc}") from exc


def append_jsonl(path: Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, default=_default)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()


def iter_jsonl(path: Path) -> Iterator[dict]:
    path = Path(path)
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def tail_jsonl(path: Path, limit: int = 200) -> list[dict]:
    """Return the last ``limit`` well-formed records of a JSONL file.

    Files are small enough (one event per line, rotated) that reading the
    whole file is fine and keeps the implementation obvious.
    """
    rows = list(iter_jsonl(path))
    return rows[-limit:]


def deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def ensure_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def short(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def chunked(items: Iterable[Any], size: int) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch

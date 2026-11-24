"""Storage backends for StackFlowOS (JSON default, optional SQLite).

Public API::

    from stackflowos.storage import JsonFileBackend, SqliteBackend
    from stackflowos.storage import migrate_json_to_sqlite, open_backend

The composition root (``app.py``) constructs the event log and memory store
without a backend today; both accept an optional ``backend=`` argument, and
they also honour ``SFO_STORAGE_BACKEND=sqlite`` / ``SFO_STORAGE_SQLITE_PATH``
so a deployment can switch without code changes.  ``migrate_json_to_sqlite``
copies an existing JSON state directory into the database.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .backend import StorageBackend, StorageError, validate_namespace
from .json_backend import JsonFileBackend
from .sqlite_backend import SCHEMA_VERSION, SqliteBackend

ENV_BACKEND = "SFO_STORAGE_BACKEND"
ENV_SQLITE_PATH = "SFO_STORAGE_SQLITE_PATH"


def default_sqlite_path(state_dir: Path) -> Path:
    """Default database location for a state directory.

    ``<state>/state/stackflowos.db`` mirrors the other domain stores, which
    live under ``<state>/state`` as well.
    """
    return Path(state_dir) / "state" / "stackflowos.db"


def open_backend(kind: str, *, state_dir: Path, sqlite_path: str | Path | None = None,
                 json_root: Path | None = None) -> StorageBackend:
    """Build a backend by name (``json`` or ``sqlite``)."""
    kind = str(kind or "json").strip().lower()
    if kind == "json":
        root = Path(json_root) if json_root is not None else Path(state_dir) / "storage"
        return JsonFileBackend(root)
    if kind == "sqlite":
        target = Path(sqlite_path) if sqlite_path else default_sqlite_path(state_dir)
        return SqliteBackend(target)
    raise StorageError(f"unknown storage backend: {kind!r} (expected json|sqlite)")


def backend_from_env(state_dir: Path) -> Optional[StorageBackend]:
    """Return an SQLite backend when the environment selects it, else ``None``.

    ``SFO_STORAGE_BACKEND=sqlite`` is the switch; ``SFO_STORAGE_SQLITE_PATH``
    overrides the default ``<state>/state/stackflowos.db``.  Returning ``None``
    for ``json`` keeps the historical JSON/JSONL code path exactly as it was.
    """
    kind = os.environ.get(ENV_BACKEND, "").strip().lower()
    if kind != "sqlite":
        return None
    path = os.environ.get(ENV_SQLITE_PATH, "").strip()
    return SqliteBackend(Path(path) if path else default_sqlite_path(state_dir))


from .migrate import migrate_json_to_sqlite  # noqa: E402  (import after backends)

__all__ = [
    "ENV_BACKEND", "ENV_SQLITE_PATH", "JsonFileBackend", "SCHEMA_VERSION",
    "SqliteBackend", "StorageBackend", "StorageError", "backend_from_env",
    "default_sqlite_path", "migrate_json_to_sqlite", "open_backend",
    "validate_namespace",
]

"""Storage backend contract for StackFlowOS persistence (L2, stdlib only).

The hub owns two very different persistence shapes and both live behind this
small interface:

* **KV** -- small, whole-value objects addressed by ``(namespace, key)``:
  per-project memory (``memory/p-1``), counters (``state/counters``) and any
  future settings blob.
* **append** -- insertion-ordered rows addressed only by ``namespace``: the
  event log (``events``) and, later, audit trails.

Two implementations ship with the hub:

* :class:`~stackflowos.storage.json_backend.JsonFileBackend` -- the historical
  on-disk layout (one JSON file per KV namespace, one JSONL file per append
  namespace).  This is the default and keeps the current files readable.
* :class:`~stackflowos.storage.sqlite_backend.SqliteBackend` -- one SQLite
  database with WAL, explicit transactions and ``user_version`` migrations.

The interface deliberately stays synchronous: callers are either the asyncio
loop (fast local calls, no network) or worker threads guarded by the callers'
own locks.  Implementations are thread safe (they take an internal
``RLock``); that is required because the event log accepts appends from
several threads.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Iterator, Protocol, runtime_checkable

#: Namespaces are used in file names by the JSON backend, so keep them to a
#: conservative character set.  Callers use dotted names such as ``memory`` or
#: ``state.counters``.
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class StorageError(RuntimeError):
    """Base class for storage failures (bad path, corrupt value, closed db)."""


def validate_namespace(namespace: str) -> str:
    """Return ``namespace`` when safe, else raise :class:`StorageError`."""
    value = str(namespace or "")
    if not value or not _NAMESPACE_RE.match(value) or value in (".", ".."):
        raise StorageError(f"invalid storage namespace: {namespace!r}")
    return value


@runtime_checkable
class StorageBackend(Protocol):
    """Minimal KV + append store used by the event log and memory store."""

    #: Short identifier (``json`` / ``sqlite``) used in health payloads.
    name: str

    # -- KV ---------------------------------------------------------------
    def get(self, namespace: str, key: str, default: Any = None) -> Any:
        """Return the stored value for ``key``, or ``default`` when absent."""

    def set(self, namespace: str, key: str, value: Any) -> None:
        """Persist ``value`` under ``(namespace, key)`` (atomic replace)."""

    def delete(self, namespace: str, key: str) -> bool:
        """Remove ``key``; return whether it existed."""

    def keys(self, namespace: str) -> list[str]:
        """Return every key in ``namespace`` (sorted)."""

    def items(self, namespace: str) -> list[tuple[str, Any]]:
        """Return every ``(key, value)`` pair in ``namespace`` (sorted)."""

    def has_prefix(self, namespace: str, prefix: str) -> bool:
        """Whether any key in ``namespace`` starts with ``prefix``."""

    # -- append -----------------------------------------------------------
    def append(self, namespace: str, row: dict) -> int:
        """Append one JSON row; return its backend-local row id."""

    def iter_rows(self, namespace: str, *, reverse: bool = False,
                  limit: int | None = None) -> Iterator[dict]:
        """Yield rows in insertion order (or reverse), optionally bounded."""

    def last_row(self, namespace: str) -> dict | None:
        """Return the last appended row, or ``None`` when empty."""

    def count(self, namespace: str) -> int:
        """Return the number of rows in ``namespace``."""

    # -- lifecycle --------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator["StorageBackend"]:
        """Group writes atomically; nested calls participate in the outer one."""

    def flush(self) -> None:
        """Force durability of pending append writes (no-op when synchronous)."""

    def close(self) -> None:
        """Release resources; subsequent calls raise :class:`StorageError`."""

    def health(self) -> dict:
        """Small JSON-friendly status payload for ``/api/health``."""


@contextmanager
def null_transaction(backend: StorageBackend) -> Iterator[StorageBackend]:
    """Fallback transaction for backends without a transactional writer."""
    yield backend


__all__ = [
    "StorageBackend", "StorageError", "null_transaction", "validate_namespace",
]

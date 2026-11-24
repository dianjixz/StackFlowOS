"""Per-project agent memory: bounded, atomic and corruption tolerant (L6).

The historical layout was one ``state/memory.json`` rewritten in full on every
``set``.  That made each write O(all projects), let one agent grow the file
without bound and turned a single invalid byte into a hub that could not boot
(the file was read eagerly during ``App.start``).

This module keeps one small JSON object per project under
``state/memory/<project_id>.json``:

* ``set`` validates the project id (M9), serialises *that project's* data and
  refuses anything above ``memory_max_bytes`` or that would push the store
  above the total-size / project-count caps -- nothing is written on rejection;
* writes are atomic (``write_json``: temp file + fsync + rename), 0600, and
  serialised by a process-wide lock so concurrent agents cannot interleave;
* a corrupt project file (or the legacy ``memory.json``) is quarantined as
  ``<name>.corrupt-<ts>`` and the store simply starts empty for it instead of
  failing the boot.  The reports are kept in :attr:`MemoryStore.corruptions`
  and drained by :class:`~stackflowos.app.App`, which emits ``memory.corrupt``
  events once the event log is ready.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .security.capabilities import validate_project_id
from .storage import StorageBackend, backend_from_env
from .util.jsonio import _default as _json_default
from .util.jsonio import write_json
from .util.logging import get_logger

#: Default per-project cap (``security.memory_max_bytes``).
DEFAULT_MAX_PROJECT_BYTES = 256 * 1024
#: Default cap across every project file (``security.memory_max_total_bytes``).
DEFAULT_MAX_TOTAL_BYTES = 4 * 1024 * 1024
#: Default number of project files (``security.memory_max_projects``).
DEFAULT_MAX_PROJECTS = 1000


def _encoded_size(project_id: str, data: dict) -> int:
    """UTF-8 size of ``data`` as JSON, or ``ValidationError`` if not encodable.

    The persisted form (indent=2) is only used to write the file; the bound is
    defined on the compact encoding of the project object so a value is
    accepted/rejected identically before and after a restart.
    """
    try:
        raw = json.dumps(data, ensure_ascii=False, default=_json_default)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"memory for project {project_id} is not JSON serialisable",
            detail={"project_id": project_id, "reason": str(exc)},
        ) from exc
    return len(raw.encode("utf-8"))


class MemoryStore:
    """Per-project key/value memory for agents (small, JSON persisted).

    ``path`` is the per-project directory (``state/memory``).  A legacy
    single-file path (``state/memory.json``) is still accepted: the file is
    then treated as the legacy store and migrated on first boot.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_project_bytes: int = DEFAULT_MAX_PROJECT_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        max_projects: int = DEFAULT_MAX_PROJECTS,
        legacy_path: Path | None = None,
        logger=None,
        backend: StorageBackend | None = None,
    ) -> None:
        raw_path = Path(path)
        if raw_path.suffix == ".json":
            # Backwards compatible construction with the old single-file path.
            self.path = raw_path.with_suffix("")
            default_legacy = raw_path
        else:
            self.path = raw_path
            default_legacy = raw_path.parent / (raw_path.name + ".json")
        self.legacy_path = Path(legacy_path) if legacy_path is not None else default_legacy
        self.max_project_bytes = max(1, int(max_project_bytes))
        self.max_total_bytes = max(1, int(max_total_bytes))
        self.max_projects = max(1, int(max_projects))
        self.log = logger or get_logger("memory")
        self._lock = threading.RLock()
        #: Optional storage backend (SQLite).  ``None`` keeps the JSON files;
        #: ``SFO_STORAGE_BACKEND=sqlite`` selects one without code changes.
        self._backend = backend if backend is not None else backend_from_env(
            self.path.parent)
        #: In-memory copy of the store; ``len(data)`` is the project count.
        self.data: dict[str, dict] = {}
        self._sizes: dict[str, int] = {}
        self._total_bytes = 0
        #: Quarantine reports collected while loading (drained by ``App``).
        self.corruptions: list[dict] = []

        if self._backend is None:
            self._ensure_dir()
            self._migrate_legacy()
        self._load_projects()

    # ------------------------------------------------------------------
    # interface kept from the old single-file store
    # ------------------------------------------------------------------
    def get(self, project_id: str, key: str) -> Any:
        pid = validate_project_id(project_id)
        with self._lock:
            return (self.data.get(pid) or {}).get(str(key))

    def set(self, project_id: str, key: str, value: Any) -> Any:
        pid = validate_project_id(project_id)
        key = str(key)
        with self._lock:
            project = dict(self.data.get(pid) or {})
            project[key] = value
            size = _encoded_size(pid, project)
            if size > self.max_project_bytes:
                raise ValidationError(
                    f"memory for project {pid} exceeds "
                    f"{self.max_project_bytes} bytes",
                    detail={"project_id": pid, "limit": self.max_project_bytes,
                            "actual": size},
                )
            old_size = self._sizes.get(pid, 0)
            new_total = self._total_bytes - old_size + size
            if new_total > self.max_total_bytes:
                raise ValidationError(
                    "memory store exceeds total size limit",
                    detail={"limit": self.max_total_bytes,
                            "actual": new_total},
                )
            if pid not in self.data and len(self.data) >= self.max_projects:
                raise ValidationError(
                    "memory store project limit reached",
                    detail={"limit": self.max_projects},
                )
            self._write_project(pid, project)
            self.data[pid] = project
            self._sizes[pid] = size
            self._total_bytes = new_total
            return value

    def all(self, project_id: str) -> dict:
        pid = validate_project_id(project_id)
        with self._lock:
            return dict(self.data.get(pid) or {})

    def health(self) -> dict:
        """Small status payload for ``/api/health`` (never raises)."""
        with self._lock:
            backend = getattr(self._backend, "name", "json")
            payload = {
                "backend": backend,
                "projects": len(self.data),
                "total_bytes": self._total_bytes,
                "max_project_bytes": self.max_project_bytes,
                "max_total_bytes": self.max_total_bytes,
            }
            if self._backend is not None:
                try:
                    payload["storage"] = self._backend.health()
                except Exception:  # noqa: BLE001 - health must never raise
                    payload["storage"] = {"writable": False}
            return payload

    # ------------------------------------------------------------------
    # additions used by App lifecycle
    # ------------------------------------------------------------------
    def delete(self, project_id: str) -> bool:
        """Drop one project's data and file; returns whether it existed."""
        pid = validate_project_id(project_id)
        with self._lock:
            existed = pid in self.data
            self.data.pop(pid, None)
            size = self._sizes.pop(pid, 0)
            self._total_bytes = max(0, self._total_bytes - size)
            if self._backend is not None:
                try:
                    self._backend.delete("memory", pid)
                except Exception as exc:  # noqa: BLE001 - deletion is best effort
                    self.log.warning("could not delete memory row %s: %s",
                                     pid, exc)
                return existed
            path = self._file_for(pid)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                self.log.warning("could not delete memory file %s: %s", path, exc)
            return existed

    def drain_corruptions(self) -> list[dict]:
        """Return and clear the quarantine reports collected while loading."""
        with self._lock:
            reports = self.corruptions
            self.corruptions = []
            return reports

    # ------------------------------------------------------------------
    # filesystem
    # ------------------------------------------------------------------
    def _file_for(self, project_id: str) -> Path:
        return self.path / f"{project_id}.json"

    def _ensure_dir(self) -> None:
        try:
            self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            self.log.warning("could not create memory directory %s: %s",
                             self.path, exc)
            return
        try:
            os.chmod(self.path, 0o700)
        except OSError:
            self.log.debug("could not chmod memory directory %s", self.path,
                           exc_info=True)

    def _write_project(self, project_id: str, project: dict) -> None:
        if self._backend is not None:
            self._backend.set("memory", project_id, project)
            return
        # Atomic replace; the temp file is created 0600 by write_json and we
        # chmod explicitly in case an existing file had looser permissions.
        if not self.path.is_dir():
            self._ensure_dir()
        path = self._file_for(project_id)
        write_json(path, project)
        try:
            os.chmod(path, 0o600)
        except OSError:
            self.log.debug("could not chmod memory file %s", path, exc_info=True)

    def _quarantine(self, path: Path, *, project_id: str | None,
                    reason: str) -> None:
        """Move a bad file aside (``<name>.corrupt-<ts>``) and record it."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        target = path.with_name(f"{path.name}.corrupt-{stamp}")
        attempt = 1
        while target.exists() and attempt < 100:
            target = path.with_name(f"{path.name}.corrupt-{stamp}-{attempt}")
            attempt += 1
        quarantined: str | None = None
        try:
            os.replace(path, target)
            quarantined = str(target)
            self.log.warning("quarantined memory file %s -> %s (%s)",
                             path, target, reason)
        except OSError as exc:
            self.log.warning("could not quarantine memory file %s: %s (%s)",
                             path, exc, reason)
        self.corruptions.append({
            "project_id": project_id,
            "path": str(path),
            "quarantined_to": quarantined,
            "reason": reason,
        })

    # ------------------------------------------------------------------
    # loading / migration
    # ------------------------------------------------------------------
    def _migrate_legacy(self) -> None:
        """Move the old single-file store into per-project files.

        A broken legacy file is quarantined instead of blocking the hub; a
        failed write or rename leaves the legacy file for the next boot.  New
        per-project files always win, so re-running a migration cannot revert
        data that was written after the last attempt.
        """
        legacy = self.legacy_path
        try:
            exists = legacy.exists()
        except OSError:
            exists = False
        if not exists:
            return
        try:
            payload = json.loads(legacy.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            self._quarantine(legacy, project_id=None,
                             reason=f"unreadable legacy memory: {exc}")
            return
        if not isinstance(payload, dict):
            self._quarantine(legacy, project_id=None,
                             reason="legacy memory is not a JSON object")
            return
        migrated = 0
        try:
            for raw_pid, values in payload.items():
                try:
                    pid = validate_project_id(raw_pid)
                except ValidationError:
                    self.log.warning(
                        "legacy memory: skipping unsafe project id %r", raw_pid)
                    continue
                if not isinstance(values, dict):
                    self.log.warning(
                        "legacy memory: skipping project %s (not an object)", pid)
                    continue
                target = self._file_for(pid)
                if target.exists():
                    continue
                project = {str(k): v for k, v in values.items()}
                try:
                    size = _encoded_size(pid, project)
                except ValidationError:
                    self.log.warning(
                        "legacy memory: skipping project %s (not serialisable)",
                        pid)
                    continue
                if size > self.max_project_bytes:
                    self.log.warning(
                        "legacy memory: project %s exceeds the %d byte cap; "
                        "skipped", pid, self.max_project_bytes)
                    continue
                self._write_project(pid, project)
                migrated += 1
            backup = legacy.with_name(legacy.name + ".migrated")
            os.replace(legacy, backup)
            self.log.info("migrated legacy memory %s -> %s (%d projects)",
                          legacy, self.path, migrated)
        except OSError:
            # Migration is best effort: never block startup, keep the old file.
            self.log.exception("legacy memory migration failed; kept %s", legacy)

    def _load_projects(self) -> None:
        if self._backend is not None:
            self._load_from_backend()
            return
        if not self.path.is_dir():
            return
        try:
            entries = sorted(self.path.glob("*.json"))
        except OSError as exc:
            self.log.warning("could not list memory directory %s: %s",
                             self.path, exc)
            return
        for entry in entries:
            try:
                pid = validate_project_id(entry.stem)
            except ValidationError:
                self.log.warning("ignoring memory file with unsafe project id: %s",
                                 entry.name)
                continue
            project = self._read_project(pid, entry)
            if project is None:
                continue
            size = _encoded_size(pid, project)
            if size > self.max_project_bytes:
                self._quarantine(
                    entry, project_id=pid,
                    reason=f"project data is {size} bytes > "
                           f"{self.max_project_bytes} byte cap")
                continue
            self.data[pid] = project
            self._sizes[pid] = size
            self._total_bytes += size
        if self._total_bytes > self.max_total_bytes:
            self.log.warning(
                "memory store on disk is over the total cap (%d > %d bytes); "
                "new writes will be rejected until it shrinks",
                self._total_bytes, self.max_total_bytes)
        if len(self.data) > self.max_projects:
            self.log.warning(
                "memory store has %d projects, over the %d project cap; "
                "new projects will be rejected", len(self.data),
                self.max_projects)

    def _load_from_backend(self) -> None:
        """Warm the store from the KV backend (``memory`` namespace)."""
        try:
            entries = self._backend.items("memory")  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - a broken backend shouldn't stop boot
            self.log.warning("could not read memory from backend: %s", exc)
            return
        for raw_pid, payload in entries:
            try:
                pid = validate_project_id(raw_pid)
            except ValidationError:
                self.log.warning("ignoring memory row with unsafe project id: %r",
                                 raw_pid)
                continue
            if not isinstance(payload, dict):
                self.log.warning("ignoring memory row %s: not an object", pid)
                continue
            project = {str(key): value for key, value in payload.items()}
            size = _encoded_size(pid, project)
            if size > self.max_project_bytes:
                self.log.warning(
                    "memory row %s is %d bytes > the %d byte cap; ignored",
                    pid, size, self.max_project_bytes)
                continue
            self.data[pid] = project
            self._sizes[pid] = size
            self._total_bytes += size
        if self._total_bytes > self.max_total_bytes:
            self.log.warning(
                "memory store in backend is over the total cap (%d > %d bytes); "
                "new writes will be rejected until it shrinks",
                self._total_bytes, self.max_total_bytes)

    def _read_project(self, project_id: str, path: Path) -> dict | None:
        try:
            raw = path.read_text("utf-8")
        except OSError as exc:
            self._quarantine(path, project_id=project_id,
                             reason=f"unreadable memory file: {exc}")
            return None
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            self._quarantine(path, project_id=project_id,
                             reason=f"invalid JSON memory file: {exc}")
            return None
        if not isinstance(payload, dict):
            self._quarantine(path, project_id=project_id,
                             reason="memory file is not a JSON object")
            return None
        return {str(k): v for k, v in payload.items()}

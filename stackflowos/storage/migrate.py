"""One-shot JSON/JSONL -> SQLite migration (idempotent, CLI + function).

``migrate_json_to_sqlite`` copies the durable parts of an existing state
directory into the SQLite backend:

* event log rows (``events/events*.jsonl`` and ``*.jsonl.gz`` archives) into the
  ``events`` append namespace, preserving ``seq``;
* per-project memory (``state/memory/<project>.json`` and the legacy
  ``state/memory.json``) into the ``memory`` KV namespace;
* project/task counters (``state/counters.json``) into ``state/counters``.

The migration is idempotent: the highest migrated event ``seq`` is recorded in
the ``meta`` namespace, so re-running the command skips rows that are already
in the database (and picks up rows appended to the JSON log since the last
run).  Memory/counters are whole values and are simply rewritten.

Boundary (documented in ``docs/team/dist.md``): the project/task domain
(``state/projects.json``) is **not** moved by default -- ``ProjectStore`` keeps
writing JSON until it is taught to use a backend.  ``--include-projects`` copies
the file into ``state/projects`` as a KV value for a future migration, but the
running hub does not read it from there.

Usage::

    python -m stackflowos.storage.migrate --dry-run
    python -m stackflowos.storage.migrate --state-dir .stackflowos \\
        --sqlite-path .stackflowos/state/stackflowos.db
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator

from .backend import StorageError
from .sqlite_backend import SqliteBackend

_EVENT_FILE_RE = re.compile(r"^events-(?P<seq>\d+)(?:-[^.]*)?\.jsonl(?P<gz>\.gz)?$")
_PROJECT_FILE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _resolve_paths(paths: Any) -> tuple[Path, Path]:
    """Return ``(state_dir, events_dir)`` from a Paths object, mapping or Path."""
    events = getattr(paths, "events_dir", None)
    state = getattr(paths, "state_dir", None)
    if state is not None and events is not None:
        return Path(state), Path(events)
    if isinstance(paths, dict):
        state = Path(paths["state_dir"])
        events = Path(paths.get("events_dir") or state / "events")
        return state, events
    state = Path(paths)
    return state, state / "events"


def _event_files(events_dir: Path) -> list[Path]:
    """Rotation-ordered event files (oldest first, active file last)."""
    ordered: list[tuple[int, str, Path]] = []
    active = events_dir / "events.jsonl"
    try:
        entries = list(events_dir.iterdir())
    except OSError:
        entries = []
    for path in entries:
        if path.name == "events.jsonl":
            continue
        match = _EVENT_FILE_RE.match(path.name)
        if match:
            ordered.append((int(match.group("seq")), path.name, path))
    ordered.sort(key=lambda item: (item[0], item[1]))
    files = [path for _, _, path in ordered]
    if active.exists():
        files.append(active)
    return files


def _iter_event_rows(path: Path) -> Iterator[dict]:
    """Yield well-formed JSON objects from a JSONL or gzipped JSONL file."""
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("type"), str):
                    yield obj
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        raise StorageError(f"cannot read event file {path}: {exc}") from exc


def _row_seq(row: dict) -> int:
    try:
        return int(row.get("seq") or 0)
    except (TypeError, ValueError):
        return 0


def migrate_json_to_sqlite(
    paths: Any,
    *,
    sqlite_path: str | Path | None = None,
    backend: SqliteBackend | None = None,
    dry_run: bool = False,
    include_projects: bool = False,
    logger: Any = None,
) -> dict:
    """Copy JSON state into SQLite; return a per-namespace summary.

    ``paths`` may be a :class:`stackflowos.config.Paths`, a mapping with
    ``state_dir``/``events_dir`` keys, or the state directory itself.  Pass an
    existing ``backend`` to reuse a connection; otherwise one is opened for
    ``sqlite_path`` (or ``$SFO_STORAGE_SQLITE_PATH`` or the default location).
    """
    state_dir, events_dir = _resolve_paths(paths)
    target = Path(sqlite_path) if sqlite_path else (
        Path(os.environ["SFO_STORAGE_SQLITE_PATH"])
        if os.environ.get("SFO_STORAGE_SQLITE_PATH")
        else state_dir / "state" / "stackflowos.db"
    )
    owns_backend = backend is None
    store = backend or SqliteBackend(target)
    summary = {
        "sqlite_path": str(store.path),
        "events": 0,
        "event_files": 0,
        "event_skipped": 0,
        "memory_projects": 0,
        "counters": 0,
        "projects": 0,
        "dry_run": bool(dry_run),
        "errors": [],
    }
    started = time.monotonic()
    try:
        summary["events"], summary["event_files"], summary["event_skipped"] = \
            _migrate_events(store, events_dir, dry_run=dry_run,
                            errors=summary["errors"])
        summary["memory_projects"] = _migrate_memory(
            store, state_dir, dry_run=dry_run, errors=summary["errors"])
        summary["counters"] = _migrate_counters(
            store, state_dir, dry_run=dry_run, errors=summary["errors"])
        if include_projects:
            summary["projects"] = _migrate_projects(
                store, state_dir, dry_run=dry_run, errors=summary["errors"])
    finally:
        if owns_backend:
            store.close()
    summary["duration_ms"] = int((time.monotonic() - started) * 1000)
    if logger is not None and summary["errors"]:
        for message in summary["errors"]:
            logger.warning("migration: %s", message)
    return summary


def _migrate_events(store: SqliteBackend, events_dir: Path, *,
                    dry_run: bool, errors: list[str]) -> tuple[int, int, int]:
    last = store.last_row("events")
    watermark = max(_row_seq(last) if last else 0,
                    int(store.get("meta", "events.last_seq", 0) or 0))
    rows: list[dict] = []
    skipped = 0
    files = _event_files(events_dir)
    for path in files:
        try:
            for row in _iter_event_rows(path):
                seq = _row_seq(row)
                if seq <= watermark:
                    skipped += 1
                    continue
                rows.append(row)
        except StorageError as exc:
            errors.append(str(exc))
    rows.sort(key=_row_seq)
    if dry_run:
        return len(rows), len(files), skipped
    if rows:
        with store.transaction():
            for row in rows:
                store.append("events", row)
            store.set("meta", "events.last_seq", _row_seq(rows[-1]))
    return len(rows), len(files), skipped


def _migrate_memory(store: SqliteBackend, state_dir: Path, *,
                    dry_run: bool, errors: list[str]) -> int:
    memory_dir = state_dir / "state" / "memory"
    projects: dict[str, dict] = {}
    try:
        entries = sorted(memory_dir.glob("*.json"))
    except OSError:
        entries = []
    for path in entries:
        if not _PROJECT_FILE_RE.match(path.stem):
            continue
        data = _read_json_object(path, errors)
        if data is not None:
            projects[path.stem] = data
    legacy = state_dir / "state" / "memory.json"
    if legacy.exists():
        data = _read_json_object(legacy, errors)
        if isinstance(data, dict):
            for project_id, values in data.items():
                if (isinstance(values, dict)
                        and _PROJECT_FILE_RE.match(str(project_id))):
                    projects.setdefault(str(project_id), values)
    if dry_run or not projects:
        return len(projects)
    with store.transaction():
        for project_id, values in projects.items():
            store.set("memory", project_id, values)
    return len(projects)


def _migrate_counters(store: SqliteBackend, state_dir: Path, *,
                      dry_run: bool, errors: list[str]) -> int:
    path = state_dir / "state" / "counters.json"
    data = _read_json_object(path, errors)
    if not isinstance(data, dict):
        return 0
    if not dry_run:
        store.set("state", "counters", data)
    return 1


def _migrate_projects(store: SqliteBackend, state_dir: Path, *,
                      dry_run: bool, errors: list[str]) -> int:
    path = state_dir / "state" / "projects.json"
    data = _read_json_object(path, errors)
    if not isinstance(data, dict):
        return 0
    if not dry_run:
        store.set("state", "projects", data)
    return 1


def _read_json_object(path: Path, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        errors.append(f"cannot read {path}: {exc}")
        return None


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python -m stackflowos.storage.migrate``."""
    parser = argparse.ArgumentParser(
        prog="stackflowos.storage.migrate",
        description="Migrate a StackFlowOS JSON state directory into SQLite.")
    parser.add_argument("--state-dir", default=None,
                        help="state directory (default: $SFO_STATE_DIR or "
                             "<repo>/.stackflowos)")
    parser.add_argument("--sqlite-path", default=None,
                        help="target database (default: "
                             "<state>/state/stackflowos.db)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be migrated, write nothing")
    parser.add_argument("--include-projects", action="store_true",
                        help="also copy state/projects.json as a KV value")
    args = parser.parse_args(argv)
    try:
        from ..config import resolve_paths

        paths = resolve_paths(state_dir=Path(args.state_dir) if args.state_dir else None)
    except Exception as exc:  # pragma: no cover - defensive CLI path
        print(f"cannot resolve state directory: {exc}", file=sys.stderr)
        return 2
    try:
        result = migrate_json_to_sqlite(
            paths, sqlite_path=args.sqlite_path, dry_run=args.dry_run,
            include_projects=args.include_projects)
    except StorageError as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


__all__ = ["main", "migrate_json_to_sqlite"]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

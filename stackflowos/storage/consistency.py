"""Read-only JSON vs SQLite divergence check (diagnostics only).

A state directory can hold the durable data in either of two shapes:

* **JSON** -- ``events/events*.jsonl`` (plus gzip archives),
  ``state/memory/<project>.json`` / ``state/memory.json`` and
  ``state/counters.json``;
* **SQLite** -- ``<state>/state/stackflowos.db`` (``rows``/``kv`` tables).

The switch between them is explicit (``python -m stackflowos.storage.migrate``)
and nothing dual-writes, so the two copies drift as soon as the hub keeps
appending to one side after a migration.  The same happens when a backend
write fails and a writer falls back to the JSON file (the fallback logs a
WARNING but the database stays stale -- see ``ProjectStore._write_counters``).

:func:`verify_divergence` reports that drift **without modifying anything**:
the database is opened with SQLite's ``mode=ro`` URI (a missing file is never
created) and the JSON files are only read.  The result is a JSON-friendly
payload intended for a health endpoint, an ops report or the CLI::

    python -m stackflowos.storage.consistency --state-dir .stackflowos

Exit codes: ``0`` consistent (or no SQLite copy to compare against), ``1``
divergence found, ``2`` the state directory could not be resolved.

Transient streaming rows (``chat.delta``/``agent.chat.delta``/``*.delta``) are
never durable by design, so they are ignored on both sides: a legacy JSONL file
containing one is not reported as divergence.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from .migrate import _PROJECT_FILE_RE, _event_files, _iter_event_rows, _resolve_paths

#: Default cap on event rows scanned per side (JSON lines or SQLite rows).
#: Past it the report is marked ``truncated`` and the counts become lower
#: bounds; the cap keeps the check bounded on very large histories.
DEFAULT_MAX_RECORDS = 100_000
#: Number of offending sequence numbers/projects listed per category.
_SAMPLE_LIMIT = 10
#: Bound on collected human-readable error strings.
_MAX_ERRORS = 20


def _is_transient(event_type: Any) -> bool:
    """Match :meth:`stackflowos.l2.eventlog.EventLog.is_transient` lazily.

    Imported inside the function to avoid a ``storage <-> l2.eventlog`` import
    cycle (the event log imports the storage package).
    """
    from ..l2.eventlog import EventLog

    return EventLog.is_transient(event_type)


def _default_sqlite_target(state_dir: Path) -> Path:
    from . import ENV_SQLITE_PATH, default_sqlite_path

    configured = os.environ.get(ENV_SQLITE_PATH, "").strip()
    return Path(configured) if configured else default_sqlite_path(state_dir)


def _json_events(events_dir: Path, *, max_records: int,
                 errors: list[str]) -> tuple[set[int], bool]:
    """Return the durable (non-transient) event ``seq`` values in JSON files."""
    from .backend import StorageError

    seqs: set[int] = set()
    truncated = False
    scanned = 0
    for path in _event_files(events_dir):
        try:
            for row in _iter_event_rows(path):
                if scanned >= max_records:
                    truncated = True
                    break
                scanned += 1
                if _is_transient(row.get("type")):
                    continue
                try:
                    seq = int(row.get("seq") or 0)
                except (TypeError, ValueError):
                    continue
                if seq > 0:
                    seqs.add(seq)
        except StorageError as exc:
            errors.append(str(exc))
        if truncated:
            break
    return seqs, truncated


def _open_readonly(path: Path) -> sqlite3.Connection:
    """Open *path* with SQLite's read-only URI (never creates the file)."""
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=? "
        "LIMIT 1", (str(name),)).fetchone()
    return row is not None


def _sqlite_events(connection: sqlite3.Connection, *, max_records: int,
                   errors: list[str]) -> tuple[set[int], bool, int]:
    """Return durable event ``seq`` values, truncation flag and malformed rows."""
    seqs: set[int] = set()
    truncated = False
    malformed = 0
    scanned = 0
    try:
        cursor = connection.execute(
            "SELECT data FROM rows WHERE namespace='events' ORDER BY row_id")
        for row in cursor:
            if scanned >= max_records:
                truncated = True
                break
            scanned += 1
            try:
                obj = json.loads(row["data"])
            except (TypeError, ValueError):
                malformed += 1
                continue
            if not isinstance(obj, dict) or _is_transient(obj.get("type")):
                continue
            try:
                seq = int(obj.get("seq") or 0)
            except (TypeError, ValueError):
                malformed += 1
                continue
            if seq > 0:
                seqs.add(seq)
    except sqlite3.Error as exc:
        errors.append(f"cannot read sqlite events: {exc}")
    return seqs, truncated, malformed


def _sqlite_kv(connection: sqlite3.Connection, namespace: str, key: str,
               errors: list[str]) -> Any:
    try:
        row = connection.execute(
            "SELECT value FROM kv WHERE namespace=? AND key=?",
            (str(namespace), str(key))).fetchone()
    except sqlite3.Error as exc:
        errors.append(f"cannot read sqlite kv {namespace}/{key}: {exc}")
        return None
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        errors.append(f"corrupt sqlite kv value {namespace}/{key}")
        return None


def _sqlite_kv_items(connection: sqlite3.Connection, namespace: str,
                     errors: list[str]) -> dict[str, Any]:
    try:
        rows = connection.execute(
            "SELECT key, value FROM kv WHERE namespace=? ORDER BY key",
            (str(namespace),)).fetchall()
    except sqlite3.Error as exc:
        errors.append(f"cannot read sqlite kv namespace {namespace}: {exc}")
        return {}
    out: dict[str, Any] = {}
    for row in rows:
        try:
            out[str(row["key"])] = json.loads(row["value"])
        except (TypeError, ValueError):
            errors.append(f"corrupt sqlite kv value {namespace}/{row['key']}")
    return out


def _json_object(path: Path, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        errors.append(f"cannot read {path}: {exc}")
        return None


def _json_counters(state_dir: Path, errors: list[str]) -> dict:
    """Return ``state/counters.json`` as a dict (``{}`` when absent/invalid)."""
    raw = _json_object(state_dir / "state" / "counters.json", errors)
    return raw if isinstance(raw, dict) else {}


def _json_memory(state_dir: Path, errors: list[str]) -> dict[str, Any]:
    """Return the JSON memory view (per-project files + legacy ``memory.json``)."""
    projects: dict[str, Any] = {}
    memory_dir = state_dir / "state" / "memory"
    try:
        entries = sorted(memory_dir.glob("*.json"))
    except OSError:
        entries = []
    for path in entries:
        if not _PROJECT_FILE_RE.match(path.stem):
            continue
        data = _json_object(path, errors)
        if data is not None:
            projects[path.stem] = data
    legacy = _json_object(state_dir / "state" / "memory.json", errors)
    if isinstance(legacy, dict):
        for project_id, values in legacy.items():
            if (isinstance(values, dict)
                    and _PROJECT_FILE_RE.match(str(project_id))):
                projects.setdefault(str(project_id), values)
    return projects


def _compare_keyed(json_map: Any, sqlite_map: Any) -> list[str]:
    """Keys whose value differs between the JSON and SQLite maps."""
    if isinstance(json_map, dict) and isinstance(sqlite_map, dict):
        keys = set(json_map) | set(sqlite_map)
        return sorted(str(key) for key in keys
                      if json_map.get(key) != sqlite_map.get(key))
    if json_map is None and sqlite_map is None:
        return []
    if json_map == sqlite_map:
        return []
    return ["<root>"]


def _event_summary(json_seqs: set[int], sqlite_seqs: set[int],
                   *, checked: bool, json_rows: int, sqlite_rows: int,
                   malformed: int) -> dict:
    # Without a checked counterpart a "missing" count would be meaningless
    # (every JSON row would look SQLite-missing), so only diff when checked.
    if checked:
        missing_in_sqlite = sorted(json_seqs - sqlite_seqs)
        missing_in_json = sorted(sqlite_seqs - json_seqs)
    else:
        missing_in_sqlite, missing_in_json = [], []
    return {
        "json_rows": int(json_rows),
        "sqlite_rows": int(sqlite_rows),
        "json_last_seq": max(json_seqs) if json_seqs else 0,
        "sqlite_last_seq": max(sqlite_seqs) if sqlite_seqs else 0,
        "missing_in_sqlite": len(missing_in_sqlite),
        "missing_in_json": len(missing_in_json),
        "samples": {
            "missing_in_sqlite": missing_in_sqlite[:_SAMPLE_LIMIT],
            "missing_in_json": missing_in_json[:_SAMPLE_LIMIT],
        },
        "malformed_sqlite_rows": int(malformed),
        "checked": bool(checked),
    }


def verify_divergence(paths: Any, *, sqlite_path: str | Path | None = None,
                      max_records: int = DEFAULT_MAX_RECORDS) -> dict:
    """Compare the JSON state files with the SQLite copy, read-only.

    ``paths`` accepts a :class:`stackflowos.config.Paths`, a mapping with
    ``state_dir``/``events_dir`` keys, or the state directory itself (the same
    shapes as :func:`stackflowos.storage.migrate_json_to_sqlite`).

    Nothing is written: a missing database is reported as ``checked: false``
    (never created), and an existing one is opened with ``mode=ro``.  The
    returned payload contains per-category counts plus small samples; the
    top-level ``consistent`` flag is ``None`` when there was nothing to compare.
    """
    state_dir, events_dir = _resolve_paths(paths)
    target = (Path(sqlite_path) if sqlite_path
              else _default_sqlite_target(state_dir))
    max_records = max(1, int(max_records))
    errors: list[str] = []
    json_seqs, truncated = _json_events(events_dir, max_records=max_records,
                                        errors=errors)
    summary: dict[str, Any] = {
        "state_dir": str(state_dir),
        "events_dir": str(events_dir),
        "sqlite_path": str(target),
        "sqlite_present": target.exists(),
        "sqlite_schema_version": None,
        "checked": False,
        "consistent": None,
        "divergences": 0,
        "truncated": bool(truncated),
        "checked_at_ms": int(time.time() * 1000),
        "events": _event_summary(json_seqs, set(), checked=False,
                                 json_rows=len(json_seqs), sqlite_rows=0,
                                 malformed=0),
        "counters": {"json_present": False, "sqlite_present": False,
                     "diverged_keys": []},
        "memory": {"json_projects": 0, "sqlite_keys": 0,
                   "diverged_keys": []},
        "errors": [],
    }
    json_counters = _json_counters(state_dir, errors)
    json_memory = _json_memory(state_dir, errors)
    summary["counters"]["json_present"] = bool(json_counters)
    summary["memory"]["json_projects"] = len(json_memory)

    if not summary["sqlite_present"]:
        if len(errors) > _MAX_ERRORS:
            errors = errors[:_MAX_ERRORS] + ["... (truncated)"]
        summary["errors"] = errors
        return summary

    try:
        connection = _open_readonly(target)
    except sqlite3.Error as exc:
        errors.append(f"cannot open sqlite database {target} read-only: {exc}")
        summary["errors"] = errors[:_MAX_ERRORS]
        return summary

    try:
        try:
            version = int(connection.execute(
                "PRAGMA user_version").fetchone()[0])
        except sqlite3.Error:
            version = None
        summary["sqlite_schema_version"] = version
        if not (_table_exists(connection, "rows")
                and _table_exists(connection, "kv")):
            errors.append(
                f"sqlite database {target} has no storage schema "
                f"(user_version={version}); run the migration CLI")
            summary["errors"] = errors[:_MAX_ERRORS]
            return summary

        sqlite_seqs, sqlite_truncated, malformed = _sqlite_events(
            connection, max_records=max_records, errors=errors)
        sqlite_counters_raw = _sqlite_kv(connection, "state", "counters", errors)
        sqlite_counters = sqlite_counters_raw \
            if isinstance(sqlite_counters_raw, dict) else {}
        sqlite_memory = _sqlite_kv_items(connection, "memory", errors)
    except sqlite3.Error as exc:
        # A corrupt/non-SQLite file surfaces on the first statement.
        errors.append(f"cannot read sqlite database {target}: {exc}")
        summary["errors"] = errors[:_MAX_ERRORS]
        return summary
    finally:
        connection.close()

    counters_diverged = _compare_keyed(json_counters, sqlite_counters)
    memory_diverged = _compare_keyed(json_memory, sqlite_memory)
    summary["checked"] = True
    summary["truncated"] = bool(truncated or sqlite_truncated)
    summary["events"] = _event_summary(
        json_seqs, sqlite_seqs, checked=True, json_rows=len(json_seqs),
        sqlite_rows=len(sqlite_seqs), malformed=malformed)
    summary["counters"] = {
        "json_present": bool(json_counters),
        "sqlite_present": bool(sqlite_counters),
        "diverged_keys": counters_diverged,
    }
    summary["memory"] = {
        "json_projects": len(json_memory),
        "sqlite_keys": len(sqlite_memory),
        "diverged_keys": memory_diverged,
    }
    divergences = (
        summary["events"]["missing_in_sqlite"]
        + summary["events"]["missing_in_json"]
        + len(counters_diverged)
        + len(memory_diverged)
    )
    summary["divergences"] = int(divergences)
    summary["consistent"] = divergences == 0
    if len(errors) > _MAX_ERRORS:
        errors = errors[:_MAX_ERRORS] + ["... (truncated)"]
    summary["errors"] = errors
    return summary


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python -m stackflowos.storage.consistency``."""
    parser = argparse.ArgumentParser(
        prog="stackflowos.storage.consistency",
        description="Read-only report of JSON vs SQLite state divergence.")
    parser.add_argument("--state-dir", default=None,
                        help="state directory (default: $SFO_STATE_DIR or "
                             "<repo>/.stackflowos)")
    parser.add_argument("--sqlite-path", default=None,
                        help="database to compare (default: "
                             "<state>/state/stackflowos.db)")
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS,
                        help="cap on event rows scanned per side "
                             "(past it the report is marked truncated)")
    args = parser.parse_args(argv)
    try:
        from ..config import resolve_paths

        paths = resolve_paths(
            state_dir=Path(args.state_dir) if args.state_dir else None)
    except Exception as exc:  # pragma: no cover - defensive CLI path
        print(f"cannot resolve state directory: {exc}", file=sys.stderr)
        return 2
    result = verify_divergence(paths, sqlite_path=args.sqlite_path,
                               max_records=args.max_records)
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    if not result["checked"]:
        return 0
    return 0 if result["consistent"] else 1


__all__ = ["DEFAULT_MAX_RECORDS", "main", "verify_divergence"]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""DIST: JSON/SQLite storage backends, transactions and JSON->SQLite migration."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from stackflowos.config import default_root, resolve_paths
from stackflowos.l2.eventlog import EventLog
from stackflowos.memory import MemoryStore
from stackflowos.storage import (
    JsonFileBackend,
    SqliteBackend,
    StorageError,
    default_sqlite_path,
    migrate_json_to_sqlite,
    open_backend,
)
from stackflowos.storage.migrate import main as migrate_main

REPO = default_root()


class _Base(unittest.TestCase):
    def setUp(self):
        base = REPO / ".stackflowos" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(base))
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def sqlite(self, name: str = "sfo.db") -> SqliteBackend:
        return SqliteBackend(self.dir / name)


class JsonBackendTests(_Base):
    def test_kv_roundtrip_and_delete(self):
        backend = JsonFileBackend(self.dir / "json")
        self.assertIsNone(backend.get("memory", "p-1"))
        backend.set("memory", "p-1", {"a": [1, 2]})
        backend.set("memory", "p-2", {"b": True})
        self.assertEqual(backend.get("memory", "p-1"), {"a": [1, 2]})
        self.assertEqual(backend.keys("memory"), ["p-1", "p-2"])
        self.assertTrue(backend.has_prefix("memory", "p-"))
        self.assertFalse(backend.has_prefix("memory", "q-"))
        self.assertTrue(backend.delete("memory", "p-1"))
        self.assertFalse(backend.delete("memory", "p-1"))
        self.assertEqual(backend.items("memory"), [("p-2", {"b": True})])
        self.assertEqual(backend.health()["backend"], "json")

    def test_append_rows_and_reverse(self):
        backend = JsonFileBackend(self.dir / "json")
        for i in range(4):
            backend.append("events", {"seq": i + 1, "type": "t"})
        self.assertEqual(backend.count("events"), 4)
        self.assertEqual([row["seq"] for row in backend.iter_rows("events")],
                         [1, 2, 3, 4])
        self.assertEqual(
            [row["seq"] for row in backend.iter_rows("events", reverse=True,
                                                     limit=2)],
            [4, 3])
        self.assertEqual(backend.last_row("events")["seq"], 4)
        backend.close()
        with self.assertRaises(StorageError):
            backend.get("memory", "p-1")


class SqliteBackendTests(_Base):
    def test_roundtrip_wal_and_schema(self):
        backend = self.sqlite()
        backend.set("memory", "p-1", {"a": 1})
        backend.set("state", "counters", {"project": 3})
        for i in range(3):
            backend.append("events", {"seq": i + 1, "type": "t"})
        self.assertEqual(backend.get("memory", "p-1"), {"a": 1})
        self.assertEqual(backend.items("state"),
                         [("counters", {"project": 3})])
        self.assertEqual(backend.count("events"), 3)
        self.assertEqual(
            [row["seq"] for row in backend.iter_rows("events", reverse=True)],
            [3, 2, 1])
        health = backend.health()
        self.assertEqual(health["journal_mode"], "wal")
        self.assertEqual(health["schema_version"], 1)
        backend.close()
        # Reopening the same file keeps schema/data and reports WAL.
        again = SqliteBackend(self.dir / "sfo.db")
        self.assertEqual(again.get("memory", "p-1"), {"a": 1})
        self.assertEqual(again.health()["journal_mode"], "wal")
        again.close()

    def test_transaction_commit_and_rollback(self):
        backend = self.sqlite()
        with backend.transaction():
            backend.set("memory", "p-1", {"v": 1})
            backend.append("events", {"seq": 1})
        self.assertEqual(backend.get("memory", "p-1"), {"v": 1})
        self.assertEqual(backend.count("events"), 1)
        with self.assertRaises(RuntimeError):
            with backend.transaction():
                backend.set("memory", "p-1", {"v": 2})
                backend.append("events", {"seq": 2})
                raise RuntimeError("boom")
        self.assertEqual(backend.get("memory", "p-1"), {"v": 1})
        self.assertEqual(backend.count("events"), 1)
        # A nested transaction participates in the outer one.
        with backend.transaction():
            backend.set("memory", "p-2", {"v": 1})
            with backend.transaction():
                backend.set("memory", "p-3", {"v": 1})
        self.assertEqual(backend.get("memory", "p-3"), {"v": 1})
        backend.close()

    def test_open_backend_and_default_path(self):
        state = self.dir / "state"
        backend = open_backend("sqlite", state_dir=state)
        self.addCleanup(backend.close)
        self.assertEqual(Path(backend.path), default_sqlite_path(state))
        with self.assertRaises(StorageError):
            open_backend("nope", state_dir=state)
        json_backend = open_backend("json", state_dir=state)
        self.addCleanup(json_backend.close)
        self.assertEqual(json_backend.name, "json")


class MemoryBackendTests(_Base):
    def test_memory_store_on_sqlite(self):
        backend = self.sqlite()
        store = MemoryStore(self.dir / "state" / "memory", backend=backend)
        store.set("p-1", "plan", {"steps": [1, 2]})
        self.assertEqual(store.get("p-1", "plan"), {"steps": [1, 2]})
        self.assertEqual(store.all("p-1"), {"plan": {"steps": [1, 2]}})
        reopened = MemoryStore(self.dir / "state" / "memory", backend=backend)
        self.assertEqual(reopened.get("p-1", "plan"), {"steps": [1, 2]})
        self.assertTrue(reopened.delete("p-1"))
        self.assertIsNone(reopened.get("p-1", "plan"))
        self.assertEqual(reopened.health()["backend"], "sqlite")


class CountersBackendTests(_Base):
    def test_project_counters_use_backend_and_survive_restart(self):
        from stackflowos.projects.store import ProjectStore

        state = self.dir / "state"
        state.mkdir(parents=True)
        backend = self.sqlite()
        store = ProjectStore(
            None,
            state_path=state / "projects.json",
            counter_path=state / "counters.json",
            workspace_root=state / "projects",
            backend=backend,
        )
        self.assertEqual(store.create("first").id, "p-1")
        self.assertEqual(backend.get("state", "counters")["project"], 1)
        self.assertFalse((state / "counters.json").exists())
        # A fresh store over the same backend continues the id sequence.
        reopened = ProjectStore(
            None,
            state_path=state / "projects.json",
            counter_path=state / "counters.json",
            workspace_root=state / "projects",
            backend=backend,
        )
        self.assertEqual(reopened.create("second").id, "p-2")
        self.assertEqual(backend.get("state", "counters")["project"], 2)


class MigrationTests(_Base):
    def _seed_state(self) -> Path:
        state = self.dir / "state"
        (state / "events").mkdir(parents=True)
        (state / "state" / "memory").mkdir(parents=True)
        # Two rotated plain files (oldest first) + the active file.
        old = {"seq": 1, "id": "ev-1", "type": "sys.booted", "source": "sys",
               "ts": 1, "data": {}}
        mid = {"seq": 2, "id": "ev-2", "type": "task.created", "source": "sys",
               "ts": 2, "data": {"task_id": "t-1"}, "project_id": "p-1"}
        for name, rows in (("events-1.jsonl", [old]),
                           ("events-2.jsonl", [mid])):
            (state / "events" / name).write_text(
                "".join(json.dumps(row) + "\n" for row in rows), "utf-8")
        # A gzip archive is migrated too (new rotation format).
        gz_row = {"seq": 3, "id": "ev-3", "type": "task.updated",
                  "source": "sys", "ts": 3, "data": {}, "project_id": "p-1"}
        with gzip.open(state / "events" / "events-3-123.jsonl.gz", "wt",
                       encoding="utf-8") as fh:
            fh.write(json.dumps(gz_row) + "\n")
        active = {"seq": 4, "id": "ev-4", "type": "sys.ready", "source": "sys",
                  "ts": 4, "data": {}}
        (state / "events" / "events.jsonl").write_text(
            json.dumps(active) + "\n", "utf-8")
        (state / "state" / "memory" / "p-1.json").write_text(
            json.dumps({"plan": "x"}), "utf-8")
        (state / "state" / "counters.json").write_text(
            json.dumps({"project": 1, "task": 2, "milestone": 0}), "utf-8")
        return state

    def test_migrates_events_memory_and_counters(self):
        state = self._seed_state()
        summary = migrate_json_to_sqlite(resolve_paths(state_dir=state))
        self.assertEqual(summary["events"], 4)
        self.assertEqual(summary["memory_projects"], 1)
        self.assertEqual(summary["counters"], 1)
        db = SqliteBackend(default_sqlite_path(state))
        try:
            self.assertEqual([row["seq"] for row in db.iter_rows("events")],
                             [1, 2, 3, 4])
            self.assertEqual(db.get("memory", "p-1"), {"plan": "x"})
            self.assertEqual(db.get("state", "counters")["task"], 2)
        finally:
            db.close()

    def test_migration_is_idempotent(self):
        state = self._seed_state()
        first = migrate_json_to_sqlite(state)
        second = migrate_json_to_sqlite(state)
        self.assertEqual(first["events"], 4)
        self.assertEqual(second["events"], 0, "re-running must not duplicate rows")
        db = SqliteBackend(default_sqlite_path(state))
        try:
            self.assertEqual(db.count("events"), 4)
        finally:
            db.close()
        # New rows appended to the JSON log after migration are picked up.
        active = state / "events" / "events.jsonl"
        with active.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"seq": 5, "id": "ev-5", "type": "x",
                                 "source": "sys", "ts": 5, "data": {}}) + "\n")
        third = migrate_json_to_sqlite(state)
        self.assertEqual(third["events"], 1)

    def test_dry_run_writes_nothing(self):
        state = self._seed_state()
        summary = migrate_json_to_sqlite(state, dry_run=True)
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["events"], 4)
        db = SqliteBackend(default_sqlite_path(state))
        try:
            self.assertEqual(db.count("events"), 0)
        finally:
            db.close()

    def test_migrated_history_is_readable_by_eventlog(self):
        state = self._seed_state()
        migrate_json_to_sqlite(state)
        backend = SqliteBackend(default_sqlite_path(state))
        try:
            log = EventLog(state / "events", backend=backend, memory_limit=2)
            self.assertEqual(log.last_seq, 4)
            self.assertEqual([r.seq for r in log.since(0)], [1, 2, 3, 4])
            self.assertEqual([r.seq for r in log.tail(2)], [3, 4])
        finally:
            backend.close()

    def test_cli_dry_run(self):
        state = self._seed_state()
        code = migrate_main(["--state-dir", str(state), "--dry-run"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()

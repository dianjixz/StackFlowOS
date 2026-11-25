"""Read-only JSON vs SQLite divergence check (audit section 3 items 2/3).

The two persistence sets are not dual-written, and a failed backend write can
fall back to JSON, so the copies drift.  ``verify_divergence`` must report that
without mutating either side.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from stackflowos.config import default_root, resolve_paths
from stackflowos.storage import (
    SqliteBackend,
    default_sqlite_path,
    migrate_json_to_sqlite,
    verify_divergence,
)
from stackflowos.storage.consistency import main as consistency_main

REPO = default_root()


def _event(seq: int, event_type: str = "test.event", **data) -> dict:
    return {"seq": seq, "id": f"ev-{seq}", "type": event_type, "source": "sys",
            "ts": 1_000 + seq, "data": data}


class ConsistencyReportTests(unittest.TestCase):
    def setUp(self):
        base = REPO / ".stackflowos" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(base))
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def seed(self) -> Path:
        """Create a small JSON state directory (two events, memory, counters)."""
        state = self.dir / "state"
        events_dir = state / "events"
        events_dir.mkdir(parents=True)
        rows = [_event(1, "sys.booted"), _event(2, "task.created", task_id="t-1")]
        (events_dir / "events.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), "utf-8")
        (state / "state" / "memory").mkdir(parents=True)
        (state / "state" / "memory" / "p-1.json").write_text(
            json.dumps({"plan": "x"}), "utf-8")
        (state / "state" / "counters.json").write_text(
            json.dumps({"project": 1, "task": 2, "milestone": 0}), "utf-8")
        return state

    def migrated(self) -> Path:
        state = self.seed()
        migrate_json_to_sqlite(resolve_paths(state_dir=state))
        return state

    def check(self, state: Path, **kwargs) -> dict:
        return verify_divergence(resolve_paths(state_dir=state), **kwargs)

    # -- happy path -------------------------------------------------------
    def test_consistent_after_migration_and_read_only(self):
        state = self.migrated()
        db_path = default_sqlite_path(state)
        json_files = [state / "events" / "events.jsonl",
                      state / "state" / "counters.json",
                      state / "state" / "memory" / "p-1.json"]
        before_json = {path: path.read_bytes() for path in json_files}
        before_db = db_path.read_bytes()

        result = self.check(state)

        self.assertTrue(result["checked"])
        self.assertTrue(result["sqlite_present"])
        self.assertEqual(result["sqlite_schema_version"], 1)
        self.assertTrue(result["consistent"])
        self.assertEqual(result["divergences"], 0)
        self.assertEqual(result["events"]["json_rows"], 2)
        self.assertEqual(result["events"]["sqlite_rows"], 2)
        self.assertEqual(result["events"]["json_last_seq"], 2)
        self.assertEqual(result["events"]["sqlite_last_seq"], 2)
        self.assertEqual(result["events"]["missing_in_sqlite"], 0)
        self.assertEqual(result["events"]["missing_in_json"], 0)
        self.assertEqual(result["counters"]["diverged_keys"], [])
        self.assertEqual(result["memory"]["diverged_keys"], [])

        # Nothing was written by the check.
        self.assertEqual({path: path.read_bytes() for path in json_files},
                         before_json)
        self.assertEqual(db_path.read_bytes(), before_db)

    def test_json_events_appended_after_migration_are_reported(self):
        state = self.migrated()
        with (state / "events" / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_event(3, "task.updated")) + "\n")

        result = self.check(state)

        self.assertFalse(result["consistent"])
        self.assertEqual(result["events"]["missing_in_sqlite"], 1)
        self.assertEqual(result["events"]["samples"]["missing_in_sqlite"], [3])
        self.assertEqual(result["events"]["missing_in_json"], 0)
        self.assertGreaterEqual(result["divergences"], 1)

    def test_sqlite_only_events_are_reported(self):
        state = self.migrated()
        backend = SqliteBackend(default_sqlite_path(state))
        try:
            backend.append("events", _event(9, "unit.registered"))
        finally:
            backend.close()

        result = self.check(state)

        self.assertFalse(result["consistent"])
        self.assertEqual(result["events"]["missing_in_json"], 1)
        self.assertEqual(result["events"]["samples"]["missing_in_json"], [9])

    # -- counters / memory ------------------------------------------------
    def test_counters_divergence_matches_json_fallback_scenario(self):
        state = self.migrated()
        # What ProjectStore._write_counters does when the backend fails: the
        # JSON file advances while SQLite keeps the stale counters.
        (state / "state" / "counters.json").write_text(
            json.dumps({"project": 2, "task": 2, "milestone": 0}), "utf-8")

        result = self.check(state)

        self.assertFalse(result["consistent"])
        self.assertEqual(result["counters"]["diverged_keys"], ["project"])
        self.assertTrue(result["counters"]["json_present"])
        self.assertTrue(result["counters"]["sqlite_present"])

    def test_memory_divergence_reports_changed_and_missing_projects(self):
        state = self.migrated()
        (state / "state" / "memory" / "p-1.json").write_text(
            json.dumps({"plan": "y"}), "utf-8")
        (state / "state" / "memory" / "p-2.json").write_text(
            json.dumps({"plan": "z"}), "utf-8")

        result = self.check(state)

        self.assertFalse(result["consistent"])
        self.assertEqual(result["memory"]["diverged_keys"], ["p-1", "p-2"])
        self.assertEqual(result["memory"]["json_projects"], 2)
        self.assertEqual(result["memory"]["sqlite_keys"], 1)

    def test_corrupt_database_reports_an_error_and_stays_unchecked(self):
        state = self.seed()
        db_path = default_sqlite_path(state)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_bytes(b"this is not a sqlite database")

        result = self.check(state)

        self.assertTrue(result["sqlite_present"])
        self.assertFalse(result["checked"])
        self.assertTrue(result["errors"])
        self.assertIsNone(result["consistent"])

    # -- transient / absence / truncation ---------------------------------
    def test_transient_legacy_rows_are_not_divergence(self):
        state = self.migrated()
        with (state / "events" / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_event(3, "chat.delta", text="half")) + "\n")

        result = self.check(state)

        self.assertTrue(result["consistent"],
                        "streaming deltas are never durable, not divergence")
        self.assertEqual(result["events"]["json_rows"], 2)

    def test_missing_sqlite_is_unchecked_and_never_created(self):
        state = self.seed()

        result = self.check(state)

        self.assertFalse(result["sqlite_present"])
        self.assertFalse(result["checked"])
        self.assertIsNone(result["consistent"])
        self.assertEqual(result["divergences"], 0)
        self.assertEqual(result["events"]["json_rows"], 2)
        self.assertEqual(result["events"]["missing_in_sqlite"], 0,
                         "unchecked must not report meaningless 'missing' rows")
        self.assertFalse(default_sqlite_path(state).exists(),
                         "the check must not create a database")

    def test_max_records_marks_the_report_truncated(self):
        state = self.migrated()

        result = self.check(state, max_records=1)

        self.assertTrue(result["truncated"])
        self.assertEqual(result["events"]["json_rows"], 1)
        self.assertEqual(result["events"]["sqlite_rows"], 1)

    # -- CLI --------------------------------------------------------------
    def test_cli_exit_codes_and_json_output(self):
        state = self.migrated()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = consistency_main(["--state-dir", str(state)])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["consistent"])

        with (state / "events" / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_event(4, "task.updated")) + "\n")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = consistency_main(["--state-dir", str(state)])
        self.assertEqual(code, 1)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["consistent"])
        self.assertEqual(payload["events"]["missing_in_sqlite"], 1)


if __name__ == "__main__":
    unittest.main()

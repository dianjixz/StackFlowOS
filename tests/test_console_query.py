"""L7: console event queries must search memory *and* event files.

``App.console`` used to take ``eventlog.tail(limit * 4)`` first and filter
afterwards, so a project's events were lost once they fell outside the newest
window.  These tests pin the filtered query semantics: newest-first scan of the
memory deque, bounded backward file replay, oldest-first results, and the
``types`` exact/prefix rules the console always had.
"""

import tempfile
import unittest
from pathlib import Path

from stackflowos.app import App
from stackflowos.config import default_root
from stackflowos.errors import BadRequest
from stackflowos.l2.eventlog import EventLog

REPO = default_root()


class ConsoleQueryTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.events_dir = Path(self._tmp.name) / "events"

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _app(log: EventLog) -> App:
        """A minimal App sufficient for ``App.console`` (no hub boot)."""
        app = App.__new__(App)
        app.eventlog = log
        return app


class ConsoleQueryTests(ConsoleQueryTestBase):
    def test_console_finds_old_project_events_outside_tail_window(self):
        log = EventLog(self.events_dir)
        for i in range(100):
            log.append("task.updated", {"i": i}, project_id="proj-a")
        for i in range(1900):
            log.append("task.updated", {"i": i}, project_id="proj-b")

        # The old implementation's window (tail(limit*4, 400)) has no proj-a.
        self.assertEqual(
            [r for r in log.tail(400) if r.project_id == "proj-a"], [])

        rows = self._app(log).console(project_id="proj-a", limit=10)
        self.assertEqual([r["seq"] for r in rows], list(range(91, 101)))
        self.assertTrue(all(r["project_id"] == "proj-a" for r in rows))

    def test_query_memory_results_are_oldest_first(self):
        log = EventLog(self.events_dir)
        for i in range(20):
            log.append("task.updated", {"i": i}, project_id="proj-a")
            log.append("task.updated", {"i": i}, project_id="proj-b")

        rows = log.query(project_id="proj-a", limit=3)
        self.assertEqual([r.seq for r in rows], [35, 37, 39])
        self.assertEqual([r.data["i"] for r in rows], [17, 18, 19])

    def test_query_types_exact_and_prefix(self):
        log = EventLog(self.events_dir)
        appends = ["rule.created", "rule.created.extra", "rulebook",
                   "sys.booted", "rule", "sys.booted.extra"]
        for event_type in appends:
            log.append(event_type, {}, project_id="p-1")

        self.assertEqual(
            [r.type for r in log.query(types=["rule"])],
            ["rule.created", "rule.created.extra", "rule"])
        # Prefix matching is dotted: "sys.booted" also matches children, while
        # an unrelated bare name is exact.
        self.assertEqual(
            [r.type for r in log.query(types=["sys.booted"])],
            ["sys.booted", "sys.booted.extra"])
        self.assertEqual(
            [r.type for r in log.query(types=["sys.booted.extra"])],
            ["sys.booted.extra"])
        self.assertEqual(
            [r.type for r in log.query(types=["rulebook"])], ["rulebook"])
        # Empty types means "no type filter", matching console semantics.
        self.assertEqual([r.type for r in log.query(types=[])], appends)

    def test_query_combines_project_and_type_filters(self):
        log = EventLog(self.events_dir)
        log.append("rule.created", {}, project_id="p-1")
        log.append("rule.created", {}, project_id="p-2")
        log.append("sys.booted", {}, project_id="p-1")
        log.append("rule.updated", {}, project_id="p-1")

        rows = log.query(project_id="p-1", types=["rule"], limit=10)
        self.assertEqual([r.type for r in rows], ["rule.created", "rule.updated"])
        self.assertEqual([r.seq for r in rows], [1, 4])

    def test_limit_bounds(self):
        log = EventLog(self.events_dir)
        for i in range(5):
            log.append("test.event", {"i": i})
        self.assertEqual(len(log.query(limit=1000)), 5)
        self.assertEqual(len(log.query(limit=1)), 1)
        for bad in (0, -1, 1001, "not-a-number"):
            with self.assertRaises(BadRequest):
                log.query(limit=bad)

    def test_no_matches_returns_empty(self):
        log = EventLog(self.events_dir)
        self.assertEqual(log.query(project_id="nope"), [])
        self.assertEqual(log.query(types=["nope"]), [])
        log.append("test.event", {})
        self.assertEqual(log.query(project_id="nope"), [])
        self.assertEqual(log.query(types=["nope"]), [])
        self.assertEqual(
            self._app(log).console(project_id="nope", limit=10), [])

    def test_query_replays_files_when_memory_window_too_small(self):
        log = EventLog(self.events_dir, memory_limit=10)
        for i in range(100):
            log.append("task.updated", {"i": i}, project_id="proj-a")
        for i in range(1900):
            log.append("task.updated", {"i": i}, project_id="proj-b")

        rows = log.query(project_id="proj-a", limit=5)
        self.assertEqual([r.seq for r in rows], [96, 97, 98, 99, 100])
        self.assertTrue(all(r.project_id == "proj-a" for r in rows))

        # A restart warms memory with only the newest 10 persisted records, so
        # the old project events can only come from the files.
        reopened = EventLog(self.events_dir, memory_limit=10)
        rows = reopened.query(project_id="proj-a", limit=5)
        self.assertEqual([r.seq for r in rows], [96, 97, 98, 99, 100])

    def test_query_replays_rotated_files_newest_first(self):
        log = EventLog(self.events_dir, memory_limit=5, rotate_bytes=200)
        for i in range(60):
            project = "proj-a" if i < 10 else "proj-b"
            log.append("task.updated", {"i": i, "pad": "x" * 120},
                       project_id=project)
        self.assertTrue(list(self.events_dir.glob("events-*.jsonl")))

        rows = log.query(project_id="proj-a", limit=3)
        self.assertEqual([r.seq for r in rows], [8, 9, 10])

    def test_query_scan_budget_bounds_file_replay(self):
        log = EventLog(self.events_dir, memory_limit=5)
        for i in range(50):
            project = "proj-a" if i < 10 else "proj-b"
            log.append("task.updated", {"i": i}, project_id=project)

        # Only the newest 5 file records are inspected: all proj-b.
        log.query_max_scan_records = 5
        self.assertEqual(log.query(project_id="proj-a", limit=3), [])
        # A byte budget that cannot cover a full line yields no match either.
        log.query_max_scan_records = 100_000
        log.query_max_scan_bytes = 5
        self.assertEqual(log.query(project_id="proj-a", limit=3), [])
        # With the default budget the old project events are found again.
        log.query_max_scan_bytes = 16 * 1024 * 1024
        rows = log.query(project_id="proj-a", limit=3)
        self.assertEqual([r.seq for r in rows], [8, 9, 10])

    def test_since_and_tail_semantics_unchanged(self):
        log = EventLog(self.events_dir)
        for i in range(5):
            log.append("test.event", {"i": i})
        self.assertEqual([r.seq for r in log.tail(3)], [3, 4, 5])
        self.assertEqual([r.seq for r in log.since(2)], [3, 4, 5])
        self.assertEqual(log.since(5), [])


if __name__ == "__main__":
    unittest.main()

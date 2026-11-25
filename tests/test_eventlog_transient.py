"""Transient/streaming events: broadcast live, never persisted, never replayed.

Audit section 3 item 4: ``chat.delta`` / ``agent.chat.delta`` (and every
``*.delta`` producer variant) are ephemeral.  A restart must not replay them,
and a row that somehow reached persistence must be skipped explicitly (and
counted) instead of handing the UI half a message.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stackflowos.config import default_root
from stackflowos.l2.eventlog import EventLog
from stackflowos.storage import SqliteBackend

REPO = default_root()


def _row(seq: int, event_type: str, **data) -> dict:
    return {"seq": seq, "id": f"ev-{seq}", "type": event_type, "source": "sys",
            "ts": 1_000 + seq, "data": data}


class TransientClassificationTests(unittest.TestCase):
    def test_delta_suffix_and_ephemeral_types_are_transient(self):
        for name in ("chat.delta", "agent.chat.delta", "worker.chat.delta",
                     "tool.exec.delta", "stream.chunk"):
            self.assertTrue(EventLog.is_transient(name), name)
        for name in ("delta", "chat.delta.done", "sys.booted",
                     "lease.acquired", ""):
            self.assertFalse(EventLog.is_transient(name), name)


class TransientPersistenceTests(unittest.TestCase):
    def setUp(self):
        base = REPO / ".stackflowos" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(base))
        self.dir = Path(self._tmp.name)
        self.events_dir = self.dir / "events"

    def tearDown(self):
        self._tmp.cleanup()

    def test_delta_suffix_is_broadcast_but_not_persisted(self):
        log = EventLog(self.events_dir)
        queue = log.subscribe()
        delta = log.append("worker.chat.delta", {"text": "half a sentence"})
        durable = log.append("sys.booted", {})
        self.assertEqual((delta.seq, durable.seq), (1, 2))
        # Live subscribers still receive it (that is the UI streaming path).
        self.assertEqual(queue.get_nowait().seq, 1)
        self.assertEqual(queue.get_nowait().seq, 2)
        lines = (self.events_dir / "events.jsonl").read_text(
            "utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("sys.booted", lines[0])

        reopened = EventLog(self.events_dir)
        self.assertEqual(reopened.last_seq, 2)
        self.assertEqual([r.type for r in reopened.since(0)], ["sys.booted"])

    def test_injected_transient_rows_are_skipped_and_counted(self):
        self.events_dir.mkdir(parents=True)
        rows = [_row(1, "chat.delta", text="a"),
                _row(2, "sys.booted"),
                _row(3, "agent.chat.delta", text="b"),
                _row(4, "task.created", task_id="t-1")]
        (self.events_dir / "events.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), "utf-8")

        log = EventLog(self.events_dir)
        # The sequence number is still consumed (restart must not reissue it).
        self.assertEqual(log.last_seq, 4)
        self.assertEqual([(r.seq, r.type) for r in log.since(0)],
                         [(2, "sys.booted"), (4, "task.created")])
        self.assertEqual(log.health()["replay_skipped_transient"], 2)

        # Same skip semantics for the query path (fresh log: separate counter).
        fresh = EventLog(self.events_dir)
        self.assertEqual([(r.seq, r.type) for r in fresh.query()],
                         [(2, "sys.booted"), (4, "task.created")])
        self.assertEqual(fresh.health()["replay_skipped_transient"], 2)

    def test_sqlite_transient_rows_are_skipped(self):
        backend = SqliteBackend(self.dir / "sfo.db")
        self.addCleanup(backend.close)
        backend.append("events", _row(1, "chat.delta", text="x"))
        backend.append("events", _row(2, "sys.booted"))

        log = EventLog(self.events_dir, backend=backend)
        self.assertEqual(log.last_seq, 2)
        self.assertEqual([(r.seq, r.type) for r in log.since(0)],
                         [(2, "sys.booted")])
        self.assertEqual(log.health()["replay_skipped_transient"], 1)

    def test_health_reports_the_skip_counter(self):
        log = EventLog(self.events_dir)
        self.assertEqual(log.health()["replay_skipped_transient"], 0)
        log.append("chat.delta", {"text": "live only"})
        self.assertEqual(log.health()["replay_skipped_transient"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

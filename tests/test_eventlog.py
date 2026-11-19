import asyncio
import tempfile
import unittest
from pathlib import Path

from stackflowos.config import default_root
from stackflowos.l2.eventlog import EventLog
from stackflowos.protocol import EventRecord

REPO = default_root()


class EventLogTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.dir = Path(self._tmp.name)
        self.events_dir = self.dir / "events"
        self.log = EventLog(self.events_dir)

    def tearDown(self):
        self._tmp.cleanup()


class EventLogTests(EventLogTestBase):
    def test_seq_is_monotonic_and_since_tail(self):
        for i in range(5):
            record = self.log.append("test.event", {"i": i})
            self.assertEqual(record.seq, i + 1)
        self.assertEqual(self.log.last_seq, 5)
        self.assertEqual([r.seq for r in self.log.tail(3)], [3, 4, 5])
        self.assertEqual([r.seq for r in self.log.since(2)], [3, 4, 5])
        self.assertEqual([r.seq for r in self.log.since(4, limit=1)], [5])
        self.assertEqual(self.log.since(5), [])

    def test_persistence_across_reopen(self):
        for i in range(3):
            self.log.append("test.event", {"i": i})
        reopened = EventLog(self.events_dir)
        self.assertEqual(reopened.last_seq, 3)
        self.assertEqual([r.seq for r in reopened.tail(10)], [1, 2, 3])
        self.assertEqual([r.data["i"] for r in reopened.since(0)], [0, 1, 2])

    def test_rotation_and_recovery(self):
        log = EventLog(self.events_dir, rotate_bytes=300)
        for i in range(12):
            log.append("test.event", {"i": i, "pad": "x" * 200})
        rotated = sorted(p.name for p in self.events_dir.glob("events-*.jsonl"))
        self.assertTrue(rotated, "expected at least one rotated file")
        self.assertEqual(log.last_seq, 12)

        reopened = EventLog(self.events_dir, memory_limit=3)
        self.assertEqual(reopened.last_seq, 12)
        # Memory only holds the last 3; the rest must be replayed from files.
        self.assertEqual([r.seq for r in reopened.since(0)], list(range(1, 13)))
        self.assertEqual([r.seq for r in reopened.tail(10)], [10, 11, 12])

    def test_truncated_last_line_is_ignored(self):
        self.log.append("test.event", {"i": 1})
        self.log.append("test.event", {"i": 2})
        with open(self.events_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write('{"seq": 99, "type": "cut')
        reopened = EventLog(self.events_dir)
        self.assertEqual(reopened.last_seq, 2)
        self.assertEqual(reopened.append("test.event", {}).seq, 3)

    def test_ephemeral_events_are_not_persisted(self):
        queue = self.log.subscribe()
        ephemeral = self.log.append("chat.delta", {"text": "hi"})
        persisted = self.log.append("sys.booted", {})
        self.assertEqual((ephemeral.seq, persisted.seq), (1, 2))
        self.assertEqual(queue.get_nowait().seq, 1)
        self.assertEqual(queue.get_nowait().seq, 2)
        # Durable file only contains the non-ephemeral record.
        lines = (self.events_dir / "events.jsonl").read_text("utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("sys.booted", lines[0])

        reopened = EventLog(self.events_dir)
        self.assertEqual(reopened.last_seq, 2)
        self.assertEqual([r.type for r in reopened.since(0)], ["sys.booted"])

    def test_append_record_reassigns_seq_but_keeps_id_and_ts(self):
        incoming = EventRecord(seq=0, type="unit.registered", source="coder.1",
                               ts=1234567890, data={"work_id": "coder.1"},
                               id="ev-fixed")
        stored = self.log.append_record(incoming)
        self.assertEqual(stored.seq, 1)
        self.assertEqual(stored.id, "ev-fixed")
        self.assertEqual(stored.ts, 1234567890)
        self.assertEqual(self.log.last_seq, 1)

    def test_since_project_filter(self):
        self.log.append("a", {}, project_id="p-1")
        self.log.append("b", {}, project_id="p-2")
        self.log.append("c", {}, project_id="p-1")
        self.assertEqual([r.type for r in self.log.since(0, project_id="p-1")],
                         ["a", "c"])
        self.assertEqual([r.type for r in self.log.since(1, project_id="p-2")],
                         ["b"])

    def test_slow_subscriber_does_not_block_and_drops_oldest(self):
        self.log.subscriber_queue_size = 3
        queue = self.log.subscribe()
        for i in range(10):
            self.log.append("stream.chunk", {"i": i})
        self.assertEqual(self.log.last_seq, 10)
        self.assertEqual(queue.qsize(), 3)
        kept = [queue.get_nowait().data["i"] for _ in range(3)]
        self.assertEqual(kept, [7, 8, 9])

    def test_unsubscribe_stops_delivery(self):
        queue = self.log.subscribe()
        self.log.unsubscribe(queue)
        self.log.append("test.event", {})
        self.assertEqual(queue.qsize(), 0)
        self.log.unsubscribe(queue)  # idempotent


class EventLogAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.events_dir = Path(self._tmp.name) / "events"
        self.log = EventLog(self.events_dir)

    async def asyncTearDown(self):
        await self.log.close()
        self._tmp.cleanup()

    async def test_subscriber_receives_records(self):
        queue = self.log.subscribe()
        record = self.log.append("unit.registered", {"work_id": "coder.1"})
        received = await asyncio.wait_for(queue.get(), timeout=1.0)
        self.assertEqual(received.seq, record.seq)
        self.assertEqual(received.type, "unit.registered")
        self.assertEqual(received.data["work_id"], "coder.1")
        await self.log.close()
        queue_after = self.log.subscribe()  # a fresh log state: no broadcasts
        self.assertEqual(queue_after.qsize(), 0)

    async def test_close_is_idempotent(self):
        await self.log.close()
        await self.log.close()


if __name__ == "__main__":
    unittest.main()

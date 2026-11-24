"""DIST: event-log rotation/archival, cross-archive replay and backends."""

from __future__ import annotations

import gzip
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from stackflowos.config import default_root
from stackflowos.l2.eventlog import EventLog
from stackflowos.storage import SqliteBackend

REPO = default_root()


class _Base(unittest.TestCase):
    def setUp(self):
        base = REPO / ".stackflowos" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(base))
        self.dir = Path(self._tmp.name)
        self.events_dir = self.dir / "events"
        self._env_backup = {
            key: os.environ.get(key) for key in
            ("SFO_EVENTLOG_ROTATE_BYTES", "SFO_EVENTLOG_ROTATE_KEEP",
             "SFO_STORAGE_BACKEND", "SFO_STORAGE_SQLITE_PATH")
        }
        for key in self._env_backup:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def archives(self) -> list[Path]:
        return sorted(self.events_dir.glob("events-*.jsonl.gz"))


class CompressedRotationTests(_Base):
    def test_small_threshold_rotates_to_gzip_archives(self):
        log = EventLog(self.events_dir, rotation_bytes=300, rotate_keep=10)
        for i in range(15):
            log.append("test.event", {"i": i, "pad": "x" * 200})
        archives = self.archives()
        self.assertTrue(archives, "expected compressed archives")
        # The active file exists again and only contains the newest tail.
        self.assertTrue((self.events_dir / "events.jsonl").exists())
        for archive in archives:
            with gzip.open(archive, "rt", encoding="utf-8") as fh:
                for line in fh:
                    json.loads(line)  # every archive line is valid JSON
        self.assertEqual(log.last_seq, 15)

    def test_reopen_replays_across_archives(self):
        log = EventLog(self.events_dir, rotation_bytes=250, rotate_keep=50,
                       memory_limit=3)
        for i in range(20):
            log.append("task.updated", {"i": i}, project_id="p-1")
        reopened = EventLog(self.events_dir, memory_limit=5)
        self.assertEqual(reopened.last_seq, 20)
        self.assertEqual([r.seq for r in reopened.since(0)], list(range(1, 21)))
        self.assertEqual([r.data["i"] for r in reopened.since(10)],
                         list(range(10, 20)))
        self.assertEqual([r.seq for r in reopened.tail(5)], [16, 17, 18, 19, 20])
        # Query scans newest archives first and stops at the limit.
        rows = reopened.query(types=["task"], limit=3)
        self.assertEqual([r.seq for r in rows], [18, 19, 20])

    def test_keep_prunes_oldest_archives(self):
        log = EventLog(self.events_dir, rotation_bytes=200, rotate_keep=2)
        for i in range(30):
            log.append("test.event", {"i": i, "pad": "y" * 120})
        self.assertLessEqual(len(self.archives()), 2)
        # Only the most recent history survives; the sequence stays monotonic.
        remaining = EventLog(self.events_dir, memory_limit=1).since(0, limit=1000)
        self.assertTrue(remaining)
        self.assertEqual(remaining[-1].seq, 30)
        self.assertTrue(all(r.seq <= 30 for r in remaining))

    def test_env_overrides_select_compressed_rotation(self):
        os.environ["SFO_EVENTLOG_ROTATE_BYTES"] = "200"
        os.environ["SFO_EVENTLOG_ROTATE_KEEP"] = "3"
        log = EventLog(self.events_dir)
        self.assertTrue(log.compress_archives)
        self.assertEqual(log.rotate_bytes, 200)
        self.assertEqual(log.rotate_keep, 3)
        for i in range(10):
            log.append("test.event", {"i": i, "pad": "z" * 100})
        self.assertTrue(self.archives())

    def test_legacy_rotate_bytes_stays_uncompressed_and_unpruned(self):
        # Compatibility path for existing callers/tests: explicit rotate_bytes
        # keeps the historical *.jsonl archives.
        log = EventLog(self.events_dir, rotate_bytes=200)
        for i in range(12):
            log.append("test.event", {"i": i, "pad": "q" * 100})
        plain = list(self.events_dir.glob("events-*.jsonl"))
        self.assertTrue(plain)
        self.assertFalse(self.archives())

    def test_keep_zero_preserves_seq_after_restart(self):
        log = EventLog(self.events_dir, rotation_bytes=200, rotate_keep=0)
        for i in range(10):
            log.append("test.event", {"i": i, "pad": "k" * 80})
        # Force the tail out too, then prune it (keep=0): no archive and no
        # active file remain, so only the durable watermark can save seq.
        log._rotate_compressed()
        self.assertFalse(self.archives())
        self.assertFalse((self.events_dir / "events.jsonl").exists())
        reopened = EventLog(self.events_dir)
        self.assertEqual(reopened.last_seq, 10)
        self.assertEqual(reopened.append("test.event", {}).seq, 11)

    def test_corrupt_archive_is_skipped_and_recorded(self):
        log = EventLog(self.events_dir, rotation_bytes=250, rotate_keep=10,
                       memory_limit=1)
        for i in range(10):
            log.append("test.event", {"i": i, "pad": "c" * 80})
        bad = self.events_dir / "events-999-1.jsonl.gz"
        bad.write_bytes(b"this is not gzip")
        # Reopen: loading memory must tolerate the corrupt archive.
        reopened = EventLog(self.events_dir, memory_limit=2)
        rows = reopened.since(0, limit=1000)
        self.assertTrue(rows)
        self.assertNotIn(999, {r.seq for r in rows})
        # query also skips it and records the error in health.
        reopened.query(types=["test.event"], limit=10)
        health = reopened.health()
        self.assertGreaterEqual(health["corrupt_archives"], 1)
        self.assertGreaterEqual(health["rotations"], 1)

    def test_health_reports_rotation_state(self):
        log = EventLog(self.events_dir, rotation_bytes=200, rotate_keep=5)
        for i in range(10):
            log.append("test.event", {"i": i, "pad": "h" * 150})
        health = log.health()
        self.assertGreaterEqual(health["rotations"], 1)
        self.assertGreater(health["archive_bytes"], 0)
        self.assertTrue(health["writable"])
        self.assertEqual(health["backend"], "jsonl")


class BackendEventLogTests(_Base):
    def test_sqlite_backend_roundtrip_and_restart(self):
        backend = SqliteBackend(self.dir / "state" / "sfo.db")
        log = EventLog(self.events_dir, backend=backend, memory_limit=3)
        for i in range(10):
            log.append("task.updated", {"i": i}, project_id="p-1")
        self.assertEqual(log.last_seq, 10)
        self.assertFalse(self.archives())
        self.assertFalse((self.events_dir / "events.jsonl").exists())
        reopened = EventLog(self.events_dir, backend=backend, memory_limit=3)
        self.assertEqual(reopened.last_seq, 10)
        self.assertEqual([r.seq for r in reopened.since(0)], list(range(1, 11)))
        self.assertEqual([r.seq for r in reopened.tail(3)], [8, 9, 10])
        self.assertEqual([r.seq for r in reopened.query(types=["task"], limit=4)],
                         [7, 8, 9, 10])
        health = reopened.health()
        self.assertEqual(health["backend"], "sqlite")
        self.assertTrue(health["writable"])

    def test_env_selects_sqlite_backend(self):
        os.environ["SFO_STORAGE_BACKEND"] = "sqlite"
        os.environ["SFO_STORAGE_SQLITE_PATH"] = str(self.dir / "env.db")
        log = EventLog(self.events_dir)
        log.append("sys.booted", {})
        self.assertEqual(log.backend.name, "sqlite")  # type: ignore[union-attr]
        reopened = EventLog(self.events_dir)
        self.assertEqual(reopened.last_seq, 1)
        self.assertEqual([r.type for r in reopened.since(0)], ["sys.booted"])


class ConcurrentRotationTests(_Base):
    def test_concurrent_append_query_subscribe(self):
        log = EventLog(self.events_dir, rotation_bytes=1200, rotate_keep=1000,
                       memory_limit=10)
        log.subscriber_queue_size = 5000
        queue = log.subscribe()
        writers = 4
        per_thread = 100
        errors: list[BaseException] = []
        barrier = threading.Barrier(writers + 2)

        def writer(worker: int) -> None:
            try:
                barrier.wait(timeout=10)
                for i in range(per_thread):
                    log.append("task.updated",
                               {"worker": worker, "i": i, "pad": "w" * 60},
                               project_id=f"p-{worker}")
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        def reader() -> None:
            try:
                barrier.wait(timeout=10)
                for _ in range(80):
                    rows = log.query(types=["task"], limit=50)
                    seqs = [row.seq for row in rows]
                    self.assertEqual(seqs, sorted(seqs))
                    self.assertEqual(len(seqs), len(set(seqs)))
                    since = log.since(0, limit=1000)
                    since_seqs = [row.seq for row in since]
                    self.assertEqual(since_seqs, sorted(set(since_seqs)))
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = ([threading.Thread(target=writer, args=(i,), daemon=True)
                    for i in range(writers)]
                   + [threading.Thread(target=reader, daemon=True)
                      for _ in range(2)])
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertEqual(errors, [], f"concurrency failure: {errors!r}")
        self.assertFalse(any(thread.is_alive() for thread in threads))
        total = writers * per_thread
        self.assertEqual(log.last_seq, total)
        all_rows = log.since(0, limit=total + 10)
        self.assertEqual([r.seq for r in all_rows], list(range(1, total + 1)))
        self.assertEqual(queue.qsize(), total)
        # Broadcasting happens after the seq-assignment lock is released, so
        # concurrent appends can reach a subscriber slightly out of seq order;
        # every record is delivered exactly once.
        received = [queue.get_nowait().seq for _ in range(total)]
        self.assertEqual(sorted(received), list(range(1, total + 1)))
        self.assertTrue(self.archives(), "expected rotations under load")
        log.unsubscribe(queue)


if __name__ == "__main__":
    unittest.main()

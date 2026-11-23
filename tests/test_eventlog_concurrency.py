"""S7: serialise event-log subscriber bookkeeping.

``subscribe``/``unsubscribe``/``close``/``_broadcast`` all touch
``EventLog._subscribers``; the log's public API is callable from worker threads
as well as the event loop, so the list must always be read/written under
``EventLog._lock``.  These tests pin the contract:

* concurrent subscribe/unsubscribe/append never raises ``RuntimeError`` /
  ``ValueError`` and leaves exactly the expected subscriber count;
* a closed log returns a *closed* queue that is not registered, never receives
  events, and does not make callers (the SSE handler) crash;
* unsubscribe is idempotent, including for queues that were never registered;
* a slow/full subscriber still drops its oldest record instead of raising.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path

from stackflowos.config import default_root
from stackflowos.l2.eventlog import EventLog, SubscriberQueue

REPO = default_root()
THREADS = 4
PER_THREAD = 250  # => 1000 subscribe/unsubscribe pairs and 1000 appends


class EventLogConcurrencyTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.events_dir = Path(self._tmp.name) / "events"
        self.log = EventLog(self.events_dir)

    def tearDown(self):
        if not self.log._closed:
            asyncio.run(self.log.close())
        self._tmp.cleanup()


class SubscriberLockTests(EventLogConcurrencyTestBase):
    def test_concurrent_subscribe_unsubscribe_append(self):
        """1000 concurrent subscriptions/appends leave a consistent list."""
        barrier = threading.Barrier(THREADS)
        errors: list[BaseException] = []
        kept: list[asyncio.Queue] = []
        kept_lock = threading.Lock()

        def worker(worker_id: int) -> None:
            try:
                barrier.wait(timeout=10)
                for i in range(PER_THREAD):
                    queue = self.log.subscribe()
                    # Ephemeral type keeps the test off disk while still
                    # exercising seq assignment and live broadcast.
                    self.log.append("stream.chunk",
                                    {"worker": worker_id, "i": i})
                    if i % 2:
                        self.log.unsubscribe(queue)
                        self.log.unsubscribe(queue)  # idempotent
                    else:
                        with kept_lock:
                            kept.append(queue)
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        workers = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(THREADS)]
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join(timeout=60)

        self.assertEqual(errors, [], f"worker raised: {errors!r}")
        self.assertFalse(any(thread.is_alive() for thread in workers))
        self.assertEqual(self.log.last_seq, THREADS * PER_THREAD)
        expected = THREADS * (PER_THREAD // 2)
        self.assertEqual(len(kept), expected)
        self.assertEqual(len(self.log._subscribers), expected)
        # Every surviving subscriber is unique; no duplicates leaked in.
        self.assertEqual(len(set(map(id, self.log._subscribers))), expected)

        # Concurrent teardown also leaves no residue.
        def drain(queues: list[asyncio.Queue]) -> None:
            try:
                for queue in queues:
                    self.log.unsubscribe(queue)
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        chunks = [kept[i::THREADS] for i in range(THREADS)]
        drainers = [threading.Thread(target=drain, args=(chunk,), daemon=True)
                    for chunk in chunks]
        for thread in drainers:
            thread.start()
        for thread in drainers:
            thread.join(timeout=30)
        self.assertEqual(errors, [], f"drain raised: {errors!r}")
        self.assertEqual(self.log._subscribers, [])

    def test_unsubscribe_is_idempotent(self):
        queue = self.log.subscribe()
        self.assertIsInstance(queue, SubscriberQueue)
        self.assertFalse(queue.closed)
        self.log.unsubscribe(queue)
        self.log.unsubscribe(queue)
        self.log.unsubscribe(asyncio.Queue())  # never registered
        self.assertEqual(self.log._subscribers, [])
        remaining = self.log.subscribe()
        self.assertEqual(self.log._subscribers, [remaining])
        self.log.unsubscribe(remaining)

    def test_slow_subscriber_drops_oldest_without_raising(self):
        self.log.subscriber_queue_size = 3
        queue = self.log.subscribe()
        for i in range(10):
            self.log.append("stream.chunk", {"i": i})
        self.assertEqual(queue.qsize(), 3)
        kept = [queue.get_nowait().data["i"] for _ in range(3)]
        self.assertEqual(kept, [7, 8, 9])

    def test_broadcast_snapshot_survives_unsubscribe_during_delivery(self):
        """Unsubscribing while a broadcast is in flight must not disturb it."""
        first = self.log.subscribe()
        second = self.log.subscribe()
        self.log.unsubscribe(first)  # first is removed before the broadcast
        self.log.append("stream.chunk", {"i": 1})
        self.assertEqual(first.qsize(), 0)
        self.assertEqual(second.qsize(), 1)
        self.log.unsubscribe(second)


class ClosedLogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.events_dir = Path(self._tmp.name) / "events"
        self.log = EventLog(self.events_dir)

    async def asyncTearDown(self):
        await self.log.close()
        self._tmp.cleanup()

    async def test_subscribe_after_close_returns_closed_unregistered_queue(self):
        live = self.log.subscribe()
        self.assertFalse(live.closed)
        await self.log.close()
        self.assertEqual(self.log._subscribers, [])

        late = self.log.subscribe()
        self.assertIsInstance(late, SubscriberQueue)
        self.assertTrue(late.closed)
        # The closed queue must not be registered (no leak) and stays empty.
        self.assertEqual(self.log._subscribers, [])
        self.assertEqual(late.qsize(), 0)
        self.assertEqual(live.qsize(), 0)

        # append keeps its existing post-close semantics (seq advances) but
        # never reaches either queue.
        record = self.log.append("stream.chunk", {})
        self.assertEqual(record.seq, 1)
        self.assertEqual(late.qsize(), 0)
        self.assertEqual(live.qsize(), 0)

    async def test_close_is_idempotent(self):
        await self.log.close()
        await self.log.close()
        self.assertTrue(self.log._closed)
        self.assertEqual(self.log._subscribers, [])

    async def test_close_racing_subscribe_leaves_no_subscribers(self):
        stop = threading.Event()
        created = 0
        errors: list[BaseException] = []
        counter_lock = threading.Lock()

        def churn() -> None:
            nonlocal created
            try:
                while not stop.is_set():
                    self.log.subscribe()
                    with counter_lock:
                        created += 1
                    time.sleep(0.001)
            except BaseException as exc:  # pragma: no cover - failure path
                with counter_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=churn, daemon=True)
                   for _ in range(THREADS)]
        for thread in threads:
            thread.start()
        await asyncio.sleep(0.05)
        await self.log.close()
        await asyncio.sleep(0.02)
        stop.set()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [], f"churn raised: {errors!r}")
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertGreater(created, 0)
        self.assertEqual(self.log._subscribers, [])
        self.assertTrue(self.log.subscribe().closed)


if __name__ == "__main__":
    unittest.main()

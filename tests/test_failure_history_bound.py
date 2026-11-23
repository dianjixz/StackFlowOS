"""S8: :class:`FailureManager` keeps its restart history bounded (§6.15).

The restart-budget bookkeeping used to live in an unbounded
``dict[str, list[float]]``: a work_id that kept failing accumulated stamps
forever and every distinct work_id left a key behind.  These tests pin the
new guarantees:

* stale timestamps are dropped and empty entries are removed;
* at most ``max_history_entries`` (default 2048) work_ids are retained;
* the window/budget/quarantine behaviour is unchanged.
"""

from __future__ import annotations

import time
import unittest
from collections import deque

from stackflowos.errors import ValidationError
from stackflowos.kernel import Context
from stackflowos.l2.failure import FailureManager, FailurePolicy
from stackflowos.l2.registry import UnitInfo
from stackflowos.protocol import EventRecord, now_ms


class FakeRegistry:
    """Minimal registry stand-in: returns queued sweep transitions."""

    def __init__(self):
        self.transitions = []
        self.quarantined = []
        self.restarts = []
        self.sweeps = 0

    def sweep(self, now_ms=None):
        self.sweeps += 1
        pending, self.transitions = self.transitions, []
        return pending

    def mark_quarantined(self, work_id, reason):
        self.quarantined.append((work_id, reason))

    def note_restart(self, work_id, *, reason=""):
        self.restarts.append((work_id, reason))


class FakeLeases:
    def __init__(self):
        self.released = []

    def release_holder(self, holder, *, reason="released"):
        self.released.append((holder, reason))
        return []


class CountingEventLog:
    """Event-log stub: counts appends without touching disk.

    The 10k-work_id scenario emits ~20k events; writing them to disk would
    only slow the test down without adding coverage for the bound.
    """

    def __init__(self):
        self.appended = 0

    def append(self, type, data, *, source="sys", project_id=None):
        self.appended += 1
        return EventRecord(seq=self.appended, type=type, source=source,
                           ts=now_ms(), data=dict(data or {}),
                           project_id=project_id)


def make_unit(work_id="coder.1", kind="coder", restart_count=0):
    return UnitInfo(work_id=work_id, kind=kind, status="offline",
                    restart_count=restart_count)


class FailureHistoryBoundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ctx = Context(name="test-s8")
        self.registry = FakeRegistry()
        self.leases = FakeLeases()
        self.log = CountingEventLog()
        self.restarted = []

    async def asyncTearDown(self):
        await self.ctx.dispose()

    def _manager(self, **kwargs):
        return FailureManager(self.ctx, self.registry, self.leases, self.log,
                              **kwargs)

    async def _restart_ok(self, unit, attempt):
        self.restarted.append((unit.work_id, attempt))
        return True

    # -- size bound -------------------------------------------------------
    async def test_ten_thousand_work_ids_are_bounded(self):
        manager = self._manager(restart_cb=self._restart_ok)
        self.assertEqual(manager.max_history_entries, 2048)

        self.registry.transitions = [
            (make_unit(f"coder.{i}"), "offline") for i in range(10_000)
        ]
        results = await manager.tick()

        self.assertEqual(len(results), 10_000)
        self.assertEqual(len(manager._history), manager.max_history_entries)
        self.assertEqual(len(manager._history), 2048)
        for stamps in manager._history.values():
            self.assertTrue(stamps, "empty entries must not survive")

        # A later idle tick keeps the history bounded and does not crash.
        self.registry.transitions = []
        await manager.tick()
        self.assertLessEqual(len(manager._history), 2048)

    async def test_size_cap_evicts_oldest_last_attempt(self):
        manager = self._manager(max_history_entries=3)
        now = time.monotonic()
        manager._history["a"] = deque([now - 100.0])
        manager._history["b"] = deque([now - 50.0])
        manager._history["c"] = deque([now - 25.0])
        manager._history["d"] = deque([now - 10.0])

        evicted = manager._prune_history(now=now)

        self.assertEqual(evicted, 1)
        self.assertEqual(set(manager._history), {"b", "c", "d"})

    async def test_work_id_eviction_uses_newest_attempt(self):
        manager = self._manager(max_history_entries=2)
        now = time.monotonic()
        # "a" started failing long ago but attempted again most recently.
        manager._history["a"] = deque([now - 300.0, now - 1.0])
        manager._history["b"] = deque([now - 200.0])
        manager._history["c"] = deque([now - 100.0])

        evicted = manager._prune_history(now=now)

        self.assertEqual(evicted, 1)
        self.assertEqual(set(manager._history), {"a", "c"})

    # -- stale / empty cleanup -------------------------------------------
    async def test_stale_stamps_and_empty_entries_are_pruned_on_tick(self):
        manager = self._manager()
        now = time.monotonic()
        manager._history["old"] = deque([now - 7200.0])
        manager._history["fresh"] = deque([now - 60.0, now - 30.0])
        manager._history["empty"] = deque()

        self.registry.transitions = []
        await manager.tick()

        self.assertNotIn("old", manager._history)
        self.assertNotIn("empty", manager._history)
        self.assertIn("fresh", manager._history)

    async def test_prune_history_reports_evictions_and_handles_empty(self):
        manager = self._manager()
        self.assertEqual(manager._prune_history(), 0)
        manager._history["old"] = deque([time.monotonic() - 7200.0])
        self.assertEqual(manager._prune_history(), 0)
        self.assertEqual(manager._history, {})

    async def test_min_retention_keeps_recent_stamps_on_tick(self):
        policy = FailurePolicy(kind="coder", window_s=60.0)
        manager = self._manager(policies={"coder": policy})
        now = time.monotonic()
        manager._history["recent"] = deque([now - 3599.0])

        self.registry.transitions = []
        await manager.tick()

        self.assertIn("recent", manager._history)
        self.assertEqual(manager._history_retention_s(), 3600.0)

    async def test_wide_policy_window_extends_retention(self):
        policy = FailurePolicy(kind="coder", window_s=7200.0)
        manager = self._manager(policies={"coder": policy})
        now = time.monotonic()
        manager._history["slow"] = deque([now - 4000.0])

        manager._prune_history(now=now)

        self.assertEqual(manager._history_retention_s(), 7200.0)
        self.assertIn("slow", manager._history)

    # -- windowed attempts unchanged -------------------------------------
    async def test_attempts_in_window_filters_and_drops_empty_keys(self):
        manager = self._manager()
        now = time.monotonic()
        manager._history["coder.1"] = deque(
            [now - 900.0, now - 100.0, now - 10.0])

        attempts = manager._attempts_in_window("coder.1", 300.0)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(list(manager._history["coder.1"]), attempts)

        manager._history["coder.2"] = deque([now - 5000.0])
        self.assertEqual(manager._attempts_in_window("coder.2", 300.0), [])
        self.assertNotIn("coder.2", manager._history)

        self.assertEqual(manager._attempts_in_window("never.seen", 300.0), [])
        self.assertNotIn("never.seen", manager._history)

    async def test_restart_budget_and_quarantine_do_not_regress(self):
        policy = FailurePolicy(kind="coder", action="restart",
                               max_restarts=2, window_s=300.0)
        manager = self._manager(policies={"coder": policy},
                                restart_cb=self._restart_ok)
        unit = make_unit()

        actions = []
        for _ in range(3):
            self.registry.transitions = [(unit, "offline")]
            results = await manager.tick()
            actions.append(results[0]["action"])

        self.assertEqual(actions, ["restart", "restart", "quarantined"])
        self.assertEqual(self.restarted, [("coder.1", 1), ("coder.1", 2)])
        self.assertEqual(
            len(manager._attempts_in_window("coder.1", 300.0)), 2)
        self.assertEqual(self.registry.quarantined,
                         [("coder.1", "restart_budget_exhausted")])

        # Fresh entries survive normal pruning on an idle tick.
        self.registry.transitions = []
        await manager.tick()
        self.assertIn("coder.1", manager._history)
        self.assertEqual(len(manager._history["coder.1"]), 2)

    # -- configuration ----------------------------------------------------
    async def test_invalid_max_history_entries_rejected(self):
        with self.assertRaises(ValidationError):
            self._manager(max_history_entries=0)
        with self.assertRaises(ValidationError):
            self._manager(max_history_entries="not-a-number")


if __name__ == "__main__":
    unittest.main()

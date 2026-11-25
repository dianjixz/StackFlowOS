"""Priority dispatch queue: ordering, parking, drain, cancel, persistence."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from stackflowos.agents.queue import (DispatchQueue, QueueEntry, QueueFull,
                                      compute_priority, task_score)
from stackflowos.agents.supervisor import AgentSupervisor
from stackflowos.config import AgentsCfg
from stackflowos.errors import Conflict
from stackflowos.kernel.context import Context

TMP_ROOT = Path(__file__).resolve().parent.parent / ".stackflowos" / "tmp"
_PID_BASE = 6_000_000


class FakeAllocator:
    def __init__(self) -> None:
        self.counter = 0
        self.allocated: list[str] = []

    def new_work_id(self, kind: str) -> str:
        self.counter += 1
        return f"{kind}.{self.counter}"

    def allocate(self, work_id: str):
        self.allocated.append(work_id)
        return SimpleNamespace(rpc=f"ipc://{work_id}.rpc",
                               events=f"ipc://{work_id}.events")

    def release(self, work_id: str) -> None:
        pass


class FakeRegistry:
    def get(self, work_id: str):
        return SimpleNamespace(status="online")


class FakeEventLog:
    def __init__(self) -> None:
        self.records: list[tuple[tuple, dict]] = []

    def append(self, *args, **kwargs):
        self.records.append((args, kwargs))

    def types(self) -> list[str]:
        return [record[0][0] for record in self.records]

    def payload(self, event_type: str) -> list[dict]:
        return [record[0][1] for record in self.records
                if record[0][0] == event_type]


class FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = 0

    def poll(self):
        return self.returncode

    def terminate(self) -> None:  # pragma: no cover - already exited
        self.returncode = 0


class DispatchQueueUnitTests(unittest.TestCase):
    def test_priority_derivation(self) -> None:
        self.assertEqual(compute_priority("coder", None, 90), 90)
        self.assertEqual(compute_priority("coder", None, 1000), 100)
        self.assertEqual(compute_priority("coder", None, -5), 0)
        urgent = compute_priority("reviewer", {"priority": 3})
        relaxed = compute_priority("reviewer", {"priority": 0})
        self.assertGreater(urgent, relaxed)
        self.assertEqual(task_score({"urgency": "urgent"}), 60)
        self.assertEqual(task_score({"priority": 2}), 45)
        # An explicit priority wins over the derived value.
        self.assertEqual(compute_priority("lead", {"priority": 0}, 5), 5)

    def test_queue_orders_by_priority_then_fifo(self) -> None:
        queue = DispatchQueue(max_size=4)
        low = queue.put(QueueEntry(agent_id="a-low", role="coder", priority=10))
        high = queue.put(QueueEntry(agent_id="a-high", role="tester", priority=90))
        same_first = queue.put(QueueEntry(agent_id="a-1", role="coder", priority=50))
        same_second = queue.put(QueueEntry(agent_id="a-2", role="coder", priority=50))
        self.assertEqual([item.id for item in queue.entries()],
                         [high.id, same_first.id, same_second.id, low.id])
        snapshot = queue.snapshot()
        self.assertEqual([item["position"] for item in snapshot], [1, 2, 3, 4])
        self.assertEqual(queue.pop().agent_id, "a-high")
        self.assertEqual(queue.pop().agent_id, "a-1")

    def test_queue_bound_and_remove(self) -> None:
        queue = DispatchQueue(max_size=1)
        entry = queue.put(QueueEntry(agent_id="a", role="coder"))
        with self.assertRaises(QueueFull):
            queue.put(QueueEntry(agent_id="b", role="coder"))
        self.assertIs(queue.remove_agent("a"), entry)
        self.assertEqual(len(queue), 0)
        self.assertIsNone(queue.pop())

    def test_queue_persistence_roundtrip(self) -> None:
        queue = DispatchQueue(max_size=2)
        queue.put(QueueEntry(agent_id="a", role="coder", priority=70,
                             project_id="p-1", task_id="t-1"))
        payload = queue.to_payload()
        restored = DispatchQueue(max_size=2)
        restored.load(payload)
        self.assertEqual(len(restored), 1)
        entry = restored.entries()[0]
        self.assertEqual((entry.agent_id, entry.priority, entry.task_id),
                         ("a", 70, "t-1"))
        # Malformed payloads never raise.
        restored.load({"entries": [{"id": "", "agent_id": ""}, "junk", 3]})
        self.assertEqual(len(restored), 0)


class _SupervisorHelpers:
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.allocator = FakeAllocator()
        self.eventlog = FakeEventLog()
        self.pids = iter(range(_PID_BASE, _PID_BASE + 100))
        self.supervisors: list[AgentSupervisor] = []

    async def asyncTearDown(self) -> None:
        for sup in self.supervisors:
            await sup.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, **cfg) -> AgentSupervisor:
        config = AgentsCfg(**cfg)
        sup = AgentSupervisor(
            Context(), registry=FakeRegistry(), allocator=self.allocator,
            eventlog=self.eventlog, config=config, sys_rpc="ipc://sys.rpc",
            state_dir=self.state, root=self.root,
            popen_factory=lambda command, kwargs: FakeProc(next(self.pids)),
            spawn_timeout_s=1.0)
        self.supervisors.append(sup)
        return sup

    async def _wait_until(self, predicate, timeout: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("condition was not reached in time")
            await asyncio.sleep(0.01)


class SupervisorQueueTests(_SupervisorHelpers, unittest.IsolatedAsyncioTestCase):
    async def test_capacity_queues_and_priority_wins_the_next_slot(self) -> None:
        sup = self._make(max_workers=1, queue_max=4)
        running = await sup.spawn("coder", "p-1", task_id="t-1")
        low = await sup.spawn("tester", "p-1", task_id="t-2", priority=10)
        high = await sup.spawn("reviewer", "p-1", task_id="t-3", priority=95)
        self.assertEqual((low.status, high.status), ("queued", "queued"))
        self.assertEqual(sup.queue_snapshot()["count"], 2)
        await sup.kill(running.id)
        await self._wait_until(lambda: high.status != "queued")
        self.assertEqual(high.status, "idle")
        self.assertEqual(low.status, "queued")
        self.assertEqual(len(sup.queue), 1)
        self.assertIn("agent.queued", self.eventlog.types())
        self.assertIn("agent.dequeued", self.eventlog.types())

    async def test_finish_and_restart_paths_backfill_the_queue(self) -> None:
        sup = self._make(max_workers=1, queue_max=2)
        running = await sup.spawn("coder", "p-1")
        waiting = await sup.spawn("tester", "p-1")
        sup.finish(running.id, "done")
        await self._wait_until(lambda: waiting.status != "queued")
        self.assertEqual(waiting.status, "idle")

    async def test_kill_of_queued_agent_removes_it(self) -> None:
        sup = self._make(max_workers=1)
        running = await sup.spawn("coder", "p-1")
        queued = await sup.spawn("tester", "p-1")
        self.assertTrue(await sup.kill(queued.id, reason="not_needed"))
        self.assertEqual(queued.status, "dead")
        self.assertEqual(len(sup.queue), 0)
        self.assertEqual(sup.queue_snapshot()["count"], 0)
        await sup.kill(running.id)

    async def test_cancel_queued_by_queue_id(self) -> None:
        sup = self._make(max_workers=1)
        running = await sup.spawn("coder", "p-1")
        queued = await sup.spawn("tester", "p-1")
        queue_id = sup.queue_snapshot()["queued"][0]["id"]
        result = sup.cancel_queued(queue_id, reason="changed_my_mind")
        self.assertTrue(result["canceled"])
        self.assertEqual(queued.status, "dead")
        self.assertEqual(len(sup.queue), 0)
        self.assertTrue(self.eventlog.payload("agent.dequeued")[0]["canceled"])
        await sup.kill(running.id)

    async def test_queue_snapshot_carries_status_and_capacity(self) -> None:
        sup = self._make(max_workers=1, queue_max=3)
        await sup.spawn("coder", "p-1")
        queued = await sup.spawn("reviewer", "p-1", task_id="t-2", priority=88)
        snapshot = sup.queue_snapshot(project_id="p-1")
        self.assertEqual(snapshot["count"], 1)
        self.assertEqual(snapshot["running"], 1)
        self.assertEqual(snapshot["max_workers"], 1)
        self.assertEqual(snapshot["queue_max"], 3)
        self.assertEqual(snapshot["available"], 0)
        item = snapshot["queued"][0]
        self.assertEqual(item["status"], "queued")
        self.assertEqual(item["agent_id"], queued.id)
        self.assertEqual(item["priority"], 88)
        self.assertIn("工人", item["display_name"])
        self.assertEqual(sup.queue_snapshot(project_id="other")["count"], 0)

    async def test_spawn_respects_task_dependencies(self) -> None:
        sup = self._make(max_workers=2)
        statuses = {"t-1": "doing", "t-2": "todo"}
        sup.task_lookup = lambda task_id: SimpleNamespace(
            id=task_id, status=statuses.get(task_id, "done"),
            deps=["t-1"] if task_id == "t-2" else [])
        with self.assertRaises(Conflict) as caught:
            await sup.spawn("coder", "p-1", task_id="t-2")
        self.assertEqual(caught.exception.detail["blocked_by"][0]["task_id"],
                         "t-1")
        # Dependent task becomes dispatchable once the dep is done.
        statuses["t-1"] = "done"
        agent = await sup.spawn("coder", "p-1", task_id="t-2")
        self.assertEqual(agent.task_id, "t-2")

    async def test_task_priority_drives_queued_priority(self) -> None:
        sup = self._make(max_workers=1)
        tasks = {
            "t-1": SimpleNamespace(id="t-1", status="todo", deps=[], priority=0),
            "t-2": SimpleNamespace(id="t-2", status="todo", deps=[], priority=3),
        }
        sup.task_lookup = lambda task_id: tasks[task_id]
        await sup.spawn("coder", "p-1", task_id="t-1")
        await sup.spawn("coder", "p-1", task_id="t-2")
        snapshot = sup.queue_snapshot()
        self.assertEqual(snapshot["queued"][0]["task_id"], "t-2")
        self.assertGreater(snapshot["queued"][0]["priority"], 50)

    async def test_save_load_persists_queue_and_limits(self) -> None:
        sup = self._make(max_workers=1, queue_max=5)
        running = await sup.spawn("coder", "p-1")
        queued = await sup.spawn("tester", "p-1", task_id="t-2")
        sup.save()
        restored = self._make(max_workers=1, queue_max=5)
        restored.load()
        snapshot = restored.queue_snapshot()
        self.assertEqual(snapshot["count"], 1)
        self.assertEqual(snapshot["queued"][0]["agent_id"], queued.id)
        self.assertEqual(restored.get(queued.id).status, "queued")
        self.assertEqual(restored.get(running.id).status, "idle")
        self.assertEqual(restored.stats()["running"], 1)
        await sup.kill(running.id)

    # -- boot resume (audit §4: a restored queue must be drained) ---------
    async def test_resume_queue_starts_persisted_entries_after_restart(
            self) -> None:
        sup = self._make(max_workers=1, queue_max=4)
        running = await sup.spawn("coder", "p-1", task_id="t-1")
        queued = await sup.spawn("tester", "p-1", task_id="t-2")
        self.assertEqual(queued.status, "queued")
        sup.save()

        # The restarted hub has one more slot than the old one, so the queue
        # restored from agents.json must start without any other trigger.
        restored = self._make(max_workers=2, queue_max=4)
        restored.load()
        waiting = restored.get(queued.id)
        self.assertEqual(waiting.status, "queued")
        self.assertEqual(len(restored.queue), 1)

        result = restored.resume_queue()
        self.assertEqual(result, {"queued": 1, "available": 1,
                                  "scheduled": True})
        await self._wait_until(lambda: waiting.status != "queued")
        self.assertEqual(waiting.status, "idle")
        self.assertEqual(len(restored.queue), 0)
        # Exactly one explicit boot event records what the snapshot held.
        restored_events = self.eventlog.payload("agent.queue_restored")
        self.assertEqual(len(restored_events), 1)
        self.assertEqual(restored_events[0]["queued"], 1)
        self.assertEqual(restored_events[0]["available"], 1)
        self.assertTrue(restored_events[0]["scheduled"])
        self.assertIn("agent.dequeued", self.eventlog.types())
        self.assertEqual(running.status, "idle")

    async def test_resume_queue_without_slot_waits_for_a_release(self) -> None:
        sup = self._make(max_workers=1, queue_max=2)
        running = await sup.spawn("coder", "p-1", task_id="t-1")
        queued = await sup.spawn("tester", "p-1", task_id="t-2")
        sup.save()

        restored = self._make(max_workers=1, queue_max=2)
        restored.load()
        result = restored.resume_queue()
        self.assertEqual(result["queued"], 1)
        self.assertEqual(result["available"], 0)
        self.assertFalse(result["scheduled"])
        waiting = restored.get(queued.id)
        self.assertEqual(waiting.status, "queued")
        events = self.eventlog.payload("agent.queue_restored")
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["scheduled"])

        # The restored queue was not lost: the next slot release drains it.
        restored.finish(restored.get(running.id).id, "done")
        await self._wait_until(lambda: waiting.status != "queued")
        self.assertEqual(waiting.status, "idle")

    async def test_resume_queue_with_empty_queue_is_a_noop(self) -> None:
        sup = self._make(max_workers=2)
        result = sup.resume_queue()
        self.assertEqual(result, {"queued": 0, "available": 2,
                                  "scheduled": False})
        self.assertNotIn("agent.queue_restored", self.eventlog.types())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class SupervisorTimeoutReaperTests(_SupervisorHelpers,
                                   unittest.IsolatedAsyncioTestCase):
    """Hub-side safety net: reclaim a wedged worker and re-dispatch its task."""

    async def test_reap_stalled_kills_and_requeues(self) -> None:
        sup = self._make(max_workers=1, queue_max=2, task_timeout_s=10)
        requeued: list[str] = []
        tasks = {"t-1": SimpleNamespace(id="t-1", status="doing", deps=[])}
        sup.task_lookup = lambda task_id: tasks[task_id]
        sup.task_requeue = lambda task_id: requeued.append(task_id)
        agent = await sup.spawn("coder", "p-1", task_id="t-1")
        now = int(agent.last_seen) + 11_000

        reaped = await sup.reap_stalled(now=now)
        self.assertEqual(len(reaped), 1)
        info = reaped[0]
        self.assertEqual(info["task_id"], "t-1")
        self.assertTrue(info.get("reassigned"))
        self.assertEqual(requeued, ["t-1"])
        self.assertEqual(agent.status, "blocked")
        self.assertEqual(agent.error_code, "TASK_TIMEOUT")
        self.assertIn("timed out", agent.error or "")
        # The replacement parked on the queue (max_workers is still 1).
        self.assertEqual(sup.queue_snapshot()["count"], 1)
        self.assertEqual(sup.queue_snapshot()["queued"][0]["reason"],
                         "timeout_retry")
        self.assertIn("agent.timeout", self.eventlog.types())
        self.assertIn("agent.blocked", self.eventlog.types())

    async def test_reap_skips_agents_inside_the_deadline(self) -> None:
        sup = self._make(max_workers=1, task_timeout_s=100)
        agent = await sup.spawn("coder", "p-1", task_id="t-1")
        self.assertEqual(await sup.reap_stalled(now=int(agent.last_seen) + 50),
                         [])
        self.assertEqual(agent.status, "idle")

    async def test_reap_does_not_revive_a_blocked_task(self) -> None:
        sup = self._make(max_workers=1, task_timeout_s=10)
        requeued: list[str] = []
        tasks = {"t-1": SimpleNamespace(id="t-1", status="blocked", deps=[])}
        sup.task_lookup = lambda task_id: tasks[task_id]
        sup.task_requeue = lambda task_id: requeued.append(task_id)
        agent = await sup.spawn("coder", "p-1", task_id="t-1")
        reaped = await sup.reap_stalled(now=int(agent.last_seen) + 11_000)
        self.assertEqual(len(reaped), 1)
        self.assertFalse(reaped[0].get("reassigned"))
        self.assertEqual(requeued, [])
        self.assertEqual(sup.queue_snapshot()["count"], 0)

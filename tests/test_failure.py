import asyncio
import tempfile
import unittest
from pathlib import Path

from stackflowos.config import default_root
from stackflowos.errors import ValidationError
from stackflowos.kernel import Context
from stackflowos.l2.eventlog import EventLog
from stackflowos.l2.failure import FailureManager, FailurePolicy
from stackflowos.l2.leases import LeaseManager
from stackflowos.l2.registry import UnitInfo, UnitRegistry
from stackflowos.protocol import now_ms

REPO = default_root()


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


def make_unit(work_id="coder.1", kind="coder", restart_count=0):
    return UnitInfo(work_id=work_id, kind=kind, status="offline",
                    restart_count=restart_count)


class FailureManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.dir = Path(self._tmp.name)
        self.ctx = Context(name="test")
        self.events = []
        self.ctx.on_any(lambda mode, name, payload: self.events.append((name, payload)))
        self.log = EventLog(self.dir / "events")
        self.registry = FakeRegistry()
        self.leases = FakeLeases()
        self.killed = []
        self.attempts = []

    async def asyncTearDown(self):
        await self.ctx.dispose()
        await self.log.close()
        self._tmp.cleanup()

    def _manager(self, **kwargs):
        return FailureManager(self.ctx, self.registry, self.leases, self.log,
                              **kwargs)

    def _event_names(self):
        return [name for name, _ in self.events]

    def _action_data(self):
        return [payload["data"] for name, payload in self.events
                if name == "failure.action"]

    async def _restart_ok(self, unit, attempt):
        self.attempts.append((unit.work_id, attempt))
        return True

    async def test_newly_offline_releases_leases_and_restarts(self):
        self.registry.transitions = [(make_unit(), "offline")]
        manager = self._manager(restart_cb=self._restart_ok,
                                kill_cb=lambda unit: self.killed.append(unit.work_id))
        results = await manager.tick()

        self.assertEqual(results[0]["action"], "restart")
        self.assertEqual(results[0]["attempt"], 1)
        self.assertEqual(self.leases.released, [("coder.1", "unit_offline")])
        self.assertEqual(self.attempts, [("coder.1", 1)])
        self.assertEqual([work_id for work_id, _ in self.registry.restarts],
                         ["coder.1"])
        self.assertEqual(self.registry.quarantined, [])
        self.assertIn("failure.detected", self._event_names())
        self.assertEqual(self._action_data()[0]["action"], "restart")
        self.assertEqual(self.killed, [])

    async def test_suspect_transitions_are_not_failures(self):
        self.registry.transitions = [(make_unit(), "suspect")]
        manager = self._manager(restart_cb=self._restart_ok)
        self.assertEqual(await manager.tick(), [])
        self.assertEqual(self.leases.released, [])
        self.assertEqual(self.attempts, [])
        self.assertNotIn("failure.detected", self._event_names())

    async def test_restart_budget_exceeded_quarantines(self):
        policy = FailurePolicy(kind="coder", action="restart", max_restarts=2)
        manager = self._manager(policies={"coder": policy},
                                restart_cb=self._restart_ok)
        unit = make_unit()
        actions = []
        for _ in range(3):
            self.registry.transitions = [(unit, "offline")]
            results = await manager.tick()
            actions.append(results[0]["action"])
        self.assertEqual(actions, ["restart", "restart", "quarantined"])
        self.assertEqual(self.attempts, [("coder.1", 1), ("coder.1", 2)])
        self.assertEqual(self.registry.quarantined,
                         [("coder.1", "restart_budget_exhausted")])
        self.assertEqual(self._action_data()[-1]["action"], "quarantined")

    async def test_restart_failure_quarantines_and_kills(self):
        async def restart(unit, attempt):
            self.attempts.append((unit.work_id, attempt))
            return False

        self.registry.transitions = [(make_unit(), "offline")]
        manager = self._manager(
            restart_cb=restart,
            kill_cb=lambda unit: self.killed.append(unit.work_id))
        results = await manager.tick()
        self.assertEqual(results[0]["action"], "quarantined")
        self.assertEqual(results[0]["reason"], "restart_failed")
        self.assertEqual(self.killed, ["coder.1"])
        self.assertEqual(self.registry.quarantined,
                         [("coder.1", "restart_failed")])

    async def test_quarantine_after_restart_count(self):
        policy = FailurePolicy(kind="coder", action="restart",
                               quarantine_after=5)
        self.registry.transitions = [(make_unit(restart_count=5), "offline")]
        manager = self._manager(policies={"coder": policy},
                                restart_cb=self._restart_ok)
        results = await manager.tick()
        self.assertEqual(results[0]["action"], "quarantined")
        self.assertEqual(results[0]["reason"], "quarantine_after")
        self.assertEqual(self.attempts, [])
        self.assertEqual(self.registry.quarantined,
                         [("coder.1", "quarantine_after")])

    async def test_policies_per_kind(self):
        policies = {
            "coder": FailurePolicy(kind="coder", action="ignore"),
            "tester": FailurePolicy(kind="tester", action="fail"),
        }
        self.registry.transitions = [
            (make_unit("coder.1", "coder"), "offline"),
            (make_unit("tester.1", "tester"), "offline"),
        ]
        manager = self._manager(
            policies=policies, restart_cb=self._restart_ok,
            kill_cb=lambda unit: self.killed.append(unit.work_id))
        results = await manager.tick()
        self.assertEqual([r["action"] for r in results], ["ignore", "fail"])
        self.assertEqual([r["work_id"] for r in results], ["coder.1", "tester.1"])
        self.assertEqual(self.registry.quarantined, [("tester.1", "policy_fail")])
        self.assertEqual(self.killed, ["tester.1"])
        self.assertEqual(manager.policies()["coder"].action, "ignore")

    async def test_missing_restart_callback_skips(self):
        self.registry.transitions = [(make_unit(), "offline")]
        manager = self._manager()
        results = await manager.tick()
        self.assertEqual(results[0]["action"], "restart_skipped")
        self.assertEqual(results[0]["reason"], "no_restart_callback")
        self.assertEqual(self.registry.quarantined, [])

    async def test_set_policy_validates(self):
        manager = self._manager()
        with self.assertRaises(ValidationError):
            manager.set_policy(FailurePolicy(kind="coder", action="explode"))
        with self.assertRaises(ValidationError):
            manager.set_policy(FailurePolicy(kind="", action="restart"))
        manager.set_policy(FailurePolicy(kind="coder", action="restart"))
        self.assertEqual(manager.policy_for("coder").action, "restart")
        self.assertEqual(manager.policy_for("unknown").action, "restart")

    async def test_start_and_stop_cancel_periodic_task(self):
        manager = self._manager(tick_s=0.01)
        await manager.start()
        self.assertIsNotNone(manager._task)
        await asyncio.sleep(0.05)
        self.assertGreater(self.registry.sweeps, 0)
        await manager.stop()
        self.assertIsNone(manager._task)
        sweeps_after_stop = self.registry.sweeps
        await asyncio.sleep(0.05)
        self.assertEqual(self.registry.sweeps, sweeps_after_stop)
        # stop() is idempotent.
        await manager.stop()

    async def test_tick_tolerates_plain_unit_transitions(self):
        self.registry.transitions = [make_unit()]
        manager = self._manager(restart_cb=self._restart_ok)
        results = await manager.tick()
        self.assertEqual(results[0]["action"], "restart")

    async def test_real_registry_and_leases_integration(self):
        registry = UnitRegistry(self.ctx, self.log, offline_after_s=0.001)
        leases = LeaseManager(self.ctx, self.log)
        unit = registry.register(UnitInfo(work_id="coder.9", kind="coder"))
        lease = leases.acquire("cap:x", "coder.9", 60)
        unit.last_seen = now_ms() - 10_000  # both liveness clocks are past

        async def restart(unit, attempt):
            self.attempts.append((unit.work_id, attempt))
            return True

        manager = FailureManager(self.ctx, registry, leases, self.log,
                                 restart_cb=restart, tick_s=1.0)
        self.assertEqual(await manager.tick(), [])   # online -> suspect
        results = await manager.tick()               # suspect -> offline
        self.assertEqual(results[0]["action"], "restart")
        self.assertEqual(leases.get(lease.id).status, "released")
        self.assertEqual(registry.get("coder.9").restart_count, 1)
        # Liveness is only re-established by a real registration/heartbeat.
        self.assertEqual(registry.get("coder.9").status, "offline")
        logged = {record.type for record in self.log.since(0)}
        self.assertIn("failure.detected", logged)
        self.assertIn("lease.released", logged)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from stackflowos.config import default_root
from stackflowos.errors import NotFound
from stackflowos.kernel import Context
from stackflowos.l2.eventlog import EventLog
from stackflowos.l2.leases import LeaseManager
from stackflowos.l2.registry import UnitInfo, UnitRegistry
from stackflowos.l2.scheduler import Scheduler

REPO = default_root()


class UnitRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.dir = Path(self._tmp.name)
        self.ctx = Context(name="test")
        self.events = []
        self.ctx.on_any(lambda mode, name, payload: self.events.append((name, payload)))
        self.log = EventLog(self.dir / "events")
        self.registry = UnitRegistry(self.ctx, self.log, offline_after_s=1.0)

    async def asyncTearDown(self):
        await self.ctx.dispose()
        await self.log.close()
        self._tmp.cleanup()

    def _register(self, work_id="coder.1", kind="coder", **kwargs):
        info = UnitInfo(work_id=work_id, kind=kind,
                        label=kwargs.pop("label", ""),
                        project_id=kwargs.pop("project_id", None),
                        abilities=kwargs.pop("abilities", []),
                        **kwargs)
        return self.registry.register(info)

    def _event_names(self):
        return [name for name, _ in self.events]

    def _event_payloads(self, name):
        return [payload for event, payload in self.events if event == name]

    def test_register_get_and_list(self):
        unit = self._register(abilities=[{"name": "fs.write", "scope": "project"}],
                              project_id="p-1")
        self.assertEqual(unit.status, "online")
        self.assertGreater(unit.started_at, 0)
        self.assertGreater(unit.last_seen, 0)
        self.assertEqual(self.registry.get("coder.1").work_id, "coder.1")
        self.assertEqual(len(self.registry.list()), 1)
        self.assertEqual(len(self.registry.list(kind="coder")), 1)
        self.assertEqual(len(self.registry.list(kind="tester")), 0)
        self.assertEqual(len(self.registry.list(project_id="p-1")), 1)
        self.assertEqual(len(self.registry.list(project_id="p-2")), 0)
        self.assertEqual(len(self.registry.online()), 1)
        with self.assertRaises(NotFound):
            self.registry.get("nope")

    def test_duplicate_register_updates_without_duplicating(self):
        first = self._register(label="first")
        created_at = first.started_at
        again = self.registry.register(UnitInfo(
            work_id="coder.1", kind="coder", label="second", restart_count=2))
        self.assertEqual(len(self.registry.list()), 1)
        self.assertEqual(again.label, "second")
        self.assertEqual(again.restart_count, 2)
        self.assertEqual(again.started_at, created_at)

    def test_events_go_to_log_and_bus(self):
        self._register()
        self.assertIn("unit.registered", self._event_names())
        self.assertIn("unit.online", self._event_names())
        logged = {record.type for record in self.log.since(0)}
        self.assertIn("unit.registered", logged)
        self.assertIn("unit.online", logged)
        payload = self._event_payloads("unit.registered")[0]["data"]
        self.assertEqual(payload["work_id"], "coder.1")
        self.assertEqual(payload["unit"]["kind"], "coder")

    def test_sweep_transitions_with_injected_time(self):
        unit = self._register()
        base = unit.last_seen
        self.assertEqual(self.registry.sweep(base + 500), [])
        transitions = self.registry.sweep(base + 1001)
        self.assertEqual([(u.work_id, s) for u, s in transitions],
                         [("coder.1", "suspect")])
        self.assertEqual(unit.status, "suspect")
        # No duplicate event while the unit stays suspect.
        self.assertEqual(self.registry.sweep(base + 1500), [])
        transitions = self.registry.sweep(base + 2001)
        self.assertEqual([(u.work_id, s) for u, s in transitions],
                         [("coder.1", "offline")])
        self.assertEqual(unit.status, "offline")
        self.assertIn("unit.suspect", self._event_names())
        self.assertIn("unit.offline", self._event_names())
        self.assertNotIn("quarantined", {u.work_id for u, _ in transitions})

    def test_heartbeat_restores_suspect_and_offline(self):
        unit = self._register()
        base = unit.last_seen
        self.registry.sweep(base + 1001)
        self.assertEqual(unit.status, "suspect")
        self.registry.heartbeat("coder.1", stats={"cpu": 0.1})
        self.assertEqual(unit.status, "online")
        self.assertEqual(unit.metadata["stats"], {"cpu": 0.1})
        self.assertIn("unit.online", self._event_names())

        base2 = unit.last_seen
        self.registry.sweep(base2 + 1001)
        self.registry.sweep(base2 + 2001)
        self.assertEqual(unit.status, "offline")
        self.registry.heartbeat("coder.1")
        self.assertEqual(unit.status, "online")
        with self.assertRaises(NotFound):
            self.registry.heartbeat("nope")

    def test_release(self):
        self._register()
        released = self.registry.release("coder.1", reason="done")
        self.assertIsNotNone(released)
        self.assertEqual(released.work_id, "coder.1")
        with self.assertRaises(NotFound):
            self.registry.get("coder.1")
        self.assertIsNone(self.registry.release("coder.1"))
        payload = self._event_payloads("unit.released")[0]["data"]
        self.assertEqual(payload["reason"], "done")

    def test_abilities_index_only_online_units(self):
        self._register("coder.1", abilities=[
            {"name": "fs.write", "scope": "project"},
            {"name": "fs.read", "scope": "project"},
        ])
        self._register("coder.2", abilities=[{"name": "fs.write", "scope": "project"}])
        self._register("tester.1", kind="tester", status="offline",
                       abilities=[{"name": "shell.exec", "scope": "project"}])
        self.assertEqual(self.registry.abilities(), {
            "fs.read": ["coder.1"],
            "fs.write": ["coder.1", "coder.2"],
        })

    def test_quarantine_hwinfo_and_restart(self):
        self._register()
        self.registry.mark_quarantined("coder.1", "bad")
        self.assertEqual(self.registry.get("coder.1").status, "quarantined")
        self.assertIn("unit.quarantined", self._event_names())

        self.registry.update_hwinfo("coder.1", {"cpu": 4, "mem": 8192})
        self.registry.update_hwinfo("coder.1", {"cpu": 8})
        self.assertEqual(self.registry.get("coder.1").hwinfo,
                         {"cpu": 8, "mem": 8192})

        self.registry.note_restart("coder.1", reason="test")
        unit = self.registry.get("coder.1")
        self.assertEqual(unit.restart_count, 1)
        self.assertEqual(unit.status, "quarantined")  # liveness is not faked
        self.assertIn("unit.restarted", self._event_names())
        with self.assertRaises(NotFound):
            self.registry.mark_quarantined("nope", "x")

    def test_stats(self):
        self._register("coder.1")
        self._register("coder.2")
        self._register("tester.1", kind="tester", status="offline")
        stats = self.registry.stats()
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["online"], 2)
        self.assertEqual(stats["by_status"], {"offline": 1, "online": 2})
        self.assertEqual(stats["by_kind"], {"coder": 2, "tester": 1})


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.dir = Path(self._tmp.name)
        self.ctx = Context(name="test")
        self.log = EventLog(self.dir / "events")
        self.registry = UnitRegistry(self.ctx, self.log, offline_after_s=1.0)

    async def asyncTearDown(self):
        await self.ctx.dispose()
        await self.log.close()
        self._tmp.cleanup()

    def _add(self, work_id, project_id=None, abilities=("fs.write",),
             status="online", **kwargs):
        return self.registry.register(UnitInfo(
            work_id=work_id, kind=work_id.split(".")[0],
            project_id=project_id, status=status,
            abilities=[{"name": name, "scope": "project"} for name in abilities],
            **kwargs))

    def test_select_requires_online_and_ability(self):
        self._add("coder.1", abilities=("fs.write",))
        self._add("coder.2", abilities=("fs.write",))
        self._add("tester.1", abilities=("shell.exec",), status="offline")
        scheduler = Scheduler(self.registry)

        self.assertIn(scheduler.select("fs.write").work_id, {"coder.1", "coder.2"})
        self.assertIsNone(scheduler.select("shell.exec"))
        self.assertIsNone(scheduler.select("fs.write",
                                           exclude={"coder.1", "coder.2"}))
        self.assertIsNone(scheduler.select("nothing.here"))

    def test_project_bonus_and_deterministic_tie_break(self):
        self._add("coder.1", project_id="p-2")
        self._add("coder.2", project_id="p-1")
        scheduler = Scheduler(self.registry)

        self.assertEqual(scheduler.select("fs.write", project_id="p-1").work_id,
                         "coder.2")
        # No project context: both score 1.0, so the lower work_id wins.
        self.assertEqual(scheduler.select("fs.write").work_id, "coder.1")
        self.assertGreater(scheduler.score(self._get("coder.2"), project_id="p-1"),
                           scheduler.score(self._get("coder.1"), project_id="p-1"))
        self.assertEqual(scheduler.score(self._get("coder.2")),
                         1.0)

    def _get(self, work_id):
        return self.registry.get(work_id)

    def test_lease_load_breaks_ties(self):
        self._add("coder.1", abilities=("fs.write", "fs.read"))
        self._add("coder.2", abilities=("fs.write", "fs.read"))
        leases = LeaseManager(self.ctx, self.log)
        leases.acquire("slot:coder.1:0", "coder.1", 60)
        scheduler = Scheduler(self.registry)
        scheduler.bind_leases(leases)
        self.assertEqual(scheduler.select("fs.write").work_id, "coder.2")
        self.assertLess(scheduler.score(self._get("coder.1")),
                        scheduler.score(self._get("coder.2")))

    def test_lease_manager_discovered_from_context(self):
        self._add("coder.1")
        self._add("coder.2")
        leases = LeaseManager(self.ctx, self.log)
        leases.acquire("slot:coder.1:0", "coder.1", 60)
        self.ctx.provide("leases", leases)
        scheduler = Scheduler(self.registry)
        self.assertEqual(scheduler.select("fs.write").work_id, "coder.2")

    def test_require_filter(self):
        self._add("coder.1", metadata={"model": "mock"},
                  hwinfo={"gpu": False})
        self._add("coder.2", metadata={"model": "gpt"},
                  hwinfo={"gpu": True})
        scheduler = Scheduler(self.registry)
        self.assertEqual(
            scheduler.select("fs.write", require={"model": "mock"}).work_id,
            "coder.1")
        self.assertEqual(
            scheduler.select("fs.write", require={"gpu": True}).work_id,
            "coder.2")
        self.assertIsNone(scheduler.select("fs.write",
                                           require={"model": "missing"}))


if __name__ == "__main__":
    unittest.main()

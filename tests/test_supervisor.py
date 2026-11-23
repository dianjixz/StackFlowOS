"""Tests for the hub-side agent process supervisor."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from stackflowos.agents.supervisor import AgentSupervisor
from stackflowos.config import AgentsCfg
from stackflowos.errors import BadRequest, Conflict, NotFound, Unavailable
from stackflowos.kernel.context import Context

TMP_ROOT = Path(__file__).resolve().parent.parent / ".stackflowos" / "tmp"


class FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.terminated = False
        self.returncode = None

    def terminate(self) -> None:
        self.terminated = True


class FakeRegistry:
    def __init__(self, online_after: int = 1) -> None:
        self.online_after = online_after
        self.polls: dict[str, int] = {}

    def get(self, work_id: str):
        count = self.polls.get(work_id, 0) + 1
        self.polls[work_id] = count
        if count >= self.online_after:
            return SimpleNamespace(work_id=work_id, status="online")
        return None


class FakeAllocator:
    def __init__(self) -> None:
        self.ids: list[str] = []
        self.allocated: dict[str, object] = {}
        self.released: list[str] = []

    def new_work_id(self, kind: str) -> str:
        count = sum(1 for work_id in self.ids
                    if work_id.rsplit(".", 1)[0] == kind)
        work_id = f"{kind}.{count}"
        self.ids.append(work_id)
        return work_id

    def allocate(self, work_id: str):
        addresses = SimpleNamespace(rpc=f"ipc://{work_id}.rpc",
                                    events=f"ipc://{work_id}.events")
        self.allocated[work_id] = addresses
        return addresses

    def release(self, work_id: str) -> None:
        self.released.append(work_id)


class FakeEventLog:
    def __init__(self) -> None:
        self.records: list[tuple[tuple, dict]] = []

    def append(self, *args, **kwargs):
        self.records.append((args, kwargs))

    def types(self) -> list[str]:
        return [record[0][0] for record in self.records]


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.registry = FakeRegistry()
        self.allocator = FakeAllocator()
        self.eventlog = FakeEventLog()
        self.popen_calls: list[tuple[list[str], dict]] = []
        self.procs: list[FakeProc] = []
        self.sup = self._make()

    def tearDown(self) -> None:
        for sup in getattr(self, "_supervisors", [self.sup]):
            for handle in list(sup._log_handles.values()):
                try:
                    handle.close()
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, *, config: AgentsCfg | None = None,
              spawn_timeout_s: float = 2.0, control=None,
              token_issuer=None,
              token_revoker=None) -> AgentSupervisor:
        sup = AgentSupervisor(
            Context(), registry=self.registry, allocator=self.allocator,
            eventlog=self.eventlog, config=config or AgentsCfg(),
            sys_rpc="ipc://sys.rpc", state_dir=self.state, root=self.root,
            popen_factory=self._popen,
            spawn_timeout_s=spawn_timeout_s, control=control,
            token_issuer=token_issuer, token_revoker=token_revoker)
        supervisors = getattr(self, "_supervisors", [])
        supervisors.append(sup)
        self._supervisors = supervisors
        return sup

    def _popen(self, command: list[str], kwargs: dict):
        self.popen_calls.append((command, kwargs))
        proc = FakeProc(pid=4000 + len(self.procs))
        self.procs.append(proc)
        return proc

    # -- command construction --------------------------------------------
    def test_build_command(self) -> None:
        command = self.sup.build_command("coder", "p-1", "t-1", "coder.0")
        self.assertEqual(command[:4],
                         [sys.executable, "-m", "stackflowos.worker", "--sys"])
        self.assertIn("ipc://sys.rpc", command)
        self.assertEqual(command[command.index("--work-id") + 1], "coder.0")
        self.assertEqual(command[command.index("--role") + 1], "coder")
        self.assertEqual(command[command.index("--state-dir") + 1],
                         str(self.state))
        self.assertEqual(command[command.index("--root") + 1], str(self.root))
        self.assertEqual(command[command.index("--project") + 1], "p-1")
        self.assertEqual(command[command.index("--task") + 1], "t-1")

        minimal = self.sup.build_command("lead", None, None, "lead.0")
        self.assertNotIn("--project", minimal)
        self.assertNotIn("--task", minimal)
        self.assertEqual(self.popen_calls, [], "command construction must not spawn")

    # -- spawn ------------------------------------------------------------
    async def test_spawn_waits_for_online_and_tracks_agent(self) -> None:
        self.registry.online_after = 2
        agent = await self.sup.spawn("coder", "p-1", task_id="t-1",
                                     reason="test")
        self.assertRegex(agent.id, r"^agent-coder-1$")
        self.assertEqual(agent.unit_id, "coder.0")
        self.assertEqual(agent.status, "idle")
        self.assertIn("工人", agent.display_name)
        self.assertEqual(self.sup.get(agent.id), agent)
        self.assertEqual(self.allocator.allocated["coder.0"].rpc,
                         "ipc://coder.0.rpc")
        self.assertEqual(self.sup._pids["coder.0"], 4000)
        self.assertEqual(self.sup.stats()["total"], 1)

        command, kwargs = self.popen_calls[0]
        self.assertIn("coder.0", command)
        self.assertEqual(kwargs["cwd"], str(self.root))
        self.assertEqual(kwargs["env"]["SFO_ROOT"], str(self.root))
        self.assertEqual(kwargs["env"]["SFO_STATE_DIR"], str(self.state))
        self.assertIn(str(self.root), kwargs["env"]["PYTHONPATH"])
        log_file = self.state / "logs" / "agents" / "coder.0.log"
        self.assertTrue(log_file.exists())
        self.assertIn("agent.spawned", self.eventlog.types())

    async def test_spawn_timeout_raises_unavailable(self) -> None:
        self.registry.online_after = 10 ** 9
        sup = self._make(spawn_timeout_s=0.25)
        with self.assertRaises(Unavailable):
            await sup.spawn("coder", "p-1")
        self.assertIn("coder.0", self.allocator.released)
        self.assertEqual(len(sup.list()), 1)
        self.assertEqual(sup.list()[0].status, "failed")
        self.assertTrue(self.procs[0].terminated)
        self.assertIn("agent.failed", self.eventlog.types())

    async def test_spawn_rejects_unknown_role_and_capacity(self) -> None:
        with self.assertRaises(BadRequest):
            await self.sup.spawn("wizard", "p-1")
        self.assertEqual(self.allocator.ids, [])

        sup = self._make(config=AgentsCfg(max_workers=1))
        await sup.spawn("coder", "p-1")
        with self.assertRaises(Conflict):
            await sup.spawn("tester", "p-1")

    # -- protocol v2 tokens (H3) -----------------------------------------
    async def test_spawn_passes_unit_token_only_through_env(self) -> None:
        issued: list[tuple] = []

        def issue(work_id, project_id, role, agent_id):
            issued.append((work_id, project_id, role, agent_id))
            return f"token-{work_id}"

        sup = self._make(token_issuer=issue)
        agent = await sup.spawn("coder", "p-1", task_id="t-1")
        command, kwargs = self.popen_calls[-1]
        self.assertEqual(kwargs["env"]["SFO_UNIT_TOKEN"], "token-coder.0")
        self.assertNotIn("token-coder.0", command)
        self.assertEqual(issued, [("coder.0", "p-1", "coder", agent.id)])

    async def test_restart_revokes_old_token_and_issues_a_new_one(self) -> None:
        issued: list[tuple] = []
        revoked: list[str] = []

        def issue(work_id, project_id, role, agent_id):
            issued.append((work_id, project_id, role, agent_id))
            return f"token-{work_id}"

        sup = self._make(token_issuer=issue, token_revoker=revoked.append)
        agent = await sup.spawn("coder", "p-1")
        unit = SimpleNamespace(work_id="coder.0", kind="coder", project_id="p-1")
        self.assertTrue(await sup.restart(unit, 1))
        self.assertIn("coder.0", revoked)
        self.assertEqual(issued[-1][0], "coder.1")
        self.assertEqual(self.popen_calls[-1][1]["env"]["SFO_UNIT_TOKEN"],
                         "token-coder.1")
        self.assertEqual(agent.unit_id, "coder.1")

    # -- restart ----------------------------------------------------------
    async def test_restart_reuses_agent_with_new_unit(self) -> None:
        self.registry.online_after = 1
        agent = await self.sup.spawn("coder", "p-1", task_id="t-1")
        old_unit = agent.unit_id
        unit = SimpleNamespace(work_id=old_unit, kind="coder", project_id="p-1")
        self.assertTrue(await self.sup.restart(unit, 1))
        self.assertEqual(agent.unit_id, "coder.1")
        self.assertEqual(agent.status, "idle")
        self.assertEqual(len(self.sup.list()), 1)
        self.assertIn(old_unit, self.allocator.released)
        self.assertIn("agent.spawned", self.eventlog.types())
        spawned = [record for record in self.eventlog.records
                   if record[0][0] == "agent.spawned"]
        self.assertTrue(spawned[-1][0][1].get("restarted"))
        self.assertEqual(spawned[-1][0][1].get("attempt"), 1)

    async def test_restart_unknown_unit_returns_false(self) -> None:
        unit = SimpleNamespace(work_id="coder.99", kind="coder",
                               project_id="p-1")
        self.assertFalse(await self.sup.restart(unit, 1))

    # -- kill -------------------------------------------------------------
    async def test_kill_without_control_terminates_process(self) -> None:
        agent = await self.sup.spawn("coder", "p-1")
        proc = self.procs[0]
        self.assertTrue(await self.sup.kill(agent.id, reason="test"))
        self.assertTrue(proc.terminated)
        self.assertEqual(agent.status, "dead")
        self.assertIn("coder.0", self.allocator.released)
        self.assertIn("agent.terminated", self.eventlog.types())
        self.assertFalse(await self.sup.kill(agent.id))

    async def test_kill_project_uses_control_and_spares_other_projects(self) -> None:
        calls: list[tuple[str, str, dict]] = []

        async def control(work_id: str, action: str, data: dict) -> None:
            calls.append((work_id, action, data))

        sup = self._make(control=control)
        coder = await sup.spawn("coder", "p-1")
        tester = await sup.spawn("tester", "p-1")
        other = await sup.spawn("coder", "p-2")
        killed = await sup.kill_project("p-1", reason="project_archived")
        self.assertEqual(killed, 2)
        self.assertEqual(coder.status, "dead")
        self.assertEqual(tester.status, "dead")
        self.assertEqual(other.status, "idle")
        self.assertEqual([call[1] for call in calls], ["unit.kill", "unit.kill"])
        self.assertEqual(calls[0][2]["reason"], "project_archived")

    # -- roster helpers ---------------------------------------------------
    async def test_register_update_finish_and_stats(self) -> None:
        agent = self.sup.register(role="reviewer", project_id="p-1",
                                  unit_id="reviewer.0", model="mock")
        self.assertEqual(agent.id, "agent-reviewer-1")
        self.assertEqual(self.sup.register(role="reviewer", project_id="p-1",
                                           unit_id="reviewer.0"), agent)
        self.assertEqual(len(self.sup.list()), 1)

        updated = self.sup.update(agent.id, {"status": "working",
                                             "stats": {"steps": 2}})
        self.assertEqual(updated.status, "working")
        self.assertEqual(updated.stats["steps"], 2)
        with self.assertRaises(BadRequest):
            self.sup.update(agent.id, {"status": "bogus"})
        with self.assertRaises(BadRequest):
            self.sup.update(agent.id, {"nonsense": 1})

        finished = self.sup.finish(agent.id, "done reviewing")
        self.assertEqual(finished.status, "done")
        self.assertEqual(finished.summary, "done reviewing")
        self.assertIn("agent.finished", self.eventlog.types())
        self.assertEqual(self.sup.stats()["by_role"], {"reviewer": 1})
        with self.assertRaises(NotFound):
            self.sup.get("agent-missing-9")

    # -- persistence ------------------------------------------------------
    async def test_save_and_load_roster(self) -> None:
        agent = await self.sup.spawn("coder", "p-1", task_id="t-1")
        self.assertTrue(self.sup.state_path.exists())
        self.sup.save()

        reloaded = self._make()
        reloaded.load()
        self.assertEqual(reloaded.stats()["total"], 1)
        restored = reloaded.get(agent.id)
        self.assertEqual(restored.unit_id, "coder.0")
        self.assertEqual(restored.task_id, "t-1")
        # Counters continue after a reload.
        new_agent = reloaded.register(role="coder", project_id="p-1",
                                      unit_id="coder.5")
        self.assertEqual(new_agent.id, "agent-coder-2")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

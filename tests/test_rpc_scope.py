"""RPC resource-scope tests (M5).

H3 (``test_rpc_auth``) proves *identity*: the hub derives the principal from the
connection credential and binds self-reported fields.  This module covers the
next layer, *scope* (security plan §5.2.5/§5.2.6): a unit credential carries
project/role/agent/task, and an external unit may only touch its own project, a
plain worker only its own task, a lead only its own project's agents/tasks.

Calls go through the same dispatcher the transport uses (``external=True`` with
a ``Principal`` freshly derived from the token), so token scope, allowlist,
binding and range checks are all exercised without a worker process.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from stackflowos.app import App
from stackflowos.config import Config, default_root
from stackflowos.errors import BadRequest, Denied, NotFound
from stackflowos.l2 import UnitInfo

REPO = default_root()


def _tmpdir() -> str:
    base = REPO / ".stackflowos" / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix="rs-", dir=str(base))


class RpcScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = _tmpdir()
        self.app = App(Config.load(root=Path(self.tmp), state_dir=Path(self.tmp)))
        await self.app.start()
        # Kill without a real worker process: no per-unit RPC (which would
        # wait for the transport timeout) for the fake roster entries below.
        self.app.supervisor.control = None

        self.pa = self.app.projects.create("Alpha", "project alpha")
        self.pb = self.app.projects.create("Beta", "project beta")
        self.ta = self.app.projects.add_task(self.pa.id, "A own task", role="coder")
        self.ta2 = self.app.projects.add_task(self.pa.id, "A other task", role="coder")
        self.tb = self.app.projects.add_task(self.pb.id, "B task", role="coder")

        self.agent_a1 = self.app.supervisor.register(
            role="coder", project_id=self.pa.id, unit_id="unit-a1",
            task_id=self.ta.id)
        self.agent_a2 = self.app.supervisor.register(
            role="coder", project_id=self.pa.id, unit_id="unit-a2",
            task_id=self.ta2.id)
        self.agent_lead = self.app.supervisor.register(
            role="lead", project_id=self.pa.id, unit_id="unit-lead-a")
        self.agent_b = self.app.supervisor.register(
            role="coder", project_id=self.pb.id, unit_id="unit-b1",
            task_id=self.tb.id)

    async def asyncTearDown(self):
        await self.app.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def _unit_auth(self, work_id: str, project_id: str | None, role: str,
                   agent_id: str | None, task_id: str | None = None) -> dict:
        token = self.app.rpc_auth.issue_unit(
            work_id, project_id, role, agent_id, task_id)
        return {"kind": "unit", "id": work_id, "token": token}

    def _operator_auth(self) -> dict:
        return {"kind": "operator", "id": "human:operator",
                "token": self.app.issue_operator_token()}

    async def _call(self, auth: dict, action: str, data: dict | None = None):
        """Dispatch as the transport would: re-derive the principal each time."""
        principal = self.app.rpc_auth.verify(auth)
        return await self.app.handle_rpc_action(
            action, dict(data or {}), src=auth["id"], external=True,
            principal=principal)

    def _worker(self) -> dict:
        return self._unit_auth("unit-a1", self.pa.id, "coder",
                               self.agent_a1.id, self.ta.id)

    def _lead(self) -> dict:
        return self._unit_auth("unit-lead-a", self.pa.id, "lead",
                               self.agent_lead.id)

    async def _assert_denied(self, auth: dict, action: str,
                             data: dict | None = None) -> None:
        with self.assertRaises(Denied):
            await self._call(auth, action, data)

    # -- credential scope ------------------------------------------------
    async def test_unit_credential_carries_task_id(self):
        auth = self._worker()
        principal = self.app.rpc_auth.verify(auth)
        self.assertEqual(principal.project_id, self.pa.id)
        self.assertEqual(principal.task_id, self.ta.id)
        self.assertTrue(principal.is_unit and principal.is_worker)
        self.assertFalse(principal.is_lead)

        # Attach flows allocate before an agent exists; registration fills it.
        token = self.app.rpc_auth.issue_unit("unit-attach", self.pa.id, "coder")
        self.app.rpc_auth.bind_agent("unit-attach", self.agent_a2.id,
                                     self.agent_a2.task_id)
        bound = self.app.rpc_auth.verify(
            {"kind": "unit", "id": "unit-attach", "token": token})
        self.assertEqual(bound.agent_id, self.agent_a2.id)
        self.assertEqual(bound.task_id, self.ta2.id)

        lead = self.app.rpc_auth.verify(self._lead())
        self.assertTrue(lead.is_lead)
        self.assertFalse(lead.is_worker)

    # -- tasks -----------------------------------------------------------
    async def test_worker_can_only_touch_its_own_task_in_its_project(self):
        worker = self._worker()
        # own project, own task -> allowed
        updated = await self._call(
            worker, "task.update", {"task_id": self.ta.id,
                                    "patch": {"title": "renamed by worker"}})
        self.assertEqual(updated["id"], self.ta.id)
        self.assertEqual(updated["title"], "renamed by worker")

        # own project, somebody else's task -> 403
        await self._assert_denied(
            worker, "task.update", {"task_id": self.ta2.id,
                                    "patch": {"title": "steal"}})
        await self._assert_denied(
            worker, "task.get", {"task_id": self.ta2.id})

        # another project -> 403 for both worker and lead
        await self._assert_denied(
            worker, "task.update", {"task_id": self.tb.id,
                                    "patch": {"title": "cross"}})
        await self._assert_denied(
            self._lead(), "task.update", {"task_id": self.tb.id,
                                          "patch": {"title": "cross"}})

        # unknown task is a 404, not a silent success
        with self.assertRaises(NotFound):
            await self._call(worker, "task.get", {"task_id": "task-missing"})

    async def test_lead_may_touch_any_task_in_its_own_project(self):
        lead = self._lead()
        for task in (self.ta, self.ta2):
            updated = await self._call(
                lead, "task.update",
                {"task_id": task.id, "patch": {"title": f"lead:{task.id}"}})
            self.assertEqual(updated["id"], task.id)

    async def test_task_claim_agent_must_be_the_principal_agent(self):
        worker = self._worker()
        await self._assert_denied(
            worker, "task.claim",
            {"task_id": self.ta.id, "agent_id": self.agent_a2.id})
        await self._assert_denied(worker, "task.claim", {"task_id": self.ta2.id})
        claimed = await self._call(worker, "task.claim", {"task_id": self.ta.id})
        self.assertEqual(claimed["assignee"], self.agent_a1.id)

    async def test_task_list_is_forced_to_own_project(self):
        worker = self._worker()
        listed = await self._call(worker, "task.list", {})
        self.assertEqual({task["project_id"] for task in listed["tasks"]},
                         {self.pa.id})
        # explicit own project is fine; another project is 403
        same = await self._call(worker, "task.list", {"project_id": self.pa.id})
        self.assertEqual({task["project_id"] for task in same["tasks"]},
                         {self.pa.id})
        await self._assert_denied(worker, "task.list", {"project_id": self.pb.id})

    async def test_task_create_is_lead_only_and_project_is_forced(self):
        await self._assert_denied(
            self._worker(), "task.create",
            {"project_id": self.pb.id, "title": "worker task"})
        created = await self._call(
            self._lead(), "task.create",
            {"project_id": self.pb.id, "title": "lead task"})
        # A unit can never create a task outside its own project: the hub
        # overwrites the caller's project id instead of trusting it.
        self.assertEqual(created["project_id"], self.pa.id)
        implicit = await self._call(
            self._lead(), "task.create", {"title": "no project given"})
        self.assertEqual(implicit["project_id"], self.pa.id)

    # -- agents ----------------------------------------------------------
    async def test_agent_kill_is_lead_only_and_same_project(self):
        lead = self._lead()
        await self._assert_denied(lead, "agent.kill", {"agent_id": self.agent_b.id})
        await self._assert_denied(lead, "agent.kill", {"agent_id": self.agent_lead.id})
        await self._assert_denied(self._worker(), "agent.kill",
                                  {"agent_id": self.agent_a2.id})
        killed = await self._call(lead, "agent.kill", {"agent_id": self.agent_a2.id})
        self.assertEqual(killed, {"ok": True})
        self.assertEqual(self.app.supervisor.get(self.agent_a2.id).status, "dead")

    async def test_agent_spawn_is_lead_only_project_forced_and_task_role_only(self):
        await self._assert_denied(self._worker(), "agent.spawn", {"role": "coder"})
        # A lead must not be able to spawn another lead/assistant/aggregator:
        # only task roles are dispatchable.
        await self._assert_denied(self._lead(), "agent.spawn", {"role": "lead"})
        await self._assert_denied(self._lead(), "agent.spawn", {"role": "assistant"})

        fake = AsyncMock(return_value=self.agent_a2)
        with patch.object(self.app.supervisor, "spawn", new=fake):
            spawned = await self._call(
                self._lead(), "agent.spawn",
                {"role": "coder", "project_id": self.pb.id})
        self.assertEqual(spawned["id"], self.agent_a2.id)
        args, kwargs = fake.await_args
        self.assertEqual(args, ("coder", self.pa.id))
        self.assertEqual(kwargs.get("task_id"), None)

    async def test_agent_register_update_finish_are_bound_to_own_agent(self):
        worker = self._worker()
        # update/finish another agent -> 403; own agent -> allowed
        await self._assert_denied(
            worker, "agent.update", {"agent_id": self.agent_b.id,
                                     "patch": {"status": "idle"}})
        await self._assert_denied(
            worker, "agent.finish", {"agent_id": self.agent_b.id, "summary": "x"})
        updated = await self._call(
            worker, "agent.update", {"agent_id": self.agent_a1.id,
                                     "patch": {"status": "idle"}})
        self.assertEqual(updated["id"], self.agent_a1.id)
        # a unit cannot move its agent to another project
        await self._assert_denied(
            worker, "agent.update",
            {"agent_id": self.agent_a1.id, "patch": {"project_id": self.pb.id}})
        # register may only speak for its own unit/agent (H3) and project
        await self._assert_denied(
            worker, "agent.register", {"unit_id": "unit-b1", "role": "coder"})
        await self._assert_denied(
            worker, "agent.register", {"agent_id": self.agent_b.id, "role": "coder"})
        await self._assert_denied(
            worker, "agent.register",
            {"role": "coder", "project_id": self.pb.id})
        registered = await self._call(
            worker, "agent.register",
            {"unit_id": "unit-a1", "role": "coder", "project_id": self.pa.id,
             "task_id": self.ta.id})
        self.assertEqual(registered["project_id"], self.pa.id)
        finished = await self._call(
            worker, "agent.finish", {"agent_id": self.agent_a1.id,
                                     "summary": "done"})
        self.assertEqual(finished["status"], "done")

    async def test_agent_list_is_forced_to_own_project(self):
        worker = self._worker()
        listed = await self._call(worker, "agent.list", {})
        self.assertEqual({agent["project_id"] for agent in listed["agents"]},
                         {self.pa.id})
        await self._assert_denied(worker, "agent.list", {"project_id": self.pb.id})

    # -- events / memory / units ----------------------------------------
    async def test_event_since_is_forced_to_own_project(self):
        worker = self._worker()
        events = await self._call(worker, "event.since", {})
        self.assertTrue(events["events"])
        self.assertEqual({event["project_id"] for event in events["events"]},
                         {self.pa.id})
        await self._assert_denied(worker, "event.since",
                                  {"project_id": self.pb.id})

    async def test_event_append_forces_project_and_source(self):
        worker = self._worker()
        seq = self.app.eventlog.last_seq
        await self._call(worker, "event.append", {
            "type": "agent.step", "project_id": self.pb.id, "source": "sys",
            "data": {"step": 1}})
        ours = [record for record in self.app.eventlog.since(seq, limit=50)
                if record.type == "agent.step"]
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0].project_id, self.pa.id)
        self.assertEqual(ours[0].source, f"agent:{self.agent_a1.id}")
        # nothing landed in the forged target project
        self.assertEqual(self.app.eventlog.since(seq, limit=50,
                                                 project_id=self.pb.id), [])

        # A unit that has not registered an agent yet is sourced as the unit.
        anonymous = self._unit_auth("unit-a-anon", self.pa.id, "coder", None)
        seq = self.app.eventlog.last_seq
        await self._call(anonymous, "event.append",
                         {"type": "agent.step", "source": "operator",
                          "data": {}})
        anonymous_events = [record for record in
                            self.app.eventlog.since(seq, limit=50)
                            if record.type == "agent.step"]
        self.assertEqual(anonymous_events[0].source, "unit:unit-a-anon")
        self.assertEqual(anonymous_events[0].project_id, self.pa.id)

    async def test_memory_is_forced_to_own_project(self):
        worker = self._worker()
        await self._call(worker, "memory.set", {"key": "k", "value": 7})
        self.assertEqual(self.app.memory.get(self.pa.id, "k"), 7)
        self.assertEqual(self.app.memory.get(self.pb.id, "k"), None)
        self.assertEqual((await self._call(worker, "memory.get", {"key": "k"}))
                         ["value"], 7)
        await self._assert_denied(worker, "memory.get",
                                  {"project_id": self.pb.id, "key": "k"})
        await self._assert_denied(worker, "memory.set",
                                  {"project_id": self.pb.id, "key": "k",
                                   "value": 1})

    async def test_unit_list_only_shows_own_project(self):
        self.app.registry.register(UnitInfo(
            work_id="unit-a1", kind="coder", project_id=self.pa.id))
        self.app.registry.register(UnitInfo(
            work_id="unit-b9", kind="coder", project_id=self.pb.id))
        worker = self._worker()
        listed = await self._call(worker, "unit.list", {})
        self.assertEqual({unit["project_id"] for unit in listed["units"]},
                         {self.pa.id})
        # Asking for another project is silently narrowed to the own project.
        narrowed = await self._call(worker, "unit.list", {"project_id": self.pb.id})
        self.assertEqual({unit["project_id"] for unit in narrowed["units"]},
                         {self.pa.id})

    # -- projects --------------------------------------------------------
    async def test_project_read_update_and_archive_are_own_project_only(self):
        lead = self._lead()
        self.assertEqual((await self._call(lead, "project.get",
                                           {"project_id": self.pa.id}))["id"],
                         self.pa.id)
        await self._assert_denied(lead, "project.get", {"project_id": self.pb.id})
        await self._assert_denied(
            lead, "project.update",
            {"project_id": self.pb.id, "patch": {"brief": "x"}})
        # Only ``brief`` is writable from the unit side.
        await self._assert_denied(
            lead, "project.update",
            {"project_id": self.pa.id, "patch": {"name": "hijacked"}})
        await self._assert_denied(
            lead, "project.update",
            {"project_id": self.pa.id,
             "patch": {"brief": "ok", "status": "done"}})
        updated = await self._call(
            lead, "project.update",
            {"project_id": self.pa.id, "patch": {"brief": "lead brief"}})
        self.assertEqual(updated["brief"], "lead brief")

        # worker cannot archive; lead can only archive its own project
        await self._assert_denied(self._worker(), "project.archive",
                                  {"project_id": self.pa.id})
        await self._assert_denied(lead, "project.archive",
                                  {"project_id": self.pb.id})
        archived = await self._call(lead, "project.archive",
                                    {"project_id": self.pa.id})
        self.assertEqual(archived["project"]["status"], "archived")

    async def test_project_list_and_create_are_operator_only(self):
        for auth in (self._worker(), self._lead()):
            await self._assert_denied(auth, "project.list", {})
            await self._assert_denied(auth, "project.create",
                                      {"name": "Nope", "spawn_lead": False})

    # -- operator / in-process paths ------------------------------------
    async def test_operator_and_in_process_paths_keep_the_global_view(self):
        operator = self._operator_auth()
        listed = await self._call(operator, "project.list", {})
        self.assertEqual({project["id"] for project in listed["projects"]},
                         {self.pa.id, self.pb.id})
        created = await self._call(
            operator, "project.create",
            {"name": "Gamma", "goal": "operator project", "spawn_lead": False})
        self.assertIn(created["id"], {p.id for p in self.app.projects.list()})
        updated = await self._call(
            operator, "task.update",
            {"task_id": self.tb.id, "patch": {"title": "operator edit"}})
        self.assertEqual(updated["title"], "operator edit")
        killed = await self._call(operator, "agent.kill",
                                  {"agent_id": self.agent_b.id})
        self.assertEqual(killed, {"ok": True})

        # Web/CLI/chat call the dispatcher without ``external`` -> trusted.
        in_process = await self.app.handle_rpc_action(
            "task.update", {"task_id": self.tb.id,
                            "patch": {"title": "in-process edit"}})
        self.assertEqual(in_process["title"], "in-process edit")
        projects = await self.app.handle_rpc_action("project.list", {})
        self.assertGreaterEqual(len(projects["projects"]), 3)

    async def test_unknown_scope_data_is_rejected_as_bad_request(self):
        # Missing task id is a client error (400), not a silent no-op.
        with self.assertRaises(BadRequest):
            await self._call(self._worker(), "task.update", {"patch": {}})
        with self.assertRaises(BadRequest):
            await self._call(self._lead(), "project.update",
                             {"project_id": self.pa.id, "patch": "not-a-dict"})


if __name__ == "__main__":
    unittest.main()

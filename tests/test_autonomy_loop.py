"""End-to-end autonomy loop on a real App: patrol → dispatch → guardian."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stackflowos.app import App
from stackflowos.autonomy.patrol import Snapshot
from stackflowos.config import Config
from stackflowos.protocol import now_ms

TMP_ROOT = Path(__file__).resolve().parents[1] / ".stackflowos" / "tmp"


class AutonomyLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(TMP_ROOT))
        self.root = Path(self._tmp.name).resolve()
        self.app = App(Config.load(root=self.root,
                                   state_dir=self.root / "state"))
        await self.app.start()
        self.service = self.app.autonomy

    async def asyncTearDown(self) -> None:
        await self.service.configure({"enabled": False})
        await self.app.stop()
        self._tmp.cleanup()

    async def _project(self, goal: str = "ship a tiny tool"):
        card = await self.app.create_project("Loop", goal, spawn_lead=False)
        return card["id"], Path(card["workspace"])

    async def _policy(self, **overrides) -> None:
        policy = {
            "enabled": True,
            "on_start": False,
            "interval_s": 600,
            "task_idle_s": 10.0,
            "dispatch_mode": "agents",
            "dispatch_cooldown_s": 0.0,
            "approve_mode": "enforce",
            "approve_risk_max": "medium",
            "approve_capabilities": ["fs.write", "agent.spawn"],
            "approve_min_age_s": 0.0,
            "approve_deny_cooldown_s": 60.0,
        }
        policy.update(overrides)
        await self.service.configure(policy)

    def _backdate(self, task_id: str, *, age_s: float = 600.0) -> None:
        task = self.app.projects.task_get(task_id)
        stamp = now_ms() - int(age_s * 1000)
        task.created_at = stamp
        task.updated_at = stamp
        self.app.projects.save()

    async def test_patrol_dispatches_an_idle_task(self) -> None:
        project_id, _ = await self._project()
        todo = next(task for task in self.app.projects.task_list(
            project_id=project_id) if task.status == "todo")
        self._backdate(todo.id)
        await self._policy()

        spawned: list[dict] = []

        async def fake_spawn(role, project_id=None, task_id=None):
            spawned.append({"role": role, "project_id": project_id,
                            "task_id": task_id})
            return {"id": "agent-coder-99", "role": role,
                    "project_id": project_id, "task_id": task_id,
                    "status": "spawning"}

        original = self.app.spawn_agent
        self.app.spawn_agent = fake_spawn
        try:
            report = await self.service.run_patrol(trigger="test")
        finally:
            self.app.spawn_agent = original

        self.assertEqual(report["error"], "")
        idle = [item for item in report["findings"]
                if item["check"] == "task.idle" and item.get("task_id") == todo.id]
        self.assertTrue(idle, report["findings"])
        dispatched = [record for record in report["dispatched"]
                      if record.get("task_id") == todo.id
                      and record.get("status") == "ok"]
        self.assertTrue(dispatched, report["dispatched"])
        self.assertEqual(dispatched[0]["role"], todo.role or "coder")
        self.assertEqual(spawned[0]["task_id"], todo.id)
        self.assertEqual(spawned[0]["project_id"], project_id)
        types = [record.type for record in self.app.eventlog.query(
            types=["autonomy"], limit=200)]
        self.assertIn("autonomy.dispatched", types)
        self.assertEqual(self.service.status()["counts"]["dispatched"], 1)

    async def test_second_patrol_respects_cooldown(self) -> None:
        project_id, _ = await self._project()
        todo = next(task for task in self.app.projects.task_list(
            project_id=project_id) if task.status == "todo")
        self._backdate(todo.id)
        await self._policy(dispatch_cooldown_s=600.0)
        calls: list[str] = []

        async def fake_spawn(role, project_id=None, task_id=None):
            calls.append(task_id)
            return {"id": f"agent-coder-{len(calls)}", "role": role,
                    "project_id": project_id, "task_id": task_id}

        original = self.app.spawn_agent
        self.app.spawn_agent = fake_spawn
        try:
            await self.service.run_patrol(trigger="test")
            await self.service.run_patrol(trigger="test")
        finally:
            self.app.spawn_agent = original
        self.assertEqual(len(calls), 1, "cooldown must suppress a second spawn")

    async def test_patrol_dispatches_a_waiting_review(self) -> None:
        project_id, _ = await self._project()
        task = self.app.projects.task_list(project_id=project_id)[0]
        # Move it through the legal state machine to review.
        self.app.projects.task_update(task.id, {"status": "doing"})
        self.app.projects.task_update(task.id, {"status": "review"})
        self._backdate(task.id)
        await self._policy()

        spawned: list[dict] = []

        async def fake_spawn(role, project_id=None, task_id=None):
            spawned.append({"role": role, "project_id": project_id,
                            "task_id": task_id})
            return {"id": "agent-reviewer-99", "role": role,
                    "project_id": project_id, "task_id": task_id}

        original = self.app.spawn_agent
        self.app.spawn_agent = fake_spawn
        try:
            report = await self.service.run_patrol(trigger="test")
        finally:
            self.app.spawn_agent = original

        reviews = [item for item in report["findings"]
                   if item["check"] == "task.review"
                   and item.get("task_id") == task.id]
        self.assertTrue(reviews, report["findings"])
        self.assertEqual(reviews[0]["dispatch"]["role"], "reviewer")
        dispatched = [record for record in report["dispatched"]
                      if record.get("task_id") == task.id]
        self.assertTrue(dispatched, report["dispatched"])
        self.assertEqual(dispatched[0]["role"], "reviewer")
        self.assertEqual(spawned[0]["role"], "reviewer")
        self.assertEqual(spawned[0]["task_id"], task.id)

    async def test_guardian_approves_inside_workspace_only(self) -> None:
        project_id, workspace = await self._project()
        inside = self.app.approvals.request(
            "agent-coder-7", "fs.write",
            {"path": str(workspace / "src" / "ok.py"), "project_id": project_id,
             "bytes": 12},
            reason="write implementation", project_id=project_id)
        outside = self.app.approvals.request(
            "agent-coder-7", "fs.write",
            {"path": str(self.root / "escape.txt"), "project_id": project_id,
             "bytes": 12},
            reason="write outside", project_id=project_id)
        self.assertEqual(inside.status, "pending")
        self.assertEqual(outside.status, "pending")
        await self._policy(dispatch_mode="off")

        with patch("stackflowos.autonomy.patrol.run_checks", return_value=[]), \
                patch("stackflowos.autonomy.patrol.collect_snapshot",
                      return_value=Snapshot(now=1)):
            report = await self.service.run_patrol(trigger="test")

        inside_now = self.app.approvals.get(inside.id)
        outside_now = self.app.approvals.get(outside.id)
        self.assertEqual(inside_now.status, "approved", inside_now.note)
        self.assertEqual(inside_now.decided_by, "autonomy:guardian")
        self.assertEqual(inside_now.mode, "once")
        self.assertEqual(outside_now.status, "pending")
        allowed = [record for record in report["approvals"]
                   if record["decision"] == "allow"]
        self.assertEqual(len(allowed), 1)
        self.assertEqual(allowed[0]["request_id"], inside.id)
        skipped = [record for record in report["approvals"]
                   if record["request_id"] == outside.id
                   and record["decision"] == "skip"]
        self.assertEqual(len(skipped), 1)
        self.assertTrue(skipped[0]["reason"], "a skip must explain itself")
        types = [record.type for record in self.app.eventlog.query(
            types=["autonomy"], limit=100)]
        self.assertIn("autonomy.approval", types)
        self.assertEqual(self.service.status()["counts"]["approvals"], 1)

    async def test_guardian_can_approve_a_leads_spawn(self) -> None:
        project_id, _ = await self._project()
        request = self.app.approvals.request(
            "agent-lead-2", "agent.spawn",
            {"project_id": project_id, "role": "coder"},
            reason="hire a coder", project_id=project_id)
        self.assertEqual(request.status, "pending")
        await self._policy(dispatch_mode="off")
        with patch("stackflowos.autonomy.patrol.run_checks", return_value=[]), \
                patch("stackflowos.autonomy.patrol.collect_snapshot",
                      return_value=Snapshot(now=1)):
            await self.service.run_patrol(trigger="test")
        approved = self.app.approvals.get(request.id)
        self.assertEqual(approved.status, "approved", approved.note)
        self.assertEqual(approved.decided_by, "autonomy:guardian")

    async def test_guardian_off_and_disabled_do_not_decide(self) -> None:
        project_id, workspace = await self._project()
        request = self.app.approvals.request(
            "agent-coder-8", "fs.write",
            {"path": str(workspace / "x.py"), "project_id": project_id},
            project_id=project_id)
        await self._policy(approve_mode="off")
        with patch("stackflowos.autonomy.patrol.run_checks", return_value=[]):
            await self.service.run_patrol(trigger="test")
        self.assertEqual(self.app.approvals.get(request.id).status, "pending")
        # Master switch off must gate approve_mode=enforce too.
        await self._policy(enabled=False, approve_mode="enforce")
        with patch("stackflowos.autonomy.patrol.run_checks", return_value=[]):
            await self.service.run_patrol(trigger="test")
        self.assertEqual(self.app.approvals.get(request.id).status, "pending")

    async def test_observe_mode_records_without_deciding(self) -> None:
        project_id, workspace = await self._project()
        request = self.app.approvals.request(
            "agent-coder-9", "fs.write",
            {"path": str(workspace / "x.py"), "project_id": project_id},
            project_id=project_id)
        await self._policy(dispatch_mode="off", approve_mode="observe")
        with patch("stackflowos.autonomy.patrol.run_checks", return_value=[]), \
                patch("stackflowos.autonomy.patrol.collect_snapshot",
                      return_value=Snapshot(now=1)):
            report = await self.service.run_patrol(trigger="test")
        self.assertEqual(self.app.approvals.get(request.id).status, "pending")
        observed = [record for record in report["approvals"]
                    if record["decision"] == "observe"]
        self.assertEqual(len(observed), 1)

    async def test_policy_hot_update_changes_mode(self) -> None:
        await self._policy(dispatch_mode="tasks", approve_mode="observe")
        self.assertEqual(self.service.status()["policy"]["dispatch_mode"],
                         "tasks")
        await self.service.configure({"dispatch_mode": "off",
                                      "approve_mode": "off"})
        status = self.service.status()
        self.assertEqual(status["policy"]["dispatch_mode"], "off")
        self.assertEqual(status["policy"]["approve_mode"], "off")
        persisted = Config.load(root=self.root, state_dir=self.root / "state")
        self.assertEqual(persisted.autonomy.dispatch_mode, "off")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

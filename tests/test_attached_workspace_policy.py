"""Attached project workspaces are the real policy boundary.

Regression: risk used to be computed against
``<projects_root>/<project_id>/workspace`` for *every* project.  Attached
projects live at an operator-chosen path, so every read/write inside the real
workspace looked like it was outside the boundary and escalated to high risk
(one approval popup per read).  The engine now resolves the project's recorded
workspace; unknown ids still fail closed.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stackflowos.app import App
from stackflowos.config import Config
from stackflowos.kernel import Context
from stackflowos.l2.eventlog import EventLog
from stackflowos.projects import ProjectStore
from stackflowos.security import ApprovalBroker, PolicyEngine

TMP_ROOT = Path(__file__).resolve().parents[1] / ".stackflowos" / "tmp"


class AttachedWorkspacePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(TMP_ROOT))
        root = Path(self._tmp.name).resolve()
        self.root = root
        self.projects_root = root / "projects"
        self.external = root / "attached"
        self.external.mkdir(parents=True)
        (self.external / "brief.md").write_text("# brief\n", "utf-8")
        self.ctx = Context(name="test")
        self.eventlog = EventLog(root / "events")
        self.projects = ProjectStore(
            self.ctx,
            state_path=root / "state" / "projects.json",
            counter_path=root / "state" / "counters.json",
            workspace_root=self.projects_root,
        )
        self.project = self.projects.create(
            "attached", "ship it", workspace=self.external,
            workspace_mode="attached-rw")
        self.policy = PolicyEngine(
            self.ctx, self.eventlog,
            rules_path=root / "security" / "rules.json",
            projects_root=self.projects_root,
            workspace_resolver=self._workspace,
        )
        self.broker = ApprovalBroker(
            self.ctx, self.policy, self.eventlog,
            approvals_path=root / "security" / "approvals.json",
            approval_ttl_s=300, grant_ttl_s=3600)

    async def asyncTearDown(self) -> None:
        await self.ctx.dispose()
        await self.eventlog.close()
        self._tmp.cleanup()

    def _workspace(self, project_id: str) -> str:
        return self.projects.get(project_id).workspace

    def test_reads_inside_attached_workspace_are_low_risk(self) -> None:
        read = {"project_id": self.project.id,
                "path": str(self.external / "brief.md")}
        self.assertEqual(self.policy.risk("fs.read", read), "low")
        decision = self.policy.evaluate("agent-coder-1", "fs.read", read)
        self.assertEqual(decision.outcome, "allow", decision.reason)

        listing = {"project_id": self.project.id, "path": str(self.external)}
        self.assertEqual(self.policy.risk("fs.list", listing), "low")
        self.assertEqual(
            self.policy.evaluate("agent-coder-1", "fs.list", listing).outcome,
            "allow")

    def test_writes_inside_attached_workspace_need_one_approval(self) -> None:
        write = {"project_id": self.project.id,
                 "path": str(self.external / "artifacts" / "out.md")}
        self.assertEqual(self.policy.risk("fs.write", write), "medium")
        self.assertEqual(
            self.policy.evaluate("agent-coder-1", "fs.write", write).outcome,
            "ask")

    def test_outside_attached_workspace_stays_high_risk(self) -> None:
        outside = {"project_id": self.project.id, "path": "/etc/passwd"}
        self.assertEqual(self.policy.risk("fs.read", outside), "high")
        self.assertEqual(
            self.policy.evaluate("agent-coder-1", "fs.read", outside).outcome,
            "ask")

    def test_unknown_project_fails_closed(self) -> None:
        unknown = {"project_id": "p-999",
                   "path": str(self.external / "brief.md")}
        # No resolver answer -> managed path that does not exist; the external
        # directory is outside it, so the request must not become low risk.
        self.assertEqual(self.policy.risk("fs.read", unknown), "high")

    def test_approval_request_auto_resolves_attached_read(self) -> None:
        request = self.broker.request(
            principal="agent-coder-1", capability="fs.read",
            scope={"project_id": self.project.id,
                   "path": str(self.external / "brief.md")},
            project_id=self.project.id)
        self.assertEqual(request.status, "approved")
        self.assertEqual(request.mode, "auto")
        self.assertEqual(request.risk, "low")

    def test_standing_grant_uses_attached_workspace_prefix(self) -> None:
        request = self.broker.request(
            principal="agent-coder-1", capability="fs.write",
            scope={"project_id": self.project.id,
                   "path": str(self.external / "artifacts" / "out.md")},
            project_id=self.project.id)
        self.assertEqual(request.status, "pending")
        self.broker.decide(request.id, decision="allow", mode="always",
                           by="tester")
        rules = [rule for rule in self.policy.list_rules(active_only=True)
                 if rule.capability == "fs.write"]
        self.assertTrue(rules)
        self.assertEqual(rules[-1].scope.get("path_prefix"),
                         str(self.external.resolve()))


class AppAttachedPolicyWiringTests(unittest.IsolatedAsyncioTestCase):
    """The hub must wire the resolver into the live policy engine."""

    async def asyncSetUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(TMP_ROOT))
        self.root = Path(self._tmp.name).resolve()
        self.app = App(Config.load(root=self.root,
                                   state_dir=self.root / "state"))
        await self.app.start()
        self.external = self.root / "repo"
        self.external.mkdir()
        (self.external / "brief.md").write_text("# brief\n", "utf-8")

    async def asyncTearDown(self) -> None:
        await self.app.stop()
        self._tmp.cleanup()

    async def test_attached_read_is_auto_allowed_by_live_policy(self) -> None:
        card = await self.app.attach_project(
            "self", str(self.external), allow_writes=True)
        project_id = card["id"]
        self.assertEqual(
            self.app.policy.risk("fs.read", {
                "project_id": project_id,
                "path": str(self.external / "brief.md"),
            }),
            "low")
        self.assertEqual(
            self.app.policy.evaluate("agent-coder-1", "fs.read", {
                "project_id": project_id,
                "path": str(self.external / "brief.md"),
            }).outcome,
            "allow")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

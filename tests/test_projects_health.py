"""Project health checks: scope, layout, board, memory, permissions, audit."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from stackflowos.projects.health import build_report
from stackflowos.projects.models import Project, WorkspaceMode
from stackflowos.projects.onboarding import (ensure_scaffold, memory_seed,
                                             seed_task_spec)

TMP_ROOT = Path(__file__).resolve().parents[1] / ".stackflowos" / "tmp"


class FakeDecision:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome


class FakePolicy:
    def __init__(self, outcomes: dict[str, str] | None = None) -> None:
        self.outcomes = outcomes or {}

    def evaluate(self, principal, capability, scope, **kwargs):
        return FakeDecision(self.outcomes.get(capability, "allow"))


class FakeEventLog:
    def __init__(self, writable: bool = True, last_seq: int = 7) -> None:
        self.writable = writable
        self.last_seq = last_seq

    def health(self):
        return {"writable": self.writable, "last_seq": self.last_seq}


def _task(status: str = "todo", title: str = "实现功能") -> dict:
    return {"id": "t-1", "title": title, "status": status, "role": "coder"}


class HealthReportTests(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="health-", dir=TMP_ROOT))
        self.ws = self.tmp / "workspace"
        self.ws.mkdir()
        self.project = Project(
            id="p-1", name="Demo", goal="ship a tiny tool", status="active",
            workspace=str(self.ws), workspace_mode=WorkspaceMode.MANAGED)
        self.tasks = [_task(), dict(_task(title="入职自检：复述目标"))]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _report(self, **kwargs):
        defaults = dict(
            memory=memory_seed(self.project),
            policy=FakePolicy(),
            agents=[],
            agent=None,
            security=None,
            paths=None,
            eventlog=FakeEventLog(),
        )
        defaults.update(kwargs)
        return build_report(self.project, self.tasks, **defaults)

    def test_empty_house_is_not_ready(self) -> None:
        empty = Project(id="p-2", name="Empty", goal="", status="planning",
                        workspace=str(self.ws),
                        workspace_mode=WorkspaceMode.MANAGED)
        report = build_report(empty, [], memory={}, policy=FakePolicy(
            {"fs.read": "ask"}), agents=[], eventlog=FakeEventLog())
        self.assertFalse(report["ready"])
        self.assertEqual(report["status"], "not_ready")
        self.assertTrue(report["not_ready_reasons"])
        ids = {check["id"] for check in report["checks"]}
        self.assertIn("workspace.layout", ids)
        self.assertIn("board.tasks", ids)
        self.assertIn("policy.reads", ids)
        self.assertIn("project.goal", ids)

    def test_onboarded_house_is_ready(self) -> None:
        ensure_scaffold(self.project)
        report = self._report()
        self.assertTrue(report["ready"], report["not_ready_reasons"])
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["summary"]["fail"], 0)
        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual(checks["workspace.path"]["status"], "ok")
        self.assertEqual(checks["workspace.layout"]["status"], "ok")
        self.assertEqual(checks["board.tasks"]["status"], "ok")
        self.assertEqual(checks["memory.seed"]["status"], "ok")
        self.assertEqual(checks["policy.reads"]["status"], "ok")
        self.assertEqual(checks["eventlog.audit"]["status"], "ok")

    def test_missing_brief_fails_a_managed_workspace(self) -> None:
        report = self._report()
        self.assertFalse(report["ready"])
        layout = next(check for check in report["checks"]
                      if check["id"] == "workspace.layout")
        self.assertEqual(layout["status"], "fail")
        self.assertIn("brief.md", layout["message"])

    def test_attached_read_only_missing_layout_only_warns(self) -> None:
        attached = Project(
            id="p-3", name="Repo", goal="keep", status="active",
            workspace=str(self.ws), workspace_mode=WorkspaceMode.ATTACHED_RO,
            external=True)
        report = build_report(
            attached, [_task(title="入职自检：x")], memory=memory_seed(attached),
            policy=FakePolicy(), agents=[], eventlog=FakeEventLog())
        layout = next(check for check in report["checks"]
                      if check["id"] == "workspace.layout")
        self.assertEqual(layout["status"], "warn")
        self.assertTrue(report["ready"], report["not_ready_reasons"])

    def test_read_that_needs_approval_is_not_ready(self) -> None:
        ensure_scaffold(self.project)
        report = self._report(policy=FakePolicy({"fs.read": "ask"}))
        self.assertFalse(report["ready"])
        check = next(item for item in report["checks"]
                     if item["id"] == "policy.reads")
        self.assertEqual(check["status"], "fail")
        self.assertIn("fs.read=ask", check["message"])

    def test_denied_write_is_not_ready(self) -> None:
        ensure_scaffold(self.project)
        report = self._report(policy=FakePolicy({"fs.write": "deny"}))
        self.assertFalse(report["ready"])
        check = next(item for item in report["checks"]
                     if item["id"] == "policy.writes")
        self.assertEqual(check["status"], "fail")

    def test_unbound_agent_is_a_fail(self) -> None:
        ensure_scaffold(self.project)
        agent = {"id": "agent-assistant-1", "role": "assistant",
                 "project_id": None, "status": "blocked",
                 "error": "未绑定项目", "error_code": "PROJECT_UNBOUND"}
        report = self._report(agent=agent)
        self.assertFalse(report["ready"])
        binding = next(item for item in report["checks"]
                       if item["id"] == "agent.binding")
        self.assertEqual(binding["status"], "fail")
        state = next(item for item in report["checks"]
                     if item["id"] == "agent.state")
        self.assertEqual(state["status"], "warn")

    def test_empty_board_fails_and_names_the_seed_task(self) -> None:
        ensure_scaffold(self.project)
        report = build_report(
            self.project, [], memory=memory_seed(self.project),
            policy=FakePolicy(), agents=[], eventlog=FakeEventLog())
        board = next(item for item in report["checks"]
                     if item["id"] == "board.tasks")
        self.assertEqual(board["status"], "fail")
        self.assertFalse(report["ready"])

    def test_missing_seed_task_is_a_warning(self) -> None:
        ensure_scaffold(self.project)
        report = build_report(
            self.project, [_task(title="普通任务")],
            memory=memory_seed(self.project), policy=FakePolicy(),
            agents=[], eventlog=FakeEventLog())
        check = next(item for item in report["checks"]
                     if item["id"] == "board.onboarding_task")
        self.assertEqual(check["status"], "warn")
        self.assertTrue(report["ready"])

    def test_offline_missing_services_are_unchecked_not_failures(self) -> None:
        ensure_scaffold(self.project)
        report = build_report(
            self.project, self.tasks, memory=None, policy=None, agents=None,
            eventlog=None)
        self.assertTrue(report["ready"], report["not_ready_reasons"])
        unchecked = [item for item in report["checks"]
                     if not item["checked"]]
        self.assertGreaterEqual(len(unchecked), 4)
        self.assertTrue(all(item["status"] == "warn" for item in unchecked))

    def test_stale_brief_is_a_warning(self) -> None:
        ensure_scaffold(self.project)
        (self.ws / "brief.md").write_text("# old goal\n", "utf-8")
        report = self._report()
        check = next(item for item in report["checks"]
                     if item["id"] == "project.goal")
        self.assertEqual(check["status"], "warn")
        self.assertIn("过期", check["message"])

    def test_blocked_agent_is_reported_with_reason(self) -> None:
        ensure_scaffold(self.project)
        agents = [{"id": "agent-coder-1", "status": "blocked",
                   "error": "workspace missing", "error_code": "WORKSPACE_MISSING"}]
        report = self._report(agents=agents)
        check = next(item for item in report["checks"]
                     if item["id"] == "agents.active")
        self.assertEqual(check["status"], "warn")
        self.assertIn("workspace missing", check["message"])

    def test_unwritable_sample_workspace_fails_hard(self) -> None:
        ensure_scaffold(self.project)
        report = self._report(eventlog=FakeEventLog(writable=False))
        self.assertFalse(report["ready"])
        check = next(item for item in report["checks"]
                     if item["id"] == "eventlog.audit")
        self.assertEqual(check["status"], "fail")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

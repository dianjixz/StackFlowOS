"""Tests for the agent run loop (claim, brain/tool cycle, finish, flags)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from stackflowos.agents.brain import Action, AgentView, MockBrain
from stackflowos.agents.models import Agent
from stackflowos.agents.runner import AgentRunner
from stackflowos.agents.tools import ToolExecutor, ToolResult
from stackflowos.config import AgentsCfg, SecurityCfg
from stackflowos.errors import NotFound

TMP_ROOT = Path(__file__).resolve().parent.parent / ".stackflowos" / "tmp"


class FakeSysAPI:
    """In-memory sys: project/task store, approvals, event log."""

    def __init__(self, *, task: dict | None, project: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.events: list[dict] = []
        self.task = dict(task) if task else None
        self.project = dict(project or {
            "id": "p-1", "name": "Demo", "goal": "写一个 hello 模块",
            "status": "active", "brief": "",
        })
        self.agent_updates: list[dict] = []
        self.finished: list[dict] = []
        self.created: list[dict] = []

    async def call(self, action: str, data: dict | None = None, *,
                   timeout: float | None = None) -> dict:
        payload = dict(data or {})
        self.calls.append((action, payload))
        if action == "event.append":
            self.events.append(payload)
            return {"seq": len(self.events)}
        if action == "event.since":
            return {"events": []}
        if action == "project.get":
            return dict(self.project)
        if action == "project.update":
            self.project.update(payload.get("patch") or {})
            return dict(self.project)
        if action == "task.get":
            if not self.task or self.task.get("id") != payload.get("task_id"):
                raise NotFound(f"task not found: {payload.get('task_id')}")
            return dict(self.task)
        if action == "task.claim":
            self.task["status"] = "doing"
            self.task["assignee"] = payload.get("agent_id")
            return dict(self.task)
        if action == "task.list":
            return {"tasks": [dict(self.task)] if self.task else []}
        if action == "task.update":
            self.task.update(payload.get("patch") or {})
            return dict(self.task)
        if action == "task.artifact":
            self.task.setdefault("artifacts", []).append(payload.get("artifact"))
            return dict(self.task)
        if action == "task.note":
            self.task.setdefault("notes", []).append(payload)
            return dict(self.task)
        if action == "task.create":
            created = {"id": f"t-{len(self.created) + 10}",
                       "status": "todo", "assignee": None,
                       **payload}
            self.created.append(created)
            return created
        if action == "agent.update":
            self.agent_updates.append(payload.get("patch") or {})
            return {"ok": True}
        if action == "agent.finish":
            self.finished.append(payload)
            return {"ok": True}
        if action in ("approval.request", "approval.status"):
            return {"id": "apr-1", "status": "approved", "mode": "rule",
                    "risk": "low", "grant_id": None}
        if action == "approval.use":
            return {"ok": True}
        if action == "memory.get":
            return {"value": None}
        if action == "memory.set":
            return {"ok": True}
        return {}

    def actions(self, name: str) -> list[dict]:
        return [data for action, data in self.calls if action == name]

    def event_types(self) -> list[str]:
        return [str(event.get("type")) for event in self.events]


class BusyBrain:
    """Always asks for another harmless tool call (never finishes)."""

    def __init__(self, tool: str = "fs.list") -> None:
        self.tool = tool

    async def next_action(self, view: AgentView) -> Action:
        return Action(tool=self.tool, args={"path": "."})


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        self.ws = self.tmp / "workspace"
        self.ws.mkdir()
        self.task = {
            "id": "t-1", "project_id": "p-1",
            "title": "实现 hello 函数",
            "description": "写一个 python 函数并做语法自检",
            "status": "todo", "role": "coder", "assignee": None,
            "artifacts": [], "notes": [],
        }
        self.sys = FakeSysAPI(task=self.task)
        self.sys.project["workspace"] = str(self.ws)
        self.agent = Agent(id="agent-coder-1", role="coder",
                           project_id="p-1", task_id="t-1")
        self.executor = ToolExecutor(
            self.sys, agent=self.agent, workspace=self.ws,
            security_config=SecurityCfg())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _runner(self, brain: Any, *, max_steps: int = 8) -> AgentRunner:
        return AgentRunner(
            self.sys, self.agent, brain, self.executor,
            config=AgentsCfg(max_steps=max_steps), workspace=self.ws)

    async def test_coder_runs_real_plan_and_finishes(self) -> None:
        runner = self._runner(MockBrain("coder", max_steps=8))
        result = await runner.run()

        self.assertEqual(result.status, "done", result.summary)
        self.assertLessEqual(result.steps, 8)
        self.assertEqual(self.sys.task["status"], "review")
        # Claim happened once, with this agent id.
        claims = self.sys.actions("task.claim")
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["agent_id"], "agent-coder-1")
        # A real artifact exists on disk.
        sources = list((self.ws / "src").glob("*.py"))
        self.assertEqual(len(sources), 1)
        self.assertIn("def run()", sources[0].read_text("utf-8"))
        # Artifact registered + finish reported.
        artifacts = self.sys.actions("task.artifact")
        self.assertEqual(artifacts[0]["artifact"]["path"],
                         str(sources[0].relative_to(self.ws)))
        self.assertEqual(len(self.sys.finished), 1)
        self.assertIn("t-1", self.sys.finished[0]["summary"])
        self.assertEqual(self.agent.status, "done")
        self.assertIsNone(self.agent.error)
        self.assertIsNone(self.agent.error_code)
        # Working state and step events were published.
        self.assertIn("working", [u.get("status") for u in self.sys.agent_updates])
        event_types = self.sys.event_types()
        self.assertIn("agent.working", event_types)
        self.assertIn("agent.step", event_types)
        self.assertIn("tool.result", event_types)
        self.assertIn("agent.finished", event_types)
        self.assertEqual(len(self.sys.actions("task.note")), 1)

    async def test_coder_self_check_failure_blocks_without_artifact(self) -> None:
        """S4: a failed self-check stops the run; nothing is submitted."""

        class FailingShellExecutor:
            """Real executor, but the coder's self-check shell always fails."""

            def __init__(self, inner: ToolExecutor) -> None:
                self.inner = inner
                self.workspace = inner.workspace
                self.calls: list[str] = []

            async def run(self, name: str,
                          args: dict | None = None) -> ToolResult:
                self.calls.append(name)
                if name == "shell.exec":
                    return ToolResult(ok=False,
                                      error="selftest failed: exit status 1")
                return await self.inner.run(name, args)

        executor = FailingShellExecutor(self.executor)
        runner = AgentRunner(
            self.sys, self.agent, MockBrain("coder", max_steps=8), executor,
            config=AgentsCfg(max_steps=8), workspace=self.ws)
        result = await runner.run()

        self.assertEqual(result.status, "blocked", result.summary)
        self.assertIn("自检失败", result.summary)
        self.assertEqual(self.sys.task["status"], "blocked")
        # The source file was written, but never registered or sent to review.
        self.assertEqual(len(list((self.ws / "src").glob("*.py"))), 1)
        self.assertEqual(self.sys.actions("task.artifact"), [])
        statuses = [(data.get("patch") or {}).get("status")
                    for data in self.sys.actions("task.update")]
        self.assertNotIn("review", statuses)
        self.assertNotIn("done", statuses)
        self.assertIn("shell.exec", executor.calls)
        self.assertEqual(len(self.sys.finished), 1)
        self.assertIn("agent.blocked", self.sys.event_types())
        self.assertNotIn("agent.failed", self.sys.event_types())
        self.assertIn("task.blocked", self.sys.event_types())
        self.assertNotIn("agent.finished", self.sys.event_types())
        self.assertEqual(self.agent.status, "blocked")
        self.assertEqual(self.agent.error_code, "PRECONDITION")
        self.assertTrue(self.agent.error)

    async def test_non_code_task_writes_markdown(self) -> None:
        self.sys.task["title"] = "整理发布说明"
        self.sys.task["description"] = "把变更写成文档"
        runner = self._runner(MockBrain("coder", max_steps=8))
        result = await runner.run()
        self.assertEqual(result.status, "done", result.summary)
        notes = list((self.ws / "notes").glob("*.md"))
        self.assertEqual(len(notes), 1)
        self.assertIn("整理发布说明", notes[0].read_text("utf-8"))
        commands = self.sys.actions("event.append")
        self.assertTrue(any(event.get("type") == "tool.result"
                            for event in commands))

    async def test_max_steps_blocks_task_and_reports_blocked(self) -> None:
        runner = self._runner(BusyBrain(), max_steps=2)
        result = await runner.run()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.steps, 2)
        self.assertIn("max_steps", result.summary)
        self.assertEqual(self.sys.task["status"], "blocked")
        self.assertEqual(len(self.sys.finished), 1)
        self.assertEqual(self.agent.status, "blocked")
        self.assertEqual(self.agent.error_code, "MAX_STEPS")
        self.assertIn("agent.blocked", self.sys.event_types())
        self.assertIn("task.blocked", self.sys.event_types())

    async def test_repeated_identical_failures_stop_the_run(self) -> None:
        class MissingFile:
            async def next_action(self, view: AgentView) -> Action:
                return Action(tool="fs.read", args={"path": "nope.txt"})

        result = await self._runner(MissingFile(), max_steps=20).run()
        self.assertEqual(result.status, "blocked")
        self.assertLess(result.steps, 20)
        self.assertEqual(self.sys.task["status"], "blocked")

    async def test_observe_receives_tool_results(self) -> None:
        seen: list[Any] = []

        class Watching:
            async def next_action(self, view: AgentView) -> Action:
                if seen:
                    return Action(finish={"summary": "done", "status": "done"})
                return Action(tool="fs.list", args={"path": "."})

            def observe(self, action: Action, result: Any) -> None:
                seen.append((action.tool, result.ok))

        await self._runner(Watching()).run()
        self.assertEqual(seen, [("fs.list", True)])

    async def test_pause_stops_without_finishing_agent(self) -> None:
        runner = self._runner(MockBrain("coder", max_steps=8))
        runner.pause()
        result = await runner.run()
        self.assertEqual(result.status, "paused")
        self.assertEqual(result.steps, 0)
        self.assertEqual(self.sys.finished, [])
        self.assertEqual(self.agent.status, "waiting")
        self.assertEqual(self.sys.task["status"], "doing")
        self.assertIn("agent.waiting", self.sys.event_types())

    async def test_kill_stops_before_work(self) -> None:
        runner = self._runner(MockBrain("coder", max_steps=8))
        runner.kill()
        result = await runner.run()
        self.assertEqual(result.status, "killed")
        self.assertEqual(result.steps, 0)
        self.assertEqual(self.agent.status, "dead")
        self.assertIn("agent.terminated", self.sys.event_types())

    async def test_programmer_error_in_tool_does_not_crash_runner(self) -> None:
        runner = self._runner(BusyBrain("fs.nope"), max_steps=4)
        result = await runner.run()
        self.assertEqual(result.status, "failed")
        self.assertIn("ToolError", result.summary)
        self.assertEqual(self.sys.task["status"], "blocked")
        self.assertEqual(self.agent.status, "failed")
        self.assertEqual(self.agent.error_code, "AGENT_CRASH")
        self.assertTrue(self.agent.error)

    async def test_claim_conflict_aborts_without_blocking_task(self) -> None:
        self.sys.task["status"] = "doing"
        self.sys.task["assignee"] = "agent-coder-9"
        # `_claim` only claims todo tasks; simulate a conflict by making the
        # task todo but refusing the claim.
        self.sys.task["status"] = "todo"

        async def conflict(action: str, data: dict | None = None, *,
                           timeout: float | None = None) -> dict:
            payload = dict(data or {})
            self.sys.calls.append((action, payload))
            if action == "project.get":
                return dict(self.sys.project)
            if action == "task.get":
                return dict(self.sys.task)
            if action == "task.claim":
                from stackflowos.errors import Conflict
                raise Conflict("already claimed")
            if action == "event.append":
                self.sys.events.append(payload)
                return {"seq": len(self.sys.events)}
            if action == "agent.finish":
                self.sys.finished.append(payload)
                return {}
            return {}

        runner = self._runner(BusyBrain())
        self.sys.call = conflict  # type: ignore[assignment]
        result = await runner.run()
        self.assertEqual(result.status, "blocked")
        self.assertIn("冲突", result.summary)
        self.assertEqual(len(self.sys.finished), 1)
        self.assertEqual(self.sys.task["status"], "todo")
        self.assertEqual(self.agent.status, "blocked")
        self.assertEqual(self.agent.error_code, "CLAIM_CONFLICT")

    async def test_unbound_project_is_blocked_with_explicit_reason(self) -> None:
        """No project scope is a precondition, not an execution failure."""
        agent = Agent(id="agent-assistant-1", role="assistant",
                      project_id=None, task_id=None)
        runner = AgentRunner(
            self.sys, agent, MockBrain("assistant", max_steps=4),
            self.executor, config=AgentsCfg(max_steps=4), workspace=self.ws)
        result = await runner.run()

        self.assertEqual(result.status, "blocked")
        self.assertIn("scope", result.summary)
        self.assertEqual(agent.status, "blocked")
        self.assertEqual(agent.error_code, "PROJECT_UNBOUND")
        self.assertTrue(agent.error)
        self.assertEqual(self.sys.actions("task.claim"), [])
        events = [event for event in self.sys.events
                  if event.get("type") == "agent.blocked"]
        self.assertTrue(events)
        self.assertEqual(events[-1]["data"].get("error_code"),
                         "PROJECT_UNBOUND")

    async def test_missing_workspace_is_blocked_with_explicit_reason(self) -> None:
        self.sys.project["workspace"] = str(self.tmp / "does-not-exist")
        result = await self._runner(MockBrain("coder", max_steps=4)).run()

        self.assertEqual(result.status, "blocked")
        self.assertIn("工作区", result.summary)
        self.assertEqual(self.agent.status, "blocked")
        self.assertEqual(self.agent.error_code, "WORKSPACE_MISSING")
        self.assertEqual(self.sys.actions("task.claim"), [])

    async def test_missing_task_is_blocked_not_failed(self) -> None:
        self.sys.task = None
        result = await self._runner(MockBrain("coder", max_steps=4)).run()

        self.assertEqual(result.status, "blocked")
        self.assertIn("不存在", result.summary)
        self.assertEqual(self.agent.status, "blocked")
        self.assertEqual(self.agent.error_code, "TASK_MISSING")
        self.assertIn("agent.blocked", self.sys.event_types())
        self.assertNotIn("agent.failed", self.sys.event_types())



if __name__ == "__main__":  # pragma: no cover
    unittest.main()

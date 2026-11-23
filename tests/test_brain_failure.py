"""S4: MockBrain must stop the coder when its self-check fails.

The runner executes one action at a time and reports the outcome through
``brain.observe``.  The coder keeps its deterministic, plan-driven design but
uses that outcome to decide whether a pending ``shell.exec`` self-check passed;
a failure terminates the task as ``blocked`` instead of registering a bad
artifact and moving it to review.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from stackflowos.agents.brain import Action, AgentView, MockBrain
from stackflowos.agents.tools import ToolResult


def _agent() -> SimpleNamespace:
    return SimpleNamespace(id="agent-test", role="coder", project_id="p-1",
                           task_id="t-1")


def _task(**overrides) -> dict:
    task = {
        "id": "t-1", "project_id": "p-1", "status": "todo",
        "title": "实现 hello 函数",
        "description": "写一个 python 函数并做语法自检",
        "role": "coder", "assignee": None, "artifacts": [], "notes": [],
    }
    task.update(overrides)
    return task


def _view(step: int, task: dict, *, tasks: list[dict] | None = None,
          project: dict | None = None) -> AgentView:
    return AgentView(
        agent=_agent(),
        project=project or {"id": "p-1", "name": "Demo", "goal": "写一个 hello 模块"},
        task=task,
        tasks=tasks if tasks is not None else [task],
        step=step,
    )


async def _drive(brain: MockBrain, task: dict, outcomes: list[ToolResult],
                 *, max_steps: int = 6) -> list[Action]:
    """Call ``next_action`` and feed one scripted outcome per tool call.

    Recording stops at the first finish action (the runner does the same).
    The view step mirrors the runner: it advances only after a tool call.
    """
    actions: list[Action] = []
    step = 0
    for outcome in outcomes:
        action = await brain.next_action(_view(step, task))
        actions.append(action)
        if action.finish is not None:
            return actions
        if action.tool:
            brain.observe(action, outcome)
            # Mimic the sys store so the next view reflects task.update.
            if action.tool == "task.update":
                task.update(action.args.get("patch") or {})
            step += 1
    action = await brain.next_action(_view(step, task))
    actions.append(action)
    return actions


class MockBrainObserveTests(unittest.IsolatedAsyncioTestCase):
    async def test_observe_records_last_tool_outcome(self) -> None:
        brain = MockBrain("coder")
        brain.observe(Action(tool="shell.exec", args={"command": "false"}),
                      ToolResult(ok=False, error="exit status 1"))
        self.assertEqual(brain._last_tool, "shell.exec")
        self.assertFalse(brain._last_ok)
        self.assertEqual(brain._last_error, "exit status 1")

        brain.observe(Action(tool="fs.write"), ToolResult(ok=True))
        self.assertEqual(brain._last_tool, "fs.write")
        self.assertTrue(brain._last_ok)
        self.assertEqual(brain._last_error, "")

    async def test_coder_self_check_failure_blocks_before_artifact(self) -> None:
        brain = MockBrain("coder")
        task = _task()
        actions = await _drive(brain, task, [
            ToolResult(ok=True),                       # task.update -> doing
            ToolResult(ok=True),                       # fs.write source
            ToolResult(ok=False, error="selftest failed: exit status 1"),
        ])

        finish = actions[-1]
        self.assertIsNotNone(finish.finish)
        self.assertEqual(finish.finish["status"], "blocked")
        self.assertIn("自检失败", finish.finish["summary"])
        self.assertIn("selftest failed", finish.finish["summary"])
        self.assertIn("产物未提交", finish.finish["summary"])
        self.assertIsNone(finish.tool)

        tools = [action.tool for action in actions[:-1]]
        self.assertEqual(tools, ["task.update", "fs.write", "shell.exec"])
        self.assertNotIn("task.artifact", tools)
        self.assertFalse(brain._compiled)
        self.assertTrue(brain._coder_blocked)

        # A failed coder stays stopped: no artifact/review even if called again.
        again = await brain.next_action(_view(3, task))
        self.assertIsNotNone(again.finish)
        self.assertEqual(again.finish["status"], "blocked")
        self.assertEqual(again.tool, None)

    async def test_coder_self_check_success_continues_to_artifact_and_review(self) -> None:
        brain = MockBrain("coder")
        task = _task()
        actions = await _drive(brain, task, [
            ToolResult(ok=True),   # task.update -> doing
            ToolResult(ok=True),   # fs.write source
            ToolResult(ok=True),   # shell.exec self-check
            ToolResult(ok=True),   # task.artifact
            ToolResult(ok=True),   # task.update -> review
        ])

        tools = [action.tool for action in actions]
        self.assertEqual(tools, ["task.update", "fs.write", "shell.exec",
                                 "task.artifact", "task.update", None])
        self.assertEqual(actions[3].args["artifact"]["path"],
                         brain._artifact_path)
        self.assertEqual(actions[4].args["patch"]["status"], "review")
        self.assertEqual(actions[-1].finish["status"], "done")
        self.assertTrue(brain._compiled)

    async def test_non_python_artifact_uses_true_and_continues(self) -> None:
        brain = MockBrain("coder")
        task = _task(title="整理发布说明", description="把变更写成文档",
                     status="doing")
        actions = await _drive(brain, task, [
            ToolResult(ok=True),   # fs.write notes/*.md
            ToolResult(ok=True),   # shell.exec "true"
            ToolResult(ok=True),   # task.artifact
            ToolResult(ok=True),   # task.update -> review
        ])

        shell = actions[1]
        self.assertEqual(shell.tool, "shell.exec")
        self.assertEqual(shell.args["command"], "true")
        self.assertTrue(actions[0].args["path"].endswith(".md"))
        self.assertEqual(actions[2].tool, "task.artifact")
        self.assertEqual(actions[3].args["patch"]["status"], "review")
        self.assertEqual(actions[-1].finish["status"], "done")

    async def test_missing_self_check_observation_blocks(self) -> None:
        brain = MockBrain("coder")
        task = _task(status="doing")
        # Drive to the shell action, then skip its observation (never executed).
        written = await brain.next_action(_view(0, task))
        self.assertEqual(written.tool, "fs.write")
        brain.observe(written, ToolResult(ok=True))
        shell = await brain.next_action(_view(1, task))
        self.assertEqual(shell.tool, "shell.exec")

        action = await brain.next_action(_view(2, task))
        self.assertIsNotNone(action.finish)
        self.assertEqual(action.finish["status"], "blocked")
        self.assertIn("自检命令未执行", action.finish["summary"])
        self.assertEqual(action.tool, None)
        self.assertFalse(brain._compiled)


class ObserveIsolationTests(unittest.IsolatedAsyncioTestCase):
    """``observe`` must not change plan-driven roles other than the coder."""

    ROLES = ("lead", "architect", "tester", "reviewer", "researcher",
             "aggregator", "assistant")

    async def _sequence(self, role: str, *, observe: bool) -> list[dict]:
        brain = MockBrain(role, max_steps=20)
        task = _task(status="doing", role=role,
                     artifacts=[{"type": "file", "path": "src/t-1-impl.py",
                                 "title": "产物"}])
        project = {"id": "p-1", "name": "Demo", "goal": "写一个 hello 模块",
                   "status": "active"}
        seen: list[dict] = []
        for step in range(6):
            view = _view(step, task, tasks=[task], project=project)
            action = await brain.next_action(view)
            seen.append(action.to_dict())
            if observe:
                brain.observe(Action(tool="shell.exec", args={"command": "false"}),
                              ToolResult(ok=False, error="boom"))
        return seen

    async def test_observe_does_not_change_other_roles(self) -> None:
        for role in self.ROLES:
            with self.subTest(role=role):
                plain = await self._sequence(role, observe=False)
                observed = await self._sequence(role, observe=True)
                self.assertEqual(plain, observed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

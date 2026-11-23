"""S4/S5: MockBrain must fail closed when a tool outcome says it failed.

S4: the coder stops when its ``shell.exec`` self-check fails instead of
registering a bad artifact and moving the task to review.
S5: the reviewer settles its ``fs.read`` on the next step; a failed read
writes a failure note and returns the task to ``todo`` (blocked) instead of
writing a "pass" verdict.  Successful reads and project-level reviews keep
their existing behaviour.
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
                 *, tasks: list[dict] | None = None,
                 max_steps: int = 6) -> list[Action]:
    """Call ``next_action`` and feed one scripted outcome per tool call.

    Recording stops at the first finish action (the runner does the same).
    The view step mirrors the runner: it advances only after a tool call.
    """
    actions: list[Action] = []
    step = 0
    for outcome in outcomes:
        action = await brain.next_action(_view(step, task, tasks=tasks))
        actions.append(action)
        if action.finish is not None:
            return actions
        if action.tool:
            brain.observe(action, outcome)
            # Mimic the sys store so the next view reflects task.update.
            if action.tool == "task.update":
                task.update(action.args.get("patch") or {})
            step += 1
    action = await brain.next_action(_view(step, task, tasks=tasks))
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


class ReviewerArtifactReadTests(unittest.IsolatedAsyncioTestCase):
    """S5: a failed artifact read must reject the review, not fake a pass."""

    def _review_task(self, **overrides) -> dict:
        task = _task(
            status="review", role="reviewer",
            title="评审实现",
            artifacts=[{"type": "file", "path": "src/t-1-impl.py",
                        "title": "实现产物"}])
        task.update(overrides)
        return task

    async def test_read_failure_returns_task_to_todo_and_blocks(self) -> None:
        brain = MockBrain("reviewer")
        task = self._review_task()
        actions = await _drive(brain, task, [
            ToolResult(ok=True),                                  # fs.list
            ToolResult(ok=False,
                       error="Permission denied: src/t-1-impl.py"),
            ToolResult(ok=True),                                  # task.note
            ToolResult(ok=True),                                  # task.update -> todo
        ])

        tools = [action.tool for action in actions]
        # The read is issued once and settled before any verdict.
        self.assertEqual(tools[:3], ["fs.list", "fs.read", "task.note"])
        self.assertEqual(tools.count("fs.read"), 1)
        self.assertNotIn("task.artifact", tools)

        note = actions[2]
        self.assertEqual(note.tool, "task.note")
        self.assertIn("读取产物失败", note.args["text"])
        self.assertIn("Permission denied", note.args["text"])

        updates = [action for action in actions if action.tool == "task.update"]
        self.assertTrue(updates)
        statuses = [action.args.get("patch", {}).get("status")
                    for action in updates]
        self.assertEqual(statuses[-1], "todo")
        self.assertNotIn("done", statuses)
        self.assertNotIn("review", statuses)

        # No "pass" review may be written.
        self.assertFalse(any(
            action.tool == "fs.write"
            and "结论：通过" in str(action.args.get("content") or "")
            for action in actions))

        finish = actions[-1]
        self.assertIsNotNone(finish.finish)
        self.assertEqual(finish.finish["status"], "blocked")
        self.assertIn("读取产物失败", finish.finish["summary"])
        self.assertFalse(brain._review_read_pending)
        self.assertTrue(brain._review_read_failed)
        self.assertTrue(brain._missing_artifacts)

    async def test_read_success_keeps_the_normal_pass_verdict(self) -> None:
        brain = MockBrain("reviewer")
        task = self._review_task()
        actions = await _drive(brain, task, [ToolResult(ok=True)] * 5)

        tools = [action.tool for action in actions]
        self.assertEqual(tools, ["fs.list", "fs.read", "fs.write",
                                 "task.artifact", "task.update", None])
        self.assertEqual(tools.count("fs.read"), 1)
        review = actions[2]
        self.assertIn("结论：通过", review.args["content"])

        updates = [action for action in actions if action.tool == "task.update"]
        self.assertEqual(updates[-1].args["patch"]["status"], "done")
        finish = actions[-1]
        self.assertEqual(finish.finish["status"], "done")
        self.assertFalse(brain._review_read_pending)
        self.assertFalse(brain._review_read_failed)
        self.assertFalse(brain._missing_artifacts)

    async def test_project_level_review_without_artifact_still_passes(self) -> None:
        brain = MockBrain("reviewer")
        task = _task(id="t-review", status="review", role="reviewer",
                     title="项目级验收", artifacts=[])
        siblings = [
            _task(id="t-1", status="done"),
            _task(id="t-2", status="review"),
        ]
        actions = await _drive(brain, task, [ToolResult(ok=True)] * 5,
                               tasks=[task, *siblings])

        tools = [action.tool for action in actions]
        self.assertNotIn("fs.read", tools)
        project_note = next(action for action in actions
                            if action.tool == "task.note")
        self.assertIn("项目级验收", project_note.args["text"])

        review = next(action for action in actions if action.tool == "fs.write")
        self.assertIn("结论：通过", review.args["content"])
        updates = [action for action in actions if action.tool == "task.update"]
        self.assertEqual(updates[-1].args["patch"]["status"], "done")
        finish = actions[-1]
        self.assertEqual(finish.finish["status"], "done")
        self.assertFalse(brain._review_read_failed)
        self.assertFalse(brain._missing_artifacts)


    async def test_claimed_project_level_review_closes_via_review_status(self) -> None:
        """A review task claimed from todo must pass through review to done."""
        brain = MockBrain("reviewer")
        task = _task(id="t-review", status="doing", role="reviewer",
                     title="项目级验收", artifacts=[])
        siblings = [_task(id="t-1", status="done"),
                    _task(id="t-2", status="done")]
        actions = await _drive(brain, task, [ToolResult(ok=True)] * 6,
                               tasks=[task, *siblings])

        updates = [action for action in actions if action.tool == "task.update"]
        self.assertEqual([action.args["patch"]["status"] for action in updates],
                         ["review", "done"])
        finish = actions[-1]
        self.assertEqual(finish.finish["status"], "done")
        self.assertFalse(brain._missing_artifacts)


class ObserveIsolationTests(unittest.IsolatedAsyncioTestCase):
    """``observe`` must not change plan-driven roles that do not use outcomes.

    The coder (S4) and reviewer (S5) deliberately consume observed tool
    outcomes; their behaviour is covered by the tests above.  Every other
    role must keep producing the same plan with or without observations.
    """

    ROLES = ("lead", "architect", "tester", "researcher",
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

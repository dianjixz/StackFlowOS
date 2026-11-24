"""Task timeout, tool-call cap and live config reload for agent workers."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from stackflowos.agents.brain import Action, AgentView
from stackflowos.agents.hotreload import (ConfigReloader, apply_runtime_config,
                                          public_runtime_config)
from stackflowos.agents.models import Agent
from stackflowos.agents.runner import AgentRunner
from stackflowos.agents.supervisor import AgentSupervisor
from stackflowos.agents.tools import ToolExecutor
from stackflowos.config import (AgentsCfg, Config, LLMCfg, SecurityCfg)
from stackflowos.kernel.context import Context
from tests.test_agent_runner import FakeSysAPI

TMP_ROOT = Path(__file__).resolve().parent.parent / ".stackflowos" / "tmp"


class SlowBrain:
    async def next_action(self, view: AgentView) -> Action:
        await asyncio.sleep(5)
        return Action(finish={"summary": "too late", "status": "done"})


class ToolLimitBrain:
    """Never finishes; asks for another harmless tool call forever."""

    async def next_action(self, view: AgentView) -> Action:
        return Action(tool="fs.list", args={"path": "."})


class RunnerLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        self.ws = self.tmp / "workspace"
        self.ws.mkdir()
        self.task = {"id": "t-1", "project_id": "p-1", "title": "limits",
                     "description": "x", "status": "todo", "role": "coder",
                     "artifacts": [], "notes": []}
        self.sys = FakeSysAPI(task=self.task)
        self.agent = Agent(id="agent-coder-1", role="coder",
                           project_id="p-1", task_id="t-1")
        self.executor = ToolExecutor(
            self.sys, agent=self.agent, workspace=self.ws,
            security_config=SecurityCfg())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _runner(self, brain, *, cfg: AgentsCfg) -> AgentRunner:
        return AgentRunner(self.sys, self.agent, brain, self.executor,
                           config=cfg, workspace=self.ws)

    async def test_task_timeout_blocks_and_reports_reason(self) -> None:
        runner = self._runner(SlowBrain(), cfg=AgentsCfg(task_timeout_s=0.05))
        result = await runner.run()
        self.assertEqual(result.status, "blocked")
        self.assertIn("超时", result.summary)
        self.assertIn("task_timeout_s", result.summary)
        self.assertEqual(self.sys.task["status"], "blocked")
        failed = [event for event in self.sys.events
                  if event.get("type") == "agent.failed"]
        self.assertTrue(failed)
        self.assertTrue(failed[-1]["data"].get("timeout"))
        self.assertEqual(failed[-1]["data"].get("task_timeout_s"), 0.05)
        self.assertEqual(self.agent.status, "failed")

    async def test_tool_call_cap_stops_the_run(self) -> None:
        cfg = AgentsCfg(max_steps=50, task_timeout_s=60,
                        max_tool_calls_per_task=2)
        runner = self._runner(ToolLimitBrain(), cfg=cfg)
        result = await runner.run()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.steps, 2)
        self.assertIn("max_tool_calls_per_task", result.summary)
        self.assertEqual(self.sys.task["status"], "blocked")
        failed = [event for event in self.sys.events
                  if event.get("type") == "agent.failed"]
        self.assertTrue(failed)
        self.assertEqual(failed[-1]["data"].get("limit"),
                         "max_tool_calls_per_task")

    async def test_reload_limits_applies_live_and_mid_run(self) -> None:
        cfg = AgentsCfg(max_steps=50, task_timeout_s=60,
                        max_tool_calls_per_task=50)
        runner = self._runner(ToolLimitBrain(), cfg=cfg)
        before = runner.reload_limits()
        self.assertEqual(before["max_tool_calls_per_task"], 50)

        class Shrinking(ToolLimitBrain):
            async def next_action(self, view: AgentView) -> Action:
                # Simulate a pushed config shrinking the limit after step 1.
                runner.config.max_tool_calls_per_task = 1
                runner.reload_limits()
                return await super().next_action(view)

        runner = self._runner(Shrinking(), cfg=cfg)
        result = await runner.run()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.steps, 1)
        self.assertIn("上限", result.summary)


class HotReloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_public_runtime_config_never_leaks_secrets(self) -> None:
        cfg = Config.load(root=self.tmp)
        cfg.llm.api_key = "super-secret"
        cfg.llm.routes = [{"role": "coder", "provider": "openai",
                           "model": "m1", "api_key": "route-secret"}]
        payload = public_runtime_config(cfg)
        self.assertNotIn("api_key", payload["llm"])
        self.assertEqual(payload["llm"]["model"], cfg.llm.model)
        route = payload["llm"]["routes"][0]
        self.assertEqual(route["model"], "m1")
        self.assertNotIn("api_key", route)
        self.assertIn("task_timeout_s", payload["agents"])

    def test_apply_runtime_config_keeps_existing_route_key(self) -> None:
        cfg = Config.load(root=self.tmp)
        cfg.llm.api_key = "primary-secret"
        cfg.llm.routes = [{"role": "coder", "provider": "openai",
                           "model": "old", "api_key": "route-secret"}]
        summary = apply_runtime_config(cfg, {
            "agents": {"task_timeout_s": 12.5, "max_tool_calls_per_task": 7},
            "llm": {"model": "new-model", "api_key": "***",
                    "routes": [{"role": "coder", "model": "new"}]},
        })
        self.assertEqual(cfg.agents.task_timeout_s, 12.5)
        self.assertEqual(cfg.agents.max_tool_calls_per_task, 7)
        self.assertEqual(cfg.llm.model, "new-model")
        self.assertEqual(cfg.llm.api_key, "primary-secret")
        self.assertEqual(cfg.llm.routes[0]["model"], "new")
        # The route's own key survives a non-secret route update.
        self.assertEqual(cfg.llm.routes[0]["api_key"], "route-secret")
        self.assertIn("agents.task_timeout_s", summary["applied"])
        self.assertTrue(summary["llm_changed"])

    async def test_config_reloader_handles_push_and_limits(self) -> None:
        cfg = Config.load(root=self.tmp)
        actions: dict[str, object] = {}

        class FakeUnit:
            def register_action(self, name, handler):
                actions[name] = handler

        class FakeRunner:
            def __init__(self) -> None:
                self.brain = "old"
                self.reloads = 0

            def reload_limits(self) -> dict:
                self.reloads += 1
                return {}

        runner = FakeRunner()
        rebuilt = []

        def rebuild() -> str:
            rebuilt.append(True)
            return "new-brain"

        reloader = ConfigReloader(
            cfg, unit=FakeUnit(), runner=runner, rebuild_brain=rebuild)
        self.assertTrue(reloader.install())
        self.assertIn("unit.configure", actions)
        handler = actions["unit.configure"]
        result = await asyncio.to_thread(
            handler, SimpleNamespace(data={"agents": {"max_steps": 3},
                                           "llm": {"model": "m2"}}))
        self.assertTrue(result["ok"])
        self.assertEqual(cfg.agents.max_steps, 3)
        self.assertEqual(cfg.llm.model, "m2")
        self.assertEqual(runner.reloads, 1)
        self.assertEqual(runner.brain, "new-brain")
        self.assertTrue(rebuilt)

    async def test_config_reloader_poll_uses_existing_rpc(self) -> None:
        cfg = Config.load(root=self.tmp)
        calls: list[str] = []

        class FakeSys:
            async def call(self, action, data=None, *, timeout=None):
                calls.append(action)
                return {"config": {"agents": {"max_steps": 9},
                                   "llm": {"model": "polled"}}}

        reloader = ConfigReloader(cfg, sys=FakeSys())
        applied = await reloader.poll_once()
        self.assertEqual(calls, ["sys.config.get"])
        self.assertEqual(cfg.agents.max_steps, 9)
        self.assertEqual(cfg.llm.model, "polled")
        self.assertTrue(applied["ok"])

    async def test_config_reloader_without_unit_or_sys_is_safe(self) -> None:
        cfg = Config.load(root=self.tmp)
        reloader = ConfigReloader(cfg)
        self.assertFalse(reloader.install())
        self.assertIsNone(await reloader.poll_once())
        self.assertFalse(reloader.start())
        await reloader.stop()


class SupervisorOnConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()

    async def asyncTearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_on_config_updates_limits_and_pushes_units(self) -> None:
        pushed: list[tuple[str, str, dict]] = []

        async def control(unit_id, action, data):
            pushed.append((unit_id, action, dict(data)))
            return {"ok": True}

        class Allocator:
            n = 0

            def new_work_id(self, kind):
                self.n += 1
                return f"{kind}.{self.n}"

            def allocate(self, work_id):
                return SimpleNamespace(rpc="ipc://x", events="ipc://y")

            def release(self, work_id):
                pass

        class Registry:
            def get(self, work_id):
                return SimpleNamespace(status="online")

        class Proc:
            pid = 7_000_000
            returncode = 0

            def poll(self):
                return 0

        sup = AgentSupervisor(
            Context(), registry=Registry(), allocator=Allocator(),
            eventlog=SimpleNamespace(append=lambda *a, **k: None,
                                     records=[]),
            config=AgentsCfg(max_workers=2), sys_rpc="ipc://sys",
            state_dir=self.state, root=self.root,
            popen_factory=lambda command, kwargs: Proc(), control=control)
        try:
            agent = await sup.spawn("coder", "p-1")
            cfg = Config.load(root=self.root, state_dir=self.state)
            cfg.agents.max_workers = 5
            cfg.agents.task_timeout_s = 99
            cfg.llm.api_key = "hidden-secret"
            result = await sup.on_config(cfg)
            self.assertEqual(result["pushed"], 1)
            unit_id, action, payload = pushed[0]
            self.assertEqual((unit_id, action), (agent.unit_id,
                                                 "unit.configure"))
            self.assertEqual(payload["agents"]["task_timeout_s"], 99)
            self.assertNotIn("api_key", payload["llm"])
            self.assertEqual(sup.max_workers, 5)
            self.assertEqual(sup.task_timeout_s, 99)
            self.assertEqual(sup.queue.max_size, cfg.agents.queue_max)
        finally:
            await sup.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

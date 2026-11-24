"""Per-agent conversations: persistence, tool loop, API and isolation."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from stackflowos.app import App
from stackflowos.config import Config, default_root
from stackflowos.errors import Conflict, NotFound
from stackflowos.session.agent_chat import AgentChatService, _parse_action
from stackflowos.web.api import WebAPI

REPO = default_root()


class ActionParserTests(unittest.TestCase):
    def test_accepts_documented_and_native_shapes(self) -> None:
        cases = {
            '{"tool": "fs.list", "args": {"path": "."}}':
                {"tool": "fs.list", "args": {"path": "."}},
            '```json\n{"tool": "fs.read", "args": {"path": "a"}}\n```':
                {"tool": "fs.read", "args": {"path": "a"}},
            '<tool name="shell.exec">{"command": "echo hi"}</tool>':
                {"tool": "shell.exec", "args": {"command": "echo hi"}},
            '<tool name="fs.list">{"path": "."}</tool>':
                {"tool": "fs.list", "args": {"path": "."}},
            '<tool_call>{"name": "fs.list", "arguments": {"path": "."}}</tool_call>':
                {"tool": "fs.list", "args": {"path": "."}},
            '<｜｜DSML｜｜ invoke name="shell.exec"><｜｜DSML｜｜ '
            'parameter name="command" string="true">echo hi</｜｜DSML｜｜ parameter>'
            '</｜｜DSML｜｜ invoke>':
                {"tool": "shell.exec", "args": {"command": "echo hi"}},
            '<invoke name="fs.list"><parameter name="path">.</parameter></invoke>':
                {"tool": "fs.list", "args": {"path": "."}},
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_parse_action(raw), expected)

    def test_plain_prose_is_not_an_action(self) -> None:
        self.assertIsNone(_parse_action("工作区里有 README.md，我读过了。"))
        self.assertIsNone(_parse_action("JSON 示例：{\"tool\": \"fs.list\"}"))


class ScriptedProvider:
    name = "scripted"
    model = "scripted-1"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list[dict], object]] = []

    async def chat(self, messages, *, tools=None):
        self.calls.append(([dict(item) for item in messages], tools))
        if self.replies:
            return self.replies.pop(0)
        return "收到。"

    async def ping(self):
        return {"ok": True}


class BlockingProvider:
    name = "scripted"
    model = "scripted-1"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def chat(self, messages, *, tools=None):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return "阻塞结束。"

    async def ping(self):
        return {"ok": True}


class AgentChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        base = REPO / ".stackflowos" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(prefix="agent-chat-", dir=str(base))
        self.tmp = Path(self._tmp.name)
        self.app = App(Config.load(root=self.tmp, state_dir=self.tmp))
        await self.app.start()
        self.card = await self.app.create_project(
            "对话测试", "验证每个 Agent 的对话", spawn_lead=False)
        self.project_id = self.card["id"]
        self.project = self.app.projects.get(self.project_id)
        self.workspace = Path(self.project.workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "notes").mkdir(exist_ok=True)
        (self.workspace / "notes" / "a.txt").write_text(
            "alpha-only-marker\n", encoding="utf-8")
        self.agent = self.app.supervisor.register(
            role="coder", project_id=self.project_id,
            unit_id="chat-test-unit", task_id=None)
        self.app.supervisor.update(self.agent.id, {"status": "idle"})

    async def asyncTearDown(self) -> None:
        # The roster records in these tests are fakes (no process behind
        # them); mark them terminal so App.stop does not try to signal a unit.
        for agent in self.app.supervisor.list():
            if str(agent.unit_id or "").startswith("chat-"):
                try:
                    self.app.supervisor.update(agent.id, {"status": "dead"})
                except Exception:
                    pass
        try:
            await self.app.stop()
        finally:
            self._tmp.cleanup()

    def use(self, provider) -> None:
        self.app.agent_chat.provider = provider

    async def wait_messages(self, count: int = 2, timeout: float = 10.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            history = self.app.agent_chat.history(self.agent.id)
            if len(history) >= count and history[-1]["role"] == "assistant":
                return history
            await asyncio.sleep(0.03)
        raise AssertionError(f"conversation did not reach {count} messages: "
                             f"{self.app.agent_chat.history(self.agent.id)}")

    async def test_tool_loop_reads_workspace_and_persists(self) -> None:
        provider = ScriptedProvider([
            '{"tool": "fs.read", "args": {"path": "notes/a.txt"}}',
            "我读到了 notes/a.txt，内容是 alpha-only-marker。",
        ])
        self.use(provider)
        sent = await self.app.agent_chat.send_user(
            self.agent.id, "读一下 notes/a.txt")
        self.assertEqual(sent["role"], "user")
        history = await self.wait_messages(2)

        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[1]["role"], "assistant")
        tools = history[1]["meta"]["tools"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["tool"], "fs.read")
        self.assertEqual(tools[0]["state"], "ok")
        self.assertIn("notes/a.txt", json.dumps(tools, ensure_ascii=False))

        # The second prompt must contain the real tool result, not just a label.
        self.assertEqual(len(provider.calls), 2)
        second = provider.calls[1][0]
        self.assertIn("工具结果（fs.read）", second[-1]["content"])
        self.assertIn("alpha-only-marker", second[-1]["content"])
        system = second[0]["content"]
        self.assertIn("Agent 对话", system)
        self.assertIn("notes/a.txt", system)

        # Persisted to <state>/state/agent-chat/<agent>.json.
        path = self.tmp / "state" / "agent-chat" / f"{self.agent.id}.json"
        self.assertTrue(path.is_file())
        payload = json.loads(path.read_text("utf-8"))
        self.assertEqual(payload["agent_id"], self.agent.id)
        self.assertEqual(len(payload["messages"]), 2)
        self.assertFalse(self.app.agent_chat.busy(self.agent.id))

        # A fresh service instance reloads the same thread from disk.
        reloaded = AgentChatService(
            self.app.ctx, self.app.eventlog, app=self.app, provider=provider,
            state_dir=self.tmp)
        self.assertEqual(len(reloaded.history(self.agent.id)), 2)

        # Audit: chat + tool events live in the same append-only stream.
        types = [record.type for record in self.app.eventlog.tail(200)]
        for expected in ("agent.chat.message", "agent.chat.tool", "tool.invoked",
                        "tool.result"):
            self.assertIn(expected, types)

    async def test_tool_scope_denies_other_projects_task(self) -> None:
        other = await self.app.create_project(
            "另一个项目", "不应该被访问", spawn_lead=False)
        other_task = self.app.projects.add_task(
            other["id"], "别的任务", "不属于 coder 的项目", role="coder")
        provider = ScriptedProvider([
            json.dumps({"tool": "task.update", "args": {
                "task_id": other_task.id, "patch": {"status": "doing"}}}),
            "那个任务不归我管。",
        ])
        self.use(provider)
        await self.app.agent_chat.send_user(self.agent.id, "改一下别的项目任务")
        history = await self.wait_messages(2)
        tools = history[1]["meta"]["tools"]
        self.assertEqual(tools[0]["state"], "denied")
        self.assertIn("DENIED", tools[0]["error"])
        self.assertEqual(self.app.projects.task_get(other_task.id).status, "todo")

    async def test_agent_without_project_can_talk_but_has_no_tools(self) -> None:
        bare = self.app.supervisor.register(
            role="coder", project_id=None, unit_id="chat-bare-unit")
        self.app.supervisor.update(bare.id, {"status": "idle"})
        provider = ScriptedProvider(["我没有绑定项目，只能给建议。"])
        self.use(provider)
        await self.app.agent_chat.send_user(bare.id, "你能做什么？")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            history = self.app.agent_chat.history(bare.id)
            if len(history) >= 2 and history[-1]["role"] == "assistant":
                break
            await asyncio.sleep(0.03)
        self.assertEqual(history[-1]["role"], "assistant")
        # One provider call, no tool catalogue, and no executor workspace.
        self.assertEqual(len(provider.calls), 1)
        self.assertIsNone(provider.calls[0][1])
        self.assertIn("没有绑定项目", provider.calls[0][0][0]["content"])

    async def test_one_turn_at_a_time_and_cancel(self) -> None:
        provider = BlockingProvider()
        self.use(provider)
        await self.app.agent_chat.send_user(self.agent.id, "慢一点回答")
        await asyncio.wait_for(provider.started.wait(), timeout=5)
        self.assertTrue(self.app.agent_chat.busy(self.agent.id))
        with self.assertRaises(Conflict):
            await self.app.agent_chat.send_user(self.agent.id, "插队")
        self.assertTrue(self.app.agent_chat.cancel(self.agent.id))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.app.agent_chat.busy(self.agent.id):
            await asyncio.sleep(0.03)
        history = self.app.agent_chat.history(self.agent.id)
        self.assertEqual(history[-1]["role"], "system")
        self.assertIn("本轮已停止", history[-1]["text"])
        self.assertFalse(self.app.agent_chat.busy(self.agent.id))

    async def test_cancel_denies_turn_approvals(self) -> None:
        provider = ScriptedProvider([
            json.dumps({"tool": "fs.write", "args": {
                "path": "notes/b.txt", "content": "should not be written"}}),
        ])
        self.use(provider)
        await self.app.agent_chat.send_user(self.agent.id, "写一个文件")
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if self.app.approvals.list(status="pending"):
                break
            await asyncio.sleep(0.03)
        pending = self.app.approvals.list(status="pending")
        self.assertTrue(pending, "fs.write should park for approval")
        self.assertEqual(pending[0].principal, self.agent.id)
        self.assertTrue(self.app.agent_chat.cancel(self.agent.id))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.app.agent_chat.busy(self.agent.id):
            await asyncio.sleep(0.03)
        self.assertFalse(self.app.agent_chat.busy(self.agent.id))
        self.assertEqual(self.app.approvals.list(status="pending"), [])
        history = self.app.agent_chat.history(self.agent.id)
        self.assertEqual(history[-1]["role"], "system")
        self.assertIn("本轮已停止", history[-1]["text"])
        self.assertFalse((self.workspace / "notes" / "b.txt").exists())

    async def test_clear_removes_thread(self) -> None:
        self.use(ScriptedProvider(["好的。"]))
        await self.app.agent_chat.send_user(self.agent.id, "你好")
        await self.wait_messages(2)
        path = self.tmp / "state" / "agent-chat" / f"{self.agent.id}.json"
        self.assertTrue(path.is_file())
        self.assertTrue(await self.app.agent_chat.clear(self.agent.id))
        self.assertEqual(self.app.agent_chat.history(self.agent.id), [])
        self.assertFalse(path.exists())
        types = [record.type for record in self.app.eventlog.tail(100)]
        self.assertIn("agent.chat.thread", types)

    async def test_unknown_agent_is_not_found(self) -> None:
        with self.assertRaises(NotFound):
            await self.app.agent_chat.send_user("agent-nobody-9", "hi")

    async def test_web_api_contract(self) -> None:
        provider = ScriptedProvider(["API 回复。"])
        self.use(provider)
        api = WebAPI(self.app)

        status, payload = await api.handle(
            "GET", f"/api/agents/{self.agent.id}/chat", {}, b"")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["agent"]["id"], self.agent.id)
        self.assertEqual(payload["data"]["messages"], [])
        self.assertEqual(payload["data"]["model"], "scripted-1")

        body = json.dumps({"text": "从 API 发一条"}).encode()
        status, payload = await api.handle(
            "POST", f"/api/agents/{self.agent.id}/chat", {}, body)
        self.assertEqual(status, 202)
        self.assertEqual(payload["data"]["message"]["text"], "从 API 发一条")
        await self.wait_messages(2)

        status, payload = await api.handle(
            "GET", f"/api/agents/{self.agent.id}/chat", {"limit": "10"}, b"")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["data"]["messages"]), 2)

        status, payload = await api.handle(
            "DELETE", f"/api/agents/{self.agent.id}/chat", {}, b"")
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["cleared"])
        status, payload = await api.handle(
            "GET", f"/api/agents/{self.agent.id}/chat", {}, b"")
        self.assertEqual(payload["data"]["messages"], [])

        status, payload = await api.handle(
            "GET", "/api/agents/agent-nobody-9/chat", {}, b"")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()

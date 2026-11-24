"""Real provider streaming must reach chat.delta / agent.chat.delta events."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from stackflowos.ai.types import LLMResult, ToolCall, Usage
from stackflowos.app import App
from stackflowos.config import Config, default_root

REPO = default_root()


def _record_events(app: App, type_: str) -> list[dict]:
    return [record.data for record in app.eventlog.tail(1000)
            if record.type == type_]


class _StreamingProvider:
    """Chat provider double with controllable streaming."""

    name = "streaming"
    model = "stream-1"

    def __init__(self, chunks: list[str], text: str | None = None,
                 stream_enabled: bool = True) -> None:
        self.chunks = list(chunks)
        self.text = text if text is not None else "".join(chunks)
        self.stream_enabled = stream_enabled
        self.calls: list[dict] = []

    async def chat_with_tools(self, messages, *, tools=None,
                              tool_choice=None, role=None, project_id=None,
                              on_delta=None, stream=None):
        self.calls.append({"messages": [dict(item) for item in messages],
                           "tools": tools, "role": role,
                           "project_id": project_id})
        if self.stream_enabled and on_delta is not None:
            for chunk in self.chunks:
                await on_delta({"type": "text", "text": chunk})
        return LLMResult(text=self.text, usage=Usage(3, 2, 5),
                         model=self.model, provider=self.name)

    async def ping(self):
        return {"ok": True, "provider": self.name}


class _NativeAgentProvider:
    name = "native"
    model = "native-1"
    stream_enabled = True

    def __init__(self, results: list[LLMResult],
                 chunks: list[list[str]]) -> None:
        self.results = list(results)
        self.chunks = list(chunks)
        self.calls: list[list[dict]] = []

    async def chat_with_tools(self, messages, *, tools=None,
                              tool_choice=None, role=None, project_id=None,
                              on_delta=None, stream=None):
        self.calls.append([dict(item) for item in messages])
        result = self.results.pop(0)
        chunks = self.chunks.pop(0) if self.chunks else []
        if self.stream_enabled and on_delta is not None:
            for chunk in chunks:
                await on_delta({"type": "text", "text": chunk})
        return result

    async def ping(self):
        return {"ok": True, "provider": self.name}


class ChatStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        base = REPO / ".stackflowos" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(prefix="chat-stream-", dir=str(base))
        self.tmp = Path(self._tmp.name)
        self.app = App(Config.load(root=self.tmp, state_dir=self.tmp))
        await self.app.start()

    async def asyncTearDown(self) -> None:
        await self.app.stop()
        self._tmp.cleanup()

    async def _wait_reply(self, expected: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            history = self.app.chat.history(10)
            if history and history[-1]["role"] == "assistant" \
                    and history[-1]["text"] == expected:
                return
            await asyncio.sleep(0.03)
        raise AssertionError(f"no reply {expected!r}: {self.app.chat.history(5)}")

    async def test_chat_deltas_come_from_provider_stream(self) -> None:
        provider = _StreamingProvider(["你好", "，世界"])
        self.app.chat.provider = provider
        await self.app.chat.send_user("你好呀")
        await self._wait_reply("你好，世界")
        deltas = [data for data in _record_events(self.app, "chat.delta")
                  if data.get("delta")]
        self.assertEqual([item["delta"] for item in deltas], ["你好", "，世界"])
        self.assertEqual("".join(item["delta"] for item in deltas),
                         "你好，世界")
        # The final message is persisted once and is not duplicated by the
        # chunked fallback.
        history = self.app.chat.history(10)
        self.assertEqual(history[-1]["text"], "你好，世界")
        self.assertEqual(len([m for m in history if m["role"] == "assistant"]),
                         2)  # hello + one reply

    async def test_stream_disabled_keeps_chunked_simulation(self) -> None:
        provider = _StreamingProvider(["你好", "，世界"], stream_enabled=False)
        self.app.chat.provider = provider
        await self.app.chat.send_user("你好呀")
        await self._wait_reply("你好，世界")
        deltas = [data for data in _record_events(self.app, "chat.delta")
                  if data.get("delta")]
        # Old behaviour: one 48-character simulated chunk for a short reply.
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["delta"], "你好，世界")


class AgentChatStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        base = REPO / ".stackflowos" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(prefix="agent-stream-",
                                                dir=str(base))
        self.tmp = Path(self._tmp.name)
        self.app = App(Config.load(root=self.tmp, state_dir=self.tmp))
        await self.app.start()
        card = await self.app.create_project(
            "流式对话", "验证 agent chat 流式增量", spawn_lead=False)
        self.project = self.app.projects.get(card["id"])
        (Path(self.project.workspace) / "notes").mkdir(parents=True,
                                                       exist_ok=True)
        (Path(self.project.workspace) / "notes" / "a.txt").write_text(
            "alpha\n", encoding="utf-8")
        self.agent = self.app.supervisor.register(
            role="coder", project_id=self.project.id,
            unit_id="chat-stream-unit")
        self.app.supervisor.update(self.agent.id, {"status": "idle"})

    async def asyncTearDown(self) -> None:
        for agent in self.app.supervisor.list():
            if str(agent.unit_id or "").startswith("chat-stream"):
                try:
                    self.app.supervisor.update(agent.id, {"status": "dead"})
                except Exception:
                    pass
        await self.app.stop()
        self._tmp.cleanup()

    async def _wait_assistant(self, timeout: float = 10.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            history = self.app.agent_chat.history(self.agent.id)
            if len(history) >= 2 and history[-1]["role"] == "assistant":
                return history
            await asyncio.sleep(0.03)
        raise AssertionError(self.app.agent_chat.history(self.agent.id))

    async def test_native_tool_call_then_streamed_prose(self) -> None:
        provider = _NativeAgentProvider(
            results=[
                LLMResult(text="", finish_reason="tool_calls",
                          tool_calls=[ToolCall(id="call-1", name="fs.read",
                                               arguments={"path": "notes/a.txt"})],
                          usage=Usage(4, 1, 5)),
                LLMResult(text="目录里有 notes/a.txt", usage=Usage(6, 4, 10)),
            ],
            chunks=[[], ["目录里有", " notes/a.txt"]],
        )
        self.app.agent_chat.provider = provider
        await self.app.agent_chat.send_user(self.agent.id, "看看目录")
        history = await self._wait_assistant()
        self.assertEqual(history[-1]["text"], "目录里有 notes/a.txt")
        self.assertEqual(history[-1]["meta"]["tools"][0]["tool"], "fs.read")
        self.assertEqual(history[-1]["meta"]["tools"][0]["state"], "ok")

        # Native protocol: second call receives a tool-role message with the
        # matching tool_call_id (not a duplicate user message).
        second = provider.calls[1]
        self.assertEqual(second[-1]["role"], "tool")
        self.assertEqual(second[-1]["tool_call_id"], "call-1")
        self.assertIn("alpha", second[-1]["content"])
        assistant_tool = [
            item for item in second
            if item.get("role") == "assistant" and item.get("tool_calls")]
        self.assertEqual(assistant_tool[0]["tool_calls"][0]["id"], "call-1")

        deltas = [data for data in _record_events(self.app, "agent.chat.delta")
                  if data.get("delta")]
        self.assertEqual([item["delta"] for item in deltas],
                         ["目录里有", " notes/a.txt"])

    async def test_agent_stream_disabled_keeps_simulated_chunks(self) -> None:
        provider = _NativeAgentProvider(
            results=[LLMResult(text="普通回答", usage=Usage(2, 2, 4))],
            chunks=[["普通", "回答"]],
        )
        provider.stream_enabled = False
        self.app.agent_chat.provider = provider
        await self.app.agent_chat.send_user(self.agent.id, "你好")
        history = await self._wait_assistant()
        self.assertEqual(history[-1]["text"], "普通回答")
        deltas = [data for data in _record_events(self.app, "agent.chat.delta")
                  if data.get("delta")]
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["delta"], "普通回答")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

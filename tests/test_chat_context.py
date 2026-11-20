"""The dialog must carry recent turns and project/task context into the LLM."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from stackflowos.app import App
from stackflowos.config import Config, default_root

REPO = default_root()


class CaptureProvider:
    name = "scripted"
    model = "scripted"

    def __init__(self) -> None:
        self.calls: list[tuple[list[dict], object]] = []

    async def chat(self, messages, *, tools=None):
        self.calls.append(([dict(item) for item in messages], tools))
        return f"回复{len(self.calls)}"

    async def ping(self):
        return {"ok": True, "provider": self.name, "model": self.model}


class ChatContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.app = App(Config.load(root=self.tmp, state_dir=self.tmp / "state"))
        await self.app.start()
        card = await self.app.create_project("上下文项目", "验证对话上下文",
                                             spawn_lead=False)
        self.project_id = card["id"]
        self.app.projects.add_task(self.project_id, "写上下文测试", "描述",
                                   role="coder")
        self.provider = CaptureProvider()
        self.app.chat.provider = self.provider

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    async def test_second_turn_has_history_and_project_context(self):
        await self.app.chat.send_user("第一句话")
        await self._wait_reply("回复1")
        await self.app.chat.send_user("第二句话")
        await self._wait_reply("回复2")
        self.assertEqual(len(self.provider.calls), 2)
        messages, _tools = self.provider.calls[1]
        self.assertEqual(messages[0]["role"], "system")
        system = messages[0]["content"]
        self.assertIn("上下文项目", system)
        self.assertIn("写上下文测试", system)
        contents = [(m["role"], m["content"]) for m in messages[1:]]
        self.assertIn(("user", "第一句话"), contents)
        self.assertIn(("assistant", "回复1"), contents)
        self.assertEqual(contents[-1], ("user", "第二句话"))

    async def _wait_reply(self, expected: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            history = self.app.chat.history(10)
            if history and history[-1]["role"] == "assistant" \
                    and history[-1]["text"] == expected:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"assistant did not reply {expected!r}")


if __name__ == "__main__":
    unittest.main()

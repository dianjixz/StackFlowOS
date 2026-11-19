"""Tests for LLM providers (mock + OpenAI-compatible against a local server)."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

from stackflowos.agents.brain import AgentView
from stackflowos.ai.llm import (LLMBrain, MockProvider,
                                OpenAICompatibleProvider, make_provider)
from stackflowos.ai.prompts import role_prompt, tool_call_protocol
from stackflowos.config import LLMCfg
from stackflowos.errors import BadRequest, Unavailable


class _Handler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible endpoint used only in tests."""

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        server = self.server
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = None
        server.requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        if server.fail:
            payload = b'{"error": {"message": "boom"}}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = json.dumps({
            "choices": [{"message": {"role": "assistant",
                                     "content": server.reply}}],
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:  # silence test output
        return


class ScriptedProvider:
    """Provider double with a queued list of replies."""

    def __init__(self, replies: list[str] | None = None,
                 error: Exception | None = None) -> None:
        self.replies = list(replies or [])
        self.error = error
        self.calls: list[tuple[list[dict], list[dict] | None]] = []

    async def chat(self, messages: list[dict], *,
                   tools: list[dict] | None = None) -> str:
        self.calls.append((messages, tools))
        if self.error is not None:
            raise self.error
        return self.replies.pop(0)

    async def ping(self) -> dict:
        return {"ok": True, "provider": "scripted"}


class MockProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_deterministic_reply(self) -> None:
        provider = MockProvider()
        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "帮我看看项目"}]
        first = await provider.chat(messages)
        second = await provider.chat(messages)
        self.assertEqual(first, second)
        self.assertIn("帮我看看项目", first)
        self.assertIn("mock", first)

    async def test_status_keyword_reply(self) -> None:
        reply = await MockProvider().chat([
            {"role": "user", "content": "现在进度怎么样？"}])
        self.assertIn("系统运行正常", reply)

    async def test_ping(self) -> None:
        self.assertEqual(await MockProvider().ping(),
                         {"ok": True, "provider": "mock"})


class OpenAIProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.requests = []
        self.httpd.fail = False
        self.httpd.reply = "你好，这是假模型回复"
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address[:2]
        self.base_url = f"http://{host}:{port}/v1"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def _provider(self) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(
            base_url=self.base_url, model="test-model", api_key="sk-test",
            temperature=0.1, max_tokens=64, timeout_s=5.0)

    async def test_chat_request_shape_and_reply(self) -> None:
        provider = self._provider()
        messages = [{"role": "system", "content": "你是助手"},
                    {"role": "user", "content": "你好"}]
        reply = await provider.chat(messages)
        self.assertEqual(reply, "你好，这是假模型回复")

        request = self.httpd.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(request["body"]["model"], "test-model")
        self.assertEqual(request["body"]["messages"], messages)
        self.assertEqual(request["body"]["temperature"], 0.1)
        self.assertEqual(request["body"]["max_tokens"], 64)

    async def test_ping_round_trip(self) -> None:
        result = await self._provider().ping()
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "openai")
        self.assertEqual(result["model"], "test-model")
        self.assertIn("latency_ms", result)
        self.assertEqual(self.httpd.requests[0]["path"], "/v1/chat/completions")

    async def test_http_error_becomes_unavailable_without_key(self) -> None:
        self.httpd.fail = True
        with self.assertRaises(Unavailable) as caught:
            await self._provider().chat([{"role": "user", "content": "x"}])
        message = str(caught.exception)
        self.assertIn("500", message)
        self.assertNotIn("sk-test", message)

    async def test_connection_error_becomes_unavailable(self) -> None:
        provider = OpenAICompatibleProvider(
            base_url="http://127.0.0.1:1/v1", model="m", api_key="sk-secret",
            timeout_s=1.0)
        with self.assertRaises(Unavailable) as caught:
            await provider.chat([{"role": "user", "content": "x"}])
        self.assertNotIn("sk-secret", str(caught.exception))

    async def test_ping_reports_failure_instead_of_raising(self) -> None:
        self.httpd.fail = True
        result = await self._provider().ping()
        self.assertFalse(result["ok"])
        self.assertIn("error", result)


class ProviderFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_make_provider_mock(self) -> None:
        provider = make_provider(LLMCfg(provider="mock"))
        self.assertIsInstance(provider, MockProvider)
        self.assertEqual((await provider.ping())["provider"], "mock")

    async def test_make_provider_openai(self) -> None:
        cfg = LLMCfg(provider="openai", base_url="http://127.0.0.1:9/v1",
                     model="m", api_key="k")
        provider = make_provider(cfg)
        self.assertIsInstance(provider, OpenAICompatibleProvider)
        self.assertEqual(provider.base_url, "http://127.0.0.1:9/v1")
        self.assertEqual(provider.model, "m")

    async def test_make_provider_rejects_unknown(self) -> None:
        with self.assertRaises(BadRequest):
            make_provider(SimpleNamespace(provider="bogus"))
        with self.assertRaises(BadRequest):
            OpenAICompatibleProvider(base_url="", model="m")
        with self.assertRaises(BadRequest):
            OpenAICompatibleProvider(base_url="http://x/v1", model="")


class LLMBrainTests(unittest.IsolatedAsyncioTestCase):
    def _view(self) -> AgentView:
        return AgentView(
            agent=SimpleNamespace(id="agent-coder-1", role="coder",
                                  task_id="t-1"),
            project={"id": "p-1", "name": "Demo", "goal": "目标", "status": "active"},
            task={"id": "t-1", "title": "实现", "description": "写代码",
                  "status": "doing", "artifacts": []},
            step=2,
            workspace="/tmp/ws",
        )

    async def test_parses_tool_json_from_prose(self) -> None:
        provider = ScriptedProvider([
            '先想一下：{"tool": "fs.list", "args": {"path": "."}} 完成'])
        action = await LLMBrain(provider, "coder").next_action(self._view())
        self.assertEqual(action.tool, "fs.list")
        self.assertEqual(action.args, {"path": "."})
        messages, tools = provider.calls[0]
        self.assertIn("StackFlowOS", messages[0]["content"])
        self.assertTrue(any(tool["name"] == "fs.write" for tool in tools))

    async def test_parses_finish(self) -> None:
        provider = ScriptedProvider([
            '{"finish": {"summary": "完成", "status": "blocked"}}'])
        action = await LLMBrain(provider, "coder").next_action(self._view())
        self.assertIsNotNone(action.finish)
        self.assertEqual(action.finish["status"], "blocked")
        self.assertEqual(action.finish["summary"], "完成")

    async def test_retries_malformed_replies(self) -> None:
        provider = ScriptedProvider([
            "这不是 JSON",
            "还不是",
            '{"finish": {"summary": "终于成功"}}',
        ])
        action = await LLMBrain(provider, "coder").next_action(self._view())
        self.assertEqual(action.finish["summary"], "终于成功")
        self.assertEqual(len(provider.calls), 3)

    async def test_three_bad_replies_finish_failed(self) -> None:
        provider = ScriptedProvider(["no", "no", "no"])
        action = await LLMBrain(provider, "coder").next_action(self._view())
        self.assertEqual(action.finish["status"], "failed")
        self.assertIn("无法解析", action.finish["summary"])

    async def test_unknown_tool_retried_then_failed(self) -> None:
        provider = ScriptedProvider([
            '{"tool": "fs.nope", "args": {}}',
            '{"tool": "fs.nope", "args": {}}',
            '{"tool": "fs.nope", "args": {}}',
        ])
        action = await LLMBrain(provider, "coder").next_action(self._view())
        self.assertEqual(action.finish["status"], "failed")
        self.assertIn("未知工具", action.finish["summary"])

    async def test_provider_error_finishes_failed(self) -> None:
        provider = ScriptedProvider(error=Unavailable("offline"))
        action = await LLMBrain(provider, "coder").next_action(self._view())
        self.assertEqual(action.finish["status"], "failed")
        self.assertIn("LLM 调用失败", action.finish["summary"])


class PromptTests(unittest.TestCase):
    def test_protocol_and_role_prompt(self) -> None:
        protocol = tool_call_protocol()
        self.assertIn("fs.write", protocol)
        self.assertIn("finish", protocol)
        prompt = role_prompt("coder")
        self.assertIn("编码工人", prompt)
        self.assertIn("fs.write", prompt)
        self.assertLess(len(prompt), 1200)
        for role in ("lead", "assistant", "architect", "researcher",
                     "tester", "reviewer", "aggregator"):
            self.assertLess(len(role_prompt(role)), 1200)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

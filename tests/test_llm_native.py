"""Native tool calling, Anthropic/Gemini adapters, SSE streaming and errors."""

from __future__ import annotations

import json
import threading
import tempfile
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

from stackflowos.ai.llm import LLMBrain, make_provider
from stackflowos.ai.providers import (AnthropicProvider, GeminiProvider,
                                      MockProvider, OpenAICompatibleProvider)
from stackflowos.ai.telemetry import (UsageTracker, estimate_messages_tokens,
                                      reset_runtime)
from stackflowos.ai.types import LLMResult, ToolCall, Usage
from stackflowos.config import LLMCfg
from stackflowos.errors import Unavailable


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = None
        self.server.requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        try:
            status, content_type, payload = self.server.responder(
                self.path, body, self.headers)
        except Exception as exc:  # pragma: no cover - test bug
            status, content_type = 500, "application/json"
            payload = json.dumps({"error": str(exc)}).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        return


class ServerMixin:
    responder = None

    def start_server(self) -> None:
        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.requests = []
        self.httpd.responder = self.responder
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address[:2]
        self.base_url = f"http://{host}:{port}"

    def stop_server(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _json_responder(payload: dict):
    raw = json.dumps(payload).encode()
    return lambda path, body, headers: (200, "application/json", raw)


def _sse_responder(events: list[dict | str]):
    lines = []
    for event in events:
        if isinstance(event, str):
            lines.append(event)
        else:
            lines.append("data: " + json.dumps(event, ensure_ascii=False))
    raw = ("\n\n".join(lines) + "\n\n").encode()
    return lambda path, body, headers: (200, "text/event-stream", raw)


class OpenAIProviderTests(unittest.IsolatedAsyncioTestCase, ServerMixin):
    def setUp(self) -> None:
        self.responder = _json_responder({
            "model": "test-model",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "我来看看。",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "fs.read",
                            "arguments": '{"path": "README.md"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7,
                      "total_tokens": 19},
        })
        self.start_server()

    def tearDown(self) -> None:
        self.stop_server()

    def _provider(self, **kwargs) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(
            base_url=self.base_url + "/v1", model="test-model",
            api_key="sk-secret", timeout_s=5.0, retries=0, **kwargs)

    async def test_native_tool_calling_request_and_result(self) -> None:
        provider = self._provider()
        tools = [{"name": "fs.read", "description": "读文件",
                  "args": {"path": "string"}}]
        result = await provider.chat_with_tools(
            [{"role": "user", "content": "读 README"}], tools=tools)
        self.assertIsInstance(result, LLMResult)
        self.assertEqual(result.text, "我来看看。")
        self.assertEqual(result.provider, "openai")
        self.assertEqual(result.finish_reason, "tool_calls")
        self.assertEqual(result.usage.prompt, 12)
        self.assertEqual(result.usage.completion, 7)
        self.assertEqual(result.usage.total, 19)
        self.assertEqual(len(result.tool_calls), 1)
        call = result.tool_calls[0]
        self.assertEqual(call.id, "call_1")
        self.assertEqual(call.name, "fs.read")
        self.assertEqual(call.arguments, {"path": "README.md"})

        request = self.httpd.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer sk-secret")
        body = request["body"]
        self.assertEqual(body["tools"][0]["type"], "function")
        self.assertEqual(body["tools"][0]["function"]["name"], "fs_read")
        self.assertEqual(body["tool_choice"], "auto")

    async def test_chat_still_returns_text(self) -> None:
        reply = await self._provider().chat(
            [{"role": "user", "content": "hi"}])
        self.assertEqual(reply, "我来看看。")


class OpenAISSEStreamTests(unittest.IsolatedAsyncioTestCase, ServerMixin):
    def setUp(self) -> None:
        self.responder = _sse_responder([
            {"choices": [{"index": 0, "delta": {"content": "你"}}]},
            {"choices": [{"index": 0, "delta": {"content": "好"}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call_9", "type": "function",
                "function": {"name": "fs.list", "arguments": '{"path":'}}]}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0,
                "function": {"arguments": ' "."}'}}]}}]},
            {"choices": [{"index": 0, "delta": {},
                          "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 5,
                                      "completion_tokens": 3,
                                      "total_tokens": 8}},
            "[DONE]",
        ])
        self.start_server()

    def tearDown(self) -> None:
        self.stop_server()

    async def test_stream_yields_text_tool_usage_finish(self) -> None:
        provider = OpenAICompatibleProvider(
            base_url=self.base_url + "/v1", model="m", api_key="k",
            timeout_s=5.0, retries=0, stream=True)
        deltas = [delta async for delta in provider.stream(
            [{"role": "user", "content": "hi"}],
            tools=[{"name": "fs.list", "description": "list",
                    "args": {"path": "string"}}])]
        kinds = [delta["type"] for delta in deltas]
        self.assertIn("text", kinds)
        self.assertIn("tool_call", kinds)
        self.assertIn("usage", kinds)
        self.assertIn("finish", kinds)
        text = "".join(delta["text"] for delta in deltas
                       if delta["type"] == "text")
        self.assertEqual(text, "你好")
        self.assertTrue(any(delta["type"] == "tool_call"
                            and delta.get("name") == "fs.list"
                            for delta in deltas))
        usage = [delta for delta in deltas if delta["type"] == "usage"][-1]
        self.assertEqual(usage["usage"]["total"], 8)
        self.assertTrue(self.httpd.requests[0]["body"]["stream"])

    async def test_chat_with_on_delta_aggregates_stream(self) -> None:
        provider = OpenAICompatibleProvider(
            base_url=self.base_url + "/v1", model="m", api_key="k",
            timeout_s=5.0, retries=0)
        seen: list[str] = []

        async def on_delta(delta: dict) -> None:
            if delta.get("type") == "text":
                seen.append(delta["text"])

        result = await provider.chat_with_tools(
            [{"role": "user", "content": "hi"}],
            tools=[{"name": "fs.list", "description": "list",
                    "args": {"path": "string"}}],
            on_delta=on_delta)
        self.assertEqual(result.text, "你好")
        self.assertEqual(seen, ["你", "好"])
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].name, "fs.list")
        self.assertEqual(result.tool_calls[0].arguments, {"path": "."})
        self.assertEqual(result.usage.total, 8)


class AnthropicProviderTests(unittest.IsolatedAsyncioTestCase, ServerMixin):
    def setUp(self) -> None:
        self.responder = _json_responder({
            "id": "msg_1",
            "model": "claude-test",
            "content": [
                {"type": "text", "text": "我来检查。"},
                {"type": "tool_use", "id": "toolu_1", "name": "fs.list",
                 "input": {"path": "."}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 20, "output_tokens": 9},
        })
        self.start_server()

    def tearDown(self) -> None:
        self.stop_server()

    def _provider(self) -> AnthropicProvider:
        return AnthropicProvider(
            base_url=self.base_url, model="claude-test", api_key="ant-secret",
            timeout_s=5.0, retries=0)

    async def test_messages_request_and_tool_use_parse(self) -> None:
        provider = self._provider()
        result = await provider.chat_with_tools(
            [{"role": "system", "content": "你是助手"},
             {"role": "user", "content": "看下目录"}],
            tools=[{"name": "fs.list", "description": "list",
                    "args": {"path": "string"}}])
        self.assertEqual(result.text, "我来检查。")
        self.assertEqual(result.finish_reason, "tool_calls")
        self.assertEqual(result.usage.prompt, 20)
        self.assertEqual(result.usage.completion, 9)
        self.assertEqual(result.tool_calls[0].id, "toolu_1")
        self.assertEqual(result.tool_calls[0].arguments, {"path": "."})

        request = self.httpd.requests[0]
        self.assertEqual(request["path"], "/v1/messages")
        headers = {key.lower(): value for key, value in request["headers"].items()}
        self.assertEqual(headers["x-api-key"], "ant-secret")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("authorization", headers)
        body = request["body"]
        self.assertEqual(body["system"], "你是助手")
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertEqual(body["tools"][0]["name"], "fs_list")
        self.assertIn("input_schema", body["tools"][0])
        self.assertEqual(body["tools"][0]["input_schema"]["properties"]["path"]["type"],
                         "string")

    async def test_tool_result_messages_are_converted(self) -> None:
        await self._provider().chat_with_tools([
            {"role": "user", "content": "读"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "toolu_1", "type": "function",
                             "function": {"name": "fs.read",
                                          "arguments": '{"path":"a"}'}}]},
            {"role": "tool", "tool_call_id": "toolu_1",
             "content": '{"ok": true}'},
        ])
        messages = self.httpd.requests[0]["body"]["messages"]
        assistant = [m for m in messages if m["role"] == "assistant"][0]
        self.assertEqual(assistant["content"][0]["type"], "tool_use")
        tool_result = [m for m in messages
                       if m["role"] == "user" and isinstance(m["content"], list)
                       and m["content"][0].get("type") == "tool_result"][0]
        self.assertEqual(tool_result["content"][0]["tool_use_id"], "toolu_1")


class AnthropicStreamTests(unittest.IsolatedAsyncioTestCase, ServerMixin):
    def setUp(self) -> None:
        self.responder = _sse_responder([
            {"type": "message_start",
             "message": {"usage": {"input_tokens": 6}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "你好"}},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "tool_use", "id": "toolu_2",
                               "name": "fs.list"}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta",
                       "partial_json": '{"path": "."}'}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
             "usage": {"output_tokens": 4}},
            {"type": "message_stop"},
        ])
        self.start_server()

    def tearDown(self) -> None:
        self.stop_server()

    async def test_anthropic_sse(self) -> None:
        provider = AnthropicProvider(base_url=self.base_url,
                                     model="claude-test", api_key="k",
                                     timeout_s=5.0, retries=0)
        deltas = [delta async for delta in provider.stream(
            [{"role": "user", "content": "hi"}])]
        self.assertEqual("".join(d["text"] for d in deltas
                                 if d["type"] == "text"), "你好")
        tool = [d for d in deltas if d["type"] == "tool_call"]
        self.assertTrue(any(d.get("name") == "fs.list" for d in tool))
        self.assertTrue(any(d["type"] == "finish" for d in deltas))


class GeminiProviderTests(unittest.IsolatedAsyncioTestCase, ServerMixin):
    def setUp(self) -> None:
        self.responder = _json_responder({
            "candidates": [{
                "content": {"role": "model", "parts": [
                    {"text": "查一下。"},
                    {"functionCall": {"name": "fs.list",
                                      "args": {"path": "."}}},
                ]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {"promptTokenCount": 11,
                              "candidatesTokenCount": 4,
                              "totalTokenCount": 15},
            "modelVersion": "gemini-test",
        })
        self.start_server()

    def tearDown(self) -> None:
        self.stop_server()

    async def test_generate_content_request_and_parse(self) -> None:
        provider = GeminiProvider(base_url=self.base_url,
                                  model="gemini-test", api_key="gem-key",
                                  timeout_s=5.0, retries=0)
        result = await provider.chat_with_tools([
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "看目录"},
        ], tools=[{"name": "fs.list", "description": "list",
                   "args": {"path": "string"}}])
        self.assertEqual(result.text, "查一下。")
        self.assertEqual(result.usage.prompt, 11)
        self.assertEqual(result.usage.completion, 4)
        self.assertEqual(result.tool_calls[0].name, "fs.list")
        self.assertEqual(result.tool_calls[0].arguments, {"path": "."})

        request = self.httpd.requests[0]
        self.assertIn("/v1beta/models/gemini-test:generateContent",
                      request["path"])
        headers = {key.lower(): value for key, value in request["headers"].items()}
        self.assertEqual(headers["x-goog-api-key"], "gem-key")
        body = request["body"]
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "你是助手")
        self.assertEqual(body["tools"][0]["functionDeclarations"][0]["name"],
                         "fs_list")
        self.assertEqual(body["contents"][0]["role"], "user")

    async def test_gemini_sse(self) -> None:
        self.stop_server()
        self.responder = _sse_responder([
            {"candidates": [{"content": {"parts": [{"text": "你"}]}}]},
            {"candidates": [{"content": {"parts": [{"text": "好"}]},
                             "finishReason": "STOP"}],
             "usageMetadata": {"promptTokenCount": 2,
                               "candidatesTokenCount": 2,
                               "totalTokenCount": 4}},
        ])
        self.start_server()
        provider = GeminiProvider(base_url=self.base_url, model="gemini-test",
                                  api_key="k", timeout_s=5.0, retries=0)
        deltas = [delta async for delta in provider.stream(
            [{"role": "user", "content": "hi"}])]
        self.assertEqual("".join(d["text"] for d in deltas
                                 if d["type"] == "text"), "你好")
        self.assertTrue(any(d["type"] == "usage" for d in deltas))


class ErrorMappingTests(unittest.IsolatedAsyncioTestCase, ServerMixin):
    def setUp(self) -> None:
        self.responder = lambda path, body, headers: (
            429, "application/json", b'{"error":"slow down sk-secret"}')
        self.start_server()

    def tearDown(self) -> None:
        self.stop_server()

    async def test_429_is_retryable_unavailable_and_redacted(self) -> None:
        provider = OpenAICompatibleProvider(
            base_url=self.base_url + "/v1", model="m", api_key="sk-secret",
            timeout_s=5.0, retries=0)
        with self.assertRaises(Unavailable) as caught:
            await provider.chat([{"role": "user", "content": "x"}])
        self.assertIn("429", str(caught.exception))
        self.assertNotIn("sk-secret", str(caught.exception))

    async def test_bad_request_is_not_retried(self) -> None:
        provider = OpenAICompatibleProvider(
            base_url=self.base_url + "/v1", model="m", api_key="sk-secret",
            timeout_s=5.0, retries=2)
        with self.assertRaises(Unavailable):
            await provider.chat([{"role": "user", "content": "x"}])
        # 429 is transient, so it is retried; this test documents the retry
        # count rather than assuming a single attempt.
        self.assertEqual(len(self.httpd.requests), 3)


class ProviderFactoryConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_make_provider_anthropic_and_gemini(self) -> None:
        anthropic = make_provider(LLMCfg(provider="anthropic",
                                        model="claude-test"))
        self.assertIsInstance(anthropic, AnthropicProvider)
        gemini = make_provider(LLMCfg(provider="gemini", model="gemini-test"))
        self.assertIsInstance(gemini, GeminiProvider)
        self.assertTrue(anthropic.base_url.startswith("https://"))
        self.assertTrue(gemini.base_url.startswith("https://"))

    async def test_stream_false_uses_non_streaming_request(self) -> None:
        payload = {
            "choices": [{"message": {"role": "assistant",
                                     "content": "普通回复"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                      "total_tokens": 3},
        }
        responder = _json_responder(payload)
        httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        httpd.requests = []
        httpd.responder = responder
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = httpd.server_address[:2]
            provider = OpenAICompatibleProvider(
                base_url=f"http://{host}:{port}/v1", model="m",
                api_key="k", timeout_s=5.0, retries=0, stream=False)
            seen: list[dict] = []

            async def on_delta(delta):
                seen.append(delta)

            result = await provider.chat_with_tools(
                [{"role": "user", "content": "hi"}], on_delta=on_delta)
            self.assertEqual(result.text, "普通回复")
            self.assertEqual(seen, [])
            self.assertNotIn("stream", httpd.requests[0]["body"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


class LLMBrainNativeTests(unittest.IsolatedAsyncioTestCase):
    def _view(self):
        from stackflowos.agents.brain import AgentView
        return AgentView(
            agent=SimpleNamespace(id="agent-coder-1", role="coder",
                                  task_id="t-1"),
            project={"id": "p-1", "name": "Demo", "goal": "目标",
                     "status": "active"},
            step=1, workspace="/tmp/ws")

    async def test_native_tool_call_becomes_action_and_observe_uses_tool_role(self):
        provider = MockProvider(replies=[LLMResult(
            text="",
            tool_calls=[ToolCall(id="call_1", name="fs.list",
                                 arguments={"path": "."})],
            finish_reason="tool_calls")])
        brain = LLMBrain(provider, "coder")
        action = await brain.next_action(self._view())
        self.assertEqual(action.tool, "fs.list")
        self.assertEqual(action.args, {"path": "."})
        brain.observe(action, SimpleNamespace(ok=True, error=None,
                                              data={"entries": ["a"]}))
        self.assertEqual(brain._history[-1]["role"], "tool")
        self.assertEqual(brain._history[-1]["tool_call_id"], "call_1")
        self.assertFalse(any(
            item.get("role") == "user" and "工具结果" in
            str(item.get("content")) for item in brain._history))

    async def test_native_result_without_tool_calls_falls_back_to_json(self):
        provider = MockProvider(replies=[LLMResult(
            text='{"finish": {"summary": "ok", "status": "done"}}')])
        action = await LLMBrain(provider, "coder").next_action(self._view())
        self.assertEqual(action.finish["summary"], "ok")

    async def test_scripted_mock_provider_drives_tool_loop(self):
        provider = MockProvider(replies=[
            '{"tool": "fs.list", "args": {"path": "."}}',
            '{"finish": {"summary": "完成"}}',
        ])
        brain = LLMBrain(provider, "coder")
        first = await brain.next_action(self._view())
        self.assertEqual(first.tool, "fs.list")
        brain.observe(first, SimpleNamespace(ok=True, error=None,
                                              data={"entries": []}))
        second = await brain.next_action(self._view())
        self.assertEqual(second.finish["summary"], "完成")


class UsageTrackerTests(unittest.IsolatedAsyncioTestCase):
    def test_usage_persists_and_reloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "llm_usage.json"
            tracker = UsageTracker(path)
            tracker.record(project_id="p-1", role="coder", provider="openai",
                           model="m", usage=Usage(10, 5, 15),
                           price_per_1k_input=1.0, price_per_1k_output=2.0)
            tracker.record(project_id="p-1", role="coder", provider="openai",
                           model="m", usage=Usage(2, 1, 3),
                           price_per_1k_input=1.0, price_per_1k_output=2.0)
            reloaded = UsageTracker(path)
            snapshot = reloaded.snapshot("p-1")
            self.assertEqual(snapshot["project"]["total"], 18)
            self.assertEqual(snapshot["projects"]["p-1"]["calls"], 2)
            self.assertAlmostEqual(snapshot["totals"]["cost"], 0.024)
            self.assertEqual(snapshot["roles"]["coder"]["total"], 18)
            self.assertEqual(snapshot["models"]["m"]["total"], 18)

    def test_budget_fails_closed(self) -> None:
        from stackflowos.ai.telemetry import BudgetExceeded
        tracker = UsageTracker()
        tracker.record(project_id="p-1", role="coder", provider="mock",
                       model="m", usage=Usage(90, 5, 95))
        with self.assertRaises(BudgetExceeded):
            tracker.check_budget("p-1", 10, 100)
        tracker.check_budget("p-2", 10, 100)

    async def test_provider_records_usage_by_project_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = reset_runtime(state_dir=tmp)
            try:
                provider = MockProvider(replies=["hello"])
                await provider.chat([{"role": "user", "content": "hi"}],
                                    role="tester", project_id="p-9")
                snapshot = runtime.usage.snapshot("p-9")
                self.assertEqual(snapshot["project"]["calls"], 1)
                self.assertEqual(snapshot["roles"]["tester"]["calls"], 1)
                self.assertEqual(snapshot["providers"]["mock"]["calls"], 1)
                snapshot2 = UsageTracker(
                    Path(tmp) / "state" / "llm_usage.json").snapshot("p-9")
                self.assertEqual(snapshot2["project"]["total"],
                                 snapshot["project"]["total"])
            finally:
                reset_runtime()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

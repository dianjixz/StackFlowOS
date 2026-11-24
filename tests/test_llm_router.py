"""Router fallback/routes, limiter/budget/circuit, context trimming."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from stackflowos.ai.context import trim_messages
from stackflowos.ai.factory import create_provider, merge_specs
from stackflowos.ai.llm import make_provider
from stackflowos.ai.limits import CircuitBreaker, RateLimited, RateLimiter
from stackflowos.ai.providers import MockProvider, OpenAICompatibleProvider
from stackflowos.ai.router import Router
from stackflowos.ai.telemetry import (BudgetExceeded, LLMRuntime,
                                      UsageTracker, estimate_tokens,
                                      reset_runtime)
from stackflowos.ai.types import Usage
from stackflowos.config import LLMCfg
from stackflowos.errors import Unavailable


class _FailHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.requests += 1
        payload = b'{"error": "boom"}'
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        return


class LimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_rpm_limit(self) -> None:
        limiter = RateLimiter(rpm=1, max_concurrency=2)
        ticket = await limiter.acquire(1)
        try:
            with self.assertRaises(RateLimited) as caught:
                await limiter.acquire(1)
            self.assertEqual(caught.exception.http_status, 429)
        finally:
            limiter.release(ticket, 1)

    async def test_tpm_limit(self) -> None:
        limiter = RateLimiter(rpm=0, tpm=10, max_concurrency=2)
        ticket = await limiter.acquire(6)
        try:
            with self.assertRaises(RateLimited):
                await limiter.acquire(6)
        finally:
            limiter.release(ticket, 6)

    async def test_concurrency_limit(self) -> None:
        limiter = RateLimiter(rpm=0, tpm=0, max_concurrency=1,
                             acquire_timeout_s=0.05)
        ticket = await limiter.acquire(1)
        try:
            with self.assertRaises(RateLimited):
                await limiter.acquire(1)
        finally:
            limiter.release(ticket)

    async def test_snapshot(self) -> None:
        limiter = RateLimiter(rpm=10, tpm=100, max_concurrency=3)
        ticket = await limiter.acquire(7)
        limiter.release(ticket, 7)
        snapshot = limiter.snapshot()
        self.assertEqual(snapshot["rpm_used"], 1)
        self.assertEqual(snapshot["tpm_used"], 7)
        self.assertEqual(snapshot["max_concurrency"], 3)


class CircuitBreakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_opens_after_consecutive_failures_and_half_opens(self) -> None:
        breaker = CircuitBreaker(threshold=2, cooldown_s=0.05)
        self.assertTrue(breaker.allow())
        breaker.record_failure("LLM HTTP 500")
        self.assertEqual(breaker.state, "closed")
        breaker.record_failure("LLM HTTP 500")
        self.assertEqual(breaker.state, "open")
        self.assertFalse(breaker.available())
        self.assertFalse(breaker.allow())
        await asyncio.sleep(0.12)
        self.assertTrue(breaker.available())
        self.assertTrue(breaker.allow())
        self.assertEqual(breaker.state, "half_open")
        self.assertFalse(breaker.allow())
        breaker.record_success()
        self.assertEqual(breaker.state, "closed")


class RouterTests(unittest.IsolatedAsyncioTestCase):
    def test_role_route_selects_provider(self) -> None:
        cfg = LLMCfg(
            provider="mock", model="primary-model",
            routes=[{"role": "coder", "provider": "mock",
                     "model": "coder-model"}])
        provider = make_provider(cfg)
        self.assertIsInstance(provider, Router)
        self.assertEqual(provider.provider_for("coder").model, "coder-model")
        self.assertEqual(provider.provider_for("tester").model, "primary-model")

    async def test_role_route_is_used_for_request(self) -> None:
        cfg = LLMCfg(
            provider="mock", model="primary-model",
            routes=[{"role": "coder", "provider": "mock",
                     "model": "coder-model"}])
        router = make_provider(cfg)
        result = await router.chat_with_tools(
            [{"role": "user", "content": "hi"}], role="coder")
        self.assertEqual(result.model, "coder-model")
        result2 = await router.chat_with_tools(
            [{"role": "user", "content": "hi"}], role="tester")
        self.assertEqual(result2.model, "primary-model")

    async def test_fallback_used_when_primary_unreachable(self) -> None:
        cfg = LLMCfg(
            provider="openai", base_url="http://127.0.0.1:1/v1",
            model="broken", api_key="sk", timeout_s=0.2, retries=0,
            fallback=[{"provider": "mock", "model": "backup"}])
        router = make_provider(cfg)
        self.assertIsInstance(router, Router)
        reply = await router.chat([{"role": "user", "content": "hi"}])
        self.assertIn("mock", reply)
        self.assertEqual(router._last_used, "mock")

    async def test_fallback_is_skipped_for_budget(self) -> None:
        cfg = LLMCfg(
            provider="mock", model="m", project_token_budget=5,
            fallback=[{"provider": "mock"}])
        router = make_provider(cfg)
        with self.assertRaises(BudgetExceeded):
            await router.chat_with_tools(
                [{"role": "user", "content": "x" * 2000}],
                project_id="p-1")

    async def test_breaker_skips_open_primary(self) -> None:
        httpd = HTTPServer(("127.0.0.1", 0), _FailHandler)
        httpd.requests = 0
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = httpd.server_address[:2]
            cfg = LLMCfg(
                provider="openai", base_url=f"http://{host}:{port}/v1",
                model="m", api_key="k", timeout_s=1.0, retries=0,
                fallback=[{"provider": "mock"}])
            router = Router(cfg)
            for _ in range(3):
                reply = await router.chat([{"role": "user", "content": "hi"}])
                self.assertIn("mock", reply)
            self.assertEqual(httpd.requests, 3)
            second = await router.chat([{"role": "user", "content": "hi"}])
            self.assertIn("mock", second)
            # Primary's breaker is open: no fourth HTTP request.
            self.assertEqual(httpd.requests, 3)
            health = router.health()
            self.assertEqual(health["provider"], "router")
            self.assertTrue(any(entry.get("circuit", {}).get("state") == "open"
                                for entry in health["providers"]))
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_merge_specs_does_not_inherit_key_across_vendors(self) -> None:
        base = {"provider": "openai", "base_url": "http://x/v1",
                "api_key": "sk-openai", "model": "m"}
        merged = merge_specs(base, {"provider": "anthropic",
                                    "model": "claude"})
        self.assertEqual(merged["base_url"], "")
        self.assertEqual(merged["api_key"], "")
        same = merge_specs(base, {"model": "m2"})
        self.assertEqual(same["api_key"], "sk-openai")

    def test_unknown_vendor_is_rejected(self) -> None:
        from stackflowos.errors import BadRequest
        with self.assertRaises(BadRequest):
            create_provider({"provider": "bogus"})


class ContextTrimTests(unittest.IsolatedAsyncioTestCase):
    def test_trim_drops_oldest_and_keeps_latest(self) -> None:
        messages = [{"role": "system", "content": "sys"}]
        for index in range(10):
            messages.append({"role": "user",
                             "content": f"turn {index} " + "x" * 600})
            messages.append({"role": "assistant",
                             "content": f"answer {index} " + "y" * 600})
        result = trim_messages(messages, 1024, reserve_tokens=128)
        self.assertGreater(result.dropped, 0)
        self.assertEqual(result.messages[0]["content"], "sys")
        self.assertTrue(any("turn 9" in str(item.get("content"))
                            for item in result.messages))
        self.assertIn("answer 9", result.messages[-1]["content"])
        self.assertLessEqual(result.tokens_after, 1024)

    def test_tool_results_are_clipped(self) -> None:
        messages = [{"role": "user", "content": "read"},
                    {"role": "tool", "tool_call_id": "c1",
                     "content": "z" * 20_000}]
        result = trim_messages(messages, 9000)
        self.assertGreater(result.clipped, 0)
        self.assertLess(len(result.messages[-1]["content"]), 20_000)

    async def test_provider_emits_trimmed_event(self) -> None:
        events: list[tuple[str, dict]] = []
        with tempfile.TemporaryDirectory() as tmp:
            runtime = reset_runtime(state_dir=tmp)
            runtime.event_sink = lambda type, data: events.append((type, data))
            try:
                provider = OpenAICompatibleProvider(
                    base_url="http://127.0.0.1:9/v1", model="m",
                    max_tokens=100, max_context_tokens=1024, timeout_s=0.1,
                    retries=0)
                messages = [{"role": "system", "content": "sys"}]
                for index in range(8):
                    messages.append({"role": "user",
                                     "content": f"{index}" + "x" * 900})
                provider._prepare(messages, None)
                self.assertTrue(any(type == "llm.context.trimmed"
                                    for type, _data in events))
            finally:
                reset_runtime()


class UsagePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_providers_share_one_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = reset_runtime(state_dir=tmp)
            try:
                first = MockProvider(replies=["a"])
                second = MockProvider(replies=["b"])
                await first.chat([{"role": "user", "content": "x"}],
                                 role="coder", project_id="p-1")
                await second.chat([{"role": "user", "content": "y"}],
                                  role="coder", project_id="p-1")
                snapshot = runtime.usage.snapshot("p-1")
                self.assertEqual(snapshot["project"]["calls"], 2)
                on_disk = UsageTracker(
                    Path(tmp) / "state" / "llm_usage.json").snapshot("p-1")
                self.assertEqual(on_disk["project"]["calls"], 2)
            finally:
                reset_runtime()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

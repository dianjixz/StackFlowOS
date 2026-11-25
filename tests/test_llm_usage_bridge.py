"""Worker->hub LLM usage bridge, configurable prices/breakers, token based
truncation and explicit mid-stream interruption handling.

Everything here is offline: providers are fakes/``MockProvider`` and the
"hub"/"worker" are two :class:`LLMRuntime` instances sharing a temporary
state directory (exactly what two processes sharing ``--state-dir`` do).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from stackflowos.ai.context import (TOOL_RESULT_TOKENS, trim_messages)
from stackflowos.ai.limits import CircuitBreaker
from stackflowos.ai.llm import (LLMBrain, MAX_HISTORY_TOKENS,
                                MockProvider, complete, make_provider)
from stackflowos.ai.providers import BaseProvider, StreamInterrupted
from stackflowos.ai.router import Router
from stackflowos.ai.telemetry import (BudgetExceeded, LLMRuntime, UsageTracker,
                                      estimate_messages_tokens,
                                      estimate_tokens, get_runtime,
                                      reset_runtime)
from stackflowos.ai.types import Usage
from stackflowos.config import (AgentsCfg, ConfigError, LLMCfg, SkillsCfg,
                                _validate_section)
from stackflowos.errors import Unavailable
from stackflowos.units.worker import _make_brain


class _CountingProvider:
    """Provider double that streams one text delta then fails."""

    def __init__(self, text: str = "partial", *, name: str = "fake",
                 model: str = "fake-model") -> None:
        self.name = name
        self.model = model
        self.breaker = CircuitBreaker()
        self.text = text
        self.calls = 0

    async def chat_with_tools(self, messages, tools=None, *, tool_choice=None,
                              role=None, project_id=None, on_delta=None,
                              stream=None):
        self.calls += 1
        if on_delta is not None:
            result = on_delta({"type": "text", "text": self.text})
            if hasattr(result, "__await__"):
                await result
        raise Unavailable("LLM HTTP 500: boom")

    async def stream(self, messages, tools=None, *, tool_choice=None,
                     role=None, project_id=None):
        yield {"type": "text", "text": self.text}
        raise Unavailable("LLM HTTP 500: boom")


class _HealthyProvider:
    """Provider double that must never be reached after partial output."""

    name = "healthy"
    model = "healthy-model"

    def __init__(self) -> None:
        self.breaker = CircuitBreaker()
        self.calls = 0

    async def chat_with_tools(self, messages, tools=None, *, tool_choice=None,
                              role=None, project_id=None, on_delta=None,
                              stream=None):
        self.calls += 1
        return SimpleNamespace(text="full answer", tool_calls=[])

    async def stream(self, messages, tools=None, *, tool_choice=None,
                     role=None, project_id=None):
        self.calls += 1
        yield {"type": "text", "text": "full answer"}


class _BrokenSSEProvider(BaseProvider):
    """Real ``BaseProvider`` streaming machinery with a mid-stream failure.

    ``_stream_deltas`` runs the sync iterator in a thread, so overriding
    ``_iter_sse_sync`` exercises the production error path instead of
    replacing the wrapper under test.
    """

    name = "broken"

    def _iter_sse_sync(self, messages, tools, tool_choice):
        yield {"type": "text", "text": "部分"}
        raise Unavailable("LLM HTTP 500: exploded")

    def _candidate_urls(self, *, stream: bool):
        return []


class _LegacyProvider:
    """Old-style provider exposing only ``chat`` (no native tools)."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.name = "legacy"
        self.model = "legacy-model"

    async def chat(self, messages, *, tools=None):
        return self.reply


def _worker_cfg(state_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        llm=LLMCfg(provider="mock", model="mock-worker",
                   price_per_1k_input=1.0, price_per_1k_output=2.0,
                   fallback=[{"provider": "mock", "model": "backup"}]),
        agents=AgentsCfg(),
        skills=SkillsCfg(inject_max_tokens=64),
        paths=SimpleNamespace(state_dir=state_dir),
    )


class WorkerUsageBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_brain_writes_usage_the_hub_tracker_can_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            # The hub built its tracker at boot; the worker is another process.
            hub = LLMRuntime()
            hub.configure(state_dir=tmp)
            reset_runtime()
            try:
                cfg = _worker_cfg(Path(tmp))
                brain = _make_brain("coder", cfg)
                self.assertIsInstance(brain, LLMBrain)
                runtime = get_runtime()
                self.assertEqual(
                    runtime.usage.path,
                    Path(tmp) / "state" / "llm_usage.json")
                # The worker's provider is wired to the same runtime.
                await brain.provider.chat_with_tools(
                    [{"role": "user", "content": "hi"}],
                    role="coder", project_id="p-worker")
                # Hub "main state" sees the worker's rows, project and cost.
                snapshot = hub.usage.snapshot("p-worker")
                self.assertEqual(snapshot["project"]["calls"], 1)
                self.assertGreaterEqual(snapshot["project"]["total"], 1)
                self.assertIn("mock", snapshot["providers"])
                self.assertGreater(snapshot["totals"]["cost"], 0.0)
                # The budget check (also the hub's llm_usage/budget view) uses
                # the shared counts, so a worker call can exhaust a project.
                used = snapshot["project"]["total"]
                with self.assertRaises(BudgetExceeded):
                    hub.usage.check_budget("p-worker", used, used)
                # And the file itself is the durable bridge.
                on_disk = UsageTracker(
                    Path(tmp) / "state" / "llm_usage.json")
                self.assertEqual(on_disk.project_tokens("p-worker"), used)
            finally:
                reset_runtime()

    async def test_hub_write_after_worker_write_merges_instead_of_clobbering(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = LLMRuntime()
            hub.configure(state_dir=tmp)
            worker = LLMRuntime()
            worker.configure(state_dir=tmp)
            # Worker records first (process exit would normally lose this).
            worker.record(project_id="p-1", role="coder", provider="mock",
                          model="m", usage=Usage(10, 5, 15),
                          price_per_1k_input=1.0)
            # The hub's next write refreshes from disk before mutating.
            hub.record(project_id="p-1", role="assistant", provider="mock",
                       model="m", usage=Usage(2, 1, 3))
            snapshot = hub.usage.snapshot("p-1")
            self.assertEqual(snapshot["project"]["calls"], 2)
            self.assertEqual(snapshot["project"]["total"], 18)
            self.assertEqual(snapshot["roles"]["coder"]["total"], 15)
            self.assertEqual(snapshot["roles"]["assistant"]["total"], 3)


class ConfigurablePriceAndBreakerTests(unittest.IsolatedAsyncioTestCase):
    def test_defaults_are_unchanged(self):
        cfg = LLMCfg()
        self.assertEqual(cfg.price_per_1k_input, 0.0)
        self.assertEqual(cfg.price_per_1k_output, 0.0)
        self.assertEqual(cfg.circuit_threshold, 3)
        self.assertEqual(cfg.circuit_cooldown_s, 30.0)
        provider = make_provider(cfg)
        self.assertEqual(provider.price_per_1k_input, 0.0)
        self.assertEqual(provider.breaker.threshold, 3)
        self.assertEqual(provider.breaker.cooldown_s, 30.0)

    def test_primary_cfg_prices_and_breaker_reach_the_provider(self):
        cfg = LLMCfg(provider="openai", base_url="http://127.0.0.1:9/v1",
                     model="m", price_per_1k_input=0.5,
                     price_per_1k_output=1.5, circuit_threshold=7,
                     circuit_cooldown_s=1.25)
        provider = make_provider(cfg)
        self.assertEqual(provider.price_per_1k_input, 0.5)
        self.assertEqual(provider.price_per_1k_output, 1.5)
        self.assertEqual(provider.breaker.threshold, 7)
        self.assertEqual(provider.breaker.cooldown_s, 1.25)

    def test_route_can_override_primary_prices(self):
        cfg = LLMCfg(provider="mock", model="primary",
                     price_per_1k_input=1.0,
                     fallback=[{"provider": "mock", "model": "backup",
                                "price_per_1k_input": 9.0}])
        router = make_provider(cfg)
        self.assertEqual(router.primary.price_per_1k_input, 1.0)
        self.assertEqual(router.fallback[0].price_per_1k_input, 9.0)

    async def test_mock_provider_records_configured_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = reset_runtime(state_dir=tmp)
            try:
                provider = make_provider(LLMCfg(
                    provider="mock", price_per_1k_input=1.0,
                    price_per_1k_output=2.0))
                result = await provider.chat_with_tools(
                    [{"role": "user", "content": "hello"}],
                    project_id="p-cost", role="coder")
                expected = (result.usage.prompt / 1000.0 * 1.0
                            + result.usage.completion / 1000.0 * 2.0)
                snapshot = runtime.usage.snapshot("p-cost")
                self.assertAlmostEqual(snapshot["project"]["cost"], expected,
                                       places=8)
            finally:
                reset_runtime()

    def test_invalid_price_and_breaker_values_are_rejected(self):
        with self.assertRaises(ConfigError):
            _validate_section("llm", LLMCfg(price_per_1k_input=-1))
        with self.assertRaises(ConfigError):
            _validate_section("llm", LLMCfg(circuit_threshold=0))
        with self.assertRaises(ConfigError):
            _validate_section("llm", LLMCfg(circuit_cooldown_s=-0.1))
        _validate_section("llm", LLMCfg())  # defaults stay valid


class TokenEstimatorUnificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_complete_uses_the_shared_estimator(self):
        result = await complete(_LegacyProvider("abcdefghij"),
                                [{"role": "user", "content": "hi"}])
        self.assertEqual(result.usage.completion, estimate_tokens("abcdefghij"))

    def test_skill_injection_is_clipped_by_tokens(self):
        rendered = "技能说明" * 2000
        brain = LLMBrain(MockProvider(), "coder",
                         skill_renderer=lambda pid, role, prompt: rendered,
                         skill_max_tokens=20)
        text = brain._skills(SimpleNamespace(task={}, project={}, agent=None))
        self.assertLess(len(text), len(rendered))
        self.assertLessEqual(estimate_tokens(text), 20)

    def test_history_trim_uses_the_shared_token_budget(self):
        brain = LLMBrain(MockProvider(), "coder")
        brain._history.append({"role": "user", "content": "opening"})
        for index in range(10):
            brain._history.append({"role": "user",
                                   "content": f"{index}" + "x" * 5000})
            brain._history.append({"role": "assistant",
                                   "content": f"{index}" + "y" * 5000})
        brain._trim()
        self.assertEqual(brain._history[0]["content"], "opening")
        self.assertLessEqual(estimate_messages_tokens(brain._history),
                             MAX_HISTORY_TOKENS)

    def test_context_tool_clip_uses_the_shared_token_budget(self):
        messages = [{"role": "user", "content": "read"},
                    {"role": "tool", "tool_call_id": "c1",
                     "content": "z" * 20_000}]
        result = trim_messages(messages, 9000)
        self.assertGreater(result.clipped, 0)
        clipped = result.messages[-1]["content"]
        self.assertLess(len(clipped), 20_000)
        self.assertLessEqual(estimate_tokens(clipped), TOOL_RESULT_TOKENS)


class PartialStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_does_not_fail_over_after_partial_output(self):
        cfg = LLMCfg(provider="mock", fallback=[{"provider": "mock"}])
        router = Router(cfg)
        first = _CountingProvider("你好")
        second = _HealthyProvider()
        router.primary = first
        router.fallback = [second]
        router.routes = []
        seen: list[str] = []

        async def on_delta(delta: dict) -> None:
            if delta.get("type") == "text":
                seen.append(delta["text"])

        with self.assertRaises(StreamInterrupted) as caught:
            await router.chat_with_tools(
                [{"role": "user", "content": "hi"}], on_delta=on_delta)
        exc = caught.exception
        self.assertEqual(exc.partial_text, "你好")
        self.assertEqual(exc.partial_chars, 2)
        self.assertIn("interrupted", exc.message)
        self.assertIn("partial", exc.message.lower())
        self.assertEqual(seen, ["你好"])
        self.assertEqual(second.calls, 0)

    async def test_router_stream_raises_explicit_interruption(self):
        cfg = LLMCfg(provider="mock", fallback=[{"provider": "mock"}])
        router = Router(cfg)
        router.primary = _CountingProvider("abc")
        router.fallback = [_HealthyProvider()]
        router.routes = []
        deltas: list[dict] = []
        with self.assertRaises(StreamInterrupted) as caught:
            async for delta in router.stream(
                    [{"role": "user", "content": "hi"}]):
                deltas.append(delta)
        self.assertEqual(caught.exception.partial_text, "abc")
        self.assertEqual(caught.exception.partial_chars, 3)
        self.assertEqual([d["text"] for d in deltas], ["abc"])

    async def test_provider_keeps_partial_usage_and_raises_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = reset_runtime(state_dir=tmp)
            try:
                provider = _BrokenSSEProvider(model="m")
                seen: list[str] = []

                async def on_delta(delta: dict) -> None:
                    if delta.get("type") == "text":
                        seen.append(delta["text"])

                with self.assertRaises(StreamInterrupted) as caught:
                    await provider.chat_with_tools(
                        [{"role": "user", "content": "hi"}],
                        project_id="p-stream", role="coder",
                        on_delta=on_delta)
                exc = caught.exception
                self.assertEqual(exc.partial_text, "部分")
                self.assertEqual(exc.partial_chars, 2)
                self.assertIn("interrupted", exc.message)
                # A failed stream was still billed: keep partial completion.
                snapshot = runtime.usage.snapshot("p-stream")
                self.assertEqual(snapshot["project"]["calls"], 1)
                self.assertEqual(snapshot["project"]["completion"],
                                 estimate_tokens("部分"))
                self.assertEqual(seen, ["部分"])
                self.assertEqual(provider._stats["failures"], 1)
            finally:
                reset_runtime()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

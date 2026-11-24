"""LLM extension routes: usage/quota and model catalogue."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stackflowos.ai.llm import make_provider
from stackflowos.ai.llm_api import build
from stackflowos.app import App
from stackflowos.config import Config, LLMCfg, default_root
from stackflowos.web.api import WebAPI

REPO = default_root()


class LLMApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        base = REPO / ".stackflowos" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(prefix="llm-api-", dir=str(base))
        self.tmp = Path(self._tmp.name)
        self.app = App(Config.load(root=self.tmp, state_dir=self.tmp))
        await self.app.start()
        self.extension = build(self.app)
        self.app.extensions.append(self.extension)

    async def asyncTearDown(self) -> None:
        await self.app.stop()
        self._tmp.cleanup()

    async def test_usage_endpoint_aggregates_and_budget(self) -> None:
        await self.app.provider.chat(
            [{"role": "user", "content": "hello"}],
            role="assistant", project_id="p-usage")
        self.app.config.llm.project_token_budget = 1000
        api = WebAPI(self.app)
        status, payload = await api.handle(
            "GET", "/api/llm/usage", {"project_id": "p-usage"}, b"")
        self.assertEqual(status, 200, payload)
        data = payload["data"]
        self.assertEqual(data["project_id"], "p-usage")
        self.assertGreaterEqual(data["project"]["total"], 1)
        self.assertEqual(data["budget"]["project_token_budget"], 1000)
        self.assertIn("remaining", data["budget"])
        self.assertIn("limits", data)
        self.assertIn("circuit", data)
        self.assertIn("providers_health", data)
        # Usage is persisted under state/llm_usage.json.
        self.assertTrue((self.tmp / "state" / "llm_usage.json").is_file())
        # Aggregates by role/provider/model are present.
        self.assertIn("assistant", data["roles"])
        self.assertIn("mock", data["providers"])
        self.assertIn("mock", data["models"])
        # And an llm.usage event was emitted.
        types = [record.type for record in self.app.eventlog.tail(100)]
        self.assertIn("llm.usage", types)

    async def test_models_endpoint_reports_primary_and_health(self) -> None:
        api = WebAPI(self.app)
        status, payload = await api.handle("GET", "/api/llm/models", {}, b"")
        self.assertEqual(status, 200, payload)
        data = payload["data"]
        self.assertEqual(data["primary"]["provider"], "mock")
        self.assertEqual(data["configured"]["provider"], "mock")
        self.assertIn("health", data)
        self.assertTrue(data["primary"]["ok"])

    async def test_models_endpoint_reports_routes_and_fallback(self) -> None:
        cfg = LLMCfg(
            provider="mock", model="primary",
            routes=[{"role": "coder", "provider": "mock",
                     "model": "coder-model"}],
            fallback=[{"provider": "mock", "model": "backup"}])
        router = make_provider(cfg)
        self.app.provider = router
        self.app.chat.provider = router
        self.app.config.llm = cfg
        extension = build(self.app)
        self.app.extensions.append(extension)
        api = WebAPI(self.app)
        status, payload = await api.handle("GET", "/api/llm/models", {}, b"")
        self.assertEqual(status, 200, payload)
        data = payload["data"]
        self.assertTrue(data["routes"])
        self.assertEqual(data["routes"][0]["role"], "coder")
        self.assertEqual(data["routes"][0]["model"], "coder-model")
        self.assertEqual(data["fallback"][0]["model"], "backup")
        # Keys never leak (the mock key is empty, but assert the shape).
        self.assertNotIn("api_key", str(data))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Changing the model in Settings must take effect without a hub restart."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stackflowos.app import App
from stackflowos.config import Config, default_root
from stackflowos.errors import ConfigError

REPO = default_root()


class LlmReloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        cfg = Config.load(root=self.tmp, state_dir=self.tmp)
        self.app = App(cfg)
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    async def test_update_config_rebuilds_chat_provider(self):
        self.assertEqual(self.app.provider.name, "mock")
        await self.app.update_config({
            "llm": {"provider": "openai",
                    "base_url": "http://127.0.0.1:9/v1",
                    "model": "test-model",
                    "api_key": "sk-test"},
        })
        self.assertEqual(self.app.provider.name, "openai")
        self.assertEqual(self.app.provider.model, "test-model")
        self.assertIs(self.app.chat.provider, self.app.provider)
        self.assertEqual(self.app.config.llm.provider, "openai")

    async def test_masked_key_is_not_written_back(self):
        await self.app.update_config({
            "llm": {"provider": "openai",
                    "base_url": "http://127.0.0.1:9/v1",
                    "model": "m", "api_key": "sk-real"},
        })
        await self.app.update_config({"llm": {"api_key": "***"}})
        self.assertEqual(self.app.config.llm.api_key, "sk-real")

    async def test_openai_requires_model(self):
        with self.assertRaises(ConfigError):
            await self.app.update_config({
                "llm": {"provider": "openai",
                        "base_url": "http://127.0.0.1:9/v1", "model": ""},
            })
        # The rejected patch did not stick.
        self.assertEqual(self.app.config.llm.provider, "mock")

    async def test_llm_test_endpoint_shape(self):
        result = await self.app.provider.ping()
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "mock")


if __name__ == "__main__":
    unittest.main()

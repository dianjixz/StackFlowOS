"""Tests for the optional-extension contract and config redaction."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

from stackflowos import extensions
from stackflowos.config import Config, DEFAULTS, redact_secrets


class _FakeExtension:
    name = "fake"

    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.configs = []

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1

    async def on_config(self, config):
        self.configs.append(config)

    def units(self):
        return [{"unit_id": "skill.0", "state": "ready", "provides": {"skills": 2}}]

    def routes(self):
        async def handler(match, query, body):
            return 200, {"ok": True, "data": {"fake": True}}

        return [("GET", r"/api/fake", handler)]

    def capabilities(self, kind=None):
        return [{"type": "skill", "name": "fake-skill", "provider": "skill.0"}]

    def health(self):
        return {"ok": True, "items": 1}


class ExtensionContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module_name = "stackflowos_fake_extension_for_test"
        module = types.ModuleType(self.module_name)
        self.extension = _FakeExtension()
        module.build = lambda app: self.extension
        sys.modules[self.module_name] = module
        self._old = extensions.EXTENSION_MODULES
        extensions.EXTENSION_MODULES = (self.module_name,)

    def tearDown(self):
        extensions.EXTENSION_MODULES = self._old
        sys.modules.pop(self.module_name, None)

    async def test_load_and_lifecycle(self):
        loaded = extensions.load_extensions(object())
        self.assertEqual([item.name for item in loaded], ["fake"])
        await extensions.start_extensions(loaded)
        await extensions.stop_extensions(loaded)
        self.assertEqual((self.extension.started, self.extension.stopped), (1, 1))

    async def test_contributions(self):
        loaded = extensions.load_extensions(object())
        self.assertEqual(extensions.extension_units(loaded)[0]["state"], "ready")
        routes = extensions.extension_routes(loaded)
        self.assertEqual(routes[0][0], "GET")
        status, payload = await routes[0][2](None, {}, {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["fake"])
        caps = extensions.extension_capabilities(loaded, "skill")
        self.assertEqual(caps[0]["name"], "fake-skill")
        self.assertTrue(extensions.extension_health(loaded)["fake"]["ok"])

    async def test_broken_build_is_isolated(self):
        bad = types.ModuleType(self.module_name + "_bad")
        bad.build = lambda app: (_ for _ in ()).throw(RuntimeError("boom"))
        sys.modules[bad.__name__] = bad
        try:
            extensions.EXTENSION_MODULES = (bad.__name__,)
            self.assertEqual(extensions.load_extensions(object()), [])
        finally:
            sys.modules.pop(bad.__name__, None)


class ConfigRedactionTests(unittest.TestCase):
    def test_public_dict_masks_secrets_and_roundtrips(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Config.load(state_dir=Path(tmp.name))
        cfg = cfg.update({
            "llm": {"api_key": "sk-secret",
                    "routes": [{"role": "coder", "provider": "openai",
                                "api_key": "sk-route"}],
                    "fallback": [{"provider": "openai", "api_key": "sk-fb"}]},
            "observability": {"webhooks": [
                {"type": "webhook", "url": "https://example.test/hook",
                 "headers": {"Authorization": "Bearer xyz"}}]},
        })
        public = cfg.public_dict()
        self.assertEqual(public["llm"]["api_key"], "***")
        self.assertEqual(public["llm"]["routes"][0]["api_key"], "***")
        self.assertEqual(
            public["observability"]["webhooks"][0]["headers"]["Authorization"],
            "***")
        self.assertNotIn("sk-secret", str(public))
        # PATCHing the masked view back must preserve the real values.
        again = cfg.update({"llm": {"api_key": "***",
                                    "routes": public["llm"]["routes"]}})
        self.assertEqual(again.llm.api_key, "sk-secret")
        self.assertEqual(again.llm.routes[0]["api_key"], "sk-route")
        self.assertNotIn("sk-secret", str(again.public_dict()))

    def test_new_sections_have_defaults(self):
        for section in ("skills", "plugins", "mcp", "observability",
                        "storage", "cluster"):
            self.assertIn(section, DEFAULTS)

    def test_redact_secrets_is_recursive(self):
        data = {"a": {"api_key": "x", "nested": [{"token": "y"}]},
                "count": 1}
        masked = redact_secrets(data)
        self.assertEqual(masked["a"]["api_key"], "***")
        self.assertEqual(masked["a"]["nested"][0]["token"], "***")
        self.assertEqual(masked["count"], 1)


if __name__ == "__main__":
    unittest.main()


class ExtensionApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_extension_routes_and_raw_reply(self):
        from stackflowos.web.api import WebAPI, WebReply

        async def json_handler(match, query, body):
            return 200, {"ok": True, "data": {"json": True}}

        async def text_handler(match, query, body):
            return 200, WebReply("sfo_up 1\n", content_type="text/plain")

        class Ext:
            name = "routes"

            def routes(self):
                return [("GET", r"/api/ext-json", json_handler),
                        ("GET", r"/api/ext-text", text_handler)]

        class App:
            extensions = [Ext()]

        api = WebAPI(App())
        status, payload = await api.handle("GET", "/api/ext-json", {}, b"")
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["json"])
        status, payload = await api.handle("GET", "/api/ext-text", {}, b"")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload, WebReply)
        self.assertEqual(payload.body, b"sfo_up 1\n")
        self.assertEqual(status, 200)

    async def test_auth_context_is_visible_to_handler(self):
        from stackflowos.web.api import WebAPI, current_auth

        seen = {}

        async def handler(match, query, body):
            seen["role"] = getattr(current_auth(), "role", None)
            return 200, {"ok": True}

        class Ext:
            name = "auth"

            def routes(self):
                return [("GET", r"/api/whoami", handler)]

        class App:
            extensions = [Ext()]

        class Decision:
            role = "viewer"

        await WebAPI(App()).handle("GET", "/api/whoami", {}, b"", {},
                                   Decision())
        self.assertEqual(seen["role"], "viewer")


class AppExtensionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_merges_extension_units_and_lifecycle(self):
        from stackflowos.app import App

        module_name = "stackflowos_fake_app_extension"
        state = {"started": 0, "stopped": 0}

        class Ext:
            name = "appfake"

            async def start(self):
                state["started"] += 1

            async def stop(self):
                state["stopped"] += 1

            def health(self):
                return {"ok": True}

            def units(self):
                return [{
                    "unit_id": "skill.0", "work_id": "skill.0",
                    "kind": "skill", "tier": "service", "label": "技能",
                    "state": "ready", "status": "online", "pid": None,
                    "runtime": {"mode": "in_process"},
                    "provides": {"skills": 3}, "actions": ["probe"],
                }]

        module = types.ModuleType(module_name)
        module.build = lambda app: Ext()
        sys.modules[module_name] = module
        old = extensions.EXTENSION_MODULES
        extensions.EXTENSION_MODULES = (module_name,)
        tmp = tempfile.TemporaryDirectory()
        try:
            app = App(Config.load(state_dir=Path(tmp.name)))
            await app.start()
            try:
                cards = {u["unit_id"]: u for u in app.unit_cards()}
                self.assertEqual(cards["skill.0"]["state"], "ready")
                self.assertEqual(cards["skill.0"]["provides"]["skills"], 3)
                self.assertEqual(state["started"], 1)
                self.assertIn("appfake", app.health()["extensions"])
            finally:
                await app.stop()
            self.assertEqual(state["stopped"], 1)
        finally:
            extensions.EXTENSION_MODULES = old
            sys.modules.pop(module_name, None)
            tmp.cleanup()


class StandbyLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_standby_waits_for_hub_lock(self):
        import asyncio
        import fcntl
        import os

        from stackflowos.app import App

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Config.load(state_dir=Path(tmp.name)).with_overrides({
            "cluster": {"standby": True, "standby_poll_s": 0.05},
        })
        lock_path = cfg.paths.run_dir / "hub.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            app = App(cfg)
            task = asyncio.create_task(app._acquire_hub_lock())
            await asyncio.sleep(0.15)
            self.assertFalse(task.done(), "standby must not steal an active lock")
            fcntl.flock(holder, fcntl.LOCK_UN)
            await asyncio.wait_for(task, timeout=2)
            self.assertIsNotNone(app._lock_fd)
            app._release_hub_lock()
        finally:
            try:
                fcntl.flock(holder, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(holder)

    async def test_non_standby_fails_fast(self):
        import fcntl
        import os

        from stackflowos.app import App
        from stackflowos.errors import Unavailable

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Config.load(state_dir=Path(tmp.name))
        lock_path = cfg.paths.run_dir / "hub.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(Unavailable):
                await App(cfg)._acquire_hub_lock()
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.close(holder)


class RawReplyServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_metrics_style_text_response(self):
        import asyncio
        import json
        import urllib.request

        from stackflowos.web.server import WebServer
        from tests.test_web import FakeApp

        async def metrics(match, query, body):
            from stackflowos.web.api import WebReply

            return 200, WebReply("sfo_up 1\n# HELP x\n", content_type=(
                "text/plain; version=0.0.4; charset=utf-8"))

        class Ext:
            name = "metrics"

            def routes(self):
                return [("GET", r"/api/metrics", metrics)]

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = FakeApp(Path(tmp.name))
        app.extensions = [Ext()]
        server = WebServer(app, host="127.0.0.1", port=0,
                           assets_dir=Path(tmp.name))
        url = await server.start()
        try:
            def fetch():
                req = urllib.request.Request(url + "/api/metrics")
                req.add_header("Authorization",
                               f"Bearer {server.launch_token}")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, resp.headers.get("Content-Type"), resp.read()

            status, ctype, body = await asyncio.to_thread(fetch)
            self.assertEqual(status, 200)
            self.assertIn("text/plain", ctype)
            self.assertEqual(body, b"sfo_up 1\n# HELP x\n")
        finally:
            await server.stop()
            await app.eventlog.close()

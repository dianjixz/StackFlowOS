"""Capability catalogue: providers, availability, project filtering and calls."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from stackflowos.agents.tools import TOOLS
from stackflowos.app import App
from stackflowos.config import Config, default_root
from stackflowos.errors import BadRequest, NotFound
from stackflowos.web.api import WebAPI

REPO = default_root()


class CapabilityCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        cfg = Config.load(root=self.tmp, state_dir=self.tmp / "state")
        cfg = cfg.update({"skills": {"dirs": [], "enabled": []},
                          "plugins": {"dirs": [], "enabled": []},
                          "mcp": {"servers": []}})
        self.app = App(cfg)
        await self.app.start()
        self.api = WebAPI(self.app)

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    async def api_call(self, method, path, query=None, body=None):
        payload = json.dumps(body or {}).encode("utf-8")
        return await self.api.handle(method, path, query or {}, payload)

    async def test_tools_are_registered_with_provider_and_risk(self):
        status, payload = await self.api_call(
            "GET", "/api/capabilities", {"type": "tool"})
        self.assertEqual(status, 200)
        entries = payload["data"]["capabilities"]
        by_name = {entry["name"]: entry for entry in entries}
        for name in TOOLS:
            self.assertIn(name, by_name)
        for name, entry in by_name.items():
            self.assertEqual(entry["type"], "tool")
            self.assertEqual(entry["provider"], "tool.0")
            self.assertEqual(entry["available"], True)
            self.assertIn(entry["risk"], ("low", "medium", "high"))
            self.assertEqual(entry["schema"]["type"], "object")
        self.assertEqual(by_name["fs.read"]["risk"], "low")
        self.assertEqual(by_name["fs.write"]["risk"], "medium")
        self.assertEqual(by_name["fs.delete"]["risk"], "high")
        self.assertEqual(by_name["shell.exec"]["risk"], "high")
        self.assertEqual(by_name["fs.read"]["capability"], "fs.read")
        self.assertIn("path", by_name["fs.read"]["schema"]["properties"])

    async def test_unknown_type_is_rejected(self):
        status, payload = await self.api_call(
            "GET", "/api/capabilities", {"type": "banana"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "BAD_REQUEST")

    async def test_availability_follows_provider_state(self):
        catalog = self.app.capability_catalog
        catalog.register({"type": "tool", "name": "probe.tool",
                          "provider": "skill.0", "enabled_in": ["*"],
                          "capability": "data.read", "risk": "low"})
        entries = {entry["name"]: entry
                   for entry in catalog.entries(kind="tool")}
        self.assertTrue(entries["probe.tool"]["available"])
        # A provider unit that is not ready makes every entry unavailable.
        self.app.unit_control.sync_state("skill.0", "failed", "boom")
        entries = {entry["name"]: entry
                   for entry in catalog.entries(kind="tool")}
        self.assertFalse(entries["probe.tool"]["available"])
        self.assertEqual(entries["probe.tool"]["provider_state"], "failed")

    async def test_project_allowlist_does_not_leak_entries(self):
        project = self.app.projects.create("Capability Project")
        other = self.app.projects.create("Other Project")
        catalog = self.app.capability_catalog
        catalog.register({"type": "skill", "name": "scoped-skill",
                          "provider": "skill.0",
                          "enabled_in": [project.id],
                          "capability": None, "risk": "low"})
        global_entries = {entry["name"] for entry in catalog.entries()}
        self.assertNotIn("scoped-skill", global_entries)
        scoped = {entry["name"] for entry in
                  catalog.entries(project_id=project.id)}
        self.assertIn("scoped-skill", scoped)
        other_entries = {entry["name"] for entry in
                         catalog.entries(project_id=other.id)}
        self.assertNotIn("scoped-skill", other_entries)
        with self.assertRaises(NotFound):
            catalog.entries(project_id="p-does-not-exist")

    async def test_call_capability_rejects_builtin_and_unknown(self):
        catalog = self.app.capability_catalog
        with self.assertRaises(BadRequest):
            await catalog.call_capability("fs.read", {"path": "x"}, None, None)
        with self.assertRaises(NotFound):
            await catalog.call_capability("nope.tool", {}, None, None)

    async def test_call_capability_executes_registered_executor_and_audits(self):
        catalog = self.app.capability_catalog
        calls = []

        async def executor(args, principal, project_id, approval_id=None):
            calls.append((args, principal, project_id, approval_id))
            return {"ok": True, "args": args}

        catalog.register({"type": "tool", "name": "echo.tool",
                          "provider": "skill.0", "enabled_in": ["*"],
                          "capability": "data.read", "risk": "low"})
        catalog.register_tool_executor("skill.0", "echo.tool", executor)
        result = await catalog.call_capability(
            "echo.tool", {"text": "hi"}, None, None)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["executed"])
        self.assertEqual(result["result"]["args"], {"text": "hi"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "human:operator")
        events = [record.type for record in
                  self.app.eventlog.query(types=["capability.called"], limit=10)]
        self.assertIn("capability.called", events)
        # Disposing the executor makes the capability unavailable again.
        catalog._executors.pop(("skill.0", "echo.tool"), None)
        with self.assertRaises(Exception):
            await catalog.call_capability("echo.tool", {}, None, None)


if __name__ == "__main__":
    unittest.main()

"""PluginLoader: manifests, contribution lifecycle, units, approvals and API."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stackflowos.app import App
from stackflowos.config import Config, default_root
from stackflowos.security import Principal
from stackflowos.security.plugin_audit import (high_risk_permissions,
                                               permission_risk)
from stackflowos.web.api import WebAPI

REPO = default_root()
EXAMPLES = REPO / "examples" / "plugins"


class PluginLoaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        cfg = Config.load(root=self.tmp, state_dir=self.tmp / "state")
        cfg = cfg.update({
            "skills": {"dirs": [], "enabled": []},
            "plugins": {"dirs": [str(EXAMPLES)], "enabled": ["hello"]},
            "mcp": {"servers": []},
        })
        self.app = App(cfg)
        await self.app.start()
        self.api = WebAPI(self.app)
        # ``enabled`` only loads at startup; do not depend on timing.
        if self.app.plugin_loader.get("hello").status != "loaded":
            await self.app.plugin_loader.load("hello")

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    async def api_call(self, method, path, query=None, body=None):
        return await self.api.handle(method, path, query or {},
                                     json.dumps(body or {}).encode("utf-8"))

    def units(self):
        return {unit["unit_id"]: unit for unit in self.app.unit_cards()}

    def catalog_tools(self):
        return {entry["name"]: entry for entry in
                self.app.capability_catalog.entries(kind="tool")}

    async def test_example_plugin_loads_with_units_catalog_and_skill(self):
        units = self.units()
        self.assertIn("plugin.hello", units)
        self.assertEqual(units["plugin.hello"]["state"], "ready")
        self.assertEqual(units["plugin.hello"]["runtime"]["mode"], "in_process")
        self.assertIsNone(units["plugin.hello"]["pid"])
        self.assertEqual(units["plugin.hello"]["provides"],
                         {"tools": 1, "skills": 1})
        self.assertEqual(units["plugin.0"]["state"], "ready")
        self.assertEqual(units["plugin.0"]["provides"]["plugins"], 1)
        tools = self.catalog_tools()
        self.assertIn("plugin.echo", tools)
        self.assertEqual(tools["plugin.echo"]["provider"], "plugin.hello")
        self.assertEqual(tools["plugin.echo"]["available"], True)
        self.assertEqual(tools["plugin.echo"]["risk"], "medium")
        skill_ids = {entry.id for entry in self.app.skills.all_entries()}
        self.assertIn("hello-skill", skill_ids)
        self.assertTrue(self.app.skills.is_enabled("hello-skill"))
        capability_skills = {entry["name"]: entry for entry in
                             self.app.capability_catalog.entries(kind="skill")}
        self.assertIn("hello-skill", capability_skills)
        self.assertEqual(capability_skills["hello-skill"]["provider"],
                         "plugin.hello")
        self.assertTrue(capability_skills["hello-skill"]["available"])
        plugin_entries = {entry["name"]: entry for entry in
                          self.app.capability_catalog.entries(kind="plugin")}
        self.assertIn("hello", plugin_entries)
        self.assertEqual(plugin_entries["hello"]["provider"], "plugin.hello")
        self.assertTrue(plugin_entries["hello"]["available"])

    async def test_api_lists_plugins_and_unload_load_cycle(self):
        status, payload = await self.api_call("GET", "/api/plugins")
        self.assertEqual(status, 200)
        plugins = {item["id"]: item for item in payload["data"]["plugins"]}
        self.assertIn("hello", plugins)
        self.assertEqual(plugins["hello"]["status"], "loaded")
        self.assertEqual(plugins["hello"]["contributes"]["tools"],
                         ["plugin.echo"])
        status, payload = await self.api_call(
            "POST", "/api/plugins/hello/unload", {}, {})
        self.assertEqual(status, 200)
        self.assertNotIn("plugin.echo", self.catalog_tools())
        self.assertEqual(self.units()["plugin.hello"]["state"], "disabled")
        self.assertNotIn("hello-skill",
                         {entry.id for entry in self.app.skills.all_entries()})
        records = [record.type for record in
                   self.app.eventlog.query(types=["plugin"], limit=50)]
        self.assertIn("plugin.loaded", records)
        self.assertIn("plugin.unloaded", records)
        status, payload = await self.api_call(
            "POST", "/api/plugins/hello/load", {}, {})
        self.assertEqual(status, 200)
        self.assertIn("plugin.echo", self.catalog_tools())
        self.assertEqual(self.units()["plugin.hello"]["state"], "ready")

    async def test_disable_enable_unit_actions_unload_the_plugin(self):
        status, payload = await self.api_call(
            "POST", "/api/units/plugin.hello/actions", {}, {"action": "disable"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["unit"]["state"], "disabled")
        self.assertNotIn("plugin.echo", self.catalog_tools())
        self.assertEqual(self.app.plugin_loader.get("hello").status,
                         "discovered")
        status, payload = await self.api_call(
            "POST", "/api/units/plugin.hello/actions", {}, {"action": "enable"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["unit"]["state"], "ready")
        self.assertIn("plugin.echo", self.catalog_tools())

    async def test_capability_call_requires_approval_then_executes_once(self):
        catalog = self.app.capability_catalog
        principal = Principal(kind="unit", id="coder.1", project_id=None)
        first = await catalog.call_capability(
            "plugin.echo", {"text": "hi"}, None, principal)
        self.assertEqual(first["status"], "pending")
        self.assertFalse(first["executed"])
        approval_id = first["approval_id"]
        self.assertTrue(approval_id)
        self.app.approvals.decide(approval_id, decision="allow", mode="once",
                                  by="operator")
        second = await catalog.call_capability(
            "plugin.echo", {"text": "hi"}, None, principal,
            approval_id=approval_id)
        self.assertEqual(second["status"], "ok")
        self.assertTrue(second["executed"])
        self.assertEqual(second["result"]["echo"], "hi")
        self.assertEqual(second["result"]["caller"], "coder.1")
        # A one-shot approval can never execute twice.
        third = await catalog.call_capability(
            "plugin.echo", {"text": "hi"}, None, principal,
            approval_id=approval_id)
        self.assertFalse(third["executed"])
        self.assertEqual(third["status"], "claimed")
        events = [record.type for record in
                  self.app.eventlog.query(types=["capability"], limit=50)]
        self.assertIn("capability.pending", events)
        self.assertIn("capability.called", events)

    async def test_apply_failure_marks_plugin_failed(self):
        broken = self.tmp / "apply_plugins"
        (broken / "boom").mkdir(parents=True)
        (broken / "boom" / "stackflow.plugin.json").write_text(json.dumps({
            "id": "boom", "name": "Boom", "version": "1.0.0",
            "entry": {"type": "python", "module": "boom_plugin:apply"},
        }), "utf-8")
        (broken / "boom" / "boom_plugin.py").write_text(
            "def apply(ctx, provides):\n    raise RuntimeError('boom')\n",
            "utf-8")
        self.app.config = self.app.config.update({
            "plugins": {"dirs": [str(EXAMPLES), str(broken)],
                        "enabled": ["hello"]}})
        self.app.plugin_loader.discover()
        with self.assertRaises(Exception):
            await self.app.plugin_loader.load("boom")
        record = self.app.plugin_loader.get("boom")
        self.assertEqual(record.status, "failed")
        self.assertIn("boom", record.error)
        self.assertEqual(self.units()["plugin.boom"]["state"], "failed")
        self.assertEqual(self.units()["plugin.0"]["state"], "degraded")

    async def test_declared_but_missing_contributions_degrade_unit(self):
        declared = self.tmp / "declared_plugins"
        (declared / "declared").mkdir(parents=True)
        (declared / "declared" / "stackflow.plugin.json").write_text(json.dumps({
            "id": "declared", "name": "Declared", "version": "1.0.0",
            "entry": {"type": "python", "module": "declared_plugin:apply"},
            "contributes": {"tools": ["missing.tool"], "skills": []},
        }), "utf-8")
        (declared / "declared" / "declared_plugin.py").write_text(
            "def apply(ctx, provides):\n    return None\n", "utf-8")
        self.app.config = self.app.config.update({
            "plugins": {"dirs": [str(EXAMPLES), str(declared)],
                        "enabled": ["hello"]}})
        self.app.plugin_loader.discover()
        record = await self.app.plugin_loader.load("declared")
        self.assertEqual(record.status, "loaded")
        units = self.units()
        self.assertEqual(units["plugin.declared"]["state"], "degraded")
        self.assertIn("tool:missing.tool",
                      units["plugin.declared"]["metadata"]["missing_contributions"])
        self.assertEqual(units["plugin.0"]["state"], "degraded")

    async def test_rpc_capability_call_bridge(self):
        self.app.policy.add_rule(effect="allow", capability="shell.exec",
                                 principal="coder.1", scope={},
                                 risk_max="high")
        principal = Principal(kind="unit", id="coder.1", project_id=None)
        result = await self.app.handle_rpc_action(
            "capability.call", {"name": "plugin.echo",
                                "args": {"text": "rpc"}},
            src="coder.1", external=True, principal=principal)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["result"]["echo"], "rpc")
        self.assertEqual(result["result"]["caller"], "coder.1")

    async def test_invalid_manifest_degrades_host_without_crashing(self):
        broken = self.tmp / "broken_plugins"
        (broken / "bad").mkdir(parents=True)
        (broken / "bad" / "stackflow.plugin.json").write_text(json.dumps({
            "id": "bad", "name": "Bad", "entry": {"type": "python",
                                                  "module": "x:apply"},
        }), "utf-8")
        self.app.config = self.app.config.update({
            "plugins": {"dirs": [str(EXAMPLES), str(broken)],
                        "enabled": ["hello"]}})
        self.app.plugin_loader.discover()
        records = {record.id: record for record in self.app.plugin_loader.list()}
        self.assertIn("bad", records)
        self.assertFalse(records["bad"].valid)
        units = self.units()
        self.assertEqual(units["plugin.0"]["state"], "degraded")
        self.assertIn("bad", units["plugin.0"]["reason"])
        with self.assertRaises(Exception):
            await self.app.plugin_loader.load("bad")
        status, payload = await self.api_call("GET", "/api/plugins")
        by_id = {item["id"]: item for item in payload["data"]["plugins"]}
        self.assertEqual(by_id["bad"]["status"], "invalid")


    # -- manifest permission audit (W3 item 4) ---------------------------
    async def _wait_for_security_event(self, predicate, timeout: float = 3.0):
        import asyncio

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for record in self.app.eventlog.query(types=["security"], limit=100):
                if record.type == "security.plugin_permissions" and predicate(record):
                    return record
            await asyncio.sleep(0.01)
        self.fail("security.plugin_permissions event was not recorded")

    async def test_high_risk_plugin_permissions_are_warned_and_audited(self):
        risky = self.tmp / "risky_plugins"
        (risky / "risky").mkdir(parents=True)
        (risky / "risky" / "stackflow.plugin.json").write_text(json.dumps({
            "id": "risky", "name": "Risky", "version": "1.0.0",
            "entry": {"type": "python", "module": "risky_plugin:apply"},
            "permissions": ["inproc", "net:api.example.com",
                            "secret:API_KEY"],
        }), "utf-8")
        (risky / "risky" / "risky_plugin.py").write_text(
            "def apply(ctx, provides):\n    return None\n", "utf-8")
        self.app.config = self.app.config.update({
            "plugins": {"dirs": [str(EXAMPLES), str(risky)],
                        "enabled": ["hello"]}})
        self.app.plugin_loader.discover()
        with self.assertLogs("sfo.security.plugins", level="WARNING") as captured:
            await self.app.plugin_loader.load("risky")
            record = await self._wait_for_security_event(
                lambda item: item.data.get("plugin_id") == "risky")
        data = record.data
        self.assertEqual(data["enforced"], False)
        self.assertEqual(data["source"], "plugin.loaded")
        self.assertIn("net:api.example.com", data["high_risk"])
        self.assertIn("secret:API_KEY", data["high_risk"])
        self.assertNotIn("inproc", data["high_risk"])
        self.assertTrue(any("NOT enforced" in line for line in captured.output))
        # The low-risk example plugin does not produce audit noise.
        hello_events = [item for item in
                        self.app.eventlog.query(types=["security"], limit=100)
                        if item.type == "security.plugin_permissions"
                        and item.data.get("plugin_id") == "hello"]
        self.assertEqual(hello_events, [])

    def test_permission_risk_classification_is_conservative(self):
        self.assertEqual(permission_risk("inproc"), "low")
        self.assertEqual(permission_risk(""), "low")
        for declared in ("net:api.github.com", "secret:github_token",
                         "shell", "fs:workspace", "unknown-capability"):
            self.assertEqual(permission_risk(declared), "high", declared)
        self.assertEqual(
            high_risk_permissions(["inproc", "net:api.github.com", "shell"]),
            ["net:api.github.com", "shell"])


class PluginPermissionStartupTests(unittest.IsolatedAsyncioTestCase):
    """A plugin loaded during startup is audited by the startup scan."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        risky = self.tmp / "plugins"
        (risky / "risky").mkdir(parents=True)
        (risky / "risky" / "stackflow.plugin.json").write_text(json.dumps({
            "id": "risky", "name": "Risky", "version": "1.0.0",
            "entry": {"type": "python", "module": "risky_plugin:apply"},
            "permissions": ["net:api.example.com"],
        }), "utf-8")
        (risky / "risky" / "risky_plugin.py").write_text(
            "def apply(ctx, provides):\n    return None\n", "utf-8")
        cfg = Config.load(root=self.tmp, state_dir=self.tmp / "state")
        cfg = cfg.update({
            "skills": {"dirs": [], "enabled": []},
            "plugins": {"dirs": [str(risky)], "enabled": ["risky"]},
            "mcp": {"servers": []},
        })
        self.app = App(cfg)

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    async def test_startup_load_is_audited_and_warned(self):
        with self.assertLogs("sfo.security.plugins", level="WARNING") as captured:
            await self.app.start()
        records = [record for record in
                   self.app.eventlog.query(types=["security"], limit=100)
                   if record.type == "security.plugin_permissions"]
        self.assertTrue(records, "startup scan did not audit the plugin")
        self.assertEqual(records[0].data["plugin_id"], "risky")
        self.assertEqual(records[0].data["source"], "startup")
        self.assertEqual(records[0].data["enforced"], False)
        self.assertTrue(any("NOT enforced" in line for line in captured.output))



if __name__ == "__main__":
    unittest.main()

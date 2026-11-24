"""SkillRegistry: scan, validation, enablement, injection and the skill.0 unit."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stackflowos.app import App
from stackflowos.config import Config, default_root
from stackflowos.web.api import WebAPI

REPO = default_root()

ALPHA = """---
name: Alpha
version: 1.2.0
description: Alpha skill
triggers: hello, 你好
allowed_roles: coder
---
# Alpha

Use this when the user greets you.
"""


class SkillRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        state_dir = self.tmp / "state"
        skills_root = state_dir / "skills"
        (skills_root / "alpha").mkdir(parents=True)
        (skills_root / "alpha" / "SKILL.md").write_text(ALPHA, "utf-8")
        (skills_root / "beta").mkdir(parents=True)
        (skills_root / "beta" / "skill.json").write_text(json.dumps({
            "id": "beta", "name": "Beta", "version": "2.0.0",
            "description": "Beta skill", "instructions": "Beta instructions.",
            "triggers": ["beta"], "allowed_roles": [],
            "requires_tools": [],
        }), "utf-8")
        (skills_root / "bad").mkdir(parents=True)
        (skills_root / "bad" / "skill.json").write_text(json.dumps({
            "id": "bad", "name": "Bad", "version": "1.0.0",
            "description": "missing instructions",
        }), "utf-8")
        cfg = Config.load(root=self.tmp, state_dir=state_dir)
        cfg = cfg.update({
            "skills": {"dirs": ["skills"], "enabled": ["alpha"],
                       "inject_max_chars": 8000},
            "plugins": {"dirs": [], "enabled": []},
            "mcp": {"servers": []},
        })
        self.app = App(cfg)
        await self.app.start()
        self.api = WebAPI(self.app)
        self.skills = self.app.skills

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    async def api_call(self, method, path, query=None, body=None):
        return await self.api.handle(method, path, query or {},
                                     json.dumps(body or {}).encode("utf-8"))

    async def test_scan_validates_invalid_entries_without_breaking_registry(self):
        entries = {entry.id: entry for entry in self.skills.all_entries()}
        self.assertEqual(set(entries), {"alpha", "beta", "bad"})
        self.assertTrue(entries["alpha"].valid)
        self.assertEqual(entries["alpha"].version, "1.2.0")
        self.assertEqual(entries["alpha"].source, "markdown")
        self.assertEqual(entries["alpha"].triggers, ["hello", "你好"])
        self.assertEqual(entries["alpha"].allowed_roles, ["coder"])
        self.assertTrue(entries["beta"].valid)
        self.assertEqual(entries["beta"].version, "2.0.0")
        self.assertFalse(entries["bad"].valid)
        self.assertIn("instructions", entries["bad"].error)
        # skill.0 reflects the real scan: one invalid manifest -> degraded.
        unit = {unit["unit_id"]: unit for unit in self.app.unit_cards()}["skill.0"]
        self.assertEqual(unit["state"], "degraded")
        self.assertIn("bad", unit["reason"])
        self.assertEqual(unit["provides"]["skills"], 2)
        self.assertEqual(unit["provides"]["invalid"], 1)
        self.assertEqual(unit["runtime"]["mode"], "in_process")
        self.assertIsNone(unit["pid"])

    async def test_api_lists_skills_and_skips_invalid_enable(self):
        status, payload = await self.api_call("GET", "/api/skills")
        self.assertEqual(status, 200)
        data = payload["data"]
        by_id = {entry["id"]: entry for entry in data["skills"]}
        self.assertEqual(by_id["alpha"]["status"], "enabled")
        self.assertEqual(by_id["beta"]["status"], "disabled")
        self.assertEqual(by_id["bad"]["status"], "invalid")
        self.assertEqual(data["summary"]["invalid"], 1)
        status, payload = await self.api_call(
            "POST", "/api/skills/bad/enable", {}, {})
        self.assertEqual(status, 409)
        status, payload = await self.api_call(
            "POST", "/api/skills/nope/enable", {}, {})
        self.assertEqual(status, 404)

    async def test_enable_disable_global_and_catalog_visibility(self):
        status, payload = await self.api_call(
            "POST", "/api/skills/beta/enable", {}, {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["skill"]["status"], "enabled")
        catalog_names = {entry["name"] for entry in
                         self.app.capability_catalog.entries(kind="skill")}
        self.assertIn("beta", catalog_names)
        self.assertIn("alpha", catalog_names)
        self.assertNotIn("bad", catalog_names)
        status, payload = await self.api_call(
            "POST", "/api/skills/alpha/disable", {}, {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["skill"]["status"], "disabled")
        catalog_names = {entry["name"] for entry in
                         self.app.capability_catalog.entries(kind="skill")}
        self.assertNotIn("alpha", catalog_names)

    async def test_project_enablement_and_no_cross_project_leak(self):
        project = self.app.projects.create("Skill Project")
        other = self.app.projects.create("Other Project")
        status, payload = await self.api_call(
            "POST", "/api/skills/beta/enable", {}, {"project_id": project.id})
        self.assertEqual(status, 200)
        entries = {entry.id: entry for entry in self.skills.all_entries()}
        self.assertTrue(entries["beta"].enabled_for(project.id))
        self.assertFalse(entries["beta"].enabled_for(other.id))
        scoped = {entry["name"] for entry in
                  self.app.capability_catalog.entries(kind="skill",
                                                      project_id=project.id)}
        self.assertIn("beta", scoped)
        other_entries = {entry["name"] for entry in
                         self.app.capability_catalog.entries(
                             kind="skill", project_id=other.id)}
        self.assertNotIn("beta", other_entries)
        status, payload = await self.api_call(
            "GET", "/api/skills", {"project_id": project.id})
        by_id = {entry["id"]: entry for entry in payload["data"]["skills"]}
        self.assertEqual(by_id["beta"]["status"], "enabled")
        # Project-level disable overrides the global enable.
        status, payload = await self.api_call(
            "POST", "/api/skills/alpha/disable", {}, {"project_id": project.id})
        self.assertEqual(status, 200)
        self.assertFalse(self.skills.is_enabled("alpha", project.id))
        self.assertTrue(self.skills.is_enabled("alpha", None))

    async def test_injection_filters_role_project_and_triggers(self):
        project = self.app.projects.create("Injection Project")
        self.skills.set_enabled("beta", True, project.id)
        text = self.skills.render_injection(
            project_id=project.id, role="coder", prompt="hello there")
        self.assertIn("Alpha skill", text)
        self.assertIn("Use this when the user greets you", text)
        self.assertNotIn("Beta skill", text)  # trigger "beta" not in prompt
        text = self.skills.render_injection(
            project_id=project.id, role="coder", prompt="beta hello")
        self.assertIn("Alpha skill", text)
        self.assertIn("Beta skill", text)
        # Role restriction: alpha is coder-only.
        tester_text = self.skills.render_injection(
            project_id=project.id, role="tester", prompt="beta hello")
        self.assertNotIn("Alpha skill", tester_text)
        self.assertIn("Beta skill", tester_text)
        # Global view does not include project-only enablement.
        global_text = self.skills.render_injection(
            role="coder", prompt="hello")
        self.assertIn("Alpha skill", global_text)
        self.assertNotIn("Beta skill", global_text)

    async def test_injection_truncates_at_char_limit(self):
        self.skills.set_enabled("beta", True)
        text = self.skills.render_injection(
            project_id=None, role="coder", prompt="beta hello",
            max_chars=100)
        self.assertLessEqual(len(text), 101)
        self.assertIn("skill truncated", text)

    async def test_events_and_state_file(self):
        records = [record.type for record in
                   self.app.eventlog.query(types=["skill"], limit=200)]
        self.assertIn("skill.scanned", records)
        self.assertIn("skill.invalid", records)
        await self.api_call("POST", "/api/skills/beta/enable", {}, {})
        records = [record.type for record in
                   self.app.eventlog.query(types=["skill"], limit=200)]
        self.assertIn("skill.enabled", records)
        await self.api_call("POST", "/api/skills/beta/disable", {}, {})
        records = [record.type for record in
                   self.app.eventlog.query(types=["skill"], limit=200)]
        self.assertIn("skill.disabled", records)
        state_path = self.tmp / "state" / "state" / "skills.json"
        self.assertTrue(state_path.exists())
        state = json.loads(state_path.read_text("utf-8"))
        self.assertIn("beta", state["global"]["disabled"])

    async def test_restart_and_disable_unit_actions(self):
        status, payload = await self.api_call(
            "POST", "/api/units/skill.0/actions", {}, {"action": "restart"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["restarted"])
        status, payload = await self.api_call(
            "POST", "/api/units/skill.0/actions", {}, {"action": "drain"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["unit"]["state"], "draining")
        status, payload = await self.api_call(
            "POST", "/api/units/skill.0/actions", {}, {"action": "undrain"})
        self.assertEqual(status, 200)
        before = self.app.unit_control.summary()["online"]
        status, payload = await self.api_call(
            "POST", "/api/units/skill.0/actions", {}, {"action": "disable"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["unit"]["state"], "disabled")
        summary = self.app.unit_control.summary()
        self.assertEqual(summary["online"], before - 1)
        skill = {unit["unit_id"]: unit for unit in self.app.unit_cards()}["skill.0"]
        self.assertEqual(skill["state"], "disabled")
        status, payload = await self.api_call(
            "POST", "/api/units/skill.0/actions", {}, {"action": "enable"})
        self.assertEqual(status, 200)
        self.assertIn(payload["data"]["unit"]["state"],
                      ("ready", "degraded"))


class EmptySkillRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_skills_is_ready_and_capability_absent(self):
        tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cfg = Config.load(root=root, state_dir=root / "state")
        cfg = cfg.update({"skills": {"dirs": ["missing"], "enabled": []},
                          "plugins": {"dirs": [], "enabled": []},
                          "mcp": {"servers": []}})
        app = App(cfg)
        await app.start()
        try:
            unit = {unit["unit_id"]: unit for unit in app.unit_cards()}["skill.0"]
            self.assertEqual(unit["state"], "ready")
            self.assertEqual(unit["provides"]["skills"], 0)
            self.assertEqual(app.skills.all_entries(), [])
            self.assertEqual(app.capability_catalog.entries(kind="skill"), [])
        finally:
            await app.stop()


if __name__ == "__main__":
    unittest.main()

"""The hub itself is sys.0; unit lists must never look empty while it runs."""

from __future__ import annotations

import asyncio
import tempfile
import unittest

from stackflowos.app import App
from stackflowos.config import Config, default_root
from stackflowos.errors import BadRequest

REPO = default_root()


class UnitVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        from pathlib import Path
        self.tmp = Path(self._tmp.name)
        self.app = App(Config.load(root=self.tmp, state_dir=self.tmp / "state"))
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    async def test_snapshot_includes_sys_unit(self):
        units = self.app.snapshot()["units"]
        sys_unit = next((unit for unit in units if unit["work_id"] == "sys.0"), None)
        self.assertIsNotNone(sys_unit)
        self.assertEqual(sys_unit["kind"], "sys")
        self.assertEqual(sys_unit["status"], "online")
        self.assertTrue(sys_unit["rpc_address"])
        self.assertTrue(sys_unit["events_address"])
        stats = self.app.stats()["units"]
        self.assertGreaterEqual(stats["total"], 10)
        self.assertEqual(stats["online"], len([
            unit for unit in units
            if (unit.get("state") or unit.get("status")) in
            ("ready", "degraded", "online", "suspect", "draining")]))
        self.assertEqual(stats["by_kind"].get("sys"), 1)
        self.assertEqual(stats["by_tier"].get("system") >= 5, True)
        self.assertEqual(self.app.health()["transport"], "ok")

    async def test_logical_service_units_are_visible(self):
        units = {unit["unit_id"]: unit for unit in self.app.unit_cards()}
        for unit_id in ("approval.0", "llm.0", "tool.0", "context.0",
                        "plugin.0", "skill.0", "mcp.0"):
            self.assertIn(unit_id, units)
            self.assertEqual(units[unit_id]["runtime"]["mode"], "in_process")
            self.assertIsNone(units[unit_id]["pid"])
        self.assertEqual(units["approval.0"]["critical"], True)
        self.assertEqual(units["skill.0"]["state"], "disabled")

    async def test_unit_cards_include_workers_after_sys(self):
        card = await self.app.attach_project("单元测试", str(self.tmp),
                                             allow_writes=False)
        units = self.app.unit_cards()
        self.assertEqual(units[0]["work_id"], "sys.0")
        self.assertIn("p-", card["id"])

    async def test_sys_unit_cannot_be_killed(self):
        with self.assertRaises(BadRequest):
            await self.app.kill_unit("sys.0")

    async def test_in_process_service_unit_cannot_be_killed(self):
        with self.assertRaises(BadRequest):
            await self.app.kill_unit("approval.0")


if __name__ == "__main__":
    unittest.main()

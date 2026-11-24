"""Reviewer write permission (security-fix-plan §14) and capability.call tool."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from stackflowos.agents.models import Agent
from stackflowos.agents.tools import ToolExecutor, tools_for
from stackflowos.config import SecurityCfg
from stackflowos.errors import Denied, NotFound
from tests.test_agent_tools import FakeSysAPI

TMP_ROOT = Path(__file__).resolve().parent.parent / ".stackflowos" / "tmp"


class ReviewerToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        self.ws = self.tmp / "workspace"
        self.ws.mkdir()
        self.sys = FakeSysAPI()
        self.agent = Agent(id="agent-reviewer-1", role="reviewer",
                           project_id="p-1", task_id="t-1")
        self.executor = ToolExecutor(
            self.sys, agent=self.agent, workspace=self.ws,
            security_config=SecurityCfg())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reviewer_tool_table_has_fs_write(self) -> None:
        names = {spec.name for spec in tools_for("reviewer")}
        self.assertIn("fs.write", names)
        self.assertIn("capability.call", names)
        # Read-only aggregator keeps its narrow table.
        aggregator = {spec.name for spec in tools_for("aggregator")}
        self.assertNotIn("fs.write", aggregator)
        self.assertNotIn("capability.call", aggregator)

    async def test_reviewer_can_write_review_file_inside_workspace(self) -> None:
        result = await self.executor.run(
            "fs.write", {"path": "review/t-1-review.md",
                         "content": "# review\n通过\n"})
        self.assertTrue(result.ok, result.error)
        written = self.ws / "review" / "t-1-review.md"
        self.assertTrue(written.is_file())
        self.assertIn("通过", written.read_text("utf-8"))
        # The path policy still confines the new write capability.
        escape = await self.executor.run(
            "fs.write", {"path": "../outside.md", "content": "x"})
        self.assertFalse(escape.ok)
        self.assertIn("escapes workspace", escape.error or "")
        self.assertFalse((self.tmp / "outside.md").exists())

    async def test_capability_call_forwards_scope_to_the_hub(self) -> None:
        self.sys.responses["capability.call"] = {
            "content": "sunny", "provider": "mcp"}
        result = await self.executor.run(
            "capability.call",
            {"name": "mcp.weather", "args": {"city": "Hangzhou"}})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["content"], "sunny")
        self.assertEqual(result.data["name"], "mcp.weather")
        call = self.sys.actions("capability.call")[0]
        self.assertEqual(call["project_id"], "p-1")
        self.assertEqual(call["task_id"], "t-1")
        self.assertEqual(call["name"], "mcp.weather")
        self.assertEqual(call["args"], {"city": "Hangzhou"})

    async def test_capability_call_maps_denied_and_missing_bridge(self) -> None:
        self.sys.errors["capability.call"] = Denied("not allowed")
        result = await self.executor.run(
            "capability.call", {"name": "mcp.dangerous"})
        self.assertFalse(result.ok)
        self.assertTrue((result.error or "").startswith("DENIED"),
                        result.error)

        self.sys.errors["capability.call"] = NotFound("unknown action")
        result = await self.executor.run(
            "capability.call", {"name": "mcp.weather"})
        self.assertFalse(result.ok)
        self.assertIn("unavailable", result.error or "")

        self.sys.errors["capability.call"] = RuntimeError("transport down")
        result = await self.executor.run(
            "capability.call", {"name": "mcp.weather"})
        self.assertFalse(result.ok)
        self.assertIn("unavailable", result.error or "")

    async def test_capability_call_rejects_missing_name(self) -> None:
        result = await self.executor.run("capability.call", {})
        self.assertFalse(result.ok)
        self.assertIn("name", result.error or "")
        self.assertEqual(self.sys.actions("capability.call"), [])

    async def test_hub_pending_approval_maps_to_denied(self) -> None:
        # The hub's own approval gate returns executed=false instead of raising;
        # the worker must not report that as a successful tool call.
        self.sys.responses["capability.call"] = {
            "tool": "mcp.weather", "provider": "mcp", "status": "pending",
            "executed": False, "approval_id": "apr-9",
            "reason": "waiting for operator"}
        result = await self.executor.run(
            "capability.call", {"name": "mcp.weather"})
        self.assertFalse(result.ok)
        self.assertTrue((result.error or "").startswith("DENIED"),
                        result.error)
        self.assertIn("waiting for operator", result.error or "")

    async def test_capability_call_requires_approval_like_other_tools(self) -> None:
        self.sys.approval = {"id": "apr-1", "status": "denied", "mode": "rule",
                             "risk": "medium", "grant_id": None,
                             "reason": "nope"}
        result = await self.executor.run(
            "capability.call", {"name": "mcp.weather"})
        self.assertFalse(result.ok)
        self.assertTrue((result.error or "").startswith("DENIED"))
        self.assertEqual(self.sys.actions("capability.call"), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

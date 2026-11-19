"""Project lifecycle administration: archive, permanent delete, sweeper."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from stackflowos.app import App
from stackflowos.config import Config, default_root
from stackflowos.errors import BadRequest, NotFound
from stackflowos.web.server import WebServer

REPO = default_root()


class ProjectAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.app = App(Config.load(root=self.tmp, state_dir=self.tmp))
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    async def _project(self, name: str = "待清理项目") -> dict:
        card = await self.app.create_project(name, "删除/归档测试", spawn_lead=False)
        task = self.app.projects.add_task(card["id"], "写点东西", role="coder")
        workspace = Path(card["workspace"])
        (workspace / "artifact.txt").write_text("hello", "utf-8")
        return {"card": card, "task": task, "workspace": workspace}

    def _pending(self, project_id: str, task_id: str | None = None):
        return self.app.approvals.request(
            "agent-coder-1", "fs.write", {"path": "/etc/outside"},
            reason="test", preview="outside", project_id=project_id,
            task_id=task_id, ttl_s=300)

    async def test_delete_removes_record_tasks_workspace_and_pending(self):
        fixture = await self._project()
        pid = fixture["card"]["id"]
        pending = self._pending(pid, fixture["task"].id)
        lease = self.app.leases.acquire(f"project:{pid}:lead", "lead.probe", 60)
        result = await self.app.delete_project(pid, confirm=fixture["card"]["name"])
        self.assertEqual(result["deleted"], pid)
        self.assertTrue(result["workspace_purged"])
        self.assertFalse(fixture["workspace"].parent.exists())
        with self.assertRaises(NotFound):
            self.app.projects.get(pid)
        self.assertEqual(self.app.projects.task_list(project_id=pid), [])
        self.assertEqual(self.app.approvals.get(pending.id).status, "denied")
        self.assertEqual([l for l in self.app.leases.list(active_only=True)
                          if l.id == lease.id], [])
        types = [r.type for r in self.app.eventlog.tail(40)]
        self.assertIn("project.deleted", types)
        self.assertIn("project.delete.completed", types)

    async def test_delete_requires_matching_confirmation(self):
        fixture = await self._project()
        pid = fixture["card"]["id"]
        with self.assertRaises(BadRequest):
            await self.app.delete_project(pid, confirm="错误的名字")
        self.assertEqual(self.app.projects.get(pid).id, pid)
        self.assertTrue(fixture["workspace"].exists())

    async def test_archive_keeps_workspace_but_stops_pending(self):
        fixture = await self._project()
        pid = fixture["card"]["id"]
        pending = self._pending(pid)
        result = await self.app.archive_project(pid)
        self.assertEqual(result["project"]["status"], "archived")
        self.assertTrue(fixture["workspace"].exists())
        self.assertEqual(self.app.approvals.get(pending.id).status, "denied")
        self.assertEqual(self.app.projects.get(pid).status, "archived")

    async def test_maintenance_sweeper_expires_approvals(self):
        fixture = await self._project()
        pending = self.app.approvals.request(
            "agent-coder-1", "fs.write", {"path": "/etc/outside"},
            reason="expire me", project_id=fixture["card"]["id"], ttl_s=1)
        self.assertEqual(pending.status, "pending")
        await asyncio.sleep(2.4)
        self.assertEqual(self.app.approvals.get(pending.id).status, "expired")

    async def test_assistant_tool_deletes_after_approval_rule(self):
        self.app.policy.add_rule(effect="allow", capability="project.delete",
                                 principal="agent-assistant", scope={},
                                 risk_max="high", note="test auto-allow")
        fixture = await self._project("助手删除测试")
        pid = fixture["card"]["id"]
        result = await self.app.assistant_tool("project.delete", {
            "project_id": pid, "purge_workspace": True,
        })
        self.assertTrue(result["ok"], result)
        with self.assertRaises(NotFound):
            self.app.projects.get(pid)

    async def test_delete_http_endpoint(self):
        server = WebServer(self.app, host="127.0.0.1", port=0)
        await server.start()
        try:
            fixture = await self._project("HTTP 删除测试")
            pid = fixture["card"]["id"]

            def call():
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.port}/api/projects/{pid}",
                    method="DELETE",
                    data=json.dumps({"confirm": fixture["card"]["name"],
                                     "purge_workspace": True}).encode())
                request.add_header("Content-Type", "application/json")
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        return response.status, json.loads(response.read().decode())
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read().decode())

            status, payload = await asyncio.to_thread(call)
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["deleted"], pid)
        finally:
            await server.stop()


if __name__ == "__main__":
    unittest.main()

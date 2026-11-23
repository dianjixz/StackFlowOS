"""Attaching an existing directory as a project."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from stackflowos.agents.tools import ToolExecutor
from stackflowos.app import App
from stackflowos.config import Config, SecurityCfg, default_root
from stackflowos.errors import BadRequest, ValidationError
from stackflowos.web.server import WebServer

REPO = default_root()


class FakeSys:
    def __init__(self) -> None:
        self.calls = []

    async def call(self, action, data=None, *, timeout=None):
        self.calls.append((action, data or {}))
        if action == "approval.request":
            return {"id": "apr-x", "status": "approved", "mode": "auto",
                    "grant_id": None}
        if action == "approval.use":
            return {"ok": True}
        if action == "event.append":
            return {"seq": 1}
        return {}


class AttachProjectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.app = App(Config.load(root=self.tmp, state_dir=self.state))
        await self.app.start()
        self.external = self.tmp / "existing-project"
        self.external.mkdir()
        (self.external / "README.md").write_text("# existing", "utf-8")
        (self.external / "src").mkdir()
        (self.external / "src" / "main.py").write_text("print(1)\n", "utf-8")

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    async def test_attach_read_only_and_read_files(self):
        card = await self.app.attach_project("已有项目", str(self.external))
        self.assertTrue(card["attached_now"])
        self.assertTrue(card["external"])
        self.assertEqual(card["workspace_mode"], "attached-ro")
        self.assertEqual(Path(card["workspace"]), self.external.resolve())
        self.assertEqual(card["total_tasks"], 0)

        listing = await self.app.operator_tool(
            "project.files", {"project_id": card["id"], "path": "src"})
        self.assertTrue(listing["ok"], listing)
        self.assertEqual(listing["data"]["entries"][0]["name"], "main.py")
        content = await self.app.operator_tool(
            "project.read", {"project_id": card["id"], "path": "README.md"})
        self.assertIn("# existing", content["data"]["content"])

    async def test_attach_is_idempotent_for_same_directory(self):
        first = await self.app.attach_project("已有项目", str(self.external))
        second = await self.app.attach_project("另一个名字", str(self.external))
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["existing"])
        self.assertFalse(second["attached_now"])

    async def test_toggle_attached_mode(self):
        card = await self.app.attach_project("已有项目", str(self.external))
        self.assertEqual(card["workspace_mode"], "attached-ro")
        changed = await self.app.operator_tool(
            "project.access", {"project_id": card["id"], "allow_writes": True})
        self.assertTrue(changed["ok"], changed)
        self.assertEqual(self.app.projects.get(card["id"]).workspace_mode,
                         "attached-rw")
        back = await self.app.set_project_access(card["id"], allow_writes=False)
        self.assertEqual(back["workspace_mode"], "attached-ro")

    async def test_delete_external_project_never_removes_directory(self):
        card = await self.app.attach_project("已有项目", str(self.external))
        result = await self.app.delete_project(card["id"], confirm=card["name"])
        self.assertFalse(result["workspace_purged"])
        self.assertTrue(result["external_workspace_preserved"])
        self.assertTrue((self.external / "README.md").exists())
        self.assertEqual(self.external, self.external.resolve())

    async def test_attach_rw_and_validation(self):
        rw = await self.app.attach_project("可写项目", str(self.external),
                                           allow_writes=True)
        self.assertEqual(rw["workspace_mode"], "attached-rw")
        with self.assertRaises(ValidationError):
            await self.app.attach_project("bad", "/etc")
        with self.assertRaises(ValidationError):
            await self.app.attach_project("bad", str(self.state))
        with self.assertRaises(ValidationError):
            await self.app.attach_project("bad", str(self.tmp / "missing"))
        with self.assertRaises(ValidationError):
            await self.app.attach_project("bad", str(self.external / "README.md"))

    async def test_read_only_executor_blocks_write_before_approval(self):
        sys = FakeSys()
        agent = SimpleNamespace(id="agent-coder-1", project_id="p-1")
        executor = ToolExecutor(sys, agent=agent, workspace=self.external,
                                security_config=SecurityCfg(), read_only=True)
        result = await executor.run("fs.write", {"path": "x.txt", "content": "x"})
        self.assertFalse(result.ok)
        self.assertIn("read-only", result.error)
        self.assertEqual(sys.calls, [])
        # Reading is still allowed (and goes through the normal approval flow).
        read = await executor.run("fs.read", {"path": "README.md"})
        self.assertTrue(read.ok, read.error)
        self.assertIn("# existing", read.data["content"])

    async def test_attached_workspace_cannot_read_state_dir(self):
        (self.state / "config.json").write_text('{"secret": true}', "utf-8")
        card = await self.app.attach_project("根目录接入", str(self.tmp))
        blocked = await self.app.operator_tool(
            "project.read", {"project_id": card["id"], "path": "state/config.json"})
        self.assertFalse(blocked["ok"])
        self.assertIn("not readable", blocked["error"])
        listing = await self.app.operator_tool(
            "project.files", {"project_id": card["id"], "path": "."})
        names = [entry["name"] for entry in listing["data"]["entries"]]
        self.assertNotIn("state", names)

    async def test_executor_deny_paths_blocks_state(self):
        (self.state / "config.json").write_text("{}", "utf-8")
        sys = FakeSys()
        agent = SimpleNamespace(id="agent-coder-1", project_id="p-1")
        executor = ToolExecutor(sys, agent=agent, workspace=self.tmp,
                                security_config=SecurityCfg(),
                                deny_paths=[self.state])
        result = await executor.run("fs.read", {"path": "state/config.json"})
        self.assertFalse(result.ok)
        self.assertIn("not readable", result.error)

    async def test_chat_attach_intent(self):
        await self.app.chat.send_user(f"打开 {self.external} 这个项目")
        deadline = asyncio.get_running_loop().time() + 10
        project = None
        while asyncio.get_running_loop().time() < deadline:
            for candidate in self.app.projects.list():
                if Path(candidate.workspace) == self.external.resolve():
                    project = candidate
                    break
            if project is not None:
                break
            await asyncio.sleep(0.1)
        self.assertIsNotNone(project, "chat did not attach the directory")
        self.assertEqual(project.workspace_mode, "attached-ro")
        history = self.app.chat.history(5)
        self.assertIn("已接入目录", history[-1]["text"])

    async def test_file_explanations_are_human_readable(self):
        text = self.app.chat._explain_tool_result("project.files", {
            "path": ".",
            "entries": [{"name": "README.md", "type": "file"},
                        {"name": "src", "type": "dir"}],
        })
        self.assertIn("README.md", text)
        self.assertIn("src/", text)
        read = self.app.chat._explain_tool_result("project.read", {
            "path": "README.md", "content": "# existing", "truncated": False})
        self.assertIn("# existing", read)

    async def test_chat_toggles_attached_access(self):
        card = await self.app.attach_project("模式项目", str(self.external))
        await self.app.chat.send_user("把 模式项目 改成可写")
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            if self.app.projects.get(card["id"]).workspace_mode == "attached-rw":
                break
            await asyncio.sleep(0.1)
        self.assertEqual(self.app.projects.get(card["id"]).workspace_mode,
                         "attached-rw")
        await self.app.chat.send_user("把 模式项目 改回只读")
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            if self.app.projects.get(card["id"]).workspace_mode == "attached-ro":
                break
            await asyncio.sleep(0.1)
        self.assertEqual(self.app.projects.get(card["id"]).workspace_mode,
                         "attached-ro")

    async def test_http_attach_endpoint(self):
        server = WebServer(self.app, host="127.0.0.1", port=0)
        await server.start()
        try:
            def call():
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.port}/api/projects/attach",
                    method="POST",
                    data=json.dumps({"name": "HTTP 接入",
                                     "path": str(self.external)}).encode())
                request.add_header("Content-Type", "application/json")
                request.add_header("X-SFO-Request", "1")
                request.add_header("Authorization", f"Bearer {server.launch_token}")
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read().decode())

            status, payload = await asyncio.to_thread(call)
            self.assertEqual(status, 201, payload)
            self.assertTrue(payload["data"]["external"])
        finally:
            await server.stop()


if __name__ == "__main__":
    unittest.main()

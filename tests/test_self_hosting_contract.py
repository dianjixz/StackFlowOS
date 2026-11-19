"""Contract tests for attaching an existing local directory (task t-15).

The project is the workspace: the system registers the path but must never
copy, move or delete it. Attached directories are read-only by default.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from stackflowos.agents.tools import ToolExecutor
from stackflowos.app import App
from stackflowos.config import Config, SecurityCfg, default_root
from stackflowos.errors import NotFound

REPO = default_root()


class FakeSys:
    """Minimal sys bus; records whether an approval was requested."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, action, data=None, *, timeout=None):
        self.calls.append((action, dict(data or {})))
        if action == "approval.request":
            return {"id": "apr-x", "status": "approved", "mode": "auto",
                    "grant_id": None}
        if action == "approval.use":
            return {"ok": True}
        if action == "event.append":
            return {"seq": 1}
        return {}


class SelfHostingContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.app = App(Config.load(root=self.tmp, state_dir=self.tmp / "state"))
        await self.app.start()
        self.external = self.tmp / "repository"
        self.external.mkdir()
        (self.external / "README.md").write_text("# repo\n", "utf-8")
        (self.external / "src").mkdir()
        (self.external / "src" / "main.py").write_text("print(1)\n", "utf-8")

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    async def test_attach_existing_directory_is_read_only_and_not_copied(self):
        card = await self.app.attach_project("自举仓库", str(self.external))
        self.assertTrue(card["external"])
        self.assertEqual(card["workspace_mode"], "attached-ro")
        self.assertEqual(Path(card["workspace"]), self.external.resolve())
        self.assertEqual((self.external / "README.md").read_text("utf-8"),
                         "# repo\n")

    async def test_attach_same_directory_returns_existing_project(self):
        first = await self.app.attach_project("自举仓库", str(self.external))
        second = await self.app.attach_project("另一个名字", str(self.external))
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["existing"])
        self.assertFalse(second["attached_now"])

    async def test_delete_attached_project_preserves_directory(self):
        card = await self.app.attach_project("自举仓库", str(self.external))
        result = await self.app.delete_project(
            card["id"], confirm=card["name"])
        self.assertFalse(result["workspace_purged"])
        self.assertTrue(result["external_workspace_preserved"])
        self.assertTrue((self.external / "README.md").exists())
        self.assertTrue((self.external / "src" / "main.py").exists())
        with self.assertRaises(NotFound):
            self.app.projects.get(card["id"])

    async def test_read_only_executor_rejects_writes_before_approval(self):
        sys = FakeSys()
        agent = SimpleNamespace(id="agent-coder-1", project_id="p-1")
        executor = ToolExecutor(sys, agent=agent, workspace=self.external,
                                security_config=SecurityCfg(), read_only=True)
        for tool, args in (
            ("fs.write", {"path": "README.md", "content": "changed"}),
            ("fs.delete", {"path": "README.md"}),
            ("shell.exec", {"command": "echo hi"}),
        ):
            with self.subTest(tool=tool):
                result = await executor.run(tool, args)
                self.assertFalse(result.ok)
                self.assertIn("read-only", result.error)
        self.assertEqual(sys.calls, [])
        self.assertEqual((self.external / "README.md").read_text("utf-8"),
                         "# repo\n")

    async def test_access_toggle_switches_attached_mode(self):
        card = await self.app.attach_project("自举仓库", str(self.external))
        await self.app.set_project_access(card["id"], allow_writes=True)
        self.assertEqual(self.app.projects.get(card["id"]).workspace_mode,
                         "attached-rw")
        await self.app.set_project_access(card["id"], allow_writes=False)
        self.assertEqual(self.app.projects.get(card["id"]).workspace_mode,
                         "attached-ro")


if __name__ == "__main__":
    unittest.main()

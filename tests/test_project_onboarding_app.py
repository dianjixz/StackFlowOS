"""App-level onboarding: scaffold, seed task, memory seed and healthcheck."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stackflowos.app import App
from stackflowos.config import Config
from stackflowos.errors import BadRequest

TMP_ROOT = Path(__file__).resolve().parents[1] / ".stackflowos" / "tmp"


class ProjectOnboardingAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(TMP_ROOT))
        self.root = Path(self._tmp.name).resolve()
        self.app = App(Config.load(root=self.root,
                                   state_dir=self.root / "state"))
        await self.app.start()

    async def asyncTearDown(self) -> None:
        await self.app.stop()
        self._tmp.cleanup()

    async def test_create_project_seeds_brief_layout_task_and_memory(self) -> None:
        card = await self.app.create_project("Demo", "ship a tiny tool",
                                             spawn_lead=False)
        project_id = card["id"]
        workspace = Path(card["workspace"])
        self.assertTrue((workspace / "brief.md").is_file())
        for name in ("tasks", "artifacts", "logs", "tmp"):
            self.assertTrue((workspace / name).is_dir(), name)
        brief = (workspace / "brief.md").read_text("utf-8")
        self.assertIn("ship a tiny tool", brief)
        self.assertIn(project_id, brief)

        tasks = self.app.projects.task_list(project_id=project_id)
        self.assertTrue(any(task.title.startswith("入职自检") for task in tasks))

        memory = self.app.memory.all(project_id)
        for key in ("goal", "workspace", "layout", "conventions", "glossary"):
            self.assertIn(key, memory)
        self.assertEqual(memory["goal"], "ship a tiny tool")

        report = self.app.project_health(project_id)
        self.assertTrue(report["ready"], report["not_ready_reasons"])
        event_types = [record.type for record in
                       self.app.eventlog.query(project_id=project_id,
                                               types=["project.onboarded"])]
        self.assertTrue(event_types)

    async def test_attach_does_not_mutate_the_external_tree(self) -> None:
        external = self.root / "existing"
        external.mkdir()
        (external / "README.md").write_text("# existing\n", "utf-8")

        card = await self.app.attach_project("Existing", str(external))
        project_id = card["id"]
        self.assertFalse((external / "brief.md").exists())
        self.assertFalse((external / "tasks").exists())
        # State-only onboarding still gives the agent a task and memory.
        self.assertTrue(self.app.projects.task_list(project_id=project_id))
        self.assertIn("goal", self.app.memory.all(project_id))
        report = self.app.project_health(project_id)
        layout = next(check for check in report["checks"]
                      if check["id"] == "workspace.layout")
        self.assertEqual(layout["status"], "warn")
        self.assertTrue(report["ready"], report["not_ready_reasons"])

    async def test_onboard_writes_attached_rw_workspace(self) -> None:
        external = self.root / "writable"
        external.mkdir()
        card = await self.app.attach_project("Writable", str(external),
                                             allow_writes=True)
        project_id = card["id"]
        self.assertFalse((external / "brief.md").exists())
        result = await self.app.onboard_project(project_id)
        self.assertEqual(result["scaffold"]["brief"], "created")
        for name in ("tasks", "artifacts", "logs", "tmp"):
            self.assertIn(name, result["scaffold"]["created"])
        self.assertTrue((external / "brief.md").is_file())
        self.assertTrue(result["health"]["ready"],
                        result["health"]["not_ready_reasons"])

    async def test_spawn_requires_a_project_scope(self) -> None:
        with self.assertRaises(BadRequest) as caught:
            await self.app.spawn_agent("coder", None)
        self.assertIn("project", caught.exception.message)

    async def test_spawn_rejects_missing_workspace(self) -> None:
        card = await self.app.create_project("Broken", "x", spawn_lead=False)
        project = self.app.projects.get(card["id"])
        # Simulate a workspace that disappeared after creation.
        project.workspace = str(self.root / "gone")
        self.app.projects.save()
        with self.assertRaises(Exception) as caught:
            await self.app.spawn_agent("coder", card["id"])
        self.assertIn("workspace", str(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

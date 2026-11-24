"""The project onboarding kit: nameplate, fixed layout, seed task, memory."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from stackflowos.projects.models import Project, WorkspaceMode
from stackflowos.projects.onboarding import (
    BRIEF_FILENAME,
    MEMORY_KEYS,
    SCAFFOLD_DIRS,
    SEED_TASK_TITLE,
    brief_markdown,
    ensure_scaffold,
    memory_seed,
    seed_task_present,
    seed_task_spec,
)

TMP_ROOT = Path(__file__).resolve().parents[1] / ".stackflowos" / "tmp"


def _project(workspace: Path, *, mode: str = WorkspaceMode.MANAGED,
             goal: str = "ship a tiny tool") -> Project:
    return Project(
        id="p-1", name="Demo", goal=goal, status="active",
        created_by="tester", workspace=str(workspace),
        workspace_mode=mode, external=mode != WorkspaceMode.MANAGED,
    )


class OnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="onboarding-", dir=TMP_ROOT))
        self.ws = self.tmp / "workspace"
        self.ws.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_brief_is_a_nameplate_with_goal_and_boundaries(self) -> None:
        project = _project(self.ws)
        text = brief_markdown(project)
        self.assertIn("p-1", text)
        self.assertIn("Demo", text)
        self.assertIn("ship a tiny tool", text)
        self.assertIn(str(self.ws), text)
        self.assertIn(BRIEF_FILENAME, text)
        for name, purpose in SCAFFOLD_DIRS:
            self.assertIn(f"`{name}/`", text)
            self.assertIn(purpose, text)
        self.assertIn("project.health", text)
        self.assertIn("sfo project health p-1", text)
        for key in MEMORY_KEYS:
            self.assertIn(f"`{key}`", text)

    def test_empty_goal_is_called_out_as_a_blocker(self) -> None:
        text = brief_markdown(_project(self.ws, goal=""))
        self.assertIn("未设置", text)

    def test_ensure_scaffold_creates_layout_and_brief(self) -> None:
        result = ensure_scaffold(_project(self.ws))
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["brief"], "created")
        for name, _purpose in SCAFFOLD_DIRS:
            self.assertTrue((self.ws / name).is_dir(), name)
        brief = (self.ws / BRIEF_FILENAME).read_text("utf-8")
        self.assertIn("ship a tiny tool", brief)
        self.assertEqual(result["refused"], None)

    def test_ensure_scaffold_is_idempotent_and_keeps_existing_brief(self) -> None:
        ensure_scaffold(_project(self.ws))
        (self.ws / BRIEF_FILENAME).write_text("# hand edited\n", "utf-8")
        result = ensure_scaffold(_project(self.ws))
        self.assertEqual(result["brief"], "kept")
        self.assertEqual(result["created"], [])
        self.assertEqual(
            (self.ws / BRIEF_FILENAME).read_text("utf-8"), "# hand edited\n")

    def test_inspection_never_writes(self) -> None:
        result = ensure_scaffold(_project(self.ws), write=False)
        self.assertEqual(result["brief"], "missing")
        self.assertIn(BRIEF_FILENAME, result["missing"])
        self.assertIn("artifacts", result["missing"])
        self.assertFalse((self.ws / "artifacts").exists())
        self.assertFalse((self.ws / BRIEF_FILENAME).exists())

    def test_attached_read_only_refuses_to_mutate_the_tree(self) -> None:
        project = _project(self.ws, mode=WorkspaceMode.ATTACHED_RO)
        result = ensure_scaffold(project)
        self.assertIn("attached-ro", result["refused"] or "")
        self.assertIn("artifacts", result["missing"])
        self.assertEqual(sorted(path.name for path in self.ws.iterdir()), [])

    def test_missing_workspace_is_reported_not_created(self) -> None:
        project = _project(self.tmp / "missing")
        result = ensure_scaffold(project)
        self.assertTrue(result["warnings"])
        self.assertIn("artifacts", result["missing"])
        self.assertFalse((self.tmp / "missing").exists())

    def test_symlinked_layout_dir_is_not_followed(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.ws / "artifacts").symlink_to(outside, target_is_directory=True)
        result = ensure_scaffold(_project(self.ws))
        self.assertTrue(any("符号链接" in warning
                            for warning in result["warnings"]))
        self.assertIn("artifacts", result["missing"])

    def test_seed_task_closes_the_loop(self) -> None:
        spec = seed_task_spec(_project(self.ws))
        self.assertEqual(spec["title"], SEED_TASK_TITLE)
        self.assertEqual(spec["role"], "researcher")
        self.assertIn("project.health", spec["description"])
        self.assertIn("brief.md", spec["description"])
        self.assertIn("artifacts/onboarding-p-1.md", spec["description"])
        self.assertIn("finish(status=blocked", spec["description"])
        self.assertEqual(spec["created_by"], "sys:onboarding")

    def test_seed_task_present_matches_title_prefix(self) -> None:
        self.assertTrue(seed_task_present(
            [{"title": SEED_TASK_TITLE + "：p-1"}]))
        self.assertFalse(seed_task_present([{"title": "实现功能"}]))
        self.assertFalse(seed_task_present([]))

    def test_memory_seed_contains_goal_layout_and_conventions(self) -> None:
        seed = memory_seed(_project(self.ws))
        self.assertEqual(set(seed), set(MEMORY_KEYS))
        self.assertEqual(seed["goal"], "ship a tiny tool")
        self.assertEqual(seed["workspace"]["path"], str(self.ws))
        self.assertEqual(seed["workspace"]["mode"], "managed")
        self.assertEqual(set(seed["layout"]), {name for name, _ in SCAFFOLD_DIRS})
        self.assertIn("artifact_path", seed["conventions"])
        self.assertEqual(seed["glossary"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

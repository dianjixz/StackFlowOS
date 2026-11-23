"""Copy-on-write regression tests for ProjectStore mutations (S1).

A rejected patch must never mutate the in-memory domain, because the next
``save()`` (from any other operation) would otherwise persist the illegal
state.  These tests pin the contract for ``update`` and ``task_update``:
validate on a deep copy, then swap and persist.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from stackflowos.errors import ValidationError
from stackflowos.kernel import Context
from stackflowos.projects import ProjectStatus, ProjectStore, TaskStatus

REPO_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = REPO_ROOT / ".stackflowos" / "tmp"


class StoreCowTestCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="store-cow-", dir=TMP_ROOT))
        self.state_path = self.tmp / "state" / "projects.json"
        self.counter_path = self.tmp / "state" / "counters.json"
        self.ctx = Context(name="test")
        self.emitted: list[tuple[str, dict]] = []
        self.ctx.on_any(
            lambda mode, name, payload: self.emitted.append((name, payload)))
        self.store = self.make_store()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_store(self, ctx: Context | None = None) -> ProjectStore:
        return ProjectStore(
            self.ctx if ctx is None else ctx,
            state_path=self.state_path,
            counter_path=self.counter_path,
            workspace_root=self.tmp / "projects",
        )

    # -- helpers ----------------------------------------------------------
    def _state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _disk_project(self, project_id: str = "p-1") -> dict:
        for item in self._state()["projects"]:
            if item["id"] == project_id:
                return item
        self.fail(f"project {project_id} not on disk")

    def _disk_task(self, task_id: str) -> dict:
        for project in self._state()["projects"]:
            for task in project["tasks"]:
                if task["id"] == task_id:
                    return task
        self.fail(f"task {task_id} not on disk")

    def _event_types(self) -> list[str]:
        return [name for name, _payload in self.emitted]

    # -- ProjectStore.update ----------------------------------------------
    def test_project_update_invalid_status_keeps_memory_and_disk(self) -> None:
        self.store.create("Old", "goal", tags=["a"])
        before = self.store.get("p-1")
        before_disk = self.state_path.read_bytes()
        before_events = self._event_types()

        with self.assertRaises(ValidationError) as caught:
            self.store.update("p-1", {"name": "new", "status": "bogus"})
        self.assertEqual(caught.exception.detail["status"], "bogus")

        # Memory: the very same object, still holding the old values.
        current = self.store.get("p-1")
        self.assertIs(current, before)
        self.assertEqual(current.name, "Old")
        self.assertEqual(current.status, ProjectStatus.ACTIVE)
        # Disk: no write happened and no event was emitted.
        self.assertEqual(self.state_path.read_bytes(), before_disk)
        self.assertEqual(self._event_types(), before_events)

        # A later save from an unrelated operation must not persist "bogus".
        self.store.save()
        self.assertEqual(self._disk_project()["name"], "Old")
        self.assertEqual(self._disk_project()["status"],
                         ProjectStatus.ACTIVE)

    def test_project_update_archive_attempt_does_not_pollute(self) -> None:
        self.store.create("Old")
        before_disk = self.state_path.read_bytes()

        with self.assertRaises(ValidationError):
            self.store.update(
                "p-1", {"name": "new", "status": ProjectStatus.ARCHIVED})

        self.assertEqual(self.store.get("p-1").name, "Old")
        self.assertEqual(self._disk_project()["name"], "Old")
        self.assertEqual(self.state_path.read_bytes(), before_disk)

    def test_project_update_success_replaces_and_persists(self) -> None:
        self.store.create("Old", "goal", tags=["a"])
        old = self.store.get("p-1")

        updated = self.store.update("p-1", {
            "name": "New", "goal": "new goal", "status": ProjectStatus.REVIEW,
            "tags": ["x"], "color": "#445566", "brief": "halfway",
            "degraded": True,
        })

        # The candidate is swapped in; the previously handed out object is a
        # snapshot and is no longer what the store serves.
        self.assertIs(updated, self.store.get("p-1"))
        self.assertIsNot(updated, old)
        self.assertEqual(old.name, "Old")
        self.assertEqual(old.status, ProjectStatus.ACTIVE)
        self.assertEqual(updated.name, "New")
        self.assertEqual(updated.status, ProjectStatus.REVIEW)
        self.assertEqual(updated.tags, ["x"])
        self.assertEqual(updated.color, "#445566")
        self.assertEqual(updated.brief, "halfway")
        self.assertTrue(updated.degraded)
        self.assertIn("project.updated", self._event_types())

        disk = self._disk_project()
        self.assertEqual(disk["name"], "New")
        self.assertEqual(disk["status"], ProjectStatus.REVIEW)
        self.assertEqual(disk["tags"], ["x"])
        self.assertEqual(disk["brief"], "halfway")

        reloaded = self.make_store()
        self.assertEqual(reloaded.get("p-1").name, "New")

    # -- ProjectStore.task_update -----------------------------------------
    def test_task_update_invalid_status_keeps_memory_and_disk(self) -> None:
        self.store.create("Demo")
        task = self.store.add_task("p-1", "old title")
        project = self.store.get("p-1")
        project_updated_at = project.updated_at
        before_disk = self.state_path.read_bytes()
        before_events = self._event_types()

        with self.assertRaises(ValidationError):
            self.store.task_update(task.id, {"title": "new", "status": "bogus"})

        current = self.store.task_get(task.id)
        self.assertIs(current, task)
        self.assertEqual(current.title, "old title")
        self.assertEqual(current.status, TaskStatus.TODO)
        self.assertIsNone(current.assignee)
        self.assertEqual(self.store.get("p-1").updated_at, project_updated_at)
        self.assertEqual(self.state_path.read_bytes(), before_disk)
        self.assertEqual(self._event_types(), before_events)

        self.store.save()
        disk = self._disk_task(task.id)
        self.assertEqual(disk["title"], "old title")
        self.assertEqual(disk["status"], TaskStatus.TODO)

    def test_task_update_illegal_transition_keeps_title(self) -> None:
        self.store.create("Demo")
        task = self.store.add_task("p-1", "old title")

        # ``todo -> done`` is illegal; the title written to the candidate
        # before the transition check must not leak into the live task.
        with self.assertRaises(ValidationError):
            self.store.task_update(task.id, {"title": "new",
                                             "status": TaskStatus.DONE})

        current = self.store.task_get(task.id)
        self.assertEqual(current.title, "old title")
        self.assertEqual(current.status, TaskStatus.TODO)
        self.assertEqual(self._disk_task(task.id)["title"], "old title")

    def test_task_update_invalid_priority_does_not_apply_other_fields(self) -> None:
        self.store.create("Demo")
        task = self.store.add_task("p-1", "old title")
        before_disk = self.state_path.read_bytes()

        for bad_priority in (9, -1, "high", True):
            with self.subTest(priority=bad_priority), \
                    self.assertRaises(ValidationError):
                self.store.task_update(task.id, {
                    "title": "new", "role": "coder",
                    "priority": bad_priority,
                })
            current = self.store.task_get(task.id)
            self.assertEqual(current.title, "old title")
            self.assertEqual(current.priority, 0)
            self.assertIsNone(current.role)
            self.assertEqual(self.state_path.read_bytes(), before_disk)

    def test_task_update_success_replaces_and_persists(self) -> None:
        self.store.create("Demo")
        old = self.store.add_task("p-1", "old title")

        updated = self.store.task_update(old.id, {
            "title": "new title", "description": "desc", "priority": 2,
            "role": "coder", "assignee": "agent.coder.1", "error": "boom",
        })
        self.assertIs(updated, self.store.task_get(old.id))
        self.assertIsNot(updated, old)
        self.assertEqual(old.title, "old title")
        self.assertEqual(updated.title, "new title")
        self.assertEqual(updated.priority, 2)
        self.assertEqual(updated.role, "coder")
        self.assertEqual(updated.assignee, "agent.coder.1")
        self.assertEqual(updated.error, "boom")

        disk = self._disk_task(old.id)
        self.assertEqual(disk["title"], "new title")
        self.assertEqual(disk["priority"], 2)
        self.assertEqual(disk["role"], "coder")

        # A legal status transition still stamps lifecycle timestamps and
        # emits the specific event.
        moved = self.store.task_update(old.id, {"status": TaskStatus.DOING})
        self.assertEqual(moved.status, TaskStatus.DOING)
        self.assertIsNotNone(moved.started_at)
        self.assertIn("task.started", self._event_types())
        self.assertEqual(self._disk_task(old.id)["status"], TaskStatus.DOING)
        self.assertEqual(self.make_store().task_get(old.id).status,
                         TaskStatus.DOING)

    # -- persistence failure rollback -------------------------------------
    def test_save_failure_rolls_back_project_and_task(self) -> None:
        self.store.create("Old")
        project = self.store.get("p-1")
        task = self.store.add_task("p-1", "old title")
        project_updated_at = self.store.get("p-1").updated_at
        before_disk = self.state_path.read_bytes()

        real_save = self.store.save

        def failing_save() -> None:
            raise OSError("disk full")

        self.store.save = failing_save  # type: ignore[method-assign]
        try:
            with self.assertRaises(OSError):
                self.store.update("p-1", {"name": "new"})
            with self.assertRaises(OSError):
                self.store.task_update(task.id, {"title": "new"})
        finally:
            self.store.save = real_save  # type: ignore[method-assign]

        self.assertIs(self.store.get("p-1"), project)
        self.assertEqual(project.name, "Old")
        self.assertEqual(project.updated_at, project_updated_at)
        self.assertIs(self.store.task_get(task.id), task)
        self.assertEqual(task.title, "old title")
        self.assertEqual(self.state_path.read_bytes(), before_disk)


if __name__ == "__main__":
    unittest.main()

"""Tests for stackflowos.projects: models, store, persistence and events."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from stackflowos.errors import Conflict, NotFound, ValidationError
from stackflowos.kernel import Context
from stackflowos.projects import (
    ProjectStatus,
    ProjectStore,
    TaskStatus,
    progress_for,
    validate_task_transition,
)
from stackflowos.projects.models import Project, Task

REPO_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = REPO_ROOT / ".stackflowos" / "tmp"


class Record:
    """Minimal event-log record matching l2.eventlog's append result."""

    def __init__(self, seq: int, type: str, data: dict, *,
                 source: str, project_id: str | None) -> None:
        self.seq = seq
        self.type = type
        self.data = data
        self.source = source
        self.project_id = project_id

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "type": self.type,
            "data": self.data,
            "source": self.source,
            "project_id": self.project_id,
        }


class RecordingEvents:
    """Fake event log that records every append."""

    def __init__(self) -> None:
        self.records: list[Record] = []

    def append(self, type: str, data: dict, *, source: str = "sys",
               project_id: str | None = None) -> Record:
        record = Record(len(self.records) + 1, type, data,
                        source=source, project_id=project_id)
        self.records.append(record)
        return record

    def types(self) -> list[str]:
        return [record.type for record in self.records]


class ProjectStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="projects-", dir=TMP_ROOT))
        self.state_path = self.tmp / "state" / "projects.json"
        self.counter_path = self.tmp / "state" / "counters.json"
        self.workspace_root = self.tmp / "projects"
        self.ctx = Context(name="test")
        self.events = RecordingEvents()
        self.emitted: list[tuple[str, dict]] = []
        self.ctx.on_any(
            lambda mode, name, payload: self.emitted.append((name, payload)))
        self.store = self.make_store()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_store(self, ctx: Context | None = None,
                   events: RecordingEvents | None = None) -> ProjectStore:
        return ProjectStore(
            self.ctx if ctx is None else ctx,
            state_path=self.state_path,
            counter_path=self.counter_path,
            events=self.events if events is None else events,
            workspace_root=self.workspace_root,
        )

    # -- projects ---------------------------------------------------------
    def test_create_get_list_update_archive(self) -> None:
        project = self.store.create(
            "Demo", "ship the demo", tags=["demo", "v1"], color="#112233",
            milestones=["MVP", {"title": "Polish", "description": "after MVP"}],
        )
        self.assertEqual(project.id, "p-1")
        self.assertEqual(project.status, ProjectStatus.ACTIVE)
        self.assertEqual(project.tags, ["demo", "v1"])
        self.assertEqual(project.color, "#112233")
        self.assertEqual([m.id for m in project.milestones], ["m-1", "m-2"])
        self.assertEqual(project.milestones[1].description, "after MVP")
        self.assertTrue(Path(project.workspace).is_dir())
        self.assertEqual(Path(project.workspace),
                         (self.workspace_root / "p-1" / "workspace").resolve())

        self.assertIs(self.store.get("p-1"), project)
        self.assertEqual([p.id for p in self.store.list()], ["p-1"])
        self.assertEqual([p.id for p in self.store.list(status="active")], ["p-1"])
        self.assertEqual(self.store.list(status="done"), [])

        updated = self.store.update("p-1", {
            "name": "Demo 2", "goal": "new goal", "status": "review",
            "tags": ["x"], "color": "#445566", "brief": "halfway", "degraded": True,
        })
        self.assertEqual(updated.name, "Demo 2")
        self.assertEqual(updated.goal, "new goal")
        self.assertEqual(updated.status, ProjectStatus.REVIEW)
        self.assertEqual(updated.tags, ["x"])
        self.assertEqual(updated.color, "#445566")
        self.assertEqual(updated.brief, "halfway")
        self.assertTrue(updated.degraded)

        with self.assertRaises(ValidationError):
            self.store.update("p-1", {"status": ProjectStatus.ARCHIVED})

        archived = self.store.archive("p-1")
        self.assertEqual(archived.status, ProjectStatus.ARCHIVED)
        self.assertIsNotNone(archived.archived_at)
        self.assertEqual([p.id for p in self.store.list(status="archived")], ["p-1"])

    def test_update_whitelist_and_validation(self) -> None:
        project = self.store.create("Demo")
        created_at = project.created_at
        self.store.update("p-1", {"unknown": "ignored", "name": "Renamed"})
        self.assertEqual(project.name, "Renamed")
        self.assertFalse(hasattr(project, "unknown"))
        self.assertGreaterEqual(project.updated_at, created_at)
        with self.assertRaises(ValidationError):
            self.store.update("p-1", {"status": "bogus"})
        with self.assertRaises(ValidationError):
            self.store.update("p-1", {"name": "   "})
        with self.assertRaises(NotFound):
            self.store.update("p-404", {"name": "nope"})

    def test_create_requires_name_and_unknown_ids(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.create("  ")
        with self.assertRaises(NotFound):
            self.store.get("p-999")
        with self.assertRaises(NotFound):
            self.store.task_get("t-999")
        with self.assertRaises(NotFound):
            self.store.add_task("p-999", "title")
        self.assertEqual(self.store.list(), [])
        # A failed create must not burn a project id.
        first = self.store.create("First")
        self.assertEqual(first.id, "p-1")
        second = self.store.create("Second")
        self.assertEqual(second.id, "p-2")

    def test_milestone_assignment_and_unknown_milestone(self) -> None:
        self.store.create("Demo", milestones=["MVP"])
        task = self.store.add_task("p-1", "build it", milestone_id="m-1")
        self.assertEqual(task.milestone_id, "m-1")
        self.assertEqual(self.store.get("p-1").milestones[0].task_ids, [task.id])
        with self.assertRaises(NotFound):
            self.store.add_task("p-1", "other", milestone_id="m-99")

    # -- persistence ------------------------------------------------------
    def test_persistence_roundtrip_and_counters(self) -> None:
        project = self.store.create("Demo", "goal", milestones=["MVP"])
        task = self.store.add_task("p-1", "write code", role="coder", priority=2,
                                   milestone_id="m-1")
        self.store.task_artifact(task.id, {"type": "file", "path": "a.py",
                                           "title": "a.py"})
        self.store.task_add_note(task.id, "started", author="agent.coder.1")
        self.store.task_add_check(task.id, "tests pass")
        self.store.task_check(task.id, item_id="c-1", note="ok")
        self.store.update("p-1", {"brief": "going well"})

        reloaded = ProjectStore(
            None,
            state_path=self.state_path,
            counter_path=self.counter_path,
            workspace_root=self.workspace_root,
        )
        loaded_project = reloaded.get("p-1")
        self.assertEqual(loaded_project.name, project.name)
        self.assertEqual(loaded_project.brief, "going well")
        self.assertEqual([m.id for m in loaded_project.milestones], ["m-1"])
        loaded_task = reloaded.task_get(task.id)
        self.assertEqual(loaded_task.title, "write code")
        self.assertEqual(loaded_task.role, "coder")
        self.assertEqual(loaded_task.priority, 2)
        self.assertEqual(len(loaded_task.artifacts), 1)
        self.assertEqual(len(loaded_task.notes), 1)
        self.assertTrue(loaded_task.checks[0]["done"])
        self.assertEqual(loaded_task.checks[0]["note"], "ok")
        self.assertEqual(loaded_project.milestones[0].task_ids, [task.id])

        # Counters continue after a full reload, including milestone ids.
        new_project = reloaded.create("Second", milestones=["Later"])
        self.assertEqual(new_project.id, "p-2")
        self.assertEqual(new_project.milestones[0].id, "m-2")
        new_task = reloaded.add_task("p-2", "next")
        self.assertEqual(new_task.id, "t-2")

        raw = json.loads(self.state_path.read_text("utf-8"))
        self.assertEqual(set(raw), {"projects", "version"})
        self.assertEqual(raw["version"], 1)
        self.assertEqual(len(raw["projects"]), 2)
        self.assertIn("tasks", raw["projects"][0])
        counters = json.loads(self.counter_path.read_text("utf-8"))
        self.assertEqual(counters, {"project": 2, "task": 2, "milestone": 2})

    def test_load_recovers_counters_from_data(self) -> None:
        self.store.create("Demo")
        self.store.add_task("p-1", "one")
        self.store.add_task("p-1", "two")
        self.counter_path.unlink()

        reloaded = ProjectStore(
            None,
            state_path=self.state_path,
            counter_path=self.counter_path,
            workspace_root=self.workspace_root,
        )
        self.assertEqual(reloaded.add_task("p-1", "three").id, "t-3")
        counters = json.loads(self.counter_path.read_text("utf-8"))
        self.assertEqual(counters["task"], 3)

    def test_model_serialisation_roundtrip(self) -> None:
        task = Task(id="t-1", project_id="p-1", title="x", deps=["t-0"],
                    artifacts=[{"type": "file", "path": "a", "title": "a", "ts": 1}],
                    checks=[{"id": "c-1", "text": "ok", "done": False, "note": ""}],
                    notes=[{"ts": 1, "author": "a", "text": "n"}])
        self.assertEqual(Task.from_dict(task.to_dict()), task)
        project = Project(id="p-1", name="n", goal="g", tags=["t"],
                          milestones=[])
        self.assertEqual(Project.from_dict(project.to_dict()), project)

    # -- tasks ------------------------------------------------------------
    def test_task_claim_conflict(self) -> None:
        self.store.create("Demo")
        task = self.store.add_task("p-1", "build")
        claimed = self.store.task_claim(task.id, "agent.coder.1")
        self.assertEqual(claimed.status, TaskStatus.DOING)
        self.assertEqual(claimed.assignee, "agent.coder.1")
        self.assertIsNotNone(claimed.started_at)

        with self.assertRaises(Conflict):
            self.store.task_claim(task.id, "agent.coder.2")

        other = self.store.add_task("p-1", "other")
        self.store.task_update(other.id, {"status": TaskStatus.DOING})
        with self.assertRaises(Conflict):
            self.store.task_claim(other.id, "agent.coder.2")

    def test_illegal_transitions(self) -> None:
        self.store.create("Demo")
        task = self.store.add_task("p-1", "build")
        with self.assertRaises(ValidationError) as caught:
            self.store.task_update(task.id, {"status": TaskStatus.DONE})
        self.assertEqual(caught.exception.detail, {"old": "todo", "new": "done"})
        self.assertIn("todo", str(caught.exception))
        self.assertIn("done", str(caught.exception))
        self.assertEqual(self.store.task_get(task.id).status, TaskStatus.TODO)

        with self.assertRaises(ValidationError):
            validate_task_transition("todo", "review")
        with self.assertRaises(ValidationError):
            validate_task_transition("blocked", "review")
        with self.assertRaises(ValidationError):
            validate_task_transition("done", "todo")
        with self.assertRaises(ValidationError):
            validate_task_transition("bogus", "todo")
        validate_task_transition("todo", "todo")
        for old, targets in {
            "todo": ("doing", "blocked"),
            "doing": ("review", "blocked", "todo"),
            "review": ("done", "todo", "blocked"),
            "blocked": ("todo", "doing"),
        }.items():
            for new in targets:
                validate_task_transition(old, new)

    def test_task_lifecycle_timestamps(self) -> None:
        self.store.create("Demo")
        task = self.store.add_task("p-1", "build")
        self.assertIsNone(task.started_at)
        self.store.task_update(task.id, {"status": TaskStatus.DOING})
        self.assertIsNotNone(task.started_at)
        self.store.task_update(task.id, {"status": TaskStatus.REVIEW})
        self.store.task_update(task.id, {"status": TaskStatus.DONE})
        self.assertIsNotNone(task.finished_at)
        archived_status = self.store.task_update(
            task.id, {"status": TaskStatus.DONE})
        self.assertEqual(archived_status.status, TaskStatus.DONE)

    def test_task_update_whitelist(self) -> None:
        self.store.create("Demo")
        task = self.store.add_task("p-1", "build")
        self.store.task_update(task.id, {
            "title": "build it", "description": "desc", "priority": 3,
            "role": "coder", "assignee": "agent.coder.1", "error": "boom",
            "notes": "ignored",
        })
        self.assertEqual(task.title, "build it")
        self.assertEqual(task.description, "desc")
        self.assertEqual(task.priority, 3)
        self.assertEqual(task.role, "coder")
        self.assertEqual(task.assignee, "agent.coder.1")
        self.assertEqual(task.error, "boom")
        self.assertEqual(task.notes, [])
        with self.assertRaises(ValidationError):
            self.store.task_update(task.id, {"priority": 9})
        with self.assertRaises(ValidationError):
            self.store.task_update(task.id, {"title": ""})

    def test_task_sorting_and_filters(self) -> None:
        self.store.create("Demo")
        low = self.store.add_task("p-1", "low", priority=0)
        high = self.store.add_task("p-1", "high", priority=3)
        mid = self.store.add_task("p-1", "mid", priority=1)
        self.assertEqual([t.id for t in self.store.task_list(project_id="p-1")],
                         [high.id, mid.id, low.id])
        self.store.task_claim(mid.id, "agent.tester.1")
        self.assertEqual([t.id for t in self.store.task_list(
            status=TaskStatus.DOING)], [mid.id])
        self.assertEqual([t.id for t in self.store.task_list(
            assignee="agent.tester.1")], [mid.id])
        self.store.create("Other")
        self.assertEqual(len(self.store.task_list(project_id="p-2")), 0)

    # -- progress / cards -------------------------------------------------
    def test_progress_values_and_counts(self) -> None:
        self.store.create("Demo")
        info = self.store.project_progress("p-1")
        self.assertEqual(info, {"progress": 0, "counts": {
            "todo": 0, "doing": 0, "review": 0, "done": 0, "blocked": 0},
            "total": 0, "done": 0})

        a = self.store.add_task("p-1", "a")  # done
        b = self.store.add_task("p-1", "b")  # doing
        c = self.store.add_task("p-1", "c")  # blocked
        self.store.add_task("p-1", "d")      # todo
        for status in (TaskStatus.DOING, TaskStatus.REVIEW, TaskStatus.DONE):
            self.store.task_update(a.id, {"status": status})
        self.store.task_claim(b.id, "agent.coder.1")
        self.store.task_update(c.id, {"status": TaskStatus.BLOCKED})

        info = self.store.project_progress("p-1")
        # (1.0 + 0.45 + 0 + 0) / 4 * 100 = 36.25 -> 36
        self.assertEqual(info["progress"], 36)
        self.assertEqual(info["counts"],
                         {"todo": 1, "doing": 1, "review": 0, "done": 1,
                          "blocked": 1})
        self.assertEqual(info["total"], 4)
        self.assertEqual(info["done"], 1)

        for task in self.store.task_list(project_id="p-1"):
            if task.status == TaskStatus.BLOCKED:
                self.store.task_update(task.id, {"status": TaskStatus.TODO})
            if task.status == TaskStatus.TODO:
                self.store.task_update(task.id, {"status": TaskStatus.DOING})
            if task.status == TaskStatus.DOING:
                self.store.task_update(task.id, {"status": TaskStatus.REVIEW})
            if task.status == TaskStatus.REVIEW:
                self.store.task_update(task.id, {"status": TaskStatus.DONE})
        self.assertEqual(self.store.project_progress("p-1")["progress"], 100)

        self.assertEqual(progress_for([]), 0)
        self.assertEqual(progress_for([
            Task(id="t-1", project_id="p-1", title="x", status="doing"),
            Task(id="t-2", project_id="p-1", title="y", status="review"),
        ]), 65)

    def test_artifacts_notes_and_checks(self) -> None:
        self.store.create("Demo")
        task = self.store.add_task("p-1", "build")
        self.store.task_artifact(task.id, {"type": "file", "path": "out/x.py",
                                           "title": "x"})
        self.assertEqual(len(task.artifacts), 1)
        self.assertIsInstance(task.artifacts[0]["ts"], int)
        self.assertEqual(task.artifacts[0]["path"], "out/x.py")

        self.store.task_add_note(task.id, "hello", author="agent.coder.1")
        self.assertEqual(task.notes[-1]["author"], "agent.coder.1")
        self.assertEqual(task.notes[-1]["text"], "hello")
        with self.assertRaises(ValidationError):
            self.store.task_add_note(task.id, "  ", author="me")

        self.store.task_add_check(task.id, "tests pass")
        self.store.task_add_check(task.id, "docs updated")
        self.assertEqual([c["id"] for c in task.checks], ["c-1", "c-2"])
        self.store.task_check(task.id, item_id="c-1", note="ran unittest")
        self.store.task_check(task.id, text="docs updated")
        self.assertTrue(task.checks[0]["done"])
        self.assertTrue(task.checks[1]["done"])
        self.assertEqual(task.checks[0]["note"], "ran unittest")
        with self.assertRaises(NotFound):
            self.store.task_check(task.id, item_id="c-99")
        with self.assertRaises(ValidationError):
            self.store.task_check(task.id)
        with self.assertRaises(ValidationError):
            self.store.task_artifact(task.id, "not-a-dict")

    def test_card_shapes(self) -> None:
        self.store.create("Demo", "goal", tags=["t"], color="#abcdef",
                          milestones=["MVP"])
        first = self.store.add_task("p-1", "first")
        doing = self.store.add_task("p-1", "second")
        self.store.task_claim(doing.id, "agent.coder.1")

        card = self.store.card("p-1")
        self.assertEqual(card["id"], "p-1")
        self.assertEqual(card["name"], "Demo")
        self.assertEqual(card["status"], ProjectStatus.ACTIVE)
        self.assertEqual(card["progress"], 23)  # 0.45 / 2 * 100 = 22.5 -> 23
        self.assertEqual(card["counts"]["doing"], 1)
        self.assertEqual(card["counts"]["todo"], 1)
        self.assertEqual(card["milestones"], 1)
        self.assertEqual(card["current_task"]["id"], doing.id)
        self.assertEqual(card["tags"], ["t"])
        self.assertEqual(card["color"], "#abcdef")
        self.assertFalse(card["degraded"])
        self.assertIsNone(card["archived_at"])
        self.assertIn("brief", card)
        self.assertIn("workspace", card)
        self.assertIn("updated_at", card)

        task_card = self.store.task_card(first.id)
        self.assertEqual(task_card["id"], first.id)
        self.assertIsInstance(task_card["deps"], list)
        self.assertIsInstance(task_card["artifacts"], list)
        self.assertIsInstance(task_card["checks"], list)
        self.assertIsInstance(task_card["notes"], list)
        self.assertEqual(task_card["progress"], 0)
        self.assertFalse(task_card["is_done"])
        self.assertEqual(task_card["check_total"], 0)

    # -- archived guard ---------------------------------------------------
    def test_archived_project_mutations_refused(self) -> None:
        self.store.create("Demo")
        task = self.store.add_task("p-1", "build")
        self.store.archive("p-1")

        for operation in (
            lambda: self.store.update("p-1", {"name": "x"}),
            lambda: self.store.archive("p-1"),
            lambda: self.store.add_task("p-1", "new"),
            lambda: self.store.task_update(task.id, {"priority": 1}),
            lambda: self.store.task_claim(task.id, "agent.coder.1"),
            lambda: self.store.task_artifact(task.id, {"path": "x"}),
            lambda: self.store.task_add_note(task.id, "n", author="me"),
            lambda: self.store.task_add_check(task.id, "c"),
        ):
            with self.assertRaises(ValidationError) as caught:
                operation()
            self.assertIn("project archived", str(caught.exception))
        self.assertEqual(self.store.task_get(task.id).status, TaskStatus.TODO)

    # -- events -----------------------------------------------------------
    def test_events_emitted_with_self_contained_data(self) -> None:
        self.store.create("Demo", milestones=["MVP"])
        task = self.store.add_task("p-1", "build", role="coder")
        self.store.task_claim(task.id, "agent.coder.1")
        self.store.task_update(task.id, {"status": TaskStatus.REVIEW})
        self.store.task_update(task.id, {"status": TaskStatus.DONE})
        self.store.task_artifact(task.id, {"type": "file", "path": "x",
                                           "title": "x"})
        self.store.task_add_note(task.id, "done", author="agent.coder.1")
        self.store.update("p-1", {"brief": "done"})
        self.store.archive("p-1")

        types = self.events.types()
        for expected in (
            "project.created", "task.created", "task.claimed", "task.updated",
            "task.review", "task.completed", "task.artifact", "task.note",
            "project.progress", "project.updated", "project.archived",
        ):
            self.assertIn(expected, types)

        created = next(r for r in self.events.records
                       if r.type == "project.created")
        self.assertEqual(created.data["id"], "p-1")
        self.assertEqual(created.project_id, "p-1")
        self.assertEqual(created.source, "sys")
        claimed = next(r for r in self.events.records
                       if r.type == "task.claimed")
        self.assertEqual(claimed.data["assignee"], "agent.coder.1")
        progress = next(r for r in self.events.records
                        if r.type == "project.progress")
        self.assertEqual(set(progress.data), {"project_id", "progress", "counts"})
        self.assertEqual(progress.data["project_id"], "p-1")

        # The context sees the exact event-log record projection.
        emitted_types = [name for name, _payload in self.emitted]
        self.assertEqual(emitted_types, types)
        ctx_progress = [payload for name, payload in self.emitted
                        if name == "project.progress"][-1]
        self.assertEqual(ctx_progress["type"], "project.progress")
        self.assertEqual(ctx_progress["data"]["progress"], 100)

    def test_no_ctx_or_events_is_fine(self) -> None:
        store = ProjectStore(
            None,
            state_path=self.state_path,
            counter_path=self.counter_path,
            workspace_root=self.workspace_root,
        )
        project = store.create("Headless")
        task = store.add_task(project.id, "work")
        store.task_claim(task.id, "agent.coder.1")
        self.assertEqual(store.get(project.id).name, "Headless")


if __name__ == "__main__":
    unittest.main()

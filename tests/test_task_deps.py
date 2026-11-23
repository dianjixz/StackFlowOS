"""Task dependency validation tests (S2).

``add_task`` and ``task_update`` accept a ``deps`` list, but that list used to
be stored verbatim: dangling ids, cross-project ids, self references and
cycles all made it to disk. These tests pin the contract that a dependency
set is validated against the same-project task graph before anything is
persisted, and that a rejected create/update leaves memory, disk and events
untouched.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from stackflowos.errors import NotFound, ValidationError
from stackflowos.kernel import Context
from stackflowos.projects import ProjectStore
from stackflowos.projects.models import Task

REPO_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = REPO_ROOT / ".stackflowos" / "tmp"


class TaskDepsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="task-deps-", dir=TMP_ROOT))
        self.state_path = self.tmp / "state" / "projects.json"
        self.counter_path = self.tmp / "state" / "counters.json"
        self.ctx = Context(name="test")
        self.emitted: list[tuple[str, dict]] = []
        self.ctx.on_any(
            lambda mode, name, payload: self.emitted.append((name, payload)))
        self.store = self.make_store()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_store(self) -> ProjectStore:
        return ProjectStore(
            self.ctx,
            state_path=self.state_path,
            counter_path=self.counter_path,
            workspace_root=self.tmp / "projects",
        )

    # -- helpers ----------------------------------------------------------
    def _state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _disk_task(self, task_id: str) -> dict:
        for project in self._state()["projects"]:
            for task in project["tasks"]:
                if task["id"] == task_id:
                    return task
        self.fail(f"task {task_id} not on disk")

    def _event_types(self) -> list[str]:
        return [name for name, _payload in self.emitted]

    def _assert_task_absent(self, task_id: str) -> None:
        with self.assertRaises(NotFound):
            self.store.task_get(task_id)

    # -- add_task: valid deps ---------------------------------------------
    def test_add_task_with_valid_deps_roundtrips(self) -> None:
        self.store.create("Demo")
        base = self.store.add_task("p-1", "base")
        dependent = self.store.add_task("p-1", "dependent", deps=[base.id])

        self.assertEqual(dependent.deps, [base.id])
        self.assertEqual(dependent.to_dict()["deps"], [base.id])
        self.assertEqual(self._disk_task(dependent.id)["deps"], [base.id])
        self.assertEqual([t.id for t in self.store.task_list()],
                         [base.id, dependent.id])

        reloaded = self.make_store()
        self.assertEqual(reloaded.task_get(dependent.id).deps, [base.id])

    def test_add_task_default_and_empty_deps(self) -> None:
        self.store.create("Demo")
        self.assertEqual(self.store.add_task("p-1", "one").deps, [])
        self.assertEqual(self.store.add_task("p-1", "two", deps=[]).deps, [])
        self.assertEqual(self._disk_task("t-2")["deps"], [])

    def test_add_task_copies_the_input_list(self) -> None:
        self.store.create("Demo")
        base = self.store.add_task("p-1", "base")
        deps = [base.id]
        task = self.store.add_task("p-1", "dependent", deps=deps)
        deps.append("t-999")
        self.assertEqual(task.deps, [base.id])

    def test_add_task_allows_chain(self) -> None:
        self.store.create("Demo")
        a = self.store.add_task("p-1", "a")
        b = self.store.add_task("p-1", "b", deps=[a.id])
        c = self.store.add_task("p-1", "c", deps=[b.id])
        self.assertEqual([a.deps, b.deps, c.deps], [[], [a.id], [b.id]])

    # -- add_task: invalid deps -------------------------------------------
    def test_add_task_unknown_dep_rejected_without_side_effects(self) -> None:
        self.store.create("Demo")
        state_before = self.state_path.read_bytes()
        counters_before = self.counter_path.read_bytes()
        events_before = self._event_types()

        with self.assertRaises(ValidationError) as caught:
            self.store.add_task("p-1", "x", deps=["t-404"])
        self.assertIn("t-404", str(caught.exception))

        self.assertEqual(self.store.task_list(), [])
        self.assertEqual(self.state_path.read_bytes(), state_before)
        self.assertEqual(self.counter_path.read_bytes(), counters_before)
        self.assertEqual(self._event_types(), events_before)
        # The rejected create must not burn a task id.
        self.assertEqual(self.store.add_task("p-1", "x").id, "t-1")

    def test_add_task_cross_project_dep_rejected(self) -> None:
        self.store.create("One")
        self.store.create("Two")
        foreign = self.store.add_task("p-2", "foreign")
        state_before = self.state_path.read_bytes()

        with self.assertRaises(ValidationError) as caught:
            self.store.add_task("p-1", "x", deps=[foreign.id])
        self.assertIn("another project", str(caught.exception))
        self.assertEqual(self.state_path.read_bytes(), state_before)
        self.assertEqual(self.store.task_list(project_id="p-1"), [])

    def test_add_task_self_reference_rejected_without_burning_id(self) -> None:
        self.store.create("Demo")
        # t-1 is the id the next successful create would receive.
        with self.assertRaises(ValidationError) as caught:
            self.store.add_task("p-1", "x", deps=["t-1"])
        self.assertIn("itself", str(caught.exception))
        self.assertEqual(self.store.task_list(), [])
        self.assertEqual(self.store.add_task("p-1", "x").id, "t-1")

    def test_add_task_cycle_through_legacy_edges_rejected(self) -> None:
        self.store.create("Demo")
        legacy = self.store.add_task("p-1", "legacy")
        # Simulate state written by an older release: an edge pointing at the
        # id that is about to be handed out (t-2).
        legacy.deps = ["t-2"]
        with self.assertRaises(ValidationError) as caught:
            self.store.add_task("p-1", "new", deps=[legacy.id])
        self.assertIn("cycle", str(caught.exception))
        self.assertEqual([task.id for task in self.store.task_list()],
                         [legacy.id])

    def test_add_task_duplicate_deps_rejected(self) -> None:
        self.store.create("Demo")
        base = self.store.add_task("p-1", "base")
        with self.assertRaises(ValidationError) as caught:
            self.store.add_task("p-1", "x", deps=[base.id, base.id])
        self.assertIn("duplicate", str(caught.exception))
        self.assertEqual(len(self.store.task_list()), 1)

    def test_add_task_malformed_deps_rejected(self) -> None:
        self.store.create("Demo")
        base = self.store.add_task("p-1", "base")
        bad_values = [
            "t-1",               # str instead of list
            base.id,             # str instead of list
            1,
            {"deps": [base.id]},
            (base.id,),          # tuple is not a list
            [1],                 # non-str entry
            [None],
            [base.id, 2],        # mixed
            [""],                # empty string
            ["   "],             # whitespace-only
        ]
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self.store.add_task("p-1", "x", deps=value)
        self.assertEqual([task.id for task in self.store.task_list()], [base.id])

    # -- task_update: valid deps ------------------------------------------
    def test_update_replaces_deps(self) -> None:
        self.store.create("Demo")
        a = self.store.add_task("p-1", "a")
        b = self.store.add_task("p-1", "b", deps=[a.id])
        c = self.store.add_task("p-1", "c")

        updated = self.store.task_update(b.id, {"deps": [c.id]})
        self.assertEqual(updated.deps, [c.id])
        self.assertEqual(updated.to_dict()["deps"], [c.id])
        self.assertEqual(self._disk_task(b.id)["deps"], [c.id])
        self.assertEqual(self.make_store().task_get(b.id).deps, [c.id])

    def test_update_can_clear_deps(self) -> None:
        self.store.create("Demo")
        a = self.store.add_task("p-1", "a")
        b = self.store.add_task("p-1", "b", deps=[a.id])
        self.assertEqual(self.store.task_update(b.id, {"deps": []}).deps, [])
        self.assertEqual(self._disk_task(b.id)["deps"], [])
        self.assertEqual(self.store.task_update(b.id, {"deps": None}).deps, [])
        self.assertEqual(self.make_store().task_get(b.id).deps, [])

    def test_update_allows_chain_rewire(self) -> None:
        self.store.create("Demo")
        a = self.store.add_task("p-1", "a")
        b = self.store.add_task("p-1", "b", deps=[a.id])
        c = self.store.add_task("p-1", "c", deps=[b.id])
        # c no longer depends on b: acyclic and accepted.
        self.assertEqual(self.store.task_update(c.id, {"deps": [a.id]}).deps,
                         [a.id])
        self.assertEqual(self.store.task_update(b.id, {"deps": []}).deps, [])

    # -- task_update: invalid deps ----------------------------------------
    def test_update_invalid_deps_keeps_memory_and_disk(self) -> None:
        self.store.create("Demo")
        a = self.store.add_task("p-1", "a")
        self.store.create("Other")
        foreign = self.store.add_task("p-2", "foreign")

        state_before = self.state_path.read_bytes()
        events_before = self._event_types()
        updated_before = a.updated_at

        bad_patches = [
            {"deps": ["t-404"]},          # unknown
            {"deps": [foreign.id]},       # cross-project
            {"deps": [a.id]},             # self reference
            {"deps": ["t-404", "t-404"]},  # duplicate (also unknown)
            {"deps": "t-1"},              # non-list
            {"deps": [1]},                # non-str
            {"deps": [""]},               # empty
            # A valid field must not leak through when deps are invalid.
            {"deps": ["t-404"], "title": "hacked", "priority": 3},
        ]
        for patch in bad_patches:
            with self.subTest(patch=patch):
                with self.assertRaises(ValidationError):
                    self.store.task_update(a.id, patch)

        current = self.store.task_get(a.id)
        self.assertEqual(current.deps, [])
        self.assertEqual(current.title, "a")
        self.assertEqual(current.priority, 0)
        self.assertEqual(current.updated_at, updated_before)
        self.assertEqual(self._disk_task(a.id)["deps"], [])
        self.assertEqual(self.state_path.read_bytes(), state_before)
        self.assertEqual(self._event_types(), events_before)

    def test_update_rejects_two_node_cycle(self) -> None:
        self.store.create("Demo")
        a = self.store.add_task("p-1", "a")
        b = self.store.add_task("p-1", "b", deps=[a.id])
        state_before = self.state_path.read_bytes()

        # A -> B -> A would close the cycle.
        with self.assertRaises(ValidationError) as caught:
            self.store.task_update(a.id, {"deps": [b.id]})
        self.assertIn("cycle", str(caught.exception))
        self.assertEqual(self.store.task_get(a.id).deps, [])
        self.assertEqual(self._disk_task(a.id)["deps"], [])
        self.assertEqual(self.state_path.read_bytes(), state_before)

    def test_update_rejects_multilevel_cycle(self) -> None:
        self.store.create("Demo")
        a = self.store.add_task("p-1", "a")
        b = self.store.add_task("p-1", "b", deps=[a.id])
        c = self.store.add_task("p-1", "c", deps=[b.id])
        state_before = self.state_path.read_bytes()

        # A -> C -> B -> A.
        with self.assertRaises(ValidationError) as caught:
            self.store.task_update(a.id, {"deps": [c.id]})
        self.assertIn("cycle", str(caught.exception))
        self.assertEqual(self.store.task_get(a.id).deps, [])
        self.assertEqual(self._disk_task(a.id)["deps"], [])
        self.assertEqual(self.state_path.read_bytes(), state_before)

        # The rejected patch did not disturb the rest of the graph.
        self.assertEqual(self.store.task_get(b.id).deps, [a.id])
        self.assertEqual(self.store.task_get(c.id).deps, [b.id])

    def test_update_rejects_deps_from_other_project_task(self) -> None:
        self.store.create("Demo")
        a = self.store.add_task("p-1", "a")
        self.store.create("Other")
        foreign = self.store.add_task("p-2", "foreign")
        with self.assertRaises(ValidationError) as caught:
            self.store.task_update(a.id, {"deps": [foreign.id]})
        self.assertIn("another project", str(caught.exception))
        self.assertEqual(self.store.task_get(a.id).deps, [])

    # -- project lifecycle ------------------------------------------------
    def test_delete_project_removes_tasks_with_deps(self) -> None:
        self.store.create("One")
        self.store.create("Two")
        a = self.store.add_task("p-1", "a")
        b = self.store.add_task("p-1", "b", deps=[a.id])
        other = self.store.add_task("p-2", "other")

        self.store.delete("p-1")
        self._assert_task_absent(a.id)
        self._assert_task_absent(b.id)
        self.assertEqual(self.store.task_get(other.id).id, other.id)

        reloaded = self.make_store()
        self.assertEqual([t.id for t in reloaded.task_list()], [other.id])

    def test_legacy_foreign_dep_does_not_break_cycle_check(self) -> None:
        """Existing damaged state is tolerated; only new edges are validated."""
        self.store.create("One")
        self.store.create("Two")
        legacy = self.store.add_task("p-1", "legacy")
        foreign = self.store.add_task("p-2", "foreign")
        legacy.deps = [foreign.id]  # pre-S2 data could contain this

        # Adding an unrelated task in p-1 stays fine.
        new = self.store.add_task("p-1", "new")
        self.assertEqual(new.deps, [])
        # Walking the graph skips the foreign node instead of exploding.
        cycle = self.store._has_dep_cycle("p-1", "t-99", [legacy.id])
        self.assertFalse(cycle)
        self.assertIsInstance(self.store.task_get(legacy.id), Task)


if __name__ == "__main__":
    unittest.main()

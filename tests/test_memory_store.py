"""L6: per-project, bounded, corruption-safe agent memory.

The old ``MemoryStore`` kept one ``state/memory.json``, rewrote all of it on
every ``set`` and raised during ``App.start`` when the file was corrupt.  These
tests pin the new contract: one file per project, size caps, quarantine of bad
files, migration of the legacy store and cleanup on project deletion.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from stackflowos.app import App, MemoryStore
from stackflowos.config import Config, default_root
from stackflowos.errors import ValidationError

REPO = default_root()


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        self.dir = self.state / "memory"
        self.legacy = self.state / "memory.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _store(self, **kwargs) -> MemoryStore:
        return MemoryStore(self.dir, **kwargs)

    # -- per-project files ------------------------------------------------
    def test_set_writes_one_file_per_project(self):
        store = self._store()
        store.set("p-1", "a", 1)
        store.set("p-2", "b", [2, 3])

        self.assertTrue((self.dir / "p-1.json").is_file())
        self.assertTrue((self.dir / "p-2.json").is_file())
        self.assertEqual(
            json.loads((self.dir / "p-1.json").read_text("utf-8")), {"a": 1})
        self.assertEqual(
            json.loads((self.dir / "p-2.json").read_text("utf-8")),
            {"b": [2, 3]})

        # Reads stay isolated per project and survive a reload.
        self.assertIsNone(store.get("p-1", "b"))
        self.assertEqual(store.all("p-2"), {"b": [2, 3]})
        reloaded = self._store()
        self.assertEqual(reloaded.get("p-1", "a"), 1)
        self.assertEqual(reloaded.all("p-2"), {"b": [2, 3]})
        self.assertEqual(set(reloaded.data), {"p-1", "p-2"})

    def test_set_does_not_touch_other_project_files(self):
        store = self._store()
        store.set("p-1", "a", 1)
        before = (self.dir / "p-1.json").read_bytes()
        store.set("p-2", "b", 2)
        self.assertEqual((self.dir / "p-1.json").read_bytes(), before)

    def test_project_id_is_validated_before_any_write(self):
        store = self._store()
        for bad in ("../../etc/passwd", "..", ".", "a/b", "", "x y"):
            with self.subTest(project_id=bad):
                with self.assertRaises(ValidationError):
                    store.set(bad, "k", 1)
                with self.assertRaises(ValidationError):
                    store.get(bad, "k")
        # Nothing escaped the memory directory.
        self.assertEqual(list(self.dir.glob("*.json")), [])

    # -- corruption -------------------------------------------------------
    def test_corrupt_project_file_is_quarantined(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        bad = self.dir / "p-bad.json"
        bad.write_text("{ this is not json", "utf-8")

        store = self._store()  # must not raise

        self.assertIsNone(store.get("p-bad", "k"))
        self.assertFalse(bad.exists())
        quarantined = list(self.dir.glob("p-bad.json.corrupt-*"))
        self.assertEqual(len(quarantined), 1)
        reports = store.drain_corruptions()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["project_id"], "p-bad")
        self.assertTrue(reports[0]["quarantined_to"])
        # The quarantined bytes are preserved for inspection.
        self.assertIn("not json", quarantined[0].read_text("utf-8"))
        # The project starts empty and remains usable.
        self.assertEqual(store.all("p-bad"), {})
        store.set("p-bad", "k", "v")
        self.assertEqual(store.get("p-bad", "k"), "v")
        self.assertEqual(self._store().get("p-bad", "k"), "v")

    def test_corrupt_legacy_file_does_not_block_startup(self):
        self.legacy.write_text("not-json", "utf-8")
        store = self._store()
        self.assertEqual(store.all("p-1"), {})
        self.assertFalse(self.legacy.exists())
        self.assertEqual(
            len(list(self.state.glob("memory.json.corrupt-*"))), 1)
        self.assertEqual(store.drain_corruptions()[0]["project_id"], None)

    def test_non_object_project_file_is_quarantined(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "p-list.json").write_text("[1, 2, 3]", "utf-8")
        store = self._store()
        self.assertEqual(store.all("p-list"), {})
        self.assertEqual(
            len(list(self.dir.glob("p-list.json.corrupt-*"))), 1)

    # -- size limits ------------------------------------------------------
    def test_oversized_value_is_rejected_without_writing(self):
        store = self._store(max_project_bytes=1024, max_total_bytes=1024 * 1024)
        with self.assertRaises(ValidationError) as ctx:
            store.set("p-1", "k", "x" * 2048)
        self.assertEqual(ctx.exception.http_status, 400)
        self.assertFalse((self.dir / "p-1.json").exists())
        self.assertIsNone(store.get("p-1", "k"))
        self.assertEqual(store.all("p-1"), {})

        store.set("p-1", "k", "small")  # under the cap: still writable
        self.assertEqual(store.get("p-1", "k"), "small")
        self.assertTrue((self.dir / "p-1.json").is_file())

    def test_oversized_project_is_rejected_after_accumulation(self):
        store = self._store(max_project_bytes=1024, max_total_bytes=1024 * 1024)
        store.set("p-1", "k", "x" * 700)
        with self.assertRaises(ValidationError):
            store.set("p-1", "k2", "y" * 700)

    def test_total_size_cap_rejects_and_keeps_other_projects(self):
        store = self._store(max_project_bytes=4096, max_total_bytes=1024)
        store.set("p-1", "k", "x" * 900)   # ~902 encoded bytes
        with self.assertRaises(ValidationError) as ctx:
            store.set("p-2", "k", "y" * 900)
        self.assertIn("total", ctx.exception.message)
        self.assertFalse((self.dir / "p-2.json").exists())
        self.assertEqual(store.get("p-1", "k"), "x" * 900)

    def test_project_count_cap_rejects_new_projects(self):
        store = self._store(max_projects=1, max_project_bytes=4096,
                            max_total_bytes=64 * 1024)
        store.set("p-1", "k", 1)
        with self.assertRaises(ValidationError) as ctx:
            store.set("p-2", "k", 2)
        self.assertIn("project limit", ctx.exception.message)
        self.assertFalse((self.dir / "p-2.json").exists())
        store.set("p-1", "k2", 2)  # existing project keeps working
        self.assertEqual(store.all("p-1"), {"k": 1, "k2": 2})

    def test_non_serialisable_value_is_a_validation_error(self):
        store = self._store()
        with self.assertRaises(ValidationError):
            store.set("p-1", "k", object())
        self.assertFalse((self.dir / "p-1.json").exists())

    # -- legacy migration -------------------------------------------------
    def test_legacy_memory_is_migrated_and_backed_up(self):
        legacy = {"p-1": {"a": 1}, "p-2": {"b": [1, 2]}}
        self.legacy.write_text(json.dumps(legacy), "utf-8")

        store = self._store()

        self.assertEqual(store.get("p-1", "a"), 1)
        self.assertEqual(store.all("p-2"), {"b": [1, 2]})
        self.assertTrue((self.dir / "p-1.json").is_file())
        self.assertTrue((self.dir / "p-2.json").is_file())
        self.assertFalse(self.legacy.exists())
        backup = self.state / "memory.json.migrated"
        self.assertTrue(backup.is_file())
        self.assertEqual(json.loads(backup.read_text("utf-8")), legacy)
        # A second boot reads the migrated files, not the old file.
        self.assertEqual(self._store().get("p-1", "a"), 1)

    def test_migration_does_not_overwrite_newer_project_files(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "p-1.json").write_text(
            json.dumps({"a": "new"}), "utf-8")
        self.legacy.write_text(
            json.dumps({"p-1": {"a": "old"}, "p-2": {"b": 2}}), "utf-8")

        store = self._store()
        self.assertEqual(store.get("p-1", "a"), "new")
        self.assertEqual(store.get("p-2", "b"), 2)

    def test_migration_skips_unsafe_project_ids(self):
        self.legacy.write_text(
            json.dumps({"../../evil": {"a": 1}, "p-ok": {"b": 2}}), "utf-8")
        store = self._store()
        self.assertEqual(store.get("p-ok", "b"), 2)
        self.assertEqual(list(self.dir.glob("*.json")),
                         [self.dir / "p-ok.json"])

    # -- compatibility / permissions -------------------------------------
    def test_old_single_file_path_still_constructs(self):
        self.legacy.write_text(json.dumps({"p-1": {"a": 1}}), "utf-8")
        store = MemoryStore(self.legacy)  # old ``memory.json`` call style
        self.assertEqual(store.path, self.dir)
        self.assertEqual(store.get("p-1", "a"), 1)
        store.set("p-2", "b", 2)
        self.assertTrue((self.dir / "p-2.json").is_file())

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_directory_and_file_permissions(self):
        store = self._store()
        store.set("p-1", "k", 1)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.dir / "p-1.json").stat().st_mode), 0o600)

    # -- concurrency ------------------------------------------------------
    def test_concurrent_sets_are_thread_safe(self):
        store = self._store(max_project_bytes=64 * 1024,
                            max_total_bytes=4 * 1024 * 1024)
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                for step in range(20):
                    store.set(f"p-{index}", f"k{step}", step)
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        for index in range(8):
            project = store.all(f"p-{index}")
            self.assertEqual(len(project), 20)
            self.assertEqual(project["k19"], 19)
            self.assertEqual(json.loads(
                (self.dir / f"p-{index}.json").read_text("utf-8")), project)


class MemoryStoreAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.app = App(Config.load(root=self.tmp, state_dir=self.tmp))
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    async def test_delete_project_removes_memory_file(self):
        card = await self.app.create_project("记忆清理", "删除测试",
                                             spawn_lead=False)
        pid = card["id"]
        self.app.memory.set(pid, "k", {"v": 1})
        path = self.app.memory.path / f"{pid}.json"
        self.assertTrue(path.is_file())

        await self.app.delete_project(pid, confirm=card["name"])

        self.assertFalse(path.exists())
        self.assertIsNone(self.app.memory.get(pid, "k"))
        self.assertEqual(self.app.memory.data, {})


class CorruptMemoryBootTests(unittest.IsolatedAsyncioTestCase):
    async def test_corrupt_memory_does_not_block_boot_and_emits_event(self):
        with tempfile.TemporaryDirectory(
                dir=str(REPO / ".stackflowos" / "tmp")) as tmp:
            root = Path(tmp)
            memory_dir = root / "state" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "p-bad.json").write_text("{broken", "utf-8")
            (root / "state" / "memory.json").write_text("nope", "utf-8")
            app = App(Config.load(root=root, state_dir=root))
            await app.start()
            try:
                types = [r.type for r in app.eventlog.tail(50)]
                self.assertIn("memory.corrupt", types)
                self.assertEqual(app.memory.all("p-bad"), {})
                self.assertEqual(
                    len(list(memory_dir.glob("p-bad.json.corrupt-*"))), 1)
                app.memory.set("p-bad", "k", 1)
                self.assertEqual(app.memory.get("p-bad", "k"), 1)
            finally:
                await app.stop()


if __name__ == "__main__":
    unittest.main()

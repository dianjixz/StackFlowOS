"""Tests for backup / restore / event export (observability.backup)."""

from __future__ import annotations

import csv
import io
import json
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from stackflowos.errors import Conflict, ValidationError
from stackflowos.observability import backup


def _make_state(root: Path) -> Path:
    state = root / ".stackflowos"
    (state / "state").mkdir(parents=True)
    (state / "projects" / "p-1" / "workspace").mkdir(parents=True)
    (state / "events").mkdir()
    (state / "config.json").write_text('{"system": {"name": "Test"}}', "utf-8")
    (state / "state" / "projects.json").write_text('{"projects": []}', "utf-8")
    (state / "state" / "approvals.json").write_text('{"requests": []}', "utf-8")
    (state / "projects" / "p-1" / "workspace" / "main.py").write_text(
        "print('hi')\n", "utf-8")
    (state / "events" / "events.jsonl").write_text(
        '{"seq": 1, "type": "sys.booted", "source": "sys", "ts": 100, '
        '"data": {}, "project_id": null}\n'
        '{"seq": 2, "type": "approval.created", "source": "sys", "ts": 101, '
        '"data": {"x": 1}, "project_id": "p-1"}\n', "utf-8")
    # Excluded / dangerous entries that must never be archived.
    (state / "run").mkdir()
    (state / "run" / "rpc.sock").write_text("not a real socket", "utf-8")
    (state / "tmp").mkdir()
    (state / "tmp" / "scratch").write_text("tmp", "utf-8")
    (state / "logs").mkdir()
    (state / "logs" / "hub.log").write_text("log", "utf-8")
    return state


class BackupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_backup_layout_manifest_and_exclusions(self):
        state = _make_state(self.tmp)
        archive = backup.create_backup(state)
        self.assertTrue(archive.exists())
        self.assertEqual(archive.parent, state / "backups")
        # Backups may contain config/state credentials: owner-only mode.
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        with tarfile.open(archive, "r:gz") as tar:
            names = set(tar.getnames())
            manifest = json.load(tar.extractfile("manifest.json"))
        self.assertIn("config.json", names)
        self.assertIn("state/projects.json", names)
        self.assertIn("projects/p-1/workspace/main.py", names)
        self.assertIn("events/events.jsonl", names)
        self.assertIn("manifest.json", names)
        self.assertFalse(any(name.startswith("run/") for name in names))
        self.assertFalse(any(name.startswith("tmp/") for name in names))
        self.assertFalse(any(name.startswith("logs/") for name in names))
        self.assertEqual(manifest["format"], "sfo-backup")
        self.assertEqual(manifest["version"], backup.BACKUP_VERSION)
        self.assertIn("created_at", manifest)
        self.assertIn("app_version", manifest)
        self.assertEqual(manifest["counts"]["files"], len(manifest["files"]))
        entry = manifest["files"]["projects/p-1/workspace/main.py"]
        self.assertEqual(entry["size"], len(b"print('hi')\n"))
        self.assertEqual(len(entry["sha256"]), 64)

    def test_retention_keeps_newest(self):
        state = _make_state(self.tmp)
        paths = []
        for _ in range(3):
            paths.append(backup.create_backup(state, keep=2))
            time.sleep(0.03)
        remaining = backup.list_backups(state)
        self.assertEqual(len(remaining), 2)
        self.assertIn(paths[-1], remaining)
        self.assertNotIn(paths[0], remaining)

    def test_round_trip_restores_files_and_keeps_safety_backup(self):
        state = _make_state(self.tmp)
        archive = backup.create_backup(state)
        (state / "projects" / "p-1" / "workspace" / "main.py").write_text(
            "changed\n", "utf-8")
        (state / "state" / "projects.json").write_text('{"projects": [1]}',
                                                      "utf-8")
        result = backup.restore(archive, state, force=True)
        self.assertTrue(result["replaced"])
        self.assertEqual(
            (state / "projects" / "p-1" / "workspace" / "main.py").read_text(),
            "print('hi')\n")
        self.assertEqual((state / "state" / "projects.json").read_text(),
                         '{"projects": []}')
        # The pre-restore snapshot survives inside the restored state.
        self.assertIsNotNone(result["backup"])
        self.assertTrue(Path(result["backup"]).exists())
        self.assertEqual(Path(result["backup"]).parent, state / "backups")
        # The archive itself lived under the state dir; it must also survive.
        self.assertTrue((state / "backups" / archive.name).exists())
        self.assertFalse((state / "run").exists())
        self.assertEqual(result["manifest"]["version"], backup.BACKUP_VERSION)

    def test_restore_requires_force_for_nonempty_target(self):
        source = _make_state(self.tmp / "source")
        archive = backup.create_backup(source)
        target = _make_state(self.tmp / "target")
        marker = target / "state" / "keep.txt"
        marker.write_text("keep", "utf-8")
        with self.assertRaises(Conflict):
            backup.restore(archive, target)
        self.assertEqual(marker.read_text(), "keep")
        # force succeeds and the pre-restore state is backed up.
        result = backup.restore(archive, target, force=True)
        self.assertEqual(result["files"], 5)
        self.assertIsNotNone(result["backup"])

    def test_restore_into_new_directory(self):
        source = _make_state(self.tmp / "source")
        archive = backup.create_backup(source)
        target = self.tmp / "new" / "state"
        result = backup.restore(archive, target)
        self.assertFalse(result["replaced"])
        self.assertIsNone(result["backup"])
        self.assertTrue((target / "state" / "projects.json").exists())

    def test_restore_rejects_path_traversal(self):
        evil = self.tmp / "evil.tar.gz"
        with tarfile.open(evil, "w:gz") as tar:
            data = b"pwned"
            info = tarfile.TarInfo("../evil.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        with self.assertRaises(ValidationError):
            backup.restore(evil, self.tmp / "target")
        self.assertFalse((self.tmp / "evil.txt").exists())

    def test_restore_rejects_absolute_and_symlink_members(self):
        absolute = self.tmp / "absolute.tar.gz"
        with tarfile.open(absolute, "w:gz") as tar:
            info = tarfile.TarInfo("/tmp/evil.txt")
            info.size = 3
            tar.addfile(info, io.BytesIO(b"bad"))
        with self.assertRaises(ValidationError):
            backup.restore(absolute, self.tmp / "target-abs")

        link = self.tmp / "link.tar.gz"
        with tarfile.open(link, "w:gz") as tar:
            info = tarfile.TarInfo("state/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        with self.assertRaises(ValidationError):
            backup.restore(link, self.tmp / "target-link")
        self.assertFalse((self.tmp / "target-link" / "state" / "link").exists())

    def test_restore_rejects_corrupt_archive(self):
        state = _make_state(self.tmp)
        archive = backup.create_backup(state)
        archive.write_bytes(archive.read_bytes()[:40])
        with self.assertRaises(ValidationError):
            backup.restore(archive, self.tmp / "target-corrupt")

    def test_restore_rejects_checksum_mismatch(self):
        archive = self.tmp / "tampered.tar.gz"
        payload = b"actual"
        manifest = {
            "format": "sfo-backup",
            "version": 1,
            "files": {"state/a.json": {
                "size": len(payload),
                "sha256": "0" * 64,
            }},
        }
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("state/a.json")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
            data = json.dumps(manifest).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        with self.assertRaises(ValidationError):
            backup.restore(archive, self.tmp / "target-tampered")

    def test_restore_rejects_extra_member_not_in_manifest(self):
        archive = self.tmp / "extra.tar.gz"
        manifest = {"format": "sfo-backup", "version": 1, "files": {}}
        with tarfile.open(archive, "w:gz") as tar:
            data = b"secret"
            info = tarfile.TarInfo("state/extra.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            data = json.dumps(manifest).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        with self.assertRaises(ValidationError):
            backup.restore(archive, self.tmp / "target-extra")

    def test_symlinks_are_not_archived(self):
        state = _make_state(self.tmp)
        outside = self.tmp / "outside.txt"
        outside.write_text("outside", "utf-8")
        (state / "projects" / "p-1" / "workspace" / "link.txt").symlink_to(
            outside)
        archive = backup.create_backup(state)
        with tarfile.open(archive, "r:gz") as tar:
            manifest = json.load(tar.extractfile("manifest.json"))
            names = set(tar.getnames())
        self.assertNotIn("projects/p-1/workspace/link.txt", names)
        self.assertNotIn("projects/p-1/workspace/link.txt",
                         manifest["files"])
        self.assertTrue(any("symlink" in item for item in manifest["skipped"]))

    def test_export_events_jsonl_and_csv(self):
        state = _make_state(self.tmp)
        events = state / "events" / "events.jsonl"
        with events.open("a", encoding="utf-8") as handle:
            for seq, type_ in ((3, "unit.failed"), (4, "approval.decided"),
                               (5, "approval.auto")):
                handle.write(json.dumps({
                    "seq": seq, "type": type_, "source": "sys",
                    "ts": 100 + seq, "data": {"seq": seq},
                    "project_id": "p-1" if seq == 4 else None,
                }) + "\n")
        jsonl = backup.export_events(state, format="jsonl", since=2,
                                     types="approval")
        rows = [json.loads(line) for line in jsonl.read_text().splitlines()]
        self.assertEqual([row["seq"] for row in rows], [4, 5])
        csv_path = backup.export_events(state, format="csv", until=3)
        with csv_path.open(newline="", encoding="utf-8") as handle:
            table = list(csv.reader(handle))
        self.assertEqual(table[0], ["seq", "ts", "type", "source",
                                    "project_id", "data"])
        self.assertEqual([row[0] for row in table[1:]], ["1", "2", "3"])

    def test_export_project_filter_and_bad_format(self):
        state = _make_state(self.tmp)
        out = backup.export_events(state, format="jsonl", project_id="p-1")
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual([row["seq"] for row in rows], [2])
        with self.assertRaises(ValidationError):
            backup.export_events(state, format="xml")

    def test_backup_status_reports_last_backup(self):
        state = _make_state(self.tmp)
        self.assertIsNone(backup.backup_status(state)["last_backup"])
        archive = backup.create_backup(state)
        status = backup.backup_status(state)
        self.assertEqual(status["last_backup"], str(archive))
        self.assertEqual(status["backups"], 1)
        self.assertGreater(status["bytes"], 0)


if __name__ == "__main__":
    unittest.main()

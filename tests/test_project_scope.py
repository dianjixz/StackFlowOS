"""M9: validate ``project_id`` before joining it into ``projects_root``.

``<projects_root>/<project_id>/workspace`` is the dynamic boundary used by
``fs.*``/``shell.exec`` risk. A hostile or stale ``project_id`` (``../../etc``,
a NUL, a path separator) used to redefine that boundary silently. Every join
now goes through :func:`stackflowos.security.capabilities.project_workspace`,
which rejects anything that is not one safe path segment with a
``VALIDATION`` (400) error.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from stackflowos.app import App
from stackflowos.errors import ValidationError
from stackflowos.kernel import Context
from stackflowos.l2.eventlog import EventLog
from stackflowos.security import (
    ApprovalBroker,
    PolicyEngine,
    is_within,
    project_workspace,
)

#: Ids a scope/rule/RPC payload could carry that must never reach a join.
INVALID_PROJECT_IDS = (
    "",
    ".",
    "..",
    "../etc",
    "p-1/../../x",
    "p-1/..",
    "p-1/../..",
    "/",
    "\\",
    "p-1\\..\\x",
    "p-1\x00",
    "p-1\n",
    "a b",
    " p-1",
    "p-1 ",
    ".p-1",
    "-p-1",
    "_p-1",
    "p-1/",
    "p-2/workspace",
    "a" * 65,
    "p-1?",
    "p-1*",
    None,
    7,
    b"p-1",
)

#: Inside ``PolicyEngine.risk``/``ApprovalBroker._risk_for`` a ``None`` id
#: simply means "no project scope" (the legacy projects-root boundary); every
#: *non-None* invalid value must raise.
INVALID_SCOPE_IDS = tuple(pid for pid in INVALID_PROJECT_IDS if pid is not None)


class ProjectWorkspaceHelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.root = self.base / "projects"
        self.workspace = self.root / "p-1" / "workspace"
        self.workspace.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_valid_id_resolves_to_the_workspace_inside_root(self):
        ws = project_workspace(self.root, "p-1")
        self.assertEqual(ws, self.workspace.resolve())
        self.assertEqual(ws.parent.parent, self.root.resolve())
        self.assertTrue(is_within(ws, self.root))

    def test_workspace_need_not_exist_yet(self):
        ws = project_workspace(self.root, "not-created")
        self.assertEqual(ws, (self.root / "not-created" / "workspace").resolve())
        self.assertFalse(ws.exists())

    def test_valid_id_shapes(self):
        for pid in ("p-1", "A", "0", "a.b_c-d", "P-1.2", "a" * 64):
            with self.subTest(project_id=pid):
                self.assertEqual(project_workspace(self.root, pid),
                                 (self.root / pid / "workspace").resolve())

    def test_invalid_ids_raise_validation(self):
        for pid in INVALID_PROJECT_IDS:
            with self.subTest(project_id=pid):
                with self.assertRaises(ValidationError) as caught:
                    project_workspace(self.root, pid)
                self.assertEqual(caught.exception.code, "VALIDATION")
                self.assertEqual(caught.exception.http_status, 400)
                self.assertEqual(str(caught.exception), "invalid project id")

    def test_symlinked_workspace_escaping_root_is_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        evil = self.root / "p-link"
        evil.mkdir()
        (evil / "workspace").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValidationError) as caught:
            project_workspace(self.root, "p-link")
        self.assertEqual(caught.exception.code, "VALIDATION")


class ProjectScopeRiskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.projects_root = self.root / "projects"
        self.workspace = self.projects_root / "p-1" / "workspace"
        self.workspace.mkdir(parents=True)
        self.ctx = Context(name="test")
        self.eventlog = EventLog(self.root / "events")
        self.policy = PolicyEngine(
            self.ctx, self.eventlog,
            rules_path=self.root / "security" / "rules.json",
            projects_root=self.projects_root)
        self.broker = ApprovalBroker(
            self.ctx, self.policy, self.eventlog,
            approvals_path=self.root / "security" / "approvals.json",
            approval_ttl_s=300, grant_ttl_s=3600)

    async def asyncTearDown(self):
        await self.ctx.dispose()
        await self.eventlog.close()
        self._tmp.cleanup()

    def test_valid_project_id_keeps_workspace_boundary(self):
        inside = self.workspace / "src" / "a.py"
        outside = self.projects_root / "outside.txt"
        self.assertEqual(
            self.policy.risk("fs.write",
                             {"project_id": "p-1", "path": str(inside)}),
            "medium")
        self.assertEqual(
            self.policy.risk("fs.write",
                             {"project_id": "p-1", "path": "/etc/passwd"}),
            "high")
        # Outside the workspace but inside the projects root.
        self.assertEqual(
            self.policy.risk("fs.write",
                             {"project_id": "p-1", "path": str(outside)}),
            "high")
        # No project scope keeps the legacy projects-root boundary.
        self.assertEqual(
            self.policy.risk("fs.write", {"path": str(outside)}), "medium")
        self.assertEqual(
            self.policy.risk("fs.write", {"project_id": None,
                                          "path": str(outside)}), "medium")

    def test_invalid_project_id_raises_instead_of_moving_the_boundary(self):
        for pid in INVALID_SCOPE_IDS:
            with self.subTest(project_id=pid):
                with self.assertRaises(ValidationError) as caught:
                    self.policy.risk("fs.write",
                                     {"project_id": pid,
                                      "path": "/etc/passwd"})
                self.assertEqual(caught.exception.code, "VALIDATION")
                self.assertEqual(caught.exception.http_status, 400)
        # The spec vector: a traversal id with an absolute path must be a 400,
        # never a silently-computed (e.g. "medium") risk.
        with self.assertRaises(ValidationError):
            self.policy.risk("fs.write",
                             {"project_id": "../../etc",
                              "path": "/etc/passwd"})

    def test_policy_evaluate_rejects_invalid_project_id(self):
        with self.assertRaises(ValidationError):
            self.policy.evaluate(
                "agent.coder.1", "fs.write",
                {"project_id": "../../etc", "path": "/etc/passwd"})

    def test_broker_risk_for_uses_the_same_boundary(self):
        inside = self.workspace / "src" / "a.py"
        outside = self.projects_root / "outside.txt"
        self.assertEqual(
            self.broker._risk_for("fs.write", {"path": str(inside)}, "p-1"),
            "medium")
        self.assertEqual(
            self.broker._risk_for("fs.write", {"path": str(outside)}, "p-1"),
            "high")
        for pid in INVALID_SCOPE_IDS:
            with self.subTest(project_id=pid):
                with self.assertRaises(ValidationError) as caught:
                    self.broker._risk_for("fs.write", {"path": "/etc/passwd"},
                                          pid)
                self.assertEqual(caught.exception.code, "VALIDATION")

    def test_broker_request_rejects_invalid_scope_project_id(self):
        with self.assertRaises(ValidationError):
            self.broker.request(
                "agent-coder-1", "fs.write",
                {"project_id": "../../etc", "path": "/etc/passwd"},
                reason="traversal")

    def test_generalise_scope_never_widens_with_an_invalid_id(self):
        inside = str(self.workspace / "src" / "a.py")
        self.assertEqual(
            self.broker._workspace_root(inside, "p-1"),
            str(self.workspace.resolve()))
        for pid in ("../../etc", "..", "p-1/../../x", "p-1\x00"):
            with self.subTest(project_id=pid):
                self.assertIsNone(self.broker._workspace_root(inside, pid))


class PurgeWorkspacePathTests(unittest.TestCase):
    """``App._purge_workspace`` must validate the record id before deleting."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve() / "projects"
        self.root.mkdir()
        self.app = SimpleNamespace(
            paths=SimpleNamespace(projects_dir=self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def _record(self, project_id: str, *, external: bool = False):
        return SimpleNamespace(id=project_id, external=external)

    def test_hostile_record_ids_are_rejected_not_resolved(self):
        outside = self.root.parent / "keep-me"
        outside.mkdir()
        for pid in ("..", "../keep-me", "p-1\x00", "p-1/../../x", "a b"):
            with self.subTest(project_id=pid):
                with self.assertRaises(ValidationError):
                    App._purge_workspace(self.app, self._record(pid))
        self.assertTrue(outside.is_dir())

    def test_valid_record_purges_only_its_own_directory(self):
        doomed = self.root / "p-9"
        (doomed / "workspace").mkdir(parents=True)
        keep = self.root / "p-10"
        keep.mkdir()
        self.assertTrue(App._purge_workspace(self.app, self._record("p-9")))
        self.assertFalse(doomed.exists())
        self.assertTrue(keep.exists())

    def test_external_workspace_is_never_purged(self):
        with self.assertRaises(ValidationError):
            App._purge_workspace(self.app,
                                 self._record("p-9", external=True))


if __name__ == "__main__":
    unittest.main()

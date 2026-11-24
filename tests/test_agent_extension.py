"""Agents extension: queue routes and project-git routes (build(app)+routes())."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from stackflowos.agents.extension import AgentExtension, build
from stackflowos.agents.queue import QueueEntry
from stackflowos.agents.supervisor import AgentSupervisor
from stackflowos.agents.workspace_git import WorkspaceGit, git_available
from stackflowos.config import AgentsCfg, SecurityCfg
from stackflowos.errors import BadRequest, Denied, NotFound
from stackflowos.kernel.context import Context
from stackflowos.web.api import WebAPI

TMP_ROOT = Path(__file__).resolve().parent.parent / ".stackflowos" / "tmp"


class FakeEventLog:
    def __init__(self) -> None:
        self.records: list[tuple[tuple, dict]] = []

    def append(self, *args, **kwargs):
        self.records.append((args, kwargs))


class FakeProjects:
    def __init__(self) -> None:
        self.items: dict[str, object] = {}

    def get(self, project_id: str):
        try:
            return self.items[project_id]
        except KeyError:
            raise NotFound(f"project not found: {project_id}") from None


def _make_supervisor(tmp: Path) -> AgentSupervisor:
    class Allocator:
        n = 0

        def new_work_id(self, kind):
            Allocator.n += 1
            return f"{kind}.{Allocator.n}"

        def allocate(self, work_id):
            return SimpleNamespace(rpc="ipc://x", events="ipc://y")

        def release(self, work_id):
            pass

    class Registry:
        def get(self, work_id):
            return SimpleNamespace(status="online")

    class Proc:
        pid = 8_000_000
        returncode = 0

        def poll(self):
            return 0

    return AgentSupervisor(
        Context(), registry=Registry(), allocator=Allocator(),
        eventlog=FakeEventLog(), config=AgentsCfg(max_workers=1, queue_max=4),
        sys_rpc="ipc://sys.rpc", state_dir=tmp / "state", root=tmp,
        popen_factory=lambda command, kwargs: Proc(), spawn_timeout_s=1.0)


class AgentExtensionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sfo-ext-", dir="/tmp"))
        (self.tmp / "state").mkdir()
        self.supervisor = _make_supervisor(self.tmp)
        self.projects = FakeProjects()
        self.app = SimpleNamespace(
            supervisor=self.supervisor, projects=self.projects,
            config=SimpleNamespace(security=SecurityCfg(workspace_git=True)),
            eventlog=FakeEventLog(), log=logging.getLogger("test.extensions"))
        self.ext = build(self.app)

    async def asyncTearDown(self) -> None:
        await self.supervisor.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _queue_one(self, *, role: str = "reviewer", task_id: str = "t-2") -> str:
        agent = self.supervisor.register(role=role, project_id="p-1",
                                         unit_id="", task_id=task_id,
                                         status="queued")
        entry = self.supervisor.queue.put(QueueEntry(
            agent_id=agent.id, role=role, project_id="p-1",
            task_id=task_id, priority=70))
        return entry.id

    async def test_build_and_routes_contract(self) -> None:
        self.assertIsInstance(self.ext, AgentExtension)
        routes = self.ext.routes()
        methods = {(method, pattern) for method, pattern, _handler in routes}
        self.assertIn(("GET", r"/api/agents/queue"), methods)
        self.assertIn(("POST", r"/api/agents/queue/([^/]+)/cancel"), methods)
        self.assertIn(("GET", r"/api/projects/([^/]+)/git"), methods)
        self.assertIn(("POST", r"/api/projects/([^/]+)/git/rollback"),
                      methods)

    async def test_queue_list_and_cancel_handlers(self) -> None:
        queue_id = self._queue_one()
        status, payload = await self.ext.queue_list(
            SimpleNamespace(group=lambda _n: None), {}, {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["count"], 1)
        self.assertEqual(payload["data"]["queued"][0]["id"], queue_id)

        status, payload = await self.ext.queue_cancel(
            SimpleNamespace(group=lambda _n: queue_id), {},
            {"reason": "test"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["canceled"])
        self.assertEqual(len(self.supervisor.queue), 0)
        with self.assertRaises(NotFound):
            await self.ext.queue_cancel(
                SimpleNamespace(group=lambda _n: "q-404"), {}, {})

    @unittest.skipUnless(git_available(), "git binary is not available")
    async def test_project_git_get_and_rollback(self) -> None:
        workspace = self.tmp / "workspace"
        workspace.mkdir()
        (workspace / "a.txt").write_text("one\n", encoding="utf-8")
        git = WorkspaceGit(workspace)
        git.ensure_repo()
        first = git.commit_task("t-1")
        (workspace / "a.txt").write_text("two\n", encoding="utf-8")
        git.commit_task("t-1")
        self.projects.items["p-1"] = SimpleNamespace(
            id="p-1", workspace=str(workspace), workspace_mode="attached-rw")

        status, payload = await self.ext.git.project_git(
            SimpleNamespace(group=lambda _n: "p-1"), {}, {})
        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertTrue(data["repo"])
        self.assertTrue(data["branch"])
        self.assertTrue(data["commits"])
        self.assertEqual(data["workspace_mode"], "attached-rw")

        status, payload = await self.ext.git.project_git_rollback(
            SimpleNamespace(group=lambda _n: "p-1"), {},
            {"ref": first["commit"]})
        self.assertEqual(status, 200)
        result = payload["data"]
        self.assertEqual(result["commit"], first["commit"])
        self.assertTrue(result["backup_branch"])
        self.assertEqual((workspace / "a.txt").read_text("utf-8"), "one\n")

    @unittest.skipUnless(git_available(), "git binary is not available")
    async def test_rollback_denied_for_read_only_workspace(self) -> None:
        workspace = self.tmp / "ro"
        workspace.mkdir()
        WorkspaceGit(workspace).ensure_repo()
        self.projects.items["p-1"] = SimpleNamespace(
            id="p-1", workspace=str(workspace), workspace_mode="attached-ro")
        with self.assertRaises(Denied):
            await self.ext.git.project_git_rollback(
                SimpleNamespace(group=lambda _n: "p-1"), {}, {"ref": "HEAD"})

    async def test_rollback_requires_a_ref(self) -> None:
        workspace = self.tmp / "ws"
        workspace.mkdir()
        self.projects.items["p-1"] = SimpleNamespace(
            id="p-1", workspace=str(workspace), workspace_mode="attached-rw")
        with self.assertRaises(BadRequest):
            await self.ext.git.project_git_rollback(
                SimpleNamespace(group=lambda _n: "p-1"), {}, {})

    async def test_webapi_registers_extension_routes(self) -> None:
        self.app.extensions = [self.ext]
        api = WebAPI(self.app)
        status, payload = await api.handle(
            "GET", "/api/agents/queue", {}, b"")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["count"], 0)
        queue_id = self._queue_one()
        status, payload = await api.handle(
            "POST", f"/api/agents/queue/{queue_id}/cancel", {},
            json.dumps({"reason": "web"}).encode("utf-8"))
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["canceled"])
        # Unknown queued id maps to a 404 through the SfoError contract.
        status, payload = await api.handle(
            "POST", "/api/agents/queue/q-404/cancel", {}, b"{}")
        self.assertEqual(status, 404)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

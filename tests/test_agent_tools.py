"""Tests for the local tool executor (approval, paths, shell, sys wrappers)."""

from __future__ import annotations

import copy
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stackflowos.agents.models import Agent
from stackflowos.agents.tools import (ToolError, ToolExecutor, get_tool,
                                      tools_for)
from stackflowos.config import SecurityCfg
from stackflowos.errors import BadRequest

TMP_ROOT = Path(__file__).resolve().parent.parent / ".stackflowos" / "tmp"


class FakeSysAPI:
    """Records calls and returns canned approvals; no transport involved."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.events: list[dict] = []
        self.responses: dict[str, object] = {}
        self.errors: dict[str, Exception] = {}
        self.approval: dict = {
            "id": "apr-default",
            "status": "approved",
            "mode": "rule",
            "risk": "low",
            "grant_id": None,
        }

    async def call(self, action: str, data: dict | None = None, *,
                   timeout: float | None = None) -> dict:
        payload = dict(data or {})
        self.calls.append((action, payload))
        if action in self.errors:
            raise self.errors[action]
        if action == "event.append":
            self.events.append(payload)
            return {"seq": len(self.events)}
        if action in self.responses:
            response = self.responses[action]
            if callable(response):
                return response(payload)
            return copy.deepcopy(response)
        if action in ("approval.request", "approval.status"):
            return dict(self.approval)
        if action == "approval.use":
            return {"ok": True}
        return {}

    def actions(self, name: str) -> list[dict]:
        return [data for action, data in self.calls if action == name]

    def event_types(self) -> list[str]:
        return [str(event.get("type")) for event in self.events]


class ToolExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        self.ws = self.tmp / "workspace"
        self.ws.mkdir()
        self.sys = FakeSysAPI()
        self.agent = Agent(id="agent-coder-1", role="coder",
                           project_id="p-1", task_id="t-1")
        self.security = SecurityCfg()
        self.executor = ToolExecutor(
            self.sys, agent=self.agent, workspace=self.ws,
            security_config=self.security)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- path confinement -------------------------------------------------
    async def test_path_escape_rejected_before_any_rpc(self) -> None:
        with self.assertRaises(ToolError) as caught:
            self.executor.check_path("../outside.txt")
        self.assertIn("escapes workspace", str(caught.exception))

        result = await self.executor.run("fs.read", {"path": "../secret.txt"})
        self.assertFalse(result.ok)
        self.assertIn("escapes workspace", result.error or "")
        self.assertEqual(self.sys.calls, [], "escape must fail before any RPC")

        checked = self.executor.check_path("notes/a.md")
        self.assertTrue(str(checked).startswith(str(self.ws)))

    async def test_must_exist(self) -> None:
        with self.assertRaises(ToolError):
            self.executor.check_path("missing.txt", must_exist=True)

    # -- approval ---------------------------------------------------------
    async def test_denied_approval_returns_tool_result(self) -> None:
        self.sys.approval = {"id": "apr-x", "status": "denied",
                             "mode": None, "risk": "high", "grant_id": None}
        result = await self.executor.run("fs.list", {"path": "."})
        self.assertFalse(result.ok)
        self.assertTrue((result.error or "").startswith("DENIED:"))
        self.assertIn("tool.denied", self.sys.event_types())
        self.assertIn("tool.invoked", self.sys.event_types())

    async def test_pending_approval_polls_and_uses_grant(self) -> None:
        self.sys.responses["approval.request"] = {
            "id": "apr-9", "status": "pending", "mode": None, "risk": "low"}
        self.sys.responses["approval.status"] = lambda data: {
            "id": "apr-9", "status": "approved", "mode": "once",
            "grant_id": "grant-9"}
        result = await self.executor.run("fs.list", {"path": "."})
        self.assertTrue(result.ok)
        self.assertEqual(len(self.sys.actions("approval.status")), 1)
        self.assertEqual(self.sys.actions("approval.use"),
                         [{"grant_id": "grant-9"}])

    async def test_auto_approval_does_not_use_grant(self) -> None:
        result = await self.executor.run("fs.list", {"path": "."})
        self.assertTrue(result.ok)
        self.assertEqual(self.sys.actions("approval.use"), [])

    async def test_approval_request_carries_principal_and_scope(self) -> None:
        await self.executor.run("fs.write", {"path": "a.txt", "content": "x"})
        requests = self.sys.actions("approval.request")
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request["principal"], "agent-coder-1")
        self.assertEqual(request["capability"], "fs.write")
        self.assertEqual(request["project_id"], "p-1")
        self.assertEqual(request["task_id"], "t-1")
        self.assertEqual(request["scope"]["path"], str(self.ws / "a.txt"))
        self.assertEqual(request["scope"]["bytes"], 1)
        self.assertGreaterEqual(request["ttl_s"], 10)

    # -- filesystem -------------------------------------------------------
    async def test_fs_write_read_list_and_delete(self) -> None:
        written = await self.executor.run(
            "fs.write", {"path": "notes/deep/a.md", "content": "# 你好\n"})
        self.assertTrue(written.ok, written.error)
        self.assertEqual(written.data["bytes"], len("# 你好\n".encode()))
        target = self.ws / "notes" / "deep" / "a.md"
        self.assertTrue(target.exists())

        read = await self.executor.run("fs.read", {"path": "notes/deep/a.md"})
        self.assertTrue(read.ok)
        self.assertEqual(read.data["content"], "# 你好\n")
        self.assertFalse(read.data["truncated"])

        listed = await self.executor.run("fs.list", {"path": "notes"})
        self.assertTrue(listed.ok)
        names = {entry["name"]: entry["type"] for entry in listed.data["entries"]}
        self.assertEqual(names, {"deep": "dir"})

        deleted = await self.executor.run("fs.delete", {"path": "notes/deep/a.md"})
        self.assertTrue(deleted.ok)
        self.assertFalse(target.exists())

    async def test_fs_read_truncates(self) -> None:
        (self.ws / "big.txt").write_text("x" * 50, encoding="utf-8")
        result = await self.executor.run("fs.read", {"path": "big.txt",
                                                     "max_bytes": 10})
        self.assertTrue(result.ok)
        self.assertTrue(result.data["truncated"])
        self.assertEqual(result.data["content"], "x" * 10)

    async def test_write_preview_redacts_inline_secret(self) -> None:
        result = await self.executor.run(
            "fs.write", {"path": "conf.txt", "content": "token=abc123secret\n"})
        self.assertTrue(result.ok)
        self.assertIn("***", result.preview)
        self.assertNotIn("abc123secret", result.preview)

    # -- shell ------------------------------------------------------------
    async def test_shell_success_and_env_whitelist(self) -> None:
        env_patch = {"SFO_LLM_API_KEY": "top-secret",
                     "MY_TOKEN": "tok-123",
                     "PATH": os.environ.get("PATH", "/usr/bin")}
        with mock.patch.dict(os.environ, env_patch, clear=False):
            result = await self.executor.run("shell.exec", {
                "command": (
                    f'{sys.executable} -c "import os;'
                    "print(os.environ.get('SFO_LLM_API_KEY','MISSING'));"
                    "print(os.environ.get('MY_TOKEN','MISSING'));"
                    "print('PATH' in os.environ)\"")
            })
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["exit_code"], 0)
        lines = result.data["stdout"].strip().splitlines()
        self.assertEqual(lines[:3], ["MISSING", "MISSING", "True"])
        log_path = Path(result.data["log"])
        self.assertTrue(log_path.exists())
        self.assertTrue(str(log_path).startswith(str(self.ws / ".artifacts")))

    async def test_shell_timeout_kills_process(self) -> None:
        result = await self.executor.run("shell.exec", {
            "command": f'{sys.executable} -c "import time; time.sleep(5)"',
            "timeout_s": 0.3,
        })
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.data["timed_out"])
        self.assertNotEqual(result.data["exit_code"], 0)

    async def test_shell_disabled_by_config(self) -> None:
        executor = ToolExecutor(
            self.sys, agent=self.agent, workspace=self.ws,
            security_config=SecurityCfg(allow_shell=False))
        result = await executor.run("shell.exec", {"command": "echo hi"})
        self.assertFalse(result.ok)
        self.assertIn("disabled", result.error or "")
        self.assertEqual(self.sys.calls, [])

    async def test_net_fetch_rejects_non_http(self) -> None:
        result = await self.executor.run(
            "net.fetch", {"url": "file:///etc/passwd"})
        self.assertFalse(result.ok)
        self.assertIn("http", result.error or "")
        self.assertEqual(self.sys.calls, [])

    # -- events / wrappers ------------------------------------------------
    async def test_events_recorded_in_order(self) -> None:
        await self.executor.run("fs.read", {"path": "missing.txt"})
        # fs.read path exists check happens at execution time, after invoked.
        types = self.sys.event_types()
        self.assertEqual(types[0], "tool.invoked")
        self.assertIn(types[-1], ("tool.failed", "tool.result"))

        self.sys.events.clear()
        await self.executor.run("fs.list", {"path": "."})
        types = self.sys.event_types()
        self.assertEqual(types, ["tool.invoked", "tool.result"])
        self.assertEqual(self.sys.events[-1]["data"]["tool"], "fs.list")

    async def test_unknown_tool_raises_tool_error(self) -> None:
        with self.assertRaises(ToolError):
            await self.executor.run("fs.nope", {})
        self.assertEqual(self.sys.calls, [])
        with self.assertRaises(BadRequest):
            get_tool("fs.nope")

    async def test_task_and_project_wrappers(self) -> None:
        await self.executor.run("task.update",
                                {"task_id": "t-1", "patch": {"status": "doing"}})
        await self.executor.run("task.artifact", {
            "task_id": "t-1",
            "artifact": {"type": "file", "path": "a.md", "title": "A"}})
        await self.executor.run("task.note",
                                {"task_id": "t-1", "text": "进度说明"})
        await self.executor.run("task.create", {
            "project_id": "p-1", "title": "新任务", "role": "coder",
            "priority": 2})
        await self.executor.run("agent.spawn",
                                {"role": "coder", "project_id": "p-1"})
        await self.executor.run("project.update", {
            "project_id": "p-1", "patch": {"brief": "ok"}})
        await self.executor.run("event.query", {"project_id": "p-1",
                                                "since": 3, "limit": 5})

        self.assertEqual(self.sys.actions("task.update")[0]["task_id"], "t-1")
        self.assertEqual(self.sys.actions("task.update")[0]["patch"],
                         {"status": "doing"})
        self.assertEqual(self.sys.actions("task.artifact")[0]["artifact"]["path"],
                         "a.md")
        self.assertEqual(self.sys.actions("task.note")[0]["text"], "进度说明")
        self.assertEqual(self.sys.actions("task.note")[0]["author"],
                         "agent-coder-1")
        create = self.sys.actions("task.create")[0]
        self.assertEqual(create["project_id"], "p-1")
        self.assertEqual(create["priority"], 2)
        spawn = self.sys.actions("agent.spawn")[0]
        self.assertEqual(spawn["role"], "coder")
        self.assertEqual(self.sys.actions("project.update")[0]["patch"],
                         {"brief": "ok"})
        self.assertEqual(self.sys.actions("event.since")[0],
                         {"seq": 3, "limit": 5, "project_id": "p-1"})

    async def test_tools_for_roles(self) -> None:
        coder_tools = {spec.name for spec in tools_for("coder")}
        self.assertIn("fs.write", coder_tools)
        self.assertIn("shell.exec", coder_tools)
        self.assertNotIn("project.update", coder_tools)
        lead_tools = {spec.name for spec in tools_for("lead")}
        self.assertIn("agent.spawn", lead_tools)
        self.assertIn("task.create", lead_tools)
        self.assertNotIn("shell.exec", lead_tools)
        self.assertEqual(tools_for("nobody"), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

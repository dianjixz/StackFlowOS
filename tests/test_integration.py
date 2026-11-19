"""End-to-end integration tests: a real hub, real worker processes.

These are slower than the unit tests (each spawn is a Python process), so they
only assert the properties that unit tests cannot: the wire protocol actually
carries unit registration, leases, approvals, task transitions and artifacts,
over both IPC and TCP.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from stackflowos.app import App
from stackflowos.cli import seed_demo
from stackflowos.config import Config, default_root
from stackflowos.errors import Unavailable
from stackflowos.web.server import WebServer

REPO = default_root()


def _tmpdir() -> str:
    base = REPO / ".stackflowos" / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix="it-", dir=str(base))


async def _boot_app(tmp: str, *, transport: str = "ipc",
                    port_pool: tuple[int, int] | None = None) -> App:
    # Keep the state dir shallow: UNIX socket paths have a ~100 byte limit.
    cfg = Config.load(root=Path(tmp), state_dir=Path(tmp))
    patch: dict = {}
    if transport != cfg.transport.mode:
        patch["transport"] = {"mode": transport}
    if port_pool:
        patch.setdefault("transport", {}).update({
            "port_pool_start": port_pool[0], "port_pool_end": port_pool[1]})
    if patch:
        cfg = cfg.update(patch)
    app = App(cfg)
    await app.start()
    return app


async def _wait_project(app: App, project_id: str, *, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        card = app.project_card(project_id)
        if card.get("status") in ("review", "done"):
            return card
        await asyncio.sleep(0.5)
    card = app.project_card(project_id)
    raise AssertionError(f"project did not reach review: {json.dumps(card, ensure_ascii=False)}")


async def _wait_chat_reply(app: App, *, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        history = app.chat.history(20)
        if history and history[-1]["role"] == "assistant":
            return history[-1]["text"]
        await asyncio.sleep(0.2)
    raise AssertionError("assistant did not reply")


class IntegrationTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = _tmpdir()
        self.app: App | None = None
        self.server: WebServer | None = None

    async def asyncTearDown(self):
        if self.server is not None:
            await self.server.stop()
        if self.app is not None:
            await self.app.stop()
        # Remove the temp tree only after the hub has released its sockets.
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_ipc_project_runs_end_to_end(self):
        self.app = await _boot_app(self.tmp, transport="ipc")
        card = await seed_demo(self.app,
                               name="集成：Markdown 工具",
                               goal="构建一个把 Markdown 转成 HTML 的命令行工具，包含实现、测试和文档")
        project_id = card["id"]
        final = await _wait_project(self.app, project_id, timeout=120)
        self.assertIn(final["status"], ("review", "done"))
        self.assertEqual(final["counts"]["done"], final["total_tasks"])
        # A real converter artifact exists and the generated program passes
        # its own selftest.
        workspace = Path(final["workspace"])
        sources = list((workspace / "src").glob("*.py"))
        self.assertTrue(sources, "no Python artifact produced")
        converter = next((p for p in sources if "markdown_to_html" in p.read_text("utf-8")), None)
        self.assertIsNotNone(converter, "coder did not produce the converter")
        proc = await asyncio.create_subprocess_exec(
            "python3", str(converter), "--selftest",
            cwd=str(workspace), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
        self.assertEqual(proc.returncode, 0, err.decode())
        self.assertIn(b"selftest ok", out)
        # Every task has at least one artifact or a review record.
        tasks = self.app.projects.task_list(project_id=project_id)
        self.assertTrue(all(t.status == "done" for t in tasks))
        # Human confirmation moves the project to done and stops its agents.
        completed = await self.app.complete_project(project_id)
        self.assertEqual(completed["status"], "done")
        active = [a for a in self.app.supervisor.list(project_id=project_id)
                  if a.status not in ("done", "failed", "dead")]
        self.assertEqual(active, [])

    async def test_tcp_transport_runs_end_to_end(self):
        self.app = await _boot_app(self.tmp, transport="tcp",
                                   port_pool=(18120, 18170))
        self.assertEqual(self.app.config.transport.mode, "tcp")
        self.assertTrue(self.app.sys_addresses.rpc.startswith("tcp://"))
        card = await seed_demo(self.app, name="集成：TCP",
                               goal="做一个 TCP 模式下可用的命令行计算器")
        final = await _wait_project(self.app, card["id"], timeout=90)
        self.assertIn(final["status"], ("review", "done"))
        self.assertGreater(files_count(final["workspace"]), 0)

    async def test_hub_lock_prevents_second_hub(self):
        self.app = await _boot_app(self.tmp, transport="ipc")
        second = App(Config.load(root=Path(self.tmp), state_dir=Path(self.tmp)))
        with self.assertRaises(Unavailable):
            await second.start()
        await second.stop()

    async def test_approval_flow_through_sys_actions(self):
        self.app = await _boot_app(self.tmp, transport="ipc")
        request = await self.app.handle_rpc_action("approval.request", {
            "principal": "coder.probe",
            "capability": "fs.write",
            "scope": {"path": "/etc/stackflowos-probe"},
            "reason": "integration test",
            "preview": "write outside the workspace",
        }, src="coder.probe")
        self.assertEqual(request["status"], "pending")
        self.assertIn(request["risk"], ("medium", "high"))
        decided = await self.app.decide_approval(request["id"], decision="allow",
                                                 mode="once", note="it-test")
        self.assertEqual(decided["status"], "approved")
        self.assertTrue(decided["grant_id"])
        self.assertTrue(self.app.approvals.use(decided["grant_id"]))
        self.assertFalse(self.app.approvals.use(decided["grant_id"]))

        denied = await self.app.handle_rpc_action("approval.request", {
            "principal": "coder.probe",
            "capability": "shell.exec",
            "scope": {"cwd": "/", "command": "rm -rf /"},
            "reason": "should not be allowed",
        }, src="coder.probe")
        self.assertIn(denied["status"], ("pending", "denied"))

    async def test_chat_assistant_status_and_project_creation(self):
        self.app = await _boot_app(self.tmp, transport="ipc")
        await self.app.chat.send_user("现在的进度怎么样")
        reply = await _wait_chat_reply(self.app)
        self.assertTrue(reply)
        await self.app.chat.send_user("创建一个项目：做一个待办清单命令行工具")
        deadline = time.monotonic() + 30
        project = None
        while time.monotonic() < deadline:
            projects = self.app.projects.list()
            if projects:
                project = projects[0]
                break
            await asyncio.sleep(0.2)
        self.assertIsNotNone(project, "chat did not create a project")
        self.assertGreaterEqual(len(self.app.projects.task_list(project_id=project.id)), 4)
        reply = await _wait_chat_reply(self.app)
        self.assertIn("项目", reply)

    async def test_failure_domain_resets_dead_worker(self):
        import signal

        self.app = await _boot_app(self.tmp, transport="ipc")
        # Speed up liveness detection for the test.
        self.app.registry.offline_after_s = 2.0
        project = await self.app.create_project("故障域", "做一个能被测出崩溃恢复的工具",
                                                spawn_lead=False)
        task = self.app.projects.task_list(project_id=project["id"])[0]
        agent = await self.app.spawn_agent("coder", project["id"], task.id)
        unit_id = agent["unit_id"]
        # The coder will block on an fs.write approval (no rules seeded), so
        # the process is reliably alive while we kill it.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.app.approvals.list(status="pending"):
                break
            await asyncio.sleep(0.2)
        unit = self.app.registry.get(unit_id)
        self.assertEqual(unit.status, "online")
        self.assertTrue(self.app.leases.list(holder=unit_id),
                        "the unit should hold an identity lease")
        os.kill(int(unit.pid), signal.SIGKILL)

        deadline = time.monotonic() + 20
        saw_offline = False
        while time.monotonic() < deadline:
            try:
                current = self.app.registry.get(unit_id)
                if current.status in ("suspect", "offline"):
                    saw_offline = True
                if current.status == "offline":
                    break
            except Exception:
                saw_offline = True
                break
            await asyncio.sleep(0.3)
        self.assertTrue(saw_offline, "dead unit was never detected")
        self.assertEqual(self.app.leases.list(holder=unit_id), [])
        # The failure policy must have started a replacement within budget.
        deadline = time.monotonic() + 25
        replacement = None
        while time.monotonic() < deadline:
            refreshed = self.app.supervisor.get(agent["id"])
            if refreshed.unit_id and refreshed.unit_id != unit_id:
                replacement = refreshed
                break
            await asyncio.sleep(0.3)
        self.assertIsNotNone(replacement, "failure domain did not restart the worker")
        self.assertIn(replacement.status, ("spawning", "idle", "working", "done", "dead", "failed"))

    async def test_web_api_and_sse(self):
        self.app = await _boot_app(self.tmp, transport="ipc")
        self.server = WebServer(self.app, host="127.0.0.1", port=0)
        await self.server.start()

        def fetch_state():
            with urllib.request.urlopen(f"http://127.0.0.1:{self.server.port}/api/state",
                                        timeout=10) as response:
                return json.loads(response.read().decode())

        payload = await asyncio.to_thread(fetch_state)
        self.assertTrue(payload["ok"])
        self.assertIn("sys", payload["data"])

        def test_llm():
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.server.port}/api/llm/test",
                method="POST", data=b"{}")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=15) as response:
                return json.loads(response.read().decode())

        llm = await asyncio.to_thread(test_llm)
        self.assertTrue(llm["ok"])
        self.assertEqual(llm["data"]["provider"], "mock")

        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
        writer.write(b"GET /api/events/stream HTTP/1.1\r\nHost: t\r\n\r\n")
        await writer.drain()
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        self.assertIn(b"text/event-stream", header)
        self.app.eventlog.append("it.event", {"x": 1}, source="it")
        seen = b""
        for _ in range(5):
            chunk = await asyncio.wait_for(reader.read(4096), 5)
            seen += chunk
            if b"it.event" in seen:
                break
        self.assertIn(b"it.event", seen)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def files_count(workspace: str) -> int:
    root = Path(workspace)
    if not root.exists():
        return 0
    return sum(1 for path in root.rglob("*") if path.is_file())


if __name__ == "__main__":
    unittest.main()

"""M7: full approval previews with hashes, truncation and on-demand detail.

The approval popup used to see only a 4000-char slice, so malicious content
beyond that limit was invisible at decision time.  The broker now stores a
bounded (64KB by default) redacted full preview, its sha256 and the original
byte count, while event payloads and list responses keep only the truncated
display text.  The authenticated ``GET /api/approvals/{id}`` returns the
stored full preview for the "查看完整内容" dialog.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from stackflowos.agents.models import Agent
from stackflowos.app import App
from stackflowos.agents.tools import ToolExecutor
from stackflowos.config import SecurityCfg, default_root
from stackflowos.kernel import Context
from stackflowos.l2 import EventLog
from stackflowos.security import ApprovalBroker, ApprovalRequest, PolicyEngine
from stackflowos.util.jsonio import read_json
from stackflowos.web.server import WebServer

REPO = default_root()
TMP_ROOT = REPO / ".stackflowos" / "tmp"
STORE = SecurityCfg().preview_store_chars


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_broker(directory: Path):
    ctx = Context(name="test")
    eventlog = EventLog(directory / "events")
    policy = PolicyEngine(
        ctx, eventlog,
        rules_path=directory / "state" / "rules.json",
        projects_root=directory / "projects")
    broker = ApprovalBroker(
        ctx, policy, eventlog,
        approvals_path=directory / "state" / "approvals.json",
        approval_ttl_s=300, grant_ttl_s=3600,
        preview_store_chars=STORE)
    return ctx, eventlog, broker


class FakeSysAPI:
    """Records RPC calls; always auto-approves the tool under test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.events: list[dict] = []

    async def call(self, action: str, data: dict | None = None, *,
                   timeout: float | None = None) -> dict:
        payload = dict(data or {})
        self.calls.append((action, payload))
        if action == "event.append":
            self.events.append(payload)
            return {"seq": len(self.events)}
        if action in ("approval.request", "approval.status"):
            return {"id": "apr-1", "status": "approved", "mode": "rule",
                    "risk": "medium", "grant_id": None}
        if action == "approval.use":
            return {"ok": True}
        return {}

    def actions(self, name: str) -> list[dict]:
        return [data for action, data in self.calls if action == name]


class BrokerPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(TMP_ROOT))
        self.dir = Path(self._tmp.name)
        self.ctx, self.eventlog, self.broker = make_broker(self.dir)
        self.events: list[tuple[str, dict]] = []
        self.ctx.on_any(lambda mode, name, payload:
                        self.events.append((name, payload)))

    async def asyncTearDown(self):
        await self.ctx.dispose()
        await self.eventlog.close()
        self._tmp.cleanup()

    def _event_data(self, name: str) -> list[dict]:
        return [payload["data"] for event, payload in self.events
                if event == name]

    def test_large_preview_is_bounded_hashed_and_flagged(self):
        payload = "A" * 100_000
        request = self.broker.request(
            "agent-coder-1", "shell.exec",
            {"cwd": str(self.dir / "workspace"), "command": "make"},
            project_id="p-1", reason="build", preview="display",
            preview_full=payload)
        self.assertEqual(request.status, "pending")
        self.assertEqual(len(request.preview_full), STORE)
        self.assertTrue(request.preview_truncated)
        self.assertEqual(request.preview_bytes, 100_000)
        self.assertEqual(request.preview_sha256, sha256(request.preview_full))
        self.assertEqual(request.preview, "display")

        public = request.to_public_dict()
        self.assertNotIn("preview_full", public)
        self.assertEqual(public["preview"], "display")
        self.assertEqual(public["preview_sha256"], sha256(payload[:STORE]))
        self.assertTrue(public["preview_truncated"])
        self.assertEqual(public["preview_bytes"], 100_000)

        detailed = request.to_public_dict(include_full=True)
        self.assertEqual(detailed["preview_full"], payload[:STORE])

        # The stored record keeps the full preview for the detail endpoint.
        stored = read_json(self.dir / "state" / "approvals.json")
        row = stored["requests"][0]
        self.assertEqual(row["preview_full"], payload[:STORE])

    async def test_app_forwards_full_preview_without_ui_truncation(self):
        fake_app = SimpleNamespace(
            approvals=self.broker, config=SimpleNamespace(security=SecurityCfg()))
        payload = "B" * 100_000
        result = await App._approval_request(fake_app, {
            "principal": "agent-coder-1",
            "capability": "shell.exec",
            "scope": {"cwd": str(self.dir / "workspace"), "command": "make"},
            "preview": "display",
            "preview_full": payload,
        }, "src")
        request = self.broker.get(result["id"])
        self.assertEqual(len(request.preview_full), STORE)
        self.assertTrue(request.preview_truncated)
        self.assertEqual(request.preview, "display")

    def test_small_preview_is_not_truncated(self):
        payload = "写入 notes/a.md\n---\nhello\n"
        request = self.broker.request(
            "agent-coder-1", "fs.write",
            {"path": str(self.dir / "workspace" / "a.md"), "bytes": 6},
            project_id="p-1", preview=payload, preview_full=payload)
        self.assertFalse(request.preview_truncated)
        self.assertEqual(request.preview_full, payload)
        self.assertEqual(request.preview_bytes, len(payload.encode("utf-8")))
        self.assertEqual(request.preview_sha256, sha256(payload))

    def test_event_payload_never_contains_full_preview(self):
        payload = "H" * 100_000 + "-UNIQUE-TAIL-VISIBLE-ONLY-IN-FULL"
        self.broker.request(
            "agent-coder-1", "shell.exec",
            {"cwd": str(self.dir / "workspace"), "command": "make"},
            project_id="p-1", preview="display preview", preview_full=payload)
        data = self._event_data("approval.requested")[-1]
        request = data["request"]
        self.assertNotIn("preview_full", request)
        self.assertEqual(request["preview_sha256"], sha256(payload[:STORE]))
        self.assertTrue(request["preview_truncated"])
        serialized = json.dumps(data, ensure_ascii=False)
        self.assertLess(len(serialized), len(payload))
        self.assertNotIn("-UNIQUE-TAIL-VISIBLE-ONLY-IN-FULL", serialized)
        self.assertNotIn("H" * 10_000, serialized)

    def test_legacy_record_without_new_fields_loads(self):
        legacy = {
            "id": "apr-legacy", "principal": "agent-coder-1",
            "capability": "fs.read", "scope": {"path": "/tmp/x"},
            "risk": "low", "reason": "", "preview": "old preview",
            "status": "approved", "mode": "auto", "created_at": 1,
            "expires_at": 2,
        }
        request = ApprovalRequest.from_dict(legacy)
        self.assertEqual(request.preview_full, "old preview")
        self.assertEqual(request.preview_sha256, sha256("old preview"))
        self.assertFalse(request.preview_truncated)
        self.assertEqual(request.preview_bytes, len(b"old preview"))


class ToolPreviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(TMP_ROOT))
        self.tmp = Path(self._tmp.name)
        self.ws = self.tmp / "workspace"
        self.ws.mkdir()
        self.sys = FakeSysAPI()
        self.agent = Agent(id="agent-coder-1", role="coder",
                           project_id="p-1", task_id="t-1")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _executor(self, config: SecurityCfg | None = None) -> ToolExecutor:
        return ToolExecutor(
            self.sys, agent=self.agent, workspace=self.ws,
            security_config=config or SecurityCfg())

    async def test_fs_write_preview_has_digest_and_both_edges(self):
        content = "\n".join(f"line {i:04d}" for i in range(2000)) + "\n"
        executor = self._executor()
        result = await executor.run("fs.write", {"path": "big.txt",
                                                 "content": content})
        self.assertTrue(result.ok, result.error)
        request = self.sys.actions("approval.request")[0]
        full = request["preview_full"]
        display = request["preview"]

        self.assertIn(f"sha256: {sha256(content)}", full)
        self.assertIn(f"bytes: {len(content.encode('utf-8'))}", full)
        self.assertIn("前 20 行", full)
        self.assertIn("后 20 行", full)
        self.assertIn("已省略", full)
        self.assertIn("line 0000", full)
        self.assertIn("line 1999", full)
        self.assertIn("line 1980", full)
        self.assertNotIn("line 100", full.split("--- 内容 ---", 1)[0])
        # Display text is a truncation of the stored full preview (M7).
        self.assertLessEqual(len(display), 4000)
        self.assertLess(len(display), len(full))
        self.assertEqual(result.preview, display)
        invoked = [e for e in self.sys.events
                   if e.get("type") == "tool.invoked"][-1]
        self.assertEqual(invoked["data"]["preview"], display)

    async def test_fs_write_small_content_is_shown_without_marker(self):
        executor = self._executor()
        await executor.run("fs.write", {"path": "small.txt",
                                        "content": "hello\nworld\n"})
        full = self.sys.actions("approval.request")[0]["preview_full"]
        self.assertIn("hello", full)
        self.assertIn("world", full)
        self.assertNotIn("已省略", full)

    async def test_shell_preview_shows_command_cwd_and_chain(self):
        executor = self._executor()
        await executor.run("shell.exec", {"command": "echo a && echo b",
                                          "cwd": "."})
        full = self.sys.actions("approval.request")[0]["preview_full"]
        self.assertIn("$ echo a && echo b", full)
        self.assertIn(f"(cwd: {self.ws})", full)
        self.assertIn("链式操作符: &&", full)

    async def test_net_preview_shows_normalised_target_and_private_flag(self):
        executor = self._executor(SecurityCfg(allow_private_network=True))
        _, _, preview = executor._prepare(
            "net.fetch", {"url": "http://127.0.0.1:8080/a?b=1"})
        self.assertIn("url: http://127.0.0.1:8080/a?b=1", preview)
        self.assertIn("目标: http://127.0.0.1:8080/a", preview)
        self.assertIn("私网: 是", preview)


class FakeApp:
    """Minimal App surface for the approval detail routes."""

    version = "0.1.0-test"

    def __init__(self, approvals, eventlog: EventLog) -> None:
        self.approvals = approvals
        self.eventlog = eventlog

    def health(self):
        return {"status": "ok", "http": "ok", "transport": "ok",
                "version": self.version, "uptime_s": 1, "units": {"online": 1}}

    def snapshot(self):
        return {"sys": {"name": "TestOS", "version": self.version},
                "units": [], "projects": [], "agents": [], "leases": [],
                "approvals": [], "rules": [], "events": [], "chat": [],
                "config": {"system": {"name": "TestOS"}}, "stats": {}}


class ApprovalDetailApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(TMP_ROOT))
        self.dir = Path(self._tmp.name)
        self.ctx, self.eventlog, self.broker = make_broker(self.dir)
        self.full = "detail-" + "x" * 200_000
        self.request = self.broker.request(
            "agent-coder-1", "shell.exec",
            {"cwd": str(self.dir / "workspace"), "command": "make"},
            project_id="p-1", preview=self.full[:4000],
            preview_full=self.full)
        self.app = FakeApp(self.broker, self.eventlog)
        self.assets = self.dir / "assets"
        (self.assets / "css").mkdir(parents=True)
        (self.assets / "index.html").write_text(
            "<!doctype html><title>SFO</title>", "utf-8")
        self.server = WebServer(self.app, host="127.0.0.1", port=0,
                                assets_dir=self.assets)
        self.url = await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()
        await self.ctx.dispose()
        await self.eventlog.close()
        self._tmp.cleanup()

    @property
    def auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.server.launch_token}"}

    def get(self, path: str) -> tuple[int, dict]:
        request = urllib.request.Request(self.url + path)
        for key, value in self.auth_headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    async def aget(self, path: str) -> tuple[int, dict]:
        return await asyncio.to_thread(self.get, path)

    async def test_list_omits_full_preview(self):
        status, payload = await self.aget("/api/approvals?status=pending")
        self.assertEqual(status, 200, payload)
        item = payload["data"]["approvals"][0]
        self.assertNotIn("preview_full", item)
        self.assertTrue(item["preview_truncated"])
        self.assertEqual(item["preview_sha256"], sha256(self.full[:STORE]))
        self.assertEqual(item["preview_bytes"], len(self.full.encode("utf-8")))

    async def test_detail_returns_full_preview(self):
        status, payload = await self.aget(
            f"/api/approvals/{self.request.id}")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["preview_full"], self.full[:STORE])
        self.assertEqual(payload["data"]["preview"],
                         self.full[:4000])

    def _status_without_auth(self, path: str) -> int:
        request = urllib.request.Request(self.url + path)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    async def test_detail_and_list_require_authentication(self):
        for path in ("/api/approvals", f"/api/approvals/{self.request.id}"):
            with self.subTest(path=path):
                status = await asyncio.to_thread(self._status_without_auth, path)
                self.assertEqual(status, 401)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

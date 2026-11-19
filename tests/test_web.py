"""HTTP/SSE server and routing tests with a fake App.

These do not boot the real hub; they pin the web contract so the UI and the
router cannot drift apart while the backend evolves.
"""
import asyncio
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from stackflowos.config import default_root
from stackflowos.errors import ValidationError
from stackflowos.l2 import EventLog
from stackflowos.web.server import WebServer

REPO = default_root()


class FakeChat:
    def __init__(self):
        self.sent = []

    def history(self, limit=100):
        return [{"id": "m1", "role": "assistant", "text": "hello", "ts": 1}]

    async def send_user(self, text, from_="human:operator"):
        self.sent.append(text)
        return {"id": "m2", "role": "user", "text": text, "ts": 2}


class FakeConfig:
    def public_dict(self):
        return {"system": {"name": "TestOS"}, "web": {"port": 0}}


class FakeApp:
    version = "0.1.0-test"

    def __init__(self, tmp: Path):
        self.eventlog = EventLog(tmp / "events")
        self.chat = FakeChat()
        self.config = FakeConfig()
        self.created = []
        self.decided = []
        self.approvals = self
        self.policy = self
        self.projects = self
        self.registry = self
        self.supervisor = self
        self.leases = self

    def stats(self):
        return {"uptime_s": 5, "units": {"online": 1}, "leases": 0,
                "pending_approvals": 0, "events": self.eventlog.last_seq}

    def snapshot(self):
        return {"sys": {"name": "TestOS", "version": self.version},
                "units": [], "projects": [], "agents": [], "leases": [],
                "approvals": [], "rules": [], "events": [], "chat": self.chat.history(),
                "config": self.config.public_dict(), "stats": self.stats()}

    def console(self, *, types=None, project_id=None, limit=200):
        return [r.to_dict() for r in self.eventlog.tail(limit)]

    async def create_project(self, name, goal, created_by="human:operator"):
        card = {"id": "p-1", "name": name, "goal": goal, "status": "active",
                "progress": 0, "counts": {}, "total_tasks": 0, "agents": [],
                "pending_approvals": 0, "leases": []}
        self.created.append((name, goal))
        return card

    def project_card(self, project_id):
        return {"id": project_id, "name": "Demo", "status": "active", "progress": 0}

    def list(self, *args, **kwargs):
        return []

    def add_rule(self, **kwargs):
        if not kwargs.get("capability"):
            raise ValidationError("capability required")
        class Rule:
            def to_dict(self):
                return {"id": "r-1", **kwargs}
        return Rule()

    def delete_rule(self, rule_id):
        return True

    def list_rules(self, **kwargs):
        return []

    async def decide_approval(self, request_id, **kwargs):
        self.decided.append((request_id, kwargs))
        return {"id": request_id, **kwargs}

    async def update_config(self, patch):
        return {"config": self.config.public_dict(), "restart_required": []}

    def task_list(self, **kwargs):
        return []

    def task_update(self, task_id, patch):
        return type("T", (), {"to_dict": lambda self: {"id": task_id, **patch}})()

    def add_task(self, *args, **kwargs):
        return type("T", (), {"to_dict": lambda self: {"id": "t-1"}})()

    def task_add_note(self, task_id, text, author="operator"):
        return type("T", (), {"to_dict": lambda self: {"id": task_id, "note": text}})()

    def task_check(self, task_id, **kwargs):
        return type("T", (), {"to_dict": lambda self: {"id": task_id, **kwargs}})()

    async def kill_unit(self, work_id):
        return {"accepted": True}

    async def spawn_agent(self, role, project_id=None, task_id=None):
        return {"id": "agent-1", "role": role, "project_id": project_id}

    async def kill_agent(self, agent_id):
        return True

    async def archive_project(self, project_id):
        return {"project": {"id": project_id}, "agents_stopped": 0}

    async def complete_project(self, project_id):
        return {"id": project_id, "status": "done"}


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.app = FakeApp(self.tmp)
        self.assets = self.tmp / "assets"
        (self.assets / "css").mkdir(parents=True)
        (self.assets / "index.html").write_text("<!doctype html><title>SFO</title>", "utf-8")
        (self.assets / "css" / "os.css").write_text("body{background:#000}", "utf-8")
        self.server = WebServer(self.app, host="127.0.0.1", port=0,
                                assets_dir=self.assets)
        self.url = await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()
        await self.app.eventlog.close()
        self._tmp.cleanup()

    async def aget(self, path: str, method="GET", data=None):
        return await asyncio.to_thread(self.get, path, method, data)

    def get(self, path: str, method="GET", data=None):
        req = urllib.request.Request(self.url + path, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
            body = json.dumps(data).encode()
        else:
            body = None
        try:
            with urllib.request.urlopen(req, body, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    async def test_health_and_state(self):
        status, payload = await self.aget("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        status, payload = await self.aget("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["sys"]["name"], "TestOS")
        self.assertIn("projects", payload["data"])

    async def test_static_files_and_traversal(self):
        def fetch(path):
            with urllib.request.urlopen(self.url + path, timeout=5) as resp:
                return resp.read()
        self.assertIn(b"<title>SFO</title>", await asyncio.to_thread(fetch, "/"))
        self.assertIn(b"background", await asyncio.to_thread(fetch, "/css/os.css"))
        status, payload = await self.aget("/api/../../etc/passwd")
        self.assertIn(status, (403, 404))

    async def test_create_project_and_decide(self):
        status, payload = await self.aget("/api/projects", "POST",
                                   {"name": "X", "goal": "do something"})
        self.assertEqual(status, 201)
        self.assertEqual(self.app.created, [("X", "do something")])
        status, payload = await self.aget("/api/approvals/apr-1/decide", "POST",
                                   {"decision": "allow", "mode": "once"})
        self.assertEqual(status, 200)
        self.assertEqual(self.app.decided[0][0], "apr-1")

    async def test_validation_error_shape(self):
        status, payload = await self.aget("/api/rules", "POST", {"effect": "allow"})
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "VALIDATION")

    async def test_bad_json(self):
        def post_bad():
            req = urllib.request.Request(self.url + "/api/chat", method="POST",
                                         data=b"{not json")
            req.add_header("Content-Type", "application/json")
            try:
                urllib.request.urlopen(req, timeout=5)
                return None
            except urllib.error.HTTPError as exc:
                return exc.code
        self.assertEqual(await asyncio.to_thread(post_bad), 400)

    async def test_unknown_route(self):
        status, payload = await self.aget("/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "NOT_FOUND")

    async def test_sse_stream(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
        writer.write(b"GET /api/events/stream HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        self.assertIn(b"200 OK", header)
        self.assertIn(b"text/event-stream", header)
        self.app.eventlog.append("test.event", {"x": 1}, source="test")
        seen = b""
        for _ in range(5):
            chunk = await asyncio.wait_for(reader.read(4096), 5)
            if not chunk:
                break
            seen += chunk
            if b"test.event" in seen:
                break
        self.assertIn(b"test.event", seen)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()

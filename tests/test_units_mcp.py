"""MCP host: stdio framing, lifecycle, units, catalogue and approval gate."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from stackflowos.app import App
from stackflowos.config import Config, default_root
from stackflowos.errors import Timeout
from stackflowos.mcp.client import MCPStdioClient
from stackflowos.security import Principal
from stackflowos.web.api import WebAPI

REPO = default_root()

SERVER_SCRIPT = r'''
import json
import os
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "newline"

def read_message():
    line = sys.stdin.buffer.readline()
    while line and not line.strip():
        line = sys.stdin.buffer.readline()
    if not line:
        return None
    stripped = line.strip()
    if stripped.lower().startswith(b"content-length:"):
        length = int(stripped.split(b":", 1)[1])
        while True:
            header = sys.stdin.buffer.readline()
            if header in (b"\r\n", b"\n", b""):
                break
        body = sys.stdin.buffer.read(length)
        return json.loads(body.decode("utf-8"))
    return json.loads(stripped.decode("utf-8"))

def send(payload):
    body = json.dumps(payload).encode("utf-8")
    if MODE == "content-length":
        sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode())
        sys.stdout.buffer.write(b"\r\n\r\n" + body)
    else:
        sys.stdout.buffer.write(body + b"\n")
    sys.stdout.buffer.flush()

def main():
    if os.environ.get("MCP_TEST_STDERR"):
        print("server log token sk-abcdefghijkl", file=sys.stderr, flush=True)
    while True:
        message = read_message()
        if message is None:
            return
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "1.0.0"},
            }})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [
                {"name": "echo", "description": "echo tool",
                 "inputSchema": {"type": "object",
                                 "properties": {"text": {"type": "string"}}}},
                {"name": "die", "description": "crash the server",
                 "inputSchema": {"type": "object", "properties": {}}},
                {"name": "slow", "description": "slow tool",
                 "inputSchema": {"type": "object", "properties": {}}},
            ]}})
        elif method == "tools/call":
            name = message["params"]["name"]
            arguments = message["params"].get("arguments") or {}
            if name == "die":
                return
            if name == "slow":
                time.sleep(0.6)
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text",
                             "text": json.dumps({"echo": arguments.get("text"),
                                                  "tool": name})}],
                "isError": False,
            }})
        elif request_id is not None:
            send({"jsonrpc": "2.0", "id": request_id,
                  "error": {"code": -32601, "message": "method not found"}})

main()
'''


class MCPClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.script = self.tmp / "mcp_server.py"
        self.script.write_text(SERVER_SCRIPT, "utf-8")

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def client(self, mode="newline", **kwargs):
        return MCPStdioClient("fake", sys.executable, [str(self.script), mode],
                              **kwargs)

    async def test_newline_framing_initialize_list_and_call(self):
        client = self.client("newline", call_timeout_s=2)
        await client.start()
        self.assertEqual(client.state, "ready")
        self.assertEqual(client.server_info["name"], "fake-mcp")
        tools = await client.list_tools()
        self.assertEqual({tool["name"] for tool in tools},
                         {"echo", "die", "slow"})
        result = await client.call_tool("echo", {"text": "hello"})
        text = json.loads(result["content"][0]["text"])
        self.assertEqual(text["echo"], "hello")
        await client.close()
        self.assertEqual(client.alive, False)

    async def test_content_length_framing_roundtrip(self):
        client = self.client("content-length", call_timeout_s=2)
        await client.start()
        self.assertEqual(client.state, "ready")
        tools = await client.list_tools()
        self.assertIn("echo", {tool["name"] for tool in tools})
        result = await client.call_tool("echo", {"text": "length"})
        self.assertEqual(json.loads(result["content"][0]["text"])["echo"],
                         "length")
        await client.close()

    async def test_timeout_is_fail_closed(self):
        client = self.client("newline", call_timeout_s=0.15)
        await client.start()
        try:
            with self.assertRaises(Timeout):
                await client.call_tool("slow", {})
        finally:
            await client.close()

    async def test_stderr_is_captured_and_redacted(self):
        client = self.client("newline", env={"MCP_TEST_STDERR": "1"},
                             call_timeout_s=2)
        await client.start()
        await asyncio.sleep(0.2)
        text = "\n".join(client.stderr_tail)
        self.assertIn("server log", text)
        self.assertNotIn("sk-abcdefghijkl", text)
        self.assertIn("***", text)
        await client.close()

    async def test_process_exit_marks_failed(self):
        client = self.client("newline", call_timeout_s=2)
        await client.start()
        with self.assertRaises(Exception):
            await client.call_tool("die", {})
        await asyncio.sleep(0.1)
        self.assertFalse(client.alive)
        self.assertEqual(client.state, "failed")
        self.assertTrue(client.error)


class MCPHostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.script = self.tmp / "mcp_server.py"
        self.script.write_text(SERVER_SCRIPT, "utf-8")
        cfg = Config.load(root=self.tmp, state_dir=self.tmp / "state")
        cfg = cfg.update({
            "skills": {"dirs": [], "enabled": []},
            "plugins": {"dirs": [], "enabled": []},
            "mcp": {"servers": [{
                "name": "echo", "command": sys.executable,
                "args": [str(self.script), "newline"],
            }], "call_timeout_s": 2.0},
        })
        self.app = App(cfg)
        await self.app.start()
        self.api = WebAPI(self.app)
        self.host = self.app.mcp_host

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    async def api_call(self, method, path, query=None, body=None):
        return await self.api.handle(method, path, query or {},
                                     json.dumps(body or {}).encode("utf-8"))

    def units(self):
        return {unit["unit_id"]: unit for unit in self.app.unit_cards()}

    async def test_server_unit_and_tools_in_catalog(self):
        units = self.units()
        self.assertIn("mcp.echo", units)
        self.assertEqual(units["mcp.echo"]["state"], "ready")
        self.assertEqual(units["mcp.echo"]["runtime"]["mode"], "subprocess")
        self.assertGreater(units["mcp.echo"]["pid"], 0)
        self.assertEqual(units["mcp.echo"]["metadata"]["connection"],
                         "connected")
        self.assertEqual(units["mcp.0"]["state"], "ready")
        entries = {entry["name"]: entry for entry in
                   self.app.capability_catalog.entries(kind="mcp_tool")}
        self.assertIn("echo", entries)
        self.assertEqual(entries["echo"]["provider"], "mcp.echo")
        self.assertTrue(entries["echo"]["available"])
        self.assertEqual(entries["echo"]["risk"], "high")
        status, payload = await self.api_call("GET", "/api/mcp/servers")
        self.assertEqual(status, 200)
        server = payload["data"]["servers"][0]
        self.assertEqual(server["connection"], "connected")
        self.assertNotIn("env", server)
        status, payload = await self.api_call("GET", "/api/mcp/tools")
        self.assertEqual(status, 200)
        self.assertEqual({tool["name"] for tool in payload["data"]["tools"]},
                         {"echo", "die", "slow"})

    async def test_capability_call_dispatches_to_mcp_with_approval_audit(self):
        self.app.policy.add_rule(effect="allow", capability="shell.exec",
                                 principal="coder.1", scope={},
                                 risk_max="high")
        principal = Principal(kind="unit", id="coder.1", project_id=None)
        result = await self.app.capability_catalog.call_capability(
            "echo", {"text": "cat"}, None, principal)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["executed"])
        self.assertEqual(result["provider"], "mcp.echo")
        payload = json.loads(result["result"]["content"][0]["text"])
        self.assertEqual(payload["echo"], "cat")
        events = [record.to_dict() for record in self.app.eventlog.query(
            types=["mcp.tool.called"], limit=20)]
        self.assertTrue(events)
        data = events[-1]["data"]
        self.assertEqual(data["principal"], "coder.1")
        self.assertEqual(data["server"], "echo")
        self.assertTrue(data["approval_id"])

    async def test_unit_principal_cannot_cross_project(self):
        first = self.app.projects.create("First")
        second = self.app.projects.create("Second")
        from stackflowos.errors import Denied
        principal = Principal(kind="unit", id="coder.9",
                              project_id=first.id)
        with self.assertRaises(Denied):
            await self.app.capability_catalog.call_capability(
                "echo", {}, second.id, principal)

    async def test_disconnect_connect_api(self):
        status, payload = await self.api_call(
            "POST", "/api/mcp/servers/echo/disconnect", {}, {})
        self.assertEqual(status, 200)
        self.assertEqual(self.units()["mcp.echo"]["state"], "disabled")
        self.assertEqual(self.app.capability_catalog.entries(kind="mcp_tool"), [])
        status, payload = await self.api_call(
            "POST", "/api/mcp/servers/echo/connect", {}, {})
        self.assertEqual(status, 200)
        self.assertEqual(self.units()["mcp.echo"]["state"], "ready")

    async def test_call_requires_approval_then_executes_once(self):
        status, payload = await self.api_call(
            "POST", "/api/mcp/call",
            {}, {"server": "echo", "tool": "echo", "args": {"text": "mcp"}})
        self.assertEqual(status, 202)
        data = payload["data"]
        self.assertEqual(data["status"], "pending")
        self.assertFalse(data["executed"])
        approval_id = data["approval_id"]
        self.app.approvals.decide(approval_id, decision="allow", mode="once",
                                  by="operator")
        status, payload = await self.api_call(
            "POST", "/api/mcp/call",
            {}, {"server": "echo", "tool": "echo", "args": {"text": "mcp"},
                 "approval_id": approval_id})
        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertEqual(data["status"], "ok")
        result = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(result["echo"], "mcp")
        status, payload = await self.api_call(
            "POST", "/api/mcp/call",
            {}, {"server": "echo", "tool": "echo", "args": {"text": "again"},
                 "approval_id": approval_id})
        self.assertEqual(status, 200)
        self.assertFalse(payload["data"]["executed"])
        self.assertEqual(payload["data"]["status"], "claimed")

    async def test_crash_marks_unit_failed_and_reconnects(self):
        self.app.policy.add_rule(effect="allow", capability="shell.exec",
                                 principal="mcp.echo", scope={},
                                 risk_max="high")
        with self.assertRaises(Exception):
            await self.host.call_tool("echo", "die", {}, principal="mcp.echo")
        await asyncio.sleep(0.2)
        units = self.units()
        self.assertEqual(units["mcp.echo"]["state"], "failed")
        self.assertIn("process exited", units["mcp.echo"]["reason"])
        self.assertEqual(units["mcp.0"]["state"], "degraded")
        disconnected = [record.type for record in
                        self.app.eventlog.query(types=["mcp"],
                                                limit=50)]
        self.assertIn("mcp.disconnected", disconnected)
        status = await self.host.connect("echo", manual=True)
        self.assertEqual(status["state"], "ready")
        self.assertEqual(self.units()["mcp.echo"]["state"], "ready")


if __name__ == "__main__":
    unittest.main()

"""Web input validation / error hygiene / request framing limits (L1).

The console used to trust the request head and query string:

* ``Content-Length: -5`` reached ``readexactly(-5)`` and surfaced as a 500;
* a non-numeric length was silently treated as zero;
* request line / header count / header bytes had no cap;
* ``limit``/``priority`` non-integers were 500s, and ``bool("false")`` turned
  ``allow_writes``/``purge_workspace`` into ``True``;
* ``WebAPI.handle`` returned ``str(exc)`` (internal paths) on unexpected errors;
* a NUL in a static path made ``Path.resolve()`` raise -> 500.

These tests pin the L1 contract end to end over a real loopback WebServer.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from stackflowos.config import default_root
from stackflowos.l2 import EventLog
from stackflowos.web.auth import CONTENT_SECURITY_POLICY
from stackflowos.web.server import (
    MAX_BODY,
    MAX_HEADER_BYTES,
    MAX_HEADERS,
    MAX_REQUEST_LINE,
    WebServer,
)

REPO = default_root()


class FakeChat:
    def __init__(self) -> None:
        self.limits: list[int] = []

    def history(self, limit: int = 100):
        self.limits.append(limit)
        return []


class FakeApp:
    """Records the arguments each handler passes to the app layer."""

    version = "0.1.0-test"

    def __init__(self, tmp: Path) -> None:
        self.eventlog = EventLog(tmp / "events")
        self.chat = FakeChat()
        self.calls: dict[str, list] = {}
        # The API reaches these subsystems through attributes; fake them all.
        self.approvals = self
        self.projects = self
        self.policy = self
        self.leases = self
        self.supervisor = self

    # -- helpers ---------------------------------------------------------
    def _record(self, event: str, **kwargs) -> None:
        self.calls.setdefault(event, []).append(kwargs)

    # -- read-only endpoints --------------------------------------------
    def health(self):
        return {"status": "ok"}

    def snapshot(self):
        return {"sys": {"name": "TestOS"}, "projects": []}

    def console(self, *, types=None, project_id=None, limit=200):
        self._record("console", types=types, project_id=project_id, limit=limit)
        return []

    def list_rules(self, **kwargs):
        self._record("list_rules", **kwargs)
        return []

    def list(self, **kwargs):
        # Shared by approvals.list and leases.list.
        self._record("list", **kwargs)
        return []

    def task_list(self, **kwargs):
        self._record("task_list", **kwargs)
        return []

    def list_agents(self, **kwargs):  # pragma: no cover - not exercised
        return []

    # -- write endpoints --------------------------------------------------
    async def attach_project(self, name, path, goal="", allow_writes=False,
                             created_by="human:operator"):
        self._record("attach_project", name=name, path=path, goal=goal,
                     allow_writes=allow_writes)
        return {"id": "p-1", "name": name, "workspace_mode":
                "attached-rw" if allow_writes else "attached-ro"}

    async def set_project_access(self, project_id, *, allow_writes,
                                 by="human:operator"):
        self._record("set_project_access", project_id=project_id,
                     allow_writes=allow_writes)
        return {"id": project_id, "workspace_mode":
                "attached-rw" if allow_writes else "attached-ro"}

    async def delete_project(self, project_id, *, purge_workspace=True,
                             confirm=None, by="human:operator"):
        self._record("delete_project", project_id=project_id,
                     purge_workspace=purge_workspace, confirm=confirm)
        return {"deleted": project_id, "workspace_purged": purge_workspace}

    def add_task(self, project_id, title, description="", role=None,
                 priority=0, deps=None, created_by="human:operator"):
        self._record("add_task", project_id=project_id, title=title,
                     priority=priority)
        return _Task({"id": "t-1", "title": title, "priority": priority})

    def task_check(self, task_id, **kwargs):
        self._record("task_check", task_id=task_id, **kwargs)
        return _Task(kwargs)

    def task_update(self, task_id, patch):
        return _Task({"id": task_id, **patch})

    def task_add_note(self, task_id, text, author="human:operator"):
        return _Task({"id": task_id, "note": text})


class _Task:
    def __init__(self, data: dict) -> None:
        self.data = data

    def to_dict(self) -> dict:
        return dict(self.data)


class RawResponse:
    def __init__(self, status: int, headers: dict, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8") or "{}")


def parse_response(data: bytes) -> RawResponse:
    head, _, payload = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1]) if lines else 0
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, separator, value = line.partition(":")
        if separator:
            headers[key.strip().lower()] = value.strip()
    return RawResponse(status, headers, payload)


class WebValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.app = FakeApp(self.tmp)
        self.assets = self.tmp / "assets"
        (self.assets / "css").mkdir(parents=True)
        (self.assets / "index.html").write_text(
            "<!doctype html><title>SFO</title>", "utf-8")
        (self.assets / "css" / "os.css").write_text("body{}", "utf-8")
        self.server = WebServer(self.app, host="127.0.0.1", port=0,
                                assets_dir=self.assets)
        await self.server.start()
        self.port = self.server.port
        self.token = self.server.launch_token

    async def asyncTearDown(self) -> None:
        await self.server.stop()
        await self.app.eventlog.close()
        self._tmp.cleanup()

    # -- raw HTTP helpers -------------------------------------------------
    def auth(self) -> list[tuple[str, str]]:
        return [("Authorization", f"Bearer {self.token}")]

    def write_headers(self) -> list[tuple[str, str]]:
        return self.auth() + [("Content-Type", "application/json"),
                              ("X-SFO-Request", "1")]

    def wire(self, method: str, path: str, *,
             headers: list[tuple[str, str]] | None = None,
             body: bytes = b"", auto_length: bool = True) -> bytes:
        lines = [f"{method} {path} HTTP/1.1",
                 f"Host: 127.0.0.1:{self.port}"]
        for key, value in headers or []:
            lines.append(f"{key}: {value}")
        if auto_length and body:
            lines.append(f"Content-Length: {len(body)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body

    async def send(self, wire: bytes, timeout: float = 5.0) -> RawResponse:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            writer.write(wire)
            await writer.drain()
            data = await asyncio.wait_for(reader.read(), timeout)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        return parse_response(data)

    # -- Content-Length / body framing ------------------------------------
    async def test_negative_content_length_is_400(self):
        resp = await self.send(
            f"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            "Content-Length: -5\r\n\r\n".encode("latin-1"))
        self.assertEqual(resp.status, 400, resp.body)
        self.assertEqual(resp.json["error"]["code"], "BAD_REQUEST")

    async def test_non_numeric_content_length_is_400(self):
        for value in ("abc", "1.5", "+3", "", "0x10"):
            with self.subTest(value=value):
                resp = await self.send(
                    f"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                    f"Content-Length: {value}\r\n\r\n".encode("latin-1"))
                self.assertEqual(resp.status, 400, resp.body)

    async def test_oversized_body_is_413(self):
        resp = await self.send(
            f"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"Content-Length: {MAX_BODY + 1}\r\n\r\n".encode("latin-1"))
        self.assertEqual(resp.status, 413, resp.body)

    async def test_chunked_transfer_encoding_is_not_implemented(self):
        resp = await self.send(
            f"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            "Transfer-Encoding: chunked\r\n\r\n".encode("latin-1"))
        self.assertIn(resp.status, (411, 501), resp.body)

    # -- request line / header caps ---------------------------------------
    async def test_oversized_request_line_is_414(self):
        path = "/" + ("a" * (MAX_REQUEST_LINE + 100))
        resp = await self.send(self.wire("GET", path, headers=self.auth()))
        self.assertIn(resp.status, (400, 414), resp.body)

    async def test_too_many_headers_is_431(self):
        headers = self.auth() + [
            (f"X-Test-{index}", "v") for index in range(MAX_HEADERS + 5)
        ]
        resp = await self.send(self.wire("GET", "/api/state", headers=headers))
        self.assertIn(resp.status, (400, 431), resp.body)

    async def test_oversized_header_block_is_431(self):
        headers = self.auth() + [("X-Big", "a" * (MAX_HEADER_BYTES + 16))]
        resp = await self.send(self.wire("GET", "/api/state", headers=headers))
        self.assertIn(resp.status, (400, 431), resp.body)

    async def test_error_responses_keep_security_headers(self):
        resp = await self.send(
            f"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            "Content-Length: -5\r\n\r\n".encode("latin-1"))
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.headers.get("x-frame-options"), "DENY")
        self.assertEqual(resp.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(resp.headers.get("content-security-policy"),
                         CONTENT_SECURITY_POLICY)

    # -- path controls -----------------------------------------------------
    async def test_nul_path_is_400(self):
        # Raw NUL byte in the request target.
        wire = (f"GET /css/a\x00b.css HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n\r\n").encode("latin-1")
        resp = await self.send(wire)
        self.assertEqual(resp.status, 400, resp.body)
        self.assertEqual(resp.json["error"]["code"], "BAD_REQUEST")

    async def test_percent_encoded_nul_path_is_400(self):
        resp = await self.send(self.wire(
            "GET", "/css/a%00b.css", headers=self.auth()))
        self.assertEqual(resp.status, 400, resp.body)

    # -- query/body integers ----------------------------------------------
    async def test_invalid_integer_query_is_400(self):
        cases = (
            "/api/events?limit=abc",
            "/api/events?since=abc",
            "/api/events?limit=-1",
            "/api/console?limit=abc",
            "/api/chat?limit=abc",
            "/api/approvals?limit=abc",
        )
        for path in cases:
            with self.subTest(path=path):
                resp = await self.send(self.wire(
                    "GET", path, headers=self.auth()))
                self.assertEqual(resp.status, 400, resp.body)
                self.assertFalse(resp.json["ok"])
                self.assertEqual(resp.json["error"]["code"], "BAD_REQUEST")

    async def test_valid_limits_still_work(self):
        resp = await self.send(self.wire(
            "GET", "/api/events?limit=5&since=0", headers=self.auth()))
        self.assertEqual(resp.status, 200, resp.body)
        resp = await self.send(self.wire(
            "GET", "/api/chat?limit=7", headers=self.auth()))
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(self.app.chat.limits[-1], 7)

    async def test_invalid_priority_is_400(self):
        for value in ("abc", 1.5, -1, 4):
            with self.subTest(value=value):
                body = json.dumps({"title": "t", "priority": value}).encode()
                resp = await self.send(self.wire(
                    "POST", "/api/projects/p-1/tasks",
                    headers=self.write_headers(), body=body))
                self.assertEqual(resp.status, 400, resp.body)

    async def test_valid_priority_is_created(self):
        body = json.dumps({"title": "t", "priority": 2}).encode()
        resp = await self.send(self.wire(
            "POST", "/api/projects/p-1/tasks",
            headers=self.write_headers(), body=body))
        self.assertEqual(resp.status, 201, resp.body)
        self.assertEqual(self.app.calls["add_task"][-1]["priority"], 2)

    # -- strict booleans ---------------------------------------------------
    async def test_allow_writes_false_string_is_false(self):
        for value in (False, "false", "0", "no", "off"):
            with self.subTest(value=value):
                body = json.dumps({"allow_writes": value}).encode()
                resp = await self.send(self.wire(
                    "POST", "/api/projects/p-1/access",
                    headers=self.write_headers(), body=body))
                self.assertEqual(resp.status, 200, resp.body)
                self.assertIs(
                    self.app.calls["set_project_access"][-1]["allow_writes"],
                    False)

    async def test_allow_writes_true_still_requires_confirm(self):
        body = json.dumps({"allow_writes": "true"}).encode()
        resp = await self.send(self.wire(
            "POST", "/api/projects/p-1/access",
            headers=self.write_headers(), body=body))
        self.assertEqual(resp.status, 400, resp.body)
        body = json.dumps({"allow_writes": "true", "confirm": True}).encode()
        resp = await self.send(self.wire(
            "POST", "/api/projects/p-1/access",
            headers=self.write_headers(), body=body))
        self.assertEqual(resp.status, 200, resp.body)
        self.assertIs(
            self.app.calls["set_project_access"][-1]["allow_writes"], True)

    async def test_invalid_boolean_is_400_not_silent(self):
        body = json.dumps({"allow_writes": "maybe"}).encode()
        resp = await self.send(self.wire(
            "POST", "/api/projects/p-1/access",
            headers=self.write_headers(), body=body))
        self.assertEqual(resp.status, 400, resp.body)

    async def test_attach_allow_writes_false_string(self):
        body = json.dumps({
            "name": "attached", "path": "/tmp/attached",
            "confirm": "/tmp/attached", "allow_writes": "false",
        }).encode()
        resp = await self.send(self.wire(
            "POST", "/api/projects/attach",
            headers=self.write_headers(), body=body))
        self.assertIn(resp.status, (200, 201), resp.body)
        self.assertIs(
            self.app.calls["attach_project"][-1]["allow_writes"], False)

    async def test_purge_workspace_false_string_is_false(self):
        body = json.dumps({
            "confirm": "Demo", "purge_workspace": "false",
        }).encode()
        resp = await self.send(self.wire(
            "DELETE", "/api/projects/p-1",
            headers=self.write_headers(), body=body))
        self.assertEqual(resp.status, 200, resp.body)
        self.assertIs(
            self.app.calls["delete_project"][-1]["purge_workspace"], False)
        self.assertFalse(resp.json["data"]["workspace_purged"])

    async def test_purge_workspace_defaults_to_true(self):
        body = json.dumps({"confirm": "Demo"}).encode()
        resp = await self.send(self.wire(
            "DELETE", "/api/projects/p-1",
            headers=self.write_headers(), body=body))
        self.assertEqual(resp.status, 200, resp.body)
        self.assertIs(
            self.app.calls["delete_project"][-1]["purge_workspace"], True)

    async def test_leases_all_flag_is_strict(self):
        resp = await self.send(self.wire(
            "GET", "/api/leases?all=true", headers=self.auth()))
        self.assertEqual(resp.status, 200, resp.body)
        self.assertIs(self.app.calls["list"][-1]["active_only"], False)
        resp = await self.send(self.wire(
            "GET", "/api/leases?all=false", headers=self.auth()))
        self.assertEqual(resp.status, 200, resp.body)
        self.assertIs(self.app.calls["list"][-1]["active_only"], True)
        resp = await self.send(self.wire(
            "GET", "/api/leases?all=banana", headers=self.auth()))
        self.assertEqual(resp.status, 400, resp.body)

    async def test_rules_active_only_is_strict(self):
        resp = await self.send(self.wire(
            "GET", "/api/rules?active_only=false", headers=self.auth()))
        self.assertEqual(resp.status, 200, resp.body)
        self.assertIs(self.app.calls["list_rules"][-1]["active_only"], False)
        resp = await self.send(self.wire(
            "GET", "/api/rules?active_only=maybe", headers=self.auth()))
        self.assertEqual(resp.status, 400, resp.body)

    async def test_task_check_done_false_string(self):
        body = json.dumps({"item_id": "i-1", "done": "false"}).encode()
        resp = await self.send(self.wire(
            "POST", "/api/tasks/t-1/check",
            headers=self.write_headers(), body=body))
        self.assertEqual(resp.status, 200, resp.body)
        self.assertIs(self.app.calls["task_check"][-1]["done"], False)

    # -- error hygiene -----------------------------------------------------
    async def test_generic_handler_exception_is_redacted(self):
        secret = "SECRET-INTERNAL-PATH-/etc/passwd"

        def boom(*args, **kwargs):
            raise RuntimeError(secret)

        self.app.console = boom
        with self.assertLogs("sfo.web.api", level="ERROR") as logs:
            resp = await self.send(self.wire(
                "GET", "/api/console?limit=10", headers=self.auth()))
        self.assertEqual(resp.status, 500, resp.body)
        error = resp.json["error"]
        self.assertEqual(error["code"], "INTERNAL")
        self.assertEqual(error["message"], "internal server error")
        self.assertRegex(error.get("request_id", ""), r"^[0-9a-f]{16}$")
        self.assertNotIn(secret.encode(), resp.body)
        self.assertNotIn(b"/etc/passwd", resp.body)
        # The stack trace is logged, not returned.
        self.assertTrue(any(record.exc_info for record in logs.records))
        self.assertTrue(any(
            record.exc_info and record.exc_info[0] is RuntimeError
            for record in logs.records))

    async def test_bad_json_message_has_no_parser_internals(self):
        resp = await self.send(self.wire(
            "POST", "/api/chat", headers=self.write_headers(),
            body=b"{not json"))
        self.assertEqual(resp.status, 400, resp.body)
        message = resp.json["error"]["message"]
        self.assertEqual(message, "invalid JSON body")
        self.assertNotIn("line", message)
        self.assertNotIn("char", message)

    # -- admission ordering / happy path ----------------------------------
    async def test_unauthenticated_write_is_401_before_body_parsing(self):
        body = b"{not json"  # would be a 400 if parsing ran first
        resp = await self.send(self.wire(
            "POST", "/api/projects/p-1/delete",
            headers=[("Content-Type", "application/json"),
                     ("X-SFO-Request", "1")], body=body))
        self.assertEqual(resp.status, 401, resp.body)
        self.assertEqual(resp.json["error"]["code"], "UNAUTHORIZED")

    async def test_authenticated_normal_request_is_200(self):
        resp = await self.send(self.wire(
            "GET", "/api/state", headers=self.auth()))
        self.assertEqual(resp.status, 200, resp.body)
        self.assertTrue(resp.json["ok"])

    async def test_static_asset_still_succeeds(self):
        resp = await self.send(self.wire("GET", "/css/os.css"))
        self.assertEqual(resp.status, 200, resp.body)
        self.assertEqual(resp.headers.get("content-type"),
                         "text/css; charset=utf-8")


if __name__ == "__main__":
    unittest.main()

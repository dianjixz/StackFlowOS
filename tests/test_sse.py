"""SSE hardening tests (H2): auth-only stream, no CORS, strict limits.

The web console exposes ``GET /api/events/stream``.  H1 made it authenticated
but deliberately kept ``Access-Control-Allow-Origin: *`` for H2 to remove.
These tests pin the H2 contract:

* unauthenticated -> 401 and no ``Access-Control-*`` headers;
* authenticated -> 200 event stream and still no CORS headers;
* ``heartbeat``/``since``/``limit`` are validated *before* the 200 response,
  so bad values yield a 400 JSON error and never subscribe to the event log;
* ``web.sse_max_clients`` bounds concurrent streams (503, counter released);
* ``limit`` caps ``since`` replay and the initial ``tail``;
* ``retry: 2000`` and the ``: ping`` heartbeat are preserved.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from stackflowos.config import Config, ConfigError, default_root
from stackflowos.l2 import EventLog
from stackflowos.web.server import WebServer

REPO = default_root()


class FakeConfig:
    def public_dict(self):
        return {"system": {"name": "TestOS"}, "web": {"port": 0}}


class FakeApp:
    """Just enough App for the SSE route."""

    version = "0.1.0-test"

    def __init__(self, tmp: Path):
        self.eventlog = EventLog(tmp / "events")
        self.config = FakeConfig()

    def health(self):
        return {"status": "ok", "http": "ok", "transport": "ok",
                "version": self.version, "uptime_s": 5,
                "units": {"online": 1}}

    def snapshot(self):
        return {"sys": {"name": "TestOS", "version": self.version},
                "units": [], "projects": [], "agents": [], "leases": [],
                "approvals": [], "rules": [], "events": [], "chat": [],
                "config": self.config.public_dict(), "stats": {}}


def parse_response_head(data: bytes) -> tuple[int, dict]:
    head, _, _ = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1]) if lines else 0
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, separator, value = line.partition(":")
        if separator:
            headers[key.strip().lower()] = value.strip()
    return status, headers


async def raw_request(port: int, path: str, *, token: str | None = None,
                      host: str | None = None,
                      timeout: float = 5.0) -> tuple[int, dict, bytes]:
    """Send a request whose response ends at EOF (every error/JSON does)."""
    if host is None:
        host = f"127.0.0.1:{port}"
    lines = [f"GET {path} HTTP/1.1", f"Host: {host}"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    status, headers = parse_response_head(data)
    _, _, payload = data.partition(b"\r\n\r\n")
    return status, headers, payload


class SseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.app = FakeApp(self.tmp)
        self.server = WebServer(
            self.app, host="127.0.0.1", port=0,
            assets_dir=self.tmp / "assets",
            # Minimum legal heartbeat keeps the disconnect/ping tests quick.
            sse_heartbeat_s=1.0,
            sse_max_clients=2,
        )
        await self.server.start()
        self.port = self.server.port
        self.token = self.server.launch_token
        self._writers: list[asyncio.StreamWriter] = []

    async def asyncTearDown(self):
        for writer in self._writers:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        await self.server.stop()
        await self.app.eventlog.close()
        self._tmp.cleanup()

    def assert_no_cors(self, headers: dict):
        leaked = [key for key in headers if key.startswith("access-control-")]
        self.assertEqual(leaked, [], f"CORS headers leaked: {headers}")

    async def open_stream(self, path: str = "/api/events/stream",
                          *, token: str | None = None):
        """Open an SSE request and return ``(reader, writer, status, headers)``."""
        token = self.token if token is None else token
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: 127.0.0.1:{self.port}",
            f"Authorization: Bearer {token}",
        ]
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        status, headers = parse_response_head(head)
        self._writers.append(writer)
        return reader, writer, status, headers

    async def read_until(self, reader: asyncio.StreamReader, needle: bytes,
                         *, timeout: float = 3.0) -> bytes:
        data = b""
        deadline = asyncio.get_running_loop().time() + timeout
        while needle not in data:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(reader.read(4096), remaining)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            data += chunk
        return data

    async def wait_for_client_count(self, expected: int, *, timeout: float = 5.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.server._sse_clients == expected:
                return
            await asyncio.sleep(0.02)
        self.fail(f"SSE client count is {self.server._sse_clients}, expected {expected}")

    async def close_stream(self, writer: asyncio.StreamWriter):
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        if writer in self._writers:
            self._writers.remove(writer)

    # -- auth / headers --------------------------------------------------
    async def test_unauthenticated_stream_is_401_without_cors(self):
        status, headers, body = await raw_request(
            self.port, "/api/events/stream")
        self.assertEqual(status, 401, body)
        self.assertIn(b"UNAUTHORIZED", body)
        self.assertEqual(headers.get("content-type"),
                         "application/json; charset=utf-8")
        self.assert_no_cors(headers)
        self.assertEqual(len(self.app.eventlog._subscribers), 0)
        self.assertEqual(self.server._sse_clients, 0)

    async def test_authenticated_stream_is_200_without_cors(self):
        reader, writer, status, headers = await self.open_stream()
        try:
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("content-type"),
                             "text/event-stream; charset=utf-8")
            self.assertIn("no-cache", headers.get("cache-control", ""))
            self.assertIn("no-store", headers.get("cache-control", ""))
            self.assertEqual(headers.get("x-content-type-options"), "nosniff")
            self.assertEqual(headers.get("x-frame-options"), "DENY")
            self.assert_no_cors(headers)
            # retry hint is preserved for EventSource reconnects.
            self.assertIn(b"retry: 2000", await self.read_until(reader, b"\n\n"))
            self.assertEqual(self.server._sse_clients, 1)
            self.assertEqual(len(self.app.eventlog._subscribers), 1)
        finally:
            await self.close_stream(writer)
        await self.wait_for_client_count(0)
        self.assertEqual(len(self.app.eventlog._subscribers), 0)

    # -- strict parameter validation ------------------------------------
    async def test_invalid_heartbeat_is_400_before_streaming(self):
        for value in ("0", "-1", "-0.5", "0.5", "NaN", "nan", "Inf",
                      "infinity", "1e999", "abc", ""):
            with self.subTest(heartbeat=value):
                status, headers, body = await raw_request(
                    self.port, f"/api/events/stream?heartbeat={value}",
                    token=self.token)
                self.assertEqual(status, 400, body)
                payload = json.loads(body)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error"]["code"], "BAD_REQUEST")
                self.assertEqual(headers.get("content-type"),
                                 "application/json; charset=utf-8")
                self.assert_no_cors(headers)
                self.assertEqual(len(self.app.eventlog._subscribers), 0)
                self.assertEqual(self.server._sse_clients, 0)

    async def test_invalid_since_is_400(self):
        for value in ("-1", "abc", "1.5", ""):
            with self.subTest(since=value):
                status, _, body = await raw_request(
                    self.port, f"/api/events/stream?since={value}",
                    token=self.token)
                self.assertEqual(status, 400, body)
                self.assertEqual(json.loads(body)["error"]["code"], "BAD_REQUEST")
                self.assertEqual(len(self.app.eventlog._subscribers), 0)

    async def test_invalid_limit_is_400(self):
        for value in ("0", "-1", "1.5", "abc", "1001", "99999", ""):
            with self.subTest(limit=value):
                status, _, body = await raw_request(
                    self.port, f"/api/events/stream?limit={value}",
                    token=self.token)
                self.assertEqual(status, 400, body)
                self.assertEqual(json.loads(body)["error"]["code"], "BAD_REQUEST")
                self.assertEqual(len(self.app.eventlog._subscribers), 0)

    # -- concurrency cap -------------------------------------------------
    async def test_too_many_clients_is_503_without_subscribing(self):
        reader_a, writer_a, status_a, _ = await self.open_stream()
        reader_b, writer_b, status_b, _ = await self.open_stream()
        self.assertEqual((status_a, status_b), (200, 200))
        self.assertEqual(self.server._sse_clients, 2)
        try:
            status, headers, body = await raw_request(
                self.port, "/api/events/stream", token=self.token)
            self.assertEqual(status, 503, body)
            payload = json.loads(body)
            self.assertEqual(payload["error"]["code"], "UNAVAILABLE")
            self.assert_no_cors(headers)
            self.assertEqual(self.server._sse_clients, 2)
            self.assertEqual(len(self.app.eventlog._subscribers), 2)
        finally:
            await self.close_stream(writer_a)
            await self.close_stream(writer_b)
        await self.wait_for_client_count(0)
        self.assertEqual(len(self.app.eventlog._subscribers), 0)

    # -- closed log (S7) -------------------------------------------------
    async def test_closed_event_log_is_503_without_subscribing(self):
        await self.app.eventlog.close()
        status, headers, body = await raw_request(
            self.port, "/api/events/stream", token=self.token)
        self.assertEqual(status, 503, body)
        payload = json.loads(body)
        self.assertEqual(payload["error"]["code"], "UNAVAILABLE")
        self.assert_no_cors(headers)
        # A closed log rejects the stream before counting a client or
        # registering a queue.
        self.assertEqual(self.server._sse_clients, 0)
        self.assertEqual(len(self.app.eventlog._subscribers), 0)

    # -- replay limits / heartbeat / counter release ---------------------
    async def test_limit_caps_tail_and_since_replay(self):
        for index in range(5):
            self.app.eventlog.append("tick", {"i": index}, source="test")

        reader, writer, status, _ = await self.open_stream(
            "/api/events/stream?limit=2")
        try:
            self.assertEqual(status, 200)
            # tail(limit=2) returns seq 4 and 5, not the older records.
            seen = await self.read_until(reader, b'"seq":5')
            self.assertIn(b'"seq":4', seen)
            self.assertIn(b'"seq":5', seen)
            self.assertNotIn(b'"seq":3', seen)
        finally:
            await self.close_stream(writer)

        await self.wait_for_client_count(0)
        reader, writer, status, _ = await self.open_stream(
            "/api/events/stream?since=1&limit=2")
        try:
            self.assertEqual(status, 200)
            # since(1, limit=2) replays seq 2 and 3, not seq 4/5.
            seen = await self.read_until(reader, b'"seq":3')
            self.assertIn(b'"seq":2', seen)
            self.assertIn(b'"seq":3', seen)
            self.assertNotIn(b'"seq":4', seen)
        finally:
            await self.close_stream(writer)
        await self.wait_for_client_count(0)

    async def test_heartbeat_ping_and_counter_release(self):
        reader, writer, status, _ = await self.open_stream(
            "/api/events/stream?heartbeat=1")
        try:
            self.assertEqual(status, 200)
            seen = await self.read_until(reader, b": ping", timeout=3.0)
            self.assertIn(b": ping", seen)
            self.assertIn(b"retry: 2000", seen)
            self.assertEqual(self.server._sse_clients, 1)
        finally:
            await self.close_stream(writer)
        # Disconnect must release the counter (and the event-log queue).
        await self.wait_for_client_count(0)
        self.assertEqual(len(self.app.eventlog._subscribers), 0)


class SseConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_defaults(self):
        cfg = Config.load(self.root)
        self.assertEqual(cfg.web.sse_heartbeat_s, 15.0)
        self.assertEqual(cfg.web.sse_max_clients, 32)

    def test_bounds_are_validated(self):
        cfg = Config.load(self.root)
        for patch in ({"web": {"sse_max_clients": 0}},
                      {"web": {"sse_max_clients": 257}},
                      {"web": {"sse_heartbeat_s": 0}},
                      {"web": {"sse_heartbeat_s": 301}}):
            with self.subTest(patch=patch):
                with self.assertRaises(ConfigError):
                    cfg.update(patch)
        self.assertFalse(cfg.paths.config_file.exists())


if __name__ == "__main__":
    unittest.main()

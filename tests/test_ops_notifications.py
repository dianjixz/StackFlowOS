"""Tests for webhook notifications (observability.notifications)."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from stackflowos.errors import Denied
from stackflowos.l2 import EventLog
from stackflowos.observability import notifications
from stackflowos.web.api import _current_auth


class _HookHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            body = {"raw": raw.decode("utf-8", "replace")}
        server = self.server
        server.requests.append({"headers": dict(self.headers), "body": body})  # type: ignore[attr-defined]
        if len(server.requests) <= server.fail_first:  # type: ignore[attr-defined]
            self.send_response(500)
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args):  # silence test server output
        pass


class _HookServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, fail_first: int = 0):
        self.requests = []
        self.fail_first = fail_first
        super().__init__(("127.0.0.1", 0), _HookHandler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/hook"


class _Observability:
    def __init__(self, webhooks, events=None, timeout=1.0):
        self.metrics_enabled = True
        self.webhooks = webhooks
        self.notify_events = events if events is not None else ["approval.created"]
        self.notify_timeout_s = timeout


class _Config:
    def __init__(self, observability):
        self.observability = observability


class FakeApp:
    def __init__(self, tmp: Path, observability: _Observability):
        self.eventlog = EventLog(tmp / "events")
        self.config = _Config(observability)


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.server = _HookServer()
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.apps: list[FakeApp] = []

    async def asyncTearDown(self):
        for app in self.apps:
            await app.eventlog.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self._tmp.cleanup()

    def _build(self, webhooks, *, events=None, fail_first: int = 0,
               name: str = "app") -> notifications.NotificationExtension:
        self.server.fail_first = fail_first
        observability = _Observability(webhooks, events=events)
        app = FakeApp(self.tmp / name, observability)
        self.apps.append(app)
        self.app = app
        return notifications.build(app)

    async def _wait(self, predicate, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail(f"condition not met within {timeout}s")

    def _events(self, type_: str) -> list[dict]:
        return [record.to_dict() for record in self.app.eventlog.tail(100)
                if record.type == type_]

    async def test_delivery_payload_is_redacted_and_audited(self):
        extension = self._build([{
            "url": self.server.url,
            "headers": {"Authorization": "Bearer top-secret"},
            "events": ["approval.created"],
            "rate_limit_per_s": 100,
            "burst": 100,
            "retry_backoff_s": 0.01,
        }])
        await extension.start()
        try:
            self.app.eventlog.append("approval.created", {
                "request_id": "apr-1",
                "api_key": "super-secret",
                "token": "also-secret",
                "safe": "visible",
            })
            await self._wait(lambda: len(self.server.requests) == 1)
            request = self.server.requests[0]
            # The configured header is sent to the sink (it is the credential
            # the operator chose) ...
            self.assertEqual(request["headers"]["Authorization"],
                             "Bearer top-secret")
            # ... but payload secrets are masked.
            body = request["body"]
            self.assertEqual(body["type"], "approval.created")
            self.assertEqual(body["data"]["api_key"], "***")
            self.assertEqual(body["data"]["token"], "***")
            self.assertEqual(body["data"]["safe"], "visible")
            self.assertIn("seq", body)
            self.assertIn("ts", body)
            self.assertIn("project_id", body)
            await self._wait(lambda: bool(self._events("approval.notified")))
            audits = self._events("approval.notified")
            self.assertEqual(audits[0]["data"]["ok"], True)
            self.assertEqual(audits[0]["data"]["event_type"], "approval.created")
            self.assertNotIn("super-secret", json.dumps(audits, ensure_ascii=False))
            self.assertNotIn("also-secret", json.dumps(audits, ensure_ascii=False))
            self.assertEqual(extension.health()["sinks"], 1)
            self.assertEqual(extension.health()["sent"], 1)
        finally:
            await extension.stop()

    async def test_event_filter_only_sends_configured_types(self):
        extension = self._build([{
            "url": self.server.url,
            "events": ["unit.failed"],
        }])
        await extension.start()
        try:
            self.app.eventlog.append("approval.created", {"x": 1})
            await asyncio.sleep(0.2)
            self.assertEqual(self.server.requests, [])
            self.app.eventlog.append("unit.failed", {"work_id": "w-1"})
            await self._wait(lambda: len(self.server.requests) == 1)
            self.assertEqual(self.server.requests[0]["body"]["type"],
                             "unit.failed")
        finally:
            await extension.stop()

    async def test_sink_without_events_uses_notify_events_default(self):
        extension = self._build([{"url": self.server.url}],
                                events=["approval.created"])
        await extension.start()
        try:
            self.app.eventlog.append("approval.created", {"x": 1})
            await self._wait(lambda: len(self.server.requests) == 1)
        finally:
            await extension.stop()

    async def test_retry_once_then_records_attempts(self):
        extension = self._build([{
            "url": self.server.url,
            "events": ["approval.created"],
            "retries": 1,
            "retry_backoff_s": 0.01,
            "rate_limit_per_s": 100,
            "burst": 100,
        }], fail_first=1)
        await extension.start()
        try:
            self.app.eventlog.append("approval.created", {"x": 1})
            await self._wait(lambda: len(self.server.requests) == 2)
            await self._wait(lambda: bool(self._events("approval.notified")))
            audit = self._events("approval.notified")[0]["data"]
            self.assertTrue(audit["ok"])
            self.assertEqual(audit["attempts"], 2)
            self.assertEqual(extension.health()["sent"], 1)
        finally:
            await extension.stop()

    async def test_permanent_failure_is_reported(self):
        extension = self._build([{
            "url": self.server.url,
            "events": ["approval.created"],
            "retries": 1,
            "retry_backoff_s": 0.01,
            "rate_limit_per_s": 100,
            "burst": 100,
        }], fail_first=99)
        await extension.start()
        try:
            self.app.eventlog.append("approval.created", {"x": 1})
            await self._wait(lambda: bool(self._events("approval.notified")))
            audit = self._events("approval.notified")[0]["data"]
            self.assertFalse(audit["ok"])
            self.assertEqual(audit["attempts"], 2)
            health = extension.health()
            self.assertEqual(health["failed"], 1)
            self.assertIsNotNone(health["last_error"])
            self.assertFalse(health["ok"])
        finally:
            await extension.stop()

    async def test_notification_meta_events_never_recurse(self):
        extension = self._build([{
            "url": self.server.url,
            "events": ["approval.notified"],
        }])
        await extension.start()
        try:
            self.app.eventlog.append("approval.notified", {"loop": True})
            await asyncio.sleep(0.2)
            self.assertEqual(self.server.requests, [])
        finally:
            await extension.stop()

    async def test_test_route_returns_per_sink_results(self):
        extension = self._build([{
            "url": self.server.url,
            "events": ["approval.created"],
            "rate_limit_per_s": 100,
            "burst": 100,
            "retry_backoff_s": 0.01,
        }])
        await extension.start()
        try:
            status, payload = await extension.handle_test(
                None, {}, {"message": "hello"})
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(len(payload["data"]["results"]), 1)
            result = payload["data"]["results"][0]
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], 200)
            self.assertEqual(self.server.requests[0]["body"]["type"],
                             "notifications.test")
            self.assertEqual(self.server.requests[0]["body"]["data"]["message"],
                             "hello")
        finally:
            await extension.stop()

    async def test_test_route_denies_viewer_role(self):
        extension = self._build([{"url": self.server.url}])
        await extension.start()
        token = _current_auth.set(SimpleNamespace(role="viewer"))
        try:
            with self.assertRaises(Denied):
                await extension.handle_test(None, {}, {})
            self.assertEqual(self.server.requests, [])
        finally:
            _current_auth.reset(token)
            await extension.stop()

    async def test_stop_unsubscribes_eventlog(self):
        extension = self._build([{"url": self.server.url}])
        await extension.start()
        self.assertEqual(len(self.app.eventlog._subscribers), 1)
        await extension.stop()
        self.assertEqual(len(self.app.eventlog._subscribers), 0)
        await extension.stop()  # idempotent

    async def test_invalid_sink_config_skipped(self):
        extension = self._build([
            {"url": "file:///etc/passwd"},
            {"url": ""},
            {"url": self.server.url, "events": ["approval.created"]},
        ])
        await extension.start()
        try:
            self.assertEqual(extension.health()["sinks"], 1)
            self.app.eventlog.append("approval.created", {"x": 1})
            await self._wait(lambda: len(self.server.requests) == 1)
        finally:
            await extension.stop()


if __name__ == "__main__":
    unittest.main()

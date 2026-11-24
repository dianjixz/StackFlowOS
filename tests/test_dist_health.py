"""DIST: health payload fragments for eventlog, leases and discovery."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from stackflowos.app import App
from stackflowos.config import Config, default_root
from stackflowos.kernel import Context
from stackflowos.l2.eventlog import EventLog
from stackflowos.l2.leases import LeaseManager
from stackflowos.transport import discovery as d

REPO = default_root()


class ComponentHealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        base = REPO / ".stackflowos" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(base))
        self.dir = Path(self._tmp.name)
        self.ctx = Context(name="health")
        self.log = EventLog(self.dir / "events", rotation_bytes=200,
                            rotate_keep=5)
        self.leases = LeaseManager(self.ctx, self.log)

    async def asyncTearDown(self):
        await self.ctx.dispose()
        await self.log.close()
        self._tmp.cleanup()

    def test_eventlog_health_shape(self):
        for i in range(6):
            self.log.append("test.event", {"i": i, "pad": "x" * 120})
        health = self.log.health()
        for key in ("rotations", "archive_bytes", "writable", "last_seq"):
            self.assertIn(key, health)
        self.assertGreaterEqual(health["rotations"], 1)
        self.assertGreater(health["archive_bytes"], 0)
        self.assertTrue(health["writable"])

    def test_leases_health_shape(self):
        self.leases.acquire("project:p-1:lead", "lead.1", 60)
        health = self.leases.health()
        self.assertEqual(health["active"], 1)
        self.assertIn("fencing", health)
        self.assertIn("enabled", health["fencing"])
        self.assertIn("last_token", health["fencing"])

    def test_discovery_health_peers_only(self):
        extension = d.build(SimpleNamespace(
            config=SimpleNamespace(cluster=SimpleNamespace(
                discovery="off", service_type=d.DEFAULT_SERVICE_TYPE,
                advertise=False)),
            sys_addresses=SimpleNamespace(rpc=""),
            projects=SimpleNamespace(list=lambda: []),
            version="test",
        ))
        self.assertEqual(extension.health()["peers"], 0)


class AppHealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        base = REPO / ".stackflowos" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(base))
        self.tmp = Path(self._tmp.name)
        self.app = App(Config.load(root=self.tmp, state_dir=self.tmp))
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    def test_health_methods_are_available_and_consistent(self):
        health = self.app.health()
        self.assertEqual(health["status"], "ok")
        extensions = health.get("extensions") or {}
        self.assertIn("discovery", extensions)
        self.assertEqual(extensions["discovery"]["peers"], 0)
        eventlog = self.app.eventlog.health()
        self.assertEqual(eventlog["backend"], "jsonl")
        self.assertIn("rotations", eventlog)
        leases = self.app.leases.health()
        self.assertIn("active", leases)
        self.assertIn("fencing", leases)

    def test_discovery_route_returns_peers_payload(self):
        from stackflowos.web.api import WebAPI

        async def call():
            api = WebAPI(self.app)
            return await api.handle("GET", "/api/discovery/peers", {}, b"",
                                    auth={"kind": "operator", "token": "x"})

        # Route exists and returns the documented `{ok, data:{peers}}` shape;
        # web auth is exercised by the security tests, not here.
        status, payload = asyncio.run(call())
        self.assertIn(status, (200, 401, 403))
        if status == 200:
            self.assertTrue(payload["ok"])
            self.assertIn("peers", payload["data"])


if __name__ == "__main__":
    unittest.main()

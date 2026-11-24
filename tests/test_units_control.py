"""Unit control: hysteresis, ticker, detail/actions APIs and UnitClient."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from stackflowos.app import App
from stackflowos.config import Config, default_root
from stackflowos.errors import BadRequest, Conflict, NotFound
from stackflowos.units.control import UnitClient, UnitStateStore, parse_unit_ref
from stackflowos.web.api import WebAPI

REPO = default_root()


class HysteresisTests(unittest.TestCase):
    def test_degrade_fail_and_recover_thresholds(self):
        store = UnitStateStore(degrade_after=2, fail_after=3, recover_after=2)
        store.ensure("u", "ready")
        self.assertIsNone(store.observe("u", False))
        transition = store.observe("u", False)
        self.assertIsNotNone(transition)
        self.assertEqual(transition["to"], "degraded")
        transition = store.observe("u", False)
        self.assertEqual(transition["to"], "failed")
        self.assertIsNone(store.observe("u", True))
        transition = store.observe("u", True)
        self.assertEqual(transition["to"], "ready")
        self.assertEqual(store.get("u").state, "ready")

    def test_sticky_degraded_does_not_recover(self):
        store = UnitStateStore()
        store.ensure("u", "ready")
        transition = store.observe("u", True, sticky_state="degraded",
                                   reason="invalid manifest")
        self.assertEqual(transition["to"], "degraded")
        for _ in range(3):
            self.assertIsNone(store.observe("u", True,
                                            sticky_state="degraded"))
        self.assertEqual(store.get("u").state, "degraded")

    def test_disable_drain_and_reenable(self):
        store = UnitStateStore()
        store.ensure("u", "ready")
        self.assertEqual(store.disable("u")["to"], "disabled")
        self.assertIsNone(store.observe("u", False))  # probes skipped
        self.assertEqual(store.enable("u")["to"], "unknown")
        self.assertEqual(store.drain("u")["to"], "draining")
        self.assertEqual(store.undrain("u")["to"], "unknown")

    def test_parse_unit_ref(self):
        self.assertEqual(parse_unit_ref("unit://approval.0"), "approval.0")
        self.assertEqual(parse_unit_ref("approval.0"), "approval.0")
        with self.assertRaises(BadRequest):
            parse_unit_ref("unit://")
        with self.assertRaises(BadRequest):
            parse_unit_ref("unit://a/b")


class UnitClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_inproc_call_and_stub_replacement_same_caller(self):
        tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cfg = Config.load(root=root, state_dir=root / "state")
        cfg = cfg.update({"skills": {"dirs": [], "enabled": []},
                          "plugins": {"dirs": [], "enabled": []},
                          "mcp": {"servers": []}})
        app = App(cfg)
        await app.start()
        try:
            client = UnitClient(app)

            async def caller(unit_client):
                return await unit_client.call("unit://approval.0",
                                              "policy.evaluate",
                                              {"principal": "human:operator",
                                               "capability": "fs.read",
                                               "scope": {"path": "x"}})

            real = await caller(client)
            self.assertIn(real["outcome"], ("allow", "ask", "deny"))
            removed = client.register(
                "approval.0", "policy.evaluate",
                lambda payload: {"outcome": "stub", "risk": "low",
                                 "reason": "stub"})
            stubbed = await caller(client)
            self.assertEqual(stubbed["outcome"], "stub")
            removed()
            restored = await caller(client)
            self.assertEqual(restored["outcome"], real["outcome"])
            requested = await client.call(
                "unit://approval.0", "approval.request",
                {"principal": "human:operator", "capability": "fs.read",
                 "scope": {"path": "x"}})
            self.assertIn(requested["status"], ("approved", "pending",
                                                "denied"))
            with self.assertRaises(NotFound):
                await client.call("unit://approval.0", "nope", {})

            # The subprocess/remote placeholder keeps the same call interface.
            original_cards = app.unit_cards

            class FakeTransport:
                async def call(self, unit_id, method, payload, *, timeout):
                    return {"via": "nng", "unit": unit_id, "method": method,
                            "timeout": timeout}

            app.unit_cards = lambda: [
                {"unit_id": "coder.1", "work_id": "coder.1",
                 "runtime": {"mode": "subprocess"}}]
            app.transport = FakeTransport()
            try:
                remote = await client.call("unit://coder.1", "ping", {})
                self.assertEqual(remote["via"], "nng")
            finally:
                app.unit_cards = original_cards
        finally:
            await app.stop()


class UnitControlApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        cfg = Config.load(root=self.tmp, state_dir=self.tmp / "state")
        cfg = cfg.update({"skills": {"dirs": [], "enabled": []},
                          "plugins": {"dirs": [], "enabled": []},
                          "mcp": {"servers": []}})
        self.app = App(cfg)
        await self.app.start()
        self.api = WebAPI(self.app)
        self.controller = self.app.unit_control

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    async def api_call(self, method, path, query=None, body=None):
        return await self.api.handle(method, path, query or {},
                                     json.dumps(body or {}).encode("utf-8"))

    async def test_summary_shape_and_health(self):
        status, payload = await self.api_call("GET", "/api/units/summary")
        self.assertEqual(status, 200)
        summary = payload["data"]
        self.assertEqual(summary["schema"], "stackflow.units/v1")
        self.assertIn(summary["health"], ("ok", "degraded", "down"))
        units = self.app.unit_cards()
        self.assertEqual(summary["total"], len(units))
        self.assertEqual(summary["online"], len([
            unit for unit in units
            if (unit.get("state") or "") in
            ("ready", "degraded", "online", "suspect", "draining")]))
        self.assertGreaterEqual(summary["by_tier"]["system"]["total"], 5)
        self.assertGreaterEqual(summary["by_tier"]["service"]["total"], 4)
        self.assertIn("busy", summary["by_tier"]["worker"])

    async def test_critical_failure_makes_health_down(self):
        summary = self.controller.summary()
        self.assertEqual(summary["health"], "ok")
        self.controller.sync_state("store.eventlog", "failed", "disk")
        summary = self.controller.summary()
        self.assertEqual(summary["health"], "down")
        self.assertIn("store.eventlog",
                      [item["unit_id"] for item in summary["problems"]])
        self.controller.sync_state("store.eventlog", "ready", None)

    async def test_detail_and_probe(self):
        status, payload = await self.api_call("GET", "/api/units/approval.0")
        self.assertEqual(status, 200)
        detail = payload["data"]
        self.assertEqual(detail["unit"]["unit_id"], "approval.0")
        self.assertEqual(detail["unit"]["runtime"]["mode"], "in_process")
        self.assertIsNone(detail["unit"]["pid"])
        self.assertIn("probe", detail["actions"])
        self.assertEqual(detail["dependency"]["edges"]["requires"],
                         ["store.eventlog"])
        self.assertEqual(detail["requires"], ["store.eventlog"])
        self.assertIsInstance(detail["events"], list)
        status, payload = await self.api_call("POST", "/api/units/approval.0/probe")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["unit"]["unit_id"], "approval.0")
        status, payload = await self.api_call("GET", "/api/units/nope.0")
        self.assertEqual(status, 404)

    async def test_action_not_declared_is_conflict(self):
        status, payload = await self.api_call(
            "POST", "/api/units/approval.0/actions", {}, {"action": "restart"})
        self.assertEqual(status, 409)
        self.assertIn("actions", payload["error"]["detail"])
        status, payload = await self.api_call(
            "POST", "/api/units/skill.0/actions", {}, {"action": "restart"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["restarted"])
        status, payload = await self.api_call(
            "POST", "/api/units/skill.0/actions", {}, {"action": "drain"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["unit"]["state"], "draining")
        # A draining unit stays in the online denominator.
        summary = self.controller.summary()
        self.assertGreaterEqual(summary["online"], 1)
        status, payload = await self.api_call(
            "POST", "/api/units/skill.0/actions", {}, {"action": "undrain"})
        self.assertEqual(status, 200)
        self.assertIn(payload["data"]["unit"]["state"], ("ready", "degraded"))

    async def test_worker_restart_action_uses_supervisor(self):
        from stackflowos.l2.registry import UnitInfo
        self.app.registry.register(UnitInfo(work_id="coder.99", kind="coder",
                                           label="Coder"))
        calls = []

        async def fake_restart(unit, attempt):
            calls.append((unit.work_id, attempt))
            return True

        self.app.supervisor.restart = fake_restart
        detail_status, detail_payload = await self.api_call(
            "GET", "/api/units/coder.99")
        self.assertEqual(detail_status, 200)
        self.assertIn("restart", detail_payload["data"]["actions"])
        status, payload = await self.api_call(
            "POST", "/api/units/coder.99/actions", {}, {"action": "restart"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["restarted"])
        self.assertEqual(calls, [("coder.99", 1)])

    async def test_ticker_hysteresis_emits_state_events(self):
        controller = self.controller
        controller.register_provider(
            "store.eventlog", probe=lambda: {"ok": False, "reason": "disk"})
        transitions = []
        for _ in range(3):
            transitions.extend(await controller.ticker.tick())
        states = [item for item in transitions
                  if item["unit_id"] == "store.eventlog"]
        self.assertIn("degraded", [item["to"] for item in states])
        self.assertIn("failed", [item["to"] for item in states])
        self.assertEqual(controller.state_of("store.eventlog"), "failed")
        controller.register_provider(
            "store.eventlog", probe=lambda: {"ok": True})
        for _ in range(2):
            await controller.ticker.tick()
        self.assertEqual(controller.state_of("store.eventlog"), "ready")
        events = [record.to_dict() for record in
                  self.app.eventlog.query(types=["unit.state_changed"],
                                          limit=500)]
        unit_ids = [event["data"].get("unit_id") for event in events]
        self.assertIn("store.eventlog", unit_ids)

    async def test_probe_timeout_is_reported(self):
        controller = self.controller
        controller.probe_timeout_s = 0.05
        unit = {unit["unit_id"]: unit for unit in
                self.app.unit_cards()}["skill.0"]

        async def slow():
            await asyncio.sleep(1.0)
            return {"ok": True}

        controller.register_provider("skill.0", probe=slow)
        result = await controller.run_probe("skill.0", unit)
        self.assertFalse(result["ok"])
        self.assertIn("timeout", result["reason"])


if __name__ == "__main__":
    unittest.main()

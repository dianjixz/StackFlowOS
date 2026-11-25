"""``GET /api/units`` contract: ``include``, state provenance and 403 refusal.

The audit found three web-plane gaps pinned here:

* ``?include=`` was accepted but ignored (docs/units-plan.md §6.1 defines
  ``descriptor``/``brief``); an unknown value must be an explicit 400.
* the list showed the instant probe state while ``/api/units/summary`` counted
  the hysteresis state machine, so both endpoints could disagree at the same
  instant.  The list now overlays the control-plane state and exposes
  ``state_source``/``state_checked_at``/``state_staleness_ms`` so the two can be
  reconciled.
* a viewer calling an operator-only control route got a 400 from the feature
  extension instead of a 403 permission refusal.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stackflowos.app import App
from stackflowos.config import Config, default_root
from stackflowos.web.api import WebAPI
from stackflowos.web.auth import AdmissionDecision

REPO = default_root()
ONLINE_STATES = {"ready", "degraded", "online", "suspect", "draining"}


class UnitsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        base = REPO / ".stackflowos" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(base))
        tmp = Path(self._tmp.name)
        cfg = Config.load(root=tmp, state_dir=tmp / "state")
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

    async def api_call(self, method, path, query=None, body=None, auth=None):
        return await self.api.handle(
            method, path, query or {}, json.dumps(body or {}).encode("utf-8"),
            auth=auth)

    @staticmethod
    def _viewer() -> AdmissionDecision:
        return AdmissionDecision.allow(authenticated=True, user="viewer",
                                       role="viewer")

    @staticmethod
    def _operator() -> AdmissionDecision:
        return AdmissionDecision.allow(authenticated=True, user="operator",
                                       role="operator")

    # -- include ---------------------------------------------------------
    async def test_unknown_include_is_an_explicit_400(self):
        status, payload = await self.api_call("GET", "/api/units",
                                              {"include": "banana"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "BAD_REQUEST")
        self.assertIn("include", payload["error"]["message"])
        self.assertEqual(payload["error"]["detail"]["allowed"],
                         ["brief", "descriptor"])

    async def test_include_descriptor_is_full_and_brief_trims_the_card(self):
        status, payload = await self.api_call("GET", "/api/units")
        self.assertEqual(status, 200)
        default_units = payload["data"]["units"]
        status, payload = await self.api_call(
            "GET", "/api/units", {"include": "descriptor"})
        self.assertEqual(status, 200)
        descriptor_units = payload["data"]["units"]
        self.assertEqual({u["unit_id"] for u in descriptor_units},
                         {u["unit_id"] for u in default_units})
        # Full descriptors keep the old flat fields the UI relies on.
        full = {u["unit_id"]: u for u in descriptor_units}
        self.assertIn("metadata", full["store.eventlog"])
        self.assertIn("provides", full["store.eventlog"])

        status, payload = await self.api_call(
            "GET", "/api/units", {"include": "brief"})
        self.assertEqual(status, 200)
        brief = {u["unit_id"]: u for u in payload["data"]["units"]}
        self.assertEqual(set(brief), set(full))
        card = brief["store.eventlog"]
        self.assertNotIn("metadata", card)
        self.assertNotIn("provides", card)
        for field in ("unit_id", "state", "status", "runtime", "lease",
                      "abilities", "state_source", "state_staleness_ms"):
            self.assertIn(field, card)

    async def test_conflicting_include_tokens_are_rejected(self):
        status, payload = await self.api_call(
            "GET", "/api/units", {"include": "brief,descriptor"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "BAD_REQUEST")

    # -- reconciliation with /api/units/summary --------------------------
    async def test_list_and_summary_use_the_same_controller_state(self):
        self.controller.sync_state("approval.0", "degraded", "disk probe")
        try:
            status, payload = await self.api_call("GET", "/api/units")
            self.assertEqual(status, 200)
            data = payload["data"]
            self.assertEqual(data["schema"], "stackflow.units/v1")
            self.assertIn("generated_at", data)
            units = data["units"]
            by_id = {unit["unit_id"]: unit for unit in units}
            approval = by_id["approval.0"]
            # The instantaneous card state would be "ready"; the controller
            # (hysteresis) has recorded "degraded" and the list must say so,
            # otherwise the two endpoints disagree at the same instant.
            self.assertEqual(approval["state"], "degraded")
            self.assertEqual(approval["status"], "degraded")
            self.assertEqual(approval["state_source"], "controller")
            self.assertIsInstance(approval["state_checked_at"], int)
            self.assertIsInstance(approval["state_staleness_ms"], int)
            self.assertGreaterEqual(approval["state_staleness_ms"], 0)

            status, summary_payload = await self.api_call(
                "GET", "/api/units/summary")
            self.assertEqual(status, 200)
            summary = summary_payload["data"]
            # The list and the summary are derived from the exact same cards
            # and the exact same effective state, so the counters reconcile.
            self.assertEqual(summary["total"], len(units))
            self.assertEqual(
                summary["online"],
                sum(1 for unit in units
                    if unit.get("state") in ONLINE_STATES))
            self.assertEqual(
                summary["degraded"],
                sum(1 for unit in units if unit.get("state") == "degraded"))
            self.assertEqual(
                summary["failed"],
                sum(1 for unit in units if unit.get("state") == "failed"))
            self.assertIn("approval.0",
                          [problem["unit_id"]
                           for problem in summary["problems"]])
        finally:
            self.controller.sync_state("approval.0", "ready", None)

    async def test_state_filter_uses_the_effective_state(self):
        self.controller.sync_state("approval.0", "degraded", "disk probe")
        try:
            status, payload = await self.api_call(
                "GET", "/api/units", {"state": "degraded"})
            self.assertEqual(status, 200)
            self.assertIn("approval.0",
                          [unit["unit_id"]
                           for unit in payload["data"]["units"]])
        finally:
            self.controller.sync_state("approval.0", "ready", None)

    async def test_probe_source_units_still_carry_staleness(self):
        # A registered worker has no control-plane record (probes skip
        # workers); it is the probe fallback path.
        from stackflowos.l2.registry import UnitInfo
        self.app.registry.register(UnitInfo(work_id="coder.99", kind="coder",
                                            label="Coder"))
        status, payload = await self.api_call(
            "GET", "/api/units", {"include": "brief"})
        self.assertEqual(status, 200)
        by_id = {unit.get("unit_id") or unit.get("work_id"): unit
                 for unit in payload["data"]["units"]}
        worker = by_id["coder.99"]
        self.assertNotIn("unit_id", worker)  # UnitInfo cards are keyed by work_id
        self.assertEqual(worker["state_source"], "probe")
        self.assertIn("state_checked_at", worker)
        self.assertIn("state_staleness_ms", worker)

    # -- operator-only routes -------------------------------------------
    async def test_viewer_control_write_is_403_not_400(self):
        status, payload = await self.api_call(
            "POST", "/api/units/approval.0/actions", {}, {"action": "probe"},
            auth=self._viewer())
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "DENIED")

        status, payload = await self.api_call(
            "POST", "/api/skills/alpha/enable", {}, {},
            auth=self._viewer())
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "DENIED")

    async def test_operator_and_in_process_calls_are_not_refused(self):
        status, payload = await self.api_call(
            "POST", "/api/units/approval.0/actions", {}, {"action": "probe"},
            auth=self._operator())
        self.assertNotEqual(status, 403)
        # No admission decision (tests/CLI in-process call) keeps the old
        # behaviour: the feature handler owns the check.
        status, payload = await self.api_call(
            "POST", "/api/units/approval.0/actions", {}, {"action": "probe"})
        self.assertNotEqual(status, 403)

    async def test_viewer_reads_stay_allowed(self):
        status, payload = await self.api_call("GET", "/api/units",
                                              auth=self._viewer())
        self.assertEqual(status, 200)
        self.assertIn("units", payload["data"])


if __name__ == "__main__":
    unittest.main()

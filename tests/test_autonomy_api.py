"""Tests for the autonomy HTTP surface (``stackflowos/autonomy/api.py``).

Two layers:
- a recording fake service drives the handlers directly, so the route shapes,
  ``dry_run`` parsing and authorization rules are pinned without a hub;
- a real ``App`` (temp state dir) + ``WebAPI`` proves the extension registers
  ``/api/autonomy*`` for real and that ``PATCH /policy`` persists via
  ``App.update_config``.

Auth decisions are handed to ``WebAPI.handle(..., auth=...)`` exactly like
``stackflowos/web/server.py`` does after admission.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from stackflowos.app import App
from stackflowos.autonomy.api import build_routes
from stackflowos.config import Config
from stackflowos.errors import BadRequest, Denied
from stackflowos.web.api import WebAPI, _current_auth
from stackflowos.web.auth import AdmissionDecision

OPERATOR = AdmissionDecision.allow(authenticated=True, role="operator")
VIEWER = AdmissionDecision.allow(authenticated=True, role="viewer")
ANONYMOUS = AdmissionDecision.allow(authenticated=False)


class _FakeService:
    """Records calls; returns fixed payloads the tests can assert on."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.report: dict = {
            "seq": 3,
            "trigger": "operator",
            "findings": [],
            "summary": {"critical": 0, "warn": 0, "info": 0},
            "dispatched": [],
            "approvals": [],
        }
        self.status_value: dict = {
            "enabled": False,
            "running": False,
            "policy": {"enabled": False, "dispatch_mode": "off"},
            "counts": {"patrols": 0},
            "findings": [],
            "report": None,
        }
        self.configure_error: Exception | None = None

    def status(self) -> dict:
        self.calls.append(("status",))
        return dict(self.status_value)

    async def run_patrol(self, *, trigger: str,
                         dry_run: bool = False) -> dict:
        self.calls.append(("run_patrol", trigger, dry_run))
        return dict(self.report)

    async def configure(self, patch: dict) -> dict:
        self.calls.append(("configure", dict(patch)))
        if self.configure_error is not None:
            raise self.configure_error
        self.status_value = {
            **self.status_value,
            "policy": {**self.status_value["policy"], **patch},
        }
        return dict(self.status_value)


def _handler(routes: list[tuple[str, str, Any]], method: str,
             path: str) -> Any:
    for route_method, pattern, handler in routes:
        if route_method == method and re.fullmatch(pattern, path):
            return handler
    raise AssertionError(f"route not built: {method} {path}")


async def _call(handler: Any, *, auth: Any = OPERATOR, query: dict | None = None,
                body: Any = None) -> tuple[int, dict]:
    """Invoke a handler directly with the per-request auth context set."""
    token = _current_auth.set(auth)
    try:
        return await handler(None, query if query is not None else {},
                             body if body is not None else {})
    finally:
        _current_auth.reset(token)


class _FakeExtension:
    name = "autonomy"

    def __init__(self, service: _FakeService) -> None:
        self.service = service

    def routes(self) -> list[tuple[str, str, Any]]:
        return build_routes(self.service)


class _FakeApp:
    def __init__(self, service: _FakeService) -> None:
        self.extensions = [_FakeExtension(service)]


class FakeServiceRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = _FakeService()
        self.routes = build_routes(self.service)

    async def test_get_routes_return_status_envelope(self) -> None:
        for path in ("/api/autonomy", "/api/autonomy/status"):
            with self.subTest(path=path):
                status, payload = await _call(
                    _handler(self.routes, "GET", path))
                self.assertEqual(status, 200)
                self.assertEqual(payload, {"ok": True,
                                           "data": self.service.status_value})
        self.assertEqual(self.service.calls, [("status",), ("status",)])

    async def test_patrol_defaults_to_operator_trigger_without_dry_run(self) -> None:
        status, payload = await _call(
            _handler(self.routes, "POST", "/api/autonomy/patrol"), body={})
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "data": self.service.report})
        self.assertEqual(self.service.calls,
                         [("run_patrol", "operator", False)])

    async def test_patrol_parses_dry_run_strictly(self) -> None:
        cases = [(True, True), (False, False), ("true", True),
                 ("false", False), ("on", True), ("off", False), (0, False)]
        handler = _handler(self.routes, "POST", "/api/autonomy/patrol")
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.service.calls.clear()
                status, payload = await _call(handler,
                                              body={"dry_run": raw})
                self.assertEqual(status, 200)
                self.assertEqual(payload["data"], self.service.report)
                self.assertEqual(self.service.calls,
                                 [("run_patrol", "operator", expected)])

    async def test_patrol_rejects_non_boolean_dry_run(self) -> None:
        handler = _handler(self.routes, "POST", "/api/autonomy/patrol")
        for raw in ("maybe", 2, [], {"nested": True}):
            with self.subTest(raw=raw):
                with self.assertRaises(BadRequest):
                    await _call(handler, body={"dry_run": raw})
        self.assertEqual(self.service.calls, [])

    async def test_policy_configures_and_returns_fresh_status(self) -> None:
        status, payload = await _call(
            _handler(self.routes, "PATCH", "/api/autonomy/policy"),
            body={"dispatch_mode": "tasks"})
        self.assertEqual(status, 200)
        self.assertIn(("configure", {"dispatch_mode": "tasks"}),
                      self.service.calls)
        # The reply is re-read *after* configure, not the pre-patch view.
        self.assertEqual(self.service.calls[-1], ("status",))
        self.assertEqual(payload["data"]["policy"]["dispatch_mode"], "tasks")

    async def test_policy_requires_a_json_object(self) -> None:
        handler = _handler(self.routes, "PATCH", "/api/autonomy/policy")
        for body in ([], "x", 1):
            with self.subTest(body=body):
                with self.assertRaises(BadRequest):
                    await _call(handler, body=body)

    async def test_authentication_required_for_every_route(self) -> None:
        cases = [("GET", "/api/autonomy", {}),
                 ("GET", "/api/autonomy/status", {}),
                 ("POST", "/api/autonomy/patrol", {}),
                 ("PATCH", "/api/autonomy/policy", {"enabled": False})]
        for path_auth in (None, ANONYMOUS):
            for method, path, body in cases:
                with self.subTest(auth=path_auth, method=method, path=path):
                    with self.assertRaises(Denied) as caught:
                        await _call(_handler(self.routes, method, path),
                                    auth=path_auth, body=body)
                    self.assertEqual(caught.exception.message,
                                     "authentication required")

    async def test_viewer_can_read_but_not_write(self) -> None:
        status, payload = await _call(
            _handler(self.routes, "GET", "/api/autonomy"), auth=VIEWER)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        for method, path, body in (
                ("POST", "/api/autonomy/patrol", {}),
                ("PATCH", "/api/autonomy/policy", {"enabled": False})):
            with self.subTest(method=method, url=path):
                with self.assertRaises(Denied) as caught:
                    await _call(_handler(self.routes, method, path),
                                auth=VIEWER, body=body)
                self.assertEqual(caught.exception.message,
                                 "operator role required")
        self.assertEqual(self.service.calls, [("status",)])

    async def test_webapi_maps_autonomy_errors_to_http(self) -> None:
        # Goes through the real extension-route registration path.
        api = WebAPI(_FakeApp(self.service))

        status, payload = await api.handle(
            "GET", "/api/autonomy", {}, b"", auth=None)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "DENIED")
        self.assertEqual(payload["error"]["message"],
                         "authentication required")

        status, payload = await api.handle(
            "POST", "/api/autonomy/patrol", {}, b"{}", auth=VIEWER)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["message"],
                         "operator role required")

        status, payload = await api.handle(
            "POST", "/api/autonomy/patrol", {},
            json.dumps({"dry_run": "maybe"}).encode(), auth=OPERATOR)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "BAD_REQUEST")
        self.assertNotIn("internal", payload["error"]["message"].lower())

        self.service.configure_error = BadRequest(
            "unknown autonomy policy fields: ['nope']")
        status, payload = await api.handle(
            "PATCH", "/api/autonomy/policy", {},
            json.dumps({"nope": 1}).encode(), auth=OPERATOR)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "BAD_REQUEST")
        self.assertIn("unknown autonomy policy fields",
                      payload["error"]["message"])

        self.service.configure_error = None
        status, payload = await api.handle(
            "POST", "/api/autonomy/patrol", {}, b"{}", auth=OPERATOR)
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"], self.service.report)


class AutonomyAppApiTests(unittest.IsolatedAsyncioTestCase):
    """Real App + WebAPI: extension loading, persistence and auth contract."""

    async def asyncSetUp(self) -> None:
        # Temp-only state: never touch the repo's .stackflowos or the live hub.
        self._tmp = tempfile.TemporaryDirectory(prefix="sfo-autonomy-api-")
        self.root = Path(self._tmp.name)
        self.state_dir = self.root / "state"
        self.app = App(Config.load(root=self.root, state_dir=self.state_dir))
        await self.app.start()
        self.api = WebAPI(self.app)

    async def asyncTearDown(self) -> None:
        await self.app.stop()
        self._tmp.cleanup()

    def _assert_status_shape(self, data: dict) -> None:
        for key in ("enabled", "policy", "last_run", "next_run_in_s",
                    "running", "counts", "findings", "report", "history",
                    "dispatches", "approvals", "guardrail"):
            self.assertIn(key, data)
        self.assertFalse(data["enabled"])
        self.assertIsInstance(data["policy"], dict)
        self.assertIsInstance(data["findings"], list)

    async def test_operator_reads_status_through_real_routes(self) -> None:
        self.assertIsNotNone(getattr(self.app, "autonomy", None),
                             "autonomy extension must load")
        for path in ("/api/autonomy", "/api/autonomy/status"):
            with self.subTest(path=path):
                status, payload = await self.api.handle(
                    "GET", path, {}, b"", auth=OPERATOR)
                self.assertEqual(status, 200, payload)
                self.assertTrue(payload["ok"])
                self._assert_status_shape(payload["data"])
        # The extension route, not a 404, served the request.
        self.assertEqual(
            self.app.config.autonomy.enabled, payload["data"]["enabled"])

    async def test_operator_patch_policy_persists_and_hot_applies(self) -> None:
        status, payload = await self.api.handle(
            "PATCH", "/api/autonomy/policy", {},
            json.dumps({"dispatch_mode": "tasks", "interval_s": 300.0}).encode(),
            auth=OPERATOR)
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["policy"]["dispatch_mode"], "tasks")
        self.assertEqual(data["policy"]["interval_s"], 300.0)
        # Hot-applied on the live object ...
        self.assertEqual(self.app.config.autonomy.dispatch_mode, "tasks")
        self.assertEqual(self.app.autonomy.cfg.dispatch_mode, "tasks")
        # ... and persisted to state/config.json (survives a reload).
        persisted = json.loads(
            (self.state_dir / "config.json").read_text("utf-8"))
        self.assertEqual(persisted["autonomy"]["dispatch_mode"], "tasks")
        reloaded = Config.load(root=self.root, state_dir=self.state_dir)
        self.assertEqual(reloaded.autonomy.dispatch_mode, "tasks")
        # A subsequent read reflects the new policy too.
        status, payload = await self.api.handle(
            "GET", "/api/autonomy/status", {}, b"", auth=OPERATOR)
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["policy"]["dispatch_mode"], "tasks")

    async def test_operator_runs_manual_patrol_as_dry_run(self) -> None:
        seen: list[dict] = []
        original = self.app.autonomy.run_patrol

        async def _spy(*args: Any, **kwargs: Any) -> dict:
            seen.append(dict(kwargs))
            return await original(*args, **kwargs)

        self.app.autonomy.run_patrol = _spy
        status, payload = await self.api.handle(
            "POST", "/api/autonomy/patrol", {},
            json.dumps({"dry_run": True}).encode(), auth=OPERATOR)
        self.assertEqual(status, 200, payload)
        report = payload["data"]
        self.assertIsInstance(report, dict)
        for key in ("seq", "trigger", "findings", "summary", "dispatched",
                    "approvals", "error"):
            self.assertIn(key, report)
        self.assertEqual(report["trigger"], "operator")
        self.assertGreaterEqual(report["seq"], 1)
        self.assertEqual(seen, [{"trigger": "operator", "dry_run": True}])
        # Dry run never dispatches, even if the patrol itself failed.
        self.assertEqual(report["dispatched"], [])
        self.assertEqual(report["approvals"], [])
        self.assertGreater(self.app.autonomy.status()["last_run"], 0)

    async def test_policy_validation_errors_are_400_not_500(self) -> None:
        cases = [
            ({}, "non-empty"),
            ({"unknown_field": 1}, "unknown autonomy policy fields"),
            ({"dispatch_mode": "bogus"}, "dispatch_mode"),
        ]
        for patch, needle in cases:
            with self.subTest(patch=patch):
                status, payload = await self.api.handle(
                    "PATCH", "/api/autonomy/policy", {},
                    json.dumps(patch).encode(), auth=OPERATOR)
                self.assertEqual(status, 400, payload)
                self.assertFalse(payload["ok"])
                self.assertIn(payload["error"]["code"],
                              ("BAD_REQUEST", "CONFIG_INVALID"))
                self.assertIn(needle, payload["error"]["message"])
        # Nothing invalid leaked into the live config or onto disk.
        self.assertEqual(self.app.config.autonomy.dispatch_mode, "off")
        reloaded = Config.load(root=self.root, state_dir=self.state_dir)
        self.assertEqual(reloaded.autonomy.dispatch_mode, "off")

    async def test_bad_patrol_body_is_400_without_running_a_patrol(self) -> None:
        status, payload = await self.api.handle(
            "POST", "/api/autonomy/patrol", {},
            json.dumps({"dry_run": "sometimes"}).encode(), auth=OPERATOR)
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["code"], "BAD_REQUEST")
        self.assertEqual(self.app.autonomy.status()["last_run"], 0)

    async def test_reads_need_auth_and_writes_need_operator(self) -> None:
        status, payload = await self.api.handle(
            "GET", "/api/autonomy", {}, b"", auth=None)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "DENIED")
        self.assertEqual(payload["error"]["message"],
                         "authentication required")

        # Viewer may read ...
        status, payload = await self.api.handle(
            "GET", "/api/autonomy", {}, b"", auth=VIEWER)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        # ... but never patch the policy or trigger a patrol.
        for method, path, body in (
                ("PATCH", "/api/autonomy/policy",
                 json.dumps({"enabled": False}).encode()),
                ("POST", "/api/autonomy/patrol",
                 json.dumps({"dry_run": True}).encode())):
            with self.subTest(method=method, path=path):
                status, payload = await self.api.handle(
                    method, path, {}, body, auth=VIEWER)
                self.assertEqual(status, 403, payload)
                self.assertEqual(payload["error"]["message"],
                                 "operator role required")
        self.assertEqual(self.app.config.autonomy.dispatch_mode, "off")
        self.assertEqual(self.app.autonomy.status()["last_run"], 0)

    async def test_anonymous_decision_cannot_read(self) -> None:
        status, payload = await self.api.handle(
            "GET", "/api/autonomy", {}, b"", auth=ANONYMOUS)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["message"],
                         "authentication required")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

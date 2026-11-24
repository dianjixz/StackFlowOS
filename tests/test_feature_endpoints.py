"""End-to-end checks for the optional subsystem HTTP surface.

These boot a real ``App`` (in a temp state dir) with every extension present and
issue the requests the UI/CLI depend on.  They intentionally assert on status
classes rather than exact payloads: each subsystem owns its schema, but a
missing route (404) or an unhandled exception (500) is a wiring bug that must
fail the build.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from stackflowos.app import App
from stackflowos.config import Config, default_root
from stackflowos.web.server import WebServer

REPO = default_root()


class FeatureEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        base = REPO / ".stackflowos" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(base))
        self.tmp = Path(self._tmp.name)
        self.app = App(Config.load(root=self.tmp, state_dir=self.tmp / "state"))
        await self.app.start()
        self.server = WebServer(self.app, host="127.0.0.1", port=0,
                                assets_dir=REPO / "stackflowos" / "webassets")
        self.url = await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()
        await self.app.stop()
        self._tmp.cleanup()

    async def request(self, path: str, method: str = "GET",
                      data: dict | None = None) -> tuple[int, object]:
        def call():
            req = urllib.request.Request(self.url + path, method=method)
            req.add_header("Authorization",
                           f"Bearer {self.server.launch_token}")
            if method not in ("GET", "HEAD"):
                req.add_header("X-SFO-Request", "1")
            body = None
            if data is not None:
                req.add_header("Content-Type", "application/json")
                body = json.dumps(data).encode()
            try:
                with urllib.request.urlopen(req, body, timeout=10) as resp:
                    raw = resp.read()
                    ctype = resp.headers.get("Content-Type", "")
                    if "json" in ctype:
                        return resp.status, json.loads(raw or b"{}")
                    return resp.status, raw
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                try:
                    return exc.code, json.loads(raw or b"{}")
                except ValueError:
                    return exc.code, raw

        return await asyncio.to_thread(call)

    async def assert_endpoint(self, path: str, method: str = "GET",
                              data: dict | None = None) -> object:
        status, payload = await self.request(path, method, data)
        self.assertNotIn(status, (404, 500, 501),
                         f"{method} {path} -> {status} {payload}")
        return payload

    async def test_capability_and_registry_endpoints(self):
        payload = await self.assert_endpoint("/api/capabilities?type=tool")
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            entries = data.get("capabilities") or data.get("entries") or []
        else:
            entries = data if isinstance(data, list) else []
        self.assertTrue(entries, f"tool catalogue must not be empty: {payload}")
        self.assertTrue(any(item.get("name") == "fs.read" for item in entries))
        await self.assert_endpoint("/api/skills")
        await self.assert_endpoint("/api/plugins")
        await self.assert_endpoint("/api/mcp/servers")
        await self.assert_endpoint("/api/mcp/tools")

    async def test_unit_detail_and_actions(self):
        cards = {unit["unit_id"]: unit for unit in self.app.unit_cards()}
        self.assertIn("approval.0", cards)
        detail = await self.assert_endpoint("/api/units/approval.0")
        if isinstance(detail, dict) and isinstance(detail.get("data"), dict):
            self.assertIn("requires", detail["data"])
        status, payload = await self.request("/api/units/approval.0/probe",
                                             "POST", {})
        self.assertNotIn(status, (404, 500))
        status, payload = await self.request("/api/units/skill.0/actions",
                                             "POST", {"action": "probe"})
        self.assertNotIn(status, (404, 500))

    async def test_observability_llm_agent_endpoints(self):
        payload = await self.assert_endpoint("/api/metrics")
        self.assertIn(b"sfo_up", payload if isinstance(payload, bytes)
                      else str(payload).encode())
        await self.assert_endpoint("/api/llm/usage")
        await self.assert_endpoint("/api/llm/models")
        await self.assert_endpoint("/api/agents/queue")
        await self.assert_endpoint("/api/sessions")
        await self.assert_endpoint("/api/discovery/peers")

    async def test_sessions_rotate_and_logout(self):
        status, payload = await self.request("/api/sessions/rotate", "POST", {})
        self.assertNotIn(status, (404, 500))
        status, payload = await self.request("/api/logout", "POST", {})
        self.assertNotIn(status, (404, 500))

    async def test_capability_call_bridge(self):
        # An unknown server must fail closed with a structured 404, not 500.
        status, payload = await self.request("/api/mcp/call", "POST", {
            "server": "missing", "tool": "missing", "args": {}})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "NOT_FOUND")

    async def test_skill_enable_roundtrip(self):
        status, payload = await self.request("/api/skills", "GET")
        self.assertEqual(status, 200)
        skills = payload.get("data", {}).get("skills", [])
        if not skills:
            return
        skill_id = skills[0]["id"]
        await self.assert_endpoint(f"/api/skills/{skill_id}/disable", "POST", {})


if __name__ == "__main__":
    unittest.main()


class ControlSocketIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_url_mints_single_use_login(self):
        import asyncio
        import urllib.error
        import urllib.request

        from stackflowos.app import App
        from stackflowos.config import Config, default_root
        from stackflowos.security.control import (control_socket_path,
                                                  query_control_socket)
        from stackflowos.web.server import WebServer

        base = default_root() / ".stackflowos" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=str(base))
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name) / "state"
        app = App(Config.load(root=Path(tmp.name), state_dir=state))
        await app.start()
        server = WebServer(app, host="127.0.0.1", port=0)
        url = await server.start()
        try:
            # The control server runs on this test's event loop; the client
            # is blocking, so it must go to a worker thread.
            reply = await asyncio.to_thread(
                query_control_socket,
                control_socket_path(app.paths.run_dir), "url")
            self.assertTrue(reply.get("ok"), reply)
            login_url = reply["url"]
            self.assertIn("token=", login_url)
            self.assertTrue(login_url.startswith(url), login_url)

            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None

            def exchange(link: str):
                # Capture the 303 itself: following it would need the cookie
                # jar that a real browser has.
                opener = urllib.request.build_opener(NoRedirect())
                try:
                    opener.open(link, timeout=5)
                    return None
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.headers.get("Set-Cookie")

            first = await asyncio.to_thread(exchange, login_url)
            self.assertEqual(first[0], 303)
            self.assertIn("sfo_session=", first[1] or "")
            second = await asyncio.to_thread(exchange, login_url)
            self.assertEqual(second[0], 401,
                             "a consumed launch token must not be reusable")
        finally:
            await server.stop()
            await app.stop()

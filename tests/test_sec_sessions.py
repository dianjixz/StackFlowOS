"""Server-side session table plus /api/sessions* extension routes."""

from __future__ import annotations

import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from stackflowos.config import default_root
from stackflowos.security.sessions import (
    ROLE_OPERATOR,
    ROLE_VIEWER,
    SessionLimitError,
    SessionManager,
    build as build_sessions_extension,
)
from stackflowos.web.server import WebServer

from tests.test_sec_support import (
    FakeApp,
    FakeConfig,
    FakeWeb,
    bearer,
    make_assets,
    raw_request,
    write_headers,
)

REPO = default_root()


class Clock:
    def __init__(self, value: float = 1_000.0):
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


class SessionManagerTests(unittest.TestCase):
    def test_create_get_touch_and_expiry(self):
        clock = Clock()
        manager = SessionManager(max_sessions=4, ttl_s=100, now=clock)
        session = manager.create("alice", ROLE_OPERATOR, authority="h:1")
        self.assertEqual(session.user, "alice")
        self.assertEqual(session.role, ROLE_OPERATOR)
        self.assertEqual(manager.count(), 1)
        got = manager.get(session.id)
        self.assertIsNotNone(got)
        clock.value += 50
        touched = manager.get(session.id)
        self.assertEqual(touched.last_seen, 1_050.0)
        clock.value += 101
        self.assertIsNone(manager.get(session.id))
        self.assertEqual(manager.count(), 0)

    def test_revoke_and_revoke_user(self):
        manager = SessionManager(max_sessions=8)
        a1 = manager.create("alice", ROLE_OPERATOR)
        a2 = manager.create("alice", ROLE_VIEWER)
        b1 = manager.create("bob", ROLE_VIEWER)
        self.assertTrue(manager.revoke(a1.id))
        self.assertFalse(manager.revoke(a1.id))
        self.assertEqual(manager.revoke_user("alice"), 1)
        self.assertEqual([row["id"] for row in manager.list_sessions()],
                         [b1.id])
        self.assertIsNone(manager.get(a2.id, touch=False))

    def test_rotate_creates_new_id_and_revokes_old(self):
        manager = SessionManager(max_sessions=2, ttl_s=100)
        old = manager.create("alice", ROLE_OPERATOR, authority="h:1")
        new = manager.rotate(old.id)
        self.assertIsNotNone(new)
        self.assertNotEqual(new.id, old.id)
        self.assertEqual((new.user, new.role, new.authority),
                         ("alice", ROLE_OPERATOR, "h:1"))
        self.assertIsNone(manager.get(old.id, touch=False))
        self.assertIsNotNone(manager.get(new.id, touch=False))
        self.assertIsNone(manager.rotate("does-not-exist"))

    def test_limit_is_fail_closed(self):
        manager = SessionManager(max_sessions=2)
        manager.create("a", ROLE_OPERATOR)
        manager.create("b", ROLE_OPERATOR)
        with self.assertRaises(SessionLimitError):
            manager.create("c", ROLE_OPERATOR)

    def test_role_validation(self):
        manager = SessionManager()
        with self.assertRaises(Exception):
            manager.create("x", "admin")
        with self.assertRaises(Exception):
            manager.create("  ", ROLE_OPERATOR)

    def test_thread_safe_creation(self):
        manager = SessionManager(max_sessions=64)

        def make(index: int) -> str:
            return manager.create(f"user-{index}", ROLE_VIEWER).id

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(make, range(32)))
        self.assertEqual(len(ids), 32)
        self.assertEqual(len(set(ids)), 32)
        self.assertEqual(manager.count(), 32)

    def test_public_dict_has_no_token_or_signature(self):
        manager = SessionManager()
        row = manager.create("alice", ROLE_OPERATOR).to_public_dict()
        self.assertEqual(
            set(row) & {"token", "cookie", "signature", "nonce"}, set())


class SessionRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.app = FakeApp(self.tmp)
        self.extension = build_sessions_extension(self.app)
        self.manager = self.app.security_sessions
        self.app.extensions = [self.extension]
        self.assets = make_assets(self.tmp)
        self.server = WebServer(self.app, host="127.0.0.1", port=0,
                                assets_dir=self.assets)
        await self.server.start()
        self.port = self.server.port

    async def asyncTearDown(self):
        await self.server.stop()
        await self.app.eventlog.close()
        self._tmp.cleanup()

    async def login(self, token: str | None = None) -> str:
        token = self.server.launch_token if token is None else token
        status, headers, body = await raw_request(
            self.port, "GET", f"/?token={token}")
        self.assertEqual(status, 303, body)
        return headers["set-cookie"].split(";", 1)[0]

    async def test_unauthenticated_sessions_route_is_401(self):
        status, _, body = await raw_request(self.port, "GET", "/api/sessions")
        self.assertEqual(status, 401, body)

    async def test_operator_can_list_sessions(self):
        cookie = await self.login()
        status, _, body = await raw_request(
            self.port, "GET", "/api/sessions", headers={"Cookie": cookie})
        self.assertEqual(status, 200, body)
        import json
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        sessions = payload["data"]["sessions"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["user"], "launch-token")
        self.assertEqual(sessions[0]["role"], ROLE_OPERATOR)
        self.assertNotIn(self.server.launch_token,
                         json.dumps(payload))

    async def test_rotate_invalidates_old_cookie_and_issues_new_one(self):
        cookie = await self.login()
        status, headers, body = await raw_request(
            self.port, "POST", "/api/sessions/rotate",
            headers={"Cookie": cookie, **write_headers()}, body=b"{}")
        self.assertEqual(status, 200, body)
        new_cookie = headers["set-cookie"].split(";", 1)[0]
        self.assertNotEqual(new_cookie, cookie)
        # Old cookie no longer maps to a live session.
        status, _, body = await raw_request(
            self.port, "GET", "/api/sessions", headers={"Cookie": cookie})
        self.assertEqual(status, 401, body)
        status, _, body = await raw_request(
            self.port, "GET", "/api/sessions", headers={"Cookie": new_cookie})
        self.assertEqual(status, 200, body)

    async def test_delete_session_by_id(self):
        first = await self.login()
        second = await self.login(self.server.auth.mint_launch_token())
        status, _, body = await raw_request(
            self.port, "GET", "/api/sessions", headers={"Cookie": first})
        import json
        sessions = json.loads(body)["data"]["sessions"]
        self.assertEqual(len(sessions), 2)
        target = [row for row in sessions
                  if row["id"] != self._session_id(first)][0]
        status, _, body = await raw_request(
            self.port, "DELETE", f"/api/sessions/{target['id']}",
            headers={"Cookie": first, **write_headers()})
        self.assertEqual(status, 200, body)
        status, _, body = await raw_request(
            self.port, "GET", "/api/sessions", headers={"Cookie": second})
        self.assertEqual(status, 401, body)  # the second cookie was revoked
        self.assertEqual(self.manager.count(), 1)

    async def test_delete_unknown_session_is_404(self):
        cookie = await self.login()
        status, _, body = await raw_request(
            self.port, "DELETE", "/api/sessions/does-not-exist",
            headers={"Cookie": cookie, **write_headers()})
        self.assertEqual(status, 404, body)

    async def test_logout_clears_cookie_and_revokes_session(self):
        cookie = await self.login()
        status, headers, body = await raw_request(
            self.port, "POST", "/api/logout",
            headers={"Cookie": cookie, **write_headers()}, body=b"{}")
        self.assertEqual(status, 200, body)
        self.assertIn("Max-Age=0", headers["set-cookie"])
        self.assertEqual(self.manager.count(), 0)
        status, _, _ = await raw_request(
            self.port, "GET", "/api/sessions", headers={"Cookie": cookie})
        self.assertEqual(status, 401)

    async def test_session_limit_returns_503(self):
        await self.login()
        self.assertEqual(self.manager.count(), 1)
        # Tighten the bound after the first login: the next exchange must fail
        # closed instead of evicting a live session or pretending success.
        self.manager.max_sessions = 1
        status, _, body = await raw_request(
            self.port, "GET",
            f"/?token={self.server.auth.mint_launch_token()}")
        self.assertEqual(status, 503, body)
        import json
        self.assertEqual(json.loads(body)["error"]["code"], "UNAVAILABLE")
        self.assertEqual(self.manager.count(), 1)

    def _session_id(self, cookie: str) -> str:
        value = cookie.split("=", 1)[1]
        claims = self.server.auth.cookie_claims(
            value, f"127.0.0.1:{self.port}")
        assert claims is not None
        return claims.session_id


class ViewerSessionTests(unittest.IsolatedAsyncioTestCase):
    VIEWER_TOKEN = "viewer-account-token-0123456789"

    async def asyncSetUp(self):
        import hashlib

        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.app = FakeApp(self.tmp)
        self.extension = build_sessions_extension(self.app)
        self.manager = self.app.security_sessions
        self.app.extensions = [self.extension]
        self.assets = make_assets(self.tmp)
        self.users = [{
            "name": "bob", "role": "viewer",
            "token_sha256": hashlib.sha256(
                self.VIEWER_TOKEN.encode()).hexdigest()}]
        self.server = WebServer(self.app, host="127.0.0.1", port=0,
                                assets_dir=self.assets, users=self.users)
        await self.server.start()
        self.port = self.server.port

    async def asyncTearDown(self):
        await self.server.stop()
        await self.app.eventlog.close()
        self._tmp.cleanup()

    async def login(self) -> str:
        status, headers, body = await raw_request(
            self.port, "GET", f"/?token={self.VIEWER_TOKEN}")
        self.assertEqual(status, 303, body)
        return headers["set-cookie"].split(";", 1)[0]

    async def test_viewer_sees_only_own_session(self):
        first = await self.login()
        status, _, body = await raw_request(
            self.port, "GET", "/api/sessions", headers={"Cookie": first})
        self.assertEqual(status, 200, body)
        import json
        rows = json.loads(body)["data"]["sessions"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user"], "bob")
        self.assertEqual(rows[0]["role"], ROLE_VIEWER)

    async def test_viewer_cannot_rotate_or_revoke(self):
        cookie = await self.login()
        status, _, _ = await raw_request(
            self.port, "POST", "/api/sessions/rotate",
            headers={"Cookie": cookie, **write_headers()}, body=b"{}")
        self.assertEqual(status, 403)
        status, _, _ = await raw_request(
            self.port, "DELETE", "/api/sessions/anything",
            headers={"Cookie": cookie, **write_headers()})
        self.assertEqual(status, 403)

    async def test_viewer_can_log_out(self):
        cookie = await self.login()
        status, headers, body = await raw_request(
            self.port, "POST", "/api/logout",
            headers={"Cookie": cookie, **write_headers()}, body=b"{}")
        self.assertEqual(status, 200, body)
        self.assertIn("Max-Age=0", headers["set-cookie"])
        self.assertEqual(self.manager.count(), 0)

    async def test_bearer_has_no_session_to_rotate(self):
        status, _, body = await raw_request(
            self.port, "POST", "/api/sessions/rotate",
            headers={**bearer(self.VIEWER_TOKEN), **write_headers()},
            body=b"{}")
        # Bearer viewer is stopped by the server-level read-only rule first.
        self.assertEqual(status, 403, body)


if __name__ == "__main__":
    unittest.main()

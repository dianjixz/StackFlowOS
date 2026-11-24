"""Single-use launch tokens, minted URLs and multi-user bearer accounts."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from stackflowos.config import default_root
from stackflowos.security.sessions import ROLE_OPERATOR, ROLE_VIEWER
from stackflowos.web.server import WebServer

from tests.test_sec_support import (
    FakeApp,
    bearer,
    make_assets,
    raw_request,
    write_headers,
)

REPO = default_root()


def _user(name: str, token: str, role: str) -> dict:
    return {"name": name, "role": role,
            "token_sha256": hashlib.sha256(token.encode()).hexdigest()}


async def _start_server(test, *, users=(), single_use=True, session_max=32):
    test._tmp = tempfile.TemporaryDirectory(
        dir=str(REPO / ".stackflowos" / "tmp"))
    test.tmp = Path(test._tmp.name)
    test.app = FakeApp(test.tmp)
    test.assets = make_assets(test.tmp)
    test.server = WebServer(
        test.app, host="127.0.0.1", port=0, assets_dir=test.assets,
        users=users, session_max=session_max,
        launch_token_single_use=single_use)
    await test.server.start()
    test.port = test.server.port
    return test.server


async def _stop_server(test):
    await test.server.stop()
    await test.app.eventlog.close()
    test._tmp.cleanup()


class LaunchTokenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await _start_server(self)

    async def asyncTearDown(self):
        await _stop_server(self)

    async def test_base_token_is_single_use_for_url_exchange(self):
        token = self.server.launch_token
        status, headers, _ = await raw_request(
            self.port, "GET", f"/?token={token}")
        self.assertEqual(status, 303)
        self.assertIn("sfo_session=", headers["set-cookie"])
        status, _, body = await raw_request(
            self.port, "GET", f"/?token={token}")
        self.assertEqual(status, 401, body)
        self.assertIn(b"UNAUTHORIZED", body)

    async def test_bearer_escape_hatch_survives_exchange(self):
        token = self.server.launch_token
        self.assertEqual((await raw_request(
            self.port, "GET", f"/?token={token}"))[0], 303)
        status, _, body = await raw_request(
            self.port, "GET", "/api/state", headers=bearer(token))
        self.assertEqual(status, 200, body)
        # A consumed token is not exchangeable again.
        self.assertEqual((await raw_request(
            self.port, "GET", f"/?token={token}"))[0], 401)

    async def test_minted_token_is_single_use_and_urls_differ(self):
        base_url = self.server.login_url
        first = self.server.mint_login_url()
        second = self.server.mint_login_url()
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, base_url)
        token = first.split("token=", 1)[1]
        status, _, _ = await raw_request(self.port, "GET", f"/?token={token}")
        self.assertEqual(status, 303)
        status, _, _ = await raw_request(self.port, "GET", f"/?token={token}")
        self.assertEqual(status, 401)

    async def test_invalid_token_requests_do_not_consume_the_real_token(self):
        token = self.server.launch_token
        # Wrong path: rejected before exchange.
        status, _, _ = await raw_request(
            self.port, "GET", f"/api/state?token={token}")
        self.assertEqual(status, 401)
        # Non-GET: rejected before exchange.
        status, _, _ = await raw_request(
            self.port, "POST", f"/?token={token}",
            headers=write_headers(), body=b"{}")
        self.assertEqual(status, 401)
        # The token is still usable exactly once on the legitimate path.
        status, _, _ = await raw_request(self.port, "GET", f"/?token={token}")
        self.assertEqual(status, 303)

    async def test_multiple_tokens_are_rejected_without_consuming(self):
        token = self.server.launch_token
        status, _, _ = await raw_request(
            self.port, "GET", f"/?token={token}&token={token}")
        self.assertEqual(status, 400)
        status, _, _ = await raw_request(self.port, "GET", f"/?token={token}")
        self.assertEqual(status, 303)

    async def test_single_use_can_be_disabled(self):
        await _stop_server(self)
        await _start_server(self, single_use=False)
        token = self.server.launch_token
        for _ in range(2):
            status, _, _ = await raw_request(
                self.port, "GET", f"/?token={token}")
            self.assertEqual(status, 303)

    async def test_login_url_contains_the_process_token(self):
        self.assertEqual(
            self.server.login_url,
            f"http://127.0.0.1:{self.port}/?token={self.server.launch_token}")


class MultiUserTests(unittest.IsolatedAsyncioTestCase):
    VIEWER = "viewer-token-0123456789"
    OPERATOR = "operator-token-9876543210"

    async def asyncSetUp(self):
        self.users = [_user("alice", self.OPERATOR, "operator"),
                      _user("bob", self.VIEWER, "viewer")]
        await _start_server(self, users=self.users)

    async def asyncTearDown(self):
        await _stop_server(self)

    async def _login(self, token: str) -> str:
        status, headers, body = await raw_request(
            self.port, "GET", f"/?token={token}")
        self.assertEqual(status, 303, body)
        return headers["set-cookie"].split(";", 1)[0]

    async def test_viewer_bearer_is_read_only(self):
        status, _, body = await raw_request(
            self.port, "GET", "/api/state", headers=bearer(self.VIEWER))
        self.assertEqual(status, 200, body)
        status, _, body = await raw_request(
            self.port, "POST", "/api/projects",
            headers={**bearer(self.VIEWER), **write_headers()},
            body=b'{"name":"X","goal":"y"}')
        self.assertEqual(status, 403, body)
        self.assertIn(b"viewer", body)
        self.assertEqual(self.app.created, [])

    async def test_operator_bearer_can_write(self):
        status, _, body = await raw_request(
            self.port, "POST", "/api/projects",
            headers={**bearer(self.OPERATOR), **write_headers()},
            body=b'{"name":"X","goal":"y"}')
        self.assertEqual(status, 201, body)
        self.assertEqual(self.app.created, [("X", "y")])

    async def test_user_cookie_binds_user_and_role(self):
        cookie = await self._login(self.VIEWER)
        status, _, body = await raw_request(
            self.port, "GET", "/", headers={"Cookie": cookie})
        self.assertEqual(status, 200, body)
        auth = self.server.auth
        claims = auth.cookie_claims(cookie.split("=", 1)[1],
                                    f"127.0.0.1:{self.port}")
        self.assertIsNotNone(claims)
        self.assertEqual(claims.user, "bob")
        self.assertEqual(claims.role, ROLE_VIEWER)
        # A viewer cookie cannot write even with CSRF headers.
        status, _, body = await raw_request(
            self.port, "POST", "/api/projects",
            headers={"Cookie": cookie, **write_headers()},
            body=b'{"name":"X","goal":"y"}')
        self.assertEqual(status, 403, body)

    async def test_operator_user_cookie_can_write(self):
        cookie = await self._login(self.OPERATOR)
        status, _, body = await raw_request(
            self.port, "POST", "/api/projects",
            headers={"Cookie": cookie, **write_headers()},
            body=b'{"name":"X","goal":"y"}')
        self.assertEqual(status, 201, body)
        self.assertEqual(self.app.created, [("X", "y")])

    async def test_invalid_user_token_is_rejected(self):
        status, _, body = await raw_request(
            self.port, "GET", "/?token=not-a-configured-token")
        self.assertEqual(status, 401, body)

    async def test_account_token_exchange_is_not_single_use(self):
        # Long-lived user credentials validate as Bearer tokens and may log in
        # repeatedly; single-use applies to minted launch links, not accounts.
        for _ in range(2):
            self.assertEqual(
                (await raw_request(self.port, "GET",
                                   f"/?token={self.OPERATOR}"))[0], 303)


if __name__ == "__main__":
    unittest.main()

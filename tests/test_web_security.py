"""Web admission / CSRF / DNS-rebinding / clickjacking regression tests (H1).

These start a real ``WebServer`` on loopback and speak raw HTTP/1.1 so each
test can control the ``Host``/``Origin``/``Sec-Fetch-Site`` headers that a
browser would send.  They pin the security contract from the fix plan:

* unauthenticated API/SSE/app-shell -> 401;
* ``?token=`` -> ``HttpOnly``/``SameSite=Strict`` cookie + 303, wrong -> 401;
* bad Host / cross-site Origin / cross-site fetch metadata -> 403;
* ``text/plain`` writes -> 415, missing ``X-SFO-Request`` -> 403;
* ``OPTIONS`` -> 405 with no ``Access-Control-*``;
* security headers on success *and* on every rejection;
* anonymous static assets, and Bearer auth for CLI/tests.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from stackflowos.config import default_root
from stackflowos.l2 import EventLog
from stackflowos.web.auth import (
    CONTENT_SECURITY_POLICY,
    COOKIE_NAME,
    WebAuth,
    security_headers,
)
from stackflowos.web.server import WebServer

REPO = default_root()

CSP_HEADER = "content-security-policy"
SECURITY_HEADER_VALUES = {
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
}


class FakeConfig:
    def public_dict(self):
        return {"system": {"name": "TestOS"}, "web": {"port": 0}}


class FakeApp:
    """Just enough App for health/state/SSE routes."""

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


async def raw_request(port: int, method: str, path: str, *, host: str | None = None,
                      headers: dict | None = None, body: bytes = b"",
                      timeout: float = 5.0) -> tuple[int, dict, bytes]:
    """Send one raw HTTP/1.1 request and parse the response.

    ``reader.read()`` runs until EOF because every server response sets
    ``Connection: close`` (except a live SSE stream, which tests never read).
    """
    if host is None:
        host = f"127.0.0.1:{port}"
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}"]
    for key, value in (headers or {}).items():
        lines.append(f"{key}: {value}")
    if body:
        lines.append(f"Content-Length: {len(body)}")
    wire = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
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
    head, _, payload = data.partition(b"\r\n\r\n")
    head_lines = head.decode("latin-1").split("\r\n")
    status = int(head_lines[0].split(" ")[1]) if head_lines else 0
    parsed: dict[str, str] = {}
    for line in head_lines[1:]:
        key, separator, value = line.partition(":")
        if separator:
            parsed[key.strip().lower()] = value.strip()
    return status, parsed, payload


class WebSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.app = FakeApp(self.tmp)
        self.assets = self.tmp / "assets"
        (self.assets / "css").mkdir(parents=True)
        (self.assets / "js").mkdir(parents=True)
        (self.assets / "index.html").write_text(
            "<!doctype html><title>SFO</title>", "utf-8")
        (self.assets / "css" / "os.css").write_text(
            "body{background:#000}", "utf-8")
        (self.assets / "js" / "main.js").write_text("export {};", "utf-8")
        self.server = WebServer(self.app, host="127.0.0.1", port=0,
                                assets_dir=self.assets)
        await self.server.start()
        self.port = self.server.port
        self.token = self.server.launch_token

    async def asyncTearDown(self):
        await self.server.stop()
        await self.app.eventlog.close()
        self._tmp.cleanup()

    def auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def get_cookie(self) -> str:
        status, headers, _ = await raw_request(
            self.port, "GET", f"/?token={self.token}")
        self.assertEqual(status, 303)
        return headers["set-cookie"].split(";", 1)[0]

    # -- authentication --------------------------------------------------
    async def test_missing_credentials_are_401(self):
        for path in ("/api/state", "/api/events/stream", "/", "/index.html"):
            with self.subTest(path=path):
                status, headers, body = await raw_request(self.port, "GET", path)
                self.assertEqual(status, 401, body)
                self.assertIn(b"UNAUTHORIZED", body)
                self.assertIn("no-store", headers.get("cache-control", ""))

    async def test_wrong_launch_token_is_401(self):
        status, _, body = await raw_request(self.port, "GET", "/?token=not-the-token")
        self.assertEqual(status, 401)
        self.assertIn(b"UNAUTHORIZED", body)

    async def test_token_exchange_sets_httponly_strict_cookie(self):
        status, headers, _ = await raw_request(
            self.port, "GET", f"/?token={self.token}")
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("location"), "./")
        cookie = headers["set-cookie"]
        self.assertTrue(cookie.startswith(f"{COOKIE_NAME}=v1."))
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn("Max-Age=", cookie)
        # The signed cookie replaces the launch token for the app shell.
        cookie_pair = cookie.split(";", 1)[0]
        status, _, body = await raw_request(
            self.port, "GET", "/", headers={"Cookie": cookie_pair})
        self.assertEqual(status, 200)
        self.assertIn(b"<title>SFO</title>", body)

    async def test_token_query_rejects_multiple_and_non_root(self):
        status, _, _ = await raw_request(
            self.port, "GET", f"/?token={self.token}&token={self.token}")
        self.assertEqual(status, 400)
        status, _, _ = await raw_request(
            self.port, "GET", f"/api/state?token={self.token}")
        self.assertEqual(status, 401)
        status, _, _ = await raw_request(
            self.port, "POST", f"/?token={self.token}",
            headers={"Content-Type": "application/json", "X-SFO-Request": "1"},
            body=b"{}")
        self.assertEqual(status, 401)

    async def test_token_is_reusable_as_bearer(self):
        status, _, body = await raw_request(
            self.port, "GET", "/api/state", headers=self.auth())
        self.assertEqual(status, 200)
        self.assertIn(b'"ok":true', body)
        status, _, body = await raw_request(
            self.port, "GET", "/?token=" + self.token)
        self.assertEqual(status, 303)

    async def test_login_url_exposes_the_token_for_the_cli(self):
        self.assertEqual(self.server.login_url,
                         f"http://127.0.0.1:{self.port}/?token={self.token}")

    async def test_bearer_can_access_api(self):
        status, headers, body = await raw_request(
            self.port, "GET", "/api/health", headers=self.auth())
        self.assertEqual(status, 200)
        self.assertTrue(body)
        self.assertIn("no-store", headers.get("cache-control", ""))

    # -- anonymous liveness probe (/healthz) ----------------------------
    async def test_liveness_probe_is_anonymous_and_data_free(self):
        status, headers, body = await raw_request(self.port, "GET", "/healthz")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"], {"status": "ok", "transport": "ok"})
        self.assertIn("no-store", headers.get("cache-control", ""))
        self.assertEqual(headers.get(CSP_HEADER), CONTENT_SECURITY_POLICY)

    async def test_liveness_probe_head_returns_headers_only(self):
        status, headers, body = await raw_request(self.port, "HEAD", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(headers["content-length"]), 0)

    async def test_liveness_probe_reports_degraded_with_503(self):
        def degraded():
            return {"status": "degraded", "transport": "down", "units": {}}

        self.app.health = degraded
        status, _, body = await raw_request(self.port, "GET", "/healthz")
        self.assertEqual(status, 503, body)
        self.assertEqual(json.loads(body)["data"]["transport"], "down")

    async def test_liveness_probe_rejects_writes(self):
        status, _, body = await raw_request(
            self.port, "POST", "/healthz",
            headers={"Content-Type": "application/json", "X-SFO-Request": "1"},
            body=b"{}")
        self.assertEqual(status, 405, body)

    async def test_liveness_probe_still_checks_host(self):
        status, _, body = await raw_request(
            self.port, "GET", "/healthz", host="evil.example:17800")
        self.assertEqual(status, 403, body)
        self.assertIn(b"FORBIDDEN", body)

    async def test_liveness_probe_does_not_accept_launch_token_query(self):
        status, _, _ = await raw_request(
            self.port, "GET", f"/healthz?token={self.token}")
        self.assertEqual(status, 401)

    async def test_api_health_stays_authenticated(self):
        status, _, body = await raw_request(self.port, "GET", "/api/health")
        self.assertEqual(status, 401, body)
        self.assertIn(b"UNAUTHORIZED", body)

    def test_requires_auth_contract(self):
        auth = WebAuth(host="127.0.0.1", port=17800)
        self.assertFalse(auth.requires_auth("/healthz"))
        self.assertFalse(auth.requires_auth("/css/os.css"))
        self.assertTrue(auth.requires_auth("/api/health"))
        self.assertTrue(auth.requires_auth("/api/state"))
        self.assertTrue(auth.requires_auth("/"))

    # -- host / origin / CSRF -------------------------------------------
    async def test_rejects_bad_host(self):
        for host in ("evil.example", "evil.example:17800", "127.0.0.1",
                     "127.0.0.1:notaport", "user@127.0.0.1:17800"):
            with self.subTest(host=host):
                status, _, body = await raw_request(
                    self.port, "GET", "/api/state", host=host, headers=self.auth())
                self.assertEqual(status, 403, body)
                self.assertIn(b"FORBIDDEN", body)

    async def test_hostless_request_is_rejected(self):
        status, _, body = await raw_request(
            self.port, "GET", "/api/state", host="", headers=self.auth())
        self.assertEqual(status, 403)

    async def test_rejects_cross_site_origin(self):
        for origin in ("https://evil.example", "http://127.0.0.1:9", "null"):
            with self.subTest(origin=origin):
                status, _, body = await raw_request(
                    self.port, "GET", "/api/state",
                    headers={**self.auth(), "Origin": origin})
                self.assertEqual(status, 403, body)

    async def test_accepts_same_origin(self):
        status, _, _ = await raw_request(
            self.port, "GET", "/api/state",
            headers={**self.auth(),
                     "Origin": f"http://127.0.0.1:{self.port}",
                     "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)

    async def test_rejects_sec_fetch_site_cross_site(self):
        status, _, body = await raw_request(
            self.port, "GET", "/api/state",
            headers={**self.auth(), "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        self.assertIn(b"FORBIDDEN", body)

    async def test_write_requires_json_content_type(self):
        status, headers, body = await raw_request(
            self.port, "POST", "/api/state",
            headers={**self.auth(), "Content-Type": "text/plain",
                     "X-SFO-Request": "1"},
            body=b'{"decision":"allow"}')
        self.assertEqual(status, 415, body)
        self.assertIn("no-store", headers.get("cache-control", ""))
        self.assertEqual(headers.get(CSP_HEADER), CONTENT_SECURITY_POLICY)

    async def test_write_requires_csrf_header(self):
        status, _, body = await raw_request(
            self.port, "POST", "/api/state",
            headers={**self.auth(), "Content-Type": "application/json"},
            body=b"{}")
        self.assertEqual(status, 403)
        self.assertIn(b"FORBIDDEN", body)

    async def test_authenticated_json_write_passes_admission(self):
        # /api/state only has a GET route, so 404 proves admission let it through.
        status, _, body = await raw_request(
            self.port, "POST", "/api/state",
            headers={**self.auth(), "Content-Type": "application/json; charset=utf-8",
                     "X-SFO-Request": "1"},
            body=b"{}")
        self.assertEqual(status, 404, body)

    async def test_options_is_405_without_cors_headers(self):
        status, headers, body = await raw_request(
            self.port, "OPTIONS", "/api/state",
            headers={"Origin": "https://evil.example",
                     "Access-Control-Request-Method": "POST"})
        self.assertEqual(status, 405, body)
        self.assertIn(b"METHOD_NOT_ALLOWED", body)
        self.assertFalse(
            [key for key in headers if key.startswith("access-control-")],
            f"CORS headers leaked: {headers}")

    # -- headers / static ------------------------------------------------
    async def test_security_headers_on_success_and_errors(self):
        cases = [
            await raw_request(self.port, "GET", "/api/state", headers=self.auth()),
            await raw_request(self.port, "GET", "/api/state"),          # 401
            await raw_request(self.port, "GET", "/api/state",
                              host="evil.example", headers=self.auth()),  # 403
            await raw_request(self.port, "GET", "/"),                   # 401 app shell
        ]
        for status, headers, _ in cases:
            with self.subTest(status=status):
                self.assertEqual(headers.get(CSP_HEADER), CONTENT_SECURITY_POLICY)
                for key, value in SECURITY_HEADER_VALUES.items():
                    self.assertEqual(headers.get(key), value)
                self.assertIn("cache-control", headers)

    async def test_static_assets_are_anonymous(self):
        for path, marker in (("/css/os.css", b"background"),
                             ("/js/main.js", b"export"),
                             ("/index.html", b"<title>SFO</title>")):
            with self.subTest(path=path):
                status, headers, body = await raw_request(self.port, "GET", path)
                if path == "/index.html":
                    # The app shell itself is authenticated.
                    self.assertEqual(status, 401)
                else:
                    self.assertEqual(status, 200)
                    self.assertIn(marker, body)
                    self.assertEqual(headers.get(CSP_HEADER),
                                     CONTENT_SECURITY_POLICY)

    async def test_traversal_is_rejected(self):
        for path in ("/css/../index.html", "/api/../../etc/passwd",
                     "/css/%2e%2e/index.html"):
            with self.subTest(path=path):
                status, _, _ = await raw_request(
                    self.port, "GET", path, headers=self.auth())
                self.assertEqual(status, 403)

    async def test_cookie_is_bound_to_authority(self):
        cookie = await self.get_cookie()
        # Same cookie, different authority -> rejected.
        status, _, _ = await raw_request(
            self.port, "GET", "/", host=f"localhost:{self.port}",
            headers={"Cookie": cookie})
        self.assertEqual(status, 401)

    async def test_secure_cookie_when_tls(self):
        auth = WebAuth(host="127.0.0.1", port=17800, secure=True,
                       trusted_hosts=(), cookie_ttl_days=7)
        cookie = auth.set_cookie_header("127.0.0.1:17800")
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertNotIn("Secure", WebAuth(port=17800).set_cookie_header(
            "127.0.0.1:17800"))

    async def test_trusted_hosts_are_accepted(self):
        auth = WebAuth(host="0.0.0.0", port=17800,
                       trusted_hosts=["console.example", "console.example:9443"])
        self.assertIn("console.example:17800", auth.allowed_authorities())
        self.assertIn("console.example:9443", auth.allowed_authorities())
        self.assertTrue(auth.authority_allowed("127.0.0.1:17800"))
        self.assertFalse(auth.authority_allowed("evil.example:17800"))

    def test_security_headers_string_contract(self):
        rendered = security_headers(cache="no-store")
        for expected in (
            "X-Frame-Options: DENY",
            "X-Content-Type-Options: nosniff",
            "Referrer-Policy: no-referrer",
            "Cross-Origin-Opener-Policy: same-origin",
            "Cross-Origin-Resource-Policy: same-origin",
            "Cache-Control: no-store",
        ):
            self.assertIn(expected, rendered)


if __name__ == "__main__":
    unittest.main()

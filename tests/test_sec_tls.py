"""TLS listener tests: HTTPS, Secure cookies, /healthz, honest remote warning."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stackflowos.config import default_root
from stackflowos.web.server import WebServer

from tests.test_sec_support import (
    FakeApp,
    make_assets,
    make_self_signed_cert,
    raw_request,
)

REPO = default_root()
OPENSSL_AVAILABLE = make_self_signed_cert is not None


class TlsTestBase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._cert_tmp = tempfile.TemporaryDirectory()
        cls.certs = make_self_signed_cert(Path(cls._cert_tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls._cert_tmp.cleanup()

    async def asyncSetUp(self):
        if self.certs is None:
            self.skipTest("openssl CLI is not available")
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.app = FakeApp(self.tmp)
        self.assets = make_assets(self.tmp)
        self.cert, self.key = self.certs
        self.server = WebServer(
            self.app, host="127.0.0.1", port=0, assets_dir=self.assets,
            tls_cert=str(self.cert), tls_key=str(self.key),
            tls_min_version="TLSv1.2")
        await self.server.start()
        self.port = self.server.port

    async def asyncTearDown(self):
        await self.server.stop()
        await self.app.eventlog.close()
        self._tmp.cleanup()


class TlsListenerTests(TlsTestBase):
    async def test_https_url_and_healthz(self):
        self.assertTrue(self.server.tls_enabled)
        self.assertTrue(self.server.url.startswith("https://"))
        status, headers, body = await raw_request(
            self.port, "GET", "/healthz", tls=True)
        self.assertEqual(status, 200, body)
        self.assertIn(b'"transport":"ok"', body)
        self.assertNotIn("access-control-allow-origin", headers)

    async def test_cookie_is_secure_over_tls(self):
        token = self.server.launch_token
        status, headers, _ = await raw_request(
            self.port, "GET", f"/?token={token}", tls=True)
        self.assertEqual(status, 303)
        cookie = headers["set-cookie"]
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        pair = cookie.split(";", 1)[0]
        status, _, body = await raw_request(
            self.port, "GET", "/", headers={"Cookie": pair}, tls=True)
        self.assertEqual(status, 200, body)
        self.assertIn(b"<title>SFO</title>", body)

    async def test_bearer_api_over_tls(self):
        status, _, body = await raw_request(
            self.port, "GET", "/api/state",
            headers={"Authorization": f"Bearer {self.server.launch_token}"},
            tls=True)
        self.assertEqual(status, 200, body)

    async def test_single_use_token_over_tls(self):
        token = self.server.launch_token
        first, _, _ = await raw_request(
            self.port, "GET", f"/?token={token}", tls=True)
        second, _, body = await raw_request(
            self.port, "GET", f"/?token={token}", tls=True)
        self.assertEqual(first, 303)
        self.assertEqual(second, 401, body)
        # The Bearer escape hatch keeps working (CLI/test API path).
        status, _, _ = await raw_request(
            self.port, "GET", "/api/state",
            headers={"Authorization": f"Bearer {token}"}, tls=True)
        self.assertEqual(status, 200)


class TlsConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.app = FakeApp(self.tmp)
        self.assets = make_assets(self.tmp)

    async def asyncTearDown(self):
        await self.app.eventlog.close()
        self._tmp.cleanup()

    def test_half_configured_tls_fails_loudly(self):
        with self.assertRaises(ValueError):
            WebServer(self.app, host="127.0.0.1", port=0,
                      assets_dir=self.assets, tls_cert="cert.pem")
        with self.assertRaises(ValueError):
            WebServer(self.app, host="127.0.0.1", port=0,
                      assets_dir=self.assets, tls_key="key.pem")

    def test_invalid_min_version_is_rejected(self):
        with self.assertRaises(ValueError):
            WebServer(self.app, host="127.0.0.1", port=0,
                      assets_dir=self.assets, tls_cert="cert.pem",
                      tls_key="key.pem", tls_min_version="TLSv1.0")

    def test_plain_http_defaults_keep_working(self):
        server = WebServer(self.app, host="127.0.0.1", port=0,
                           assets_dir=self.assets)
        self.assertFalse(server.tls_enabled)
        self.assertTrue(server.url.startswith("http://"))
        self.assertFalse(server.insecure_remote)

    def test_non_loopback_without_tls_is_not_pretended_safe(self):
        server = WebServer(self.app, host="0.0.0.0", port=0,
                           assets_dir=self.assets)
        self.assertFalse(server.tls_enabled)
        self.assertTrue(server.insecure_remote)
        self.assertTrue(server.security_notes)
        self.assertIn("plaintext", " ".join(server.security_notes))


if __name__ == "__main__":
    unittest.main()

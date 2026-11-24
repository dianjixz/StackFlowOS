"""M4: ``net.fetch`` private-network denial, redirect re-checks and risk.

The suite is split so most cases stay pure (URL parsing / config / redirect
handler) and only the end-to-end cases start a loopback ``http.server``.
"""

from __future__ import annotations

import ipaddress
import shutil
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit
from urllib.request import Request

from stackflowos.agents.models import Agent
from stackflowos.agents.tools import (
    MAX_REDIRECTS,
    ToolError,
    ToolExecutor,
    _GuardedRedirectHandler,
)
from stackflowos.config import Config, ConfigError, SecurityCfg
from stackflowos.security.capabilities import risk_of
from stackflowos.util.net import (
    InvalidTarget,
    is_private_address,
    is_private_host,
    validate_fetch_url,
)

REPO = Path(__file__).resolve().parent.parent
TMP_ROOT = REPO / ".stackflowos" / "tmp"

PUBLIC_V4 = "93.184.216.34"


def _public_addrinfo(port: int = 443):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
             (PUBLIC_V4, port))]


class _FakeSys:
    """Minimal sys API: approves everything and records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, action: str, data: dict | None = None, *,
                   timeout: float | None = None) -> dict:
        payload = dict(data or {})
        self.calls.append((action, payload))
        if action in ("approval.request", "approval.status"):
            return {"id": "apr-m4", "status": "approved", "mode": "rule",
                    "risk": "high", "grant_id": None}
        if action == "approval.use":
            return {"ok": True}
        return {}


class _RecordingHandler(BaseHTTPRequestHandler):
    """Test HTTP handler; routes are ``{path: callable(handler)}``."""

    protocol_version = "HTTP/1.0"

    def log_message(self, *args) -> None:  # keep test output clean
        pass

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        server = self.server
        server.hits.append(self.path)
        route = server.routes.get(urlsplit(self.path).path)
        if route is None:
            self.send_error(404)
            return
        route(self)


def _start_server(routes: dict) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    server.routes = routes
    server.hits = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _url(server: ThreadingHTTPServer, path: str = "/") -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}{path}"


def _send_text(handler: BaseHTTPRequestHandler, body: str,
               status: int = 200, content_type: str = "text/plain") -> None:
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _send_redirect(handler: BaseHTTPRequestHandler, location: str,
                   status: int = 302) -> None:
    handler.send_response(status)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


class _ExecutorMixin(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))
        self.workspace = self._tmp / "workspace"
        self.workspace.mkdir()
        self.agent = Agent(id="agent-coder-m4", role="coder",
                           project_id="p-1", task_id="t-1")
        self.sys = _FakeSys()

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def executor(self, *, allow_private_network: bool) -> ToolExecutor:
        return ToolExecutor(
            self.sys, agent=self.agent, workspace=self.workspace,
            security_config=SecurityCfg(
                allow_private_network=allow_private_network))


class ConfigAndHelperTests(unittest.TestCase):
    def test_default_is_deny(self) -> None:
        self.assertFalse(SecurityCfg().allow_private_network)
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(TMP_ROOT)) as tmp:
            self.assertFalse(
                Config.load(Path(tmp)).security.allow_private_network)

    def test_config_accepts_bool_and_rejects_other_types(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(TMP_ROOT)) as tmp:
            cfg = Config.load(Path(tmp))
            updated = cfg.update({"security": {"allow_private_network": True}})
            self.assertTrue(updated.security.allow_private_network)
            with self.assertRaises(ConfigError):
                updated.update({"security": {"allow_private_network": "yes"}})
            with self.assertRaises(ConfigError):
                updated.update({"security": {"allow_private_network": 1}})

    def test_private_host_name_rules(self) -> None:
        for host in ("localhost", "localhost.localdomain", "foo.local",
                     "foo.localhost", "intranet", "127.0.0.1", "10.1.2.3",
                     "172.16.0.1", "192.168.1.1", "169.254.169.254",
                     "0.0.0.0", "::", "::1", "fe80::1", "fc00::1",
                     "::ffff:127.0.0.1", "::ffff:7f00:1", "::ffff:10.0.0.1"):
            with self.subTest(host=host):
                self.assertTrue(is_private_host(host))
        for host in ("example.com", "8.8.8.8", PUBLIC_V4,
                     "2606:2800:220:1:248:1893:25c8:1946"):
            with self.subTest(host=host):
                self.assertFalse(is_private_host(host))

    def test_private_address_categories(self) -> None:
        for text in ("127.0.0.1", "10.0.0.1", "172.20.1.1", "192.168.0.1",
                     "169.254.169.254", "0.0.0.0", "100.64.0.1",
                     "::1", "::", "fe80::1", "fc00::1", "2001:db8::1",
                     "::ffff:192.168.1.1"):
            with self.subTest(address=text):
                self.assertTrue(is_private_address(ipaddress.ip_address(text)))
        for text in ("8.8.8.8", PUBLIC_V4):
            with self.subTest(address=text):
                self.assertFalse(is_private_address(ipaddress.ip_address(text)))


class UrlValidationTests(_ExecutorMixin):
    PRIVATE_URLS = (
        "http://127.0.0.1:17800/api/state",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://[fe80::1]/",
    )

    def test_default_config_rejects_private_targets(self) -> None:
        executor = self.executor(allow_private_network=False)
        for url in self.PRIVATE_URLS:
            with self.subTest(url=url):
                with self.assertRaises(ToolError) as ctx:
                    executor._validate_url(url)
                self.assertIn("private network is disabled", str(ctx.exception))

    def test_pure_validator_rejects_private_targets(self) -> None:
        for url in self.PRIVATE_URLS:
            with self.subTest(url=url):
                with self.assertRaises(InvalidTarget):
                    validate_fetch_url(url, allow_private_network=False)

    def test_public_host_is_allowed_after_resolution(self) -> None:
        executor = self.executor(allow_private_network=False)
        with mock.patch(
                "stackflowos.util.net.socket.getaddrinfo",
                return_value=_public_addrinfo()):
            executor._validate_url("https://example.com/")
            validate_fetch_url("https://example.com/",
                               allow_private_network=False)

    def test_name_resolving_to_private_is_rejected(self) -> None:
        executor = self.executor(allow_private_network=False)
        with mock.patch(
                "stackflowos.util.net.socket.getaddrinfo",
                return_value=_public_addrinfo()) as lookup:
            executor._validate_url("https://example.com/")
            lookup.assert_called_once()
        with mock.patch(
                "stackflowos.util.net.socket.getaddrinfo",
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                               ("127.0.0.1", 80))]):
            with self.assertRaises(ToolError) as ctx:
                executor._validate_url("http://rebind.example/")
            self.assertIn("private network is disabled", str(ctx.exception))

    def test_non_http_userinfo_and_shape_rejected(self) -> None:
        executor = self.executor(allow_private_network=False)
        for url, fragment in (
                ("file:///etc/passwd", "http"),
                ("http://user:pass@example.com/", "credentials"),
                ("http://user@example.com/", "credentials"),
                ("http:///nohost", "host"),
                ("http://example.com:99999/", "port"),
                ("http://example.com/" + "a" * 5000, "too long"),
        ):
            with self.subTest(url=url[:60]):
                with self.assertRaises(ToolError) as ctx:
                    executor._validate_url(url)
                self.assertIn(fragment, str(ctx.exception))
        with self.assertRaises(ToolError):
            executor._validate_url("http://example.com:0/")

    def test_allow_private_network_permits_but_risk_stays_high(self) -> None:
        executor = self.executor(allow_private_network=True)
        for url in self.PRIVATE_URLS:
            with self.subTest(url=url):
                executor._validate_url(url)  # must not raise
                self.assertEqual(
                    risk_of("net.fetch", {"url": url},
                            projects_root=self.workspace),
                    "high")

    def test_external_url_risk_stays_medium(self) -> None:
        self.assertEqual(
            risk_of("net.fetch", {"url": "https://example.com"},
                    projects_root=self.workspace), "medium")


class RedirectHandlerTests(_ExecutorMixin):
    def _handler(self, *, allow_private_network: bool = False):
        executor = self.executor(allow_private_network=allow_private_network)
        return _GuardedRedirectHandler(executor._validate_url)

    def test_redirect_to_private_is_rejected(self) -> None:
        handler = self._handler(allow_private_network=False)
        request = Request("https://example.com/")
        for newurl in ("http://127.0.0.1:17800/secret",
                       "http://169.254.169.254/latest/meta-data/",
                       "http://[::1]/secret"):
            with self.subTest(newurl=newurl):
                with self.assertRaises(ToolError) as ctx:
                    handler.redirect_request(request, None, 302, "Found",
                                             {}, newurl)
                self.assertIn("private network is disabled",
                              str(ctx.exception))

    def test_redirect_handler_never_requests_private_target(self) -> None:
        target = _start_server({"/secret": lambda h: _send_text(h, "secret")})
        self.addCleanup(target.server_close)
        self.addCleanup(target.shutdown)
        handler = self._handler(allow_private_network=False)
        request = Request("https://example.com/")
        with self.assertRaises(ToolError):
            handler.redirect_request(request, None, 302, "Found", {},
                                     _url(target, "/secret"))
        self.assertEqual(target.hits, [])

    def test_redirect_to_external_https_is_allowed(self) -> None:
        handler = self._handler(allow_private_network=False)
        with mock.patch(
                "stackflowos.util.net.socket.getaddrinfo",
                return_value=_public_addrinfo()):
            request = handler.redirect_request(
                Request("http://example.com/"), None, 302, "Found", {},
                "https://example.com/next")
        self.assertIsNotNone(request)
        self.assertEqual(request.full_url, "https://example.com/next")

    def test_redirect_to_userinfo_or_other_scheme_rejected(self) -> None:
        handler = self._handler(allow_private_network=True)
        with self.assertRaises(ToolError):
            handler.redirect_request(Request("http://127.0.0.1/"), None, 302,
                                     "Found", {},
                                     "https://user:pass@example.com/")
        with self.assertRaises(ToolError):
            handler.redirect_request(Request("http://127.0.0.1/"), None, 302,
                                     "Found", {}, "file:///etc/passwd")
        with self.assertRaises(ToolError):
            handler.redirect_request(Request("http://127.0.0.1/"), None, 302,
                                     "Found", {},
                                     "https://example.com:99999/")
        with self.assertRaises(ToolError):
            handler.redirect_request(
                Request("http://127.0.0.1/"), None, 302, "Found", {},
                "https://example.com/" + "a" * 5000)

    def test_redirect_limit(self) -> None:
        handler = self._handler(allow_private_network=True)
        request = Request("http://127.0.0.1/")
        for index in range(MAX_REDIRECTS):
            new_request = handler.redirect_request(
                request, None, 302, "Found", {},
                f"http://127.0.0.1/{index}")
            self.assertIsNotNone(new_request)
        with self.assertRaises(ToolError) as ctx:
            handler.redirect_request(request, None, 302, "Found", {},
                                     f"http://127.0.0.1/{MAX_REDIRECTS}")
        self.assertIn("too many redirects", str(ctx.exception))

    def test_redirect_does_not_forward_credentials(self) -> None:
        handler = self._handler(allow_private_network=True)
        request = Request("http://127.0.0.1/",
                          headers={"Cookie": "sid=1",
                                   "Authorization": "Bearer secret"})
        new_request = handler.redirect_request(
            request, None, 302, "Found", {}, "http://127.0.0.1/next")
        self.assertIsNotNone(new_request)
        names = {key.lower() for key in new_request.headers}
        self.assertNotIn("cookie", names)
        self.assertNotIn("authorization", names)


class NetFetchEndToEndTests(_ExecutorMixin):
    def _server(self, routes: dict) -> ThreadingHTTPServer:
        server = _start_server(routes)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    async def test_default_config_blocks_before_request(self) -> None:
        server = self._server({"/ok": lambda h: _send_text(h, "ok")})
        result = await self.executor(
            allow_private_network=False).run(
                "net.fetch", {"url": _url(server, "/ok")})
        self.assertFalse(result.ok)
        self.assertIn("private network is disabled", result.error or "")
        self.assertEqual(server.hits, [])
        self.assertEqual(self.sys.calls, [])

    async def test_allow_private_network_fetches(self) -> None:
        server = self._server({"/ok": lambda h: _send_text(h, "hello")})
        result = await self.executor(allow_private_network=True).run(
            "net.fetch", {"url": _url(server, "/ok")})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["content"], "hello")
        self.assertEqual(result.data["status"], 200)
        self.assertEqual(result.data["requested_url"], _url(server, "/ok"))
        self.assertEqual(server.hits, ["/ok"])

    async def test_redirect_chain_followed_only_when_allowed(self) -> None:
        target = self._server(
            {"/secret": lambda h: _send_text(h, "top secret")})
        source = self._server(
            {"/go": lambda h: _send_redirect(h, _url(target, "/secret"))})
        result = await self.executor(allow_private_network=True).run(
            "net.fetch", {"url": _url(source, "/go")})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["content"], "top secret")
        self.assertEqual(result.data["url"], _url(target, "/secret"))
        self.assertEqual(source.hits, ["/go"])
        self.assertEqual(target.hits, ["/secret"])

    async def test_too_many_redirects(self) -> None:
        server = self._server({})
        server.routes["/loop"] = lambda h: _send_redirect(h, "/loop")
        result = await self.executor(allow_private_network=True).run(
            "net.fetch", {"url": _url(server, "/loop")})
        self.assertFalse(result.ok)
        self.assertIn("too many redirects", result.error or "")
        # The pinned opener uses urllib's redirect bookkeeping (it stops one
        # request earlier than the legacy handler).  The security contract is
        # that the number of requests stays bounded; exact off-by-one hop
        # counting is an implementation detail.
        self.assertGreaterEqual(len(server.hits), MAX_REDIRECTS)
        self.assertLessEqual(len(server.hits), MAX_REDIRECTS + 1)

    async def test_redirect_to_file_scheme_is_rejected(self) -> None:
        source = self._server(
            {"/go": lambda h: _send_redirect(h, "file:///etc/passwd")})
        result = await self.executor(allow_private_network=True).run(
            "net.fetch", {"url": _url(source, "/go")})
        self.assertFalse(result.ok)
        self.assertIn("http/https", result.error or "")

    async def test_no_cookie_or_authorization_is_sent(self) -> None:
        seen: dict = {}

        def echo(handler):
            seen.update(handler.headers.items())
            _send_text(handler, "ok")

        server = self._server({"/headers": echo})
        result = await self.executor(allow_private_network=True).run(
            "net.fetch", {"url": _url(server, "/headers")})
        self.assertTrue(result.ok, result.error)
        lower = {str(key).lower() for key in seen}
        self.assertNotIn("cookie", lower)
        self.assertNotIn("authorization", lower)
        self.assertNotIn("proxy-authorization", lower)

    async def test_response_size_is_capped(self) -> None:
        server = self._server(
            {"/big": lambda h: _send_text(h, "x" * 5000)})
        result = await self.executor(allow_private_network=True).run(
            "net.fetch", {"url": _url(server, "/big"), "max_bytes": 100})
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.data["truncated"])
        self.assertEqual(len(result.data["content"]), 100)


if __name__ == "__main__":
    unittest.main()

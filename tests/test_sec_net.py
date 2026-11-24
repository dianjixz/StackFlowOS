"""DNS-pinned net.fetch path and the legacy (unpinned) escape hatch."""

from __future__ import annotations

import http.server
import socket
import ssl
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from stackflowos.config import default_root
from stackflowos.util import net

from tests.test_sec_support import make_self_signed_cert

REPO = default_root()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib callback name
        self.server.seen_hosts.append(self.headers.get("Host"))
        self.server.seen_paths.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"hello-from-pinned"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence the test log
        return


class _V6Server(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6


class _Server:
    def __init__(self, certs: tuple[Path, Path] | None = None, *,
                 host: str = "127.0.0.1", httpd_class=None):
        build = httpd_class or http.server.ThreadingHTTPServer
        self.httpd = build((host, 0), _Handler)
        self.httpd.seen_hosts = []
        self.httpd.seen_paths = []
        self.httpd.sni_names = []
        self.scheme = "http"
        if certs is not None:
            cert, key = certs
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(cert), str(key))
            context.sni_callback = (
                lambda sslobj, name, ctx: self.httpd.sni_names.append(name))
            self.httpd.socket = context.wrap_socket(
                self.httpd.socket, server_side=True)
            self.scheme = "https"
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc_info):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _fake_getaddrinfo(mapping: dict[str, str], counter: dict[str, int]):
    real = socket.getaddrinfo

    def fake(host, port, family=0, type=0, proto=0, flags=0):
        if host in mapping:
            counter[host] = counter.get(host, 0) + 1
            address = mapping[host]
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                     "", (address, int(port or 80)))]
        return real(host, port, family, type, proto, flags)

    return fake


class ResolveTargetTests(unittest.TestCase):
    def test_resolves_once_and_validates_public_address(self):
        counter: dict[str, int] = {}
        mapping = {"pinned.test": "93.184.216.34"}
        with mock.patch.object(net.socket, "getaddrinfo",
                               _fake_getaddrinfo(mapping, counter)):
            target = net.resolve_target("http://pinned.test/a",
                                        allow_private_network=False)
        self.assertEqual(str(target.addresses[0]), "93.184.216.34")
        self.assertEqual(counter["pinned.test"], 1)
        self.assertEqual(target.host_header, "pinned.test")
        self.assertEqual(target.port, 80)

    def test_private_resolution_is_rejected(self):
        counter: dict[str, int] = {}
        mapping = {"internal.test": "10.0.0.5"}
        with mock.patch.object(net.socket, "getaddrinfo",
                               _fake_getaddrinfo(mapping, counter)):
            with self.assertRaises(net.InvalidTarget):
                net.resolve_target("http://internal.test/",
                                   allow_private_network=False)
            # The opt-in flag permits the target but keeps the address pinned.
            target = net.resolve_target("http://internal.test/",
                                        allow_private_network=True)
        self.assertEqual(str(target.addresses[0]), "10.0.0.5")

    def test_credentials_are_rejected_before_resolution(self):
        with self.assertRaises(net.InvalidTarget):
            net.resolve_target("http://user:pass@pinned.test/",
                               allow_private_network=False)


class PinnedFetchTests(unittest.TestCase):
    def test_pinned_connection_keeps_host_header(self):
        counter: dict[str, int] = {}
        with _Server() as server:
            mapping = {"pinned.test": "127.0.0.1"}
            with mock.patch.object(net.socket, "getaddrinfo",
                                   _fake_getaddrinfo(mapping, counter)):
                result = net.fetch_url(
                    f"http://pinned.test:{server.port}/ok",
                    allow_private_network=True, pin_dns=True)
            self.assertEqual(result.status, 200)
            self.assertEqual(result.body, b"hello-from-pinned")
            self.assertEqual(server.httpd.seen_hosts,
                             [f"pinned.test:{server.port}"])
            self.assertEqual(counter["pinned.test"], 1)

    def test_redirect_hop_is_revalidated_and_pinned(self):
        counter: dict[str, int] = {}
        with _Server() as server:
            mapping = {"pinned.test": "127.0.0.1"}
            with mock.patch.object(net.socket, "getaddrinfo",
                                   _fake_getaddrinfo(mapping, counter)):
                result = net.fetch_url(
                    f"http://pinned.test:{server.port}/redirect",
                    allow_private_network=True, pin_dns=True)
            self.assertEqual(result.status, 200)
            self.assertTrue(result.url.endswith("/ok"))
            self.assertEqual(server.httpd.seen_paths, ["/redirect", "/ok"])
            # Every hop resolves at the pinned connection (no stale cache).
            self.assertGreaterEqual(counter["pinned.test"], 2)

    def test_private_target_denied_before_connect(self):
        with self.assertRaises(net.InvalidTarget):
            net.fetch_url("http://internal.test/",
                          allow_private_network=False, pin_dns=True)

    def test_legacy_unpinned_path_still_works(self):
        with _Server() as server:
            mapping = {"legacy.test": "127.0.0.1"}
            with mock.patch.object(net.socket, "getaddrinfo",
                                   _fake_getaddrinfo(mapping, {})):
                result = net.fetch_url(
                    f"http://legacy.test:{server.port}/ok",
                    allow_private_network=True, pin_dns=False)
            self.assertEqual(result.status, 200)
            self.assertEqual(result.body, b"hello-from-pinned")
            self.assertEqual(server.httpd.seen_hosts,
                             [f"legacy.test:{server.port}"])

    def test_legacy_path_rejects_private_targets(self):
        with self.assertRaises(net.InvalidTarget):
            net.fetch_url("http://127.0.0.1:9/",
                          allow_private_network=False, pin_dns=False)

    def test_https_pinned_connection_sends_sni(self):
        with tempfile.TemporaryDirectory() as cert_dir:
            certs = make_self_signed_cert(Path(cert_dir))
            if certs is None:
                self.skipTest("openssl CLI is not available")
            with _Server(certs) as server:
                mapping = {"localhost": "127.0.0.1"}
                client = ssl.create_default_context()
                client.check_hostname = False
                client.verify_mode = ssl.CERT_NONE
                with mock.patch.object(net.socket, "getaddrinfo",
                                       _fake_getaddrinfo(mapping, {})):
                    result = net.fetch_url(
                        f"https://localhost:{server.port}/ok",
                        allow_private_network=True, pin_dns=True,
                        context=client)
                self.assertEqual(result.status, 200)
                self.assertEqual(server.httpd.sni_names, ["localhost"])
                self.assertEqual(server.httpd.seen_hosts,
                                 [f"localhost:{server.port}"])



class IPv6PinnedFetchTests(unittest.TestCase):
    def test_ipv6_literal_is_pinned_and_host_header_is_bracketed(self):
        if not socket.has_ipv6:
            self.skipTest("IPv6 is not available")
        try:
            server = _Server(host="::1", httpd_class=_V6Server)
        except OSError as exc:
            self.skipTest(f"cannot bind ::1: {exc}")
        with server:
            result = net.fetch_url(
                f"http://[::1]:{server.port}/ok",
                allow_private_network=True, pin_dns=True)
            self.assertEqual(result.status, 200)
            self.assertEqual(result.body, b"hello-from-pinned")
            self.assertEqual(server.httpd.seen_hosts,
                             [f"[::1]:{server.port}"])


class AddressHelpersTests(unittest.TestCase):
    def test_private_addresses_include_metadata_and_mapped_forms(self):
        import ipaddress

        for raw in ("127.0.0.1", "10.1.2.3", "169.254.169.254",
                    "::1", "fc00::1", "::ffff:127.0.0.1"):
            with self.subTest(raw=raw):
                self.assertTrue(net.is_private_address(ipaddress.ip_address(raw)))
        self.assertFalse(net.is_private_address(
            ipaddress.ip_address("93.184.216.34")))

    def test_validate_fetch_url_contract_is_unchanged(self):
        with self.assertRaises(net.InvalidTarget):
            net.validate_fetch_url("ftp://example.com/",
                                   allow_private_network=True)
        parsed = net.validate_fetch_url("http://example.com/x",
                                        allow_private_network=True)
        self.assertEqual(parsed.hostname, "example.com")

    def test_guarded_redirect_rejects_disallowed_target(self):
        handler = net.GuardedRedirectHandler(
            lambda target: (_ for _ in ()).throw(
                net.InvalidTarget("redirect to disallowed target")))
        with self.assertRaises(net.InvalidTarget):
            handler.redirect_request(
                None, None, 302, "Found", {}, "http://internal.test/")


if __name__ == "__main__":
    unittest.main()

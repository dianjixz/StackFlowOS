"""Boundary-safe policy matchers (M1: ``url_prefix``).

``url_prefix`` must compare URL structure, never raw string prefixes: a rule
for ``https://api.example.com`` must not match
``https://api.example.com.evil.io/x``.
"""

import tempfile
import unittest
from pathlib import Path

from stackflowos.kernel import Context
from stackflowos.l2.eventlog import EventLog
from stackflowos.security.policy import PolicyEngine, _url_prefix_match


SPEC_VECTORS = (
    # url, expected, want
    ("https://api.example.com/x", "https://api.example.com", True),
    ("https://api.example.com/x/y", "https://api.example.com/x", True),
    ("https://api.example.com.evil.io/x", "https://api.example.com", False),
    ("https://api.example.com:8443/x", "https://api.example.com", False),
    ("http://api.example.com/x", "https://api.example.com", False),
    ("https://api.example.com/xevil", "https://api.example.com/x", False),
    ("https://user:pass@api.example.com/x", "https://api.example.com", False),
)


class UrlPrefixMatchTests(unittest.TestCase):
    def test_spec_vectors(self):
        for url, expected, want in SPEC_VECTORS:
            with self.subTest(url=url, expected=expected):
                self.assertIs(_url_prefix_match(url, expected), want)

    def test_path_prefix_is_segment_bounded(self):
        cases = (
            ("https://api.example.com/", "https://api.example.com", True),
            ("https://api.example.com", "https://api.example.com", True),
            ("https://api.example.com/x/", "https://api.example.com/x", True),
            ("https://api.example.com/x/y/z", "https://api.example.com/x/", True),
            ("https://api.example.com/xy", "https://api.example.com/x", False),
            ("https://api.example.com/x", "https://api.example.com/", True),
        )
        for url, expected, want in cases:
            with self.subTest(url=url, expected=expected):
                self.assertIs(_url_prefix_match(url, expected), want)

    def test_default_ports_are_equivalent_to_explicit_ones(self):
        cases = (
            ("https://api.example.com:443/x", "https://api.example.com", True),
            ("https://api.example.com/x", "https://api.example.com:443", True),
            ("http://api.example.com:80/x", "http://api.example.com", True),
            ("http://api.example.com:443/x", "http://api.example.com", False),
            ("https://api.example.com:80/x", "https://api.example.com", False),
            ("https://api.example.com:8443/x", "https://api.example.com:8443", True),
        )
        for url, expected, want in cases:
            with self.subTest(url=url, expected=expected):
                self.assertIs(_url_prefix_match(url, expected), want)

    def test_scheme_and_host_are_case_insensitive(self):
        self.assertIs(
            _url_prefix_match("HTTPS://API.Example.COM/x",
                              "https://api.example.com"), True)
        self.assertIs(
            _url_prefix_match("https://api.example.com/x",
                              "HTTPS://API.EXAMPLE.COM"), True)

    def test_ipv6_brackets_are_compared_without_brackets(self):
        cases = (
            ("http://[::1]:17800/x", "http://[::1]:17800", True),
            ("http://[::1]:17801/x", "http://[::1]:17800", False),
            ("http://[::1]:17800/x", "http://[::1]:17801", False),
            ("https://[::1]:17800/x", "http://[::1]:17800", False),
            ("http://[::1]/x", "http://[::1]", True),
        )
        for url, expected, want in cases:
            with self.subTest(url=url, expected=expected):
                self.assertIs(_url_prefix_match(url, expected), want)

    def test_userinfo_is_rejected_on_either_side(self):
        cases = (
            ("https://user@api.example.com/x", "https://api.example.com", False),
            ("https://user:pass@api.example.com/x", "https://api.example.com", False),
            ("https://api.example.com/x", "https://user:pass@api.example.com", False),
            ("https://api.example.com@evil.io/x", "https://api.example.com", False),
        )
        for url, expected, want in cases:
            with self.subTest(url=url, expected=expected):
                self.assertIs(_url_prefix_match(url, expected), want)

    def test_malformed_urls_return_false_without_raising(self):
        cases = (
            ("", "https://api.example.com"),
            ("not a url", "https://api.example.com"),
            ("//api.example.com/x", "//api.example.com"),
            ("http://[::1", "http://[::1]"),
            ("https://api.example.com:bad/x", "https://api.example.com"),
            ("https://api.example.com/x", "https://api.example.com:bad"),
            ("https:///x", "https://"),
        )
        for url, expected in cases:
            with self.subTest(url=url, expected=expected):
                self.assertIs(_url_prefix_match(url, expected), False)

    def test_non_url_expectations_never_match(self):
        for expected in (None, 42, "", "*", "https://"):
            with self.subTest(expected=expected):
                self.assertIs(
                    _url_prefix_match("https://api.example.com/x", expected),
                    False)


class PolicyUrlPrefixIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """The engine's scope matcher must use the structural matcher."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.ctx = Context(name="test")
        self.eventlog = EventLog(root / "events")
        self.policy = PolicyEngine(
            self.ctx, self.eventlog,
            rules_path=root / "security" / "rules.json",
            projects_root=root / "projects")

    async def asyncTearDown(self):
        await self.ctx.dispose()
        await self.eventlog.close()
        self._tmp.cleanup()

    def test_match_scope_uses_url_structure(self):
        expected = {"url_prefix": "https://api.example.com"}
        self.assertTrue(self.policy.match_scope(
            {"url": "https://api.example.com/x"}, expected))
        self.assertFalse(self.policy.match_scope(
            {"url": "https://api.example.com.evil.io/x"}, expected))
        self.assertFalse(self.policy.match_scope(
            {"url": "https://api.example.com:8443/x"}, expected))
        self.assertFalse(self.policy.match_scope(
            {"url": "https://user:pass@api.example.com/x"}, expected))
        self.assertFalse(self.policy.match_scope({"url": None}, expected))
        self.assertFalse(self.policy.match_scope({}, expected))


if __name__ == "__main__":
    unittest.main()

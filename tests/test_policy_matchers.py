"""Boundary-safe policy matchers (M1 ``url_prefix``, M2 ``path_glob``).

``url_prefix`` must compare URL structure, never raw string prefixes: a rule
for ``https://api.example.com`` must not match
``https://api.example.com.evil.io/x``.

``path_glob`` must match the *normalized* absolute path only, with ``*``
bounded to one path segment and ``**`` crossing directories, so a traversal
path cannot smuggle itself past a workspace rule.
"""

import tempfile
import unittest
from pathlib import Path

from stackflowos.kernel import Context
from stackflowos.l2.eventlog import EventLog
from stackflowos.security.policy import (
    PolicyEngine,
    _match_path_glob,
    _url_prefix_match,
)


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


class PathGlobMatchTests(unittest.TestCase):
    """M2: ``path_glob`` matches normalized paths with segment-safe wildcards."""

    def test_spec_vectors(self):
        cases = (
            # path, pattern, want
            ("/ws/proj/a", "/ws/proj/*", True),
            ("/ws/proj/a/b", "/ws/proj/*", False),
            ("/ws/proj/a/b", "/ws/proj/**", True),
            # ``..`` is resolved before matching (/ws/proj/../../etc/passwd
            # canonicalises to /etc/passwd), so the old raw-path bypass dies.
            ("/ws/proj/../../etc/passwd", "/ws/proj/*", False),
            # A pattern containing a ``..`` segment is never allowed.
            ("/ws/proj/a", "/ws/proj/*/../*", False),
        )
        for path, pattern, want in cases:
            with self.subTest(path=path, pattern=pattern):
                self.assertIs(_match_path_glob(path, pattern), want)

    def test_star_is_bounded_to_one_segment(self):
        cases = (
            ("/ws/proj/a/b", "/ws/proj/a/*", True),
            ("/ws/proj/a/b/c", "/ws/proj/a/*", False),
            ("/ws/proj/a/b", "/ws/proj/*/*", True),
            ("/ws/proj/a/b/c", "/ws/proj/*/*", False),
            ("/ws/proj/a-b", "/ws/proj/a*b", True),
            ("/ws/proj/axb", "/ws/proj/a*b", True),
            ("/ws/proj/axyzb", "/ws/proj/a*b", True),
            ("/ws/proj", "/ws/proj/*", False),
        )
        for path, pattern, want in cases:
            with self.subTest(path=path, pattern=pattern):
                self.assertIs(_match_path_glob(path, pattern), want)

    def test_double_star_crosses_segments(self):
        cases = (
            ("/ws/proj/a", "/ws/proj/**", True),
            ("/ws/proj/a/b", "/ws/proj/**", True),
            ("/ws/proj/a/b/c/d", "/ws/proj/**", True),
            ("/ws/proj/a/b/c", "/ws/proj/*/**", True),
            ("/ws/proj/a", "/ws/proj/**/c", False),
            ("/ws/proj/a/b/c", "/ws/proj/**/c", True),
            ("/ws/proj/x/c", "/ws/proj/**/c", True),
        )
        for path, pattern, want in cases:
            with self.subTest(path=path, pattern=pattern):
                self.assertIs(_match_path_glob(path, pattern), want)

    def test_question_mark_matches_one_non_separator_char(self):
        cases = (
            ("/ws/proj/a", "/ws/proj/?", True),
            ("/ws/proj/ab", "/ws/proj/?", False),
            ("/ws/proj/a", "/ws/proj/?b", False),
            ("/ws/proj/ab", "/ws/proj/a?", True),
            ("/ws/proj/a/b", "/ws/proj/?", False),
        )
        for path, pattern, want in cases:
            with self.subTest(path=path, pattern=pattern):
                self.assertIs(_match_path_glob(path, pattern), want)

    def test_character_classes(self):
        cases = (
            ("/ws/proj/a", "/ws/proj/[abc]", True),
            ("/ws/proj/b", "/ws/proj/[abc]", True),
            ("/ws/proj/d", "/ws/proj/[abc]", False),
            ("/ws/proj/d", "/ws/proj/[!abc]", True),
            ("/ws/proj/a", "/ws/proj/[!abc]", False),
            ("/ws/proj/a", "/ws/proj/[^abc]", False),
            ("/ws/proj/d", "/ws/proj/[^abc]", True),
            # A class must never match the separator, negated or not.
            ("/ws/proj/x/y", "/ws/proj/[!abc]", False),
            ("/ws/proj/x/y", "/ws/proj/[abc]", False),
        )
        for path, pattern, want in cases:
            with self.subTest(path=path, pattern=pattern):
                self.assertIs(_match_path_glob(path, pattern), want)

    def test_regex_metacharacters_are_literal(self):
        cases = (
            ("/ws/proj/a.b", "/ws/proj/a.b", True),
            ("/ws/proj/axb", "/ws/proj/a.b", False),
            ("/ws/proj/a+b", "/ws/proj/a+b", True),
            ("/ws/proj/a(b)", "/ws/proj/a(b)", True),
            ("/ws/proj/a$", "/ws/proj/a$", True),
        )
        for path, pattern, want in cases:
            with self.subTest(path=path, pattern=pattern):
                self.assertIs(_match_path_glob(path, pattern), want)

    def test_unsafe_or_malformed_patterns_never_match(self):
        patterns = (
            "",                    # empty
            "proj/*",              # relative
            "*.md",
            None,                  # non-string
            42,
            b"/ws/proj/*",
            "/ws/proj/\x00*",      # NUL in pattern
            "/ws/proj/*\x00",
            "/ws/proj/../*",       # ``..`` segment
            "/ws/../ws/proj/*",
            "/ws/proj/a/..",
            "/ws/proj/[abc",       # unterminated class
            "/ws/proj/[]",         # empty class
            "/ws/proj/[!]",        # empty negated class
            "/ws/proj/[a/b]",      # class would cross a segment
            "/ws/proj/[[:alpha:]]",  # POSIX classes are unsupported
        )
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                self.assertIs(_match_path_glob("/ws/proj/a", pattern), False)

    def test_nul_in_path_never_matches(self):
        self.assertIs(_match_path_glob("/ws/proj/\x00a", "/ws/proj/*"), False)

    def test_percent_escapes_are_not_decoded_here(self):
        # Unquoting belongs to the caller; the policy layer must treat
        # ``%2e%2e`` as a literal file name, never as ``..``.
        self.assertIs(_match_path_glob("/ws/proj/%2e%2e/etc", "/ws/proj/*"),
                      False)
        self.assertIs(
            _match_path_glob("/ws/proj/%2e%2e/etc", "/ws/proj/%2e%2e/*"), True)

    def test_relative_concrete_path_is_resolved_against_cwd(self):
        self.assertIs(_match_path_glob("a", "/ws/proj/*", cwd="/ws/proj"), True)
        self.assertIs(
            _match_path_glob("../etc/passwd", "/ws/proj/*", cwd="/ws/proj"),
            False)
        self.assertIs(
            _match_path_glob("../../etc/passwd", "/ws/proj/*", cwd="/ws/proj"),
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


class PolicyPathGlobIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """The engine's scope matcher must use the normalized, bounded matcher."""

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

    def test_match_scope_normalizes_and_bounds_path_glob(self):
        one_level = {"path_glob": "/ws/proj/*"}
        any_depth = {"path_glob": "/ws/proj/**"}
        self.assertTrue(self.policy.match_scope({"path": "/ws/proj/a"}, one_level))
        self.assertFalse(self.policy.match_scope(
            {"path": "/ws/proj/a/b"}, one_level))
        self.assertTrue(self.policy.match_scope(
            {"path": "/ws/proj/a/b"}, any_depth))
        # Normalized first: the traversal path is /etc/passwd, not under proj.
        self.assertFalse(self.policy.match_scope(
            {"path": "/ws/proj/../../etc/passwd"}, one_level))
        # The raw (pre-normalization) string is never matched either.
        self.assertFalse(self.policy.match_scope(
            {"cwd": "/ws/proj/../../etc"}, one_level))
        # Pattern must be absolute.
        self.assertFalse(self.policy.match_scope(
            {"path": "/ws/proj/a"}, {"path_glob": "proj/*"}))
        # No usable concrete path.
        self.assertFalse(self.policy.match_scope({}, one_level))


if __name__ == "__main__":
    unittest.main()

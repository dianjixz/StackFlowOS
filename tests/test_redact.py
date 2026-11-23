"""Credential redaction (M8) - one rule set for previews and operator audits."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stackflowos.agents.models import Agent
from stackflowos.agents.tools import ToolExecutor
from stackflowos.config import SecurityCfg
from stackflowos.session.operator import _redact_args
from stackflowos.util.redact import is_secret_key, redact_text

#: A realistic three-segment JWT (header/payload/signature).
JWT = ("eyJhbGciOiJIUzI1NiJ9."
       "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
       "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
GHP = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"
NPM = "npm_" + "abcdefghijklmnopqrstuvwxyz"
OPENAI = "sk-" + "abcdefghijklmnop"

#: Inputs that must survive redaction verbatim (no false positives).
BENIGN = (
    "/home/user/project",
    "make test",
    "echo hello",
    "python3 -m unittest discover -s tests -q",
    "普通中文文本，没有密钥或令牌。",
    "tokenizer 和 monkey key 不是密钥",
    "The Authorization header is required",
    "https://example.com/path?q=1",
)


class RedactTextTests(unittest.TestCase):
    """Inline rules from docs/security-fix-plan.md §6.17.3."""

    def assert_redacted(self, text: str, *secrets: str) -> str:
        out = redact_text(text)
        for secret in secrets:
            self.assertNotIn(secret, out)
        self.assertIn("***", out)
        return out

    def test_authorization_header_masks_whole_value(self) -> None:
        self.assertEqual(self.assert_redacted(
            "Authorization: Bearer abcdefgh", "abcdefgh"), "Authorization: ***")

    def test_proxy_authorization_and_set_cookie(self) -> None:
        out = self.assert_redacted(
            "Proxy-Authorization: Basic dXNlcjphYmM=", "dXNlcjphYmM=")
        self.assertEqual(out, "Proxy-Authorization: ***")
        self.assertEqual(self.assert_redacted(
            "Set-Cookie: session=abc; HttpOnly", "session=abc"),
            "Set-Cookie: ***")

    def test_bare_bearer_token(self) -> None:
        self.assertEqual(self.assert_redacted(
            "Bearer abcdefgh", "abcdefgh"), "Bearer ***")

    def test_inline_key_value_pair_keeps_the_key(self) -> None:
        self.assertEqual(self.assert_redacted(
            "api_key=sk-123456", "123456"), "api_key=***")

    def test_url_userinfo(self) -> None:
        self.assertEqual(self.assert_redacted(
            "https://user:pass@example.com/x", "user:pass"),
            "https://***@example.com/x")

    def test_cookie_header(self) -> None:
        self.assertEqual(self.assert_redacted(
            "Cookie: session=abc", "session=abc"), "Cookie: ***")

    def test_jwt_and_provider_tokens(self) -> None:
        for token in (JWT, GHP, NPM, OPENAI):
            with self.subTest(token=token):
                self.assertEqual(redact_text(f"key is {token}"), "key is ***")

    def test_environment_variable_assignments(self) -> None:
        for text, name in (
            ("SFO_LLM_API_KEY=sk-proj-abcdefghijkl", "SFO_LLM_API_KEY"),
            ("MY_TOKEN=tok-123", "MY_TOKEN"),
            ("export AWS_SECRET_ACCESS_KEY=abcdef123456",
             "AWS_SECRET_ACCESS_KEY"),
        ):
            with self.subTest(name=name):
                out = redact_text(text)
                self.assertIn(name, out)
                self.assertNotIn("abcdef", out)
                self.assertIn("***", out)

    def test_plain_text_is_not_touched(self) -> None:
        for text in BENIGN:
            with self.subTest(text=text):
                self.assertEqual(redact_text(text), text)

    def test_idempotent(self) -> None:
        corpus = list(BENIGN) + [
            "Authorization: Bearer abcdefgh",
            "Bearer abcdefgh",
            "api_key=sk-123456",
            "https://user:pass@example.com/x",
            "Cookie: session=abc",
            "SFO_LLM_API_KEY=sk-proj-abcdefghijkl",
            JWT, GHP, NPM, OPENAI,
            'password="hunter2"',
            "x-api-key: abcdef123456",
        ]
        for text in corpus:
            with self.subTest(text=text):
                once = redact_text(text)
                self.assertEqual(redact_text(once), once)

    def test_secret_key_names(self) -> None:
        for key in ("token", "api_key", "Authorization", "X-Api-Key",
                    "password", "credential", "SECRET"):
            with self.subTest(key=key):
                self.assertTrue(is_secret_key(key))
        for key in ("path", "command", "url", "note", "title"):
            with self.subTest(key=key):
                self.assertFalse(is_secret_key(key))


class SafeArgsTests(unittest.TestCase):
    """The executor's ``tool.invoked`` args use the same rules as audits."""

    def setUp(self) -> None:
        # ``_safe_args`` is a pure formatter; it needs no live executor.
        self.executor = object.__new__(ToolExecutor)

    def test_content_value_and_patch_stay_dropped(self) -> None:
        out = self.executor._safe_args({
            "path": "conf.txt",
            "content": "top-secret-body",
            "value": "top-secret-value",
            "patch": {"token": "top-secret-patch"},
        })
        self.assertEqual(out, {"path": "conf.txt"})

    def test_secret_named_args_are_masked_by_key(self) -> None:
        out = self.executor._safe_args({
            "token": "abc123",
            "api_key": "abcdef123456",
            "note": "Authorization: Bearer abcdefgh",
            "path": "/home/user/project",
            "max_bytes": 10,
            "flag": True,
        })
        self.assertEqual(out["token"], "***")
        self.assertEqual(out["api_key"], "***")
        self.assertNotIn("abcdefgh", out["note"])
        self.assertIn("***", out["note"])
        self.assertEqual(out["path"], "/home/user/project")
        self.assertEqual(out["max_bytes"], 10)
        self.assertTrue(out["flag"])

    def test_operator_audit_args_share_the_rules(self) -> None:
        out = _redact_args({
            "password": "hunter2",
            "note": "Bearer abcdefgh",
            "path": "/home/user/project",
            "nested": {"SFO_LLM_API_KEY": "sk-proj-abcdefghijkl"},
        })
        self.assertEqual(out["password"], "***")
        self.assertEqual(out["note"], "Bearer ***")
        self.assertEqual(out["path"], "/home/user/project")
        self.assertEqual(out["nested"]["SFO_LLM_API_KEY"], "***")


class PreviewRedactionTests(unittest.TestCase):
    """The fs.write approval preview goes through the same redactor."""

    def test_write_preview_hides_authorization_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executor = ToolExecutor(
                None,
                agent=Agent(id="agent-coder-1", role="coder",
                            project_id="p-1"),
                workspace=Path(tmp),
                security_config=SecurityCfg(),
            )
            _, _, preview = executor._prepare(
                "fs.write",
                {"path": "conf.txt",
                 "content": "Authorization: Bearer abcdefgh\n"})
        self.assertIn("Authorization: ***", preview)
        self.assertNotIn("abcdefgh", preview)

    def test_shell_preview_hides_inline_bearer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executor = ToolExecutor(
                None,
                agent=Agent(id="agent-coder-1", role="coder",
                            project_id="p-1"),
                workspace=Path(tmp),
                security_config=SecurityCfg(),
            )
            _, _, preview = executor._prepare(
                "shell.exec",
                {"command": "curl -H 'Authorization: Bearer abcdefgh'",
                 "cwd": "."})
        self.assertNotIn("abcdefgh", preview)
        self.assertIn("***", preview)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

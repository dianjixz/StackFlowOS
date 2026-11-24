"""operator._audit redacts and truncates result/error fields (M8 follow-up)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stackflowos.config import default_root
from stackflowos.session.operator import OPERATOR_TOOLS, OperatorTools

from tests.test_sec_support import FakeApp

REPO = default_root()


def _last_event(app):
    records = app.eventlog.tail(1)
    assert records, "no audit event was appended"
    return records[0].to_dict()


class OperatorAuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.app = FakeApp(self.tmp)
        self.app.config.security.audit_max_field_chars = 120
        self.app.config.security.audit_redact = True
        self.tools = OperatorTools(self.app)
        self.spec = OPERATOR_TOOLS["project.get"]

    async def asyncTearDown(self):
        await self.app.eventlog.close()
        self._tmp.cleanup()

    async def test_result_is_redacted_and_truncated(self):
        secret = "abcdef1234567890"
        self.tools._audit(
            "project.get", self.spec, {"project_id": "p-1"}, ok=True,
            subject="human:operator",
            data={
                "token": secret,
                "url": "https://user:pass@example.com/x",
                "note": f"Authorization: Bearer {secret}",
                "filler": "x" * 400,
            })
        payload = _last_event(self.app)["data"]
        result = payload["result"]
        self.assertLessEqual(len(result), 120)
        self.assertNotIn(secret, result)
        self.assertNotIn("Bearer " + secret, result)
        self.assertNotIn("user:pass", result)
        self.assertIn("***", result)

    async def test_error_is_redacted_and_truncated(self):
        self.tools._audit(
            "project.get", self.spec, {"project_id": "p-1"}, ok=False,
            subject="human:operator",
            error="failed token=supersecret123456 " + "y" * 400)
        payload = _last_event(self.app)["data"]
        error = payload["error"]
        self.assertLessEqual(len(error), 120)
        self.assertNotIn("supersecret123456", error)
        self.assertIn("token=***", error)

    async def test_unknown_tool_error_is_redacted(self):
        token = "ghp_" + "A" * 30
        result = await self.tools.run(token)
        self.assertFalse(result["ok"])
        payload = _last_event(self.app)["data"]
        self.assertNotIn(token, payload["error"])
        self.assertIn("***", payload["error"])

    async def test_audit_is_append_only(self):
        self.tools._audit("a", self.spec, {}, ok=True, subject="s",
                          data={"token": "first-secret-1111"})
        first = _last_event(self.app)
        self.tools._audit("b", self.spec, {}, ok=True, subject="s",
                          data={"token": "second-secret-2222"})
        second = _last_event(self.app)
        self.assertNotEqual(first["seq"], second["seq"])
        original = first["data"]["result"]
        self.assertNotIn("second-secret-2222", original)
        # Re-reading the first record still yields the same payload.
        again = self.app.eventlog.since(first["seq"] - 1, limit=1)[0].to_dict()
        self.assertEqual(again["data"]["result"], original)

    async def test_args_remain_redacted(self):
        self.tools._audit(
            "project.get", self.spec,
            {"project_id": "p-1", "token": "args-secret-3333"}, ok=True,
            subject="s", data={})
        payload = _last_event(self.app)["data"]
        self.assertEqual(payload["args"]["token"], "***")
        self.assertNotIn("args-secret-3333", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()

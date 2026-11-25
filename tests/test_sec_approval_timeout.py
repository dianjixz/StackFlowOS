"""W3 item 5: approval timeouts are an explicit denial + escalation, not a
silent 300s drop.

The broker already refused expired requests (fail closed); this suite pins the
new observable parts:

* a WARNING is logged when a pending request expires;
* ``approval.timeout`` is appended with ``decision="deny"`` and
  ``escalated=True``;
* the bounded ``timeout_queue()`` (the minimal operator queue) gets one entry;
* the optional ``timeout_handler`` is invoked but can never approve anything
  and its failures do not break expiry;
* a request decided before the deadline is *not* escalated.
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stackflowos.config import SecurityCfg, default_root
from stackflowos.errors import Conflict
from stackflowos.kernel import Context
from stackflowos.l2.eventlog import EventLog
from stackflowos.security.approvals import ApprovalBroker
from stackflowos.security.policy import PolicyEngine

REPO = default_root()


class ApprovalTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.dir = Path(self._tmp.name)
        self.ctx = Context(name="test")
        self.log = EventLog(self.dir / "events")
        self.projects_root = self.dir / "projects"
        self.workspace = self.projects_root / "p-1" / "workspace"
        self.workspace.mkdir(parents=True)
        self.rules_path = self.dir / "security" / "rules.json"
        self.approvals_path = self.dir / "security" / "approvals.json"
        self.logger = logging.getLogger("sfo.security.approvals")
        self.handled: list = []
        self.policy = PolicyEngine(self.ctx, self.log,
                                   rules_path=self.rules_path,
                                   projects_root=self.projects_root)
        self.broker = ApprovalBroker(
            self.ctx, self.policy, self.log,
            approvals_path=self.approvals_path,
            approval_ttl_s=300, grant_ttl_s=3600,
            timeout_handler=self.handled.append,
            logger=self.logger)

    async def asyncTearDown(self):
        await self.ctx.dispose()
        await self.log.close()
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def _pending(self):
        request = self.broker.request(
            "agent.coder.1", "shell.exec",
            {"cwd": str(self.workspace), "command": "echo hi"},
            project_id="p-1", reason="timeout test", preview="echo hi")
        self.assertEqual(request.status, "pending")
        return request

    def _timeout_events(self) -> list:
        return [record for record in
                self.log.query(types=["approval.timeout"], limit=50)]

    # -- tests -----------------------------------------------------------
    def test_default_approval_ttl_is_300_seconds(self):
        # Premise check: the default is 300s and a timeout is a denial.
        self.assertEqual(SecurityCfg().approval_ttl_s, 300)

    async def test_sweep_timeout_denies_and_escalates(self):
        request = self._pending()
        with self.assertLogs(self.logger.name, level="WARNING") as captured:
            expired = self.broker.sweep(now_ms=request.expires_at + 1)
        self.assertEqual([item.id for item in expired], [request.id])
        self.assertEqual(self.broker.get(request.id).status, "expired")
        self.assertTrue(any("timed out" in line for line in captured.output))

        events = self._timeout_events()
        self.assertEqual(len(events), 1)
        data = events[0].data
        self.assertEqual(data["decision"], "deny")
        self.assertTrue(data["escalated"])
        self.assertEqual(data["escalation"]["request_id"], request.id)
        self.assertEqual(data["escalation"]["source"], "sweep")
        self.assertEqual(data["escalation"]["escalated_to"], "operator")

        queue = self.broker.timeout_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["request_id"], request.id)
        self.assertEqual(queue[0]["escalated_to"], "operator")
        # The hook observed the escalation but cannot approve it.
        self.assertEqual([item.id for item in self.handled], [request.id])
        with self.assertRaises(Conflict):
            self.broker.decide(request.id, decision="allow")
        # A later sweep does not double-record the same timeout.
        self.broker.sweep(now_ms=request.expires_at + 5_000)
        self.assertEqual(len(self._timeout_events()), 1)

    async def test_poll_timeout_escalates(self):
        request = self._pending()
        with mock.patch("stackflowos.security.approvals._now_ms",
                        return_value=request.expires_at + 1_000):
            with self.assertLogs(self.logger.name, level="WARNING"):
                refreshed = self.broker.poll(request.id)
        self.assertEqual(refreshed.status, "expired")
        events = self._timeout_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data["escalation"]["source"], "poll")
        self.assertEqual(self.broker.timeout_queue()[0]["source"], "poll")

    async def test_decision_before_timeout_is_not_escalated(self):
        request = self._pending()
        self.broker.decide(request.id, decision="allow", mode="once",
                           by="operator")
        self.assertEqual(self.broker.get(request.id).status, "approved")
        self.assertEqual(self._timeout_events(), [])
        self.assertEqual(self.broker.timeout_queue(), [])
        self.assertEqual(self.handled, [])

    async def test_failing_escalation_handler_does_not_break_denial(self):
        def boom(_request):
            raise RuntimeError("operator queue unavailable")

        self.broker.set_timeout_handler(boom)
        request = self._pending()
        with self.assertLogs(self.logger.name, level="WARNING"):
            expired = self.broker.sweep(now_ms=request.expires_at + 1)
        self.assertEqual([item.id for item in expired], [request.id])
        self.assertEqual(self.broker.get(request.id).status, "expired")
        self.assertEqual(len(self._timeout_events()), 1)
        # Clearing the hook stops further escalation calls.
        self.broker.set_timeout_handler(None)
        self.assertIsNone(self.broker._timeout_handler)


if __name__ == "__main__":
    unittest.main()

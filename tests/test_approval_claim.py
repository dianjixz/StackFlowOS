"""M10: one-shot approvals are atomic and executed through a single claim.

Covers security plan §5.4.6:

* ``PolicyEngine.evaluate_and_consume`` decides and spends a one-shot grant
  under one lock, so two concurrent identical requests cannot both win;
* ``ApprovalBroker.claim`` binds an approved ``once`` request to exactly one
  execution (``claim_id``), later claims conflict and ``use`` never deducts a
  claimed grant twice;
* the tool executor claims *before* touching the handler and fails closed;
* the ``approval.claim`` RPC is owner-bound (H3) while operators keep access;
* ``claim_id``/``claimed_at`` round-trip through approvals.json.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from stackflowos.agents.models import Agent
from stackflowos.agents.tools import ToolExecutor
from stackflowos.app import App
from stackflowos.config import Config, SecurityCfg, default_root
from stackflowos.errors import Conflict, Denied
from stackflowos.kernel import Context
from stackflowos.l2.eventlog import EventLog
from stackflowos.security import (
    ApprovalBroker,
    ApprovalRequest,
    PolicyEngine,
)

REPO = default_root()


def _tmpdir(prefix: str) -> tempfile.TemporaryDirectory:
    base = REPO / ".stackflowos" / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=str(base))


class ApprovalClaimTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = _tmpdir("m10-")
        self.dir = Path(self._tmp.name)
        self.ctx = Context(name="test")
        self.events: list[tuple[str, dict]] = []
        self.ctx.on_any(
            lambda mode, name, payload: self.events.append((name, payload)))
        self.log = EventLog(self.dir / "events")
        self.projects_root = self.dir / "projects"
        self.workspace = self.projects_root / "p-1" / "workspace"
        self.workspace.mkdir(parents=True)
        self.rules_path = self.dir / "security" / "rules.json"
        self.approvals_path = self.dir / "security" / "approvals.json"
        self.policy = PolicyEngine(
            self.ctx, self.log, rules_path=self.rules_path,
            projects_root=self.projects_root)
        self.broker = ApprovalBroker(
            self.ctx, self.policy, self.log,
            approvals_path=self.approvals_path,
            approval_ttl_s=300, grant_ttl_s=3600)

    async def asyncTearDown(self):
        await self.ctx.dispose()
        await self.log.close()
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def _request(self, command: str = "make test", *,
                 principal: str = "agent-coder-1"):
        request = self.broker.request(
            principal, "shell.exec",
            {"cwd": str(self.workspace), "command": command},
            project_id="p-1")
        self.assertEqual(request.status, "pending")
        return request

    def _approved_once(self, command: str = "make test"):
        request = self._request(command)
        self.broker.decide(request.id, decision="allow", mode="once",
                           by="operator")
        return self.broker.get(request.id)

    def _event_data(self, name: str) -> list[dict]:
        return [payload["data"] for event, payload in self.events
                if event == name]

    # -- atomic evaluate + consume --------------------------------------
    async def test_concurrent_requests_spend_one_shot_grant_once(self):
        self._approved_once()
        scope = {"cwd": str(self.workspace), "command": "make test"}

        results = await asyncio.gather(
            asyncio.to_thread(self.broker.request, "agent-coder-1",
                              "shell.exec", dict(scope), project_id="p-1"),
            asyncio.to_thread(self.broker.request, "agent-coder-1",
                              "shell.exec", dict(scope), project_id="p-1"),
        )
        statuses = sorted(request.status for request in results)
        self.assertEqual(statuses, ["approved", "pending"],
                         "exactly one concurrent request may win a one-shot")
        winner = next(r for r in results if r.status == "approved")
        self.assertEqual(winner.mode, "grant")
        self.assertIsNotNone(winner.grant_id)
        # The winner already consumed the grant, so it is gone and a later
        # audit ``use`` reports False instead of failing the execution.
        self.assertIsNone(self.policy.get_rule(winner.grant_id))
        self.assertFalse(self.broker.use(winner.grant_id))
        # The request-time spend is still audited exactly once.
        self.assertEqual(len(self._event_data("approval.used")), 1)

    def test_evaluate_and_consume_never_returns_a_spent_allow(self):
        grant = self.policy.add_rule(
            effect="allow", capability="fs.write", principal="agent-coder-1",
            scope={}, risk_max="high", uses_left=1, source="grant")
        scope = {"path": str(self.workspace / "a.txt")}
        first = self.policy.evaluate_and_consume(
            "agent-coder-1", "fs.write", scope, risk="medium")
        self.assertEqual(first.outcome, "allow")
        self.assertEqual(first.rule.id, grant.id)
        self.assertIsNone(self.policy.get_rule(grant.id))
        second = self.policy.evaluate_and_consume(
            "agent-coder-1", "fs.write", scope, risk="medium")
        self.assertEqual(second.outcome, "ask")
        self.assertIsNone(second.rule)

    # -- claim semantics -------------------------------------------------
    def test_claim_is_single_use_and_owner_bound(self):
        request = self._approved_once()
        grant_id = request.grant_id
        claim_id = self.broker.claim(request.id)
        self.assertTrue(claim_id.startswith("clm"))
        stored = self.broker.get(request.id)
        self.assertEqual(stored.claim_id, claim_id)
        self.assertIsNotNone(stored.claimed_at)
        self.assertEqual(stored.status, "approved")

        with self.assertRaises(Conflict):
            self.broker.claim(request.id)

        # claim already spent the grant; the legacy path must not deduct again
        self.assertFalse(self.broker.use(grant_id))
        self.assertFalse(self.broker.use(grant_id))
        self.assertIsNone(self.policy.get_rule(grant_id))

        claimed = self._event_data("approval.claimed")
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["claim_id"], claim_id)
        self.assertEqual(claimed[0]["request_id"], request.id)
        # Audit event carries identifiers only, never preview/scope content.
        self.assertNotIn("preview", claimed[0])
        self.assertNotIn("request", claimed[0])
        self.assertNotIn("scope", claimed[0])

    def test_wrong_owner_cannot_claim(self):
        request = self._approved_once()
        with self.assertRaises(Denied):
            self.broker.claim(request.id, principal="agent-coder-2")
        self.assertIsNone(self.broker.get(request.id).claim_id)
        self.assertIsNotNone(self.policy.get_rule(request.grant_id))
        # The real owner can still claim afterwards.
        self.assertTrue(self.broker.claim(request.id))

    def test_operator_may_claim_without_owner(self):
        request = self._approved_once()
        self.assertTrue(self.broker.claim(request.id, principal=None))
        with self.assertRaises(Conflict):
            self.broker.claim(request.id, principal=None)

    async def test_concurrent_claims_have_exactly_one_winner(self):
        request = self._approved_once()
        results = await asyncio.gather(
            asyncio.to_thread(self._try_claim, request.id),
            asyncio.to_thread(self._try_claim, request.id),
        )
        winners = [item for item in results if isinstance(item, str)]
        conflicts = [item for item in results if isinstance(item, Conflict)]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(conflicts), 1, results)
        self.assertEqual(self.broker.get(request.id).claim_id, winners[0])

    def _try_claim(self, request_id: str):
        try:
            return self.broker.claim(request_id)
        except Conflict as exc:
            return exc

    def test_claim_rejects_pending_non_once_and_spent_grants(self):
        pending = self._request("pending thing")
        with self.assertRaises(Conflict):
            self.broker.claim(pending.id)
        self.assertIsNone(self.broker.get(pending.id).claim_id)

        # An auto-allow request has no one-shot grant to claim.
        auto = self.broker.request(
            "agent-coder-1", "fs.read",
            {"path": str(self.workspace / "a.txt")}, project_id="p-1")
        self.assertEqual(auto.status, "approved")
        with self.assertRaises(Conflict):
            self.broker.claim(auto.id)

        # A session grant also has no single claim slot.
        session = self._request("session thing")
        self.broker.decide(session.id, decision="allow", mode="session")
        with self.assertRaises(Conflict):
            self.broker.claim(session.id)

        # A legacy ``use`` already spent the grant: claim fails closed.
        once = self._approved_once("legacy use")
        self.assertTrue(self.broker.use(once.grant_id))
        with self.assertRaises(Conflict):
            self.broker.claim(once.id)
        self.assertIsNone(self.broker.get(once.id).claim_id)

    def test_claim_id_persists_across_reload(self):
        request = self._approved_once()
        claim_id = self.broker.claim(request.id)
        reloaded = ApprovalBroker(
            self.ctx, self.policy, self.log,
            approvals_path=self.approvals_path,
            approval_ttl_s=300, grant_ttl_s=3600)
        loaded = reloaded.get(request.id)
        self.assertEqual(loaded.status, "approved")
        self.assertEqual(loaded.mode, "once")
        self.assertEqual(loaded.claim_id, claim_id)
        self.assertEqual(loaded.claimed_at, self.broker.get(request.id).claimed_at)
        with self.assertRaises(Conflict):
            reloaded.claim(request.id)

    def test_old_persisted_request_defaults_to_unclaimed(self):
        request = ApprovalRequest(
            id="apr-old", principal="agent-coder-1", capability="fs.read",
            scope={}, risk="low", status="approved", mode="once",
            created_at=1)
        old = request.to_dict()
        old.pop("claim_id", None)
        old.pop("claimed_at", None)
        loaded = ApprovalRequest.from_dict(old)
        self.assertIsNone(loaded.claim_id)
        self.assertIsNone(loaded.claimed_at)


class _FakeSys:
    """Minimal SysAPI for the executor claim path."""

    def __init__(self, approval: dict, claim_result: dict) -> None:
        self.approval = approval
        self.claim_result = claim_result
        self.calls: list[tuple[str, dict]] = []
        self.events: list[dict] = []

    async def call(self, action: str, data: dict | None = None, *,
                   timeout: float | None = None) -> dict:
        payload = dict(data or {})
        self.calls.append((action, payload))
        if action in ("approval.request", "approval.status"):
            return dict(self.approval)
        if action == "approval.claim":
            return dict(self.claim_result)
        if action == "event.append":
            self.events.append(payload)
            return {"seq": len(self.events)}
        return {}

    def actions(self, name: str) -> list[dict]:
        return [data for action, data in self.calls if action == name]

    def event_types(self) -> list[str]:
        return [str(event.get("type")) for event in self.events]


class ToolExecutorClaimTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = _tmpdir("m10-tool-")
        self.ws = Path(self._tmp.name) / "workspace"
        self.ws.mkdir()
        self.agent = Agent(id="agent-coder-1", role="coder",
                           project_id="p-1", task_id="t-1")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _executor(self, sys) -> ToolExecutor:
        return ToolExecutor(sys, agent=self.agent, workspace=self.ws,
                            security_config=SecurityCfg())

    async def test_once_approval_is_claimed_before_execution(self):
        sys = _FakeSys(
            {"id": "apr-1", "status": "approved", "mode": "once",
             "risk": "medium", "grant_id": "grant-1"},
            {"claim_id": "clm-1"})
        result = await self._executor(sys).run("fs.list", {"path": "."})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(sys.actions("approval.claim"),
                         [{"request_id": "apr-1"}])
        self.assertEqual(sys.actions("approval.use"), [])
        self.assertIn("tool.result", sys.event_types())

    async def test_failed_claim_never_reaches_the_handler(self):
        sys = _FakeSys(
            {"id": "apr-1", "status": "approved", "mode": "once",
             "risk": "medium", "grant_id": "grant-1"},
            {"ok": False})
        result = await self._executor(sys).run("fs.list", {"path": "."})
        self.assertFalse(result.ok)
        self.assertEqual(result.error,
                         "DENIED: one-shot approval already claimed")
        self.assertIn("tool.denied", sys.event_types())
        self.assertNotIn("tool.result", sys.event_types())
        self.assertEqual(sys.actions("approval.use"), [])

    async def test_claim_error_is_fail_closed(self):
        sys = _FakeSys(
            {"id": "apr-1", "status": "approved", "mode": "once",
             "risk": "medium", "grant_id": "grant-1"},
            {"claim_id": "clm-1"})

        class _Boom(_FakeSys):
            async def call(self, action, data=None, *, timeout=None):
                if action == "approval.claim":
                    self.calls.append((action, dict(data or {})))
                    raise RuntimeError("sys down")
                return await super().call(action, data, timeout=timeout)

        boom = _Boom(sys.approval, sys.claim_result)
        result = await self._executor(boom).run("fs.list", {"path": "."})
        self.assertFalse(result.ok)
        self.assertIn("already claimed", result.error or "")
        self.assertNotIn("tool.result", boom.event_types())


class _RpcClaimTests(unittest.IsolatedAsyncioTestCase):
    """``approval.claim`` over the authenticated dispatcher (H3 owner rules)."""

    async def asyncSetUp(self):
        self._tmp = _tmpdir("m10-rpc-")
        self.tmp = self._tmp.name
        self.app = App(Config.load(root=Path(self.tmp), state_dir=Path(self.tmp)))
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    def _unit_auth(self, work_id: str, agent_id: str) -> dict:
        token = self.app.rpc_auth.issue_unit(
            work_id, None, "coder", agent_id)
        return {"kind": "unit", "id": work_id, "token": token}

    def _operator_auth(self) -> dict:
        return {"kind": "operator", "id": "human:operator",
                "token": self.app.issue_operator_token()}

    async def _call(self, auth: dict, action: str, data: dict | None = None):
        principal = self.app.rpc_auth.verify(auth)
        return await self.app.handle_rpc_action(
            action, dict(data or {}), src=auth["id"], external=True,
            principal=principal)

    def _approved_once(self, principal: str) -> dict:
        request = self.app.approvals.request(
            principal, "shell.exec",
            {"cwd": self.tmp, "command": "make test"}, project_id=None)
        self.assertEqual(request.status, "pending")
        decided = self.app.approvals.decide(
            request.id, decision="allow", mode="once")
        self.assertEqual(decided.status, "approved")
        return decided.to_dict()

    async def test_owner_unit_can_claim_operator_can_claim(self):
        request = self._approved_once("agent-coder-1")
        auth = self._unit_auth("unit-owner", "agent-coder-1")
        reply = await self._call(auth, "approval.claim",
                                 {"request_id": request["id"]})
        self.assertTrue(str(reply["claim_id"]).startswith("clm"))

        other = self._approved_once("agent-coder-9")
        operator = self._operator_auth()
        reply = await self._call(operator, "approval.claim",
                                 {"request_id": other["id"]})
        self.assertTrue(reply["claim_id"])

    async def test_other_unit_cannot_claim_someone_elses_request(self):
        request = self._approved_once("agent-coder-1")
        thief = self._unit_auth("unit-thief", "agent-coder-2")
        with self.assertRaises(Denied) as caught:
            await self._call(thief, "approval.claim",
                             {"request_id": request["id"]})
        self.assertEqual(caught.exception.http_status, 403)
        self.assertIsNone(self.app.approvals.get(request["id"]).claim_id)
        self.assertIsNotNone(
            self.app.policy.get_rule(request["grant_id"]))


if __name__ == "__main__":
    unittest.main()

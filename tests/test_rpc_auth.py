"""Protocol v2 RPC identity/authorization tests (H3).

Covers the properties the security plan §5.2 requires from the identity layer
only: authenticated envelopes, hub-derived principals, operator-only actions,
and own-unit/own-lease/own-approval binding.  Resource-range checks for
task/agent/event (M5/M6/X2) are deliberately out of scope here.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from stackflowos.app import App
from stackflowos.config import Config, default_root
from stackflowos.errors import Denied, Unauthorized
from stackflowos.protocol import PROTOCOL_VERSION, Envelope
from stackflowos.transport import RpcClient

REPO = default_root()


def _tmpdir() -> str:
    base = REPO / ".stackflowos" / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix="ra-", dir=str(base))


class RpcAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = _tmpdir()
        self.app = App(Config.load(root=Path(self.tmp), state_dir=Path(self.tmp)))
        await self.app.start()
        self._clients: list[RpcClient] = []

    async def asyncTearDown(self):
        for client in self._clients:
            await client.stop()
        await self.app.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    async def _connect(self, auth, *, src: str = "probe") -> RpcClient:
        client = RpcClient(self.app.sys_addresses.rpc, src=src, dst="sys",
                           auth=auth, timeout=5.0, contexts=2)
        await client.start()
        self._clients.append(client)
        return client

    def _unit_auth(self, work_id: str = "unit-a", *,
                   project_id: str | None = None, role: str = "coder",
                   agent_id: str | None = "agent-coder-1") -> dict:
        token = self.app.rpc_auth.issue_unit(work_id, project_id, role, agent_id)
        return {"kind": "unit", "id": work_id, "token": token}

    def _operator_auth(self) -> dict:
        return {"kind": "operator", "id": "human:operator",
                "token": self.app.issue_operator_token()}

    # -- authentication --------------------------------------------------
    async def test_missing_or_invalid_token_is_unauthorized(self):
        anonymous = await self._connect(None)
        with self.assertRaises(Unauthorized) as caught:
            await anonymous.call("sys.ping")
        self.assertEqual(caught.exception.code, "UNAUTHORIZED")

        wrong = await self._connect(
            {"kind": "unit", "id": "unit-a", "token": "not-a-token"})
        with self.assertRaises(Unauthorized):
            await wrong.call("sys.ping")

        unknown_operator = await self._connect(
            {"kind": "operator", "id": "human:operator", "token": "nope"})
        with self.assertRaises(Unauthorized):
            await unknown_operator.call("sys.ping")

        # ``sys`` is an in-process-only kind; the wire can never claim it.
        with self.assertRaises(Unauthorized):
            self.app.rpc_auth.verify(
                {"kind": "sys", "id": "sys.0", "token": "whatever"})

    async def test_v1_envelope_and_unauthenticated_v2_are_rejected(self):
        self.assertEqual(PROTOCOL_VERSION, 2)
        client = await self._connect(None)

        legacy = Envelope.rpc("sys.ping", {}, src="unit-a", dst="sys")
        legacy.v = 1
        reply = await client._exchange(legacy, 5.0)
        self.assertFalse(reply.ok)
        self.assertEqual(reply.error["code"], "UNAUTHORIZED")

        no_auth = Envelope.rpc("sys.ping", {}, src="unit-a", dst="sys")
        reply = await client._exchange(no_auth, 5.0)
        self.assertFalse(reply.ok)
        self.assertEqual(reply.error["code"], "UNAUTHORIZED")

        # A valid token does not rescue an envelope that claims v1.
        auth = self._unit_auth()
        stale = Envelope.rpc("sys.ping", {}, src="unit-a", dst="sys", auth=auth)
        stale.v = 1
        reply = await client._exchange(stale, 5.0)
        self.assertFalse(reply.ok)
        self.assertEqual(reply.error["code"], "UNAUTHORIZED")

        good = await self._connect(auth, src="unit-a")
        self.assertTrue((await good.call("sys.ping"))["ok"])

    async def test_unknown_or_revoked_unit_token_is_unauthorized(self):
        auth = self._unit_auth("unit-a")
        client = await self._connect(auth, src="unit-a")
        self.assertTrue((await client.call("sys.ping"))["ok"])
        self.app.rpc_auth.revoke("unit-a")
        with self.assertRaises(Unauthorized):
            await client.call("sys.ping")

    # -- action allowlist ------------------------------------------------
    async def test_unit_cannot_call_privileged_actions(self):
        unit = await self._connect(self._unit_auth("unit-a"), src="unit-a")
        calls = [
            ("rule.create", {"effect": "allow", "capability": "shell.exec"}),
            ("rule.update", {"rule_id": "rule-x"}),
            ("rule.delete", {"rule_id": "rule-x"}),
            ("project.delete", {"project_id": "p-1"}),
            ("project.archive", {"project_id": "p-1"}),
            ("unit.kill", {"work_id": "unit-b"}),
            ("unit.allocate", {"kind": "coder"}),
            ("sys.config.set", {"patch": {}}),
            ("approval.decide", {"request_id": "apr-x", "decision": "allow"}),
        ]
        for action, data in calls:
            with self.subTest(action=action):
                with self.assertRaises(Denied):
                    await unit.call(action, data)

    async def test_new_unit_cannot_issue_its_own_token(self):
        unit = await self._connect(self._unit_auth("unit-a"), src="unit-a")
        with self.assertRaises(Denied):
            await unit.call("unit.allocate", {"kind": "coder"})

    # -- src is display-only ---------------------------------------------
    async def test_src_is_not_used_for_authorization(self):
        # The token belongs to unit-a/agent-coder-1 but ``src`` claims unit-b.
        client = await self._connect(
            self._unit_auth("unit-a", agent_id="agent-coder-1"), src="unit-b")
        with self.assertRaises(Denied):
            await client.call("approval.request", {
                "principal": "agent-coder-2", "capability": "fs.read",
                "scope": {"path": "/etc/hosts"}})
        # A plain request is bound to the connection principal, not to src.
        request = await client.call("approval.request", {
            "capability": "fs.read", "scope": {"path": "/etc/hosts"}})
        self.assertEqual(request["principal"], "agent-coder-1")

    # -- own unit / own lease --------------------------------------------
    async def test_unit_release_binds_to_the_connection_principal(self):
        client = await self._connect(self._unit_auth("unit-a"), src="unit-a")
        with self.assertRaises(Denied):
            await client.call("unit.release", {"work_id": "unit-b"})
        with self.assertRaises(Denied):
            await client.call("unit.register", {"work_id": "unit-b"})
        with self.assertRaises(Denied):
            await client.call("unit.heartbeat", {"work_id": "unit-b"})
        with self.assertRaises(Denied):
            await client.call("unit.addresses", {"work_id": "unit-b"})

    async def test_lease_holder_is_forced_to_the_principal(self):
        a = await self._connect(self._unit_auth("unit-a"), src="unit-a")
        b = await self._connect(
            self._unit_auth("unit-b", agent_id="agent-coder-2"), src="unit-b")

        with self.assertRaises(Denied):
            await a.call("lease.acquire", {"resource": "cap:test",
                                           "holder": "unit-b", "ttl_s": 30})
        lease = await a.call("lease.acquire", {"resource": "cap:test",
                                               "ttl_s": 30})
        self.assertEqual(lease["holder"], "unit-a")

        with self.assertRaises(Denied):
            await b.call("lease.list", {"holder": "unit-a"})
        with self.assertRaises(Denied):
            await b.call("lease.release", {"lease_id": lease["id"]})
        with self.assertRaises(Denied):
            await b.call("lease.renew", {"lease_id": lease["id"], "ttl_s": 30})

        self.assertEqual((await b.call("lease.list", {}))["leases"], [])
        seen = await a.call("lease.list", {})
        self.assertEqual([item["holder"] for item in seen["leases"]], ["unit-a"])
        released = await a.call("lease.release", {"lease_id": lease["id"]})
        self.assertEqual(released["id"], lease["id"])

    async def test_unit_release_revokes_the_token(self):
        client = await self._connect(self._unit_auth("unit-z"), src="unit-z")
        self.assertTrue((await client.call("sys.ping"))["ok"])
        await client.call("unit.release", {})
        self.assertFalse(self.app.rpc_auth.has_unit("unit-z"))
        with self.assertRaises(Unauthorized):
            await client.call("sys.ping")

    # -- own approvals ---------------------------------------------------
    async def test_approval_status_and_use_are_owner_only(self):
        a = await self._connect(
            self._unit_auth("unit-a", agent_id="agent-coder-1"), src="unit-a")
        b = await self._connect(
            self._unit_auth("unit-b", agent_id="agent-coder-2"), src="unit-b")

        request = await a.call("approval.request", {
            "capability": "fs.read", "scope": {"path": "/etc/hosts"}})
        self.assertEqual(request["status"], "pending")

        with self.assertRaises(Denied):
            await b.call("approval.status", {"request_id": request["id"]})
        status = await a.call("approval.status", {"request_id": request["id"]})
        self.assertEqual(status["id"], request["id"])

        decided = await self.app.decide_approval(
            request["id"], decision="allow", mode="once", note="rpc-auth test")
        grant_id = decided["grant_id"]
        with self.assertRaises(Denied):
            await b.call("approval.use", {"grant_id": grant_id})
        self.assertTrue((await a.call("approval.use", {"grant_id": grant_id}))["ok"])

    # -- operator path ---------------------------------------------------
    async def test_operator_token_can_call_privileged_actions(self):
        auth = self._operator_auth()
        self.assertEqual(self.app.operator_token, auth["token"])
        self.assertTrue(self.app.rpc_auth.verify_operator(auth["token"]))
        self.assertFalse(self.app.rpc_auth.verify_operator(auth["token"] + "x"))
        operator = await self._connect(auth, src="cli")

        rule = await operator.call("rule.create", {
            "effect": "allow", "capability": "fs.read", "principal": "*",
            "scope": {}, "risk_max": "low"})
        self.assertTrue(rule["id"])
        self.assertTrue((await operator.call(
            "rule.delete", {"rule_id": rule["id"]}))["deleted"])

        request = await operator.call("approval.request", {
            "capability": "fs.read", "scope": {"path": "/etc/hosts"}})
        self.assertEqual(request["principal"], "human:operator")
        with self.assertRaises(Denied):
            await operator.call("approval.request", {
                "principal": "agent-x", "capability": "fs.read",
                "scope": {}})

    async def test_in_process_operator_path_is_unaffected(self):
        request = await self.app.handle_rpc_action("approval.request", {
            "principal": "coder.probe", "capability": "fs.write",
            "scope": {"path": "/etc/stackflowos-probe"},
            "reason": "in-process",
        }, src="coder.probe")
        self.assertEqual(request["status"], "pending")

        rule = await self.app.handle_rpc_action("rule.create", {
            "effect": "deny", "capability": "net.fetch", "principal": "*",
            "scope": {},
        }, src="operator")
        self.assertIn("id", rule)
        await self.app.handle_rpc_action(
            "rule.delete", {"rule_id": rule["id"]}, src="operator")

    # -- replay cache ----------------------------------------------------
    async def test_replay_cache_key_uses_the_principal_not_src(self):
        handler = self.app._make_env_handler("sys.ping",
                                             self.app._actions["sys.ping"])
        first = Envelope.rpc("sys.ping", {}, src="unit-b", dst="sys",
                             auth=self._unit_auth("unit-a"))
        second = Envelope.rpc("sys.ping", {}, src="unit-b", dst="sys",
                              auth=self._unit_auth("unit-b"))
        second.id = first.id  # identical envelope id from a different caller
        await handler(first)
        await handler(second)
        keys = sorted(key for key in self.app._rpc_cache if key.endswith(first.id))
        self.assertEqual(keys, [f"unit:unit-a|{first.id}",
                                f"unit:unit-b|{second.id}"])


if __name__ == "__main__":
    unittest.main()

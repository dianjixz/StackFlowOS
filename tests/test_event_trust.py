"""Event-channel trust tests (X2, security plan §5.2.7).

Two properties are covered end to end:

* the hub never turns an unauthenticated PUB frame on ``sys.0.events`` into an
  audit record (so any local process can still connect, but forging
  ``approval.decided`` has no effect), and
* ``event.append`` over RPC only accepts worker telemetry types with a bounded
  payload; hub-only audit/system types stay unreachable from units while the
  in-process operator path keeps working.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import pynng

from stackflowos.app import ALLOWED_RPC_EVENT_TYPES, App
from stackflowos.config import Config, default_root
from stackflowos.errors import BadRequest, Denied
from stackflowos.protocol import EventRecord

REPO = default_root()

#: Hub-only event types a unit must never be able to append.
FORBIDDEN_EVENT_TYPES = (
    "approval.decided",
    "approval.requested",
    "rule.created",
    "rule.updated",
    "project.deleted",
    "project.created",
    "operator.action",
    "sys.booted",
    "sys.shutdown",
    "agent.spawned",       # trusted supervisor-only telemetry
    "does.not.exist",      # unknown types are denied, not ignored
)


def _tmpdir() -> str:
    base = REPO / ".stackflowos" / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix="et-", dir=str(base))


class EventTrustTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = _tmpdir()
        self.app = App(Config.load(root=Path(self.tmp), state_dir=Path(self.tmp)))
        await self.app.start()
        self.project = self.app.projects.create("X2 events", "event trust")

    async def asyncTearDown(self):
        await self.app.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def _unit_auth(self, work_id: str = "unit-a1", *, project_id: str | None = None,
                   role: str = "coder", agent_id: str | None = "agent-coder-1") -> dict:
        token = self.app.rpc_auth.issue_unit(work_id, project_id, role, agent_id)
        return {"kind": "unit", "id": work_id, "token": token}

    def _operator_auth(self) -> dict:
        return {"kind": "operator", "id": "human:operator",
                "token": self.app.issue_operator_token()}

    async def _call(self, auth: dict, action: str, data: dict | None = None):
        """Dispatch exactly like the transport: derive the principal, external=True."""
        principal = self.app.rpc_auth.verify(auth)
        return await self.app.handle_rpc_action(
            action, dict(data or {}), src=auth["id"], external=True,
            principal=principal)

    def _records(self, seq: int = 0) -> list[EventRecord]:
        return self.app.eventlog.since(seq, limit=5000)

    async def _wait_record(self, event_type: str, *, since: int = 0,
                           timeout: float = 15.0) -> EventRecord | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            matches = [record for record in self._records(since)
                       if record.type == event_type]
            if matches:
                return matches[-1]
            await asyncio.sleep(0.05)
        return None

    # -- external PUB cannot write audit ---------------------------------
    async def test_external_pub_cannot_forge_an_audit_event(self):
        # Keep a probe callback so the test knows the forged frame really
        # arrived at the hub's long-lived subscriber; it must still never
        # reach the event log.
        received: list[EventRecord] = []
        self.app.transport.on_event(lambda record: received.append(record))
        forged = EventRecord(
            seq=0, type="approval.decided", source="operator",
            ts=int(time.time() * 1000),
            data={"request_id": "apr-forged", "decision": "allow",
                  "principal": "agent-forged"},
            project_id="p-forged",
        )
        last_seq = self.app.eventlog.last_seq
        publisher = pynng.Pub0(dial=self.app.sys_addresses.events,
                               block_on_dial=False)
        try:
            deadline = time.monotonic() + 2.0
            while not received and time.monotonic() < deadline:
                publisher.send(forged.to_envelope().to_bytes(), block=False)
                await asyncio.sleep(0.05)
        finally:
            publisher.close()
        self.assertTrue(received, "forged frame did not reach the hub subscriber")
        self.assertEqual(received[0].type, "approval.decided")
        # The hub must not have persisted it or advanced the audit sequence.
        self.assertEqual(self.app.eventlog.last_seq, last_seq)
        self.assertFalse([
            record for record in self._records(last_seq)
            if record.type == "approval.decided"
        ])
        # ...and the hub is still healthy afterwards.
        self.assertEqual(self.app.health()["status"], "ok")
        result = await self._call(
            self._unit_auth(project_id=self.project.id), "event.append",
            {"type": "agent.step", "data": {"step": 2}})
        self.assertGreater(result["seq"], last_seq)

    # -- event.append type allowlist -------------------------------------
    async def test_event_append_denies_audit_and_system_types(self):
        unit = self._unit_auth(project_id=self.project.id)
        for event_type in FORBIDDEN_EVENT_TYPES:
            with self.subTest(type=event_type):
                with self.assertRaises(Denied):
                    await self._call(unit, "event.append", {
                        "type": event_type,
                        "data": {"forged": True},
                        "source": "sys",
                        "project_id": "p-forged",
                    })
        # Missing/malformed type is a 400, not silently accepted.
        for payload in ({}, {"type": ""}, {"type": 42}):
            with self.subTest(payload=payload):
                with self.assertRaises(BadRequest):
                    await self._call(unit, "event.append", payload)

    async def test_event_append_allows_worker_telemetry_and_binds_provenance(self):
        unit = self._unit_auth(project_id=self.project.id,
                               agent_id="agent-coder-1")
        seq = self.app.eventlog.last_seq
        for event_type in ("agent.step", "tool.result", "unit.paused"):
            result = await self._call(unit, "event.append", {
                "type": event_type,
                # Both are ignored/overridden by the hub.
                "source": "sys",
                "project_id": "p-forged",
                "data": {"ok": True},
            })
            self.assertGreater(result["seq"], 0)
        records = self._records(seq)
        self.assertEqual([record.type for record in records],
                         ["agent.step", "tool.result", "unit.paused"])
        for record in records:
            self.assertEqual(record.source, "agent:agent-coder-1")
            self.assertEqual(record.project_id, self.project.id)

        # A unit without a bound agent is sourced as the unit itself.
        anonymous = self._unit_auth("unit-anon", project_id=self.project.id,
                                    agent_id=None)
        seq = self.app.eventlog.last_seq
        await self._call(anonymous, "event.append", {
            "type": "agent.step", "source": "operator", "data": {}})
        record = self._records(seq)[0]
        self.assertEqual(record.source, "unit:unit-anon")
        self.assertEqual(record.project_id, self.project.id)

        # The allowlist is exactly the worker telemetry set from the plan.
        self.assertEqual(
            ALLOWED_RPC_EVENT_TYPES,
            frozenset({
                "agent.step", "agent.working", "agent.waiting", "agent.failed",
                "agent.finished", "agent.terminated", "agent.registered",
                "task.review", "task.blocked",
                "tool.invoked", "tool.result", "tool.failed", "tool.denied",
                "unit.paused",
            }))

    async def test_event_append_rejects_oversized_data(self):
        unit = self._unit_auth(project_id=self.project.id)
        with self.assertRaises(BadRequest):
            await self._call(unit, "event.append", {
                "type": "tool.result",
                "data": {"blob": "x" * 70000},
            })
        # A payload under the 64 KiB cap (including JSON overhead) is fine.
        result = await self._call(unit, "event.append", {
            "type": "tool.result",
            "data": {"blob": "x" * 1024},
        })
        self.assertGreater(result["seq"], 0)

    # -- trusted paths keep working --------------------------------------
    async def test_in_process_and_operator_paths_are_unaffected(self):
        # Web/chat/operator audit code appends in-process (external=False);
        # it must still be able to write hub-only types such as operator.*.
        seq = self.app.eventlog.last_seq
        result = await self.app.handle_rpc_action("event.append", {
            "type": "operator.action",
            "data": {"action": "in-process"},
            "source": "operator",
        }, src="operator")
        self.assertGreater(result["seq"], seq)
        record = self._records(seq)[-1]
        self.assertEqual(record.type, "operator.action")

        # An authenticated operator may still emit the allowed telemetry
        # types over RPC (identity stays operator-owned, not unit-forged).
        operator = self._operator_auth()
        result = await self._call(operator, "event.append", {
            "type": "agent.step", "source": "operator",
            "project_id": self.project.id, "data": {"step": 1}})
        self.assertGreater(result["seq"], 0)

    # -- real worker still reports through event.append -------------------
    async def test_real_worker_emits_agent_registered_and_unit_paused(self):
        project = await self.app.create_project("X2 worker", "做一个小工具",
                                                spawn_lead=False)
        task = self.app.projects.task_list(project_id=project["id"])[0]
        seq = self.app.eventlog.last_seq
        agent = await self.app.spawn_agent("coder", project["id"], task.id)
        try:
            registered = await self._wait_record("agent.registered", since=seq)
            self.assertIsNotNone(
                registered, "real worker did not append agent.registered")
            self.assertEqual(registered.project_id, project["id"])
            self.assertEqual(registered.source, f"agent:{agent['id']}")

            # The mock coder blocks on an fs.write approval, so the unit is
            # still alive when the hub asks it to pause; ``unit.paused`` now
            # travels back through authenticated event.append.
            paused = await self.app._control_unit(
                agent["unit_id"], "unit.pause", {"reason": "x2-test"})
            self.assertTrue(paused["paused"])
            paused_record = await self._wait_record("unit.paused", since=seq,
                                                    timeout=5.0)
            self.assertIsNotNone(
                paused_record, "real worker did not append unit.paused")
            self.assertEqual(paused_record.project_id, project["id"])
            self.assertIn(paused_record.source,
                          (f"agent:{agent['id']}", f"unit:{agent['unit_id']}"))
        finally:
            await self.app.kill_agent(agent["id"])


if __name__ == "__main__":
    unittest.main()

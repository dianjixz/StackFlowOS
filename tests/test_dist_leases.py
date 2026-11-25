"""DIST: lease fencing tokens, clock-skew tolerance and resource scoping."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stackflowos.config import default_root
from stackflowos.errors import BadRequest, Conflict, Denied, NotFound
from stackflowos.kernel import Context
from stackflowos.l2.eventlog import EventLog
from stackflowos.l2.leases import (
    LeaseManager,
    validate_resource_scope,
)
from stackflowos.protocol import now_ms

REPO = default_root()


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        base = REPO / ".stackflowos" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(base))
        self.dir = Path(self._tmp.name)
        self.ctx = Context(name="test")
        self.events = []
        self.ctx.on_any(lambda mode, name, payload: self.events.append((name, payload)))
        self.log = EventLog(self.dir / "events")
        self._env_backup = {key: os.environ.get(key) for key in
                            ("SFO_LEASE_FENCING", "SFO_LEASE_CLOCK_SKEW_S",
                             "SFO_LEASE_FENCING_PATH")}
        for key in self._env_backup:
            os.environ.pop(key, None)

    async def asyncTearDown(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        await self.ctx.dispose()
        await self.log.close()
        self._tmp.cleanup()


class FencingTests(_Base):
    def test_tokens_are_monotonic_per_resource(self):
        manager = LeaseManager(self.ctx, self.log)
        first = manager.acquire("project:p-1:lead", "lead.1", 60)
        self.assertEqual(first.token, 1)
        manager.release(first.id)
        second = manager.acquire("project:p-1:lead", "lead.2", 60)
        self.assertEqual(second.token, 2)
        # Another resource gets its own token sequence.
        other = manager.acquire("cap:fs.write", "lead.2", 60)
        self.assertEqual(other.token, 1)
        # Renewal keeps the token (same ownership incarnation).
        renewed = manager.renew(second.id, "lead.2", 120)
        self.assertEqual(renewed.token, 2)
        # Preemption mints a new token for the new owner.
        preemptible = manager.acquire("cap:gpu", "coder.1", 60,
                                      preemptible=True, priority=1)
        winner = manager.acquire("cap:gpu", "coder.2", 60, priority=9)
        self.assertGreater(winner.token, preemptible.token)

    def test_tokens_persist_across_restart(self):
        path = self.dir / "state" / "lease_fencing.json"
        manager = LeaseManager(self.ctx, self.log, fencing_path=path)
        lease = manager.acquire("slot:coder.1:0", "coder.1", 60)
        self.assertEqual(lease.token, 1)
        self.assertTrue(path.exists())
        payload = json.loads(path.read_text("utf-8"))
        self.assertEqual(payload["tokens"]["slot:coder.1:0"], 1)

        restarted = LeaseManager(self.ctx, self.log, fencing_path=path)
        fresh = restarted.acquire("slot:coder.1:0", "other.2", 60)
        self.assertEqual(fresh.token, 2, "restart must not reset the token")
        # A different state dir starts from zero.
        other = LeaseManager(self.ctx, self.log,
                             fencing_path=self.dir / "elsewhere.json")
        self.assertEqual(other.acquire("slot:coder.1:0", "x", 60).token, 1)

    def test_default_fencing_path_derives_from_eventlog_state_dir(self):
        manager = LeaseManager(self.ctx, self.log)
        self.assertEqual(manager.fencing_path,
                         self.dir / "state" / "lease_fencing.json")
        manager.acquire("cap:x", "coder.1", 60)
        self.assertTrue(manager.fencing_path.exists())

    def test_fencing_can_be_disabled(self):
        manager = LeaseManager(self.ctx, self.log, fencing=False)
        first = manager.acquire("cap:x", "coder.1", 60)
        second = manager.acquire("cap:y", "coder.2", 60)
        self.assertEqual((first.token, second.token), (0, 0))
        self.assertFalse(manager.health()["fencing"]["enabled"])

    def test_env_selects_fencing_and_skew(self):
        os.environ["SFO_LEASE_FENCING"] = "0"
        os.environ["SFO_LEASE_CLOCK_SKEW_S"] = "5"
        manager = LeaseManager(self.ctx, self.log)
        self.assertFalse(manager.fencing)
        self.assertEqual(manager.clock_skew_s, 5.0)

    def test_verify_and_require(self):
        manager = LeaseManager(self.ctx, self.log)
        lease = manager.acquire("cap:x", "coder.1", 60)
        self.assertTrue(manager.verify(lease.id, lease.token))
        self.assertTrue(manager.verify(lease.id, lease.token, holder="coder.1"))
        self.assertFalse(manager.verify(lease.id, lease.token, holder="coder.2"))
        self.assertFalse(manager.verify(lease.id, lease.token + 1))
        with self.assertRaises(Denied):
            manager.require(lease.id, lease.token + 1)
        manager.release(lease.id)
        self.assertFalse(manager.verify(lease.id, lease.token))
        with self.assertRaises(NotFound):
            manager.verify("lease-missing", 1)

    def test_renew_and_release_accept_fencing_token(self):
        manager = LeaseManager(self.ctx, self.log)
        lease = manager.acquire("cap:x", "coder.1", 60)
        renewed = manager.renew(lease.id, "coder.1", 30, token=lease.token)
        self.assertEqual(renewed.token, lease.token)
        with self.assertRaises(Denied):
            manager.renew(lease.id, "coder.1", 30, token=lease.token + 99)
        with self.assertRaises(Denied):
            manager.release(lease.id, token=lease.token + 99)
        released = manager.release(lease.id, token=lease.token)
        self.assertEqual(released.status, "released")

    def test_fencing_token_in_events(self):
        manager = LeaseManager(self.ctx, self.log)
        lease = manager.acquire("cap:x", "coder.1", 60)
        acquired = [payload for name, payload in self.events
                    if name == "lease.acquired"]
        self.assertEqual(acquired[0]["data"]["token"], lease.token)
        self.assertEqual(acquired[0]["data"]["lease"]["token"], lease.token)


class FencingPersistFailureTests(_Base):
    """Best-effort fencing persistence must warn and expose its failure count."""

    def test_persist_failure_warns_and_is_counted_in_health(self):
        path = self.dir / "state" / "lease_fencing.json"
        manager = LeaseManager(self.ctx, self.log, fencing_path=path)
        with mock.patch("stackflowos.l2.leases.write_json",
                        side_effect=OSError("read-only filesystem")):
            with self.assertLogs(self.ctx.log, level="WARNING") as captured:
                lease = manager.acquire("slot:coder.1:0", "coder.1", 60)
        self.assertEqual(lease.token, 1)
        self.assertIn("could not persist lease fencing tokens",
                      "\n".join(captured.output))
        fencing = manager.health()["fencing"]
        self.assertFalse(fencing["persisted"])
        self.assertEqual(fencing["persist_failures"], 1)
        self.assertFalse(path.exists(),
                         "a failed write must not leave partial state behind")

        # One failed write must not stop the in-memory counter from advancing.
        manager.release(lease.id)
        with mock.patch("stackflowos.l2.leases.write_json",
                        side_effect=OSError("read-only filesystem")):
            second = manager.acquire("slot:coder.1:0", "coder.2", 60)
        self.assertEqual(second.token, 2)
        self.assertEqual(manager.health()["fencing"]["persist_failures"], 2)

        # Documented consequence: no durable high-water mark, so a restarted
        # hub may re-issue an already used token.
        restarted = LeaseManager(self.ctx, self.log, fencing_path=path)
        self.assertEqual(
            restarted.acquire("slot:coder.1:0", "coder.3", 60).token, 1)

    def test_persist_failure_recovers_and_counter_stays_cumulative(self):
        path = self.dir / "state" / "lease_fencing.json"
        manager = LeaseManager(self.ctx, self.log, fencing_path=path)
        with mock.patch("stackflowos.l2.leases.write_json",
                        side_effect=OSError("disk full")):
            manager.acquire("cap:fs.write", "coder.1", 60)
        self.assertFalse(manager.health()["fencing"]["persisted"])

        manager.acquire("cap:fs.read", "coder.2", 60)  # disk writable again
        fencing = manager.health()["fencing"]
        self.assertTrue(fencing["persisted"])
        self.assertEqual(fencing["persist_failures"], 1,
                         "the failure counter is cumulative, not a gauge")
        payload = json.loads(path.read_text("utf-8"))
        self.assertEqual(payload["tokens"]["cap:fs.write"], 1)
        self.assertEqual(payload["tokens"]["cap:fs.read"], 1)


class ClockSkewTests(_Base):
    def test_sweep_does_not_expire_within_grace(self):
        manager = LeaseManager(self.ctx, self.log, clock_skew_s=5.0)
        lease = manager.acquire("cap:x", "coder.1", 1)
        # Exactly at (and just before) the grace deadline: still active.
        self.assertEqual(manager.sweep(lease.expires_at), [])
        self.assertEqual(manager.sweep(lease.expires_at + 4999), [])
        self.assertTrue(manager.verify(lease.id, lease.token))
        # Past expires_at + skew: expired.
        expired = manager.sweep(lease.expires_at + 5001)
        self.assertEqual([row.id for row in expired], [lease.id])
        self.assertFalse(manager.verify(lease.id, lease.token))

    def test_acquire_after_skewed_expiry(self):
        manager = LeaseManager(self.ctx, self.log, clock_skew_s=2.0)
        old = manager.acquire("cap:x", "coder.1", 1)
        manager.sweep(old.expires_at + 2001)
        self.assertEqual(old.status, "expired")
        fresh = manager.acquire("cap:x", "coder.2", 60)
        self.assertEqual(fresh.status, "active")
        self.assertGreater(fresh.token, old.token)

    def test_health(self):
        manager = LeaseManager(self.ctx, self.log, clock_skew_s=2.0)
        manager.acquire("cap:a", "coder.1", 60)
        health = manager.health()
        self.assertEqual(health["active"], 1)
        self.assertTrue(health["fencing"]["enabled"])
        self.assertEqual(health["fencing"]["last_token"], 1)
        self.assertEqual(health["clock_skew_s"], 2.0)


class ResourceScopeTests(unittest.TestCase):
    def test_matching_project_resource(self):
        self.assertEqual(
            validate_resource_scope("project:p-1:lead", "p-1", False),
            "project:p-1:lead")
        self.assertEqual(
            validate_resource_scope("project:p-1", "p-1", False),
            "project:p-1")

    def test_cross_project_is_denied(self):
        with self.assertRaises(Denied) as caught:
            validate_resource_scope("project:p-2:lead", "p-1", False)
        self.assertEqual(caught.exception.detail["project_id"], "p-1")
        with self.assertRaises(Denied):
            validate_resource_scope("project:p-1:lead", None, False)

    def test_operator_bypasses_scope(self):
        self.assertEqual(
            validate_resource_scope("project:p-2:lead", "p-1", True),
            "project:p-2:lead")
        # A Principal-like object exposes is_operator as a property.
        class Principal:
            is_operator = True

        self.assertEqual(
            validate_resource_scope("project:p-9:lead", None, Principal()),
            "project:p-9:lead")

    def test_other_resources_are_unchanged(self):
        for resource in ("unit:coder.1", "cap:fs.write", "file:/tmp/x",
                         "slot:coder.1:0", "custom-resource", ""):
            with self.subTest(resource=resource):
                self.assertEqual(
                    validate_resource_scope(resource, "p-1", False), resource)

    def test_malformed_project_resource(self):
        with self.assertRaises(BadRequest):
            validate_resource_scope("project:", "p-1", False)


if __name__ == "__main__":
    unittest.main()

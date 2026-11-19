import tempfile
import unittest
from pathlib import Path

from stackflowos.config import default_root
from stackflowos.errors import Conflict, Denied, NotFound
from stackflowos.kernel import Context
from stackflowos.l2.eventlog import EventLog
from stackflowos.l2.leases import Lease, LeaseManager
from stackflowos.protocol import now_ms

REPO = default_root()


class LeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=str(REPO / ".stackflowos" / "tmp"))
        self.dir = Path(self._tmp.name)
        self.ctx = Context(name="test")
        self.events = []
        self.ctx.on_any(lambda mode, name, payload: self.events.append((name, payload)))
        self.log = EventLog(self.dir / "events")
        self.leases = LeaseManager(self.ctx, self.log)

    async def asyncTearDown(self):
        await self.ctx.dispose()
        await self.log.close()
        self._tmp.cleanup()

    def _event_types(self):
        return [name for name, _ in self.events]

    def test_acquire_and_conflict(self):
        lease = self.leases.acquire("project:p-1:lead", "lead.1", 60)
        self.assertEqual(lease.status, "active")
        self.assertTrue(lease.active())
        self.assertGreater(lease.expires_at, lease.created_at)
        self.assertIn("lease.acquired", self._event_types())

        with self.assertRaises(Conflict) as caught:
            self.leases.acquire("project:p-1:lead", "lead.2", 60)
        self.assertIn("lead.1", str(caught.exception))
        self.assertEqual(caught.exception.detail["holder"], "lead.1")
        self.assertEqual(caught.exception.detail["lease_id"], lease.id)

        # A different resource is not blocked.
        other = self.leases.acquire("cap:fs.write", "lead.2", 60)
        self.assertNotEqual(other.id, lease.id)

    def test_renew(self):
        lease = self.leases.acquire("slot:coder.1:0", "coder.1", 10)
        old_expiry = lease.expires_at
        renewed = self.leases.renew(lease.id, "coder.1", 30)
        self.assertEqual(renewed.id, lease.id)
        self.assertEqual(renewed.ttl_s, 30)
        self.assertGreater(renewed.expires_at, old_expiry)
        self.assertIn("lease.renewed", self._event_types())

        with self.assertRaises(Denied):
            self.leases.renew(lease.id, "coder.2")
        with self.assertRaises(NotFound):
            self.leases.renew("lease-missing", "coder.1")

        self.leases.release(lease.id)
        with self.assertRaises(Conflict):
            self.leases.renew(lease.id, "coder.1")

    def test_non_renewable_lease_is_denied(self):
        lease = self.leases.acquire("cap:x", "coder.1", 10, renewable=False)
        with self.assertRaises(Denied):
            self.leases.renew(lease.id, "coder.1")

    def test_release_and_holder_mismatch(self):
        lease = self.leases.acquire("cap:x", "coder.1", 60)
        with self.assertRaises(Denied):
            self.leases.release(lease.id, holder="coder.2")
        released = self.leases.release(lease.id, reason="done")
        self.assertEqual(released.status, "released")
        self.assertEqual(self.leases.list(active_only=True), [])
        # Releasing again is idempotent, not an error.
        self.assertEqual(self.leases.release(lease.id).status, "released")
        with self.assertRaises(NotFound):
            self.leases.release("lease-missing")
        payload = [p for name, p in self.events if name == "lease.released"][0]
        self.assertEqual(payload["data"]["reason"], "done")

    def test_expiry_by_sweep(self):
        lease = self.leases.acquire("cap:x", "coder.1", 1)
        self.assertEqual(self.leases.sweep(now_ms() + 2000), [lease])
        self.assertEqual(lease.status, "expired")
        self.assertFalse(lease.active())
        self.assertEqual(self.leases.list(active_only=True), [])
        self.assertEqual(self.leases.sweep(now_ms() + 5000), [])
        self.assertIn("lease.expired", self._event_types())

    def test_acquire_after_expiry(self):
        old = self.leases.acquire("cap:x", "coder.1", 1)
        self.leases.sweep(now_ms() + 2000)
        self.assertEqual(old.status, "expired")
        fresh = self.leases.acquire("cap:x", "coder.2", 60)
        self.assertEqual(fresh.status, "active")

    def test_preemption_by_priority(self):
        low = self.leases.acquire("cap:gpu", "coder.1", 60,
                                  preemptible=True, priority=1)
        high = self.leases.acquire("cap:gpu", "coder.2", 60, priority=5)
        self.assertEqual(low.status, "preempted")
        self.assertEqual(high.status, "active")
        self.assertEqual(high.holder, "coder.2")
        self.assertIn("lease.preempted", self._event_types())
        self.assertEqual(self.leases.list(active_only=True), [high])

    def test_non_preemptible_lease_is_not_stolen(self):
        self.leases.acquire("cap:gpu", "coder.1", 60,
                            preemptible=False, priority=1)
        with self.assertRaises(Conflict):
            self.leases.acquire("cap:gpu", "coder.2", 60, priority=99)

    def test_release_holder(self):
        first = self.leases.acquire("cap:a", "coder.1", 60)
        second = self.leases.acquire("cap:b", "coder.1", 60)
        third = self.leases.acquire("cap:c", "coder.2", 60)
        released = self.leases.release_holder("coder.1", reason="unit_offline")
        self.assertEqual({lease.id for lease in released}, {first.id, second.id})
        self.assertEqual(first.status, "released")
        self.assertEqual(second.status, "released")
        self.assertEqual(self.leases.get(third.id).status, "active")
        self.assertEqual(self.leases.release_holder("nobody"), [])

    def test_ttl_clamped_to_max(self):
        manager = LeaseManager(self.ctx, self.log, max_ttl_s=10)
        lease = manager.acquire("cap:x", "coder.1", 9999)
        self.assertEqual(lease.ttl_s, 10)
        self.assertAlmostEqual(lease.expires_at - lease.created_at, 10_000, delta=50)

    def test_list_filters_and_holders(self):
        a = self.leases.acquire("cap:a", "coder.1", 60)
        b = self.leases.acquire("cap:b", "coder.2", 60)
        self.assertEqual(self.leases.list(holder="coder.1"), [a])
        self.assertEqual(self.leases.list(resource="cap:b"), [b])
        self.assertEqual(len(self.leases.list()), 2)
        self.assertEqual(self.leases.holders(),
                         {"coder.1": ["cap:a"], "coder.2": ["cap:b"]})
        self.leases.release(a.id)
        self.assertEqual(self.leases.list(holder="coder.1"), [])
        self.assertEqual(len(self.leases.list(holder="coder.1",
                                              active_only=False)), 1)

    def test_lease_roundtrip(self):
        lease = Lease(id="lease-1", resource="cap:x", holder="coder.1", ttl_s=5,
                      created_at=1, expires_at=9999999999999)
        self.assertEqual(Lease.from_dict(lease.to_dict()), lease)
        self.assertTrue(lease.active(now_ms=5))
        self.assertFalse(lease.active(now_ms=9999999999999))


if __name__ == "__main__":
    unittest.main()

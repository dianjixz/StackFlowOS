"""Regression tests: ``project_card`` attributes leases by *exact* project id.

Plan §6.2 (L2): the old projection used ``project_id in json.dumps(resource)``,
so ``project_card("p-1")`` also listed ``project:p-10:lead``.  These tests pin
the exact match, including metadata-only ownership and unrelated resources.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from stackflowos.app import App, _lease_project_id
from stackflowos.config import Config, default_root

REPO = default_root()


def _fake_lease(resource, metadata=None):
    return SimpleNamespace(resource=resource, metadata=metadata or {})


class LeaseProjectIdTests(unittest.TestCase):
    """The helper the projection relies on, tested without an app."""

    def test_project_resource_uses_exact_id(self):
        self.assertEqual(_lease_project_id(_fake_lease("project:p-1:lead")), "p-1")
        self.assertEqual(_lease_project_id(_fake_lease("project:p-10:lead")), "p-10")
        self.assertEqual(_lease_project_id(_fake_lease("project:p-1")), "p-1")
        self.assertEqual(_lease_project_id(_fake_lease("project:p-1:worker:0")), "p-1")
        # The old substring bug in one assertion.
        self.assertNotEqual(_lease_project_id(_fake_lease("project:p-10:lead")), "p-1")

    def test_project_resource_wins_over_metadata(self):
        lease = _fake_lease("project:p-1:lead", {"project_id": "p-10"})
        self.assertEqual(_lease_project_id(lease), "p-1")

    def test_metadata_fallback(self):
        self.assertEqual(
            _lease_project_id(_fake_lease("unit:x", {"project_id": "p-1"})), "p-1")
        self.assertIsNone(_lease_project_id(_fake_lease("unit:x")))
        self.assertIsNone(_lease_project_id(_fake_lease("unit:x", {"project_id": None})))
        self.assertIsNone(_lease_project_id(_fake_lease("unit:x", {"project_id": 1})))

    def test_unrelated_or_malformed_resources_are_unowned(self):
        for resource in ("unit:p-1", "cap:fs.write", "projectx:p-1:lead", "p-1", ""):
            with self.subTest(resource=resource):
                self.assertIsNone(_lease_project_id(_fake_lease(resource)))
        self.assertIsNone(_lease_project_id(SimpleNamespace()))
        self.assertIsNone(_lease_project_id(
            SimpleNamespace(resource=None, metadata=None)))
        self.assertIsNone(_lease_project_id(_fake_lease("project:")))


class ProjectCardLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        base = REPO / ".stackflowos" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(base))
        self.tmp = Path(self._tmp.name)
        self.app = App(Config.load(root=self.tmp, state_dir=self.tmp))
        await self.app.start()
        # A fresh state dir hands out p-1, p-2, ... in creation order.  Create
        # ten so the interesting pair p-1 / p-10 really exists.
        for index in range(1, 11):
            project = self.app.projects.create(f"L2 project {index}")
            self.assertEqual(project.id, f"p-{index}")

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    def _card_resources(self, project_id: str) -> list[str]:
        card = self.app.project_card(project_id)
        return sorted(lease["resource"] for lease in card["leases"])

    def test_prefix_project_ids_do_not_cross_match(self):
        self.app.leases.acquire("project:p-1:lead", "lead.p1", 60)
        self.app.leases.acquire("project:p-10:lead", "lead.p10", 60)
        self.assertEqual(self._card_resources("p-1"), ["project:p-1:lead"])
        self.assertEqual(self._card_resources("p-10"), ["project:p-10:lead"])

    def test_unrelated_leases_are_not_attributed(self):
        self.app.leases.acquire("project:p-1:lead", "lead.p1", 60)
        self.app.leases.acquire("project:p-10:lead", "lead.p10", 60)
        self.app.leases.acquire("unit:unit-1", "unit-1", 60)
        self.app.leases.acquire("cap:fs.write", "unit-2", 60)
        self.assertEqual(self._card_resources("p-1"), ["project:p-1:lead"])
        self.assertEqual(self._card_resources("p-10"), ["project:p-10:lead"])

    def test_metadata_only_lease_is_attributed(self):
        self.app.leases.acquire(
            "unit:unit-1", "unit-1", 60, metadata={"project_id": "p-1"})
        self.assertEqual(self._card_resources("p-1"), ["unit:unit-1"])
        self.assertEqual(self._card_resources("p-10"), [])

    def test_released_leases_are_hidden(self):
        lease = self.app.leases.acquire("project:p-1:lead", "lead.p1", 60)
        self.app.leases.release(lease.id, holder="lead.p1")
        self.assertEqual(self._card_resources("p-1"), [])

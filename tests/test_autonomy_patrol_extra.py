"""Extra patrol checks: review hand-off and blocked tasks (Lead additions)."""

from __future__ import annotations

import unittest

from stackflowos.autonomy.models import SEVERITY_ORDER
from stackflowos.autonomy.patrol import Snapshot, run_checks
from stackflowos.config import AutonomyCfg


def _snapshot(*, tasks=None, agents=None, now: int = 1_000_000) -> Snapshot:
    return Snapshot(
        now=now,
        projects=[{"id": "p-1", "name": "Demo", "status": "active"}],
        tasks={"p-1": list(tasks or [])},
        agents=list(agents or []),
        health={"status": "ok"},
        task_timeout_s=1800.0,
    )


def _task(**overrides) -> dict:
    task = {
        "id": "t-1",
        "project_id": "p-1",
        "title": "实现",
        "status": "todo",
        "role": "coder",
        "deps": [],
        "created_at": 100_000,
        "updated_at": 100_000,
    }
    task.update(overrides)
    return task


class ExtraPatrolChecksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AutonomyCfg(task_idle_s=120.0)
        self.now = 1_000_000

    def _find(self, findings, check: str):
        return [item for item in findings if item.check == check]

    def test_review_task_waits_and_is_dispatchable(self) -> None:
        findings = run_checks(_snapshot(
            tasks=[_task(status="review", updated_at=self.now - 600_000)],
            now=self.now), self.config)
        review = self._find(findings, "task.review")
        self.assertEqual(len(review), 1)
        finding = review[0]
        self.assertEqual(finding.task_id, "t-1")
        self.assertEqual(finding.severity, "warn")
        self.assertIsNotNone(finding.dispatch)
        self.assertEqual(finding.dispatch.kind, "spawn")
        self.assertEqual(finding.dispatch.role, "reviewer")
        self.assertEqual(finding.dispatch.task_id, "t-1")
        self.assertIn("review", finding.detail)

    def test_fresh_review_is_not_reported(self) -> None:
        findings = run_checks(_snapshot(
            tasks=[_task(status="review", updated_at=self.now - 1_000)],
            now=self.now), self.config)
        self.assertEqual(self._find(findings, "task.review"), [])

    def test_review_with_active_reviewer_is_not_reported(self) -> None:
        findings = run_checks(_snapshot(
            tasks=[_task(status="review", updated_at=self.now - 600_000)],
            agents=[{"id": "agent-reviewer-1", "role": "reviewer",
                     "status": "working", "task_id": "t-1"}],
            now=self.now), self.config)
        self.assertEqual(self._find(findings, "task.review"), [])

    def test_blocked_task_is_reported_without_dispatch(self) -> None:
        findings = run_checks(_snapshot(
            tasks=[_task(status="blocked", error="WORKSPACE_MISSING",
                         updated_at=self.now - 600_000)],
            now=self.now), self.config)
        blocked = self._find(findings, "task.blocked")
        self.assertEqual(len(blocked), 1)
        finding = blocked[0]
        self.assertEqual(finding.severity, "warn")
        self.assertIsNone(finding.dispatch)
        self.assertIn("WORKSPACE_MISSING", finding.detail)
        self.assertTrue(finding.fingerprint)

    def test_unknown_time_still_reports_blocked(self) -> None:
        findings = run_checks(_snapshot(
            tasks=[_task(status="blocked", created_at=0, updated_at=0)],
            now=self.now), self.config)
        self.assertEqual(len(self._find(findings, "task.blocked")), 1)

    def test_review_fingerprint_is_stable_across_messages(self) -> None:
        first = run_checks(_snapshot(
            tasks=[_task(status="review", updated_at=self.now - 600_000)],
            now=self.now), self.config)
        second = run_checks(_snapshot(
            tasks=[_task(status="review", updated_at=self.now - 900_000)],
            now=self.now + 300_000), self.config)
        self.assertEqual(self._find(first, "task.review")[0].fingerprint,
                         self._find(second, "task.review")[0].fingerprint)

    def test_severity_vocabulary_matches_models(self) -> None:
        findings = run_checks(_snapshot(
            tasks=[_task(status="review", updated_at=self.now - 600_000),
                   _task(id="t-2", status="blocked",
                         updated_at=self.now - 600_000)],
            now=self.now), self.config)
        self.assertTrue(findings)
        for finding in findings:
            self.assertIn(finding.severity, SEVERITY_ORDER)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

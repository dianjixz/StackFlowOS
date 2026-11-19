"""Scheduled aggregation must be change-driven, not a 4-minute loop."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stackflowos.agents.models import Agent
from stackflowos.agents.roles import ROLES
from stackflowos.app import App
from stackflowos.config import Config, default_root

REPO = default_root()


class AggregationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / ".stackflowos" / "tmp"))
        self.tmp = Path(self._tmp.name)
        self.app = App(Config.load(root=self.tmp, state_dir=self.tmp / "state"))
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.stop()
        self._tmp.cleanup()

    async def test_signature_changes_only_with_task_shape(self):
        card = await self.app.create_project("聚合测试", "signature",
                                             spawn_lead=False)
        task = self.app.projects.add_task(card["id"], "任务", "描述",
                                                role="coder")
        project = self.app.projects.get(card["id"])
        first = self.app.aggregation_signature(project, [task])
        self.assertEqual(first, self.app.aggregation_signature(project, [task]))
        task.status = "doing"
        task.updated_at += 1
        second = self.app.aggregation_signature(project, [task])
        self.assertNotEqual(first, second)

    async def test_prune_keeps_recent_aggregators_only(self):
        card = await self.app.create_project("聚合清理", "prune",
                                             spawn_lead=False)
        for index in range(12):
            agent = Agent(
                id=f"agent-aggregator-{index}", role="aggregator",
                project_id=card["id"], status="done",
                created_at=1_000 + index, last_seen=1_000 + index)
            self.app.supervisor._agents[agent.id] = agent
        removed = self.app.supervisor.prune_finished(keep_aggregators=5)
        self.assertEqual(removed, 7)
        remaining = [a for a in self.app.supervisor.list(project_id=card["id"])
                     if a.role == "aggregator"]
        self.assertEqual(len(remaining), 5)
        self.assertEqual({a.id for a in remaining},
                         {f"agent-aggregator-{i}" for i in range(7, 12)})

    def test_aggregator_cannot_write_workspace_files(self):
        self.assertNotIn("fs.write", ROLES["aggregator"].tools)


if __name__ == "__main__":
    unittest.main()

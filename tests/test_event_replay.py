"""Event replay filters: by project, agent, source, type and time window."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from stackflowos.errors import BadRequest
from stackflowos.l2.eventlog import EventLog

TMP_ROOT = Path(__file__).resolve().parents[1] / ".stackflowos" / "tmp"


class EventReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="replay-", dir=TMP_ROOT))
        self.log = EventLog(self.tmp / "events")

    async def asyncTearDown(self) -> None:
        await self.log.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self) -> None:
        self.log.append("tool.invoked", {"tool": "fs.read", "agent_id": "agent-coder-1"},
                        source="agent:agent-coder-1", project_id="p-1")
        self.log.append("tool.result", {"tool": "fs.read", "ok": True,
                                        "agent_id": "agent-coder-1"},
                        source="agent:agent-coder-1", project_id="p-1")
        self.log.append("agent.step", {"step": 1},
                        source="agent:agent-tester-1", project_id="p-2")
        self.log.append("sys.booted", {}, source="sys", project_id=None)
        self.log.append("agent.finished", {"agent_id": "agent-coder-1"},
                        source="sys", project_id="p-1")

    def test_agent_filter_matches_source_and_data(self) -> None:
        self._seed()
        records = self.log.query(agent_id="agent-coder-1")
        self.assertEqual([record.type for record in records],
                         ["tool.invoked", "tool.result", "agent.finished"])
        self.assertTrue(all(record.project_id == "p-1" for record in records))

    def test_source_filter_is_exact(self) -> None:
        self._seed()
        records = self.log.query(source="sys")
        self.assertEqual([record.type for record in records],
                         ["sys.booted", "agent.finished"])

    def test_type_prefix_and_project_filters_compose(self) -> None:
        self._seed()
        records = self.log.query(project_id="p-1", types=["tool"])
        self.assertEqual([record.type for record in records],
                         ["tool.invoked", "tool.result"])
        self.assertEqual(self.log.query(types=["agent"])[-1].type,
                         "agent.finished")

    def test_time_window_is_inclusive(self) -> None:
        from stackflowos.protocol import EventRecord

        for ts in (1_000, 2_000, 3_000):
            self.log.append_record(EventRecord(
                seq=0, type="agent.step", source="agent:agent-x", ts=ts,
                data={}, project_id="p-9"))
        all_records = self.log.query(project_id="p-9")
        self.assertEqual([record.ts for record in all_records],
                         [1_000, 2_000, 3_000])
        self.assertEqual(
            [record.ts for record in self.log.query(project_id="p-9",
                                                    since_ts=2_000)],
            [2_000, 3_000])
        self.assertEqual(
            [record.ts for record in self.log.query(project_id="p-9",
                                                    until_ts=2_000)],
            [1_000, 2_000])
        self.assertEqual(
            [record.ts for record in self.log.query(project_id="p-9",
                                                    since_ts=2_000,
                                                    until_ts=2_000)],
            [2_000])
        self.assertEqual(self.log.query(project_id="p-9", since_ts=3_001), [])
        self.assertEqual(self.log.query(project_id="p-9", until_ts=999), [])

    def test_invalid_time_filter_is_a_bad_request(self) -> None:
        with self.assertRaises(BadRequest):
            self.log.query(since_ts="not-a-number")
        with self.assertRaises(BadRequest):
            self.log.query(until_ts=-1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

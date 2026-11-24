"""Autonomy service: loop lifecycle, persistence and patrol plumbing."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stackflowos.app import App
from stackflowos.autonomy.models import DispatchHint, Finding
from stackflowos.autonomy.patrol import Snapshot
from stackflowos.config import Config
from stackflowos.errors import BadRequest

TMP_ROOT = Path(__file__).resolve().parents[1] / ".stackflowos" / "tmp"


def _finding(**overrides) -> Finding:
    data = {
        "check": "task.idle",
        "severity": "warn",
        "title": "任务闲置",
        "fingerprint": "fp-1",
        "project_id": "p-1",
        "task_id": "t-1",
        "fix": "派工",
        "dispatch": DispatchHint(kind="spawn", role="coder", task_id="t-1"),
        "detected_at": 1,
    }
    data.update(overrides)
    return Finding(**data)


class AutonomyServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(TMP_ROOT))
        self.root = Path(self._tmp.name).resolve()
        self.app = App(Config.load(root=self.root,
                                   state_dir=self.root / "state"))
        await self.app.start()
        self.service = self.app.autonomy

    async def asyncTearDown(self) -> None:
        await self.app.stop()
        self._tmp.cleanup()

    async def _patrol(self, findings: list[Finding], *, dry_run: bool = True):
        snapshot = Snapshot(now=1234)
        with patch("stackflowos.autonomy.patrol.collect_snapshot",
                   return_value=snapshot) as collect, \
                patch("stackflowos.autonomy.patrol.run_checks",
                      return_value=findings) as checks:
            report = await self.service.run_patrol(trigger="test",
                                                   dry_run=dry_run)
        collect.assert_called()
        checks.assert_called()
        return report

    async def test_disabled_status_shape(self) -> None:
        status = self.service.status()
        self.assertFalse(status["enabled"])
        self.assertFalse(status["running"])
        self.assertIsNone(status["next_run_in_s"])
        self.assertEqual(status["findings"], [])
        self.assertIn("policy", status)
        self.assertIn("guardrail", status)
        self.assertEqual(status["counts"]["patrols"], 0)

    async def test_units_descriptor_is_registered(self) -> None:
        units = self.service.units()
        self.assertEqual(len(units), 1)
        unit = units[0]
        self.assertEqual(unit["unit_id"], "autonomy.0")
        self.assertEqual(unit["kind"], "autonomy")
        self.assertIn(unit["state"], ("ready", "disabled"))
        ids = {card["unit_id"] for card in self.app.unit_cards()}
        self.assertIn("autonomy.0", ids)

    async def test_dry_run_patrol_reports_and_persists(self) -> None:
        report = await self._patrol([_finding()])
        self.assertEqual(report["findings"][0]["check"], "task.idle")
        self.assertEqual(report["summary"]["warn"], 1)
        self.assertTrue(report["headline"])
        self.assertEqual(report["dispatched"], [])
        self.assertTrue((self.root / "state" / "state" /
                         "autonomy.json").is_file())
        types = [record.type for record in self.app.eventlog.query(
            types=["autonomy"], limit=50)]
        self.assertIn("autonomy.patrol.started", types)
        self.assertIn("autonomy.patrol.completed", types)
        self.assertIn("autonomy.finding", types)
        status = self.service.status()
        self.assertEqual(status["counts"]["patrols"], 1)
        self.assertEqual(status["findings"][0]["fingerprint"], "fp-1")
        self.assertEqual(status["last_run"], report["finished_at"])

    async def test_dry_run_does_not_dispatch_even_when_enabled(self) -> None:
        await self.service.configure({
            "enabled": True, "on_start": False, "interval_s": 600,
            "dispatch_mode": "agents", "approve_mode": "off",
        })
        with patch("stackflowos.autonomy.dispatcher.Dispatcher") as fake:
            await self._patrol([_finding()], dry_run=True)
        fake.assert_not_called()
        await self.service.configure({"enabled": False})

    async def test_disabled_patrol_does_not_dispatch(self) -> None:
        await self.service.configure({
            "enabled": False, "dispatch_mode": "agents",
        })
        with patch("stackflowos.autonomy.dispatcher.Dispatcher") as fake:
            await self._patrol([_finding()], dry_run=False)
        fake.assert_not_called()

    async def test_enabled_patrol_calls_dispatcher_and_guardian(self) -> None:
        await self.service.configure({
            "enabled": True, "on_start": False, "interval_s": 600,
            "dispatch_mode": "agents", "approve_mode": "observe",
        })
        with patch("stackflowos.autonomy.dispatcher.Dispatcher") as dispatch, \
                patch("stackflowos.autonomy.guardian.Guardian") as guardian:
            dispatch.return_value.apply = _async([{
                "fingerprint": "fp-1", "action": "spawn", "status": "ok",
                "role": "coder", "task_id": "t-1", "agent_id": "agent-coder-1",
                "ts": 1}])
            guardian.return_value.apply = _async([{
                "request_id": "apr-1", "decision": "observe",
                "capability": "fs.write", "principal": "agent-coder-1",
                "risk": "medium", "reason": "ok", "ts": 1}])
            report = await self._patrol([_finding()], dry_run=False)
        dispatch.assert_called_once()
        guardian.assert_called_once()
        self.assertEqual(report["dispatched"][0]["status"], "ok")
        self.assertEqual(report["approvals"][0]["decision"], "observe")
        types = [record.type for record in self.app.eventlog.query(
            types=["autonomy"], limit=50)]
        self.assertIn("autonomy.dispatched", types)
        self.assertIn("autonomy.approval", types)
        self.assertEqual(self.service.status()["counts"]["dispatched"], 1)
        self.assertEqual(self.service.status()["counts"]["approvals"], 0)
        await self.service.configure({"enabled": False})

    async def test_finding_events_are_emitted_only_for_new_warnings(self) -> None:
        await self._patrol([_finding(fingerprint="fp-a", severity="warn")])
        first = [record for record in self.app.eventlog.query(
            types=["autonomy.finding"], limit=50)]
        self.assertEqual(len(first), 1)
        await self._patrol([_finding(fingerprint="fp-a", severity="warn")])
        second = [record for record in self.app.eventlog.query(
            types=["autonomy.finding"], limit=50)]
        self.assertEqual(len(second), 1, "unchanged finding must not re-event")
        await self._patrol([_finding(fingerprint="fp-b", severity="critical")])
        third = [record for record in self.app.eventlog.query(
            types=["autonomy.finding"], limit=50)]
        self.assertEqual(len(third), 2)

    async def test_info_findings_do_not_emit_finding_events(self) -> None:
        await self._patrol([_finding(severity="info", fingerprint="fp-info")])
        types = [record.type for record in self.app.eventlog.query(
            types=["autonomy.finding"], limit=50)]
        self.assertEqual(types, [])

    async def test_configure_persists_and_reconciles_loop(self) -> None:
        await self.service.configure({
            "enabled": True, "on_start": False, "interval_s": 600,
        })
        self.assertTrue(self.app.config.autonomy.enabled)
        self.assertIsNotNone(self.service._task)
        self.assertFalse(self.service._task.done())
        persisted = Config.load(root=self.root, state_dir=self.root / "state")
        self.assertTrue(persisted.autonomy.enabled)
        await self.service.configure({"enabled": False})
        await asyncio.sleep(0)
        self.assertFalse(self.app.config.autonomy.enabled)
        self.assertIsNone(self.service._task)
        reloaded = Config.load(root=self.root, state_dir=self.root / "state")
        self.assertFalse(reloaded.autonomy.enabled)

    async def test_configure_rejects_unknown_fields(self) -> None:
        with self.assertRaises(BadRequest):
            await self.service.configure({"enabled": True, "nonsense": 1})
        self.assertFalse(self.app.config.autonomy.enabled)

    async def test_configure_emits_policy_changed(self) -> None:
        await self.service.configure({"approve_mode": "observe"})
        types = [record.type for record in self.app.eventlog.query(
            types=["autonomy"], limit=50)]
        self.assertIn("autonomy.policy.changed", types)
        await self.service.configure({"approve_mode": "off"})

    async def test_state_survives_service_reload(self) -> None:
        await self._patrol([_finding()])
        seq = self.service.status()["counts"]["patrols"]
        from stackflowos.autonomy.service import AutonomyService

        fresh = AutonomyService(self.app)
        status = fresh.status()
        self.assertEqual(status["counts"]["patrols"], seq)
        self.assertEqual(status["findings"][0]["fingerprint"], "fp-1")
        self.app.autonomy = self.service

    async def test_corrupt_state_file_is_reset(self) -> None:
        path = self.root / "state" / "state" / "autonomy.json"
        path.write_text("{not json", "utf-8")
        from stackflowos.autonomy.service import AutonomyService

        fresh = AutonomyService(self.app)
        self.assertEqual(fresh.status()["counts"]["patrols"], 0)
        self.app.autonomy = self.service


def _async(value):
    async def _call(*_args, **_kwargs):
        return value

    return _call


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

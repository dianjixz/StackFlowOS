"""Tests for Prometheus metrics (observability.metrics)."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from stackflowos.l2 import EventLog
from stackflowos.observability import metrics
from stackflowos.web.api import WebReply


class _Observability:
    def __init__(self, enabled: bool = True):
        self.metrics_enabled = enabled


class _Config:
    def __init__(self, enabled: bool = True):
        self.observability = _Observability(enabled)


class _Supervisor:
    def __init__(self, agents):
        self._agents = agents

    def list(self):
        return [SimpleNamespace(status=status, role=role)
                for status, role in self._agents]

    def stats(self):
        return {"by_status": {}, "by_role": {}}


class _Projects:
    def __init__(self, projects, tasks):
        self._projects = projects
        self._tasks = tasks

    def list(self):
        return [SimpleNamespace(status=status) for status in self._projects]

    def task_list(self):
        return [SimpleNamespace(status=status) for status in self._tasks]


class _Approvals:
    def __init__(self, pending: int = 0):
        self._pending = pending

    def pending_count(self):
        return self._pending


class _Leases:
    def __init__(self, count: int = 0):
        self._count = count

    def list(self, *, active_only: bool = True):
        return [SimpleNamespace(id=f"l-{i}") for i in range(self._count)]


class _Provider:
    def __init__(self, usage=None, name="openai", model="gpt-test"):
        self.name = name
        self.model = model
        if usage is not None:
            self.usage = usage


class FakeApp:
    def __init__(self, tmp: Path, *, enabled: bool = True):
        self.eventlog = EventLog(tmp / "events")
        self.config = _Config(enabled)
        self.started_at = int(time.time() * 1000) - 5000
        self.supervisor = _Supervisor([("working", "coder"),
                                       ("idle", "tester")])
        self.projects = _Projects(["active", "review"],
                                  ["doing", "todo", "done"])
        self.approvals = _Approvals(2)
        self.leases = _Leases(3)
        self.provider = _Provider(
            usage={"tokens": [{"provider": "openai", "model": "gpt-test",
                               "kind": "prompt", "count": 11},
                              {"provider": "openai", "model": "gpt-test",
                               "kind": "completion", "count": 4}],
                   "cost_usd": 0.0125})
        self.cards = [
            {"state": "ready", "tier": "system"},
            {"state": 'odd"state', "tier": "worker", "restart_count": 4},
        ]

    def unit_cards(self):
        return self.cards


class MetricsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.app = FakeApp(self.tmp)
        self.extension = metrics.build(self.app)
        await self.extension.start()

    async def asyncTearDown(self):
        await self.extension.stop()
        await self.app.eventlog.close()
        self._tmp.cleanup()

    async def test_required_families_and_values(self):
        self.app.eventlog.append("approval.decided", {"decision": "allow"})
        self.app.eventlog.append("approval.decided", {"decision": "deny"})
        await asyncio.sleep(0.05)  # let the subscriber task consume
        text = self.extension.metrics_text()
        for name in (
            "sfo_up", "sfo_uptime_seconds", "sfo_events_total", "sfo_event_seq",
            "sfo_units", "sfo_unit_restarts_total", "sfo_agents",
            "sfo_agent_queue_depth", "sfo_projects", "sfo_tasks",
            "sfo_approvals_pending", "sfo_approvals_decided_total",
            "sfo_leases_active", "sfo_llm_tokens_total",
            "sfo_llm_cost_usd_total", "sfo_sse_clients",
            "sfo_http_requests_total",
        ):
            self.assertIn(f"# HELP {name} ", text)
            self.assertIn(f"# TYPE {name} ", text)
        self.assertIn("sfo_up 1", text)
        self.assertIn('sfo_units{state="ready",tier="system"} 1', text)
        # Label values are escaped (quote/backslash/newline safe).
        self.assertIn('sfo_units{state="odd\\"state",tier="worker"} 1', text)
        self.assertIn("sfo_unit_restarts_total 4", text)
        self.assertIn('sfo_agents{status="idle",role="tester"} 1', text)
        self.assertIn('sfo_projects{status="active"} 1', text)
        self.assertIn('sfo_tasks{status="done"} 1', text)
        self.assertIn("sfo_approvals_pending 2", text)
        self.assertIn('sfo_approvals_decided_total{decision="allow"} 1', text)
        self.assertIn('sfo_approvals_decided_total{decision="deny"} 1', text)
        self.assertIn("sfo_leases_active 3", text)
        self.assertIn('sfo_llm_tokens_total{provider="openai",model="gpt-test",kind="prompt"} 11', text)
        self.assertIn("sfo_llm_cost_usd_total 0.0125", text)
        self.assertIn("sfo_sse_clients 0", text)
        self.assertIn("sfo_agent_queue_depth 0", text)

    async def test_route_returns_web_reply_with_prometheus_content_type(self):
        status, reply = await self.extension.handle_metrics(None, {}, {})
        self.assertEqual(status, 200)
        self.assertIsInstance(reply, WebReply)
        self.assertEqual(reply.content_type,
                         "text/plain; version=0.0.4; charset=utf-8")
        self.assertIn(b"sfo_up 1", reply.body)
        self.assertTrue(self.extension.health()["enabled"])

    async def test_disabled_metrics_route_is_404(self):
        app = FakeApp(self.tmp / "disabled", enabled=False)
        extension = metrics.build(app)
        try:
            status, payload = await extension.handle_metrics(None, {}, {})
            self.assertEqual(status, 404)
            self.assertFalse(payload["ok"])
            await extension.start()
            self.assertEqual(extension.metrics_text().startswith("# metrics"),
                             True)
        finally:
            await extension.stop()
            await app.eventlog.close()

    async def test_stop_unsubscribes_eventlog(self):
        self.assertEqual(len(self.app.eventlog._subscribers), 1)
        await self.extension.stop()
        self.assertEqual(len(self.app.eventlog._subscribers), 0)
        # The tearDown stop is idempotent.
        await self.extension.stop()

    async def test_http_and_sse_instrumentation(self):
        metrics.ensure_instrumentation()
        metrics.record_http_request(self.app, "GET", 200)
        metrics.record_http_request(self.app, "GET", 200)
        metrics.record_http_request(self.app, "POST", 500)
        metrics.record_sse_client(self.app, 1)
        text = self.extension.metrics_text()
        self.assertIn('sfo_http_requests_total{method="GET",status="200"} 2', text)
        self.assertIn('sfo_http_requests_total{method="POST",status="500"} 1', text)
        self.assertIn("sfo_sse_clients 1", text)
        metrics.record_sse_client(self.app, -1)
        self.assertIn("sfo_sse_clients 0", self.extension.metrics_text())

    async def test_optional_llm_usage_shapes(self):
        self.app.provider = _Provider(
            usage={"tokens": {"openai": {"gpt-x": {"prompt": 7,
                                                  "completion": 3}}},
                   "total_cost_usd": 0.5})
        text = self.extension.metrics_text()
        self.assertIn(
            'sfo_llm_tokens_total{provider="openai",model="gpt-x",kind="completion"} 3',
            text)
        self.assertIn(
            'sfo_llm_tokens_total{provider="openai",model="gpt-x",kind="prompt"} 7',
            text)
        self.assertIn("sfo_llm_cost_usd_total 0.5", text)

    async def test_usage_tracker_snapshot_shape(self):
        bucket = {"prompt": 10, "completion": 5, "total": 15,
                  "cost": 0.02, "calls": 2}
        self.app.provider = _Provider(usage={
            "totals": dict(bucket),
            "providers": {"openai": dict(bucket)},
            "models": {"gpt-snap": dict(bucket)},
            "projects": {"p-1": dict(bucket)},
        })
        text = self.extension.metrics_text()
        self.assertIn(
            'sfo_llm_tokens_total{provider="openai",model="gpt-snap",kind="prompt"} 10',
            text)
        self.assertIn(
            'sfo_llm_tokens_total{provider="openai",model="gpt-snap",kind="completion"} 5',
            text)
        self.assertIn("sfo_llm_cost_usd_total 0.02", text)
        # Bucket bookkeeping fields are not tokens.
        self.assertNotIn('kind="cost"', text)
        self.assertNotIn('kind="calls"', text)

    async def test_missing_llm_usage_still_documents_family(self):
        self.app.provider = _Provider(usage=None, name="none", model="none")
        text = self.extension.metrics_text()
        self.assertIn("# HELP sfo_llm_tokens_total ", text)
        self.assertNotIn("sfo_llm_tokens_total{", text)
        self.assertIn("sfo_llm_cost_usd_total 0", text)

    async def test_health_reports_subscription(self):
        health = self.extension.health()
        self.assertTrue(health["ok"])
        self.assertTrue(health["subscribed"])
        self.assertEqual(health["last_error"], None)


if __name__ == "__main__":
    unittest.main()

"""Tests for ``stackflowos.autonomy.patrol``.

Every check is exercised on synthetic snapshots (no hub, no worker, no state
directory).  ``collect_snapshot`` is exercised on a fake app that counts and
raises its way through the probes.
"""

from __future__ import annotations

import types

import pytest

from stackflowos.autonomy.models import DispatchHint, Finding, fingerprint
from stackflowos.autonomy.patrol import Snapshot, collect_snapshot, run_checks

NOW = 1_700_000_000_000
MINUTE = 60 * 1000
HOUR = 60 * MINUTE


def cfg(**overrides):
    values = {
        "task_idle_s": 120.0,
        "approval_age_s": 120.0,
        "failure_window_s": 3600.0,
        "failure_threshold": 3,
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def snapshot(**fields) -> Snapshot:
    fields.setdefault("now", NOW)
    return Snapshot(**fields)


def checks_of(findings: list[Finding]) -> list[str]:
    return [finding.check for finding in findings]


def by_check(findings: list[Finding], check: str) -> list[Finding]:
    return [finding for finding in findings if finding.check == check]


# ----------------------------------------------------------------------
# empty / malformed systems
# ----------------------------------------------------------------------
def test_empty_system_has_no_findings():
    empty = Snapshot()
    assert run_checks(empty, None) == []

    healthy = snapshot(health={"status": "ok"},
                       units=[{"unit_id": "sys.0", "kind": "sys",
                               "state": "ready", "status": "online"}])
    assert run_checks(healthy, cfg()) == []


def test_malformed_snapshot_and_config_do_not_raise():
    broken = Snapshot(
        now=NOW,
        health="not-a-dict",
        stats=None,
        units=[None, 42, "x", {"kind": "sys"}],
        agents=[None, "x", {"status": "working"}],
        projects=[None, "x", {}, {"id": None}],
        tasks={"p": [None, "x", {}]},  # type: ignore[dict-item]
        approvals=[None, {"status": "pending"}],
        leases=[None, {"status": "active"}, {"status": "active", "holder": ""}],
        events=[None, {"type": "agent.failed"}],
        project_health={"p": None, "q": "bad"},
        task_timeout_s="oops",  # type: ignore[arg-type]
        max_workers=None,  # type: ignore[arg-type]
    )
    findings = run_checks(broken, object())
    assert isinstance(findings, list)

    # A config object missing every autonomy attribute falls back to defaults.
    assert isinstance(run_checks(snapshot(), types.SimpleNamespace()), list)


# ----------------------------------------------------------------------
# hub / units / projects
# ----------------------------------------------------------------------
def test_hub_degraded_reports_critical_only_when_not_ok():
    assert by_check(run_checks(snapshot(health={"status": "degraded",
                                                "transport": "down"}), cfg()),
                    "hub.degraded")[0].severity == "critical"

    degraded = by_check(run_checks(
        snapshot(health={"status": "degraded", "transport": "down",
                         "http": "down"}), cfg()), "hub.degraded")[0]
    assert degraded.severity == "critical"
    assert "degraded" in degraded.detail
    assert "down" in degraded.detail
    assert degraded.fingerprint

    assert by_check(run_checks(snapshot(health={"status": "ok"}), cfg()),
                    "hub.degraded") == []
    assert by_check(run_checks(snapshot(health={}), cfg()),
                    "hub.degraded") == []


def test_unit_problem_flags_non_worker_failures():
    units = [
        {"unit_id": "sys.0", "kind": "sys", "state": "ready",
         "status": "online"},
        {"unit_id": "coder.2", "kind": "worker", "state": "failed",
         "status": "failed"},  # workers are the failure domain's problem
        {"unit_id": "web.1", "kind": "web", "state": "ready",
         "status": "failed"},
        {"unit_id": "llm.1", "kind": "llm", "state": "unreachable",
         "status": "offline", "reason": "连接超时"},
    ]
    findings = by_check(run_checks(snapshot(units=units), cfg()),
                        "unit.problem")
    assert {finding.unit_id for finding in findings} == {"web.1", "llm.1"}
    assert all(finding.severity == "critical" for finding in findings)
    assert any("连接超时" in finding.detail for finding in findings)

    assert by_check(run_checks(snapshot(units=units[:1]), cfg()),
                    "unit.problem") == []


def test_project_degraded_and_healthy_projects():
    projects = [
        {"id": "p1", "name": "Alpha", "status": "active", "degraded": True},
        {"id": "p2", "name": "Beta", "status": "degraded"},
        {"id": "p3", "name": "Gamma", "status": "active"},
        {"id": "p4", "name": "Delta", "status": "planning", "degraded": False},
    ]
    findings = by_check(run_checks(snapshot(projects=projects), cfg()),
                        "project.degraded")
    assert [finding.project_id for finding in findings] == ["p1", "p2"]
    assert all(finding.severity == "critical" for finding in findings)


def test_project_no_plan_dispatches_a_lead():
    projects = [
        {"id": "p1", "name": "Empty", "status": "active"},
        {"id": "p2", "name": "HasTasks", "status": "active"},
        {"id": "p3", "name": "Planning", "status": "planning"},
        {"id": "p4", "name": "HasLead", "status": "active"},
    ]
    tasks = {"p2": [{"id": "t1", "project_id": "p2", "status": "todo"}]}
    agents = [{"id": "agent-lead-1", "role": "lead", "project_id": "p4",
               "status": "working"}]
    findings = by_check(run_checks(snapshot(projects=projects, tasks=tasks,
                                            agents=agents), cfg()),
                        "project.no_plan")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.project_id == "p1"
    assert finding.severity == "warn"
    assert isinstance(finding.dispatch, DispatchHint)
    assert finding.dispatch.kind == "spawn"
    assert finding.dispatch.role == "lead"


def test_project_health_reports_first_three_problem_checks():
    report = {
        "project_id": "p1",
        "project_name": "Alpha",
        "ready": False,
        "summary": {"ok": 1, "warn": 1, "fail": 3, "unchecked": 0},
        "checks": [
            {"id": "scope", "label": "作用域", "status": "ok",
             "message": "ok"},
            {"id": "workspace", "label": "工作区", "status": "fail",
             "message": "目录不存在", "fix": "恢复目录"},
            {"id": "board", "label": "任务看板", "status": "fail",
             "message": "看板为空", "fix": "创建任务"},
            {"id": "memory", "label": "项目记忆", "status": "warn",
             "message": "缺记忆键", "fix": "补齐记忆"},
            {"id": "events", "label": "事件审计", "status": "fail",
             "message": "不可写", "fix": "修权限"},
        ],
        "next_actions": ["恢复目录", "创建任务", "修权限"],
    }
    findings = by_check(run_checks(
        snapshot(project_health={"p1": report}), cfg()), "project.health")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "warn"
    assert finding.project_id == "p1"
    assert finding.fix == "恢复目录"
    assert [item["id"] for item in finding.evidence["checks"]] == [
        "workspace", "board", "memory"]
    assert finding.evidence["next_actions"] == ["恢复目录", "创建任务", "修权限"]
    assert "3 项阻断" in finding.detail and "1 项告警" in finding.detail

    ready = {"ready": True, "checks": []}
    assert by_check(run_checks(snapshot(project_health={"p1": ready}), cfg()),
                    "project.health") == []


# ----------------------------------------------------------------------
# tasks
# ----------------------------------------------------------------------
def test_task_idle_dispatches_unclaimed_ready_tasks():
    tasks = {
        "p1": [
            {"id": "t1", "project_id": "p1", "title": "写文档",
             "status": "todo", "deps": [],
             "created_at": NOW - 10 * MINUTE,
             "updated_at": NOW - 10 * MINUTE},
            {"id": "t2", "project_id": "p1", "title": "有人在干",
             "status": "todo", "created_at": NOW - 10 * MINUTE},
            {"id": "t3", "project_id": "p1", "title": "被依赖阻塞",
             "status": "todo", "deps": ["t1"],
             "created_at": NOW - 10 * MINUTE},
            {"id": "t4", "project_id": "p1", "title": "依赖不存在",
             "status": "todo", "deps": ["missing"],
             "created_at": NOW - 10 * MINUTE},
            {"id": "t5", "project_id": "p1", "title": "测试一下",
             "status": "todo", "role": "tester", "deps": ["t9"],
             "created_at": NOW - 10 * MINUTE},
            {"id": "t9", "project_id": "p1", "title": "已完成的依赖",
             "status": "done", "created_at": NOW - 20 * MINUTE},
        ],
    }
    agents = [{"id": "agent-coder-1", "role": "coder", "project_id": "p1",
               "task_id": "t2", "status": "working"}]
    findings = by_check(run_checks(snapshot(tasks=tasks, agents=agents), cfg()),
                        "task.idle")
    assert [finding.task_id for finding in findings] == ["t1", "t5"]
    roles = {finding.task_id: finding.dispatch.role for finding in findings}
    assert roles == {"t1": "coder", "t5": "tester"}
    assert all(finding.dispatch.kind == "spawn" for finding in findings)
    assert all(finding.severity == "warn" for finding in findings)

    fresh = {"p1": [{"id": "t1", "project_id": "p1", "status": "todo",
                     "created_at": NOW - 60 * 1000}]}
    assert by_check(run_checks(snapshot(tasks=fresh), cfg()),
                    "task.idle") == []


def test_task_stalled_flags_doing_tasks_past_timeout_without_agent():
    tasks = {
        "p1": [
            {"id": "s1", "project_id": "p1", "title": "卡住",
             "status": "doing", "started_at": NOW - 31 * MINUTE},
            {"id": "s2", "project_id": "p1", "title": "刚开始",
             "status": "doing", "started_at": NOW - 60 * 1000},
            {"id": "s3", "project_id": "p1", "title": "有人在干",
             "status": "doing", "started_at": NOW - 31 * MINUTE},
        ],
    }
    agents = [{"id": "agent-coder-1", "role": "coder", "project_id": "p1",
               "task_id": "s3", "status": "working"}]
    snap = snapshot(tasks=tasks, agents=agents, task_timeout_s=1800.0)
    findings = by_check(run_checks(snap, cfg()), "task.stalled")
    assert [finding.task_id for finding in findings] == ["s1"]
    assert findings[0].dispatch is None
    assert findings[0].severity == "warn"


# ----------------------------------------------------------------------
# agents / approvals / leases / events
# ----------------------------------------------------------------------
def test_agent_failure_aggregates_by_role_and_error_code():
    agents = [
        {"id": "agent-coder-1", "role": "coder", "status": "failed",
         "error_code": "WORKSPACE_MISSING", "last_seen": NOW - 60 * 1000},
        {"id": "agent-coder-2", "role": "coder", "status": "failed",
         "error_code": "WORKSPACE_MISSING", "last_seen": NOW - 120 * 1000},
        {"id": "agent-coder-3", "role": "coder", "status": "blocked",
         "error_code": "WORKSPACE_MISSING", "last_seen": NOW - 180 * 1000},
        {"id": "agent-tester-1", "role": "tester", "status": "blocked",
         "last_seen": NOW - 60 * 1000},
        {"id": "agent-coder-9", "role": "coder", "status": "failed",
         "error_code": "PROMPT_TOO_LONG", "last_seen": NOW - 2 * HOUR},
        {"id": "agent-coder-4", "role": "coder", "status": "failed",
         "error_code": "OLD", "last_seen": NOW - 2 * HOUR},
    ]
    findings = by_check(run_checks(snapshot(agents=agents), cfg()),
                        "agent.failure")
    by_code = {(finding.evidence["role"],
                finding.evidence["error_code"]): finding
               for finding in findings}
    assert set(by_code) == {("coder", "WORKSPACE_MISSING"), ("tester", "UNKNOWN")}
    assert by_code[("coder", "WORKSPACE_MISSING")].severity == "critical"
    assert by_code[("coder", "WORKSPACE_MISSING")].evidence["count"] == 3
    assert by_code[("tester", "UNKNOWN")].severity == "warn"
    assert by_code[("tester", "UNKNOWN")].evidence["count"] == 1


def test_approval_pending_warn_and_critical_and_cap():
    approvals = [
        {"id": "a1", "status": "pending", "capability": "fs.write",
         "principal": "agent-coder-1", "risk": "medium",
         "created_at": NOW - 5 * MINUTE},
        {"id": "a2", "status": "pending", "capability": "shell.exec",
         "principal": "agent-coder-2", "risk": "high",
         "created_at": NOW - 15 * MINUTE},
        {"id": "a3", "status": "pending", "capability": "net.fetch",
         "principal": "agent-researcher-1", "risk": "low",
         "created_at": NOW - 30 * 1000},
        {"id": "a4", "status": "approved", "capability": "fs.write",
         "created_at": NOW - HOUR},
    ]
    findings = by_check(run_checks(snapshot(approvals=approvals), cfg()),
                        "approval.pending")
    by_id = {finding.evidence["request_id"]: finding for finding in findings}
    assert set(by_id) == {"a1", "a2"}
    assert by_id["a1"].severity == "warn"
    assert by_id["a2"].severity == "critical"
    assert by_id["a2"].fingerprint

    many = [{"id": f"req-{index}", "status": "pending",
             "capability": "fs.write", "principal": "agent-coder-1",
             "created_at": NOW - 5 * MINUTE} for index in range(25)]
    capped = by_check(run_checks(snapshot(approvals=many), cfg()),
                      "approval.pending")
    assert len(capped) == 20


def test_approval_denial_groups_by_capability_and_principal_role():
    def denied(event_type, capability, principal, ts, decision="deny"):
        return {"type": event_type, "ts": ts,
                "data": {"decision": decision, "capability": capability,
                         "principal": principal}}

    events = [
        denied("approval.decided", "fs.write", "agent-coder-3",
               NOW - 5 * MINUTE),
        denied("approval.decided", "fs.write", "agent-coder-7",
               NOW - 4 * MINUTE),
        denied("approval.decided", "fs.write", "agent-coder-9",
               NOW - 3 * MINUTE),
        denied("approval.decided", "fs.write", "agent-coder-3",
               NOW - 2 * HOUR),  # outside the window
        denied("approval.decided", "shell.exec", "agent-coder-3",
               NOW - 2 * MINUTE, decision="allow"),
        {"type": "approval.expired", "ts": NOW - MINUTE, "data": {}},
    ]
    findings = by_check(run_checks(snapshot(events=events), cfg()),
                        "approval.denial")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "warn"
    assert finding.evidence["capability"] == "fs.write"
    assert finding.evidence["principal_prefix"] == "agent-coder"
    assert finding.evidence["count"] == 3

    assert by_check(run_checks(snapshot(events=events[:2]), cfg()),
                    "approval.denial") == []


def test_lease_orphan_ignores_live_holders():
    units = [{"unit_id": "sys.0", "kind": "sys", "state": "ready"}]
    agents = [{"id": "agent-coder-1", "role": "coder", "unit_id": "coder.1",
               "status": "working"}]
    leases = [
        {"id": "l1", "status": "active", "holder": "ghost.1",
         "resource": "unit:ghost.1", "expires_at": NOW + MINUTE},
        {"id": "l2", "status": "active", "holder": "coder.1",
         "resource": "unit:coder.1", "expires_at": NOW + MINUTE},
        {"id": "l3", "status": "active", "holder": "sys.0",
         "resource": "unit:sys.0", "expires_at": NOW + MINUTE},
        {"id": "l4", "status": "released", "holder": "ghost.2",
         "resource": "unit:ghost.2"},
    ]
    findings = by_check(run_checks(snapshot(units=units, agents=agents,
                                            leases=leases), cfg()),
                        "lease.orphan")
    assert [finding.evidence["lease_id"] for finding in findings] == ["l1"]
    assert findings[0].severity == "warn"


def test_events_failure_burst_requires_threshold():
    def event(event_type, ts):
        return {"type": event_type, "ts": ts, "source": "sys", "data": {}}

    burst = [
        event("unit.failed", NOW - 60 * 1000),
        event("agent.failed", NOW - 2 * 60 * 1000),
        event("tool.error", NOW - 3 * 60 * 1000),
        event("project.updated", NOW - 60 * 1000),  # not a failure type
        event("unit.failed", NOW - 2 * HOUR),       # outside the window
    ]
    findings = by_check(run_checks(snapshot(events=burst), cfg()),
                        "events.failure_burst")
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert findings[0].evidence["total"] == 3
    assert findings[0].evidence["by_type"] == {
        "agent.failed": 1, "tool.error": 1, "unit.failed": 1}

    assert by_check(run_checks(snapshot(events=burst[:2]), cfg()),
                    "events.failure_burst") == []


# ----------------------------------------------------------------------
# ordering / fingerprints
# ----------------------------------------------------------------------
def test_findings_sorted_by_severity_then_check():
    snap = snapshot(
        health={"status": "degraded"},
        units=[{"unit_id": "llm.1", "kind": "llm", "state": "unreachable"}],
        projects=[{"id": "p1", "name": "HasTask", "status": "active"},
                  {"id": "p2", "name": "Empty", "status": "active"}],
        tasks={"p1": [{"id": "t1", "project_id": "p1", "status": "todo",
                       "created_at": NOW - 10 * MINUTE}]},
    )
    checks = checks_of(run_checks(snap, cfg()))
    assert checks == ["hub.degraded", "unit.problem",
                      "project.no_plan", "task.idle"]
    severities = [finding.severity
                  for finding in run_checks(snap, cfg())]
    assert severities == ["critical", "critical", "warn", "warn"]


def test_fingerprints_are_stable_across_time_and_counts():
    def idle_snapshot(now, task_id="t1"):
        return snapshot(
            now=now,
            tasks={"p1": [{"id": task_id, "project_id": "p1",
                           "status": "todo", "deps": [],
                           "created_at": now - 10 * MINUTE,
                           "updated_at": now - 10 * MINUTE}]},
        )

    first = run_checks(idle_snapshot(NOW), cfg())
    second = run_checks(idle_snapshot(NOW + 5 * MINUTE), cfg())
    assert [finding.fingerprint for finding in first] == [
        finding.fingerprint for finding in second]
    assert first[0].fingerprint == fingerprint("task.idle", "p1", "t1")

    other = run_checks(idle_snapshot(NOW, task_id="t2"), cfg())
    assert other[0].fingerprint != first[0].fingerprint


# ----------------------------------------------------------------------
# collect_snapshot
# ----------------------------------------------------------------------
class _FakeProject:
    def __init__(self, project_id, status="active"):
        self.project_id = project_id
        self.status = status

    def to_dict(self):
        return {"id": self.project_id, "name": self.project_id.upper(),
                "status": self.status}


class _FakeTask:
    def __init__(self, task_id, project_id):
        self.task_id = task_id
        self.project_id = project_id

    def to_dict(self):
        return {"id": self.task_id, "project_id": self.project_id,
                "status": "todo"}


class _FakeAgent:
    def __init__(self, agent_id, project_id, task_id):
        self.agent_id = agent_id
        self.project_id = project_id
        self.task_id = task_id

    def to_dict(self):
        return {"id": self.agent_id, "role": "coder", "status": "working",
                "project_id": self.project_id, "task_id": self.task_id,
                "unit_id": "coder.1"}


class _FakeApproval:
    def to_dict(self):
        return {"id": "req-1", "status": "pending",
                "preview": "truncated", "preview_full": "SECRET"}

    def to_public_dict(self):
        public = self.to_dict()
        public.pop("preview_full")
        return public


class _FakeLease:
    def to_dict(self):
        return {"id": "l1", "status": "active", "holder": "sys.0",
                "resource": "unit:sys.0", "expires_at": NOW + MINUTE}


class _FakeEvent:
    def to_dict(self):
        return {"seq": 1, "type": "unit.registered", "ts": NOW,
                "source": "sys", "data": {}}


class _FakeSupervisor:
    def __init__(self, agents):
        self._agents = agents
        self.calls = 0

    def list(self):
        self.calls += 1
        return list(self._agents)


class _FakeStore:
    def __init__(self, projects, tasks):
        self._projects = projects
        self._tasks = tasks
        self.list_calls = 0
        self.task_list_calls = 0

    def list(self):
        self.list_calls += 1
        return list(self._projects)

    def task_list(self):
        self.task_list_calls += 1
        return list(self._tasks)


class _FakeBroker:
    def __init__(self, approvals):
        self._approvals = approvals
        self.kwargs = None

    def list(self, **kwargs):
        self.kwargs = kwargs
        return list(self._approvals)


class _FakeLeaseManager:
    def __init__(self, leases):
        self._leases = leases
        self.kwargs = None

    def list(self, **kwargs):
        self.kwargs = kwargs
        return list(self._leases)


class _FakeEventLog:
    def __init__(self, events):
        self._events = events
        self.kwargs = None

    def query(self, **kwargs):
        self.kwargs = kwargs
        return list(self._events)


class _FakeApp:
    def __init__(self, projects, tasks, *, health_error=False):
        self.projects = _FakeStore(projects, tasks)
        self.supervisor = _FakeSupervisor([
            _FakeAgent("agent-coder-1", "p1", "t1")])
        self.approvals = _FakeBroker([_FakeApproval()])
        self.leases = _FakeLeaseManager([_FakeLease()])
        self.eventlog = _FakeEventLog([_FakeEvent()])
        self.config = types.SimpleNamespace(
            agents=types.SimpleNamespace(task_timeout_s=900.0,
                                         max_workers=4))
        self._health_error = health_error
        self.health_calls = 0
        self.health_calls_by_project = []

    def health(self):
        self.health_calls += 1
        if self._health_error:
            raise RuntimeError("transport down")
        return {"status": "ok", "transport": "ok"}

    def stats(self):
        return {"units": {"total": 1}}

    def unit_cards(self):
        return [{"unit_id": "sys.0", "kind": "sys", "state": "ready"}]

    def project_health(self, project_id):
        self.health_calls_by_project.append(project_id)
        if project_id == "p-bad":
            raise RuntimeError("probe failed")
        return {"project_id": project_id, "ready": True, "checks": []}


def test_collect_snapshot_reads_projections_and_groups_tasks():
    projects = [_FakeProject("p1", "active"), _FakeProject("p2", "planning")]
    tasks = [_FakeTask("t1", "p1"), _FakeTask("t2", "p1"),
             _FakeTask("t3", "p2")]
    app = _FakeApp(projects, tasks)

    snap = collect_snapshot(app, now=NOW)

    assert snap.now == NOW
    assert snap.health == {"status": "ok", "transport": "ok"}
    assert snap.stats == {"units": {"total": 1}}
    assert snap.units[0]["unit_id"] == "sys.0"
    assert snap.agents[0]["unit_id"] == "coder.1"
    assert [project["id"] for project in snap.projects] == ["p1", "p2"]
    assert {project_id: [task["id"] for task in rows]
            for project_id, rows in snap.tasks.items()} == {
        "p1": ["t1", "t2"], "p2": ["t3"]}
    assert app.projects.task_list_calls == 1
    assert snap.approvals == [{"id": "req-1", "status": "pending",
                              "preview": "truncated"}]
    assert app.approvals.kwargs == {"limit": 200}
    assert snap.leases[0]["id"] == "l1"
    assert app.leases.kwargs == {"active_only": True}
    assert snap.events[0]["type"] == "unit.registered"
    assert app.eventlog.kwargs == {"limit": 300}
    assert snap.task_timeout_s == 900.0
    assert snap.max_workers == 4
    # planning projects are never probed
    assert app.health_calls_by_project == ["p1"]


def test_collect_snapshot_swallows_errors_and_skips_broken_projects():
    projects = [_FakeProject("p1", "active"), _FakeProject("p-bad", "active"),
                _FakeProject("p3", "degraded")]
    app = _FakeApp(projects, [], health_error=True)

    snap = collect_snapshot(app)

    assert snap.health == {}
    assert app.health_calls == 1
    # p-bad raised inside project_health; p3 is still probed afterwards.
    assert app.health_calls_by_project == ["p1", "p-bad", "p3"]
    assert sorted(snap.project_health) == ["p1", "p3"]

    empty = collect_snapshot(object(), now=123)
    assert empty.now == 123
    assert empty.health == {} and empty.stats == {}
    assert empty.units == [] and empty.agents == [] and empty.projects == []
    assert empty.tasks == {} and empty.approvals == [] and empty.leases == []
    assert empty.events == [] and empty.project_health == {}
    assert empty.task_timeout_s == 1800.0 and empty.max_workers == 6


def test_collect_snapshot_bounds_project_health_to_fifty():
    projects = [_FakeProject(f"p{index}", "active") for index in range(60)]
    app = _FakeApp(projects, [])

    snap = collect_snapshot(app, now=NOW)

    assert len(app.health_calls_by_project) == 50
    assert len(snap.project_health) == 50


def test_collect_snapshot_with_missing_services_uses_defaults():
    app = types.SimpleNamespace()  # no health/stats/supervisor/projects/config
    snap = collect_snapshot(app, now=NOW)
    assert snap.now == NOW
    assert snap.task_timeout_s == 1800.0
    assert snap.max_workers == 6
    assert run_checks(snap, cfg()) == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

"""System patrol: read-only checks that turn state into findings.

Implementation contract: ``docs/team/autonomy-plan.md`` §3.2.

The module never mutates state.  :func:`collect_snapshot` only reads existing
service projections (and swallows every probe failure), while
:func:`run_checks` is a pure function over one :class:`Snapshot`.  That split
lets tests build synthetic snapshots without a hub — no worker, no transport,
no state directory.

Findings are advisory data: the dispatcher decides whether a
``DispatchHint`` becomes work, the guardian decides whether a pending approval
is safe to auto-approve.  Patrol only reports what it sees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..protocol import now_ms
from .models import (
    ACTIVE_AGENT_STATUSES,
    DispatchHint,
    Finding,
    fingerprint,
    severity_rank,
)

#: Project statuses whose health report is collected (and probed over the
#: filesystem), matching ``App.project_health`` callers in the console.
_HEALTH_PROJECT_STATUSES = frozenset({"active", "review", "degraded"})
#: Hard bound on ``app.project_health`` probes per patrol: every call runs
#: filesystem write probes, so an unbounded loop would be expensive.
_MAX_PROJECT_HEALTH = 50
#: Individual pending-approval findings per patrol (oldest first).
_MAX_PENDING_APPROVALS = 20
#: A pending approval older than this is ``critical`` (milliseconds).
_PENDING_CRITICAL_MS = 10 * 60 * 1000
#: Unit states that mean "this unit is not healthy" (any non-worker unit).
_UNIT_PROBLEM_STATES = frozenset({"failed", "unreachable"})
#: Event types that count as a runtime failure for ``events.failure_burst``.
_FAILURE_EVENT_TYPES = frozenset({"unit.failed", "agent.failed", "tool.error"})
#: Same (capability, principal-role) denials needed before patrol speaks up.
_DENIAL_THRESHOLD = 3
#: Defaults matching ``config.AutonomyCfg`` / ``config.AgentsCfg``.
_DEFAULT_TASK_IDLE_S = 120.0
_DEFAULT_APPROVAL_AGE_S = 120.0
_DEFAULT_FAILURE_WINDOW_S = 3600.0
_DEFAULT_FAILURE_THRESHOLD = 3
_DEFAULT_TASK_TIMEOUT_S = 1800.0
_DEFAULT_MAX_WORKERS = 6


@dataclass
class Snapshot:
    """Everything one patrol needs, collected once and treated as immutable."""

    now: int = 0
    health: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    units: list[dict] = field(default_factory=list)
    agents: list[dict] = field(default_factory=list)
    projects: list[dict] = field(default_factory=list)
    tasks: dict[str, list[dict]] = field(default_factory=dict)
    approvals: list[dict] = field(default_factory=list)
    leases: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    project_health: dict[str, dict] = field(default_factory=dict)
    task_timeout_s: float = 1800.0
    max_workers: int = 6


# ----------------------------------------------------------------------
# small, total helpers (never raise on malformed input)
# ----------------------------------------------------------------------
def _safe(obj: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    """Call ``obj.name(*args, **kwargs)``; ``None`` when missing or broken."""
    if obj is None:
        return None
    hook = getattr(obj, name, None)
    if not callable(hook):
        return None
    try:
        return hook(*args, **kwargs)
    except Exception:  # noqa: BLE001 - one broken probe must not stop patrol
        return None


def _rows(value: Any, method: str = "to_dict") -> list[dict]:
    """Normalise a service projection into a list of plain dicts."""
    if not isinstance(value, (list, tuple)):
        return []
    rows: list[dict] = []
    for item in value:
        if isinstance(item, dict):
            rows.append(dict(item))
            continue
        hook = getattr(item, method, None)
        if not callable(hook):
            continue
        try:
            row = hook()
        except Exception:  # noqa: BLE001 - skip one bad record
            continue
        if isinstance(row, dict):
            rows.append(dict(row))
    return rows


def _as_dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    try:
        return str(value or "").strip()
    except Exception:  # noqa: BLE001 - a hostile __str__ is just empty text
        return ""


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _field(snapshot: Any, name: str, default: Any = None) -> Any:
    if isinstance(snapshot, dict):
        return snapshot.get(name, default)
    return getattr(snapshot, name, default)


def _cfg(config: Any, name: str, default: Any) -> Any:
    if isinstance(config, dict):
        return config.get(name, default)
    return getattr(config, name, default)


def _minutes(ms: int) -> int:
    return max(0, int(ms) // 60000)


def _seconds(ms: int) -> int:
    return max(0, int(ms) // 1000)


def _principal_prefix(principal: str) -> str:
    """``agent-coder-3`` -> ``agent-coder`` (the Agent Teams "role" prefix)."""
    parts = _text(principal).split("-")
    if len(parts) >= 2:
        return "-".join(parts[:2])
    return _text(principal)


# ----------------------------------------------------------------------
# collection
# ----------------------------------------------------------------------
def collect_snapshot(app: Any, *, now: int | None = None) -> Snapshot:
    """Gather the current system state (read-only) for one patrol.

    Every field is best-effort: a raising service, a missing method or a
    non-dict row degrades to an empty value instead of failing the patrol.
    ``now`` is the patrol start timestamp (milliseconds); the service passes
    its own ``now_ms()`` so findings and the report agree on time.
    """
    try:
        timestamp = int(now) if now is not None else now_ms()
    except (TypeError, ValueError):
        timestamp = now_ms()
    snapshot = Snapshot(now=timestamp)

    try:
        snapshot.health = _as_dict(_safe(app, "health"))
        snapshot.stats = _as_dict(_safe(app, "stats"))
        snapshot.units = _rows(_safe(app, "unit_cards"))

        supervisor = getattr(app, "supervisor", None)
        snapshot.agents = _rows(_safe(supervisor, "list"))

        store = getattr(app, "projects", None)
        snapshot.projects = _rows(_safe(store, "list"))

        grouped: dict[str, list[dict]] = {}
        for task in _rows(_safe(store, "task_list")):
            project_id = _text(task.get("project_id"))
            grouped.setdefault(project_id, []).append(task)
        snapshot.tasks = grouped

        broker = getattr(app, "approvals", None)
        snapshot.approvals = _rows(_safe(broker, "list", limit=200),
                                   method="to_public_dict")

        leases = getattr(app, "leases", None)
        snapshot.leases = _rows(_safe(leases, "list", active_only=True))

        eventlog = getattr(app, "eventlog", None)
        snapshot.events = _rows(_safe(eventlog, "query", limit=300))
    except Exception:  # noqa: BLE001 - collection is best-effort by contract
        pass

    # ``project_health`` is the expensive probe: only live projects, bounded,
    # and a single failing project is skipped rather than failing the patrol.
    try:
        health_map: dict[str, dict] = {}
        attempts = 0
        for project in snapshot.projects:
            if attempts >= _MAX_PROJECT_HEALTH:
                break
            if _text(project.get("status")) not in _HEALTH_PROJECT_STATUSES:
                continue
            project_id = _text(project.get("id"))
            if not project_id:
                continue
            attempts += 1
            report = _safe(app, "project_health", project_id)
            if isinstance(report, dict) and report:
                health_map[project_id] = dict(report)
        snapshot.project_health = health_map
    except Exception:  # noqa: BLE001 - defensive; probes never fail patrol
        pass

    try:
        agents_cfg = getattr(getattr(app, "config", None), "agents", None)
        snapshot.task_timeout_s = _float(
            getattr(agents_cfg, "task_timeout_s", _DEFAULT_TASK_TIMEOUT_S),
            _DEFAULT_TASK_TIMEOUT_S)
        snapshot.max_workers = _int(
            getattr(agents_cfg, "max_workers", _DEFAULT_MAX_WORKERS),
            _DEFAULT_MAX_WORKERS)
    except Exception:  # noqa: BLE001 - keep dataclass defaults
        pass
    return snapshot


# ----------------------------------------------------------------------
# check context
# ----------------------------------------------------------------------
@dataclass
class _Context:
    """Normalised snapshot + derived indexes + thresholds for one check run."""

    now: int
    health: dict
    units: list[dict]
    agents: list[dict]
    projects: list[dict]
    tasks: dict[str, list[dict]]
    approvals: list[dict]
    leases: list[dict]
    events: list[dict]
    project_health: dict[str, dict]
    active_agents: list[dict]
    active_by_project: dict[str, list[dict]]
    active_by_task: dict[str, list[dict]]
    task_idle_ms: int
    task_timeout_ms: int
    approval_age_ms: int
    failure_window_ms: int
    failure_threshold: int
    max_workers: int


def _build_context(snapshot: Any, config: Any) -> _Context:
    now = _int(_field(snapshot, "now"))
    agents = _rows(_field(snapshot, "agents"))

    active_agents: list[dict] = []
    active_by_project: dict[str, list[dict]] = {}
    active_by_task: dict[str, list[dict]] = {}
    for agent in agents:
        if _text(agent.get("status")) not in ACTIVE_AGENT_STATUSES:
            continue
        active_agents.append(agent)
        project_id = _text(agent.get("project_id"))
        task_id = _text(agent.get("task_id"))
        if project_id:
            active_by_project.setdefault(project_id, []).append(agent)
        if task_id:
            active_by_task.setdefault(task_id, []).append(agent)

    raw_tasks = _field(snapshot, "tasks")
    tasks: dict[str, list[dict]] = {}
    if isinstance(raw_tasks, dict):
        for project_id, rows in raw_tasks.items():
            clean = _rows(rows)
            if clean:
                tasks[_text(project_id)] = clean

    raw_health = _field(snapshot, "project_health")
    project_health: dict[str, dict] = {}
    if isinstance(raw_health, dict):
        for project_id, report in raw_health.items():
            if isinstance(report, dict):
                project_health[_text(project_id)] = dict(report)

    task_idle_s = max(0.0, _float(_cfg(
        config, "task_idle_s", _DEFAULT_TASK_IDLE_S), _DEFAULT_TASK_IDLE_S))
    approval_age_s = max(0.0, _float(_cfg(
        config, "approval_age_s", _DEFAULT_APPROVAL_AGE_S),
        _DEFAULT_APPROVAL_AGE_S))
    failure_window_s = max(0.0, _float(_cfg(
        config, "failure_window_s", _DEFAULT_FAILURE_WINDOW_S),
        _DEFAULT_FAILURE_WINDOW_S))
    failure_threshold = max(1, _int(_cfg(
        config, "failure_threshold", _DEFAULT_FAILURE_THRESHOLD),
        _DEFAULT_FAILURE_THRESHOLD))
    task_timeout_s = max(0.0, _float(
        _field(snapshot, "task_timeout_s", _DEFAULT_TASK_TIMEOUT_S),
        _DEFAULT_TASK_TIMEOUT_S))

    return _Context(
        now=now,
        health=_as_dict(_field(snapshot, "health")),
        units=_rows(_field(snapshot, "units")),
        agents=agents,
        projects=_rows(_field(snapshot, "projects")),
        tasks=tasks,
        approvals=_rows(_field(snapshot, "approvals")),
        leases=_rows(_field(snapshot, "leases")),
        events=_rows(_field(snapshot, "events")),
        project_health=project_health,
        active_agents=active_agents,
        active_by_project=active_by_project,
        active_by_task=active_by_task,
        task_idle_ms=int(task_idle_s * 1000),
        task_timeout_ms=int(task_timeout_s * 1000),
        approval_age_ms=int(approval_age_s * 1000),
        failure_window_ms=int(failure_window_s * 1000),
        failure_threshold=failure_threshold,
        max_workers=_int(_field(snapshot, "max_workers"), 0),
    )


# ----------------------------------------------------------------------
# checks (one function per §3.2 table row)
# ----------------------------------------------------------------------
def _check_hub_degraded(ctx: _Context) -> list[Finding]:
    """``health.status != "ok"`` is a critical hub problem."""
    if not ctx.health:
        return []
    status = _text(ctx.health.get("status") or "ok")
    if status == "ok":
        return []
    transport = _text(ctx.health.get("transport"))
    http = _text(ctx.health.get("http"))
    bits = [f"health.status={status}"]
    if transport:
        bits.append(f"transport={transport}")
    if http:
        bits.append(f"http={http}")
    return [Finding(
        check="hub.degraded",
        severity="critical",
        title="hub 健康状态降级",
        detail="，".join(bits) + "。",
        fingerprint=fingerprint("hub.degraded"),
        fix="检查 hub 进程日志与 NNG/HTTP 传输状态，恢复后本轮巡检会自动消失。",
        evidence={"status": status, "transport": transport, "http": http},
        detected_at=ctx.now,
    )]


def _check_unit_problem(ctx: _Context) -> list[Finding]:
    """Non-worker units in ``failed``/``unreachable`` are critical."""
    findings: list[Finding] = []
    for unit in ctx.units:
        if _text(unit.get("kind")) == "worker":
            continue
        state = _text(unit.get("state"))
        status = _text(unit.get("status"))
        if state not in _UNIT_PROBLEM_STATES and status != "failed":
            continue
        unit_id = _text(unit.get("unit_id") or unit.get("work_id"))
        label = unit_id or "未知单元"
        reason = _text(unit.get("reason"))
        detail = f"state={state or '?'}，status={status or '?'}"
        if reason:
            detail += f"，原因：{reason}"
        findings.append(Finding(
            check="unit.problem",
            severity="critical",
            title=f"单元 {label} 状态异常",
            detail=detail + "。",
            fingerprint=fingerprint("unit.problem", unit_id),
            unit_id=unit_id or None,
            fix="重启或重新注册该单元，并检查其进程日志。",
            evidence={"unit_id": unit_id,
                      "kind": _text(unit.get("kind")),
                      "state": state, "status": status, "reason": reason},
            detected_at=ctx.now,
        ))
    return findings


def _check_project_degraded(ctx: _Context) -> list[Finding]:
    """A project flagged ``degraded`` (or in that status) needs a human."""
    findings: list[Finding] = []
    for project in ctx.projects:
        project_id = _text(project.get("id"))
        if not project_id:
            continue
        status = _text(project.get("status"))
        degraded = bool(project.get("degraded"))
        if not degraded and status != "degraded":
            continue
        name = _text(project.get("name")) or project_id
        findings.append(Finding(
            check="project.degraded",
            severity="critical",
            title=f"项目 {name} 处于降级状态",
            detail=f"status={status or '?'}，degraded={degraded}。",
            fingerprint=fingerprint("project.degraded", project_id),
            project_id=project_id,
            fix="查看项目事件与 Agent 失败原因，修复后把项目状态改回 active。",
            evidence={"project_id": project_id, "status": status,
                      "degraded": degraded},
            detected_at=ctx.now,
        ))
    return findings


def _check_project_no_plan(ctx: _Context) -> list[Finding]:
    """An ``active`` project with no tasks and no live lead needs a plan."""
    findings: list[Finding] = []
    for project in ctx.projects:
        project_id = _text(project.get("id"))
        if not project_id or _text(project.get("status")) != "active":
            continue
        if ctx.tasks.get(project_id):
            continue
        leads = [agent for agent in ctx.active_by_project.get(project_id, [])
                 if _text(agent.get("role")) == "lead"]
        if leads:
            continue
        name = _text(project.get("name")) or project_id
        findings.append(Finding(
            check="project.no_plan",
            severity="warn",
            title=f"项目 {name} 没有计划",
            detail="项目处于 active，但任务看板为空且没有在岗 Lead。",
            fingerprint=fingerprint("project.no_plan", project_id),
            project_id=project_id,
            fix="为项目派一个 Lead 规划首批任务（或手工创建任务）。",
            evidence={"project_id": project_id, "tasks": 0,
                      "active_leads": 0, "status": "active"},
            dispatch=DispatchHint(
                kind="spawn", role="lead",
                title=f"为项目 {name} 规划首批任务",
                description="项目 active 且看板为空：先拆解目标为可执行任务。"),
            detected_at=ctx.now,
        ))
    return findings


def _check_project_health(ctx: _Context) -> list[Finding]:
    """``project_health.ready is False``: report the first blocking checks."""
    findings: list[Finding] = []
    for project_id, report in ctx.project_health.items():
        ready = report.get("ready")
        if ready not in (False, 0):  # ``None``/missing is unknown, not not_ready
            continue
        checks = [check for check in _rows(report.get("checks"))
                  if _text(check.get("status")) in ("fail", "warn")]
        fail_count = sum(1 for check in _rows(report.get("checks"))
                         if _text(check.get("status")) == "fail")
        warn_count = sum(1 for check in _rows(report.get("checks"))
                         if _text(check.get("status")) == "warn")
        next_actions = [str(action) for action in
                        (report.get("next_actions") or [])
                        if _text(action)][:3]
        highlight = [
            {"id": _text(check.get("id")),
             "label": _text(check.get("label")),
             "status": _text(check.get("status")),
             "message": _text(check.get("message"))}
            for check in checks[:3]
        ]
        name = _text(report.get("project_name")) or project_id
        bits = [f"{fail_count} 项阻断", f"{warn_count} 项告警"]
        if highlight:
            bits.append("；".join(
                f"{item['label'] or item['id']}：{item['message']}"
                for item in highlight))
        findings.append(Finding(
            check="project.health",
            severity="warn",
            title=f"项目 {name} 健康检查未就绪",
            detail="，".join(bits[:2]) + ("；" + bits[2] if len(bits) > 2 else "")
            + "。",
            fingerprint=fingerprint("project.health", project_id),
            project_id=project_id,
            fix=(next_actions[0] if next_actions else
                 "运行项目健康检查并修复下一个阻断项。"),
            evidence={"project_id": project_id,
                      "summary": _as_dict(report.get("summary")),
                      "checks": highlight,
                      "next_actions": next_actions},
            detected_at=ctx.now,
        ))
    return findings


def _task_deps_done(task: dict, by_id: dict[str, str]) -> bool:
    """True when the task has no deps, or every dep exists and is ``done``."""
    deps = task.get("deps")
    if not isinstance(deps, (list, tuple)) or not deps:
        return True
    for dep in deps:
        dep_id = _text(dep)
        if not dep_id or by_id.get(dep_id) != "done":
            return False
    return True


def _check_task_idle(ctx: _Context) -> list[Finding]:
    """A ``todo`` task with nobody on it and no blocker is dispatchable."""
    findings: list[Finding] = []
    for project_id, rows in ctx.tasks.items():
        by_id = {_text(task.get("id")): _text(task.get("status"))
                 for task in rows if _text(task.get("id"))}
        for task in rows:
            if _text(task.get("status")) != "todo":
                continue
            task_id = _text(task.get("id"))
            if not task_id:
                continue
            if ctx.active_by_task.get(task_id):
                continue
            if not _task_deps_done(task, by_id):
                continue
            stamp = max(_int(task.get("updated_at")),
                        _int(task.get("created_at")))
            if not stamp:
                continue
            idle_ms = ctx.now - stamp
            if idle_ms <= ctx.task_idle_ms:
                continue
            title = _text(task.get("title")) or task_id
            role = _text(task.get("role")) or "coder"
            findings.append(Finding(
                check="task.idle",
                severity="warn",
                title=f"任务 {task_id} 闲置超过 {_minutes(idle_ms)} 分钟",
                detail=(f"任务「{title}」处于 todo 且无人认领，已闲置 "
                        f"{_minutes(idle_ms)} 分钟（阈值 "
                        f"{_minutes(ctx.task_idle_ms)} 分钟），依赖已满足。"),
                fingerprint=fingerprint("task.idle", project_id, task_id),
                project_id=project_id or None,
                task_id=task_id,
                fix=f"派一个 {role} 认领该任务，或把它移出 todo。",
                evidence={"task_id": task_id, "idle_s": _seconds(idle_ms),
                          "threshold_s": _seconds(ctx.task_idle_ms),
                          "deps": list(task.get("deps") or []),
                          "role": role},
                dispatch=DispatchHint(kind="spawn", role=role,
                                      task_id=task_id),
                detected_at=ctx.now,
            ))
    return findings


def _check_task_stalled(ctx: _Context) -> list[Finding]:
    """A ``doing`` task past the worker wall-clock cap needs a human."""
    findings: list[Finding] = []
    for project_id, rows in ctx.tasks.items():
        for task in rows:
            if _text(task.get("status")) != "doing":
                continue
            task_id = _text(task.get("id"))
            if not task_id or ctx.active_by_task.get(task_id):
                continue
            started_at = (_int(task.get("started_at"))
                          or _int(task.get("updated_at"))
                          or _int(task.get("created_at")))
            if not started_at:
                continue
            elapsed_ms = ctx.now - started_at
            if elapsed_ms <= ctx.task_timeout_ms:
                continue
            title = _text(task.get("title")) or task_id
            findings.append(Finding(
                check="task.stalled",
                severity="warn",
                title=f"任务 {task_id} 执行超时",
                detail=(f"任务「{title}」处于 doing 已 {_minutes(elapsed_ms)} "
                        f"分钟，超过 {_minutes(ctx.task_timeout_ms)} 分钟上限，"
                        "且没有在岗 Agent。"),
                fingerprint=fingerprint("task.stalled", project_id, task_id),
                project_id=project_id or None,
                task_id=task_id,
                fix="把任务退回 todo 或重新派工；确认 worker 是否已超时退出。",
                evidence={"task_id": task_id,
                          "elapsed_s": _seconds(elapsed_ms),
                          "timeout_s": _seconds(ctx.task_timeout_ms),
                          "started_at": started_at},
                detected_at=ctx.now,
            ))
    return findings


def _check_agent_failure(ctx: _Context) -> list[Finding]:
    """Aggregate blocked/failed agents by (role, error_code) in the window."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for agent in ctx.agents:
        if _text(agent.get("status")) not in ("blocked", "failed"):
            continue
        stamp = _int(agent.get("last_seen")) or _int(agent.get("created_at"))
        if stamp and ctx.now - stamp > ctx.failure_window_ms:
            continue  # known-old record; an unknown stamp still counts
        role = _text(agent.get("role")) or "unknown"
        error_code = _text(agent.get("error_code")) or "UNKNOWN"
        groups.setdefault((role, error_code), []).append(agent)

    findings: list[Finding] = []
    for (role, error_code), rows in sorted(groups.items()):
        count = len(rows)
        critical = count >= ctx.failure_threshold
        severity = "critical" if critical else "warn"
        findings.append(Finding(
            check="agent.failure",
            severity=severity,
            title=(f"Agent 失败聚集：{role} · {error_code}" if critical else
                   f"Agent 执行失败：{role} · {error_code}"),
            detail=(f"{_minutes(ctx.failure_window_ms)} 分钟内 {count} 个 "
                    f"{role} Agent 失败（错误码 {error_code}），"
                    f"阈值 {ctx.failure_threshold}。"),
            fingerprint=fingerprint("agent.failure", role, error_code),
            fix="查看失败 Agent 的事件和日志，按 error_code 补齐前置条件或修复执行错误。",
            evidence={"role": role, "error_code": error_code,
                      "count": count,
                      "window_s": _seconds(ctx.failure_window_ms),
                      "threshold": ctx.failure_threshold,
                      "statuses": sorted({_text(row.get("status"))
                                          for row in rows})},
            detected_at=ctx.now,
        ))
    return findings


def _check_approval_pending(ctx: _Context) -> list[Finding]:
    """Pending approvals older than ``approval_age_s`` are waiting on a human."""
    candidates: list[tuple[int, dict]] = []
    for approval in ctx.approvals:
        if _text(approval.get("status")) != "pending":
            continue
        request_id = _text(approval.get("id") or approval.get("request_id"))
        created_at = _int(approval.get("created_at"))
        if not request_id or not created_at:
            continue
        age_ms = ctx.now - created_at
        if age_ms <= ctx.approval_age_ms:
            continue
        candidates.append((age_ms, approval))
    candidates.sort(key=lambda item: (-item[0], _text(item[1].get("id"))))

    findings: list[Finding] = []
    for age_ms, approval in candidates[:_MAX_PENDING_APPROVALS]:
        request_id = _text(approval.get("id") or approval.get("request_id"))
        capability = _text(approval.get("capability")) or "?"
        principal = _text(approval.get("principal")) or "?"
        risk = _text(approval.get("risk")) or "?"
        critical = age_ms > _PENDING_CRITICAL_MS
        findings.append(Finding(
            check="approval.pending",
            severity="critical" if critical else "warn",
            title=(f"审批等待超过 {_minutes(age_ms)} 分钟：{capability}"),
            detail=(f"{principal} 请求 {capability}（risk={risk}）已等待 "
                    f"{_minutes(age_ms)} 分钟。"),
            fingerprint=fingerprint("approval.pending", request_id),
            project_id=_text(approval.get("project_id")) or None,
            task_id=_text(approval.get("task_id")) or None,
            fix="在审批面板处理该请求；同类请求反复出现时再调整策略或代理审批。",
            evidence={"request_id": request_id, "capability": capability,
                      "principal": principal, "risk": risk,
                      "age_s": _seconds(age_ms)},
            detected_at=ctx.now,
        ))
    return findings


def _check_approval_denial(ctx: _Context) -> list[Finding]:
    """Repeated denials of one (capability, principal-role) hint at a rule gap."""
    groups: dict[tuple[str, str], int] = {}
    for event in ctx.events:
        event_type = _text(event.get("type"))
        data = _as_dict(event.get("data"))
        if event_type == "approval.decided":
            if _text(data.get("decision")) != "deny":
                continue
        elif event_type not in ("approval.denied", "approval.deny"):
            continue
        stamp = _int(event.get("ts"))
        if stamp and ctx.now - stamp > ctx.failure_window_ms:
            continue
        capability = _text(data.get("capability"))
        principal = _text(data.get("principal"))
        if not capability or not principal:
            request = _as_dict(data.get("request"))
            capability = capability or _text(request.get("capability"))
            principal = principal or _text(request.get("principal"))
        if not capability or not principal:
            continue
        key = (capability, _principal_prefix(principal))
        groups[key] = groups.get(key, 0) + 1

    findings: list[Finding] = []
    for (capability, prefix), count in sorted(groups.items()):
        if count < _DENIAL_THRESHOLD:
            continue
        findings.append(Finding(
            check="approval.denial",
            severity="warn",
            title=f"审批被拒聚集：{capability} · {prefix}*",
            detail=(f"{_minutes(ctx.failure_window_ms)} 分钟内 {prefix}* 的 "
                    f"{capability} 请求被拒 {count} 次，可能缺授权规则或用法有误。"),
            fingerprint=fingerprint("approval.denial", capability, prefix),
            fix="检查审批记录；请求合法就补一条 grant/规则，否则让对应 Agent 修正参数。",
            evidence={"capability": capability, "principal_prefix": prefix,
                      "count": count,
                      "window_s": _seconds(ctx.failure_window_ms),
                      "threshold": _DENIAL_THRESHOLD},
            detected_at=ctx.now,
        ))
    return findings


def _check_lease_orphan(ctx: _Context) -> list[Finding]:
    """An active lease whose holder is not a live unit/agent is orphaned."""
    holders: set[str] = set()
    for unit in ctx.units:
        for key in ("work_id", "unit_id"):
            value = _text(unit.get(key))
            if value:
                holders.add(value)
    for agent in ctx.active_agents:
        for key in ("unit_id", "id"):
            value = _text(agent.get(key))
            if value:
                holders.add(value)

    findings: list[Finding] = []
    for lease in ctx.leases:
        if _text(lease.get("status")) != "active":
            continue
        holder = _text(lease.get("holder"))
        if not holder or holder in holders:
            continue
        resource = _text(lease.get("resource"))
        findings.append(Finding(
            check="lease.orphan",
            severity="warn",
            title=f"租约持有者不在岗：{holder}",
            detail=(f"资源 {resource or '?'} 的活跃租约由 {holder} 持有，"
                    "但该单元/Agent 不在岗。"),
            fingerprint=fingerprint("lease.orphan", holder, resource),
            fix="确认持有者已退出后释放或等待租约过期；若进程仍在，检查心跳。",
            evidence={"lease_id": _text(lease.get("id")),
                      "resource": resource, "holder": holder,
                      "expires_at": _int(lease.get("expires_at"))},
            detected_at=ctx.now,
        ))
    return findings


def _check_failure_burst(ctx: _Context) -> list[Finding]:
    """A burst of unit/agent/tool failures in the window is critical."""
    counts: dict[str, int] = {}
    total = 0
    for event in ctx.events:
        event_type = _text(event.get("type"))
        if event_type not in _FAILURE_EVENT_TYPES:
            continue
        stamp = _int(event.get("ts"))
        if stamp and ctx.now - stamp > ctx.failure_window_ms:
            continue
        counts[event_type] = counts.get(event_type, 0) + 1
        total += 1
    if total < ctx.failure_threshold:
        return []
    by_type = "、".join(f"{name} {count}"
                        for name, count in sorted(counts.items()))
    return [Finding(
        check="events.failure_burst",
        severity="critical",
        title="失败事件集中爆发",
        detail=(f"{_minutes(ctx.failure_window_ms)} 分钟内失败事件 {total} 条"
                f"（阈值 {ctx.failure_threshold}）：{by_type}。"),
        fingerprint=fingerprint("events.failure_burst"),
        fix="先定位失败来源（事件流/单元日志），恢复后自治才会重新派工。",
        evidence={"window_s": _seconds(ctx.failure_window_ms),
                  "threshold": ctx.failure_threshold,
                  "total": total, "by_type": dict(sorted(counts.items()))},
        detected_at=ctx.now,
    )]


def _check_task_review(ctx: _Context) -> list[Finding]:
    """A ``review`` task whose reviewer left (or never came) is dispatchable.

    Reviews are real work: without this check a project can sit at
    ``progress=85%`` forever after the coder finished, because the only thing
    left is "wait for a human to notice the review column".
    """
    findings: list[Finding] = []
    for project_id, rows in ctx.tasks.items():
        for task in rows:
            if _text(task.get("status")) != "review":
                continue
            task_id = _text(task.get("id"))
            if not task_id or ctx.active_by_task.get(task_id):
                continue
            stamp = max(_int(task.get("updated_at")),
                        _int(task.get("created_at")))
            if not stamp:
                continue
            waiting_ms = ctx.now - stamp
            if waiting_ms <= ctx.task_idle_ms:
                continue
            title = _text(task.get("title")) or task_id
            findings.append(Finding(
                check="task.review",
                severity="warn",
                title=f"任务 {task_id} 等待评审",
                detail=(f"任务「{title}」处于 review 已 {_minutes(waiting_ms)} "
                        f"分钟，没有在岗 Agent 处理（阈值 "
                        f"{_minutes(ctx.task_idle_ms)} 分钟）。"),
                fingerprint=fingerprint("task.review", project_id, task_id),
                project_id=project_id or None,
                task_id=task_id,
                fix="派一个 reviewer 评审产物；通过则 done，不通过打回 todo。",
                evidence={"task_id": task_id, "status": "review",
                          "waiting_s": _seconds(waiting_ms),
                          "threshold_s": _seconds(ctx.task_idle_ms)},
                dispatch=DispatchHint(kind="spawn", role="reviewer",
                                      task_id=task_id),
                detected_at=ctx.now,
            ))
    return findings


def _check_task_blocked(ctx: _Context) -> list[Finding]:
    """Surface blocked tasks: they need a precondition, not another worker."""
    findings: list[Finding] = []
    for project_id, rows in ctx.tasks.items():
        for task in rows:
            if _text(task.get("status")) != "blocked":
                continue
            task_id = _text(task.get("id"))
            if not task_id:
                continue
            stamp = max(_int(task.get("updated_at")),
                        _int(task.get("created_at")))
            if stamp and ctx.now - stamp <= ctx.task_idle_ms:
                continue
            title = _text(task.get("title")) or task_id
            error = _text(task.get("error")) or "无原因"
            findings.append(Finding(
                check="task.blocked",
                severity="warn",
                title=f"任务 {task_id} 阻塞",
                detail=(f"任务「{title}」处于 blocked：{error}。"
                        "blocked 表示缺前置条件/到预算上限，不是 worker 崩溃。"),
                fingerprint=fingerprint("task.blocked", project_id, task_id),
                project_id=project_id or None,
                task_id=task_id,
                fix="按任务 error / 失败原因补前置条件，或由人决定取消该任务。",
                evidence={"task_id": task_id, "status": "blocked",
                          "error": error},
                detected_at=ctx.now,
            ))
    return findings


_CHECKS = (
    _check_hub_degraded,
    _check_unit_problem,
    _check_project_degraded,
    _check_project_no_plan,
    _check_project_health,
    _check_task_idle,
    _check_task_stalled,
    _check_task_review,
    _check_task_blocked,
    _check_agent_failure,
    _check_approval_pending,
    _check_approval_denial,
    _check_lease_orphan,
    _check_failure_burst,
)


def _sorted(findings: list[Finding]) -> list[Finding]:
    """Severity high→low, then check/project/task (stable for dedupe/report)."""
    return sorted(findings, key=lambda finding: (
        -severity_rank(finding.severity),
        finding.check,
        finding.project_id or "",
        finding.task_id or "",
        finding.agent_id or "",
        finding.unit_id or "",
        finding.fingerprint,
    ))


def run_checks(snapshot: Snapshot, config: Any) -> list[Finding]:
    """Evaluate every patrol check and return findings (sorted by severity).

    Pure: it only reads ``snapshot`` and ``config`` and never writes state.
    A check that raises is skipped (failure isolation); the rest still report.
    """
    findings: list[Finding] = []
    try:
        ctx = _build_context(snapshot, config)
    except Exception:  # noqa: BLE001 - malformed snapshot still yields []
        return findings
    for check in _CHECKS:
        try:
            findings.extend(check(ctx))
        except Exception:  # noqa: BLE001 - one broken check must not stop rest
            continue
    return _sorted(findings)


__all__ = ["Snapshot", "collect_snapshot", "run_checks"]

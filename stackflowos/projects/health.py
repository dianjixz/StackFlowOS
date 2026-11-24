"""Project health: one call that answers "is this house ready for an agent?".

Every project gets a verdict plus per-check evidence.  The checks are the
signals an agent needs before it starts: project scope, workspace path and
write boundary, the fixed layout / brief, a non-empty task board, seeded
memory, read pre-authorization, audit writability and (when applicable) the
agent's own binding.

The report is deliberately plain data so three callers can share it:

* the ``project.health`` agent tool (same checks a worker would run);
* ``GET /api/projects/{id}/healthcheck`` for the console;
* ``sfo project health <id>`` for an operator (offline, reads state only).

Statuses are ``ok`` / ``warn`` / ``fail``.  ``fail`` means *not ready*: an
agent would be blocked on a missing precondition.  ``warn`` is a signal worth
fixing but not a blocker.  ``checked=False`` marks a check that could not run
because the caller passed no live service (the offline CLI); it never turns
into a failure.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..protocol import now_ms
from . import onboarding

HEALTH_SCHEMA = 1

#: Statuses that mean "a process is alive and working" for the agents check.
_ACTIVE_AGENT_STATUSES = frozenset(
    {"queued", "spawning", "idle", "working", "waiting"})
_OPEN_TASK_STATUSES = frozenset({"todo", "doing", "review"})
_FAIL = "fail"
_WARN = "warn"
_OK = "ok"


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _check(checks: list[dict], check_id: str, label: str, status: str,
           message: str, *, fix: str | None = None,
           detail: Any = None, checked: bool = True) -> None:
    entry: dict[str, Any] = {
        "id": check_id,
        "label": label,
        "status": status,
        "message": message,
        "checked": bool(checked),
    }
    if fix:
        entry["fix"] = fix
    if detail is not None:
        entry["detail"] = detail
    checks.append(entry)


def _counts(tasks: list[Any]) -> dict[str, int]:
    out = {status: 0 for status in
           ("todo", "doing", "review", "done", "blocked")}
    for task in tasks or []:
        status = _text(_field(task, "status", ""))
        if status in out:
            out[status] += 1
    return out


def _memory_values(memory: Any, project_id: str) -> dict | None:
    if memory is None:
        return None
    hook = getattr(memory, "all", None)
    if callable(hook):
        try:
            value = hook(project_id)
        except Exception:  # noqa: BLE001 - a broken store is a warning, not a crash
            return None
        return dict(value) if isinstance(value, dict) else {}
    if isinstance(memory, dict):
        return dict(memory)
    return None


def _policy_outcome(policy: Any, capability: str, scope: dict) -> str | None:
    try:
        decision = policy.evaluate("agent-healthcheck", capability, scope)
    except Exception:  # noqa: BLE001 - health must never raise
        return None
    return _text(getattr(decision, "outcome", ""))


def _workspace_writable(workspace: Path) -> tuple[bool, str]:
    probe = workspace / f".sfo-health-{os.getpid()}"
    try:
        probe.write_text("ok", "utf-8")
        probe.unlink()
        return True, ""
    except OSError as exc:
        try:
            probe.unlink()
        except OSError:
            pass
        return False, str(exc)


def build_report(project: Any, tasks: list[Any], *, memory: Any = None,
                 policy: Any = None, agents: list[Any] | None = None,
                 agent: Any = None, security: Any = None,
                 paths: Any = None, eventlog: Any = None) -> dict:
    """Return the health verdict for one project.

    ``project``/``tasks``/``agent``/``agents`` may be dataclasses or dicts.
    ``memory`` is a store with ``all(project_id)`` (or a plain dict), ``policy``
    an object with ``evaluate(principal, capability, scope)``.
    """
    checks: list[dict] = []
    project_id = _text(_field(project, "id", ""))
    name = _text(_field(project, "name", "")) or project_id
    workspace = onboarding.workspace_path(project)
    mode = _text(_field(project, "workspace_mode", "managed")) or "managed"
    external = bool(_field(project, "external", False))
    goal = _text(_field(project, "goal", ""))
    brief_text = _text(_field(project, "brief", ""))

    # 1. scope / nameplate --------------------------------------------------
    scope_detail = {
        "project_id": project_id,
        "name": name,
        "status": _text(_field(project, "status", "")),
        "workspace": str(workspace) if workspace else "",
        "workspace_mode": mode,
        "external": external,
    }
    if not project_id:
        _check(checks, "project.scope", "项目作用域（门牌）", _FAIL,
               "项目 id 为空：Agent 无法确定 scope。",
               fix="重新创建项目；派工时必须传入 project_id。",
               detail=scope_detail)
    else:
        _check(checks, "project.scope", "项目作用域（门牌）", _OK,
               f"{project_id} · {name} · {mode}", detail=scope_detail)

    if agent is not None:
        agent_project = _text(_field(agent, "project_id", ""))
        bound = agent_project == project_id and bool(project_id)
        _check(
            checks, "agent.binding", "Agent 绑定", _OK if bound else _FAIL,
            (f"Agent {_text(_field(agent, 'id', '?'))} 已绑定本项目"
             if bound else
             f"Agent 未绑定本项目（agent.project_id={agent_project or '空'}）"),
            fix=None if bound else "派工时指定 project_id；不要创建无项目 scope 的 Agent。",
            detail={"agent_id": _text(_field(agent, "id", "")),
                    "role": _text(_field(agent, "role", "")),
                    "task_id": _field(agent, "task_id"),
                    "agent_status": _text(_field(agent, "status", ""))})

    # 2. workspace path / boundary -----------------------------------------
    if workspace is None:
        _check(checks, "workspace.path", "工作区路径", _FAIL,
               "项目没有 workspace 路径。",
               fix="补全项目工作区（托管项目自动创建；接入项目重新 attach）。")
    elif not workspace.is_absolute():
        _check(checks, "workspace.path", "工作区路径", _FAIL,
               f"workspace 不是绝对路径：{workspace}",
               fix="使用绝对路径接入或创建项目。")
    elif not workspace.is_dir():
        _check(checks, "workspace.path", "工作区路径", _FAIL,
               f"工作区不存在或不是目录：{workspace}",
               fix="恢复目录，或按正确路径重新接入项目。")
    else:
        detail = {"path": str(workspace), "mode": mode}
        if paths is not None and not external and mode == "managed":
            projects_root = Path(_field(paths, "projects_dir", ""))
            try:
                workspace.resolve().relative_to(projects_root.resolve())
            except (ValueError, OSError):
                detail["outside_projects_root"] = True
        _check(checks, "workspace.path", "工作区路径", _OK,
               f"{workspace}（{mode}）", detail=detail)

        if onboarding.writable_mode(project):
            writable, error = _workspace_writable(workspace)
            _check(checks, "workspace.write", "工作区可写", _OK if writable else _FAIL,
                   ("写入探针成功" if writable else f"写入失败：{error}"),
                   fix=None if writable else "检查目录权限/挂载；或改为只读接入。")
        else:
            _check(checks, "workspace.write", "工作区可写", _WARN,
                   "只读接入（attached-ro）：写/执行按设计被拒绝；"
                   "入职自检任务无法写 artifact，需要操作员切换为可写接入。")

    # 3. fixed layout / brief ----------------------------------------------
    scaffold = onboarding.ensure_scaffold(project, write=False)
    missing = list(scaffold.get("missing") or [])
    if missing:
        severity = _WARN if not onboarding.writable_mode(project) else _FAIL
        _check(
            checks, "workspace.layout", "目录骨架", severity,
            "缺少：" + ", ".join(missing),
            fix=("运行 `sfo project scaffold %s` 补齐（只写 workspace，不碰状态）。"
                 % project_id) if onboarding.writable_mode(project) else
                "只读接入不会写入骨架；由操作员手工创建或改为可写接入。",
            detail={"missing": missing,
                    "created": list(scaffold.get("created") or []),
                    "brief": scaffold.get("brief")})
    elif scaffold.get("brief") == "unsafe":
        _check(checks, "workspace.layout", "目录骨架", _WARN,
               "brief.md 是符号链接或非普通文件，已跳过。",
               fix="把 brief.md 换成普通文件，避免指到工作区外。")
    else:
        _check(checks, "workspace.layout", "目录骨架", _OK,
               "brief.md + tasks/ artifacts/ logs/ tmp/ 齐全",
               detail={"brief": scaffold.get("brief")})

    if not goal and brief_text:
        # A hand-written brief can carry the intent even when project.goal is
        # empty; the board check below decides whether that is enough.
        pass
    if not goal and not brief_text:
        board_open = any(_text(_field(task, "status", "")) in _OPEN_TASK_STATUSES
                         for task in tasks or [])
        _check(checks, "project.goal", "项目目标", _WARN if board_open else _FAIL,
               "项目 goal 为空，Agent 无法知道要做什么。",
               fix="在项目设置里补全 goal，并更新 brief.md。")
    elif workspace is not None and (workspace / onboarding.BRIEF_FILENAME).is_file():
        try:
            text = (workspace / onboarding.BRIEF_FILENAME).read_text("utf-8")
        except OSError:
            text = ""
        if goal and goal not in text:
            _check(checks, "project.goal", "项目目标", _WARN,
                   "brief.md 里找不到当前 goal（可能已过期）。",
                   fix="更新 brief.md，或在项目设置里重新保存 goal。")
        else:
            _check(checks, "project.goal", "项目目标", _OK,
                   "goal 与 brief.md 一致。")

    # 4. task board ---------------------------------------------------------
    counts = _counts(tasks)
    total = sum(counts.values())
    open_count = sum(counts[status] for status in _OPEN_TASK_STATUSES)
    if total == 0:
        _check(checks, "board.tasks", "任务看板", _FAIL,
               "看板为空：Agent 没有工作信号。",
               fix="创建至少一个任务（创建项目会自动写入入职自检任务）。",
               detail=counts)
    elif open_count == 0:
        _check(checks, "board.tasks", "任务看板", _WARN,
               "没有可推进的任务（全部 done/blocked）。",
               fix="补充新任务，或评审 review 任务后归档项目。",
               detail=counts)
    else:
        _check(checks, "board.tasks", "任务看板", _OK,
               f"任务 {total} 个，可推进 {open_count} 个",
               detail=counts)
    if tasks is not None and not onboarding.seed_task_present(tasks):
        _check(checks, "board.onboarding_task", "入职自检任务", _WARN,
               "没有入职自检任务；新 Agent 缺少一次完整闭环。",
               fix="POST /api/projects/{id}/onboard 或手工创建一条“入职自检”任务。")

    # 5. memory seed --------------------------------------------------------
    values = _memory_values(memory, project_id)
    if values is None:
        _check(checks, "memory.seed", "项目记忆", _WARN,
               "未连接记忆存储，跳过检查（离线模式）。",
               checked=False)
    else:
        missing_keys = [key for key in onboarding.MEMORY_KEYS if key not in values]
        if missing_keys:
            _check(checks, "memory.seed", "项目记忆", _WARN,
                   "缺少记忆键：" + ", ".join(missing_keys),
                   fix="调用 project.health/onboard 补齐，或写 memory.set。",
                   detail={"missing": missing_keys})
        else:
            _check(checks, "memory.seed", "项目记忆", _OK,
                   "goal/glossary/conventions 等初始键齐全")

    # 6. read pre-authorization --------------------------------------------
    if policy is None or workspace is None:
        _check(checks, "policy.reads", "读能力预授权", _WARN,
               "未连接策略引擎或没有 workspace，跳过检查（离线模式）。",
               checked=False)
    else:
        probes = {
            "fs.read": {"project_id": project_id,
                        "path": str(workspace / onboarding.BRIEF_FILENAME)},
            "fs.list": {"project_id": project_id, "path": str(workspace)},
            "data.read": {"project_id": project_id},
        }
        outcomes = {cap: _policy_outcome(policy, cap, scope)
                    for cap, scope in probes.items()}
        blocked = {cap: outcome for cap, outcome in outcomes.items()
                   if outcome not in ("allow", None)}
        if blocked:
            _check(checks, "policy.reads", "读能力预授权", _FAIL,
                   "读动作会被审批/拒绝：" + ", ".join(
                       f"{cap}={outcome}" for cap, outcome in blocked.items()),
                   fix=("检查 security.auto_allow_low 与 workspace 解析；"
                        "读类应预授权，否则人会被审批淹没。"),
                   detail=outcomes)
        else:
            _check(checks, "policy.reads", "读能力预授权", _OK,
                   "fs.read / fs.list / data.read 自动放行", detail=outcomes)

    # 7. write path is gated (not auto) ------------------------------------
    if policy is None or workspace is None:
        _check(checks, "policy.writes", "写能力审批", _WARN,
               "未连接策略引擎或没有 workspace，跳过检查（离线模式）。",
               checked=False)
    else:
        scope = {"project_id": project_id,
                 "path": str(workspace / "artifacts" / ".health-probe")}
        outcome = _policy_outcome(policy, "fs.write", scope)
        if outcome == "deny":
            _check(checks, "policy.writes", "写能力审批", _FAIL,
                   "fs.write 被策略拒绝，Agent 无法交付产物。",
                   fix="检查 deny 规则；写类应保持人审（medium），而不是拒绝。")
        else:
            _check(checks, "policy.writes", "写能力审批", _OK,
                   f"fs.write 风险处置：{outcome or '未知'}（预期 allow/ask）",
                   detail={"outcome": outcome, "workspace_mode": mode})

        shell_enabled = bool(getattr(security, "allow_shell", True))
        shell_outcome = _policy_outcome(
            policy, "shell.exec",
            {"project_id": project_id, "cwd": str(workspace),
             "command": "python3 -V"})
        if not shell_enabled:
            _check(checks, "policy.shell", "执行能力审批", _WARN,
                   "security.allow_shell=false：需要自检命令的角色会受限。")
        elif shell_outcome == "deny":
            _check(checks, "policy.shell", "执行能力审批", _WARN,
                   "shell.exec 被策略拒绝；coder/tester 无法自检。")
        else:
            _check(checks, "policy.shell", "执行能力审批", _OK,
                   f"shell.exec 风险处置：{shell_outcome or '未知'}（预期 ask）")

    # 8. audit / event log --------------------------------------------------
    if eventlog is not None:
        try:
            health = eventlog.health()
        except Exception:  # noqa: BLE001 - health must never raise
            health = {"writable": False}
        writable = bool(health.get("writable"))
        _check(checks, "eventlog.audit", "事件审计", _OK if writable else _FAIL,
               (f"可写，last_seq={health.get('last_seq')}"
                if writable else "事件目录不可写：动作无法留痕。"),
               fix=None if writable else "检查 state/events 权限；审计不可写时不应派工。",
               detail=health)
    elif paths is not None:
        events_dir = Path(_field(paths, "events_dir", ""))
        writable = bool(events_dir) and os.access(events_dir, os.W_OK)
        _check(checks, "eventlog.audit", "事件审计", _OK if writable else _FAIL,
               ("事件目录可写（离线检查）" if writable
                else f"事件目录不可写：{events_dir}"),
               fix=None if writable else "检查 state/events 权限。")
    else:
        _check(checks, "eventlog.audit", "事件审计", _WARN,
               "未连接事件日志，跳过检查（离线模式）。", checked=False)

    # 9. workers / agent state ---------------------------------------------
    if agents is None:
        _check(checks, "agents.active", "在岗 Agent", _WARN,
               "未连接 Supervisor，跳过检查（离线模式）。", checked=False)
    else:
        by_status: dict[str, int] = {}
        for item in agents:
            status = _text(_field(item, "status", ""))
            by_status[status] = by_status.get(status, 0) + 1
        active = sum(by_status.get(status, 0)
                     for status in _ACTIVE_AGENT_STATUSES)
        blocked_agents = [
            item for item in agents
            if _text(_field(item, "status", "")) == "blocked"]
        if blocked_agents:
            reasons = "; ".join(
                f"{_text(_field(item, 'id', '?'))}: "
                f"{_text(_field(item, 'error', '')) or '无原因'}"
                for item in blocked_agents[:3])
            _check(checks, "agents.active", "在岗 Agent", _WARN,
                   f"{len(blocked_agents)} 个 Agent 处于 blocked：{reasons}",
                   fix="按 error_code/error 补齐前置条件；blocked 不是执行错误。",
                   detail={"by_status": by_status})
        elif active == 0 and _text(_field(project, "status", "")) == "active":
            _check(checks, "agents.active", "在岗 Agent", _WARN,
                   "项目 active 但没有在岗 Agent。",
                   detail={"by_status": by_status})
        else:
            _check(checks, "agents.active", "在岗 Agent", _OK,
                   f"在岗 {active} 个", detail={"by_status": by_status})

    if agent is not None and _text(_field(agent, "status", "")) in (
            "blocked", "failed"):
        status = _text(_field(agent, "status", ""))
        _check(checks, "agent.state", "当前 Agent 状态",
               _WARN if status == "blocked" else _FAIL,
               f"Agent {status}：{_text(_field(agent, 'error', '')) or '无原因'}",
               fix=("按 error_code 补前置条件；blocked 表示缺条件/到上限，"
                    "failed 才是执行错误。"),
               detail={"status": status,
                       "error_code": _text(_field(agent, "error_code", ""))})
    elif agent is not None:
        _check(checks, "agent.state", "当前 Agent 状态", _OK,
               f"{_text(_field(agent, 'status', '?'))}")

    summary = {
        "ok": sum(1 for check in checks if check["status"] == _OK),
        "warn": sum(1 for check in checks if check["status"] == _WARN),
        "fail": sum(1 for check in checks if check["status"] == _FAIL),
        "unchecked": sum(1 for check in checks if not check["checked"]),
    }
    not_ready = [check["message"] for check in checks
                 if check["status"] == _FAIL]
    next_actions: list[str] = []
    for check in checks:
        fix = check.get("fix")
        if fix and fix not in next_actions and check["status"] != _OK:
            next_actions.append(fix)
    return {
        "schema": HEALTH_SCHEMA,
        "project_id": project_id,
        "project_name": name,
        "checked_at": now_ms(),
        "ready": not not_ready,
        "status": "ready" if not not_ready else "not_ready",
        "summary": summary,
        "checks": checks,
        "not_ready_reasons": not_ready,
        "next_actions": next_actions,
    }


__all__ = ["HEALTH_SCHEMA", "build_report"]

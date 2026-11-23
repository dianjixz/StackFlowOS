"""Operator tools for the chat assistant.

The assistant is the human's proxy: commands typed into the chat are operator
intent, so they execute directly instead of parking in the approval queue.
Every call is appended to the event log as ``operator.action`` (arguments are
redacted/truncated), so "the assistant has root" never means "unaudited".

Worker agents keep the approval gate: only the in-hub assistant bypasses it.
This module deliberately depends on ``app`` only by duck typing to avoid a
circular import with :mod:`stackflowos.app`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..errors import BadRequest, SfoError
from ..util.logging import get_logger


@dataclass(frozen=True)
class OperatorTool:
    name: str
    capability: str
    description: str
    args: dict


#: The closed set of operations the assistant may perform. If it is not here,
#: the assistant cannot invent it. Keep names stable: the model prompt embeds
#: this catalogue.
OPERATOR_TOOLS: dict[str, OperatorTool] = {
    "sys.status": OperatorTool(
        "sys.status", "data.read", "查看系统/项目/审批/单元摘要", {}),
    "project.list": OperatorTool(
        "project.list", "data.read", "列出项目卡片", {}),
    "project.get": OperatorTool(
        "project.get", "data.read", "读取一个项目", {"project_id": "string"}),
    "project.create": OperatorTool(
        "project.create", "project.create", "创建项目（系统分配工作区）",
        {"name": "string", "goal": "string"}),
    "project.attach": OperatorTool(
        "project.attach", "project.attach",
        "接入已存在的本地目录作为项目（默认只读，不复制/删除该目录；"
        "confirm 必须是目录路径或项目名，由用户提供）",
        {"name": "string?", "path": "string", "goal": "string?",
         "allow_writes": "boolean?", "confirm": "string"}),
    "project.access": OperatorTool(
        "project.access", "project.attach",
        "切换接入目录的读写模式（只读/可写；切可写时 confirm=true）",
        {"project_id": "string", "allow_writes": "boolean",
         "confirm": "boolean?"}),
    "project.update": OperatorTool(
        "project.update", "project.update", "更新项目字段",
        {"project_id": "string", "patch": "object"}),
    "project.archive": OperatorTool(
        "project.archive", "project.archive", "归档项目并停止其 Agent",
        {"project_id": "string"}),
    "project.delete": OperatorTool(
        "project.delete", "project.delete",
        "永久删除项目、任务与工作区（confirm 必须是项目名，由用户提供）",
        {"project_id": "string", "purge_workspace": "boolean?",
         "confirm": "string"}),
    "project.complete": OperatorTool(
        "project.complete", "project.update", "人工确认项目完成",
        {"project_id": "string"}),
    "project.files": OperatorTool(
        "project.files", "data.read", "列出项目工作区文件",
        {"project_id": "string", "path": "string?"}),
    "project.read": OperatorTool(
        "project.read", "data.read", "读取项目工作区文本文件",
        {"project_id": "string", "path": "string",
         "max_bytes": "integer?"}),
    "task.list": OperatorTool(
        "task.list", "data.read", "列出任务",
        {"project_id": "string?"}),
    "task.create": OperatorTool(
        "task.create", "task.manage", "创建任务",
        {"project_id": "string", "title": "string", "description": "string?",
         "role": "string?", "priority": "integer?"}),
    "task.update": OperatorTool(
        "task.update", "task.manage", "更新任务字段/状态",
        {"task_id": "string", "patch": "object"}),
    "task.note": OperatorTool(
        "task.note", "task.manage", "给任务追加备注",
        {"task_id": "string", "text": "string"}),
    "task.check": OperatorTool(
        "task.check", "task.manage", "勾选/新增任务验收项",
        {"task_id": "string", "text": "string?", "item_id": "string?",
         "done": "boolean?"}),
    "agent.list": OperatorTool(
        "agent.list", "data.read", "列出 Agent", {"project_id": "string?"}),
    "agent.spawn": OperatorTool(
        "agent.spawn", "agent.spawn", "派工/拉起 Agent",
        {"role": "string", "project_id": "string?", "task_id": "string?"}),
    "agent.kill": OperatorTool(
        "agent.kill", "agent.kill", "终止 Agent", {"agent_id": "string"}),
    "unit.list": OperatorTool(
        "unit.list", "data.read", "列出单元进程", {}),
    "unit.kill": OperatorTool(
        "unit.kill", "unit.control", "终止一个单元", {"work_id": "string"}),
    "approval.list": OperatorTool(
        "approval.list", "data.read", "列出审批",
        {"status": "string?", "project_id": "string?"}),
    "approval.decide": OperatorTool(
        "approval.decide", "approval.decide",
        "放行/拒绝审批（也接受 approval.allow / approval.deny；"
        "confirm 必须是 request_id，由用户提供）",
        {"request_id": "string", "decision": "allow|deny",
         "mode": "once|session|always?", "note": "string?",
         "confirm": "string"}),
    "rule.list": OperatorTool(
        "rule.list", "data.read", "列出放行规则", {}),
    "rule.create": OperatorTool(
        "rule.create", "sys.config", "新增放行/拒绝规则",
        {"effect": "allow|deny", "capability": "string", "principal": "string?",
         "scope": "object?", "risk_max": "string?", "note": "string?"}),
    "rule.delete": OperatorTool(
        "rule.delete", "sys.config", "删除规则", {"rule_id": "string"}),
    "rule.update": OperatorTool(
        "rule.update", "sys.config", "更新规则",
        {"rule_id": "string", "patch": "object"}),
    "config.get": OperatorTool(
        "config.get", "sys.config", "读取系统配置（密钥脱敏）", {}),
    "config.set": OperatorTool(
        "config.set", "sys.config", "修改系统配置",
        {"patch": "object"}),
    "lease.list": OperatorTool(
        "lease.list", "data.read", "列出活跃租约", {}),
    "lease.release": OperatorTool(
        "lease.release", "unit.control", "释放一个租约",
        {"lease_id": "string", "holder": "string?"}),
    "memory.get": OperatorTool(
        "memory.get", "data.read", "读取项目记忆",
        {"project_id": "string", "key": "string"}),
    "memory.set": OperatorTool(
        "memory.set", "data.write", "写入项目记忆",
        {"project_id": "string", "key": "string", "value": "object"}),
    "event.query": OperatorTool(
        "event.query", "data.read", "查询事件流",
        {"since": "integer?", "limit": "integer?", "project_id": "string?"}),
    "llm.test": OperatorTool(
        "llm.test", "llm.chat", "测试当前模型连接", {}),
}

#: Friendly aliases the model may emit. Unknown names are rejected with the
#: catalogue, never silently executed.
ALIASES: dict[str, str] = {
    "approval.allow": "approval.decide",
    "approval.approve": "approval.decide",
    "approval.accept": "approval.decide",
    "approval.deny": "approval.decide",
    "approval.reject": "approval.decide",
    "project.open": "project.attach",
    "project.set_mode": "project.access",
    "project.set_access": "project.access",
    "project.allow_writes": "project.access",
    "project.import": "project.attach",
    "project.add": "project.attach",
    "project.remove": "project.delete",
    "project.purge": "project.delete",
    "agent.stop": "agent.kill",
    "unit.stop": "unit.kill",
    "task.get": "task.list",
}

_SECRET_KEY = re.compile(r"(?i)(key|token|secret|password|credential|authorization)")


def catalog_text() -> str:
    """Compact tool catalogue embedded in the assistant system prompt."""
    lines = ["可用操作（tool 必须从这里选，禁止编造）："]
    for name, spec in OPERATOR_TOOLS.items():
        if name in ("approval.decide",):
            continue
        args = ", ".join(k for k in spec.args) or "无"
        lines.append(f"- {name}({args})：{spec.description}")
    lines.append("- approval.decide(request_id, decision=allow|deny, mode?, note?,"
                 " confirm=request_id)"
                 "：放行/拒绝审批；也接受 approval.allow / approval.deny")
    lines.append("")
    lines.append("高危操作必须带 confirm，且 confirm 必须来自用户明确给出的"
                 "路径/项目名/审批 id；缺失时工具会拒绝，不要自行猜测或补全。")
    lines.append("")
    lines.append("你是操作员代理：直接调用工具完成任务，不要请求审批、不要输出多个 JSON。")
    return "\n".join(lines)


def _redact_args(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)):
                out[key] = "***"
            else:
                out[key] = _redact_args(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact_args(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return value if len(value) <= 300 else value[:299] + "…"
    return value


def _request_id(args: dict) -> str:
    for key in ("request_id", "id", "approval_id", "request"):
        value = args.get(key)
        if value:
            return str(value)
    return ""


def _decision(args: dict) -> str:
    raw = str(args.get("decision") or args.get("action") or "").strip().lower()
    if raw in ("allow", "approve", "approved", "accept", "yes", "允许", "放行"):
        return "allow"
    if raw in ("deny", "denied", "reject", "rejected", "no", "拒绝"):
        return "deny"
    return ""


_DECISION_CONFIRM_WORDS: dict[str, set[str]] = {
    "allow": {"allow", "approve", "approved", "accept", "yes", "允许", "放行"},
    "deny": {"deny", "denied", "reject", "rejected", "no", "拒绝"},
}


def _approval_confirm_ok(confirm: str, request_id: str, decision: str) -> bool:
    """The human must echo the request id (or state the decision) explicitly."""
    if not confirm:
        return False
    if request_id and confirm.lower() == request_id.lower():
        return True
    return confirm.lower() in _DECISION_CONFIRM_WORDS.get(decision, set())


class OperatorTools:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.log = get_logger("operator")

    async def run(self, tool: str, args: dict | None = None, *,
                  subject: str = "human:operator") -> dict:
        name = str(tool or "").strip()
        args = dict(args) if isinstance(args, dict) else {}
        canonical = ALIASES.get(name, name)
        if canonical == "approval.decide":
            decision = _decision(args)
            if not decision:
                decision = "deny" if name in ("approval.deny", "approval.reject") else "allow"
            args["decision"] = decision
            args["request_id"] = _request_id(args)
        spec = OPERATOR_TOOLS.get(canonical)
        if spec is None:
            supported = sorted(OPERATOR_TOOLS) + sorted(ALIASES)
            self._audit(name, None, args, ok=False, subject=subject,
                        error=f"unknown tool: {name}")
            return {
                "ok": False,
                "code": "UNKNOWN_TOOL",
                "error": f"没有这个操作工具：{name}",
                "supported": supported,
            }
        handler = getattr(self, "_" + canonical.replace(".", "_"), None)
        if handler is None:  # pragma: no cover - catalogue/handler mismatch
            return {"ok": False, "error": f"没有操作实现：{canonical}"}
        try:
            data = await handler(args)
        except SfoError as exc:
            self._audit(canonical, spec, args, ok=False, subject=subject,
                        error=f"{exc.code}: {exc.message}")
            return {"ok": False, "code": exc.code,
                    "error": f"{exc.code}: {exc.message}",
                    "detail": exc.detail}
        except Exception as exc:  # noqa: BLE001 - the chat must not die
            self.log.exception("operator tool %s failed", canonical)
            self._audit(canonical, spec, args, ok=False, subject=subject,
                        error=f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(data, dict):
            data = {"value": data}
        self._audit(canonical, spec, args, ok=True, subject=subject, data=data)
        return {"ok": True, "tool": canonical, "data": data}

    # -- audit -----------------------------------------------------------
    def _audit(self, requested: str, spec: OperatorTool | None, args: dict, *,
               ok: bool, subject: str, data: dict | None = None,
               error: str | None = None) -> None:
        try:
            payload = {
                "tool": requested,
                "capability": spec.capability if spec else None,
                "subject": subject,
                "args": _redact_args(args),
                "ok": bool(ok),
            }
            if error:
                payload["error"] = error[:500]
            if data:
                summary = json.dumps(data, ensure_ascii=False, default=str)
                payload["result"] = summary[:800]
            project_id = args.get("project_id")
            self.app.eventlog.append("operator.action", payload,
                                     source="agent-assistant",
                                     project_id=str(project_id) if project_id else None)
        except Exception:
            self.log.debug("operator audit failed", exc_info=True)

    # -- handlers --------------------------------------------------------
    async def _sys_status(self, args: dict) -> dict:
        app = self.app
        projects = [{"id": p.id, "name": p.name, "status": p.status,
                     "progress": app.project_card(p.id).get("progress", 0)}
                    for p in app.projects.list()]
        return {
            "summary": app.state_summary(),
            "stats": app.stats(),
            "projects": projects,
            "pending_approvals": [r.to_dict() for r in
                                  app.approvals.list(status="pending", limit=50)],
            "agents": [a.to_dict() for a in app.supervisor.list()],
        }

    async def _project_list(self, args: dict) -> dict:
        return {"projects": [self.app.project_card(p.id)
                             for p in self.app.projects.list()]}

    async def _project_get(self, args: dict) -> dict:
        return self.app.project_card(str(args.get("project_id") or ""))

    async def _project_create(self, args: dict) -> dict:
        name = str(args.get("name") or "").strip()
        goal = str(args.get("goal") or "").strip()
        if not name:
            raise BadRequest("project name required")
        return await self.app.create_project(name, goal or name,
                                             created_by="agent-assistant")

    async def _project_attach(self, args: dict) -> dict:
        path = str(args.get("path") or "").strip()
        name = str(args.get("name") or "").strip()
        confirm = str(args.get("confirm") or "").strip()
        # Attaching widens the system's reach to an arbitrary host directory;
        # the human must echo the path (or the project name) in the same call.
        if not confirm or confirm not in (path, name):
            raise BadRequest(
                "project.attach requires confirm=<path>",
                detail={"path": path or None, "name": name or None})
        return await self.app.attach_project(
            name, path,
            goal=str(args.get("goal") or ""),
            allow_writes=bool(args.get("allow_writes", False)),
            created_by="agent-assistant")

    async def _project_update(self, args: dict) -> dict:
        project_id = str(args.get("project_id") or "")
        patch = args.get("patch")
        if not isinstance(patch, dict):
            raise BadRequest("project.update requires patch object")
        project = self.app.projects.update(project_id, patch)
        return self.app.project_card(project.id)

    async def _project_access(self, args: dict) -> dict:
        allow = bool(args.get("allow_writes", False))
        if allow and args.get("confirm") is not True:
            # Read-only is safe; enabling writes on an attached host directory
            # needs a second, explicit confirmation in the same call.
            raise BadRequest(
                "project.access requires confirm=true to enable writes",
                detail={"project_id": str(args.get("project_id") or "")})
        return await self.app.set_project_access(
            str(args.get("project_id") or ""), allow_writes=allow,
            by="agent-assistant")

    async def _project_archive(self, args: dict) -> dict:
        return await self.app.archive_project(
            str(args.get("project_id") or ""), by="agent-assistant")

    async def _project_delete(self, args: dict) -> dict:
        project_id = str(args.get("project_id") or "")
        target = self.app.projects.get(project_id)
        confirm = str(args.get("confirm") or "").strip()
        # The human must type the project name; never fill it from the record.
        if confirm not in (target.name, target.id):
            raise BadRequest(
                "project deletion requires confirm=<project name>",
                detail={"project_id": project_id, "name": target.name})
        purge = args.get("purge_workspace", True)
        if not isinstance(purge, bool):
            purge = True
        return await self.app.delete_project(
            project_id, purge_workspace=purge, confirm=confirm,
            by="agent-assistant")

    async def _project_complete(self, args: dict) -> dict:
        return await self.app.complete_project(str(args.get("project_id") or ""))

    async def _project_files(self, args: dict) -> dict:
        from pathlib import Path

        project = self.app.projects.get(str(args.get("project_id") or ""))
        root = Path(project.workspace).resolve()
        relative = str(args.get("path") or ".").strip() or "."
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            raise BadRequest("path escapes project workspace")
        if not target.exists():
            raise BadRequest(f"path does not exist: {relative}")
        entries = []
        for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
            try:
                stat = item.stat()
            except OSError:
                continue
            if self._denied_path(item.resolve()):
                continue
            entries.append({
                "name": item.name,
                "type": "file" if item.is_file() else "dir",
                "size": stat.st_size if item.is_file() else None,
            })
        return {"path": relative, "entries": entries[:500]}

    def _denied_path(self, target) -> bool:
        from pathlib import Path

        state = Path(self.app.paths.state_dir).resolve()
        target = Path(target).resolve()
        if target == state:
            return True
        if state in target.parents:
            # Managed workspaces live under <state>/projects and must stay
            # readable; everything else inside the state dir is private.
            projects = (state / "projects").resolve()
            return not (target == projects or projects in target.parents)
        for name in ("config.json", "events", "logs", "run", "tmp", "state"):
            denied = (state / name).resolve()
            if target == denied or denied in target.parents:
                return True
        return False

    async def _project_read(self, args: dict) -> dict:
        from pathlib import Path

        project = self.app.projects.get(str(args.get("project_id") or ""))
        root = Path(project.workspace).resolve()
        relative = str(args.get("path") or "").strip()
        if not relative:
            raise BadRequest("path required")
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            raise BadRequest("path escapes project workspace")
        if not target.is_file():
            raise BadRequest(f"not a file: {relative}")
        if self._denied_path(target):
            raise BadRequest("path is not readable (StackFlowOS state)")
        limit = min(int(args.get("max_bytes") or 200_000), 2_000_000)
        raw = target.read_bytes()[:limit]
        return {"path": relative, "content": raw.decode("utf-8", "replace"),
                "truncated": target.stat().st_size > limit}

    async def _task_list(self, args: dict) -> dict:
        tasks = self.app.projects.task_list(
            project_id=args.get("project_id") or None,
            status=args.get("status") or None)
        return {"tasks": [t.to_dict() for t in tasks]}

    async def _task_create(self, args: dict) -> dict:
        task = self.app.projects.add_task(
            str(args.get("project_id") or ""),
            str(args.get("title") or ""),
            str(args.get("description") or ""),
            role=args.get("role"),
            priority=int(args.get("priority") or 0),
            created_by="agent-assistant",
        )
        return task.to_dict()

    async def _task_update(self, args: dict) -> dict:
        patch = args.get("patch")
        if not isinstance(patch, dict):
            raise BadRequest("task.update requires patch object")
        return self.app.projects.task_update(
            str(args.get("task_id") or ""), patch).to_dict()

    async def _task_note(self, args: dict) -> dict:
        return self.app.projects.task_add_note(
            str(args.get("task_id") or ""),
            str(args.get("text") or ""),
            author="agent-assistant").to_dict()

    async def _task_check(self, args: dict) -> dict:
        task = self.app.projects.task_check(
            str(args.get("task_id") or ""),
            item_id=args.get("item_id"),
            text=args.get("text"),
            done=bool(args.get("done", True)),
            note=str(args.get("note") or ""))
        return task.to_dict()

    async def _agent_list(self, args: dict) -> dict:
        return {"agents": [a.to_dict() for a in self.app.supervisor.list(
            project_id=args.get("project_id") or None,
            status=args.get("status") or None)]}

    async def _agent_spawn(self, args: dict) -> dict:
        return await self.app.spawn_agent(
            str(args.get("role") or ""), args.get("project_id") or None,
            args.get("task_id") or None)

    async def _agent_kill(self, args: dict) -> dict:
        return {"ok": await self.app.kill_agent(str(args.get("agent_id") or ""))}

    async def _unit_list(self, args: dict) -> dict:
        return {"units": [u.to_dict() for u in self.app.registry.list()]}

    async def _unit_kill(self, args: dict) -> dict:
        return await self.app.kill_unit(str(args.get("work_id") or ""))

    async def _approval_list(self, args: dict) -> dict:
        items = self.app.approvals.list(
            status=args.get("status") or None,
            project_id=args.get("project_id") or None, limit=200)
        return {"approvals": [a.to_dict() for a in items]}

    async def _approval_decide(self, args: dict) -> dict:
        request_id = _request_id(args)
        decision = _decision(args)
        if not request_id:
            raise BadRequest("approval.decide requires request_id")
        if not decision:
            raise BadRequest("approval.decide requires decision=allow|deny")
        confirm = str(args.get("confirm") or "").strip()
        if not _approval_confirm_ok(confirm, request_id, decision):
            raise BadRequest(
                "approval.decide requires confirm=<request_id>",
                detail={"request_id": request_id})
        mode = str(args.get("mode") or "once")
        if mode not in ("once", "session", "always"):
            mode = "once"
        request = await self.app.decide_approval(
            request_id, decision=decision, mode=mode,
            note=str(args.get("note") or "decided by assistant"),
            by="agent-assistant")
        return request

    async def _rule_list(self, args: dict) -> dict:
        return {"rules": [r.to_dict()
                          for r in self.app.policy.list_rules(active_only=False)]}

    async def _rule_create(self, args: dict) -> dict:
        rule = self.app.policy.add_rule(
            effect=str(args.get("effect") or "allow"),
            capability=str(args.get("capability") or ""),
            principal=str(args.get("principal") or "*"),
            scope=dict(args.get("scope") or {}),
            risk_max=str(args.get("risk_max") or "high"),
            expires_at=args.get("expires_at"),
            uses_left=args.get("uses_left"),
            note=str(args.get("note") or ""),
            created_by="agent-assistant",
        )
        return rule.to_dict()

    async def _rule_delete(self, args: dict) -> dict:
        return {"deleted": self.app.policy.delete_rule(
            str(args.get("rule_id") or args.get("id") or ""))}

    async def _config_get(self, args: dict) -> dict:
        return self.app.config.public_dict()

    async def _config_set(self, args: dict) -> dict:
        patch = args.get("patch")
        if not isinstance(patch, dict):
            patch = {k: v for k, v in args.items() if k != "tool"}
        if not patch:
            raise BadRequest("config.set requires a patch object")
        return await self.app.update_config(patch)

    async def _lease_list(self, args: dict) -> dict:
        return {"leases": [l.to_dict()
                           for l in self.app.leases.list(active_only=True)]}

    async def _lease_release(self, args: dict) -> dict:
        lease = self.app.leases.release(
            str(args.get("lease_id") or ""),
            holder=args.get("holder") or None,
            reason="operator_requested")
        return lease.to_dict()

    async def _memory_get(self, args: dict) -> dict:
        return {"value": self.app.memory.get(
            str(args.get("project_id") or ""), str(args.get("key") or ""))}

    async def _memory_set(self, args: dict) -> dict:
        project_id = str(args.get("project_id") or "")
        key = str(args.get("key") or "")
        value = self.app.memory.set(project_id, key, args.get("value"))
        self.app.eventlog.append("memory.updated", {
            "project_id": project_id, "key": key,
        }, source="agent-assistant", project_id=project_id)
        return {"project_id": project_id, "key": key, "value": value}

    async def _rule_update(self, args: dict) -> dict:
        patch = args.get("patch")
        if not isinstance(patch, dict):
            raise BadRequest("rule.update requires patch object")
        return self.app.policy.update_rule(
            str(args.get("rule_id") or args.get("id") or ""), patch).to_dict()

    async def _event_query(self, args: dict) -> dict:
        records = self.app.eventlog.since(
            int(args.get("since") or 0),
            limit=int(args.get("limit") or 100),
            project_id=args.get("project_id") or None)
        return {"events": [r.to_dict() for r in records]}

    async def _llm_test(self, args: dict) -> dict:
        provider = getattr(self.app, "provider", None)
        if provider is None:
            raise BadRequest("no llm provider")
        result = await provider.ping()
        return {**result, "provider": getattr(provider, "name", ""),
                "model": getattr(provider, "model", "")}


__all__ = ["OPERATOR_TOOLS", "ALIASES", "OperatorTools", "catalog_text"]

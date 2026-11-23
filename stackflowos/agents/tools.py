"""Tool registry and local tool execution for agent workers.

Design contract (docs/architecture.md §6.4):

    resolve scope -> risk -> sys.approval.request -> poll -> verify grant
    -> execute (re-check path/cwd) -> approval.use -> tool.result event

The executor runs *inside the worker process*; sys only grants capabilities and
audits.  It never imports ``security``/``l2``/``projects``: the security config
is injected, and the policy decision is always taken by sys (``request_permission``)
so a local bug cannot widen permissions.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ..errors import BadRequest, Denied, SfoError
from ..util.logging import get_logger
from .api import call_ok, emit_event, request_permission
from .roles import get_role

#: Per-stream cap for captured shell output; the full text goes to a log file.
OUTPUT_CAP_BYTES = 20 * 1024
#: Default cap for fs.read / net.fetch payloads.
DEFAULT_MAX_BYTES = 200_000
#: Hard cap for one read/fetch, protects the process memory.
MAX_READ_BYTES = 2_000_000
#: Outbound HTTP timeout (seconds).
NET_TIMEOUT_S = 15.0
#: Longest single ``wait`` tool call (seconds).
MAX_WAIT_S = 60.0

_ENV_SECRET = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)
_SECRET_TEXT = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|credential|authorization)\b"
    r"(\s*[=:]\s*)([^\s,;]+)")


@dataclass(frozen=True)
class ToolSpec:
    """Static description of one tool (name, capability, args schema)."""

    name: str
    capability: str
    description: str
    args: dict
    risk_hint: str = "low"


class ToolError(Exception):
    """A tool failed for a reason the executor should report, not crash on."""


@dataclass
class ToolResult:
    """Outcome of one tool call as seen by the brain/runner."""

    ok: bool
    data: dict = field(default_factory=dict)
    error: str | None = None
    preview: str = ""


def _spec(name: str, capability: str, description: str, args: dict,
          risk: str = "low") -> ToolSpec:
    return ToolSpec(name=name, capability=capability, description=description,
                    args=args, risk_hint=risk)


TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in (
    _spec("fs.read", "fs.read", "读取 workspace 内的文本文件",
          {"path": "string", "max_bytes": "integer"}),
    _spec("fs.list", "fs.list", "列出 workspace 内的目录",
          {"path": "string"}),
    _spec("fs.write", "fs.write", "写入 workspace 内的文件（自动建父目录）",
          {"path": "string", "content": "string"}, "medium"),
    _spec("fs.delete", "fs.delete", "删除 workspace 内的文件或目录",
          {"path": "string"}, "high"),
    _spec("shell.exec", "shell.exec", "在 workspace 内执行 shell 命令",
          {"command": "string", "cwd": "string", "timeout_s": "number"}, "high"),
    _spec("net.fetch", "net.fetch", "抓取 http/https 网页",
          {"url": "string", "max_bytes": "integer"}, "medium"),
    _spec("task.update", "task.manage",
          "更新任务（task_id 默认自己的任务；patch 可含 status=todo|doing|review|done|blocked、"
          "title、description、error）",
          {"task_id": "string", "patch": "object"}),
    _spec("task.artifact", "task.manage",
          "为任务登记产物（artifact 如 {\"type\":\"file\",\"path\":\"src/a.py\",\"title\":\"...\"}）",
          {"task_id": "string", "artifact": "object"}),
    _spec("task.note", "task.manage", "给任务追加一条备注",
          {"task_id": "string", "text": "string"}),
    _spec("task.create", "task.manage", "在项目中创建任务",
          {"project_id": "string", "title": "string", "description": "string",
           "role": "string", "priority": "integer"}),
    _spec("task.list", "data.read", "列出项目任务",
          {"project_id": "string"}),
    _spec("project.get", "data.read", "读取项目",
          {"project_id": "string"}),
    _spec("project.update", "project.update", "更新项目字段（如 brief）",
          {"project_id": "string", "patch": "object"}, "medium"),
    _spec("project.archive", "project.archive", "归档项目并停止其 Agent（保留工作区）",
          {"project_id": "string"}, "medium"),
    _spec("project.delete", "project.delete", "永久删除项目记录与工作区（不可恢复）",
          {"project_id": "string", "purge_workspace": "boolean",
           "confirm": "string"}, "high"),
    _spec("agent.spawn", "agent.spawn", "按角色拉起一个工人",
          {"role": "string", "project_id": "string", "task_id": "string",
           "reason": "string"}, "medium"),
    _spec("agent.kill", "agent.kill", "杀死一个工人",
          {"agent_id": "string", "reason": "string"}, "high"),
    _spec("agent.list", "data.read", "列出 agent",
          {"project_id": "string"}),
    _spec("event.query", "data.read", "按 seq 查询事件",
          {"project_id": "string", "since": "integer", "limit": "integer"}),
    _spec("memory.get", "data.read", "读取项目记忆",
          {"project_id": "string", "key": "string"}),
    _spec("memory.set", "data.write", "写入项目记忆",
          {"project_id": "string", "key": "string", "value": "object"}, "medium"),
    _spec("unit.list", "data.read", "列出在线单元", {}),
    _spec("wait", "data.read",
          "等待若干秒后返回最新任务看板（派工后等待工人推进时使用）",
          {"seconds": "number"}),
)}


def get_tool(name: str) -> ToolSpec:
    """Return a tool spec or raise :class:`BadRequest` for unknown names."""
    spec = TOOLS.get(str(name))
    if spec is None:
        raise BadRequest(f"unknown tool: {name!r}",
                         detail={"tools": sorted(TOOLS)})
    return spec


def tools_for(role: str) -> list[ToolSpec]:
    """Tools granted to ``role`` (unknown role -> empty list)."""
    try:
        definition = get_role(role)
    except BadRequest:
        return []
    return [TOOLS[name] for name in definition.tools if name in TOOLS]


# ---------------------------------------------------------------------------
# argument helpers
# ---------------------------------------------------------------------------

def _arg_str(args: dict, key: str, default: str | None = None, *,
             required: bool = False) -> str:
    value = args.get(key, default)
    if value is None:
        if required:
            raise ToolError(f"missing required argument: {key}")
        raise ToolError(f"argument {key!r} is required")
    if not isinstance(value, str):
        raise ToolError(f"argument {key!r} must be a string")
    if not value:
        raise ToolError(f"argument {key!r} must not be empty")
    return value


def _arg_optional_str(args: dict, key: str, default: str | None = None) -> str | None:
    value = args.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f"argument {key!r} must be a string")
    return value


def _arg_int(args: dict, key: str, default: int, *, minimum: int = 0,
             maximum: int | None = None) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"argument {key!r} must be an integer")
    number = int(value)
    if number < minimum:
        raise ToolError(f"argument {key!r} must be >= {minimum}")
    if maximum is not None:
        number = min(number, maximum)
    return number


def _arg_float(args: dict, key: str, default: float) -> float:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"argument {key!r} must be a number")
    return float(value)


def _arg_dict(args: dict, key: str, default: dict | None = None) -> dict:
    value = args.get(key, default)
    if not isinstance(value, dict):
        raise ToolError(f"argument {key!r} must be an object")
    return value


def _board(payload: Any) -> list[dict]:
    """Compact task board rows from a ``task.list`` reply."""
    rows = payload
    if isinstance(payload, dict):
        rows = payload.get("tasks", payload.get("value", []))
    out = []
    for item in rows if isinstance(rows, list) else []:
        if isinstance(item, dict):
            out.append({key: item.get(key) for key in
                        ("id", "title", "status", "role", "assignee")})
    return out


def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _redact(text: str) -> str:
    """Best-effort removal of inline credentials from human previews."""
    return _SECRET_TEXT.sub(lambda match: f"{match.group(1)}{match.group(2)}***", text)


class ToolExecutor:
    """Execute one tool call with approval, path confinement and auditing."""

    def __init__(self, sys: Any, *, agent: Any, workspace: Path,
                 security_config: Any, logger: Any = None,
                 event_source: str | None = None,
                 read_only: bool = False,
                 deny_paths: list[Path] | None = None) -> None:
        self.sys = sys
        self.agent = agent
        self.workspace = Path(workspace).resolve()
        self.read_only = bool(read_only)
        self.deny_paths = [Path(item).resolve() for item in (deny_paths or [])]
        self.config = security_config
        self.log = logger or get_logger("agents.tools")
        self.source = event_source or str(getattr(agent, "id", "agent"))
        self.max_preview = int(self._cfg("max_preview_chars", 4000))
        #: Every invoked tool name, useful for tests and diagnostics.
        self.calls: list[str] = []

    # -- config / identity ------------------------------------------------
    def _cfg(self, name: str, default: Any) -> Any:
        return getattr(self.config, name, default)

    @property
    def agent_id(self) -> str:
        return str(getattr(self.agent, "id", "agent"))

    @property
    def project_id(self) -> str | None:
        return getattr(self.agent, "project_id", None)

    @property
    def task_id(self) -> str | None:
        return getattr(self.agent, "task_id", None)

    def _task_id_arg(self, args: dict) -> str:
        """``task_id`` argument, defaulting to the agent's own task."""
        task_id = _arg_optional_str(args, "task_id") or self.task_id
        if not task_id:
            raise ToolError("argument 'task_id' is required (agent has no task)")
        return task_id

    # -- path confinement -------------------------------------------------
    def check_path(self, path: str | Path, *, must_exist: bool = False) -> Path:
        """Resolve ``path`` inside the workspace or raise :class:`ToolError`.

        Symlinks are resolved before the containment check, so ``..`` and
        symlink escapes are both rejected.  This runs before any RPC.
        """
        raw = str(path)
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise ToolError(f"cannot resolve path {raw!r}: {exc}") from exc
        try:
            resolved.relative_to(self.workspace)
        except ValueError:
            raise ToolError(f"path escapes workspace: {raw!r}") from None
        for denied in self.deny_paths:
            if resolved == denied or denied in resolved.parents:
                raise ToolError(f"path is not readable: {raw!r}")
        if must_exist and not resolved.exists():
            raise ToolError(f"path does not exist: {raw!r}")
        return resolved

    # -- public entry point ----------------------------------------------
    async def run(self, name: str, args: dict | None = None) -> ToolResult:
        """Execute a tool call.  Unknown tool names raise :class:`ToolError`."""
        try:
            spec = get_tool(name)
        except BadRequest as exc:
            raise ToolError(f"unknown tool: {name!r}") from exc
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return await self._fail(spec, "arguments must be an object",
                                    preview="")
        self.calls.append(spec.name)

        try:
            scope, reason, preview = self._prepare(spec.name, args)
        except ToolError as exc:
            # Invalid invocation: nothing was requested from sys and no side
            # effect happened, so report locally without any RPC.
            return ToolResult(ok=False, error=str(exc), preview="")

        await self._emit("tool.invoked", {
            "tool": spec.name,
            "capability": spec.capability,
            "args": self._safe_args(args),
            "preview": preview,
        })

        ttl = int(self._cfg("approval_ttl_s", 300))
        try:
            approval = await request_permission(
                self.sys, principal=self.agent_id, capability=spec.capability,
                scope=scope, reason=reason, preview=preview,
                project_id=self.project_id, task_id=self.task_id, ttl_s=ttl)
        except Denied as exc:
            detail = getattr(exc, "detail", None)
            if isinstance(detail, dict):
                request_id = str(detail.get("request_id") or "")
            else:
                request_id = str(detail or "")
            await self._emit("tool.denied", {
                "tool": spec.name,
                "capability": spec.capability,
                "request_id": request_id,
                "reason": exc.message,
                "preview": preview,
            })
            return ToolResult(ok=False, error=f"DENIED: {exc.message}",
                              preview=preview)
        except SfoError as exc:
            return await self._fail(
                spec, f"{exc.code}: {exc.message}", preview=preview)
        except Exception as exc:  # noqa: BLE001 - sys unavailable must not crash
            self.log.exception("approval request failed for %s", spec.name)
            return await self._fail(
                spec, f"{type(exc).__name__}: {exc}", preview=preview)

        if not await self._spend_once_grant(approval):
            reason = "one-shot approval was already used"
            await self._emit("tool.denied", {
                "tool": spec.name,
                "capability": spec.capability,
                "request_id": str(approval.get("id") or ""),
                "reason": reason,
                "preview": preview,
            })
            return ToolResult(ok=False, error=f"DENIED: {reason}", preview=preview)

        handler = self._handlers().get(spec.name)
        if handler is None:  # pragma: no cover - registry/handler mismatch
            return await self._fail(spec, f"no executor for tool {spec.name!r}",
                                    preview=preview)
        try:
            data = await handler(args, scope)
            if data is None:
                data = {}
            if not isinstance(data, dict):
                data = {"value": data}
        except ToolError as exc:
            return await self._fail(spec, str(exc), preview=preview)
        except SfoError as exc:
            return await self._fail(spec, f"{exc.code}: {exc.message}",
                                    preview=preview)
        except Exception as exc:  # noqa: BLE001 - tool bugs are data, not crashes
            self.log.exception("tool %s failed", spec.name)
            return await self._fail(
                spec, f"{type(exc).__name__}: {exc}", preview=preview)

        await self._use_grant(approval)
        await self._emit("tool.result", {
            "tool": spec.name,
            "ok": True,
            "summary": self._summarize(data),
            "preview": _truncate(preview, 400),
        })
        return ToolResult(ok=True, data=data, preview=preview)

    # -- orchestration helpers -------------------------------------------
    async def _fail(self, spec: ToolSpec, error: str, *, preview: str) -> ToolResult:
        await self._emit("tool.failed", {
            "tool": spec.name,
            "capability": spec.capability,
            "error": error,
            "preview": _truncate(preview, 400),
        })
        return ToolResult(ok=False, error=error, preview=preview)

    async def _spend_once_grant(self, approval: dict) -> bool:
        """Spend a human one-shot grant *before* acting; fail closed."""
        if str(approval.get("mode") or "") != "once":
            return True
        grant_id = approval.get("grant_id")
        if not grant_id:
            return False
        try:
            result = await call_ok(self.sys, "approval.use",
                                   {"grant_id": str(grant_id)})
        except Exception:  # noqa: BLE001 - cannot prove the grant: refuse
            self.log.debug("approval.use failed for grant %s", grant_id,
                           exc_info=True)
            return False
        return bool(result.get("ok"))

    async def _use_grant(self, approval: dict) -> None:
        grant_id = approval.get("grant_id") if isinstance(approval, dict) else None
        mode = str(approval.get("mode") or "") if isinstance(approval, dict) else ""
        if not grant_id or mode != "grant":
            return
        try:
            await call_ok(self.sys, "approval.use", {"grant_id": str(grant_id)})
        except Exception:  # pragma: no cover - audit failure must not fail work
            self.log.debug("approval.use failed for grant %s", grant_id,
                           exc_info=True)

    async def _emit(self, type: str, data: dict) -> None:
        payload = dict(data)
        payload.setdefault("agent_id", self.agent_id)
        if self.project_id is not None:
            payload.setdefault("project_id", self.project_id)
        try:
            await emit_event(self.sys, type, payload,
                             project_id=self.project_id, source=self.source)
        except Exception:  # pragma: no cover - telemetry is best effort
            self.log.debug("cannot emit %s", type, exc_info=True)

    def _safe_args(self, args: dict) -> dict:
        out: dict[str, Any] = {}
        for key, value in args.items():
            if key in ("content", "value", "patch"):
                continue
            if isinstance(value, str):
                out[key] = _truncate(_redact(value), 200)
            elif isinstance(value, (int, float, bool)):
                out[key] = value
        return out

    @staticmethod
    def _summarize(data: dict) -> dict:
        out: dict[str, Any] = {}
        for key in ("path", "bytes", "exit_code", "timed_out", "truncated",
                    "log", "status", "task_id", "project_id", "agent_id", "id"):
            if key in data:
                out[key] = data[key]
        if isinstance(data.get("entries"), list):
            out["entries"] = len(data["entries"])
        if isinstance(data.get("content"), str):
            out["chars"] = len(data["content"])
        if isinstance(data.get("stdout"), str):
            out["stdout_chars"] = len(data["stdout"])
        return out

    # -- scope / reason / preview (runs before any RPC) -------------------
    def _prepare(self, name: str, args: dict) -> tuple[dict, str, str]:
        limit = self.max_preview
        if self.read_only and name in ("fs.write", "fs.delete", "shell.exec"):
            raise ToolError(
                "project workspace is attached read-only (attached-ro); "
                "writing files or executing commands is disabled")
        base_scope: dict[str, Any] = {}
        if self.project_id is not None:
            base_scope["project_id"] = self.project_id

        if name == "fs.read":
            path = self.check_path(_arg_str(args, "path", required=True))
            scope = {**base_scope, "path": str(path)}
            return scope, "读取文件", _truncate(str(path), limit)

        if name == "fs.list":
            path = self.check_path(_arg_str(args, "path", "."))
            scope = {**base_scope, "path": str(path)}
            return scope, "列出目录", _truncate(str(path), limit)

        if name == "fs.write":
            path = self.check_path(_arg_str(args, "path", required=True))
            content = args.get("content")
            if not isinstance(content, str):
                raise ToolError("argument 'content' must be a string")
            scope = {**base_scope, "path": str(path),
                     "bytes": len(content.encode("utf-8"))}
            preview = _truncate(_redact(f"写入 {path}\n---\n{content}"), limit)
            return scope, "写入文件", preview

        if name == "fs.delete":
            path = self.check_path(_arg_str(args, "path", required=True))
            scope = {**base_scope, "path": str(path)}
            return scope, "删除文件", _truncate(str(path), limit)

        if name == "shell.exec":
            if not bool(self._cfg("allow_shell", True)):
                raise ToolError("shell execution is disabled by security config")
            command = _arg_str(args, "command", required=True)
            cwd = self.check_path(_arg_str(args, "cwd", "."), must_exist=True)
            if not cwd.is_dir():
                raise ToolError(f"cwd is not a directory: {cwd}")
            scope = {**base_scope, "cwd": str(cwd), "command": command}
            preview = _truncate(_redact(f"$ {command}\n(cwd: {cwd})"), limit)
            return scope, "执行命令", preview

        if name == "net.fetch":
            url = _arg_str(args, "url", required=True)
            self._validate_url(url)
            scope = {**base_scope, "url": url}
            return scope, "抓取网页", _truncate(_redact(url), limit)

        if name == "task.update":
            task_id = self._task_id_arg(args)
            patch = _arg_dict(args, "patch")
            scope = {**base_scope, "task_id": task_id}
            preview = _truncate(
                _redact(json.dumps(patch, ensure_ascii=False, default=str)), limit)
            return scope, "更新任务", preview

        if name == "task.artifact":
            task_id = self._task_id_arg(args)
            artifact = _arg_dict(args, "artifact")
            scope = {**base_scope, "task_id": task_id}
            preview = _truncate(
                _redact(json.dumps(artifact, ensure_ascii=False, default=str)), limit)
            return scope, "登记任务产物", preview

        if name == "task.note":
            task_id = self._task_id_arg(args)
            text = _arg_str(args, "text", required=True)
            scope = {**base_scope, "task_id": task_id}
            return scope, "记录任务备注", _truncate(_redact(text), limit)

        if name == "task.create":
            project_id = _arg_optional_str(args, "project_id", self.project_id)
            if not project_id:
                raise ToolError("project_id is required (agent has no project)")
            title = _arg_str(args, "title", required=True)
            description = _arg_optional_str(args, "description", "") or ""
            role = _arg_optional_str(args, "role")
            priority = _arg_int(args, "priority", 1, minimum=0, maximum=3)
            scope = {**base_scope, "project_id": project_id}
            preview_parts = [f"任务：{title}", f"角色：{role or '任意'}",
                             f"优先级：{priority}"]
            if description:
                preview_parts.append(f"说明：{_truncate(description, 200)}")
            return scope, "创建任务", _truncate(
                _redact("\n".join(preview_parts)), limit)

        if name == "task.list":
            project_id = _arg_optional_str(args, "project_id", self.project_id)
            scope = {"project_id": project_id} if project_id else {}
            return scope, "读取任务列表", _truncate(str(project_id or "全部"), limit)

        if name == "project.get":
            project_id = _arg_optional_str(args, "project_id", self.project_id)
            if not project_id:
                raise ToolError("project_id is required")
            return {**base_scope, "project_id": project_id}, "读取项目", project_id

        if name == "project.update":
            project_id = _arg_optional_str(args, "project_id", self.project_id)
            if not project_id:
                raise ToolError("project_id is required")
            patch = _arg_dict(args, "patch")
            preview = _truncate(
                _redact(json.dumps(patch, ensure_ascii=False, default=str)), limit)
            return ({**base_scope, "project_id": project_id},
                    "更新项目", preview)

        if name == "project.archive":
            project_id = _arg_optional_str(args, "project_id", self.project_id)
            if not project_id:
                raise ToolError("project_id is required")
            return ({**base_scope, "project_id": project_id}, "归档项目",
                    _truncate(f"归档项目 {project_id}（停止其 Agent，保留工作区）",
                              limit))

        if name == "project.delete":
            project_id = _arg_optional_str(args, "project_id", self.project_id)
            if not project_id:
                raise ToolError("project_id is required")
            purge = args.get("purge_workspace", True)
            if not isinstance(purge, bool):
                raise ToolError("argument 'purge_workspace' must be a boolean")
            scope = {**base_scope, "project_id": project_id,
                     "purge_workspace": purge}
            preview = (f"永久删除项目 {project_id}"
                       + ("（含工作区，不可恢复）" if purge else "（保留工作区）"))
            return scope, "删除项目", _truncate(preview, limit)

        if name == "agent.spawn":
            role = _arg_str(args, "role", required=True)
            project_id = _arg_optional_str(args, "project_id", self.project_id)
            task_id = _arg_optional_str(args, "task_id")
            if not project_id:
                raise ToolError("project_id is required (agent has no project)")
            scope = {"project_id": project_id, "role": role}
            if task_id:
                scope["task_id"] = task_id
            preview = f"派工：{role} -> {project_id}"
            if task_id:
                preview += f" / {task_id}"
            return scope, "拉起工人", _truncate(preview, limit)

        if name == "agent.kill":
            agent_id = _arg_str(args, "agent_id", required=True)
            reason = _arg_optional_str(args, "reason", "") or ""
            scope = {**base_scope, "agent_id": agent_id}
            return scope, "杀死工人", _truncate(
                f"{agent_id} {reason}".strip(), limit)

        if name == "agent.list":
            project_id = _arg_optional_str(args, "project_id", self.project_id)
            scope = {"project_id": project_id} if project_id else {}
            return scope, "读取 Agent 列表", _truncate(str(project_id or "全部"), limit)

        if name == "event.query":
            project_id = _arg_optional_str(args, "project_id", self.project_id)
            since = _arg_int(args, "since", 0, minimum=0)
            limit_n = _arg_int(args, "limit", 100, minimum=1, maximum=500)
            scope = {"project_id": project_id, "since": since,
                     "limit": limit_n} if project_id else {"since": since,
                                                           "limit": limit_n}
            return scope, "读取事件", f"since={since} limit={limit_n}"

        if name == "memory.get":
            project_id = _arg_optional_str(args, "project_id", self.project_id)
            key = _arg_str(args, "key", required=True)
            if not project_id:
                raise ToolError("project_id is required")
            return ({**base_scope, "project_id": project_id, "key": key},
                    "读取项目记忆", f"{project_id}/{key}")

        if name == "memory.set":
            project_id = _arg_optional_str(args, "project_id", self.project_id)
            key = _arg_str(args, "key", required=True)
            if not project_id:
                raise ToolError("project_id is required")
            return ({**base_scope, "project_id": project_id, "key": key},
                    "写入项目记忆", f"{project_id}/{key}")

        if name == "unit.list":
            return {}, "读取单元列表", "unit.list"

        if name == "wait":
            seconds = _arg_float(args, "seconds", 5.0)
            scope = {"project_id": self.project_id} if self.project_id else {}
            return scope, "等待任务推进", f"wait {min(max(seconds, 0.0), MAX_WAIT_S):g}s"

        raise ToolError(f"no planner for tool {name!r}")  # pragma: no cover

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            raise ToolError(f"only http/https URLs are allowed: {url!r}")
        if not parsed.netloc:
            raise ToolError(f"URL has no host: {url!r}")

    # -- handlers ---------------------------------------------------------
    def _handlers(self) -> dict[str, Callable[[dict, dict], Awaitable[dict]]]:
        return {
            "fs.read": self._fs_read,
            "fs.list": self._fs_list,
            "fs.write": self._fs_write,
            "fs.delete": self._fs_delete,
            "shell.exec": self._shell_exec,
            "net.fetch": self._net_fetch,
            "task.update": self._task_update,
            "task.artifact": self._task_artifact,
            "task.note": self._task_note,
            "task.create": self._task_create,
            "task.list": self._task_list,
            "project.get": self._project_get,
            "project.update": self._project_update,
            "project.archive": self._project_archive,
            "project.delete": self._project_delete,
            "agent.spawn": self._agent_spawn,
            "agent.kill": self._agent_kill,
            "agent.list": self._agent_list,
            "event.query": self._event_query,
            "memory.get": self._memory_get,
            "memory.set": self._memory_set,
            "unit.list": self._unit_list,
            "wait": self._wait,
        }

    async def _fs_read(self, args: dict, scope: dict) -> dict:
        path = self.check_path(_arg_str(args, "path", required=True),
                               must_exist=True)
        if not path.is_file():
            raise ToolError(f"not a file: {path}")
        max_bytes = _arg_int(args, "max_bytes", DEFAULT_MAX_BYTES, minimum=1,
                             maximum=MAX_READ_BYTES)
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        body = raw[:max_bytes]
        return {
            "path": str(path),
            "content": body.decode("utf-8", "replace"),
            "truncated": truncated,
            "bytes": len(body),
        }

    async def _fs_list(self, args: dict, scope: dict) -> dict:
        path = self.check_path(_arg_str(args, "path", "."), must_exist=True)
        if not path.is_dir():
            raise ToolError(f"not a directory: {path}")
        entries: list[dict] = []
        try:
            with os.scandir(path) as iterator:
                found = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise ToolError(f"cannot list {path}: {exc}") from exc
        for entry in found:
            try:
                if entry.is_symlink():
                    kind = "link"
                elif entry.is_dir():
                    kind = "dir"
                else:
                    kind = "file"
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                kind, size = "other", 0
            entries.append({"name": entry.name, "type": kind, "size": size})
        return {"path": str(path), "entries": entries}

    async def _fs_write(self, args: dict, scope: dict) -> dict:
        path = self.check_path(_arg_str(args, "path", required=True))
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("argument 'content' must be a string")
        if path == self.workspace:
            raise ToolError("cannot overwrite the workspace root")
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = content.encode("utf-8")
        path.write_bytes(raw)
        return {"path": str(path), "bytes": len(raw)}

    async def _fs_delete(self, args: dict, scope: dict) -> dict:
        path = self.check_path(_arg_str(args, "path", required=True),
                               must_exist=True)
        if path == self.workspace:
            raise ToolError("refusing to delete the workspace root")
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        return {"path": str(path), "deleted": True}

    def _shell_env(self) -> dict[str, str]:
        """Environment whitelist: drop credential-looking and SFO_* keys."""
        env: dict[str, str] = {}
        for key, value in os.environ.items():
            if key.startswith("SFO_"):
                continue
            if _ENV_SECRET.search(key):
                continue
            env[key] = value
        return env

    async def _shell_exec(self, args: dict, scope: dict) -> dict:
        if not bool(self._cfg("allow_shell", True)):
            raise ToolError("shell execution is disabled by security config")
        command = _arg_str(args, "command", required=True)
        cwd = self.check_path(_arg_str(args, "cwd", "."), must_exist=True)
        if not cwd.is_dir():
            raise ToolError(f"cwd is not a directory: {cwd}")
        timeout_s = _arg_float(args, "timeout_s",
                               float(self._cfg("shell_timeout_s", 60)))
        if timeout_s <= 0:
            timeout_s = float(self._cfg("shell_timeout_s", 60))

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._shell_env(),
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(),
                                                    timeout=timeout_s)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                proc.kill()
            except ProcessLookupError:  # pragma: no cover - race
                pass
            stdout, stderr = await proc.communicate()
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise

        exit_code = proc.returncode if proc.returncode is not None else -1
        out_capped = stdout[:OUTPUT_CAP_BYTES].decode("utf-8", "replace")
        err_capped = stderr[:OUTPUT_CAP_BYTES].decode("utf-8", "replace")
        truncated = len(stdout) > OUTPUT_CAP_BYTES or len(stderr) > OUTPUT_CAP_BYTES
        log_path = self._write_shell_log(command, cwd, exit_code, timed_out,
                                         stdout, stderr)
        return {
            "exit_code": exit_code,
            "stdout": out_capped,
            "stderr": err_capped,
            "truncated": truncated,
            "timed_out": timed_out,
            "log": str(log_path) if log_path else "",
            "cwd": str(cwd),
        }

    def _write_shell_log(self, command: str, cwd: Path, exit_code: int,
                         timed_out: bool, stdout: bytes,
                         stderr: bytes) -> Path | None:
        """Persist the full (uncapped) output under ``<workspace>/.artifacts``."""
        log_path = self.workspace / ".artifacts" / f"shell-{int(time.time() * 1000)}.log"
        header = (f"$ {command}\ncwd: {cwd}\nexit: {exit_code} "
                  f"timed_out: {timed_out}\n--- stdout ---\n").encode("utf-8")
        body = header + stdout + b"\n--- stderr ---\n" + stderr + b"\n"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_bytes(body)
        except OSError:
            self.log.warning("cannot write shell log %s", log_path, exc_info=True)
            return None
        return log_path

    async def _net_fetch(self, args: dict, scope: dict) -> dict:
        url = _arg_str(args, "url", required=True)
        self._validate_url(url)
        max_bytes = _arg_int(args, "max_bytes", DEFAULT_MAX_BYTES, minimum=1,
                             maximum=MAX_READ_BYTES)
        status, final_url, content_type, body = await asyncio.to_thread(
            self._http_get, url, max_bytes)
        truncated = len(body) > max_bytes
        return {
            "status": status,
            "url": final_url,
            "content": body[:max_bytes].decode("utf-8", "replace"),
            "content_type": content_type,
            "truncated": truncated,
        }

    @staticmethod
    def _http_get(url: str, max_bytes: int) -> tuple[int, str, str, bytes]:
        request = Request(url, headers={
            "User-Agent": "StackFlowOS/0.1 (+stdlib)",
            "Accept": "*/*",
        })
        try:
            with urlopen(request, timeout=NET_TIMEOUT_S) as response:
                final_url = response.geturl()
                parsed = urlsplit(final_url)
                if parsed.scheme not in ("http", "https"):
                    raise ToolError(
                        f"redirected to unsupported scheme: {parsed.scheme!r}")
                status = int(getattr(response, "status", 200) or 200)
                headers = getattr(response, "headers", None)
                content_type = headers.get("Content-Type", "") if headers else ""
                body = response.read(max_bytes + 1)
                return status, final_url, content_type, body
        except ToolError:
            raise
        except Exception as exc:  # urllib raises many exception types
            raise ToolError(f"fetch failed: {type(exc).__name__}: {exc}") from exc

    # -- sys-bound tools --------------------------------------------------
    async def _task_update(self, args: dict, scope: dict) -> dict:
        task_id = self._task_id_arg(args)
        patch = _arg_dict(args, "patch")
        return await call_ok(self.sys, "task.update",
                             {"task_id": task_id, "patch": patch})

    async def _task_artifact(self, args: dict, scope: dict) -> dict:
        task_id = self._task_id_arg(args)
        artifact = _arg_dict(args, "artifact")
        return await call_ok(self.sys, "task.artifact",
                             {"task_id": task_id, "artifact": artifact})

    async def _task_note(self, args: dict, scope: dict) -> dict:
        task_id = self._task_id_arg(args)
        text = _arg_str(args, "text", required=True)
        return await call_ok(self.sys, "task.note",
                             {"task_id": task_id, "text": text,
                              "author": self.agent_id})

    async def _task_create(self, args: dict, scope: dict) -> dict:
        project_id = (_arg_optional_str(args, "project_id", self.project_id)
                      or scope.get("project_id"))
        if not project_id:
            raise ToolError("project_id is required")
        payload: dict[str, Any] = {
            "project_id": project_id,
            "title": _arg_str(args, "title", required=True),
        }
        description = _arg_optional_str(args, "description")
        role = _arg_optional_str(args, "role")
        if description:
            payload["description"] = description
        if role:
            payload["role"] = role
        if "priority" in args:
            payload["priority"] = _arg_int(args, "priority", 1, minimum=0,
                                           maximum=3)
        payload["created_by"] = self.agent_id
        return await call_ok(self.sys, "task.create", payload)

    async def _task_list(self, args: dict, scope: dict) -> dict:
        project_id = _arg_optional_str(args, "project_id", self.project_id)
        payload = {"project_id": project_id} if project_id else {}
        return await call_ok(self.sys, "task.list", payload)

    async def _project_get(self, args: dict, scope: dict) -> dict:
        project_id = _arg_optional_str(args, "project_id", self.project_id)
        if not project_id:
            raise ToolError("project_id is required")
        return await call_ok(self.sys, "project.get", {"project_id": project_id})

    async def _project_update(self, args: dict, scope: dict) -> dict:
        project_id = _arg_optional_str(args, "project_id", self.project_id)
        if not project_id:
            raise ToolError("project_id is required")
        patch = _arg_dict(args, "patch")
        return await call_ok(self.sys, "project.update",
                             {"project_id": project_id, "patch": patch})

    async def _project_archive(self, args: dict, scope: dict) -> dict:
        project_id = _arg_optional_str(args, "project_id", self.project_id)
        if not project_id:
            raise ToolError("project_id is required")
        return await call_ok(self.sys, "project.archive",
                             {"project_id": project_id})

    async def _project_delete(self, args: dict, scope: dict) -> dict:
        project_id = _arg_optional_str(args, "project_id", self.project_id)
        if not project_id:
            raise ToolError("project_id is required")
        confirm = _arg_optional_str(args, "confirm")
        if not confirm:
            project = await call_ok(self.sys, "project.get",
                                    {"project_id": project_id})
            confirm = str(project.get("name") or project_id)
        purge = args.get("purge_workspace", True)
        if not isinstance(purge, bool):
            raise ToolError("argument 'purge_workspace' must be a boolean")
        return await call_ok(self.sys, "project.delete", {
            "project_id": project_id,
            "purge_workspace": purge,
            "confirm": confirm,
        })

    async def _agent_spawn(self, args: dict, scope: dict) -> dict:
        role = _arg_str(args, "role", required=True)
        project_id = _arg_optional_str(args, "project_id", self.project_id)
        if not project_id:
            raise ToolError("project_id is required")
        payload: dict[str, Any] = {
            "role": role,
            "project_id": project_id,
            "reason": _arg_optional_str(args, "reason",
                                        f"spawned by {self.agent_id}") or "",
        }
        task_id = _arg_optional_str(args, "task_id")
        if task_id:
            payload["task_id"] = task_id
        return await call_ok(self.sys, "agent.spawn", payload)

    async def _agent_kill(self, args: dict, scope: dict) -> dict:
        agent_id = _arg_str(args, "agent_id", required=True)
        reason = _arg_optional_str(args, "reason", "killed by agent") or ""
        return await call_ok(self.sys, "agent.kill",
                             {"agent_id": agent_id, "reason": reason})

    async def _agent_list(self, args: dict, scope: dict) -> dict:
        project_id = _arg_optional_str(args, "project_id", self.project_id)
        payload = {"project_id": project_id} if project_id else {}
        return await call_ok(self.sys, "agent.list", payload)

    async def _event_query(self, args: dict, scope: dict) -> dict:
        project_id = _arg_optional_str(args, "project_id", self.project_id)
        since = _arg_int(args, "since", 0, minimum=0)
        limit = _arg_int(args, "limit", 100, minimum=1, maximum=500)
        payload: dict[str, Any] = {"seq": since, "limit": limit}
        if project_id:
            payload["project_id"] = project_id
        return await call_ok(self.sys, "event.since", payload)

    async def _memory_get(self, args: dict, scope: dict) -> dict:
        project_id = _arg_optional_str(args, "project_id", self.project_id)
        key = _arg_str(args, "key", required=True)
        if not project_id:
            raise ToolError("project_id is required")
        return await call_ok(self.sys, "memory.get",
                             {"project_id": project_id, "key": key})

    async def _memory_set(self, args: dict, scope: dict) -> dict:
        project_id = _arg_optional_str(args, "project_id", self.project_id)
        key = _arg_str(args, "key", required=True)
        if not project_id:
            raise ToolError("project_id is required")
        return await call_ok(self.sys, "memory.set",
                             {"project_id": project_id, "key": key,
                              "value": args.get("value")})

    async def _unit_list(self, args: dict, scope: dict) -> dict:
        return await call_ok(self.sys, "unit.list", {})

    async def _wait(self, args: dict, scope: dict) -> dict:
        seconds = min(max(_arg_float(args, "seconds", 5.0), 0.0), MAX_WAIT_S)
        await asyncio.sleep(seconds)
        out: dict[str, Any] = {"waited_s": seconds}
        if self.project_id:
            tasks = await call_ok(self.sys, "task.list",
                                  {"project_id": self.project_id})
            out["tasks"] = _board(tasks)
        return out


__all__ = [
    "DEFAULT_MAX_BYTES",
    "OUTPUT_CAP_BYTES",
    "TOOLS",
    "ToolError",
    "ToolExecutor",
    "ToolResult",
    "ToolSpec",
    "get_tool",
    "tools_for",
]

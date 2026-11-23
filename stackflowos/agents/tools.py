"""Tool registry and local tool execution for agent workers.

Design contract (docs/architecture.md §6.4):

    resolve scope -> risk -> sys.approval.request -> poll -> claim one-shot
    -> execute (re-check path/cwd) -> approval.use (auto grants) -> tool.result

The executor runs *inside the worker process*; sys only grants capabilities and
audits.  It never imports ``security``/``l2``/``projects``: the security config
is injected, and the policy decision is always taken by sys (``request_permission``)
so a local bug cannot widen permissions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from ..errors import BadRequest, Denied, SfoError
from ..util.logging import get_logger
from ..util.net import (
    PRIVATE_NETWORK_ERROR,
    InvalidTarget,
    is_private_host,
    validate_fetch_url,
)
from ..util.redact import is_secret_key, redact_text
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
#: Maximum redirect hops followed for one ``net.fetch`` (M4).
MAX_REDIRECTS = 5
#: Longest single ``wait`` tool call (seconds).
MAX_WAIT_S = 60.0

#: Only these headers are sent on a fetch; no cookies/credentials ever are.
_SAFE_FETCH_HEADERS = {
    "User-Agent": "StackFlowOS/0.1 (+stdlib)",
    "Accept": "*/*",
}
#: Stripped again on every redirect, should anything ever add them.
_SENSITIVE_HEADERS = frozenset({
    "authorization", "cookie", "cookie2", "proxy-authorization",
})

#: Bytes read from a child stream per pump iteration; keeps memory flat.
_SHELL_CHUNK_BYTES = 64 * 1024
#: Grace period between SIGTERM and SIGKILL for a timed-out process group (H4).
_SHELL_TERM_GRACE_S = 3.0
#: Grace period to reap a SIGKILLed process group before abandoning the pipes.
_SHELL_KILL_GRACE_S = 5.0
#: Bound on the post-EOF ``proc.wait()`` of an otherwise finished command.
_SHELL_EXIT_GRACE_S = 5.0
#: Cancellation path: keep shutdown short so callers are not delayed.
_SHELL_CANCEL_TERM_GRACE_S = 0.5
_SHELL_CANCEL_KILL_GRACE_S = 1.0
#: Poll interval when waiting for a whole process group to disappear.
_SHELL_GROUP_POLL_S = 0.05

_ENV_SECRET = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)


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


def _strip_credentials(request: Request) -> None:
    """Defensively drop credential headers before following a redirect."""
    for mapping in (request.headers, request.unredirected_hdrs):
        for key in [item for item in mapping
                    if str(item).lower() in _SENSITIVE_HEADERS]:
            del mapping[key]


class _GuardedRedirectHandler(HTTPRedirectHandler):
    """Validate each redirect target *before* urllib requests it (M4).

    ``validate`` is the executor's URL check (scheme/userinfo/port/private
    network/length), so a redirect to the cloud metadata service or an
    intranet host is rejected without a second request being sent.

    urllib's own ``http_error_30x`` rejects non-http(s) targets first, so this
    override checks the absolute target *before* delegating; ``redirect_request``
    keeps the same guard for direct calls and other handlers.
    """

    #: urllib's own bookkeeping; our counter below is authoritative.
    max_repeats = MAX_REDIRECTS
    max_redirections = MAX_REDIRECTS

    def __init__(self, validate: Callable[[str], None]) -> None:
        super().__init__()
        self._validate = validate
        self.hops = 0
        self._hop_validated = False

    def _validate_hop(self, newurl: str) -> None:
        self.hops += 1
        if self.hops > MAX_REDIRECTS:
            raise ToolError(f"too many redirects (max {MAX_REDIRECTS})")
        self._validate(newurl)

    def http_error_302(self, req: Request, fp: Any, code: int, msg: str,
                       headers: Any) -> Any:
        location = None
        if headers is not None:
            location = headers.get("location") or headers.get("uri")
        if location:
            # Resolve relative Locations and validate before urllib's scheme
            # gate (which would otherwise raise a generic HTTPError).
            self._validate_hop(urljoin(req.full_url, str(location).strip()))
            self._hop_validated = True
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = \
        http_error_302

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Request | None:
        if not self._hop_validated:
            # Direct call (not via http_error_30x): validate the target here.
            self._validate_hop(newurl)
        self._hop_validated = False
        new_request = super().redirect_request(
            req, fp, code, msg, headers, newurl)
        if new_request is not None:
            _strip_credentials(new_request)
        return new_request


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


def _require_delete_confirm(args: dict) -> str:
    """``confirm`` must be supplied by the caller; never auto-filled (M6)."""
    confirm = _arg_optional_str(args, "confirm")
    if not confirm:
        raise ToolError("project deletion requires confirm=<project name>")
    return confirm


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
    """Best-effort removal of inline credentials from human previews.

    Delegates to :func:`stackflowos.util.redact.redact_text` so previews and
    operator audits share one rule set (M8).
    """
    return redact_text(text)


#: Lines shown from each edge of a large ``fs.write`` payload (M7).
_WRITE_EDGE_LINES = 20


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _shell_chain_operators(command: str) -> list[str]:
    """Chained operators in ``command``, ignoring quoted/escaped copies.

    The approval reviewer must see that ``rm -rf x && curl …`` is a chain,
    even though the shell itself is what finally parses it.  This is a small
    scanner rather than a full shell parser: it only needs to avoid false
    positives from quoted arguments.
    """
    operators: list[str] = []
    index, length = 0, len(command)
    while index < length:
        char = command[index]
        if char in ("'", '"'):
            quote = char
            index += 1
            while index < length:
                if command[index] == "\\" and quote == '"':
                    index += 2
                    continue
                if command[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        if char == "\\":
            index += 2
            continue
        pair = command[index:index + 2]
        if pair in ("&&", "||"):
            if pair not in operators:
                operators.append(pair)
            index += 2
            continue
        if char in (";", "|", "&"):
            if char not in operators:
                operators.append(char)
        index += 1
    return operators


def _write_preview(path: Path, content: str, store_limit: int) -> str:
    """Full approval preview for ``fs.write`` (M7).

    Includes the destination, byte count, content hash and a head/tail view so
    the reviewer can see what actually lands on disk.  The redacted content is
    kept after the summary (bounded by ``store_limit``); the broker stores at
    most ``security.preview_store_chars`` and flags ``preview_truncated``.
    """
    raw = content.encode("utf-8")
    redacted = _redact(content)
    header = [f"写入 {path}", f"bytes: {len(raw)} · sha256: {_sha256_hex(raw)}"]
    try:
        if path.exists():
            old = path.stat().st_size if path.is_file() else None
            header[1] += (" · mode: overwrite"
                          + (f" (old {old} bytes)" if old is not None else ""))
        else:
            header[1] += " · mode: new"
    except OSError:
        header[1] += " · mode: unknown"
    lines = redacted.splitlines()
    if len(lines) <= _WRITE_EDGE_LINES * 2:
        # Small payload: show it in full, no omission marker.
        return "\n".join(header + ["---", redacted])
    head = "\n".join(lines[:_WRITE_EDGE_LINES])
    tail = "\n".join(lines[-_WRITE_EDGE_LINES:])
    omitted = lines[_WRITE_EDGE_LINES:-_WRITE_EDGE_LINES]
    omitted_bytes = len("\n".join(omitted).encode("utf-8"))
    summary = "\n".join([
        f"--- 前 {_WRITE_EDGE_LINES} 行 ---", head,
        f"…（已省略 {len(omitted)} 行 / {omitted_bytes} 字节）…",
        f"--- 后 {_WRITE_EDGE_LINES} 行 ---", tail,
        "--- 内容 ---",
    ])
    # Leave room for the redacted body within the store bound; the header and
    # summary stay intact so the display (truncated to max_preview_chars)
    # still shows both edges of the payload.
    budget = max(0, int(store_limit) - len(summary) - 1)
    body = redacted[:budget] if len(redacted) > budget else redacted
    return "\n".join(header) + "\n" + summary + "\n" + body


def _shell_preview(command: str, cwd: Path) -> str:
    """Full approval preview for ``shell.exec`` (M7)."""
    redacted = _redact(command)
    operators = _shell_chain_operators(command)
    return "\n".join([
        f"$ {redacted}",
        f"(cwd: {cwd})",
        "链式操作符: " + (" ".join(operators) if operators else "无"),
    ])


def _net_preview(url: str) -> str:
    """Full approval preview for ``net.fetch`` (M7)."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    target = f"{(parsed.scheme or '').lower()}://{host}"
    if parsed.port is not None:
        target += f":{parsed.port}"
    target += parsed.path or "/"
    private = is_private_host(host)
    lines = [
        f"url: {_redact(url)}",
        f"目标: {target}",
        f"私网: {'是' if private else '否'}",
    ]
    if private:
        lines.append("（security.allow_private_network 允许私网目标）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# shell process-group / bounded-output helpers
# ---------------------------------------------------------------------------

class _ShellLog:
    """Best-effort streaming sink for a command's *complete* output.

    Writes are synchronous and never raise: an unwritable workspace or a full
    disk must not change the tool's result.  Stream sections are inserted
    lazily so interleaved stdout/stderr chunks stay readable.
    """

    def __init__(self, handle: Any, path: Path) -> None:
        self._handle = handle
        self.path = path
        self._stream: str | None = None
        self.failed = False

    def write(self, stream: str, chunk: bytes) -> None:
        if self.failed or not chunk:
            return
        try:
            if stream != self._stream:
                if self._stream is not None:
                    self._handle.write(b"\n")
                self._handle.write(f"--- {stream} ---\n".encode("utf-8"))
                self._stream = stream
            self._handle.write(chunk)
        except (OSError, ValueError):
            self.failed = True

    def close(self, *, exit_code: int, timed_out: bool) -> None:
        try:
            if not self.failed:
                self._handle.write(
                    f"\n--- end ---\nexit: {exit_code} "
                    f"timed_out: {timed_out}\n".encode("utf-8"))
        except (OSError, ValueError):
            self.failed = True
        finally:
            try:
                self._handle.close()
            except (OSError, ValueError):
                pass


class _CappedStream:
    """Drain one child stream without buffering it in full.

    Memory stays bounded by ``cap``: the first ``cap`` bytes are kept so short
    output is returned verbatim, and after an overflow a rolling tail of the
    last ``cap`` bytes is retained (the middle is dropped).  Every chunk is
    also forwarded to the optional log sink, so the complete output is
    persisted on disk instead of in memory.
    """

    def __init__(self, stream: Any, *, cap: int = OUTPUT_CAP_BYTES,
                 log: _ShellLog | None = None, name: str = "stdout",
                 logger: Any = None) -> None:
        self._stream = stream
        self._cap = max(1, int(cap))
        self._log = log
        self._name = name
        self._logger = logger
        self._head = bytearray()
        self._tail = bytearray()
        self._total = 0
        self.error: BaseException | None = None

    @property
    def total_bytes(self) -> int:
        return self._total

    @property
    def truncated(self) -> bool:
        return self._total > self._cap

    async def pump(self) -> None:
        """Read to EOF, forwarding whole chunks to the log as they arrive."""
        stream = self._stream
        if stream is None:
            return
        try:
            while True:
                chunk = await stream.read(_SHELL_CHUNK_BYTES)
                if not chunk:
                    return
                self._feed(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - caller cleans the process up
            self.error = exc
            if self._logger is not None:
                self._logger.warning("shell %s capture failed: %s",
                                     self._name, exc)

    def _feed(self, chunk: bytes) -> None:
        self._total += len(chunk)
        if self._log is not None:
            self._log.write(self._name, chunk)
        room = self._cap - len(self._head)
        if room > 0:
            self._head.extend(chunk[:room])
            chunk = chunk[room:]
        if chunk:
            self._tail.extend(chunk)
            overflow = len(self._tail) - self._cap
            if overflow > 0:
                del self._tail[:overflow]

    def render(self) -> bytes:
        if self._total <= self._cap:
            return bytes(self._head)
        marker = b"\n... [truncated] ...\n"
        budget = self._cap - len(marker)
        if budget <= 0:
            return marker[: self._cap]
        head_n = budget // 2
        tail_n = budget - head_n
        return bytes(self._head[:head_n]) + marker + bytes(self._tail[-tail_n:])

    def text(self) -> str:
        return self.render().decode("utf-8", "replace")


def _group_id(proc: asyncio.subprocess.Process) -> int | None:
    """Process-group id of *proc*; ``start_new_session`` makes it the leader."""
    if not hasattr(os, "killpg"):
        return None  # non-POSIX: fall back to signalling the direct child
    try:
        return os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        # The leader may already be reaped while its group still has members;
        # a session leader's pgid never changes, so its pid is the group id.
        return proc.pid


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> bool:
    """Signal the whole process group, falling back to the direct child."""
    pgid = _group_id(proc)
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        if sig == getattr(signal, "SIGKILL", None):
            proc.kill()
        else:
            proc.terminate()
        return True
    except (ProcessLookupError, OSError):
        return False


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # cannot tell: assume alive so the caller escalates
    return True


def _abandon_process(proc: asyncio.subprocess.Process) -> None:
    """Close the subprocess transport (pipe fds + child) as a last resort.

    This is what makes the "unkillable child" path safe: even if a signal did
    not take effect, closing the transport releases our pipe fds instead of
    leaving the worker waiting for EOF forever.
    """
    transport = getattr(proc, "_transport", None)
    if transport is None:  # pragma: no cover - asyncio always sets it
        return
    try:
        transport.close()
    except Exception:  # noqa: BLE001 - teardown is best effort
        pass


async def _wait_process(proc: asyncio.subprocess.Process, timeout: float) -> bool:
    """Await child exit with a hard bound; never blocks forever."""
    if proc.returncode is not None:
        return True
    try:
        await asyncio.wait_for(proc.wait(), timeout=max(0.0, timeout))
    except asyncio.TimeoutError:
        return False
    return True


async def _wait_group_gone(proc: asyncio.subprocess.Process,
                           pgid: int | None, timeout: float) -> bool:
    """Wait (bounded) until the child is reaped and its process group is empty."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    while True:
        if proc.returncode is None:
            remaining = deadline - loop.time()
            try:
                await asyncio.wait_for(
                    proc.wait(), timeout=min(_SHELL_GROUP_POLL_S,
                                             max(0.0, remaining)))
            except asyncio.TimeoutError:
                pass
        if proc.returncode is not None and (
                pgid is None or not _group_alive(pgid)):
            return True
        remaining = deadline - loop.time()
        if remaining <= 0.0:
            return False
        await asyncio.sleep(min(_SHELL_GROUP_POLL_S, remaining))


async def _settle_pumps(tasks: list[asyncio.Task], *, timeout: float = 1.0) -> None:
    """Give capture tasks a moment to finish, then cancel the stragglers."""
    pending = [task for task in tasks if not task.done()]
    if not pending:
        return
    _, pending = await asyncio.wait(pending, timeout=max(0.0, timeout))
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=1.0)


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
        #: Bound for the full preview sent to the broker (M7).
        self.max_preview_store = max(
            1, int(self._cfg("preview_store_chars", 65536)))
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
            scope, reason, preview_full = self._prepare(spec.name, args)
        except ToolError as exc:
            # Invalid invocation: nothing was requested from sys and no side
            # effect happened, so report locally without any RPC.
            return ToolResult(ok=False, error=str(exc), preview="")

        # M7: the broker/audit log keeps a bounded full preview; everything
        # human-facing on the hot path (events, tool result) stays display
        # sized so one approval cannot bloat the event log.
        display = _truncate(preview_full, self.max_preview)

        await self._emit("tool.invoked", {
            "tool": spec.name,
            "capability": spec.capability,
            "args": self._safe_args(args),
            "preview": display,
        })

        ttl = int(self._cfg("approval_ttl_s", 300))
        try:
            approval = await request_permission(
                self.sys, principal=self.agent_id, capability=spec.capability,
                scope=scope, reason=reason, preview=display,
                preview_full=preview_full,
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
                "preview": display,
            })
            return ToolResult(ok=False, error=f"DENIED: {exc.message}",
                              preview=display)
        except SfoError as exc:
            return await self._fail(
                spec, f"{exc.code}: {exc.message}", preview=display)
        except Exception as exc:  # noqa: BLE001 - sys unavailable must not crash
            self.log.exception("approval request failed for %s", spec.name)
            return await self._fail(
                spec, f"{type(exc).__name__}: {exc}", preview=display)

        if not await self._claim_once(approval):
            reason = "one-shot approval already claimed"
            await self._emit("tool.denied", {
                "tool": spec.name,
                "capability": spec.capability,
                "request_id": str(approval.get("id") or ""),
                "reason": reason,
                "preview": display,
            })
            return ToolResult(ok=False, error=f"DENIED: {reason}", preview=display)

        handler = self._handlers().get(spec.name)
        if handler is None:  # pragma: no cover - registry/handler mismatch
            return await self._fail(spec, f"no executor for tool {spec.name!r}",
                                    preview=display)
        try:
            data = await handler(args, scope)
            if data is None:
                data = {}
            if not isinstance(data, dict):
                data = {"value": data}
        except ToolError as exc:
            return await self._fail(spec, str(exc), preview=display)
        except SfoError as exc:
            return await self._fail(spec, f"{exc.code}: {exc.message}",
                                    preview=display)
        except Exception as exc:  # noqa: BLE001 - tool bugs are data, not crashes
            self.log.exception("tool %s failed", spec.name)
            return await self._fail(
                spec, f"{type(exc).__name__}: {exc}", preview=display)

        await self._use_grant(approval)
        await self._emit("tool.result", {
            "tool": spec.name,
            "ok": True,
            "summary": self._summarize(data),
            "preview": _truncate(display, 400),
        })
        return ToolResult(ok=True, data=data, preview=display)

    # -- orchestration helpers -------------------------------------------
    async def _fail(self, spec: ToolSpec, error: str, *, preview: str) -> ToolResult:
        await self._emit("tool.failed", {
            "tool": spec.name,
            "capability": spec.capability,
            "error": error,
            "preview": _truncate(preview, 400),
        })
        return ToolResult(ok=False, error=error, preview=preview)

    async def _claim_once(self, approval: dict) -> bool:
        """Claim a human one-shot approval *before* acting; fail closed.

        M10: the execution slot is bound by ``approval.claim`` -- exactly one
        caller can claim an approved ``once`` request (the broker spends the
        grant under its lock and returns a ``claim_id``); everyone else gets a
        conflict and must not execute.  Auto grants (``mode == "grant"``)
        keep their existing post-execution ``approval.use`` audit path.
        """
        if str(approval.get("mode") or "") != "once":
            return True
        request_id = approval.get("id")
        if not request_id:
            return False
        try:
            result = await call_ok(self.sys, "approval.claim",
                                   {"request_id": str(request_id)})
        except Exception:  # noqa: BLE001 - cannot prove the claim: refuse
            self.log.debug("approval.claim failed for request %s", request_id,
                           exc_info=True)
            return False
        return bool(result.get("claim_id"))

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
            if is_secret_key(key):
                out[key] = "***"
            elif isinstance(value, str):
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
        # ``preview`` here is the full (redacted, bounded) preview; the caller
        # derives the display text with ``max_preview_chars`` (M7).
        limit = self.max_preview_store
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
            raw = content.encode("utf-8")
            scope = {**base_scope, "path": str(path), "bytes": len(raw)}
            preview = _write_preview(path, content, limit)
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
            return scope, "执行命令", _truncate(
                _shell_preview(command, cwd), limit)

        if name == "net.fetch":
            url = _arg_str(args, "url", required=True)
            self._validate_url(url)
            scope = {**base_scope, "url": url}
            return scope, "抓取网页", _truncate(_net_preview(url), limit)

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
            _require_delete_confirm(args)
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

    def _allow_private_network(self) -> bool:
        """Whether ``security.allow_private_network`` opted into SSRF targets."""
        return bool(self._cfg("allow_private_network", False))

    def _validate_url(self, url: str) -> None:
        """Pre-flight check shared by the initial URL, redirects and the end.

        Rejects non-http(s) schemes, userinfo, empty/invalid hosts, over-long
        URLs and - unless ``security.allow_private_network`` is on - every
        private/loopback/link-local target, including *all* addresses the host
        resolves to. Runs before the approval request and again on each hop.
        """
        try:
            validate_fetch_url(
                url, allow_private_network=self._allow_private_network())
        except InvalidTarget as exc:
            message = str(exc)
            if message == PRIVATE_NETWORK_ERROR:
                raise ToolError(PRIVATE_NETWORK_ERROR) from exc
            raise ToolError(message) from exc

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

    def _default_shell_timeout(self) -> float:
        """Configured shell timeout, sanitized so it can never hang a worker."""
        try:
            value = float(self._cfg("shell_timeout_s", 60))
        except (TypeError, ValueError):
            return 60.0
        if not math.isfinite(value) or value <= 0:
            return 60.0
        return value

    def _open_shell_log(self, command: str, cwd: Path) -> _ShellLog | None:
        """Create a uniquely named log file for streaming the full output.

        Best effort: when ``.artifacts`` is unwritable the tool still returns
        its result, just without a ``log`` path.
        """
        directory = self.workspace / ".artifacts"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"shell-{time.time_ns()}-{secrets.token_hex(4)}.log"
            handle = path.open("xb")
        except OSError as exc:
            self.log.warning("cannot create shell log under %s: %s",
                             directory, exc)
            return None
        log = _ShellLog(handle, path)
        try:
            handle.write(f"$ {command}\ncwd: {cwd}\n".encode("utf-8", "replace"))
        except (OSError, ValueError):
            log.failed = True
        return log

    async def _terminate_group(self, proc: asyncio.subprocess.Process) -> bool:
        """SIGTERM the whole group, then SIGKILL it; bounded at every step."""
        pgid = _group_id(proc)
        _signal_group(proc, signal.SIGTERM)
        if await _wait_group_gone(proc, pgid, _SHELL_TERM_GRACE_S):
            return True
        _signal_group(proc, signal.SIGKILL)
        if await _wait_group_gone(proc, pgid, _SHELL_KILL_GRACE_S):
            return True
        self.log.warning("shell process group %s survived SIGKILL", pgid)
        return False

    async def _shell_exec(self, args: dict, scope: dict) -> dict:
        if not bool(self._cfg("allow_shell", True)):
            raise ToolError("shell execution is disabled by security config")
        command = _arg_str(args, "command", required=True)
        cwd = self.check_path(_arg_str(args, "cwd", "."), must_exist=True)
        if not cwd.is_dir():
            raise ToolError(f"cwd is not a directory: {cwd}")
        default_timeout = self._default_shell_timeout()
        timeout_s = _arg_float(args, "timeout_s", default_timeout)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            # H4 only needs to stay hang-free; strict 400s (and the upper
            # bound) are handled by the later timeout-validation item.
            timeout_s = default_timeout

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._shell_env(),
            start_new_session=True,  # setsid: a timeout can kill the whole group
        )
        log = self._open_shell_log(command, cwd)
        out_reader = _CappedStream(proc.stdout, log=log, name="stdout",
                                   logger=self.log)
        err_reader = _CappedStream(proc.stderr, log=log, name="stderr",
                                   logger=self.log)
        pumps = [
            asyncio.create_task(out_reader.pump(), name="shell-stdout"),
            asyncio.create_task(err_reader.pump(), name="shell-stderr"),
        ]
        timed_out = False
        try:
            try:
                _, pending = await asyncio.wait(pumps, timeout=timeout_s)
                if pending:
                    # The command, or a descendant still holding its pipes, is
                    # running past the deadline: kill the process group.
                    timed_out = True
                    await self._terminate_group(proc)
                elif not await _wait_process(proc, _SHELL_EXIT_GRACE_S):
                    timed_out = True
                    await self._terminate_group(proc)
                elif out_reader.error is not None or err_reader.error is not None:
                    # A capture failure must not leave the child running.
                    await self._terminate_group(proc)
            except asyncio.CancelledError:
                # Cancellation gets the same process-group cleanup (bounded),
                # then the cancellation is allowed to propagate to the caller.
                pgid = _group_id(proc)
                _signal_group(proc, signal.SIGTERM)
                if not await _wait_group_gone(
                        proc, pgid, _SHELL_CANCEL_TERM_GRACE_S):
                    _signal_group(proc, signal.SIGKILL)
                    await _wait_group_gone(proc, pgid,
                                           _SHELL_CANCEL_KILL_GRACE_S)
                raise
        finally:
            exit_code = proc.returncode if proc.returncode is not None else -1
            try:
                # Release pipe fds even if a signal did not take effect; the
                # pumps below then see EOF instead of blocking forever.
                _abandon_process(proc)
                await _settle_pumps(pumps)
            finally:
                if log is not None:
                    log.close(exit_code=exit_code, timed_out=timed_out)

        return {
            "exit_code": exit_code,
            "stdout": out_reader.text(),
            "stderr": err_reader.text(),
            "truncated": out_reader.truncated or err_reader.truncated,
            "timed_out": timed_out,
            "log": str(log.path) if log is not None else "",
            "cwd": str(cwd),
        }

    async def _net_fetch(self, args: dict, scope: dict) -> dict:
        url = _arg_str(args, "url", required=True)
        # Re-check at execution time too: the approval happened earlier and
        # the URL/redirect target must be revalidated against live DNS.
        self._validate_url(url)
        max_bytes = _arg_int(args, "max_bytes", DEFAULT_MAX_BYTES, minimum=1,
                             maximum=MAX_READ_BYTES)
        status, final_url, content_type, body = await asyncio.to_thread(
            self._http_get, url, max_bytes)
        truncated = len(body) > max_bytes
        return {
            "status": status,
            "requested_url": url,
            "url": final_url,
            "content": body[:max_bytes].decode("utf-8", "replace"),
            "content_type": content_type,
            "truncated": truncated,
        }

    def _http_get(self, url: str, max_bytes: int) -> tuple[int, str, str, bytes]:
        self._validate_url(url)
        handler = _GuardedRedirectHandler(self._validate_url)
        opener = build_opener(handler)
        # Explicit allowlist: no Cookie/Authorization is ever attached, and no
        # ambient cookie jar or credential store is consulted.
        request = Request(url, headers=dict(_SAFE_FETCH_HEADERS))
        try:
            # Local opener, not ``urlopen``: no globally installed handlers
            # (cookies/proxies) can loosen the redirect policy.
            with opener.open(request, timeout=NET_TIMEOUT_S) as response:
                final_url = response.geturl()
                # Final target re-checked after the last hop.
                self._validate_url(final_url)
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
        confirm = _require_delete_confirm(args)
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

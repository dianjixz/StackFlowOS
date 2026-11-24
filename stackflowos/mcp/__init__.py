"""MCP host: configured stdio servers as ``mcp.<name>`` units.

Each server is a real subprocess (``runtime.mode=subprocess``, real pid) and a
unit of its own; the tools it advertises are capability-catalogue entries with
``provider="mcp.<name>"``.  The host never invents a connected state: the unit
is ``ready`` only after ``initialize`` + ``tools/list`` succeeded, ``failed``
when the process exits, and the host reconnects desired servers with backoff.

Tool calls always go through ``app.approvals`` (``shell.exec`` as the execution
capability, high risk) and are audited; a still-pending request returns
``status="pending"`` without executing anything (fail-closed).  Callers retry
with ``approval_id`` after a human/rule decision.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from ..capabilities import (
    DEFAULT_TOOL_CAPABILITY,
    ensure_approved,
    principal_identity,
)
from ..errors import (
    BadRequest,
    Conflict,
    NotFound,
    Unavailable,
)
from ..l2.eventlog import emit_both
from ..protocol import now_ms
from ..util.logging import get_logger
from ..util.redact import redact_text
from .client import MCPError, MCPStdioClient, MCPToolError

MAX_RECONNECT_DELAY_S = 60.0
MONITOR_INTERVAL_S = 5.0
MAX_STDERR_TAIL = 50


@dataclass
class ServerState:
    """Runtime state of one configured MCP server."""

    name: str
    config: dict
    state: str = "stopped"        # disabled|stopped|starting|ready|failed
    client: MCPStdioClient | None = None
    tools: list[dict] = field(default_factory=list)
    last_error: str | None = None
    last_connected: int | None = None
    failures: int = 0
    retry_at: float = 0.0
    executor_disposers: list = field(default_factory=list)
    manager: Any = None

    @property
    def unit_id(self) -> str:
        return f"mcp.{self.name}"

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    @property
    def connection(self) -> str:
        return {"ready": "connected", "starting": "starting",
                "failed": "failed", "stopped": "stopped",
                "disabled": "disabled"}.get(self.state, self.state)

    def unit_state(self) -> tuple[str, str | None]:
        if not self.enabled:
            return "disabled", "disabled in mcp.servers config"
        manager = self.manager
        if manager is not None and self.name in getattr(
                manager, "_manual_disabled", set()):
            return "disabled", "disabled by operator"
        if self.state == "ready":
            return "ready", None
        if self.state == "starting":
            return "starting", "connecting"
        if self.state == "failed":
            return "failed", self.last_error
        return "stopped", self.last_error

    def status_dict(self) -> dict:
        info: dict = {
            "name": self.name,
            "unit_id": self.unit_id,
            "state": self.state,
            "connection": self.connection,
            "enabled": self.enabled,
            "command": str(self.config.get("command") or ""),
            "args": list(self.config.get("args") or []),
            "cwd": self.config.get("cwd"),
            "framing": str(self.config.get("framing") or "newline"),
            "env_keys": sorted((self.config.get("env") or {}).keys()),
            "tools": [str(tool.get("name")) for tool in self.tools
                      if tool.get("name")],
            "tool_count": len(self.tools),
            "last_error": self.last_error,
            "last_connected": self.last_connected,
            "pid": (self.client.pid if self.client is not None
                    and self.client.alive else None),
            "stderr_tail": (list(self.client.stderr_tail)
                            if self.client is not None else []),
        }
        return info


class MCPHost:
    """MCP extension object: lifecycle, units, catalogue and approval gate."""

    name = "mcp"

    def __init__(self, app: Any) -> None:
        self.app = app
        self.log = get_logger("mcp")
        section = getattr(getattr(app, "config", None), "mcp", None)
        self.call_timeout_s = float(getattr(section, "call_timeout_s", 30.0)
                                    or 30.0)
        self.max_servers = int(getattr(section, "max_servers", 16) or 16)
        self.max_tools = int(getattr(section, "max_tools", 256) or 256)
        self._servers: dict[str, ServerState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._desired: set[str] = set()
        self._manual_disabled: set[str] = set()
        self._suppressed = False
        self._monitor_task: asyncio.Task | None = None
        self._retry_tasks: set[asyncio.Task] = set()
        self._stopped = False

    # -- configuration ----------------------------------------------------
    def _raw_servers(self) -> list[dict]:
        section = getattr(getattr(self.app, "config", None), "mcp", None)
        value = getattr(section, "servers", []) or []
        return [item for item in value if isinstance(item, dict)]

    def reload_servers(self) -> dict[str, ServerState]:
        servers: dict[str, ServerState] = {}
        for index, raw in enumerate(self._raw_servers()):
            name = str(raw.get("name") or "").strip()
            if not name or name == "0":
                if name == "0":
                    self.log.warning("MCP server name '0' is reserved for the "
                                     "host unit; ignored")
                continue
            if name in servers:
                self.log.warning("duplicate MCP server name %r ignored", name)
                continue
            if len(servers) >= self.max_servers:
                self.log.warning("mcp.max_servers reached; %r ignored", name)
                break
            command = str(raw.get("command") or "").strip()
            if not command:
                continue
            config = {
                "name": name,
                "command": command,
                "args": [str(item) for item in (raw.get("args") or [])],
                "env": {str(key): str(value)
                        for key, value in (raw.get("env") or {}).items()},
                "cwd": str(raw["cwd"]) if raw.get("cwd") else None,
                "enabled": bool(raw.get("enabled", True)),
                "framing": str(raw.get("framing") or "newline"),
            }
            state = ServerState(name=name, config=config, manager=self)
            if not config["enabled"]:
                state.state = "disabled"
            servers[name] = state
        self._servers = servers
        self._locks = {name: asyncio.Lock() for name in servers}
        return servers

    def servers(self) -> list[ServerState]:
        return list(self._servers.values())

    def get(self, name: str) -> ServerState:
        state = self._servers.get(str(name))
        if state is None:
            raise NotFound(f"unknown MCP server: {name!r}",
                           detail={"server": name})
        return state

    @property
    def _catalog(self):
        return getattr(self.app, "capability_catalog", None)

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        self._stopped = False
        self.reload_servers()
        for server in self._servers.values():
            if server.enabled:
                self._desired.add(server.name)
        await asyncio.gather(*[
            self._connect_quiet(name) for name in sorted(self._desired)
        ], return_exceptions=True)
        self._register_control()
        self._monitor_task = asyncio.create_task(
            self._monitor(), name="mcp-monitor")
        self._sync_host_unit()

    async def stop(self) -> None:
        self._stopped = True
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._monitor_task = None
        for server in list(self._servers.values()):
            if server.client is not None:
                await self._close_client(server)
        for task in list(self._retry_tasks):
            task.cancel()
        if self._retry_tasks:
            await asyncio.gather(*self._retry_tasks, return_exceptions=True)
            self._retry_tasks.clear()
        self._sync_host_unit()

    async def _connect_quiet(self, name: str) -> None:
        try:
            await self.connect(name, manual=False)
        except Exception as exc:  # noqa: BLE001 - connection failure is a state
            self.log.warning("MCP server %s did not connect: %s", name, exc)

    async def connect(self, name: str, *, manual: bool = True) -> dict:
        server = self.get(name)
        lock = self._locks.setdefault(server.name, asyncio.Lock())
        async with lock:
            if server.state == "ready" and server.client is not None \
                    and server.client.alive:
                self._desired.add(server.name)
                self._manual_disabled.discard(server.name)
                return server.status_dict()
            if server.state == "starting":
                raise Conflict(f"MCP server {name!r} is already starting")
            if self._suppressed and manual:
                raise Conflict("MCP host is disabled")
            self._manual_disabled.discard(server.name)
            if manual:
                server.config["enabled"] = True
            server.state = "starting"
            server.last_error = None
            self._sync_unit(server)
            self._emit("mcp.starting", {"server": server.name,
                                        "unit_id": server.unit_id})
            client = MCPStdioClient(
                server.name, server.config["command"],
                server.config.get("args") or [],
                env=server.config.get("env") or {},
                cwd=server.config.get("cwd"),
                framing=server.config.get("framing") or "newline",
                call_timeout_s=self.call_timeout_s,
                logger=self.log.getChild(server.name),
                on_exit=lambda reason, srv=server: self._on_client_exit(srv, reason),
            )
            server.client = client
            try:
                await client.start()
                tools = await client.list_tools()
                server.tools = [tool for tool in tools
                                if isinstance(tool, dict)][: self.max_tools]
                server.state = "ready"
                server.last_connected = now_ms()
                server.failures = 0
                server.retry_at = 0.0
                server.last_error = None
                if manual:
                    self._desired.add(server.name)
                self._register_executors(server)
            except Exception as exc:  # noqa: BLE001
                server.state = "failed"
                server.last_error = f"{type(exc).__name__}: {exc}"
                server.failures += 1
                server.retry_at = time.monotonic() + self._backoff(server.failures)
                await self._close_client(server)
                self._emit("mcp.connect_failed", {
                    "server": server.name, "unit_id": server.unit_id,
                    "error": redact_text(str(server.last_error))[:500],
                })
                self._sync_unit(server)
                self._sync_host_unit()
                raise Unavailable(
                    f"MCP server {name!r} failed to connect: {exc}",
                    detail={"server": server.name,
                            "error": server.last_error}) from exc
            self._emit("mcp.connected", {
                "server": server.name, "unit_id": server.unit_id,
                "tools": len(server.tools),
                "server_info": dict(client.server_info),
            })
            self.log.info("MCP server %s connected (%d tools)",
                          server.name, len(server.tools))
            self._sync_unit(server)
            self._sync_host_unit()
            return server.status_dict()

    async def disconnect(self, name: str, *, manual: bool = True) -> dict:
        server = self.get(name)
        lock = self._locks.setdefault(server.name, asyncio.Lock())
        async with lock:
            if manual:
                self._desired.discard(server.name)
                self._manual_disabled.add(server.name)
            await self._close_client(server)
            server.state = "stopped"
            server.last_error = None
            self._emit("mcp.disconnected", {
                "server": server.name, "unit_id": server.unit_id,
                "reason": "operator" if manual else "host",
            })
            self._sync_unit(server)
            self._sync_host_unit()
            return server.status_dict()

    async def _close_client(self, server: ServerState) -> None:
        for disposer in reversed(server.executor_disposers):
            try:
                disposer()
            except Exception:  # pragma: no cover
                pass
        server.executor_disposers = []
        server.tools = []
        client = server.client
        server.client = None
        if client is not None:
            try:
                await client.close()
            except Exception:  # pragma: no cover - shutdown best effort
                self.log.debug("closing MCP client %s failed",
                               server.name, exc_info=True)

    def _on_client_exit(self, server: ServerState, reason: str) -> None:
        """Called from the client reader when the subprocess dies."""
        if self._stopped:
            return
        server.state = "failed"
        server.last_error = f"process exited: {reason}"
        server.failures += 1
        server.retry_at = time.monotonic() + self._backoff(server.failures)
        for disposer in reversed(server.executor_disposers):
            try:
                disposer()
            except Exception:  # pragma: no cover
                pass
        server.executor_disposers = []
        server.tools = []
        if server.name in self._desired and not self._suppressed \
                and not self._stopped:
            task = asyncio.create_task(self._reconnect_later(server.name),
                                       name=f"mcp-reconnect-{server.name}")
            self._retry_tasks.add(task)
            task.add_done_callback(self._retry_tasks.discard)
        self._emit("mcp.disconnected", {
            "server": server.name, "unit_id": server.unit_id,
            "reason": redact_text(str(reason))[:200],
        })
        self._sync_unit(server)
        self._sync_host_unit()

    @staticmethod
    def _backoff(failures: int) -> float:
        return min(MAX_RECONNECT_DELAY_S, 5.0 * (2 ** max(0, failures - 1)))

    async def on_config(self, config) -> None:
        """Reload server definitions and reconnect what is still desired."""
        for server in list(self._servers.values()):
            if server.client is not None:
                await self._close_client(server)
        old_desired = set(self._desired)
        self.reload_servers()
        self._desired = {server.name for server in self._servers.values()
                         if server.enabled}
        if self._suppressed:
            return
        self._desired |= {name for name in old_desired
                          if name in self._servers}
        self._register_control()
        await asyncio.gather(*[self._connect_quiet(name)
                               for name in sorted(self._desired)],
                             return_exceptions=True)
        self._sync_host_unit()

    async def _reconnect_later(self, name: str, delay: float = 1.0) -> None:
        """First reconnect attempt right after a crash (monitor keeps retrying)."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self._stopped or name not in self._desired:
            return
        server = self._servers.get(name)
        if server is None or server.state in ("ready", "starting"):
            return
        try:
            await self.connect(name, manual=False)
        except Exception:  # noqa: BLE001 - the monitor retries with backoff
            return

    async def _monitor(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(MONITOR_INTERVAL_S)
            except asyncio.CancelledError:
                raise
            now = time.monotonic()
            for server in list(self._servers.values()):
                if server.name not in self._desired:
                    continue
                if server.state in ("ready", "starting"):
                    continue
                if now < server.retry_at:
                    continue
                try:
                    await self.connect(server.name, manual=False)
                except Exception:  # noqa: BLE001 - retried on the next tick
                    continue

    # -- catalogue --------------------------------------------------------
    def _register_executors(self, server: ServerState) -> None:
        catalog = self._catalog
        if catalog is None:
            return
        for tool in server.tools:
            tool_name = str(tool.get("name") or "").strip()
            if not tool_name:
                continue
            schema = tool.get("inputSchema") or tool.get("input_schema") or \
                {"type": "object", "properties": {}}
            entry = {
                "type": "mcp_tool",
                "name": tool_name,
                "description": str(tool.get("description") or ""),
                "capability": DEFAULT_TOOL_CAPABILITY,
                "risk": "high",
                "schema": schema if isinstance(schema, dict) else {},
                "provider": server.unit_id,
                "enabled_in": ["*"],
                "metadata": {"server": server.name},
            }
            try:
                disposer = catalog.register(entry, provider=server.unit_id)
                executor = self._make_executor(server.name, tool_name)
                executor_disposer = catalog.register_tool_executor(
                    server.unit_id, tool_name, executor)
            except Exception:  # pragma: no cover - catalogue is best effort
                self.log.debug("catalogue registration for %s failed",
                               tool_name, exc_info=True)
                continue
            server.executor_disposers.extend([disposer, executor_disposer])

    def _make_executor(self, server_name: str, tool_name: str):
        async def execute(args: dict, principal: str,
                          project_id: str | None,
                          approval_id: str | None = None) -> Any:
            return await self._raw_call(server_name, tool_name, args,
                                        principal=principal,
                                        project_id=project_id,
                                        approval_id=approval_id)

        execute.__name__ = f"mcp_{server_name}_{tool_name}"
        return execute

    def capabilities(self, kind: str | None = None) -> list[dict]:
        if kind not in (None, "mcp_tool"):
            return []
        result: list[dict] = []
        for server in self._servers.values():
            if server.state != "ready":
                continue
            for tool in server.tools:
                name = str(tool.get("name") or "")
                if not name:
                    continue
                result.append({
                    "type": "mcp_tool",
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "provider": server.unit_id,
                    "enabled_in": ["*"],
                    "risk": "high",
                    "schema": tool.get("inputSchema") or {},
                    "capability": DEFAULT_TOOL_CAPABILITY,
                    "metadata": {"server": server.name},
                })
        return result

    # -- calls / approval -------------------------------------------------
    async def call_tool(self, server_name: str, tool_name: str,
                        args: dict | None = None,
                        *, project_id: str | None = None,
                        principal: Any = None,
                        approval_id: str | None = None) -> dict:
        """Approval-gated tool call used by ``/api/mcp/call``."""
        server = self.get(server_name)
        if server.state != "ready":
            raise Conflict(
                f"MCP server {server_name!r} is not connected "
                f"(state={server.state})",
                detail={"server": server_name, "state": server.state})
        tool = str(tool_name or "").strip()
        if not tool:
            raise BadRequest("tool name is required")
        if not any(str(item.get("name")) == tool for item in server.tools):
            raise NotFound(
                f"MCP server {server_name!r} exposes no tool {tool!r}",
                detail={"server": server_name, "tool": tool})
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise BadRequest("tool args must be an object")
        identity = principal_identity(principal or f"mcp.{server_name}")
        scope = {"command": f"mcp.{server_name}:{tool}"}
        if project_id:
            scope["project_id"] = str(project_id)
        gate = await ensure_approved(
            self.app, principal=identity, capability=DEFAULT_TOOL_CAPABILITY,
            scope=scope, project_id=(str(project_id) if project_id else None),
            reason=f"MCP tool {server_name}.{tool}",
            preview=redact_text(str({"server": server_name, "tool": tool,
                                     "args": args}))[:2000],
            risk="high", approval_id=approval_id)
        if not gate.get("approved"):
            self._emit("mcp.call_pending", {
                "server": server_name, "tool": tool, "principal": identity,
                "approval_id": gate.get("approval_id"),
                "status": gate.get("status"),
            })
            return {"status": str(gate.get("status") or "denied"),
                    "executed": False, "server": server_name, "tool": tool,
                    "approval_id": gate.get("approval_id"),
                    "reason": gate.get("reason")}
        result = await self._raw_call(
            server_name, tool, args, principal=identity,
            project_id=(str(project_id) if project_id else None),
            approval_id=gate.get("approval_id"))
        return {"status": "ok", "executed": True, "server": server_name,
                "tool": tool, "approval_id": gate.get("approval_id"),
                "result": result}

    async def call_with_approval(self, server_name: str, tool_name: str,
                                 args: dict | None = None, *,
                                 project_id: str | None = None,
                                 principal: Any = None,
                                 approval_id: str | None = None) -> dict:
        """Named alias for :meth:`call_tool` (approval-gated MCP execution)."""
        return await self.call_tool(
            server_name, tool_name, args, project_id=project_id,
            principal=principal, approval_id=approval_id)

    async def _raw_call(self, server_name: str, tool_name: str,
                        args: dict, *, principal: str,
                        project_id: str | None,
                        approval_id: str | None = None) -> dict:
        """Execute a tool after approval; audited, no further approval."""
        server = self.get(server_name)
        if server.client is None or server.state != "ready":
            raise Unavailable(
                f"MCP server {server_name!r} disconnected",
                detail={"server": server_name, "state": server.state})
        tool = str(tool_name or "").strip()
        if not any(str(item.get("name")) == tool for item in server.tools):
            raise NotFound(f"MCP server {server_name!r} exposes no tool {tool!r}")
        try:
            result = await server.client.call_tool(tool, dict(args or {}))
        except MCPToolError as exc:
            self._audit_failure(server, tool, principal, project_id,
                                approval_id, exc)
            raise Unavailable(
                f"MCP tool {server_name}.{tool} failed: {exc}",
                detail={"server": server_name, "tool": tool}) from exc
        except MCPError as exc:
            self._audit_failure(server, tool, principal, project_id,
                                approval_id, exc)
            raise Unavailable(
                f"MCP protocol error calling {server_name}.{tool}: {exc}",
                detail={"server": server_name, "tool": tool}) from exc
        except Exception as exc:  # noqa: BLE001
            self._audit_failure(server, tool, principal, project_id,
                                approval_id, exc)
            raise
        self._emit("mcp.tool.called", {
            "server": server_name, "tool": tool, "principal": principal,
            "approval_id": approval_id, "ok": True,
            "args_preview": redact_text(str(args))[:1000],
        }, project_id=project_id)
        return result

    def _audit_failure(self, server: ServerState, tool: str, principal: str,
                       project_id: str | None, approval_id: str | None,
                       exc: Exception) -> None:
        self._emit("mcp.tool.failed", {
            "server": server.name, "tool": tool, "principal": principal,
            "approval_id": approval_id, "error": type(exc).__name__,
            "message": redact_text(str(exc))[:500],
        }, project_id=project_id)

    # -- units ------------------------------------------------------------
    def _server_unit(self, server: ServerState) -> dict:
        state, reason = server.unit_state()
        client = server.client
        now = now_ms()
        config = getattr(self.app, "config", None)
        host = getattr(getattr(config, "system", None), "host_label", "")
        descriptor = {
            "work_id": server.unit_id,
            "unit_id": server.unit_id,
            "kind": "mcp",
            "role": "tool-provider",
            "tier": "service",
            "label": f"MCP · {server.name}",
            "project_id": None,
            "pid": client.pid if client is not None and client.alive else None,
            "host": host,
            "transport": "stdio",
            "rpc_address": None,
            "events_address": None,
            "status": {"ready": "online", "degraded": "degraded",
                       "failed": "failed", "starting": "starting",
                       "stopped": "offline", "disabled": "disabled"}.get(
                           state, "online"),
            "state": state,
            "reason": reason,
            "abilities": [{"name": "mcp.connect", "scope": "system"},
                          {"name": "mcp.list_tools", "scope": "service"},
                          {"name": "mcp.call_tool", "scope": "service"}],
            "hwinfo": {},
            "started_at": server.last_connected or getattr(self.app,
                                                           "started_at", now),
            "last_seen": server.last_connected or now,
            "restart_count": 0,
            "metadata": {
                "description": f"MCP stdio server {server.name}",
                "connection": server.connection,
                "command": server.config.get("command"),
                "args": list(server.config.get("args") or []),
                "framing": server.config.get("framing") or "newline",
                "env_keys": sorted((server.config.get("env") or {}).keys()),
                "tools": [str(tool.get("name")) for tool in server.tools
                          if tool.get("name")],
                "last_error": server.last_error,
            },
            "runtime": {"mode": "subprocess", "host_unit": "sys.0",
                        "host": host,
                        "pid": client.pid if client is not None and client.alive
                        else None,
                        "module": f"mcp:{server.config.get('command')}",
                        "started_at": server.last_connected or
                        getattr(self.app, "started_at", now)},
            "endpoints": [{"name": "stdio", "transport": "stdio",
                           "address": f"stdio://{server.name}"}],
            "ref": f"unit://{server.unit_id}",
            "critical": False,
            "requires": ["approval.0", "tool.0"],
            "actions": ["probe", "restart", "disable", "enable",
                        "drain", "undrain"],
            "lease": {"type": "probe", "ttl_s": int(self.call_timeout_s * 2),
                      "interval_s": 5,
                      "renewed_at": server.last_connected or now},
            "provides": {"tools": len(server.tools)},
        }
        return self._overlay(descriptor)

    def _host_state_reason(self) -> tuple[str, str | None]:
        enabled = [server for server in self._servers.values()
                   if server.enabled]
        failed = [server for server in enabled if server.state == "failed"]
        if self._suppressed:
            return "disabled", "disabled by operator"
        if failed:
            return "degraded", (f"{len(failed)} server(s) failed: " + ", ".join(
                server.name for server in failed[:5]))
        return "ready", None

    def _host_unit(self) -> dict:
        servers = list(self._servers.values())
        enabled = [server for server in servers if server.enabled]
        failed = [server for server in enabled if server.state == "failed"]
        connected = [server for server in enabled if server.state == "ready"]
        state, reason = self._host_state_reason()
        now = now_ms()
        config = getattr(self.app, "config", None)
        host = getattr(getattr(config, "system", None), "host_label", "")
        descriptor = {
            "work_id": "mcp.0",
            "unit_id": "mcp.0",
            "kind": "mcp",
            "role": "host",
            "tier": "service",
            "label": "MCP 宿主",
            "project_id": None,
            "pid": None,
            "host": host,
            "transport": "inproc",
            "rpc_address": None,
            "events_address": None,
            "status": {"ready": "online", "degraded": "degraded",
                       "failed": "failed", "disabled": "disabled"}.get(
                           state, "online"),
            "state": state,
            "reason": reason,
            "abilities": [{"name": "mcp.connect", "scope": "system"},
                          {"name": "mcp.list_tools", "scope": "system"},
                          {"name": "mcp.call_tool", "scope": "system"}],
            "hwinfo": {},
            "started_at": getattr(self.app, "started_at", now),
            "last_seen": now,
            "restart_count": 0,
            "metadata": {
                "description": "MCP server 连接器；每个已配置 server 登记为 mcp.<name>",
                "servers": {server.name: server.state for server in servers},
            },
            "runtime": {"mode": "in_process", "host_unit": "sys.0",
                        "host": host, "pid": None,
                        "module": "stackflowos.mcp:MCPHost",
                        "started_at": getattr(self.app, "started_at", now)},
            "endpoints": [{"name": "inproc", "transport": "inproc",
                           "address": "mcp.0"}],
            "ref": "unit://mcp.0",
            "critical": False,
            "requires": ["approval.0", "tool.0"],
            "actions": ["probe", "restart", "disable", "enable",
                        "drain", "undrain"],
            "lease": {"type": "host", "host_unit": "sys.0",
                      "interval_s": 5, "renewed_at": now},
            "provides": {"servers": len(servers),
                         "connected": len(connected),
                         "tools": sum(len(server.tools) for server in enabled)},
        }
        return self._overlay(descriptor)

    def _overlay(self, descriptor: dict) -> dict:
        control = getattr(self.app, "unit_control", None)
        overlay = getattr(control, "overlay", None)
        if overlay is None:
            return descriptor
        try:
            return overlay(descriptor)
        except Exception:  # pragma: no cover
            return descriptor

    def units(self) -> list[dict]:
        units = [self._host_unit()]
        units.extend(self._server_unit(server) for server in self._servers.values())
        return units

    def unit_state(self, name: str) -> str | None:
        if name == "mcp.0":
            return self._host_unit()["state"]
        server_name = name[4:] if name.startswith("mcp.") else name
        server = self._servers.get(server_name)
        if server is None:
            return None
        return server.unit_state()[0]

    # -- control hooks ----------------------------------------------------
    def _register_control(self) -> None:
        control = getattr(self.app, "unit_control", None)
        register = getattr(control, "register_provider", None)
        if register is None:
            return
        try:
            register("mcp.0", probe=self.probe_host, restart=self.restart_host,
                     disable=self.disable_host, enable=self.enable_host)
        except Exception:  # pragma: no cover
            self.log.debug("mcp.0 control registration failed", exc_info=True)
        for server in self._servers.values():
            self._register_server_control(server)
        self._sync_host_unit()

    def _register_server_control(self, server: ServerState) -> None:
        control = getattr(self.app, "unit_control", None)
        register = getattr(control, "register_provider", None)
        if register is None:
            return

        async def probe(name: str = server.name) -> dict:
            current = self._servers.get(name)
            if current is None:
                return {"ok": False, "reason": "server disappeared"}
            state, reason = current.unit_state()
            result = {"ok": state in ("ready", "disabled", "starting"),
                      "detail": f"state={state}",
                      "checks": [{"name": "connection",
                                  "ok": state == "ready",
                                  "detail": current.connection}]}
            if state == "failed":
                result["ok"] = False
                result["reason"] = reason
            return result

        async def restart(name: str = server.name) -> dict:
            try:
                await self.disconnect(name, manual=False)
            except Exception:  # pragma: no cover - already stopped is fine
                pass
            return await self.connect(name, manual=False)

        async def disable(name: str = server.name) -> None:
            self._manual_disabled.add(name)
            self._desired.discard(name)
            try:
                await self.disconnect(name, manual=True)
            except Exception:  # pragma: no cover
                pass
            current = self._servers.get(name)
            if current is not None:
                current.state = "disabled"
                self._sync_unit(current)

        async def enable(name: str = server.name) -> None:
            self._manual_disabled.discard(name)
            self._desired.add(name)
            current = self._servers.get(name)
            if current is not None:
                current.config["enabled"] = True
            await self.connect(name, manual=True)

        try:
            register(server.unit_id, probe=probe, restart=restart,
                     disable=disable, enable=enable)
        except Exception:  # pragma: no cover
            self.log.debug("mcp control registration failed for %s",
                           server.name, exc_info=True)
        self._sync_unit(server)

    async def restart_host(self) -> dict:
        for server in list(self._servers.values()):
            try:
                await self.disconnect(server.name, manual=False)
            except Exception:  # pragma: no cover
                pass
        self._suppressed = False
        self.reload_servers()
        self._desired = {server.name for server in self._servers.values()
                         if server.enabled}
        await asyncio.gather(*[self._connect_quiet(name)
                               for name in sorted(self._desired)],
                             return_exceptions=True)
        self._register_control()
        self._sync_host_unit()
        return {"servers": len(self._servers),
                "connected": sum(1 for server in self._servers.values()
                                 if server.state == "ready")}

    async def disable_host(self) -> None:
        self._suppressed = True
        for server in list(self._servers.values()):
            try:
                await self.disconnect(server.name, manual=True)
            except Exception:  # pragma: no cover
                pass
        self._sync_host_unit()

    async def enable_host(self) -> None:
        self._suppressed = False
        self._desired = {server.name for server in self._servers.values()
                         if server.enabled}
        await asyncio.gather(*[self._connect_quiet(name)
                               for name in sorted(self._desired)],
                             return_exceptions=True)
        self._sync_host_unit()

    def probe_host(self) -> dict:
        failed = [server for server in self._servers.values()
                  if server.enabled and server.state == "failed"]
        result = {"ok": not failed,
                  "checks": [{"name": "servers",
                              "ok": not failed,
                              "detail": f"{len(self._servers)} configured"}]}
        if failed:
            result["sticky_state"] = "degraded"
            result["reason"] = f"{len(failed)} server(s) failed"
        return result

    def _sync_unit(self, server: ServerState) -> None:
        control = getattr(self.app, "unit_control", None)
        sync = getattr(control, "sync_state", None)
        if sync is None:
            return
        try:
            state, reason = server.unit_state()
            sync(server.unit_id, state, reason)
        except Exception:  # pragma: no cover
            pass

    def _sync_host_unit(self) -> None:
        control = getattr(self.app, "unit_control", None)
        sync = getattr(control, "sync_state", None)
        if sync is None:
            return
        try:
            state, reason = self._host_state_reason()
            sync("mcp.0", state, reason)
        except Exception:  # pragma: no cover
            pass

    # -- HTTP -------------------------------------------------------------
    def routes(self) -> list[tuple[str, str, Any]]:
        return [
            ("GET", r"/api/mcp/servers", self.api_servers),
            ("POST", r"/api/mcp/servers/([^/]+)/connect", self.api_connect),
            ("POST", r"/api/mcp/servers/([^/]+)/disconnect", self.api_disconnect),
            ("GET", r"/api/mcp/tools", self.api_tools),
            ("POST", r"/api/mcp/call", self.api_call),
        ]

    async def api_servers(self, match, query, body):
        return 200, {"ok": True, "data": {
            "servers": [server.status_dict()
                        for server in self._servers.values()],
            "summary": self.health(),
            "unit_id": "mcp.0",
        }}

    async def api_connect(self, match, query, body):
        self._require_operator()
        status = await self.connect(match.group(1), manual=True)
        return 200, {"ok": True, "data": {"server": status}}

    async def api_disconnect(self, match, query, body):
        self._require_operator()
        status = await self.disconnect(match.group(1), manual=True)
        return 200, {"ok": True, "data": {"server": status}}

    async def api_tools(self, match, query, body):
        server_filter = query.get("server") or None
        tools: list[dict] = []
        for server in self._servers.values():
            if server_filter is not None and server.name != str(server_filter):
                continue
            for tool in server.tools:
                tools.append({
                    "server": server.name,
                    "unit_id": server.unit_id,
                    "name": str(tool.get("name") or ""),
                    "description": str(tool.get("description") or ""),
                    "inputSchema": tool.get("inputSchema") or {},
                    "available": server.state == "ready",
                })
        return 200, {"ok": True, "data": {"tools": tools,
                                          "count": len(tools),
                                          "server": server_filter}}

    async def api_call(self, match, query, body):
        self._require_operator()
        body = body or {}
        server_name = str(body.get("server") or "").strip()
        tool_name = str(body.get("tool") or "").strip()
        if not server_name or not tool_name:
            raise BadRequest("server and tool are required")
        args = body.get("args") or {}
        project_id = body.get("project_id") or None
        if isinstance(project_id, str):
            project_id = project_id.strip() or None
        approval_id = body.get("approval_id") or None
        result = await self.call_tool(
            server_name, tool_name, args, project_id=project_id,
            principal=f"mcp.{server_name}",
            approval_id=(str(approval_id) if approval_id else None))
        status = 202 if result.get("status") == "pending" else 200
        return status, {"ok": True, "data": result}

    def _require_operator(self) -> None:
        from ..web.api import current_auth
        role = getattr(current_auth(), "role", None)
        if role is not None and role != "operator":
            raise BadRequest("operator role required")

    def health(self) -> dict:
        servers = list(self._servers.values())
        enabled = [server for server in servers if server.enabled]
        return {
            "ok": not any(server.state == "failed" for server in enabled),
            "servers": len(servers),
            "enabled": len(enabled),
            "connected": sum(1 for server in enabled
                             if server.state == "ready"),
            "failed": sum(1 for server in enabled
                          if server.state == "failed"),
            "tools": sum(len(server.tools) for server in enabled),
            "suppressed": self._suppressed,
        }

    # -- events -----------------------------------------------------------
    def _emit(self, event_type: str, data: dict,
              project_id: str | None = None) -> None:
        ctx = getattr(self.app, "ctx", None)
        eventlog = getattr(self.app, "eventlog", None)
        if ctx is None or eventlog is None:
            return
        try:
            safe = {key: (redact_text(value) if isinstance(value, str) else value)
                    for key, value in data.items()}
            emit_both(ctx, eventlog, event_type, safe, project_id)
        except Exception:  # pragma: no cover
            self.log.debug("mcp event %s failed", event_type, exc_info=True)


def build(app: Any) -> MCPHost:
    host = MCPHost(app)
    app.mcp_host = host
    app.mcp = host
    return host


__all__ = ["MAX_RECONNECT_DELAY_S", "MCPHost", "ServerState", "build"]

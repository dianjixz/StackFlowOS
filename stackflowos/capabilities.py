"""Unified capability catalogue (``/api/capabilities`` + ``capability.call``).

The catalogue is the *data* side of the unit model: tools, skills, plugins and
MCP tools are entries here, while the things that host/execute them are units
(``tool.0``, ``skill.0``, ``plugin.<id>``, ``mcp.<name>``).  An entry therefore
carries an explicit ``provider`` and a derived ``available`` flag -- it is never
the provider's own truth, only a projection of the provider unit's state.

Providers contribute in two ways:

* extensions implement ``capabilities(kind)`` (the documented contract), and
  this catalogue aggregates them on every request;
* in-process providers (plugins/MCP) may also call :meth:`register` and
  :meth:`register_tool_executor` at runtime.  Registrations return a disposer
  so a plugin unload removes its entries atomically.

``call_capability`` is the single execution entry point for worker RPC
(``capability.call``): it resolves an *available* catalogue entry, routes it to
the registered executor and -- critically -- always passes through
``app.approvals`` first.  Built-in tools (``tool.0``) are rejected: they are
executed by the worker's own executor, this path only dispatches extension
providers.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable

from .errors import (
    BadRequest,
    Conflict,
    Denied,
    InternalError,
    NotFound,
    SfoError,
    Unavailable,
)
from .security.capabilities import CAPABILITIES, RISK_ORDER
from .util.logging import get_logger
from .util.redact import redact_text

#: Catalogue entry types accepted by ``/api/capabilities``.
TYPES: tuple[str, ...] = ("tool", "skill", "plugin", "mcp_tool")

#: Entry types that name an executable tool.
EXECUTABLE_TYPES = frozenset({"tool", "mcp_tool"})

#: Built-in tools are executed inside the worker, never through this module.
BUILTIN_PROVIDER = "tool.0"

#: Placeholder capability for tools whose provider has no real security
#: capability.  It is the high-risk execution primitive, so the default is
#: fail-closed (approval required) rather than silently allowed.
DEFAULT_TOOL_CAPABILITY = "shell.exec"


def _max_risk(*levels: str | None) -> str:
    best = "low"
    for value in levels:
        if value and RISK_ORDER.get(str(value), 0) > RISK_ORDER.get(best, 0):
            best = str(value)
    return best


def tool_schema(spec: Any) -> dict:
    """Minimal JSON-schema for a ``ToolSpec``'s ``args`` mapping.

    The spec stores ``{"arg": "string"}``; that is enough to validate arity and
    types, so we do not invent a large schema the executor does not enforce.
    """
    args = getattr(spec, "args", {}) or {}
    properties: dict[str, dict] = {}
    for name, kind in args.items():
        kind = str(kind or "string")
        if kind in ("string", "integer", "number", "boolean", "object", "array"):
            properties[str(name)] = {"type": kind}
        else:
            properties[str(name)] = {"type": "string", "description": kind}
    return {"type": "object", "properties": properties, "required": []}


def tool_risk(spec: Any) -> str:
    """Risk derived from the tool spec *and* the security capability table."""
    risk_hint = str(getattr(spec, "risk_hint", "low") or "low")
    capability = str(getattr(spec, "capability", "") or "")
    base = CAPABILITIES[capability].risk if capability in CAPABILITIES else None
    return _max_risk(risk_hint, base)


def tool_entries() -> list[dict]:
    """Every built-in ``agents.tools.TOOLS`` entry as ``type=tool``."""
    from .agents.tools import TOOLS

    entries: list[dict] = []
    for name in sorted(TOOLS):
        spec = TOOLS[name]
        entries.append({
            "type": "tool",
            "name": name,
            "description": str(getattr(spec, "description", "") or ""),
            "capability": str(getattr(spec, "capability", "") or ""),
            "risk": tool_risk(spec),
            "schema": tool_schema(spec),
            "args": dict(getattr(spec, "args", {}) or {}),
            "provider": BUILTIN_PROVIDER,
            "enabled_in": ["*"],
            "metadata": {"source": "agents.tools.TOOLS"},
        })
    return entries


def provider_state(app: Any, unit_id: str,
                   states: dict[str, str] | None = None) -> str | None:
    """The provider unit's current state (control module first, then app)."""
    if states is not None and unit_id in states:
        return states[unit_id]
    control = getattr(app, "unit_control", None)
    if control is not None:
        try:
            state = control.state_of(unit_id)
        except Exception:  # pragma: no cover - diagnostics must never break
            state = None
        if state:
            return str(state)
    try:
        for unit in app.unit_cards():
            if str(unit.get("unit_id") or unit.get("work_id")) == unit_id:
                return str(unit.get("state") or unit.get("status") or "")
    except Exception:  # pragma: no cover - a broken unit must not hide tools
        return None
    return None


def _provider_states(app: Any) -> dict[str, str]:
    states: dict[str, str] = {}
    control = getattr(app, "unit_control", None)
    if control is not None:
        try:
            states.update({str(k): str(v) for k, v in control.states().items()})
        except Exception:  # pragma: no cover
            pass
    try:
        for unit in app.unit_cards():
            unit_id = str(unit.get("unit_id") or unit.get("work_id"))
            if unit_id:
                states.setdefault(unit_id,
                                  str(unit.get("state") or unit.get("status") or ""))
    except Exception:  # pragma: no cover
        pass
    return states


def _principal_identity(principal: Any) -> str:
    """Approval principal string for a :class:`security.Principal` or str."""
    identity = getattr(principal, "identity", None)
    if identity:
        return str(identity)
    if principal is None:
        return "human:operator"
    return str(principal)


def principal_identity(principal: Any) -> str:
    """Public alias used by MCP/plugin execution paths."""
    return _principal_identity(principal)


def _resolve_scope(principal: Any, project_id: str | None) -> tuple[str, str | None]:
    """Bind a caller to its project scope (unit principals cannot cross over)."""
    identity = _principal_identity(principal)
    kind = str(getattr(principal, "kind", "") or "")
    if kind == "unit":
        own = getattr(principal, "project_id", None)
        requested = project_id
        if own:
            own = str(own)
            if requested is not None and str(requested) != own:
                raise Denied(
                    f"{getattr(principal, 'id', identity)} may not call "
                    f"capabilities of project {requested!r}")
            return identity, own
        if requested is not None:
            raise Denied("unit principal has no project scope")
        return identity, None
    return identity, (str(project_id) if project_id else None)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _json_preview(value: Any, limit: int = 2000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return redact_text(text)[:limit]


async def ensure_approved(app: Any, *, principal: str,
                          capability: str, scope: dict,
                          project_id: str | None, reason: str,
                          preview: str, risk: str | None = None,
                          approval_id: str | None = None) -> dict:
    """Run the broker's approval path and report whether execution may happen.

    Fail-closed by construction: a missing broker, a denied/expired request, a
    still-pending request or an already-claimed one-shot grant all return
    ``approved=False`` -- the caller must not execute anything.
    """
    broker = getattr(app, "approvals", None)
    if broker is None:
        return {"approved": False, "status": "denied",
                "reason": "approval broker unavailable"}
    try:
        if approval_id:
            request = broker.poll(str(approval_id), principal=principal)
        else:
            request = broker.request(
                principal, capability, dict(scope or {}),
                reason=reason, preview=preview,
                project_id=project_id, risk=risk)
    except Denied:
        raise
    except SfoError as exc:
        return {"approved": False, "status": "denied",
                "reason": exc.message, "error": exc.to_dict()}
    status = str(getattr(request, "status", ""))
    approval = {"approval_id": getattr(request, "id", None),
                "status": status}
    if status not in ("approved",):
        return {"approved": False, **approval,
                "reason": str(getattr(request, "note", "") or status)}
    if str(getattr(request, "mode", "") or "") == "once":
        if getattr(request, "claim_id", None):
            return {"approved": False, "approval_id": approval["approval_id"],
                    "status": "claimed",
                    "reason": "one-shot approval already used"}
        try:
            broker.claim(request.id, principal=principal)
        except SfoError as exc:
            return {"approved": False, "approval_id": approval["approval_id"],
                    "status": "claimed", "reason": exc.message,
                    "error": exc.to_dict()}
    return {"approved": True, **approval}


class CapabilityCatalog:
    """Aggregated, provider-aware capability catalogue extension."""

    name = "capabilities"

    def __init__(self, app: Any) -> None:
        self.app = app
        self.log = get_logger("capabilities")
        self._tools: list[dict] = tool_entries()
        self._dynamic: dict[tuple[str, str, str], dict] = {}
        self._executors: dict[tuple[str, str], Callable[..., Any]] = {}

    # -- registration -----------------------------------------------------
    @staticmethod
    def _key(entry: dict) -> tuple[str, str, str]:
        return (str(entry.get("type") or ""), str(entry.get("name") or ""),
                str(entry.get("provider") or ""))

    def register(self, entry: dict, *, provider: str | None = None) -> Callable[[], None]:
        """Register one live catalogue entry; returns an idempotent disposer."""
        if not isinstance(entry, dict):
            raise BadRequest("capability entry must be an object")
        item = dict(entry)
        item.setdefault("type", "tool")
        item.setdefault("provider", provider or BUILTIN_PROVIDER)
        item.setdefault("enabled_in", ["*"])
        item.setdefault("available", True)
        if item["type"] not in TYPES:
            raise BadRequest(f"unsupported capability type: {item['type']!r}")
        if not item.get("name"):
            raise BadRequest("capability entry requires a name")
        key = self._key(item)
        self._dynamic[key] = item

        def remove() -> None:
            if self._dynamic.get(key) is item:
                del self._dynamic[key]

        return remove

    def register_tool_executor(self, provider: str, name: str,
                               executor: Callable[..., Any]) -> Callable[[], None]:
        """Register the async callable that executes one extension tool."""
        key = (str(provider), str(name))
        self._executors[key] = executor

        def remove() -> None:
            if self._executors.get(key) is executor:
                del self._executors[key]

        return remove

    def executor_for(self, provider: str, name: str) -> Callable[..., Any] | None:
        return self._executors.get((str(provider), str(name)))

    # -- listing ----------------------------------------------------------
    def _contributed(self, kind: str | None) -> list[dict]:
        entries: list[dict] = []
        for extension in getattr(self.app, "extensions", []) or []:
            if extension is self:
                continue
            hook = getattr(extension, "capabilities", None)
            if hook is None:
                continue
            try:
                value = hook(kind) if kind is not None else hook(None)
            except TypeError:
                try:
                    value = hook()
                except Exception:
                    self.log.exception("extension %r failed to list capabilities",
                                       getattr(extension, "name", "?"))
                    continue
            except Exception:
                self.log.exception("extension %r failed to list capabilities",
                                   getattr(extension, "name", "?"))
                continue
            if isinstance(value, list):
                entries.extend(item for item in value if isinstance(item, dict))
        return entries

    def raw_entries(self, kind: str | None = None) -> list[dict]:
        """All entries before project/availability projection, deduplicated."""
        merged: dict[tuple[str, str, str], dict] = {}
        for entry in self._tools:
            merged.setdefault(self._key(entry), dict(entry))
        for key, entry in self._dynamic.items():
            merged[key] = dict(entry)
        for entry in self._contributed(kind):
            item = dict(entry)
            item.setdefault("provider", BUILTIN_PROVIDER)
            item.setdefault("enabled_in", ["*"])
            item.setdefault("type", "tool")
            if item["type"] not in TYPES:
                continue
            merged.setdefault(self._key(item), item)
        result = list(merged.values())
        if kind is not None:
            result = [item for item in result if item.get("type") == kind]
        result.sort(key=lambda item: (str(item.get("type")), str(item.get("name")),
                                      str(item.get("provider"))))
        return result

    @staticmethod
    def _visible_in(entry: dict, project_id: str | None) -> bool:
        enabled_in = entry.get("enabled_in")
        if enabled_in is None:
            enabled_in = ["*"]
        if not isinstance(enabled_in, (list, tuple, set)):
            enabled_in = [enabled_in]
        scopes = [str(item) for item in enabled_in]
        if project_id is None:
            return "*" in scopes
        return "*" in scopes or str(project_id) in scopes

    def entries(self, kind: str | None = None,
                project_id: str | None = None) -> list[dict]:
        """Catalogue entries projected for one project (validated scope)."""
        if project_id is not None:
            self._require_project(project_id)
        states = _provider_states(self.app)
        result: list[dict] = []
        for entry in self.raw_entries(kind):
            if not self._visible_in(entry, project_id):
                continue
            item = dict(entry)
            provider = str(item.get("provider") or "")
            state = provider_state(self.app, provider, states) if provider else None
            item["provider_state"] = state
            item["available"] = bool(state == "ready")
            item["enabled_in"] = list(item.get("enabled_in") or ["*"])
            item.setdefault("metadata", {})
            result.append(item)
        return result

    def capabilities(self, kind: str | None = None) -> list[dict]:
        """Extension hook: global catalogue view (``enabled_in`` includes ``*``)."""
        return self.entries(kind=kind, project_id=None)

    def find_tool(self, name: str, project_id: str | None = None,
                  *, provider: str | None = None) -> list[dict]:
        candidates = [
            entry for entry in self.entries(project_id=project_id)
            if entry.get("type") in EXECUTABLE_TYPES
            and (entry.get("name") == name
                 or f"{entry.get('provider')}::{entry.get('name')}" == name)
            and (provider is None or entry.get("provider") == provider)
        ]
        return candidates

    def _require_project(self, project_id: str) -> None:
        projects = getattr(self.app, "projects", None)
        if projects is None:
            raise NotFound(f"project {project_id!r} not found")
        projects.get(project_id)  # raises NotFound

    # -- execution (RPC capability.call) ---------------------------------
    async def call_capability(self, name: str, args: dict,
                              project_id: str | None,
                              principal: Any,
                              approval_id: str | None = None) -> dict:
        """Resolve + approve + execute one catalogue tool.

        Only available entries can run.  Approval is mandatory (fail-closed):
        a pending request is returned as a *result* (``status="pending"``) and
        the tool is not invoked.  Callers may retry with ``approval_id`` once a
        human/rule approves it.
        """
        tool_name = str(name or "").strip()
        if not tool_name:
            raise BadRequest("capability name is required")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise BadRequest("capability args must be an object")
        identity, effective_project = _resolve_scope(principal, project_id)
        candidates = self.find_tool(tool_name, effective_project)
        if not candidates:
            raise NotFound(f"no available capability named {tool_name!r}",
                           detail={"project_id": effective_project})
        if len(candidates) > 1:
            available = [item for item in candidates if item.get("available")]
            if len(available) != 1:
                raise Conflict(
                    f"capability {tool_name!r} is provided by multiple "
                    f"providers: {sorted(str(i.get('provider')) for i in candidates)}",
                    detail={"providers": sorted(str(i.get("provider"))
                                                for i in candidates)})
            candidates = available
        entry = candidates[0]
        provider = str(entry.get("provider") or "")
        if provider == BUILTIN_PROVIDER:
            raise BadRequest(
                f"{tool_name!r} is a built-in tool; the worker executor handles it")
        if not entry.get("available"):
            raise Unavailable(
                f"capability {tool_name!r} is unavailable (provider {provider} "
                f"state={entry.get('provider_state')!r})",
                detail={"provider": provider,
                        "provider_state": entry.get("provider_state")})
        executor = self.executor_for(provider, str(entry.get("name")))
        if executor is None:
            raise Unavailable(
                f"provider {provider!r} registered no executor for "
                f"{entry.get('name')!r}")

        capability = str(entry.get("capability") or DEFAULT_TOOL_CAPABILITY)
        if capability not in CAPABILITIES:
            capability = DEFAULT_TOOL_CAPABILITY
        risk = _max_risk(str(entry.get("risk") or "high"))
        scope = {"command": f"{provider}:{entry.get('name')}"}
        if effective_project:
            scope["project_id"] = effective_project
        gate = await ensure_approved(
            self.app, principal=identity, capability=capability, scope=scope,
            project_id=effective_project,
            reason=f"capability {entry.get('name')} via {provider}",
            preview=_json_preview({"tool": entry.get("name"),
                                   "provider": provider, "args": args}),
            risk=risk, approval_id=approval_id)
        base = {"tool": entry.get("name"), "provider": provider,
                "approval_id": gate.get("approval_id")}
        if not gate.get("approved"):
            self.app.eventlog.append("capability.pending", {
                **base, "principal": identity, "status": gate.get("status"),
                "reason": gate.get("reason"),
            }, source="sys", project_id=effective_project)
            return {**base, "status": str(gate.get("status") or "denied"),
                    "executed": False, "reason": gate.get("reason")}

        try:
            result = await _maybe_await(executor(
                dict(args), identity, effective_project,
                approval_id=gate.get("approval_id")))
        except Exception as exc:  # noqa: BLE001 - audited, then surfaced
            self.app.eventlog.append("capability.failed", {
                **base, "principal": identity, "error": type(exc).__name__,
                "message": redact_text(str(exc))[:500],
            }, source="sys", project_id=effective_project)
            if isinstance(exc, SfoError):
                raise
            raise InternalError(
                f"capability {entry.get('name')!r} failed: {type(exc).__name__}",
                detail={"provider": provider}) from exc
        self.app.eventlog.append("capability.called", {
            **base, "principal": identity, "ok": True,
            "args_preview": _json_preview(args),
        }, source="sys", project_id=effective_project)
        return {**base, "status": "ok", "executed": True, "result": result}

    # -- HTTP -------------------------------------------------------------
    def routes(self) -> list[tuple[str, str, Any]]:
        return [("GET", r"/api/capabilities", self.api_capabilities)]

    async def api_capabilities(self, match, query, body):
        kind = query.get("type") or query.get("kind") or None
        kind = str(kind) if kind else None
        if kind is not None and kind not in TYPES:
            raise BadRequest(
                f"type must be one of {list(TYPES)}", detail={"types": list(TYPES)})
        project_id = query.get("project_id") or None
        if isinstance(project_id, str):
            project_id = project_id.strip() or None
        entries = self.entries(kind=kind, project_id=project_id)
        return 200, {"ok": True, "data": {
            "schema": "stackflow.capabilities/v1",
            "project_id": project_id,
            "count": len(entries),
            "by_type": {item: sum(1 for e in entries if e.get("type") == item)
                        for item in TYPES},
            "capabilities": entries,
        }}

    def health(self) -> dict:
        try:
            entries = self.entries()
        except Exception as exc:  # pragma: no cover - health never raises
            return {"ok": False, "error": type(exc).__name__}
        return {"ok": True, "entries": len(entries),
                "tools": sum(1 for e in entries if e.get("type") == "tool"),
                "executors": len(self._executors)}


def build(app: Any) -> CapabilityCatalog:
    catalog = CapabilityCatalog(app)
    app.capability_catalog = catalog
    app.capabilities = catalog
    return catalog


__all__ = [
    "BUILTIN_PROVIDER", "CapabilityCatalog", "DEFAULT_TOOL_CAPABILITY",
    "EXECUTABLE_TYPES", "TYPES", "build", "ensure_approved",
    "principal_identity", "provider_state", "tool_entries", "tool_risk",
    "tool_schema",
]

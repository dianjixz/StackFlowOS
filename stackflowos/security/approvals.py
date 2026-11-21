"""Approval broker: the popup behind every risky capability (§6.3).

Flow: a worker asks for a capability → :meth:`ApprovalBroker.request` consults
the :class:`PolicyEngine` → ``deny`` and ``allow`` resolve immediately (mode
``auto``/``rule``/``grant``), otherwise the request becomes ``pending`` until
a human decides or the TTL expires. **Fail closed**: a pending request never
auto-approves, and an expired request means the action is refused.

Approving creates a grant rule (once/session) or a standing rule (always);
``use`` spends one-shot grants. Every request/decision/use is written to the
event log, so the audit page is just a filtered event stream. Pending requests
and the last 200 decided ones are persisted atomically.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from ..errors import BadRequest, Conflict, NotFound, ValidationError
from ..kernel import Context
from ..l2.eventlog import EventLog, emit_both
from ..protocol import new_id, now_ms as _now_ms
from ..util.jsonio import read_json, write_json
from .capabilities import risk_of
from .policy import PolicyEngine

_MODE_CHOICES = ("once", "session", "always")
#: Destructive/targeted capabilities keep their exact scope in standing grants.
_EXACT_SCOPE_CAPS = frozenset({"fs.delete", "project.delete", "project.archive",
                               "agent.kill", "sys.config", "unit.control"})
_AGENT_ID = re.compile(r"^(agent([-.])[a-z_]+)\2\d+$")


def _generalise_principal(principal: str) -> str:
    """``agent-coder-3`` -> ``agent-coder-*`` (any worker of the same role)."""
    match = _AGENT_ID.match(str(principal))
    return f"{match.group(1)}{match.group(2)}*" if match else str(principal)


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
_DECISION_CHOICES = ("allow", "deny")


@dataclass
class ApprovalRequest:
    """One capability request and its lifecycle state."""

    id: str
    principal: str
    capability: str
    scope: dict
    risk: str
    reason: str = ""
    preview: str = ""
    project_id: str | None = None
    task_id: str | None = None
    status: str = "pending"    # pending|approved|denied|expired|canceled
    mode: str | None = None    # rule|grant|auto|once|session|always
    grant_id: str | None = None
    created_at: int = 0
    expires_at: int = 0
    decided_at: int | None = None
    decided_by: str | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ApprovalRequest":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in (d or {}).items() if k in known}
        request = cls(**data)
        if not isinstance(request.scope, dict):
            request.scope = {}
        return request


class ApprovalBroker:
    """Creates, decides, expires and persists approval requests."""

    def __init__(self, ctx: Context, policy: PolicyEngine, eventlog: EventLog, *,
                 approvals_path: Path, approval_ttl_s: int = 300,
                 grant_ttl_s: int = 3600,
                 logger: logging.Logger | None = None) -> None:
        self.ctx = ctx
        self.policy = policy
        self.eventlog = eventlog
        self.approvals_path = Path(approvals_path)
        self.approval_ttl_s = int(approval_ttl_s)
        self.grant_ttl_s = int(grant_ttl_s)
        self.log = logger or getattr(ctx, "log",
                                     logging.getLogger("sfo.security.approvals"))
        self._requests: dict[str, ApprovalRequest] = {}
        self._lock = threading.RLock()
        self.load()

    # -- request ----------------------------------------------------------
    def request(self, principal: str, capability: str, scope: dict, *,
                reason: str = "", preview: str = "",
                project_id: str | None = None, task_id: str | None = None,
                risk: str | None = None,
                ttl_s: int | None = None) -> ApprovalRequest:
        """Evaluate a capability request, auto-resolving or parking it."""
        if not principal:
            raise BadRequest("approval principal is required")
        if ttl_s is not None and int(ttl_s) <= 0:
            raise ValidationError("approval ttl_s must be positive")
        now = _now_ms()
        scope = dict(scope or {})
        effective_project = project_id or scope.get("project_id")
        if risk is None:
            risk = self._risk_for(capability, scope, effective_project)
        decision = self.policy.evaluate(principal, capability, scope,
                                        risk=risk, now_ms=now)
        request = ApprovalRequest(
            id=new_id("apr"),
            principal=str(principal),
            capability=str(capability),
            scope=scope,
            risk=risk,
            reason=str(reason or ""),
            preview=str(preview or ""),
            project_id=project_id,
            task_id=task_id,
            created_at=now,
        )
        if decision.outcome == "deny":
            request.status = "denied"
            request.mode = "auto"
            request.decided_at = now
            request.decided_by = "policy"
            request.note = decision.reason
            self._store(request)
            self._emit("approval.auto", request, decision="deny",
                       rule_id=decision.rule.id if decision.rule else None,
                       policy_reason=decision.reason)
            return request
        if decision.outcome == "allow":
            rule = decision.rule
            request.status = "approved"
            request.mode = ("rule" if rule is not None and rule.source == "rule"
                            else ("grant" if rule is not None else "auto"))
            request.grant_id = rule.id if rule is not None else None
            request.decided_at = now
            request.decided_by = "policy"
            request.note = decision.reason
            self._store(request)
            self._emit("approval.auto", request, decision="allow",
                       rule_id=request.grant_id, mode=request.mode,
                       policy_reason=decision.reason)
            return request
        request.status = "pending"
        ttl = int(ttl_s if ttl_s is not None else self.approval_ttl_s)
        request.expires_at = now + ttl * 1000
        self._store(request)
        self._emit("approval.requested", request)
        return request

    def get(self, request_id: str) -> ApprovalRequest:
        """Return one request or raise :class:`NotFound`."""
        with self._lock:
            request = self._requests.get(request_id)
        if request is None:
            raise NotFound(f"unknown approval request: {request_id!r}",
                           detail={"request_id": request_id})
        return request

    def list(self, *, status: str | None = None,
             project_id: str | None = None,
             limit: int = 200) -> list[ApprovalRequest]:
        """Pending first, then newest history; optionally filtered."""
        with self._lock:
            requests = list(self._requests.values())
        items = [
            request for request in requests
            if (status is None or request.status == status)
            and (project_id is None or request.project_id == project_id)
        ]
        items.sort(key=lambda request: (0 if request.status == "pending" else 1,
                                        -request.created_at, request.id))
        return items[:max(0, int(limit))]

    # -- decisions --------------------------------------------------------
    def decide(self, request_id: str, *, decision: str, mode: str = "once",
               by: str = "operator", note: str = "") -> ApprovalRequest:
        """Approve or deny a pending request, materialising a grant/rule."""
        if decision not in _DECISION_CHOICES:
            raise BadRequest(f"decision must be one of {list(_DECISION_CHOICES)}")
        if decision == "allow" and mode not in _MODE_CHOICES:
            raise BadRequest(f"mode must be one of {list(_MODE_CHOICES)}")
        now = _now_ms()
        with self._lock:
            request = self.get(request_id)
            if request.status != "pending":
                raise Conflict(
                    f"approval {request_id!r} is already {request.status!r}",
                    detail={"request_id": request_id, "status": request.status})
        if decision == "deny":
            request.status = "denied"
            request.mode = None
        else:
            request.status = "approved"
            request.mode = mode
            grant = self._materialise_grant(request, mode, by, now)
            request.grant_id = grant.id
        request.decided_at = now
        request.decided_by = str(by or "operator")
        request.note = str(note or request.note or "")
        request.expires_at = request.expires_at or now
        with self._lock:
            self._requests[request.id] = request
        self.save()
        self._emit("approval.decided", request, decision=decision,
                   mode=request.mode, by=request.decided_by,
                   grant_id=request.grant_id)
        if decision == "allow" and mode != "once":
            self.resolve_pending()
        return request

    def _materialise_grant(self, request: ApprovalRequest, mode: str,
                           by: str, now: int) -> Any:
        if mode == "once":
            return self.policy.add_rule(
                effect="allow", capability=request.capability,
                principal=request.principal, scope=request.scope,
                risk_max=request.risk,
                expires_at=now + min(self.grant_ttl_s, 300) * 1000,
                uses_left=1,
                note=f"approval {request.id} (once)",
                created_by=by, source="grant")
        # Workers are ephemeral and their paths/commands vary, so a standing
        # grant is widened to "the same kind of action by the same role";
        # an exact copy of the request would never match again.
        principal = _generalise_principal(request.principal)
        scope = self._generalise_scope(request)
        if mode == "session":
            return self.policy.add_rule(
                effect="allow", capability=request.capability,
                principal=principal, scope=scope,
                risk_max=request.risk,
                expires_at=now + self.grant_ttl_s * 1000,
                uses_left=None,
                note=f"approval {request.id} (session)",
                created_by=by, source="grant")
        return self.policy.add_rule(
            effect="allow", capability=request.capability,
            principal=principal, scope=scope,
            risk_max=request.risk,
            expires_at=None, uses_left=None,
            note=f"approval {request.id} (always)",
            created_by=by, source="rule")

    def _generalise_scope(self, request: ApprovalRequest) -> dict:
        """Rule scope pattern covering requests like ``request``."""
        scope = dict(request.scope or {})
        capability = request.capability
        if capability in _EXACT_SCOPE_CAPS:
            return scope
        project_id = scope.get("project_id")
        pattern: dict[str, Any] = {}
        if project_id:
            pattern["project_id"] = project_id
        if capability in ("fs.read", "fs.list", "fs.write"):
            raw = scope.get("path") or scope.get("cwd")
            if raw:
                pattern["path_prefix"] = self._path_root(raw, project_id)
            return pattern
        if capability == "shell.exec":
            if scope.get("command") is not None:
                pattern["command"] = scope["command"]
            return pattern
        if capability == "net.fetch":
            host = _host_of(str(scope.get("url") or ""))
            if host:
                return {"domain": host}
            return scope
        return pattern

    def _path_root(self, raw: Any, project_id: str | None) -> str:
        """Project workspace when the path is inside it, else its directory."""
        path = Path(str(raw))
        projects_root = getattr(self.policy, "projects_root", None)
        if project_id and projects_root:
            workspace = Path(projects_root) / str(project_id) / "workspace"
            try:
                path.resolve().relative_to(workspace.resolve())
                return str(workspace)
            except (ValueError, OSError):
                pass
        return str(path if path.is_dir() else path.parent)

    def resolve_pending(self, *, by: str = "policy") -> list[ApprovalRequest]:
        """Approve/deny parked requests that current rules now decide.

        Called after a rule is created so already-waiting requests (several
        workers often ask for the same thing) do not each need a click.
        """
        now = _now_ms()
        resolved: list[ApprovalRequest] = []
        with self._lock:
            pending = [r for r in self._requests.values()
                       if r.status == "pending"]
        for request in pending:
            try:
                decision = self.policy.evaluate(
                    request.principal, request.capability, request.scope,
                    risk=request.risk, now_ms=now)
            except Exception:  # noqa: BLE001 - stale request must not block others
                continue
            if decision.outcome not in ("allow", "deny"):
                continue
            with self._lock:
                if request.status != "pending":
                    continue
                rule = decision.rule
                if decision.outcome == "allow":
                    request.status = "approved"
                    request.mode = ("rule" if rule is not None
                                    and rule.source == "rule" else "grant")
                    request.grant_id = rule.id if rule is not None else None
                else:
                    request.status = "denied"
                    request.mode = "auto"
                request.decided_at = now
                request.decided_by = by
                request.note = decision.reason
            resolved.append(request)
            self._emit("approval.auto", request, decision=decision.outcome,
                       rule_id=request.grant_id, mode=request.mode,
                       policy_reason=decision.reason)
        if resolved:
            self.save()
        return resolved

    # -- usage / expiry ---------------------------------------------------
    def poll(self, request_id: str) -> ApprovalRequest:
        """Refresh one request, expiring it when its TTL has passed."""
        request = self.get(request_id)
        now = _now_ms()
        if request.status == "pending" and request.expires_at and now >= request.expires_at:
            with self._lock:
                request.status = "expired"
                request.decided_at = now
                request.note = request.note or "expired"
            self.save()
            self._emit("approval.expired", request)
        return request

    def use(self, grant_id: str) -> bool:
        """Spend one use of a grant; emits ``approval.used`` when allowed."""
        rule = self.policy.get_rule(grant_id)
        allowed = self.policy.consume(grant_id)
        if allowed:
            self._emit_grant_used(grant_id, rule)
        return allowed

    def sweep(self, now_ms: int | None = None) -> list[ApprovalRequest]:
        """Expire every pending request past its deadline (fail closed)."""
        now = now_ms if now_ms is not None else _now_ms()
        expired: list[ApprovalRequest] = []
        with self._lock:
            for request in self._requests.values():
                if request.status == "pending" and request.expires_at \
                        and now >= request.expires_at:
                    request.status = "expired"
                    request.decided_at = now
                    request.note = request.note or "expired"
                    expired.append(request)
        if expired:
            self.save()
            for request in expired:
                self._emit("approval.expired", request)
        return expired

    def pending_count(self) -> int:
        """Number of requests still waiting for a human decision."""
        with self._lock:
            return sum(1 for request in self._requests.values()
                       if request.status == "pending")

    # -- persistence ------------------------------------------------------
    def save(self) -> None:
        """Atomically persist pending requests plus the last 200 decided."""
        with self._lock:
            pending = [request for request in self._requests.values()
                       if request.status == "pending"]
            decided = [request for request in self._requests.values()
                       if request.status != "pending"]
        decided.sort(key=lambda request: (request.decided_at or request.created_at,
                                          request.id))
        payload = {
            "version": 1,
            "requests": [request.to_dict() for request in pending + decided[-200:]],
        }
        write_json(self.approvals_path, payload)

    def load(self) -> None:
        """Load persisted requests; a missing/corrupt file means none."""
        try:
            raw = read_json(self.approvals_path, default=None)
        except ValueError:
            self.log.warning("ignoring unreadable approvals file %s",
                             self.approvals_path)
            return
        rows = raw.get("requests") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return
        requests: dict[str, ApprovalRequest] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                request = ApprovalRequest.from_dict(row)
            except (TypeError, ValueError):
                continue
            requests[request.id] = request
        with self._lock:
            self._requests = requests

    # -- internals --------------------------------------------------------
    def _store(self, request: ApprovalRequest) -> None:
        with self._lock:
            self._requests[request.id] = request
        self.save()

    def _risk_for(self, capability: str, scope: dict,
                  project_id: str | None) -> str:
        projects_root = getattr(self.policy, "projects_root", None) or Path.cwd()
        workspace = None
        if project_id:
            workspace = Path(projects_root) / str(project_id) / "workspace"
        return risk_of(capability, scope, projects_root=Path(projects_root),
                       workspace=workspace)

    def _emit_grant_used(self, grant_id: str, rule: Any) -> None:
        data = {"grant_id": grant_id}
        if rule is not None:
            data.update({"capability": rule.capability,
                         "principal": rule.principal,
                         "effect": rule.effect})
        emit_both(self.ctx, self.eventlog, "approval.used", data)

    def _emit(self, type: str, request: ApprovalRequest, **extra: Any) -> None:
        data = {
            "request_id": request.id,
            "status": request.status,
            "principal": request.principal,
            "capability": request.capability,
            "scope": request.scope,
            "risk": request.risk,
            "mode": request.mode,
            "grant_id": request.grant_id,
            "request": request.to_dict(),
        }
        data.update(extra)
        emit_both(self.ctx, self.eventlog, type, data, request.project_id)


__all__ = ["ApprovalRequest", "ApprovalBroker"]

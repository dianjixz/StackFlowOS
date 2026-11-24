"""Guardian: bounded, audited self-approval of pending requests.

Implementation contract: see ``docs/team/autonomy-plan.md`` §3.3 and
``docs/autonomy.md``.

The guardian never invents a grant or bypasses the approval broker: it only
calls the normal :meth:`App.decide_approval` entry point (``mode="once"``,
``by="autonomy:guardian"``) after re-checking the operator's envelope for every
single request.  Anything outside the envelope is a ``skip`` with a human
readable reason, which the service keeps in the report but does not turn into
an event.

Fences, in order (first failure wins):

1. the request is still pending;
2. its principal is a worker (``agent-*``), not a human or the system;
3. the capability is allowlisted and never a :data:`HARD_DENY_CAPABILITIES`
   member (delete/kill/config/project/approval actions always need a human);
4. its risk does not exceed ``approve_risk_max``;
5. its project exists, is not archived/done and is allowed by
   ``approve_projects``;
6. capability specifics: ``fs.write`` stays inside the project workspace (and
   outside ``.git``), ``agent.spawn`` role/count, ``shell.exec`` regex + cwd +
   no shell chaining, ``net.fetch`` domain allowlist;
7. a recent human denial for the same agent role + capability is respected;
8. the hourly approval budget is not exhausted;
9. the request is old enough (``approve_min_age_s``).

``off`` never evaluates; ``observe`` records recommendations but touches
nothing; only ``enforce`` + ``allow`` consumes budget and decides.  A lost race
with a human (``Conflict``) becomes a ``skip`` record and is not retried.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..errors import Conflict
from ..protocol import now_ms
from ..security.capabilities import (
    RISK_ORDER,
    normalize_path,
    validate_project_id,
)
from .models import ACTIVE_AGENT_STATUSES, HARD_DENY_CAPABILITIES

_HOUR_MS = 3600 * 1000
#: Bound on persisted guardian records (newest win).
_MAX_STATE_RECORDS = 100
_REASON_MAX = 500

#: Simple shell metacharacters that mean "this command chains/interprets";
#: ``$(`` is checked separately because ``$`` alone is harmless.
_SHELL_CONTROL_CHARS = (";", "|", "&", ">", "<", "`", "\n", "\r")


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _generalise_principal(principal: str) -> str:
    """``agent-coder-3`` -> ``agent-coder`` (same worker role)."""
    return re.sub(r"-\d+$", "", _text(principal))


@dataclass
class Verdict:
    """One guardian decision about one pending approval request."""

    request_id: str
    decision: str          # allow | observe | skip
    reason: str
    capability: str = ""
    principal: str = ""
    risk: str = ""
    project_id: str | None = None


class Guardian:
    """Evaluate pending approvals against the operator's envelope."""

    def __init__(self, app: Any, config: Any, state: dict, *,
                 now: Any = now_ms) -> None:
        self.app = app
        self.config = config
        self.state = state if isinstance(state, dict) else {}
        self._clock = now

    # ------------------------------------------------------------------
    # config / state helpers
    # ------------------------------------------------------------------
    def _config(self, name: str, default: Any) -> Any:
        cfg = self.config
        if cfg is None:
            return default
        if isinstance(cfg, dict):
            value = cfg.get(name, default)
        else:
            value = getattr(cfg, name, default)
        return default if value is None else value

    def _config_int(self, name: str, default: int) -> int:
        return _int(self._config(name, default), default)

    def _config_float(self, name: str, default: float) -> float:
        return _float(self._config(name, default), default)

    def _config_list(self, name: str, default: list[str]) -> list[str]:
        value = self._config(name, default)
        if not isinstance(value, (list, tuple)):
            return list(default)
        return [_text(item) for item in value]

    def _now_ms(self) -> int:
        value = self._clock
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001 - a broken clock must not break us
                value = None
        if value is None:
            return now_ms()
        return _int(value, now_ms())

    def _mode(self) -> str:
        return _text(self._config("approve_mode", "off")).strip().lower()

    def _rate_rows(self) -> list:
        rate = self.state.get("rate")
        if not isinstance(rate, dict):
            rate = {}
            self.state["rate"] = rate
        rows = rate.get("approvals")
        if not isinstance(rows, list):
            rows = []
            rate["approvals"] = rows
        return rows

    def _approvals_last_hour(self, now: int) -> int:
        rows = self._rate_rows()
        cutoff = now - _HOUR_MS
        kept = [_int(row, -1) for row in rows if _int(row, -1) >= cutoff]
        rows[:] = kept
        return len(kept)

    def _touch_rate(self, now: int) -> None:
        self._rate_rows().append(now)

    def _remember(self, record: dict) -> None:
        rows = self.state.get("approvals")
        if not isinstance(rows, list):
            rows = []
            self.state["approvals"] = rows
        request_id = _text(record.get("request_id"))
        decision = _text(record.get("decision"))
        rows[:] = [
            row for row in rows
            if not (isinstance(row, dict)
                    and _text(row.get("request_id")) == request_id
                    and _text(row.get("decision")) == decision)
        ]
        rows.append(dict(record))
        if len(rows) > _MAX_STATE_RECORDS:
            del rows[:-_MAX_STATE_RECORDS]

    # ------------------------------------------------------------------
    # domain / roster lookups (never raise)
    # ------------------------------------------------------------------
    def _agents(self) -> list[Any]:
        supervisor = getattr(self.app, "supervisor", None)
        lister = getattr(supervisor, "list", None)
        if not callable(lister):
            return []
        try:
            rows = lister()
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(rows, (list, tuple)):
            return []
        agents = []
        for agent in rows:
            if hasattr(agent, "to_dict") and callable(agent.to_dict):
                try:
                    agents.append(agent.to_dict())
                    continue
                except Exception:  # noqa: BLE001
                    pass
            agents.append(agent)
        return agents

    def _active_agents_in_project(self, project_id: str) -> int:
        count = 0
        for agent in self._agents():
            if _text(_field(agent, "project_id", "")) != project_id:
                continue
            if _text(_field(agent, "status", "")) in ACTIVE_AGENT_STATUSES:
                count += 1
        return count

    def _project(self, project_id: str) -> Any:
        store = getattr(self.app, "projects", None)
        getter = getattr(store, "get", None)
        if not callable(getter):
            return None
        try:
            validate_project_id(project_id)
            return getter(project_id)
        except Exception:  # noqa: BLE001 - missing/invalid project fail closed
            return None

    def _workspace(self, project_id: str, project: Any) -> Path | None:
        policy = getattr(self.app, "policy", None)
        resolver = getattr(policy, "workspace_for", None)
        raw: Any = None
        if callable(resolver):
            try:
                raw = resolver(project_id)
            except Exception:  # noqa: BLE001
                raw = None
        if not raw:
            raw = _field(project, "workspace", None)
        if not raw:
            return None
        try:
            return Path(str(raw)).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return None

    def _as_dict(self, request: Any) -> dict:
        if isinstance(request, dict):
            return dict(request)
        for name in ("to_public_dict", "to_dict"):
            getter = getattr(request, name, None)
            if callable(getter):
                try:
                    data = getter()
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(data, dict):
                    return dict(data)
        return {}

    def _pending(self) -> list[dict]:
        broker = getattr(self.app, "approvals", None)
        lister = getattr(broker, "list", None)
        if not callable(lister):
            return []
        try:
            rows = lister(status="pending", limit=50)
        except TypeError:  # fake brokers with a narrower signature
            try:
                rows = lister()
            except Exception:  # noqa: BLE001
                return []
        except Exception:  # noqa: BLE001 - never break a patrol
            return []
        if not isinstance(rows, (list, tuple)):
            return []
        return [self._as_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # fences
    # ------------------------------------------------------------------
    def _human_denial_reason(self, principal: str, capability: str,
                             now: int) -> str:
        cooldown = self._config_float("approve_deny_cooldown_s", 86400.0)
        if cooldown <= 0:
            return ""
        broker = getattr(self.app, "approvals", None)
        lister = getattr(broker, "list", None)
        if not callable(lister):
            return ""
        try:
            rows = lister(status="denied", limit=200)
        except TypeError:
            try:
                rows = lister()
            except Exception:  # noqa: BLE001
                return ""
        except Exception:  # noqa: BLE001
            return ""
        if not isinstance(rows, (list, tuple)):
            return ""
        wanted_principal = _generalise_principal(principal)
        for raw in rows:
            row = self._as_dict(raw)
            if not row:
                continue
            if not _text(_field(row, "decided_by", "")).startswith("human"):
                continue
            if _text(_field(row, "capability", "")) != capability:
                continue
            if _generalise_principal(
                    _text(_field(row, "principal", ""))) != wanted_principal:
                continue
            decided_at = _int(_field(row, "decided_at", 0) or
                              _field(row, "created_at", 0), 0)
            if decided_at and now - decided_at < int(cooldown * 1000):
                return ("a human denied the same action recently; "
                        "respecting approve_deny_cooldown_s")
        return ""

    def _rate_reason(self, now: int) -> str:
        limit = self._config_int("approve_max_per_hour", 20)
        if limit <= 0:
            return "hourly approval quota is disabled (0/h)"
        if self._approvals_last_hour(now) >= limit:
            return f"hourly approval quota exhausted ({limit}/h)"
        return ""

    def _age_reason(self, request: dict, now: int) -> str:
        min_age = self._config_float("approve_min_age_s", 0.0)
        if min_age <= 0:
            return ""
        created = _int(_field(request, "created_at", 0), 0)
        if created and now - created < int(min_age * 1000):
            return (f"request is younger than approve_min_age_s "
                    f"({min_age:.0f}s); leave it for a human")
        return ""

    def _check_fs_write(self, scope: dict, project_id: str, project: Any,
                        risk_max: str) -> tuple[str, str]:
        """Return ``(verdict, reason)``; verdict is "allow" or "skip"."""
        if _text(_field(project, "workspace_mode", "")) == "attached-ro":
            return "skip", "project is attached read-only"
        raw_path = scope.get("path")
        if not raw_path:
            return "skip", "fs.write request has no path"
        workspace = self._workspace(project_id, project)
        if workspace is None:
            return "skip", "project workspace is unknown"
        target = normalize_path(raw_path, cwd=scope.get("cwd"))
        try:
            relative = target.relative_to(workspace)
        except (ValueError, OSError):
            return "skip", "path is outside the project workspace"
        if ".git" in relative.parts:
            return "skip", "path is inside .git"
        return "allow", "fs.write inside the project workspace"

    def _check_agent_spawn(self, scope: dict, project_id: str,
                           risk_max: str) -> tuple[str, str]:
        role = _text(scope.get("role", "")).strip()
        allowed_roles = self._config_list(
            "approve_roles",
            ["architect", "coder", "tester", "reviewer", "researcher"])
        if role not in allowed_roles:
            return "skip", f"role {role or 'unknown'!r} is not in approve_roles"
        limit = self._config_int("approve_max_agents_per_project", 4)
        if limit <= 0:
            return "skip", "approve_max_agents_per_project is 0; no auto-spawn"
        active = self._active_agents_in_project(project_id)
        if active >= limit:
            return "skip", (f"project already has {active} active agents "
                            f"(limit {limit})")
        return "allow", "agent.spawn within role and project limits"

    def _check_shell(self, scope: dict, project_id: str, project: Any,
                     risk_max: str) -> tuple[str, str]:
        if not bool(self._config("approve_shell", False)):
            return "skip", "shell.exec auto-approval is disabled"
        if risk_max != "high":
            return "skip", "shell.exec requires approve_risk_max='high'"
        command = _text(scope.get("command", ""))
        if not command.strip():
            return "skip", "shell.exec request has no command"
        patterns = self._config_list("approve_shell_patterns", [])
        if not patterns:
            return "skip", "approve_shell_patterns is empty"
        matched = False
        for pattern in patterns:
            try:
                if re.fullmatch(pattern, command) is not None:
                    matched = True
                    break
            except re.error:
                continue
        if not matched:
            return "skip", "command does not match approve_shell_patterns"
        if "$(" in command or any(ch in command
                                  for ch in _SHELL_CONTROL_CHARS):
            return "skip", "command contains shell control operators"
        raw_cwd = scope.get("cwd")
        if not raw_cwd:
            return "skip", "shell.exec request has no cwd"
        workspace = self._workspace(project_id, project)
        if workspace is None:
            return "skip", "project workspace is unknown"
        target = normalize_path(raw_cwd)
        try:
            target.relative_to(workspace)
        except (ValueError, OSError):
            return "skip", "cwd is outside the project workspace"
        return "allow", "shell command matches the operator allowlist"

    def _check_net_fetch(self, scope: dict) -> tuple[str, str]:
        url = _text(scope.get("url", ""))
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            scheme = (parts.scheme or "").lower()
        except ValueError:
            return "skip", "net.fetch URL is not parseable"
        if scheme not in ("http", "https") or not host:
            return "skip", "net.fetch URL must be absolute http(s)"
        domains = [item.strip().lower().lstrip(".")
                   for item in self._config_list("approve_domains", [])
                   if item.strip()]
        if not domains:
            return "skip", "approve_domains is empty"
        for domain in domains:
            if host == domain or host.endswith("." + domain):
                return "allow", f"net.fetch host {host} is in approve_domains"
        return "skip", f"host {host} is not in approve_domains"

    # ------------------------------------------------------------------
    # evaluate
    # ------------------------------------------------------------------
    def evaluate(self, request: dict) -> Verdict:
        """Return a pure decision for one request; never raises."""
        try:
            return self._evaluate(request)
        except Exception as exc:  # noqa: BLE001 - a bad request is just a skip
            request_id = _text(_field(request, "id", "") or
                               _field(request, "request_id", ""))
            return Verdict(
                request_id=request_id, decision="skip",
                reason=f"evaluation failed: {type(exc).__name__}: {exc}",
                capability=_text(_field(request, "capability", "")),
                principal=_text(_field(request, "principal", "")),
                risk=_text(_field(request, "risk", "")),
                project_id=_text(_field(request, "project_id", "")) or None,
            )

    def _evaluate(self, request: Any) -> Verdict:
        request = self._as_dict(request)
        request_id = _text(request.get("id") or request.get("request_id"))
        principal = _text(request.get("principal"))
        capability = _text(request.get("capability"))
        scope = request.get("scope")
        if not isinstance(scope, dict):
            scope = {}
        risk = _text(request.get("risk") or "high").strip().lower()
        project_id = request.get("project_id") or scope.get("project_id")
        project_id = _text(project_id) or None

        def verdict(decision: str, reason: str) -> Verdict:
            return Verdict(request_id=request_id, decision=decision,
                           reason=reason, capability=capability,
                           principal=principal, risk=risk,
                           project_id=project_id)

        # 1. pending only.
        if _text(request.get("status")) != "pending":
            return verdict("skip", "request is not pending")
        # 2. workers only; humans and the system decide for themselves.
        if not principal.startswith("agent-"):
            return verdict("skip", "principal is not a worker (agent-*)")
        # 3. capability allowlist + hard denylist.
        if capability in HARD_DENY_CAPABILITIES:
            return verdict(
                "skip", f"{capability} is hard-denied; a human must decide")
        allowed = self._config_list("approve_capabilities",
                                    ["fs.write", "agent.spawn"])
        if capability not in allowed:
            return verdict(
                "skip", f"{capability or 'unknown'} is not in "
                        "approve_capabilities")
        # 4. risk ceiling.
        risk_max = _text(self._config("approve_risk_max", "medium")).lower()
        if RISK_ORDER.get(risk, 2) > RISK_ORDER.get(
                risk_max, RISK_ORDER["medium"]):
            return verdict(
                "skip", f"risk {risk} exceeds approve_risk_max {risk_max}")
        # 5. project existence / lifecycle / allowlist.
        if not project_id:
            return verdict("skip", "request has no project_id")
        project = self._project(project_id)
        if project is None:
            return verdict("skip", f"project {project_id} does not exist")
        project_status = _text(_field(project, "status", ""))
        if project_status in ("archived", "done"):
            return verdict(
                "skip", f"project {project_id} is {project_status}")
        allowed_projects = self._config_list("approve_projects", [])
        if allowed_projects and project_id not in allowed_projects:
            return verdict(
                "skip", f"project {project_id} is not in approve_projects")

        # 6. capability-specific fences.
        decision = "allow"
        reason = "within the approval envelope"
        if capability == "fs.write":
            decision, reason = self._check_fs_write(
                scope, project_id, project, risk_max)
        elif capability == "agent.spawn":
            decision, reason = self._check_agent_spawn(
                scope, project_id, risk_max)
        elif capability == "shell.exec":
            decision, reason = self._check_shell(
                scope, project_id, project, risk_max)
        elif capability == "net.fetch":
            decision, reason = self._check_net_fetch(scope)
        if decision != "allow":
            return verdict("skip", reason)

        now = self._now_ms()
        # 7. respect recent human denials.
        denial = self._human_denial_reason(principal, capability, now)
        if denial:
            return verdict("skip", denial)
        # 8. hourly budget (only enforce + allow spends it).
        rate = self._rate_reason(now)
        if rate:
            return verdict("skip", rate)
        # 9. let a human look first.
        age = self._age_reason(request, now)
        if age:
            return verdict("skip", age)
        return verdict("allow", reason)

    # ------------------------------------------------------------------
    # apply
    # ------------------------------------------------------------------
    async def apply(self, requests: list[dict] | None = None) -> list[dict]:
        mode = self._mode()
        if mode not in ("observe", "enforce"):
            return []
        if requests is None:
            rows = self._pending()
        else:
            try:
                rows = list(requests)
            except TypeError:
                return []
        decide = getattr(self.app, "decide_approval", None)
        records: list[dict] = []
        for raw in rows:
            try:
                request = self._as_dict(raw)
                if not request:
                    continue
                verdict = self.evaluate(request)
                now = self._now_ms()
                decision = verdict.decision
                reason = verdict.reason
                if mode == "observe":
                    decision = "observe"
                    reason = ("would allow: " if verdict.decision == "allow"
                              else "would skip: ") + verdict.reason
                elif verdict.decision == "allow":
                    if not callable(decide):
                        decision = "skip"
                        reason = "decide_approval is unavailable"
                    else:
                        try:
                            await decide(
                                verdict.request_id, decision="allow",
                                mode="once", by="autonomy:guardian",
                                note=verdict.reason)
                        except Conflict as exc:
                            decision = "skip"
                            reason = (f"request was already handled: "
                                      f"{type(exc).__name__}: {exc}")
                        except Exception as exc:  # noqa: BLE001 - isolate
                            decision = "skip"
                            reason = (f"decide_approval failed: "
                                      f"{type(exc).__name__}: {exc}")
                        else:
                            decision = "allow"
                            reason = verdict.reason
                            self._touch_rate(now)
                record = self._record(
                    request_id=verdict.request_id, decision=decision,
                    reason=reason, capability=verdict.capability,
                    principal=verdict.principal, risk=verdict.risk,
                    project_id=verdict.project_id, ts=now)
                records.append(record)
                self._remember(record)
            except Exception as exc:  # noqa: BLE001 - one bad row never breaks
                try:
                    row = self._as_dict(raw)
                    record = self._record(
                        request_id=_text(row.get("id") or
                                         row.get("request_id")),
                        decision="skip",
                        reason=(f"guardian error: {type(exc).__name__}: "
                                f"{exc}"),
                        capability=_text(row.get("capability")),
                        principal=_text(row.get("principal")),
                        risk=_text(row.get("risk")),
                        project_id=_text(row.get("project_id")) or None,
                        ts=self._now_ms())
                    records.append(record)
                    self._remember(record)
                except Exception:  # noqa: BLE001
                    pass
        records.sort(key=lambda item: _int(item.get("ts", 0), 0))
        return records

    @staticmethod
    def _record(*, request_id: str, decision: str, reason: str,
                capability: str, principal: str, risk: str,
                project_id: str | None, ts: int) -> dict:
        return {
            "request_id": request_id,
            "decision": decision,
            "reason": _text(reason)[:_REASON_MAX],
            "capability": capability,
            "principal": principal,
            "risk": risk,
            "project_id": project_id,
            "ts": _int(ts, 0),
        }


__all__ = ["Guardian", "Verdict"]

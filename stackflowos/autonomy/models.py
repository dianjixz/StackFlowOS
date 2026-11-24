"""Data shapes for the autonomy loop: findings, dispatch hints, reports.

These are deliberately plain dataclasses with ``to_dict``/``from_dict`` and no
service references.  The loop itself lives in :mod:`stackflowos.autonomy.service`;
checks live in :mod:`~stackflowos.autonomy.patrol`; actions live in
``dispatcher``/``guardian``.  Keeping the shapes here lets every part be tested
without a hub.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

#: Severity vocabulary, low to high.  A patrol report's headline severity is
#: the highest finding it contains.
SEVERITY_ORDER: dict[str, int] = {"info": 0, "warn": 1, "critical": 2}

#: How aggressively the governor may turn findings into work.
DISPATCH_MODES: tuple[str, ...] = ("off", "tasks", "agents")

#: How aggressively the guardian may decide pending approvals.
APPROVE_MODES: tuple[str, ...] = ("off", "observe", "enforce")

#: Agent statuses that mean "this agent can still make progress".  Mirrors
#: ``stackflowos.agents.models``; duplicated here so patrol has no agent import.
ACTIVE_AGENT_STATUSES: frozenset[str] = frozenset(
    {"queued", "spawning", "idle", "working", "waiting"})

#: Approval statuses after which nobody needs to decide anything.
TERMINAL_APPROVAL_STATUSES: frozenset[str] = frozenset(
    {"approved", "denied", "expired", "canceled"})

#: Capabilities the guardian never auto-approves, even if configured.  This is
#: the last line of defence behind config validation: deleting files, killing
#: units, changing policy or managing projects always needs a human.
HARD_DENY_CAPABILITIES: frozenset[str] = frozenset({
    "fs.delete",
    "unit.control",
    "sys.config",
    "project.create",
    "project.update",
    "project.attach",
    "project.archive",
    "project.delete",
    "agent.kill",
    "approval.decide",
})


def fingerprint(*parts: Any) -> str:
    """Return a stable short fingerprint for a finding or dispatch record.

    Only identity-bearing parts belong here (check name + ids).  Never include
    timestamps, counts or human-readable messages: the same condition must map
    to the same fingerprint across patrols, otherwise dedupe and cooldowns
    would never trigger.
    """
    raw = json.dumps([str(part) for part in parts], ensure_ascii=False,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def severity_rank(value: Any) -> int:
    """Numeric rank of a severity string (unknown -> high, fail loud)."""
    return SEVERITY_ORDER.get(str(value or ""), SEVERITY_ORDER["critical"])


def max_severity(values: Any) -> str:
    """Highest severity in an iterable of severity strings (default ``info``)."""
    best = "info"
    for value in values or ():
        candidate = str(value or "")
        if severity_rank(candidate) > severity_rank(best):
            best = candidate
    return best


@dataclass(frozen=True)
class DispatchHint:
    """What the governor could do about a finding.

    ``kind="spawn"`` starts a worker (usually for an existing idle task);
    ``kind="task"`` first creates a task, then (in ``agents`` mode) spawns for
    it.  A hint is advisory data: the dispatcher still applies quotas,
    cooldowns and project/workspace guards.
    """

    kind: str
    role: str = ""
    task_id: str | None = None
    title: str = ""
    description: str = ""
    priority: int = 1

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "role": self.role,
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description,
            "priority": int(self.priority),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "DispatchHint | None":
        if not isinstance(data, dict):
            return None
        kind = str(data.get("kind") or "")
        if kind not in ("spawn", "task"):
            return None
        try:
            priority = int(data.get("priority", 1))
        except (TypeError, ValueError):
            priority = 1
        return cls(
            kind=kind,
            role=str(data.get("role") or ""),
            task_id=(str(data["task_id"]) if data.get("task_id") else None),
            title=str(data.get("title") or ""),
            description=str(data.get("description") or ""),
            priority=max(0, min(3, priority)),
        )


@dataclass
class Finding:
    """One thing a patrol noticed, with enough evidence to act on it."""

    check: str
    severity: str
    title: str
    detail: str = ""
    fingerprint: str = ""
    project_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    unit_id: str | None = None
    fix: str = ""
    evidence: dict = field(default_factory=dict)
    dispatch: DispatchHint | None = None
    detected_at: int = 0

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_ORDER:
            raise ValueError(f"unknown severity: {self.severity!r}")
        if not self.check:
            raise ValueError("finding.check is required")
        if not self.fingerprint:
            self.fingerprint = fingerprint(
                self.check, self.project_id, self.task_id,
                self.agent_id, self.unit_id)

    @property
    def dispatchable(self) -> bool:
        return self.dispatch is not None

    def to_dict(self) -> dict:
        return {
            "check": self.check,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "fingerprint": self.fingerprint,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "unit_id": self.unit_id,
            "fix": self.fix,
            "evidence": dict(self.evidence),
            "dispatchable": self.dispatchable,
            "dispatch": self.dispatch.to_dict() if self.dispatch else None,
            "detected_at": int(self.detected_at or 0),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Finding | None":
        if not isinstance(data, dict) or not data.get("check"):
            return None
        severity = str(data.get("severity") or "info")
        if severity not in SEVERITY_ORDER:
            severity = "info"
        evidence = data.get("evidence")
        return cls(
            check=str(data["check"]),
            severity=severity,
            title=str(data.get("title") or data["check"]),
            detail=str(data.get("detail") or ""),
            fingerprint=str(data.get("fingerprint") or ""),
            project_id=(str(data["project_id"]) if data.get("project_id")
                        else None),
            task_id=str(data["task_id"]) if data.get("task_id") else None,
            agent_id=str(data["agent_id"]) if data.get("agent_id") else None,
            unit_id=str(data["unit_id"]) if data.get("unit_id") else None,
            fix=str(data.get("fix") or ""),
            evidence=dict(evidence) if isinstance(evidence, dict) else {},
            dispatch=DispatchHint.from_dict(data.get("dispatch")),
            detected_at=int(data.get("detected_at") or 0),
        )


def severity_counts(findings: list[Finding]) -> dict[str, int]:
    """Count findings by severity, always returning every severity key."""
    counts = {"critical": 0, "warn": 0, "info": 0, "total": 0}
    for finding in findings:
        counts[finding.severity] += 1
        counts["total"] += 1
    return counts


def diff_fingerprints(previous: list[Finding],
                      current: list[Finding]) -> tuple[list[str], list[str]]:
    """Return ``(new, resolved)`` fingerprints between two patrol snapshots."""
    before = {finding.fingerprint for finding in previous}
    after = {finding.fingerprint for finding in current}
    new = sorted(after - before)
    resolved = sorted(before - after)
    return new, resolved


@dataclass
class PatrolReport:
    """One completed patrol, including what it dispatched/approved."""

    seq: int = 0
    started_at: int = 0
    finished_at: int = 0
    trigger: str = "timer"
    findings: list[Finding] = field(default_factory=list)
    dispatched: list[dict] = field(default_factory=list)
    approvals: list[dict] = field(default_factory=list)
    new_fingerprints: list[str] = field(default_factory=list)
    resolved_fingerprints: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def duration_ms(self) -> int:
        if not self.started_at or not self.finished_at:
            return 0
        return max(0, int(self.finished_at) - int(self.started_at))

    @property
    def headline(self) -> str:
        """One-line human summary for the panel/status payload."""
        counts = severity_counts(self.findings)
        parts = []
        if counts["critical"]:
            parts.append(f"{counts['critical']} 个严重问题")
        if counts["warn"]:
            parts.append(f"{counts['warn']} 个待处理项")
        if not parts:
            parts.append("系统正常")
        if self.dispatched:
            ok = [item for item in self.dispatched if item.get("status") == "ok"]
            if ok:
                parts.append(f"自主派工 {len(ok)} 次")
        if self.approvals:
            allowed = [item for item in self.approvals
                       if item.get("decision") == "allow"]
            if allowed:
                parts.append(f"代理放行 {len(allowed)} 次")
        return "，".join(parts)

    def to_dict(self) -> dict:
        return {
            "seq": int(self.seq),
            "started_at": int(self.started_at),
            "finished_at": int(self.finished_at),
            "duration_ms": self.duration_ms,
            "trigger": self.trigger,
            "findings": [finding.to_dict() for finding in self.findings],
            "summary": severity_counts(self.findings),
            "headline": self.headline,
            "dispatched": [dict(item) for item in self.dispatched],
            "approvals": [dict(item) for item in self.approvals],
            "new_fingerprints": list(self.new_fingerprints),
            "resolved_fingerprints": list(self.resolved_fingerprints),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "PatrolReport | None":
        if not isinstance(data, dict):
            return None
        findings = [item for item in (
            Finding.from_dict(row) for row in data.get("findings") or [])
            if item is not None]
        return cls(
            seq=int(data.get("seq") or 0),
            started_at=int(data.get("started_at") or 0),
            finished_at=int(data.get("finished_at") or 0),
            trigger=str(data.get("trigger") or "timer"),
            findings=findings,
            dispatched=[dict(row) for row in data.get("dispatched") or []
                        if isinstance(row, dict)],
            approvals=[dict(row) for row in data.get("approvals") or []
                       if isinstance(row, dict)],
            new_fingerprints=[str(row) for row in
                              data.get("new_fingerprints") or []],
            resolved_fingerprints=[str(row) for row in
                                   data.get("resolved_fingerprints") or []],
            error=str(data.get("error") or ""),
        )


__all__ = [
    "ACTIVE_AGENT_STATUSES",
    "APPROVE_MODES",
    "DISPATCH_MODES",
    "DispatchHint",
    "Finding",
    "HARD_DENY_CAPABILITIES",
    "PatrolReport",
    "SEVERITY_ORDER",
    "TERMINAL_APPROVAL_STATUSES",
    "diff_fingerprints",
    "fingerprint",
    "max_severity",
    "severity_counts",
    "severity_rank",
]

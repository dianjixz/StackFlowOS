"""System patrol: read-only checks that turn state into findings.

Implementation contract: see ``docs/team/autonomy-plan.md`` §3.2.

This module must not mutate state.  It collects one snapshot and runs pure
checks over it so tests can build synthetic snapshots without a hub.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Finding


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


def collect_snapshot(app: Any, *, now: int | None = None) -> Snapshot:
    """Gather the current system state (read-only) for one patrol."""
    raise NotImplementedError("patrol.collect_snapshot: see autonomy-plan §3.2")


def run_checks(snapshot: Snapshot, config: Any) -> list[Finding]:
    """Evaluate every patrol check and return findings (sorted by severity)."""
    raise NotImplementedError("patrol.run_checks: see autonomy-plan §3.2")


__all__ = ["Snapshot", "collect_snapshot", "run_checks"]

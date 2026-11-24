"""Agent data model.

An agent is a short-lived worker attached to a project and (usually) a task.
It is the durable view of a process that ``AgentSupervisor`` started; every
field here is safe to serialise into the roster snapshot.

Status machine (docs/architecture.md §8.1)::

    spawning -> idle -> working -> waiting -> done | failed | dead

A spawn request that arrives while ``agents.max_workers`` workers are running
is parked first::

    queued -> spawning -> idle -> ...
    queued -> dead            (operator canceled it / project archived)

``queued`` agents own no process and do not count against ``max_workers``.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, fields

#: Every legal agent status, in lifecycle order.
AGENT_STATUSES = ("queued", "spawning", "idle", "working", "waiting",
                  "done", "failed", "dead")

#: Statuses from which no further work happens.
TERMINAL_AGENT_STATUSES = ("done", "failed", "dead")

#: Human labels used to build display names.  Falls back to the capitalised
#: role name for unknown roles.
_ROLE_LABELS = {
    "lead": "Lead",
    "architect": "Architect",
    "coder": "Coder",
    "tester": "Tester",
    "reviewer": "Reviewer",
    "researcher": "Researcher",
    "aggregator": "Aggregator",
    "assistant": "Assistant",
}


def now_ms() -> int:
    """Wall-clock milliseconds, the timestamp unit used across StackFlowOS."""
    return int(time.time() * 1000)


def display_name_for(role: str, n: int) -> str:
    """Build a readable, stable display name, e.g. ``Coder · 工人 3``."""
    label = _ROLE_LABELS.get(str(role), str(role).replace("_", " ").title() or "Agent")
    return f"{label} · 工人 {n}"


@dataclass
class Agent:
    """Serialisable agent record (roster entry)."""

    id: str
    role: str
    project_id: str | None = None
    task_id: str | None = None
    unit_id: str | None = None
    status: str = "spawning"
    model: str = ""
    display_name: str = ""
    created_at: int = 0
    last_seen: int = 0
    stats: dict = field(default_factory=dict)
    summary: str = ""
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = now_ms()
        if not self.last_seen:
            self.last_seen = self.created_at
        if not self.display_name:
            self.display_name = self.id or self.role
        if not isinstance(self.stats, dict):
            self.stats = {}

    @property
    def is_terminal(self) -> bool:
        """True once the agent can no longer do work."""
        return self.status in TERMINAL_AGENT_STATUSES

    def touch(self) -> None:
        """Update ``last_seen`` to now."""
        self.last_seen = now_ms()

    def to_dict(self) -> dict:
        """Return a JSON-serialisable copy."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Agent":
        """Rebuild an agent from :meth:`to_dict` output (extra keys ignored)."""
        known = {f.name for f in fields(cls)}
        clean = {key: value for key, value in (data or {}).items() if key in known}
        stats = clean.get("stats")
        clean["stats"] = dict(stats) if isinstance(stats, dict) else {}
        return cls(**clean)

    # -- convenience for stats -------------------------------------------
    def bump(self, key: str, amount: int = 1) -> int:
        """Increment ``stats[key]`` and return the new value."""
        value = int(self.stats.get(key, 0)) + amount
        self.stats[key] = value
        return value


__all__ = [
    "AGENT_STATUSES",
    "TERMINAL_AGENT_STATUSES",
    "Agent",
    "display_name_for",
    "now_ms",
]

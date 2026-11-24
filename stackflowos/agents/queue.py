"""Priority dispatch queue for agent spawns.

When the roster already has ``agents.max_workers`` running workers a new
``agent.spawn`` no longer fails: it parks a :class:`QueueEntry` here and the
supervisor starts the worker as soon as a slot frees up.  The queue is small,
pure data and dependency-free so it can be persisted inside ``agents.json`` and
unit-tested without a hub.

Ordering contract: higher ``priority`` first, FIFO (lower sequence number)
between equal priorities.  ``priority`` is a 0..100 integer derived from the
task priority (``task.priority`` 0..3, or an explicit ``urgency`` string),
plus a small role weight, unless the caller passed one explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .models import now_ms


class QueueFull(Exception):
    """No room left in the dispatch queue (``agents.queue_max``)."""


#: Explicit ``priority`` argument bounds.  0 is the least urgent, 100 the most.
PRIORITY_MIN = 0
PRIORITY_MAX = 100

#: Task ``priority`` (0..3) -> queue priority contribution.  Task priority is
#: the operator-facing urgency (3 = urgent red), so it dominates the role
#: weight: an urgent reviewer outranks a relaxed coder.
TASK_PRIORITY_WEIGHTS = {0: 15, 1: 30, 2: 45, 3: 60}

#: Optional ``urgency`` string on a task/data payload -> contribution.  Kept
#: for API callers that describe urgency directly instead of a 0..3 integer.
URGENCY_WEIGHTS = {
    "relaxed": 15, "low": 15,
    "normal": 30, "medium": 30,
    "high": 45,
    "urgent": 60, "critical": 60,
}

#: Role weight: a project is only as useful as its lead, a review unblocks the
#: next task, etc.  Small on purpose -- task urgency wins.
ROLE_WEIGHTS = {
    "lead": 12,
    "reviewer": 8,
    "architect": 7,
    "coder": 6,
    "tester": 5,
    "researcher": 4,
    "aggregator": 3,
    "assistant": 2,
}

#: Display/tie-break order of task roles (docs/architecture.md §8.1): earlier
#: roles in the dispatch table get a tiny bonus so equal priorities keep the
#: documented order instead of insertion order.
DISPLAY_ORDER = ("architect", "coder", "tester", "reviewer", "researcher")


def clamp_priority(value: Any, default: int = 50) -> int:
    """Coerce a caller-supplied priority into ``0..100`` (never raises)."""
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(PRIORITY_MIN, min(PRIORITY_MAX, number))


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def task_score(task: Any) -> int:
    """Urgency contribution of a task object/dict (0 when absent)."""
    if task is None:
        return 0
    urgency = _field(task, "urgency")
    if isinstance(urgency, str):
        lowered = urgency.strip().lower()
        if lowered in URGENCY_WEIGHTS:
            return URGENCY_WEIGHTS[lowered]
    value = _field(task, "priority", 0)
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return TASK_PRIORITY_WEIGHTS.get(number, 0)


def compute_priority(role: str, task: Any = None,
                     explicit: Any = None) -> int:
    """Dispatch priority for one spawn request.

    An explicit value always wins (it is clamped, not rejected, so old callers
    that pass a boolean/None stay compatible).  Otherwise the task urgency and
    then the role weight decide; the display order breaks remaining ties.
    """
    if explicit is not None:
        return clamp_priority(explicit)
    score = task_score(task)
    score += ROLE_WEIGHTS.get(str(role), 1)
    try:
        index = DISPLAY_ORDER.index(str(role))
    except ValueError:
        index = len(DISPLAY_ORDER)
    # 0..4 bonus: earlier in the documented dispatch order sorts first.
    score += max(0, len(DISPLAY_ORDER) - index)
    return max(PRIORITY_MIN, min(PRIORITY_MAX, score))


@dataclass
class QueueEntry:
    """One parked ``agent.spawn`` request."""

    id: str = ""
    agent_id: str = ""
    role: str = ""
    project_id: str | None = None
    task_id: str | None = None
    priority: int = 50
    reason: str = ""
    seq: int = 0
    queued_at: int = 0
    #: Extra fields preserved for API consumers (never secrets).
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.queued_at:
            self.queued_at = now_ms()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "role": self.role,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "priority": self.priority,
            "reason": self.reason,
            "seq": self.seq,
            "queued_at": self.queued_at,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "QueueEntry":
        clean: dict[str, Any] = {}
        for name in ("id", "agent_id", "role", "reason"):
            clean[name] = str((data or {}).get(name) or "")
        for name in ("project_id", "task_id"):
            value = (data or {}).get(name)
            clean[name] = str(value) if value else None
        clean["priority"] = clamp_priority((data or {}).get("priority"))
        try:
            clean["seq"] = int((data or {}).get("seq") or 0)
            clean["queued_at"] = int((data or {}).get("queued_at") or 0)
        except (TypeError, ValueError):
            clean["seq"] = 0
            clean["queued_at"] = 0
        extra = (data or {}).get("extra")
        clean["extra"] = dict(extra) if isinstance(extra, dict) else {}
        return cls(**clean)

    def sort_key(self) -> tuple:
        """Higher priority first, then FIFO, then the id for determinism."""
        return (-self.priority, self.seq, self.id)


class DispatchQueue:
    """Bounded, persisted priority queue of parked spawn requests."""

    def __init__(self, max_size: int = 64, *, counter: int = 0) -> None:
        self.max_size = max(1, int(max_size))
        self._entries: dict[str, QueueEntry] = {}
        self._counter = max(0, int(counter))
        self._seq = 0

    # -- sizing -----------------------------------------------------------
    def set_max_size(self, max_size: int) -> None:
        """Shrink/grow the bound; already queued entries are kept."""
        self.max_size = max(1, int(max_size))

    # -- mutation ---------------------------------------------------------
    def put(self, entry: QueueEntry) -> QueueEntry:
        if len(self._entries) >= self.max_size:
            raise QueueFull(f"agent queue is full: {len(self._entries)}/"
                            f"{self.max_size}")
        self._counter += 1
        self._seq += 1
        entry.id = entry.id or f"q-{self._counter}"
        entry.seq = entry.seq or self._seq
        if not entry.queued_at:
            entry.queued_at = now_ms()
        self._entries[entry.id] = entry
        return entry

    def pop(self) -> QueueEntry | None:
        """Remove and return the highest-priority entry (None when empty)."""
        if not self._entries:
            return None
        entry = min(self._entries.values(), key=QueueEntry.sort_key)
        self._entries.pop(entry.id, None)
        return entry

    def get(self, entry_id: str) -> QueueEntry | None:
        return self._entries.get(str(entry_id))

    def remove(self, entry_id: str) -> QueueEntry | None:
        return self._entries.pop(str(entry_id), None)

    def remove_agent(self, agent_id: str) -> QueueEntry | None:
        for entry in list(self._entries.values()):
            if entry.agent_id == agent_id:
                return self._entries.pop(entry.id, None)
        return None

    def clear(self) -> None:
        self._entries.clear()

    # -- views ------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def entries(self, *, project_id: str | None = None) -> list[QueueEntry]:
        values: Iterable[QueueEntry] = self._entries.values()
        if project_id is not None:
            values = [entry for entry in values
                      if entry.project_id == project_id]
        return sorted(values, key=QueueEntry.sort_key)

    def snapshot(self, *, project_id: str | None = None) -> list[dict]:
        """JSON-ready queue view with 1-based positions and wait time."""
        now = now_ms()
        out: list[dict] = []
        for position, entry in enumerate(self.entries(project_id=project_id),
                                         start=1):
            data = entry.to_dict()
            data["position"] = position
            data["waited_ms"] = max(0, now - int(entry.queued_at or now))
            out.append(data)
        return out

    # -- persistence ------------------------------------------------------
    def to_payload(self) -> dict:
        return {
            "counter": self._counter,
            "seq": self._seq,
            "entries": [entry.to_dict() for entry in self.entries()],
        }

    def load(self, payload: Any) -> None:
        """Restore a snapshot; malformed entries are skipped, never raised."""
        if not isinstance(payload, dict):
            return
        try:
            self._counter = max(0, int(payload.get("counter") or 0))
            self._seq = max(0, int(payload.get("seq") or 0))
        except (TypeError, ValueError):
            self._counter = 0
            self._seq = 0
        entries: dict[str, QueueEntry] = {}
        for raw in payload.get("entries") or []:
            if not isinstance(raw, dict):
                continue
            try:
                entry = QueueEntry.from_dict(raw)
            except (TypeError, ValueError):
                continue
            if not entry.id or not entry.agent_id:
                continue
            entries[entry.id] = entry
            self._seq = max(self._seq, entry.seq)
        self._entries = entries


__all__ = [
    "DispatchQueue",
    "PRIORITY_MAX",
    "PRIORITY_MIN",
    "QueueEntry",
    "QueueFull",
    "ROLE_WEIGHTS",
    "URGENCY_WEIGHTS",
    "clamp_priority",
    "compute_priority",
    "task_score",
]

"""Project / task / milestone data models.

These are the pure data shapes of the project domain (docs/architecture.md §7).
They stay JSON-friendly: every model has ``to_dict`` / ``from_dict``, all
timestamps are integer milliseconds, and no model holds a live service or
context reference.

Two rules live here so they can be reused and tested without a store:

* the task state machine (:data:`ALLOWED_TASK_TRANSITIONS` and
  :func:`validate_task_transition`);
* the project progress formula (:func:`progress_for`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import ValidationError


class TaskStatus:
    """Constant set of task statuses (plain strings keep JSON trivial)."""

    TODO = "todo"
    DOING = "doing"
    REVIEW = "review"
    DONE = "done"
    BLOCKED = "blocked"

    ALL: tuple[str, ...] = (TODO, DOING, REVIEW, DONE, BLOCKED)


class WorkspaceMode:
    """How a project's workspace is owned."""

    MANAGED = "managed"            # created by StackFlowOS under projects/<id>
    ATTACHED_RO = "attached-ro"    # existing directory, agents may only read
    ATTACHED_RW = "attached-rw"    # existing directory, agents may write

    ALL: tuple[str, ...] = (MANAGED, ATTACHED_RO, ATTACHED_RW)


class ProjectStatus:
    """Constant set of project statuses."""

    PLANNING = "planning"
    ACTIVE = "active"
    REVIEW = "review"
    DONE = "done"
    DEGRADED = "degraded"
    ARCHIVED = "archived"

    ALL: tuple[str, ...] = (PLANNING, ACTIVE, REVIEW, DONE, DEGRADED, ARCHIVED)
    #: Statuses that ``ProjectStore.update`` is allowed to set. Archiving has
    #: its own method because it must also stamp ``archived_at``.
    UPDATABLE: tuple[str, ...] = (PLANNING, ACTIVE, REVIEW, DONE, DEGRADED)


#: Legal task transitions. ``done`` is terminal; ``blocked`` can only return
#: to ``todo`` or ``doing``; ``review`` can be rejected back to ``todo``.
ALLOWED_TASK_TRANSITIONS: dict[str, set[str]] = {
    TaskStatus.TODO: {TaskStatus.DOING, TaskStatus.BLOCKED},
    TaskStatus.DOING: {TaskStatus.REVIEW, TaskStatus.BLOCKED, TaskStatus.TODO},
    TaskStatus.REVIEW: {TaskStatus.DONE, TaskStatus.TODO, TaskStatus.BLOCKED},
    TaskStatus.BLOCKED: {TaskStatus.TODO, TaskStatus.DOING},
    TaskStatus.DONE: set(),
}

#: How much of a task counts as complete for project progress (0.0 .. 1.0).
TASK_PROGRESS_WEIGHTS: dict[str, float] = {
    TaskStatus.DONE: 1.0,
    TaskStatus.REVIEW: 0.85,
    TaskStatus.DOING: 0.45,
    TaskStatus.TODO: 0.0,
    TaskStatus.BLOCKED: 0.0,
}


def validate_task_transition(old: str, new: str) -> None:
    """Raise :class:`ValidationError` if ``old -> new`` is not a legal move.

    Re-applying the current status is a no-op and is accepted so that generic
    ``update`` patches (for example a UI saving a form) do not fail when the
    status field did not actually change.
    """
    if old not in TaskStatus.ALL:
        raise ValidationError(
            f"unknown task status: {old!r}", detail={"old": old, "new": new})
    if new not in TaskStatus.ALL:
        raise ValidationError(
            f"unknown task status: {new!r}", detail={"old": old, "new": new})
    if old == new:
        return
    if new not in ALLOWED_TASK_TRANSITIONS.get(old, set()):
        raise ValidationError(
            f"illegal task transition: {old} -> {new}",
            detail={"old": old, "new": new},
        )


def progress_for(tasks: list["Task"]) -> int:
    """Return project progress as an integer percentage (0..100).

    Each task contributes a fixed weight (``done=1.0``, ``review=.85``,
    ``doing=.45``, ``todo=blocked=0``) and the result is the mean weight,
    rounded half-up. An empty task list is 0.
    """
    if not tasks:
        return 0
    total = sum(TASK_PROGRESS_WEIGHTS.get(task.status, 0.0) for task in tasks)
    percent = total / len(tasks) * 100.0
    return max(0, min(100, int(percent + 0.5)))


def _str_list(value: Any) -> list[str]:
    """Coerce a JSON value into a list of strings without mutating it."""
    if not value:
        return []
    return [str(item) for item in value]


def _dict_list(value: Any) -> list[dict]:
    if not value:
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


@dataclass
class Task:
    """One unit of work inside a project."""

    id: str
    project_id: str
    title: str
    description: str = ""
    status: str = TaskStatus.TODO
    priority: int = 0  # 0..3
    role: str | None = None
    assignee: str | None = None
    created_by: str = "operator"
    deps: list[str] = field(default_factory=list)  # task ids
    artifacts: list[dict] = field(default_factory=list)  # type/path/title/ts
    checks: list[dict] = field(default_factory=list)  # id/text/done/note
    notes: list[dict] = field(default_factory=list)  # ts/author/text
    created_at: int = 0
    updated_at: int = 0
    started_at: int | None = None
    finished_at: int | None = None
    error: str | None = None
    milestone_id: str | None = None

    def to_dict(self) -> dict:
        """Return a JSON-serialisable copy of the task."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "role": self.role,
            "assignee": self.assignee,
            "created_by": self.created_by,
            "deps": list(self.deps),
            "artifacts": [dict(item) for item in self.artifacts],
            "checks": [dict(item) for item in self.checks],
            "notes": [dict(item) for item in self.notes],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "milestone_id": self.milestone_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        """Rebuild a task from :meth:`to_dict` output (missing keys default)."""
        return cls(
            id=str(data["id"]),
            project_id=str(data.get("project_id", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            status=str(data.get("status", TaskStatus.TODO)),
            priority=int(data.get("priority", 0) or 0),
            role=data.get("role"),
            assignee=data.get("assignee"),
            created_by=str(data.get("created_by", "operator")),
            deps=_str_list(data.get("deps")),
            artifacts=_dict_list(data.get("artifacts")),
            checks=_dict_list(data.get("checks")),
            notes=_dict_list(data.get("notes")),
            created_at=int(data.get("created_at", 0) or 0),
            updated_at=int(data.get("updated_at", 0) or 0),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            error=data.get("error"),
            milestone_id=data.get("milestone_id"),
        )


@dataclass
class Milestone:
    """A named checkpoint a group of tasks belongs to."""

    id: str
    project_id: str
    title: str
    description: str = ""
    status: str = "todo"  # todo|doing|done
    task_ids: list[str] = field(default_factory=list)
    created_at: int = 0

    def to_dict(self) -> dict:
        """Return a JSON-serialisable copy of the milestone."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "task_ids": list(self.task_ids),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Milestone":
        """Rebuild a milestone from :meth:`to_dict` output."""
        return cls(
            id=str(data["id"]),
            project_id=str(data.get("project_id", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            status=str(data.get("status", "todo")),
            task_ids=_str_list(data.get("task_ids")),
            created_at=int(data.get("created_at", 0) or 0),
        )


@dataclass
class Project:
    """A goal with a workspace, a task list and milestones."""

    id: str
    name: str
    goal: str = ""
    status: str = ProjectStatus.PLANNING
    created_by: str = "operator"
    created_at: int = 0
    updated_at: int = 0
    tags: list[str] = field(default_factory=list)
    color: str = "#7c5cff"
    milestones: list[Milestone] = field(default_factory=list)
    brief: str = ""
    workspace: str = ""
    workspace_mode: str = WorkspaceMode.MANAGED
    external: bool = False
    degraded: bool = False
    archived_at: int | None = None

    def to_dict(self) -> dict:
        """Return a JSON-serialisable copy of the project.

        Tasks are not part of the model contract; the store embeds them in the
        persisted project record (``ProjectStore.save``).
        """
        return {
            "id": self.id,
            "name": self.name,
            "goal": self.goal,
            "status": self.status,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tags": list(self.tags),
            "color": self.color,
            "milestones": [milestone.to_dict() for milestone in self.milestones],
            "brief": self.brief,
            "workspace": self.workspace,
            "workspace_mode": self.workspace_mode,
            "external": self.external,
            "degraded": self.degraded,
            "archived_at": self.archived_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        """Rebuild a project from :meth:`to_dict` output."""
        milestones = [
            item if isinstance(item, Milestone) else Milestone.from_dict(item)
            for item in (data.get("milestones") or [])
            if isinstance(item, (dict, Milestone))
        ]
        return cls(
            id=str(data["id"]),
            name=str(data.get("name", "")),
            goal=str(data.get("goal", "")),
            status=str(data.get("status", ProjectStatus.PLANNING)),
            created_by=str(data.get("created_by", "operator")),
            created_at=int(data.get("created_at", 0) or 0),
            updated_at=int(data.get("updated_at", 0) or 0),
            tags=_str_list(data.get("tags")),
            color=str(data.get("color") or "#7c5cff"),
            milestones=milestones,
            brief=str(data.get("brief", "")),
            workspace=str(data.get("workspace", "")),
            workspace_mode=str(data.get("workspace_mode") or WorkspaceMode.MANAGED),
            external=bool(data.get("external", False)),
            degraded=bool(data.get("degraded", False)),
            archived_at=data.get("archived_at"),
        )


__all__ = [
    "ALLOWED_TASK_TRANSITIONS",
    "Milestone",
    "Project",
    "ProjectStatus",
    "TASK_PROGRESS_WEIGHTS",
    "Task",
    "WorkspaceMode",
    "TaskStatus",
    "progress_for",
    "validate_task_transition",
]

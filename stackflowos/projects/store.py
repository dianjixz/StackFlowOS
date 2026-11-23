"""Persistent project store.

The store is the only writer of the project domain: it owns the JSON state
file, the id counter file, the project workspaces and the event emission
(``project.*`` / ``task.*``) described in docs/architecture.md §5.6 and §7.

Design notes:

* every mutation is persisted before its event is emitted, so an event
  listener that reloads state never sees a half-written domain;
* ``<state>/state/projects.json`` is ``{"projects": [...], "version": 1}``.
  Tasks are embedded in their project record under ``"tasks"`` because the
  architecture file keeps the whole project domain in one file and
  :class:`~stackflowos.projects.models.Project` itself has no task field;
* ``<state>/state/counters.json`` is ``{"project": n, "task": n,
  "milestone": n}`` with the same atomic-write semantics as
  :class:`stackflowos.util.jsonio.Counter`;
* ``update`` / ``task_update`` are copy-on-write: the patch is applied to a
  deep copy, validated there, and only then swapped into the store, so a
  rejected patch never leaves a half-mutated domain that a later ``save``
  could persist;
* the store is not thread-safe by design; StackFlowOS runs one asyncio loop.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from ..errors import Conflict, NotFound, ValidationError
from ..kernel.context import Context
from ..util.jsonio import now_ms, read_json, write_json
from ..util.logging import get_logger
from .models import (
    WorkspaceMode,
    Milestone,
    Project,
    ProjectStatus,
    TASK_PROGRESS_WEIGHTS,
    Task,
    TaskStatus,
    progress_for,
    validate_task_transition,
)

_COUNTER_KINDS = ("project", "task", "milestone")
_ID_PREFIX = {"project": "p", "task": "t", "milestone": "m"}
_UPDATE_FIELDS = ("name", "goal", "status", "tags", "color", "brief", "degraded")
_TASK_UPDATE_FIELDS = (
    "title",
    "description",
    "status",
    "priority",
    "assignee",
    "role",
    "error",
    "deps",
)


def _as_str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _validate_priority(priority: Any) -> int:
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ValidationError(f"priority must be an integer 0..3, got {priority!r}")
    if priority < 0 or priority > 3:
        raise ValidationError(f"priority must be within 0..3, got {priority}")
    return priority


def _validate_dep_ids(value: Any) -> list[str]:
    """Normalise a ``deps`` value into a list of distinct, non-empty task ids.

    ``None`` means "no dependencies" (the default of :meth:`add_task`). Any
    other non-list value, non-string entry, blank entry or duplicate is
    rejected instead of being coerced, so a bad caller cannot silently create
    a malformed task graph.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError(
            f"deps must be a list of task ids, got {type(value).__name__}")
    deps: list[str] = []
    seen: set[str] = set()
    for dep in value:
        if not isinstance(dep, str):
            raise ValidationError(
                f"deps entries must be strings, got {type(dep).__name__}: {dep!r}")
        if not dep.strip():
            raise ValidationError("deps entries must be non-empty task ids")
        if dep in seen:
            raise ValidationError(f"duplicate task dependency: {dep!r}",
                                  detail={"dep": dep})
        seen.add(dep)
        deps.append(dep)
    return deps


def _id_number(ident: str, prefix: str) -> int:
    """Parse ``p-12`` into ``12``; return 0 for foreign ids."""
    if not ident.startswith(prefix + "-"):
        return 0
    try:
        return int(ident.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


class ProjectStore:
    """Load, mutate and persist projects, tasks and milestones.

    ``ctx`` may be ``None`` (tests, seeding tools); when it is a
    :class:`~stackflowos.kernel.context.Context` the store emits every event
    through it and logs through ``ctx.log``. ``events`` is an optional
    event-log sink with ``append(type, data, *, source, project_id)``.
    """

    def __init__(
        self,
        ctx: Context,
        *,
        state_path: Path,
        counter_path: Path,
        events: Any = None,
        workspace_root: Path | None = None,
    ) -> None:
        self.ctx = ctx
        self.state_path = Path(state_path)
        self.counter_path = Path(counter_path)
        self.events = events
        base = Path(workspace_root) if workspace_root is not None \
            else self.state_path.parent / "projects"
        self.workspace_root = base
        self._log = ctx.log if ctx is not None else get_logger("projects")
        self._projects: dict[str, Project] = {}
        self._tasks: dict[str, Task] = {}
        self._counters: dict[str, int] = self._read_counters()
        self.load()

    # -- persistence ------------------------------------------------------
    def load(self) -> None:
        """(Re)load projects and tasks from :attr:`state_path`.

        A missing file means an empty store. Id counters are raised to at
        least the highest id found on disk so a lost counter file cannot make
        the store hand out duplicate ids.
        """
        raw = read_json(self.state_path, default=None)
        self._projects.clear()
        self._tasks.clear()
        if raw is not None:
            if not isinstance(raw, dict):
                raise ValueError(f"invalid project state file: {self.state_path}")
            for item in raw.get("projects") or []:
                if not isinstance(item, dict):
                    continue
                project = Project.from_dict(item)
                self._projects[project.id] = project
                for task_item in item.get("tasks") or []:
                    if not isinstance(task_item, dict):
                        continue
                    task = Task.from_dict(task_item)
                    if not task.project_id:
                        task.project_id = project.id
                    self._tasks[task.id] = task
        self._counters = self._read_counters()
        self._sync_counters()

    def save(self) -> None:
        """Atomically write the whole project domain to :attr:`state_path`."""
        projects: list[dict] = []
        for project in sorted(self._projects.values(),
                              key=lambda item: (item.created_at, item.id)):
            record = project.to_dict()
            tasks = sorted(
                (task for task in self._tasks.values()
                 if task.project_id == project.id),
                key=lambda task: (-task.priority, task.created_at, task.id),
            )
            record["tasks"] = [task.to_dict() for task in tasks]
            projects.append(record)
        write_json(self.state_path, {"projects": projects, "version": 1})

    def _read_counters(self) -> dict[str, int]:
        raw = read_json(self.counter_path, default=None)
        counters = {kind: 0 for kind in _COUNTER_KINDS}
        if isinstance(raw, dict):
            for kind in _COUNTER_KINDS:
                try:
                    counters[kind] = max(0, int(raw.get(kind, 0) or 0))
                except (TypeError, ValueError):
                    counters[kind] = 0
        return counters

    def _write_counters(self) -> None:
        write_json(self.counter_path, dict(self._counters))

    def _sync_counters(self) -> None:
        """Raise counters to the highest id present in the loaded data."""
        maxima = {kind: 0 for kind in _COUNTER_KINDS}
        for project_id, project in self._projects.items():
            maxima["project"] = max(
                maxima["project"], _id_number(project_id, _ID_PREFIX["project"]))
            for milestone in project.milestones:
                maxima["milestone"] = max(
                    maxima["milestone"],
                    _id_number(milestone.id, _ID_PREFIX["milestone"]))
        for task_id in self._tasks:
            maxima["task"] = max(
                maxima["task"], _id_number(task_id, _ID_PREFIX["task"]))
        changed = False
        for kind, value in maxima.items():
            if value > self._counters.get(kind, 0):
                self._counters[kind] = value
                changed = True
        if changed:
            self._write_counters()

    def _next_id(self, kind: str) -> str:
        number = self._counters.get(kind, 0) + 1
        self._counters[kind] = number
        self._write_counters()
        return f"{_ID_PREFIX[kind]}-{number}"

    # -- events -----------------------------------------------------------
    def _emit(self, event_type: str, data: dict, *,
              project_id: str | None = None) -> None:
        """Append to the event log (if any) and emit through the context."""
        record: Any = None
        if self.events is not None:
            try:
                record = self.events.append(
                    event_type, data, source="sys", project_id=project_id)
            except TypeError:
                # Duck-typed sink with the minimal append(type, data) shape.
                record = self.events.append(event_type, data)
        if record is not None and hasattr(record, "to_dict"):
            payload = record.to_dict()
        elif isinstance(record, dict):
            payload = record
        else:
            payload = {
                "type": event_type,
                "data": data,
                "source": "sys",
                "project_id": project_id,
                "ts": now_ms(),
            }
        if self.ctx is not None:
            self.ctx.emit(event_type, payload)
        self._log.debug("project event %s", event_type)

    def _emit_progress(self, project: Project) -> None:
        """Emit ``project.progress`` for ``project``."""
        info = self.project_progress(project.id)
        self._emit(
            "project.progress",
            {
                "project_id": project.id,
                "progress": info["progress"],
                "counts": info["counts"],
            },
            project_id=project.id,
        )

    # -- projects ---------------------------------------------------------
    def create(
        self,
        name: str,
        goal: str = "",
        *,
        created_by: str = "operator",
        tags: list[str] | None = None,
        color: str | None = None,
        milestones: Any = None,
        workspace: str | Path | None = None,
        workspace_mode: str | None = None,
    ) -> Project:
        """Create an active project.

        Without ``workspace`` the store creates a managed workspace under
        ``<workspace_root>/<project_id>/workspace``. With ``workspace`` the
        project *attaches* an existing directory and never creates/deletes it;
        ``workspace_mode`` is then ``attached-ro`` (default) or ``attached-rw``.
        """
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValidationError("project name is required")
        now = now_ms()
        project_id = self._next_id("project")
        if workspace is not None:
            target = Path(str(workspace)).expanduser().resolve()
            if not target.exists():
                raise ValidationError(f"workspace does not exist: {target}")
            if not target.is_dir():
                raise ValidationError(f"workspace is not a directory: {target}")
            mode = str(workspace_mode or WorkspaceMode.ATTACHED_RO)
            if mode not in (WorkspaceMode.ATTACHED_RO, WorkspaceMode.ATTACHED_RW):
                raise ValidationError(
                    f"attached projects use attached-ro/attached-rw, got {mode!r}")
            external = True
            workspace_path = target
        else:
            if workspace_mode not in (None, WorkspaceMode.MANAGED):
                raise ValidationError("managed projects cannot set an attached mode")
            mode = WorkspaceMode.MANAGED
            external = False
            workspace_path = (self.workspace_root / project_id / "workspace").resolve()
            workspace_path.mkdir(parents=True, exist_ok=True)
        project = Project(
            id=project_id,
            name=clean_name,
            goal=goal or "",
            status=ProjectStatus.ACTIVE,
            created_by=created_by or "operator",
            created_at=now,
            updated_at=now,
            tags=_as_str_list(tags),
            color=color or "#7c5cff",
            workspace=str(workspace_path),
            workspace_mode=mode,
            external=external,
        )
        for spec in milestones or []:
            project.milestones.append(self._new_milestone(project.id, spec, now))
        self._projects[project.id] = project
        self.save()
        self._emit("project.created", project.to_dict(), project_id=project.id)
        if external:
            self._emit("project.attached", {
                "project_id": project.id,
                "path": project.workspace,
                "workspace_mode": project.workspace_mode,
            }, project_id=project.id)
        return project

    def _new_milestone(self, project_id: str, spec: Any, now: int) -> Milestone:
        """Build a milestone from a title string, dict or Milestone."""
        if isinstance(spec, Milestone):
            title, description, status = spec.title, spec.description, spec.status
            task_ids = list(spec.task_ids)
        elif isinstance(spec, str):
            title, description, status, task_ids = spec, "", "todo", []
        elif isinstance(spec, dict):
            title = str(spec.get("title", ""))
            description = str(spec.get("description", ""))
            status = str(spec.get("status", "todo"))
            task_ids = _as_str_list(spec.get("task_ids"))
        else:
            raise ValidationError(f"invalid milestone spec: {spec!r}")
        return Milestone(
            id=self._next_id("milestone"),
            project_id=project_id,
            title=title.strip(),
            description=description,
            status=status,
            task_ids=task_ids,
            created_at=now,
        )

    def get(self, project_id: str) -> Project:
        """Return a project or raise :class:`NotFound`."""
        project = self._projects.get(str(project_id))
        if project is None:
            raise NotFound(f"project {project_id!r} not found")
        return project

    def list(self, *, status: str | None = None) -> list[Project]:
        """Return projects, optionally filtered by status, in creation order."""
        projects = [
            project for project in self._projects.values()
            if status is None or project.status == status
        ]
        return sorted(projects, key=lambda project: (project.created_at, project.id))

    def update(self, project_id: str, patch: dict) -> Project:
        """Apply a whitelisted project patch and emit ``project.updated``.

        The patch is applied to a deep copy, validated there, and only then
        swapped into the store, so a rejected patch leaves memory and disk
        untouched. Archiving is not allowed through this method: use
        :meth:`archive` because it also stamps ``archived_at``.
        """
        if not isinstance(patch, dict):
            raise ValidationError("project patch must be a dict")
        project = self.get(project_id)
        self._require_mutable(project)
        candidate = copy.deepcopy(project)
        if "name" in patch:
            name = str(patch.get("name") or "").strip()
            if not name:
                raise ValidationError("project name is required")
            candidate.name = name
        if "goal" in patch:
            candidate.goal = str(patch.get("goal") or "")
        if "status" in patch:
            status = str(patch.get("status") or "")
            if status == ProjectStatus.ARCHIVED:
                raise ValidationError(
                    "use archive() to archive a project",
                    detail={"status": status},
                )
            if status not in ProjectStatus.UPDATABLE:
                raise ValidationError(
                    f"invalid project status: {status!r}",
                    detail={"status": status,
                            "allowed": list(ProjectStatus.UPDATABLE)},
                )
            candidate.status = status
        if "tags" in patch:
            candidate.tags = _as_str_list(patch.get("tags"))
        if "color" in patch:
            color = str(patch.get("color") or "").strip()
            if color:
                candidate.color = color
        if "brief" in patch:
            candidate.brief = str(patch.get("brief") or "")
        if "degraded" in patch:
            candidate.degraded = bool(patch.get("degraded"))
        candidate.updated_at = now_ms()
        self._commit_project(candidate)
        self._emit("project.updated", candidate.to_dict(),
                   project_id=candidate.id)
        return candidate

    def _commit_project(self, candidate: Project) -> None:
        """Swap ``candidate`` into the store and persist it.

        On a write failure the previous object is restored so memory and disk
        stay consistent (``save`` writes the whole domain atomically).
        """
        previous = self._projects[candidate.id]
        self._projects[candidate.id] = candidate
        try:
            self.save()
        except Exception:
            self._projects[candidate.id] = previous
            raise

    def archive(self, project_id: str) -> Project:
        """Move a project to ``archived`` and stamp ``archived_at``."""
        project = self.get(project_id)
        self._require_mutable(project)
        now = now_ms()
        project.status = ProjectStatus.ARCHIVED
        project.archived_at = now
        project.updated_at = now
        self.save()
        self._emit("project.archived", project.to_dict(), project_id=project.id)
        return project

    def set_workspace_mode(self, project_id: str, mode: str) -> Project:
        """Switch an attached project between read-only and writable."""
        project = self.get(project_id)
        if not project.external:
            raise ValidationError("workspace mode only applies to attached projects")
        if mode not in (WorkspaceMode.ATTACHED_RO, WorkspaceMode.ATTACHED_RW):
            raise ValidationError(
                f"attached projects use attached-ro/attached-rw, got {mode!r}")
        project.workspace_mode = mode
        project.updated_at = now_ms()
        self.save()
        self._emit("project.access.updated", {
            "project_id": project.id,
            "workspace_mode": mode,
        }, project_id=project.id)
        return project

    def delete(self, project_id: str) -> Project:
        """Permanently remove one project and every task belonging to it.

        The event log is append-only: ``project.deleted`` records exactly what
        disappeared. Workspace files are the caller's responsibility (the app
        purges them after agents are stopped), so the store stays filesystem-
        agnostic beyond its own JSON state.
        """
        project = self.get(project_id)
        removed_tasks = [task_id for task_id, task in self._tasks.items()
                         if task.project_id == project_id]
        for task_id in removed_tasks:
            self._tasks.pop(task_id, None)
        self._projects.pop(project_id, None)
        self.save()
        self._emit("project.deleted", {
            "id": project.id,
            "name": project.name,
            "status": project.status,
            "workspace": project.workspace,
            "workspace_mode": project.workspace_mode,
            "external": project.external,
            "tasks_removed": len(removed_tasks),
        }, project_id=project.id)
        return project

    def _require_mutable(self, project: Project) -> None:
        if project.status == ProjectStatus.ARCHIVED:
            raise ValidationError(
                "project archived",
                detail={"project_id": project.id},
            )

    # -- tasks ------------------------------------------------------------
    def _validate_task_deps(self, project_id: str, task_id: str,
                            value: Any) -> list[str]:
        """Validate a task's dependency ids against the project graph.

        Every dependency must name an existing task in the same project and
        the resulting edges must not introduce a cycle (self-reference
        included). The whole set is checked before anything is persisted, so
        a rejected create/update never leaves a dangling or cyclic edge
        behind.
        """
        deps = _validate_dep_ids(value)
        for dep in deps:
            if dep == task_id:
                raise ValidationError(
                    "task cannot depend on itself",
                    detail={"task_id": task_id},
                )
            target = self._tasks.get(dep)
            if target is None:
                raise ValidationError(
                    f"unknown task dependency: {dep!r}",
                    detail={"dep": dep, "project_id": project_id},
                )
            if target.project_id != project_id:
                raise ValidationError(
                    f"task dependency {dep!r} belongs to another project",
                    detail={"dep": dep, "project_id": project_id,
                            "dep_project_id": target.project_id},
                )
        if self._has_dep_cycle(project_id, task_id, deps):
            raise ValidationError(
                "task dependency cycle detected",
                detail={"task_id": task_id, "deps": list(deps)},
            )
        return deps

    def _has_dep_cycle(self, project_id: str, task_id: str,
                       deps: list[str]) -> bool:
        """Return whether ``task_id`` is reachable from ``deps``.

        Only edges inside ``project_id`` are followed; missing or foreign
        targets are skipped so pre-existing legacy data cannot crash the
        check. Because the graph is acyclic apart from the task being
        validated, the new edge set forms a cycle exactly when one of its
        targets can reach ``task_id``.
        """
        pending = list(deps)
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == task_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            node = self._tasks.get(current)
            if node is None or node.project_id != project_id:
                continue
            pending.extend(node.deps)
        return False

    def add_task(
        self,
        project_id: str,
        title: str,
        description: str = "",
        *,
        role: str | None = None,
        priority: int = 0,
        deps: list[str] | None = None,
        created_by: str = "operator",
        milestone_id: str | None = None,
    ) -> Task:
        """Add a ``todo`` task to a project and emit ``task.created``.

        Dependencies are validated before the id counter is advanced, so a
        rejected task does not burn a task id or write ``counters.json``.
        """
        project = self.get(project_id)
        self._require_mutable(project)
        clean_title = (title or "").strip()
        if not clean_title:
            raise ValidationError("task title is required")
        _validate_priority(priority)
        milestone = self._find_milestone(project, milestone_id)
        # The next id is deterministic (single-threaded store): peek at it so
        # the self-reference check can run before ``_next_id`` persists the
        # counter bump.
        next_task_id = f"{_ID_PREFIX['task']}-{self._counters.get('task', 0) + 1}"
        clean_deps = self._validate_task_deps(project.id, next_task_id, deps)
        now = now_ms()
        task = Task(
            id=self._next_id("task"),
            project_id=project.id,
            title=clean_title,
            description=description or "",
            status=TaskStatus.TODO,
            priority=priority,
            role=role,
            assignee=None,
            created_by=created_by or "operator",
            deps=clean_deps,
            created_at=now,
            updated_at=now,
            milestone_id=milestone.id if milestone is not None else None,
        )
        if milestone is not None:
            milestone.task_ids.append(task.id)
        self._tasks[task.id] = task
        project.updated_at = now
        self.save()
        self._emit("task.created", task.to_dict(), project_id=project.id)
        self._emit_progress(project)
        return task

    def _find_milestone(self, project: Project,
                        milestone_id: str | None) -> Milestone | None:
        if not milestone_id:
            return None
        for milestone in project.milestones:
            if milestone.id == milestone_id:
                return milestone
        raise NotFound(
            f"milestone {milestone_id!r} not found in project {project.id!r}")

    def task_get(self, task_id: str) -> Task:
        """Return a task or raise :class:`NotFound`."""
        task = self._tasks.get(str(task_id))
        if task is None:
            raise NotFound(f"task {task_id!r} not found")
        return task

    def task_list(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        assignee: str | None = None,
    ) -> list[Task]:
        """List tasks deterministically: priority desc, created_at asc, id."""
        tasks = [
            task for task in self._tasks.values()
            if (project_id is None or task.project_id == str(project_id))
            and (status is None or task.status == status)
            and (assignee is None or task.assignee == assignee)
        ]
        return sorted(tasks, key=lambda task: (-task.priority, task.created_at, task.id))

    def _task_project(self, task: Task) -> Project:
        project = self.get(task.project_id)
        self._require_mutable(project)
        return project

    def task_claim(self, task_id: str, agent_id: str) -> Task:
        """Atomically claim a ``todo`` task: ``todo -> doing``.

        Only an unassigned, not-yet-started task may be claimed; anything else
        raises :class:`Conflict` (the caller should not silently steal work).
        """
        if not (agent_id or "").strip():
            raise ValidationError("agent_id is required to claim a task")
        task = self.task_get(task_id)
        project = self._task_project(task)
        if task.assignee:
            raise Conflict(
                f"task {task.id} already claimed by {task.assignee}",
                detail={"task_id": task.id, "assignee": task.assignee},
            )
        if task.status != TaskStatus.TODO:
            raise Conflict(
                f"task {task.id} is not claimable (status={task.status})",
                detail={"task_id": task.id, "status": task.status},
            )
        now = now_ms()
        task.status = TaskStatus.DOING
        task.assignee = agent_id
        task.started_at = now
        task.updated_at = now
        project.updated_at = now
        self.save()
        self._emit(
            "task.claimed",
            {**task.to_dict(), "assignee": agent_id},
            project_id=project.id,
        )
        self._emit_progress(project)
        return task

    def task_update(self, task_id: str, patch: dict) -> Task:
        """Apply a whitelisted task patch, validating status transitions.

        The patch is applied to a deep copy and validated there; only a fully
        valid patch replaces the stored task, so a rejected update leaves
        memory and disk untouched. ``deps`` is re-validated like on creation
        (same project, existing, no self-reference, no cycle). Status changes
        also emit the specific lifecycle event (``task.started`` /
        ``task.review`` / ``task.completed`` / ``task.blocked``) in addition
        to ``task.updated``.
        """
        if not isinstance(patch, dict):
            raise ValidationError("task patch must be a dict")
        task = self.task_get(task_id)
        project = self._task_project(task)
        candidate = copy.deepcopy(task)
        old_status = candidate.status
        if "title" in patch:
            title = str(patch.get("title") or "").strip()
            if not title:
                raise ValidationError("task title is required")
            candidate.title = title
        if "description" in patch:
            candidate.description = str(patch.get("description") or "")
        if "status" in patch:
            new_status = str(patch.get("status") or "")
            validate_task_transition(candidate.status, new_status)
            candidate.status = new_status
        if "priority" in patch:
            candidate.priority = _validate_priority(patch.get("priority"))
        if "assignee" in patch:
            assignee = patch.get("assignee")
            candidate.assignee = None if assignee is None else str(assignee)
        if "role" in patch:
            role = patch.get("role")
            candidate.role = None if role is None else str(role)
        if "error" in patch:
            error = patch.get("error")
            candidate.error = None if error is None else str(error)
        if "deps" in patch:
            candidate.deps = self._validate_task_deps(
                project.id, candidate.id, patch.get("deps"))
        now = now_ms()
        if candidate.status != old_status:
            if candidate.status == TaskStatus.DOING and not candidate.started_at:
                candidate.started_at = now
            if candidate.status == TaskStatus.DONE:
                candidate.finished_at = now
            elif old_status == TaskStatus.DONE:
                candidate.finished_at = None
        candidate.updated_at = now
        self._commit_task(candidate, project, now)
        self._emit("task.updated", candidate.to_dict(),
                   project_id=project.id)
        if candidate.status != old_status:
            lifecycle = {
                TaskStatus.DOING: "task.started",
                TaskStatus.REVIEW: "task.review",
                TaskStatus.DONE: "task.completed",
                TaskStatus.BLOCKED: "task.blocked",
            }.get(candidate.status)
            if lifecycle is not None:
                self._emit(lifecycle, candidate.to_dict(), project_id=project.id)
        self._emit_progress(project)
        return candidate

    def _commit_task(self, candidate: Task, project: Project, now: int) -> None:
        """Swap ``candidate`` into the store and persist it.

        ``project.updated_at`` is stamped only after the patch validated, and
        both objects are rolled back if the write fails.
        """
        previous_task = self._tasks[candidate.id]
        previous_updated_at = project.updated_at
        self._tasks[candidate.id] = candidate
        project.updated_at = now
        try:
            self.save()
        except Exception:
            self._tasks[candidate.id] = previous_task
            project.updated_at = previous_updated_at
            raise

    def task_artifact(self, task_id: str, artifact: dict) -> Task:
        """Attach an artifact (``type``/``path``/``title`` + ``ts``) to a task."""
        if not isinstance(artifact, dict):
            raise ValidationError("artifact must be a dict")
        task = self.task_get(task_id)
        project = self._task_project(task)
        now = now_ms()
        entry = {
            "type": str(artifact.get("type", "file")),
            "path": str(artifact.get("path", "")),
            "title": str(artifact.get("title", "")),
            "ts": now,
        }
        task.artifacts.append(entry)
        task.updated_at = now
        project.updated_at = now
        self.save()
        self._emit("task.artifact", {**task.to_dict(), "artifact": entry},
                   project_id=project.id)
        self._emit_progress(project)
        return task

    def task_add_note(self, task_id: str, text: str, *, author: str) -> Task:
        """Append a note (``ts``/``author``/``text``) to a task."""
        clean_text = (text or "").strip()
        if not clean_text:
            raise ValidationError("note text is required")
        task = self.task_get(task_id)
        project = self._task_project(task)
        now = now_ms()
        note = {"ts": now, "author": author or "operator", "text": clean_text}
        task.notes.append(note)
        task.updated_at = now
        project.updated_at = now
        self.save()
        self._emit("task.note", {**task.to_dict(), "note": note},
                   project_id=project.id)
        self._emit_progress(project)
        return task

    def task_add_check(self, task_id: str, text: str) -> Task:
        """Append an acceptance-check item to a task."""
        clean_text = (text or "").strip()
        if not clean_text:
            raise ValidationError("check text is required")
        task = self.task_get(task_id)
        project = self._task_project(task)
        now = now_ms()
        item = {
            "id": self._next_check_id(task),
            "text": clean_text,
            "done": False,
            "note": "",
        }
        task.checks.append(item)
        task.updated_at = now
        project.updated_at = now
        self.save()
        self._emit("task.updated", task.to_dict(), project_id=project.id)
        self._emit_progress(project)
        return task

    @staticmethod
    def _next_check_id(task: Task) -> str:
        existing = {str(item.get("id")) for item in task.checks}
        number = len(task.checks) + 1
        while f"c-{number}" in existing:
            number += 1
        return f"c-{number}"

    def task_check(
        self,
        task_id: str,
        *,
        item_id: str | None = None,
        text: str | None = None,
        done: bool = True,
        note: str = "",
    ) -> Task:
        """Mark a check item done/undone by id or exact text."""
        task = self.task_get(task_id)
        project = self._task_project(task)
        item: dict | None = None
        if item_id is not None:
            item = next(
                (check for check in task.checks if check.get("id") == item_id),
                None,
            )
        elif text is not None:
            clean = text.strip()
            item = next(
                (check for check in task.checks
                 if str(check.get("text", "")).strip() == clean),
                None,
            )
        else:
            raise ValidationError("task_check needs item_id or text")
        if item is None:
            target = item_id if item_id is not None else text
            raise NotFound(f"check item {target!r} not found in task {task.id}")
        item["done"] = bool(done)
        item["note"] = str(note or "")
        now = now_ms()
        task.updated_at = now
        project.updated_at = now
        self.save()
        self._emit("task.updated", {**task.to_dict(), "check": dict(item)},
                   project_id=project.id)
        self._emit_progress(project)
        return task

    # -- projections ------------------------------------------------------
    def project_progress(self, project_id: str) -> dict:
        """Return ``{progress, counts, total, done}`` for a project."""
        self.get(project_id)
        tasks = self.task_list(project_id=project_id)
        counts = {status: 0 for status in TaskStatus.ALL}
        for task in tasks:
            counts[task.status] = counts.get(task.status, 0) + 1
        return {
            "progress": progress_for(tasks),
            "counts": counts,
            "total": len(tasks),
            "done": counts[TaskStatus.DONE],
        }

    def card(self, project_id: str) -> dict:
        """Return the UI summary card for a project."""
        project = self.get(project_id)
        info = self.project_progress(project.id)
        current = next(
            (task for task in self.task_list(project_id=project.id)
             if task.status == TaskStatus.DOING),
            None,
        )
        return {
            "id": project.id,
            "name": project.name,
            "goal": project.goal,
            "status": project.status,
            "progress": info["progress"],
            "counts": info["counts"],
            "current_task": self.task_card(current.id) if current is not None else None,
            "milestones": len(project.milestones),
            "updated_at": project.updated_at,
            "brief": project.brief,
            "workspace": project.workspace,
            "workspace_mode": project.workspace_mode,
            "external": project.external,
            "tags": list(project.tags),
            "color": project.color,
            "degraded": project.degraded,
            "archived_at": project.archived_at,
        }

    def task_card(self, task_id: str) -> dict:
        """Flatten a task plus the derived progress fields the UI needs."""
        task = self.task_get(task_id)
        weight = TASK_PROGRESS_WEIGHTS.get(task.status, 0.0)
        data = task.to_dict()
        data.update({
            "progress": int(weight * 100 + 0.5),
            "progress_weight": weight,
            "check_total": len(task.checks),
            "check_done": sum(1 for item in task.checks if item.get("done")),
            "is_done": task.status == TaskStatus.DONE,
        })
        return data


__all__ = ["ProjectStore"]

"""Dispatch: turn findings into tasks and workers, within quotas.

Implementation contract: see ``docs/team/autonomy-plan.md`` §3.3.

The dispatcher is deliberately the only autonomy module that mutates the
project domain and the agent roster.  It runs after a patrol, receives the
findings (which carry an optional :class:`~stackflowos.autonomy.models.
DispatchHint`) and applies every guard before acting:

* mode: ``tasks`` only creates tasks, ``agents`` also spawns workers;
* dedupe/cooldown on the finding fingerprint (persisted in ``state``);
* project guards: project exists, is active/review/degraded, has a real
  workspace and is not an ``attached-ro`` (read-only) project;
* per-hour task/spawn budgets and a global cap on agents the governor itself
  dispatched (tracked through ``agent_id`` records + ``supervisor.list()``);
* at most :data:`MAX_ACTIONS_PER_RUN` real actions per patrol.

Every individual action is failure isolated: a broken project, a validation
error from the store or a lost spawn race becomes one ``status="error"``
record (with a truncated error string) and never escapes :meth:`apply`.
Skipped actions are returned too, so the UI/report can explain why nothing
happened; the autonomy service only writes events for non-skipped records.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..protocol import now_ms
from .models import ACTIVE_AGENT_STATUSES, fingerprint as make_fingerprint

#: Hard ceiling on real actions (task creations + spawns) in one patrol.
MAX_ACTIONS_PER_RUN = 5

#: How often one fingerprint may be retried before a human (or a new finding)
#: must intervene.  Without this a permanently blocked/failed task that the
#: failure domain keeps resetting to ``todo`` would be re-dispatched forever,
#: once per cool-down.  Both successful and failed attempts count.
MAX_ATTEMPTS_PER_FINDING = 5

_HOUR_MS = 3600 * 1000
#: Errors are stored/truncated to this many characters.
_ERROR_MAX = 300
_DISPATCHABLE_PROJECT_STATUSES = frozenset({"active", "review", "degraded"})


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a dataclass/object or a plain dict."""
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


def _error_text(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:_ERROR_MAX]


class Dispatcher:
    """Bounded executor for dispatchable findings."""

    def __init__(self, app: Any, config: Any, state: dict, *,
                 now: Any = now_ms) -> None:
        self.app = app
        self.config = config
        self.state = state if isinstance(state, dict) else {}
        self._clock = now

    # ------------------------------------------------------------------
    # config / state helpers (all tolerant of a missing/broken config)
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

    def _rate_rows(self, key: str) -> list:
        rate = self.state.get("rate")
        if not isinstance(rate, dict):
            rate = {}
            self.state["rate"] = rate
        rows = rate.get(key)
        if not isinstance(rows, list):
            rows = []
            rate[key] = rows
        return rows

    def _recent(self, key: str, now: int) -> int:
        """Return (and prune) the number of stamps for ``key`` in the last hour."""
        rows = self._rate_rows(key)
        cutoff = now - _HOUR_MS
        kept = [_int(row, -1) for row in rows if _int(row, -1) >= cutoff]
        rows[:] = kept
        return len(kept)

    def _touch(self, key: str, now: int) -> None:
        self._rate_rows(key).append(now)

    def _prior(self, fingerprint: str) -> dict | None:
        dispatch = self.state.get("dispatch")
        if not isinstance(dispatch, dict):
            return None
        prior = dispatch.get(fingerprint)
        return prior if isinstance(prior, dict) else None

    def _remember(self, fingerprint: str, record: dict, *,
                  error: str = "") -> dict:
        dispatch = self.state.get("dispatch")
        if not isinstance(dispatch, dict):
            dispatch = {}
            self.state["dispatch"] = dispatch
        prior = dispatch.get(fingerprint)
        attempts = _int(prior.get("attempts"), 0) + 1 if isinstance(
            prior, dict) else 1
        row = dict(record)
        row["attempts"] = attempts
        if error:
            row["error"] = error[:_ERROR_MAX]
        dispatch[fingerprint] = row
        return row

    # ------------------------------------------------------------------
    # supervisor / project lookups (never raise)
    # ------------------------------------------------------------------
    def _agents(self) -> list[Any]:
        supervisor = getattr(self.app, "supervisor", None)
        lister = getattr(supervisor, "list", None)
        if not callable(lister):
            return []
        try:
            rows = lister()
        except Exception:  # noqa: BLE001 - roster is advisory for guards
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

    def _active_agent_on_task(self, task_id: str | None) -> bool:
        if not task_id:
            return False
        target = str(task_id)
        for agent in self._agents():
            status = _text(_field(agent, "status", ""))
            if status in ACTIVE_AGENT_STATUSES and \
                    _text(_field(agent, "task_id", "")) == target:
                return True
        return False

    def _governor_agents(self) -> int:
        """Count live agents the governor dispatched, from its own records."""
        agents = {}
        for agent in self._agents():
            agent_id = _text(_field(agent, "id", ""))
            if agent_id:
                agents[agent_id] = agent
        dispatch = self.state.get("dispatch")
        if not isinstance(dispatch, dict):
            return 0
        live: set[str] = set()
        for row in dispatch.values():
            if not isinstance(row, dict):
                continue
            agent_id = _text(row.get("agent_id", ""))
            if not agent_id or agent_id in live:
                continue
            agent = agents.get(agent_id)
            if agent is not None and _text(_field(agent, "status", "")) in \
                    ACTIVE_AGENT_STATUSES:
                live.add(agent_id)
        return len(live)

    def _workspace_for(self, project_id: str) -> Any:
        policy = getattr(self.app, "policy", None)
        resolver = getattr(policy, "workspace_for", None)
        if not callable(resolver):
            return None
        try:
            return resolver(project_id)
        except Exception:  # noqa: BLE001 - unknown/bad id -> caller skips
            return None

    def _project_problem(self, project_id: str | None) -> str:
        """Return "" when dispatch is allowed, else a short skip reason."""
        if not project_id:
            return "finding has no project_id"
        store = getattr(self.app, "projects", None)
        getter = getattr(store, "get", None)
        if not callable(getter):
            return "project store is unavailable"
        try:
            project = getter(project_id)
        except Exception:  # noqa: BLE001 - NotFound/ValidationError included
            return f"project {project_id} does not exist"
        if project is None:
            return f"project {project_id} does not exist"
        status = _text(_field(project, "status", ""))
        if status in ("archived", "done"):
            return f"project {project_id} is {status}"
        if status not in _DISPATCHABLE_PROJECT_STATUSES:
            return f"project {project_id} status {status or 'unknown'!r} is not dispatchable"
        if _text(_field(project, "workspace_mode", "")) == "attached-ro":
            return f"project {project_id} is attached read-only"
        workspace = _field(project, "workspace", None)
        if not workspace:
            workspace = self._workspace_for(project_id)
        if not workspace:
            return f"project {project_id} has no workspace"
        try:
            if not Path(str(workspace)).expanduser().is_dir():
                return f"project {project_id} workspace directory is missing"
        except (OSError, ValueError):
            return f"project {project_id} workspace is invalid"
        return ""

    # ------------------------------------------------------------------
    # records
    # ------------------------------------------------------------------
    @staticmethod
    def _record(fingerprint: str, action: str, status: str,
                project_id: str | None = None,
                task_id: str | None = None,
                agent_id: str | None = None,
                role: str = "", reason: str = "", ts: int = 0) -> dict:
        return {
            "fingerprint": fingerprint,
            "action": action,
            "status": status,
            "project_id": project_id,
            "task_id": task_id,
            "agent_id": agent_id,
            "role": role or "",
            "reason": reason or "",
            "ts": _int(ts, 0),
        }

    # ------------------------------------------------------------------
    # the actions
    # ------------------------------------------------------------------
    def _create_task(self, *, fingerprint: str, project_id: str,
                     title: str, description: str, role: str,
                     priority: int, finding_title: str,
                     now: int) -> tuple[dict, str | None]:
        marker = f"[autonomy:{fingerprint}] {finding_title}".strip()
        text = f"{description}\n\n{marker}" if description else marker
        task_name = title or finding_title or "自治派工任务"
        store = self.app.projects
        # Count the attempt against the hourly budget even if the store rejects
        # it: the quota bounds actions, not just successes.
        self._touch("tasks", now)
        try:
            task = store.add_task(
                project_id, task_name, text,
                role=role or None,
                priority=max(0, min(2, _int(priority, 1))),
                created_by="autonomy:governor",
            )
        except Exception as exc:  # noqa: BLE001 - failure isolation
            error = _error_text(exc)
            record = self._record(fingerprint, "task", "error", project_id,
                                  None, None, role, error, now)
            self._remember(fingerprint, record, error=error)
            return record, None
        task_id = _text(_field(task, "id", "")) or None
        record = self._record(fingerprint, "task", "ok", project_id, task_id,
                              None, role, "task created", now)
        self._remember(fingerprint, record)
        return record, task_id

    async def _spawn(self, *, fingerprint: str, project_id: str,
                     task_id: str | None, role: str, now: int) -> dict:
        # Count the attempt against the hourly budget even when the spawn fails.
        self._touch("spawns", now)
        try:
            agent = await self.app.spawn_agent(role, project_id, task_id)
        except Exception as exc:  # noqa: BLE001 - SfoError/Conflict included
            error = _error_text(exc)
            record = self._record(fingerprint, "spawn", "error", project_id,
                                  task_id, None, role, error, now)
            self._remember(fingerprint, record, error=error)
            return record
        agent_id = _text(_field(agent, "id", "")) or None
        record = self._record(fingerprint, "spawn", "ok", project_id, task_id,
                              agent_id, role, "agent spawned", now)
        self._remember(fingerprint, record)
        return record

    # ------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------
    async def apply(self, findings: list[Any]) -> list[dict]:
        try:
            items = list(findings or [])
        except TypeError:
            return []
        try:
            mode = _text(self._config("dispatch_mode", "off")).strip().lower()
        except Exception:  # noqa: BLE001
            return []
        if mode not in ("tasks", "agents"):
            return []

        cooldown_ms = int(max(0.0, self._config_float(
            "dispatch_cooldown_s", 900.0)) * 1000)
        max_tasks = self._config_int("dispatch_max_tasks_per_hour", 10)
        max_spawns = self._config_int("dispatch_max_spawns_per_hour", 4)
        max_agents = self._config_int("dispatch_max_agents", 2)
        records: list[dict] = []
        actions = 0

        for finding in items:
            if actions >= MAX_ACTIONS_PER_RUN:
                break
            try:
                hint = _field(finding, "dispatch", None)
                if hint is None:
                    continue
                kind = _text(_field(hint, "kind", "")).strip().lower()
                if kind not in ("task", "spawn"):
                    continue
                fingerprint = _text(_field(finding, "fingerprint", "")).strip()
                project_id = _field(finding, "project_id", None)
                project_id = _text(project_id) or None
                if not fingerprint:
                    fingerprint = make_fingerprint(
                        _field(finding, "check", ""), project_id,
                        _field(finding, "task_id", None))
                finding_title = _text(_field(finding, "title", "")) or _text(
                    _field(finding, "check", ""))
                hint_title = _text(_field(hint, "title", "")).strip()
                hint_desc = _text(_field(hint, "description", ""))
                role = _text(_field(hint, "role", "")).strip()
                hint_task = _field(hint, "task_id", None)
                hint_task = _text(hint_task) or None

                now = self._now_ms()

                # 1. mode gate: never act outside the configured mode.
                if mode == "tasks" and kind == "spawn":
                    records.append(self._record(
                        fingerprint, "spawn", "skipped", project_id,
                        hint_task, None, role,
                        "dispatch_mode='tasks' does not spawn agents", now))
                    continue

                # 2. fingerprint dedupe / cooldown.  An error record may be
                # retried (after the cool-down); successes wait it out.
                prior = self._prior(fingerprint)
                if prior is not None:
                    prior_status = _text(prior.get("status", ""))
                    prior_ts = _int(prior.get("ts", 0))
                    if prior_status != "error" and \
                            now - prior_ts < cooldown_ms:
                        records.append(self._record(
                            fingerprint, "task" if kind == "task" else "spawn",
                            "skipped", project_id, hint_task, None, role,
                            "same finding dispatched recently "
                            f"(cooldown {cooldown_ms // 1000}s)", now))
                        continue
                    # 2b. long-term retry budget: a task that keeps coming back
                    # must not be re-dispatched forever.  Old records without
                    # ``attempts`` count as zero, so persisted state from a
                    # previous version keeps working.
                    attempts = _int(prior.get("attempts", 0), 0)
                    if attempts >= MAX_ATTEMPTS_PER_FINDING:
                        records.append(self._record(
                            fingerprint, "task" if kind == "task" else "spawn",
                            "skipped", project_id, hint_task, None, role,
                            f"retry budget exhausted "
                            f"({MAX_ATTEMPTS_PER_FINDING} attempts); needs a "
                            "human or a new finding", now))
                        continue

                # 3. project / workspace guard (applies to tasks and spawns).
                problem = self._project_problem(project_id)
                if problem:
                    records.append(self._record(
                        fingerprint, "task" if kind == "task" else "spawn",
                        "skipped", project_id, hint_task, None, role,
                        problem, now))
                    continue

                task_id: str | None = hint_task
                if kind == "task":
                    # 3b. in agents mode the task exists to be staffed in the
                    # same round: refuse up front instead of piling up tasks
                    # that cannot be spawned right now.
                    if mode == "agents":
                        if not self._spawn_allowed(
                                now, max_spawns, max_agents, hint_task):
                            records.append(self._record(
                                fingerprint, "spawn", "skipped", project_id,
                                hint_task, None, role,
                                self._spawn_block_reason(
                                    now, max_spawns, max_agents, hint_task),
                                now))
                            continue
                    task_id = None
                    if prior is not None:
                        task_id = _text(prior.get("task_id", "")) or None
                    if task_id is None:
                        if max_tasks <= 0 or \
                                self._recent("tasks", now) >= max_tasks:
                            records.append(self._record(
                                fingerprint, "task", "skipped", project_id,
                                None, None, role,
                                "hourly task quota exhausted "
                                f"({max_tasks}/h)", now))
                            continue
                        record, task_id = self._create_task(
                            fingerprint=fingerprint, project_id=project_id,
                            title=hint_title, description=hint_desc,
                            role=role, priority=_int(
                                _field(hint, "priority", 1), 1),
                            finding_title=finding_title, now=now)
                        records.append(record)
                        actions += 1
                        if record["status"] != "ok":
                            continue
                        now = self._now_ms()

                # 4. spawn path (kind=spawn, or a task in agents mode).
                if kind == "spawn" or mode == "agents":
                    if actions >= MAX_ACTIONS_PER_RUN:
                        break
                    block = self._spawn_block_reason(
                        now, max_spawns, max_agents,
                        task_id if kind == "task" else hint_task)
                    if block:
                        records.append(self._record(
                            fingerprint, "spawn", "skipped", project_id,
                            task_id if kind == "task" else hint_task, None,
                            role or "coder", block, now))
                        continue
                    record = await self._spawn(
                        fingerprint=fingerprint, project_id=project_id,
                        task_id=task_id if kind == "task" else hint_task,
                        role=role or "coder", now=now)
                    records.append(record)
                    actions += 1
            except Exception as exc:  # noqa: BLE001 - one finding never breaks a run
                try:
                    fingerprint = _text(
                        _field(finding, "fingerprint", "")).strip()
                    project_id = _text(_field(finding, "project_id", "")) or None
                    records.append(self._record(
                        fingerprint or "unknown",
                        "task" if _text(_field(
                            _field(finding, "dispatch", None), "kind", ""))
                        == "task" else "spawn",
                        "error", project_id, None, None, "",
                        _error_text(exc), self._now_ms()))
                except Exception:  # noqa: BLE001 - truly nothing left to do
                    pass

        records.sort(key=lambda item: _int(item.get("ts", 0), 0))
        return records

    # ------------------------------------------------------------------
    # spawn guard helpers
    # ------------------------------------------------------------------
    def _spawn_block_reason(self, now: int, max_spawns: int, max_agents: int,
                            task_id: str | None) -> str:
        """Return "" when a spawn may proceed, else the skip reason."""
        if task_id and self._active_agent_on_task(task_id):
            return f"task {task_id} already has an active agent"
        if max_spawns <= 0:
            return "hourly spawn quota is disabled (0/h)"
        if self._recent("spawns", now) >= max_spawns:
            return f"hourly spawn quota exhausted ({max_spawns}/h)"
        if max_agents <= 0:
            return "governor agent cap is disabled (dispatch_max_agents=0)"
        if self._governor_agents() >= max_agents:
            return f"governor agent cap reached ({max_agents})"
        return ""

    def _spawn_allowed(self, now: int, max_spawns: int, max_agents: int,
                       task_id: str | None) -> bool:
        return not self._spawn_block_reason(
            now, max_spawns, max_agents, task_id)


__all__ = ["Dispatcher", "MAX_ACTIONS_PER_RUN", "MAX_ATTEMPTS_PER_FINDING"]

"""The agent run loop.

One runner drives one agent inside one worker process:

    claim task -> agent.working -> loop(brain.next_action -> executor.run)
    -> finish (task.note + task review/blocked + agent.finish)

The runner is defensive: an exception in a tool or brain marks the agent
failed and blocks the task, but never crashes the worker.  It also exposes
``pause()``/``kill()`` flags so the unit RPC handler (``unit.pause`` /
``unit.kill``) can steer the loop between steps.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import BadRequest, Conflict, NotFound, SfoError, ValidationError
from ..util.logging import get_logger
from .api import call_ok, emit_event
from .brain import Action, AgentView, Brain
from .models import Agent, now_ms
from .tools import TOOLS, ToolExecutor, ToolResult, tools_for
from .workspace_git import GitError, WorkspaceGit

#: Loop guards: give up instead of burning steps on a hopeless agent.
MAX_FAIL_STREAK = 6
MAX_REPEAT_FAILURES = 3
MAX_REPEAT_DENIED = 2

#: Terminal run statuses returned to the worker glue.
TERMINAL_RUN_STATUSES = ("done", "failed", "blocked", "killed", "paused")


class TaskDeadlineExceeded(Exception):
    """Raised inside the loop when ``agents.task_timeout_s`` is exhausted."""


@dataclass
class RunResult:
    """Outcome of one agent run."""

    summary: str
    status: str
    steps: int


def _as_dict(value: Any, *keys: str) -> dict:
    """Unwrap a dict payload that may nest the object under one of ``keys``."""
    if not isinstance(value, dict):
        return {}
    for key in keys:
        nested = value.get(key)
        if isinstance(nested, dict):
            return nested
    return value


def _as_list(value: Any, *keys: str) -> list[dict]:
    """Unwrap a list payload that may nest it under one of ``keys``."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in (*keys, "value"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


def _task_status(task: dict | None) -> str:
    return str(task.get("status") or "") if isinstance(task, dict) else ""


def _short_args(args: dict) -> dict:
    """Strip bulky/secret fields before putting args into an event."""
    out: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if key in ("content", "value", "patch"):
            continue
        if isinstance(value, str):
            out[key] = value if len(value) <= 160 else value[:159] + "…"
        elif isinstance(value, (int, float, bool)):
            out[key] = value
    return out


class AgentRunner:
    """Drive one :class:`Agent` until its brain finishes, fails or is stopped."""

    def __init__(self, sys: Any, agent: Agent, brain: Brain,
                 executor: ToolExecutor, *, config: Any, logger: Any = None,
                 workspace: Path | None = None) -> None:
        self.sys = sys
        self.agent = agent
        self.brain = brain
        self.executor = executor
        agents_cfg = getattr(config, "agents", config)
        self.config = agents_cfg
        self.max_steps = int(getattr(agents_cfg, "max_steps", 16) or 16)
        self.log = logger or get_logger("agents.runner")
        default_workspace = getattr(executor, "workspace", Path.cwd())
        self.workspace = Path(workspace or default_workspace)
        self.source = str(getattr(agent, "id", "agent"))
        self._paused = False
        self._killed = False
        self._steps = 0
        self._last_seq = 0
        self._events: list[dict] = []
        self._tool_results: list[dict] = []
        self._fail_streak = 0
        self._repeat_key: tuple | None = None
        self._repeat_count = 0
        # -- limits / deadline (re-read live from ``agents`` config) --------
        self._tool_calls = 0
        self._deadline_at: float | None = None
        self._started_at: float | None = None
        self._timeout_s = 0.0
        # -- per-task git branch isolation (attached-rw workspaces) --------
        self._git: WorkspaceGit | None = None
        self._git_branch = ""
        self._git_started = False

    # -- control flags (unit.pause / unit.kill) --------------------------
    def pause(self) -> None:
        """Ask the loop to stop after the current step."""
        self._paused = True

    def resume(self) -> None:
        """Clear a pause request."""
        self._paused = False

    def kill(self) -> None:
        """Ask the loop to exit as soon as possible."""
        self._killed = True

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def killed(self) -> bool:
        return self._killed

    @property
    def steps(self) -> int:
        return self._steps

    # -- main loop --------------------------------------------------------
    async def run(self) -> RunResult:
        """Run the brain/tool loop and return a final :class:`RunResult`.

        Every task is bounded twice: ``agents.task_timeout_s`` (wall clock, via
        ``asyncio.wait_for`` around each brain/tool await) and
        ``agents.max_tool_calls_per_task`` (runaway-loop protection).  Both
        limits are re-read live so a pushed config change applies mid-task.
        """
        try:
            claim_result = await self._claim()
            if claim_result is not None:
                return claim_result
            self._begin_deadline()
            await self._mark_working()
            await self._git_begin()
            while True:
                if self._killed:
                    return await self._finish("收到 kill 请求，停止工作。", "killed",
                                              self._steps)
                if self._paused:
                    return await self._finish("收到 pause 请求，暂停工作。", "paused",
                                              self._steps)
                if self._expired():
                    return await self._finish_timeout()
                if self._steps >= self._max_steps():
                    return await self._finish(
                        f"达到最大步数 max_steps={self._max_steps()}，停止。",
                        "failed", self._steps)
                view = await self._view()
                action = await self._await_bounded(self.brain.next_action(view))
                if action is None:
                    return await self._finish("大脑返回空动作。", "failed", self._steps)
                if action.finish is not None:
                    payload = action.finish if isinstance(action.finish, dict) else {}
                    summary = str(payload.get("summary") or "任务结束。")
                    status = str(payload.get("status") or "done")
                    if status not in ("done", "failed", "blocked"):
                        status = "done"
                    return await self._finish(summary, status, self._steps)
                if not action.tool:
                    return await self._finish("大脑未给出工具调用或结束动作。",
                                              "failed", self._steps)
                if self._tool_limit_reached():
                    return await self._finish_tool_limit(action)
                result = await self._await_bounded(self._execute(action))
                self._steps += 1
                self._tool_calls += 1
                self._bump_stats(result)
                # The LLM needs the *content* of fs.read/shell output, not
                # just a one-line event summary, otherwise it re-reads files
                # forever. Keep a small, bounded window for the next prompt.
                self._tool_results.append({
                    "tool": action.tool,
                    "ok": bool(result.ok),
                    "data": result.data if result.ok else {},
                    "error": result.error,
                })
                self._tool_results = self._tool_results[-3:]
                await self._emit("agent.step", {
                    "agent_id": self.agent.id,
                    "role": self.agent.role,
                    "task_id": self.agent.task_id,
                    "step": self._steps,
                    "action": {"tool": action.tool,
                               "args": _short_args(action.args or {})},
                    "ok": bool(result.ok),
                    "error": result.error,
                })
                if not result.ok:
                    self.log.info("tool %s failed for %s: %s", action.tool,
                                  self.agent.id, result.error)
                await self._observe(action, result)
                stuck = self._track_failure(action, result)
                if stuck:
                    return await self._finish(stuck, "blocked", self._steps)
                if action.say:
                    await self._emit("agent.step", {
                        "agent_id": self.agent.id,
                        "step": self._steps,
                        "say": action.say,
                    })
        except TaskDeadlineExceeded:
            return await self._finish_timeout()
        except Exception as exc:  # noqa: BLE001 - worker must never crash
            self.log.exception("agent %s crashed", getattr(self.agent, "id", "?"))
            summary = f"agent 运行失败：{type(exc).__name__}: {exc}"
            try:
                return await self._finish(summary, "failed", self._steps)
            except Exception:  # pragma: no cover - sys unreachable
                self.log.exception("cannot report failure for %s", self.agent.id)
                return RunResult(summary, "failed", self._steps)

    # -- limits / deadline -------------------------------------------------
    def _max_steps(self) -> int:
        value = getattr(self.config, "max_steps", self.max_steps)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return max(1, int(self.max_steps))

    def _task_timeout_s(self) -> float:
        try:
            return max(0.0, float(getattr(self.config, "task_timeout_s", 0) or 0))
        except (TypeError, ValueError):
            return 0.0

    def _max_tool_calls(self) -> int:
        try:
            return int(getattr(self.config, "max_tool_calls_per_task", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _tool_limit_reached(self) -> bool:
        limit = self._max_tool_calls()
        return limit > 0 and self._tool_calls >= limit

    def reload_limits(self) -> dict:
        """Re-read limits after a live config push (safe mid-task)."""
        self.max_steps = self._max_steps()
        timeout = self._task_timeout_s()
        if self._timeout_s > 0 or self._deadline_at is not None:
            self._timeout_s = timeout
            if timeout > 0 and self._started_at is not None:
                self._deadline_at = self._started_at + timeout
            else:
                self._deadline_at = None
        return {"max_steps": self._max_steps(),
                "task_timeout_s": self._task_timeout_s(),
                "max_tool_calls_per_task": self._max_tool_calls()}

    def _begin_deadline(self) -> None:
        self._timeout_s = self._task_timeout_s()
        try:
            self._started_at = asyncio.get_running_loop().time()
        except RuntimeError:  # pragma: no cover - sync test helper
            self._started_at = 0.0
        self._deadline_at = (self._started_at + self._timeout_s
                             if self._timeout_s > 0 else None)

    def _remaining(self) -> float | None:
        if self._deadline_at is None:
            return None
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:  # pragma: no cover - sync test helper
            return self._deadline_at
        return self._deadline_at - now

    def _expired(self) -> bool:
        remaining = self._remaining()
        return remaining is not None and remaining <= 0

    async def _await_bounded(self, awaitable: Any) -> Any:
        """Await one step with the remaining task budget as the timeout."""
        remaining = self._remaining()
        if remaining is None:
            return await awaitable
        if remaining <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise TaskDeadlineExceeded()
        try:
            return await asyncio.wait_for(awaitable, timeout=remaining)
        except asyncio.TimeoutError as exc:
            if self._expired():
                raise TaskDeadlineExceeded() from exc
            raise

    async def _finish_timeout(self) -> RunResult:
        timeout = self._timeout_s or self._task_timeout_s()
        summary = (f"任务超时：超过 agents.task_timeout_s={timeout:g}s，"
                   f"停止并由 hub 回收。")
        self.log.warning("agent %s timed out after %ss", self.agent.id, timeout)
        return await self._finish(
            summary, "blocked", self._steps,
            extra={"timeout": True, "task_timeout_s": timeout,
                   "tool_calls": self._tool_calls})

    async def _finish_tool_limit(self, action: Action) -> RunResult:
        limit = self._max_tool_calls()
        summary = (f"工具调用次数达到上限 agents.max_tool_calls_per_task="
                   f"{limit}，停止（最后一次尝试：{action.tool}）。")
        self.log.warning("agent %s hit the tool-call limit (%s)",
                         self.agent.id, limit)
        return await self._finish(
            summary, "blocked", self._steps,
            extra={"limit": "max_tool_calls_per_task",
                   "max_tool_calls_per_task": limit,
                   "tool_calls": self._tool_calls})

    # -- git isolation (attached-rw only) ---------------------------------
    def _security_config(self) -> Any:
        return getattr(self.executor, "config", None)

    async def _git_begin(self) -> None:
        """Put an attached-rw workspace on a per-task branch (best effort)."""
        security = self._security_config()
        if not bool(getattr(security, "workspace_git", False)):
            return
        if not self.agent.task_id or not self.agent.project_id:
            return
        try:
            project = _as_dict(await call_ok(
                self.sys, "project.get",
                {"project_id": self.agent.project_id}), "project")
        except SfoError:
            return
        if str(project.get("workspace_mode") or "") != "attached-rw":
            return
        workspace = Path(str(project.get("workspace") or self.workspace))
        if not workspace.is_dir():
            return
        try:
            git = WorkspaceGit(workspace, logger=self.log)
            git.ensure_repo()
            info = git.task_branch(self.agent.project_id, self.agent.task_id)
        except GitError as exc:
            self.log.warning("workspace git unavailable for %s: %s",
                             self.agent.project_id, exc)
            return
        except Exception:  # pragma: no cover - git must never kill a task
            self.log.exception("workspace git setup failed for %s",
                               self.agent.project_id)
            return
        self._git = git
        self._git_branch = str(info.get("branch") or "")
        self._git_started = True
        await self._emit("agent.working", {
            "agent_id": self.agent.id,
            "role": self.agent.role,
            "task_id": self.agent.task_id,
            "git_branch": self._git_branch,
        })

    async def _git_end(self, status: str) -> dict:
        """Commit the task branch (when enabled) and report its state."""
        git = self._git
        if git is None:
            return {}
        self._git = None
        result: dict = {"branch": self._git_branch or ""}
        security = self._security_config()
        commit = bool(getattr(security, "workspace_git_commit", True))
        try:
            if commit:
                result.update(git.commit_task(
                    self.agent.task_id, status=status,
                    message=(f"sfo: {self.agent.task_id or self.agent.id} "
                             f"({status})")))
            else:
                result.update(git.diff_summary())
        except GitError as exc:
            self.log.warning("task commit failed for %s: %s",
                             self.agent.task_id, exc)
            result["error"] = str(exc)
        except Exception:  # pragma: no cover - git must never kill a task
            self.log.exception("task commit crashed for %s",
                               self.agent.task_id)
        result.setdefault("branch", self._git_branch)
        return result

    # -- tool execution helpers --------------------------------------------
    async def _execute(self, action: Action) -> ToolResult:
        """Run one tool call; role-forbidden calls become failed results."""
        tool = str(action.tool)
        allowed = {spec.name for spec in tools_for(self.agent.role)}
        if allowed and tool in TOOLS and tool not in allowed:
            return ToolResult(
                ok=False,
                error=(f"tool {tool!r} is not available to role "
                       f"{self.agent.role!r}; available: {sorted(allowed)}"))
        return await self.executor.run(tool, dict(action.args or {}))

    async def _observe(self, action: Action, result: ToolResult) -> None:
        """Feed the tool outcome back to brains that keep a conversation."""
        observe = getattr(self.brain, "observe", None)
        if observe is None:
            return
        try:
            outcome = observe(action, result)
            if hasattr(outcome, "__await__"):
                await outcome
        except Exception:  # noqa: BLE001 - memory failure must not kill the agent
            self.log.exception("brain.observe failed for %s", self.agent.id)

    def _track_failure(self, action: Action, result: ToolResult) -> str:
        """Return a stop reason when the agent is looping on failures."""
        if result.ok:
            self._fail_streak = 0
            self._repeat_key = None
            self._repeat_count = 0
            return ""
        self._fail_streak += 1
        try:
            key = (action.tool, json.dumps(action.args or {}, sort_keys=True,
                                           default=str))
        except Exception:  # pragma: no cover - unserialisable args
            key = (action.tool, repr(action.args))
        if key == self._repeat_key:
            self._repeat_count += 1
        else:
            self._repeat_key, self._repeat_count = key, 1
        denied = str(result.error or "").startswith("DENIED")
        if denied and self._repeat_count >= MAX_REPEAT_DENIED:
            return (f"工具 {action.tool} 被拒绝 {self._repeat_count} 次，停止重试："
                    f"{result.error}")
        if self._repeat_count >= MAX_REPEAT_FAILURES:
            return (f"同一调用 {action.tool} 连续失败 {self._repeat_count} 次，"
                    f"停止：{result.error}")
        if self._fail_streak >= MAX_FAIL_STREAK:
            return f"连续 {self._fail_streak} 次工具调用失败，停止：{result.error}"
        return ""

    # -- lifecycle helpers ------------------------------------------------
    async def _claim(self) -> RunResult | None:
        """Claim the assigned task if it is still ``todo``.

        A conflict means another agent owns it; we abort instead of taking
        over (the task board is authoritative).
        """
        task_id = self.agent.task_id
        if not task_id:
            return None
        try:
            task = _as_dict(await call_ok(self.sys, "task.get",
                                          {"task_id": task_id}), "task")
        except NotFound:
            return await self._abort(f"任务 {task_id} 不存在。", "failed")
        except SfoError as exc:
            return await self._abort(f"读取任务失败：{exc.code}: {exc.message}",
                                     "failed")
        if _task_status(task) != "todo":
            return None
        try:
            await call_ok(self.sys, "task.claim",
                          {"task_id": task_id, "agent_id": self.agent.id})
        except Conflict as exc:
            return await self._abort(
                f"认领任务 {task_id} 冲突，已被其他 Agent 持有：{exc.message}",
                "failed")
        except SfoError as exc:
            return await self._abort(
                f"认领任务 {task_id} 失败：{exc.code}: {exc.message}", "failed")
        return None

    async def _mark_working(self) -> None:
        await self._update_agent("working")
        await self._emit("agent.working", {
            "agent_id": self.agent.id,
            "role": self.agent.role,
            "project_id": self.agent.project_id,
            "task_id": self.agent.task_id,
        })

    async def _view(self) -> AgentView:
        project: dict = {}
        if self.agent.project_id:
            try:
                project = _as_dict(await call_ok(
                    self.sys, "project.get",
                    {"project_id": self.agent.project_id}), "project")
            except SfoError:
                project = {}

        task: dict | None = None
        if self.agent.task_id:
            try:
                task = _as_dict(await call_ok(
                    self.sys, "task.get", {"task_id": self.agent.task_id}), "task")
            except SfoError:
                task = None

        tasks: list[dict] = []
        if self.agent.project_id:
            try:
                payload = await call_ok(
                    self.sys, "task.list", {"project_id": self.agent.project_id})
                tasks = _as_list(payload, "tasks")
            except SfoError:
                tasks = []

        if self.agent.project_id:
            await self._pull_events()

        return AgentView(
            agent=self.agent,
            project=project,
            task=task,
            tasks=tasks,
            events=list(self._events),
            step=self._steps,
            workspace=str(self.workspace),
            recent=list(self._events[-6:]),
            tool_results=list(self._tool_results),
        )

    async def _pull_events(self) -> None:
        """Fetch events newer than the last seen seq and keep a small window."""
        payload: dict[str, Any] = {"seq": self._last_seq, "limit": 50}
        if self.agent.project_id:
            payload["project_id"] = self.agent.project_id
        try:
            raw = await call_ok(self.sys, "event.since", payload)
        except SfoError:
            return
        fresh = _as_list(raw, "events")
        known = {int(event.get("seq") or 0) for event in self._events}
        for event in fresh:
            seq = int(event.get("seq") or 0)
            if seq and seq in known:
                continue
            self._events.append(event)
            known.add(seq)
            if seq > self._last_seq:
                self._last_seq = seq
        self._events = self._events[-40:]

    # -- finish / abort ---------------------------------------------------
    async def _finish(self, summary: str, status: str, steps: int, *,
                      extra: dict | None = None) -> RunResult:
        """Record the outcome on the task, the agent roster and the event log."""
        git_info = await self._git_end(status) if status != "paused" else {}
        payload_extra = dict(extra or {})
        if git_info:
            payload_extra.setdefault("git", git_info)
        await self._note_task(summary)
        if self.agent.task_id:
            if status == "done":
                await self._set_task_status("review", summary)
                await self._emit("task.review", {
                    "task_id": self.agent.task_id,
                    "agent_id": self.agent.id,
                    "summary": summary,
                })
            elif status in ("failed", "blocked"):
                await self._block_task(summary)
        if status == "paused":
            await self._update_agent("waiting")
            self.agent.status = "waiting"
            self.agent.summary = summary
            self.agent.touch()
            await self._emit("agent.waiting", {
                "agent_id": self.agent.id,
                "role": self.agent.role,
                "project_id": self.agent.project_id,
                "task_id": self.agent.task_id,
                "reason": "paused",
            })
            return RunResult(summary, status, steps)

        agent_state = {"done": "done", "failed": "failed",
                       "blocked": "failed", "killed": "dead"}.get(status, "done")
        await self._call_quiet("agent.finish",
                               {"agent_id": self.agent.id, "summary": summary})
        if agent_state != "done":
            await self._update_agent(agent_state)
        self.agent.status = agent_state
        self.agent.summary = summary
        if status in ("failed", "blocked"):
            self.agent.error = summary
        self.agent.touch()
        event_type = {"done": "agent.finished", "failed": "agent.failed",
                      "blocked": "agent.failed", "killed": "agent.terminated"}.get(
            status, "agent.finished")
        payload = {
            "agent_id": self.agent.id,
            "role": self.agent.role,
            "project_id": self.agent.project_id,
            "task_id": self.agent.task_id,
            "summary": summary,
            "status": agent_state,
        }
        payload.update(payload_extra)
        await self._emit(event_type, payload)
        return RunResult(summary, status, steps)

    async def _abort(self, summary: str, status: str) -> RunResult:
        """Abort before owning a task (claim conflict / missing task)."""
        await self._call_quiet("agent.finish",
                               {"agent_id": self.agent.id, "summary": summary})
        await self._update_agent("failed")
        self.agent.status = "failed"
        self.agent.error = summary
        self.agent.summary = summary
        self.agent.touch()
        await self._emit("agent.failed", {
            "agent_id": self.agent.id,
            "role": self.agent.role,
            "project_id": self.agent.project_id,
            "task_id": self.agent.task_id,
            "summary": summary,
        })
        return RunResult(summary, status, self._steps)

    async def _note_task(self, summary: str) -> None:
        if not self.agent.task_id:
            return
        text = summary if len(summary) <= 2000 else summary[:1999] + "…"
        await self._call_quiet("task.note", {
            "task_id": self.agent.task_id,
            "text": text,
            "author": self.agent.id,
        })

    async def _set_task_status(self, new_status: str, summary: str) -> None:
        task_id = self.agent.task_id
        if not task_id:
            return
        try:
            current = _as_dict(await call_ok(self.sys, "task.get",
                                             {"task_id": task_id}), "task")
        except SfoError:
            current = {}
        if _task_status(current) in ("review", "done"):
            return
        try:
            await call_ok(self.sys, "task.update",
                          {"task_id": task_id, "patch": {
                              "status": new_status,
                          }})
        except (BadRequest, ValidationError) as exc:
            self.log.warning("cannot set task %s to %s: %s", task_id,
                             new_status, exc)
        except SfoError as exc:
            self.log.warning("cannot set task %s to %s: %s", task_id,
                             new_status, exc)

    async def _block_task(self, summary: str) -> None:
        task_id = self.agent.task_id
        if not task_id:
            return
        try:
            current = _as_dict(await call_ok(self.sys, "task.get",
                                             {"task_id": task_id}), "task")
        except SfoError:
            current = {}
        if _task_status(current) in ("done", "blocked"):
            return
        try:
            await call_ok(self.sys, "task.update",
                          {"task_id": task_id, "patch": {
                              "status": "blocked",
                              "error": summary[:1000],
                          }})
        except SfoError as exc:
            self.log.warning("cannot block task %s: %s", task_id, exc)
        await self._emit("task.blocked", {
            "task_id": task_id,
            "agent_id": self.agent.id,
            "error": summary,
        })

    async def _update_agent(self, status: str) -> None:
        try:
            await call_ok(self.sys, "agent.update", {
                "agent_id": self.agent.id,
                "patch": {"status": status, "last_seen": now_ms()},
            })
        except SfoError:
            self.log.debug("agent.update failed for %s", self.agent.id,
                           exc_info=True)

    async def _call_quiet(self, action: str, payload: dict) -> None:
        try:
            await call_ok(self.sys, action, payload)
        except SfoError:
            self.log.debug("%s failed", action, exc_info=True)

    async def _emit(self, type: str, data: dict) -> None:
        try:
            await emit_event(self.sys, type, data,
                             project_id=self.agent.project_id,
                             source=self.source)
        except Exception:  # pragma: no cover - telemetry is best effort
            self.log.debug("cannot emit %s", type, exc_info=True)

    def _bump_stats(self, result: ToolResult) -> None:
        self.agent.stats["steps"] = int(self.agent.stats.get("steps", 0)) + 1
        self.agent.stats["tool_calls"] = (
            int(self.agent.stats.get("tool_calls", 0)) + 1)
        if result.ok:
            self.agent.stats["ok_calls"] = (
                int(self.agent.stats.get("ok_calls", 0)) + 1)


__all__ = ["AgentRunner", "RunResult", "TERMINAL_RUN_STATUSES"]

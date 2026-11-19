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

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import BadRequest, Conflict, NotFound, SfoError, ValidationError
from ..util.logging import get_logger
from .api import call_ok, emit_event
from .brain import AgentView, Brain
from .models import Agent, now_ms
from .tools import ToolExecutor, ToolResult

#: Terminal run statuses returned to the worker glue.
TERMINAL_RUN_STATUSES = ("done", "failed", "blocked", "killed", "paused")


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
        """Run the brain/tool loop and return a final :class:`RunResult`."""
        try:
            claim_result = await self._claim()
            if claim_result is not None:
                return claim_result
            await self._mark_working()
            while True:
                if self._killed:
                    return await self._finish("收到 kill 请求，停止工作。", "killed",
                                              self._steps)
                if self._paused:
                    return await self._finish("收到 pause 请求，暂停工作。", "paused",
                                              self._steps)
                if self._steps >= self.max_steps:
                    return await self._finish(
                        f"达到最大步数 max_steps={self.max_steps}，停止。",
                        "failed", self._steps)
                view = await self._view()
                action = await self.brain.next_action(view)
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
                result = await self.executor.run(action.tool,
                                                 dict(action.args or {}))
                self._steps += 1
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
                if action.say:
                    await self._emit("agent.step", {
                        "agent_id": self.agent.id,
                        "step": self._steps,
                        "say": action.say,
                    })
        except Exception as exc:  # noqa: BLE001 - worker must never crash
            self.log.exception("agent %s crashed", getattr(self.agent, "id", "?"))
            summary = f"agent 运行失败：{type(exc).__name__}: {exc}"
            try:
                return await self._finish(summary, "failed", self._steps)
            except Exception:  # pragma: no cover - sys unreachable
                self.log.exception("cannot report failure for %s", self.agent.id)
                return RunResult(summary, "failed", self._steps)

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
    async def _finish(self, summary: str, status: str, steps: int) -> RunResult:
        """Record the outcome on the task, the agent roster and the event log."""
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
        await self._emit(event_type, {
            "agent_id": self.agent.id,
            "role": self.agent.role,
            "project_id": self.agent.project_id,
            "task_id": self.agent.task_id,
            "summary": summary,
            "status": agent_state,
        })
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

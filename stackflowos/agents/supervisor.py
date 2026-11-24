"""Hub-side agent process manager.

``AgentSupervisor`` owns the roster of agents and the worker subprocesses
behind them.  It does not speak the wire protocol itself: it allocates a unit
through the injected ``allocator``, starts ``stackflowos.worker`` with
``subprocess``, waits for the registry to report the unit online, and persists
a roster snapshot.

Everything external is duck-typed on purpose (registry, allocator, eventlog),
so this module can be tested — and imported — without transport/l2/security.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..errors import BadRequest, Conflict, NotFound, Unavailable
from ..util.jsonio import read_json, write_json
from ..util.logging import get_logger
from .hotreload import public_runtime_config
from .models import (AGENT_STATUSES, TERMINAL_AGENT_STATUSES, Agent,
                     display_name_for, now_ms)
from .queue import (DispatchQueue, QueueEntry, QueueFull, compute_priority,
                    clamp_priority)
from .roles import get_role

ControlFn = Callable[[str, str, dict], Awaitable[Any]]

#: Statuses that own an OS process (queued agents do not count against
#: ``agents.max_workers`` and are not signalled on shutdown).
RUNNING_AGENT_STATUSES = ("spawning", "idle", "working", "waiting")
QUEUED_AGENT_STATUS = "queued"

# S3: bounded TERM→KILL escalation.  Tests shorten these through the
# constructor instead of sleeping for real seconds.
_TERM_GRACE_S = 3.0
_KILL_GRACE_S = 2.0
_EXIT_POLL_S = 0.05


def _group_id(pid: int) -> int | None:
    """Process-group id for a worker ``pid``; ``None`` when unsupported."""
    if pid <= 0 or not callable(getattr(os, "killpg", None)):
        return None
    getpgid = getattr(os, "getpgid", None)
    if callable(getpgid):
        try:
            return int(getpgid(pid))
        except (ProcessLookupError, PermissionError, OSError):
            pass
    # Workers are session leaders (``start_new_session=True``), so a group id
    # never changes: the pid still addresses orphaned grandchildren after the
    # leader itself was reaped.
    return pid


def _group_alive(pgid: int) -> bool:
    """Whether *pgid* still has members (best effort; assumes alive)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # cannot tell: assume alive so the caller escalates
    return True


def _process_exited(proc: Any) -> bool:
    """Return True once the child exited, reaping it through ``poll``."""
    if getattr(proc, "returncode", None) is not None:
        return True
    poll = getattr(proc, "poll", None)
    if not callable(poll):
        return False
    try:
        return poll() is not None
    except (OSError, ValueError):  # pragma: no cover - defensive
        return False


def _signal_process(proc: Any, pgid: int | None, *, kill: bool) -> bool:
    """Signal the whole group, falling back to the direct child."""
    sig = getattr(signal, "SIGKILL" if kill else "SIGTERM", None)
    killpg = getattr(os, "killpg", None)
    if pgid is not None and sig is not None and callable(killpg):
        try:
            killpg(pgid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            pass
    method = getattr(proc, "kill" if kill else "terminate", None)
    if not callable(method):
        return False
    try:
        method()
        return True
    except (ProcessLookupError, OSError):
        return False


async def _wait_gone(proc: Any, pgid: int | None, timeout: float) -> bool:
    """Wait (bounded) until the child is reaped and its group is empty."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    while True:
        if _process_exited(proc) and (pgid is None or not _group_alive(pgid)):
            return True
        remaining = deadline - loop.time()
        if remaining <= 0.0:
            return False
        await asyncio.sleep(min(_EXIT_POLL_S, remaining))


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it is awaitable (registry may be sync or async)."""
    if inspect.isawaitable(value):
        return await value
    return value


def _unit_field(unit: Any, name: str, default: Any = None) -> Any:
    if unit is None:
        return default
    if isinstance(unit, dict):
        return unit.get(name, default)
    return getattr(unit, name, default)


class AgentSupervisor:
    """Spawn, stop and track agent worker processes."""

    def __init__(self, ctx: Any, *, registry: Any, allocator: Any, eventlog: Any,
                 config: Any, sys_rpc: str, state_dir: Path, root: Path,
                 popen_factory: Any = None, command_factory: Any = None,
                 spawn_timeout_s: float = 15.0, logger: Any = None,
                 control: ControlFn | None = None,
                 token_issuer: Any = None,
                 token_revoker: Any = None,
                 terminate_grace_s: float | None = None,
                 kill_grace_s: float | None = None) -> None:
        self.ctx = ctx
        self.registry = registry
        self.allocator = allocator
        self.eventlog = eventlog
        self.config = getattr(config, "agents", config)
        self.max_workers = int(getattr(self.config, "max_workers", 6))
        self.queue_max = max(1, int(getattr(self.config, "queue_max", 64)))
        self.task_timeout_s = float(getattr(self.config, "task_timeout_s", 1800.0))
        self.max_tool_calls_per_task = int(
            getattr(self.config, "max_tool_calls_per_task", 200))
        self.config_reload_s = float(getattr(self.config, "config_reload_s", 10.0))
        self.sys_rpc = str(sys_rpc)
        self.state_dir = Path(state_dir)
        self.root = Path(root)
        self.spawn_timeout_s = float(spawn_timeout_s)
        #: Priority dispatch queue (agents.max_workers reached -> park here).
        self.queue = DispatchQueue(max_size=self.queue_max)
        #: Optional callbacks wired by the agents extension (build(app)):
        #: ``task_lookup(task_id)`` returns a Task/dict for deps + priority,
        #: ``task_requeue(task_id)`` moves a stuck ``doing`` task back to todo.
        self.task_lookup: Callable[[str], Any] | None = None
        self.task_requeue: Callable[[str], Any] | None = None
        #: Last non-secret payload pushed to workers by :meth:`on_config`.
        self.last_pushed_config: dict = {}
        self._drain_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None
        self._starting: set[str] = set()
        self._closing = False
        # S3: how long a terminating worker gets between SIGTERM and SIGKILL,
        # and between SIGKILL and giving up on the pid.
        self.terminate_grace_s = (_TERM_GRACE_S if terminate_grace_s is None
                                  else max(0.0, float(terminate_grace_s)))
        self.kill_grace_s = (_KILL_GRACE_S if kill_grace_s is None
                             else max(0.0, float(kill_grace_s)))
        self.log = logger or get_logger("agents.supervisor")
        self.control = control
        # Protocol v2 credential hooks (H3). ``token_issuer(work_id,
        # project_id, role, agent_id, task_id)`` returns the unit token handed
        # to the worker through SFO_UNIT_TOKEN; ``token_revoker(work_id)``
        # invalidates it when the process is released/restarted/killed.  The
        # scope fields (project/role/agent/task) travel with the credential so
        # later RPC calls can be authorized without trusting the wire.
        self.token_issuer = token_issuer
        self.token_revoker = token_revoker
        self._popen_factory = popen_factory
        self._command_factory = command_factory

        self._agents: dict[str, Agent] = {}
        self._seen_online: set[str] = set()
        self._counters: dict[str, int] = {}
        self._procs: dict[str, Any] = {}
        self._pids: dict[str, int] = {}
        self._log_handles: dict[str, Any] = {}
        self._addresses: dict[str, Any] = {}

        self.state_path = self.state_dir / "state" / "agents.json"
        self.logs_dir = self.state_dir / "logs" / "agents"
        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover - read-only state dir
            self.log.warning("cannot create agent log dir %s", self.logs_dir)

    # ------------------------------------------------------------------
    # command construction
    # ------------------------------------------------------------------
    def build_command(self, role: str, project_id: str | None,
                      task_id: str | None, work_id: str) -> list[str]:
        """Build the default worker command line."""
        command = [
            sys.executable,
            "-m", "stackflowos.worker",
            "--sys", self.sys_rpc,
            "--work-id", work_id,
            "--role", role,
            "--state-dir", str(self.state_dir),
            "--root", str(self.root),
        ]
        if project_id:
            command += ["--project", project_id]
        if task_id:
            command += ["--task", task_id]
        return command

    def _command(self, role: str, project_id: str | None,
                 task_id: str | None, work_id: str) -> list[str]:
        factory = self._command_factory
        if factory is not None:
            return [str(part) for part in
                    factory(role, project_id, task_id, work_id)]
        return self.build_command(role, project_id, task_id, work_id)

    def _popen(self, command: list[str], kwargs: dict) -> Any:
        factory = self._popen_factory
        if factory is not None:
            return factory(command, kwargs)
        return subprocess.Popen(command, **kwargs)

    async def _issue_token(self, work_id: str, project_id: str | None,
                           role: str, agent_id: str | None,
                           task_id: str | None = None) -> str | None:
        """Ask the hub for this unit's token (sync or async issuer)."""
        issuer = self.token_issuer
        if issuer is None:
            return None
        value = issuer(work_id, project_id, role, agent_id, task_id)
        if inspect.isawaitable(value):
            value = await value
        return str(value) if value else None

    def _revoke_token(self, work_id: str | None) -> None:
        revoker = self.token_revoker
        if revoker is None or not work_id:
            return
        try:
            value = revoker(str(work_id))
            if inspect.isawaitable(value):
                try:
                    asyncio.get_running_loop().create_task(value)
                except RuntimeError:  # pragma: no cover - no loop
                    pass
        except Exception:  # pragma: no cover - revocation is best effort
            self.log.debug("revoke token for %s failed", work_id, exc_info=True)

    # ------------------------------------------------------------------
    # spawn
    # ------------------------------------------------------------------
    async def spawn(self, role: str, project_id: str | None = None, *,
                    task_id: str | None = None, reason: str = "",
                    priority: int | None = None) -> Agent:
        """Spawn a worker -- or park the request when capacity is exhausted.

        Compatibility: the old signature ``spawn(role, project_id,
        task_id=..., reason=...)`` still works.  The new ``priority`` keyword
        overrides the derived value (task priority + role weight).

        At ``agents.max_workers`` running workers the request is queued (status
        ``queued``) instead of raising; :meth:`drain_queue` starts it when a
        slot frees, and ``agents.queue_max`` bounds the waiters.
        """
        get_role(role)  # validate role before allocating anything
        task = self._ready_task(task_id)
        chosen = compute_priority(role, task, priority)
        if self._running_count() >= self.max_workers:
            return self._enqueue(role, project_id, task_id, reason,
                                 priority=chosen)
        work_id = str(self.allocator.new_work_id(role))
        agent = self.register(role=role, project_id=project_id,
                              unit_id=work_id, task_id=task_id)
        agent.stats["priority"] = chosen
        await self._allocate_and_launch(agent, role=role,
                                        project_id=project_id,
                                        task_id=task_id, reason=reason,
                                        work_id=work_id, priority=chosen)
        return agent

    # -- priority queue ---------------------------------------------------
    def _running_count(self) -> int:
        return sum(1 for agent in self._agents.values()
                   if agent.status in RUNNING_AGENT_STATUSES)

    def _ready_task(self, task_id: str | None) -> Any:
        """Look up the task and enforce dependency ordering (S2 contract).

        Only wired when the agents extension installs ``task_lookup`` (the real
        hub); bare unit tests keep the old permissive behaviour.  A task whose
        ``deps`` are not all ``done`` is refused with :class:`Conflict` so a
        lead cannot dispatch downstream work early.
        """
        lookup = self.task_lookup
        if lookup is None or not task_id:
            return None
        task = lookup(task_id)
        if task is None:
            return None
        deps = _unit_field(task, "deps") or []
        pending: list[dict] = []
        for dep_id in deps:
            try:
                dep = lookup(str(dep_id))
            except Exception:  # a dangling dep must fail closed (not dispatch)
                dep = None
            status = _unit_field(dep, "status")
            if status != "done":
                pending.append({"task_id": str(dep_id), "status": status or "missing"})
        if pending:
            raise Conflict(
                f"task {task_id} is blocked by unfinished dependencies",
                detail={"task_id": str(task_id), "blocked_by": pending})
        return task

    def _enqueue(self, role: str, project_id: str | None,
                 task_id: str | None, reason: str, *, priority: int) -> Agent:
        """Park one spawn request; raise :class:`Conflict` when full."""
        if len(self.queue) >= self.queue.max_size:
            raise Conflict(
                f"agent queue is full: {len(self.queue)}/{self.queue.max_size}",
                detail={"queue_max": self.queue.max_size})
        agent = self.register(role=role, project_id=project_id, unit_id="",
                              task_id=task_id, status=QUEUED_AGENT_STATUS)
        agent.stats["priority"] = clamp_priority(priority)
        agent.summary = f"queued (priority {agent.stats['priority']})"
        agent.touch()
        try:
            entry = self.queue.put(QueueEntry(
                agent_id=agent.id, role=role, project_id=project_id,
                task_id=task_id, priority=int(agent.stats["priority"]),
                reason=reason))
        except QueueFull as exc:
            # Roll the roster back so a refused request leaves no ghost agent.
            self._agents.pop(agent.id, None)
            raise Conflict(
                f"agent queue is full: {len(self.queue)}/{self.queue.max_size}",
                detail={"queue_max": self.queue.max_size}) from exc
        self.save()
        self._emit("agent.queued", {
            "queue_id": entry.id,
            "agent_id": agent.id,
            "role": role,
            "project_id": project_id,
            "task_id": task_id,
            "priority": entry.priority,
            "position": len(self.queue),
            "reason": reason,
        })
        return agent

    def _schedule_drain(self) -> None:
        """Start a background drain when a slot may have opened up."""
        if self._closing or not self.queue:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - sync test call
            return
        if self._drain_task is not None and not self._drain_task.done():
            return
        self._drain_task = loop.create_task(self._drain_safe())
        self._drain_task.add_done_callback(self._drain_done)

    def _drain_done(self, task: asyncio.Task) -> None:
        if self._drain_task is task:
            self._drain_task = None
        if not task.cancelled() and task.exception() is not None:
            self.log.warning("queue drain failed: %s", task.exception())

    async def _drain_safe(self) -> None:
        try:
            await self.drain_queue()
        except Exception:  # pragma: no cover - drain must never kill the hub
            self.log.exception("queue drain crashed")

    async def drain_queue(self) -> list[Agent]:
        """Start as many queued spawns as free slots allow (FIFO by priority)."""
        started: list[Agent] = []
        guard = 0
        while self.queue and self._running_count() < self.max_workers:
            entry = self.queue.pop()
            if entry is None:  # pragma: no cover - len() checked above
                break
            guard += 1
            if guard > self.queue.max_size + 1:  # pragma: no cover - defensive
                break
            agent = self._agents.get(entry.agent_id)
            if agent is None or agent.status != QUEUED_AGENT_STATUS:
                continue
            self._emit("agent.dequeued", {
                "queue_id": entry.id,
                "agent_id": agent.id,
                "role": entry.role,
                "project_id": entry.project_id,
                "task_id": entry.task_id,
                "priority": entry.priority,
                "reason": entry.reason,
            })
            self._starting.add(agent.id)
            try:
                await self._allocate_and_launch(
                    agent, role=entry.role, project_id=entry.project_id,
                    task_id=entry.task_id, reason=entry.reason,
                    priority=entry.priority)
            except Exception as exc:  # keep draining the rest
                self.log.warning("queued spawn %s failed: %s", agent.id, exc)
                agent.status = "failed"
                agent.error = f"dequeue failed: {exc}"
                agent.touch()
                self.save()
                self._emit("agent.failed", {
                    "agent_id": agent.id, "role": entry.role,
                    "project_id": entry.project_id,
                    "task_id": entry.task_id,
                    "summary": agent.error,
                })
            else:
                started.append(agent)
            finally:
                self._starting.discard(agent.id)
        self.save()
        return started

    async def _allocate_and_launch(self, agent: Agent, *, role: str,
                                   project_id: str | None,
                                   task_id: str | None, reason: str,
                                   work_id: str | None = None,
                                   priority: int = 50) -> Agent:
        """Allocate a unit, launch its worker and report it online."""
        work_id = str(work_id or self.allocator.new_work_id(role))
        agent.unit_id = work_id
        agent.stats["priority"] = int(priority)
        try:
            addresses = await _maybe_await(self.allocator.allocate(work_id))
        except Exception as exc:
            raise Unavailable(
                f"cannot allocate unit {work_id}: {exc}") from exc
        self._addresses[work_id] = addresses
        try:
            token = await self._issue_token(work_id, project_id, role,
                                            agent.id, task_id)
            await self._launch(agent, work_id, token=token)
        except Exception as exc:
            agent.status = "failed"
            agent.error = f"spawn failed: {exc}"
            agent.touch()
            self._release(work_id)
            self.save()
            self._emit("agent.failed", {
                "agent_id": agent.id, "role": role,
                "project_id": project_id, "task_id": task_id,
                "summary": agent.error,
            })
            raise
        if agent.status in ("spawning", QUEUED_AGENT_STATUS):
            agent.status = "idle"
        agent.error = None
        agent.touch()
        agent.stats["pid"] = self._pids.get(work_id, 0)
        self._emit("agent.spawned", {
            "agent_id": agent.id,
            "role": role,
            "project_id": project_id,
            "task_id": task_id,
            "unit_id": work_id,
            "display_name": agent.display_name,
            "model": agent.model,
            "reason": reason,
            "priority": int(priority),
        })
        self.save()
        self._ensure_monitor()
        return agent

    def cancel_queued(self, queue_id: str, *,
                      reason: str = "canceled") -> dict:
        """Cancel one queued spawn before it starts (operator action)."""
        entry = self.queue.get(str(queue_id))
        if entry is None:
            # Accept an agent id as convenience (the UI knows both).
            entry = next((candidate for candidate in self.queue.entries()
                          if candidate.agent_id == str(queue_id)), None)
        if entry is None:
            raise NotFound(f"queued spawn not found: {queue_id}")
        agent = self._agents.get(entry.agent_id)
        if agent is None:
            self.queue.remove(entry.id)
            raise NotFound(f"agent not found: {entry.agent_id}")
        if (agent.id in self._starting
                or agent.status != QUEUED_AGENT_STATUS):
            raise Conflict(
                f"agent {agent.id} already left the queue (status={agent.status})")
        self.queue.remove(entry.id)
        agent.status = "dead"
        agent.summary = f"queue canceled: {reason}"
        agent.error = None
        agent.touch()
        self.save()
        payload = {
            "queue_id": entry.id,
            "agent_id": agent.id,
            "role": agent.role,
            "project_id": agent.project_id,
            "task_id": agent.task_id,
            "priority": entry.priority,
            "canceled": True,
            "reason": reason,
        }
        self._emit("agent.dequeued", payload)
        return {"queue_id": entry.id, "canceled": True,
                "agent": agent.to_dict()}

    def queue_snapshot(self, *, project_id: str | None = None) -> dict:
        """Status view for ``GET /api/agents/queue``."""
        queued = self.queue.snapshot(project_id=project_id)
        for item in queued:
            agent = self._agents.get(str(item.get("agent_id") or ""))
            if agent is not None:
                item["display_name"] = agent.display_name
                item["model"] = agent.model
                item["status"] = agent.status
                item["created_at"] = agent.created_at
        running = self._running_count()
        return {
            "queued": queued,
            "count": len(queued),
            "running": running,
            "max_workers": self.max_workers,
            "queue_max": self.queue.max_size,
            "available": max(0, self.max_workers - running),
        }

    async def _launch(self, agent: Agent, work_id: str,
                      token: str | None = None) -> None:
        """Start the worker process and wait for ``unit.register``."""
        command = self._command(agent.role, agent.project_id, agent.task_id,
                                work_id)
        handle = self._open_log(work_id)
        env = os.environ.copy()
        env["SFO_ROOT"] = str(self.root)
        env["SFO_STATE_DIR"] = str(self.state_dir)
        if token:
            # Never in argv: argv shows up in ps/logs. The worker only reads
            # this env var and its shell environment strips SFO_* anyway.
            env["SFO_UNIT_TOKEN"] = token
        # The worker runs with cwd=<root> (the operator's workspace), which is
        # not necessarily where this package lives (tests use a temp root).
        # Put both on sys.path so `python -m stackflowos.worker` resolves.
        package_root = Path(__file__).resolve().parents[2]
        parts = [str(self.root), str(package_root)]
        existing = env.get("PYTHONPATH", "")
        if existing:
            parts.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(parts)
        kwargs = {
            "cwd": str(self.root),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": handle,
            "stderr": subprocess.STDOUT,
            "start_new_session": True,
        }
        try:
            proc = self._popen(command, kwargs)
        except Exception as exc:
            self._close_log(work_id)
            raise Unavailable(f"cannot start worker {work_id}: {exc}") from exc
        self._procs[work_id] = proc
        pid = int(getattr(proc, "pid", 0) or 0)
        if pid:
            self._pids[work_id] = pid
        self.log.info("spawned %s as %s (pid=%s)", agent.role, work_id, pid or "-")

        online = await self._wait_online(work_id)
        if not online:
            await self._terminate(work_id)
            self._close_log(work_id)
            raise Unavailable(
                f"worker {work_id} did not come online within "
                f"{self.spawn_timeout_s:g}s")

    def note_online(self, work_id: str) -> None:
        """Record that ``work_id`` registered, even if it exits immediately.

        Short-lived workers can register and finish inside one poll interval;
        without this the spawn would report a false failure.
        """
        self._seen_online.add(str(work_id))

    async def _wait_online(self, work_id: str) -> bool:
        deadline = time.monotonic() + max(0.0, self.spawn_timeout_s)
        while True:
            if str(work_id) in self._seen_online:
                return True
            try:
                unit = await _maybe_await(self.registry.get(work_id))
            except Exception:  # registry hiccup must not abort a spawn
                self.log.debug("registry.get(%s) failed", work_id, exc_info=True)
                unit = None
            if _unit_field(unit, "status") == "online":
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.2)

    # ------------------------------------------------------------------
    # restart / kill
    # ------------------------------------------------------------------
    async def restart(self, unit: Any, attempt: int) -> bool:
        """Replace the dead worker behind ``unit`` with a fresh process."""
        old_work_id = _unit_field(unit, "work_id")
        if not old_work_id:
            return False
        agent = next((candidate for candidate in self._agents.values()
                      if candidate.unit_id == old_work_id), None)
        if agent is None:
            return False
        self._release(old_work_id)
        await self._terminate(old_work_id)
        self._close_log(old_work_id)

        new_work_id = str(self.allocator.new_work_id(agent.role))
        # Bind the roster entry to the new unit *before* launching so the
        # worker's agent.register dedupes onto this agent instead of creating
        # a second one for the same replacement process.
        agent.unit_id = new_work_id
        try:
            addresses = await _maybe_await(self.allocator.allocate(new_work_id))
            self._addresses[new_work_id] = addresses
            token = await self._issue_token(new_work_id, agent.project_id,
                                            agent.role, agent.id, agent.task_id)
            await self._launch(agent, new_work_id, token=token)
        except Exception as exc:
            self.log.warning("restart of %s failed: %s", agent.id, exc)
            agent.status = "failed"
            agent.error = f"restart {attempt} failed: {exc}"
            agent.touch()
            self._release(new_work_id)
            self.save()
            self._emit("agent.failed", {
                "agent_id": agent.id, "role": agent.role,
                "project_id": agent.project_id, "task_id": agent.task_id,
                "summary": agent.error,
            })
            return False

        if agent.status == "spawning":
            agent.status = "idle"
        agent.error = None
        agent.touch()
        agent.stats["pid"] = self._pids.get(new_work_id, 0)
        self.save()
        self._emit("agent.spawned", {
            "agent_id": agent.id,
            "role": agent.role,
            "project_id": agent.project_id,
            "task_id": agent.task_id,
            "unit_id": new_work_id,
            "restarted": True,
            "attempt": int(attempt),
        })
        return True

    async def kill(self, agent_id: str, *, reason: str = "killed") -> bool:
        """Stop one agent's process and mark it dead."""
        agent = self._agents.get(agent_id)
        if agent is None or agent.status in TERMINAL_AGENT_STATUSES:
            return False
        if agent.status == QUEUED_AGENT_STATUS:
            # A queued agent has no process; killing it just leaves the queue.
            entry = self.queue.remove_agent(agent.id)
            agent.status = "dead"
            agent.summary = f"killed while queued: {reason}"
            agent.touch()
            self.save()
            if entry is not None:
                self._emit("agent.dequeued", {
                    "queue_id": entry.id,
                    "agent_id": agent.id,
                    "role": agent.role,
                    "project_id": agent.project_id,
                    "task_id": agent.task_id,
                    "priority": entry.priority,
                    "canceled": True,
                    "reason": reason,
                })
            self._emit("agent.terminated", {
                "agent_id": agent.id, "role": agent.role,
                "project_id": agent.project_id, "task_id": agent.task_id,
                "reason": reason, "queued": True,
            })
            self._schedule_drain()
            return True
        work_id = agent.unit_id
        handled = False
        if self.control is not None and work_id:
            try:
                await _maybe_await(self.control(
                    work_id, "unit.kill", {"reason": reason}))
                handled = True
            except Exception:
                self.log.warning("control kill failed for %s", work_id,
                                 exc_info=True)
        if not handled and work_id:
            await self._terminate(work_id)
            self._release(work_id)
        if work_id:
            self._close_log(work_id)
        agent.status = "dead"
        agent.summary = f"killed: {reason}"
        agent.touch()
        self.save()
        self._emit("agent.terminated", {
            "agent_id": agent.id,
            "role": agent.role,
            "project_id": agent.project_id,
            "task_id": agent.task_id,
            "reason": reason,
        })
        self._schedule_drain()
        return True

    async def kill_project(self, project_id: str, *,
                           reason: str = "project_archived") -> int:
        """Kill every non-terminal agent of a project; return the count."""
        victims = [agent for agent in self._agents.values()
                   if agent.project_id == project_id
                   and agent.status not in TERMINAL_AGENT_STATUSES]
        killed = 0
        for agent in victims:
            if await self.kill(agent.id, reason=reason):
                killed += 1
        return killed

    async def archive(self, project_id: str) -> int:
        """Alias used when a project is archived (architecture §8.1)."""
        return await self.kill_project(project_id, reason="project_archived")

    # ------------------------------------------------------------------
    # live config / timeout reaping
    # ------------------------------------------------------------------
    async def on_config(self, config: Any) -> dict:
        """Apply a live config patch: limits, queue bound and worker push.

        Called by ``App.update_config`` with the *full* Config.  Every running
        worker receives ``unit.configure`` with the non-secret runtime view
        (limits + provider/model/base_url); API keys are never pushed.  A
        worker that does not implement the action is recorded and skipped --
        the persisted config stays the source of truth.
        """
        agents_cfg = getattr(config, "agents", config)
        self.config = agents_cfg
        self.max_workers = max(1, int(getattr(agents_cfg, "max_workers",
                                             self.max_workers)))
        self.queue_max = max(1, int(getattr(agents_cfg, "queue_max",
                                            self.queue_max)))
        self.queue.set_max_size(self.queue_max)
        self.task_timeout_s = float(getattr(agents_cfg, "task_timeout_s",
                                            self.task_timeout_s))
        self.max_tool_calls_per_task = int(getattr(
            agents_cfg, "max_tool_calls_per_task", self.max_tool_calls_per_task))
        self.config_reload_s = float(getattr(agents_cfg, "config_reload_s",
                                             self.config_reload_s))
        payload = public_runtime_config(config)
        self.last_pushed_config = payload
        pushed: list[dict] = []
        failed: list[dict] = []
        for agent in list(self._agents.values()):
            if (agent.status not in RUNNING_AGENT_STATUSES
                    or not agent.unit_id or self.control is None):
                continue
            try:
                result = await _maybe_await(self.control(
                    agent.unit_id, "unit.configure", dict(payload)))
            except Exception as exc:  # worker may be older / unreachable
                failed.append({"agent_id": agent.id, "unit_id": agent.unit_id,
                               "error": f"{type(exc).__name__}: {exc}"})
                self.log.warning("configure push to %s failed: %s",
                                 agent.unit_id, exc)
                continue
            pushed.append({"agent_id": agent.id, "unit_id": agent.unit_id,
                           "result": result})
            self._emit("agent.configured", {
                "agent_id": agent.id,
                "unit_id": agent.unit_id,
                "role": agent.role,
                "project_id": agent.project_id,
                "task_id": agent.task_id,
            })
        # A higher max_workers or a smaller queue may free slots immediately.
        self._schedule_drain()
        return {"pushed": len(pushed), "failed": len(failed),
                "agents": pushed, "errors": failed, "config": payload}

    def _ensure_monitor(self) -> None:
        """Start the timeout watchdog once (idempotent, loop-safe)."""
        if self._closing or self.task_timeout_s <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - sync call
            return
        if self._monitor_task is not None and not self._monitor_task.done():
            return
        self._monitor_task = loop.create_task(self._monitor_loop())
        self._monitor_task.add_done_callback(self._monitor_done)

    def _monitor_done(self, task: asyncio.Task) -> None:
        if self._monitor_task is task:
            self._monitor_task = None

    async def _monitor_loop(self) -> None:
        while not self._closing:
            timeout = float(self.task_timeout_s)
            interval = 5.0 if timeout <= 0 else max(
                0.25, min(5.0, timeout / 4.0))
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                return
            if self._closing:
                return
            try:
                await self.reap_stalled()
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                return
            except Exception:  # pragma: no cover - watchdog must survive
                self.log.exception("timeout reaper failed")

    async def reap_stalled(self, *, now: int | None = None) -> list[dict]:
        """Reclaim task agents that blew ``agents.task_timeout_s``.

        ``last_seen`` is refreshed by the worker heartbeat; a worker that is
        wedged (tool call/LLM hang, no heartbeat) is killed, its unit released,
        the task moved back to ``todo`` when it is still ``doing`` and a
        replacement spawn queued.  Runner-reported timeouts are handled by the
        runner itself (agent.finish + task.blocked), so this is the safety net
        for a process that cannot report.
        """
        timeout = float(self.task_timeout_s)
        if timeout <= 0:
            return []
        current = int(now if now is not None else now_ms())
        reaped: list[dict] = []
        for agent in list(self._agents.values()):
            if agent.status not in RUNNING_AGENT_STATUSES:
                continue
            if not agent.task_id:
                continue
            last = int(agent.last_seen or agent.created_at or current)
            if current - last <= timeout * 1000.0:
                continue
            info: dict[str, Any] = {
                "agent_id": agent.id,
                "role": agent.role,
                "project_id": agent.project_id,
                "task_id": agent.task_id,
                "unit_id": agent.unit_id,
                "last_seen": last,
                "timeout_s": timeout,
            }
            self._emit("agent.timeout", info)
            work_id = agent.unit_id
            if work_id and self.control is not None:
                try:
                    await _maybe_await(self.control(
                        work_id, "unit.kill", {"reason": "task_timeout"}))
                except Exception:
                    self.log.warning("timeout kill via control failed for %s",
                                     work_id, exc_info=True)
            if work_id:
                await self._terminate(work_id)
                self._release(work_id)
                self._close_log(work_id)
            # A timeout is a budget/precondition stop, not an execution bug:
            # keep it distinct from "failed" so a human raises the limit
            # instead of hunting for a crash.
            agent.status = "blocked"
            agent.error = f"task timed out after {timeout:g}s"
            agent.error_code = "TASK_TIMEOUT"
            agent.summary = agent.error
            agent.touch()
            self._emit("agent.blocked", {
                "agent_id": agent.id, "role": agent.role,
                "project_id": agent.project_id, "task_id": agent.task_id,
                "summary": agent.error, "error": agent.error,
                "status": "blocked", "error_code": "TASK_TIMEOUT",
                "timeout": True,
            })
            task = None
            if self.task_lookup is not None:
                try:
                    task = self.task_lookup(agent.task_id)
                except Exception:
                    task = None
            status = _unit_field(task, "status")
            if task is not None and status not in ("doing", "todo"):
                # Blocked/done/review: do not fight the worker's own report.
                info["reassigned"] = False
            else:
                if task is not None and status == "doing" and self.task_requeue:
                    try:
                        result = self.task_requeue(agent.task_id)
                        if inspect.isawaitable(result):
                            await result
                        info["task_requeued"] = True
                    except Exception as exc:
                        info["task_requeue_error"] = str(exc)
                try:
                    replacement = self._enqueue(
                        agent.role, agent.project_id, agent.task_id,
                        reason="timeout_retry",
                        priority=int(agent.stats.get("priority", 50)))
                    info["replacement_agent_id"] = replacement.id
                    info["reassigned"] = True
                except Conflict as exc:
                    info["reassigned"] = False
                    info["requeue_error"] = exc.message
            reaped.append(info)
        if reaped:
            self.save()
            self._schedule_drain()
        return reaped

    async def close(self) -> None:
        """Stop the watchdog/drain helpers (hub shutdown)."""
        self._closing = True
        for task in (self._drain_task, self._monitor_task):
            if task is not None and not task.done():
                task.cancel()
        self._drain_task = None
        self._monitor_task = None

    # ------------------------------------------------------------------
    # roster (sync)
    # ------------------------------------------------------------------
    def register(self, *, role: str, project_id: str | None, unit_id: str,
                 task_id: str | None = None, model: str = "",
                 status: str = "spawning") -> Agent:
        """Create (or refresh) the roster entry for a unit.

        ``unit_id`` may be empty only for a queued agent: it has no process and
        gets its unit id when :meth:`drain_queue` starts it.
        """
        if not unit_id and status != QUEUED_AGENT_STATUS:
            raise BadRequest("unit_id is required to register an agent")
        for existing in self._agents.values():
            if unit_id and existing.unit_id == unit_id:
                existing.role = role
                existing.project_id = project_id
                existing.task_id = task_id or existing.task_id
                existing.model = model or existing.model
                existing.touch()
                self.save()
                return existing
        n = int(self._counters.get(role, 0)) + 1
        self._counters[role] = n
        agent = Agent(
            id=f"agent-{role}-{n}",
            role=role,
            project_id=project_id,
            task_id=task_id,
            unit_id=unit_id or None,
            status=status,
            model=model or "",
            display_name=display_name_for(role, n),
        )
        self._agents[agent.id] = agent
        self.save()
        return agent

    def update(self, agent_id: str, patch: dict) -> Agent:
        """Apply a whitelisted patch to one agent and persist the roster."""
        agent = self.get(agent_id)
        if not isinstance(patch, dict):
            raise BadRequest("agent patch must be an object")
        allowed = {"status", "model", "display_name", "task_id", "project_id",
                   "summary", "error", "error_code", "stats", "last_seen"}
        unknown = set(patch) - allowed
        if unknown:
            raise BadRequest(f"unknown agent fields: {sorted(unknown)}")
        status = patch.get("status")
        if status is not None and status not in AGENT_STATUSES:
            raise BadRequest(f"invalid agent status: {status!r}")
        for key, value in patch.items():
            if key == "stats":
                if not isinstance(value, dict):
                    raise BadRequest("agent stats must be an object")
                agent.stats.update(value)
            elif key == "last_seen":
                agent.last_seen = int(value or now_ms())
            else:
                setattr(agent, key, value)
        if "last_seen" not in patch:
            agent.touch()
        self.save()
        if agent.status in TERMINAL_AGENT_STATUSES:
            self._schedule_drain()
        return agent

    def finish(self, agent_id: str, summary: str, *,
               status: str = "done", error: str | None = None,
               error_code: str | None = None) -> Agent:
        """Record a terminal-ish result on the roster.

        ``blocked`` and ``failed`` are deliberately distinct: ``blocked`` means a
        precondition is missing (scope/workspace/task/limit) and ``failed``
        means execution actually raised.  Both must carry a non-empty
        ``error`` so the UI and the next agent can explain the stop; a status
        without a reason would reproduce the "failed with an empty error"
        diagnosis hole.
        """
        if status not in AGENT_STATUSES:
            raise BadRequest(f"invalid agent status: {status!r}")
        agent = self.get(agent_id)
        agent.status = status
        agent.summary = str(summary or "")
        agent.error_code = (str(error_code) if error_code else None)
        if status in ("blocked", "failed", "dead"):
            fallback = (f"agent stopped as {status} without a reason; "
                        "inspect the event log for the last step")
            agent.error = str(error or summary or fallback)
        else:
            agent.error = None
        agent.touch()
        self.save()
        event_type = {"done": "agent.finished", "blocked": "agent.blocked",
                      "failed": "agent.failed",
                      "dead": "agent.terminated"}.get(status, "agent.finished")
        self._emit(event_type, {
            "agent_id": agent.id,
            "role": agent.role,
            "project_id": agent.project_id,
            "task_id": agent.task_id,
            "summary": agent.summary,
            "status": agent.status,
            "error": agent.error,
            "error_code": agent.error_code,
        })
        if agent.status in TERMINAL_AGENT_STATUSES:
            self._schedule_drain()
        return agent

    def list(self, *, project_id: str | None = None,
             status: str | None = None) -> list[Agent]:
        """List agents, optionally filtered by project/status."""
        agents = list(self._agents.values())
        if project_id is not None:
            agents = [agent for agent in agents if agent.project_id == project_id]
        if status is not None:
            agents = [agent for agent in agents if agent.status == status]
        return agents

    def prune_finished(self, *, keep_aggregators: int = 5) -> int:
        """Drop old finished aggregator records; events keep the full history.

        Aggregators are periodic, so without this the roster (and the project
        card avatars) grows without bound even when the project is idle.
        """
        finished = [agent for agent in self._agents.values()
                    if agent.role == "aggregator" and agent.is_terminal]
        by_project: dict[str | None, list[Agent]] = {}
        for agent in finished:
            by_project.setdefault(agent.project_id, []).append(agent)
        remove: set[str] = set()
        for agents in by_project.values():
            agents.sort(key=lambda item: item.last_seen, reverse=True)
            remove.update(agent.id for agent in agents[int(keep_aggregators):])
        if not remove:
            return 0
        for agent_id in remove:
            self._agents.pop(agent_id, None)
        self.save()
        return len(remove)

    def get(self, agent_id: str) -> Agent:
        """Return one agent or raise :class:`NotFound`."""
        agent = self._agents.get(agent_id)
        if agent is None:
            raise NotFound(f"agent not found: {agent_id}")
        return agent

    def stats(self) -> dict:
        """Aggregate counter used by the status bar."""
        by_status: dict[str, int] = {}
        by_role: dict[str, int] = {}
        for agent in self._agents.values():
            by_status[agent.status] = by_status.get(agent.status, 0) + 1
            by_role[agent.role] = by_role.get(agent.role, 0) + 1
        running = sum(1 for agent in self._agents.values()
                      if agent.status in RUNNING_AGENT_STATUSES)
        return {
            "total": len(self._agents),
            "active": sum(1 for agent in self._agents.values()
                          if agent.status not in TERMINAL_AGENT_STATUSES),
            "running": running,
            "queued": len(self.queue),
            "queue_max": self.queue.max_size,
            "by_status": by_status,
            "by_role": by_role,
            "max_workers": self.max_workers,
        }

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def save(self) -> None:
        """Persist the roster snapshot (best effort; never raises)."""
        payload = {
            "agents": [agent.to_dict() for agent in self._agents.values()],
            "counters": dict(self._counters),
            "queue": self.queue.to_payload(),
            "limits": {
                "max_workers": self.max_workers,
                "queue_max": self.queue.max_size,
                "task_timeout_s": self.task_timeout_s,
                "max_tool_calls_per_task": self.max_tool_calls_per_task,
                "config_reload_s": self.config_reload_s,
            },
        }
        try:
            write_json(self.state_path, payload)
        except (OSError, ValueError):
            self.log.warning("cannot save agent roster to %s", self.state_path,
                             exc_info=True)

    def load(self) -> None:
        """Restore the roster snapshot (best effort; never raises)."""
        try:
            payload = read_json(self.state_path, default=None)
        except (OSError, ValueError):
            self.log.warning("cannot read agent roster %s", self.state_path,
                             exc_info=True)
            return
        if not isinstance(payload, dict):
            return
        agents: dict[str, Agent] = {}
        for entry in payload.get("agents") or []:
            if not isinstance(entry, dict):
                continue
            try:
                agent = Agent.from_dict(entry)
            except (TypeError, ValueError):
                continue
            agents[agent.id] = agent
        counters = payload.get("counters")
        if isinstance(counters, dict):
            for role, value in counters.items():
                try:
                    self._counters[str(role)] = int(value)
                except (TypeError, ValueError):
                    continue
        self._agents = agents
        self.queue.load(payload.get("queue"))
        # A queued roster entry without a queue row would never start: drop it
        # back to a failed state instead of leaving a ghost that occupies a
        # roster slot forever.
        for agent in list(self._agents.values()):
            if agent.status != QUEUED_AGENT_STATUS:
                continue
            if any(entry.agent_id == agent.id for entry in self.queue.entries()):
                continue
            agent.status = "failed"
            agent.error = "queued agent lost its queue entry (snapshot)"
            agent.touch()

    # ------------------------------------------------------------------
    # process / event plumbing
    # ------------------------------------------------------------------
    def _open_log(self, work_id: str) -> Any:
        path = self.logs_dir / f"{work_id}.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(path, "ab", buffering=0)
        except OSError:  # pragma: no cover - read-only state dir
            self.log.warning("cannot open worker log %s", path)
            return subprocess.DEVNULL
        self._log_handles[work_id] = handle
        return handle

    def _close_log(self, work_id: str) -> None:
        handle = self._log_handles.pop(work_id, None)
        if handle is None or handle is subprocess.DEVNULL:
            return
        try:
            handle.close()
        except OSError:  # pragma: no cover
            pass

    async def _terminate(self, work_id: str) -> bool:
        """Stop a worker's process group with bounded TERM→KILL escalation.

        Workers are spawned with ``start_new_session=True``, so the group is
        signalled first: a TERM-ignoring child cannot keep the unit (or its
        pipes) alive.  Both waits are bounded and poll asynchronously, so a
        stubborn child cannot block the event loop for long.  The pid/proc
        bookkeeping and the log handle are released on every path.
        """
        proc = self._procs.pop(work_id, None)
        pid = self._pids.pop(work_id, None)
        try:
            if proc is None or _process_exited(proc):
                return True
            pgid = _group_id(pid or int(getattr(proc, "pid", 0) or 0))
            _signal_process(proc, pgid, kill=False)
            if await _wait_gone(proc, pgid, self.terminate_grace_s):
                return True
            self.log.warning(
                "worker %s ignored SIGTERM after %ss; sending SIGKILL to %s",
                work_id, self.terminate_grace_s, pgid or pid or "-")
            _signal_process(proc, pgid, kill=True)
            if await _wait_gone(proc, pgid, self.kill_grace_s):
                return True
            self.log.warning(
                "worker %s kill_failed: survived SIGKILL after %ss; "
                "dropping pid %s", work_id, self.kill_grace_s, pid or "-")
            return False
        finally:
            # Never leak the pid slot or the log fd, even when the child is
            # unkillable (kernel D state) or an exception escaped.
            self._pids.pop(work_id, None)
            self._close_log(work_id)

    def _release(self, work_id: str | None) -> None:
        if not work_id:
            return
        self._revoke_token(work_id)
        try:
            self.allocator.release(work_id)
        except Exception:  # pragma: no cover - release is best effort
            self.log.debug("release %s failed", work_id, exc_info=True)
        self._addresses.pop(work_id, None)

    def _emit(self, type: str, data: dict) -> None:
        if self.eventlog is None:
            return
        source = "sys"
        project_id = data.get("project_id") if isinstance(data, dict) else None
        attempts = (
            lambda: self.eventlog.append(type, data, source=source,
                                         project_id=project_id),
            lambda: self.eventlog.append(type, data, source=source),
            lambda: self.eventlog.append(
                {"type": type, "data": data, "source": source}),
        )
        for attempt in attempts:
            try:
                result = attempt()
            except TypeError:
                continue
            except Exception:  # pragma: no cover - audit is best effort
                self.log.debug("cannot append event %s", type, exc_info=True)
                return
            if inspect.isawaitable(result):
                try:
                    asyncio.get_running_loop().create_task(result)
                except RuntimeError:  # pragma: no cover
                    pass
            return


__all__ = ["AgentSupervisor"]

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
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..errors import BadRequest, Conflict, NotFound, Unavailable
from ..util.jsonio import read_json, write_json
from ..util.logging import get_logger
from .models import (AGENT_STATUSES, TERMINAL_AGENT_STATUSES, Agent,
                     display_name_for, now_ms)
from .roles import get_role

ControlFn = Callable[[str, str, dict], Awaitable[Any]]


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
                 control: ControlFn | None = None) -> None:
        self.ctx = ctx
        self.registry = registry
        self.allocator = allocator
        self.eventlog = eventlog
        self.config = getattr(config, "agents", config)
        self.max_workers = int(getattr(self.config, "max_workers", 6))
        self.sys_rpc = str(sys_rpc)
        self.state_dir = Path(state_dir)
        self.root = Path(root)
        self.spawn_timeout_s = float(spawn_timeout_s)
        self.log = logger or get_logger("agents.supervisor")
        self.control = control
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

    # ------------------------------------------------------------------
    # spawn
    # ------------------------------------------------------------------
    async def spawn(self, role: str, project_id: str | None = None, *,
                    task_id: str | None = None, reason: str = "") -> Agent:
        """Allocate a unit, start a worker process and wait until online."""
        get_role(role)  # validate role before allocating anything
        active = [agent for agent in self._agents.values()
                  if agent.status not in TERMINAL_AGENT_STATUSES]
        if len(active) >= self.max_workers:
            raise Conflict(
                f"agent capacity reached: {len(active)}/{self.max_workers}",
                detail={"max_workers": self.max_workers})

        work_id = str(self.allocator.new_work_id(role))
        try:
            addresses = await _maybe_await(self.allocator.allocate(work_id))
        except Exception as exc:
            raise Unavailable(
                f"cannot allocate unit {work_id}: {exc}") from exc
        self._addresses[work_id] = addresses

        agent = self.register(role=role, project_id=project_id,
                              unit_id=work_id, task_id=task_id)
        try:
            await self._launch(agent, work_id)
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

        if agent.status == "spawning":
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
        })
        self.save()
        return agent

    async def _launch(self, agent: Agent, work_id: str) -> None:
        """Start the worker process and wait for ``unit.register``."""
        command = self._command(agent.role, agent.project_id, agent.task_id,
                                work_id)
        handle = self._open_log(work_id)
        env = os.environ.copy()
        env["SFO_ROOT"] = str(self.root)
        env["SFO_STATE_DIR"] = str(self.state_dir)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (str(self.root) + (os.pathsep + existing
                                               if existing else ""))
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
            self._terminate(work_id)
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
        self._terminate(old_work_id)
        self._close_log(old_work_id)

        new_work_id = str(self.allocator.new_work_id(agent.role))
        try:
            addresses = await _maybe_await(self.allocator.allocate(new_work_id))
            self._addresses[new_work_id] = addresses
            await self._launch(agent, new_work_id)
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

        agent.unit_id = new_work_id
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
            self._terminate(work_id)
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
    # roster (sync)
    # ------------------------------------------------------------------
    def register(self, *, role: str, project_id: str | None, unit_id: str,
                 task_id: str | None = None, model: str = "") -> Agent:
        """Create (or refresh) the roster entry for a unit."""
        if not unit_id:
            raise BadRequest("unit_id is required to register an agent")
        for existing in self._agents.values():
            if existing.unit_id == unit_id:
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
            unit_id=unit_id,
            status="spawning",
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
                   "summary", "error", "stats", "last_seen"}
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
        return agent

    def finish(self, agent_id: str, summary: str, *,
               status: str = "done") -> Agent:
        """Record a terminal-ish result on the roster."""
        if status not in AGENT_STATUSES:
            raise BadRequest(f"invalid agent status: {status!r}")
        agent = self.get(agent_id)
        agent.status = status
        agent.summary = str(summary or "")
        agent.touch()
        self.save()
        event_type = "agent.finished" if status == "done" else "agent.failed"
        self._emit(event_type, {
            "agent_id": agent.id,
            "role": agent.role,
            "project_id": agent.project_id,
            "task_id": agent.task_id,
            "summary": agent.summary,
        })
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
        return {
            "total": len(self._agents),
            "active": sum(1 for agent in self._agents.values()
                          if agent.status not in TERMINAL_AGENT_STATUSES),
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

    def _terminate(self, work_id: str) -> None:
        proc = self._procs.pop(work_id, None)
        if proc is None:
            return
        try:
            terminate = getattr(proc, "terminate", None)
            if callable(terminate):
                terminate()
        except Exception:  # pragma: no cover - process may already be gone
            self.log.debug("terminate %s failed", work_id, exc_info=True)
        self._pids.pop(work_id, None)

    def _release(self, work_id: str | None) -> None:
        if not work_id:
            return
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

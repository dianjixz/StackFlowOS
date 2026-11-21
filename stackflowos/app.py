"""The StackFlowOS runtime: wires kernel, transport, L2, security, projects,
agents and the conversation surface into one process (the sys unit).

Design rule followed here: this module only *composes*; every concept lives in
its own module. Handlers are thin translations between the wire protocol and
those services, and privileged actions (config, rules, approval decisions) are
never callable by worker units over RPC.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import platform
import shutil
import socket
import time
from pathlib import Path
from typing import Any, Callable

from .agents import AgentSupervisor, ToolExecutor
from .ai import make_provider
from .config import Config
from .errors import (BadRequest, Conflict, Denied, NotFound, SfoError,
                     Unavailable, ValidationError)
from .kernel import Context
from .l2 import EventLog, FailureManager, FailurePolicy, LeaseManager, Scheduler, UnitInfo, UnitRegistry
from .projects import ProjectStore, plan_for
from .security import ApprovalBroker, PolicyEngine
from .session.chat import ChatService
from .transport import AddressAllocator, HubTransport, socket_dir_for
from .util.jsonio import read_json, write_json
from .util.logging import get_logger, setup_logging

VERSION = "0.1.0"
PRIVILEGED_ACTIONS = {"sys.config.set", "rule.create", "rule.update", "rule.delete",
                      "approval.decide"}


def now_ms() -> int:
    return int(time.time() * 1000)


class MemoryStore:
    """Per-project key/value memory for agents (small, JSON persisted)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        data = read_json(self.path, default={}) or {}
        self.data: dict[str, dict] = data if isinstance(data, dict) else {}

    def get(self, project_id: str, key: str) -> Any:
        return (self.data.get(project_id) or {}).get(key)

    def set(self, project_id: str, key: str, value: Any) -> Any:
        self.data.setdefault(project_id, {})[key] = value
        write_json(self.path, self.data)
        return value

    def all(self, project_id: str) -> dict:
        return dict(self.data.get(project_id) or {})


class App:
    """Composition root for the hub process."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.paths = config.paths
        self.started_at = now_ms()
        self.version = VERSION
        self._actions: dict[str, Callable[[dict, str], Any]] = {}
        self._rpc_inflight: dict[str, asyncio.Task] = {}
        self._rpc_cache: dict[str, tuple[float, dict]] = {}
        self._bg_tasks: set[asyncio.Task] = set()
        self._aggregator_task: asyncio.Task | None = None
        self._maintenance_task: asyncio.Task | None = None
        self._stopped = False
        self._lock_fd: int | None = None
        self._assistant_executor: ToolExecutor | None = None
        self._assistant_agent = None
        self.log = get_logger("app")

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, config: Config | None = None, *, root: Path | None = None,
                    state_dir: Path | None = None) -> "App":
        return cls(config or Config.load(root=root, state_dir=state_dir))

    async def start(self) -> "App":
        cfg = self.config
        setup_logging("INFO", self.paths.log_file)
        log = self.log
        log.info("booting %s v%s (transport=%s state=%s)",
                 cfg.system.name, self.version, cfg.transport.mode, self.paths.state_dir)

        self.ctx = Context(name="root")
        self._acquire_hub_lock()
        self.eventlog = EventLog(self.paths.events_dir, logger=get_logger("events"))
        self.allocator = AddressAllocator(
            mode=cfg.transport.mode,
            host=cfg.transport.host,
            run_dir=socket_dir_for(self.paths.run_dir, cfg.transport.mode, log),
            port_pool=(cfg.transport.port_pool_start, cfg.transport.port_pool_end),
            advertise_host=cfg.transport.advertise_host,
            logger=get_logger("address"),
        )
        self.sys_addresses = self.allocator.ensure_sys()
        self.transport = HubTransport(sys_addresses=self.sys_addresses,
                                      timeout=cfg.transport.rpc_timeout_s,
                                      logger=get_logger("transport"))
        self.transport.on_event(self._on_unit_event)

        self.registry = UnitRegistry(self.ctx, self.eventlog,
                                     offline_after_s=cfg.agents.offline_after_s,
                                     logger=get_logger("registry"))
        self.leases = LeaseManager(self.ctx, self.eventlog)
        self.projects = ProjectStore(
            self.ctx,
            state_path=self.paths.state_dir / "state" / "projects.json",
            counter_path=self.paths.state_dir / "state" / "counters.json",
            events=self.eventlog,
            workspace_root=self.paths.projects_dir,
        )
        self.policy = PolicyEngine(
            self.ctx, self.eventlog,
            rules_path=self.paths.state_dir / "state" / "rules.json",
            config=cfg.security,
            projects_root=self.paths.projects_dir,
        )
        self.approvals = ApprovalBroker(
            self.ctx, self.policy, self.eventlog,
            approvals_path=self.paths.state_dir / "state" / "approvals.json",
            approval_ttl_s=cfg.security.approval_ttl_s,
            grant_ttl_s=cfg.security.grant_ttl_s,
        )
        self.memory = MemoryStore(self.paths.state_dir / "state" / "memory.json")
        self.scheduler = Scheduler(self.registry)
        if hasattr(self.scheduler, "bind_leases"):
            self.scheduler.bind_leases(self.leases)
        self.supervisor = AgentSupervisor(
            self.ctx,
            registry=self.registry,
            allocator=self.allocator,
            eventlog=self.eventlog,
            config=cfg.agents,
            sys_rpc=self.sys_addresses.rpc,
            state_dir=self.paths.state_dir,
            root=self.paths.root,
            control=self._control_unit,
            logger=get_logger("agents"),
        )
        self.supervisor.load()
        self.failure = FailureManager(
            self.ctx, self.registry, self.leases, self.eventlog,
            policies={
                "lead": FailurePolicy("lead", "restart", 3, 300.0, 5),
                "aggregator": FailurePolicy("aggregator", "restart", 3, 300.0, 5),
                "assistant": FailurePolicy("assistant", "ignore"),
                "coder": FailurePolicy("coder", "restart", 2, 180.0, 5),
                "architect": FailurePolicy("architect", "restart", 2, 180.0, 5),
                "tester": FailurePolicy("tester", "restart", 2, 180.0, 5),
                "reviewer": FailurePolicy("reviewer", "restart", 2, 180.0, 5),
                "researcher": FailurePolicy("researcher", "restart", 2, 180.0, 5),
            },
            restart_cb=self.supervisor.restart,
            kill_cb=self._kill_unit_supervisor,
            tick_s=2.0,
        )

        self._register_actions()
        await self.transport.start()
        self.ctx.on("unit.offline", self._on_unit_offline)
        await self.failure.start()
        self.unit_lease_ttl_s = max(30, int(cfg.agents.offline_after_s * 3))
        self._start_aggregation_loop(cfg.agents.aggregator_interval_s)
        self._start_maintenance_loop()

        self.provider = make_provider(cfg.llm)
        self.chat = ChatService(
            self.ctx, self.eventlog, app=self,
            provider=self.provider,
            state_path=self.paths.state_dir / "state" / "chat.json",
            logger=get_logger("chat"),
        )
        await self.chat.start()

        self._cancel_orphan_approvals()
        self.eventlog.append("sys.booted", {
            "version": self.version,
            "transport": cfg.transport.mode,
            "host": cfg.system.host_label,
            "web_port": cfg.web.port,
            "sys_rpc": self.sys_addresses.rpc,
            "sys_events": self.sys_addresses.events,
        }, source="sys")
        self.eventlog.append("sys.ready", {"units": 0}, source="sys")
        log.info("hub ready: rpc=%s events=%s web=%s",
                 self.sys_addresses.rpc, self.sys_addresses.events, cfg.web.port)
        return self

    def _start_aggregation_loop(self, interval_s: float) -> None:
        """Periodically run one aggregation agent per active project."""
        if interval_s <= 0:
            return

        async def loop() -> None:
            while not self._stopped:
                await asyncio.sleep(max(30.0, float(interval_s)))
                if self._stopped:
                    return
                try:
                    for project in self.projects.list():
                        if project.status not in ("active", "review", "degraded"):
                            continue
                        if self.projects.task_list(project_id=project.id) == []:
                            continue
                        running = [a for a in self.supervisor.list(project_id=project.id)
                                   if a.role == "aggregator"
                                   and a.status not in ("done", "failed", "dead")]
                        if running:
                            continue
                        try:
                            await self.supervisor.spawn(
                                "aggregator", project.id,
                                reason="scheduled_summary")
                        except SfoError:
                            self.log.debug("scheduled aggregator skipped for %s",
                                           project.id)
                except Exception:
                    self.log.exception("aggregation loop iteration failed")

        self._aggregator_task = asyncio.create_task(loop(), name="aggregation-loop")

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.log.info("shutting down")
        if self._aggregator_task is not None:
            self._aggregator_task.cancel()
            try:
                await self._aggregator_task
            except (asyncio.CancelledError, Exception):
                pass
            self._aggregator_task = None
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except (asyncio.CancelledError, Exception):
                pass
            self._maintenance_task = None
        for task in list(self._bg_tasks):
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        try:
            for agent in self.supervisor.list():
                if agent.status not in ("done", "failed", "dead"):
                    await self.supervisor.kill(agent.id, reason="hub_shutdown")
        except Exception:
            self.log.exception("failed to stop agents")
        try:
            await self.failure.stop()
        except Exception:
            self.log.exception("failure manager stop failed")
        try:
            await self.chat.stop()
        except Exception:
            self.log.exception("chat stop failed")
        try:
            self.eventlog.append("sys.shutdown", {}, source="sys")
        except Exception:
            pass
        try:
            await self.transport.stop()
        except Exception:
            self.log.exception("transport stop failed")
        try:
            self.allocator.cleanup()
        except Exception:
            self.log.exception("allocator cleanup failed")
        try:
            result = self.eventlog.close()
            if hasattr(result, "__await__"):
                await result
        except Exception:
            pass
        try:
            await self.ctx.dispose()
        except Exception:
            self.log.exception("context dispose failed")
        self._release_hub_lock()

    # ------------------------------------------------------------------
    # single-hub lock
    # ------------------------------------------------------------------
    def _acquire_hub_lock(self) -> None:
        """Prevent two hubs from sharing one state directory.

        Overlapping hubs would unlink each other's IPC sockets on shutdown
        (the old process cleans up paths that a new process already rebound).
        A POSIX flock is released by the kernel when the process dies, so a
        stale lock file is harmless.
        """
        lock_path = self.paths.run_dir / "hub.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            raise Unavailable(f"cannot open hub lock {lock_path}: {exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise Unavailable(
                f"another StackFlowOS hub is using {self.paths.state_dir}") from exc
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        self._lock_fd = fd

    def _release_hub_lock(self) -> None:
        fd, self._lock_fd = self._lock_fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # RPC dispatch
    # ------------------------------------------------------------------
    def _register_actions(self) -> None:
        self._actions = {
            "sys.ping": self._sys_ping,
            "sys.info": self._sys_info,
            "sys.hwinfo": self._sys_hwinfo,
            "sys.config.get": self._sys_config_get,
            "sys.config.set": self._sys_config_set,
            "sys.stats": self._sys_stats,
            "unit.allocate": self._unit_allocate,
            "unit.addresses": self._unit_addresses,
            "unit.register": self._unit_register,
            "unit.heartbeat": self._unit_heartbeat,
            "unit.release": self._unit_release,
            "unit.list": self._unit_list,
            "unit.kill": self._unit_kill,
            "lease.acquire": self._lease_acquire,
            "lease.renew": self._lease_renew,
            "lease.release": self._lease_release,
            "lease.list": self._lease_list,
            "approval.request": self._approval_request,
            "approval.status": self._approval_status,
            "approval.use": self._approval_use,
            "approval.list": self._approval_list,
            "rule.list": self._rule_list,
            "rule.create": self._rule_create,
            "rule.update": self._rule_update,
            "rule.delete": self._rule_delete,
            "project.create": self._project_create,
            "project.list": self._project_list,
            "project.get": self._project_get,
            "project.update": self._project_update,
            "project.archive": self._project_archive,
            "project.delete": self._project_delete,
            "task.create": self._task_create,
            "task.list": self._task_list,
            "task.get": self._task_get,
            "task.claim": self._task_claim,
            "task.update": self._task_update,
            "task.artifact": self._task_artifact,
            "task.note": self._task_note,
            "task.check": self._task_check,
            "agent.register": self._agent_register,
            "agent.update": self._agent_update,
            "agent.finish": self._agent_finish,
            "agent.spawn": self._agent_spawn,
            "agent.kill": self._agent_kill,
            "agent.list": self._agent_list,
            "event.append": self._event_append,
            "event.since": self._event_since,
            "memory.get": self._memory_get,
            "memory.set": self._memory_set,
            "chat.send": self._chat_send,
        }
        for action, handler in self._actions.items():
            self.transport.register_action(action, self._make_env_handler(action, handler))

    RPC_CACHE_TTL_S = 300.0
    RPC_CACHE_MAX = 2000

    def _make_env_handler(self, action: str, handler):
        async def handle(env):
            # RPC retries reuse the envelope id. Non-idempotent actions
            # (agent.spawn, project.create, ...) must be executed once: join
            # the in-flight task of the first attempt and replay its result.
            key = f"{env.src}|{env.id}"
            cached = self._rpc_cache.get(key)
            now = time.time()
            if cached is not None and cached[0] > now - self.RPC_CACHE_TTL_S:
                return cached[1]
            task = self._rpc_inflight.get(key)
            if task is None:
                data = env.data if isinstance(env.data, dict) else {}
                task = asyncio.create_task(
                    self.handle_rpc_action(action, data, src=env.src))
                self._rpc_inflight[key] = task

                def _done(t: asyncio.Task, key: str = key) -> None:
                    self._rpc_inflight.pop(key, None)

                task.add_done_callback(_done)
            try:
                result = await task
            except BaseException:
                raise
            payload = result if isinstance(result, dict) else {"value": result}
            if len(self._rpc_cache) > self.RPC_CACHE_MAX:
                cutoff = now - self.RPC_CACHE_TTL_S
                self._rpc_cache = {k: v for k, v in self._rpc_cache.items()
                                   if v[0] > cutoff}
                if len(self._rpc_cache) > self.RPC_CACHE_MAX:
                    self._rpc_cache.clear()
            self._rpc_cache[key] = (now, payload)
            return payload
        return handle

    async def handle_rpc_action(self, action: str, data: dict, *, src: str = "sys.0") -> dict:
        handler = self._actions.get(action)
        if handler is None:
            raise BadRequest(f"unknown action: {action}")
        if action in PRIVILEGED_ACTIONS and src not in ("sys.0", "operator", "web"):
            raise Denied(f"{action} is a privileged action; ask the operator")
        return await handler(data or {}, src)

    async def _control_unit(self, work_id: str, action: str, data: dict | None = None):
        return await self.transport.call(work_id, action, data or {}, timeout=3.0)

    async def _kill_unit_supervisor(self, unit) -> None:
        agent = self._agent_for_unit(unit.work_id)
        if agent is not None:
            try:
                await self.supervisor.kill(agent.id, reason="failure_domain")
            except Exception:
                self.log.debug("failure-domain kill failed", exc_info=True)

    # ------------------------------------------------------------------
    # sys.*
    # ------------------------------------------------------------------
    async def _sys_ping(self, data, src):
        return {"ok": True, "ts": now_ms()}

    async def _sys_info(self, data, src):
        return {
            "name": self.config.system.name,
            "version": self.version,
            "host": self.config.system.host_label,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "uptime_s": int(time.time() - self.started_at / 1000),
            "transport": self.config.transport.mode,
            "sys_rpc": self.sys_addresses.rpc,
            "sys_events": self.sys_addresses.events,
            "web_port": self.config.web.port,
            "last_seq": self.eventlog.last_seq,
            "units": self.registry.stats(),
            "llm": {
                "provider": getattr(self.provider, "name", ""),
                "model": getattr(self.provider, "model", ""),
            },
        }

    async def _sys_hwinfo(self, data, src):
        return hwinfo()

    async def _sys_config_get(self, data, src):
        return self.config.public_dict()

    async def _sys_config_set(self, data, src):
        patch = data.get("patch") if isinstance(data.get("patch"), dict) else data
        result = await self.update_config(patch)
        return result

    async def _sys_stats(self, data, src):
        return self.stats()

    # ------------------------------------------------------------------
    # units
    # ------------------------------------------------------------------
    async def _unit_allocate(self, data, src):
        kind = str(data.get("kind") or "").strip()
        if not kind or not all(ch.isalnum() or ch in "-_" for ch in kind):
            raise BadRequest("unit kind must be alphanumeric")
        work_id = self.allocator.new_work_id(kind)
        addresses = self.allocator.allocate(work_id)
        return {
            "work_id": work_id,
            "rpc_address": addresses.rpc,
            "events_address": addresses.events,
            "sys_rpc_address": self.sys_addresses.rpc,
            "sys_events_address": self.sys_addresses.events,
            "transport": self.config.transport.mode,
        }

    async def _unit_addresses(self, data, src):
        work_id = str(data.get("work_id") or "")
        if not work_id:
            raise BadRequest("work_id required")
        addresses = self.allocator.addresses(work_id)
        if addresses is None:
            raise NotFound(f"no allocation for {work_id}")
        return {
            "work_id": work_id,
            "rpc_address": addresses.rpc,
            "events_address": addresses.events,
            "sys_rpc_address": self.sys_addresses.rpc,
            "sys_events_address": self.sys_addresses.events,
            "transport": self.config.transport.mode,
        }

    async def _unit_register(self, data, src):
        work_id = str(data.get("work_id") or "").strip()
        if not work_id:
            raise BadRequest("work_id required")
        kind = str(data.get("kind") or "worker")
        addresses = self.allocator.addresses(work_id)
        rpc_address = data.get("rpc_address") or (addresses.rpc if addresses else "")
        events_address = data.get("events_address") or (addresses.events if addresses else "")
        info = UnitInfo(
            work_id=work_id,
            kind=kind,
            label=str(data.get("label") or ""),
            project_id=data.get("project_id"),
            pid=int(data["pid"]) if data.get("pid") is not None else None,
            host=str(data.get("host") or self.config.system.host_label),
            transport=str(data.get("transport") or self.config.transport.mode),
            rpc_address=str(rpc_address or ""),
            events_address=str(events_address or ""),
            status="online",
            abilities=list(data.get("abilities") or []),
            hwinfo=dict(data.get("hwinfo") or {}),
            started_at=int(data.get("started_at") or now_ms()),
            last_seen=now_ms(),
            metadata=dict(data.get("metadata") or {}),
        )
        if not info.rpc_address:
            raise BadRequest("rpc_address required (allocate the unit first)")
        self.registry.register(info)
        self.transport.add_unit(info.work_id, info.rpc_address)
        if hasattr(self.supervisor, "note_online"):
            self.supervisor.note_online(info.work_id)
        # A unit holds a renewable lease on its own identity/address. This is
        # the process-level "I occupy this until T" claim that the failure
        # domain releases when the heartbeat stops.
        try:
            self.leases.acquire(f"unit:{info.work_id}", info.work_id,
                                self.unit_lease_ttl_s, renewable=True,
                                metadata={"kind": info.kind,
                                          "project_id": info.project_id})
        except Exception:
            self.log.debug("unit lease acquisition failed for %s", info.work_id,
                           exc_info=True)
        return {"work_id": info.work_id, "registered_at": info.last_seen}

    async def _unit_heartbeat(self, data, src):
        work_id = str(data.get("work_id") or src)
        unit = self.registry.heartbeat(work_id, stats=data.get("stats"))
        if unit is None:  # pragma: no cover - registry raises in practice
            raise NotFound(f"unit {work_id} not registered")
        try:
            active = self.leases.list(resource=f"unit:{work_id}", active_only=True)
            if active:
                self.leases.renew(active[0].id, work_id, self.unit_lease_ttl_s)
            else:
                self.leases.acquire(f"unit:{work_id}", work_id,
                                    self.unit_lease_ttl_s, renewable=True,
                                    metadata={"kind": unit.kind,
                                              "project_id": unit.project_id})
        except Exception:
            self.log.debug("unit lease renewal failed for %s", work_id,
                           exc_info=True)
        now = now_ms()
        for agent in self.supervisor.list():
            if agent.unit_id == work_id and agent.status not in ("dead", "done", "failed"):
                try:
                    self.supervisor.update(agent.id, {"last_seen": now})
                except Exception:
                    pass
                break
        return {"server_ts": now}

    async def _unit_release(self, data, src):
        work_id = str(data.get("work_id") or src)
        unit = self.registry.release(work_id, reason=str(data.get("reason") or "released"))
        if unit is not None:
            self.transport.remove_unit(work_id)
            self.leases.release_holder(work_id, reason="unit_released")
            self._mark_agent_dead(work_id, reason="unit_released")
            self._delay_release(work_id)
        return {"released": unit is not None}

    async def _unit_list(self, data, src):
        units = self.registry.list(
            kind=data.get("kind"),
            project_id=data.get("project_id"),
            status=data.get("status"),
        )
        return {"units": [u.to_dict() for u in units]}

    async def _unit_kill(self, data, src):
        work_id = str(data.get("work_id") or "")
        if not work_id:
            raise BadRequest("work_id required")
        agent = self._agent_for_unit(work_id)
        if agent is not None:
            ok = await self.supervisor.kill(agent.id, reason=str(data.get("reason") or "killed"))
            return {"accepted": ok}
        try:
            await self.transport.call(work_id, "unit.kill",
                                      {"reason": data.get("reason") or "killed"}, timeout=3.0)
            return {"accepted": True}
        except Exception:
            self.registry.mark_quarantined(work_id, "kill requested, unit unreachable")
            return {"accepted": False, "detail": "unit unreachable; marked quarantined"}

    def _delay_release(self, work_id: str) -> None:
        async def release_later() -> None:
            await asyncio.sleep(3.0)
            try:
                self.allocator.release(work_id)
            except Exception:
                self.log.debug("delayed allocator release failed for %s", work_id)

        task = asyncio.create_task(release_later())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _agent_for_unit(self, work_id: str):
        for agent in self.supervisor.list():
            if agent.unit_id == work_id:
                return agent
        return None

    def _mark_agent_dead(self, work_id: str, *, reason: str) -> None:
        agent = self._agent_for_unit(work_id)
        if agent is None or agent.status in ("done", "failed", "dead"):
            return
        try:
            self.supervisor.update(agent.id, {"status": "dead", "error": reason})
        except Exception:
            self.log.debug("could not mark agent dead", exc_info=True)

    # ------------------------------------------------------------------
    # leases
    # ------------------------------------------------------------------
    async def _lease_acquire(self, data, src):
        if not data.get("resource"):
            raise BadRequest("resource required")
        lease = self.leases.acquire(
            str(data["resource"]),
            str(data.get("holder") or src),
            int(data.get("ttl_s") or 300),
            renewable=bool(data.get("renewable", True)),
            preemptible=bool(data.get("preemptible", False)),
            priority=int(data.get("priority") or 0),
            metadata=dict(data.get("metadata") or {}),
        )
        return lease.to_dict()

    async def _lease_renew(self, data, src):
        lease = self.leases.renew(str(data.get("lease_id") or ""),
                                  str(data.get("holder") or src),
                                  int(data["ttl_s"]) if data.get("ttl_s") else None)
        return lease.to_dict()

    async def _lease_release(self, data, src):
        lease = self.leases.release(str(data.get("lease_id") or ""),
                                    holder=data.get("holder") or src,
                                    reason=str(data.get("reason") or "released"))
        return lease.to_dict()

    async def _lease_list(self, data, src):
        leases = self.leases.list(holder=data.get("holder"),
                                   resource=data.get("resource"),
                                   active_only=bool(data.get("active_only", True)))
        return {"leases": [lease.to_dict() for lease in leases]}

    # ------------------------------------------------------------------
    # approvals / rules
    # ------------------------------------------------------------------
    async def _approval_request(self, data, src):
        request = self.approvals.request(
            principal=str(data.get("principal") or src),
            capability=str(data.get("capability") or ""),
            scope=dict(data.get("scope") or {}),
            reason=str(data.get("reason") or ""),
            preview=str(data.get("preview") or "")[: self.config.security.max_preview_chars],
            project_id=data.get("project_id"),
            task_id=data.get("task_id"),
            risk=data.get("risk"),
            ttl_s=int(data["ttl_s"]) if data.get("ttl_s") else None,
        )
        return request.to_dict()

    async def _approval_status(self, data, src):
        request = self.approvals.poll(str(data.get("request_id") or ""))
        return request.to_dict()

    async def _approval_use(self, data, src):
        ok = self.approvals.use(str(data.get("grant_id") or ""))
        return {"ok": ok}

    async def _approval_list(self, data, src):
        items = self.approvals.list(status=data.get("status"),
                                     project_id=data.get("project_id"),
                                     limit=int(data.get("limit") or 200))
        return {"approvals": [a.to_dict() for a in items]}

    async def _approval_decide(self, data, src):
        request = self.approvals.decide(
            str(data.get("request_id") or data.get("id") or ""),
            decision=str(data.get("decision") or ""),
            mode=str(data.get("mode") or "once"),
            by=str(data.get("by") or "operator"),
            note=str(data.get("note") or ""),
        )
        return request.to_dict()

    async def _rule_list(self, data, src):
        rules = self.policy.list_rules(active_only=bool(data.get("active_only")))
        return {"rules": [r.to_dict() for r in rules]}

    async def _rule_create(self, data, src):
        rule = self.policy.add_rule(
            effect=str(data.get("effect") or "allow"),
            capability=str(data.get("capability") or ""),
            principal=str(data.get("principal") or "*"),
            scope=dict(data.get("scope") or {}),
            risk_max=str(data.get("risk_max") or "high"),
            expires_at=data.get("expires_at"),
            uses_left=data.get("uses_left"),
            note=str(data.get("note") or ""),
            created_by=str(data.get("created_by") or "operator"),
        )
        if rule.effect == "allow":
            self.approvals.resolve_pending()
        return rule.to_dict()

    async def _rule_update(self, data, src):
        rule = self.policy.update_rule(str(data.get("rule_id") or data.get("id") or ""),
                                       dict(data.get("patch") or {}))
        self.approvals.resolve_pending()
        return rule.to_dict()

    async def _rule_delete(self, data, src):
        return {"deleted": self.policy.delete_rule(str(data.get("rule_id") or data.get("id") or ""))}

    # ------------------------------------------------------------------
    # projects / tasks
    # ------------------------------------------------------------------
    async def _project_create(self, data, src):
        name = str(data.get("name") or "").strip()
        goal = str(data.get("goal") or "").strip()
        if not name:
            raise BadRequest("project name required")
        card = await self.create_project(
            name=name, goal=goal or name,
            created_by=str(data.get("created_by") or src),
            spawn_lead=bool(data.get("spawn_lead", True)),
        )
        return card

    async def _project_list(self, data, src):
        cards = [self.project_card(p.id) for p in self.projects.list(status=data.get("status"))]
        return {"projects": cards}

    async def _project_get(self, data, src):
        return self.project_card(str(data.get("project_id") or ""))

    async def _project_update(self, data, src):
        project = self.projects.update(str(data.get("project_id") or ""),
                                       dict(data.get("patch") or {}))
        return self.project_card(project.id)

    async def _project_archive(self, data, src):
        return await self.archive_project(str(data.get("project_id") or ""),
                                          by=str(data.get("by") or src))

    async def _project_delete(self, data, src):
        return await self.delete_project(
            str(data.get("project_id") or ""),
            purge_workspace=bool(data.get("purge_workspace", True)),
            confirm=data.get("confirm"),
            by=str(data.get("by") or src),
        )

    async def _task_create(self, data, src):
        task = self.projects.add_task(
            str(data.get("project_id") or ""),
            str(data.get("title") or ""),
            str(data.get("description") or ""),
            role=data.get("role"),
            priority=int(data.get("priority") or 0),
            deps=list(data.get("deps") or []),
            created_by=str(data.get("created_by") or src),
            milestone_id=data.get("milestone_id"),
        )
        return task.to_dict()

    async def _task_list(self, data, src):
        tasks = self.projects.task_list(
            project_id=data.get("project_id"),
            status=data.get("status"),
            assignee=data.get("assignee"),
        )
        return {"tasks": [t.to_dict() for t in tasks]}

    async def _task_get(self, data, src):
        return self.projects.task_get(str(data.get("task_id") or "")).to_dict()

    async def _task_claim(self, data, src):
        task = self.projects.task_claim(str(data.get("task_id") or ""),
                                        str(data.get("agent_id") or src))
        return task.to_dict()

    async def _task_update(self, data, src):
        task = self.projects.task_update(str(data.get("task_id") or ""),
                                         dict(data.get("patch") or {}))
        return task.to_dict()

    async def _task_artifact(self, data, src):
        task = self.projects.task_artifact(str(data.get("task_id") or ""),
                                           dict(data.get("artifact") or {}))
        return task.to_dict()

    async def _task_note(self, data, src):
        task = self.projects.task_add_note(str(data.get("task_id") or ""),
                                           str(data.get("text") or ""),
                                           author=str(data.get("author") or src))
        return task.to_dict()

    async def _task_check(self, data, src):
        task = self.projects.task_check(
            str(data.get("task_id") or ""),
            item_id=data.get("item_id"),
            text=data.get("text"),
            done=bool(data.get("done", True)),
            note=str(data.get("note") or ""),
        )
        return task.to_dict()

    # ------------------------------------------------------------------
    # agents
    # ------------------------------------------------------------------
    async def _agent_register(self, data, src):
        role = str(data.get("role") or "")
        unit_id = str(data.get("unit_id") or src)
        # The supervisor pre-creates the agent when it spawns the process;
        # the worker's registration binds that record to its live unit.
        for existing in self.supervisor.list():
            if existing.unit_id == unit_id and existing.role == role:
                patch = {"status": "idle"}
                if data.get("project_id"):
                    patch["project_id"] = data["project_id"]
                if data.get("task_id"):
                    patch["task_id"] = data["task_id"]
                if data.get("model"):
                    patch["model"] = data["model"]
                agent = self.supervisor.update(existing.id, patch)
                return agent.to_dict()
        agent = self.supervisor.register(
            role=role,
            project_id=data.get("project_id"),
            unit_id=unit_id,
            task_id=data.get("task_id"),
            model=str(data.get("model") or ""),
        )
        if role == "lead" and agent.project_id:
            try:
                project = self.projects.get(agent.project_id)
                if project.degraded:
                    self.projects.update(agent.project_id, {"degraded": False})
            except Exception:
                pass
        return agent.to_dict()

    async def _agent_update(self, data, src):
        agent = self.supervisor.update(str(data.get("agent_id") or ""),
                                       dict(data.get("patch") or {}))
        return agent.to_dict()

    async def _agent_finish(self, data, src):
        agent = self.supervisor.finish(str(data.get("agent_id") or ""),
                                       str(data.get("summary") or ""),
                                       status=str(data.get("status") or "done"))
        return agent.to_dict()

    async def _agent_spawn(self, data, src):
        agent = await self.supervisor.spawn(
            str(data.get("role") or ""),
            data.get("project_id"),
            task_id=data.get("task_id"),
            reason=str(data.get("reason") or "requested"),
        )
        return agent.to_dict()

    async def _agent_kill(self, data, src):
        agent_id = str(data.get("agent_id") or "")
        try:
            agent = self.supervisor.get(agent_id)
        except Exception:
            agent = None
        ok = await self.supervisor.kill(agent_id,
                                        reason=str(data.get("reason") or "killed"))
        if agent is not None:
            self._cancel_agent_approvals(agent, "agent killed")
        return {"ok": ok}

    async def _agent_list(self, data, src):
        agents = self.supervisor.list(project_id=data.get("project_id"),
                                      status=data.get("status"))
        return {"agents": [a.to_dict() for a in agents]}

    # ------------------------------------------------------------------
    # events / memory / chat
    # ------------------------------------------------------------------
    def _on_unit_event(self, record) -> None:
        try:
            stored = self.eventlog.append_record(record)
        except Exception:
            self.log.exception("failed to persist remote event")
            return
        try:
            self.ctx.emit(stored.type, stored.to_dict())
        except Exception:
            self.log.exception("event dispatch failed for %s", stored.type)

    def _on_unit_offline(self, payload: dict) -> None:
        data = payload.get("data") if isinstance(payload, dict) else {}
        work_id = (data or {}).get("work_id")
        if not work_id:
            return
        self.transport.remove_unit(work_id)
        self.leases.release_holder(work_id, reason="unit_offline")
        agent = self._agent_for_unit(work_id)
        if agent is None:
            return
        self.supervisor.update(agent.id, {
            "status": "dead",
            "error": "unit offline",
        })
        self._cancel_agent_approvals(agent, "agent offline")
        if agent.role == "lead" and agent.project_id:
            try:
                self.projects.update(agent.project_id, {"degraded": True})
                self.eventlog.append("project.degraded", {
                    "project_id": agent.project_id,
                    "reason": "lead unit offline",
                }, source="sys", project_id=agent.project_id)
            except Exception:
                self.log.debug("could not mark project degraded", exc_info=True)
        # Free the task so a restarted or replacement worker can pick it up.
        if agent.task_id:
            try:
                task = self.projects.task_get(agent.task_id)
                if task.status in ("doing",):
                    self.projects.task_update(task.id, {
                        "status": "todo", "assignee": None,
                        "error": "retry: previous agent went offline",
                    })
                elif task.status == "review":
                    self.projects.task_update(task.id, {
                        "status": "todo", "assignee": None,
                        "error": "retry: reviewer went offline",
                    })
            except Exception:
                self.log.debug("could not reset task for dead agent", exc_info=True)

    async def _event_append(self, data, src):
        record = self.eventlog.append(
            str(data.get("type") or ""),
            dict(data.get("data") or {}),
            source=str(data.get("source") or src),
            project_id=data.get("project_id"),
        )
        return {"seq": record.seq, "id": record.id}

    async def _event_since(self, data, src):
        records = self.eventlog.since(
            int(data.get("seq") or 0),
            limit=int(data.get("limit") or 500),
            project_id=data.get("project_id"),
        )
        return {"events": [r.to_dict() for r in records]}

    async def _memory_get(self, data, src):
        return {"value": self.memory.get(str(data.get("project_id") or ""),
                                         str(data.get("key") or ""))}

    async def _memory_set(self, data, src):
        value = self.memory.set(str(data.get("project_id") or ""),
                                str(data.get("key") or ""),
                                data.get("value"))
        self.eventlog.append("memory.updated", {
            "project_id": data.get("project_id"),
            "key": data.get("key"),
        }, source=str(src), project_id=data.get("project_id"))
        return {"value": value}

    async def _chat_send(self, data, src):
        message = await self.chat.send_user(str(data.get("text") or ""),
                                            from_=str(data.get("from") or "human:operator"))
        return message

    # ------------------------------------------------------------------
    # high-level operations used by the web layer and the chat assistant
    # ------------------------------------------------------------------
    async def create_project(self, name: str, goal: str, *, created_by: str = "human:operator",
                             spawn_lead: bool = True) -> dict:
        plan = plan_for(goal, title=name)
        milestone_titles: list[str] = []
        for item in plan:
            if item.milestone and item.milestone not in milestone_titles:
                milestone_titles.append(item.milestone)
        project = self.projects.create(
            name, goal, created_by=created_by,
            milestones=[{"title": title} for title in milestone_titles],
        )
        title_to_milestone = {m.title: m.id for m in project.milestones}
        for item in plan:
            self.projects.add_task(
                project.id, item.title, item.description,
                role=item.role, priority=item.priority,
                created_by=created_by,
                milestone_id=title_to_milestone.get(item.milestone or ""),
            )
        self.projects.update(project.id, {"status": "active"})
        if spawn_lead:
            try:
                await self.supervisor.spawn("lead", project.id, reason="project_created")
            except SfoError as exc:
                self.log.warning("could not spawn lead for %s: %s", project.id, exc.message)
                self.eventlog.append("agent.failed", {
                    "role": "lead", "project_id": project.id,
                    "error": exc.message,
                }, source="sys", project_id=project.id)
        return self.project_card(project.id)

    async def archive_project(self, project_id: str, *, by: str = "human:operator") -> dict:
        project = self.projects.get(project_id)
        killed = await self.supervisor.kill_project(project.id, reason="project_archived")
        stopped = await self._wait_agents_stopped(project.id, timeout=5.0)
        canceled = self._cancel_project_approvals(project.id, "project archived")
        leases = self._release_project_leases(project.id)
        project = self.projects.archive(project.id)
        self.eventlog.append("project.archive.completed", {
            "project_id": project.id,
            "by": by,
            "agents_stopped": killed,
            "agents_stopped_cleanly": stopped,
            "approvals_canceled": canceled,
            "leases_released": leases,
        }, source="operator", project_id=project.id)
        return {
            "project": self.project_card(project.id),
            "agents_stopped": killed,
            "agents_stopped_cleanly": stopped,
            "approvals_canceled": canceled,
            "leases_released": leases,
        }

    async def delete_project(self, project_id: str, *, purge_workspace: bool = True,
                             confirm: str | None = None,
                             by: str = "human:operator") -> dict:
        """Permanently remove a project after stopping its agents.

        This is the destructive path: callers must pass ``confirm`` equal to
        the project name (or id). The event log keeps the deletion record; the
        project JSON record, its tasks and (optionally) the workspace are gone.
        """
        project = self.projects.get(project_id)
        expected = {project.name, project.id}
        if confirm is None or str(confirm).strip() not in expected:
            raise BadRequest(
                "project deletion requires confirm=<project name>",
                detail={"project_id": project_id, "name": project.name})
        task_count = len(self.projects.task_list(project_id=project_id))
        killed = await self.supervisor.kill_project(project_id, reason="project_deleted")
        stopped = await self._wait_agents_stopped(project_id, timeout=8.0)
        canceled = self._cancel_project_approvals(project_id, "project deleted")
        leases = self._release_project_leases(project_id)
        record = self.projects.delete(project_id)
        self.memory.data.pop(project_id, None)
        try:
            write_json(self.paths.state_dir / "state" / "memory.json", self.memory.data)
        except Exception:
            self.log.debug("memory cleanup failed", exc_info=True)
        purged = False
        purge_error: str | None = None
        if purge_workspace:
            try:
                purged = self._purge_workspace(record)
            except Exception as exc:
                purge_error = str(exc)
                self.log.exception("workspace purge failed for %s", project_id)
                self.eventlog.append("project.purge.failed", {
                    "project_id": project_id, "error": purge_error,
                }, source="sys", project_id=project_id)
        self.eventlog.append("project.delete.completed", {
            "project_id": project_id,
            "name": record.name,
            "by": by,
            "tasks_removed": task_count,
            "agents_stopped": killed,
            "agents_stopped_cleanly": stopped,
            "approvals_canceled": canceled,
            "leases_released": leases,
            "workspace_purged": purged,
            "purge_error": purge_error,
        }, source="operator", project_id=project_id)
        return {
            "deleted": project_id,
            "name": record.name,
            "tasks_removed": task_count,
            "agents_stopped": killed,
            "agents_stopped_cleanly": stopped,
            "approvals_canceled": canceled,
            "leases_released": leases,
            "workspace_purged": purged,
            "purge_error": purge_error,
        }

    async def _wait_agents_stopped(self, project_id: str, *,
                                   timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        terminal = ("done", "failed", "dead")
        while time.monotonic() < deadline:
            active_agents = [agent for agent in self.supervisor.list(project_id=project_id)
                             if agent.status not in terminal]
            # supervisor.kill marks the roster dead before the OS process
            # actually exits; also wait for the unit to release its address.
            active_units = [unit for unit in self.registry.list(project_id=project_id)
                            if unit.status in ("online", "suspect")]
            if not active_agents and not active_units:
                return True
            await asyncio.sleep(0.2)
        return False

    def _cancel_orphan_approvals(self) -> int:
        """On boot, deny pending approvals whose principal agent is gone."""
        canceled = 0
        for request in self.approvals.list(status="pending"):
            principal = request.principal
            if not principal.startswith("agent-"):
                continue
            try:
                agent = self.supervisor.get(principal)
            except Exception:
                agent = None
            if agent is not None and agent.status not in ("dead", "failed", "done"):
                continue
            try:
                self.approvals.decide(request.id, decision="deny", by="system",
                                      note="orphaned: agent is not active")
                canceled += 1
            except Exception:
                self.log.debug("could not cancel orphan approval %s", request.id,
                               exc_info=True)
        if canceled:
            self.log.info("canceled %d orphaned approval(s) on boot", canceled)
        return canceled

    def _cancel_agent_approvals(self, agent, reason: str) -> int:
        """Deny approvals left behind by a dead agent so they stop hanging."""
        canceled = 0
        principals = {agent.id, agent.unit_id}
        for request in self.approvals.list(status="pending"):
            if request.principal not in principals:
                continue
            try:
                self.approvals.decide(request.id, decision="deny", by="system",
                                      note=reason)
                canceled += 1
            except Exception:
                self.log.debug("could not cancel approval %s", request.id,
                               exc_info=True)
        return canceled

    def _cancel_project_approvals(self, project_id: str, reason: str) -> int:
        canceled = 0
        for request in self.approvals.list(status="pending", project_id=project_id):
            try:
                self.approvals.decide(request.id, decision="deny", by="system",
                                      note=reason)
                canceled += 1
            except Exception:
                self.log.debug("could not cancel approval %s", request.id,
                               exc_info=True)
        return canceled

    def _release_project_leases(self, project_id: str) -> int:
        released = 0
        prefix = f"project:{project_id}:"
        for lease in list(self.leases.list(active_only=True)):
            if not (lease.resource.startswith(prefix)
                    or (lease.metadata or {}).get("project_id") == project_id):
                continue
            try:
                self.leases.release(lease.id, holder=lease.holder,
                                    reason="project_closed")
                released += 1
            except Exception:
                self.log.debug("could not release lease %s", lease.id,
                               exc_info=True)
        for agent in self.supervisor.list(project_id=project_id):
            for holder in (agent.id, agent.unit_id):
                if not holder:
                    continue
                try:
                    released += len(self.leases.release_holder(
                        holder, reason="project_closed"))
                except Exception:
                    self.log.debug("could not release leases for %s", holder,
                                   exc_info=True)
        return released

    def _purge_workspace(self, project) -> bool:
        """Remove ``<projects_dir>/<project_id>`` after a containment check."""
        projects_root = self.paths.projects_dir.resolve()
        project_dir = (self.paths.projects_dir / project.id).resolve()
        if project_dir == projects_root or project_dir.parent != projects_root:
            raise ValidationError(
                "refusing to purge outside the projects directory",
                detail={"path": str(project_dir), "root": str(projects_root)})
        if project_dir.name != project.id:
            raise ValidationError("refusing to purge an unexpected path")
        if not project_dir.exists():
            return False
        shutil.rmtree(project_dir)
        return True

    def _start_maintenance_loop(self) -> None:
        """Expire approvals and leases even when nobody is polling them."""
        async def loop() -> None:
            while not self._stopped:
                await asyncio.sleep(1.0)
                if self._stopped:
                    return
                try:
                    expired = self.approvals.sweep()
                    if expired:
                        self.log.info("%d approval(s) expired", len(expired))
                except Exception:
                    self.log.exception("approval sweep failed")
                try:
                    self.leases.sweep()
                except Exception:
                    self.log.exception("lease sweep failed")

        self._maintenance_task = asyncio.create_task(loop(), name="maintenance-loop")


    async def complete_project(self, project_id: str, *, by: str = "human:operator") -> dict:
        project = self.projects.get(project_id)
        if project.status not in ("review", "active"):
            raise Conflict(f"project {project_id} is {project.status}")
        for task in self.projects.task_list(project_id=project_id):
            if task.status in ("review",):
                self.projects.task_update(task.id, {"status": "done"})
        self.projects.update(project_id, {"status": "done"})
        await self.supervisor.kill_project(project_id, reason="project_done")
        self.eventlog.append("project.completed", {"project_id": project_id, "by": by},
                             source="operator", project_id=project_id)
        return self.project_card(project_id)

    async def decide_approval(self, request_id: str, *, decision: str, mode: str = "once",
                              note: str = "", by: str = "human:operator") -> dict:
        request = self.approvals.decide(request_id, decision=decision, mode=mode,
                                        by=by, note=note)
        self.eventlog.append("approval.ui_decided", {
            "request_id": request.id, "decision": decision, "mode": mode,
            "by": by,
        }, source="operator", project_id=request.project_id)
        return request.to_dict()

    async def spawn_agent(self, role: str, project_id: str | None = None,
                          task_id: str | None = None) -> dict:
        agent = await self.supervisor.spawn(role, project_id, task_id=task_id,
                                            reason="operator_requested")
        return agent.to_dict()

    async def kill_agent(self, agent_id: str) -> bool:
        return await self.supervisor.kill(agent_id, reason="operator_requested")

    async def kill_unit(self, work_id: str) -> dict:
        return await self._unit_kill({"work_id": work_id, "reason": "operator_requested"},
                                     "operator")

    async def update_config(self, patch: dict) -> dict:
        if not isinstance(patch, dict) or not patch:
            raise BadRequest("config patch required")
        patch = dict(patch)
        llm_patch = patch.get("llm")
        if isinstance(llm_patch, dict):
            llm_patch = dict(llm_patch)
            # The UI shows the key as "***"; echoing that back must not
            # overwrite the real secret.
            if llm_patch.get("api_key") == "***":
                llm_patch.pop("api_key")
            patch["llm"] = llm_patch
        previous_mode = self.config.transport.mode
        previous_port = self.config.web.port
        previous_provider = getattr(self.provider, "name", "")
        # Build the candidate provider before persisting so an invalid model
        # or base URL cannot leave a broken config on disk.
        candidate = self.config.with_overrides(patch)
        new_provider = make_provider(candidate.llm)
        cfg = self.config.update(patch)
        self.config = cfg
        # Apply what can be applied live.
        try:
            self.approvals.approval_ttl_s = cfg.security.approval_ttl_s
            self.approvals.grant_ttl_s = cfg.security.grant_ttl_s
            self.policy.auto_allow_low = cfg.security.auto_allow_low
            self.policy.medium_default = cfg.security.medium_default
            self.policy.high_default = cfg.security.high_default
            self.approvals.resolve_pending()
        except Exception:
            self.log.exception("live config application failed")
        # Chat sessions hold a provider instance; rebuild it so a model
        # switch takes effect on the next message instead of on hub restart.
        self.provider = new_provider
        if getattr(self, "chat", None) is not None:
            self.chat.provider = new_provider
        if getattr(new_provider, "name", "") != previous_provider:
            self.eventlog.append("sys.llm.updated", {
                "provider": getattr(new_provider, "name", ""),
                "model": getattr(new_provider, "model", ""),
            }, source="sys")
            self.log.info("llm provider switched to %s (%s)",
                          getattr(new_provider, "name", "?"),
                          getattr(new_provider, "model", ""))
        restart_required = []
        if cfg.transport.mode != previous_mode:
            restart_required.append("transport.mode")
        if cfg.web.port != previous_port:
            restart_required.append("web.port")
        self.eventlog.append("sys.config.updated", {
            "patch": patch, "restart_required": restart_required,
        }, source="operator")
        return {"config": cfg.public_dict(), "restart_required": restart_required}

    async def assistant_tool(self, tool: str, args: dict, *, wait: bool = False,
                             on_pending=None) -> dict:
        """Execute a chat-assistant tool call behind the approval gate.

        With ``wait`` the call parks until a human decides (or the approval
        expires) and then carries on; ``on_pending`` is awaited once with the
        parked request so the chat can tell the user where to approve.
        """
        mapping = {
            "project.create": ("project.create", {}),
            "project.list": ("data.read", {}),
            "project.get": ("data.read", {"project_id": args.get("project_id")}),
            "task.list": ("data.read", {"project_id": args.get("project_id")}),
            "unit.list": ("data.read", {}),
            "event.query": ("data.read", {"project_id": args.get("project_id")}),
            "agent.spawn": ("agent.spawn", {"project_id": args.get("project_id"),
                                           "role": args.get("role")}),
            "agent.kill": ("agent.kill", {"agent_id": args.get("agent_id")}),
            "project.update": ("project.update", {"project_id": args.get("project_id")}),
            "project.archive": ("project.archive", {"project_id": args.get("project_id")}),
            "project.delete": ("project.delete", {
                "project_id": args.get("project_id"),
                "purge_workspace": bool(args.get("purge_workspace", True)),
            }),
        }
        if tool not in mapping:
            return {"ok": False, "error": f"assistant tool {tool!r} is not allowed"}
        capability, scope = mapping[tool]
        preview = str(args)
        if tool in ("project.delete", "project.archive"):
            try:
                target = self.projects.get(str(args.get("project_id") or ""))
                action = "永久删除" if tool == "project.delete" else "归档"
                preview = (f"{action}项目「{target.name}」({target.id})\n"
                           f"工作区：{target.workspace}\n"
                           f"任务数：{len(self.projects.task_list(project_id=target.id))}")
            except SfoError:
                pass
        request = self.approvals.request(
            "agent-assistant", capability, scope,
            reason=f"assistant tool: {tool}",
            preview=preview[: self.config.security.max_preview_chars],
            project_id=args.get("project_id"),
        )
        if request.status == "pending":
            if not wait:
                return {"ok": False, "error": f"已提交审批 {request.id}，等待人工放行"}
            if on_pending is not None:
                try:
                    await on_pending(request)
                except Exception:  # noqa: BLE001 - notification is best effort
                    self.log.debug("assistant on_pending failed", exc_info=True)
            while request.status == "pending":
                await asyncio.sleep(0.5)
                request = self.approvals.poll(request.id)
        if request.status != "approved":
            why = request.note or request.status
            return {"ok": False, "error": f"审批未通过（{request.status}：{why}）"}
        try:
            if tool == "project.create":
                card = await self.create_project(
                    str(args.get("name") or "未命名项目"),
                    str(args.get("goal") or args.get("name") or ""),
                    created_by="agent-assistant",
                )
                result = card
            elif tool == "project.list":
                result = {"projects": [self.project_card(p.id) for p in self.projects.list()]}
            elif tool == "project.get":
                result = self.project_card(str(args.get("project_id") or ""))
            elif tool == "task.list":
                result = {"tasks": [t.to_dict() for t in
                                    self.projects.task_list(project_id=args.get("project_id"))]}
            elif tool == "unit.list":
                result = {"units": [u.to_dict() for u in self.registry.list()]}
            elif tool == "event.query":
                result = {"events": [r.to_dict() for r in self.eventlog.since(
                    int(args.get("since") or 0), limit=int(args.get("limit") or 100),
                    project_id=args.get("project_id"))]}
            elif tool == "agent.spawn":
                result = await self.spawn_agent(str(args.get("role") or "coder"),
                                                args.get("project_id"), args.get("task_id"))
            elif tool == "agent.kill":
                result = {"ok": await self.kill_agent(str(args.get("agent_id") or ""))}
            elif tool == "project.update":
                result = self.projects.update(str(args.get("project_id") or ""),
                                              dict(args.get("patch") or {})).to_dict()
            elif tool == "project.archive":
                result = await self.archive_project(
                    str(args.get("project_id") or ""), by="agent-assistant")
            elif tool == "project.delete":
                target = self.projects.get(str(args.get("project_id") or ""))
                result = await self.delete_project(
                    target.id,
                    purge_workspace=bool(args.get("purge_workspace", True)),
                    confirm=target.name, by="agent-assistant")
            else:  # pragma: no cover
                return {"ok": False, "error": "unhandled tool"}
        except SfoError as exc:
            return {"ok": False, "error": f"{exc.code}: {exc.message}"}
        if request.grant_id:
            self.approvals.use(request.grant_id)
        return {"ok": True, "data": result}

    # ------------------------------------------------------------------
    # projections for the UI
    # ------------------------------------------------------------------
    def project_card(self, project_id: str) -> dict:
        card = self.projects.card(project_id)
        counts = card.get("counts") or {}
        card["total_tasks"] = sum(int(v) for v in counts.values())
        agents = [a.to_dict() for a in self.supervisor.list(project_id=project_id)
                  if a.status not in ("dead",)]
        pending = [r for r in self.approvals.list(status="pending", project_id=project_id)]
        leases = self.leases.list(active_only=True)
        card["agents"] = agents
        card["pending_approvals"] = len(pending)
        card["leases"] = [l.to_dict() for l in leases
                          if card.get("id") in json_dumps_safe(l.resource)]
        return card

    def stats(self) -> dict:
        units = self.registry.stats()
        agents = self.supervisor.stats()
        return {
            "units": units,
            "agents": agents,
            "projects": len(self.projects.list()),
            "active_projects": len([p for p in self.projects.list()
                                    if p.status in ("active", "review", "degraded")]),
            "leases": len(self.leases.list(active_only=True)),
            "pending_approvals": self.approvals.pending_count(),
            "events": self.eventlog.last_seq,
            "uptime_s": int(time.time() - self.started_at / 1000),
        }

    def state_summary(self) -> str:
        snap = self.snapshot()
        projects = snap.get("projects") or []
        active = [p for p in projects if p.get("status") in ("active", "review", "degraded")]
        agents = [a for a in (snap.get("agents") or [])
                  if a.get("status") in ("working", "waiting", "idle")]
        approvals = snap.get("approvals") or []
        parts = [f"{len(active)} 个进行中项目", f"{len(agents)} 个在岗 Agent",
                 f"{len(approvals)} 个待审批"]
        return "，".join(parts)

    def snapshot(self) -> dict:
        try:
            projects = [self.project_card(p.id) for p in self.projects.list()]
        except Exception:
            self.log.exception("project snapshot failed")
            projects = []
        agents = [a.to_dict() for a in self.supervisor.list()]
        units = [u.to_dict() for u in self.registry.list()]
        approvals = [a.to_dict() for a in self.approvals.list(status="pending", limit=100)]
        rules = [r.to_dict() for r in self.policy.list_rules(active_only=True)
                 if r.source == "rule"]
        events = [r.to_dict() for r in self.eventlog.tail(80)]
        chat = self.chat.history(80) if hasattr(self, "chat") else []
        leases = [l.to_dict() for l in self.leases.list(active_only=True)]
        info = {
            "name": self.config.system.name,
            "version": self.version,
            "host": self.config.system.host_label,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "transport": self.config.transport.mode,
            "sys_rpc": self.sys_addresses.rpc,
            "sys_events": self.sys_addresses.events,
            "web_port": self.config.web.port,
            "last_seq": self.eventlog.last_seq,
            "llm": {
                "provider": getattr(self.provider, "name", ""),
                "model": getattr(self.provider, "model", ""),
            },
            "stats": self.stats(),
        }
        return {
            "sys": info,
            "units": units,
            "projects": projects,
            "agents": agents,
            "leases": leases,
            "approvals": approvals,
            "rules": rules,
            "events": events,
            "chat": chat,
            "config": self.config.public_dict(),
            "stats": self.stats(),
        }

    def console(self, *, types: list[str] | None = None, project_id: str | None = None,
                limit: int = 200) -> list[dict]:
        records = self.eventlog.tail(max(limit * 4, 400))
        if project_id:
            records = [r for r in records if r.project_id == project_id]
        if types:
            wanted = set(types)
            records = [r for r in records if r.type in wanted
                       or any(r.type.startswith(t + ".") for t in wanted)]
        return [r.to_dict() for r in records[-limit:]]


def json_dumps_safe(value: Any) -> str:
    try:
        import json
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def hwinfo() -> dict:
    info: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "hostname": socket.gethostname(),
    }
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            mem = {}
            for line in fh:
                key, _, rest = line.partition(":")
                mem[key.strip()] = rest.strip().split()[0]
        info["mem_total_kb"] = int(mem.get("MemTotal", "0"))
        info["mem_available_kb"] = int(mem.get("MemAvailable", "0"))
    except Exception:
        pass
    try:
        info["loadavg"] = list(os.getloadavg())
    except Exception:
        pass
    try:
        usage = shutil.disk_usage(str(Path.home()))
        info["disk"] = {"total": usage.total, "free": usage.free}
    except Exception:
        pass
    return info

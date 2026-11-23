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

from .agents import AgentSupervisor, TASK_ROLES, ToolExecutor
from .ai import make_provider
from .config import Config
from .errors import (BadRequest, Conflict, Denied, NotFound, SfoError,
                     Unauthorized, Unavailable, ValidationError)
from .kernel import Context
from .l2 import EventLog, FailureManager, FailurePolicy, LeaseManager, Scheduler, UnitInfo, UnitRegistry
from .protocol import PROTOCOL_VERSION
from .projects import ProjectStore, plan_for
from .security import (ApprovalBroker, PolicyEngine, Principal,
                       RpcAuthRegistry, project_workspace)
from .session.chat import ChatService
from .session.operator import OperatorTools
from .transport import AddressAllocator, HubTransport, socket_dir_for
from .util.jsonio import read_json, write_json
from .util.logging import get_logger, setup_logging

VERSION = "0.1.0"

#: Operator-only actions.  Worker units are denied these over RPC regardless of
#: the ``src`` they claim; in-process operator/web calls are unaffected.
#: ``rule.*`` is expanded explicitly because the dispatch table is flat.
#: ``agent.kill``/``project.archive`` are *not* here since M5: a lead may do
#: them inside its own project, which ``_authorize_scope`` enforces.  Creating
#: or enumerating projects stays operator-only (no unit may see the global
#: project table).
PRIVILEGED_ACTIONS = {
    "sys.config.set",
    "rule.create", "rule.update", "rule.delete",
    "approval.decide", "approval.list",
    "project.create", "project.list", "project.delete",
    "unit.kill", "unit.allocate",
    "chat.send",
}

#: Task actions that resolve one task.  External units may only touch tasks of
#: their own project; plain workers only the single task bound to their token.
TASK_SCOPED_ACTIONS = frozenset({
    "task.get", "task.update", "task.artifact", "task.note", "task.check",
    "task.claim",
})

#: Fields a unit (lead or worker) may change through ``project.update``.  The
#: rest of the card (name/goal/status/tags/color/degraded) is operator-only.
UNIT_PROJECT_PATCH_FIELDS = frozenset({"brief"})

#: Actions whose handler receives the authenticated principal as a third
#: argument (identity/owner binding: own unit, own lease, own approval).
PRINCIPAL_ACTIONS = frozenset({
    "unit.addresses", "unit.register", "unit.heartbeat", "unit.release",
    "lease.acquire", "lease.renew", "lease.release", "lease.list",
    "approval.status", "approval.use",
})


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
        self._aggregated_signatures: dict[str, tuple] = {}
        self._maintenance_task: asyncio.Task | None = None
        self._stopped = False
        self._lock_fd: int | None = None
        self._assistant_executor: ToolExecutor | None = None
        self._assistant_agent = None
        self.operator = OperatorTools(self)
        self.log = get_logger("app")
        # Protocol v2 credentials: unit tokens are issued for every spawned
        # worker; operator tokens exist only in this process' memory.
        self.rpc_auth = RpcAuthRegistry(logger=get_logger("security.rpc_auth"))
        self.operator_token: str | None = None

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
            token_issuer=self.rpc_auth.issue_unit,
            token_revoker=self.rpc_auth.revoke,
            logger=get_logger("agents"),
        )
        self.supervisor.load()
        self.supervisor.prune_finished()
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

    @staticmethod
    def aggregation_signature(project: Any, tasks: list[Any]) -> tuple:
        """What makes a new summary necessary: task identity/status changes."""
        return (
            str(getattr(project, "status", "")),
            tuple((str(getattr(task, "id", "")),
                   str(getattr(task, "status", "")),
                   int(getattr(task, "updated_at", 0) or 0))
                  for task in tasks),
        )

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
                    self.supervisor.prune_finished()
                    for project in self.projects.list():
                        if project.status not in ("active", "review", "degraded"):
                            continue
                        tasks = self.projects.task_list(project_id=project.id)
                        if tasks == []:
                            continue
                        signature = self.aggregation_signature(project, tasks)
                        if self._aggregated_signatures.get(project.id) == signature:
                            continue
                        running = [a for a in self.supervisor.list(project_id=project.id)
                                   if a.role == "aggregator"
                                   and a.status not in ("done", "failed", "dead")]
                        if running:
                            continue
                        try:
                            await self.supervisor.spawn(
                                "aggregator", project.id,
                                reason="task_changed")
                            # Only re-summarise when the task shape actually
                            # changed; otherwise a finished aggregator would
                            # respawn every interval forever.
                            self._aggregated_signatures[project.id] = signature
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
        self.rpc_auth.revoke_all()
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
            "approval.decide": self._approval_decide,
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
            # Authenticate *before* anything else: protocol v1 clients and
            # missing/forged tokens are rejected with 401, and the replay
            # cache is keyed by the authenticated principal (``src`` is
            # self-reported and must never influence deduplication).
            principal = self._authenticate_env(env)
            # RPC retries reuse the envelope id. Non-idempotent actions
            # (agent.spawn, project.create, ...) must be executed once: join
            # the in-flight task of the first attempt and replay its result.
            key = f"{principal.kind}:{principal.id}|{env.id}"
            cached = self._rpc_cache.get(key)
            now = time.time()
            if cached is not None and cached[0] > now - self.RPC_CACHE_TTL_S:
                return cached[1]
            task = self._rpc_inflight.get(key)
            if task is None:
                data = env.data if isinstance(env.data, dict) else {}
                task = asyncio.create_task(
                    self.handle_rpc_action(action, data, src=env.src,
                                           external=True, principal=principal))
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

    def _authenticate_env(self, env) -> Principal:
        """Derive the caller principal from an external envelope (or raise 401).

        ``Envelope.src`` never participates: protocol v2 requires a valid
        ``auth`` mapping, and only the token registry decides identity.
        """
        version = int(getattr(env, "v", 0) or 0)
        if version != PROTOCOL_VERSION:
            raise Unauthorized(
                f"unsupported rpc protocol version {version!r}; "
                f"this hub requires v{PROTOCOL_VERSION}")
        if getattr(env, "auth", None) is None:
            raise Unauthorized("rpc auth required (protocol v2)")
        return self.rpc_auth.verify(env.auth)

    def _authorize(self, principal: Principal, action: str) -> None:
        """Deny operator-only actions to units (``src`` is not consulted)."""
        if principal.is_operator or principal.kind == "sys":
            return
        if action in PRIVILEGED_ACTIONS:
            raise Denied(f"{action} is an operator action; worker units may not call it")
        if not principal.is_unit:
            raise Denied(f"unsupported rpc principal kind: {principal.kind!r}")

    def _bind_external(self, principal: Principal, action: str,
                       data: dict) -> dict:
        """Bind identity-bearing fields to the authenticated principal.

        A unit may only speak for itself: ``work_id``/``unit_id``, lease
        holders and approval principals are forced to the connection's
        principal, and an explicit mismatch is denied (not silently ignored).
        ``data["principal"]`` is never trusted.
        """
        if principal.is_unit:
            if action in ("unit.addresses", "unit.register",
                          "unit.heartbeat", "unit.release"):
                claimed = data.get("work_id")
                if claimed is not None and str(claimed) != principal.id:
                    raise Denied(
                        f"{principal.id} may not act for unit {claimed!r}")
                data["work_id"] = principal.id
            elif action == "agent.register":
                claimed = data.get("unit_id")
                if claimed is not None and str(claimed) != principal.id:
                    raise Denied(
                        f"{principal.id} may not register an agent for {claimed!r}")
                data["unit_id"] = principal.id
            elif action == "approval.request":
                claimed = data.get("principal")
                actual = principal.identity
                if claimed and str(claimed) != actual:
                    raise Denied(
                        f"{principal.id} may not request approvals as {claimed!r}")
                data["principal"] = actual
            elif action in ("lease.acquire", "lease.renew", "lease.release",
                            "lease.list"):
                claimed = data.get("holder")
                if claimed is not None and str(claimed) != principal.id:
                    raise Denied(
                        f"{principal.id} may not act on leases of {claimed!r}")
                data["holder"] = principal.id
        elif principal.is_operator:
            if action == "approval.request":
                claimed = data.get("principal")
                if claimed and str(claimed) != principal.identity:
                    raise Denied(
                        f"operator may not request approvals as {claimed!r}")
                data["principal"] = principal.identity
        return data

    # ------------------------------------------------------------------
    # resource scopes (M5)
    # ------------------------------------------------------------------
    def _authorize_scope(self, principal: Principal, action: str,
                         data: dict) -> dict:
        """Enforce project/task/agent resource ranges for external units.

        H3 bound *identity* (work_id/lease holder/approval principal); this
        layer adds *scope* (plan §5.2.5/§5.2.6): a unit only sees and mutates
        the project its credential was issued for, a plain worker only the one
        task it was spawned for, and only a lead may create tasks, spawn/kill
        agents or archive its own project.  Operators and in-process (``sys``)
        callers keep the global view.  ``data`` is mutated and returned so the
        handler never sees a caller-chosen project/src.
        """
        if principal.is_operator or principal.kind == "sys":
            return data
        if not principal.is_unit:
            raise Denied(f"unsupported rpc principal kind: {principal.kind!r}")

        if action == "task.list":
            return self._force_own_project(principal, data)
        if action in TASK_SCOPED_ACTIONS:
            return self._scope_task(principal, action, data)
        if action == "task.create":
            return self._scope_task_create(principal, data)
        if action == "agent.spawn":
            return self._scope_agent_spawn(principal, data)
        if action == "agent.kill":
            return self._scope_agent_kill(principal, data)
        if action in ("agent.register", "agent.update", "agent.finish"):
            return self._scope_agent_identity(principal, action, data)
        if action == "agent.list":
            return self._force_own_project(principal, data)
        if action == "event.append":
            # The hub assigns audit provenance: a unit cannot emit events for
            # another project nor forge an agent/unit source string.  Event
            # *type* whitelisting is X2, not M5.
            data["project_id"] = principal.project_id
            data["source"] = (f"agent:{principal.agent_id}"
                              if principal.agent_id
                              else f"unit:{principal.id}")
            return data
        if action == "event.since":
            return self._force_own_project(principal, data)
        if action in ("memory.get", "memory.set"):
            return self._force_own_project(principal, data)
        if action == "unit.list":
            # The registry filter is the only view a unit gets: no global
            # enumeration of other projects' processes.
            data["project_id"] = self._require_own_project(principal)
            return data
        if action in ("project.get", "project.update"):
            return self._scope_project(principal, action, data)
        if action == "project.archive":
            return self._scope_project_archive(principal, data)
        return data

    def _require_own_project(self, principal: Principal) -> str:
        project_id = principal.project_id
        if not project_id:
            raise Denied(f"{principal.id} has no project scope")
        return str(project_id)

    def _force_own_project(self, principal: Principal, data: dict) -> dict:
        """Force ``data['project_id']`` to the unit's project (403 on another)."""
        own = self._require_own_project(principal)
        requested = data.get("project_id")
        if requested is not None and str(requested) != own:
            raise Denied(
                f"{principal.id} may not access project {requested!r}")
        data["project_id"] = own
        return data

    def _principal_task_id(self, principal: Principal) -> str | None:
        """The single task a plain worker may touch (credential or roster)."""
        if principal.task_id:
            return str(principal.task_id)
        if not principal.agent_id:
            return None
        try:
            agent = self.supervisor.get(principal.agent_id)
        except SfoError:
            return None
        return agent.task_id

    def _scope_task(self, principal: Principal, action: str,
                    data: dict) -> dict:
        task_id = str(data.get("task_id") or "").strip()
        if not task_id:
            raise BadRequest("task_id required")
        task = self.projects.task_get(task_id)  # 404 for unknown ids
        own_project = self._require_own_project(principal)
        if task.project_id != own_project:
            raise Denied(
                f"{principal.id} may not access task {task.id} of project "
                f"{task.project_id!r}")
        if not principal.is_lead:
            own_task = self._principal_task_id(principal)
            if not own_task or task.id != str(own_task):
                raise Denied(
                    f"{principal.id} may only act on its own task {own_task!r}")
        if action == "task.claim":
            agent_id = principal.agent_id
            if not agent_id:
                raise Denied(f"{principal.id} has no bound agent to claim with")
            claimed = data.get("agent_id")
            if claimed is not None and str(claimed) != str(agent_id):
                raise Denied(
                    f"{principal.id} may not claim for agent {claimed!r}")
            data["agent_id"] = str(agent_id)
        return data

    def _scope_task_create(self, principal: Principal, data: dict) -> dict:
        if not principal.is_lead:
            raise Denied("only a project lead may create tasks")
        data["project_id"] = self._require_own_project(principal)
        return data

    def _scope_agent_spawn(self, principal: Principal, data: dict) -> dict:
        if not principal.is_lead:
            raise Denied("only a project lead may spawn agents")
        role = str(data.get("role") or "")
        if role not in TASK_ROLES:
            raise Denied(
                "lead may only spawn task roles "
                f"({', '.join(TASK_ROLES)}), not {role!r}")
        data["project_id"] = self._require_own_project(principal)
        return data

    def _scope_agent_kill(self, principal: Principal, data: dict) -> dict:
        if not principal.is_lead:
            raise Denied("only a project lead may kill agents")
        agent_id = str(data.get("agent_id") or "").strip()
        if not agent_id:
            raise BadRequest("agent_id required")
        target = self.supervisor.get(agent_id)  # 404 when unknown
        own = self._require_own_project(principal)
        if target.project_id != own:
            raise Denied(
                f"{principal.id} may not kill agent {target.id} of project "
                f"{target.project_id!r}")
        if (target.id == principal.agent_id
                or (target.unit_id and target.unit_id == principal.id)):
            raise Denied("a lead may not kill itself")
        return data

    def _scope_agent_identity(self, principal: Principal, action: str,
                              data: dict) -> dict:
        own_agent = principal.agent_id
        if action == "agent.register":
            # ``_bind_external`` already forced unit_id to the principal id.
            claimed = data.get("agent_id")
            allowed = {principal.id}
            if own_agent:
                allowed.add(str(own_agent))
            if claimed is not None and str(claimed) not in allowed:
                raise Denied(
                    f"{principal.id} may not register agent {claimed!r}")
            requested = data.get("project_id")
            if requested is not None and str(requested) != str(principal.project_id):
                raise Denied(
                    f"{principal.id} may not register an agent in project "
                    f"{requested!r}")
            if principal.project_id is not None:
                data["project_id"] = str(principal.project_id)
            requested_task = data.get("task_id")
            if requested_task is not None and principal.project_id:
                task = self.projects.task_get(str(requested_task))
                if task.project_id != principal.project_id:
                    raise Denied(
                        f"{principal.id} may not bind to task {task.id} of "
                        f"project {task.project_id!r}")
            return data
        claimed = str(data.get("agent_id") or "")
        if not own_agent:
            raise Denied(f"{principal.id} has no bound agent")
        if claimed != str(own_agent):
            raise Denied(f"{principal.id} may not update agent {claimed!r}")
        if action == "agent.update":
            patch = data.get("patch")
            if isinstance(patch, dict):
                requested = patch.get("project_id")
                if requested is not None and str(requested) != str(principal.project_id):
                    raise Denied(
                        f"{principal.id} may not move its agent to project "
                        f"{requested!r}")
                requested_task = patch.get("task_id")
                if requested_task is not None:
                    task = self.projects.task_get(str(requested_task))
                    if task.project_id != principal.project_id:
                        raise Denied(
                            f"{principal.id} may not bind to task {task.id} "
                            f"of project {task.project_id!r}")
        return data

    def _scope_project(self, principal: Principal, action: str,
                       data: dict) -> dict:
        data = self._force_own_project(principal, data)
        if action == "project.update":
            patch = data.get("patch")
            if not isinstance(patch, dict):
                raise BadRequest("project patch must be an object")
            extra = sorted(set(patch) - UNIT_PROJECT_PATCH_FIELDS)
            if extra:
                raise Denied(
                    "units may not change project fields "
                    f"{extra}; only 'brief' is allowed")
        return data

    def _scope_project_archive(self, principal: Principal, data: dict) -> dict:
        if not principal.is_lead:
            raise Denied("only a project lead may archive its project")
        return self._force_own_project(principal, data)

    async def handle_rpc_action(self, action: str, data: dict, *, src: str = "sys.0",
                                external: bool = False,
                                principal: Principal | None = None) -> dict:
        """Dispatch one sys action.

        ``external`` marks calls that arrived over the transport: then a
        verified ``principal`` is mandatory, privileged actions are denied to
        units, identity fields are bound to the principal, resource scopes are
        enforced (own project/unit/task), and ``src`` is display-only.
        In-process calls (Web/CLI/chat) pass ``principal=None`` and keep the
        trusted operator path.
        """
        handler = self._actions.get(action)
        if handler is None:
            raise BadRequest(f"unknown action: {action}")
        data = dict(data or {})
        if external:
            if principal is None:
                raise Unauthorized("external rpc requires an authenticated principal")
            self._authorize(principal, action)
            data = self._bind_external(principal, action, data)
            data = self._authorize_scope(principal, action, data)
        if action in PRINCIPAL_ACTIONS:
            return await handler(data, src, principal)
        return await handler(data, src)

    def issue_operator_token(self) -> str:
        """Issue an in-memory operator token for the CLI/tests (never saved)."""
        token = self.rpc_auth.issue_operator()
        self.operator_token = token
        return token

    @staticmethod
    def _owner_principal(principal: Principal | None) -> str | None:
        """Owner string used by approval checks (``None`` = operator view)."""
        if principal is None or principal.is_operator:
            return None
        return principal.identity

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
        # Only operator/in-process callers reach this handler (worker units are
        # denied by PRIVILEGED_ACTIONS); the worker receives the token through
        # SFO_UNIT_TOKEN, never through argv or the wire reply log.
        unit_token = self.rpc_auth.issue_unit(work_id, data.get("project_id"),
                                              role=kind,
                                              agent_id=data.get("agent_id"),
                                              task_id=data.get("task_id"))
        return {
            "work_id": work_id,
            "rpc_address": addresses.rpc,
            "events_address": addresses.events,
            "sys_rpc_address": self.sys_addresses.rpc,
            "sys_events_address": self.sys_addresses.events,
            "transport": self.config.transport.mode,
            "unit_token": unit_token,
        }

    async def _unit_addresses(self, data, src, principal=None):
        work_id = (principal.id if principal is not None and principal.is_unit
                   else str(data.get("work_id") or ""))
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

    async def _unit_register(self, data, src, principal=None):
        work_id = (principal.id if principal is not None and principal.is_unit
                   else str(data.get("work_id") or "").strip())
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

    async def _unit_heartbeat(self, data, src, principal=None):
        work_id = (principal.id if principal is not None and principal.is_unit
                   else str(data.get("work_id") or src))
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

    async def _unit_release(self, data, src, principal=None):
        work_id = (principal.id if principal is not None and principal.is_unit
                   else str(data.get("work_id") or src))
        unit = self.registry.release(work_id, reason=str(data.get("reason") or "released"))
        # The token dies with the unit even if it was already not registered.
        self.rpc_auth.revoke(work_id)
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
    async def _lease_acquire(self, data, src, principal=None):
        if not data.get("resource"):
            raise BadRequest("resource required")
        holder = (principal.id if principal is not None and principal.is_unit
                  else str(data.get("holder") or src))
        lease = self.leases.acquire(
            str(data["resource"]),
            holder,
            int(data.get("ttl_s") or 300),
            renewable=bool(data.get("renewable", True)),
            preemptible=bool(data.get("preemptible", False)),
            priority=int(data.get("priority") or 0),
            metadata=dict(data.get("metadata") or {}),
        )
        return lease.to_dict()

    async def _lease_renew(self, data, src, principal=None):
        holder = (principal.id if principal is not None and principal.is_unit
                  else str(data.get("holder") or src))
        lease = self.leases.renew(str(data.get("lease_id") or ""), holder,
                                  int(data["ttl_s"]) if data.get("ttl_s") else None)
        return lease.to_dict()

    async def _lease_release(self, data, src, principal=None):
        holder = (principal.id if principal is not None and principal.is_unit
                  else (data.get("holder") or src))
        lease = self.leases.release(str(data.get("lease_id") or ""),
                                    holder=holder,
                                    reason=str(data.get("reason") or "released"))
        return lease.to_dict()

    async def _lease_list(self, data, src, principal=None):
        holder = data.get("holder")
        if principal is not None and principal.is_unit:
            # A unit sees only the leases it holds; asking for another holder
            # is treated as an attempt to enumerate someone else's state.
            if holder is not None and str(holder) != principal.id:
                raise Denied(f"{principal.id} may not list leases of {holder!r}")
            holder = principal.id
        leases = self.leases.list(holder=holder,
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
            ttl_s=int(data["ttl_s"]) if data.get("ttl_s") else None,
        )
        return request.to_dict()

    async def _approval_status(self, data, src, principal=None):
        request = self.approvals.poll(
            str(data.get("request_id") or ""),
            principal=self._owner_principal(principal))
        return request.to_dict()

    async def _approval_use(self, data, src, principal=None):
        ok = self.approvals.use(
            str(data.get("grant_id") or ""),
            principal=self._owner_principal(principal))
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
                # A unit's token remembers which agent (and task) it belongs
                # to, so later approval requests are keyed by the agent and
                # plain workers are scope-limited to that task. Attach flows
                # only learn both here.
                self.rpc_auth.bind_agent(unit_id, agent.id, agent.task_id)
                return agent.to_dict()
        agent = self.supervisor.register(
            role=role,
            project_id=data.get("project_id"),
            unit_id=unit_id,
            task_id=data.get("task_id"),
            model=str(data.get("model") or ""),
        )
        self.rpc_auth.bind_agent(unit_id, agent.id, agent.task_id)
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
        self.rpc_auth.revoke(work_id)
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

    async def attach_project(self, name: str, path: str, *, goal: str = "",
                             allow_writes: bool = False,
                             created_by: str = "human:operator") -> dict:
        """Attach an existing local directory as a project.

        The directory is never created, copied or deleted by the system. Agents
        are read-only unless ``allow_writes`` is explicitly true; even then
        they must still pass normal capability approvals.
        """
        target = self._validate_attach_path(path)
        for existing in self.projects.list():
            try:
                same = Path(existing.workspace).resolve() == target
            except (OSError, RuntimeError):
                same = False
            if same and existing.status != "archived":
                card = self.project_card(existing.id)
                card["attached_now"] = False
                card["existing"] = True
                return card
        mode = "attached-rw" if allow_writes else "attached-ro"
        project = self.projects.create(
            name or target.name, goal, created_by=created_by,
            workspace=target, workspace_mode=mode)
        self.eventlog.append("project.attach.completed", {
            "project_id": project.id,
            "name": project.name,
            "path": project.workspace,
            "workspace_mode": mode,
            "by": created_by,
        }, source="operator", project_id=project.id)
        card = self.project_card(project.id)
        card["attached_now"] = True
        card["existing"] = False
        return card

    async def set_project_access(self, project_id: str, *, allow_writes: bool,
                                 by: str = "human:operator") -> dict:
        mode = "attached-rw" if allow_writes else "attached-ro"
        project = self.projects.set_workspace_mode(project_id, mode)
        self.eventlog.append("project.access.changed", {
            "project_id": project.id,
            "workspace_mode": mode,
            "by": by,
        }, source="operator", project_id=project.id)
        return self.project_card(project.id)

    def _validate_attach_path(self, path: str) -> Path:
        raw = str(path or "").strip()
        if not raw:
            raise BadRequest("path required")
        target = Path(raw).expanduser()
        try:
            target = target.resolve()
        except (OSError, RuntimeError) as exc:
            raise BadRequest(f"invalid path: {exc}") from exc
        if not target.exists():
            raise ValidationError(f"目录不存在：{target}")
        if not target.is_dir():
            raise ValidationError(f"不是目录：{target}")
        if target == Path(target.anchor):
            raise ValidationError("refusing to attach the filesystem root")
        state = self.paths.state_dir.resolve()
        if target == state or state in target.parents:
            raise ValidationError("refusing to attach the StackFlowOS state directory")
        denied = ("/etc", "/root", "/proc", "/sys", "/dev", "/boot",
                  "/bin", "/sbin", "/lib", "/lib64", "/usr")
        for prefix in denied:
            denied_path = Path(prefix)
            if target == denied_path or denied_path in target.parents:
                raise ValidationError(f"refusing to attach system directory: {prefix}")
        return target

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
        external_preserved = bool(record.external)
        if purge_workspace and external_preserved:
            purge_error = "external workspace is never deleted"
        elif purge_workspace:
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
            "external_workspace_preserved": external_preserved,
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
            "external_workspace_preserved": external_preserved,
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
        if getattr(project, "external", False):
            raise ValidationError("refusing to purge an attached external workspace")
        projects_root = self.paths.projects_dir.resolve()
        # M9: validate the record's id (it may come from old/hostile state)
        # before joining it into the projects root, then keep the explicit
        # containment check on the directory we are about to delete.
        project_dir = project_workspace(projects_root, project.id).parent
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
        if work_id == "sys.0":
            raise BadRequest("sys.0 is the hub itself; stop the hub process instead")
        if work_id in {unit.get("unit_id") for unit in self.logical_units()}:
            raise BadRequest(
                "in-process service units cannot be killed independently; "
                "restart the hub or disable the service")
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

    async def operator_tool(self, tool: str, args: dict) -> dict:
        """Run one assistant/operator tool directly (audited, no approval queue)."""
        return await self.operator.run(tool, args)

    async def assistant_tool(self, tool: str, args: dict) -> dict:
        """Compatibility alias for the operator tool entry point."""
        return await self.operator_tool(tool, args)

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

    def health(self) -> dict:
        transport_ok = bool(getattr(self.transport, "running", False))
        return {
            "status": "ok" if transport_ok else "degraded",
            "http": "ok",
            "transport": "ok" if transport_ok else "down",
            "version": self.version,
            "uptime_s": int(time.time() - self.started_at / 1000),
            "units": self.stats().get("units", {}),
        }

    def stats(self) -> dict:
        cards = self.unit_cards()
        by_status: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        by_tier: dict[str, int] = {}
        online = 0
        for card in cards:
            status = str(card.get("status") or "")
            state = str(card.get("state") or status)
            by_status[status] = by_status.get(status, 0) + 1
            by_kind[str(card.get("kind") or "")] = (
                by_kind.get(str(card.get("kind") or ""), 0) + 1)
            tier = str(card.get("tier") or "worker")
            by_tier[tier] = by_tier.get(tier, 0) + 1
            if state in ("ready", "degraded", "online", "suspect", "draining"):
                online += 1
        units = {
            "total": len(cards),
            "online": online,
            "by_status": dict(sorted(by_status.items())),
            "by_kind": dict(sorted(by_kind.items())),
            "by_tier": dict(sorted(by_tier.items())),
        }
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

    def sys_unit(self) -> dict:
        """The hub itself is a unit: sys.0 with addresses and a lifecycle."""
        now = now_ms()
        return {
            "work_id": "sys.0",
            "unit_id": "sys.0",
            "kind": "sys",
            "role": "hub",
            "tier": "system",
            "label": "hub · 系统单元",
            "project_id": None,
            "pid": os.getpid(),
            "host": self.config.system.host_label,
            "transport": self.config.transport.mode,
            "rpc_address": self.sys_addresses.rpc,
            "events_address": self.sys_addresses.events,
            "status": "online",
            "state": "ready",
            "reason": None,
            "abilities": [
                {"name": "sys.rpc", "scope": "system"},
                {"name": "sys.events", "scope": "system"},
                {"name": "web.http", "scope": "console"},
            ],
            "hwinfo": {},
            "started_at": self.started_at,
            "last_seen": now,
            "restart_count": 0,
            "metadata": {"role": "hub", "web_port": self.config.web.port,
                         "description": "hub 进程：注册表/监管/配置/HTTP/NNG 入口"},
            "runtime": {
                "mode": "sys",
                "host_unit": None,
                "host": self.config.system.host_label,
                "pid": os.getpid(),
                "module": "stackflowos.app:App",
                "started_at": self.started_at,
            },
            "endpoints": [
                {"name": "rpc", "transport": "nng-req",
                 "address": self.sys_addresses.rpc},
                {"name": "events", "transport": "nng-pub",
                 "address": self.sys_addresses.events},
                {"name": "http", "transport": "http",
                 "address": f"http://127.0.0.1:{self.config.web.port}"},
            ],
            "ref": "unit://sys.0",
            "critical": True,
            "requires": [],
            "actions": ["probe"],
            "lease": {"type": "host", "host_unit": None, "interval_s": 5,
                      "renewed_at": now},
            "provides": {},
        }

    def _logical_service(self, unit_id: str, kind: str, label: str,
                         abilities: list[str], *, tier: str = "service",
                         critical: bool = False, state: str = "ready",
                         reason: str | None = None,
                         requires: tuple[str, ...] = (),
                         provides: dict | None = None,
                         endpoints: list[dict] | None = None,
                         metadata: dict | None = None) -> dict:
        """A hub-resident service unit (no pid, no fake socket address)."""
        now = now_ms()
        status = {"ready": "online", "degraded": "degraded",
                  "disabled": "disabled", "failed": "failed",
                  "waiting": "waiting"}.get(state, "online")
        return {
            "work_id": unit_id,
            "unit_id": unit_id,
            "kind": kind,
            "role": None,
            "tier": tier,
            "label": label,
            "project_id": None,
            "pid": None,
            "host": self.config.system.host_label,
            "transport": "inproc",
            "rpc_address": None,
            "events_address": None,
            "status": status,
            "state": state,
            "reason": reason,
            "abilities": [{"name": name, "scope": "system"}
                          for name in abilities],
            "hwinfo": {},
            "started_at": self.started_at,
            "last_seen": now,
            "restart_count": 0,
            "metadata": metadata or {},
            "runtime": {
                "mode": "in_process",
                "host_unit": "sys.0",
                "host": self.config.system.host_label,
                "pid": None,
                "module": f"stackflowos.app:{unit_id}",
                "started_at": self.started_at,
            },
            "endpoints": endpoints or [
                {"name": "inproc", "transport": "inproc", "address": unit_id}],
            "ref": f"unit://{unit_id}",
            "critical": critical,
            "requires": list(requires),
            "actions": ["probe"],
            "lease": {"type": "host", "host_unit": "sys.0",
                      "interval_s": 5, "renewed_at": now},
            "provides": provides or {},
        }

    def logical_units(self) -> list[dict]:
        """The hub's real services exposed as in-process units.

        This deliberately does not invent a process/PID for them: runtime.mode
        and endpoints carry the truth, while the old flat fields stay null.
        """
        from .agents.tools import TOOLS

        units: list[dict] = []
        events_ok = os.access(self.paths.events_dir, os.W_OK)

        units.append(self._logical_service(
            "store.eventlog", "store", "事件日志",
            ["eventlog.append", "eventlog.query"], tier="system",
            critical=True,
            state="ready" if events_ok else "degraded",
            reason=None if events_ok else "events 目录不可写",
            provides={"events": self.eventlog.last_seq},
            metadata={"description": "append-only JSONL 事件流"},
        ))
        units.append(self._logical_service(
            "store.project", "store", "项目存储",
            ["project.read", "project.write"], tier="system",
            critical=True, provides={"projects": len(self.projects.list())},
            metadata={"description": "项目/任务/里程碑"},
        ))
        units.append(self._logical_service(
            "store.memory", "store", "记忆存储",
            ["memory.get", "memory.set"], tier="system",
            provides={"projects": len(self.memory.data)},
            metadata={"description": "按项目隔离的 key/value 记忆"},
        ))
        units.append(self._logical_service(
            "scheduler.0", "scheduler", "调度器",
            ["task.dispatch", "lease.renew"], tier="system",
            metadata={"description": "任务调度与租约续期"},
        ))
        units.append(self._logical_service(
            "gateway.0", "gateway", "Web/SSE 网关",
            ["http", "sse"], tier="system",
            endpoints=[{"name": "http", "transport": "http",
                        "address": f"http://127.0.0.1:{self.config.web.port}"}],
            metadata={"description": "HTTP API、SSE 与静态 UI"},
        ))
        units.append(self._logical_service(
            "context.0", "context", "上下文容器",
            ["context.inject", "plugin.mount"], tier="system",
            metadata={"description": "Cordis-style Context / effect / disposer"},
        ))
        units.append(self._logical_service(
            "approval.0", "approval", "审批中心",
            ["policy.evaluate", "approval.request", "approval.decide"],
            critical=True,
            provides={"pending": self.approvals.pending_count()},
            requires=("store.eventlog",),
            metadata={"description": "能力→风险→策略→审批→授予→审计"},
        ))
        provider = getattr(self, "provider", None)
        llm_state = "ready"
        llm_reason = None
        llm_model = ""
        llm_name = ""
        if provider is None:
            llm_state, llm_reason = "disabled", "未配置 LLM provider"
        else:
            llm_name = str(getattr(provider, "name", "") or "")
            llm_model = str(getattr(provider, "model", "") or "")
            if llm_name == "openai" and not getattr(provider, "api_key", ""):
                llm_state, llm_reason = "degraded", "未配置 API Key"
        units.append(self._logical_service(
            "llm.0", "llm", "LLM 服务",
            ["llm.chat", "llm.models"], state=llm_state, reason=llm_reason,
            endpoints=[{"name": "remote", "transport": "http",
                        "address": str(getattr(provider, "base_url", "") or "")}]
            if provider is not None else [],
            provides={"model": llm_model, "provider": llm_name},
            metadata={"description": "OpenAI 兼容 provider（无状态，可多实例）",
                      "model": llm_model, "provider": llm_name},
        ))
        units.append(self._logical_service(
            "tool.0", "tool", "工具注册表",
            ["tool.catalog", "tool.dispatch"], provides={"tools": len(TOOLS)},
            requires=("approval.0",),
            metadata={"description": "内置工具目录、schema 校验与审批挂钩"},
        ))
        units.append(self._logical_service(
            "skill.0", "skill", "技能注册表",
            ["skill.list", "skill.inject"], state="disabled",
            reason="SkillRegistry 尚未实现（规划 Phase 2）",
            provides={"skills": 0},
            metadata={"description": "skill manifest 目录；单个 skill 不是单元"},
        ))
        units.append(self._logical_service(
            "plugin.0", "plugin", "插件宿主",
            ["plugin.load", "plugin.unload"], provides={"plugins": 0},
            requires=("context.0", "tool.0"),
            metadata={"description": "插件加载器；每个外部插件是 plugin.N"},
        ))
        units.append(self._logical_service(
            "mcp.0", "mcp", "MCP 宿主",
            ["mcp.connect", "mcp.list_tools", "mcp.call_tool"],
            provides={"servers": 0},
            requires=("approval.0", "tool.0"),
            metadata={"description": "MCP server 连接器；每个已配置 server 将登记为 mcp.<name>（协议接入 Phase 2）"},
        ))
        return units

    def unit_cards(self) -> list[dict]:
        """sys + hub services + every registered worker (the system's ``ps``)."""
        return ([self.sys_unit()] + self.logical_units()
                + [u.to_dict() for u in self.registry.list()])

    def snapshot(self) -> dict:
        try:
            projects = [self.project_card(p.id) for p in self.projects.list()]
        except Exception:
            self.log.exception("project snapshot failed")
            projects = []
        agents = [a.to_dict() for a in self.supervisor.list()]
        units = self.unit_cards()
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

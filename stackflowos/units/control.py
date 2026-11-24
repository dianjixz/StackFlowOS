"""Unit control plane: probes, hysteresis, actions and unit-to-unit calls.

This module turns ``/api/units`` from a static list into a live view:

* :class:`UnitStateStore` keeps one state machine per unit with the planned
  hysteresis (2 consecutive failures -> ``degraded``, 3 -> ``failed``, 2
  consecutive successes -> ``ready``) plus manual ``disabled``/``draining``
  overlays;
* :class:`HealthTicker` is one asyncio task probing every non-worker unit every
  5s with a per-unit timeout; every transition is emitted as
  ``unit.state_changed`` (EventLog + kernel bus);
* actions ``probe/restart/disable/enable/drain/undrain`` are only allowed when
  the unit's descriptor declares them; in-process services use rebuild hooks
  registered by their owning extension and return 409 when no hook exists;
* :class:`UnitClient` implements ``call("unit://<id>", method, payload)`` with
  in-process handlers (with ``approval.0`` built in) and an NNG placeholder for
  subprocess/remote units, so callers do not change when a unit moves out of
  process.

The ticker probes *core* services with real checks (event-log writability,
project/memory reads, provider configuration, transport liveness) -- not with a
constant ``ready``.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from ..errors import BadRequest, Conflict, NotFound, SfoError, Unavailable
from ..l2.eventlog import emit_both
from ..protocol import now_ms
from ..util.logging import get_logger

ONLINE_STATES = frozenset({"ready", "degraded", "online", "suspect", "draining"})
PROBLEM_STATES = frozenset({"degraded", "failed", "unreachable"})
_STATUS_BY_STATE = {
    "ready": "online", "degraded": "degraded", "failed": "failed",
    "disabled": "disabled", "draining": "draining", "starting": "starting",
    "waiting": "waiting", "unreachable": "unreachable", "stopped": "offline",
    "unknown": "unknown",
}


def parse_unit_ref(ref: str) -> str:
    """Resolve ``unit://<id>`` (or a bare id) to a unit id."""
    text = str(ref or "").strip()
    if text.startswith("unit://"):
        text = text[len("unit://"):]
    if not text or "/" in text or text.startswith("unit:"):
        raise BadRequest(f"invalid unit ref: {ref!r}",
                         detail={"ref": ref})
    return text


@dataclass
class UnitRecord:
    unit_id: str
    state: str = "unknown"
    reason: str | None = None
    since: int = field(default_factory=now_ms)
    health: dict = field(default_factory=dict)
    failures: int = 0
    successes: int = 0
    disabled: bool = False
    draining: bool = False
    prev_state: str | None = None
    source: str = "init"

    def to_dict(self) -> dict:
        return {
            "unit_id": self.unit_id, "state": self.state,
            "reason": self.reason, "since": self.since,
            "health": dict(self.health), "disabled": self.disabled,
            "draining": self.draining, "source": self.source,
            "failures": self.failures, "successes": self.successes,
        }


class UnitStateStore:
    """Thread-safe per-unit state machine with probe hysteresis."""

    def __init__(self, *, degrade_after: int = 2, fail_after: int = 3,
                 recover_after: int = 2) -> None:
        self.degrade_after = max(1, int(degrade_after))
        self.fail_after = max(self.degrade_after + 1, int(fail_after))
        self.recover_after = max(1, int(recover_after))
        self._records: dict[str, UnitRecord] = {}
        self._lock = threading.RLock()

    # -- lookup -----------------------------------------------------------
    def get(self, unit_id: str) -> UnitRecord | None:
        with self._lock:
            return self._records.get(str(unit_id))

    def states(self) -> dict[str, str]:
        with self._lock:
            return {unit_id: record.state
                    for unit_id, record in self._records.items()}

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [record.to_dict() for record in self._records.values()]

    def ensure(self, unit_id: str, state: str = "unknown",
               reason: str | None = None, *, source: str = "init") -> UnitRecord:
        with self._lock:
            record = self._records.get(str(unit_id))
            if record is None:
                record = UnitRecord(unit_id=str(unit_id), state=str(state),
                                    reason=reason, source=source)
                self._records[record.unit_id] = record
            return record

    # -- transitions ------------------------------------------------------
    @staticmethod
    def _change(record: UnitRecord, state: str, reason: str | None,
                source: str) -> dict | None:
        state = str(state)
        if record.state == state and record.reason == reason:
            return None
        previous = record.state
        record.state = state
        record.reason = reason
        record.since = now_ms()
        record.source = source
        return {"unit_id": record.unit_id, "from": previous, "to": state,
                "reason": reason, "at": record.since, "source": source}

    def set_state(self, unit_id: str, state: str, reason: str | None = None,
                  *, source: str = "external") -> dict | None:
        """Force a state (extension truth/manual); resets hysteresis counters."""
        with self._lock:
            record = self.ensure(unit_id)
            if record.disabled and str(state) != "disabled":
                return None
            if record.draining and str(state) not in ("draining", "disabled"):
                return None
            record.failures = 0
            record.successes = 0
            return self._change(record, state, reason, source)

    def observe(self, unit_id: str, ok: bool, *,
                checks: list | None = None, reason: str | None = None,
                sticky_state: str | None = None,
                detail: str | None = None,
                source: str = "probe") -> dict | None:
        """Apply one probe result through the hysteresis state machine."""
        with self._lock:
            record = self.ensure(unit_id)
            record.health = {
                "ok": bool(ok),
                "checked_at": now_ms(),
                "checks": list(checks or []),
            }
            if detail:
                record.health["detail"] = str(detail)
            record.health["failures"] = record.failures
            if record.disabled or record.draining:
                return None
            if ok:
                record.failures = 0
                if sticky_state in ("degraded", "failed"):
                    record.successes = 0
                    return self._change(record, str(sticky_state),
                                        reason or record.reason, source)
                if record.state in ("unknown", "starting"):
                    return self._change(record, "ready", None, source)
                if record.state in ("degraded", "failed"):
                    record.successes += 1
                    if record.successes >= self.recover_after:
                        return self._change(record, "ready", None, source)
                    return None
                record.successes = min(self.successes_cap(),
                                       record.successes + 1)
                return None
            record.successes = 0
            record.failures += 1
            record.health["failures"] = record.failures
            if record.failures >= self.fail_after:
                return self._change(record, "failed",
                                    reason or "probe failed", source)
            if record.failures >= self.degrade_after \
                    and record.state not in ("degraded", "failed"):
                return self._change(record, "degraded",
                                    reason or "probe failed", source)
            return None

    def successes_cap(self) -> int:
        return max(self.recover_after, 2)

    # -- manual overlays --------------------------------------------------
    def disable(self, unit_id: str, reason: str | None = None) -> dict | None:
        with self._lock:
            record = self.ensure(unit_id)
            if not record.disabled:
                record.prev_state = record.state
                record.disabled = True
                record.draining = False
                record.failures = record.successes = 0
                return self._change(record, "disabled",
                                    reason or "disabled by operator",
                                    "manual")
            return None

    def enable(self, unit_id: str) -> dict | None:
        with self._lock:
            record = self.ensure(unit_id)
            if not record.disabled:
                return None
            record.disabled = False
            record.failures = record.successes = 0
            return self._change(record, "unknown", None, "manual")

    def drain(self, unit_id: str, reason: str | None = None) -> dict | None:
        with self._lock:
            record = self.ensure(unit_id)
            if record.draining or record.disabled:
                return None
            record.prev_state = record.state
            record.draining = True
            return self._change(record, "draining",
                                reason or "draining by operator", "manual")

    def undrain(self, unit_id: str) -> dict | None:
        with self._lock:
            record = self.ensure(unit_id)
            if not record.draining:
                return None
            record.draining = False
            state = record.prev_state or "unknown"
            record.prev_state = None
            return self._change(record, state, None, "manual")


class HealthTicker:
    """One asyncio task applying probe results to :class:`UnitStateStore`."""

    def __init__(self, controller: "UnitController", *,
                 interval_s: float = 5.0, probe_timeout_s: float = 2.0,
                 initial: bool = True) -> None:
        self.controller = controller
        self.interval_s = max(0.1, float(interval_s))
        self.probe_timeout_s = max(0.05, float(probe_timeout_s))
        self.initial = bool(initial)
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._stopped = False
        if self.initial:
            await self.tick()
        self._task = asyncio.create_task(self._loop(), name="unit-health-ticker")

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(self.interval_s)
            except asyncio.CancelledError:
                raise
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - a tick must never die
                self.controller.log.exception("unit health tick failed")

    async def tick(self) -> list[dict]:
        """Probe every core/extension unit once; return state transitions."""
        async with self._lock:
            transitions: list[dict] = []
            for unit in self.controller.probe_units():
                transition = await self.probe_unit(unit)
                if transition:
                    transitions.append(transition)
            return transitions

    async def probe_unit(self, unit: dict) -> dict | None:
        unit_id = str(unit.get("unit_id") or unit.get("work_id") or "")
        if not unit_id:
            return None
        record = self.controller.store.get(unit_id)
        if record is not None and (record.disabled or record.draining):
            return None
        result = await self.controller.run_probe(unit_id, unit)
        if result is None:
            return None
        ok = bool(result.get("ok", True))
        checks = result.get("checks")
        if not isinstance(checks, list):
            checks = []
        transition = self.controller.store.observe(
            unit_id, ok, checks=checks,
            reason=str(result.get("reason") or "") or None,
            sticky_state=result.get("sticky_state"),
            detail=str(result.get("detail") or "") or None)
        if transition:
            self.controller.emit_transition(transition)
        return transition


class UnitController:
    """The ``units/control`` extension object."""

    name = "unit_control"

    def __init__(self, app: Any, *, interval_s: float = 5.0,
                 probe_timeout_s: float = 2.0) -> None:
        self.app = app
        self.log = get_logger("units.control")
        self.store = UnitStateStore()
        self.interval_s = interval_s
        self.probe_timeout_s = probe_timeout_s
        self.ticker = HealthTicker(self, interval_s=interval_s,
                                   probe_timeout_s=probe_timeout_s)
        self._probes: dict[str, Callable[[], Any]] = {}
        self._restart_hooks: dict[str, Callable[[], Any]] = {}
        self._disable_hooks: dict[str, Callable[[], Any]] = {}
        self._enable_hooks: dict[str, Callable[[], Any]] = {}
        self._state_events: deque[dict] = deque(maxlen=500)
        self.unit_client = UnitClient(app)
        app.unit_client = self.unit_client

    # -- registration -----------------------------------------------------
    def register_provider(self, unit_id: str, *, probe: Callable | None = None,
                          restart: Callable | None = None,
                          disable: Callable | None = None,
                          enable: Callable | None = None) -> dict:
        """Register live hooks for one unit (extension-owned units)."""
        unit_id = str(unit_id)
        if probe is not None:
            self._probes[unit_id] = probe
        if restart is not None:
            self._restart_hooks[unit_id] = restart
        if disable is not None:
            self._disable_hooks[unit_id] = disable
        if enable is not None:
            self._enable_hooks[unit_id] = enable
        return {"unit_id": unit_id,
                "probe": probe is not None, "restart": restart is not None,
                "disable": disable is not None, "enable": enable is not None}

    def state_of(self, unit_id: str) -> str | None:
        record = self.store.get(unit_id)
        return record.state if record is not None else None

    def states(self) -> dict[str, str]:
        return self.store.states()

    def sync_state(self, unit_id: str, state: str,
                   reason: str | None = None) -> None:
        """Extensions report their live truth (scan result, connection...)."""
        transition = self.store.set_state(unit_id, state, reason,
                                          source="extension")
        if transition:
            self.emit_transition(transition)

    def overlay(self, descriptor: dict) -> dict:
        """Return a descriptor with control-plane state applied."""
        unit_id = str(descriptor.get("unit_id") or descriptor.get("work_id") or "")
        record = self.store.get(unit_id)
        if record is None:
            return descriptor
        merged = dict(descriptor)
        merged["state"] = record.state
        merged["status"] = _STATUS_BY_STATE.get(record.state,
                                                descriptor.get("status"))
        merged["reason"] = record.reason
        merged["since"] = record.since
        merged["health"] = dict(record.health)
        merged["disabled"] = record.disabled
        merged["draining"] = record.draining
        return merged

    # -- probing ----------------------------------------------------------
    def probe_units(self) -> list[dict]:
        units: list[dict] = []
        try:
            cards = self.app.unit_cards()
        except Exception:  # pragma: no cover - a broken card is one unit
            self.log.exception("cannot enumerate units for probing")
            return units
        for unit in cards:
            if str(unit.get("tier") or "") == "worker":
                continue
            units.append(unit)
        return units

    async def run_probe(self, unit_id: str, unit: dict) -> dict | None:
        """Run one probe with a hard timeout; never raises."""
        probe = self._probes.get(unit_id)
        try:
            if probe is not None:
                value = await self._call_probe(probe)
            else:
                value = self._builtin_probe(unit_id, unit)
                if value is None:
                    return None
        except asyncio.TimeoutError:
            return {"ok": False, "reason": "probe timeout",
                    "checks": [{"name": "probe", "ok": False,
                                "detail": "timeout"}]}
        except Exception as exc:  # noqa: BLE001 - probe failures are state
            return {"ok": False, "reason": f"probe error: {type(exc).__name__}",
                    "checks": [{"name": "probe", "ok": False,
                                "detail": type(exc).__name__}]}
        if isinstance(value, bool):
            return {"ok": value}
        if not isinstance(value, dict):
            return {"ok": True, "detail": str(value)[:200]}
        return value

    async def _call_probe(self, probe: Callable) -> Any:
        if inspect.iscoroutinefunction(probe):
            return await asyncio.wait_for(probe(),
                                          timeout=self.probe_timeout_s)
        call = probe
        value = await asyncio.wait_for(
            asyncio.to_thread(call), timeout=self.probe_timeout_s)
        if inspect.isawaitable(value):
            return await asyncio.wait_for(value, timeout=self.probe_timeout_s)
        return value

    def _builtin_probe(self, unit_id: str, unit: dict) -> dict | None:
        app = self.app
        if unit_id == "sys.0":
            running = bool(getattr(getattr(app, "transport", None),
                                   "running", False))
            return {"ok": running,
                    "checks": [{"name": "transport", "ok": running}],
                    "reason": None if running else "transport is not running"}
        if unit_id == "store.eventlog":
            events_dir = getattr(getattr(app, "paths", None), "events_dir", None)
            writable = bool(events_dir is not None and os.access(events_dir,
                                                                 os.W_OK))
            seq = getattr(getattr(app, "eventlog", None), "last_seq", None)
            return {"ok": writable,
                    "checks": [{"name": "writable", "ok": writable,
                                "detail": str(events_dir)},
                               {"name": "last_seq", "ok": seq is not None,
                                "detail": str(seq)}],
                    "reason": None if writable else "events dir is not writable"}
        if unit_id == "store.project":
            try:
                projects = app.projects.list()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "reason": f"project store: {exc}"}
            return {"ok": True, "checks": [{"name": "list", "ok": True,
                                            "detail": f"{len(projects)} project(s)"}]}
        if unit_id == "store.memory":
            data = getattr(getattr(app, "memory", None), "data", None)
            ok = isinstance(data, dict)
            return {"ok": ok,
                    "checks": [{"name": "data", "ok": ok,
                                "detail": f"{len(data) if ok else 0} project(s)"}]}
        if unit_id == "scheduler.0":
            scheduler = getattr(app, "scheduler", None)
            leases = getattr(app, "leases", None)
            ok = scheduler is not None and leases is not None
            return {"ok": ok, "checks": [{"name": "instance", "ok": ok}]}
        if unit_id == "gateway.0":
            ctx = getattr(app, "ctx", None)
            ok = ctx is not None and not getattr(ctx, "disposed", True)
            return {"ok": ok,
                    "checks": [{"name": "context", "ok": ok,
                                "detail": "hub in-process gateway"}]}
        if unit_id == "context.0":
            ctx = getattr(app, "ctx", None)
            ok = ctx is not None and not getattr(ctx, "disposed", True)
            children = len(getattr(ctx, "_children", []) or [])
            return {"ok": ok, "checks": [{"name": "alive", "ok": ok,
                                          "detail": f"{children} child context(s)"}]}
        if unit_id == "approval.0":
            try:
                pending = app.approvals.pending_count()
                policy_ok = app.policy is not None
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "reason": f"approval broker: {exc}"}
            return {"ok": policy_ok,
                    "checks": [{"name": "pending", "ok": True,
                                "detail": f"{pending} pending"},
                               {"name": "policy", "ok": policy_ok}]}
        if unit_id == "llm.0":
            provider = getattr(app, "provider", None)
            if provider is None:
                return {"ok": False, "reason": "no llm provider configured"}
            name = str(getattr(provider, "name", "") or "")
            if name == "openai" and not getattr(provider, "api_key", ""):
                return {"ok": False,
                        "checks": [{"name": "api_key", "ok": False}],
                        "reason": "openai provider has no API key"}
            return {"ok": True,
                    "checks": [{"name": "provider", "ok": True,
                                "detail": name}]}
        if unit_id == "tool.0":
            from ..agents.tools import TOOLS
            ok = bool(TOOLS)
            return {"ok": ok, "checks": [{"name": "catalogue", "ok": ok,
                                          "detail": f"{len(TOOLS)} tool(s)"}]}
        return None

    # -- actions ----------------------------------------------------------
    def _descriptor(self, unit_id: str) -> dict:
        try:
            for unit in self.app.unit_cards():
                if str(unit.get("unit_id") or unit.get("work_id")) == unit_id:
                    descriptor = dict(unit)
                    # Worker descriptors come from UnitInfo: they carry neither
                    # ``tier`` nor an action list.  Normalise both, and expose
                    # the actions the supervisor supports so
                    # /api/units/{id}/actions can restart a worker.
                    if not descriptor.get("tier"):
                        descriptor["tier"] = "worker"
                    if str(descriptor.get("tier") or "") == "worker" and \
                            not descriptor.get("actions"):
                        descriptor["actions"] = ["restart", "drain", "undrain"]
                    return self.overlay(descriptor)
        except Exception:  # pragma: no cover
            pass
        raise NotFound(f"unknown unit: {unit_id!r}",
                       detail={"unit_id": unit_id})

    def _declared_actions(self, descriptor: dict) -> list[str]:
        actions = descriptor.get("actions")
        if not isinstance(actions, list):
            return []
        return [str(action) for action in actions]

    async def act(self, unit_id: str, action: str) -> dict:
        action = str(action or "").strip()
        descriptor = self._descriptor(unit_id)
        declared = self._declared_actions(descriptor)
        if action not in declared:
            raise Conflict(
                f"unit {unit_id!r} does not declare action {action!r}",
                detail={"unit_id": unit_id, "actions": declared})
        if action == "probe":
            result = await self.probe_now(unit_id)
            return {"action": action, "unit_id": unit_id,
                    "result": result, "unit": self._descriptor(unit_id)}
        if action == "restart":
            return await self.restart(unit_id)
        if action == "disable":
            return await self.disable(unit_id)
        if action == "enable":
            return await self.enable(unit_id)
        if action == "drain":
            transition = self.store.drain(unit_id)
            if transition:
                self.emit_transition(transition)
            return {"action": action, "unit_id": unit_id,
                    "unit": self._descriptor(unit_id)}
        if action == "undrain":
            transition = self.store.undrain(unit_id)
            if transition:
                self.emit_transition(transition)
            descriptor = self._descriptor(unit_id)
            await self.probe_now(unit_id)
            return {"action": action, "unit_id": unit_id,
                    "unit": self._descriptor(unit_id)}
        raise BadRequest(f"unknown action: {action!r}",
                         detail={"actions": declared})

    async def restart(self, unit_id: str) -> dict:
        descriptor = self._descriptor(unit_id)
        tier = str(descriptor.get("tier") or "")
        if tier == "worker":
            registry = getattr(self.app, "registry", None)
            supervisor = getattr(self.app, "supervisor", None)
            if registry is None or supervisor is None:
                raise Conflict("worker supervision is unavailable")
            try:
                unit = registry.get(unit_id)
            except SfoError as exc:
                raise Conflict(f"worker {unit_id!r} is not registered: "
                               f"{exc.message}") from exc
            try:
                ok = await supervisor.restart(unit, 1)
            except Exception as exc:  # noqa: BLE001
                raise Conflict(f"worker restart failed: {exc}") from exc
            if not ok:
                raise Conflict(f"supervisor refused to restart {unit_id!r}")
            return {"action": "restart", "unit_id": unit_id,
                    "restarted": True, "unit": self._descriptor(unit_id)}
        hook = self._restart_hooks.get(unit_id)
        if hook is None:
            raise Conflict(
                f"unit {unit_id!r} has no rebuild hook; restart is not "
                "possible for this in-process service (disable/probe instead)",
                detail={"unit_id": unit_id, "actions":
                        self._declared_actions(descriptor)})
        result = await self._run_hook(hook)
        await self.probe_now(unit_id)
        return {"action": "restart", "unit_id": unit_id, "restarted": True,
                "result": result, "unit": self._descriptor(unit_id)}

    async def disable(self, unit_id: str) -> dict:
        descriptor = self._descriptor(unit_id)
        hook = self._disable_hooks.get(unit_id)
        if hook is None:
            raise Conflict(
                f"unit {unit_id!r} has no disable hook; it cannot be disabled "
                "independently", detail={"unit_id": unit_id})
        await self._run_hook(hook)
        transition = self.store.disable(unit_id)
        if transition:
            self.emit_transition(transition)
        return {"action": "disable", "unit_id": unit_id,
                "unit": self._descriptor(unit_id)}

    async def enable(self, unit_id: str) -> dict:
        hook = self._enable_hooks.get(unit_id)
        if hook is None:
            raise Conflict(
                f"unit {unit_id!r} has no enable hook; it cannot be enabled "
                "independently", detail={"unit_id": unit_id})
        transition = self.store.enable(unit_id)
        if transition:
            self.emit_transition(transition)
        await self._run_hook(hook)
        await self.probe_now(unit_id)
        return {"action": "enable", "unit_id": unit_id,
                "unit": self._descriptor(unit_id)}

    async def _run_hook(self, hook: Callable) -> Any:
        try:
            value = hook()
        except Exception as exc:  # noqa: BLE001
            raise Conflict(f"unit hook failed: {exc}") from exc
        if inspect.isawaitable(value):
            try:
                return await asyncio.wait_for(value,
                                              timeout=max(30.0,
                                                          self.probe_timeout_s * 10))
            except Exception as exc:  # noqa: BLE001
                raise Conflict(f"unit hook failed: {exc}") from exc
        return value

    async def probe_now(self, unit_id: str) -> dict:
        unit = self._descriptor(unit_id)
        if str(unit.get("tier") or "") == "worker":
            raise Conflict("worker liveness comes from its heartbeat, not a "
                           "probe", detail={"unit_id": unit_id})
        transition = await self.ticker.probe_unit(unit)
        return {"unit_id": unit_id, "transition": transition,
                "unit": self._descriptor(unit_id)}

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        try:
            cards = self.app.unit_cards()
        except Exception:  # pragma: no cover
            cards = []
        for unit in cards:
            if str(unit.get("tier") or "") == "worker":
                continue
            unit_id = str(unit.get("unit_id") or unit.get("work_id") or "")
            if not unit_id:
                continue
            state = str(unit.get("state") or unit.get("status") or "unknown")
            self.store.ensure(unit_id, state, unit.get("reason"))
        await self.ticker.start()

    async def stop(self) -> None:
        await self.ticker.stop()

    # -- events -----------------------------------------------------------
    def emit_transition(self, transition: dict) -> None:
        record = dict(transition)
        record.setdefault("at", now_ms())
        self._state_events.append(record)
        ctx = getattr(self.app, "ctx", None)
        eventlog = getattr(self.app, "eventlog", None)
        if ctx is None or eventlog is None:
            return
        try:
            emit_both(ctx, eventlog, "unit.state_changed", record)
        except Exception:  # pragma: no cover - events never break probes
            self.log.debug("unit.state_changed emit failed", exc_info=True)

    def recent_events(self, unit_id: str, limit: int = 20) -> list[dict]:
        rows: list[dict] = []
        eventlog = getattr(self.app, "eventlog", None)
        if eventlog is not None:
            try:
                records = eventlog.query(types=["unit.state_changed"],
                                         limit=500)
            except Exception:  # pragma: no cover
                records = []
            for record in records:
                data = record.to_dict().get("data") or {}
                if str(data.get("unit_id")) == str(unit_id):
                    rows.append(record.to_dict())
        if not rows:
            rows = [dict(item) for item in self._state_events
                    if str(item.get("unit_id")) == str(unit_id)]
        return rows[-max(1, int(limit)):]

    # -- units / summary --------------------------------------------------
    def units(self) -> list[dict]:
        # Each provider extension contributes its own descriptors and applies
        # UnitController.overlay(); this extension must not call
        # app.logical_units() from here (recursion guard is deliberate).
        return []

    def capabilities(self, kind: str | None = None) -> list[dict]:
        return []

    def _effective_state(self, unit: dict) -> str:
        unit_id = str(unit.get("unit_id") or unit.get("work_id") or "")
        record = self.store.get(unit_id)
        if record is not None:
            return record.state
        return str(unit.get("state") or unit.get("status") or "")

    def summary(self) -> dict:
        try:
            cards = self.app.unit_cards()
        except Exception:  # pragma: no cover
            cards = []
        by_tier: dict[str, dict] = {
            "system": {"total": 0, "online": 0},
            "service": {"total": 0, "online": 0, "degraded": 0, "failed": 0},
            "worker": {"total": 0, "online": 0, "busy": 0},
        }
        online = 0
        degraded = 0
        failed = 0
        problems: list[dict] = []
        critical_bad = False
        for card in cards:
            state = self._effective_state(card)
            tier = str(card.get("tier") or "worker")
            bucket = by_tier.setdefault(tier, {"total": 0, "online": 0})
            bucket["total"] = bucket.get("total", 0) + 1
            is_online = state in ONLINE_STATES
            if is_online:
                online += 1
                bucket["online"] = bucket.get("online", 0) + 1
            if state == "degraded":
                degraded += 1
                bucket["degraded"] = bucket.get("degraded", 0) + 1
            if state == "failed":
                failed += 1
                bucket["failed"] = bucket.get("failed", 0) + 1
            if state in PROBLEM_STATES:
                unit_id = str(card.get("unit_id") or card.get("work_id"))
                record = self.store.get(unit_id)
                problems.append({
                    "unit_id": unit_id, "tier": tier, "state": state,
                    "reason": card.get("reason") or
                    (record.reason if record is not None else None),
                })
            if card.get("critical") and state != "ready":
                critical_bad = True
                problem = {
                    "unit_id": str(card.get("unit_id") or card.get("work_id")),
                    "tier": tier, "state": state,
                    "reason": card.get("reason"),
                }
                if problem not in problems:
                    problems.append(problem)
        busy = 0
        supervisor = getattr(self.app, "supervisor", None)
        if supervisor is not None:
            try:
                busy = sum(1 for agent in supervisor.list()
                           if getattr(agent, "status", "") == "working")
            except Exception:  # pragma: no cover
                busy = 0
        by_tier["worker"]["busy"] = busy
        if critical_bad:
            health = "down"
        elif degraded or failed:
            health = "degraded"
        else:
            health = "ok"
        return {
            "schema": "stackflow.units/v1",
            "generated_at": now_ms(),
            "total": len(cards),
            "online": online,
            "health": health,
            "degraded": degraded,
            "failed": failed,
            "by_tier": by_tier,
            "problems": problems,
        }

    def dependency(self, unit_id: str) -> dict:
        descriptor = self._descriptor(unit_id)
        requires = [str(item) for item in (descriptor.get("requires") or [])]
        wants = [str(item) for item in (descriptor.get("wants") or [])]
        dependents: list[str] = []
        try:
            cards = self.app.unit_cards()
        except Exception:  # pragma: no cover
            cards = []
        for card in cards:
            other = str(card.get("unit_id") or card.get("work_id") or "")
            if other == unit_id:
                continue
            edges = [str(item) for item in (card.get("requires") or [])] + \
                [str(item) for item in (card.get("wants") or [])]
            if unit_id in edges:
                dependents.append(other)
        return {
            "unit_id": unit_id,
            "requires": [{"unit_id": item, "state": self.state_of(item)}
                         for item in requires],
            "wants": [{"unit_id": item, "state": self.state_of(item)}
                      for item in wants],
            "dependents": sorted(dependents),
            "edges": {"requires": requires, "wants": wants,
                      "dependents": sorted(dependents)},
        }

    def detail(self, unit_id: str) -> dict:
        unit = self._descriptor(unit_id)
        return {
            "schema": "stackflow.unit/v1",
            "unit": unit,
            "descriptor": unit,
            "state": unit.get("state"),
            "since": unit.get("since"),
            "health": unit.get("health") or {},
            "actions": self._declared_actions(unit),
            "requires": [str(item) for item in (unit.get("requires") or [])],
            "wants": [str(item) for item in (unit.get("wants") or [])],
            "dependency": self.dependency(unit_id),
            "events": self.recent_events(unit_id, 20),
        }

    # -- routes -----------------------------------------------------------
    def routes(self) -> list[tuple[str, str, Any]]:
        return [
            ("GET", r"/api/units/summary", self.api_summary),
            ("GET", r"/api/units/([^/]+)", self.api_detail),
            ("POST", r"/api/units/([^/]+)/probe", self.api_probe),
            ("POST", r"/api/units/([^/]+)/actions", self.api_actions),
        ]

    async def api_summary(self, match, query, body):
        return 200, {"ok": True, "data": self.summary()}

    async def api_detail(self, match, query, body):
        return 200, {"ok": True, "data": self.detail(match.group(1))}

    async def api_probe(self, match, query, body):
        result = await self.probe_now(match.group(1))
        return 200, {"ok": True, "data": result}

    async def api_actions(self, match, query, body):
        self._require_operator()
        body = body or {}
        action = str(body.get("action") or "").strip()
        if not action:
            raise BadRequest("action is required")
        result = await self.act(match.group(1), action)
        return 200, {"ok": True, "data": result}

    def _require_operator(self) -> None:
        from ..web.api import current_auth
        role = getattr(current_auth(), "role", None)
        if role is not None and role != "operator":
            raise BadRequest("operator role required")

    def health(self) -> dict:
        states = self.store.states()
        by_state: dict[str, int] = {}
        for state in states.values():
            by_state[state] = by_state.get(state, 0) + 1
        return {
            "ok": True,
            "units": len(states),
            "by_state": dict(sorted(by_state.items())),
            "ticker": bool(self.ticker._task is not None
                           and not self.ticker._task.done()),
            "interval_s": self.interval_s,
        }


class UnitClient:
    """``call("unit://<id>", method, payload)`` with an in-process registry.

    The caller-facing interface is identical for every runtime mode: in-process
    units resolve to registered handlers (``approval.0`` has policy/approval
    handlers built in), while subprocess/remote units are routed through the
    hub transport as a placeholder.  Moving ``approval.0`` to its own process
    later therefore does not change call sites.
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self.log = get_logger("units.client")
        self._handlers: dict[tuple[str, str], Callable[[dict], Any]] = {}

    def register(self, unit_id: str, method: str,
                 handler: Callable[[dict], Any]) -> Callable[[], None]:
        unit_id = parse_unit_ref(unit_id)
        key = (unit_id, str(method))
        self._handlers[key] = handler

        def remove() -> None:
            if self._handlers.get(key) is handler:
                del self._handlers[key]

        return remove

    def has(self, ref: str, method: str) -> bool:
        try:
            unit_id = parse_unit_ref(ref)
        except BadRequest:
            return False
        return (unit_id, str(method)) in self._handlers or \
            self._builtin_handler(unit_id, str(method)) is not None

    async def call(self, ref: str, method: str,
                   payload: dict | None = None, *, timeout: float = 8.0) -> Any:
        unit_id = parse_unit_ref(ref)
        method = str(method or "")
        if not method:
            raise BadRequest("unit method is required")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise BadRequest("unit call payload must be an object")
        handler = self._handlers.get((unit_id, method))
        if handler is None:
            handler = self._builtin_handler(unit_id, method)
        if handler is not None:
            try:
                value = handler(dict(payload))
            except SfoError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise BadRequest(
                    f"unit {unit_id} call {method} failed: {exc}") from exc
            if inspect.isawaitable(value):
                return await value
            return value
        descriptor = self._descriptor(unit_id)
        mode = str((descriptor.get("runtime") or {}).get("mode") or "")
        if mode in ("subprocess", "remote", "sys"):
            transport = getattr(self.app, "transport", None)
            if transport is None:
                raise Unavailable(f"unit {unit_id} is not callable in-process")
            return await transport.call(unit_id, method, payload,
                                        timeout=timeout)
        raise NotFound(f"unit {unit_id!r} has no in-process method {method!r}",
                       detail={"unit_id": unit_id, "method": method})

    def _descriptor(self, unit_id: str) -> dict:
        for unit in self.app.unit_cards():
            if str(unit.get("unit_id") or unit.get("work_id")) == unit_id:
                return unit
        raise NotFound(f"unknown unit: {unit_id!r}",
                       detail={"unit_id": unit_id})

    def _builtin_handler(self, unit_id: str,
                         method: str) -> Callable[[dict], Any] | None:
        app = self.app
        if unit_id == "approval.0":
            if method == "policy.evaluate":
                def evaluate(payload: dict):
                    decision = app.policy.evaluate(
                        str(payload.get("principal") or "human:operator"),
                        str(payload.get("capability") or ""),
                        dict(payload.get("scope") or {}),
                        risk=payload.get("risk"))
                    return {"outcome": decision.outcome,
                            "risk": decision.risk,
                            "reason": decision.reason,
                            "rule_id": getattr(decision.rule, "id", None)}
                return evaluate
            if method == "approval.request":
                def request(payload: dict):
                    request_obj = app.approvals.request(
                        str(payload.get("principal") or "human:operator"),
                        str(payload.get("capability") or ""),
                        dict(payload.get("scope") or {}),
                        reason=str(payload.get("reason") or ""),
                        preview=str(payload.get("preview") or ""),
                        project_id=payload.get("project_id"),
                        task_id=payload.get("task_id"),
                        risk=payload.get("risk"))
                    return request_obj.to_public_dict()
                return request
            if method == "approval.decide":
                def decide(payload: dict):
                    request_obj = app.approvals.decide(
                        str(payload.get("request_id") or ""),
                        decision=str(payload.get("decision") or "allow"),
                        mode=str(payload.get("mode") or "once"),
                        by=str(payload.get("by") or "operator"),
                        note=str(payload.get("note") or ""))
                    return request_obj.to_public_dict()
                return decide
        if unit_id == "tool.0" and method == "tool.catalog":
            def catalog(payload: dict):
                capability = getattr(app, "capability_catalog", None)
                if capability is None:
                    return {"tools": []}
                return {"tools": capability.entries(kind="tool")}
            return catalog
        return None


def build(app: Any) -> UnitController:
    controller = UnitController(app)
    app.unit_control = controller
    app.units_control = controller
    return controller


__all__ = [
    "HealthTicker", "ONLINE_STATES", "PROBLEM_STATES", "UnitClient",
    "UnitController", "UnitRecord", "UnitStateStore", "build", "parse_unit_ref",
]

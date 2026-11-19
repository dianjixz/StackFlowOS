"""Failure domain: what happens when a unit stops answering (§5.3).

The failure manager is the reaper of the system. On every tick it asks the
registry to advance its liveness clocks and acts *only on newly offline
units*:

1. release all their leases (a dead holder must never pin resources);
2. emit ``failure.detected``;
3. apply the kind's :class:`FailurePolicy` — restart within a per-window
   budget, fail, or ignore;
4. quarantine units whose budget or restart history is exhausted.

Restarts are delegated to ``restart_cb(unit, attempt)`` (the supervisor owns
processes); ``kill_cb(unit)`` is used for fail/quarantine. Both may be async.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import asdict, dataclass, fields
from typing import Any, Callable

from ..errors import BadRequest, ValidationError
from ..kernel import Context
from ..protocol import EventRecord
from .eventlog import EventLog, emit_both
from .leases import LeaseManager
from .registry import UnitInfo, UnitRegistry

ACTIONS = ("restart", "fail", "ignore")


@dataclass
class FailurePolicy:
    """Per-unit-kind reaction to a liveness failure."""

    kind: str
    action: str = "restart"     # restart|fail|ignore
    max_restarts: int = 3
    window_s: float = 300.0
    quarantine_after: int = 5

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FailurePolicy":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


class FailureManager:
    """Sweeps liveness, releases leases and applies restart policies."""

    def __init__(self, ctx: Context, registry: UnitRegistry,
                 leases: LeaseManager, eventlog: EventLog, *,
                 policies: dict[str, FailurePolicy] | None = None,
                 restart_cb: Callable[[UnitInfo, int], Any] | None = None,
                 kill_cb: Callable[[UnitInfo], Any] | None = None,
                 tick_s: float = 2.0) -> None:
        self.ctx = ctx
        self.registry = registry
        self.leases = leases
        self.eventlog = eventlog
        self.log = getattr(ctx, "log", logging.getLogger("sfo.l2.failure"))
        self.tick_s = float(tick_s)
        self.restart_cb = restart_cb
        self.kill_cb = kill_cb
        self._policies: dict[str, FailurePolicy] = {}
        for policy in (policies or {}).values():
            self.set_policy(policy)
        # work_id -> monotonic attempt timestamps (seconds) inside the window.
        self._history: dict[str, list[float]] = {}
        self._task: asyncio.Task | None = None
        self._running = False

    # -- policy -----------------------------------------------------------
    def set_policy(self, policy: FailurePolicy) -> None:
        """Install/replace the policy for ``policy.kind``."""
        if not isinstance(policy, FailurePolicy):
            raise BadRequest("set_policy expects a FailurePolicy")
        if not policy.kind:
            raise ValidationError("failure policy kind is required")
        if policy.action not in ACTIONS:
            raise ValidationError(
                f"invalid failure action: {policy.action!r}",
                detail={"allowed": list(ACTIONS)})
        if policy.max_restarts < 0:
            raise ValidationError("max_restarts must be >= 0")
        if policy.quarantine_after < 0:
            raise ValidationError("quarantine_after must be >= 0")
        self._policies[policy.kind] = policy

    def policies(self) -> dict[str, FailurePolicy]:
        return dict(self._policies)

    def policy_for(self, kind: str) -> FailurePolicy:
        """Return the configured policy, or the safe default for ``kind``."""
        policy = self._policies.get(kind)
        if policy is None:
            return FailurePolicy(kind=kind)
        return policy

    # -- tick loop --------------------------------------------------------
    async def tick(self) -> list[dict]:
        """Sweep once and handle every unit that just went offline."""
        results: list[dict] = []
        for transition in self.registry.sweep():
            unit, status = self._unpack_transition(transition)
            if status != "offline":
                continue
            results.append(await self._handle_offline(unit))
        return results

    async def start(self) -> None:
        """Start the periodic tick loop (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="sfo-failure-tick")

    async def stop(self) -> None:
        """Cancel the periodic tick loop (idempotent, never raises)."""
        self._running = False
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # a crashed loop must not break shutdown
            self.log.exception("failure tick loop ended with an error")

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.tick_s)
            if not self._running:
                break
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("failure tick failed")

    # -- handling ---------------------------------------------------------
    async def _handle_offline(self, unit: UnitInfo) -> dict:
        work_id = unit.work_id
        kind = unit.kind
        reason = "unit_offline"

        released = self._release_leases(work_id)
        self._emit("failure.detected", unit, {
            "reason": reason,
            "released_leases": [lease.id for lease in released],
        })

        policy = self.policy_for(kind)
        if policy.action == "ignore":
            self._emit("failure.action", unit, {"action": "ignore",
                                                "policy": policy.to_dict()})
            return {"work_id": work_id, "kind": kind, "action": "ignore",
                    "released_leases": len(released)}

        if policy.action == "fail":
            await self._kill(unit)
            self._quarantine(unit, reason="policy_fail",
                             action="fail", policy=policy)
            return {"work_id": work_id, "kind": kind, "action": "fail",
                    "reason": "policy_fail",
                    "released_leases": len(released)}

        # action == "restart"
        attempts = self._attempts_in_window(work_id, policy.window_s)
        if int(unit.restart_count) >= policy.quarantine_after:
            self._quarantine(unit, reason="quarantine_after",
                             action="quarantined", policy=policy,
                             attempt=len(attempts))
            return {"work_id": work_id, "kind": kind, "action": "quarantined",
                    "reason": "quarantine_after", "attempts": len(attempts),
                    "released_leases": len(released)}
        if len(attempts) >= policy.max_restarts:
            self._quarantine(unit, reason="restart_budget_exhausted",
                             action="quarantined", policy=policy,
                             attempt=len(attempts))
            return {"work_id": work_id, "kind": kind, "action": "quarantined",
                    "reason": "restart_budget_exhausted",
                    "attempts": len(attempts),
                    "released_leases": len(released)}
        if self.restart_cb is None:
            self._emit("failure.action", unit,
                       {"action": "restart_skipped",
                        "reason": "no_restart_callback",
                        "policy": policy.to_dict()})
            return {"work_id": work_id, "kind": kind,
                    "action": "restart_skipped",
                    "reason": "no_restart_callback",
                    "released_leases": len(released)}

        attempt = len(attempts) + 1
        self._history.setdefault(work_id, []).append(time.monotonic())
        try:
            ok = await _maybe_await(self.restart_cb(unit, attempt))
        except Exception:
            self.log.exception("restart callback failed for %s", work_id)
            ok = False
        if ok:
            self._note_restart(unit, attempt)
            self._emit("failure.action", unit,
                       {"action": "restart", "attempt": attempt,
                        "policy": policy.to_dict()})
            return {"work_id": work_id, "kind": kind, "action": "restart",
                    "attempt": attempt, "released_leases": len(released)}

        await self._kill(unit)
        self._quarantine(unit, reason="restart_failed", action="quarantined",
                         policy=policy, attempt=attempt)
        return {"work_id": work_id, "kind": kind, "action": "quarantined",
                "reason": "restart_failed", "attempt": attempt,
                "released_leases": len(released)}

    def _quarantine(self, unit: UnitInfo, *, reason: str, action: str,
                    policy: FailurePolicy, attempt: int | None = None) -> None:
        self.registry.mark_quarantined(unit.work_id, reason)
        data = {"action": action, "reason": reason,
                "policy": policy.to_dict()}
        if attempt is not None:
            data["attempt"] = attempt
        self._emit("failure.action", unit, data)

    def _release_leases(self, work_id: str) -> list:
        try:
            return list(self.leases.release_holder(work_id,
                                                   reason="unit_offline"))
        except Exception:
            self.log.exception("could not release leases of %s", work_id)
            return []

    def _attempts_in_window(self, work_id: str, window_s: float) -> list[float]:
        cutoff = time.monotonic() - float(window_s)
        history = [stamp for stamp in self._history.get(work_id, ())
                   if stamp >= cutoff]
        self._history[work_id] = history
        return history

    def _note_restart(self, unit: UnitInfo, attempt: int) -> None:
        note = getattr(self.registry, "note_restart", None)
        if callable(note):
            note(unit.work_id, reason=f"failure.restart attempt={attempt}")
        else:  # duck-typed registries in tests / partial deployments
            unit.restart_count += 1

    async def _kill(self, unit: UnitInfo) -> None:
        if self.kill_cb is None:
            return
        try:
            await _maybe_await(self.kill_cb(unit))
        except Exception:
            self.log.exception("kill callback failed for %s", unit.work_id)

    @staticmethod
    def _unpack_transition(transition: Any) -> tuple[UnitInfo, str]:
        if isinstance(transition, tuple):
            return transition[0], transition[1]
        unit = transition
        return unit, getattr(unit, "status", "offline")

    def _emit(self, type: str, unit: UnitInfo, extra: dict) -> EventRecord:
        data = {"work_id": unit.work_id, "kind": unit.kind,
                "project_id": unit.project_id, "unit": unit.to_dict()}
        data.update(extra)
        return emit_both(self.ctx, self.eventlog, type, data, unit.project_id)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


__all__ = ["FailurePolicy", "FailureManager", "ACTIONS"]

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
from collections import deque
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

    #: Hard ceiling for the number of distinct work_ids kept in the restart
    #: history.  Without it a long-lived deployment that churns through
    #: work_ids would grow the dict without bound (§6.15 / S8).
    MAX_HISTORY_ENTRIES = 2048
    #: Never prune attempt timestamps younger than this, regardless of the
    #: configured policy window (which may be shorter than one hour).
    MIN_HISTORY_RETENTION_S = 3600.0

    def __init__(self, ctx: Context, registry: UnitRegistry,
                 leases: LeaseManager, eventlog: EventLog, *,
                 policies: dict[str, FailurePolicy] | None = None,
                 restart_cb: Callable[[UnitInfo, int], Any] | None = None,
                 kill_cb: Callable[[UnitInfo], Any] | None = None,
                 tick_s: float = 2.0,
                 max_history_entries: int | None = None) -> None:
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
        if max_history_entries is None:
            max_history_entries = self.MAX_HISTORY_ENTRIES
        try:
            max_history_entries = int(max_history_entries)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "max_history_entries must be an integer") from exc
        if max_history_entries < 1:
            raise ValidationError("max_history_entries must be >= 1")
        self.max_history_entries = max_history_entries
        # work_id -> monotonic attempt timestamps (seconds) inside the window.
        # Bounded by _prune_history(): stale stamps are dropped, empty entries
        # removed, and at most ``max_history_entries`` work_ids are retained.
        self._history: dict[str, deque[float]] = {}
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
        self._prune_history()
        results: list[dict] = []
        for transition in self.registry.sweep():
            unit, status = self._unpack_transition(transition)
            if status != "offline":
                continue
            results.append(await self._handle_offline(unit))
        if len(self._history) > self.max_history_entries:
            self._prune_history()
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
        self._history.setdefault(work_id, deque()).append(time.monotonic())
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
        stamps = self._history.get(work_id)
        if stamps is None:
            return []
        while stamps and stamps[0] < cutoff:
            stamps.popleft()
        if not stamps:
            # An expired entry must not keep a key alive forever.
            self._history.pop(work_id, None)
            return []
        return list(stamps)

    # -- bounded history (S8) ---------------------------------------------
    def _history_retention_s(self) -> float:
        """How old an attempt stamp may get before it is pruned.

        The widest configured policy window wins (so a policy with a
        multi-hour window keeps its budget accurate), but never less than
        :attr:`MIN_HISTORY_RETENTION_S`.
        """
        widest = 0.0
        for policy in self._policies.values():
            widest = max(widest, float(policy.window_s))
        return max(widest, self.MIN_HISTORY_RETENTION_S)

    def _prune_history(self, now: float | None = None) -> int:
        """Bound the restart history (called at the start of every tick).

        Drops attempt timestamps older than the retention window, deletes
        work_ids whose deque became empty, then evicts the least recently
        attempted work_ids until at most ``max_history_entries`` remain.
        Returns the number of work_ids evicted by the size cap.
        """
        if not self._history:
            return 0
        now = time.monotonic() if now is None else float(now)
        cutoff = now - self._history_retention_s()
        for work_id in list(self._history):
            stamps = self._history[work_id]
            while stamps and stamps[0] < cutoff:
                stamps.popleft()
            if not stamps:
                del self._history[work_id]

        evicted = 0
        overflow = len(self._history) - self.max_history_entries
        if overflow > 0:
            # Deques are append-ordered, so the last stamp is the newest.
            oldest = sorted(self._history.items(),
                            key=lambda item: item[1][-1])[:overflow]
            for work_id, _ in oldest:
                del self._history[work_id]
                evicted += 1
        return evicted

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

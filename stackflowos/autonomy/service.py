"""Autonomy service: the loop that patrols, dispatches and self-approves.

Design contract: ``docs/team/autonomy-plan.md`` and ``docs/autonomy.md``.

One patrol = collect snapshot → run read-only checks → persist report + emit
events → (only when enabled and not a dry run) let the dispatcher create/spawn
work, then let the guardian decide pending approvals within the operator's
envelope.  Every step is failure-isolated: a broken check or a lost race with a
human must not take the hub down or wedge the next patrol.

The service is started by ``extensions.start_extensions`` after the core
services exist; ``build(app)`` attaches it as ``app.autonomy`` so the operator
tools, routes and CLI can reach the same instance.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
from pathlib import Path
from typing import Any

from ..errors import BadRequest, Conflict, SfoError
from ..protocol import now_ms
from ..util.jsonio import read_json, write_json
from ..util.logging import get_logger
from .models import (
    APPROVE_MODES,
    DISPATCH_MODES,
    Finding,
    PatrolReport,
    diff_fingerprints,
    severity_counts,
)

#: Hard ceiling on how many findings are emitted as individual events per run.
_MAX_FINDING_EVENTS = 20
#: How many dispatcher/guardian records are written to the event log per run.
_MAX_ACTION_EVENTS = 20
#: Bound on the persisted dispatch map (one record per fingerprint).
_MAX_DISPATCH_RECORDS = 300
#: Bound on the persisted guardian records.
_MAX_APPROVAL_RECORDS = 200


def _blank_state() -> dict:
    return {
        "version": 1,
        "seq": 0,
        "last_run": 0,
        "counts": {"patrols": 0, "findings": 0, "dispatched": 0,
                   "approvals": 0, "errors": 0},
        "report": None,
        "history": [],
        "dispatch": {},
        "approvals": [],
        "rate": {"tasks": [], "spawns": [], "approvals": []},
    }


class AutonomyService:
    """Hub-resident extension implementing the autonomy loop."""

    name = "autonomy"

    def __init__(self, app: Any) -> None:
        self.app = app
        self.log = get_logger("autonomy")
        self.state_path = Path(app.paths.state_dir) / "state" / "autonomy.json"
        self._state = self._load_state()
        self.report: PatrolReport | None = PatrolReport.from_dict(
            self._state.get("report"))
        self._cfg = getattr(app.config, "autonomy", None)
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._running = False
        self._lock = asyncio.Lock()
        self._next_at: int | None = None
        #: Last serialised policy, used to emit ``autonomy.policy.changed``
        #: exactly once per real change (initial load is not a change).
        self._policy_snapshot = self._serialise_policy()
        self._ensure_state_shape()
        app.autonomy = self

    # ------------------------------------------------------------------
    # extension contract
    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._stopped = False
        self._reload_config()
        self._reconcile_loop()
        self.log.info("autonomy %s (dispatch=%s approve=%s interval=%.0fs)",
                      "enabled" if self.enabled else "disabled",
                      self._mode("dispatch"), self._mode("approve"),
                      float(getattr(self.cfg, "interval_s", 0) or 0))

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._next_at = None

    async def on_config(self, config: Any) -> None:
        """Re-read ``config.autonomy`` after a live config patch."""
        previous = self._policy_snapshot
        self._reload_config()
        current = self._serialise_policy()
        if current != previous:
            self._policy_snapshot = current
            self._emit("autonomy.policy.changed", {
                "policy": self._policy_dict(),
                "enabled": self.enabled,
            })
            self.log.info("autonomy policy updated (dispatch=%s approve=%s)",
                          self._mode("dispatch"), self._mode("approve"))
        if not self._stopped:
            self._reconcile_loop()

    def health(self) -> dict:
        """Probe summary included in the hub's ``/api/health`` payload."""
        counts = self._state.get("counts") or {}
        return {
            "ok": True,
            "enabled": self.enabled,
            "running": self._running,
            "last_run": int(self._state.get("last_run") or 0),
            "findings": int(counts.get("findings") or 0),
            "patrols": int(counts.get("patrols") or 0),
            "dispatched": int(counts.get("dispatched") or 0),
            "approvals": int(counts.get("approvals") or 0),
            "errors": int(counts.get("errors") or 0),
        }

    def units(self) -> list[dict]:
        """Describe ``autonomy.0`` in the unit-card shape used by the hub."""
        cfg = self.cfg
        now = now_ms()
        state = "ready" if self.enabled else "disabled"
        host = getattr(getattr(self.app, "config", None), "system", None)
        host_label = getattr(host, "host_label", "")
        counts = self._state.get("counts") or {}
        return [{
            "work_id": "autonomy.0",
            "unit_id": "autonomy.0",
            "kind": "autonomy",
            "role": "governor",
            "tier": "service",
            "label": "自治中枢 · 巡检/派工/审批",
            "project_id": None,
            "pid": None,
            "host": host_label,
            "transport": "inproc",
            "rpc_address": None,
            "events_address": None,
            "status": "online" if self.enabled else "disabled",
            "state": state,
            "reason": None if self.enabled else "自治未启用",
            "abilities": [
                {"name": "autonomy.patrol", "scope": "system"},
                {"name": "autonomy.dispatch", "scope": "projects"},
                {"name": "autonomy.approve", "scope": "approvals"},
            ],
            "hwinfo": {},
            "started_at": int(self._state.get("last_run") or 0),
            "last_seen": now,
            "restart_count": 0,
            "metadata": {
                "description": "自治闭环：巡检 → 派工 → 代理审批",
                "dispatch_mode": self._mode("dispatch"),
                "approve_mode": self._mode("approve"),
                "findings": int(counts.get("findings") or 0),
            },
            "runtime": {
                "mode": "in_process",
                "host_unit": "sys.0",
                "host": host_label,
                "pid": None,
                "module": "stackflowos.autonomy.service:AutonomyService",
                "started_at": int(self._state.get("last_run") or 0),
            },
            "endpoints": [
                {"name": "inproc", "transport": "inproc",
                 "address": "autonomy.0"},
            ],
            "ref": "unit://autonomy.0",
            "critical": False,
            "requires": ["approval.0", "store.project", "store.eventlog"],
            "actions": ["probe"],
            "lease": {"type": "host", "host_unit": "sys.0",
                      "interval_s": 5, "renewed_at": now},
            "provides": {
                "patrols": int(counts.get("patrols") or 0),
                "findings": int(counts.get("findings") or 0),
                "dispatched": int(counts.get("dispatched") or 0),
                "approvals": int(counts.get("approvals") or 0),
            },
        }]

    def routes(self) -> list[tuple[str, str, Any]]:
        from .api import build_routes

        return build_routes(self)

    # ------------------------------------------------------------------
    # config / loop
    # ------------------------------------------------------------------
    @property
    def cfg(self) -> Any:
        # ``app.config`` is replaced wholesale by a live patch; always read the
        # current object so a policy change takes effect on the next patrol.
        current = getattr(getattr(self.app, "config", None), "autonomy", None)
        if current is not None:
            self._cfg = current
        return self._cfg

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", False))

    def _mode(self, kind: str) -> str:
        return str(getattr(self.cfg,
                           "dispatch_mode" if kind == "dispatch"
                           else "approve_mode", "off") or "off")

    def _reload_config(self) -> None:
        self._cfg = getattr(getattr(self.app, "config", None), "autonomy",
                            self._cfg)

    def _serialise_policy(self) -> str:
        try:
            return json.dumps(self._policy_dict(), sort_keys=True,
                              ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return ""

    def _policy_dict(self) -> dict:
        try:
            return dataclasses.asdict(self.cfg)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return {}

    async def configure(self, patch: dict) -> dict:
        """Validate, persist and apply a partial autonomy policy."""
        if not isinstance(patch, dict) or not patch:
            raise BadRequest("autonomy policy patch must be a non-empty object")
        known = {field.name for field in dataclasses.fields(type(self.cfg))}
        unknown = sorted(set(patch) - known)
        if unknown:
            raise BadRequest(
                f"unknown autonomy policy fields: {unknown}",
                detail={"fields": unknown, "known": sorted(known)})
        await self.app.update_config({"autonomy": dict(patch)})
        # ``update_config`` fans ``on_config`` out to extensions, which
        # re-reads the policy and reconciles the loop.  Return the fresh view.
        return self.status()

    # -- scheduling ------------------------------------------------------
    def _reconcile_loop(self) -> None:
        if self._stopped:
            return
        if not self.enabled:
            task, self._task = self._task, None
            if task is not None and not task.done():
                task.cancel()
            self._next_at = None
            return
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.create_task(self._loop(),
                                                 name="autonomy-loop")
            except RuntimeError:  # no running loop (unit tests, sync caller)
                self._task = None

    async def _loop(self) -> None:
        cfg = self.cfg
        first = bool(getattr(cfg, "on_start", True)) and not int(
            self._state.get("last_run") or 0)
        delay = 5.0 if first else max(10.0, float(
            getattr(cfg, "interval_s", 120.0) or 120.0))
        try:
            while not self._stopped:
                self._next_at = now_ms() + int(delay * 1000)
                await asyncio.sleep(delay)
                self._next_at = None
                if self._stopped or not self.enabled:
                    return
                try:
                    await self.run_patrol(trigger="timer")
                except asyncio.CancelledError:
                    raise
                except Conflict:
                    self.log.debug("autonomy patrol already running; skipping")
                except Exception:
                    self.log.exception("autonomy patrol failed")
                    self._count("errors")
                delay = max(10.0, float(
                    getattr(self.cfg, "interval_s", 120.0) or 120.0))
        finally:
            self._next_at = None

    def next_run_in_s(self) -> int | None:
        """Seconds until the next scheduled patrol (``None`` when disabled)."""
        if not self.enabled:
            return None
        if self._next_at is None:
            if bool(getattr(self.cfg, "on_start", True)) and not int(
                    self._state.get("last_run") or 0):
                return 5
            return max(0, int(float(getattr(self.cfg, "interval_s", 120.0)
                                    or 0)))
        return max(0, int((self._next_at - now_ms()) / 1000))

    # ------------------------------------------------------------------
    # the patrol
    # ------------------------------------------------------------------
    async def run_patrol(self, *, trigger: str = "operator",
                         dry_run: bool = False) -> dict:
        """Run one patrol and, when enabled, dispatch + self-approve.

        ``dry_run`` always reports and never acts, even on an enabled system.
        Manual patrols are allowed while autonomy is disabled: inspection is
        read-only and useful before an operator turns the loop on.
        """
        if self._lock.locked():
            raise Conflict("autonomy patrol is already running")
        async with self._lock:
            self._running = True
            try:
                return await self._run_patrol_locked(trigger=trigger,
                                                     dry_run=dry_run)
            finally:
                self._running = False

    async def _run_patrol_locked(self, *, trigger: str,
                                 dry_run: bool) -> dict:
        from .patrol import collect_snapshot, run_checks

        started = now_ms()
        self._state["seq"] = int(self._state.get("seq") or 0) + 1
        report = PatrolReport(seq=self._state["seq"], started_at=started,
                              trigger=str(trigger or "operator"))
        previous = self.report.findings if self.report is not None else []
        self._emit("autonomy.patrol.started", {
            "seq": report.seq, "trigger": report.trigger,
            "dry_run": bool(dry_run), "enabled": self.enabled,
        })
        try:
            snapshot = collect_snapshot(self.app, now=started)
            report.findings = run_checks(snapshot, self.cfg)
            report.new_fingerprints, report.resolved_fingerprints = (
                diff_fingerprints(previous, report.findings))
        except Exception as exc:  # noqa: BLE001 - a broken check is one report
            self.log.exception("autonomy patrol collection failed")
            report.error = f"{type(exc).__name__}: {exc}"
            self._count("errors")
            self._emit("autonomy.error", {
                "phase": "patrol", "trigger": report.trigger,
                "error": type(exc).__name__, "message": str(exc)[:500],
            })

        acts = bool(self.enabled) and not dry_run and not report.error
        if acts and self._mode("dispatch") in DISPATCH_MODES and \
                self._mode("dispatch") != "off":
            try:
                from .dispatcher import Dispatcher

                report.dispatched = await Dispatcher(
                    self.app, self.cfg, self._state).apply(report.findings)
                for record in report.dispatched:
                    if str(record.get("status")) == "ok":
                        self._count("dispatched")
            except Exception as exc:  # noqa: BLE001
                self.log.exception("autonomy dispatch failed")
                report.error = report.error or f"dispatch: {type(exc).__name__}"
                self._count("errors")
                self._emit("autonomy.error", {
                    "phase": "dispatch", "error": type(exc).__name__,
                    "message": str(exc)[:500],
                })
        if acts and self._mode("approve") in APPROVE_MODES and \
                self._mode("approve") != "off":
            try:
                from .guardian import Guardian

                report.approvals = await Guardian(
                    self.app, self.cfg, self._state).apply()
                for record in report.approvals:
                    if str(record.get("decision")) == "allow":
                        self._count("approvals")
            except Exception as exc:  # noqa: BLE001
                self.log.exception("autonomy guardian failed")
                report.error = report.error or f"guardian: {type(exc).__name__}"
                self._count("errors")
                self._emit("autonomy.error", {
                    "phase": "guardian", "error": type(exc).__name__,
                    "message": str(exc)[:500],
                })

        report.finished_at = now_ms()
        self._record_findings(report, dry_run=dry_run)
        self._record_actions(report)
        self.report = report
        self._state["last_run"] = report.finished_at
        self._state["counts"]["findings"] = len(report.findings)
        self._state["counts"]["patrols"] = int(
            self._state["counts"].get("patrols") or 0) + 1
        summary = severity_counts(report.findings)
        self._remember(report)
        self._save()
        self._emit("autonomy.patrol.completed", {
            "seq": report.seq,
            "trigger": report.trigger,
            "summary": summary,
            "headline": report.headline,
            "findings": [finding.fingerprint for finding in report.findings],
            "new": report.new_fingerprints,
            "resolved": report.resolved_fingerprints,
            "dispatched": len(report.dispatched),
            "approvals": len(report.approvals),
            "error": report.error,
        })
        return report.to_dict()

    # ------------------------------------------------------------------
    # projections
    # ------------------------------------------------------------------
    def status(self) -> dict:
        """The full view used by ``GET /api/autonomy`` and the console app."""
        counts = dict(self._state.get("counts") or {})
        report = self.report.to_dict() if self.report is not None else None
        dispatches = sorted(
            (record for record in (self._state.get("dispatch") or {}).values()
             if isinstance(record, dict)),
            key=lambda item: int(item.get("ts") or 0), reverse=True)[:50]
        approvals = list(reversed(
            [record for record in self._state.get("approvals") or []
             if isinstance(record, dict)]))[:50]
        return {
            "enabled": self.enabled,
            "running": self._running,
            "last_run": int(self._state.get("last_run") or 0),
            "next_run_in_s": self.next_run_in_s(),
            "interval_s": float(getattr(self.cfg, "interval_s", 0) or 0),
            "policy": self._policy_dict(),
            "counts": counts,
            "findings": report["findings"] if report else [],
            "report": report,
            "history": list(self._state.get("history") or []),
            "dispatches": dispatches,
            "approvals": approvals,
            "guardrail": self._guardrail(),
        }

    def _guardrail(self) -> dict:
        cutoff = now_ms() - 3600 * 1000
        rate = self._state.get("rate") or {}
        return {
            "spawns_last_hour": _count_recent(rate.get("spawns"), cutoff),
            "tasks_last_hour": _count_recent(rate.get("tasks"), cutoff),
            "approvals_last_hour": _count_recent(rate.get("approvals"), cutoff),
            "limits": {
                "max_spawns_per_hour": int(getattr(
                    self.cfg, "dispatch_max_spawns_per_hour", 0) or 0),
                "max_tasks_per_hour": int(getattr(
                    self.cfg, "dispatch_max_tasks_per_hour", 0) or 0),
                "max_approvals_per_hour": int(getattr(
                    self.cfg, "approve_max_per_hour", 0) or 0),
                "max_agents": int(getattr(
                    self.cfg, "dispatch_max_agents", 0) or 0),
            },
        }

    # ------------------------------------------------------------------
    # persistence / events
    # ------------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            raw = read_json(self.state_path, default=None)
        except (OSError, ValueError):
            # A corrupt state file must not stop the hub from booting or from
            # starting the loop: quarantine it by resetting to a blank state.
            self.log.warning("autonomy state is unreadable; starting fresh",
                             exc_info=True)
            return _blank_state()
        state = _blank_state()
        if not isinstance(raw, dict):
            return state
        try:
            state["seq"] = int(raw.get("seq") or 0)
            state["last_run"] = int(raw.get("last_run") or 0)
            counts = raw.get("counts")
            if isinstance(counts, dict):
                for key in state["counts"]:
                    try:
                        state["counts"][key] = int(counts.get(key) or 0)
                    except (TypeError, ValueError):
                        state["counts"][key] = 0
            history = raw.get("history")
            if isinstance(history, list):
                state["history"] = [row for row in history
                                    if isinstance(row, dict)]
            dispatch = raw.get("dispatch")
            if isinstance(dispatch, dict):
                state["dispatch"] = {
                    str(key): value for key, value in dispatch.items()
                    if isinstance(value, dict)}
            approvals = raw.get("approvals")
            if isinstance(approvals, list):
                state["approvals"] = [row for row in approvals
                                      if isinstance(row, dict)]
            rate = raw.get("rate")
            if isinstance(rate, dict):
                for key in state["rate"]:
                    rows = rate.get(key)
                    if isinstance(rows, list):
                        state["rate"][key] = [row for row in rows
                                              if isinstance(row, (int, float))]
            report = raw.get("report")
            if isinstance(report, dict):
                state["report"] = report
        except (TypeError, ValueError):
            self.log.warning("autonomy state is corrupt; resetting", exc_info=True)
            return _blank_state()
        return state

    def _ensure_state_shape(self) -> None:
        blank = _blank_state()
        for key, value in blank.items():
            self._state.setdefault(key, value)
        for key, value in blank["counts"].items():
            self._state["counts"].setdefault(key, value)
        for key, value in blank["rate"].items():
            self._state["rate"].setdefault(key, value)

    def _save(self) -> None:
        try:
            self._trim()
            report = self.report.to_dict() if self.report else None
            self._state["report"] = report
            write_json(self.state_path, self._state)
        except Exception:  # noqa: BLE001 - persistence must not stop the loop
            self.log.exception("could not persist autonomy state")

    def _trim(self) -> None:
        keep = max(1, int(getattr(self.cfg, "report_keep", 20) or 20))
        history = self._state.get("history") or []
        self._state["history"] = history[-keep:]
        dispatch = self._state.get("dispatch") or {}
        if len(dispatch) > _MAX_DISPATCH_RECORDS:
            newest = sorted(dispatch.items(),
                            key=lambda item: int(
                                (item[1] or {}).get("ts") or 0),
                            reverse=True)[: _MAX_DISPATCH_RECORDS]
            self._state["dispatch"] = dict(newest)
        approvals = self._state.get("approvals") or []
        self._state["approvals"] = approvals[-_MAX_APPROVAL_RECORDS:]
        cutoff = now_ms() - 3600 * 1000
        rate = self._state.get("rate") or {}
        for key in ("tasks", "spawns", "approvals"):
            rate[key] = [ts for ts in rate.get(key) or []
                         if isinstance(ts, (int, float)) and ts >= cutoff]

    def _remember(self, report: PatrolReport) -> None:
        counts = severity_counts(report.findings)
        self._state["history"].append({
            "seq": report.seq,
            "finished_at": report.finished_at,
            "trigger": report.trigger,
            "summary": counts,
            "headline": report.headline,
            "error": report.error,
        })
        keep = max(1, int(getattr(self.cfg, "report_keep", 20) or 20))
        self._state["history"] = self._state["history"][-keep:]

    def _record_findings(self, report: PatrolReport, *, dry_run: bool) -> None:
        new = set(report.new_fingerprints)
        emitted = 0
        for finding in report.findings:
            if emitted >= _MAX_FINDING_EVENTS:
                break
            if finding.fingerprint not in new:
                continue
            if finding.severity == "info":
                continue
            self._emit("autonomy.finding", {
                **finding.to_dict(), "seq": report.seq,
                "dry_run": bool(dry_run),
            }, project_id=finding.project_id)
            emitted += 1

    def _record_actions(self, report: PatrolReport) -> None:
        emitted = 0
        for record in report.dispatched:
            if emitted >= _MAX_ACTION_EVENTS:
                break
            if str(record.get("status")) == "skipped":
                continue
            self._emit("autonomy.dispatched", {
                **record, "seq": report.seq,
            }, project_id=record.get("project_id"))
            emitted += 1
        for record in report.approvals:
            if emitted >= _MAX_ACTION_EVENTS:
                break
            if str(record.get("decision")) == "skip":
                continue
            self._emit("autonomy.approval", {
                **record, "seq": report.seq,
            }, project_id=record.get("project_id"))
            emitted += 1

    def _count(self, key: str, amount: int = 1) -> None:
        counts = self._state.setdefault("counts", {})
        try:
            counts[key] = int(counts.get(key) or 0) + int(amount)
        except (TypeError, ValueError):
            counts[key] = int(amount)

    def _emit(self, type: str, data: dict, *,
              project_id: str | None = None) -> None:
        try:
            self.app.eventlog.append(type, data, source="autonomy",
                                     project_id=project_id)
        except Exception:  # noqa: BLE001 - events must never break the loop
            self.log.debug("could not emit %s", type, exc_info=True)

    # ------------------------------------------------------------------
    # helpers used by api/operator/cli
    # ------------------------------------------------------------------
    async def dispatch_now(self) -> dict:
        """Run one patrol triggered by a human (same semantics as the timer)."""
        return await self.run_patrol(trigger="operator")

    def recent_events(self, *, limit: int = 50) -> list[dict]:
        """Recent autonomy events for the console/panel (best effort)."""
        try:
            records = self.app.eventlog.query(
                types=["autonomy"], limit=max(1, int(limit)))
        except Exception:  # noqa: BLE001
            return []
        return [record.to_dict() for record in records]


def _count_recent(rows: Any, cutoff: int) -> int:
    if not isinstance(rows, list):
        return 0
    return sum(1 for row in rows
               if isinstance(row, (int, float)) and row >= cutoff)


__all__ = ["AutonomyService"]

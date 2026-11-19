"""Unit registry: who is alive, where to reach them, what they can do.

A *unit* is a process with an address, a lifecycle and (usually) a lease;
the hub itself is ``sys.0``, while a worker unit typically hosts one agent
(docs/architecture.md §5.1). The registry lists worker units — the ``ps`` of
StackFlowOS:

* ``register`` is upsert-like: re-registering a ``work_id`` updates it in place;
* heartbeats move ``suspect``/``offline`` units back to ``online``;
* ``sweep`` applies the two clocks — ``offline_after_s`` to ``suspect`` and
  ``2 * offline_after_s`` to ``offline`` — and only emits on transitions;
* ``abilities()`` is the capability catalogue: the union of what currently
  online units honestly registered.

Every transition is written to the event log and emitted on the kernel bus so
the UI can react without polling.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from ..errors import BadRequest, NotFound
from ..kernel import Context
from ..protocol import EventRecord, now_ms as _now_ms
from .eventlog import EventLog, emit_both

STATUSES = ("online", "suspect", "offline", "quarantined")


@dataclass
class UnitInfo:
    """Registration record of one unit process."""

    work_id: str
    kind: str
    label: str = ""
    project_id: str | None = None
    pid: int | None = None
    host: str = ""
    transport: str = "ipc"
    rpc_address: str = ""
    events_address: str = ""
    status: str = "online"      # online|suspect|offline|quarantined
    abilities: list[dict] = field(default_factory=list)   # [{"name","scope"}]
    hwinfo: dict = field(default_factory=dict)
    started_at: int = 0
    last_seen: int = 0
    restart_count: int = 0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "UnitInfo":
        """Build a unit from a dict, ignoring unknown keys."""
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in (d or {}).items() if k in known}
        info = cls(**data)
        if not isinstance(info.abilities, list):
            info.abilities = []
        if not isinstance(info.hwinfo, dict):
            info.hwinfo = {}
        if not isinstance(info.metadata, dict):
            info.metadata = {}
        return info


class UnitRegistry:
    """In-memory unit table with lifecycle events and liveness sweeping."""

    def __init__(self, ctx: Context, eventlog: EventLog, *,
                 offline_after_s: float = 8.0,
                 logger: logging.Logger | None = None) -> None:
        self.ctx = ctx
        self.eventlog = eventlog
        self.offline_after_s = float(offline_after_s)
        self.log = logger or getattr(ctx, "log", logging.getLogger("sfo.l2.registry"))
        self._units: dict[str, UnitInfo] = {}
        self._lock = threading.RLock()

    # -- lifecycle --------------------------------------------------------
    def register(self, info: UnitInfo) -> UnitInfo:
        """Register or update a unit; emits ``unit.registered`` (+ online)."""
        if not isinstance(info, UnitInfo):
            raise BadRequest("register expects a UnitInfo")
        if not info.work_id:
            raise BadRequest("unit.work_id is required")
        if not info.kind:
            info.kind = info.work_id.split(".")[0]
        now = _now_ms()
        with self._lock:
            stored = UnitInfo.from_dict(info.to_dict())
            stored.started_at = stored.started_at or now
            stored.last_seen = stored.last_seen or now
            previous = self._units.get(stored.work_id)
            if previous is not None:
                stored.restart_count = max(previous.restart_count,
                                           stored.restart_count)
                stored.started_at = previous.started_at or stored.started_at
                stored.metadata = {**previous.metadata, **stored.metadata}
            self._units[stored.work_id] = stored
        self._emit_unit("unit.registered", stored)
        if stored.status == "online":
            self._emit_unit("unit.online", stored)
        return stored

    def heartbeat(self, work_id: str, *, stats: dict | None = None) -> UnitInfo:
        """Record a liveness ping; restores ``suspect``/``offline`` to online."""
        now = _now_ms()
        with self._lock:
            unit = self._get_locked(work_id)
            unit.last_seen = now
            if stats is not None:
                unit.metadata["stats"] = dict(stats)
            restored = unit.status in ("suspect", "offline")
            if restored:
                unit.status = "online"
        if restored:
            self._emit_unit("unit.online", unit, reason="heartbeat")
        return unit

    def release(self, work_id: str, *, reason: str = "released") -> UnitInfo | None:
        """Remove a unit from the table; emits ``unit.released`` when known."""
        with self._lock:
            unit = self._units.pop(work_id, None)
        if unit is None:
            return None
        self._emit_unit("unit.released", unit, reason=reason)
        return unit

    # -- lookup -----------------------------------------------------------
    def get(self, work_id: str) -> UnitInfo:
        with self._lock:
            return self._get_locked(work_id)

    def list(self, *, kind: str | None = None,
             project_id: str | None = None,
             status: str | None = None) -> list[UnitInfo]:
        with self._lock:
            units = list(self._units.values())
        result = [
            unit for unit in units
            if (kind is None or unit.kind == kind)
            and (project_id is None or unit.project_id == project_id)
            and (status is None or unit.status == status)
        ]
        return sorted(result, key=lambda unit: unit.work_id)

    def online(self) -> list[UnitInfo]:
        return self.list(status="online")

    # -- liveness ---------------------------------------------------------
    def sweep(self, now_ms: int | None = None) -> list[tuple[UnitInfo, str]]:
        """Advance liveness clocks; return ``(unit, new_status)`` transitions."""
        now = now_ms if now_ms is not None else _now_ms()
        suspect_after = self.offline_after_s * 1000.0
        offline_after = suspect_after * 2.0
        transitions: list[tuple[UnitInfo, str]] = []
        with self._lock:
            units = list(self._units.values())
            for unit in units:
                if unit.status not in ("online", "suspect"):
                    continue
                age = now - unit.last_seen
                if unit.status == "online" and age >= suspect_after:
                    unit.status = "suspect"
                    transitions.append((unit, "suspect"))
                elif unit.status == "suspect" and age >= offline_after:
                    unit.status = "offline"
                    transitions.append((unit, "offline"))
        for unit, status in transitions:
            self._emit_unit(f"unit.{status}", unit, reason="sweep",
                            last_seen=unit.last_seen)
        return transitions

    # -- administration ---------------------------------------------------
    def mark_quarantined(self, work_id: str, reason: str) -> UnitInfo:
        """Quarantine a unit (it stays listed but is never scheduled again)."""
        with self._lock:
            unit = self._get_locked(work_id)
            unit.status = "quarantined"
        self._emit_unit("unit.quarantined", unit, reason=reason)
        return unit

    def update_hwinfo(self, work_id: str, hwinfo: dict) -> UnitInfo:
        """Merge fresh hardware/statistics information into a unit."""
        with self._lock:
            unit = self._get_locked(work_id)
            unit.hwinfo.update(dict(hwinfo or {}))
        return unit

    def note_restart(self, work_id: str, *, reason: str = "") -> UnitInfo:
        """Bump ``restart_count`` after a successful supervised restart.

        Deliberately does **not** flip the unit back to ``online``: a restart
        may replace the process with a new ``work_id`` (the supervisor does),
        and liveness is only ever established by a real registration or
        heartbeat. Fabricating ``online`` here would leave a ghost unit that
        the scheduler would happily pick.
        """
        with self._lock:
            unit = self._get_locked(work_id)
            unit.restart_count += 1
        self._emit_unit("unit.restarted", unit, reason=reason,
                        restart_count=unit.restart_count)
        return unit

    # -- catalogue --------------------------------------------------------
    def abilities(self) -> dict[str, list[str]]:
        """Capability name -> sorted work ids of online units offering it."""
        catalogue: dict[str, list[str]] = {}
        for unit in self.online():
            for ability in unit.abilities:
                if not isinstance(ability, dict):
                    continue
                name = ability.get("name")
                if not name:
                    continue
                catalogue.setdefault(str(name), []).append(unit.work_id)
        return {name: sorted(set(ids)) for name, ids in sorted(catalogue.items())}

    def stats(self) -> dict:
        """Counts by status/kind plus the headline numbers for the status bar."""
        with self._lock:
            units = list(self._units.values())
        by_status: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for unit in units:
            by_status[unit.status] = by_status.get(unit.status, 0) + 1
            by_kind[unit.kind] = by_kind.get(unit.kind, 0) + 1
        return {
            "total": len(units),
            "online": by_status.get("online", 0),
            "by_status": dict(sorted(by_status.items())),
            "by_kind": dict(sorted(by_kind.items())),
        }

    # -- internals --------------------------------------------------------
    def _get_locked(self, work_id: str) -> UnitInfo:
        unit = self._units.get(work_id)
        if unit is None:
            raise NotFound(f"unknown unit: {work_id!r}", detail={"work_id": work_id})
        return unit

    def _emit_unit(self, type: str, unit: UnitInfo, **extra: Any) -> EventRecord:
        data = {
            "work_id": unit.work_id,
            "kind": unit.kind,
            "label": unit.label,
            "status": unit.status,
            "project_id": unit.project_id,
            "unit": unit.to_dict(),
        }
        data.update(extra)
        return emit_both(self.ctx, self.eventlog, type, data, unit.project_id)


__all__ = ["UnitInfo", "UnitRegistry", "STATUSES"]

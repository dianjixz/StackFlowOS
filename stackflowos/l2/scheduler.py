"""Scheduler: advice, not authority (§5.4).

``select`` answers "which online unit is the best place to run this ability?"
It never performs the action: the caller still has to ``agent.spawn`` or
``lease.acquire`` explicitly. Scoring is intentionally simple and
deterministic so two hubs agree:

* ability must be offered by the unit (only ``online`` units qualify);
* same project as the caller wins: ``+10``;
* lower active-lease load is better: ``+1/(1+load)``;
* ties break on ``work_id`` so the result is stable.
"""

from __future__ import annotations

import logging
from typing import Any

from .registry import UnitInfo, UnitRegistry

_PROJECT_BONUS = 10.0


class Scheduler:
    """Scores online units for an ability and returns the best candidate."""

    def __init__(self, registry: UnitRegistry) -> None:
        self.registry = registry
        self.log = getattr(registry, "log", logging.getLogger("sfo.l2.scheduler"))
        self._leases: Any = None
        self._leases_resolved = False

    def bind_leases(self, leases: Any) -> None:
        """Attach a LeaseManager for load-aware scoring (optional)."""
        self._leases = leases
        self._leases_resolved = True

    def select(self, ability: str, *, project_id: str | None = None,
               exclude: Any = (), require: dict | None = None) -> UnitInfo | None:
        """Return the best online unit for ``ability``, or ``None``."""
        excluded = set(exclude or ())
        candidates = [
            unit for unit in self.registry.online()
            if unit.work_id not in excluded
            and _has_ability(unit, ability)
            and _matches_require(unit, require)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda unit: (-self.score(unit, project_id=project_id),
                                          unit.work_id))
        return candidates[0]

    def score(self, unit: UnitInfo, *, project_id: str | None = None) -> float:
        """Rank a unit: project affinity plus inverse lease load."""
        score = 0.0
        if project_id is not None and unit.project_id == project_id:
            score += _PROJECT_BONUS
        load = self._lease_load(unit.work_id)
        score += 1.0 / (1.0 + load)
        return score

    # -- internals --------------------------------------------------------
    def _lease_load(self, work_id: str) -> int:
        leases = self._resolve_leases()
        if leases is None:
            return 0
        try:
            return len(leases.list(holder=work_id, active_only=True))
        except Exception:  # scoring must never fail a dispatch
            self.log.debug("lease load lookup failed for %s", work_id,
                           exc_info=True)
            return 0

    def _resolve_leases(self) -> Any:
        if self._leases_resolved:
            return self._leases
        self._leases_resolved = True
        ctx = getattr(self.registry, "ctx", None)
        if ctx is None:
            return None
        try:
            self._leases = ctx.get("leases", None)
        except Exception:
            self._leases = None
        return self._leases


def _has_ability(unit: UnitInfo, ability: str) -> bool:
    for entry in unit.abilities:
        if isinstance(entry, dict):
            name = entry.get("name")
        else:
            name = entry
        if name == ability:
            return True
    return False


def _matches_require(unit: UnitInfo, require: dict | None) -> bool:
    """Require ``key=value`` pairs on metadata/hwinfo (or unit attributes)."""
    if not require:
        return True
    for key, expected in require.items():
        found = False
        for source in (unit.metadata, unit.hwinfo):
            if isinstance(source, dict) and key in source:
                found = True
                if source[key] != expected:
                    return False
        if not found and getattr(unit, str(key), None) != expected:
            return False
    return True


__all__ = ["Scheduler"]

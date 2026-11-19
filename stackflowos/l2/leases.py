"""Leases: time-bounded, revocable claims on named resources (§5.2).

A lease answers the question every long-running agent must answer before it
starts work: *"how long do I own this?"* Resources are plain namespaced
strings (``unit:<work_id>``, ``project:<id>:lead``, ``cap:<name>``,
``file:<abs>``, ``slot:<work_id>:<n>``).

Semantics:

* at most one ``active`` lease per resource; a second acquire is a
  :class:`Conflict` whose detail names the current holder;
* an ``active`` lease may be preempted only when it was declared
  ``preemptible`` and the new request has a strictly higher priority;
* ``sweep`` expires stale leases; ``release_holder`` is the death cleanup
  hook used by the failure domain;
* every transition lands in the event log and on the kernel bus.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from ..errors import BadRequest, Conflict, Denied, NotFound, ValidationError
from ..kernel import Context
from ..protocol import EventRecord, new_id, now_ms as _now_ms
from .eventlog import EventLog, emit_both


@dataclass
class Lease:
    """A holder's exclusive, expiring claim on a resource."""

    id: str
    resource: str
    holder: str
    ttl_s: int
    renewable: bool = True
    preemptible: bool = False
    priority: int = 0
    created_at: int = 0
    expires_at: int = 0
    status: str = "active"    # active|released|expired|preempted
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Lease":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in (d or {}).items() if k in known}
        lease = cls(**data)
        if not isinstance(lease.metadata, dict):
            lease.metadata = {}
        return lease

    def active(self, now_ms: int | None = None) -> bool:
        """True while the lease is held and has not run out of time."""
        if self.status != "active":
            return False
        now = now_ms if now_ms is not None else _now_ms()
        return now < self.expires_at


class LeaseManager:
    """In-memory lease table with TTL sweeping and priority preemption."""

    def __init__(self, ctx: Context, eventlog: EventLog, *,
                 max_ttl_s: int = 86400) -> None:
        self.ctx = ctx
        self.eventlog = eventlog
        self.max_ttl_s = int(max_ttl_s)
        self.log = getattr(ctx, "log", logging.getLogger("sfo.l2.leases"))
        self._leases: dict[str, Lease] = {}
        self._lock = threading.RLock()

    # -- acquire / renew / release ---------------------------------------
    def acquire(self, resource: str, holder: str, ttl_s: int, *,
                renewable: bool = True, preemptible: bool = False,
                priority: int = 0,
                metadata: dict | None = None) -> Lease:
        """Take a lease; conflicts when another holder owns the resource."""
        self._validate_args(resource, holder, ttl_s)
        now = _now_ms()
        ttl = self._clamp_ttl(ttl_s)
        with self._lock:
            existing = self._active_lease_for(resource)
            if existing is not None and not existing.active(now):
                existing.status = "expired"
                expired = existing
            else:
                expired = None
            if expired is not None:
                self._emit("lease.expired", expired, reason="ttl")
                existing = None
            if existing is not None:
                if existing.preemptible and int(priority) > int(existing.priority):
                    existing.status = "preempted"
                    self._emit("lease.preempted", existing,
                               reason=f"preempted by {holder!r}")
                else:
                    raise Conflict(
                        f"resource {resource!r} is held by {existing.holder!r} "
                        f"until {existing.expires_at}",
                        detail={
                            "resource": resource,
                            "holder": existing.holder,
                            "lease_id": existing.id,
                            "expires_at": existing.expires_at,
                            "priority": existing.priority,
                        },
                    )
            lease = Lease(
                id=new_id("lease"),
                resource=str(resource),
                holder=str(holder),
                ttl_s=ttl,
                renewable=bool(renewable),
                preemptible=bool(preemptible),
                priority=int(priority),
                created_at=now,
                expires_at=now + ttl * 1000,
                status="active",
                metadata=dict(metadata or {}),
            )
            self._leases[lease.id] = lease
        self._emit("lease.acquired", lease)
        return lease

    def renew(self, lease_id: str, holder: str,
              ttl_s: int | None = None) -> Lease:
        """Extend a lease; holder must match and the lease must be active."""
        with self._lock:
            lease = self._get_locked(lease_id)
            if not lease.active():
                raise Conflict(
                    f"lease {lease_id!r} is not active (status={lease.status!r})",
                    detail={"lease_id": lease_id, "status": lease.status},
                )
            if lease.holder != holder:
                raise Denied(
                    f"lease {lease_id!r} is held by {lease.holder!r}, not {holder!r}",
                    detail={"lease_id": lease_id, "holder": lease.holder},
                )
            if not lease.renewable:
                raise Denied(
                    f"lease {lease_id!r} is not renewable",
                    detail={"lease_id": lease_id},
                )
            ttl = self._clamp_ttl(ttl_s if ttl_s is not None else lease.ttl_s)
            lease.ttl_s = ttl
            lease.expires_at = _now_ms() + ttl * 1000
        self._emit("lease.renewed", lease)
        return lease

    def release(self, lease_id: str, *, holder: str | None = None,
                reason: str = "released") -> Lease:
        """Release a lease (idempotent for already inactive leases)."""
        with self._lock:
            lease = self._get_locked(lease_id)
            if holder is not None and lease.holder != holder:
                raise Denied(
                    f"lease {lease_id!r} is held by {lease.holder!r}, not {holder!r}",
                    detail={"lease_id": lease_id, "holder": lease.holder},
                )
            if not lease.active():
                return lease
            lease.status = "released"
        self._emit("lease.released", lease, reason=reason)
        return lease

    def release_holder(self, holder: str, *,
                       reason: str = "released") -> list[Lease]:
        """Release every active lease of a holder (unit death cleanup)."""
        with self._lock:
            targets = [lease for lease in self._leases.values()
                       if lease.holder == holder and lease.active()]
            for lease in targets:
                lease.status = "released"
        for lease in targets:
            self._emit("lease.released", lease, reason=reason)
        return targets

    # -- lookup -----------------------------------------------------------
    def get(self, lease_id: str) -> Lease:
        with self._lock:
            return self._get_locked(lease_id)

    def list(self, *, holder: str | None = None,
             resource: str | None = None,
             active_only: bool = True) -> list[Lease]:
        now = _now_ms()
        with self._lock:
            leases = list(self._leases.values())
        result = [
            lease for lease in leases
            if (holder is None or lease.holder == holder)
            and (resource is None or lease.resource == resource)
            and (not active_only or lease.active(now))
        ]
        return sorted(result, key=lambda lease: (lease.created_at, lease.id))

    def holders(self) -> dict[str, list[str]]:
        """Active leases grouped by holder (load view for the scheduler)."""
        grouped: dict[str, list[str]] = {}
        for lease in self.list(active_only=True):
            grouped.setdefault(lease.holder, []).append(lease.resource)
        return {holder: sorted(resources)
                for holder, resources in sorted(grouped.items())}

    # -- expiry -----------------------------------------------------------
    def sweep(self, now_ms: int | None = None) -> list[Lease]:
        """Expire timed-out leases and emit ``lease.expired`` for each."""
        now = now_ms if now_ms is not None else _now_ms()
        expired: list[Lease] = []
        with self._lock:
            for lease in self._leases.values():
                if lease.status == "active" and now >= lease.expires_at:
                    lease.status = "expired"
                    expired.append(lease)
        for lease in expired:
            self._emit("lease.expired", lease, reason="ttl")
        return expired

    # -- internals --------------------------------------------------------
    def _active_lease_for(self, resource: str) -> Lease | None:
        for lease in self._leases.values():
            if lease.resource == resource and lease.status == "active":
                return lease
        return None

    def _get_locked(self, lease_id: str) -> Lease:
        lease = self._leases.get(lease_id)
        if lease is None:
            raise NotFound(f"unknown lease: {lease_id!r}",
                           detail={"lease_id": lease_id})
        return lease

    def _clamp_ttl(self, ttl_s: int) -> int:
        try:
            ttl = int(ttl_s)
        except (TypeError, ValueError) as exc:
            raise BadRequest("lease ttl_s must be an integer") from exc
        if ttl <= 0:
            raise ValidationError("lease ttl_s must be positive",
                                  detail={"ttl_s": ttl_s})
        return min(ttl, self.max_ttl_s)

    def _validate_args(self, resource: str, holder: str, ttl_s: int) -> None:
        if not resource or not isinstance(resource, str):
            raise BadRequest("lease resource must be a non-empty string")
        if not holder or not isinstance(holder, str):
            raise BadRequest("lease holder must be a non-empty string")
        self._clamp_ttl(ttl_s)

    def _emit(self, type: str, lease: Lease, **extra: Any) -> EventRecord:
        data = {
            "lease_id": lease.id,
            "resource": lease.resource,
            "holder": lease.holder,
            "status": lease.status,
            "expires_at": lease.expires_at,
            "lease": lease.to_dict(),
        }
        data.update(extra)
        project_id = lease.metadata.get("project_id") if isinstance(lease.metadata, dict) else None
        return emit_both(self.ctx, self.eventlog, type, data,
                         str(project_id) if project_id else None)


__all__ = ["Lease", "LeaseManager"]

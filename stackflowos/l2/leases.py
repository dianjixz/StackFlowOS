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

Fencing (``cluster.lease_fencing``)
-----------------------------------
Each acquisition of a resource gets a **monotonically increasing fencing
token**.  The token is persisted under ``<state>/state/lease_fencing.json``
(best effort: a write failure is logged and the in-memory counter keeps
increasing -- a restarted hub may then re-issue tokens, which the health
payload reports as ``persisted: false``).  A holder that was paused long
enough for its lease to expire must not act on its stale token: consumers
verify ``(lease_id, token)`` before touching the protected resource.  Renewal
keeps the token; only a new acquisition increments it.

Clock-skew tolerance (``cluster.lease_clock_skew_s``)
----------------------------------------------------
Expiry checks add a grace window: a lease is considered expired only once
``now >= expires_at + clock_skew_s``.  A sweeper whose clock is a little ahead
therefore cannot expire a lease that is still valid for its holder.  ``Lease
.active()`` itself keeps the exact mathematical meaning (``now < expires_at``)
for compatibility; the manager uses the grace-aware check.

Wiring note for the composition root
------------------------------------
``app.py`` currently builds ``LeaseManager(ctx, eventlog)``.  To honour config,
pass::

    LeaseManager(ctx, eventlog,
                 fencing=cfg.cluster.lease_fencing,
                 clock_skew_s=cfg.cluster.lease_clock_skew_s,
                 fencing_path=self.paths.state_dir / "state" / "lease_fencing.json")

Until that wiring lands, the manager reads ``SFO_LEASE_FENCING`` (default on)
and ``SFO_LEASE_CLOCK_SKEW_S`` (default 0) from the environment and derives the
fencing file from the event log's state directory.  ``validate_resource_scope``
is the helper the RPC layer should call before ``lease.acquire`` so a plain
worker cannot take ``project:<other>:lead``.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from ..errors import BadRequest, Conflict, Denied, NotFound, ValidationError
from ..kernel import Context
from ..protocol import EventRecord, new_id, now_ms as _now_ms
from ..util.jsonio import read_json, write_json
from .eventlog import EventLog, emit_both

ENV_FENCING = "SFO_LEASE_FENCING"
ENV_CLOCK_SKEW = "SFO_LEASE_CLOCK_SKEW_S"
ENV_FENCING_PATH = "SFO_LEASE_FENCING_PATH"


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
    #: Monotonic, per-resource fencing token (0 when fencing is disabled).
    token: int = 0
    #: Hub timestamp of the last acquire/renew (clock-skew diagnostics).
    last_renewed_at: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Lease":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in (d or {}).items() if k in known}
        lease = cls(**data)
        if not isinstance(lease.metadata, dict):
            lease.metadata = {}
        lease.token = int(lease.token or 0)
        lease.last_renewed_at = int(lease.last_renewed_at or 0)
        return lease

    def active(self, now_ms: int | None = None) -> bool:
        """True while the lease is held and has not run out of time."""
        if self.status != "active":
            return False
        now = now_ms if now_ms is not None else _now_ms()
        return now < self.expires_at


class LeaseManager:
    """In-memory lease table with TTL sweeping, fencing and preemption."""

    def __init__(self, ctx: Context, eventlog: EventLog, *,
                 max_ttl_s: int = 86400,
                 fencing: bool | None = None,
                 fencing_path: Path | str | None = None,
                 clock_skew_s: float | None = None) -> None:
        self.ctx = ctx
        self.eventlog = eventlog
        self.max_ttl_s = int(max_ttl_s)
        self.log = getattr(ctx, "log", logging.getLogger("sfo.l2.leases"))
        self.fencing = _env_bool(ENV_FENCING, True) if fencing is None \
            else bool(fencing)
        skew = _env_float(ENV_CLOCK_SKEW, 0.0) if clock_skew_s is None \
            else float(clock_skew_s)
        self.clock_skew_s = max(0.0, float(skew))
        self._skew_ms = int(self.clock_skew_s * 1000)
        if fencing_path is None:
            env_path = os.environ.get(ENV_FENCING_PATH, "").strip()
            state_dir = Path(eventlog.events_dir).parent
            fencing_path = (Path(env_path) if env_path
                            else state_dir / "state" / "lease_fencing.json")
        self.fencing_path = Path(fencing_path)
        self._leases: dict[str, Lease] = {}
        self._lock = threading.RLock()
        #: resource -> last issued fencing token.
        self._tokens: dict[str, int] = {}
        #: Whether the last token write reached disk.
        self._token_persist_ok = True
        self._load_tokens()

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
            if existing is not None and not self._is_active(existing, now):
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
                            "token": existing.token,
                            "expires_at": existing.expires_at,
                            "priority": existing.priority,
                        },
                    )
            token = self._next_token_locked(resource)
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
                token=token,
                last_renewed_at=now,
            )
            self._leases[lease.id] = lease
        self._emit("lease.acquired", lease)
        return lease

    def renew(self, lease_id: str, holder: str,
              ttl_s: int | None = None, *, token: int | None = None) -> Lease:
        """Extend a lease; holder must match and the lease must be active.

        When ``token`` is supplied it must equal the lease's fencing token;
        this lets an RPC layer reject a paused caller's stale renewal without a
        separate ``verify`` round-trip.
        """
        with self._lock:
            lease = self._get_locked(lease_id)
            if not self._is_active(lease):
                raise Conflict(
                    f"lease {lease_id!r} is not active (status={lease.status!r})",
                    detail={"lease_id": lease_id, "status": lease.status},
                )
            if lease.holder != holder:
                raise Denied(
                    f"lease {lease_id!r} is held by {lease.holder!r}, not {holder!r}",
                    detail={"lease_id": lease_id, "holder": lease.holder},
                )
            if token is not None and not _token_matches(lease, token):
                raise Denied(
                    f"stale fencing token for lease {lease_id!r}",
                    detail={"lease_id": lease_id, "token": token,
                            "current_token": lease.token},
                )
            if not lease.renewable:
                raise Denied(
                    f"lease {lease_id!r} is not renewable",
                    detail={"lease_id": lease_id},
                )
            ttl = self._clamp_ttl(ttl_s if ttl_s is not None else lease.ttl_s)
            now = _now_ms()
            lease.ttl_s = ttl
            # max(now, last_renewed_at) keeps a lease from *shrinking* when a
            # caller with a slightly-behind clock renews it: the deadline is
            # anchored on the latest server-side observation.
            anchor = max(now, lease.last_renewed_at)
            lease.last_renewed_at = anchor
            lease.expires_at = anchor + ttl * 1000
        self._emit("lease.renewed", lease)
        return lease

    def release(self, lease_id: str, *, holder: str | None = None,
                reason: str = "released", token: int | None = None) -> Lease:
        """Release a lease (idempotent for already inactive leases).

        ``token`` is optional; when supplied a stale token is rejected instead
        of silently releasing an incarnation the caller no longer owns.
        """
        with self._lock:
            lease = self._get_locked(lease_id)
            if holder is not None and lease.holder != holder:
                raise Denied(
                    f"lease {lease_id!r} is held by {lease.holder!r}, not {holder!r}",
                    detail={"lease_id": lease_id, "holder": lease.holder},
                )
            if token is not None and not _token_matches(lease, token):
                raise Denied(
                    f"stale fencing token for lease {lease_id!r}",
                    detail={"lease_id": lease_id, "token": token,
                            "current_token": lease.token},
                )
            if not self._is_active(lease):
                return lease
            lease.status = "released"
        self._emit("lease.released", lease, reason=reason)
        return lease

    def release_holder(self, holder: str, *,
                       reason: str = "released") -> list[Lease]:
        """Release every active lease of a holder (unit death cleanup)."""
        now = _now_ms()
        with self._lock:
            targets = [lease for lease in self._leases.values()
                       if lease.holder == holder and self._is_active(lease, now)]
            for lease in targets:
                lease.status = "released"
        for lease in targets:
            self._emit("lease.released", lease, reason=reason)
        return targets

    # -- fencing ----------------------------------------------------------
    def verify(self, lease_id: str, token: int, *,
               holder: str | None = None) -> bool:
        """True when ``(lease_id, token)`` is the live, holder-owned lease.

        Raises :class:`NotFound` for an unknown lease id.  A mismatch (stale
        token, released/expired lease, or another holder) returns ``False``:
        the caller must abandon the protected action.  This is the check a
        paused process runs before it writes after a suspected pause.
        """
        now = _now_ms()
        with self._lock:
            lease = self._get_locked(lease_id)
            if not self._is_active(lease, now):
                return False
            try:
                if int(token) != int(lease.token):
                    return False
            except (TypeError, ValueError):
                return False
            if holder is not None and str(holder) != lease.holder:
                return False
            return True

    def require(self, lease_id: str, token: int, *,
                holder: str | None = None) -> Lease:
        """Like :meth:`verify` but raises :class:`Denied` instead of ``False``."""
        if self.verify(lease_id, token, holder=holder):
            return self.get(lease_id)
        raise Denied(
            f"stale or invalid fencing token for lease {lease_id!r}",
            detail={"lease_id": lease_id, "token": token},
        )

    def token_for(self, lease_id: str) -> int:
        """Return the fencing token of any known lease."""
        with self._lock:
            return int(self._get_locked(lease_id).token)

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
            and (not active_only or self._is_active(lease, now))
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
        """Expire timed-out leases and emit ``lease.expired`` for each.

        A lease is only expired once the configured clock-skew grace has also
        elapsed, so a scan can never expire a lease that is still valid for its
        holder on a slightly different clock.
        """
        now = now_ms if now_ms is not None else _now_ms()
        expired: list[Lease] = []
        with self._lock:
            for lease in self._leases.values():
                if lease.status == "active" and not self._is_active(lease, now):
                    lease.status = "expired"
                    expired.append(lease)
        for lease in expired:
            self._emit("lease.expired", lease, reason="ttl")
        return expired

    # -- health -----------------------------------------------------------
    def health(self) -> dict:
        """Lease/fencing summary for ``/api/health`` (never raises)."""
        now = _now_ms()
        with self._lock:
            active = [lease for lease in self._leases.values()
                      if self._is_active(lease, now)]
            tokens = dict(self._tokens)
        return {
            "active": len(active),
            "fencing": {
                "enabled": self.fencing,
                "resources": len(tokens),
                "last_token": max(tokens.values()) if tokens else 0,
                "path": str(self.fencing_path),
                "persisted": bool(self._token_persist_ok),
            },
            "clock_skew_s": self.clock_skew_s,
            "max_ttl_s": self.max_ttl_s,
        }

    # -- internals --------------------------------------------------------
    def _is_active(self, lease: Lease, now: int | None = None) -> bool:
        """Grace-aware active check (see the module docstring)."""
        if lease.status != "active":
            return False
        now = _now_ms() if now is None else now
        return now < int(lease.expires_at) + self._skew_ms

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

    # -- fencing persistence ---------------------------------------------
    def _next_token_locked(self, resource: str) -> int:
        if not self.fencing:
            return 0
        token = int(self._tokens.get(str(resource), 0)) + 1
        self._tokens[str(resource)] = token
        self._persist_tokens_locked()
        return token

    def _persist_tokens_locked(self) -> None:
        try:
            self.fencing_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(self.fencing_path,
                       {"version": 1, "tokens": dict(self._tokens)})
            self._token_persist_ok = True
        except OSError as exc:
            self._token_persist_ok = False
            self.log.warning("could not persist lease fencing tokens to %s: %s",
                             self.fencing_path, exc)

    def _load_tokens(self) -> None:
        if not self.fencing:
            return
        try:
            raw = read_json(self.fencing_path, default=None)
        except ValueError as exc:
            self.log.warning("ignoring corrupt lease fencing file %s: %s",
                             self.fencing_path, exc)
            self._token_persist_ok = False
            return
        if not isinstance(raw, dict):
            return
        tokens = raw.get("tokens")
        if not isinstance(tokens, dict):
            return
        for resource, value in tokens.items():
            try:
                token = int(value)
            except (TypeError, ValueError):
                continue
            if token > 0:
                self._tokens[str(resource)] = token

    def _emit(self, type: str, lease: Lease, **extra: Any) -> EventRecord:
        data = {
            "lease_id": lease.id,
            "resource": lease.resource,
            "holder": lease.holder,
            "status": lease.status,
            "expires_at": lease.expires_at,
            "token": lease.token,
            "lease": lease.to_dict(),
        }
        data.update(extra)
        project_id = lease.metadata.get("project_id") if isinstance(lease.metadata, dict) else None
        return emit_both(self.ctx, self.eventlog, type, data,
                         str(project_id) if project_id else None)


def _token_matches(lease: Lease, token: int) -> bool:
    try:
        return int(token) == int(lease.token)
    except (TypeError, ValueError):
        return False


def validate_resource_scope(resource: str, project_id: str | None,
                            is_operator: bool) -> str:
    """Reject cross-project ``project:<id>[:role]`` lease resources.

    ``resource`` follows the architecture namespace.  Only the ``project:``
    prefix is project-scoped:

    * ``project:<id>`` and ``project:<id>:<suffix>`` must name the caller's
      project; a caller without a project scope is rejected;
    * operators (``is_operator`` truthy, or a principal object with an
      ``is_operator`` property) bypass the check;
    * every other resource (``unit:``, ``cap:``, ``file:``, ``slot:`` or a
      custom name) is returned unchanged for backwards compatibility.

    Returns the resource unchanged so call sites can inline it::

        resource = validate_resource_scope(
            str(data["resource"]),
            principal.project_id if principal else None,
            principal.is_operator if principal else False)
    """
    text = str(resource or "")
    operator = bool(getattr(is_operator, "is_operator", is_operator))
    if not text.startswith("project:"):
        return text
    parts = text.split(":")
    target = parts[1].strip() if len(parts) > 1 else ""
    if not target:
        raise BadRequest("project lease resource is missing a project id",
                         detail={"resource": text})
    if operator:
        return text
    if not project_id:
        raise Denied(
            "caller has no project scope for a project-scoped lease",
            detail={"resource": text},
        )
    if str(project_id) != target:
        raise Denied(
            f"lease resource {text!r} belongs to project {target!r}, "
            f"not {project_id!r}",
            detail={"resource": text, "project_id": project_id},
        )
    return text


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "disabled")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


__all__ = ["Lease", "LeaseManager", "validate_resource_scope"]

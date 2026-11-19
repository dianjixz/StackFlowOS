"""Narrow RPC interface used by agents.

An agent never talks to the transport directly: it receives a ``SysAPI``-like
object (real implementation lives in ``units/``) and calls ``await
sys.call(action, data)``.  The helpers in this module implement the approval
dance of docs/architecture.md §6.4 and the event append call used by the tool
executor and runner.

The module deliberately imports nothing from ``transport``/``l2``/``security``
so it can be unit-tested with a fake RPC object.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol, runtime_checkable

from ..errors import Denied, Timeout, error_from_dict

#: Statuses an approval request can carry when it will not change any more.
FINAL_APPROVAL_STATUSES = ("approved", "denied", "expired", "canceled")


@runtime_checkable
class SysAPI(Protocol):
    """Minimal sys-side RPC surface (one method, one dict in, one dict out)."""

    async def call(self, action: str, data: dict | None = None, *,
                   timeout: float | None = None) -> dict:
        ...


async def call_ok(sys: SysAPI, action: str, data: dict | None = None, *,
                  timeout: float | None = None) -> dict:
    """Call ``sys`` and raise if the reply describes an error.

    Real implementations raise :class:`SfoError`; a fake (or a transport that
    returns an ``{"ok": false, "error": {...}}`` envelope) is normalised here.
    """
    result = await sys.call(action, data or {}, timeout=timeout)
    if isinstance(result, dict):
        if result.get("ok") is False and isinstance(result.get("error"), dict):
            raise error_from_dict(result["error"])
        return result
    return {"value": result}


async def poll_approval(sys: SysAPI, request_id: str, *, timeout_s: float = 300,
                        interval_s: float = 0.5) -> dict:
    """Poll ``approval.status`` until the request reaches a final status.

    Raises :class:`Timeout` when the request is still pending after
    ``timeout_s``.  The caller treats anything that is not ``approved`` as a
    denial (fail closed).
    """
    if not request_id:
        raise Denied("approval request id is missing")
    interval = max(0.01, float(interval_s))
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        request = await call_ok(sys, "approval.status", {"request_id": request_id})
        status = str(request.get("status") or "")
        if status in FINAL_APPROVAL_STATUSES:
            return request
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Timeout(
                f"approval {request_id} was not decided within {timeout_s:g}s")
        await asyncio.sleep(min(interval, remaining))


async def request_permission(sys: SysAPI, *, principal: str, capability: str,
                             scope: dict, reason: str, preview: str,
                             project_id: str | None = None,
                             task_id: str | None = None,
                             ttl_s: int | None = None) -> dict:
    """Ask sys for a capability and block until it is approved or refused.

    Returns the approved :class:`ApprovalRequest`-shaped dict.  Raises
    :class:`Denied` for denied/expired requests and :class:`Timeout` when the
    approval did not arrive in time.
    """
    payload: dict[str, Any] = {
        "principal": principal,
        "capability": capability,
        "scope": scope or {},
        "reason": reason,
        "preview": preview or "",
    }
    if project_id is not None:
        payload["project_id"] = project_id
    if task_id is not None:
        payload["task_id"] = task_id
    if ttl_s is not None:
        payload["ttl_s"] = int(ttl_s)

    request = await call_ok(sys, "approval.request", payload)
    status = str(request.get("status") or "")
    request_id = str(request.get("id") or request.get("request_id") or "")

    if status == "approved":
        return request
    if status == "pending":
        if not request_id:
            raise Denied("approval request returned no id")
        request = await poll_approval(
            sys, request_id,
            timeout_s=float(ttl_s) if ttl_s is not None else 300.0)
        if str(request.get("status") or "") == "approved":
            return request
        reason_text = str(request.get("note") or request.get("reason") or "")
        suffix = f" ({reason_text})" if reason_text else ""
        raise Denied(
            f"permission denied: {capability} request={request_id}{suffix}",
            detail={"request_id": request_id})
    reason_text = str(request.get("note") or request.get("reason") or "")
    suffix = f" ({reason_text})" if reason_text else ""
    raise Denied(
        f"permission {status or 'denied'}: {capability} request={request_id}{suffix}",
        detail={"request_id": request_id})


async def emit_event(sys: SysAPI, type: str, data: dict, *,
                     project_id: str | None = None,
                     source: str | None = None) -> None:
    """Append one event through sys (``event.append``).

    Callers that must not fail because of telemetry wrap this in try/except;
    the helper itself propagates :class:`SfoError`.
    """
    payload: dict[str, Any] = {"type": type, "data": data or {}}
    if project_id is not None:
        payload["project_id"] = project_id
    if source is not None:
        payload["source"] = source
    await call_ok(sys, "event.append", payload)


__all__ = [
    "FINAL_APPROVAL_STATUSES",
    "SysAPI",
    "call_ok",
    "poll_approval",
    "request_permission",
    "emit_event",
]

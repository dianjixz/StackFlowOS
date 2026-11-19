"""Error types shared by every layer of StackFlowOS.

The transport carries errors as ``{"code": ..., "message": ..., "detail": ...}``
objects; every layer raises one of the exceptions below and the RPC server
turns it into that shape. Keep the codes stable: they are part of the
protocol contract (see docs/protocol.md).
"""

from __future__ import annotations

from typing import Any


class SfoError(Exception):
    """Base error. ``code`` is a stable machine-readable identifier."""

    code = "INTERNAL"
    http_status = 500

    def __init__(self, message: str, *, code: str | None = None,
                 detail: Any = None, http_status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.detail = detail
        if http_status is not None:
            self.http_status = http_status

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


class BadRequest(SfoError):
    code = "BAD_REQUEST"
    http_status = 400


class ValidationError(SfoError):
    code = "VALIDATION"
    http_status = 400


class NotFound(SfoError):
    code = "NOT_FOUND"
    http_status = 404


class Conflict(SfoError):
    code = "CONFLICT"
    http_status = 409


class Denied(SfoError):
    """The permission system refused the action."""

    code = "DENIED"
    http_status = 403


class Unauthorized(SfoError):
    code = "UNAUTHORIZED"
    http_status = 401


class LeaseExpired(SfoError):
    code = "LEASE_EXPIRED"
    http_status = 409


class Timeout(SfoError):
    code = "TIMEOUT"
    http_status = 504


class Unavailable(SfoError):
    code = "UNAVAILABLE"
    http_status = 503


class InternalError(SfoError):
    code = "INTERNAL"
    http_status = 500


class ConfigError(ValidationError):
    code = "CONFIG_INVALID"


class PermissionPending(SfoError):
    """A capability request is queued for human approval."""

    code = "PERMISSION_PENDING"
    http_status = 202


def error_from_dict(payload: dict) -> SfoError:
    code = str(payload.get("code") or "INTERNAL")
    message = str(payload.get("message") or code)
    detail = payload.get("detail")
    mapping = {
        "BAD_REQUEST": BadRequest,
        "VALIDATION": ValidationError,
        "NOT_FOUND": NotFound,
        "CONFLICT": Conflict,
        "DENIED": Denied,
        "UNAUTHORIZED": Unauthorized,
        "LEASE_EXPIRED": LeaseExpired,
        "TIMEOUT": Timeout,
        "UNAVAILABLE": Unavailable,
    }
    cls = mapping.get(code, SfoError)
    err = cls(message, detail=detail)
    err.code = code
    return err

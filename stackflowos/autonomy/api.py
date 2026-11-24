"""HTTP routes for the autonomy subsystem.

Contract: ``docs/team/autonomy-plan.md`` §4 and ``docs/autonomy.md``.  The
extension object (:class:`~stackflowos.autonomy.service.AutonomyService`)
returns these routes from ``routes()``; ``WebAPI._build_routes`` registers them,
so ``web/api.py`` stays a composition root.

Every handler has the extension signature
``async (match, query, body) -> (status, payload)`` and returns the shared
``{"ok": True, "data": ...}`` envelope.  Failures are raised as
``stackflowos.errors`` (``BadRequest``/``Denied``) and mapped to HTTP by
``WebAPI``; handlers never turn a client mistake into a 500.

Authorization:
- reads (GET) need an authenticated admission decision;
- writes (POST/PATCH) additionally need the ``operator`` role.  A viewer is
  explicitly read-only, mirroring ``stackflowos/security/sessions.py``.
"""

from __future__ import annotations

from typing import Any

from ..errors import BadRequest, Denied
from ..web.api import _as_bool, current_auth

__all__ = ["build_routes"]

ROLE_OPERATOR = "operator"


def _require_authenticated() -> Any:
    """Return the admission decision or refuse an anonymous request."""
    auth = current_auth()
    if auth is None or getattr(auth, "authenticated", False) is not True:
        raise Denied("authentication required")
    return auth


def _require_operator() -> Any:
    """Require an authenticated operator (viewers are read-only)."""
    auth = _require_authenticated()
    if str(getattr(auth, "role", "") or "") != ROLE_OPERATOR:
        raise Denied("operator role required")
    return auth


def build_routes(service: Any) -> list[tuple[str, str, Any]]:
    """Build ``(method, regex_pattern, handler)`` tuples for the autonomy API.

    ``service`` only needs the ``status()``, ``run_patrol(*, trigger, dry_run)``
    and ``configure(patch)`` methods; tests pass a recording fake.
    """

    async def get_status(match: Any, query: dict, body: dict
                         ) -> tuple[int, dict]:
        _require_authenticated()
        return 200, {"ok": True, "data": service.status()}

    async def run_patrol(match: Any, query: dict, body: dict
                         ) -> tuple[int, dict]:
        _require_operator()
        # ``POST /api/autonomy/patrol`` accepts an empty body; ``dry_run`` is
        # parsed strictly so a typo ("false" is a real boolean spelling, but
        # "maybe" is not) is a 400 instead of a silent full patrol.
        payload = body if isinstance(body, dict) else {}
        dry_run = _as_bool(payload.get("dry_run"), default=False)
        report = await service.run_patrol(trigger="operator", dry_run=dry_run)
        return 200, {"ok": True, "data": report}

    async def patch_policy(match: Any, query: dict, body: dict
                           ) -> tuple[int, dict]:
        _require_operator()
        if not isinstance(body, dict):
            raise BadRequest(
                "autonomy policy patch must be a non-empty object")
        # ``configure`` owns validation (empty object, unknown fields, enum
        # ranges) and returns the fresh status; re-read it so the reply is the
        # post-patch view even if a service implementation returns something
        # else (or nothing).
        await service.configure(body)
        return 200, {"ok": True, "data": service.status()}

    # Longest paths first: ``WebAPI`` iterates in registration order and uses
    # ``fullmatch``, so order only matters for readability here.
    return [
        ("GET", r"/api/autonomy/status", get_status),
        ("GET", r"/api/autonomy", get_status),
        ("POST", r"/api/autonomy/patrol", run_patrol),
        ("PATCH", r"/api/autonomy/policy", patch_policy),
    ]

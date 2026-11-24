"""Autonomy: the self-driving loop (patrol → dispatch → bounded approval).

The package is an extension (see :mod:`stackflowos.extensions`): importing the
package is cheap and never pulls in the hub.  ``build(app)`` creates the single
:class:`~stackflowos.autonomy.service.AutonomyService` and attaches it as
``app.autonomy``.
"""

from __future__ import annotations

from typing import Any


def build(app: Any) -> Any:
    """Extension entry point used by :mod:`stackflowos.extensions`."""
    existing = getattr(app, "autonomy", None)
    if existing is not None and getattr(existing, "name", "") == "autonomy":
        return existing
    from .service import AutonomyService

    return AutonomyService(app)


__all__ = ["build"]

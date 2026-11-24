"""Dispatch: turn findings into tasks and workers, within quotas.

Implementation contract: see ``docs/team/autonomy-plan.md`` §3.3.
"""

from __future__ import annotations

from typing import Any

from .models import Finding


class Dispatcher:
    """Bounded executor for dispatchable findings."""

    def __init__(self, app: Any, config: Any, state: dict, *,
                 now: Any = None) -> None:
        self.app = app
        self.config = config
        self.state = state
        self._now = now

    async def apply(self, findings: list[Finding]) -> list[dict]:
        raise NotImplementedError("dispatcher.Dispatcher.apply: see plan §3.3")


__all__ = ["Dispatcher"]

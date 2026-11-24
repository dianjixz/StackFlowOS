"""Guardian: bounded, audited self-approval of pending requests.

Implementation contract: see ``docs/team/autonomy-plan.md`` §3.3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Verdict:
    """One guardian decision about one pending approval request."""

    request_id: str
    decision: str          # allow | observe | skip
    reason: str
    capability: str = ""
    principal: str = ""
    risk: str = ""
    project_id: str | None = None


class Guardian:
    """Evaluate pending approvals against the operator's envelope."""

    def __init__(self, app: Any, config: Any, state: dict, *,
                 now: Any = None) -> None:
        self.app = app
        self.config = config
        self.state = state
        self._now = now

    def evaluate(self, request: dict) -> Verdict:
        raise NotImplementedError("guardian.Guardian.evaluate: see plan §3.3")

    async def apply(self, requests: list[dict] | None = None) -> list[dict]:
        raise NotImplementedError("guardian.Guardian.apply: see plan §3.3")


__all__ = ["Guardian", "Verdict"]

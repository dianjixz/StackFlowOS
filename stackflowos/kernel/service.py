"""Minimal service base class (Cordis ``Service`` analogue).

Subclasses declare a stable service name and are registered on the context
passed to ``__init__``. The registration disappears with the owning context,
so a service never outlives the plugin scope that created it.
"""

from __future__ import annotations

import logging
from typing import Any

from .context import Context


class Service:
    #: Service name visible through ``ctx.get(name)``.
    provide: str | None = None

    def __init__(self, ctx: Context, name: str | None = None) -> None:
        self.ctx = ctx
        self.name = name or self.provide or type(self).__name__.lower()
        self.log: logging.Logger = ctx.log
        ctx.provide(self.name, self)

    def effect(self, execute, label: str = "anonymous"):
        return self.ctx.effect(execute, label)

    async def aclose(self) -> None:
        """Optional async cleanup hook; contexts call it on dispose."""
        return None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"

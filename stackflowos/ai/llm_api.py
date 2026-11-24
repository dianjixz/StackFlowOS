"""Extension: LLM usage/quota and model-status API.

This module follows the optional-extension contract in
``stackflowos/extensions.py``; the composition root must list
``stackflowos.ai.llm_api`` in ``EXTENSION_MODULES`` (Lead-owned file).  It
deliberately does not touch ``web/api.py``.

Routes
------
``GET /api/llm/usage?project_id=``
    Token/cost aggregates by project/role/provider/model, the project budget,
    live RPM/TPM/concurrency counters and circuit-breaker states.

``GET /api/llm/models``
    Configured primary provider, role routes and fallback chain with cached
    health (last latency/failures/circuit).  API keys are never included.

No new streaming endpoint is needed: SSE clients already receive ephemeral
``chat.delta`` / ``agent.chat.delta`` events from ``/api/events/stream``.
"""

from __future__ import annotations

import time
from typing import Any

from ..errors import BadRequest
from ..util.logging import get_logger
from .telemetry import get_runtime

_log = get_logger("ai.llm_api")


def _ok(data: Any) -> tuple[int, dict]:
    return 200, {"ok": True, "data": data}


def _fail(message: str, *, status: int = 400,
          code: str = "BAD_REQUEST") -> tuple[int, dict]:
    return status, {"ok": False, "error": {"code": code, "message": message}}


def _provider_entry(provider: Any) -> dict:
    describe = getattr(provider, "describe", None)
    if callable(describe):
        entry = describe()
    else:
        health = getattr(provider, "health", None)
        entry = {
            "name": getattr(provider, "name", "?"),
            "provider": getattr(provider, "name", "?"),
            "model": getattr(provider, "model", ""),
            "base_url": getattr(provider, "base_url", ""),
            "health": health() if callable(health) else {},
        }
    entry.pop("api_key", None)
    health = entry.get("health") if isinstance(entry.get("health"), dict) else {}
    breaker = getattr(provider, "breaker", None)
    if breaker is not None and hasattr(breaker, "snapshot"):
        entry.setdefault("circuit", breaker.snapshot())
    limiter = getattr(provider, "limiter", None)
    if limiter is not None and hasattr(limiter, "snapshot"):
        entry.setdefault("limits", limiter.snapshot())
    entry["ok"] = bool(health.get("ok", True))
    entry["last_latency_ms"] = health.get("last_latency_ms")
    entry["last_error"] = health.get("last_error") or ""
    return entry


class LLMExtension:
    """Usage/quota and model catalogue routes for the LLM subsystem."""

    name = "llm_api"

    def __init__(self, app: Any) -> None:
        self.app = app
        self.log = get_logger("ai.llm_api")
        self._attach()
        self._started_at = int(time.time() * 1000)

    # -- lifecycle --------------------------------------------------------
    def _attach(self) -> None:
        provider = getattr(self.app, "provider", None)
        if provider is None:
            return
        try:
            from .llm import attach_runtime
            attach_runtime(provider, self.app)
        except Exception:  # telemetry must not block the endpoint
            self.log.debug("could not attach llm runtime", exc_info=True)

    async def start(self) -> None:
        self._attach()

    async def on_config(self, config: Any) -> None:
        self._attach()

    # -- provider introspection ------------------------------------------
    def _provider(self) -> Any:
        provider = getattr(self.app, "provider", None)
        if provider is None:
            raise BadRequest("no llm provider configured")
        return provider

    @staticmethod
    def _provider_list(provider: Any) -> list[dict]:
        entries = [dict(_provider_entry(provider), role="primary")]
        for role, route_provider, spec in getattr(provider, "routes", []) or []:
            entry = _provider_entry(route_provider)
            entry["role"] = role
            entries.append(entry)
        for fallback_provider in getattr(provider, "fallback", []) or []:
            entry = _provider_entry(fallback_provider)
            entry["role"] = "fallback"
            entries.append(entry)
        return entries

    # -- routes -----------------------------------------------------------
    def routes(self) -> list[tuple[str, str, Any]]:
        return [
            ("GET", r"/api/llm/usage", self.usage),
            ("GET", r"/api/llm/models", self.models),
        ]

    async def usage(self, match: Any, query: dict,
                    body: Any) -> tuple[int, dict]:
        project_id = None
        if isinstance(query, dict) and query.get("project_id"):
            project_id = str(query["project_id"]).strip() or None
        provider = self._provider()
        runtime = get_runtime()
        snapshot = runtime.usage.snapshot(project_id)
        cfg = getattr(self.app, "config", None)
        llm_cfg = getattr(cfg, "llm", None)
        budget = int(getattr(llm_cfg, "project_token_budget", 0) or 0)
        used = int((snapshot.get("project") or {}).get("total") or 0) \
            if project_id else int((snapshot.get("totals") or {}).get("total") or 0)
        remaining = max(0, budget - used) if budget else None
        entries = self._provider_list(provider)
        limits = None
        circuit = None
        for entry in entries:
            if entry.get("role") == "primary":
                limits = entry.get("limits")
                circuit = entry.get("circuit")
                break
        return _ok({
            "project_id": project_id,
            "totals": snapshot.get("totals") or {},
            "projects": snapshot.get("projects") or {},
            "roles": snapshot.get("roles") or {},
            "providers": snapshot.get("providers") or {},
            "models": snapshot.get("models") or {},
            "recent": snapshot.get("recent") or [],
            "project": snapshot.get("project"),
            "cost": (snapshot.get("totals") or {}).get("cost", 0.0),
            "budget": {
                "project_token_budget": budget,
                "used": used,
                "remaining": remaining,
                "exceeded": bool(budget and used >= budget),
            },
            "limits": limits or {},
            "circuit": circuit or {},
            "providers_health": entries,
            "updated_at": snapshot.get("updated_at", 0),
            "generated_at": int(time.time() * 1000),
        })

    async def models(self, match: Any, query: dict,
                     body: Any) -> tuple[int, dict]:
        provider = self._provider()
        cfg = getattr(self.app, "config", None)
        llm_cfg = getattr(cfg, "llm", None)
        entries = self._provider_list(provider)
        health = getattr(provider, "health", None)
        return _ok({
            "primary": entries[0] if entries else _provider_entry(provider),
            "providers": entries,
            "health": health() if callable(health) else {},
            "configured": {
                "provider": getattr(llm_cfg, "provider", None),
                "model": getattr(llm_cfg, "model", None),
                "base_url": getattr(llm_cfg, "base_url", None),
                "stream": getattr(llm_cfg, "stream", None),
                "rpm": getattr(llm_cfg, "rpm", None),
                "tpm": getattr(llm_cfg, "tpm", None),
                "max_concurrency": getattr(llm_cfg, "max_concurrency", None),
                "max_context_tokens": getattr(llm_cfg, "max_context_tokens", None),
                "project_token_budget": getattr(
                    llm_cfg, "project_token_budget", None),
            },
            "routes": [
                {key: value for key, value in {
                    "role": entry.get("role"),
                    "provider": entry.get("provider") or entry.get("name"),
                    "model": entry.get("model"),
                    "base_url": entry.get("base_url"),
                    "ok": entry.get("ok"),
                    "circuit": entry.get("circuit"),
                }.items() if key == "role" or value is not None}
                for entry in entries if entry.get("role") not in
                ("primary", "fallback")
            ],
            "fallback": [
                {"provider": entry.get("provider") or entry.get("name"),
                 "model": entry.get("model"),
                 "ok": entry.get("ok")}
                for entry in entries if entry.get("role") == "fallback"
            ],
            "generated_at": int(time.time() * 1000),
        })


def build(app: Any) -> LLMExtension:
    """Extension entry point (called by ``extensions.load_extensions``)."""
    return LLMExtension(app)


__all__ = ["LLMExtension", "build"]

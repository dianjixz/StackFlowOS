"""Role routing, fallback chains and circuit breaking across providers.

``Router`` is itself provider-shaped (``chat`` / ``chat_with_tools`` /
``stream`` / ``ping`` / ``health``), so ``ChatService``, ``AgentChatService``
and ``LLMBrain`` do not need to know whether they are talking to one provider
or several.

Selection order for a request:

1. the first ``llm.routes`` entry whose ``role`` matches (or whose ``roles``
   list contains the role); otherwise the primary provider;
2. then every ``llm.fallback`` entry in order.

A provider whose breaker is open is skipped (moving to the next candidate); a
project budget violation is terminal and never falls back (fail closed).
"""

from __future__ import annotations

import time
from typing import Any, AsyncIterator, Callable

from ..errors import SfoError, Unavailable
from ..util.logging import get_logger
from .factory import create_provider, merge_specs, spec_from_config
from .providers import BaseProvider, MockProvider, _is_transient
from .telemetry import BudgetExceeded, estimate_messages_tokens, get_runtime
from .types import LLMResult

_log = get_logger("ai.router")


class Router:
    """Provider facade that selects by role and fails over."""

    name = "router"

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        base_spec = spec_from_config(cfg)
        self.primary_spec = base_spec
        self.primary = create_provider(base_spec)
        self.routes: list[tuple[str, Any, dict]] = []
        self.fallback: list[Any] = []
        self.route_specs: list[dict] = []
        self.fallback_specs: list[dict] = []
        for entry in getattr(cfg, "routes", None) or []:
            if not isinstance(entry, dict):
                continue
            spec = merge_specs(base_spec, entry)
            provider = create_provider(spec)
            roles = entry.get("roles")
            if isinstance(roles, list):
                role_names = [str(item) for item in roles]
            else:
                role_names = [str(entry.get("role") or "")]
            for role_name in role_names:
                if role_name:
                    self.routes.append((role_name, provider, spec))
            self.route_specs.append({**spec, "role": entry.get("role"),
                                     "roles": roles})
        for entry in getattr(cfg, "fallback", None) or []:
            if not isinstance(entry, dict):
                continue
            spec = merge_specs(base_spec, entry)
            self.fallback.append(create_provider(spec))
            self.fallback_specs.append(spec)
        self.model = getattr(self.primary, "model", "")
        self.stream_enabled = bool(getattr(cfg, "stream", True))
        self.max_context_tokens = int(
            getattr(cfg, "max_context_tokens", 128_000) or 128_000)
        self.max_tokens = int(getattr(cfg, "max_tokens", 4096) or 4096)
        self.project_token_budget = int(
            getattr(cfg, "project_token_budget", 0) or 0)
        self.runtime = get_runtime()
        self._last_used: str | None = None
        self._last_error = ""

    # -- lifecycle --------------------------------------------------------
    def attach_runtime(self, runtime: Any) -> None:
        self.runtime = runtime
        for provider in self.all_providers():
            hook = getattr(provider, "attach_runtime", None)
            if callable(hook):
                hook(runtime)
            else:
                try:
                    provider.runtime = runtime
                except Exception:  # pragma: no cover - defensive
                    pass

    def all_providers(self) -> list[Any]:
        seen: list[Any] = []
        for provider in [self.primary] + [item[1] for item in self.routes] \
                + list(self.fallback):
            if provider not in seen:
                seen.append(provider)
        return seen

    # -- selection --------------------------------------------------------
    def _route_for(self, role: str | None) -> Any | None:
        if not role:
            return None
        for route_role, provider, _spec in self.routes:
            if route_role == str(role):
                return provider
        return None

    def _candidates(self, role: str | None) -> list[Any]:
        candidates: list[Any] = []
        selected = self._route_for(role) or self.primary
        candidates.append(selected)
        if selected is not self.primary and self.primary not in candidates:
            candidates.append(self.primary)
        for provider in self.fallback:
            if provider not in candidates:
                candidates.append(provider)
        return candidates

    # -- provider interface ----------------------------------------------
    async def chat_with_tools(
            self, messages: list[dict], tools: list[dict] | None = None, *,
            tool_choice: Any = None,
            role: str | None = None,
            project_id: str | None = None,
            on_delta: Callable[[dict], Any] | None = None,
            stream: bool | None = None) -> LLMResult:
        self._check_budget(messages, project_id)
        candidates = self._candidates(role)
        last_error: BaseException | None = None
        for provider in candidates:
            breaker = getattr(provider, "breaker", None)
            if breaker is not None and hasattr(breaker, "available") \
                    and not breaker.available():
                _log.info("skipping %s: circuit %s", getattr(provider, "name", "?"),
                          breaker.state)
                continue
            try:
                result = await provider.chat_with_tools(
                    messages, tools=tools, tool_choice=tool_choice,
                    role=role, project_id=project_id, on_delta=on_delta,
                    stream=stream)
            except BudgetExceeded:
                raise
            except SfoError as exc:
                last_error = exc
                _log.info("provider %s failed (%s); trying next",
                          getattr(provider, "name", "?"), exc)
                continue
            else:
                self._last_used = str(getattr(provider, "name", ""))
                self._last_error = ""
                return result
        if last_error is not None:
            raise last_error
        raise Unavailable("LLM unavailable: all providers are circuit-open")

    async def chat(self, messages: list[dict], *,
                   tools: list[dict] | None = None,
                   on_delta: Callable[[dict], Any] | None = None,
                   role: str | None = None,
                   project_id: str | None = None) -> str:
        _ = tools
        result = await self.chat_with_tools(
            messages, role=role, project_id=project_id, on_delta=on_delta)
        if not result.text and not result.tool_calls:
            raise Unavailable("LLM returned an empty message")
        return result.text

    async def stream(self, messages: list[dict],
                     tools: list[dict] | None = None, *,
                     tool_choice: Any = None,
                     role: str | None = None,
                     project_id: str | None = None
                     ) -> AsyncIterator[dict]:
        self._check_budget(messages, project_id)
        last_error: BaseException | None = None
        for provider in self._candidates(role):
            breaker = getattr(provider, "breaker", None)
            if breaker is not None and hasattr(breaker, "available") \
                    and not breaker.available():
                continue
            yielded = False
            try:
                async for delta in provider.stream(
                        messages, tools=tools, tool_choice=tool_choice,
                        role=role, project_id=project_id):
                    yielded = True
                    yield delta
                self._last_used = str(getattr(provider, "name", ""))
                return
            except BudgetExceeded:
                raise
            except SfoError as exc:
                last_error = exc
                if yielded:
                    # Never replay a partially streamed answer elsewhere.
                    raise
                _log.info("stream provider %s failed (%s); trying next",
                          getattr(provider, "name", "?"), exc)
                continue
        if last_error is not None:
            raise last_error
        raise Unavailable("LLM unavailable: all providers are circuit-open")

    async def ping(self) -> dict:
        result = await self.primary.ping()
        result["routes"] = [
            {"role": role, "provider": getattr(provider, "name", ""),
             "model": getattr(provider, "model", "")}
            for role, provider, _spec in self.routes
        ]
        return result

    # -- budget ------------------------------------------------------------
    def _check_budget(self, messages: list[dict],
                      project_id: str | None) -> None:
        if not project_id or not self.project_token_budget:
            return
        self.runtime.usage.check_budget(
            project_id, estimate_messages_tokens(messages),
            self.project_token_budget)

    # -- introspection ------------------------------------------------------
    @property
    def usage(self) -> dict:
        """Aggregate usage snapshot (convenience for the API/tests)."""
        return self.runtime.usage.snapshot()

    def usage_snapshot(self, project_id: str | None = None) -> dict:
        return self.runtime.usage.snapshot(project_id)

    @staticmethod
    def _provider_entry(provider: Any) -> dict:
        describe = getattr(provider, "describe", None)
        if callable(describe):
            entry = describe()
        else:
            health = getattr(provider, "health", None)
            entry = {
                "name": getattr(provider, "name", "?"),
                "model": getattr(provider, "model", ""),
                "health": health() if callable(health) else {},
            }
        breaker = getattr(provider, "breaker", None)
        if breaker is not None and hasattr(breaker, "snapshot"):
            entry.setdefault("circuit", breaker.snapshot())
        return entry

    def health(self) -> dict:
        entries = [self._provider_entry(provider)
                   for provider in self.all_providers()]
        ok = any(bool((entry.get("health") or {}).get("ok", True))
                 for entry in entries)
        return {
            "ok": ok,
            "provider": "router",
            "model": self.model,
            "last_used": self._last_used,
            "last_error": self._last_error,
            "providers": entries,
            "routes": [
                {"role": role,
                 "provider": getattr(provider, "name", ""),
                 "model": getattr(provider, "model", "")}
                for role, provider, _spec in self.routes
            ],
            "fallback": [
                {"provider": getattr(provider, "name", ""),
                 "model": getattr(provider, "model", "")}
                for provider in self.fallback
            ],
            "updated_at": int(time.time() * 1000),
        }

    def describe(self) -> dict:
        return self.health()

    def provider_for(self, role: str | None) -> Any:
        """Exposed for tests/API: the provider a role resolves to."""
        return self._route_for(role) or self.primary


__all__ = ["Router"]

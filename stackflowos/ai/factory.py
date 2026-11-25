"""Provider construction shared by ``make_provider`` and the router.

A *spec* is a plain dict using the same field names as ``LLMCfg`` plus the
optional per-route fields (``role``/``roles``, ``price_per_1k_input``,
``price_per_1k_output``, ``circuit_threshold``, ``circuit_cooldown_s``).
Keeping this in one place means a route/fallback entry is validated exactly
like the primary provider and tests can construct providers without a Config.
"""

from __future__ import annotations

from typing import Any

from ..errors import BadRequest
from .providers import (AnthropicProvider, BaseProvider, GeminiProvider,
                        MockProvider, OpenAICompatibleProvider)

_COMMON_KEYS = (
    "base_url", "model", "api_key", "temperature", "max_tokens", "timeout_s",
    "retries", "retry_backoff_s", "stream", "rpm", "tpm", "max_concurrency",
    "max_context_tokens", "project_token_budget", "price_per_1k_input",
    "price_per_1k_output", "circuit_threshold", "circuit_cooldown_s",
)

_PROVIDER_ALIASES = {
    "": "mock", "mock": "mock",
    "openai": "openai", "openai-compatible": "openai",
    "openai_compatible": "openai",
    "anthropic": "anthropic", "claude": "anthropic",
    "gemini": "gemini", "google": "gemini",
}


def spec_from_config(cfg: Any) -> dict:
    """Turn an ``LLMCfg``-like object into a provider spec dict."""
    return {
        "provider": str(getattr(cfg, "provider", "mock") or "mock"),
        **{key: getattr(cfg, key) for key in _COMMON_KEYS
           if hasattr(cfg, key)},
    }


def merge_specs(base: dict, override: dict) -> dict:
    """Merge a route/fallback entry over the primary provider spec."""
    merged = dict(base)
    for key, value in (override or {}).items():
        if value is not None:
            merged[key] = value
    provider = str((override or {}).get("provider") or base.get("provider")
                   or "mock").strip().lower()
    merged["provider"] = provider
    base_provider = str(base.get("provider") or "mock").strip().lower()
    if provider != base_provider:
        # Never silently point a different vendor at the primary base URL or
        # key; the entry must name its own endpoint/key when it changes vendor.
        if not (override or {}).get("base_url"):
            merged["base_url"] = ""
        if not (override or {}).get("api_key"):
            merged["api_key"] = ""
    return merged


def normalise_provider_name(value: Any) -> str:
    raw = str(value if value is not None else "mock").strip().lower()
    name = _PROVIDER_ALIASES.get(raw)
    if name is None:
        raise BadRequest(f"unsupported llm provider: {raw!r}")
    return name


def provider_kwargs(spec: dict) -> dict:
    """Return only the keyword arguments a provider constructor accepts."""
    allowed = set(_COMMON_KEYS) | {"model", "stream"}
    return {key: spec[key] for key in allowed if key in spec}


def create_provider(spec: dict) -> Any:
    """Build one provider from a spec dict."""
    name = normalise_provider_name(spec.get("provider"))
    kwargs = provider_kwargs(spec)
    kwargs["stream"] = bool(spec.get("stream", True))
    if name == "mock":
        mock_keys = {"model", "stream", "rpm", "tpm", "max_concurrency",
                     "max_context_tokens", "project_token_budget",
                     "max_tokens", "price_per_1k_input", "price_per_1k_output",
                     "circuit_threshold", "circuit_cooldown_s"}
        return MockProvider(**{key: value for key, value in kwargs.items()
                               if key in mock_keys})
    kwargs.pop("base_url", None)
    kwargs.pop("model", None)
    if name == "openai":
        return OpenAICompatibleProvider(
            base_url=str(spec.get("base_url") or ""),
            model=str(spec.get("model") or ""), **kwargs)
    if name == "anthropic":
        return AnthropicProvider(
            base_url=str(spec.get("base_url") or ""),
            model=str(spec.get("model") or ""), **kwargs)
    if name == "gemini":
        return GeminiProvider(
            base_url=str(spec.get("base_url") or ""),
            model=str(spec.get("model") or ""), **kwargs)
    raise BadRequest(f"unsupported llm provider: {name!r}")  # pragma: no cover


__all__ = [
    "create_provider",
    "merge_specs",
    "normalise_provider_name",
    "provider_kwargs",
    "spec_from_config",
]

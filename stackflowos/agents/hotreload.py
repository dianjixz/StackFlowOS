"""Live configuration for running agent workers.

The hub owns the persisted config.  A running worker must follow a live change
without a restart:

* the hub pushes ``unit.configure`` to every online unit from
  ``AgentSupervisor.on_config`` (called by ``App.update_config``);
* optionally (``agents.config_reload_s > 0``) the worker polls
  ``sys.config.get`` as a reconciliation backstop, so a missed push converges.

The payload never contains secrets.  Only whitelisted runtime fields travel to
the worker: agent limits and the LLM provider/model/base URL plus non-secret
route fields.  API keys stay in the worker's environment/config file unless the
operator only changed non-secret route fields (``routes`` entries are merged
field-by-field, keys preserved).

``apply_runtime_config`` mutates the worker's in-memory :class:`Config` in
place; the runner re-reads ``task_timeout_s`` / ``max_tool_calls_per_task`` /
``max_steps`` every step, so no restart is needed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("sfo.agents.hotreload")

#: Agent config fields safe to push/mutate live.
AGENT_RUNTIME_FIELDS = (
    "heartbeat_s", "offline_after_s", "max_workers", "max_steps",
    "task_poll_s", "task_timeout_s", "max_tool_calls_per_task", "queue_max",
    "config_reload_s", "aggregator_interval_s",
)

#: Primary LLM fields safe to push/mutate live (``api_key`` excluded).
LLM_RUNTIME_FIELDS = (
    "provider", "model", "base_url", "temperature", "max_tokens",
    "timeout_s", "stream", "rpm", "tpm", "max_concurrency",
    "max_context_tokens", "project_token_budget", "retries", "retry_backoff_s",
    "price_per_1k_input", "price_per_1k_output",
    "circuit_threshold", "circuit_cooldown_s",
)

#: Name fragments that mark a secret field worth stripping from nested data.
_SECRET_FRAGMENTS = ("key", "secret", "token", "password", "credential")


def _section(value: Any) -> dict:
    """Return a plain dict view of a config section (dataclass or mapping)."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    data: dict = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            item = getattr(value, name)
        except Exception:  # pragma: no cover - exotic attributes
            continue
        if callable(item):
            continue
        data[name] = item
    return data


def _is_secret_field(name: str) -> bool:
    lowered = str(name).lower()
    return any(fragment in lowered for fragment in _SECRET_FRAGMENTS)


def _sanitize_route(route: Any) -> dict:
    """Copy one route/fallback entry without credentials or unknown objects."""
    if not isinstance(route, dict):
        return {}
    return {str(key): value for key, value in route.items()
            if not _is_secret_field(str(key))
            and isinstance(value, (str, int, float, bool, type(None)))}


def public_runtime_config(config: Any) -> dict:
    """Non-secret runtime view of a full ``Config`` pushed to workers.

    Accepts the full config or an ``agents`` section for convenience.  The
    returned dict is JSON-serialisable and safe to put into an RPC payload.
    """
    agents = _section(getattr(config, "agents", config))
    llm = _section(getattr(config, "llm", None))
    runtime_agents = {name: agents[name] for name in AGENT_RUNTIME_FIELDS
                      if name in agents}
    runtime_llm = {name: llm[name] for name in LLM_RUNTIME_FIELDS
                   if name in llm and not _is_secret_field(name)}
    for nested in ("routes", "fallback"):
        entries = llm.get(nested)
        if isinstance(entries, list):
            runtime_llm[nested] = [_sanitize_route(item) for item in entries]
    return {"agents": runtime_agents, "llm": runtime_llm}


def _old_routes(config: Any) -> list[dict]:
    llm = getattr(config, "llm", None)
    current = getattr(llm, "routes", None) or []
    return [item for item in current if isinstance(item, dict)]


def _merge_routes(existing: list[dict], incoming: list[Any]) -> list[dict]:
    """Field-by-field merge that keeps existing API keys and local extras.

    Incoming entries are matched to existing ones by ``role`` then ``provider``
    then index; only non-secret scalars from the payload are written.  This lets
    an operator change ``model``/``base_url`` for a route without re-sending its
    key (which is never included in the hub payload anyway).
    """
    merged: list[dict] = [dict(item) for item in existing]
    for index, raw in enumerate(incoming):
        if not isinstance(raw, dict):
            continue
        clean = _sanitize_route(raw)
        if not clean:
            continue
        target: dict | None = None
        role = clean.get("role")
        provider = clean.get("provider")
        for candidate in merged:
            if role is not None and candidate.get("role") == role:
                target = candidate
                break
        if target is None and provider is not None:
            for candidate in merged:
                if candidate.get("provider") == provider:
                    target = candidate
                    break
        if target is None and index < len(merged):
            target = merged[index]
        if target is None:
            merged.append(clean)
        else:
            for key, value in clean.items():
                if not _is_secret_field(key):
                    target[key] = value
    return merged


def apply_runtime_config(config: Any, payload: Any) -> dict:
    """Apply a pushed/polled payload to an in-memory Config.

    Never raises for unknown/secret fields; returns a summary so the caller can
    log and report what took effect.
    """
    summary = {"applied": [], "skipped": [], "llm_changed": False,
               "provider": "", "model": ""}
    if not isinstance(payload, dict):
        return summary
    for section_name, fields in (("agents", AGENT_RUNTIME_FIELDS),
                                 ("llm", LLM_RUNTIME_FIELDS)):
        incoming = payload.get(section_name)
        if not isinstance(incoming, dict):
            continue
        section = getattr(config, section_name, None)
        if section is None:
            summary["skipped"].append(section_name)
            continue
        for name in fields:
            if name not in incoming or _is_secret_field(name):
                continue
            value = incoming[name]
            if not hasattr(section, name):
                summary["skipped"].append(f"{section_name}.{name}")
                continue
            try:
                setattr(section, name, value)
            except Exception:  # pragma: no cover - frozen custom sections
                summary["skipped"].append(f"{section_name}.{name}")
                continue
            summary["applied"].append(f"{section_name}.{name}")
    llm_payload = payload.get("llm")
    llm_cfg = getattr(config, "llm", None)
    if isinstance(llm_payload, dict) and llm_cfg is not None:
        changed_llm = [name for name in ("provider", "model", "base_url")
                       if name in llm_payload]
        if changed_llm:
            summary["llm_changed"] = True
        summary["provider"] = str(getattr(llm_cfg, "provider", ""))
        summary["model"] = str(getattr(llm_cfg, "model", ""))
        if isinstance(llm_payload.get("routes"), list):
            try:
                llm_cfg.routes = _merge_routes(_old_routes(config),
                                               llm_payload["routes"])
                summary["applied"].append("llm.routes")
            except Exception:  # pragma: no cover - defensive
                summary["skipped"].append("llm.routes")
        if isinstance(llm_payload.get("fallback"), list):
            try:
                llm_cfg.fallback = _merge_routes(
                    list(getattr(llm_cfg, "fallback", None) or []),
                    llm_payload["fallback"])
                summary["applied"].append("llm.fallback")
            except Exception:  # pragma: no cover - defensive
                summary["skipped"].append("llm.fallback")
    if summary["provider"] or summary["model"]:
        summary["llm_changed"] = bool(summary["llm_changed"])
    return summary


class ConfigReloader:
    """Register ``unit.configure`` on a worker and optionally poll the hub."""

    ACTION = "unit.configure"

    def __init__(self, config: Any, *, unit: Any = None, sys: Any = None,
                 runner: Any = None, executor: Any = None, agent: Any = None,
                 rebuild_brain: Callable[[], Any] | None = None,
                 logger: Any = None) -> None:
        self.config = config
        self.unit = unit
        self.sys = sys
        self.runner = runner
        self.executor = executor
        self.agent = agent
        self.rebuild_brain = rebuild_brain
        self.log = logger or logger_for("sfo.agents.hotreload")
        self._poll_task: asyncio.Task | None = None
        self._stopped = False
        self.last_applied: dict = {}
        self.pushes = 0
        self.polls = 0

    # -- hub push ---------------------------------------------------------
    def install(self) -> bool:
        """Register the ``unit.configure`` RPC action (idempotent)."""
        register = getattr(self.unit, "register_action", None)
        if not callable(register):
            self.log.debug("no unit transport: unit.configure not registered")
            return False
        register(self.ACTION, self._handle_env)
        return True

    def _handle_env(self, env: Any) -> dict:
        data = getattr(env, "data", None)
        return self.handle(data if isinstance(data, dict) else {})

    def handle(self, payload: dict) -> dict:
        """Apply one pushed/polled payload and rebind limits/brain."""
        result = apply_runtime_config(self.config, payload)
        self.pushes += 1
        self.last_applied = result
        if self.runner is not None:
            reload_limits = getattr(self.runner, "reload_limits", None)
            if callable(reload_limits):
                try:
                    reload_limits()
                except Exception:  # pragma: no cover - defensive
                    self.log.exception("runner.reload_limits failed")
        if result.get("llm_changed") and callable(self.rebuild_brain):
            try:
                brain = self.rebuild_brain()
                if brain is not None and self.runner is not None:
                    self.runner.brain = brain
            except Exception:  # pragma: no cover - keep the old brain
                self.log.exception("brain rebuild after config change failed")
        if self.agent is not None:
            try:
                self.agent.stats["config_reloads"] = int(
                    self.agent.stats.get("config_reloads", 0)) + 1
            except Exception:  # pragma: no cover - agent may be a dict
                pass
        return {"ok": True, "applied": result.get("applied", []),
                "llm_changed": bool(result.get("llm_changed")),
                "provider": result.get("provider", ""),
                "model": result.get("model", ""),
                "reload_s": self.reload_interval}

    # -- worker polling ---------------------------------------------------
    @property
    def reload_interval(self) -> float:
        agents_cfg = getattr(self.config, "agents", self.config)
        try:
            return max(0.0, float(getattr(agents_cfg, "config_reload_s", 0) or 0))
        except (TypeError, ValueError):
            return 0.0

    async def fetch(self) -> dict | None:
        """Fetch the hub config once through the existing RPC client."""
        if self.sys is None:
            return None
        try:
            reply = await self.sys.call("sys.config.get", {})
        except Exception as exc:  # hub unreachable / action absent
            self.log.debug("config poll failed: %s", exc)
            return None
        self.polls += 1
        if not isinstance(reply, dict):
            return None
        payload = reply.get("config") if isinstance(reply.get("config"), dict) \
            else reply
        if isinstance(payload.get("data"), dict):  # wrapped API reply
            payload = payload["data"]
        return payload

    async def poll_once(self) -> dict | None:
        payload = await self.fetch()
        if payload is None:
            return None
        return self.handle(payload)

    def start(self) -> bool:
        """Start the reconcile loop when ``config_reload_s > 0``."""
        if self._stopped or self.reload_interval <= 0 or self.sys is None:
            return False
        if self._poll_task is not None and not self._poll_task.done():
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - sync call
            return False
        self._poll_task = loop.create_task(self._poll_loop())
        self._poll_task.add_done_callback(self._poll_done)
        return True

    def _poll_done(self, task: asyncio.Task) -> None:
        if self._poll_task is task:
            self._poll_task = None
        if not task.cancelled() and task.exception() is not None:
            self.log.debug("config poll loop ended: %s", task.exception())

    async def _poll_loop(self) -> None:
        while not self._stopped:
            interval = self.reload_interval
            if interval <= 0:
                interval = 10.0
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                return
            if self._stopped:
                return
            try:
                await self.poll_once()
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                return
            except Exception:  # pragma: no cover - polling must be best effort
                self.log.debug("config poll iteration failed", exc_info=True)

    async def stop(self) -> None:
        self._stopped = True
        task = self._poll_task
        self._poll_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def logger_for(name: str) -> Any:
    """Worker logger that never depends on the logging setup order."""
    from ..util.logging import get_logger

    return get_logger(name)


def install_config_reload(unit: Any, config: Any, *, sys: Any = None,
                          runner: Any = None, executor: Any = None,
                          agent: Any = None,
                          rebuild_brain: Callable[[], Any] | None = None,
                          logger: Any = None,
                          start_polling: bool = True) -> ConfigReloader:
    """Wire ``unit.configure`` into a worker and start the poll backstop."""
    reloader = ConfigReloader(
        config, unit=unit, sys=sys, runner=runner, executor=executor,
        agent=agent, rebuild_brain=rebuild_brain, logger=logger)
    reloader.install()
    if start_polling:
        reloader.start()
    return reloader


__all__ = [
    "AGENT_RUNTIME_FIELDS",
    "ConfigReloader",
    "LLM_RUNTIME_FIELDS",
    "apply_runtime_config",
    "install_config_reload",
    "public_runtime_config",
]

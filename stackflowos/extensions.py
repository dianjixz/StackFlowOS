"""Optional subsystem extension points.

The hub is a composition root: core services (event log, projects, approvals,
agents, chat) are always built, while newer subsystems -- capabilities, skills,
plugins, MCP, unit control, observability, discovery, cluster standby -- are
loaded through this module.  Keeping them behind one small contract lets each
subsystem live in its own package, be tested in isolation, and be absent from a
minimal install without the composition root growing a branch for every one.

A module opts in by defining::

    def build(app) -> Extension | None

The returned object may implement any of these optional methods/attributes:

``name``
    Stable identifier used in logs and ``/api/extensions``.
``async start()`` / ``async stop()``
    Lifecycle hooks, called after the core services and in reverse order.
``units() -> list[dict]``
    Unit descriptors contributed to ``/api/units``.  A descriptor with the same
    ``unit_id`` as a core unit replaces it (this is how ``skill.0`` stops being
    hard-coded as ``disabled``).
``routes() -> list[(method, regex, handler)]``
    Extra ``/api/*`` routes.  The handler has the normal WebAPI signature
    ``async (match, query, body) -> (status, payload)``.
``capabilities(kind: str) -> list[dict]``
    Catalogue entries for ``/api/capabilities`` (``tool``, ``skill``,
    ``plugin``, ``mcp_tool``).
``health() -> dict``
    Probe summary included in the hub ``/api/health`` payload.
``async on_config(config)``
    Called after a successful live config patch.

Failures are isolated: one broken subsystem is logged and skipped, never
preventing the hub from booting.  That is deliberate -- the alternative is a
typo in an optional plugin directory taking the whole control plane down.
"""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any, Iterable

logger = logging.getLogger("sfo.extensions")

#: Import order matters: catalogue first, then providers that contribute to it,
#: then consumers (unit control aggregates the providers' descriptors).
EXTENSION_MODULES: tuple[str, ...] = (
    "stackflowos.capabilities",
    "stackflowos.skills",
    "stackflowos.plugins",
    "stackflowos.mcp",
    "stackflowos.units.control",
    "stackflowos.ai.llm_api",
    "stackflowos.agents.extension",
    "stackflowos.autonomy",
    "stackflowos.security.sessions",
    "stackflowos.security.control",
    "stackflowos.observability.metrics",
    "stackflowos.observability.notifications",
    "stackflowos.observability.backup",
    "stackflowos.transport.discovery",
    "stackflowos.cluster.standby",
)


def load_extensions(app: Any) -> list[Any]:
    """Import and build every known extension, isolating failures."""
    from importlib import import_module

    loaded: list[Any] = []
    for module_name in EXTENSION_MODULES:
        try:
            module = import_module(module_name)
        except ModuleNotFoundError as exc:
            # Missing optional subsystem (or a missing parent package) is
            # normal; a missing third-party dependency of an installed
            # subsystem is a real installation error.
            missing = str(exc.name or "")
            if missing and (module_name == missing
                            or module_name.startswith(missing + ".")):
                continue
            logger.exception("extension %s failed to import", module_name)
            continue
        except Exception:
            logger.exception("extension %s failed to import", module_name)
            continue
        build = getattr(module, "build", None)
        if build is None:
            logger.warning("extension %s has no build(app) hook", module_name)
            continue
        try:
            extension = build(app)
        except Exception:
            logger.exception("extension %s failed to build", module_name)
            continue
        if extension is None:
            continue
        if not getattr(extension, "name", ""):
            extension.name = module_name.rsplit(".", 1)[-1]
        loaded.append(extension)
    if loaded:
        logger.info("loaded extensions: %s",
                    ", ".join(str(getattr(ext, "name", "?")) for ext in loaded))
    return loaded


async def maybe_await(value: Any) -> Any:
    """Await ``value`` when it is awaitable, return it otherwise."""
    if inspect.isawaitable(value):
        return await value
    return value


async def start_extensions(extensions: Iterable[Any]) -> None:
    for extension in extensions:
        hook = getattr(extension, "start", None)
        if hook is None:
            continue
        started = time.monotonic()
        try:
            await maybe_await(hook())
        except Exception:
            logger.exception("extension %s failed to start",
                             getattr(extension, "name", "?"))
        else:
            logger.debug("extension %s started in %.0fms",
                         getattr(extension, "name", "?"),
                         (time.monotonic() - started) * 1000)


async def stop_extensions(extensions: Iterable[Any]) -> None:
    for extension in reversed(list(extensions)):
        hook = getattr(extension, "stop", None)
        if hook is None:
            continue
        try:
            await maybe_await(hook())
        except Exception:
            logger.exception("extension %s failed to stop",
                             getattr(extension, "name", "?"))


async def notify_config(extensions: Iterable[Any], config: Any) -> None:
    for extension in extensions:
        hook = getattr(extension, "on_config", None)
        if hook is None:
            continue
        try:
            await maybe_await(hook(config))
        except Exception:
            logger.exception("extension %s rejected config",
                             getattr(extension, "name", "?"))


def extension_units(extensions: Iterable[Any]) -> list[dict]:
    units: list[dict] = []
    for extension in extensions:
        hook = getattr(extension, "units", None)
        if hook is None:
            continue
        try:
            contributed = hook()
        except Exception:
            logger.exception("extension %s failed to describe units",
                             getattr(extension, "name", "?"))
            continue
        if isinstance(contributed, list):
            units.extend(item for item in contributed if isinstance(item, dict))
    return units


def extension_routes(extensions: Iterable[Any]) -> list[tuple[str, str, Any]]:
    routes: list[tuple[str, str, Any]] = []
    for extension in extensions:
        hook = getattr(extension, "routes", None)
        if hook is None:
            continue
        try:
            contributed = hook()
        except Exception:
            logger.exception("extension %s failed to expose routes",
                             getattr(extension, "name", "?"))
            continue
        for item in contributed or ():
            if (isinstance(item, tuple) and len(item) == 3
                    and isinstance(item[0], str) and isinstance(item[1], str)):
                routes.append(item)
    return routes


def extension_capabilities(extensions: Iterable[Any],
                           kind: str | None = None) -> list[dict]:
    entries: list[dict] = []
    for extension in extensions:
        hook = getattr(extension, "capabilities", None)
        if hook is None:
            continue
        try:
            contributed = hook(kind) if kind is not None else hook(None)
        except TypeError:
            try:
                contributed = hook()
            except Exception:
                logger.exception("extension %s failed to list capabilities",
                                 getattr(extension, "name", "?"))
                continue
        except Exception:
            logger.exception("extension %s failed to list capabilities",
                             getattr(extension, "name", "?"))
            continue
        if isinstance(contributed, list):
            entries.extend(item for item in contributed if isinstance(item, dict))
    return entries


def extension_health(extensions: Iterable[Any]) -> dict:
    report: dict[str, Any] = {}
    for extension in extensions:
        hook = getattr(extension, "health", None)
        if hook is None:
            continue
        name = str(getattr(extension, "name", "?"))
        try:
            value = hook()
        except Exception as exc:  # noqa: BLE001 - health must never raise
            report[name] = {"ok": False, "error": type(exc).__name__}
        else:
            report[name] = value
    return report


__all__ = [
    "EXTENSION_MODULES", "extension_capabilities", "extension_health",
    "extension_routes", "extension_units", "load_extensions", "maybe_await",
    "notify_config", "start_extensions", "stop_extensions",
]

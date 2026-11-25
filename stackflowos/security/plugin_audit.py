"""Audit plugin manifest ``permissions`` when a plugin is loaded (W3, item 4).

The plugin loader deliberately supports only *trusted, in-process* plugins:
``permissions`` in ``stackflow.plugin.json`` is declarative metadata, it is
**not** an OS/network/filesystem boundary.  A plugin runs inside the hub
process with the hub's privileges, so a manifest that asks for the network, a
secret or shell access is a request the platform cannot honour or confine.

This module does not pretend to enforce anything.  It makes the gap visible:

* every load (and every plugin already loaded at startup) is checked for
  high-risk permission declarations;
* a WARNING is logged for every load that declares them;
* a ``security.plugin_permissions`` audit event records the declaration, the
  classified high-risk entries and ``"enforced": False``.

It is started by the security control extension (``security/control.py``),
which is loaded after the plugins extension, so startup loads are scanned
directly and later loads are picked up from the live ``plugin.loaded`` event
stream.  Failure to subscribe/parse is logged at debug and never affects the
plugin loader.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable

from ..util.logging import get_logger

#: Permission prefixes that grant something the hub process can already do
#: with its full privileges (network, secrets, filesystem, processes, ...).
_HIGH_RISK_PREFIXES = (
    "net:", "network:", "secret:", "secrets:", "fs:", "file:", "files:",
    "shell", "exec", "subprocess", "process:", "proc:", "env:", "env-var:",
    "admin:", "system:", "rpc:", "storage:", "llm:", "http:", "https:",
    "socket:", "db:", "database:",
)

#: Declarations that are inert: they do not widen what an in-process plugin
#: can do, so they are not worth a warning on every boot.
_LOW_RISK = frozenset({
    "", "inproc", "in-process", "in_process", "none", "read", "readonly",
    "read-only", "metadata",
})


def permission_risk(permission: Any) -> str:
    """Classify one declared permission as ``low`` or ``high``.

    Conservative by design: anything not in the short inert allow-list is
    high risk, because an unknown declaration cannot be verified and the
    plugin already runs with hub privileges.
    """
    text = str(permission or "").strip().lower()
    if text in _LOW_RISK:
        return "low"
    if text.startswith(_HIGH_RISK_PREFIXES):
        return "high"
    # Unknown vocabulary is still a declaration of intent; treat it as high.
    return "high"


def high_risk_permissions(permissions: Iterable[Any]) -> list[str]:
    """Return the declared permissions classified as high risk (stable order)."""
    result: list[str] = []
    for item in permissions or ():
        text = str(item or "").strip()
        if text and permission_risk(text) == "high" and text not in result:
            result.append(text)
    return result


class PluginPermissionAuditor:
    """Warn + audit when a loaded plugin declares high-risk permissions."""

    name = "security.plugin-permissions"

    def __init__(self, app: Any, *,
                 logger: logging.Logger | None = None) -> None:
        self.app = app
        self.log = logger or get_logger("security.plugins")
        self._eventlog: Any = None
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        loader = getattr(self.app, "plugin_loader", None)
        if loader is None:
            # Bare app (tests, CLI helpers): nothing to audit.
            return
        self._audit_loaded(source="startup")
        eventlog = getattr(self.app, "eventlog", None)
        if eventlog is None:
            return
        subscribe = getattr(eventlog, "subscribe", None)
        if not callable(subscribe):
            return
        try:
            self._eventlog = eventlog
            self._queue = subscribe()
            self._task = asyncio.create_task(
                self._consume(self._queue), name="plugin-permission-audit")
        except Exception:  # pragma: no cover - live audit is optional
            self.log.debug("plugin permission audit could not subscribe",
                           exc_info=True)
            self._queue = None
            self._task = None

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        queue, self._queue = self._queue, None
        eventlog, self._eventlog = self._eventlog, None
        unsubscribe = getattr(eventlog, "unsubscribe", None)
        if queue is not None and callable(unsubscribe):
            try:
                unsubscribe(queue)
            except Exception:  # pragma: no cover - defensive
                pass

    # -- audit -----------------------------------------------------------
    async def _consume(self, queue: asyncio.Queue) -> None:
        while True:
            record = await queue.get()
            try:
                if getattr(record, "type", "") != "plugin.loaded":
                    continue
                data = getattr(record, "data", None) or {}
                plugin_id = str(data.get("plugin_id") or "")
                if plugin_id:
                    self.audit_plugin(plugin_id, source="plugin.loaded")
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - a bad record must not stop it
                self.log.debug("plugin permission audit failed", exc_info=True)

    def _audit_loaded(self, *, source: str) -> None:
        loader = getattr(self.app, "plugin_loader", None)
        records = getattr(loader, "list", None)
        if not callable(records):
            return
        try:
            items = list(records())
        except Exception:  # pragma: no cover - defensive
            self.log.debug("plugin list unavailable for permission audit",
                           exc_info=True)
            return
        for record in items:
            if str(getattr(record, "status", "")) != "loaded":
                continue
            self._audit_record(record, source=source)

    def audit_plugin(self, plugin_id: str, *, source: str = "load") -> dict | None:
        """Audit one plugin id; returns the event payload (or ``None``)."""
        loader = getattr(self.app, "plugin_loader", None)
        getter = getattr(loader, "get", None)
        if not callable(getter):
            return None
        try:
            record = getter(str(plugin_id))
        except Exception:  # NotFound or loader unavailable
            return None
        return self._audit_record(record, source=source)

    def _audit_record(self, record: Any, *, source: str) -> dict | None:
        manifest = getattr(record, "manifest", None)
        if not isinstance(manifest, dict):
            manifest = {}
        permissions = [str(item) for item in (manifest.get("permissions") or [])
                       if isinstance(item, str)]
        high = high_risk_permissions(permissions)
        if not high:
            return None
        plugin_id = str(getattr(record, "id", "") or "")
        data = {
            "plugin_id": plugin_id,
            "path": str(getattr(record, "path", "") or ""),
            "permissions": permissions,
            "high_risk": high,
            "enforced": False,
            "source": source,
            "note": ("manifest permissions are declarative only; an "
                     "in-process plugin runs with hub privileges"),
        }
        self.log.warning(
            "plugin %s declares high-risk permissions %s, but they are NOT "
            "enforced (in-process plugin has hub privileges)",
            plugin_id or "?", ", ".join(high))
        self._emit("security.plugin_permissions", data)
        return data

    # -- event -----------------------------------------------------------
    def _emit(self, event_type: str, data: dict) -> None:
        eventlog = getattr(self.app, "eventlog", None)
        if eventlog is None:
            return
        try:
            from ..l2.eventlog import emit_both

            ctx = getattr(self.app, "ctx", None)
            if ctx is not None:
                emit_both(ctx, eventlog, event_type, data)
            else:
                eventlog.append(event_type, data, source="sys")
        except Exception:  # pragma: no cover - audit must never break loading
            self.log.debug("plugin permission audit event failed", exc_info=True)


def build_auditor(app: Any) -> PluginPermissionAuditor:
    """Create an auditor for *app* (started by the security control extension)."""
    return PluginPermissionAuditor(app)


__all__ = ["PluginPermissionAuditor", "build_auditor", "permission_risk",
           "high_risk_permissions"]

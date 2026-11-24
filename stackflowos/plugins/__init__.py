"""Trusted in-process plugin loader (``plugin.0`` host, ``plugin.<id>`` units).

A plugin is a directory below ``plugins.dirs`` containing
``stackflow.plugin.json``::

    {
      "id": "hello", "name": "Hello", "version": "0.1.0",
      "entry": {"type": "python", "module": "hello_plugin:apply"},
      "requires": ["context.0", "tool.0"],
      "contributes": {"tools": ["plugin.echo"], "skills": ["hello-skill"]},
      "permissions": ["inproc"],
      "config_schema": "schema.json"
    }

Only **trusted, in-process** plugins are supported: ``apply(ctx, provides)``
runs inside the hub, contributes tools/skills through ``provides`` and must
return an optional disposer.  Everything it registers is bound to a Cordis
child context created with ``app.ctx.child(...)``; unloading disposes that
context, so catalogue entries, executors and services disappear atomically
(the ``effect`` mechanism in :mod:`stackflowos.kernel.context`).
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Awaitable, Callable

from ..errors import BadRequest, Conflict, NotFound
from ..l2.eventlog import emit_both
from ..protocol import now_ms
from ..util.logging import get_logger
from ..util.redact import redact_text

PLUGIN_FILE = "stackflow.plugin.json"
MAX_MANIFEST_BYTES = 256 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_SCAN_DEPTH = 3
DEFAULT_CAPABILITY = "shell.exec"


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass
class PluginRecord:
    """One discovered plugin directory and its live state."""

    id: str
    path: str
    manifest: dict = field(default_factory=dict)
    valid: bool = True
    error: str | None = None
    status: str = "discovered"     # discovered|loaded|failed|invalid
    loaded_at: int | None = None
    module: ModuleType | None = None
    child: Any = None
    contributions: dict = field(default_factory=lambda: {"tools": [], "skills": []})

    @property
    def unit_id(self) -> str:
        return f"plugin.{self.id}"

    @property
    def name(self) -> str:
        return str(self.manifest.get("name") or self.id)

    @property
    def version(self) -> str:
        return str(self.manifest.get("version") or "")

    def tool_count(self) -> int:
        return len(self.contributions.get("tools") or [])

    def skill_count(self) -> int:
        return len(self.contributions.get("skills") or [])

    def to_dict(self) -> dict:
        manifest = self.manifest
        return {
            "id": self.id,
            "unit_id": self.unit_id,
            "name": self.name,
            "version": self.version,
            "path": self.path,
            "status": self.status,
            "valid": self.valid,
            "error": self.error,
            "loaded_at": self.loaded_at,
            "requires": list(manifest.get("requires") or []),
            "permissions": list(manifest.get("permissions") or []),
            "config_schema": manifest.get("config_schema"),
            "contributes": {
                "tools": list(self.contributions.get("tools") or []),
                "skills": list(self.contributions.get("skills") or []),
            },
            "declared_contributes": dict(manifest.get("contributes") or {}),
        }


def _read_manifest(path: Path) -> dict:
    raw = path.read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError(f"{PLUGIN_FILE} exceeds {MAX_MANIFEST_BYTES} bytes")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{PLUGIN_FILE} must contain an object")
    return data


def validate_manifest(data: dict, path: Path) -> tuple[dict | None, list[str]]:
    """Validate/normalise one manifest; return ``(manifest, errors)``."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return None, [f"{PLUGIN_FILE} must contain an object"]
    plugin_id = data.get("id")
    if not isinstance(plugin_id, str) or not ID_RE.match(plugin_id.strip()):
        errors.append(f"invalid plugin id: {plugin_id!r}")
        plugin_id = str(plugin_id or path.parent.name)
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("name must be a non-empty string")
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        errors.append("version must be a non-empty string")
    entry = data.get("entry")
    module_path = ""
    callable_name = ""
    if not isinstance(entry, dict):
        errors.append("entry must be an object")
    else:
        if str(entry.get("type") or "python") != "python":
            errors.append("entry.type must be 'python'")
        raw_module = entry.get("module")
        if not isinstance(raw_module, str) or ":" not in raw_module:
            errors.append("entry.module must be 'module:callable'")
        else:
            module_path, _, callable_name = raw_module.partition(":")
            if not module_path.strip() or not callable_name.strip():
                errors.append("entry.module must be 'module:callable'")
    runtime = str(data.get("runtime") or "in_process")
    if runtime != "in_process":
        errors.append(f"runtime {runtime!r} is not supported "
                      "(only trusted in_process plugins are loadable)")
    requires = data.get("requires", [])
    if not isinstance(requires, list) or not all(
            isinstance(item, str) for item in requires):
        errors.append("requires must be a list of strings")
    permissions = data.get("permissions", [])
    if not isinstance(permissions, list) or not all(
            isinstance(item, str) for item in permissions):
        errors.append("permissions must be a list of strings")
    contributes = data.get("contributes", {})
    if contributes is None:
        contributes = {}
    if not isinstance(contributes, dict):
        errors.append("contributes must be an object")
        contributes = {}
    for key in ("tools", "skills"):
        value = contributes.get(key, [])
        if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value):
            errors.append(f"contributes.{key} must be a list of strings")
    config_schema = data.get("config_schema")
    if config_schema is not None and not isinstance(
            config_schema, (str, dict)):
        errors.append("config_schema must be a string or object")
    if errors:
        return None, errors
    manifest = {
        "id": plugin_id.strip(),
        "name": name.strip(),
        "version": version.strip(),
        "entry": {"type": "python",
                  "module": f"{module_path.strip()}:{callable_name.strip()}"},
        "runtime": runtime,
        "requires": [str(item) for item in requires],
        "permissions": [str(item) for item in permissions],
        "contributes": {
            "tools": [str(item) for item in contributes.get("tools", [])],
            "skills": [str(item) for item in contributes.get("skills", [])],
        },
        "config_schema": config_schema,
        "path": str(path),
    }
    return manifest, []


class PluginProvides:
    """The ``provides`` object handed to ``apply(ctx, provides)``."""

    def __init__(self, loader: "PluginLoader", record: PluginRecord) -> None:
        self.loader = loader
        self.record = record
        self._disposers: list[Callable[[], Any]] = []
        self._disposed = False

    def tool(self, name: str, *, description: str = "",
             capability: str | None = None, risk: str | None = None,
             schema: dict | None = None, handler: Callable[..., Any] | None = None,
             enabled_in: list[str] | None = None) -> Callable[[], None]:
        """Contribute one catalogue tool (optionally executable)."""
        if self._disposed:
            raise RuntimeError("plugin provides already disposed")
        tool_name = str(name or "").strip()
        if not tool_name:
            raise BadRequest("contributed tool requires a name")
        catalog = getattr(self.loader.app, "capability_catalog", None)
        entry = {
            "type": "tool",
            "name": tool_name,
            "description": str(description or ""),
            "capability": str(capability or DEFAULT_CAPABILITY),
            "risk": str(risk or "high"),
            "schema": schema or {"type": "object", "properties": {},
                                 "required": []},
            "provider": self.record.unit_id,
            "enabled_in": list(enabled_in or ["*"]),
            "metadata": {"plugin": self.record.id},
        }
        disposers: list[Callable[[], Any]] = []
        if catalog is not None:
            disposers.append(catalog.register(entry,
                                              provider=self.record.unit_id))
            if handler is not None:
                disposers.append(catalog.register_tool_executor(
                    self.record.unit_id, tool_name,
                    self._wrap_handler(tool_name, handler)))
        self.record.contributions["tools"].append(tool_name)

        def dispose() -> None:
            for item in reversed(disposers):
                try:
                    item()
                except Exception:  # pragma: no cover - cleanup is best effort
                    self.loader.log.debug("tool contribution cleanup failed",
                                          exc_info=True)
            try:
                self.record.contributions["tools"].remove(tool_name)
            except ValueError:
                pass

        self._disposers.append(dispose)
        return dispose

    def _wrap_handler(self, name: str,
                      handler: Callable[..., Any]) -> Callable[..., Awaitable[Any]]:
        async def execute(args: dict, principal: str,
                          project_id: str | None,
                          approval_id: str | None = None) -> Any:
            kwargs: dict[str, Any] = {}
            try:
                params = inspect.signature(handler).parameters
            except (TypeError, ValueError):  # pragma: no cover - builtins
                params = {}
            accepts_kwargs = any(
                param.kind == inspect.Parameter.VAR_KEYWORD
                for param in params.values())
            if "principal" in params or accepts_kwargs:
                kwargs["principal"] = principal
            if "project_id" in params or accepts_kwargs:
                kwargs["project_id"] = project_id
            return await _maybe_await(handler(dict(args), **kwargs))

        execute.__name__ = f"plugin_{self.record.id}_{name}"
        return execute

    def skill(self, manifest: dict) -> Any:
        """Contribute one skill entry (see ``SkillRegistry.add_external_skill``)."""
        if self._disposed:
            raise RuntimeError("plugin provides already disposed")
        skills = getattr(self.loader.app, "skills", None)
        if skills is None:
            raise BadRequest("skill registry is not available")
        entry, remove = skills.add_external_skill(
            manifest, provider=self.record.unit_id)
        self.record.contributions["skills"].append(entry.id)

        def dispose() -> None:
            try:
                remove()
            finally:
                try:
                    self.record.contributions["skills"].remove(entry.id)
                except ValueError:
                    pass

        self._disposers.append(dispose)
        return entry

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        for disposer in reversed(self._disposers):
            try:
                disposer()
            except Exception:  # pragma: no cover
                self.loader.log.debug("plugin contribution dispose failed",
                                      exc_info=True)


class PluginLoader:
    """Discovery, trusted in-process loading and the ``plugin.<id>`` units."""

    name = "plugins"

    def __init__(self, app: Any) -> None:
        self.app = app
        self.log = get_logger("plugins")
        section = getattr(getattr(app, "config", None), "plugins", None)
        self.max_plugins = int(getattr(section, "max_plugins", 32) or 32)
        self._records: dict[str, PluginRecord] = {}
        self._modules: dict[Path, ModuleType] = {}
        self._suppressed = False
        self._disabled: set[str] = set()
        self._lock = asyncio_lock()

    # -- configuration ----------------------------------------------------
    def _section(self, name: str, default: Any) -> Any:
        section = getattr(getattr(self.app, "config", None), "plugins", None)
        return getattr(section, name, default)

    def resolve_dirs(self) -> list[Path]:
        state_dir = Path(getattr(getattr(self.app, "paths", None), "state_dir",
                                 Path.cwd()))
        roots: list[Path] = []
        for raw in self._section("dirs", ["plugins"]) or []:
            path = Path(str(raw)).expanduser()
            if not path.is_absolute():
                path = state_dir / path
            roots.append(path)
        return roots

    def _enabled_ids(self) -> list[str]:
        return [str(item) for item in (self._section("enabled", []) or [])]

    # -- discovery --------------------------------------------------------
    def discover(self) -> list[PluginRecord]:
        records: dict[str, PluginRecord] = {}
        seen: set[str] = set()
        for root in self.resolve_dirs():
            if not root.is_dir():
                continue
            for manifest_path in self._walk_manifests(root):
                try:
                    data = _read_manifest(manifest_path)
                except (OSError, ValueError) as exc:
                    record = PluginRecord(
                        id=manifest_path.parent.name, path=str(manifest_path.parent),
                        valid=False, status="invalid",
                        error=f"invalid {PLUGIN_FILE}: {exc}")
                    records.setdefault(record.id, record)
                    continue
                manifest, errors = validate_manifest(data, manifest_path)
                plugin_id = (manifest or {}).get("id") or \
                    str(data.get("id") or manifest_path.parent.name)
                if errors:
                    record = PluginRecord(
                        id=str(plugin_id), path=str(manifest_path.parent),
                        valid=False, status="invalid",
                        error="; ".join(errors))
                elif str(plugin_id) in seen:
                    record = PluginRecord(
                        id=str(plugin_id), path=str(manifest_path.parent),
                        valid=False, status="invalid",
                        error=f"duplicate plugin id {plugin_id!r}")
                    # Fail closed for the whole id: the first manifest becomes
                    # invalid too, so no half-configured plugin is loadable.
                    records[record.id] = record
                    continue
                elif len(records) >= self.max_plugins:
                    record = PluginRecord(
                        id=str(plugin_id), path=str(manifest_path.parent),
                        valid=False, status="invalid",
                        error=f"max_plugins ({self.max_plugins}) exceeded")
                else:
                    record = PluginRecord(
                        id=str(plugin_id), path=str(manifest_path.parent),
                        manifest=manifest or {}, valid=True)
                    seen.add(record.id)
                records.setdefault(record.id, record)
        self._records = dict(sorted(records.items()))
        self._emit_invalid()
        self._sync_host_unit()
        return list(self._records.values())

    def _walk_manifests(self, root: Path):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames
                                 if not name.startswith(".")
                                 and name != "__pycache__")
            depth = len(Path(dirpath).relative_to(root).parts)
            if depth >= MAX_SCAN_DEPTH:
                dirnames[:] = []
            if PLUGIN_FILE in filenames:
                dirnames[:] = []
                yield Path(dirpath) / PLUGIN_FILE

    def _emit_invalid(self) -> None:
        for record in self._records.values():
            if not record.valid:
                self._emit("plugin.invalid", {
                    "plugin_id": record.id, "path": record.path,
                    "reason": redact_text(str(record.error or ""))[:500],
                })

    # -- public lookup ----------------------------------------------------
    def list(self) -> list[PluginRecord]:
        return list(self._records.values())

    def get(self, plugin_id: str) -> PluginRecord:
        record = self._records.get(str(plugin_id))
        if record is None:
            raise NotFound(f"unknown plugin: {plugin_id!r}",
                           detail={"plugin_id": plugin_id})
        return record

    def _catalog(self):
        return getattr(self.app, "capability_catalog", None)

    # -- loading ----------------------------------------------------------
    def _resolve_entry(self, record: PluginRecord):
        module_spec = str((record.manifest.get("entry") or {}).get("module") or "")
        module_part, _, callable_path = module_spec.partition(":")
        if module_part.endswith(".py"):
            module_part = module_part[:-3]
        if not module_part or not callable_path:
            raise BadRequest(f"plugin {record.id!r} entry.module is invalid")
        module = self._load_module(record, module_part)
        target: Any = module
        for part in callable_path.split("."):
            target = getattr(target, part)
        if not callable(target):
            raise BadRequest(
                f"plugin {record.id!r} entry callable "
                f"{callable_path!r} is not callable")
        return module, target

    def _load_module(self, record: PluginRecord, module_part: str) -> ModuleType:
        directory = Path(record.path)
        rel = module_part.replace(".", "/")
        candidates = [directory / f"{rel}.py", directory / rel / "__init__.py"]
        for candidate in candidates:
            if candidate.is_file():
                cached = self._modules.get(candidate)
                if cached is not None:
                    return cached
                module_name = f"sfo_plugin_{record.id}_{abs(hash(str(candidate))) & 0xffff:x}"
                spec = importlib.util.spec_from_file_location(
                    module_name, str(candidate))
                if spec is None or spec.loader is None:
                    raise BadRequest(
                        f"cannot load plugin module {candidate}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    sys.modules.pop(module_name, None)
                    raise
                self._modules[candidate] = module
                return module
        # Fall back to an installed/importable module.
        return importlib.import_module(module_part)

    async def load(self, plugin_id: str) -> PluginRecord:
        async with self._lock:
            return await self._load_locked(str(plugin_id))

    async def _load_locked(self, plugin_id: str) -> PluginRecord:
        record = self.get(plugin_id)
        if not record.valid:
            raise Conflict(f"plugin {plugin_id!r} is invalid: {record.error}",
                           detail={"plugin_id": plugin_id,
                                   "error": record.error})
        if record.status == "loaded":
            raise Conflict(f"plugin {plugin_id!r} is already loaded")
        if self._suppressed:
            raise Conflict("plugin host is disabled")
        try:
            module, apply = self._resolve_entry(record)
        except Exception as exc:  # noqa: BLE001 - surfaced as Conflict
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {exc}"
            self._emit("plugin.failed", {"plugin_id": plugin_id,
                                         "error": record.error})
            self._sync_host_unit()
            raise Conflict(f"plugin {plugin_id!r} failed to load: {exc}",
                           detail={"plugin_id": plugin_id,
                                   "error": record.error}) from exc
        record.module = module
        provides = PluginProvides(self, record)
        child = self.app.ctx.child(f"plugin.{plugin_id}")
        record.child = child

        def setup():
            result = apply(child, provides)
            if inspect.isawaitable(result):
                raise TypeError(
                    "plugin apply() must be synchronous (return a disposer)")
            return [provides.dispose, result] if result is not None \
                else provides.dispose

        try:
            child.effect(setup, label=f"plugin.{plugin_id}")
        except Exception as exc:  # noqa: BLE001 - plugin failures are isolated
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {exc}"
            try:
                provides.dispose()
            finally:
                await child.dispose()
            self._emit("plugin.failed", {
                "plugin_id": plugin_id,
                "error": redact_text(record.error)[:500],
            })
            self._sync_host_unit()
            raise Conflict(
                f"plugin {plugin_id!r} failed during apply: {exc}",
                detail={"plugin_id": plugin_id,
                        "error": record.error}) from exc
        record.status = "loaded"
        record.error = None
        record.loaded_at = now_ms()
        self._disabled.discard(plugin_id)
        self._emit("plugin.loaded", {
            "plugin_id": plugin_id,
            "unit_id": record.unit_id,
            "version": record.version,
            "provides": {"tools": record.tool_count(),
                         "skills": record.skill_count()},
        })
        self.log.info("plugin %s loaded (%d tools, %d skills)",
                      plugin_id, record.tool_count(), record.skill_count())
        self._sync_unit(record)
        self._sync_host_unit()
        return record

    async def unload(self, plugin_id: str) -> PluginRecord:
        async with self._lock:
            return await self._unload_locked(str(plugin_id))

    async def _unload_locked(self, plugin_id: str) -> PluginRecord:
        record = self.get(plugin_id)
        if record.status != "loaded" or record.child is None:
            raise Conflict(f"plugin {plugin_id!r} is not loaded")
        child = record.child
        record.child = None
        if child is not None:
            await child.dispose()
        record.status = "discovered"
        record.loaded_at = None
        record.contributions = {"tools": [], "skills": []}
        self._emit("plugin.unloaded", {
            "plugin_id": plugin_id,
            "unit_id": record.unit_id,
        })
        self._sync_unit(record)
        self._sync_host_unit()
        return record

    async def restart(self, plugin_id: str) -> PluginRecord:
        async with self._lock:
            record = self.get(plugin_id)
            if record.status == "loaded":
                await self._unload_locked(plugin_id)
            return await self._load_locked(plugin_id)

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        self.discover()
        for plugin_id in self._enabled_ids():
            if plugin_id == "*":
                targets = [record.id for record in self._records.values()
                           if record.valid]
            else:
                targets = [plugin_id]
            for target in targets:
                try:
                    await self.load(target)
                except Conflict as exc:
                    self.log.warning("plugin %s not loaded at startup: %s",
                                     target, exc.message)
        self._register_host_control()
        for record in self._records.values():
            if record.valid:
                self._register_control(record)

    async def stop(self) -> None:
        async with self._lock:
            for record in list(self._records.values()):
                if record.status == "loaded":
                    try:
                        await self._unload_locked(record.id)
                    except Exception:  # pragma: no cover
                        self.log.exception("plugin %s unload failed", record.id)
        self._sync_host_unit()

    async def on_config(self, config) -> None:
        # Unload first: discover() rebuilds PluginRecord objects, and a loaded
        # child context must never be orphaned by a record replacement.
        for record in list(self._records.values()):
            if record.status == "loaded":
                try:
                    await self.unload(record.id)
                except Conflict:
                    pass
        discovered = self.discover()
        desired = {str(item) for item in getattr(config.plugins, "enabled", [])}
        if "*" in desired:
            desired = {record.id for record in discovered if record.valid}
        for record in discovered:
            try:
                if record.id in desired and record.status != "loaded":
                    await self.load(record.id)
                elif record.id not in desired and record.status == "loaded":
                    await self.unload(record.id)
            except Conflict as exc:
                self.log.warning("plugin reconcile failed for %s: %s",
                                 record.id, exc.message)
        self._register_host_control()
        for record in discovered:
            if record.valid:
                self._register_control(record)

    def _register_host_control(self) -> None:
        control = getattr(self.app, "unit_control", None)
        register = getattr(control, "register_provider", None)
        if register is None:
            return
        try:
            register("plugin.0", probe=self.probe_host,
                     restart=self.restart_host, disable=self.disable_host,
                     enable=self.enable_host)
        except Exception:  # pragma: no cover
            self.log.debug("plugin.0 control registration failed", exc_info=True)
        self._sync_host_unit()

    # -- units ------------------------------------------------------------
    def _missing_contributions(self, record: PluginRecord) -> list[str]:
        if record.status != "loaded":
            return []
        declared = record.manifest.get("contributes") or {}
        missing: list[str] = []
        for name in declared.get("tools") or []:
            if name not in (record.contributions.get("tools") or []):
                missing.append(f"tool:{name}")
        for name in declared.get("skills") or []:
            if name not in (record.contributions.get("skills") or []):
                missing.append(f"skill:{name}")
        return missing

    def _record_state(self, record: PluginRecord) -> str:
        if not record.valid:
            return "disabled"
        if record.status == "loaded":
            return "degraded" if self._missing_contributions(record) else "ready"
        if record.status == "failed":
            return "failed"
        if self._suppressed or record.id in self._disabled:
            return "disabled"
        return "disabled"

    def _record_reason(self, record: PluginRecord) -> str | None:
        missing = self._missing_contributions(record)
        if missing:
            return "declared contributions missing: " + ", ".join(missing)
        return record.error

    def unit_state(self, plugin_id: str) -> str | None:
        record = self._records.get(str(plugin_id))
        if record is None:
            return None
        return self._record_state(record)

    def _unit(self, record: PluginRecord) -> dict:
        state = self._record_state(record)
        now = now_ms()
        config = getattr(self.app, "config", None)
        host = getattr(getattr(config, "system", None), "host_label", "")
        requires = ["context.0", "tool.0"]
        for item in record.manifest.get("requires") or []:
            if item not in requires:
                requires.append(item)
        descriptor = {
            "work_id": record.unit_id,
            "unit_id": record.unit_id,
            "kind": "plugin",
            "role": "tool-provider",
            "tier": "service",
            "label": record.name,
            "project_id": None,
            "pid": None,
            "host": host,
            "transport": "inproc",
            "rpc_address": None,
            "events_address": None,
            "status": {"ready": "online", "failed": "failed",
                       "disabled": "disabled",
                       "degraded": "degraded"}.get(state, "online"),
            "state": state,
            "reason": self._record_reason(record),
            "abilities": [{"name": "plugin.apply", "scope": "system"},
                          {"name": "plugin.provide", "scope": "system"}],
            "hwinfo": {},
            "started_at": record.loaded_at or getattr(self.app, "started_at", now),
            "last_seen": now,
            "restart_count": 0,
            "metadata": {
                "description": f"in-process 插件 {record.id}",
                "manifest": record.path,
                "permissions": list(record.manifest.get("permissions") or []),
                "requires": list(record.manifest.get("requires") or []),
                "contributions": {
                    "tools": list(record.contributions.get("tools") or []),
                    "skills": list(record.contributions.get("skills") or []),
                },
                "missing_contributions": self._missing_contributions(record),
                "error": record.error,
            },
            "runtime": {"mode": "in_process", "host_unit": "context.0",
                        "host": host, "pid": None,
                        "module": str((record.manifest.get("entry") or {})
                                      .get("module") or ""),
                        "started_at": record.loaded_at or
                        getattr(self.app, "started_at", now)},
            "endpoints": [{"name": "inproc", "transport": "inproc",
                           "address": record.unit_id}],
            "ref": f"unit://{record.unit_id}",
            "critical": False,
            "requires": requires,
            "actions": ["probe", "restart", "disable", "enable",
                        "drain", "undrain"],
            "lease": {"type": "host", "host_unit": "context.0",
                      "interval_s": 5, "renewed_at": now},
            "provides": {"tools": record.tool_count(),
                         "skills": record.skill_count()},
        }
        return self._overlay(descriptor)

    def _host_state_reason(self) -> tuple[str, str | None]:
        valid = [record for record in self._records.values() if record.valid]
        failed = [record for record in valid if record.status == "failed"]
        invalid = [record for record in self._records.values() if not record.valid]
        degraded = [record for record in valid
                    if self._record_state(record) == "degraded"]
        if self._suppressed:
            return "disabled", "disabled by operator"
        if invalid:
            return "degraded", (f"{len(invalid)} invalid manifest(s): " + ", ".join(
                record.id for record in invalid[:5]))
        if failed:
            return "degraded", (f"{len(failed)} failed plugin(s): " + ", ".join(
                record.id for record in failed[:5]))
        if degraded:
            return "degraded", (f"{len(degraded)} degraded plugin(s): " + ", ".join(
                record.id for record in degraded[:5]))
        return "ready", None

    def _host_unit(self) -> dict:
        valid = [record for record in self._records.values() if record.valid]
        failed = [record for record in valid if record.status == "failed"]
        invalid = [record for record in self._records.values() if not record.valid]
        loaded = [record for record in valid if record.status == "loaded"]
        state, reason = self._host_state_reason()
        now = now_ms()
        config = getattr(self.app, "config", None)
        host = getattr(getattr(config, "system", None), "host_label", "")
        descriptor = {
            "work_id": "plugin.0",
            "unit_id": "plugin.0",
            "kind": "plugin",
            "role": "host",
            "tier": "service",
            "label": "插件宿主",
            "project_id": None,
            "pid": None,
            "host": host,
            "transport": "inproc",
            "rpc_address": None,
            "events_address": None,
            "status": {"ready": "online", "degraded": "degraded",
                       "disabled": "disabled", "failed": "failed"}.get(state,
                                                                       "online"),
            "state": state,
            "reason": reason,
            "abilities": [{"name": "plugin.load", "scope": "system"},
                          {"name": "plugin.unload", "scope": "system"}],
            "hwinfo": {},
            "started_at": getattr(self.app, "started_at", now),
            "last_seen": now,
            "restart_count": 0,
            "metadata": {"description": "插件加载器；每个外部插件是 plugin.<id>",
                         "plugins": {record.id: record.status
                                     for record in self._records.values()}},
            "runtime": {"mode": "in_process", "host_unit": "sys.0",
                        "host": host, "pid": None,
                        "module": "stackflowos.plugins:PluginLoader",
                        "started_at": getattr(self.app, "started_at", now)},
            "endpoints": [{"name": "inproc", "transport": "inproc",
                           "address": "plugin.0"}],
            "ref": "unit://plugin.0",
            "critical": False,
            "requires": ["context.0", "tool.0"],
            "actions": ["probe", "restart", "disable", "enable",
                        "drain", "undrain"],
            "lease": {"type": "host", "host_unit": "sys.0",
                      "interval_s": 5, "renewed_at": now},
            "provides": {"plugins": len(loaded),
                         "discovered": len(valid),
                         "failed": len(failed), "invalid": len(invalid)},
        }
        return self._overlay(descriptor)

    def _overlay(self, descriptor: dict) -> dict:
        control = getattr(self.app, "unit_control", None)
        overlay = getattr(control, "overlay", None)
        if overlay is None:
            return descriptor
        try:
            return overlay(descriptor)
        except Exception:  # pragma: no cover
            return descriptor

    def units(self) -> list[dict]:
        units = [self._host_unit()]
        units.extend(self._unit(record) for record in self._records.values()
                     if record.valid)
        return units

    def capabilities(self, kind: str | None = None) -> list[dict]:
        if kind not in (None, "plugin", "tool"):
            return []
        result: list[dict] = []
        if kind in (None, "plugin"):
            for record in self._records.values():
                if not record.valid:
                    continue
                result.append({
                    "type": "plugin",
                    "name": record.id,
                    "description": record.name,
                    "version": record.version,
                    "provider": record.unit_id,
                    "enabled_in": ["*"],
                    "risk": "high",
                    "schema": {"type": "plugin",
                               "requires": list(record.manifest.get("requires") or []),
                               "permissions": list(record.manifest.get("permissions")
                                                   or [])},
                    "capability": None,
                    "metadata": {"path": record.path,
                                 "status": record.status},
                })
        if kind in (None, "tool"):
            for record in self._records.values():
                if record.status != "loaded":
                    continue
                for name in record.contributions.get("tools") or []:
                    result.append({
                        "type": "tool",
                        "name": name,
                        "description": f"插件 {record.id} 提供的工具",
                        "provider": record.unit_id,
                        "enabled_in": ["*"],
                        "risk": "high",
                        "schema": {"type": "object", "properties": {},
                                   "required": []},
                        "capability": DEFAULT_CAPABILITY,
                        "metadata": {"plugin": record.id},
                    })
        return result

    # -- control hooks ----------------------------------------------------
    def _register_control(self, record: PluginRecord) -> None:
        if not record.valid:
            return
        control = getattr(self.app, "unit_control", None)
        register = getattr(control, "register_provider", None)
        if register is None:
            return

        async def restart(plugin_id: str = record.id) -> dict:
            updated = await self.restart(plugin_id)
            return updated.to_dict()

        async def disable(plugin_id: str = record.id) -> None:
            self._disabled.add(plugin_id)
            if plugin_id in self._records and \
                    self._records[plugin_id].status == "loaded":
                await self.unload(plugin_id)
            self._sync_unit(self._records.get(plugin_id, record))

        async def enable(plugin_id: str = record.id) -> None:
            self._disabled.discard(plugin_id)
            if plugin_id in self._records and \
                    self._records[plugin_id].status != "loaded":
                try:
                    await self.load(plugin_id)
                except Conflict as exc:
                    raise Conflict(exc.message) from exc
            self._sync_unit(self._records.get(plugin_id, record))

        async def probe(plugin_id: str = record.id) -> dict:
            current = self._records.get(plugin_id)
            if current is None:
                return {"ok": False, "reason": "plugin disappeared"}
            state = self._record_state(current)
            result = {"ok": state in ("ready", "disabled"),
                      "detail": f"status={current.status}",
                      "checks": [{"name": "loaded",
                                  "ok": current.status == "loaded"}]}
            if state == "failed":
                result["ok"] = False
                result["reason"] = self._record_reason(current)
            elif state == "degraded":
                result["sticky_state"] = "degraded"
                result["reason"] = self._record_reason(current)
            return result

        try:
            register(record.unit_id, probe=probe, restart=restart,
                     disable=disable, enable=enable)
        except Exception:  # pragma: no cover
            self.log.debug("plugin control registration failed", exc_info=True)
        sync = getattr(control, "sync_state", None)
        if sync is not None:
            try:
                sync(record.unit_id, self._record_state(record),
                     self._record_reason(record))
            except Exception:  # pragma: no cover
                pass

    def _sync_unit(self, record: PluginRecord | None) -> None:
        control = getattr(self.app, "unit_control", None)
        sync = getattr(control, "sync_state", None)
        if sync is None or record is None:
            return
        try:
            sync(record.unit_id, self._record_state(record), record.error)
        except Exception:  # pragma: no cover
            pass

    def _sync_host_unit(self) -> None:
        control = getattr(self.app, "unit_control", None)
        sync = getattr(control, "sync_state", None)
        if sync is None:
            return
        try:
            state, reason = self._host_state_reason()
            sync("plugin.0", state, reason)
        except Exception:  # pragma: no cover
            pass

    def probe_host(self) -> dict:
        failed = [record for record in self._records.values()
                  if record.valid and record.status == "failed"]
        invalid = [record for record in self._records.values() if not record.valid]
        result = {"ok": not (failed or invalid),
                  "checks": [{"name": "plugins", "ok": not failed,
                              "detail": f"{len(self._records)} discovered"},
                             {"name": "invalid", "ok": not invalid,
                              "detail": f"{len(invalid)} invalid"}]}
        if failed or invalid:
            result["sticky_state"] = "degraded"
            result["reason"] = f"{len(failed)} failed, {len(invalid)} invalid"
        return result

    async def restart_host(self) -> dict:
        await self.stop()
        self._suppressed = False
        await self.start()
        return {"discovered": len(self._records),
                "loaded": sum(1 for record in self._records.values()
                              if record.status == "loaded")}

    async def disable_host(self) -> None:
        self._suppressed = True
        async with self._lock:
            for record in list(self._records.values()):
                if record.status == "loaded":
                    await self._unload_locked(record.id)
        self._sync_host_unit()

    async def enable_host(self) -> None:
        self._suppressed = False
        await self.start()

    # -- HTTP -------------------------------------------------------------
    def routes(self) -> list[tuple[str, str, Any]]:
        return [
            ("GET", r"/api/plugins", self.api_plugins),
            ("POST", r"/api/plugins/([^/]+)/load", self.api_load),
            ("POST", r"/api/plugins/([^/]+)/unload", self.api_unload),
        ]

    async def api_plugins(self, match, query, body):
        return 200, {"ok": True, "data": {
            "plugins": [record.to_dict() for record in self._records.values()],
            "summary": self.health(),
            "unit_id": "plugin.0",
        }}

    async def api_load(self, match, query, body):
        self._require_operator()
        record = await self.load(match.group(1))
        return 200, {"ok": True, "data": {"plugin": record.to_dict(),
                                          "unit_id": record.unit_id}}

    async def api_unload(self, match, query, body):
        self._require_operator()
        record = await self.unload(match.group(1))
        return 200, {"ok": True, "data": {"plugin": record.to_dict(),
                                          "unit_id": record.unit_id}}

    def _require_operator(self) -> None:
        from ..web.api import current_auth
        role = getattr(current_auth(), "role", None)
        if role is not None and role != "operator":
            raise BadRequest("operator role required")

    def health(self) -> dict:
        records = list(self._records.values())
        return {
            "ok": not any((not record.valid) or record.status == "failed"
                          for record in records),
            "discovered": len(records),
            "loaded": sum(1 for record in records if record.status == "loaded"),
            "failed": sum(1 for record in records if record.status == "failed"),
            "invalid": sum(1 for record in records if not record.valid),
            "suppressed": self._suppressed,
        }

    # -- events -----------------------------------------------------------
    def _emit(self, event_type: str, data: dict) -> None:
        ctx = getattr(self.app, "ctx", None)
        eventlog = getattr(self.app, "eventlog", None)
        if ctx is None or eventlog is None:
            return
        try:
            safe = {key: (redact_text(value) if isinstance(value, str) else value)
                    for key, value in data.items()}
            emit_both(ctx, eventlog, event_type, safe)
        except Exception:  # pragma: no cover
            self.log.debug("plugin event %s failed", event_type, exc_info=True)


def asyncio_lock():
    import asyncio
    return asyncio.Lock()


def build(app: Any) -> PluginLoader:
    loader = PluginLoader(app)
    app.plugin_loader = loader
    app.plugins = loader
    return loader


__all__ = ["PLUGIN_FILE", "PluginLoader", "PluginProvides", "PluginRecord",
           "build", "validate_manifest"]

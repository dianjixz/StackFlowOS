"""SkillRegistry: declarative skills as catalogue entries (``skill.0`` unit).

A skill is data (instructions + trigger/role/tool metadata), never a unit.
The registry scans configured directories, validates every manifest in
isolation, persists global/project enablement, and renders the injectable
prompt section for an agent turn.

Public hand-off API for the Lead/agent wiring is documented in
``docs/team/units.md``; the two functions that matter are::

    registry.render_injection(project_id=None, role=None, prompt=None,
                              triggers=None, max_chars=None) -> str
    registry.is_enabled(skill_id, project_id=None) -> bool

The unit descriptor (``skill.0``) is derived from the real scan result: ready
when the scan completed (even with zero skills), degraded when at least one
manifest is invalid, failed when the scan itself raised.  ``skill.0`` is
therefore no longer hard-coded to ``disabled``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from ..errors import BadRequest, Conflict, NotFound
from ..l2.eventlog import emit_both
from ..protocol import now_ms
from ..util.jsonio import read_json, write_json
from ..util.logging import get_logger
from ..util.redact import redact_text
from .manifest import SkillEntry, parse_skill_dir

STATE_VERSION = 1
MAX_SCAN_DEPTH = 4


class SkillRegistry:
    """Directory-scanning, validation-first skill registry extension."""

    name = "skills"

    def __init__(self, app: Any) -> None:
        self.app = app
        self.log = get_logger("skills")
        section = getattr(getattr(app, "config", None), "skills", None)
        self.max_skills = int(getattr(section, "max_skills", 200) or 200)
        self.inject_max_chars = int(
            getattr(section, "inject_max_chars", 8000) or 8000)
        self._scanned: list[SkillEntry] = []
        self._external: dict[str, tuple[SkillEntry, str]] = {}
        self._state: dict = {"global": {"enabled": [], "disabled": []},
                             "projects": {}}
        self._state_loaded = False
        self._scan_error: str | None = None
        self._suppressed = False
        state_dir = Path(getattr(getattr(app, "paths", None), "state_dir",
                                 Path.cwd()))
        self.state_path = state_dir / "state" / "skills.json"

    # -- configuration ----------------------------------------------------
    def _section(self, name: str, default: Any) -> Any:
        section = getattr(getattr(self.app, "config", None), "skills", None)
        return getattr(section, name, default)

    def resolve_dirs(self) -> list[Path]:
        state_dir = Path(getattr(getattr(self.app, "paths", None), "state_dir",
                                 Path.cwd()))
        raw_dirs = self._section("dirs", ["skills"]) or []
        roots: list[Path] = []
        for raw in raw_dirs:
            path = Path(str(raw)).expanduser()
            if not path.is_absolute():
                path = state_dir / path
            roots.append(path)
        return roots

    # -- state persistence ------------------------------------------------
    def _load_state(self) -> None:
        if self._state_loaded:
            return
        self._state_loaded = True
        data = None
        if self.state_path.exists():
            try:
                data = read_json(self.state_path, default=None)
            except ValueError as exc:
                self.log.warning("skills state unreadable, resetting: %s", exc)
                data = None
        state = {"global": {"enabled": [], "disabled": []}, "projects": {}}
        if isinstance(data, dict):
            glob = data.get("global")
            if isinstance(glob, dict):
                state["global"]["enabled"] = self._string_list(glob.get("enabled"))
                state["global"]["disabled"] = self._string_list(glob.get("disabled"))
            projects = data.get("projects")
            if isinstance(projects, dict):
                for project_id, bucket in projects.items():
                    if not isinstance(bucket, dict):
                        continue
                    state["projects"][str(project_id)] = {
                        "enabled": self._string_list(bucket.get("enabled")),
                        "disabled": self._string_list(bucket.get("disabled")),
                    }
        # ``config.skills.enabled`` is the operator's declarative baseline.
        for skill_id in self._string_list(self._section("enabled", [])):
            if skill_id not in state["global"]["enabled"]:
                state["global"]["enabled"].append(skill_id)
        self._state = state

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple, set)):
            return []
        return [str(item) for item in value if isinstance(item, str) and item.strip()]

    def save(self) -> None:
        try:
            write_json(self.state_path, {
                "version": STATE_VERSION,
                "global": {
                    "enabled": sorted(set(self._state["global"]["enabled"])),
                    "disabled": sorted(set(self._state["global"]["disabled"])),
                },
                "projects": {
                    str(pid): {
                        "enabled": sorted(set(bucket.get("enabled") or [])),
                        "disabled": sorted(set(bucket.get("disabled") or [])),
                    }
                    for pid, bucket in self._state.get("projects", {}).items()
                },
            })
        except OSError as exc:  # pragma: no cover - disk failures are reported
            self.log.warning("cannot persist skills state: %s", exc)

    # -- scanning ---------------------------------------------------------
    def scan(self) -> list[SkillEntry]:
        """Scan every configured root and rebuild the entry list."""
        self._load_state()
        entries: list[SkillEntry] = []
        seen: set[str] = set()
        error: str | None = None
        for root in self.resolve_dirs():
            try:
                if not root.is_dir():
                    continue
                for directory in self._walk_skill_dirs(root):
                    entry = parse_skill_dir(directory)
                    if entry.id in seen:
                        entry = SkillEntry(
                            id=entry.id, name=entry.name or directory.name,
                            path=str(directory), source=entry.source,
                            valid=False,
                            error=f"duplicate skill id {entry.id!r}")
                    elif len(entries) >= self.max_skills:
                        entry = SkillEntry(
                            id=entry.id, name=entry.name or directory.name,
                            path=str(directory), source=entry.source,
                            valid=False,
                            error=f"max_skills ({self.max_skills}) exceeded")
                    else:
                        seen.add(entry.id)
                    entries.append(entry)
            except OSError as exc:
                error = f"cannot scan {root}: {exc}"
                self.log.warning("skill scan failed for %s: %s", root, exc)
        entries.sort(key=lambda item: item.id)
        self._scanned = entries
        self._scan_error = error
        self._apply_state()
        invalid = [item for item in entries if not item.valid]
        self._emit("skill.scanned", {
            "skills": len(entries), "valid": len(entries) - len(invalid),
            "invalid": len(invalid),
            "dirs": [str(path) for path in self.resolve_dirs()],
        })
        for item in invalid:
            self._emit("skill.invalid", {
                "skill_id": item.id, "path": item.path,
                "reason": redact_text(str(item.error or ""))[:500],
            })
        self._sync_unit()
        return entries

    def _walk_skill_dirs(self, root: Path):
        """Yield skill directories, never descending into a skill."""
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name for name in dirnames
                if not name.startswith(".") and name != "__pycache__")
            depth = len(Path(dirpath).relative_to(root).parts)
            if depth >= MAX_SCAN_DEPTH:
                dirnames[:] = []
            if "skill.json" in filenames or "SKILL.md" in filenames:
                dirnames[:] = []
                yield Path(dirpath)

    # -- enablement -------------------------------------------------------
    def _apply_state(self) -> None:
        self._load_state()
        enabled = set(self._state["global"]["enabled"])
        disabled = set(self._state["global"]["disabled"])
        for entry in self._scanned:
            entry.global_enabled = bool(
                entry.valid and entry.id in enabled and entry.id not in disabled)
            projects: list[str] = []
            disabled_projects: list[str] = []
            for project_id, bucket in self._state.get("projects", {}).items():
                if entry.id in (bucket.get("disabled") or []):
                    disabled_projects.append(str(project_id))
                elif entry.id in (bucket.get("enabled") or []):
                    projects.append(str(project_id))
            entry.enabled_projects = sorted(projects)
            entry.disabled_projects = sorted(disabled_projects)
        for skill_id, (entry, _provider) in self._external.items():
            entry.global_enabled = bool(entry.valid and skill_id not in disabled)
            projects = []
            disabled_projects = []
            for project_id, bucket in self._state.get("projects", {}).items():
                if skill_id in (bucket.get("disabled") or []):
                    disabled_projects.append(str(project_id))
                elif skill_id in (bucket.get("enabled") or []):
                    projects.append(str(project_id))
            entry.enabled_projects = sorted(projects)
            entry.disabled_projects = sorted(disabled_projects)

    def all_entries(self) -> list[SkillEntry]:
        entries = list(self._scanned) + [item[0] for item in self._external.values()]
        return sorted(entries, key=lambda item: item.id)

    def get(self, skill_id: str) -> SkillEntry:
        for entry in self.all_entries():
            if entry.id == str(skill_id):
                return entry
        raise NotFound(f"unknown skill: {skill_id!r}",
                       detail={"skill_id": skill_id})

    def is_enabled(self, skill_id: str, project_id: str | None = None) -> bool:
        try:
            return self.get(skill_id).enabled_for(project_id)
        except NotFound:
            return False

    def _bucket(self, project_id: str | None) -> dict:
        self._load_state()
        if project_id is None:
            return self._state["global"]
        bucket = self._state["projects"].setdefault(
            str(project_id), {"enabled": [], "disabled": []})
        return bucket

    def _require_project(self, project_id: str | None) -> None:
        if project_id is None:
            return
        projects = getattr(self.app, "projects", None)
        if projects is None:
            raise NotFound(f"project {project_id!r} not found")
        projects.get(project_id)

    def set_enabled(self, skill_id: str, enabled: bool,
                    project_id: str | None = None) -> SkillEntry:
        entry = self.get(skill_id)
        if not entry.valid:
            raise Conflict(f"skill {skill_id!r} is invalid: {entry.error}",
                           detail={"skill_id": skill_id, "error": entry.error})
        self._require_project(project_id)
        bucket = self._bucket(project_id)
        enabled_list = bucket.setdefault("enabled", [])
        disabled_list = bucket.setdefault("disabled", [])
        if enabled:
            if skill_id not in enabled_list:
                enabled_list.append(skill_id)
            if skill_id in disabled_list:
                disabled_list.remove(skill_id)
        else:
            if skill_id not in disabled_list:
                disabled_list.append(skill_id)
            if skill_id in enabled_list:
                enabled_list.remove(skill_id)
        self.save()
        self._apply_state()
        self._emit("skill.enabled" if enabled else "skill.disabled", {
            "skill_id": skill_id,
            "project_id": project_id,
            "scope": "project" if project_id else "global",
            "by": "operator",
        }, project_id=project_id)
        self._sync_unit()
        return entry

    # -- external (plugin) skills ----------------------------------------
    def add_external_skill(self, data: dict, *,
                           provider: str) -> tuple[SkillEntry, Callable[[], None]]:
        """Register a plugin-provided skill; returns ``(entry, disposer)``."""
        if not isinstance(data, dict):
            raise BadRequest("skill contribution must be an object")
        skill_id = str(data.get("id") or "").strip()
        if not skill_id:
            raise BadRequest("contributed skill requires an id")
        if skill_id in self._external or any(
                entry.id == skill_id for entry in self._scanned):
            raise BadRequest(f"duplicate skill id {skill_id!r}")
        name = str(data.get("name") or skill_id).strip()
        version = str(data.get("version") or "0.0.0").strip() or "0.0.0"
        instructions = str(data.get("instructions") or "").strip()
        if not instructions:
            raise BadRequest(f"contributed skill {skill_id!r} requires instructions")
        roles = data.get("allowed_roles") or []
        triggers = data.get("triggers") or []
        tools = data.get("requires_tools") or []
        for field_name, value in (("allowed_roles", roles), ("triggers", triggers),
                                  ("requires_tools", tools)):
            if not isinstance(value, (list, tuple)) or not all(
                    isinstance(item, str) for item in value):
                raise BadRequest(f"{field_name} must be a list of strings")
        entry = SkillEntry(
            id=skill_id, name=name, version=version,
            description=str(data.get("description") or ""),
            instructions=instructions, triggers=[str(i) for i in triggers],
            allowed_roles=[str(i) for i in roles],
            requires_tools=[str(i) for i in tools],
            path=str(data.get("path") or ""), source="plugin",
            provider=provider, global_enabled=True)
        self._external[skill_id] = (entry, provider)
        self._apply_state()
        self._emit("skill.scanned", {"skills": len(self.all_entries()),
                                     "valid": len(self.all_entries()),
                                     "invalid": self._invalid_count(),
                                     "external": provider})

        def remove() -> None:
            item = self._external.pop(skill_id, None)
            if item is not None:
                self._emit("skill.scanned", {
                    "skills": len(self.all_entries()),
                    "valid": len(self.all_entries()),
                    "invalid": self._invalid_count(),
                    "removed": skill_id,
                })

        return entry, remove

    def _invalid_count(self) -> int:
        return sum(1 for item in self.all_entries() if not item.valid)

    # -- injection rendering ---------------------------------------------
    def render_injection(self, project_id: str | None = None,
                         role: str | None = None,
                         prompt: str | None = None, *,
                         triggers: list[str] | None = None,
                         max_chars: int | None = None) -> str:
        """Render enabled skills as one prompt section.

        Filtering:

        * project scope: globally enabled skills plus skills enabled for
          ``project_id`` (project-level ``disabled`` wins);
        * ``role``: ``allowed_roles`` must be empty or contain the role;
        * ``prompt``/``triggers``: a skill with triggers is only injected when
          one of its triggers occurs in the prompt (case-insensitive) or is
          listed explicitly.  A skill with triggers is skipped when neither is
          supplied;
        * ``requires_tools``: when ``role`` is known, the role must actually be
          granted every required tool.

        The returned text is a single markdown block truncated to
        ``max_chars`` (default ``skills.inject_max_chars``) at a character
        boundary.  Returns ``""`` when nothing applies.
        """
        limit = int(max_chars or self.inject_max_chars)
        prompt_text = str(prompt or "")
        lowered = prompt_text.lower()
        explicit_triggers = {str(item).strip().lower()
                             for item in (triggers or []) if str(item).strip()}
        role_tools: set[str] | None = None
        if role:
            try:
                from ..agents.tools import tools_for
                role_tools = {spec.name for spec in tools_for(str(role))}
            except Exception:  # pragma: no cover - best-effort filter
                role_tools = None

        selected: list[SkillEntry] = []
        for entry in self.all_entries():
            if not entry.valid or not entry.enabled_for(project_id):
                continue
            if role is not None:
                if entry.allowed_roles and str(role) not in entry.allowed_roles:
                    continue
            elif entry.allowed_roles:
                # A role-restricted skill is not injected without a role.
                continue
            if entry.triggers:
                matched = any(str(trigger).strip().lower() in lowered
                              for trigger in entry.triggers if str(trigger).strip())
                matched = matched or any(
                    str(trigger).strip().lower() in explicit_triggers
                    for trigger in entry.triggers if str(trigger).strip())
                if not matched:
                    continue
            if role_tools is not None and entry.requires_tools:
                if not set(entry.requires_tools).issubset(role_tools):
                    continue
            selected.append(entry)

        blocks: list[str] = []
        for entry in selected:
            header = f"## Skill: {entry.name} ({entry.id} v{entry.version})"
            body = entry.instructions.strip()
            if entry.description:
                body = f"{entry.description.strip()}\n\n{body}"
            blocks.append(f"{header}\n{body}")
        text = "\n\n".join(blocks)
        if limit > 0 and len(text) > limit:
            text = text[:max(0, limit - 24)].rstrip() + "\n...[skill truncated]..."
        return text

    # Compatibility alias used by early agent wiring.
    def injection_text(self, project_id: str | None = None,
                       role: str | None = None,
                       prompt: str | None = None, **kwargs: Any) -> str:
        return self.render_injection(project_id=project_id, role=role,
                                     prompt=prompt, **kwargs)

    # -- unit / capabilities / routes ------------------------------------
    def derived_state(self) -> tuple[str, str | None]:
        if self._suppressed:
            return "disabled", "disabled by operator"
        if self._scan_error:
            return "failed", self._scan_error
        invalid = [item for item in self._scanned if not item.valid]
        if invalid:
            names = ", ".join(item.id for item in invalid[:5])
            return "degraded", f"{len(invalid)} invalid skill manifest(s): {names}"
        return "ready", None

    def _unit(self) -> dict:
        state, reason = self.derived_state()
        entries = self.all_entries()
        enabled = [item for item in entries if item.valid and item.global_enabled]
        now = now_ms()
        descriptor = {
            "work_id": "skill.0",
            "unit_id": "skill.0",
            "kind": "skill",
            "role": None,
            "tier": "service",
            "label": "技能注册表",
            "project_id": None,
            "pid": None,
            "host": getattr(
                getattr(getattr(self.app, "config", None), "system", None),
                "host_label", ""),
            "transport": "inproc",
            "rpc_address": None,
            "events_address": None,
            "status": {"ready": "online", "degraded": "degraded",
                       "disabled": "disabled", "failed": "failed",
                       "draining": "draining"}.get(state, "online"),
            "state": state,
            "reason": reason,
            "abilities": [{"name": "skill.list", "scope": "system"},
                          {"name": "skill.inject", "scope": "service"}],
            "hwinfo": {},
            "started_at": getattr(self.app, "started_at", now),
            "last_seen": now,
            "restart_count": 0,
            "metadata": {
                "description": "skill manifest 目录；单个 skill 不是单元",
                "dirs": [str(path) for path in self.resolve_dirs()],
                "invalid": [{"id": item.id, "error": item.error}
                            for item in entries if not item.valid],
                "plugins": len(self._external),
            },
            "runtime": {"mode": "in_process", "host_unit": "sys.0",
                        "host": getattr(
                            getattr(getattr(self.app, "config", None),
                                    "system", None), "host_label", ""),
                        "pid": None, "module": "stackflowos.skills:SkillRegistry",
                        "started_at": getattr(self.app, "started_at", now)},
            "endpoints": [{"name": "inproc", "transport": "inproc",
                           "address": "skill.0"}],
            "ref": "unit://skill.0",
            "critical": False,
            "requires": ["store.eventlog"],
            "actions": ["probe", "restart", "disable", "enable",
                        "drain", "undrain"],
            "lease": {"type": "host", "host_unit": "sys.0",
                      "interval_s": 5, "renewed_at": now},
            "provides": {"skills": len([e for e in entries if e.valid]),
                         "invalid": len([e for e in entries if not e.valid]),
                         "enabled": len(enabled)},
        }
        return self._overlay(descriptor)

    def _overlay(self, descriptor: dict) -> dict:
        control = getattr(self.app, "unit_control", None)
        overlay = getattr(control, "overlay", None)
        if overlay is None:
            return descriptor
        try:
            return overlay(descriptor)
        except Exception:  # pragma: no cover - diagnostics only
            return descriptor

    def units(self) -> list[dict]:
        return [self._unit()]

    def capabilities(self, kind: str | None = None) -> list[dict]:
        if kind not in (None, "skill"):
            return []
        result: list[dict] = []
        for entry in self.all_entries():
            if not entry.valid:
                continue
            scopes: list[str] = []
            if entry.global_enabled:
                scopes.append("*")
            scopes.extend(entry.enabled_projects)
            if not scopes:
                continue
            result.append({
                "type": "skill",
                "name": entry.id,
                "description": entry.description,
                "version": entry.version,
                "provider": entry.provider,
                "enabled_in": scopes,
                "risk": "low",
                "schema": {"type": "skill",
                           "triggers": list(entry.triggers),
                           "allowed_roles": list(entry.allowed_roles),
                           "requires_tools": list(entry.requires_tools)},
                "capability": None,
                "metadata": {"path": entry.path, "source": entry.source},
            })
        return result

    def health(self) -> dict:
        entries = self.all_entries()
        invalid = [item for item in entries if not item.valid]
        state, reason = self.derived_state()
        return {"ok": state in ("ready", "degraded"), "state": state,
                "reason": reason, "skills": len(entries),
                "invalid": len(invalid),
                "enabled": sum(1 for item in entries
                               if item.valid and item.global_enabled),
                "dirs": [str(path) for path in self.resolve_dirs()]}

    def routes(self) -> list[tuple[str, str, Any]]:
        return [
            ("GET", r"/api/skills", self.api_skills),
            ("POST", r"/api/skills/([^/]+)/enable", self.api_enable),
            ("POST", r"/api/skills/([^/]+)/disable", self.api_disable),
        ]

    async def api_skills(self, match, query, body):
        project_id = query.get("project_id") or None
        if isinstance(project_id, str):
            project_id = project_id.strip() or None
        self._require_project(project_id)
        entries = [entry.to_dict(project_id) for entry in self.all_entries()]
        state, reason = self.derived_state()
        return 200, {"ok": True, "data": {
            "skills": entries,
            "project_id": project_id,
            "summary": {
                "total": len(entries),
                "valid": sum(1 for item in self.all_entries() if item.valid),
                "invalid": self._invalid_count(),
                "enabled": sum(1 for entry in self.all_entries()
                               if entry.enabled_for(project_id)),
                "state": state,
                "reason": reason,
            },
            "unit_id": "skill.0",
        }}

    async def api_enable(self, match, query, body):
        return self._toggle(match, body, True)

    async def api_disable(self, match, query, body):
        return self._toggle(match, body, False)

    def _toggle(self, match, body: dict, enabled: bool):
        self._require_operator()
        project_id = None
        if isinstance(body, dict) and body.get("project_id") not in (None, ""):
            project_id = str(body["project_id"]).strip() or None
        entry = self.set_enabled(match.group(1), enabled, project_id)
        return 200, {"ok": True, "data": {
            "skill": entry.to_dict(project_id),
            "project_id": project_id,
            "enabled": enabled,
        }}

    def _require_operator(self) -> None:
        from ..web.api import current_auth
        role = getattr(current_auth(), "role", None)
        if role is not None and role != "operator":
            raise BadRequest("operator role required")

    # -- lifecycle --------------------------------------------------------
    def _sync_unit(self) -> None:
        control = getattr(self.app, "unit_control", None)
        sync = getattr(control, "sync_state", None)
        if sync is None:
            return
        state, reason = self.derived_state()
        try:
            sync("skill.0", state, reason)
        except Exception:  # pragma: no cover - unit control is diagnostic
            self.log.debug("skill.0 state sync failed", exc_info=True)

    def _register_control(self) -> None:
        control = getattr(self.app, "unit_control", None)
        register = getattr(control, "register_provider", None)
        if register is None:
            return
        try:
            register("skill.0", probe=self.probe, restart=self.restart,
                     disable=self.disable_unit, enable=self.enable_unit)
        except Exception:  # pragma: no cover
            self.log.debug("skill.0 control registration failed", exc_info=True)
        self._sync_unit()

    def probe(self) -> dict:
        state, reason = self.derived_state()
        checks = [{"name": "scan", "ok": self._scan_error is None,
                   "detail": f"{len(self._scanned)} dir(s)"},
                  {"name": "invalid", "ok": not any(
                      not item.valid for item in self._scanned),
                   "detail": f"{self._invalid_count()} invalid"}]
        result = {"ok": self._scan_error is None, "checks": checks,
                  "detail": f"{len(self.all_entries())} skill(s)"}
        if state == "degraded":
            result["sticky_state"] = "degraded"
            result["reason"] = reason
        return result

    async def restart(self) -> dict:
        before = len(self._scanned)
        self._scan_error = None
        self.scan()
        return {"rescanned": True, "before": before,
                "after": len(self._scanned), "state": self.derived_state()[0]}

    async def disable_unit(self) -> None:
        self._suppressed = True
        self._sync_unit()
        self._emit("skill.disabled", {"skill_id": "*", "scope": "unit",
                                      "by": "operator"})

    async def enable_unit(self) -> None:
        self._suppressed = False
        self._sync_unit()

    async def start(self) -> None:
        self._load_state()
        self._scan_error = None
        self.scan()
        self._register_control()

    async def stop(self) -> None:
        self.save()

    async def on_config(self, config) -> None:
        self.max_skills = int(getattr(config.skills, "max_skills",
                                       self.max_skills) or self.max_skills)
        self.inject_max_chars = int(
            getattr(config.skills, "inject_max_chars",
                    self.inject_max_chars) or self.inject_max_chars)
        self._state_loaded = False
        self._apply_state()
        self.scan()

    # -- events -----------------------------------------------------------
    def _emit(self, event_type: str, data: dict,
              project_id: str | None = None) -> None:
        ctx = getattr(self.app, "ctx", None)
        eventlog = getattr(self.app, "eventlog", None)
        if ctx is None or eventlog is None:
            return
        try:
            safe = {key: (redact_text(value) if isinstance(value, str) else value)
                    for key, value in data.items()}
            emit_both(ctx, eventlog, event_type, safe, project_id)
        except Exception:  # pragma: no cover - events never break the API
            self.log.debug("skill event %s failed", event_type, exc_info=True)


def build(app: Any) -> SkillRegistry:
    registry = SkillRegistry(app)
    app.skills = registry
    app.skill_registry = registry
    return registry


__all__ = ["SkillRegistry", "build"]

"""Skill manifest discovery/validation (stdlib only).

A skill is a *directory* below one of ``skills.dirs`` containing either a
``skill.json`` manifest or a ``SKILL.md`` file (optionally with a simple YAML
front-matter block).  It is data, not a unit: the registry (``skill.0``) is the
unit, each directory is a catalogue entry.

Validation is total: a broken manifest produces an *invalid entry with a
reason* instead of raising, so one bad skill cannot take the registry down.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

#: Skill ids are used in URLs and state files; keep them boring.
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Hard read caps: a skill is instructions, not a payload.
MAX_MANIFEST_BYTES = 256 * 1024
MAX_INSTRUCTIONS_CHARS = 200_000
MAX_DESCRIPTION_CHARS = 2_000

_LIST_FIELDS = ("triggers", "allowed_roles", "requires_tools")


@dataclass
class SkillEntry:
    """One scanned skill directory, valid or not."""

    id: str
    name: str = ""
    version: str = "0.0.0"
    description: str = ""
    instructions: str = ""
    triggers: list[str] = field(default_factory=list)
    allowed_roles: list[str] = field(default_factory=list)
    requires_tools: list[str] = field(default_factory=list)
    path: str = ""
    source: str = "markdown"      # markdown | json | mixed
    valid: bool = True
    error: str | None = None
    provider: str = "skill.0"
    global_enabled: bool = False
    enabled_projects: list[str] = field(default_factory=list)
    disabled_projects: list[str] = field(default_factory=list)

    def enabled_for(self, project_id: str | None = None) -> bool:
        if not self.valid:
            return False
        if project_id is not None:
            if str(project_id) in self.disabled_projects:
                return False
            if str(project_id) in self.enabled_projects:
                return True
        return bool(self.global_enabled)

    def status_for(self, project_id: str | None = None) -> str:
        if not self.valid:
            return "invalid"
        return "enabled" if self.enabled_for(project_id) else "disabled"

    def to_dict(self, project_id: str | None = None) -> dict:
        return {
            "id": self.id,
            "name": self.name or self.id,
            "version": self.version,
            "description": self.description,
            "triggers": list(self.triggers),
            "allowed_roles": list(self.allowed_roles),
            "requires_tools": list(self.requires_tools),
            "path": self.path,
            "source": self.source,
            "valid": bool(self.valid),
            "status": self.status_for(project_id),
            "error": self.error,
            "provider": self.provider,
            "enabled_global": bool(self.global_enabled),
            "enabled_projects": sorted(self.enabled_projects),
            "disabled_projects": sorted(self.disabled_projects),
            "instructions_chars": len(self.instructions),
        }


def _read_text(path: Path, limit: int = MAX_MANIFEST_BYTES) -> str:
    with path.open("rb") as fh:
        raw = fh.read(limit + 1)
    if len(raw) > limit:
        raw = raw[:limit]
    return raw.decode("utf-8", errors="replace")


def _parse_scalar(value: str):
    value = value.strip()
    if not value:
        return ""
    if value.startswith("[") or value.startswith("{"):
        try:
            return json.loads(value)
        except ValueError:
            return value
    if (value.startswith('"') and value.endswith('"')) or \
            (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Parse a tiny YAML-ish ``---`` front-matter block (no dependencies)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            end = index
            break
    if end is None:
        return {}, text
    meta: dict = {}
    last_key: str | None = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and last_key:
            value = meta.get(last_key)
            if not isinstance(value, list):
                value = [] if not value else [value]
                meta[last_key] = value
            value.append(_parse_scalar(line.lstrip()[2:]))
            continue
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        last_key = key.strip()
        meta[last_key] = _parse_scalar(raw)
    body = "\n".join(lines[end + 1:]).strip()
    return meta, body


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _first_paragraph(body: str) -> str:
    paragraph: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            if paragraph:
                break
            continue
        if stripped.startswith("#"):
            continue
        paragraph.append(stripped)
    return " ".join(paragraph)[:MAX_DESCRIPTION_CHARS]


def _as_string_list(value, *, field_name: str) -> tuple[list[str] | None, str | None]:
    if value is None or value == "":
        return [], None
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return None, f"{field_name} must be a list of strings"
    result: list[str] = []
    for item in items:
        if not isinstance(item, str):
            return None, f"{field_name} must be a list of strings"
        text = item.strip()
        if text:
            result.append(text)
    if len(result) > 64:
        return None, f"{field_name} has too many entries (max 64)"
    return result, None


def _invalid(directory: Path, skill_id: str, error: str,
             *, source: str = "markdown", name: str = "") -> SkillEntry:
    return SkillEntry(id=skill_id or directory.name, name=name or skill_id or directory.name,
                      path=str(directory), source=source, valid=False, error=error)


def parse_skill_dir(directory: Path) -> SkillEntry:
    """Read one skill directory and return a (possibly invalid) entry."""
    directory = Path(directory)
    manifest_path = directory / "skill.json"
    markdown_path = directory / "SKILL.md"
    manifest: dict = {}
    meta: dict = {}
    body = ""
    source = "markdown"

    if manifest_path.is_file():
        source = "json"
        try:
            data = json.loads(_read_text(manifest_path))
        except ValueError as exc:
            return _invalid(directory, directory.name,
                            f"invalid skill.json: {exc}", source="json")
        if not isinstance(data, dict):
            return _invalid(directory, directory.name,
                            "skill.json must contain an object", source="json")
        manifest = data
    if markdown_path.is_file():
        try:
            text = _read_text(markdown_path)
        except OSError as exc:
            return _invalid(directory, directory.name,
                            f"cannot read SKILL.md: {exc}")
        meta, body = parse_front_matter(text)
        if manifest:
            source = "mixed"

    if not manifest and not markdown_path.is_file():
        return _invalid(directory, directory.name,
                        "no SKILL.md or skill.json found")

    # --- identity -----------------------------------------------------
    raw_id = manifest.get("id", meta.get("id"))
    if raw_id is None:
        if manifest:
            return _invalid(directory, directory.name,
                            "skill.json requires an id", source=source)
        raw_id = directory.name
    if not isinstance(raw_id, str) or not ID_RE.match(raw_id.strip()):
        return _invalid(directory, directory.name,
                        f"invalid skill id: {raw_id!r}",
                        source=source)
    skill_id = raw_id.strip()

    raw_name = manifest.get("name", meta.get("name"))
    if raw_name is not None and (not isinstance(raw_name, str) or not raw_name.strip()):
        return _invalid(directory, skill_id, "name must be a non-empty string",
                        source=source)
    name = (raw_name or "").strip() or _first_heading(body) or skill_id

    raw_version = manifest.get("version", meta.get("version"))
    if raw_version is None:
        if manifest:
            return _invalid(directory, skill_id,
                            "skill.json requires a version", source=source)
        raw_version = "0.0.0"
    if not isinstance(raw_version, str) or not raw_version.strip():
        return _invalid(directory, skill_id, "version must be a non-empty string",
                        source=source)
    version = raw_version.strip()

    raw_description = manifest.get("description", meta.get("description"))
    if raw_description is not None and not isinstance(raw_description, str):
        return _invalid(directory, skill_id, "description must be a string",
                        source=source)
    description = (raw_description or _first_paragraph(body))[:MAX_DESCRIPTION_CHARS]

    # --- instructions -------------------------------------------------
    raw_instructions = manifest.get("instructions", meta.get("instructions"))
    if raw_instructions is None:
        raw_instructions = body
    if not isinstance(raw_instructions, str) or not raw_instructions.strip():
        return _invalid(directory, skill_id,
                        "instructions must be a non-empty string", source=source)
    instructions = raw_instructions.strip()
    if len(instructions) > MAX_INSTRUCTIONS_CHARS:
        instructions = instructions[:MAX_INSTRUCTIONS_CHARS]

    # --- lists --------------------------------------------------------
    lists: dict[str, list[str]] = {}
    for field_name in _LIST_FIELDS:
        value = manifest.get(field_name, meta.get(field_name))
        parsed, error = _as_string_list(value, field_name=field_name)
        if error:
            return _invalid(directory, skill_id, error, source=source)
        lists[field_name] = parsed or []

    return SkillEntry(
        id=skill_id, name=name, version=version, description=description,
        instructions=instructions, triggers=lists["triggers"],
        allowed_roles=lists["allowed_roles"],
        requires_tools=lists["requires_tools"], path=str(directory),
        source=source)


__all__ = ["ID_RE", "MAX_INSTRUCTIONS_CHARS", "SkillEntry", "parse_front_matter",
           "parse_skill_dir"]

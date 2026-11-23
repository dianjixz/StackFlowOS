"""Capability catalogue and risk assessment (§6.1).

A capability is a *named permission* with a base risk. The catalogue is the
closed table from docs/architecture.md §6.1 — if it is not here, the system
cannot grant it (``get_capability`` raises :class:`BadRequest`).

``risk_of`` is the dynamic half: the same ``fs.write`` is medium inside a
project workspace and high outside it; ``shell.exec`` is high when the command
matches a destructive pattern. Escalation never lowers a capability's base
risk, it only raises it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..errors import BadRequest, ValidationError
from ..util.net import is_private_host

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class CapabilityDef:
    """Static description of one capability."""

    name: str
    risk: str
    description: str
    scope_keys: tuple[str, ...] = ()


#: Exactly the table in docs/architecture.md §6.1.
CAPABILITIES: dict[str, CapabilityDef] = {
    "fs.read": CapabilityDef("fs.read", "low", "读文件", ("path",)),
    "fs.list": CapabilityDef("fs.list", "low", "列目录", ("path",)),
    "fs.write": CapabilityDef("fs.write", "medium",
                              "写文件（workspace 外升 high）", ("path", "bytes")),
    "fs.delete": CapabilityDef("fs.delete", "high", "删文件（默认不可用）", ("path",)),
    "shell.exec": CapabilityDef("shell.exec", "high",
                                "执行命令（危险模式升 high）", ("cwd", "command")),
    "net.fetch": CapabilityDef("net.fetch", "medium", "出站 HTTP", ("url",)),
    "llm.chat": CapabilityDef("llm.chat", "low", "调模型", ("model",)),
    "project.create": CapabilityDef("project.create", "low", "建项目", ()),
    "project.update": CapabilityDef("project.update", "medium", "改项目",
                                    ("project_id",)),
    "project.attach": CapabilityDef("project.attach", "high",
                                    "接入已有本地目录作为项目", ("path",)),
    "project.archive": CapabilityDef("project.archive", "medium",
                                     "归档项目并停止其 Agent", ("project_id",)),
    "project.delete": CapabilityDef("project.delete", "high",
                                    "永久删除项目记录与工作区", ("project_id",
                                                               "purge_workspace")),
    "task.manage": CapabilityDef("task.manage", "low", "建/改任务",
                                 ("project_id",)),
    "agent.spawn": CapabilityDef("agent.spawn", "medium", "拉起工人",
                                 ("project_id", "role")),
    "agent.kill": CapabilityDef("agent.kill", "high", "杀掉工人", ("agent_id",)),
    "unit.control": CapabilityDef("unit.control", "high", "暂停/杀死单元",
                                  ("work_id",)),
    "sys.config": CapabilityDef("sys.config", "high", "改系统配置", ("section",)),
    "approval.decide": CapabilityDef("approval.decide", "high",
                                     "放行/拒绝审批", ("request_id",)),
    "data.read": CapabilityDef("data.read", "low", "读项目数据/事件",
                               ("project_id",)),
    "data.write": CapabilityDef("data.write", "medium", "写项目记忆",
                                ("project_id",)),
}


def get_capability(name: str) -> CapabilityDef:
    """Return the definition for ``name`` or raise :class:`BadRequest`."""
    try:
        return CAPABILITIES[str(name)]
    except KeyError as exc:
        raise BadRequest(f"unknown capability: {name!r}",
                         detail={"capability": name,
                                 "known": sorted(CAPABILITIES)}) from exc


#: Commands that are destructive enough to always escalate to high risk.
DANGEROUS_PATTERNS: tuple[re.Pattern, ...] = (
    # rm -rf /  (and variants with extra flags / trailing glob)
    re.compile(r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+/(?:\s|$|\*)", re.IGNORECASE),
    re.compile(r"\brm\b[^;&|\n]*\s--no-preserve-root\b", re.IGNORECASE),
    # privilege escalation
    re.compile(r"\bsudo\b", re.IGNORECASE),
    re.compile(r"\b(?:su|doas)\s+-\s*(?:root|c)\b", re.IGNORECASE),
    # filesystem creation / wipe
    re.compile(r"\bmkfs(?:\.[a-zA-Z0-9]+)?\b", re.IGNORECASE),
    re.compile(r"\bwipefs\b", re.IGNORECASE),
    # raw device writes
    re.compile(r"\bdd\s+[^|;&\n]*\bif\s*=", re.IGNORECASE),
    re.compile(r"\bdd\s+[^|;&\n]*\bof\s*=\s*/dev/", re.IGNORECASE),
    re.compile(r">\s*/dev/(?:sd|nvme|hd|vd|disk)", re.IGNORECASE),
    # pipe-to-shell installers
    re.compile(r"\b(?:curl|wget)\b[^|;&\n]*\|\s*(?:sudo\s+)?"
               r"(?:(?:ba|da|z|k)?sh|python[0-9.]*|perl|ruby)\b", re.IGNORECASE),
    # world-writable root / recursive chmod of system paths
    re.compile(r"\bchmod\s+(?:-R\s+)?777\s+/", re.IGNORECASE),
    re.compile(r"\bchown\s+(?:-R\s+)?[^;|&\n]*\s+/(?:\s|$)", re.IGNORECASE),
    # fork bomb: :(){ :|:& };:
    re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", re.IGNORECASE),
    # system power / reboot
    re.compile(r"\b(?:shutdown|reboot|poweroff|halt)\b", re.IGNORECASE),
)


#: A project id is one path segment: it is joined as
#: ``<projects_root>/<project_id>/workspace``, so it must never be able to
#: carry ``/``, ``\\``, ``..``, NUL or whitespace into that join.
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_project_id(project_id: object) -> str:
    """Return ``project_id`` when it is a safe single path segment.

    Project ids reach the policy layer from rules, RPC scopes and persisted
    records — input that may be hostile or stale. Reject anything that is not
    a plain ``[A-Za-z0-9][A-Za-z0-9._-]{0,63}`` string instead of silently
    normalising it, so a bad id is a 400 (:class:`ValidationError`) rather
    than a redefined filesystem boundary.
    """
    if not isinstance(project_id, str):
        raise ValidationError("invalid project id",
                              detail={"project_id": repr(project_id)})
    if project_id in (".", "..") or not _PROJECT_ID_RE.fullmatch(project_id):
        raise ValidationError("invalid project id",
                              detail={"project_id": project_id})
    return project_id


def project_workspace(projects_root: str | Path, project_id: str) -> Path:
    """Resolve ``<projects_root>/<project_id>/workspace`` safely.

    The id is validated first, then both paths are resolved and the result is
    checked to stay inside ``projects_root`` (symlink-safe via
    :func:`is_within`). The directory does not need to exist: callers such as
    :func:`risk_of` only need the boundary, not the files.
    """
    pid = validate_project_id(project_id)
    root = Path(projects_root).resolve()
    workspace = (root / pid / "workspace").resolve()
    if not is_within(workspace, root):
        raise ValidationError(
            "invalid project id",
            detail={"project_id": pid,
                    "reason": "workspace escapes projects root"})
    return workspace


def is_within(path: str | Path, root: str | Path) -> bool:
    """True when ``path`` resolves inside ``root`` (symlink-safe)."""
    try:
        resolved = Path(path).expanduser().resolve()
        boundary = Path(root).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    try:
        resolved.relative_to(boundary)
    except ValueError:
        return False
    return True


def normalize_path(path: str | Path, *, cwd: str | Path | None = None) -> Path:
    """Expand ``~``, resolve relative paths against ``cwd`` and canonicalise."""
    target = Path(str(path)).expanduser()
    if not target.is_absolute():
        base = Path(str(cwd)).expanduser() if cwd is not None else Path.cwd()
        if not base.is_absolute():
            base = base.resolve()
        target = base / target
    try:
        return target.resolve()
    except (OSError, RuntimeError):
        return target.absolute()


def risk_of(capability: str, scope: dict, *, projects_root: Path,
            workspace: Path | None = None) -> str:
    """Base risk of ``capability``, escalated by the concrete scope.

    ``workspace`` (a project's workspace) narrows the allowed boundary;
    without it, ``projects_root`` is the boundary. Unknown scope keys are
    ignored on purpose: the capability table is the contract.
    """
    definition = get_capability(capability)
    level = definition.risk
    scope = scope or {}
    boundary = Path(workspace) if workspace is not None else Path(projects_root)

    if capability.startswith("fs."):
        raw_path = scope.get("path")
        if raw_path:
            target = normalize_path(raw_path, cwd=scope.get("cwd"))
            if not is_within(target, boundary):
                level = _max_risk(level, "high")
    elif capability == "shell.exec":
        raw_cwd = scope.get("cwd")
        if raw_cwd:
            target = normalize_path(raw_cwd)
            if not is_within(target, boundary):
                level = _max_risk(level, "high")
        command = str(scope.get("command") or "")
        if command and _is_dangerous(command):
            level = _max_risk(level, "high")
    elif capability == "net.fetch":
        url = str(scope.get("url") or "")
        if url:
            parts = urlsplit(url)
            scheme = (parts.scheme or "").lower()
            if scheme not in ("http", "https"):
                level = _max_risk(level, "high")
            elif is_private_host(parts.hostname or ""):
                # M4: private targets are never "medium like any fetch";
                # they stay high even when ``allow_private_network`` is on.
                level = _max_risk(level, "high")

    return level


def _max_risk(current: str, candidate: str) -> str:
    if RISK_ORDER.get(candidate, 2) > RISK_ORDER.get(current, 0):
        return candidate
    return current


def _is_dangerous(command: str) -> bool:
    return any(pattern.search(command) for pattern in DANGEROUS_PATTERNS)


__all__ = [
    "CAPABILITIES",
    "RISK_ORDER",
    "CapabilityDef",
    "DANGEROUS_PATTERNS",
    "get_capability",
    "is_private_host",
    "is_within",
    "normalize_path",
    "project_workspace",
    "risk_of",
    "validate_project_id",
]

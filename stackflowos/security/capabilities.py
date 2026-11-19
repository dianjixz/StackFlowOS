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

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..errors import BadRequest

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
            elif _is_private_host(parts.hostname or ""):
                level = _max_risk(level, "medium")

    return level


def _max_risk(current: str, candidate: str) -> str:
    if RISK_ORDER.get(candidate, 2) > RISK_ORDER.get(current, 0):
        return candidate
    return current


def _is_dangerous(command: str) -> bool:
    return any(pattern.search(command) for pattern in DANGEROUS_PATTERNS)


def _is_private_host(host: str) -> bool:
    """True for loopback, RFC1918/link-local, ``.local`` and single-label hosts."""
    host = host.strip().strip("[]").lower()
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host
    return (address.is_private or address.is_loopback or address.is_link_local
            or address.is_reserved or address.is_unspecified)


__all__ = [
    "CAPABILITIES",
    "RISK_ORDER",
    "CapabilityDef",
    "DANGEROUS_PATTERNS",
    "get_capability",
    "is_within",
    "normalize_path",
    "risk_of",
]

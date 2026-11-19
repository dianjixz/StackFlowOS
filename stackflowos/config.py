"""Configuration and filesystem layout.

Precedence: built-in defaults < ``<state>/config.json`` < environment.
The state directory defaults to ``<repo>/.stackflowos`` and holds every byte
this system writes: config, event log, project workspaces, logs. Nothing is
written outside the repository unless the operator explicitly points
``SFO_STATE_DIR`` elsewhere.
"""

from __future__ import annotations

import dataclasses
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

from .errors import ConfigError
from .util.jsonio import deep_merge, ensure_dir, read_json, write_json

ENV_ROOT = "SFO_ROOT"
ENV_STATE = "SFO_STATE_DIR"
ENV_WEB_PORT = "SFO_WEB_PORT"
ENV_TRANSPORT = "SFO_TRANSPORT_MODE"
ENV_LLM_PROVIDER = "SFO_LLM_PROVIDER"
ENV_LLM_BASE_URL = "SFO_LLM_BASE_URL"
ENV_LLM_MODEL = "SFO_LLM_MODEL"
ENV_LLM_API_KEY = "SFO_LLM_API_KEY"


def default_root() -> Path:
    env = os.environ.get(ENV_ROOT)
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


@dataclass
class SystemCfg:
    name: str = "StackFlowOS"
    host_label: str = field(default_factory=lambda: socket.gethostname())
    language: str = "zh-CN"


@dataclass
class TransportCfg:
    mode: str = "ipc"              # ipc | tcp
    host: str = "127.0.0.1"
    advertise_host: str | None = None   # what remote workers should dial
    port_pool_start: int = 17820
    port_pool_end: int = 17899
    ipc_dir: str | None = None
    rpc_timeout_s: float = 8.0
    sys_rpc_address: str | None = None  # filled at boot, read-only at runtime


@dataclass
class WebCfg:
    host: str = "127.0.0.1"
    port: int = 17800
    open_browser: bool = False
    sse_heartbeat_s: float = 15.0


@dataclass
class SecurityCfg:
    auto_allow_low: bool = True
    medium_default: str = "ask"    # ask | deny
    high_default: str = "ask"      # ask | deny
    approval_ttl_s: int = 300
    grant_ttl_s: int = 3600
    shell_timeout_s: int = 60
    allow_shell: bool = True
    max_preview_chars: int = 4000


@dataclass
class LLMCfg:
    provider: str = "mock"          # mock | openai
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 1200
    timeout_s: float = 60.0


@dataclass
class AgentsCfg:
    heartbeat_s: float = 2.0
    offline_after_s: float = 8.0
    max_workers: int = 6
    max_steps: int = 16
    task_poll_s: float = 1.5
    auto_spawn_project_lead: bool = True
    aggregator_interval_s: float = 240.0
    restart_limit: int = 3
    restart_window_s: float = 300.0


DEFAULTS = {
    "system": dataclasses.asdict(SystemCfg()),
    "transport": dataclasses.asdict(TransportCfg()),
    "web": dataclasses.asdict(WebCfg()),
    "security": dataclasses.asdict(SecurityCfg()),
    "llm": dataclasses.asdict(LLMCfg()),
    "agents": dataclasses.asdict(AgentsCfg()),
}

_SECTIONS = {
    "system": SystemCfg,
    "transport": TransportCfg,
    "web": WebCfg,
    "security": SecurityCfg,
    "llm": LLMCfg,
    "agents": AgentsCfg,
}


@dataclass
class Paths:
    root: Path
    state_dir: Path
    run_dir: Path
    events_dir: Path
    logs_dir: Path
    projects_dir: Path
    tmp_dir: Path

    @property
    def config_file(self) -> Path:
        return self.state_dir / "config.json"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "stackflowos.log"

    def ensure(self) -> "Paths":
        for path in (self.state_dir, self.run_dir, self.events_dir,
                     self.logs_dir, self.projects_dir, self.tmp_dir):
            ensure_dir(path)
        return self

    def project_workspace(self, project_id: str) -> Path:
        return ensure_dir(self.projects_dir / project_id / "workspace")

    def to_dict(self) -> dict:
        return {name: str(getattr(self, name)) for name in
                ("root", "state_dir", "run_dir", "events_dir", "logs_dir",
                 "projects_dir", "tmp_dir")}


def resolve_paths(root: Path | None = None, state_dir: Path | None = None) -> Paths:
    root = Path(root or default_root()).resolve()
    env_state = os.environ.get(ENV_STATE)
    state = Path(state_dir or env_state or root / ".stackflowos").expanduser().resolve()
    return Paths(
        root=root,
        state_dir=state,
        run_dir=state / "run",
        events_dir=state / "events",
        logs_dir=state / "logs",
        projects_dir=state / "projects",
        tmp_dir=state / "tmp",
    )


def _coerce(section: str, key: str, value: Any, expected: Any) -> Any:
    origin = get_origin(expected)
    if origin is not None:
        # Optional[T] / T | None
        args = [a for a in get_args(expected) if a is not type(None)]
        if value is None:
            return None
        if args:
            return _coerce(section, key, value, args[0])
        return value
    if expected is bool:
        if isinstance(value, bool):
            return value
        raise ConfigError(f"{section}.{key} must be a boolean")
    if expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{section}.{key} must be an integer")
        return value
    if expected is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{section}.{key} must be a number")
        return float(value)
    if expected is str:
        if not isinstance(value, str):
            raise ConfigError(f"{section}.{key} must be a string")
        return value
    return value


def _build_section(name: str, raw: dict) -> Any:
    cls = _SECTIONS[name]
    hints = get_type_hints(cls)
    known = set(hints)
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown config keys in {name}: {sorted(unknown)}")
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        clean[key] = _coerce(name, key, value, hints[key])
    section = cls(**clean)
    _validate_section(name, section)
    return section


def _validate_section(name: str, section: Any) -> None:
    if name == "transport":
        if section.mode not in ("ipc", "tcp"):
            raise ConfigError("transport.mode must be 'ipc' or 'tcp'")
        if not (1024 <= section.port_pool_start < section.port_pool_end <= 65535):
            raise ConfigError("invalid transport port pool")
        if section.rpc_timeout_s <= 0:
            raise ConfigError("transport.rpc_timeout_s must be positive")
    elif name == "web":
        if not (1 <= section.port <= 65535):
            raise ConfigError("web.port out of range")
    elif name == "security":
        if section.medium_default not in ("ask", "deny"):
            raise ConfigError("security.medium_default must be ask|deny")
        if section.high_default not in ("ask", "deny"):
            raise ConfigError("security.high_default must be ask|deny")
        if not (10 <= section.approval_ttl_s <= 86400):
            raise ConfigError("security.approval_ttl_s out of range")
        if not (10 <= section.grant_ttl_s <= 30 * 86400):
            raise ConfigError("security.grant_ttl_s out of range")
    elif name == "llm":
        if section.provider not in ("mock", "openai"):
            raise ConfigError("llm.provider must be mock|openai")
        if section.provider == "openai" and not section.base_url:
            raise ConfigError("llm.base_url is required for the openai provider")
    elif name == "agents":
        if section.heartbeat_s <= 0 or section.offline_after_s <= section.heartbeat_s:
            raise ConfigError("agents.offline_after_s must exceed heartbeat_s")
        if not (1 <= section.max_workers <= 32):
            raise ConfigError("agents.max_workers out of range")


@dataclass
class Config:
    system: SystemCfg
    transport: TransportCfg
    web: WebCfg
    security: SecurityCfg
    llm: LLMCfg
    agents: AgentsCfg
    paths: Paths

    # -- load / save ------------------------------------------------------
    @classmethod
    def load(cls, root: Path | None = None, state_dir: Path | None = None) -> "Config":
        paths = resolve_paths(root, state_dir).ensure()
        raw = {k: dict(v) for k, v in DEFAULTS.items()}
        overrides = read_json(paths.config_file, default={})
        if overrides:
            if not isinstance(overrides, dict):
                raise ConfigError("config.json must contain an object")
            raw = deep_merge(raw, overrides)
        env_patch = _env_patch()
        raw = deep_merge(raw, env_patch)
        sections = {name: _build_section(name, raw.get(name, {})) for name in _SECTIONS}
        return cls(paths=paths, **sections)

    def to_dict(self) -> dict:
        return {name: dataclasses.asdict(getattr(self, name)) for name in _SECTIONS}

    def public_dict(self) -> dict:
        data = self.to_dict()
        key = data["llm"].get("api_key") or ""
        data["llm"]["api_key"] = "***" if key else ""
        data["paths"] = self.paths.to_dict()
        return data

    def update(self, patch: dict) -> "Config":
        """Deep-merge a validated patch, persist it, return a new Config."""
        if not isinstance(patch, dict) or not patch:
            raise ConfigError("config patch must be a non-empty object")
        for name in patch:
            if name not in _SECTIONS:
                raise ConfigError(f"unknown config section: {name}")
        # Validate by building a candidate from current + patch without
        # touching disk first, so a rejected patch persists nothing.
        merged_raw = deep_merge(self.to_dict(), patch)
        sections = {name: _build_section(name, merged_raw[name]) for name in _SECTIONS}
        current_overrides = read_json(self.paths.config_file, default={})
        if not isinstance(current_overrides, dict):
            current_overrides = {}
        write_json(self.paths.config_file, deep_merge(current_overrides, patch))
        return Config(paths=self.paths, **sections)

    def save(self) -> None:
        write_json(self.paths.config_file, self.to_dict())


def _env_patch() -> dict:
    patch: dict[str, Any] = {}
    if os.environ.get(ENV_TRANSPORT):
        patch.setdefault("transport", {})["mode"] = os.environ[ENV_TRANSPORT]
    if os.environ.get(ENV_WEB_PORT):
        try:
            patch.setdefault("web", {})["port"] = int(os.environ[ENV_WEB_PORT])
        except ValueError as exc:
            raise ConfigError(f"{ENV_WEB_PORT} must be an integer") from exc
    llm = {}
    if os.environ.get(ENV_LLM_PROVIDER):
        llm["provider"] = os.environ[ENV_LLM_PROVIDER]
    if os.environ.get(ENV_LLM_BASE_URL):
        llm["base_url"] = os.environ[ENV_LLM_BASE_URL]
    if os.environ.get(ENV_LLM_MODEL):
        llm["model"] = os.environ[ENV_LLM_MODEL]
    if os.environ.get(ENV_LLM_API_KEY):
        llm["api_key"] = os.environ[ENV_LLM_API_KEY]
    if llm:
        patch["llm"] = llm
    return patch

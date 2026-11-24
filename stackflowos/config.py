"""Configuration and filesystem layout.

Precedence: built-in defaults < ``<state>/config.json`` < environment.
The state directory defaults to ``<repo>/.stackflowos`` and holds every byte
this system writes: config, event log, project workspaces, logs. Nothing is
written outside the repository unless the operator explicitly points
``SFO_STATE_DIR`` elsewhere.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import re
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
    #: L8: allow ``transport.host`` to be wildcard or non-loopback.  Default
    #: False keeps the RPC listener on the local machine only; authentication
    #: (protocol v2) is independent and does not make a public bind safe.
    allow_remote: bool = False


@dataclass
class WebCfg:
    host: str = "127.0.0.1"
    port: int = 17800
    open_browser: bool = False
    sse_heartbeat_s: float = 15.0
    #: Bound on concurrent SSE streams; protects fds/memory (H2).
    sse_max_clients: int = 32
    # Extra exact ``host[:port]`` authorities accepted in the Host header when
    # the console is bound to a non-loopback interface (H1 admission).
    trusted_hosts: list[str] = field(default_factory=list)
    # Lifetime of the HMAC-signed browser session cookie.
    cookie_ttl_days: int = 7
    #: L8: allow ``web.host`` to be wildcard or non-loopback.  Default False
    #: keeps the console on loopback; Host/Origin admission (H1) does not make
    #: a wildcard listener private.
    allow_remote: bool = False
    #: TLS listener certificate/key pair (PEM paths).  When both are set the
    #: console serves HTTPS directly instead of relying on a reverse proxy.
    tls_cert: str = ""
    tls_key: str = ""
    #: Minimum negotiated TLS version for the built-in listener.
    tls_min_version: str = "TLSv1.2"
    #: A launch token is single-use by default: exchanging it for a cookie
    #: invalidates it, so a URL captured from a terminal cannot be replayed.
    #: ``sfo url`` mints a fresh one through the operator control socket.
    launch_token_single_use: bool = True
    #: Bound on server-side browser sessions (logout/rotation/listing).
    session_max: int = 32
    #: Multi-user accounts.  Each entry is
    #: ``{"name": str, "token_sha256": sha256-hex, "role": "operator"|"viewer"}``.
    #: The per-process launch token always maps to the ``operator`` role; these
    #: accounts let a deployment hand out scoped, revocable bearer tokens.
    users: list[dict] = field(default_factory=list)


@dataclass
class SecurityCfg:
    auto_allow_low: bool = True
    medium_default: str = "ask"    # ask | deny
    high_default: str = "ask"      # ask | deny
    approval_ttl_s: int = 300
    grant_ttl_s: int = 3600
    shell_timeout_s: int = 60
    #: Hard upper bound for a single ``shell.exec`` (L5); a caller-supplied
    #: ``timeout_s`` is clamped to this so one command cannot pin a worker.
    shell_max_timeout_s: int = 600
    #: Cap for the streamed shell output log under ``.artifacts`` (L4).  Once
    #: reached the log is truncated with a marker instead of filling the disk.
    shell_log_max_bytes: int = 64 * 1024 * 1024
    allow_shell: bool = True
    #: ``net.fetch`` may reach loopback/RFC1918/link-local targets (SSRF, M4).
    #: Off by default; when on, private targets stay high risk.
    allow_private_network: bool = False
    #: Chars of an approval preview shown by default (UI/events, M7).
    max_preview_chars: int = 4000
    #: Bounded storage for the redacted full preview behind the details
    #: endpoint (M7).  ``max_preview_chars`` still controls what is shown.
    preview_store_chars: int = 65536
    #: Per-project agent memory cap (L6): a ``memory.set`` whose JSON encoding
    #: of that project's data exceeds this is rejected with a 400.
    memory_max_bytes: int = 256 * 1024
    #: Cap for the whole per-project memory store (L6), across every file.
    memory_max_total_bytes: int = 4 * 1024 * 1024
    #: Number of project memory files allowed (L6).
    memory_max_projects: int = 1000
    #: OS sandbox for ``shell.exec``: off | auto | bwrap | landlock.  ``auto``
    #: picks the strongest backend available on this host, falling back to a
    #: clearly-logged "unsandboxed" mode instead of pretending isolation.
    sandbox: str = "off"
    #: Let sandboxed commands reach the network.  Off by default: a sandboxed
    #: shell gets no network namespace by default when bwrap is available.
    sandbox_allow_network: bool = False
    #: tmpfs size (MiB) for the sandbox's writable /tmp.
    sandbox_tmp_mb: int = 256
    #: Attached read-write project workspaces: put each task on its own git
    #: branch and commit after the task, so agent edits can be reviewed and
    #: rolled back instead of silently landing on the operator's branch.
    workspace_git: bool = False
    workspace_git_commit: bool = True
    #: Redact audit ``result``/``error`` payloads (secrets, bearer tokens) and
    #: cap their size before they are persisted.
    audit_redact: bool = True
    audit_max_field_chars: int = 4000
    #: Pin ``net.fetch`` to the IP resolved at admission time (DNS rebinding
    #: TOCTOU); disabled only for tests that need the old resolver path.
    net_pin_dns: bool = True


@dataclass
class LLMCfg:
    provider: str = "mock"          # mock | openai | anthropic | gemini
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout_s: float = 60.0
    #: Emit incremental ``chat.delta`` events while a provider streams.
    stream: bool = True
    #: Per-provider request/token limits (0 = unlimited).  Enforced hub-side
    #: so one project cannot exhaust the shared key.
    rpm: int = 0
    tpm: int = 0
    max_concurrency: int = 8
    #: Context-window budget used for trimming before a request.
    max_context_tokens: int = 128000
    #: Per-project token budget (0 = unlimited); exceeded calls fail closed.
    project_token_budget: int = 0
    #: Extra providers tried in order when the primary is unavailable.  Each
    #: entry has the same shape as a ``routes`` entry without ``role``.
    fallback: list[dict] = field(default_factory=list)
    #: Role-specific routes: ``{"role": "coder", "provider": ..., "model":
    #: ..., "base_url": ..., "api_key": ..., "temperature": ..., ...}``.
    #: A role without an entry uses the primary provider above.
    routes: list[dict] = field(default_factory=list)
    retries: int = 2
    retry_backoff_s: float = 0.5


@dataclass
class AgentsCfg:
    heartbeat_s: float = 2.0
    offline_after_s: float = 8.0
    max_workers: int = 6
    max_steps: int = 32
    task_poll_s: float = 1.5
    auto_spawn_project_lead: bool = True
    aggregator_interval_s: float = 240.0
    restart_limit: int = 3
    restart_window_s: float = 300.0
    #: Wall-clock cap for one agent task; the worker reports ``blocked`` and
    #: the hub reclaims/reassigns instead of letting a stuck loop run forever.
    task_timeout_s: float = 1800.0
    #: Hard cap on tool calls inside one task (runaway-loop protection).
    max_tool_calls_per_task: int = 200
    #: Spawn requests beyond ``max_workers`` wait in a priority queue instead
    #: of failing; this bounds that queue.
    queue_max: int = 64
    #: How often a running worker reconciles its live config (models, limits)
    #: with the hub.  0 disables the poll.
    config_reload_s: float = 10.0


@dataclass
class AutonomyCfg:
    """The self-driving loop: patrol, dispatch, bounded self-approval.

    Everything is off by default.  Enabling autonomy is an explicit operator
    decision with three independent switches (``enabled``, ``dispatch_mode``,
    ``approve_mode``) and hard envelopes: quotas, capability allowlists,
    project/role filters, a risk ceiling and a shell allowlist.  The guardian
    never decides permanent/destructive capabilities (see
    ``autonomy.models.HARD_DENY_CAPABILITIES``).
    """

    enabled: bool = False
    #: Wall-clock seconds between patrols.
    interval_s: float = 120.0
    #: Run one patrol shortly after the hub is ready (instead of waiting a
    #: full interval).
    on_start: bool = True
    #: Recent reports kept in ``state/autonomy.json`` (the latest is always
    #: kept separately).
    report_keep: int = 20
    #: How long a ``todo`` task may sit with no live agent before dispatch.
    task_idle_s: float = 120.0
    #: How long a pending approval may wait before patrol calls it out.
    approval_age_s: float = 120.0
    #: Window used to aggregate agent failures.
    failure_window_s: float = 3600.0
    #: Same (role, error_code) failures at/above this become ``critical``.
    failure_threshold: int = 3
    #: off = observe only; tasks = create remediation tasks; agents = also
    #: spawn workers for them.
    dispatch_mode: str = "off"
    #: Cap on concurrently active agents the governor itself dispatched.
    dispatch_max_agents: int = 2
    dispatch_max_tasks_per_hour: int = 10
    dispatch_max_spawns_per_hour: int = 4
    #: Per-finding cooldown after an attempt (successful or failed).
    dispatch_cooldown_s: float = 900.0
    #: off = never decide; observe = record recommendations only; enforce =
    #: actually approve within the envelope.
    approve_mode: str = "off"
    #: Highest risk the guardian may approve.  ``high`` also requires explicit
    #: ``approve_shell``/``approve_domains`` opt-ins for those capabilities.
    approve_risk_max: str = "medium"
    #: Capabilities the guardian may approve (single requests, mode="once").
    approve_capabilities: list[str] = field(
        default_factory=lambda: ["fs.write", "agent.spawn"])
    #: Project allowlist; empty means every active/review/degraded project.
    approve_projects: list[str] = field(default_factory=list)
    #: Roles the guardian may spawn on a lead's behalf (``lead`` is excluded
    #: by default: the governor spawns leads from its own dispatcher, not from
    #: a worker request).
    approve_roles: list[str] = field(
        default_factory=lambda: ["architect", "coder", "tester",
                                 "reviewer", "researcher"])
    approve_max_per_hour: int = 20
    #: Let a human see a request first; 0 means decide immediately.
    approve_min_age_s: float = 0.0
    #: Respect recent human denials: skip matching requests for this long.
    approve_deny_cooldown_s: float = 86400.0
    #: Per-project cap on live agents when auto-approving ``agent.spawn``.
    approve_max_agents_per_project: int = 4
    #: Auto-approve ``shell.exec`` only when it matches one of the operator's
    #: regexes and runs inside the project workspace.  Requires
    #: ``approve_risk_max="high"`` and a non-empty pattern list.
    approve_shell: bool = False
    approve_shell_patterns: list[str] = field(default_factory=list)
    #: Domains the guardian may auto-approve for ``net.fetch`` (exact host or
    #: dot-suffix, e.g. ``example.com``).
    approve_domains: list[str] = field(default_factory=list)


@dataclass
class SkillsCfg:
    #: Skill roots (relative paths resolve against the state dir).  Every
    #: sub-directory with a ``SKILL.md`` or ``skill.json`` is one skill.
    dirs: list[str] = field(default_factory=lambda: ["skills"])
    #: Globally enabled skill ids; project-level enable lists are stored in
    #: the registry state file.
    enabled: list[str] = field(default_factory=list)
    inject_max_chars: int = 8000
    max_skills: int = 200


@dataclass
class PluginsCfg:
    #: Plugin roots; every sub-directory with ``stackflow.plugin.json`` is one.
    dirs: list[str] = field(default_factory=lambda: ["plugins"])
    enabled: list[str] = field(default_factory=list)
    max_plugins: int = 32
    #: Reserved: untrusted plugins will run in a subprocess in a later phase;
    #: today only signed/in-repo manifests are loaded.
    allow_untrusted: bool = False


@dataclass
class MCPCfg:
    #: MCP stdio servers: ``{"name": str, "command": str, "args": [...],
    #: "env": {...}, "cwd": str, "enabled": bool}``.
    servers: list[dict] = field(default_factory=list)
    call_timeout_s: float = 30.0
    max_servers: int = 16
    max_tools: int = 256


@dataclass
class ObservabilityCfg:
    #: Prometheus exposition at ``/api/metrics`` (authenticated).
    metrics_enabled: bool = True
    #: Outbound notification sinks: ``{"type": "webhook", "url": ...,
    #: "events": ["approval.created", ...], "headers": {...}}``.
    webhooks: list[dict] = field(default_factory=list)
    #: Event types forwarded when a sink does not name its own list.
    notify_events: list[str] = field(default_factory=lambda: [
        "approval.created", "approval.expired", "unit.failed",
        "sys.booted", "sys.shutdown",
    ])
    notify_timeout_s: float = 5.0
    #: Rotation for the hub log file (the agent logs rotate already).
    log_rotate_bytes: int = 32 * 1024 * 1024
    log_rotate_keep: int = 5
    #: Number of backups kept by ``sfo backup``.
    backup_keep: int = 7


@dataclass
class StorageCfg:
    #: ``json`` keeps the current file stores; ``sqlite`` moves the event log,
    #: memory and counters to a single transactional database.
    backend: str = "json"
    sqlite_path: str = ""
    #: Event-log rotation/archival: roll the active JSONL file once it grows
    #: past this size and keep this many compressed archives.
    eventlog_rotate_bytes: int = 64 * 1024 * 1024
    eventlog_rotate_keep: int = 5


@dataclass
class ClusterCfg:
    #: Discover peers / let workers find the hub without a hand-written TCP
    #: address (mDNS over multicast DNS, stdlib only).
    discovery: str = "off"          # off | mdns
    service_type: str = "_stackflowos._tcp.local."
    advertise: bool = True
    #: Cold-standby hub: a second process waits on the hub lock and takes over
    #: when the active process exits (no quorum, single shared state dir).
    standby: bool = False
    standby_poll_s: float = 2.0
    #: Fencing tokens on leases: monotonically increasing per resource, so a
    #: holder that lost its lease cannot act on a stale token after a pause.
    lease_fencing: bool = True
    #: Tolerated clock skew before a lease is considered expired.
    lease_clock_skew_s: float = 5.0


DEFAULTS = {
    "system": dataclasses.asdict(SystemCfg()),
    "transport": dataclasses.asdict(TransportCfg()),
    "web": dataclasses.asdict(WebCfg()),
    "security": dataclasses.asdict(SecurityCfg()),
    "llm": dataclasses.asdict(LLMCfg()),
    "agents": dataclasses.asdict(AgentsCfg()),
    "autonomy": dataclasses.asdict(AutonomyCfg()),
    "skills": dataclasses.asdict(SkillsCfg()),
    "plugins": dataclasses.asdict(PluginsCfg()),
    "mcp": dataclasses.asdict(MCPCfg()),
    "observability": dataclasses.asdict(ObservabilityCfg()),
    "storage": dataclasses.asdict(StorageCfg()),
    "cluster": dataclasses.asdict(ClusterCfg()),
}

_SECTIONS = {
    "system": SystemCfg,
    "transport": TransportCfg,
    "web": WebCfg,
    "security": SecurityCfg,
    "llm": LLMCfg,
    "agents": AgentsCfg,
    "autonomy": AutonomyCfg,
    "skills": SkillsCfg,
    "plugins": PluginsCfg,
    "mcp": MCPCfg,
    "observability": ObservabilityCfg,
    "storage": StorageCfg,
    "cluster": ClusterCfg,
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
    if origin is list:
        if not isinstance(value, list):
            raise ConfigError(f"{section}.{key} must be a list")
        args = get_args(expected)
        item_type = args[0] if args else Any
        return [_coerce(section, key, item, item_type) for item in value]
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


def normalize_bind_host(host: str) -> str:
    """Normalise a bind host for policy checks (L8).

    Strips surrounding whitespace and one pair of IPv6 brackets so
    ``" [::1] "`` and ``"::1"`` are judged identically.  The original
    spelling (for DNS names) is otherwise preserved.
    """
    value = str(host or "").strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1].strip()
    return value


_LOOPBACK_NAMES = frozenset({"localhost"})
#: Explicit wildcard spellings; IP literals are also checked with
#: ``ipaddress`` so every textual form of the unspecified address is caught.
_WILDCARD_HOSTS = frozenset({"", "*", "0.0.0.0", "::", "0:0:0:0:0:0:0:0"})


def is_loopback_host(host: str) -> bool:
    """True for ``localhost``, ``127.0.0.0/8`` and ``::1`` (L8)."""
    value = normalize_bind_host(host).lower()
    if value in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def is_wildcard_host(host: str) -> bool:
    """True when ``host`` means "bind every interface" (L8)."""
    value = normalize_bind_host(host).lower()
    if value in _WILDCARD_HOSTS:
        return True
    try:
        return ipaddress.ip_address(value).is_unspecified
    except ValueError:
        return False


def _validate_bind_policy(name: str, section: Any) -> None:
    """Reject wildcard/non-loopback bind hosts unless the section opts in (L8).

    ``transport.allow_remote`` only governs ``transport.host`` and
    ``web.allow_remote`` only governs ``web.host``; the two are deliberately
    independent so allowing the RPC port to the LAN cannot silently expose the
    browser console (and vice versa).
    """
    host = section.host
    if not isinstance(host, str):
        raise ConfigError(f"{name}.host must be a string")
    normalized = normalize_bind_host(host)
    if normalized != host:
        section.host = normalized
    if not isinstance(section.allow_remote, bool):
        raise ConfigError(f"{name}.allow_remote must be a boolean")
    if is_loopback_host(normalized):
        return
    if section.allow_remote:
        return
    if is_wildcard_host(normalized):
        problem = "binds every network interface"
    else:
        problem = "is not a loopback address"
    hint = f"{name}.allow_remote=true"
    if name == "web":
        hint += " (or pass --allow-remote)"
    raise ConfigError(
        f"{name}.host {normalized!r} {problem}; remote binds are disabled by "
        f"default. Set {hint} to allow other machines to connect, and keep the "
        "listener behind an SSH tunnel or an authenticated TLS reverse proxy"
    )


def _validate_section(name: str, section: Any) -> None:
    if name == "transport":
        _validate_bind_policy(name, section)
        if section.mode not in ("ipc", "tcp"):
            raise ConfigError("transport.mode must be 'ipc' or 'tcp'")
        if not (1024 <= section.port_pool_start < section.port_pool_end <= 65535):
            raise ConfigError("invalid transport port pool")
        if section.rpc_timeout_s <= 0:
            raise ConfigError("transport.rpc_timeout_s must be positive")
    elif name == "web":
        _validate_bind_policy(name, section)
        if not (1 <= section.port <= 65535):
            raise ConfigError("web.port out of range")
        if not (1 <= section.cookie_ttl_days <= 365):
            raise ConfigError("web.cookie_ttl_days out of range (1..365)")
        if not (1 <= section.sse_heartbeat_s <= 300):
            raise ConfigError("web.sse_heartbeat_s out of range (1..300)")
        if not (1 <= section.sse_max_clients <= 256):
            raise ConfigError("web.sse_max_clients out of range (1..256)")
        if any(not item.strip() for item in section.trusted_hosts):
            raise ConfigError("web.trusted_hosts entries must be non-empty strings")
        # A TLS listener is available; a half-configured pair must still fail
        # loudly instead of silently serving plaintext with an unused path.
        if not isinstance(section.tls_cert, str) or not isinstance(section.tls_key, str):
            raise ConfigError("web.tls_cert and web.tls_key must be strings")
        cert = section.tls_cert.strip()
        key = section.tls_key.strip()
        if bool(cert) != bool(key):
            raise ConfigError(
                "web.tls_cert and web.tls_key must be set together")
        if section.tls_min_version not in ("TLSv1.2", "TLSv1.3"):
            raise ConfigError("web.tls_min_version must be TLSv1.2 or TLSv1.3")
        if not isinstance(section.launch_token_single_use, bool):
            raise ConfigError("web.launch_token_single_use must be a boolean")
        if not (1 <= section.session_max <= 1024):
            raise ConfigError("web.session_max out of range (1..1024)")
        if not isinstance(section.users, list):
            raise ConfigError("web.users must be a list")
        for index, user in enumerate(section.users):
            if not isinstance(user, dict):
                raise ConfigError(f"web.users[{index}] must be an object")
            name = user.get("name")
            digest = user.get("token_sha256")
            role = user.get("role", "viewer")
            if not isinstance(name, str) or not name.strip():
                raise ConfigError(f"web.users[{index}].name is required")
            if not isinstance(digest, str) or len(digest) != 64 or any(
                    ch not in "0123456789abcdef" for ch in digest.lower()):
                raise ConfigError(
                    f"web.users[{index}].token_sha256 must be a sha256 hex digest")
            if role not in ("operator", "viewer"):
                raise ConfigError(
                    f"web.users[{index}].role must be operator|viewer")
    elif name == "security":
        if section.medium_default not in ("ask", "deny"):
            raise ConfigError("security.medium_default must be ask|deny")
        if section.high_default not in ("ask", "deny"):
            raise ConfigError("security.high_default must be ask|deny")
        if not (10 <= section.approval_ttl_s <= 86400):
            raise ConfigError("security.approval_ttl_s out of range")
        if not (10 <= section.grant_ttl_s <= 30 * 86400):
            raise ConfigError("security.grant_ttl_s out of range")
        if not (1 <= section.shell_max_timeout_s <= 3600):
            raise ConfigError(
                "security.shell_max_timeout_s out of range (1..3600)")
        if not (1024 * 1024 <= section.shell_log_max_bytes <= 1024 ** 3):
            raise ConfigError(
                "security.shell_log_max_bytes out of range (1048576..1073741824)")
        if not isinstance(section.allow_private_network, bool):
            raise ConfigError("security.allow_private_network must be a boolean")
        if section.max_preview_chars < 100:
            raise ConfigError("security.max_preview_chars must be >= 100")
        if not (1024 <= section.preview_store_chars <= 262144):
            raise ConfigError(
                "security.preview_store_chars out of range (1024..262144)")
        if not (1024 <= section.memory_max_bytes <= 4 * 1024 * 1024):
            raise ConfigError(
                "security.memory_max_bytes out of range (1024..4194304)")
        if not (1024 <= section.memory_max_total_bytes <= 256 * 1024 * 1024):
            raise ConfigError(
                "security.memory_max_total_bytes out of range (1024..268435456)")
        if section.memory_max_total_bytes < section.memory_max_bytes:
            raise ConfigError(
                "security.memory_max_total_bytes must be >= security.memory_max_bytes")
        if not (1 <= section.memory_max_projects <= 100000):
            raise ConfigError(
                "security.memory_max_projects out of range (1..100000)")
        if section.sandbox not in ("off", "auto", "bwrap", "landlock"):
            raise ConfigError("security.sandbox must be off|auto|bwrap|landlock")
        for flag in ("sandbox_allow_network", "workspace_git",
                     "workspace_git_commit", "audit_redact", "net_pin_dns"):
            if not isinstance(getattr(section, flag), bool):
                raise ConfigError(f"security.{flag} must be a boolean")
        if not (16 <= section.sandbox_tmp_mb <= 65536):
            raise ConfigError("security.sandbox_tmp_mb out of range")
        if not (256 <= section.audit_max_field_chars <= 65536):
            raise ConfigError("security.audit_max_field_chars out of range")
    elif name == "llm":
        if section.provider not in ("mock", "openai", "anthropic", "gemini"):
            raise ConfigError(
                "llm.provider must be mock|openai|anthropic|gemini")
        if section.provider == "openai":
            if not section.base_url:
                raise ConfigError("llm.base_url is required for the openai provider")
            if not section.model:
                raise ConfigError("llm.model is required for the openai provider")
        if section.provider in ("anthropic", "gemini") and not section.model:
            raise ConfigError(
                f"llm.model is required for the {section.provider} provider")
        if section.max_tokens < 1:
            raise ConfigError("llm.max_tokens must be positive")
        if section.rpm < 0 or section.tpm < 0:
            raise ConfigError("llm.rpm/llm.tpm must be >= 0")
        if section.max_concurrency < 1:
            raise ConfigError("llm.max_concurrency must be >= 1")
        if section.max_context_tokens < 1024:
            raise ConfigError("llm.max_context_tokens must be >= 1024")
        if section.project_token_budget < 0:
            raise ConfigError("llm.project_token_budget must be >= 0")
        if section.retries < 0 or section.retry_backoff_s < 0:
            raise ConfigError("llm.retries/retry_backoff_s must be >= 0")
        if not isinstance(section.routes, list) or not isinstance(section.fallback, list):
            raise ConfigError("llm.routes/llm.fallback must be lists")
        for field_name, entries in (("routes", section.routes),
                                    ("fallback", section.fallback)):
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    raise ConfigError(f"llm.{field_name}[{index}] must be an object")
                if entry.get("provider") and entry["provider"] not in (
                        "mock", "openai", "anthropic", "gemini"):
                    raise ConfigError(
                        f"llm.{field_name}[{index}].provider is unsupported")
                if field_name == "routes" and not isinstance(
                        entry.get("role"), str):
                    raise ConfigError(
                        f"llm.routes[{index}].role is required")
    elif name == "agents":
        if section.heartbeat_s <= 0 or section.offline_after_s <= section.heartbeat_s:
            raise ConfigError("agents.offline_after_s must exceed heartbeat_s")
        if not (1 <= section.max_workers <= 32):
            raise ConfigError("agents.max_workers out of range")
        if section.task_timeout_s <= 0:
            raise ConfigError("agents.task_timeout_s must be positive")
        if not (1 <= section.max_tool_calls_per_task <= 100000):
            raise ConfigError("agents.max_tool_calls_per_task out of range")
        if not (1 <= section.queue_max <= 10000):
            raise ConfigError("agents.queue_max out of range")
        if section.config_reload_s < 0:
            raise ConfigError("agents.config_reload_s must be >= 0")
    elif name == "autonomy":
        _validate_autonomy(section)
    elif name == "skills":
        if not isinstance(section.dirs, list) or not all(
                isinstance(item, str) and item.strip() for item in section.dirs):
            raise ConfigError("skills.dirs must be a list of non-empty strings")
        if not isinstance(section.enabled, list) or not all(
                isinstance(item, str) for item in section.enabled):
            raise ConfigError("skills.enabled must be a list of strings")
        if not (1000 <= section.inject_max_chars <= 1_000_000):
            raise ConfigError("skills.inject_max_chars out of range")
        if not (1 <= section.max_skills <= 10000):
            raise ConfigError("skills.max_skills out of range")
    elif name == "plugins":
        if not isinstance(section.dirs, list) or not all(
                isinstance(item, str) and item.strip() for item in section.dirs):
            raise ConfigError("plugins.dirs must be a list of non-empty strings")
        if not isinstance(section.enabled, list):
            raise ConfigError("plugins.enabled must be a list")
        if not (1 <= section.max_plugins <= 1024):
            raise ConfigError("plugins.max_plugins out of range")
        if not isinstance(section.allow_untrusted, bool):
            raise ConfigError("plugins.allow_untrusted must be a boolean")
    elif name == "mcp":
        if not isinstance(section.servers, list):
            raise ConfigError("mcp.servers must be a list")
        for index, server in enumerate(section.servers):
            if not isinstance(server, dict):
                raise ConfigError(f"mcp.servers[{index}] must be an object")
            if not isinstance(server.get("name"), str) or not server["name"].strip():
                raise ConfigError(f"mcp.servers[{index}].name is required")
            if not isinstance(server.get("command"), str) or not server["command"].strip():
                raise ConfigError(f"mcp.servers[{index}].command is required")
            args = server.get("args", [])
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise ConfigError(f"mcp.servers[{index}].args must be strings")
            if server.get("env") is not None and not isinstance(server["env"], dict):
                raise ConfigError(f"mcp.servers[{index}].env must be an object")
        if section.call_timeout_s <= 0:
            raise ConfigError("mcp.call_timeout_s must be positive")
        if not (1 <= section.max_servers <= 256):
            raise ConfigError("mcp.max_servers out of range")
        if not (1 <= section.max_tools <= 10000):
            raise ConfigError("mcp.max_tools out of range")
    elif name == "observability":
        if not isinstance(section.metrics_enabled, bool):
            raise ConfigError("observability.metrics_enabled must be a boolean")
        if not isinstance(section.webhooks, list):
            raise ConfigError("observability.webhooks must be a list")
        for index, sink in enumerate(section.webhooks):
            if not isinstance(sink, dict):
                raise ConfigError(
                    f"observability.webhooks[{index}] must be an object")
            if sink.get("type", "webhook") != "webhook":
                raise ConfigError(
                    f"observability.webhooks[{index}].type must be webhook")
            if not isinstance(sink.get("url"), str) or not sink["url"]:
                raise ConfigError(
                    f"observability.webhooks[{index}].url is required")
        if section.notify_timeout_s <= 0:
            raise ConfigError("observability.notify_timeout_s must be positive")
        if not (0 <= section.log_rotate_bytes <= 1024 ** 3):
            raise ConfigError("observability.log_rotate_bytes out of range")
        if not (0 <= section.log_rotate_keep <= 100):
            raise ConfigError("observability.log_rotate_keep out of range")
        if not (0 <= section.backup_keep <= 100):
            raise ConfigError("observability.backup_keep out of range")
    elif name == "storage":
        if section.backend not in ("json", "sqlite"):
            raise ConfigError("storage.backend must be json|sqlite")
        if section.sqlite_path and not isinstance(section.sqlite_path, str):
            raise ConfigError("storage.sqlite_path must be a string")
        if not (1_000_000 <= section.eventlog_rotate_bytes <= 16 * 1024 ** 3):
            raise ConfigError("storage.eventlog_rotate_bytes out of range")
        if not (0 <= section.eventlog_rotate_keep <= 1000):
            raise ConfigError("storage.eventlog_rotate_keep out of range")
    elif name == "cluster":
        if section.discovery not in ("off", "mdns"):
            raise ConfigError("cluster.discovery must be off|mdns")
        if not isinstance(section.service_type, str) or not section.service_type:
            raise ConfigError("cluster.service_type is required")
        for flag in ("advertise", "standby", "lease_fencing"):
            if not isinstance(getattr(section, flag), bool):
                raise ConfigError(f"cluster.{flag} must be a boolean")
        if section.standby_poll_s <= 0:
            raise ConfigError("cluster.standby_poll_s must be positive")
        if section.lease_clock_skew_s < 0:
            raise ConfigError("cluster.lease_clock_skew_s must be >= 0")


def _validate_autonomy(section: AutonomyCfg) -> None:
    """Validate the autonomy envelope before anything is persisted.

    Imports are local so ``config`` stays a leaf module at import time and the
    autonomy package can import config without a cycle.
    """
    from .autonomy.models import (
        APPROVE_MODES,
        DISPATCH_MODES,
        HARD_DENY_CAPABILITIES,
    )
    from .security.capabilities import (
        CAPABILITIES,
        RISK_ORDER,
        validate_project_id,
    )

    for flag in ("enabled", "on_start", "approve_shell"):
        if not isinstance(getattr(section, flag), bool):
            raise ConfigError(f"autonomy.{flag} must be a boolean")
    if not (10.0 <= float(section.interval_s) <= 86400.0):
        raise ConfigError("autonomy.interval_s out of range (10..86400)")
    if not (1 <= int(section.report_keep) <= 200):
        raise ConfigError("autonomy.report_keep out of range (1..200)")
    if not (10.0 <= float(section.task_idle_s) <= 86400.0):
        raise ConfigError("autonomy.task_idle_s out of range (10..86400)")
    if not (10.0 <= float(section.approval_age_s) <= 86400.0):
        raise ConfigError("autonomy.approval_age_s out of range (10..86400)")
    if not (60.0 <= float(section.failure_window_s) <= 7 * 86400.0):
        raise ConfigError("autonomy.failure_window_s out of range (60..604800)")
    if not (1 <= int(section.failure_threshold) <= 100):
        raise ConfigError("autonomy.failure_threshold out of range (1..100)")
    if section.dispatch_mode not in DISPATCH_MODES:
        raise ConfigError(
            f"autonomy.dispatch_mode must be one of {list(DISPATCH_MODES)}")
    for field_name, maximum in (("dispatch_max_agents", 32),
                                ("dispatch_max_tasks_per_hour", 10000),
                                ("dispatch_max_spawns_per_hour", 10000)):
        value = getattr(section, field_name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"autonomy.{field_name} must be an integer")
        if not (0 <= value <= maximum):
            raise ConfigError(
                f"autonomy.{field_name} out of range (0..{maximum})")
    for field_name in ("dispatch_cooldown_s", "approve_min_age_s",
                       "approve_deny_cooldown_s"):
        value = getattr(section, field_name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"autonomy.{field_name} must be a number")
        if value < 0:
            raise ConfigError(f"autonomy.{field_name} must be >= 0")
    if section.approve_mode not in APPROVE_MODES:
        raise ConfigError(
            f"autonomy.approve_mode must be one of {list(APPROVE_MODES)}")
    if section.approve_risk_max not in RISK_ORDER:
        raise ConfigError(
            f"autonomy.approve_risk_max must be one of {sorted(RISK_ORDER)}")
    if not isinstance(section.approve_capabilities, list) or not all(
            isinstance(item, str) and item for item in section.approve_capabilities):
        raise ConfigError(
            "autonomy.approve_capabilities must be a list of capability names")
    for capability in section.approve_capabilities:
        if capability not in CAPABILITIES:
            raise ConfigError(
                f"autonomy.approve_capabilities: unknown capability "
                f"{capability!r}")
        if capability in HARD_DENY_CAPABILITIES:
            raise ConfigError(
                f"autonomy.approve_capabilities may not include {capability!r}: "
                "destructive/system capabilities always need a human")
    if not isinstance(section.approve_projects, list):
        raise ConfigError("autonomy.approve_projects must be a list")
    for project_id in section.approve_projects:
        if not isinstance(project_id, str):
            raise ConfigError(
                "autonomy.approve_projects entries must be project ids")
        try:
            validate_project_id(project_id)
        except Exception as exc:
            raise ConfigError(
                f"autonomy.approve_projects: invalid project id "
                f"{project_id!r}") from exc
    if not isinstance(section.approve_roles, list) or not all(
            isinstance(item, str) and item for item in section.approve_roles):
        raise ConfigError(
            "autonomy.approve_roles must be a list of role names")
    if not (0 <= int(section.approve_max_per_hour) <= 10000):
        raise ConfigError("autonomy.approve_max_per_hour out of range")
    if not (0 <= int(section.approve_max_agents_per_project) <= 32):
        raise ConfigError(
            "autonomy.approve_max_agents_per_project out of range (0..32)")
    patterns = section.approve_shell_patterns
    if not isinstance(patterns, list) or not all(
            isinstance(item, str) for item in patterns):
        raise ConfigError(
            "autonomy.approve_shell_patterns must be a list of regex strings")
    for pattern in patterns:
        if not pattern:
            raise ConfigError(
                "autonomy.approve_shell_patterns entries must not be empty")
        try:
            re.compile(pattern)
        except (re.error, OverflowError, RecursionError) as exc:
            raise ConfigError(
                f"invalid autonomy.approve_shell_patterns regex {pattern!r}: "
                f"{exc}") from None
    domains = section.approve_domains
    if not isinstance(domains, list) or not all(
            isinstance(item, str) and item.strip() for item in domains):
        raise ConfigError(
            "autonomy.approve_domains must be a list of host names")
    capabilities = set(section.approve_capabilities)
    if section.approve_shell:
        if "shell.exec" not in capabilities:
            raise ConfigError(
                "autonomy.approve_shell requires shell.exec in "
                "autonomy.approve_capabilities")
        if not patterns:
            raise ConfigError(
                "autonomy.approve_shell requires approve_shell_patterns")
        if section.approve_risk_max != "high":
            raise ConfigError(
                "autonomy.approve_shell requires approve_risk_max='high'")
    if "shell.exec" in capabilities and not section.approve_shell:
        raise ConfigError(
            "shell.exec in approve_capabilities requires approve_shell=true")
    if "net.fetch" in capabilities and not domains:
        raise ConfigError(
            "net.fetch in approve_capabilities requires approve_domains")
    if section.approve_shell and "shell.exec" not in capabilities:
        raise ConfigError(
            "approve_shell=true requires shell.exec in approve_capabilities")


_REDACTED = "***"
_SECRET_KEY_PARTS = ("api_key", "apikey", "token", "secret", "password",
                     "authorization", "bearer", "credential")


def _looks_secret_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(part in lowered for part in _SECRET_KEY_PARTS)


def redact_secrets(value: Any, *, _key: str = "") -> Any:
    """Return a copy with secret-looking string fields replaced by ``***``.

    Used for ``/api/config`` and for event payloads: a config patch that carries
    a new API key must never be written to the append-only audit stream.
    """
    if isinstance(value, dict):
        return {key: ("***" if (_looks_secret_key(key) and isinstance(item, str)
                                and item) else redact_secrets(item, _key=str(key)))
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item, _key=_key) for item in value]
    return value


def _restore_redacted(current: Any, patch: Any) -> Any:
    """Replace ``***`` placeholders in a patch with the current real values.

    The UI echoes back masked secrets (API keys, token hashes, webhook
    headers).  Persisting the literal ``***`` would destroy the secret, so a
    placeholder only survives here when the current config has a value for the
    same path.  Lists are matched by ``name`` when both sides are dicts with a
    unique name, otherwise by index.
    """
    if patch == _REDACTED and current is not None and not isinstance(
            current, (dict, list)):
        return current
    if isinstance(patch, dict) and isinstance(current, dict):
        return {key: _restore_redacted(current.get(key), item)
                for key, item in patch.items()}
    if isinstance(patch, list) and isinstance(current, list):
        current_by_name = {
            str(item["name"]): item for item in current
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        result = []
        for index, item in enumerate(patch):
            if (isinstance(item, dict) and isinstance(item.get("name"), str)
                    and item["name"] in current_by_name):
                result.append(_restore_redacted(current_by_name[item["name"]], item))
            elif index < len(current):
                result.append(_restore_redacted(current[index], item))
            else:
                result.append(item)
        return result
    return patch


def _redact_public(data: dict, paths: Paths) -> dict:
    clean = redact_secrets(data)
    clean["paths"] = paths.to_dict()
    return clean


@dataclass
class Config:
    system: SystemCfg
    transport: TransportCfg
    web: WebCfg
    security: SecurityCfg
    llm: LLMCfg
    agents: AgentsCfg
    autonomy: AutonomyCfg
    skills: SkillsCfg
    plugins: PluginsCfg
    mcp: MCPCfg
    observability: ObservabilityCfg
    storage: StorageCfg
    cluster: ClusterCfg
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
        return _redact_public(data, self.paths)

    def update(self, patch: dict) -> "Config":
        """Deep-merge a validated patch, persist it, return a new Config."""
        return self.apply(patch, persist=True)

    def with_overrides(self, patch: dict) -> "Config":
        """Apply CLI/runtime overrides without writing config.json.

        Flags like ``--port`` or ``--transport`` are meant for one process;
        persisting them used to leak a demo's TCP mode into the next hub boot.
        """
        return self.apply(patch, persist=False)

    def apply(self, patch: dict, *, persist: bool = True) -> "Config":
        """Validate a patch against the current config; optionally persist it."""
        if not isinstance(patch, dict) or not patch:
            raise ConfigError("config patch must be a non-empty object")
        for name in patch:
            if name not in _SECTIONS:
                raise ConfigError(f"unknown config section: {name}")
        # Masked secrets echoed by the UI are placeholders, not new values.
        patch = _restore_redacted(self.to_dict(), patch)
        # Validate by building a candidate from current + patch without
        # touching disk first, so a rejected patch persists nothing.
        merged_raw = deep_merge(self.to_dict(), patch)
        sections = {name: _build_section(name, merged_raw[name]) for name in _SECTIONS}
        if persist:
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

"""Policy engine: deny-first rules over capabilities and scopes (§6.2).

Rules are small, inspectable objects that say *who may do what, within which
scope, up to which risk, until when, how many times*. Evaluation order is
deliberately conservative:

1. any matching ``deny`` rule wins;
2. otherwise the first matching ``allow`` rule whose ``risk_max`` covers the
   concrete risk allows;
3. otherwise defaults apply — low risk may auto-allow, medium/high follow
   ``security.medium_default`` / ``security.high_default``.

Rules persist atomically to ``rules_path`` and every mutation emits
``rule.created`` / ``rule.updated`` / ``rule.deleted``. ``consume`` spends a
one-shot grant (``uses_left``); standing rules (``uses_left is None``) are
never consumed.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import shlex
import threading
from dataclasses import asdict, dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..errors import BadRequest, NotFound, ValidationError
from ..kernel import Context
from ..l2.eventlog import EventLog, emit_both
from ..protocol import EventRecord, new_id, now_ms as _now_ms
from ..util.jsonio import read_json, write_json
from .capabilities import (
    CAPABILITIES,
    RISK_ORDER,
    is_within,
    get_capability,
    normalize_path,
    project_workspace,
    risk_of,
)


@dataclass
class Rule:
    """One allow/deny statement about a capability for a principal."""

    id: str
    effect: str                       # allow|deny
    capability: str                   # exact name or "*"
    principal: str = "*"              # fnmatch: "agent.*", "coder.3", "human:operator"
    scope: dict = field(default_factory=dict)
    risk_max: str = "high"
    expires_at: int | None = None
    uses_left: int | None = None
    note: str = ""
    source: str = "rule"              # rule|grant
    created_by: str = "operator"
    created_at: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in (d or {}).items() if k in known}
        rule = cls(**data)
        if not isinstance(rule.scope, dict):
            rule.scope = {}
        return rule

    def active(self, now_ms: int | None = None) -> bool:
        """True while the rule is unexpired and still has uses left."""
        now = now_ms if now_ms is not None else _now_ms()
        if self.expires_at is not None and now >= int(self.expires_at):
            return False
        if self.uses_left is not None and int(self.uses_left) <= 0:
            return False
        return True


@dataclass
class Decision:
    """Outcome of one policy evaluation."""

    outcome: str                      # allow|deny|ask
    risk: str
    reason: str
    rule: Rule | None = None


class PolicyEngine:
    """Rule store, scope matcher and evaluator."""

    def __init__(self, ctx: Context, eventlog: EventLog, *, rules_path: Path,
                 config: Any = None,
                 projects_root: Path | None = None) -> None:
        self.ctx = ctx
        self.eventlog = eventlog
        self.rules_path = Path(rules_path)
        self.log = getattr(ctx, "log", logging.getLogger("sfo.security.policy"))
        sec = _section(config, "security")
        self.auto_allow_low = bool(_value(sec, "auto_allow_low", True))
        self.medium_default = str(_value(sec, "medium_default", "ask"))
        self.high_default = str(_value(sec, "high_default", "ask"))
        if projects_root is None:
            paths = getattr(config, "paths", None)
            projects_dir = getattr(paths, "projects_dir", None)
            projects_root = projects_dir if projects_dir is not None else Path.cwd()
        self.projects_root = Path(projects_root)
        self._rules: dict[str, Rule] = {}
        self._lock = threading.RLock()
        self._warned: set[tuple] = set()
        self.load()

    # -- rule management --------------------------------------------------
    def add_rule(self, *, effect: str, capability: str, principal: str = "*",
                 scope: dict | None = None, risk_max: str = "high",
                 expires_at: int | None = None, uses_left: int | None = None,
                 note: str = "", created_by: str = "operator",
                 source: str = "rule", rule_id: str | None = None) -> Rule:
        """Create and persist a rule, emitting ``rule.created``."""
        self._validate(effect=effect, capability=capability, risk_max=risk_max,
                       uses_left=uses_left)
        rule = Rule(
            id=rule_id or new_id("grant" if source == "grant" else "rule"),
            effect=str(effect),
            capability=str(capability),
            principal=str(principal or "*"),
            scope=dict(scope or {}),
            risk_max=str(risk_max),
            expires_at=None if expires_at is None else int(expires_at),
            uses_left=None if uses_left is None else int(uses_left),
            note=str(note or ""),
            source=str(source),
            created_by=str(created_by or "operator"),
            created_at=_now_ms(),
        )
        with self._lock:
            if rule.id in self._rules:
                raise ValidationError(f"rule {rule.id!r} already exists")
            self._rules[rule.id] = rule
        self.save()
        self._warn_legacy_command_regex(rule)
        self._emit("rule.created", rule, reason=rule.note)
        return rule

    def update_rule(self, rule_id: str, patch: dict) -> Rule:
        """Apply a validated patch to an existing rule."""
        if not isinstance(patch, dict) or not patch:
            raise BadRequest("rule patch must be a non-empty object")
        allowed = {"effect", "capability", "principal", "scope", "risk_max",
                   "expires_at", "uses_left", "note", "created_by", "source"}
        unknown = set(patch) - allowed
        if unknown:
            raise BadRequest(f"unknown rule fields: {sorted(unknown)}",
                             detail={"fields": sorted(unknown)})
        with self._lock:
            rule = self._get_locked(rule_id)
            candidate = Rule.from_dict({**rule.to_dict(), **patch})
        self._validate(effect=candidate.effect, capability=candidate.capability,
                       risk_max=candidate.risk_max, uses_left=candidate.uses_left)
        candidate.scope = dict(candidate.scope or {})
        with self._lock:
            self._rules[rule_id] = candidate
        self.save()
        self._warn_legacy_command_regex(candidate)
        self._emit("rule.updated", candidate)
        return candidate

    def delete_rule(self, rule_id: str) -> bool:
        """Delete a rule; returns False when it did not exist."""
        with self._lock:
            rule = self._rules.pop(rule_id, None)
        if rule is None:
            return False
        self.save()
        self._emit("rule.deleted", rule)
        return True

    def get_rule(self, rule_id: str) -> Rule | None:
        """Return a rule by id, or ``None`` when it does not exist."""
        with self._lock:
            return self._rules.get(rule_id)

    def list_rules(self, *, active_only: bool = False) -> list[Rule]:
        """List rules oldest first; ``active_only`` hides expired/used-up ones."""
        now = _now_ms()
        with self._lock:
            rules = list(self._rules.values())
        if active_only:
            rules = [rule for rule in rules if rule.active(now)]
        return sorted(rules, key=lambda rule: (rule.created_at, rule.id))

    # -- scope matching ---------------------------------------------------
    def match_scope(self, scope: dict, pattern: dict) -> bool:
        """Match a concrete capability scope against a rule scope pattern."""
        scope = scope or {}
        pattern = pattern or {}
        for key, expected in pattern.items():
            if not self._match_key(key, expected, scope):
                return False
        return True

    def _match_key(self, key: str, expected: Any, scope: dict) -> bool:
        if key == "path_prefix":
            raw = scope.get("path") or scope.get("cwd")
            if raw is None:
                return False
            return is_within(normalize_path(raw, cwd=scope.get("cwd")),
                             normalize_path(str(expected)))
        if key == "path_glob":
            raw = scope.get("path") or scope.get("cwd")
            if raw is None:
                return False
            return _match_path_glob(raw, expected, cwd=scope.get("cwd"))
        if key == "command_regex":
            command = scope.get("command")
            if command is None:
                return False
            pattern = str(expected)
            try:
                matched = re.fullmatch(pattern, str(command)) is not None
            except re.error:
                self._warn_once(("invalid", pattern),
                                "invalid command_regex in rule: %r", expected)
                return False
            if not matched:
                return False
            ops = _shell_control_ops(str(command))
            if ops and not _pattern_allows_shell_controls(pattern):
                self._warn_once(
                    ("chain", pattern),
                    "command_regex %r matched a command with shell control "
                    "operators %s but the pattern does not explicitly allow "
                    "them; refusing the match", pattern, sorted(ops))
                return False
            return True
        if key == "domain":
            url = str(scope.get("url") or "")
            if not url:
                return False
            host = _host_of(url)
            wanted = str(expected).lower()
            return bool(host) and (host == wanted or host.endswith("." + wanted))
        if key == "url_prefix":
            url = scope.get("url")
            if not url:
                return False
            return _url_prefix_match(str(url), expected)
        if key == "equals":
            if not isinstance(expected, dict):
                return False
            return all(scope.get(k) == v for k, v in expected.items())
        # project_id / role / agent_id / work_id / model / ... and any extra
        # key: exact-match against the same key of the concrete scope.
        if key not in scope:
            return False
        return scope.get(key) == expected

    # -- evaluation -------------------------------------------------------
    def evaluate(self, principal: str, capability: str, scope: dict, *,
                 risk: str | None = None, now_ms: int | None = None) -> Decision:
        """Decide allow/deny/ask for one request (deny rules win)."""
        get_capability(capability)  # unknown capabilities are a bad request
        now = now_ms if now_ms is not None else _now_ms()
        if risk is None:
            risk = self.risk(capability, scope)
        matching = self._matching(principal, capability, scope, now)
        for rule in matching:
            if rule.effect == "deny":
                return Decision("deny", risk,
                                f"denied by rule {rule.id}: {rule.note or 'deny'}",
                                rule)
        for rule in matching:
            if rule.effect != "allow":
                continue
            if RISK_ORDER.get(risk, 2) <= _risk_index(rule.risk_max):
                return Decision("allow", risk,
                                f"allowed by rule {rule.id} "
                                f"({rule.source}) risk<={rule.risk_max}", rule)
        return self._default(risk)

    def risk(self, capability: str, scope: dict) -> str:
        """Dynamic risk for a capability under the configured project root.

        ``scope["project_id"]`` is validated before the workspace join (M9):
        a hostile id such as ``../../etc`` must raise :class:`ValidationError`
        instead of silently moving the boundary outside ``projects_root``.
        """
        workspace = None
        project_id = (scope or {}).get("project_id")
        if project_id is not None:
            workspace = project_workspace(self.projects_root, project_id)
        return risk_of(capability, scope, projects_root=self.projects_root,
                       workspace=workspace)

    def consume(self, rule_id: str) -> bool:
        """Spend one use of a grant; standing rules pass without changes."""
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule is None or not rule.active():
                return False
            if rule.uses_left is None:
                return True
            rule.uses_left = int(rule.uses_left) - 1
            if rule.uses_left <= 0:
                del self._rules[rule_id]
                deleted = True
            else:
                deleted = False
        self.save()
        if deleted:
            self._emit("rule.deleted", rule, reason="uses_exhausted")
        return True

    # -- persistence ------------------------------------------------------
    def save(self) -> None:
        """Atomically write the rule set to ``rules_path``."""
        with self._lock:
            payload = {
                "version": 1,
                "rules": [rule.to_dict() for rule in
                          sorted(self._rules.values(),
                                 key=lambda r: (r.created_at, r.id))],
            }
        write_json(self.rules_path, payload)

    def load(self) -> None:
        """Load rules from disk; a missing/corrupt file means an empty set."""
        try:
            raw = read_json(self.rules_path, default=None)
        except ValueError:
            self.log.warning("ignoring unreadable rules file %s", self.rules_path)
            return
        rows = raw.get("rules") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return
        rules: dict[str, Rule] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                rule = Rule.from_dict(row)
            except (TypeError, ValueError):
                continue
            rules[rule.id] = rule
            self._warn_legacy_command_regex(rule)
        with self._lock:
            self._rules = rules

    # -- internals --------------------------------------------------------
    def _matching(self, principal: str, capability: str, scope: dict,
                  now: int) -> list[Rule]:
        with self._lock:
            rules = list(self._rules.values())
        result = []
        for rule in sorted(rules, key=lambda r: (r.created_at, r.id)):
            if not rule.active(now):
                continue
            if rule.capability not in (capability, "*"):
                continue
            if not fnmatch.fnmatchcase(str(principal), rule.principal):
                continue
            if not self.match_scope(scope, rule.scope):
                continue
            result.append(rule)
        return result

    def _warn_legacy_command_regex(self, rule: Rule) -> None:
        """Warn (never fail, never disable) about unanchored legacy patterns.

        ``command_regex`` now uses :func:`re.fullmatch`, so a pattern without
        ``^``/``\\A`` ... ``$``/``\\Z`` anchors silently matches less than its
        author intended.  Deny rules are *not* skipped: skipping them would
        quietly remove a protection, so the rule stays loaded and the operator
        gets a warning to rewrite it.
        """
        scope = rule.scope if isinstance(rule.scope, dict) else {}
        pattern = scope.get("command_regex")
        if not isinstance(pattern, str) or not pattern:
            return
        if _pattern_is_fully_anchored(pattern):
            return
        self.log.warning(
            "legacy unanchored command_regex in rule %s: %r; command_regex is "
            "now matched with re.fullmatch, so rewrite it as '^...$' "
            "(or '\\A...\\Z')", rule.id, pattern)

    def _warn_once(self, key: tuple, message: str, *args: Any) -> None:
        """Emit a policy warning once per distinct problem."""
        with self._lock:
            if key in self._warned:
                return
            self._warned.add(key)
        self.log.warning(message, *args)

    def _default(self, risk: str) -> Decision:
        level = _risk_index(risk)
        if level <= RISK_ORDER["low"]:
            if self.auto_allow_low:
                return Decision("allow", risk, "auto")
            return Decision("ask", risk, "default:low")
        setting = self.medium_default if level == RISK_ORDER["medium"] \
            else self.high_default
        if setting == "deny":
            return Decision("deny", risk, f"default:{risk}")
        return Decision("ask", risk, f"default:{risk}")

    def _get_locked(self, rule_id: str) -> Rule:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise NotFound(f"unknown rule: {rule_id!r}",
                           detail={"rule_id": rule_id})
        return rule

    def _validate(self, *, effect: str, capability: str, risk_max: str,
                  uses_left: int | None) -> None:
        if effect not in ("allow", "deny"):
            raise ValidationError(f"invalid rule effect: {effect!r}",
                                  detail={"allowed": ["allow", "deny"]})
        if capability != "*" and capability not in CAPABILITIES:
            raise BadRequest(f"unknown capability: {capability!r}")
        if risk_max not in RISK_ORDER:
            raise ValidationError(f"invalid risk_max: {risk_max!r}",
                                  detail={"allowed": sorted(RISK_ORDER)})
        if uses_left is not None and int(uses_left) < 0:
            raise ValidationError("uses_left must be >= 0")

    def _emit(self, type: str, rule: Rule, **extra: Any) -> EventRecord:
        data = {"rule_id": rule.id, "effect": rule.effect,
                "capability": rule.capability, "principal": rule.principal,
                "scope": rule.scope, "risk_max": rule.risk_max,
                "source": rule.source, "rule": rule.to_dict()}
        data.update(extra)
        project_id = rule.scope.get("project_id") if isinstance(rule.scope, dict) else None
        return emit_both(self.ctx, self.eventlog, type, data,
                         str(project_id) if project_id else None)


def _risk_index(risk: str) -> int:
    return RISK_ORDER.get(str(risk), RISK_ORDER["high"])


# Shell control operators that split or redirect a command line.  ``shlex``
# emits runs of these as standalone tokens; a quoted ``;`` stays inside a word
# token, which is exactly the distinction the chain guard needs.
_SHELL_CONTROL_CHARS = frozenset(";|&<>()")
# Marker for a command ``shlex`` cannot parse (unbalanced quotes).  It makes
# the op set non-empty so plain patterns fail closed on malformed commands.
_UNPARSED = "<unparsed>"


def _shell_control_ops(command: str) -> set[str]:
    """Return the unquoted shell control operators present in ``command``.

    ``;``, ``&&``, ``||``, ``|``, ``&``, ``(``, ``)``, ``<`` and ``>`` are
    recognised with ``shlex`` in POSIX mode with ``punctuation_chars`` enabled.
    Unquoted newlines are kept as tokens (whitespace is narrowed to spaces,
    tabs and CR) and command substitution -- ``$(...)`` and backticks -- is
    detected by a quote-aware scan because it also runs inside double quotes.

    Quoted control characters are not operators: ``echo "a;b"`` yields an
    empty set.  A command that cannot be parsed yields a non-empty marker set
    so callers treat it as chained (fail closed).
    """
    text = command if isinstance(command, str) else str(command)
    ops: set[str] = set()
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        # Keep newline out of the separators so an unquoted newline becomes a
        # standalone token; a quoted newline stays inside a word token.
        lexer.whitespace = " \t\r"
        tokens = list(lexer)
    except (TypeError, ValueError):
        ops.add(_UNPARSED)
        tokens = []
    for token in tokens:
        if token and set(token) <= _SHELL_CONTROL_CHARS:
            ops.add(token)
    ops.update(_quote_sensitive_ops(text))
    return ops


def _quote_sensitive_ops(command: str) -> set[str]:
    """Find operators that work inside double quotes: ``$(``, backtick, newline."""
    ops: set[str] = set()
    quote: str | None = None
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if quote == '"':
            if ch == '"':
                quote = None
            elif ch == "`":
                ops.add("`")
            elif ch == "$" and i + 1 < n and command[i + 1] == "(":
                ops.add("$(")
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "`":
            ops.add("`")
        elif ch == "$" and i + 1 < n and command[i + 1] == "(":
            ops.add("$(")
        elif ch == "\n":
            ops.add("\n")
        i += 1
    return ops


_PATTERN_CONTROL_LITERALS = frozenset(";&<>")


def _pattern_allows_shell_controls(pattern: str) -> bool:
    """True when a ``command_regex`` explicitly permits a shell control op.

    Splitting/redirection characters (``;``, ``&``, ``<``, ``>``), command
    substitution (``$(``), backticks and escaped literals (``\\;``, ``\\|``,
    ...) count.  A bare ``|`` is regex alternation, *not* an explicit shell
    pipe, so a literal pipe must be written ``\\|`` (or inside a character
    class); otherwise an ordinary alternation rule would accidentally opt out
    of the chain guard.
    """
    i, n = 0, len(pattern)
    in_class = False
    while i < n:
        ch = pattern[i]
        if ch == "\\" and i + 1 < n:
            nxt = pattern[i + 1]
            if nxt in _PATTERN_CONTROL_LITERALS or nxt in ("|", "`"):
                return True
            if nxt == "$":
                # ``\$(`` / ``\$\(`` both describe a literal command
                # substitution opener.
                j = i + 2
                if j < n and pattern[j] == "(":
                    return True
                if j + 1 < n and pattern[j] == "\\" and pattern[j + 1] == "(":
                    return True
            i += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch in _PATTERN_CONTROL_LITERALS or ch == "`":
            return True
        elif ch == "|":
            if in_class:
                return True
        elif ch == "$" and i + 1 < n and pattern[i + 1] == "(":
            return True
        i += 1
    return False


def _pattern_is_fully_anchored(pattern: str) -> bool:
    """True when ``pattern`` starts with ``^``/``\\A`` and ends with ``$``/``\\Z``."""
    if not (pattern.startswith("^") or pattern.startswith(r"\A")):
        return False
    if pattern.endswith(r"\Z"):
        return True
    if not pattern.endswith("$"):
        return False
    # An odd number of trailing backslashes escapes the final '$'.
    j = len(pattern) - 2
    slashes = 0
    while j >= 0 and pattern[j] == "\\":
        slashes += 1
        j -= 1
    return slashes % 2 == 0


def _host_of(url: str) -> str:
    if "://" in url:
        return (urlsplit(url).hostname or "").lower()
    return url.split("/", 1)[0].lower()


_DEFAULT_PORTS = {"http": 80, "https": 443}
_INVALID_PORT = object()


def _url_hostname(parts: Any) -> str:
    """Lower-cased hostname, with IPv6 brackets removed."""
    host = parts.hostname or ""
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.lower()


def _url_port(parts: Any) -> int | None | object:
    """Effective port: the explicit one, else the scheme default."""
    try:
        port = parts.port
    except ValueError:
        return _INVALID_PORT
    if port is not None:
        return port
    return _DEFAULT_PORTS.get((parts.scheme or "").lower())


def _url_prefix_match(url: str, expected: Any) -> bool:
    """Match ``url`` against ``expected`` by URL structure, never by string prefix.

    The scheme, hostname and effective port must be identical; the path must be
    equal to, or nested below, the expected path on a path-segment boundary.
    Userinfo and malformed URLs are rejected rather than raising.
    """
    try:
        u = urlsplit(str(url))
        e = urlsplit(str(expected))
    except (TypeError, ValueError):
        return False
    if not u.scheme or not e.scheme:
        return False
    if u.scheme.lower() != e.scheme.lower():
        return False
    host, wanted = _url_hostname(u), _url_hostname(e)
    if not host or host != wanted:
        return False
    if u.username or u.password or e.username or e.password:
        return False
    u_port, e_port = _url_port(u), _url_port(e)
    if u_port is _INVALID_PORT or e_port is _INVALID_PORT or u_port != e_port:
        return False
    path, prefix = u.path or "/", e.path or "/"
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


_MAX_PATH_CHARS = 8192


def _match_path_glob(raw: Any, pattern: Any, *, cwd: Any = None) -> bool:
    """Match the *normalized* absolute path against an absolute glob pattern.

    Only the canonical path is matched -- never the caller-supplied raw string
    -- so a path like ``/ws/proj/../../etc/passwd`` is first resolved (here to
    ``/etc/passwd``) and can no longer slip through a ``/ws/proj/*`` rule.
    ``*`` stays within one path segment and ``**`` crosses segments.  Unsafe or
    unsupported patterns (relative, ``..`` segments, NUL, bad class syntax) and
    malformed paths fail closed.
    """
    if not isinstance(pattern, str) or not pattern:
        return False
    if "\x00" in pattern or len(pattern) > _MAX_PATH_CHARS:
        return False
    if not pattern.startswith("/") or ".." in pattern.split("/"):
        return False
    compiled = _compile_path_glob(pattern)
    if compiled is None:
        return False
    try:
        raw_text = str(raw)
    except (TypeError, ValueError):
        return False
    if "\x00" in raw_text:
        return False
    try:
        path = normalize_path(raw_text, cwd=cwd)
    except (OSError, RuntimeError, ValueError):
        return False
    text = str(path)
    if "\x00" in text or len(text) > _MAX_PATH_CHARS:
        return False
    return compiled.fullmatch(text) is not None


@lru_cache(maxsize=512)
def _compile_path_glob(pattern: str) -> re.Pattern[str] | None:
    """Compile an absolute glob to a regex (cached); ``None`` means no match."""
    regex = _glob_to_regex(pattern)
    if regex is None:
        return None
    try:
        return re.compile(regex)
    except re.error:
        return None


def _glob_to_regex(pattern: str) -> str | None:
    """Translate a POSIX-ish glob into a regex used with ``fullmatch``.

    ``*`` -> one path segment (``[^/]*``), ``**`` -> any depth (``.*``),
    ``?`` -> one non-separator char (``[^/]``) and simple ``[...]`` classes
    (``[abc]``, ``[!abc]`` / ``[^abc]``) are supported.  Everything else is a
    literal, so regex metacharacters carry no extra meaning.  Unsupported class
    syntax returns ``None`` so the caller fails closed instead of guessing.
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            parsed = _glob_class(pattern, i)
            if parsed is None:
                return None
            rendered, i = parsed
            out.append(rendered)
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


def _glob_class(pattern: str, start: int) -> tuple[str, int] | None:
    """Parse a ``[...]`` class at ``start``; returns (regex, next index)."""
    i, n = start + 1, len(pattern)
    negate = False
    if i < n and pattern[i] in "!^":
        negate = True
        i += 1
    chars: list[str] = []
    while i < n:
        ch = pattern[i]
        if ch == "]" and chars:
            break
        chars.append(ch)
        i += 1
    else:
        return None  # unterminated class
    if not chars or "/" in chars or "[" in chars or "\\" in chars:
        return None  # unsupported syntax: fail closed
    body = "".join(re.escape(ch) for ch in chars)
    if negate:
        # A negated class must never match the path separator.
        return f"[^/{body}]", i + 1
    return f"[{body}]", i + 1


def _section(config: Any, name: str) -> Any:
    """Return ``config.<name>``, ``config`` itself, or None (config may be absent)."""
    if config is None:
        return None
    section = getattr(config, name, None)
    if section is not None:
        return section
    if isinstance(config, dict):
        return config.get(name, config)
    return config


def _value(section: Any, key: str, default: Any) -> Any:
    if section is None:
        return default
    if isinstance(section, dict):
        return section.get(key, default)
    return getattr(section, key, default)


__all__ = ["Rule", "Decision", "PolicyEngine"]

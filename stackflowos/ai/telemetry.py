"""Token usage accounting and the shared LLM runtime.

Usage is aggregated by project, role, provider and model and persisted to
``<state_dir>/state/llm_usage.json`` with an atomic replace.  A single
:class:`LLMRuntime` object is shared by every provider instance built by
:func:`stackflowos.ai.llm.make_provider`; rebuilding the provider on a config
patch therefore does not double-count or lose history, and tests can inject a
fresh tracker with :func:`get_runtime` / :func:`reset_runtime`.

The tracker is deliberately small and dependency-free: it must work from
worker processes too (no event loop required).  All mutations happen under a
``threading.Lock``; writes are atomic via ``util.jsonio.write_json``.
"""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..errors import Unavailable
from ..util.jsonio import read_json, write_json
from ..util.logging import get_logger
from .types import Usage

_log = get_logger("ai.usage")

#: Conservative fallback tokenizer: ~3 characters per token.  Real tokenizers
#: over/under-count for CJK and code; over-estimating keeps budgets safe.
#: There is no tokenizer dependency on purpose (stdlib-only providers, and a
#: worker must be able to run without the hub's Python environment).  Every
#: truncation below goes through the helpers in this module so the advertised
#: token count and the number of bytes actually kept by a clip can never
#: disagree.
CHARS_PER_TOKEN = 3


def estimate_tokens(text: Any) -> int:
    """Estimate tokens for a string (conservative characters/3 heuristic).

    This is the *single* estimator used for budgets, provider usage estimates,
    context trimming and every token-based truncation.
    """
    if text is None:
        return 0
    if not isinstance(text, str):
        text = str(text)
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN)) if text else 0


def tokens_to_chars(tokens: Any) -> int:
    """Character budget equivalent to ``tokens`` under :func:`estimate_tokens`."""
    try:
        value = int(tokens or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value) * CHARS_PER_TOKEN


def clip_text_to_tokens(text: Any, max_tokens: Any, *,
                        suffix: str = "…") -> str:
    """Clip ``text`` to a token budget using the shared estimator.

    The returned string is guaranteed to estimate at/below ``max_tokens``
    (the suffix counts against the budget), so a caller that clipped to N
    tokens and a later estimate both report the same bound.  A non-positive
    budget returns ``""`` instead of silently keeping the whole text.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    try:
        budget = int(max_tokens or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget <= 0:
        return ""
    if estimate_tokens(text) <= budget:
        return text
    suffix = str(suffix or "")
    if estimate_tokens(suffix) >= budget:
        # The marker alone would blow the budget: keep a shortened marker
        # rather than returning text that estimates above the limit.
        return clip_text_to_tokens(suffix, budget, suffix="")
    # Reserve the suffix first, then spend the remaining chars on content.
    content_chars = max(0, tokens_to_chars(budget)
                        - (len(suffix) + 1))
    return text[:content_chars] + suffix


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate prompt tokens for a chat message list."""
    total = 0
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        total += 4  # role/formatting overhead
        total += estimate_tokens(message.get("content"))
        tool_calls = message.get("tool_calls")
        if tool_calls:
            total += estimate_tokens(str(tool_calls))
        for key in ("name", "tool_call_id"):
            if message.get(key):
                total += estimate_tokens(str(message[key]))
    return total


class BudgetExceeded(Unavailable):
    """The project's token budget is exhausted; fail closed (no fallback)."""

    code = "UNAVAILABLE"
    http_status = 429

    def __init__(self, message: str, *, project_id: str | None = None) -> None:
        super().__init__(message)
        self.project_id = project_id


class UsageTracker:
    """Thread-safe, optionally persistent usage aggregation.

    When ``path`` is shared by several processes (hub + worker units all point
    at ``<state_dir>/state/llm_usage.json``), each tracker refreshes from disk
    before mutating or reading, so usage recorded by a worker becomes visible
    to the hub's ``llm_usage`` snapshot and to its budget checks without a
    restart.  Writes remain atomic; a read-modify-write race between two
    processes still loses at most the rows written in that window (the next
    record refreshes first, so history is not wiped by a later writer).
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._data = self._load()
        self._disk_stamp: tuple[int, int, int] | None = self._stat_stamp()
        #: In-memory rows not on disk yet (coalescing/OS error); blocks a
        #: refresh that would discard them.
        self._dirty = False
        self._last_save = 0.0
        #: Minimum seconds between writes from a burst; 0 disables coalescing.
        self.save_interval_s = 0.0

    # -- persistence ------------------------------------------------------
    @staticmethod
    def _empty() -> dict:
        empty_bucket = {"prompt": 0, "completion": 0, "total": 0,
                        "cost": 0.0, "calls": 0}
        return {
            "version": 1,
            "updated_at": 0,
            "totals": dict(empty_bucket),
            "projects": {},
            "roles": {},
            "providers": {},
            "models": {},
            "recent": [],
        }

    def _stat_stamp(self) -> tuple[int, int, int] | None:
        """Identity of the on-disk file (mtime_ns, size, inode).

        ``write_json`` replaces the file atomically, so the inode changes on
        every write even when a coarse filesystem clock would report the same
        mtime: the stamp reliably detects another process's update.
        """
        if self.path is None:
            return None
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (int(stat.st_mtime_ns), int(stat.st_size), int(stat.st_ino))

    def _read_raw(self) -> dict | None:
        if self.path is None:
            return None
        try:
            raw = read_json(self.path, default=None)
        except ValueError:
            _log.warning("ignoring corrupt llm usage file %s", self.path)
            return None
        return raw if isinstance(raw, dict) else None

    def _normalise(self, raw: dict) -> dict:
        merged = self._empty()
        for key in ("totals", "projects", "roles", "providers", "models"):
            value = raw.get(key)
            if key == "totals" and isinstance(value, dict):
                merged[key].update(
                    {name: value.get(name, merged[key][name])
                     for name in merged[key]})
            elif isinstance(value, dict):
                merged[key] = {
                    str(name): self._bucket(item) for name, item in value.items()
                }
        recent = raw.get("recent")
        if isinstance(recent, list):
            merged["recent"] = [item for item in recent[-200:]
                                if isinstance(item, dict)]
        merged["updated_at"] = int(raw.get("updated_at") or 0)
        return merged

    def _load(self) -> dict:
        raw = self._read_raw()
        return self._normalise(raw) if raw is not None else self._empty()

    def _refresh_locked(self) -> None:
        """Merge another process's writes into memory before touching state."""
        if self.path is None or self._dirty:
            return
        stamp = self._stat_stamp()
        if stamp is None or stamp == self._disk_stamp:
            return
        raw = self._read_raw()
        if raw is None:
            self._disk_stamp = stamp
            return
        self._data = self._normalise(raw)
        self._disk_stamp = stamp

    @staticmethod
    def _bucket(value: Any) -> dict:
        item = value if isinstance(value, dict) else {}
        return {
            "prompt": int(item.get("prompt") or 0),
            "completion": int(item.get("completion") or 0),
            "total": int(item.get("total") or 0),
            "cost": float(item.get("cost") or 0.0),
            "calls": int(item.get("calls") or 0),
        }

    def _save_locked(self) -> None:
        if self.path is None:
            return
        now = time.monotonic()
        if self.save_interval_s and now - self._last_save < self.save_interval_s:
            self._dirty = True
            return
        self._last_save = now
        self._data["updated_at"] = int(time.time() * 1000)
        try:
            write_json(self.path, self._data)
        except OSError:
            self._dirty = True
            _log.warning("cannot persist llm usage to %s", self.path,
                         exc_info=True)
            return
        self._dirty = False
        self._disk_stamp = self._stat_stamp()

    def flush(self) -> None:
        """Force a write (used by tests and graceful shutdown)."""
        with self._lock:
            self._last_save = 0.0
            self._refresh_locked()
            self._save_locked()

    # -- mutations --------------------------------------------------------
    @staticmethod
    def _add(bucket: dict, usage: Usage, cost: float) -> None:
        bucket["prompt"] += usage.prompt
        bucket["completion"] += usage.completion
        bucket["total"] += usage.total or (usage.prompt + usage.completion)
        bucket["cost"] = round(float(bucket.get("cost") or 0.0) + cost, 8)
        bucket["calls"] += 1

    def record(self, *, project_id: str | None, role: str | None,
               provider: str, model: str, usage: Usage,
               price_per_1k_input: float = 0.0,
               price_per_1k_output: float = 0.0) -> dict:
        """Add one request's usage and persist.  Returns the recorded bucket."""
        cost = (usage.prompt / 1000.0) * float(price_per_1k_input or 0.0) \
            + (usage.completion / 1000.0) * float(price_per_1k_output or 0.0)
        project = str(project_id or "")
        role_key = str(role or "")
        provider_key = str(provider or "unknown")
        model_key = str(model or "unknown")
        with self._lock:
            self._refresh_locked()
            self._add(self._data["totals"], usage, cost)
            for key, bucket_key in ((project, "projects"), (role_key, "roles"),
                                    (provider_key, "providers"),
                                    (model_key, "models")):
                if not key:
                    continue
                bucket = self._data[bucket_key].setdefault(
                    key, {"prompt": 0, "completion": 0, "total": 0,
                          "cost": 0.0, "calls": 0})
                self._add(bucket, usage, cost)
            self._data["recent"].append({
                "ts": int(time.time() * 1000),
                "project_id": project or None,
                "role": role_key or None,
                "provider": provider_key,
                "model": model_key,
                "prompt": usage.prompt,
                "completion": usage.completion,
                "total": usage.total,
                "cost": cost,
            })
            del self._data["recent"][:-200]
            self._save_locked()
            return self._bucket_copy(self._data["totals"])

    @staticmethod
    def _bucket_copy(bucket: dict) -> dict:
        return dict(UsageTracker._bucket(bucket))

    def reset(self) -> None:
        with self._lock:
            self._data = self._empty()
            self._dirty = False
            self._last_save = 0.0
            self._save_locked()

    # -- queries ----------------------------------------------------------
    def snapshot(self, project_id: str | None = None) -> dict:
        """Return a JSON-safe copy of the aggregates (optionally per project)."""
        with self._lock:
            self._refresh_locked()
            data = {
                "totals": self._bucket_copy(self._data["totals"]),
                "projects": {k: self._bucket_copy(v) for k, v in
                             self._data["projects"].items()},
                "roles": {k: self._bucket_copy(v) for k, v in
                          self._data["roles"].items()},
                "providers": {k: self._bucket_copy(v) for k, v in
                              self._data["providers"].items()},
                "models": {k: self._bucket_copy(v) for k, v in
                           self._data["models"].items()},
                "recent": [dict(item) for item in self._data["recent"][-50:]],
                "updated_at": self._data.get("updated_at", 0),
            }
        if project_id:
            key = str(project_id)
            data["project"] = data["projects"].get(
                key, {"prompt": 0, "completion": 0, "total": 0,
                      "cost": 0.0, "calls": 0})
        return data

    def project_tokens(self, project_id: str | None) -> int:
        if not project_id:
            return 0
        with self._lock:
            self._refresh_locked()
            bucket = self._data["projects"].get(str(project_id)) or {}
            return int(bucket.get("total") or 0)

    def total_tokens(self) -> int:
        with self._lock:
            self._refresh_locked()
            return int(self._data["totals"].get("total") or 0)

    def check_budget(self, project_id: str | None, predicted_tokens: int,
                     budget: int) -> None:
        """Raise :class:`BudgetExceeded` when a project would exceed its cap."""
        if not budget or budget <= 0 or not project_id:
            return
        used = self.project_tokens(project_id)
        if used + max(0, int(predicted_tokens)) > int(budget):
            raise BudgetExceeded(
                f"project token budget exceeded for {project_id}: "
                f"{used}/{budget} used, {predicted_tokens} more requested",
                project_id=str(project_id))


class LLMRuntime:
    """Shared state injected into providers: usage tracker + event sink."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.usage = UsageTracker()
        self.event_sink: Callable[[str, dict], Any] | None = None
        self.state_dir: Path | None = None
        #: Overrides used by tests before a state dir is attached.
        self.provider_config: Any = None

    def configure(self, *, state_dir: Path | str | None = None,
                  event_sink: Callable[[str, dict], Any] | None = None) -> None:
        """Attach a state dir and/or event sink.

        Switching to a *different* state dir reloads the tracker from that
        directory's file (restart recovery).  Re-attaching the same directory
        keeps the in-memory aggregates so a config hot-reload cannot reset
        counters.
        """
        with self._lock:
            if state_dir is not None:
                path = Path(state_dir) / "state" / "llm_usage.json"
                if self.state_dir != Path(state_dir) or self.usage.path != path:
                    self.state_dir = Path(state_dir)
                    self.usage = UsageTracker(path)
                    self.usage.save_interval_s = 0.0
            if event_sink is not None:
                self.event_sink = event_sink

    def emit(self, type: str, data: dict) -> None:
        sink = self.event_sink
        if sink is None:
            return
        try:
            sink(type, dict(data or {}))
        except Exception:  # telemetry must never break a model request
            _log.debug("llm runtime event sink failed", exc_info=True)

    def record(self, *, project_id: str | None, role: str | None,
               provider: str, model: str, usage: Usage,
               price_per_1k_input: float = 0.0,
               price_per_1k_output: float = 0.0) -> dict:
        bucket = self.usage.record(
            project_id=project_id, role=role, provider=provider, model=model,
            usage=usage, price_per_1k_input=price_per_1k_input,
            price_per_1k_output=price_per_1k_output)
        self.emit("llm.usage", {
            "project_id": project_id or None,
            "role": role or None,
            "provider": provider,
            "model": model,
            "prompt": usage.prompt,
            "completion": usage.completion,
            "total": usage.total,
            "cost": round(
                usage.prompt / 1000.0 * float(price_per_1k_input or 0.0)
                + usage.completion / 1000.0 * float(price_per_1k_output or 0.0),
                8),
            "cumulative": bucket,
        })
        return bucket


_RUNTIME = LLMRuntime()


def get_runtime() -> LLMRuntime:
    return _RUNTIME


def reset_runtime(*, state_dir: Path | str | None = None) -> LLMRuntime:
    """Replace the process-wide runtime (tests / worker bootstrap)."""
    global _RUNTIME
    _RUNTIME = LLMRuntime()
    if state_dir is not None:
        _RUNTIME.configure(state_dir=state_dir)
    return _RUNTIME


def attach_provider_runtime(provider: Any, app: Any) -> None:
    """Wire a provider (or router tree) to the hub's state dir and event log."""
    runtime = get_runtime()
    paths = getattr(app, "paths", None)
    eventlog = getattr(app, "eventlog", None)

    def sink(type: str, data: dict) -> None:
        if eventlog is None:
            return
        eventlog.append(type, data, source="sys")

    runtime.configure(
        state_dir=getattr(paths, "state_dir", None),
        event_sink=sink if eventlog is not None else None)
    hook = getattr(provider, "attach_runtime", None)
    if callable(hook):
        try:
            hook(runtime)
        except Exception:  # pragma: no cover - defensive
            _log.debug("provider runtime attach failed", exc_info=True)


__all__ = [
    "BudgetExceeded",
    "CHARS_PER_TOKEN",
    "LLMRuntime",
    "UsageTracker",
    "attach_provider_runtime",
    "clip_text_to_tokens",
    "estimate_messages_tokens",
    "estimate_tokens",
    "get_runtime",
    "reset_runtime",
    "tokens_to_chars",
]

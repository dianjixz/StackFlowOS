"""Prometheus metrics for the hub (``GET /api/metrics``).

Design constraints:

* **Scrape is non-blocking and side-effect free.**  Every number is read from
  in-memory structures of the core services; no disk/network I/O happens while
  rendering.  Optional subsystems are probed with ``getattr`` and may simply be
  missing.
* **Fail-open.**  A broken optional source must not turn a scrape into a 500 or
  affect the hub: each collector has its own guard and a failure is counted in
  ``/api/health``.
* **Exposition is boring.**  One ``# HELP``/``# TYPE`` pair per family, sorted
  label sets, correct backslash/quote/newline escaping, and the exact
  Prometheus text content type (``text/plain; version=0.0.4``).
* **Optional instrumentation is explicit.**  The standard library server does
  not persist request or SSE counters, so this extension wraps
  ``WebAPI.handle`` / ``WebServer._serve_sse`` once per process to observe them
  (without changing return values).  Both wrappers are defensive and are
  documented in ``docs/team/ops.md``.  Only requests routed through
  ``WebAPI.handle`` are counted in ``sfo_http_requests_total``: ``/healthz``,
  static assets and the SSE route are served by ``WebServer`` before the API
  router, so they are not part of that counter (live SSE clients are covered by
  ``sfo_sse_clients``).

``observability.metrics_enabled = false`` keeps the extension loaded (health
still reports it) but the route answers ``404`` -- exposing metrics is opt-in.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
import weakref
from typing import Any, Iterable

from ..util.logging import get_logger
from ..web.api import WebReply
from . import config_of, safe_float, safe_int
from .logging import configure_from_app

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_MAX_SEED_EVENTS = 1000

#: Per-app HTTP observations: ``{app: {(method, status): count}}``.
_http_counts: "weakref.WeakKeyDictionary[Any, dict[tuple[str, str], int]]" = (
    weakref.WeakKeyDictionary())
#: Per-app live SSE stream count.
_sse_clients: "weakref.WeakKeyDictionary[Any, int]" = weakref.WeakKeyDictionary()
_instrumented = False

_HTTP_HELP = ("Total routed /api/* requests handled by the web console, by HTTP "
              "method and response status. Requests that never reach the API "
              "router -- /healthz, static assets and the SSE stream "
              "(/api/events/stream) -- are NOT counted here; live SSE clients "
              "are exposed by sfo_sse_clients instead.")
_SSE_HELP = "Currently connected /api/events/stream (SSE) clients."


# -- metric text helpers -------------------------------------------------
def _escape_help(text: Any) -> str:
    return str(text).replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label(value: Any) -> str:
    text = str(value)
    return (text.replace("\\", "\\\\").replace("\n", "\\n")
            .replace('"', '\\"'))


def _format_number(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    number = safe_float(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "+Inf" if number > 0 else "-Inf"
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return format(number, ".12g")


def _labels(pairs: Iterable[tuple[str, Any]]) -> str:
    items = [(str(name), _escape_label(value))
             for name, value in pairs if name]
    if not items:
        return ""
    return "{" + ",".join(f'{name}="{value}"' for name, value in items) + "}"


def _family(lines: list[str], name: str, kind: str, help_text: str,
            samples: Iterable[tuple[Iterable[tuple[str, Any]], Any]]) -> None:
    lines.append(f"# HELP {name} {_escape_help(help_text)}")
    lines.append(f"# TYPE {name} {kind}")
    for label_pairs, value in samples:
        lines.append(f"{name}{_labels(label_pairs)} {_format_number(value)}")


def _sorted_counts(counts: dict[Any, int]) -> list[tuple[Any, int]]:
    return sorted(counts.items(), key=lambda item: str(item[0]))


# -- optional instrumentation -------------------------------------------
def record_http_request(app: Any, method: str, status: Any) -> None:
    """Record one API request; safe to call from any thread/context."""
    if app is None:
        return
    key = (str(method or "?").upper(), str(safe_int(status, 0)))
    try:
        bucket = _http_counts.get(app)
        if bucket is None:
            bucket = {}
            _http_counts[app] = bucket
        bucket[key] = bucket.get(key, 0) + 1
    except TypeError:  # app is not weak-referenceable: instrumentation only
        return


def record_sse_client(app: Any, delta: int) -> None:
    """Adjust the live SSE client gauge for *app*."""
    if app is None:
        return
    try:
        _sse_clients[app] = max(0, _sse_clients.get(app, 0) + int(delta))
    except TypeError:
        return


def sse_client_count(app: Any) -> int:
    """Best-effort SSE client count (instrumented wrapper or known attribute)."""
    if app is None:
        return 0
    try:
        value = _sse_clients.get(app)
    except TypeError:
        value = None
    if value is not None:
        return max(0, int(value))
    for attribute in ("web_server", "server", "web", "http_server", "api_server"):
        server = getattr(app, attribute, None)
        raw = getattr(server, "_sse_clients", None)
        if raw is not None:
            return max(0, safe_int(raw))
    return 0


def ensure_instrumentation() -> None:
    """Install the HTTP/SSE observation wrappers exactly once per process."""
    global _instrumented
    if _instrumented:
        return
    try:
        from ..web.api import WebAPI
        from ..web.server import WebServer
    except Exception:  # pragma: no cover - importable in every real install
        return

    if not getattr(WebAPI.handle, "_sfo_observability", False):
        original_handle = WebAPI.handle

        async def handle(self, *args, **kwargs):
            result = await original_handle(self, *args, **kwargs)
            method = kwargs.get("method")
            if method is None and args:
                method = args[0]
            try:
                status = result[0] if isinstance(result, tuple) and result else 0
                record_http_request(getattr(self, "app", None), method, status)
            except Exception:
                pass
            return result

        handle._sfo_observability = True  # type: ignore[attr-defined]
        WebAPI.handle = handle  # type: ignore[method-assign]

    if not getattr(WebServer._serve_sse, "_sfo_observability", False):
        original_sse = WebServer._serve_sse

        async def serve_sse(self, *args, **kwargs):
            app = getattr(self, "app", None)
            record_sse_client(app, 1)
            try:
                return await original_sse(self, *args, **kwargs)
            finally:
                record_sse_client(app, -1)

        serve_sse._sfo_observability = True  # type: ignore[attr-defined]
        WebServer._serve_sse = serve_sse  # type: ignore[method-assign]

    _instrumented = True


# -- collectors ----------------------------------------------------------
def _safe(call, default, log=None, label="collector"):
    try:
        return call()
    except Exception:
        if log is not None:
            log.debug("metrics collector %s failed", label, exc_info=True)
        return default


def _unit_cards(app: Any) -> list[dict]:
    cards = _safe(lambda: app.unit_cards(), [], label="unit_cards")
    if isinstance(cards, list):
        return [card for card in cards if isinstance(card, dict)]
    return []


def _agents(app: Any) -> list[tuple[str, str]]:
    supervisor = getattr(app, "supervisor", None)
    if supervisor is None:
        return []
    rows = _safe(lambda: supervisor.list(), [], label="agents")
    out: list[tuple[str, str]] = []
    for agent in rows or []:
        if isinstance(agent, dict):
            out.append((str(agent.get("status") or "unknown"),
                        str(agent.get("role") or "unknown")))
        else:
            out.append((str(getattr(agent, "status", "unknown")),
                        str(getattr(agent, "role", "unknown"))))
    return out


def _projects(app: Any) -> list[Any]:
    store = getattr(app, "projects", None)
    if store is None:
        return []
    rows = _safe(lambda: store.list(), [], label="projects")
    return list(rows or [])


def _tasks(app: Any) -> list[Any]:
    store = getattr(app, "projects", None)
    if store is None:
        return []
    rows = _safe(lambda: store.task_list(), [], label="tasks")
    return list(rows or [])


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _queue_depth(app: Any) -> int | None:
    """Best-effort queued-dispatch depth, or ``None`` when no source knows it.

    ``None`` is rendered as ``-1`` plus ``sfo_agent_queue_known 0``: a missing
    queue source must never look like an empty queue (0), which would hide a
    stuck dispatcher.
    """
    objects = [app, getattr(app, "supervisor", None),
               getattr(app, "agents", None), getattr(app, "scheduler", None)]
    seen: set[int] = set()
    for obj in objects:
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        for name in ("queue_depth", "queue_size", "pending_dispatches",
                     "pending_count", "queued"):
            raw = getattr(obj, name, None)
            if raw is None:
                continue
            if callable(raw):
                raw = _safe(raw, None, label=f"queue.{name}")
                if inspect.isawaitable(raw):
                    try:
                        raw.close()
                    except Exception:
                        pass
                    continue
            if isinstance(raw, dict):
                # A mapping without any of the accepted keys carries no
                # measurement: fall through to the next candidate instead of
                # pretending the depth is 0.
                measured = False
                for key in ("depth", "queued", "size", "pending"):
                    if key in raw:
                        raw = raw[key]
                        measured = True
                        break
                if not measured:
                    continue
            value = safe_int(raw, -1)
            if value >= 0:
                return value
    supervisor = getattr(app, "supervisor", None)
    stats = _safe(lambda: supervisor.stats(), {}, label="supervisor.stats")
    if isinstance(stats, dict):
        for key in ("queue_depth", "queued", "pending_dispatches"):
            if key in stats:
                return max(0, safe_int(stats[key]))
    return None


def _llm_usage(app: Any) -> dict | None:
    """Find an optional LLM usage snapshot without importing the LLM package."""
    candidates: list[Any] = [
        app,
        getattr(app, "llm", None),
        getattr(app, "provider", None),
        getattr(app, "chat", None),
        getattr(app, "agent_chat", None),
        getattr(getattr(app, "agent_chat", None), "provider", None),
    ]
    seen: set[int] = set()
    for obj in candidates:
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        for name in ("llm_usage", "usage_snapshot", "usage_stats", "usage"):
            raw = getattr(obj, name, None)
            if raw is None:
                continue
            if callable(raw):
                try:
                    raw = raw()
                except TypeError:
                    continue
                except Exception:
                    continue
            if inspect.isawaitable(raw):
                try:
                    raw.close()
                except Exception:
                    pass
                continue
            if isinstance(raw, dict) and raw:
                return raw
    # Providers expose the shared runtime instead of a usage method: the
    # ``UsageTracker.snapshot()`` behind ``runtime.usage`` is the canonical
    # accounting object for every provider (mock/openai/anthropic/gemini).
    runtime_seen: set[int] = set()
    for obj in candidates:
        if obj is None:
            continue
        runtime = getattr(obj, "runtime", None)
        if runtime is None or id(runtime) in runtime_seen:
            continue
        runtime_seen.add(id(runtime))
        usage = getattr(runtime, "usage", None)
        if usage is None:
            continue
        snapshot = getattr(usage, "snapshot", None)
        try:
            value = snapshot() if callable(snapshot) else usage
        except Exception:
            continue
        if isinstance(value, dict) and value:
            return value
    # ``provider.stats()`` may embed usage rather than expose a method.
    provider = getattr(app, "provider", None)
    stats = _safe(lambda: provider.stats(), None, label="provider.stats")
    if isinstance(stats, dict):
        for key in ("usage", "llm_usage", "tokens"):
            value = stats.get(key)
            if isinstance(value, dict) and value:
                return value
    return None


def _provider_model(app: Any) -> tuple[str, str]:
    for attribute in ("provider",
                      "agent_chat.provider", "chat.provider"):
        current: Any = app
        for part in attribute.split("."):
            current = getattr(current, part, None)
            if current is None:
                break
        if current is not None:
            return (str(getattr(current, "name", "") or "unknown"),
                    str(getattr(current, "model", "") or "unknown"))
    return ("unknown", "unknown")


def _usage_tokens(usage: dict, app: Any) -> list[tuple[str, str, str, int]]:
    """Normalise the optional LLM usage shapes into ``(provider, model, kind, n)``.

    Supported shapes (first match wins so one request is never double-counted):

    * ``{"tokens": [{"provider","model","kind","count"}...]}``
    * ``{"tokens": {provider: {model: {kind: n}}}}``
    * ``{"models": {model: {"prompt","completion","total",...}}}``
      (StackFlowOS ``UsageTracker.snapshot()``; provider is inferred when the
      snapshot has exactly one provider)
    * ``{"providers": {provider: {...bucket...}}}``
    * ``{"totals": {...bucket...}}``
    * ``{"prompt_tokens": n, "completion_tokens": n}``

    Bucket fields other than prompt/completion/total (``cost``, ``calls``) are
    deliberately not turned into token samples.
    """
    rows: list[tuple[str, str, str, int]] = []
    default_provider, default_model = _provider_model(app)

    def add_bucket(provider: str, model: str, bucket: Any) -> None:
        if not isinstance(bucket, dict):
            if isinstance(bucket, (int, float)):
                rows.append((provider, model, "total", safe_int(bucket)))
            return
        for kind in ("prompt", "completion", "total"):
            value = bucket.get(kind)
            if isinstance(value, (int, float)):
                rows.append((provider, model, kind, safe_int(value)))

    tokens = usage.get("tokens")
    if isinstance(tokens, dict):
        for provider, models in tokens.items():
            if isinstance(models, dict):
                for model, kinds in models.items():
                    if isinstance(kinds, dict):
                        for kind, count in kinds.items():
                            if isinstance(count, (int, float)):
                                rows.append((str(provider), str(model),
                                             str(kind), safe_int(count)))
                    else:
                        rows.append((str(provider), str(model), "total",
                                     safe_int(kinds)))
            else:
                rows.append((str(provider), default_model, "total",
                             safe_int(models)))
        return _fold(rows)
    if isinstance(tokens, list):
        for item in tokens:
            if not isinstance(item, dict):
                continue
            rows.append((
                str(item.get("provider") or default_provider),
                str(item.get("model") or default_model),
                str(item.get("kind") or item.get("type") or "total"),
                safe_int(item.get("count", item.get("tokens",
                                                     item.get("total", 0)))),
            ))
        return _fold(rows)
    if isinstance(tokens, (int, float)):
        return _fold([(default_provider, default_model, "total",
                       safe_int(tokens))])

    providers = usage.get("providers")
    models = usage.get("models")
    if not isinstance(models, dict):
        models = usage.get("by_model")
    if isinstance(models, dict) and models:
        single_provider = None
        if isinstance(providers, dict) and len(providers) == 1:
            single_provider = str(next(iter(providers)))
        for model, bucket in models.items():
            provider, model_name = (single_provider or default_provider,
                                    str(model))
            if "/" in model_name:
                provider, model_name = model_name.split("/", 1)
            add_bucket(provider, model_name, bucket)
        return _fold(rows)
    if isinstance(providers, dict) and providers:
        for provider, bucket in providers.items():
            add_bucket(str(provider), default_model, bucket)
        return _fold(rows)
    totals = usage.get("totals")
    if isinstance(totals, dict) and totals:
        add_bucket(default_provider, default_model, totals)
        return _fold(rows)

    # Plain ``{"prompt_tokens": ..., "completion_tokens": ...}`` shape.
    for kind in ("prompt", "completion", "total"):
        for key in (kind, f"{kind}_tokens"):
            if key in usage and isinstance(usage[key], (int, float)):
                rows.append((default_provider, default_model, kind,
                             safe_int(usage[key])))
    return _fold(rows)


def _fold(rows: list[tuple[str, str, str, int]]) -> list[tuple[str, str, str, int]]:
    """Fold duplicate label sets into one sample and sort deterministically."""
    folded: dict[tuple[str, str, str], int] = {}
    for provider, model, kind, count in rows:
        key = (provider or "unknown", model or "unknown", kind or "total")
        folded[key] = folded.get(key, 0) + max(0, count)
    return [(provider, model, kind, count)
            for (provider, model, kind), count in sorted(folded.items())]

def _usage_cost(usage: dict) -> float:
    for key in ("cost_usd", "total_cost_usd", "cost", "total_cost"):
        if key in usage:
            return max(0.0, safe_float(usage.get(key)))
    totals = usage.get("totals")
    if isinstance(totals, dict) and "cost" in totals:
        return max(0.0, safe_float(totals.get("cost")))
    total = 0.0
    tokens = usage.get("tokens")
    if isinstance(tokens, list):
        for item in tokens:
            if isinstance(item, dict):
                total += max(0.0, safe_float(
                    item.get("cost_usd", item.get("cost", 0))))
    if total:
        return total
    # ``UsageTracker.snapshot``: providers aggregate equals the grand total.
    for key in ("providers", "models"):
        bucket_map = usage.get(key)
        if isinstance(bucket_map, dict):
            for bucket in bucket_map.values():
                if isinstance(bucket, dict):
                    total += max(0.0, safe_float(bucket.get("cost", 0)))
            if total:
                return total
    return 0.0


class MetricsExtension:
    """Extension object implementing the ``metrics`` hook set."""

    name = "metrics"

    def __init__(self, app: Any, *, enabled: bool = True) -> None:
        self.app = app
        self.enabled = bool(enabled)
        self.log = get_logger("observability.metrics")
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._decided: dict[str, int] = {"allow": 0, "deny": 0}
        self._events_seen = 0
        self._seeded_seq = 0
        self._scrapes = 0
        self._last_error: str | None = None
        self._log_config: dict = {}
        self._instrumented = False

    # -- lifecycle -------------------------------------------------------
    def routes(self) -> list[tuple[str, str, Any]]:
        return [("GET", r"/api/metrics", self.handle_metrics)]

    async def start(self) -> None:
        self._log_config = configure_from_app(self.app)
        if self.enabled:
            ensure_instrumentation()
            self._instrumented = True
        self._seed_approvals()
        try:
            self._queue = self.app.eventlog.subscribe()
        except Exception:
            self._last_error = "eventlog unavailable"
            self.log.exception("metrics could not subscribe to the event log")
            return
        if getattr(self._queue, "closed", False):
            self._last_error = "eventlog closed"
            self._queue = None
            return
        self._running = True
        self._task = asyncio.create_task(self._consume(), name="sfo-metrics")

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._queue is not None:
            self.app.eventlog.unsubscribe(self._queue)
            self._queue = None

    async def on_config(self, config: Any) -> None:
        observability = getattr(config, "observability", None)
        if observability is not None:
            self.enabled = bool(getattr(observability, "metrics_enabled", True))
        self._log_config = configure_from_app(self.app)
        if self.enabled:
            ensure_instrumentation()
            self._instrumented = True

    def health(self) -> dict:
        return {
            "ok": bool((not self.enabled) or self._running),
            "enabled": self.enabled,
            "subscribed": self._running,
            "events_seen": self._events_seen,
            "scrapes": self._scrapes,
            "last_error": self._last_error,
            "log_rotation": bool(self._log_config.get("rotating", False)),
        }

    # -- event subscription ----------------------------------------------
    def _seed_approvals(self) -> None:
        try:
            rows = self.app.eventlog.query(
                types=["approval.decided", "approval.auto"],
                limit=_MAX_SEED_EVENTS)
        except Exception:
            return
        for record in rows or []:
            if getattr(record, "type", "") not in ("approval.decided",
                                                   "approval.auto"):
                continue
            self._seeded_seq = max(self._seeded_seq,
                                   safe_int(getattr(record, "seq", 0)))
            data = getattr(record, "data", {}) or {}
            decision = str(data.get("decision") or "").lower()
            if decision in ("allow", "deny"):
                self._decided[decision] = self._decided.get(decision, 0) + 1

    async def _consume(self) -> None:
        queue = self._queue
        if queue is None:
            return
        while self._running:
            try:
                record = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.debug("metrics subscriber error", exc_info=True)
                await asyncio.sleep(0.05)
                continue
            try:
                self._observe(record)
            except Exception:
                self.log.debug("metrics event observation failed", exc_info=True)

    def _observe(self, record: Any) -> None:
        self._events_seen += 1
        if getattr(record, "type", "") not in ("approval.decided",
                                               "approval.auto"):
            return
        if safe_int(getattr(record, "seq", 0)) <= self._seeded_seq:
            return
        data = getattr(record, "data", {}) or {}
        decision = str(data.get("decision") or "").lower()
        if decision in ("allow", "deny"):
            self._decided[decision] = self._decided.get(decision, 0) + 1

    # -- rendering -------------------------------------------------------
    def _render(self) -> str:
        app = self.app
        lines: list[str] = []
        if not self.enabled:
            return "# metrics are disabled (observability.metrics_enabled=false)\n"

        started_at = safe_int(getattr(app, "started_at", 0))
        uptime = max(0.0, time.time() - started_at / 1000.0) if started_at else 0.0
        last_seq = _safe(lambda: app.eventlog.last_seq, 0, self.log,
                         "eventlog.last_seq")
        _family(lines, "sfo_up", "gauge",
                "1 when the hub process is serving this scrape.", [((), 1)])
        _family(lines, "sfo_uptime_seconds", "gauge",
                "Seconds since the hub app started.", [((), uptime)])
        _family(lines, "sfo_events_total", "counter",
                "Total events sequenced by the hub event log.", [((), last_seq)])
        _family(lines, "sfo_event_seq", "gauge",
                "Last globally assigned event sequence number.", [((), last_seq)])

        unit_counts: dict[tuple[str, str], int] = {}
        restart_total = 0
        for card in _unit_cards(app):
            state = str(card.get("state") or card.get("status") or "unknown")
            tier = str(card.get("tier") or "unknown")
            key = (state, tier)
            unit_counts[key] = unit_counts.get(key, 0) + 1
            restart_total += max(0, safe_int(card.get("restart_count", 0)))
        _family(lines, "sfo_units", "gauge",
                "Units (including sys.0) by lifecycle state and tier.",
                [((("state", state), ("tier", tier)), count)
                 for (state, tier), count in sorted(unit_counts.items())])
        _family(lines, "sfo_unit_restarts_total", "counter",
                "Sum of supervised unit restart counters.", [((), restart_total)])

        agent_counts: dict[tuple[str, str], int] = {}
        for status, role in _agents(app):
            key = (status, role)
            agent_counts[key] = agent_counts.get(key, 0) + 1
        if not agent_counts:
            stats = _safe(lambda: app.supervisor.stats(), {}, self.log,
                          "supervisor.stats")
            if isinstance(stats, dict):
                for status, count in (stats.get("by_status") or {}).items():
                    agent_counts[(str(status), "unknown")] = safe_int(count)
        _family(lines, "sfo_agents", "gauge",
                "Agent roster by status and role.",
                [((("status", status), ("role", role)), count)
                 for (status, role), count in sorted(agent_counts.items())])
        queue_depth = _queue_depth(app)
        _family(lines, "sfo_agent_queue_depth", "gauge",
                "Queued agent dispatches waiting for a worker; -1 means no "
                "source reported a queue depth (see sfo_agent_queue_known).",
                [((), -1 if queue_depth is None else queue_depth)])
        _family(lines, "sfo_agent_queue_known", "gauge",
                "1 when a live source provided sfo_agent_queue_depth, 0 when "
                "the queue depth is unknown (-1).",
                [((), 0 if queue_depth is None else 1)])

        project_counts: dict[str, int] = {}
        for project in _projects(app):
            status = str(_attr(project, "status", "unknown") or "unknown")
            project_counts[status] = project_counts.get(status, 0) + 1
        _family(lines, "sfo_projects", "gauge",
                "Projects by lifecycle status.",
                [((("status", status),), count)
                 for status, count in sorted(project_counts.items())])

        task_counts: dict[str, int] = {}
        for task in _tasks(app):
            status = str(_attr(task, "status", "unknown") or "unknown")
            task_counts[status] = task_counts.get(status, 0) + 1
        _family(lines, "sfo_tasks", "gauge",
                "Tasks by workflow status across all projects.",
                [((("status", status),), count)
                 for status, count in sorted(task_counts.items())])

        pending = _safe(lambda: app.approvals.pending_count(), 0, self.log,
                        "approvals.pending_count")
        _family(lines, "sfo_approvals_pending", "gauge",
                "Approval requests waiting for a human decision.", [((),
                max(0, safe_int(pending)))])
        decided = dict(self._decided)
        decided.setdefault("allow", 0)
        decided.setdefault("deny", 0)
        _family(lines, "sfo_approvals_decided_total", "counter",
                "Approval decisions recorded since startup, by decision.",
                [((("decision", decision),), count)
                 for decision, count in sorted(decided.items())])

        leases = _safe(lambda: app.leases.list(active_only=True), [], self.log,
                       "leases.list")
        _family(lines, "sfo_leases_active", "gauge",
                "Active leases currently held.", [((), len(leases or []))])

        # Fencing persistence is best-effort: a failed write still issues the
        # token in memory, so a restart may re-issue it.  Surface that instead
        # of leaving it only in the log.
        leases_hook = getattr(getattr(app, "leases", None), "health", None)
        leases_health = (_safe(leases_hook, {}, self.log, "leases.health")
                         if callable(leases_hook) else {}) or {}
        fencing = (leases_health.get("fencing")
                   if isinstance(leases_health, dict) else None) or {}
        _family(lines, "sfo_lease_fencing_persist_failures_total", "counter",
                "Lease fencing token writes that failed to reach disk (the "
                "token was still issued in memory; a restart may re-issue it).",
                [((), max(0, safe_int(fencing.get("persist_failures", 0))))])

        eventlog_hook = getattr(getattr(app, "eventlog", None), "health", None)
        eventlog_health = (_safe(eventlog_hook, {}, self.log, "eventlog.health")
                           if callable(eventlog_hook) else {}) or {}
        _family(lines, "sfo_eventlog_replay_skipped_transient_total", "counter",
                "Transient events (chat/agent deltas) skipped during replay "
                "and memory warm-up; they are never replayable.",
                [((), max(0, safe_int(
                    eventlog_health.get("replay_skipped_transient", 0))))])

        usage = _llm_usage(app)
        token_rows: list[tuple[str, str, str, int]] = []
        if usage is not None:
            try:
                token_rows = _usage_tokens(usage, app)
            except Exception:
                self.log.debug("could not normalise llm usage", exc_info=True)
                token_rows = []
        _family(lines, "sfo_llm_tokens_total", "counter",
                "LLM tokens consumed, by provider/model/kind (when the LLM "
                "subsystem exposes a usage snapshot).",
                [((("provider", provider), ("model", model), ("kind", kind)),
                  count) for provider, model, kind, count in token_rows])
        cost = _usage_cost(usage) if isinstance(usage, dict) else 0.0
        _family(lines, "sfo_llm_cost_usd_total", "counter",
                "Estimated LLM spend in USD (when the LLM subsystem exposes "
                "a usage snapshot).", [((), cost)])

        _family(lines, "sfo_sse_clients", "gauge",
                "Connected /api/events/stream clients.", [((),
                sse_client_count(app))])
        try:
            http_counts = _http_counts.get(app) or {}
        except TypeError:
            http_counts = {}
        _family(lines, "sfo_http_requests_total", "counter", _HTTP_HELP,
                [(((("method", method), ("status", status))), count)
                 for (method, status), count in sorted(http_counts.items())])
        return "\n".join(lines) + "\n"

    def metrics_text(self) -> str:
        """Render the exposition (used by the route and by tests)."""
        self._scrapes += 1
        try:
            return self._render()
        except Exception as exc:  # never break a scrape because of one source
            self._last_error = type(exc).__name__
            self.log.exception("metrics rendering failed")
            return ("# HELP sfo_up 1 when the hub process is serving this scrape.\n"
                    "# TYPE sfo_up gauge\nsfo_up 1\n"
                    f"# metrics render error: {type(exc).__name__}\n")

    # -- route -----------------------------------------------------------
    async def handle_metrics(self, match: Any, query: dict, body: dict):
        if not self.enabled:
            return 404, {"ok": False, "error": {
                "code": "NOT_FOUND",
                "message": "metrics are disabled (observability.metrics_enabled=false)"}}
        return 200, WebReply(self.metrics_text(), content_type=_CONTENT_TYPE)


def build(app: Any) -> MetricsExtension:
    """Extension entry point used by :mod:`stackflowos.extensions`."""
    config = config_of(app)
    observability = getattr(config, "observability", None)
    enabled = bool(getattr(observability, "metrics_enabled", True))
    return MetricsExtension(app, enabled=enabled)


__all__ = ["MetricsExtension", "_CONTENT_TYPE", "build",
           "ensure_instrumentation", "record_http_request",
           "record_sse_client", "sse_client_count"]

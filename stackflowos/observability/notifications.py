"""Outbound webhook notifications for hub events.

The operator configures sinks under ``observability.webhooks``::

    {"type": "webhook", "url": "https://hooks.example/sfo",
     "events": ["approval.created", "unit.failed"],
     "headers": {"Authorization": "Bearer ..."}, "timeout_s": 3,
     "rate_limit_per_s": 5, "burst": 5, "retries": 1}

A sink without ``events`` receives ``observability.notify_events``.  Event
types support dotted prefixes exactly like ``EventLog.query`` (``"approval"``
matches ``approval.created`` but not ``approvalbook``).

Delivery must never slow the event log or the HTTP loop:

* one background consumer drains ``eventlog.subscribe()``;
* each sink delivery is an ``asyncio.Task`` that runs the blocking urllib call
  in ``asyncio.to_thread``;
* per-sink token bucket (rate/burst), one retry with a short backoff, bounded
  in-flight tasks and a bounded dedupe window.

Failures are visible: every final outcome (success or failure after the retry)
is recorded as an ``approval.notified`` event (never re-forwarded, so a sink
subscribed to everything cannot notify about its own notifications).  Payload
data and error text are redacted before sending/recording; configured headers
are never logged.  ``POST /api/notifications/test`` (operator only) sends one
synthetic event to every sink and returns per-sink results.
"""

from __future__ import annotations

import asyncio
import json
import time
import types
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..errors import Denied
from ..util.logging import get_logger
from . import (META_EVENTS, app_version, config_of, now_ms, redact_structure,
               redact_text, safe_float, safe_int)

_MAX_SEEN = 10_000
_MAX_INFLIGHT = 32
_MAX_PENDING_TASKS = 256


def _type_matches(event_type: str, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return False
    return any(event_type == pattern or event_type.startswith(pattern + ".")
               for pattern in patterns)


def _normalise_events(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [item.strip() for item in raw.split(",")]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _clean_headers(raw: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    if not isinstance(raw, dict):
        return headers
    for key, value in raw.items():
        name = str(key or "").replace("\r", "").replace("\n", "").strip()
        if not name:
            continue
        text = str(value or "").replace("\r", "").replace("\n", "").strip()
        headers[name] = text
    return headers


@dataclass
class _TokenBucket:
    """Small async token bucket serialising one sink's deliveries."""

    rate: float = 5.0
    burst: int = 5
    tokens: float = field(init=False)
    updated: float = field(init=False)
    lock: asyncio.Lock = field(init=False)

    def __post_init__(self) -> None:
        self.rate = max(0.1, float(self.rate))
        self.burst = max(1, int(self.burst))
        self.tokens = float(self.burst)
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.burst,
                                  self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                await asyncio.sleep(max(0.005, (1.0 - self.tokens) / self.rate))


@dataclass
class Sink:
    """One configured webhook destination."""

    name: str
    url: str
    events: tuple[str, ...]
    headers: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 5.0
    retries: int = 1
    retry_backoff_s: float = 0.5
    rate_limit_per_s: float = 5.0
    burst: int = 5
    bucket: _TokenBucket | None = None

    def matches(self, event_type: str) -> bool:
        return _type_matches(event_type, self.events)

    def __post_init__(self) -> None:
        self.timeout_s = min(60.0, max(0.05, float(self.timeout_s)))
        self.retries = min(3, max(0, int(self.retries)))
        self.retry_backoff_s = min(5.0, max(0.0, float(self.retry_backoff_s)))
        self.bucket = _TokenBucket(self.rate_limit_per_s, self.burst)


def _post_json(url: str, payload: dict, headers: dict[str, str],
               timeout_s: float) -> tuple[int | None, str | None]:
    """POST one JSON body; returns ``(status, error)`` and never raises."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "StackFlowOS-observability/1",
    }
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            # Drain a bounded amount so the connection can be reused/closed.
            try:
                response.read(4096)
            except Exception:
                pass
            return int(response.status), None
    except urllib.error.HTTPError as exc:
        try:
            exc.read(1024)
        except Exception:
            pass
        return int(exc.code), f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return None, redact_text(str(exc))[:300] or type(exc).__name__


class NotificationExtension:
    """Extension object implementing the notifications hook set."""

    name = "notifications"

    def __init__(self, app: Any) -> None:
        self.app = app
        self.log = get_logger("observability.notifications")
        self._sinks: list[Sink] = []
        self._default_events: tuple[str, ...] = ()
        self._timeout_s = 5.0
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._running = False
        self._semaphore = asyncio.Semaphore(_MAX_INFLIGHT)
        self._seen: set[tuple[str, int]] = set()
        self._seen_order: deque[tuple[str, int]] = deque()
        self._sent = 0
        self._failed = 0
        self._dropped = 0
        self._last_error: str | None = None
        self._last_error_sink: str | None = None
        self.reload(config_of(app))

    # -- configuration ---------------------------------------------------
    def reload(self, config: Any) -> None:
        """Rebuild sinks from ``config.observability`` (never raises)."""
        observability = getattr(config, "observability", None)
        webhooks = getattr(observability, "webhooks", []) or []
        defaults = _normalise_events(
            getattr(observability, "notify_events", []) or [])
        timeout = safe_float(getattr(observability, "notify_timeout_s", 5.0), 5.0)
        timeout = min(60.0, max(0.05, timeout))
        self._default_events = defaults
        self._timeout_s = timeout
        sinks: list[Sink] = []
        names: set[str] = set()
        problem: str | None = None
        for index, raw in enumerate(webhooks):
            if not isinstance(raw, dict):
                problem = f"webhooks[{index}] is not an object"
                continue
            if raw.get("enabled", True) is False:
                continue
            url = str(raw.get("url") or "").strip()
            if not url:
                problem = f"webhooks[{index}] has no url"
                continue
            scheme = urlsplit(url).scheme.lower()
            if scheme not in ("http", "https"):
                problem = (f"webhooks[{index}] has unsupported scheme "
                           f"{scheme or '(none)'}")
                continue
            if "events" in raw:
                events = _normalise_events(raw.get("events"))
            else:
                events = defaults
            name = str(raw.get("name") or f"webhook-{index + 1}").strip()
            base = name
            suffix = 2
            while name in names:
                name = f"{base}-{suffix}"
                suffix += 1
            names.add(name)
            sink = Sink(
                name=name,
                url=url,
                events=events,
                headers=_clean_headers(raw.get("headers")),
                timeout_s=safe_float(raw.get("timeout_s"), timeout) or timeout,
                retries=safe_int(raw.get("retries"), 1),
                retry_backoff_s=safe_float(raw.get("retry_backoff_s"), 0.5),
                rate_limit_per_s=safe_float(raw.get("rate_limit_per_s"), 5.0),
                burst=safe_int(raw.get("burst"), 5),
            )
            sinks.append(sink)
        self._sinks = sinks
        if problem:
            self._last_error = problem
        # Log only names/counts: URLs and headers can contain credentials.
        self.log.info("notifications: %d sink(s), default events=%s",
                      len(sinks), ",".join(defaults) or "(none)")

    async def on_config(self, config: Any) -> None:
        self.reload(config)

    # -- lifecycle -------------------------------------------------------
    def routes(self) -> list[tuple[str, str, Any]]:
        return [("POST", r"/api/notifications/test", self.handle_test)]

    async def start(self) -> None:
        if self._running:
            return
        # The semaphore is loop-bound on 3.10+; recreate it in the running loop.
        self._semaphore = asyncio.Semaphore(_MAX_INFLIGHT)
        try:
            self._queue = self.app.eventlog.subscribe()
        except Exception:
            self._last_error = "eventlog unavailable"
            self.log.exception("notifications could not subscribe to eventlog")
            return
        if getattr(self._queue, "closed", False):
            self._last_error = "eventlog closed"
            self._queue = None
            return
        self._running = True
        self._task = asyncio.create_task(self._consume(), name="sfo-notifications")

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        pending = list(self._tasks)
        for item in pending:
            item.cancel()
        if pending:
            try:
                await asyncio.gather(*pending, return_exceptions=True)
            except Exception:  # pragma: no cover - gather never raises this way
                pass
        self._tasks.clear()
        if self._queue is not None:
            self.app.eventlog.unsubscribe(self._queue)
            self._queue = None

    def health(self) -> dict:
        return {
            "ok": bool(self._running and not self._last_error),
            "sinks": len(self._sinks),
            "running": self._running,
            "sent": self._sent,
            "failed": self._failed,
            "dropped": self._dropped,
            "queue": self._queue.qsize() if self._queue is not None else 0,
            "last_error": self._last_error,
            "last_error_sink": self._last_error_sink,
        }

    # -- delivery --------------------------------------------------------
    def _payload(self, record: Any) -> dict:
        data = getattr(record, "data", {}) or {}
        return {
            "type": str(getattr(record, "type", "")),
            "seq": safe_int(getattr(record, "seq", 0)),
            "ts": safe_int(getattr(record, "ts", now_ms())),
            "project_id": getattr(record, "project_id", None),
            "source": str(getattr(record, "source", "") or ""),
            "data": redact_structure(data),
            "hub": {"version": app_version()},
        }

    def _mark_seen(self, sink: Sink, seq: int) -> bool:
        key = (sink.name, int(seq))
        if key in self._seen:
            return False
        self._seen.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > _MAX_SEEN:
            old = self._seen_order.popleft()
            self._seen.discard(old)
        return True

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
                self.log.debug("notification subscriber error", exc_info=True)
                await asyncio.sleep(0.05)
                continue
            if getattr(record, "type", "") in META_EVENTS:
                continue
            sinks = [sink for sink in self._sinks if sink.matches(record.type)]
            if not sinks:
                continue
            if len(self._tasks) >= _MAX_PENDING_TASKS:
                self._dropped += len(sinks)
                self.log.warning("notification backlog full; dropped %s event %s",
                                 record.type, getattr(record, "seq", "?"))
                continue
            payload = self._payload(record)
            for sink in sinks:
                if not self._mark_seen(sink, safe_int(getattr(record, "seq", 0))):
                    continue
                task = asyncio.create_task(
                    self._deliver_one(sink, record, payload),
                    name=f"sfo-notify-{sink.name}")
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _deliver_one(self, sink: Sink, record: Any, payload: dict, *,
                           audit: bool = True) -> dict:
        async with self._semaphore:
            assert sink.bucket is not None
            await sink.bucket.acquire()
            attempts = 0
            status: int | None = None
            error: str | None = None
            started = time.monotonic()
            for attempt in range(1, sink.retries + 2):
                attempts = attempt
                status, error = await asyncio.to_thread(
                    _post_json, sink.url, payload, sink.headers, sink.timeout_s)
                if status is not None and 200 <= status < 300:
                    self._sent += 1
                    result = {
                        "sink": sink.name,
                        "ok": True,
                        "status": status,
                        "attempts": attempts,
                        "latency_ms": int((time.monotonic() - started) * 1000),
                        "error": None,
                    }
                    self._last_error = None
                    self._last_error_sink = None
                    if audit:
                        self._audit(record, result)
                    return result
                if attempt <= sink.retries:
                    await asyncio.sleep(sink.retry_backoff_s)
            self._failed += 1
            result = {
                "sink": sink.name,
                "ok": False,
                "status": status,
                "attempts": attempts,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": error or "delivery failed",
            }
            self._last_error = result["error"]
            self._last_error_sink = sink.name
            self.log.warning("webhook sink %s failed: status=%s error=%s",
                             sink.name, status, result["error"])
            if audit:
                self._audit(record, result)
            return result

    def _audit(self, record: Any, result: dict) -> None:
        """Record the final delivery outcome without exposing secrets."""
        try:
            self.app.eventlog.append(
                "approval.notified",
                {
                    "sink": result["sink"],
                    "event_type": str(getattr(record, "type", "")),
                    "event_seq": safe_int(getattr(record, "seq", 0)),
                    "ok": bool(result["ok"]),
                    "status": result["status"],
                    "attempts": result["attempts"],
                    "error": result["error"],
                },
                source="observability",
                project_id=getattr(record, "project_id", None),
            )
        except Exception:
            self.log.debug("could not record approval.notified", exc_info=True)

    # -- test route ------------------------------------------------------
    @staticmethod
    def _require_operator() -> None:
        """Only an operator may send a notification test.

        The role lives on the admission decision once the multi-user work
        lands; until then the process launch token is an operator credential.
        An explicitly non-operator role is always refused (fail closed).
        """
        from ..web.api import current_auth

        decision = current_auth()
        if decision is None:  # in-process call (tests/CLI): authenticated caller
            return
        role = getattr(decision, "role", None) or "operator"
        if role != "operator":
            raise Denied("notification test requires the operator role")

    def _test_payload(self, message: str = "") -> dict:
        return {
            "type": "notifications.test",
            "seq": 0,
            "ts": now_ms(),
            "project_id": None,
            "source": "observability",
            "data": redact_structure({
                "message": message or "StackFlowOS notification test",
                "test": True,
            }),
            "hub": {"version": app_version()},
        }

    async def send_test(self, *, sink_name: str | None = None,
                        message: str = "") -> list[dict]:
        """Send a synthetic event to all (or one) sink; returns raw results."""
        sinks = [sink for sink in self._sinks
                 if sink_name is None or sink.name == sink_name]
        if not sinks:
            return []
        record = types.SimpleNamespace(
            type="notifications.test", seq=0, ts=now_ms(),
            project_id=None, data={"test": True}, source="observability")
        payload = self._test_payload(message)
        results = await asyncio.gather(*(
            self._deliver_one(sink, record, payload, audit=True)
            for sink in sinks), return_exceptions=True)
        out: list[dict] = []
        for sink, result in zip(sinks, results):
            if isinstance(result, BaseException):
                out.append({"sink": sink.name, "ok": False, "status": None,
                            "attempts": 0, "latency_ms": 0,
                            "error": redact_text(str(result))[:300]}
                           )
            else:
                out.append(result)
        return out

    async def handle_test(self, match: Any, query: dict, body: dict):
        self._require_operator()
        sink_name = body.get("sink") or query.get("sink") or None
        message = str(body.get("message") or query.get("message") or "")
        results = await self.send_test(
            sink_name=str(sink_name) if sink_name else None, message=message)
        return 200, {
            "ok": True,
            "data": {
                "results": results,
                "sinks": len(self._sinks),
                "sent_to": len(results),
                "tested_at": now_ms(),
            },
        }


def build(app: Any) -> NotificationExtension:
    """Extension entry point used by :mod:`stackflowos.extensions`."""
    return NotificationExtension(app)


__all__ = ["NotificationExtension", "Sink", "build"]

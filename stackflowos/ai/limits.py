"""Client-side LLM guardrails: concurrency, RPM/TPM, budgets and breakers.

Everything here is stdlib and event-loop friendly.  The hub is the only caller
for hub-side chat, but worker processes build the same providers, so the
limiter degrades gracefully when no loop is running (tests call the sync
snapshot methods directly).

Semantics:

* local RPM/TPM violations raise :class:`RateLimited` (``UNAVAILABLE``/429
  semantics, retryable);
* a project token budget violation raises
  :class:`stackflowos.ai.telemetry.BudgetExceeded`, which callers must treat as
  terminal (fail closed, never fall back to another paid model);
* a breaker opens after N consecutive transient failures and moves to
  half-open after the cooldown so one probe can close it again.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from ..errors import Unavailable

#: Sliding window for RPM/TPM accounting.
WINDOW_S = 60.0


class RateLimited(Unavailable):
    """Local client-side rate limit hit (not an upstream 429)."""

    code = "UNAVAILABLE"
    http_status = 429

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s
        self.local = True


class RateLimiter:
    """Semaphore + sliding-window RPM/TPM limiter for one provider."""

    def __init__(self, *, rpm: int = 0, tpm: int = 0,
                 max_concurrency: int = 8,
                 acquire_timeout_s: float = 60.0) -> None:
        self.rpm = max(0, int(rpm or 0))
        self.tpm = max(0, int(tpm or 0))
        self.max_concurrency = max(1, int(max_concurrency or 1))
        self.acquire_timeout_s = max(0.1, float(acquire_timeout_s or 60.0))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sem: asyncio.Semaphore | None = None
        self._lock: asyncio.Lock | None = None
        self._rpm_events: deque[float] = deque()
        self._tpm_events: deque[list[float]] = deque()
        self._last_retry_after = 1.0

    # -- loop-bound state -------------------------------------------------
    def _state(self) -> tuple[asyncio.Semaphore, asyncio.Lock]:
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._sem is None or self._lock is None:
            self._loop = loop
            self._sem = asyncio.Semaphore(self.max_concurrency)
            self._lock = asyncio.Lock()
            self._rpm_events.clear()
            self._tpm_events.clear()
        return self._sem, self._lock

    @staticmethod
    def _prune(now: float, events: deque) -> None:
        while events:
            first = events[0]
            timestamp = first[0] if isinstance(first, list) else first
            if now - timestamp <= WINDOW_S:
                break
            events.popleft()

    async def acquire(self, estimated_tokens: int = 0) -> dict:
        """Acquire one slot.  Returns a ticket for :meth:`release`."""
        sem, lock = self._state()
        try:
            await asyncio.wait_for(sem.acquire(),
                                   timeout=self.acquire_timeout_s)
        except asyncio.TimeoutError as exc:
            raise RateLimited(
                "LLM HTTP 429: local concurrency limit reached "
                f"({self.max_concurrency})") from exc
        tokens = max(0, int(estimated_tokens or 0))
        now = time.monotonic()
        async with lock:
            self._prune(now, self._rpm_events)
            self._prune(now, self._tpm_events)
            if self.rpm and len(self._rpm_events) >= self.rpm:
                wait = max(0.0, WINDOW_S - (now - self._rpm_events[0]))
                self._last_retry_after = wait
                sem.release()
                raise RateLimited(
                    f"LLM HTTP 429: local rpm limit {self.rpm}/min exceeded",
                    retry_after_s=wait)
            used = sum(int(item[1]) for item in self._tpm_events)
            if self.tpm and used + tokens > self.tpm:
                wait = (max(0.0, WINDOW_S - (now - self._tpm_events[0][0]))
                        if self._tpm_events else WINDOW_S)
                self._last_retry_after = wait
                sem.release()
                raise RateLimited(
                    f"LLM HTTP 429: local tpm limit {self.tpm}/min exceeded "
                    f"({used}+{tokens})",
                    retry_after_s=wait)
            self._rpm_events.append(now)
            ticket = {"ts": now, "estimated": tokens, "entry": None}
            if tokens:
                entry = [now, float(tokens)]
                self._tpm_events.append(entry)
                ticket["entry"] = entry
            return ticket

    def release(self, ticket: dict | None = None,
                actual_tokens: int | None = None) -> None:
        """Release the semaphore after a call, adjusting TPM usage."""
        sem = self._sem
        if sem is not None:
            sem.release()
        if ticket is None or actual_tokens is None:
            return
        entry = ticket.get("entry")
        if isinstance(entry, list) and actual_tokens:
            # TPM is a safety limit, so only ever raise the recorded value.
            entry[1] = max(float(entry[1]), float(actual_tokens))

    def snapshot(self) -> dict:
        now = time.monotonic()
        self._prune(now, self._rpm_events)
        self._prune(now, self._tpm_events)
        return {
            "rpm_limit": self.rpm,
            "rpm_used": len(self._rpm_events),
            "tpm_limit": self.tpm,
            "tpm_used": int(sum(int(item[1]) for item in self._tpm_events)),
            "max_concurrency": self.max_concurrency,
            "available_slots": (self._sem._value if self._sem is not None
                                else self.max_concurrency),
            "retry_after_s": round(self._last_retry_after, 3),
        }


class CircuitBreaker:
    """Consecutive-failure breaker: closed -> open -> half-open -> closed."""

    def __init__(self, *, threshold: int = 3,
                 cooldown_s: float = 30.0) -> None:
        self.threshold = max(1, int(threshold or 1))
        self.cooldown_s = max(0.0, float(cooldown_s if cooldown_s is not None else 30.0))
        self._failures = 0
        self._state = "closed"
        self._opened_at = 0.0
        self._half_open = False
        self._last_error = ""

    @property
    def state(self) -> str:
        return self._state

    def available(self) -> bool:
        """Non-mutating check used by the router to skip open providers."""
        if self._state == "closed":
            return True
        if self._state == "open":
            return time.monotonic() - self._opened_at >= self.cooldown_s
        return False

    def allow(self) -> bool:
        if self._state == "closed":
            return True
        if self._state == "open":
            if time.monotonic() - self._opened_at >= self.cooldown_s:
                self._state = "half_open"
                self._half_open = True
                return True
            return False
        # half_open: exactly one probe is allowed at a time.
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._state = "closed"
        self._half_open = False
        self._last_error = ""

    def record_failure(self, error: str = "") -> None:
        self._last_error = str(error or "")[:300]
        if self._state == "half_open":
            self._state = "open"
            self._opened_at = time.monotonic()
            self._half_open = False
            return
        self._failures += 1
        if self._failures >= self.threshold:
            self._state = "open"
            self._opened_at = time.monotonic()
            self._half_open = False

    def reset(self) -> None:
        self._failures = 0
        self._state = "closed"
        self._opened_at = 0.0
        self._half_open = False
        self._last_error = ""

    def snapshot(self) -> dict:
        retry_after = 0.0
        if self._state == "open":
            retry_after = max(0.0, self.cooldown_s
                              - (time.monotonic() - self._opened_at))
        return {
            "state": self._state,
            "failures": self._failures,
            "threshold": self.threshold,
            "cooldown_s": self.cooldown_s,
            "retry_after_s": round(retry_after, 3),
            "last_error": self._last_error,
        }


def is_local_limit(error: BaseException) -> bool:
    return isinstance(error, RateLimited)


def breaker_snapshot(value: Any) -> dict:
    if isinstance(value, CircuitBreaker):
        return value.snapshot()
    return {}


__all__ = [
    "CircuitBreaker",
    "RateLimited",
    "RateLimiter",
    "WINDOW_S",
    "breaker_snapshot",
    "is_local_limit",
]

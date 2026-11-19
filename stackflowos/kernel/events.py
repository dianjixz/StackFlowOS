"""Cordis-style typed event bus.

Semantics are intentionally close to Cordis 4 (see
``vendor/cordis/src/events.ts`` in DeepSeek Harness):

* ``emit``      — synchronous fan-out, listener failures are isolated.
* ``parallel``  — every listener runs concurrently, all settle, errors aggregate.
* ``serial``    — listeners await in order until one "bails" (returns non-null
                  and non-false); the bail value is returned.
* ``bail``      — synchronous variant of ``serial``.
* ``waterfall`` — listeners compose around ``next()``; not calling ``next()``
                  vetoes the rest of the chain.

Every dispatch also notifies "any" observers, which is how the event log and
the UI stream see the whole system without subscribing to each event name.
Observers are notified inline and must not raise. They must not emit unbounded
events themselves or they will recurse; the event log avoids that by
publishing to its own subscriber list.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable

from ..util.logging import get_logger

Listener = Callable[..., Any]


def is_bailed(value: Any) -> bool:
    """Cordis bail predicate: null and false do not stop a chain."""
    return value is not None and value is not False


@dataclass(slots=True)
class Hook:
    callback: Listener
    ctx: Any = None
    prepend: bool = False


class EventBus:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._hooks: dict[str, list[Hook]] = {}
        self._any: list[Hook] = []
        self.log = logger or get_logger("events")

    # -- registration -----------------------------------------------------
    def on(self, name: str, callback: Listener, *, prepend: bool = False,
           ctx: Any = None) -> Callable[[], None]:
        hook = Hook(callback, ctx, prepend)
        bucket = self._hooks.setdefault(name, [])
        if prepend:
            bucket.insert(0, hook)
        else:
            bucket.append(hook)
        removed = False

        def dispose() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            try:
                bucket.remove(hook)
            except ValueError:
                pass

        return dispose

    def on_any(self, callback: Listener, *, ctx: Any = None) -> Callable[[], None]:
        hook = Hook(callback, ctx)
        self._any.append(hook)
        removed = False

        def dispose() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            try:
                self._any.remove(hook)
            except ValueError:
                pass

        return dispose

    def listener_count(self, name: str) -> int:
        return len(self._hooks.get(name, ()))

    def names(self) -> list[str]:
        return sorted(n for n, hooks in self._hooks.items() if hooks)

    # -- dispatch ---------------------------------------------------------
    def _callbacks(self, name: str) -> list[Listener]:
        return [h.callback for h in self._hooks.get(name, ())]

    def _notify_any(self, mode: str, name: str, payload: Any) -> None:
        for hook in list(self._any):
            try:
                result = hook.callback(mode, name, payload)
                if inspect.isawaitable(result):
                    self._spawn(result)
            except Exception:  # observers must never break dispatch
                self.log.exception("on_any observer failed for %s", name)

    def emit(self, name: str, payload: Any = None) -> None:
        self._notify_any("emit", name, payload)
        for callback in self._callbacks(name):
            try:
                result = callback(payload)
                if inspect.isawaitable(result):
                    self.log.warning(
                        "async listener on emit(%s) scheduled as a task; use parallel()", name)
                    self._spawn(result)
            except Exception:
                self.log.exception("listener failed for event %s", name)

    async def parallel(self, name: str, payload: Any = None) -> list[Any]:
        self._notify_any("parallel", name, payload)
        callbacks = self._callbacks(name)
        if not callbacks:
            return []
        results = await asyncio.gather(
            *(self._invoke(cb, payload, name) for cb in callbacks),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise ExceptionGroup(
                f"one or more listeners of event {name!r} failed", errors)
        return results

    async def serial(self, name: str, payload: Any = None) -> Any:
        self._notify_any("serial", name, payload)
        for callback in self._callbacks(name):
            result = await self._invoke(callback, payload, name)
            if is_bailed(result):
                return result
        return None

    def bail(self, name: str, payload: Any = None) -> Any:
        self._notify_any("bail", name, payload)
        for callback in self._callbacks(name):
            try:
                result = callback(payload)
            except Exception:
                self.log.exception("bail listener failed for event %s", name)
                continue
            if inspect.isawaitable(result):
                raise TypeError(f"bail listener for {name!r} returned an awaitable")
            if is_bailed(result):
                return result
        return None

    async def waterfall(self, name: str, payload: Any = None,
                        inner: Callable[[], Any] | None = None) -> Any:
        self._notify_any("waterfall", name, payload)
        callbacks = list(self._callbacks(name))
        depth = 0

        async def dispatch(index: int) -> Any:
            nonlocal depth
            if index >= len(callbacks):
                if inner is None:
                    return None
                result = inner()
                return await result if inspect.isawaitable(result) else result
            callback = callbacks[index]
            if depth > 100:
                raise RecursionError("waterfall next() recursion")
            called = False

            async def next_() -> Any:
                nonlocal depth, called
                if called:
                    raise RuntimeError("next() called multiple times")
                called = True
                depth += 1
                try:
                    return await dispatch(index + 1)
                finally:
                    depth -= 1

            result = callback(payload, next_)
            return await result if inspect.isawaitable(result) else result

        return await dispatch(0)

    # -- helpers ----------------------------------------------------------
    async def _invoke(self, callback: Listener, payload: Any, name: str) -> Any:
        try:
            result = callback(payload)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception as exc:
            self.log.exception("listener failed for event %s", name)
            raise exc

    def _spawn(self, awaitable: Any) -> None:
        try:
            asyncio.get_running_loop().create_task(self._await_quietly(awaitable))
        except RuntimeError:
            # No loop: run best-effort in a fresh one. This only happens in
            # synchronous scripts/tests that misuse emit with async listeners.
            asyncio.run(self._await_quietly(awaitable))

    async def _await_quietly(self, awaitable: Any) -> None:
        try:
            await awaitable
        except Exception:
            self.log.exception("async listener failed")

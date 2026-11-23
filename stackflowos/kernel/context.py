"""Cordis-style dependency container and plugin lifecycle.

A ``Context`` is a tree. Children inherit services from their parents and can
override or isolate them. Every registration below a context is owned by it:

* ``provide``  — a named service, removed when the context is disposed;
* ``effect``   — a setup function returning a disposer, run in reverse order;
* ``on``       — an event listener, removed with the context;
* ``plugin``   — a child context whose disposal tears down everything it set up.

That "everything registered comes with a disposer" property is the safety belt
from docs/vision.md §5: it is what lets an agent try things without leaving the
system in a half-configured state.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable

from ..util.logging import get_logger
from .events import EventBus

_MISSING = object()
Disposer = Callable[[], Any]

#: How long ``dispose`` waits for in-flight plugin startups before cancelling.
#: A plugin that takes longer is considered stuck; disposal must still finish.
_STARTUP_TIMEOUT_S = 5.0
#: Grace period granted to a cancelled startup task to unwind.
_STARTUP_CANCEL_GRACE_S = 1.0


class PluginHandle:
    """Handle for a plugin mounted on a child context."""

    def __init__(self, ctx: "Context", name: str) -> None:
        self.ctx = ctx
        self.name = name
        self.state = "pending"
        self.error: BaseException | None = None
        self._task: asyncio.Task | None = None

    async def ready(self) -> "PluginHandle":
        if self._task is not None:
            await self._task
        if self.error is not None:
            raise self.error
        return self

    async def dispose(self) -> None:
        await self.ctx.dispose()

    def __repr__(self) -> str:
        return f"<PluginHandle {self.name} state={self.state}>"


class Context:
    def __init__(self, parent: "Context | None" = None, name: str = "root",
                 *, bus: EventBus | None = None,
                 logger: logging.Logger | None = None) -> None:
        self.parent = parent
        self.name = name
        self._services: dict[str, Any] = {}
        self._disposers: list[Any] = []
        self._children: list["Context"] = []
        self._isolated: set[str] = set()
        self._service_target: "Context | None" = None
        self._disposed = False
        # Plugin startup tasks mounted on this context. ``dispose`` settles
        # them before tearing anything down so a late ``run`` cannot register
        # effects after the disposer list has been cleared.
        self._start_tasks: set[asyncio.Task] = set()
        # Serialises the "check disposed/append disposer" window against the
        # "snapshot and clear disposers" window in ``dispose``. Both critical
        # sections are synchronous; the lock is never held across an await.
        self._lifecycle_lock = asyncio.Lock()
        # The handle of the plugin this context was created for, if any.
        self._plugin_handle: PluginHandle | None = None
        self._bus = bus or (parent._bus if parent else EventBus())
        self._logger = logger or (
            parent._logger.getChild(_safe_name(name)) if parent else get_logger(name))

    # -- identity ---------------------------------------------------------
    @property
    def bus(self) -> EventBus:
        return self._bus

    @property
    def root(self) -> "Context":
        ctx = self
        while ctx.parent is not None:
            ctx = ctx.parent
        return ctx

    @property
    def path(self) -> str:
        parts: list[str] = []
        ctx: Context | None = self
        while ctx is not None and ctx.parent is not None:
            parts.append(ctx.name)
            ctx = ctx.parent
        return "/".join(reversed(parts)) or "root"

    @property
    def log(self) -> logging.Logger:
        return self._logger

    @property
    def disposed(self) -> bool:
        return self._disposed

    def __repr__(self) -> str:
        return f"<Context {self.path} services={sorted(self._services)}>"

    # -- services ---------------------------------------------------------
    def provide(self, name: str, value: Any) -> Disposer:
        """Register ``value`` as service ``name`` on this context.

        A plugin context publishes its services on the context that mounted
        the plugin (Cordis semantics: the plugin receives the mounting
        context, so siblings can see its services), while the registration is
        still torn down with the plugin.
        """
        self._assert_alive()
        target = self._service_target or self
        if name in target._services:
            raise RuntimeError(f"service {name!r} already provided in {self.path}")
        target._services[name] = value

        def remove() -> None:
            if target._services.get(name) is value:
                del target._services[name]

        return self._collect(remove)

    def get(self, name: str, default: Any = _MISSING) -> Any:
        ctx: Context | None = self
        while ctx is not None:
            if name in ctx._services:
                return ctx._services[name]
            if name in ctx._isolated:
                break
            ctx = ctx.parent
        if default is not _MISSING:
            return default
        raise KeyError(f"service {name!r} is not provided (from {self.path})")

    def require(self, *names: str) -> tuple[Any, ...]:
        return tuple(self.get(name) for name in names)

    def has(self, name: str) -> bool:
        try:
            self.get(name)
            return True
        except KeyError:
            return False

    def services(self) -> dict[str, Any]:
        return dict(self._services)

    # -- lifecycle --------------------------------------------------------
    def _collect(self, disposer: Any) -> Any:
        """Record a disposer on this context and return an idempotent version."""
        if disposer is None:
            def noop() -> None:
                return None
            return noop
        self._disposers.append(disposer)
        called = False

        def dispose() -> None:
            nonlocal called
            if called:
                return
            called = True
            try:
                self._disposers.remove(disposer)
            except ValueError:
                return
            _run_sync_disposer(disposer, self.log)

        return dispose

    def effect(self, execute: Callable[[], Any],
               label: str = "anonymous") -> Callable[[], Any]:
        """Run ``execute`` now; its disposers are torn down in reverse order."""
        self._assert_alive()
        try:
            produced = execute()
        except Exception:
            self.log.exception("effect %s failed during setup", label)
            raise
        collected: list[Any] = []

        def collect(target: Any) -> None:
            if target is None:
                return
            if isinstance(target, (list, tuple, set)):
                for item in target:
                    collect(item)
                return
            if not callable(target) and not _has_close(target):
                raise TypeError(f"effect {label!r} returned an invalid disposer: {target!r}")
            collected.append(target)

        collect(produced)
        for target in collected:
            self._disposers.append(target)

        def dispose() -> None:
            self._assert_alive(soft=True)
            for target in reversed(collected):
                try:
                    self._disposers.remove(target)
                except ValueError:
                    continue
                _run_sync_disposer(target, self.log)

        return dispose

    def child(self, name: str) -> "Context":
        self._assert_alive()
        ctx = Context(self, name, bus=self._bus)
        self._children.append(ctx)
        return ctx

    def fork(self, name: str) -> "Context":
        return self.child(name)

    def isolate(self, name: str) -> "Context":
        """Child context where ``name`` cannot fall through to the parent."""
        ctx = self.child(f"{self.name}:isolate({name})")
        ctx._isolated.add(name)
        return ctx

    def plugin(self, plugin: Any, config: dict | None = None) -> PluginHandle:
        """Mount a plugin (function, class, or object with ``apply``)."""
        self._assert_alive()
        name = getattr(plugin, "__name__", None) or getattr(plugin, "name", None) \
            or type(plugin).__name__
        child = self.child(str(name))
        # Services a plugin provides are published on the mounting context so
        # that siblings (and the mounting context itself) can resolve them.
        child._service_target = self
        handle = PluginHandle(child, child.name)
        child._plugin_handle = handle

        async def run() -> None:
            try:
                if isinstance(plugin, type):
                    target = plugin(child, config if config is not None else {})
                elif hasattr(plugin, "apply"):
                    target = plugin.apply(child, config if config is not None else {})
                else:
                    target = plugin(child, config if config is not None else {})
                target = await _maybe_await(target)
                if target is not None:
                    # Register and check under the lifecycle lock: if dispose
                    # won the race while this plugin was awaiting, the effect
                    # must be torn down here instead of being appended to a
                    # list that has already been drained.
                    late = False
                    async with child._lifecycle_lock:
                        if child._disposed:
                            late = True
                        else:
                            child._disposers.append(target)
                            if child._disposed:
                                # dispose() slipped in between append and this
                                # check; undo the registration and clean up.
                                try:
                                    child._disposers.remove(target)
                                except ValueError:
                                    pass
                                late = True
                    if late:
                        try:
                            await _run_disposer(target)
                        except Exception:
                            child.log.exception(
                                "plugin %s disposer failed during startup cleanup", name)
                handle.state = "disposed" if child._disposed else "active"
            except asyncio.CancelledError:
                # dispose() settled/cancelled this in-flight startup.
                handle.state = "disposed"
                raise
            except Exception as exc:
                handle.state = "failed"
                handle.error = exc
                child.log.exception("plugin %s failed to start", name)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Synchronous callers (tests, CLI setup) get a sync start path.
            try:
                if isinstance(plugin, type):
                    target = plugin(child, config if config is not None else {})
                elif hasattr(plugin, "apply"):
                    target = plugin.apply(child, config if config is not None else {})
                else:
                    target = plugin(child, config if config is not None else {})
                if inspect.isawaitable(target):
                    raise RuntimeError(
                        "async plugin needs a running event loop; use await handle.ready()")
                if target is not None:
                    child._disposers.append(target)
                handle.state = "active"
            except Exception as exc:
                handle.state = "failed"
                handle.error = exc
                child.log.exception("plugin %s failed to start", name)
        else:
            handle._task = loop.create_task(run())
            # Track the startup task on both the mounting context and the
            # plugin context: either one can be disposed first while the
            # plugin is still awaiting.
            for owner in (self, child):
                owner._start_tasks.add(handle._task)
                handle._task.add_done_callback(owner._start_tasks.discard)
        return handle

    async def dispose(self) -> None:
        if self._disposed:
            return
        # Mark disposed first: a startup that returns after this point must
        # clean up its own effect instead of registering it.
        self._disposed = True
        await self._settle_start_tasks()
        for child in reversed(self._children):
            try:
                await child.dispose()
            except Exception:
                self.log.exception("error disposing child %s", child.path)
        self._children.clear()
        # Snapshot and clear atomically, then run disposers without the lock
        # (they may await and may themselves touch the context).
        async with self._lifecycle_lock:
            disposers = list(reversed(self._disposers))
            self._disposers.clear()
        for disposer in disposers:
            try:
                await _run_disposer(disposer)
            except Exception:
                self.log.exception("error running disposer in %s", self.path)
        self._services.clear()
        if self._plugin_handle is not None and \
                self._plugin_handle.state in ("pending", "active"):
            self._plugin_handle.state = "disposed"

    async def _settle_start_tasks(self) -> None:
        """Wait for (and if needed cancel) plugin startups mounted here."""
        current = asyncio.current_task()
        tasks = [task for task in self._start_tasks
                 if task is not current and not task.done()]
        if not tasks:
            return
        try:
            done, pending = await asyncio.wait(tasks, timeout=_STARTUP_TIMEOUT_S)
            for task in done:
                _consume_task_result(task)
            if pending:
                self.log.warning(
                    "plugin startup still running after %.1fs during dispose of %s; cancelling",
                    _STARTUP_TIMEOUT_S, self.path)
                for task in pending:
                    task.cancel()
                done, pending = await asyncio.wait(
                    pending, timeout=_STARTUP_CANCEL_GRACE_S)
                for task in done:
                    _consume_task_result(task)
                for task in pending:
                    self.log.warning(
                        "plugin startup ignored cancellation during dispose of %s",
                        self.path)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            raise
        finally:
            for task in tasks:
                self._start_tasks.discard(task)

    async def __aenter__(self) -> "Context":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.dispose()

    async def close(self) -> None:
        await self.dispose()

    def _assert_alive(self, soft: bool = False) -> None:
        if not self._disposed:
            return
        if soft:
            return
        raise RuntimeError(f"context {self.path!r} is already disposed")

    # -- events -----------------------------------------------------------
    def on(self, name: str, callback: Callable[..., Any], *,
           prepend: bool = False) -> Disposer:
        self._assert_alive()
        disposer = self._bus.on(name, callback, prepend=prepend, ctx=self)
        return self._collect(disposer)

    def on_any(self, callback: Callable[..., Any]) -> Disposer:
        self._assert_alive()
        disposer = self._bus.on_any(callback, ctx=self)
        return self._collect(disposer)

    def emit(self, name: str, payload: Any = None) -> None:
        self._bus.emit(name, payload)

    async def parallel(self, name: str, payload: Any = None) -> list[Any]:
        return await self._bus.parallel(name, payload)

    async def serial(self, name: str, payload: Any = None) -> Any:
        return await self._bus.serial(name, payload)

    def bail(self, name: str, payload: Any = None) -> Any:
        return self._bus.bail(name, payload)

    async def waterfall(self, name: str, payload: Any = None,
                        inner: Callable[[], Any] | None = None) -> Any:
        return await self._bus.waterfall(name, payload, inner)


# ---------------------------------------------------------------------------
# disposer plumbing
# ---------------------------------------------------------------------------

def _has_close(target: Any) -> bool:
    return any(callable(getattr(target, attr, None))
               for attr in ("aclose", "close", "dispose", "stop"))


def _close_of(target: Any) -> Callable[[], Any]:
    for attr in ("aclose", "close", "dispose", "stop"):
        fn = getattr(target, attr, None)
        if callable(fn):
            return fn
    raise TypeError(f"object {target!r} has no close method")


async def _run_disposer(disposer: Any) -> Any:
    """Invoke a disposer (callable or close()-able) and await its result."""
    result = disposer() if callable(disposer) else _close_of(disposer)()
    if inspect.isawaitable(result):
        return await result
    return result


def _consume_task_result(task: asyncio.Task) -> None:
    """Retrieve a startup task's outcome so asyncio does not log it later."""
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


def _run_sync_disposer(disposer: Any, log: logging.Logger) -> None:
    try:
        if callable(disposer):
            result = disposer()
        elif _has_close(disposer):
            result = _close_of(disposer)()
        else:
            return
        if inspect.isawaitable(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(result)
            else:
                loop.create_task(_await_quietly(result, log))
    except Exception:
        log.exception("disposer failed")


async def _await_quietly(awaitable: Any, log: logging.Logger) -> None:
    try:
        await awaitable
    except Exception:
        log.exception("async disposer failed")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _safe_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)

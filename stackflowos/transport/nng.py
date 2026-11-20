"""pynng-based transport (L0) of StackFlowOS.

Three primitives carry the entire control and event plane:

* :class:`RpcServer` / :class:`RpcClient` -- REQ/REP request-reply over IPC or
  TCP, one :class:`~stackflowos.protocol.Envelope` per message.
* :class:`EventPublisher` / :class:`EventSubscriber` -- PUB/SUB event stream.
* :class:`HubTransport` / :class:`UnitTransport` -- the two sides wired
  together according to the topology in ``docs/architecture.md`` §2:
  the hub listens for RPC and events; each unit dials the hub.

Design notes
------------
* No threads.  Every socket is created, used and closed from the asyncio
  loop; pynng's ``arecv``/``asend`` are used throughout.
* REP sockets are multiplexed through ``contexts`` concurrent contexts so a
  slow handler cannot serialise or block unrelated calls.
* A REQ context that timed out is unusable (its protocol state is stuck
  waiting for a reply), so the client drops it, creates a fresh one and
  retries the call exactly once -- reusing the same envelope id, which lets
  an idempotent server deduplicate the retry.
* Decoding failures cannot be answered reliably (there is no trustworthy
  request id), so they are logged and the frame is skipped.
* All socket closes go through :func:`_safe_close`: pynng raises from
  ``Context.__del__`` when a socket was closed under it, which must never
  turn into an unraisable exception in normal shutdown.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

import pynng

# pynng 0.9's Context.__del__ calls close() again after the owner already
# closed it, which prints "Exception ignored ... Closed" at shutdown. The
# close is harmless; silence the noise without changing socket semantics.
_orig_ctx_del = getattr(pynng.Context, "__del__", None)
if _orig_ctx_del is not None:
    def _quiet_ctx_del(self):  # pragma: no cover - shutdown noise only
        try:
            _orig_ctx_del(self)
        except Exception:
            pass
    try:
        pynng.Context.__del__ = _quiet_ctx_del
    except Exception:
        pass

from ..errors import SfoError, Timeout, Unavailable, error_from_dict
from ..protocol import (
    KIND_EVENT,
    KIND_REPLY,
    KIND_RPC,
    Envelope,
    ErrorCode,
    EventRecord,
)
from ..util.logging import get_logger
from .address import UnitAddresses

#: A handler receives the request envelope and returns reply data.  Both
#: synchronous and asynchronous handlers are supported.
ActionHandler = Callable[[Envelope], "dict | None | Awaitable[dict | None]"]
#: Event callbacks receive a fully decoded :class:`EventRecord`.
EventCallback = Callable[[EventRecord], "None | Awaitable[None]"]

_IPC_PREFIX = "ipc://"
_TCP_BIND_ATTEMPTS = 5


def is_ipc(address: str) -> bool:
    """Return ``True`` for an ``ipc://`` transport address."""
    return isinstance(address, str) and address.startswith(_IPC_PREFIX)


def cleanup_ipc_address(address: str) -> None:
    """Remove a stale IPC socket file named by ``address``.

    Safe on missing files and on non-IPC addresses.  Only call this for an
    address you own (a listener), never for an address you merely dial: the
    file belongs to the listening side.
    """
    if not is_ipc(address):
        return
    path = _ipc_path(address)
    try:
        if path.is_dir():
            get_logger("transport").warning(
                "refusing to unlink directory at ipc path %s", path)
            return
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:  # pragma: no cover - unusual filesystem errors
        get_logger("transport").warning(
            "cannot remove ipc socket %s: %s", path, exc)


def _ipc_path(address: str) -> Path:
    return Path(address[len(_IPC_PREFIX):])


def _safe_close(obj: Any) -> None:
    """Close a pynng socket/context, swallowing 'already closed' errors."""
    if obj is None:
        return
    try:
        obj.close()
    except Exception:  # pynng raises Closed for already-closed handles
        pass


def _decode(raw: bytes, log: logging.Logger, label: str) -> Envelope | None:
    try:
        return Envelope.from_bytes(raw)
    except ValueError as exc:
        # Without a trustworthy envelope there is no id to reply to: drop it.
        log.warning("%s: dropping undecodable frame (%d bytes): %s",
                    label, len(raw), exc)
        return None


async def _open_listener(sock_cls: Any, address: str, *, log: logging.Logger,
                         label: str, attempts: int = _TCP_BIND_ATTEMPTS) -> Any:
    """Create a listening socket, handling stale IPC files and busy ports."""
    if is_ipc(address):
        path = _ipc_path(address)
        try:
            if path.exists():
                log.debug("%s: removing stale ipc socket %s", label, path)
                cleanup_ipc_address(address)
        except OSError:  # pragma: no cover - defensive
            pass
        try:
            return sock_cls(listen=address)
        except pynng.AddressInUse as exc:
            # Some platforms leave a file behind even when nothing listens;
            # remove once and retry exactly once, as the contract requires.
            log.warning("%s: %s busy (%s); unlinking and retrying once",
                        label, address, exc)
            cleanup_ipc_address(address)
            try:
                return sock_cls(listen=address)
            except Exception as retry_exc:
                raise Unavailable(
                    f"{label}: cannot listen on {address}: {retry_exc}") from retry_exc
        except Exception as exc:
            raise Unavailable(
                f"{label}: cannot listen on {address}: {exc}") from exc

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return sock_cls(listen=address)
        except pynng.AddressInUse as exc:
            last = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(0.02 * (attempt + 1))
        except Exception as exc:
            raise Unavailable(
                f"{label}: cannot listen on {address}: {exc}") from exc
    raise Unavailable(f"{label}: address {address} is still in use: {last}")


def _reply_payload(env: Envelope, result: Any) -> Envelope:
    if result is None:
        return env.reply({})
    if isinstance(result, dict):
        return env.reply(result)
    # Handlers are typed to return dict|None, but be forgiving in one place.
    return env.reply({"value": result})


def _configure_lane(ctx: pynng.Context, log: logging.Logger) -> None:
    """Give a REP lane a send timeout so a vanished peer cannot wedge it.

    NNG REQ/REP has no request id at the socket level: once a REQ peer
    disconnects, a pending REP send has nobody to deliver to. Without a
    timeout the lane blocks forever and the server slowly runs out of lanes
    (observed after worker processes exit while a heartbeat reply is in
    flight). The timeout lets ``_serve`` recycle the lane.
    """
    try:
        ctx.send_timeout = 5000
    except Exception:  # pragma: no cover - option unsupported
        log.debug("could not set REP lane send_timeout")


class RpcServer:
    """REP server with ``contexts`` concurrent request lanes."""

    def __init__(self, address: str, *, name: str = "rpc", contexts: int = 4,
                 logger: logging.Logger | None = None) -> None:
        if contexts < 1:
            raise ValueError("contexts must be >= 1")
        self._address = address
        self._name = name
        self._contexts = contexts
        self._log = logger or get_logger("transport.rpc")
        self._handlers: dict[str, ActionHandler] = {}
        self._sock: pynng.Rep0 | None = None
        self._ctxs: list[pynng.Context] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False

    @property
    def address(self) -> str:
        return self._address

    @property
    def running(self) -> bool:
        return self._running

    def register(self, action: str, handler: ActionHandler) -> None:
        """Register (or replace) the handler for ``action``."""
        if not isinstance(action, str) or not action:
            raise ValueError("action must be a non-empty string")
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._handlers[action] = handler

    async def start(self) -> None:
        if self._running:
            return
        sock = await _open_listener(pynng.Rep0, self._address,
                                    log=self._log, label=self._name)
        contexts: list[pynng.Context] = []
        tasks: list[asyncio.Task[None]] = []
        try:
            for index in range(self._contexts):
                ctx = sock.new_context()
                _configure_lane(ctx, self._log)
                contexts.append(ctx)
                tasks.append(asyncio.create_task(
                    self._serve(ctx, index),
                    name=f"{self._name}-rpc-{index}"))
        except Exception:
            for ctx in contexts:
                _safe_close(ctx)
            _safe_close(sock)
            raise
        self._sock = sock
        self._ctxs = contexts
        self._tasks = tasks
        self._running = True
        self._log.info("%s listening on %s (%d contexts)",
                       self._name, self._address, self._contexts)

    async def stop(self) -> None:
        if self._sock is None and not self._running:
            return
        self._running = False
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception) and not isinstance(
                        result, asyncio.CancelledError):
                    self._log.warning("%s: request lane ended with %r",
                                      self._name, result)
        ctxs, self._ctxs = self._ctxs, []
        sock, self._sock = self._sock, None
        for ctx in ctxs:
            _safe_close(ctx)
        _safe_close(sock)
        cleanup_ipc_address(self._address)
        self._log.info("%s stopped (%s)", self._name, self._address)

    # -- internals --------------------------------------------------------
    async def _serve(self, ctx: pynng.Context, index: int) -> None:
        label = f"{self._name}[{index}]"
        while self._running:
            try:
                raw = await ctx.arecv()
            except asyncio.CancelledError:
                raise
            except pynng.Closed:
                return
            except Exception as exc:
                if not self._running:
                    return
                self._log.warning("%s: receive failed: %s", label, exc)
                await asyncio.sleep(0.01)  # avoid a hot loop on a broken socket
                continue

            env = _decode(raw, self._log, label)
            if env is None:
                continue
            if env.kind != KIND_RPC:
                self._log.warning("%s: ignoring %s envelope from %s",
                                  label, env.kind, env.src)
                continue

            reply = await self._dispatch(env, label)
            try:
                payload = reply.to_bytes()
            except ValueError as exc:  # oversized reply: keep the lane alive
                self._log.error("%s: reply to %s too large: %s",
                                label, env.action, exc)
                payload = env.reply_error(
                    ErrorCode.INTERNAL.value, "reply too large").to_bytes()
            try:
                await ctx.asend(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    return
                # A REQ peer that disappeared leaves a REP lane waiting for a
                # peer forever (send timeout fires). Retire the wedged lane
                # and open a fresh one so the server keeps serving.
                self._log.info("%s: send failed (%s); recycling lane",
                               label, exc)
                try:
                    new_ctx = self._sock.new_context() if self._sock else None
                except Exception:
                    new_ctx = None
                if new_ctx is None:
                    return
                _configure_lane(new_ctx, self._log)
                _safe_close(ctx)
                ctx = new_ctx
                if index < len(self._ctxs):
                    self._ctxs[index] = new_ctx
                continue

    async def _dispatch(self, env: Envelope, label: str) -> Envelope:
        handler = self._handlers.get(env.action)
        if handler is None:
            self._log.warning("%s: unknown action %r from %s",
                              label, env.action, env.src)
            return env.reply_error(
                ErrorCode.BAD_REQUEST.value,
                f"unknown action: {env.action}")
        try:
            result = handler(env)
            if inspect.isawaitable(result):
                result = await result
            return _reply_payload(env, result)
        except asyncio.CancelledError:
            raise
        except SfoError as exc:
            return env.reply_error(exc.code, exc.message, exc.detail)
        except Exception as exc:
            # Local traceback for operators; only a clean message on the wire.
            self._log.exception("%s: handler %r raised", label, env.action)
            message = str(exc) or type(exc).__name__
            return env.reply_error(ErrorCode.INTERNAL.value, message)


class RpcClient:
    """REQ client with a pooled set of request contexts."""

    def __init__(self, address: str, *, src: str = "sys", dst: str = "sys",
                 timeout: float = 8.0, contexts: int = 4,
                 logger: logging.Logger | None = None) -> None:
        if contexts < 1:
            raise ValueError("contexts must be >= 1")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._address = address
        self._src = src
        self._dst = dst
        self._timeout = float(timeout)
        self._contexts = contexts
        self._log = logger or get_logger("transport.rpc.client")
        self._sock: pynng.Req0 | None = None
        self._queue: asyncio.Queue[pynng.Context] | None = None
        self._live: list[pynng.Context] = []
        self._running = False

    @property
    def address(self) -> str:
        return self._address

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return
        sock = pynng.Req0(dial=self._address, block_on_dial=False)
        contexts: list[pynng.Context] = []
        try:
            for _ in range(self._contexts):
                contexts.append(sock.new_context())
        except Exception:
            for ctx in contexts:
                _safe_close(ctx)
            _safe_close(sock)
            raise
        queue: asyncio.Queue[pynng.Context] = asyncio.Queue(
            maxsize=self._contexts)
        for ctx in contexts:
            queue.put_nowait(ctx)
        self._sock = sock
        self._queue = queue
        self._live = contexts
        self._running = True
        self._log.debug("rpc client for %s started (%d contexts)",
                        self._address, self._contexts)

    async def stop(self) -> None:
        self._close_now()

    def _close_now(self) -> None:
        """Synchronous shutdown, also used when a hub drops a dead unit."""
        if self._sock is None and not self._running:
            return
        self._running = False
        queue, self._queue = self._queue, None
        if queue is not None:
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - race guard
                    break
        live, self._live = self._live, []
        for ctx in live:
            _safe_close(ctx)
        sock, self._sock = self._sock, None
        _safe_close(sock)
        self._log.debug("rpc client for %s stopped", self._address)

    async def call(self, action: str, data: dict | None = None, *,
                   dst: str | None = None, timeout: float | None = None) -> dict:
        """Call ``action`` and return the reply's ``data``.

        Raises the matching :class:`~stackflowos.errors.SfoError` subclass when
        the server replies ``ok=false``; transport failures raise
        :class:`~stackflowos.errors.Timeout` or
        :class:`~stackflowos.errors.Unavailable`.  A timeout or a closed
        connection is retried once with a fresh context and the same envelope
        id (servers may deduplicate by id).
        """
        if not self._running or self._sock is None or self._queue is None:
            raise Unavailable(
                f"rpc client for {self._address} is not started")
        if not isinstance(action, str) or not action:
            raise ValueError("action must be a non-empty string")
        per_timeout = float(self._timeout if timeout is None else timeout)
        if per_timeout <= 0:
            raise Timeout(f"rpc {action}: timeout must be positive")

        env = Envelope.rpc(action, data or {}, src=self._src,
                           dst=dst or self._dst)
        last_error: SfoError | None = None
        for attempt in range(2):
            try:
                reply = await self._exchange(env, per_timeout)
            except asyncio.TimeoutError:
                last_error = Timeout(
                    f"rpc {action} to {self._address} timed out "
                    f"after {per_timeout:.1f}s")
                self._log.warning("%s (attempt %d/2)",
                                  last_error.message, attempt + 1)
                if not self._running:
                    break
                continue
            except (pynng.Closed, pynng.Timeout) as exc:
                last_error = Unavailable(
                    f"rpc {action} to {self._address} failed: {exc}")
                self._log.warning("%s (attempt %d/2)",
                                  last_error.message, attempt + 1)
                if not self._running:
                    break
                continue
            except asyncio.CancelledError:
                raise
            except SfoError:
                raise
            if not reply.ok:
                payload = reply.error if isinstance(reply.error, dict) else {}
                if not payload:
                    payload = {"code": ErrorCode.INTERNAL.value,
                               "message": "server replied ok=false without error"}
                raise error_from_dict(payload)
            return reply.data

        assert last_error is not None
        raise last_error

    async def _exchange(self, env: Envelope, timeout: float) -> Envelope:
        queue = self._queue
        sock = self._sock
        if queue is None or sock is None:
            raise pynng.Closed("rpc client closed")

        # Waiting for a free lane is a timeout, but there is no context to
        # discard yet; the context-bound error handling starts below.
        ctx = await asyncio.wait_for(queue.get(), timeout)
        try:
            reply = await self._roundtrip(ctx, env, timeout)
        except asyncio.TimeoutError:
            self._discard(ctx)
            raise
        except (pynng.Closed, pynng.Timeout, pynng.BadState) as exc:
            self._discard(ctx)
            raise
        except asyncio.CancelledError:
            self._discard(ctx)
            raise
        except Exception as exc:
            self._discard(ctx)
            raise Unavailable(
                f"rpc {env.action} to {self._address} failed: {exc}") from exc
        else:
            self._release(ctx)
            return reply

    async def _roundtrip(self, ctx: pynng.Context, env: Envelope,
                         timeout: float) -> Envelope:
        """Send one request and wait for the reply carrying the same id."""
        await asyncio.wait_for(ctx.asend(env.to_bytes()), timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"no matching reply for {env.id}")
            raw = await asyncio.wait_for(ctx.arecv(), remaining)
            reply = _decode(raw, self._log, f"rpc-client {self._address}")
            if reply is None:
                continue
            if reply.id != env.id:
                # A late reply from an abandoned attempt: ignore it and keep
                # waiting for the one that belongs to this call.
                self._log.warning(
                    "rpc %s: ignoring reply id %s (expected %s)",
                    env.action, reply.id, env.id)
                continue
            if reply.kind != KIND_REPLY:
                self._log.warning(
                    "rpc %s: expected reply envelope, got %s",
                    env.action, reply.kind)
                continue
            return reply

    def _release(self, ctx: pynng.Context) -> None:
        queue = self._queue
        if queue is None or not self._running:
            _safe_close(ctx)
            try:
                self._live.remove(ctx)
            except ValueError:
                pass
            return
        try:
            queue.put_nowait(ctx)
        except asyncio.QueueFull:  # pragma: no cover - pool invariant broken
            _safe_close(ctx)
            try:
                self._live.remove(ctx)
            except ValueError:
                pass

    def _discard(self, ctx: pynng.Context) -> None:
        """Throw away a REQ context whose protocol state is unusable."""
        _safe_close(ctx)
        try:
            self._live.remove(ctx)
        except ValueError:
            pass
        if not self._running or self._sock is None:
            return
        try:
            fresh = self._sock.new_context()
        except Exception as exc:  # pragma: no cover - closing socket race
            self._log.warning("cannot recreate rpc context for %s: %s",
                              self._address, exc)
            return
        self._live.append(fresh)
        queue = self._queue
        if queue is None:
            _safe_close(fresh)
            return
        try:
            queue.put_nowait(fresh)
        except asyncio.QueueFull:  # pragma: no cover - pool invariant broken
            _safe_close(fresh)
            try:
                self._live.remove(fresh)
            except ValueError:
                pass


class EventPublisher:
    """PUB socket that dials the hub's events listener."""

    def __init__(self, address: str, *, src: str = "sys",
                 logger: logging.Logger | None = None) -> None:
        self._address = address
        self._src = src
        self._log = logger or get_logger("transport.events.pub")
        self._sock: pynng.Pub0 | None = None

    @property
    def address(self) -> str:
        return self._address

    async def start(self) -> None:
        if self._sock is not None:
            return
        self._sock = pynng.Pub0(dial=self._address, block_on_dial=False)
        self._log.info("event publisher for %s dialing %s",
                       self._src, self._address)

    def publish(self, record: EventRecord) -> None:
        """Publish one record without blocking; dropped when nobody listens."""
        sock = self._sock
        if sock is None:
            self._log.debug("event publisher for %s not started; dropping %s",
                            self._src, record.type)
            return
        try:
            sock.send(record.to_envelope().to_bytes(), block=False)
        except Exception as exc:  # pragma: no cover - pub rarely raises
            self._log.warning("publish failed for %s: %s", self._address, exc)

    async def stop(self) -> None:
        sock, self._sock = self._sock, None
        _safe_close(sock)
        self._log.debug("event publisher for %s stopped", self._address)


class EventSubscriber:
    """SUB socket that listens for events and fans them out to callbacks."""

    def __init__(self, address: str, *,
                 logger: logging.Logger | None = None) -> None:
        self._address = address
        self._log = logger or get_logger("transport.events.sub")
        self._callbacks: list[EventCallback] = []
        self._sock: pynng.Sub0 | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def address(self) -> str:
        return self._address

    def on_record(self, cb: EventCallback) -> None:
        if not callable(cb):
            raise TypeError("callback must be callable")
        self._callbacks.append(cb)

    async def start(self) -> None:
        if self._running:
            return
        sock = await _open_listener(pynng.Sub0, self._address,
                                    log=self._log, label="events-subscriber")
        try:
            sock.subscribe(b"")
        except Exception:
            _safe_close(sock)
            raise
        self._sock = sock
        self._running = True
        self._task = asyncio.create_task(self._recv_loop(),
                                         name=f"events-sub-{self._address}")
        self._log.info("event subscriber listening on %s", self._address)

    async def stop(self) -> None:
        if self._sock is None and not self._running:
            return
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            results = await asyncio.gather(task, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception) and not isinstance(
                        result, asyncio.CancelledError):
                    self._log.warning("event subscriber ended with %r", result)
        sock, self._sock = self._sock, None
        _safe_close(sock)
        cleanup_ipc_address(self._address)
        self._log.debug("event subscriber stopped (%s)", self._address)

    # -- internals --------------------------------------------------------
    async def _recv_loop(self) -> None:
        sock = self._sock
        if sock is None:  # pragma: no cover - start always sets it
            return
        while self._running:
            try:
                raw = await sock.arecv()
            except asyncio.CancelledError:
                raise
            except pynng.Closed:
                return
            except Exception as exc:
                if not self._running:
                    return
                self._log.warning("event subscriber receive failed: %s", exc)
                await asyncio.sleep(0.01)
                continue
            env = _decode(raw, self._log, "events-subscriber")
            if env is None:
                continue
            if env.kind != KIND_EVENT:
                self._log.warning("event subscriber ignored %s envelope",
                                  env.kind)
                continue
            try:
                record = EventRecord.from_envelope(env)
            except Exception as exc:  # pragma: no cover - defensive
                self._log.warning("cannot build event record: %s", exc)
                continue
            for callback in list(self._callbacks):
                try:
                    result = callback(record)
                    if inspect.isawaitable(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A bad consumer must never break the stream.
                    self._log.exception("event callback %r failed", callback)


class HubTransport:
    """Hub-side transport: serve sys RPC, subscribe to events, call units."""

    def __init__(self, *, sys_addresses: UnitAddresses, timeout: float = 8.0,
                 logger: logging.Logger | None = None) -> None:
        self._sys = sys_addresses
        self._timeout = float(timeout)
        self._log = logger or get_logger("transport.hub")
        self._handlers: dict[str, ActionHandler] = {}
        self._event_callbacks: list[EventCallback] = []
        self._units: dict[str, RpcClient] = {}
        self._server: RpcServer | None = None
        self._subscriber: EventSubscriber | None = None
        self._lock = asyncio.Lock()
        self._running = False

    @property
    def sys_addresses(self) -> UnitAddresses:
        return self._sys

    @property
    def running(self) -> bool:
        """True while the RPC server and event subscriber are both up."""
        return bool(self._running)

    def register_action(self, action: str, handler: ActionHandler) -> None:
        """Register a sys-side RPC action (before or after :meth:`start`)."""
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._handlers[action] = handler
        if self._server is not None:
            self._server.register(action, handler)

    def on_event(self, cb: EventCallback) -> None:
        if not callable(cb):
            raise TypeError("callback must be callable")
        self._event_callbacks.append(cb)

    def add_unit(self, work_id: str, rpc_address: str) -> None:
        """Track a unit's RPC endpoint; the dial starts on the first call."""
        if not work_id or not rpc_address:
            raise ValueError("work_id and rpc_address are required")
        old = self._units.pop(work_id, None)
        if old is not None:
            old._close_now()
        self._units[work_id] = RpcClient(
            rpc_address, src="sys", dst=work_id, timeout=self._timeout,
            logger=self._log)
        self._log.debug("hub tracks unit %s at %s", work_id, rpc_address)

    def remove_unit(self, work_id: str) -> None:
        client = self._units.pop(work_id, None)
        if client is not None:
            client._close_now()
            self._log.debug("hub dropped unit %s", work_id)

    async def start(self) -> None:
        if self._running:
            return
        server = RpcServer(self._sys.rpc, name="sys-rpc", contexts=8,
                           logger=self._log)
        for action, handler in self._handlers.items():
            server.register(action, handler)
        subscriber = EventSubscriber(self._sys.events, logger=self._log)
        subscriber.on_record(self._fanout)
        self._server = server
        self._subscriber = subscriber
        self._running = True
        try:
            await subscriber.start()
            await server.start()
            for client in list(self._units.values()):
                await client.start()
        except Exception:
            self._running = False
            await self.stop()
            raise
        self._log.info("hub transport up: rpc=%s events=%s",
                       self._sys.rpc, self._sys.events)

    async def call(self, work_id: str, action: str, data: dict | None = None, *,
                   timeout: float | None = None) -> dict:
        client = await self._ensure_unit(work_id)
        return await client.call(action, data, dst=work_id, timeout=timeout)

    async def stop(self) -> None:
        if self._server is None and self._subscriber is None and not self._units:
            self._running = False
            return
        self._running = False
        for client in list(self._units.values()):
            client._close_now()
        subscriber, self._subscriber = self._subscriber, None
        if subscriber is not None:
            await subscriber.stop()
        server, self._server = self._server, None
        if server is not None:
            await server.stop()
        self._log.info("hub transport stopped")

    # -- internals --------------------------------------------------------
    async def _ensure_unit(self, work_id: str) -> RpcClient:
        async with self._lock:
            client = self._units.get(work_id)
            if client is None:
                raise Unavailable(f"unknown unit: {work_id}")
            if not client.running:
                await client.start()
            return client

    async def _fanout(self, record: EventRecord) -> None:
        for callback in list(self._event_callbacks):
            try:
                result = callback(record)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log.exception("hub event callback %r failed", callback)


class UnitTransport:
    """Worker-side transport: call the hub, serve own RPC, publish events."""

    def __init__(self, *, sys_rpc: str, work_id: str,
                 addresses: UnitAddresses, events_target: str,
                 logger: logging.Logger | None = None) -> None:
        self._sys_rpc = sys_rpc
        self._work_id = work_id
        self._addresses = addresses
        self._events_target = events_target
        self._log = logger or get_logger("transport.unit")
        self._handlers: dict[str, ActionHandler] = {}
        self._client: RpcClient | None = None
        self._server: RpcServer | None = None
        self._publisher: EventPublisher | None = None
        self._running = False

    @property
    def work_id(self) -> str:
        return self._work_id

    @property
    def addresses(self) -> UnitAddresses:
        return self._addresses

    def register_action(self, action: str, handler: ActionHandler) -> None:
        """Register a unit-side RPC action (before or after :meth:`start`)."""
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._handlers[action] = handler
        if self._server is not None:
            self._server.register(action, handler)

    async def start(self) -> None:
        if self._running:
            return
        client = RpcClient(self._sys_rpc, src=self._work_id, dst="sys",
                           logger=self._log)
        server = RpcServer(self._addresses.rpc, name=f"unit-{self._work_id}",
                           contexts=4, logger=self._log)
        for action, handler in self._handlers.items():
            server.register(action, handler)
        # NOTE: the unit's own events endpoint is reserved for future peer
        # subscriptions; this version only publishes by dialing the hub.
        publisher = EventPublisher(self._events_target, src=self._work_id,
                                   logger=self._log)
        self._client = client
        self._server = server
        self._publisher = publisher
        self._running = True
        try:
            await client.start()
            await server.start()
            await publisher.start()
        except Exception:
            self._running = False
            await self.stop()
            raise
        self._log.info("unit %s transport up: rpc=%s", self._work_id,
                       self._addresses.rpc)

    async def call(self, action: str, data: dict | None = None, *,
                   timeout: float | None = None) -> dict:
        client = self._client
        if client is None or not client.running:
            raise Unavailable(f"unit {self._work_id} transport is not started")
        return await client.call(action, data, dst="sys", timeout=timeout)

    async def call_at(self, address: str, action: str, data: dict | None = None,
                      *, timeout: float | None = None) -> dict:
        """Place a one-off call to another unit's RPC endpoint."""
        client = RpcClient(address, src=self._work_id, dst="sys",
                           logger=self._log)
        await client.start()
        try:
            return await client.call(action, data, timeout=timeout)
        finally:
            await client.stop()

    def publish(self, record: EventRecord) -> None:
        publisher = self._publisher
        if publisher is None:
            self._log.debug("unit %s has no publisher; dropping %s",
                            self._work_id, record.type)
            return
        publisher.publish(record)

    async def stop(self) -> None:
        if not self._running and self._publisher is None:
            return
        self._running = False
        publisher, self._publisher = self._publisher, None
        if publisher is not None:
            await publisher.stop()
        server, self._server = self._server, None
        if server is not None:
            await server.stop()
        client, self._client = self._client, None
        if client is not None:
            await client.stop()
        self._log.info("unit %s transport stopped", self._work_id)

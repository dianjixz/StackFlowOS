"""Optional ZeroMQ <-> pynng bridge (``pyzmq`` is an *optional* dependency).

StackFlowOS speaks JSON :class:`~stackflowos.protocol.Envelope` frames over
pynng (REQ/REP + PUB/SUB).  External tools and edge devices sometimes only
speak ZeroMQ; this module translates between the two without changing the wire
format: the payload stays the exact StackFlowOS envelope JSON, only the socket
library changes.

Three bridge directions are implemented:

``ZmqRpcBridge`` (ZMQ REP -> pynng REQ -> hub)
    Binds a ZMQ ``REP`` endpoint (``--listen``), forwards each request envelope
    to the hub's sys RPC address (``--connect``) and sends the reply envelope
    back.  Handshake/error envelopes keep their ``id`` so the ZMQ client can
    match replies; hub :class:`SfoError` codes are relayed verbatim.

``ZmqEventIngestBridge`` (ZMQ SUB -> pynng PUB)
    Subscribes to foreign ZMQ event publishers and republishes decoded
    :class:`EventRecord` frames as pynng events (``--sub-connect`` ->
    ``--events-connect``).  Note the hub intentionally does not trust raw PUB
    frames for its audit log (X2); this is for peer/edge event fan-out, not for
    injecting audit records into the hub.

``NngEventExportBridge`` (pynng SUB -> ZMQ PUB)
    Binds a pynng ``SUB`` endpoint (``--events-listen``) and forwards every
    received record to a ZMQ ``PUB`` endpoint (``--pub-listen``/``--pub-connect``)
    as raw envelope JSON for ZMQ-only consumers (dashboards, other runtimes).

Installation: ``pip install pyzmq``.  Without it, :func:`available` returns
``False``, the bridge constructors still validate their parameters, and
``start()`` raises :class:`ZmqUnavailable` with a clear message.  Tests in this
repository cover the parameter/fallback path because CI does not install pyzmq.

CLI::

    python -m stackflowos.transport.zmq_bridge \\
        --listen tcp://0.0.0.0:5555 --connect tcp://127.0.0.1:17820
    python -m stackflowos.transport.zmq_bridge \\
        --sub-connect tcp://publisher:5556 --events-connect tcp://127.0.0.1:17821
    python -m stackflowos.transport.zmq_bridge \\
        --events-listen tcp://127.0.0.1:17901 --pub-listen tcp://127.0.0.1:5557
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from typing import Any

from ..errors import SfoError
from ..protocol import KIND_EVENT, KIND_RPC, Envelope, EventRecord
from ..util.logging import get_logger
from .nng import EventPublisher, EventSubscriber, RpcClient

#: Import guard: importing this module must never require pyzmq.
try:  # pragma: no cover - exercised only when pyzmq is installed
    import zmq as _zmq  # type: ignore
    _ZMQ_IMPORT_ERROR: Exception | None = None
except Exception as _exc:  # pragma: no cover - the common CI path
    _zmq = None
    _ZMQ_IMPORT_ERROR = _exc


class ZmqUnavailable(RuntimeError):
    """Raised when the ZMQ bridge is used without ``pyzmq`` installed."""


def available() -> bool:
    """Whether the optional ``pyzmq`` dependency is importable."""
    return _zmq is not None


def require_zmq() -> Any:
    """Return the ``zmq`` module or raise :class:`ZmqUnavailable`."""
    if _zmq is None:
        raise ZmqUnavailable(
            "pyzmq is not installed: the ZMQ bridge is an optional feature; "
            "install it with `pip install pyzmq` (detail: "
            f"{_ZMQ_IMPORT_ERROR})")
    return _zmq


def _zmq_asyncio() -> Any:
    require_zmq()
    import zmq.asyncio as zmq_asyncio  # type: ignore

    return zmq_asyncio


_ZMQ_SCHEMES = ("tcp://", "ipc://", "inproc://", "pgm://", "epgm://", "vmci://")


def validate_zmq_endpoint(address: str, name: str) -> str:
    """Validate a ZMQ endpoint spelling (works without pyzmq installed)."""
    text = str(address or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    if not any(text.startswith(scheme) for scheme in _ZMQ_SCHEMES):
        raise ValueError(
            f"{name} must be a ZMQ endpoint (tcp://, ipc://, inproc://...), "
            f"got {text!r}")
    if text.endswith("://"):
        raise ValueError(f"{name} is missing an address: {text!r}")
    return text


def _validate_pynng_address(address: str, name: str) -> str:
    text = str(address or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    if not (text.startswith("tcp://") or text.startswith("ipc://")):
        raise ValueError(
            f"{name} must be a StackFlowOS pynng address (tcp:// or ipc://), "
            f"got {text!r}")
    return text


@dataclass
class BridgeConfig:
    """Validated CLI/builder configuration for one bridge instance."""

    mode: str = "rpc"              # rpc | ingest | export
    listen: str = ""
    connect: str = ""
    sub_connect: str = ""
    pub_listen: str = ""
    pub_connect: str = ""
    events_listen: str = ""
    events_connect: str = ""
    topic: str = ""
    timeout_s: float = 8.0
    contexts: int = 4
    log_level: str = "INFO"
    logger: logging.Logger | None = field(default=None, repr=False)

    def validate(self) -> "BridgeConfig":
        if self.mode == "rpc":
            self.listen = validate_zmq_endpoint(self.listen, "--listen")
            self.connect = _validate_pynng_address(self.connect, "--connect")
        elif self.mode == "ingest":
            self.sub_connect = validate_zmq_endpoint(self.sub_connect,
                                                     "--sub-connect")
            self.events_connect = _validate_pynng_address(
                self.events_connect, "--events-connect")
        elif self.mode == "export":
            self.events_listen = _validate_pynng_address(
                self.events_listen, "--events-listen")
            if self.pub_listen:
                self.pub_listen = validate_zmq_endpoint(self.pub_listen,
                                                        "--pub-listen")
            elif self.pub_connect:
                self.pub_connect = validate_zmq_endpoint(self.pub_connect,
                                                         "--pub-connect")
            else:
                raise ValueError("export mode needs --pub-listen or --pub-connect")
        else:
            raise ValueError(f"unknown bridge mode: {self.mode!r}")
        if self.timeout_s <= 0:
            raise ValueError("--timeout must be positive")
        if self.contexts < 1:
            raise ValueError("--contexts must be >= 1")
        return self


class ZmqRpcBridge:
    """Bridge ZMQ REQ/REP calls onto the hub's pynng sys RPC endpoint."""

    def __init__(self, config: BridgeConfig) -> None:
        config.validate()
        self.config = config
        self.log = config.logger or get_logger("transport.zmq_bridge")
        self._rep: Any = None
        self._ctx: Any = None
        self._client: RpcClient | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self.stats = {"requests": 0, "errors": 0}

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        zmq_asyncio = _zmq_asyncio()
        if self._running:
            return
        ctx = zmq_asyncio.Context.instance()
        rep = ctx.socket(require_zmq().REP)
        try:
            rep.bind(self.config.listen)
        except Exception as exc:
            try:
                rep.close(0)
            except Exception:
                pass
            raise ZmqUnavailable(
                f"cannot bind ZMQ REP endpoint {self.config.listen}: {exc}") from exc
        client = RpcClient(self.config.connect, src="zmq-bridge", dst="sys",
                           timeout=self.config.timeout_s,
                           contexts=self.config.contexts, logger=self.log)
        await client.start()
        self._ctx = ctx
        self._rep = rep
        self._client = client
        self._running = True
        self._task = asyncio.create_task(self._serve(),
                                         name="zmq-rpc-bridge")
        self.log.info("ZMQ RPC bridge up: %s -> %s",
                      self.config.listen, self.config.connect)

    async def _serve(self) -> None:
        while self._running:
            try:
                raw = await self._rep.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - socket teardown
                if not self._running:
                    return
                self.log.warning("ZMQ REP receive failed: %s", exc)
                await asyncio.sleep(0.05)
                continue
            try:
                reply = await self._handle(raw)
            except Exception as exc:  # pragma: no cover - defensive
                self.stats["errors"] += 1
                self.log.exception("ZMQ bridge handler failed")
                reply = _bridge_error_envelope(
                    None, "INTERNAL", f"bridge failure: {type(exc).__name__}")
            try:
                await self._rep.send(reply)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - socket teardown
                if not self._running:
                    return
                self.log.warning("ZMQ REP send failed: %s", exc)

    async def _handle(self, raw: bytes) -> bytes:
        self.stats["requests"] += 1
        try:
            env = Envelope.from_bytes(raw)
        except ValueError as exc:
            self.stats["errors"] += 1
            return _bridge_error_envelope(
                None, "BAD_REQUEST", f"malformed StackFlowOS envelope: {exc}")
        if env.kind != KIND_RPC:
            self.stats["errors"] += 1
            return _bridge_error_envelope(
                env, "BAD_REQUEST",
                f"ZMQ RPC bridge expects kind=rpc, got {env.kind!r}")
        client = self._client
        if client is None:
            return _bridge_error_envelope(env, "UNAVAILABLE", "bridge not started")
        try:
            data = await client.call(env.action, dict(env.data or {}),
                                     dst=env.dst or "sys")
        except SfoError as exc:
            self.stats["errors"] += 1
            return env.reply_error(exc.code, exc.message, exc.detail).to_bytes()
        except Exception as exc:  # transport failure: relay as UNAVAILABLE
            self.stats["errors"] += 1
            return env.reply_error(
                "UNAVAILABLE",
                f"bridge could not reach the hub: {type(exc).__name__}: {exc}"
            ).to_bytes()
        return env.reply(data).to_bytes()

    async def stop(self) -> None:
        if not self._running and self._rep is None:
            return
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        rep, self._rep = self._rep, None
        if rep is not None:
            try:
                rep.close(0)
            except Exception:
                pass
        client, self._client = self._client, None
        if client is not None:
            await client.stop()
        self.log.info("ZMQ RPC bridge stopped")


class ZmqEventIngestBridge:
    """Bridge a foreign ZMQ PUB stream into pynng events (records only)."""

    def __init__(self, config: BridgeConfig) -> None:
        if config.mode != "ingest":
            config.mode = "ingest"
        config.validate()
        self.config = config
        self.log = config.logger or get_logger("transport.zmq_bridge")
        self._sub: Any = None
        self._publisher: EventPublisher | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self.stats = {"events": 0, "errors": 0}

    async def start(self) -> None:
        zmq_asyncio = _zmq_asyncio()
        if self._running:
            return
        ctx = zmq_asyncio.Context.instance()
        sub = ctx.socket(require_zmq().SUB)
        try:
            sub.setsockopt(require_zmq().SUBSCRIBE, self.config.topic.encode())
            sub.connect(self.config.sub_connect)
        except Exception as exc:
            try:
                sub.close(0)
            except Exception:
                pass
            raise ZmqUnavailable(
                f"cannot connect ZMQ SUB endpoint {self.config.sub_connect}: "
                f"{exc}") from exc
        publisher = EventPublisher(self.config.events_connect,
                                   src="zmq-bridge", logger=self.log)
        await publisher.start()
        self._sub = sub
        self._publisher = publisher
        self._running = True
        self._task = asyncio.create_task(self._serve(),
                                         name="zmq-event-ingest-bridge")
        self.log.info("ZMQ event ingest bridge up: %s -> %s",
                      self.config.sub_connect, self.config.events_connect)

    async def _serve(self) -> None:
        while self._running:
            try:
                raw = await self._sub.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - socket teardown
                if not self._running:
                    return
                self.log.warning("ZMQ SUB receive failed: %s", exc)
                await asyncio.sleep(0.05)
                continue
            try:
                env = Envelope.from_bytes(raw)
                if env.kind != KIND_EVENT:
                    self.stats["errors"] += 1
                    continue
                recorder = EventRecord.from_envelope(env)
            except (ValueError, TypeError) as exc:
                self.stats["errors"] += 1
                self.log.debug("dropping malformed ZMQ event frame: %s", exc)
                continue
            publisher = self._publisher
            if publisher is not None:
                publisher.publish(recorder)
                self.stats["events"] += 1

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        sub, self._sub = self._sub, None
        if sub is not None:
            try:
                sub.close(0)
            except Exception:
                pass
        publisher, self._publisher = self._publisher, None
        if publisher is not None:
            await publisher.stop()


class NngEventExportBridge:
    """Bind a pynng SUB endpoint and republish records on a ZMQ PUB socket."""

    def __init__(self, config: BridgeConfig) -> None:
        if config.mode != "export":
            config.mode = "export"
        config.validate()
        self.config = config
        self.log = config.logger or get_logger("transport.zmq_bridge")
        self._subscriber: EventSubscriber | None = None
        self._pub: Any = None
        self._running = False
        self.stats = {"events": 0, "errors": 0}

    async def start(self) -> None:
        zmq_mod = require_zmq()
        if self._running:
            return
        ctx = _zmq_asyncio().Context.instance()
        pub = ctx.socket(zmq_mod.PUB)
        try:
            if self.config.pub_listen:
                pub.bind(self.config.pub_listen)
            else:
                pub.connect(self.config.pub_connect)
        except Exception as exc:
            try:
                pub.close(0)
            except Exception:
                pass
            raise ZmqUnavailable(f"cannot open ZMQ PUB socket: {exc}") from exc
        subscriber = EventSubscriber(self.config.events_listen, logger=self.log)
        subscriber.on_record(self._forward)
        await subscriber.start()
        self._pub = pub
        self._subscriber = subscriber
        self._running = True
        self.log.info("ZMQ event export bridge up: %s -> %s",
                      self.config.events_listen,
                      self.config.pub_listen or self.config.pub_connect)

    def _forward(self, record: EventRecord) -> None:
        pub = self._pub
        if pub is None:
            return
        try:
            pub.send(record.to_envelope().to_bytes(), flags=0)
            self.stats["events"] += 1
        except Exception as exc:  # pragma: no cover - ZMQ teardown
            self.stats["errors"] += 1
            self.log.debug("ZMQ PUB send failed: %s", exc)

    async def stop(self) -> None:
        self._running = False
        subscriber, self._subscriber = self._subscriber, None
        if subscriber is not None:
            await subscriber.stop()
        pub, self._pub = self._pub, None
        if pub is not None:
            try:
                pub.close(0)
            except Exception:
                pass


def _bridge_error_envelope(env: Envelope | None, code: str,
                           message: str) -> bytes:
    if env is None:
        env = Envelope.rpc("bridge.error", {}, src="sys", dst="zmq-client")
    return env.reply_error(code, message).to_bytes()


def build_bridge(config: BridgeConfig) -> Any:
    """Construct the bridge for ``config.mode`` (validates parameters)."""
    if config.mode == "rpc":
        return ZmqRpcBridge(config)
    if config.mode == "ingest":
        return ZmqEventIngestBridge(config)
    if config.mode == "export":
        return NngEventExportBridge(config)
    config.validate()  # raises the descriptive ValueError
    raise ValueError(f"unknown bridge mode: {config.mode!r}")


def parse_args(argv: list[str] | None = None) -> BridgeConfig:
    """Parse CLI arguments into a :class:`BridgeConfig` (no pyzmq needed)."""
    parser = argparse.ArgumentParser(
        prog="stackflowos.transport.zmq_bridge",
        description="Bridge StackFlowOS pynng envelopes to/from ZeroMQ "
                    "(requires the optional pyzmq package).")
    parser.add_argument("--listen", default="",
                        help="ZMQ REP endpoint to bind, e.g. tcp://0.0.0.0:5555")
    parser.add_argument("--connect", default="",
                        help="hub sys RPC address (tcp:// or ipc://)")
    parser.add_argument("--sub-connect", default="",
                        help="foreign ZMQ PUB endpoint to subscribe to")
    parser.add_argument("--events-connect", default="",
                        help="hub events address to publish pynng events to")
    parser.add_argument("--events-listen", default="",
                        help="local pynng SUB endpoint to bind (export mode)")
    parser.add_argument("--pub-listen", default="",
                        help="ZMQ PUB endpoint to bind (export mode)")
    parser.add_argument("--pub-connect", default="",
                        help="ZMQ PUB endpoint to connect (export mode)")
    parser.add_argument("--topic", default="",
                        help="ZMQ SUB topic filter (ingest mode)")
    parser.add_argument("--timeout", type=float, default=8.0,
                        help="per-call hub timeout in seconds")
    parser.add_argument("--contexts", type=int, default=4,
                        help="concurrent pynng RPC contexts")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    if args.listen or args.connect:
        mode = "rpc"
        if not (args.listen and args.connect):
            raise ValueError("RPC mode needs both --listen and --connect")
    elif args.sub_connect or args.events_connect:
        mode = "ingest"
        if not (args.sub_connect and args.events_connect):
            raise ValueError("ingest mode needs both --sub-connect and "
                             "--events-connect")
    elif args.events_listen or args.pub_listen or args.pub_connect:
        mode = "export"
    else:
        raise ValueError("nothing to do: pass --listen/--connect, "
                         "--sub-connect/--events-connect or "
                         "--events-listen with --pub-listen/--pub-connect")
    return BridgeConfig(
        mode=mode, listen=args.listen, connect=args.connect,
        sub_connect=args.sub_connect, events_connect=args.events_connect,
        events_listen=args.events_listen, pub_listen=args.pub_listen,
        pub_connect=args.pub_connect, topic=args.topic,
        timeout_s=args.timeout, contexts=args.contexts,
        log_level=args.log_level,
    ).validate()


async def _run(config: BridgeConfig) -> int:
    level = getattr(logging, str(config.log_level or "INFO").upper(),
                    logging.INFO)
    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    bridge = build_bridge(config)
    await bridge.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:  # pragma: no cover - signal path
        pass
    finally:
        await bridge.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry: returns an exit code (2 for usage/dependency errors)."""
    try:
        config = parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    except ValueError as exc:
        print(f"zmq_bridge: {exc}", file=sys.stderr)
        return 2
    if not available():
        message = ("optional dependency pyzmq is not installed; "
                   "`pip install pyzmq` to run the bridge")
        print(f"zmq_bridge: {message}", file=sys.stderr)
        return 2
    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        return 0
    except ZmqUnavailable as exc:
        print(f"zmq_bridge: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

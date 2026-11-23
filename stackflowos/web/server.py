"""A small, dependency-free HTTP/1.1 server with Server-Sent Events.

Why hand-rolled: StackFlowOS already depends on pynng only. The web surface
needs exactly three things — static files, JSON endpoints and an event stream —
and the stdlib covers all three. No build step, no ASGI stack, nothing to
install on a robot or a phone-adjacent device.

The server binds loopback by default and is an operator console for one person
on one machine.  Every request passes :class:`stackflowos.web.auth.WebAuth`
before routing: a per-process launch token is exchanged for an ``HttpOnly``
cookie, Host/Origin are checked (DNS rebinding, CSRF) and all responses carry
the console's security headers.  Only CSS/JS/images/favicon are anonymous.
"""

from __future__ import annotations

import asyncio
import math
import mimetypes
import posixpath
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..errors import BadRequest, SfoError, Unavailable
from ..protocol import dumps
from ..util.logging import get_logger
from .auth import WebAuth, security_headers

ASSETS_DIR = Path(__file__).resolve().parent.parent / "webassets"
MAX_BODY = 1024 * 1024
DEFAULT_SSE_HEARTBEAT_S = 15.0
DEFAULT_SSE_MAX_CLIENTS = 32
DEFAULT_SSE_LIMIT = 200
MAX_SSE_LIMIT = 1000
MIN_SSE_HEARTBEAT_S = 1.0
MAX_SSE_HEARTBEAT_S = 300.0

_INT_PARAM = re.compile(r"^[+-]?\d+$")
_FLOAT_PARAM = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")

_EXTRA_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
}


class WebServer:
    def __init__(self, app, *, host: str = "127.0.0.1", port: int = 17800,
                 assets_dir: Path | None = None, logger=None,
                 trusted_hosts: tuple[str, ...] = (),
                 cookie_ttl_days: int = 7,
                 secure: bool = False,
                 launch_token: str | None = None,
                 sse_heartbeat_s: float = DEFAULT_SSE_HEARTBEAT_S,
                 sse_max_clients: int = DEFAULT_SSE_MAX_CLIENTS) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.assets_dir = Path(assets_dir or ASSETS_DIR)
        self.log = logger or get_logger("web")
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.Task] = set()
        # SSE defaults come from ``config.web``; the request may override
        # ``heartbeat``/``since``/``limit`` within their validated ranges.
        self.sse_heartbeat_s = float(sse_heartbeat_s)
        self.sse_max_clients = int(sse_max_clients)
        self._sse_clients = 0
        # Browser admission: token -> HMAC cookie, Host/Origin/CSRF checks.
        # Both secrets are per-process and memory-only.
        self.auth = WebAuth(host=host, port=port,
                            trusted_hosts=trusted_hosts,
                            cookie_ttl_days=cookie_ttl_days,
                            secure=secure,
                            launch_token=launch_token)
        # Imported lazily to keep server.py reusable in tests.
        from .api import WebAPI
        self.api = WebAPI(app)

    @property
    def url(self) -> str:
        return f"http://{_display_host(self.host)}:{self.port}"

    @property
    def launch_token(self) -> str:
        """Per-process launch token (tests/CLI only; never log it)."""
        return self.auth.launch_token

    @property
    def login_url(self) -> str:
        """Browser login URL including the per-process launch token."""
        return f"{self.url}/?token={self.auth.launch_token}"

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port,
                                                  limit=MAX_BODY + 65536)
        sockets = self._server.sockets or []
        if sockets:
            self.port = sockets[0].getsockname()[1]
        # Keep the authority allow-list in sync with the ephemeral port.
        self.auth.port = self.port
        self.log.info("web listening on %s", self.url)
        return self.url

    async def stop(self) -> None:
        # Python 3.12's Server.wait_closed() waits for live connections too,
        # so clients must be cancelled first or an open SSE stream deadlocks
        # shutdown.
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for task in list(self._clients):
            task.cancel()
        if self._clients:
            await asyncio.gather(*self._clients, return_exceptions=True)
        if server is not None:
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=3.0)
            except asyncio.TimeoutError:
                self.log.debug("http server wait_closed timed out")

    # ------------------------------------------------------------------
    # connection handling
    # ------------------------------------------------------------------
    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._clients.add(task)
        try:
            request = await self._read_request(reader)
            if request is None:
                return
            method, target, headers, body = request
            parts = urlsplit(target)
            path = _normalize_path(parts.path)
            if path is None:
                await self._send_json(writer, 403, {"ok": False, "error": {
                    "code": "FORBIDDEN", "message": "path traversal is not allowed"}})
                return
            query_pairs = parse_qs(parts.query, keep_blank_values=True)
            query = {k: v[-1] if v else "" for k, v in query_pairs.items()}
            decision = self.auth.check(method, path, headers,
                                       query_pairs.get("token", ()))
            if decision.login:
                await self._send_redirect(
                    writer, "./", self.auth.set_cookie_header(headers.get("host", "")))
                return
            if not decision.allowed:
                await self._send_json(writer, decision.status, {
                    "ok": False,
                    "error": {"code": decision.code, "message": decision.message},
                })
                return
            if path == "/api/events/stream":
                await self._serve_sse(writer, query)
                return
            if path.startswith("/api/"):
                status, payload = await self.api.handle(method, path, query, body, headers)
                await self._send_json(writer, status, payload)
                return
            await self._serve_static(writer, method, path)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            raise
        except SfoError as exc:
            try:
                await self._send_json(writer, exc.http_status, {"ok": False,
                                                                "error": exc.to_dict()})
            except Exception:
                pass
        except Exception:
            self.log.exception("web request failed")
            try:
                await self._send_json(writer, 500, {"ok": False, "error": {
                    "code": "INTERNAL", "message": "internal server error"}})
            except Exception:
                pass
        finally:
            if task is not None:
                self._clients.discard(task)
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:
                pass

    async def _read_request(self, reader: asyncio.StreamReader):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=30)
        except asyncio.TimeoutError:
            return None
        if not request_line or not request_line.strip():
            return None
        try:
            method, target, _version = request_line.decode("latin-1").strip().split(" ", 2)
        except ValueError:
            return None
        headers: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=30)
            if not line or line in (b"\r\n", b"\n"):
                break
            key, _, value = line.decode("latin-1").partition(":")
            headers[key.strip().lower()] = value.strip()
        length = 0
        try:
            length = int(headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            raise SfoError("request body too large", code="BAD_REQUEST")
        body = b""
        if length:
            body = await asyncio.wait_for(reader.readexactly(length), timeout=30)
        return method.upper(), target, headers, body

    async def _send_json(self, writer: asyncio.StreamWriter, status: int, payload: Any) -> None:
        body = dumps(payload).encode("utf-8")
        header = (
            f"HTTP/1.1 {status} {_reason(status)}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            + security_headers(cache="no-store")
            + "Connection: close\r\n"
            "\r\n"
        ).encode("latin-1")
        writer.write(header + body)
        await writer.drain()

    async def _send_redirect(self, writer: asyncio.StreamWriter, location: str,
                             cookie: str) -> None:
        header = (
            "HTTP/1.1 303 See Other\r\n"
            f"Location: {location}\r\n"
            f"Set-Cookie: {cookie}\r\n"
            "Content-Length: 0\r\n"
            + security_headers(cache="no-store")
            + "Connection: close\r\n"
            "\r\n"
        ).encode("latin-1")
        writer.write(header)
        await writer.drain()

    async def _serve_static(self, writer: asyncio.StreamWriter, method: str, path: str) -> None:
        if method not in ("GET", "HEAD"):
            await self._send_json(writer, 405, {"ok": False, "error": {
                "code": "METHOD_NOT_ALLOWED", "message": "method not allowed"}})
            return
        if path in ("/", ""):
            path = "/index.html"
        relative = path.lstrip("/")
        candidate = (self.assets_dir / relative).resolve()
        try:
            candidate.relative_to(self.assets_dir.resolve())
        except ValueError:
            await self._send_json(writer, 403, {"ok": False, "error": {
                "code": "DENIED", "message": "forbidden"}})
            return
        if not candidate.is_file():
            # SPA-ish fallback for unknown app routes
            if "." not in Path(path).name:
                candidate = self.assets_dir / "index.html"
            if not candidate.is_file():
                await self._send_json(writer, 404, {"ok": False, "error": {
                    "code": "NOT_FOUND", "message": "not found"}})
                return
        try:
            data = candidate.read_bytes()
        except OSError:
            await self._send_json(writer, 404, {"ok": False, "error": {
                "code": "NOT_FOUND", "message": "not found"}})
            return
        ctype = _EXTRA_TYPES.get(candidate.suffix.lower())
        if ctype is None:
            ctype = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        header = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(data)}\r\n"
            + security_headers(cache="no-cache")
            + "Connection: close\r\n"
            "\r\n"
        ).encode("latin-1")
        writer.write(header)
        if method == "GET":
            writer.write(data)
        await writer.drain()

    async def _serve_sse(self, writer: asyncio.StreamWriter, query: dict) -> None:
        """Open an authenticated event stream.

        Every query parameter is validated *before* the 200 response and
        before subscribing: a rejected request must neither look like a live
        stream nor leave a queue behind.  The response deliberately carries no
        ``Access-Control-*`` headers (H2): the console is same-origin only.
        """
        try:
            heartbeat = _parse_heartbeat(query.get("heartbeat"), self.sse_heartbeat_s)
            since = _parse_int_param("since", query.get("since"),
                                     default=0, minimum=0)
            limit = _parse_int_param("limit", query.get("limit"),
                                     default=DEFAULT_SSE_LIMIT,
                                     minimum=1, maximum=MAX_SSE_LIMIT)
            if self._sse_clients >= self.sse_max_clients:
                raise Unavailable("too many concurrent event stream clients")
        except SfoError as exc:
            await self._send_json(writer, exc.http_status, {
                "ok": False, "error": exc.to_dict(),
            })
            return
        queue = self.app.eventlog.subscribe()
        self._sse_clients += 1
        try:
            header = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/event-stream; charset=utf-8\r\n"
                + security_headers(cache="no-cache, no-store")
                + "Connection: close\r\n"
                "X-Accel-Buffering: no\r\n"
                "\r\n"
            ).encode("latin-1")
            writer.write(header)
            writer.write(b"retry: 2000\n\n")
            if since > 0:
                backlog = self.app.eventlog.since(since, limit=limit)
            else:
                backlog = self.app.eventlog.tail(limit)
            for record in backlog:
                writer.write(b"data: " + dumps(record.to_dict()).encode("utf-8") + b"\n\n")
            await writer.drain()
            while True:
                try:
                    record = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    writer.write(b": ping\n\n")
                    await writer.drain()
                    continue
                writer.write(b"data: " + dumps(record.to_dict()).encode("utf-8") + b"\n\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            return
        except Exception:
            self.log.debug("sse stream ended", exc_info=True)
        finally:
            self._sse_clients -= 1
            try:
                self.app.eventlog.unsubscribe(queue)
            except Exception:
                pass


def _parse_heartbeat(raw: str | None, default: float) -> float:
    """Strictly parse ``?heartbeat=``; ``None`` means "use the config value"."""
    if raw is None:
        return float(default)
    text = raw.strip()
    if not _FLOAT_PARAM.match(text):
        raise BadRequest("heartbeat must be a number")
    try:
        value = float(text)
    except (TypeError, ValueError):  # pragma: no cover - guarded by the regex
        raise BadRequest("heartbeat must be a number") from None
    if not math.isfinite(value) or not (
            MIN_SSE_HEARTBEAT_S <= value <= MAX_SSE_HEARTBEAT_S):
        raise BadRequest("heartbeat must be a finite number in [1, 300]")
    return value


def _parse_int_param(name: str, raw: str | None, *, default: int,
                     minimum: int, maximum: int | None = None) -> int:
    """Strictly parse one integer query parameter inside its allowed range."""
    if raw is None:
        return default
    text = raw.strip()
    if not _INT_PARAM.match(text):
        raise BadRequest(f"{name} must be an integer")
    try:
        value = int(text, 10)
    except ValueError:  # pragma: no cover - guarded by the regex
        raise BadRequest(f"{name} must be an integer") from None
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is None:
            raise BadRequest(f"{name} must be >= {minimum}")
        raise BadRequest(f"{name} must be in [{minimum}, {maximum}]")
    return value


def _display_host(host: str) -> str:
    host = (host or "").strip()
    if host in ("", "0.0.0.0", "::"):
        return "127.0.0.1"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _normalize_path(raw_path: str) -> str | None:
    """Unquote and collapse ``.``/``//`` before admission and routing.

    Returns ``None`` when the request contains a ``..`` segment.  Without this,
    ``/css/../index.html`` would look anonymous to the admission layer while
    the static handler resolves it to the authenticated app shell; a hard 403
    is simpler and safer than trying to normalise traversal away.
    """
    path = unquote(raw_path or "")
    if not path.startswith("/"):
        path = "/" + path
    if any(segment == ".." for segment in path.split("/")):
        return None
    path = posixpath.normpath(path)
    if path.startswith("//"):
        path = "/" + path.lstrip("/")
    return path


def _reason(status: int) -> str:
    return {
        200: "OK", 201: "Created", 202: "Accepted", 204: "No Content",
        303: "See Other",
        400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
        405: "Method Not Allowed", 409: "Conflict",
        415: "Unsupported Media Type",
        500: "Internal Server Error",
        503: "Service Unavailable", 504: "Gateway Timeout",
    }.get(status, "OK")

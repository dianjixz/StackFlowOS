"""A small, dependency-free HTTP/1.1 server with Server-Sent Events.

Why hand-rolled: StackFlowOS already depends on pynng only. The web surface
needs exactly three things — static files, JSON endpoints and an event stream —
and the stdlib covers all three. No build step, no ASGI stack, nothing to
install on a robot or a phone-adjacent device.

The server binds loopback by default and is an operator console for one person
on one machine.  Every request passes :class:`stackflowos.web.auth.WebAuth`
before routing: a per-process launch token is exchanged for an ``HttpOnly``
cookie, Host/Origin are checked (DNS rebinding, CSRF) and all responses carry
the console's security headers.  Only CSS/JS/images/favicon and the data-free
``/healthz`` liveness probe are anonymous.
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
from .api import WebReply

ASSETS_DIR = Path(__file__).resolve().parent.parent / "webassets"
MAX_BODY = 1024 * 1024
# Request framing limits (L1).  A loopback console still must not let one
# client tie up memory with a giant request line, header block or body.
MAX_REQUEST_LINE = 8192
MAX_HEADERS = 100
MAX_HEADER_BYTES = 16 * 1024
HEADER_TIMEOUT_S = 10.0
BODY_TIMEOUT_S = 30.0
DEFAULT_SSE_HEARTBEAT_S = 15.0
DEFAULT_SSE_MAX_CLIENTS = 32
DEFAULT_SSE_LIMIT = 200
MAX_SSE_LIMIT = 1000
MIN_SSE_HEARTBEAT_S = 1.0
MAX_SSE_HEARTBEAT_S = 300.0

_INT_PARAM = re.compile(r"^[+-]?\d+$")
_FLOAT_PARAM = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_CONTENT_LENGTH = re.compile(r"^[0-9]+$")


class _HttpError(Exception):
    """A rejected request that still deserves a JSON error response (L1).

    Raised while parsing the request head/body, i.e. before authentication.
    ``_handle_client`` renders it through :meth:`WebServer._send_json` so the
    rejection carries the same security headers as every other response.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


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
            if _has_control_chars(target):
                await self._send_json(writer, 400, {"ok": False, "error": {
                    "code": "BAD_REQUEST", "message": "invalid request path"}})
                return
            try:
                parts = urlsplit(target)
            except ValueError:
                await self._send_json(writer, 400, {"ok": False, "error": {
                    "code": "BAD_REQUEST", "message": "invalid request target"}})
                return
            if _invalid_path(parts.path):
                # NUL/control characters would reach ``Path.resolve()`` and
                # surface as an opaque 500; reject them before admission.
                await self._send_json(writer, 400, {"ok": False, "error": {
                    "code": "BAD_REQUEST", "message": "invalid request path"}})
                return
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
            if path == "/healthz":
                await self._serve_liveness(writer, method)
                return
            if path == "/api/events/stream":
                await self._serve_sse(writer, query)
                return
            if path.startswith("/api/"):
                status, payload = await self.api.handle(
                    method, path, query, body, headers, decision)
                if isinstance(payload, WebReply):
                    await self._send_raw(writer, payload.status or status,
                                         payload.body, payload.content_type,
                                         extra_headers=payload.headers)
                else:
                    await self._send_json(writer, status, payload)
                return
            await self._serve_static(writer, method, path)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            raise
        except _HttpError as exc:
            try:
                await self._send_json(writer, exc.status, {"ok": False, "error": {
                    "code": exc.code, "message": exc.message}})
            except Exception:
                pass
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
        """Read one request head/body with hard framing limits (L1).

        Every rejection raises :class:`_HttpError` so the caller can answer with
        a normal JSON error instead of falling through to the generic 500.
        """
        try:
            request_line = await asyncio.wait_for(reader.readline(),
                                                  timeout=HEADER_TIMEOUT_S)
        except asyncio.TimeoutError:
            return None
        except (ValueError, asyncio.LimitOverrunError):
            raise _HttpError(414, "URI_TOO_LONG",
                             "request line is too long") from None
        if not request_line or not request_line.strip():
            return None
        if len(request_line) > MAX_REQUEST_LINE:
            raise _HttpError(414, "URI_TOO_LONG", "request line is too long")
        try:
            method, target, _version = request_line.decode("latin-1").strip().split(" ", 2)
        except (ValueError, UnicodeDecodeError):
            raise _HttpError(400, "BAD_REQUEST", "malformed request line") from None

        headers: dict[str, str] = {}
        header_count = 0
        header_bytes = 0
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(),
                                              timeout=HEADER_TIMEOUT_S)
            except asyncio.TimeoutError:
                return None
            except (ValueError, asyncio.LimitOverrunError):
                raise _HttpError(431, "HEADERS_TOO_LARGE",
                                 "request headers are too large") from None
            if not line or line in (b"\r\n", b"\n"):
                break
            header_count += 1
            header_bytes += len(line)
            if header_count > MAX_HEADERS:
                raise _HttpError(431, "TOO_MANY_HEADERS",
                                 "too many request headers")
            if header_bytes > MAX_HEADER_BYTES or len(line) > MAX_HEADER_BYTES:
                raise _HttpError(431, "HEADERS_TOO_LARGE",
                                 "request headers are too large")
            key, separator, value = line.decode("latin-1").partition(":")
            if not separator:
                raise _HttpError(400, "BAD_REQUEST", "malformed request header")
            key = key.strip().lower()
            value = value.strip()
            if not key or _has_control_chars(key) or _has_control_chars(value):
                raise _HttpError(400, "BAD_REQUEST", "malformed request header")
            headers[key] = value

        transfer_encoding = (headers.get("transfer-encoding") or "").strip().lower()
        if transfer_encoding:
            # Only identity framing with Content-Length is implemented.
            if transfer_encoding == "chunked":
                raise _HttpError(501, "NOT_IMPLEMENTED",
                                 "chunked transfer encoding is not supported")
            raise _HttpError(400, "BAD_REQUEST", "unsupported Transfer-Encoding")

        length = 0
        raw_length = headers.get("content-length")
        if raw_length is not None:
            if not _CONTENT_LENGTH.fullmatch(raw_length):
                raise _HttpError(400, "BAD_REQUEST", "invalid Content-Length")
            length = int(raw_length)
            if length > MAX_BODY:
                raise _HttpError(413, "PAYLOAD_TOO_LARGE",
                                 "request body is too large")
        body = b""
        if length:
            try:
                body = await asyncio.wait_for(reader.readexactly(length),
                                              timeout=BODY_TIMEOUT_S)
            except asyncio.TimeoutError:
                return None
            except asyncio.IncompleteReadError:
                return None
        return method.upper(), target, headers, body

    async def _send_json(self, writer: asyncio.StreamWriter, status: int, payload: Any,
                         *, include_body: bool = True) -> None:
        body = dumps(payload).encode("utf-8")
        header = (
            f"HTTP/1.1 {status} {_reason(status)}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            + security_headers(cache="no-store")
            + "Connection: close\r\n"
            "\r\n"
        ).encode("latin-1")
        writer.write(header + (body if include_body else b""))
        await writer.drain()

    async def _send_raw(self, writer: asyncio.StreamWriter, status: int,
                        body: bytes, content_type: str, *,
                        extra_headers: dict[str, str] | None = None) -> None:
        """Send a non-JSON response (Prometheus text, download, ...).

        ``WebReply`` headers are restricted to a safe prefix set: a feature
        handler must not be able to inject its own framing or security-header
        overrides.
        """
        safe_prefixes = ("content-disposition", "x-", "cache-control")
        extra = ""
        for key, value in (extra_headers or {}).items():
            name = str(key).strip().lower()
            text = str(value).replace("\r", "").replace("\n", "")
            if not name or name not in safe_prefixes and not name.startswith(("x-", "content-disposition")):
                continue
            extra += f"{name.title()}: {text}\r\n"
        header = (
            f"HTTP/1.1 {status} {_reason(status)}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            + extra
            + security_headers(cache="no-store")
            + "Connection: close\r\n"
            "\r\n"
        ).encode("latin-1")
        writer.write(header + body)
        await writer.drain()

    async def _serve_liveness(self, writer: asyncio.StreamWriter, method: str) -> None:
        """Answer the anonymous ``/healthz`` liveness probe.

        The process supervisor cannot know the per-process launch token, so
        this exact path is exempt from session auth (see
        ``web.auth.ANONYMOUS_PATHS``).  It must therefore carry no project
        data: the payload is only ``status``/``transport``.  ``transport`` is
        what distinguishes "HTTP thread alive" from "pynng IPC alive"; a down
        transport answers 503 so ``curl --fail`` and the health timer notice.
        ``HEAD`` returns the same headers without a body.
        """
        if method not in ("GET", "HEAD"):
            await self._send_json(writer, 405, {"ok": False, "error": {
                "code": "METHOD_NOT_ALLOWED", "message": "method not allowed"}})
            return
        try:
            transport = str(self.app.health().get("transport") or "unknown")
        except Exception:
            self.log.exception("liveness probe failed")
            transport = "unknown"
        ok = transport == "ok"
        payload = {"ok": ok, "data": {
            "status": "ok" if ok else "degraded",
            "transport": transport,
        }}
        await self._send_json(writer, 200 if ok else 503, payload,
                              include_body=(method == "GET"))

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
        try:
            candidate = (self.assets_dir / relative).resolve()
        except (OSError, ValueError):
            # Defence in depth: NUL/overlong paths are rejected before routing,
            # but ``resolve()`` must never turn a bad path into a 500.
            await self._send_json(writer, 400, {"ok": False, "error": {
                "code": "BAD_REQUEST", "message": "invalid request path"}})
            return
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
            queue = self.app.eventlog.subscribe()
            # S7: a closed log hands back a queue that will never receive
            # records.  End the request with an error instead of holding a
            # silent stream open; the queue is not registered, so nothing leaks.
            if getattr(queue, "closed", False):
                raise Unavailable("event log is closed")
            self._sse_clients += 1
        except SfoError as exc:
            await self._send_json(writer, exc.http_status, {
                "ok": False, "error": exc.to_dict(),
            })
            return
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


def _has_control_chars(text: str) -> bool:
    """True when ``text`` contains C0 controls or DEL (NUL included)."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in text)


def _invalid_path(raw_path: str) -> bool:
    """Reject control characters before and after percent-decoding."""
    return _has_control_chars(raw_path or "") or _has_control_chars(
        unquote(raw_path or ""))


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
        411: "Length Required", 413: "Payload Too Large", 414: "URI Too Long",
        415: "Unsupported Media Type",
        431: "Request Header Fields Too Large",
        500: "Internal Server Error", 501: "Not Implemented",
        503: "Service Unavailable", 504: "Gateway Timeout",
    }.get(status, "OK")

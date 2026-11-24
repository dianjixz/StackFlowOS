"""MCP stdio JSON-RPC client (newline *and* ``Content-Length`` framing).

The MCP stdio transport is newline-delimited JSON, while several existing
tools/tests use the LSP-style ``Content-Length`` header.  The reader accepts
both (it sniffs every frame), the writer is explicit: ``framing="newline"``
(default, per MCP), ``"content-length"`` or ``"auto"`` (same as newline for
outgoing frames).  This keeps one client usable against both flavours without
a second implementation.

Everything is stdlib: ``asyncio.create_subprocess_exec`` + JSON.  The process
group is terminated on close; pending requests fail closed when the child
exits or the timeout fires.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import deque
from typing import Any, Callable

from ..errors import BadRequest, Timeout, Unavailable
from ..util.logging import get_logger
from ..util.redact import redact_text

DEFAULT_TIMEOUT_S = 30.0
MAX_FRAME_BYTES = 8 * 1024 * 1024
STDERR_TAIL = 50


class MCPError(Exception):
    """A JSON-RPC error returned by the server (or a protocol violation)."""

    def __init__(self, message: str, *, code: int | None = None,
                 data: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data


class MCPToolError(MCPError):
    """``tools/call`` returned ``isError=true``."""


class MCPStdioClient:
    """One MCP server subprocess speaking JSON-RPC over stdio."""

    def __init__(self, name: str, command: str, args: list[str] | None = None,
                 *, env: dict | None = None, cwd: str | None = None,
                 framing: str = "newline",
                 call_timeout_s: float = DEFAULT_TIMEOUT_S,
                 client_info: dict | None = None,
                 logger: Any = None,
                 on_exit: Callable[[str], None] | None = None,
                 on_notification: Callable[[dict], None] | None = None) -> None:
        self.name = str(name)
        self.command = str(command)
        self.args = [str(item) for item in (args or [])]
        self.env = {str(key): str(value) for key, value in (env or {}).items()}
        self.cwd = cwd or None
        self.framing = framing if framing in ("newline", "content-length") \
            else "newline"
        self.call_timeout_s = float(call_timeout_s)
        self.client_info = dict(client_info or {
            "name": "stackflowos", "version": "0.1.0"})
        self.log = logger or get_logger(f"mcp.{self.name}")
        self.on_exit = on_exit
        self.on_notification = on_notification
        self.state = "new"
        self.error: str | None = None
        self.pid: int | None = None
        self.server_info: dict = {}
        self.server_capabilities: dict = {}
        self.stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL)
        self._worker: asyncio.subprocess.Process | None = None
        self._stdin: asyncio.StreamWriter | None = None
        self._stdout: asyncio.StreamReader | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._closed = False
        self._exit_reported = False

    # -- properties -------------------------------------------------------
    @property
    def alive(self) -> bool:
        return (self._worker is not None and self._worker.returncode is None
                and not self._closed)

    @property
    def worker(self) -> asyncio.subprocess.Process | None:
        return self._worker

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> "MCPStdioClient":
        if self._worker is not None:
            raise BadRequest(f"MCP client {self.name!r} already started")
        self.state = "starting"
        env = dict(os.environ)
        env.update(self.env)
        try:
            self._worker = await asyncio.create_subprocess_exec(
                self.command, *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env, cwd=self.cwd, limit=MAX_FRAME_BYTES)
        except (OSError, ValueError) as exc:
            self.state = "failed"
            self.error = f"cannot spawn MCP server: {exc}"
            raise Unavailable(self.error, detail={"server": self.name}) from exc
        self.pid = self._worker.pid
        assert self._worker.stdin is not None
        assert self._worker.stdout is not None
        assert self._worker.stderr is not None
        self._stdin = self._worker.stdin
        self._stdout = self._worker.stdout
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name=f"mcp-reader-{self.name}")
        self._stderr_task = asyncio.create_task(
            self._stderr_loop(), name=f"mcp-stderr-{self.name}")
        try:
            await self._initialize()
        except Exception:
            self.state = "failed"
            await self.close()
            raise
        self.state = "ready"
        return self

    async def _initialize(self) -> None:
        result = await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": self.client_info,
        }, timeout=min(self.call_timeout_s, 15.0))
        if isinstance(result, dict):
            self.server_info = dict(result.get("serverInfo") or {})
            self.server_capabilities = dict(result.get("capabilities") or {})
        await self._notify("notifications/initialized")

    async def close(self) -> None:
        self._closed = True
        self._fail_pending(Unavailable(
            f"MCP server {self.name!r} closed", detail={"server": self.name}))
        if self._worker is not None and self._worker.returncode is None:
            try:
                self._worker.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._worker.wait(), timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                try:
                    self._worker.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self._worker.wait(), timeout=3.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self.state != "failed":
            self.state = "closed"

    # -- JSON-RPC ---------------------------------------------------------
    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _send(self, payload: dict) -> None:
        if self._stdin is None or not self.alive:
            raise Unavailable(f"MCP server {self.name!r} is not running",
                              detail={"server": self.name})
        body = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_FRAME_BYTES:
            raise BadRequest("MCP frame too large")
        if self.framing == "content-length":
            frame = (b"Content-Length: " + str(len(body)).encode("ascii")
                     + b"\r\n\r\n" + body)
        else:
            frame = body + b"\n"
        self._stdin.write(frame)
        await self._stdin.drain()

    async def _notify(self, method: str, params: dict | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)

    async def _request(self, method: str, params: dict | None = None,
                       *, timeout: float | None = None) -> Any:
        request_id = self._new_id()
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id,
                                   "method": method}
        if params is not None:
            payload["params"] = params
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(payload)
            limit = self.call_timeout_s if timeout is None else float(timeout)
            try:
                message = await asyncio.wait_for(future, timeout=limit)
            except asyncio.TimeoutError:
                raise Timeout(
                    f"MCP request {method!r} timed out after {limit:g}s",
                    detail={"server": self.name, "method": method}) from None
        finally:
            self._pending.pop(request_id, None)
        if not isinstance(message, dict):
            raise MCPError(f"invalid JSON-RPC response from {self.name}")
        error = message.get("error")
        if isinstance(error, dict):
            raise MCPError(str(error.get("message") or "MCP error"),
                           code=error.get("code"), data=error.get("data"))
        return message.get("result")

    # -- reader/writer tasks ---------------------------------------------
    async def _reader_loop(self) -> None:
        assert self._stdout is not None
        reason = "eof"
        try:
            while not self._closed:
                message = await self._read_frame()
                self._dispatch(message)
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except EOFError:
            reason = "eof"
        except Exception as exc:  # noqa: BLE001 - the process is gone either way
            reason = f"{type(exc).__name__}: {exc}"
            self.log.warning("MCP %s reader failed: %s", self.name, exc)
        finally:
            self._fail_pending(Unavailable(
                f"MCP server {self.name!r} exited ({reason})",
                detail={"server": self.name}))
            if not self._closed:
                self.state = "failed"
                self.error = reason
                self._report_exit(reason)

    async def _read_frame(self) -> dict:
        assert self._stdout is not None
        while True:
            line = await self._stdout.readline()
            if not line:
                raise EOFError()
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.lower().startswith(b"content-length:"):
                headers: dict[str, str] = {}
                key, _, value = stripped.partition(b":")
                headers[key.strip().lower().decode("ascii", "replace")] = \
                    value.strip().decode("ascii", "replace")
                while True:
                    header_line = await self._stdout.readline()
                    if not header_line:
                        raise EOFError()
                    if header_line in (b"\r\n", b"\n"):
                        break
                    key, _, value = header_line.partition(b":")
                    headers[key.strip().lower().decode("ascii", "replace")] = \
                        value.strip().decode("ascii", "replace")
                try:
                    length = int(headers.get("content-length", ""))
                except ValueError:
                    raise MCPError("invalid Content-Length frame") from None
                if length < 0 or length > MAX_FRAME_BYTES:
                    raise MCPError("invalid Content-Length size")
                body = await self._stdout.readexactly(length)
                try:
                    return json.loads(body.decode("utf-8"))
                except ValueError as exc:
                    raise MCPError(f"invalid JSON frame: {exc}") from exc
            try:
                return json.loads(stripped.decode("utf-8"))
            except ValueError:
                self.log.warning("MCP %s: ignoring non-JSON line", self.name)
                continue

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        request_id = message.get("id")
        if request_id is not None:
            future = self._pending.get(int(request_id)) \
                if isinstance(request_id, int) else None
            if future is not None and not future.done():
                future.set_result(message)
                return
            if "method" in message:
                # Server-initiated request; we do not implement any yet.
                asyncio.create_task(self._reply_method_not_found(message))
            return
        method = str(message.get("method") or "")
        if method:
            if self.on_notification is not None:
                try:
                    self.on_notification(message)
                except Exception:  # pragma: no cover
                    self.log.debug("MCP notification callback failed",
                                   exc_info=True)

    async def _reply_method_not_found(self, message: dict) -> None:
        try:
            await self._send({"jsonrpc": "2.0", "id": message.get("id"),
                              "error": {"code": -32601,
                                        "message": "method not found"}})
        except Exception:  # pragma: no cover - best effort
            pass

    async def _stderr_loop(self) -> None:
        assert self._worker is not None and self._worker.stderr is not None
        stderr = self._worker.stderr
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    return
                text = redact_text(line.decode("utf-8", "replace").rstrip())
                if text:
                    self.stderr_tail.append(text[:500])
                    self.log.debug("[%s stderr] %s", self.name, text[:500])
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - stderr is diagnostics only
            return

    def _fail_pending(self, exc: BaseException) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    def _report_exit(self, reason: str) -> None:
        if self._exit_reported:
            return
        self._exit_reported = True
        self._closed = True
        if self.on_exit is None:
            return
        try:
            self.on_exit(reason)
        except Exception:  # pragma: no cover
            self.log.debug("MCP on_exit callback failed", exc_info=True)

    # -- MCP methods ------------------------------------------------------
    async def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor: str | None = None
        for _ in range(100):
            params = {"cursor": cursor} if cursor else {}
            result = await self._request("tools/list", params)
            if not isinstance(result, dict):
                raise MCPError("tools/list returned a non-object result")
            batch = result.get("tools")
            if not isinstance(batch, list):
                raise MCPError("tools/list result lacks a tools array")
            tools.extend(item for item in batch if isinstance(item, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        result = await self._request("tools/call", {
            "name": str(name),
            "arguments": dict(arguments or {}),
        })
        if not isinstance(result, dict):
            raise MCPError("tools/call returned a non-object result")
        if result.get("isError"):
            text = _content_text(result)
            raise MCPToolError(text or f"tool {name!r} failed",
                              data={"tool": name})
        return result

    # -- diagnostics ------------------------------------------------------
    def status(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "pid": self.pid if self.alive else None,
            "command": self.command,
            "args": list(self.args),
            "framing": self.framing,
            "server_info": dict(self.server_info),
            "error": self.error,
            "stderr_tail": list(self.stderr_tail),
        }


def _content_text(result: dict) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    return "\n".join(parts)


def python_executable() -> str:
    return sys.executable or "python3"


__all__ = ["DEFAULT_TIMEOUT_S", "MCPError", "MCPStdioClient", "MCPToolError",
           "MAX_FRAME_BYTES", "STDERR_TAIL", "python_executable"]

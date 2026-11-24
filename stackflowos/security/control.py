"""Operator control socket for ``sfo url`` / ``sfo status`` (security plan §5.1.6).

The per-process launch token is never written to disk or the journal.  A
non-TTY process (systemd, a container) still needs a way for the human at the
machine to mint a fresh browser login link, so the hub listens on a Unix
domain socket:

* path ``<state>/run/control.sock``, mode ``0600``, under the state dir that is
  already ``0700``;
* every connection is checked with ``SO_PEERCRED``: a different uid is
  refused, and if the kernel cannot report credentials the request is refused
  (fail closed);
* the only actions are ``url`` (mint a single-use login URL), ``status``
  (address + session count) and ``ping``.  No shutdown, no token dump;
* the token travels only over that 0600 same-uid socket.  Nothing is logged.

The hub starts this as an optional extension (``build(app)`` -> object with
``start``/``stop``).  :class:`stackflowos.web.server.WebServer` attaches itself
in ``start()`` via ``app.security_control.attach_web(server)``; before that
attachment a ``url`` request answers ``web server is not ready`` instead of
returning a broken link.

CLI wiring (Lead-owned ``cli.py``)::

    from stackflowos.security.control import control_socket_path, query_control_socket
    reply = query_control_socket(control_socket_path(cfg.paths.run_dir), "url")
    if reply.get("ok"):
        print(reply["url"])

``query_control_socket`` is synchronous and safe to call before the asyncio
loop starts; it never raises for an unreachable socket (returns an error dict).
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import socket
import stat
import struct
from pathlib import Path
from typing import Any

from ..util.logging import get_logger

DEFAULT_SOCKET_NAME = "control.sock"
MAX_REQUEST_BYTES = 4096
MAX_REQUEST_LINE = 4096
PEER_CHECK_AVAILABLE = hasattr(socket, "SO_PEERCRED")
#: Errno values that mean "a stale socket from a dead hub" rather than a live
#: service we must not steal.
_STALE_ERRNOS = frozenset({
    errno.ECONNREFUSED,
    errno.ENOENT,
    errno.ECONNRESET,
})


def control_socket_path(run_dir: str | os.PathLike[str]) -> Path:
    """Canonical control-socket path for a state ``run`` directory."""
    return Path(run_dir) / DEFAULT_SOCKET_NAME


def peer_credentials(sock: socket.socket) -> tuple[int, int, int] | None:
    """Return ``(pid, uid, gid)`` of the peer, or ``None`` if unsupported.

    ``SO_PEERCRED`` is Linux-specific.  Callers must treat ``None`` as a
    refusal: without kernel-provided identity the socket would trust any local
    process, including a sandboxed worker running as the same user.
    """
    if not PEER_CHECK_AVAILABLE:
        return None
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                              struct.calcsize("3i"))
    except OSError:
        return None
    try:
        pid, uid, gid = struct.unpack("3i", raw)
    except struct.error:  # pragma: no cover - kernel always returns 3 ints
        return None
    return int(pid), int(uid), int(gid)


class ControlServer:
    """Asyncio Unix-socket server exposing ``url``/``status``/``ping``."""

    def __init__(self, socket_path: str | os.PathLike[str], *,
                 logger=None, expected_uid: int | None = None) -> None:
        self.path = Path(socket_path)
        self.log = logger or get_logger("security.control")
        self.expected_uid = os.getuid() if expected_uid is None else int(expected_uid)
        self._server: asyncio.AbstractServer | None = None
        self._web: Any = None

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
        self._clear_stale_socket()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.path), limit=MAX_REQUEST_BYTES)
        try:
            os.chmod(self.path, 0o600)
        except OSError as exc:  # pragma: no cover - unusual filesystem
            self.log.warning("could not chmod control socket: %s", exc)
        self.log.info("control socket listening at %s", self.path)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=2.0)
            except asyncio.TimeoutError:  # pragma: no cover
                self.log.debug("control socket wait_closed timed out")
        try:
            info = self.path.lstat()
        except OSError:
            return
        if stat.S_ISSOCK(info.st_mode) and info.st_uid == self.expected_uid:
            try:
                self.path.unlink()
            except OSError:  # pragma: no cover
                pass

    def attach_web(self, web: Any) -> None:
        """Attach the running web server (called from ``WebServer.start``)."""
        self._web = web

    def detach_web(self, web: Any = None) -> None:
        if web is None or self._web is web:
            self._web = None

    # -- path safety -----------------------------------------------------
    def _clear_stale_socket(self) -> None:
        """Remove a dead socket; refuse to touch anything else."""
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RuntimeError(f"cannot inspect control socket path: {exc}") from exc
        if not stat.S_ISSOCK(info.st_mode):
            raise RuntimeError(
                f"refusing to replace non-socket path {self.path}")
        if info.st_uid != self.expected_uid:
            raise RuntimeError(
                f"refusing to replace control socket owned by uid {info.st_uid}")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(self.path))
        except OSError as exc:
            if exc.errno in _STALE_ERRNOS:
                try:
                    self.path.unlink()
                except OSError as unlink_exc:  # pragma: no cover
                    raise RuntimeError(
                        f"cannot remove stale control socket: {unlink_exc}")
                return
            raise RuntimeError(
                f"control socket {self.path} is not usable: {exc}") from exc
        else:
            raise RuntimeError(
                f"another hub is already listening on {self.path}")
        finally:
            probe.close()

    # -- request handling ------------------------------------------------
    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        try:
            # Consume the bounded request line *before* closing a refused
            # connection: closing a Unix socket with unread peer data turns
            # the client's recv into ECONNRESET, hiding the real error.
            try:
                line = await asyncio.wait_for(reader.readline(),
                                              timeout=5.0)
            except asyncio.TimeoutError:
                await self._reply(writer, {"ok": False, "error": "request timeout"})
                return
            if not line or len(line) > MAX_REQUEST_LINE:
                await self._reply(writer, {"ok": False, "error": "invalid request"})
                return
            raw_sock = writer.get_extra_info("socket")
            creds = peer_credentials(raw_sock) if raw_sock is not None else None
            if creds is None:
                await self._reply(writer, {"ok": False,
                                           "error": "peer credentials unavailable"})
                return
            pid, uid, _gid = creds
            if uid != self.expected_uid:
                self.log.warning("control socket peer uid %s refused (pid %s)",
                                 uid, pid)
                await self._reply(writer, {"ok": False, "error": "permission denied"})
                return
            try:
                request = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                await self._reply(writer, {"ok": False, "error": "invalid JSON"})
                return
            if not isinstance(request, dict):
                await self._reply(writer, {"ok": False, "error": "invalid request"})
                return
            action = str(request.get("action") or "").strip().lower()
            await self._reply(writer, self._dispatch(action))
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            return
        except Exception:  # pragma: no cover - never leak internals
            self.log.exception("control socket request failed")
            try:
                await self._reply(writer, {"ok": False,
                                           "error": "internal error"})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:
                pass

    def _dispatch(self, action: str) -> dict:
        if action == "ping":
            return {"ok": True, "data": {"pong": True}}
        if action == "url":
            web = self._web
            if web is None:
                return {"ok": False, "error": "web server is not ready"}
            mint = getattr(web, "mint_login_url", None)
            if mint is None:
                return {"ok": False, "error": "web server cannot mint URLs"}
            try:
                url = str(mint())
            except Exception as exc:  # pragma: no cover - defensive
                return {"ok": False, "error": f"cannot mint login URL: {type(exc).__name__}"}
            if not url:
                return {"ok": False, "error": "empty login URL"}
            # Never log ``url``: it contains a bearer-equivalent launch token.
            return {"ok": True, "url": url}
        if action == "status":
            web = self._web
            data: dict[str, Any] = {"running": web is not None}
            if web is not None:
                data["url"] = getattr(web, "url", "")
                data["tls"] = bool(getattr(web, "tls_enabled", False))
                sessions = getattr(web, "session_manager", None)
                count = getattr(sessions, "count", None)
                if callable(count):
                    try:
                        data["sessions"] = int(count())
                    except Exception:  # pragma: no cover
                        pass
            return {"ok": True, "data": data}
        return {"ok": False, "error": "unknown action"}

    async def _reply(self, writer: asyncio.StreamWriter, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        writer.write(data)
        await writer.drain()


class ControlExtension:
    """Extension object wiring the control socket into the hub lifecycle."""

    name = "security-control"

    def __init__(self, app: Any, server: ControlServer | None) -> None:
        self.app = app
        self.server = server

    async def start(self) -> None:
        if self.server is None:
            return
        try:
            await self.server.start()
        except Exception as exc:
            # A broken control socket must not stop the hub; ``sfo url`` will
            # report that the socket is unavailable instead.
            self.server.log.warning("control socket disabled: %s", exc)
            self.server = None

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.stop()

    def attach_web(self, web: Any) -> None:
        if self.server is not None:
            self.server.attach_web(web)

    def detach_web(self, web: Any = None) -> None:
        if self.server is not None:
            self.server.detach_web(web)

    def health(self) -> dict:
        return {"socket": str(self.server.path) if self.server else None,
                "listening": bool(self._listening())}

    def _listening(self) -> bool:
        if self.server is None:
            return False
        try:
            return self.server.path.is_socket()
        except OSError:
            return False


def build(app: Any) -> ControlExtension:
    """Extension entry point: create the socket under ``<state>/run``."""
    paths = getattr(app, "paths", None)
    run_dir = getattr(paths, "run_dir", None)
    server: ControlServer | None = None
    if run_dir is not None:
        server = ControlServer(control_socket_path(run_dir))
    extension = ControlExtension(app, server)
    try:
        app.security_control = extension
    except Exception:  # pragma: no cover - defensive
        pass
    return extension


# ----------------------------------------------------------------------
# synchronous client (used by ``sfo url`` / ``sfo status``)
# ----------------------------------------------------------------------
def query_control_socket(socket_path: str | os.PathLike[str], action: str = "url",
                         *, timeout: float = 2.0,
                         extra: dict | None = None) -> dict:
    """Send one request to the hub control socket; never raises on failure.

    Returns the decoded reply, or ``{"ok": False, "error": "..."}`` when the
    socket is absent/unreachable/malformed.  The caller must check ``ok``.
    """
    path = Path(socket_path)
    if not path.exists():
        return {"ok": False, "error": "control socket not available"}
    request: dict[str, Any] = {"action": str(action or "").strip().lower()}
    if extra:
        request.update(extra)
    data = json.dumps(request).encode("utf-8") + b"\n"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(float(timeout))
    try:
        sock.connect(str(path))
        sock.sendall(data)
        chunks: list[bytes] = []
        total = 0
        while total < 65536:
            chunk = sock.recv(65536 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    except OSError as exc:
        return {"ok": False, "error": f"control socket error: {exc.strerror or exc}"}
    finally:
        sock.close()
    raw = b"".join(chunks).strip()
    if not raw:
        return {"ok": False, "error": "empty control response"}
    try:
        reply = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "error": "invalid control response"}
    return reply if isinstance(reply, dict) else {
        "ok": False, "error": "invalid control response"}


def request_login_url(run_dir: str | os.PathLike[str]) -> str | None:
    """Convenience for the CLI: return a fresh URL or ``None``."""
    reply = query_control_socket(control_socket_path(run_dir), "url")
    if reply.get("ok") and reply.get("url"):
        return str(reply["url"])
    return None


__all__ = [
    "ControlExtension",
    "ControlServer",
    "DEFAULT_SOCKET_NAME",
    "MAX_REQUEST_BYTES",
    "PEER_CHECK_AVAILABLE",
    "build",
    "control_socket_path",
    "peer_credentials",
    "query_control_socket",
    "request_login_url",
]

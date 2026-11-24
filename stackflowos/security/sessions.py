"""Server-side browser session table and its optional web extension.

The console used to trust a stateless HMAC cookie: it proved *a* token had
been exchanged, but the server could not list, rotate or revoke it.  This
module adds the missing server-side state:

* :class:`SessionManager` - a thread-safe in-memory table of
  ``id -> Session(user, role, created, last_seen, expires)`` with explicit
  ``revoke``/``rotate``/``cleanup`` operations and a hard ``max_sessions``
  bound.  Session ids are random 32-byte URL-safe strings; the cookie carries
  one only inside its signed payload, so an id alone is not a credential.
* :class:`SessionsExtension` - the optional subsystem loaded through
  ``extensions.build(app)``.  It owns the manager (also published on
  ``app.security_sessions`` so :class:`stackflowos.web.server.WebServer` can
  attach at construction time) and exposes the browser-facing endpoints:

  ``GET    /api/sessions``             list (operator: all, viewer: own)
  ``POST   /api/sessions/rotate``      rotate the current cookie (operator)
  ``DELETE /api/sessions/{id}``        revoke one session (operator)
  ``POST   /api/logout``               revoke the current session + clear cookie

Token values and cookie signatures never appear in a response; only session
metadata does.  The extension degrades safely when the running server has not
attached yet (``app.security_web`` missing): metadata endpoints still work,
cookie-issuing endpoints answer 503 instead of pretending to rotate.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..errors import BadRequest, Denied, NotFound, Unavailable

ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"
VALID_ROLES = frozenset({ROLE_OPERATOR, ROLE_VIEWER})

DEFAULT_SESSION_TTL_S = 7 * 86400
DEFAULT_SESSION_MAX = 32


class SessionLimitError(Unavailable):
    """The configured ``web.session_max`` bound was reached (fail closed)."""

    code = "UNAVAILABLE"


@dataclass
class Session:
    """One authenticated browser session."""

    id: str
    user: str
    role: str
    created: float
    last_seen: float
    expires: float
    authority: str = ""
    source: str = "cookie"

    def active(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) < self.expires

    def to_public_dict(self) -> dict:
        """Metadata safe to show an authenticated operator."""
        return {
            "id": self.id,
            "user": self.user,
            "role": self.role,
            "created": self.created,
            "last_seen": self.last_seen,
            "expires": self.expires,
            "authority": self.authority,
            "source": self.source,
        }


class SessionManager:
    """Thread-safe, bounded, in-memory session table.

    ``now`` is injectable so tests can move time without sleeping.  Every
    read that validates a session also performs an opportunistic expired-entry
    sweep, keeping memory bounded even when no request calls ``cleanup``.
    """

    def __init__(self, *, max_sessions: int = DEFAULT_SESSION_MAX,
                 ttl_s: int = DEFAULT_SESSION_TTL_S,
                 now: Callable[[], float] | None = None) -> None:
        self.max_sessions = max(1, int(max_sessions))
        self.ttl_s = max(60, int(ttl_s))
        self._now = now or time.time
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}

    # -- lifecycle -------------------------------------------------------
    def _sweep_locked(self, now: float) -> int:
        expired = [sid for sid, item in self._sessions.items()
                   if not item.active(now)]
        for sid in expired:
            del self._sessions[sid]
        return len(expired)

    def cleanup(self) -> int:
        """Remove expired sessions; returns how many were removed."""
        with self._lock:
            return self._sweep_locked(float(self._now()))

    def create(self, user: str, role: str = ROLE_VIEWER, *,
               authority: str = "", ttl_s: int | None = None,
               session_id: str | None = None,
               source: str = "cookie") -> Session:
        """Insert a new session or raise :class:`SessionLimitError`."""
        name = str(user or "").strip()
        if not name:
            raise BadRequest("session user is required")
        if role not in VALID_ROLES:
            raise BadRequest("session role must be operator|viewer")
        ttl = max(60, int(ttl_s if ttl_s is not None else self.ttl_s))
        with self._lock:
            now = float(self._now())
            self._sweep_locked(now)
            if len(self._sessions) >= self.max_sessions:
                raise SessionLimitError(
                    f"session limit reached ({self.max_sessions})")
            sid = str(session_id or secrets.token_urlsafe(32))
            if sid in self._sessions:
                raise BadRequest("session id already exists")
            item = Session(id=sid, user=name, role=role, created=now,
                           last_seen=now, expires=now + ttl,
                           authority=str(authority or ""),
                           source=str(source or "cookie"))
            self._sessions[sid] = item
            return _copy(item)

    def get(self, session_id: str | None, *, touch: bool = True) -> Session | None:
        """Return an active session, refreshing ``last_seen`` by default."""
        if not session_id:
            return None
        with self._lock:
            now = float(self._now())
            item = self._sessions.get(str(session_id))
            if item is None:
                return None
            if not item.active(now):
                del self._sessions[str(session_id)]
                return None
            if touch:
                item.last_seen = now
            return _copy(item)

    def is_valid(self, session_id: str | None) -> bool:
        return self.get(session_id, touch=False) is not None

    def revoke(self, session_id: str | None) -> bool:
        """Remove one session; returns whether it existed and was active."""
        if not session_id:
            return False
        with self._lock:
            now = float(self._now())
            item = self._sessions.get(str(session_id))
            if item is None:
                return False
            del self._sessions[str(session_id)]
            return item.active(now)

    def revoke_user(self, user: str) -> int:
        """Revoke every session belonging to *user*; returns the count."""
        name = str(user or "")
        with self._lock:
            doomed = [sid for sid, item in self._sessions.items()
                      if item.user == name]
            for sid in doomed:
                del self._sessions[sid]
            return len(doomed)

    def rotate(self, session_id: str | None) -> Session | None:
        """Revoke *session_id* and issue a fresh session with the same claims."""
        old = self.get(session_id, touch=False)
        if old is None:
            return None
        with self._lock:
            self.revoke(old.id)
            return self.create(old.user, old.role, authority=old.authority,
                               ttl_s=max(60, int(old.expires - old.created)),
                               source=old.source)

    # -- introspection ---------------------------------------------------
    def list_sessions(self) -> list[dict]:
        """Active sessions as public metadata, oldest first."""
        with self._lock:
            now = float(self._now())
            self._sweep_locked(now)
            return [item.to_public_dict()
                    for item in sorted(self._sessions.values(),
                                       key=lambda row: row.created)]

    def count(self) -> int:
        with self._lock:
            self._sweep_locked(float(self._now()))
            return len(self._sessions)

    def __len__(self) -> int:  # pragma: no cover - convenience
        return self.count()


def _copy(item: Session) -> Session:
    return Session(**{name: getattr(item, name) for name in item.__dataclass_fields__})


# ----------------------------------------------------------------------
# web extension
# ----------------------------------------------------------------------
def _current_auth():
    try:
        from ..web.api import current_auth

        return current_auth()
    except Exception:  # pragma: no cover - web package always present
        return None


def _web_reply(payload: dict, *, status: int = 200,
               headers: dict[str, str] | None = None):
    from ..web.api import WebReply

    return WebReply(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    content_type="application/json; charset=utf-8",
                    status=status, headers=headers or {})


def _role(auth: Any) -> str:
    role = str(getattr(auth, "role", "") or "")
    return role if role in VALID_ROLES else ""


def _require_operator(auth: Any) -> None:
    if auth is None:
        raise Denied("authentication required")
    if getattr(auth, "authenticated", False) is not True:
        raise Denied("authentication required")
    if _role(auth) != ROLE_OPERATOR:
        raise Denied("operator role required")


def _session_id(auth: Any) -> str:
    return str(getattr(auth, "session_id", "") or "")


class SessionsExtension:
    """Extension object: session manager + ``/api/sessions*`` routes."""

    name = "security-sessions"

    def __init__(self, app: Any, manager: SessionManager) -> None:
        self.app = app
        self.manager = manager

    # extension contract -------------------------------------------------
    async def start(self) -> None:
        self.manager.cleanup()

    async def stop(self) -> None:
        return None

    def health(self) -> dict:
        return {"sessions": self.manager.count(),
                "max_sessions": self.manager.max_sessions}

    def routes(self) -> list[tuple[str, str, Any]]:
        return [
            ("GET", r"/api/sessions", self.sessions_list),
            ("POST", r"/api/sessions/rotate", self.sessions_rotate),
            ("DELETE", r"/api/sessions/([^/]+)", self.sessions_revoke),
            ("POST", r"/api/logout", self.logout),
        ]

    # handlers -----------------------------------------------------------
    async def sessions_list(self, match, query: dict, body: dict):
        auth = _current_auth()
        if auth is None or getattr(auth, "authenticated", False) is not True:
            raise Denied("authentication required")
        rows = self.manager.list_sessions()
        if _role(auth) != ROLE_OPERATOR:
            mine = _session_id(auth)
            rows = [row for row in rows if row["id"] == mine]
        return 200, {"ok": True, "data": {
            "sessions": rows,
            "count": len(rows),
            "max_sessions": self.manager.max_sessions,
        }}

    async def sessions_rotate(self, match, query: dict, body: dict):
        auth = _current_auth()
        _require_operator(auth)
        sid = _session_id(auth)
        if not sid:
            # Bearer (CLI/test) callers have no browser session to rotate.
            raise BadRequest("no browser session to rotate")
        server = getattr(self.app, "security_web", None)
        if server is None:
            raise Unavailable("web server is not attached")
        rotated = self.manager.rotate(sid)
        if rotated is None:
            raise NotFound("session not found")
        header = _cookie_header_for(server, rotated,
                                    authority=str(getattr(auth, "authority", "") or ""))
        if header is None:
            # Roll back rather than leave a session the browser cannot use.
            self.manager.revoke(rotated.id)
            raise Unavailable("session cookie could not be issued")
        return 200, _web_reply(
            {"ok": True, "data": {
                "session": rotated.to_public_dict(),
                "rotated": True,
            }}, headers={"Set-Cookie": header})

    async def sessions_revoke(self, match, query: dict, body: dict):
        auth = _current_auth()
        _require_operator(auth)
        target = str(match.group(1) or "")
        if not target:
            raise BadRequest("session id is required")
        if not self.manager.revoke(target):
            raise NotFound("session not found")
        return 200, {"ok": True, "data": {"revoked": target}}

    async def logout(self, match, query: dict, body: dict):
        auth = _current_auth()
        if auth is None or getattr(auth, "authenticated", False) is not True:
            raise Denied("authentication required")
        sid = _session_id(auth)
        revoked = bool(sid) and self.manager.revoke(sid)
        server = getattr(self.app, "security_web", None)
        clear = _clear_cookie_header(server)
        if clear is None:
            return 200, {"ok": True, "data": {"logged_out": True,
                                              "revoked": revoked}}
        return 200, _web_reply(
            {"ok": True, "data": {"logged_out": True, "revoked": revoked}},
            headers={"Set-Cookie": clear})


def _cookie_header_for(server: Any, session: Session, *, authority: str) -> str | None:
    auth = getattr(server, "auth", None)
    issue = getattr(auth, "set_cookie_header_for_session", None)
    if issue is None:
        return None
    try:
        return str(issue(session, authority=authority))
    except Exception:
        return None


def _clear_cookie_header(server: Any) -> str | None:
    if server is None:
        return None
    auth = getattr(server, "auth", None)
    clear = getattr(auth, "clear_cookie_header", None)
    if clear is None:
        return None
    try:
        return str(clear())
    except Exception:
        return None


def build(app: Any) -> SessionsExtension:
    """Extension entry point: create/attach the shared session manager."""
    cfg = getattr(app, "config", None)
    web = getattr(cfg, "web", None)
    max_sessions = int(getattr(web, "session_max", DEFAULT_SESSION_MAX) or
                       DEFAULT_SESSION_MAX)
    ttl_days = int(getattr(web, "cookie_ttl_days", 7) or 7)
    manager = getattr(app, "security_sessions", None)
    if not isinstance(manager, SessionManager):
        manager = SessionManager(max_sessions=max_sessions,
                                 ttl_s=max(60, ttl_days * 86400))
        try:
            app.security_sessions = manager
        except Exception:  # pragma: no cover - defensive
            pass
    extension = SessionsExtension(app, manager)
    try:
        app.security_sessions_extension = extension
    except Exception:  # pragma: no cover
        pass
    return extension


__all__ = [
    "DEFAULT_SESSION_MAX",
    "DEFAULT_SESSION_TTL_S",
    "ROLE_OPERATOR",
    "ROLE_VIEWER",
    "VALID_ROLES",
    "Session",
    "SessionLimitError",
    "SessionManager",
    "SessionsExtension",
    "build",
]

"""Browser admission control for the operator console (security fix H1/H3).

The console is an operator control plane.  A per-process launch token is
exchanged for an HMAC-signed, ``HttpOnly`` cookie; every request additionally
has to pass Host/Origin/CSRF admission checks before it reaches the router.

Design notes:

* The launch token and the cookie signing key are generated per process with
  :mod:`secrets` and only ever live in memory.  They are never written to disk
  and never logged; the CLI prints the login URL only on a TTY.
* Launch tokens are **single-use for the URL exchange path**: once
  ``GET /?token=...`` has been redeemed the old token can no longer be
  exchanged again.  ``Authorization: Bearer`` is the explicit CLI/test API
  path and keeps accepting the in-memory process secret (a deliberate,
  documented trade-off: CLI commands and the existing test-suite send the same
  token on every request).  ``sfo url`` mints a fresh single-use token through
  the operator control socket.
* Cookies are bound to the ``Host`` authority so a DNS-rebinding attacker
  cannot reuse a cookie issued for ``127.0.0.1``.  They also carry the
  server-side session id, user and role; the session table can list, rotate
  and revoke them.  ``Secure`` is set automatically on an HTTPS listener.
* ``config.web.users`` provides long-lived bearer accounts
  (``token_sha256`` + ``operator``/``viewer``).  Comparisons use
  :func:`hmac.compare_digest`; a viewer cookie/token may only issue GET/HEAD
  requests (the server enforces the write side, so a route cannot forget).
* Anonymous static assets (CSS/JS/images/favicon) still pass Host/Origin
  admission, but carry no data.  The same holds for the exact ``/healthz``
  liveness probe: a process supervisor (systemd ``ExecStartPost``, health
  timer) cannot know the per-process token, so that one path answers without
  a session while still returning no project data.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlsplit

from ..errors import Unavailable
from ..security.sessions import (
    ROLE_OPERATOR,
    ROLE_VIEWER,
    VALID_ROLES,
    Session,
    SessionManager,
)

COOKIE_NAME = "sfo_session"
COOKIE_VERSION = "v1"
DEFAULT_COOKIE_TTL_DAYS = 7

# The console UI is built on the Astryx design system, whose runtime injects
# one <style> element (anchored-overlay position fallbacks) and sets CSS
# custom properties on elements for size/position props.  A strict
# ``style-src 'self'`` blocks both and silently breaks overlays, so styles are
# allowed inline while *scripts* stay same-origin only: injected CSS cannot
# execute JavaScript, and every value the console renders is text (no
# ``innerHTML`` from server data), so the XSS ceiling is unchanged.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'; object-src 'none'"
)
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=()"

#: Static assets that are safe to serve without a session.  They contain no
#: project data; every other path (including the app shell and ``/api/*``) is
#: authenticated.
ANONYMOUS_PREFIXES = ("/css/", "/js/", "/img/")

#: Exact paths that answer without a session.  ``/healthz`` is the liveness
#: probe used by the process supervisor: systemd cannot know the per-process
#: launch token, and probing with a persisted token would make startup depend
#: on that secret.  The handler returns only ``status``/``transport`` (never
#: project data) and still passes Host/Origin admission; every ``/api/*`` path
#: stays authenticated.
ANONYMOUS_PATHS = frozenset({"/healthz"})

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_LOOPBACK_OR_WILDCARD_BIND = {
    "", "127.0.0.1", "localhost", "::1", "0.0.0.0", "::",
}
_MAX_TOKEN_LENGTH = 4096


def security_headers(*, cache: str = "no-store") -> str:
    """Response headers shared by HTML, JSON, SSE and every rejection."""
    return (
        f"Content-Security-Policy: {CONTENT_SECURITY_POLICY}\r\n"
        "X-Frame-Options: DENY\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "Referrer-Policy: no-referrer\r\n"
        "Cross-Origin-Opener-Policy: same-origin\r\n"
        "Cross-Origin-Resource-Policy: same-origin\r\n"
        f"Permissions-Policy: {PERMISSIONS_POLICY}\r\n"
        f"Cache-Control: {cache}\r\n"
    )


@dataclass(frozen=True)
class AdmissionDecision:
    """Outcome of one admission check.

    ``user``/``role``/``session_id`` are consumed by ``WebAPI`` handlers via
    ``current_auth()``.  ``role`` defaults to ``operator`` so older/fake app
    objects (and tests that construct decisions by hand) keep working; an
    anonymous decision explicitly carries an empty role and is never treated
    as privileged by the server.
    """

    allowed: bool
    authenticated: bool = False
    login: bool = False
    status: int = 200
    code: str = ""
    message: str = ""
    user: str = ""
    role: str = ROLE_OPERATOR
    session_id: str = ""
    authority: str = ""

    @classmethod
    def allow(cls, *, authenticated: bool = False,
              login: bool = False, user: str = "",
              role: str = ROLE_OPERATOR, session_id: str = "",
              authority: str = "") -> "AdmissionDecision":
        return cls(allowed=True, authenticated=authenticated, login=login,
                   user=user, role=role if authenticated else "",
                   session_id=session_id, authority=authority)

    @classmethod
    def deny(cls, status: int, code: str, message: str) -> "AdmissionDecision":
        return cls(allowed=False, status=status, code=code, message=message)


@dataclass(frozen=True)
class CookieClaims:
    user: str
    role: str
    session_id: str
    authority: str


class WebAuth:
    """Launch-token / cookie authentication plus request admission rules."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 17800,
                 trusted_hosts: Iterable[str] = (),
                 cookie_ttl_days: int = DEFAULT_COOKIE_TTL_DAYS,
                 secure: bool = False,
                 launch_token: str | None = None,
                 users: Iterable[dict] | None = None,
                 sessions: SessionManager | None = None,
                 single_use: bool = True,
                 scheme: str = "http",
                 now: Callable[[], float] | None = None) -> None:
        self.host = host or ""
        self.port = int(port)
        self.scheme = "https" if str(scheme).lower() == "https" else "http"
        self.trusted_hosts = tuple(
            str(item).strip() for item in trusted_hosts if str(item).strip()
        )
        self.cookie_ttl_s = max(60, int(cookie_ttl_days) * 86400)
        self.secure = bool(secure)
        # A per-process token; SFO_WEB_TOKEN may override it explicitly for
        # non-TTY deployments (never persisted by us).
        self.launch_token = launch_token or secrets.token_urlsafe(32)
        self.single_use = bool(single_use)
        self.sessions = sessions
        self.users = _normalize_users(users)
        self._key = secrets.token_bytes(32)
        self._now = now or time.time
        # Only tokens minted for the current process are exchangeable in
        # addition to the base token.  The base token stays valid as a Bearer
        # secret even after its URL exchange (see module docstring).
        self._lock = threading.Lock()
        self._minted: set[str] = set()
        self._base_exchanged = False

    # ------------------------------------------------------------------
    # token helpers
    # ------------------------------------------------------------------
    def mint_launch_token(self) -> str:
        """Create a fresh launch token registered for the URL exchange."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._minted.add(token)
        return token

    def login_url_for(self, token: str, *, scheme: str | None = None,
                      host: str | None = None,
                      port: int | None = None) -> str:
        """Format the browser login URL for *token*."""
        scheme = self.scheme if scheme is None else str(scheme)
        name = self.host if host is None else str(host)
        number = self.port if port is None else int(port)
        authority = _display_host(name)
        return f"{scheme}://{authority}:{number}/?token={quote(str(token), safe='')}"

    def check_launch_token(self, token: str | None) -> bool:
        """True when *token* can still be exchanged for a cookie."""
        if not _usable_token(token):
            return False
        with self._lock:
            if not self._base_exchanged and _same_token(token, self.launch_token):
                return True
            return any(_same_token(token, item) for item in self._minted)

    def check_bearer_token(self, token: str | None) -> bool:
        """True when *token* is an in-memory process secret (CLI/test path)."""
        if not _usable_token(token):
            return False
        with self._lock:
            if _same_token(token, self.launch_token):
                # Deliberately *not* conditioned on ``_base_exchanged``: the
                # Bearer path is the explicit CLI/test API (Lead constraint).
                return True
            return any(_same_token(token, item) for item in self._minted)

    def exchange_launch_token(self, token: str | None) -> bool:
        """Redeem a launch token, invalidating it when single-use is on."""
        if not _usable_token(token):
            return False
        with self._lock:
            if not self._base_exchanged and _same_token(token, self.launch_token):
                if self.single_use:
                    self._base_exchanged = True
                return True
            for item in tuple(self._minted):
                if _same_token(token, item):
                    if self.single_use:
                        self._minted.discard(item)
                    return True
        return False

    def authenticate_bearer(self, header: str | None) -> tuple[str, str] | None:
        """Resolve a ``Bearer`` header to ``(user, role)`` or ``None``."""
        token = _bearer_token(header)
        if token is None:
            return None
        if self.check_bearer_token(token):
            return "launch-token", ROLE_OPERATOR
        return self._user_token_claims(token)

    def _user_token_claims(self, token: str | None) -> tuple[str, str] | None:
        if not _usable_token(token):
            return None
        try:
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        except UnicodeEncodeError:  # pragma: no cover - surrogates
            return None
        match: tuple[str, str] | None = None
        # Compare against every configured account (constant-time per entry)
        # so an early match does not leak account position/time.
        for name, (expected, role) in self.users.items():
            if hmac.compare_digest(digest, expected):
                match = (name, role)
        return match

    # ------------------------------------------------------------------
    # authority helpers
    # ------------------------------------------------------------------
    def allowed_authorities(self) -> set[str]:
        """Exact ``host:port`` authorities accepted in the ``Host`` header."""
        port = self.port
        authorities = {f"{name}:{port}" for name in _LOOPBACK_HOSTS}
        bind = (self.host or "").strip()
        if bind.startswith("[") and bind.endswith("]"):
            bind = bind[1:-1]
        if bind not in _LOOPBACK_OR_WILDCARD_BIND:
            authorities.add(_authority(bind, port))
        for entry in self.trusted_hosts:
            parsed = _trusted_authority(entry, port)
            if parsed:
                authorities.add(parsed)
        return {item.lower() for item in authorities}

    def authority_allowed(self, host: str | None) -> bool:
        host = (host or "").strip()
        if not host or len(host) > 255:
            return False
        # Exact-match against the derived allow-list.  This also rejects
        # userinfo ("a@b"), paths, spaces and comma-separated duplicates.
        if any(ch in host for ch in " \t\r\n/@\\,;\"'<>"):
            return False
        return host.lower() in self.allowed_authorities()

    def origin_allowed(self, origin: str | None, host: str | None) -> bool:
        if origin is None:
            return True
        origin = origin.strip()
        if not origin or origin.lower() == "null":
            return False
        try:
            parts = urlsplit(origin)
        except ValueError:
            return False
        if parts.scheme.lower() not in ("http", "https"):
            return False
        if parts.username or parts.password:
            return False
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            return False
        return parts.netloc.lower() == (host or "").strip().lower()

    # ------------------------------------------------------------------
    # cookies
    # ------------------------------------------------------------------
    def issue_cookie(self, authority: str, *, user: str = "launch-token",
                     role: str = ROLE_OPERATOR,
                     session_id: str | None = None) -> str:
        """Sign a cookie payload, creating a server-side session when needed."""
        authority = (authority or "").strip().lower()
        if role not in VALID_ROLES:
            role = ROLE_VIEWER
        sid = str(session_id or "")
        if self.sessions is not None and not sid:
            session = self.sessions.create(
                user, role, authority=authority, ttl_s=self.cookie_ttl_s)
            sid = session.id
        elif self.sessions is not None and sid:
            if self.sessions.get(sid, touch=False) is None:
                raise Unavailable("session is no longer valid")
        issued = int(self._now())
        payload = {
            "authority": authority,
            "issuedAt": issued,
            "expiresAt": issued + self.cookie_ttl_s,
            "nonce": secrets.token_hex(16),
            "user": str(user or "launch-token"),
            "role": role,
        }
        if sid:
            payload["sid"] = sid
        payload_b64 = _b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        signature = _b64encode(self._sign(payload_b64))
        return f"{COOKIE_VERSION}.{payload_b64}.{signature}"

    def _cookie_header(self, value: str) -> str:
        parts = [
            f"{COOKIE_NAME}={value}",
            "HttpOnly",
            "SameSite=Strict",
            "Path=/",
            f"Max-Age={self.cookie_ttl_s}",
        ]
        if self.secure:
            parts.append("Secure")
        return "; ".join(parts)

    def set_cookie_header(self, authority: str, *, user: str = "launch-token",
                          role: str = ROLE_OPERATOR,
                          session_id: str | None = None) -> str:
        return self._cookie_header(self.issue_cookie(
            authority, user=user, role=role, session_id=session_id))

    def set_cookie_header_for_session(self, session: Session, *,
                                      authority: str = "") -> str:
        bind = str(authority or session.authority or "").strip().lower()
        value = self.issue_cookie(bind, user=session.user, role=session.role,
                                  session_id=session.id)
        return self._cookie_header(value)

    def clear_cookie_header(self) -> str:
        parts = [
            f"{COOKIE_NAME}=",
            "HttpOnly",
            "SameSite=Strict",
            "Path=/",
            "Max-Age=0",
        ]
        if self.secure:
            parts.append("Secure")
        return "; ".join(parts)

    def verify_cookie(self, value: str | None, authority: str | None) -> bool:
        return self.cookie_claims(value, authority) is not None

    def cookie_claims(self, value: str | None,
                      authority: str | None) -> CookieClaims | None:
        if not isinstance(value, str) or not value:
            return None
        bits = value.split(".")
        if len(bits) != 3 or bits[0] != COOKIE_VERSION:
            return None
        payload_b64, signature_b64 = bits[1], bits[2]
        try:
            signature = _b64decode(signature_b64)
        except (ValueError, TypeError):
            return None
        if not hmac.compare_digest(signature, self._sign(payload_b64)):
            return None
        try:
            payload = json.loads(_b64decode(payload_b64).decode("utf-8"))
        except (ValueError, TypeError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        expected_authority = (authority or "").strip().lower()
        if payload.get("authority") != expected_authority:
            return None
        try:
            issued_at = float(payload["issuedAt"])
            expires_at = float(payload["expiresAt"])
        except (KeyError, TypeError, ValueError):
            return None
        now = float(self._now())
        if issued_at > now + 60:
            return None
        if expires_at <= now:
            return None
        if not payload.get("nonce"):
            return None
        user = payload.get("user")
        role = payload.get("role")
        sid = str(payload.get("sid") or "")
        if not isinstance(user, str) or not user:
            return None
        if role not in VALID_ROLES:
            return None
        if self.sessions is not None:
            # Fail closed: a signed cookie must still map to a live server-side
            # session.  A cookie without ``sid`` predates the session table and
            # is not accepted by a server that has one.
            if not sid:
                return None
            session = self.sessions.get(sid, touch=True)
            if session is None:
                return None
            if session.user != user or session.role != role:
                return None
        return CookieClaims(user=user, role=role, session_id=sid,
                            authority=expected_authority)

    def _sign(self, payload_b64: str) -> bytes:
        message = f"{COOKIE_VERSION}.{payload_b64}".encode("ascii")
        return hmac.new(self._key, message, hashlib.sha256).digest()

    # ------------------------------------------------------------------
    # authentication / admission
    # ------------------------------------------------------------------
    def authenticate(self, headers: dict,
                     host: str | None) -> tuple[str, str, str] | None:
        """Return ``(user, role, session_id)`` for a valid request credential."""
        bearer = self.authenticate_bearer(headers.get("authorization"))
        if bearer is not None:
            return bearer[0], bearer[1], ""
        claims = self.cookie_claims(
            _cookie_value(headers.get("cookie"), COOKIE_NAME), host)
        if claims is None:
            return None
        return claims.user, claims.role, claims.session_id

    def is_authenticated(self, headers: dict, host: str | None) -> bool:
        return self.authenticate(headers, host) is not None

    def requires_auth(self, path: str) -> bool:
        if path in ANONYMOUS_PATHS:
            return False
        if path in ("/", "", "/index.html"):
            return True
        if path == "/favicon.ico":
            return False
        return not path.startswith(ANONYMOUS_PREFIXES)

    def check(self, method: str, path: str, headers: dict,
              token_values: Iterable[str] = ()) -> AdmissionDecision:
        """Apply Host/Origin/CSRF/auth rules to one request.

        Order matters: authority and origin are checked first (DNS rebinding /
        CSRF), then authentication (so unauthenticated writes are always 401
        rather than leaking body-parser behaviour), then the JSON/CSRF header
        requirements for non-GET/HEAD methods.
        """
        method = (method or "").upper()
        host = headers.get("host") or ""
        if not self.authority_allowed(host):
            return AdmissionDecision.deny(
                403, "FORBIDDEN", "request host is not allowed")
        if method == "OPTIONS":
            # No CORS preflight: cross-site JSON writes cannot get a green
            # light, and the response below never carries Access-Control-*.
            return AdmissionDecision.deny(
                405, "METHOD_NOT_ALLOWED", "preflight requests are not allowed")
        origin = headers.get("origin")
        if origin is not None and not self.origin_allowed(origin, host):
            return AdmissionDecision.deny(
                403, "FORBIDDEN", "request origin is not allowed")
        if (headers.get("sec-fetch-site") or "").strip().lower() == "cross-site":
            return AdmissionDecision.deny(
                403, "FORBIDDEN", "cross-site requests are not allowed")

        tokens = [str(item) for item in token_values]
        if tokens:
            if method != "GET" or path != "/":
                return AdmissionDecision.deny(
                    401, "UNAUTHORIZED",
                    "launch tokens are only accepted at GET /?token=...")
            if len(tokens) != 1:
                return AdmissionDecision.deny(
                    400, "BAD_REQUEST", "exactly one launch token is allowed")
            token = tokens[0]
            if self.exchange_launch_token(token):
                return AdmissionDecision.allow(
                    authenticated=True, login=True, user="launch-token",
                    role=ROLE_OPERATOR, authority=host)
            # Long-lived ``config.web.users`` accounts may also log in with
            # their bearer token; the account token itself is not consumed.
            claims = self._user_token_claims(token)
            if claims is not None:
                return AdmissionDecision.allow(
                    authenticated=True, login=True, user=claims[0],
                    role=claims[1], authority=host)
            return AdmissionDecision.deny(
                401, "UNAUTHORIZED", "invalid launch token")

        claims = self.authenticate(headers, host)
        if self.requires_auth(path) and claims is None:
            return AdmissionDecision.deny(
                401, "UNAUTHORIZED", "authentication required")
        # Viewer accounts are read-only.  Reject before the Content-Type /
        # CSRF checks so a viewer write is always 403 (never a confusing 415);
        # the server repeats this check as defence in depth.
        if (claims is not None and claims[1] != ROLE_OPERATOR
                and method not in ("GET", "HEAD")
                and path != "/api/logout"):
            return AdmissionDecision.deny(
                403, "FORBIDDEN", "viewer role is read-only")

        if method not in ("GET", "HEAD"):
            content_type = (headers.get("content-type") or "")
            content_type = content_type.split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return AdmissionDecision.deny(
                    415, "UNSUPPORTED_MEDIA_TYPE",
                    "Content-Type must be application/json")
            if (headers.get("x-sfo-request") or "").strip() != "1":
                return AdmissionDecision.deny(
                    403, "FORBIDDEN", "missing X-SFO-Request header")
        if claims is None:
            return AdmissionDecision.allow(authenticated=False, authority=host)
        return AdmissionDecision.allow(
            authenticated=True, user=claims[0], role=claims[1],
            session_id=claims[2], authority=host)


# ----------------------------------------------------------------------
# small helpers (kept module-level so tests can exercise them directly)
# ----------------------------------------------------------------------
def _normalize_users(users: Iterable[dict] | None) -> dict[str, tuple[str, str]]:
    """Validate ``config.web.users`` defensively; invalid entries are skipped."""
    table: dict[str, tuple[str, str]] = {}
    for entry in users or ():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        digest = str(entry.get("token_sha256") or "").strip().lower()
        role = str(entry.get("role") or ROLE_VIEWER)
        if (not name or len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)
                or role not in VALID_ROLES):
            continue
        table.setdefault(name, (digest, role))
    return table


def _usable_token(token: str | None) -> bool:
    return (isinstance(token, str) and 0 < len(token) <= _MAX_TOKEN_LENGTH)


def _same_token(provided: str, expected: str) -> bool:
    try:
        left = provided.encode("utf-8")
        right = expected.encode("utf-8")
    except UnicodeEncodeError:  # pragma: no cover - surrogates
        return False
    return hmac.compare_digest(left, right)


def _display_host(host: str) -> str:
    host = (host or "").strip()
    if host in ("", "0.0.0.0", "::"):
        return "127.0.0.1"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    text = text.strip()
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _authority(host: str, port: int) -> str:
    host = host.strip()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{int(port)}"


def _trusted_authority(entry: str, default_port: int) -> str | None:
    """Normalise a ``web.trusted_hosts`` entry to ``host:port``."""
    entry = (entry or "").strip()
    if not entry:
        return None
    if "://" in entry:
        try:
            entry = urlsplit(entry).netloc
        except ValueError:
            return None
    if not entry or any(ch in entry for ch in " \t\r\n/@\\,;\"'<>"):
        return None
    if entry.startswith("["):
        end = entry.find("]")
        if end < 0:
            return None
        host, rest = entry[: end + 1], entry[end + 1:]
        if rest.startswith(":"):
            port = rest[1:]
        elif rest == "":
            port = str(int(default_port))
        else:
            return None
        return f"{host}:{port}" if str(port).isdigit() else None
    if ":" in entry:
        host, _, port = entry.rpartition(":")
        if not host or not port.isdigit():
            return None
        return f"{host}:{port}"
    return f"{entry}:{int(default_port)}"


def _bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    scheme, separator, value = header.strip().partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def _cookie_value(header: str | None, name: str) -> str | None:
    for chunk in (header or "").split(";"):
        key, separator, value = chunk.strip().partition("=")
        if separator and key == name:
            return value
    return None


__all__ = [
    "ANONYMOUS_PATHS",
    "ANONYMOUS_PREFIXES",
    "AdmissionDecision",
    "COOKIE_NAME",
    "CONTENT_SECURITY_POLICY",
    "CookieClaims",
    "PERMISSIONS_POLICY",
    "WebAuth",
    "security_headers",
]

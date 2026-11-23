"""Browser admission control for the operator console (security fix H1).

The console is a single-user control plane.  A per-process launch token is
exchanged for an HMAC-signed, ``HttpOnly`` cookie; every request additionally
has to pass Host/Origin/CSRF admission checks before it reaches the router.

Design notes:

* The launch token and the cookie signing key are generated per process with
  :mod:`secrets` and only ever live in memory.  They are never written to disk
  and never logged; the CLI prints the login URL only on a TTY.
* Cookies are bound to the ``Host`` authority so a DNS-rebinding attacker
  cannot reuse a cookie issued for ``127.0.0.1``.
* ``Authorization: Bearer <launch_token>`` is the test/CLI path; it does not
  depend on cookies.
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
import time
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlsplit

COOKIE_NAME = "sfo_session"
COOKIE_VERSION = "v1"
DEFAULT_COOKIE_TTL_DAYS = 7

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
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
    """Outcome of one admission check."""

    allowed: bool
    authenticated: bool = False
    login: bool = False
    status: int = 200
    code: str = ""
    message: str = ""

    @classmethod
    def allow(cls, *, authenticated: bool = False,
              login: bool = False) -> "AdmissionDecision":
        return cls(allowed=True, authenticated=authenticated, login=login)

    @classmethod
    def deny(cls, status: int, code: str, message: str) -> "AdmissionDecision":
        return cls(allowed=False, status=status, code=code, message=message)


class WebAuth:
    """Launch-token / cookie authentication plus request admission rules."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 17800,
                 trusted_hosts: Iterable[str] = (),
                 cookie_ttl_days: int = DEFAULT_COOKIE_TTL_DAYS,
                 secure: bool = False,
                 launch_token: str | None = None,
                 now: Callable[[], float] | None = None) -> None:
        self.host = host or ""
        self.port = int(port)
        self.trusted_hosts = tuple(
            str(item).strip() for item in trusted_hosts if str(item).strip()
        )
        self.cookie_ttl_s = max(60, int(cookie_ttl_days) * 86400)
        self.secure = bool(secure)
        # A per-process token; SFO_WEB_TOKEN may override it explicitly for
        # non-TTY deployments (never persisted by us).
        self.launch_token = launch_token or secrets.token_urlsafe(32)
        self._key = secrets.token_bytes(32)
        self._now = now or time.time

    # ------------------------------------------------------------------
    # token / authority helpers
    # ------------------------------------------------------------------
    def check_launch_token(self, token: str | None) -> bool:
        if not isinstance(token, str) or not token:
            return False
        try:
            provided = token.encode("utf-8")
        except (UnicodeEncodeError, AttributeError):
            return False
        return hmac.compare_digest(provided, self.launch_token.encode("ascii"))

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
    def issue_cookie(self, authority: str) -> str:
        issued = int(self._now())
        payload = {
            "authority": (authority or "").strip().lower(),
            "issuedAt": issued,
            "expiresAt": issued + self.cookie_ttl_s,
            "nonce": secrets.token_hex(16),
        }
        payload_b64 = _b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        signature = _b64encode(self._sign(payload_b64))
        return f"{COOKIE_VERSION}.{payload_b64}.{signature}"

    def set_cookie_header(self, authority: str) -> str:
        value = self.issue_cookie(authority)
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

    def verify_cookie(self, value: str | None, authority: str | None) -> bool:
        if not isinstance(value, str) or not value:
            return False
        bits = value.split(".")
        if len(bits) != 3 or bits[0] != COOKIE_VERSION:
            return False
        payload_b64, signature_b64 = bits[1], bits[2]
        try:
            signature = _b64decode(signature_b64)
        except (ValueError, TypeError):
            return False
        if not hmac.compare_digest(signature, self._sign(payload_b64)):
            return False
        try:
            payload = json.loads(_b64decode(payload_b64).decode("utf-8"))
        except (ValueError, TypeError, UnicodeDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        if payload.get("authority") != (authority or "").strip().lower():
            return False
        try:
            issued_at = float(payload["issuedAt"])
            expires_at = float(payload["expiresAt"])
        except (KeyError, TypeError, ValueError):
            return False
        now = float(self._now())
        if issued_at > now + 60:
            return False
        if expires_at <= now:
            return False
        return bool(payload.get("nonce"))

    def _sign(self, payload_b64: str) -> bytes:
        message = f"{COOKIE_VERSION}.{payload_b64}".encode("ascii")
        return hmac.new(self._key, message, hashlib.sha256).digest()

    # ------------------------------------------------------------------
    # authentication / admission
    # ------------------------------------------------------------------
    def is_authenticated(self, headers: dict, host: str | None) -> bool:
        bearer = _bearer_token(headers.get("authorization"))
        if bearer is not None and self.check_launch_token(bearer):
            return True
        cookie = _cookie_value(headers.get("cookie"), COOKIE_NAME)
        return bool(cookie) and self.verify_cookie(cookie, host)

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
            if not self.check_launch_token(tokens[0]):
                return AdmissionDecision.deny(
                    401, "UNAUTHORIZED", "invalid launch token")
            return AdmissionDecision.allow(authenticated=True, login=True)

        authenticated = self.is_authenticated(headers, host)
        if self.requires_auth(path) and not authenticated:
            return AdmissionDecision.deny(
                401, "UNAUTHORIZED", "authentication required")

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
        return AdmissionDecision.allow(authenticated=authenticated)


# ----------------------------------------------------------------------
# small helpers (kept module-level so tests can exercise them directly)
# ----------------------------------------------------------------------
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

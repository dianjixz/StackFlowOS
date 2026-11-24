"""Outbound URL and private-network guards (M4, SSRF).

This module is deliberately dependency-free and lives in ``util`` so both
:mod:`stackflowos.security.capabilities` (risk escalation) and
:mod:`stackflowos.agents.tools` (``net.fetch``) share one rule set. The tool
executor must not import the ``security`` package, so a shared helper here is
the only way to keep the two checks consistent.

Nothing here performs I/O except :func:`resolve_host_addresses`, which uses
``socket.getaddrinfo``. Redirect following and response handling stay in the
tool executor.
"""

from __future__ import annotations

import functools
import http.client
import ipaddress
import socket
import ssl
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence
from urllib.parse import SplitResult, urlsplit

#: Hard bound on an accepted URL (initial target and every redirect hop).
MAX_URL_LENGTH = 4096
#: Only these schemes may be fetched or redirected to.
ALLOWED_SCHEMES = ("http", "https")
#: Exact error text required by the M4 contract/tests.
PRIVATE_NETWORK_ERROR = "private network is disabled"

#: Control characters never belong in a URL we are about to request.
_UNSAFE_URL_CHARS = frozenset("\r\n\t\x00")
#: Names that always denote the local machine.
_LOCAL_NAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
})
#: Suffixes reserved for the local network / mDNS.
_LOCAL_SUFFIXES = (".local", ".localhost")


class InvalidTarget(ValueError):
    """A URL or one of its resolved addresses is not an allowed target."""


def is_private_address(address: ipaddress.IPv4Address
                       | ipaddress.IPv6Address) -> bool:
    """True for any address that is not a public, globally routable one.

    Covers loopback, RFC1918, link-local (including the ``169.254.169.254``
    metadata service), ``0.0.0.0``/``::``, ``fc00::/7``, ``fe80::/10``,
    reserved/unspecified/multicast ranges and IPv4-mapped IPv6 addresses.
    """
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        # ``::ffff:127.0.0.1`` must be judged as the embedded IPv4 address.
        return is_private_address(mapped)
    return bool(
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
        or not address.is_global
    )


def is_private_host(host: str) -> bool:
    """Name-level private check (no DNS): literals, localhost, ``.local``.

    Used by risk escalation, where a DNS lookup would block the approval path.
    The executor additionally resolves the name and checks every address.
    """
    value = str(host or "").strip().strip("[]").lower().rstrip(".")
    if not value:
        return False
    if value in _LOCAL_NAMES or value.endswith(_LOCAL_SUFFIXES):
        return True
    value = value.split("%", 1)[0]  # drop an IPv6 zone id
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        # Single-label names (``intranet``) are internal by convention.
        return "." not in value
    return is_private_address(address)


def parse_fetch_url(url: str) -> SplitResult:
    """Parse and shape-check a URL without touching the network."""
    if not isinstance(url, str) or not url:
        raise InvalidTarget("url is required")
    if len(url) > MAX_URL_LENGTH:
        raise InvalidTarget(
            f"URL is too long (max {MAX_URL_LENGTH} characters)")
    if any(ch in _UNSAFE_URL_CHARS or ord(ch) < 0x20 or ord(ch) == 0x7F
           for ch in url):
        raise InvalidTarget("URL contains control characters")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise InvalidTarget(f"invalid URL: {url!r}") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidTarget(f"only http/https URLs are allowed: {url!r}")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidTarget("URL must not contain credentials (userinfo)")
    if not parsed.netloc or not parsed.hostname:
        raise InvalidTarget(f"URL has no host: {url!r}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidTarget(f"invalid port in URL: {url!r}") from exc
    if port is not None and not (1 <= port <= 65535):
        raise InvalidTarget(f"invalid port in URL: {url!r}")
    return parsed


def resolve_host_addresses(host: str,
                           port: int | None = None
                           ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve ``host`` to every address ``getaddrinfo`` returns."""
    try:
        infos = socket.getaddrinfo(host, port if port is not None else 80,
                                   type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise InvalidTarget(f"cannot resolve host {host!r}: {exc}") from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4]
        raw = str(sockaddr[0]) if sockaddr else ""
        raw = raw.split("%", 1)[0]
        try:
            addresses.append(ipaddress.ip_address(raw))
        except ValueError as exc:
            raise InvalidTarget(
                f"cannot parse resolved address for {host!r}") from exc
    if not addresses:
        raise InvalidTarget(f"cannot resolve host {host!r}")
    return addresses


def assert_public_target(host: str, port: int | None = None) -> None:
    """Reject a host that is (or resolves to) a non-public address."""
    if is_private_host(host):
        raise InvalidTarget(PRIVATE_NETWORK_ERROR)
    for address in resolve_host_addresses(host, port):
        if is_private_address(address):
            # DNS rebinding between this check and the connect is a known
            # residual (TOCTOU); the redirect hop gets its own check.
            raise InvalidTarget(PRIVATE_NETWORK_ERROR)


def validate_fetch_url(url: str, *, allow_private_network: bool) -> SplitResult:
    """Full pre-flight check for one ``net.fetch`` target or redirect hop."""
    parsed = parse_fetch_url(url)
    if not allow_private_network:
        assert_public_target(parsed.hostname or "", parsed.port)
    return parsed




# ----------------------------------------------------------------------
# DNS-pinned connections (M4 follow-up: close the rebinding TOCTOU)
# ----------------------------------------------------------------------
#: Default ports used when a URL omits one.
DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class PinnedTarget:
    """A URL resolved exactly once to a validated set of addresses."""

    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]

    @property
    def host_header(self) -> str:
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        default = DEFAULT_PORTS.get(self.scheme)
        return host if self.port == default else f"{host}:{self.port}"


def resolve_target(url: str, *, allow_private_network: bool) -> PinnedTarget:
    """Parse *url* and resolve it once, validating every returned address.

    Connecting to the addresses stored here (rather than re-resolving in
    ``http.client``) is what closes the DNS-rebinding window.
    """
    parsed = parse_fetch_url(url)
    host = parsed.hostname or ""
    port = parsed.port or DEFAULT_PORTS[(parsed.scheme or "http").lower()]
    if not allow_private_network and is_private_host(host):
        raise InvalidTarget(PRIVATE_NETWORK_ERROR)
    addresses = tuple(resolve_host_addresses(host, port))
    if not allow_private_network:
        for address in addresses:
            if is_private_address(address):
                raise InvalidTarget(PRIVATE_NETWORK_ERROR)
    return PinnedTarget(url=url, scheme=(parsed.scheme or "").lower(),
                        host=host, port=int(port), addresses=addresses)


def _socket_addresses(
    host: str, port: int, *, allow_private_network: bool
) -> list[tuple[int, tuple]]:
    """One ``getaddrinfo`` call -> validated, deduplicated connect targets."""
    if not allow_private_network and is_private_host(host):
        raise InvalidTarget(PRIVATE_NETWORK_ERROR)
    infos = resolve_host_addresses(host, port)
    if not allow_private_network:
        for address in infos:
            if is_private_address(address):
                raise InvalidTarget(PRIVATE_NETWORK_ERROR)
    seen: set[str] = set()
    result: list[tuple[int, tuple]] = []
    for address in infos:
        key = str(address)
        if key in seen:
            continue
        seen.add(key)
        if address.version == 6:
            result.append((socket.AF_INET6,
                           (str(address), int(port), 0, 0)))
        else:
            result.append((socket.AF_INET, (str(address), int(port))))
    return result


def _connect_pinned(host: str, port: int, timeout,
                    allow_private_network: bool) -> socket.socket:
    """Open a TCP socket to the validated addresses of *host* (no re-resolve)."""
    targets = _socket_addresses(host, port,
                                allow_private_network=allow_private_network)
    effective_timeout = timeout
    if effective_timeout is getattr(socket, "_GLOBAL_DEFAULT_TIMEOUT", None):
        effective_timeout = None
    last_error: OSError | None = None
    for family, sockaddr in targets:
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if effective_timeout is not None:
                sock.settimeout(effective_timeout)
            sock.connect(sockaddr)
        except OSError as exc:
            last_error = exc
            sock.close()
            continue
        return sock
    raise last_error or OSError(f"cannot connect to {host}:{port}")


class PinnedHTTPConnection(http.client.HTTPConnection):
    """``HTTPConnection`` that connects to a pre-resolved, validated address."""

    def __init__(self, host: str, *, allow_private_network: bool = False,
                 **kwargs) -> None:
        super().__init__(host, **kwargs)
        self._allow_private_network = bool(allow_private_network)

    def connect(self) -> None:
        if self._tunnel_host:  # pragma: no cover - proxies are disabled
            raise InvalidTarget("proxies are not supported for pinned fetches")
        self.sock = _connect_pinned(self.host, self.port or 80,
                                    self.timeout, self._allow_private_network)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """``HTTPSConnection`` with a pinned address and correct SNI/Host."""

    def __init__(self, host: str, *, allow_private_network: bool = False,
                 **kwargs) -> None:
        super().__init__(host, **kwargs)
        self._allow_private_network = bool(allow_private_network)

    def connect(self) -> None:
        if self._tunnel_host:  # pragma: no cover - proxies are disabled
            raise InvalidTarget("proxies are not supported for pinned fetches")
        sock = _connect_pinned(self.host, self.port or 443,
                               self.timeout, self._allow_private_network)
        context = self._context or ssl.create_default_context()
        # ``server_hostname`` is the URL hostname: SNI and certificate
        # hostname checking stay correct even though we connect to an IP.
        self.sock = context.wrap_socket(sock, server_hostname=self.host)


class PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, allow_private_network: bool = False) -> None:
        super().__init__()
        self._allow_private_network = bool(allow_private_network)

    def http_open(self, req):
        return self.do_open(
            functools.partial(PinnedHTTPConnection,
                              allow_private_network=self._allow_private_network),
            req)


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, allow_private_network: bool = False,
                 context: ssl.SSLContext | None = None) -> None:
        super().__init__(context=context)
        self._allow_private_network = bool(allow_private_network)

    def https_open(self, req):
        return self.do_open(
            functools.partial(PinnedHTTPSConnection,
                              allow_private_network=self._allow_private_network),
            req, context=self._context)


class GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect hop before following it (M4).

    The pinned connections additionally re-resolve and re-check each hop, so
    the two protections are independent layers.
    """

    max_redirections = 5

    def __init__(self, validate: Callable[[str], None]) -> None:
        super().__init__()
        self._validate = validate

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not isinstance(newurl, str) or not newurl:
            raise InvalidTarget("redirect target is missing")
        self._validate(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_pinned_opener(
    *, allow_private_network: bool = False,
    redirect_handler: urllib.request.HTTPRedirectHandler | None = None,
    context: ssl.SSLContext | None = None,
) -> urllib.request.OpenerDirector:
    """Opener with pinned connections and no ambient proxy/cookie handling."""
    handlers: list[urllib.request.BaseHandler] = [
        urllib.request.ProxyHandler({}),
        PinnedHTTPHandler(allow_private_network),
        PinnedHTTPSHandler(allow_private_network, context=context),
    ]
    if redirect_handler is not None:
        handlers.append(redirect_handler)
    return urllib.request.build_opener(*handlers)


def validate_redirect(url: str, *, allow_private_network: bool) -> None:
    """One-hop redirect guard used by the legacy (non-pinned) path."""
    validate_fetch_url(url, allow_private_network=allow_private_network)


@dataclass(frozen=True)
class FetchResult:
    status: int
    url: str
    content_type: str
    body: bytes
    truncated: bool = False


DEFAULT_SAFE_HEADERS = {
    "User-Agent": "StackFlowOS-net.fetch/1",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}


def fetch_url(
    url: str, *, allow_private_network: bool = False, pin_dns: bool = True,
    timeout: float = 15.0, max_bytes: int = 2 * 1024 * 1024,
    headers: dict[str, str] | None = None,
    context: ssl.SSLContext | None = None,
) -> FetchResult:
    """Fetch *url* with per-hop SSRF checks.

    ``pin_dns=True`` (``security.net_pin_dns``) resolves inside the connection
    and connects to the validated IP, so a rebinding answer between the
    pre-flight check and the connect cannot redirect the socket.  Setting
    ``pin_dns=False`` keeps the historical urllib path for tests/operators
    that need it, but still re-validates every redirect hop.
    """
    parsed = parse_fetch_url(url)
    safe_headers = dict(DEFAULT_SAFE_HEADERS)
    if headers:
        safe_headers.update({str(k): str(v) for k, v in headers.items()})
    if pin_dns:
        if not allow_private_network and is_private_host(parsed.hostname or ""):
            raise InvalidTarget(PRIVATE_NETWORK_ERROR)
        redirect = GuardedRedirectHandler(
            lambda target: validate_redirect(
                target, allow_private_network=allow_private_network))
        opener = build_pinned_opener(
            allow_private_network=allow_private_network,
            redirect_handler=redirect, context=context)
    else:
        validate_fetch_url(url, allow_private_network=allow_private_network)
        redirect = GuardedRedirectHandler(
            lambda target: validate_redirect(
                target, allow_private_network=allow_private_network))
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), redirect)
    request = urllib.request.Request(url, headers=safe_headers)
    with opener.open(request, timeout=float(timeout)) as response:
        # Final hop is validated by the pinned connection itself; for the
        # legacy path re-check explicitly before reading the body.
        final_url = response.geturl()
        if not pin_dns:
            validate_fetch_url(final_url,
                              allow_private_network=allow_private_network)
        status = int(getattr(response, "status", 200) or 200)
        response_headers = getattr(response, "headers", None)
        content_type = (response_headers.get("Content-Type", "")
                        if response_headers else "")
        body = response.read(int(max_bytes) + 1)
    return FetchResult(status=status, url=final_url,
                       content_type=content_type, body=body[:int(max_bytes)],
                       truncated=len(body) > int(max_bytes))


__all__ = [
    "ALLOWED_SCHEMES",
    "MAX_URL_LENGTH",
    "PRIVATE_NETWORK_ERROR",
    "DEFAULT_PORTS",
    "FetchResult",
    "GuardedRedirectHandler",
    "InvalidTarget",
    "PinnedHTTPConnection",
    "PinnedHTTPHandler",
    "PinnedHTTPSConnection",
    "PinnedHTTPSHandler",
    "PinnedTarget",
    "assert_public_target",
    "build_pinned_opener",
    "fetch_url",
    "is_private_address",
    "is_private_host",
    "parse_fetch_url",
    "resolve_host_addresses",
    "resolve_target",
    "validate_fetch_url",
    "validate_redirect",
]

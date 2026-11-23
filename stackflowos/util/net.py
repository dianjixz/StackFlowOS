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

import ipaddress
import socket
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


__all__ = [
    "ALLOWED_SCHEMES",
    "MAX_URL_LENGTH",
    "PRIVATE_NETWORK_ERROR",
    "InvalidTarget",
    "assert_public_target",
    "is_private_address",
    "is_private_host",
    "parse_fetch_url",
    "resolve_host_addresses",
    "validate_fetch_url",
]

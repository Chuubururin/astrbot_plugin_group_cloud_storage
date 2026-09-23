"""External API base -- error types, URL validation, state normalization.

Provides:
- ExternalApiError / OpenListApiError: cross-service error hierarchy
- ErrorKind / classify_error: error classification for degradation 
- validate_base_url: SSRF protection 
- normalize_task_state: state normalization 
"""

from __future__ import annotations

import ipaddress
import socket
from enum import Enum
from urllib.parse import urlparse


class ExternalApiError(Exception):
    """Cross-service unified error base class.

    code=None indicates a network-level failure (did not reach remote).
    """

    def __init__(self, service: str, message: str, code: int | None = None):
        self.service = service
        self.message = message
        self.code = code
        super().__init__(f"[{service}] {message}")


class OpenListApiError(ExternalApiError):
    """Carries OpenList envelope code/message."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__("openlist", message, code)


class ErrorKind(Enum):
    """Error classification for degradation decisions ."""

    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"
    REMOTE_ERROR = "remote_error"


def classify_error(exc: Exception) -> ErrorKind:
    """Classify exception into ErrorKind for degradation logic.

    - httpx.TimeoutException -> TIMEOUT
    - HTTP 404/405 -> UNSUPPORTED
    - Others -> REMOTE_ERROR
    """
    exc_name = type(exc).__name__
    # httpx timeout exceptions (check both class name and message)
    if "Timeout" in exc_name or "TimeoutException" in exc_name:
        return ErrorKind.TIMEOUT
    if "timeout" in str(exc).lower():
        return ErrorKind.TIMEOUT
    # HTTP status-based classification
    if getattr(exc, "code", None) in (404, 405):
        return ErrorKind.UNSUPPORTED
    if getattr(exc, "status_code", None) in (404, 405):
        return ErrorKind.UNSUPPORTED
    return ErrorKind.REMOTE_ERROR


def _is_restricted_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if IP address is in a restricted range using attribute checks.

    Covers: loopback, private, link-local, reserved, multicast, unspecified,
    and IPv6 unique-local addresses (fc00::/7). IPv4-mapped IPv6 addresses
    are recursively checked via .ipv4_mapped.
    """
    if ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        return ip.is_private
    if isinstance(ip, ipaddress.IPv6Address):
        # unique-local addresses (fc00::/7)
        if ip.packed[0] & 0xfe == 0xfc:
            return True
        # IPv4-mapped IPv6 addresses
        mapped = ip.ipv4_mapped
        if mapped is not None and _is_restricted_ip(mapped):
            return True
    return False


# Allowed schemes for outbound requests
_ALLOWED_SCHEMES = {"http", "https"}


def _normalize_origin(host: str) -> str:
    """Normalize a host for allow-list comparison (lowercase, no brackets)."""
    return (host or "").strip().strip("[]").lower()


def _is_trusted_origin(
    host: str,
    port: int | None,
    scheme: str,
    trusted_origins: frozenset[tuple[str, int]] | None,
) -> bool:
    """Whether (host, port) is an explicitly allow-listed trusted origin.

    OWASP SSRF guidance (Case 1: the application only talks to identified,
    trusted applications): keep an allow-list of the endpoints we own
    instead of disabling the private-address checks globally. Only an exact
    host+port match is trusted; a different port on the same host is not.
    """
    if not trusted_origins:
        return False
    default_port = 443 if scheme == "https" else 80
    return (_normalize_origin(host), port or default_port) in trusted_origins


def validate_base_url_structure(url: str, *, allow_private: bool = False) -> str:
    """I/O-free half of :func:`validate_base_url`.

    Everything here is pure string/parse work, so it is safe on a synchronous
    path (e.g. a constructor).  It covers:

    1. Scheme whitelist (http/https only)
    2. Hostname presence
    3. Restricted-range check for a **literal IP** host

    The one check that needs the network - DNS resolution for a *hostname* -
    is deliberately NOT done here; see :func:`validate_base_url` and
    ``OpenListClient._ensure_validated``.  Doing it here would put a blocking
    ``getaddrinfo`` on whatever synchronous path happens to construct the
    client (W-3b).

    Returns the normalized ``scheme://host[:port]``.
    """
    parsed = urlparse(url)

    # Scheme whitelist
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ExternalApiError(
            "openlist",
            f"URL scheme '{parsed.scheme}' not allowed (only http/https). "
            f"Received: {url}",
        )

    # Extract hostname
    hostname = parsed.hostname
    if not hostname:
        raise ExternalApiError("openlist", f"URL has no hostname: {url}")

    # Literal IP: classified right here, no lookup needed
    try:
        ip = ipaddress.ip_address(hostname)
        _check_ip_address(ip, allow_private, url)
    except ValueError:
        pass  # a hostname - its DNS check is deferred

    # Normalize: scheme://host[:port]
    port = parsed.port
    if port:
        return f"{parsed.scheme}://{hostname}:{port}"
    return f"{parsed.scheme}://{hostname}"


def validate_hostname_dns(url: str, *, allow_private: bool = False) -> None:
    """The DNS half of :func:`validate_base_url` - **blocking**.

    A no-op for literal IPs (already classified by
    :func:`validate_base_url_structure`) and when ``allow_private`` is set.
    Async callers MUST wrap this in ``asyncio.to_thread`` (same convention as
    ``assert_fetch_url_allowed`` / ``resolve_and_pin_ip``).
    """
    if allow_private:
        return
    hostname = urlparse(url).hostname
    if not hostname:
        return
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        _check_dns(hostname, url)
    # else: literal IP, no lookup to do


def validate_base_url(url: str, *, allow_private: bool = False) -> str:
    """Validate and normalize base URL with SSRF protection .

    Checks:
    1. Scheme whitelist (http/https only)
    2. DNS resolution (async-friendly, cached)
    3. Restrict loopback/private/link-local/reserved/multicast addresses

    Convenience composition of the two halves: the synchronous structure
    check plus the blocking DNS check.  Callers on an async path should
    prefer ``validate_base_url_structure`` at construction and defer
    ``validate_hostname_dns`` through ``asyncio.to_thread`` (W-3b).

    Args:
        url: The base URL to validate
        allow_private: If True, allow private/reserved addresses

    Returns:
        Normalized base_url (scheme://host[:port])

    Raises:
        ExternalApiError: If URL violates SSRF protection rules
    """
    normalized = validate_base_url_structure(url, allow_private=allow_private)
    validate_hostname_dns(url, allow_private=allow_private)
    return normalized


def _check_ip_address(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    allow_private: bool,
    url: str,
    hint: str = "openlist_allow_private_address",
) -> None:
    """Check if IP address is in restricted range."""
    if allow_private:
        return
    if _is_restricted_ip(ip):
        raise ExternalApiError(
            "openlist",
            f"URL resolves to restricted address {ip}. "
            f"Set {hint}=true to allow. "
            f"Received: {url}",
        )


def _check_dns(
    hostname: str, url: str, hint: str = "openlist_allow_private_address"
) -> None:
    """Perform DNS resolution and check resulting addresses."""
    try:
        # Blocking DNS resolution; callers should use asyncio.to_thread
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for _family, _, _, _, sockaddr in infos:
            ip_str = str(sockaddr[0])
            try:
                ip = ipaddress.ip_address(ip_str)
                _check_ip_address(ip, False, url, hint)
            except ValueError:
                continue
    except socket.gaierror as e:
        raise ExternalApiError(
            "openlist", f"DNS resolution failed for {hostname}: {e}. Received: {url}"
        ) from e


def assert_fetch_url_allowed(
    url: str,
    *,
    allow_private: bool = False,
    hint: str = "fetch_allow_private_address",
    trusted_origins: frozenset[tuple[str, int]] | None = None,
) -> str:
    """SSRF validation for server-side fetch URLs (shared by the fetch pipeline).

    - Scheme whitelist: http/https only (ftp/smb go through their own
      adapters and never pass through this function)
    - Rejects loopback/private/link-local/reserved/multicast addresses
      (same restricted-range table as validate_base_url)
    - Hostnames are DNS-resolved and every resolved address is re-checked
      (blocking call; async callers should wrap it in asyncio.to_thread)

    allow_private=True skips the address checks (config key
    fetch_allow_private_address, same semantics as
    openlist_allow_private_address). Returns the original URL unchanged.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ExternalApiError(
            "openlist",
            f"URL scheme '{parsed.scheme}' not allowed (only http/https). "
            f"Received: {url}",
        )
    hostname = parsed.hostname
    if not hostname:
        raise ExternalApiError("openlist", f"URL has no hostname: {url}")
    if _is_trusted_origin(hostname, parsed.port, parsed.scheme, trusted_origins):
        return url
    try:
        ip = ipaddress.ip_address(hostname)
        _check_ip_address(ip, allow_private, url, hint)
    except ValueError:
        # Not a literal IP: resolve DNS and re-check each resolved address
        if not allow_private:
            _check_dns(hostname, url, hint)
    return url


def assert_fetch_host_allowed(
    host: str,
    *,
    allow_private: bool = False,
    hint: str = "fetch_allow_private_address",
    port: int | None = None,
    scheme: str = "",
    trusted_origins: frozenset[tuple[str, int]] | None = None,
) -> str:
    """SSRF validation for a bare hostname/IP (no URL, no scheme).

    For protocol adapters whose scheme is not http/https (smb://, sftp://):
    they cannot pass assert_fetch_url_allowed's scheme whitelist, but the
    host still must clear the same restricted-range checks before
    connecting. Literal IPs are classified directly; hostnames are
    DNS-resolved and every resolved address is re-checked (blocking call;
    async callers should wrap it in asyncio.to_thread). Returns the host
    unchanged.
    """
    if not host:
        raise ExternalApiError("openlist", "Empty host is not allowed")
    if _is_trusted_origin(host, port, scheme, trusted_origins):
        return host
    try:
        ip = ipaddress.ip_address(host)
        _check_ip_address(ip, allow_private, host, hint)
    except ValueError:
        # Not a literal IP: resolve DNS and re-check each resolved address
        if not allow_private:
            _check_dns(host, host, hint)
    return host


def resolve_and_pin_ip(
    url: str,
    *,
    allow_private: bool = False,
    hint: str = "fetch_allow_private_address",
    trusted_origins: frozenset[tuple[str, int]] | None = None,
) -> tuple[str, str | None]:
    """SSRF-safe DNS resolution: validate + (http only) pin the IP.

    Returns (url, pinned_ip_or_None):
    - Literal IP: (url, None) — no DNS rebinding risk.
    - https hostname: validate all resolved addresses, return
      (original_url, None). TLS already binds the connection to the
      hostname (SNI + certificate identity), so a rebinding attacker
      cannot present a valid certificate for a private endpoint; pinning
      the IP here would instead replace the URL hostname with an IP and
      break certificate verification (httpx does not expose
      server_hostname for IP connections).
    - http hostname: validate all resolved addresses and return
      (url_with_ip, original_hostname) — plain http has no TLS identity
      binding, so the caller must connect to the validated IP and set the
      Host header to the original hostname to close the check-to-connect
      rebinding window.

    Raises ExternalApiError if any resolved address is restricted.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ExternalApiError("openlist", f"URL has no hostname: {url}")
    # Own endpoint (explicit allow-list): trusted by configuration, not input
    if _is_trusted_origin(hostname, parsed.port, parsed.scheme, trusted_origins):
        return url, None
    # Literal IP: no DNS rebinding risk
    try:
        ip = ipaddress.ip_address(hostname)
        _check_ip_address(ip, allow_private, url, hint)
        return url, None
    except ValueError:
        pass
    # Hostname: resolve, validate all results, pin to the first safe address
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ExternalApiError(
            "openlist", f"DNS resolution failed for {hostname}: {e}. Received: {url}"
        ) from e
    pinned_ip: str | None = None
    for _family, _, _, _, sockaddr in infos:
        ip_str = str(sockaddr[0])
        try:
            ip = ipaddress.ip_address(ip_str)
            _check_ip_address(ip, allow_private, url, hint)
            if pinned_ip is None:
                pinned_ip = ip_str
        except ValueError:
            continue
    if pinned_ip is None:
        raise ExternalApiError(
            "openlist",
            f"DNS resolution for {hostname} returned no valid addresses. Received: {url}",
        )
    # https: keep the original URL (TLS binds hostname identity; see docstring)
    if parsed.scheme == "https":
        return url, None
    # Rebuild URL with pinned IP; preserve port
    port = parsed.port
    pinned_host = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    if port:
        pinned_url = f"{parsed.scheme}://{pinned_host}:{port}{parsed.path or ''}"
        if parsed.query:
            pinned_url += f"?{parsed.query}"
    else:
        pinned_url = f"{parsed.scheme}://{pinned_host}{parsed.path or ''}"
        if parsed.query:
            pinned_url += f"?{parsed.query}"
    return pinned_url, hostname


def normalize_task_state(state: str) -> str:
    """Normalize an OpenList task state to its internal representation.

    The mapping lives in BridgeTaskState.from_external(); unknown states
    normalize to 'unknown' rather than a guess.
    """
    from core.domain.enums import BridgeTaskState

    return BridgeTaskState.from_external(state).value

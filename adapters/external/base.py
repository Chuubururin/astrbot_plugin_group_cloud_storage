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
    if hasattr(exc, "code") and exc.code in (404, 405):
        return ErrorKind.UNSUPPORTED
    if hasattr(exc, "status_code") and exc.status_code in (404, 405):
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


def validate_base_url(url: str, *, allow_private: bool = False) -> str:
    """Validate and normalize base URL with SSRF protection .

    Checks:
    1. Scheme whitelist (http/https only)
    2. DNS resolution (async-friendly, cached)
    3. Restrict loopback/private/link-local/reserved/multicast addresses

    Args:
        url: The base URL to validate
        allow_private: If True, allow private/reserved addresses

    Returns:
        Normalized base_url (scheme://host[:port])

    Raises:
        ExternalApiError: If URL violates SSRF protection rules
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

    # Skip DNS check for IP addresses
    try:
        ip = ipaddress.ip_address(hostname)
        _check_ip_address(ip, allow_private, url)
    except ValueError:
        # Not an IP address, needs DNS resolution
        if not allow_private:
            _check_dns(hostname, url)

    # Normalize: scheme://host[:port]
    port = parsed.port
    if port:
        return f"{parsed.scheme}://{hostname}:{port}"
    return f"{parsed.scheme}://{hostname}"


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
            ip_str = sockaddr[0]
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
    try:
        ip = ipaddress.ip_address(hostname)
        _check_ip_address(ip, allow_private, url, hint)
    except ValueError:
        # Not a literal IP: resolve DNS and re-check each resolved address
        if not allow_private:
            _check_dns(hostname, url, hint)
    return url


def resolve_and_pin_ip(
    url: str,
    *,
    allow_private: bool = False,
    hint: str = "fetch_allow_private_address",
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
        ip_str = sockaddr[0]
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


# State normalization map
# Mapping is centralized in core.domain.enums.BridgeTaskState.from_external()
# This function is kept as a backward-compatible entry point.
_STATE_MAP = None  # Unused; mapping lives in BridgeTaskState.from_external()


def normalize_task_state(state: str) -> str:
    """Normalize OpenList task state to internal representation.

    Delegates to BridgeTaskState.from_external() for single source of truth.
    Unknown states are returned as 'unknown' (do not guess).
    """
    from core.domain.enums import BridgeTaskState

    return BridgeTaskState.from_external(state).value

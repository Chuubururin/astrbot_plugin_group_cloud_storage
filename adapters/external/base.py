"""External API base -- error types, URL validation, state normalization.

Provides:
- ExternalApiError / OpenListApiError: cross-service error hierarchy
- ErrorKind / classify_error: error classification for degradation (REQ-05)
- validate_base_url: SSRF protection (REQ-11)
- normalize_task_state: state normalization (REQ-05)
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
    """Error classification for degradation decisions (REQ-05)."""

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


# Restricted address ranges for SSRF protection (REQ-11)
_RESTRICTED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("10.0.0.0/8"),  # private class A
    ipaddress.ip_network("172.16.0.0/12"),  # private class B
    ipaddress.ip_network("192.168.0.0/16"),  # private class C
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    ipaddress.ip_network("0.0.0.0/8"),  # unspecified
    ipaddress.ip_network("224.0.0.0/4"),  # multicast
    ipaddress.ip_network("::1/128"),  # loopback IPv6
    ipaddress.ip_network("fc00::/7"),  # unique local IPv6
    ipaddress.ip_network("fe80::/10"),  # link-local IPv6
    ipaddress.ip_network("::ffff:127.0.0.0/104"),  # IPv4-mapped loopback
]

# Allowed schemes for outbound requests
_ALLOWED_SCHEMES = {"http", "https"}


def validate_base_url(url: str, *, allow_private: bool = False) -> str:
    """Validate and normalize base URL with SSRF protection (REQ-11).

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
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, allow_private: bool, url: str
) -> None:
    """Check if IP address is in restricted range."""
    if allow_private:
        return
    for network in _RESTRICTED_NETWORKS:
        if ip in network:
            raise ExternalApiError(
                "openlist",
                f"URL resolves to restricted address {ip}. "
                f"Set openlist_allow_private_address=true to allow. "
                f"Received: {url}",
            )


def _check_dns(hostname: str, url: str) -> None:
    """Perform DNS resolution and check resulting addresses."""
    try:
        # Blocking DNS resolution; callers should use asyncio.to_thread
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for family, _, _, _, sockaddr in infos:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
                _check_ip_address(ip, False, url)
            except ValueError:
                continue
    except socket.gaierror as e:
        raise ExternalApiError(
            "openlist", f"DNS resolution failed for {hostname}: {e}. Received: {url}"
        )


# State normalization map (REQ-05)
# 统一使用 core.domain.enums.BridgeTaskState.from_external() 映射
# 保留此函数作为向后兼容入口
_STATE_MAP = None  # 已迁移至 BridgeTaskState.from_external()


def normalize_task_state(state: str) -> str:
    """Normalize OpenList task state to internal representation.

    Delegates to BridgeTaskState.from_external() for single source of truth.
    Unknown states are returned as 'unknown' (do not guess).
    """
    from core.domain.enums import BridgeTaskState

    return BridgeTaskState.from_external(state).value

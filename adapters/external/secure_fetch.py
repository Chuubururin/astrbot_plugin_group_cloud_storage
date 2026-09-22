"""SSRF-safe outbound fetch — the single implementation for the whole plugin.

History: the manual redirect loop (validate + DNS-pin every hop, keep the
original Host header for http, refuse over-limit bodies) existed in four
async copies (transfer.HttpAdapter, files/download x2, ingest/video) plus a
sync copy in download_server_io whose redirect semantics *diverged* (it
refused redirects outright instead of re-validating each hop). Every SSRF
hardening had to be applied N times; missing one copy was a vulnerability.

This module is that one implementation:

- every hop goes through ``resolve_and_pin_ip`` (loopback/private/reserved
  rejected unless ``allow_private`` or the origin is explicitly trusted);
- redirects are followed manually and re-validated per hop (DNS-rebinding
  safe), up to ``max_redirects``;
- bodies are capped at ``max_bytes`` mid-stream, never buffered past it;
- file writes land on a unique ``.part`` temp and publish via atomic rename,
  so a failed fetch never leaves a half file at ``dest``.

Errors are ``ValueError`` subclasses (call sites and tests historically catch
``ValueError``); the ``transfer`` adapter maps them onto ``FetchRejected`` to
keep its queue-visible contract unchanged.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx

from .base import resolve_and_pin_ip


class SecureFetchError(ValueError):
    """Base: a rejected URL, an exhausted redirect chain, or an over-cap body."""


class UrlRejectedError(SecureFetchError):
    """SSRF guard rejected this hop (scheme/host/DNS resolved to forbidden range)."""


class RedirectLimitError(SecureFetchError):
    """The redirect chain did not settle within ``max_redirects`` hops."""


class SizeLimitError(SecureFetchError):
    """The response body exceeded ``max_bytes``."""


@dataclass(frozen=True)
class FetchPolicy:
    max_bytes: int
    timeout: float
    allow_private: bool = False
    trusted_origins: frozenset[tuple[str, int]] = frozenset()
    max_redirects: int = 5
    chunk_size: int = 1 << 16


def _prepare_hop(url: str, policy: FetchPolicy) -> tuple[str, dict[str, str]]:
    """Validate + pin one hop (blocking DNS: callers wrap in to_thread).

    http hostnames are rewritten to the validated IP with the original host
    preserved as ``Host:``; https keeps the URL so TLS binds identity
    (SNI + certificate) — see ``resolve_and_pin_ip``.
    """
    try:
        pinned_url, original_host = resolve_and_pin_ip(
            url,
            allow_private=policy.allow_private,
            trusted_origins=policy.trusted_origins,
        )
    except Exception as e:
        raise UrlRejectedError(f"fetch url rejected: {e}") from e
    headers = {"Host": original_host} if original_host else {}
    return pinned_url, headers


def _follow_redirect_or_none(
    resp: httpx.Response, url: str, hop: int, policy: FetchPolicy
) -> str | None:
    if not (300 <= resp.status_code < 400):
        return None
    location = resp.headers.get("location")
    if location is None:
        # Un-followable 3xx (malformed redirect without Location). httpx's
        # is_redirect requires the header, and raise_for_status() does not
        # cover 3xx at all -- without this guard the (usually empty)
        # redirect body would be returned as a successful payload.
        raise SecureFetchError(
            f"fetch got {resp.status_code} redirect without Location"
        )
    if hop == policy.max_redirects:
        raise RedirectLimitError(f"fetch redirects exceeded ({policy.max_redirects})")
    return urljoin(url, location)


def _publish_tmp(tmp: Path, dest: Path) -> None:
    tmp.replace(dest)


def _new_tmp(dest: Path) -> Path:
    return dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.part")


async def fetch_bytes(url: str, policy: FetchPolicy, *, site: str = "fetch") -> bytes:
    """Fetch a URL into memory with the full per-hop SSRF loop + byte cap."""
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=policy.timeout
    ) as client:
        for hop in range(policy.max_redirects + 1):
            pinned_url, headers = await asyncio.to_thread(_prepare_hop, url, policy)
            async with client.stream("GET", pinned_url, headers=headers) as resp:
                nxt = _follow_redirect_or_none(resp, url, hop, policy)
                if nxt is not None:
                    url = nxt
                    continue
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes(chunk_size=policy.chunk_size):
                    total += len(chunk)
                    if total > policy.max_bytes:
                        raise SizeLimitError(
                            f"{site}: response exceeds {policy.max_bytes} bytes"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
    raise RedirectLimitError(f"{site}: redirect loop exited without a response")


async def fetch_to_file(
    url: str, dest: Path, policy: FetchPolicy, *, site: str = "fetch"
) -> int:
    """Stream a URL to ``dest`` (atomic .part publish); returns byte count."""
    total = 0
    tmp = _new_tmp(dest)
    try:
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=policy.timeout
        ) as client:
            for hop in range(policy.max_redirects + 1):
                pinned_url, headers = await asyncio.to_thread(
                    _prepare_hop, url, policy
                )
                async with client.stream("GET", pinned_url, headers=headers) as resp:
                    nxt = _follow_redirect_or_none(resp, url, hop, policy)
                    if nxt is not None:
                        url = nxt
                        continue
                    resp.raise_for_status()
                    with tmp.open("wb") as fh:
                        async for chunk in resp.aiter_bytes(chunk_size=policy.chunk_size):
                            total += len(chunk)
                            if total > policy.max_bytes:
                                raise SizeLimitError(
                                    f"{site}: response exceeds {policy.max_bytes} bytes"
                                )
                            fh.write(chunk)
                    _publish_tmp(tmp, dest)
                    return total
        raise RedirectLimitError(f"{site}: redirect loop exited without a response")
    finally:
        tmp.unlink(missing_ok=True)


def fetch_to_file_sync(
    url: str, dest: Path, policy: FetchPolicy, *, site: str = "fetch"
) -> int:
    """Synchronous twin of :func:`fetch_to_file` for worker-thread callers
    (the SFTP virtual FS). Same per-hop revalidation, same atomic publish."""
    total = 0
    tmp = _new_tmp(dest)
    try:
        with httpx.Client(follow_redirects=False, timeout=policy.timeout) as client:
            for hop in range(policy.max_redirects + 1):
                pinned_url, headers = _prepare_hop(url, policy)
                with client.stream("GET", pinned_url, headers=headers) as resp:
                    nxt = _follow_redirect_or_none(resp, url, hop, policy)
                    if nxt is not None:
                        url = nxt
                        continue
                    resp.raise_for_status()
                    with tmp.open("wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size=policy.chunk_size):
                            total += len(chunk)
                            if total > policy.max_bytes:
                                raise SizeLimitError(
                                    f"{site}: response exceeds {policy.max_bytes} bytes"
                                )
                            fh.write(chunk)
                    _publish_tmp(tmp, dest)
                    return total
        raise RedirectLimitError(f"{site}: redirect loop exited without a response")
    finally:
        tmp.unlink(missing_ok=True)

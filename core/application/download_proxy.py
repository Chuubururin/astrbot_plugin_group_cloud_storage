"""Remote-URL proxy registry + streaming server (bad-link #18 fix).

OpenList offline download derives the stored filename from the URL tail
unless the response carries Content-Disposition (same semantics as
AlistGo's internal/offline_download/http client). QQ CDN album links end
in a spec segment (/0 /400 /800...), so submitting them directly stores
the media under a spec name. register_proxy() wraps a remote URL under a
token; GET /download?proxy=<token>&token=... streams it back with a
Content-Disposition carrying the real name.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from core.log import logger

_STREAM_CHUNK = 1 << 16

# A client that connects and stops reading (TCP zero window) must not pin
# this coroutine -- and the open upstream response -- forever (L6). Mirrors
# download_server_io._DRAIN_TIMEOUT so both streaming helpers behave alike.
_DRAIN_TIMEOUT = 30.0

# Proxy tokens are single-use and expire quickly: the only intended client
# is an OpenList offline-download job started seconds after registration.
_TTL_SECONDS = 3600


class ProxyRegistry:
    """token -> {url, name, ts}; register() mints the proxied URL."""

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}

    def register(
        self,
        url: str,
        name: str,
        http_base: str,
        token: str,
        *,
        allow_private: bool = False,
    ) -> str:
        self._gc()
        t = uuid.uuid4().hex[:10]
        self._entries[t] = {
            "url": url,
            "name": name or "file",
            "ts": time.time(),
            "allow_private": allow_private,
        }
        return f"{http_base}/download?proxy={t}&token={token}"

    def pop(self, t: str) -> dict | None:
        entry = self._entries.get(t)
        if entry is not None:
            # Single-use: the token can't be replayed after the first hit.
            self._entries.pop(t, None)
        return entry

    def _gc(self) -> None:
        now = time.time()
        for k in [k for k, v in self._entries.items() if now - v["ts"] > _TTL_SECONDS]:
            self._entries.pop(k, None)


def _header_safe(name: str) -> str:
    """Strip CR/LF from filenames to prevent HTTP header injection."""
    return name.replace("\r", "").replace("\n", "")


def ascii_fallback(name: str) -> str:
    """ASCII-only RFC6266 `filename=` value derived from the real name.

    `filename*=` (RFC5987) is understood by modern clients, but a client that
    only honours the legacy `filename=` parameter gets nothing usable if we
    omit it. Non-ASCII characters become "_"; the extension is preserved so
    the saved file still has a type. Never returns an empty string.
    """
    import re as _re

    cleaned = _re.sub(r"[^\x20-\x7e]", "_", _header_safe(name))
    cleaned = cleaned.replace("\\", "_").replace("/", "_").replace('"', "_").strip()
    return cleaned or "download"


def _content_disposition(name: str) -> str:
    """Full RFC6266 Content-Disposition value: ASCII fallback + encoded real name."""
    from urllib.parse import quote as _quote

    return (
        f"attachment; filename=\"{ascii_fallback(name)}\"; "
        f"filename*=UTF-8''{_quote(name)}"
    )


def _open_client():
    """httpx client seam: follow_redirects=False because the loop below
    validates every hop itself (tests inject a mock transport here)."""
    import httpx

    return httpx.AsyncClient(follow_redirects=False, timeout=180.0)


async def _drain(writer) -> None:
    """``writer.drain()`` with a deadline (L6).

    Without it a client that opens the connection and never reads (TCP zero
    window) blocks this coroutine forever, holding both the open upstream
    response and the source handle that the surrounding cleanup expects to
    reclaim. Same semantics as download_server_io._drain().
    """
    await asyncio.wait_for(writer.drain(), timeout=_DRAIN_TIMEOUT)


async def serve_staged(writer: asyncio.StreamWriter, entry: dict, reply) -> None:
    """Stream a local staged file with its registered name in
    Content-Disposition (moved here so the HTTP handler stays thin)."""
    src_path = Path(entry["path"])
    name = _header_safe(entry["name"])
    total = src_path.stat().st_size
    head = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {total}\r\n"
        f"Content-Disposition: {_content_disposition(name)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("latin-1")
    writer.write(head)
    with src_path.open("rb") as fh:
        while True:
            chunk = fh.read(_STREAM_CHUNK)
            if not chunk:
                break
            writer.write(chunk)
            await _drain(writer)


async def serve_proxy(
    writer: asyncio.StreamWriter, entry: dict, reply
) -> None:
    """Stream one registered remote URL back with a Content-Disposition
    carrying the caller-chosen filename.

    SSRF-guarded on every hop (same helper as every outbound fetch).
    Live 2026-09-12: QQ CDN album links 302 to the same host over http
    with a spec-segment tail, so the previous single-hop proxy answered
    502 and the album->netdisk offline download stored nothing — follow
    up to 5 redirects, re-validating each hop like the panel download
    stream does.
    """
    url = entry["url"]
    name = _header_safe(entry["name"])
    allow_priv = bool(entry.get("allow_private", False))
    try:
        from adapters.external.base import assert_fetch_url_allowed

        # Proxy URLs are registered by the plugin itself (not external
        # user input), so respect the fetch_allow_private_address config
        # for Docker / LAN deployments where dlserver and OpenList run on
        # private IPs (Issue #8 test environment).
        assert_fetch_url_allowed(url, allow_private=allow_priv)
    except Exception as e:
        logger.warning(f"[dlserver] proxy url rejected: {e}")
        await reply(writer, 400, b"proxy url rejected")
        return
    try:
        import asyncio as _asyncio
        from urllib.parse import urljoin

        async with _open_client() as hc:
            for _hop in range(6):  # 1 direct + up to 5 redirects
                from adapters.external.base import resolve_and_pin_ip

                pinned_url, original_host = await _asyncio.to_thread(
                    resolve_and_pin_ip, url, allow_private=allow_priv
                )
                headers = {}
                if original_host:
                    # Keep routing correct for virtual-hosted origins when
                    # the pinned-IP URL replaces the hostname.
                    headers["Host"] = original_host
                resp = await hc.send(
                    hc.build_request("GET", pinned_url, headers=headers), stream=True
                )
                if 300 <= resp.status_code < 400:
                    location = resp.headers.get("location")
                    await resp.aclose()
                    if not location or _hop == 5:
                        logger.warning("[dlserver] proxy: redirect blocked")
                        await reply(writer, 502, b"proxy redirect blocked")
                        return
                    url = urljoin(url, location)
                    continue
                resp.raise_for_status()
                ctype = resp.headers.get("content-type") or "application/octet-stream"
                head = (
                    "HTTP/1.1 200 OK\r\n"
                    f"Content-Type: {ctype}\r\n"
                    f"Content-Disposition: {_content_disposition(name)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("latin-1")
                writer.write(head)
                async for chunk in resp.aiter_bytes(_STREAM_CHUNK):
                    if not chunk:
                        break
                    writer.write(chunk)
                    await _drain(writer)
                await resp.aclose()
                return
    except Exception as e:
        logger.warning(f"[dlserver] proxy fetch failed: {e}")
        try:
            await reply(writer, 502, b"proxy fetch failed")
        except Exception:
            pass

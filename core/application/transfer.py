"""TransferService - multi-protocol transfer pipeline (ingress only).

Ingress (to cloud): remote file -multi-protocol fetch (http/https/ftps/smb)->
local staging -OneBot11 upload-> QQ server. ftp:// URLs are always upgraded
to explicit TLS (FTPS); plaintext FTP is never used.

Modular: each protocol implements ProtocolAdapter.get (fetch to local
staging); protocol details (URL parsing/auth/rate limits/size caps) are
consolidated inside the adapters.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit, unquote

import httpx

from adapters.external.base import assert_fetch_url_allowed
from core.application.queue import OpQueue
from core.config import PluginConfig
from core.log import logger
from ports.meta_store import MetaStorePort

FETCH_MAX_BYTES = 2 * 1024**3
FETCH_TIMEOUT_SEC = 180.0
_HTTP_REDIRECT_MAX = 5

_INGRESS_SCHEMES = ("http", "https", "ftp", "smb")


def parse_target(url: str) -> dict:
    """Parse a protocol target URL into structured fields (auth/host/path)."""
    p = urlsplit(url)
    return {
        "scheme": (p.scheme or "").lower(),
        "host": p.hostname or "",
        "port": p.port,
        "user": unquote(p.username) if p.username else "",
        "password": unquote(p.password) if p.password else "",
        "path": unquote(p.path) or "/",
        "url": url,
    }


class ProtocolAdapter:
    """Single-protocol transfer adapter: get() fetches to local staging."""

    scheme = ""

    def __init__(self, max_bytes: int, timeout: float):
        self.max_bytes = max_bytes
        self.timeout = timeout

    async def get(self, target: dict, dest: Path) -> int:
        raise NotImplementedError


class HttpAdapter(ProtocolAdapter):
    scheme = "http"

    def __init__(self, max_bytes: int, timeout: float, allow_private: bool = False):
        super().__init__(max_bytes, timeout)
        # SSRF gate (fetch_allow_private_address): validate the host before
        # each hop
        self._allow_private = allow_private

    def _validate_url(self, url: str) -> None:
        """http/https + private/reserved address validation (blocking DNS
        resolution, invoked via to_thread)."""
        try:
            assert_fetch_url_allowed(url, allow_private=self._allow_private)
        except Exception as e:
            raise ValueError(f"fetch url rejected: {e}") from e

    async def get(self, target: dict, dest: Path) -> int:
        total = 0
        url = target["url"]
        # Manual redirect loop: every hop is re-validated against private and
        # reserved address ranges so a 302 redirect cannot reach the intranet
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=self.timeout
        ) as client:
            for _hop in range(_HTTP_REDIRECT_MAX + 1):
                await asyncio.to_thread(self._validate_url, url)
                async with client.stream("GET", url) as resp:
                    if resp.is_redirect and resp.has_redirect_location:
                        if _hop == _HTTP_REDIRECT_MAX:
                            raise ValueError(
                                f"fetch redirects exceeded ({_HTTP_REDIRECT_MAX})"
                            )
                        url = urljoin(url, resp.headers["location"])
                        logger.debug(f"[transfer] fetch redirect -> {url}")
                        continue
                    resp.raise_for_status()
                    with dest.open("wb") as fh:
                        async for chunk in resp.aiter_bytes(1 << 16):
                            fh.write(chunk)
                            total += len(chunk)
                            if total > self.max_bytes:
                                raise ValueError(
                                    f"fetch exceeds max bytes ({self.max_bytes})"
                                )
                    break
        return total


class FtpAdapter(ProtocolAdapter):
    scheme = "ftp"

    def _conn(self, target: dict):
        import ftplib
        import ssl

        host = target["host"]
        user = target["user"] or "anonymous"
        password = target["password"] if target["user"] else "anonymous@"

        # FTPS-only (explicit TLS): AUTH TLS on the control channel plus
        # PROT P on the data channel, so credentials and payloads are never
        # transmitted in cleartext. Servers without TLS support are
        # rejected with an explicit error instead of downgrading.
        ftps = ftplib.FTP_TLS(context=ssl.create_default_context())
        ftps.connect(host, target["port"] or 21, timeout=self.timeout)
        try:
            ftps.login(user, password)
            ftps.prot_p()
        except (ftplib.error_perm, ftplib.error_proto, ssl.SSLError, OSError, EOFError):
            ftps.close()
            raise ValueError(
                "FTP 服务器不支持 FTPS（TLS），为避免明文传输已中止连接"
            ) from None
        return ftps

    async def get(self, target: dict, dest: Path) -> int:
        def _run():
            ftp = self._conn(target)
            try:
                written = 0

                def _cb(data: bytes) -> None:
                    nonlocal written
                    dest_fh.write(data)
                    written += len(data)
                    if written > self.max_bytes:
                        raise ValueError("fetch exceeds max bytes (ftp)")

                with dest.open("wb") as dest_fh:
                    ftp.retrbinary(f"RETR {target['path']}", _cb, blocksize=1 << 16)
            finally:
                try:
                    ftp.quit()
                except Exception:
                    ftp.close()

        await asyncio.to_thread(_run)
        return dest.stat().st_size


class SmbAdapter(ProtocolAdapter):
    scheme = "smb"

    def _conn(self, target: dict):
        from smb.SMBConnection import SMBConnection

        share, _, _ = target["path"].lstrip("/").partition("/")
        if not share:
            raise ValueError("smb url needs share: smb://host/share/path")
        conn = SMBConnection(
            target["user"] or "guest",
            target["password"] or "",
            "astrbot",
            "astrbot",
            use_ntlm_v2=True,
        )
        if not conn.connect(target["host"], target["port"] or 445, timeout=int(self.timeout)):
            raise ValueError(f"smb connect failed: {target['host']}")
        return conn, share

    async def get(self, target: dict, dest: Path) -> int:
        def _run():
            conn, share = self._conn(target)
            try:
                _, _, path = target["path"].lstrip("/").partition("/")
                with dest.open("wb") as fh:
                    conn.retrieveFile(share, path, fh, timeout=int(self.timeout))
            finally:
                conn.close()

        await asyncio.to_thread(_run)
        return dest.stat().st_size


class TransferService:
    def __init__(
        self,
        store: MetaStorePort,
        queue: OpQueue,
        tmp_dir: Path,
        config: dict | None = None,
        download_info: Callable[..., Awaitable[tuple[str, str]]] | None = None,
    ):
        self.store = store
        self.queue = queue
        self.tmp_dir = Path(tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        # Config object injection: unified PluginConfig boundary (dicts pass
        # through for compatibility, see core.config.model)
        cfg = config if isinstance(config, PluginConfig) else PluginConfig(config or {})
        fetch_max = int(cfg.get("fetch_max_bytes", FETCH_MAX_BYTES) or FETCH_MAX_BYTES)
        fetch_timeout = float(
            cfg.get("fetch_timeout_sec", FETCH_TIMEOUT_SEC) or FETCH_TIMEOUT_SEC
        )
        # SSRF gate: loopback/private/reserved addresses are denied by
        # default (same semantics as openlist_allow_private_address)
        allow_private = bool(cfg.get("fetch_allow_private_address", False))
        self._allow_private = allow_private
        self._download_info = download_info
        self._adapters: dict[str, ProtocolAdapter] = {
            "http": HttpAdapter(fetch_max, fetch_timeout, allow_private),
            "https": HttpAdapter(fetch_max, fetch_timeout, allow_private),
            "ftp": FtpAdapter(fetch_max, fetch_timeout),
            "smb": SmbAdapter(fetch_max, fetch_timeout),
        }

    @staticmethod
    def parse_target(url: str) -> dict:
        return parse_target(url)

    def _adapter(self, t: dict, schemes: tuple[str, ...]) -> ProtocolAdapter:
        scheme = t["scheme"]
        if scheme not in schemes:
            raise ValueError(f"unsupported scheme: {scheme} (only {'/'.join(schemes)})")
        return self._adapters[scheme]

    async def download_to(self, url: str, dest: Path) -> int:
        """Ingress pipeline: multi-protocol fetch to local staging; returns
        the byte count.

        http/https targets get one SSRF validation at the entrance (fails
        fast with a clear user-facing error); per-hop re-validation of
        redirects happens inside HttpAdapter.
        """
        t = parse_target(url)
        if t["scheme"] in ("http", "https"):
            adapter = self._adapter(t, _INGRESS_SCHEMES)
            if isinstance(adapter, HttpAdapter):
                await asyncio.to_thread(adapter._validate_url, t["url"])
        return await self._adapter(t, _INGRESS_SCHEMES).get(t, dest)

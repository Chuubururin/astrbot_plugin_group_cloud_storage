"""TransferService - multi-protocol transfer pipeline (ingress only).

Ingress (to cloud): remote file -multi-protocol fetch (http/https/sftp/smb)->
local staging -OneBot11 upload-> QQ server. SFTP targets always use SSH
transport (paramiko); plaintext protocols are never used.

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

from adapters.external.base import assert_fetch_url_allowed, resolve_and_pin_ip
from core.application.queue import OpQueue
from core.config import PluginConfig
from core.log import logger
from ports.meta_store import MetaStorePort

FETCH_MAX_BYTES = 2 * 1024**3
FETCH_TIMEOUT_SEC = 180.0
_HTTP_REDIRECT_MAX = 5

_INGRESS_SCHEMES = ("http", "https", "sftp", "smb")


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

    def __init__(self, max_bytes: int, timeout: float, allow_private: bool = False):
        self.max_bytes = max_bytes
        self.timeout = timeout
        self._allow_private = allow_private

    async def get(self, target: dict, dest: Path) -> int:
        raise NotImplementedError


class HttpAdapter(ProtocolAdapter):
    scheme = "http"

    def __init__(self, max_bytes: int, timeout: float, allow_private: bool = False):
        super().__init__(max_bytes, timeout, allow_private)

    def _resolve_url(self, url: str) -> tuple[str, str | None]:
        """SSRF validation + DNS pinning (blocking, invoked via to_thread).

        Returns (url, original_hostname_or_None). For http hostnames the
        URL is rewritten to the validated IP and the caller sets Host:
        original_hostname (no TLS identity to rely on). For https the
        original URL is kept — TLS binds the hostname (SNI + certificate),
        and pinning the IP would break certificate verification.
        """
        try:
            return resolve_and_pin_ip(url, allow_private=self._allow_private)
        except Exception as e:
            raise ValueError(f"fetch url rejected: {e}") from e

    async def get(self, target: dict, dest: Path) -> int:
        total = 0
        url = target["url"]
        # Manual redirect loop: every hop is re-validated + pinned against
        # private and reserved address ranges so a 302 redirect cannot reach
        # the intranet (DNS rebinding protection).
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=self.timeout
        ) as client:
            for _hop in range(_HTTP_REDIRECT_MAX + 1):
                pinned_url, original_host = await asyncio.to_thread(
                    self._resolve_url, url
                )
                headers = {}
                if original_host:
                    headers["Host"] = original_host
                async with client.stream("GET", pinned_url, headers=headers) as resp:
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


class SmbAdapter(ProtocolAdapter):
    scheme = "smb"

    def _conn(self, target: dict):
        from smb.SMBConnection import SMBConnection

        share, _, _ = target["path"].lstrip("/").partition("/")
        if not share:
            raise ValueError("smb url needs share: smb://host/share/path")

        # SSRF protection: validate host before connecting
        if not self._allow_private:
            assert_fetch_url_allowed(f"smb://{target['host']}", allow_private=False)

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


class SftpAdapter(ProtocolAdapter):
    scheme = "sftp"

    def __init__(
        self,
        max_bytes: int,
        timeout: float,
        allow_private: bool = False,
        host_key_store: "Path | None" = None,
    ):
        super().__init__(max_bytes, timeout, allow_private)
        # TOFU host-key store (OpenSSH-style trust on first use, RFC 4251
        # §4.1): first connection records the fingerprint, later connections
        # must match. A fresh RSA host key per boot is download_server's
        # own SFTP endpoint; ingest targets here are long-lived hosts.
        self._host_key_store = host_key_store

    def _conn(self, target: dict):
        import paramiko

        host = target["host"]
        user = target["user"] or "anonymous"
        password = target["password"] if target["user"] else ""

        # SSRF protection: validate host before connecting
        if not self._allow_private:
            assert_fetch_url_allowed(f"sftp://{host}", allow_private=False)

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if self._host_key_store is not None:
            # TOFU: AutoAddPolicy against a plugin-owned file — new keys are
            # persisted per host so a later mismatch is rejected instead of
            # silently re-trusted (plain AutoAddPolicy never rejects).
            self._host_key_store.parent.mkdir(parents=True, exist_ok=True)
            client.load_host_keys(str(self._host_key_store))
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            host,
            port=target["port"] or 22,
            username=user,
            password=password,
            timeout=self.timeout,
        )
        sftp = client.open_sftp()
        return client, sftp

    async def get(self, target: dict, dest: Path) -> int:
        def _run():
            ssh, sftp = self._conn(target)
            try:
                path = target["path"]
                sftp.get(path, str(dest))
            finally:
                sftp.close()
                ssh.close()

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
        # Unified size resolution: string-unit "fetch_max_size" (base 1000)
        # with the legacy byte-count key as fallback (see PluginConfig);
        # 0 = unset → built-in default.
        fetch_max = cfg.fetch_max_bytes or FETCH_MAX_BYTES
        fetch_timeout = float(
            cfg.get("fetch_timeout_sec", FETCH_TIMEOUT_SEC) or FETCH_TIMEOUT_SEC
        )
        # SSRF gate: loopback/private/reserved addresses are denied by
        # default (same semantics as openlist_allow_private_address)
        allow_private = bool(cfg.get("fetch_allow_private_address", False))
        self._allow_private = allow_private
        self._download_info = download_info
        # SFTP TOFU store: fingerprints persist under the plugin data tmp dir
        sftp_host_keys = self.tmp_dir / "sftp_known_hosts"
        self._adapters: dict[str, ProtocolAdapter] = {
            "http": HttpAdapter(fetch_max, fetch_timeout, allow_private),
            "https": HttpAdapter(fetch_max, fetch_timeout, allow_private),
            "sftp": SftpAdapter(
                fetch_max, fetch_timeout, allow_private, host_key_store=sftp_host_keys
            ),
            "smb": SmbAdapter(fetch_max, fetch_timeout, allow_private),
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
                await asyncio.to_thread(adapter._resolve_url, t["url"])
        return await self._adapter(t, _INGRESS_SCHEMES).get(t, dest)

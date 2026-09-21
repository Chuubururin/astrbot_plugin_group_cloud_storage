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

from adapters.external.base import (
    assert_fetch_host_allowed,
    resolve_and_pin_ip,
)
from core.application.queue import OpQueue
from core.config import PluginConfig
from core.domain.enums import OneBotApiError, OneBotErrorKind
from core.log import logger
from ports.meta_store import MetaStorePort

FETCH_MAX_BYTES = 2 * 1024**3
FETCH_TIMEOUT_SEC = 180.0
_HTTP_REDIRECT_MAX = 5

_INGRESS_SCHEMES = ("http", "https", "sftp", "smb")


class FetchRejected(OneBotApiError, ValueError):
    """Deterministic fetch rejection (SSRF refusal / bad scheme / size cap).

    Queue semantics: OneBotApiError(LOCAL_ERROR) so OpQueue ends the fetch op on
    the first attempt instead of replaying it 3x (2/4/8s) with an identical
    failure. Legacy contract: it is also a ValueError, which this module's
    historical callers (webapi/security paths) still catch.
    """


def download_endpoint_origins(config) -> set[tuple[str, int]]:
    """(host, port) pairs of this plugin's OWN download endpoints.

    The fetch pipeline denies loopback/private addresses by default, but the
    distribution service legitimately fetches from our own download server
    (a loopback direct link). Those exact endpoints are allow-listed instead
    of disabling the private-address gate globally (OWASP SSRF: prefer an
    allow-list of identified and trusted applications).
    """
    cfg = config if isinstance(config, PluginConfig) else PluginConfig(config or {})
    if not bool(cfg.get("download_server_enabled", False)):
        return set()
    host = str(cfg.get("download_server_host", "127.0.0.1") or "127.0.0.1")
    ports = {
        int(cfg.get("download_http_port", 0) or 0),
        int(cfg.get("download_sftp_port", 0) or 0),
        int(cfg.get("download_smb_port", 0) or 0),
    }
    ports.discard(0)
    hosts = {host}
    # A wildcard bind also serves on loopback.
    if host in ("0.0.0.0", "::"):
        hosts |= {"127.0.0.1", "::1", "localhost"}
    return {
        (h.strip().strip("[]").lower(), p) for h in hosts for p in ports
    }


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

    def __init__(
        self,
        max_bytes: int,
        timeout: float,
        allow_private: bool = False,
        trusted_origins: frozenset[tuple[str, int]] | None = None,
    ):
        self.max_bytes = max_bytes
        self.timeout = timeout
        self._allow_private = allow_private
        # Explicit allow-list of endpoints we own (OWASP SSRF Case 1): the
        # loopback/private checks are skipped ONLY for an exact host+port
        # match, never by relaxing allow_private globally.
        self._trusted = trusted_origins or frozenset()

    async def get(self, target: dict, dest: Path) -> int:
        raise NotImplementedError


class HttpAdapter(ProtocolAdapter):
    scheme = "http"

    def __init__(
        self,
        max_bytes: int,
        timeout: float,
        allow_private: bool = False,
        trusted_origins: frozenset[tuple[str, int]] | None = None,
    ):
        super().__init__(max_bytes, timeout, allow_private, trusted_origins)

    def _resolve_url(self, url: str) -> tuple[str, str | None]:
        """SSRF validation + DNS pinning (blocking, invoked via to_thread).

        Returns (url, original_hostname_or_None). For http hostnames the
        URL is rewritten to the validated IP and the caller sets Host:
        original_hostname (no TLS identity to rely on). For https the
        original URL is kept — TLS binds the hostname (SNI + certificate),
        and pinning the IP would break certificate verification.
        """
        try:
            return resolve_and_pin_ip(
                url,
                allow_private=self._allow_private,
                trusted_origins=self._trusted,
            )
        except Exception as e:
            raise FetchRejected(
                OneBotErrorKind.LOCAL_ERROR, "fetch", f"fetch url rejected: {e}"
            ) from e

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
                                raise FetchRejected(
                                    OneBotErrorKind.LOCAL_ERROR,
                                    "fetch",
                                    f"fetch exceeds max bytes ({self.max_bytes})",
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

        # SSRF protection: validate host before connecting. SMB has no
        # http(s) URL, so use the host-only check (assert_fetch_url_allowed
        # rejects non-http schemes outright).
        if not self._allow_private:
            assert_fetch_host_allowed(
                target["host"],
                allow_private=False,
                port=target["port"] or 445,
                scheme="smb",
                trusted_origins=self._trusted,
            )

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
                # Size cap on every protocol (http enforces it while
                # streaming): stat the remote file first so an oversized
                # transfer is rejected before it can fill local disk. A
                # server that refuses the attribute query falls through to
                # the download_to post-check.
                try:
                    attrs = conn.getAttributes(share, path)
                except Exception:
                    attrs = None
                if attrs is not None and attrs.file_size > self.max_bytes:
                    raise FetchRejected(
                        OneBotErrorKind.LOCAL_ERROR,
                        "fetch",
                        f"fetch exceeds max bytes ({self.max_bytes})",
                    )
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
        trusted_origins: frozenset[tuple[str, int]] | None = None,
    ):
        super().__init__(max_bytes, timeout, allow_private, trusted_origins)
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

        # SSRF protection: validate host before connecting (host-only
        # check; sftp:// cannot pass the http/https scheme whitelist).
        if not self._allow_private:
            assert_fetch_host_allowed(
                host,
                allow_private=False,
                port=target["port"] or 22,
                scheme="sftp",
                trusted_origins=self._trusted,
            )

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if self._host_key_store is not None:
            # TOFU: AutoAddPolicy against a plugin-owned file — new keys are
            # persisted per host so a later mismatch is rejected instead of
            # silently re-trusted (plain AutoAddPolicy never rejects).
            self._host_key_store.parent.mkdir(parents=True, exist_ok=True)
            if not self._host_key_store.exists():
                # paramiko HostKeys.load() raises FileNotFoundError on a
                # missing file; seed an empty store so the first connection
                # can record its key (TOFU) instead of failing outright.
                self._host_key_store.touch()
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
                # M5: self.timeout only covered client.connect; sftp.get() has
                # no read timeout of its own, so a peer that accepts the
                # connection and then stalls blocked this worker thread
                # forever. fetch is a BULK op: the bulk slot and the worker
                # stayed occupied and the op never reached a terminal state
                # (recovery needed a restart). The channel timeout is an
                # *idle* timeout applied per socket read, so an actively
                # transferring part is never cut while a stalled peer raises
                # instead of blocking.
                try:
                    chan = sftp.get_channel()
                    if chan is not None:
                        chan.settimeout(self.timeout)
                except Exception as e:
                    # Stub/minimal SFTP clients expose no channel: the transfer
                    # still runs, just without the idle timeout.
                    logger.debug(f"[transfer] sftp idle timeout not armed: {e}")
                # Size cap on every protocol (see SmbAdapter): reject an
                # oversized remote file before downloading it.
                try:
                    st = sftp.stat(path)
                except Exception:
                    st = None
                if st is not None and st.st_size > self.max_bytes:
                    raise FetchRejected(
                        OneBotErrorKind.LOCAL_ERROR,
                        "fetch",
                        f"fetch exceeds max bytes ({self.max_bytes})",
                    )
                sftp.get(path, str(dest))
            finally:
                sftp.close()
                ssh.close()

        try:
            await asyncio.to_thread(_run)
        except TimeoutError as e:
            # M5: a stalled transfer must surface as a classified timeout
            # (retriable, same shape as the SMB side) instead of a raw socket
            # timeout escaping unclassified.
            raise OneBotApiError(
                OneBotErrorKind.TIMEOUT, "sftp_get", f"sftp transfer timed out: {e}"
            ) from e
        return dest.stat().st_size


class TransferService:
    def __init__(
        self,
        store: MetaStorePort,
        queue: OpQueue,
        tmp_dir: Path,
        config: dict | None = None,
        download_info: Callable[..., Awaitable[tuple[str, str]]] | None = None,
        trusted_origins: set[tuple[str, int]] | None = None,
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
        trusted = frozenset(trusted_origins or ())
        self._trusted_origins = trusted
        self._adapters: dict[str, ProtocolAdapter] = {
            "http": HttpAdapter(fetch_max, fetch_timeout, allow_private, trusted),
            "https": HttpAdapter(fetch_max, fetch_timeout, allow_private, trusted),
            "sftp": SftpAdapter(
                fetch_max, fetch_timeout, allow_private,
                host_key_store=sftp_host_keys, trusted_origins=trusted,
            ),
            "smb": SmbAdapter(fetch_max, fetch_timeout, allow_private, trusted),
        }

    @staticmethod
    def parse_target(url: str) -> dict:
        return parse_target(url)

    def _adapter(self, t: dict, schemes: tuple[str, ...]) -> ProtocolAdapter:
        scheme = t["scheme"]
        if scheme not in schemes:
            raise FetchRejected(
                OneBotErrorKind.LOCAL_ERROR,
                "fetch",
                f"unsupported scheme: {scheme} (only {'/'.join(schemes)})",
            )
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
        adapter = self._adapter(t, _INGRESS_SCHEMES)
        n = await adapter.get(t, dest)
        # Protocol-neutral backstop: sftp/smb stream without a live byte
        # counter, so a server that dodged the size pre-check still cannot
        # smuggle an over-limit file into the ingest pipeline.
        if n > adapter.max_bytes:
            dest.unlink(missing_ok=True)
            raise FetchRejected(
                OneBotErrorKind.LOCAL_ERROR,
                "fetch",
                f"fetch exceeds max bytes ({adapter.max_bytes})",
            )
        return n

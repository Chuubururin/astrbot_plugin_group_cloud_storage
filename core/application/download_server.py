"""DownloadServerService - local multi-protocol download service (the
plugin acts as the download server).

Semantics (distinct from "egress = push to an external target"):
- The host exposes **download service addresses** (http / sftp / smb);
  external clients hit this host directly to pull cloud files. The address
  (host/port/token) comes from the plugin config
  (_conf_schema download_server_*)
- HTTP: `GET /download?group&id&token` - single files 302-redirect to the
  QQ CDN direct link (zero proxy load); volumes/videos are reassembled
  locally and streamed back. `GET /download?staged=<token>&token` serves a
  registered staged file (e.g. an essence text exported to a .txt)
- SFTP: paramiko virtual filesystem (/<group_id>/<filename>); reads pull bytes
  from the cloud on demand and reuse the cache on later opens;
  /staged/<name> serves staged files. Optional dep.
- SMB: impacket smbserver (optional dependency) sharing a cache directory;
  entries materialize on demand via ensure_local()/register_staged()
"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import shutil
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, quote, urlparse

from core.application.download_proxy import ProxyRegistry, serve_proxy, serve_staged
from core.application.download_server_io import (
    cleanup_recon,
    collect_resources,
    join_sftp_connections,
    serve_local_file,
    start_sftp_server,
    start_smb_server,
    stop_smb_server,
)
from core.config import PluginConfig
from core.log import logger
from ports.meta_store import MetaStorePort


# L7: an SFTP client stats and then opens the same path, and stat() used to
# page the whole group (page_size=500, max_pages=200 -> up to 200 SQL
# queries per call). Hits are cached briefly; the targeted keyword query
# below usually answers on the first round-trip.
_ROW_CACHE_TTL = 5.0
_LOOKUP_PAGE = 50

# Bind-everywhere addresses. Valid to listen on, meaningless to publish: a link
# built from one resolves on the client to the client's own machine.
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "[::]"})


def _safe_header_name(value: str) -> str:
    """Strip CR/LF from a header-bound string (filename/URL): no header injection."""
    return value.replace("\r", "").replace("\n", "")


def _close_socket(sock) -> None:
    """Wake a peer thread blocked in ``accept()`` and release the port.

    ``close()`` alone does not do it: the listener stays alive, the port
    stays bound, and a reloaded instance fails to bind - the M12 symptom
    the SFTP branch hit first. Shared by the SFTP and SMB teardown paths.
    """
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except Exception:
        pass


class DownloadServerService:
    def __init__(
        self,
        store: MetaStorePort,
        config: dict | None = None,
        download_info: Callable[..., Awaitable[tuple[str, str]]] | None = None,
    ):
        self.store = store
        # Config object injection: unified PluginConfig boundary (dicts pass
        # through for compatibility, see core.config.model)
        cfg = config if isinstance(config, PluginConfig) else PluginConfig(config or {})
        self.enabled = bool(cfg.get("download_server_enabled", False))
        self.host = str(cfg.get("download_server_host", "127.0.0.1") or "127.0.0.1")
        # What links, SMB UNC and SFTP info advertise. Empty falls back to the
        # bind host so a single-address deployment keeps one knob.
        published = str(cfg.get("download_public_host", "") or "").strip()
        self.wildcard_publish = published in _WILDCARD_HOSTS
        if published and not self.wildcard_publish:
            self.public_host = published
        elif self.host in _WILDCARD_HOSTS:
            # Binding everywhere publishes no usable address; a wildcard typed
            # into download_public_host is handled as a config error instead.
            # Loopback keeps the local use that a wildcard bind has always
            # served, and the warning says what to set for remote clients.
            self.public_host = "127.0.0.1"
        else:
            self.public_host = self.host
        self.http_port = int(cfg.get("download_http_port", 0) or 0)
        self.sftp_port = int(cfg.get("download_sftp_port", 0) or 0)
        self.smb_port = int(cfg.get("download_smb_port", 0) or 0)
        self.token = str(cfg.get("download_token", "") or "")
        self.allow_private = bool(cfg.get("fetch_allow_private_address", False))
        # Download-cache housekeeping budget (download_cache.sweep_cache).
        # The mkdtemp root used to grow until the next plugin reload: the
        # rmtree in shutdown() was the only cleanup, so a long-running bot
        # filled the temp dir. 0 (or negative) disables the rule.
        self.cache_max_bytes = (
            int(cfg.get("download_cache_max_mb", 1024) or 0) * 1024 * 1024
        )
        self.cache_ttl_seconds = (
            int(cfg.get("download_cache_ttl_hours", 24) or 0) * 3600
        )
        self._cache_swept_at: float | None = None
        self._download_info = download_info
        self._http_server: asyncio.AbstractServer | None = None
        self._sftp_thread: threading.Thread | None = None
        self._sftp_server = None
        self._sftp_sock: socket.socket | None = None
        self._smb_sock: socket.socket | None = None
        self._smb_thread: threading.Thread | None = None
        self._smb_server = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sftp_auth = ("cloud", self.token)
        # Private mkdtemp root (0700, owner-only): a predictable fixed name
        # in the shared temp dir invites pre-creation/symlink tricks.
        self._cache_root = Path(tempfile.mkdtemp(prefix="cloudstorage-"))
        self._cache_dir = self._cache_root / "cloudsftp"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._smb_dir = self._cache_root / "cloudsmb"
        self._smb_dir.mkdir(parents=True, exist_ok=True)
        # Staged-file registry: token -> {path, name} (essence text exports
        # and other host-generated artifacts served over http/sftp)
        self._staged: dict[str, dict] = {}
        self._row_cache: dict[tuple[str, str], tuple[float, dict]] = {}
        # One lock per cache path: concurrent SFTP opens of one resource must
        # not each re-download it (download_server_io.materialize_to_cache)
        self._cache_locks: dict[str, threading.Lock] = {}
        self._cache_locks_guard = threading.Lock()
        # Remote-URL proxy registry (bad-link #18: Content-Disposition
        # injection so offline download stores the real filename)
        self._proxy_registry = ProxyRegistry()
        self.smb_available = False
        try:  # optional dependency: keep http/sftp working without impacket
            import impacket  # noqa: F401

            self.smb_available = True
        except ImportError:
            self.smb_available = False

    # ---------- Addresses ----------

    def http_base(self) -> str:
        return f"http://{self.public_host}:{self.http_port}"

    def download_url(self, group_id: str, id: int) -> str:
        return (
            f"{self.http_base()}/download?group={group_id}&id={id}&token={self.token}"
        )

    def register_proxy(self, url: str, name: str, *, allow_private: bool = False) -> str:
        """Register a remote URL under a fixed name; returns a proxied URL."""
        return self._proxy_registry.register(
            url, name, self.http_base(), self.token, allow_private=allow_private
        )

    def register_staged(self, path: str | Path, name: str) -> dict:
        """Register a local file as a downloadable artifact; returns its
        http/ftp/smb address info (used by essence text distribution)."""
        p = Path(path)
        token = uuid.uuid4().hex[:10]
        self._staged[token] = {"path": str(p), "name": name or p.name, "ts": time.time()}
        # Keep the registry bounded: drop entries older than 24h and unlink
        # orphaned temp files so they don't accumulate on disk.
        now = time.time()
        for k in [k for k, v in self._staged.items() if now - v["ts"] > 86400]:
            entry = self._staged.pop(k, None)
            if entry is not None:
                try:
                    Path(entry["path"]).unlink(missing_ok=True)
                except OSError:
                    pass
        http_url = f"{self.http_base()}/download?staged={token}&token={self.token}"
        info: dict = {"token": token, "http_url": http_url}
        if self.sftp_port > 0:
            info["sftp"] = {
                **self.sftp_info(),
                "path": f"/staged/{token}_{quote(name or p.name)}",
            }
        if self.smb_port > 0 and self.smb_available:
            safe = f"staged_{token}_{Path(name or p.name).name}"
            try:
                target = self._smb_dir / safe
                if Path(p).resolve() != target.resolve():
                    # Streamed copy: read_bytes() held a second full copy of
                    # the artifact in RAM (staged exports can be large).
                    shutil.copyfile(p, target)
                info["smb"] = {"share": self.smb_share(), "path": f"{safe}"}
            except OSError as e:
                logger.debug(f"[dlserver] staged smb copy failed: {e}")
        return info

    def smb_share(self) -> str:
        return "cloud"

    def smb_info(self, group_id: str, name: str) -> dict:
        safe = f"{group_id}_{Path(name or 'file').name}"
        return {
            "share": self.smb_share(),
            "path": safe,
            "unc": f"\\\\{self.public_host}\\{self.smb_share()}\\{safe}",
        }

    async def ensure_local(self, group_id: str, id: int, name: str) -> Path | None:
        """Materialize a cloud resource into the SMB cache directory (best
        effort; used so the SMB share has the file when the user opens it).

        The copy runs in a worker thread: reassembled files can be
        gigabytes and a blocking read/write would stall the event loop.
        """
        try:
            src, _ = await self._download_info(str(group_id), int(id))
        except Exception as e:
            logger.debug(f"[dlserver] smb materialize failed for {group_id}/{id}: {e}")
            return None
        sp = Path(src)
        if not sp.exists():
            return None
        target = self._smb_dir / f"{group_id}_{Path(name or sp.name).name}"

        def _copy() -> None:
            if sp.resolve() == target.resolve():
                return
            # Stream in chunks: read_bytes() would hold a whole copy in RAM
            with sp.open("rb") as fin, target.open("wb") as fout:
                while True:
                    chunk = fin.read(1024 * 1024)
                    if not chunk:
                        break
                    fout.write(chunk)
            # The reassembled source is disposable once it sits in the share
            # directory: download_info() produces a fresh file per call, so
            # nothing else can reference it (M8).
            cleanup_recon(sp)

        try:
            await asyncio.to_thread(_copy)
        except OSError as e:
            logger.debug(f"[dlserver] smb cache copy failed: {e}")
            return None
        return target

    def sftp_info(self) -> dict:
        return {
            "host": self.public_host,
            "port": self.sftp_port,
            "user": self._sftp_auth[0],
            "password": self._sftp_auth[1],
        }

    def smb_credentials(self) -> tuple[str, str]:
        """Fixed user + download_token as the share password: the SMB channel
        authenticates with the same pair as SFTP (M14)."""
        return self._sftp_auth

    # ---------- Cross-thread calls (SFTP thread -> plugin main loop) ----------

    def _run_in_loop(self, coro, timeout: float = 180.0):
        """Cross-thread call: run a coroutine on the plugin's main event loop
        (SFTP thread -> asyncio).

        ``coro`` is either a coroutine object or a zero-argument callable
        returning one; ``asyncio.run_coroutine_threadsafe`` only accepts the
        former. A callable used to be passed straight through (or, worse, a
        misspelled call site passed nothing awaitable at all), and because the
        returned future never completes when no task was ever scheduled, the
        failure surfaced 180s later as a bare TimeoutError rather than a
        TypeError at the offending line -- _find_row's caller then swallowed it
        and reported SFTP_NO_SUCH_FILE for every file. Normalise here so both
        shapes are safe and a genuine mistake fails fast.
        """
        if self._loop is None:
            raise RuntimeError("dlserver loop not ready")
        if callable(coro) and not inspect.iscoroutine(coro):
            coro = coro()
        if not inspect.iscoroutine(coro):
            raise TypeError(
                f"_run_in_loop expects a coroutine or a callable returning one, "
                f"got {type(coro).__name__}"
            )
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    # ---------- Lifecycle ----------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if not self.enabled:
            logger.info("[dlserver] disabled by config")
            return
        if not self.token:
            logger.warning(
                "[dlserver] download_server_enabled=true but download_token is "
                "empty — download service disabled (fail-closed). "
                "Set download_token in plugin config to enable."
            )
            self.enabled = False
            return
        # A wildcard is a bind address, not something a client can dial: a link
        # built from it (`http://0.0.0.0:6186`) resolves to the client's own
        # host. Typing one into download_public_host is a config error we
        # cannot guess past, so fail closed the way an empty token does.
        if self.wildcard_publish:
            logger.warning(
                f"[dlserver] download_public_host={self.public_host!r} is a wildcard "
                "— download service disabled (fail-closed). Bind on 0.0.0.0 is fine; "
                "set download_public_host to the address clients should reach."
            )
            self.enabled = False
            return
        if self.host in _WILDCARD_HOSTS:
            logger.info(
                "[dlserver] binding a wildcard address; links advertise 127.0.0.1. "
                "Set download_public_host for LAN / OpenList-container clients."
            )
        if self.http_port > 0:
            self._http_server = await asyncio.start_server(
                self._handle_http, self.host, self.http_port
            )
            logger.info(f"[dlserver] http download on :{self.http_port}")
        if self.sftp_port > 0:
            self._start_sftp()
        if self.smb_port > 0:
            self._start_smb()

    async def shutdown(self) -> None:
        if self._http_server is not None:
            self._http_server.close()
            await self._http_server.wait_closed()
            self._http_server = None
        if self._sftp_server is not None:
            try:
                self._sftp_server.close()
            except Exception:
                pass
            self._sftp_server = None
        _close_socket(self._sftp_sock)
        self._sftp_sock = None
        # Accept loop stopped: join live sessions so no handler outlives the
        # service (M20).
        join_sftp_connections(self)
        if self._sftp_thread is not None and self._sftp_thread.is_alive():
            self._sftp_thread.join(timeout=2.0)
        # SMB rides raw socketserver, not asyncio: there is no wait_closed()
        # counterpart. stop() is not a teardown - it calls server_close()
        # (a no-op on BaseServer) and never sets the __shutdown_request flag
        # that serve_forever() polls, while the SMB thread is not a daemon.
        # The old stop() + join(2.0) therefore always burned the full timeout
        # and left the instance alive with the port still bound (L7, the M12
        # twin that the SFTP branch had already fixed for itself).
        stop_smb_server(self)
        _close_socket(self._smb_sock)
        self._smb_sock = None
        # Let the SMB thread leave start() before the cache root is removed
        # below: deleting the share directory from under a live server is
        # exactly what the local-variable instance used to allow (M12).
        # server_close() has already woken the thread, so this join now
        # returns promptly instead of timing out.
        if self._smb_thread is not None and self._smb_thread.is_alive():
            self._smb_thread.join(timeout=2.0)
        # Remove the private cache root created in __init__ (mkdtemp is ours
        # to clean up, per the tempfile contract).
        if getattr(self, "_cache_root", None) is not None:
            shutil.rmtree(self._cache_root, ignore_errors=True)
            self._cache_root = None

    # ---------- HTTP ----------

    async def _handle_http(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            # BUG-14: readuntil timeout stops slowloris-style header holding.
            request = (await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=30.0
            )).decode("latin-1")
            line = request.split("\r\n", 1)[0]
            parts = line.split(" ")
            if len(parts) < 2:
                await self._reply(writer, 400, b"bad request")
                return
            method, raw_path = parts[0], parts[1]
            headers = {}
            for hline in request.split("\r\n")[1:]:
                hkey, _, hval = hline.partition(":")
                if hkey:
                    headers[hkey.strip().lower()] = hval.strip()
            parsed = urlparse(raw_path)
            q = parse_qs(parsed.query)
            if parsed.path == "/health":
                await self._reply(writer, 200, b"ok")
                return
            if parsed.path != "/download" or method != "GET":
                await self._reply(writer, 404, b"not found")
                return
            if not self.token or not hmac.compare_digest(
                q.get("token", [""])[0].encode("utf-8"), self.token.encode("utf-8")
            ):
                await self._reply(writer, 401, b"unauthorized")
                return
            group = q.get("group", [""])[0]
            rid = q.get("id", [""])[0]
            staged = q.get("staged", [""])[0]
            proxy = q.get("proxy", [""])[0]
            if proxy:
                entry = self._proxy_registry.pop(proxy)
                if not entry:
                    await self._reply(writer, 404, b"proxy not found")
                    return
                await serve_proxy(writer, entry, self._reply)
                return
            if staged:
                # Staged artifact (e.g. essence text export); the token was
                # already verified above for every request.
                entry = self._staged.get(staged)
                if not entry or not Path(entry["path"]).exists():
                    await self._reply(writer, 404, b"staged file not found")
                    return
                await serve_staged(writer, entry, self._reply)
                return
            if not group or not rid.isdigit():
                await self._reply(writer, 400, b"bad request")
                return
            try:
                src, name = await self._download_info(group, int(rid))
            except ValueError as e:
                # Client-side condition (unknown group/id, volumes not ready);
                # the generic handler below would mask it as 500.
                logger.debug(f"[dlserver] download info rejected: {e}")
                await self._reply(writer, 404, b"resource not found")
                return
            name = _safe_header_name(name)
            src = _safe_header_name(str(src))  # OneBot URL -> raw Location header
            # src arrives already encoded for the reply head: any inline comment
            # placed between this statement and Path(src) is consumed as the
            # expression (L1) - use a block comment only when needed.
            src_path = Path(src)
            if not src_path.exists():
                # Single file: the target is a live CDN link, not a local
                # path. A bare 302 to that link carries no Content-
                # Disposition, and a QQ CDN URL ends in a spec segment
                # (/0 /400 /800), so OpenList fell back to the URL tail and
                # stored the file under a numeric name (Issue #8). Wrap it in
                # the proxy registry instead and 302 to *our* proxy URL:
                # OpenList follows the redirect and hits serve_proxy(), which
                # answers with a proper Content-Disposition. The body now
                # relays through this process -- the accepted trade for a
                # correct filename.
                proxied = self.register_proxy(src, name, allow_private=self.allow_private)
                body = (
                    f"HTTP/1.1 302 Found\r\nLocation: {proxied}\r\n"
                    f"Content-Length: 0\r\nConnection: close\r\n\r\n"
                ).encode("latin-1")
                writer.write(body)
                await writer.drain()
                writer.close()
                return
            # Volumes/videos: stream the locally reassembled file with byte
            # range support (Accept-Ranges / 206 / 416); the recon_* source
            # is reclaimed once the body is on the wire.
            await serve_local_file(writer, src_path, name, headers.get("range"))
        except Exception as e:
            logger.warning(f"[dlserver] http error: {e}")
            try:
                await self._reply(writer, 500, b"internal error")
            except Exception:
                pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    @staticmethod
    async def _reply(writer, code: int, body: bytes) -> None:
        reason = {
            200: "OK",
            400: "Bad Request",
            401: "Unauthorized",
            404: "Not Found",
            500: "Internal Error",
        }.get(code, "ERR")
        writer.write(
            (
                f"HTTP/1.1 {code} {reason}\r\n"
                f"Content-Type: text/plain\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("latin-1")
            + body
        )
        await writer.drain()

    # ---------- SFTP (paramiko virtual filesystem: /<group_id>/<filename>) ----------

    def _find_row(self, group: str, name: str) -> dict:
        """Resolve one file row without paging the whole group (L7).

        ``collect_resources`` walks every page (a group can hold more rows
        than a single page returns), so every SFTP stat/open could cost
        hundreds of SQL queries. The store's keyword filter is a superset of
        the exact name match, so ask for that first and fall back to the full
        scan only when it misses. Hits are cached for a few seconds because a
        client stats and then opens the same path.
        """
        key = (group, name)
        now = time.monotonic()
        hit = self._row_cache.get(key)
        if hit is not None and now - hit[0] < _ROW_CACHE_TTL:
            return dict(hit[1])

        async def _lookup():
            from core.domain.sync import ResourceQuery

            result = await self.store.query_resources(
                ResourceQuery(
                    group_id=group, keyword=name, page_size=_LOOKUP_PAGE
                )
            )
            for it in list(getattr(result, "items", None) or []):
                if it.name == name:
                    return {"id": it.id, "size": it.size}
            for it in await collect_resources(self.store, group):
                if it.name == name:
                    return {"id": it.id, "size": it.size}
            raise FileNotFoundError(name)

        # The argument is a coroutine OBJECT built here and handed to the
        # loop thread (see _run_in_loop below).
        row = self._run_in_loop(_lookup())
        if len(self._row_cache) > 1024:  # bounded: a long-lived service
            self._row_cache.clear()
        self._row_cache[key] = (now, dict(row))
        return row

    def _start_sftp(self) -> None:
        """Start the paramiko virtual FS (implementation lives in
        download_server_io: this module sits on the 700-line gate)."""
        start_sftp_server(self)

    # ---------- SMB (impacket smbserver, optional dependency) ----------

    def _start_smb(self) -> None:
        """SMB share over the cache directory (share name "cloud").

        impacket is an optional dependency: without it the SMB channel is
        disabled and callers fall back to the http/sftp notice. Entries are
        materialized on demand (ensure_local / register_staged write into
        the shared directory before the user opens the UNC path).
        """
        start_smb_server(self)

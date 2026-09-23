"""Download-server I/O helpers (split out of download_server.py).

download_server.py sits right under the 700-line architecture gate, so the
pieces that are not part of the service's public surface live here:

- byte-range parsing plus the local-file streaming response (the recon_*
  reassembly artifact is reclaimed once the body has been written)
- group resource pagination (the SFTP virtual FS used to stop at row 500)
- the SFTP cache materializer (repeat opens reuse the cached bytes)
- the SFTP virtual filesystem and its accept loop (one thread per connection)

The SMB bootstrap moved to download_smb.py (this module sits on the 700-line
gate) and is re-exported below, so importers keep their existing path.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import shutil
import socket
import threading
import time
import uuid
from pathlib import Path

from core.application.download_cache import maybe_sweep
from core.application.download_proxy import _content_disposition
from core.log import logger

from core.application.download_smb import (
    configure_smb_server as configure_smb_server,
    start_smb_server as start_smb_server,
    stop_smb_server as stop_smb_server,
)

_STREAM_CHUNK = 1 << 16
_RECON_PREFIX = "recon_"
# A client that connects and stops reading (TCP zero window) must not pin the
# request coroutine, the src_path handle and the recon_* source forever (L6).
_DRAIN_TIMEOUT = 30.0
# Sentinel returned by parse_range(): a syntactically valid Range that no byte
# of the body can satisfy (RFC 9110 -> 416), as opposed to a header we choose
# to ignore (-> full 200 body).
_UNSATISFIABLE = "\x00unsatisfiable"


# ---------- HTTP: byte ranges + local file streaming ----------


def parse_range(value: str | None, total: int) -> tuple[int, int] | str | None:
    """Parse a single-range ``Range: bytes=...`` header value.

    Returns an inclusive ``(start, end)``, ``None`` when the whole body must
    be sent (absent / malformed / multi-range: fail open to 200), or
    ``_UNSATISFIABLE`` for a valid range beyond the body length.
    """
    if not value:
        return None
    spec = value.strip()
    if not spec.lower().startswith("bytes="):
        return None
    spec = spec[len("bytes="):].strip()
    if "," in spec or "-" not in spec:
        return None
    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()
    try:
        if not first:  # suffix form: the trailing N bytes
            n = int(last)
            if n <= 0 or total <= 0:
                return _UNSATISFIABLE
            return (max(0, total - n), total - 1)
        start = int(first)
        end = int(last) if last else total - 1
    except ValueError:
        return None
    if start < 0 or start >= total or end < start:
        return _UNSATISFIABLE
    return (start, min(end, total - 1))


def cleanup_recon(path: Path) -> None:
    """Unlink a ``recon_*`` reassembly artifact (best effort).

    Only reassembler output matches: a single-file download hands back a
    cloud URL, never a local path, so nothing else can be removed by
    accident.
    """
    try:
        if path.name.startswith(_RECON_PREFIX):
            path.unlink(missing_ok=True)
    except OSError as e:
        logger.debug(f"[dlserver] recon cleanup failed for {path.name}: {e}")


async def _drain(writer) -> None:
    """``writer.drain()`` with a deadline (L6).

    Without it a client that opens the connection and never reads (TCP zero
    window) blocks this coroutine forever: the src_path handle and the
    recon_* artifact that ``finally`` is meant to reclaim stay behind.
    """
    await asyncio.wait_for(writer.drain(), timeout=_DRAIN_TIMEOUT)


async def serve_local_file(
    writer, src_path: Path, name: str, range_header: str | None
) -> None:
    """Stream a locally materialized file (reassembled volumes/videos).

    Adds the byte-range handling the endpoint used to lack (Accept-Ranges,
    206 + Content-Range, 416) and reclaims the recon_* artifact once the
    body is on the wire: download_info() produces a fresh one per call, so
    the file is ours alone.
    """
    try:
        total = src_path.stat().st_size
        span = parse_range(range_header, total)
        if span is _UNSATISFIABLE:
            head = (
                "HTTP/1.1 416 Range Not Satisfiable\r\n"
                f"Content-Range: bytes */{total}\r\n"
                "Content-Length: 0\r\nConnection: close\r\n\r\n"
            ).encode("latin-1")
            writer.write(head)
            await _drain(writer)
            return
        if span is None:
            start, end, code = 0, total - 1, 200
        else:
            start, end, code = span[0], span[1], 206
        length = max(0, end - start + 1)
        head = (
            f"HTTP/1.1 {code} {'Partial Content' if code == 206 else 'OK'}\r\n"
            "Content-Type: application/octet-stream\r\n"
            "Accept-Ranges: bytes\r\n"
            f"Content-Length: {length}\r\n"
            + (f"Content-Range: bytes {start}-{end}/{total}\r\n" if code == 206 else "")
            + f"Content-Disposition: {_content_disposition(name)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head)
        remaining = length
        with src_path.open("rb") as fh:
            if start:
                fh.seek(start)
            while remaining > 0:
                chunk = fh.read(min(_STREAM_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                writer.write(chunk)
                await _drain(writer)
    finally:
        cleanup_recon(src_path)


# ---------- Resource pagination ----------


async def collect_resources(
    store, group_id: str, *, page_size: int = 500, max_pages: int = 200
) -> list:
    """Every resource row of one group, paging until the store is exhausted.

    The SFTP virtual FS used a single page_size=500 query, so the 501st file
    of a group could be neither listed nor opened.
    """
    from core.domain.sync import ResourceQuery

    items: list = []
    page = 1
    while page <= max_pages:
        result = await store.query_resources(
            ResourceQuery(group_id=group_id, page_size=page_size, page=page)
        )
        batch = list(getattr(result, "items", None) or [])
        items.extend(batch)
        total = int(getattr(result, "total", 0) or 0)
        if not batch or len(batch) < page_size or len(items) >= total:
            break
        page += 1
    return items


# ---------- SFTP cache materialization ----------


def _cache_lock(svc, key: str) -> threading.Lock:
    """One lock per cache path: SFTP is a thread per connection.

    Two clients opening the same resource used to resolve the CDN link and
    download it independently - a duplicate transfer, a duplicate recon_*
    reassembly artifact, and one wasted download per extra opener. The
    registry is bounded by the number of distinct resources opened in the
    service's lifetime (same shape as svc._row_cache).
    """
    with svc._cache_locks_guard:
        lock = svc._cache_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            svc._cache_locks[key] = lock
        return lock


def cache_path(svc, info: dict) -> Path:
    # Last path component only: group/name are group-file data, and a value
    # carrying "/" or ".." would write the cache outside _cache_dir.
    group = Path(str(info["group"])).name or "group"
    name = Path(str(info["name"])).name or "file"
    return svc._cache_dir / f"{group}_{info['id']}_{name}"


def materialize_to_cache(svc, info: dict) -> Path:
    """Local path for a cloud resource, materialized on first open.

    M7: the cache is consulted before anything else. It used to be written
    but never read, so every SFTP open re-resolved the CDN link and
    downloaded (re-reassembling volumes) again: one leaked recon_* file per
    open and no reuse at all.

    Serialised per cache path: two clients opening one resource at the same
    time must not both download it (see _cache_lock).
    """
    cache = cache_path(svc, info)
    if _cache_is_complete(cache, info):
        logger.debug(f"[dlserver] cache hit: {cache.name}")
        return cache
    # Housekeeping on the miss path: the cache root used to grow until the
    # next plugin reload (shutdown()'s rmtree was the only cleanup). Never
    # let it break the download itself.
    try:
        maybe_sweep(svc)
    except Exception as e:  # housekeeping must never break a download
        logger.debug(f"[dlserver] cache sweep failed: {e}")
    with _cache_lock(svc, str(cache)):
        # Double check: a concurrent opener may have filled the cache while
        # this thread waited for the lock, in which case it already did the
        # work and reclaimed its own recon_* source.
        if _cache_is_complete(cache, info):
            logger.debug(f"[dlserver] cache hit: {cache.name}")
            return cache
        src, _ = svc._run_in_loop(svc._download_info(info["group"], info["id"]))
        sp = Path(src)
        if sp.exists():
            try:
                _copy_into_cache(sp, cache)
            finally:
                # The bytes are cached now, or the copy is unrecoverable:
                # either way the recon_* source is disposable. Cleaning up
                # only on the success path leaked one reassembly artifact
                # per failed copy (L6).
                cleanup_recon(sp)
        else:
            _stream_url_into_cache(svc, src, cache, info)
    return cache



def _cache_is_complete(cache: Path, info: dict) -> bool:
    """A cache hit is reused only when the file is actually whole (L6).

    ``cache.exists()`` alone accepted a truncated or zero-length placeholder
    and served it as the resource for every later open.
    """
    try:
        actual = cache.stat().st_size
    except OSError:
        return False
    expected = int(info.get("size") or 0)
    return actual >= expected if expected > 0 else actual > 0


def _copy_into_cache(sp: Path, cache: Path) -> None:
    tmp = cache.with_name(cache.name + f".{uuid.uuid4().hex[:8]}.part")
    try:
        with sp.open("rb") as fin, tmp.open("wb") as out:
            shutil.copyfileobj(fin, out, _STREAM_CHUNK)
        os.replace(tmp, cache)
    finally:
        tmp.unlink(missing_ok=True)


def _stream_url_into_cache(svc, src: str, cache: Path, info: dict) -> None:
    """Cloud direct link (from the cloud API) streamed to the cache file.

    Delegates to the plugin-wide secure_fetch implementation (sync twin --
    this runs on an SFTP connection thread): per-hop SSRF re-validation
    replaces the old single pre-check that refused redirects outright, and
    the body is capped near the expected resource size instead of unbounded.
    """
    from adapters.external.secure_fetch import FetchPolicy, fetch_to_file_sync

    expected = int(info.get("size") or 0)
    # +1 MiB slack: tolerate small metadata drift without breaking a
    # legitimate download; unknown size falls back to the QQ group file
    # ceiling (~10 GB).
    max_bytes = expected + (1 << 20) if expected > 0 else 10 * 1024**3
    svc._run_in_loop(
        asyncio.to_thread(
            fetch_to_file_sync,
            src,
            cache,
            FetchPolicy(max_bytes=max_bytes, timeout=180.0, allow_private=False),
            site="download_server",
        )
    )


# ---------- SFTP (paramiko virtual filesystem) ----------


def build_sftp_interface(svc):
    """Build the read-only paramiko SFTPServerInterface subclass for a
    service. Module level (instead of nested in start_sftp_server) so the
    virtual FS semantics - listing and open() - are testable on their own."""
    import paramiko
    from paramiko import SFTP_FAILURE, SFTP_NO_SUCH_FILE
    from paramiko import SFTP_OP_UNSUPPORTED, SFTP_PERMISSION_DENIED

    class _SFTPInterface(paramiko.SFTPServerInterface):
        """Read-only virtual FS: /<group_id>/<filename> plus /staged/<token>_<name>;
        cloud content materializes to the cache directory on first open and is
        reused by every later open."""

        def _resolve(self, path: str) -> dict | None:
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2 and parts[0] == "staged":
                token = parts[1].split("_", 1)[0]
                entry = svc._staged.get(token)
                if not entry or not Path(entry["path"]).exists():
                    return None
                return {
                    "staged": token,
                    "path": entry["path"],
                    "name": entry["name"],
                    "size": Path(entry["path"]).stat().st_size,
                }
            if len(parts) != 2:
                return None
            group, name = parts
            try:
                row = svc._find_row(group, name)
            except FileNotFoundError:
                return None
            except Exception as e:
                # A store failure / loop timeout used to escape stat()/open()
                # and abort the whole SSH session instead of answering
                # SFTP_NO_SUCH_FILE (L6).
                logger.debug(f"[dlserver] sftp resolve failed {group}/{name}: {e}")
                return None
            return {
                "group": group,
                "name": name,
                "id": row["id"],
                "size": int(row.get("size") or 0),
            }

        def list_folder(self, path: str):
            parts = [p for p in path.split("/") if p]
            if not parts:
                async def _groups():
                    groups = await svc.store.list_groups()
                    entries = []
                    for g in groups:
                        info = paramiko.SFTPAttributes()
                        info.filename = str(g.group_id)
                        info.st_mode = 0o40555  # dr-xr-xr-x
                        entries.append(info)
                    if svc._staged:
                        info = paramiko.SFTPAttributes()
                        info.filename = "staged"
                        info.st_mode = 0o40555
                        entries.append(info)
                    return entries

                try:
                    return svc._run_in_loop(_groups())
                except Exception:
                    return []
            if parts[0] == "staged" and len(parts) == 1:
                entries = []
                for token, entry in list(svc._staged.items()):
                    info = paramiko.SFTPAttributes()
                    info.filename = f"{token}_{entry['name']}"
                    try:
                        info.st_size = Path(entry["path"]).stat().st_size
                    except OSError:
                        continue
                    # U4: same mode as stat(): a staged artifact is a plain
                    # readable file, and a listing that omits the mode leaves
                    # clients with "unknown" permissions for a file they can
                    # stat() just fine.
                    info.st_mode = 0o100644
                    entries.append(info)
                return entries
            if len(parts) == 1:
                async def _files():
                    entries = []
                    for it in await collect_resources(svc.store, parts[0]):
                        info = paramiko.SFTPAttributes()
                        info.filename = it.name
                        info.st_size = it.size
                        # U4: list_folder must report the same mode as stat()
                        # below, otherwise OpenSSH clients (and callers that
                        # derive an mtime filter from the listing) treat the
                        # entry as "unknown" and skip or fail it.
                        info.st_mode = 0o100644  # -rw-r--r--
                        entries.append(info)
                    return entries

                try:
                    return svc._run_in_loop(_files())
                except Exception:
                    return []
            return []

        def stat(self, path: str):
            info = self._resolve(path)
            if info is None:
                return SFTP_NO_SUCH_FILE
            attrs = paramiko.SFTPAttributes()
            attrs.st_size = info.get("size") or 0
            attrs.st_mode = 0o100644  # -rw-r--r--
            return attrs

        def lstat(self, path: str):
            return self.stat(path)

        def open(self, path: str, flags: int, attr):
            info = self._resolve(path)
            if info is None:
                return SFTP_NO_SUCH_FILE
            fh = None
            try:
                if info.get("staged"):
                    fh = Path(info["path"]).open("rb")
                else:
                    # Cache-first: a repeat open must not hit the cloud again
                    fh = materialize_to_cache(svc, info).open("rb")
                # paramiko contract: SFTPHandle whose `readfile` delegates
                # read()/close() to the python file object.
                handle = paramiko.SFTPHandle()
                handle.readfile = fh
                return handle
            except Exception as e:
                if fh is not None:  # a half-built handle must not leak the fd
                    try:
                        fh.close()
                    except Exception:
                        pass
                logger.debug(f"[dlserver] sftp open failed: {e}")
                return SFTP_FAILURE

        def remove(self, path: str):
            return SFTP_PERMISSION_DENIED

        def rename(self, oldpath: str, newpath: str):
            return SFTP_PERMISSION_DENIED

        def mkdir(self, path: str, attr):
            return SFTP_PERMISSION_DENIED

        def rmdir(self, path: str):
            return SFTP_PERMISSION_DENIED

        def chattr(self, path: str, attr):
            return SFTP_PERMISSION_DENIED

        def symlink(self, target_path: str, path: str):
            return SFTP_PERMISSION_DENIED

        def readlink(self, path: str):
            return SFTP_OP_UNSUPPORTED

    return _SFTPInterface


def start_sftp_server(svc) -> None:
    """Start the paramiko SFTP server on its own thread.

    One thread per accepted connection: the accept loop used to run the whole
    SSH session inline (the comment claimed per-connection threads but none
    were ever created), so a single slow client blocked every later
    connection until it went away.
    """
    try:
        import paramiko
    except ImportError:
        logger.warning(
            "[dlserver] paramiko not installed; sftp disabled "
            "(pip install paramiko)"
        )
        return

    from paramiko.sftp_server import SFTPServer

    _SFTPInterface = build_sftp_interface(svc)
    # Fresh ephemeral host key per boot: read-only service, key re-accept
    # after restart is acceptable.
    host_key = paramiko.RSAKey.generate(2048)

    class _ServerInterface(paramiko.ServerInterface):
        def get_allowed_auths(self, username):
            return "password"

        def check_auth_password(self, username, password):
            # compare_digest on UTF-8 bytes: constant time, no TypeError
            # on non-ASCII input (str compare_digest rejects it).
            a0, a1 = (s.encode("utf-8") for s in svc._sftp_auth)
            u = str(username or "").encode("utf-8")
            p = str(password or "").encode("utf-8")
            ok = hmac.compare_digest(u, a0) and hmac.compare_digest(p, a1)
            return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

        def check_channel_request(self, kind, chanid):
            # paramiko answers OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED for
            # every channel by default, so open_sftp() was refused right
            # after a successful authentication (H6).
            if kind == "session":
                return paramiko.OPEN_SUCCEEDED
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_shell_request(self, channel):
            return False

        def check_channel_pty_request(
            self, channel, term, width, height, pixelwidth, pixelheight, modes
        ):
            return False

    svc._sftp_conn_threads: list[threading.Thread] = []
    svc._sftp_conn_lock = threading.Lock()

    def _handle_client(client_sock) -> None:
        transport = None
        try:
            transport = paramiko.Transport(client_sock)
            transport.local_version = "SSH-2.0-AstrBot-SFTP"
            # Subsystem negotiation: the transport starts the SFTP subsystem
            # (SFTPServer runs the session loop) when the client opens an
            # "sftp" channel.
            transport.set_subsystem_handler(
                "sftp", SFTPServer, sftp_si=_SFTPInterface
            )
            transport.add_server_key(host_key)
            transport.start_server(server=_ServerInterface())
            # Block until this session ends; the accept loop keeps running.
            while transport.is_active():
                time.sleep(0.2)
        except Exception as e:
            logger.debug(f"[dlserver] sftp session error: {e}")
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
            else:
                # Transport() itself raised: nobody owns the accepted socket
                # yet, so close it here or every failed connection leaks an
                # fd (L6).
                try:
                    client_sock.close()
                except Exception:
                    pass
            with svc._sftp_conn_lock:
                try:
                    svc._sftp_conn_threads.remove(threading.current_thread())
                except ValueError:
                    pass

    def _serve() -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((svc.host, svc.sftp_port))
            sock.listen(100)
            svc._sftp_sock = sock
            logger.info(f"[dlserver] sftp listening on :{svc.sftp_port}")
            while True:
                try:
                    client_sock, _addr = sock.accept()
                except OSError:
                    break  # socket closed by shutdown()
                conn = threading.Thread(
                    target=_handle_client, args=(client_sock,), daemon=True
                )
                with svc._sftp_conn_lock:
                    svc._sftp_conn_threads.append(conn)
                conn.start()
        except Exception as e:
            logger.warning(f"[dlserver] sftp serve loop failed: {e}")

    svc._sftp_thread = threading.Thread(target=_serve, daemon=True)
    svc._sftp_thread.start()
    logger.info(f"[dlserver] sftp on :{svc.sftp_port}")


def join_sftp_connections(svc, timeout: float = 5.0) -> None:
    """Join live per-connection threads so shutdown leaves no session behind."""
    with getattr(svc, "_sftp_conn_lock", threading.Lock()):
        conns = list(getattr(svc, "_sftp_conn_threads", None) or [])
    for t in conns:
        if t.is_alive():
            t.join(timeout=timeout)

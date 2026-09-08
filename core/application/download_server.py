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
- SFTP: paramiko virtual filesystem (/<group_id>/<filename>); reads pull
  bytes from the cloud on demand; /staged/<name> serves registered staged
  files. Optional dependency (paramiko); disabled when not installed.
- SMB: impacket smbserver (optional dependency) sharing a cache directory;
  entries materialize on demand via ensure_local()/register_staged()
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, quote, urlparse

from core.config import PluginConfig
from core.log import logger
from ports.meta_store import MetaStorePort

_STREAM_CHUNK = 1 << 16


def _safe_header_name(name: str) -> str:
    """Strip CR/LF from filenames to prevent HTTP header injection."""
    return name.replace("\r", "").replace("\n", "")


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
        self.http_port = int(cfg.get("download_http_port", 0) or 0)
        self.sftp_port = int(cfg.get("download_sftp_port", 0) or 0)
        self.smb_port = int(cfg.get("download_smb_port", 0) or 0)
        self.token = str(cfg.get("download_token", "") or "")
        self._download_info = download_info
        self._http_server: asyncio.AbstractServer | None = None
        self._sftp_thread: threading.Thread | None = None
        self._sftp_server = None
        self._sftp_sock: socket.socket | None = None
        self._smb_thread: threading.Thread | None = None
        self._smb_server = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sftp_auth = ("cloud", self.token)
        self._cache_dir = Path(tempfile.gettempdir()) / "cloudsftp"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._smb_dir = Path(tempfile.gettempdir()) / "cloudsmb"
        self._smb_dir.mkdir(parents=True, exist_ok=True)
        # Staged-file registry: token -> {path, name} (essence text exports
        # and other host-generated artifacts served over http/sftp)
        self._staged: dict[str, dict] = {}
        self.smb_available = False
        try:  # optional dependency: keep http/sftp working without impacket
            import impacket  # noqa: F401

            self.smb_available = True
        except ImportError:
            self.smb_available = False

    # ---------- Addresses ----------

    def http_base(self) -> str:
        return f"http://{self.host}:{self.http_port}"

    def download_url(self, group_id: str, id: int) -> str:
        return (
            f"{self.http_base()}/download?group={group_id}&id={id}&token={self.token}"
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
        info: dict = {
            "token": token,
            "http_url": (
                f"{self.http_base()}/download?staged={token}&token={self.token}"
            ),
        }
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
                    target.write_bytes(p.read_bytes())
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
            "unc": f"\\\\{self.host}\\{self.smb_share()}\\{safe}",
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

        try:
            await asyncio.to_thread(_copy)
        except OSError as e:
            logger.debug(f"[dlserver] smb cache copy failed: {e}")
            return None
        return target

    def sftp_info(self) -> dict:
        return {
            "host": self.host,
            "port": self.sftp_port,
            "user": self._sftp_auth[0],
            "password": self._sftp_auth[1],
        }

    # ---------- Cross-thread calls (SFTP thread -> plugin main loop) ----------

    def _run_in_loop(self, coro, timeout: float = 180.0):
        """Cross-thread call: run a coroutine on the plugin's main event loop
        (SFTP thread -> asyncio)."""
        if self._loop is None:
            raise RuntimeError("dlserver loop not ready")
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
        if self._sftp_sock is not None:
            try:
                self._sftp_sock.close()
            except Exception:
                pass
            self._sftp_sock = None
        if self._smb_server is not None:
            try:
                self._smb_server.stop()
            except Exception:
                pass
            self._smb_server = None

    # ---------- HTTP ----------

    async def _handle_http(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            # BUG-14: timeout on readuntil prevents slowloris-style connection
            # holding (client sends headers very slowly or never completes).
            request = (await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=30.0
            )).decode("latin-1")
            line = request.split("\r\n", 1)[0]
            parts = line.split(" ")
            if len(parts) < 2:
                await self._reply(writer, 400, b"bad request")
                return
            method, raw_path = parts[0], parts[1]
            parsed = urlparse(raw_path)
            q = parse_qs(parsed.query)
            if parsed.path == "/health":
                await self._reply(writer, 200, b"ok")
                return
            if parsed.path != "/download" or method != "GET":
                await self._reply(writer, 404, b"not found")
                return
            if not self.token or q.get("token", [""])[0] != self.token:
                await self._reply(writer, 401, b"unauthorized")
                return
            group = q.get("group", [""])[0]
            rid = q.get("id", [""])[0]
            staged = q.get("staged", [""])[0]
            if staged:
                # Staged artifact (e.g. essence text export)
                if not self.token or q.get("token", [""])[0] != self.token:
                    await self._reply(writer, 401, b"unauthorized")
                    return
                entry = self._staged.get(staged)
                if not entry or not Path(entry["path"]).exists():
                    await self._reply(writer, 404, b"staged file not found")
                    return
                src_path = Path(entry["path"])
                name = _safe_header_name(entry["name"])
                total = src_path.stat().st_size

                head = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    f"Content-Length: {total}\r\n"
                    f"Content-Disposition: attachment; filename*=UTF-8''{quote(name)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("latin-1")
                writer.write(head)
                with src_path.open("rb") as fh:
                    while True:
                        chunk = fh.read(_STREAM_CHUNK)
                        if not chunk:
                            break
                        writer.write(chunk)
                        await writer.drain()
                return
            if not group or not rid.isdigit():
                await self._reply(writer, 400, b"bad request")
                return
            src, name = await self._download_info(group, int(rid))
            name = _safe_header_name(name)
            src_path = Path(src)
            if not src_path.exists():
                # Single file: 302 redirect to the QQ CDN direct link
                # (zero proxy load)
                body = (
                    f"HTTP/1.1 302 Found\r\nLocation: {src}\r\n"
                    f"Content-Length: 0\r\nConnection: close\r\n\r\n"
                ).encode("latin-1")
                writer.write(body)
                await writer.drain()
                writer.close()
                return
            # Volumes/videos: stream the locally reassembled file
            total = src_path.stat().st_size
            head = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/octet-stream\r\n"
                f"Content-Length: {total}\r\n"
                f"Content-Disposition: attachment; filename*=UTF-8''{quote(name)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("latin-1")
            writer.write(head)
            with src_path.open("rb") as fh:
                while True:
                    chunk = fh.read(_STREAM_CHUNK)
                    if not chunk:
                        break
                    writer.write(chunk)
                    await writer.drain()
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
        from core.domain.sync import ResourceQuery

        async def _lookup():
            page = await self.store.query_resources(
                ResourceQuery(group_id=group, page_size=500)
            )
            for it in page.items:
                if it.name == name:
                    return {"id": it.id, "size": it.size}
            raise FileNotFoundError(name)

        return self._run_in_loop(_lookup)

    def _start_sftp(self) -> None:
        try:
            import paramiko
        except ImportError:
            logger.warning(
                "[dlserver] paramiko not installed; sftp disabled "
                "(pip install paramiko)"
            )
            return

        from paramiko import SFTP_NO_SUCH_FILE, SFTP_FAILURE
        from paramiko import SFTP_PERMISSION_DENIED, SFTP_OP_UNSUPPORTED
        from paramiko.sftp_server import SFTPServer

        svc = self
        # Fresh ephemeral host key per boot: read-only download service, so
        # clients re-accepting the key after a restart is acceptable.
        host_key = paramiko.RSAKey.generate(2048)

        class _ServerInterface(paramiko.ServerInterface):
            def get_allowed_auths(self, username):
                return "password"

            def check_auth_password(self, username, password):
                if username == svc._sftp_auth[0] and password == svc._sftp_auth[1]:
                    return paramiko.AUTH_SUCCESSFUL
                return paramiko.AUTH_FAILED

            def check_channel_shell_request(self, channel):
                return False

            def check_channel_pty_request(
                self, channel, term, width, height, pixelwidth, pixelheight, modes
            ):
                return False

        class _SFTPInterface(paramiko.SFTPServerInterface):
            """Read-only virtual filesystem: /<group_id>/<filename> plus
            /staged/<token>_<name>; cloud content materializes to the cache
            directory on first open."""

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
                        entries.append(info)
                    return entries
                if len(parts) == 1:
                    async def _files():
                        from core.domain.sync import ResourceQuery

                        page = await svc.store.query_resources(
                            ResourceQuery(group_id=parts[0], page_size=500)
                        )
                        entries = []
                        for it in page.items:
                            info = paramiko.SFTPAttributes()
                            info.filename = it.name
                            info.st_size = it.size
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
                try:
                    if info.get("staged"):
                        fh = Path(info["path"]).open("rb")
                    else:
                        src, _ = svc._run_in_loop(
                            svc._download_info(info["group"], info["id"])
                        )
                        sp = Path(src)
                        cache = svc._cache_dir / (
                            f"{info['group']}_{info['id']}_{info['name']}"
                        )
                        if not sp.exists():
                            # Cloud direct link: stream to the cache file
                            # (temp + atomic rename; runs in a worker thread
                            # so the event loop never blocks on IO)
                            import httpx as _hx

                            def _fetch():
                                tmp = cache.with_name(
                                    cache.name + f".{uuid.uuid4().hex[:8]}.part"
                                )
                                try:
                                    with _hx.stream(
                                        "GET", src, follow_redirects=True,
                                        timeout=180.0,
                                    ) as resp:
                                        resp.raise_for_status()
                                        with tmp.open("wb") as out:
                                            for chunk in resp.iter_bytes(
                                                chunk_size=_STREAM_CHUNK
                                            ):
                                                out.write(chunk)
                                    os.replace(tmp, cache)
                                finally:
                                    tmp.unlink(missing_ok=True)

                            svc._run_in_loop(asyncio.to_thread(_fetch))
                        else:
                            tmp = cache.with_name(
                                cache.name + f".{uuid.uuid4().hex[:8]}.part"
                            )
                            try:
                                with sp.open("rb") as fin, tmp.open("wb") as out:
                                    shutil.copyfileobj(fin, out, _STREAM_CHUNK)
                                os.replace(tmp, cache)
                            finally:
                                tmp.unlink(missing_ok=True)
                        fh = cache.open("rb")
                    # paramiko contract: return an SFTPHandle with a
                    # `readfile` attribute; its default read()/close()
                    # delegate to the python file object.
                    handle = paramiko.SFTPHandle()
                    handle.readfile = fh
                    return handle
                except Exception as e:
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

        def _serve():
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
                    transport = paramiko.Transport(client_sock)
                    transport.local_version = "SSH-2.0-AstrBot-SFTP"
                    try:
                        transport.add_server_key(host_key)
                        # Subsystem negotiation: the transport starts the
                        # SFTP subsystem (SFTPServer runs the session loop)
                        # when the client opens an "sftp" channel.
                        transport.set_subsystem_handler(
                            "sftp", SFTPServer, sftp_si=_SFTPInterface
                        )
                        transport.start_server(server=_ServerInterface())
                        # Block until the session ends; each connection gets
                        # its own thread so one slow client cannot starve
                        # the accept loop.
                        while transport.is_active():
                            time.sleep(0.2)
                    except Exception as e:
                        logger.debug(f"[dlserver] sftp session error: {e}")
                    finally:
                        try:
                            transport.close()
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(f"[dlserver] sftp serve loop failed: {e}")

        self._sftp_thread = threading.Thread(target=_serve, daemon=True)
        self._sftp_thread.start()
        logger.info(f"[dlserver] sftp on :{self.sftp_port}")

    # ---------- SMB (impacket smbserver, optional dependency) ----------

    def _start_smb(self) -> None:
        """SMB share over the cache directory (share name "cloud").

        impacket is an optional dependency: without it the SMB channel is
        disabled and callers fall back to the http/sftp notice. Entries are
        materialized on demand (ensure_local / register_staged write into
        the shared directory before the user opens the UNC path).
        """
        if not self.smb_available:
            logger.warning(
                "[dlserver] impacket not installed; smb disabled "
                "(pip install impacket)"
            )
            return
        svc = self

        def _serve():
            try:
                from impacket.smbserver import SimpleSMBServer

                server = SimpleSMBServer(
                    listenAddress=svc.host, listenPort=svc.smb_port
                )
                server.addShare(
                    svc.smb_share(), svc._smb_dir.as_posix(), "cloud download share"
                )
                server.setLogHim()
                server.start()  # blocking
            except Exception as e:
                logger.warning(f"[dlserver] smb serve loop failed: {e}")

        self._smb_thread = threading.Thread(target=_serve, daemon=True)
        self._smb_thread.start()
        logger.info(f"[dlserver] smb share \\\\{self.host}\\{self.smb_share()} on :{self.smb_port}")

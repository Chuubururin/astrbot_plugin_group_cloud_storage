"""DownloadServerService - local multi-protocol download service (the
plugin acts as the download server).

Semantics (distinct from "egress = push to an external target"):
- The host exposes **download service addresses** (http / ftp / smb);
  external clients hit this host directly to pull cloud files. The address
  (host/port/token) comes from the plugin config
  (_conf_schema download_server_*)
- HTTP: `GET /download?group&id&token` - single files 302-redirect to the
  QQ CDN direct link (zero proxy load); volumes/videos are reassembled
  locally and streamed back. `GET /download?staged=<token>&token` serves a
  registered staged file (e.g. an essence text exported to a .txt)
- FTP: pyftpdlib virtual filesystem (/<group_id>/<filename>); RETR pulls
  bytes from the cloud on demand; /staged/<name> serves registered staged
  files
- SMB: impacket smbserver (optional dependency) sharing a cache directory;
  entries materialize on demand via ensure_local()/register_staged()
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlparse, quote

from core.config import PluginConfig
from core.log import logger
from ports.meta_store import MetaStorePort

_STREAM_CHUNK = 1 << 16


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
        self.ftp_port = int(cfg.get("download_ftp_port", 0) or 0)
        self.smb_port = int(cfg.get("download_smb_port", 0) or 0)
        self.token = str(cfg.get("download_token", "") or "")
        self._download_info = download_info
        self._http_server: asyncio.AbstractServer | None = None
        self._ftp_thread: threading.Thread | None = None
        self._ftp_server = None
        self._smb_thread: threading.Thread | None = None
        self._smb_server = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ftp_auth = ("cloud", self.token or "cloud")
        self._cache_dir = Path(tempfile.gettempdir()) / "cloudftp"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._smb_dir = Path(tempfile.gettempdir()) / "cloudsmb"
        self._smb_dir.mkdir(parents=True, exist_ok=True)
        # Staged-file registry: token -> {path, name} (essence text exports
        # and other host-generated artifacts served over http/ftp)
        self._staged: dict[str, dict] = {}
        self.smb_available = False
        try:  # optional dependency: keep http/ftp working without impacket
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
        # Keep the registry bounded: drop entries older than 24h
        now = time.time()
        for k in [k for k, v in self._staged.items() if now - v["ts"] > 86400]:
            self._staged.pop(k, None)
        info: dict = {
            "token": token,
            "http_url": (
                f"{self.http_base()}/download?staged={token}&token={self.token}"
            ),
        }
        if self.ftp_port > 0:
            info["ftp"] = {
                **self.ftp_info(),
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

    def ftp_info(self) -> dict:
        return {
            "host": self.host,
            "port": self.ftp_port,
            "user": self._ftp_auth[0],
            "password": self._ftp_auth[1],
        }

    # ---------- Cross-thread calls (FTP thread -> plugin main loop) ----------

    def _run_in_loop(self, coro, timeout: float = 180.0):
        """Cross-thread call: run a coroutine on the plugin's main event loop
        (FTP thread -> asyncio)."""
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
        if self.http_port > 0:
            self._http_server = await asyncio.start_server(
                self._handle_http, "0.0.0.0", self.http_port
            )
            logger.info(f"[dlserver] http download on :{self.http_port}")
        if self.ftp_port > 0:
            self._start_ftp()
        if self.smb_port > 0:
            self._start_smb()

    async def shutdown(self) -> None:
        if self._http_server is not None:
            self._http_server.close()
            await self._http_server.wait_closed()
            self._http_server = None
        if self._ftp_server is not None:
            try:
                self._ftp_server.close_all()
            except Exception:
                pass
            self._ftp_server = None
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
            request = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
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
            if self.token and q.get("token", [""])[0] != self.token:
                await self._reply(writer, 401, b"unauthorized")
                return
            group = q.get("group", [""])[0]
            rid = q.get("id", [""])[0]
            staged = q.get("staged", [""])[0]
            if staged:
                # Staged artifact (e.g. essence text export)
                if self.token and q.get("token", [""])[0] != self.token:
                    await self._reply(writer, 401, b"unauthorized")
                    return
                entry = self._staged.get(staged)
                if not entry or not Path(entry["path"]).exists():
                    await self._reply(writer, 404, b"staged file not found")
                    return
                src_path = Path(entry["path"])
                name = entry["name"]
                total = src_path.stat().st_size
                from urllib.parse import quote as _q

                head = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    f"Content-Length: {total}\r\n"
                    f"Content-Disposition: attachment; filename*=UTF-8''{_q(name)}\r\n"
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
                f'Content-Disposition: attachment; filename="{name}"\r\n'
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

    # ---------- FTP (pyftpdlib virtual filesystem: /<group_id>/<filename>) ----------

    def _stat(self, path: str) -> dict:
        parts = [p for p in path.split("/") if p]
        if len(parts) == 2 and parts[0] == "staged":
            # /staged/<token>_<name> -> staged registry lookup
            token = parts[1].split("_", 1)[0]
            entry = self._staged.get(token)
            if not entry or not Path(entry["path"]).exists():
                raise FileNotFoundError(path)
            size = Path(entry["path"]).stat().st_size
            return {"group": "staged", "name": parts[1], "id": 0, "size": size,
                    "staged": token}
        if len(parts) != 2:
            raise FileNotFoundError(path)
        group, name = parts
        row = self._find_row(group, name)
        return {
            "group": group,
            "name": name,
            "id": row["id"],
            "size": int(row.get("size") or 0),
        }

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

    def _start_ftp(self) -> None:
        try:
            from pyftpdlib.authorizers import DummyAuthorizer
            from pyftpdlib.filesystems import AbstractedFS
            from pyftpdlib.handlers import FTPHandler
            from pyftpdlib.servers import FTPServer
        except ImportError:
            logger.warning("[dlserver] pyftpdlib not installed; ftp disabled")
            return

        svc = self

        class CloudFS(AbstractedFS):
            def isdir(self, path):
                return path in ("/", "")

            def isfile(self, path):
                try:
                    svc._stat(path)
                    return True
                except Exception:
                    return False

            def listdir(self, path):
                if path in ("/", ""):
                    async def _groups():
                        groups = await svc.store.list_groups()
                        names = [str(g.group_id) for g in groups]
                        if svc._staged:
                            names.append("staged")
                        return names

                    try:
                        return svc._run(_groups)
                    except Exception:
                        return ["staged"] if svc._staged else []
                if path == "/staged":
                    return [
                        f"{token}_{entry['name']}"
                        for token, entry in list(svc._staged.items())
                    ]
                return []

            def stat(self, path):
                st = svc._stat(path)
                import os as _os

                return _os.stat_result(
                    (
                        33188,
                        0,
                        0,
                        1,
                        0,
                        0,
                        int(st.get("size") or 0),
                        0,
                        0,
                        0,
                    )
                )

            def open(self, path, mode):
                st = svc._stat(path)
                if st.get("staged"):
                    return Path(svc._staged[st["staged"]]["path"]).open("rb")
                src, _ = svc._run(svc._download_info(st["group"], st["id"]))
                sp = Path(src)
                cache = svc._cache_dir / f"{st['group']}_{st['id']}_{st['name']}"
                if not sp.exists():
                    import httpx as _hx

                    def _fetch():
                        resp = _hx.get(src, follow_redirects=True, timeout=180.0)
                        resp.raise_for_status()
                        cache.write_bytes(resp.content)

                    svc._run_in_loop(asyncio.to_thread(_fetch))
                else:
                    cache.write_bytes(sp.read_bytes())
                return cache.open("rb")

        authorizer = DummyAuthorizer()
        authorizer.add_user(self._ftp_auth[0], self._ftp_auth[1], "/", perm="elr")
        handler = FTPHandler
        handler.authorizer = authorizer
        handler.abstracted_fs = CloudFS
        handler.banner = "AstrBot cloud download service"
        try:
            self._ftp_server = FTPServer(("0.0.0.0", self.ftp_port), handler)
            self._ftp_thread = threading.Thread(
                target=self._ftp_server.serve_forever,
                kwargs={"timeout": 1, "blocking": True},
                daemon=True,
            )
            self._ftp_thread.start()
            logger.info(f"[dlserver] ftp download on :{self.ftp_port}")
        except Exception as e:
            logger.warning(f"[dlserver] ftp start failed: {e}")
            self._ftp_server = None

    # ---------- SMB (impacket smbserver, optional dependency) ----------

    def _start_smb(self) -> None:
        """SMB share over the cache directory (share name "cloud").

        impacket is an optional dependency: without it the SMB channel is
        disabled and callers fall back to the http/ftp notice. Entries are
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
                    listenAddress="0.0.0.0", listenPort=svc.smb_port
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

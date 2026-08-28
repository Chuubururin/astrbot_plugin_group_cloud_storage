"""DownloadServerService —— v1.6 本机多协议下载服务（插件作为下载服务端）。

语义（与「中转导出=推送至外部目标」区分）：
- 本机对外提供 **下载服务地址**（http / ftp），外部客户端直接访问本机接口拉取云端文件；
  服务地址（host/端口/token）由 AstrBot 插件配置设置（_conf_schema download_server_*）
- HTTP：`GET /download?group&id&token` —— 单文件 302 跳转 QQ CDN 直链（零代理负载），
  分卷/视频本地重组后流式返回
- FTP：pyftpdlib 虚拟文件系统（/<群号>/<文件名>），RETR 时按需从云端取字节
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlparse

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
        cfg = config or {}
        self.enabled = bool(cfg.get("download_server_enabled", False))
        self.host = str(cfg.get("download_server_host", "127.0.0.1") or "127.0.0.1")
        self.http_port = int(cfg.get("download_http_port", 0) or 0)
        self.ftp_port = int(cfg.get("download_ftp_port", 0) or 0)
        self.token = str(cfg.get("download_token", "") or "")
        self._download_info = download_info
        self._http_server: asyncio.AbstractServer | None = None
        self._ftp_thread: threading.Thread | None = None
        self._ftp_server = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ftp_auth = ("cloud", self.token or "cloud")
        self._cache_dir = Path(tempfile.gettempdir()) / "cloudftp"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 地址 ----------

    def http_base(self) -> str:
        return f"http://{self.host}:{self.http_port}"

    def download_url(self, group_id: str, id: int) -> str:
        return (
            f"{self.http_base()}/download?group={group_id}&id={id}&token={self.token}"
        )

    def ftp_info(self) -> dict:
        return {
            "host": self.host,
            "port": self.ftp_port,
            "user": self._ftp_auth[0],
            "password": self._ftp_auth[1],
        }

    # ---------- 跨线程调用（FTP 线程 → 插件主循环） ----------

    def _run_in_loop(self, coro, timeout: float = 180.0):
        """跨线程调用：在插件主事件循环中执行协程（FTP 线程 → asyncio）。"""
        if self._loop is None:
            raise RuntimeError("dlserver loop not ready")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    # ---------- 生命周期 ----------

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
            if not group or not rid.isdigit():
                await self._reply(writer, 400, b"bad request")
                return
            src, name = await self._download_info(group, int(rid))
            src_path = Path(src)
            if not src_path.exists():
                # 单文件：302 跳转 QQ CDN 直链（零代理负载）
                body = (
                    f"HTTP/1.1 302 Found\r\nLocation: {src}\r\n"
                    f"Content-Length: 0\r\nConnection: close\r\n\r\n"
                ).encode("latin-1")
                writer.write(body)
                await writer.drain()
                writer.close()
                return
            # 分卷/视频：流式返回本地重组文件
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

    # ---------- FTP（pyftpdlib 虚拟文件系统：/<群号>/<文件名>） ----------

    def _stat(self, path: str) -> dict:
        parts = [p for p in path.split("/") if p]
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
                if path not in ("/", ""):
                    return []

                async def _groups():
                    groups = await svc.store.list_groups()
                    return [str(g.group_id) for g in groups]

                try:
                    return svc._run(_groups)
                except Exception:
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
